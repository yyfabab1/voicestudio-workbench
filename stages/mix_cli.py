# -*- coding: utf-8 -*-
r"""
混音：伴奏轨 + 变声人声 -> 成品轨。

★ 为什么不用 ffmpeg 的 amix：整合包自带的是老版 ffmpeg，amix 没有 normalize
  选项，直接报错。而且这里要做的事（平滑噪声门、响度对齐、防削波）ffmpeg
  也不好表达。

★ 为什么第一个参数必须是**分离出来的伴奏轨**而不是原曲：
  原曲里带着原唱，拿原曲当伴奏混出来就是两个人在唱
  （实测成品与原曲波形相关系数 0.870、原曲振幅占比 0.538）——
  这正是"唱出来没法听"的主因。

用法：
    python mix_cli.py <伴奏> <人声> <输出> [伴奏增益|auto] [人声增益|auto] [门限dB|off]

    增益传 auto（默认）时按各自 RMS 自动对齐到推荐响度：
        伴奏 RMS ~ 0.12，人声 RMS ~ 0.18（人声略突出）
    门限默认 off；给 -45 之类会开平滑噪声门 —— RVC 在间奏/静音段会留底噪
    甚至冒怪音，压掉之后成品干净很多。

退出码：0 成功；1 失败。
"""
import sys
from math import gcd

import numpy as np
import soundfile as sf
from scipy.ndimage import uniform_filter1d
from scipy.signal import resample_poly

if len(sys.argv) < 4:
    print("用法: mix_cli.py <伴奏> <人声> <输出> [伴奏增益|auto] [人声增益|auto] [门限dB|off]")
    sys.exit(1)

a_path, b_path, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
gain_a_raw = sys.argv[4] if len(sys.argv) > 4 else "auto"
gain_b_raw = sys.argv[5] if len(sys.argv) > 5 else "auto"
gate_raw = sys.argv[6] if len(sys.argv) > 6 else "off"

TARGET_INST_RMS = 0.12     # 伴奏的目标均方根
TARGET_VOICE_RMS = 0.18    # 人声略高于伴奏，保证字听得清
TARGET_MIX_RMS = 0.13      # 成品整体响度目标
PEAK_CEIL = 0.95           # 峰值天花板（留余量，防解码端削波）
GAIN_MIN, GAIN_MAX = 0.15, 8.0   # 自动增益的合理范围，别把极小声放大成噪音


def load(p):
    w, sr = sf.read(p, always_2d=True, dtype="float32")
    return w, sr


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2) + 1e-12))


def soft_gate(x, sr, thresh_db=-45.0, knee_db=9.0):
    """平滑噪声门：低于阈值的段压到接近 0，阈值以上完全保留。

    为什么用平滑而不是硬切：硬切会在每次过门限的地方留下「咔哒」声，
    比底噪还难听。这里用 20ms 滑动 RMS 算增益曲线，并做 9dB 软膝过渡，
    再把增益曲线本身也平滑一遍，逐样本跳变就没了。
    """
    win = max(3, int(sr * 0.02))
    out = np.empty_like(x)
    for ch in range(x.shape[1]):
        s = x[:, ch].astype(np.float64)
        env = np.sqrt(uniform_filter1d(s ** 2, win, mode="nearest") + 1e-12)
        env_db = 20.0 * np.log10(env + 1e-12)
        g = np.clip((env_db - thresh_db) / knee_db, 0.0, 1.0)
        g = uniform_filter1d(g, win, mode="nearest")
        out[:, ch] = (s * g).astype(np.float32)
    return out


a, sr_a = load(a_path)     # 伴奏
b, sr_b = load(b_path)     # 人声

if sr_b != sr_a:
    g = gcd(int(sr_a), int(sr_b))
    b = resample_poly(b, sr_a // g, sr_b // g, axis=0)

# 声道数对齐（单声道人声 + 立体声伴奏这类情况）
if a.shape[1] != b.shape[1]:
    if a.shape[1] == 1:
        a = np.repeat(a, b.shape[1], axis=1)
    elif b.shape[1] == 1:
        b = np.repeat(b, a.shape[1], axis=1)
    else:
        b = b[:, :a.shape[1]]

# 以伴奏（= 原曲）的长度为准：人声短了补零、长了截断。
# 这样成品时长恒定等于原曲，不会因为 RVC 少吐几个样本就整首歌短一截。
n = len(a)
if n == 0:
    print("MIX_FAIL 伴奏轨是空的")
    sys.exit(1)
if len(b) < n:
    b = np.pad(b, ((0, n - len(b)), (0, 0)))
else:
    b = b[:n]

gate_note = "off"
if gate_raw and str(gate_raw).lower() != "off":
    try:
        b = soft_gate(b, sr_a, thresh_db=float(gate_raw), knee_db=9.0)
        gate_note = "%.0fdB" % float(gate_raw)
    except Exception as e:
        gate_note = "off(%s)" % e

voice_rms_raw = rms(b)
inst_rms_raw = rms(a)

# ---- 增益：auto 就按目标响度对齐，给数字就按数字 ----
if str(gain_a_raw).lower() == "auto":
    gain_a = TARGET_INST_RMS / max(inst_rms_raw, 1e-6)
else:
    gain_a = float(gain_a_raw)
if str(gain_b_raw).lower() == "auto":
    gain_b = TARGET_VOICE_RMS / max(voice_rms_raw, 1e-6)
else:
    gain_b = float(gain_b_raw)
gain_a = float(np.clip(gain_a, GAIN_MIN, GAIN_MAX))
gain_b = float(np.clip(gain_b, GAIN_MIN, GAIN_MAX))

x = a[:n] * gain_a + b[:n] * gain_b

# ---- 响度对齐到目标，但绝不让峰值越过天花板 ----
mix_rms = rms(x)
peak = float(np.max(np.abs(x))) if x.size else 0.0
if mix_rms > 1e-9 and peak > 1e-9:
    k = min(TARGET_MIX_RMS / mix_rms, PEAK_CEIL / peak)
    x = (x * k).astype(np.float32)

peak_final = float(np.max(np.abs(x))) if x.size else 0.0
clipped = int(np.sum(np.abs(x) > 1.0))
sf.write(out_path, x, sr_a, subtype="PCM_24")

print("MIX_STATS inst_rms=%.4f voice_rms=%.4f gain_inst=%.3f gain_voice=%.3f "
      "mix_rms=%.4f peak=%.3f gate=%s"
      % (inst_rms_raw, voice_rms_raw, gain_a, gain_b, rms(x), peak_final, gate_note))
print("MIX_DONE %.2f 秒 %d Hz %d 声道 峰值 %.3f 削幅点 %d 截取 %d/%d 样本"
      % (n / sr_a, sr_a, x.shape[1], peak_final, clipped, n, len(a)))
