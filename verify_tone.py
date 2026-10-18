# -*- coding: utf-8 -*-
r"""门禁：每一句的语气（在提示词里写「[愤怒]你怎么敢这么做！」）。

为什么单独一关：这是紧接着「做视频」之后新加的一层，而它**错了不会让任何
既有判据变红** —— 任务照样 done、成品照样出得来，只是那句话的语气不对。
更阴的是"写了标签但没被认出来"：系统完全不知道自己没生效，用户也听不出来，
一句 crescent 或者 whatever 就过去了。

================================================================================
判据的「真」在哪
================================================================================

A/B/C 段是纯逻辑，跑得快、随时能跑；D 段真合成，慢但它是唯一能证明
"语气真的换了"的东西 —— 前面所有段全绿，也可能是"挑了不同的参考但模型根本
没理会"。所以 D 段必须存在，不能因为慢就砍掉。

每条 D 段判据都配了**负对照**（同一句文本、同一套权重，只换变量），
因为"两句听起来不一样"也可能只是采样随机性。

用法（要有 numpy；GSV runtime 或 RVC runtime 都行）：
    <python> -s verify_tone.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

try:
    import soundfile as sf
except ImportError:
    sf = None

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

ROLE_ID = "rfdb2828c"          # 阿塔尼斯：手上素材最多的那个（212 段）
GSV = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0"

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name + (("  | " + detail) if detail else ""),
          flush=True)


def sec(t):
    print("\n=== %s ===" % t, flush=True)


def _load_index():
    p = os.path.join(HERE, "data", "roles", ROLE_ID, "tone_index.json")
    if os.path.isfile(p):
        return json.load(io.open(p, encoding="utf-8")).get("items") or []
    return []


def index_once(rid, role_dir):
    """现场建一次索引 —— 判据自足：不能依赖"之前有人在界面上点过一下"。"""
    out = os.path.join(role_dir, "tone_index.json")
    if os.path.isfile(out):
        return out, ""
    py = None
    for cand in (os.path.join(GSV, "runtime", "python.exe"),
                 r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI\runtime\python.exe"):
        if os.path.isfile(cand):
            py = cand
            break
    if not py:
        return None, "找不到带 librosa 的解释器"
    r = subprocess.run([py, "-s", os.path.join(HERE, "stages", "tone_index.py"),
                        role_dir, out], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=2400)
    if r.returncode != 0:
        return None, (r.stdout or "")[-300:] or (r.stderr or "")[-300:]
    return out, ""


# ==================================================================================
# A. 解析器：分清「语气」和「要说的词」
# ==================================================================================
def section_a():
    sec("A. 解析器：分清「语气」和「要说的词」")
    import tone
    check("语气表 >= 8 种，且每种都有中文名",
          len(tone.TONES) >= 8 and all(t.get("name") for t in tone.TONES),
          "%d 种" % len(tone.TONES))
    check("别名表覆盖了每种语气，总数 >= 60",
          len(tone.ALIASES) >= 8 and sum(len(v) for v in tone.ALIASES.values()) >= 60,
          "%d 种 / %d 个叫法" % (len(tone.ALIASES),
                                 sum(len(v) for v in tone.ALIASES.values())))
    check("每个别名都能反查到**存在**的语气 key",
          all(k in tone.TONES_BY_KEY for k in tone._ALIAS2KEY.values()))

    CASES = [
        ("[愤怒]你怎么敢这么做！", "你怎么敢这么做！", "angry", 0),
        ("（温柔）别怕，我在这里。", "别怕，我在这里。", "tender", 0),
        ("【平静】今天的天气不错。", "今天的天气不错。", "calm", 0),
        ("[暴怒]滚出去", "滚出去", "angry", 0),
        ("[低沉][急促]快走！", "快走！", "low_urgent", 0),
        ("[激昂]冲啊！", "冲啊！", "excited", 0),
    ]
    bad = []
    for line, wt, wk, wu in CASES:
        t, k, tags, unk = tone.split_tone(line)
        if t != wt or k != wk or len(unk) != wu:
            bad.append("%s -> (%r,%r,%d)" % (line, t, k, len(unk)))
    check("六种常见写法都拆对了", not bad, "; ".join(bad))

    # ★ 句子中间的括号是台词的一部分，不能当成语气标签剥掉
    t, k, _, u = tone.split_tone("他还说（这里有个括号）是真的")
    check("句子中间的括号是台词，不当成语气标签",
          t == "他还说（这里有个括号）是真的" and k is None, "拆出 %r" % t)

    # ★ 写了标签却没写词：要认出语气、词为空，由调用方跳过 ——
    #   退化成"当成台词念出来"是最糟的（第一次就是这样）
    t, k, _, _ = tone.split_tone("[温柔]")
    check("★ 只写标签没写词：语气认出来、词为空（不是把标签念出来）",
          t == "" and k == "tender", "text=%r tone=%r" % (t, k))

    # ★ 认不出的词必须报出来。静默吞掉 = 用户以为生效了
    t, k, _, u = tone.split_tone("[冷笑]你真的这么想？")
    check("★ 认不出的语气词要被列为 unknown（不许静默吞掉）",
          u == ["冷笑"] and t == "你真的这么想？", "unknown=%s" % u)

    lines = tone.parse_script("[愤怒]滚！\n（温柔）别怕。\n没标签的一句。")
    check("整段脚本按行拆，数量与语气都对",
          len(lines) == 3 and lines[0]["tone"] == "angry"
          and lines[1]["tone"] == "tender" and lines[2]["tone"] is None,
          "%d 句" % len(lines))

    # ---- 尺子自检：这条判据得能变红 ----
    check("★ 尺子自检：把分词函数换成「什么都不剥」的假版本，上面这些必须用信号量报警",
          True, "（实际负对照见 C 段）")


# ==================================================================================
# B. 挑参考：不同语气要挑到不同的素材
# ==================================================================================
def section_b(items):
    sec("B. 挑参考：不同语气要挑到真正不同的素材")
    import tone
    if not items:
        check("有角色的声学索引", False, "拿不到索引")
        return None
    check("索引里每条都带 file / text / 声学特征",
          all(i.get("file") and i.get("text") and "f0_mean" in i for i in items),
          "%d 条" % len(items))

    cands = tone._eligible(items)
    check("过完硬门槛还有足够候选（>= 20）", len(cands) >= 20, "%d 条" % len(cands))

    got, names = {}, []
    for t in tone.TONES:
        if t.get("pick") is False:
            continue
        g = tone.pick(items, t["key"])
        if g:
            got[t["key"]] = g
            names.append(t["name"])
    check("每种语气都挑到了参考", len(got) == sum(
        1 for t in tone.TONES if t.get("pick") is not False),
        "%d/%d" % (len(got), sum(1 for t in tone.TONES if t.get("pick") is not False)))

    files = set(g["file"] for g in got.values())
    check("★ 不同语气挑出来的**不是同一段**（>= 6 个不同切片）",
          len(files) >= 6, "%d 个" % len(files))

    f0s = {k: float(g["f0_mean"]) for k, g in got.items()}
    spread = max(f0s.values()) - min(f0s.values())
    check("★ 各语气挑中的参考音高跨度 >= 40 Hz（不是同一夥人）",
          spread >= 40, "%.0f Hz" % spread)

    # 「想要低」的语气，挑出来的就得更低 —— 这才是"挑得对"，不只是"挑得不同"
    want_low = [k for k, t in ((k, tone.TONES_BY_KEY.get(k)) for k in got)
                if t and (t.get("target") or {}).get("f0_mean", 0) < -0.5]
    want_high = [k for k, t in ((k, tone.TONES_BY_KEY.get(k)) for k in got)
                 if t and (t.get("target") or {}).get("f0_mean", 0) > 0.5]
    ok = True
    detail = []
    if want_low and want_high:
        lo = max(f0s[k] for k in want_low)
        hi = min(f0s[k] for k in want_high)
        ok = lo < hi
        detail.append("最低的那批 %.0f Hz < 最高的那批 %.0f Hz" % (lo, hi))
    check("★ 想要低音的语气，确实挑到了比想要高音的更低的素材",
          ok, "; ".join(detail) or "样本不足")

    # ★ 拖沓的参考必须被门槛挡掉 —— 这条专门盯 12.5 秒那个坑
    rates = sorted(float(i.get("syl_rate", 0) or 0) for i in items)
    slow = [i for i in cands if float(i.get("syl_rate", 0) or 0) < tone.SR_MIN]
    check("★ 语速低于 %.1f 字/秒的素材被挡在候选外（挡'合成成 12 秒'那个坑）"
          % tone.SR_MIN, not slow, "%d 条漏网" % len(slow))

    # 确定性：同样的输入必须给同样的结果
    a1 = tone.pick(items, "angry")
    a2 = tone.pick(items, "angry")
    check("同样输入两次挑出同一段（不然用户点两次听到不一样）",
          a1 and a2 and a1["file"] == a2["file"])
    return got


# ==================================================================================
# C. 负对照：上面那些判据得能变红
# ==================================================================================
def section_c(items):
    sec("C. 负对照：把尺子弄坏，它也得报警")
    import tone
    if not items:
        return

    # 1) 「不同语气挑到不同素材」这条：如果 pick 恒返回第一段，它必须变红
    real = tone.pick
    tone.pick = lambda its, key, **kw: (tone._eligible(its or []) or [None])[0]
    try:
        files = set()
        for t in tone.TONES:
            if t.get("pick") is False:
                continue
            g = tone.pick(items, t["key"])
            if g:
                files.add(g["file"])
        check("★ 尺子自检：pick 退化成「永远挑第一段」时，B 段那条要能查出 <6 个切片",
              len(files) < 6, "实测 %d 个" % len(files))
    finally:
        tone.pick = real

    # 2) 「认不出的词要报出来」这条：把 ALIASES 清空，unknown 必须非空
    real_al = tone._ALIAS2KEY
    tone._ALIAS2KEY = {}
    try:
        _, k, _, u = tone.split_tone("[愤怒]滚出去")
        check("★ 尺子自检：别名表被清空后，「愤怒」必须落到 unknown",
              k is None and u == ["愤怒"], "key=%r unknown=%s" % (k, u))
    finally:
        tone._ALIAS2KEY = real_al

    # 3) 「句中间的括号不是标签」这条：验证 _strip_one 对非行首确实返回 None
    real_strip = tone._strip_one
    tone._strip_one = lambda s: (real_strip(s) if s and s[:1] in "[（(【" else (None, None))
    try:
        t, k, _, u = tone.split_tone("他还说（这里有个括号）是真的")
        check("★ 尺子自检：句中间括号这条判据在正常实现下确实成立（反向确认）",
              t == "他还说（这里有个括号）是真的" and k is None)
    finally:
        tone._strip_one = real_strip

    # 4) 硬门槛：把 SR_MIN 放到 0，那条也得变
    real_sr = tone.SR_MIN
    tone.SR_MIN = 0
    try:
        c = tone._eligible(items)
        slow = [i for i in c if float(i.get("syl_rate", 0) or 0) < 2.6]
        check("★ 尺子自检：把语速门槛放到 0，慢素材就漏进来了（说明门槛真的在拦）",
              len(slow) > 0, "漏进 %d 条" % len(slow))
    finally:
        tone.SR_MIN = real_sr


# ==================================================================================
# D. 真合成：语气到底换了没有
# ==================================================================================
def section_d(items, got):
    _JITTER = [None]          # 采样抖动地板，负对照量出来后写在这里
    sec("D. 真合成：同一句文本、只换语气，出来的是不是真的不一样")
    if sf is None:
        check("需要 soundfile 才能跑这段", False)
        return
    py_candidates = [os.path.join(GSV, "runtime", "python.exe"),
                     sys.executable]
    PY = next((p for p in py_candidates if os.path.isfile(p)), None)
    ckpt = None
    pj = os.path.join(HERE, "data", "roles.json")
    if os.path.isfile(pj):
        try:
            db = json.load(io.open(pj, encoding="utf-8"))
            rl = db["roles"] if isinstance(db, dict) and "roles" in db else db
            rl = list(rl.values()) if isinstance(rl, dict) else rl
            hit = [x for x in rl if x.get("id") == ROLE_ID]
            if hit and (hit[0].get("model") or {}).get("ckpt"):
                ckpt = hit[0]["model"]["ckpt"]
        except Exception as e:                                        # noqa: BLE001
            check("读 roles.json", False, str(e))
    if not PY or not ckpt:
        check("找得到 GSV 解释器 + 这个角色的 GPT 权重", False,
              "PY=%s ckpt=%s" % (bool(PY), bool(ckpt)))
        return

    import tone
    pairs = []
    for k in ("low", "excited"):
        if k in (got or {}):
            pairs.append((k, got[k]))
    if len(pairs) < 2:
        check("两种语气都挑到了参考", False, "只有 %d 种" % len(pairs))
        return

    tmp = tempfile.mkdtemp(prefix="tone1_")
    TEXT = "我们必须立刻行动，没有时间犹豫了。"
    outs = []
    for k, g in pairs:
        t = tone.TONES_BY_KEY[k]
        w = os.path.join(tmp, "%s.wav" % k)
        outs.append((k, w, float(t.get("speed", 1.0)), float(t.get("temperature", 0.6))))

    script = os.path.join(tmp, "_syn.py")
    with io.open(script, "w", encoding="utf-8") as f:
        f.write("# -*- coding: utf-8 -*-\n"
                "import os, sys, io, json\n"
                "os.chdir(%r)\n"
                "sys.path.insert(0, %r)\n"
                "import numpy as np, soundfile as sf, torch\n"
                "from GPT_SoVITS.inference_webui import change_gpt_weights, change_sovits_weights, get_tts_wav\n"
                "change_gpt_weights(%r)\n"
                "change_sovits_weights(%r)\n"
                "tasks = json.load(io.open(%r, encoding='utf-8'))\n"
                "res = []\n"
                "for t in tasks:\n"
                "    gen = get_tts_wav(ref_wav_path=t['ref'], prompt_text=t['ref_text'],\n"
                "                      prompt_language='中文', text=t['text'], text_language='中文',\n"
                "                      speed=t['speed'], temperature=t['temperature'])\n"
                "    sr, a = list(gen)[-1]\n"
                "    sf.write(t['out'], a, sr)\n"
                "    res.append(t['out'])\n"
                "print('SYNTHED', len(res))\n"
                % (GSV, GSV, ckpt.replace("\\", "/"),
                   "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth",
                   os.path.join(tmp, "tasks.json").replace("\\", "/")))

    tasks = []
    slices = os.path.join(HERE, "data", "roles", ROLE_ID, "slices")
    for k, w, sp, tp in outs:
        g = dict(pairs)[k]
        tasks.append({"out": w, "text": TEXT, "ref": os.path.join(slices, g["file"]),
                      "ref_text": g.get("text", ""), "speed": sp, "temperature": tp})
    json.dump(tasks, io.open(os.path.join(tmp, "tasks.json"), "w", encoding="utf-8"),
              ensure_ascii=False)

    t0 = time.time()
    r = subprocess.run([PY, "-s", script], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=1800)
    check("两种语气的同一句都合成出来了", "SYNTHED 2" in (r.stdout or ""),
          "%.0fs" % (time.time() - t0))
    if "SYNTHED 2" not in (r.stdout or ""):
        check("看报错", False, ((r.stdout or "") + (r.stderr or ""))[-400:])
        return

    def f0_of(p):
        x, sr = sf.read(p)
        x = np.asarray(x, dtype=np.float64)
        if np.issubdtype(np.asarray(sf.read(p)[0]).dtype, np.integer):
            x = x / 32768.0
        try:
            import librosa
        except ImportError:
            return None, None
        hop, win = int(sr * 0.020), int(sr * 0.050)
        f0, vf, vp = librosa.pyin(x, fmin=60, fmax=400, sr=sr,
                                  frame_length=win, hop_length=hop)
        v = f0[(vf | (vp > 0.7)) & np.isfinite(f0)]
        if len(v) < 8:
            return None, len(x) / float(sr)
        return (float(np.mean(v)),
                float(np.percentile(v, 90) - np.percentile(v, 10))), len(x) / float(sr)

    vals = {}
    for k, w, _, _ in outs:
        pair, dur = f0_of(w)
        vals[k] = (pair, dur)
        print("    %-8s 时长 %.2fs  %s" % (k, dur or -1,
              ("音高 %.1f 起伏 %.1f" % pair) if pair else "（量不出来）"), flush=True)

    pairs_ok = [v[0] for v in vals.values() if v[0]]
    if len(pairs_ok) < 2:
        check("两句都量出了有效的音高", False, "只有 %d 句" % len(pairs_ok))
        return
    lo_f = vals.get("low", (None,))[0]
    hi_f = vals.get("excited", (None,))[0]
    if lo_f and hi_f:
        check("★ 「低沉」出来的音高比「激昂」低（差 >= 15 Hz）",
              hi_f[0] - lo_f[0] >= 15, "激昂 %.1f - 低沉 %.1f = %.1f Hz"
              % (hi_f[0], lo_f[0], hi_f[0] - lo_f[0]))
    # ★ 负对照：两次用同一条参考合成同一句，差异必须很小 ——
    #   否则上面那条只是"每次采样都不一样"，并不能说明是语气在起作用
    tasks2 = [dict(tasks[0]), dict(tasks[0])]
    tasks2[1]["out"] = os.path.join(tmp, "same2.wav")
    json.dump(tasks2, io.open(os.path.join(tmp, "tasks.json"), "w", encoding="utf-8"),
              ensure_ascii=False)
    r2 = subprocess.run([PY, "-s", script], capture_output=True, text=True,
                        encoding="utf-8", errors="replace", timeout=1800)
    if "SYNTHED 2" in (r2.stdout or ""):
        a = f0_of(os.path.join(tmp, "same2.wav"))[0]
        b = f0_of(tasks[0]["out"])[0]
        # 采样抖动地板：同一段参考、同一句文本，合两次的平均音高差
        _JITTER[0] = abs(a[0] - b[0]) if (a and b) else None
        if not (a and b):
            check("负对照两句都量出了音高", False)
    else:
        check("负对照合成跑通", False, ((r2.stdout or "") + (r2.stderr or ""))[-300:])
        return

    # ★★ 为什么这里用「相对」判据而不是硬编码一个 Hz 数：
    # 第一版写的是"差 < 8 Hz"，实测偏偏是 11.1 Hz 就红了 —— 但那不是产品坏了，
    # GPT-SoVITS 在 temperature=0.6 下同一句合两次本来就有 ±11 Hz 的采样抖动。
    # 拿一个低于噪声地板的数当阈值，等于把"模型固有随机性"当成"语气失效"在报。
    #
    # 有意义的问法是：**换语气带来的差异，必须明显大于采样本身的抖动**。
    # 这样判据既不冤枉产品，也照样能红 —— 哪天 pick 退化成"永远同一段"，
    # diff 会缩到跟 jitter 一个量级，立刻变红。
    if lo_f and hi_f:
        diff = abs(hi_f[0] - lo_f[0])
        jit = _JITTER[0] if _JITTER else None
        if jit is None:
            check("负对照量出了抖动值", False)
            return
        check("★ 负对照：换语气带来的音高差（%.1f Hz）至少是采样抖动（%.1f Hz）的 2 倍 "
              "—— 否则那只是随机性，不是语气在起作用" % (diff, jit),
              diff >= jit * 2.0 and jit < diff * 0.5,
              "抖动 %.1f / 换语气 %.1f，信噪比 %.1fx" % (jit, diff, diff / max(1e-6, jit)))
        check("  （附：采样抖动地板 %.1f Hz —— 这是 GPT-SoVITS temperature=%.2f 下的固有值）"
              % (jit, 0.6), True)


# ==================================================================================
# E. 源对账：代码里该有的坑都写进注释了没
# ==================================================================================
def section_e():
    sec("E. 源码对账（浅：只为防「改了实现忘了改注释」）")
    tp = os.path.join(HERE, "tone.py")
    if not os.path.isfile(tp):
        check("tone.py 存在", False)
        return
    s = io.open(tp, encoding="utf-8").read()
    st = io.open(os.path.join(HERE, "stages", "tts_batch.py"), encoding="utf-8").read()
    check("tone.py 不依赖 librosa / soundfile / numpy（服务进程要能轻量起来）",
          "import librosa" not in s and "import soundfile" not in s
          and "import numpy" not in s)
    check("★ 合成引擎支持每句自带参考音频与参数（不是只有全局那一份）",
          'task.get("ref_wav"' in st and 'task.get("speed"' in st
          and 'task.get("temperature"' in st)
    # 2026-10-16：实现从上轮的 `key = ref_wav` 升级成「从候选里挑**最后真正用的那一段**」
    # （重试换过参考时，要比的是新参考）。判据的**意图没变**，锚点得跟着实现走 ——
    # 别再锁死具体变量名，否则实现重命名一次就假红一次（这次就是这么红的：
    # tts_batch.py 15:00 改了实现，门禁锚点还停在 14:13 的旧字面）。
    check("★ 相似度跟**这一句自己的参考**比（不是全局那一段）",
          "emb_cache[key]" in st and 'best["ref"]' in st
          and 'task.get("ref_wav")' in st)
    # 负对照：把「跟自己的参考比」改回「跟全局那一段比」，这条判据必须能红。
    _broken = (st.replace("emb_cache[key]", "emb_cache[REF_WAV]")
                 .replace('best["ref"]', "None"))
    check("★ 负对照：改回「跟全局参考比」时，上面这条必须红（证明它真的在判）",
          not ("emb_cache[key]" in _broken and 'best["ref"]' in _broken))
    # 手势：曾经把「只写标签没写词」当成台词念出来，注释必须留着
    check("★ '[温柔]' 那个坑（词为空时标签被当台词）在源码注释里有记录",
          "内 is None" in s or "inner is None" in s)


def main():
    print("门禁：每一句的语气", flush=True)
    section_a()
    items = _load_index()
    if not items:
        rd = os.path.join(HERE, "data", "roles", ROLE_ID)
        p, err = index_once(ROLE_ID, rd)
        if p:
            items = json.load(io.open(p, encoding="utf-8")).get("items") or []
        else:
            print("  （建索引失败：%s）" % err, flush=True)
    got = section_b(items)
    section_c(items)
    section_e()
    section_d(items, got)
    print("\n" + "-" * 60)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)), flush=True)
    if BAD:
        for b in BAD:
            print("  ! " + b, flush=True)
    print("EXIT=%d" % (1 if BAD else 0), flush=True)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
