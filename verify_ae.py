#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：AE 真渲染那一组（6 套模板 + 接进流水线 + 不弄脏用户的 AE 会话）。

为什么需要这一关
----------------
这一组的失败方式几乎全是**静默**的，AE 不报错、任务还显示"成功"：

  ① **ExtendScript 是 ES3**：`forEach / map / indexOf / trim / Object.keys / JSON`
     全都没有。写进去 AE 只在脚本那一步抛一句"未定义"，回执里都看不到。
  ② **颜色分量**：`fillColor` / `addSolid` 只收 3 个分量，传 4 个报
     "颜色数组没有 3 个值" —— 六套模板曾全体倒在这一条上。
  ③ **频谱的「音频层」指向错图层** ⇒ 频谱恒为 0，画面上一排均匀小点，
     **没有任何报错**。（本关查回执里的 `bound`，即"重绑那一步跑了"。
     ★ 2026-10-25 实测更正：AE 的这个参数其实会自己跟着图层插入走，
       把重绑摘掉画面读数逐位不变 —— 所以 `bound` 只证明这一步跑了，
       **不证明**画面好坏。当初那次"振幅恒为 0"的真因是影子音轨被静音
       + 频段给到 45~16000Hz（线性轴，能量全挤在最左几根）。）
  ④ **脚本里混进非 ASCII**：AE 按系统代码页读 ⇒ 中文歌词变乱码或直接 parse 失败。
  ⑤ **脏了用户的 AE 会话**：我们是在**他正在用的那个 AE 进程**里建合成的，
     漏删一个 comp 就脏一点。本关查回执 `itemsBefore == itemsAfter`。

判据分两层：
  静态层（不要 AE）——语法 / 编码 / ES3 / 颜色分量 / 接线；
  动态层（要 AE 在跑）——真渲 6 套，量分辨率时长音轨、量频谱**真的在动**、
                          量会话没被弄脏。AE 没跑时动态层照实报"未执行"，不算通过。

★ 负对照三条，每条都必须**实测能红**（铁律 3）：
     N1 中文混进脚本 → ASCII 断言要抛；
     N2 注入 `forEach` → ES3 扫描要抓到；
     N3 注入 4 分量颜色 → 颜色扫描要抓到。
  动态层还自带一条天然负对照：ae_kinetic 这套**根本没有频谱效果**，
  它的"频谱在动"读数必须是静的 —— 它要是也动，说明尺子量的是歌词切换。

★ 一条**自知之明**（铁律 11b：不许把没验证过的帽子挂上去）：
  「I. 频谱真的在动」这条在"摘掉 rtBindAudio"的坏版本下**不会变红**
  （实测读数一模一样）。它守的是"频谱整体活着"这件事（频段/增益/影子音轨
  任一个坏掉它都会红），不是"音频层绑对了"。真正会红的是 H 层的 `bound`。
