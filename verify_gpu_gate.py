# -*- coding: utf-8 -*-
r"""门禁：显卡闸门 —— 同一时刻只允许一个任务占显卡，后来的排队（不是抢）。

为什么单开一关（真事故，2026-10-22）：
    同一首歌（198 秒）被提交两次，界面上两个任务都显示"歌声转换"：

        · 两个 RVC 进程同时跑同一张 8G 卡：显存 7789 / 8151 MiB（96%），
          日志里出现 `CUDA error: an illegal memory access`（+12 半音那次体检
          直接退出码 1）；
        · 两个进程工作集 5.7G + 7.0G，把 31.6G 物理内存吃到只剩 0.2G ⇒ 整机 swap；
        · 结果：按 RVC_EST 本该 ~5 分钟（298 秒）的一步，跑了 26 分钟还没出来。

    根因是**没有闸门**：每个任务一个线程，谁先跑谁占卡，后来者只会把两个
    一起拖慢。所以这里钉三件事：
      A. 占卡步骤（RVC 推理 / UVR 分离）真的串行 —— 用真调用（把 studio.run
         换成 sleep 的假实现）验证时间区间**不重叠**；
      B. 排队期间界面说人话：写"排队等显卡 + 排在第几位"，否则用户看着转圈
         又会再点一次 —— 那是这次事故的自我强化环节；
      C. 排队中能被取消，且队列不残留。

    外加两条源码级保证（防后人把闸门摘了）：
      D. rvc_convert / _uvr_run 体内必须出现 gpu_slot；
      E. server.py 的"一键唱"入口必须有重复提交拦截（同一个角色 + 同一首歌
         已经在跑时返回 409）。

    ★ 负对照：把闸门旁路成直通，再跑同一组并发 —— A 那条"区间不重叠"必须
      **变红**。不会变红的尺子等于没有尺子。

本关不碰显卡、不跑模型、不写 data/jobs（用内存里的假 job），几秒钟跑完。

退出码：0 全部通过；1 有失败。
"""
import contextlib
import inspect
import os
import shutil
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import studio  # noqa: E402

N_PASS = 0
FAILS = []


def check(name, cond, extra=""):
    global N_PASS
    if cond:
        N_PASS += 1
        print("  [PASS] %s%s" % (name, ("  (%s)" % extra) if extra else ""))
    else:
        FAILS.append(name)
        print("  [FAIL] %s%s" % (name, ("  (%s)" % extra) if extra else ""))
    return bool(cond)


# ---------------------------------------------------------------- 测试夹具

_SPANS = []
_SPAN_LOCK = threading.Lock()


def _fake_run(job, cmd, **kw):
    """顶替 studio.run：不跑真推理，只占住闸门一小会儿并记录进出时刻。"""
    t0 = time.time()
    time.sleep(0.45)
    t1 = time.time()
    with _SPAN_LOCK:
        _SPANS.append(((job or {}).get("id"), t0, t1))
    return 0, "fake"


_TMPDIR = tempfile.mkdtemp(prefix="vgg_")


def _fake_job(jid):
    """内存里的假任务：够 step_hint / job_cancelled 用，不落 data/jobs
    （落盘目标指到临时目录，本关结束就删 —— 门禁不许在仓库里留垃圾）。"""
    return {"id": jid, "kind": "sing", "role": "rtest", "state": "running",
            "steps": [], "_step_idx": -1, "stage": "", "detail": "",
            "dir": _TMPDIR, "_logfile": os.path.join(_TMPDIR, jid + ".log")}


# B 关要抓"排队中那个任务"的展示内容，用这个容器把 job 递出来
_WAIT_JOB = [{}]


def _overlap(spans):
    """任意两段区间是否重叠（重叠 = 两个任务同时在占卡）。"""
    ss = sorted(spans, key=lambda x: x[1])
    for i in range(len(ss) - 1):
        if ss[i + 1][1] < ss[i][2] - 1e-6:
            return True, (ss[i], ss[i + 1])
    return False, None


@contextlib.contextmanager
def _no_gate(job, what):
    """负对照用：直通"闸门"，什么也不拦。"""
    yield


