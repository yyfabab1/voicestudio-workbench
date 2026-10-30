# -*- coding: utf-8 -*-
"""一次性：给多角色链路打变异，确认 verify_multising 真的会红。

★ 为什么两个文件都要变异：
  · stages/multising.py —— 分配表与拼接（纯数学，靠合成信号量出来）；
  · studio.py            —— 接线（高频补偿 / 自动歌词 / 逐人体检 / 换法重拼）。
  只变异数学、不变异接线的话，"接了一半"这种错照样全绿（踩过不止一次）。

跑完会把两个文件原样写回；有任何一个变异没被抓住 → 退出码 1。
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(HERE, "verify_multising.py")
PY = r"C:\Users\yyfabab\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
MS = os.path.join(HERE, "stages", "multising.py")
ST = os.path.join(HERE, "studio.py")

MUTS = [
    (MS, "不做响度对齐（两个角色音量不再拉平）",
     "    xs = [x * g for x, g in zip(xs, gains)]",
     "    xs = list(xs)"),
    (MS, "plan_takes 里 phrase 用了 section 的阈值（两种模式不再有区别）",
     "    gap = GAP_SECTION if mode == \"section\" else GAP_PHRASE",
     "    gap = GAP_SECTION"),
    (MS, "句间空隙不归前一句（留缝）",
     "        if st > cursor:\n            # 空隙（或前奏）归前一句那个人；还没有前一句就归这一句\n            _push(cursor, st, takes[-1][2] if takes else who)",
     "        if st > cursor:\n            cursor = st"),
    (MS, "rotate 退化成全给第一个人",
     "    if mode == \"rotate\":\n        return [i % n_roles for i in range(n)]",
     "    if mode == \"rotate\":\n        return [0] * n"),
    (MS, "phrase 分组阈值写成 0（每组只有一句 = 退化成 rotate）",
     "    gap = GAP_SECTION if mode == \"section\" else GAP_PHRASE",
     "    gap = GAP_SECTION if mode == \"section\" else 0.0"),
    (MS, "mask 不归一化（接缝处可能塌陷）",
     "    out = np.zeros(n, dtype=np.float32)\n    for x, m in zip(xs, masks):\n        out += x * (m / total)",
     "    out = np.zeros(n, dtype=np.float32)\n    for x, m in zip(xs, masks):\n        out += x * m"),
    (MS, "接缝数统计恒为 0",
     "        if prev is not None and k != prev:\n            seams += 1",
     "        if False:\n            seams += 1"),
    (MS, "叠唱不乘和声音量（整轨直接相加 = 抢主旋律 + 削顶）",
     "    for x in xs[1:]:\n        out += x * float(other_gain)",
     "    for x in xs[1:]:\n        out += x"),
    (MS, "叠唱去掉峰值保护（超 1 就削顶）",
     "    out, scaled, peak = _peak_guard(out)",
     "    scaled, peak = False, 0.0"),
    (MS, "响度对齐退回整轨 RMS（一句大一句小）",
     "        vals.append(_rms_masked(x, m) if secs >= min_sec else _rms(x))",
     "        vals.append(_rms(x))"),
    (MS, "逐人体检的「跳过段」恒为空（每个人整曲都比一遍 = 抽签）",
     "    for s, e in role_spans(takes, k):",
     "    for s, e in []:"),
    (MS, "取样窗口取最短那段（多半太短，体检直接报段太短）",
     "    s, e = max(sp, key=lambda x: x[1] - x[0])",
     "    s, e = min(sp, key=lambda x: x[1] - x[0])"),
    (MS, "角色区间不合并（碎段当成独立小区间）",
     "        if spans and s - spans[-1][1] <= join_gap:",
     "        if False:"),
    (ST, "多角色不补高频（界面选了也白选）",
     "            used_wav, hf_info = hf_brighten(job, out_wav, vocal_wav, hf_out, hf)",
     "            used_wav, hf_info = out_wav, None  # hf_brighten(...) 被删"),
    (ST, "多角色不体检（成品卡上一个数都没有）",
     "            qc_items = multi_qc_roles(job, vocal_wav, outs, names,\n"
     "                                      [x.get(\"pitch_used\", 0) for x in roles],\n"
     "                                      qc_takes, total)",
     "            qc_items = []  # multi_qc_roles(...) 被删"),
    (ST, "没认过词也不自动认（静默按时间均分）",
     "        segs, cached_dur, lyric_src = ensure_lyric_times(\n"
     "            job, song_path, sep_model=sep_model, auto_lyric=auto_lyric)",
     "        pass  # ensure_lyric_times(...) 被删"),
    (ST, "重拼不重新混伴奏（出来的成品没有伴奏）",
     "    mix_wav, tail = _multi_mix(job, inst_wav, voice_wav,\n"
     "                               os.path.join(final, \"mix_%s.wav\" % tag))",
     "    mix_wav, tail = None, \"\"  # _multi_mix(...) 被删"),
    (ST, "分配表预览不返回逐句角色（手动编辑器没法回填）",
     "                      \"role\": int(line_roles[i]) if i < len(line_roles) else 0})",
     "                      \"who\": int(line_roles[i]) if i < len(line_roles) else 0})"),
    # ---- 本轮的缺陷：组数 < 人数时有人一句都唱不到（端到端量出来的） ----
    (MS, "分组不够也不拆（只有第一个人开口，第二个人 0 秒）",
     "    gs, _nb = groups_for_roles(segs, n_roles, gap_sec)",
     "    gs, _nb = group_lines(segs, gap_sec), 0"),
    (MS, "split_groups 第一轮就停（拆了等于没拆）",
     "        if len(g) < 2:\n            break",
     "        break"),
    (ST, "sing_multi 不把分组信息传进去（说明文案成了永不触发的死代码）",
     "            segs or [], total, len(roles), mode=mode, manual=manual, info=_gi)",
     "            segs or [], total, len(roles), mode=mode, manual=manual)"),
    (ST, "预览里写 job 日志（预览是纯函数，接口当场 500）",
     "        stats = _ms.takes_stats(takes, n)\n    if mode == \"chorus\":",
     "        stats = _ms.takes_stats(takes, n)\n        log(job, \"x\")\n"
     "    if mode == \"chorus\":"),
]

ORIG = {}
bad = 0
for path, name, old, new in MUTS:
    if path not in ORIG:
        ORIG[path] = io.open(path, encoding="utf-8").read()
    src = io.open(path, encoding="utf-8").read()
    if old not in src:
        print("[??] 锚点没找到：%s" % name)
        bad += 1
        continue
    io.open(path, "w", encoding="utf-8", newline="\n").write(src.replace(old, new, 1))
    r = subprocess.run([PY, GATE], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=HERE, timeout=900)
    nfail = sum(1 for ln in (r.stdout or "").splitlines()
                if ln.strip().startswith("[FAIL]"))
    red = (r.returncode != 0)
    print("%-6s rc=%d  FAIL=%d  %s" % ("红了" if red else "★没红", r.returncode, nfail, name))
    if not red:
        bad += 1
    io.open(path, "w", encoding="utf-8", newline="\n").write(ORIG[path])

for path, src in ORIG.items():
    io.open(path, "w", encoding="utf-8", newline="\n").write(src)
print("\n还原完成。没被抓住的变异：%d / %d" % (bad, len(MUTS)))
sys.exit(1 if bad else 0)
