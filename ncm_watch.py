r"""
网易云 .ncm **下载完自动解密**：挂着这个守护，客户端一下完歌，它就把 .ncm
解成标准 mp3/flac 并登记进 VoiceStudio 曲库 —— 你不用再手动点「解密并翻唱」。

------------------------------------------------------------------ 边界
跟 ncm.py 一样（那句边界在这里同样适用）：本脚本只处理**你自己机器上已经
下载好的文件**，不下载、不搜索、不碰网易云账号，也不提供任何"绕过付费"
的能力。硬盘上没有 .ncm，它就安静地等着。

------------------------------------------------------------------ 用法
  python ncm_watch.py                 # 常驻守护（前台，日志同时写文件）
  python ncm_watch.py --start         # 后台起守护（无窗口）并退出
  python ncm_watch.py --once          # 只扫一遍就退出（门禁 / 手动补一次）
  python ncm_watch.py --status        # 看守护在不在、监视哪些目录、已解多少
  python ncm_watch.py --stop          # 停掉后台守护
  python ncm_watch.py --install       # 装开机自启（启动文件夹）+ 立刻起一个
  python ncm_watch.py --uninstall     # 去掉开机自启
  python ncm_watch.py --dir "D:\\某目录" [--dir ...]   # 追加监视目录（写进配置）
  python ncm_watch.py --interval 6    # 轮询秒数（默认 6）

------------------------------------------------------------------ 设计要点
1) **只解"下载完的"**。文件还在写时 size/mtime 一直在变，这时解必是半截。
   判据：连续 **2 次**轮询（默认 12 秒）size+mtime 都不变才动它。
2) **幂等**。已解过的记在 data/ncm_watch_done.json（记 size+mtime），
   原文件没变就绝不重复解 —— 一首歌解一份（~10MB），重复解会吃盘。
3) **优先走 server 的 HTTP 接口**（127.0.0.1:8756）：server 活着时曲库是它
   在管，直接改文件会和它抢写。server 没起才回落到本地 local_lib.add_files。
4) **原文件一个字节都不动**。解密产物落在 data/music 下。
"""

import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "stages"))

# NCM_WATCH_DATA 可以整个换掉状态目录（门禁用它隔离，不去动真实曲库/已解清单）
DATA = os.path.abspath(os.environ.get("NCM_WATCH_DATA")
                       or os.path.join(HERE, "data"))
LOG_PATH = os.path.join(DATA, "ncm_watch.log")
DONE_PATH = os.path.join(DATA, "ncm_watch_done.json")
CFG_PATH = os.path.join(DATA, "ncm_watch.json")
PID_PATH = os.path.join(DATA, "ncm_watch.pid")

PORT = 8756
DEFAULT_INTERVAL = 6
STABLE_ROUNDS = 2        # 连续几次 size+mtime 不变才算"下载完了"
MAX_RETRY = 3            # 同一个文件失败最多重试几次（之后等它再变）
LOG_MAX_BYTES = 512 * 1024

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# ---------------------------------------------------------------- 配置

def load_cfg():
    cfg = {"dirs": [], "interval": DEFAULT_INTERVAL}
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            got = json.load(f)
        if isinstance(got, dict):
            cfg.update({k: v for k, v in got.items() if k in ("dirs", "interval")})
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    os.makedirs(DATA, exist_ok=True)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=1)


