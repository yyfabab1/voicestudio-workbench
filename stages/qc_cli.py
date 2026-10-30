# -*- coding: utf-8 -*-
r"""成品客观质检：把「听起来不对」变成几个能对账的数。

比什么：把**原唱人声轨**和**角色变声后的人声轨**逐帧比基频。
用 RVC 自带的 RMVPE —— 和推理链里同一个 f0 提取器，口径一致（不用另装依赖，
也不会出现"质检用的尺子和干活用的尺子不是一把"）。两条轨来自同一首歌、时间轴
相同，所以可以**逐帧直接比**，不需要动态时间规整。

为什么这样比是有意义的：RVC 是「保 f0 曲线、换音色」。也就是说它的输出基频应当
等于输入基频**整体平移 pitch 个半音**、形状不变。于是三件事都能量出来：

  1. 实际平移了几个半音（shift_semitones）
     —— 跟用户设的 pitch 对得上吗？对不上就是"设了根本没生效"。
        这一类静默失效是本项目反复踩的坑：界面上填了 -19，实际按 0 唱，
        用户只会觉得"难听"，完全不知道是设置没生效。
  2. 去掉整体平移之后的**音准吻合率**（match_ratio / match_ratio_loose）
     —— 旋律曲线有没有被搞乱。原口径是"±50 音分内"，但实测它在人声素材上
        **没有判别力**（把人声精确变调、理论上该读 100% 的情况，它也只读 47%，
        因为颤音/滑音让逐帧基频一直在动）。所以另加宽松口径 ±150 音分当主判据，
        并且两项都必须在**固定 lag=0** 处算（原因见文件里"时间轴怎么对齐"一段：
        用"轮廓相关最大"去挑对齐会挑到错位处，把好录制报成跑调）。
  3. 「唱 / 不唱」图案的最佳滞后（lag_ms）
     —— 时间轴有没有错位。混音拿错轨、人声短了补零、切片起止不一致，
        都会在这里露出来。0ms 是正常，几百 ms 就是真的错开了。
        这一项**只用「唱/不唱」图案**来定：它不随时移平滑，比平滑的
        f0 轮廓可靠（实测同一份数据：图案给 0.0ms，f0 轮廓给 -130ms）。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器）：
    runtime\python.exe -s <本脚本> <原唱人声.wav> <角色人声.wav> <输出.json> [起点秒] [时长秒]

输出 JSON —— **只放量出来的数，不做判断**（判断放在 studio.py，好写人话、
也好在验收里单独断言）：
    {"src_med":263.0, "dst_med":86.7, "shift_semitones":-19.0,
     "match_ratio":0.66, "match_ratio_loose":0.78, "octave_err_ratio":0.11,
     "contour_corr":0.95, "contour_corr_best":0.96, "contour_lag_ms":-130.0,
     "dev_iqr_cents":73.0, "src_iqr_cents":347.0, "dst_iqr_cents":340.0,
     "spread_ratio":0.98,
     "lag_ms":0.0, "voiced_lag_ms":0.0, "voiced_lag_corr":0.92,
     "hop_ms":10.0, "frames":3000, "voiced_src":1846, "voiced_dst":1717,
     "dst_peak":0.95, "clip_ratio":0.0, "dst_rms":0.21,
     "consonant_sim":0.81, "consonant_frames":180,
     "hf_ratio_src":0.31, "hf_ratio_dst":0.27,
     "skipped_secs":0.0, "skipped_spec":""}

退出码：0 成功；2 参数/环境错误；1 量不出有效基频（两条轨里至少一条没人声）。
"""
import json
import os
import sys

if len(sys.argv) < 4:
    print("参数不足：需要 <原唱人声.wav> <角色人声.wav> <输出.json> [起点秒] [时长秒] [跳过段]")
    sys.exit(2)