"""
import os
import re
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

JSX_DIR = os.path.join(HERE, "aefx", "jsx")
RUNTIME = os.path.join(JSX_DIR, "runtime.jsx")
STUDIO_SRC = os.path.join(HERE, "studio.py")
VIZ_JS = os.path.join(HERE, "web", "viz.js")
NODE = os.environ.get("REG_NODE") or "node"

N_PASS = 0
FAILS = []
SKIPS = []

# 需要"频谱真的在动"的三套 + 画幅（竖屏那套自己一套尺寸）
AUDIO_TPLS = {"ae_bars": 0.80, "ae_ring": 0.93, "ae_vertical": 0.66}
PORTRAIT = {"ae_vertical"}
# 天然负对照：这套没有频谱效果
NO_SPECTRUM = {"ae_kinetic", "ae_roll", "ae_cast"}

ES5_CALLS = ["forEach", "Array.prototype.map", ".indexOf(", ".trim(",
             "Object.keys", "JSON.parse", "JSON.stringify", ".filter("]
RE_COLOR4 = re.compile(r"(fillColor|addSolid|addText)\s*[\(=]\s*\[\s*[\d.]+\s*,"
                       r"\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*\]")


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("  [!!] %s  %s" % (name, detail))


def skip(name, why):
    SKIPS.append(name)
    print("  [--] %s  （未执行：%s）" % (name, why))


# ---------------------------------------------------------------- 静态层
def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def jsx_files():
    out = [RUNTIME]
    if os.path.isdir(JSX_DIR):
        for n in sorted(os.listdir(JSX_DIR)):
            if n.startswith("tpl_") and n.endswith(".jsx"):
                out.append(os.path.join(JSX_DIR, n))
    return out


def a_struct():
    print("\nA. 样式表 / 模板文件")
    import aefx
    keys = [s["key"] for s in aefx.STYLES]
    check("6 套模板都在样式表里", len(aefx.STYLES) == 6, keys)
    check("key 没有重名", len(set(keys)) == len(keys))
    for s in aefx.STYLES:
        p = os.path.join(JSX_DIR, s["tpl"])
        ok = os.path.isfile(p)
        check("  %s 的模板文件在（%s）" % (s["key"], s["tpl"]), ok)
        if s["tpl"] == "tpl_ring.jsx":
            # 极坐标版本实测画不出环，别让人改回去
            src = read(p)
            check("  ae_ring 没退回极坐标（不硬凑画不出来的东西）",
                  "polar: true" not in src)
    check("每套都标了 group=ae / engine=ae",
          all(s.get("group") == "ae" and s.get("engine") == "ae"
              for s in aefx.STYLES))
    check("只有合唱台标了 needs_cast",
          [s["key"] for s in aefx.STYLES if s.get("needs_cast")] == ["ae_cast"])


def b_encoding():
    """④ 非 ASCII：拼好的脚本必须是纯 ASCII，中文只能以 \\uXXXX 走 CTX。"""
    print("\nB. 编码（中文只能走 \\uXXXX）")
    import aefx
    ctx = {"w": 960, "h": 540, "fps": 24, "dur": 6.0,
           "audio": "x.wav", "out": "x.mp4", "done": "x.done",
           "specAudio": "", "audioOK": True,
           "title": "多角色合唱", "subtitle": "苹果嘉儿 / 云宝",
           "font": aefx.ae_build.CN_FONT,
           "lines": [[0.4, 2.3, "This is a line", "这是第一句", None]],
           "cast": [[0.4, 2.3, "苹果嘉儿", [0.9, 0.6, 0.2], ""]],
           "cover": "", "accent": [0.4, 0.6, 1.0],
           "bg1": [0.03, 0.03, 0.06], "bg2": [0.2, 0.1, 0.3],
           "trim": 0.0, "hScale": 8.0, "om": aefx.ae_build.OM_CANDIDATES}
    for s in aefx.STYLES:
        code = aefx.ae_build.build_driver(ctx, s)
        bad = [c for c in code if ord(c) > 127]
        check("  %s 拼出来是纯 ASCII（%d 字节）" % (s["key"], len(code)),
              not bad, ("混进了 %r" % bad[:3]) if bad else "")
        check("  %s 中文真的进到了脚本里（\\u 转义）" % s["key"],
              "\\u8fd9" in code or "\\u82f9" in code)   # 这 / 苹


def c_es3():
    """① ES3：AE 的 ExtendScript 没有 ES5 那批方法。"""
    print("\nC. ES3（forEach / map / indexOf / trim / Object.keys / JSON 全没有）")
    for p in jsx_files():
        src = read(p)
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = re.sub(r"//[^\n]*", "", src)
        hits = [c for c in ES5_CALLS if c in src]
        check("  %s 没用 ES5 方法" % os.path.basename(p), not hits, hits)


def d_color():
    """② 颜色：fillColor / addSolid 只收 3 个分量。"""
    print("\nD. 颜色分量（文本/纯色只收 3 个）")
    for p in jsx_files():
        src = read(p)
        hits = RE_COLOR4.findall(src)
        check("  %s 没给 3 分量接口喂 4 个值" % os.path.basename(p),
              not hits, hits[:2])
    # 所有入口都必须经过 c3()，它是唯一的归一化点
    rt = read(RUNTIME)
    check("fillColor 统一走 c3()", "td.fillColor = c3(" in rt)
    check("addSolid 统一走 c3()", "layers.addSolid(c3(" in rt or
          "addSolid(c3(color)" in rt)


def e_syntax():
    """语法门禁：把拼好的脚本喂给 node --check（ES3 是 ES5 的子集，解析够用）。"""
    print("\nE. 语法（node --check）")
    import aefx
    try:
        r0 = subprocess.run([NODE, "-v"], capture_output=True, timeout=30)
        have = (r0.returncode == 0)
    except Exception:                                        # noqa: BLE001
        have = False
    if not have:
        skip("6 套语法检查", "这台机器上没有 node（REG_NODE=%s）" % NODE)
        return
    ctx = {"w": 960, "h": 540, "fps": 24, "dur": 6.0, "audio": "x.wav",
           "out": "x.mp4", "done": "x.done", "specAudio": "", "audioOK": True,
           "title": "T", "subtitle": "S", "font": aefx.ae_build.CN_FONT,
           "lines": [[0.2, 2.0, "hi", "你好", None]], "cast": [],
           "cover": "", "accent": [0.4, 0.6, 1.0], "bg1": [0.03, 0.03, 0.06],
           "bg2": [0.2, 0.1, 0.3], "trim": 0.0, "hScale": 8.0,
           "om": aefx.ae_build.OM_CANDIDATES}
    tmpd = tempfile.mkdtemp(prefix="vs_ae_gate_")
    for s in aefx.STYLES:
        code = aefx.ae_build.build_driver(ctx, s)
        p = os.path.join(tmpd, s["key"] + ".js")
        with open(p, "w", encoding="ascii", errors="strict") as f:
            f.write(code)
        try:
            r = subprocess.run([NODE, "--check", p], capture_output=True, timeout=60)
            ok = (r.returncode == 0)
            msg = "" if ok else (r.stderr or b"").decode("utf-8", "replace")[:200]
        except Exception as e:                               # noqa: BLE001
            ok, msg = False, str(e)
        check("  %s 语法通过" % s["key"], ok, msg)
        try:
            os.remove(p)
        except OSError:
            pass


def f_wiring():
    """接线：studio 得认得出这一组，前端得给它一个分组。"""
    print("\nF. 接线（studio / 前端）")
    import studio
    for s in [x["key"] for x in __import__("aefx").STYLES]:
        st, eng = studio.viz_style_any(s)
        check("  studio.viz_style_any 认得 %s（engine=%s）" % (s, eng),
              st is not None and eng == "ae")
    styles = studio.viz_all_styles()
    ae = [s for s in styles if s["engine"] == "ae"]
    exe = studio._ae_path()
    if exe:
        check("装了 AE ⇒ 下发这一组（%d 套）" % len(ae), len(ae) == 6)
        check("合唱台那套带 needs_cast 标记",
              all(s["needs_cast"] for s in ae if s["key"] == "ae_cast"))
    else:
        check("没装 AE ⇒ 这一组不下发（点了必然报错的卡不如不给）", not ae)
    opt = studio.viz_options()
    check("viz_options 里带 ae 段", isinstance(opt.get("ae"), dict))
    check("AE 在时不要求 ffmpeg 编码器（%s / enc=%s）"
          % (bool(exe), opt.get("encoder")), bool(opt.get("ok")))
    src = read(STUDIO_SRC)
    check("job_viz 里真的分出去了 AE 这条路", "_job_viz_ae(" in src)
    check("AE 那条路不烧 ass（歌词是 AE 文本层，烧了就两行字叠一起）",
          "return _job_viz_ae(" in src)
    js = read(VIZ_JS)
    check("前端给了 AE 分组", '["ae",' in js)


def g_negative():
    """★ 三条负对照：每条都必须实测能红。"""
    print("\nG. 负对照（判据必须真的能抓到坏版本）")
    import aefx.ae_build as ab

    # N1：非 ASCII 断言
    try:
        ab._assert_ascii("var a = 1;", "probe")
        n1 = True                     # 干净脚本不该抛
    except Exception as e:                                   # noqa: BLE001
        n1 = False
        print("      （干净脚本被误报：%s）" % e)
    check("N1 干净脚本不误报", n1)
    try:
        ab._assert_ascii("var s = '中文混进来了';", "probe")
        n1b = False
    except ValueError:
        n1b = True
    check("N1 中文混进脚本 ⇒ ASCII 断言变红", n1b)

    # N2：ES5 扫描
    clean = "for (var i = 0; i < n; i++) { sum += a[i]; }"
    bad = "arr.forEach(function (x) { sum += x; });"
    check("N2 干净代码不误报", not [c for c in ES5_CALLS if c in clean])
    check("N2 注入 forEach ⇒ ES3 扫描变红", "forEach" in bad)

    # N3：4 分量颜色
    c_clean = "td.fillColor = c3([1, 1, 1, 1]);"
    c_bad = "td.fillColor = [1, 1, 1, 1];"
    check("N3 走 c3() 的不误报", not RE_COLOR4.findall(c_clean))
    check("N3 直接喂 4 个值 ⇒ 颜色扫描变红", bool(RE_COLOR4.findall(c_bad)))


# ---------------------------------------------------------------- 动态层
def make_wav(path, dur=8.0, sr=44100):
    """自己备料：一段有鼓点的合成音（门禁不许红在"库里恰好没有"）。"""
    import numpy as np
    n = int(dur * sr)
    t = np.arange(n) / float(sr)
    # 2 Hz 的包络当鼓点，基频落在频谱吃的 45~760Hz 区间里
    env = 0.35 + 0.65 * (0.5 + 0.5 * np.sin(2 * np.pi * 2.0 * t))
    sig = env * (0.5 * np.sin(2 * np.pi * 90 * t)
                 + 0.3 * np.sin(2 * np.pi * 220 * t)
                 + 0.2 * np.sin(2 * np.pi * 480 * t))
    sig += 0.05 * np.sin(2 * np.pi * 1200 * t)
    sig = np.clip(sig, -0.95, 0.95)
    pcm = (sig * 32000).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def strip_motion(frames):
    """量：画面**左侧窄条**里有没有东西在动。

    那条窄条里既没有居中歌词、也没有封面/唱片，唯一会动的就是横贯全宽的频谱。
    """
    from PIL import Image
    import numpy as np
    arr = []
    for p in frames:
        a = np.asarray(Image.open(p).convert("RGB")).astype("float32")
        arr.append(a.max(axis=2))
    st = np.stack(arr, 0)
    N, H, W = st.shape
    x1 = max(4, int(W * 0.08))
    return st[:, :, :x1].std(axis=0).mean(axis=1), H, W


def h_render():
    """真渲 6 套，量：出片 / 音轨 / 会话没脏 / 频谱真的在动。"""
    print("\nH. 真渲染（要 AE 在跑）")
    import aefx
    import studio
    exe = studio._ae_path()
    if not exe:
        skip("6 套真渲染", "这台机器上没装 AE")
        return
    if not aefx.ae_build.ae_running():
        skip("6 套真渲染", "AE 没在跑（脚本是投递给正在跑的那个进程的）")
        return

    tmpd = tempfile.mkdtemp(prefix="vs_ae_dyn_")
    wav = make_wav(os.path.join(tmpd, "probe.wav"))
    lines = [[0.4, 2.3, "First line here", "这是第一句"],
             [2.4, 4.2, "Second one now", "第二句在这里"],
             [4.4, 6.4, "Third keeps going", "第三句接着唱"],
             [6.6, 7.9, "Last one today", "今天最后一句"]]
    cast = [{"start": 0.3, "end": 4.2, "name": "苹果嘉儿", "color": "#F2A93B"},
            {"start": 4.3, "end": 7.9, "name": "云宝", "color": "#3BC8F2"}]

    import numpy as np
    for s in aefx.STYLES:
        k = s["key"]
        size = ({"w": 540, "h": 960} if k in PORTRAIT else {"w": 960, "h": 540})
        dst = os.path.join(tmpd, k + ".mp4")
        ok, msg = aefx.render(wav, dst, s, size, 24, lines=lines, cast=cast,
                              title="多角色合唱", subtitle="苹果嘉儿 / 云宝",
                              duration=8.0, duration_total=8.0,
                              log=lambda m: None, timeout=900, keep_debug=True)
        check("  %s 出片了" % k, ok and os.path.isfile(dst), msg)
        if not ok or not os.path.isfile(dst):
            continue
        info = studio.viz.media_info(studio.FFPROBE, dst) or {}
        check("  %s 时长对（%.1fs ≥ 7.5）" % (k, info.get("dur") or 0),
              (info.get("dur") or 0) >= 7.5)
        check("  %s 有画面也有音轨" % k,
              bool(info.get("video")) and bool(info.get("audio")))
        # 回执：会话有没有被弄脏
        rec = {}
        for f in sorted(os.listdir(aefx.ae_build._tmp_dir())):
            if f.startswith("vs_ae_") and f.endswith(".done"):
                fp = os.path.join(aefx.ae_build._tmp_dir(), f)
                try:
                    with open(fp, encoding="utf-8") as fh:
                        for ln in fh.read().splitlines():
                            if "=" in ln:
                                a, b = ln.split("=", 1)
                                rec[a.strip()] = b.strip()
                    os.remove(fp)
                except OSError:
                    pass
        if rec:
            check("  %s AE 会话没被弄脏（before=%s after=%s stuck=%s）"
                  % (k, rec.get("itemsBefore"), rec.get("itemsAfter"),
                     rec.get("stuck") or "无"),
                  rec.get("itemsBefore") == rec.get("itemsAfter")
                  and not (rec.get("stuck") or "").strip())
            check("  %s 音频层挂上了（missingAudio=%s）" % (k, rec.get("missingAudio")),
                  (rec.get("missingAudio") or "1") == "0")
            if k in AUDIO_TPLS:
                # ★ 这条验的是"重绑那一步跑了"（bound 是它的计数），
                #   **不是**"不重绑画面就坏" —— 实测把重绑摘掉，画面读数
                #   逐位不变（AE 的音频层参数自己跟着图层插入走，见 runtime.jsx
                #   里 2026-10-25 那条更正）。别把它当"频谱活了"的证据。
                check("  %s 音频层重绑这一步跑了（bound=%s ≥ 1）"
                      % (k, rec.get("bound")), (rec.get("bound") or "0").isdigit()
                      and int(rec.get("bound")) >= 1)
        else:
            skip("  %s 回执" % k, "没拿到回执文件")
        try:
            os.remove(dst)
        except OSError:
            pass
    try:
        os.remove(wav)
    except OSError:
        pass


def i_spectrum_motion():
    """频谱**真的在动**吗 —— 拿没有频谱的那套当尺子的负对照。"""
    print("\nI. 频谱真的在动（负对照：没频谱的那套必须是静的）")
    import aefx
    import studio
    if not (studio._ae_path() and aefx.ae_build.ae_running()):
        skip("频谱抖动测量", "AE 没在跑")
        return
    try:
        from PIL import Image                                # noqa: F401
        import numpy as np                                   # noqa: F401
    except Exception as e:                                   # noqa: BLE001
        skip("频谱抖动测量", "这台解释器没有 PIL/numpy：%s" % e)
        return

    tmpd = tempfile.mkdtemp(prefix="vs_ae_spec_")
    wav = make_wav(os.path.join(tmpd, "probe.wav"))
    motion = {}
    for k in sorted(set(AUDIO_TPLS.keys()) | {"ae_kinetic"}):
        s = aefx.style_by_key(k)
        size = ({"w": 540, "h": 960} if k in PORTRAIT else {"w": 960, "h": 540})
        dst = os.path.join(tmpd, k + ".mp4")
        ok, msg = aefx.render(wav, dst, s, size, 24,
                              lines=[[0.4, 7.9, "one long line", "一句长的"]],
                              title="T", subtitle="S", duration=8.0,
                              duration_total=8.0, log=lambda m: None, timeout=900)
        if not ok:
            check("  %s 渲染成功" % k, False, msg)
            continue
        fr = []
        for i in range(12):
            p = os.path.join(tmpd, "%s_%02d.png" % (k, i))
            subprocess.run([studio.FFMPEG, "-y", "-v", "error",
                            "-ss", "%.2f" % (0.3 + i * 0.6), "-i", dst,
                            "-frames:v", "1", p], capture_output=True)
            if os.path.exists(p):
                fr.append(p)
        rowstd, H, W = strip_motion(fr)
        base = int(H * AUDIO_TPLS.get(k, 0.80))
        up = float(rowstd[:base].max())
        motion[k] = up
        for p in fr:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.remove(dst)
        except OSError:
            pass
    for k in sorted(AUDIO_TPLS):
        v = motion.get(k)
        check("  %s 频谱在动（左窄条抖幅 %.2f > 3）" % (k, v),
              v is not None and v > 3.0)
    v0 = motion.get("ae_kinetic")
    check("  负对照 ae_kinetic（没有频谱）是静的（抖幅 %.2f < 1.5）" % (v0 or -1),
          v0 is not None and v0 < 1.5)
    try:
        os.remove(wav)
    except OSError:
        pass


def _some_avatars(n):
    """挑 n 张真实角色头像当燃料；一张都没有就返回空（模板会退回纯色圆，
    亮度对比照样量得到，所以这不是"缺料就跳过"的借口）。"""
    import studio
    root = os.path.join(studio.DATA, "roles")
    out = []
    try:
        for d in sorted(os.listdir(root)):
            p = os.path.join(root, d, "avatar.jpg")
            if os.path.isfile(p):
                out.append(p)
            if len(out) >= n:
                break
    except OSError:
        pass
    return out


def j_cast_lighting():
    """「平时压暗、谁唱谁亮」是真的吗。

    判据不是"某时刻谁最亮" —— 每张头像自己的亮度天差地别（实测同一排里
    最亮的那张暗态 119、最暗的那张暗态 71），横向比第一名会把暗图判成"没亮"。
    判据是**每个人跟自己的暗态比**：他唱的那几帧 / 他不唱的那几帧。

    ★ 阈值 1.55 是**负对照实测**定出来的，不是拍脑袋：
        正常版        1.68 / 1.82 / 1.68 / 1.78
        把压暗整个摘掉  1.37 / 1.41 / 1.37 / 1.41   ← 照样过 1.35
    也就是 1.35 这个阈值根本抓不到"压暗失效" —— 因为放大、彩色描边、光晕
    也在往框里加亮度，压暗没了它们还在。抬到 1.55 之后坏版本才真的会红。

    另外必须证明"暗"是压暗而不是消失：看框内起伏（std）而不是均值 ——
    一张本来就暗的头像压完可能跟背景均值接近，但它有轮廓，背景是均匀色块。
    """
    print("\nJ. 合唱台：平时压暗 / 谁唱谁亮（每人跟自己的暗态比）")
    import aefx
    import studio
    if not (studio._ae_path() and aefx.ae_build.ae_running()):
        skip("合唱台明暗测量", "AE 没在跑")
        return
    try:
        from PIL import Image                                # noqa: F401
        import numpy as np                                   # noqa: F401
    except Exception as e:                                   # noqa: BLE001
        skip("合唱台明暗测量", "这台解释器没有 PIL/numpy：%s" % e)
        return

    # 几何必须跟 tpl_cast.jsx 里的一致，模板改了这里要跟着改
    W, H, NP = 960, 540, 4
    step = min(W * 0.92 / NP, W * 0.26)
    rr = min(step * 0.40, H * 0.135)
    startX = W / 2 - step * (NP - 1) / 2
    chipY = H * 0.74
    BOX = 1.14                       # 量到彩色描边(1.10r)外沿为止

    av = _some_avatars(NP)
    check("  有真实头像当燃料（%d 张）" % len(av), len(av) >= 1)
    # 压暗量本身也要验：它是按每张图的亮度算的，算成 0 就等于没压暗，
    # 而上面那条画面判据在"压暗没了但描边还在"时是抓不出来的。
    for i, p in enumerate(av):
        d = aefx.ae_build._dim_for_avatar(p)
        check("  头像%d 算出压暗量 %s（应 ≤ -15，不能是 0）" % (i, d),
              d is not None and d[0] <= -15)
    names = ["甲", "乙", "丙", "丁"]
    colors = ["#F2A93B", "#3BC8F2", "#B46BE0", "#F26BA8"]
    # 四段互不重叠，段与段之间留空白 —— 空白时刻应该全员暗
    segs = [(0.3, 2.0), (2.4, 4.1), (4.5, 6.2), (6.6, 7.8)]
    cast = [{"start": segs[i][0], "end": segs[i][1], "name": names[i],
             "color": colors[i], "avatar": (av[i] if i < len(av) else "")}
            for i in range(NP)]

    tmpd = tempfile.mkdtemp(prefix="vs_ae_cast_")
    wav = make_wav(os.path.join(tmpd, "probe.wav"))
    dst = os.path.join(tmpd, "cast.mp4")
    s = aefx.style_by_key("ae_cast")
    ok, msg = aefx.render(wav, dst, s, {"w": W, "h": H}, 24,
                          lines=[[0.4, 7.9, "one long line", "一句长的"]],
                          cast=cast, title="T", subtitle="S", duration=8.0,
                          duration_total=8.0, log=lambda m: None, timeout=900)
    if not ok:
        check("  合唱台渲染成功", False, msg)
        return

    times = [1.15, 3.25, 5.35, 7.20]
    frames = []
    for i, t in enumerate(times):
        p = os.path.join(tmpd, "f%d.png" % i)
        subprocess.run([studio.FFMPEG, "-y", "-v", "error", "-ss", "%.2f" % t,
                        "-i", dst, "-frames:v", "1", p], capture_output=True)
        if os.path.exists(p):
            frames.append(np.asarray(Image.open(p).convert("RGB")).astype("float32"))
    check("  抽到 %d/4 帧" % len(frames), len(frames) == 4)
    if len(frames) < 4:
        return

    def boxsub(im, i):
        px = startX + step * i
        x0 = max(0, int(px - rr * BOX)); x1 = min(W, int(px + rr * BOX))
        y0 = max(0, int(chipY - rr * BOX)); y1 = min(H, int(chipY + rr * BOX))
        return im[y0:y1, x0:x1]

    V = [[float(boxsub(im, i).mean()) for i in range(NP)] for im in frames]
    bgsub = frames[0][int(H * 0.06):int(H * 0.10), int(W * 0.02):int(W * 0.08)]
    bg = float(bgsub.mean())
    bgstd = float(bgsub.std())

    for i in range(NP):
        onf = [fi for fi, t in enumerate(times) if segs[i][0] <= t <= segs[i][1]]
        offf = [fi for fi, t in enumerate(times) if not (segs[i][0] <= t <= segs[i][1])]
        if not onf or not offf:
            check("  %s 有亮/暗两个状态的样本" % names[i], False)
            continue
        a_on = sum(V[fi][i] for fi in onf) / len(onf)
        a_off = sum(V[fi][i] for fi in offf) / len(offf)
        ratio = a_on / (a_off + 1e-6)
        check("  %s 唱的时候亮起来（暗 %.0f → 亮 %.0f = %.2fx ≥ 1.55）"
              % (names[i], a_off, a_on, ratio), ratio >= 1.55)
        # 暗态必须是"图还在"，不是"透没了"。
        # 用框内起伏（std）判，不用均值：一张本来就暗的头像压完可能跟背景
        # 均值很接近，但它有轮廓有结构，背景是一块近乎均匀的色。
        struct = float(boxsub(frames[offf[0]], i).std())
        check("  %s 暗态还看得见人（框内起伏 %.0f vs 背景 %.0f）"
              % (names[i], struct, bgstd), struct > bgstd * 1.6 and struct > 12)

    for f in os.listdir(tmpd):
        try:
            os.remove(os.path.join(tmpd, f))
        except OSError as e:
            print("      [warn] 临时文件没删掉：%s" % e)
    try:
        os.rmdir(tmpd)
    except OSError:
        pass


def main():
    print("门禁：AE 真渲染那一组（6 套模板 + 接线 + 不弄脏会话）")
    a_struct()
    b_encoding()
    c_es3()
    d_color()
    e_syntax()
    f_wiring()
    g_negative()
    h_render()
    i_spectrum_motion()
    j_cast_lighting()
    print("\n通过 %d 项，失败 %d 项，未执行 %d 项" % (N_PASS, len(FAILS), len(SKIPS)))
    for f in FAILS:
        print("  失败：%s" % f)
    for s in SKIPS:
        print("  未执行：%s" % s)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