def auto_dirs():
    """网易云把歌下在哪（复用曲库那套探测，别在这儿重复造一份答案）。"""
    out = []
    try:
        import local_lib
        # ★ 用 ncm_dirs()（**精确到躺着 .ncm 的子目录**），
        #   不是 netease_dirs()（那是给用户看的下载**根目录**）。
        #   本守护的 scan_once 是 os.listdir 非递归盯着这几个目录的 ——
        #   喂它一个根目录，子目录里新下载的 VIP 歌就永远看不见，
        #   自动解密静默失效（守护照常在跑、照常打日志，什么都没发生）。
        for p in local_lib.ncm_dirs():
            out.append(p)
    except Exception:
        pass
    for p in (os.path.expandvars(r"%USERPROFILE%\Music\CloudMusic"),
              os.path.expandvars(r"%USERPROFILE%\Downloads")):
        if os.path.isdir(p):
            out.append(os.path.abspath(p))
    seen, uniq = set(), []
    for p in out:
        p = os.path.abspath(p)
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def watch_dirs(cfg, only=False):
    """监视哪些目录。

    only=True 时只看 --dir 指定的那几个（门禁/排错用：不然一扫就把真实下载
    目录里所有老歌都补一遍，测试污染真实曲库）。"""
    ds = list(cfg.get("dirs") or [])
    if not only:
        for p in auto_dirs():
            if p not in ds:
                ds.append(p)
    return [p for p in ds if os.path.isdir(p)]


# ---------------------------------------------------------------- 日志

def log(msg):
    line = "%s  %s" % (time.strftime("%m-%d %H:%M:%S"), msg)
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    try:
        os.makedirs(DATA, exist_ok=True)
        if os.path.isfile(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
                keep = f.readlines()[-400:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(keep)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 状态

def load_done():
    try:
        with open(DONE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_done(d):
    try:
        os.makedirs(DATA, exist_ok=True)
        tmp = DONE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, DONE_PATH)
    except Exception as e:
        log("  写已解清单失败：%s" % e)


def sig_of(path):
    try:
        st = os.stat(path)
        return [int(st.st_size), int(st.st_mtime)]
    except Exception:
        return None


# ---------------------------------------------------------------- 干活

def via_server(paths, port=PORT):
    """让 server 去解（它在管曲库，抢写会丢数据）。"""
    import urllib.request
    body = json.dumps({"paths": paths}).encode("utf-8")
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/music/local/decrypt" % port,
        data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode("utf-8"))


def via_local(paths):
    import local_lib
    return local_lib.add_files(paths)


def handle(path):
    """解一个 .ncm 并登记进曲库。返回 (ok, 说明)。"""
    try:
        out = via_server([path])
        how = "server"
    except Exception as e1:
        try:
            out = via_local([path])
            how = "本地（server 没起：%s）" % str(e1)[:60]
        except Exception as e2:
            return False, "server 与本地都失败：%s / %s" % (str(e1)[:80], str(e2)[:80])
    added = out.get("added") or []
    bad = out.get("bad") or []
    if bad:
        return False, str(bad[0].get("why"))[:160]
    if not added:
        return False, "曲库没登记上（返回结果为空）"
    t = added[0].get("title") or os.path.basename(path)
    return True, "%s → %s（%s）" % (t, added[0].get("path"), how)


def scan_once(state, dirs, quiet=False):
    """扫一轮：找出新出现的 / 下载完成的 .ncm 并处理。

    state: {"done": {...}, "pend": {path: {"sig": [...], "n": int, "try": int}}}
    """
    done = state["done"]
    pend = state["pend"]
    found = new = ok = fail = skip = 0
    cur = set()
    for d in dirs:
        try:
            names = os.listdir(d)
        except Exception:
            continue
        for nm in names:
            if not nm.lower().endswith(".ncm"):
                continue
            p = os.path.abspath(os.path.join(d, nm))
            cur.add(p)
            found += 1
            sig = sig_of(p)
            if not sig or sig[0] <= 0:
                continue
            if done.get(p, {}).get("sig") == sig:
                skip += 1
                continue
            if done.get(p, {}).get("sig") is not None:
                # 记过但文件变了（重新下载/换了一版）⇒ 重新解
                pass
            e = pend.get(p)
            if not e or e.get("sig") != sig:
                pend[p] = {"sig": sig, "n": 1, "try": 0}
                if not quiet:
                    log("  看到 %s（等它下载完…）" % nm)
                continue
            e["n"] += 1
            if e["n"] < STABLE_ROUNDS:
                continue
            # 稳定了 ⇒ 动手
            good, why = handle(p)
            if good:
                ok += 1
                new += 1
                log("✔ 已解密并登记：%s" % why)
                done[p] = {"sig": sig, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                           "out": why}
                pend.pop(p, None)
            else:
                e["try"] = e.get("try", 0) + 1
                fail += 1
                log("✘ 解密失败（第 %d 次）：%s —— %s" % (e["try"], nm, why))
                if e["try"] >= MAX_RETRY:
                    done[p] = {"sig": sig, "err": why,
                               "at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    pend.pop(p, None)
    for p in list(pend):
        if p not in cur:
            pend.pop(p, None)          # 文件被删了，别一直惦记
    save_done(done)
    return {"dirs": len(dirs), "found": found, "new": new, "ok": ok,
            "fail": fail, "skip": skip}


def run_once(dirs, interval=DEFAULT_INTERVAL):
    """--once 用的"扫一遍就走"：但为了等到"下载完"，至少要过 STABLE_ROUNDS 轮，
    中间隔 interval 秒。首次运行时把已存在的老文件也一并补掉（幂等，不会重解）。"""
    state = {"done": load_done(), "pend": {}}
    r = scan_once(state, dirs)
    if r["found"] and r["ok"] == 0 and r["fail"] == 0:
        time.sleep(interval)
        r2 = scan_once(state, dirs, quiet=True)
        for k in ("new", "ok", "fail", "skip"):
            r[k] += r2[k]
    return r


# ---------------------------------------------------------------- 守护

def pid_alive(pid):
    """这个 pid 还活着吗。

    ★ tasklist 在中文 Windows 上输出的是 **GBK**，用默认 utf-8 去解会在
    子进程读取线程里抛 UnicodeDecodeError（而且是在**别的线程**里抛，
    主流程看着像"命令没输出"）—— 于是"守护明明在跑"被判成"没在跑"。
    按本机控制台码页解，解不出来的直接替换掉。
    """
    enc = "gbk"
    try:
        import locale
        enc = locale.getpreferredencoding(False) or "gbk"
    except Exception:
        pass
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid],
                             capture_output=True, timeout=15)
        txt = (out.stdout or b"").decode(enc, "replace")
        return str(pid) in txt
    except Exception:
        return False


