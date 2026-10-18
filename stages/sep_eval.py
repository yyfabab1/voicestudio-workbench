r"""
分离质量的**量化尺子**（跑在 GSV 的 python 下：那边有 numpy / soundfile / scipy）。

为什么要有这个东西：以前分离是「固定用 Roformer，跑不通就退 HP5」，**跑通就算好**——
分离质量从来没有被量过。于是"漏了一大片伴奏进来"和"把人声削掉一半"这两种
最常见的失败，在日志里长得一模一样（都是"人声轨 xx 秒 / 伴奏轨 xx 秒"），
只有把成品听一遍才知道。而成品难听的时候，锅往往被甩给变声模型。

三个子命令：
  score  <mix> <vocal> <inst>   给一次分离打分（**没有真值**时用，0~100，越高越好）
  sdr    <ref_vocal> <vocal>    有真值时算真 SDR（校准 / 门禁用，dB，越高越好）
  slice  <in> <out> <start> <dur>  切一段音频（给"先试 30 秒"用）

------------------------------------------------------------------ 指标
没有真值时只能问三个问题，这三个加起来基本能分出好坏：

1) **漏了多少伴奏进来（leak）**：人声轨和伴奏轨的**幅度谱相关**。
   两者本该互不相干；漏伴奏时人声轨里会出现一整片跟伴奏同步的能量。
   ⚠ 注意人声和伴奏天然同节奏（同一首歌），所以绝对值不会是 0 ——
     这个量只在**同一首歌的若干个候选之间比**时才有意义，别拿它跨歌比较。

2) **该安静的地方安静不安静（floor）**：人声轨能量包络的 10 分位 / 90 分位（dB）。
   干净的人声在句与句之间应该是**真安静**的；漏了伴奏就变成"一直有底噪"，
   这个比值会明显抬高。这是三个量里判别力最强的一个。

3) **人声还在不在（body）**：人声轨 RMS / 原曲 RMS。
   太低 = 分离器把人声也当伴奏削掉了（overkill）；太高 = 根本没分离（≈原曲）。

综合分：score = 100 * (0.40*干净 + 0.35*安静 + 0.15*饱满 + 0.10*高频不刺)
★ 权重不是拍脑袋定的，是 `_probe_sep_calib.py` 用「合成真值」跑出来的：
  把一份已知的人声和伴奏重新混在一起（真值已知），让几个模型各分一遍，
  再拿各指标跟真 SDR 比排序，谁跟真值排序一致谁拿高权重。

------------------------------------------------------------------ 依赖
只用 numpy + soundfile。GSV 的 runtime 里有；**不要**在这里 import torch
（加载 torch 要十几秒，而这个脚本会被调用很多次）。
"""
import os
import sys

import numpy as np
import soundfile as sf

N_FFT = 2048
HOP = 512


def _load(path):
    a, sr = sf.read(path)
    if a.ndim > 1:
        a = a.mean(axis=1)
    return np.asarray(a, dtype=np.float64), int(sr)


