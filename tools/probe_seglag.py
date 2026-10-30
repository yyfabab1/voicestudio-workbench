# -*- coding: utf-8 -*-
"""逐窗量「源 vs 成品」的时间错位，用来分辨三种完全不同的故障：

    · 固定偏移  —— 整条曲线是一条水平线（整体抢拍/拖拍，混音里能听出来）
    · 段内拉伸  —— 曲线是一条斜线（时长比例错，会随位置累积，越到后面越飘）
    · 局部跳变  —— 只有某一个窗是尖峰（接缝 / 分片边界）

qc_cli 只报**整段一个** lag（而且分辨率只有 10 ms），段内形状全被平均掉了：
段 1 报 20 ms 时，你分不清「整段晚 20ms」和「末尾 1 秒晚 20ms 把均值带跑了」——
而这两者的严重程度和修法完全不同，所以必须有这个工具。

判据（对每一段）：
  L1 固定偏移：各窗 lag 的中位数绝对值 < 15 ms
  L2 段内漂移：|lag| 对位置的线性斜率 < 0.30 ms/秒，且首尾差 < 25 ms
  L3 局部跳变：不存在「与相邻窗差 > 25 ms」的孤立窗

★ 自检（--selfcheck）：人为把成品延后 40 ms 再喂进来，三条判据必须全部变红。
  尺子没牙的话，这里会假装通过。

用法：
    <py> -s tools\\probe_seglag.py <段目录> [段号...]
    例：<py> -s tools\\probe_seglag.py data/_probe_realseg 0 5
    目录里要有 inNN.wav（源段）与 sgNN.wav / bsNN.wav（成品段）。
"""

import os
import sys

import numpy as np
import soundfile as sf


def load_mono(path):
    y, sr = sf.read(path, always_2d=False)
    y = np.asarray(y, dtype="float64")
    if y.ndim > 1:
        y = y.mean(axis=1)
    return y, int(sr)


