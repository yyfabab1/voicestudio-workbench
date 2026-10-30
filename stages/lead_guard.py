# -*- coding: utf-8 -*-
r"""非主唱段保护：原曲里**不是主唱唱的那几段**（伴唱 / 和声 / 垫唱 / 群唱），
不要被当成主唱一起"唱"出来。

## 为什么需要（用户 2026-10-27 报的问题）
分离出来的人声轨是"所有人声"：主唱 + 伴唱 + 和声 + 垫唱。
RVC 只认 f0 曲线，它分不出谁在唱 —— 于是**伴唱那几段也被角色唱了一遍**，
叠在一起就成了"很多人一起唱、发闷发哑"。

## 判据：**相对这首歌自己的主唱基线**，不是绝对阈值
人声轨是立体声。主唱通常**居中**（L/R 高度相关、侧能量小），伴唱/和声通常
**变宽**（L/R 去相关、侧能量大）。实测 6 首歌，侧/中比与相关性的差别很大：

    歌 A  相关 p50 0.87  侧/中 0.28     歌 D  相关 p50 0.99  侧/中 0.06
    歌 C  相关 p50 0.45  侧/中 0.62     歌 E  相关 p50 0.85  侧/中 0.31

⇒ 用绝对阈值一定翻车（同一阈值下歌 C 会圈 60%、歌 D 只圈 0.3%）。
所以这里用**这首歌自己的中位数当基线**：只圈"明显比它自己更宽"的段。

## 三道闸门（宁可不动，也不许误伤主唱）
1. 相对判据：corr < 基线 − 0.35 且 侧/中 > 基线 × 1.8 且 侧/中 > 0.15
2. 持续性：至少 0.6 秒、且并掉 < 0.4 秒的缝（伴唱是成段的，不是单帧毛刺）
3. 上限：一旦圈出的时长 > 有声时长的 35% 或 > 全曲 30% ⇒ **判定不可靠，整段保护
   撤销**，并在报告里说清"判据在这首歌上失效"（宁可不做，也不许把主唱静音）

## 三种模式（与口哨保护同一套语义）
    keep（auto 等价）  那几段换回**原始人声轨** —— 伴唱还是原版伴唱，只是角色不唱它
    mute               那几段静音
    off                完全不动（等于旧行为）

用法（cwd 随意，只要 numpy + soundfile）：
    python lead_guard.py --src <原人声轨.wav> --rvc <变声后.wav> --out <输出.wav> \
        [--mode keep|mute|off] [--report <json>]

stdout 只打一行机器可读结论（LEAD_DONE / LEAD_SKIP / LEAD_FAIL）。
"""

import argparse
import json
import os
import sys

import numpy as np
import soundfile as sf

SR_TARGET = 0        # 保持输入采样率
WIN_MS = 100.0       # 分析帧长
HOP_MS = 50.0        # 帧移
SILENCE_DB = -40.0   # 低于这个（相对全曲峰值）的帧当静音，不参与判定
CORR_DROP = 0.45     # 相关性要比"自己基线"低这么多（0.35->0.45：实测 Summer Vibe 一次圈 47s 原声，太狠）
SMR_MULT = 2.2       # 侧/中要比"自己基线"高这么多倍（同上，收紧）
SMR_MIN = 0.15       # 侧/中绝对下限（太窄的不算伴唱）
MIN_SPAN_SEC = 1.0   # 最短保护段（0.6->1.0：短于 1 秒的多半是换气/尾音，不算伴唱）
MERGE_GAP_SEC = 0.4  # 段间小于这个的缝并起来
MAX_PROTECT_RATIO = 0.12   # 超过有声时长的这个比例 ⇒ 判据不可靠，撤销（0.35->0.12）
MAX_TOTAL_RATIO = 0.12     # 超过全曲时长的这个比例 ⇒ 同上（0.30->0.12）
XFADE_MS = 20.0      # 与 nonvocal_guard 一致的边界交叉淡化


def _load(p):
    x, sr = sf.read(p, dtype="float32", always_2d=True)
    return x, int(sr)


