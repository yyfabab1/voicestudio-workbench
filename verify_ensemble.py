# -*- coding: utf-8 -*-
r"""门禁：合唱增强四件事 —— 独唱轨 / 8 人上限 / 角色头像 / 「谁唱谁亮」视频。

为什么单开一关：
    这四件事的共同点是"**看起来很直观，但错了也照样跑完**"：

      ① 独唱轨：多角色本来就是每个角色把整首唱一遍再拼 —— 那几条整首的轨
         一直躺在 final 目录里当中间产物。登记它只是"把你已经付过的算力
         还给你"，但漏了登记的话界面上一个独唱按钮都没有，**不报错**。
      ② 8 人上限：这个数字在前端写一个、后端写一个。两边漂了，症状是
         "选第 5 个人时突然报错"或者"界面允许、后端拒绝" —— 都能跑完，
         只是某一步莫名其妙。所以这里**比对两边是同一个数**。
      ③ 头像：上传收的是二进制。路由放错一侧（掉到 read_json_body 后面）
         会让请求当场挂死，而且日志里只有一句解码错 —— 所以这里查**行号顺序**。
      ④ 视频：头像"谁唱谁亮"是像素级的事。判据不能是"能出片"，
         必须是"该亮的那个人，区域亮度显著高于别人"，并且配负对照
         （把时间段改给另一个人，亮的必须跟着换）。

命名：本关只验"接线与几何"，不跑 RVC、不真唱。真实听感在 verify_multising。

退出码：0 全部通过；1 有失败。
"""
import io
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

N_PASS = 0
N_FAIL = 0
FAILS = []
QUIET = False          # 尺子自检时静音，别把日志刷成一片红


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        if not QUIET:
            print("  [ok] %s" % name)
    else:
        N_FAIL += 1
        FAILS.append((name, detail))
        print("! FAIL  %s  | %s" % (name, detail))


def rd(p):
    with io.open(os.path.join(HERE, p), encoding="utf-8", errors="replace") as f:
        return f.read()


# ---------------------------------------------------------------- A 人数上限

def A_cap(T):
    """人数上限：前后端必须是同一个数。

    ★ 这是本项目踩过的典型坑：两边各写一个 4，改一边就是"界面能选 5 个、
      后端拒收"或者"界面挡住第 5 个、后端其实支持"。
    """
    out = []
    m = re.search(r"^MULTI_MAX_ROLES\s*=\s*(\d+)", T["studio"], re.M)
    n_back = int(m.group(1)) if m else -1
    out.append(("后端人数上限 = 8", n_back == 8, "MULTI_MAX_ROLES = %s" % n_back))

    m2 = re.search(r"const STAGE_MAX_CASTS\s*=\s*(\d+)", T["app"])
    n_front = int(m2.group(1)) if m2 else -1
    out.append(("前端人数上限与后端一致", n_front == n_back,
                "前端 %s / 后端 %s" % (n_front, n_back)))

    # 大厅之外还有一处硬编码（doSingMulti 曾写死 4），三处必须都是同一个数
    bad = re.findall(r"picks\.length\s*>\s*(\d+)", T["app"])
    out.append(("没有别处硬编码人数", all(x == str(n_back) for x in bad) if bad
                else True, "发现硬编码：%s" % (bad or "无")))
    return out


# ---------------------------------------------------------------- B 每人独唱

def B_solo(T):
    out = []
    # 口径：必须真的把整首歌那条轨**写进 params**（"solo_tracks" 这四个字
    # 在 studio.py 里有四五处出现：赋值、注释、读取、回写）。
    # 只查"出现过"等于没查 —— 删掉登记行照样绿。所以钉死那一行的形状。
    out.append(("成品登记了每人的独唱轨",
                re.search(r'^\s+"solo_tracks":\s*solo_tracks,\s*$', T["studio"], re.M) is not None,
                "params 里没有 \"solo_tracks\": solo_tracks 这一行 —— 独唱轨没登记，界面上拉不出来"))
    out.append(("独唱轨带 dry 路径", re.search(r'"dry":\s*outs\[i\]', T["studio"]) is not None,
                "没有把第 i 条整首轨记进 dry"))
    out.append(("有按需混独唱的入口", "def multi_solo_mix(" in T["studio"], "缺 multi_solo_mix"))
    # 干声/伴奏丢了必须说人话（否则读起来像"角色坏了"）
    m = re.search(r"def multi_solo_mix\(.*?\n(?=\ndef |\Z)", T["studio"], re.S)
    body = m.group(0) if m else ""
    out.append(("独唱缺文件时说人话", "独唱轨文件不见了" in body and "重新跑" in body,
                "没找到针对性提示"))
    out.append(("同步混音可以不挂在任务里", "if job is None:" in T["studio"],
                "当前 _multi_mix 只能带 job 跑，界面上现混会没有入口"))
    out.append(("server 有 /api/multi/solo", '"/api/multi/solo"' in T["server"],
                "路由缺失"))
    out.append(("成品卡会画独唱按钮", "data-solo=" in T["app"] and "function soloBlock" in T["app"],
                "前端没有独唱块"))
    return out


# ---------------------------------------------------------------- C 角色头像

def C_avatar(T):
    out = []
    out.append(("能存头像", "def set_role_avatar(" in T["studio"], "缺 set_role_avatar"))
    out.append(("能撤头像", "def clear_role_avatar(" in T["studio"], "缺 clear_role_avatar"))
    out.append(("头像裁成方图", "AVATAR_PX" in T["studio"] and "crop(" in T["studio"],
                "没有居中裁方 —— 非方图会被拉变形"))

    # ★ 二进制路由必须在 read_json_body **之前**：放错一侧 = 请求静默挂死
    srv = T["server"]
    i_json = srv.find("body = read_json_body(self)")
    i_av = srv.find('path.endswith("/avatar")')
    out.append(("头像上传在 read_json_body 之前", 0 <= i_av < i_json and i_json > 0,
                "avatar 路由在 %d，read_json_body 在 %d" % (i_av, i_json)))

    # DELETE 的通配分支之前必须先把 /avatar 挑出来，否则 rid 变成 "rXXX/avatar"
    d = srv[srv.find("def _route_delete"):]
    d = d[:d.find("def ", 10) if d.find("def ", 10) > 0 else len(d)]
    i_avd = d.find('path.endswith("/avatar")')
    i_wild = d.find('if path.startswith("/api/role/"):')
    out.append(("撤头像排在角色通配之前", 0 <= i_avd < i_wild,
                "avatar=%d 通配=%d（顺序错了会去删一个不存在的角色）" % (i_avd, i_wild)))

    out.append(("角色卡走统一头像函数", "function avatarHtml" in T["app"]
                and T["app"].count("avatarHtml(") >= 2, "avatarHtml 没被复用"))
    out.append(("抽屉头像可点上传", 'id="fAvatar"' in T["html"] and "pickAvatar()" in T["html"],
                "index.html 里没有头像入口"))
    out.append(("头像有图才用图", "r.avatar" in T["app"], "没有读 r.avatar"))
    return out


