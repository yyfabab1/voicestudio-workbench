r"""
Seed-VC 的配套小工具（跑在 RVC 的 python 下，只用 numpy/scipy/soundfile）。

为什么独立成一个 CLI 而不是在 studio.py 里直接写：studio 跑在 GSV 的 python 里，
那套环境没有 soundfile/scipy；而 Seed-VC 训练/推理又跑在 RVC 的 python 里。
用子进程各跑各的，两边谁也不 import 谁（这个项目的三条环境纪律之一）。

三个子命令：
  prep    把角色素材整理成 Seed-VC 的训练集目录
  prompt  把角色素材拼成一条长参考音频（推理时的 target = 音色来源）
  cross   交叉合成：低频取 RVC 干声（音色/共振峰），高频取原唱（辅音/气声）

stdout 只输出**机器可读的那一行**（PREP/PROMPT/CROSS 开头），状态与说明走 stdout
也一样，但调用方只认那一行 —— 别把进度噪声混进去。
"""
import argparse
import glob
import json
import math
import os
import shutil
import sys

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

# ---- cross 的参数（跟 SoulX-Singer/cross_synth.py 一致）-------------------
N_FFT = 2048
HOP = 512
DEFAULT_FC = 3200.0      # 分频点：以下取 A（RVC），以上取 B（原唱）
DEFAULT_FB = 1200.0      # 过渡带宽度


