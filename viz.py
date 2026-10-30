# -*- coding: utf-8 -*-
r"""音频可视化视频渲染（把成品变成能直接发出去的 MP4）。

为什么单独一个模块
------------------------------------------------------------------
`studio.py` 已经五千多行，可视化这块自成一体：样式表 / 滤镜链 / 字幕 /
编码器探测，全是"跟 ffmpeg 打交道"的活，跟角色库没关系。拆出来也好测。

不 import studio：需要的（ffmpeg 路径、日志回调）由调用方传进来，
免得 studio ↔ viz 互相 import 打死结。本模块只依赖标准库 + ffmpeg。

这台机器上的 ffmpeg 有三个坑（都实测过，不是猜的）
------------------------------------------------------------------
1. **没有 libx264**（`--disable-libx264`）。所以不能写 `-c:v libx264`，
   必须走硬件编码器。能用的是 `h264_qsv`（Intel 核显）和 `h264_mf`
   （Windows MediaFoundation）；`h264_nvenc` 虽然**列在 -encoders 里**
   但真跑必报 `Cannot get the preset configuration: unsupported param (12)`
   —— 2021 年的 ffmpeg 配新驱动，preset 查询接口对不上；`h264_amf` 缺 DLL。
   ⇒ 所以探测必须**真编一帧**，只看列表会选到坏的那个。
2. **`showfreqs` 没有帧率参数**（别的 show* 滤镜叫 `fps`，它连 `rate` 都没有）。
   链尾必须补一个 `fps=N`，否则帧率不定。
3. **滤镜链末尾必须 `format=yuv420p`**，且帧率要是标准值 ——
   否则 qsv 报 `Current frame rate is unsupported`、
   mf 报 `could not set output type (80004005)`。
   实测 `vstack` 拼完两条视频流后帧率会变得不确定，就是靠链尾这两个补回来的。
"""
import json
import os
import re
import subprocess
import time

# ---------------------------------------------------------------- 样式表

# 每个样式的 `chain` 是滤镜链模板，可用占位符：
#   {W} {H} 画幅像素、{FPS} 帧率
# ★ 模板里**不要**再写帧率之外的收尾 —— 收尾统一由 `finish_chain()` 补。
STYLES = [
    {
        "key": "cqt", "name": "频谱瀑布", "tag": "最像音乐可视化",
        # ★ `axis=0` 关掉那排钢琴键盘 —— 默认是画的，而且它横在画面正中，
        #   把频谱切成上下两半，看着像渲染坏了（实测第一版就是这样）。
        # ★ cscheme 是 **6 个浮点**（不是 9 个），写成 9 个会报
        #   「Error initializing filter 'showcqt'」+ Invalid argument，
        #   报错完全不提参数个数。这里干脆不指定，用它的默认彩虹配色。
        "chain": ("showcqt=s={W}x{H}:fps={FPS}:count=6:axis=0:"
                  "sono_g=3:bar_g=2:sono_v=16:bar_v=12"),
        # ★ 实测（_probe_viz_fill.py 逐行数像素）：showcqt **只把内容画在上半 50%**，
        #   无论画幅是 1920x1080 / 1080x1920 / 1080x1080 都是 50.1%，
        #   下半是空出来的轴区（axis=0 / axis_h=0 都压不掉它）。
        #   所以按 2 倍高渲染、再把有内容的上半裁出来 —— 这样是**零缩放损失**的，
        #   比"裁完再拉伸"清楚得多（拉伸会把频谱条纹拉粗）。
        "render_ratio": 2.0,
        "desc": "横轴时间、纵轴频率，从上往下落，颜色越亮代表能量越强。适合流行、摇滚、国风。",
    },
    {
        "key": "bars", "name": "频谱柱", "tag": "均衡器样式",
        # ★ showfreqs **不吃 fps/rate**（查过 -h filter=showfreqs），靠链尾补
        "chain": "showfreqs=s={W}x{H}:mode=bar:ascale=log:fscale=log:cmode=separate",
        "desc": "一根根跳动的柱子，低音在左、高音在右。像音响上的均衡器，节奏感最直观。",
    },
    {
        "key": "wave", "name": "声波", "tag": "安静、干净",
        "chain": "showwaves=s={W}x{H}:mode=cline:rate={FPS}:colors=0x66ccff|0xff6688",
        "desc": "左右声道各一条波形线。适合说唱、念白、抒情慢歌，画面干净不抢戏。",
    },
    {
        "key": "scope", "name": "立体声相位", "tag": "炫技",
        "chain": "avectorscope=s={W}x{H}:mode=lissajous:rate={FPS}:draw=line:"
                 "scale=sqrt:zoom=1.3",
        "desc": "李萨如图形，左右声道互相画出来的形状。电子乐、舞曲最出效果。",
    },
    {
        "key": "stack", "name": "频谱＋声波", "tag": "信息量最大",
        # ★ 上半屏**不能用 showspectrum**：它是"从左往右滚动填充"的，
        #   视频开头那一两秒画面是空的（截 6 秒小样时尤其明显，看着像坏了）。
        #   showcqt 一上来就是满的，换掉。
        "chain": None,
        "stack": True,          # 这条链是两条支流拼的，得自己写 [0:a]
        "desc": "上半屏频谱、下半屏左右声道波形，一屏看全。",
    },
    {
        "key": "neon", "name": "霓虹脉冲", "tag": "适合短视频",
        # ★ 这个 build **没有 `eq` 滤镜**（`No such filter: 'eq'`）——
        #   `eq` 挺常用，但不能想当然。等价效果用 `vibrance` + `hue` 拼。
        "chain": ("showcqt=s={W}x{H}:fps={FPS}:count=4:axis=0:sono_v=15,"
                  "vibrance=intensity=0.7,hue=h=14*sin(2*PI*t/8)"),
        "render_ratio": 2.0,
        "desc": "频谱叠一层随时间转色的调色，颜色会缓慢流动。竖屏发短视频最抓眼。",
    },
]

