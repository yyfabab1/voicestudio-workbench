# -*- coding: utf-8 -*-
r"""门禁：长台词不许乱（说话这一侧）。

老板的原话：「说话太长台词会乱掉」。

================================================================================
这一关在防什么
================================================================================

**错了不会让任何既有判据变红** —— 任务照样 done、成品照样出得来、"出声体检"
照样全 OK（丢字之后时长依然落在正常区间里），只是那半句话**根本没念出来**。
这是本项目里最阴的一类事故：静默交付一个"少了一半内容"的成品。

实测（改造前，_probe_long.py）：147 字整段合成，识别 CER 41.5%，秒/字从 0.24
掉到 0.15 —— 整块跳掉了「可是你从来不肯站出来」「我等了那么久」「我连你的影子
都没再见过」。切到 <=28 字再拼回来，CER 8.2%。

================================================================================
判据的真在哪
================================================================================

A 段算得快，但只证明"切分器自己自洽"；
D 段**跑产品的真入口（studio.speak_role）**，把成品交给 faster-whisper 识别、
跟原文比字错误率 —— 这是唯一能证明"用户真能听到完整台词"的东西。
E 段证明"重试那条路真的会走"（用一个故意设高的基准把每段逼成可疑），
它要是假的，段首丢字就只能靠运气了。

负对照（B 段）都是**把尺子弄坏**：关掉 min_len 保护、把预算调到 3 —— 判据
必须在那时变红，否则它测的不是规则本身，只是"没出错"。

用法（GSV runtime 或 venv 都行；本脚本只用标准库，合成走子进程）：
    <python> -s verify_speech_long.py
"""
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import studio  # noqa: E402

GSV = (r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版"
       r"\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0")
PY = os.path.join(GSV, "runtime", "python.exe")
STAGES = os.path.join(HERE, "stages")
WORK = os.path.join(HERE, "_speechlong")
def pick_role():
    """★ 不能写死角色 id，权重名也不能拼出来。

    这里原本是 `ROLE = "rfdb2828c"` + `CKPT = .../ROLE + "-e150.ckpt"`。
    2026-10-18 清掉那批角色之后，这个 id 已经不在库里，而且就算换对了角色，
    权重也不一定叫 e150（大帝现在是 e60）⇒ 两条一起红，都不是产品问题。
    （跟 `verify_tone` / `verify_train_rounds_ui` 里写死「阿塔尼斯」是同一类失误。）

    ⇒ 每次现挑：素材最多的那个、优先有 GPT 权重的；权重路径**从角色记录里读**，
      不靠拼名字。
    """
    try:
        rs = [r for r in (studio.load_roles().get("roles") or [])
              if not r.get("pending")]
    except Exception:                                             # noqa: BLE001
        return "", ""
    if not rs:
        return "", ""
    with_ckpt = [r for r in rs if (r.get("model") or {}).get("ckpt")]
    r = max(with_ckpt or rs, key=lambda x: len(x.get("segments") or []))
    return (r.get("id") or ""), ((r.get("model") or {}).get("ckpt") or "")


ROLE, _CKPT_REL = pick_role()
S2G = "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth"
CKPT = os.path.join(GSV, *_CKPT_REL.split("/")) if _CKPT_REL else ""
WHISPER = os.path.join(HERE, "models", "faster-whisper-medium")

import tone  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    # ★ detail 用 str() 兜一层，并且**不要**用 `if detail` 判断：
    #   0.0 / 0 都是 falsy，会把"量到 0"这个关键信息吞掉（正是本项目踩过的
    #   `or 0` 类假绿）。传错类型（float/dict）也不该让整个门禁崩掉 ——
    #   崩掉连汇总行都没有，比判红难查一个量级（本段刚踩过）。
    txt = "" if detail is None or detail == "" else "  | " + str(detail)
    print(("  PASS  " if cond else "! FAIL  ") + name + txt, flush=True)


def sec(t):
    print("\n=== %s ===" % t, flush=True)