def _load(path, sr_out=None):
    a, sr = sf.read(path)
    if a.ndim > 1:
        a = a.mean(1)
    a = np.asarray(a, dtype=np.float64)
    if sr_out and sr != sr_out:
        g = math.gcd(int(sr), int(sr_out))
        a = resample_poly(a, sr_out // g, sr // g)
        sr = sr_out
    return a, sr


def _stft(a):
    win = np.hanning(N_FFT + 1)[:N_FFT]
    pad = N_FFT
    ap = np.concatenate([np.zeros(pad), a, np.zeros(pad)])
    nfr = 1 + (len(ap) - N_FFT) // HOP
    S = np.empty((nfr, N_FFT // 2 + 1), dtype=np.complex128)
    for i in range(nfr):
        seg = ap[i * HOP:i * HOP + N_FFT]
        S[i] = np.fft.rfft(seg * win)
    return S, win, len(ap)


def _istft(S, win, total_len):
    n = total_len
    y = np.zeros(n)
    wsum = np.zeros(n)
    for i in range(S.shape[0]):
        seg = np.fft.irfft(S[i])[:N_FFT]
        s = i * HOP
        y[s:s + N_FFT] += seg * win
        wsum[s:s + N_FFT] += win ** 2
    y = y / np.maximum(wsum, 1e-8)
    return y[N_FFT:n - N_FFT]


# ---------------------------------------------------------------- prep

def cmd_prep(a):
    """把角色的切片拷成 Seed-VC 的训练集。

    过滤条件沿用阿塔尼斯那次实测用的口径（dur>=2.0 且 voiced>=0.60）：
    · 太短的段（<2s）模型学不到稳定的音色，只会把噪声当特征学进去
    · voiced 是有声比例，太低说明这段大半是静音/环境噪声
    191 段 / 16.1 分钟那次的结论：微调有效且没付「咬字变差」的代价。

    ★ 只**新增/覆盖**，不删目录里的旧文件：整目录重建要批量删除（本机有删除
      钩子、而且误删素材不可逆），而切片文件名是稳定的，重跑一次自然就是
      「补进新素材」。
    """
    db = json.load(open(os.path.join(a.vs, "data", "roles.json"), encoding="utf-8"))
    role = None
    for r in db["roles"]:
        if r["id"] == a.role:
            role = r
            break
    if role is None:
        print("PREP_ERR 没有这个角色：%s" % a.role)
        return 2
    slices = os.path.join(a.vs, "data", "roles", a.role, "slices")
    os.makedirs(a.out, exist_ok=True)
    segs = [s for s in role.get("segments", [])
            if (s.get("dur") or 0) >= a.min_dur and (s.get("voiced") or 0) >= a.min_voiced]
    if not segs:
        print("PREP_ERR 没有符合要求的素材（需要 时长≥%.1fs 且 有声比例≥%.2f 的段）"
              % (a.min_dur, a.min_voiced))
        return 3
    if a.max_segs > 0:
        # 按时长从长到短取：同样的数据量下，长段比一堆碎段信息量大
        segs = sorted(segs, key=lambda s: -(s.get("dur") or 0))[:a.max_segs]
    n, total = 0, 0.0
    for i, s in enumerate(segs):
        src = os.path.join(slices, s["file"])
        if not os.path.isfile(src):
            continue
        # 名字统一成 s%03d_原名：Seed-VC 只是 glob 目录里的 wav，名字无所谓，
        # 但保留原名在排错时能一眼看出这段是哪条素材。
        dst = os.path.join(a.out, "s%04d_%s" % (i, os.path.basename(src)))
        if not dst.lower().endswith(".wav"):
            dst += ".wav"
        try:
            shutil.copyfile(src, dst)
            x, sr = sf.read(dst)
            total += (len(x) / float(sr)) if sr else 0.0
            n += 1
        except Exception as e:
            sys.stderr.write("  跳过 %s（读不了：%s）\n" % (s["file"], e))
    if not n:
        print("PREP_ERR 素材文件都不在磁盘上（slices 目录空了？）")
        return 4
    print("PREP n=%d secs=%.1f dir=%s" % (n, total, a.out))
    return 0


# ---------------------------------------------------------------- prompt

def cmd_prompt(a):
    """拼一条长参考音频：推理时它是 target，决定"唱出来是谁的声音"。

    硬顶 25 秒：whisper 编码器 WHISPER_MEL_FRAMES=3000 且 truncation=False，
    超过 30 秒会因 pad 为负直接崩（实测过）。
    取样顺序：先挑「长 + 有声比例高」的段（音色证据最足），不够再放宽门槛。
    """
    db = json.load(open(os.path.join(a.vs, "data", "roles.json"), encoding="utf-8"))
    role = None
    for r in db["roles"]:
        if r["id"] == a.role:
            role = r
            break
    if role is None:
        print("PROMPT_ERR 没有这个角色：%s" % a.role)
        return 2
    slices = os.path.join(a.vs, "data", "roles", a.role, "slices")
    allseg = list(role.get("segments", []))
    cand = []
    for min_dur, min_voiced in ((3.0, 0.75), (2.0, 0.60), (1.2, 0.40)):
        cand = [s for s in allseg
                if (s.get("dur") or 0) >= min_dur and (s.get("voiced") or 0) >= min_voiced]
        cand.sort(key=lambda s: -((s.get("dur") or 0) * (s.get("voiced") or 0)))
        if sum(min(s.get("dur") or 0, 12.0) for s in cand) >= a.max_sec:
            break
    chunks, used, total, out_sr = [], [], 0.0, None
    for s in cand:
        p = os.path.join(slices, s["file"])
        if not os.path.isfile(p):
            continue
        try:
            x, sr = sf.read(p)
        except Exception:
            continue
        if x.ndim > 1:
            x = x.mean(1)
        x = np.asarray(x, dtype=np.float32)
        # 以第一段为准统一采样率，不一致的直接跳过（混着拼会在接缝处爆音）
        if out_sr is None:
            out_sr = sr
        elif sr != out_sr:
            continue
        room = a.max_sec - total
        if room <= 0.3:
            break
        if len(x) > room * sr:
            x = x[:int(room * sr)]
        chunks.append(x)
        gap = np.zeros(int(0.20 * sr), dtype=np.float32) if chunks[:-1] else None
        if gap is not None:
            chunks.insert(-1, gap)
            total += 0.20
        total += len(x) / float(sr)
        used.append(s["file"])
        if total >= a.max_sec - 0.3:
            break
    if not chunks or out_sr is None:
        print("PROMPT_ERR 拼不出参考音频（素材文件都不在？）")
        return 3
    y = np.concatenate(chunks).astype(np.float32)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    sf.write(a.out, y, int(out_sr))
    print("PROMPT secs=%.2f sr=%d segs=%d out=%s"
          % (len(y) / float(out_sr), int(out_sr), len(used), a.out))
    return 0


# ---------------------------------------------------------------- cross

def cmd_cross(a):
    """交叉合成：低频取 A（RVC 干声，音色/共振峰），高频取 B（原唱，辅音与气声）。

    为什么要它（两个失败中间态逼出来的实测结论）：
      · source = 原唱人声   → 咬字清晰，但原唱残留高，听得出原唱味
      · source = RVC 干声   → 残留低，但糊（RVC 声码器把谐波做得过于规整）
    两者的缺陷落在**不同的频带**上，所以按频带拼：相位统一用 A 的。
    """
    A, sr = _load(a.a)
    B, _ = _load(a.b, sr_out=sr)
    n = min(len(A), len(B))
    A, B = A[:n], B[:n]
    # 响度对齐到 A：两边振幅差太多时，拼接后的频响会整体偏向响的那一边
    ra = float(np.sqrt((A ** 2).mean()))
    rb = float(np.sqrt((B ** 2).mean()))
    if rb > 1e-9 and ra > 1e-9:
        B = B * (ra / rb)
    Sa, win, tl = _stft(A)
    Sb, _, _ = _stft(B)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    w = 1.0 / (1.0 + np.exp(-(freqs - a.fc) / max(1.0, a.fb / 4.0)))
    m = np.abs(Sa) * (1.0 - w) + np.abs(Sb) * w
    y = _istft(m * np.exp(1j * np.angle(Sa)), win, tl)[:n]
    pk = float(np.abs(y).max())
    if pk > 1e-6 and float(np.abs(A).max()) > 1e-6:
        y = y * (float(np.abs(A).max()) / pk)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    sf.write(a.out, y.astype(np.float32), int(sr))
    print("CROSS sr=%d secs=%.2f fc=%.0f fb=%.0f out=%s"
          % (sr, len(y) / float(sr), a.fc, a.fb, a.out))
    return 0


# ---------------------------------------------------------------- 信息

def cmd_list(a):
    """列出某个 run 目录下已经有的档位（给界面用）。"""
    pats = (os.path.join(a.run, "DiT_epoch_*_step_*.pth"), os.path.join(a.run, "ft_model.pth"))
    rows = []
    for p in pats:
        for f in sorted(glob.glob(p)):
            rows.append(f)
    print("LIST " + json.dumps([os.path.basename(x) for x in rows]))
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("prep")
    p.add_argument("--vs", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--min-dur", type=float, default=2.0)
    p.add_argument("--min-voiced", type=float, default=0.60)
    p.add_argument("--max-segs", type=int, default=400)

    p = sub.add_parser("prompt")
    p.add_argument("--vs", required=True)
    p.add_argument("--role", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--max-sec", type=float, default=25.0)

    p = sub.add_parser("cross")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("out")
    p.add_argument("fc", type=float, nargs="?", default=DEFAULT_FC)
    p.add_argument("fb", type=float, nargs="?", default=DEFAULT_FB)

    p = sub.add_parser("list")
    p.add_argument("--run", required=True)

    a = ap.parse_args()
    if a.cmd == "prep":
        return cmd_prep(a)
    if a.cmd == "prompt":
        return cmd_prompt(a)
    if a.cmd == "cross":
        return cmd_cross(a)
    if a.cmd == "list":
        return cmd_list(a)
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