# ---------------------------------------------------------------- D 合唱视频

def D_viz(T):
    out = []
    out.append(("vizfx 注册了 cast 样式", '"key": "cast"' in T["vizfx"],
                "样式表里没有 cast"))
    out.append(("cast 用 draw_cast 画", "draw_cast" in T["vizfx"], "缺 draw_cast"))
    out.append(("谁在唱是按分配表算的", "_cast_who_at" in T["vizfx"],
                "没有按 takes 求当前演唱者"))
    out.append(("段间空隙归上一段那个人", "prev[-1][2]" in T["vizfx"],
                "空隙里会全员暗掉，看起来像卡帧"))
    out.append(("studio 能把分配表变成视频数据", "def viz_cast_of(" in T["studio"],
                "缺 viz_cast_of"))
    # ★ 口径升级（2026-10-21）：拷贝现在挪到了 cast 分支**之前**（背景/合唱台选项
    #   任何样式都要用）。所以不能只查"分支里有 dict(style)"，要查"拷贝早于任何写入"。
    def _copy_first(s):
        """浅拷贝是否**早于**任何 cast 写入 —— 早于才不会污染全局 STYLES。"""
        i_cp = s.find('if style.get("needs_cast") or cast_opt or bg:')
        if i_cp < 0:
            return False
        return (s.find('style["cast_opt"] = dict(cast_opt)') > i_cp
                and s.find('style["cast"] = dict(cast, title=_t') > i_cp
                and "style = dict(style)" in s[i_cp:i_cp + 260])

    _i_cp = T["studio"].find('if style.get("needs_cast") or cast_opt or bg:')
    _i_w1 = T["studio"].find('style["cast_opt"] = dict(cast_opt)')
    _i_w2 = T["studio"].find('style["cast"] = dict(cast, title=_t')
    out.append(("样式是浅拷贝之后才写进去的（不污染全局 STYLES）",
                _copy_first(T["studio"]),
                "拷贝在写入之后 ⇒ 后面每次渲染都会带着上一份的头像/背景"))
    out.append(("cast 不再额外烧字幕", 'use_ass = None if style.get("needs_cast")' in T["studio"],
                "两套歌词会叠在一起"))
    # 负对照：把浅拷贝从条件里挪走，上面那条必须红（不然它只是个摆设）
    _reg = T["studio"].replace(
        'if style.get("needs_cast") or cast_opt or bg:\n        style = dict(style)',
        'if style.get("needs_cast") or cast_opt or bg:\n        pass', 1)
    out.append(("负对照：浅拷贝挪到写入之后那条会红（%s）"
                % ("红" if not _copy_first(_reg) else "没红 —— 尺子坏了"),
                _reg != T["studio"] and not _copy_first(_reg),
                "负对照没变红 ⇒ 上面那条判据是摆设"))
    out.append(("server 把 cast 传下去", "cast=cast" in T["server"], "job_viz 没收到 cast"))
    out.append(("多角色成品默认选 cast", 'vizPickStyle(pick)' in T["vizjs"]
                and 'keys.indexOf("cast") >= 0 ? "cast" : keys[0]' in T["vizjs"],
                "用户点对唱成品做视频，默认还是光谱仪"))
    return out


# ---------------------------------------------------------------- E 市场导入

def E_market(T):
    out = []
    out.append(("训练中间产物被挡住", "trash = re.compile" in T["mkt"],
                "D_/G_ 那两个训练 checkpoint 会被当成角色装进来"))
    out.append(("完整文件清单有专门入口", "def file_list(" in T["mkt"],
                "detail 的 files 只给 40 条，找索引会漏"))
    out.append(("权重落盘保留子目录", "rel = [p.strip() for p in pick.split" in T["studio"],
                "同名 pth 会互相顶掉（Scootaloo 两版会变成同一个模型）"))
    # 同样钉住「调用」而不是「定义」：def _fetch_index( 一直在，删掉调用照样命中。
    # 调用行是缩进的，定义行顶格 —— 用 ^\s+ 把两者分开。
    out.append(("索引会跟着权重一起下",
                re.search(r'^\s+_fetch_index\(job, repo, pick, dst\)\s*$', T["studio"], re.M) is not None,
                "权重下完没有去拿同目录的 .index —— 装完 index:false，检索降级成 0"))
    out.append(("指定一批文件的安装路径存在", "files_wanted" in T["studio"]
                and "files_wanted" in T["server"], "批量指定没接上"))
    out.append(("判断文件在不在仓库里用完整清单", "pack_set = set(packs)" in T["studio"],
                "用截断的 files 判断会把第 41 个之后的文件判成不存在"))
    return out


# ---------------------------------------------------------------- F 像素（真跑）

