#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：多人的「自己决定哪一句谁唱」，**没认过词也得能用**。

为什么需要这一关
----------------
这条链路上有两个"看着能用、其实从来没生效"的坑，都被老板那句话戳出来了：

  ① `multi_plan_preview` 量时长用的是 `wav_seconds()` —— 它只走 `wave` 模块、
     **只认 wav**。用户的歌大多是 mp3/m4a ⇒ 时长恒 0 ⇒ 分配表整片 0 秒、
     一段都列不出来。页面上的表现是"预览面板空着"，完全不报错。
     （正确的工具 `_probe_dur_safe()` 项目里早就有了，只是这里没用它。）

  ② 就算前端把用户点的分配表送上来，`plan_takes` 第一句是
     `if not segs: return takes_even(...)` —— **manual 直接被丢掉**。
     用户点了一份表、点开跑，出来的还是均分，而且不报错。

所以这一关盯三件事：
  · 量时长必须走能读 mp3 的那条路；
  · 没词时必须造出"按时间切的段"，让 manual 有落脚点；
  · 判据本身要有牙 —— 把 manual 抽掉必须变红。

★ 2026-10-27 补：① 当时**只修了预览那条路**，而 A 段的判据是"查源码字符串"
  且只搜 `multi_plan_preview` 的函数体 ⇒ 真正跑任务的 `_multi_sing` 一直用
  `wav_seconds(song_path)`，**门禁全程绿着，用户点「多角色合唱」第 2 步整条失败**
  （报错还说是"分离后的干声读不出来"，其实干声 197.504 秒好好的，坏的是 mp3 原曲）。
  ⇒ A 段整个换成**行为判据**：用 float32 wav 当"非 PCM 歌"的替身（零外部依赖），
  先断言它真的让 `wav_seconds` 返回 0，再断言 `_song_total_secs` 量得出来，
  并配两条负对照（全 0 不许编数 / 换回 wav_seconds 必须变红）。
