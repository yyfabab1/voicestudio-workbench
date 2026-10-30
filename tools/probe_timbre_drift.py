#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""沿时间轴量**音色漂移** —— 用来回答「是不是越唱越不像这个角色」。

为什么要写它
------------
之前所有尺子（devIQR / 八度错 / 吻合率 / 轮廓相关）测的都是**音准**。
但老板说的「越唱越差」很可能不是跑调，而是：
  (a) 音色漂走了 —— 后半段不再像前半段那个人；
  (b) 角色感丢失 —— 后半段越来越接近**原唱**（这个抱怨历史上反复出现过）。
这两种都测不到音准，必须用说话人嵌入（eres2net 的 sv 头，192 维）来量。

用法（cwd / 第一个参数必须是 GPT-SoVITS 整合包根目录，TK 里的加载路径是相对的）：
  runtime\\python.exe -s <本脚本> <整合包根目录> <成品.wav> [源vocal.wav] [输出json]

输出：每段相对「成品第 1 段」的余弦相似度，以及相对「源同一段」的相似度。
"""
import json
import os
import sys

import numpy as np

PKG_ROOT = os.path.abspath(sys.argv[1])
DST = os.path.abspath(sys.argv[2])
SRC = os.path.abspath(sys.argv[3]) if len(sys.argv) > 3 and sys.argv[3] else ""
OUT = sys.argv[4] if len(sys.argv) > 4 else ""

SEG = 10.0        # 每段多长（秒）
STEP = 10.0       # 步长

os.chdir(PKG_ROOT)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)
sys.path.append(os.path.join(os.getcwd(), "GPT_SoVITS", "eres2net"))

import torch  # noqa: E402
import torchaudio  # noqa: E402
from GPT_SoVITS.sv import SV  # noqa: E402

SR = 16000


def load16k(path):
    wav, sr = torchaudio.load(path)
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != SR:
        wav = torchaudio.functional.resample(wav, sr, SR)
    return wav.squeeze(0)


def segments(w, min_secs=1.5):
    """切成 [SEG] 秒一段，STEP 步长；丢掉太短的和几乎没声的最后一段。"""
    out = []
    t = 0.0
    n = int(SEG * SR)
    while t + SEG <= len(w) / float(SR) + 1e-6:
        seg = w[int(t * SR):int(t * SR) + n]
        if len(seg) >= min_secs * SR and float(seg.abs().max()) > 1e-4:
            out.append((round(t, 1), seg))
        t += STEP
    return out


def embed(sv, chunks):
    """一批切片 → sv 向量列表（192 维）。和 feat_cli 完全同一套算法。"""
    import kaldi as Kaldi  # noqa: E402  （eres2net 的 fbank 前端）
    res = []
    B = 8
    for i in range(0, len(chunks), B):
        batch = chunks[i:i + B]
        maxlen = max(c.numel() for c in batch)
        padded = torch.stack(
            [torch.nn.functional.pad(c, (0, maxlen - c.numel())) for c in batch])
        with torch.no_grad():
            _f = torch.stack([
                Kaldi.fbank(w0.unsqueeze(0), num_mel_bins=80,
                            sample_frequency=16000, dither=0)
                for w0 in padded])
            if sv.is_half:
                _f = _f.half()
            s2 = sv.embedding_model.forward(_f.cuda())
        for row in s2.float().cpu().numpy().reshape(len(batch), -1):
            res.append(row)
    return res


def cos(a, b):
    a = np.asarray(a, dtype="float64").reshape(-1)
    b = np.asarray(b, dtype="float64").reshape(-1)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return float("nan")
    return float(np.dot(a, b) / (na * nb))


def main():
    device = "cuda" if torch.cuda.is_available() else "cpu"
    print("设备 %s | 加载 eres2net…" % device)
    sys.stdout.flush()
    sv = SV(device, True)

    wd = load16k(DST)
    print("成品 %.1f 秒" % (len(wd) / float(SR)))
    segs_d = segments(wd)
    print("切成 %d 段（每段 %.0f 秒）" % (len(segs_d), SEG))
    sys.stdout.flush()
    ed = embed(sv, [s for _, s in segs_d])

    es = []
    if SRC and os.path.isfile(SRC):
        ws = load16k(SRC)
        segs_s = segments(ws)
        es = embed(sv, [s for _, s in segs_s])
        print("源  %d 段" % len(es))

    ref_first = ed[0]
    rows = []
    for i, (t, _) in enumerate(segs_d):
        r = {"at": t, "self": cos(ed[i], ref_first)}
        if i < len(es):
            r["src"] = cos(ed[i], es[i])
        rows.append(r)

    print("\n%-8s %14s %14s" % ("at(s)", "vs 成品第1段", "vs 源同段(越低越像角色)"))
    print("-" * 46)
    for r in rows:
        s2 = ("%14.3f" % r["src"]) if "src" in r else "            -"
        print("%-8.1f %14.3f %s" % (r["at"], r["self"], s2))

    sl = np.array([r["self"] for r in rows])
    at = np.array([r["at"] for r in rows])
    if len(rows) >= 3:
        rho = float(np.corrcoef(at, sl)[0, 1])
        print("\n★ 位置 vs「像不像开头的自己」 rho = %+.3f" % rho)
        print("  首段 %.3f → 末段 %.3f（掉 %.3f）" % (sl[0], sl[-1], sl[0] - sl[-1]))
        if rho < -0.7 and (sl[0] - sl[-1]) > 0.08:
            print("  ⇒ **音色漂移坐实**：越到后面越不像开头那个人。")
        else:
            print("  ⇒ 音色沿时间基本稳定（掉幅 %.3f）。" % (sl[0] - sl[-1]))

    if any("src" in r for r in rows):
        ss = np.array([r["src"] for r in rows if "src" in r])
        at2 = np.array([r["at"] for r in rows if "src" in r])
        if len(ss) >= 3:
            rho2 = float(np.corrcoef(at2, ss)[0, 1])
            print("\n★ 位置 vs「像不像原唱」 rho = %+.3f" % rho2)
            print("  首段 %.3f → 末段 %.3f（涨 %.3f）" % (ss[0], ss[-1], ss[-1] - ss[0]))
            if rho2 > 0.7 and (ss[-1] - ss[0]) > 0.05:
                print("  ⇒ **角色感后半丢失**：越唱越滑向原唱的音色。")
            else:
                print("  ⇒ 角色感沿时间基本稳定。")

    if OUT:
        json.dump({"rows": rows, "seg": SEG}, open(OUT, "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("\n写到 %s" % OUT)
    print("DONE")


if __name__ == "__main__":
    main()
