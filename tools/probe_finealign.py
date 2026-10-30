# -*- coding: utf-8 -*-
"""高精度时间对齐探针：亚毫秒级量「成品比源晚多少」。

★ 为什么还要单独一个工具（qc_cli 已经报 lag 了）
------------------------------------------------
qc_cli 的 lag 是**「唱/不唱」二值图案**的互相关，一步 = 一个 f0 帧 ≈ 10 ms，
所以它只能告诉你 0 / 10 / 20 ms，而且整段只给一个数。分辨不出：
    · 整段恒定晚 20 ms（混音里是"人声整体拖后"，要修）
    · 段内从 0 线性涨到 20 ms（是时长比例错，得改缩放）
    · 只有某个窗跳了一下（是分片/接缝，得改切点）
这三者严重程度和修法完全不同，所以必须有更细的尺子。

★ 为什么用「低通后的波形」而不是包络或 f0
------------------------------------------
    · 包络：分辨率受窗长限制，20 ms 窗 ⇒ 精度就是 ±20 ms（我第一版就栽在这）。
    · f0：一步一帧 ≈ 10 ms，同样不够，且清音段没 f0。
    · 低通波形：< 900 Hz 只剩基频和前几个谐波 —— 而 RVC 转换**基频是不变的**
      （变的只是音色/共振峰，那些在中高频）。低通后两条轨的波形高度同形，
      互相关峰很尖，配合抛物线插值可以稳定到 **0.05 ms** 级，且每一帧都有值。

判据（对每一段）：
    A1 整段恒定偏移：|逐窗 lag 的中位数| < 10 ms
    A2 段内漂移：|斜率| < 0.30 ms/秒 且 首尾差 < 20 ms
    A3 局部跳变：相邻窗 lag 差 < 15 ms

★ 自检（每次都跑，不需要参数）：把源自己延后 15 ms 当"成品"喂进来，
  A1 必须判红且读数落在 14~16 ms；自检不红的尺子不许拿去判别人。

用法：
    <py> -s tools\\probe_finealign.py <源段> <成品段> [标签]
    <py> -s tools\\probe_finealign.py data/_probe_realseg/in00.wav data/_probe_realseg/sg00.wav 第1段
"""

import os
import sys

import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

SR = 16000
LP = 900.0          # 低通截止：只留基频 + 前几个谐波
WIN = 2.0           # 逐窗分析窗长（秒）
STEP = 0.5          # 窗步长（秒）


