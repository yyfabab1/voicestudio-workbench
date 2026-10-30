# -*- coding: utf-8 -*-
"""把一段音频交给本机的 Adobe After Effects 去渲染。

为什么这么绕
------------
AE 的脚本环境（ExtendScript）是**纯 ES3**：没有 JSON、没有 forEach/map/trim，
也没有 `Project.saveAs`（26.0 实测已移除）。而 AE 又是**单实例**：`-r` 投递的
脚本一定跑在老板正在用的那个 AE 里。

所以这里的规矩是：

  1. 数据永远不靠 JSON 解析 —— 参数直接在 Python 侧拼成 JS 字面量，
     所有中文用 `\\uXXXX` 转义 ⇒ 生成的 jsx 是**纯 ASCII**，彻底告别编码坑；
  2. 进去之前先给项目里的 item 拍一张 id 快照，出来之后只删**不在快照里**的
     东西 ⇒ 老板自己的素材一个都不少；
  3. 输出模块模板名是**本地化**的（`H.264 - 匹配渲染设置 - 15 Mbps`），
     写英文 "H.264" 必报错 ⇒ 候选名从 payload 里带过去逐个试。

（这三条全是这轮在真 AE 上撞出来的，改之前先重读一遍。）
"""
from __future__ import print_function

import json
import os
import subprocess
import time
import uuid

JSX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jsx")

# 频谱要"看得见的振幅"。真实歌曲里大多数频段只有 10% 电平，直接喂原音，
# 柱子永远是贴着底线的一排小点（实测过：一排均匀的珠子，一点起伏都没有）。
# 所以另做一条**只有频谱在吃**的影子音轨：响度拉平 + 压缩，
# 声音仍然是原来那条（这条是静音的），画面才会跟着鼓点跳。
SPEC_FF_FILTER = ("loudnorm=I=-14:TP=-1.2:LRA=9,"
                  "acompressor=threshold=-20dB:ratio=6:attack=3:release=90")

# 中文字体必须给 PostScript 名，写 "Microsoft YaHei" 会被 AE 当成无效字符
CN_FONT = "MicrosoftYaHei"

# 输出模块模板：中文版 AE 的真名就是带中文的这一串，写英文 "H.264" 必然报错。
# 顺序即优先级，谁先 applyTemplate 成功用谁。
OM_CANDIDATES = [
    "H.264 - 匹配渲染设置 - 15 Mbps",
    "H.264 - 匹配渲染设置 - 10 Mbps",
    "H.264",
    "无损",
]


def _log(log, msg):
    if log:
        try:
            log(msg)
        except Exception:                                    # noqa: BLE001
            pass


