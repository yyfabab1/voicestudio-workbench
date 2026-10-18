# -*- coding: utf-8 -*-
"""「角色唱新歌」：YuE2 凭空作曲演唱 → 我们的分离 → RVC 换成角色音色 → 混音。

和翻唱那条路的区别
─────────────────────────────────────────────────────────────────
翻唱：`现成的歌` → 分离 → 换声 → 混音。**必须先有一首歌**。
本文件：`风格描述 + 歌词` → YuE2 作曲编曲演唱 → 分离 → 换声 → 混音。
⇒ 角色能唱**世界上还不存在的新歌**，这是翻唱给不了的。

分工
─────────────────────────────────────────────────────────────────
  * **YuE2 负责「写」**：作曲、编曲、演唱（48 kHz）
  * **我们负责「像」**：它克隆不了具体音色（只能 LoRA 微调），而我们有角色
    模型 + 声纹中心 ⇒ 换声必须由 RVC 做。
  * 分轨**不用它**、用我们自己的 `separate_song` —— 这样新源头接进的是
    **同一条已验证的管线**，下游（换声/混音/质检/成品登记）一行都不用改。

★ 为什么直接打 `yue-server.exe` 的 HTTP，而不是官方说的 MCP
─────────────────────────────────────────────────────────────────
README 说 MCP 在 `127.0.0.1:8791/mcp`，那是 **Tauri 窗口进程**提供的。
本机实测：应用进程起来了、窗口标题也在，但**没有任何监听端口**——它的
窗口在另一个 window station 里没真正创建，Tauri 的 setup 没跑完，
MCP 自然也没挂上。而且 8791 在本机被另一个常驻服务占着。

好消息是引擎本体是独立的：`resources/yue2-cpp/yue-server.exe`，
自己就能起 HTTP（`/props` `/synth` `/job`），**完全不依赖 GUI**。
⇒ 我们就直连引擎。这反而是更彻底的一体化：不需要人开窗口。

引擎的请求格式不是猜的，是 `GET /props` 自己报的（含全部 defaults）。
"""

import gzip
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

# 引擎地址。默认 8807 —— **不要用 8791**：本机 8791 被 workbuddy2api 常驻占用。
DEFAULT_ENGINE = os.environ.get("YUE2_ENGINE") or "http://127.0.0.1:8807"

# 安装目录（便携版）。可用环境变量指到别处。
YUE2_ROOT = os.environ.get("YUE2_ROOT") or r"D:\AHC\YuE2-Studio"

# 引擎与模型（相对 YUE2_ROOT）
ENGINE_EXE = os.path.join("resources", "yue2-cpp", "yue-server.exe")
MODEL_DIR = os.path.join("data", "models", "yue2-cpp")

# 模型集：8 GB 卡只能跑 Q5_K_M（Q8_0 会顶到 4.4 GB 权重 + KV，容易爆）。
MODEL_FILES = {
    "backbone": ["YuE2-3B-Q5_K_M.gguf", "YuE2-3B-Q6_K.gguf", "YuE2-3B-Q8_0.gguf"],
    "vae": ["YuE2-Vae-F32.gguf"],
    "transcriber": ["SheetSage2-Q8_0.gguf", "SheetSage2-F32.gguf"],
}

# 起引擎最多等多久（Vulkan 自检 + 载权重要十几秒）
BOOT_WAIT = 180

# 一首歌最多等多久。实测 45 秒的歌用 54 秒（0.8x 实时），5 分钟的歌约 6 分钟，
# 但采样运气差时会慢很多，给足。
GEN_BUDGET = 60 * 60


class NotReady(Exception):
    """引擎没起，或者模型不全。"""
    pass


class Yue2Fail(Exception):
    """引擎那边出错。"""
    pass


# ────────────────────────────────────────────────────────────────────
# HTTP
# ────────────────────────────────────────────────────────────────────

def _call(path, data=None, timeout=120, method=None, base=None):
    """返回 (headers, raw_bytes)。服务端要求 gzip，不给会 406。"""
    base = base or DEFAULT_ENGINE
    headers = {"Accept-Encoding": "gzip", "User-Agent": "VoiceStudio/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        base.rstrip("/") + path, data=data, headers=headers,
        method=method or ("POST" if data is not None else "GET"))
    r = urllib.request.urlopen(req, timeout=timeout)
    raw = r.read()
    if r.headers.get("Content-Encoding") == "gzip":
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
    return r.headers, raw


def _json(path, data=None, timeout=60, base=None):
    _, raw = _call(path, data=data, timeout=timeout, base=base)
    return json.loads(raw.decode("utf-8"))


def is_ready(base=None):
    """引擎活着吗？只读，不产生副作用。"""
    try:
        d = _json("/health", timeout=5, base=base)
        return str(d.get("status")) == "ok"
    except Exception:
        return False


def props(base=None):
    return _json("/props", timeout=20, base=base)


# ────────────────────────────────────────────────────────────────────
# 引擎的启动与自愈
# ────────────────────────────────────────────────────────────────────

