# -*- coding: utf-8 -*-
r"""tools/probe_drift.py —— 量「成品相对源」的时间轴漂移随位置的分布。

为什么要有它：
    前面已经查出：同一段素材，放进整首跑 vs 单独跑，偏差 IQR 是 54.0 vs 7.1 cents。
    同时 qc_cli 在整首模式下报 0s 处 lag=0.0ms、170s 处 lag=-70.0ms ⇒ **错位会累积**。
    但"累积"有两种完全不同的形状，修法南辕北辙：
      · 线性拉伸（时长比例错了）：整条轴被均匀地缩放 ⇒ 一次重采样就能修好；
      · 每片跳变（分片边界各自 reset）：形状是锯齿 ⇒ 必须改分片/拼接逻辑。
    不量清楚就动手，会改错地方。

怎么量：逐窗（默认 20 秒，步长 10 秒）算 vocal 与 voice 的**包络互相关**峰值位置。
    ★ 为什么用能量包络而不是 f0 曲线找 lag —— qc_cli.py 里栽过一次真坑：
      旋律轮廓是平滑的，两条平滑曲线错开一点相关系数不降反升，"取相关最大"天然
      偏向错位位置（实测挑到 -130ms）。能量包络/唱-不唱判决是方波，抹不动，可靠。

用法（纯 CPU）：
    python tools/probe_drift.py <源vocal.wav> <成品voice.wav> [--win 20] [--step 10]

退出码：0 成功；2 参数错误。
"""
import argparse
import os
import sys

import numpy as np
import soundfile as sf


def load_mono(path):
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    return np.asarray(x, dtype=np.float64), int(sr)


def envelope(x, sr, win_ms=20.0):
    """短时能量包络（去均值），返回 (env, hop_sec)。"""
    w = max(1, int(sr * win_ms / 1000.0))
    n = len(x) // w
    if n < 4:
        return None, None
    e = np.sqrt(np.mean(x[:n * w].reshape(n, w) ** 2, axis=1))
    e = np.nan_to_num(e, nan=0.0)
    e = np.maximum(e, 0.0)
    e = e - e.mean()
    if e.std() > 1e-12:
        e = e / e.std()
    return e, (w / float(sr))


def best_lag(a, b, hop, max_ms=400.0, refine=4):
    """用互相关找 b 相对 a 的滞后（毫秒）。正 = b 比 a 晚。

    先在 hop 粒度上找粗解，再用抛物线插值细化到 hop/refine。
    """
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    k = int(max_ms / 1000.0 / hop)
    k = max(1, min(k, n // 2))
    best, bi = -1e18, 0
    for lag in range(-k, k + 1):
        if lag >= 0:
            u, v = a[:n - lag], b[lag:]
        else:
            u, v = a[-lag:], b[:n + lag]
        if len(u) < 8:
            continue
        c = float(np.dot(u, v) / len(u))
        if c > best:
            best, bi = c, lag
    # 抛物线细化
    def corr_at(lag):
        if lag >= 0:
            u, v = a[:n - lag], b[lag:]
        else:
            u, v = a[-lag:], b[:n + lag]
        return float(np.dot(u, v) / len(u)) if len(u) >= 8 else -1e18
    c0, cm, cp = corr_at(bi), corr_at(bi - 1), corr_at(bi + 1)
    den = (cm - 2 * c0 + cp)
    delta = 0.5 * (cm - cp) / den if abs(den) > 1e-18 else 0.0
    return (bi + delta) * hop * 1000.0, best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--win", type=float, default=20.0)
    ap.add_argument("--step", type=float, default=10.0)
    a = ap.parse_args()
    for p in (a.src, a.dst):
        if not os.path.isfile(p):
            print("不存在：%s" % p)
            return 2

    xs, srs = load_mono(a.src)
    xd, srd = load_mono(a.dst)
    print("源   %.3f 秒 @%d" % (len(xs) / srs, srs))
    print("成品 %.3f 秒 @%d" % (len(xd) / srd, srd))
    print("时长差 %+.1f ms（成品-源）\n" % ((len(xd) / srd - len(xs) / srs) * 1000.0))

    # 统一到 16k 便于逐样本比较
    try:
        import librosa
        xs = librosa.resample(xs, orig_sr=srs, target_sr=16000)
        xd = librosa.resample(xd, orig_sr=srd, target_sr=16000)
        sr = 16000
    except Exception:
        print("需要 librosa 做重采样")
        return 2

    total = min(len(xs), len(xd)) / float(sr)
    print("%-10s %10s %10s" % ("窗口起点", "滞后ms", "相关"))
    print("-" * 34)
    rows = []
    t = 0.0
    while t + a.win <= total:
        i0 = int(t * sr)
        i1 = int((t + a.win) * sr)
        es, hs = envelope(xs[i0:i1], sr)
        ed, hd = envelope(xd[i0:i1], sr)
        if es is not None and ed is not None:
            lag, c = best_lag(es, ed, hs)
            rows.append((round(t, 1), lag, c))
            print("%-10s %10.1f %10.3f" % (round(t, 1), lag, c))
        t += a.step

    if len(rows) >= 4:
        ts = np.asarray([r[0] for r in rows], dtype=float)
        ls = np.asarray([r[1] for r in rows], dtype=float)
        rho = float(np.corrcoef(ts, ls)[0, 1])
        # 线性拟合斜率 = 每 1 秒累积多少毫秒
        sl = float(np.polyfit(ts, ls, 1)[0])
        vals = np.asarray([r[1] for r in rows])
        print("\n★ 位置 vs 滞后 线性相关 rho = %+.3f，斜率 %.3f ms/秒"
              % (rho, sl))
        print("  首窗 %.1f ms → 末窗 %.1f ms，区间跨度 %.1f ms"
              % (ls[0], ls[-1], vals.max() - vals.min()))
        # ★ 判定用「回归斜率 + 拟合优度」，不要用「相邻窗跳变 vs 总跨度」。
        #   第二版踩过：修好之后总跨度只剩 21ms，于是 12.4ms 的相邻跳变就超过了
        #   它的 80%，工具一本正经地报"锯齿/分片跳变"—— 明明是基本对齐了。
        #   残差尺度判据在信号变小后必然失效，改看斜率本身有没有超出容差。
        n = len(ts)
        # R² 用 rho² 近似（单变量线性回归）
        r2 = rho * rho if abs(rho) <= 1.0 else 1.0
        print("  拟合 R² = %.3f（斜率 %.3f ms/秒）" % (r2, sl))
        TOL = 0.15          # 判定阈值：低于它就是"残留抖动"，不会再累积出可听的错位
        if abs(sl) >= TOL and r2 >= 0.5:
            tot = sl * (ts[-1] - ts[0])
            print("  ⇒ **线性累积**（均匀拉伸）：整段累积 %.0f ms，一次按长度重采样即可修正。"
                  % abs(tot))
        elif abs(sl) < TOL:
            print("  ⇒ **基本对齐**：残留斜率 %.3f ms/秒，整段累积最多 %.0f ms，在可听阈值以下。"
                  % (abs(sl), abs(sl) * (ts[-1] - ts[0])))
        else:
            print("  ⇒ 趋势不明确（斜率 %.3f 但拟合只有 R²=%.2f），看上面的表格自己判断。"
                  % (sl, r2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