# ------------------------------------------------------------------ STYLES
#
# key 统一用 `ae_` 前缀：界面那一侧不许写 `VIZ_STYLE === "cast"` 这种判断，
# 同理这里也不许在别处写 `if engine == "py"` 之类 —— 一律看标记。
STYLES = [
    {
        "key": "ae_kinetic", "name": "AE 卡点歌词", "tag": "歌词卡点",
        "tpl": "tpl_kinetic.jsx", "group": "ae", "engine": "ae",
        "ae_lyrics": True, "needs_cast": False,
        "desc": "一句进来就带一个轻微上顶的动作，当前句最大最白，"
                "原文在上、中文在下，句与句之间不留空档。"
                "照音乐 App 歌词页排的 —— 想要「像正经歌词视频」就选它。",
    },
    {
        "key": "ae_roll", "name": "AE 滚动歌词墙", "tag": "整首都在眼前",
        "tpl": "tpl_roll.jsx", "group": "ae", "engine": "ae",
        "ae_lyrics": True, "needs_cast": False,
        "desc": "整首歌词排成一列往上滚，正在唱的那句停在画面正中变亮，"
                "前后的句子半透明留着（上下用羽化遮罩淡出）。"
                "想让人跟着唱、看得见下一句的时候用它。",
    },
    {
        "key": "ae_cast", "name": "AE 合唱台（平时暗着，谁唱谁亮）", "tag": "多角色专用",
        "tpl": "tpl_cast.jsx", "group": "ae", "engine": "ae",
        "ae_lyrics": True, "needs_cast": True, "cast_lay": "mv",
        "desc": "底部一排角色头像，**平时全都是压暗的**（不是把透明度调低透出背景，"
                "是把像素压暗 —— 脸还在，只是没打光）。轮到谁，谁整块亮起来、"
                "放大、往上抬一点，套上自己的彩色描边和光晕；唱完又暗回去。"
                "背景整块淡成那个人的颜色，歌词也跟着换色。"
                "多人分唱这首歌时用这个最清楚 —— 需要成品带分配表才会亮"
                "（没有分配表时按独唱处理，全程亮着，不会一片黑）。",
    },
    {
        "key": "ae_vertical", "hScale": 20.0, "name": "AE 竖屏短片", "tag": "手机直出",
        "tpl": "tpl_vertical.jsx", "group": "ae", "engine": "ae",
        "ae_lyrics": True, "needs_cast": False,
        "desc": "1080x1920 竖屏版：上半屏封面（缓慢放大），下半屏频谱 + 歌词。"
                "发手机端短视频用这个。",
    },
    {
        "key": "ae_bars", "hScale": 21.0, "name": "AE 音频·条形频谱", "tag": "最直观",
        "tpl": "tpl_bars.jsx", "group": "ae", "engine": "ae",
        "ae_lyrics": True, "needs_cast": False,
        "desc": "音频模板一号：画面主体就是**这条音频真实的频谱**"
                "（AE 自带 Audio Spectrum 效果，跟着鼓点跳），"
                "下面再垫一层细波形做质感。封面浮在上面缓慢呼吸。",
    },
    {
        "key": "ae_ring", "hScale": 33.0, "name": "AE 音频·黑胶唱片机", "tag": "转盘 + 频谱",
        "tpl": "tpl_ring.jsx", "group": "ae", "engine": "ae",
        "ae_lyrics": True, "needs_cast": False,
        "desc": "音频模板二号：一整套唱机 —— 唱盘 8 秒转一圈（封面当唱片标签，"
                "转起来看得见），带纹路、中心孔、不跟着转的高光，"
                "唱臂从外圈慢慢往里走；底下立一条**真实频谱**当基座，上面一条细波形。"
                "（原本想做「频谱绕成一圈」的极坐标版，AE 在脚本建的路径上"
                "只画得出路径起点一个小箭头，实测三次都这样，所以不硬凑，改成唱机。）",
    },
]


def style_by_key(key):
    for s in STYLES:
        if s["key"] == key:
            return s
    return None


def public_styles():
    """给 /api/viz/options 的那份（带上前端需要的字段）。"""
    out = []
    for s in STYLES:
        out.append({
            "key": s["key"], "name": s["name"], "tag": s.get("tag", ""),
            "desc": s["desc"], "engine": "ae", "group": "ae",
            "needs_cast": bool(s.get("needs_cast")),
            "cast_lay": s.get("cast_lay") or "",
            "ae_lyrics": True,
            "note": "AE 真渲染：画面由 After Effects 排的，比逐帧手绘快很多，"
                    "但要求 After Effects 已经开着。",
        })
    return out