def read_pid():
    try:
        with open(PID_PATH, encoding="utf-8") as f:
            return int((f.read().strip() or "0"))
    except Exception:
        return 0


def write_pid(pid):
    try:
        os.makedirs(DATA, exist_ok=True)
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except Exception:
        pass


def start_bg():
    pid = read_pid()
    if pid and pid_alive(pid):
        return pid, "守护已经在跑（pid %d），没有重复起" % pid
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.isfile(pyw):
        py = pyw
    cf = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    try:
        p = subprocess.Popen([py, os.path.abspath(__file__), "--serve"],
                             cwd=HERE, creationflags=cf,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return 0, "起不来：%s" % e
    write_pid(p.pid)
    return p.pid, "后台守护已启动（pid %d）" % p.pid


def stop_bg():
    pid = read_pid()
    if not pid or not pid_alive(pid):
        return "没有在跑的守护"
    subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                   capture_output=True, timeout=20)
    try:
        os.remove(PID_PATH)
    except Exception:
        pass
    return "已停掉守护（pid %d）" % pid


STARTUP = os.path.expandvars(
    r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
VBS_NAME = "VoiceStudio-ncm自动解密.vbs"


def install():
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.isfile(pyw):
        py = pyw
    script = os.path.abspath(__file__)
    vbs = os.path.join(STARTUP, VBS_NAME)
    body = ('Set ws = CreateObject("WScript.Shell")\n'
            'ws.CurrentDirectory = "%s"\n'
            'ws.Run """%s"" ""%s"" --serve", 0, False\n'
            % (HERE, py, script))
    try:
        os.makedirs(STARTUP, exist_ok=True)
        with open(vbs, "w", encoding="mbcs", errors="replace") as f:
            f.write(body)
    except Exception as e:
        return "装开机自启失败：%s" % e
    pid, msg = start_bg()
    return "开机自启已装：%s\n%s" % (vbs, msg)


def uninstall():
    vbs = os.path.join(STARTUP, VBS_NAME)
    if os.path.isfile(vbs):
        try:
            os.remove(vbs)
            return "已去掉开机自启：%s" % vbs
        except Exception as e:
            return "删不掉：%s" % e
    return "本来就没装开机自启"


def status(dirs):
    pid = read_pid()
    done = load_done()
    okd = [k for k, v in done.items() if not v.get("err")]
    lines = ["守护进程：%s" % ("在跑（pid %d）" % pid if pid and pid_alive(pid)
                              else "没在跑（用 --start 起一个）"),
             "开机自启：%s" % ("已装" if os.path.isfile(os.path.join(STARTUP, VBS_NAME))
                              else "没装（用 --install 装）"),
             "监视目录（%d）：" % len(dirs)]
    for d in dirs:
        try:
            n = len([f for f in os.listdir(d) if f.lower().endswith(".ncm")])
        except Exception:
            n = 0
        lines.append("  · %s（%d 个 .ncm）" % (d, n))
    lines.append("已解密登记：%d 首" % len(okd))
    if len(done) != len(okd):
        lines.append("  失败/待重试：%d" % (len(done) - len(okd)))
    lines.append("日志：%s" % LOG_PATH)
    return "\n".join(lines)


def serve(dirs, interval):
    state = {"done": load_done(), "pend": {}}
    log("=== 守护起来了：监视 %d 个目录，每 %d 秒看一次 ===" % (len(dirs), interval))
    for d in dirs:
        log("  · %s" % d)
    write_pid(os.getpid())
    tick = 0
    while True:
        try:
            r = scan_once(state, dirs)
            if r["new"] or r["fail"]:
                log("本轮：新解 %d 首，失败 %d" % (r["new"], r["fail"]))
        except Exception as e:
            log("本轮扫描出错（继续等下一轮）：%s" % e)
        tick += 1
        if tick % 20 == 0:
            # 客户端可能装在别处/用户新加了目录，隔一阵重探一次
            for p in auto_dirs():
                if p not in dirs:
                    dirs.append(p)
                    log("  新增监视目录：%s" % p)
        time.sleep(interval)


# ---------------------------------------------------------------- main

def main(argv):
    args = argv[1:]
    cfg = load_cfg()

    def flag(name):
        return name in args

    def val(name, default=None, cast=str):
        if name in args:
            i = args.index(name)
            if i + 1 < len(args):
                try:
                    return cast(args[i + 1])
                except Exception:
                    return default
        return default

    for i, a in enumerate(args):
        if a == "--dir" and i + 1 < len(args):
            p = os.path.abspath(os.path.expanduser(args[i + 1]))
            if p not in cfg["dirs"]:
                cfg["dirs"].append(p)

    if flag("--install"):
        save_cfg(cfg)
        print(install())
        return 0
    if flag("--uninstall"):
        print(uninstall())
        return 0
    if flag("--stop"):
        print(stop_bg())
        return 0
    if flag("--start"):
        save_cfg(cfg)
        _, msg = start_bg()
        print(msg)
        return 0
    if flag("--status"):
        print(status(watch_dirs(cfg)))
        return 0

    interval = val("--interval", cfg.get("interval") or DEFAULT_INTERVAL, int) \
        or DEFAULT_INTERVAL
    save_cfg(cfg)
    dirs = watch_dirs(cfg, only=flag("--only"))

    if flag("--serve"):
        serve(dirs, interval)
        return 0
    if flag("--once"):
        r = run_once(dirs, interval)
        print("WATCH dirs=%d found=%d new=%d ok=%d fail=%d skip=%d"
              % (r["dirs"], r["found"], r["new"], r["ok"], r["fail"], r["skip"]))
        return 0 if r["fail"] == 0 else 1

    print(__doc__)
    print("当前监视目录：")
    for d in dirs:
        print("  · %s" % d)
    print("\n加 --start 后台常驻，或 --install 装开机自启。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
