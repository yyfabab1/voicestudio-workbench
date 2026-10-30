#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：合唱台四种构型都要「每个人都真的亮得起来」。

为什么需要这一关
----------------
这一族样式的地基是「**谁在唱谁亮**」，而且亮的必须和听到的是同一个人。
它最容易坏的两种方式都不报错：

  ① **数据没接上**：studio 那边写的是 `if style.get("key") == "cast"`，
     新加的 duet / ring / stage 永远拿不到分配表 ⇒ 画面一排空位，任务还显示"成功"。
  ② **有人被静默漏掉**：多角色分配那种"成品出来了，但第二个人 0 秒"的老坑
     （见项目铁律 22）。画面里少一个人，比报错难发现得多。

所以这一关不测"画面好不好看"，只测三件硬事实：
  A. 四个构型都在样式表里，且都标了 needs_cast / cast_lay
  B. studio 是**按标记**分派的，不是按 key 硬编码
  C. ★ 每个成员单独唱的那一帧，与"没人唱"那一帧**必须不一样**（他真的被画亮了）；
     且四种构型都要成立。这一条同时兜住 ①②：没接上数据 ⇒ 差分是 0；
     漏掉某个人 ⇒ 他那帧差分是 0。

★ 判据用"该成员唱 vs 没人唱"的**帧间差分**，不用绝对亮度：
  头像本身的颜色差异很大（浅蓝的角色天生比深棕的亮），直接比亮度会得出
  "一开始就亮的那位永远在唱"这种错误结论。