# ------------------------------------------------------------------ AE 在哪
def find_ae():
    """找 AfterFX.exe。装过就有注册表记录，先看注册表再看固定位置。"""
    try:
        import winreg                                        # py3 Windows
    except Exception:                                        # noqa: BLE001
        winreg = None
    path = None
    try:
        import winreg as _wr                                 # noqa: F811
        k = _wr.OpenKey(_wr.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall")
        i = 0
        while True:
            try:
                sub = _wr.EnumKey(k, i)
            except OSError:
                break
            i += 1
            try:
                sk = _wr.OpenKey(k, sub)
                nm = _wr.QueryValueEx(sk, "DisplayName")[0]
            except OSError:
                continue
            if nm and "After Effects" in nm:
                try:
                    loc = _wr.QueryValueEx(sk, "InstallLocation")[0]
                except OSError:
                    loc = ""
                if loc:
                    cand = os.path.join(loc, "Adobe After Effects 2026",
                                        "Support Files", "AfterFX.exe")
                    if os.path.isfile(cand):
                        path = cand
                        break
                    for root, _d, files in os.walk(loc):
                        if "AfterFX.exe" in files:
                            path = os.path.join(root, "AfterFX.exe")
                            break
                    if path:
                        break
    except Exception:                                        # noqa: BLE001
        pass
    if path and os.path.isfile(path):
        return path
    for cand in (r"D:\ae视频制作\Adobe After Effects 2026\Support Files\AfterFX.exe",
                 r"C:\Program Files\Adobe\Adobe After Effects 2026\Support Files\AfterFX.exe",
                 r"C:\Program Files\Adobe\Adobe After Effects 2025\Support Files\AfterFX.exe"):
        if os.path.isfile(cand):
            return cand
    return None


def ae_running():
    """AE 是否在跑。单实例应用 —— 跑着就只能往它身上发脚本。"""
    try:
        p = subprocess.run(["tasklist", "/FI", "IMAGENAME eq AfterFX.exe", "/NH"],
                           capture_output=True, timeout=30)
        out = (p.stdout or b"").decode("utf-8", "replace")
        return "AfterFX.exe" in out
    except Exception:                                        # noqa: BLE001
        return False


def ensure_ae(exe, log=None, wait=180):
    """没在跑就拉起来，然后等它能收脚本为止。返回 (可用?, 说明)。

    判断标准是**真的收到回执**，不是"进程存在" —— AE 起来之后还有一段
    初始化，这时候发脚本会石沉大海（实测过：投递返回 0，但什么都没发生）。
    """
    if ae_running():
        return True, "已经在跑"
    if not exe or not os.path.isfile(exe):
        return False, "这台机器上没找到 After Effects（AfterFX.exe）。"
    _log(log, "  After Effects 没开着，正在启动…（首次启动要等一会儿）")
    try:
        DETACHED = 0x00000008
        NEWPROC = 0x00000200
        subprocess.Popen([exe], creationflags=DETACHED | NEWPROC,
                         close_fds=True)
    except Exception as e:                                   # noqa: BLE001
        return False, "启动 After Effects 失败：%s" % e
    # 轮询：丢一个探针脚本过去，看它会不会回写一个文件
    stamp = "vs_ae_ready_%d" % int(time.time())
    probe_jsx = os.path.join(_tmp_dir(), stamp + ".jsx")
    probe_out = os.path.join(_tmp_dir(), stamp + ".txt")
    for _f in (probe_jsx, probe_out):
        if os.path.exists(_f):
            try:
                os.remove(_f)
            except OSError:
                pass
    with open(probe_jsx, "w") as fh:
        fh.write('var f=new File("%s");f.encoding="UTF-8";f.open("w");'
                 'f.write("ready "+app.version);f.close();\n'
                 % probe_out.replace("\\", "/"))
    t0 = time.time()
    while time.time() - t0 < wait:
        time.sleep(3)
        try:
            subprocess.run([exe, "-r", probe_jsx], timeout=15,
                           capture_output=True)
        except Exception:                                    # noqa: BLE001
            pass
        time.sleep(2)
        if os.path.exists(probe_out):
            try:
                txt = open(probe_out, encoding="utf-8").read().strip()
            except Exception:                                # noqa: BLE001
                txt = "ok"
            return True, "After Effects 起来了（%s），用时 %.0f 秒" % (
                txt, time.time() - t0)
    return False, ("等了 %.0f 秒 After Effects 还是没收脚本 —— "
                   "多半是它弹了个窗口（许可以及「恢复这一年？」之类）卡在那儿，"
                   "去屏幕上看一眼、点掉，再来一次。" % wait)


# ------------------------------------------------------------------ 拼脚本
_DRIVER = r"""
/* ------------------------------------------------------------------ driver
 *
 * The ordering here is deliberate:
 *   snapshot -> build -> render -> purge -> write receipt LAST.
 * Python only returns once the receipt file shows up, so "receipt exists" must
 * mean "AE has already been cleaned up". If we wrote the receipt before the
 * purge, the next render would snapshot our own leftovers as if they were the
 * user's items, and they could never be deleted again.
 *
 * (kept ASCII on purpose: every Chinese string travels inside CTX as \\uXXXX,
 *  so the generated file survives any code page AE happens to read it in.)
 */
var RES = { status: "FAIL", msg: "", note: "" };
var _itemsBefore = app.project.numItems;
var _snap = rtSnapshot();
var _t0 = Date.now();

function _write() {
    var f = new File(CTX.done);
    f.encoding = "UTF-8";
    f.open("w");
    f.write("status=" + RES.status + "\n" +
            "msg=" + RES.msg + "\n" +
            "ms=" + (Date.now() - _t0) + "\n" +
            "itemsBefore=" + _itemsBefore + "\n" +
            "itemsAfter=" + app.project.numItems + "\n" +
            "purged=" + (RES.purged === undefined ? -1 : RES.purged) + "\n" +
            "stuck=" + (RES.stuck === undefined ? "" : RES.stuck) + "\n" +
            "layers=" + (RES.layers === undefined ? -1 : RES.layers) + "\n" +
            "om=" + (RES.om === undefined ? "" : RES.om) + "\n" +
            "missingAudio=" + (RT.missingAudio ? 1 : 0) + "\n" +
            "specErr=" + (RT.specErr || "") + "\n" +
            "bound=" + (RES.bound === undefined ? -1 : RES.bound) + "\n" +
            "size=" + (RES.size === undefined ? -1 : RES.size) + "\n");
    f.close();
}

try {
    if (typeof app.beginSuppressDialogs === "function") app.beginSuppressDialogs();
} catch (e0) { }

try {
    if (!CTX.audioOK) {
        RES.status = "FAIL";
        RES.msg = "audio file missing: " + CTX.audio;
    } else {
        var comp = TPL_build(CTX);
        /* Layer indexes shift as layers are added, so the audio binding of every
         * spectrum effect is refreshed here, once, with the final stack. */
        try { var _bound = rtBindAudio(comp, RT.audioLayer); RES.bound = _bound; } catch (eB) { }
        if (!comp) {
            RES.status = "FAIL";
            RES.msg = "TPL_build returned nothing";
        } else {
            RES.layers = comp.numLayers;
            var rq = app.project.renderQueue.items.add(comp);
            var om = rq.outputModule(1);
            for (var i = 0; i < CTX.om.length; i++) {
                try { om.applyTemplate(CTX.om[i]); RES.om = CTX.om[i]; break; }
                catch (e1) { }
            }
            var ofile = new File(CTX.out);
            if (ofile.exists) ofile.remove();
            try { om.file = ofile; }
            catch (e2) { RES.note = "cannot set output file"; }
            app.project.renderQueue.render();
            RES.size = ofile.exists ? ofile.length : 0;
            if (RES.size > 4096) {
                RES.status = "OK";
                RES.msg = "size=" + RES.size;
            } else {
                RES.status = "FAIL";
                RES.msg = "render produced no usable file (size=" + RES.size + ")";
            }
        }
    }
} catch (eRoot) {
    RES.status = "FAIL";
    RES.msg = eRoot.toString() + " @line " + (eRoot.line || "?");
} finally {
    try { if (typeof app.endSuppressDialogs === "function") app.endSuppressDialogs(false); }
    catch (e3) { }
    try {
        var cl = rtPurge(_snap);
        RES.purged = cl.gone;
        RES.stuck = cl.stuck.join(",");
    } catch (e4) { RES.stuck = "purge exception"; }
    _write();
}
"""


def _as_rgb(color, default=(0.45, 0.6, 1.0)):
    """颜色统一转成 AE 要的 0..1 三分量。"""
    if not color:
        return list(default)
    try:
        if isinstance(color, str):
            h = color.lstrip("#")
            if len(h) == 6:
                return [int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
            return list(default)
        if len(color) >= 3:
            vals = [float(x) for x in color[:3]]
            if max(vals) > 1.0:                              # 8bit 也要能收
                vals = [v / 255.0 for v in vals]
            return vals
    except Exception:                                        # noqa: BLE001
        pass
    return list(default)


_DIM_TARGET = 95.0      # 暗态希望落在的亮度（0..255）
_LIT_TARGET = 160.0     # 亮态希望被抬到的亮度
_DIM_MIN, _DIM_MAX = -145, -15      # 压暗量：最多压 145，最少也压 15
_LIT_MIN, _LIT_MAX = 0, 45          # 提亮量：暗图多抬一点，亮图不抬（会过曝）


def _dim_for_avatar(path):
    """这张图该压多暗、亮起来该抬多高 —— 按它自己有多亮来定，不是一刀切。

    为什么不能统一 -85：AE 的"亮度"是**加性**的（像素直接加这个值），
    所以同一刀切下去，原本就亮的头像（均值 237）压完还有 152 —— 看着像没暗；
    而原本就暗的头像（均值 118）压完只剩 73 —— 跟背景（55）快糊在一起，
    看不出是谁。门禁里那条"暗态仍能看见"就是被它抓出来的。

    反过来看亮态：暗图就算一点不压，它本身也只有 118，跟亮图没法比。
    所以亮态也按同一条尺子抬 —— 暗图抬到 160，亮图本来就够亮就不抬
    （再抬就过曝了）。两头一起调，"谁唱谁亮"才对谁都成立。

    返回 [压暗量, 提亮量]。量不到（没装 PIL、图打不开）就返回 None，
    模板退回固定值 —— 宁可用个粗糙的数，也不许因此渲染失败。
    """
    try:
        from PIL import Image
    except Exception:                                        # noqa: BLE001
        return None
    try:
        im = Image.open(path)
        im = im.convert("RGB")
        im.thumbnail((48, 48))
        px = list(im.getdata())
        if not px:
            return None
        lum = sum(0.299 * r + 0.587 * g + 0.114 * b for r, g, b in px) / len(px)
    except Exception:                                        # noqa: BLE001
        return None

    def _clamp(v, lo, hi):
        return lo if v < lo else (hi if v > hi else v)

    dim = int(round(_DIM_TARGET - lum))
    lit = int(round(_LIT_TARGET - lum))
    return [_clamp(dim, _DIM_MIN, _DIM_MAX), _clamp(lit, _LIT_MIN, _LIT_MAX)]


def _win(p):
    """AE 的 File 吃正斜杠路径；Windows 反斜杠会被当转义吃掉。"""
    return (p or "").replace("\\", "/")


def _ffmpeg_exe():
    """找一个能用的 ffmpeg。项目里 viz 已经探过一次，这里只是兜底。"""
    env = os.environ.get("VS_FFMPEG")
    if env and os.path.isfile(env):
        return env
    for cand in (r"D://JianyingPro//Apps//8.2.0.13017//ffmpeg.exe",
                 r"D://JianyingPro//Apps//8.2.0.13017//ffmpeg.exe"):
        if os.path.isfile(cand):
            return cand
    try:
        import shutil as _sh
        return _sh.which("ffmpeg")
    except Exception:                                        # noqa: BLE001
        return None


def make_spec_feed(src, dst):
    """做一条给频谱看的影子音轨。失败就返回 None（退化成直接吃原音）。"""
    ff = _ffmpeg_exe()
    if not ff:
        return None
    cmd = [ff, "-y", "-v", "error", "-i", src,
           "-af", SPEC_FF_FILTER, "-ac", "1", "-ar", "48000", "-c:a", "pcm_s16le", dst]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=300)
    except Exception:                                        # noqa: BLE001
        return None
    if p.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) < 1024:
        return None
    return dst


