r"""
本地音乐库：扫描你自己的音乐目录，把歌导进曲库。

为什么单独做这个：在线源再多也是"别人的开放授权曲"，风格有限。而你手里
本来就有歌 —— 下载的、自己存的、录音棚导出的。**这些是你自己的文件，
导入自己机器上的曲库没有任何第三方问题**，所以这条是本模块的正当用途。

设计上踩过的两个坑，都写在代码注释里，别再犯：

1) **扩展名会骗人，不能信**。实测本机 `Downloads\7月5日.m4a` 的魔数是
   `ff f1 ...` —— 那是 **AAC ADTS 裸流**，不是 m4a（真 m4a 的第 5-8 字节
   是 `ftyp`）。按扩展名走会送给错解码器。所以这里一律**先嗅魔数再定类型**。

2) **各音乐客户端的下载目录位置不同，很多是加密缓存**（不能直接用）。
   本模块只认标准音频容器：mp3 / flac / wav / m4a / aac / ogg / opus / wma…
   扫到加密格式（.qmc / .mflac / .kgg / .ncm 等）会**跳过并注明原因**，
   不猜、不假装能用 —— 需要的话请在客户端里另存/导出成标准格式再导入。

导入方式：**只登记路径，不复制文件**。理由是导入上百首歌如果复制一份，
会白白吃掉几个 GB；而且保留原文件位置，才能继续做分轨编辑（用户明确要
"留好分开唱的空间"）。代价是原文件移动/改名后链接失效，所以扫描时会
记录 size+mtime，用它们判断是否已被移动，在曲库里标出来。
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

AUDIO_EXTS = (".mp3", ".flac", ".wav", ".m4a", ".mp4", ".aac", ".ogg",
              ".opus", ".wma", ".aiff", ".aif", ".ape", ".alac", ".m4b")
ENCRYPTED_HINT = {
    ".ncm": "加密容器（.ncm），请在客户端里导出成 mp3/flac 后再导入",
    ".qmc": "QQ音乐加密缓存（.qmc），需要在客户端内另存为标准格式",
    ".mflac": "QQ音乐加密容器（.mflac），同上",
    ".kugou": "酷狗加密缓存，同上",
    ".kwm": "酷我加密缓存，同上",
    ".xm": "虾米加密缓存，同上",
}

# 音乐客户端的**程序目录**：整棵剪掉，不去里面找歌。
#
# 为什么需要：客户端的安装目录常常**同时**是下载目录的父目录
# （客户端常把歌下在安装目录旁边）。这时递归 walk 会钻进
# 程序目录，捞出 `listenTogetherDialing.wav` / `showListenTogetherEmoticon.wav`
# 这类**客户端自己的提示音** —— 它们真实存在、ffprobe 也量得出时长，
# 但**不是歌**。列给用户的后果是"扫出 9 首，点开一半是系统音效"，
# 比扫不到更劝退（扫不到至少不会骗人）。
#
# 名单按**目录名**匹配（不区分大小写），宁可多列 —— 剪掉的只可能是
# 程序文件，不会是用户自己放的歌。
PROG_DIRS = frozenset((
    # 客户端程序目录
    "resource", "locales", "package", "redist_packages", "swiftshader",
    "aegis", "cache", "log", "nim", "temp", "tempd", "ad", "aioresource",
    "discern", "dumps", "info", "localdata", "localware", "nrtc", "preview",
    "safemode", "update", "library", "statics", "webapp", "font", "skin",
    # 通用
    "node_modules", "__pycache__", ".git", "appdata", "program files",
    "program files (x86)", "windows", "system32", "$recycle.bin",
    "system volume information",
))

# 剪枝只挡得住**新扫描**。历史上（剪枝上线之前）被扫进来并登记进曲库的
# 提示音还躺在 `data/music_library.json` 里，界面上照旧显示成"本地已下载"
# 的歌 —— 用户点「试听」听到一声"叮"，点「导进翻唱台」会拿音效去翻唱。
# 所以还需要一个**按路径**的判定：曲库里这些老条目得能被认出来。
#
# 判定故意收紧（路径必须落在客户端 `resource/` 下 **且** 文件名是那几个
# 提示音前缀），不按扩展名一刀切 —— 否则会误伤用户真放在客户端目录下的歌。
PROG_SOUND_PREFIXES = (
    "listentogether", "showlistentogether", "togethermember",
)


def is_prog_sound(path):
    """这个路径是不是音乐客户端的自带提示音（不是歌）。

    条件（两条都要满足，缺一不判）：
      1. 路径里有 `\\resource\\\\` —— 客户端资源目录，用户的歌不放这；
      2. 文件名（不含扩展名）以 PROG_SOUND_PREFIXES 里某个前缀打头。
    判错的代价不对称：漏判 ⇒ 用户曲库里多一条噪音；误判 ⇒ 用户一首真歌
    被标成"提示音"、可能被清理掉。所以宁可漏判。
    """
    p = (path or "").replace("/", "\\").lower()
    if "\\resource\\" not in p:
        return False
    name = os.path.basename(p)
    stem = os.path.splitext(name)[0]
    return stem.startswith(PROG_SOUND_PREFIXES)


def _magic(path):
    """读文件头判断真实类型。返回 (ext, 说明)。"""
    try:
        with open(path, "rb") as f:
            b = f.read(16)
    except Exception as e:
        return "", "读不了：%s" % str(e)[:40]
    if not b:
        return "", "空文件"
    if b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2",
                                    b"\xff\xfa"):
        return ".mp3", "MP3"
    if b[4:8] == b"ftyp":
        brand = b[8:12].decode("ascii", "replace")
        if brand.startswith("M4A") or brand.startswith("mp42"):
            return ".m4a", "M4A/MP4 音频"
        return ".m4a", "MP4 容器（%s）" % brand
    if b[:4] == b"fLaC":
        return ".flac", "FLAC"
    if b[:4] == b"OggS":
        return ".ogg", "OGG"
    if b[:4] == b"RIFF" and b[8:12] == b"WAVE":
        return ".wav", "WAV"
    if b[:2] == b"\xff\xf1" or b[:2] == b"\xff\xf9":
        # 关键：扩展名写着 .m4a，实际是 AAC ADTS 裸流
        return ".aac", "AAC 裸流（扩展名不可信）"
    if b[:4] == b"\x00\x00\x00\x18" or b[:4] == b"\x00\x00\x00\x1c":
        return ".m4a", "裸 AAC/MP4 片段"
    if b[:4] == b"FORM" and b[8:12] == b"AIFF":
        return ".aiff", "AIFF"
    if b[:4] == b"MAC ":
        return ".ape", "Monkey's Audio"
    if b[:4] == b"wvpk":
        return ".wv", "WavPack"
    if b[:4] == b"\x30\x26\xb2\x75":
        return ".m4a", "ASF/WMA"
    if b[:3] == b"\x1a\x45\xdf":
        return ".webm", "Matroska/WebM 音频"
    return "", "未识别的音频格式"


def _dur(path):
    """用 ffprobe/ffmpeg 量时长；量不到返回 0（不阻断导入）。"""
    import subprocess
    import shutil

    for exe in ("ffprobe", "ffmpeg"):
        p = shutil.which(exe)
        if p:
            try:
                args = ([p, "-v", "quiet", "-show_entries", "format=duration",
                         "-of", "csv=p=0", path] if exe == "ffprobe" else
                        [p, "-i", path, "-f", "null", "-"])
                out = subprocess.run(args, capture_output=True, timeout=25)
                txt = (out.stdout or b"").decode("utf-8", "replace").strip()
                m = re.search(r"([0-9]+\.[0-9]+)", txt)
                if m:
                    return float(m.group(1))
            except Exception:
                pass
    return 0.0


def _valid_size(path, ext):
    """光看魔数会被"假头文件"骗过去 —— 必须再卡一道最小体积。

    实测踩过：`broken.wav`（17 字节文本冒充 RIFF/WAVE）和 `trunc.mp3`
    （17 字节、只有 ID3 头没有音频帧）都能过魔数检查被当成可用音频，
    导进曲库后到合成阶段才炸。魔数只能证明"开头像"，不能证明"这是一首歌"。

    阈值按格式取最保守值：
      - 无帧头的容器（mp3 ID3 / wav）要求 >= 16KB，
        否则连 1 秒立体声 44.1k 都装不下；
      - flac/ape/wv 必须有完整帧头，>= 4KB；
      - 天然高码率的 aac/m4a >= 1KB 即可。
    """
    try:
        size = os.path.getsize(path)
    except Exception:
        return False, 0
    need = {
        ".mp3": 16 * 1024, ".wav": 16 * 1024, ".flac": 4 * 1024,
        ".ape": 4 * 1024, ".wv": 4 * 1024, ".aiff": 4 * 1024,
        ".m4a": 1024, ".aac": 1024, ".ogg": 1024, ".opus": 1024,
        ".webm": 1024, ".m4b": 1024,
    }.get(ext, 16 * 1024)
    if size < need:
        return False, size
    # 扫描期就跳过陈年目录里的空壳：只留真能播的
    return True, size


def scan_dir(root, recursive=True, need_dur=False, limit=4000, skip_dirs=None):
    """扫描目录里的音频文件。递归深度可控，返回真实结果列表。

    skip_dirs：目录名（小写）集合，命中的子目录整棵剪掉不再往下走。

    为什么要这个参数：扫**音乐客户端的安装目录**时（客户端装在
    客户端安装根目录，下载目录就在它下面），整棵 walk 会钻进一堆程序目录，
    实测从里面捞出 `listenTogetherDialing.wav` / `showListenTogetherEmoticon.wav`
    这种**客户端自己的提示音** —— 它们确实是可播放的 wav，但**不是歌**，
    列给用户体验上就是"扫出 9 首，点开一半是系统音效"。
    程序目录名单见 PROG_DIRS。
    """
    root = os.path.abspath(os.path.expanduser(root.strip()))
    found, skipped = [], []
    if not os.path.isdir(root):
        return {"root": root, "ok": False, "err": "目录不存在",
                "items": [], "skipped": []}
    skip = tuple(x.lower() for x in (skip_dirs or ()))
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if not recursive:
            dirnames[:] = []
        elif skip:
            # 前缀匹配：程序目录常带版本号/位宽后缀（实测 `webapp91x64`），
            # 只做精确比对会让它们整棵漏网。
            dirnames[:] = [d for d in dirnames
                           if not any(d.lower().startswith(s) for s in skip)]
        for fn in filenames:
            if n >= limit:
                break
            ext = os.path.splitext(fn)[1].lower()
            full = os.path.join(dirpath, fn)
            if ext in ENCRYPTED_HINT:
                skipped.append({"path": full, "name": fn,
                                "why": ENCRYPTED_HINT[ext]})
                continue
            if ext not in AUDIO_EXTS:
                continue
            n += 1
            try:
                size = os.path.getsize(full)
            except Exception as e:
                skipped.append({"path": full, "name": fn,
                                "why": "读不了：%s" % str(e)[:40]})
                continue
            ext2, desc = _magic(full)
            if not ext2:
                # 扩展名像音频但魔数不是 —— 多半是改过名或损坏，如实标出
                skipped.append({"path": full, "name": fn,
                                "why": desc or "不是可识别的音频文件"})
                continue
            ok_sz, sz = _valid_size(full, ext2)
            if not ok_sz:
                skipped.append({"path": full, "name": fn,
                                "why": "文件只有 %d 字节，只是空壳/假头，不是一首歌"
                                       % sz})
                continue
            item = {
                "path": full,
                "name": os.path.splitext(fn)[0],
                "ext": ext2,
                "kind": desc,
                "size": sz,
                "mtime": int(os.path.getmtime(full)),
                "dur": _dur(full) if need_dur else 0.0,
                "src": "local",
            }
            found.append(item)
        if n >= limit:
            break
    found.sort(key=lambda x: x["path"].lower())
    skipped.sort(key=lambda x: x["path"].lower())
    return {"root": root, "ok": True, "err": None,
            "items": found, "skipped": skipped}


#候选目录。注意顺序：**能真扫到歌的排前面**，纯程序目录排后面。
#
# 候选目录：**能真扫到歌的排前面**，程序目录排后面。
# 教训：列出来就得是真能扫到东西的，不然用户点一次白跑一次。
DEFAULT_DIRS = [
    ("我的音乐", os.path.expandvars(r"%USERPROFILE%\Music")),
    ("下载", os.path.expandvars(r"%USERPROFILE%\Downloads")),
    ("QQ音乐", os.path.expandvars(r"%USERPROFILE%\Music\Tencent\QQMusic")),
    ("酷狗音乐", os.path.expandvars(r"%USERPROFILE%\Music\Kugou")),
    ("QQ音乐缓存(多加密)", os.path.expandvars(r"%APPDATA%\Tencent\QQMusic")),
]


def _is_under(child, parent):
    """child 是 parent 的下级目录吗（自身不算）。"""
    c = os.path.abspath(child).rstrip("\\/").lower()
    p = os.path.abspath(parent).rstrip("\\/").lower()
    return c != p and c.startswith(p + "\\")


def candidate_dirs():
    """列出本机常见音乐目录，标明存在与否，让用户直接点。

    顺手数一下每个目录里到底有几首**真能导入**的歌 —— 有song_count 字段。
    理由：光给"存在/不存在"不够，用户点一个0 首的目录等于白跑一趟。
    数一遍很便宜（只嗅魔数+查大小，不解码），换来的是不误导。
    """
    out = []
    for name, p in DEFAULT_DIRS:
        p = os.path.abspath(p)
        ok = os.path.isdir(p)
        item = {"name": name, "path": p, "exists": ok, "song_count": None}
        if ok:
            try:
                r = scan_dir(p, recursive=True, need_dur=False, limit=3000)
                item["song_count"] = len(r["items"])
            except Exception:
                item["song_count"] = None
        out.append(item)
    return out


def _stat_sig(path):
    try:
        st = os.stat(path)
        return [int(st.st_size), int(st.st_mtime)]
    except Exception:
        return None


def add_files(paths, sig_check=True):
    """把本地文件登记进曲库（不复制文件）。

    sig_check：导入时记下 size+mtime，之后曲库能判断原文件是否还在原地。
    """
    import sys as _s
    if ROOT not in _s.path:
        _s.path.insert(0, ROOT)
    import studio

    added, bad = [], []
    for p0 in paths:
        p = os.path.abspath(os.path.expanduser(p0))
        if not os.path.isfile(p):
            bad.append({"path": p, "why": "文件不存在"})
            continue

        ext2, desc = _magic(p)
        if not ext2:
            bad.append({"path": p, "why": "不是可识别的音频文件"})
            continue
        ok_sz, size = _valid_size(p, ext2)
        if not ok_sz:
            bad.append({"path": p,
                        "why": "文件只有 %d 字节，空壳或假头，不是一首歌" % size})
            continue
        # 时长必须真的量出来。这里曾经图省事写 dur=0.0，结果本地导入的歌
        # 在曲库里全是 0 秒 —— 界面看不出长短，RVC 跑之前也没法预判文件是否正常。
        # （实测 probe_duration 对伪装成 .m4a 的 AAC 裸流能量出 13.47 秒，
        #   所以不是格式不支持，是没去量。）
        try:
            import music_src
            dur = music_src.probe_duration(p)
        except Exception:
            dur = 0.0
        song = {
            "title": os.path.splitext(os.path.basename(p))[0],
            "path": p,
            "size": size,
            "dur": dur,
            "src": "local",
            "item": "local:%s" % p,
            "sig": _stat_sig(p) if sig_check else None,
            "at": studio._now(),
        }
        studio.add_library_song(song)
        added.append(song)
    return {"added": added, "bad": bad}


def check_library():
    """核对曲库条目与磁盘现状，**只读、不写库**。

    之前的recheck_library 有两个真问题：
      a) 全库 grep 零调用方 —— 死代码，界面上根本没有这个功能；
      b) 它把missing/moved 标记 **save_library 持久化**了。
         于是文件临时挪走 → 标记入库 → 文件放回来，标记**不会自动清掉**，
         一首歌会永远挂着"文件丢失"。状态是"每次读时算出来的"，
         不是"发生过的事就永久记账"。

    返回 (标注后的条目, 统计)。标注规则与 music_src.local_library 一致，
    两边必须同口径，否则同一首歌在两个接口里状态会打架。
    """
    import sys as _s
    if ROOT not in _s.path:
        _s.path.insert(0, ROOT)
    import music_src

    out = []
    n_missing = n_changed = 0
    for s in music_src.local_library():
        s = dict(s)
        p = s.get("path") or ""
        sig = s.get("sig")
        if p and os.path.isfile(p) and sig:
            cur = _stat_sig(p)
            if cur and list(cur) != list(sig):
                s["changed"] = True
                s["why"] = "原文件在原位，但大小/时间变过了"
                n_changed += 1
        out.append(s)
        if s.get("missing"):
            n_missing += 1
    return out, {"total": len(out), "missing": n_missing, "changed": n_changed}


def relink(item, new_path):
    """把曲库里某一条指向新的文件位置（用户自己把歌挪了/换了盘）。

    只改曲库记录，不动任何文件。新路径必须过一遍和导入时**同样**的校验 ——
    不然"重新定位"就成了绕过质量闸门的后门（指向加密缓存或 17 字节假文件）。
    """
    import sys as _s
    if ROOT not in _s.path:
        _s.path.insert(0, ROOT)
    import studio

    if not (item or "").strip():
        return {"ok": False, "err": "没说要重新定位哪一条"}
    p = os.path.abspath(os.path.expanduser(str(new_path or "").strip().strip('"')))
    if not os.path.isfile(p):
        return {"ok": False, "err": "这个路径上没有文件：%s" % p}

    ext2, desc = _magic(p)
    if not ext2:
        return {"ok": False, "err": "那不是可识别的音频文件（可能是加密缓存或改错文件了）"}
    ok_sz, size = _valid_size(p, ext2)
    if not ok_sz:
        return {"ok": False, "err": "那文件只有 %d 字节，不是完整的一首歌" % size}

    try:
        import music_src
        dur = music_src.probe_duration(p)
    except Exception:
        dur = 0.0

    db = studio.load_library()
    hit = None
    for s in db.get("songs", []):
        if (s.get("item") or "") == item:
            hit = s
            break
    if hit is None:
        return {"ok": False, "err": "曲库里没有这一条（可能已经被移除了）"}

    old = hit.get("path") or ""
    hit["path"] = p
    hit["size"] = size
    hit["dur"] = dur
    hit["sig"] = _stat_sig(p)
    # 重新定位成功 ⇒旧状态作废（missing/changed 都不该再挂着）
    hit.pop("missing", None)
    hit.pop("changed", None)
    hit.pop("why", None)
    hit["relinked_at"] = studio._now()
    # 扩展名与真实格式不一致时如实标注（不拒绝：内容确实是好的音频，
    # 但下游 ffmpeg 可能按扩展名判断，得让用户知道有个不一致）
    real_ext = os.path.splitext(p)[1].lower()
    if ext2 and real_ext and ext2 != real_ext:
        hit["ext_mismatch"] = "文件实际是 %s，但扩展名是 %s" % (ext2.upper().lstrip("."), real_ext)
    studio.save_library(db)
    return {"ok": True, "why": "已指向新位置，时长 %.1f 秒" % dur,
            "title": hit.get("title"), "old": old, "new": p}


def forget(item):
    """从曲库移除一条记录。**只删记录，绝不动用户的文件。**"""
    import sys as _s
    if ROOT not in _s.path:
        _s.path.insert(0, ROOT)
    import studio

    db = studio.load_library()
    songs = db.get("songs", [])
    keep = [s for s in songs if (s.get("item") or "") != item]
    if len(keep) == len(songs):
        return {"ok": False, "err": "曲库里没有这一条"}
    db["songs"] = keep
    studio.save_library(db)
    return {"ok": True, "left": len(keep)}