def F_pixels():
    """「谁唱谁亮」必须是像素事实，不是"能出片"。

    ★ 判据形式：在某人负责的时间段里，**他那块区域的亮度**必须显著高于
      别人的区域。配一条负对照：把整首改成另一个人唱，亮的必须跟着换 ——
      否则这个判据在"根本没实现高亮"的画面里也可能凑巧通过（比如全黑）。
    """
    out = []
    try:
        import numpy as np
        from PIL import Image

        import vizfx
    except Exception as e:                       # noqa: BLE001
        return [("像素判据可跑（依赖齐）", False, "%s: %s" % (type(e).__name__, e))]

    tmp = tempfile.mkdtemp(prefix="gat_cast_")
    avs = []
    for k, col in enumerate([(235, 70, 70), (70, 175, 235), (110, 225, 110)]):
        p = os.path.join(tmp, "a%d.png" % k)
        Image.new("RGB", (240, 160), col).save(p)
        avs.append(p)
    members = [{"name": "甲甲", "avatar": avs[0], "color": "#e05050"},
               {"name": "乙乙", "avatar": avs[1], "color": "#50b4e6"},
               {"name": "丙丙", "avatar": avs[2], "color": "#78dc78"}]
    takes = [[0.0, 1.0, 0], [1.0, 2.0, 1], [2.0, 3.0, 2]]
    base = dict(vizfx.style_by_key("cast"))
    base["cast"] = {"members": members, "takes": takes,
                    "lines": [{"start": 0.0, "end": 3.0, "text": "测试"}],
                    "title": "门禁"}

    sr = 16000
    mono = (np.sin(np.arange(sr * 3) * 2 * np.pi * 220.0 / sr) * 0.3).astype(np.float32)
    an = vizfx.analyze(np.stack([mono, mono], axis=1), 30, nb=56, sr=sr)

    W, H = 960, 540
    sc = vizfx.Scene(W, H, base, 56)
    sc.fps = 30
    n = len(members)
    r = max(18, int(min(sc.mind * 0.115, (W * 0.86) / max(1.0, n * 2 + (n - 1) * 0.38))))
    gap = r * 0.38
    total = n * 2 * r + (n - 1) * gap
    x0 = (W - total) / 2.0 + r
    cy = H * 0.38
    boxes = []
    for k in range(n):
        cx = x0 + k * (2 * r + gap)
        boxes.append((int(cx - r * 0.78), int(cy - r * 0.78),
                      int(cx + r * 0.78), int(cy + r * 0.78)))

    def lum(frame, box):
        return float(np.asarray(frame.crop(box).convert("L"), dtype=np.float32).mean())

    for i, who in ((10, 0), (45, 1), (80, 2)):
        f = vizfx.draw_cast(sc, an, i, {})
        v = [lum(f, b) for b in boxes]
        others = [v[k] for k in range(n) if k != who]
        out.append(("第 %d 秒亮的应该是第 %d 人" % (i // 30, who + 1),
                    v[who] > max(others) * 1.25,
                    "自己 %.1f / 别人最高 %.1f" % (v[who], max(others))))

    # 负对照：把第 3 段也交给第 1 个人 ⇒ i=80 时亮的必须是第 1 人
    bad = dict(base)
    bad["cast"] = dict(base["cast"], takes=[[0.0, 1.0, 0], [1.0, 2.0, 1], [2.0, 3.0, 0]])
    sc2 = vizfx.Scene(W, H, bad, 56)
    sc2.fps = 30
    f2 = vizfx.draw_cast(sc2, an, 80, {})
    v2 = [lum(f2, b) for b in boxes]
    out.append(("负对照：改了分配，亮的人跟着换",
                v2[0] > max(v2[1], v2[2]) * 1.25,
                "改后亮度 %s" % ["%.1f" % z for z in v2]))

    # ---- 双语字幕：英文大字在上、**中文小字在下面**（老板的原话）----
    # 口径：量**最下面那段墨的宽度**（不是"有没有字"）。
    #   · 有中文 ⇒ 最下面那段是中文：窄（本机实测 166px vs 英文整行 483px ≈ 0.34）；
    #   · 没有中文 ⇒ 最下面那段还是英文：宽度接近整行（实测 483/483 = 1.00）。
    # 这一对（0.34 vs 1.00）分得很开，所以"中文没画"和"中文和英文一样大"
    # 都会当场变红。第一版用固定 y 坐标去猜，量出来 31 vs 35 —— 猜坐标是在自欺。
    EN_TXT = "Followed my heart into"
    ZH_TXT = "跟随我的心进入"

    def cast_frame(zh):
        st = dict(base)
        st["cast"] = dict(base["cast"], lines=[
            {"start": 0.0, "end": 3.0, "text": EN_TXT, "zh": zh}])
        s = vizfx.Scene(W, H, st, 56)
        s.fps = 30
        return vizfx.draw_cast(s, an, 10, {})

    def tail_stat(frame, n=25):
        """返回 (墨行数, 最下面那段墨的宽度, 整块墨的宽度)。"""
        a = np.asarray(frame.convert("L"), dtype=np.float32)[
            int(H * 0.60):int(H * 0.93), int(W * 0.06):int(W * 0.94)]
        m = a > 200
        ys = [y for y in range(m.shape[0]) if m[y].any()]
        if not ys:
            return 0, 0, 0
        y0 = max(ys[0], ys[-1] - n + 1)
        cols = m[y0:ys[-1] + 1].any(axis=0)
        xs = np.where(cols)[0]
        axs = np.where(m.any(axis=0))[0]
        return (len(ys),
                int(xs[-1] - xs[0] + 1) if len(xs) else 0,
                int(axs[-1] - axs[0] + 1))

    n_z, tail_z, all_z = tail_stat(cast_frame(ZH_TXT))
    n_e, tail_e, all_e = tail_stat(cast_frame(""))
    out.append(("中文那一行真的画出来了（而且比英文窄）",
                n_z >= 8 and tail_z >= 40 and all_z > 0 and tail_z < all_z * 0.65,
                "最下段 %dpx / 整块 %dpx" % (tail_z, all_z)))
    out.append(("负对照：中文空着时最下面那段就是英文（不是窄行）",
                all_e > 0 and tail_e > all_e * 0.65,
                "最下段 %dpx / 整块 %dpx" % (tail_e, all_e)))
    return out


# ---------------------------------------------------------------- H 字幕（可改 + 中文）

def H_subs(T):
    """字幕这块的两件事：**用户能改字** + **英文下面有小的中文**。

    ★ 这里最容易"看起来做了其实没接上"的三处：
      ① 前端改了字，但提交时没带上去 ⇒ 画面上还是识别出来的那版；
      ② 存是存了，但渲染没读 ⇒ 改了等于没改；
      ③ 截片段做视频时字幕时间轴没平移 ⇒ 一句都不显示（本轮实测踩过）。
    """
    out = []
    S, V, X, A = T["studio"], T["vizfx"], T["vizjs"], T["app"]
    sub = T["subs"]

    # ---- 合并（纯函数，只盖字）
    out.append(("字幕合并是独立的纯函数", "def merge(" in sub,
                "vizsubs.merge 缺失"))
    out.append(("合并只盖 text / zh 两个字段",
                'for key in ("text", "zh"):' in sub,
                "合并范围写歪了 —— 连 start/end 一起改会把「谁唱谁亮」改乱"))
    out.append(("用户清空某句也算他的编辑（不许悄悄回落）",
                "if isinstance(v, str):" in sub,
                "写成 if v: 的话，用户清空后会被原来的词盖回来"))

    # ---- 翻译：两级 + 失败必须留痕
    out.append(("翻译有本地模型那一档", "def _by_llm(" in sub, "没有本地模型档"))
    out.append(("翻译有在线词典兜底", "def _by_dict(" in sub, "没有兜底档"))
    out.append(("先本地模型、后在线词典",
                sub.find("_by_llm(texts") < sub.find("_by_dict(texts")
                if ("_by_llm(texts" in sub and "_by_dict(texts" in sub) else False,
                "顺序反了（质量高的那档应该先试）"))
    out.append(("回来的行数必须和要翻的行数一致", "行数对不上" in sub,
                "不校验行数 ⇒ 少一行会让后面所有行整体错位"))
    out.append(("翻译失败会留痕（不留就是静默失败）",
                "def _say(" in sub and "_say(log, \"本地翻译" in sub,
                "HTTP 接口进来时 log 是 None，失败一个字都不留（本轮踩过）"))

    # ---- 后端接线
    # ★ 这里原来匹配的是字面量 `'"zh": ""}'` —— 那等于把判据钉在"变量名叫 zh、
    #   且写死空串"这个写法上。后来 zh 改成从双语缓存里取（`"zh": zh or ""`），
    #   功能一点没变，判据却红了 —— 是尺子绑了写法，不是回归。
    #   改成语义级：viz_cast_of 里必须给每一行塞 zh，并报出"整首有没有中文"。
    #   两条都仍是"少一个就红"（负对照见尺子自检）。
    _zc = S.find("def viz_cast_of")
    _zbody = S[_zc:S.find("\ndef ", _zc + 1)] if _zc >= 0 else ""
    out.append(("成品数据带中文那一栏",
                bool(re.search(r'"zh":\s*[^,\n}]+', _zbody))
                and '"has_zh":' in _zbody,
                "viz_cast_of 没有 zh / has_zh"))
    out.append(("改过的字幕会存进这条成品",
                'p["viz_lines"] = clean' in S, "viz_lines_save 没落到 params"))
    out.append(("存进去的字幕真的被读回来",
                "vizsubs.merge(lines, p.get(\"viz_lines\"))" in S,
                "存了不读 = 改了等于没改"))
    out.append(("翻完中文会存回去", "viz_lines_save(rid, index, rows)" in S,
                "translate_viz_lines 没落盘"))
    # 口径要按**形参名**查，不能查 "lines=None):" —— 那个字串只在 lines 是最后一个
    # 形参时才成立，后面加个 cast_opt / bg 就会误判成"没接上"（本轮踩过）。
    out.append(("做视频能收「界面上现改的字」", "cast=None, lines=None," in S,
                "job_viz 没有 lines 形参"))
    out.append(("截片段时字幕跟着平移",
                'dict(ln, start=float(ln.get("start") or 0) - off' in S,
                "不平移 ⇒ 截出来的片子一句词都不显示（本轮实测）"))
    out.append(("截片段时分配表也跟着平移",
                '[float(a) - off, float(b) - off, int(k)]' in S,
                "不平移 ⇒ 谁唱谁亮整段对不上"))

    # ---- 服务端路由
    for path, what in (("/api/viz/cast", "读字幕表"),
                       ("/api/viz/lines", "存字幕"),
                       ("/api/viz/translate", "翻中文")):
        out.append(("server 有 %s（%s）" % (path, what),
                    '"%s"' % path in T["server"], "路由缺失"))
    out.append(("做视频任务把现改的字幕传下去",
                'lines=body.get("lines") or None' in T["server"],
                "前端改了字但没传给任务 ⇒ 画面上还是老词"))

    # ---- 画法
    out.append(("合唱台读中文那一栏", 'ln = lines[cur]' in V
                and 'zh = (ln.get("zh") or "").strip()' in V,
                "draw_cast 没读 zh"))
    out.append(("中文画在英文下面（同一个 y 往下推）",
                "y_zh = y_en + sc.mind * 0.078 * 1.16" in V,
                "中文没跟着英文走，位置会漂"))
    out.append(("中文比英文小", 'sc.mind * 0.046, (236, 226, 255, 226)' in V,
                "中文和英文一样大就不是「小的中文」了"))

    # ---- 前端
    for fn, what in (("function vizSubsRefresh(", "读字幕表"),
                     ("function vizSubsCollect(", "收集表格里改过的字"),
                     ("function vizSubsSave(", "保存"),
                     ("function vizSubsTranslate(", "自动填中文")):
        out.append(("前端有 %s（%s）" % (fn.rstrip("( "), what), fn in X, "没接上"))
    out.append(("字幕表只在「合唱台」+ 多角色成品时出现",
                'if (!vizIsCast() || !src.startsWith(VIZ_OUT))' in X,
                "别的样式也给表 ⇒ 用户以为改了会生效"))
    out.append(("开始制作时把表格里的字一起提交",
                "body.lines = vizSubsCollect()" in X,
                "不带上就是「改了没生效」"))
    out.append(("页面上真有这张表（不是函数空跑）",
                'id="vizSubs"' in T["html"] and 'id="subsList"' in T["html"],
                "HTML 里没有容器"))
    out.append(("表里两格：英文一格、中文一格",
                'class="inp sen"' in X and 'class="inp szh"' in X,
                "只有一格的话就改不了中文"))

    # ---- 成品自带歌词（别只活在缓存里）
    #   实测教训：歌词缓存是按「路径+大小+mtime」命名的临时文件，被清过一次之后，
    #   4 条成品的字幕全变成空的（multi_lines 还写着 65 / 19），界面上只能让人
    #   重新认词。成品自己存一份，这条链路才不依赖一个随时会被清的文件。
    out.append(("分句压缩是独立的纯函数", "def _segs_brief(" in S, "没有 _segs_brief"))
    out.append(("首次合唱与换法重排都存了自带分句",
                S.count('"multi_segs": _segs_brief(segs),') == 2,
                "只在一处存 ⇒ 重排出来的那版又只有缓存里有词（数到 %d 处）"
                % S.count('"multi_segs": _segs_brief(segs),')))
    _i_seg = S.find('for r2 in (p.get("multi_segs")')
    _i_cache = S.find("segs, _dur = lyric_segments_any(src)")
    out.append(("读字幕时「成品自带的分句」排在「识别缓存」前面",
                _i_seg >= 0 and _i_cache >= 0 and _i_seg < _i_cache,
                "顺序反了 / 少了一档 ⇒ 缓存被清过就还是一行都没有"
                "（multi_segs@%d，缓存@%d）" % (_i_seg, _i_cache)))
    try:
        import studio as _st
        _b = _st._segs_brief([{"start": 1.23456, "text": " hi "},
                              {"start": 2, "text": "   "},
                              "junk", {"start": "x", "text": "no"}])
        out.append(("_segs_brief 只留「秒 + 词」，空句/坏行丢掉",
                    _b == [[1.235, "hi"]], repr(_b)))
    except Exception as e:                          # noqa: BLE001
        out.append(("_segs_brief 跑得起来", False, e))
    return out


# ------------------------------------------------- I 合唱台的画面选项（扫色/动效/竖屏/片头/背景）

def I_castopts(T):
    """合唱台的画面选项：扫色 / 头像动效 / 竖屏重排 / 排版 / 片头片尾 / 背景。

    ★ 这一节全部是**像素事实 + 负对照**：每一项都先量"开着"再量"关着"，
      两边必须差得开。只量"开着的时候有点黄"是没用的 —— 背景本来就偏紫。
    """
    out = []
    S, X = T["studio"], T["vizjs"]
    try:
        import numpy as np
        from PIL import Image

        import vizfx
    except Exception as e:                       # noqa: BLE001
        return [("画面选项的像素判据可跑（依赖齐）", False, "%s: %s" % (type(e).__name__, e))]

    tmp = tempfile.mkdtemp(prefix="gat_castopt_")
    avs = []
    for k, col in enumerate([(235, 70, 70), (70, 175, 235), (110, 225, 110),
                             (245, 205, 60), (180, 120, 240), (90, 220, 190),
                             (240, 140, 90), (150, 160, 240)]):
        p = os.path.join(tmp, "a%d.png" % k)
        Image.new("RGB", (200, 200), col).save(p)
        avs.append(p)
    names = ["甲", "乙", "丙", "丁", "戊", "己", "庚", "辛"]
    members = [{"name": names[k], "avatar": avs[k],
                "color": "#%02x%02x%02x" % c}
               for k, c in enumerate([(235, 70, 70), (70, 175, 235), (110, 225, 110),
                                      (245, 205, 60), (180, 120, 240), (90, 220, 190),
                                      (240, 140, 90), (150, 160, 240)])]
    takes = [[0.0, 1.5, 0], [1.5, 3.0, 1], [3.0, 4.5, 2], [4.5, 6.0, 3]]
    lines = [{"start": 0.0, "end": 3.0, "text": "Followed my heart into",
              "zh": "跟随我的心进入"},
             {"start": 3.0, "end": 6.0, "text": "And I never looked back", "zh": ""}]
    sr = 16000
    # ★ 测试音**必须有起伏**。恒定振幅的正弦，bass 是常数 ⇒ 动效振幅恒定 ⇒
    #   相邻两帧只差 0.34 像素，四舍五入后一像素都不变（实测帧间差 0.000）。
    #   那是「门禁自己的尺子太钝」，不是产品没动 —— 换一条会起伏的音再量。
    _t = np.arange(sr * 6) / float(sr)
    mono = (np.sin(_t * 2 * np.pi * 220.0)
            * (0.10 + 0.30 * np.abs(np.sin(_t * 2 * np.pi * 1.7)))).astype(np.float32)
    an = vizfx.analyze(np.stack([mono, mono], axis=1), 30, nb=56, sr=sr)

    def frame(size, i, opt=None, extra=None, mem=None, lns=None, tk=None):
        st = dict(vizfx.style_by_key("cast"))
        st["cast"] = {"members": mem or members, "takes": tk or takes,
                      "lines": lns or lines, "title": "测试歌名"}
        if opt is not None:
            st["cast_opt"] = opt
        if extra:
            st.update(extra)
        sc = vizfx.Scene(size[0], size[1], st, 56)
        sc.fps, sc.dur = 30, 6.0
        return vizfx.draw_cast(sc, an, i, {})

    def arr(f):
        return np.asarray(f.convert("L"), dtype=np.float32)

    def ink(f, y0, y1):
        a = arr(f)
        return int((a[int(a.shape[0] * y0):int(a.shape[0] * y1)] > 200).sum())

    LAND, TALL = (960, 540), (540, 960)

    # ---- ① 逐句扫色：同一句从头唱到尾，黄像素必须单调涨；关掉必须归零
    def yell(f):
        a = np.asarray(f.convert("RGB"), dtype=np.int16)
        return int(((a[:, :, 0] > 200) & (a[:, :, 1] > 165) & (a[:, :, 2] < 150)).sum())

    y0 = yell(frame(LAND, int(0.20 * 30)))
    y1 = yell(frame(LAND, int(1.50 * 30)))
    y2 = yell(frame(LAND, int(2.80 * 30)))
    out.append(("扫色：这句越唱到后面，黄字越多", y0 < y1 < y2 and y2 > y0 * 3,
                "0.2s=%d / 1.5s=%d / 2.8s=%d 黄像素" % (y0, y1, y2)))
    out.append(("负对照：关掉扫色后一个黄像素都没有",
                yell(frame(LAND, int(0.20 * 30), {"karaoke": False})) == 0,
                "仍然有 %d 个黄像素" % yell(frame(LAND, int(0.20 * 30), {"karaoke": False}))))

    # ---- ② 头像动效：隔几帧要"在动"；关掉必须几乎不动
    def jitter(i, motion):
        """隔 6 帧（0.2 秒）比一次。

        ★ 不能比相邻两帧：动效是 3.4 rad/s 的慢起伏，一帧只挪 0.34 像素，
          取整后完全一样 —— 这条判据就是因此假红过一次。
        """
        f1 = arr(frame(LAND, i, {"motion": motion}))
        f2 = arr(frame(LAND, i + 6, {"motion": motion}))
        return float(np.abs(f1 - f2).mean())

    j_on, j_off = jitter(20, True), jitter(20, False)
    out.append(("头像动效：隔 6 帧画面真的在变", j_on > 0.15, "帧间差 %.3f" % j_on))
    out.append(("负对照：关掉动效后只剩歌词在变（不到开着时的三分之一）",
                j_off < 0.10 and j_off < j_on * 0.35,
                "关掉 %.3f / 开着 %.3f" % (j_off, j_on)))

    # ---- ③ 竖屏重排：横屏一排（谁唱都同一个 y）；竖屏分列（换个人要换行）
    #   ★ 不能再用「最亮的那一行」猜是谁在唱：竖屏 8 人是 4 列 2 行，
    #     第 0 人和第 2 人恰好都在第一行，量出来 239 → 239 —— 判据自己量不出差别。
    #     改成**指定只有某一个人在唱**，直接量他那团亮块的中心行。
    def who_y(size, who):
        a = arr(frame(size, 30, None, None, None, None, [[0.0, 6.0, who]]))
        h = a.shape[0]
        y0, y1 = int(h * 0.06), int(h * 0.66)
        return y0 + int(a[y0:y1].mean(axis=1).argmax())

    ly0, ly5 = who_y(LAND, 0), who_y(LAND, 5)
    ty0, ty5 = who_y(TALL, 0), who_y(TALL, 5)
    out.append(("横屏还是一排（换人 y 不变）", abs(ly5 - ly0) <= 3,
                "%d → %d" % (ly0, ly5)))
    out.append(("竖屏把人排成多行（换人换行）", (ty5 - ty0) > TALL[1] * 0.10,
                "%d → %d（画面高 %d，差 %d）" % (ty0, ty5, TALL[1], ty5 - ty0)))

    # ★ 竖屏重排的目的就是「手机上脸大一圈」。有意义的不是像素直径，而是
    #   **直径占画面宽度的比例** —— 竖屏在手机上是按宽度铺满的，16:9 横屏
    #   铺满时只剩中间一条。所以量 r/w，不量 r（实测 0.087 vs 0.046，近 1.9 倍）。
    _rt = vizfx._cast_layout(540, 960, 8, 540)[1]
    _rl = vizfx._cast_layout(960, 540, 8, 540)[1]
    out.append(("负对照：竖屏的脸占画面宽度近一倍（手机上确实大一圈）",
                _rt > _rl and _rt / 540.0 > (_rl / 960.0) * 1.5,
                "竖屏 r=%d（%.3f 宽）/ 横屏 r=%d（%.3f 宽）"
                % (_rt, _rt / 540.0, _rl, _rl / 960.0)))

    # ---- ④ 片头 / 片尾：片头那段只出歌名、不出歌词；片尾要淡到黑
    op = {"intro": 1.5, "outro": 1.0}
    fi = frame(LAND, int(0.4 * 30), op)
    fm = frame(LAND, int(3.0 * 30), op)
    fe = frame(LAND, int(5.85 * 30), op)
    out.append(("片头只放歌名、不抢歌词（歌词区是空的）",
                ink(fi, 0.30, 0.55) > 1500 and ink(fi, 0.64, 0.92) == 0,
                "标题带 %d 墨 / 歌词带 %d 墨" % (ink(fi, 0.30, 0.55), ink(fi, 0.64, 0.92))))
    out.append(("负对照：不设片头时同一时刻就是正常画面（歌词在）",
                ink(frame(LAND, int(0.4 * 30), {"intro": 0, "outro": 0}), 0.64, 0.92) > 1500,
                "歌词带 %d 墨" % ink(frame(LAND, int(0.4 * 30), {"intro": 0, "outro": 0}),
                                   0.64, 0.92)))
    out.append(("片尾淡出到黑（亮度掉一半以上）",
                arr(fe).mean() < arr(fm).mean() * 0.5,
                "片尾 %.1f / 正片 %.1f" % (arr(fe).mean(), arr(fm).mean())))

    # ---- ⑤ 三套排版：墨落在不同的带里
    b_lo = ink(frame(LAND, 30, {"layout": "bottom"}), 0.62, 0.84)
    b_hi = ink(frame(LAND, 30, {"layout": "bottom"}), 0.40, 0.58)
    out.append(("排版「底部」：墨在下面，不在中间", b_lo > 3000 and b_hi < 400,
                "下带 %d / 中段带 %d" % (b_lo, b_hi)))
    mem8 = members[:8]
    c_a = ink(frame(TALL, 30, {"layout": "center"}, mem=mem8), 0.50, 0.62)
    c_b = ink(frame(TALL, 30, {"layout": "center"}, mem=mem8), 0.64, 0.80)
    out.append(("排版「画面中段」：墨真的上移了（比底部高）", c_a > 1500 and c_a > c_b,
                "中段带 %d / 下带 %d" % (c_a, c_b)))
    s_l = int((arr(frame(LAND, 30, {"layout": "side"}))[:, :480] > 200).sum())
    s_r = int((arr(frame(LAND, 30, {"layout": "side"}))[:, 480:] > 200).sum())
    out.append(("排版「靠左」：字压到左半边", s_l > s_r * 1.3, "左 %d / 右 %d" % (s_l, s_r)))

    # ---- ⑥ 排版不许压在头像上（纯函数不变量，最容易被改坏的一条）
    bad = []
    for _n in (3, 4, 8):
        for _sz in (LAND, TALL):
            _pos, _r, _bb = vizfx._cast_layout(_sz[0], _sz[1], _n, min(_sz))
            for _lay in ("bottom", "center", "side"):
                for _zh in (True, False):
                    _ye, _yp = vizfx._cast_lyric_ys(_bb, _sz[1], _lay, _zh)
                    if _yp < _bb - 0.5:
                        bad.append("%dx%d/%d人/%s/zh=%s: 上句 %.0f < 块底 %.0f"
                                   % (_sz[0], _sz[1], _n, _lay, _zh, _yp, _bb))
    out.append(("三套排版 × 两种画幅 × 有无中文：上一句都不压在头像块上",
                not bad, "；".join(bad[:3])))

    # ---- ⑦ 横屏几何没被这轮改动挪动（门禁 F 段是按这套坐标量"谁亮"的）
    _p, _r, _bb = vizfx._cast_layout(960, 540, 3, 540)
    out.append(("横屏 3 人的几何还是原来那套（r=62、居中在 0.38h）",
                _r == 62 and abs(_p[1][1] - 540 * 0.38) < 0.6,
                "r=%d，第 2 人 cy=%.1f" % (_r, _p[1][1])))

    # ---- ⑧ 背景：纯色 / 我的图片
    fg = np.asarray(frame(LAND, 30, None, {"bg_color": (12, 40, 90)}).convert("RGB"))
    corner = fg[6, 6].tolist()
    out.append(("纯色背景：角落真的是那个蓝（蓝远大于红）",
                corner[2] > corner[0] * 3 and corner[2] > 50, "角落 RGB=%s" % corner))
    bgp = os.path.join(tmp, "bg.png")
    Image.new("RGB", (64, 64), (200, 40, 40)).save(bgp)
    fq = np.asarray(frame(LAND, 30, None, {"bg_image": bgp}).convert("RGB"))
    cq = fq[6, 6].tolist()
    out.append(("图片背景：角落变成图上的红（红远大于蓝）",
                cq[0] > cq[2] * 2 and cq[0] > 40, "角落 RGB=%s" % cq))

    # ---- ⑨ 接线：界面上的开关真的接到了渲染里
    out.append(("默认就开着扫色和动效（用户不用自己去翻开关）",
                "def cast_opt_of(" in T["vizfx"]
                and '"karaoke": True' in T["vizfx"] and '"motion": True' in T["vizfx"],
                "CAST_OPT_DEFAULT 里没开"))
    out.append(("做视频的函数收 cast_opt / bg 两个参数", "cast_opt=None, bg=None" in S,
                "job_viz 没这两个形参"))
    out.append(("写了选项才浅拷贝样式（不污染别的渲染）",
                S.find('if style.get("needs_cast") or cast_opt or bg:')
                < S.find('style["cast_opt"] = dict(cast_opt)'),
                "拷贝在写入之后 ⇒ 会把这一份的背景带到下一次渲染"))
    out.append(("背景图只认自己目录里的（不是随便给个路径就读）",
                "def _under(" in S and "_under(p, [UPLOADS, DATA, VIZ_DIR])" in S,
                "少了路径准入"))
    out.append(("服务端把 cast_opt / bg 传下去了",
                'cast_opt=body.get("cast_opt")' in T["server"]
                and 'bg=body.get("bg")' in T["server"], "路由没接"))
    for fn, what in (("function vizBgChange(", "选背景"),
                     ("function vizCastOptShow(", "显隐合唱台开关"),
                     ("function vizWhatLine(", "把选项翻成人话"),
                     ("function vizRedo(", "重出视频")):
        out.append(("前端有 %s（%s）" % (fn.rstrip("( "), what), fn in X, "没接上"))
    out.append(("提交时把选项一起发出去",
                "body.cast_opt = {" in X and "body.bg = {" in X, "前端没带上"))
    out.append(("「重出视频」用的是表单里现在的字幕（不是旧 body）",
                "vizGo();" in X[X.find("function vizRedo("):X.find("async function vizListLoad(")],
                "重发旧 body 会把刚改的字丢掉"))
    need = ['id="vizCastOpt"', 'id="vizKaraoke"', 'id="vizMotion"', 'id="vizLayout"',
            'id="vizIntro"', 'id="vizOutro"', 'id="vizBg"', 'id="btnSubsRedo"']
    miss = [k for k in need if k not in T["html"]]
    out.append(("页面上真有这些控件（不是函数空跑）", not miss, "缺 %s" % miss))
    return out


# -------------------------------------------- J 谁唱谁亮：**以音频为准**
def J_who(T):
    """「谁在唱」这条数据不许再信记录，要拿波形去对。

    ★ 老板报的正是「亮的人和唱的人对不上」。记录（multi_takes）是拼接时
      顺手写下的，任何"改了分配没重拼 / 重排留旧表"都会让它和成品脱节，
      而那种片子看着处处对、就是听着错。波形是成品自己身上的东西：
      成品就是从某人独唱轨原样剪出来的，采样级一模一样。
    ★ 这一节全部用**合成素材**自足验证：三条独唱轨 + 一张已知的表，
      拼出成品再让扫描去还原。不依赖用户库里恰好有哪条成品。
    """
    out = []
    S, X = T["studio"], T["vizjs"]
    try:
        import numpy as np
        import wave
        import studio
        import vizfx
    except Exception as e:                       # noqa: BLE001
        return [("逐窗认人这一节能跑（依赖齐）", False,
                 "%s: %s" % (type(e).__name__, e))]

    tmp = tempfile.mkdtemp(prefix="gat_who_")
    sr = 16000
    tt = np.arange(int(sr * 6)) / float(sr)
    # 三个人：音高不同 ⇒ 波形完全不像（这是"可分"的前提，实测差距 0.74）
    refs = [(np.sin(2 * np.pi * f * tt) * 0.30
             + np.sin(2 * np.pi * f * 2 * tt) * 0.10).astype(np.float32)
            for f in (220.0, 311.0, 440.0)]
    real = [[0.0, 2.0, 0], [2.0, 4.0, 1], [4.0, 6.0, 2]]
    mix = np.zeros(int(sr * 6), dtype=np.float32)
    for a, b, k in real:
        mix[int(a * sr):int(b * sr)] = refs[k][int(a * sr):int(b * sr)]

    def _wav(p, arr):
        a = (np.clip(arr, -1.0, 1.0) * 32000).astype("<i2")
        with wave.open(p, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(sr)
            f.writeframes(a.tobytes())

    mixp = os.path.join(tmp, "mix.wav")
    _wav(mixp, mix)
    refp = []
    for j, r in enumerate(refs):
        p = os.path.join(tmp, "r%d.wav" % j)
        _wav(p, r)
        refp.append(p)

    who = studio.multi_who_scan(mixp, refp)

    # ---- ① 端到端：能从音频里还原出那张表（段对、人也要对）
    bad = []
    if not who:
        bad.append("一个段都没算出来")
    else:
        for a, b, k in real:
            mid = (a + b) / 2.0
            got = vizfx._cast_who_at(who, mid)
            if got != k:
                bad.append("%.1fs 该是 %d，算成 %d" % (mid, k, got))
    out.append(("逐窗认人：能从成品里还原出「谁在唱」（三个时刻段段对）",
                not bad, "；".join(bad[:3])))

    # ---- ② 负对照：把三个人的轨换顺序 ⇒ 认出来的必须跟着变
    #   （不然就是"反正都判第一个人"那种蒙的）
    who2 = studio.multi_who_scan(mixp, [refp[1], refp[0], refp[2]])
    got0 = vizfx._cast_who_at(who2 or [], 1.0)
    got1 = vizfx._cast_who_at(who2 or [], 3.0)
    out.append(("负对照：把三个人的轨换顺序，认出来的人跟着换（不是蒙的）",
                got0 == 1 and got1 == 0,
                "换序后 1.0s 判 %d（期望1）/ 3.0s 判 %d（期望0）" % (got0, got1)))

    # ---- ③ 段表必须规整（重叠 ⇒ 换人慢半秒，实测过）
    messy = [[0.0, 5.5, 0], [5.0, 11.5, 1], [10.5, 13.5, 2]]
    tidy = studio._who_tidy(messy)
    overlap = [(tidy[i][1], tidy[i + 1][0]) for i in range(len(tidy) - 1)
               if tidy[i][1] > tidy[i + 1][0]]
    out.append(("规整之后相邻段不再重叠", not overlap, "还有重叠 %s" % overlap[:2]))
    out.append(("负对照：不规整的话 11.0 秒会亮错人（这就是「换人慢半秒」）",
                vizfx._cast_who_at(tidy, 11.0) == 2
                and vizfx._cast_who_at(messy, 11.0) == 1,
                "规整后判 %d（期望2）/ 不规整判 %d（期望1，即错的那半秒）"
                % (vizfx._cast_who_at(tidy, 11.0),
                   vizfx._cast_who_at(messy, 11.0))))

    # ---- ④ 脱节能被量出来（不然"记录错了"永远没人知道）
    same = studio._who_disagree([[0.0, 2.0, 0], [2.0, 4.0, 1]],
                                [[0.0, 2.0, 0], [2.0, 4.0, 1]])[0]
    off = studio._who_disagree([[0.0, 2.0, 0], [2.0, 4.0, 1]],
                               [[0.0, 2.0, 1], [2.0, 4.0, 0]])[0]
    out.append(("记录与音频脱节能被量出来（一致 0 / 错位 100%）",
                same == 0.0 and off > 0.9, "一致 %.2f / 错位 %.2f" % (same, off)))

    # ---- ⑤ 接线与降级
    out.append(("逐窗认人是独立的纯函数",
                "def multi_who_scan(" in S and "def _who_tidy(" in S,
                "没有 multi_who_scan / _who_tidy"))
    out.append(("取合唱数据时**先拿音频算的、记录只当兜底**",
                "who = multi_who_of(rid, idx)" in S
                and 'takes, who_src = (who, "wave") if who else (_rec,' in S,
                "记录排在了音频前面 ⇒ 又回到「信记录」那条老路上"))
    out.append(("算出来的表存进成品（下次不用再算一遍）",
                '"multi_who"] = who' in S, "没有落盘"))
    out.append(("★ 没有 numpy 时逐窗认人自动让位（studio 不能被它拖死）",
                "if np is None:" in S and "import numpy as _n" in S,
                "numpy 是硬依赖 ⇒ 精简解释器下 import studio 会直接炸"))
    out.append(("界面能说清「这是按音频还是按记录亮的」",
                "function vizWhoNote(" in X and 'id="whoNote"' in T["html"],
                "前端没接上"))
    return out


# ---------------------------------------------------------------- 尺子自检

def selftest(T):
    """把关键接线改坏一次，确认判据真的会红。

    ★ 为什么必须有：这套判据全是"字串在不在"，写歪了（比如查的词本身
      就出现在注释里）会永远为真 —— 那种绿比红更害人。
    """
    global QUIET, N_PASS, N_FAIL
    before_p, before_f = N_PASS, N_FAIL
    cases = [
        ("人数写回 4", {"studio": T["studio"].replace("MULTI_MAX_ROLES = 8",
                                                      "MULTI_MAX_ROLES = 4")}, A_cap),
        ("独唱轨不登记", {"studio": T["studio"].replace('"solo_tracks": solo_tracks,', "")}, B_solo),
        ("头像路由挪到 JSON 之后", {"server": T["server"].replace(
            'if path.startswith("/api/role/") and path.endswith("/avatar"):\n'
            '            return self.role_avatar(path[len("/api/role/"):-len("/avatar")])',
            "")}, C_avatar),
        ("cast 直接改全局样式", {"studio": T["studio"].replace(
            "        style = dict(style)\n", "")}, D_viz),
        ("索引不再跟着下", {"studio": T["studio"].replace(
            "        _fetch_index(job, repo, pick, dst)", "")}, E_market),
        # 字幕这块的三处"看起来做了其实没接上"
        ("截片段时字幕不平移", {"studio": T["studio"].replace(
            'dict(ln, start=float(ln.get("start") or 0) - off', "")}, H_subs),
        ("字幕存了不读回来", {"studio": T["studio"].replace(
            "vizsubs.merge(lines, p.get(\"viz_lines\"))", "")}, H_subs),
        ("做视频没带上现改的字", {"server": T["server"].replace(
            'lines=body.get("lines") or None', "")}, H_subs),
        # 成品自带歌词这条链：存 / 读的顺序，各改坏一次
        ("成品不再自带分句", {"studio": T["studio"].replace(
            '"multi_segs": _segs_brief(segs),', "")}, H_subs),
        ("读字幕时只剩缓存那一档", {"studio": T["studio"].replace(
            'for r2 in (p.get("multi_segs") or []):', 'for r2 in ():')}, H_subs),
        # 画面选项：这几条的判据是**像素**，改源码字串改不动画面
        # ⇒ 用 "__mod__" 当场把那个函数换掉，跑完再换回来。
        ("扫色默认被关掉", {"__mod__": "karaoke_off"}, I_castopts),
        ("动效默认被关掉（头像不动了）", {"__mod__": "motion_off"}, I_castopts),
        ("竖屏不重排（还是挤一排）", {"__mod__": "layout_flat"}, I_castopts),
        ("选项没进到渲染里（片头片尾被吞）", {"__mod__": "opt_ignored"}, I_castopts),
        ("排版不避开头像", {"__mod__": "lyric_no_avoid"}, I_castopts),
        # 逐窗认人：这两条改坏，画面就退回"信记录"那条老路
        ("逐窗认人直接关掉（退回信记录）", {"__mod__": "who_off"}, J_who),
        ("段表不规整（换人慢半秒）", {"__mod__": "who_untidy"}, J_who),
        ("界面少了「重出视频」按钮", {"html": T["html"].replace('id="btnSubsRedo"', "")},
         I_castopts),
    ]
    QUIET = True
    caught = 0

    def _mod(name):
        """把 vizfx 的某个函数临时换掉，返回"换回来"的函数。

        ★ 为什么像素类判据必须这样改坏：那些判据量的是**画出来的像素**，
          在源码字串上动刀根本影响不到已经 import 进来的模块。
          一开始我用 replace 改 T["vizfx"]，5 条改坏全"抓住了" —— 那是假的，
          因为改的是那份字符串，画的时候用的还是原来的函数。
        """
        import studio
        import vizfx
        if name == "karaoke_off":
            d = vizfx.CAST_OPT_DEFAULT

            def undo():
                d["karaoke"] = True
            d["karaoke"] = False
            return undo
        if name == "motion_off":
            # ★ 不能改 CAST_OPT_DEFAULT：判据里的每一帧都显式传了 {"motion": ...}，
            #   默认值根本不参与 —— 改默认值"抓住了"是假的（实测没抓住）。
            #   要从**取选项那一层**把动效按住。
            old = vizfx.cast_opt_of

            def nomotion(style):
                return dict(old(style), motion=False)

            vizfx.cast_opt_of = nomotion

            def undo():
                vizfx.cast_opt_of = old
            return undo
        if name == "layout_flat":
            old = vizfx._cast_layout

            def flat(w, h, n, mind, bottom_limit=None):
                return old(w, max(1.0, w * 0.5), n, mind)      # 假装它是横屏

            vizfx._cast_layout = flat

            def undo():
                vizfx._cast_layout = old
            return undo
        if name == "opt_ignored":
            old = vizfx.cast_opt_of

            def drop(style):
                return dict(vizfx.CAST_OPT_DEFAULT, intro=0.0, outro=0.0)

            vizfx.cast_opt_of = drop

            def undo():
                vizfx.cast_opt_of = old
            return undo
        if name == "who_off":
            old = studio.multi_who_scan
            noop = lambda *a, **k: None           # noqa: E731
            noop.__name__ = "multi_who_scan"
            studio.multi_who_scan = noop

            def undo():
                studio.multi_who_scan = old
            return undo
        if name == "who_untidy":
            old = studio._who_tidy
            identity = lambda w: [list(x) for x in (w or [])]   # noqa: E731
            identity.__name__ = "_who_tidy"
            studio._who_tidy = identity

            def undo():
                studio._who_tidy = old
            return undo
        if name == "lyric_no_avoid":
            old = vizfx._cast_lyric_ys
            vizfx._cast_lyric_ys = lambda bb, h, lay, zh: (h * 0.688, h * 0.652)

            def undo():
                vizfx._cast_lyric_ys = old
            return undo
        raise RuntimeError("不认识的改坏方式：%s" % name)

    for label, patch, fn in cases:
        T2 = dict(T)
        undo = None
        if "__mod__" in patch:
            undo = _mod(patch["__mod__"])
        else:
            T2.update(patch)
        try:
            rows = fn(T2)
        finally:
            if undo:
                undo()
        red = sum(1 for _n, ok, _d in rows if not ok)
        if red:
            caught += 1
        else:
            FAILS.append(("尺子自检没抓住：%s" % label, "改坏了仍然全绿"))
    QUIET = False
    N_PASS, N_FAIL = before_p, before_f
    out = [("尺子自检：%d 种改坏全都能抓住" % len(cases), caught == len(cases),
            "抓住 %d/%d" % (caught, len(cases)))]
    return out


def main():
    global N_PASS, N_FAIL
    T = {"studio": rd("studio.py"), "server": rd("server.py"),
         "app": rd("web/app.js"), "vizjs": rd("web/viz.js"),
         "html": rd("web/index.html"), "mkt": rd("modelhub.py"),
         "vizfx": rd("vizfx.py"), "subs": rd("vizsubs.py")}

    print("== A 人数上限 ==")
    for n, c, d in A_cap(T):
        check(n, c, d)
    print("== B 每人独唱 ==")
    for n, c, d in B_solo(T):
        check(n, c, d)
    print("== C 角色头像 ==")
    for n, c, d in C_avatar(T):
        check(n, c, d)
    print("== D 合唱视频 ==")
    for n, c, d in D_viz(T):
        check(n, c, d)
    print("== E 市场导入 ==")
    for n, c, d in E_market(T):
        check(n, c, d)
    print("== F 谁唱谁亮（像素）==")
    for n, c, d in F_pixels():
        check(n, c, d)
    print("== H 字幕（可改 + 中文）==")
    for n, c, d in H_subs(T):
        check(n, c, d)
    print("== I 合唱台画面选项（扫色/动效/竖屏/片头/背景）==")
    for n, c, d in I_castopts(T):
        check(n, c, d)
    print("== J 谁唱谁亮：以音频为准（不信记录）==")
    for n, c, d in J_who(T):
        check(n, c, d)
    print("== G 尺子自检 ==")
    for n, c, d in selftest(T):
        check(n, c, d)

    print("\n通过 %d 项，失败 %d 项" % (N_PASS, N_FAIL))
    if N_FAIL:
        for n, d in FAILS:
            print("  ! %s | %s" % (n, d))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