def _stft(a):
    win = np.hanning(N_FFT + 1)[:N_FFT]
    pad = N_FFT
    ap = np.concatenate([np.zeros(pad), a, np.zeros(pad)])
    nfr = 1 + (len(ap) - N_FFT) // HOP
    S = np.empty((nfr, N_FFT // 2 + 1), dtype=np.complex128)
    for i in range(nfr):
        S[i] = np.fft.rfft(ap[i * HOP:i * HOP + N_FFT] * win)
    return S


def _env(a, sr, hop=HOP):
    """能量包络（dB），按 HOP 分帧。"""
    n = max(1, (len(a) - N_FFT) // hop)
    e = np.empty(n)
    for i in range(n):
        seg = a[i * hop:i * hop + N_FFT]
        e[i] = np.sqrt(np.mean(seg * seg)) + 1e-12
    return 20.0 * np.log10(e)


def _corr_median(A, B):
    """两路幅度谱逐帧相关，取中位数。

    为什么不整段算一条相关系数：人声只在部分帧里有，整段算会被"两边都是静音"
    的帧稀释成一个虚高的数。逐帧再取中位数，问的是"典型的那一帧有多像"。
    """
    n = min(len(A), len(B))
    cs = []
    for i in range(n):
        a, b = A[i], B[i]
        sa, sb = a.std(), b.std()
        if sa < 1e-9 or sb < 1e-9:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if np.isfinite(c):
            cs.append(c)
    if not cs:
        return 0.0
    return float(np.median(cs))


def score(mix_path, vocal_path, inst_path):
    mix, sr = _load(mix_path)
    v, _ = _load(vocal_path)
    i, _ = _load(inst_path)
    n = min(len(mix), len(v), len(i))
    mix, v, i = mix[:n], v[:n], i[:n]

    # ---- 1) 漏伴奏：人声轨 vs 伴奏轨的谱相关
    V, I = _stft(v), _stft(i)
    leak = _corr_median(np.abs(V), np.abs(I))
    # 只取"有能量的帧"再算一次：静音帧两边都是 0，会把相关拉到没意义的值
    e_v = np.abs(V).sum(axis=1)
    e_i = np.abs(I).sum(axis=1)
    live = (e_v > 0.05 * np.median(e_v[e_v > 0])) & (e_i > 0)
    leak_live = _corr_median(np.abs(V[live]), np.abs(I[live])) if live.sum() > 8 else leak
    leak = max(leak, leak_live)

    # ---- 2) 该安静的地方安不安静
    # ★★ 这里踩过一次：最早我把"越安静越好"直接当分（floor 越低分越高），
    #   `_probe_sep_calib.py` 用真值一比发现**排序是反的**：HP2 的 floor 最安静
    #   (-98dB) 但真值 SDR 最差(3.40dB)，因为它把人声也一起削掉了 —— "削到没声音"
    #   当然安静。所以下面改成**区间型**：动态范围落在 -40~-70dB 给满分，
    #   太吵（漏伴奏）和太静（削过头）两侧都扣分。
    env = _env(v, sr)
    if len(env) < 10:
        floor_db = -60.0
    else:
        floor_db = float(np.percentile(env, 10) - np.percentile(env, 90))
    quiet = float(np.clip(1.0 - min(abs(floor_db + 55.0) / 45.0, 1.0) * 0.6, 0.0, 1.0))

    # ---- 3) 人声还在不在（相对原曲）
    # ★ 只做"兜底惩罚"，不参与好坏排序：body_ratio 的大小强烈依赖这首歌本身
    #   人声混得有多响（同一首歌不同混音能差一倍），拿它当加分项会把"人声响的歌"
    #   排到前面。它只在明显不对劲时（削没了 / 根本没分离）把分拉下来。
    r = (np.sqrt(np.mean(v * v)) + 1e-12) / (np.sqrt(np.mean(mix * mix)) + 1e-12)
    if r < 0.12:
        body = float(r / 0.12) * 0.5
    elif r > 0.90:
        body = float(max(0.0, 1.0 - (r - 0.90) / 0.30))
    else:
        body = 1.0

    # ---- 4) 高频：漏镲片/齿音会让 6k 以上占比异常高
    f = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    Pv = (np.abs(V) ** 2).mean(axis=0)
    hi = float(Pv[f >= 6000].sum() / max(Pv.sum(), 1e-12))
    # 人声里 6k 以上占比通常 <8%；>25% 基本是镲片漏进来了
    hf = float(np.clip(1.0 - (hi - 0.08) / 0.17, 0.0, 1.0))

    # ★ 权重是 `_probe_sep_calib.py` 用**合成真值**跑出来的，不是拍的：
    #   同一份已知人声+伴奏重新混成一首歌，让几个模型各分一遍，再拿各指标跟真 SDR
    #   比排序 —— leak 是唯一稳定跟真值同序的量（0.057/0.126/0.246 对 SDR
    #   9.21/5.49/3.40），所以给它最大权重；body 只当兜底惩罚。
    # ★ body 是**乘性闸门**，不是加性的一项。
    #   加性时踩过：把人声削到只剩 6%（相当于没声音）会因为"又干净又安静"拿到
    #   高分（实测 66.7 > 干净版 61.5）—— 那种结果根本没法拿来唱歌。改成乘性之后，
    #   人声没了就直接把总分压到两成，再干净也没用。
    total = (100.0 * (0.55 * (1.0 - leak) + 0.25 * quiet + 0.12 * hf + 0.08 * body)
             * (0.2 + 0.8 * body))
    return {"score": round(total, 2), "leak": round(leak, 4),
            "floor_db": round(floor_db, 2), "quiet": round(quiet, 3),
            "body_ratio": round(r, 4), "body": round(body, 3),
            "hf_ratio": round(hi, 4), "hf": round(hf, 3)}


def sdr(ref_path, got_path):
    """真值 SDR（dB）：ref = 真的人声，got = 分离出来的。

    经典 SDR = 10*log10( ||target||^2 / ||error||^2 )，target = 真值在估计上的投影。
    这里做的是"整体增益对齐后的 SDR"（BSS Eval 里叫 SDR 的 scale-invariant 版）：
    不先对齐增益的话，音量差 1dB 就能让 SDR 差好几分贝，那是**音量**不是**质量**。
    """
    ref, sr = _load(ref_path)
    got, _ = _load(got_path)
    n = min(len(ref), len(got))
    ref, got = ref[:n], got[:n]
    a = float(np.dot(got, ref)) / max(float(np.dot(ref, ref)), 1e-12)
    proj = a * ref
    err = got - proj
    num = float(np.dot(proj, proj))
    den = float(np.dot(err, err)) + 1e-12
    return round(10.0 * np.log10(max(num, 1e-12) / den), 3)


def _slice(src, dst, start, dur):
    a, sr = sf.read(src)
    s = int(max(0.0, start) * sr)
    e = int(min(len(a), s + dur * sr))
    sf.write(dst, a[s:e], sr)
    return float((e - s) / float(sr))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    try:
        if cmd == "score" and len(sys.argv) >= 5:
            d = score(sys.argv[2], sys.argv[3], sys.argv[4])
            print("SEP_SCORE " + " ".join("%s=%s" % (k, v) for k, v in d.items()))
            return 0
        if cmd == "sdr" and len(sys.argv) >= 4:
            print("SEP_SDR %.3f" % sdr(sys.argv[2], sys.argv[3]))
            return 0
        if cmd == "slice" and len(sys.argv) >= 6:
            got = _slice(sys.argv[2], sys.argv[3], float(sys.argv[4]), float(sys.argv[5]))
            print("SEP_SLICE secs=%.2f" % got)
            return 0
    except Exception as e:
        print("SEP_ERR %s: %s" % (type(e).__name__, str(e)[:200]))
        return 1
    print("SEP_ERR 参数不对：%s" % " ".join(sys.argv[1:]))
    return 2


if __name__ == "__main__":
    sys.exit(main())
