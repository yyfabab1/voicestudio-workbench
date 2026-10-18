r"""门禁：网易云 .ncm **自动解密守护**（ncm_watch.py）。

为什么单开一关：它坏起来**一点动静都没有** ——
  · 监视目录探测错了 ⇒ 守护一直空转，用户下载完的歌永远不进曲库（界面照常能用，
    只是"新歌怎么还没出现"，很难联想到守护）；
  · 稳定性判定写错（文件还在写就动手）⇒ 解出来是半截噪音，而且**半截文件
    不会被重试**（sig 已经记进 done），这首歌就永久坏了；
  · 幂等失效 ⇒ 每一轮都重解一遍，一首 10MB 的歌每 6 秒写一次，几小时吃满磁盘；
  · 走本地直写而绕过 server ⇒ 和 server 抢写 library.json，用户刚导入的歌会丢。
既有 16 关一条都不会红 ⇒ 必须专门盯。

判据四段（★ = 负对照，换坏版本必须变红）：
  A 目录探测：真的盯着网易云 VIP 目录；only 模式不会顺手扫真实目录
  B --once 真解密：解出来是能播的 mp3/flac、进了曲库、原 .ncm 一个字节没动
  C 幂等 / 稳定性 / 坏文件：重跑不多解一份、文件还在变时不动手、坏文件要报错
  D 安装：开机自启 vbs 写对、能卸掉

用法（venv 跑，跟其它关卡同一个解释器）：
    <venv>/Scripts/python.exe -s verify_ncmwatch.py
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "stages"))
import ncm_watch  # noqa: E402
import studio  # noqa: E402

PY = sys.executable
SCRIPT = os.path.join(HERE, "ncm_watch.py")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run(args, state_dir, secs=120):
    """跑一次 ncm_watch.py。状态目录换到临时目录（绝不污染真实 data/）。"""
    env = dict(os.environ)
    env["NCM_WATCH_DATA"] = state_dir
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([PY, "-s", SCRIPT] + list(args), cwd=HERE, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=secs)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def watch_line(out):
    for ln in (out or "").splitlines():
        if ln.startswith("WATCH "):
            d = {}
            for kv in ln.split()[1:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    try:
                        d[k] = int(v)
                    except ValueError:
                        d[k] = v
            return d
    return {}


def find_ncm():
    """找一个真 .ncm 当测试样本（本机 VIP 目录下就有 3 首）。"""
    for d in ncm_watch.auto_dirs():
        for p in sorted(glob.glob(os.path.join(d, "*.ncm"))):
            if os.path.getsize(p) > 100 * 1024:
                return p
    return None


# ---------------------------------------------------------------- A 目录探测

def section_a():
    print("\n=== A. 监视目录探测 ===")
    dirs = ncm_watch.auto_dirs()
    # ★ 判据钉的是**真契约**：守护监视的目录里必须真的躺着 .ncm。
    #
    #   以前钉的是"目录名必须叫 VipSongsDownload" —— 那只是实现细节。
    #   探测改成"下载根目录优先、父子目录去重"之后，`netease_dirs()` 返回的是
    #   `D:\CloudMusic`（它涵盖 VIP 子目录），根目录顶层一个 .ncm 都没有。
    #
    #   而守护的 scan_once 是 `os.listdir(d)` —— **非递归**。把根目录喂给它，
    #   子目录里新下载的 VIP 歌就永远看不见 ⇒ 自动解密**静默失效**
    #   （守护照常在跑、日志照常"扫过 N 个目录"，什么都没发生）。
    #   ⇒ 所以 auto_dirs() 必须用 local_lib.ncm_dirs()（精确到躺着 .ncm 的
    #     子目录）。这条断言现在正好守住"别再把根目录喂给守护"。
    hits = []
    for d in dirs:
        try:
            n = len([f for f in os.listdir(d) if f.lower().endswith(".ncm")])
        except Exception:
            n = 0
        if n:
            hits.append((d, n))
    check("★ 守护监视的目录里真的躺着 .ncm（精确到子目录：根目录顶层是没有的）",
          bool(hits), "监视=%s 命中=%s" % (dirs, hits))
    check("★ 其中含网易云 VIP 下载目录（VIP 歌下完才会被自动解密）",
          any("vipsongsdownload" in (d or "").lower() for d, _ in hits),
          [d for d, _ in hits])

    st = ncm_watch.status(ncm_watch.watch_dirs({"dirs": [], "interval": 6}))
    check("--status 说得清：在不在跑 / 监视哪几个目录 / 已解多少首",
          "监视目录" in st and "已解密登记" in st, st.splitlines()[0])

    tmp = os.path.join(HERE, "_ncmwatch_probe_dir")
    os.makedirs(tmp, exist_ok=True)
    try:
        only = ncm_watch.watch_dirs({"dirs": [tmp]}, only=True)
        both = ncm_watch.watch_dirs({"dirs": [tmp]}, only=False)
        check("★ only 模式只盯指定目录（门禁/排错不会顺手把真实下载目录扫一遍）",
              only == [tmp] and len(both) >= len(only),
              "only=%s 全量=%d 个目录" % (only, len(both)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- B/C 真解密

def section_bc(src):
    print("\n=== B/C. --once 真解密 / 幂等 / 稳定性 / 坏文件 ===")
    if not src:
        check("本机找得到一个真 .ncm 当样本（否则本关无从谈起）", False)
        return
    tmp = tempfile.mkdtemp(prefix="vs_ncmwatch_")
    state = os.path.join(tmp, "state")
    wdir = os.path.join(tmp, "dl")
    os.makedirs(state)
    os.makedirs(wdir)
    try:
        before = md5(src)
        t0 = time.time()
        before_paths = set(s.get("path") or "" for s in studio.load_library().get("songs", []))
        lib0 = len(before_paths)

        # ---- B1 首次：解出来并登记进曲库
        dst = os.path.join(wdir, os.path.basename(src))
        shutil.copyfile(src, dst)
        rc1, out1 = run(["--once", "--only", "--dir", wdir, "--interval", "2"], state)
        w1 = watch_line(out1)
        check("--once：新出现的一首被解密（WATCH 行报 ok=1）",
              rc1 == 0 and w1.get("ok") == 1 and w1.get("fail") == 0,
              "rc=%s %s" % (rc1, (out1.strip().splitlines() or [""])[-1]))

        lib = studio.load_library().get("songs", [])
        hit = [s for s in lib if (s.get("from_ncm") or "").replace("/", "\\")
               == dst.replace("/", "\\")]
        check("解密产物登记进曲库，并记下来历（from_ncm 指向原文件）",
              len(hit) == 1 and os.path.isfile(hit[0].get("path") or "")
              and (hit[0].get("dur") or 0) > 30.0,
              "条数=%s 时长=%s" % (len(hit), hit and hit[0].get("dur")))
        check("产物是标准音频（扩展名 mp3/flac 且体积合理）",
              bool(hit) and os.path.splitext(hit[0]["path"])[1].lower() in (".mp3", ".flac")
              and os.path.getsize(hit[0]["path"]) > 100 * 1024,
              hit and hit[0]["path"])
        check("★ 原 .ncm 一个字节都没动（解密是只读操作）",
              md5(src) == before and md5(dst) == before,
              "原文件 md5 %s" % before[:12])

        # ---- C1 幂等：再跑一次，一首都不许多解
        rc2, out2 = run(["--once", "--only", "--dir", wdir, "--interval", "2"], state)
        w2 = watch_line(out2)
        lib2 = studio.load_library().get("songs", [])
        n_same = len([s for s in lib2 if (s.get("path") or "") == (hit[0]["path"] if hit else "")])
        check("★ 幂等：同一首再跑一次不再解第二份（new=0，曲库不重复登记）",
              rc2 == 0 and w2.get("new") == 0 and n_same <= 1,
              "new=%s 曲库同路径条数=%s" % (w2.get("new"), n_same))

        # ---- C2 稳定性：文件还在变（下载中）就不许动手
        wdir2 = os.path.join(tmp, "dl2")
        os.makedirs(wdir2)
        st2 = {"done": ncm_watch.load_done(), "pend": {}}
        env_state = os.path.join(tmp, "state2")
        os.makedirs(env_state, exist_ok=True)
        ncm_watch.DONE_PATH = os.path.join(env_state, "ncm_watch_done.json")
        st2 = {"done": {}, "pend": {}}
        d2 = os.path.join(wdir2, os.path.basename(src))
        shutil.copyfile(src, d2)
        r_a = ncm_watch.scan_once(st2, [wdir2], quiet=True)
        check("稳定性（1/2）：刚出现的文件先记下来，不立刻解密",
              r_a.get("ok") == 0 and r_a.get("fail") == 0 and r_a.get("new") == 0,
              r_a)
        # 假装"还在下载"：把 size/mtime 变掉
        time.sleep(0.05)
        with open(d2, "ab") as f:
            f.write(b"\x00" * 4096)
        os.utime(d2, (time.time() + 5, time.time() + 5))
        r_b = ncm_watch.scan_once(st2, [wdir2], quiet=True)
        check("★ 稳定性（2/2）：文件还在变（下载中）就不动手（解半截会永久坏一首）",
              r_b.get("ok") == 0 and r_b.get("new") == 0,
              r_b)
        r_c = ncm_watch.scan_once(st2, [wdir2], quiet=True)
        check("稳定性（收尾）：稳定之后这一轮才真的解出来",
              r_c.get("ok") == 1, r_c)
        ncm_watch.DONE_PATH = os.path.join(HERE, "data", "ncm_watch_done.json")

        # ---- C3 坏文件：必须报错（退出码非 0），且重试到上限后不再纠缠
        bad = os.path.join(wdir2, "坏文件.ncm")
        with open(bad, "wb") as f:
            f.write(b"this is not an ncm file at all")
        rc3, out3 = run(["--once", "--only", "--dir", wdir2, "--interval", "1"], state,
                        secs=120)
        w3 = watch_line(out3)
        check("★ 坏文件：报失败且退出码非 0（不是悄悄跳过当没看见）",
              rc3 != 0 and (w3.get("fail") or 0) >= 1,
              "rc=%s fail=%s" % (rc3, w3.get("fail")))

        lib3 = studio.load_library().get("songs", [])
        check("（附带）跑完这一圈曲库没被写坏：条目数没莫名暴涨",
              len(lib3) - lib0 <= 3, "新增 %d 条（含本次解密产物）" % (len(lib3) - lib0))

        # ---- 收尾：把本关自己造出来的曲库条目 + 产物**清干净**
        # ★ 门禁也是污染源（铁律 12b）。同一首歌从不同临时路径解出来会在曲库里
        #   留下「Côme」「Côme (2)」「Côme (3)」——用户看着就是"怎么重复了"。
        #   只删**本关新增的**（不在开跑前那批里），老的一条都不碰。
        kept, dropped = [], 0
        for s in studio.load_library().get("songs", []):
            p = s.get("path") or ""
            if p in before_paths:          # 开跑前就在库里 ⇒ 一律不动
                kept.append(s)
                continue
            try:
                fresh_file = os.path.isfile(p) and os.path.getmtime(p) >= t0 - 2
            except Exception:
                fresh_file = False
            if not fresh_file:             # 不是本关这会儿写出来的 ⇒ 不是本关造的
                kept.append(s)
                continue
            try:
                os.remove(p)
                dropped += 1               # 条目跟着文件一起消失
            except Exception:
                kept.append(s)
        db = studio.load_library()
        db["songs"] = kept
        studio.save_library(db)
        print("    [清理] 本关产生的 %d 个测试条目已从曲库移除" % dropped)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- D 安装

def section_d():
    print("\n=== D. 开机自启的装卸（用临时启动目录，不动用户真装的那份） ===")
    tmp = tempfile.mkdtemp(prefix="vs_ncmwatch_inst_")
    old_startup = ncm_watch.STARTUP
    old_pid = ncm_watch.PID_PATH
    try:
        ncm_watch.STARTUP = tmp
        ncm_watch.PID_PATH = os.path.join(tmp, "pid")
        msg = ncm_watch.install()
        vbs = os.path.join(tmp, ncm_watch.VBS_NAME)
        body = ""
        if os.path.isfile(vbs):
            with open(vbs, "r", encoding="mbcs", errors="replace") as f:
                body = f.read()
        check("装开机自启：启动目录里出现 vbs，内容指向 pythonw + 脚本 + --serve",
              os.path.isfile(vbs) and "pythonw" in body.lower()
              and "ncm_watch.py" in body and "--serve" in body,
              (body or msg).replace("\n", " | ")[:160])
        check("vbs 里的脚本路径是绝对路径（开机时工作目录不是这里）",
              os.path.abspath(SCRIPT).lower() in body.lower(),
              os.path.abspath(SCRIPT))
        msg2 = ncm_watch.uninstall()
        check("★ 负对照：卸掉之后 vbs 真的没了",
              not os.path.isfile(vbs) and "已去掉" in msg2, msg2)
    finally:
        ncm_watch.STARTUP = old_startup
        ncm_watch.PID_PATH = old_pid
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    os.chdir(HERE)
    print("门禁：.ncm 自动解密守护（ncm_watch.py）")
    src = find_ncm()
    section_a()
    section_bc(src)
    section_d()
    print("-" * 62)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 失败项：" + "、".join(BAD))
    else:
        print("结论：ncm 自动解密门禁全绿")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
