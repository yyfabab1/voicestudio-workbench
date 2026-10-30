r"""
一次性 A/B：**分离模型融合（ensemble）到底有没有用**。

两种尺子同时量，因为单看任何一种都可能被骗：
  1) sep_eval.score（无真值，0~100）—— 主证据，它是不依赖真值的尺子，
     权重经过合成真值校准（见 sep_eval 的文档串）。
  2) SDR（有真值，dB）—— 佐证。真值是**合成**出来的（人声 A + 伴奏 B），
     ★ 它的局限照实写在下面，别把它当绝对真理。

------------------------------------------------------------------ 真值怎么造的，以及为什么它不完美

真值 = 一首歌分离出来的人声  +  **另一首**歌分离出来的伴奏，重新混在一起。
这么造是因为手上没有带分轨（stems）的素材，而"拿某个模型的输出当真值"必然
偏向那个模型。

局限：人声那段来自 bs_roformer 的输出，所以 bs 在这把尺子上**天然占便宜**
（它等于在还原自己）。因此 SDR 这一栏只能用来看**趋势**，不能用来判定
"bs 比 ensemble 强"。真要看 ensemble 值不值，主证据是 score 那一栏，
SDR 只回答"融合之后有没有把人声搞坏"。

------------------------------------------------------------------ 结论（2026-10-22 实测）

**没有收益，且不接入流水线。** 三把尺子一致：

| 方案           | 第1首 score | 第2首 score | 合成真值 SDR |
|----------------|-------------|-------------|--------------|
| 最好的单模型   | **87.6**    | **80.6**    | **10.98 dB** |
| 波形平均 top2  | 86.6        | 80.1        | 10.94 dB     |
| 波形平均 top4  | 83.4        | 78.3        |  9.96 dB     |
| mask 平均 top2 | 80.0        | **66.0**    |  9.69 dB     |
| mask 平均 top4 | 77.4        | 64.2        |  9.06 dB     |

mask 平均**明显有害**（第 2 首掉 14.6 分，leak 从 0.102 涨到 0.366）：
`|V|/|X|` 这个除法在原曲能量小的时频点会把误差放大，clamp 到 1 之后安静段的
噪声被整段保留。而"相位冲突"这个 mask 平均本来要防的问题，在 UVR 这类模型上
**根本不存在** —— 它们是确定性重建，各模型输出的相位是一致的（实测波形平均
没有出现抵消）。相位无关是 **RVC** 那边的需求（它每次生成的相位是随机的），
不是分离的。

因此 `stages/sep_ens.py` 已归档到 `_archive/20261022-ens/`，本脚本把需要的
两个融合函数**内联**了，保证它以后还能独立重跑、独立复现上面的表格。

用法：
    runtime\python.exe -X utf8 _ab_sepens.py
输出：控制台表格 + _ab_sepens.json
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stages"))

GSV_ROOT = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0"
PY = os.path.join(GSV_ROOT, "runtime", "python.exe")
FFMPEG = os.path.join(GSV_ROOT, "runtime", "ffmpeg.exe")

MODELS = [
    ("bs", "model_bs_roformer_ep_317_sdr_12.9755"),
    ("mb", "mel_band_roformer_vocals_becruily"),
    ("hp5", "HP5_only_main_vocal"),
    ("hp2", "HP2_all_vocals"),
]
PROBE_SEC = 30.0

# 素材：两首真实歌曲（各取中间 30 秒）
SONGS = [
    os.path.join(HERE, "data", "music", "Imagine Dragons - Sharks.mp3"),
    os.path.join(HERE, "data", "music", "Marianas Trench - Celebrity Status.mp3"),
]

WORK = os.path.join(HERE, "_ab_ens")
atexit.register(lambda: shutil.rmtree(WORK, ignore_errors=True)
                if os.environ.get("AB_ENS_KEEP") != "1" else None)


def sh(cmd, timeout=1800):
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def clip(src, dst, sec=PROBE_SEC):
    """从中间切一段，2 声道 44100（UVR 的硬要求）。"""
    import subprocess as sp
    p = sp.run('"%s" -v quiet -i "%s" -ac 2 -ar 44100 -f wav -' % (FFMPEG, src),
               shell=True, capture_output=True)
    dur = None
    try:
        import re
        p2 = sp.run('"%s" -i "%s" -f null -' % (FFMPEG, src), shell=True,
                    capture_output=True, text=True, encoding="utf-8", errors="replace")
        m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", p2.stderr or "")
        if m:
            dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        pass
    start = max(0.0, ((dur or sec * 3) - sec) / 2.0)
    rc, out = sh('"%s" -y -v error -i "%s" -ss %.2f -t %.2f -ac 2 -ar 44100 "%s"'
                 % (FFMPEG, src, start, sec, dst))
    return rc == 0 and os.path.isfile(dst)


def uvr(model, in_dir, vdir, idir):
    for d in (vdir, idir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    t0 = time.time()
    rc, out = sh('"%s" -s "%s" "%s" %s "%s" "%s" "%s" 10 wav cuda True'
                 % (PY, os.path.join(HERE, "stages", "uvr_cli.py"), GSV_ROOT,
                    model, in_dir, vdir, idir), timeout=2400)
    vs = [f for f in os.listdir(vdir) if f.lower().endswith(".wav")]
    ins = [f for f in os.listdir(idir) if f.lower().endswith(".wav")]
    if rc == 0 and vs and ins:
        return os.path.join(vdir, vs[0]), os.path.join(idir, ins[0]), time.time() - t0
    return None, None, time.time() - t0


def run_task(tag, mix_wav):
    """对一段混音跑 4 个模型，返回 {短名: (vocal路径, inst路径, 秒)}。"""
    print("\n---- %s : 4 个模型各分一遍 ----" % tag, flush=True)
    base = os.path.join(WORK, tag)
    pin = os.path.join(base, "in")
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(pin, exist_ok=True)
    shutil.copyfile(mix_wav, os.path.join(pin, "source.wav"))
    got = {}
    for short, m in MODELS:
        v, i, sec = uvr(m, pin, os.path.join(base, "v_" + short),
                        os.path.join(base, "i_" + short))
        if not v:
            print("  %-5s 失败" % short, flush=True)
            continue
        got[short] = (v, i, sec)
        print("  %-5s ok  %.0fs" % (short, sec), flush=True)
    return got


# ---------------------------------------------------------------- 融合函数（内联）
# 从归档的 stages/sep_ens.py 抄在这里，让本脚本不依赖任何已删除的模块。
N_FFT = 2048
HOP = 512
EPS = 1e-8


def _stft(x):
    win = np.hanning(N_FFT + 1)[:N_FFT]
    ap = np.concatenate([np.zeros(N_FFT), x, np.zeros(N_FFT)])
    nfr = 1 + (len(ap) - N_FFT) // HOP
    idx = np.arange(nfr)[:, None] * HOP + np.arange(N_FFT)[None, :]
    return np.fft.rfft(ap[idx] * win, axis=1)


def _istft(Z, length):
    win = np.hanning(N_FFT + 1)[:N_FFT]
    nfr = len(Z)
    fr = np.fft.irfft(Z, n=N_FFT, axis=1) * win
    pad_n = (-nfr) % 4
    fr2 = np.concatenate([fr, np.zeros((pad_n, N_FFT))])
    w2 = np.concatenate([np.tile(win, (nfr, 1)), np.zeros((pad_n, N_FFT))])
    nblk = len(fr2) // 4
    g = fr2[:nblk * 4].reshape(nblk, 4, N_FFT)
    gw = w2[:nblk * 4].reshape(nblk, 4, N_FFT)
    total = nblk * N_FFT + 3 * HOP
    buf = np.zeros(total)
    acc = np.zeros(total)
    for k in range(4):
        buf[k * HOP:k * HOP + nblk * N_FFT] += g[:, k, :].ravel()
        acc[k * HOP:k * HOP + nblk * N_FFT] += gw[:, k, :].ravel()
    out = buf / np.maximum(acc, EPS)
    out = out[N_FFT:N_FFT + length]
    if len(out) < length:
        out = np.concatenate([out, np.zeros(length - len(out))])
    return out[:length]


def _load(path):
    a, sr = sf.read(path)
    if a.ndim > 1:
        a = a.mean(axis=1)
    return np.asarray(a, dtype=np.float64), int(sr)


def mask_blend(mix, vocals):
    n = min([len(mix)] + [len(v) for v in vocals])
    mix = mix[:n]
    vocals = [v[:n] for v in vocals]
    X = _stft(mix)
    ax = np.abs(X) + EPS
    ms = [np.clip(np.abs(_stft(v)) / ax, 0.0, 1.0) for v in vocals]
    M = np.mean(np.stack(ms, axis=0), axis=0)
    vocal = _istft(M * X, len(mix))
    return vocal, mix - vocal


def wave_blend(vocals):
    n = min(len(v) for v in vocals)
    return np.mean(np.stack([v[:n] for v in vocals], axis=0), axis=0)


def main():
    import numpy as np
    import soundfile as sf
    import sep_eval

    os.makedirs(WORK, exist_ok=True)
    report = {}

    # ============ 实验 1：真实歌曲，无真值尺子（score） ============
    print("=" * 66)
    print("实验 1  真实歌曲 · sep_eval.score（无真值，主证据）")
    print("=" * 66)
    song_scores = {}
    for si, src in enumerate(SONGS):
        if not os.path.isfile(src):
            print("  素材不存在，跳过：%s" % src)
            continue
        tag = "song%d" % si
        mix = os.path.join(WORK, tag + ".wav")
        if not clip(src, mix):
            print("  切段失败：%s" % src)
            continue
        got = run_task(tag, mix)
        if len(got) < 2:
            continue
        rows = {}
        for short, (v, i, sec) in got.items():
            sc = sep_eval.score(mix, v, i)
            rows[short] = sc
            print("  %-5s score=%5.1f leak=%.3f body=%.3f"
                  % (short, sc["score"], sc["leak"], sc["body_ratio"]), flush=True)

        # 融合方案：按 score 排序取 top-k，以及所有两两组合
        order = sorted(rows, key=lambda k: -rows[k]["score"])
        mix_x, sr = _load(mix)
        vs = {k: _load(got[k][0])[0] for k in got}
        insts = {k: _load(got[k][1])[0] for k in got}
        n = min([len(mix_x)] + [len(v) for v in vs.values()])
        mix_x = mix_x[:n]
        vs = {k: v[:n] for k, v in vs.items()}
        insts = {k: v[:n] for k, v in insts.items()}

        combos = {}
        for k in (2, 3, 4):
            if len(order) >= k:
                combos["mask_top%d" % k] = order[:k]
                combos["wave_top%d" % k] = order[:k]
        for a in range(len(order)):
            for b in range(a + 1, len(order)):
                combos["mask_%s+%s" % (order[a], order[b])] = [order[a], order[b]]

        for name, members in combos.items():
            if name.startswith("wave"):
                vv = wave_blend([vs[m] for m in members])
                ii = mix_x - vv
            else:
                vv, ii = mask_blend(mix_x, [vs[m] for m in members])
            # inst 用什么打分？融合出来的伴奏（保持自洽）
            tmpv = os.path.join(WORK, tag + "_ens_v.wav")
            tmpi = os.path.join(WORK, tag + "_ens_i.wav")
            sf.write(tmpv, vv, sr, subtype="PCM_16")
            sf.write(tmpi, ii, sr, subtype="PCM_16")
            sc = sep_eval.score(mix, tmpv, tmpi)
            rows[name] = sc
            print("  %-16s score=%5.1f leak=%.3f body=%.3f"
                  % (name, sc["score"], sc["leak"], sc["body_ratio"]), flush=True)
        song_scores[tag] = rows
        report.setdefault("songs", {})[tag] = rows

    # ============ 实验 2：合成真值 · SDR ============
    print("\n" + "=" * 66)
    print("实验 2  合成真值（人声A + 伴奏B）· SDR dB")
    print("=" * 66)
    # 先拿到两首歌的人声/伴奏（用 bs）
    parts = {}
    for si, src in enumerate(SONGS[:2]):
        if not os.path.isfile(src):
            continue
        mix = os.path.join(WORK, "song%d.wav" % si)
        if not os.path.isfile(mix):
            if not clip(src, mix):
                continue
        v, i, _ = uvr(MODELS[0][1], os.path.join(WORK, "song%d" % si, "in"),
                      os.path.join(WORK, "gt_v%d" % si), os.path.join(WORK, "gt_i%d" % si))
        if v:
            parts[si] = (v, i)
    if len(parts) >= 2:
        v_a, _ = parts[0]
        _, i_b = parts[1]
        a, sr = _load(v_a)
        b, _ = _load(i_b)
        n = min(len(a), len(b), int(sr * PROBE_SEC))
        true_v = a[:n]
        acc = b[:n]
        # 伴奏压到跟人声差不多响（真实混音里人声通常略突出）
        g = (np.sqrt(np.mean(true_v ** 2)) / (np.sqrt(np.mean(acc ** 2)) + 1e-12)) * 1.3
        syn = true_v + acc * g
        syn_path = os.path.join(WORK, "syn_mix.wav")
        true_path = os.path.join(WORK, "syn_true.wav")
        sf.write(syn_path, syn, sr, subtype="PCM_16")
        sf.write(true_path, true_v, sr, subtype="PCM_16")

        got = run_task("syn", syn_path)
        if len(got) >= 2:
            rows = {}
            for short, (v, i, sec) in got.items():
                rows[short] = {"sdr": sep_eval.sdr(true_path, v)}
                print("  %-5s SDR=%6.2f dB" % (short, rows[short]["sdr"]), flush=True)
            order = sorted(rows, key=lambda k: -rows[k]["sdr"])
            sx, _ = _load(syn_path)
            vs = {k: _load(got[k][0])[0] for k in got}
            n = min([len(sx)] + [len(v) for v in vs.values()])
            sx = sx[:n]
            vs = {k: v[:n] for k, v in vs.items()}
            combos = {}
            for k in (2, 3, 4):
                if len(order) >= k:
                    combos["mask_top%d" % k] = order[:k]
                    combos["wave_top%d" % k] = order[:k]
            for a_ in range(len(order)):
                for b_ in range(a_ + 1, len(order)):
                    combos["mask_%s+%s" % (order[a_], order[b_])] = [order[a_], order[b_]]
            for name, members in combos.items():
                if name.startswith("wave"):
                    vv = wave_blend([vs[m] for m in members])
                else:
                    vv, _ = mask_blend(sx, [vs[m] for m in members])
                tmpv = os.path.join(WORK, "syn_ens.wav")
                sf.write(tmpv, vv, sr, subtype="PCM_16")
                rows[name] = {"sdr": sep_eval.sdr(true_path, tmpv)}
                print("  %-16s SDR=%6.2f dB" % (name, rows[name]["sdr"]), flush=True)
            report["synth"] = rows

    # ============ 汇总 ============
    print("\n" + "=" * 66)
    print("汇总")
    print("=" * 66)
    for tag, rows in report.get("songs", {}).items():
        singles = {k: v["score"] for k, v in rows.items() if "+" not in k and "top" not in k}
        best_single = max(singles.items(), key=lambda kv: kv[1])
        print("\n[%s] 最好单模型：%s = %.1f" % (tag, best_single[0], best_single[1]))
        for k in sorted(rows, key=lambda k: -rows[k]["score"]):
            if k in singles:
                continue
            d = rows[k]["score"] - best_single[1]
            print("    %-16s %5.1f  (%+.1f)" % (k, rows[k]["score"], d))
    if "synth" in report:
        rows = report["synth"]
        singles = {k: v["sdr"] for k, v in rows.items() if "+" not in k and "top" not in k}
        best_single = max(singles.items(), key=lambda kv: kv[1])
        avg_single = sum(singles.values()) / len(singles)
        print("\n[合成真值] 最好单模型：%s = %.2f dB；单模型平均 %.2f dB"
              % (best_single[0], best_single[1], avg_single))
        for k in sorted(rows, key=lambda k: -rows[k]["sdr"]):
            if k in singles:
                continue
            print("    %-16s %6.2f dB  (对比单模型均值 %+.2f)"
                  % (k, rows[k]["sdr"], rows[k]["sdr"] - avg_single))

    json.dump(report, open(os.path.join(HERE, "_ab_sepens.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("\n已写出 _ab_sepens.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