SRC = sys.argv[1]
DST = sys.argv[2]
OUT = sys.argv[3]
START = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
SECS = float(sys.argv[5]) if len(sys.argv) > 5 else 0.0
# ★ 「跳过段」= 相对本段起点的秒区间列表，形如 "3.1-4.5,7.2-9.0"。
#   为什么必须有这个东西：非歌声段保护会把口哨/纯音那几段换回**原始人声**
#   （见 nonvocal_guard.py），那些段是**故意不变调**的。如果质检照旧把它们算进
#   音准统计，会得到荒谬的结果 —— 实测：不排除时"音准吻合 58.9%、逐帧偏差 IQR
#   1905 音分、起伏比 2.12"（看着像整首歌都跑调了），排除后才是真实水平。
#   口径：被排除的帧 f0 一律置 0（等价于"这几帧不在唱"），后续所有统计自然忽略。
SKIPSPEC = sys.argv[6] if len(sys.argv) > 6 else ""

# RVC 的 RMVPE 权重放在 assets/rmvpe 下，且它按**相对路径**找 —— 必须让 cwd 是
# RVC 根目录，并把 cwd 塞进 sys.path（脚本是用 `python -s <stages/qc_cli.py>` 起的，
# sys.path[0] 是本脚本所在的 stages/，不含 RVC 根 ⇒ import infer.rmvpe 直接报
# ModuleNotFoundError；这个坑 pitch_cli.py 已经踩过一次）。
os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import librosa  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402

from infer.rmvpe import RMVPE  # noqa: E402

DEVICE = "cuda:0" if torch.cuda.is_available() else "cpu"
FT = os.path.join(os.environ["rmvpe_root"], "rmvpe.pt")
if not os.path.exists(FT):
    print("找不到 RMVPE 权重：%s" % FT)
    sys.exit(2)
print("当前设备：%s | 加载 RMVPE…" % DEVICE)
sys.stdout.flush()
MODEL = RMVPE(FT, is_half=(DEVICE != "cpu"), device=DEVICE)


def clip_slice(path):
    """读成 16kHz 单声道并取 [START, START+SECS) 这一段。"""
    y, _ = librosa.load(path, sr=16000, mono=True)
    y = np.asarray(y, dtype=np.float32)
    a = int(max(0.0, START) * 16000)
    if SECS > 0:
        return y[a:a + int(SECS * 16000)]
    return y[a:]


xs = clip_slice(SRC)
xd = clip_slice(DST)
n = int(min(xs.size, xd.size))
if n < 16000:                     # 不满 1 秒，量什么都不靠谱
    print("音频太短（%.2f 秒），没法质检。" % (n / 16000.0))
    sys.exit(1)
xs, xd = xs[:n], xd[:n]

print("逐帧提基频…（%.1f 秒）" % (n / 16000.0))
sys.stdout.flush()
f0s = np.asarray(MODEL.infer_from_audio(xs, thred=0.03), dtype=np.float32).reshape(-1)
f0d = np.asarray(MODEL.infer_from_audio(xd, thred=0.03), dtype=np.float32).reshape(-1)
m = int(min(f0s.size, f0d.size))
if m < 20:
    print("提不出足够的帧（原唱 %d / 角色 %d）" % (f0s.size, f0d.size))
    sys.exit(1)
f0s, f0d = f0s[:m], f0d[:m]

# ★ 帧长**自己量**，不写死 10ms：万一上游改了提取器的 hop，这里会跟着变，
#   而不是把"滞后 500ms"报成"滞后 50 帧"那种没人看得懂的错。
hop_ms = (n / 16000.0) * 1000.0 / m

# 跳过段（相对本段起点）→ 帧级屏蔽
skip_frames = np.zeros(m, dtype=bool)
if SKIPSPEC.strip():
    for part in SKIPSPEC.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            a_s, b_s = [float(x) for x in part.split("-")]
        except Exception:
            continue
        i0 = int(a_s * 1000.0 / hop_ms)
        i1 = int(b_s * 1000.0 / hop_ms)
        skip_frames[max(0, i0):max(0, i1)] = True