SIZES = [
    {"key": "landscape", "name": "横屏 1080p", "w": 1920, "h": 1080,
     "desc": "B 站、YouTube"},
    {"key": "vertical", "name": "竖屏 1080×1920", "w": 1080, "h": 1920,
     "desc": "抖音、快手、视频号"},
    {"key": "square", "name": "方形 1080×1080", "w": 1080, "h": 1080,
     "desc": "朋友圈、小红书"},
]

FPS_CHOICES = [30, 60]


def style_by_key(key):
    for s in STYLES:
        if s["key"] == key:
            return s
    return None


def size_by_key(key):
    for s in SIZES:
        if s["key"] == key:
            return s
    return None


def media_info(ffprobe, path):
    """读一个媒体文件的真实参数（时长 / 有没有视频流 / 分辨率）。读不到返回 None。

    这个函数是用来在**渲染前后各量一次**的 —— 两处都是真踩过的坑：

    * 渲染**前**：判断 `start` 有没有超出音频长度。超出的话 ffmpeg **不报错**，
      照样给你一个几百字节的 mp4（实测 262 字节），任务显示"完成"、
      库里也照样列出来，用户点开却是一片漆黑 —— 比直接报错坑得多。
    * 渲染**后**：确认产物**真是一条视频**。老规矩：文件在 ≠ 能用。
    """
    try:
        p = subprocess.run([ffprobe, "-v", "error", "-print_format", "json",
                            "-show_streams", "-show_format", path],
                           capture_output=True, timeout=60)
    except Exception:                                        # noqa: BLE001
        return None
    if p.returncode != 0:
        return None
    try:
        d = json.loads((p.stdout or b"").decode("utf-8", "replace"))
    except ValueError:
        return None
    st = d.get("streams") or []
    v = [s for s in st if s.get("codec_type") == "video"]
    a = [s for s in st if s.get("codec_type") == "audio"]
    if not v and not a:
        return None
    try:
        dur = float((d.get("format") or {}).get("duration") or 0.0)
    except (TypeError, ValueError):
        dur = 0.0
    return {"video": bool(v), "audio": bool(a), "dur": dur,
            "w": int((v[0] if v else {}).get("width") or 0),
            "h": int((v[0] if v else {}).get("height") or 0)}


# ---------------------------------------------------------------- 编码器探测

_CACHE = {"at": 0.0, "enc": None}


def _run(cmd, timeout=90):
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return p.returncode, (p.stderr or b"").decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return -99, "超时"
    except Exception as e:                                   # noqa: BLE001
        return -98, "%s: %s" % (type(e).__name__, e)