def _concurrent(use_gate, n=2, hold=0.45):
    """起 n 个线程各跑一次 studio.rvc_convert（run 被换成 sleep），返回区间表。"""
    global _SPANS
    _SPANS = []
    old_run, old_gate = studio.run, studio.gpu_slot
    studio.run = _fake_run
    if not use_gate:
        studio.gpu_slot = _no_gate
    try:
        ths = []
        for i in range(n):
            j = _fake_job("g%d" % i)

            def _go(jj=j):
                try:
                    studio.rvc_convert(jj, "m.pth", "", "in.wav", "out.wav", 0, 0.5)
                except Exception:
                    pass

            ths.append(threading.Thread(target=_go))
        for t in ths:
            t.start()
        for t in ths:
            t.join(30)
        with _SPAN_LOCK:
            return list(_SPANS)
    finally:
        studio.run = old_run
        studio.gpu_slot = old_gate


# ---------------------------------------------------------------- A. 真串行


def a_serial():
    print("\nA. 占卡步骤必须串行（真调用，run 换成 sleep）")
    spans = _concurrent(use_gate=True, n=2)
    check("两次占卡都真的跑到了", len(spans) == 2, "区间数 %d" % len(spans))
    ov, pair = _overlap(spans)
    check("有闸门时两个任务的时间区间**不重叠**（串行）", not ov,
          "重叠段 %s" % (pair,) if ov else "间隔 %.2fs" % (
              abs(spans[0][2] - spans[1][1]) if len(spans) == 2 else -1))
    if len(spans) == 2:
        total = max(s[2] for s in spans) - min(s[1] for s in spans)
        check("串行总时长 ≈ 两次之和（没有退化成排队 + 死锁）",
              total < 0.45 * 2 * 1.8, "总时长 %.2fs" % total)


# ---------------------------------------------------------------- B. 排队可见


def b_queue_visible():
    print("\nB. 排队期间界面必须说人话（否则用户会再点一次）")
    seen = {"stage": "", "detail": "", "waiting": None}
    _WAIT_JOB[0] = {}

    def _holder():
        with studio.gpu_slot(_fake_job("hold"), "歌声转换（RVC）"):
            time.sleep(1.2)

    def _waiter():
        j = _fake_job("wait")
        _WAIT_JOB[0] = j
        try:
            with studio.gpu_slot(j, "歌声转换（RVC）"):
                pass
        except Exception:
            pass

    t1 = threading.Thread(target=_holder)
    t1.start()
    time.sleep(0.25)
    t2 = threading.Thread(target=_waiter)
    t2.start()
    # 等的过程中抓一次排队任务的展示内容
    for _ in range(20):
        time.sleep(0.05)
        st = studio.gpu_gate_stats()
        if st.get("waiting"):
            seen["waiting"] = list(st["waiting"])
            seen["stage"] = _WAIT_JOB[0].get("stage", "")
            seen["detail"] = _WAIT_JOB[0].get("detail", "")
            break
    t1.join(15)
    t2.join(15)

    check("等待中的任务会被登记进队列", seen["waiting"] == ["wait"],
          "waiting=%s" % (seen["waiting"],))
    check("排队时 stage 写成「排队等显卡」", seen["stage"] == "排队等显卡",
          "stage=%r" % seen["stage"])
    check("排队时 detail 说清自己排第几个（「第 1 个在等的」= 下一个）",
          "你是第 1 个在等的" in seen["detail"], (seen["detail"] or "")[:60])
    check("排队提示里说清「抢卡会更慢」（掐断再点一次的冲动）",
          "抢" in seen["detail"] and "慢" in seen["detail"],
          (seen["detail"] or "")[:60])
    check("运行结束后闸门名额被放掉（holder 清空）",
          studio.gpu_gate_stats().get("holder") in (None, ""),
          "holder=%s" % studio.gpu_gate_stats().get("holder"))
    check("运行结束后队列为空（不留残影）",
          studio.gpu_gate_stats().get("waiting") == [],
          "waiting=%s" % studio.gpu_gate_stats().get("waiting"))


# ---------------------------------------------------------------- C. 可取消