if skip_frames.any():
    f0s = f0s.copy()
    f0d = f0d.copy()
    f0s[skip_frames] = 0.0
    f0d[skip_frames] = 0.0

vs = f0s > 1.0                    # "这一帧在唱"（RMVPE 对静音/气声给 0）
vd = f0d > 1.0
src_n, dst_n = int(vs.sum()), int(vd.sum())
if src_n < 20 or dst_n < 20:
    print("有效基频太少（原唱 %d 帧 / 角色 %d 帧）—— 至少有一条不是人声。" % (src_n, dst_n))
    sys.exit(1)

src_med = float(np.median(f0s[vs]))
dst_med = float(np.median(f0d[vd]))

# ---- 0. 时间轴怎么对齐（这一步栽过一次，改法写在这里）----
#
# 为什么要有"找偏移"这件事：RVC 内部会给自己垫 3 秒（t_pad）、在垫过的音频上提
# f0，输出再把垫的部分裁掉；只要中间某个环节错开几帧，"逐帧直接比"出来的相关性
# 就会掉到 0，看着像"旋律完全没跟上"，其实是尺子没对齐。
#
# ★★ 但**用什么来定对齐**比"要不要对齐"更要命 —— 这里踩过一次真坑（实测）：
#   原实现用「f0 轮廓相关最大」来挑滞后，它挑到 lag = **-130ms**（相关 +0.960，
#   比 0ms 处的 +0.950 还高一丁点），而在那个**错位**上算吻合率只剩 **31.9%**；
#   物理正确的 lag=0 处其实有 **65.6%**。
#   根因：旋律轮廓是**平滑**的，两条平滑曲线错开一点点，相关系数不但不掉、反而
#   因为抹平了细节而微升 —— 于是"取相关最大"这个标准天然偏向"错开一点点"的
#   位置。用错位位置报音准 = 把一段好录音报成跑调（实测把一个试听任务误判成
#   失败）。搜索范围设成 ±1.5 秒时这个偏差尤其明显。
#   ⇒ 现在的做法（三条，缺一不可）：
#     ① 音准统计**固定在 lag=0** —— 混音是按样本对齐的，两条轨本来就该差 0；
#     ② 滞后搜索只当"**错位检测**"用，而且改用「唱/不唱」图案来定：
#        它不随时移平滑（判决图案是方波，抹不动），实测同一份数据它给 0.0ms、
#        相关 0.92，比 f0 轮廓可靠得多。
#     ③ f0 轮廓相关照报（它是"有没有跟着旋律走"的好指标），但**只报数、
#        不拿它选对齐**。
def _smooth(x, k=9):
    """滑动平均。给「唱/不唱」图案去毛刺用 —— voiced/unvoiced 判决本身抖得厉害
    （气声、字头都可能被判成 unvoiced），不平滑的话相关性全是噪声。"""
    k = max(1, int(k))
    if k < 3 or x.size < k:
        return x
    return np.convolve(x, np.ones(k, dtype=np.float32) / k, mode="same")


def _lag_corr(a, b, kk):
    """把 b 相对 a 平移 kk 帧之后的归一化相关；太短返回 None。"""
    if kk >= 0:
        x, y = a[:a.size - kk], b[kk:]
    else:
        x, y = a[-kk:], b[:b.size + kk]
    if x.size < 20:
        return None
    return float(np.dot(x, y) / (np.linalg.norm(x) * np.linalg.norm(y) + 1e-9))


