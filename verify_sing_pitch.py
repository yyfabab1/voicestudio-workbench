# -*- coding: utf-8 -*-
r"""门禁：唱歌的音高不许「设了一个会翻八度的档还不知道」。

老板的原话：「唱歌也是越唱越差」。

================================================================================
这一关在防什么
================================================================================

**错了不会让任何既有判据变红**，也不会报错 —— 整首歌照样唱完、成品照样出得来、
混音照样对齐。唯一的症状是**成品里有一部分帧翻到了相邻八度上**（听感：飘、毛、像
两个人在合唱），而 `describe_qc` 只会在**唱完之后**补一句"有 18% 的帧在八度上跳"。
那时整首 RVC 已经烧掉了十几分钟。这是本项目最贵的一类"静默报废"：
钱（机时）花了，东西不能用。

================================================================================
实测（改造前的历史数据，见 studio.py「音高体检」段的完整出处）
================================================================================

同一首歌（Three Days Grace - Pain，202.9 秒）、**同一个 --input**、同一个模型
ra500fb92.pth、同一套参数（index-rate 0.50 / rms-mix 0.25 / protect 0.33）、
同一段质检窗口 —— **只改 --pitch**：

        pitch  +0  → 宽松吻合 93.0%   八度错  3.4%
        pitch -12  → 宽松吻合 81.2%   八度错 17.2%     ← 整八度这一档炸了
        pitch  +3  → 宽松吻合 96.8%   八度错  1.4%

所以**不是代码随时间退化**。但也不能推广成"−12 一定坏"：同样 −12，
丹阳子（193.3s）只有 2.1%，登阶（200.7s）是 18.4% / 17.2%（两次可复现）。
同一个角色、同一个音高设定，一首 2%、一首 18% ⇒ **逐首不同，只能实测**。
这就是「音高体检」（audition_pitch）存在的全部理由。

★ **口径警告（踩过一次，务必别重犯）**：上面那些数都是**产品单窗**质检报的 ——
  它只取一段（`_qs=(总长-30)*0.35`），同一份成品换一段能报 2.5% 也能报 21.5%
  （登阶那一版七窗实测）。所以单窗数**能说明"会不会炸"，不能当"整首多坏"的基准**。
  整曲真值要用 `studio.qc_singing_whole`（多窗等距铺开）算，见 `_probe_whole_qc_truth.py`。
  本关 B 段那条「登阶 18.4% 该改判到 0」是**分支样例**（输入是手写表），
  不是真值断言；D 段真跑只断言"每档都量出了数"，**不押具体真值** —— 就是为避开这个坑。

================================================================================
判据的真在哪
================================================================================

A 段只是源码对账（快，但只证明"东西都在"）；
B 段把判定逻辑逐条分支走一遍（真跑 RVC 走不了这么多分支）；
C 段是**尺子自检**：把阈值旋到底/顶到天，判定**必须跟着变** ——
  不变就说明那条判据根本没在判，只是恰好没出错；
D 段**真跑** audit_pitch：造一段 100 秒的干声（用角色自己的切片拼，不依赖任何
  缓存残留），真的为每档音高起一次 RVC + 质检，断言每一档都量出了数；
E 段**跑产品真入口** studio.sing_role，用一个"改判"的桩喂进去，断言
  **整首真的按改判后的音高唱了**、并且改判前后两个音高都写进了产物参数里
  （以前产物只存质检数、不存参数，导致 16 版成品要靠人工对日志才能归因）。

用法（venv 或托管 python 都行；RVC/ffmpeg 都走子进程）：
    <python> -s verify_sing_pitch.py
"""
import json
import inspect
import os
import re
import shutil
import sys
import time
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

WORK = os.path.join(HERE, "_pitchaud")
ROLE = "ra500fb92"
PITCH = -12

import studio  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + detail) if detail else ""), flush=True)


def sec(t):
    print("\n=== %s ===" % t, flush=True)


def mk_job(tag, wd):
    return {"id": "vs_" + tag, "role": ROLE, "kind": "sing", "title": "门禁",
            "state": "running", "stage": "", "pct": 0, "dir": wd,
            "_logfile": os.path.join(wd, "job.log"), "steps": [], "_step_idx": -1}


