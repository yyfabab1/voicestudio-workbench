# -*- coding: utf-8 -*-
r"""非歌声段保护：把「口哨 / 非歌唱音」从 RVC 的嘴里救回来。

## 为什么需要这一步（实测的机制，不是猜的）

RVC 只认 f0 曲线，它分不出「人在唱」和「人在吹口哨」。分离后的人声轨里，
口哨段是一条**极高、极纯**的持续音（实测神耀：f0 662 Hz、谱平坦度 0.0013，
而正常歌唱段是 f0 262 Hz、平坦度 0.19 —— 差了一个数量级）。
RVC 会把它当"唱"，用角色的嗓音重新发出那个音：

    实测（同一段 19 秒，角色 = 男声）：
      正常歌唱段   输出 f0 中位 87 Hz   （= 262 降 19 半音，正确）
      口哨段       输出 f0 中位 220 Hz  （= 662 降 19 半音，数学也对，但那是**高两个八度**）
                    输出/输入 RMS 比 2.97 倍，谱平坦度 0.013 → 0.245（纯音被人声化）

对男声角色来说，220 Hz 的持续音就是用假声在吼。用户听到的"每到吹口哨就吼"
就是这一段。**调 RVC 的参数救不了**，因为参数改不了"口哨不是唱"这件事。

## 做法

用**谱平坦度**（低频靠、窄带 = 纯音）当主判据，f0 当辅助：
逐帧判定「非歌声」，然后**把 RVC 输出里对应的那几段换回原始人声轨**
（口哨不承载角色音色，保留原声是翻唱里的常规做法），边界做 20 ms 交叉淡化。

三种模式：
  keep（默认） 这段用**原始人声轨** —— 口哨还在，只是不再被吼
  mute         这段静音 —— 不想要口哨时用
  off          不动 —— 完全等于旧行为

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器）：
    runtime\python.exe -s <本脚本> --src <原人声轨.wav> --rvc <RVC输出.wav> \
        --out <输出.wav> [--mode keep|mute|off] [--report <json>]

退出码：0 成功；2 参数错误；1 处理失败。
"""
import argparse
import json
import os
import sys
import traceback

import numpy as np

if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))

SR16 = 16000
HOP16 = 160          # 10 ms

# 判据（都在真实素材上量过，见文件头）：
#   谱平坦度 < 0.05  —— 口哨/纯音 0.001~0.03，正常歌唱 0.15~0.30，
#                       清辅音/气声是宽带噪声（平坦度接近 1），不会被误伤。
#   帧数下限 15（0.15 s）—— 滤掉单帧毛刺。
FLAT_MAX = 0.05
MIN_FRAMES = 15
XFADE_MS = 20.0
GAP_MERGE = 0.5         # 相邻两段间隔小于这个秒数就并成一段（口哨音符之间的换气）


def load_mono(path, sr_out):
    import librosa
    y, _ = librosa.load(path, sr=sr_out, mono=True)
    return np.asarray(y, dtype=np.float32)