# 约定：lag_ms > 0 表示**角色人声比原唱晚** lag_ms（把它往前挪这么多才对得齐）。
# 这个符号不是猜的 —— 验收里给角色人声前面垫 500ms 静音，实测必须报 +500。
as_, ad = _smooth(vs.astype(np.float32)), _smooth(vd.astype(np.float32))
as_ = as_ - float(as_.mean())
ad = ad - float(ad.mean())
maxlag = int(max(1, round(2000.0 / hop_ms)))       # 搜 ±2 秒
voiced_lag_corr, voiced_lag_k = -2.0, 0
for kk in range(-maxlag, maxlag + 1):
    _c = _lag_corr(as_, ad, kk)
    if _c is not None and _c > voiced_lag_corr:
        voiced_lag_corr, voiced_lag_k = _c, kk
lag_ms = voiced_lag_k * hop_ms                     # ← 「错位检测」就以它为准


def _contour_scan(lo_ms=-1500, hi_ms=1500, step_ms=10, min_frames=25):
    """扫一遍 f0 轮廓相关，**仅供查看**（f0 在哪个滞后最像）。

    保留它是为了排查问题时有得看；但结论里**不用它定对齐**（原因见上面）。
    """
    out = []
    for _l in range(lo_ms, hi_ms + 1, step_ms):
        _k = int(round(_l / hop_ms))
        if _k >= 0:
            x, y = f0s[:f0s.size - _k], f0d[_k:]
        else:
            x, y = f0s[-_k:], f0d[:f0d.size + _k]
        if x.size < min_frames:
            continue
        mm = (x > 1.0) & (y > 1.0)
        if int(mm.sum()) < min_frames:
            continue
        lx_ = 1200.0 * np.log2(x[mm])
        ly_ = 1200.0 * np.log2(y[mm])
        if lx_.std() < 1e-6 or ly_.std() < 1e-6:
            continue
        out.append((float(np.corrcoef(lx_, ly_)[0, 1]), _l, int(mm.sum())))
    return out


_scan = _contour_scan()
contour_corr_best, contour_lag_ms = 0.0, 0.0
if _scan:
    _c, _l, _n = max(_scan, key=lambda r: r[0])
    contour_corr_best, contour_lag_ms = _c, float(_l)

# ---- 1. 音准统计：**固定 lag=0**（上面第 ① 条）----
xs, xd = f0s, f0d
both = (xs > 1.0) & (xd > 1.0)
if int(both.sum()) < 20:
    print("同时有声的帧太少（%d），没法比音准。" % int(both.sum()))
    sys.exit(1)
aligned_frames = int(both.sum())
lxv = 1200.0 * np.log2(xs[both])
lyv = 1200.0 * np.log2(xd[both])
cents = lyv - lxv
med_cents = float(np.median(cents))
shift = med_cents / 100.0                          # 中位数 = 抗八度错帧
dev = cents - med_cents
absdev = np.abs(dev)
# ★ 光用「±50 音分内」这一个口径**在本项目的素材上没有判别力** —— 实测（见
#   _probe_lag_diag.py 的 [C] 段）：把原唱**精确变调**（数学上偏差恒为 0，
#   理论上该读 100%），RMVPE 这把尺子也只读出 **47.2%**。原因是人声有颤音/滑音，
#   10ms 一帧的基频本来就在动，"±50 音分"（半个半音）太紧。
#   ⇒ 保留原口径（与历史数字可比），另加一个**宽松口径 ±150 音分**当主判据。
#     它必须配有负对照（门禁里断言）：拿两段无关人声来比，宽松口径也得掉下去。
# ★ 「八度错」单独拎出来：这是 RVC/基频提取器最典型、听感最糟的一种错
#   （旋律突然跳一个八度）。它在 ±50 口径里跟"轻微跑调"混在一起看不出来。
match_ratio = float(np.mean(absdev < 50.0))        # 原口径，保留
match_ratio_loose = float(np.mean(absdev < 150.0))  # 宽松口径，主判据
octave_err = float(np.mean((absdev >= 900.0) & (absdev <= 1500.0)))
dev_iqr = float(np.percentile(dev, 75) - np.percentile(dev, 25))
contour_corr = float(np.corrcoef(lxv, lyv)[0, 1])   # lag=0 处的轮廓相关

