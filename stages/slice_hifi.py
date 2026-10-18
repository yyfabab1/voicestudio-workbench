# -*- coding: utf-8 -*-
r"""保真切片：跟 `tools/slice_audio.py` 切出**同样的段落**，但保住原采样率。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么要单独写一个（而不是改 GPT-SoVITS 自带的那份）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`tools/slice_audio.py` 是 GPT-SoVITS 自带的，里面**三处写死 32000**：

    slicer = Slicer(sr=32000, ...)          # ①
    audio  = load_audio(inp_path, 32000)    # ②
    wavfile.write(..., 32000, ...)          # ③

它是给**说话模型**用的（GSV 全程 32k），所以这么写没错。但我们同一份切片还拿去
训 **RVC 歌声模型**，于是歌声训练素材被锁在 32k —— 奈奎斯特 ⇒ **高频上限 16 kHz**。
（2026-10-16 实测：分离出来的登阶人声是 44.1k stereo，切完的 353 段全是 32k。）

文献（RVC 官方 FAQ / 多篇实践）对训练素材采样率的要求是 **44.1k 或 48k，且全库统一**。
差的那一段（16k~22.05k）正是齿音、气声、"像不像本人在唱"的那部分 ⇒ 这是
**能靠改代码拿回来的质量**，不是玄学。

不改自带工具的原因：GSV 自己的流程（WebUI / 一键脚本）也在用它，我们动第三方
目录，哪天对方升级就冲突，而且我们**不需要**改它 —— 只要我们的链路有自己的一份。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
★ 关键：这四个"长度"参数的单位是**毫秒**，而且 Slicer 自己会按 sr 换算
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
`min_length / min_interval / hop_size / max_sil_kept` 看起来像"帧数"，其实不是 ——
看 `tools/slicer2.py` 的 `Slicer.__init__`：

        min_interval  = sr * min_interval / 1000          ← 除 1000，按毫秒换算
        self.hop_size = round(sr * hop_size / 1000)
        self.min_length = round(sr * min_length / 1000 / self.hop_size)

  以及它 `main()` 里 argparse 的 help 原文：
        --min_length     "The minimum **milliseconds** required ..."
        --min_interval   "The minimum **milliseconds** for a silence part ..."
        --hop_size       "Frame length in **milliseconds**"
        --max_sil_kept   "... presented in **milliseconds**"

⇒ 所以这四个**原样传给 Slicer 就行**，它已经按 sr 换算过了，**千万不要自己再乘一遍**。

★ 这个"千万不要"是**踩出来的**，不是读代码看出来的：
  第一版我望文生义，把它们当成"32000 Hz 下的帧数"，于是自作聪明乘了 sr/32000
  ⇒ `min_length` 4000 ms（4 秒）被我算成 6 秒 ⇒ 切出来的段被合并（3 段变 2 段，
  第一段从 4.23s 涨到 6.64s）。是 `_probe_slice_hifi.py` 的"切分行为必须不变"
  那条判据把它挡下来的 —— 只验"输出是不是 48k"的话，这个副作用会一路绿灯到线上。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
用法（参数顺序与 `slice_audio.py` 一一对应，只多一个 sr）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    <gsv-python> -s stages/slice_hifi.py \
        <inp> <opt_root> <sr> <threshold> <min_length_ms> <min_interval_ms> \
        <hop_size_ms> <max_sil_kept_ms> <_max> <alpha> <i_part> <all_part>

输出文件名格式与自带工具**完全一致**（`原名_%010d_%010d.wav`）—— 下游（ASR 列表、
合格段筛选、去重）只认这个格式，改了就会静默丢段。
"""
import os
import sys
import traceback

import numpy as np
from scipy.io import wavfile

# ★ 这两行不能省：本工具住在 VoiceStudio/stages 下，而 `tools.my_utils` / `slicer2`
#   在 GPT-SoVITS 根目录里。脚本目录不等于 cwd，不显式插进去就是 ModuleNotFoundError。
#   （自带那份之所以不用插，是因为它本身就躺在 tools/ 里、cwd 又是 GSV 根目录。）
for _p in (os.environ.get("GSV_ROOT", ""),
           os.path.join(os.environ.get("GSV_ROOT", ""), "tools")):
    if _p and os.path.isdir(_p):
        sys.path.insert(0, _p)

from tools.my_utils import load_audio  # noqa: E402
from slicer2 import Slicer             # noqa: E402

def slice(inp, opt_root, sr, threshold, min_length, min_interval, hop_size,
          max_sil_kept, _max, alpha, i_part, all_part):
    sr = int(sr)
    os.makedirs(opt_root, exist_ok=True)
    if os.path.isfile(inp):
        input = [inp]
    elif os.path.isdir(inp):
        input = [os.path.join(inp, name) for name in sorted(list(os.listdir(inp)))]
    else:
        return "输入路径存在但既不是文件也不是文件夹"

    # ★ 这四个**原样传**：单位是毫秒，Slicer 内部自己按 sr 换算（见文件头那段踩坑
    #   记录）。在这里做任何乘法都是在悄悄改切分行为。
    slicer = Slicer(
        sr=sr,
        threshold=int(threshold),
        min_length=int(min_length),
        min_interval=int(min_interval),
        hop_size=int(hop_size),
        max_sil_kept=int(max_sil_kept),
    )
    _max = float(_max)
    alpha = float(alpha)
    for inp_path in input[int(i_part)::int(all_part)]:
        try:
            name = os.path.basename(inp_path)
            audio = load_audio(inp_path, sr)
            for chunk, start, end in slicer.slice(audio):
                tmp_max = np.abs(chunk).max()
                if tmp_max > 1:
                    chunk /= tmp_max
                chunk = (chunk / tmp_max * (_max * alpha)) + (1 - alpha) * chunk
                wavfile.write(
                    "%s/%s_%010d_%010d.wav" % (opt_root, name, start, end),
                    sr,
                    (chunk * 32767).astype(np.int16),
                )
        except Exception:
            # 自带工具这里是裸 except + 打堆栈，行为保持一致：单段失败不拖垮整批，
            # 但必须**看得见**（否则"少切了几段"会伪装成"素材本来就少"）。
            print(inp_path, "->fail->", traceback.format_exc())
    return "执行完毕（保真切片 sr=%d；长度参数是毫秒，Slicer 内部换算）" % sr


if __name__ == "__main__":
    print(slice(*sys.argv[1:]))