"""
import json
import os
import re
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
SRC = os.path.join(HERE, "vizfx.py")
STUDIO = os.path.join(HERE, "studio.py")
JS = os.path.join(HERE, "web", "viz.js")
NODE = os.environ.get("REG_NODE") or "node"

N_PASS = 0
FAILS = []
KINDS = ("row", "duet", "ring", "stage")
KEYS = {"row": "cast", "duet": "duet", "ring": "ring", "stage": "stage"}


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("  [!!] %s  %s" % (name, detail))


def fake_an(nframes=140, nb=56):
    rng = np.random.RandomState(4321)
    t = np.arange(nframes, dtype=np.float32)[:, None]
    spec = 0.40 + 0.34 * np.sin(t * 0.21 + np.arange(nb)[None, :] * 0.23)
    spec += 0.07 * rng.rand(nframes, nb)
    spec = np.clip(spec, 0.0, 1.0).astype(np.float32)
    peak = np.clip(spec + 0.05, 0.0, 1.0).astype(np.float32)
    bass = np.clip(spec[:, :6].mean(axis=1) * 1.5, 0.0, 1.0).astype(np.float32)
    wave = (0.5 * np.sin(np.arange(nframes)[:, None] * 0.5
                         + np.arange(192)[None, :] * 0.3)).astype(np.float32)
    return {"spec": spec, "peak": peak, "bass": bass, "mid": bass.copy(),
            "high": bass * 0.6, "wave": wave, "nb": nb}


NAMES = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
COLORS = ["#a06bff", "#5ad1ff", "#ff9f43", "#ff6ba8", "#7ee787", "#ffe066",
          "#6be0c8", "#ff8080"]


def make_cast(n, who=-1, span=20.0):
    """`who=-1` = 谁都没在唱（负对照 / 基线帧）。"""
    members = [{"name": NAMES[k % len(NAMES)], "avatar": "", "color": COLORS[k % len(COLORS)]}
               for k in range(n)]
    if who is None or who < 0:
        takes = []
    else:
        takes = [[0.0, span, int(who)]]
    lines = [{"start": 0.0, "end": span, "text": "hold that note",
              "zh": "守住这个音"}]
    return {"members": members, "takes": takes, "lines": lines,
            "title": "Gate", "song": "Gate"}


def render(kind, cast, an, at=90, size=(270, 480)):
    import vizfx
    st = dict(vizfx.style_by_key(KEYS[kind]))
    st["cast"] = cast
    sc = vizfx.Scene(size[0], size[1], st, an["nb"])
    sc.fps = 30
    sc.dur = an["spec"].shape[0] / 30.0
    sc.set_supersample(1)          # 这一关只判几何/接线，不判光滑度 ⇒ 用快的
    state = {"peak": True}
    img = None
    for i in range(max(0, at - 2), at + 1):
        img = st["draw"](sc, an, i, state)
    return np.asarray(img.convert("L"), dtype=np.float32)


def diff_energy(a, b):
    return float(np.abs(a - b).mean())


# ---------------------------------------------------------------- A 结构

def a_struct():
    print("\nA 结构（四种构型都在，且都声明了 needs_cast）")
    import vizfx
    keys = [s["key"] for s in vizfx.STYLES]
    for kind in KINDS:
        k = KEYS[kind]
        check("样式表里有 %s（%s）" % (k, kind), k in keys)
        st = vizfx.style_by_key(k)
        if not st:
            continue
        check("%-6s 标了 needs_cast" % k, st.get("needs_cast") is True)
        check("%-6s 的 cast_lay == %s" % (k, kind), st.get("cast_lay") == kind,
              str(st.get("cast_lay")))
        check("%-6s 有独立 draw 函数" % k, callable(st.get("draw")))
    check("四个构型的 draw 各不相同（不是同一个函数挂四个名）",
          len({id(vizfx.style_by_key(KEYS[k]).get("draw")) for k in KINDS}) == 4)


# ---------------------------------------------------------------- B 接线

def b_wiring():
    print("\nB 接线（studio 按标记分派，不是按 key 硬编码）")
    src = open(STUDIO, encoding="utf-8").read()
    check("studio 里有 needs_cast 判断", "needs_cast" in src)
    # 老写法必须绝迹：`== "cast"` 会让新构型永远拿不到分配表
    n_hard = src.count('style.get("key") == "cast"')
    check("★ 没有 `style.get(\"key\") == \"cast\"` 这种硬编码", n_hard == 0,
          "还有 %d 处" % n_hard)
    # 三处关键点都要在 needs_cast 分支里：装 cast、缺数据拦下、cast 样式不烧 ass
    check("装分配表那段在 needs_cast 分支里（≥2 处）",
          src.count("if style.get(\"needs_cast\")") >= 2,
          "有 %d 处" % src.count("if style.get(\"needs_cast\")"))
    check("缺分配表时会**拦下**而不是画一排空位",
          "没有「谁唱哪句」" in src or "多角色" in src)

    vsrc = open(SRC, encoding="utf-8").read()
    check("draw_cast 里有 cast_lay 分派", 'sc.style.get("cast_lay")' in vsrc)
    check("_draw_cast_variant 认得三个新构型",
          all(('if kind == "%s"' % k) in vsrc for k in ("duet", "ring", "stage")))


# ---------------------------------------------------------------- C 每个人都亮

def c_everyone():
    print("\nC 每个成员都能亮起来（该成员唱 vs 没人唱，画面必须不一样）")
    an = fake_an()
    for kind in KINDS:
        for n in (2, 4, 6):
            base = render(kind, make_cast(n, -1), an)
            m = make_cast(n)
            ens = [diff_energy(render(kind, make_cast(n, k), an), base)
                   for k in range(n)]
            lo = min(ens)
            check("%-5s %d 人：每个人都至少改变画面 %.2f（最小 %.2f）"
                  % (kind, n, lo, lo), lo > 0.35,
                  "各人 %.2f" % (sum(ens) / len(ens)) if ens else "无")

    # ★ 负对照：把 takes 清空，"第 k 人唱"那帧应该和基线一模一样。
    #   如果这一条不红，说明上面的差分根本不是由"谁在唱"驱动的
    #   （可能只是脉冲环/粒子的逐帧随机），那 C 段就全是假绿。
    print("\n  ★ 负对照：把分配表清空后，差分必须掉到 0 附近")
    an2 = fake_an()
    for kind in KINDS[:2]:
        base = render(kind, make_cast(4, -1), an2)
        # 同样的 members、同样的帧号，但 takes 为空 ⇒ 谁都不亮
        dup = render(kind, make_cast(4, -1), an2)
        check("%-5s 无分配表时两帧完全一致（差 %.4f）" % (kind, diff_energy(base, dup)),
              diff_energy(base, dup) < 0.02)


# ---------------------------------------------------------------- D 边界

def d_edges():
    print("\nD 边界（人很少 / 人很多 / 一个人都没有，都不许崩）")
    an = fake_an()
    for kind in KINDS:
        for n in (1, 3, 8, 9):
            try:
                img = render(kind, make_cast(n, 0), an)
                ok = img.shape == (480, 270)
                err = ""
            except Exception as e:                            # noqa: BLE001
                ok, err = False, "%s: %s" % (type(e).__name__, e)
            check("%-5s %d 人能画出来" % (kind, n), ok, err)
        # 空 members：不能抛异常（返回一张背景）
        try:
            st_cast = make_cast(0, -1)
            st_cast["members"] = []
            render(kind, st_cast, an)
            ok, err = True, ""
        except Exception as e:                                # noqa: BLE001
            ok, err = False, "%s: %s" % (type(e).__name__, e)
        check("%-5s 空 members 不崩" % kind, ok, err)


# ---------------------------------------------------------------- E 前端接线

def _js_code(js):
    """去掉注释后的代码 —— 判「有没有硬编码」不能把注释里那句反例算进去。"""
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", js)


def _js_func(js, name):
    """抓一个顶层 function 的源码（一直到行首那个 } 为止）。"""
    m = re.search(r"function\s+%s\s*\(.*?\n\}" % re.escape(name), js, flags=re.S)
    return m.group(0) if m else ""


def _run_node(body, tag):
    """把一段 JS 交给 node 真跑一遍，返回最后一行 JSON。"""
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".js", prefix="castlay_%s_" % tag)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        p = subprocess.run([NODE, path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode != 0:
            return None, (p.stderr or p.stdout or "").strip()[:200]
        return json.loads((p.stdout or "").strip().splitlines()[-1]), ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def e_frontend():
    print("\nE 前端接线（新构型在界面上不许静默少一份能力）")
    raw = open(JS, encoding="utf-8").read()
    code = _js_code(raw)

    # ① 字面量判定绝迹：写死 === "cast" 的话，每加一款构型就多一处静默失效
    n_hard = code.count('=== "cast"') + code.count('!== "cast"') + code.count('["cast"]')
    check("★ viz.js 里没有 `=== \"cast\"` 这类硬编码", n_hard == 0, "还有 %d 处" % n_hard)

    check("定义了 vizIsCast（按 needs_cast 标记判）", "function vizIsCast" in code)
    check("定义了 vizCastKeys（列合唱族）", "function vizCastKeys" in code)

    # ② 历史上「装了但从没走到」的 5 个能力点，逐个确认走的是标记判定
    for fn, why in (("vizCastOptShow", "合唱台开关面板显隐"),
                    ("vizSubsRefresh", "歌词/字幕表刷新"),
                    ("vizWhatLine", "「这次会做成什么样」"),
                    ("vizFromOutput", "多人成品自动切合唱族"),
                    ("vizGo", "提交时带 cast_opt")):
        body = _js_func(code, fn)
        hit = body and (("vizIsCast(" in body) or ("vizCastKeys(" in body))
        check("%-15s 走标记判定（%s）" % (fn, why), bool(hit),
              "" if hit else "函数体里没找到（或函数不存在）")

    # ③ 后端必须把标记下发下去 —— 前端判定再对，没字段也是空转
    try:
        import studio
        styles = studio.viz_all_styles()
    except Exception as e:                                    # noqa: BLE001
        check("能拿到后端样式表（viz_all_styles）", False, "%s: %s" % (type(e).__name__, e))
        return
    check("后端每条样式都带 needs_cast 字段", all("needs_cast" in s for s in styles))
    check("后端每条样式都带 cast_lay 字段", all("cast_lay" in s for s in styles))
    mp = {s["key"]: {"needs_cast": bool(s.get("needs_cast")),
                     "cast_lay": s.get("cast_lay") or ""} for s in styles}
    cast_keys = sorted(k for k, v in mp.items() if v["needs_cast"])
    other = sorted(k for k, v in mp.items() if not v["needs_cast"])
    check("后端下发的合唱族 ≥ 4 款", len(cast_keys) >= 4, str(cast_keys))

    # ④ ★ 真跑：抽出两个函数喂真样式表，在 node 里执行（只做文本匹配抓不到「函数写错了」）
    f1, f2 = _js_func(code, "vizIsCast"), _js_func(code, "vizCastKeys")
    if not (f1 and f2):
        check("抽出 vizIsCast / vizCastKeys 源码", False)
        return

    def probe(m):
        return ("let VIZ_STYLE = 'orbit';\n"
                "let VIZ_STYLE_MAP = %s;\n%s\n%s\n"
                "const out = {};\n"
                "for (const k of Object.keys(VIZ_STYLE_MAP)) out[k] = vizIsCast(k);\n"
                "console.log(JSON.stringify({each: out, keys: vizCastKeys().sort()}));\n"
                % (json.dumps(m, ensure_ascii=False), f1, f2))

    r, err = _run_node(probe(mp), "real")
    if r is None:
        check("node 真跑 vizIsCast", False, err)
        return
    bad = [k for k in cast_keys if not r["each"].get(k)]
    check("★ 合唱族每一款 vizIsCast 都为真", not bad, "漏了 %s" % bad)
    bad2 = [k for k in other if r["each"].get(k)]
    check("非合唱族 vizIsCast 都为假", not bad2, "误判 %s" % bad2)
    check("vizCastKeys() 正好等于后端合唱族", r["keys"] == cast_keys, str(r["keys"]))

    # ⑤ ★ 负对照：后端不下发 needs_cast 时，**不许**凭空把样式当合唱族
    #    （写成 `s.cast_lay || true` 这种"默认当真"，这里就会红）
    mp0 = {k: {"cast_lay": v["cast_lay"]} for k, v in mp.items()}
    r0, err0 = _run_node(probe(mp0), "neg")
    if r0 is None:
        check("负对照能跑（node）", False, err0)
    else:
        check("★ 负对照：后端不下发 needs_cast ⇒ 不再把任何样式当合唱族",
              r0["keys"] == [], str(r0["keys"]))

    # ⑥ 每一款构型的「这次会做成什么样」都要有自己那句
    wl = _js_func(code, "vizWhatLine")
    lays = sorted({v["cast_lay"] for v in mp.values() if v["needs_cast"]})
    miss = [l for l in lays if ("%s:" % l) not in wl]
    check("每一款 cast_lay 都有各自的说明文案", not miss, "缺 %s" % miss)


def render_rgb(kind, cast, an, singer=True, at=90, size=(270, 480)):
    """和 `render` 一样，但要**彩色**的 —— 判"名字用的是不是角色自己的色"非得有颜色。"""
    import vizfx
    st = dict(vizfx.style_by_key(KEYS[kind]))
    st["cast"] = cast
    st["cast_opt"] = {"singer": singer}
    sc = vizfx.Scene(size[0], size[1], st, an["nb"])
    sc.fps = 30
    sc.dur = an["spec"].shape[0] / 30.0
    sc.set_supersample(1)
    img = None
    for i in range(max(0, at - 2), at + 1):
        img = st["draw"](sc, an, i, {"peak": True})
    return np.asarray(img.convert("RGB"), dtype=np.int16)


def g_singer():
    print("\nG 歌词前面带演唱者名字（老板：谁唱的前面加名字）")
    src = open(SRC, encoding="utf-8").read()
    check("_draw_named 已定义", "def _draw_named(" in src)
    # 定义 1 处 + 两处调用（_cast_lyric 一份、row 老路自己一份）
    check("两个绘制入口都接了名字", src.count("_draw_named(") >= 3,
          "出现 %d 次" % src.count("_draw_named("))
    check("singer 是默认开的", '"singer": True' in src)

    an = fake_an()
    # ① 开关名字，画面必须真的变（四款构型各判一次）
    for kind in KINDS:
        on = render_rgb(kind, make_cast(4, 1), an, singer=True)
        off = render_rgb(kind, make_cast(4, 1), an, singer=False)
        d = float(np.abs(on.astype("float32") - off.astype("float32")).mean())
        check("%-5s 关掉名字画面确实变（差 %.3f）" % (kind, d), d > 0.05, d)

    # ② row 构型：名字必须落在**歌词那一带**，而且带的是角色自己的颜色
    # ★ `make_cast` 造的 lines 没有 role 字段 ⇒ 默认 0（第 1 个人）。
    #   这里要测"第 2 个人唱的这句"，就必须把 role 显式写进去；
    #   否则找的是 member[1] 的颜色，画的却是 member[0] 的，判据恒 0（踩过）。
    cast = make_cast(4, 1)
    cast["lines"] = [dict(x, role=1) for x in cast["lines"]]
    col = cast["members"][1]["color"].lstrip("#")
    rgb = tuple(int(col[i:i + 2], 16) for i in (0, 2, 4))
    on = render_rgb("row", cast, an, singer=True)
    off = render_rgb("row", cast, an, singer=False)
    H = on.shape[0]
    band = slice(int(H * 0.60), int(H * 0.80))     # 歌词那一带（头像在 0.345h）

    def near(img, c, tol=60):
        m = (np.abs(img[band, :, 0] - c[0]) < tol) & \
            (np.abs(img[band, :, 1] - c[1]) < tol) & \
            (np.abs(img[band, :, 2] - c[2]) < tol)
        return int(m.sum())

    n_on, n_off = near(on, rgb), near(off, rgb)
    check("名字画在歌词带里，用的是这个角色的颜色（开 %d / 关 %d 像素）"
          % (n_on, n_off), n_on > 40 and n_on > n_off * 3, "%d vs %d" % (n_on, n_off))

    # ③★ 负对照：换一个颜色完全不同的角色 → 名字的颜色必须跟着换。
    #   如果名字根本没画（或者画成了固定白色），这一条不会红 ⇒ 上面的 ② 是假绿。
    cast2 = make_cast(4, 1)
    cast2["lines"] = [dict(x, role=1) for x in cast2["lines"]]
    cast2["members"][1]["color"] = "#FF2D2D"
    on2 = render_rgb("row", cast2, an, singer=True)
    same_px = near(on2, rgb)                 # 旧色还剩多少
    new_rgb = (255, 0x2D, 0x2D)
    new_px = near(on2, new_rgb)
    check("★ 负对照：换角色颜色，名字颜色跟着换（旧色 %d → 新色 %d）"
          % (same_px, new_px), new_px > 40 and new_px > same_px)


# ---------------------------------------------------------------- H MV 排版

MV_GAP_K = 0.15        # 中文副行与大字之间至少留多少"字号"的空气


def _mv_frame(cast, an, text, zh, size, at=90):
    """渲一帧 mv（ss=1：这一节只判排版几何，不判光滑度）。"""
    import vizfx
    c = dict(cast)
    c["lines"] = [{"start": 0.0, "end": 20.0, "text": text, "zh": zh, "role": 0}]
    st = dict(vizfx.style_by_key("mv"))
    st["cast"] = c
    st["cast_opt"] = {"singer": True}
    sc = vizfx.Scene(size[0], size[1], st, an["nb"])
    sc.fps = 30
    sc.dur = an["spec"].shape[0] / 30.0
    sc.set_supersample(1)
    img = None
    for i in range(max(0, at - 2), at + 1):
        img = st["draw"](sc, an, i, {"peak": True})
    return np.asarray(img.convert("L"), dtype=np.float32)


def _yband(a, b, thr=10.0):
    """两帧差集占的 y 区间（含端点）。

    为什么用"两帧相减"而不是读绘制函数里的变量：变量是对的、画出来不对，
    正是这类排版 bug 的样子（行距拿图层高算，图层高一半是字体留白）。
    相减量的是**字真画在哪**，跟实现怎么算无关。
    """
    rows = np.where((np.abs(a - b) > thr).any(axis=1))[0]
    return (int(rows[0]), int(rows[-1])) if len(rows) else None


def h_mv_layout():
    """MV 样式：中文副行不许贴在大字的下伸笔画上 + 竖屏长句要折两行。

    ★ 口径是**间隙 / 字号**，不是"有没有重叠"：旧版实测间隙 0.04 字号
      （g / y 的尾巴几乎碰到汉字），严格说没叠上，但视觉上就是"压字"。
      门槛定 0.15 字号：实测旧公式横屏 0.08 / 竖屏 -0.57（竖屏是真叠字），
      新版横屏 0.33 / 竖屏 0.38 —— 两边都离门槛足够远，不会因字体微调翻面。
    ★ 负对照不靠"改坏源码再跑"：旧公式的坐标是**算得出来的**
      （中文中心 = base + 图层高 × 0.36），拿它跟实测的大字底一比就知道
      判据会不会红。省一次改坏-回滚，也避免"忘了回滚"。
    """
    print("\nH  MV 排版：中文副行留呼吸 + 竖屏长句折行")
    import vizfx
    an = fake_an()
    cast = make_cast(3, who=0)
    long_t = "Something ain't right"
    for tag, size in (("横屏", (480, 270)), ("竖屏", (270, 480))):
        portrait = size[1] > size[0]
        A = _mv_frame(cast, an, long_t, "有点不对劲", size)
        B = _mv_frame(cast, an, long_t, "", size)
        C = _mv_frame(cast, an, "", "", size)
        g, z = _yband(B, C), _yband(A, B)
        sz = int(min(size) * (0.098 if portrait else 0.125))
        check("%s：大字与中文副行都画出了像素（判据自检，否则下面的比较没意义）"
              % tag, g is not None and z is not None, "大字 %s / 中文 %s" % (g, z))
        if not (g and z):
            continue
        gap = z[0] - g[1] - 1
        check("%s：中文顶离大字墨迹底 %.0f px（%.2f 字号 ≥ %.2f）"
              % (tag, gap, gap / float(sz), MV_GAP_K), gap >= MV_GAP_K * sz,
              "大字底 y=%d / 中文顶 y=%d" % (g[1], z[0]))
        # 负对照：旧标题公式（中文中心 = base + 图层高 × 0.36）算出来是多少
        lay = vizfx._mv_text(long_t, sz, True, int(size[0] * 0.88))
        base = size[1] * (0.50 if portrait else 0.63)
        zh_half = (z[1] - z[0] + 1) / 2.0
        old_gap = (base + lay.size[1] * 0.36) - zh_half - g[1] - 1
        check("%s：负对照 —— 旧公式的间隙 %.0f px（%.2f 字号）落在门槛内侧，"
              "这条判据拿到旧版会**红**" % (tag, old_gap, old_gap / float(sz)),
              old_gap < MV_GAP_K * sz)

    # 竖屏长句必须折两行：一行塞进 88% 宽会被压小，还贴着左右边框
    sizeP, sizeL = (270, 480), (480, 270)
    sP, sL = int(min(sizeP) * 0.098), int(min(sizeL) * 0.125)
    CP = _mv_frame(cast, an, "", "", sizeP)
    CL = _mv_frame(cast, an, "", "", sizeL)
    hl_p = _yband(_mv_frame(cast, an, long_t, "", sizeP), CP)
    hs_p = _yband(_mv_frame(cast, an, "yes", "", sizeP), CP)
    hl_l = _yband(_mv_frame(cast, an, long_t, "", sizeL), CL)
    if hl_p and hs_p and hl_l:
        one = float(hs_p[1] - hs_p[0] + 1)
        r_p = (hl_p[1] - hl_p[0] + 1) / one
        r_l = ((hl_l[1] - hl_l[0] + 1) / float(sL)) / (one / float(sP))
        check("竖屏：同一条长句折成两行（块高 %.2f 倍单行 ≥ 1.40）" % r_p, r_p >= 1.40,
              "长句 %d px / 单行 %d px" % (hl_p[1] - hl_p[0] + 1, int(one)))
        check("负对照：同一条长句在横屏（不折）只有 %.2f 倍单行 ⇒ 这条判据量的确实是"
              "「折没折」，不是画幅差" % r_l, r_l < 1.40)
    else:
        check("竖屏折行：三条参照都量到了", False,
              "长句 %s / 单行 %s / 横屏 %s" % (hl_p, hs_p, hl_l))


# ---------------------------------------------------------------- I 干净版

def _mv_rgb(key, cast, an, size=(480, 270), at=90):
    """渲一帧**彩色**画面（判色差非要有颜色）。"""
    import vizfx
    st = dict(vizfx.style_by_key(key))
    st["cast"] = cast
    st["cast_opt"] = {"singer": True}
    sc = vizfx.Scene(size[0], size[1], st, an["nb"])
    sc.fps = 30
    sc.dur = an["spec"].shape[0] / 30.0
    sc.set_supersample(1)
    img = None
    for i in range(max(0, at - 2), at + 1):
        img = st["draw"](sc, an, i, {"peak": True})
    return np.asarray(img.convert("RGB"), dtype=np.int16)


def i_mvclean():
    """干净版：色差 / 噪点 / 扫描线必须是真的关掉了，不是"淡了一点"。

    ★ 这一节是被实战逼出来的：第一版干净样片里字边**还有红青**，因为
      `_mv_text` 里写的是 `o = max(2, int(size*dx_k))` —— dx_k=0 也至少偏 2px。
      肉眼看"好像干净了"，像素一数 173 个红点。所以判据必须数颜色，不能看感觉。
    """
    print("\nI  MV 干净版：色差与噪点是不是真的关掉了")
    import vizfx
    an = fake_an()
    cast = make_cast(3, who=0)
    keys = [s["key"] for s in vizfx.STYLES]
    check("样式表里有 mvclean", "mvclean" in keys, "前三项：%s" % (keys[:3],))
    a = _mv_rgb("mvclean", cast, an)
    b = _mv_rgb("mv", cast, an)

    def near(x, rgb, tol=26):
        return int((np.abs(x - np.array(rgb)).max(axis=2) <= tol).sum())

    cr, cc = near(a, (255, 48, 96)), near(a, (0, 226, 255))
    mr, mc = near(b, (255, 48, 96)), near(b, (0, 226, 255))
    check("干净版没有红/青色差（红 %d / 青 %d 像素，门槛 <5）" % (cr, cc),
          cr < 5 and cc < 5)
    check("负对照：片头版同口径有红/青（红 %d / 青 %d，门槛 >50）⇒ 判据分得开"
          % (mr, mc), mr > 50 and mc > 50)

    h = a.shape[0]
    band_a = a[int(0.34 * h):int(0.44 * h)]
    band_b = b[int(0.34 * h):int(0.44 * h)]
    check("干净版空白区是纯色（std %.2f < 1.5：没有噪点也没有扫描线）"
          % band_a.std(), band_a.std() < 1.5)
    check("负对照：片头版空白区 std %.2f > 2（噪点确实被这条量具抓到）"
          % band_b.std(), band_b.std() > 2.0)

    check("干净版画面不是全黑（均值 %.1f > 6）" % a.mean(), a.mean() > 6)
    white = near(a, (255, 255, 255), tol=18)
    check("干净版歌词是白字（近白像素 %d > 200）" % white, white > 200)


def main():
    print("门禁：合唱台四构型（每个人都亮得起来 + 数据真的接上了）")
    a_struct()
    b_wiring()
    c_everyone()
    d_edges()
    e_frontend()
    g_singer()
    h_mv_layout()
    i_mvclean()
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    for f in FAILS:
        print("  失败：%s" % f)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
