r"""
用 **RMVPE** 提一条完整的 F0 序列（带时间轴），存成 npz，交给分析脚本。

为什么要另开一个 CLI，而不是复用 pitch_cli.py：
  pitch_cli 的 f0_frames() 返回的是**有声帧的 f0 列表**——它为了算"音域中位数"
  把 0 帧全丢了，于是**时间轴没了**。测颤音必须知道"哪一帧接着哪一帧"，
  只有列表是算不出来的。

为什么用 RMVPE 而不是 librosa.pyin：
  实测（同一段 60 秒成品）：pyin 的发声占比只有 0.26，F0 从 77 Hz 一直跨到
  318 Hz —— 真人唱歌不会这么宽，说明它把伴奏残留/噪声也判成了人声。
  项目本来就用 RMVPE（音准那轮实测 rmvpe 优于 fcpe：八度错 5.1%→3.3%），
  这里跟产品保持同一把尺子，而不是为了诊断另造一套。

用法（cwd 必须是 RVC 根目录，跟其它 stages/*_cli.py 一样）：
    python -s stages/vib_cli.py in.wav out.npz
"""
import os
import sys

SRC = sys.argv[1]
OUT = sys.argv[2]

os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))

# 跟 pitch_cli.py 一样的坑：脚本是 `python -s <stages/xxx.py>` 起的，
# sys.path[0] 是 stages/，不含 RVC 根 ⇒ import infer.rmvpe 直接炸。
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import torch  # noqa: E402

from infer.rmvpe import RMVPE  # noqa: E402

SR_IN = 16000
HOP = 160          # RMVPE 的帧移（@16kHz ⇒ 100 帧/秒）

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
print("设备 %s | 加载 RMVPE…" % DEVICE)
sys.stdout.flush()
_model = RMVPE(os.path.join(os.environ["rmvpe_root"], "rmvpe.pt"),
               is_half=(DEVICE != "cpu"), device=DEVICE)

x, sr = sf.read(SRC, dtype="float32", always_2d=False)
if x.ndim > 1:
    x = x.mean(axis=1)
if sr != SR_IN:                      # RMVPE 吃 16k
    n = int(round(len(x) * SR_IN / float(sr)))
    x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)
print("读入 %.1fs（原 sr=%d → 16000）" % (len(x) / float(SR_IN), sr))
sys.stdout.flush()

f0 = np.asarray(_model.infer_from_audio(x, thred=0.03), dtype=np.float32).reshape(-1)
fps = SR_IN / float(HOP)

# ★ 帧数对账：RMVPE 的 hop 如果哪天改了，这里会立刻炸出来，
#   而不是让下游拿着一个错的帧率算出一堆看似合理的数字。
expect = int(round(len(x) / float(HOP)))
print("F0 帧数 %d（按 hop=%d 应约 %d，差 %d）"
      % (len(f0), HOP, expect, abs(len(f0) - expect)))
if abs(len(f0) - expect) > max(8, expect * 0.02):
    print("!! 帧数对不上，帧率不可信，拒绝产出")
    sys.exit(3)

voiced = f0 > 1.0
vals = f0[voiced]
print("有声帧 %d / %d（%.2f）" % (int(voiced.sum()), len(f0), float(voiced.mean())))
if vals.size:
    q = np.percentile(vals, [5, 50, 95])
    print("F0 分位 Hz：%.0f / %.0f / %.0f" % (q[0], q[1], q[2]))
sys.stdout.flush()

np.savez(OUT, f0=f0, sr=np.int32(SR_IN), hop=np.int32(HOP), fps=np.float64(fps))
print("已写出 %s" % OUT)