# 一段 100+ 字的台词：长到必须切，又长到"不切必然出事"（实测 CER 41.5%）
LONG = ("我知道你一直都在看着我。从很久以前就是这样，从来没有变过。"
        "可是你从来不肯站出来，哪怕只有一次，哪怕只是一句话也好。"
        "我等了那么久，等到连自己都快忘了在等什么，等到所有的人都走了，"
        "只剩下我一个人还站在原地。你说过会回来，说过天亮之前一定赶到，"
        "说过这场仗打完我们就走，可是天亮了一次又一次，战场上只剩下风声，"
        "我连你的影子都没再见过。")

PUNCT = "，,。.、！!？?；;：:…—‘’“”\"'()（）《》　 \t\n"
# Whisper 的中文输出偶尔是繁体（实测把「着」写成「著」），那不是丢字，是识别口径。
# 不归一的话，一次全绿的门禁会被一个字判红，然后我就会去改阈值（把尺子调歪）。
FANTI = {"著": "着", "說": "说", "過": "过", "們": "们", "來": "来", "這": "这",
         "麼": "么", "個": "个", "時": "时", "會": "会", "為": "为", "沒": "没",
         "還": "还", "讓": "让", "聽": "听", "覺": "觉", "經": "经", "對": "对",
         "樣": "样", "後": "后", "現": "现", "點": "点", "遠": "远", "裡": "里"}


def norm(t):
    s = "".join(ch for ch in (t or "") if ch not in PUNCT)
    return "".join(FANTI.get(c, c) for c in s)


def lev(a, b):
    if a == b:
        return 0
    m, n = len(a), len(b)
    if not m:
        return n
    if not n:
        return m
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i] + [0] * n
        ai = a[i - 1]
        for j in range(1, n + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (0 if ai == b[j - 1] else 1))
        prev = cur
    return prev[n]