def c_cancel():
    print("\nC. 排队中要能取消，且不留残影")
    got = {"raised": False}

    def _holder():
        with studio.gpu_slot(_fake_job("hold2"), "歌声转换（RVC）"):
            time.sleep(1.5)

    def _waiter():
        j = _fake_job("wait2")
        with studio.CTRL_LOCK:
            studio.CTRL["wait2"] = {"cancelled": True, "pids": [], "seen_pids": []}
        try:
            with studio.gpu_slot(j, "歌声转换（RVC）"):
                pass
        except studio.Cancelled:
            got["raised"] = True
        except Exception as e:
            got["raised"] = "别的异常: %r" % e

    t1 = threading.Thread(target=_holder)
    t1.start()
    time.sleep(0.3)
    t2 = threading.Thread(target=_waiter)
    t2.start()
    t2.join(15)
    t1.join(15)
    with studio.CTRL_LOCK:
        studio.CTRL.pop("wait2", None)

    check("排队的任务被取消时立刻抛 Cancelled（不等到天荒地老）",
          got["raised"] is True, "raised=%r" % got["raised"])
    check("取消后队列里不留这个任务",
          "wait2" not in (studio.gpu_gate_stats().get("waiting") or []),
          "waiting=%s" % studio.gpu_gate_stats().get("waiting"))


# ---------------------------------------------------------------- D/E 源码


def d_source():
    print("\nD. 源码级：两个占卡入口都必须真的过闸门")
    try:
        s1 = inspect.getsource(studio.rvc_convert)
        s2 = inspect.getsource(studio._uvr_run)
        # 2026-10-26 加了 MSST 这条新通路（第二种分离框架），它同样占 cuda。
        # 只查 _uvr_run 的话，将来有人把 gpu_slot 从 _msst_run 里删掉，
        # 这条闸门照样全绿 —— 那就又是把尺子配错了人。
        s3 = inspect.getsource(studio._msst_run)
    except Exception as e:
        check("能取到三个占卡函数的源码", False, repr(e))
        return
    check("rvc_convert 体内过 gpu_slot", "gpu_slot(" in s1)
    check("_uvr_run 体内过 gpu_slot", "gpu_slot(" in s2)
    check("★ _msst_run 体内也要过 gpu_slot（新加的第二条分离通路同样占卡）",
          "gpu_slot(" in s3)
    src = inspect.getsource(studio)
    check("闸门用的是可重入锁（嵌套调用不会把自己锁死）",
          "_GPU_GATE = threading.RLock()" in src, "")


def e_dup_guard():
    print("\nE. 源码级：一键唱入口必须拦重复提交")
    p = os.path.join(HERE, "server.py")
    try:
        txt = open(p, "r", encoding="utf-8", errors="replace").read()
    except Exception as ex:
        check("能读到 server.py", False, repr(ex))
        return
    check("有「已经在唱这首歌」的提示文案", "已经在唱这首歌了" in txt)
    check("重复提交返回 409（前端能据此提示，而不是静默再开一个）",
          '"job": studio.job_public(_dup)}, 409' in txt)
    check("拦截条件里认了 running / queued 两种状态",
          '"running", "queued"' in txt)


# ---------------------------------------------------------------- 负对照


def f_negative():
    print("\nF. 负对照：闸门旁路后，同一组并发必须**重叠**（证明 A 那条有牙）")
    spans = _concurrent(use_gate=False, n=2)
    check("旁路闸门后仍跑满两次", len(spans) == 2, "区间数 %d" % len(spans))
    ov, pair = _overlap(spans)
    check("★ 没有闸门时区间**重叠**（判据 A 会变红 → 尺子有牙）", ov,
          "重叠 %s" % (pair,) if ov else "竟然没重叠，说明 A 那条判据是假绿")


# ---------------------------------------------------------------- 文案一致


def g_copy():
    print("\nG. 文案：一步不许同时给出两个差 3 倍的预期")
    try:
        txt = open(os.path.join(HERE, "studio.py"), "r", encoding="utf-8",
                   errors="replace").read()
    except Exception as ex:
        check("能读到 studio.py", False, repr(ex))
        return
    check("旧的「十几分钟」口径已删（那是 20 秒分片之前的数）",
          "本机 3 分 45 秒的歌实测要十几分钟" not in txt)
    check("歌声转换的提示改用 RVC_EST 同一口径",
          "rvc_est_seconds(voice_secs)" in txt)
    check("超时提示说明「多半是有别的任务在抢卡」",
          "有别的任务在抢卡" in txt)


def main():
    print("显卡闸门门禁：不许两个任务抢同一张卡（2026-10-22 事故回归）")
    a_serial()
    b_queue_visible()
    c_cancel()
    d_source()
    e_dup_guard()
    f_negative()
    g_copy()
    shutil.rmtree(_TMPDIR, ignore_errors=True)
    print("\n通过 %d / 失败 %d" % (N_PASS, len(FAILS)))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    print("ALLDONE")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
