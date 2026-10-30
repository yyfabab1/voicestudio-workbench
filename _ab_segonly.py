# -*- coding: utf-8 -*-
"""「只转自己分到的那几句」A/B 实测 —— 证明（或证伪）它和整首转换听不出差别。

用法（约 3~5 分钟，会占满显卡，别在这时候打游戏）：
    "D:/RVC/Retrieval-based-Voice-Conversion-WebUI/runtime/python.exe" _ab_segonly.py

做什么：
    A 组：把一整段干声整首转一次（现在的默认做法）
    B 组：只转"分到的那几句"（前后留上下文），再放回原时间轴
    然后在**同一个人的同一段**上，逐样本比对 A 和 B。

怎么算通过（三条都要）：
    ① **幅度谱相关** > 0.90        —— 相位无关。★ 不能用波形相关：同一段音频跑两次
       RVC，波形相关只有 -0.24~0.45（相位/音高轻微抖动就会掉下来），那是尺子错，
       不是音质差。实测同一段 A/B 的谱相关是 0.941 / 0.910。
    ② 四个频段的能量占比差 < 4 个百分点 —— "听起来是不是同一个音色"的粗筛。
       （实测两段是 0.029 / 0.002，阈值留一点余量，别把正常抖动判成红）
    ③ 响度差 < 1.5 dB —— 不会一句大一句小。
    另外报一个**方向性指标**（不算通过条件，但更关键）：
      B~源干声 的谱相关 应当 ≥ A~源干声 - 0.03
      —— 说明"只转几句"没有比"整首转"更偏离原唱。
"""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "stages"))

RVC_PY = r"D:/RVC/Retrieval-based-Voice-Conversion-WebUI/runtime/python.exe"
RVC_ROOT = r"D:/RVC/Retrieval-based-Voice-Conversion-WebUI"
MODEL = "ra500fb92.pth"          # 大帝
INDEX = (r"D:/RVC/Retrieval-based-Voice-Conversion-WebUI/assets/indices/"
         r"ra500fb92_added_IVF1700_Flat_nprobe_1_ra500fb92_v1.index")
SEC = 60.0                        # 取多长来比（越长越准，也越慢）
CHUNK = 20.0


def main():
    import numpy as np
    import soundfile as sf
    import segonly as S

    tmp = os.path.join(ROOT, "_ab_tmp")
    os.makedirs(tmp, exist_ok=True)
    # ---- 找一段现成的干声 ----
    src = None
    import glob
    for p in sorted(glob.glob(os.path.join(ROOT, "data/cache/sep/*/vocal.wav"))):
        d = wav_dur(p)
        if d and d > SEC + 5:
            src = p
            break
    if not src:
        print("找不到够长的人声轨（data/cache/sep/*/vocal.wav）")
        return 1
    print("素材：%s" % src)

    x, sr = sf.read(src, dtype="float32", always_2d=False)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = x[:int(SEC * sr)]
    base = os.path.join(tmp, "src.wav")
    sf.write(base, x, sr, subtype="PCM_16")
    dur = len(x) / float(sr)
    print("取前 %.1f 秒 @ %d Hz" % (dur, sr))

    # 两个人各唱两段：0 号 [2,17] [32,47]，1 号 [17,32] [47,58]
    takes = [(2.0, 17.0, 0), (17.0, 32.0, 1), (32.0, 47.0, 0), (47.0, 58.0, 1)]

    # ---- A 组：整首 ----
    a_out = os.path.join(tmp, "A.wav")
    t = run_rvc(base, a_out)
    print("A 组（整首 %.1f 秒）：%.1f 秒" % (dur, t))
    a, sr_a = sf.read(a_out, dtype="float32", always_2d=False)
    if a.ndim > 1:
        a = a.mean(axis=1)

    # ---- B 组：只转自己那几句 ----
    segs = S.takes_of(takes, 0)
    ok, why, d = S.should_use(takes, 0, dur)
    print("B 组（0 号角色）：%s" % why)
    wins = S.plan_windows(S.merge_segments(segs), dur)
    cat = os.path.join(tmp, "B_in.wav")
    m = S.build_input(base, wins, cat)
    conv = os.path.join(tmp, "B_conv.wav")
    t2 = run_rvc(cat, conv)
    print("B 组（只转 %.1f 秒）：%.1f 秒" % (m["dur"], t2))
    b_out = os.path.join(tmp, "B.wav")
    r = S.scatter(conv, m, dur, b_out)
    b, sr_b = sf.read(b_out, dtype="float32", always_2d=False)
    if b.ndim > 1:
        b = b.mean(axis=1)
    print("  scale=%.4f drift=%.3f 秒" % (r.get("scale", 1), r.get("drift", 0)))

    # ---- 比对（相位无关：谱相关 + 频段能量 + 响度）----
    print("\n=== 在自己唱的每一段上比 A / B ===")
    A2, B2 = rs(A, sr_a, 44100), rs(B, sr_b, 44100)
    S2 = rs(x, sr, 44100) if sr != 44100 else x
    sr = 44100
    rows = []
    for a0, b0 in segs:
        i0, i1 = int((a0 + 0.2) * sr), int((b0 - 0.2) * sr)
        pa, pb, ps = A2[i0:i1], B2[i0:i1], S2[i0:i1]
        c_ab = spec_corr(pa, pb)
        c_as = spec_corr(pa, ps)
        c_bs = spec_corr(pb, ps)
        ba, bb = band_ratio(pa), band_ratio(pb)
        bd = max(abs(ba[k] - bb[k]) for k in ba)
        db = 20 * np.log10((rms(pb) + 1e-9) / (rms(pa) + 1e-9))
        rows.append((a0, b0, c_ab, bd, db, c_as, c_bs))
        print("  [%5.1f~%5.1f 秒] 谱相关 A~B %.4f | 频段最大差 %.3f | 响度差 %+.2f dB"
              % (a0, b0, c_ab, bd, db))
        print("                A~源 %.4f | B~源 %.4f（B 不该明显更低）" % (c_as, c_bs))
    if not rows:
        print("没有可比对的段")
        return 1
    c_min = min(r[2] for r in rows)
    bd_max = max(r[3] for r in rows)
    db_max = max(abs(r[4]) for r in rows)
    drift = max(r[5] - r[6] for r in rows)
    ok = c_min > 0.90 and bd_max < 0.04 and db_max < 1.5 and drift <= 0.03
    print("\n结论：谱相关最低 %.4f（要 >0.90）| 频段最大差 %.3f（要 <0.04）| "
          "响度差最大 %.2f dB（要 <1.5）| B 比 A 更偏离源 %.4f（要 <=0.03）"
          % (c_min, bd_max, db_max, drift))
    print("判定：%s" % ("听着没差别 —— 可以默认开（当前默认就是开）" if ok
                     else "★ 有差别，别默认开"))
    return 0 if ok else 1


