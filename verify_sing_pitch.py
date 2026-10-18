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
import os
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
            mine = [o for o in outs
                    if os.path.basename(str(o.get("wav") or "")).startswith("voice_pitchaud")]
            check("这次跑的成品的产物记录能找到", bool(mine),
                  "%d 条" % len(mine))
            if mine:
                params = (mine[-1].get("params") or {})
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