def frame_flat(x16):
    """逐帧谱平坦度（10 ms 帧 / 10 ms 跳）。"""
    n = 1 + max(0, (len(x16) - 400) // HOP16)
    if n <= 0:
        return np.zeros(0, dtype=np.float32)
    idx = np.arange(400)[None, :] + np.arange(n)[:, None] * HOP16
    fr = x16[idx] * np.hanning(400).astype(np.float32)[None, :]
    spec = np.abs(np.fft.rfft(fr, n=512, axis=1)) + 1e-10
    return (np.exp(np.log(spec).mean(axis=1)) / spec.mean(axis=1)).astype(np.float32)


def median3(a):
    if len(a) < 3:
        return a
    b = a.copy()
    b[1:-1] = np.median(np.stack([a[:-2], a[1:-1], a[2:]]), axis=0)
    return b


def frame_mask(x16, f0=None):
    """返回逐帧布尔（True = 非歌声），以及一组可核对的量。"""
    flat = median3(frame_flat(x16))
    n = len(flat)
    if n == 0:
        return np.zeros(0, dtype=bool), {}
    # 帧能量（判断"这段到底有没有声音"，免得把静音段的低平坦度当口哨）
    nz = 1 + max(0, (len(x16) - 400) // HOP16)
    idx = np.arange(400)[None, :] + np.arange(min(n, nz))[:, None] * HOP16
    fr = x16[idx] * np.hanning(400).astype(np.float32)[None, :]
    rms = np.sqrt((fr ** 2).mean(axis=1) + 1e-12)
    rms = np.pad(rms, (0, max(0, n - len(rms))), constant_values=0.0)[:n]
    # ★ 门槛不能用"高于全曲 rms 的 25 分位"（踩过）：非歌声段自己占比一高，
    #   这个分位数就**落在非歌声段身上**，于是把那一段里"偏轻"的一半帧判成
    #   "没声音、不算"，剩下的是些不足 15 帧的碎片，全被最短段过滤掉 ⇒
    #   一段都判不出来。实测：合成素材「唱3秒-口哨2秒-唱3秒」因此判 0 段，
    #   而真实素材里口哨连续出现时也会踩到同一条。
    #   改成相对**峰值**的小门槛 —— 它只排掉真正的静音（间奏、留白），
    #   跟"非歌声段占多少比例"无关。
    _thr = max(0.005, 0.02 * float(np.percentile(rms, 95)))
    loud = rms > _thr

    m = (flat < FLAT_MAX) & loud
    if f0 is not None and len(f0) >= n:
        f0 = f0[:n]
        # 纯音 + 有声（f0>0）。RMVPE 对纯音有时给 0，所以这里**不强制**要有 f0，
        # 但只要提得到 f0，就要求它也落在"高音区" —— 排除低频乐器的低平坦段。
        m = m & ((f0 <= 0) | (f0 >= 300))

    # 合并成段、滤掉太短的
    def _runs(a, gap_frames):
        runs = []
        i = 0
        n = len(a)
        while i < n:
            if a[i]:
                j = i
                while j < n and a[j]:
                    j += 1
                if j - i >= MIN_FRAMES:      # ★ 先按"单段最短"筛，再合并。
                    runs.append([i, j])      #   反过来的话，两个 0.15 秒的毛刺
                i = j                        #   会被并成一个 0.7 秒的"假段"。
            else:
                i += 1
        # 间隔小的并起来：口哨是由一串音符组成的，音符之间那点换气不该
        # 把整段切成十几块 —— 切碎之后每块都要单独 crossfade，听感上会
        # 反复在"原声/角色"之间横跳，比不处理还怪。
        merged = []
        for r in runs:
            if merged and r[0] - merged[-1][1] <= gap_frames:
                merged[-1][1] = r[1]
            else:
                merged.append(r)
        return merged

    out = np.zeros(n, dtype=bool)
    for i0, i1 in _runs(m, int(round(GAP_MERGE / (HOP16 / float(SR16)))),):
        out[i0:i1] = True
    stats = {
        "frames": int(n),
        "flat_med": round(float(np.median(flat)), 5),
        "flat_p10": round(float(np.percentile(flat, 10)), 5),
        "nonvocal_frames": int(out.sum()),
        "protected_secs": round(float(out.sum()) * HOP16 / float(SR16), 2),
    }
    return out, stats


def segments_of(mask, hop_s):
    segs = []
    i = 0
    n = len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            segs.append([round(i * hop_s, 2), round(j * hop_s, 2)])
            i = j
        else:
            i += 1
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="分离后的人声轨（原声）")
    ap.add_argument("--rvc", required=True, help="RVC 变声输出")
    ap.add_argument("--out", required=True)
    ap.add_argument("--mode", default="keep", choices=["keep", "mute", "off"])
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    import soundfile as sf

    if not (os.path.isfile(a.src) and os.path.isfile(a.rvc)):
        print("[err] 输入文件不存在")
        return 2

    sr = sf.info(a.rvc).samplerate          # 以 RVC 输出的采样率为准（它是主轨）
    rvc = load_mono(a.rvc, sr)
    src = load_mono(a.src, sr)
    n = min(len(rvc), len(src))
    if n < sr:
        print("[err] 音频太短")
        return 1
    rvc, src = rvc[:n], src[:n]

    if a.mode == "off":
        out = rvc
        stats = {"mode": "off", "protected_secs": 0.0, "segments": []}
    else:
        x16 = load_mono(a.src, SR16)
        f0 = None
        try:
            import torch
            from infer.rmvpe import RMVPE
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            rmvpe = RMVPE(os.path.join(os.environ["rmvpe_root"], "rmvpe.pt"),
                          is_half=(dev == "cuda"), device=dev)
            f0 = np.asarray(rmvpe.infer_from_audio(x16, thred=0.03), dtype=np.float32)
        except Exception:
            print("[warn] 提不到 f0，只用谱平坦度判定：%s" % traceback.format_exc(limit=1))
        mask, stats = frame_mask(x16, f0)
        hop_s = HOP16 / float(SR16)
        stats["mode"] = a.mode
        stats["segments"] = segments_of(mask, hop_s)

        # 帧 mask → 样本 mask（每帧覆盖 HOP16/16000 秒）
        spf = max(1, int(round(n * hop_s / max(1e-9, len(mask))))) if len(mask) else n
        smask = np.zeros(n, dtype=np.float32)
        for k, on in enumerate(mask):
            if on:
                s = int(k * hop_s * sr)
                e = min(n, int((k + 1) * hop_s * sr) + spf)
                smask[s:e] = 1.0
        # 平滑 = 自动交叉淡化（窗 ≈ XFADE_MS）
        w = max(1, int(XFADE_MS / 1000.0 * sr))
        kern = np.hanning(w * 2 + 1)
        kern = kern / kern.sum()
        smask = np.convolve(smask, kern, mode="same").astype(np.float32)
        smask = np.clip(smask * 1.6, 0.0, 1.0)     # 补回卷积带来的衰减

        if a.mode == "keep":
            out = rvc * (1.0 - smask) + src * smask
        else:                                       # mute
            out = rvc * (1.0 - smask)

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    sf.write(a.out, out.astype(np.float32), sr)

    print("NONVOCAL mode=%s  采样率 %d  时长 %.2f 秒" % (a.mode, sr, n / float(sr)))
    print("  全曲谱平坦度中位 %.4f（正常歌唱约 0.15~0.30，口哨 <0.05）" % stats.get("flat_med", 0))
    print("  判定为非歌声的时长 %.2f 秒，共 %d 段"
          % (stats.get("protected_secs", 0), len(stats.get("segments") or [])))
    for s, e in (stats.get("segments") or [])[:40]:
        print("    %7.2f ~ %7.2f s（%.2f 秒）" % (s, e, e - s))
    if a.report:
        json.dump(stats, open(a.report, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("NONVOCAL_DONE %.2f" % float(n / float(sr)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