def build_test_vocal(out_wav, target_secs=100.0):
    """用角色自己的切片拼一段够长的干声。

    ★ 为什么不用 data/cache/sep 里的现成干声：那是**运行时缓存**，会被清
      （实测 18a93a7e6ae28125 就已经没了）。门禁依赖它就会变成"今天绿明天红"。
      切片（data/roles/<rid>/slices）是角色自己的素材，属于项目数据，稳定。
    全部切片都是 mono/16bit/32kHz，直接首尾相接即可，不需要重采样。
    """
    import glob
    fs = sorted(glob.glob(os.path.join(HERE, "data", "roles", ROLE, "slices", "*.wav")))
    if not fs:
        return False, "角色没有切片素材"
    got = 0.0
    with wave.open(out_wav, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(32000)
        for f in fs:
            try:
                r = wave.open(f, "r")
                if (r.getnchannels(), r.getsampwidth(), r.getframerate()) != (1, 2, 32000):
                    r.close()
                    continue
                raw = r.readframes(r.getnframes())
                r.close()
            except Exception:
                continue
            w.writeframes(raw)
            got += len(raw) / 2.0 / 32000.0
            if got >= target_secs:
                break
    return (got >= studio.AUDITION_MIN_SONG), "拼出 %.0f 秒" % got


def main():
    os.makedirs(WORK, exist_ok=True)
    print("音高体检门禁 | 角色 %s | 设定音高 %+d" % (ROLE, PITCH), flush=True)

    # ----------------------------- A. 源码对账 -----------------------------
    sec("A. 源码对账（常量 / 函数 / 接线 / 产物字段都在）")
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("常量 AUDITION 存在且默认为真", studio.AUDITION is True,
          "AUDITION=%r" % (studio.AUDITION,))
    check("体检段长度合理（5~60 秒）", 5.0 <= studio.AUDITION_SECS <= 60.0,
          "%.1f 秒" % studio.AUDITION_SECS)
    check("短歌不体检的界线存在", studio.AUDITION_MIN_SONG > 0,
          "%.0f 秒" % studio.AUDITION_MIN_SONG)
    # 体检口径的**好档上界 8.3%**、**坏档下界 13.2%** ⇒ 阈值必须夹在两簇之间、且不贴任一侧。
    # （收窄到 0.085~0.125 是有意的：改这个数就是在改整条功能的灵敏度，必须被门禁看见。）
    check("八度错阈值夹在实测两簇之间且不贴边（好档上界 8.3% ~ 坏档下界 13.2%）",
          0.085 < studio.AUDITION_OCTAVE_BAD < 0.125,
          "%.1f%%" % (studio.AUDITION_OCTAVE_BAD * 100))
    check("改判后的吻合落差限制存在", 0 < studio.AUDITION_MATCH_DROP < 0.2,
          "%.2f" % studio.AUDITION_MATCH_DROP)
    check("软提示的'明显更稳'比例存在（0~1 之间）", 0 < studio.AUDITION_BETTER < 1.0,
          "%.2f" % studio.AUDITION_BETTER)
    check("软提示的文字在源码里（不是写了没接）",
          "想更稳可以换它重唱一版对比" in src)
    for fn in ("pick_audition", "audition_pitch", "_audition_candidates",
               "_pick_excerpt", "_montage", "_montage_starts", "_rms_blocks"):
        check("函数 %s 存在且可调用" % fn, callable(getattr(studio, fn, None)))
    check("体检素材是**多段等分取样**（不是挑一段 —— 挑一段会漏判，实测过）",
          int(studio.AUDITION_PARTS) >= 2, "%d 段" % studio.AUDITION_PARTS)
    check("sing_role 里真的调用了体检（不是写了没用）",
          "audition_pitch(job, vocal_wav" in src)
    check("体检改判后真的改了整首用的音高",
          'pitch = int(audition["chosen"])' in src)
    check("改判会**响亮地**告知用户（日志里说改用哪一档）",
          "体检后**改用" in src)
    check("产物里记下体检表", '"audition": audition' in src)
    check("产物里记下改判前的音高（能看出改判前后差别）",
          '"pitch_asked": int(_pitch_asked)' in src)
    check("产物里记下模型名（以前没记，复盘只能翻日志）",
          '"model": os.path.basename(rvc_model or "")' in src)

    # ------------- A2. 大跨度变调必须提醒（2026-10-24 加）-------------
    # 防什么：用户把音高设成 +12（整整一个八度），音高体检还报「这一档没问题」
    # —— 因为体检只量 f0 准不准，而变调对 f0 基本就是纯平移，任何档都能过。
    # 实测见 studio.py 里 PITCH_BAD_FROM 那段注释（高频占比 0.95% → 3.45%）。
    sec("A2. 大跨度变调必须提醒（±12 是实测必然失真的档）")
    check("函数 pitch_shift_warn 存在且可调用",
          callable(getattr(studio, "pitch_shift_warn", None)))
    check("两个阈值存在且就是实测那两个数（半个八度 6 / 一个八度 11）",
          getattr(studio, "PITCH_WARN_FROM", None) == 6
          and getattr(studio, "PITCH_BAD_FROM", None) == 11,
          "warn>=%s bad>=%s" % (getattr(studio, "PITCH_WARN_FROM", None),
                                getattr(studio, "PITCH_BAD_FROM", None)))
    _w = studio.pitch_shift_warn
    check("小跨度（0 / ±5）不打扰用户",
          _w(0) == ("", "") and _w(5)[0] == "" and _w(-5)[0] == "",
          "0→%r +5→%r −5→%r" % (_w(0)[0], _w(5)[0], _w(-5)[0]))
    check("超过半个八度（±6 ~ ±10）给黄牌且文案非空",
          _w(6)[0] == "warn" and _w(-10)[0] == "warn" and bool(_w(6)[1]),
          "+6→%r −10→%r" % (_w(6)[0], _w(-10)[0]))
    check("整整一个八度（±11 / ±12 / ±24）给红牌",
          _w(11)[0] == "bad" and _w(12)[0] == "bad" and _w(-24)[0] == "bad",
          "+12→%r −24→%r" % (_w(12)[0], _w(-24)[0]))
    check("红牌文案必须给出**换角色**这个真解法（只说'会失真'等于没说）",
          "角色" in (_w(12)[1] or ""))
    # ★ 负对照：把坏档线抬到 30（等于关掉红牌），±12 必须不再判红。
    #   没有这条就不知道上面那三句是"真的在看这个数"还是恰好一直成立。
    _bad_bak = studio.PITCH_BAD_FROM
    _neg_lv = None
    try:
        studio.PITCH_BAD_FROM = 30
        _neg_lv = _w(12)[0]     # ★ 必须**现场**取：复原之后再读就又是 bad 了，
    finally:                    #   打印出来的证据会和结论自相矛盾。
        studio.PITCH_BAD_FROM = _bad_bak
    # 复原后证据也不能跟结论打架：这里顺便确认它在正常阈值下确实是 bad
    _back_lv = _w(12)[0]
    check("负对照：坏档线抬到 30 之后 ±12 不再判红（证明这条有牙）",
          _neg_lv != "bad",
          "抬到 30 时 +12→%r；复原回 11 后 +12→%r" % (_neg_lv, _back_lv))
    check("负对照后阈值已复原（不许污染后面的真跑段）",
          studio.PITCH_BAD_FROM == 11, "%s" % studio.PITCH_BAD_FROM)
    check("sing 流程真的调用了它（不是写了没接）",
          "pitch_shift_warn(pitch)" in src)

    _html = open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()
    _js = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
    check("界面有提醒落点：角色页和合唱台各一处",
          'id="pitchWarn"' in _html and 'id="sgPitchWarnBox"' in _html)
    check("两个变调控件改值当天就刷新提醒（不用等提交）",
          "onPitchPick()" in _html and "sgPitchWarn()" in _html)
    check("自定义半音输入框也接上了（它不是 select，容易漏）",
          re.search(r'id="inpPitch"[^>]*oninput="onPitchPick\(\)"', _html)
          is not None or re.search(
              r'id="inpPitch"[\s\S]{0,200}?oninput="onPitchPick\(\)"', _html)
          is not None)
    check("前台三个函数在且互相接线",
          "function pitchRisk(" in _js and "function paintPitchWarn(" in _js
          and "function sgPitchWarn(" in _js)
    check("开局先按当前值刷一次（浏览器会恢复上次那个 ±12）",
          "paintPitchInit" in _js and "paintPitchInit()" in _js)
    _opt12 = re.search(r'<option value="12">([^<]*)</option>', _html)
    check("★ ±12 那个下拉选项不许只写好处，必须写出代价",
          bool(_opt12) and any(k in (_opt12.group(1) if _opt12 else "")
                               for k in ("慎用", "失真", "金属", "电音")),
          (_opt12.group(1) if _opt12 else "找不到 +12 这个选项")[:40])

    # --------------------------- B. 判定逻辑分支 ---------------------------
    sec("B. 判定逻辑：每条分支逐条走（含期望值）")
    P = studio.pick_audition
    # 前三条的期望值直接来自实测真值（见文件头对照表）
    cases = [
        ("Pain：−12 实测 17.2% 该改判，且要挑**离 −12 最近**的达标档（0，不是 +3）",
         [{"pitch": -12, "match": 0.812, "octave": 0.172},
          {"pitch": 0, "match": 0.930, "octave": 0.034},
          {"pitch": 3, "match": 0.968, "octave": 0.014}], -12, 0, True),
        ("丹阳子：−12 实测 2.1% 达标 ⇒ 不许动用户的设定",
         [{"pitch": -12, "match": 0.850, "octave": 0.021},
          {"pitch": 0, "match": 0.840, "octave": 0.050}], -12, -12, False),
        ("登阶：−12 实测 18.4% 该改判到 0",
         [{"pitch": -12, "match": 0.710, "octave": 0.184},
          {"pitch": 0, "match": 0.860, "octave": 0.030},
          {"pitch": 12, "match": 0.800, "octave": 0.060}], -12, 0, True),
        ("对照档也都不达标 ⇒ 维持原设定（不硬换）",
         [{"pitch": -12, "match": 0.60, "octave": 0.20},
          {"pitch": 0, "match": 0.61, "octave": 0.15}], -12, -12, False),
        ("候选达标但整体唱得明显更差 ⇒ 不换",
         [{"pitch": -12, "match": 0.90, "octave": 0.17},
          {"pitch": 0, "match": 0.70, "octave": 0.03}], -12, -12, False),
        ("设定值本来就达标 ⇒ 不动",
         [{"pitch": 0, "match": 0.97, "octave": 0.01},
          {"pitch": -12, "match": 0.81, "octave": 0.17}], 0, 0, False),
        ("设定那一档没量出来 ⇒ 按用户设的走",
         [{"pitch": 0, "match": 0.90, "octave": 0.02}], -12, -12, False),
        ("设定那一档质检失败（None）⇒ 按用户设的走",
         [{"pitch": -12, "match": None, "octave": None},
          {"pitch": 0, "match": 0.90, "octave": 0.02}], -12, -12, False),
        ("候选表是空的 ⇒ 按用户设的走（不许抛异常）", [], -12, -12, False),
        ("就近原则：−12 坏了，−24 与 0 都达标 ⇒ 距离相同时取八度错更低的",
         [{"pitch": -12, "match": 0.70, "octave": 0.18},
          {"pitch": -24, "match": 0.86, "octave": 0.02},
          {"pitch": 0, "match": 0.88, "octave": 0.05}], -12, -24, True),
    ]
    for name, cands, req, exp_p, exp_ch in cases:
        try:
            p, ch, why = P(cands, req)
        except Exception as e:                      # noqa: BLE001
            check(name, False, "抛异常 %s" % e)
            continue
        check(name, p == exp_p and bool(ch) == bool(exp_ch),
              "选用 %+d 改判=%s 期望 %+d/%s（%s）" % (p, ch, exp_p, exp_ch, why[:70]))
    check("理由文字总是非空（用户要看得懂为什么）",
          all(P(c, r)[2].strip() for _, c, r, _, _ in cases))

    # 软提示：设定档**达标**、旁边有档明显更稳 ⇒ 不改判，但理由里必须点出那档。
    # 反过来（旁边那档不够好）⇒ 理由里**不许**出现多余提示（不然每条都提示=没提示）。
    p_soft, ch_soft, why_soft = P([{"pitch": -12, "match": 0.760, "octave": 0.080},
                                   {"pitch": 0, "match": 0.800, "octave": 0.040}], -12)
    check("★ 设定档达标但有档明显更稳 ⇒ 不改判，只在理由里提示那档",
          (p_soft == -12) and (not ch_soft) and ("+0" in why_soft),
          "选用 %+d 改判=%s | %s" % (p_soft, ch_soft, why_soft[:96]))
    p_q, ch_q, why_q = P([{"pitch": -12, "match": 0.850, "octave": 0.030},
                          {"pitch": 0, "match": 0.840, "octave": 0.028}], -12)
    check("★ 旁边那档只略好（不到 0.6 倍）⇒ 不许提示（否则等于每条都提示）",
          (p_q == -12) and (not ch_q) and ("重唱一版对比" not in why_q),
          "选用 %+d | %s" % (p_q, why_q[:96]))
    p_r, _, why_r = P([{"pitch": -12, "match": 0.760, "octave": 0.080},
                       {"pitch": 0, "match": 0.600, "octave": 0.030}], -12)
    check("★ 更稳但那档整体唱得更差（吻合掉超 5 个百分点）⇒ 不许提示",
          (p_r == -12) and ("重唱一版对比" not in why_r),
          "选用 %+d | %s" % (p_r, why_r[:96]))

    cands_neg = studio._audition_candidates(-12)
    check("候选包含用户设定那一档", -12 in cands_neg, str(cands_neg))
    check("候选包含相邻八度 ±12", (0 in cands_neg and -24 in cands_neg), str(cands_neg))
    check("候选不超过绝对值上限",
          all(abs(x) <= studio.AUDITION_MAX_ABS for x in studio._audition_candidates(-19)),
          str(studio._audition_candidates(-19)))

    # --------------------------- C. 尺子自检（负对照）---------------------------
    # ★ 把阈值旋到极端，判定**必须跟着变**。不变 ⇒ 这条判据根本没在判。
    #   注意候选档的 octave 用 **0.000**（不是 0.05）：阈值旋到 0 时，"设定档 0.021 > 0"
    #   已经不达标，若候选也 >0 就同样不达标 ⇒ pool 为空 ⇒ 仍然不改判，
    #   这条自检会**假红**（第一次就写错了）。留一个恰好 0 的达标档才测得到阈值本身。
    sec("C. 尺子自检：把阈值旋到底/顶到天，判定必须跟着变")
    one = [{"pitch": -12, "match": 0.850, "octave": 0.021},
           {"pitch": 0, "match": 0.840, "octave": 0.000}]
    base = P(one, -12)
    check("（基线）丹阳子那档在默认阈值下不改判", base[0] == -12 and not base[1],
          base[2][:60])
    bad_bak = studio.AUDITION_OCTAVE_BAD
    try:
        studio.AUDITION_OCTAVE_BAD = 0.0     # 任何八度错都算"不达标"
        p1, c1, _ = P(one, -12)
        check("★ 阈值旋到 0 ⇒ 同一份数据必须改判（判据真的在判）",
              bool(c1) and p1 != -12, "改用 %+d 改判=%s" % (p1, c1))
        studio.AUDITION_OCTAVE_BAD = 1.0     # 任何八度错都算"达标"
        p2, c2, _ = P([{"pitch": -12, "match": 0.60, "octave": 0.20},
                       {"pitch": 0, "match": 0.90, "octave": 0.02}], -12)
        check("★ 阈值顶到 100% ⇒ 再坏的一档也不许改判（阈值真的在起作用）",
              (not c2) and p2 == -12, "选用 %+d 改判=%s" % (p2, c2))
    finally:
        studio.AUDITION_OCTAVE_BAD = bad_bak
    p3, c3, _ = P(one, -12)
    check("★ 阈值复原后行为回到基线（说明上面两次变化是阈值造成的）",
          (p3, bool(c3)) == (base[0], bool(base[1])),
          "选用 %+d 改判=%s" % (p3, c3))
    drop_bak = studio.AUDITION_MATCH_DROP
    try:
        studio.AUDITION_MATCH_DROP = 10.0    # 等于"完全不卡吻合落差"
        p4, c4, _ = P([{"pitch": -12, "match": 0.90, "octave": 0.17},
                       {"pitch": 0, "match": 0.10, "octave": 0.02}], -12)
        check("★ 放开吻合落差限制 ⇒ 会挑到'不翻八度但唱得很差'的那档（说明这条限制真在拦）",
              bool(c4) and p4 == 0, "选用 %+d 改判=%s" % (p4, c4))
    finally:
        studio.AUDITION_MATCH_DROP = drop_bak

    # ------------------------------ D. 真跑体检 ------------------------------
    sec("D. 真跑 audition_pitch：造 100 秒干声，真的为每档音高起一次 RVC + 质检")
    role = studio.get_role(ROLE)
    model = (role.get("rvc") or {}).get("model") if role else None
    check("角色存在且有 RVC 模型", bool(model), str(model and os.path.basename(model)))
    if not model:
        return summary()
    idx = studio._find_rvc_index(model)
    vocal = os.path.join(WORK, "test_vocal.wav")
    t0 = time.time()
    ok_v, why_v = build_test_vocal(vocal, 100.0)
    check("测试干声拼出来了（用角色自己的切片，不依赖缓存）", ok_v, why_v)
    if not ok_v:
        return summary()

    wd = os.path.join(WORK, "run")
    if os.path.isdir(wd):
        shutil.rmtree(wd, ignore_errors=True)
    os.makedirs(wd, exist_ok=True)
    job = mk_job("pitch", wd)
    t1 = time.time()
    try:
        r = studio.audition_pitch(job, vocal, PITCH, model, idx, 0.50,
                                  rms_mix_rate=0.25, protect=0.33)
    except Exception as e:                          # noqa: BLE001
        check("★ 体检真跑不许抛异常（异常必须自己吞掉、退化成按用户设定唱）", False, str(e)[:160])
        return summary()
    dt = time.time() - t1
    print("      体检用时 %.0f 秒（拼素材 %.0f 秒）" % (dt, t1 - t0))
    check("★ 体检真跑没抛异常", True, "%.0f 秒" % dt)
    check("没被跳过（这段干声比界线长）", not r.get("skipped"),
          str(r.get("skipped") or "正常体检"))
    want_n = len(studio._audition_candidates(PITCH))
    check("每一档候选都试了且都在表里",
          len(r.get("cands") or []) == want_n,
          "%d 档：%s" % (len(r.get("cands") or []),
                        [c.get("pitch") for c in r.get("cands") or []]))
    check("候选里含用户设定那一档",
          PITCH in [c.get("pitch") for c in r.get("cands") or []])
    measured = [c for c in (r.get("cands") or []) if c.get("octave") is not None]
    check("★ 真的量出了数（不是每档都是 None）", len(measured) >= 2,
          "%d/%d 档量出八度错：%s"
          % (len(measured), want_n,
             [(c["pitch"], round(c["octave"] * 100, 1)) for c in measured]))
    check("每档量出的比例都在 0~1 之间（尺子没给出荒唐值）",
          all(0.0 <= c["octave"] <= 1.0 for c in measured))
    check("体检段落在素材范围内",
          bool(r.get("starts")) and all(
              0.0 <= x <= max(0.0, 100.0 - r["secs"] / max(1, len(r["starts"])) + 1.0)
              for x in r["starts"]),
          "取样点 %s（每段 %.0f 秒）" % (r.get("starts"), r["secs"] / max(1, len(r.get("starts") or [1]))))
    # ★ 这条是防"等分取样悄悄退化成挑一段"：取样点必须**铺开**在整首上。
    check("★ 取样点真的铺开在整首上（不是都挤在一处）",
          len(r.get("starts") or []) >= 2
          and (max(r["starts"]) - min(r["starts"])) >= 0.30 * 100.0,
          "取样点 %s，跨度 %.0f 秒 / 素材 %d 秒"
          % (r.get("starts"), (max(r["starts"]) - min(r["starts"])) if r.get("starts") else 0.0,
             100))
    check("选用的音高一定在候选里",
          r["chosen"] in [c.get("pitch") for c in r.get("cands") or []],
          "%+d" % r["chosen"])
    check("结论文字非空", bool((r.get("reason") or "").strip()), (r.get("reason") or "")[:70])
    same = studio.pick_audition(r["cands"], PITCH)
    check("同一张体检表再判一次结果相同（确定性）",
          same[0] == r["chosen"] and bool(same[1]) == bool(r["changed"]))
    try:
        json.dumps(r, ensure_ascii=False)
        okj = True
    except Exception as e:                          # noqa: BLE001
        okj = False
        print("      JSON 序列化失败：%s" % e)
    check("★ 体检表能塞进产物 JSON（不然写 roles.json 会炸）", okj)
    print("      体检表：%s" % json.dumps(r, ensure_ascii=False)[:220])

    # --------------------------- E. 产品真入口 ---------------------------
    sec("E. 跑产品真入口：改判后整首要用新音高，且两个音高都进产物")
    role_bak = os.path.join(WORK, "roles_backup.json")
    roles_json = studio.ROLES_JSON
    shutil.copy2(roles_json, role_bak)
    final_dir = os.path.join(HERE, "data", "roles", ROLE, "final")
    before = set(os.listdir(final_dir)) if os.path.isdir(final_dir) else set()

    clip_v = os.path.join(WORK, "clip_vocal.wav")
    ok_c, _ = None, None
    try:
        import subprocess
        clip_cmd = ('"%s" -y -v error -t 22 -i "%s" -ac 2 -ar 44100 "%s"'
                    % (studio.FFMPEG, vocal, clip_v))
        rc = subprocess.run(clip_cmd, shell=True, capture_output=True).returncode
        ok_c = (rc == 0 and os.path.exists(clip_v))
    except Exception as e:                          # noqa: BLE001
        print("      裁片段失败：%s" % e)
    check("测试用短片段裁出来了", bool(ok_c))

    STUB_TO = 5
    real_pick = studio.audition_pitch
    bak_min, bak_on = studio.AUDITION_MIN_SONG, studio.AUDITION
    newfiles = []
    params = None
    err = None
    if ok_c:
        studio.AUDITION_MIN_SONG = 1.0     # 让短片段也走体检那条路
        studio.AUDITION = True
        studio.audition_pitch = lambda job, v, req, *a, **k: {
            "start": 0.0, "secs": 22.0, "cands": [
                {"pitch": int(req), "match": 0.70, "octave": 0.18},
                {"pitch": STUB_TO, "match": 0.93, "octave": 0.02}],
            "chosen": STUB_TO, "changed": True,
            "reason": "门禁桩：故意改判到 %+d" % STUB_TO, "skipped": None}
        try:
            wd2 = os.path.join(WORK, "sing")
            if os.path.isdir(wd2):
                shutil.rmtree(wd2, ignore_errors=True)
            os.makedirs(wd2, exist_ok=True)
            job2 = mk_job("sing", wd2)
            try:
                studio.sing_role(job2, ROLE, clip_v, pitch=PITCH, index_rate=0.50,
                                 prefix="pitchaud", mix=False, save=True,
                                 qc=False, pre_sep=(clip_v, clip_v),
                                 nonvocal="off")
            except Exception as e:                  # noqa: BLE001
                err = e
            check("★ 带着改判结果跑完 sing_role（不失败）", err is None,
                  (str(err)[:200] if err else ""))
            rec = studio.get_role(ROLE) or {}
            outs = [o for o in (rec.get("outputs") or [])
                    if str(o.get("kind")) not in ("preview",)]
            # ★★ 2026-10-24：这里原来是 startswith("voice_pitchaud")，被实测推翻了。
            #   成品名是**每一步都可能加前缀**的：高频补偿 -> voice_hf_*、
            #   去金属声 -> voice_dm_*。第四轮给去金属声加了「过冲（破音）」这条
            #   独立的分支之后，它**在不电的素材上也会动手**（过冲是另一个毛病），
            #   于是这一档（ΔHNR 1.49、4-8k ×1.16，明明不电）也被写成了 voice_dm_*，
            #   按 "voice_" 前缀找 ⇒ 0 条 ⇒ 门禁直接 AttributeError 崩掉。
            #   ⇒ 认这次跑的 **tag**（prefix + 时间戳），不认前缀。
            mine = [o for o in outs
                    if "pitchaud_" in os.path.basename(str(o.get("wav") or ""))]
            check("这次跑的成品的产物记录能找到", bool(mine),
                  "%d 条" % len(mine))
            if mine:
                params = (mine[-1].get("params") or {})
            params = params or {}      # 找不到也别崩：崩了后面 7 条判据全看不到
            check("★ 产物里记了参数（params 存在）", bool(params),
                  json.dumps(params, ensure_ascii=False)[:150] if params else "空")
            check("★ 整首用的是**改判后**的音高",
                  params.get("pitch") == STUB_TO,
                  "pitch=%r 期望 %+d" % (params.get("pitch"), STUB_TO))
            check("★ 同时记下用户原本设的音高（能看出改判过）",
                  params.get("pitch_asked") == PITCH,
                  "pitch_asked=%r 期望 %+d" % (params.get("pitch_asked"), PITCH))
            check("产物记了模型名", bool(params.get("model")), str(params.get("model")))
            check("产物记了 f0 方法 / 检索强度",
                  bool(params.get("f0")) and params.get("index_rate") == 0.50,
                  "f0=%r index_rate=%r" % (params.get("f0"), params.get("index_rate")))
            check("产物里带了体检表（含每一档的数）",
                  isinstance(params.get("audition"), dict)
                  and len((params["audition"] or {}).get("cands") or []) == 2)
            lg = os.path.join(job2["dir"], "job.log")
            txt = open(lg, encoding="utf-8", errors="replace").read() if os.path.exists(lg) else ""
            check("★ 改判这件事在日志里说得明明白白（不悄悄改）",
                  ("改用" in txt) and ("%+d" % STUB_TO in txt),
                  "日志 %d 字节" % len(txt))
        finally:
            studio.audition_pitch = real_pick
            studio.AUDITION_MIN_SONG, studio.AUDITION = bak_min, bak_on
    # 还原 roles.json + 清掉本次建的成品
    shutil.copy2(role_bak, roles_json)
    if os.path.isdir(final_dir):
        for f in os.listdir(final_dir):
            if f not in before:
                newfiles.append(os.path.join(final_dir, f))
    n_del = 0
    for p in newfiles:
        try:
            os.remove(p)
            n_del += 1
        except Exception:
            pass
    print("      还原 roles.json，清掉本次新建的成品 %d 个" % n_del)
    left = [f for f in os.listdir(final_dir)] if os.path.isdir(final_dir) else []
    check("没在角色成品目录里留下垃圾",
          all(f in before for f in left),
          "多出来：%s" % [f for f in left if f not in before])

    # ------------------- F. f0 选型由实测决定（不靠文献） -------------------
    f_f0_method(job, vocal, model, idx)

    return summary()


# ---------------------------------------------------------------- f0 选型
def _run_rvc_one(job, src, out, model, idx, f0, pitch=-12):
    """跑一次 RVC 转换（直接调 CLI，跟探针同口径）。成功返回 True。

    ★ 解释器必须用 **studio.RVC_PY**（RVC 的 runtime），不是 studio.PY（那是
      GPT-SoVITS 的）—— 两个 runtime 的依赖不通用，用错了命令一行输出都没有，
      症状就是"RVC 没跑出来"，看不出是解释器拿错了（第一版就写错过一次）。
    """
    import subprocess as _sp
    cmd = '"%s" -s "%s" --model "%s" --input "%s" --output "%s" --f0 %s ' \
          '--pitch %d --index-rate 0.50 --rms-mix-rate 0.25 --protect 0.33' % (
              studio.RVC_PY, os.path.join(HERE, "stages", "rvc_infer.py"),
              model, os.path.abspath(src), os.path.abspath(out), f0, pitch)
    if idx:
        cmd += ' --index "%s"' % idx
    try:
        p = _sp.run(cmd, shell=True, cwd=studio.RVC_ROOT,
                    capture_output=True, text=True,
                    encoding="utf-8", errors="replace")
        rc = p.returncode
        if rc != 0:
            print("      rc=%d %s" % (rc, ((p.stdout or "") + (p.stderr or ""))[-300:]))
    except Exception as e:                          # noqa: BLE001
        print("      调 CLI 失败：%s" % e)
        rc = -1
    return rc == 0 and os.path.isfile(out)


def f_f0_method(job, vocal, model, idx):
    """f0 提取方法必须是**实测更稳**的那一个。

    为什么要有这一段：文献口径并不一致（有的说 fcpe 最快，有的说 rmvpe 最准），
    而本机实测恰恰**和资料相反**（fcpe 39.8s 最慢、抖动 31.7 cents；rmvpe 23.8s、
    抖动 17.1 cents）。注释写在源码里会被忘，这里用机器把它钉住：
    谁把 `RVC_F0_METHOD` 改回去，这一段就会红。

    ★ 判据用的是**相对比较**（当前选型 == 实测抖动更小的那个），不是绝对阈值
      —— 绝对阈值会随素材/模型漂移，相对比较不会（两台机器都能得出各自的答案）。
    """
    sec("F. f0 提取方法：当前选型必须是实测更稳的那个（不是文献说的那个）")
    src = os.path.join(WORK, "f0src.wav")
    ok, _ = studio.clip_audio(job, vocal, src, 0.0, 20.0)
    if not ok or not os.path.isfile(src):
        check("f0 对照素材裁出来了", False, "裁剪失败")
        return
    check("f0 对照素材裁出来了", True, "20 秒")

    res = {}
    for m in ("rmvpe", "fcpe"):
        out = os.path.join(WORK, "f0_%s.wav" % m)
        if not _run_rvc_one(job, src, out, model, idx, m):
            check("f0=%s 跑通" % m, False, "RVC 没跑出来")
            continue
        try:
            q = studio.qc_singing_whole(job, os.path.abspath(src), os.path.abspath(out))
        except Exception as e:                      # noqa: BLE001
            q = None
            print("      qc 失败：%s" % e)
        d = (q or {}).get("dev_iqr_cents")
        res[m] = d
        check("f0=%s 量出了音高抖动" % m, d is not None,
              ("%.1f cents" % d) if d is not None else "None")

    if len(res) < 2 or any(v is None for v in res.values()):
        print("      两档没都量出来，跳过选型判定")
        return

    best = min(res, key=lambda k: res[k])
    worst = max(res, key=lambda k: res[k])
    detail = "  ".join("%s %.1f" % (k, v) for k, v in sorted(res.items(), key=lambda x: x[1]))
    check("★ 两档抖动确实有差别（这把尺子有分辨力，不是都差不多）",
          (res[worst] - res[best]) >= 2.0, "%s（差 %.1f cents）" % (detail, res[worst] - res[best]))
    check("★ 当前 RVC_F0_METHOD 就是实测更稳的那个",
          studio.RVC_F0_METHOD == best,
          "当前=%s 更稳=%s | %s" % (studio.RVC_F0_METHOD, best, detail))

    # 负对照：把选型换成更抖的那个 ⇒ 上面那条必须变红（证明它真的在判）
    bak = studio.RVC_F0_METHOD
    try:
        studio.RVC_F0_METHOD = worst
        red = (studio.RVC_F0_METHOD != best)
    finally:
        studio.RVC_F0_METHOD = bak
    check("★ 负对照：换成更抖的那档时判据会红（不是写死的）", red,
          "换成 %s ⇒ 判定=%s" % (worst, "红" if red else "仍绿（尺子坏了）"))
    check("负对照之后选型已复原", studio.RVC_F0_METHOD == bak, studio.RVC_F0_METHOD)

    e_early_stop()
    g_metal()


def e_early_stop():
    """E. 体检早停：第 1 档够好就不再试对照。

    为什么值得单开一段（时间账）：体检每档 = 一次真 RVC（实测 17.0s）+ 一次质检（6.7s）
    ≈ 23.7 秒，默认试 3 档 ⇒ 光体检 71 秒。而实测 +0 档八度错只有 1.0~1.1%、
    宽松吻合 97.7%，那种情况下后两档买不到任何东西（pick_audition 的规则是
    "设定档达标就不改判"）。所以加了早停 —— 但**必须证明它只在真的没必要时才停**，
    否则就变成"为了快而漏掉该改判的档"（那正是本关要防的事）。

    判据分三层：① 规则表（含 5 条负对照）② 源码接线 ③ **打桩真跑**，数 RVC 调用次数
    差值必须正好是 2（证明省的是推理，不是别的地方）。
    """
    sec("E. 体检早停：第 1 档够好就不再试对照（每档 ~23.7 秒）")
    f = getattr(studio, "_audition_should_stop", None)
    check("早停判据是独立函数（门禁能直接测，不必真跑模型）", callable(f))
    check("常量存在：早停的八度系数 / 吻合绝对线",
          hasattr(studio, "AUDITION_EARLY_OCTAVE") and hasattr(studio, "AUDITION_EARLY_MATCH"))
    if not callable(f) or not hasattr(studio, "AUDITION_EARLY_MATCH"):
        return

    def row(p, o, m):
        return {"pitch": p, "octave": o, "match": m}

    check("好档（八度错 1.0% / 吻合 97.7%）⇒ 停", f([row(0, 0.010, 0.977)], 0, 3, 0) is True)
    check("负对照：八度错 6%（刚过阈值一半 5.5%）⇒ 不许停",
          f([row(0, 0.060, 0.977)], 0, 3, 0) is False)
    check("负对照：吻合只有 90% ⇒ 不许停（光不翻八度不够）",
          f([row(0, 0.010, 0.900)], 0, 3, 0) is False)
    check("负对照：这一档没量出来（None）⇒ 不许停",
          f([row(0, None, None)], 0, 3, 0) is False)
    check("★ 负对照：第 1 档不达标时照旧试满（−12 8% 那种要挑对照档）",
          f([row(-12, 0.080, 0.900)], 0, 3, -12) is False)
    check("负对照：已经是第 2 档了 ⇒ 不许停（只认第 1 档）",
          f([row(0, 0.010, 0.977), row(-12, 0.010, 0.977)], 1, 3, 0) is False)
    check("负对照：第 1 档不是设定档 ⇒ 规则不适用",
          f([row(-12, 0.010, 0.977)], 0, 3, 0) is False)
    # ★ 尺子自检：把吻合线抬到不可能，同一组好档必须变成"不停"
    bak_m = studio.AUDITION_EARLY_MATCH
    try:
        studio.AUDITION_EARLY_MATCH = 2.0
        red = (f([row(0, 0.010, 0.977)], 0, 3, 0) is False)
    finally:
        studio.AUDITION_EARLY_MATCH = bak_m
    check("★ 尺子自检：阈值一改，同一组数据就不许停（判据不是恒真）", red)

    src = inspect.getsource(studio.audition_pitch)
    check("循环里真的接了早停（不是算了不用）",
          "_audition_should_stop(" in src and "\n                break" in src)
    check("早停时**说出**省了几秒（不许静默省时间）", "剩下的 %d 档不试了" in src)

    # ---- ③ 打桩真跑：数 RVC 调用次数，差值必须正好 2 档 ----
    import re

    wav = os.path.join(WORK, "early_probe_src.wav")
    ok, why = build_test_vocal(wav, 100.0)
    if not ok:
        check("能造出体检用的长干声（打桩段的前提）", False, why)
        return

    def _rvc_calls(early_match):
        n = {"rvc": 0}
        bak = (studio.run, studio.qc_singing, studio._montage,
               studio.AUDITION_EARLY_MATCH)

        def _run(job, cmd, **kw):
            if "rvc_infer.py" in cmd:
                n["rvc"] += 1
            # 把命令里出现的 .wav 路径都造出来（RVC 的 --output 必须在，
            # 否则 audition 会当成"推理没跑通"直接 continue）
            for m in re.finditer(r'"([^"]+\.wav)"', cmd):
                p = m.group(1)
                try:
                    if not os.path.exists(p):
                        open(p, "wb").close()
                except Exception:
                    pass
            return 0, ""

        def _qc(job, *a, **kw):
            return {"octave_err_ratio": 0.010, "match_ratio_loose": 0.977,
                    "contour_corr": 0.9}

        try:
            studio.run = _run
            studio.qc_singing = _qc
            studio._montage = lambda *a, **kw: ([0.0, 33.0, 66.0], "门禁打桩")
            studio.AUDITION_EARLY_MATCH = early_match
            studio.audition_pitch(mk_job("early", WORK), wav, 0, "m.pth", "", 0.5)
        finally:
            (studio.run, studio.qc_singing, studio._montage,
             studio.AUDITION_EARLY_MATCH) = bak
        return n["rvc"]

    n_stop = _rvc_calls(0.95)
    n_full = _rvc_calls(2.0)          # 2.0 = 永远不满足 ⇒ 等同于关掉早停
    check("早停生效时只跑 1 档推理", n_stop == 1, "实际 %d 档" % n_stop)
    check("关掉早停时跑满 3 档（证明差值来自早停，不是别处少跑）",
          n_full == 3, "实际 %d 档" % n_full)
    check("★ 早停净省 2 次推理（每次 ~23.7 秒 ⇒ 约 47 秒/首）",
          (n_full - n_stop) == 2, "%d → %d 档" % (n_full, n_stop))
    check("打桩之后 audition 的现场已复原（没污染后面的真跑段）",
          studio.AUDITION_EARLY_MATCH == bak_m and studio.qc_singing is not None)


def g_metal():
    """G. 体检必须量得出"这一档会电"（2026-10-24 加）。

    为什么单开一段：老板两次反馈「明显电音」，而老体检**只量音准**。
    八度错对变调近乎纯平移 —— +12 设上去音准照样 98.6%，体检说"这一档没问题"，
    可实测 HNR 比原唱高 10.4dB、4-8k 能量是原唱的 4.5 倍，听感就是金属声。
    **量不出电音的体检等于替错误档位背书**，这是本项目最贵的一类静默。

    判据三条线：① 常量 ② metal_verdict 的每条分支（含负对照）
    ③ 早停不许停在一个会电的档上（停在那就永远找不到替代档，指路功能作废）。
    """
    sec("G. 体检要量得出「这一档会电」（不是只量音准）")
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("常量 AUDITION_METAL_HNR_DB 存在",
          hasattr(studio, "AUDITION_METAL_HNR_DB"))
    check("常量 AUDITION_METAL_HF_REL 存在",
          hasattr(studio, "AUDITION_METAL_HF_REL"))
    check("HNR 阈值是个能看出来的量（1~10 dB）",
          1.0 <= getattr(studio, "AUDITION_METAL_HNR_DB", 0) <= 10.0,
          str(getattr(studio, "AUDITION_METAL_HNR_DB", None)))
    f = getattr(studio, "metal_verdict", None)
    check("metal_verdict 是纯函数（能喂数字验分支，不必真跑模型）", callable(f))
    if not callable(f):
        return
    base = {"hnr_db": 9.14, "hf_ratio": 0.0076}
    # 实测原型：苹果嘉儿 +12（HNR 18.95、4-8k 3.45%）
    bad, dh, rel = f(base, {"hnr_db": 18.95, "hf_ratio": 0.0345})
    check("★ 气声被抹掉（HNR +9.8dB）且高频堆积（4-8k ×4.5）⇒ 判电", bad is True,
          "差 %.1f dB / %.2f 倍" % (dh, rel))
    # 大帝 +3：只高 1.7dB ⇒ 不该判电
    bad2, dh2, _r2 = f(base, {"hnr_db": 10.86, "hf_ratio": 0.0095})
    check("★ 只差 1.7 dB（大帝 +3 那一版）⇒ 不判电（别把正常档也报红）",
          bad2 is False, "差 %.1f dB" % dh2)
    # ★ 降八度：HNR 反而低 4.6dB，可 4-8k 占比照样翻 3 倍（低频整体下移导致）
    bad3, dh3, rel3 = f(base, {"hnr_db": 6.79, "hf_ratio": 0.0294})
    check("★ 降八度（HNR 反而低 4.6dB、4-8k ×2.9）⇒ **不**判电 —— "
          "高频占比会骗人，必须配合气声丢失",
          bad3 is False, "差 %.1f dB / %.2f 倍" % (dh3, rel3))
    check("负对照：HNR 只高 1dB 但 4-8k 翻 3 倍 ⇒ 判电（两条路都通）",
          f(base, {"hnr_db": 10.2, "hf_ratio": 0.023})[0] is True)

    # ---- 早停：会电就不能停 ----
    g = getattr(studio, "_audition_should_stop", None)
    if callable(g):
        def row(p, o, m, metal=None):
            r = {"pitch": p, "octave": o, "match": m}
            if metal is not None:
                r["metal"] = metal
            return r
        check("★ 好档但**会电** ⇒ 不许早停（停了就找不到不电的档，指路作废）",
              g([row(0, 0.010, 0.977, True)], 0, 3, 0) is False)
        check("对照：同样的档不电 ⇒ 照旧早停（早停规则没被误伤）",
              g([row(0, 0.010, 0.977, False)], 0, 3, 0) is True)
        check("对照：会电的档即使八度错为 0 也不许停",
              g([row(12, 0.0, 0.99, True)], 0, 3, 12) is False)

    # ---- pick_audition：电了要指路，不许谎称"都电" ----
    c1 = [{"pitch": 12, "match": 0.895, "octave": 0.002, "metal": True,
           "hnr_delta": 9.8, "hf_rel": 4.5},
          {"pitch": 0, "match": 0.87, "octave": 0.01, "metal": False}]
    _p, _ch, why = studio.pick_audition(c1, 12)
    check("★ 设定档会电、旁边有不电的档 ⇒ 文案指出来换哪一档",
          "金属声" in why and "+0" in why, why[:60])
    c2 = [{"pitch": 0, "match": 0.9917, "octave": 0.0, "metal": True,
           "hnr_delta": 5.37, "hf_rel": 1.87},
          {"pitch": -12, "match": 0.9267, "octave": 0.0615, "metal": False}]
    _p2, _ch2, why2 = studio.pick_audition(c2, 0)
    check("★ 不电的档音准掉太多 ⇒ 照实说掉几个点（不许谎称'试过的几档都会电'）",
          "掉" in why2 and "-12" in why2 and "都会电" not in why2, why2[:70])
    check("★ 会电也不擅自改判（用户设 −12 是想压低，替他改是越权）",
          _ch is False and _p == 12 and _ch2 is False and _p2 == 0)
    # ★ 要比的是**调用点**不是函数定义：`_audition_should_stop` 的定义在文件里
    #   靠前（6500 行），而 metal 测量在 7000 行 —— 拿定义的位置比会得到
    #   "测量在早停之后"的荒谬结论（第一版就栽在这，判据自己红了）。
    i_metal = src.find("for m in _metal_batch(job, [clip, c_out])")
    i_stop = src.find("if _audition_should_stop(")
    check("源码里电音测量在**早停调用之前**（挪到后面 metal 永远是 None）",
          0 < i_metal < i_stop, "测量 @%d / 早停 @%d" % (i_metal, i_stop))


def summary():
    print("\n" + "=" * 78, flush=True)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)), flush=True)
    for x in BAD:
        print("  FAIL: %s" % x, flush=True)
    if not BAD:
        print("音高体检：全绿", flush=True)
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