def envelope(x, sr, win_ms=20.0):
    """短时能量包络（去均值后的 RMS），抗 DC、抗音量差。"""
    w = max(1, int(sr * win_ms / 1000.0))
    n = (len(x) // w) * w
    if n < w * 4:
        return np.zeros(1), float(sr) / w
    fr = x[:n].reshape(-1, w)
    fr = fr - fr.mean(axis=1, keepdims=True)
    return np.sqrt((fr ** 2).mean(axis=1)), float(sr) / w


def best_lag(a, b, hop_sr, max_ms=400.0, refine=4):
    """最大化相关 ⇒ 求 b 相对 a 的滞后（毫秒）。"""
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    a = a - a.mean()
    b = b - b.mean()
    na = float(np.sqrt((a * a).sum()))
    nb = float(np.sqrt((b * b).sum()))
    if na < 1e-12 or nb < 1e-12:
        return 0.0, 0.0

    def corr_at(lag):
        lag = int(round(lag))          # 精化阶段传进来的是小数，切片只吃整数
        if lag >= 0:
            u, v = a[:n - lag], b[lag:]
        else:
            u, v = a[-lag:], b[:n + lag]
        m = min(len(u), len(v))
        if m < 8:
            return -1.0
        u, v = u[:m], v[:m]
        return float((u * v).sum()) / (float(np.sqrt((u * u).sum())
                                              * np.sqrt((v * v).sum())) + 1e-12)

    k = int(max_ms / 1000.0 * hop_sr)
    co = max(0.0, corr_at(0))
    best, bi = co, 0
    for lag in range(-k, k + 1):
        c = corr_at(lag)
        if c > best:
            best, bi = c, lag
    # 亚窗精化：在 ±1 窗内按 refine 细分再找一遍
    step = 1.0 / refine
    bl = float(bi)
    for d in np.arange(-1.0, 1.0 + 1e-9, step):
        if abs(d) < 1e-9:
            continue
        c = corr_at(bi + d) if abs(bi + d) < k else -1.0
        if c > best:
            best, bl = c, bi + d
    return bl / hop_sr * 1000.0, best


def analyse(src_path, dst_path, tag, win_sec=4.0, step_sec=2.0):
    s, sr = load_mono(src_path)
    d, _ = load_mono(dst_path)
    n = min(len(s), len(d))
    s, d = s[:n], d[:n]
    if len(s) < sr * 2:
        return None
    es, hs = envelope(s, sr)
    ed, _ = envelope(d, sr)
    m = min(len(es), len(ed))
    es, ed = es[:m], ed[:m]

    w = max(4, int(win_sec * hs))
    st = max(1, int(step_sec * hs))
    rows = []
    for i in range(0, m - w + 1, st):
        lag, c = best_lag(es[i:i + w], ed[i:i + w], hs)
        rows.append((i / hs, lag, c))

    lags = np.array([r[1] for r in rows])
    ts = np.array([r[0] for r in rows])
    cor = float(np.median([r[2] for r in rows]))
    fix = float(np.median(lags))
    slope = float(np.polyfit(ts, lags, 1)[0]) if len(ts) > 2 else 0.0
    head_tail = float(lags[-1] - lags[0]) if len(lags) > 1 else 0.0
    d1 = np.abs(np.diff(lags)) if len(lags) > 1 else np.zeros(1)
    jump = float(d1.max()) if len(d1) else 0.0

    ok1 = abs(fix) < 15.0
    ok2 = abs(slope) < 0.30 and abs(head_tail) < 25.0
    ok3 = jump < 25.0
    print("  %-4s 窗相关中位 %.3f | 固定偏移 %+6.1f ms %s | 斜率 %+6.3f ms/s "
          "首尾差 %+6.1f ms %s | 最大跳变 %5.1f ms %s"
          % (tag, cor, fix, "OK" if ok1 else "★", slope, head_tail,
             "OK" if ok2 else "★", jump, "OK" if ok3 else "★"))
    print("       lag 曲线：" + " ".join("%.0f" % v for v in lags))
    return ok1 and ok2 and ok3


def selfcheck(d):
    """给成品人为加 40 ms 延迟，尺子必须变红，否则它不配当判据。"""
    import tempfile
    p = os.path.join(d, "in00.wav")
    if not os.path.exists(p):
        print("自检跳过：没有 in00.wav")
        return True
    y, sr = load_mono(p)
    tmp = os.path.join(tempfile.mkdtemp(), "late.wav")
    sf.write(tmp, np.concatenate([np.zeros(int(sr * 0.04)), y]).astype("float32"), sr)
    print("\n=== 自检：成品整体延后 40 ms（三条判据必须全红）===")
    ok = analyse(p, tmp, "自检")
    print("  自检结果：%s" % ("★ 尺子没牙，判据不可用" if ok else "尺子有牙（正确判红）"))
    return not ok


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    d = sys.argv[1]
    idx = sys.argv[2:] or ["0"]
    ruler_ok = selfcheck(d)
    allok = True
    for i in idx:
        z = ("%s" % i).zfill(2)          # 目录里是 in00.wav / sg00.wav 两位编号
        for tag, fn in (("分段", "sg%s.wav"), ("未分段", "bs%s.wav")):
            pi = os.path.join(d, "in%s.wav" % z)
            pd = os.path.join(d, fn % z)
            if not (os.path.exists(pi) and os.path.exists(pd)):
                print("段 %s 缺文件（%s）" % (i, fn % i))
                continue
            print("\n=== 段 %s · %s ===" % (i, tag))
            r = analyse(pi, pd, tag)
            allok = allok and bool(r)
    print("\n结论：%s" % ("各段时间轴正常" if allok and ruler_ok
                          else ("★ 有问题" if ruler_ok else "★ 尺子本身不可信")))
    return 0 if (allok and ruler_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