def asr_text(wav, tag):
    """跑 faster-whisper，返回归一化后的识别文本（失败返回 None）。"""
    out = os.path.join(WORK, "asr_%s.json" % tag)
    if not os.path.exists(out):
        subprocess.run([PY, "-s", os.path.join(STAGES, "lyrics_asr.py"),
                        "--wav", wav, "--out", out, "--model", WHISPER,
                        "--lang", "zh", "--max-sec", "600"],
                       cwd=GSV, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if not os.path.exists(out):
        return None
    j = json.load(open(out, encoding="utf-8"))
    return norm((j.get("line") or "").replace("|", ""))


def tts_batch(tasks, tag):
    """直接调合成器（E 段那些"构造出来的极端场景"走这里）。"""
    tj = os.path.join(WORK, "tasks_%s.json" % tag)
    rj = os.path.join(WORK, "result_%s.json" % tag)
    json.dump(tasks, open(tj, "w", encoding="utf-8"), ensure_ascii=False)
    ref_wav = (studio.get_role(ROLE).get("ref") or {}).get("file")
    ref_text = (studio.get_role(ROLE).get("ref") or {}).get("text", "")
    rw = os.path.join(HERE, "data", "roles", ROLE, "slices", ref_wav or "")
    p = subprocess.run([PY, "-s", os.path.join(STAGES, "tts_batch.py"), GSV,
                        CKPT, S2G, rw, ref_text, "中文", tj, rj],
                       cwd=GSV, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    assert os.path.exists(rj), "tts_batch 没产出：%s" % (p.stdout or "")[-400:]
    return json.load(open(rj, encoding="utf-8")), p.stdout or ""


def main():
    os.makedirs(WORK, exist_ok=True)
    print("门禁：长台词不许乱（角色 %s）" % ROLE)

    # ============================== A. 切分器 ==============================
    sec("A. 切分器（纯逻辑）")
    segs = tone.split_segments(LONG)
    print("      原文 %d 字 -> %d 段：%s"
          % (len(LONG), len(segs), "/".join(str(len(s["text"])) for s in segs)))
    check("长台词被切开了（不是一句硬塞）", len(segs) >= 3, "%d 段" % len(segs))
    check("★ 切分不丢字（拼回去和原文一字不差）",
          norm("".join(s["text"] for s in segs)) == norm(LONG),
          "%d 字 vs %d 字" % (len(norm("".join(s["text"] for s in segs))), len(norm(LONG))))
    mx = max(len(s["text"]) for s in segs)
    check("每段不超过预算（允许 min_len 的溢出）", mx <= tone.SEG_BUDGET + tone.SEG_MIN,
          "最长 %d 字（预算 %d）" % (mx, tone.SEG_BUDGET))
    tails = [len(tone._clauses(s["text"])[-1]) for s in segs
             if len(tone._clauses(s["text"])) >= 2]
    check("★ 没有一段以短子句**收尾**（实测这种形状会被吞掉半句）",
          all(t >= tone.SEG_MIN for t in tails) if tails else True,
          "最短尾子句 %d 字" % (min(tails) if tails else 0))
    check("★ 带标点的短句会被拆成独立段（实测这个形状能完整念出）",
          any(len(s["text"]) < tone.SEG_MIN for s in segs)
          and all(s["text"][-1:] in (tone._END_PUNCT + tone._MID_PUNCT)
                  for s in segs if len(s["text"]) < tone.SEG_MIN),
          "最短段 %d 字" % min(len(s["text"]) for s in segs))
    check("段尾标点 -> 气口长度对得上",
          tone.gap_for("你好。") == tone.GAP_END
          and tone.gap_for("你好，") == tone.GAP_MID
          and tone.gap_for("你好") == tone.GAP_CUT)
    hard = tone.split_segments("啊" * 70)
    check("没有标点的长串会被硬切",
          len(hard) >= 2 and max(len(s["text"]) for s in hard) <= tone.SEG_BUDGET,
          "%d 段" % len(hard))
    check("纯标点/空白不算台词", tone.split_segments("  。  ") == [])
    check("短句原样返回（不该被切开也不该被吞）",
          [s["text"] for s in tone.split_segments("你好。")] == ["你好。"])
    check("『孤儿尾』被并回上一段",
          len(tone.split_segments("我一直在等你回来。好。")) == 1,
          str(tone.split_segments("我一直在等你回来。好。")))
    check("★ 预算调大就等于不切（负对照的基准）",
          len(tone.split_segments(LONG, budget=100000)) == 1)

    # ============================ B. 负对照 ============================
    sec("B. 负对照：把尺子弄坏，它必须报警")
    off = tone.split_segments(LONG, budget=28, min_len=0)
    off_tails = [len(tone._clauses(s["text"])[-1]) for s in off
                 if len(tone._clauses(s["text"])) >= 2]
    check("★ 尺子自检：关掉拆段规则后，「以短子句收尾的段」真的会出现",
          bool(off_tails) and min(off_tails) < tone.SEG_MIN,
          "最短尾子句 %d 字" % (min(off_tails) if off_tails else 0))
    tiny = tone.split_segments(LONG, budget=6)
    check("★ 尺子自检：把预算压到 6，段数必须成倍变多",
          len(tiny) >= len(segs) * 2, "%d 段 vs %d 段" % (len(tiny), len(segs)))
    drop = tone.split_segments("你好。世界。", budget=100000)
    check("★ 尺子自检：『预算调大就不切』这条对短文本同样成立", len(drop) == 1)

    # ============================ C. 源码对账 ============================
    sec("C. 源码对账（参数只有一个来源）")
    src_s = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    src_t = open(os.path.join(STAGES, "tts_batch.py"), encoding="utf-8").read()
    check("产品真的用了切分器（studio 调 tone.split_segments）",
          "tone.split_segments(" in src_s)
    check("每段多次合成的开关存在且 >= 2",
          "SPEAK_TAKES = %d" % studio.SPEAK_TAKES in src_s and studio.SPEAK_TAKES >= 2,
          "SPEAK_TAKES=%s" % studio.SPEAK_TAKES)
    check("秒/字基准的校准系数存在（素材比产物慢 11%，不校正会全判可疑）",
          0.5 < studio.SPEAK_SPC_CALIB <= 1.0, "calib=%s" % studio.SPEAK_SPC_CALIB)
    check("studio 会给重试准备**另一段**参考（alt_refs）",
          'task["alt_refs"] = alts' in src_s and "def _alt_refs(" in src_s
          and "按每秒字数从快到慢" in src_s)
    check("★ 重试换的参考要**往说得快的方向**挑（实测慢参考救不回来）",
          "fast_first" in src_s and "1.0 / sr" in src_s)
    check("studio 传了 join/gap（段要拼回一句）",
          'task["join"] = joinp' in src_s and 'task["gap"] = sg["gap"]' in src_s)
    check("合成器认 takes / base_spc / alt_refs / join",
          all(k in src_t for k in ('task.get("takes")', 'task.get("base_spc")',
                                  'task.get("alt_refs")', 'task.get("join")')))
    check("合成器有『可疑就再试』的判定（trust）", "def trust(" in src_t)
    check("★ 兼容：不传 takes 就只跑一次（老调用方行为不变）",
          'int(task.get("takes") or 1)' in src_t)
    check("★ 两次差得不多时用第一次（同一句写两遍不该听起来不一样）",
          "<= 0.08" in src_t)

    # ======================= D. 真跑产品路径 =======================
    sec("D. 真跑：studio.speak_role 合成一段长台词，再识别回来对账")
    role_bak = os.path.join(WORK, "roles_backup.json")
    roles_json = studio.ROLES_JSON
    shutil.copy2(roles_json, role_bak)
    final_dir = os.path.join(HERE, "data", "roles", ROLE, "final")
    before = set(os.listdir(final_dir)) if os.path.isdir(final_dir) else set()
    t_start = time.time()
    job = {"id": "vs_speechlong", "role": ROLE, "kind": "speak", "title": "门禁",
           "state": "running", "stage": "", "pct": 0, "dir": WORK,
           "_logfile": os.path.join(WORK, "job.log"), "steps": [], "_step_idx": -1}
    out_wav = None
    hyp = None
    err = None
    want = norm(LONG)
    try:
        try:
            r = studio.speak_role(job, ROLE, [{"raw": LONG}])
        except Exception as e:                     # noqa: BLE001
            err, r = e, []
        check("★ 产品路径跑通（长台词不该整句失败）", err is None,
              (str(err)[:180] if err else ""))
        print("      speak_role 返回 %d 条结果" % len(r))
        joins = [x for x in r if x.get("join_path")]
        segs_n = max([x.get("seg_count") or 1 for x in r] or [1])
        check("★ 一句话被切成多段合成（产品路径真的切了）", segs_n >= 3,
              "%d 段" % segs_n)
        check("★ 段被拼回**一个**成品文件",
              len(set(x.get("join_path") for x in joins)) == 1,
              "%d 组" % len(set(x.get("join_path") for x in joins)))
        if joins:
            out_wav = joins[0]["join_path"]
        check("成品文件真的在盘上", bool(out_wav) and os.path.exists(out_wav),
              os.path.basename(out_wav or "-"))
        check("每段最多只跑 SPEAK_TAKES 次合成",
              all((x.get("takes_used") or 1) <= studio.SPEAK_TAKES for x in r),
              str(sorted(set(x.get("takes_used") or 1 for x in r))))
        # 产物清单 + 日志
        role2 = studio.get_role(ROLE)
        outs = [x for x in (role2.get("outputs") or []) if x.get("kind") == "speech"]
        seg_outs = [x for x in outs if x.get("wav") == out_wav]
        check("★ 产物清单里是**拼好的那一句**，不是一堆段",
              len(seg_outs) == 1 and (seg_outs[0].get("segs") or 1) >= 3,
              "segs=%s" % (seg_outs[0].get("segs") if seg_outs else "-"))
        logtxt = open(job["_logfile"], encoding="utf-8").read() if os.path.exists(
            job["_logfile"]) else ""
        check("日志里说清了「切成几段」（用户要知道发生了什么）",
              "切成" in logtxt and "段合成后拼回一句" in logtxt)

        # ★ 识别成品、跟原文比 —— 这一关唯一能证明"用户真能听到完整台词"的判据
        if out_wav and os.path.exists(out_wav):
            hyp = asr_text(out_wav, "pos")
        if hyp is None:
            check("识别成品台词（faster-whisper）", False, "识别失败")
        else:
            cer = lev(want, hyp) / max(1, len(want))
            print("      原文 %d 字 | 识别 %d 字 | CER %.1f%%"
                  % (len(want), len(hyp), cer * 100))
            print("      识别：%s" % hyp)
            check("★ 长台词念完整了（字错误率 <= 15%）", cer <= 0.15,
                  "CER %.1f%%" % (cer * 100))
            check("★ 没有整块跳句（识别长度不少于原文的 85%）",
                  len(hyp) >= len(want) * 0.85, "%d / %d" % (len(hyp), len(want)))
    finally:
        shutil.copy2(role_bak, roles_json)
        # 门禁产生的音频**移走**不删（移动不是删除，不受批量删除钩子管），
        # 免得污染角色的成品目录 —— 留着的话下一轮门禁读到的产物会串味。
        moved = os.path.join(WORK, "moved")
        os.makedirs(moved, exist_ok=True)
        for fn in set(os.listdir(final_dir)) - before:
            src = os.path.join(final_dir, fn)
            if os.path.isfile(src):
                shutil.move(src, os.path.join(moved, fn))
        segd = os.path.join(final_dir, "_seg")
        if os.path.isdir(segd):
            for fn in os.listdir(segd):
                shutil.move(os.path.join(segd, fn), os.path.join(moved, "seg_" + fn))

    # ============ D2. 负对照：不切分（老路子）—— 判据必须能红 ============
    sec("D2. 负对照：整段直接交给模型（改造前的老路子）")
    negp = os.path.join(WORK, "neg_whole.wav")
    tts_batch([{"text": LONG, "out": negp}], "N")
    hypn = asr_text(negp, "neg")
    if hypn is None:
        check("负对照（整段合成）能识别", False, "识别失败")
    else:
        cern = lev(want, hypn) / max(1, len(want))
        print("      整段版：识别 %d 字 | CER %.1f%%" % (len(hypn), cern * 100))
        print("      识别：%s" % hypn)
        check("★ 负对照：整段合成确实会乱（CER >= 20%）—— 说明这把尺子抓得住老毛病",
              cern >= 0.20, "CER %.1f%%" % (cern * 100))
        if hyp is not None:
            cer2 = lev(want, hyp) / max(1, len(want))
            check("★ 切分版比整段版好一个量级（不到一半）", cer2 <= cern / 2.0,
                  "%.1f%% vs %.1f%%" % (cer2 * 100, cern * 100))

    # ======================= E. 重试链路 =======================
    sec("E. 重试链路真的会走（把基准故意设高，逼每一段都『可疑』）")
    ref_file = (studio.get_role(ROLE).get("ref") or {}).get("file") or ""
    ref_dir = os.path.join(HERE, "data", "roles", ROLE, "slices")
    text1 = "我知道你一直都在看着我。从很久以前就是这样，从来没有变过。"
    # ★ 备选必须**用产品自己的挑法**（studio._alt_refs）拿，不许在这里手写过滤条件。
    #   历史教训（2026-10-18）：这行原来是手写的
    #       `if ... and 3.0 <= float(s.get("dur") or 0) <= 10.0: alt = s`
    #   而产品 _alt_refs 当时用的是 1.0~14.0 —— 比下游 GPT-SoVITS 的硬要求宽得多，
    #   于是把大帝库里一段 **1.60 秒**的切片选成了备选，真机上"换参考重试"必然
    #   抛 OSError（参考音频在3~10秒范围外），长句丢字唯一的兜底等于不存在。
    #   可这一关**用自己手挑的合法段去跑**，永远绿。
    #   ⇒ 门禁一旦比产品"更聪明"，它测的就只是自己的假设，不再是产品。
    _role_e = studio.get_role(ROLE)
    _alts_e = studio._alt_refs(_role_e, studio.role_dir(ROLE), None, None, "", n=1)
    alt = dict(_alts_e[0]) if _alts_e else None
    if alt is None:
        check("E 段前置：产品自己挑得出一条备选参考", False, "studio._alt_refs 返回空")
    else:
        common = {"out": os.path.join(WORK, "E_take.wav"), "base_spc": 0.9,
                  "ref_wav": os.path.join(ref_dir, ref_file),
                  "ref_text": (_role_e.get("ref") or {}).get("text", ""),
                  "alt_refs": [alt]}
        r2, _o2 = tts_batch([dict(common, text=text1, takes=studio.SPEAK_TAKES)], "E2")
        rec = r2[0] if r2 else {}
        check("★ 基准设高后，这段时间真的被合成了两次",
              (rec.get("takes_used") or 1) >= 2, "takes_used=%s" % rec.get("takes_used"))
        check("★ 重试确实**换了参考**（不是原地重掷同一个骰子）",
              len(set(t.get("ref") for t in (rec.get("tries") or []))) >= 2,
              "用了 %s" % sorted(set(t.get("ref", "")[-14:]
                                     for t in (rec.get("tries") or []))))
        check("每一次尝试的指标都记下来了（事后能对账）",
              isinstance(rec.get("tries"), list) and len(rec.get("tries") or []) >= 2,
              "%d 次" % len(rec.get("tries") or []))
        r1, _o1 = tts_batch([dict(common, text=text1, out=os.path.join(WORK, "E_one.wav"),
                                  takes=1)], "E1")
        rec1 = r1[0] if r1 else {}
        check("★ 负对照：不给 takes 就只跑一次（takes_used 这个指标不是恒真的）",
              rec1.get("takes_used") is None, "takes_used=%s" % rec1.get("takes_used"))

    # ============ F. 参考音频合法性（3~10 秒是下游的硬要求） ============
    sec("F. 参考音频合法性：产品挑出来的参考必须能被 GPT-SoVITS 接受")
    # 这一关为什么必须有（2026-10-18，跑说话轮数 A/B 时暴露的真 bug）：
    #   GPT-SoVITS 在 TTS_infer_pack/TTS.py:917 对参考音频是**硬性** 3~10 秒
    #   （16k 下 48000~160000 采样点），超了直接 raise，**没有自动裁剪**。
    #   而产品里两个出口都比它宽：
    #     · pick_reference：`dur < 2.0` 才排除，3~10 秒只是打分降权；
    #     · _alt_refs     ：`1.0 <= dur <= 14.0` 才算候选。
    #   后果不是"效果差一点"，是整条链路抛异常：长台词每段固定合成两遍，
    #   第 1 遍可疑 → 第 2 遍换参考 → 换到 1.60 秒那段 → OSError；
    #   而"换参考"是长句丢字唯一的兜底（八个旋钮全试过，只有它有用）。
    #   ⇒ 兜底等于不存在。这一关就钉这里。
    # 用 `or {}`：万一这个角色被删了，本段要**判红**而不是整关崩掉
    # （没汇总行的门禁比红的门禁更难查）。
    _f_role = studio.get_role("ra500fb92") or {}
    _f_rd = studio.role_dir("ra500fb92")
    _f_slices = os.path.join(_f_rd, "slices")

    check("护栏区间对齐下游（3~10 秒，不是我们自己拍脑袋的数）",
          (studio.REF_MIN_SEC, studio.REF_MAX_SEC) == (3.0, 10.0),
          "%s~%s" % (studio.REF_MIN_SEC, studio.REF_MAX_SEC))

    _rd_ok = getattr(studio, "ref_dur_ok", None)
    if not callable(_rd_ok):
        check("★ studio.ref_dur_ok 存在（护栏必须能被复用、被测量）", False, "缺这个函数")
        _rd_ok = lambda d: False          # noqa: E731  让后面别连锁崩
    else:
        _cases = [(1.60, False), (2.99, False), (3.00, True), (5.0, True),
                  (10.00, True), (10.01, False), (14.0, False), (None, False)]
        _mis = [c[0] for c in _cases if bool(_rd_ok(c[0])) != c[1]]
        check("★ 时长护栏边界正反都对（1.60 拦 / 3.00 放 / 10.01 拦 / None 拦）",
              not _mis, "错判 %s" % _mis)

    _f_segs = [s for s in (_f_role.get("segments") or [])
               if (s.get("text") or "").strip()]
    _f_illegal = [s for s in _f_segs if not _rd_ok(float(s.get("dur") or 0))]
    # ★ 空真防护：库里必须**真的**存在非法段。否则下面两条只是在空集上绿，
    #   换成另一个角色（全是合法段）照样绿 —— 那种绿证明不了任何事。
    check("★ 空真防护：这个角色确实存在 3~10 秒之外的切片（否则下面两条测的是空集）",
          len(_f_illegal) > 0, "%d/%d 段非法" % (len(_f_illegal), len(_f_segs)))

    _pr_dur = None
    try:
        _pp, _ = studio.pick_reference(_f_role, _f_slices)
        _pr_dur = studio.wav_seconds(_pp)
    except Exception as e:
        _pr_dur = "%s: %s" % (type(e).__name__, str(e)[:60])
    # ⚠ detail 必须是 str：check() 里是 `"  | " + detail`，传 float 会 TypeError
    #   （本段刚踩过：_pr_dur 是 float，整个门禁崩在打印那一行，比判红更难查）。
    _pr_show = ("%.2f 秒" % _pr_dur) if isinstance(_pr_dur, float) else str(_pr_dur)
    check("★ 主参考（pick_reference）挑出来的时长必须合法",
          isinstance(_pr_dur, float) and _rd_ok(_pr_dur), _pr_show)

    _f_alts = studio._alt_refs(_f_role, _f_rd, None, None, "", n=6)
    _alt_durs = [studio.wav_seconds(a["ref_wav"]) for a in _f_alts]
    check("★ 备选参考（_alt_refs）每一条时长都合法",
          bool(_alt_durs) and all(_rd_ok(x) for x in _alt_durs),
          ", ".join("%.2f" % x for x in _alt_durs) or "一条都没给")
    _illegal_names = set(os.path.basename(s["file"]) for s in _f_illegal)
    _leaked = [os.path.basename(a["ref_wav"]) for a in _f_alts
               if os.path.basename(a["ref_wav"]) in _illegal_names]
    check("★ 负对照：库里那些非法段**没有**混进备选",
          not _leaked, "混进了 %s" % _leaked)

    # ★ 变异负对照：把护栏换成"永远放行"，_alt_refs 必须立刻吐出非法段。
    #   这条才真正证明上面几条的绿是**护栏挣来的**，而不是"数据恰好干净"。
    _orig_guard = studio.ref_dur_ok
    try:
        studio.ref_dur_ok = lambda d: True
        _alts2 = studio._alt_refs(_f_role, _f_rd, None, None, "", n=6)
        _durs2 = [studio.wav_seconds(a["ref_wav"]) for a in _alts2]
        _mut_hit = any(not _orig_guard(x) for x in _durs2)
    finally:
        studio.ref_dur_ok = _orig_guard
    check("★ 变异负对照：护栏放宽后立刻选到非法段（说明绿是护栏挣来的）",
          _mut_hit, "放行后选到 %s" % [round(x, 2) for x in _durs2])

    # ---- 源码对账：长句重试那条路不许再被一个 None 带崩 ----
    _tb_src = open(os.path.join(STAGES, "tts_batch.py"), encoding="utf-8").read()
    # ⚠ 必须定位到**赋值语句** `rec["tries"] = [`，不能用裸的 `rec["tries"]`：
    #   裸串会先命中源码里解释这件事的**注释**，于是判据读到一段说明文字、
    #   找不到 `is not None` 就判红 —— 第一版就是这么误红的（尺子自己歪了，
    #   和产品无关）。判据要找的是代码，不是提到代码的地方。
    _ti = _tb_src.find('rec["tries"] = [')
    _tseg = _tb_src[_ti:_ti + 700] if _ti >= 0 else ""
    # 背景：tries 里除了成功记录，异常分支还会塞 {"m": None, ...}；
    # 原代码 `[dict(t["m"], ref=t["ref"]) for t in tries]` 遇到它就
    # TypeError: 'NoneType' object is not iterable —— 整个任务连成品都不写。
    _tfail = ("找不到赋值语句 rec[\"tries\"] = [" if _ti < 0
              else _tseg.strip().splitlines()[0][:80])
    check("★ 源码对账：记录 tries 时对 m=None 有防护（否则长句重试整条崩）",
          _ti >= 0 and "is not None" in _tseg, _tfail)
    check("tts_batch 自己也有一层参考时长护栏（纵深防御，防历史任务里的脏备选）",
          "def ref_seconds" in _tb_src and "REF_MIN_SEC" in _tb_src)
    # gpt_path 必须在 import 推理模块**之前**设好，否则 inference_webui 会按
    # weight.json 里"上次用过的"路径先加载一次（白加载 + 那文件一没了就整个挂）。
    _i_env = _tb_src.find('os.environ["gpt_path"]')
    _i_imp = _tb_src.find("inference_webui import")
    check("★ tts_batch 在 import 推理模块**之前**就设好了 gpt_path"
          "（否则加载的是 weight.json 里的旧权重，旧文件一删就整体瘫痪）",
          _i_env >= 0 and _i_imp > _i_env,
          "env@%d / import@%d" % (_i_env, _i_imp))

    # ============ G. 子进程失败不许静默复用旧结果 ============
    sec("G. 合成失败时不许把上一次的成品当成这次的结果")
    # 为什么单独立一段（2026-10-18，本轮最坏的一类假绿）：
    #   speak_role 原来是
    #       rc, out = run(...)
    #       results = json.load(open(rj)) if os.path.exists(rj) else []
    #   tts_batch 失败时不写新的 result.json ⇒ 这里的 `if os.path.exists` 成立
    #   ⇒ **读到上一次的成品** ⇒ 上层看到"成功"、拿到成品路径、算出指标，
    #   而用户听到的是上一次的音频。这一步什么都没干，报表却全绿。
    #
    #   实测后果：说话轮数 A/B 的 B 臂三次"测量"全部指向 A 臂的 wav，
    #   "20 轮 vs 60 轮"看起来一模一样（CER 极差 0.0），实际上 20 轮一次都没测到。
    #   同一个 job 目录被复用时（探针重跑、同一关连跑两遍）必然踩到。
    #
    #   注：`verify_ckpt` 里这个坑**早就修过**（注释写着"坏权重就蒙混过关了"），
    #   speak_role 那处漏了 —— 同一个缺陷两个出口，只修了一个。
    _sk = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    _si = _sk.find("def speak_role")
    _sj = _sk.find("\ndef ", _si + 1)
    _sbody = _sk[_si:_sj] if _si >= 0 and _sj > _si else ""
    check("★ speak_role 跑之前先删旧结果文件（否则失败时读到上一次的成品）",
          "os.remove(rj)" in _sbody)
    check("★ speak_role 检查了合成子进程的退出码，失败就报错",
          "if rc != 0:" in _sbody and "raise Fail" in _sbody)
    # 只"存在"不够，**顺序**也要对：检查必须在读取之前，否则还是先读了旧结果。
    # ⚠ 比位置之前必须**剔掉注释行**：源码里讲这件事的注释本身就写着
    #   `json.load(open(rj))`，裸搜会命中注释、把顺序判反（第一版就这么误红了）。
    _sbody_code = "\n".join(ln for ln in _sbody.splitlines()
                            if not ln.lstrip().startswith("#"))
    _i_rc = _sbody_code.find("if rc != 0:")
    _i_load = _sbody_code.find("json.load(open(rj")
    check("★ 退出码检查在读取结果**之前**（顺序也要对，不能只是存在）",
          _i_rc >= 0 and _i_load > _i_rc, "rc@%d / load@%d" % (_i_rc, _i_load))

    # ============================== 汇总 ==============================
    print("\n" + "-" * 60)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - " + b)
    print("EXIT=%d" % (1 if BAD else 0))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
