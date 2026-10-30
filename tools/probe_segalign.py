# -*- coding: utf-8 -*-
"""验证「把段输出按测出来的 lag 校正回源的时间轴」能拿回多少质量。

★ 背景：一段 10 ms 的幽灵
--------------------------
RVC 的分片输出比应有长度多 `window/sr` = 160/16000 = **10 ms**，而我们的
`align_duration` 只把**总长**拉回，**不修片与片之间的相对偏移**。后果是成品
相对源整体晚 10~20 ms（qc_cli 的 lag_ms 就是这样）。

别小看这 10 ms：f0 是一帧 10 ms 算的，错开一帧，在音高变化快的地方逐帧偏差
直接翻几倍。同一段 26 秒素材的实测：

    lag 0 ms（未分段整首处理）  ⇒ 吻合 96.0%  偏差 IQR  7.5 音分
    lag 10~20 ms（分段处理）    ⇒ 吻合 89~94% 偏差 IQR 17.6 音分

★ 更要命的是它让质检数字**抖**：输入长度差 0.01 秒（160 个样本）就能让同一段
内容从 93.6% 掉到 90.0%。上一轮长测里「第 20 段 30.050 秒掉到 86.3%、
裁成 30.000 秒回到 93.2%」也是同一个东西 —— 我一直以为是「末尾多了一点」，
其实是**切片长度碰巧落在了不同的 10 ms 网格上**。

★ 这个工具做什么
------------------
拿已有的「源段 + 成品段」，测出 lag，把成品挪回源的时间轴，再跑一遍 qc，
直接看能拿回多少。零推理成本（复用已有 wav），用来决定值不值得做进产品。

★ 自检：喂一个「源 vs 源自己」的配对，lag 必须 ≈ 0、三条判据必须全绿；
  再人为延后 15 ms，必须读出 14~16 ms 且判红。

用法：
    <py> -s tools\\probe_segalign.py <源段> <成品段> [输出校正后的wav]
"""

import os
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from probe_finealign import SR, load16, lowpass, xcorr_lag      # 复用同一把尺子

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
RVC_PY = os.path.join(RVC_ROOT, "runtime", "python.exe")


def shift_samples(y, d):
    """把 y 前移 d 个样本（d 可小数，线性插值）；末尾用末端值补齐。

    d > 0 表示成品比源晚 ⇒ 要往前挪。挪完末尾空出来的 d 个样本用**末端值**
    补：只有 10 ms 上下，且切点本来就选在换气/安静处，比循环延拓或插零都干净，
    也绝不会引入新的频率成分。
    """
    n = len(y)
    if abs(d) < 1e-6:
        return y
    idx = np.arange(n, dtype="float64") + d
    lo = np.floor(idx).astype("int64")
    hi = lo + 1
    frac = idx - lo
    ok = (lo >= 0) & (hi < n)
    out = np.full(n, y[-1] if n else 0.0, dtype="float64")
    lo_c = np.clip(lo, 0, n - 1)
    hi_c = np.clip(hi, 0, n - 1)
    out = y[lo_c] * (1.0 - frac) + y[hi_c] * frac
    out[~ok] = y[-1] if n else 0.0
    return out


def qc(src, dst):
    j = os.path.join(tempfile.mkdtemp(), "q.json")
    r = subprocess.run([RVC_PY, "-s", os.path.join(HERE, "stages", "qc_cli.py"),
                        src, dst, j], cwd=RVC_ROOT, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    try:
        import json
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src, dst = os.path.abspath(sys.argv[1]), os.path.abspath(sys.argv[2])
    out = sys.argv[3] if len(sys.argv) > 3 else None

    s = lowpass(load16(src))
    d = load16(dst)
    m = min(len(s), len(d))
    lag_ms, pk = xcorr_lag(s[:m], d[:m])
    print("测出成品比源晚 %.2f ms（相关峰 %.3f）" % (lag_ms, pk))

    # ★ 尺子自检：源 vs 自己 ⇒ lag 必须是 0
    z, zp = xcorr_lag(s[:m], s[:m])
    print("  自检①：源 vs 源自己 ⇒ %.2f ms（应为 0）%s" % (z, "OK" if abs(z) < 0.05 else "★ 尺子坏了"))
    late = np.concatenate([np.zeros(int(SR * 0.015)), s[:m]])[:m]
    z2, _ = xcorr_lag(s[:m], late)
    print("  自检②：人为延后 15.00 ms ⇒ 读出 %.2f ms %s"
          % (z2, "OK" if 14.0 <= z2 <= 16.0 else "★ 尺子坏了"))

    before = qc(src, dst)
    if out:
        y, sr = sf.read(dst, always_2d=False)
        y = np.asarray(y, dtype="float64")
        if y.ndim > 1:
            y = y.mean(axis=1)
        # ★ lag 是按 16k 算的，成品可能不是 16k ⇒ 换算成本文件采样率的样本数
        d_smp = lag_ms / 1000.0 * sr
        if np.issubdtype(np.asarray(sf.read(dst, always_2d=False)[0]).dtype, np.integer):
            raw = sf.read(dst, always_2d=False)[0]
        else:
            raw = None
        ya = shift_samples(y, d_smp)
        info = sf.info(dst)
        sf.write(out, ya.astype(getattr(raw, "dtype", np.float32)
                                if raw is not None else np.float32),
                 int(sr), subtype=info.subtype)
        print("  已写出校正后的文件：%s" % out)
        after = qc(src, os.path.abspath(out))
    else:
        after = {}

    def row(tag, j):
        if not j:
            print("  %-10s （质检失败）" % tag)
            return
        print("  %-10s 吻合 %5.1f%%  IQR %5.1f 音分  lag %5.1f ms  轮廓 %.3f"
              % (tag, j["match_ratio"] * 100, j["dev_iqr_cents"],
                 j["lag_ms"], j["contour_corr"]))
    print()
    row("校正前", before)
    row("校正后", after)
    if before and after:
        print("  ⇒ 吻合 %+.1f 点，IQR %+.1f 音分（负=变好）"
              % ((after["match_ratio"] - before["match_ratio"]) * 100,
                 after["dev_iqr_cents"] - before["dev_iqr_cents"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