def find_model(kind):
    """按优先级找存在的模型文件。找不到返回 None。"""
    d = os.path.join(YUE2_ROOT, MODEL_DIR)
    for name in MODEL_FILES.get(kind, []):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def missing_models():
    """缺哪些。返回中文说明的列表（空 = 齐了）。"""
    miss = []
    for kind, label in (("backbone", "主模型"), ("vae", "VAE"), ("transcriber", "乐谱转录")):
        if not find_model(kind):
            miss.append("%s（%s 下应有的 %s）" % (label, MODEL_DIR, MODEL_FILES[kind][0]))
    return miss


def engine_cmd(port=None):
    """拼引擎启动命令。port 不给就从 DEFAULT_ENGINE 里取。

    ★ **不加 `--keep-loaded`**：引擎默认"阶段之间把模型卸掉"。
      实测空闲时整卡只占 882 MiB（8151 总量里的），跑歌时才涨到 ~2.5 GB，
      跑完又落回去。这一条很重要 —— 我们这个 VoiceStudio 里 RVC、分离、
      改词都要用同一张 8 GB 卡，引擎要是常驻 2.6 GB 权重，后面每一步都可能 OOM，
      而错误只会出现在 RVC 那边（"CUDA out of memory"），看起来跟写歌毫无关系。
    """
    exe = os.path.join(YUE2_ROOT, ENGINE_EXE)
    if not os.path.isfile(exe):
        raise NotReady("找不到引擎 %s（YuE2 装在哪？用环境变量 YUE2_ROOT 指一下）" % exe)
    miss = missing_models()
    if miss:
        raise NotReady("模型不全：%s" % "；".join(miss))
    if port is None:
        try:
            port = int(DEFAULT_ENGINE.rsplit(":", 1)[1].strip("/"))
        except Exception:
            port = 8807
    return [
        exe,
        "--model", find_model("backbone"),
        "--vae", find_model("vae"),
        "--transcriber", find_model("transcriber"),
        "--host", "127.0.0.1",
        "--port", str(port),
    ]


def ensure_engine(base=None, say=None, boot_wait=BOOT_WAIT):
    """引擎没起就自己拉起来。返回 True 表示可用。

    ★ 这是「一体化」的关键一步：用户不需要知道有 yue-server 这个东西。
    """
    base = base or DEFAULT_ENGINE
    if is_ready(base):
        return True
    cmd = engine_cmd()
    log_path = os.path.join(YUE2_ROOT, "_engine.log")
    if say:
        say("  引擎没在跑，自动拉起：%s" % os.path.basename(cmd[0]))
    # 独立进程组：VoiceStudio 退出时不要连带把引擎杀掉（下次还能复用）。
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008  # DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        f = open(log_path, "ab")
        subprocess.Popen(cmd, cwd=YUE2_ROOT, stdout=f, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, **kwargs)
    except Exception as e:
        raise NotReady("拉不起引擎：%s: %s" % (type(e).__name__, e))

    t0 = time.time()
    while time.time() - t0 < boot_wait:
        if is_ready(base):
            if say:
                say("  引擎已就绪（%.0f 秒）" % (time.time() - t0))
            return True
        time.sleep(2)
    tail = ""
    try:
        with open(log_path, "rb") as f:
            tail = f.read()[-600:].decode("utf-8", "replace")
    except Exception:
        pass
    raise NotReady("引擎 %.0f 秒还没起来。日志尾部：\n%s" % (boot_wait, tail))


# ────────────────────────────────────────────────────────────────────
# 生成
# ────────────────────────────────────────────────────────────────────

def _split_parts(raw, boundary):
    """把 multipart/mixed 拆成 [(content_type, body), ...]。

    ★ 返回体不是纯音频：第一个 part 是 JSON（style/lyrics/生成的 ABC 乐谱），
      音频在后面的 part 里。直接把整个响应存成 .mp3 会得到一个打不开的文件
      —— 因为前面带着一段 JSON 和 boundary。
    """
    out = []
    for p in raw.split(b"--" + boundary):
        p = p.strip(b"\r\n")
        if not p or p.startswith(b"--"):
            continue
        j = p.find(b"\r\n\r\n")
        if j < 0:
            continue
        hdr = p[:j].decode("utf-8", "replace")
        ct = ""
        for line in hdr.splitlines():
            if line.lower().startswith("content-type"):
                ct = line.split(":", 1)[1].strip()
        out.append((ct, p[j + 4:]))
    return out


def _boundary_of(headers):
    ct = headers.get("Content-Type") or ""
    for bit in ct.split(";"):
        bit = bit.strip()
        if bit.startswith("boundary="):
            return bit[9:].strip('"').encode("ascii")
    return b"yue2-batch-boundary"


