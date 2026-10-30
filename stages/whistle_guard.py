# -*- coding: utf-8 -*-
r"""口哨专用保护（高基频 + 无谐波 + 稳定 + 短段）：把原曲里的口哨从角色嘴里救回来。

## 为什么单独做一步（而不是放宽 nonvocal_guard）
`nonvocal_guard` 的判据是「谱平坦度低 + 单分音」，它同时受两个方向拉扯：
  · 放宽 ⇒ 干净的高音延长会被当成口哨换回原声（项目以前的"少声音"事故）；
  · 收紧 ⇒ 真实口哨混着混响/气声时漏检（用户报的"口哨还在被唱"）。
2026-10-28 实测：一批歌里平坦度 <0.05 的候选帧，其实**全是干净高音延长**
（f0 中位 327 Hz、0% 的帧 >500 Hz、抖动和正常唱歌一样）⇒ 那条判据在这里是对的，
不能放宽。口哨需要**另一条独立证据链**。

## 判据（四条同时成立，缺一不可）
    ① 基频高：主峰 550 ~ 3000 Hz（实测那批高音延长只有 490~520 Hz，天然分开）
    ② 无谐波：2 倍频能量 / 基频能量 < 0.05（口哨是一根正弦，假声/高音有二次谐波）
    ③ 够纯：谱平坦度 < 0.06、主峰 ±3 bin 能量占比 > 0.55、2.5k 以上能量 < 0.02
    ④ 够稳：整段内主频的标准差 < 25 音分（口哨极稳；颤音、滑音会更抖）
再叠三条安全闸门：
    · 段长 0.2 ~ 3.0 秒（太短是噪声毛刺，太长多半不是口哨）
    · 总量 ≤ 有声时长的 6% 且 ≤ 全曲 5% —— 超了判"不可靠"，一段都不动
（宁可不做，也不许把角色的高音段静音 —— 这条是项目拿事故换来的。）

## 三种模式（与 nonvocal_guard / lead_guard 同一套语义）
    keep（默认） 那段换回原始人声 —— 口哨还在，只是不再被角色"唱"
    mute         那段静音
    off          完全不动

用法（cwd 随意，只要 numpy + soundfile）：
    python whistle_guard.py --src <原人声轨.wav> --rvc <变声后.wav> --out <输出.wav> \
        [--mode keep|mute|off] [--report <json>]

stdout 只打一行机器可读结论（WHISTLE_DONE / WHISTLE_SKIP / WHISTLE_FAIL）。
"""

import argparse
import json
import os
import sys

import numpy as np
import soundfile as sf

SR16 = 16000
WIN = 1024               # 64 ms @16k
HOP = 160                # 10 ms
BAND_LO, BAND_HI = 400.0, 4000.0

F0_MIN, F0_MAX = 550.0, 3000.0   # ① 高基频（实测高音延长 490~520，天然分开）
H2_MAX = 0.05                    # ② 二次谐波 / 基频
FLAT_MAX = 0.06                  # ③ 纯度
PEAK_SHARE_MIN = 0.55
HF_MAX = 0.02
JITTER_MAX_CENTS = 25.0          # ④ 稳定性
SPAN_MIN, SPAN_MAX = 0.2, 3.0    # 段长
GAP_MERGE = 0.15
MAX_VOICED_RATIO = 0.06          # 总量闸门
MAX_TOTAL_RATIO = 0.05
XFADE_MS = 20.0


def _read_mono16(path):
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    x = x.mean(axis=1)
    if sr != SR16 and len(x) > 1:
        n = int(round(len(x) * SR16 / float(sr)))
        x = np.interp(np.linspace(0, 1, n, endpoint=False),
                      np.linspace(0, 1, len(x), endpoint=False), x).astype(np.float32)
    return x


def _frames(x):
    n = (len(x) - WIN) // HOP + 1
    if n <= 0:
        return None, None
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    return x[idx], n


def _spec(fr):
    w = np.hanning(WIN).astype(np.float32)
    return np.abs(np.fft.rfft(fr * w, axis=1)).astype(np.float32)


def analyze(x):
    """返回 (逐帧判据 dict, 统计 dict)。"""
    fr, n = _frames(x)
    if fr is None:
        return None, {"reason": "太短，分不出帧"}
    S = _spec(fr)
    freqs = np.fft.rfftfreq(WIN, 1.0 / SR16)
    band = (freqs >= BAND_LO) & (freqs <= BAND_HI)
    s = S[:, band]
    fb = freqs[band]
    tot = s.sum(axis=1) + 1e-12
    flat = np.exp(np.log(np.maximum(s, 1e-12)).mean(axis=1)) / (s.mean(axis=1) + 1e-12)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    loud = rms > max(0.004, 0.02 * float(np.percentile(rms, 95)))
    # 主峰
    arg = s.argmax(axis=1)
    fpk = fb[arg]
    rows = np.arange(len(arg))
    near = np.zeros(s.shape, dtype=bool)
    for k in range(-3, 4):
        near[rows, np.clip(arg + k, 0, s.shape[1] - 1)] = True
    share = (s * near).sum(axis=1) / tot
    # 二次谐波能量 / 基频能量
    h2 = np.zeros(len(arg), dtype=np.float32)
    for i in range(len(arg)):
        def e(target):
            m = np.abs(fb - target) <= 40.0
            return float(s[i][m].sum()) + 1e-12
        h2[i] = e(2.0 * fpk[i]) / e(fpk[i])
    hf_m = (freqs >= 2500) & (freqs <= 7000)
    hf = S[:, hf_m].sum(axis=1) / (S.sum(axis=1) + 1e-12)
    flag = (loud & (fpk >= F0_MIN) & (fpk <= F0_MAX) & (flat < FLAT_MAX)
            & (share > PEAK_SHARE_MIN) & (h2 < H2_MAX) & (hf < HF_MAX))
    stats = {"frames": int(len(flag)), "sr": SR16,
             "f_pk_med": round(float(np.median(fpk[loud])) if loud.any() else 0.0, 1),
             "flat_p50": round(float(np.median(flat[loud])) if loud.any() else 0.0, 4)}
    return {"flag": flag, "fpk": fpk, "rms": rms, "loud": loud}, stats


