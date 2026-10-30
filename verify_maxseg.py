#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：RVC 的「外层子进程分段」（--maxseg）必须真的按设计干活。

为什么需要这一关
----------------
 findings（2026-10-22 晚，全是实测）：
   · 同一段素材 X，单独跑 devIQR 17.2；塞进 200 秒里不分段跑 25.4，**推理 784.6 秒**；
     切成 7 段各起一个子进程跑 devIQR **16.7**，**131.0 秒** —— 又快又好。
   · 关键是：改成同进程分段**不管用**。实测三段耗时 7.4 / 4.0 / **119.5** 秒 ——
     决定快慢的不是这一段有多长，是这个进程之前已经干了多少活。
     ⇒ 所以必须**跨进程**，而且父进程自己一个模型都不许加载。

这 locks 住的就是这几条。任何一条被改回去，这里必须变红。
"""
import importlib.util
import os
import re
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(HERE, "stages", "rvc_infer.py")

N_PASS = 0
FAILS = []


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("  [!!] %s  %s" % (name, detail))


def read_stage():
    with open(STAGE, encoding="utf-8") as f:
        return f.read()


def load_mod():
    """把 stages/rvc_infer.py 当模块加载（它的顶层不 import RVC 的东西，安全）。"""
    spec = importlib.util.spec_from_file_location("rvc_infer_under_test", STAGE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ------------------------------------------------------------ A. plan_cuts 行为
def a_cuts():
    print("\nA. plan_cuts：切得动 / 切多长 / 切在哪")
    import numpy as np

    mod = load_mod()
    sr = 16000
    # 造一段：唱 2.5 秒、换气 0.5 秒，循环 —— 换气的密度按真实歌来（人声差不多
    #   就是这个频率），不是每 10 秒才喘一次。
    #   ★ 别把间隙搞太稀：搜索窗只有 ±3 秒，间隙间隔一旦超过它的两倍，
    #     窗里常常一个安静点都没有，plan_cuts 就只能退而求其次挑最不响的那帧，
    #     那时**错的不是实现，是这份测试素材**。
    tone = (0.6 * np.sin(2 * np.pi * 300 * np.arange(int(2.5 * sr)) / sr)).astype("float32")
    gap = np.zeros(int(0.5 * sr), dtype="float32")
    y = np.concatenate([np.concatenate([tone, gap]) for _ in range(40)])
    total = len(y) / float(sr)
    print("   测试素材 %.0f 秒（每 %.1f 秒处有 %.1f 秒换气）" % (total, 3.0, 0.5))

    check("短音频（<=maxseg）不切", mod.plan_cuts(y[:int(20 * sr)], 30.0) == [])

    cuts = mod.plan_cuts(y, 30.0)
    print("   切点：%s" % ", ".join("%.2f" % c for c in cuts))
    bounds = [0.0] + cuts + [total]
    segs = [bounds[i + 1] - bounds[i] for i in range(len(bounds) - 1)]
    check("切开了（%.0f 秒 / maxseg 30 ⇒ 至少 4 段）" % total, len(cuts) >= 3,
          "实际 %d 段" % len(segs))
    check("每段都不超过 maxseg（留 3.1 秒找切点的余量）", max(segs) <= 33.1,
          "最长 %.2f 秒" % max(segs))
    check("切点递增且不越界",
          all(bounds[i] < bounds[i + 1] for i in range(len(bounds) - 1))
          and cuts == sorted(cuts) and cuts[0] > 0 and cuts[-1] < total)

    # ★ 形状：能量 mustn't 落在响的那 9 秒中间
    def rms_at(t):
        s = y[int(t * sr):int((t + 0.25) * sr)]
        return float(np.sqrt((s.astype("float64") ** 2).mean())) if len(s) else -1.0

    loud_pts = [c for c in cuts if rms_at(c) > 0.05]
    check("每个切点都落在安静处（rms<0.05）—— 别把音符从中间劈开",
          not loud_pts, "落在响处的切点：%s" % loud_pts)

    # ★ 负对照：如果把「找能量最小」换成图省事的等分，上面那条必须判红
    def naive_cuts(y16, maxseg, search=3.0):
        tt = len(y16) / 16000.0
        n = max(1, int(-(-tt // maxseg)))
        return [round(i * tt / n, 3) for i in range(1, n)]

    naive = naive_cuts(y, 30.0)
    naive_loud = [c for c in naive if rms_at(c) > 0.05]
    check("负对照：换成等分（不看能量）时会有切点落在响处 ⇒ 上面那条真有牙",
          len(naive_loud) > 0, "等分切点里落在响处的有 %d 个" % len(naive_loud))

    check("maxseg<=0 视为关闭", mod.plan_cuts(y, 0) == [])


# ------------------------------------------------------- B. 子进程命令完整性
def b_cmd():
    print("\nB. child_cmd：子进程必须拿到**全部**会改变结果的参数")
    mod = load_mod()
    a = types.SimpleNamespace(
        model="m.pth", pitch=3, f0_method="rmvpe", index_rate=0.5,
        rms_mix_rate=0.25, protect=0.33, speaker_id=0, resample_sr=0, chunk=20.0,
        index="")
    # ★ 故意让 a.index 为空、真正用挑出来的那个 path —— 这是最容易写错的地方
    cmd = mod.child_cmd(a, "/tmp/picked.index", "in.wav", "out.wav")
    joined = " ".join(cmd)

    for flag, val in [("--model", "m.pth"), ("--input", "in.wav"), ("--output", "out.wav"),
                      ("--pitch", "3"), ("--f0", "rmvpe"), ("--index-rate", "0.5"),
                      ("--rms-mix-rate", "0.25"), ("--protect", "0.33"),
                      ("--speaker-id", "0"), ("--resample-sr", "0"), ("--chunk", "20.0")]:
        i = cmd.index(flag)
        check(" %s 传下去了" % flag, cmd[i + 1] == val, "%s=%s" % (flag, cmd[i + 1]))

    check("★ 索引传的是挑选后的那个（不是空的 --index）",
          cmd[cmd.index("--index") + 1] == "/tmp/picked.index")
    check("★ 子进程的 --maxseg 被钉成 0（子不该再切，否则无限递归）",
          cmd[cmd.index("--maxseg") + 1] == "0")
    check("子进程用同一个脚本、同一个解释器",
          cmd[0] == sys.executable and cmd[1] == "-s"
          and os.path.basename(cmd[2]) == "rvc_infer.py")
    # 负对照：手滑用 a.index 的话，判据必须红
    bad = mod.child_cmd(a, "", "in.wav", "out.wav")
    check("负对照：传空索引时上面那条必须失效（=它有牙）",
          bad[bad.index("--index") + 1] != "/tmp/picked.index")
    del joined


# ------------------------------------------------------------- C. 静态接线
def c_wiring():
    print("\nC. 静态：几条「改回去就变慢」的约束")
    src = read_stage()

    i_load = src.index("vc = VC(config)")
    i_seg = src.index("if a.maxseg > 0")
    check("★ 分不分段的判断在**加载模型之前**（父进程一旦加载就占显存拖慢子进程）",
          i_seg < i_load, "判断@%d 加载@%d" % (i_seg, i_load))
    # 负对照用**最小示例**讲清楚，不靠 replace 去改真实源码 ——
    #   真实源码里有多处 vc = VC(...)，replace 掉头一个之后 index 的语义就变了，
    #   判据红得让人看不出到底是谁的错。
    good = ("if a.maxseg > 0 and _dur > a.maxseg * 1.5:\n"
            "    return finalize(a, sr, audio)\n\nvc = VC(config)\n")
    bad = ("vc = VC(config)\nvc.get_vc(model_name)\n\n"
           "if a.maxseg > 0 and _dur > a.maxseg * 1.5:\n"
           "    return finalize(a, sr, audio)\n")
    measure = lambda s: s.index("a.maxseg > 0") < s.index("vc = VC(config)")
    check("负对照：判断挪到加载之后时上面那条失效 ⇒ 它有牙",
          measure(good) and not measure(bad),
          "good=%s bad=%s" % (measure(good), measure(bad)))

    check("★ 门槛是 maxseg 的 1.5 倍（刚过线就切开是不划算的）",
          bool(re.search(r"_dur\s*>\s*a\.maxseg\s*\*\s*1\.5", src)))

    check("★ 子进程 stdout 是**实时转发**（用了 Popen，不是吞到最后的 run）",
          "subprocess.Popen(" in src and "for ln in proc.stdout" in src)
    check("负对照：换成 run 会失去实时转发（上面判据把 run 排除在外）",
          "subprocess.run(child_cmd" not in src)

    check("默认就开着（default=30，不是 0）",
          bool(re.search(r'"--maxseg".*default\s*=\s*30\.0', src, re.S))
          or bool(re.search(r'"--maxseg",\s*type=float,\s*default=30\.0', src)))
    check("临时目录有回收（atexit + rmtree，不落垃圾）",
          "atexit.register(shutil.rmtree" in src and "tempfile.mkdtemp" in src)

    # ★ 理由必须落在代码里：下一个人看到"分段"很容易顺手改回同进程
    #   （写起来更简单），所以那段实测数字本身就是 deliverable 的一部分。
    i_doc = src.index("def convert_segmented")
    doc = src[i_doc:src.index("import subprocess", i_doc)]
    check("convert_segmented 注释里写明了「为什么必须跨进程」+ 实测数字",
          "119.5" in doc and ("同进程" in doc or "一个干净进程" in doc),
          "长度 %d 字" % len(doc))
    check("负对照：把实测数字删掉后上面那条失效（=它在查真东西）",
          "119.5" in src and "119.5" not in src.replace("119.5", "", 1))


# ---------------------------------------------------------------- main
def main():
    print("=" * 72)
    print("verify_maxseg —— RVC 外层子进程分段")
    print("=" * 72)
    a_cuts()
    b_cmd()
    c_wiring()
    print("\n" + "=" * 72)
    print("通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    for f in FAILS:
        print("  红：%s" % f)
    print("=" * 72)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