def rs(y, s0, s1):
    """简单线性重采样到 s1（只为了能在同一时间轴上比，不追求音质）。"""
    import numpy as np
    if int(s0) == int(s1):
        return y
    n = int(len(y) * float(s1) / s0)
    return np.interp(np.arange(n) * float(s0) / s1,
                     np.arange(len(y)), y).astype("float32")


_NFFT, _HOP = 2048, 512


def _mag(x):
    import numpy as np
    win = np.hanning(_NFFT)
    return np.array([np.abs(np.fft.rfft(x[i:i + _NFFT] * win))
                     for i in range(0, max(1, len(x) - _NFFT), _HOP)])


def spec_corr(p, q):
    """幅度谱相关（相位无关）。两个都太短就返回 0。"""
    import numpy as np
    if len(p) < _NFFT * 2 or len(q) < _NFFT * 2:
        return 0.0
    m1, m2 = _mag(p).ravel(), _mag(q).ravel()
    n = min(len(m1), len(m2))
    m1, m2 = m1[:n] - m1[:n].mean(), m2[:n] - m2[:n].mean()
    d = np.sqrt((m1 * m1).mean()) * np.sqrt((m2 * m2).mean())
    return float((m1 * m2).mean() / d) if d > 1e-9 else 0.0


def band_ratio(p):
    """四个频段占总能量的比例（粗筛音色：低频/人声/中高/高频）。"""
    import numpy as np
    M = _mag(p)
    f = np.fft.rfftfreq(_NFFT, 1.0 / 44100)
    tot = M.sum(axis=1) + 1e-9
    out = {}
    for k, a, b in (("低<300", 0, 300), ("人声300-3k", 300, 3000),
                    ("中高3-8k", 3000, 8000), ("高>8k", 8000, 20000)):
        out[k] = float((M[:, (f >= a) & (f < b)].sum(axis=1) / tot).mean())
    return out


def wav_dur(p):
    import wave
    try:
        w = wave.open(p, "r")
        s = w.getnframes() / float(w.getframerate())
        w.close()
        return s
    except Exception:
        return 0.0


def rms(x):
    import numpy as np
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def corr(a, b):
    import numpy as np
    a = a - a.mean()
    b = b - b.mean()
    da, db = np.sqrt(np.mean(a * a)), np.sqrt(np.mean(b * b))
    if da < 1e-9 or db < 1e-9:
        return 0.0
    return float(np.mean(a * b) / (da * db))


def run_rvc(inp, out):
    import time
    cmd = [RVC_PY, "-s", os.path.join(ROOT, "stages", "rvc_infer.py"),
           "--model", MODEL, "--input", inp, "--output", out,
           "--f0", "rmvpe", "--pitch", "0", "--index-rate", "0.5",
           "--rms-mix-rate", "0.25", "--protect", "0.33",
           "--index", INDEX, "--chunk", str(CHUNK)]
    t0 = time.time()
    p = subprocess.run(cmd, cwd=RVC_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    dt = time.time() - t0
    if p.returncode != 0 or not os.path.isfile(out):
        print("RVC 失败（退出码 %s）：\n%s" % (p.returncode, (p.stdout or "")[-1500:]))
        raise SystemExit(1)
    return dt


if __name__ == "__main__":
    sys.exit(main())