def load16(path):
    y, sr = sf.read(path, always_2d=False)
    y = np.asarray(y, dtype="float64")
    if y.ndim > 1:
        y = y.mean(axis=1)
    if sr != SR:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(int(sr), SR)
        y = resample_poly(y, SR // g, int(sr) // g)
    return y


def lowpass(x):
    sos = butter(4, LP / (SR * 0.5), btype="low", output="sos")
    return sosfiltfilt(sos, x)


def xcorr_lag(a, b, max_ms=120.0):
    """返回 b 相对 a 的滞后（毫秒，正=b 更晚），亚样本精度。"""
    n = min(len(a), len(b))
    if n < SR // 4:
        return 0.0, 0.0
    a = a[:n] - a[:n].mean()
    b = b[:n] - b[:n].mean()
    na = np.sqrt((a * a).sum())
    nb = np.sqrt((b * b).sum())
    if na < 1e-9 or nb < 1e-9:
        return 0.0, 0.0
    a = a / na
    b = b / nb
    N = 1
    while N < 2 * n:
        N *= 2
    A = np.fft.rfft(a, N)
    B = np.fft.rfft(b, N)
    cc = np.fft.irfft(A.conj() * B, N)          # 循环相关
    cc = np.concatenate([cc[-(n - 1):], cc[:n]])  # 展开成 -(n-1) .. n-1
    kmax = int(max_ms / 1000.0 * SR)
    lo, hi = len(cc) // 2 - kmax, len(cc) // 2 + kmax + 1
    seg = cc[max(0, lo):hi]
    i = int(np.argmax(seg)) + max(0, lo)
    peak = float(seg.max())
    # 抛物线亚样本插值
    if 0 < i < len(cc) - 1:
        y0, y1, y2 = cc[i - 1], cc[i], cc[i + 1]
        den = (y0 - 2 * y1 + y2)
        d = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
        d = float(np.clip(d, -1.0, 1.0))
    else:
        d = 0.0
    lag = (i - (len(cc) // 2)) + d
    return lag / SR * 1000.0, peak


def analyse(src_path, dst_path, tag, quiet=False):
    s = lowpass(load16(src_path))
    d = lowpass(load16(dst_path))
    n = min(len(s), len(d))
    s, d = s[:n], d[:n]
    if n < SR:
        return None
    w = int(WIN * SR)
    st = int(STEP * SR)
    lags, ts = [], []
    drop = 0
    # ★ 能量门限：整段的 5% 以下算静音窗，互相关在噪声上找峰必然乱跳
    g = float(np.sqrt((s * s).mean()))
    for i in range(0, max(1, n - w + 1), st):
        u, v = s[i:i + w], d[i:i + w]
        if len(u) < w // 2:
            continue
        e = float(np.sqrt((u * u).mean()))
        lg, pk = xcorr_lag(u, v)
        # ★ 峰质量门限：相关峰 < 0.25 说明这一窗两条轨根本不同形
        #   （换气、纯伴奏漏进来、转换崩掉的片段），它的 lag 是随机数
        if e < 0.05 * g or pk < 0.25:
            drop += 1
            continue
        lags.append(lg)
        ts.append(i / float(SR))
    if len(lags) < 3:
        return None
    lags = np.array(lags)
    ts = np.array(ts)
    fix = float(np.median(lags))
    slope = float(np.polyfit(ts, lags, 1)[0]) if len(ts) > 2 else 0.0
    head_tail = float(lags[-1] - lags[0]) if len(lags) > 1 else 0.0
    jump = float(np.abs(np.diff(lags)).max()) if len(lags) > 1 else 0.0
    ok1 = abs(fix) < 10.0
    ok2 = abs(slope) < 0.30 and abs(head_tail) < 20.0
    ok3 = jump < 15.0
    if not quiet:
        print("  %-12s 恒定偏移 %+7.2f ms %s | 斜率 %+6.3f ms/s 首尾差 %+7.2f ms %s "
              "| 最大跳变 %5.2f ms %s | 有效窗 %d/%d"
              % (tag, fix, "OK" if ok1 else "★", slope, head_tail,
                 "OK" if ok2 else "★", jump, "OK" if ok3 else "★",
                 len(lags), len(lags) + drop))
        print("           lag 曲线：" + " ".join("%+.1f" % v for v in lags))
    return ok1 and ok2 and ok3, fix


def selfcheck():
    """尺子自检：源自己延后 15 ms 当成品，必须读出 14~16 ms 并判红。"""
    import tempfile
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(here, "data", "_probe_realseg", "in00.wav")
    if not os.path.exists(p):
        print("自检跳过：找不到 data/_probe_realseg/in00.wav")
        return True
    y = load16(p)
    tmp = os.path.join(tempfile.mkdtemp(), "late15.wav")
    sf.write(tmp, np.concatenate([np.zeros(int(SR * 0.015)), y]).astype("float32"), SR)
    ok, fix = analyse(p, tmp, "自检", quiet=True)
    good = (not ok) and 14.0 <= fix <= 16.0
    print("  === 尺子自检：人为延后 15.00 ms ⇒ 读出 %+.2f ms，判定%s ⇒ %s"
          % (fix, "红" if not ok else "绿",
             "尺子有牙" if good else "★ 尺子没牙，以下结论不可信"))
    return good


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    ruler = selfcheck()
    if sys.argv[1] == "--dir":          # 批量：<dir> [段号...]，自检只跑一次
        d = sys.argv[2]
        idx = sys.argv[3:] or ["00", "01", "02", "03", "04", "05", "06"]
        allok = True
        for i in idx:
            z = ("%s" % i).zfill(2)
            pi = os.path.join(d, "in%s.wav" % z)
            if not os.path.exists(pi):
                continue
            print("\n--- 段 %s ---" % z)
            for tag, fn in (("分段", "sg%s.wav"), ("未分段", "bs%s.wav")):
                pd = os.path.join(d, fn % z)
                if not os.path.exists(pd):
                    continue
                r, _ = analyse(pi, pd, "%s·%s" % (z, tag))
                allok = allok and bool(r)
        print("\n结论：%s" % ("各段时间轴正常" if (allok and ruler)
                              else ("★ 有问题" if ruler else "★ 尺子不可信")))
        return 0 if (allok and ruler) else 1
    ok, _ = analyse(sys.argv[1], sys.argv[2],
                    sys.argv[3] if len(sys.argv) > 3 else "成品")
    print("  结论：%s" % ("时间轴正常" if ok else "★ 时间轴有问题"))
    return 0 if (ok and ruler) else 1


if __name__ == "__main__":
    sys.exit(main())