def remux_original_audio(video, audio_src):
    """把 AE 渲出来的视频配上**原始**音轨（AE 里用的是压缩过的影子音轨）。

    为什么不直接让 AE 用原音轨：频谱要看得见的振幅，原音轨大多数频段只有
    10% 电平，柱子会趴成一排小点。所以 AE 里喂了压缩版，出来再换回原音。
    """
    ff = _ffmpeg_exe()
    if not ff or not os.path.isfile(video) or not os.path.isfile(audio_src):
        return False
    tmp = video + ".mux.mp4"
    cmd = [ff, "-y", "-v", "error", "-i", video, "-i", audio_src,
           "-map", "0:v:0", "-map", "1:a:0",
           "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", tmp]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=600)
    except Exception:                                        # noqa: BLE001
        return False
    if p.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) < 4096:
        return False
    try:
        os.replace(tmp, video)
    except OSError:
        return False
    return True


def _tmp_dir():
    d = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "_tmp")
    try:
        os.makedirs(d)
    except OSError:
        pass
    return d


def build_driver(ctx, style):
    """拼一份可以直接丢给 AE 的完整脚本。

    ★ 生成结果必须是**纯 ASCII**。所有中文要么以 \\uXXXX 的形式出现
      （json.dumps 干的事），要么是英文注释 —— 只要有一个非 ASCII 混进来，
      AE 就可能按系统代码页去读它，症状是乱码甚至整个脚本 parse 失败。
      与其赌它这次读对了，不如在这里直接炸掉。
    """
    parts = []
    for name in ("runtime.jsx", style["tpl"]):
        with open(os.path.join(JSX_DIR, name), encoding="utf-8") as fh:
            parts.append(fh.read())
    payload = "var CTX = %s;\n" % json.dumps(ctx, ensure_ascii=True)
    parts.append(payload)
    parts.append(_DRIVER)
    code = "\n".join(parts)
    _assert_ascii(code, style["key"])
    return code


