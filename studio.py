r"""
VoiceStudio 核心：角色库 + 流水线编排。

和上一版（AutoVoice 的 auto_voice.py）最大的区别是「角色」成了一等公民：
以前一个视频对应一个音色，跑完就结束；现在一个视频里的每个声音都会被拆出来
单独建一个角色，各自有自己的素材、标注、模型、成品和介绍，并且每个都还能
继续加料训练、说话、唱歌。

目录约定：
    data/roles.json              角色索引
    data/roles/<id>/             角色目录
        slices/                  属于这个角色的切片
        asr.list                 标注（人工可改）
        final/                   合成成品
        state.json               这一步跑到哪了
    data/jobs/<id>/              任务日志
    data/uploads/                上传的视频
"""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DATA = os.path.join(HERE, "data")
ROLES_DIR = os.path.join(DATA, "roles")
JOBS_DIR = os.path.join(DATA, "jobs")
UPLOADS = os.path.join(DATA, "uploads")
ROLES_JSON = os.path.join(DATA, "roles.json")
STAGES = os.path.join(HERE, "stages")

GSV_ROOT = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0"
RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
PY = os.path.join(GSV_ROOT, "runtime", "python.exe")
RVC_PY = os.path.join(RVC_ROOT, "runtime", "python.exe")
FFMPEG = os.path.join(GSV_ROOT, "runtime", "ffmpeg.exe")
FFPROBE = os.path.join(GSV_ROOT, "runtime", "ffprobe.exe")

# ---- 改词（YingMusic-Singer-Plus）------------------------------------
#
# 为什么必须独立成第三个环境：它是 py3.10 + torch 2.7.1(cu128)，
# 和上面两套 runtime 的 torch 版本都不一样。三个环境各跑各的子进程，
# 谁也别 import 谁 —— 混进同一个进程会崩在完全不相干的地方，
# 而报错信息只会指向那一行，根因（解释器/依赖）根本看不出来。
YING_ROOT = r"D:\AHC\YingMusic-Singer-Plus-main"
YING_PY = os.path.join(YING_ROOT, ".venv", "Scripts", "python.exe")
# 改词一次生成的块长（秒）。实测 28 秒一块在 8GB 显存上稳；
# 再长就容易出现整块跑偏，再短则块间拼接缝隙变多。
LYRIC_CHUNK_SEC = 28.0

S2G = "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth"
GPT_WEIGHT_DIR = os.path.join(GSV_ROOT, "GPT_weights_v5turbo")

import seedvc  # noqa: E402  Seed-VC（44.1k 歌声精修 + 角色微调）的路径/档位，只读
# 可视化视频（把成品渲染成能直接发出去的 MP4）。这个模块**不反向 import 本文件**，
# 只依赖标准库 + ffmpeg，所以这里顶层 import 不会打结。
import viz  # noqa: E402

# ★ 逐帧手绘那套要用 numpy + PIL。运行时的 GPT-SoVITS python 和门禁 venv 里都有，
#   但**不是每个会 import studio 的解释器都有** —— `verify_disk` /
#   `verify_train_rounds` 跑的是精简的托管 python，那里没有 numpy。
#   顶层硬 import 会让那一关直接 `ModuleNotFoundError: numpy`，
#   看起来像"磁盘检查坏了"，其实是可视化模块把整个 studio 拖死了（实测踩过）。
#   所以这里降级成"没有就少一套引擎"，并在用的时候给出能看懂的原因。
try:
    import vizfx  # noqa: E402
    _VIZFX_ERR = ""
except Exception as _e:                                          # noqa: BLE001
    vizfx = None
    _VIZFX_ERR = "%s: %s" % (type(_e).__name__, _e)

# 「每一句的语气」：tone.py 只用标准库（math），所以可以硬 import ——
# 它不需要 numpy/librosa，那些重的活都在 stages/tone_index.py 里跑子进程。
import tone  # noqa: E402

# 切片参数，顺序对应 `tools/slice_audio.py`：
#   阈值dB / 每段最短 / 最短间隔 / 跳步 / 静音最多留 / 归一化峰值 / 混合系数 / 分片号 / 总分片
#
# ★ 中间那四个的单位是**毫秒**，不是帧（`tools/slicer2.py` 的 Slicer 内部自己按
#   sr 换算；它 argparse 的 help 原文写的就是 milliseconds）。
#   ⇒ 也就是：每段至少 **4 秒**、静音满 **0.3 秒**才切、静音最多留 **0.5 秒**。
#   望文生义当成"帧"再去乘一次采样率比例，会把 4 秒悄悄改成 6 秒（实测：3 段被并成
#   2 段）—— 这个坑由 `_probe_slice_hifi.py` 的"切分行为必须不变"判据挡着。
SLICE_ARGS = ["-34", "4000", "300", "10", "500", "0.9", "0.25", "0", "1"]

# ★★ 切片**输出**的采样率（2026-10-16 起）。为什么是 48000：
#   GPT-SoVITS 自带的 `tools/slice_audio.py` 三处写死 32000（`Slicer(sr=)` /
#   `load_audio()` / `wavfile.write()`），那是给**说话模型**用的，没错。
#   但我们同一份切片还拿去训 **RVC 歌声模型** ⇒ 素材被锁在 32k，奈奎斯特上限
#   **16 kHz**。实测：分离出的登阶人声是 44.1k stereo，切完 353 段全是 32k，
#   上面那 16k~22.05k 一段（齿音、气声、"像不像本人在唱"的那部分）当场没了。
#   文献对训练素材的要求是 **44.1k 或 48k 且全库统一**；RVC 的档位里 v1 只有
#   32k/40k/48k、v2 只有 32k/48k ⇒ **48k 是唯一两代都支持的最高档**，
#   且 48k 的奈奎斯特 24k > 44.1k 的 22.05k，从 44.1k 升上去**不丢任何信息**。
#   ⇒ 我们用自己的 `stages/slice_hifi.py` 切，不改第三方工具（见那个文件的头）。
#
# ⚠ 长度参数**一个字都不用改**：它们是毫秒，Slicer 内部按 sr 换算（见上）。
#   曾经在这里多乘了一次 sr/32000，把"每段至少 4 秒"变成 6 秒 ⇒ 段被合并。
#   现在有探针盯着：段数、每段起止秒数必须和 32k 那份**逐段对齐**。
#
# 生效范围：**只有新导入的素材**。老角色的切片已经是 32k，不会重切（重切也救不回
# 已经丢掉的高频）—— next import 才吃得到，这点在体检里会照实说。
SLICE_SR = 48000
PALETTE = ["#6c8cff", "#ff7a59", "#2fb8a8", "#c86dd7", "#e0b53c", "#4fc75a", "#ff6f91", "#5ec8f2"]

# ---- 人声分离（唱歌链路的第一步，也是"唱出来没法听"的根治点）------------
#
# 为什么唱歌前必须分离：RVC 是"人声变人声"的模型，它靠 f0（基频）曲线工作。
# 把整首歌（含鼓、贝斯、和声）直接喂进去，它会去给鼓点也编一条 f0，
# 输出就成了跟着节奏抖的一锅粥；更要命的是混音时如果再拿**原曲**当伴奏，
# 原唱人声会跟着角色一起唱 —— 实测成品波形与原曲相关系数 0.870、
# 原曲振幅占比 0.538，也就是成品里一大半是原唱。那当然"没法听"。
#
# 分离器优先用 Roformer（SDR 12.98，2024 年的 SOTA），失败自动回退 VR 的
# HP5 —— Roformer 需要配套 yaml，缺失时它连权重都读不了，不能让它把整条链带死。
SEP_CACHE = os.path.join(DATA, "cache", "sep")
UVR_MODEL_BEST = "model_bs_roformer_ep_317_sdr_12.9755"
UVR_MODEL_SAFE = "HP5_only_main_vocal"

# ---- 分离选优（2026-10-12 加）------------------------------------------
#
# 以前是「固定用 Roformer，跑不通就退 HP5」——**跑通就算好**，质量从来没被量过。
# 于是"漏了一片伴奏进来"和"把人声削掉一半"在日志里长得一模一样。现在改成：
#   ① 先切 30 秒代表性片段，让**每个候选模型**各分一遍；
#   ② 用 stages/sep_eval.py 量化打分（leak / 动态范围 / 饱满度 / 高频）；
#   ③ 分最高的那个去跑全曲。
# 代价只有"多跑 2 段 30 秒"（实测 8~15 秒一个），换来的是每一首歌都用对了模型。
#
# ★ 评分权重是 `_probe_sep_calib.py` 用**合成真值**校准出来的，不是拍脑袋：
#   把已知的人声+伴奏重新混成一首歌（真值已知），各模型分一遍，再拿指标跟真 SDR
#   比排序。实测 SDR 9.21 / 5.49 / 3.40 对应 score 96.4 / 81.4 / 72.0 —— 同序。
#   floor（越安静越好）当时是**反的**（削到没声音当然安静），已经改成区间型。
SEP_POLICY = "v2"                 # 改权重/候选就 +1，旧缓存自然失效重算
UVR_CANDIDATES = (UVR_MODEL_BEST, UVR_MODEL_SAFE, "HP2_all_vocals")
SEP_PROBE_SEC = 30.0              # 先拿这么长一段去试各模型
SEP_MAX_CAND = 3                  # 最多试几个（再多就是白烧显卡）
SEP_RETRY_LEAK = 0.15             # 泄漏超过这个值 ⇒ 人声轨再分离一遍（去残留）
SEP_PICK_STATS = os.path.join(DATA, "sep_pick_stats.json")

for d in (DATA, ROLES_DIR, JOBS_DIR, UPLOADS, SEP_CACHE):
    os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------- 小工具


def log(job, msg=""):
    line = str(msg)
    print("[%s] %s" % (job.get("id", "-"), line), flush=True)
    with open(job["_logfile"], "a", encoding="utf-8") as f:
        f.write(line + "\n")


# 视频后缀：丢进「歌」槽时先抽音轨再往下走。
# 参考了 ffmpeg.wasm / Mediabunny 那类"视频→音频"的思路，只是本机已经有 ffmpeg，
# 没必要在浏览器里再跑一遍 WASM（那对几百 MB 的视频也不现实）。
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v",
             ".ts", ".mpeg", ".mpg", ".wmv", ".vob", ".rmvb")


def is_video(path):
    return str(path or "").lower().endswith(VIDEO_EXT)


def extract_audio(src, dst=None, timeout=1800):
    """从视频里把音轨抽出来（ffmpeg），返回 (路径, 说明)。

    失败返回 (None, 原因) —— 不抛异常：抽不出来就让用户自己去转，
    别把上传这一步整个弄死。
    """
    src = str(src or "")
    if not os.path.isfile(src):
        return None, "文件不在：%s" % src
    base = os.path.splitext(os.path.basename(src))[0]
    dst = dst or os.path.join(UPLOADS, base + "_音轨.wav")
    try:
        # -vn 丢掉视频流；统一成 44.1k 立体声，后面分离/变声/混音都吃这个规格
        p = subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", src, "-vn",
             "-ac", "2", "-ar", "44100", "-acodec", "pcm_s16le", dst],
            capture_output=True, timeout=timeout)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])
    if p.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) < 1024:
        err = (p.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        return None, "抽不出音轨（这个视频可能没有音频流，或编码 ffmpeg 不认）：%s" % (
            err[-1][:160] if err else "退出码 %s" % p.returncode)
    secs = wav_seconds(dst)
    return dst, "已从视频抽出音轨（%.1f 秒）" % (secs or 0)


def wav_seconds(path):
    try:
        w = wave.open(path, "r")
        s = w.getnframes() / float(w.getframerate())
        w.close()
        return s
    except Exception:
        return 0.0


def wav_rms(path):
    import array

    try:
        w = wave.open(path, "r")
        raw = w.readframes(w.getnframes())
        w.close()
    except Exception:
        return 0.0
    try:
        a = array.array("h")
        a.frombytes(raw)
    except Exception:
        return 0.0
    if not a:
        return 0.0
    acc = 0.0
    step = max(1, len(a) // 20000)
    cnt = 0
    for i in range(0, len(a), step):
        v = a[i] / 32768.0
        acc += v * v
        cnt += 1
    return (acc / max(cnt, 1)) ** 0.5


def base_env():
    env = os.environ.copy()
    env["PATH"] = os.path.join(GSV_ROOT, "runtime") + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # ★ 子进程的 stdout 接的是管道 ⇒ Python 默认**块缓冲（4~8KB）**，
    #   它的 print 会一直堵在子进程缓冲区里，直到进程结束或攒满 8KB。
    #   实测：让角色唱歌（RVC 推理）跑了 **15 分钟**，任务日志里除了一行
    #   "$ …infer.cli…" 之外**一个字都没有**，进度条纹丝不动 —— 用户的原话是
    #   "看不见进度以及做到哪一步"。而那个脚本明明开头就 print 了
    #   "当前设备：… | 推理精度：…"。根因就是这四个字：块缓冲。
    #   加这一个环境变量，所有走 run() 的子进程（拆解/训练/推理/混音）都立刻逐行可见。
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _tail(out, n=6):
    """取输出末尾几行 —— 报错时直接把真正的原因摆出来，
    而不是只说「失败了」。用户要能看见每一步卡在哪、为什么。"""
    if not out:
        return ""
    lines = [ln.rstrip() for ln in str(out).splitlines() if ln.strip()]
    return " / ".join(lines[-n:])[-400:]


_PROG_RE = re.compile(r"(\d{1,3})\s*%")


def _pct_from_line(job, line):
    """从子进程的一行里抠一个进度百分比；抠不到就什么都不做。

    为什么需要：进度条要动才让人安心。RVC 的推理链里**没有 tqdm**，
    多数行抠不到百分比 —— 那也不要紧（日志在动、步骤名在，用户就知道没卡死）；
    抠得到就是白赚。训练那类会打百分比的脚本因此也能直接受益。
    """
    m = _PROG_RE.search(line or "")
    if not m:
        return
    p = int(m.group(1))
    if not (0 <= p <= 100):
        return
    # 只前进不回退：有的子进程分几段各自从 0 开始，进度条来回跳
    # 比不动更让人以为坏了。
    if p > (job.get("pct") or 0):
        job["pct"] = p


# ---------------------------------------------------------------- 暂停 / 继续
#
# ★ 为什么是"挂起进程"而不是"在 Python 侧停手"：任务的全部重活都在子进程里
#   （ffmpeg / UVR 分离 / RVC 推理 / 训练）。Python 侧就算停下来不读管道，
#   子进程照样把显卡跑满、照样往磁盘写 —— 那不叫暂停，只是"界面不动了"。
#   Windows 上挂起整棵进程树用 ntdll 的 NtSuspendProcess（Process Explorer
#   同款做法），零依赖、能连孙进程一起停住（shell=True 会先起 cmd.exe，
#   只挂 cmd 而不管它下面的 python，等于没挂）。
#
#   CTRL 不能存进 job（job 要写 JSON），线程同步对象和句柄都不可序列化，
#   所以另开一张按 jid 索引的表。

CTRL = {}
CTRL_LOCK = threading.Lock()


def _ctrl(job):
    with CTRL_LOCK:
        c = CTRL.get(job["id"])
        if c is None:
            c = {"pause": threading.Event(), "handles": [], "paused": False,
                 "pids": [], "seen_pids": [], "cancelled": False}
            c["pause"].set()          # set = 允许继续跑
            CTRL[job["id"]] = c
        return c


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    PROCESS_SUSPEND_RESUME = 0x0800
    PROCESS_TERMINATE = 0x0001
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    # ★ argtypes 必须显式声明：不声明时 ctypes 默认按 C int 传参，
    #   而快照句柄在 64 位下是 8 字节 —— 会被截断成垃圾值，Process32First
    #   直接失败，进程树就永远只有一个根节点。实测症状：按了暂停，
    #   计数还在涨（因为 python 子进程根本没被挂起）。
    _kernel32.Process32First.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(_PROCESSENTRY32)]
    _kernel32.Process32First.restype = wintypes.BOOL
    _kernel32.Process32Next.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(_PROCESSENTRY32)]
    _kernel32.Process32Next.restype = wintypes.BOOL


def _tree_pids(root):
    """root 自己 + 所有后代进程的 pid。非 Windows 或快照失败就只返回 root。"""
    if os.name != "nt":
        return [root]
    TH32CS_SNAPPROCESS = 0x00000002
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) in (-1, 0xFFFFFFFFFFFFFFFF):
        return [root]
    try:
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        parent = {}
        ok = _kernel32.Process32First(snap, ctypes.byref(pe))
        while ok:
            parent[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
            ok = _kernel32.Process32Next(snap, ctypes.byref(pe))
    finally:
        _kernel32.CloseHandle(snap)
    out = {root}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent.items():
            if ppid in out and pid not in out:
                out.add(pid)
                changed = True
    return sorted(out)


def _open_all(pids):
    hs = []
    if os.name != "nt":
        return hs
    for pid in pids:
        h = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
        if h:
            hs.append(h)
    return hs


def _suspend_pids(pids):
    """挂起这些 pid **以及它们的全部后代**，返回句柄列表（恢复时要用同一批）。

    ★ 必须连后代一起挂：run() 用 shell=True 起命令，Windows 会先拉一个
    cmd.exe，真正干活的是它下面的 python.exe —— 只挂 cmd 等于没挂。
    实测（探针 _probe_pause.py）：只挂根进程时，按了暂停计数照涨。
    """
    allp = set()
    for pid in pids or []:
        allp.update(_tree_pids(pid))
    hs = _open_all(sorted(allp))
    if os.name == "nt":
        for h in hs:
            with contextlib.suppress(Exception):
                _ntdll.NtSuspendProcess(h)
    return hs


def _resume_handles(hs):
    if os.name != "nt":
        return
    for h in hs or []:
        with contextlib.suppress(Exception):
            _ntdll.NtResumeProcess(h)
        with contextlib.suppress(Exception):
            _kernel32.CloseHandle(h)


def _kill_pids(pids):
    """终止这些 pid **以及它们的全部后代**，返回真正发出终止的个数。

    ★ 顺序有讲究：先杀子孙、最后杀根。反过来先杀 cmd.exe，它底下那个
      python 会变成孤儿继续占着显卡 —— 而"点了取消显卡还在跑"正是
      取消功能最不能出的洋相。

    ★ 绝不杀自己：pids 里混进当前进程 pid 的可能性不是零（进程树快照
      是全局的，同一台机器上跑着多个 python），杀掉自己等于服务直接没。
    """
    roots = set(int(p) for p in (pids or []) if p)
    allp = set()
    for pid in roots:
        allp.update(_tree_pids(pid))
    allp.discard(os.getpid())
    if os.name != "nt":
        n = 0
        for pid in sorted(allp):
            with contextlib.suppress(Exception):
                os.kill(pid, 9)
                n += 1
        return n
    n = 0
    # 根最后杀：sorted 里把 root 排到末尾
    order = sorted(allp - roots) + sorted(allp & roots)
    for pid in order:
        h = _kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            continue
        try:
            if _kernel32.TerminateProcess(h, 1):
                n += 1
        finally:
            _kernel32.CloseHandle(h)
    return n


def _paused_extra(job):
    """这个任务累计被暂停了多久（秒）。

    已用时长 / 预计剩余都必须扣掉它 —— 否则用户暂停十分钟再回来，
    会看到"已用 28 分钟"（实际只跑了 18 分钟），ETA 也跟着错。
    """
    t = job.get("_paused_total") or 0
    if job.get("_paused_at"):
        t += time.time() - job["_paused_at"]
    return t


def pause_job(jid):
    """挂起任务：连子进程带孙进程一起停住。返回 (ok, 说明)。"""
    job = get_job(jid)
    if not job:
        return False, "没有这个任务"
    # 只拒绝"已经结束"的任务。**不要**写成 `!= "running"` —— 那会把
    # queued（刚建好还没进 wrapper）这种边界状态一并拒掉，
    # 症状是"按了暂停没反应"，而真正的原因藏在返回值里没人看。
    if job.get("state") in ("done", "failed", "interrupted"):
        return False, "这个任务已经结束了"
    c = _ctrl(job)
    if c["paused"]:
        return True, "已经在暂停中"
    c["pause"].clear()
    c["paused"] = True
    c["handles"].extend(_suspend_pids(c.get("pids") or []))
    job["paused"] = True
    job["_paused_at"] = time.time()
    log(job, "|| 已暂停 —— 子进程已经挂起，显卡/CPU 会空出来，随时可以继续。")
    save_job(job)
    return True, "已暂停"


def resume_job(jid):
    job = get_job(jid)
    if not job:
        return False, "没有这个任务"
    c = _ctrl(job)
    if not c["paused"]:
        return True, "本来就在运行"
    _resume_handles(c["handles"])
    c["handles"] = []
    c["paused"] = False
    c["pause"].set()
    job["paused"] = False
    if job.get("_paused_at"):
        job["_paused_total"] = (job.get("_paused_total") or 0) + (time.time() - job["_paused_at"])
        job["_paused_at"] = None
    log(job, ">> 已继续（累计暂停 %.0f 秒）" % (job.get("_paused_total") or 0))
    save_job(job)
    return True, "已继续"


def cancel_job(jid, cleanup=False):
    """取消任务：杀掉整棵进程树，并把任务落成 cancelled。返回 (ok, 说明)。

    和暂停的区别要讲清楚，因为两者按钮挨在一起：
      暂停 = 挂起，随时能接着跑（进度和已跑出来的东西都还在）；
      取消 = 不要了，进程直接终止，已经跑出来的中间产物也不再有用。

    ★ 必须先"恢复"再杀：暂停时我们手里握着进程的挂起句柄，
      直接杀掉进程而句柄不释放，会一路泄漏到服务重启。
      另外挂起中的进程虽然能被 TerminateProcess 杀掉，
      但先恢复能让它把自己打开的文件句柄正常关掉，少留几个锁死的临时文件。

    cleanup=True 时，连这个任务已经产生的中间产物一起删（work 目录）。
    真正的删除**不在这一刻做** —— 线程可能还握着文件句柄、还在往里写，
    现在删要么删不掉要么把线程搞崩。改成记一个标记，等 wrapper 收尾时再删。
    """
    job = get_job(jid)
    if not job:
        return False, "没有这个任务"
    if job.get("state") in ("done", "failed", "cancelled", "interrupted"):
        return False, "这个任务已经结束了"
    c = _ctrl(job)
    c["cancelled"] = True
    c["pause"].set()          # 别让还在等"允许继续"的地方一直卡着
    if c["paused"]:
        _resume_handles(c["handles"])
        c["handles"] = []
        c["paused"] = False
        job["paused"] = False
        if job.get("_paused_at"):
            job["_paused_total"] = (job.get("_paused_total") or 0) + (
                time.time() - job["_paused_at"])
            job["_paused_at"] = None
    pids = list(c.get("pids") or []) + list(c.get("seen_pids") or [])
    killed = _kill_pids(pids)
    job["state"] = "cancelled"
    job["cancel_requested"] = True
    job["_cleanup_after"] = bool(cleanup)
    job["ended"] = _now()
    log(job, "✕ 已取消 —— 终止了 %d 个进程（含子进程）。已经跑出来的部分不再保留。"
        % killed)
    save_job(job)
    return True, "已取消（终止 %d 个进程）" % killed


def job_cancelled(job):
    """这个任务被取消了吗。和 job_paused 一样：只读 CTRL，绝不碰 JOBS_LOCK。"""
    if not isinstance(job, dict):
        job = get_job(job)
    if not job:
        return False
    with CTRL_LOCK:
        c = CTRL.get(job.get("id"))
    return bool(c and c.get("cancelled"))


def job_paused(job):
    """这个任务当前是否被暂停。

    ★ 只读 CTRL，**绝不**去 get_job —— 因为 job_public 会在持有 JOBS_LOCK 时
    被调用（list_jobs 就是 `with JOBS_LOCK: [job_public(j) ...]`），
    而 get_job 自己也要 JOBS_LOCK。threading.Lock 不可重入，于是直接死锁：
    整个服务卡死，连 POST /api/jobs 都不再响应（实测就是这么炸的，
    而服务日志一片安静，看起来像"请求没进来"）。
    参数可以是 job 字典，也可以是 jid。
    """
    if not isinstance(job, dict):
        job = get_job(job)
    if not job:
        return False
    # 只读、不创建：job_public 会遍历全部历史任务，用 _ctrl() 会给 58 个
    # 早就结束的任务都建一条 CTRL，纯属浪费。
    with CTRL_LOCK:
        c = CTRL.get(job.get("id"))
    return bool(c and c["paused"])



def run(job, cmd, cwd=GSV_ROOT, env=None, timeout=None, on_line=None,
        heartbeat=None):
    """跑一条命令，逐行把输出写进任务日志。

    on_line: 每行输出后的回调。训练/推理进度就靠它 —— 以前这里只管把行丢进日志，
    结果那几十分钟里 pct 一直是 0，界面上的进度条**纹丝不动**，
    用户完全没法确认"还在跑吗、跑到哪了"。

    env: 覆盖环境变量。**值为 None 表示删掉这个变量** —— 这样验收脚本才能
    干净地关掉 PYTHONUNBUFFERED 去做负对照（不用猜"空串算不算开"）。

    heartbeat=(预计秒数, 说明): 给"自己不打进度"的命令**估**一个进度。
    为什么需要：RVC 的推理链里连 tqdm 都没有，整首歌跑 18 分钟一句话不说，
    进度条永远是 0 —— 用户的原话是"看不见进度以及做到哪一步"。
    这里按"这首歌实测大约要多久"线性外推，**封顶 90%**（剩下的 10% 留给真正结束），
    并在 detail 里明说这是估算。估错也没关系：它只会让进度条动起来，
    真正作数的仍然是结束时的 100%。
    """
    e = base_env()
    if env:
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
    log(job, "  $ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    # ★ 取消检查（起进程之前）：上一步的进程刚被杀掉、这一步马上又要起一个 ——
    #   不在这里拦，用户点了取消任务照样往下跑（实测：取消了还在做分离）。
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    t0 = time.time()
    p = subprocess.Popen(
        cmd, cwd=cwd, env=e, shell=isinstance(cmd, str),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    hb_stop = threading.Event()
    c = _ctrl(job)
    # 把当前子进程登记进 CTRL：暂停/取消按钮点下来时，要动的正是它（以及它的子孙）。
    c["pids"] = [p.pid]
    # seen_pids 是**累积**的：任务被取消的那一刻可能正好卡在两步之间的空档
    # （pids 已被清成空），只杀 c["pids"] 就会漏掉刚跑完那一步留下的孤儿。
    c["seen_pids"] = sorted(set(c.get("seen_pids") or []) | {p.pid})
    if job_cancelled(job):
        # 用户在 Popen 之后、读输出之前按了取消 ⇒ 立刻终止，别等它跑完
        _kill_pids([p.pid])
        raise Cancelled("任务已被取消")
    if c["paused"]:
        # 用户在两步之间的空档按了暂停 ⇒ 这个刚起的子进程要**立刻**挂起，
        # 否则会出现"明明暂停了显卡还在跑"的鬼故事。
        c["handles"].extend(_suspend_pids([p.pid]))
    if heartbeat:
        exp, label = heartbeat

        def _hb():
            while not hb_stop.wait(15):
                if job.get("state") in ("done", "failed", "interrupted"):
                    return
                if c["paused"]:
                    # 暂停期间不推进进度、也不改 eta —— 否则"还剩几分钟"会一直
                    # 往下掉，恢复时变成一个不对的数。
                    continue
                el = time.time() - t0 - _paused_extra(job)
                pct = int(min(90, el / max(1.0, exp) * 100))
                if pct > (job.get("pct") or 0):
                    job["pct"] = pct
                job["eta"] = int(max(0, exp - el))
                job["detail"] = label

        threading.Thread(target=_hb, daemon=True).start()
    out = []
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            out.append(line)
            log(job, "    " + line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    # 进度解析不该有能力弄崩训练本身。解析失败就当这行没进度。
                    pass
        p.wait(timeout=timeout)
    finally:
        hb_stop.set()
        c["pids"] = []
    log(job, "  -> 退出码 %d，用时 %.1fs" % (p.returncode, time.time() - t0))
    # ★ 取消检查（进程结束之后）：子进程是被我们杀掉的，退出码必然非 0。
    #   不在这里拦的话，各步骤会把"进程被杀"当成"命令失败"，
    #   于是用户取消一个任务，看到的是"失败：RVC 推理失败（退出码 1）"
    #   —— 他会对着自己亲手取消的任务去找 bug。
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    return p.returncode, "\n".join(out)


class Fail(Exception):
    pass


class Cancelled(Exception):
    """任务被用户取消。

    为什么不能只靠"杀进程"：流水线是一串步骤，每步自己起一个子进程。
    杀掉当前这一步的进程，下一步会**立刻再起一个**——用户点了取消，
    结果任务照跑（实测：取消后 UVR 分离接着开跑，显卡还是满的）。
    所以取消必须同时在两条线上生效：
      ① 杀掉当前进程树（连子孙，shell=True 下真正干活的是孙进程）；
      ② 在每个步骤边界抛出本异常，让 wrapper 把任务落成 cancelled。
    cancelled 和 failed 要分开：failed 意味着"去看日志排错"，
    cancelled 是"我不要了"，混在一起会让用户对着自己取消的任务去找 bug。
    """


# ---------------------------------------------------------------- 角色库


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _roles_backup_path():
    return ROLES_JSON + ".bak"


def load_roles():
    """读角色库。

    **解析失败不再静默返回空列表** —— 这是真事故的根源：
    `except: return {"roles": []}` 会让下一次任何写入（建角色/改角色）
    把**空列表**原样存回磁盘，于是磁盘上的角色目录还在、元数据却全没了
    （实测角色 A/B 的切片、成品、训练数据都完好，只有索引被抹平）。
    ⇒ 解析失败时先自动备份坏文件，再从**磁盘上的角色目录**重建索引，
      并且**拒绝**返回一个"看起来正常"的空库。
    """
    if not os.path.exists(ROLES_JSON):
        return {"roles": []}
    try:
        db = json.load(open(ROLES_JSON, encoding="utf-8"))
        if isinstance(db, dict) and isinstance(db.get("roles"), list):
            return db
        raise ValueError("结构不对：不是 {roles:[...]}")
    except Exception as e:
        # 保留现场，别删——用户可能还要手工抢救
        try:
            bad = ROLES_JSON + ".corrupt-%d" % int(time.time())
            shutil.copy2(ROLES_JSON, bad)
            sys.stderr.write("[角色库] 解析失败(%s)，已备份到 %s，改为从磁盘重建\n"
                             % (e, bad))
        except Exception:
            pass
        rebuilt = rebuild_roles_from_disk()
        if rebuilt:
            return rebuilt
        # 重建不出来也不能悄悄给空库：宁可让调用方报错，也别覆盖真数据
        raise RuntimeError("角色库损坏且无法从磁盘重建：%s" % ROLES_JSON)


def rebuild_roles_from_disk():
    """扫描 data/roles/*/ 把每个角色目录重新登记成一条记录。

    切片、成品、训练数据都在磁盘上，只有索引丢了 —— 所以索引是**可重建**的。
    重建出的记录会从磁盘把段数/时长/成品数如实数出来，不编数据。
    """
    root = os.path.join(DATA, "roles")
    if not os.path.isdir(root):
        return None
    out = []
    for rid in sorted(os.listdir(root)):
        d = os.path.join(root, rid)
        if not os.path.isdir(d) or not rid.startswith("r"):
            continue
        slices = []
        sd = os.path.join(d, "slices")
        if os.path.isdir(sd):
            for fn in sorted(os.listdir(sd)):
                if not fn.lower().endswith((".wav", ".mp3", ".flac")):
                    continue
                fp = os.path.join(sd, fn)
                dur = 0.0
                try:
                    dur = _probe_dur_safe(fp)
                except Exception:
                    dur = 0.0
                slices.append({"file": fn, "dur": round(dur, 3),
                               "text": _asr_lookup(d, fn), "voiced": 1.0})
        finals = []
        fd = os.path.join(d, "final")
        if os.path.isdir(fd):
            for fn in sorted(os.listdir(fd)):
                if fn.lower().endswith((".wav", ".mp3", ".flac")):
                    finals.append({"wav": fn, "at": _now(),
                                   "text": os.path.splitext(fn)[0]})
        out.append({
            "id": rid,
            "name": "角色 %s" % rid[-4:],
            "intro": "",
            "kind": "speech",
            "kind_locked": True,      # 重建的不要被自动预判乱改
            "color": PALETTE[len(out) % len(PALETTE)],
            "source": "从磁盘重建",
            "created": _now(),
            "segments": slices,
            "ref": None,
            "model": None,
            "rvc": None,
            "outputs": finals,
            "stats": {"segments": len(slices),
                      "seconds": round(sum(s["dur"] for s in slices), 1)},
            "rebuilt": True,
        })
    if not out:
        return None
    db = {"roles": out}
    save_roles(db)
    return db


def _probe_dur_safe(path):
    """尽量量出时长：先试 wave（零依赖、秒回），不行再退 ffprobe。"""
    import contextlib
    try:
        with contextlib.closing(wave.open(path, "rb")) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        pass
    import subprocess
    try:
        r = subprocess.run(["ffprobe", "-v", "quiet", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, timeout=20)
        return float((r.stdout or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def _asr_lookup(role_dir_path, fname):
    """从 asr.list 里取这段切片识别出来的文字。"""
    lst = os.path.join(role_dir_path, "asr.list")
    if not os.path.isfile(lst):
        return ""
    stem = os.path.splitext(fname)[0]
    try:
        for line in open(lst, encoding="utf-8"):
            parts = line.rstrip("\n").split("|", 1)
            if len(parts) == 2 and parts[0].strip() == stem:
                return parts[1].strip()
    except Exception:
        pass
    return ""


def save_roles(db):
    # 每次写之前先留一份上一版。没有这道保险，上面那个"空库覆盖真数据"
    # 就是**不可逆**的 —— 磁盘目录还在也救不回元数据（切片能重建，文字/模型信息不能）。
    try:
        if os.path.exists(ROLES_JSON) and os.path.getsize(ROLES_JSON) > 0:
            shutil.copy2(ROLES_JSON, _roles_backup_path())
    except Exception:
        pass
    tmp = ROLES_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ROLES_JSON)


def get_role(rid):
    for r in load_roles()["roles"]:
        if r["id"] == rid:
            return r
    return None


# 「这个角色现在能干什么」。
#
# 为什么要有它：装了 17 个市场角色之后，卡片上一律写着「未训练」，而「未训练」这
# 三个字说的是「没有**说话**模型」—— 这些角色的歌声模型是现成能用的，用户看到
# 「未训练」只会以为整张卡都是废的。反过来，卡片上那个「💬 TA 说话」按钮对它们
# 又是**点了必然失败**的（speak_role 第一句就校验 model），用户要输入文字、点合成、
# 看着任务跑起来、等几秒才收到一句红字。入口能不能走通，必须在点之前就看得见。
#
# 两条铁律：
#   ① **不持久化**。能力是「当前状态的读数」，不是角色的属性 —— 素材一加、模型一训
#      就该变。写进 roles.json 就会出现过期的「可说话」（铁律 12d）。
#   ② 判据必须跟真正执行任务的那段代码**同一个**（speak_role 看 role["model"]、
#      sing_job 看 role["rvc"]["model"] 且素材 ≥10 段）。这里放宽一点，用户就会
#      看到一个绿色的「能用」然后点下去失败 —— 那比不显示更糟。
_ABILITY_MIN_SEG = 10          # 与 sing_job 的 10 段门槛保持一致


def role_ability(r):
    """实时算这个角色「现在能干什么 / 还差什么」。返回可直接给前端用的字典。"""
    segs = [s for s in (r.get("segments") or [])
            if float(s.get("dur", 0) or 0) >= 2.0]
    n = len(segs)
    has_speak = bool(r.get("model"))
    has_sing = bool((r.get("rvc") or {}).get("model"))
    enough = n >= _ABILITY_MIN_SEG

    if has_speak:
        speak = {"state": "ready", "label": "可说话", "why": "说话模型已经训好了"}
    elif enough:
        speak = {"state": "trainable", "label": "训练后可说话",
                 "why": "还没有说话模型；现在有 %d 段素材，够训一个（要几分钟）" % n}
    else:
        speak = {"state": "blocked", "label": "还不能说话",
                 "why": "还没有说话模型，素材也只有 %d 段（至少 %d 段）"
                        % (n, _ABILITY_MIN_SEG)}

    if has_sing:
        sing = {"state": "ready", "label": "可唱歌", "why": "已经有歌声模型，选首歌就能唱"}
    elif enough:
        sing = {"state": "trainable", "label": "可唱歌（先训一次）",
                "why": "还没有歌声模型；现在有 %d 段素材，第一次唱会先训一个" % n}
    else:
        sing = {"state": "blocked", "label": "还不能唱歌",
                "why": "没有歌声模型，素材也只有 %d 段（至少 %d 段）"
                       % (n, _ABILITY_MIN_SEG)}

    # 给一句「下一步该干嘛」——按「能拿到的结果最直接」排：
    # 已经能出东西就别再让用户去导素材。
    if speak["state"] == "ready" or sing["state"] == "ready":
        nxt = {"code": "ready", "label": "可以直接用",
               "hint": "这个角色现在就能出成品。"}
    elif enough:
        nxt = {"code": "train", "label": "去训练",
               "hint": "素材够了，跑一次训练就能说话 / 唱歌。"}
    else:
        nxt = {"code": "import", "label": "先导入素材",
               "hint": "导入一段这个人的视频或音频，切好片就能训练。"}

    return {
        "segments_ok": n, "need": _ABILITY_MIN_SEG,
        "speak": speak, "sing": sing, "next": nxt,
    }


def ability_summary(roles):
    """角色库顶部的汇总：一眼看出这一堆里哪些能立刻用。"""
    a = [role_ability(r) for r in roles]
    return {
        "total": len(roles),
        "can_speak": sum(1 for x in a if x["speak"]["state"] == "ready"),
        "can_sing": sum(1 for x in a if x["sing"]["state"] == "ready"),
        "need_data": sum(1 for x in a if x["next"]["code"] == "import"),
    }


def _next_color(db):
    return PALETTE[len(db["roles"]) % len(PALETTE)]


def role_dir(rid):
    return os.path.join(ROLES_DIR, rid)


def create_role(name=None, kind="speech", source=""):
    db = load_roles()
    rid = "r" + uuid.uuid4().hex[:8]
    d = role_dir(rid)
    for sub in ("slices", "final"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    role = {
        "id": rid,
        "name": name or ("角色 %d" % (len(db["roles"]) + 1)),
        "intro": "",
        "kind": kind,               # speech / singing
        "kind_locked": False,       # 人工改过之后就不再被自动预判覆盖
        "color": _next_color(db),
        "source": source,
        "created": _now(),
        "segments": [],             # [{file,dur,text,voiced,core}]
        "ref": None,                # {file,text}
        "model": None,              # {ckpt,epochs,at}
        "rvc": None,                # {model,at}
        "outputs": [],              # [{text,wav,at,kind}]
        "voice": None,              # 声纹中心 {center,n,dim,model,at}，用于"加素材时认人"
        "stats": {"segments": 0, "seconds": 0.0},
    }
    db["roles"].append(role)
    save_roles(db)
    return role


def mutate_role(rid, fn):
    """
    原子地「读出来 -> 改 -> 存回去」。

    之前的写法是 `role = get_role(rid); role['x']=1; save_roles(load_roles())`，
    最后那句 load_roles() 又从磁盘读了一遍，等于把刚改的东西原样覆盖掉 ——
    表现是改完不生效（角色段数存进去 63 段，读出来 0 段）。全部改走这里。
    """
    db = load_roles()
    hit = None
    for r in db["roles"]:
        if r["id"] == rid:
            fn(r)
            hit = r
            break
    if hit is None:
        return None
    save_roles(db)
    return hit


def mutate_roles(fn):
    db = load_roles()
    fn(db)
    save_roles(db)
    return db


def update_role(rid, patch):
    db = load_roles()
    for r in db["roles"]:
        if r["id"] == rid:
            for k in ("name", "intro", "kind", "color", "ref", "kind_locked"):
                if k in patch:
                    r[k] = patch[k]
                    # 用户亲手改了名字 ⇒ 这个名字不再是"自动译出来的"，
                    # 以后补译字典时**不许**再把它改回去（那是他说的算的）。
                    if k == "name":
                        r.pop("name_auto", None)
            if patch.get("kind") is not None:
                r["kind_locked"] = True
            r["updated"] = _now()
            save_roles(db)
            return r
    return None


def rvc_artifacts(rid):
    """这个角色在 **RVC 目录**里留下的全部家当：实验目录 / 权重 / 索引。

    注意 RVC 那边的命名：实验目录就是角色 id（`logs/<rid>/`），权重是
    `assets/weights/<rid>*.pth`，索引名里也含 id。三处都对得上，才好精确清。
    """
    out = {"exp": [], "weights": [], "indices": []}
    exp = os.path.join(RVC_ROOT, "logs", rid)
    if os.path.isdir(exp):
        out["exp"].append(exp)
    wdir = os.path.join(RVC_ROOT, "assets", "weights")
    if os.path.isdir(wdir):
        out["weights"] = [os.path.join(wdir, f) for f in os.listdir(wdir)
                          if f.startswith(rid) and f.endswith(".pth")]
    idir = os.path.join(RVC_ROOT, "assets", "indices")
    if os.path.isdir(idir):
        out["indices"] = [os.path.join(idir, f) for f in os.listdir(idir)
                          if rid in f]
    return out


def purge_rvc_artifacts(rid):
    """删角色时，把它在 RVC 那边留下的东西一起清掉。返回 (删掉的, 没删掉的)。

    ★ 不做这件事就是**真金白银的磁盘泄漏**：一个角色的 RVC 实验目录里躺着
      G_*.pth + D_*.pth 约 1 GB、权重 52 MB、索引几 MB；而以前 `delete_role()`
      只删**本项目**的角色目录 ⇒ 用户删 10 个角色，就有 10 GB 静静躺在 RVC 目录里，
      界面上看不见、也没有任何入口再去删它。

    删除失败**不许掀翻调用方**：本机那个批量删除钩子抛的是 `SystemExit`
    （`except Exception` 捕不到，实测掀翻过整个门禁进程），所以这里逐项
    `except BaseException`，最后把没删掉的照实报出来，交给上层/gc 接着清。
    """
    arts = rvc_artifacts(rid)
    removed, left = [], []
    for p in arts["weights"] + arts["indices"]:
        try:
            os.remove(p)
            removed.append(p)
        except BaseException as e:
            left.append((p, "%s: %s" % (type(e).__name__, str(e)[:80])))
    for d in arts["exp"]:
        try:
            shutil.rmtree(d)
            removed.append(d)
        except BaseException as e:
            left.append((d, "%s: %s" % (type(e).__name__, str(e)[:80])))
    if left:
        sys.stderr.write("[warn] 角色 %s 的 RVC 产物没删干净（%d 项）：%s\n"
                         % (rid, len(left),
                            "; ".join("%s (%s)" % (p, why) for p, why in left)))
    return removed, left


def delete_role(rid):
    db = load_roles()
    db["roles"] = [r for r in db["roles"] if r["id"] != rid]
    save_roles(db)
    d = role_dir(rid)
    shutil.rmtree(d, ignore_errors=True)
    # 顺手把 RVC 那边的断点和权重一起清掉（不清就是几个 G 的磁盘泄漏，见上）
    try:
        purge_rvc_artifacts(rid)
    except BaseException as e:                       # 钩子抛 SystemExit，必须这么接
        sys.stderr.write("[warn] 清理角色 %s 的 RVC 产物时出错：%s: %s\n"
                         % (rid, type(e).__name__, str(e)[:120]))
    # ★ `ignore_errors=True` 会**静默吞掉**删除失败（文件被占用、权限不够、或者被本机的
    #   安全策略拦下）。那种情况下角色从界面上消失了、文件却永远留在盘上 ——
    #   用户既看不见、也再没有入口去删它（`orphan_role_dirs()` 就是为此存在的）。
    #   所以这里必须**复核一次**，留残就明确报出来（stderr 会进服务日志）。
    if os.path.isdir(d):
        sys.stderr.write("[warn] 角色 %s 已从角色库移除，但磁盘目录没删干净，"
                         "成了孤儿目录：%s\n" % (rid, d))


def orphan_role_dirs():
    """列出 `data/roles/` 下**不在角色库里**的目录（删角色时 rmtree 失败留下的残骸）。

    为什么需要它：`delete_role` 是先摘登记、再删目录。万一删目录那一步失败（被占用 /
    权限 / 安全策略），角色就不在库里了 ⇒ 界面上看不见、清理面板也扫不到，
    但它占的磁盘还在。这些就是"幽灵目录"。返回 [{id,path,mb}]。
    """
    known = {r["id"] for r in load_roles()["roles"]}
    out = []
    if not os.path.isdir(ROLES_DIR):
        return out
    for n in sorted(os.listdir(ROLES_DIR)):
        p = os.path.join(ROLES_DIR, n)
        if n in known or not os.path.isdir(p):
            continue
        size = 0
        for dp, _dn, fns in os.walk(p):
            for f in fns:
                try:
                    size += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        out.append({"id": n, "path": p, "mb": round(size / 1024.0 ** 2, 1)})
    return out


def refresh_stats(rid):
    db = load_roles()
    for r in db["roles"]:
        if r["id"] == rid:
            segs = r.get("segments", [])
            r["stats"] = {
                "segments": len(segs),
                "seconds": round(sum(s.get("dur", 0) for s in segs), 1),
            }
            save_roles(db)
            return r["stats"]
    return None


def write_list(rid):
    """把角色当前的标注写成 GPT-SoVITS 要的 .list（人工改文本后靠它生效）。"""
    role = get_role(rid)
    d = role_dir(rid)
    rows = []
    for s in role.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        rows.append("%s|%s|%s|%s" % (
            os.path.join(d, "slices", s["file"]), s["file"], "zh", text))
    p = os.path.join(d, "asr.list")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    return p, len(rows)


# ---------------------------------------------------------------- 任务


JOBS = {}
JOBS_LOCK = threading.Lock()


def new_job(role_id, kind, title, epochs_total=None):
    jid = "j" + uuid.uuid4().hex[:8]
    d = os.path.join(JOBS_DIR, jid)
    os.makedirs(d, exist_ok=True)
    job = {
        "id": jid, "role": role_id, "kind": kind, "title": title,
        "state": "queued", "stage": "", "pct": 0, "started": _now(),
        "ended": None, "error": None, "dir": d,
        # 训练类任务记录总轮数，前端才能画"还剩几轮"而不只是一条百分比
        "epochs_total": epochs_total, "epochs_done": None, "detail": None,
        # steps 让界面能画出「每一步」的清单：名称/状态/耗时/说明。
        # 以前只有一句 stage 文本在变，用户看到的是刷屏的日志，
        # 不知道走到第几步、哪步最慢、卡在哪 —— 这就是他要"看见每一步"的原因。
        "steps": [],
        "_step_idx": -1,
        "_logfile": os.path.join(d, "log.txt"),
    }
    with JOBS_LOCK:
        JOBS[jid] = job
    save_job(job)
    return job


def begin_step(job, name, note=""):
    """开始一步。同一批流程里重名的步骤会合并成一条（更新说明），不重复堆。"""
    # ★ 步骤边界也要拦取消：run() 只能拦住"正在起子进程"的那一段，
    #   而两步之间还有纯 Python 的活（拷文件、写清单、挑参考音频），
    #   那段要是跑起来，取消就只是"晚一点继续跑"而已。
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    with JOBS_LOCK:
        steps = job.setdefault("steps", [])
        job["_step_idx"] = job.get("_step_idx", -1) + 1
        steps.append({"n": job["_step_idx"] + 1, "name": name,
                      "state": "doing", "note": note,
                      "at": _now(), "secs": None})
    save_job(job)
    log(job, "【第 %d 步】%s%s" % (job["_step_idx"] + 1, name,
                                  ("　" + note) if note else ""))


def end_step(job, ok=True, note=""):
    with JOBS_LOCK:
        steps = job.setdefault("steps", [])
        i = job.get("_step_idx", -1)
        if 0 <= i < len(steps):
            steps[i]["state"] = "done" if ok else "fail"
            if note:
                steps[i]["note"] = note
    save_job(job)
    log(job, "　└ %s%s" % ("完成" if ok else "失败",
                             ("：" + note) if note else ""))


def fail_step(job, err):
    """失败时把原因**钉在这一步**上 —— 用户一眼能看出卡在哪、为什么。"""
    with JOBS_LOCK:
        steps = job.setdefault("steps", [])
        i = job.get("_step_idx", -1)
        if 0 <= i < len(steps):
            steps[i]["state"] = "fail"
            steps[i]["note"] = str(err)[:400]
    save_job(job)
    log(job, "　└ 失败：%s" % str(err)[:400])


def job_public(job):
    d = {k: v for k, v in job.items() if k not in ("_logfile", "dir", "_step_idx", "_dirty")}
    # result 是任务自己塞的，可能带 Path / 其它不可序列化的东西。
    # 不做这层清洗的话，GET /api/job/<id> 会直接 500，前端就再也拿不到任务状态了 ——
    # 症状是"任务卡住不动"，根因却是序列化，跟任务本身无关。
    try:
        json.dumps(d.get("result"))
    except Exception:
        d["result"] = None
    # 自动歌词的结果同理（它是一堆 dict，字段一旦带 Path 就会让整个任务状态拿不到）
    if d.get("lyric_result") is not None:
        try:
            json.dumps(d["lyric_result"])
        except Exception:
            d["lyric_result"] = None
    # 已用时长 / 预计剩余：用户判断"还要等多久、是不是卡死了"全靠这两个数。
    # 以前只有 state 和 pct，训练几十分钟 pct 一直是 0（见 step_train 的进度解析），
    # 界面上等于一片死寂 —— 用户没法确认进度。
    d["elapsed"] = _job_elapsed(job)
    d["eta"] = _job_eta(job)
    # 暂停状态：界面要靠它把按钮切成「继续」、并把状态写成"已暂停"。
    d["paused"] = job_paused(job)
    return d


def _ts(s):
    """把 'YYYY-MM-DD HH:MM' 解析成时间戳；解析不了就返回 None（不猜）。"""
    if not s:
        return None
    try:
        return time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M"))
    except Exception:
        return None


def _job_elapsed(job):
    a = _ts(job.get("started"))
    b = _ts(job.get("ended")) or time.time()
    if a is None:
        return None
    # 扣掉暂停时间：用户要的是"实际跑了多久"，暂停的十几分钟不该算进去。
    return max(0, int(b - a - _paused_extra(job)))


def _job_eta(job):
    """按已用时间 / 已完成比例外推剩余秒数。样本不足就不给，别显示一个假数字。"""
    if job.get("state") != "running":
        return None
    pct = job.get("pct") or 0
    el = _job_elapsed(job)
    # 少于 8% 的样本外推误差太大，宁可不说 —— 宁可没 ETA，也不要一个看着精确的假 ETA
    if not el or pct < 8:
        return None
    return int(el * (100.0 - pct) / pct)


# ---------------------------------------------------------------- 任务落盘
#
# JOBS 以前是纯内存 dict：服务一重启，正在跑的任务和全部历史一起消失。
# 训练动辄几十分钟，重启一次用户就彻底失明、也没法查上次到底跑到哪。
# 现在每个任务在 data/jobs/<id>/job.json 留一份，服务启动时重建索引。

def _job_file(d):
    return os.path.join(d, "job.json")


def save_job(job):
    """把任务写回磁盘。原子写 + 失败只记不抛 —— 落盘不该拖垮正在跑的训练。"""
    d = {
        "id": job["id"], "role": job.get("role"), "kind": job.get("kind"),
        "title": job.get("title"), "state": job.get("state"),
        "stage": job.get("stage"), "pct": job.get("pct") or 0,
        "detail": job.get("detail"),
        "started": job.get("started"), "ended": job.get("ended"),
        "error": job.get("error"), "steps": job.get("steps") or [],
        "epochs_total": job.get("epochs_total"),
        "epochs_done": job.get("epochs_done"),
        # 暂停状态也落盘：刷新页面/重开界面时按钮得是「继续」而不是「暂停」。
        # （注意：_paused_at 不落盘 —— 服务重启后子进程早就没了，
        #  把"正在暂停中"带回来只会让它永远停在那，所以恢复时一律当未暂停。）
        "paused": bool(job.get("paused")),
        "_paused_total": job.get("_paused_total") or 0,
    }
    try:
        p = _job_file(job["dir"])
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        log(job, "  [warn] 任务落盘失败：%s" % e)


def load_jobs():
    """启动时把磁盘上的任务读回内存。

    重启前是 running/queued 的，进程已经死了 —— 必须**标成中断**，
    不能让它继续显示"训练中"。否则用户会对着一个永远不会结束的进度条干等，
    这正是"看不见进度"最糟的一种形式：看着在跑，其实在几小时前就死了。
    """
    out = []
    try:
        names = sorted(os.listdir(JOBS_DIR))
    except Exception:
        return out
    for n in names:
        p = os.path.join(JOBS_DIR, n, "job.json")
        if not os.path.isfile(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("id"):
            continue
        if d.get("state") in ("running", "queued"):
            d["state"] = "interrupted"
            d["error"] = "服务在 %s 重启，这个任务被中断了" % _now()
            d["ended"] = d.get("ended") or _now()
            # 步骤清单里最后一条"doing"要落到失败态，否则界面上永远挂着一个转圈
            for s in reversed(d.get("steps") or []):
                if s.get("state") == "doing":
                    s["state"] = "fail"
                    s["note"] = "服务重启，任务中断在这一步"
                    break
        job = dict(d)
        job["dir"] = os.path.join(JOBS_DIR, job["id"])
        job["_logfile"] = os.path.join(job["dir"], "log.txt")
        job["_step_idx"] = len(job.get("steps") or []) - 1
        out.append(job)
    return out


def restore_jobs():
    with JOBS_LOCK:
        for j in load_jobs():
            JOBS[j["id"]] = j
    return len(JOBS)


def list_jobs():
    with JOBS_LOCK:
        return [job_public(j) for j in JOBS.values()]


def get_job(jid):
    with JOBS_LOCK:
        return JOBS.get(jid)


def job_log(jid, offset=0):
    job = get_job(jid)
    if not job:
        return "", 0
    p = job["_logfile"]
    if not os.path.exists(p):
        return "", 0
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        txt = f.read()
    return txt[offset:], len(txt)


def _cleanup_job_work(job):
    """删掉这个任务自己产生的中间产物，返回释放字节数。

    只删内容、**保留 job.json 和 log.txt**：用户取消完还想回来看一眼
    "上次取消的是哪一个、跑到哪一步"，把整个任务目录端掉等于把证据烧了，
    任务中心里那条也会变成打不开的死链接。
    """
    d = job.get("dir")
    if not d or not os.path.isdir(d):
        return 0
    keep = {"job.json", "log.txt"}
    freed = 0
    for name in sorted(os.listdir(d)):
        if name in keep or name.startswith("."):
            continue
        p = os.path.join(d, name)
        try:
            sz = _path_size(p)
            _rm_path(p)
            freed += sz
        except Exception as e:
            log(job, "  [warn] 中间产物没清干净（%s）：%s" % (name, e))
    return freed


def run_in_thread(job, fn):
    def wrapper():
        job["state"] = "running"
        save_job(job)
        try:
            # 任务函数的返回值存进 job["result"]：前端要靠它拿到"到底产出了什么"
            #（比如导歌的真实落盘路径），否则只能靠猜 —— 猜就是"靠运气覆盖"。
            job["result"] = fn()
            job["state"] = "done"
            job["pct"] = 100
        except Cancelled as e:
            # ★ 取消不是失败。写成 failed 的话，用户会对着一个自己亲手取消的
            #   任务去翻日志找原因，而日志最后一行就是他自己点的那次取消。
            job["state"] = "cancelled"
            job["error"] = str(e) or "已被用户取消"
            log(job, "")
            log(job, "✕ 任务已取消。")
        except Fail as e:
            job["state"] = "failed"
            job["error"] = str(e)
            log(job, "")
            log(job, "! " + str(e))
        except Exception as e:
            import traceback

            job["state"] = "failed"
            job["error"] = "%s: %s" % (type(e).__name__, e)
            log(job, traceback.format_exc())
        job["ended"] = _now()
        # 最后一条还在 doing 的步骤必须落成终态 —— 否则界面永远挂着一个"正在做"
        with JOBS_LOCK:
            for s in reversed(job.get("steps") or []):
                if s.get("state") == "doing":
                    if job.get("state") == "cancelled":
                        s["state"] = "cancel"
                        if not s.get("note"):
                            s["note"] = "在这一步被取消"
                    else:
                        s["state"] = "fail"
                        if not s.get("note"):
                            s["note"] = (job.get("error") or "任务结束在这一步")[:400]
                    break
        # 取消时要求"连中间产物一起删"的，到这里才真删：此刻线程已经收尾，
        # 文件句柄都放开了，不会出现"删不掉"或"把还在写的目录端掉"。
        if job.get("_cleanup_after"):
            freed = _cleanup_job_work(job)
            if freed:
                log(job, "✕ 中间产物已清掉，释放 %s。" % _humansize(freed))
        save_job(job)
        # 收尾后把 CTRL 里这条丢掉：它还握着 pause Event 和一堆 pid，
        # 留着只会让 job_paused 对一个早就结束的任务返回奇怪的值。
        with CTRL_LOCK:
            CTRL.pop(job.get("id"), None)

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return job


# ---------------------------------------------------------------- 流水线各步


def step_audio(job, video, out_wav):
    job["stage"] = "抽取音频"
    rc, out = run(job, '"%s" -i "%s" -vn -acodec pcm_s16le -ac 2 -ar 44100 "%s" -y'
                  % (FFMPEG, video, out_wav), timeout=1800)
    if rc != 0 or not os.path.exists(out_wav):
        raise Fail("抽音频失败，视频可能没有音轨或格式不认识。")
    secs = wav_seconds(out_wav)
    log(job, "  音频 %.1f 秒" % secs)
    if secs < 10:
        raise Fail("音频只有 %.1f 秒，太短了，训不出音色（至少要 1 分钟以上）。" % secs)
    return out_wav


def step_uvr(job, audio_wav, work, enabled=True, model=None):
    """导入链里的分离：跟唱歌链**同一套选优**（以前这里写死 HP5，而唱歌链用
    Roformer —— 同一份素材两条链分出来的质量不一样，训练出来的模型也跟着飘）。
    """
    job["stage"] = "分离人声"
    vocal_dir = os.path.join(work, "02_vocal")
    os.makedirs(vocal_dir, exist_ok=True)
    if not enabled:
        shutil.copy(audio_wav, os.path.join(vocal_dir, "vocal.wav"))
        return vocal_dir
    in_dir = os.path.join(work, "_uvr_in")
    inst_dir = os.path.join(work, "02_inst")
    for d in (in_dir, inst_dir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    shutil.copy(audio_wav, os.path.join(in_dir, "source.wav"))

    chosen = model or UVR_MODEL_BEST
    if not model:
        rows = _sep_probe(job, audio_wav, work, _sep_candidates())
        if rows:
            chosen = rows[0]["model"]
            log(job, "  ⇒ 用 %s（得分 %.1f）" % (chosen[:28], rows[0]["score"]))
    v, i, why = _uvr_run(job, chosen, in_dir, vocal_dir, inst_dir)
    if not v:
        for m in _sep_candidates():
            if m == chosen:
                continue
            v, i, why = _uvr_run(job, m, in_dir, vocal_dir, inst_dir)
            if v:
                chosen = m
                break
    if not v:
        raise Fail("人声分离没产出文件。\n%s" % str(why)[:600])
    log(job, "  用 %s 分离完成" % chosen[:28])
    return vocal_dir


# ------------------------------------------------ 人声分离（唱歌链路的第 0 步）


def _sep_cache_key(song_path, model=None):
    """按「原曲路径 + 大小 + 修改时间 + 选优策略版本」算缓存键。

    为什么把 mtime 也算进去：同一个路径可能被换掉内容（重新下载/剪辑），
    只看路径会拿旧缓存去唱新歌 —— 那种错很难看出来（"我换歌了怎么没变"）。

    ★ 键里**不再带模型名**（v2 起）：模型是**选出来的**，不是指定的。带进去的话
    换了一天选到另一个模型就会缓存 miss 再跑一遍全曲；现在把选中的模型写进
    `meta.json`，命中缓存时直接复用并如实说"上次选的是谁、多少分"。
    """
    import hashlib

    st = os.stat(song_path)
    raw = "%s|%d|%d|%s|%s" % (os.path.abspath(song_path), st.st_size,
                              int(st.st_mtime), model or "auto", SEP_POLICY)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _sep_stats_load():
    try:
        return json.load(open(SEP_PICK_STATS, encoding="utf-8"))
    except Exception:
        return {}


def _sep_stats_add(model, score):
    """记住每个模型的历史平均分：**好用的先试**，省下的都是显卡时间。

    这不是"针对某一首歌调参"（那是过拟合），而是跨所有歌的统计 —— 模型强弱
    本来就是普遍规律，跟具体哪首歌没关系。
    """
    d = _sep_stats_load()
    row = d.get(model) or {"n": 0, "sum": 0.0, "best": 0.0}
    row["n"] = int(row.get("n") or 0) + 1
    row["sum"] = float(row.get("sum") or 0.0) + float(score or 0)
    row["avg"] = round(row["sum"] / max(row["n"], 1), 2)
    row["best"] = max(float(row.get("best") or 0), float(score or 0))
    d[model] = row
    try:
        tmp = SEP_PICK_STATS + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, SEP_PICK_STATS)
    except Exception:
        pass


def _uvr_run(job, model, in_dir, vdir, idir, timeout=3600):
    """跑一次分离，返回 (vocal, inst, 错误)。"""
    for d in (vdir, idir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    rc, out = run(job, '"%s" -s "%s" "%s" %s "%s" "%s" "%s" 10 wav cuda True'
                  % (PY, os.path.join(STAGES, "uvr_cli.py"), GSV_ROOT, model,
                     in_dir, vdir, idir), timeout=timeout)
    vs = [f for f in os.listdir(vdir) if f.lower().endswith((".wav", ".flac"))]
    ins = [f for f in os.listdir(idir) if f.lower().endswith((".wav", ".flac"))]
    if rc == 0 and vs and ins:
        return os.path.join(vdir, vs[0]), os.path.join(idir, ins[0]), None
    return None, None, "退出码 %d，%s" % (rc, _tail(out, 3))


def _sep_score(job, mix_wav, vocal_wav, inst_wav):
    """给一次分离打分（0~100，越高越好）。打分失败返回 None（不拦流程）。"""
    rc, out = run(job, '"%s" -s "%s" score "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "sep_eval.py"),
                     mix_wav, vocal_wav, inst_wav), timeout=900)
    m = re.search(r"SEP_SCORE (.+)", out or "")
    if rc != 0 or not m:
        return None
    d = {}
    for kv in m.group(1).split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                d[k] = float(v)
            except Exception:
                d[k] = v
    return d or None


def _sep_probe(job, src_wav, work, cands):
    """拿中间 30 秒片段，让各候选模型各分一遍，返回 [(模型, 分数), ...]。

    为什么只试 30 秒：全曲跑三个模型要多花 4~6 分钟，而"哪个模型更适合这首歌"
    这件事，30 秒的有代表性的片段基本就定下来了（实测三个模型的分数差 15 分，
    不是噪声级别的差异）。省下来的时间留给变声，那才是大头。
    """
    try:
        total = wav_seconds(src_wav)
    except Exception:
        total = 0.0
    probe = os.path.join(work, "probe.wav")
    if total and total > SEP_PROBE_SEC * 1.5:
        start = max(0.0, (total - SEP_PROBE_SEC) / 2.0)
        rc, out = run(job, '"%s" -y -v error -i "%s" -ss %.2f -t %.2f -ac 2 -ar 44100 "%s"'
                      % (FFMPEG, src_wav, start, SEP_PROBE_SEC, probe), timeout=600)
        if rc != 0 or not os.path.isfile(probe):
            log(job, "  试听片段切不出来，改用全曲去试（慢一点）")
            probe = src_wav
    else:
        probe = src_wav
    pin = os.path.join(work, "probe_in")
    shutil.rmtree(pin, ignore_errors=True)
    os.makedirs(pin, exist_ok=True)
    shutil.copyfile(probe, os.path.join(pin, "source.wav"))

    rows = []
    for m in cands[:SEP_MAX_CAND]:
        t0 = time.time()
        v, i, why = _uvr_run(job, m, pin,
                             os.path.join(work, "pv_" + m[:20]),
                             os.path.join(work, "pi_" + m[:20]), timeout=1200)
        if not v:
            log(job, "  候选 %s 没跑通（%.0fs）：%s" % (m[:28], time.time() - t0, why))
            continue
        sc = _sep_score(job, os.path.join(pin, "source.wav"), v, i)
        if not sc:
            log(job, "  候选 %s 分不出来（跳过）" % m[:28])
            continue
        rows.append({"model": m, "score": sc.get("score", 0.0), "leak": sc.get("leak"),
                     "secs": round(time.time() - t0, 1)})
        log(job, "  候选 %-28s 得分 %5.1f（漏伴奏 %.3f，%.0fs）"
            % (m[:28], sc.get("score", 0.0), sc.get("leak") or 0, time.time() - t0))
        _sep_stats_add(m, sc.get("score", 0.0))
    rows.sort(key=lambda r: -r["score"])
    return rows


def _sep_candidates():
    """候选顺序：历史平均分高的先试（省时间），没历史就按经验顺序。"""
    st = _sep_stats_load()
    def key(m):
        row = st.get(m) or {}
        return (-(row.get("avg") or 0), UVR_CANDIDATES.index(m) if m in UVR_CANDIDATES else 99)
    return sorted(UVR_CANDIDATES, key=key)


def _ncm_mod():
    """ncm 在根目录（不在 stages/），按需导入。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import ncm
    return ncm


# 解密产物放这儿：和分离缓存同根，清缓存时一起清；**不进曲库**（曲库是"用户
# 导入的歌"，而这里只是"为了能跑分离临时解出来的那一份"）。
DECODED_DIR = os.path.join(SEP_CACHE, "_decoded")


def ensure_playable_audio(job, src, out_dir=None):
    """把「ffmpeg 读不了的加密容器」先解成普通音频，返回可用路径。

    out_dir 决定解密产物落在哪，两种调用场景不一样：
      * 上传入口 ⇒ 传 `UPLOADS`：曲库会长期引用这个路径，必须放在稳定位置；
      * 分离兜底 ⇒ 不传：解到 `DECODED_DIR`（就是"为了能跑分离"临时解的），
        而曲库那条记录仍指向原 .ncm，用户下次从「我的音乐」导入时才会
        正式解密进 `data/music`。

    为什么需要这一步：从「我的音乐」直接选网易云的 VIP 歌时，拿到的可能是
    `.ncm` —— 它是**加密容器**，不是音频。原样喂给 ffmpeg 只会得到
    `Invalid data found when processing input`，而界面弹的是「原曲转码失败，
    可能是文件损坏或格式不认识」：驴唇不对马嘴，用户会去怀疑自己的文件坏了，
    而不是发现"它得先解密"。

    普通音频（mp3/wav/flac/m4a…）原样返回，不做任何多余动作。
    ★ 复用已有产物：同一首 .ncm 不会解出第二份（用户多点几次不该堆文件）。
    """
    import hashlib

    # job 允许是 None：上传接口在处理文件时还没有任务上下文，
    # 但同样需要"当场解密"这个能力。
    def _lg(msg):
        if job is None:
            print("[-] %s" % msg, flush=True)
        else:
            log(job, msg)

    src = str(src or "")
    if not os.path.isfile(src):
        return src
    try:
        if not _ncm_mod().is_ncm(src):
            return src
    except Exception:
        return src      # ncm 模块有问题不该把整条链带死，让下游按原样报错

    if out_dir:
        # 上传场景：产物就落在 out_dir 下，按歌名精确找（找到就复用）。
        # ★ 不能扫整个目录 —— uploads/ 里还躺着别人的歌，扫到谁算谁就完蛋了。
        stem = os.path.splitext(os.path.basename(src))[0]
        for ext in (".mp3", ".flac", ".m4a", ".wav"):
            p = os.path.join(out_dir, stem + ext)
            if os.path.isfile(p):
                _lg("  这首 .ncm 之前已经解过：%s" % os.path.basename(p))
                return p
        target = out_dir
    else:
        # 分离兜底：解到按源文件 hash 分的子目录 —— 只含这一首的产物，
        # 扫目录复用是安全的；同名不同歌（两首"曹操.ncm"）也不会互相盖。
        tag = hashlib.sha1(os.path.abspath(src).encode("utf-8")).hexdigest()[:16]
        target = os.path.join(DECODED_DIR, tag)
        if os.path.isdir(target):
            for f in sorted(os.listdir(target)):
                p = os.path.join(target, f)
                if os.path.isfile(p) and not f.endswith(".part"):
                    _lg("  这首是网易云加密的 .ncm，复用上次解出来的音频：%s"
                        % os.path.basename(p))
                    return p

    _lg("  这首是网易云加密的 .ncm（ffmpeg 读不了加密容器）⇒ 先解密成普通音频")
    try:
        r = _ncm_mod().decrypt(src, target)
    except Exception as e:
        raise Fail("这首是网易云加密的 .ncm，解密失败了：%s\n"
                   "它在客户端里能正常播放的话，可以先导出成 mp3 再放进来。"
                   % str(e)[:200])
    _lg("  解密完成：%s（%.1f 秒）"
        % (os.path.basename(r["path"]), r.get("dur") or 0))
    return r["path"]


def separate_song(job, song_path, model=None, use_cache=True):
    """把一首歌拆成「人声轨」和「伴奏轨」，返回 (vocal_wav, inst_wav)。

    唱歌**必须**先做这一步，否则：
      * 整首歌进 RVC ⇒ 鼓和贝斯也会被当成"唱"，输出跟着节奏抖成一团；
      * 混音时只能拿原曲当伴奏 ⇒ 原唱和角色一起唱（实测成品波形与原曲
        相关系数 0.870、原曲占比 0.538，就是"没法听"的主因）。

    缓存按「原曲 + 分离模型」做键：分离一首 4 分钟的歌要一两分钟，而用户改一个
    参数（音高/相似度）就要重跑一次，每次都重新分离纯属浪费。
    """
    # .ncm 是加密容器，ffmpeg 读不了 ⇒ 先解成普通音频，再算缓存键、往下走。
    # 放最前面是有意的：唱歌 / 识别歌词 / 试听小样三条链路都经过这里，
    # 兜在这一点，任何入口送进来的加密歌都不会再走到 ffmpeg 那一步去撞墙。
    song_path = ensure_playable_audio(job, song_path)

    key = _sep_cache_key(song_path, model)
    cache_dir = os.path.join(SEP_CACHE, key)
    vocal = os.path.join(cache_dir, "vocal.wav")
    inst = os.path.join(cache_dir, "inst.wav")
    meta_path = os.path.join(cache_dir, "meta.json")

    if use_cache and os.path.isfile(vocal) and os.path.isfile(inst):
        who = ""
        try:
            mt = json.load(open(meta_path, encoding="utf-8"))
            who = "（上次选的是 %s，得分 %.1f）" % (
                str(mt.get("chosen") or "")[:28], float(mt.get("score") or 0))
        except Exception:
            pass
        log(job, "  复用上次分离好的「人声 + 伴奏」%s" % who)
        return vocal, inst

    work = os.path.join(cache_dir, "_work")
    in_dir = os.path.join(work, "in")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(in_dir, exist_ok=True)
    # 统一转成 2 声道 44.1kHz —— UVR5 原版就要求这个格式，视频抽出来的
    # 音频/各种码率的 mp3 直接喂进去有的模型会炸。
    src = os.path.join(in_dir, "source.wav")
    rc, out = run(job, '"%s" -y -v error -i "%s" -ac 2 -ar 44100 "%s"'
                  % (FFMPEG, song_path, src), timeout=1800)
    if rc != 0 or not os.path.exists(src):
        raise Fail("原曲转码失败，可能是文件损坏或格式不认识。\n%s" % _tail(out, 3))

    vdir = os.path.join(work, "vocal")
    idir = os.path.join(work, "inst")

    # ---- 选模型：先试 30 秒片段打分，再让分最高的跑全曲 ----
    chosen, rows = (model or UVR_MODEL_BEST), []
    if not model:
        log(job, "  先拿 %.0f 秒片段试几个分离器，谁分高用谁" % SEP_PROBE_SEC)
        rows = _sep_probe(job, src, work, _sep_candidates())
        if rows:
            chosen = rows[0]["model"]
            log(job, "  ⇒ 用 %s（得分 %.1f）跑完这一首"
                % (chosen[:28], rows[0]["score"]))
        else:
            chosen = UVR_MODEL_BEST
            log(job, "  一个候选都没跑通，按经验用 %s" % UVR_MODEL_BEST)

    v, i, why = _uvr_run(job, chosen, in_dir, vdir, idir)
    if not v and chosen != UVR_MODEL_SAFE:
        # Roformer 需要配套的 .yaml，整合包不一定带 ⇒ 读不到权重会直接失败。
        # 不能让它把整条链带死 —— 回退到训练时一直在用的 VR 模型。
        log(job, "  %s 全曲没跑通：%s" % (chosen[:28], why))
        chosen = UVR_MODEL_SAFE
        log(job, "  改用常规分离器 %s（质量略低但稳定）" % UVR_MODEL_SAFE)
        v, i, why = _uvr_run(job, chosen, in_dir, vdir, idir)
    if not v:
        # 最后一个兜底：每个候选都试一遍全曲，绝不因为"选错了"就整条链挂掉
        for m in _sep_candidates():
            if m == chosen:
                continue
            log(job, "  再试 %s …" % m[:28])
            v, i, why = _uvr_run(job, m, in_dir, vdir, idir)
            if v:
                chosen = m
                break
    if not v:
        raise Fail("人声分离失败，所有分离器都没跑通。\n最后一个错误：%s" % why)

    # ---- 二次分离：漏伴奏还多的话，把人声轨再分离一遍（去残留）----
    # 这是 UVR 社区的标准操作：第一遍剩下的伴奏，第二遍往往能再刮掉一层。
    # 只在**确实漏了**的时候做（leak 超过阈值），不白花两分钟。
    sc1 = _sep_score(job, src, v, i)
    if (not model) and sc1 and (sc1.get("leak") or 0) > SEP_RETRY_LEAK:
        log(job, "  第一遍漏伴奏还不少（leak=%.3f），把人声轨再分离一遍"
            % sc1["leak"])
        v2dir = os.path.join(work, "vocal2")
        i2dir = os.path.join(work, "inst2")
        v_in = os.path.join(work, "vin")
        shutil.rmtree(v_in, ignore_errors=True)
        os.makedirs(v_in, exist_ok=True)
        shutil.copyfile(v, os.path.join(v_in, "source.wav"))
        v2, i2, why2 = _uvr_run(job, chosen, v_in, v2dir, i2dir, timeout=1800)
        if v2:
            sc2 = _sep_score(job, v, v2, i2)
            if sc2 and (sc2.get("score") or 0) > (sc1.get("score") or 0):
                log(job, "  第二遍更好（%.1f → %.1f），用第二遍的人声轨"
                    % (sc1["score"], sc2["score"]))
                v, i, sc1 = v2, i2, sc2
            else:
                log(job, "  第二遍没更好（%.1f → %s），保留第一遍"
                    % (sc1["score"], sc2 and sc2.get("score")))
        else:
            log(job, "  第二遍没跑通（%s），保留第一遍" % why2)

    # 原子落盘：先写 .part 再 replace，避免半个文件被下次当成有效缓存复用。
    os.makedirs(cache_dir, exist_ok=True)
    for s, d in ((v, vocal), (i, inst)):
        tmp = d + ".part"
        shutil.copyfile(s, tmp)
        os.replace(tmp, d)
    try:
        json.dump({"chosen": chosen, "score": (sc1 or {}).get("score"),
                   "leak": (sc1 or {}).get("leak"), "rows": rows,
                   "ver": SEP_POLICY, "at": _now()},
                  open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    shutil.rmtree(work, ignore_errors=True)
    log(job, "  人声轨 %.1f 秒 / 伴奏轨 %.1f 秒（%s，得分 %s）"
        % (wav_seconds(vocal), wav_seconds(inst), chosen[:28],
           ("%.1f" % sc1["score"]) if sc1 else "未评分"))
    return vocal, inst


def step_slice(job, vocal_dir, out_dir):
    job["stage"] = "切分片段"
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    # ★ 先走保真切片（48k，见 SLICE_SR 的注释）；它跟自带工具切出的是**同一批段**，
    #   只是采样率更高。真出问题时**退化**到自带那份 32k —— 增强功能失败不许比
    #   改之前更差，这条边界必须现场兜住，不能靠"它应该不会失败"。
    hifi = os.path.join(HERE, "stages", "slice_hifi.py")
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" %d %s'
                  % (PY, hifi, vocal_dir, out_dir, SLICE_SR, " ".join(SLICE_ARGS)),
                  env={"GSV_ROOT": GSV_ROOT}, timeout=1800)
    files = [f for f in os.listdir(out_dir) if f.lower().endswith(".wav")]
    if not files:
        log(job, "  保真切片（%d Hz）没切出东西，退回自带切片工具（32k）" % SLICE_SR)
        rc, out = run(job, '"%s" -s tools/slice_audio.py "%s" "%s" %s'
                      % (PY, vocal_dir, out_dir, " ".join(SLICE_ARGS)), timeout=1800)
        files = [f for f in os.listdir(out_dir) if f.lower().endswith(".wav")]
    if not files:
        raise Fail("一个片段都没切出来，可能人声轨是空的。")
    good = []
    for f in files:
        p = os.path.join(out_dir, f)
        s = wav_seconds(p)
        r = wav_rms(p)
        if s >= 1.0 and s <= 15.0 and r > 0.005:
            good.append((f, s, r))
    log(job, "  切出 %d 段，合格 %d 段" % (len(files), len(good)))
    if len(good) < 5:
        raise Fail("只有 %d 段可用（至少 5 段）。" % len(good))
    return out_dir, [f for f, _, _ in good]


def step_feat(job, slice_dir, out_json):
    job["stage"] = "提取声纹"
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "feat_cli.py"), GSV_ROOT, slice_dir, out_json),
                  timeout=3600)
    if not os.path.exists(out_json):
        raise Fail("特征提取失败。")
    return json.load(open(out_json, encoding="utf-8"))


def step_split_speakers(job, feats, n_speakers=None):
    job["stage"] = "分离不同声音"
    # 必须**用整合包的 python 起子进程**跑，不能在服务进程里 import diarize。
    #
    # 事故记录：服务是用零依赖的托管Python 起的（不是整合包那个），
    # `import diarize` 当场ModuleNotFoundError: No module named 'numpy'，
    # 整个导入流程在第 6 步崩掉 —— 而前 5 步（抽音/UVR/切片/特征）全部正常，
    # 所以界面上看是"跑到一半 Training failed"，很难想到是缺依赖。
    #
    # 教训：**依赖 numpy 的模块不能在服务进程里 import**，服务用什么解释器
    # 启动是可变的（bat 用整合包、手动起可能用别的）⇒ 不能把正确性押在它上面。
    # 跟其它步骤统一走子进程，就彻底与"服务用什么 python"解耦了。
    work = os.path.join(job["dir"], "work")
    os.makedirs(work, exist_ok=True)
    fin = os.path.join(work, "feats_for_split.json")
    fout = os.path.join(work, "split_result.json")
    with open(fin, "w", encoding="utf-8") as f:
        json.dump(feats, f, ensure_ascii=False)
    n_arg = "" if n_speakers is None else str(int(n_speakers))
    # 这里踩过一个很坑的错：写成
    #     run(job, '"%s" -s "%s" "%s" %s' % (PY, ..., fin, fout, n_arg))
    # 报TypeError: not all arguments converted during string formatting。
    # 看着像"格式化语法错"，其实**报错来自 run() 内部那行 log()**——
    # 命令行里带了% 字符（本机路径就有 %USERPROFILE% 之类），
    # 而 run() 里对命令做了 % 格式化/百分号处理。教训：**带 % 的路径
    # 不能进 %格式化**。所以这里改用 str.format，占位符用 {}。
    cmd = '"{}" -s "{}" "{}" {}'.format(
        PY, os.path.join(STAGES, "diarize.py"), fin, fout, n_arg)
    rc, out = run(job, cmd)
    if rc != 0 or not os.path.exists(fout):
        raise Fail("说话人分离失败（退出码 %s）。%s" % (rc, _tail(out)))
    res = json.load(open(fout, encoding="utf-8"))

    for t in res.get("trace", []):
        log(job, "    k=%s %s —— %s" % (t[0], "成立" if t[1] else "不采纳", t[2]))
    groups = {}
    for f, lab in zip(feats, res["labels"]):
        groups.setdefault(int(lab), []).append(f)
    log(job, "  分成 %d 组：%s" % (res["k"], {k: len(v) for k, v in sorted(groups.items())}))
    return groups, res


# ------------------------------------------------------------ 声纹认人
#
# 需求原话：「可以在添加素材的情况下自动区分这个素材是谁，把属于这个角色的素材加进去，
# 不会被其他角色影响」。以前只有两条路：全新建一个角色（同一个人的新素材会散成好几个
# 角色），或者用户在界面上手动指定要塞给谁。这里补第三种：**自动认人**。
#
# 判据 = 「新素材每一段到某个角色中心向量的余弦距离」的**中位数**。
# 为什么不是"两个中心之间的余弦距离"（第一版就是这么写的，被实测否掉）：
#   中心对中心主要量的是"每侧有多少段"（抽样噪声），不是"是不是同一个人"。
#   实测：同一个角色切 4 份（每份 53 段）中心距离最大 0.131，而随机分半（每份
#   106 段）只有 0.004 —— 差两个数量级；换成"异人"侧更是直接重叠
#   （同人 0.030~0.131 vs 异人 0.066~0.264）。
#   改用逐段对中心取中位数后：段少也不会把中心带跑，少数分错的段也挪不动中位数。
#
# ★ 用哪一份向量：`sv`，**不是** `emb`。feat_cli 一次存两份（见 stages/feat_cli.py）：
#     emb = `compute_embedding3()` 走 `forward3`，是**时间平均后的融合特征**（20480 维）。
#           GPT-SoVITS 拿它做别的用途，不是给"是不是同一个人"训练的。
#     sv  = 模型自带的说话人嵌入头 `forward` → `seg_1(stats)`（**192 维**）。
#           eres2net 这份预训练权重本来就是为说话人验证训的，这一层才是真嵌入。
#   实测（`_probe_speaker_combo.py`：真实素材，40 正 / 40 负）：
#     阈值        emb 收进率 / 误收率      sv 收进率 / 误收率
#     0.20        68% / 0%                70% / 0%
#     0.22        88% / **2%**           85% / 0%
#     0.23        88% / **10%**          92% / 0%
#     0.25        98% / **15%**          92% / **8%**
#   ⇒ `emb` 的"零误收安全带"只有 0.20~0.21（宽 0.01），一放宽立刻漏；
#     `sv` 的安全带是 0.20~0.24（宽 0.04），同一档上收进率还更高。
#   ⇒ 附带的实惠：192 维 vs 20480 维 —— 角色声纹缓存小 100 倍、比对快 100 倍。
#   ★ 还量过"两个向量 AND 投票"（要两道闸都放行才并）：**没有收益**。
#     异人侧样本两道闸拦的是同一批（"只有 emb 拦下" 0 个、"只有 sv 拦下" 0 个），
#     所以 AND 只会被弱的那道（emb）拖住 —— 同人收进率从 92% 掉到 78%，误收没有更少。
#     ⇒ 用 sv 单路。
#
# ★★ **薄中心**才是真正的坑（这一条是被一条"红得有道理"的门禁判据逼出来的）。
#   门禁里那个基准角色的中心只用 **17 段**算出来，结果"别人的素材"到它只有 0.2103
#   —— 低于当时的阈值 0.23 ⇒ 库里要是没有那个人自己的角色卡，这段素材就会被并进来。
#   一开始我猜是"中心太糊 ⇒ 什么都离它近"，被实测**否掉**：同人自比几乎不随段数变
#   （0.180 → 0.163）。真正的原因是异人侧的**左尾**被压低了。40 次抽样量出来
#   （`_probe_center_quality.py`；★ 每档必须抽够 40 次 —— 只抽 8 次时 p10 取到的是
#   最小值，结果在 0.198~0.272 之间乱跳，看着像"没规律"，其实是样本不够）：
#     中心用 k 段 | 同人 p95（越高越危险） | 异人 p25（越低越危险） | 异人 p10
#       8        | **0.3151**            | 0.2517                | 0.1976
#       12       | 0.1981                | 0.2566                | 0.2355
#       17       | 0.2007                | 0.2579                | 0.2404
#       22       | 0.1973                | 0.2496                | 0.2266
#       30       | 0.1897                | 0.2652                | 0.2379
#       45       | 0.1891                | 0.2684                | 0.2371
#       60       | 0.1801                | 0.2614                | 0.2453
#       120      | 0.1800                | 0.2624                | 0.2521
#   ⇒ 段数 < 30 时异人 p10 落到 0.2266~0.2404（**低于 0.23 阈值** ⇒ 会误收），
#     而 ≥30 段时异人 p25 稳定在 0.26 以上、同人 p95 在 0.19 以下，余量有 0.07。
#   ⇒ 两条修正：① 中心段数 < MIN_CENTER_SEGS 的角色**不作为认人候选**（薄中心直接出局）；
#              ② 阈值从 0.23 收到 **0.21**。
#     在 ≥30 段中心的条件下：同人 p95 0.19 < **0.21** < 异人 p25 0.26。
#
# 阈值实测（`_probe_speaker_calib.py` + `_probe_speaker_combo.py` + `_probe_center_quality.py`）：
#   sv 同人 p10/med/p90 = 0.144 / 0.178 / 0.228；异人 = 0.266 / 0.281 / 0.317。
#   ⚠ **样本只有 1 个异人**（另一个视频里的人声）。"误收 0%"只能说是"在这一个人
#     身上没漏"，不等于在所有人身上都不漏。所以取向定死：**宁可认不出（新建角色），
#     也不许错并** —— 错并是静默污染训练集，认不出只是多建一个角色、用户手动一并就好。
VOICE_KIND = "sv"        # 声纹向量种类；换模型/换向量时改这里 ⇒ 老缓存自动失效重算
VOICE_DIM_REF = 192      # 仅供文档/门禁参考；真正的判据是"两侧维度相等"
MATCH_DIST = 0.21        # 逐段到中心距离的**中位数**门槛（见上；≥30 段中心下安全带 0.19~0.26）
MIN_CENTER_SEGS = 30     # 中心至少用这么多段才算"实心"，否则不作为候选（见上，薄中心会误收）
MATCH_MIN_SEGS = 6       # 新素材少于 6 段，中位数不稳 ⇒ 宁可不认（认错比认不出更糟）
# ★ 维度**不写死**。第一版我按文档写了 VOICE_DIM=192，第一次跑就翻车：feat_cli 存下来的
#   `emb` 不是 eres2net 那一层 192 维的 embedding，而是 `compute_embedding3` 出来的
#   多层拼接（实测 20480 维）。真正要守的性质不是"等于 192"，而是"**两侧维度必须一致，
#   不一致就不许比**"（长度不一样的向量 zip 起来只会静默截断，得出一个看起来正常的错数）。
#   所以维度只记在角色记录里（voice.dim），比之前先核对。
ROLE_VOICE_SEGS = 60     # 一个角色的声纹中心最多抽多少段来算（稳定 vs 耗时）
VOICE_MIN_DUR = 2.0      # 和 diarize.py 的质量门控保持一致
VOICE_MIN_VOICED = 0.30


def _emb_center(embs):
    """一组 embedding 的均值，再 L2 归一化。返回 list（好写进 json）。"""
    import math
    if not embs:
        return None
    n, d = len(embs), len(embs[0])
    c = [0.0] * d
    for e in embs:
        for i, v in enumerate(e):
            c[i] += v
    c = [v / n for v in c]
    norm = math.sqrt(sum(v * v for v in c)) or 1e-12
    return [v / norm for v in c]


def _emb_dist(a, b):
    """余弦距离（0 = 同一个向量）。两条都得是先归一化过的中心。

    ★ 长度不一致时**返回 None，绝不上截断**：zip 会静默按短的那个截断，
      然后算出一个"看起来很正常"的距离 —— 两个不同模型的特征被这么比过，
      结论是没有意义的（踩过的同类坑：坐标系不一样就该显式拒绝）。
    """
    import math
    if not a or not b or len(a) != len(b):
        return None
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(x * x for x in b)) or 1e-12
    return 1.0 - num / (na * nb)


def _voice_pick(files, limit=ROLE_VOICE_SEGS, segs=None):
    """从一批切片里挑出用来算声纹中心的段：先按质量门控筛，再等间隔抽样。

    等间隔（而不是取前 N 个）是因为切片是按时间排的，取前面 60 段可能全是同一段
    副歌/同一句话，中心会偏向那一段的音色；等间隔能铺满整条素材。
    """
    ok = []
    meta = {s.get("file"): s for s in (segs or [])}
    for f in files:
        m = meta.get(os.path.basename(f)) or {}
        dur = float(m.get("dur") or 0)
        voiced = float(m.get("voiced") or 0)
        # 没带元信息时（老角色）只能先收进来，靠 feat 结果再筛
        if meta and (dur < VOICE_MIN_DUR or voiced < VOICE_MIN_VOICED):
            continue
        ok.append(f)
    if not ok:
        return []
    if len(ok) <= limit:
        return ok
    step = len(ok) / float(limit)
    return [ok[int(i * step)] for i in range(limit)]


def _feat_subprocess(files, tag):
    """让 feat_cli 只算 `files` 这几段，返回它输出的记录列表（失败返回 []）。

    必须走子进程：feat_cli 依赖 numpy/torch，而服务用什么解释器起是可变的
    （见 step_split_speakers 上面那段事故记录，同一个坑不再踩第二次）。

    ★ **名字走 argv、结果走 stdout ⇒ 零拷贝、零临时文件、零删除。**
      这版之前是"mkdtemp + copy 60 个 wav + 跑完 rmtree"，更早一版是"写一个清单文件、
      跑完删掉" —— 两种写法一次调用都是几十个删除动作，而**本机环境层有批量删除钩子**
      （阈值 50/轮）。被它掐断的样子是"整个脚本一行输出都没有"，非常难查。
      顺带还省掉了 60 个 4 秒 wav 的复制（每次导入都要拷一遍）。
    ★ Windows 命令行有长度上限（约 32K），名字太多时宁可不认人，也不塞爆。
    ★ 这里**不能**借 run(None, ...)：run 里第一句就是 log(job, ...)，而 log 直接
      job.get(...) —— 传 None 当场 AttributeError。认人这一步可能在"没有任务上下文"
      的时候被调到（比如给老角色补算中心），所以自己起子进程、自己收输出。
    """
    if not files:
        return []
    dirs = {os.path.dirname(f) for f in files}
    if len(dirs) != 1:
        # 只支持"同一个目录里的若干文件"；跨目录宁可不认，别猜
        return []
    names = [os.path.basename(f) for f in files]
    if sum(len(n) + 1 for n in names) > 20000:
        return []
    cmd = [PY, "-s", os.path.join(STAGES, "feat_cli.py"),
           GSV_ROOT, dirs.pop(), "-"] + names
    try:
        r = subprocess.run(cmd, cwd=GSV_ROOT, env=base_env(),
                           capture_output=True, text=True, timeout=1800)
    except Exception as e:
        sys.stderr.write("[warn] 声纹提取(%s)起不来：%s\n" % (tag, e))
        return []
    if r.returncode != 0 or not (r.stdout or "").strip():
        sys.stderr.write("[warn] 声纹提取(%s)失败：rc=%s %s\n"
                         % (tag, r.returncode, (r.stderr or "")[-200:]))
        return []
    try:
        return json.loads(r.stdout)
    except Exception as e:
        sys.stderr.write("[warn] 声纹提取(%s)的输出不是 JSON：%s\n" % (tag, e))
        return []


def build_role_voice(rid, job=None, force=False):
    """算并落盘一个角色的声纹中心。返回 voice dict（失败返回 None）。

    结果缓存在角色记录的 `voice` 字段里（跟 segments 一起进 roles.json）。
    素材变了（追加/重拆）必须用 `force=True` 重算 —— 调用点就是 job_ingest 的建档循环
    （那边每建/更一次角色都 force 一次）。不重算的话下一次认人用的还是老中心，
    而这个角色已经多了一批素材，严重时会把别人认成它。
    （注：这里**没有**单独的"标脏"函数，别去找 `refresh_role_voice` —— 不存在，
      历史上只是文档里提过一句，实际做法一律是就地 force 重算。）
    ★ 缓存带 `kind`（用的哪一路向量）。`kind` 和当前 VOICE_KIND 不一致 ⇒ 视为**陈旧**，
      即使不 force 也重算 —— 否则从 emb 换到 sv 之后，老角色的 20480 维中心会一直
      被当成有效缓存：维度对不上 ⇒ 每次认人它都被 skip ⇒ 表现成"这个角色永远认不出"，
      而且**不报错**。这类"看着正常实则是废数据"的缓存必须靠版本号淘汰。
    """
    role = get_role(rid)
    if not role:
        return None
    v = role.get("voice") or {}
    if v.get("center") and v.get("kind") == VOICE_KIND and not force:
        return role["voice"]
    sd = os.path.join(role_dir(rid), "slices")
    if not os.path.isdir(sd):
        return None
    files = [os.path.join(sd, f) for f in sorted(os.listdir(sd))
             if f.lower().endswith(".wav")]
    picked = _voice_pick(files, segs=role.get("segments"))
    if len(picked) < MATCH_MIN_SEGS:
        return None
    vecs = [x[VOICE_KIND] for x in _feat_subprocess(picked, rid)
            if x.get(VOICE_KIND)]
    if len(vecs) < MATCH_MIN_SEGS:
        return None
    dims = {len(e) for e in vecs}
    if len(dims) != 1:
        # 同一批里维度都不一致，说明提取那边出了状况 —— 宁可不认
        raise Fail("同一批声纹的维度不一致：%s" % sorted(dims))
    c = _emb_center(vecs)
    voice = {"center": c, "n": len(vecs), "dim": len(c), "kind": VOICE_KIND,
             "model": "eres2net/feat_cli", "at": _now()}
    mutate_role(rid, lambda r: r.__setitem__("voice", voice))
    if job:
        log(job, "  角色 %s 的声纹中心：用了 %d 段（%s，%d 维）"
            % (rid, len(vecs), VOICE_KIND, len(c)))
    return voice


def ensure_role_voices(job=None, rids=None):
    """把还缺声纹中心的角色补算出来（认人之前调一次）。

    为什么要这一步：这个功能是后加的，**老角色记录里没有 voice 字段**
    （比如用户以前导入的素材）。不补算的话，第一次用"自动认人"时会发现
    "库里一个中心都没有"，于是全部新建角色 —— 功能看起来像没生效。
    补算一次就缓存在角色记录里，之后每次导入都直接复用。
    ★ 换了声纹向量（`kind` 变了）也算"缺"：老中心维度对不上，留着只会静默失效。

    返回 (算好了几个, 跳过几个)。跳过 = 切片目录不在或段数太少。
    """
    made = skipped = 0
    for r in load_roles()["roles"]:
        if rids and r["id"] not in rids:
            continue
        v = r.get("voice") or {}
        # 有中心**而且是对的那一路向量**才算有效缓存；kind 变了要重算（见 build_role_voice）
        if v.get("center") and v.get("kind") == VOICE_KIND:
            continue
        if r.get("pending"):
            continue
        try:
            if build_role_voice(r["id"], None, force=True):
                made += 1
                if job:
                    log(job, "  补算声纹中心：%s（%s）" % (r.get("name"), r["id"]))
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            if job:
                log(job, "  [warn] %s 的声纹中心算不出：%s" % (r.get("name"), e))
    return made, skipped


def match_clusters(job, groups, feats):
    """给每个簇找「这是谁」。返回 ({簇标签: 角色id}, [给人看的说明])。

    只认**过了阈值**的；认不出的返回 None，由上层新建角色。
    ★ 判据是"这一堆里的每一段到角色中心距离的**中位数**"，不是"两个中心之间的距离"
      —— 原因和实测数据见 MATCH_DIST 上面的注释（中心对中心会被抽样噪声带跑）。
    """
    meta = {f["file"]: f for f in feats}
    known = []
    skipped_dim = stale = thin = 0
    for r in load_roles()["roles"]:
        v = r.get("voice")
        if v and v.get("center"):
            if v.get("kind") != VOICE_KIND:
                # 老缓存（换了向量种类）—— 上层应该已经用 ensure_role_voices 补算过；
                # 还在这儿说明补算失败/被跳过。绝不拿它比（维度对不上会静默截断）。
                stale += 1
                continue
            # ★ 中心段数不够 ⇒ 不出任候选。实测：<30 段时"异人"侧 p10 会掉到
            #   0.2266~0.2404，低于阈值 ⇒ 别人的素材会被并进来（薄中心把异人距离整体压低）。
            #   这类角色仍然留着中心（素材攒够了自己会变"实心"），只是这次不参与认人。
            if int(v.get("n") or 0) < MIN_CENTER_SEGS:
                thin += 1
                continue
            known.append((r["id"], r.get("name") or r["id"], v["center"], v.get("dim")))
    if not known:
        bits = []
        if stale:
            bits.append("%d 个是旧格式声纹（%s 之前算的）" % (stale, VOICE_KIND))
        if thin:
            bits.append("%d 个的声纹中心段数不足 %d（薄中心会误收，不许当候选）"
                        % (thin, MIN_CENTER_SEGS))
        return {}, ["角色库里没有可用的声纹中心%s（这次全部新建角色）"
                    % ("：" + "、".join(bits) if bits else "（第一次导入时这是正常的）")]

    assign, notes = {}, []
    if thin:
        notes.append("有 %d 个角色的声纹中心段数不足 %d，这次不当候选"
                     "（薄中心会把别人认成它）" % (thin, MIN_CENTER_SEGS))
    for lab in sorted(groups, key=lambda x: str(x)):
        try:
            if int(lab) < 0:            # 没挂上的杂音，不参与认人
                continue
        except (TypeError, ValueError):
            pass
        segs = groups[lab]
        vecs = [meta[f["file"]][VOICE_KIND] for f in segs
                if meta.get(f["file"], {}).get(VOICE_KIND)]
        if len(vecs) < MATCH_MIN_SEGS:
            notes.append("第 %s 堆只有 %d 段，太短不认人（宁可不认，也不乱认）"
                         % (lab, len(vecs)))
            continue
        d_new = len(vecs[0])
        # 每一段各自和每个角色的中心比，取中位数当这一堆的"像不像"
        scored = []
        for kid, kn, kc, kdim in known:
            if kdim != d_new:
                skipped_dim += 1
                continue
            ds = sorted(x for x in (_emb_dist(e, kc) for e in vecs)
                        if x is not None)
            if not ds:
                continue
            scored.append((ds[len(ds) // 2], kid, kn))
        if not scored:
            notes.append("第 %s 堆的声纹维度（%d）和库里所有角色都对不上，"
                         "这次不认人（换了声纹模型时必须重新建档）"
                         % (lab, d_new))
            continue
        scored.sort(key=lambda t: t[0])
        d, kid, kn = scored[0]
        if d <= MATCH_DIST:
            assign[lab] = kid
            notes.append("第 %s 堆（%d 段）认成「%s」：中位距离 %.3f ≤ 阈值 %.2f"
                         % (lab, len(segs), kn, d, MATCH_DIST))
        else:
            second = ("；第二近的「%s」%.3f" % (scored[1][2], scored[1][0])
                      if len(scored) > 1 else "")
            notes.append("第 %s 堆（%d 段）谁都不像：最近的「%s」中位距离 %.3f > 阈值 %.2f%s"
                         " ⇒ 新建角色"
                         % (lab, len(segs), kn, d, MATCH_DIST, second))
    return assign, notes


def step_asr(job, slice_dir, out_dir, lang="zh"):
    job["stage"] = "语音识别"
    os.makedirs(out_dir, exist_ok=True)
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" %s'
                  % (PY, os.path.join(STAGES, "asr_cli.py"), GSV_ROOT, slice_dir, out_dir, lang),
                  timeout=3600)
    list_path = os.path.join(out_dir, "%s.list" % os.path.basename(slice_dir))
    if not os.path.exists(list_path):
        raise Fail("语音识别没产出标注文件。")
    texts = {}
    for l in open(list_path, encoding="utf-8"):
        p = l.rstrip("\n").split("|")
        if len(p) >= 4:
            texts[os.path.basename(p[0].replace("\\", "/"))] = p[3].strip()
    return texts


def _disk_free(path):
    """返回该路径所在磁盘的剩余字节数；拿不到就返回 None（不当成 0）。"""
    try:
        import shutil
        p = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        while p and not os.path.isdir(p):
            p = os.path.dirname(p)
        return shutil.disk_usage(p or ".").free
    except Exception:
        return None


def step_check_disk(job, need_gb=6.0):
    """训练前检查磁盘空间。

    以前是**跑到第 4 个epoch 存 ckpt 时才炸** No space left on device：
    GPU 已经空转了几十分钟，用户看到的是一堆 traceback，完全不知道是磁盘满了。
    这里提前量一次，不够就当场说清楚缺多少、缺在哪个盘。

    need_gb 的来由：一次 v5turbo 训练保存的 ckpt 单个约 890MB，
    lightning 默认留 topk=1 + last 一份，加上 hubert/wav32k/semantic 中间产物，
    按 6GB 留余量是实测过的保守值。
    """
    free = _disk_free(GSV_ROOT)
    if free is None:
        return
    need = need_gb * 1024 ** 3
    if free >= need:
        log(job, "  磁盘可用 %.1f GB，够用" % (free / 1024.0 ** 3))
        return
    # 光说"请清理磁盘"等于没说：用户不知道该删什么、也不敢乱删。
    # 所以这里直接告诉他顶栏有「存储」面板，并且报出那里现在能腾出多少 ——
    # 这两个数字并排，他才知道"清一下就够了"还是要另想办法。
    try:
        reclaim = scan_storage().get("reclaimable") or 0
    except Exception:
        reclaim = 0
    tip = ("\n顶栏「🧹 存储」里现在能腾出 %.1f GB%s。"
           % (reclaim / 1024.0 ** 3,
              "，清掉就够这次训练了" if reclaim >= need - free else
              "，还不够，需要再腾 %.1f GB" % ((need - free - reclaim) / 1024.0 ** 3))
           if reclaim else "")
    raise Fail(
        "磁盘空间不足：训练产物目录所在磁盘只剩 %.1f GB，至少需要 %.0f GB。\n"
        "训练会在保存模型权重时崩掉（No space left on device），"
        "而且那时 GPU 已经空转了几十分钟。\n"
        "请先清理磁盘后重试。%s" % (free / 1024.0 ** 3, need_gb, tip))


def step_prep(job, exp_name, list_path, slice_dir):
    job["stage"] = "格式化数据集"
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "prep_all.py"), GSV_ROOT, exp_name, list_path, slice_dir),
                  timeout=3600)
    if rc != 0:
        raise Fail("数据集格式化失败（退出码 %d）。" % rc)


def step_train(job, exp_name, epochs, batch, version="v5turbo",
               pct_from=0, pct_to=88):
    job["stage"] = "训练音色"
    job["epochs_total"] = epochs
    job["epochs_done"] = None
    step_check_disk(job)
    # batch 先按大的试，显存不够再往下退 —— 8GB 卡上 batch 8 通常比 4 快一截
    for b in ([batch] if batch else [8, 4, 2]):
        log(job, "  尝试 batch=%d" % b)

        def _track(line, _b=b):
            """从 lightning 的进度行里读出"第几轮 / 共几轮"。

            实测那行的样子是：
                Epoch 14/14 ---------------- 15/15 0:00:01 • 10.66it/s v_num: 1.000
            它用 \r 刷新、同一行反复覆盖，所以按 \n 切出来的"日志行"里
            **只有最后一条留存**（真实日志里 grep "Epoch" 只有 1 处就是证明）。
            ⇒ 必须边跑边解析，等跑完再回头看日志是拿不到中间进度的。

            区间用 pct_from→pct_to 而不是写死 0→88：一次导入可能要连着训
            多个角色，每个角色都得在自己那一段里推进度，否则第二个角色会
            直接继承前一个的 88%，进度条在多角色场景下彻底失真。
            """
            m = re.search(r"Epoch\s+(\d+)\s*/\s*(\d+)", line)
            if not m:
                return
            done, total = int(m.group(1)) + 1, int(m.group(2))
            # 不封顶的话进度条会在最后一个 epoch 冲到区间顶端然后卡住不动 ——
            # 那比不显示更像坏了。剩下的留给体检环节。
            span = max(pct_to - pct_from, 1)
            job["epochs_done"] = done
            job["pct"] = max(job.get("pct") or 0,
                             int(pct_from + span * done / max(total, 1)))
            job["detail"] = "第 %d / %d 轮" % (done, total)
            job["stage"] = "训练音色 · 第 %d/%d 轮" % (done, total)
            save_job(job)

        rc, out = run(job, '"%s" -s "%s" "%s" %s %s %d %d 2 5'
                      % (PY, os.path.join(STAGES, "train_cli.py"), GSV_ROOT, exp_name, version, b, epochs),
                      timeout=7200, on_line=_track)
        if rc == 0:
            break
        o = out.lower()
        if "out of memory" in o or "cuda oom" in o or "allocation" in o:
            log(job, "  显存不够，降到更小的 batch 重试")
            continue
        # 磁盘满的报错长得像一堆系统调用栈，用户看不出真正原因 —— 单独识别出来。
        if "no space left on device" in o or "errno 28" in o:
            raise Fail(
                "磁盘空间不足：训练跑到一半存不下模型权重了。\n"
                "产物目录所在磁盘只剩 %.1f GB。\n"
                "请先清理磁盘再重试。"
                % ((_disk_free(GSV_ROOT) or 0) / 1024.0 ** 3))
        raise Fail("训练失败（退出码 %d）。\n%s" % (rc, out[-1000:]))
    else:
        raise Fail("所有 batch 都因显存不足失败，请关掉其它占显存的程序后重试。")

    cks = sorted([f for f in os.listdir(GPT_WEIGHT_DIR)
                  if f.startswith(exp_name + "-e") and f.endswith(".ckpt")]) \
        if os.path.isdir(GPT_WEIGHT_DIR) else []

    def ep(f):
        try:
            return int(f.split("-e")[-1].split(".")[0])
        except Exception:
            return -1

    if not cks:
        raise Fail("训练说跑完了，但没找到权重文件。")
    best = max(cks, key=ep)
    log(job, "  权重: %s" % ", ".join(cks))
    return os.path.join("GPT_weights_v5turbo", best), ep(best)


REF_VOICE_TOPN = 12   # 参考音频声纹复核最多比这么多段（再多只是拖慢每次"说话"）
# ★ 声纹复核的总开关。留成开关而不是写死，是因为它**实测会让合成变不稳定**
#   （2026-10-16）：大帝身上，开着它"换语气带来的音高差"从 65 Hz 掉到 30.9 Hz，
#   而"同一句重复合成的抖动"从 0.2 Hz 涨到 21.3 Hz ⇒ 语气控制的信噪比 318x → 1.4x，
#   `verify_tone` 的负对照当场判红。默认值见下，改这一个常量就能切换做 A/B。
REF_USE_VOICE = False

# ---- 参考音频的合法时长：3~10 秒（**GPT-SoVITS 的硬性要求，不是我们的偏好**）----
# 出处：GPT_SoVITS/TTS_infer_pack/TTS.py:917 —— 16k 采样点数必须落在
#   48000 ~ 160000 之间（= 3.0~10.0 秒），否则直接
#   `raise OSError("参考音频在3~10秒范围外，请更换！")`，**没有自动裁剪**。
#
# ★ 这一条被踩过两次（2026-10-18，`_probe_gsv_epochs_ab` 跑长台词时暴露）：
#   · pick_reference 原写法：`dur < 2.0` 才排除，3~10 秒只是"打分降权"；
#   · _alt_refs 原写法：`1.0 <= dur <= 14.0` 才算候选。
#   两个区间都比下游**宽**，于是 1.6 秒的段被当成合法参考选进去。后果不是"效果差"，
#   是**整条链路抛异常**：
#     长台词每段固定合成两遍（takes=2），第 1 遍可疑 → 第 2 遍换参考 → 换到
#     1.60 秒那段 → OSError → 而"换参考重试"本来是长句丢字**唯一的兜底手段**
#     （见 _alt_refs 的注释：八个旋钮全试过，只有换参考能改变结果）。
#   ⇒ 兜底等于不存在，而且异常还会把整个任务带崩（`dict(None,...)` 那条）。
#
# 教训：护栏的区间必须**等于或严于**下游，宽一点都不行；宽出来的部分不是"宽容"，
#       是把下游的硬报错请到上游来。
REF_MIN_SEC = 3.0
REF_MAX_SEC = 10.0


def ref_dur_ok(dur):
    """这段音频能不能直接当 GPT-SoVITS 的参考（3~10 秒）。拿不到时长算不合格。"""
    try:
        dv = float(dur)
    except (TypeError, ValueError):
        return False
    return REF_MIN_SEC <= dv <= REF_MAX_SEC


def pick_reference(role, slices_dir):
    """
    挑参考音频：时长 3~10 秒、响度适中，挑其中**最靠近 5 秒且响度在舒适区**的那段。

    ★★ 关于「用声纹挑最典型的段」：这个想法**实测被否决了**（2026-10-16），
       代码留着但默认关（REF_USE_VOICE=False）。过程记在这里免得有人再来一遍：

       想法看着很对 —— 时长/响度合格的段里仍可能混着带笑/带口音/带噪音的片段，
       拿它当参考整句都会偏；声纹距角色中心越近＝越典型＝越能代表这个人。

       实测（`verify_tone` 的负对照，大帝）：
                        换语气带来的音高差   同一句重复合成的抖动   信噪比
          声纹复核 开        30.9 Hz              21.3 Hz           1.4x
          声纹复核 关        55.1 Hz               1.7 Hz          32.5x
       ⇒ 挑"最典型"的那段反而让合成**明显变不稳定**：抖动从 1.7 Hz 涨到 21.3 Hz
         （12 倍），换语气能拉开的差距反而从 55.1 Hz 掉到 30.9 Hz。
         推测原因：最"平均"的那段往往也是最平淡、起伏最小的段，
         拿它当参考，模型对语气的响应就钝了。
       ⇒ **文献/直觉说了不算，实测说了算。** 默认关；想再试就把 REF_USE_VOICE 改 True。
    """
    cands = []
    for s in role.get("segments", []):
        if not (s.get("text") or "").strip():
            continue
        p = os.path.join(slices_dir, s["file"])
        if not os.path.exists(p):
            continue
        dur = s.get("dur", 0)
        # ★ 先按 3~10 秒硬过滤。这不是"偏好"，是下游的硬要求（见 REF_MIN_SEC 注释）：
        #   以前这里是 `dur < 2.0` 才排除、3~10 秒只做降权，于是 2~3 秒的段仍可能
        #   被选中当主参考 ⇒ 每一句合成都在 GPT-SoVITS 内部抛 OSError。
        if not ref_dur_ok(dur):
            continue
        r = wav_rms(p)
        if r <= 0.005:
            continue
        # 合法区间内，越接近 5 秒越稳（太短音色信息不够，太长容易带换气/静音）
        score = abs(dur - 5.0)
        if not (0.02 <= r <= 0.35):
            score += 2.0
        cands.append((score, p, s["file"], s["text"]))
    if not cands:
        # 退化前再补一次：segments 里的 dur 是切片时记的，可能和磁盘上的真实文件
        # 不一致（后续被替换/重剪过）。实量一遍再判，别因为一份旧元数据把角色判死。
        for s in role.get("segments", []):
            if not (s.get("text") or "").strip():
                continue
            p = os.path.join(slices_dir, s["file"])
            if not os.path.exists(p):
                continue
            if not ref_dur_ok(wav_seconds(p)):
                continue
            if wav_rms(p) > 0.01:
                cands.append((9.0, p, s["file"], s["text"]))
    if not cands:
        # 一段合法的都没有 = 硬约束无解。不许"凑合用"（凑合的结果是下游报一个
        # 让人一头雾水的 OSError），把真实时长分布打出来，告诉人该怎么修。
        durs = []
        for s in role.get("segments") or []:
            if not (s.get("text") or "").strip():
                continue
            dd = float(s.get("dur") or 0) or wav_seconds(os.path.join(slices_dir, s["file"]))
            if dd:
                durs.append(dd)
        durs.sort()
        raise Fail(
            "这个角色没有一段切片落在 %.0f~%.0f 秒之间，而这是 GPT-SoVITS 对参考音频的"
            "硬性要求（超了它直接报「参考音频在3~10秒范围外」，不会自动裁剪）。\n"
            "现有 %d 段切片：最短 %.2f 秒 / 中位 %.2f 秒 / 最长 %.2f 秒。\n"
            "要么重新切片让每段落在 %.0f~%.0f 秒，要么补更长的素材再切。"
            % (REF_MIN_SEC, REF_MAX_SEC, len(durs),
               durs[0] if durs else 0,
               durs[len(durs) // 2] if durs else 0,
               durs[-1] if durs else 0, REF_MIN_SEC, REF_MAX_SEC))
    cands.sort(key=lambda x: x[0])
    _, path, fname, text = cands[0]

    # ★ 声纹复核：在上面"时长/响度都合格"的候选里，再挑**最像这个人平常状态**的那段。
    #   这一段以前只写在函数头的注释里、代码并没做（实测对过：只有时长+响度打分）。
    #   为什么要这一步：参考音频直接决定合成的音色和韵律，而时长/响度合格的段里
    #   仍然可能混着"带笑 / 带口音 / 带噪音 / 情绪很偏"的那几段 —— 拿它当参考，
    #   整句都会跟着偏。声纹距角色中心越近 = 越典型 = 越能代表这个人。
    #   ★ 只对前 REF_VOICE_TOPN 个候选算：声纹要起 GSV 的子进程，全量算会明显拖慢
    #     每次"说话"，而排后面的候选本来就不该被选中。
    center = (role.get("voice") or {}).get("center")
    if REF_USE_VOICE and center and len(cands) > 1:
        try:
            top = cands[:REF_VOICE_TOPN]
            recs = _feat_subprocess([c[1] for c in top], "ref")
            dist = {}
            for r in recs:
                v = r.get(VOICE_KIND)
                if not v:
                    continue
                d = _emb_dist(v, center)
                if d is not None:
                    dist[os.path.basename(r.get("file") or "")] = d
            if dist:
                # 距离优先；距离量不出来（dist 里没有）的保持原来的座次
                top.sort(key=lambda c: (dist.get(c[2], 9.9), c[0]))
                path, fname, text = top[0][1], top[0][2], top[0][3]
        except Exception:
            # 声纹算不出来就退回"只按时长响度"的结果，绝不能让说话跑不起来
            pass
    return path, text


# ---------------------------------------------------------------- 完整流程


def _set_pending(rid):
    """把这个角色标成「待归类」（来源不明的一堆片段）。"""
    db = load_roles()
    for r in db["roles"]:
        if r["id"] == rid:
            r["pending"] = True
    save_roles(db)


def _attach_segments(rid, seg_records, sources, replace=False):
    """把一批片段挂到角色上，返回**真正新增了几段**。

    replace=False（追加模式）：老片段全部保留，同名跳过（不重复计数）；
    replace=True （首次建档）：整份替换 —— 重拆同一个视频时不能越拆越多。

    ★ 已经有人手工校对过的文本一律不覆盖：追加素材时把整 rebuilding 一遍的话，
      用户改过的错别字会被 ASR 的识别结果冲掉，而他自己也未必记得改过哪些。
    """
    names = [os.path.splitext(s)[0] for s in (sources or [])]
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import singing

    def _apply(r):
        old = {s["file"]: s for s in (r.get("segments") or [])}
        merged = [] if replace else list(r.get("segments") or [])
        added = 0
        for s in seg_records:
            key = s["file"]
            if key in old:
                # 同名 = 同一段素材又来了一遍。追加模式下跳过（不重复），
                # 但**保留老的那一条**（里面可能有人工改过的文本）。
                if replace:
                    o = old[key] or {}
                    merged.append(dict(s, text=o.get("text") or s.get("text", "")))
                    added += 1
                continue
            merged.append(s)
            added += 1
        r["segments"] = merged
        if not r.get("kind_locked"):
            kind, conf, why = singing.guess(merged)
            r["kind"] = kind
            r["kind_guess"] = {"kind": kind, "conf": conf, "why": why}
        r["source"] = "、".join(sources or [])
        # intro 是"这张卡是谁"的唯一线索，素材变多了就要跟着更新
        secs = sum(float(s.get("dur") or 0) for s in merged)
        if not (r.get("intro") or "").strip() or replace:
            src_txt = "《%s》" % "》《".join(names[:3]) + ("…" if len(names) > 3 else "")
            r["intro"] = "来自%s的一个声音 · %d 段素材 · %s" % (
                src_txt, len(merged), _humantime(secs))
        else:
            # 已有简介的（可能人工改过）：只把素材数字刷新掉，简介保留
            r["intro"] = re.sub(r"·\s*\d+\s*段素材\s*·[^·]*$",
                                "· %d 段素材 · %s" % (len(merged), _humantime(secs)),
                                r["intro"]) or r["intro"]
        r["split_info"] = {
            "segments": len(merged),
            "seconds": round(secs, 1),
        }
        r["_added"] = added

    role = mutate_role(rid, _apply)
    return (role or {}).get("_added", 0)


def job_ingest(job, video=None, n_speakers=None, uvr=True, lang="zh", epochs=None,
               do_train=True, quality="standard", videos=None, mode="auto",
               append_to=None):
    """视频 -> 角色（可一个视频拆多个，也可多个视频合成一个）。

    videos: 视频文件列表。选了多个文件时，它们通常**都是同一个人在说话**
             （比如同一个 UP 主的几期视频），用户要的自然是"合成一个模型"。
    mode:
            merge     —— 全部合成**一个**角色（不管里面有几个人在说话）
            auto      —— 把多个文件的片段倒进同一个池子里做声纹聚类，
                         同一个人的素材**跨文件自动合到一起**（这才是"多个视频
                         一个模型"的正解），不同的人仍然分开建档。
            match     —— **自动认人**：先像 auto 一样按人分堆，再把每一堆拿去和
                         库里已有角色的**声纹中心**比。认得出的直接并进那个角色
                         （追加，不新建），认不出的照 auto 新建。这样"同一个人的
                         新素材"不会散成好几个角色，别人的素材也不会被混进来。
            per_video —— 每个视频各自建一个角色（素材差别很大时用）
    append_to: 指定一个已有角色的 id —— 新素材**追加**到它身上，而不是新建角色。
            这是"后续还能加素材"的入口：追加完可以顺便重训。
    """
    vids = [v for v in list(videos or []) if v]
    if video:
        vids = [video] + vids          # 兼容以前的单文件调用方式
    if not vids:
        raise Fail("没有要处理的视频。")
    for v in vids:
        if not os.path.isfile(v):
            raise Fail("文件不存在：%s" % v)
    many = len(vids) > 1
    if append_to and not get_role(append_to):
        raise Fail("要追加到的角色不存在：%s" % append_to)
    # 追加就意味着"这些都是同一个人"，再去按人分组就自相矛盾了
    if append_to:
        mode = "merge"

    work = os.path.join(job["dir"], "work")
    os.makedirs(work, exist_ok=True)
    # ★ 多个视频的片段最终要倒进**同一个池子**里一起聚类。
    #   如果各切各的、各自聚类再合并，那同一个人在两个视频里就是两个角色 ——
    #   用户拿到的还是两个模型，等于没合并。
    all_slices = os.path.join(work, "slices_all")
    os.makedirs(all_slices, exist_ok=True)

    sources = []
    total_segs = 0
    for i, v in enumerate(vids):
        tag = " · 第 %d/%d 个《%s》" % (i + 1, len(vids),
                                        os.path.basename(v)) if many else ""
        sources.append(os.path.basename(v))
        begin_step(job, "抽取音轨" + tag, "从视频里取出声音")
        audio = step_audio(job, v, os.path.join(work, "v%d_audio.wav" % i))
        end_step(job, True, "已取出音频")

        begin_step(job, "分离人声与伴奏" + tag, "去掉背景音乐和杂音")
        vocal = step_uvr(job, audio, work, enabled=uvr)
        end_step(job, True, "人声已分离")

        begin_step(job, "切分片段" + tag, "把连续人声切成一句一句")
        slice_dir, good = step_slice(job, vocal, os.path.join(work, "v%d_slices" % i))
        end_step(job, True, "切出 %d 段，合格 %d 段"
                 % (len(os.listdir(slice_dir)), len(good)))

        # 只把合格的片段留下来参与后续，噪音片段直接丢
        keep = set(good)
        for f in os.listdir(slice_dir):
            if f not in keep:
                try:
                    os.remove(os.path.join(slice_dir, f))
                except Exception:
                    pass
        # 拷进总池：名字前面加 v<i>_ 前缀，这样多个视频里同名的片段
        # （切片脚本按时间命名，不同视频必然重名）不会互相覆盖。
        for f in sorted(keep):
            shutil.copy(os.path.join(slice_dir, f),
                        os.path.join(all_slices, "v%d_%s" % (i, f)))
        total_segs += len(keep)
        log(job, "  《%s》贡献 %d 段" % (os.path.basename(v), len(keep)))

    slice_dir = all_slices

    job["stage"] = "汇总多段素材"
    begin_step(job, "提取声纹特征", "给每段算出「这是谁的声音」")
    feats = step_feat(job, slice_dir, os.path.join(work, "feat.json"))
    end_step(job, True, "算出 %d 段特征" % len(feats))

    if mode == "merge":
        begin_step(job, "合并素材", "按你的选择，全部合成一个人")
        groups = {0: list(feats)}
        res = {"k": 1}
        end_step(job, True, "%d 段素材全部归到同一个角色" % len(feats))
    elif mode == "per_video":
        begin_step(job, "按视频分别建档", "每个视频一个角色")
        groups = {}
        for f in feats:
            v_i = str(f.get("file", "")).split("_", 1)[0]
            groups.setdefault(v_i, []).append(f)
        res = {"k": len(groups)}
        end_step(job, True, "分成 %d 个角色：%s"
                 % (len(groups), {k: len(v) for k, v in sorted(groups.items())}))
    else:
        begin_step(job, "分离不同说话人",
                   ("你指定了 %d 个人" % n_speakers) if n_speakers
                   else "自动判断有几个人在说话")
        groups, res = step_split_speakers(job, feats, n_speakers)
        end_step(job, True, "分成 %d 组：%s"
                 % (res["k"], {k: len(v) for k, v in sorted(groups.items())}))

    # ---- 认人（mode=match）：这几堆素材分别是库里哪一个角色？----
    # 认得出的直接并进去，认不出的照原样新建。只有 match 模式做这件事：
    # auto 是"这次导入里怎么分人"，match 是"这些人在我库里是谁"，两个问题不能混。
    assign = {}
    if mode == "match" and not append_to:
        begin_step(job, "认人", "看看这几堆素材分别是谁的声音")
        made_n, skip_n = ensure_role_voices(job)
        if made_n or skip_n:
            log(job, "  声纹中心：补算 %d 个，跳过 %d 个（切片不在或段太少）"
                % (made_n, skip_n))
        assign, notes = match_clusters(job, groups, feats)
        for t in notes:
            log(job, "  " + t)
        end_step(job, True, ("认出了 %d 堆（并到已有角色），其余新建"
                             % len(assign)) if assign
                 else "一堆都没认出来，全部新建角色")

    begin_step(job, "语音识别", "听出每句说了什么字")
    texts = step_asr(job, slice_dir, os.path.join(work, "04_asr"), lang)
    end_step(job, True, "识别了 %d 句" % len(texts))

    begin_step(job, "建立角色", "按说话人分别建档")
    # singing.py 只用标准库（statistics），不需要 numpy，
    # 所以可以安全地在本进程 import —— 与 diarize 不同，别一律改子进程。
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import singing

    db = load_roles()
    made = []
    names = [os.path.splitext(s)[0] for s in sources]
    for gi, lab in enumerate(sorted(groups, key=lambda x: str(x))):
        segs = groups[lab]
        # ★ lab 有两种形状：auto 模式是聚类给的**整数**标签（-1 = 没挂上的
        #   杂音），per_video 模式是我自己起的 "v0"/"v1" 这种**字符串**键。
        #   直接写 `lab < 0` 在 per_video 上会 TypeError（str 和 int 比不了）——
        #   两种形状混在同一个循环里，就得先问清楚它是哪一种。
        try:
            pending = int(lab) < 0
        except (TypeError, ValueError):
            pending = False
        if pending and not append_to:
            # 没挂上的片段单独放一个「待归类」角色，让用户自己决定
            rid = None
            for r in db["roles"]:
                if r.get("pending") and r.get("source") == sources[0]:
                    rid = r["id"]
            if rid is None:
                role = create_role(name="待归类的片段", source=sources[0])
                _set_pending(role["id"])
                db = load_roles()
                rid = role["id"]
            else:
                role = get_role(rid)
        elif append_to:
            rid = append_to
            role = get_role(rid)
        elif assign.get(lab):
            # ★ 认人认出来了：这些素材是库里已有的那个人 ⇒ 并进去（追加语义），
            #   不新建角色。这正是需求里的"把属于这个角色的素材加进去"。
            rid = assign[lab]
            role = get_role(rid)
            log(job, "  第 %s 堆并到已有角色「%s」（%s）"
                % (lab, role.get("name"), rid))
        else:
            nm = "角色 %s" % (chr(ord("A") + gi))
            role = create_role(name=nm, source="、".join(sources))
            rid = role["id"]

        # 这一轮对这个角色到底是"追加"还是"首次建档"：
        # 认人认出来的那几堆也是追加 —— 用 replace=True 会把老素材整份换掉，
        # 那就等于把用户既有的角色清空了（踩不起）。
        is_append = bool(append_to) or bool(assign.get(lab))

        d = role_dir(rid)
        sd = os.path.join(d, "slices")
        os.makedirs(sd, exist_ok=True)
        seg_records = []
        for f in segs:
            src = os.path.join(slice_dir, f["file"])
            # ★ 目标文件已存在且大小一致时**不要覆盖**：同一个视频被重复追加时
            #   重算一遍再拷回去毫无意义，反而可能把用户已经校对过的内容弄脏。
            dst = os.path.join(sd, f["file"])
            if not os.path.exists(dst):
                shutil.copy(src, dst)
            seg_records.append({
                "file": f["file"],
                "dur": f.get("dur", 0),
                "voiced": f.get("voiced", 0),
                "text": texts.get(f["file"], ""),
            })

        added = _attach_segments(rid, seg_records, sources, replace=not is_append)
        refresh_stats(rid)
        role = get_role(rid)
        log(job, "  角色 %s：累计 %d 段（本次新增 %d 段），%.1f 秒"
            % (rid, len(role.get("segments") or []), added,
               (role.get("stats") or {}).get("seconds", 0.0)))
        # 素材变了，声纹中心必须重算 —— 不重算的话下一次认人用的还是老中心，
        # 而这个角色已经多了一批素材（严重的话会把别人认成它）。
        try:
            build_role_voice(rid, job, force=True)
        except Exception as e:
            # 认人是个增强功能，算不出中心不该把整条导入链带死
            log(job, "  [warn] 声纹中心没算出来（不影响建档）：%s" % e)
        made.append(rid)

    end_step(job, True, "建好 %d 个角色：%s"
             % (len(made), "、".join((get_role(r) or {}).get("name", r)
                                     for r in made)))

    failed = []
    if do_train:
        todo = [r for r in made if not get_role(r).get("pending")]
        total = len(todo)
        for i, rid in enumerate(made):
            if get_role(rid).get("pending"):
                continue
            nm = get_role(rid)["name"]
            begin_step(job, "训练音色：%s" % nm, "第 %d/%d 个角色" % (i + 1, max(total, 1)))
            # 每个角色在 60%→97% 里切自己的一段。写死单点进度的话，
            # 第二个角色开始训练时进度条会从上一个角色的高位跳回去 —— 用户看着像倒退。
            lo = 60 + int(37 * i / max(total, 1))
            hi = 60 + int(37 * (i + 1) / max(total, 1))
            job["stage"] = "训练 %s (%d/%d)" % (nm, i + 1, total)
            job["pct"] = max(job.get("pct") or 0, lo)
            save_job(job)
            try:
                train_role(job, rid, epochs, batch=None, quality=quality,
                           pct_from=lo, pct_to=hi)
                end_step(job, True, "%s 已训好" % nm)
            except Exception as e:
                # 以前只 log 一句 [warn] 就继续 ⇒ 任务整体"成功"，
                # 用户看到的是"跑完了"但某个角色其实没训出来，**失败被吞掉了**。
                # 现在：既标红这一步，也记进 failed，最后一并如实上报。
                fail_step(job, e)
                log(job, "  [失败] 角色 %s（%s）训练失败：%s" % (nm, rid, e))
                failed.append({"role": rid, "name": nm, "err": str(e)})
    job["result_roles"] = made
    if failed:
        # 不能假装整体成功。任务标成"部分失败"，把每个失败的角色说清楚。
        job["failed_roles"] = failed
        end_step(job, False, "%d/%d 个角色训练失败：%s"
                 % (len(failed), max(total, 1),
                    "；".join("%s（%s）" % (f["name"], f["err"][:80]) for f in failed)))
        log(job, "完成，但有 %d 个角色训练失败（详见上面标红的那一步）" % len(failed))
    else:
        log(job, "完成，共建/更新 %d 个角色：%s" % (len(made), ", ".join(made)))


def _humantime(sec):
    m, s = divmod(int(round(sec)), 60)
    if m:
        return "%d 分 %d 秒" % (m, s)
    return "%d 秒" % s


def _install_one_pack(job, repo, pick, name=None, budget=8):
    """下载**一个**包（zip 或单个 .pth），把里面的权重逐个变成角色。

    返回建好的角色列表。包里某个权重加载不了就跳过（文件删干净、角色卡片撤回，
    不留半截），一个都没成才由调用方统一报错 —— 单个包失败不该把整批拖死。
    """
    import zipfile

    import modelhub

    url = modelhub.file_url(repo, pick)
    dst = os.path.join(modelhub.download_dir(repo), os.path.basename(pick))
    dst = os.path.join(modelhub.download_dir(repo), os.path.basename(pick))
    if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
        log(job, "  这个文件已经下过了，直接用（%.1f MB）" % (os.path.getsize(dst) / 1048576.0))
    else:
        begin_step(job, "下载模型", "%s　来自 %s" % (os.path.basename(pick), repo))
        rc, out = run(job, '"%s" -u "%s" "%s" "%s"'
                      % (PY, os.path.join(STAGES, "dl_cli.py"), url, dst), timeout=7200)
        if rc != 0 or not os.path.isfile(dst):
            end_step(job, False, "下载失败")
            raise Fail("下载失败（镜像有时候会超时，可以再点一次）。\n%s" % _tail(out, 4))
        end_step(job, True, "%.1f MB" % (os.path.getsize(dst) / 1048576.0))

    # ---- 解开 / 找出里面的权重 ----
    cands = []
    if pick.lower().endswith(".zip"):
        begin_step(job, "解开 %s" % os.path.basename(pick)[:28], "看看里面装了几个角色")
        ex = os.path.join(os.path.dirname(dst),
                          "x_" + re.sub(r"[^0-9A-Za-z_\-]+", "_", os.path.splitext(os.path.basename(pick))[0]))
        shutil.rmtree(ex, ignore_errors=True)
        os.makedirs(ex, exist_ok=True)
        try:
            with zipfile.ZipFile(dst) as z:
                z.extractall(ex)
        except Exception as e:
            end_step(job, False, "解不开")
            raise Fail("解不开这个压缩包：%s" % e)
        for root, _, fs in os.walk(ex):
            for f in fs:
                if f.lower().endswith(".pth"):
                    cands.append(os.path.join(root, f))
        end_step(job, True, "里面 %d 个 .pth" % len(cands))
    elif pick.lower().endswith(".pth"):
        cands = [dst]
    else:
        raise Fail("先不处理这个格式：%s（RVC 要 .pth，或装着 .pth 的 .zip）"
                   % os.path.splitext(pick)[1])
    if not cands:
        raise Fail("里面没找到 .pth（这个包里不是 RVC 权重）")
    cands.sort(key=lambda p: -os.path.getsize(p))
    if len(cands) > budget:
        log(job, "  里面有 %d 个权重，先装最大的 %d 个" % (len(cands), budget))
        cands = cands[:budget]

    wdir = _rvc_weights_dir()
    idir = os.path.join(RVC_ROOT, "assets", "indices")
    os.makedirs(wdir, exist_ok=True)
    os.makedirs(idir, exist_ok=True)

    made = []
    for pth in cands:
        raw = modelhub.clean_name(name or os.path.basename(pth))
        # ★ 角色卡上写**中文名**（本地字典，不联网不花钱）；原名塞进简介，
        #   用户随时能对着原名去核对（万一字典里那条翻错了，他一眼能看出来）。
        base = modelhub.to_cn(raw) or raw
        begin_step(job, "装上「%s」" % base, "放进 RVC 并建档")
        # ★ 先建档拿 id：权重/索引的文件名都按这个 id 来（跟自训模型同口径）。
        #   反过来（先落盘再问 id）就会多一次改名，改名一旦漏了索引，
        #   症状是"唱得出来但音色检索没生效"，非常难查。
        role = create_role(name=base, kind="singing", source="modelhub:%s" % repo)
        rid = role["id"]

        # 1) 权重
        wp = os.path.join(wdir, rid + ".pth")
        shutil.copyfile(pth, wp)
        # 2) 索引：同目录下找 .index（优先跟权重同名的那个）
        src_idx = None
        stem = os.path.splitext(os.path.basename(pth))[0].lower()
        for f in os.listdir(os.path.dirname(pth)):
            if f.lower().endswith(".index"):
                if os.path.splitext(f)[0].lower() == stem:
                    src_idx = os.path.join(os.path.dirname(pth), f)
                    break
                src_idx = src_idx or os.path.join(os.path.dirname(pth), f)
        idx_ok = False
        if src_idx:
            shutil.copyfile(src_idx, os.path.join(
                idir, "%s_added_IVF1_Flat_nprobe_1_%s_v1.index" % (rid, rid)))
            idx_ok = True
        # 3) 真的加载一次：文件在 ≠ 能加载
        rc2, out2 = run(job, '"%s" -s "%s" "%s"'
                        % (RVC_PY, os.path.join(STAGES, "rvc_ckpt_check.py"), wp),
                        cwd=RVC_ROOT, timeout=900)
        if rc2 != 0:
            end_step(job, False, "这个权重不能用")
            for p in (wp, os.path.join(idir, "%s_added_IVF1_Flat_nprobe_1_%s_v1.index" % (rid, rid))):
                with contextlib.suppress(Exception):
                    os.remove(p)
            with contextlib.suppress(Exception):
                delete_role(rid)
            log(job, "  %s：%s（跳过）" % (base, _tail(out2, 2)))
            continue
        end_step(job, True, "%s，索引%s" % (base, "已对上" if idx_ok else "没有（也能唱，检索弱一点）"))

        # ★ 默认参数绑定（_p=rid 这种写法）：闭包里的循环变量会在下一轮被改写，
        #   不绑定的话角色卡上会串成最后一个角色的信息。
        def _set(r, _p=rid, _repo=repo, _pick=pick, _base=base, _raw=raw):
            r["rvc"] = {"model": _p + ".pth", "epochs": 0, "at": _now(),
                        "index": idx_ok, "imported": True, "repo": _repo,
                        "file": os.path.basename(_pick)}
            r["intro"] = "来自声音市场的模型（%s）" % _repo
            if _base != _raw:
                # 卡片上是中文名 ⇒ 简介里必须留着原名，不然用户没法核对
                r["intro"] = "原名 %s · 来自声音市场（%s）" % (_raw, _repo)
                r["alias"] = _raw
            r["kind"] = "singing"
            r["kind_locked"] = True

        mutate_role(rid, _set)
        made.append({"id": rid, "name": base, "raw": raw, "index": idx_ok})
        log(job, "  ✔ 角色「%s」已建好，可以直接拿去唱歌" % base)

    return made


def _market_stem(r):
    """市场安装时记下来的**原始压缩包名**（Applejack_Talking_Singing_e320_s8000.zip）。

    同一个角色装了两份的时候，alias 是一样的、只有文件名不同 —— 这是唯一有据可查
    的区分依据。撞名时的后缀一律从这儿取，**绝不现编**。
    """
    f = ((r.get("rvc") or {}).get("file") or "").strip()
    if not f:
        return ""
    return re.sub(r"\.(zip|rar|7z|pth|onnx|tar(\.gz)?)$", "", f, flags=re.I).strip()


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _rest_after(stem, raw):
    """文件名里"去掉角色名之后剩下的那截"，保留原始写法（大小写/分隔符）好读。

      Applejack_Talking_Singing_e320_s8000 − Applejack → _Talking_Singing_e320_s8000
      AdagioDazzleV3 − AdagioDazzle                   → V3
      CozyGlow V2    − CozyGlow                       → " V2"
    """
    ns, nr = _norm(stem), _norm(raw)
    if not nr or not ns.startswith(nr):
        return ""
    cnt = 0
    for i, ch in enumerate(stem):
        if ch.isalnum():
            cnt += 1
            if cnt == len(nr):
                return stem[i + 1:]
    return ""


def _dup_tag(raw, stem, maxc=20):
    """撞名时的后缀。来源按"哪个信息量最大"排序，**全都是真实存在的字符串**：

      ① 文件名比角色名多出来的那截   Applejack…s8000 → "Talking Singing e320"
      ② 文件名整体（①取空但文件名≠原名）  AriaBlazeV3   → "V3"
      ③ 原名里的版本尾巴                 AdagioDazzleV2 → "V2"
      ④ 原名本身（拼错的那个，原名就是区分点）AddagioDazzle → "AddagioDazzle"

    全都取不到才返回空串，让调用方退回「第N份」。
    """
    rest = _rest_after(stem, raw)
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", rest) if t]
    if toks:
        out = ""
        for t in toks:
            cand = (out + " " + t).strip() if out else t
            if len(cand) > maxc:
                break
            out = cand
        return out
    if stem and _norm(stem) != _norm(raw):
        return stem[:maxc]
    m = re.search(r"(?i)\s*(v\d+|\d+)$", raw)
    if m:
        return m.group(1).upper()
    return raw[:maxc]


def retranslate_imported(all_roles=False):
    """给**已经装进来**的市场角色把英文名补译成中文名。返回 [{'id','from','to'}]。

    为什么需要它：字典是慢慢补的。2026-10-13 之前装的那批角色，卡片上还是一水
    英文（当时字典里没有那条名字），用户不该为了享受后来补的字典去重装一遍。

    四条规矩（每一条都得守住，不然就是偷偷改用户的东西）：
      ① 默认**只动来自声音市场**的角色（source 以 `modelhub:` 开头）—— 自己建的
         角色叫什么名字是用户说了算，不许替他改；
      ② 用户自己改过的名字不动。判断依据：现在这个名字**是不是我们译出来的**
         （等于译名、或译名+我们加的后缀）。是才允许重新取名，不是就跳过；
      ③ 原名**永远留着**：写进 alias 和简介「原名 xxx · 来自声音市场（repo）」，
         万一哪条译错了他一眼能对着原名核对；
      ④ 撞名时的后缀只从**市场原始文件名 / 原名**里取，绝不现编一个（见 _dup_tag）。

    2026-10-13 第二轮改了发名字的顺序：原来按角色表顺序先到先得，实测干净名
    「阿达吉奥·达佐」被 V2 那个包拿走了、正主反倒挂着后缀。现在先按 name_rank
    排座次（字典精确命中 > 剥掉版本尾才命中 > difflib 纠错 > 只能音译）。
    """
    # modelhub 只在需要的时候 import（它联网那部分依赖重、启动时不该拖住）
    import modelhub
    _cjk = re.compile(r"[\u4e00-\u9fff]")
    db = load_roles()

    # ---- 第一步：挑候选 ----------------------------------------------------
    cands = []
    for r in db["roles"]:
        src = r.get("source") or ""
        market = src.startswith("modelhub:")
        if not (market or all_roles):
            continue
        raw = (r.get("alias") or (r.get("name") or "")).strip()
        if not raw:
            continue
        cn = modelhub.to_cn(raw)
        if not cn:
            continue
        cur = (r.get("name") or "").strip()
        # 名字里已经有中文 ⇒ 只在我们"这名字本来就是自动译出来的"时才重新取名。
        # 用户自己改过的名字（哪怕改成了别的中文）一律不动 —— 那是他说的算的。
        if cur and _cjk.search(cur) and not (cur == cn or cur.startswith(cn + " (")
                                             or cur.startswith(cn + "·")):
            continue
        cands.append((modelhub.name_rank(raw), r.get("created") or "", r, raw, cn, cur))

    # ---- 第二步：排座次，谁更正谁拿干净名 ----------------------------------
    # 不排座次就是按角色表顺序先到先得 —— 实测把干净名「阿达吉奥·达佐」发给了
    # V2 那个包，正主反倒被挂上后缀。现在字典精确命中的排最前，只能音译的排最后。
    cands.sort(key=lambda t: (t[0], t[1]))

    # 已经用掉的名字（同一个仓库常常同时有 AriaBlaze / AriaBlazeV2 / AriaBlazeV3，
    # 都翻成「阿里亚·布雷兹」的话三张卡长得一样，用户没法挑）
    used = set((x.get("name") or "").strip() for x in db["roles"])
    for _, _, r, raw, cn, cur in cands:
        used.discard(cur)                      # 先把候选自己占的旧名腾出来，免得跟自己撞

    done, nth = [], {}
    for _, _, r, raw, cn, cur in cands:
        name = cn
        if name in used:
            tag = _dup_tag(raw, _market_stem(r))
            if not tag:
                n = nth.get(cn, 1) + 1
                nth[cn] = n
                tag = "第%d份" % n
            name = "%s (%s)" % (cn, tag)
            while name in used:
                name = name + "·"
        used.add(name)
        if name == cur:
            continue
        src = r.get("source") or ""
        repo = src.split(":", 1)[1] if src.startswith("modelhub:") else ""
        r["name"] = name
        r["alias"] = raw
        r["name_auto"] = True        # 记一笔"这名字是我们译的"，以后用户手改就作废
        r["intro"] = ("原名 %s · 来自声音市场（%s）" % (raw, repo)
                      if repo else "原名 %s" % raw)
        done.append({"id": r["id"], "from": cur, "to": name})
    if done:
        save_roles(db)
    return done


def job_install_model(job, repo, fname=None, name=None, max_roles=8, all_packs=False):
    """从**声音市场**装别人训练好的模型，装完直接变成能唱的角色。

    为什么要把"下载 → 解压 → 放进 RVC 目录 → 对上索引 → 建角色卡片"一口气做完：
    这几步少做一步，症状都是"模型加载失败"或者"能选但唱出来是哑的"，而用户
    根本无从判断是**模型本身有问题**还是**我少放了一个文件**。约定死在这里：
      · 权重必须落进 RVC 的 assets/weights，且命名 = 角色 id（跟自训模型同口径）；
      · 索引必须落进 assets/indices，且命名必须能被 `_find_rvc_index()` 认出来
        （`<rid>_added_..._<rid>_v1.index`）—— 不这么取名，index_rate 会被白降级成 0；
      · 装之前先用 `rvc_ckpt_check.py` 真的 torch.load 一次：文件在 ≠ 能加载。

    ★★ all_packs：一个仓库里往往躺着**一整个作品的角色**（小马宝莉那几个合集
    仓库实测有 14~16 个 zip）。只装第一个文件的话，用户点一次「安装」拿到的是
    文件清单最靠前那个（常常是边角角色），而他要的是全角色。所以默认整批装，
    每个包一步进度；某个包坏了跳过它继续下一个，全部失败才报错。
    """
    import modelhub

    job["stage"] = "读仓库"
    info = modelhub.detail(repo)
    if info.get("error"):
        raise Fail("读不到这个仓库：%s" % info["error"])
    files = info.get("files") or []
    if not files:
        raise Fail("这个仓库里一个文件都没有")
    packs = [p["file"] for p in (info.get("packs") or [])]
    if not packs:
        raise Fail("这个仓库里没有权重文件（要 .pth，或装着 .pth 的 .zip）")
    if fname and fname in files:
        picks = [fname]                     # 指定了就只装这一个
    elif all_packs:
        picks = packs
    else:
        picks = packs[:1]
    log(job, "这个仓库里有 %d 个可装的包，本次装 %d 个%s"
        % (len(packs), len(picks),
           "" if len(picks) == 1 else "（只想装某一个的话，展开卡片上的包列表，点那一个「装这个」）"))

    job["stage"] = "下载模型"
    made = []
    for i, pick in enumerate(picks):
        if len(made) >= max_roles:
            log(job, "  已经装好 %d 个，到这次的上限了（还想装可以再点一次）" % len(made))
            break
        if len(picks) > 1:
            log(job, "【第 %d/%d 个包】%s" % (i + 1, len(picks), os.path.basename(pick)))
        try:
            made += _install_one_pack(job, repo, pick, name=name,
                                      budget=max_roles - len(made))
        except Fail as e:
            # 单个包没装成（下不动 / 里面没有 .pth）不该把整批拖死
            log(job, "  ✘ 这个包没装成：%s（跳过，继续下一个）" % str(e).splitlines()[0])
        except Exception as e:
            log(job, "  ✘ 这个包出问题了：%s: %s（跳过）" % (type(e).__name__, str(e)[:120]))

    if not made:
        raise Fail("一个都没装成 —— 上面每一步都写了原因，最常见的是这些权重 RVC 加载不了。")
    job["stage"] = "完成"
    log(job, "共装好 %d 个角色：%s" % (len(made), "、".join(m["name"] for m in made)))
    # ★ 顺手把库里**还挂着英文名的老角色**也补译成中文：字典是后来一点点补的，
    #   用户不该为了享受新字典去重装一遍，也不该看着一半中文一半英文。
    #   （只动 source 是 modelhub: 的那些 —— 自己建的角色不许替他改名。）
    fixed = retranslate_imported()
    if fixed:
        log(job, "顺手把 %d 个还写着英文名的老角色译成了中文：%s"
            % (len(fixed), "、".join("%s→%s" % (f["from"], f["to"]) for f in fixed)))
    return {"installed": made, "repo": repo,
            "file": (picks[0] if len(picks) == 1 else ""), "count": len(made)}


def train_role(job, rid, epochs=None, batch=None, version="v5turbo", quality="standard",
               pct_from=0, pct_to=88):
    """训练（或继续训练）一个角色。会先按当前标注重写 list。

    quality 档位 -> 训练轮数：fast=15 / standard=30 / perfect=60
    （GPT 侧官方配置 s1longer-v2.yaml 的默认是 20，这三档是对照官方值定的）。
    轮数越多音色越像，但过头会过拟合（合成开始复读/漏字）——所以训练完会
    逐个体检 ckpt（拿真实判据合成一句话），不行就自动降回更早的权重。

    这里训的是 **GPT 侧**（stages/train_cli.py → s1_train.py），产物在
    GPT_weights_v5turbo/；v5turbo 不训 SoVITS 侧。详见下面「轮数体检」的注释。

    pct_from/pct_to: 本角色训练在整条任务进度条里占的区间。一次导入会连着
    训多个角色，不给区间的话第二个角色会继承前一个的百分比。
    """
    if not epochs:
        epochs = QUALITY_PRESETS.get(quality, 30)
    # ---- 轮数体检：先分清"训的是哪一侧" ----
    # 本项目的训练路径是 stages/train_cli.py → GPT_SoVITS/s1_train.py，也就是
    # **GPT 侧（文本→语义）**；v5turbo 不训 SoVITS。产物落在 GPT_weights_v5turbo/。
    #
    # 两个轮数完全不是一个量级，别混：
    #   · SoVITS 侧（s2_train 的 sovits_epoch）：官方默认 **8**（v1/v2/v2Pro/v2ProPlus），
    #     v3/v4 是 2 —— 这一侧本项目根本不训；
    #   · GPT 侧（s1_train 的 epochs，**我们实际传的就是这个**）：官方配置
    #     GPT_SoVITS/configs/s1longer-v2.yaml 实测默认 **20**
    #     （同目录 s1.yaml / s1big*.yaml 是 300，s1mq.yaml 是 100）。
    # 早先这里把 8 当成"我们该用的轮数"，并推出"大帝 60 轮 = 官方 7.5 倍"，是错的：
    # 60 轮对 20 轮是 3.0 倍；150 轮（阿塔尼斯现役）才是 7.5 倍。
    #
    # 过拟合的症状（复读 / 漏字 / 长句乱掉）本身成立，所以提示保留，但基准对齐 20。
    # ⚠ 这里**只提示不动默认值**：改默认会影响所有角色，必须先有同素材 A/B 数据
    #   （`_probe_gsv_epochs_ab.py`）。拿到数据再改，不许凭文献直接改。
    if int(epochs) >= 20:
        log(job, "  [提示] 这次训 %d 轮。我们训的是 **GPT 侧**，官方配置 "
                 "s1longer-v2.yaml 的默认轮数是 **20**"
                 "（常被引用的 8 轮是 SoVITS 侧的 sovits_epoch，v5turbo 不训它）。"
                 "轮数明显超出的常见症状是**复读 / 漏字 / 长句乱掉**。"
                 "训完会逐个体检 ckpt 并自动降档；听感发飘就用 20 轮重训一版对比。"
                 % int(epochs))
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在: %s" % rid)
    d = role_dir(rid)
    list_path, n = write_list(rid)
    if n < 3:
        raise Fail("可用标注只有 %d 条（至少要 3 条）。先到角色页补几句文本，或重新上传素材。" % n)

    step_prep(job, rid, list_path, os.path.join(d, "slices"))
    ckpt, ep = step_train(job, rid, epochs, batch, pct_from=pct_from, pct_to=pct_to)

    # ---- 训练后体检：合成一句话过判据，不过就降档到更早的 ckpt ----
    # 体检阶段接上区间尾部。体检要真跑推理合成，每句 30 秒上下，
    # 不给反馈的话这里又是一段"看不出动静"的等待。
    job["stage"] = "出声体检"
    job["detail"] = "训练完成，正在验证音色"
    job["pct"] = max(job.get("pct") or 0, pct_to)
    save_job(job)
    try:
        ref_file, ref_text = _ensure_ref(role, rid, d, job)
        ref_wav = os.path.join(d, "slices", ref_file)
        cks = sorted([f for f in os.listdir(GPT_WEIGHT_DIR)
                      if f.startswith(rid + "-e") and f.endswith(".ckpt")],
                     key=lambda f: _ckpt_ep(f), reverse=True)
        chosen, vinfo = ckpt, None
        cand_list = cks[:4]
        vspan = max(pct_to - 88, 1) if pct_to <= 88 else 11
        vbase = 88 if pct_to <= 88 else pct_to
        for ci, f in enumerate(cand_list):   # 最多体检 4 个，避免无限拖
            cand = os.path.join("GPT_weights_v5turbo", f)
            log(job, "  体检 %s ..." % f)
            job["pct"] = vbase + int(vspan * ci / max(len(cand_list), 1))
            job["detail"] = "验证第 %d 个权重：%s" % (ci + 1, f)
            save_job(job)
            ok, info = verify_ckpt(job, rid, cand, ref_wav, ref_text)
            if ok:
                chosen, vinfo, ep = cand, info, _ckpt_ep(f)
                log(job, "  体检通过（语速 %.2f 秒/字，相似度 %s）"
                    % (info.get("sec_per_char") or -1, info.get("similarity")))
                break
            log(job, "  体检没过：%s，试更早的权重" % info.get("error"))
        if vinfo is None:
            log(job, "  [warn] 所有 ckpt 体检都没过，先用最高轮数的，建议减少轮数重训")
        else:
            # 体检通过的这段音频直接留作试听样本 —— 训练完立刻能听，
            # 不用再手动去「说话」页敲一句才知道像不像
            sample = vinfo.get("out")

            def _sample(r, _p=sample, _ep=ep):
                outs = r.setdefault("outputs", [])
                outs[:] = [o for o in outs
                           if not (o.get("kind") == "speech" and o.get("text") == VERIFY_TEXT
                                   and o.get("epoch") == _ep)]
                if _p and os.path.exists(_p):
                    outs.append({
                        "text": VERIFY_TEXT, "wav": _p, "at": _now(), "kind": "speech",
                        "track": "体检样本 · %d 轮" % _ep,
                        "seconds": vinfo.get("seconds"),
                        "similarity": vinfo.get("similarity"),
                    })

            mutate_role(rid, _sample)
        ckpt = chosen
    except Fail as e:
        log(job, "  [warn] 体检环节出错（%s），直接用最高轮数权重" % e)

    def _apply(r):
        r["model"] = {"ckpt": ckpt.replace("\\", "/"), "epochs": ep, "at": _now(),
                      "quality": quality, "verified": bool(vinfo)}
        if not r.get("ref"):
            try:
                p, t = pick_reference(r, os.path.join(d, "slices"))
                r["ref"] = {"file": os.path.basename(p), "text": t}
            except Fail as e:
                log(job, "  [warn] %s" % e)

    role = mutate_role(rid, _apply)
    log(job, "  角色 %s 训练完成：%s（%d 轮）" % (rid, ckpt, ep))
    # 训练完顺手把这一轮的中间存档收一收（只留最近几份）。
    # 不做的话：每轮 155MB × 每训一次十几份，训三四次就把盘吃掉好几个 G，
    # 而用户没有任何入口去删 —— 这就是「存储清理」面板存在的理由。
    try:
        r = prune_gsv_weights(rid)
        if r.get("freed"):
            log(job, "  已顺手清掉 %d 份旧存档，释放 %s"
                % (len(r.get("deleted") or []), r.get("freed_s")))
    except Exception as e:
        log(job, "  [warn] 清理旧存档时出错（不影响本次训练）：%s" % e)
    return role["model"]


# 体检用的固定句子：字数固定，语速判据（秒/字）才有意义
VERIFY_TEXT = "今天天气不错，我们一起去公园散散步，顺便聊聊最近发生的事情。"
QUALITY_PRESETS = {"fast": 15, "standard": 30, "perfect": 60}
RVC_QUALITY_PRESETS = {"fast": 50, "standard": 150, "perfect": 300}

# 训练轮数上限：GPT-SoVITS 说话模型和 RVC 唱歌模型的上限不一样。
# GPT-SoVITS 过头会过拟合（合成发疯），60 轮已接近可用极限，卡到 200 只是兜底；
# RVC 轮数少会沙哑、**越多越稳**，官方默认就是 20000 轮，所以这里给 5000。
MAX_TRAIN_EPOCHS = {"gsv": 200, "rvc": 5000}

# ---------------------------------------------------------------------------
# RVC 架构（v1 / v2）与采样率（2026-10-16 新增 v2 支持）
#
# 官方 FAQ 给的 v1/v2 对比：
#     v1 —— 256 维特征（9 层 HuBERT）、1 个判别器、采样率 32k/40k/48k、训练更快
#     v2 —— **768 维特征（12 层 HuBERT）、4 个判别器、采样率 32k/48k、质量更好**
#   原文："v2 generally produces better results but requires more GPU memory
#          during training."
#
# ★ 两条硬约束（都不是我们选的，是 RVC 自己的）：
#   ① **v2 没有 40k** —— configs/v2/ 下面只有 32k.json 和 48k.json。
#      所以 v2 必须配 48k（或 32k），不能沿用我们一直在用的 40k。
#   ② **换架构/采样率必须从头重训**。而且 logs/<exp>/config.json 会被新架构覆盖，
#      旧断点是旧架构、读进来必然 size mismatch。⇒ v2 走**独立的 exp 名**
#      （<rid>_v2），和 v1 的断点、索引、权重彻底分开，互不干扰。
#
# ★ 默认仍是 v1/40k：**不改动现有行为**，大帝现有的 300 轮模型不受影响。
#   v2 是一项"下一回重训时可以选的更好架构"，不是偷偷把默认值换掉。
# ---------------------------------------------------------------------------
RVC_ARCH_DEFAULT = "v1"
RVC_ARCH_SR = {"v1": "40k", "v2": "48k"}          # 每版配套的采样率
RVC_ARCH_SRS = {"v1": ("32k", "40k", "48k"),       # 每版支持的采样率（硬约束）
                "v2": ("32k", "48k")}
RVC_ARCH_EXP_SUFFIX = {"v1": "", "v2": "_v2"}      # v2 用独立 exp，见约束 ②
# 批大小：v1/40k 在本机 8GB 卡上 bs=4 一直很稳；v2 官方说更吃显存 ⇒ 往下让。
# ★★ **这个数是真跑出来的，不是猜的**（2026-10-16，`_probe_v2_train.py`）：
#    用大帝 353 段素材真跑 v2/48k、bs=2、2 轮，**全流程通过**：
#      · 底模 `f0G48k.pth` / `f0D48k.pth` 加载报 `<All keys matched successfully>`
#      · 提特征落到 `3_feature768`（v2 的 768 维，索引训练确认 `特征形状：(66300, 768)`）
#      · 训练 2 轮正常保存，索引建成 `..._ra500fb92_v2_v2.index`
#      · **显存只用了 3039 MiB，还剩 4853 MiB**
#    ⇒ bs=2 在 8GB 卡上绰绰有余。想更快可以往 3 / 4 调，但那两档**没实测过**，
#      爆了就把这里改回 2（改这一个常量就行，不用动训练命令）。
RVC_ARCH_BS = {"v1": 4, "v2": 2}
# 采样率对显存的额外要求（2026-10-18 实测新增）。
# v2 的 bs 是照 **32k** 定的。48k 的生成器上采样是 12,10,2,2（32k 是 10,8,2,2），
# 判别器也要在 48k 上工作，同样 bs 下显存明显更高。
# 实测症状（这台 8GB 卡、v2/48k、bs=2、**显存已被用户程序占走 4.2GB 时**）：
# 开训后 GPU 利用率 99%、显存 7755/8151 MiB，
# 但 **tfevents 十分钟一个字没写、轮次一行都没出** —— 是显存打满后的空转，
# 不是"跑得慢"。这种卡死比报 OOM 更坏：没有报错，只能干等。
# 所以 48k 默认降到 1，宁可慢一点也不要卡住。
# ⚠ 这是**显存压力**护栏，不是 48k 的固有属性：同一台机器上显存只剩 1.8GB 被占时，
#   32k 的 bs=2 跑得很顺（95 秒/轮）。48k 想用 bs=2 就先腾显存，再把这里改开。
# ⚠ 这是**显存压力**护栏，不是 48k 的固有属性：同一台机器上显存只剩 1.8GB 被占时，
#   48k 的 bs=2 只要 ~90 秒/轮（实测 15 轮 22 分钟跑完）；32k 同样很顺。
#   所以真正该看的是「还剩多少显存」，不是「采样率多少」—— 下面这个阈值配合
#   `_vram_free_mb()` 做自适应，显存够就放回默认批大小（否则白慢 5 倍）。
RVC_SR_BS_CAP = {"48k": 1}
# 48k + 默认批大小（v2 是 2）大约要这么多空闲显存才安全。
# 实测：被占 4.2GB（只剩 ~3.9GB）时假死；被占 1.8GB（剩 ~6.3GB）时全程 5951 MiB 跑完。
# 取 4800 留约 1GB 余量（判别器那部分峰值比稳态高）。
RVC_48K_BS2_FREE_MB = 4800

# ---------------------------------------------------------------------------
# 自定义底模（2026-10-18 新增）——「唱歌专用底模」这条路
#
# 为什么：RVC 官方底模（assets/pretrained*/*）是在**语音/朗读**语料上训的。
# 社区一致反映：拿它训出来的模型去唱歌会"像念歌词"，因为底模本身没见过
# sustained tone（持续音）/ 颤音 / 滑音。底模决定的是**初始化先验** ——
# 先验里没有"唱歌"这件事，后面拿多少台词素材都补不回来。
# 这正是本项目 2026-10-18 实测确认的瓶颈：全库零唱歌素材，模型只能用台词训。
#
# KLM-X-10（SeoulStreamingStation / 韩国 AI Hub 社区）：语料是多语种剧本 +
# 男女歌手**歌唱数据**，模型卡标签明确写 `singing-voice-conversion`。
#   · **只有 32k 版**（作者说 40k/48k 在追加训练中）⇒ 必须配 v2/32k
#     （v2 支持 32k；用 v2 是因为 768 维特征、官方说质量更好）
#   · 语言标签 ko/jp/en —— **没有中文**，这是风险点，必须用本库素材实测
#   · 许可证 cc-by-nc-2.0（非商用）
#
# 实测状态：2026-10-18 已下载，并用 `rvc_pretrain_check.py` 在 v2/32k 下
# 验过 `missing=0 / unexpected=0` —— 真能加载，不是"文件在就算数"。
# ---------------------------------------------------------------------------
RVC_CUSTOM_PRETRAINS = {
    "klm": {
        "sub": "pretrained_custom",
        "g": "KLMX10_f0G32k.pth",
        "d": "KLMX10_f0D32k.pth",
        "arch": "v2",
        "sr": "32k",
        "label": "KLM-X-10 唱歌底模",
    },
    # 同一系列（SeoulStreamingStation 的 KLM 歌唱语料）的 48k 版，见 KLM49_HFG 仓库。
    # 为什么值得多一个 48k：32k 的天花板是 16kHz —— 齿音/气声那一层**生成不出来**，
    # 而"AI 感重 / 咬字不清"的抱怨正好落在这一层。官方 lj1995 的 f0G48k 是**说话**
    # 底模，所以"48k + 歌唱语料"这个组合只有 KLM 家族有。
    # ⚠ 这个仓库**没有 model card、没有语言列表、没有许可证**（KLM-X-10 至少写了
    #   cc-by-nc-2.0 + ko/jp/en）。所以它只能算**待验证候选**，采用与否看实测。
    "klm48": {
        "sub": "pretrained_custom",
        "g": "G_KLM_HFG_48k.pth",
        "d": "D_KLM_HFG_48k.pth",
        "arch": "v2",
        "sr": "48k",
        "label": "KLM-HFG 唱歌底模（48k）",
    },
}

# ---------------------------------------------------------------------------
# 开训前的素材体检（2026-10-16 新增）
#
# 为什么要有这一步：多篇 RVC 训练实践（含官方仓库 FAQ 的 "How do I improve
# training quality?"）在数据质量上给的标准高度一致 —— 而且一致认为
# **数据质量 > 数据数量 > 训练轮数**。以前我们开训前只看"够不够 10 段"，
# 响度一不一致、底噪干不干净、采样率够不够，全都没量过。
#
# 阈值来源（每一条都写清是文献给的还是本机实测的）：
#   采样率  44.1k/48k 且全库统一    —— 文献
#   单段    3~15 秒（5~10 最佳）     —— 文献
#   总时长  10~50 分钟               —— 文献
#   底噪    <-60 优秀 / <-50 合格 / >-40 不合格  —— 文献
#   响度一致 ±3 dB 优秀 / ±6 dB 合格 —— 文献
#
# ★ 本机实测校准（`_probe_traindata.py`，大帝 353 段，2026-10-16）：
#     32k 统一 / 中位 4.87s / 29.5 分钟 / 底噪中位 **-91.2 dB** / 响度跨度 **3.0 dB**
#     ⇒ 除了采样率，大帝的素材全部压在"优秀"线上。
#   ⚠ 所以 TRAIN_SR_MIN **不能**按文献写 44100 —— 本机切片用的是 GPT-SoVITS 自带的
#     `tools/slice_audio.py`，它写死 `load_audio(inp_path, 32000)` 再按 32000 写出
#     （那是给说话模型用的），**现有角色的切片因此全是 32k**。按 44.1k 判会满屏报警
#     而用户一点办法都没有（切片已经切完了）。⇒ 采样率只报"信息"级，并说清
#     "高频上限被锁在 16 kHz、下次导入新素材时会保住原采样率"。
# ---------------------------------------------------------------------------
TRAIN_SEG_MIN = 3.0            # 单段最短（短于它信息量不足）
TRAIN_SEG_MAX = 15.0           # 单段最长
TRAIN_TOTAL_MIN = 600.0        # 总时长下限 10 分钟
TRAIN_TOTAL_MAX = 3000.0       # 总时长上限 50 分钟（再多边际收益很小）
TRAIN_FLOOR_BAD = -40.0        # 底噪高于此 ⇒ 这一段不合格
TRAIN_FLOOR_WARN = -50.0       # 介于 warn~bad ⇒ 合格但不够好
TRAIN_LOUD_SPAN_GOOD = 3.0     # 响度 p10~p90 跨度；超过 GOOD 提醒、超过 WARN 警告
TRAIN_LOUD_SPAN_WARN = 6.0
TRAIN_SR_MIN = 32000           # 见上：按本机切片工具的实际情况定，不按文献的 44.1k
TRAIN_AUDIT_MAX = 150          # 体检最多抽这么多段（够统计了，别把开训拖慢）


def clamp_epochs(n, kind="gsv"):
    """把用户填的自定义轮数收进合理范围。返回 (轮数, 提示或 None)。"""
    hi = MAX_TRAIN_EPOCHS.get(kind, 200)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0, None
    if n > hi:
        return hi, "你要的 %d 轮超过上限，已按 %d 轮开始训。" % (n, hi)
    return n, None


_RVC_STATE_CACHE = {}      # exp -> (G文件mtime, 轮数)


def _rvc_state_stamp(exp):
    """用来判断缓存是否过期：logs/<exp>/G_*.pth 的 mtime。"""
    d = os.path.join(RVC_ROOT, "logs", exp)
    try:
        gs = [os.path.join(d, f) for f in os.listdir(d)
              if f.startswith("G_") and f.endswith(".pth")]
    except OSError:
        return None
    if not gs:
        return None
    return max(os.path.getmtime(p) for p in gs)


def rvc_trained_epochs(exp, use_cache=True):
    """这个 RVC 实验之前训到第几轮（从没训过返回 0）。

    走 stages/rvc_state.py —— 读 logs/<exp>/G_*.pth 里的 epoch 字段（该字段
    名叫 iteration 但存的是 epoch，见该脚本注释）。那必须用 RVC 自己的 python
    打开（torch 是整合包里的），且 cwd 必须是 RVC 根目录，所以放在子进程里跑，
    不能在这个进程里 import torch。读不出来就当 0（没训过），宁可多跑一轮，
    也不要因为探测失败拦住用户。

    起步一次约 3 秒（大头是 torch 的 import），而界面每打开一次角色详情都会问，
    所以按 G 文件 mtime 缓存：文件没动就直接回缓存值。
    """
    stamp = _rvc_state_stamp(exp)
    if stamp is None:
        return 0
    hit = _RVC_STATE_CACHE.get(exp)
    if use_cache and hit and hit[0] == stamp:
        return hit[1]
    try:
        out = subprocess.run(
            '"%s" -s "%s" %s' % (RVC_PY, os.path.join(STAGES, "rvc_state.py"), exp),
            cwd=RVC_ROOT, capture_output=True, timeout=180)
        tail = (out.stdout or b"").decode("utf-8", "ignore").strip().split("\n")[-1]
        n = int(tail.strip())
    except Exception:
        return (hit[1] if hit else 0)
    _RVC_STATE_CACHE[exp] = (stamp, n)
    return n


# 兼容旧名字：老代码/脚本按 _rvc_trained_epochs 调用
_rvc_trained_epochs = rvc_trained_epochs


def _rvc_weight_epoch(f):
    """从 `<exp>_e150_s1200.pth` 里读出轮数；裸 `<exp>.pth`（最终权重）返回 None。"""
    m = re.search(r"_e(\d+)_s\d+\.pth$", f)
    return int(m.group(1)) if m else None


def pick_rvc_weight(names, base_dir=None):
    """在候选权重里挑「训练得最充分」的那一份。

    规则：带轮数后缀的按轮数数值取最大；裸 `<exp>.pth` 是 train.py 训完时
    通过 savee() 另存的最终权重，永远优先于任何中间快照。

    names 里的元素可以是文件名，也可以是绝对路径（两处调用方给的不一样）。
    base_dir 给的是「文件名相对谁」——裸文件名用 os.path.getmtime 会拿 CWD
    去解析（训练时 CWD 是 RVC 根，不是 weights 目录），结果全变 0，
    于是"取最新的"退化成"取第一个"。所以调用方要给 base_dir。
    多个裸权重时按**文件 mtime** 取最新 —— 注释一直这么写，代码却是 max()
    按字典序，名不副实；字典序在这里本来就是个坑（`e150` < `e90`）。
    """
    def _base(f):
        return os.path.basename(f)

    def _mtime(f):
        p = f
        if not os.path.isabs(p) and base_dir:
            p = os.path.join(base_dir, p)
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    named = [f for f in names if _rvc_weight_epoch(_base(f)) is not None]
    bare = [f for f in names if _rvc_weight_epoch(_base(f)) is None]
    if bare:
        # 多个裸权重时取最后修改的（最新的那次训练）
        return max(bare, key=_mtime)
    if named:
        return max(named, key=lambda f: _rvc_weight_epoch(_base(f)))
    # 走到这里说明候选是空的（每个文件名都必然能落进上面两支之一）。
    # 旧代码是 `sorted(names)[-1]`，空列表会直接 IndexError。
    return max(names, key=_mtime) if names else None


# ------------------------------------------------ RVC 底模（预训练模型）
#
# ★ 这是「唱出来的歌没法听」的**头号原因**，而且它坏得**一点声音都没有**：
#   RVC 的训练脚本 -pg / -pd（pretrainG / pretrainD）默认是空字符串，
#   不传就从随机初始化开始训。从零训出来的模型**根本学不会跟着旋律唱**
#   —— 它只会模仿训练素材的平均音色，于是：
#       · 输出死死卡在角色自己的音区（实测原唱 268Hz、角色输出 106Hz）
#       · --pitch 从 -12 到 +12 扫一遍，输出音高**纹丝不动**
#       · 听起来就是一坨糊，谁都只会说"难听"，没人知道是模型没底子
#   本机实测：logs/r8c323d43/train.log 里 'pretrainG': ''，66 条素材、300 轮，
#   整条链从分离到混音全对，成品照样没法听 —— 因为模型本身是废的。
#
#   官方 WebUI 的取名规则（webui.py 的 get_pretrained_models）：
#       assets/pretrained{f0}...  —— v1 用 assets/pretrained/，v2 用 assets/pretrained_v2/
#       文件名 {f0}G{sr}.pth（生成器） / {f0}D{sr}.pth（判别器）
#   v2 的 40k 底模也能喂 v1 吗？不能 —— config 的 hidden_channels 不一样（192 vs 768），
#   加载会直接报尺寸不匹配。所以版本必须对上。

RVC_PRETRAIN_HOSTS = (
    # hf-mirror 在国内能直连；hf 官方本机实测连不上（HTTP 000），放第二候选。
    "https://hf-mirror.com/lj1995/VoiceConversionWebUI/resolve/main",
    "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main",
)


def rvc_pretrained_paths(version="v1", sr="40k", custom=None):
    """这一版（version + 采样率）对应的底模应该在哪。

    custom 传 `RVC_CUSTOM_PRETRAINS` 的键（如 "klm"）时改用自定义底模。
    """
    c = RVC_CUSTOM_PRETRAINS.get(custom) if custom else None
    if c:
        d = os.path.join(RVC_ROOT, "assets", c["sub"])
        return (os.path.join(d, c["g"]), os.path.join(d, c["d"]))
    sub = "pretrained_v2" if str(version).lower() == "v2" else "pretrained"
    d = os.path.join(RVC_ROOT, "assets", sub)
    return (os.path.join(d, "f0G%s.pth" % sr), os.path.join(d, "f0D%s.pth" % sr))


def rvc_pretrained_here(version="v1", sr="40k", custom=None):
    """本机有没有这一版的底模。返回 (G, D)，缺哪个哪个是 None。"""
    gp, dp = rvc_pretrained_paths(version, sr, custom)
    ok = lambda p: os.path.isfile(p) and os.path.getsize(p) > 10 * 1024 * 1024
    return (gp if ok(gp) else None, dp if ok(dp) else None)


def check_rvc_pretrained(job, version="v1", sr="40k", custom=None):
    """开训前体检一次底模：真的能加载进要训的那个架构吗。

    为什么非验不可：底模"文件在"不等于"用得上"。版本/采样率选错时文件照样在位，
    但键名和形状对不上 —— 训练跑得欢，底模却等于没用，最后又训出一个不会跟调
    的模型，而日志里只有一屏 size mismatch，没人看得懂。
    返回 (是否通过, 说明)。
    """
    gp, dp = rvc_pretrained_paths(version, sr, custom)
    rc, out = run(job, '"%s" -s "%s" %s %s "%s" "%s"'
                  % (RVC_PY, os.path.join(STAGES, "rvc_pretrain_check.py"),
                     version, sr, gp, dp),
                  cwd=RVC_ROOT, timeout=900)
    tail = [ln for ln in (out or "").splitlines() if ln.strip().startswith("PRETRAIN_")]
    msg = tail[-1] if tail else ("体检脚本没输出（退出码 %d）" % rc)
    return rc == 0, msg


def ensure_rvc_pretrained(job, version="v1", sr="40k", custom=None):
    """保证底模在位，不在就下载。返回 (G, D)；实在拿不到返回 (None, None)。

    拿不到时**不抛异常** —— 由调用方决定是硬失败还是继续（比如离线场景），
    但调用方必须把"没有底模"这件事说给用户听。

    ★ 自定义底模**不自动下载**：它们的下载地址不在 RVC_PRETRAIN_HOSTS 里
      （那是官方仓库的固定目录结构），硬塞进去只会得到 404 + 一屏重试日志。
      缺了就立刻返回 None，让调用方明说"这个底模要先放好"。
    """
    gp, dp = rvc_pretrained_here(version, sr, custom)
    if gp and dp:
        return gp, dp
    if custom:
        if job:
            # ★ 不能用 rvc_pretrained_here 的返回值：缺件时它是 (None, None)，
            #   os.path.basename(None) 会直接 TypeError 崩掉（门禁 G 段抓到的）。
            #   要用 paths（它永远给出路径字符串，不管文件在不在）。
            _gp, _dp = rvc_pretrained_paths(version, sr, custom)
            log(job, "  [warn] 自定义底模 %s 不在位（%s / %s）—— 需要手工放好，"
                     "不会自动下载" % (custom, os.path.basename(_gp),
                                       os.path.basename(_dp)))
        return (None, None)
    sub = "pretrained_v2" if str(version).lower() == "v2" else "pretrained"
    os.makedirs(os.path.dirname(rvc_pretrained_paths(version, sr)[0]), exist_ok=True)
    if job:
        log(job, "  本机没有 RVC 底模，正在下载（约 150MB，一次就好）…")
    for name, dst in (("f0G%s.pth" % sr, gp), ("f0D%s.pth" % sr, dp)):
        if dst:                       # 已有一个就别重复下
            continue
        dst = rvc_pretrained_paths(version, sr)[0 if name.startswith("f0G") else 1]
        got = False
        for host in RVC_PRETRAIN_HOSTS:
            url = "%s/%s/%s" % (host, sub, name)
            if job:
                log(job, "  尝试 %s" % url)
            rc, out = run(job, '"%s" -u "%s" "%s" "%s"'
                          % (PY, os.path.join(STAGES, "dl_cli.py"), url, dst),
                          timeout=3600)
            if rc == 0 and os.path.isfile(dst):
                got = True
                break
            if job:
                log(job, "  失败（退出码 %d）：%s" % (rc, _tail(out, 2)))
        if not got:
            if job:
                log(job, "  [warn] %s 下载不下来" % name)
    return rvc_pretrained_here(version, sr)


# ------------------------------------------------ RVC 音质参数
#
# ★ 这几个默认值是**实测选出来的**，不是抄教程。方法：拿用户自己的角色 +
#   自己的歌，切同一段 19 秒干声，跑参数矩阵。两轮测法不一样，都要看：
#
#   第一轮（只量音准）—— 后来发现这一轮的量具坏了，结论作废，见下面第 ③ 条。
#     组                                 match  contour  devIQR  起伏比
#     rmvpe ir0.75 rms0.25 p0.33（旧默认） 0.860   0.968     29     0.93
#     fcpe  ir0.75 rms0.25 p0.33          0.880   0.966     23     0.90
#     fcpe  ir0.50 rms0.25 p0.50          0.891   0.965     23     0.95
#     fcpe  ir0.75 rms0.50 p0.33          0.833   0.963     34     0.98   ← 掉得最狠
#
#   第二轮（量具修好后重测 protect；音准 + 咬字一起看）：
#     protect   吻合(±50)  宽松(±150)  轮廓   八度错   **ASR 字错误率**
#       0.50      87.5%     97.2%     0.968   0.3%       70.5%   ← 咬字最差
#       0.40      87.4%     97.4%     0.968   0.3%       54.1%
#       0.33      88.8%     97.8%     0.968   0.3%       54.1%
#       0.20      88.9%     97.8%     0.968   0.3%       52.5%
#
#   结论：
#   ① 换 FCPE 让逐帧偏差从 29 音分收紧到 23（这一条两轮都成立）。
#   ② rms-mix-rate **必须**留在 0.25 —— 网上有教程说 0.9「保留音量起伏」，
#      实测是反的（RVC 文档：越接近 1 越是恒定响度），照抄那篇会掉 3 个百分点。
#   ③ **protect 必须小于 0.5**。RVC 源码里是 `if protect < 0.5` 才启用清辅音
#      保护，所以 0.50 = **完全不保护**，清辅音也会被模型"唱"一遍 ⇒ 咬字糊。
#      第一轮曾把它改成 0.50（当时只量音准、且量具本身有 bug），第二轮用
#      **ASR 字错误率**当咬字代理重测：0.50 的字错误率 70.5%，比开启保护的
#      52~54% 高出一大截。音准则几乎不受影响（87~89%，都很好）。
#      0.33 / 0.40 / 0.20 三者差异在噪声内 ⇒ 取 RVC 社区默认的 0.33。
#   ④ index-rate 取 0.50：太高会吞字，太低音色不稳。
RVC_RMS_MIX_RATE = 0.25   # 保留原唱的音量起伏（0=全跟随原唱，1=恒定响度）
RVC_PROTECT = 0.33        # 必须 <0.5，否则等于关闭清辅音保护（见 ③）
RVC_INDEX_RATE = 0.50     # 检索强度；太高会吞字，太低音色不稳
#   ★★ 0.50 不是拍的，是 **2026-10-16 实测**出来的权衡拐点（`_probe_indexrate.py`
#      + `_probe_irate_qc.py`）。同一份人声（登阶 201s）、同模型、pitch −12、
#      f0/rms/protect 全固定，**只改 index_rate**，三把尺子一起量：
#
#        index_rate   音色(距角色中心，越小越像)   八度错    音准吻合   咬字(高频占比)
#          0.30              0.2645                8.1%      79.1%       0.37x
#          0.50  ←现值       0.2559                9.9%      76.9%       0.37x
#          0.70              0.2450 ←音色最好      11.2%     75.0%       0.36x
#          0.85              0.2494                11.5%     74.8%       0.35x
#
#      ⇒ **音色和音准是反的**：index_rate 越高音色越像角色（0.30→0.70 单调改善），
#        但**八度错单调恶化**（8.1%→11.5%）、吻合单调下降（79.1%→74.8%）。
#        0.70 音色最好，代价是八度错 +1.3 个百分点、吻合 −1.9 个百分点。
#      ⇒ 0.50 落在两条曲线的交叉区：音色已经比 0.30 好一截，音准还没开始明显掉。
#        官方 FAQ 推荐 0.5~0.75，实测 0.50 在"两者都不差"的位置。
#      ⚠ 别只看音色就往上调：0.70 的八度错 11.2% 已经越过音高体检的坏档线
#        （AUDITION_OCTAVE_BAD=0.11），会让体检更频繁地判"这一档不达标"。
#      ⚠ 咬字（4~8kHz 高频占比）四档只从 0.37x 掉到 0.35x —— "太高会吞字"在
#        这首歌上**没被证实**（相对变化 5%，远小于音色和音准的变化）。
# ★★ f0（基频）提取方法 —— **2026-10-17 实测改成了 rmvpe**，别再改回去。
#
#   文献口径（多篇一致）：rmvpe 是"六边形战士/2026 无脑首选"，fcpe 是"最快"、
#   pm 是"最快但容易哑音"。但**文献说了不算，本机实测说了算**
#   （`_probe_f0ab.py`：大帝模型 + 同一段 30 秒人声，pitch −12、index_rate 0.50、
#   protect 0.33 全部固定，只换 f0 方法）：
#
#     f0     推理用时   八度错   吻合(宽松)   轮廓相关   音高抖动(IQR)
#     fcpe    39.8 s    5.1 %     90.1 %      +0.606     31.7 cents   ← 又慢又抖
#     rmvpe   23.8 s    3.3 %     93.0 %      +0.714     17.1 cents   ★ 又快又准
#     pm      19.4 s    4.5 %     88.2 %        —           —
#
#   ⇒ rmvpe **同时**赢了速度和精度：八度错降 35%，音高抖动**直接减半**
#     （31.7 → 17.1 cents；1 个半音 = 100 cents，31.7 相当于三分之一半音的飘移）。
#     这条正对着老板说的"唱歌越唱越差 / 音听着飘" —— 地基（f0 曲线）本身在抖，
#     后面模型再好也唱不稳。
#   ⚠ 实测结果和资料说法**相反**：资料说 fcpe 最快，本机实测 fcpe 反而最慢
#     （39.8 s vs rmvpe 23.8 s）。所以换这个不花时间，是**白捡的收益**。
#   ⚠ 咬字（4~8kHz 高频占比）三档完全一样（都是输入的 0.44x）—— 换 f0 不影响咬字。
#   ⚠ Crepe 测不了：本机这份 `infer/vc/pipeline.py` 只认 pm / rmvpe / fcpe
#     （`get_f0` 第一行就 `if f0_method not in ("pm","rmvpe","fcpe"): raise`）。
RVC_F0_METHOD = "rmvpe"

# 非歌声段保护：口哨 / 纯音被 RVC 当"唱"会变成高两个八度的吼（实测见
# stages/nonvocal_guard.py 的文件头）。三种模式，默认保留原声。
NONVOCAL_MODES = ("keep", "mute", "off")


def _rvc_quality_args(rms_mix_rate=None, protect=None):
    """拼 RVC 推理的音质参数。

    ★ 为什么要显式传：这两个参数**不传就是用 RVC 自己的默认值**，而以前一个
    都没传，等于把音质交给了别人的默认值：
      --rms-mix-rate 默认 **1.0** —— 输出的响度包络 100% 抄输入音频；RVC 官方
        WebUI 的默认是 0.25。对干净人声输入，1.0 会把原唱的气息/音量起伏
        整条搬过来，听感一顿一顿、句尾突然掉下去。0.25 让模型自己控响度。
      --protect 默认 0.33 —— **必须小于 0.5**：RVC 源码里是 `if protect < 0.5`
         才启用清辅音保护，取 0.50 等于**完全不保护**，清辅音会被模型"唱"过
         一遍，咬字发糊（实测 ASR 字错误率 70.5% vs 开启保护的 52~54%）。
         详见上面参数表的第 ③ 条。
    """
    r = RVC_RMS_MIX_RATE if rms_mix_rate is None else float(rms_mix_rate)
    p = RVC_PROTECT if protect is None else float(protect)
    return "--rms-mix-rate %.2f --protect %.2f" % (max(0.0, min(1.0, r)),
                                                   max(0.0, min(0.5, p)))


# ------------------------------------------------ 非歌声段保护（"吹口哨就吼"的正解）
#
# 机制（实测，不是推测）：RVC 认不出"口哨"和"唱"的区别 —— 分离后的人声轨里，
# 口哨是一条极高极纯的持续音（实测神耀：f0 662Hz、谱平坦度 0.0013，正常歌唱段
# 是 f0 262Hz、平坦度 0.19，差一个数量级）。RVC 把它当"唱"，用角色嗓音重发出来：
# 男声角色把 662Hz 降 19 半音得到 220Hz —— 用假声持续吼。这一段调参救不了。
# 所以做法是：把这些段判出来，**不用 RVC 的输出，换回原始人声轨**。

def guard_nonvocal(job, src_vocal, rvc_voice, out_wav, mode="keep"):
    """把 RVC 输出里的「非歌声段」（口哨/纯音）换回原始人声。

    返回 (要用的 wav 路径, 说明 dict 或 None)。跑不通时**不抛异常** ——
    退回 RVC 原样输出（音质差一点，但不能把整条链带死）。
    """
    if mode not in NONVOCAL_MODES:
        mode = "keep"
    if mode == "off":
        return rvc_voice, None
    report = os.path.join(os.path.dirname(out_wav) or ".", "_nonvocal.json")
    rc, out = run(job,
                  '"%s" -s "%s" --src "%s" --rvc "%s" --out "%s" --mode %s --report "%s"'
                  % (RVC_PY, os.path.join(STAGES, "nonvocal_guard.py"),
                     src_vocal, rvc_voice, out_wav, mode, report),
                  cwd=RVC_ROOT, timeout=1800)
    if rc != 0 or not os.path.exists(out_wav):
        log(job, "  [warn] 非歌声段保护没跑通（退出码 %d），按 RVC 原样输出：%s"
            % (rc, _tail(out, 3)))
        return rvc_voice, None
    info = None
    with contextlib.suppress(Exception):
        with open(report, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(report)
    for line in (out or "").splitlines():
        if line.strip().startswith("全曲谱平坦度") or line.strip().startswith("判定为非歌声"):
            log(job, "  " + line.strip())
    return out_wav, info


# ------------------------------------------------ 成品客观质检（把"没法听"变成数）
#
# 为什么值得单独做一步：以前"唱得好不好"只能靠耳朵，出了问题也说不出是哪一种。
# 这一步用 RMVPE（和推理链同一个 f0 提取器）把**原唱人声轨**和**角色变声后的
# 人声轨**逐帧比基频，给出四个能对账的数：
#   shift_semitones  实际整体平移了几个半音 —— 跟设定的 pitch 对得上吗？
#   match_ratio      对齐后音高落在同一个音上的比例（±50 音分内）
#   contour_corr     旋律轮廓的相关性（先在 ±1.5 秒里找最像的位置再比）
#   spread_ratio     角色人声的音高起伏 ÷ 原唱的起伏 —— ≈1 忠实地跟；≈0 唱成平线
# spread_ratio 这一项是**专门补 match_ratio 的盲**：一条完全不动的平线，
# 去掉整体偏移后每帧偏差都是 0，吻合率反而 100%，看着完美。
#
# 判据本身（够不够好）不放在 stage 里，而是放在这里写成人话 —— stage 只负责量，
# 这样在验收里可以放心地对"量出来的数"下断言，而不用去猜 stage 的判断逻辑。

def qc_singing(job, src_vocal, dst_voice, expect_pitch=None, start=None, secs=None,
               skip=None):
    """跑一次客观质检，返回五元组或 None（跑不通不抛异常，不能把整条链带死）。

    skip 是**绝对时间**的 [(起, 止), ...] 秒区间：被非歌声段保护换回原声的那几段
    要排除掉 —— 它们故意没变调，算进音准会得出"整首歌都跑调"的假结论
    （实测不排除时吻合 58.9%/偏差 IQR 1905 音分，排除后才是真实水平）。
    """
    tag = "qc_%s.json" % time.strftime("%m%d_%H%M%S")
    out_json = os.path.join((job or {}).get("dir") or DATA, tag)
    cmd = '"%s" -s "%s" "%s" "%s" "%s"' % (
        RVC_PY, os.path.join(STAGES, "qc_cli.py"), src_vocal, dst_voice, out_json)
    # ★ 试听小样那条路 start 就是 0.0 —— 原先写的是 `if start:`，0.0 为假，
    #   于是窗口参数被整个跳过，后面拼上来的"跳过段"就顶到了 argv[4]，
    #   报的是 "could not convert string to float: '16.27-17.70,...'"
    #   （看着像段格式错了，其实是有没有传窗口）。所以这里必须判 `is not None`。
    has_win = (start is not None and secs)
    if has_win:
        cmd += " %.3f %.3f" % (float(start), float(secs))
    if skip and has_win:
        st, en = float(start), float(start) + float(secs)
        parts = []
        for a, b in skip:
            a2, b2 = max(st, float(a)), min(en, float(b))
            if b2 - a2 > 0.05:
                parts.append("%.3f-%.3f" % (a2 - st, b2 - st))
        if parts:
            cmd += ' "%s"' % ",".join(parts)
    rc, _out = run(job, cmd, cwd=RVC_ROOT, timeout=1800)
    if rc != 0 or not os.path.isfile(out_json):
        return None
    try:
        with open(out_json, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


QC_WHOLE_WIN = 30.0     # 整曲质检每窗长度（和单窗质检同一口径，好对照）
QC_WHOLE_MAX = 8        # 最多几窗 —— 歌再长也不让质检时间无限涨
# ★ 为什么还要一条"整曲"质检（单窗那条已经跑了为什么不就完了）：
#   单窗那条取的是 `_qs = (总长-30)*0.35` 固定位置的一段。而八度错在**同一首歌内部**
#   变化极大 —— 实测登阶那一版（201 秒）按 30 秒窗口**等距扫七窗**：
#       窗口起点   0s    28s    57s    85s   114s   142s   171s
#       八度错   12.7%   8.2%  14.7%  16.1%   3.5%   5.6%  16.9%
#   同一份成品，报 3.5% 也对、报 16.9% 也对 —— **报出来的数取决于取到哪一段**。
#   给用户看一个抽签出来的数，比不给更糟：他会照着这个数决定要不要重唱。
#   ⇒ 整曲多窗：把整首铺满（等距铺开、含头含尾），再聚合成一个数，并单独报出
#     「最差那一窗」—— 听感由最差的那段决定，不是平均值。


def _whole_windows(total, win=QC_WHOLE_WIN, max_win=QC_WHOLE_MAX):
    """把 [0, total] 铺满：返回 [(起点, 长度)]，等距铺开且**含头含尾**。"""
    if total <= win or max_win <= 1:
        return [(0.0, total)]
    # ceil(total/win) 用整数算术写，避免为这一处去 import math
    n = int(total // win) + (1 if (total % win) > 1e-6 else 0)
    n = min(int(max_win), max(2, n))
    step = (total - win) / float(n - 1)
    return [(round(i * step, 3), win) for i in range(n)]


def qc_singing_whole(job, src_vocal, dst_voice, win=QC_WHOLE_WIN,
                     max_win=QC_WHOLE_MAX, skip=None):
    """整曲多窗质检：铺满整首逐窗量，聚合成一个能代表"整首"的数。

    聚合口径（每条都写清为什么）：
      · 八度错 / 宽松吻合 / 起伏比 —— 按**有效帧数加权**平均（唱得多的窗更代表整首）
      · 轮廓相关 —— 各窗**简单平均**（相关系数不是比例，不能加权）
      · 另报 worst_octave / worst_at —— **最差那一窗**。听感由最差那段决定：
        整首平均 12% 但有一段 21%，人耳记住的是那段。
    任何一窗失败都不影响其余 —— 拿不到就不聚合它，全拿不到才返回 None。
    返回 dict 或 None（跑不通不抛异常，不能把整条链带死）。
    """
    total = wav_seconds(src_vocal) or 0.0
    if total < 3.0:
        return None
    per = []
    for (s0, sec) in _whole_windows(total, win, max_win):
        d = qc_singing(job, src_vocal, dst_voice, start=s0, secs=min(sec, total - s0),
                       skip=skip)
        if d:
            d["_at"] = round(s0, 1)
            per.append(d)
    if not per:
        return None
    tot_f = sum(int(d.get("aligned_frames") or 0) for d in per)

    def wavg(k):
        if tot_f <= 0:
            vals = [d.get(k) for d in per if d.get(k) is not None]
            return (sum(vals) / len(vals)) if vals else None
        num = sum(float(d.get(k) or 0.0) * int(d.get("aligned_frames") or 0) for d in per)
        return num / tot_f

    def avg(k):
        vals = [d.get(k) for d in per if d.get(k) is not None]
        return (sum(vals) / len(vals)) if vals else None

    worst = max(per, key=lambda d: float(d.get("octave_err_ratio") or 0.0))
    return {
        "windows": len(per),
        "win_secs": round(win, 1),
        "tot_secs": round(total, 1),
        "match_ratio": wavg("match_ratio"),
        "match_ratio_loose": wavg("match_ratio_loose"),
        "octave_err_ratio": wavg("octave_err_ratio"),
        "spread_ratio": wavg("spread_ratio"),
        "contour_corr": avg("contour_corr"),
        # ★ 音高抖动（cents，1 个半音 = 100）—— 2026-10-17 加进来。
        #   它量的是"唱出来的音在自己该在的位置附近晃多厉害"，对应老板说的
        #   "唱歌越唱越差 / 音听着飘"：**八度错是偶尔翻八度，抖动是全程都在晃**，
        #   后者才是"不像在唱歌"的日常观感。之前这个数只躺在 qc_cli 的 JSON 里，
        #   返回值里没有 ⇒ 谁想用都得去翻日志（第一版门禁就因为读不到它假红过一次）。
        #   实测对照（大帝 + 30 秒人声，只换 f0 方法）：fcpe 31.7 / rmvpe 17.1。
        "dev_iqr_cents": avg("dev_iqr_cents"),
        "shift_semitones": avg("shift_semitones"),
        "worst_octave": float(worst.get("octave_err_ratio") or 0.0),
        "worst_at": worst.get("_at"),
        "worst_match": worst.get("match_ratio_loose"),
        "per_window": [{"at": d.get("_at"),
                        "octave_err_ratio": d.get("octave_err_ratio"),
                        "match_ratio_loose": d.get("match_ratio_loose"),
                        "contour_corr": d.get("contour_corr")} for d in per],
    }


def describe_qc_whole(w):
    """把整曲质检翻成人话，返回 (是否需要提醒, 一行摘要)。"""
    if not w:
        return False, ""
    oer = float(w.get("octave_err_ratio") or 0.0)
    mrl = float(w.get("match_ratio_loose") or 0.0)
    worst = float(w.get("worst_octave") or 0.0)
    line = ("整曲 %d 窗：音准吻合 %.1f%%、八度跳 %.1f%%、起伏比 %.2f（最差一窗 %.1f%% @%.0fs）"
            % (w.get("windows") or 0, mrl * 100, oer * 100,
               float(w.get("spread_ratio") or 0.0), worst * 100,
               float(w.get("worst_at") or 0.0)))
    warn = []
    if oer > 0.10:
        warn.append("整首有 %.1f%% 的帧在八度上跳（听感：飘、像两个人在唱）" % (oer * 100))
    if worst > 0.15:
        warn.append("最差的一段到 %.1f%%（第 %.0f 秒附近）"
                    % (worst * 100, float(w.get("worst_at") or 0.0)))
    if mrl < 0.75:
        warn.append("整首音准吻合只有 %.1f%%" % (mrl * 100))
    if warn:
        return True, (line + " —— " + "；".join(warn)
                      + "。可以换一档音高（相邻八度 / 0 半音）重唱一版对比")
    return False, line


def describe_qc(d, expect_pitch=None):
    """把质检的数翻成人话，返回 (是否通过, 给用户看的一行, 详细行列表)。

    ★ 阈值不是拍脑袋定的，是拿**负对照**量出来的（_probe_ruler_negctl.py，
    同一段人声做出已知真值的四个变体，实测）：
        dst = src 本身          宽松吻合 100.0%  轮廓相关 +1.000  八度错  0.0%
        dst = src 倒放          宽松吻合  13.0%  轮廓相关 -0.658  八度错 19.4%
        dst = src 按 1 秒乱序   宽松吻合  32.2%  轮廓相关 +0.112  八度错  8.2%
        dst = 后一半降 5 半音   宽松吻合  65.4%  轮廓相关 +0.758  八度错  7.1%
        真实的 RVC 输出          宽松吻合  95.6%  轮廓相关 +0.897  八度错  1.0%
    于是：宽松吻合 <0.70（会把"半对半错"判红、真实产物判绿）；
         轮廓相关 <0.50（把倒放/乱序判红）。
    ★ 为什么主判据用「宽松口径 ±150 音分」而不是 ±50：±50 在人声上**没有判别力**
    —— 把原唱精确变调（数学上偏差恒 0，理论上该读 100%）也只有 47.2%，因为颤音
    和滑音让逐帧基频本来就在动。用 ±50 会把好录制报成失败（实测误报过一次）。
    """
    if not d:
        return None, "客观质检没跑出来（不影响成品，成品仍可用）", []
    det = []
    mr = d.get("match_ratio") or 0.0
    mrl = d.get("match_ratio_loose")
    if mrl is None:
        mrl = mr                       # 兼容没有新字段的旧质检记录
    sp = d.get("spread_ratio") or 0.0
    cc = d.get("contour_corr") or 0.0
    oer = d.get("octave_err_ratio") or 0.0
    lag = d.get("lag_ms") or 0.0
    shift = d.get("shift_semitones") or 0.0

    det.append("音准吻合 %.1f%%（宽松口径 %.1f%%）、旋律轮廓相关 %+.2f、音高起伏比 %.2f、"
               "八度跳 %.1f%%、时间错位 %+.0fms"
               % (mr * 100, mrl * 100, cc, sp, oer * 100, lag))
    bad = []
    if abs(lag) > 250:
        bad.append("人声和伴奏差了 %.0fms（对齐有问题）" % lag)
    if oer > 0.15:
        bad.append("有 %.0f%% 的帧在八度上跳（旋律会突然翻高/翻低）" % (oer * 100))
    if sp < 0.5:
        bad.append("角色唱得太平：音高起伏只有原唱的 %.0f%%，旋律基本没跟"
                   % (sp * 100))
    elif mrl < 0.70:
        bad.append("音准吻合只有 %.0f%%，旋律跟得不稳" % (mrl * 100))
    if cc < 0.50:
        bad.append("旋律轮廓相关只有 %+.2f（跟走调/错位一个量级）" % cc)
    if expect_pitch is not None:
        want = float(expect_pitch)
        det.append("音高设定 %+d 半音，实测 %+.1f 半音" % (int(want), shift))
        if abs(shift - want) > 2.0:
            bad.append("音高设定没生效：设了 %+d，实测只有 %+.1f" % (int(want), shift))
    if (d.get("clip_ratio") or 0) > 0.001:
        bad.append("有 %.2f%% 的采样削幅（会破音）" % ((d.get("clip_ratio") or 0) * 100))
    if bad:
        return False, "客观质检发现问题：" + "；".join(bad), det
    return True, ("客观质检通过：音准吻合 %.0f%%（宽松口径 %.0f%%），音高起伏比 %.2f"
                  % (mr * 100, mrl * 100, sp)), det


def _role_ref_dir(rid):
    """角色的「音域参考」素材目录。

    优先 rvc_dataset（就是这个歌声模型的训练音源，音域口径最一致），
    退回手工切片 slices。
    """
    d = role_dir(rid)
    for sub in ("rvc_dataset", "slices"):
        p = os.path.join(d, sub)
        if os.path.isdir(p) and any(f.lower().endswith(".wav") for f in os.listdir(p)):
            return p
    return None


def estimate_pitch(job, vocal_wav, role):
    """量原唱与角色的基频中位数，返回 (半音, 依据文字)。

    拿不到就返回 (0, 原因) —— 宁可回到「不变调」这个保守默认，也不要瞎给一个
    数让整首歌报废。所以这里所有的失败路径都不抛异常。
    """
    rid = role.get("id")
    ref_dir = _role_ref_dir(rid)
    if not ref_dir:
        return 0, "这个角色没有可用的干声素材，先按不变调（0）来"
    d = role_dir(rid)
    out_json = os.path.join(d, "final", "_pitch_%d.json" % int(time.time()))
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" 8'
                  % (RVC_PY, os.path.join(STAGES, "pitch_cli.py"),
                     vocal_wav, ref_dir, out_json), cwd=RVC_ROOT, timeout=900)
    data = None
    if rc == 0 and os.path.exists(out_json):
        try:
            data = json.load(open(out_json, encoding="utf-8"))
        except Exception:
            data = None
    with contextlib.suppress(Exception):
        os.remove(out_json)
    if not data:
        return 0, "音高估计没跑通（退出码 %d），先按不变调（0）来：%s" % (rc, _tail(out, 2))
    p = int(data.get("semitones", 0))
    why = ("原唱基频中位 %.0f Hz，角色 %.0f Hz ⇒ %+d 半音"
           % (data.get("src_med", 0), data.get("ref_med", 0), p))
    if abs(p) >= 10:
        why += "（差了大半个八度，通常说明原唱和角色的音域/性别不同）"
    if str(role.get("kind")) == "speech" and abs(p) >= 6:
        # 说话素材的基频中位数**低于**同一个人唱歌时的音域，所以按它算出来的
        # 降幅会偏大。这是方法本身的偏差，得说出来，不能让用户以为它是准的。
        why += "；角色参考是说话素材，唱歌音域通常更高，觉得闷就往正数再调 2~4"
    return p, why


def _find_rvc_index(model_name):
    """按 RVC 自己的规则找到这个模型能用的 added 索引库，返回路径或 None。

    以前这里是 `os.path.exists(assets/indices/<stem>.index)`，这个拼法**永远
    不成立**：train_index.py 产出的名字是
        <exp>_added_IVF<n>_Flat_nprobe_1_<exp>_v1.index
    而 stem 还是 `<exp>_e150_s1200` 之类的带轮数后缀，两边对不上。
    结果就是自训模型也被判定成「没有索引库」，index_rate 被白降级成 0，
    明明刚建好的索引一次没用上。

    RVC 推理侧 infer/vc/utils.py 的 get_index_path_from_model() 才是权威：
      - 先把模型名里的 `_e<轮>_s<步>` 尾巴去掉，剩下实验名
      - 跳过名字里带 trained 的（那是合并索引，不是 added）
      - 接受 `<exp>_added_*` / 含 `_<exp>_v1` / 含 `_<exp>_v2` / 名字里直接
        含完整模型名的
    这里照同样的口径实现一份，避免为一个纯查文件的判断去起 RVC 的 torch 运行时。
    """
    stem = os.path.splitext(os.path.basename(model_name or ""))[0]
    exp = re.sub(r"_e\d+_s\d+$", "", stem, flags=re.IGNORECASE)
    if not exp:
        return None
    root = os.path.join(RVC_ROOT, "assets", "indices")
    if not os.path.isdir(root):
        return None
    le, ls = exp.lower(), stem.lower()
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(".index") or "trained" in name.lower():
                continue
            low = os.path.splitext(name)[0].lower()
            if (low.startswith(le + "_added_")
                    or ("_" + le + "_v1") in low
                    or ("_" + le + "_v2") in low
                    or ls in low):
                return os.path.join(dirpath, name)
    return None


def _ckpt_ep(f):
    try:
        return int(f.split("-e")[-1].split(".")[0])
    except Exception:
        return -1


def _ensure_ref(role, rid, d, job):
    """角色没有参考音频时挑一个并保存。返回 (ref_file, ref_text)。"""
    ref_file = (role.get("ref") or {}).get("file")
    ref_text = (role.get("ref") or {}).get("text", "")
    if not ref_file:
        p, t = pick_reference(role, os.path.join(d, "slices"))
        ref_file, ref_text = os.path.basename(p), t
        mutate_role(rid, lambda r: r.__setitem__("ref", {"file": ref_file, "text": ref_text}))
    return ref_file, ref_text


def verify_ckpt(job, rid, ckpt, ref_wav, ref_text, lang="zh"):
    """
    拿一个 ckpt 合成一句体检文本，过出声判据才算数。
    返回 (ok, info)：info 是合成结果（ok 时）或失败原因（不 ok 时）。
    """
    tasks = [{"text": VERIFY_TEXT,
              "out": os.path.join(job["dir"], "verify_%s.wav" % os.path.basename(ckpt))}]
    tj = os.path.join(job["dir"], "verify_tasks.json")
    rj = os.path.join(job["dir"], "verify_result.json")
    json.dump(tasks, open(tj, "w", encoding="utf-8"), ensure_ascii=False)

    # 必须先清旧结果：合成失败时它不会更新，读到的会是上一次的「合格」，
    # 坏权重就蒙混过关了（实测抓到过）
    if os.path.exists(rj):
        os.remove(rj)

    lang_name = {"zh": "中文", "ja": "日文", "en": "英文", "ko": "韩文", "yue": "粤语"}.get(lang, "中文")
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "tts_batch.py"), GSV_ROOT,
                     ckpt.replace("\\", "/"), S2G, ref_wav, ref_text.replace('"', ""),
                     lang_name, tj, rj), timeout=1800)
    if rc != 0:
        return False, {"error": "合成失败（退出码 %d）：%s" % (rc, (out or "")[-200:])}
    results = json.load(open(rj, encoding="utf-8")) if os.path.exists(rj) else []
    if not results:
        return False, {"error": "合成没产出结果（退出码 %d）" % rc}
    r = results[0]
    return bool(r.get("ok")), r


def tone_index_path(rid):
    return os.path.join(role_dir(rid), "tone_index.json")


# ---------------------------------------------------------------- 长台词
#
# 老板的反馈：「说话太长台词会乱掉」。根因和实测数据记在 tone.py 顶部（那里是
# 切分器所在）与 stages/tts_batch.py 顶部（那里管"多跑一次取更完整的那次"）。
# 这里只放两个数：
SPEAK_TAKES = 3        # 每段最多合成几次（第一次可疑才跑第二次，以此类推）
#   ★ 为什么不是"永远跑两次"：正常段第一次就合格，多跑纯属浪费；而可疑段
#     （秒/字掉到基准 85% 以下 = 有字没念出来）多跑一次能救回来一半以上
#     （实测同一段同一参考两次结果不同，换参考后 5 个里 2 个成功）。
#   ★ 为什么给到 3：门禁真跑时撞见过"换了参考还是丢"的段
#     （「哪怕只是一句话也好。我等了那么久，」两次都只念出前半句），
#     第三个参考能把这类的成功率再抬一截。代价只有那几段多跑 2 秒。
SPEAK_SPC_CALIB = 0.9  # 素材的"秒/字"要打折才等于合成产物的正常值
#   ★ 这个 0.9 是量出来的，不是拍的：阿塔尼斯 212 段素材的中位是 0.271 秒/字，
#     而同一角色合成出来的正常段是 0.20~0.27（中位 0.24）—— 产物比素材快约 11%。
#     不校正的话，"正常段"会被系统性地误判成"偏短"，每段都白跑一次第二次合成。


def _base_spc_from_role(role):
    """这个角色「一个字念多久」的粗基准 —— 只用元数据，不碰音频、不依赖语气索引。

    为什么需要它：判断"这一段是不是被吞了字"唯一的廉价信号就是**时长**。
    没有基准的话，0.19 秒/字到底是"念得快"还是"少了六个字"根本分不清。
    """
    v = []
    for s in role.get("segments") or []:
        n = len([c for c in (s.get("text") or "") if c.strip()])
        dur = float(s.get("dur") or 0)
        if n < 6 or not (1.0 <= dur <= 14.0):
            continue
        spc = dur / n
        if 0.12 <= spc <= 0.7:
            v.append(spc)
    if not v:
        return 0.0
    v.sort()
    # 返回**素材**的秒/字，不含校准系数 —— 校准统一在 speak_role 里乘，
    # 免得"用索引算的基准"和"用元数据算的基准"两条路各乘一次、越乘越偏。
    return round(v[len(v) // 2], 4)


def _alt_refs(role, d, items, tone_key, used_wav, n=2):
    """给「这一段没合成好、重来一次」准备的备选参考。

    为什么重试要**换参考**而不是原地重跑：段首丢字虽然随机，但同一段参考跑两次
    实测给出的是同一个坏结果（等于掷同一个骰子）；换一段参考才有机会绕开它。

    ★ 而且换的时候要**往"说得快"的方向换**（_probe_long5.py，同一段文本）：
        参考 0.264 秒/字（正常偏快） → 完整念出 ✓
        参考 0.224 秒/字（偏快）     → 完整念出 ✓
        参考 0.172 秒/字（偏慢）     → 照样吞掉 ✘
        默认参考（0.21 附近，拖）     → 吞掉 ✘
      语速/top_k/top_p/温度八个旋钮全试过，一点用都没有 —— 能改变结果的只有
      **换参考**。所以这里不是"随便抓一段别的"，是按每秒字数从快到慢往下挑。

    `items` 没有索引（角色没建过）时退回切片元数据算每秒字数，照样能挑快到慢。
    """
    used = set([os.path.basename(used_wav or "")])
    picks = []

    # file -> 时长。语气索引（items）里不带 dur，只能从切片元数据反查；
    # 反查不到或不可信时再实量文件（成本只落在个别文件上，不会每句都读盘）。
    dur_of = {}
    for s in role.get("segments") or []:
        if s.get("file"):
            dur_of[os.path.basename(s["file"])] = float(s.get("dur") or 0)

    def _dur(f):
        dd = dur_of.get(f)
        if ref_dur_ok(dd):
            return dd
        return wav_seconds(os.path.join(d, "slices", f))

    def fast_first(rows):
        """rows: [(spc, file, text)] —— spc 小的（说得快的）排前面。

        ★ 时长校验放在这里，是因为两个候选来源（语气索引 / 切片元数据）最后
          都从这一个出口出去 —— 闸在这里就闸住了全部出口。
          为什么必须闸：下游 GPT-SoVITS 对参考音频是硬性 3~10 秒，超了直接
          raise OSError。以前这里写的是 `1.0 <= dur <= 14.0`，于是大帝备选里
          那段 **1.60 秒**的切片被选中，长台词每段第 2 遍重试必抛异常 ——
          而"换参考"是长句丢字唯一的兜底（见上面八个旋钮的实测）。
          换句话说：护栏宽了一点，兜底就整个不存在了。
        """
        for spc, f, t in sorted(rows, key=lambda x: x[0]):
            if f in used or not t:
                continue
            if not ref_dur_ok(_dur(f)):
                continue        # 宁可少给备选，也不给一个下游必然报错的
            picks.append((f, t))
            used.add(f)
            if len(picks) >= n:
                break

    if items:
        rows = []
        for it in items:
            f = it.get("file")
            t = (it.get("text") or "").strip()
            sr = float(it.get("syl_rate") or 0)
            if f and t and sr > 0:
                rows.append((1.0 / sr, os.path.basename(f), t))
        fast_first(rows)
    if len(picks) < n:
        rows = []
        for s in role.get("segments") or []:
            f = s.get("file")
            t = (s.get("text") or "").strip()
            dur = float(s.get("dur") or 0)
            nc = len([c for c in t if c.strip()])
            if f and t and nc >= 6 and dur > 0:
                rows.append((dur / nc, os.path.basename(f), t))
        fast_first(rows)

    slices = os.path.join(d, "slices")
    return [{"ref_wav": os.path.join(slices, f), "ref_text": t} for f, t in picks]


def ensure_tone_index(rid, job=None):
    """拿到角色的声学索引；没有就现场建一个。

    建索引要用 librosa，那套在 GSV runtime 里才有，所以走子进程 ——
    不能把 librosa 塞进服务进程（同 torch schedule 冲突的教训见 tts_batch 顶部）。

    返回 (items 列表, 备注)。失败也不抛 —— 没有索引就退化成「只能用参数控语气」，
    不该让整个「说话」功能挂掉。
    """
    p = tone_index_path(rid)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8")).get("items") or [], ""
        except Exception as e:                                       # noqa: BLE001
            pass
    d = role_dir(rid)
    if not os.path.isdir(os.path.join(d, "slices")):
        return [], "没有切片"
    if job:
        log(job, "第一次用语气功能 —— 先把这个角色的 212 段语音量一遍（约 1 分钟，只做一次）")
    rc, out = run(job, '"%s" -s "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "tone_index.py"), d, p),
                  timeout=1800)
    if rc != 0 or not os.path.isfile(p):
        return [], "建索引失败：%s" % ((out or "")[-200:] or "退出码 %s" % rc)
    try:
        return json.load(open(p, encoding="utf-8")).get("items") or [], ""
    except Exception as e:                                           # noqa: BLE001
        return [], "索引读不出来：%s" % e


def speak_role(job, rid, texts, lang="zh"):
    """用角色音色说几句话。

    `texts` 可以是字符串列表（老写法），也可以是词典列表 —— 词典写法能带
    `tone`（语气 key）和 `raw`（原始一行），后者用来就地认 `[愤怒]` 这种标签。

    为什么要支持「在提示词里写语气」而不是给每行配个下拉：
    老板就是这么写词的—— "[愤怒]你怎么敢这么做！" —— 让他再去点表单是倒退。
    """
    role = get_role(rid)
    if not role or not role.get("model"):
        raise Fail("这个角色还没训练过，先点「训练」再说。")
    d = role_dir(rid)
    final = os.path.join(d, "final")
    os.makedirs(final, exist_ok=True)

    ref_file = (role.get("ref") or {}).get("file")
    ref_text = (role.get("ref") or {}).get("text", "")
    if not ref_file:
        p, t = pick_reference(role, os.path.join(d, "slices"))
        ref_file, ref_text = os.path.basename(p), t
        mutate_role(rid, lambda r: r.__setitem__("ref", {"file": ref_file, "text": ref_text}))
    ref_wav = os.path.join(d, "slices", ref_file)

    # ---------------------------------------------------------------- 拆句 + 认语气
    # 每一行都可能是「[愤怒]你怎么敢」这种带标签的写法，也可能是干净的台词。
    lines = []
    for it in texts:
        if isinstance(it, dict):
            raw = str(it.get("raw") or it.get("text") or "")
            forced = it.get("tone")
        else:
            raw = str(it or "")
            forced = None
        text, key, tags, unknown = tone.split_tone(raw)
        if forced and not tags:
            key = forced
        text = (text or "").strip()
        if not text:
            continue
        lines.append({"text": text, "tone": key, "tags": tags, "unknown": unknown})
    if not lines:
        raise Fail("没有要说的文本。")

    want_tone = [L for L in lines if L["tone"] and L["tone"] != "auto"]
    items, idx_err = ([], "")
    if want_tone:
        items, idx_err = ensure_tone_index(rid, job)
        if idx_err:
            log(job, "  （语气索引没建起来：%s —— 这次先用参数的办法）" % idx_err)

    # ★ 这个角色的"正常秒/字"。判"这一段是不是被吞了字"只有时长这一个廉价信号，
    #   没有基准分不清"念得快"和"少了六个字"。有语气索引就用它的语速中位，
    #   没有就退回切片元数据（时长/字数）—— 保证没建过索引的角色也能用上。
    raw_spc = tone.median_spc(items) or _base_spc_from_role(role)
    base_spc = round(raw_spc * SPEAK_SPC_CALIB, 4) if raw_spc else 0.0

    stamp = time.strftime("%m%d_%H%M%S")
    seg_dir = os.path.join(final, "_seg")
    tasks, notes, meta = [], [], []
    for i, L in enumerate(lines):
        # ★ 一句话切成几个"合成单元"（整段合成 147 字实测会丢三大块，见 tone.py 顶部）
        segs = tone.split_segments(L["text"])
        if not segs:
            continue
        joinp = os.path.join(final, "say_%s_%02d.wav" % (stamp, i + 1))
        key = L["tone"]
        rw, rt, spd, temp = "", "", 1.0, 0.6
        if key and items:
            got = tone.pick(items, key)
            t = tone.TONES_BY_KEY.get(key)
            nm = tone.TONES_BY_KEY.get(key, {}).get("name", key)
            if got:
                rw = os.path.join(d, "slices", got["file"])
                rt = got.get("text", "")
                spd = float(t.get("speed", 1.0))
                temp = float(t.get("temperature", 0.6))
                notes.append("第 %d 句【%s】→ 参考「%s」"
                             % (i + 1, nm, (got.get("text") or "")[:16]))
            else:
                notes.append("第 %d 句【%s】没挑到合适的参考，用默认音色" % (i + 1, nm))
        elif key:
            # 没有索引时，参数（语速/温度）还是要给 —— 不然用户写了标签却听不出区别
            t = tone.TONES_BY_KEY.get(key)
            if t:
                spd = float(t.get("speed", 1.0))
                temp = float(t.get("temperature", 0.6))
        for u in L["unknown"]:
            notes.append("★ 第 %d 句写的「%s」不在语气表里，当成普通文字处理了"
                         % (i + 1, u))
        if len(segs) > 1:
            notes.append("第 %d 句 %d 字 → 按换气点切成 %d 段合成后拼回一句（段长 %s）"
                         % (i + 1, len(L["text"]), len(segs),
                            "/".join(str(len(s["text"])) for s in segs)))

        alts = _alt_refs(role, d, items, key, rw, n=SPEAK_TAKES - 1)
        for j, sg in enumerate(segs):
            one = (len(segs) == 1)
            task = {"text": sg["text"],
                    "out": joinp if one else os.path.join(
                        seg_dir, "say_%s_%02d_seg%02d.wav" % (stamp, i + 1, j + 1))}
            if base_spc or alts:
                task["takes"] = SPEAK_TAKES
            if rw:
                task["ref_wav"] = rw
                task["ref_text"] = rt
                task["speed"] = spd
                task["temperature"] = temp
            if alts:
                task["alt_refs"] = alts
            if base_spc:
                task["base_spc"] = base_spc
            if not one:
                task["join"] = joinp
                task["gap"] = sg["gap"]
            tasks.append(task)
            meta.append({"line": i, "seg": j, "segs": len(segs), "join": joinp})
    if not tasks:
        raise Fail("没有要说的文本。")

    if base_spc:
        notes.append("每段合成两遍取更完整的那次（这个角色的正常语速 %.3f 秒/字；"
                     "整段直接合成会在长台词里跳字）" % base_spc)
    for n in notes:
        log(job, "  " + n)
    # ★ 认不出的标签**必须说出来**。静默吞掉的话，用户以为自己标了语气，
    #   实际是被当成台词念出来 —— 这种"没生效但不知道"比报错难受得多。

    tj = os.path.join(job["dir"], "tts_tasks.json")
    rj = os.path.join(job["dir"], "tts_result.json")
    json.dump(tasks, open(tj, "w", encoding="utf-8"), ensure_ascii=False)

    lang_name = {"zh": "中文", "ja": "日文", "en": "英文", "ko": "韩文", "yue": "粤语"}.get(lang, "中文")
    job["stage"] = "合成语音"
    # ★★ 跑之前先删掉上一次的结果文件，跑完**必须检查退出码**。
    #   原来的写法是：
    #       rc, out = run(...)
    #       results = json.load(open(rj)) if os.path.exists(rj) else []
    #   tts_batch 失败时不会写新的 result.json，于是这里**静默读到上一次的成品** ——
    #   上层看到"成功"、拿到成品路径、算出指标，而用户听到的是**上一次的音频**。
    #   这是最坏的一类假绿：这一步什么都没干，报表却全绿。
    #
    #   实测（2026-10-18 说话轮数 A/B）：B 臂三次测量全部指向 A 臂的 wav ——
    #   k=0 因 weight.json 指向被挪走的旧权重而失败（退出码 2），
    #   k=1 / k=2 读到上一轮留下的 result.json，于是"20 轮 vs 60 轮"
    #   看起来一模一样（CER 极差 0.0），实际上 **20 轮一次都没测到**。
    #   同一个 job 目录被多次复用（探针、重跑）时必然踩到。
    if os.path.exists(rj):
        try:
            os.remove(rj)
        except OSError:
            pass
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "tts_batch.py"), GSV_ROOT,
                     role["model"]["ckpt"], S2G, ref_wav, ref_text.replace('"', ""),
                     lang_name, tj, rj), timeout=3600)
    if rc != 0:
        _tail = "\n".join((out or "").strip().splitlines()[-6:])
        raise Fail("合成子进程失败（退出码 %d）：\n%s" % (rc, _tail or "（没有输出）"))
    if not os.path.exists(rj):
        raise Fail("合成子进程没产出结果文件（退出码 0 但 %s 不存在）"
                   % os.path.basename(rj))
    results = json.load(open(rj, encoding="utf-8"))

    # 每句用了什么语气、切了几段，跟着成品一起存 —— 过几天回听时得知道当时怎么写的。
    # ★ 一句话可能由多段拼成（join），产物清单里**只列拼好的那一句**，
    #   段文件留在盘上给复听用；否则用户看到"我说了 3 句"却列出 11 个文件。
    idx_line = {}
    for k, m in enumerate(meta):
        idx_line[k] = m["line"]
    groups = {}
    for k, m in enumerate(meta):
        groups.setdefault(m["join"], []).append(k)

    def _add(r):
        for jp, ids in groups.items():
            rs = [x for x in results if x.get("index") in ids]
            good = [x for x in rs if x.get("ok")]
            if not good:
                continue
            li = idx_line.get(ids[0], 0)
            L = lines[li] if li < len(lines) else {"text": "", "tone": ""}
            sims = [x.get("similarity") for x in good if x.get("similarity")]
            warns = [x["warn"] for x in rs if x.get("warn")]
            jsec = next((x.get("join_seconds") for x in rs
                         if x.get("join_seconds")), None)
            r.setdefault("outputs", []).append({
                "text": L.get("text", ""), "wav": jp, "at": _now(), "kind": "speech",
                "seconds": jsec if jsec is not None else good[0].get("seconds"),
                "similarity": round(min(sims), 4) if sims else None,
                "tone": L.get("tone") or "",
                "ref_clip": good[0].get("ref_wav", ""),
                "segs": len(ids),
                "warn": "；".join(warns),
            })

    mutate_role(rid, _add)
    for x in results:
        if x.get("warn"):
            log(job, "  ⚠ 第 %d 句：%s" % (idx_line.get(x.get("index"), 0) + 1, x["warn"]))
    bad = [r for r in results if not r.get("ok")]
    if bad:
        # ★ 报段号，不能报句号：一句话被切成 8 段时，说"第 1 句没通过"等于什么都没说
        #   （实测就是这么误报的 —— 8 段都属于第 1 句，报"第 1 句"让人以为是整句）。
        b = bad[0]
        seg_no = (b.get("seg_no") or 0) + 1
        seg_cnt = b.get("seg_count") or 1
        where = ("第 %d 段（这一句共 %d 段）" % (seg_no, seg_cnt)) if seg_cnt > 1 else "这一句"
        raise Fail("%s 没通过出声体检：%s" % (where, b.get("error")))
    return results


# ------------------------------------------------ 自动歌词（不想再让用户自己去抄原词）
#
# 改词链路里「原歌词」是硬需求：YingMusic 靠它把新词对到原曲的旋律上。但以前这一步
# 得用户自己抄一遍 —— 现在点一下：分离人声 → 识别 → 按标点/时长折成句 → 填进界面。
#
# 为什么用 faster-whisper 而不是整合包自带的 ASR（tools/asr）：
#   ① 那个对**歌唱**的中文识别率更低；
#   ② 它只给「整首拼成一行」的文本，**没有时间戳** —— 没有时间戳就没法按时值分句，
#      而句数配不上时长正是"改完词后段唱乱"的根因（踩过）。
# 模型走 ModelScope（国内可直连），只下 1.5GB 一次。

WHISPER_SUBDIR = "faster-whisper-medium"
LYRIC_CACHE = os.path.join(DATA, "cache", "lyrics")


def whisper_dir():
    return os.path.join(HERE, "models", WHISPER_SUBDIR)


def whisper_ready():
    d = whisper_dir()
    return all(os.path.isfile(os.path.join(d, f))
               for f in ("model.bin", "config.json", "tokenizer.json"))


def ensure_whisper(job):
    if whisper_ready():
        return True
    log(job, "  第一次用「自动歌词」，要下语音识别模型（约 1.5 GB，走国内镜像），只下一次")
    rc, out = run(job, '"%s" -s "%s" --dir "%s" --size %s'
                  % (PY, os.path.join(STAGES, "dl_whisper.py"),
                     os.path.join(HERE, "models"), WHISPER_SUBDIR.split("-")[-1]),
                  timeout=7200)
    if rc != 0 or not whisper_ready():
        log(job, "  [warn] 识别模型没准备好：%s" % _tail(out, 3))
        return False
    return True


def recognize_lyrics(job, song_path, lang="zh", sep_model=None, use_cache=True):
    """自动识别一首歌的歌词。返回 {"line": "...", "segments": [...], ...}。

    失败抛 Fail —— 这个功能是用户主动点的，失败了要让他看见原因
    （而不是安静地返回空字符串，那样他会以为"这歌没词"）。
    """
    if not song_path or not os.path.isfile(song_path):
        raise Fail("歌曲文件不存在：%s" % song_path)
    if not ensure_whisper(job):
        raise Fail("语音识别模型没准备好。可以手动把 faster-whisper-medium 放到：\n  %s"
                   % whisper_dir())

    job["stage"] = "识别歌词"
    begin_step(job, "分离人声", "歌词得从人声里认；顺手复用翻唱那份分离缓存，不白跑。")
    vocal, _inst = separate_song(job, song_path, model=sep_model)
    end_step(job, True, "人声轨就绪")

    os.makedirs(LYRIC_CACHE, exist_ok=True)
    # 缓存键复用分离那套（路径+大小+mtime+模型），额外带上语种 ——
    # 换了语种要重认（上一次的结论对不上）。
    out_json = os.path.join(LYRIC_CACHE, _sep_cache_key(song_path, "lyrics|%s" % lang) + ".json")
    if use_cache and os.path.isfile(out_json):
        with contextlib.suppress(Exception):
            with open(out_json, encoding="utf-8") as f:
                d = json.load(f)
            log(job, "  复用上次的识别结果（%d 句）" % len(d.get("segments") or []))
            return d

    begin_step(job, "识别歌词（faster-whisper）",
               "按词级时间戳，把这首歌在唱什么折成一句一句 —— 直接填进改词台的「原歌词」栏。"
               "第一次要加载模型（十几秒），之后很快。")
    tmp = os.path.join(LYRIC_CACHE, "_asr_in.wav")
    rc, o1 = run(job, '"%s" -y -v error -i "%s" -ac 1 -ar 16000 "%s"'
                 % (FFMPEG, vocal, tmp), timeout=900)
    if rc != 0:
        end_step(job, False, "人声轨转码失败")
        raise Fail("人声轨转码失败：%s" % _tail(o1, 2))
    rc, out = run(job, '"%s" -s "%s" --wav "%s" --out "%s" --model "%s" --lang %s'
                  % (PY, os.path.join(STAGES, "lyrics_asr.py"), tmp, out_json,
                     whisper_dir(), lang), timeout=3600,
                  on_line=lambda ln: _pct_from_line(job, ln))
    with contextlib.suppress(Exception):
        os.remove(tmp)
    if rc != 0 or not os.path.isfile(out_json):
        end_step(job, False, "识别失败")
        raise Fail("歌词识别没跑通（退出码 %d）：\n%s" % (rc, _tail(out, 4)))
    with contextlib.suppress(Exception):
        with open(out_json, encoding="utf-8") as f:
            d = json.load(f)
    end_step(job, True, "识别出 %d 句" % len(d.get("segments") or []))
    return d


def lyrics_job(job, song_path, lang="zh", sep_model=None):
    """「自动歌词」的任务体：把识别结果挂到 job 上，前端完成后回填输入框。"""
    d = recognize_lyrics(job, song_path, lang=lang, sep_model=sep_model)
    job["lyric_result"] = {
        "line": d.get("line") or "",
        "segments": d.get("segments") or [],
        "duration": d.get("duration") or 0,
        "song": os.path.basename(song_path),
    }
    log(job, "  识别结果：%s" % ((d.get("line") or "")[:160]))
    return d


def rewrite_lyrics(job, vocal_wav, orig_lyric, new_lyric, out_wav, nfe=16):
    """
    改词：把人声轨**重新唱一遍** —— 旋律照原曲，词换成用户写的新词。

    为什么必须有这一步、为什么它只能待在链路的这个位置：
      RVC 是音色转换器，它输出唱的是什么词，100% 由喂进去的音频决定，
      给它任何文本参数都没用。所以「改词唱」必须在变声**之前**
      先把「唱着新词的干声」造出来。造出来之后，后面的变声和混音
      一行都不用改 —— 这正是把它插在这里的理由。

    歌词走**文件**传给子进程，不拼进命令行：中文、引号、换行直接进 Windows
    命令行会被转义搞坏，而且用户写的一个引号就能把命令截断（等于歌词能注入命令）。
    """
    if not os.path.isfile(YING_PY):
        raise Fail("改词要用的环境不在了：%s\n"
                   "改词依赖本机已部署的 YingMusic-Singer-Plus（D 盘那个独立目录）。"
                   % YING_PY)
    fd = tempfile.mkdtemp(prefix="lyric_")
    fo = os.path.join(fd, "orig.txt")
    fn = os.path.join(fd, "new.txt")
    # 留着不删：改词出问题时，用户/我能直接看当时真正送进去的是哪两段词。
    with open(fo, "w", encoding="utf-8") as f:
        f.write(orig_lyric or "")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(new_lyric or "")

    secs = wav_seconds(vocal_wav) or 0
    n = max(1, int(round(secs / LYRIC_CHUNK_SEC))) if secs else 4
    # 实测：加载模型约 50 秒（一次性），之后每块 28 秒音频约 90 秒。
    est = int(60 + n * 90)
    cmd = ('"%s" "%s" --vocal "%s" --orig-file "%s" --new-file "%s" '
           '--out "%s" --nfe %d --chunk %.0f'
           % (YING_PY, os.path.join(STAGES, "lyric_edit.py"),
              vocal_wav, fo, fn, out_wav, int(nfe), LYRIC_CHUNK_SEC))
    rc, out = run(job, cmd, timeout=5400,
                  on_line=lambda ln: _pct_from_line(job, ln),
                  heartbeat=(est, "改词中：%d 块，本机约 %d 分钟（估算，以结束为准）"
                             % (n, round(est / 60.0))))
    if rc != 0 or not os.path.exists(out_wav):
        raise Fail("改词失败（退出码 %d）。\n%s" % (rc, _tail(out, 6)))
    for ln in (out or "").splitlines():
        if "⚠" in ln or "完成：" in ln or "注意：" in ln:
            log(job, "  " + ln.strip())
    return out_wav


# ---------------------------------------------------------------- Seed-VC
#
# 为什么在 RVC 之外还要这一层（全是实测，不是听说）：
#   · RVC 那一步负责"把音高/音域对到角色身上"，输出是 40k 声码器的声音；
#     Seed-VC（44.1k DiT + BigVGAN）再过一遍，音色分数 0.7099 → **0.7674**（微调后）。
#   · ★ **44.1k 只是容器提升，不是带宽提升**：16k 以上频段的能量只有 0.012
#     （原唱是 0.25），有效带宽约 12k。它换来的是**音色**，不是更宽的频响 ——
#     界面上别把"44.1k"吹成"高频更好"。
#   · 微调（阿塔尼斯 191 段 / 16.1 分钟 / 1000 步）：**600 步是唯一「音色↑、
#     原唱残留↓、咬字↑」三项全胜的档位**；1000 步反而过拟合（咬字变差、亮度掉）。
#     ⇒ 默认 600 步。官方警告的"微调会升高 WER"在本例没发生。
#
# 三个函数：prepare（整理素材）/ finetune（微调）/ polish（唱歌时精修）。
# 引擎的**路径和档位清单**都在 seedvc.py 里（只读，不 import studio，避免循环）。

SEEDVC_MIN_SEGS = 5        # 硬门槛：再少真学不出音色
SEEDVC_MIN_SECS = 10.0


def seedvc_env(with_libs=True):
    """跑 Seed-VC 的环境。

    PYTHONPATH 挂两处：引擎根目录（它自己的模块）+ _svclibs（缺的依赖，
    装在这个隔离目录里，不动 RVC 本体）。torch 一律不装，用运行时的 2.7.1
    —— 50 系显卡只有 torch≥2.7 能跑，官方钉的 2.2 在这台机器上起不来。
    """
    if with_libs:
        return {"PYTHONPATH": seedvc.SEEDVC_ROOT + os.pathsep + seedvc.SEEDVC_LIBS}
    return {"PYTHONPATH": seedvc.SEEDVC_ROOT}


def seedvc_prepare(job, rid):
    """把角色素材整理成 Seed-VC 的训练集目录。

    只**新增/覆盖**、不删旧文件：切片文件名是稳定的，重跑一次自然就是
    "把新素材补进去"；整目录重建要批量删除，而误删素材是不可逆的。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    ok, why = seedvc.enabled()
    if not ok:
        raise Fail("Seed-VC 用不了：%s" % why)
    out = seedvc.dataset_dir(role)
    begin_step(job, "整理微调素材",
               "挑出时长 ≥2 秒、有声比例 ≥60% 的素材段，拷成 Seed-VC 的训练集。"
               "碎段和静音段只会教模型学噪声，所以要滤掉。")
    rc, txt = run(job, '"%s" -s "%s" prep --vs "%s" --role "%s" --out "%s"'
                  % (RVC_PY, os.path.join(STAGES, "seedvc_cli.py"), HERE, rid, out),
                  cwd=RVC_ROOT, timeout=1800)
    m = re.search(r"^PREP n=(\d+) secs=([\d.]+)", txt or "", re.M)
    if rc != 0 or not m:
        end_step(job, False, "素材整理失败")
        raise Fail("整理微调素材失败（退出码 %d）。\n%s" % (rc, _tail(txt, 4)))
    n, secs = int(m.group(1)), float(m.group(2))
    if n < SEEDVC_MIN_SEGS or secs < SEEDVC_MIN_SECS:
        end_step(job, False, "素材不够")
        raise Fail("符合要求的素材只有 %d 段 / %.1f 秒，太少（至少要 %d 段 / %.0f 秒）。\n"
                   "微调是拿角色的干声教模型「这个声音是谁」，素材太少只会学到噪声。"
                   % (n, secs, SEEDVC_MIN_SEGS, SEEDVC_MIN_SECS))
    end_step(job, True, "%d 段 / %.1f 分钟" % (n, secs / 60.0))
    log(job, "  训练集目录：%s" % out)

    def _set(r):
        sv = dict(r.get("seedvc") or {})
        sv.update({"dataset": seedvc.dataset_name(role), "run": seedvc.run_name(role),
                   "segs": n, "secs": secs, "at": _now()})
        r["seedvc"] = sv

    mutate_role(rid, _set)
    return {"segs": n, "secs": secs, "dir": out}


def seedvc_finetune(job, rid, steps=None):
    """用这个角色的干声去微调 Seed-VC（让它更会发这个人的声音）。

    每次都从**基础权重**重新微调（不是接着上次的微调再训）：实测那条路
    （1000 步一次跑完、每 200 步存一档）就是从基础权重开始的，中间档是为了
    挑步数用的；接着旧档训等于换个起点重来，没法复现 600 步那个拐点。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    steps = int(steps or seedvc.DEFAULT_STEPS)
    if steps not in seedvc.STEPS_CHOICES:
        steps = min(seedvc.STEPS_CHOICES, key=lambda c: abs(c - steps))
    info = seedvc_prepare(job, rid)

    # 每存一档约 2.2GB（DiT + 优化器状态），600 步默认存 3 档 ⇒ 按 8GB 留余量。
    step_check_disk(job, need_gb=8.0)

    job["stage"] = "微调"
    begin_step(job, "微调 %d 步" % steps,
               "拿上面那些素材教模型「这个人的声音长什么样」。本机实测 1000 步 "
               "7 分 43 秒（8GB 卡峰值 6.1GB），600 步通常五分钟内。\n"
               "每 200 步存一档，训完可以在下面挑档位 —— 步数不是越多越好，"
               "实测 600 步最好，1000 步会过拟合（咬字变糊）。")
    # 实测 0.46 秒/步（1000 步 7m43s，含约 1 分钟加载）
    est = int(steps * 0.6 + 120)

    def _track(line):
        # train.py 每 log_interval 步打一行：epoch 3, step 600, loss: 0.55
        m = re.search(r"step\s+(\d+)", line or "")
        if not m:
            return
        s = int(m.group(1))
        job["epochs_done"] = s
        job["epochs_total"] = steps
        pct = int(min(90, s * 100.0 / max(steps, 1)))
        if pct > (job.get("pct") or 0):
            job["pct"] = pct
        job["detail"] = "第 %d / %d 步" % (s, steps)
        job["stage"] = "微调 · 第 %d/%d 步" % (s, steps)

    cmd = ('"%s" train.py --config %s --pretrained-ckpt %s --dataset-dir "%s" '
           '--run-name %s --batch-size 1 --max-steps %d --max-epochs 10 '
           '--save-every %d --num-workers 0'
           % (seedvc.py_path(), seedvc.CONFIG, seedvc.BASE_CKPT, info["dir"],
              seedvc.run_name(role), steps, min(seedvc.SAVE_EVERY, steps)))
    rc, txt = run(job, cmd, cwd=seedvc.SEEDVC_ROOT, env=seedvc_env(True),
                  timeout=7200, on_line=_track,
                  heartbeat=(est, "微调中（本机约 0.5 秒/步，%d 步预计 %d 分钟）"
                             % (steps, round(est / 60.0))))
    if rc != 0:
        end_step(job, False, "微调失败")
        raise Fail("Seed-VC 微调失败（退出码 %d）。\n%s" % (rc, _tail(txt, 6)))
    rows = seedvc.ckpts(role)
    if not rows:
        end_step(job, False, "没找到权重")
        raise Fail("训练说跑完了，但 runs/%s 下面没找到权重文件。" % seedvc.run_name(role))
    # 默认挑 600 步（实测拐点）；没有就取最接近 600 的那一档
    pick = min(rows, key=lambda r: (abs(r["step"] - seedvc.DEFAULT_STEPS), r["final"]))
    for r in rows:
        log(job, "  档位 %s（%s，%.1f GB）" % (r["label"], r["at"], r["size"] / 1024.0 ** 3))

    def _set(r):
        sv = dict(r.get("seedvc") or {})
        sv.update({"ckpt": pick["path"], "step": pick["step"], "at": _now(),
                   "steps": steps})
        r["seedvc"] = sv

    mutate_role(rid, _set)
    end_step(job, True, "已出 %d 档，默认用 %s" % (len(rows), pick["label"]))
    return {"steps": steps, "ckpts": [r["label"] for r in rows], "selected": pick["label"]}


def seedvc_polish(job, rid, src_vocal, rvc_vocal, out_wav):
    """把 RVC 干声再过一遍 Seed-VC（44.1k），抬音色。返回成品路径。

    ★ source 用**交叉合成**的结果，不是直接拿 RVC 干声：
      直接 RVC 干声 → 糊（RVC 声码器把谐波做得过于规整，辅音和气声没了）；
      直接原唱     → 咬字清晰但原唱残留重。
      低频取 RVC（音色/共振峰）、高频取原唱（辅音/气声），两头都保住 ——
      这是实测比出来的，不是拍脑袋。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    ok, why = seedvc.enabled()
    if not ok:
        raise Fail("Seed-VC 用不了：%s" % why)
    ck = seedvc.selected(role)
    if not ck:
        raise Fail("这个角色还没有 Seed-VC 微调档位 —— 先去「训练」页跑一次微调。")
    ckpt = seedvc.ckpt_abs(ck["path"])
    if not os.path.isfile(ckpt):
        raise Fail("选中的档位文件已经不在了：%s\n可能是在「存储」里被清掉了，"
                   "重新微调或换一个档位。" % ckpt)
    final = os.path.dirname(out_wav)
    base, _ = os.path.splitext(out_wav)
    prompt = os.path.join(final, "seedvc_prompt.wav")
    cross_wav = base + "_cross.wav"
    outdir = base + "_sv"

    begin_step(job, "准备音色参考",
               "从这个角色的素材里拼一条长参考（硬顶 25 秒：whisper 编码器最多 "
               "3000 帧，超了会崩），推理时它决定「唱出来是谁的声音」。")
    rc, txt = run(job, '"%s" -s "%s" prompt --vs "%s" --role "%s" --out "%s" --max-sec 25'
                  % (RVC_PY, os.path.join(STAGES, "seedvc_cli.py"), HERE, rid, prompt),
                  cwd=RVC_ROOT, timeout=900)
    if rc != 0:
        end_step(job, False, "参考没拼出来")
        raise Fail("拼音色参考失败（退出码 %d）。\n%s" % (rc, _tail(txt, 4)))
    end_step(job, True, "音色参考已就绪")

    begin_step(job, "交叉合成",
               "低频取 RVC 干声（音色与共振峰），高频取原唱（辅音与气声）—— "
               "直接拿 RVC 干声当输入会糊，直接拿原唱当输入会带原唱味。")
    rc, txt = run(job, '"%s" -s "%s" cross "%s" "%s" "%s" 3200 1200'
                  % (RVC_PY, os.path.join(STAGES, "seedvc_cli.py"),
                     rvc_vocal, src_vocal, cross_wav), cwd=RVC_ROOT, timeout=1800)
    if rc != 0 or not os.path.isfile(cross_wav):
        end_step(job, False, "交叉合成失败")
        raise Fail("交叉合成失败（退出码 %d）。\n%s" % (rc, _tail(txt, 4)))
    end_step(job, True, "已合成待转换的干声")

    secs = wav_seconds(cross_wav) or 0
    # 实测 225 秒的歌 2 分 10 秒（RTF 0.37），加约 1 分钟加载模型
    est = int(secs * 0.6 + 90) if secs else 600
    begin_step(job, "音色精修（Seed-VC 44.1k）",
               "用 %s 档位把上面那条干声重唱一遍，输出 44.1kHz。本机约 %d 分钟。"
               % (ck["label"], round(est / 60.0)))
    cmd = ('"%s" inference.py --source "%s" --target "%s" --output "%s" '
           '--diffusion-steps %d --length-adjust 1.0 --inference-cfg-rate %s '
           '--f0-condition True --auto-f0-adjust False --semi-tone-shift 0 '
           '--checkpoint "%s" --config "%s" --fp16 True'
           % (seedvc.py_path(), cross_wav, prompt, outdir,
              seedvc.DIFFUSION_STEPS, seedvc.CFG_RATE, ckpt,
              os.path.join(seedvc.SEEDVC_ROOT, seedvc.config_path(role))))
    rc, txt = run(job, cmd, cwd=seedvc.SEEDVC_ROOT, env=seedvc_env(True),
                  timeout=3600, on_line=lambda ln: _pct_from_line(job, ln),
                  heartbeat=(est, "44.1k 重唱中（本机约 %d 分钟）" % round(est / 60.0)))
    wavs = sorted(glob.glob(os.path.join(outdir, "*.wav")))
    if rc != 0 or not wavs:
        end_step(job, False, "精修失败")
        raise Fail("Seed-VC 精修失败（退出码 %d）。\n%s" % (rc, _tail(txt, 6)))
    shutil.copyfile(wavs[0], out_wav)
    end_step(job, True, "已出 44.1k 精修人声")
    return out_wav


# ============================ 音高体检（整首开唱之前先量一小段） ============================
#
# ★ 这一段是「唱歌越唱越差」的根治点，来龙去脉必须写清楚，否则后人会把阈值当拍脑袋的数字。
#
# 【现象】老板的原话：「唱歌也是越唱越差」。盘上同一个角色（大帝 ra500fb92）从 10-13
#   到 10-16 一共 16 版成品。按成品记录里的质检数排成时间线看着像在退化
#   （前半均值 90.8% → 后半 83.0%），**但那是假的** —— 那 16 版其实是 7 首歌，
#   早期几首本来就「好唱」。必须按**同一首歌**才比得出来。
#
# 【控制变量对照】Three Days Grace - Pain，202.9 秒：
#   **同一个 --input**（cache/sep/18a93a7e6ae28125/vocal.wav）、同一个模型
#   ra500fb92.pth、同一套参数（index-rate 0.50 / rms-mix 0.25 / protect 0.33）、
#   同一段质检窗口（studio 的 _qs=(202.9-30)*0.35 ≈ 60.5s 起 30 秒）。
#   只改 --pitch：
#         pitch  +0  → 宽松吻合 93.0%  八度错  3.4%
#         pitch -12  → 宽松吻合 81.2%  八度错 17.2%     ← 整八度这一档炸了
#         pitch  +3  → 宽松吻合 96.8%  八度错  1.4%
#   所以**不是代码随时间退化**（同一首歌前后 6 小时，同模型同参数）。
#
# 【但不能推广成「−12 一定坏」】换个歌就翻过来了 —— 同样是 pitch −12：
#        丹阳子（a4db67eb80575a06，193s）  → 单窗  2.1% / 整曲  8.3%
#        登阶（b772c3d0cf5cf4d3，201s）    → 单窗 18.4% / 整曲 10.6%
#        登阶(2)（be13d29deea8523c，201s） → 单窗 17.2% / 整曲 11.5%
#   ⚠ **两个口径会给同一首歌相反的名次**：丹阳子 −12 单窗只 2.1%（看着最干净），
#   整曲却是 8.3%、最差一窗 37.5%（比登阶还糟）；登阶 −12 单窗 18.4%（看着最糟），
#   整曲只 10.6%。**单窗那个数只回答「会不会在某一段炸」，不回答「整首有多坏」**。
#   整曲真值一律用 qc_singing_whole 多窗算（出处 `_probe_whole_qc_truth.py`，可重跑）。
#   同一个角色、同一个音高设定，整曲从 8.3% 到 11.5%。**逐首不同，只能实测。**
#   —— 这正是要「体检」而不是「定规则」的理由。
#
# 【为什么不在推理入口直接修】八度错的来源在 RVC 自己的 pipeline 里（f0 量化成 256 档
#   coarse pitch 交给模型，模型在低音区/谐波强的段落会翻八度）。f0 是在 pipeline
#   内部提的，**我们的入口拿不到、也不该去 patch 整合包**（见 rvc_infer.py 文件头）。
#   ⇒ 能做、且立刻有效的是：**开唱前把这一档音高实测一遍**，坏了就换一档（或者至少说清楚）。
#
# 【成本】一次体检最多 4 条候选 × 30 秒 RVC + 4 次质检。相对「整首十几分钟唱完才发现坏了」，
#   这是把试错成本从十几分钟压到一两分钟（和「试听小样」同一个思路，只是自动做掉）。
#
# 【★ 体检素材为什么要"从三个位置各取一段拼起来"】—— 真跑把我第一版打掉了：
#   第一版按"全曲能量最高"挑一段 24 秒。实测在登阶上它挑到第 124~148 秒，
#   那一段的 −12 只有 **2.9%** 八度错 ⇒ 判"没问题"；而产品自己那段（59.7~89.7 秒）
#   是 **18.4%**。**漏判**。根因是八度错在**同一首歌内部**变化极大 —— 同一版输出按
#   30 秒窗口扫，从 3.5% 一路到 16.9%（完整七窗数据见 AUDITION_PARTS 旁的注释）。
#   ⇒ 换成"把整首按位置等分成三段、每段取一小段拼成一条素材"：开销一样（每个候选
#     还是只起一次 RVC），但取样覆盖整首，结论不再取决于"碰巧挑到哪一段"。
#   教训归到铁律里：**判据的取样方式本身就是判据的一部分，取样不代表性 = 判据不成立**。
AUDITION = True             # 整首开唱前先体检；关掉就回到"设什么唱什么"
AUDITION_SECS = 30.0        # 体检素材总长 = 几个片段拼起来
AUDITION_PARTS = 3          # 从**几个不同位置**各取一段拼成体检素材（必须 >= 2，理由见下）
#   ★ 为什么必须多段拼、而不是挑一段：八度错在**同一首歌内部**变化极大。实测登阶
#     那一版（201 秒整首输出）按 30 秒窗口**等距扫七窗**：
#         窗口起点   0s    28s    57s    85s   114s   142s   171s
#         八度错   12.7%   8.2%  14.7%  16.1%   3.5%   5.6%  16.9%   （整曲加权 10.6%）
#     —— 同一首歌里从 3.5% 变到 16.9%。第一版按"全曲能量最高"挑单段，正好挑到
#     124~148 秒（那一带只有 2.9%），把一档**实际会炸**的音高判成"没问题"（漏判，
#     真跑当场打脸）。等分取样把整首盖住，结论才不取决于"挑到哪一段"。
#     （这串数的出处：`_probe_whole_qc_truth.py`，可重跑复现。）
AUDITION_MIN_SONG = 75.0    # 人声短于这个就不体检 —— 本来就只有一段，直接唱
AUDITION_OCTAVE_BAD = 0.11  # 八度错超过这个比例 ⇒ 这一档音高算"不达标"
#   ★ 0.11 比的是**体检量出来的数**（不是产品单窗数，也不是整曲数，三个口径都不一样！）。
#     体检口径实测（三段等分取样，每档起一次 RVC + 质检；同一档的区间是多次真跑的范围）：
#        丹阳子   −24→2.9~3.5%   −12→3.2~3.3%   +0→1.1~1.4%    （三档都不坏）
#        登阶     −24→13.2~16.1% −12→7.6~8.3%   +0→4.0~4.6%    （−24 明显坏）
#     好档最大观测 8.3%、坏档最小观测 13.2% ⇒ 阈值取两者之间、偏向居中的 11%。
#   ★ 为什么不是 12%（原来就是 12）：**体检值自己会抖**。同一份素材、同一套取样点，
#     登阶 −24 三次真跑分别量出 16.1% / 15.0% / 13.2% —— 跨度近 3 个百分点。
#     12% 离最坏观测（13.2%）只剩 1.2 个百分点，随时可能把"明显坏的档"漏成"没问题"
#     （漏判 = 用户白唱十几分钟，正是本功能要防的事）。11% 给坏档留 2.2、
#     给好档留 2.7，两侧都站得住。
#   ★ 整曲多窗真值（studio.qc_singing_whole，`_probe_whole_qc_truth.py` 出）另给一套，
#     用来核对体检有没有把方向判反：
#        丹阳子 +3→1.7%   −12→8.3%;   登阶 −12→10.6%;   登阶(2) −12→11.5%
#     ⚠ 注意：整曲口径下"好/坏"两簇离得比单窗口径**近得多**（11.5% vs 16.1%），
#     所以阈值余量没那么宽裕 —— 不要再引用"坏的全 ≥17%、隔着一个数量级"那种话，
#     那是**单窗**口径的说辞，已作废（详见 verify_sing_pitch.py 文件头的口径警告）。
AUDITION_MATCH_DROP = 0.05  # 改判后的音准吻合**不许比原来低超过 5 个百分点**
#   为什么除了八度错还要卡这一条：否则可能挑到"不翻八度、但整体唱得明显更差"的一档
#   （八度错是"翻八度"的专项指标，不代表整体音准好）。
AUDITION_BETTER = 0.6       # 设定档达标、但对照档八度错不到它的这个比例 ⇒ 值得提示
#   ★ 为什么要这条"软提示"：硬闸（0.12）只拦"明显坏"。实测登阶设定 −12 时体检量到
#     8.0%，没过 12% 的闸 ⇒ 不改判；可同一张表里 +0 只有 4.0%（好一倍），用户
#     永远看不到。软提示**不改音高**（那是用户的艺术决定），只在理由文字里点一句
#     "旁边那档更好，想更稳可以换它重唱对比"。
AUDITION_MAX_ABS = 24       # 候选音高的绝对值上限（再远就不合常理了）


def _rms_blocks(path, block_sec=1.0, max_blocks=1200):
    """按秒算粗略能量包络（标准库，不依赖 numpy）。

    为什么不用 wav_rms：它把**整个文件**读进内存，200 秒立体声要 70MB 字节；
    这里只按块累加，读一块丢一块。
    返回 [(块起点秒, 块内平均绝对值)]，读不了就返回 []。
    """
    import array

    try:
        w = wave.open(path, "r")
    except Exception:
        return []
    try:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        if sw != 2 or ch < 1:       # 只处理 16-bit PCM；其余交给调用方降级
            return []
        per = max(1, int(fr * block_sec))
        out = []
        idx = 0
        while len(out) < max_blocks:
            raw = w.readframes(per)
            if not raw:
                break
            a = array.array("h")
            try:
                a.frombytes(raw)
            except Exception:
                break
            if not a:
                break
            acc = 0
            step = max(1, len(a) // 4000)
            cnt = 0
            for i in range(0, len(a), step):
                acc += abs(a[i])
                cnt += 1
            out.append((idx * block_sec, (acc / max(cnt, 1)) / 32768.0))
            idx += 1
    finally:
        with contextlib.suppress(Exception):
            w.close()
    return out


def _pick_excerpt(path, secs, block_sec=1.0):
    """在整首里挑一段「最能代表这一段区间」的片段：给定区间内能量最高的连续窗口。

    ★ 为什么按能量挑、而不是像试听小样那样固定取 35%：高能量处通常是副歌，音高最
      明确、也最容易被听出来；静音/留白段量不出音准。
    返回 [(起点秒, 该窗口平均能量)]，整首返回一个元素；估不出来返回 []。
    """
    blocks = _rms_blocks(path, block_sec)
    if not blocks:
        return []
    n = len(blocks)
    nb = max(1, int(round(secs / block_sec)))
    if n <= nb:
        return [(0.0, sum(e for _, e in blocks) / max(1, n))]
    best, best_e = 0, -1.0
    for s in range(0, n - nb + 1):
        e = sum(v for _, v in blocks[s:s + nb]) / float(nb)
        if e > best_e:
            best, best_e = s, e
    return [(best * block_sec, best_e)]


def _montage_starts(path, total_secs, parts):
    """把整首按位置等分成 parts 个区，每个区里挑能量最高的一小段 —— 返回起点秒列表。

    ★ 这是"一段代表不了整首"的解法（踩坑记录见 AUDITION_PARTS 的注释）：
      等分保证**每一段歌曲都被看到**，而不是被"全曲能量最高处"一个点代表。
      每个区内再挑能量最高处，是为了避开区内的留白/气口 —— 那里量不出音准。
    """
    blocks = _rms_blocks(path, 1.0)
    if not blocks:
        return []
    n = len(blocks)
    part_secs = total_secs / float(parts)
    nb = max(1, int(round(part_secs)))
    lo, hi = int(n * 0.05), int(n * 0.95)
    if hi - lo < nb * parts:                 # 太短，退回"整首挑一段"
        p = _pick_excerpt(path, total_secs)
        return [s for s, _ in p]
    zone = (hi - lo) / float(parts)
    starts = []
    for k in range(parts):
        z0 = int(lo + k * zone)
        z1 = int(min(hi, lo + (k + 1) * zone))
        last = max(z0, z1 - nb)
        best, best_e = z0, -1.0
        for s in range(z0, last + 1):
            e = sum(v for _, v in blocks[s:s + nb]) / float(nb)
            if e > best_e:
                best, best_e = s, e
        starts.append(float(best))
    return starts


def _montage(vocal_wav, out_wav, total_secs, parts, fade_ms=20.0):
    """把等分取样出来的几段拼成**一条**体检素材（带 20ms 淡入淡出，接缝不出咔哒）。

    为什么拼成一条、而不是分别跑：每个候选音高只起一次 RVC、一次质检，
    取样覆盖了整首，而开销和"只跑一段"一样。
    只用标准库（wave+array）：切片素材/分离出来的人声都是 16-bit PCM。
    返回 (起点秒列表, 说明)；读不了就返回 ([], 原因)，由调用方退化成单段裁剪。
    """
    import array

    starts = _montage_starts(vocal_wav, total_secs, parts)
    if not starts:
        return [], "读不了能量包络（不是 16-bit PCM？）"
    part_secs = total_secs / float(parts)
    try:
        w = wave.open(vocal_wav, "r")
    except Exception as e:                       # noqa: BLE001
        return [], "打不开干声：%s" % e
    try:
        ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if sw != 2 or ch < 1:
            return [], "不是 16-bit PCM（sw=%d ch=%d）" % (sw, ch)
        nf = max(1, int(sr * fade_ms / 1000.0))
        with wave.open(out_wav, "w") as o:
            o.setnchannels(ch)
            o.setsampwidth(2)
            o.setframerate(sr)
            for s0 in starts:
                w.setpos(int(s0 * sr))
                raw = w.readframes(int(part_secs * sr))
                a = array.array("h")
                a.frombytes(raw)
                n = len(a) // ch
                k = min(nf, n // 2)
                for i in range(k):               # 淡入 / 淡出，接缝不咔哒
                    g = i / float(k)
                    for c in range(ch):
                        a[i * ch + c] = int(a[i * ch + c] * g)
                        j = (n - 1 - i) * ch + c
                        a[j] = int(a[j] * g)
                o.writeframes(a.tobytes())
    finally:
        with contextlib.suppress(Exception):
            w.close()
    return starts, "取 %d 段 × %.0f 秒" % (len(starts), part_secs)


def pick_audition(cands, requested):
    """纯逻辑：从体检结果里决定**用哪一档音高整首唱**。返回 (音高, 是否改判, 理由)。

    cands 是 [{"pitch": int, "match": float|None, "octave": float|None}, ...]，
    match = 宽松吻合率(0~1)，octave = 八度错比例(0~1)；没跑通的那档是 None。

    ★ 为什么抽成纯函数：这是整段功能里唯一有"判断"的地方，抽出来才能用构造数据
      在门禁里把每条分支逐条走一遍（真跑一遍 RVC 要几十秒，走不了几条分支）。
    ★ 规则故意只有两步：
      ① 用户设的那档**达标**（八度错 ≤ AUDITION_OCTAVE_BAD）⇒ 原样用，不改判。
      ② 不达标 ⇒ 在"达标"的对照档里挑**离用户设定最近**的那一档；一个达标的都没有，
         就维持原设定，并把最好的一档报出来让用户自己决定。
    ★ 为什么"最近"而不是"最好"：用户设 −12 是想把声音压低。候选里 −12 炸了、
      0 和 +3 都好，那 0 离他的意图更近（就是 −12 的正上方一个八度），+3 更远。
      按"八度错最低"去挑会挑到 +3，等于把用户想要的"压低"改成"升调" ——
      那是替用户做艺术决定，不该由我们做。
      （实测 Pain 单窗：−12 是 17.2%、0 是 3.4%、+3 是 1.4%。按"最好"会挑 +3。）
    ★ 为什么不抛异常、什么情况都返回一个音高：体检是锦上添花，任何一档没量出来
      都必须能退化成"按用户设的唱"，绝不能让唱歌失败。
    """
    if not cands:
        return int(requested), False, "没有拿到任何体检数据，按你设的 %+d 唱" % int(requested)
    by = {}
    for c in cands:
        with contextlib.suppress(Exception):
            by[int(c.get("pitch"))] = c
    base = by.get(int(requested))
    if base is None or base.get("octave") is None:
        return int(requested), False, "你设的 %+d 这一档没量出来，按它唱" % int(requested)
    bo = float(base["octave"])
    bm = float(base.get("match") or 0.0)
    if bo <= AUDITION_OCTAVE_BAD:
        # 设定档达标 ⇒ **一定不改判**（用户设 −12 是想压低，替他把音高抬起来是越权）。
        # 但若旁边有档明显更稳（八度错不到它 AUDITION_BETTER 倍），值得点出来 ——
        # 实测登阶：设定 −12 体检 8.0%（达标），可同一张表里 +0 只有 4.0%。
        # 不提示的话用户永远看不到那一档。只写进理由文字，音高照旧按他设的。
        better = []
        for c in cands:
            if c.get("octave") is None:
                continue
            try:
                p, o = int(c.get("pitch")), float(c["octave"])
            except (TypeError, ValueError):
                continue
            if p == int(requested) or abs(p) > AUDITION_MAX_ABS:
                continue
            if o > bo * AUDITION_BETTER:
                continue
            if c.get("match") is not None and float(c["match"]) < bm - AUDITION_MATCH_DROP:
                continue
            better.append((abs(p - int(requested)), p, o))
        if better:
            better.sort(key=lambda x: (x[0], x[2]))
            _, bp2, bo2 = better[0]
            return int(requested), False, (
                "八度错 %.1f%%（阈值 %.0f%%）—— 这一档没问题；不过 %+d 只有 %.1f%%，"
                "想更稳可以换它重唱一版对比（没替你改，仍按你设的 %+d 唱）"
                % (bo * 100, AUDITION_OCTAVE_BAD * 100, bp2, bo2 * 100, int(requested)))
        return int(requested), False, (
            "八度错 %.1f%%（阈值 %.0f%%）—— 这一档没问题" % (bo * 100, AUDITION_OCTAVE_BAD * 100))
    pool = []
    for c in cands:
        if c.get("octave") is None:
            continue
        try:
            p, o = int(c.get("pitch")), float(c["octave"])
        except (TypeError, ValueError):
            continue
        if p == int(requested) or abs(p) > AUDITION_MAX_ABS or o > AUDITION_OCTAVE_BAD:
            continue
        # 吻合不许比原来差太多（原来没量出来就不卡这条）
        if c.get("match") is not None and float(c["match"]) < bm - AUDITION_MATCH_DROP:
            continue
        pool.append(c)
    if not pool:
        others = [c for c in cands
                  if c.get("octave") is not None and int(c.get("pitch")) != int(requested)]
        if others:
            b = min(others, key=lambda c: float(c["octave"]))
            return int(requested), False, (
                "你设的 %+d 八度错 %.1f%% 偏高，但对照档都没达标（最好的是 %+d：%.1f%%）"
                " ⇒ 仍按 %+d 唱，你可以自己换一档再试"
                % (int(requested), bo * 100, int(b["pitch"]), float(b["octave"]) * 100,
                   int(requested)))
        return int(requested), False, (
            "你设的 %+d 八度错 %.1f%% 偏高，但没有可用的对照档" % (int(requested), bo * 100))
    best = sorted(pool, key=lambda c: (abs(int(c["pitch"]) - int(requested)),
                                       float(c["octave"]),
                                       -float(c.get("match") or 0.0)))[0]
    bp = int(best["pitch"])
    return bp, True, (
        "你设的 %+d 有 %.1f%% 的帧翻八度（不达标）；离它最近且达标的 %+d 只有 %.1f%%，"
        "吻合 %.1f%% → %.1f%% ⇒ 已改用 %+d"
        % (int(requested), bo * 100, bp, float(best["octave"]) * 100,
           bm * 100, float(best.get("match") or 0.0) * 100, bp))


def _audition_candidates(requested):
    """要试哪几档音高：你设的那档 + 相邻八度(±12) + 0，去重、限幅。"""
    out = [int(requested)]
    for p in (int(requested) - 12, int(requested) + 12, 0):
        if abs(p) <= AUDITION_MAX_ABS and p not in out:
            out.append(p)
    return out


def audition_pitch(job, vocal_wav, requested, rvc_model, idx, index_rate,
                   rms_mix_rate=None, protect=None):
    """开唱前的音高体检：拿一小段真唱几档音高，量出来再决定整首用哪档。

    返回 dict（给日志/产物用）：
      {"start","secs","cands":[{pitch,match,octave}],"chosen","changed","reason","skipped"}
    任何一步不通都不抛异常 —— 体检失败最多退化成"按你设的唱"，不能把唱歌带死。
    """
    res = {"start": 0.0, "starts": [], "secs": 0.0, "montage": "", "cands": [],
           "chosen": int(requested), "changed": False, "reason": "未体检", "skipped": None}
    try:
        total = wav_seconds(vocal_wav) or 0.0
        if not AUDITION:
            res["skipped"] = "体检已关闭"
            return res
        if total < AUDITION_MIN_SONG:
            res["skipped"] = ("人声只有 %.0f 秒（< %.0f 秒），直接唱不体检"
                              % (total, AUDITION_MIN_SONG))
            return res
        secs = min(AUDITION_SECS, total)
        parts = max(1, int(AUDITION_PARTS))
        while parts > 1 and secs / parts < 4.0:     # 每段至少 4 秒才有意义
            parts -= 1
        work = os.path.join((job or {}).get("dir") or DATA, "_audition")
        os.makedirs(work, exist_ok=True)
        clip = os.path.join(work, "probe.wav")
        starts, why = _montage(vocal_wav, clip, secs, parts)
        if not starts:
            # 退化：拼不了就退回"裁一段"（旧路）。体检宁可弱一点，也不能整个不跑。
            pick = _pick_excerpt(vocal_wav, secs)
            s0 = pick[0][0] if pick else 0.0
            s0 = max(0.0, min(s0, max(0.0, total - secs)))
            ok, _ = clip_audio(job, vocal_wav, clip, s0, secs)
            if not ok:
                res["skipped"] = "体检素材裁剪失败"
                return res
            starts, why = [s0], "退化成单段（%s）" % why
        res["starts"] = [round(x, 1) for x in starts]
        res["start"] = res["starts"][0]
        res["secs"] = secs
        res["montage"] = why

        cands = _audition_candidates(requested)
        for p in cands:
            c_out = os.path.join(work, "cand_%+d.wav" % p)
            cmd = ('"%s" -s "%s" --model "%s" --input "%s" --output "%s" '
                   '--f0 %s --pitch %d --index-rate %s %s --index "%s"'
                   % (RVC_PY, os.path.join(STAGES, "rvc_infer.py"), rvc_model,
                      clip, c_out, RVC_F0_METHOD, int(p), index_rate,
                      _rvc_quality_args(rms_mix_rate, protect), idx or ""))
            rc, _out = run(job, cmd, cwd=RVC_ROOT, timeout=1800)
            row = {"pitch": int(p), "match": None, "octave": None}
            if rc != 0 or not os.path.exists(c_out):
                log(job, "    体检 %+d 半音：推理没跑通（退出码 %d）" % (p, rc))
                res["cands"].append(row)
                continue
            # ★ 质检要拿**裁好的片段**（clip）跟候选产物比，不能再传 start/secs：
            #   候选产物**本身就是那一段**（24 秒），而 qc_cli 会把 start/secs 同时套到
            #   两条轨上 —— 再套一次 33~57 秒，切出来是空的，qc_cli 直接报
            #   「音频太短（0.00 秒）」退出码 1（实测踩到）。
            #   两条轨都是同一段、时间轴都从 0 开始，所以整段比就是对的。
            d = qc_singing(job, clip, c_out)
            if d:
                row["match"] = d.get("match_ratio_loose")
                row["octave"] = d.get("octave_err_ratio")
                row["contour"] = d.get("contour_corr")
            res["cands"].append(row)
            log(job, "    体检 %+d 半音：八度错 %s，宽松吻合 %s"
                % (p,
                   ("%.1f%%" % (row["octave"] * 100)) if row["octave"] is not None else "-",
                   ("%.1f%%" % (row["match"] * 100)) if row["match"] is not None else "-"))
        chosen, changed, why = pick_audition(res["cands"], requested)
        res.update({"chosen": chosen, "changed": changed, "reason": why})
        return res
    except Exception as e:      # 体检是"锦上添花"，绝不能把唱歌带死
        res["skipped"] = "体检异常（已忽略，按你设的唱）：%s" % e
        return res


def sing_role(job, rid, song_path, pitch=0, index_rate=None, model=None, mix=True,
              sep_model=None, rms_mix_rate=None, protect=None, prefix="sing",
              save=True, gate=True, pre_sep=None, output_kind="singing", label=None,
              qc=True, lyric=None, nonvocal="keep", engine="rvc"):
    """
    让角色唱歌：**先分离人声** -> RVC 只对干声变声 -> 用伴奏轨混音。

    ★ 为什么必须先分离（「唱出来没法听」的根治点，不是调参能救的）：
      RVC 是「人声 → 人声」的模型，靠 f0（基频）曲线工作。把整首歌直接喂进去，
      鼓和贝斯也会被它当成"唱"，输出的音高跟着鼓点乱跑；更要命的是混音时
      如果拿**原曲**当伴奏，原唱人声会和角色一起唱 —— 实测成品波形与原曲的
      相关系数 0.870、原曲振幅占比 0.538，也就是成品里一大半是原唱。
      正确顺序：原曲 →(UVR5)→ 人声轨 + 伴奏轨；RVC 只对**人声轨**变声；
      再用**伴奏轨**（不含原唱）混合。

    角色自己没训过 RVC 模型时，不会偷偷拿别人的模型冒充 —— 会直接停下来说明原因，
    因为「用了别人的音色」这种事必须让用户知道。

    pitch 可以是整数半音，也可以是 "auto"（按原唱与角色的音域差自动估计）。
    rms_mix_rate / protect 是 RVC 的音质参数，见 _rvc_quality_args() 的说明。

    输出两条轨，分开存：
      voice —— 变声后的人声（干声），后续还能重新编曲/混音
      mix   —— 伴奏 + 角色人声的成品，直接能听能发
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    if index_rate is None:
        index_rate = RVC_INDEX_RATE
    index_rate = float(index_rate)
    rvc_model = model or (role.get("rvc") or {}).get("model")
    if not rvc_model:
        raise Fail(
            "这个角色还没有歌声模型，没法唱歌。\n"
            "歌声模型（RVC）和说话模型（GPT-SoVITS）是两套东西：说话模型只能照着文本念，"
            "唱不出来。需要先用这个角色的干声素材去训一个 RVC 模型。")
    # ★ 没底模训出来的模型"不会跟调"，这是"唱出来没法听"最常见的根因。
    #   在这里明说，用户才不会把它当成自己素材的问题（那样他会白忙很久）。
    if (role.get("rvc") or {}).get("pretrained") is False:
        log(job, "  [注意] 这个歌声模型是**没有底模、从零训**的：它学不会跟着旋律唱，"
                 "输出会卡在角色自己的音区、变调也可能完全没反应。"
                 "如果成品听着不对，优先用底模重训一次（不是素材的问题）。")
    d = role_dir(rid)
    final = os.path.join(d, "final")
    os.makedirs(final, exist_ok=True)
    # prefix == "preview" 时用固定名（preview_latest）：小样只保留一份，
    # 每试听一次就覆盖上一条，不会在成品清单里堆出一排样片。
    stamp = "latest" if prefix == "preview" else time.strftime("%m%d_%H%M%S")
    tag = "%s_%s" % (prefix, stamp)
    out_wav = os.path.join(final, "voice_%s.wav" % tag)

    try:
        song_secs = wav_seconds(song_path)
    except Exception:
        song_secs = 0

    # ---- 第 0 步：分离人声 / 伴奏（见 separate_song 的说明，这步不能省）----
    if pre_sep:
        # 试听小样那条路已经分离过了，这里不重复跑（分离是这一步里最贵的）。
        vocal_wav, inst_wav = pre_sep
        log(job, "  用已经分离好的轨，跳过分离")
    else:
        job["stage"] = "分离人声"
        begin_step(job, "分离人声与伴奏",
                   "把原曲拆成「人声轨」和「伴奏轨」。变声只能对着人声做 —— "
                   "对着整首歌做，鼓和贝斯的音高也会被当成「唱」，输出跟着节奏乱跑；"
                   "混音也只能用伴奏，否则原唱会跟着角色一起唱。")
        vocal_wav, inst_wav = separate_song(job, song_path, model=sep_model)
        end_step(job, True, "人声轨 + 伴奏轨已就绪")

    # ---- 改词（可选）：把人声轨换成「唱新词」的版本，旋律不变 ----
    # 放在这里、而不是变声之后，是因为 RVC 改不了词（见 rewrite_lyrics 的说明）。
    # 换掉 vocal_wav 之后，下面的变声 / 混音 / 质检全都不用改一行 ——
    # 它们本来就只认 vocal_wav 这个名字。
    if lyric:
        orig_lyric, new_lyric = lyric
        job["stage"] = "改词"
        begin_step(job, "改写歌词（保留原曲旋律）",
                   "先把人声重新唱一遍：旋律照原曲，词换成你写的新词。"
                   "长歌会切成约 %d 秒一块逐块生成，每块都会对齐回原来的长度，"
                   "免得人声和伴奏错开。这一步比变声慢，是整个链路里第二耗时的一段。"
                   % int(LYRIC_CHUNK_SEC))
        vocal_wav = rewrite_lyrics(job, vocal_wav, orig_lyric, new_lyric,
                                   os.path.join(final, "lyric_%s.wav" % tag))
        end_step(job, True, "已唱出新词的人声轨")

    # ---- 音高：可以是固定半音，也可以是 "auto" ----
    if isinstance(pitch, str) and pitch.strip().lower() == "auto":
        job["stage"] = "估音高"
        begin_step(job, "估算音高（自动）",
                   "比对原唱人声与角色音域的基频中位数，算该升/降几个半音。"
                   "男声唱女声原曲通常要 -12，女声唱男声原曲通常要 +12。")
        p, why = estimate_pitch(job, vocal_wav, role)
        pitch = p
        end_step(job, True, "自动音高 %+d 半音（%s）" % (p, why))
        log(job, "  自动音高：%+d 半音 —— %s" % (p, why))
    else:
        pitch = int(pitch)

    # 特征索引库缺失时不能用 index-rate>0（RVC 会直接报错退出）。
    # 自训模型会带索引；用户从别处拿来的模型通常不带 —— 这里自动降级，
    # 少一点音色细节，但不会整首歌失败。
    #
    # 判断必须走 _find_rvc_index()（= RVC 自己的匹配规则），不能自己拼
    # `<stem>.index`：索引真名带 IVF 参数且模型名带 _eN_sN 尾巴，拼不出来。
    idx = _find_rvc_index(rvc_model)
    if index_rate > 0 and not idx:
        log(job, "  这个模型没有特征索引库，改用 index-rate 0 演唱")
        index_rate = 0
    elif index_rate > 0:
        log(job, "  用特征索引库：%s" % os.path.basename(idx))

    # ---- 音高体检：整首开唱**之前**先拿一小段实测几档音高 ----
    # ★ 记住"用户原本设的是哪个"：体检可能改判，产物里要能看出改判前后的差别。
    _pitch_asked = int(pitch)
    # 为什么值得多花这一两分钟：整首 RVC 是本链路最贵的一步（本机 3 分 45 秒的歌十几分钟）。
    # 而同一首歌、同模型同参数、只改音高，八度错能从 3.4% 蹦到 17.2%（见文件头对照表）；
    # 换个歌又翻回来（丹阳子 −12 单窗只有 2.1%、整曲却有 8.3%）。既然逐首不同、
    # 又只能靠实测知道，那就把它量在小段上 —— 唱完十几分钟才发现音高坏了，代价太高。
    audition = None
    if AUDITION and (wav_seconds(vocal_wav) or 0.0) >= AUDITION_MIN_SONG:
        job["stage"] = "音高体检"
        begin_step(job, "音高体检（先试唱一小段）",
                   "整首开唱前，先从**整首歌的三个不同位置**各取一小段拼成约 %.0f 秒，"
                   "把「你设的 %+d 半音」和「相邻八度」各试唱一遍，量八度错和音准吻合。"
                   "整八度是听感差别最大的一档，而八度错在同一首歌内部能从 3%% 变到 20%%，"
                   "先量出来，免得整首白唱。"
                   % (AUDITION_SECS, int(pitch)))
        audition = audition_pitch(job, vocal_wav, pitch, rvc_model, idx, index_rate,
                                 rms_mix_rate=rms_mix_rate, protect=protect)
        if audition.get("skipped"):
            end_step(job, True, "没体检（%s）" % audition["skipped"])
            log(job, "  音高体检跳过：%s" % audition["skipped"])
        else:
            log(job, "  音高体检（%s；取样自第 %s 秒；全曲 %.0f 秒）："
                % (audition.get("montage") or "-",
                   "、".join("%.0f" % x for x in (audition.get("starts") or [])),
                   wav_seconds(vocal_wav) or 0))
            for c in audition["cands"]:
                log(job, "    %+3d 半音：八度错 %s，宽松吻合 %s"
                    % (c["pitch"],
                       ("%5.1f%%" % (c["octave"] * 100)) if c.get("octave") is not None else "  -  ",
                       ("%5.1f%%" % (c["match"] * 100)) if c.get("match") is not None else "  -  "))
            log(job, "    ⇒ %s" % audition["reason"])
            end_step(job, True, audition["reason"])
            if audition["changed"]:
                # ★ 改判**必须响亮**：用户在界面上设的是 A，我们按 B 唱了 ——
                #   悄悄改等于骗他。所以日志、步骤条、产物参数三处都要留下。
                log(job, "  [注意] 你设的是 %+d 半音，体检后**改用 %+d 半音**整首演唱。"
                         "想坚持原设定，把音高改回来再唱一次即可。" % (int(pitch), audition["chosen"]))
                pitch = int(audition["chosen"])

    # ★ 这一步**必须**有步骤条：RVC 的推理整条链里没有任何 tqdm，
    #   只有开头几行 print，所以"进度百分比"根本无从谈起。
    #   能做的、也最有用的是：把"现在做到哪一步"明确摆出来，并把**用时**顶上去
    #   ——用户要回答的其实是"还在跑吗、跑到哪了"。
    voice_secs = wav_seconds(vocal_wav) or song_secs
    job["stage"] = "歌声转换"
    begin_step(job, "歌声转换（RVC）",
               "按这个角色的音色重唱（输入是分离好的人声轨%s，音高 %+d 半音）。"
               "这一步没有细粒度百分比，看下面的「已用」和日志；%s"
               % (("，%.0f 秒" % voice_secs) if voice_secs else "", pitch,
                  "本机 3 分 45 秒的歌实测要十几分钟，显卡被占满是正常的"))
    # on_line：RVC 会打「当前设备/选择模型/选择索引」这类行 —— 有百分比就顺手用，
    # 没有也不影响（进度条不动，但日志在动、步骤名在，用户就知道没卡死）。
    # heartbeat：只按**人声轨**的长度估（间奏的静音段 RVC 很快就过），
    # 免得进度条在干等时一动不动。
    est = int(voice_secs * 5.0 + 60) if voice_secs else 900
    hb = (est, "人声转换中（按本机速率估算，约 %d 分钟；实际以结束为准）" % round(est / 60.0))

    def _rvc(input_wav, ir):
        # ★ 走自己的入口（stages/rvc_infer.py）而不是整合包自带的 `infer.cli`：
        #   ① CLI 的 --f0-method 只放开 pm / rmvpe，**FCPE 被挡在外面**；
        #      而实测 FCPE 把逐帧音高偏差从 29 音分收紧到 23（见文件头的参数表）。
        #   ② 需要显式指定 index，才能在"索引真不可用"时明确降级、说清原因。
        #   语义与 CLI 一致：输出已存在就覆盖 —— 小样用的是固定名 preview_latest，
        #   不覆盖的话第二次试听必然失败（CLI 原版遇到已存在文件是直接退出码 1）。
        c = ('"%s" -s "%s" --model "%s" --input "%s" --output "%s" '
             '--f0 %s --pitch %d --index-rate %s %s --index "%s"'
             % (RVC_PY, os.path.join(STAGES, "rvc_infer.py"), rvc_model,
                input_wav, out_wav, RVC_F0_METHOD, int(pitch), ir,
                _rvc_quality_args(rms_mix_rate, protect), idx or ""))
        return run(job, c, cwd=RVC_ROOT, timeout=3600,
                   on_line=lambda ln: _pct_from_line(job, ln), heartbeat=hb)

    rc, out = _rvc(vocal_wav, index_rate)
    # 兜底：索引命名规则不一致导致仍报 index 错误时，去掉索引再试一次
    if rc != 0 and index_rate > 0 and "added index" in (out or "").lower():
        log(job, "  索引仍然不可用，去掉索引重试一次")
        rc, out = _rvc(vocal_wav, 0)
    if rc != 0 or not os.path.exists(out_wav):
        end_step(job, False, "歌声转换失败")
        raise Fail("歌声转换失败（退出码 %d）。\n%s" % (rc, _tail(out, 4)))
    end_step(job, True, "已生成变声人声")

    # ---- Seed-VC 精修（可选）：44.1k 再过一遍，抬音色 ----
    # 放在**口哨保护之前**：保护那一步会把"不是唱"的段换回原声，先精修再保护，
    # 保护换回去的那几段才不会被 Seed-VC 又唱一遍（那就白保护了）。
    if engine == "seedvc":
        job["stage"] = "音色精修"
        sv_out = os.path.join(final, "voice_sv_%s.wav" % tag)
        try:
            out_wav = seedvc_polish(job, rid, vocal_wav, out_wav, sv_out)
        except Fail as e:
            # 精修失败不该把整首歌赔进去：RVC 那条干声本身是好的，降级继续。
            # 但**必须写进日志和步骤条** —— 悄悄降级等于骗用户"这就是精修版"。
            log(job, "  [warn] 音色精修没做成，继续用 RVC 干声出成品：%s" % e)
            end_step(job, False, "精修失败，已改用 RVC 干声（见日志）")

    # ---- 非歌声段保护：口哨 / 纯音段换回原始人声 ----
    # 为什么放在变声之后、混音之前：判据只有拿到 f0 和谱特征才能算（要起 torch），
    # 而"换回原声"必须对**已经变过声**的那条轨做 —— 否则会把没变的整段都换掉。
    voice_wav = out_wav
    guard_info = None
    if nonvocal != "off":
        job["stage"] = "非歌声段保护"
        begin_step(job, "非歌声段保护（口哨 / 纯音）",
                   "口哨、纯音这类**不是唱**的声音，RVC 会当成「唱」用角色嗓音再发一遍："
                   "实测男声角色会把 662Hz 的口哨降成 220Hz 的持续音 —— 那就是「吼」。"
                   "这一步把它们判出来，在那几段换回原始人声（口哨不体现角色音色，"
                   "翻唱里保留原版是常规做法）。")
        g_out = os.path.join(final, "voiceg_%s.wav" % tag)
        voice_wav, guard_info = guard_nonvocal(job, vocal_wav, out_wav, g_out, nonvocal)
        if guard_info and guard_info.get("protected_secs"):
            end_step(job, True, "已保护 %.1f 秒非歌声段（%d 段，模式 %s）"
                     % (guard_info.get("protected_secs", 0),
                        len(guard_info.get("segments") or []), nonvocal))
        else:
            end_step(job, True, "这首歌里没检测到口哨/纯音段")

    tracks = [{"label": "人声（干声）", "wav": voice_wav}]
    # 先占位：mix=False 时下面不会赋值，而写产物清单时要读它（否则 NameError）
    mix_wav = None

    # 成品轨：**伴奏轨** + 变声人声。
    # ★ 这里绝对不能用原曲 —— 原曲里带着原唱，混出来就是「原唱 + 角色」
    #   两个人在唱（实测相关系数 0.870）。这正是之前"没法听"的主因。
    # 用自带的混音脚本而不是 ffmpeg —— 整合包的 ffmpeg 是老版，
    # amix 不支持 normalize 选项会直接报错。
    if mix:
        mix_wav = os.path.join(final, "mix_%s.wav" % tag)
        job["stage"] = "混合成品"
        begin_step(job, "混合成品",
                   "伴奏轨 + 角色人声，自动对齐响度、把残留底噪压掉，"
                   "出可直接听/发的成品")
        rc2, out2 = run(job, '"%s" -s "%s" "%s" "%s" "%s" auto auto %s'
                        % (PY, os.path.join(STAGES, "mix_cli.py"),
                           inst_wav, voice_wav, mix_wav,
                           "-45" if gate else "off"), timeout=1200)
        if rc2 == 0 and os.path.exists(mix_wav):
            for line in reversed((out2 or "").splitlines()):
                if line.strip().startswith("MIX_DONE"):
                    log(job, "  混合轨：%s" % line.strip())
                    break
            tracks.insert(0, {"label": "成品（人声+伴奏）", "wav": mix_wav})
            end_step(job, True, "成品轨已生成")
        else:
            log(job, "  [warn] 混合轨没做成（退出码 %d），人声干轨仍然可用：%s"
                % (rc2, _tail(out2, 3)))
            end_step(job, True, "混合轨没做成，人声干轨仍可用（已说明）")

    # ---- 客观质检：拿原唱人声轨和角色人声轨逐帧比基频 ----
    # 放在这里（而不是等用户去听）是因为"唱得对不对"以前没有一把尺子：
    # 音高没生效、旋律被唱平、人声和伴奏错位，听起来都只是"难听"，
    # 用户说不清、我也猜不准。量出来之后至少能指名道姓。
    qc_result = None
    if qc and wav_seconds(vocal_wav) > 3:
        job["stage"] = "客观质检"
        begin_step(job, "客观质检",
                   "用 RMVPE 逐帧比「原唱人声」和「角色人声」的基频："
                   "音准吻合率、旋律轮廓、音高起伏比、人声与伴奏是否对齐。"
                   "只量不修 —— 至少让「难听」有个具体的说法。")
        # ★ 质检**只取一段**（30 秒），不整首比。
        #   整首比会出错：一首歌的旋律本来就大量重复，在一两百秒里搜"最像的位置"
        #   很容易锁到另一段副歌上 —— 实测整首比给出"对齐差 1200ms"，
        #   而同一段 12 秒窗口里对齐是 0ms。窗口短、重复少，结果才可信。
        _tot = wav_seconds(vocal_wav) or 0
        _qs = max(0.0, (_tot - 30.0) * 0.35) if _tot > 32 else 0.0
        _qd = min(30.0, _tot) if _tot else 0.0
        qc_result = qc_singing(job, vocal_wav, voice_wav, expect_pitch=pitch,
                               start=_qs, secs=_qd,
                               skip=(guard_info or {}).get("segments"))
        ok, line, det = describe_qc(qc_result, expect_pitch=pitch)
        for dd in det:
            log(job, "  " + dd)
        if qc_result and qc_result.get("skipped_secs"):
            log(job, "  质检已排除 %.1f 秒非歌声段 —— 那几段是**故意保留的原声**"
                     "（口哨不跟着变调），按角色音高去比会得出假结论"
                % qc_result["skipped_secs"])
        if qc_result is not None:
            log(job, "  %s" % line)
            end_step(job, ok, line)
        else:
            end_step(job, True, "客观质检没跑出来（不影响成品）")

    # ---- 整曲质检：上面那个数只代表**一段**，这里把整首铺满再给一个数 ----
    # 为什么必须再量一遍：上面单窗取的是 (总长-30)*0.35 那一段，而八度错在同一首歌
    # 内部能从 3.5% 变到 16.9%（七窗实测，见 qc_singing_whole 上方注释）。
    # 用户照着"抽签出来的数"决定要不要重唱，比不给数更糟。
    qc_whole = None
    if qc and (wav_seconds(vocal_wav) or 0.0) > QC_WHOLE_WIN * 1.5:
        job["stage"] = "整曲质检"
        begin_step(job, "整曲质检（铺满整首，不只量一段）",
                   "把整首歌从第 0 秒铺到结尾，每 %.0f 秒量一窗，再聚合成一个能代表"
                   "「整首」的数，并单独报出**最差那一窗** —— 听感由最差的那段决定。"
                   "只量一段的话，同一份成品可能报 3%% 也可能报 20%%。" % QC_WHOLE_WIN)
        qc_whole = qc_singing_whole(job, vocal_wav, voice_wav,
                                    skip=(guard_info or {}).get("segments"))
        w_warn, w_line = describe_qc_whole(qc_whole)
        if qc_whole:
            log(job, "  %s" % w_line)
            for w in (qc_whole.get("per_window") or []):
                log(job, "    第 %5.0f 秒：八度跳 %5.1f%%，吻合 %5.1f%%"
                    % (w.get("at") or 0.0,
                       (w.get("octave_err_ratio") or 0.0) * 100,
                       (w.get("match_ratio_loose") or 0.0) * 100))
            end_step(job, not w_warn, w_line)
        else:
            end_step(job, True, "整曲质检没跑出来（不影响成品）")

    if save:
        def _add(r):
            if output_kind == "preview":
                # 小样只留一条：试听几次就把成品清单淹了，用户会找不到正片。
                # 每次覆盖上一条（文件名也是固定的 preview_latest）。
                r["outputs"] = [o for o in (r.get("outputs") or [])
                                if o.get("kind") != "preview"]
            # 歌词跟着成品走：改过词就用新词；没改词但这首歌之前识别过，用缓存那份
            # （绝不为了唱歌去触发识别 —— 那要下一 1.5GB 的模型，见 _cached_lyric）。
            # 时间轴只在"句数对得上"时才给，对不上宁可不给（均分出来的假时间轴更坑）。
            _lyr = _lyric_text(lyric)
            _segs = ((job.get("lyric_result") or {}).get("segments") or []) if _lyr else []
            if not _lyr:
                _lyr, _segs = _cached_lyric(song_path)
            for t in tracks:
                r.setdefault("outputs", []).append({
                    "text": label or os.path.basename(song_path), "wav": t["wav"],
                    "at": _now(),
                    "kind": output_kind, "track": t["label"],
                    "seconds": round(wav_seconds(t["wav"]), 2),
                    "lyric": _lyr,
                    "lyric_lrc": _write_lrc(t["wav"], _lyr, _segs),
                    # ★ 把这条成品用到的四条轨都记下来，前端才能做「多轨试听 / A-B 对比」：
                    #   出问题时用户能自己把伴奏、干声单独听一遍，定位到底是哪一步坏了
                    #   （只给一条混好的成品，用户只能说"难听"，说不出难听在哪）。
                    "stems": {
                        "mix": t["wav"] if t["label"].startswith("成品") else mix_wav,
                        "voice": voice_wav,
                        "inst": inst_wav,
                        "src": song_path,
                    },
                    "qc": qc_result,
                    # 整曲质检（铺满整首的那个数）—— 单窗那个 qc 只代表一段，
                    # 这个才能回答"整首到底有多少帧在翻八度"。
                    "qc_whole": qc_whole,
                    # 把"保护了多久"记进产物：用户过几天回头看这条成品，
                    # 能知道当时口哨段是怎么处理的（而不是只看到一个成品文件）。
                    "nonvocal": ({"mode": nonvocal,
                                  "protected_secs": (guard_info or {}).get("protected_secs", 0),
                                  "segments": (guard_info or {}).get("segments") or []}
                                 if nonvocal != "off" else None),
                    # ★ 把「这一版到底是拿什么参数唱的」记进产物。
                    #   为什么要补这个：查「唱歌越唱越差」时，成品记录里只有质检数，
                    #   **没有音高、没有用的是哪个 RVC 模型** —— 于是看到"某版八度错 18%"
                    #   根本没法归因，只能回头去翻 data/jobs/*/log.txt 里那行 `--pitch`。
                    #   16 版成品全靠人工对日志才对上账（对照表见文件头「音高体检」一段）。
                    "params": {
                        "pitch": int(pitch),              # 实际用的音高（体检改判后是改判值）
                        "pitch_asked": int(_pitch_asked),  # 用户在界面上设的那个
                        "model": os.path.basename(rvc_model or ""),
                        "f0": RVC_F0_METHOD,
                        "index_rate": float(index_rate),
                        "rms_mix_rate": (None if rms_mix_rate is None else float(rms_mix_rate)),
                        "protect": (None if protect is None else float(protect)),
                        "engine": engine,
                        "nonvocal": nonvocal,
                        "audition": audition,             # 体检表（含每档的八度错/吻合）
                    },
                })

        mutate_role(rid, _add)
    return tracks


# ------------------------------------------------ 凭空写歌（YuE2 作曲 + 我们的音色）
def make_original_song(job, rid, style, lyrics, duration=120, pitch=0,
                       index_rate=None, sep_model=None, rms_mix_rate=None,
                       protect=None, steps=32, prefix="new", **kw):
    """写一首**世界上还不存在的歌**，用角色的音色唱出来。

    和 sing_role 的关系：这不是另一条管线，而是**给它换一个源头**。

        翻唱：  现成的歌 ─────────────┐
        写歌：  风格+歌词 → YuE2 出曲 ─┴→ 分离 → 换声 → 混音 → 质检 → 登记成品

    ⇒ 第 0 步（分离）之后的全部逻辑一行都不用重写，因为 sing_role 本来就
      只认「一首歌的文件路径」，它不关心那首歌是怎么来的。

    为什么要 YuE2 而不是直接用角色说话：RVC 只能"换音色"，不能"从无到有
    编曲演唱"。YuE2 会作曲、配器、唱，但**克隆不了具体音色**（只能靠 LoRA
    微调）；我们正好相反 —— 有 26 个角色模型和声纹中心，但唱不了新歌。
    两边各补对方的短板，这就是"取长补短"的落点。

    返回 (tracks, meta)：tracks 与 sing_role 的一致，meta 是 YuE2 的元信息
    （含它写的 ABC 乐谱，以后想改旋律用得上）。
    """
    from stages import yue2 as _y2

    role = get_role(rid)
    if not role:
        raise Fail("没有这个角色：%s" % rid)

    log(job, "写歌：风格「%s」" % (style or "")[:70])
    log(job, "      歌词 %d 行，目标 %.0f 秒" % (len([l for l in (lyrics or "").splitlines()
                                                    if l.strip()]), duration))

    job["stage"] = "作曲演唱"
    begin_step(job, "作曲演唱（YuE2）",
               "用 YuE2 按「风格 + 歌词」凭空写一首歌并唱出来（48 kHz）。"
               "这一步产出的是**占位人声**：旋律、编曲、节奏都是它的，"
               "音色还不是角色的 —— 下一步换声才把音色换成角色。")

    meta = {}
    try:
        audio, meta = _y2.synth(job, style, lyrics, duration=duration, steps=steps,
                                say=lambda m: log(job, m))
    except _y2.NotReady as e:
        end_step(job, False, str(e))
        raise Fail("YuE2 没就绪：%s\n"
                   "（它是可选组件 —— 装好后会自动拉起，见 stages/yue2.py 的说明）" % e)
    except _y2.Yue2Fail as e:
        end_step(job, False, str(e))
        raise Fail("YuE2 作曲失败：%s" % e)

    secs = 0.0
    try:
        secs = wav_seconds(audio)
    except Exception:
        pass
    end_step(job, True, "曲子做好了：%.1f 秒（%s）"
             % (secs, os.path.basename(audio)))

    # ---- 从这里开始完全交给 sing_role（分离 / 换声 / 混音 / 质检 / 登记）----
    tracks = sing_role(job, rid, audio, pitch=pitch, index_rate=index_rate,
                       sep_model=sep_model, rms_mix_rate=rms_mix_rate,
                       protect=protect, prefix=prefix, **kw)

    # 把"这歌是谁写的"记进成品：过几天回头看，能分清哪些是翻唱、哪些是原创。
    # ★ 用 sing_role 返回的 tracks 里的**真实文件路径**做匹配，不要靠文件名前缀猜：
    #   实测成品登记的名字是 `voiceg_<tag>.wav`（gate 处理后的那份，多一个 g），
    #   而 sing_role 里生成的是 `voice_<tag>.wav` —— 按 `voice_<prefix>` 去前缀匹配
    #   会一条都命中不了，症状是"跑完了但 origin 一个都没写"。tracks 是权威来源。
    want = set(os.path.basename(t.get("wav") or "") for t in (tracks or [])
               if t.get("wav"))
    marked = [0]

    def _mark(role_doc):
        for o in role_doc.get("outputs") or []:
            if os.path.basename(o.get("wav") or "") in want:
                o["origin"] = {
                    "kind": "yue2-new-song",
                    "style": style,
                    "lyrics": lyrics,
                    "duration": round(duration, 1),
                    # ★ 两个 id 都要记，缺一个就断链：
                    #   job     = 本项目的任务 id（能回溯到 data/jobs/<id>/ 里的质检报告）
                    #   song_id = YuE2 引擎的曲子 id（对应 yue2_<id>.wav）
                    # 第一版只记了引擎的曲子 id，还叫 job_id —— 于是拿它去 data/jobs/
                    # 下找质检报告一条都找不到（统计里"新歌链路 0 样本"就是这么来的）。
                    "job": (job or {}).get("id"),
                    "song_id": meta.get("_job_id"),
                    "elapsed": meta.get("_elapsed"),
                    "abc": meta.get("abc") or "",
                }
                marked[0] += 1
        return marked[0]
    try:
        mutate_role(rid, _mark)
    except Exception as e:
        # 不吞：标不上来源不是要命的事，但必须留痕，否则以后看到成品没有 origin
        # 会以为是"没走这条路"，实际是写失败了。
        log(job, "  [warn] 来源标记没写进成品：%s: %s" % (type(e).__name__, e))
    if not marked[0]:
        log(job, "  [warn] 有成品生成，但没匹配到可标记的登记项（tracks=%d）"
            % len(tracks or []))
    return tracks, meta


# ------------------------------------------------ 成品带歌词（唱的是哪版词，得跟着成品走）
def _lyric_text(lyric):
    """从 (原词, 新词) 里挑出**实际唱的那份**：改过词就是新词。

    为什么要存进成品：过几天回头听这条，光有音频根本不知道当时唱的是原词还是
    改过的那版 —— 想再改一版、或者想跟着唱，都对不上。
    """
    if not lyric:
        return ""
    if isinstance(lyric, (tuple, list)):
        orig, new = (list(lyric) + ["", ""])[:2]
        return (new or orig or "").strip()
    return str(lyric).strip()


def _cached_lyric(song_path, lang="zh"):
    """这首歌**之前**识别过歌词吗？只读缓存，绝不触发识别。

    ★ 为什么不在唱歌流程里自动跑 ASR：那玩意儿第一次要下一 1.5GB 的语音识别模型。
      用户只是想唱首歌，不该被拖着等一个 G 多的下载 —— 而且他根本没要求识别歌词。
      所以：识别过（有缓存）就顺手带上；没识别过就在成品上留个「识别歌词」按钮，
      他自己点，点了才跑，进度也能看见。
    """
    try:
        p = os.path.join(LYRIC_CACHE,
                         _sep_cache_key(song_path, "lyrics|%s" % lang) + ".json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return (d.get("line") or "").strip(), (d.get("segments") or [])
    except Exception:
        pass
    return "", []


def job_output_lyric(job, rid, index, lang="zh"):
    """给**某一条已有成品**补歌词：识别 → 写回这条成品 → 旁边生成 .lrc。

    为什么要单独开一个任务（而不是唱歌时顺手做）：见 `_cached_lyric` 的说明 ——
    识别要下模型，那是用户该自己决定要不要等的事。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    outs = role.get("outputs") or []
    if index < 0 or index >= len(outs):
        raise Fail("没有第 %d 条成品" % (index + 1))
    o = outs[index]
    src = ((o.get("stems") or {}).get("src")) or None
    if not src or not os.path.isfile(src):
        raise Fail("这条成品记的原曲不在了（%s），没法识别" % (src or "没记"))
    d = recognize_lyrics(job, src, lang=lang)
    line = (d.get("line") or "").strip()
    if not line:
        raise Fail("没识别出歌词（可能是纯音乐，或者识别语言不对）")
    segs = d.get("segments") or []

    def _set(r):
        r["outputs"][index]["lyric"] = line
        r["outputs"][index]["lyric_lrc"] = _write_lrc(r["outputs"][index]["wav"], line, segs)

    mutate_role(rid, _set)
    job["stage"] = "完成"
    log(job, "  歌词已写进第 %d 条成品（%d 句）" % (index + 1, len(segs)))
    return {"index": index, "lines": len(segs), "lyric": line}


# ------------------------------------------------ 可视化视频（成品 → 能直接发的 MP4）
#
# 为什么要有这一块：老板做完翻唱，成品是个 wav —— 想发出去还得自己找软件配画面。
# 这里把「音频 + 歌词 + 标题」直接渲染成 MP4，一步到位。
#
# 全部用项目里**已有的那个 ffmpeg**（GPT-SoVITS runtime 里那个），不装任何新依赖：
# 滤镜（showcqt / showfreqs / showwaves / avectorscope）、字幕（libass）、
# 硬件编码（qsv / mf）它都有。这个 ffmpeg 的三个坑写在 `viz.py` 顶部的注释里。

VIZ_DIR = os.path.join(DATA, "videos")


VIZ_MODES = [
    {
        "key": "viz", "name": "生成可视化画面", "needVideo": False,
        "desc": "画面完全由程序生成。输入只有声音 —— 所以它压根没有「本来的画面」"
                "可以遮挡，适合把一整首歌做成一支能直接发的 MV。",
    },
    {
        "key": "mux", "name": "用我的视频当画面 · 只换音轨", "needVideo": True,
        "desc": "画面一个像素都不动，只把声音换成这条成品。几秒钟出片，"
                "**绝不会遮挡**原来的画面。想要「配上我唱的歌、画面还是我的画面」就用这个。",
    },
    {
        "key": "blend", "name": "我的视频 + 可视化叠加", "needVideo": True,
        "desc": "画面内容全都还在，可视化的光按「屏幕混合」叠上去 —— "
                "黑的地方不动、亮的地方更亮。想要一点氛围、又不想挡住画面时用这个。",
    },
]


def viz_options():
    """给前端：有哪些样式 / 画幅，以及**编码器在这台机器上到底能不能用**。

    编码器要真探（真编一帧），不能只看 `-encoders` 列表 —— 本机
    `h264_nvenc` 列得出来但真跑必炸，照列表选就是"每个用户都出不了片"。
    """
    enc, tested = viz.probe_encoder(FFMPEG, os.path.join(DATA, "cache"))
    return {
        "ok": bool(enc),
        "encoder": enc,
        "encoderName": {"h264_qsv": "Intel 核显加速", "h264_mf": "系统编码（MediaFoundation）",
                        "h264_nvenc": "NVIDIA 显卡加速", "h264_amf": "AMD 显卡加速"}.get(enc, enc or ""),
        "tested": tested,
        "reason": "" if enc else "这台机器上没有能用的 H.264 编码器，做不了视频。",
        "styles": viz_all_styles(),
        "modes": VIZ_MODES,
        "sizes": viz.SIZES,
        "fps": viz.FPS_CHOICES,
        "dir": VIZ_DIR,
    }


def viz_all_styles():
    """两套引擎的样式合到一起给前端。

    · `py`（手绘逐帧，vizfx）—— 好看，但 3 分钟的歌要渲 3~5 分钟；
    · `ffmpeg`（内置滤镜，viz）—— 几秒出片，画面朴素一点（但有渐变背景 + 暗角撑着）。

    每组给一句实话告诉用户"要等多久"，别让他在那儿猜是不是卡死了。
    """
    out = []
    if vizfx is not None:
        for s in vizfx.STYLES:
            out.append({"key": s["key"], "name": s["name"], "tag": s.get("tag", ""),
                        "desc": s["desc"], "engine": "py", "group": "draw",
                        "note": "逐帧手绘，画面最精致；一首歌要等几分钟。"})
    for s in viz.STYLES:
        out.append({"key": s["key"], "name": s["name"], "tag": s.get("tag", ""),
                    "desc": s["desc"], "engine": "ffmpeg", "group": "fast",
                    "note": "用 ffmpeg 内置滤镜，几秒钟就出片。"})
    return out


def viz_style_any(key):
    """按 key 找样式，两个引擎都找。返回 (样式, 引擎)。"""
    if vizfx is not None:
        s = vizfx.style_by_key(key)
        if s:
            return s, "py"
    s = viz.style_by_key(key)
    if s:
        return s, "ffmpeg"
    return None, None


def _need_vizfx():
    """手绘引擎不可用时，给一句人能看懂的话（而不是 AttributeError）。"""
    if vizfx is None:
        raise Fail("这台机器上的 Python 没有 numpy/PIL，手绘那套样式做不了"
                   "（换「快速」那组的样式，或者用运行 voice studio 的那个解释器）。\n%s"
                   % _VIZFX_ERR)
    return vizfx


def _safe_name(s, fallback="video"):
    """把标题变成能当文件名的东西（Windows 不允许 \\ / : * ? " < > |）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(s or "")).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s[:80] or fallback)


def _viz_out_path(name, style_key):
    """产物路径。同名就加序号，**不覆盖**用户已经做好的片。"""
    os.makedirs(VIZ_DIR, exist_ok=True)
    base = "%s_%s" % (_safe_name(name), style_key)
    p = os.path.join(VIZ_DIR, base + ".mp4")
    i = 2
    while os.path.exists(p):
        p = os.path.join(VIZ_DIR, "%s_%d.mp4" % (base, i))
        i += 1
    return p


def job_viz(job, audio, title="", subtitle="", lrc_path=None, style_key="cqt",
            size_key="landscape", fps=30, start=None, duration=None,
            out_name=None, bg_video=None, mode="viz"):
    """把一段音频做成视频。

    `audio`   成品 wav 或任意音频文件的绝对路径
    `lrc_path` 带时间轴的 .lrc；给了才会烧歌词，没给就只有画面 + 标题
    `start`/`duration` 不给就是整首

    ★ 三种做法，取决于**画面从哪来**（老板问「加进去是不是遮挡了本来的画面」
      之后加的）：
        `mode="viz"`   （默认）画面 100% 生成 —— 输入本来就只有声音，
                        没有"本来的画面"这回事，也就谈不上遮挡；
        `mode="mux"`   用 `bg_video` 当画面，**只换音轨**，画面一个像素都不动。
                        想要"配上我的歌、画面还是我的画面"就用这个，几秒钟出片；
        `mode="blend"` `bg_video` 当底，可视化的光按**屏幕混合**叠上去 ——
                        画面内容全在，只是让亮的地方更亮，要氛围又不想全挡住时用。
    """
    if not audio or not os.path.isfile(audio):
        raise Fail("找不到要渲染的音频：%s" % (audio or "没给"))
    mode = (mode or "viz").strip().lower()
    if mode not in ("viz", "mux", "blend"):
        raise Fail("不认识这个做法：%s" % mode)
    if mode != "viz" and (not bg_video or not os.path.isfile(bg_video)):
        raise Fail("「用我的视频当画面」要先选一个视频文件（现在给的是：%s）"
                   % (bg_video or "没选"))

    # ★ 起始秒数先对着**真实时长**核一遍。超出长度时 ffmpeg 不报错，
    #   照样吐一个几百字节的空壳 mp4（实测 262 字节）：任务显示"完成"、
    #   视频库里也列出来，用户点开一片漆黑 —— 比直接告诉他"截错了"坑得多。
    _info = viz.media_info(FFPROBE, audio)
    _total = (_info or {}).get("dur") or 0.0
    if _total <= 0.0:
        raise Fail("读不出这段音频的长度（文件可能不是音频，或者已经坏了）：%s"
                   % os.path.basename(audio))
    if start is not None and start >= _total - 0.05:
        raise Fail("从第 %.0f 秒开始，但这首歌一共才 %.1f 秒 —— 截出来会是空的。"
                   "把「起始」改小一点（或干脆不勾「只做一段」做整首）。"
                   % (start, _total))
    if duration is not None and duration <= 0.05:
        raise Fail("要做的时长太短了（%.2f 秒），至少给 1 秒。" % duration)

    style, engine = viz_style_any(style_key)
    if not style:
        raise Fail("不认识这个样式：%s" % style_key)
    size = viz.size_by_key(size_key)
    if not size:
        raise Fail("不认识这个画幅：%s" % size_key)
    fps = 60 if int(fps or 30) >= 60 else 30

    begin_step(job, "检查编码器", "这台机器能不能做 H.264")
    enc, tested = viz.probe_encoder(FFMPEG, os.path.join(DATA, "cache"))
    if not enc:
        # 把每个候选"为什么不行"照实报出来 —— 只说"编码器不可用"用户没法排查
        why = "；".join("%s：%s" % (t["enc"], t["why"] or "不可用")
                        for t in tested if not t["ok"])
        raise Fail("这台机器上没有能用的 H.264 编码器，做不了视频。\n%s" % why)
    end_step(job, True, "用 %s" % enc)

    # ---- 字幕
    ass = None
    n_line = 0
    begin_step(job, "准备字幕", "标题 + 歌词时间轴")
    if lrc_path and os.path.isfile(lrc_path):
        lines = viz.parse_lrc(lrc_path)
        if lines:
            ass = os.path.join(job["dir"], "viz.ass")
            # ★ 截片段（start/duration）时时间轴要一起平移：.lrc 里是**原曲的绝对时间**，
            #   不平移的话截出来的片子里歌词会全错位（而且看起来"像识别坏了"）。
            n_line = viz.build_ass(ass, lines, size, title=title, subtitle=subtitle,
                                   offset=(start or 0.0), duration=duration)
    if not ass:
        # 没歌词也要有标题，不能因为没歌词就变成一个没信息的光板
        ass = os.path.join(job["dir"], "viz.ass")
        viz.build_ass(ass, [], size, title=title, subtitle=subtitle,
                      offset=0.0, duration=duration)
    end_step(job, True, ("%d 句歌词" % n_line) if n_line else "只有标题（这首没时间轴）")

    # 视频比要配的声短 ⇒ 循环播放铺满整首。不循环的话歌还在放、画面早就黑了。
    loop = False
    if mode != "viz":
        _vdur = (viz.media_info(FFPROBE, bg_video) or {}).get("dur") or 0.0
        _want = duration if duration else max(0.0, _total - (start or 0.0))
        loop = bool(_vdur > 0.5 and _vdur < _want - 0.5)
        if loop:
            log(job, "  你的视频只有 %.1f 秒，比这段声音短 —— 会自动循环播放铺满。"
                % _vdur)

    def _render_layer(dst_layer, ass_file):
        """渲一层画面（viz 模式直接渲到产物；blend 模式渲到中间文件）。

        `ass_file` 分开传是因为：blend 模式**这一步不能烧字幕** ——
        先烧再叠的话字幕会被 screen 提亮成一团白，得叠完再烧。
        """
        if engine == "py":
            _need_vizfx()
            # 手绘逐帧：几分钟。进度每一小段报一次，用户知道在动；
            # `should_stop` 让「取消」能立刻停 —— 不然要等整首画完。
            return vizfx.render_fx(
                FFMPEG, enc, audio, dst_layer, style, size, fps, ass=ass_file,
                duration=duration, start=start, log=lambda m: log(job, m),
                should_stop=lambda: job_cancelled(job))
        # 背景图：铺在滤镜产物底下的渐变。没有它画面就是个死黑的洞。
        # 生成失败不算致命（顶多回到黑底），所以不抛异常。
        bg = None
        if vizfx is not None:
            try:
                bg = vizfx.make_bg_png(os.path.join(job["dir"], "viz_bg.png"),
                                       size["w"], size["h"], vizfx.FFMPEG_BG)
            except Exception as e:                           # noqa: BLE001
                log(job, "  （背景图没生成出来，退回黑底：%s）" % e)
        return viz.render(FFMPEG, enc, audio, dst_layer, style, size, fps,
                          ass=ass_file, duration=duration, start=start, bg=bg,
                          log=lambda m: log(job, m))

    # 产物名里带的是"怎么做"的标记：配音那条路没有样式可言，写样式名会误导
    _tag = {"viz": style_key, "mux": "配音", "blend": style_key + "叠加"}[mode]
    dst = out_name or _viz_out_path(title or os.path.basename(audio), _tag)
    begin_step(job, "渲染视频",
               "%s · %dx%d · %dfps"
               % ({"mux": "只换音轨（画面不动）"}.get(mode, style["name"]),
                  size["w"], size["h"], fps))

    if mode == "mux":
        # 只换音轨：画面一个像素都不动。不渲染、不等，几秒钟出片。
        ok, msg = viz.mux_video(FFMPEG, bg_video, audio, dst, size, fps=fps,
                                ass=ass, encoder=enc, duration=duration,
                                start=start, loop=loop,
                                log=lambda m: log(job, m))
    elif mode == "blend":
        lyr = os.path.join(job["dir"], "viz_layer.mp4")
        ok, msg = _render_layer(lyr, None)
        if ok and not job_cancelled(job):
            ok, msg = viz.blend_video(FFMPEG, lyr, bg_video, audio, dst, size,
                                      fps=fps, ass=ass, encoder=enc,
                                      duration=duration, start=start, loop=loop,
                                      log=lambda m: log(job, m))
    else:
        ok, msg = _render_layer(dst, ass)
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    if not ok:
        job["error"] = msg
        raise Fail(msg)
    # ★ 产物也要量一遍：ffmpeg 返回 0 不代表出的是视频
    _out = viz.media_info(FFPROBE, dst)
    if not _out or not _out.get("video") or (_out.get("dur") or 0) < 0.3:
        try:
            os.remove(dst)                 # 空壳别留在库里恶心人
        except OSError:
            pass
        raise Fail("渲染出来的是个空壳（%.1f KB / 时长 %.2f 秒），已删掉。"
                   "多半是「起始」秒数截到了音频外面。"
                   % (os.path.getsize(dst) / 1024.0 if os.path.exists(dst) else 0,
                      (_out or {}).get("dur") or 0.0))
    end_step(job, True, msg)

    mb = os.path.getsize(dst) / 1048576.0
    job["stage"] = "完成"
    log(job, "  视频已生成：%s" % os.path.basename(dst))
    return {"path": dst, "name": os.path.basename(dst), "mb": round(mb, 1),
            "style": style["key"], "style_name": style["name"],
            "size": "%dx%d" % (size["w"], size["h"]), "fps": fps,
            "lyric_lines": n_line, "encoder": enc}


def viz_library():
    """已经做好的视频（按时间倒序）。前端「做视频」页列出来给下载/预览。"""
    out = []
    if os.path.isdir(VIZ_DIR):
        for n in os.listdir(VIZ_DIR):
            if not n.lower().endswith(".mp4"):
                continue
            p = os.path.join(VIZ_DIR, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"name": n, "path": p, "mb": round(st.st_size / 1048576.0, 1),
                        "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


def _lrc_ts(sec):
    m, s = divmod(max(0.0, float(sec)), 60.0)
    return "%02d:%05.2f" % (int(m), s)


def _write_lrc(wav, text, segs=None):
    """成品旁边写一份 .lrc。时间轴对得上就带，对不上就只写文本（照样能看能复制）。

    ★ 两条不做的：
      ① **不按行数均分时间** —— 均分出来的时间轴看着像真的，其实一句都对不上，
         播放器照着错的时间滚，用户会以为识别坏了，比干脆不给时间轴更坑；
      ② 改过词之后行数变了就**不给时间轴**（识别到的是原词的时间，套不上去）。
    """
    if not text:
        return ""
    p = os.path.splitext(wav)[0] + ".lrc"
    try:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        timed = segs and len(segs) == len(lines)
        with open(p, "w", encoding="utf-8-sig") as f:      # BOM：不然有些播放器中文乱码
            for i, line in enumerate(lines):
                if timed:
                    f.write("[%s]%s\n" % (_lrc_ts(segs[i].get("start", 0)), line))
                else:
                    f.write(line + "\n")
        return p
    except Exception:
        return ""


def clip_audio(job, src, dst, start_sec, secs):
    """裁一段音频，统一输出 44.1kHz 立体声 wav。"""
    rc, out = run(job, '"%s" -y -v error -ss %.3f -t %.3f -i "%s" -ac 2 -ar 44100 "%s"'
                  % (FFMPEG, max(0.0, start_sec), secs, src, dst), timeout=600)
    return (rc == 0 and os.path.exists(dst)), out


def _sing_preview(job, rid, song_path, secs, pitch, index_rate, rms_mix_rate,
                  protect, sep_model, lyric=None, nonvocal="keep", engine="rvc"):
    """试听小样：整首链路照走，但只对其中一小段跑 RVC。

    为什么值得单独做一条路：正式的 RVC 变声是这首歌里**唯一**的长耗时步骤
    （本机 3分45秒 → 18 分钟）。用户改一个音高就要重等 18 分钟才知道对不对，
    效率极低。这里用「分离照做（有缓存）+ 只转换一段（几十秒）」把试错成本
    从十几分钟压到一两分钟。小样会以 kind="preview" 记进角色成品清单，但
    **永远只留一条**（下一次试听就把上一条摘掉），不会把正片挤下去。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    if not (role.get("rvc") or {}).get("model"):
        raise Fail(
            "这个角色还没有歌声模型，没法试听。\n"
            "歌声模型（RVC）和说话模型（GPT-SoVITS）是两套东西：说话模型只能照着"
            "文本念。先把这个角色的 RVC 歌声模型训出来再试听。")

    d = role_dir(rid)
    final = os.path.join(d, "final")
    os.makedirs(final, exist_ok=True)
    # 小样用固定文件名（preview_latest）：每次覆盖上一条，界面上永远只有一份
    # "当前样片"，不会试听几次就堆出一排。
    stamp = "latest"

    job["stage"] = "分离人声"
    begin_step(job, "分离人声与伴奏",
               "试听小样也要走这一步 —— 分离结果会缓存，正式跑全曲时直接复用，"
               "不会白做。")
    vocal, inst = separate_song(job, song_path, model=sep_model)
    end_step(job, True, "人声轨 + 伴奏轨已就绪（已缓存，正式跑直接复用）")

    total = wav_seconds(song_path) or wav_seconds(vocal)
    secs = int(secs)
    if total:
        secs = max(8, min(secs, int(total)))
    # 从 35% 处起：避开前奏，通常已经进主歌/副歌，更能代表整首。
    start = max(0.0, (total - secs) * 0.35) if total else 0.0
    clip_v = os.path.join(final, "_pv_voice_%s.wav" % stamp)
    clip_i = os.path.join(final, "_pv_inst_%s.wav" % stamp)
    ok1, _ = clip_audio(job, vocal, clip_v, start, secs)
    ok2, _ = clip_audio(job, inst, clip_i, start, secs)
    if not (ok1 and ok2):
        raise Fail("试听片段裁剪失败。")
    log(job, "  试听片段：第 %.0f 秒起 %.0f 秒（整首 %.0f 秒）" % (start, secs, total))

    tracks = sing_role(
        job, rid, clip_v, pitch=pitch, index_rate=index_rate, prefix="preview",
        rms_mix_rate=rms_mix_rate, protect=protect, save=True, pre_sep=(clip_v, clip_i),
        output_kind="preview", label=os.path.basename(song_path), lyric=lyric,
        nonvocal=nonvocal, engine=engine)

    # 裁出来的中间片段用完了就删；小样本身留在 final 里给用户听。
    for p in (clip_v, clip_i):
        with contextlib.suppress(Exception):
            os.remove(p)
    return tracks


def sing_job(job, rid, song_path, pitch=0, index_rate=None, quality="standard",
             auto_train=True, model=None, epochs=None, preview_secs=0,
             rms_mix_rate=None, protect=None, sep_model=None, lyric=None,
             nonvocal="keep", engine="rvc"):
    """
    「一键唱」入口：缺歌声模型就先把模型训出来，再直接开唱。

    用户只想拖一个歌进来，不想先关心模型有没有 —— 所以把训练和唱歌串成一次任务，
    中间不用他点第二次。训练用档位轮数（默认标准 150 轮）。

    epochs 给了就按自定义轮数训（翻唱台选「自定义」时传进来），
    能训到 300 轮以上 —— 档位里的 300 只是推荐值，不是上限。

    preview_secs > 0 时走「试听小样」那条路（见 _sing_preview）。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    if preview_secs:
        return _sing_preview(job, rid, song_path, int(preview_secs), pitch,
                             index_rate, rms_mix_rate, protect, sep_model,
                             lyric=lyric, nonvocal=nonvocal, engine=engine)
    if not model and not (role.get("rvc") or {}).get("model") and auto_train:
        n = len([s for s in role.get("segments", []) if s.get("dur", 0) >= 2.0])
        if n < 10:
            raise Fail(
                "这个角色只有 %d 段干声（至少要 10 段）可以直接训歌声模型。\n"
                "可以先用它的说话音色唱不了歌 —— 请再导入这个人的更多素材。" % n)
        log(job, "  这个角色还没有歌声模型，先训一个（%s）。慢一点，但省得你手动操作。"
            % ("自定义 %d 轮" % epochs if epochs
               else {"fast": "快速 50 轮", "standard": "标准 150 轮",
                     "perfect": "完美 300 轮"}.get(quality, quality)))
        train_rvc(job, rid, epochs=epochs, quality=quality)
    return sing_role(job, rid, song_path, pitch=pitch, index_rate=index_rate,
                     model=model, mix=True, sep_model=sep_model,
                     rms_mix_rate=rms_mix_rate, protect=protect, lyric=lyric,
                     nonvocal=nonvocal, engine=engine)


def _rvc_spec_race(out):
    """RVC 是不是崩在**它自己**那个音频缓存的写竞争上。

    `train/data_utils.py` 在 DataLoader worker 里「首次用到才写」`*.spec.pt`，
    既没锁也不是原子写；4 个 worker 一起跑时，Windows 上会偶发

        PermissionError: [Errno 13] Permission denied: ...\\0_gt_wavs\\14_2.spec.pt

    训练于是**在第 1 轮就挂掉**，甩给用户一句看不懂的英文 —— 他会以为是自己的
    素材坏了。这是 RVC 自己的锅：`num_workers=4` 在 `train/train.py` 里写死，
    配置文件和命令行都改不了。我们能做的只有**认出它、自动重来一次**：
    崩过之后大部分 spec 缓存已经落盘，重跑时竞争窗口小得多（实测重试一次就好）。

    ★ 只在**同时**出现 PermissionError/Errno 13 和 .spec.pt 时才认，
      真正的训练错误（底模不对、显存不够…）绝不能被这个重试悄悄吞掉。
    """
    s = out or ""
    return ("PermissionError" in s or "Errno 13" in s) and ".spec.pt" in s


def _wav_quality(path):
    """标准库读一段 wav，量出体检要的几个数。读不动返回 None（不抛）。

    返回 dict：{sr, dur, rms_db, floor_db, peak}
      rms_db   整段响度（含间隙）
      floor_db **底噪地板** = 能量最低的 10% 帧的响度 —— 那几帧就是"这个人没在唱"
                的时候，量出来的就是环境底噪/电流声。整段 rms 会被歌声本身抬上去，
                看不出底噪，所以必须单独取静音帧。
    """
    try:
        import wave
        import array
        import math
        w = wave.open(path, "rb")
        sr, ch, sw, n = (w.getframerate(), w.getnchannels(),
                         w.getsampwidth(), w.getnframes())
        raw = w.readframes(n)
        w.close()
        if sw == 2:
            a = array.array("h")
            a.frombytes(raw)
            scale = 32768.0
        elif sw == 4:
            a = array.array("i")
            a.frombytes(raw)
            scale = 2147483648.0
        else:
            return None
        if ch > 1:
            a = a[::ch]      # 只取左声道：立体声的两路一样，算两遍没意义
        if not len(a):
            return None
        tot = 0.0
        peak = 0.0
        for v in a:
            f = float(v)
            tot += f * f
            if f < 0:
                f = -f
            if f > peak:
                peak = f
        rms = math.sqrt(tot / len(a)) / scale
        # 20ms 分帧，取能量最低的 10% 帧当底噪
        fl = max(1, int(sr * 0.02))
        fr = []
        i = 0
        while i + fl <= len(a):
            s = 0.0
            for v in a[i:i + fl]:
                s += float(v) * float(v)
            fr.append(math.sqrt(s / fl) / scale)
            i += fl
        if not fr:
            return None
        fr.sort()
        k = max(1, len(fr) // 10)
        floor = sum(fr[:k]) / k
        def _db(x):
            return 20.0 * math.log10(x) if x > 0 else -120.0

        return {"sr": sr, "dur": len(a) / float(sr), "rms_db": _db(rms),
                "floor_db": _db(floor), "peak": peak / scale}
    except Exception:
        return None


def audit_train_data(rid, segs=None):
    """开训前给素材做一次体检。**只读、不改任何数据、不抛异常。**

    返回 dict（跑不通返回 None）—— 体检失败不该把训练挡住，这是"给个提醒"不是"设卡"。

    判据全部来自 TRAIN_* 那组常量旁的文献依据与本机实测。
    结果里 `issues` 是给人看的 [(级别, 说明)]，级别 ∈ bad / warn / info：
      bad  = 文献明确判"不合格"
      warn = 合格但不在最佳区间
      info = 只陈述事实（采样率那条就是，因为用户此刻改不了）
    """
    try:
        role = get_role(rid) or {}
        if segs is None:
            segs = [s for s in (role.get("segments") or []) if (s.get("dur") or 0) >= 2.0]
        sd = os.path.join(role_dir(rid), "slices")
        if not segs or not os.path.isdir(sd):
            return None
        # 抽样：段数多时等距抽（不是取前 N 段 —— 素材是按时间排的，前 N 段不代表整批）
        if len(segs) > TRAIN_AUDIT_MAX:
            step = len(segs) / float(TRAIN_AUDIT_MAX)
            idx = [int(i * step) for i in range(TRAIN_AUDIT_MAX)]
            picked = [segs[i] for i in idx]
        else:
            picked = list(segs)
        rows = []
        for s in picked:
            p = os.path.join(sd, s.get("file") or "")
            if not os.path.isfile(p):
                continue
            q = _wav_quality(p)
            if q:
                q["voiced"] = s.get("voiced")
                rows.append(q)
        if not rows:
            return None

        def _pct(xs, p):
            xs = sorted(xs)
            return xs[max(0, min(len(xs) - 1, int((len(xs) - 1) * p)))]

        durs = [r["dur"] for r in rows]
        rms = [r["rms_db"] for r in rows]
        floors = [r["floor_db"] for r in rows]
        srs = {}
        for r in rows:
            srs[r["sr"]] = srs.get(r["sr"], 0) + 1
        total = sum(durs) / float(len(durs)) * len(segs)   # 按抽样均值外推整批
        span = _pct(rms, .9) - _pct(rms, .1)

        issues = []
        # ① 采样率
        top_sr = max(srs.items(), key=lambda kv: kv[1])[0]
        if len(srs) > 1:
            issues.append(("bad", "采样率不统一（%s），混着训会互相拖累。"
                                  % "、".join("%dHz×%d" % (k, v) for k, v in sorted(srs.items()))))
        if top_sr < 44100:
            issues.append(("info", "切片是 %d Hz（高频上限 %.1f kHz）。文献推荐 44.1k/48k；"
                                   "这是切片工具为说话模型降的，下次导入新素材时会保住原采样率。"
                                   % (top_sr, top_sr / 2000.0)))
        # ② 总时长
        if total < TRAIN_TOTAL_MIN:
            issues.append(("warn", "素材合计 %.1f 分钟，文献建议的下限是 %.0f 分钟 —— "
                                   "偏少时模型容易沙哑、音色不稳。"
                                   % (total / 60.0, TRAIN_TOTAL_MIN / 60.0)))
        elif total > TRAIN_TOTAL_MAX:
            issues.append(("info", "素材合计 %.1f 分钟，超过文献说的 %.0f 分钟；"
                                   "再多边际收益很小，训练时间却线性变长。"
                                   % (total / 60.0, TRAIN_TOTAL_MAX / 60.0)))
        # ③ 单段时长
        #   ★ 门槛定 10% 而不是"有就报"：体检是**抽样**的（段数多时等距抽 150 段），
        #     占比在百分之几量级时会随抽样抖动 —— 大帝实测 2.5%~3%，报出来是噪声。
        #     真正要提醒的是"这批素材里有一大截是碎片"。
        short = sum(1 for d in durs if d < TRAIN_SEG_MIN)
        long_ = sum(1 for d in durs if d > TRAIN_SEG_MAX)
        if short and 100.0 * short / len(durs) >= 10.0:
            issues.append(("warn", "有 %.0f%% 的段短于 %.0f 秒（信息量不足，"
                                   "文献建议 3~15 秒）" % (100.0 * short / len(durs), TRAIN_SEG_MIN)))
        if long_ and 100.0 * long_ / len(durs) >= 10.0:
            issues.append(("warn", "有 %.0f%% 的段长于 %.0f 秒" % (100.0 * long_ / len(durs), TRAIN_SEG_MAX)))
        # ④ 底噪
        bad = sum(1 for f in floors if f > TRAIN_FLOOR_BAD)
        meh = sum(1 for f in floors if TRAIN_FLOOR_WARN < f <= TRAIN_FLOOR_BAD)
        if bad:
            issues.append(("bad", "%d 段底噪高于 %.0f dB（文献判「不合格」），"
                                  "这类段会把环境噪音教给模型。" % (bad, TRAIN_FLOOR_BAD)))
        elif meh:
            issues.append(("warn", "%d 段底噪在 %.0f~%.0f dB 之间（合格但不够干净）。"
                                   % (meh, TRAIN_FLOOR_WARN, TRAIN_FLOOR_BAD)))
        # ⑤ 响度一致
        #   ★ 只在越过"合格"线（±6 dB）时才报。±3 dB 是**优秀**线 —— 大帝实测
        #     3.0~3.1 dB 正好骑在这条线上，而体检是抽样的，每次跑会在 3.0/3.1 之间跳，
        #     报出来的是抖动不是问题（铁律：不许把间歇性抖动伪装成能力变化）。
        if span > TRAIN_LOUD_SPAN_WARN:
            issues.append(("warn", "各段响度跨度 %.1f dB（文献：±3 dB 优秀 / ±6 dB 合格），"
                                   "忽大忽小会让模型学到音量起伏而不是音色。" % span))
        # ⑥ 削波
        clip = sum(1 for r in rows if r["peak"] >= 0.999)
        if clip:
            issues.append(("bad", "%d 段音量顶到头（削波失真），这种段教出来的是爆音。" % clip))

        verdict = "bad" if any(lv == "bad" for lv, _ in issues) else (
            "warn" if any(lv == "warn" for lv, _ in issues) else "ok")
        return {"n": len(rows), "sampled": len(rows) < len(segs), "total_secs": total,
                "sr": srs, "top_sr": top_sr,
                "dur_med": _pct(durs, .5), "rms_med": _pct(rms, .5),
                "rms_span": span, "floor_med": _pct(floors, .5),
                "issues": issues, "verdict": verdict}
    except Exception:
        return None


def describe_train_audit(a):
    """把体检结果压成一行给人看的话（用户不看表格，看结论）。"""
    if not a:
        return "素材体检没跑成（不影响训练）"
    head = ("%d 段 / %.1f 分钟 / %dHz / 响度 %.1f dB（跨度 %.1f）/ 底噪 %.0f dB"
            % (a["n"], a["total_secs"] / 60.0, a["top_sr"],
               a["rms_med"], a["rms_span"], a["floor_med"]))
    if a["verdict"] == "ok":
        return head + " —— 素材没问题"
    bad = [m for lv, m in a["issues"] if lv == "bad"]
    warn = [m for lv, m in a["issues"] if lv == "warn"]
    tail = []
    if bad:
        tail.append("**要紧的**：" + "；".join(bad))
    if warn:
        tail.append("可以更好：" + "；".join(warn))
    return head + " —— " + "。".join(tail)


def recommend_rvc_epochs(total_secs, n_segs):
    """按素材量推荐训练轮数。返回 (建议轮数, 一句为什么)。

    ★ 为什么需要这个：轮数不是越多越好 —— 多篇实践一致指出**过训会出机械感/金属声**
      （官方 FAQ 原话："Over-training causes robotic artifacts… roll back to an
      earlier checkpoint"）。而"多少算多"取决于素材量：
        · 素材少 ⇒ 每轮见到的样本少 ⇒ 需要**更多轮**才收敛
          （官方 FAQ："Train longer, 500+ epochs for small datasets"）
        · 素材足 ⇒ 更少轮就够，**继续训只是在背训练集的噪音**

    分档（按总时长，段数只用于兜底）：

    2026-10-18 按**社区实测表**重标 —— 旧分档整体偏高，尤其"素材越多训得越多"
    那几档反了（素材多反而该少训）。三份独立来源给出同一张表：
      · RVC 2.3 实测文章（aixplore.cc）：<3min 不建议正式训练 / 3~5min 350~500 /
        5~10min 280~450 / 10~20min 150~280 / 20~40min 80~160 / 40~60min 50~100 /
        60min+ 30~80
      · voicechanger.live 训练指南：over-training ⇒ 机械/金属声，"best checkpoint
        is rarely the last one"，经验甜点在 200~400 轮（对应 10~20 分钟素材）
      · Applio 文档 / 社区配置：10 分钟以上素材用 200 轮附近，40 分钟以上不必
        机械地训几百轮
    取每档**偏保守的下半段**（宁可少训再说，因为过训的代价是机械感，无法通过
    推理参数救回来；欠训只是音色不够稳，退回去续训即可）：

        < 3 分钟    400  数据太少，怎么训都是过拟合，该做的是补素材
        3~5 分钟    350
        5~10 分钟   300
        10~20 分钟  200   ← 社区甜点区
        20~40 分钟  120   ← 大帝 29.6 分钟落这档（旧表给 200，实际训了 300）
        40~60 分钟   75
        > 60 分钟    55
    纯函数，不抛异常。
    """
    try:
        t = float(total_secs or 0) / 60.0
    except (TypeError, ValueError):
        t = 0.0
    if t <= 0 and n_segs:
        t = n_segs * 5.0 / 60.0      # 时长量不出来就用段数估（一段约 5 秒）
    if t < 3:
        return 400, "素材不到 3 分钟 —— 社区共识是这量级**不适合正式训练**，怎么训都在背训练集；该做的是补素材"
    if t < 5:
        return 350, "素材 %.1f 分钟，属小数据集 ⇒ 要训到 350 轮以上才收敛（社区表 350~500）" % t
    if t < 10:
        return 300, "素材 %.1f 分钟，偏少 ⇒ 300 轮上下（社区表 280~450）" % t
    if t < 20:
        return 200, "素材 %.1f 分钟，正落社区甜点区 ⇒ 200 轮上下就够（社区表 150~280）" % t
    if t < 40:
        return 120, "素材 %.1f 分钟，很充裕 ⇒ 120 轮上下（社区表 80~160）；再多是在背训练集的噪音，容易出机械感" % t
    if t < 60:
        return 75, "素材 %.1f 分钟，量很大 ⇒ 75 轮上下（社区表 50~100），不必机械地训几百轮" % t
    return 55, "素材 %.1f 分钟，量极大 ⇒ 55 轮上下（社区表 30~80），按 step 和试听结果停，不是轮数越多越好" % t


def _vram_free_mb():
    """显卡现在还剩多少 MB 显存（拿不到返回 None）。

    为什么要有它：48k 的批大小限制本质是**显存压力**，不是采样率属性。
    实测同一台机器：被占 4.2GB 时 48k/bs=2 假死（GPU 99% 但一轮都推不动）；
    被占 1.8GB 时 48k/bs=2 全程 5951 MiB、~90 秒/轮。写死 bs=1 等于在显存充裕时
    白慢 5 倍。拿不到就返回 None，调用方退回保守值 —— 探测失败不许改变结论。
    """
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, timeout=15)
        lines = (out.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        return int(lines[0].strip()) if lines else None
    except Exception:
        return None


def _assert_exp_sr(exp, sr):
    """开训前确认 `logs/<exp>` 里的旧断点和这次的采样率一致。

    为什么单独抽成函数：exp 名只带**架构**不带**采样率**
    （`<rid>` / `<rid>_v2`），所以 v2/32k 和 v2/48k 会落进同一个实验目录。
    32k 生成器的上采样是 10,8,2,2，48k 是 12,10,2,2 —— 直接在 32k 断点上续
    48k 会在加载权重时撞 size mismatch，报错里只有一屏张量尺寸和一个退出码，
    用户完全猜不到原因是"换了采样率"。

    抽出来还有个好处：验收能**直接调它**做负对照（见第 30 关 G 段），
    不用为了验这道护栏去真跑一次训练。
    """
    cfg = os.path.join(RVC_ROOT, "logs", exp, "config.json")
    if not os.path.isfile(cfg):
        return
    try:
        with open(cfg, encoding="utf-8") as f:
            old_sr = int((json.load(f).get("data") or {}).get("sampling_rate") or 0)
    except Exception:
        return          # 读不出来就不拦（不能因为一份坏 config 把训练卡死）
    s = str(sr).strip().lower()
    try:
        want = int(float(s[:-1]) * 1000) if s.endswith("k") else int(s)
    except Exception:
        return
    if old_sr and want and old_sr != want:
        raise Fail(
            "这个实验目录已经有 **%d Hz** 的训练断点（logs/%s），"
            "这次要训的是 **%d Hz**。\n"
            "两者不能共用一个实验目录：生成器的上采样配置不一样，"
            "直接续训会在加载权重时撞 size mismatch（报错里只有一堆张量尺寸）。\n"
            "要选一个：\n"
            "  · 换成和旧断点一样的采样率（%d Hz）；\n"
            "  · 或者换一个底模 —— 自定义底模会带独立的实验名，天然不冲突；\n"
            "  · 或者把旧目录改名留档，让这次从空目录开始：\n"
            "      logs\\%s  →  logs\\%s_sr%d\n"
            "    （改名不会删你原来的模型，想回退把名字改回来即可。）"
            % (old_sr, exp, want, old_sr, exp, exp, old_sr))


def train_rvc(job, rid, epochs=None, sr=None, quality="standard", restart=False,
              arch=None, pretrain=None, bind=True):
    """
    用角色的干声素材训一个 RVC 歌声模型。

    RVC 训练是三段式：切分+预处理 -> 提特征 -> 训练+建索引。
    和 GPT-SoVITS 那套完全独立，所以这里要跨到 RVC 的目录和 python 去跑。

    quality 档位 -> 训练轮数：fast=50 / standard=150 / perfect=300。
    RVC 和说话模型不一样，轮数少了会沙哑，宁可多训。默认 150。

    arch = "v1"（默认）/ "v2"：v2 是官方说质量更好的架构（768 维特征 + 4 判别器），
    但**只能配 32k/48k**（没有 40k），且必须从零重训 ⇒ 用独立的 exp 名，见
    RVC_ARCH_* 那组常量旁的注释。

    pretrain = None（官方语音底模）/ "klm" 等 `RVC_CUSTOM_PRETRAINS` 的键。
    ★ 指定自定义底模时，**架构和采样率由底模自己决定**（KLM 只有 32k ⇒ v2/32k），
      传别的会被忽略 —— 底模和架构对不上是"训练照跑、底模全废"的根源。

    bind = True 时训完把模型绑到角色上；False 只训练、产物留在
    `assets/weights/` 供对比，**不动角色现有模型**（做对照实验用）。
    """
    # ---- 自定义底模：它锁死架构和采样率，且必须走独立 exp ----
    pretrain = (str(pretrain).strip().lower() if pretrain else "") or None
    if pretrain:
        cp = RVC_CUSTOM_PRETRAINS.get(pretrain)
        if not cp:
            raise Fail("没有这个自定义底模：%s（可选：%s）"
                       % (pretrain, " / ".join(RVC_CUSTOM_PRETRAINS) or "无"))
        gp0, dp0 = rvc_pretrained_here(cp["arch"], cp["sr"], pretrain)
        if not (gp0 and dp0):
            raise Fail(
                "自定义底模「%s」没放齐：\n  %s / %s\n"
                "这两个文件**不会自动下载**（不在官方仓库的固定目录结构里），"
                "先手工放好再训。"
                % (cp["label"], os.path.basename(cp["g"]), os.path.basename(cp["d"])))
        arch, sr = cp["arch"], cp["sr"]

    # ---- 架构与采样率：两件事一起定，因为它们是配套的 ----
    arch = str(arch or RVC_ARCH_DEFAULT).lower()
    if arch not in RVC_ARCH_SR:
        arch = RVC_ARCH_DEFAULT
    if not sr:
        sr = RVC_ARCH_SR[arch]
    sr = str(sr).lower()
    if sr not in RVC_ARCH_SRS[arch]:
        raise Fail("采样率 %s 和 %s 架构不配套 —— %s 只能配 %s。"
                   % (sr, arch, arch, " / ".join(RVC_ARCH_SRS[arch])))
    if not epochs:
        epochs = RVC_QUALITY_PRESETS.get(quality, 150)
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    segs = [s for s in role.get("segments", []) if s.get("dur", 0) >= 2.0]
    if len(segs) < 10:
        raise Fail(
            "训歌声模型需要至少 10 段、合计 1 分钟以上的干净干声，"
            "这个角色只有 %d 段。素材不够硬训只会得到一个沙哑的模型。" % len(segs))

    d = role_dir(rid)
    # ★ v2 用独立 exp 名：同一个 exp 下 config.json 会被新架构覆盖，
    #   旧断点（v1 架构）读进来必然 mismatch。分开就两边都安全。
    exp = rid + RVC_ARCH_EXP_SUFFIX.get(arch, "")
    # ★ 换了自定义底模就必须换 exp：RVC 靠 logs/<exp>/G_*.pth 判断"要不要续训"，
    #   而同名下已有断点时会**跳过底模加载**（见下面 was_scratch 那段）。跟官方
    #   底模的模型共用 exp，等于新底模永远不生效 —— 而且会把你原来的断点覆盖掉。
    if pretrain:
        exp += "_" + pretrain
    # ★ exp 名只带**架构**不带**采样率**（`ra500fb92` / `ra500fb92_v2`），
    #   所以 v2/32k 和 v2/48k 会落在同一个 exp 目录里。这不是理论风险：同一批
    #   特征目录名（3_feature768）和同一个 G_*.pth 会被两种采样的训练共用，
    #   48k 的生成器上采样率是 12,10,2,2，32k 是 10,8,2,2 ⇒ load_state_dict 直接
    #   size mismatch，用户看到的是一屏张量尺寸和一个退出码，完全猜不到是"换了采样率"。
    #   所以在开训前把这件事说明白，而不是让他去读报错。
    _assert_exp_sr(exp, sr)
    if arch != RVC_ARCH_DEFAULT:
        log(job, "  架构 %s / %s（v2 用独立的实验名 %s，不会动你原来的模型）"
                 % (arch, sr, exp))
    dataset = os.path.join(d, "rvc_dataset")
    os.makedirs(dataset, exist_ok=True)
    src = os.path.join(d, "slices")
    n = 0
    for s in segs:
        p = os.path.join(src, s["file"])
        if os.path.exists(p):
            shutil.copy(p, os.path.join(dataset, "%03d.wav" % n))
            n += 1
    log(job, "  准备了 %d 段干声" % n)

    # ---- 开训前：给素材做一次体检 + 按素材量给轮数建议 ----
    # ★ 两条都**只报不拦**：体检跑不通、或者结论不好看，都不该把训练挡住
    #   （用户可能就是想硬训一版听听）。这是"给个提醒"，不是"设卡"。
    audit = None
    try:
        audit = audit_train_data(rid, segs)
        if audit:
            log(job, "  素材体检：%s" % describe_train_audit(audit))
            for lv, m in audit["issues"]:
                if lv == "bad":
                    log(job, "    [要紧] %s" % m)
                elif lv == "warn":
                    log(job, "    [可以更好] %s" % m)
    except Exception:
        # 体检是增强，绝不能把训练带死（铁律：增强功能失败要退化，不是崩）
        audit = None
    try:
        _base = audit.get("total_secs") if audit else sum(s.get("dur", 0) for s in segs)
        rec, why = recommend_rvc_epochs(_base, n)
        # 1.3 而不是 1.5：大帝 29.5 分钟推荐 200、perfect 档正是 300（=1.5 倍），
        # 卡在等号上就永远提示不出来 —— 这是"提醒"不是判据，宁可早说。
        if int(epochs) > int(rec) * 1.3:
            log(job, "  [提示] 这次训 %d 轮，但按素材量推荐的是 %d 轮（%s）。"
                     "轮数不是越多越好 —— 训过头会出机械感/金属声，"
                     "文献给的做法是退回早一点的存档试听对比。" % (int(epochs), rec, why))
        elif int(epochs) < int(rec) * 0.5:
            log(job, "  [提示] 这次训 %d 轮，按素材量推荐的是 %d 轮（%s）。"
                     "轮数偏少时模型容易沙哑、音色不稳。" % (int(epochs), rec, why))
    except Exception:
        pass

    # RVC 的训练脚本都在 train/ 下面，必须用 `-m` 以模块方式跑：
    # 直接 `python train/preprocess.py` 时 sys.path[0] 是 train/ 目录，
    # 里面的 train.py 会抢先被当成 train 包，导致 from train import utils 循环导入
    # （ModuleNotFoundError / partially initialized module 两种报错，实测都踩过）。
    # PYTHONPATH 里也**不能**再加 RVC_ROOT/train，否则同样把 train 指向 train.py。
    rvc_env = {"PYTHONPATH": RVC_ROOT}

    # RVC 的 -te 是「训到第几轮」而不是「再训几轮」，而且 train.py 会自动从
    # logs/<exp>/G_*.pth 续训。已经训到 N 轮再传 -te N，循环一次都不跑，
    # 末尾 `if epoch >= hps.total_epoch` 引用未绑定的 epoch 直接 NameError。
    # 所以这里先把「已经多少轮」问清楚：不够就往下续，够了就明说而不是崩。
    # ---- 能不能用上底模：这条决定了「唱出来能不能听」----
    # RVC 的 train.py 只在**没有历史断点**时才去加载 -pg（见 train.py 的
    # try load_checkpoint / except 里才 load_pretrained_generator）。
    # 也就是说：只要 logs/<exp>/G_*.pth 还在，续训就**根本不会**用底模。
    # 所以「手上有个从零训的废模型」这种情况，光加 -pg 是救不回来的，
    # 必须先把断点挪走、从头训一次。这里把这件事摆到台面上，别让用户白等
    # 几十分钟，训完发现还是不跟调。
    rvc_exp0 = os.path.join(RVC_ROOT, "logs", exp)
    ckpts = sorted(glob.glob(os.path.join(rvc_exp0, "[GD]_*.pth")))
    was_scratch = (role.get("rvc") or {}).get("pretrained") is False
    if ckpts and was_scratch and not restart:
        raise Fail(
            "这个角色现有的歌声模型是**没用底模、从零训**的（输出不跟旋律、"
            "变调无效，就是「唱出来没法听」最常见的原因）。\n"
            "而 RVC 只在没有历史断点时才会用底模 —— 直接在旧模型上续训，"
            "底模依然不会被加载，训完还是老样子。\n"
            "要修好它，请勾选「从头重训（换用底模）」再开始。\n"
            "（旧的断点会挪到 logs/%s/_从零训的旧断点/ 保留，不会丢；"
            "预处理好的特征会复用，不用重新提。）" % exp)
    if restart and ckpts:
        bak = os.path.join(rvc_exp0, "_从零训的旧断点")
        os.makedirs(bak, exist_ok=True)
        for p in ckpts:
            # 移动而不是复制：这两个文件加起来 1G 多，复制一份可能把盘撑满
            shutil.move(p, os.path.join(bak, os.path.basename(p)))
        log(job, "  已挪走 %d 个旧断点到 %s，这次会用底模从头训。" % (len(ckpts), bak))
    # ★ 光挪断点不够：`assets/weights/<exp>_e<N>_s<M>.pth` 是**每一轮存档**留下的
    #   中间权重，上一轮训到 300 的话那里就躺着 e165…e300 一堆。不挪走的话：
    #     · 界面/接口问"训到第几轮"时会被这些旧文件带偏（实测：ckpt 读到 150，
    #       而权重文件名里最大是 300，对不上）；
    #     · 万一最终权重没写成，挑权重的逻辑还可能挑到旧的那一份 —— 那就是
    #       "重训了却还在用废模型"，比不重训更坑。
    if restart:
        wdir = os.path.join(RVC_ROOT, "assets", "weights")
        stale = [os.path.join(wdir, f) for f in os.listdir(wdir)
                 if f.startswith(exp) and f.endswith(".pth")] if os.path.isdir(wdir) else []
        if stale:
            wbak = os.path.join(rvc_exp0, "_从零训的旧断点")
            os.makedirs(wbak, exist_ok=True)
            for p in stale:
                # 同理用移动；同名的新权重随后会被训练覆写回来
                shutil.move(p, os.path.join(wbak, os.path.basename(p)))
            log(job, "  已挪走 %d 个上一轮的中间权重到 %s（避免和新训的混在一起）"
                % (len(stale), wbak))
    if restart and was_scratch:
        log(job, "  旧模型是从零训的，这次会换成「底模 + 你的素材」重训。")

    done = 0 if restart else rvc_trained_epochs(rid)
    if done and done >= epochs:
        # 以前的文案让用户「去加素材」，这是错的 —— RVC 本来就支持续训，
        # 缺的只是「把目标调高」。用户问的正是"训完一次还能不能接着训"，
        # 所以这里必须直接告诉他能怎么做，而不是把他支走。
        raise Fail(
            "这个角色的歌声模型已经训到 %d 轮了，你要的 %d 轮没有更多可训的轮数。\n"
            "RVC 的轮数是「训到第几轮」，不是「再训几轮」——同一份素材接着训是有效的，\n"
            "但目标必须比已经训过的轮数大。想把音色继续练细：\n"
            "  · 把「训练质量」选「自定义…」，轮数填 %d（或更高，上限 5000），\n"
            "    会从第 %d 轮接着往下训；\n"
            "  · 轮数不变的话，重训得到的是同一水平的模型，不会有额外提升。\n"
            "（素材本身没变时，提升主要来自更干净的干声 / 更多素材。）"
            % (done, epochs, max(done * 2, done + 50), done))

    rvc_exp = os.path.join(RVC_ROOT, "logs", exp)
    os.makedirs(os.path.join(rvc_exp, "0_gt_wavs"), exist_ok=True)

    job["stage"] = "RVC 预处理"
    job["pct"] = 12
    save_job(job)
    rc, out = run(job, '"%s" -m train.preprocess "%s" %s 4 "%s" True 3.7'
                  % (RVC_PY, dataset, int(sr[:-1]) * 1000,
                     os.path.join(RVC_ROOT, "logs", exp)),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        raise Fail("RVC 预处理失败（退出码 %d）。\n%s" % (rc, out[-800:]))

    # 提 F0（基频）：这一版 RVC 的脚本在 train/dataset/ 下，签名是
    #   extract_f0.py cuda <卡数> <第几张卡> <卡号> <exp目录> <is_half>
    # 之前写成 train/extract_f0_print.py 是照老版本的记忆，那个脚本早没了。
    n_p = max(1, min(os.cpu_count() or 4, 4))
    job["stage"] = "RVC 提取基频"
    job["pct"] = 32
    save_job(job)
    rc, out = run(job, '"%s" train/dataset/extract_f0.py cuda 1 0 0 "%s" True'
                  % (RVC_PY, os.path.join(RVC_ROOT, "logs", exp)),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        raise Fail("RVC 提取基频失败（退出码 %d）。\n%s" % (rc, out[-800:]))

    # 提 HuBERT 特征：extract_hubert_feature.py <device> <卡数> <第几张> <卡号> <exp> <version> <is_half>
    job["stage"] = "RVC 提取音色特征"
    job["pct"] = 52
    save_job(job)
    rc, out = run(job, '"%s" train/dataset/extract_hubert_feature.py cuda 1 0 0 "%s" %s True'
                  % (RVC_PY, os.path.join(RVC_ROOT, "logs", exp), arch),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        raise Fail("RVC 提取音色特征失败（退出码 %d）。\n%s" % (rc, out[-800:]))

    # ---- 训练前置：WebUI 在开训前会写 config.json 和 filelist.txt，
# 直接调 train.py 是绕不过这两份文件的（utils.get_hparams 强制读 config.json），
# 所以这里照 RVC WebUI 的同一套规则生成，不能自己发挥。
# ----
    exp_dir = os.path.join(RVC_ROOT, "logs", exp)
    cfg_src = os.path.join(RVC_ROOT, "configs", arch, "%s.json" % sr)
    with open(cfg_src, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.pop("speaker_info", None)          # 单说话人模式不需要
    with open(os.path.join(exp_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    gt = os.path.join(exp_dir, "0_gt_wavs")
    # ★ 特征目录名跟着架构变（RVC 自己的规则，见 extract_hubert_feature.py）：
    #     v1 → 3_feature256（256 维）/ v2 → 3_feature768（768 维）
    fea = os.path.join(exp_dir, "3_feature256" if arch == "v1" else "3_feature768")
    f0 = os.path.join(exp_dir, "2a_f0")
    f0nsf = os.path.join(exp_dir, "2b-f0nsf")
    names = sorted(os.path.splitext(x)[0] for x in os.listdir(gt) if x.endswith(".wav"))
    lines, skipped = [], 0
    for n in names:
        if not (os.path.exists(os.path.join(fea, n + ".npy"))
                and os.path.exists(os.path.join(f0, n + ".wav.npy"))
                and os.path.exists(os.path.join(f0nsf, n + ".wav.npy"))):
            skipped += 1
            continue
        lines.append("%s/%s.wav|%s/%s.npy|%s/%s.wav.npy|%s/%s.wav.npy|0" % (
            gt.replace("\\", "\\\\"), n, fea.replace("\\", "\\\\"), n,
            f0.replace("\\", "\\\\"), n, f0nsf.replace("\\", "\\\\"), n))
    if not lines:
        raise Fail("特征文件不完整（%d 段有音频但缺特征），提特征那一步没成功。" % skipped)
    with open(os.path.join(exp_dir, "filelist.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(job, "  训练清单：%d 条可用%s"
        % (len(lines), "（%d 段缺特征已跳过）" % skipped if skipped else ""))

    # 训练：这版 train.py 自己 parse 命令行（train/utils.py 的 get_hparams），
    # 必填：-se 保存间隔 -te 总轮数 -bs 批 -e 实验名 -sr 采样率 -v 版本；
    # -sw=1 才会把权重存下来（不存就没有 .pth），-l 存 latest，-c 缓存进显存（8G 卡关掉省显存）
    job["stage"] = "RVC 训练"
    job["epochs_total"] = epochs
    job["epochs_done"] = None

    def _rvc_track(line):
        """把 RVC 的轮次进度实时读出来 —— 150 轮要十几分钟，pct 不动的话
        和"卡死了"没区别（用户已经为这个提过一次意见）。

        ★ 这一版 RVC 打印的**不是** `Epoch: 37/150`，而是中文的
          `INFO:r8c323d43:====> 轮次：50 [时间] | (耗时)`
        只有开头有一行 `训练轮次：1 [0%]`。**只看英文 Epoch 的话进度条会一直
        卡在开训那一刻**（实测：跑到第 50 轮了，pct 还停在 58），
        所以两种写法都得认，总数取我们自己传的 -te。
        """
        s = line
        m = re.search(r"Epoch[:\s]+(\d+)\s*/\s*(\d+)", s)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
        else:
            m2 = re.search(r"轮次[：:]\s*(\d+)", s)
            if not m2:
                return
            done = int(m2.group(1))
            total = int(epochs or 0) or int(job.get("epochs_total") or 0)
        if not total:
            return
        if done <= (job.get("epochs_done") or 0):
            # 开训那行 `训练轮次：1 [0%]` 也会被"轮次"正则命中，
            # 不挡住的话进度会被拽回第 1 轮。同一轮的重复输出同理。
            return
        job["epochs_done"] = done
        job["pct"] = max(job.get("pct") or 0, 58 + int(34 * done / max(total, 1)))
        job["stage"] = "RVC 训练 · 第 %d/%d 轮" % (done, total)
        job["detail"] = "第 %d / %d 轮" % (done, total)
        save_job(job)

    # ---- 底模：没有它，训出来的模型不会跟调（详见上面的长注释）----
    # 放在开训**之前**，因为下载要一两分钟，用户得看见它在干什么。
    job["stage"] = "准备 RVC 底模"
    job["pct"] = max(job.get("pct") or 0, 58)
    save_job(job)
    pg, pd = ensure_rvc_pretrained(job, arch, sr, pretrain)
    if not (pg and pd):
        # 硬失败而不是"没底模也接着训"：从零训出来的模型听起来就是"没法听"，
        # 而且用户完全没法自己定位原因（他会以为是自己素材不好）。
        # 宁可挡在这里让他知道，也不要交给他一个废模型。
        raise Fail(
            "RVC 底模（预训练模型）没准备好，训练不能开始。\n"
            "没有底模就是从随机初始化开始训，训出来的歌声模型**不会跟着旋律唱**"
            "（输出卡在角色自己的音区、变调完全无效）——这正是「唱出来的歌没法听」"
            "最主要的原因，而且事后极难排查。\n"
            "请手动下载这两个文件放到：\n"
            "  %s\n"
            "  %s\n"
            "下载地址（任选一个源）：\n%s"
            % (rvc_pretrained_paths(arch, sr, pretrain)[0],
               rvc_pretrained_paths(arch, sr, pretrain)[1],
               "\n".join("  %s/pretrained%s/f0%s%s.pth"
                         % (h, "_v2" if arch == "v2" else "", x, sr)
                         for h in RVC_PRETRAIN_HOSTS for x in "GD")))
    # 光"文件在"不够 —— 版本/采样率选错时文件照样在位，但加载不进架构，
    # 训练会静默退化成"没底模"。所以开训前必验一次（详见 check_rvc_pretrained）。
    begin_step(job, "准备 RVC 底模", "歌声模型要从一个预训练过的底模上继续练，"
                                     "否则学不会跟旋律")
    ok, msg = check_rvc_pretrained(job, arch, sr, pretrain)
    if not ok:
        end_step(job, False, "底模加载不上：%s" % msg)
        raise Fail(
            "RVC 底模和要训的架构对不上，训练不能开始。\n%s\n"
            "常见原因是版本或采样率选错（%s/%s 要用 assets/pretrained%s/ 下的 "
            "f0G%s.pth + f0D%s.pth）。换成对得上的底模再试。"
            % (msg, arch, sr, "_v2" if arch == "v2" else "", sr, sr))
    log(job, "  底模就绪：%s / %s（%s）" % (os.path.basename(pg), os.path.basename(pd), msg))
    end_step(job, True, "已就位且加载正常（%s）" % os.path.basename(pg))
    used_pretrained = True
    job["stage"] = "RVC 训练"

    # 训练：这版 train.py 自己 parse 命令行（train/utils.py 的 get_hparams），
    # 必填：-se 保存间隔 -te 总轮数 -bs 批 -e 实验名 -sr 采样率 -v 版本；
    # -sw=1 才会把权重存下来（不存就没有 .pth），-l 存 latest，-c 缓存进显存（8G 卡关掉省显存）
    # ★ -pg/-pd 是底模，没有它就从随机初始化训 —— 见上面「RVC 底模」的长注释。
    # ★ -bs 跟着架构走：v2 是 768 维特征 + 4 个判别器，官方明说"更吃显存"，
    #   本机是 8GB 卡，v1/40k 用 4 跑得很稳，v2 就得往下让（见 RVC_ARCH_BS）。
    # ★ 再叠一层采样率上限（见 RVC_SR_BS_CAP）：48k 在**显存不够**时会静默卡死
    #   （GPU 99% + tfevents 十分钟不写），比报 OOM 更难查。
    #   但这层限制本质是显存，所以先问一句"现在还剩多少"，够就放回默认批大小。
    _cap = RVC_SR_BS_CAP.get(str(sr).lower())
    if _cap:
        _free = _vram_free_mb()
        if _free is not None and _free >= RVC_48K_BS2_FREE_MB:
            log(job, "  显存还剩 %d MB（够 48k 用默认批大小）⇒ 不再压到 %d。"
                     % (_free, _cap))
            _cap = None
        else:
            log(job, "  显存只剩 %s MB，48k 用默认批大小约需 %d MB ⇒ 压到 bs=%d。"
                     "关掉其它占显存的程序再训会快很多（实测能快 5 倍）。"
                     % (_free if _free is not None else "?", RVC_48K_BS2_FREE_MB, _cap))
    _bs = min(RVC_ARCH_BS.get(arch, 4), _cap or 99)
    _train_cmd = ('"%s" -m train.train -e "%s" -sr %s -f0 1 -bs %d -te %d -se 15 '
                  '-g 0 -sw 1 -l 1 -c 0 -v %s -pg "%s" -pd "%s"'
                  % (RVC_PY, exp, sr, _bs, epochs, arch, pg, pd))
    if _bs < 4:
        log(job, "  %s/%s 比 v1 吃显存，批大小已按 %d 跑（v1/40k 是 4）；"
                 "如果中途报显存不够，再往下调。" % (arch, sr, _bs))
    rc, out = run(job, _train_cmd, cwd=RVC_ROOT, env=rvc_env,
                  timeout=21600, on_line=_rvc_track)
    # ★ 见 _rvc_spec_race：这是 RVC 自己的偶发写竞争，而且重试一次基本就好。
    #   不接住的话，用户看到的是第 1 轮就崩 + 一句英文 PermissionError。
    for _retry in (1, 2):
        if rc == 0 or not _rvc_spec_race(out):
            break
        log(job, "  [提示] RVC 在准备音频缓存时撞上文件占用（它自己的老问题，"
                 "Windows 上偶发），自动重来一次（第 %d 次）" % _retry)
        # 进度条得从头算：不清掉的话 _rvc_track 会把重跑的第 1 轮当成"没前进"给挡掉
        job["epochs_done"] = None
        job["stage"] = "RVC 训练"
        save_job(job)
        time.sleep(3)
        rc, out = run(job, _train_cmd, cwd=RVC_ROOT, env=rvc_env,
                      timeout=21600, on_line=_rvc_track)
    if rc != 0:
        raise Fail("RVC 训练失败（退出码 %d）。\n%s" % (rc, out[-1500:]))

    # 建特征索引库：train_index.py <实验名> <版本> <索引输出目录> <CPU数>
    # 没有索引时唱歌会自动降级 index-rate 0，有索引音色细节更足
    index_root = os.path.join(RVC_ROOT, "assets", "indices")
    os.makedirs(index_root, exist_ok=True)
    job["stage"] = "RVC 建立索引"
    job["pct"] = max(job.get("pct") or 0, 92)
    job["detail"] = "训练完成，正在建立索引"
    save_job(job)
    rc, out = run(job, '"%s" train/train_index.py "%s" %s "%s" %d'
                  % (RVC_PY, exp, arch, index_root,
                     max(1, min(os.cpu_count() or 4, 4))),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        log(job, "  [warn] 索引没建成（退出码 %d），唱歌时会自动用 index-rate 0：%s"
            % (rc, (out or "")[-200:]))

    weights = os.path.join(RVC_ROOT, "assets", "weights")
    os.makedirs(weights, exist_ok=True)
    found = [f for f in os.listdir(weights) if f.startswith(exp) and f.endswith(".pth")]
    if not found:
        raise Fail("RVC 训练结束了但没找到权重文件。")
    # 选权重必须**按轮数数值排序**，不能按文件名字典序。
    #
    # RVC 每次到 save_every 存一份 `<exp>_e<轮数>_s<步数>.pth`，训完再存一份不带
    # 轮数的 `<exp>.pth`（那才是真正的最终权重，process_ckpt.py 的 savee 用
    # hps.name 直接命名）。以前这里写的是 `sorted(...)[-1]` —— 字典序：
    #     "..._e150_..." < "..._e90_..."      e150 排在 e90 前面！
    #     "..._e300_..." < "..._e90_..."     e300 也排在 e90 前面！
    # 所以训满 300 轮，角色绑定的却是 e90 那份，白训 210 轮。
    # 这不是理论问题：本机 data/roles.json 里 r8c323d43 就是 epochs=300
    # 却绑着 r8c323d43_e90_s1620.pth。
    best = pick_rvc_weight(found, base_dir=weights)
    if not best:
        raise Fail("RVC 训练结束了但没能从权重里挑出可用的一份：%s"
                   % ", ".join(sorted(found)[:6]))
    # 记下"这次训的时候有没有底模"：以后唱歌时就能直接说
    # "你手上的模型是从零训的，唱出来不跟调是正常的，重训一次就好"
    # —— 否则用户只会觉得是自己的素材不行，永远找不到真正的原因。
    if not bind:
        # 对照实验模式：产物留在 assets/weights/ 供 A/B，**不动角色现有模型**。
        # 为什么需要这个模式：换底模 / 换轮数这类实验要能跑完还能退回去。
        # 一旦绑了角色，"实验版效果更差"就得手工改 roles.json 恢复。
        log(job, "  [实验模式] 不绑定到角色 —— 产物：%s" % best)
        log(job, "  声音模型（未绑定）：%s（%d 轮）%s"
            % (best, _rvc_weight_epoch(best) or epochs,
               "（含索引库）" if _find_rvc_index(best) else "（无索引库）"))
        job["experiment_model"] = best
        return best
    role = mutate_role(rid, lambda r: r.__setitem__(
        "rvc", {"model": best, "epochs": epochs, "at": _now(),
                "weight_epoch": _rvc_weight_epoch(best),
                "pretrained": bool(used_pretrained),
                "index": bool(_find_rvc_index(best))}))
    if role["rvc"].get("weight_epoch") not in (None, epochs):
        log(job, "  [warn] 绑定的权重实际是 %s 轮（不是刚训的 %d 轮）"
            % (role["rvc"]["weight_epoch"], epochs))
    log(job, "  歌声模型：%s（%d 轮）%s"
        % (best, role["rvc"].get("weight_epoch") or epochs,
           "（含索引库，音色细节更足）" if role["rvc"]["index"]
           else "（无索引库，唱歌时自动用 index-rate 0）"))
    # 训练完顺手收掉这一轮的中间存档（只留最近几份）。
    #
    # ★ 不只是为了省空间：RVC 下一轮续训时，上一轮的 `<exp>_e<N>_s<M>.pth`
    #   还赖在 assets/weights 里，问"训到第几轮"会被它们带偏 ——
    #   已经踩过一次：断点读到 150 轮，权重文件名里最大却是 300，对不上。
    try:
        r = prune_rvc_archives(exp)
        if r.get("freed"):
            log(job, "  已顺手清掉 %d 份中间存档，释放 %s"
                % (len(r.get("deleted") or []), r.get("freed_s")))
    except Exception as e:
        log(job, "  [warn] 清理中间存档时出错（不影响本次训练）：%s" % e)
    return role["rvc"]


def music_fetch_job(job, item_id, title=None):
    """下载一首歌到本地曲库并做时长体检，返回落盘信息。"""
    import music_src

    job["stage"] = "解析下载地址"
    job["pct"] = 10
    log(job, "解析：%s" % (title or item_id))
    try:
        res = music_src.fetch(item_id, timeout=300)
    except Exception as e:
        raise Fail("下载失败：%s" % e)

    job["stage"] = "校验音频"
    job["pct"] = 60
    dur = music_src.probe_duration(res["path"])
    log(job, "已下载 %s（%.1f MB）" % (res["path"], res["size"] / 1048576.0))
    if dur <= 0:
        # 时长量不出来只警告不失败：ffmpeg 缺失不该阻断导歌。
        log(job, "  [warn] 量不到时长（ffmpeg 不可用），仍按可用处理")
    else:
        log(job, "  时长：%.1f 秒" % dur)

    song = {
        "title": title or os.path.basename(res["path"]),
        "path": res["path"],
        "size": res["size"],
        "dur": round(dur, 1),
        "src": res["src"],
        "item": item_id,
        "at": _now(),
    }
    add_library_song(song)
    job["pct"] = 100
    log(job, "已加入本地曲库：%s" % song["title"])
    return song


LIBRARY_JSON = os.path.join(DATA, "music_library.json")
_LIBRARY_LOCK = threading.Lock()


def load_library():
    try:
        with open(LIBRARY_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"songs": []}


def save_library(db):
    with _LIBRARY_LOCK:
        tmp = LIBRARY_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LIBRARY_JSON)


def add_library_song(song):
    db = load_library()
    # 同一个曲目（item id）只留一条：文件路径带时间戳，光按 path 去重拦不住重复下载。
    item = song.get("item")
    keep = [s for s in db["songs"]
            if not (item and s.get("item") == item)
            and s.get("path") != song.get("path")]
    db["songs"] = keep
    db["songs"].insert(0, song)
    db["songs"] = db["songs"][:200]
    save_library(db)
    return song

# ================================================================ 存储管理
#
# 为什么要有这一段：训一次歌声模型会在两个地方各留下一串 55MB 的权重
# （RVC 的 assets/weights 每 15 轮一份 + logs/<exp> 里的 G/D 断点），
# 每拆一个视频又会在 data/jobs/<id>/work/ 下留下 250MB 中间产物，
# 传上来的原片更是动辄 700MB。跑十几次就是十几 G —— 但**用户没有任何入口
# 去删它们**，只能自己进资源管理器翻三层目录猜哪个能删；删错（把角色正在
# 用的那一份删了）模型当场就废。
#
# 所以这段做的事：把散在几个根目录的占用全扫出来，按「删了会怎样」分类，
# 把**正在被角色引用的那一份锁死**，剩下的才允许勾，删完再留一份清单。
#
# ★ 这里刻意不做「移到回收站」：那只是换个地方占着，用户要的是"别占位置"。
#   所以默认真删 —— 代价是不可撤销，所以才必须先分类 + 上锁 + 二次确认，
#   而不是给用户一个「一键清空」。
STORAGE_JSON = os.path.join(DATA, "storage.json")
TRASH_LOG_DIR = os.path.join(DATA, "_trash_log")

# 训练完自动保留几个中间存档（0 = 只留最终权重）。
# 为什么要留而不是全删：留最近 2 份，"最新那份反而更差"时还能退回去 ——
# 这不是假设，RVC 训到后期偶尔会过拟合变尖。
KEEP_RVC_ARCHIVES = 2
# 分离缓存上限（GB）。一首 4 分钟的歌约 100MB，20 首就 2G。
CACHE_MAX_GB = 3.0

# RVC 训练中间产物里"重训时会自动重建"的那几个目录。
# 不能写 logs/<exp> 整体：那里面还躺着 G_/D_ 断点，删了就没法续训了。
RVC_PREP_DIRS = ("0_gt_wavs", "1_16k_wavs", "2a_f0", "2b-f0nsf",
                 "3_feature256", "5_wav32")


def _humansize(n):
    n = float(n or 0)
    if abs(n) < 1024.0:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
    return "%.1f PB" % n


def _path_size(p):
    """文件或目录的真实占用（只看 st_size，不读内容）。"""
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except Exception:
            return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _mt_s(p):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    except Exception:
        return "-"


def storage_settings():
    d = {"keep_archives": KEEP_RVC_ARCHIVES, "auto_prune": True,
         "cache_max_gb": CACHE_MAX_GB}
    try:
        with open(STORAGE_JSON, "r", encoding="utf-8") as f:
            d.update(json.load(f))
    except Exception:
        pass
    return d


def save_storage_settings(patch):
    d = storage_settings()
    d.update(patch or {})
    os.makedirs(DATA, exist_ok=True)
    tmp = STORAGE_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORAGE_JSON)
    return d


def _rvc_weights_dir():
    return os.path.join(RVC_ROOT, "assets", "weights")


def _rvc_logs_dir():
    return os.path.join(RVC_ROOT, "logs")


def _running_job_ids():
    """正在跑/排队/暂停的任务 —— 它们的目录正在被写，删了任务会炸。"""
    return set(j.get("id") for j in list_jobs()
               if j.get("state") in ("running", "queued", "paused"))


def _protected_map():
    """不可删路径 -> {"why": 原因, "kind": 锁的类型}。

    ★ 最容易漏的就是「角色正在用的那份权重」：删了它，界面上角色还在、唱歌
      按钮还能点，一跑才报"找不到模型"。用户不会想到是自己刚勾掉的那项造成的。
      所以必须在这里显式锁住，并写清是谁在用。

    ★ kind 决定这把锁能不能被"强制解锁"：
        role       —— 只是某个角色在用，用户想连模型一起删是可以的（他会重训）
        pretrained —— 底模，删了要重新下载（本机 huggingface 直连 000），永远不解锁
        job        —— 任务正在跑，删了任务会炸，永远不解锁

    ★ 值写成 {"why","kind"} 而不是光一个字符串，就是为了上面这件事。
      但外面（门禁的负对照等）传进来的可能还是老的字符串形式，
      所以读的地方一律兼容两种：字符串一律按"硬锁"处理（不给解锁）。
    """
    prot = {}

    def _lock(path, why, kind):
        prot[os.path.abspath(path)] = {"why": why, "kind": kind}

    db = load_roles()
    roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
    roles = roles if isinstance(roles, list) else list(roles.values())
    for r in roles:
        name = r.get("name") or r.get("id")
        rv = r.get("rvc") or {}
        if rv.get("model"):
            _lock(os.path.join(_rvc_weights_dir(), rv["model"]),
                  "角色「%s」正在用它唱歌" % name, "role")
            # 同名索引库：RVC 靠 _find_rvc_index 匹配，名字带 IVF 参数拼不出来
            ix = _find_rvc_index(rv["model"])
            if ix:
                _lock(ix, "角色「%s」的歌声索引库" % name, "role")
        # ★ 说话模型在 r["model"]["ckpt"]，**不是** r["gsv"]["weight"]。
        #   读错字段的后果已经发生过一次：保护表压根没锁住它，清理时
        #   角色正在用的那份 ckpt 被当成"没人用的旧存档"删掉了，
        #   于是角色卡片照样显示"已训练"，一点朗读就崩在找不到模型。
        #   而且 ckpt 记的是**带目录的相对路径**（"GPT_weights_v5turbo/xxx.ckpt"），
        #   直接 join(GPT_WEIGHT_DIR, ckpt) 会拼出两层重复目录 ⇒ 锁到空气上。
        #   所以两种拼法都锁（多锁几个不存在的路径没有副作用，
        #   少锁一个就又是上面那个事故）。
        ck = (r.get("model") or {}).get("ckpt")
        if ck:
            ck = ck.replace("\\", "/")
            cands = [os.path.join(GSV_ROOT, ck.replace("/", os.sep)),
                     os.path.join(GPT_WEIGHT_DIR, os.path.basename(ck))]
            if os.path.isabs(ck):
                cands = [ck]
            for p in cands:
                _lock(p, "角色「%s」正在用它说话" % name, "role")
    # 底模：所有歌声模型都从它上面长出来，删了以后每次重训都要重新下载
    # （而且不一定下得动 —— huggingface.co 在本机直接 HTTP 000）。
    for v in ("v1", "v2"):
        for s in ("40k", "32k", "48k"):
            gp, dp = rvc_pretrained_paths(v, s)
            for p in (gp, dp):
                _lock(p, "RVC 底模（所有歌声模型的起点）", "pretrained")
    for jid in _running_job_ids():
        _lock(os.path.join(JOBS_DIR, jid), "任务正在运行中", "job")
    return prot


def _lock_kind(path, prot):
    """这把锁是什么类型（role / pretrained / job）。没锁返回空串。

    只有 kind == "role" 的才允许被"强制解锁"—— 因为那只是"删了要重训"，
    而底模和正在跑的任务删了是真的救不回来。
    """
    ap = _nc(path)
    for p, v in (prot or {}).items():
        if _nc(p) == ap or (os.path.isdir(p) and _under(ap, p)):
            return (v or {}).get("kind", "hard") if isinstance(v, dict) else "hard"
    return ""


def _nc(p):
    """Windows 下路径比较要忽略大小写，否则 D:\\a 和 d:\\A 会被当成两个路径。"""
    return os.path.normcase(os.path.abspath(p))


def _under(child, parent):
    try:
        return os.path.commonpath([_nc(parent), _nc(child)]) == _nc(parent)
    except Exception:
        return False


def _lock_for(path, prot):
    """某个路径被保护吗？返回原因（空串=没被保护）。

    ★ 必须查「祖先」：受保护的常常是**目录**（比如正在运行的任务目录
      data/jobs/<id>），而要删的是它下面的 work/。只比对全等的话，
      那一项会被标成"可删"，用户勾了却在真正删的时候才被拒 ——
      界面上就成了"点了没反应"，比一开始标灰更糟。
    """
    ap = _nc(path)
    for p, v in (prot or {}).items():
        if _nc(p) == ap or (os.path.isdir(p) and _under(ap, p)):
            # 兼容老的字符串形式（门禁的负对照会直接塞字符串）：
            # 认不出类型的一律当硬锁，不给解锁。
            return (v or {}).get("why", "") if isinstance(v, dict) else (v or "")
    return ""


def _mk_item(group, path, kind, name=None, note="", lock="", parts=None, exp=""):
    ap = os.path.abspath(path)
    return {
        "id": "%s:%s" % (group, _nc(ap)),
        "group": group,
        "kind": kind,
        # exp：属于哪个训练实验。界面/清理策略要按它分组判断
        # （"这个实验还有没有对应角色" 决定了它的断点能不能删）。
        # 不能靠路径现猜：预处理那一项的 path 就是 logs/<exp> 本身，
        # 取 dirname 会拿到 "logs"，而断点那一项取 dirname 才是 <exp> ——
        # 两种形状混在一个组里，靠猜必然错一次。
        "exp": exp,
        "name": name or os.path.basename(ap.rstrip("\\/")) or ap,
        "path": ap,
        # parts 非空时**只删 parts**（用于"一个目录里的某几个子目录"这种项），
        # 避免为了清预处理特征把整个实验目录（含断点）端掉。
        "parts": [os.path.abspath(x) for x in (parts or [])],
        "size": _path_size(ap) if not parts else sum(_path_size(x) for x in parts),
        "mtime_s": _mt_s(ap),
        "note": note,
        "lock": lock,
        "safe": not lock,
        # suggest = 「一键勾选安全项」会选上它：只选删了绝不心疼的那些
        "suggest": (not lock) and kind in (
            "archive", "ckpt", "prep", "bak", "jobwork", "cache", "trash"),
    }


def _scan_rvc_weights(prot):
    d = _rvc_weights_dir()
    if not os.path.isdir(d):
        return []
    by_exp = {}
    for f in sorted(os.listdir(d)):
        if not f.endswith(".pth"):
            continue
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        m = re.match(r"^(.*?)(?:_e(\d+)_s(\d+))?\.pth$", f)
        exp = m.group(1) if m else f[:-4]
        ep = int(m.group(2)) if (m and m.group(2)) else -1
        by_exp.setdefault(exp, []).append((f, p, ep))
    items = []
    for exp, lst in by_exp.items():
        # 轮数最高的几份算"新的"，其余才是真正的旧存档
        newest = sorted({e for _, _, e in lst if e >= 0}, reverse=True)[:KEEP_RVC_ARCHIVES]
        for _f, p, ep in lst:
            lock = _lock_for(p, prot)
            if ep < 0:
                items.append(_mk_item("rvc_weight", p, "final", lock=lock,
                                      exp=exp,
                                      note="最终权重（角色唱歌用的就是它）"))
            elif ep in newest:
                items.append(_mk_item("rvc_weight", p, "archive", lock=lock,
                                      exp=exp,
                                      note="中间存档 · 第 %d 轮（较新，留着可退回）" % ep))
            else:
                items.append(_mk_item("rvc_weight", p, "archive", lock=lock,
                                      exp=exp,
                                      note="中间存档 · 第 %d 轮（已有更新的，删了没影响）" % ep))
    return sorted(items, key=lambda x: -x["size"])


def _scan_rvc_logs(prot):
    root = _rvc_logs_dir()
    if not os.path.isdir(root):
        return []
    items = []
    for exp in sorted(os.listdir(root)):
        ed = os.path.join(root, exp)
        if not os.path.isdir(ed):
            continue
        for f in sorted(os.listdir(ed)):
            if re.match(r"^[GD]_\d+\.pth$", f):
                p = os.path.join(ed, f)
                items.append(_mk_item("rvc_ckpt", p, "ckpt", exp=exp,
                                      name="%s / %s" % (exp, f),
                                      note="训练断点：删了不能续训，已训好的模型不受影响",
                                      lock=_lock_for(p, prot)))
        prep = [os.path.join(ed, x) for x in RVC_PREP_DIRS
                if os.path.isdir(os.path.join(ed, x))]
        if prep:
            items.append(_mk_item(
                "rvc_ckpt", ed, "prep", parts=prep, exp=exp,
                name="%s / 预处理特征（%d 项）" % (exp, len(prep)),
                note="重训时会自动重新生成，可以放心删",
                lock=_lock_for(ed, prot)))
        bak = os.path.join(ed, "_从零训的旧断点")
        if os.path.isdir(bak):
            items.append(_mk_item("rvc_ckpt", bak, "bak", exp=exp,
                                  name="%s / _从零训的旧断点" % exp,
                                  note="换底模重训时挪走的旧模型，确认新模型能用就可以删",
                                  lock=_lock_for(bak, prot)))
    return sorted(items, key=lambda x: -x["size"])


def _scan_gsv_weights(prot):
    d = GPT_WEIGHT_DIR
    if not os.path.isdir(d):
        return []
    items = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".ckpt"):
            continue
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        lock = _lock_for(p, prot)
        items.append(_mk_item("gsv_ckpt", p, "ckpt", lock=lock,
                              note="说话模型权重%s" % ("" if lock else "（角色没在用）")))
    return sorted(items, key=lambda x: -x["size"])


def _scan_jobs(prot):
    if not os.path.isdir(JOBS_DIR):
        return []
    items = []
    for jid in sorted(os.listdir(JOBS_DIR)):
        d = os.path.join(JOBS_DIR, jid)
        if not os.path.isdir(d):
            continue
        meta = {}
        try:
            with open(os.path.join(d, "job.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
        st = meta.get("state") or "?"
        title = (meta.get("title") or jid)[:26]
        lock = _lock_for(d, prot)
        work = os.path.join(d, "work")
        if os.path.isdir(work):
            note = ("中间产物：成品早就拷进角色目录了，删了不影响已有成品"
                    if st != "failed" else "失败任务的中间产物，没产出任何成品")
            items.append(_mk_item("job", work, "jobwork",
                                  name="%s · %s [%s]" % (jid, title, st),
                                  note=note, lock=_lock_for(work, prot) or lock))
        elif not lock:
            # 只剩日志的任务目录：很小，但也给个入口（默认不勾）
            it = _mk_item("job", d, "joblog",
                          name="%s · %s [%s]" % (jid, title, st),
                          note="任务日志：删了就看不回当时每一步干了什么")
            it["suggest"] = False
            items.append(it)
    return sorted(items, key=lambda x: -x["size"])


def _scan_uploads(prot):
    if not os.path.isdir(UPLOADS):
        return []
    # 曲库里被引用的不能删：删了条目还在，点播放就是空的
    used = set()
    try:
        for s in load_library().get("songs", []):
            if s.get("path"):
                used.add(_nc(s["path"]))
    except Exception:
        pass
    items = []
    for f in sorted(os.listdir(UPLOADS)):
        p = os.path.join(UPLOADS, f)
        if not os.path.isfile(p):
            continue
        lock = _lock_for(p, prot)
        if _nc(p) in used:
            lock = lock or "曲库里还有这首歌，删了就播不了"
        items.append(_mk_item("upload", p, "upload", lock=lock,
                              note="上传的原片 / 原曲"))
    return sorted(items, key=lambda x: -x["size"])


def _scan_cache(prot):
    if not os.path.isdir(SEP_CACHE):
        return []
    items = []
    for k in sorted(os.listdir(SEP_CACHE)):
        p = os.path.join(SEP_CACHE, k)
        if not os.path.isdir(p):
            continue
        items.append(_mk_item("cache", p, "cache",
                              name="分离缓存 %s" % k[:8],
                              note="下次唱同一首歌能直接复用（重算要 1~2 分钟）",
                              lock=_lock_for(p, prot)))
    return sorted(items, key=lambda x: -x["size"])


def _scan_trash(prot):
    d = os.path.join(DATA, "_trash")
    if not os.path.isdir(d):
        return []
    return sorted(
        [_mk_item("trash", os.path.join(d, f), "trash", note="早先留下的残留")
         for f in sorted(os.listdir(d))],
        key=lambda x: -x["size"])


def _scan_pretrained(prot):
    """底模单独一组：摆出来说明它占多少，但**锁死不给删**。

    用户看到两个几百 MB 的 .pth 一定会想删，而删了以后每次重训都得重新下载
    （还不一定下得动）。与其让他删完再来问"为什么训出来的不跟调"，
    不如一开始就写明白。
    """
    items = []
    seen = set()
    for v in ("v1", "v2"):
        gp, dp = rvc_pretrained_paths(v, "40k")
        for p in (gp, dp):
            if os.path.isfile(p) and _nc(p) not in seen:
                seen.add(_nc(p))
                items.append(_mk_item(
                    "pretrained", p, "pretrained",
                    note="RVC 底模：没有它训出来的模型不会跟旋律",
                    lock="底模不能删（所有歌声模型都从它开始训）"))
    return items


STORAGE_GROUPS = [
    {"id": "rvc_weight", "name": "歌声模型权重", "root": _rvc_weights_dir,
     "desc": "RVC 每 15 轮存一份 55MB，训完再存一份最终的。除了角色正在用的和最近两份，其余都是纯占地方。"},
    {"id": "rvc_ckpt", "name": "训练断点与预处理特征", "root": _rvc_logs_dir,
     "desc": "断点用于续训（删了不能续训，但已训好的模型不受影响）；预处理特征重训时会自动重算。"},
    {"id": "gsv_ckpt", "name": "说话模型权重", "root": lambda: GPT_WEIGHT_DIR,
     "desc": "GPT-SoVITS 每轮一份，一份 155MB。角色正在用的那份会被锁住。"},
    {"id": "job", "name": "任务中间产物", "root": lambda: JOBS_DIR,
     "desc": "每拆一个视频约 250MB。成品早就拷进角色目录了，这里只是过程文件。"},
    {"id": "upload", "name": "上传的原片", "root": lambda: UPLOADS,
     "desc": "原始视频/音频，最大的一项。曲库里还在用的会被锁住。"},
    {"id": "cache", "name": "人声分离缓存", "root": lambda: SEP_CACHE,
     "desc": "换了模型或原曲就失效，清掉只是下次重算一遍（1~2 分钟）。"},
    {"id": "pretrained", "name": "底模（不可删）", "root": lambda: os.path.join(RVC_ROOT, "assets"),
     "desc": "所有歌声模型的起点，锁死。"},
    {"id": "trash", "name": "回收站残留", "root": lambda: os.path.join(DATA, "_trash"),
     "desc": "以前留下的残留文件。"},
]

_SCANNERS = {
    "rvc_weight": _scan_rvc_weights,
    "rvc_ckpt": _scan_rvc_logs,
    "gsv_ckpt": _scan_gsv_weights,
    "job": _scan_jobs,
    "upload": _scan_uploads,
    "cache": _scan_cache,
    "pretrained": _scan_pretrained,
    "trash": _scan_trash,
}


def _storage_allowed_roots():
    """删除白名单的根。不在这里面的一律拒绝 —— 这是安全底线：
    万一前端被改坏或有人手搓请求，也删不到 data/roles 那种地方。"""
    roots = []
    for g in STORAGE_GROUPS:
        try:
            r = os.path.abspath(g["root"]())
        except Exception:
            continue
        if os.path.exists(r):
            roots.append(r)
    return roots


def _dangling_refs():
    """角色记录里指向的权重文件**已经不在**的那些 —— 返回给人看的提示列表。

    为什么会到这一步：文件被手工删了、被上一版有 bug 的清理删了、
    或者整个 GPT-SoVITS 目录挪过位置。共同点是角色卡片依然写着"已训练"，
    一点朗读/唱歌才报"找不到模型" —— 用户只会以为功能坏了。
    这里把它列出来，界面上就能直接说"这个要重训"，而不是让人去猜。
    """
    out = []
    try:
        db = load_roles()
        roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
        roles = roles if isinstance(roles, list) else list((roles or {}).values())
    except Exception as e:
        return ["角色库读不出来，没法检查模型是否还在：%s" % e]
    for r in roles or []:
        nm = r.get("name") or r.get("id")
        rv = r.get("rvc") or {}
        if rv.get("model") and not os.path.isfile(
                os.path.join(_rvc_weights_dir(), rv["model"])):
            out.append("角色「%s」的歌声模型 %s 已经不在磁盘上了，唱歌前需要重训。"
                       % (nm, rv["model"]))
        sv = r.get("seedvc") or {}
        if sv.get("ckpt") and not os.path.isfile(seedvc.ckpt_abs(sv["ckpt"])):
            # 微调档位被删掉时，界面上还写着"用 ft600 唱歌"，点下去才发现唱不了
            # ⇒ 和歌声模型一样，在这里当面说出来（唱歌时会降级回 RVC 并写进日志）。
            out.append("角色「%s」的音色微调档 %s 已经不在磁盘上了，"
                       "可以换一档或重新微调。" % (nm, sv["ckpt"]))
        g = r.get("model") or {}
        if g.get("ckpt"):
            p = os.path.join(GSV_ROOT, str(g["ckpt"]).replace("\\", "/")
                             .replace("/", os.sep))
            if not os.path.isfile(p):
                p2 = os.path.join(GPT_WEIGHT_DIR, os.path.basename(str(g["ckpt"])))
                if not os.path.isfile(p2):
                    out.append("角色「%s」的说话模型 %s 已经不在磁盘上了，朗读前需要重训。"
                               % (nm, g["ckpt"]))
    return out


def scan_storage():
    """扫一遍全部可清理项，返回 {groups, total, reclaimable, free, settings}。

    扫十几个 G 的文件元数据要几秒，所以调用方会做缓存，别每帧都调。
    """
    prot = _protected_map()
    out_groups, total, reclaim = [], 0, 0
    for g in STORAGE_GROUPS:
        fn = _SCANNERS.get(g["id"])
        desc = g.get("desc", "")
        try:
            items = fn(prot) if fn else []
        except Exception as e:
            items = []
            desc += "（扫描失败：%s）" % e
        for it in items:
            # 锁的类型要给到界面：只有 role 锁能被"强制解锁"，
            # 界面得知道哪一格勾上会动到正在用的模型，才好把风险写在那一格上。
            it["lock_kind"] = _lock_kind(it["path"], prot) if it.get("lock") else ""
            total += it.get("size") or 0
            if it.get("safe"):
                reclaim += it.get("size") or 0
        out_groups.append({
            "id": g["id"], "name": g["name"], "desc": desc,
            "size": sum(i.get("size") or 0 for i in items),
            "size_s": _humansize(sum(i.get("size") or 0 for i in items)),
            "count": len(items),
            "items": items,
        })
    free = 0
    try:
        import ctypes
        n = ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(HERE), None, None, ctypes.byref(n))
        free = n.value
    except Exception:
        pass
    return {
        "at": time.time(),
        "groups": out_groups,
        # 悬空引用：角色记着"我有模型"，但那个文件已经不在磁盘上了。
        # 这种状态最坑人 —— 界面显示已训练，一点就崩。必须在这里喊出来，
        # 否则用户只会以为"唱歌功能坏了"。
        "warnings": _dangling_refs(),
        "total": total,
        "total_s": _humansize(total),
        "reclaimable": reclaim,
        "reclaimable_s": _humansize(reclaim),
        "free": free,
        "free_s": _humansize(free) if free else "",
        "settings": storage_settings(),
    }


def _delete_block_reason(ap, prot, force=False):
    """空串=可删，否则返回不可删的原因。

    force=True 只放行一种锁：kind == "role"（"某个角色正在用这份模型"）。
    用户明确要求"连之前训练的模型一起删"时，这是他的选择 —— 删完角色会
    如实变回"还没训练"，重训一次就好。
    底模（pretrained）和正在跑的任务（job）**任何情况下都不解锁**：
    前者删了要重新下载而本机 huggingface 直连 000，后者删了任务当场崩。
    """
    if not os.path.exists(ap):
        return "已经不在了"
    why = _lock_for(ap, prot)
    if why and not (force and _lock_kind(ap, prot) == "role"):
        return why
    apn = _nc(ap)
    if not any(_under(apn, r) for r in _storage_allowed_roots()):
        return "不在允许清理的目录里"
    return ""


def _rm_path(p):
    if os.path.isdir(p):
        shutil.rmtree(p)
    else:
        os.remove(p)


def _weight_candidates(rel, base):
    """角色记录里的权重名 -> 它可能在磁盘上的全部路径（归一化后的集合）。

    ★ 必须和 _protected_map 里用的是同一套规则，否则会出现
      "锁的时候锁在 A，删完断引用时去比对 B" —— 文件删掉了、引用还留着，
      角色继续显示"已训练"，一点就崩。两处共用这一个函数就不会再走偏。

    之所以要产生多个候选：说话模型记的是带目录的相对路径
    （"GPT_weights_v5turbo/xxx.ckpt"），而歌声模型记的是纯文件名，
    join 的基准目录也不一样。
    """
    out = set()
    if not rel:
        return out
    rel = str(rel).replace("\\", "/")
    if os.path.isabs(rel):
        out.add(_nc(rel))
        return out
    out.add(_nc(os.path.join(base, rel.replace("/", os.sep))))
    out.add(_nc(os.path.join(base, os.path.basename(rel))))
    return out


def _purge_role_refs(paths):
    """删掉模型文件之后，把角色记录里指向它的字段一起断掉。返回的条目会显示给用户。

    ★ 不做这一步的后果很具体（已经发生过一次）：角色卡片照样显示
      "已训练 · 第 15 轮"，朗读/唱歌按钮照样能点，一跑才崩在"找不到模型文件"；
      而用户不会把这个错和自己半小时前在存储面板勾掉的那一格联系起来。
      ⇒ 删了什么就把引用断在哪，界面立刻如实变回"还没训练，去训一次"。
    """
    gone = set(_nc(p) for p in (paths or []) if p)
    if not gone:
        return []
    hit = []

    def _fix(db):
        roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
        for r in roles or []:
            nm = r.get("name") or r.get("id")
            rv = r.get("rvc")
            if isinstance(rv, dict) and rv.get("model") and \
                    (_weight_candidates(rv["model"], _rvc_weights_dir()) & gone):
                rv["model"] = None
                rv["index"] = False
                rv["weight_epoch"] = None
                hit.append("角色「%s」的歌声模型已删除，需要重训才能再唱歌" % nm)
            g = r.get("model")
            if isinstance(g, dict) and g.get("ckpt") and \
                    (_weight_candidates(g["ckpt"], GPT_WEIGHT_DIR) & gone):
                r["model"] = None
                hit.append("角色「%s」的说话模型已删除，需要重训才能再朗读" % nm)
        return db

    try:
        mutate_roles(_fix)
    except Exception as e:
        # 清引用失败不能把"删除"这件事回滚 —— 文件已经没了。
        # 但要留句话，让人知道还有个尾巴没收拾。
        hit.append("（警告：角色记录没能同步更新：%s）" % e)
    return hit


def storage_delete(targets, dry=False, force=False):
    """真删。targets 里每项可以是路径字符串，也可以是 {"path","parts"}。

    返回 {deleted, failed, freed, freed_s, log, refs}。
    force=True 时允许删掉"角色正在用的模型"（见 _delete_block_reason）。

    ★ 不做"移到回收站"：那只是换个地方占着。不可撤销 ⇒ 每一步都先过
      _delete_block_reason，删完写一份清单（纯文本几 KB，真出问题至少
      知道删过什么）。

    ★ 删完必须过一遍 _purge_role_refs：删掉的若是某个角色正在用的权重，
      角色记录里的引用要一起断掉，否则它永远显示"已训练"，
      点下去才崩在找不到模型。
    """
    prot = _protected_map()
    res = {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B", "log": "",
           "refs": []}
    for t in (targets or []):
        ap = os.path.abspath(t["path"] if isinstance(t, dict) else t)
        parts = [os.path.abspath(x) for x in (t.get("parts") or [])] \
            if isinstance(t, dict) else []
        why = _delete_block_reason(ap, prot, force=force)
        if not why and parts:
            for q in parts:
                why = _delete_block_reason(q, prot, force=force)
                if why:
                    why = "%s -> %s" % (os.path.basename(q), why)
                    break
        if why:
            res["failed"].append({"path": ap, "why": why})
            continue
        todo = parts or [ap]
        sz = sum(_path_size(x) for x in todo)
        if dry:
            res["deleted"].append({"path": ap, "size": sz, "dry": True})
            res["freed"] += sz
            continue
        try:
            for q in todo:
                if os.path.exists(q):
                    _rm_path(q)
        except Exception as e:
            res["failed"].append({"path": ap, "why": str(e)})
            continue
        left = [q for q in todo if os.path.exists(q)]
        if left:
            res["failed"].append({"path": ap, "why": "删了但还在（可能被别的程序占用）"})
            continue
        res["deleted"].append({"path": ap, "size": sz})
        res["freed"] += sz
    res["freed_s"] = _humansize(res["freed"])
    if res["deleted"] and not dry:
        # 只拿**确认删掉**的那些路径去断引用：没删成的不能算，
        # 否则会出现"文件还在、角色记录却被清空"这种凭空的数据丢失。
        res["refs"] = _purge_role_refs([d["path"] for d in res["deleted"]])
    if res["deleted"] and not dry:
        try:
            os.makedirs(TRASH_LOG_DIR, exist_ok=True)
            lg = os.path.join(TRASH_LOG_DIR,
                              "deleted_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
            with open(lg, "w", encoding="utf-8") as f:
                f.write("# VoiceStudio 清理记录 %s\n"
                        % time.strftime("%Y-%m-%d %H:%M:%S"))
                f.write("# 共 %d 项，释放 %s\n"
                        % (len(res["deleted"]), res["freed_s"]))
                for d in res["deleted"]:
                    f.write("%s\t%s\n" % (_humansize(d["size"]), d["path"]))
            res["log"] = lg
        except Exception:
            pass
    return res


def prune_rvc_archives(exp, keep=None, dry=False):
    """训练结束后瘦身：只留最终权重 + 最近 keep 份中间存档。

    为什么必须做：RVC 每 15 轮存一份 55MB，训 300 轮就是 20 份 ≈ 1.1G，
    而且**下一轮续训时它们还赖在那儿**，界面问"训到第几轮"会被带偏
    （已经踩过一次：ckpt 读到 150，权重文件名里最大却是 300）。
    """
    keep = KEEP_RVC_ARCHIVES if keep is None else int(keep)
    d = _rvc_weights_dir()
    if not os.path.isdir(d):
        return {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B"}
    prot = _protected_map()
    arcs = []
    for f in sorted(os.listdir(d)):
        m = re.match(r"^%s_e(\d+)_s(\d+)\.pth$" % re.escape(exp), f)
        if not m:
            continue
        p = os.path.join(d, f)
        if os.path.isfile(p):
            arcs.append((int(m.group(1)), p))
    arcs.sort()
    if keep > 0 and len(arcs) > keep:
        arcs = arcs[:len(arcs) - keep]
    elif keep <= 0:
        pass
    else:
        arcs = []
    # 双保险：正在被角色用的那份，就算它在名单里也不删
    targets = [p for _, p in arcs if _nc(p) not in [_nc(k) for k in prot]]
    return storage_delete(targets, dry=dry)


def prune_gsv_weights(rid, keep=2, dry=False):
    """说话模型瘦身：只留最近 keep 份中间存档（角色正在用的那份永远不删）。

    GPT-SoVITS 每 5 轮存一份 155MB，训 30 轮就 6 份 ≈ 1G。它的 ckpt 名是
    `<rid>-e<轮数>.ckpt`，跟 RVC 那套命名不一样，所以单独写一份。
    """
    d = GPT_WEIGHT_DIR
    if not os.path.isdir(d):
        return {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B"}
    prot = _protected_map()
    arcs = []
    for f in sorted(os.listdir(d)):
        m = re.match(r"^%s-e(\d+)\.ckpt$" % re.escape(rid), f)
        if not m:
            continue
        p = os.path.join(d, f)
        if os.path.isfile(p):
            arcs.append((int(m.group(1)), p))
    arcs.sort()
    if keep > 0 and len(arcs) > keep:
        arcs = arcs[:len(arcs) - keep]
    elif keep > 0:
        arcs = []
    protn = set(_nc(k) for k in prot)
    return storage_delete([p for _, p in arcs if _nc(p) not in protn], dry=dry)


def prune_cache(max_gb=None, dry=False):
    """分离缓存 LRU：超上限就从最旧的开始删。"""
    st = storage_settings()
    lim = float(max_gb if max_gb is not None else st.get("cache_max_gb", CACHE_MAX_GB))
    if not os.path.isdir(SEP_CACHE):
        return {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B"}
    dirs = [os.path.join(SEP_CACHE, k) for k in os.listdir(SEP_CACHE)
            if os.path.isdir(os.path.join(SEP_CACHE, k))]
    dirs.sort(key=lambda p: os.path.getmtime(p))
    lim_b = lim * 1024.0 ** 3
    total = sum(_path_size(p) for p in dirs)
    targets = []
    for p in dirs:
        if total <= lim_b:
            break
        total -= _path_size(p)
        targets.append(p)
    return storage_delete(targets, dry=dry)