def probe_encoder(ffmpeg, cache_dir=None):
    """挑一个**真能编出片**的 H.264 编码器。

    ★ 只看 `ffmpeg -encoders` 是不够的：`h264_nvenc` 在列表里好好的，
      真跑却报 `Cannot get the preset configuration: unsupported param (12)`
      （老 ffmpeg + 新驱动）。照列表选，结果就是**每一个用户都出不了片**。
      所以这里对每个候选真编 1 帧，编得出来才算数。

    结果缓存到 cache_dir（按 ffmpeg 的 mtime 失效），省得每次启动都探。
    """
    candidates = [
        ("h264_qsv", []),                     # Intel 核显，质量最好
        ("h264_nvenc", []),                   # N 卡（本机坏，但别的机器可能好）
        ("h264_amf", []),                     # A 卡
        ("h264_mf", ["-rate_control", "quality"]),   # Windows 内置，兜底
    ]
    try:
        key = os.path.getmtime(ffmpeg)
    except OSError:
        key = 0
    cpath = os.path.join(cache_dir, "viz_encoder.json") if cache_dir else None
    if cpath and os.path.exists(cpath):
        try:
            with open(cpath, encoding="utf-8") as f:
                c = json.load(f)
            if c.get("mtime") == key and c.get("enc"):
                return c["enc"], c.get("tested", [])
        except Exception:                                    # noqa: BLE001
            pass

    tmpdir = cache_dir or os.path.dirname(os.path.abspath(ffmpeg))
    tmp = os.path.join(tmpdir, "_enc_probe.mp4")
    tested, ok = [], None
    for enc, extra in candidates:
        rc, err = _run([ffmpeg, "-y", "-v", "error", "-f", "lavfi",
                        "-i", "color=c=black:s=320x240:r=30:d=1", "-frames:v", "1",
                        "-c:v", enc] + extra + [tmp])
        good = (rc == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0)
        tested.append({"enc": enc, "ok": good,
                       "why": "" if good else (err.strip().splitlines() or [""])[0][:160]})
        if good and ok is None:
            ok = enc
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except OSError:
            pass
    if cpath and ok:
        try:
            with open(cpath, "w", encoding="utf-8") as f:
                json.dump({"mtime": key, "enc": ok, "tested": tested}, f)
        except OSError:
            pass
    return ok, tested


# ---------------------------------------------------------------- 字幕

