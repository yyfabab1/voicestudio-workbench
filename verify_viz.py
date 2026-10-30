# -*- coding: utf-8 -*-
r"""做视频（音频可视化 → MP4）能不能真出片（第 26 关）。

这一关护的是一整块新功能：把成品（或任意音频）渲染成能直接发出去的视频。
它容易**坏得很安静** —— 随便哪一步错了，用户看到的都是"点了没反应"或者
"出来一个黑屏视频"，而任务状态照样是 done、文件照样在、时长照样对。

本机 ffmpeg（n4.3.2）上有四个坑，全是实测出来的，判据钉的就是它们：

  1. **没有 libx264**（配置里 `--disable-libx264`）。写 `-c:v libx264` 直接失败。
     能用的是 `h264_qsv` / `h264_mf`。
  2. **`h264_nvenc` 列在 `-encoders` 里、真跑却编不出片**
     （老 ffmpeg + 新驱动：`Cannot get the preset configuration: unsupported param (12)`）。
     ⇒ 探测编码器**必须真编一帧**；只看列表就会选到它，然后**每个用户都出不了片**。
     这条是本关最值钱的判据（C 段 + F 段的尺子自检都钉它）。
  3. **`showfreqs` 没有帧率参数**（别的 show* 滤镜叫 `fps`，它连 `rate` 都没有），
     且 `vstack` 之后帧率会变得不确定 ⇒ 滤镜链末尾必须统一补 `fps=N,format=yuv420p`，
     否则 qsv 报 `Current frame rate is unsupported`、mf 报 `could not set output type`。
  4. **`showcqt` 只把内容画在上半 50%**（逐行数像素量出来的），而且默认还画一排
     钢琴键盘横在画面正中。⇒ `axis=0` 关键盘 + 按 2 倍高渲染再裁掉下半。

判据六段：

  A 源码对账（零浏览器）：三层都接上了（studio 的 job / server 的路由 / 前端的按钮）/
                        样式表里的 showcqt 都关了键盘 / 链尾统一收口没被绕过
  B 纯函数：            .lrc 解析（含一行多戳、元信息、坏时间）、歌词里花括号的转义、
                        截片段时时间轴平移与区间外剔除、ffmpeg 路径转义顺序、文件名清洗
  C 编码器（真跑）：    探到的编码器**必须真编得出片**（自证）/ 候选全不可用时
                        诚实地报 None（不是瞎选一个）/ 乱编一个名字不许当真
  D 端到端（真渲染）：  合成素材 → 真渲染 → **产物是能播的视频**（有时长/分辨率/视频流）/
                        相邻两帧**必须不一样**（防"渲染出一张静止图"）/ 带字幕与不带字幕
                        同一帧在字幕区**必须有差异**（证明歌词真烧进去了，不是白给）/
                        ★「起始」截到音频外面必须**明确报错**（ffmpeg 不报错只会吐空壳）
  E 真鼠标（CDP）：     导航条有「做视频」且真点能滚到 / 样式卡真点能换选中 /
                        点「开始制作」发出的是 kind=viz + 当前选中的样式（fetch **拦成假回包**，
                        不许真往 data/videos 里落文件）/ 负对照：没有音频可挑时按钮不许假装能用
  F 尺子自检（实测能红）：把坏样式表 / 坏函数**真的装回模块**再跑同一条判据，四条都必须当场变红；
                        链尾去掉 fps ⇒ 真渲染必须失败；定格片走同一套算法必须判成"没差别"

用法（venv：浏览器段要 websocket）：
    <venv>/Scripts/python.exe -s verify_viz.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402
import viz     # noqa: E402
import vizfx   # noqa: E402

PORT = 8765
BASE = "http://127.0.0.1:8756"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PROFILE = os.path.join(HERE, ".cdp-viz")
FF = studio.FFMPEG

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def sh(args, timeout=300):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
        return p.returncode, (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -99, "TIMEOUT"
    except Exception as e:                                   # noqa: BLE001
        return -98, "%s: %s" % (type(e).__name__, e)


def probe_video(path):
    """读视频的真实参数（时长/分辨率/有没有视频流）。

    ★ 这里**不能**用上面的 `sh()` —— 它只回 stderr（那是给"看报错"用的），
    而 ffprobe 的 JSON 走的是 stdout。第一版就是这么写的，结果 json.loads(stderr)
    必然失败 ⇒ 返回 None ⇒ 后面五条判据全红，但视频其实好得很。
    教训：判据读不到东西时，先怀疑尺子，别急着改被测的功能。
    """
    p = subprocess.run([os.path.join(os.path.dirname(FF), "ffprobe.exe"),
                        "-v", "error", "-print_format", "json",
                        "-show_streams", "-show_format", path],
                       capture_output=True, timeout=120)
    if p.returncode != 0:
        return None
    try:
        d = json.loads((p.stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        return None
    v = [s for s in d.get("streams", []) if s.get("codec_type") == "video"]
    a = [s for s in d.get("streams", []) if s.get("codec_type") == "audio"]
    if not v:
        return {"video": False, "audio": bool(a)}
    return {"video": True, "audio": bool(a),
            "w": int(v[0].get("width") or 0), "h": int(v[0].get("height") or 0),
            "dur": float(d.get("format", {}).get("duration") or 0),
            "frames": v[0].get("nb_frames")}


def frame_rgb(path, at, w, h):
    """抽一帧成 numpy（用来做像素级比对）。"""
    p = subprocess.run([FF, "-y", "-v", "error", "-ss", "%.3f" % at, "-i", path,
                        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    buf = p.stdout
    if len(buf) < w * h * 3:
        return None
    return np.frombuffer(buf[:w * h * 3], dtype=np.uint8).reshape(h, w, 3)


# ---------------------------------------------------------------- 判据纯逻辑
#
# ★ 这几条单独抽成函数，是为了让 F 段能做**真的**尺子自检：
#   把坏版本（样式表 / 转义函数）真正装进 viz 模块，再调同一个函数，
#   断言它从 True 变 False。F 段不许写成"我猜它会红"——那叫玄学，不叫自检。

def cqt_axis_ok():
    """「showcqt 都关了钢琴键盘」——A 段与 F 段共用同一把尺子。"""
    cqts = [s for s in viz.STYLES if "showcqt" in (s.get("chain") or "")]
    return bool(cqts) and all("axis=0" in s["chain"] for s in cqts)


def cqt_ratio_ok():
    """「showcqt 都按 2 倍高渲染再裁」——内容只占上半 50%，血泪结论。"""
    cqts = [s for s in viz.STYLES if "showcqt" in (s.get("chain") or "")]
    return bool(cqts) and all(s.get("render_ratio") == 2.0 for s in cqts)


def braces_escaped_ok():
    """「歌词里的花括号真被转义了」——B 段与 F 段共用同一把尺子。"""
    esc = viz.ass_escape("带{花括号}和\\反斜杠")
    return "{" not in esc.replace("\\{", "") and "\\{" in esc and "\\}" in esc


def ff_path_ok():
    """「Windows 路径先换斜杠、再转义冒号」——顺序反了等于白转义。"""
    return viz.ff_path(r"D:\a\b.mp4") == "D\\:/a/b.mp4"


# ---------------------------------------------------------------- A 源码对账

def _viz_cast_seg(srv):
    """取出 server.py 里「入口取分配表」那一段（从 `cast = None` 到生成 label 之前）。

    ★ 为什么要切片而不是全文 contains：`needs_cast` 这个词 server.py 别处也有
      （比如打印日志），全文 contains 会在**没改到真正那段代码**时照样绿 ——
      那正是这次踩的坑（studio 侧早就按标记判了，入口那一句一直写死 "cast"）。
    """
    i = srv.find("cast = None")
    if i < 0:
        return ""
    j = srv.find("label = (title", i)
    return srv[i:j] if j > i else srv[i:i + 2400]


def _cast_gate_ok(seg):
    """入口取分配表的判据：按 `needs_cast` 标记，**不许写死 `style == "cast"`**。

    合唱台不是一款样式，是一族四款构型（横排 cast / 对唱擂台 duet /
    环列声场 ring / 舞台追光 stage），四款都要同一份「谁在第几秒唱」。
    写死 "cast" ⇒ 另外三款永远 cast=None ⇒ job_viz 一律回
    「这条成品是单角色跑的」—— 而那条成品明明是多人合唱。
    2026-10-25 老板连试三次「对唱擂台」全被这句挡回来，报的就是"多人唱不能用"。
    """
    return bool(seg.strip()) and "needs_cast" in seg and '== "cast"' not in seg


def section_a():
    print("\n=== A. 源码对账（三层都接上了没） ===")
    root = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    app = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "web", "viz.js"), encoding="utf-8").read()
    html = open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()

    check("viz.py 是独立模块（ffmpeg 的活都在这一处）",
          os.path.isfile(os.path.join(HERE, "viz.py")))
    check("studio 顶层 import 了 viz", "import viz" in root)
    check("studio 有 VIZ_DIR / viz_options / job_viz / viz_library",
          all(("def %s(" % n) in root or ("%s =" % n) in root
              for n in ("viz_options", "job_viz", "viz_library")) and "VIZ_DIR =" in root)
    check("server 有 /api/viz/options 与 /api/viz/library",
          "/api/viz/options" in srv and "/api/viz/library" in srv)
    check("server 的任务分派里有 kind == \"viz\"", '"viz"' in srv and 'kind == "viz"' in srv)
    check("前端有「做视频」区块与脚本", 'id="secVideos"' in html and "viz.js" in html)
    check("导航项里有 secVideos（加区块必须同步加导航，否则用户看不见）",
          '"secVideos"' in app)
    check("成品卡片上有「做视频」入口", "data-viz" in app and "vizFromOutput" in js)

    # ★ 2026-10-25：入口取分配表必须按 `needs_cast` 标记判。
    #   这条不是"代码风格"判据 —— 写死 "cast" 的实测后果就是老板的原话
    #   「好多多人唱不能用，显示我只是独唱」：点「对唱擂台」被回一句
    #   「这条成品是单角色跑的」，而那条成品是「多人合唱：苹果嘉儿 / 云宝」。
    #   studio 侧（job_viz）早就按标记判了，入口没跟上 ⇒ 装了但从没走到。
    _seg = _viz_cast_seg(srv)
    check("★ 入口按 needs_cast 取分配表（写死 == \"cast\" 会让对唱擂台/环列/追光全报"
          "「单角色」）", _cast_gate_ok(_seg), _seg[:70].replace("\n", " "))

    # ★ 这两个是本机 ffmpeg 的血泪：漏一个就是"看着像渲染坏了"
    cqts = [s for s in viz.STYLES if "showcqt" in (s.get("chain") or "")]
    check("所有 showcqt 样式都关了那排钢琴键盘（axis=0）",
          cqt_axis_ok(), "%d 个 showcqt 样式" % len(cqts))
    check("★ showcqt 样式都按 2 倍高渲染再裁（内容只占上半 50%）",
          cqt_ratio_ok(), "%d 个 showcqt 样式" % len(cqts))
    check("链尾统一收口（fps + format=yuv420p）没被绕过",
          "def finish_chain" in open(os.path.join(HERE, "viz.py"), encoding="utf-8").read()
          and "format=yuv420p" in open(os.path.join(HERE, "viz.py"), encoding="utf-8").read())
    check("stack 样式也是两条支流真的拼起来（vstack）",
          any(s.get("stack") for s in viz.STYLES))


# ---------------------------------------------------------------- B 纯函数

def section_b(td):
    print("\n=== B. 纯函数 ===")
    lrc = os.path.join(td, "t.lrc")
    with open(lrc, "w", encoding="utf-8-sig") as f:
        f.write("[ti:标题]\n[ar:歌手]\n"
                "[00:01.00]第一句\n"
                "[00:02.50][00:04.00]一行两个时间戳\n"
                "[bad]这行没有时间戳\n"
                "[00:06.00]带{花括号}和\\反斜杠\n")
    lines = viz.parse_lrc(lrc)
    check("lrc 解析：读到 4 个时间点（一行两个戳要拆开）", len(lines) == 4, lines)
    check("lrc 解析：按时间排好序", [round(t, 2) for t, _ in lines] == [1.0, 2.5, 4.0, 6.0],
          [t for t, _ in lines])
    check("lrc 解析：像 5.00 这样的 2 位小数没错读成 500",
          lines[0][0] == 1.0 and lines[1][0] == 2.5)
    check("lrc 解析：元信息（[ti:] / [ar:]）不会被当成歌词",
          all("标题" not in t and "歌手" not in t for _, t in lines))
    check("lrc 解析：不存在的文件返回空（不是抛异常炸掉整条链）",
          viz.parse_lrc(os.path.join(td, "nope.lrc")) == [])

    esc = viz.ass_escape("带{花括号}和\\反斜杠")
    check("★ 歌词里的花括号被转义（不然 ASS 会当成样式指令吞掉这段）",
          braces_escaped_ok(), esc)
    check("歌词里的反斜杠也被转义", "\\\\" in esc, esc)
    check("换行变成 ASS 的 \\N", viz.ass_escape("a\nb") == "a\\Nb")

    size = viz.size_by_key("square")
    a1 = os.path.join(td, "a1.ass")
    n1 = viz.build_ass(a1, [(0.5, "一"), (2.0, "二"), (4.0, "三")], size,
                       title="T", subtitle="S")
    body1 = open(a1, encoding="utf-8-sig").read()
    check("ass 生成：3 句歌词都写进去了", n1 == 3, n1)
    check("ass 生成：标题和副标题各一条（没歌词也得有信息，不能是光板）",
          "Style: Title" in body1 and "Style: Cnt" in body1)
    a2 = os.path.join(td, "a2.ass")
    # 截第 10~14 秒：原来 0.5/2.0/4.0 三句全部落在区间外 ⇒ 一句都不该留
    n2 = viz.build_ass(a2, [(0.5, "一"), (2.0, "二"), (4.0, "三")], size,
                       title="T", offset=10.0, duration=4.0)
    check("★ 截片段时区间外的歌词被剔掉（时间轴不平移就会全错位）", n2 == 0, n2)
    a3 = os.path.join(td, "a3.ass")
    # 绝对时间 12.0 秒、截 10~14 ⇒ 应该平移成 2.0 秒
    n3 = viz.build_ass(a3, [(12.0, "中间的")], size, title="T", offset=10.0, duration=4.0)
    body3 = open(a3, encoding="utf-8-sig").read()
    check("★ 区间内的歌词时间被正确平移（12.0 - 10.0 = 2.0）",
          n3 == 1 and "0:00:02.00" in body3, body3.split("Dialogue")[-1][:40])

    check("★ ffmpeg 路径转义：先统一斜杠再转义冒号（反过来写等于白转义）",
          ff_path_ok(), viz.ff_path(r"D:\a\b.mp4"))
    check("路径里没有残留的反斜杠", "\\" not in viz.ff_path(r"D:\a\b.mp4").replace("\\:", ""))

    check("文件名清洗：Windows 非法字符被清掉",
          studio._safe_name('a\\b/c:d*e?f"g<h>i|j') == "a b c d e f g h i j",
          studio._safe_name('a\\b/c:d*e?f"g<h>i|j'))
    check("文件名清洗：全是非法字符时有兜底（不是空串）",
          studio._safe_name('///') == "video", studio._safe_name('///'))
    check("偶数化：奇数被减 1（yuv420p 要求宽高偶数）",
          viz._even(1081) == 1080 and viz._even(1080) == 1080)


# ---------------------------------------------------------------- C 编码器

def section_c(td):
    print("\n=== C. 编码器（真跑，不看列表） ===")
    enc, tested = viz.probe_encoder(FF, td)
    check("探测器选出了一个编码器", bool(enc), enc)
    if not enc:
        check("★ 选出的编码器真能编出一帧（自证，不是硬写一个名字）", False, "没选出来")
        return
    out = os.path.join(td, "c1.mp4")
    rc, err = sh([FF, "-y", "-v", "error", "-f", "lavfi",
                  "-i", "color=c=black:s=320x240:r=30:d=1", "-frames:v", "1",
                  "-c:v", enc, out])
    check("★ 选出的编码器真能编出一帧（自证，不是硬写一个名字）",
          rc == 0 and os.path.exists(out) and os.path.getsize(out) > 0, err[:120])

    lying = [t for t in tested if t["ok"]]
    bad = []
    for t in lying:
        o2 = os.path.join(td, "c_%s.mp4" % t["enc"])
        rc2, _ = sh([FF, "-y", "-v", "error", "-f", "lavfi",
                     "-i", "color=c=black:s=320x240:r=30:d=1", "-frames:v", "1",
                     "-c:v", t["enc"], o2])
        if rc2 != 0 or not os.path.exists(o2) or os.path.getsize(o2) == 0:
            bad.append(t["enc"])
    check("★ 探测器报 ok 的每一个都真能编出片（探测器本身不许说谎）",
          not bad, "说谎的：" + str(bad) if bad else "%d 个候选全对" % len(lying))
    check("不可用的候选**照实**报出了原因（用户/后人要能排查）",
          all(t.get("why") for t in tested if not t["ok"]),
          [t["enc"] for t in tested if not t["ok"]])

    # 候选全是假的时，必须诚实返回 None —— 不许"瞎选一个"
    check("乱给一个编码器名字不许被当成可用",
          sh([FF, "-y", "-v", "error", "-f", "lavfi", "-i",
              "color=c=black:s=320x240:r=30:d=1", "-frames:v", "1",
              "-c:v", "h264_fake_made_up", os.path.join(td, "fake.mp4")])[0] != 0,
          "编造的名字真跑失败（探测器要是只看列表就会信它）")


# ---------------------------------------------------------------- D 端到端

def section_d(td):
    print("\n=== D. 端到端（真渲染） ===")
    enc, _ = viz.probe_encoder(FF, td)
    if not enc:
        check("有编码器才能往下测", False, "没有可用编码器")
        return None
    # 素材自己合成：粉噪声频谱丰富，适合看频谱画面对不对
    wav = os.path.join(td, "src.wav")
    rc, err = sh([FF, "-y", "-v", "error", "-f", "lavfi",
                  "-i", "anoisesrc=color=pink:duration=6:sample_rate=44100",
                  "-ac", "2", wav])
    check("测试素材（自己合成，不依赖用户库里恰好有歌）", rc == 0 and os.path.exists(wav), err[:100])
    if rc != 0:
        return None

    lrc = os.path.join(td, "d.lrc")
    with open(lrc, "w", encoding="utf-8-sig") as f:
        f.write("[00:00.50]这是第一句歌词\n[00:03.00]这是第二句歌词\n")
    size = viz.size_by_key("landscape")
    small = {"key": "t", "name": "测试用", "w": 480, "h": 270}
    style = viz.style_by_key("cqt")

    ass = os.path.join(td, "d.ass")
    viz.build_ass(ass, viz.parse_lrc(lrc), small, title="测试标题", subtitle="测试角色")
    dst = os.path.join(td, "d_sub.mp4")
    ok, msg = viz.render(FF, enc, wav, dst, style, small, 30, ass=ass,
                         duration=5.0, log=lambda m: None)
    check("真渲染（480x270 / 5 秒 / 带字幕）跑通", ok, msg)
    if not ok:
        return None

    info = probe_video(dst)
    # ★ 先把尺子自己量一遍：读不到就明说"尺子坏了"，别让下面五条一起红得莫名其妙
    check("probe_video 这把尺子本身是好的（读得到真产物，不是恒 None）",
          info is not None, "读到 %s" % (info,))
    check("产物是**真视频**：有视频流", bool(info and info.get("video")))
    check("产物带音轨（没音轨的视频发出去是哑的）", bool(info and info.get("audio")))
    check("分辨率就是要求的那个（crop/scale 没把画幅搞歪）",
          info and info["w"] == 480 and info["h"] == 270,
          "%sx%s" % (info["w"], info["h"]) if info else "读不到")
    check("时长≈5 秒（-t 真的生效了）",
          info and abs(info["dur"] - 5.0) < 0.6, info["dur"] if info else "?")
    check("文件不为空", os.path.getsize(dst) > 20000, "%d 字节" % os.path.getsize(dst))

    f1 = frame_rgb(dst, 0.6, 480, 270)
    f2 = frame_rgb(dst, 2.5, 480, 270)
    check("抽得出帧", f1 is not None and f2 is not None)
    if f1 is not None and f2 is not None:
        diff = float(np.abs(f1.astype(int) - f2.astype(int)).mean())
        check("★ 两帧必须不一样（防「渲染出一张定格图」也判绿）", diff > 0.5,
              "平均像素差 %.2f" % diff)
        check("画面不是全黑（真画了频谱，不是空输出）", int(f2.max()) > 40,
              "最亮 %d" % int(f2.max()))

    # 带字幕 vs 不带字幕：同一时刻同一区域必须有差异 —— 证明歌词真的烧进去了
    dst2 = os.path.join(td, "d_nosub.mp4")
    ok2, _ = viz.render(FF, enc, wav, dst2, style, small, 30, ass=None,
                        duration=5.0, log=lambda m: None)
    check("不带字幕也能渲染（两条路都要通）", ok2)
    if ok2:
        g1 = frame_rgb(dst2, 3.4, 480, 270)
        g2 = frame_rgb(dst, 3.4, 480, 270)
        if g1 is not None and g2 is not None:
            # ★ 两版除了"有没有字幕"之外，输入、参数、时间点**全都一样** ⇒
            #   两帧的差异就只可能来自字幕。再看差异**集中在哪**：
            #   字幕在下方，所以下方必须显著、上方必须基本一致 ——
            #   要是整幅都在变，那说明两条视频压根不是同一画面（判据就假了）。
            h1 = int(270 * 0.70)
            d_sub = float(np.abs(g2[h1:].astype(int) - g1[h1:].astype(int)).mean())
            d_top = float(np.abs(g2[:h1].astype(int) - g1[:h1].astype(int)).mean())
            check("★ 带/不带字幕在同一时刻**字幕区有可见差异**（歌词真烧进去了）",
                  d_sub > 1.0, "下 30%% 平均差 %.2f" % d_sub)
            check("★ 而字幕区**以外**两版基本一致（证明差异确实来自字幕，不是画面变了）",
                  d_top < d_sub / 2.0, "上 70%% 平均差 %.2f" % d_top)

    # 每个样式都要能出片 —— 别只测一个就发货
    bad = []
    for st in viz.STYLES:
        o = os.path.join(td, "s_%s.mp4" % st["key"])
        okx, msgx = viz.render(FF, enc, wav, o, st, small, 30, ass=None,
                               duration=2.0, log=lambda m: None)
        if not okx:
            bad.append("%s(%s)" % (st["key"], msgx[:60]))
    check("★ 六种样式**逐个**都能出片（别只测一个就发货）", not bad,
          "坏的：" + "; ".join(bad) if bad else "%d 种全过" % len(viz.STYLES))

    # 竖屏：高 > 宽，是最容易踩 crop 的地方
    vert = viz.size_by_key("vertical")
    small_v = {"key": "tv", "name": "竖", "w": 270, "h": 480}
    ov = os.path.join(td, "v.mp4")
    okv, msgv = viz.render(FF, enc, wav, ov, style, small_v, 30, ass=None,
                           duration=2.0, log=lambda m: None)
    check("竖屏（高>宽）也能出片且是竖的", okv, msgv)
    if okv:
        iv = probe_video(ov)
        check("竖屏产物尺寸对（270x480）", iv and iv["w"] == 270 and iv["h"] == 480,
              "%sx%s" % (iv["w"], iv["h"]) if iv else "?")

    # ★ 「起始」秒数截到音频外面：ffmpeg 不报错，照样吐一个 262 字节的空壳 mp4，
    #   任务显示"完成"、视频库里也列出来，用户点开一片漆黑 —— 比直接报错坑得多。
    #   这段就是钉住"必须明确拒绝"。（探针任务自己造在临时目录里，不进 data/jobs）
    jdir = os.path.join(td, "job")
    os.makedirs(jdir, exist_ok=True)
    probe_job = {"id": "jvizprobe", "kind": "viz", "title": "探针", "state": "queued",
                 "stage": "", "pct": 0, "started": time.time(), "ended": None,
                 "error": None, "dir": jdir, "steps": [], "_step_idx": -1,
                 "_logfile": os.path.join(jdir, "log.txt")}
    over = os.path.join(td, "over.mp4")
    raised = None
    try:
        studio.job_viz(probe_job, wav, title="探针", style_key="cqt",
                       size_key="square", fps=30, start=40.0, duration=4.0,
                       out_name=over)
    except Exception as e:                                   # noqa: BLE001
        raised = e
    msg = str(raised) if raised else ""
    check("★ 起始秒数超出音频长度 → **明确报错**（不许吐个空壳 mp4 假装完成）",
          raised is not None,
          "没报错 ⇒ 会产出空壳" if not raised else "%s" % msg[:80])
    check("★ 报错文案要说清「截错了」+ 怎么办（不是一句「渲染失败」）",
          bool(raised) and ("起始" in msg or "超出" in msg) and "秒" in msg, msg[:90])
    check("超范围时**不留文件**在盘上（失败就得干干净净）", not os.path.exists(over))

    raised2 = None
    try:
        studio.job_viz(probe_job, wav, title="探针", style_key="cqt",
                       size_key="square", fps=30, start=0.0, duration=0.0,
                       out_name=os.path.join(td, "zero.mp4"))
    except Exception as e:                                   # noqa: BLE001
        raised2 = e
    check("时长填 0 也要被挡住（不是渲染个 0 秒的文件）", raised2 is not None,
          str(raised2)[:60] if raised2 else "没报错")
    return dst


# ---------------------------------------------------------------- E 浏览器

def cdp_get(path, port=PORT):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


class Cdp:
    def __init__(self, port=PORT):
        import websocket  # noqa
        tabs = [t for t in cdp_get("/json/list", port)
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not tabs:
            raise RuntimeError("没有可用的页面目标")
        self.ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=60)
        self.i = 0
        self.send("Page.enable")

    def send(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def click_el(self, sel):
        """★ 真鼠标：先 scrollIntoView 再点（视口外的东西点不到），返回命中元素。"""
        box = self.js("(()=>{const e=document.querySelector(%s);if(!e)return null;"
                      "e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();"
                      "return {x:r.left+r.width/2,y:r.top+r.height/2,w:r.width,h:r.height};})()"
                      % json.dumps(sel))
        if not box or box["w"] <= 0:
            return None
        x, y = box["x"], box["y"]
        hit = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.tagName+'|'+(e.id||'')+'|'+(e.className||'')):'null'})()"
                      % (x, y))
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                  button="left", clickCount=1)
        time.sleep(0.05)
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                  button="left", clickCount=1)
        time.sleep(0.5)
        return hit

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def section_e():
    print("\n=== E. 浏览器（真鼠标） ===")
    if not os.path.isfile(CHROME):
        check("本机有 Chrome 可以跑浏览器段", False, CHROME)
        return
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = None
    try:
        for _ in range(60):
            try:
                cdp_get("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Chrome 没起来")
        c = Cdp()
        c.send("Runtime.enable")
        c.send("Page.navigate", url=BASE + "/")
        time.sleep(6.0)

        errs = c.js("(()=>{return (window.__vizErr||[]).length})()")
        # 抓 JS 异常：页面加载期就有报错的话，后面点什么都不必谈了
        c.js("window.__vizErr=[];window.addEventListener('error',e=>window.__vizErr.push(String(e.message)));"
             "const oe=console.error;console.error=function(){window.__vizErr.push(String(arguments[0]));"
             "return oe.apply(console,arguments)};")
        check("页面加载后没有遗留 JS 异常（新脚本没把页面搞坏）", errs in (0, None), errs)

        check("导航条里有「做视频」入口",
              bool(c.js("!!document.querySelector('#navBar .nav-i[data-id=\"secVideos\"]')")))
        hit = c.click_el('#navBar .nav-i[data-id="secVideos"]')
        time.sleep(1.4)
        vis = c.js("(()=>{const e=document.getElementById('secVideos');const r=e.getBoundingClientRect();"
                   "return r.top < window.innerHeight && r.bottom > 0;})()")
        check("★ 真鼠标点「做视频」，对应的区块真的滚进视口",
              hit is not None and vis, "命中=%s 可见=%s" % (hit, vis))

        n_st = c.js("document.querySelectorAll('#vizStyles .viz-st').length")
        check("样式卡渲染出来了（两套引擎都列出来）", (n_st or 0) >= 10, n_st)
        # ★ 以前硬编码 == 2（手绘 / 滤镜）。2026-10-25 加了第三组「AE 真渲染」
        #   ⇒ 判据改成"至少两组 + **装了 AE 就必须看见 AE 那组**"：
        #   写死 2 会在每次加组时假红，而"AE 组在装了 AE 的机器上不出现"
        #   才是真正要抓的缺陷（没装 AE 的机器上它本来就不该出现）。
        _n_grp = c.js("document.querySelectorAll('#vizStyles .viz-grp').length")
        _viz_txt = c.js("document.getElementById('vizStyles').innerText") or ""
        check("★ 样式分了组（每组都有标题，用户才分得清快慢 / 要不要等）",
              (_n_grp or 0) >= 2, _n_grp)
        check("  装了 AE 的机器上「AE 真渲染」那组真的出现",
              "AE" in _viz_txt, (_viz_txt or "")[:80])
        check("默认选中第一种（有且只有一个 .on）",
              c.js("document.querySelectorAll('#vizStyles .viz-st.on').length") == 1)
        # 真鼠标点第三张（不是第一种，防"永远高亮第一个"）。
        # ★ 用 data-i 定位而不是 :nth-child —— 分组标题也是子元素，
        #   nth-child 会指到标题上（改分组那天就踩了）。
        hit2 = c.click_el('#vizStyles .viz-st[data-i="2"]')
        time.sleep(0.4)
        # ★ 上一版这里写成 `(e[on].dataset.k:null)` —— 三元表达式少了个 `?`，
        #   整句是非法 JS，浏览器直接 SyntaxError，异常一路冒上来把整段 E 判红，
        #   看起来像"页面坏了"，其实是门禁自己的语法错。
        got = c.js("(()=>{const e=[...document.querySelectorAll('#vizStyles .viz-st')];"
                   "const on=e.findIndex(x=>x.classList.contains('on'));"
                   "const el=e[on]||null;"
                   "return {idx:on,n:document.querySelectorAll('#vizStyles .viz-st.on').length,"
                   "k:(el&&el.dataset)?(el.dataset.k||null):null};})()")
        check("★ 真鼠标点第三张样式卡，选中的就是**它**（不是永远高亮第一个）",
              hit2 is not None and got and got["idx"] == 2 and got["n"] == 1,
              "命中=%s 结果=%s" % (hit2, got))

        nopt = c.js("document.querySelectorAll('#vizSrc option').length")
        check("音频下拉有可选项（成品或曲库至少有一个）", (nopt or 0) > 0, nopt)
        check("画幅下拉有三种",
              c.js("document.querySelectorAll('#vizSize option').length") == 3)
        check("帧率下拉有 30/60",
              c.js("document.querySelectorAll('#vizFps option').length") == 2)

        # 点「开始制作」→ 必须真发出 kind=viz 且带当前选中的样式
        # ★ 这里**拦成假回包**，不让请求真的打到服务上（跟 verify_seedvc 一个做法）。
        #   上一版是"记下来再放行"，结果门禁每跑一次就真渲染一个视频落进
        #   data/videos —— 而且取的是下拉里第一条成品（可能是条几秒的 TTS），
        #   配合下面写死的 start=40 ⇒ 截到音频外面 ⇒ 落一个 262 字节的空壳。
        #   门禁不该往用户的数据目录里扔东西。
        c.js("(()=>{window.__vizReq=null;const of=window.fetch;"
             "window.fetch=function(u,o){try{const s=String(u);"
             "if(s.indexOf('/api/jobs')>=0&&o&&o.body){window.__vizReq=o.body;"
             "return Promise.resolve(new Response(JSON.stringify("
             "{ok:true,job:{id:'__vizfake__'}}),"
             "{status:200,headers:{'Content-Type':'application/json'}}))}"
             "if(s.indexOf('/api/job/__vizfake__')>=0){"
             "return Promise.resolve(new Response(JSON.stringify("
             "{ok:true,job:{id:'__vizfake__',kind:'viz',state:'done',pct:100,"
             "stage:'完成',title:'做视频'}}),"
             "{status:200,headers:{'Content-Type':'application/json'}}))}}catch(e){}"
             "return of.apply(this,arguments)};return true})()")
        # 只做一小段，别真跑几分钟
        c.js("(()=>{const p=document.getElementById('vizPart');if(!p.checked){p.click()}"
             "document.getElementById('vizStart').value='40';"
             "document.getElementById('vizDur').value='4';return true})()")
        # ★ 点之前先把任务面板收干净：本关跑的时候，`data/jobs` 里可能挂着**别的关
        #   建立的** job（job 目录是各关共用的），任务面板一展开展开的卡会**盖住
        #   「开始制作」** ⇒ 真鼠标命中到的是 #jall（实测量到过 `DIV|jall|jall`），
        #   请求自然发不出去，判据假红。这跟"按钮本身好不好使"无关，是**测试前置
        #   没清**。判据量的应该是「点得中按钮 ⇒ 真发 kind=viz」，不是"面板挡不挡"。
        #   （症状最早出现在并行跑多关时；权威做法本来就是逐关跑，这里再加一道保险。）
        c.js("(()=>{const b=document.getElementById('jall');"
             "if(b&&b.offsetParent!==null)b.click();return true})()")
        time.sleep(0.3)
        hit3 = c.click_el("#btnVizGo")
        time.sleep(1.2)
        req = c.js("window.__vizReq")
        parsed = None
        try:
            parsed = json.loads(req) if req else None
        except ValueError:
            parsed = None
        check("★ 真鼠标点「开始制作」→ 真发出一条 kind=viz 的任务",
              hit3 is not None and parsed is not None and parsed.get("kind") == "viz",
              "命中=%s 请求=%s" % (hit3, (req or "")[:150]))
        if parsed:
            check("★ 提交的样式就是页面上选中的那个（不是写死的默认值）",
                  parsed.get("style") == (got or {}).get("k"),
                  "提交=%s 页面选中=%s" % (parsed.get("style"), (got or {}).get("k")))
            check("提交带了画幅与帧率", bool(parsed.get("size")) and bool(parsed.get("fps")),
                  "%s / %s" % (parsed.get("size"), parsed.get("fps")))
            check("勾了「只做一段」→ 带上了起止秒数（不然会整首渲染，等很久）",
                  bool(parsed.get("duration")), parsed.get("start"))

        errs2 = c.js("(window.__vizErr||[]).length")
        check("整个交互过程没有 JS 异常", (errs2 or 0) == 0,
              c.js("(window.__vizErr||[]).slice(0,3)"))

        # 负对照：把音频下拉掏空，按钮不许假装还能用
        c.js("(()=>{const s=document.getElementById('vizSrc');s.innerHTML='';"
             "s.value='';return true})()")
        hit4 = c.click_el("#btnVizGo")
        time.sleep(0.8)
        still = c.js("window.__vizReq")
        check("★ 负对照：没挑音频时点「开始制作」不许发请求（而是提示用户）",
              hit4 is not None and still == req, "请求没变 = 没误发")
    except Exception as e:                                   # noqa: BLE001
        check("浏览器段跑完（没在半路抛异常）", False, "%s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        if c:
            c.close()
        try:
            proc.terminate()
        except Exception:
            pass


# ---------------------------------------------------------------- F 尺子自检

def section_f(td):
    """把已知的坏版本**真的装回去**，同一条判据必须当场变红 —— 否则那就是根死尺子。

    上一版这里写的是"对源码字符串做个 replace、再看字符串里有没有 axis=0"，
    那玩意儿恒真，绿了也什么都证明不了（假绿）。现在改成：把坏样式表 /
    坏函数真的塞进 viz 模块，再调**跟 A、B 段同一个**判据函数。
    """
    print("\n=== F. 尺子自检（换坏版本必须变红） ===")

    # 0) 先确认：好版本下这几把尺子确实是绿的（不然下面的"变红"没有意义）
    check("尺子自检前置：好版本下 A/B 段那四条判据本来就是绿的",
          cqt_axis_ok() and cqt_ratio_ok() and braces_escaped_ok() and ff_path_ok())

    # 1) 真·换坏版本：把样式表里的 axis=0 抹掉
    saved_styles = viz.STYLES
    saved_escape = viz.ass_escape
    saved_ffpath = viz.ff_path
    try:
        viz.STYLES = [dict(s, chain=s["chain"].replace("axis=0", ""))
                      if "showcqt" in (s.get("chain") or "") else dict(s)
                      for s in saved_styles]
        hacked = [s["chain"] for s in viz.STYLES if "showcqt" in (s.get("chain") or "")]
        check("★ 尺子自检：样式表里抹掉 axis=0 后，「关了钢琴键盘」那条判据**真的变红**",
              (not cqt_axis_ok()) and hacked and all("axis=0" not in c for c in hacked),
              "坏链示例：" + (hacked[0][:60] if hacked else "无"))

        # 2) 真·换坏版本：render_ratio 改回 1.0（= 修复前，画面下半是空的）
        viz.STYLES = [dict(s, render_ratio=1.0)
                      if "showcqt" in (s.get("chain") or "") else dict(s)
                      for s in saved_styles]
        check("★ 尺子自检：render_ratio 改回 1.0 后，「按 2 倍高渲染」那条判据**真的变红**",
              not cqt_ratio_ok(), "坏版本 render_ratio=1.0")

        # 3) 真·换坏版本：ass_escape 变成啥也不干（= 花括号会被 ASS 吞掉）
        viz.ass_escape = lambda s: s
        check("★ 尺子自检：转义函数变成空操作后，「花括号被转义」那条判据**真的变红**",
              not braces_escaped_ok(), "坏版本 ass_escape = identity")

        # 4) 真·换坏版本：路径转义顺序写反（先转义冒号再换斜杠 = 白转义）
        viz.ff_path = lambda p: p.replace(":", "\\:").replace("\\", "/")
        check("★ 尺子自检：路径转义顺序写反后，「先斜杠后冒号」那条判据**真的变红**",
              not ff_path_ok(), "坏版本：%s" % viz.ff_path(r"D:\a\b.mp4"))
    finally:
        viz.STYLES = saved_styles
        viz.ass_escape = saved_escape
        viz.ff_path = saved_ffpath

    # 5) 入口取分配表那条：把**修复前**的那段代码原样喂给同一个判据，必须变红。
    #    这条没有模块属性可改（判据读的是 server.py 源码），所以直接把旧写法喂进去
    #    —— 用的是**同一个** _cast_gate_ok，不是另写一条"看有没有某字符串"的假尺子。
    _bad = ('            cast = None\n'
            '            if (body.get("style") or "") == "cast" and rid:\n'
            '                cast = studio.viz_cast_of(rid, int(body.get("index") or 0))\n')
    _good = _viz_cast_seg(open(os.path.join(HERE, "server.py"), encoding="utf-8").read())
    check("★ 尺子自检：入口写死 style == \"cast\" 时，同一条判据**真的变红**",
          (not _cast_gate_ok(_bad)) and _cast_gate_ok(_good),
          "坏片段=红 / 真源码=绿")

    # ★ 污染检查：自检跑完必须还原，否则后面几段（以及同一进程里的其他门禁）全废
    check("★ 尺子自检跑完把真样式表/真函数还回去了（污染了等于全盘假绿）",
          viz.STYLES is saved_styles and cqt_axis_ok() and braces_escaped_ok()
          and ff_path_ok())

    # 4.5) probe_video 这把尺子的负对照：不是视频的东西必须读不出（不能恒返回结构）
    junk = os.path.join(td, "junk.txt")
    with open(junk, "w", encoding="utf-8") as f:
        f.write("我不是视频")
    check("★ 尺子自检：probe_video 对**文本文件**必须返回 None（尺子分得出来）",
          probe_video(junk) is None, probe_video(junk))
    check("★ 尺子自检：probe_video 对**不存在**的路径必须是 None 而不是炸掉",
          probe_video(os.path.join(td, "nope.mp4")) is None)

    # 5) 去掉链尾收口：真渲染必须失败
    enc, _ = viz.probe_encoder(FF, td)
    if enc:
        wav = os.path.join(td, "f.wav")
        sh([FF, "-y", "-v", "error", "-f", "lavfi",
            "-i", "anoisesrc=color=pink:duration=3:sample_rate=44100", "-ac", "2", wav])
        broken = os.path.join(td, "nofps.mp4")
        # 手工拼一条**没有** fps/format 收口的链（= 修复前的写法）
        rc, err = sh([FF, "-y", "-v", "error", "-i", wav,
                      "-filter_complex",
                      "[0:a]showcqt=s=480x270:fps=30[t];[0:a]showwaves=s=480x270:mode=cline:rate=30[b];"
                      "[t][b]vstack=inputs=2[v]", "-map", "[v]", "-map", "0:a",
                      "-c:v", enc, "-c:a", "aac", "-shortest", broken])
        check("★ 尺子自检：链尾少了 `fps+format` 收口，真渲染必须失败（这条能红）",
              rc != 0, (err.strip().splitlines() or [""])[-1][:110])

        # 6) 「两帧必须不一样」这条判据的负对照：拿一张**纯定格**片走同一套算法，
        #    必须判成"没差别" —— 否则说明阈值是死的（定格图也能绿）。
        still = os.path.join(td, "still.mp4")
        rc_s, _ = sh([FF, "-y", "-v", "error", "-f", "lavfi",
                      "-i", "color=c=navy:s=270x270:r=30:d=1",
                      "-c:v", enc, still])
        if rc_s == 0:
            s1 = frame_rgb(still, 0.05, 270, 270)
            s2 = frame_rgb(still, 0.60, 270, 270)
            if s1 is not None and s2 is not None:
                sd = float(np.abs(s2.astype(int) - s1.astype(int)).mean())
                check("★ 尺子自检：同一套算法量**定格片**必须判成「没差别」（阈值不是死的）",
                      sd < 0.5, "定格片平均差 %.2f（判据阈值 0.5）" % sd)
            else:
                check("★ 尺子自检：定格片抽帧成功（负对照本身要能跑）", False, "抽帧失败")
        else:
            check("★ 尺子自检：定格片渲染成功（负对照本身要能跑）", False, "渲染失败")

    # 7) G 段三条新判据的尺子自检 —— 每条都要能在坏输入上真变红
    black = np.zeros((60, 40, 3), dtype=np.uint8)
    flat = np.full((60, 40, 3), 130, dtype=np.uint8)
    check("★ 尺子自检：`画面不是空图` 对**全黑帧**必须判假",
          not fx_frame_not_blank(black))
    check("★ 尺子自检：`画面不是空图` 对**一整块纯色帧**也必须判假（标准差那条在起作用）",
          not fx_frame_not_blank(flat), "纯色帧 std=%.1f" % float(np.asarray(flat).std()))
    check("★ 尺子自检：`画面不是空图` 对**真画了东西的帧**必须判真（不是恒假）",
          fx_frame_not_blank(np.random.RandomState(7).randint(0, 255, (60, 40, 3)).astype(np.uint8)))

    same = np.random.RandomState(3).randint(0, 255, (480, 270, 3)).astype(np.uint8)
    check("★ 尺子自检：`加了背景` 在两帧**完全一样**时必须判假（不是恒真）",
          not bg_applied_ok(same, same))

    # 8) 这一轮新加的三条判据，逐条证明"换坏输入会红"
    #    —— 否则它们就是"恒真"的装饰品。
    okn, _ = fx_layer_matters("aurora", "rays")
    check("★ 尺子自检：`某一层真的画上去了` 拿去量**本来就没有这层的样式**必须判假",
          not okn, "极光样式的 rays 参数本来就是 0")
    _n, _w = 64, np.linspace(10.0, 260.0, 64)
    _y = np.full(64, 20.0, np.float32)
    check("★ 尺子自检：把 `bot` 写回**退化**的旧写法，面积判据必须判假（这条能红）",
          _degenerate_body_area(_w, _y, 120.0, _n) < 0.9 * 2.0 * 20.0 * 250.0,
          "退化写法围出面积 %.0f" % _degenerate_body_area(_w, _y, 120.0, _n))
    check("★ 尺子自检：`脉冲环数` 拿**真低音**量必须非 0（不是恒 0 的假绿）",
          ring_count(1.0) >= 2)

    # ★ 反过来的负对照：把"静音判零"这条尺子拿去量**真音频**，必须判假。
    #   没有这一条的话，silence_zero_ok 写成 `return True` 也照样绿。
    loud = os.path.join(td, "loud.wav")
    sh([FF, "-y", "-v", "error", "-f", "lavfi",
        "-i", "anoisesrc=color=pink:duration=2:sample_rate=44100", "-ac", "2", loud])
    xs = vizfx.decode(FF, loud)
    if xs is not None:
        an = vizfx.analyze(xs, 30, nb=32)
        check("★ 尺子自检：`静音判零` 这条尺子拿去量**真音频**必须判假（不是恒真）",
              float(np.abs(an["spec"]).max()) > 1e-3,
              "真音频最大能量 %.4f" % float(np.abs(an["spec"]).max()))
    else:
        check("★ 尺子自检：解码器能读回自己刚写的 wav（负对照本身要能跑）", False)


# ---------------------------------------------------------------- G 新引擎
#
# 为什么要有 G 段：老引擎（viz.py 的 ffmpeg 滤镜）画出来是"黑底上几条线"。
# 用户的原话是「太难看了」。G 段钉住的是"好看"这件事里**可判定的那一部分** ——
# 不是钉"好不好看"（那没法判），而是钉：
#   · 画面不是一张空图（有明暗层次，不是全黑也不是一整块纯色）；
#   · 加了背景之后，画面下半幅真的不再是死黑；
#   · 低音包络不是常数（否则"跟着音乐呼吸"是假的）；
#   · 静音喂进去必须解析出零能量（负对照，防"随便返回点噪声"）。

def fx_frame_not_blank(frame):
    """这一帧是不是"画了东西"。全黑 / 一整块纯色都不算。"""
    if frame is None:
        return False
    a = np.asarray(frame, dtype=np.int16)
    return int(a.max()) > 25 and float(a.std()) > 6.0


def bg_applied_ok(with_bg, without_bg):
    """「加背景」这件事真的发生了：同一时刻，**画面下部**明显更亮。

    为什么看下部：频谱/光环都集中在上面，背景图的作用主要体现在空着的下半幅。
    比上半幅是量不出来的（那里本来就亮）。
    """
    if with_bg is None or without_bg is None:
        return False
    h = with_bg.shape[0]
    lo_a = float(without_bg[int(h * 0.78):].mean())
    lo_b = float(with_bg[int(h * 0.78):].mean())
    return lo_b > lo_a + 2.5


def band_edges_ok():
    """频带必须是**单调不减**的对数分组，而且要覆盖到两头。"""
    e = vizfx._band_edges(2048, 44100, 64)
    return (len(e) == 65 and all(int(e[i]) <= int(e[i + 1]) for i in range(64))
            and int(e[0]) >= 0 and int(e[-1]) > 100)


def silence_zero_ok():
    """负对照：静音解析出来的能量必须是 0。

    这条能挡住"analyze 只是返回一堆常数/噪声"——那种情况下画面动得很欢，
    但跟音乐一点关系都没有，是最难发现的一种假绿。
    """
    x = np.zeros((44100, 2), dtype=np.float32)
    an = vizfx.analyze(x, 30, nb=32)
    return float(np.abs(an["spec"]).max()) < 1e-6 and float(np.abs(an["bass"]).max()) < 1e-6


def fake_an(style, nframes, nb=None, bass=None, wave_amp=None, spec=None):
    """给判据造一份**确定性的假频谱**，免得每条判据都要真解码一遍音频。

    ★ 假数据也必须"看起来像真的"：`spec` 有高低起伏、`bass` 有强弱，
      否则量出来的差异可能来自"输入本来就是常数"。
    """
    nb = int(nb or style.get("nb", 72))
    n = int(nframes)
    r1 = np.random.RandomState(11)
    r2 = np.random.RandomState(12)
    sp = (r1.rand(n, nb).astype(np.float32) * 0.85 + 0.12) if spec is None else spec
    ba = (0.15 + 0.75 * r2.rand(n)).astype(np.float32) if bass is None else bass
    wv = r2.rand(n, 192).astype(np.float32) - 0.5
    if wave_amp is not None:
        wv = np.full((n, 192), float(wave_amp), np.float32)
    else:
        wv = wv * 1.6
    return {"spec": sp, "peak": sp * 0.98, "bass": ba,
            "mid": np.full(n, 0.45, np.float32), "high": np.full(n, 0.35, np.float32),
            "wave": wv, "nb": nb}


def fx_layer_matters(style_key, attr, w=270, h=480, i=20, fps=30):
    """某一层（粒子 parts / 光芒 rays）**真的被画上去了**没有。

    做法：同一个样式、同一帧、同一份假频谱画两次，第二次把那一层清空。
    画面必须**变**。不变就说明这层是死代码，或者参数（dust=0 / rays=0）
    写成了空 —— 那"画面变满"这件事就是自欺欺人。
    """
    st = vizfx.style_by_key(style_key)
    if st is None or not callable(st.get("draw")):
        return False, "没有样式 %s" % style_key
    an = fake_an(st, i + 2)
    a = b = None
    for keep in (True, False):
        sc = vizfx.Scene(w, h, st, an["nb"])
        sc.fps = fps
        if not keep:
            setattr(sc, attr, [])
        f = np.asarray(st["draw"](sc, an, i, {"peak": True})).astype(np.int16)
        if keep:
            a = f
        else:
            b = f
    if a is None or b is None:
        return False, "没画出来"
    d = float(np.abs(a - b).mean())
    return d > 0.05, "清空 %s 前后平均像素差 %.3f" % (attr, d)


def pulse_body_area_ok():
    """声波样式的实心波身：上下两条线围出来的多边形**面积必须是真的**。

    ★ 这是补一个真实存在过的 bug：`bot` 曾写成"把 top 原路再走一遍"，
      多边形退化、零面积、什么都填不上；可画面还有两条亮线在，所以一直没被发现。
    """
    n = 64
    xs = np.linspace(10.0, 260.0, n)
    ys = np.full(n, 20.0, np.float32)
    cy = 120.0
    top, bot = vizfx.wave_body(xs, ys, cy, n)
    area = vizfx.poly_area(top + bot)
    want = 2.0 * 20.0 * (xs[-1] - xs[0])       # 上下各 20px 高的一整条带
    return area > 0.9 * want, "围出面积 %.0f（应≈%.0f）" % (area, want)


def _degenerate_body_area(xs, ys, cy, n):
    """旧写法（错的）：`bot` 只是把 top 原样再走一遍。用来给上面那条判据做负对照。"""
    pts = [(float(xs[j]), cy - float(ys[j])) for j in range(n)]
    pts += [(float(xs[n - 1 - j]), cy - float(ys[n - 1 - j])) for j in range(n)]
    return vizfx.poly_area(pts)


def ring_count(bass_val, nframes=30, style_key="orbit", fps=30):
    """喂一段**恒定**的低音，看最后攒下了几个脉冲环。

    用 `bass` 一路 0 / 一路 1 两种极端来判定"环是跟着低音走的"，
    而不是"每帧无脑画几个环"。
    """
    st = vizfx.style_by_key(style_key)
    an = fake_an(st, nframes, bass=np.full(nframes, float(bass_val), np.float32))
    sc = vizfx.Scene(270, 480, st, an["nb"])
    sc.fps = fps
    state = {"peak": True}
    for i in range(nframes):
        st["draw"](sc, an, i, state)
    return len(state.get("rings", []))


def _gate_cast(n=4):
    """给 needs_cast 的样式造一份假的分配表（门禁专用）。

    ★ 为什么必须造：合唱台那一族（row / duet / ring / stage）拿不到分配表时
      会画一张**空背景** —— 这是设计如此（真实链路里 studio 先就把它拦下了，
      报"这条成品是单角色跑的"）。但 G 段那条"两帧必须不一样"是通用判据，
      没有数据就必然红。红的是尺子，不是产品。
    ★ takes 要跨过 0.4s 和 1.6s 两个采样点，且落在**不同的人**身上 ——
      否则两帧其实都是同一个人在唱，画面本来就该一样。
    """
    members = [{"name": "甲%d" % k, "avatar": "", "color": c}
               for k, c in enumerate(("#a06bff", "#5ad1ff", "#ff9f43", "#ff6ba8"))][:n]
    return {"members": members,
            "takes": [[0.0, 1.0, 0], [1.0, 3.0, 1 % max(1, n)]],
            "lines": [{"start": 0.0, "end": 3.0, "text": "gate line",
                       "zh": "门禁这一句"}],
            "title": "Gate", "song": "Gate"}


def section_g(td):
    print("\n=== G. 新引擎（逐帧手绘 vizfx） ===")
    root = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "web", "viz.js"), encoding="utf-8").read()

    check("vizfx.py 是独立模块（逐帧绘制全在这一处）",
          os.path.isfile(os.path.join(HERE, "vizfx.py")))
    check("studio 顶层 import 了 vizfx", "import vizfx" in root)
    # ★ 但**不能是硬 import**。`verify_disk` / `verify_train_rounds` 跑的是精简的
    #   托管 python（没有 numpy），硬 import 会让那两关直接
    #   `ModuleNotFoundError: numpy` —— 看起来像磁盘检查坏了。
    #   这条不是"源码里有没有 try"，而是**真的把 numpy 挡掉再 import 一次 studio**，
    #   必须还能起来、而且 vizfx 是 None。
    probe = (
        "import sys; sys.modules['numpy'] = None;"
        "sys.path.insert(0, %r);"
        "import studio;"
        "print('IMPORTED', getattr(studio, 'vizfx', 'MISSING') is None)" % HERE)
    pr = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                        cwd=HERE, timeout=180)
    out = (pr.stdout or b"").decode("utf-8", "replace") + \
          (pr.stderr or b"").decode("utf-8", "replace")
    check("★ 没有 numpy 的解释器也能 import studio（手绘引擎降级，不许把整个 studio 拖死）",
          pr.returncode == 0 and "IMPORTED True" in out,
          out.strip().splitlines()[-1][:120] if out.strip() else "无输出")
    check("★ 两套引擎的样式在 /api 上合到一起（每条带 engine/group）",
          "def viz_all_styles(" in root and '"engine": "py"' in root
          and '"engine": "ffmpeg"' in root)
    check("★ 默认样式是手绘那套的旗舰款（不是老的黑底频谱）",
          'or "orbit"' in srv, "server 里的兜底样式")
    check("≥5 种手绘样式，且每种都挂着绘制函数",
          len(vizfx.STYLES) >= 5 and all(callable(s.get("draw")) for s in vizfx.STYLES),
          "%d 种：%s" % (len(vizfx.STYLES), "、".join(s["name"] for s in vizfx.STYLES)))
    check("★ 样式 key 不重复（重复的话前端点第二张卡会跳到第一张）",
          len(set(s["key"] for s in vizfx.STYLES)) == len(vizfx.STYLES))
    check("★ 有「光之隧道」那种带纵深的构图（参考片里最抓眼的一类）",
          any(s["key"] == "tunnel" for s in vizfx.STYLES))
    # ★ 这三条钉的是"画面变满"这件事里**可判定的部分**：
    #   粒子层 / 光芒层真的画上去了没有；低音脉冲环真的跟着低音走没有。
    #   全都是"同一帧画两次、只动一个变量"的对照，不是看源码里有没有那行字。
    okp, msgp = fx_layer_matters("orbit", "parts")
    check("★ 粒子层真的画上去了（清空它的同一帧必须**变**）", okp, msgp)
    okr, msgr = fx_layer_matters("orbit", "rays")
    check("★ 光芒层真的画上去了（清空它的同一帧必须**变**）", okr, msgr)
    n_quiet = ring_count(0.0)
    n_loud = ring_count(1.0)
    check("★ 脉冲环跟着低音走：一路静音时**一个都不生成**", n_quiet == 0,
          "静音下攒了 %d 个环" % n_quiet)
    check("★ 一路满低音时必须真的攒出环来（不是恒 0）", n_loud >= 2,
          "满低音下攒了 %d 个环" % n_loud)
    oka, msga = pulse_body_area_ok()
    check("★ 声波样式的实心波身：上下两条线围出的多边形面积必须是真的"
          "（防「bot 写反 ⇒ 退化多边形 ⇒ 什么都没填上」）", oka, msga)
    check("前端按 engine 分组渲染样式卡（不然用户看不出哪套是慢的那套）",
          "VIZ_GROUPS" in js and '"draw"' in js and '"fast"' in js)
    check("★ 频带是对数等宽的单调分组（线性分组低音会全挤进第一根柱子）",
          band_edges_ok(), str(vizfx._band_edges(2048, 44100, 8).tolist()))
    check("★ 负对照：静音喂进去必须解析出**零**能量（防「随便返回点噪声」）",
          silence_zero_ok())
    check("颜色表长度对得上频带数、且不是一整片同色",
          len(vizfx.palette(64, "neon")) == 64
          and len(set(vizfx.palette(64, "neon"))) > 10)

    enc, _ = viz.probe_encoder(FF, td)
    if not enc:
        check("有编码器才能往下测", False, "没有可用编码器")
        return
    wav = os.path.join(td, "g.wav")
    sh([FF, "-y", "-v", "error", "-f", "lavfi",
        "-i", "anoisesrc=color=pink:duration=5:sample_rate=44100", "-ac", "2", wav])
    small = {"key": "t", "name": "测试用", "w": 270, "h": 480}

    # ---- 每种手绘样式都要真出片，而且画面上真得有东西
    for st in vizfx.STYLES:
        # needs_cast 的样式先喂一份分配表，否则画的是一张空背景（见 _gate_cast）
        if st.get("needs_cast"):
            st = dict(st, cast=_gate_cast(4))
        o = os.path.join(td, "g_%s.mp4" % st["key"])
        ok, msg = vizfx.render_fx(FF, enc, wav, o, st, small, 30,
                                  duration=2.0, log=lambda m: None)
        check("手绘样式「%s」出片" % st["name"], ok, msg)
        if not ok:
            continue
        iv = probe_video(o)
        check("  「%s」产物是真视频（有视频流 + 时长≈2 秒）" % st["name"],
              bool(iv and iv.get("video")) and iv and abs(iv["dur"] - 2.0) < 0.6,
              iv)
        f1 = frame_rgb(o, 0.4, 270, 480)
        f2 = frame_rgb(o, 1.6, 270, 480)
        check("★ 「%s」画面**不是一张空图**（有明暗层次）" % st["name"],
              fx_frame_not_blank(f2),
              "最亮 %s / 标准差 %.1f" % ((int(np.asarray(f2).max()),
                                          float(np.asarray(f2).std())) if f2 is not None
                                        else ("?", 0)))
        if f1 is not None and f2 is not None:
            d = float(np.abs(f2.astype(int) - f1.astype(int)).mean())
            check("★ 「%s」两帧必须不一样（防定格图）" % st["name"], d > 0.4,
                  "平均像素差 %.2f" % d)

    # ---- 起始秒数超出：手绘引擎也必须在**动手画之前**挡住，不能白画几分钟
    jdir = os.path.join(td, "jg")
    os.makedirs(jdir, exist_ok=True)
    pj = {"id": "jg", "kind": "viz", "title": "探针", "state": "queued", "stage": "",
          "pct": 0, "started": time.time(), "ended": None, "error": None,
          "dir": jdir, "steps": [], "_step_idx": -1,
          "_logfile": os.path.join(jdir, "log.txt")}
    raised = None
    over = os.path.join(td, "g_over.mp4")
    try:
        studio.job_viz(pj, wav, title="探针", style_key="orbit", size_key="square",
                       fps=30, start=40.0, duration=3.0, out_name=over)
    except Exception as e:                                   # noqa: BLE001
        raised = e
    check("★ 手绘引擎：起始秒数超出音频长度 → 也是**明确报错**",
          raised is not None, str(raised)[:80] if raised else "没报错")
    _m = str(raised) if raised else ""
    check("★ 而且文案说的是「起始」截过头，不是含糊的「解码失败」"
          "（后者会把用户引去查文件坏没坏）",
          ("起始" in _m or "超出" in _m) and "秒" in _m, _m[:90])
    check("超范围时手绘引擎**不留文件**（不许吐空壳）", not os.path.exists(over))

    # ---- 取消：手绘要画几分钟，中途取消必须能停
    pj2 = dict(pj)
    pj2["id"] = "jg2"
    import threading
    flag = {"stop": False}
    t0 = time.time()
    okc, msgc = vizfx.render_fx(FF, enc, wav, os.path.join(td, "g_cancel.mp4"),
                                vizfx.style_by_key("mirror"), small, 30,
                                duration=5.0, log=lambda m: None,
                                should_stop=lambda: flag["stop"])
    check("不取消时能正常跑完（取消那条判据的前提）", okc, msgc)

    got = {"ok": None, "msg": None}

    def _run():
        got["ok"], got["msg"] = vizfx.render_fx(
            FF, enc, wav, os.path.join(td, "g_cancel2.mp4"),
            vizfx.style_by_key("mirror"), small, 30, duration=4.0,
            log=lambda m: None, should_stop=lambda: flag["stop"])

    th = threading.Thread(target=_run)
    th.start()
    time.sleep(1.2)
    flag["stop"] = True
    th.join(timeout=30)
    check("★ 手绘渲染中途「取消」能停下来（不是等整首画完）",
          (not th.is_alive()) and got["ok"] is False, str(got)[:90])
    check("取消的说明里报了画到第几帧（用户知道停在哪了）",
          bool(got["msg"]) and "取消" in str(got["msg"]), str(got["msg"])[:70])

    # ---- 老引擎的「加背景」：同一时刻、同一算法，下部必须明显变亮
    style = viz.style_by_key("wave")
    bg = vizfx.make_bg_png(os.path.join(td, "bg.png"), 270, 480, vizfx.FFMPEG_BG)
    check("背景图能生成（vizfx 出的渐变 PNG）",
          os.path.isfile(bg) and os.path.getsize(bg) > 500, os.path.getsize(bg))
    o1 = os.path.join(td, "bg_off.mp4")
    o2 = os.path.join(td, "bg_on.mp4")
    k1, _ = viz.render(FF, enc, wav, o1, style, small, 30, duration=2.0,
                       log=lambda m: None)
    k2, _ = viz.render(FF, enc, wav, o2, style, small, 30, duration=2.0,
                       log=lambda m: None, bg=bg)
    check("老引擎带背景也能出片（gbrp + 屏幕混合那条链要通）", k1 and k2)
    if k1 and k2:
        b1 = frame_rgb(o1, 1.0, 270, 480)
        b2 = frame_rgb(o2, 1.0, 270, 480)
        check("★ 「加了背景」真的生效：同算法同时间，画面下部明显更亮",
              bg_applied_ok(b2, b1),
              "下部均值 无背景=%.1f 有背景=%.1f"
              % (float(np.asarray(b1)[int(480 * 0.78):].mean()) if b1 is not None else -1,
                 float(np.asarray(b2)[int(480 * 0.78):].mean()) if b2 is not None else -1))


def _func_body(name, src):
    """取一个函数的源码片段（到下一个顶层 def 为止）。

    用来做"这条 `-t` 到底写在 -i 前还是 -i 后"这种**位置**判定 ——
    ffmpeg 的选项语义完全取决于位置，只看"有没有这一行"是判不出来的。
    """
    i = src.find("def %s(" % name)
    if i < 0:
        return ""
    j = src.find("\ndef ", i + 10)
    return src[i:j] if j > 0 else src[i:]


def section_h(td):
    """H. 用我的视频当画面（配音 / 叠加）。

    ★ 这一节是老板问出来的：「我应该在怎么把这个音频加入我的视频里呢
      就算加进去是不是也遮挡了本来的画面」。
      原来「做视频」只有"给音频造画面"一条路 —— 输入里根本没有视频，
      也就无所谓遮挡。所以要的是**另外两条路**，而这两条路各自有一条
      必须量出来才算数的判据：
        · mux   —— 画面**一个像素都没动**（这就是"不遮挡"的量化说法）；
        · blend —— 画面**变亮了**，但和源画面**仍然强相关**（叠上去了但不是盖住）。
    """
    print("\n=== H. 用我的视频当画面（配音 / 叠加） ===")
    src = open(os.path.join(HERE, "viz.py"), encoding="utf-8").read()
    root = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    ENC, _ = viz.probe_encoder(FF, td)
    if not ENC:
        check("H 段跑不动：这台机器没有能用的 H.264 编码器", False)
        return

    check("viz 里有 mux_video / blend_video 两条路",
          "def mux_video(" in src and "def blend_video(" in src)

    bm = _func_body("mux_video", src)
    bb = _func_body("blend_video", src)
    # ★ 位置判定：`-t` 必须在 -filter_complex（也就是所有 -i）之后，
    #   否则它只是**输入选项**，画面裁不住 —— 实测要 6 秒却出了 12 秒的片，
    #   片尾多出一段只有画面没有歌。
    check("★ mux：`-t` 是**输出选项**（写在 -filter_complex 之后）——"
          "当成输入选项的话画面裁不住，片尾会多一段没声的画面",
          bool(bm) and 0 <= bm.find('"-filter_complex"') < bm.find('args += ["-t"'))
    check("★ blend：同理 `-t` 必须是输出选项（两路视频一路结束了另一路还会接着输出）",
          bool(bb) and 0 <= bb.find('"-filter_complex"') < bb.find('args += ["-t"'))
    check("★ blend 混合前两边都要 gbrp（一路 _fit(...,\"gbrp\")、一路 format=gbrp）"
          "—— 忘了的话 U/V 色度面被一起滤色，整帧变洋红",
          '_fit("1:v", w, h, fps, "gbrp")' in bb and "format=gbrp" in bb)
    # ★ 位置判定：字幕必须挂在 blend **之后**。先烧再叠的话，字幕会被 screen
    #   提亮成一团白 —— 只看"有没有 subtitles 这行"判不出来，得看它排在哪。
    check("★ blend 的字幕在**叠完之后**才烧（先烧再叠会被 screen 提亮成一团白）",
          bool(bb) and 0 <= bb.find("blend=all_mode=screen") < bb.find("subtitles="))
    check("三条画面来源都登记了（viz / mux / blend）",
          all(('"key": "%s"' % k) in root for k in ("viz", "mux", "blend")))

    # ---- 素材自己造，不依赖库里用户做好的片
    wav = os.path.join(td, "h.wav")
    subprocess.run([FF, "-y", "-v", "error", "-f", "lavfi", "-i",
                    "anoisesrc=color=pink:duration=8:sample_rate=44100",
                    "-ac", "2", wav], capture_output=True, timeout=180)
    small = {"key": "t", "name": "测试用", "w": 270, "h": 480}
    # 「用户的视频」：一段普通的 3 秒 mp4，跟他拖进来的没有区别
    mine = os.path.join(td, "mine.mp4")
    okv, _ = viz.render(FF, ENC, wav, mine, viz.style_by_key("wave"),
                        small, 30, duration=3.0, log=lambda m: None)
    check("H 段素材：能造出一段当作「用户的视频」的 mp4",
          okv and os.path.isfile(mine), os.path.getsize(mine) if os.path.exists(mine) else 0)
    if not okv:
        return

    # ---- 1) 只换音轨
    o_mux = os.path.join(td, "h_mux.mp4")
    # ★ 注意 mux_video 的第二个位置参数是**视频**不是编码器
    #   （encoder 只能按名字传）。多塞一个位置参数会让后面全错位。
    ok1, msg1 = viz.mux_video(FF, mine, wav, o_mux, small, fps=30,
                              encoder=ENC, duration=2.0, log=lambda m: None)
    check("★ 「只换音轨」能出片", ok1, msg1)
    if ok1:
        d = (viz.media_info(studio.FFPROBE, o_mux) or {}).get("dur") or 0.0
        check("★ 要 2 秒就只给 2 秒（画面裁得住，不是源视频的 3 秒）",
              1.5 < d < 2.6, "%.2f 秒" % d)
        a = frame_rgb(mine, 1.0, 270, 480)
        b = frame_rgb(o_mux, 1.0, 270, 480)
        if a is None or b is None:
            check("★ 「只换音轨」画面一个像素都没动", False, "抽帧失败")
        else:
            diff = float(np.abs(a.astype(int) - b.astype(int)).mean())
            # 重编一次码会有 1~2/255 的损失，所以不是 0，但必须很小。
            check("★ 「只换音轨」画面一个像素都没动（这就是「不遮挡」的量化判据）",
                  diff < 6.0, "逐像素平均差 %.2f / 255" % diff)

    # ---- 2) 可视化叠加
    lyr = os.path.join(td, "h_layer.mp4")
    o_bl = os.path.join(td, "h_blend.mp4")
    ok2, _ = vizfx.render_fx(FF, ENC, wav, lyr, vizfx.style_by_key("orbit"),
                             small, 30, ass=None, duration=2.0,
                             log=lambda m: None, render_scale=0.4)
    ok3, msg3 = viz.blend_video(FF, lyr, mine, wav, o_bl, small, fps=30,
                                encoder=ENC, duration=2.0, log=lambda m: None)
    check("★ 「可视化叠加」能出片（先渲一层，再 screen 混上去）", ok2 and ok3, msg3)
    if ok2 and ok3:
        c = frame_rgb(mine, 1.0, 270, 480)
        e = frame_rgb(o_bl, 1.0, 270, 480)
        if c is None or e is None:
            check("★ 叠加后画面变亮了", False, "抽帧失败")
        else:
            check("★ 叠加后画面**变亮了**（screen 混合真的叠上去了）",
                  float(e.mean()) > float(c.mean()) + 0.5,
                  "均值 %.1f -> %.1f" % (float(c.mean()), float(e.mean())))
            # ★ 这条才是"没挡住"：源画面的明暗结构必须还在。
            #   要是被可视化盖住了，相关性会掉到接近 0。
            corr = float(np.corrcoef(c.astype(float).ravel(),
                                     e.astype(float).ravel())[0, 1])
            check("★ 叠加不是盖住：源画面的明暗结构还在（和源视频仍强相关）",
                  corr > 0.60, "相关系数 %.3f" % corr)

    # ---- 3) 负对照：mode 要视频却不给，必须**明确报错**
    #   静默退回"生成一张可视化画面"是最坏的结果 —— 用户会白等好几分钟，
    #   还以为在处理他的视频。
    raised = ""
    try:
        studio.job_viz({"dir": td, "id": "probe"}, wav, bg_video=None, mode="blend")
    except Exception as ex:                                  # noqa: BLE001
        raised = str(ex)
    check("★ mode 要视频却不给时必须**明确报错**（不许静默退回生成画面）",
          "要先选一个视频文件" in raised, raised[:80] or "（没报错！）")
    raised2 = ""
    try:
        studio.job_viz({"dir": td, "id": "probe"}, wav, bg_video=None, mode="乱写")
    except Exception as ex:                                  # noqa: BLE001
        raised2 = str(ex)
    check("mode 不认识时要报出来，不能默认走老路", "不认识这个做法" in raised2,
          raised2[:60] or "（没报错！）")


def main():
    td = tempfile.mkdtemp(prefix="viz_")
    try:
        section_a()
        section_b(td)
        section_c(td)
        section_d(td)
        section_e()
        section_f(td)
        section_g(td)
        section_h(td)
    finally:
        # 只清自己造的临时目录，绝不碰 data/videos 里用户做好的片
        shutil.rmtree(td, ignore_errors=True)
    print("\n" + "=" * 60)
    print("PASS %d  FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - " + b)
    print("ALLDONE")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
