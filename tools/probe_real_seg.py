# -*- coding: utf-8 -*-
"""真实歌曲按段质检：分段产物逐段打分，并和「未分段产物」的同一段比。

★ 为什么还要这个
----------------
10 分钟压力测试用的是「同一段素材重复 20 次」的合成音频，好处是每一份难度
一样，坏处也在这儿：素材严格 30 秒一个周期，切点被能量最低处吸到同一个相位，
于是第 2~19 段的长度**恰好都是 30.000 秒**，最后一段恰好是 30.050 秒 ——
真实歌曲不会这么整齐。那个离群的最后一段（86.3%）到底是不是合成素材造出来的
假象，只能回到真实歌曲上验。

判据：
  R1 分段产物**没有哪一段**明显掉队（最低段 ≥ 各段中位数 - 8 个百分点）
  R2 分段产物逐段 vs 未分段产物同一段：均值差 < 2 个百分点（不许整条变糊）

用法：
    <py> -s tools\\probe_real_seg.py <源人声> <分段产物> <未分段产物>
"""

import os
import subprocess
import sys

import numpy as np
import soundfile as sf

RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
RVC_PY = os.path.join(RVC_ROOT, "runtime", "python.exe")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(HERE, "data", "_probe_realseg")
MAXSEG = 30.0


def main():
    # ★ 先转成绝对路径再 chdir：下面会把工作目录切到 RVC 根目录，
    #   相对路径立刻就找不着了（实测报 LibsndfileError: System error）。
    src, seg_wav, base_wav = (os.path.abspath(p)
                              for p in (sys.argv[1], sys.argv[2], sys.argv[3]))
    os.makedirs(WORK, exist_ok=True)
    # ★ 必须在 import rvc_infer 之前把 cwd 切到 RVC 根目录：
    #   rvc_infer 的模块顶层会按 os.getcwd() 拼 rmvpe_root 等环境变量，
    #   在别的目录下 import 会把错误值写进当前进程环境并传给所有子进程。
    #   （2026-10-23 长歌探针踩过：报 FileNotFoundError 找 rmvpe.pt。）
    os.chdir(RVC_ROOT)
    sys.path.insert(0, os.path.join(HERE, "stages"))
    from rvc_infer import plan_cuts

    import librosa
    y16, _ = librosa.load(src, sr=16000, mono=True)
    y16 = np.asarray(y16, dtype="float32")
    total = len(y16) / 16000.0
    bounds = [0.0] + plan_cuts(y16, MAXSEG) + [total]
    n = len(bounds) - 1
    print("源 %.1f 秒 ⇒ %d 段；切点 %s"
          % (total, n, ", ".join("%.2f" % c for c in bounds[1:-1])))

    ys, _ = librosa.load(seg_wav, sr=16000, mono=True)
    yb, _ = librosa.load(base_wav, sr=16000, mono=True)
    L = min(len(ys), len(yb), len(y16))

    rows = []
    for i in range(n):
        s, e = bounds[i], bounds[i + 1]
        a, b = int(s * 16000), int(e * 16000)
        if b - a < 16000:
            continue
        pin = os.path.join(WORK, "in%02d.wav" % i)
        pseg = os.path.join(WORK, "sg%02d.wav" % i)
        pbas = os.path.join(WORK, "bs%02d.wav" % i)
        sf.write(pin, y16[a:b], 16000)
        sf.write(pseg, ys[a:min(b, L)], 16000)
        sf.write(pbas, yb[a:min(b, L)], 16000)

        def qc(p):
            j = p.replace(".wav", ".json")
            r = subprocess.run([RVC_PY, "-s", os.path.join(HERE, "stages", "qc_cli.py"),
                                pin, p, j], cwd=RVC_ROOT, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            try:
                import json
                return json.loads(r.stdout.strip().splitlines()[-1])
            except Exception:
                return {}
        qs, qb = qc(pseg), qc(pbas)
        rows.append({
            "i": i + 1, "sec": e - s,
            "sm": qs.get("match_ratio"), "bm": qb.get("match_ratio"),
            "si": qs.get("dev_iqr_cents"), "bi": qb.get("dev_iqr_cents"),
            "sc": qs.get("contour_corr"), "bc": qb.get("contour_corr"),
        })

    print("\n段  时长    分段吻合%  未分段吻合%   差   分段IQR  未分段IQR  分段轮廓")
    for r in rows:
        f = lambda v, p="%.1f": (p % v) if isinstance(v, (int, float)) else "-"
        print(" %d  %5.1f     %6s      %6s   %+5s   %6s    %6s     %s"
              % (r["i"], r["sec"],
                 f(r["sm"] * 100 if r["sm"] else None),
                 f(r["bm"] * 100 if r["bm"] else None),
                 f((r["sm"] - r["bm"]) * 100 if (r["sm"] and r["bm"]) else None),
                 f(r["si"]), f(r["bi"]), f(r["sc"], "%.3f")))

    ms = np.array([r["sm"] for r in rows if r["sm"]], dtype="float64")
    bs = np.array([r["bm"] for r in rows if r["bm"]], dtype="float64")
    if len(ms) < 3:
        print("段太少")
        return 2
    med = float(np.median(ms))
    low, li = float(ms.min()), int(np.argmin(ms)) + 1
    ok1 = (med - low) < 0.08
    print("\nR1 分段各段：中位 %.1f%%，最低 %.1f%%（第 %d 段），差 %.1f 点 ⇒ %s（阈值 8 点）"
          % (med * 100, low * 100, li, (med - low) * 100, "OK" if ok1 else "★ 有段掉队"))
    dm = float(np.mean(ms) - np.mean(bs))
    ok2 = abs(dm) < 0.02
    print("R2 分段 vs 未分段（同段同口径）：均值 %.1f%% vs %.1f%%，差 %+.1f 点 ⇒ %s（阈值 2 点）"
          % (np.mean(ms) * 100, np.mean(bs) * 100, dm * 100, "OK" if ok2 else "★ 整条变糊"))
    print("\n结论：%s" % ("真实歌曲分段后无掉队段、与不分段同一水平" if (ok1 and ok2)
                        else "★ 真实歌曲分段后有问题"))
    return 0 if (ok1 and ok2) else 1


if __name__ == "__main__":
    sys.exit(main())