# ★ 光有 match_ratio 抓不住最要命的一种毛病：**唱成一条平线**。
#   因为 match_ratio 是"去掉整体平移之后"算的 —— 一条完全不动的直线，
#   每一帧的偏差都是 0 音分，吻合率反而是 100%！看起来好得不得了。
#   所以必须再量一个"音高起伏的幅度"：两条轨各自的四分位距（音分）。
#   忠实变声 ⇒ 输出起伏 ≈ 输入起伏（比值≈1）；唱成平线 ⇒ 比值≈0。
src_iqr_c = float(np.percentile(1200.0 * np.log2(xs[both]), 75)
                  - np.percentile(1200.0 * np.log2(xs[both]), 25))
dst_iqr_c = float(np.percentile(1200.0 * np.log2(xd[both]), 75)
                  - np.percentile(1200.0 * np.log2(xd[both]), 25))
spread_ratio = (dst_iqr_c / src_iqr_c) if src_iqr_c > 1e-6 else 0.0

# ---- 2. 「唱/不唱」图案的滞后已经在上面算过了（lag_ms / voiced_lag_corr）----
# 这一段原先在原地又算了一遍，现已合并到上面 `_lag_corr` 那段，避免两处口径分叉。

# ---- 3. 削幅 / 响度（用原始采样率读，16k 重采样会把峰值抹平）----
raw, _ = librosa.load(DST, sr=None, mono=True)
raw = np.asarray(raw, dtype=np.float32)
dst_peak = float(np.max(np.abs(raw))) if raw.size else 0.0
clip_ratio = float(np.mean(np.abs(raw) >= 0.999)) if raw.size else 0.0
dst_rms = float(np.sqrt(np.mean(raw ** 2))) if raw.size else 0.0

# ---- 4. 咬字 / 清辅音保真（专门给 RVC 的 protect 这类参数定标）----
# 为什么必须单独量这一项：RVC 的 `protect` 参数**只作用在没有基频但有能量的帧**
# （清辅音 s/sh/t/k、呼吸、气口）。这些帧决定"咬字清不清楚"，但它们不含音高，
# 所以在上面那一堆音准指标里**完全看不出来**。实测过：把 protect 从 0.33 调到
# 0.50（源码里 `if protect < 0.5` 才生效 ⇒ 0.50 = **关闭保护**），音准几乎不变，
# 但清辅音会被模型"唱"过一遍，听感就是含混、字发不清。
# 量法：在"无 f0 但有能量"的帧上比两条轨的**幅度谱形状**（余弦相似度）与
# **高频占比**。注意 dst 是**角色音色**，谱形本来就有系统性差异，所以这个数的
# **绝对值**别当成绩单看，它的用途是**同一套素材下横向比较不同参数**。
_S = np.abs(librosa.stft(xs, n_fft=1024, hop_length=160))
_D = np.abs(librosa.stft(xd, n_fft=1024, hop_length=160))
_nn = int(min(_S.shape[1], _D.shape[1], f0s.size, f0d.size))
_S, _D = _S[:, :_nn], _D[:, :_nn]
_en = np.sqrt(np.mean(_S ** 2, axis=0))
# ★ 门槛**不能用全局能量分位数**（踩过）：清辅音（s/sh/t/k）和字间气口本来就
#   落在低能量段，用"高于全局 25/60 分位"去筛，实测只剩个位数帧 —— 因为有能量
#   的帧全是带基频的元音。改成相对**峰值**的绝对门槛，只排掉真正的静音。
_uv = (f0s[:_nn] <= 1.0) & (_en > 0.05 * float(np.percentile(_en, 99)))
consonant_frames = int(_uv.sum())
consonant_sim = None
hf_src = hf_dst = 0.0
if consonant_frames >= 10:
    _a, _b = _S[:, _uv], _D[:, _uv]
    _na, _nb = np.linalg.norm(_a, axis=0), np.linalg.norm(_b, axis=0)
    _ok = (_na > 1e-9) & (_nb > 1e-9)
    if int(_ok.sum()) >= 10:
        consonant_frames = int(_ok.sum())
        consonant_sim = float(np.mean(np.sum(_a[:, _ok] * _b[:, _ok], axis=0)
                                      / (_na[_ok] * _nb[_ok])))
        hf_src = float(np.sum(_S[256:, _uv]) / max(1e-9, np.sum(_S[:, _uv])))
        hf_dst = float(np.sum(_D[256:, _uv]) / max(1e-9, np.sum(_D[:, _uv])))

