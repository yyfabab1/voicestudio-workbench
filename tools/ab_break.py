#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把"破音"做成能听的 A/B：同一段、同一套后处理，**只换一个变量**。

为什么不用指标直接定案：破音是听感词，我手上三把尺子（削波 / 谐波性 / 音高跳线）
在老板指的那段上**都不比主歌差**（副歌 6.2% 破帧 vs 主歌 9.5%）。也就是说
"哪里破"这件事，指标给不出结论，只能让耳朵定。与其我猜十轮，不如给几版。

每组除被改变的那一项外，其余全部固定：
    base    pitch -8  index 0.50  protect 0.33   ← 现在这条成品的参数
    p40     protect 0.40（清辅音保护更多，no/sh 这类字不容易被"唱"一遍）
    ir35    index 0.50→0.35（检索更弱，音色略飘但八度抖应该少）
    pitch6  pitch -6（少降 2 个半音，高音不吃力；音色会偏高一点）
    pitch4  pitch -4

后处理也统一：都做一次 hf 补偿（fc=5500），跟成品一致 —— 否则听的是
"没补高频的 RVC 干声"，和老板平时听到的不是一回事。

用法：
    python tools/ab_break.py --from 160.3 --len 7.8 \
        --model "/d/RVC/.../rbe0d5b48.pth" --index "...index"
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
os.chdir(HERE)

import soundfile as sf  # noqa: E402
import studio  # noqa: E402


def sh(cmd, cwd=None, tag=""):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                       errors="replace")
    ok = p.returncode == 0
    print("  [%s] %s" % ("ok" if ok else "!!", tag))
    if not ok:
        tail = (p.stderr or p.stdout or "").strip().splitlines()[-3:]
        for t in tail:
            print("      " + t)
    return ok


VARIANTS = [
    ("base",   {"pitch": -8}),
    ("p40",    {"pitch": -8, "protect": 0.40}),
    ("ir35",   {"pitch": -8, "index_rate": 0.35}),
    ("pitch6", {"pitch": -6}),
    ("pitch4", {"pitch": -4}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vocal", default="data/cache/sep/0b6d24747d402969/vocal.wav")
    ap.add_argument("--from", dest="st", type=float, default=160.3)
    ap.add_argument("--len", dest="du", type=float, default=7.8)
    ap.add_argument("--model", default=r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI\assets\weights\rbe0d5b48.pth")
    ap.add_argument("--index", default="data/modelhub/MusicBox27_MyLittlePonyRVC/"
                                       "x_Applejack_Talking_Singing_e320_s8000/"
                                       "added_IVF674_Flat_nprobe_1_Applejack_"
                                       "Talking_Singing_v2.index")
    ap.add_argument("--out", default="data/_preview/ab")
    ap.add_argument("--fc", type=int, default=5500)
    a = ap.parse_args()
    for p in (a.vocal, a.model, a.index):
        if not os.path.isfile(p):
            print("找不到：%s" % p)
            return 2
    os.makedirs(a.out, exist_ok=True)
    # ★ 全部转绝对路径：子进程的 cwd 是 RVC 自己的根目录，
    #   相对路径在那儿一个都找不到（第一次跑就栽在这）。
    a.vocal = os.path.abspath(a.vocal)
    a.model = os.path.abspath(a.model)
    a.index = os.path.abspath(a.index)
    a.out = os.path.abspath(a.out)
    ff = studio.FFMPEG
    rpy = studio.RVC_PY
    root = studio.RVC_ROOT

    # 1) 切一段源（单声道 44.1k，RVC 自己会再转）
    src = os.path.join(a.out, "_src.wav")
    if not sh([ff, "-y", "-ss", "%.3f" % a.st, "-t", "%.3f" % a.du,
               "-i", a.vocal, "-ac", "1", "-ar", "44100", src, "-loglevel", "error"],
              tag="切源 %.1f~%.1f 秒" % (a.st, a.st + a.du)):
        return 1
    info = sf.info(src)
    print("源片段：%.2f 秒 %d Hz" % (info.frames / info.samplerate, info.samplerate))

    made = []
    for name, opt in VARIANTS:
        pitch = opt.get("pitch", -8)
        ir = opt.get("index_rate", studio.RVC_INDEX_RATE)
        pr = opt.get("protect", studio.RVC_PROTECT)
        raw = os.path.join(a.out, "%s_raw.wav" % name)
        fin = os.path.join(a.out, "%s.wav" % name)
        cmd = [rpy, "-s", os.path.join(studio.STAGES, "rvc_infer.py"),
               "--model", a.model, "--input", src, "--output", raw,
               "--f0", studio.RVC_F0_METHOD, "--pitch", str(int(pitch)),
               "--index-rate", str(ir), "--rms-mix-rate", str(studio.RVC_RMS_MIX_RATE),
               "--protect", str(pr), "--index", a.index,
               "--chunk", str(studio.RVC_CHUNK_SEC)]
        print("\n== %s  pitch=%d index=%.2f protect=%.2f" % (name, pitch, ir, pr))
        if not sh(cmd, cwd=root, tag="RVC 推理"):
            continue
        # 2) 统一做 hf 补偿（和成品一致）
        if not sh([rpy, "-s", os.path.join(studio.STAGES, "seedvc_cli.py"), "cross",
                   raw, src, fin, str(a.fc), "1200"],
                  cwd=root, tag="高频补偿 fc=%d" % a.fc):
            fin = raw
        if os.path.isfile(fin):
            i2 = sf.info(fin)
            made.append((name, fin, i2.frames / i2.samplerate))
    print("\n=== 产出 ===")
    for name, p, sec in made:
        print("  %-7s %-34s %.2f 秒  %.0f KB"
              % (name, os.path.basename(p), sec, os.path.getsize(p) / 1024.0))
    if not made:
        print("一个都没成，看上面的报错")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
