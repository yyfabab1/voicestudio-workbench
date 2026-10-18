# -*- coding: utf-8 -*-
r"""
估算「原唱音域 vs 角色音域」的差距，给出变声该升/降几个半音。

为什么需要它：RVC 的 --pitch 是要人填的。填 0 = 完全不变调 —— 原曲是女声而
角色是男声时，等于让男声硬顶着女声的音高唱，出来又薄又飘，这也是「唱出来
没法听」的一个常见来源。

本脚本用 RVC 自带的 RMVPE（就是推理链里同一个 f0 提取器，口径一致）分别量
原唱人声和角色参考干声的**基频中位数**，再按
    半音 = 12 * log2(角色中位数 / 原唱中位数)
给出建议值。这条公式正好能复现社区的通行经验：女声原曲给男声唱 ≈ -12，
男声原曲给女声唱 ≈ +12，同性别 ≈ 0 —— 而且是量出来的，不是查表查出来的。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器）：
    runtime\python.exe -s <本脚本> <原唱人声.wav> <角色参考目录> <输出.json> [最多段数]

输出 JSON：
    {"src_med": 220.1, "ref_med": 132.4, "semitones": -9,
     "src_frames": 12345, "ref_frames": 6789, "ref_used": 8}
退出码：0 成功；2 参数/环境错误；1 量不出有效基频。
"""
import json
import os
import sys

if len(sys.argv) < 4:
    print("参数不足：需要 原唱人声 角色参考目录 输出json [最多段数]")
    sys.exit(2)

SRC = sys.argv[1]
REF_DIR = sys.argv[2]
OUT = sys.argv[3]
MAXN = int(sys.argv[4]) if len(sys.argv) > 4 else 8

os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))

# ★ 必须把 cwd（= RVC 根目录）塞进 sys.path：
#   脚本是用 `python -s <stages/pitch_cli.py>` 起的，sys.path[0] 是本脚本所在的
#   stages/ 目录，**不含** RVC 根 —— 于是 `import infer.rmvpe` 直接
#   ModuleNotFoundError（实测：自动音高静默降级成 0，用户完全看不出来）。
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import librosa  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402

from infer.rmvpe import RMVPE  # noqa: E402

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
print("当前设备：%s | 加载 RMVPE…" % DEVICE)
sys.stdout.flush()
_model = RMVPE(os.path.join(os.environ["rmvpe_root"], "rmvpe.pt"),
               is_half=(DEVICE != "cpu"), device=DEVICE)


def f0_frames(path, max_secs=None):
    """读成 16kHz 单声道，返回（该文件所有有声帧的 f0 列表, 有效帧数）。

    只取有声帧（f0 > 1Hz）：RMVPE 对静音/气声会给 0，参与中位数会把结果拉低。
    """
    x, _ = librosa.load(path, sr=16000, mono=True)
    if x.size == 0:
        return [], 0
    if max_secs:
        x = x[: int(16000 * max_secs)]
    f0 = _model.infer_from_audio(x, thred=0.03)
    f0 = np.asarray(f0, dtype=np.float32).reshape(-1)
    v = f0[f0 > 1.0]
    return [float(t) for t in v], int(v.size)


print("量原唱人声：%s" % os.path.basename(SRC))
sys.stdout.flush()
src_vals, src_n = f0_frames(SRC, max_secs=150)      # 前 150 秒足够代表音域，省时间
print("  有声帧 %d" % src_n)

ref_vals, ref_n, used = [], 0, 0
names = sorted(f for f in os.listdir(REF_DIR)
               if f.lower().endswith((".wav", ".flac", ".mp3")))
for name in names[:MAXN]:
    vals, n = f0_frames(os.path.join(REF_DIR, name))
    if n:
        ref_vals.extend(vals)
        ref_n += n
        used += 1
print("  角色参考：用了 %d 段，有声帧 %d" % (used, ref_n))
sys.stdout.flush()

if src_n < 20 or ref_n < 20:
    print("有效基频太少（原唱 %d 帧 / 角色 %d 帧），没法可靠估算。" % (src_n, ref_n))
    sys.exit(1)

src_med = float(np.median(src_vals))
ref_med = float(np.median(ref_vals))
import math  # noqa: E402

semi = int(round(12.0 * math.log2(ref_med / src_med)))
semi = max(-24, min(24, semi))

data = {"src_med": round(src_med, 2), "ref_med": round(ref_med, 2),
        "semitones": semi, "src_frames": src_n, "ref_frames": ref_n, "ref_used": used}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
print("PITCH_DONE 原唱中位 %.1f Hz，角色中位 %.1f Hz ⇒ 建议 %+d 半音"
      % (src_med, ref_med, semi))
print(json.dumps(data, ensure_ascii=False))