def spans_of(info):
    flag = info["flag"]
    fpk = info["fpk"]
    hop_s = HOP / float(SR16)
    runs, i = [], 0
    n = len(flag)
    while i < n:
        if flag[i]:
            j = i
            while j < n and flag[j]:
                j += 1
            runs.append((i, j))
            i = j
        else:
            i += 1
    # 先并掉很短的空隙，再按段长与稳定性筛
    merged = []
    for s, e in runs:
        if merged and (s - merged[-1][1]) * hop_s < GAP_MERGE:
            merged[-1] = (merged[-1][0], e)
        else:
            merged.append((s, e))
    out = []
    for s, e in merged:
        dur = (e - s) * hop_s
        if not (SPAN_MIN <= dur <= SPAN_MAX):
            continue
        seg = fpk[s:e]
        if len(seg) < 2:
            continue
        cents = 1200.0 * np.log2(np.maximum(seg, 1e-3) / max(1e-3, float(np.median(seg))))
        if float(np.std(cents)) > JITTER_MAX_CENTS:
            continue
        out.append((s * hop_s, e * hop_s))
    return out


def _sample_mask(n, sr, spans, xfade_ms=XFADE_MS):
    m = np.zeros(n, dtype=np.float32)
    for s, e in spans:
        a, b = int(s * sr), min(n, int(e * sr))
        if b > a:
            m[a:b] = 1.0
    w = max(1, int(xfade_ms / 1000.0 * sr))
    if w > 1 and m.any():
        k = np.hanning(w * 2 + 1)
        k = k / k.sum()
        m = np.convolve(m, k, mode="same").astype(np.float32)
        m = np.clip(m * 1.6, 0.0, 1.0)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--rvc", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="keep", choices=("keep", "mute", "off"))
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    rep = {"ok": False, "mode": a.mode}
    try:
        x = _read_mono16(a.src)
        rvc, sr_r = sf.read(a.rvc, dtype="float32", always_2d=True)
        rvc = rvc.mean(axis=1)
    except Exception as e:
        print("WHISTLE_FAIL 读不了音频：%s" % e)
        return 1
    info, stats = analyze(x)
    rep.update(stats or {})
    if info is None:
        if a.report:
            json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("WHISTLE_SKIP %s" % stats.get("reason"))
        return 0

    spans = spans_of(info)
    voiced_secs = float(info["loud"].sum()) * HOP / float(SR16)
    total_secs = len(x) / float(SR16)
    prot = float(sum(e - s for s, e in spans))
    rep.update({"voiced_secs": round(voiced_secs, 2), "total_secs": round(total_secs, 2),
                "protected_secs": round(prot, 2),
                "spans": [[round(s, 2), round(e, 2)] for s, e in spans]})
    reliable = True
    if prot > MAX_VOICED_RATIO * max(voiced_secs, 1e-9):
        reliable = False
        rep["reject"] = ("圈出 %.2f 秒 > 有声时长 %.0f%% —— 判据不可靠，整段撤销"
                         % (prot, MAX_VOICED_RATIO * 100))
    elif prot > MAX_TOTAL_RATIO * max(total_secs, 1e-9):
        reliable = False
        rep["reject"] = ("圈出 %.2f 秒 > 全曲 %.0f%% —— 判据不可靠，整段撤销"
                         % (prot, MAX_TOTAL_RATIO * 100))
    rep["reliable"] = reliable
    if not reliable:
        spans = []
        rep["protected_secs"] = 0.0
        rep["spans"] = []

    if a.mode == "off" or not spans:
        rep["ok"] = True
        if a.report:
            json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("WHISTLE_SKIP protected=0 reason=%s" % (rep.get("reject") or "没找到口哨段"))
        return 0

    # ★ 输出必须跟**变声轨的采样率**（16k 只是分析用的）：整条链下游是 44.1k/48k，
    #   这里若按 16k 写盘，后面混音会把整首歌拉成慢速（这个坑在别处踩过）。
    sr_out = int(sr_r) if sr_r else SR16
    x_out = x
    if sr_out != SR16 and len(x) > 1:
        n2 = int(round(len(x) * sr_out / float(SR16)))
        x_out = np.interp(np.linspace(0, 1, n2, endpoint=False),
                          np.linspace(0, 1, len(x), endpoint=False), x).astype(np.float32)
    n = min(len(x_out), len(rvc))
    m = _sample_mask(n, sr_out, spans)
    if a.mode == "keep":
        out = rvc[:n] * (1.0 - m) + x_out[:n] * m
    else:
        out = rvc[:n] * (1.0 - m)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    sf.write(a.out, out.astype(np.float32), sr_out)
    rep["ok"] = True
    if a.report:
        json.dump(rep, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("WHISTLE_DONE mode=%s protected=%.2f spans=%d f_pk_med=%.0f flat_p50=%.3f"
          % (a.mode, prot, len(spans), float(rep.get("f_pk_med") or 0),
             float(rep.get("flat_p50") or 0)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
