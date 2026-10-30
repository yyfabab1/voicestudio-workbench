# -*- coding: utf-8 -*-
r"""tools/fix_drifted_product.py —— 把**已经唱好**的成品救回来，不用重唱。

背景
----
2026-10-22 查出：RVC 的输出比输入短一点点，而且是**按比例**铺在整条时间轴上的
⇒ 错位随位置线性累积（实测 Fight Song 204 秒累积 -80ms、Don't You Worry 241 秒
累积 -107ms）。混音时人声相对伴奏越跑越前，听感就是"越唱越差、后面发飘发破"。

根因已经在 `stages/rvc_infer.py` 的 `align_duration()` 里修掉（下次唱就是好的），
但**之前唱好的成品仍然是歪的**。这首歌重唱一次要 5~13 分钟，而修好它只需要一次
比例缩放（~1.0004 ⇒ 音高变化 0.7 音分，听不出来）。

这个工具就是干这个：把人声轨按源长度校正回去，可选顺手重新混一版成品。

★ 复用的原则：
    算法**只有一份**，在 stages/rvc_infer.py 的 align_duration() 里。本工具用同一套
    「从源码里抠出函数」的办法加载它（和 verify_drift.py 一致），不复制代码，
    免得以后改了一边忘了另一边。

用法（默认只是报告，不动任何文件）
----------------------------------
    python tools/fix_drifted_product.py <voice.wav> --src <源vocal.wav>
    python tools/fix_drifted_product.py <voice.wav> --src <源vocal.wav> --apply
    # 顺便重新混音（需要给伴奏轨和输出路径）
    python tools/fix_drifted_product.py <voice.wav> --src <源vocal.wav> --inst <inst.wav> \
           --mix <新mix.wav> --apply [--mixpy <解释器>]

--apply 的行为
--------------
    · 人声轨：写到 <原名>_fix.wav（**不覆盖原件**）；
    · 加 --inplace 时才直接改原文件，改之前先备份成 <原名>.bak；
    · 给 --inst/--mix 时再调用 stages/mix_cli.py 重混一份成品。
退出码：0 成功；1 参数或处理失败。
"""
import argparse
import os
import re
import shutil
import subprocess
import sys

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAGE = os.path.join(ROOT, "stages", "rvc_infer.py")


def load_align_duration():
    """从 stages/rvc_infer.py 里取出 align_duration —— 保证算法只有一份。"""
    with open(STAGE, encoding="utf-8") as fh:
        src = fh.read()
    m = re.search(r"^def align_duration\(.*?(?=^\S)", src, re.S | re.M)
    if not m:
        return None
    ns = {"np": np}
    exec(compile(m.group(0), "align_duration", "exec"), ns)  # noqa: S102
    return ns.get("align_duration")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("voice", help="成品人声轨 voice_*.wav")
    ap.add_argument("--src", required=True, help="源干声 vocal.wav（对它对长度）")
    ap.add_argument("--inst", default="", help="伴奏轨，给了才会重新混音")
    ap.add_argument("--mix", default="", help="重新混出来的成品路径")
    ap.add_argument("--apply", action="store_true", help="真的写文件（默认只报告）")
    ap.add_argument("--inplace", action="store_true", help="直接改原文件（会先备份 .bak）")
    ap.add_argument("--mixpy", default="", help="跑 mix_cli.py 的解释器路径")
    a = ap.parse_args()

    for p in (a.voice, a.src) + tuple([x for x in (a.inst,) if x]):
        if not os.path.isfile(p):
            print("找不到文件：%s" % p)
            return 1

    f = load_align_duration()
    if f is None:
        print("stages/rvc_infer.py 里找不到 align_duration")
        return 1

    vi = sf.info(a.voice)
    si = sf.info(a.src)
    v_sec = vi.frames / float(vi.samplerate)
    s_sec = si.frames / float(si.samplerate)
    want = int(round(si.frames * float(vi.samplerate) / float(si.samplerate)))
    drift = (vi.frames - want) / float(vi.samplerate) * 1000.0

    print("人声轨 %s：%.3f 秒 @%d" % (os.path.basename(a.voice), v_sec, vi.samplerate))
    print("源干声 %s：%.3f 秒 @%d" % (os.path.basename(a.src), s_sec, si.samplerate))
    print("漂移 %+.1f ms（%d 样本 → 应为 %d 样本），相对偏差 %.4f%%"
          % (drift, vi.frames, want, abs(drift) / (v_sec * 1000.0) * 100.0))

    if abs(drift) < 2.0:
        print("\n漂移在 2 ms 以内，不用修（也不会去做无谓的重采样）。")
        return 0

    if not a.apply:
        print("\n[dry-run] 没动任何文件。加 --apply 执行修正。")
        return 0

    print("\n读音频…")
    y, sr = sf.read(a.voice, dtype="float32", always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    # 保持原 subtype：读回来是 float32 是因为我们指定了 dtype，
    # 原文件其实是 int16（PCM_16）；soundfile 写的时候按 subtype 走。
    out, _ = f(y, want, sr)
    out = np.asarray(out, dtype=np.float32)

    if a.inplace:
        bak = a.voice + ".bak"
        if not os.path.exists(bak):
            shutil.copy2(a.voice, bak)
            print("已备份原件：%s" % bak)
        target = a.voice
    else:
        target = os.path.splitext(a.voice)[0] + "_fix.wav"
    sf.write(target, out, sr, subtype=vi.subtype)
    print("已写人声轨：%s（%.3f 秒）" % (target, len(out) / float(sr)))

    if a.inst and a.mix:
        if not os.path.isfile(a.inst):
            print("伴奏不存在：%s" % a.inst)
            return 1
        py = a.mixpy
        if not py:
            guess = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI\runtime\python.exe"
            py = guess if os.path.isfile(guess) else sys.executable
        cmd = [py, "-s", os.path.join(ROOT, "stages", "mix_cli.py"),
               a.inst, target, a.mix, "auto", "auto", "-45"]
        print("重新混音：%s" % " ".join('"%s"' % c for c in cmd))
        rc = subprocess.call(cmd)
        if rc != 0 or not os.path.isfile(a.mix):
            print("混音失败（rc=%d）—— 人声轨 %s 已经修好，可以先听它" % (rc, target))
            return 1
        print("已写成品：%s" % a.mix)
    return 0


if __name__ == "__main__":
    sys.exit(main())