def analyze(src_stereo, sr):
    """返回 (帧 flag 数组, 统计 dict)。src_stereo: (n, 2)。"""
    L = src_stereo[:, 0].astype(np.float64)
    R = src_stereo[:, 1].astype(np.float64) if src_stereo.shape[1] > 1 else L
    win = max(16, int(sr * WIN_MS / 1000.0))
    hop = max(8, int(sr * HOP_MS / 1000.0))
    n = len(L)
    nf = max(0, (n - win) // hop + 1)
    peak = float(np.abs(src_stereo).max()) + 1e-12
    thr = peak * (10 ** (SILENCE_DB / 20.0))
    corrs, smrs, voiced = [], [], []
    for i in range(nf):
        a = L[i * hop:i * hop + win]
        b = R[i * hop:i * hop + win]
        m = (a + b) / 2.0
        s = (a - b) / 2.0
        rm = float(np.sqrt((m ** 2).mean()))
        if rm < thr:
            voiced.append(False)
            corrs.append(1.0)
            smrs.append(0.0)
            continue
        if a.std() > 1e-9 and b.std() > 1e-9:
            c = float(np.corrcoef(a, b)[0, 1])
        else:
            c = 1.0
        voiced.append(True)
        corrs.append(c)
        smrs.append(float(np.sqrt((s ** 2).mean()) / (rm + 1e-9)))
    corrs = np.asarray(corrs, dtype=np.float64)
    smrs = np.asarray(smrs, dtype=np.float64)
    voiced = np.asarray(voiced, dtype=bool)
    if not voiced.any():
        return None, {"reason": "全曲没有可判定的人声帧"}
    base_corr = float(np.median(corrs[voiced]))
    base_smr = float(np.median(smrs[voiced]))
    flag = (voiced & (corrs < base_corr - CORR_DROP)
            & (smrs > base_smr * SMR_MULT) & (smrs > SMR_MIN))

    # 持续段 + 并缝
    hop_s = hop / float(sr)
    runs, start = [], None
    for i, f in enumerate(flag):
        if f and start is None:
            start = i
        elif not f and start is not None:
            runs.append((start, i))
            start = None
    if start is not None:
        runs.append((start, len(flag)))
    merged = []
    for s, e in runs:
        if merged and (s - merged[-1][1]) * hop_s < MERGE_GAP_SEC:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    spans = [(s * hop_s, e * hop_s) for s, e in merged
             if (e - s) * hop_s >= MIN_SPAN_SEC]

    voiced_secs = float(voiced.sum()) * hop_s
    total_secs = n / float(sr)
    prot_secs = float(sum(e - s for s, e in spans))
    stats = {"sr": sr, "base_corr": round(base_corr, 3),
             "base_smr": round(base_smr, 3), "voiced_secs": round(voiced_secs, 2),
             "total_secs": round(total_secs, 2), "protected_secs": round(prot_secs, 2),
             "spans": [[round(s, 2), round(e, 2)] for s, e in spans]}
    reliable = True
    if prot_secs > MAX_PROTECT_RATIO * max(voiced_secs, 1e-9):
        reliable = False
        stats["reject"] = ("圈出 %.1f 秒 > 有声时长的 %.0f%% —— 判据在这首歌上不可靠，"
                           "整段撤销（不做比误伤主唱好）"
                           % (prot_secs, MAX_PROTECT_RATIO * 100))
    elif prot_secs > MAX_TOTAL_RATIO * max(total_secs, 1e-9):
        reliable = False
        stats["reject"] = ("圈出 %.1f 秒 > 全曲的 %.0f%% —— 判据在这首歌上不可靠，"
                           "整段撤销" % (prot_secs, MAX_TOTAL_RATIO * 100))
    stats["reliable"] = reliable
    return (spans if reliable else []), stats


def _sample_mask(n, sr, spans, xfade_ms=XFADE_MS):
    m = np.zeros(n, dtype=np.float32)
    for s, e in spans:
        a, b = int(s * sr), min(n, int(e * sr))
        if b > a:
            m[a:b] = 1.0
    w = max(1, int(xfade_ms / 1000.0 * sr))
    if w > 1 and m.any():
        kern = np.hanning(w * 2 + 1)
        kern = kern / kern.sum()
        m = np.convolve(m, kern, mode="same").astype(np.float32)
        m = np.clip(m * 1.6, 0.0, 1.0)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="原始人声轨（立体声才能判；单声道会直接跳过）")
    ap.add_argument("--rvc", required=True, help="变声后的干声")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="keep", choices=("keep", "mute", "off"))
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    rep = {"ok": False, "mode": a.mode}
    try:
        src, sr = _load(a.src)
        rvc, sr2 = _load(a.rvc)
    except Exception as e:
        print("LEAD_FAIL 读不了音频：%s" % e)
        return 1
    if src.shape[1] < 2:
        # 单声道没有立体声线索 ⇒ 明确说"做不了"，绝不瞎判
        rep["reason"] = "人声轨是单声道，没有立体声线索（判不了谁是伴唱）"
        if a.report:
            json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("LEAD_SKIP mono")
        return 0
    spans, stats = analyze(src, sr)
    rep.update(stats or {})
    rep["mode"] = a.mode
    if a.mode == "off" or not spans:
        rep["ok"] = True
        rep["protected_secs"] = 0.0
        if a.report:
            json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("LEAD_SKIP mode=%s protected=0" % a.mode)
        return 0

    # 对齐长度/采样率（RVC 与源可能不同 sr）
    rvc_mono = rvc.mean(axis=1) if rvc.shape[1] > 1 else rvc[:, 0]
    if sr2 != sr and len(rvc_mono) > 1:
        # 线性插值重采样（不依赖 librosa：这条链跑在 GPT-SoVITS runtime 上）
        n_out = int(round(len(rvc_mono) * sr / float(sr2)))
        x_old = np.linspace(0.0, 1.0, len(rvc_mono), endpoint=False)
        x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
        rvc_mono = np.interp(x_new, x_old, rvc_mono).astype(np.float32)
    n = min(len(src), len(rvc_mono))
    src_m = src[:n].mean(axis=1)
    m = _sample_mask(n, sr, spans)
    if a.mode == "keep":
        out = rvc_mono[:n] * (1.0 - m) + src_m * m
    else:
        out = rvc_mono[:n] * (1.0 - m)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    sf.write(a.out, out.astype(np.float32), sr)
    rep["ok"] = True
    if a.report:
        json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("LEAD_DONE mode=%s protected=%.2f spans=%d base_corr=%.2f base_smr=%.2f"
          % (a.mode, float(rep.get("protected_secs") or 0), len(spans),
             float(rep.get("base_corr") or 0), float(rep.get("base_smr") or 0)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