# 引擎支持的输出格式（实测，不是猜的）：
#   mp3   —— OK（默认，128 kbps 有损）
#   wav32 —— OK（32 位浮点 WAV，无损，48 kHz）
#   flac / wav / FLAC / pcm —— 全部 400 `unknown output format`
# ⇒ 统一用 wav32。mp3 的有损压缩会先吃掉一次 16 kHz 以上，正是我们花了
#   一轮功夫（slice_hifi + rmvpe）保下来的那部分，不能在这里又丢一次。
FMT_LOSSLESS = "wav32"
FMT_MP3 = "mp3"
KNOWN_FMTS = ("wav32", "mp3")


def synth(job, style, lyrics, duration=180.0, steps=32, fmt=FMT_LOSSLESS,
          out_path=None, say=None, base=None, budget=GEN_BUDGET):
    """写一首歌，返回 (音频路径, 元信息 dict)。

    `job` 只用来打日志（studio.log 那种 dict），可以传 None。
    音频格式默认 wav32（无损 48k）—— 后面要过 RVC，mp3 的有损压缩会先吃掉
    一次高频，正是我们花力气保下来的那部分。
    """
    if fmt not in KNOWN_FMTS:
        raise Yue2Fail("引擎不支持输出格式 %r（只认 %s）" % (fmt, "/".join(KNOWN_FMTS)))
    base = base or DEFAULT_ENGINE

    def _say(m):
        """日志出口。

        ★ 这里**不能**写 `from ..studio import log`：`stages` 不是包
          （目录里没有 `__init__.py`），`..` 会直接 ImportError。
          当前调用方都传了 `say=`，所以这条 fallback 走不到 —— 正因为走不到，
          这种错才最阴：改天有人不传 say，就会在一次生成到一半时炸，而堆栈
          指向的是一行"看起来只是打日志"的代码。
        """
        if say:
            say(m)
        elif job is not None:
            try:
                import studio as _st  # studio.py 在 sys.path 上（HERE 已插入）
                _st.log(job, m)
            except Exception:
                pass

    ensure_engine(base=base, say=_say)

    body = {"style": style, "lyrics": lyrics,
            "duration": float(duration), "steps": int(steps),
            "output_format": fmt}
    try:
        h, raw = _call("/synth", json.dumps(body).encode("utf-8"),
                       timeout=120, base=base)
        jid = json.loads(raw.decode("utf-8")).get("id")
    except Exception as e:
        raise Yue2Fail("提交生成失败：%s: %s" % (type(e).__name__, e))
    if not jid:
        raise Yue2Fail("引擎没给任务 id：%s" % raw[:200])
    _say("  作曲中（任务 %s，目标 %.0f 秒）" % (jid[:12], duration))

    t0 = time.time()
    status = None
    while time.time() - t0 < budget:
        try:
            st = _json("/job?id=" + jid, timeout=30, base=base)
        except Exception:
            time.sleep(3)
            continue
        s = str(st.get("status") or "")
        if s != status:
            _say("    [%4.0fs] %s" % (time.time() - t0, s))
            status = s
        if s in ("done", "error", "failed", "cancelled"):
            break
        time.sleep(3)
    if status != "done":
        raise Yue2Fail("生成没成功（%.0f 秒后状态 %s）" % (time.time() - t0, status))

    try:
        h, raw = _call("/job?id=%s&result=1" % jid, timeout=300, base=base)
    except Exception as e:
        raise Yue2Fail("取结果失败：%s: %s" % (type(e).__name__, e))

    parts = _split_parts(raw, _boundary_of(h))
    meta = {}
    audio = None
    for ct, body_bytes in parts:
        if "json" in ct:
            try:
                meta = json.loads(body_bytes.decode("utf-8", "replace"))
            except Exception:
                pass
        elif ct.startswith("audio/") or body_bytes[:4] in (b"fLaC", b"RIFF", b"ID3\x03"):
            audio = body_bytes
    if audio is None:
        raise Yue2Fail("结果里没有音频（parts=%s）" % [c for c, _ in parts])

    ext = {"wav32": ".wav", "mp3": ".mp3"}.get(fmt, "." + fmt)
    if out_path is None:
        out_dir = os.path.join(YUE2_ROOT, "out")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, "yue2_%s%s" % (jid[:12], ext))
    else:
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as f:
        f.write(audio)
    meta["_job_id"] = jid
    meta["_elapsed"] = round(time.time() - t0, 1)
    meta["_file"] = out_path
    _say("  做好了：%s（%.1f MB，%.0f 秒）"
         % (os.path.basename(out_path), os.path.getsize(out_path) / 2 ** 20, meta["_elapsed"]))
    return out_path, meta


def status_text(base=None):
    """给界面用的一行状态。"""
    if not is_ready(base=base):
        miss = missing_models()
        if miss:
            return "YuE2 未就绪：模型不全（%s）" % "；".join(miss)
        return "YuE2 引擎未运行（会自动拉起）"
    try:
        p = props(base=base)
        return "YuE2 %s · 模型 %s · 48 kHz" % (
            p.get("version"), os.path.basename(p.get("model") or ""))
    except Exception:
        return "YuE2 引擎在跑"