"""
import importlib.util
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STUDIO = os.path.join(HERE, "studio.py")
MULTI = os.path.join(HERE, "stages", "multising.py")
APPJS = os.path.join(HERE, "web", "app.js")

N_PASS = 0
FAILS = []


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s" % name)
    else:
        FAILS.append(name)
        print("  [!!] %s\n        %s" % (name, detail or "无诊断信息"))


def load_multi():
    st = os.path.dirname(MULTI)
    if st not in sys.path:
        sys.path.insert(0, st)
    spec = importlib.util.spec_from_file_location("multising_gate", MULTI)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_studio():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import studio
    return studio


def _py_nocomment(src):
    """把 Python 源码里的**注释**挖成空格，其余原样保留。

    ★ 为什么必须有这一步：`_multi_sing` 里那条反例注释本身就写着
      「不能用 `wav_seconds(song_path)`」，纯子串搜索会把它当成"代码还在用它"
      ⇒ **假红**（C 段 2026-10-24 已经被同一个坑绊过一次：
        「只有注释提到旧写法不许误判」）。
    ★ 只挖注释、不重新拼 token：token 拼回去会把 `wav_seconds(song_path)`
      变成 `wav_seconds ( song_path )`，断词模式就再也匹配不上了。
    """
    import io
    import tokenize
    try:
        marks = [(t.start, t.end) for t in
                 tokenize.generate_tokens(io.StringIO(src).readline)
                 if t.type == tokenize.COMMENT]
    except Exception:                                       # noqa: BLE001
        return src
    if not marks:
        return src
    rows = src.splitlines(keepends=True)
    for (sr, sc), (er, ec) in reversed(marks):              # 从后往前，列偏移不乱
        if sr == er:
            line = rows[sr - 1]
            rows[sr - 1] = line[:sc] + " " * (ec - sc) + line[ec:]
    return "".join(rows)


def _mk_wav(path, secs, subtype):
    """造一段正弦 wav。subtype 决定 `wave` 模块读不读得了。"""
    import numpy as np
    import soundfile as sf
    sr = 44100
    n = int(sr * secs)
    t = np.arange(n) / float(sr)
    y = (0.2 * np.sin(2 * np.pi * 440.0 * t)).astype("float32")
    sf.write(path, np.stack([y, y], axis=1), sr, subtype=subtype)
    return path


def a_duration():
    print("\nA 量时长：mp3 那类「wave 读不了」的歌也要量得出来")
    #
    # ★ 这一段 2026-10-27 整个换掉过。上一版是三条**查源码字符串**的判据
    #   （`"_probe_dur_safe(" in body`、`"wav_seconds(song_path)" not in body`），
    #   而且只搜 `multi_plan_preview` 的函数体 —— 于是 **预览那条路修好了，
    #   真正跑任务的 `_multi_sing` 一直用 wav_seconds，从来没人测到**。
    #   表现：门禁一路绿，用户点「多角色合唱」第 2 步整条失败。
    #   这是铁律 11c 的原样复现（"查源码字符串"不是判据）⇒ 换成行为判据。
    #
    st = _load_studio()
    src = open(STUDIO, encoding="utf-8").read()
    tmp = tempfile.mkdtemp(prefix="gate_dur_")
    try:
        # ① 先证明**替身真的复现了病因**，否则后面全绿也是白送的。
        #   float32 wav 就是最干净的替身：`wave.open()` 对 format=3 直接抛
        #   "unknown format: 3" ⇒ 跟 mp3 一样让 wav_seconds 返回 0.0，
        #   而 _probe_dur_safe 退到 ffprobe 仍能量出来。
        #   （不真造 mp3：那要 ffmpeg 编码器，门禁不该为这个加外部依赖。）
        song = _mk_wav(os.path.join(tmp, "song_float32.wav"), 3.0, "FLOAT")
        check("★ 替身真的复现病因：wave 读不出这个文件（= mp3 的表现）",
              st.wav_seconds(song) == 0.0, "wav_seconds=%r" % st.wav_seconds(song))

        # ② 产品函数必须读得出来 —— 这是行为判据（也顺带守住了 ffprobe 全路径，
        #   裸名 ffprobe 在本机 PATH 上会 ENOENT ⇒ 这里会返回 0 而变红）。
        f = getattr(st, "_song_total_secs", None)
        check("_song_total_secs 存在（门禁能直接测，不必真跑一遍合唱）",
              callable(f))
        if callable(f):
            tot, d = f(song, 0.0, None)
            check("★ 只有原曲可用时也量得出时长（不够之前就是整条失败的那个场景）",
                  tot > 2.9, "total=%.3f 明细=%s" % (tot, d))
            check("  同一个函数对普通 PCM wav 也照样准（不是只会读怪格式）",
                  abs(f(_mk_wav(os.path.join(tmp, "pcm.wav"), 2.0, "PCM_16"),
                        0.0, None)[0] - 2.0) < 0.05)

            # ③ 第三路保底：原曲彻底读不到，只剩分离后的干声
            vocal = os.path.join(tmp, "vocal.wav")
            _mk_wav(vocal, 2.0, "PCM_16")
            tot2, d2 = f(os.path.join(tmp, "gone.mp3"), 0.0, vocal)
            check("★ 原曲读不到时由「分离后的干声」兜底（干声必然是 PCM wav）",
                  abs(tot2 - 2.0) < 0.05, "total=%.3f 明细=%s" % (tot2, d2))

            # ④ 负对照 A：三路全 0 ⇒ 必须老实返回 0，不许编一个看起来正常的数
            tot3, d3 = f(os.path.join(tmp, "gone.mp3"), 0.0, None)
            check("  负对照：三个来源全 0 ⇒ 老实返回 0（不许编数）",
                  tot3 == 0.0 and d3["song"] == 0.0 and d3["vocal"] == 0.0,
                  "total=%.3f 明细=%s" % (tot3, d3))

            # ⑤ 负对照 B（最关键）：把量时长的工具换回 wav_seconds
            #    = 上一版的做法 ⇒ 上面②那条**必须不成立**。证明判据有牙。
            old = st._probe_dur_safe
            try:
                st._probe_dur_safe = st.wav_seconds
                tot_bad, _bx = f(song, 0.0, None)
            finally:
                st._probe_dur_safe = old
            check("  负对照：换回 wav_seconds 量原曲 ⇒ ②必然不成立（判据有牙）",
                  not (tot_bad > 2.9),
                  "坏版本 total=%.3f（应当 0，若 >2.9 说明判据抓不住）" % tot_bad)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ⑥ 接线检查 —— ★ 这不是行为判据，**不会在负对照下变红**，别当它是尺子。
    #   它守的是"别再有人手写 wav_seconds(song_path)"，属于防回潮的提醒。
    nocmt = _py_nocomment(src)
    # 尺子自检：去注释器既要去掉注释里的旧写法，又不能把真代码也挖掉。
    _SYN_CMT = "def f():\n    # 不能用 wav_seconds(song_path)\n    return 1\n"
    _SYN_REAL = "def f():\n    return wav_seconds(song_path)\n"
    check("★ 尺子自检：去注释器确实挖掉注释、又不动真代码",
          "wav_seconds(song_path)" not in _py_nocomment(_SYN_CMT)
          and "wav_seconds(song_path)" in _py_nocomment(_SYN_REAL))
    # 三个调用点全点名（2026-10-27 实测：当时一共三处，只改两处会漏掉试听小样那条）。
    for fn in ("sing_multi", "_sing_preview", "multi_plan_preview"):
        i = nocmt.find("def %s(" % fn)
        j = nocmt.find("\ndef ", i + 1) if i >= 0 else -1
        body = nocmt[i:j] if i >= 0 else ""
        check("接线（非行为判据）：%s 里不出现 wav_seconds(song_path)" % fn,
              bool(body) and "wav_seconds(song_path)" not in body,
              "有人把它换回去了？")


def b_pseudo_segs():
    print("\nB 没认过词也要能逐段指定（伪句子）")
    src = open(STUDIO, encoding="utf-8").read()
    i = src.find("def multi_plan_preview(")
    j = src.find("\ndef ", i + 1)
    body = src[i:j] if i >= 0 else ""
    check("没词时会造出按时间切的段", "takes_even(" in body and "_real_lyric" in body)
    check("lyric_ready 报的是**真歌词**有没有（不能被伪句子顶掉）",
          '"lyric_ready": _real_lyric' in body)
    check("chorus 模式不造伪句子（叠唱本来就不分句）",
          'mode != "chorus"' in body)


def _js_code(s):
    """剥掉 // 行注释，只留实际会执行的代码。

    为什么非剥不可：这一关最初直接在整份 app.js 里找旧写法那段字符串，结果被
    自己写的说明性注释（里面原样引用了旧写法）命中 —— **注释让判据恒绿不起来**，
    于是报了一条假红。这里只剥行注释足够（这一条判的是显示门槛，
    `!d.lyric_ready` 不会作为字符串出现在代码里）。
    """
    out = []
    for ln in s.splitlines():
        # 只处理行注释；http:// 这类会被截短，但对本判据无影响（显示门槛不会写在 URL 里）
        t = ln.split("//", 1)[0]
        out.append(t)
    return "\n".join(out)


def gate_uses_lyric_ready(src_js):
    """返回 True：隐藏面板的那个门槛**还在**拿 lyric_ready 说事（= 缺陷还在）。"""
    code = _js_code(src_js)
    i = code.find('list.style.display = "none"')
    if i < 0:
        return True                       # 连藏面板的动作都没找到 ⇒ 形状变了，算可疑
    # 往上找最近的 `if (...)` —— 那个条件就是显示门槛
    j = code.rfind("if (", 0, i)
    if j < 0:
        return True
    cond = code[j:i]
    return "lyric_ready" in cond


def c_frontend():
    print("\nC 前端：面板不许再因为'没认过词'而整个藏起来")
    s = open(APPJS, encoding="utf-8").read()
    check("隐藏面板的门槛不再拿 lyric_ready 说事",
          not gate_uses_lyric_ready(s))
    check("隐藏面板只按模式判（mode !== manual）",
          'if (mode !== "manual") {' in _js_code(s))
    check("manual 模式下没词也有专门说明",
          "按时间切的" in s and "逐段指定" in s)
    # ★ 服务端算出来的那段话（"已按时间对齐" / "XX 没有独唱句"）必须在
    #   **逐句指定**模式也显示出来。以前只有最底下那个 else 才追加 d.hint
    #   ⇒ manual 模式下服务端说的话一句都到不了界面（2026-10-24 第五轮实测）。
    # ★ 阈值 4 = renderMultiPlan 里四个分支各一处（manual+没词 / 只没词 /
    #   manual+有词 / 其它）。★ 别凭印象写阈值：我第一版写 3（以为是三处），
    #   实测坏掉一处之后它照样 3 ⇒ **判据没牙**（绿着说"每个分支都显示了"）。
    #   定这条之前先 grep 数过：好版本就是 4。
    _code = _js_code(s)
    _n_hint = _code.count("d.hint) hint.innerHTML +=")
    check("★ 服务端的话在**每个**分支都显示（含逐句指定，不能只给非 manual）",
          _n_hint >= 4, "追加 d.hint 的地方只有 %d 处（好版本是 4）" % _n_hint)

    # ★ 尺子自检（上一轮的教训：判据被注释误伤过一次，得证明它既认得出真缺陷、
    #   又不会被注释骗）—— 两造都对这款「.static.display」形状做检测。
    OLD = ('function renderMultiPlan(d, mode) {\n'
           '  if (mode !== "manual" || !d.lyric_ready) {\n'
           '    list.style.display = "none";\n'
           '    return;\n'
           '  }\n}')
    CMT = ('  // 以前这里是 `mode !== "manual" || !d.lyric_ready`\n'
           '  if (mode !== "manual") {\n'
           '    list.style.display = "none";\n'
           '  }\n')
    check("★ 尺子自检：喂旧版本必须判得出有缺陷", gate_uses_lyric_ready(OLD) is True)
    check("★ 尺子自检：只有注释提到旧写法不许误判", gate_uses_lyric_ready(CMT) is False)


def d_behavior(mod):
    print("\nD 行为：manual 真的能改分配（纯函数，不碰真文件）")
    dur = 120.0
    segs = [{"start": float(k * 10), "end": float((k + 1) * 10), "text": "x"}
            for k in range(12)]

    # ① 有句子 + manual 全给第 2 人 ⇒ takes 里只应出现角色 1
    man = [1] * len(segs)
    takes, lr, deg = mod.plan_takes(segs, dur, 2, mode="manual", manual=man)
    roles = sorted({int(t[2]) for t in takes})
    check("有句子时 manual 生效（全给第 2 人 ⇒ 只有角色 1）",
          roles == [1], "实际 %s" % roles)

    # ② 交替指定 ⇒ 两个人都有份，且时长接近
    man2 = [k % 2 for k in range(len(segs))]
    takes2, _, _ = mod.plan_takes(segs, dur, 2, mode="manual", manual=man2)
    st = mod.takes_stats(takes2, 2)
    secs = sorted(round(float(x["sec"]), 1) for x in st)
    check("交替指定 ⇒ 两个人都分到了", len({int(t[2]) for t in takes2}) == 2)
    check("交替指定 ⇒ 两人时长接近（%s）" % (secs,),
          abs(secs[0] - secs[1]) <= 12.0, secs)

    # ③★ 负对照：segs 为空时 manual 会被整份丢掉 —— 这**正是**为什么后端
    #   必须造伪句子。这一条红不了才怪，它就是在描述那个坑本身。
    takes3, _, deg3 = mod.plan_takes([], dur, 2, mode="manual", manual=[1, 1, 1])
    check("★ 负对照：segs 为空时 manual 确实被丢掉（所以必须造伪句子）",
          deg3 is True, "manual 在空 segs 下居然生效了？")


def e_always_someone():
    print("\nE 不许伪造「每个人都分到了」")
    src = open(STUDIO, encoding="utf-8").read()
    i = src.find("def multi_plan_preview(")
    j = src.find("\ndef ", i + 1)
    body = src[i:j] if i >= 0 else ""
    # 拆不动 / 不够分时必须说出来，不能静默给一份"看起来正常"的表
    check("拆不动时 hint 会说'注定有人唱不到'",
          "注定有人唱不到" in body or "short" in body)
    check("拆过组时 hint 会说'已按句拆成 N 组'", "split" in body)


def f_after_lyric():
    """★★ F 认过词之后：预览与开跑必须是同一份句子。

    这一整段守的是本轮**真正的根因**：
      预览 `multi_plan_preview` 走 `lyric_segments_any` —— **只读缓存**；
      开跑 `sing_multi` 走 `ensure_lyric_times` —— **会自动认一次词**。
    ⇒ 没认过词时预览给的是"按时间切的 22 段"，开跑却认出 60 句真句子，
      用户点的 manual 只覆盖前 22 句，后面全用第 22 个值（一个人独唱到底），
      不报错、不提示。这就是"我选了却不按我选的来"。

    备料规矩（§24c）：探针缺料自己备，并用 atexit 保证还原 ——
    这里临时造一份歌词缓存，跑完必删，不许污染用户的歌词库。
    """
    print("\nF 认过词之后：预览与开跑必须是同一份句子（★ 根因）")
    import glob
    sys.path.insert(0, HERE)
    try:
        import studio
    except Exception as e:                                  # noqa: BLE001
        check("能加载 studio", False, str(e)[:200])
        return
    songs = sorted(glob.glob(os.path.join(HERE, "data", "music", "*.*")))
    songs = [p for p in songs if p.lower().endswith(
        (".mp3", ".wav", ".m4a", ".flac"))]
    if not songs:
        check("曲库里有一首歌可用来备料", False, "data/music 下没有音频")
        return
    song = songs[0]
    try:
        _d0 = studio.multi_plan_preview(song, [], mode="manual")
    except Exception:                                       # noqa: BLE001
        _d0 = None
    # 必须挑一首**当前没认过词**的歌，否则造缓存会覆盖用户的真结果
    song = None
    for p in songs:
        try:
            if studio.lyric_segments_any(p, "zh")[0]:
                continue
            # ★ 有同目录 .lrc 的歌会被 lrc 顶掉（G 段专门测那条路）⇒
            #   F 段测的是"ASR 认出来的句"，必须挑一首**没有** lrc 的歌，
            #   否则它绿不绿全看 data/music 里第一首是谁 —— 那是靠运气覆盖。
            if studio.local_lrc_path(p):
                continue
            song = p
            break
        except Exception:                                   # noqa: BLE001
            continue
    if not song:
        check("找得到一首没认过词、也没有同目录 .lrc 的歌来备料", False,
              "data/music 里每首都认过词或都带 lrc —— 备料会覆盖真结果")
        return

    N = 37
    segs = [{"start": round(i * 6.0, 2), "end": round(i * 6.0 + 5.2, 2),
             "text": "第%d句词" % (i + 1)} for i in range(N)]
    cache = studio.lyric_cache_path(song, "zh", None)
    existed = os.path.isfile(cache)
    if existed:
        check("备料不会覆盖已有歌词", False, cache)
        return

    def _restore():
        with __import__("contextlib").suppress(Exception):
            os.remove(cache)
    import atexit
    atexit.register(_restore)
    try:
        studio.save_lyric_segments(song, segs, lang="zh")
    except Exception as e:                                  # noqa: BLE001
        check("能造一份临时歌词缓存", False, str(e)[:200])
        return

    db = studio.load_roles()
    roles = (db.get("roles") or [])[:2]
    if len(roles) < 2:
        check("仓库里至少有两个角色", False, "只有 %d 个" % len(roles))
        return
    casts = [{"rid": r.get("id")} for r in roles]

    try:
        d = studio.multi_plan_preview(song, casts, mode="manual", manual=None)
    except Exception as e:                                  # noqa: BLE001
        check("认过词后能排出分配表", False, str(e)[:200])
        return
    check("认过词 ⇒ lyric_ready 为真", d.get("lyric_ready") is True,
          str(d.get("lyric_ready")))
    check("列表就是真句子（句数 = 认出来的 %d 句，不是按时间切的段）" % N,
          len(d.get("lines") or []) == N, "实际 %d 句" % len(d.get("lines") or []))
    check("句子带得上歌词文本（不是空白一片）",
          bool((d["lines"][0].get("text") or "").strip()),
          repr(d["lines"][0].get("text"))[:60])

    # 指定生效：整首给第二个人
    man_all1 = [1] * N
    try:
        d1 = studio.multi_plan_preview(song, casts, mode="manual", manual=man_all1)
    except Exception as e:                                  # noqa: BLE001
        check("份数一致时 manual 正常生效", False, str(e)[:200])
        d1 = None
    if d1:
        sec1 = (d1.get("per_role") or [{}] * 2)[1].get("sec", 0)
        check("逐句指定真的生效（全给第 2 人 ⇒ 他拿到整首）",
              sec1 > 1.0 and (d1.get("per_role") or [{}] * 2)[0].get("sec", 0) == 0,
              str(d1.get("per_role")))

    # ---- 一句都没点时，不许产出「整首一个人」（铁律 22）------------------
    #   实测（2026-10-24 第五轮）：`manual` 为空时 assign_lines 返回 `[0]*n`
    #   ⇒ 预览直接显示「苹果嘉儿 174.46 秒 / 1 段，云宝 0 秒 / 0 段」，
    #   而界面上一句都还没点。这是"分工"功能最坏的失效形态：一份看着正常的独唱表。
    _secs0 = [float(x.get("sec") or 0.0) for x in (d.get("per_role") or [])]
    check("★ 一句都没点时不许产出「整首一个人」（第二个人 0 秒）",
          bool(_secs0) and all(s > 0.5 for s in _secs0),
          "per_role=%s" % [(x.get("name"), x.get("sec"))
                           for x in (d.get("per_role") or [])])
    check("  并且 hint 说清这是**替他排的**（不是他自己点的）",
          "逐句轮流" in (d.get("hint") or ""), repr(d.get("hint"))[:80])

    # ---- ★★ 旧表（份数对不上）：按时间对齐，不拒绝、不静默补齐 ----------
    #   判据（2026-10-24 第五轮重写）。老判据断言"必须拒绝 ValueError"，
    #   被老板的真实场景推翻：他按 14 段点好，认完词变 86 句 ⇒ 任务直接失败、
    #   14 次点击全废，还要在 86 行里重点一遍。**拒绝不是"不静默"，是把他赶走。**
    #   正确做法：他点的是"那**一段时间**归谁"，句数变了时间没变 ⇒ 按时间落回去。
    #
    #   ★ 这一条必须能同时抓住两种坏实现，所以把"特殊的那一份"放在**最后一份**：
    #       ① 老的静默补齐 `manual[i] if i<len else manual[-1]`
    #          ⇒ 第 22 份之后全沿用最后一份的值 ⇒ 后半首整个变掉 ⇒ 红；
    #       ② 直接拒绝（ValueError）⇒ 根本拿不到分配 ⇒ 红。
    OLD = 22
    _old = [0] * OLD
    _old[OLD - 1] = 1                      # 旧表的**最后一份**给第 2 人
    _i3 = {}
    try:
        mod = load_multi()
    except Exception as e:                                  # noqa: BLE001
        mod = None
        check("能加载 multising（旧表对齐那条要用它的纯函数）", False, str(e)[:200])
    _lr = None
    if mod is not None:
        try:
            _lr = mod.assign_lines(segs, 2, mode="manual", manual=_old, info=_i3)
        except Exception as e:                              # noqa: BLE001
            check("★ 旧表（份数对不上）按**时间**对齐，不许拒绝", False,
                  "它抛了 %s：%s —— 老板的 14 次点击就这么废了"
                  % (type(e).__name__, str(e)[:80]))
    if _lr is not None:
        _st0 = min(float(s["start"]) for s in segs)
        _en0 = max(float(s["end"]) for s in segs)
        _span = max(_en0 - _st0, 1e-9)
        _lo = _st0 + (OLD - 1) / float(OLD) * _span     # 旧表最后一份的起始秒
        _mid = [(float(s["start"]) + float(s["end"])) / 2.0 for s in segs]
        _got = [i for i, v in enumerate(_lr) if int(v) == 1]
        check("★ 旧表（份数对不上）按**时间**对齐，不许拒绝、不许静默补齐",
              bool(_got) and all(_mid[i] >= _lo - 1e-6 for i in _got),
              "给第 2 人的句落在 %s 秒（旧表最后一份从 %.1f 秒起）"
              % ([round(_mid[i], 1) for i in _got][:6], _lo))
        check("  对齐这件事记进 info（调用方要靠它说出来）",
              (_i3.get("remap") or {}).get("from") == OLD
              and (_i3.get("remap") or {}).get("to") == N,
              str(_i3.get("remap")))
        try:
            _d3 = studio.multi_plan_preview(song, casts, mode="manual",
                                            manual=_old)
            check("  界面 hint 会说「已按时间对齐」（用户得知道自己那份还在）",
                  "对齐" in (_d3.get("hint") or ""), repr(_d3.get("hint"))[:80])
        except Exception as e:                              # noqa: BLE001
            check("  界面 hint 会说「已按时间对齐」（用户得知道自己那份还在）",
                  False, "%s: %s" % (type(e).__name__, str(e)[:80]))

    # 开跑口径 vs 预览口径：必须是同一份句子
    try:
        job = studio.new_job(None, "gate_probe", "门禁探针")
        s2, _dur2, src2 = studio.ensure_lyric_times(job, song, auto_lyric=True)
    except Exception as e:                                  # noqa: BLE001
        check("能问到开跑时的分句", False, str(e)[:200])
        return
    check("开跑读到的句数 == 预览句数（否则你点的表只对上一半）",
          len(s2 or []) == N, "开跑 %d 句 / 预览 %d 句 / 来源 %s"
          % (len(s2 or []), N, src2))
    check("开跑来源是缓存（不是又认一次 ⇒ 句数不会漂移）", src2 == "cache", src2)

    # 界面必须给得出"先认一次词"这条路
    js = open(APPJS, encoding="utf-8").read()
    check("没认过词时界面上有「先认一次词」按钮",
          'id="btnLyricFirst"' in js and "onLyricFirst" in js)
    check("认词走的是只认词的任务（kind=lyrics）",
          'kind: "lyrics"' in js)
    check("认完会自动重排列表（不是让用户自己刷新）",
          "loadMultiPlan();" in js and "onLyricFirst" in js)


def g_lrc_lines():
    """★★ G 自选句子 = 本地 .lrc 的分段 + 中英双语 + 一句都不合并。

    老板（2026-10-25）：「自选句子依旧不完整 / 我要的是中英双语 / 不会压缩
    综合 / 按歌词文件的分段 / 我依次决定谁唱歌」。

    以前那一列是 ASR 的 60~86 句再按换气**合并成乐句**（实测 13 首平均
    61.9 → 21.2 句）。合并是"不完整"的根因，也是"这一句给谁"点不了的根因
    —— 一句里塞着两三句，点下去等于给整段指派。

    备料（§24c）：临时目录里造一份 .lrc + 一份假曲库条目，**不动**用户的
    music_library.json，atexit 保证删掉。

    ★ 负对照两条，都必须实测能红（铁律 3 / 11b）：
      N1 坏版本把相邻两句并成一句（= 又压缩综合了）⇒ 句数减半 ⇒ 必须红；
      N2 坏版本把中文摘掉 ⇒ 双语判据必须红。
    """
    print("\nG 自选句子：本地 .lrc 的分段 / 中英双语 / 一句都不合并")
    sys.path.insert(0, HERE)
    try:
        import studio
    except Exception as e:                                  # noqa: BLE001
        check("能加载 studio", False, str(e)[:200])
        return
    import tempfile
    import atexit
    import shutil

    tmp = tempfile.mkdtemp(prefix="vs_gate_lrc_")
    atexit.register(lambda: shutil.rmtree(tmp, True))

    # ---- 造一份同目录 .lrc：30 行，两行一个乐句（方便验不合并）----
    NL = 30
    # ★ 文件名必须是**歌名去掉扩展名**那份（gate.mp3 → gate.lrc）。
    #   写成 "gate.mp3.lrc" 是找不到的 —— splitext 只去最后一段。
    lrc = os.path.join(tmp, "gate.lrc")
    texts = []
    with open(lrc, "w", encoding="utf-8") as f:
        f.write('{"t":0,"c":[{"tx":"作词: "},{"tx":"Gate"}]}\n')   # 元信息行：必须被丢掉
        for i in range(NL):
            t = "第%d句原文" % (i + 1)
            texts.append(t)
            # ★ 时间戳按 LRC 规矩写 `[mm:ss.xx]`：`%06.2f` 会写出 `[00:000.00]`
            #   （分钟三位）⇒ parse_lrc 的正则不认 ⇒ 整首变 0 句。
            f.write("[%02d:%05.2f]%s\n" % (int(i * 2.0) // 60,
                                           (i * 2.0) % 60, t))
    song = os.path.join(tmp, "gate_song.mp3")
    with open(song, "wb") as f:
        f.write(b"\0" * 1024)            # 只是要让 os.path.isfile 成立

    lib = {"songs": [{"path": song}]}

    # ---- ① 找得到 --------------------------------------------------
    got = studio.local_lrc_path(song, lib)
    check("歌曲旁边同名的 .lrc 能找到", got == lrc, str(got))

    # ---- ② 用的是 .lrc 的分段，且一句没合并 ------------------------
    segs, src, lp = studio.plan_segs(song, dur=60.0, lib=lib)
    check("有 .lrc 时来源就是 lrc（src=%s）" % src, src == "lrc", str(src))
    check("★ 句数 == .lrc 的行数 %d（不是合并后的）" % NL,
          len(segs) == NL, "实际 %d 句" % len(segs))
    check("  元信息行（作词）没被当成一句",
          not any("作词" in (s.get("text") or "") for s in segs),
          str([s.get("text") for s in segs][:3]))
    check("  原文逐行对得上（没丢句、没并句）",
          [s.get("text") for s in segs] == texts,
          str([s.get("text") for s in segs][:4]))
    check("  n_sub 全是 1（没有任何一句是合出来的）",
          all(int(s.get("n_sub") or 1) == 1 for s in segs))
    check("  时间轴单调、句句挨着（不留缝）",
          all(float(segs[i + 1]["start"]) >= float(segs[i]["start"])
              for i in range(len(segs) - 1))
          and all(float(segs[i]["end"]) >= float(segs[i]["start"]) + 0.4
                  for i in range(len(segs))),
          str([(s["start"], s["end"]) for s in segs[:3]]))

    # ---- ③ 双语：两句都给全 ----------------------------------------
    # 翻译要联网/本地网关，翻不动就跳过（不许因为网关不在就判红 —— 那跟产品无关）
    zhs = [(s.get("zh") or "").strip() for s in segs]
    if any(zhs):
        check("★ 每一行都带中文（中英双语，不是二选一）", all(zhs),
              "空 %d/%d 行" % (zhs.count(""), len(zhs)))
        check("  原文**也**还在（不是被中文顶掉）",
              all((s.get("text") or "").strip() for s in segs))
    else:
        check("★ 每一行都带中文（中英双语，不是二选一）", False,
              "翻译一次都没成（本地网关 8791 或词典都没通）—— 中文那列会空着")

    # ---- ④ 界面：双语两行都画出来 ----------------------------------
    js = open(APPJS, encoding="utf-8").read()
    i = js.find("function manualRowsHTML(")
    body = js[i:i + 3000] if i >= 0 else ""
    check("界面上原文（.mor）和中文（.mzh）**同时**画出来",
          'class="mor"' in body and 'class="mzh"' in body,
          "manualRowsHTML 里没找到两行")
    check("  不再有「×N 合了几段」的角标（已经不合并了）",
          "msub" not in body, "还在画 ×N —— 用户会以为还在合并")
    css = open(os.path.join(HERE, "web", "style.css"), encoding="utf-8").read()
    check("  双语两行有对应样式（.mor/.mzh）",
          ".mline .mor" in css and ".mline .mzh" in css)

    # ---- ★ 负对照 N1：又把句子合并了 ⇒ 必须红 ----------------------
    _real = studio.lrc_segs
    try:
        def _merged(path, lib=None, dur=0.0):
            s, p = _real(path, lib=lib, dur=dur)
            out = []
            for k in range(0, len(s) - 1, 2):        # 两两并句 = 压缩综合
                a = dict(s[k])
                a["text"] = (s[k].get("text") or "") + " " + \
                            (s[k + 1].get("text") or "")
                a["end"] = s[k + 1].get("end")
                a["n_sub"] = 2
                out.append(a)
            return out, p
        studio.lrc_segs = _merged
        s2, _src2, _ = studio.plan_segs(song, dur=60.0, lib=lib)
        n_ok = (len(s2) == NL)
        check("★ 负对照：坏版本把句子并成一半 ⇒ 判据必须红",
              not n_ok, "坏版本反而绿了（%d 句，尺子没牙）" % len(s2))
    finally:
        studio.lrc_segs = _real

    # ---- ★ 负对照 N2：中文被摘掉 ⇒ 双语判据必须红 ------------------
    _real_zh = studio.lrc_zh_for
    try:
        studio.lrc_zh_for = lambda p, s, log=None: ([""] * len(s), "")
        s3, _src3, _ = studio.plan_segs(song, dur=60.0, lib=lib)
        zh_ok = all((x.get("zh") or "").strip() for x in s3)
        check("★ 负对照：中文被摘掉 ⇒ 双语判据必须红",
              not zh_ok, "坏版本反而绿了 —— 尺子没牙")
    finally:
        studio.lrc_zh_for = _real_zh

    # ---- ⑤ 预览 vs 开跑：必须是同一份句 ----------------------------
    src_py = open(STUDIO, encoding="utf-8").read()
    i1 = src_py.find("def multi_plan_preview(")
    i2 = src_py.find("def sing_multi(")
    b1 = src_py[i1:i1 + 9000] if i1 >= 0 else ""
    b2 = src_py[i2:i2 + 9000] if i2 >= 0 else ""
    check("预览走 plan_segs（句子来源只有一处）", "plan_segs(" in b1)
    check("开跑也走 plan_segs（否则你点的表只对上一半）", "plan_segs(" in b2)


def h_import_lrc():
    """★★ H 自己导入歌词（老板：「要我可以自己导入」）。

    自动那份是按歌名在同目录里**猜**的 —— 同名歌、翻唱版、不同语种版
    都可能猜错，猜错就是整列词都是别人的。所以他必须能自己贴一份。

    守四件事：
      ① 导入的那份**顶掉**自动的（优先级最高）；
      ② 没时间轴的纯文本**必须拒收并说明**（收了就是一份对不上的表）；
      ③ 删掉导入的 ⇒ 回到自动那份（不留垃圾）；
      ④ 界面和服务端都得有这条通道（不然只是个内部函数）。

    ★ 负对照：坏版本 local_lrc_path 不认导入的那份 ⇒ status 不是 import ⇒ 必须红。
    """
    print("\nH 自己导入歌词")
    sys.path.insert(0, HERE)
    try:
        import studio
    except Exception as e:                                  # noqa: BLE001
        check("能加载 studio", False, str(e)[:200])
        return
    import tempfile
    import atexit
    import shutil

    tmp = tempfile.mkdtemp(prefix="vs_gate_imp_")
    atexit.register(lambda: shutil.rmtree(tmp, True))
    song = os.path.join(tmp, "imp_song.mp3")
    with open(song, "wb") as f:
        f.write(b"\0" * 512)
    # 造一份"自动能找到的"（同目录那份），好验证导入的会顶掉它
    auto_lrc = os.path.join(tmp, "auto.lrc")
    with open(auto_lrc, "w", encoding="utf-8") as f:
        for i in range(12):
            f.write("[%02d:%05.2f]自动第%d句\n" % (int(i * 2) // 60,
                                                 (i * 2) % 60, i + 1))
    lib = {"songs": [{"path": song}]}

    def _path():
        return studio.local_lrc_path(song, lib)

    # ① 导入的顶掉自动的
    txt = "\n".join("[%02d:%05.2f]导入第%d句" % (int(i * 3) // 60,
                                                (i * 3) % 60, i + 1)
                    for i in range(20))
    try:
        n, p = studio.lrc_import_save(song, txt)
    except Exception as e:                                  # noqa: BLE001
        check("能存一份自己导入的歌词", False, "%s: %s"
              % (type(e).__name__, str(e)[:160]))
        return
    check("导入了 %d 句" % n, n == 20, str(n))
    check("★ 导入的那份**顶掉**自动找到的（他的话最大）",
          _path() == p, "用的还是 %s" % _path())
    st = studio.lrc_status(song)
    check("  status 报的是 import（界面要说清用的是哪份）",
          st.get("src") == "import", str(st))
    try:
        segs, src, lp = studio.plan_segs(song, dur=90.0, lib=lib)
        check("  plan_segs 也认这份（src=%s）" % src, src == "import", str(src))
        check("  内容就是导入的那 %d 句" % n,
              len(segs) == 20 and "导入第1句" == (segs[0].get("text") or ""),
              str([s.get("text") for s in segs][:2]))
    except Exception as e:                                  # noqa: BLE001
        check("  plan_segs 也认这份", False, str(e)[:160])

    # ② 没时间轴的纯文本：拒收 + 说明
    try:
        studio.lrc_import_save(song, "第一行词\n第二行词\n第三行词\n")
        check("★★ 没时间轴的纯文本被拒收（收了就是对不上的表）", False,
              "它收下了 —— 分句全靠 [mm:ss]，没有就等于没分段")
    except studio.Fail as e:
        check("★★ 没时间轴的纯文本被拒收（收了就是对不上的表）", True)
        check("  拒绝时说清**为什么**（要带时间轴的那种）",
              "时间轴" in str(e) and "[00:" in str(e), str(e)[:120])
    except Exception as e:                                  # noqa: BLE001
        check("★★ 没时间轴的纯文本被拒收", False,
              "抛的是 %s 不是 studio.Fail ⇒ 界面会显示 500，看不懂"
              % type(e).__name__)

    # ③ 删掉 ⇒ 回到自动那份
    studio.lrc_import_clear(song)
    check("删掉导入的 ⇒ 回到自动找到的那份", _path() == auto_lrc, str(_path()))
    check("  status 不再报 import",
          studio.lrc_status(song).get("src") != "import",
          str(studio.lrc_status(song)))

    # ④ 通道：前端 + 服务端
    js = open(APPJS, encoding="utf-8").read()
    check("界面上有「自己导入」的入口", 'data-lrc="edit"' in js
          and "/api/multi/lrc/save" in js)
    check("  导入后会自动重排列表（不然他看不见换了）",
          "lrcBarLoad" in js and "loadMultiPlan(true)" in js)
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    check("服务端三条路都在（查 / 存 / 删）",
          '/api/multi/lrc"' in srv and "/api/multi/lrc/save" in srv
          and "/api/multi/lrc/clear" in srv)

    # ★ 负对照：坏版本"存了但不用"（把导入那档的优先级弄丢了）⇒ 判据必须红。
    #   ★ 不能靠"让保存直接抛异常"来造红 —— 那是**崩了**不是**判红**，
    #     照铁律 11b 那算假尺子。要造的是真会发生的坏实现：优先级里漏掉导入。
    _real_np = studio.local_lrc_path
    _real_ip = studio.lrc_import_path

    def _no_import(song_path, lib=None):
        p = _real_np(song_path, lib)
        imp = _real_ip(song_path)
        try:
            if p and imp and os.path.samefile(p, imp):
                return ""                 # 坏版本：不认导入的那份
        except OSError:
            pass
        return p

    try:
        studio.lrc_import_save(song, txt)
        studio.local_lrc_path = _no_import
        bad_src = studio.lrc_status(song).get("src")
        # ★ 负对照的语义是"期望判据**红**"：坏版本下 status 不该还是 import。
        #   它要是还报 import ⇒ 说明这条判据根本没在看"用没用导入的那份"。
        check("★ 负对照：坏版本不认导入的那份 ⇒ 判据必须红",
              bad_src != "import",
              "坏版本下 status 还是 %s ⇒ 判据没红，尺子没牙" % bad_src)
        studio.lrc_import_clear(song)
    except Exception as e:                                  # noqa: BLE001
        check("★ 负对照：坏版本不认导入的那份 ⇒ 判据必须红", False,
              str(e)[:160])
    finally:
        studio.local_lrc_path = _real_np


def main():
    print("门禁：多人「自己决定哪一句谁唱」（没认过词也能用）")
    a_duration()
    b_pseudo_segs()
    c_frontend()
    d_behavior(load_multi())
    e_always_someone()
    f_after_lyric()
    g_lrc_lines()
    h_import_lrc()
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    for f in FAILS:
        print("  失败：%s" % f)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