def _assert_ascii(code, where):
    bad = [(i, c) for i, c in enumerate(code) if ord(c) > 127]
    if not bad:
        return
    i, c = bad[0]
    line = code[:i].count("\n") + 1
    snippet = code[max(0, i - 40):i + 20].replace("\n", "\\n")
    raise ValueError(
        "给 AE 的脚本里混进了非 ASCII 字符（第 %d 行，字符 %r）：…%s…\n"
        "所有中文都必须以 \\uXXXX 的形式走 CTX，注释请用英文。"
        % (line, c, snippet))


def _read_result(done_path):
    info = {}
    try:
        with open(done_path, encoding="utf-8") as fh:
            for line in fh.read().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
    except Exception:                                        # noqa: BLE001
        pass
    return info


def render(audio, dst, style, size, fps, lines=None, cast=None, title="",
           subtitle="", cover="", start=None, duration=None, duration_total=None,
           log=None, timeout=3600, keep_debug=False, h_scale=None):
    """渲一条。返回 (成功?, 说明)。

    `lines` [(开始秒, 文本)] 或 [(开始秒, 结束秒, 文本, 中文)] —— 两种都能吃。
    `cast`  [{'start','end','name','color','avatar'}] —— 给合唱台用的。
    """
    t0 = time.time()
    if h_scale is None:
        # 频谱振幅在 AE 里只映射到 maxH 的一小部分；每套样式自带的倍率就是
        # 实测调出来的"看着像音乐"的那一档，调用方不用管。
        h_scale = style.get("hScale", 1.0) if isinstance(style, dict) else 1.0
    audio = os.path.abspath(audio or "")
    dst = os.path.abspath(dst or "")
    if not os.path.isfile(audio):
        return False, "找不到音频：%s" % (audio or "没给")
    try:
        os.makedirs(os.path.dirname(dst))
    except OSError:
        pass

    exe = find_ae()
    if not exe:
        return False, ("这台机器上找不着 After Effects。"
                       "这条样式要用 AE 渲染，换一组样式（或先装 AE）。")
    ok, why = ensure_ae(exe, log=log)
    if not ok:
        return False, why

    # ---- 时间轴：能拿到总长就用总长，不行就拿最后一句 + 尾巴
    total = float(duration_total or 0.0)
    if total <= 0 and lines:
        try:
            mx = 0.0
            for ln in lines:
                v = float(ln[1]) if len(ln) > 3 else float(ln[0])
                if v > mx:
                    mx = v
            total = mx + 3.0
        except Exception:                                    # noqa: BLE001
            total = 0.0
    if total <= 0:
        return False, "拿不到这段音频的时长，做不了。"

    start = float(start or 0.0)
    if start < 0:
        start = 0.0
    want = float(duration) if duration else max(1.0, total - start)

    # ---- 歌词：裁到区间里并平移
    ls = []
    for ln in lines or []:
        try:
            if len(ln) >= 4:
                a, b, txt, zh = float(ln[0]), float(ln[1]), ln[2], ln[3]
            elif len(ln) == 2:
                a, b, txt, zh = float(ln[0]), None, ln[1], ""
            else:
                continue
        except Exception:                                    # noqa: BLE001
            continue
        if b is None:
            b = a + 4.0
        a -= start
        b -= start
        if b <= 0.2 or a >= want - 0.1:
            continue
        a = max(0.0, a)
        b = min(want, b)
        ls.append([round(a, 3), round(b, 3), txt or "", zh or ""])

    # ---- 合唱台的人 + 颜色落到每一行上
    cs = []
    color_of = {}
    for c in cast or []:
        try:
            if isinstance(c, dict):
                a = float(c.get("start") or 0.0)
                b = float(c.get("end") or 0.0)
                nm = c.get("name") or "?"
                col = _as_rgb(c.get("color"))
                av = c.get("avatar") or ""
            else:
                a, b, nm = float(c[0]), float(c[1]), c[2]
                col = _as_rgb(c[3] if len(c) > 3 else None)
                av = c[4] if len(c) > 4 else ""
        except Exception:                                    # noqa: BLE001
            continue
        a -= start
        b -= start
        if b <= 0.05 or a >= want - 0.05:
            continue
        avw = _win(av) if av and os.path.isfile(av) else ""
        # ★ 每个人压多暗，得看他这张图本身有多亮 —— 见 _dim_for_avatar 的注释。
        # 统一 -85 会让本来就暗的头像压完只剩一坨黑，看不出是谁。
        dv = _dim_for_avatar(av) if avw else None
        cs.append([round(max(0.0, a), 3), round(min(want, b), 3), nm, col, avw,
                   dv if dv is not None else ""])
        color_of.setdefault(nm, col)
    # 每行按中点找是谁在唱，好把字染成他的颜色
    out_lines = []
    for a, b, txt, zh in ls:
        mid = (a + b) / 2.0
        who = None
        for c in cs:
            if c[0] <= mid < c[1]:
                who = c[3]
                break
        out_lines.append([a, b, txt, zh, who or None])

    accent = None
    if cs:
        accent = cs[0][3]
    elif color_of:
        accent = list(color_of.values())[0]

    uid = "vs_ae_%s" % uuid.uuid4().hex[:10]
    tmpd = _tmp_dir()
    spec_feed = make_spec_feed(audio, os.path.join(tmpd, uid + "_spec.wav"))
    if spec_feed:
        _log(log, "  频谱会吃一条响度拉平过的影子音轨（声音还是原来那条）")
    jsx_path = os.path.join(tmpd, uid + ".jsx")
    done_path = os.path.join(tmpd, uid + ".done")
    for _f in (jsx_path, done_path):
        if os.path.exists(_f):
            try:
                os.remove(_f)
            except OSError:
                pass

    ctx = {
        "w": int(size["w"]), "h": int(size["h"]), "fps": int(fps),
        "dur": round(want, 3),
        "audio": _win(audio), "out": _win(dst), "done": _win(done_path),
        "specAudio": _win(spec_feed) if spec_feed else "",
        "audioOK": True,
        "title": title or "", "subtitle": subtitle or "", "font": CN_FONT,
        "lines": out_lines, "cast": cs,
        "cover": _win(cover) if cover and os.path.isfile(cover) else "",
        "accent": accent, "bg1": [0.03, 0.035, 0.06], "bg2": [0.18, 0.09, 0.30],
        "trim": round(start, 3), "hScale": float(h_scale or 1.0), "hScale": float(h_scale or 1.0),
        "om": OM_CANDIDATES,
    }

    with open(jsx_path, "w") as fh:
        fh.write(build_driver(ctx, style))

    _log(log, "  AE 正在排 %dx%d @%dfps（%s）…"
         % (ctx["w"], ctx["h"], ctx["fps"], style["name"]))
    try:
        subprocess.run([exe, "-r", jsx_path], timeout=60, capture_output=True)
    except Exception as e:                                   # noqa: BLE001
        _log(log, "  （投递脚本时出了点岔子：%s）" % e)

    # 回执出现 == AE 已经完事且清理过了
    waited = 0.0
    info = {}
    while waited < timeout:
        time.sleep(2.0)
        waited += 2.0
        if os.path.exists(done_path):
            time.sleep(0.4)
            info = _read_result(done_path)
            break
    else:
        return False, ("AE 渲了 %.0f 秒还没出回执（上限 %.0f 秒）。\n"
                       "多半是 After Effects 正弹着某个窗口等你点，"
                       "去屏幕上看一眼再来。" % (waited, timeout))

    # ★ 删不掉要**说出来**，不能 `except: pass` 静默吞掉。
    #   实测：影子音轨一次 1.5MB，被挡下 100 次就是 95MB 躺在 _tmp 里，
    #   而任务照样显示"完成" —— 这正是"静默失败"最烦人的那种。
    _left = []

    def _rm(p):
        if not p or not os.path.exists(p):
            return
        try:
            os.remove(p)
        except OSError as _e:
            _left.append(os.path.basename(p))
            _log(log, "  （临时文件没删掉：%s —— %s）"
                 % (os.path.basename(p), _e))

    if spec_feed:
        _rm(spec_feed)
    if not keep_debug:
        for _f in (jsx_path, done_path):
            _rm(_f)
    if _left:
        _log(log, "  ★ 有 %d 个临时文件没清掉，堆在 _tmp 里（不影响成品，但占盘）"
             % len(_left))

    # 影子音轨在 AE 里当音轨用，成品要换回原音
    if spec_feed and (info.get("status") or "").strip() == "OK":
        if remux_original_audio(dst, audio):
            _log(log, "  已把原始音轨换回去（AE 里那条压缩版只是喂频谱的）")
        else:
            _log(log, "  ★ 换回原始音轨失败，成品里是压缩过的版本")

    status = (info.get("status") or "").strip()
    msg = (info.get("msg") or "").strip()
    size_b = -1
    try:
        size_b = int(info.get("size") or -1)
    except ValueError:
        pass
    items_before = info.get("itemsBefore")
    items_after = info.get("itemsAfter")
    clean = (items_before == items_after) and not (info.get("stuck") or "").strip()

    tail = "耗时 %.0f 秒" % (time.time() - t0)
    if status != "OK":
        return False, ("AE 渲染失败：%s\n%s"
                       % (msg or "（没说原因）", tail))
    if not clean:
        _log(log, "  ★ AE 那边没打扫干净：before=%s after=%s stuck=%s"
             % (items_before, items_after, info.get("stuck")))
    mb = size_b / 1048576.0 if size_b and size_b > 0 else (
        os.path.getsize(dst) / 1048576.0 if os.path.exists(dst) else 0)
    note = "" if clean else "（AE 项目里有残留没删掉，日志里会列出来）"
    return True, "%.1f MB · %s%s" % (mb, tail, note)