_ASS_HEAD = """[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Lrc,{FONT},{FS},&H00FFFFFF,&H000000FF,&H00101010,&H96000000,-1,0,0,0,100,100,0,0,1,{OL},1,2,{ML},{MR},{MV},1
Style: Title,{FONT},{TS},&H00FFFFFF,&H000000FF,&H00101010,&H96000000,-1,0,0,0,100,100,0,0,1,{OL},1,7,{ML},{MR},{TV},1
Style: Cnt,{FONT},{CS},&H00E8E8E8,&H000000FF,&H00101010,&H96000000,0,0,0,0,100,100,0,0,1,{OL},0,3,{ML},{MR},{CV},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# Windows 上一定有的中文字体。找不到就退回默认（ASS 会替我们选一个，
# 大不了字丑一点，但不会因为缺字体整条链失败）。
FONT_STACK = "Microsoft YaHei"


def ass_escape(s):
    r"""把一行歌词变成 ASS 里安全的文本。

    ASS 里 `{...}` 是**样式覆盖块**，歌词里真出现花括号必须转义，
    否则那一段会被 ffmpeg 当指令吞掉（用户看到歌词少了一截，还不知道为什么）。
    """
    s = str(s or "")
    s = s.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return s.replace("\r", "").replace("\n", "\\N")


def _ass_ts(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return "%d:%02d:%05.2f" % (h, m, s)


def build_ass(dst, lines, size, title="", subtitle="", offset=0.0, duration=None):
    """写一份 .ass 字幕：标题常驻 + 歌词按时间轴逐行。

    lines: [(开始秒, 文本), ...]（从 .lrc 解析出来的，时间轴来自识别结果）
    offset / duration: 只截取原曲的一段时，把时间轴平移并裁掉区间外的行。
    """
    w, h = size["w"], size["h"]
    # 字号按画幅高的比例走 —— 竖屏和横屏不能共用一套尺寸
    fs = max(18, int(h * 0.052))
    ts = max(16, int(h * 0.040))
    cs = max(14, int(h * 0.026))
    ml = max(16, int(w * 0.06))
    ol = max(2, int(fs * 0.09))
    out = [_ASS_HEAD.format(W=w, H=h, FONT=FONT_STACK, FS=fs, TS=ts, CS=cs,
                            OL=ol, ML=ml, MR=ml,
                            MV=max(24, int(h * 0.07)),      # 歌词离底边
                            TV=max(20, int(h * 0.045)),      # 标题
                            CV=max(14, int(h * 0.030)))]
    if title:
        out.append("Dialogue: 0,%s,%s,Title,,0,0,0,,%s\n"
                   % (_ass_ts(0), _ass_ts(duration if duration else 3600), ass_escape(title)))
    if subtitle:
        out.append("Dialogue: 0,%s,%s,Cnt,,0,0,0,,%s\n"
                   % (_ass_ts(0), _ass_ts(duration if duration else 3600), ass_escape(subtitle)))

    end_of = (offset + duration) if duration else None
    n = 0
    for i, (st, txt) in enumerate(lines):
        if not str(txt).strip():
            continue
        # 每行的结束 = 下一行的开始（这是 LRC 的惯例：只标开始不标结束）
        nxt = None
        for j in range(i + 1, len(lines)):
            if str(lines[j][1]).strip():
                nxt = lines[j][0]
                break
        en = nxt if (nxt is not None and nxt > st) else st + 4.0
        if en - st > 12.0:               # 长间奏别让一句挂太久
            en = st + 12.0
        if end_of is not None:
            st2, en2 = st - offset, en - offset
            if en2 <= 0 or st2 >= duration:   # 整行落在区间外
                continue
            st, en = max(0.0, st2), min(float(duration), en2)
        if en <= st:
            continue
        out.append("Dialogue: 0,%s,%s,Lrc,,0,0,0,,%s\n"
                   % (_ass_ts(st), _ass_ts(en), ass_escape(txt)))
        n += 1
    with open(dst, "w", encoding="utf-8-sig") as f:     # BOM：libass 对 utf-8 更稳
        f.write("".join(out))
    return n


_LRC_TS = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")


def parse_lrc(path):
    """读 .lrc 成 [(秒, 文本)]。一行多个时间戳的也拆开（LRC 允许）。

    读不了就返回空 —— 没有歌词只是少一层叠加，不该让整个渲染失败。
    """
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError:
        return []
    out = []
    for line in raw.splitlines():
        stamps = _LRC_TS.findall(line)
        if not stamps:
            continue
        txt = _LRC_TS.sub("", line).strip()
        if not txt or txt.startswith("["):        # [ar:] [ti:] 这类元信息
            continue
        for mm, ss in stamps:
            try:
                out.append((int(mm) * 60 + float(ss.replace(":", ".")), txt))
            except ValueError:
                continue
    out.sort(key=lambda x: x[0])
    return out


# ---------------------------------------------------------------- 滤镜链


def ff_path(p):
    r"""把本地路径转成 ffmpeg filter 参数里能用的形式。

    ★ 顺序是**先统一 `/`、再转义 `:`**。反过来（先转义冒号再换斜杠）
      会把刚加上的 `\:` 里的反斜杠也换成 `/`，等于白转义 ——
      症状是 `Unable to parse option value "D/AHC/..." as image size`
      （冒号被当参数分隔符，路径后半段被当成下一个参数了）。
    """
    return str(p).replace("\\", "/").replace(":", "\\:")


def finish_chain(chain, fps):
    """给滤镜链收口。

    ★ 这一步不是"锦上添花"，是**必须的**：
      · `showfreqs` 根本没有帧率参数；
      · `vstack` 拼完帧率会变得不确定；
      少了它，qsv 报 `Current frame rate is unsupported`、
      mf 报 `could not set output type (80004005)` —— 报错信息完全指不到真因。
    """
    return "%s,fps=%s,format=yuv420p" % (chain, fps)


def _even(n):
    """yuv420p 要求宽高都是偶数，奇数会直接编码失败。"""
    n = int(n)
    return n - (n % 2)


def build_render_args(ffmpeg, encoder, audio, dst, style, size, fps,
                      ass=None, duration=None, start=None,
                      title=None, video_bitrate="6000k", bg=None):
    """拼出完整的 ffmpeg 命令行。

    `duration=None` 表示整首；`start` 是起点秒数（截片段用）。
    ★ `-ss`/`-t` 都放在 `-i` **前面**：这样 ffmpeg 只解码需要的那一段，
      裁一小段时几乎是瞬时的；放在后面会先把整首解一遍再裁。
    `bg`：一张静态背景图。给了就把它当输入 0 循环铺底，滤波链的产物按
      **屏幕混合**叠上去 —— 这样画面不再是一个死黑的洞（用户的原话是"太难看"）。
      ★ 混合前两边都必须先 `format=gbrp`：直接在 yuv420p 上 blend，
        U/V 两个色度面也会被"滤色"，颜色会整个坏掉（实测：整帧变成洋红）。
    """
    w, h = size["w"], size["h"]
    ratio = style.get("render_ratio", 1.0)
    # showcqt 系：按 ratio 倍高渲染，再裁出有内容的那一块（见 STYLES 里的说明）
    rh = _even(h * ratio)
    crop = ",crop=%d:%d:0:0" % (w, h) if ratio > 1.0 else ""
    ain = "1:a" if bg else "0:a"

    # ★ `fc` 里**自己带输入标签**（`[1:a]…`）。外面不要再补一遍 ——
    #   补了就变成 `[1:a][1:a]showwaves`，报
    #   「Too many inputs specified for the "showwaves" filter」（实测踩过）。
    if style.get("stack"):
        top = _even(h * 0.66)
        bot = _even(h - top)
        top_r = _even(top * ratio)
        fc = ("[{A}]showcqt=s={W}x{TOPR}:fps={FPS}:count=4:axis=0:sono_v=15{CROP}[_t];"
              "[{A}]showwaves=s={W}x{BOT}:mode=cline:rate={FPS}[_b];"
              "[_t][_b]vstack=inputs=2").format(
                  A=ain, W=w, TOPR=top_r, BOT=bot, FPS=fps,
                  CROP=",crop=%d:%d:0:0" % (w, top) if ratio > 1.0 else "")
    else:
        fc = "[%s]" % ain + style["chain"].format(W=w, H=rh, FPS=fps) + crop

    # 进度条：一条随时间生长的细线，让观众知道还有多久。
    # 它接在 fc 的**最后一条链**上，所以放在这里是对的。
    bar = max(3, int(h * 0.006))
    fc += (",drawbox=x=0:y=ih-%d:w=iw*t/%s:h=%d:color=0xffffff@0.55:t=fill"
           % (bar, ("%.3f" % duration) if duration else "1e9", bar))

    if bg:
        # 两边都转 gbrp → 屏幕混合 → 回 yuv420p → 暗角 → 字幕
        vf = ("[0:v]format=gbrp[_bg];%s,format=gbrp[_vz];"
              "[_bg][_vz]blend=all_mode=screen,format=yuv420p,vignette=PI/4.6"
              ",fps=%s" % (fc, fps))
    else:
        vf = fc + ",fps=%s,format=yuv420p" % fps

    if ass:
        vf += ",subtitles='%s'" % ff_path(ass)
    # ★ 链尾必须显式打上 `[v]` 标签：`-filter_complex` 里没标签的输出
    #   用 `-map [v]` 是引用不到的，报错是
    #   「Output with label 'v' does not exist in any defined filter graph」。
    vf += "[v]"

    args = [ffmpeg, "-y", "-v", "error", "-nostdin"]
    if bg:
        args += ["-loop", "1", "-i", bg]
    if start:
        args += ["-ss", "%.3f" % start]
    if duration:
        args += ["-t", "%.3f" % duration]
    args += ["-i", audio, "-filter_complex", vf, "-map", "[v]", "-map", ain]
    if encoder == "h264_mf":
        args += ["-rate_control", "quality"]
    args += ["-c:v", encoder, "-b:v", video_bitrate,
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest",
             "-movflags", "+faststart", dst]
    return args


def render(ffmpeg, encoder, audio, dst, style, size, fps, ass=None, log=None,
           duration=None, start=None, timeout=7200, bg=None):
    """真跑一次渲染，返回 (成功?, 说明)。

    `log` 是调用方给的进度回调（收一行文本）。
    """
    def _lg(m):
        if log:
            log(m)
    args = build_render_args(ffmpeg, encoder, audio, dst, style, size, fps,
                             ass=ass, duration=duration, start=start, bg=bg)
    _lg("  正在渲染 %dx%d @%dfps，样式「%s」…"
        % (size["w"], size["h"], fps, style["name"]))
    t0 = time.time()
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "渲染超时（超过 %d 秒）" % timeout
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        # 报错只留最后一行经常指不到真因（ffmpeg 的真因常在前面几行），
        # 留三行：够定位，又不会把日志刷爆。
        tail = "\n".join(err.splitlines()[-3:]) if err else "原因不明"
        return False, "渲染失败：" + tail
    mb = os.path.getsize(dst) / 1048576.0
    _lg("  渲染完成：%.1f MB，用时 %.1f 秒" % (mb, time.time() - t0))
    return True, "%.1f MB" % mb


# ------------------------------------------------------------------ 配音 / 叠加
#
# ★ 老板的原话：「我应该在怎么把这个音频加入我的视频里呢 就算加进去是不是也
#   遮挡了本来的画面」—— 他问的是**另一件事**：他手里已经有一段视频，想把
#   唱好的歌配上去，而且不希望盖住原来的画面。
#
# 「做视频」原来那条路是"给音频造画面"—— 画面 100% 是生成的，它压根没有
#   "本来的画面"可以遮挡。所以对这个问题的正确答案不是调参数，是**补两条路**：
#
#     · mux_video   —— 画面一个像素都不动，只把音轨换成成品（秒出，绝不遮挡）；
#     · blend_video —— 画面还在，可视化的光按**屏幕混合**叠上去（黑的地方不动、
#                      亮的地方更亮），想要氛围又不想全挡住时用。
#
# 两条路都不用改 vizfx：blend 那条是「先照常渲一段可视化，再用 ffmpeg 混上去」，
# 渲染和混合是两次独立的 ffmpeg 调用，各自都好排错。


def _fit(tag, w, h, fps, pix):
    """把一路视频抻到目标画幅：等比缩放后居中补黑边。

    ★ 用 `force_original_aspect_ratio=decrease` + `pad`，**不要**直接 `scale=W:H`
      —— 那会把 16:9 硬拉成 9:16，画面里的人变成扁的。
    """
    return ("[{T}]scale={W}:{H}:force_original_aspect_ratio=decrease,"
            "pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black,fps={F},format={P}"
            ).format(T=tag, W=w, H=h, F=fps, P=pix)


def mux_video(ffmpeg, video, audio, dst, size, fps=30, ass=None,
              encoder="h264_qsv", duration=None, start=None,
              video_bitrate="6000k", loop=False, log=None, timeout=7200):
    """只换音轨：画面原样保留（裁到目标画幅），把成品的音配上去。

    这是「加进去了但不遮挡」最干净的一条路 —— 画面一个像素都不动。

    `loop`：原视频比歌短时循环播放，铺满整首。★ 别无条件开：
      `-stream_loop -1` 会让 ffmpeg 一直读输入，靠 `-shortest` 才收得住，
      视频本来就比歌长时开了纯属浪费解码。
    """
    def _lg(m):
        if log:
            log(m)
    w, h = size["w"], size["h"]
    args = [ffmpeg, "-y", "-v", "error", "-nostdin"]
    # ★ `-ss` 放在 audio 的 `-i` **前面**：它是输入选项，ffmpeg 只解码需要的那一段，
    #   截片段时几乎是瞬时的（当输出选项放到最后会先把整首解一遍再裁）。
    if start:
        args += ["-ss", "%.3f" % start]
    args += ["-i", audio]                               # 0：成品的声
    if loop:
        args += ["-stream_loop", "-1"]                  # ★ 必须紧贴它作用的那路 -i
    args += ["-i", video]                               # 1：老板自己的画面

    vf = _fit("1:v", w, h, fps, "yuv420p")
    if ass:
        vf += ",subtitles='%s'" % ff_path(ass)
    vf += "[v]"
    args += ["-filter_complex", vf, "-map", "[v]", "-map", "0:a"]
    # ★ `-t` 必须是**输出选项**：放在所有 -i 之后。
    #   写在第一个 `-i` 前面的话它只作用于那一路输入 —— 结果就是声音被裁到
    #   6 秒、画面却照原长输出（实测：12 秒的视频配 6 秒的歌，出来 359 帧
    #   而不是 180 帧，片尾多了 6 秒只有画面没有歌）。
    if duration:
        args += ["-t", "%.3f" % duration]
    if encoder == "h264_mf":
        args += ["-rate_control", "quality"]
    args += ["-c:v", encoder, "-b:v", video_bitrate, "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-shortest",
             "-movflags", "+faststart", dst]

    _lg("  正在把声音配到你的视频上（%dx%d @%dfps）…" % (w, h, fps))
    t0 = time.time()
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "配音超时（超过 %d 秒）" % timeout
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        tail = "\n".join(err.splitlines()[-3:]) if err else "原因不明"
        return False, "配音失败：" + tail
    mb = os.path.getsize(dst) / 1048576.0
    _lg("  配音完成：%.1f MB，用时 %.1f 秒" % (mb, time.time() - t0))
    return True, "%.1f MB" % mb


def blend_video(ffmpeg, viz_mp4, video, audio, dst, size, fps=30, ass=None,
                encoder="h264_qsv", duration=None, start=None,
                video_bitrate="6000k", loop=False, log=None, timeout=7200):
    """把渲好的可视化**叠**到老板自己的视频上，配成品的音。

    用**屏幕混合**（screen）：黑的叠上去还是黑的，只有亮的地方让画面变亮。
    原来画面里的内容因此全都还在 —— 这就是「加进去了但没挡住」。

    ★ 三个输入别记混：0=可视化（只要画面）, 1=老板的视频, 2=成品的声。
    ★ 混合前两边都要 `format=gbrp`。直接在 yuv420p 上 blend，
      U/V 色度面也会被一起「滤色」，整帧变洋红（这个坑踩过一次了）。
    ★ 字幕在**这一步**烧，不在渲染那步烧：叠完再烧字才清楚，
      先烧再叠的话字幕会被 screen 提亮成一团白。
    """
    def _lg(m):
        if log:
            log(m)
    w, h = size["w"], size["h"]
    args = [ffmpeg, "-y", "-v", "error", "-nostdin"]
    # ★ viz_mp4 是**专门为这次任务渲的**：它的起点和长度已经是对的，
    #   不要再给它 -ss/-t（裁两次会越裁越短）。
    args += ["-i", viz_mp4]                             # 0：可视化
    if loop:
        args += ["-stream_loop", "-1"]
    args += ["-i", video]                               # 1：老板的视频
    if start:
        args += ["-ss", "%.3f" % start]                 # 输入选项，只对 audio 生效
    args += ["-i", audio]                               # 2：成品的声

    fc = (_fit("1:v", w, h, fps, "gbrp") + "[_bg];"
          "[0:v]scale=%d:%d,fps=%d,format=gbrp[_vz];"
          "[_bg][_vz]blend=all_mode=screen,format=yuv420p" % (w, h, fps))
    if ass:
        fc += ",subtitles='%s'" % ff_path(ass)
    fc += "[v]"
    args += ["-filter_complex", fc, "-map", "[v]", "-map", "2:a"]
    # ★ 同上：`-t` 是输出选项。另外**必须**显式给 —— 光靠 `-shortest` 不行：
    #   blend 的两路视频输入里一路结束了，另一路会接着输出（实测输出的是
    #   video 的 12 秒而不是可视化的 6 秒）。
    if duration:
        args += ["-t", "%.3f" % duration]
    if encoder == "h264_mf":
        args += ["-rate_control", "quality"]
    args += ["-c:v", encoder, "-b:v", video_bitrate, "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-shortest",
             "-movflags", "+faststart", dst]

    _lg("  正在把可视化叠到你的视频上（%dx%d @%dfps）…" % (w, h, fps))
    t0 = time.time()
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "叠加超时（超过 %d 秒）" % timeout
    err = (p.stderr or b"").decode("utf-8", "replace").strip()
    if p.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) == 0:
        tail = "\n".join(err.splitlines()[-3:]) if err else "原因不明"
        return False, "叠加失败：" + tail
    mb = os.path.getsize(dst) / 1048576.0
    _lg("  叠加完成：%.1f MB，用时 %.1f 秒" % (mb, time.time() - t0))
    return True, "%.1f MB" % mb