data = {
    "src_med": round(src_med, 2), "dst_med": round(dst_med, 2),
    "shift_semitones": round(shift, 2),
    # —— 音准（全部在 lag=0 处算，见文件上方"时间轴怎么对齐"那段）——
    "match_ratio": round(match_ratio, 4),            # ±50 音分内；原口径，保留
    "match_ratio_loose": round(match_ratio_loose, 4),  # ±150 音分内；主判据
    "octave_err_ratio": round(octave_err, 4),        # |偏差| 落在 900~1500 音分的比例
    "contour_corr": round(contour_corr, 4),          # lag=0 处的旋律轮廓相关
    "contour_corr_best": round(contour_corr_best, 4),  # 全扫描里的最高相关（仅参考）
    "contour_lag_ms": round(contour_lag_ms, 1),      # 最高相关所在位置（**不用于对齐**）
    "dev_iqr_cents": round(dev_iqr, 1),
    "src_iqr_cents": round(src_iqr_c, 1),            # 原唱的音高起伏幅度
    "dst_iqr_cents": round(dst_iqr_c, 1),            # 角色的音高起伏幅度
    "spread_ratio": round(spread_ratio, 4),          # ≈1 忠实；≈0 说明唱成了平线
    # —— 时间轴（来自「唱/不唱」图案，不随时移平滑，比 f0 轮廓可靠）——
    "lag_ms": round(lag_ms, 1),
    "aligned_frames": aligned_frames,
    "voiced_lag_ms": round(lag_ms, 1), "voiced_lag_corr": round(voiced_lag_corr, 4),
    "hop_ms": round(hop_ms, 3), "frames": m,
    "voiced_src": src_n, "voiced_dst": dst_n,
    "dst_peak": round(dst_peak, 4), "clip_ratio": round(clip_ratio, 6),
    "dst_rms": round(dst_rms, 4),
    # —— 咬字 / 清辅音（无 f0 但有能量的帧上量，给 protect 定标）——
    "consonant_sim": (round(consonant_sim, 4) if consonant_sim is not None else None),
    "consonant_frames": consonant_frames,
    "hf_ratio_src": round(hf_src, 4), "hf_ratio_dst": round(hf_dst, 4),
    "skipped_secs": round(float(skip_frames.sum()) * hop_ms / 1000.0, 2),
    "skipped_spec": SKIPSPEC,
}
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False)
print("QC_DONE 平移 %+.2f 半音 | 吻合(±50) %.1f%% / 宽松(±150) %.1f%% | 八度错 %.1f%% "
      "| 轮廓相关 %+.3f @0ms | 起伏比 %.2f | 错位 %+.0fms | 咬字相似度 %s | 削幅 %.3f%%%s"
      % (shift, match_ratio * 100, match_ratio_loose * 100, octave_err * 100,
         contour_corr, spread_ratio, lag_ms,
         ("%.3f" % consonant_sim) if consonant_sim is not None else "n/a",
         clip_ratio * 100,
         ("  | 已跳过非歌声段 %.1f 秒（那几段故意不变调，不该算进音准）"
          % (float(skip_frames.sum()) * hop_ms / 1000.0)) if skip_frames.any() else ""))
print(json.dumps(data, ensure_ascii=False))
