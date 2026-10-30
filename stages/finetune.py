# -*- coding: utf-8 -*-
r"""
精修台：把一条人声 / 成品切成一个个「音块」，让你逐个改调、补音、去哑音、拆叠音。

================================================================================
为什么要有这个模块
================================================================================

分离和变声是**整轨**动作：一次把整首歌过一遍模型。可真正难听的那几处往往是
局部的 —— 副歌第二句「了」字哑掉、某句尾巴糊在混响里、和声和主唱叠在一起
互相打架。整轨重跑解决不了这些，只会换个地方哑。

所以这里做的是**音块级**编辑（就是 RipX 那类工具干的事）：
先把音频按音节切开，再让你对每一块单独下指令。

================================================================================
两条硬约束（决定了实现方式，别随手改）
================================================================================

1. **只依赖 numpy + soundfile**。
   服务进程用的是 GSV runtime 那个解释器（numpy 1.26 + soundfile 都有）。
   librosa 能用但**不是必须**：本模块要的是「点开就出结果」，而 librosa 的
   pyin 跑一首 4 分钟的歌要几十秒。所以基频用**逐块自相关**算 ——
   每块只有 0.1~1 秒，全曲几百块加起来也比整首跑一遍 pyin 快一个数量级。
   变调那一步倒是用得上 librosa（相位声码器质量更好），所以它是**可选加速**，
   没有就退回 numpy 重采样（见 `_pitch_shift`）。

2. **自动修复只产出 edits 列表，绝不就地改音频**。
   「一键去哑音」点下去，发生的是"生成了一份修改清单"，用户能在界面上
   逐个看到它打算干什么、逐条撤销。直接改音频的话，一点就没了、没法比对，
   而且用户根本不知道它动了哪几块 —— 这正是"自动修音"最招人骂的地方。

================================================================================
对外接口（server.py 只调这些）
================================================================================

    analyze(path, opts)            -> 切块 + 量特征 + 诊断，返回给界面画钢琴卷帘
    auto_fix(blocks, diag, kind)   -> 生成 edits（上面第 2 条）
    render(src, blocks, edits,out) -> 按 edits 渲染出 wav
    split_layers(src, blocks, ...) -> 按音高把音块分层导出（手动分和声）
    dereverb_whole(src, out, amt)  -> 整轨去混响

命令行自检（改完这个文件的第一件事）：
    python stages/finetune.py <音频> <输出json>
"""
import json
import os
import sys
import wave

import numpy as np

# ---------------------------------------------------------------------------
# 常量：每一个都写了"为什么是这个数"，改之前先读
# ---------------------------------------------------------------------------

SR_WORK = 44100          # 统一工作采样率：UVR/ffmpeg 那一侧也是 44.1k，口径一致
WIN_RMS = 1024           # RMS 窗 ~23ms：比一个音节短，比一个基频周期长
HOP_RMS = 256            # ~5.8ms：找起音要够细，粗了起音点会偏
MIN_BLK = 0.06           # 短于 60ms 的不是音节（是齿音/气声），丢掉
MAX_BLK = 1.10           # 长于 1.1s 的八成是几个音黏一起了，要在能量谷处切开
GAP_MERGE = 0.12         # 间隔小于 120ms 的算同一个音（换气不会这么短）
ATTACK_BACK = 0.05       # 起音往前多留 50ms：能量门限会吃掉辅音，得还回去

F0_MIN, F0_MAX = 70.0, 1100.0   # 人声基频范围（含假声/童声）
HARM_DEAD = 0.34         # 谐波度低于这个 ⇒ 有声音但没清晰音高（糊在混响里）
HARM_OK = 0.55           # 高于这个算"音高清楚"
RMS_DEAD = 0.30          # 块的 RMS 低于邻域中位数的这个比例 ⇒ 哑音
RMS_WEAK = 0.55          # ⇒ 偏弱（提示，不算问题）
STACK_MIN = 0.45         # 第二个基频的自相关峰 ≥ 主峰的这个比例 ⇒ 叠音
GAP_IOI = 1.7            # 间隔 ≥ 中位间隔的这个倍数 ⇒ 可疑空档（可能少了个音）
GAP_MIN = 0.20           # 且至少 200ms（比这短的是正常停顿）
CLIP_LVL = 0.995         # 峰值贴到这个 ⇒ 爆音

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def midi_name(m):
    """MIDI 音高 -> 「A3」这种写法。m<=0 表示没量出来。"""
    if not m or m <= 0:
        return "—"
    m = int(round(m))
    return "%s%d" % (NOTE_NAMES[m % 12], m // 12 - 1)


def f0_to_midi(f):
    return 69 + 12 * np.log2(f / 440.0) if f and f > 0 else 0.0


# ---------------------------------------------------------------------------
# 读写：统一成 float32，保留声道
# ---------------------------------------------------------------------------

def read_audio(path, sr=None):
    """读成 float32。返回 (y, sr)；y 形状是 (n,) 单声道 或 (n, ch) 多声道。"""
    try:
        import soundfile as sf
        y, native = sf.read(path, dtype="float32", always_2d=True)
        y = np.asarray(y, dtype=np.float32)
        if sr and native != sr:
            y = _resample_linear(y, native, sr)
            native = sr
        if y.shape[1] == 1:
            return y[:, 0], native
        return y, native
    except Exception:
        pass
    # 兜底：标准库 wave（只有 PCM 16/24/32bit，够用了）
    with wave.open(path, "rb") as w:
        ch, sw, fs, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    if sw == 2:
        a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        a = np.frombuffer(raw, dtype=np.uint8).astype(np.float32) / 128.0 - 1.0
    y = a.reshape(-1, ch)
    if sr and fs != sr:
        y = _resample_linear(y, fs, sr)
        fs = sr
    return (y[:, 0] if ch == 1 else y), fs


def write_wav(path, y, sr):
    """写 16bit PCM wav。`y` 可以是 (n,) 或 (n, ch)。"""
    y = np.asarray(y, dtype=np.float32)
    if y.ndim == 1:
        y = y[:, None]
    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 1.0:                       # 只在真超了的时候压，别每次都动音量
        y = y / peak * 0.99
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        import soundfile as sf
        sf.write(path, y, sr, subtype="PCM_16")
        return path
    except Exception:
        pass
    import wave as _w
    pcm = (np.clip(y, -1.0, 1.0) * 32767.0).astype("<i2")
    with _w.open(path, "wb") as w:
        w.setnchannels(y.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return path


def _mono(y):
    return y if y.ndim == 1 else y.mean(axis=1)


def _resample_linear(y, sr_in, sr_out):
    """线性插值重采样（够用：只在"采样率不统一"时才走这一步）。"""
    if sr_in == sr_out:
        return y
    n = y.shape[0]
    m = max(1, int(round(n * float(sr_out) / float(sr_in))))
    src = np.arange(n, dtype=np.float64)
    dst = np.linspace(0, n - 1, m)
    if y.ndim == 1:
        return np.interp(dst, src, y.astype(np.float64)).astype(np.float32)
    out = np.empty((m, y.shape[1]), dtype=np.float32)
    for c in range(y.shape[1]):
        out[:, c] = np.interp(dst, src, y[:, c].astype(np.float64))
    return out


# ---------------------------------------------------------------------------
# 特征：基频（自相关）/ 谐波度 / 频谱平坦度
# ---------------------------------------------------------------------------

def _autocorr_f0(x, sr, fmin=F0_MIN, fmax=F0_MAX):
    """逐块基频：**归一化自相关**找峰。返回 (f0, harm, f2, r2)。

    * `f0`   主基频（Hz）；量不出来是 0
    * `harm` 主峰的归一化高度 0~1 —— 这就是"谐波度"：1 = 完美周期，
             0.3 左右 = 有声音但音高糊（混响/气声/噪声盖住了）
    * `f2`   第二个基频（Hz），0 表示没有
    * `r2`   第二峰 / 主峰

    为什么不用 librosa.pyin：pyin 是整首跑的（一首歌几十秒），而这里每块只有
    零点几秒 —— 全曲几百块各自做一次 FFT 自相关，实测比整首 pyin 快一个数量级，
    且对"短到 100ms 的音"反而更稳（pyin 需要足够长的上下文才敢判浊音）。

    为什么先减均值再归一化：直流分量会让 lag=0 附近的尾巴把真峰抬没了。
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if n < 128:
        return 0.0, 0.0, 0.0, 0.0
    x = x - float(x.mean())
    e = float(np.sqrt(np.dot(x, x)))
    if e <= 1e-9:
        return 0.0, 0.0, 0.0, 0.0
    x = x / e
    N = 1 << int(np.ceil(np.log2(2 * n)))
    X = np.fft.rfft(x, N)
    ac = np.fft.irfft(X * np.conj(X), N)[:n]
    if ac[0] <= 1e-12:
        return 0.0, 0.0, 0.0, 0.0
    ac = ac / ac[0]

    lo = max(2, int(sr / fmax))
    hi = min(int(sr / fmin), n - 2)
    if hi <= lo + 2:
        return 0.0, 0.0, 0.0, 0.0

    seg = ac[lo:hi + 1]
    k = int(np.argmax(seg)) + lo
    peak = float(ac[k])
    # 抛物线插值：整 lag 的分辨率在高频处太粗（A4=440Hz 时相邻 lag 差 3 个半音）
    # ★ 八度纠正：自相关最经典的翻车方式是把真基频的**低八度**当成基频
    #   （比如真 392Hz 被读成 196Hz）。判据是"k/2 处是不是同样高的一个峰"：
    #   真基频在 196Hz 时，lag(392) 落在它的**波谷**上（不是峰）；
    #   而 196 是 392 的误判时，lag(392) 处必然也是个等高（或更高）的峰。
    #   所以只在"确实又是一个局部极大、且高度 ≥ 0.90 主峰"时才往上翻。
    for m in (2, 3):
        j = int(round(k / m))
        if j >= lo and 0 < j < n - 1:
            if ac[j] >= ac[j - 1] and ac[j] >= ac[j + 1] and ac[j] > 0.90 * peak:
                k, peak = j, float(ac[j])
                break
    if 0 < k < n - 1:
        a, b, c = float(ac[k - 1]), float(ac[k]), float(ac[k + 1])
        den = a - 2 * b + c
        d = (a - c) / (2 * den) if abs(den) > 1e-12 else 0.0
        kk = k + float(np.clip(d, -1.0, 1.0))
    else:
        kk = float(k)
    f0 = float(sr) / kk if kk > 0 else 0.0
    if not (fmin <= f0 <= fmax):
        f0 = 0.0

    # ---- 第二个基频：找不是主峰谐波/次谐波的另一个峰（叠音的证据）----
    f2, r2 = 0.0, 0.0
    if f0 > 0 and peak > 0.2:
        cands = []
        for j in range(lo + 1, hi):
            if ac[j] <= ac[j - 1] or ac[j] < ac[j + 1]:
                continue                      # 只要局部极大
            if ac[j] < 0.30 * peak:
                continue
            f = float(sr) / j
            if _is_harmonic_of(f, f0):
                continue          # 是主音的整数倍/整数分之一 ⇒ 谐波，不是第二个音
            cands.append((float(ac[j]), f))
        if cands:
            cands.sort(reverse=True)
            r2, f2 = cands[0]
            r2 = r2 / peak
    return f0, float(np.clip(peak, 0.0, 1.0)), f2, float(np.clip(r2, 0.0, 1.0))


def _is_harmonic_of(f, f0, tol=0.06):
    """f 是不是 f0 的谐波 / 次谐波（整数倍或整数分之一）。

    ★ 两个方向都要查，而且**各自**判：只查 f/f0 的话，f0/2（低八度）算出来是
      0.5，`round(0.5)` 在 Python 里是 0（银行家舍入）⇒ 「round(ratio)>=1」
      这个守卫直接失效 ⇒ 自相关的那些次谐波峰全被当成"第二个音"
      （实测 7 个纯音块**全 7 个**都被判成叠音）。
    """
    if not f or not f0:
        return False
    for r in (f / f0, f0 / f):
        k = round(r)
        if k >= 1 and abs(r - k) / k < tol:
            return True
    return False


def _independent_partials(x, sr, fmin=F0_MIN, fmax=1400.0, top=6):
    """找出"互相不成整数倍"的几个最强谱峰 → [(幅度, 频率), ...] 按幅度降序。

    ★ 为什么叠音必须靠**谱峰**判，不能靠自相关：
      A4(440) 叠 E5(659) 这两个音合在一起，**公共周期是 1/220**（440=2×220、
      659≈3×220），所以自相关老老实实报 220Hz —— 那数学上没错，
      但 220 这个"虚拟基频"上**根本没有能量**，用户要看见的是 A4。
      而谱峰法直接在有能量的地方取值：440 最强 ⇒ 报 A4；659 跟 440 不成
      整数倍（1.5 倍）⇒ 是第二个音，叠音成立。
      反过来，一个单音的 2/3 次谐波（880、1320）会被"整数倍"这条滤掉，
      不会被误报成叠音 —— 这正是自相关那版做不到的事。
    """
    x = np.asarray(x, dtype=np.float64)
    n = x.size
    if n < 256:
        return []
    N = 1 << int(np.ceil(np.log2(max(2048, n * 4))))
    X = np.abs(np.fft.rfft((x - float(x.mean())) * np.hanning(n), N))
    fr = np.fft.rfftfreq(N, 1.0 / float(sr))
    lo = int(np.searchsorted(fr, fmin))
    hi = int(np.searchsorted(fr, fmax))
    mag, fs = X[lo:hi], fr[lo:hi]
    if mag.size < 16:
        return []
    mx = float(mag.max())
    if mx <= 1e-12:
        return []
    pk = []
    for j in range(1, mag.size - 1):
        if mag[j] >= mag[j - 1] and mag[j] > mag[j + 1] and mag[j] > 0.18 * mx:
            a, b, c = float(mag[j - 1]), float(mag[j]), float(mag[j + 1])
            den = a - 2 * b + c
            d = (a - c) / (2 * den) if abs(den) > 1e-12 else 0.0
            pk.append((b, float(fs[j] + float(np.clip(d, -1, 1)) * (fs[1] - fs[0]))))
    if not pk:
        return []
    pk.sort(reverse=True)
    keep = []
    for m, f in pk:
        if all(not _is_harmonic_of(f, kf, tol=0.07) for _, kf in keep):
            keep.append((m, f))
        if len(keep) >= top:
            break
    return keep


def _flatness(x):
    """频谱平坦度 0~1：1 = 白噪声，接近 0 = 纯音。

    用来区分「清晰的元音」和「被混响糊掉的尾巴」—— 后者的谐波结构被抹平了，
    平坦度会明显抬头。
    """
    x = np.asarray(x, dtype=np.float64)
    if x.size < 64:
        return 0.0
    w = np.hanning(x.size)
    X = np.abs(np.fft.rfft((x - x.mean()) * w))
    X = np.clip(X, 1e-10, None)
    return float(np.exp(np.log(X).mean()) / X.mean())


def _rms_env(x, sr, win=WIN_RMS, hop=HOP_RMS):
    """短时 RMS 包络。返回 (env, times)。"""
    n = x.size
    if n < win:
        return np.array([float(np.sqrt(np.mean(x ** 2)))]), np.array([0.0])
    nfr = 1 + (n - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(nfr)[:, None]
    fr = x[idx]
    env = np.sqrt((fr ** 2).mean(axis=1))
    return env, (np.arange(nfr) * hop + win / 2.0) / float(sr)


# ---------------------------------------------------------------------------
# 切块：把连续有声区分成音节
# ---------------------------------------------------------------------------

def segment(y, sr, min_blk=MIN_BLK, max_blk=MAX_BLK):
    """返回 [{"t0","t1"}, ...]（秒）。

    流程：能量门限找有声区 → 合并过近的 → 太长的在能量谷处切开 → 太短的丢掉
    → 起音往前补一点（能量门限会吃掉辅音）。
    """
    x = _mono(np.asarray(y, dtype=np.float32)).astype(np.float64)
    n = x.size
    if n < sr * 0.05:
        return []
    env, times = _rms_env(x, sr)
    if env.size == 0:
        return []

    # 门限：底噪的 2.5 倍，但至少 0.004（-48dBFS）。
    # 全曲都安静时 percentile 会退化成 0，那个下界是必须的。
    floor = float(np.percentile(env, 10))
    gate = max(floor * 2.5, 0.004, float(env.max()) * 0.04)
    # ★ 门限不许高过峰值的 45%：
    #   `floor` 取的是 p10，**整条音频都在响**（压缩得很狠的母带、没换气的清唱
    #   片段、或者用户截的 3 秒副歌）时 p10 落在响段上 ⇒ gate = 2.5×p10 会比
    #   峰值还高 ⇒ 整首判成静音 ⇒ **一个音块都切不出来**，而且不报错，
    #   界面上就是"0 个音块"，用户只能以为功能坏了。
    #   实测本项目 40 条真实成品：p10/峰值 最大 0.116（⇢ 门限 0.29×峰值），
    #   离 0.45 的上限还有一倍余量 ⇒ 这一刀**不会动到任何一条真实音频**，
    #   只把"整条都在响"那种从「切不出块」救成「整段算有声、再交给 _split_long
    #   在能量谷处切开」。宁可切得粗，也不能一个都切不出来。
    gate = min(gate, float(env.max()) * 0.45)

    on = env > gate
    # 形态学闭运算：填掉小于 GAP_MERGE 的缝。
    # ★ k 要取**一半**：腐蚀 k + 膨胀 k 能填掉的缝宽约是 2k 帧，直接拿
    #   GAP_MERGE 的帧数当 k 会把两倍的缝也填掉 —— 实测 120ms 的换气缝被
    #   一并抹平，7 个音只切出 5 块（两个音黏成一块，基频也就跟着瞎了）。
    close = max(1, int(round(GAP_MERGE * sr / HOP_RMS / 2.0)))
    if close > 1:
        on = _dilate(_erode(on, close), close)

    segs = _mask_runs(on)
    out = []
    for a, b in segs:
        t0, t1 = float(times[a]), float(times[b - 1])
        if t1 - t0 > max_blk:
            out += _split_long(x, sr, t0, t1, max_blk, min_blk)
        else:
            out.append((t0, t1))

    # 丢太短的 + 往前补起音 + **不许重叠**
    # ★ 为什么要显式钉死不重叠：起音往前补 50ms、尾巴往后留 20ms 之后，相邻两块
    #   会咬在一起（实测 0.00–0.43 和 0.41–0.90）。界面上无所谓，但**改一块就会
    #   顺带改掉邻居的开头** —— 用户明明只点了第 3 句，第 4 句的第一个字也跟着
    #   变了。所以后一块的起点必须顶到前一块的终点。
    res = []
    prev_end = 0.0
    for t0, t1 in out:
        if t1 - t0 < min_blk:
            continue
        a = max(0.0, t0 - ATTACK_BACK)
        b = min(n / float(sr), t1 + 0.02)
        a = max(a, prev_end)
        if b - a < min_blk * 0.5:
            continue
        res.append({"t0": round(a, 4), "t1": round(b, 4)})
        prev_end = b
    return res


def _erode(m, k):
    """一维腐蚀：只有连续 k 个都是 True 的位置才留 True。**长度不变**
    （左边补 k-1 个 False，否则结果会短 k-1 格，闭运算完末尾就少一截）。"""
    m = np.asarray(m, dtype=bool)
    if k <= 1 or m.size == 0:
        return m
    p = np.concatenate([np.zeros(k - 1, dtype=bool), m])
    c = np.cumsum(np.concatenate([[0], p.astype(np.int32)]))
    return (c[k:] - c[:-k]) >= k


def _dilate(m, k):
    """一维膨胀：True 左右各扩 k-1 格（长度不变）。"""
    m = np.asarray(m, dtype=bool)
    if k <= 1 or m.size == 0:
        return m
    out = m.copy()
    for s in range(1, k):
        out[s:] |= m[:-s]
        out[:-s] |= m[s:]
    return out


def _mask_runs(m):
    """布尔数组 -> [(start, end), ...] 每段连续 True。"""
    m = np.asarray(m, dtype=bool)
    if not m.any():
        return []
    d = np.diff(np.concatenate([[0], m.view(np.int8), [0]]))
    st = np.where(d == 1)[0]
    en = np.where(d == -1)[0]
    return [(int(a), int(b)) for a, b in zip(st, en)]


def _split_long(x, sr, t0, t1, max_blk, min_blk):
    """一段太长（几个音黏一起了）⇒ 在能量谷处切开。

    切点必须是**局部能量极小**，随便按等分切会把一个音切成两半。
    二分递归，直到每段 <= max_blk。
    """
    total = t1 - t0
    if total <= max_blk:
        return [(t0, t1)]
    a, b = int(t0 * sr), int(t1 * sr)
    seg = x[a:b]
    if seg.size < 256:
        return [(t0, t1)]
    env, times = _rms_env(seg, sr)
    if env.size < 8:
        return [(t0, t1)]
    # 只在中间 60% 里找谷：贴着两端切等于没切
    lo, hi = int(env.size * 0.2), int(env.size * 0.8)
    if hi <= lo:
        return [(t0, t1)]
    j = lo + int(np.argmin(env[lo:hi]))
    tc = float(t0 + times[j])
    # 切完两边都得还能叫"一个音"
    if tc - t0 < min_blk or t1 - tc < min_blk:
        return [(t0, t1)]
    return (_split_long(x, sr, t0, tc, max_blk, min_blk)
            + _split_long(x, sr, tc, t1, max_blk, min_blk))


# ---------------------------------------------------------------------------
# 分析：切块 + 逐块量特征
# ---------------------------------------------------------------------------

def analyze(path, max_blocks=2000):
    """主入口。返回给界面画钢琴卷帘用的那一大坨（见文件头「对外接口」）。"""
    y, sr = read_audio(path, sr=SR_WORK)
    mono = _mono(y).astype(np.float64)
    dur = mono.size / float(sr)
    segs = segment(y, sr)[:max_blocks]

    blocks = []
    for i, s in enumerate(segs):
        a, b = int(s["t0"] * sr), int(s["t1"] * sr)
        b = min(b, mono.size)
        if b - a < int(0.02 * sr):
            continue
        x = mono[a:b]
        f0, harm, _f2ac, _r2ac = _autocorr_f0(x, sr)
        # 主音高优先用**谱峰**（见 _independent_partials 里那段为什么）；
        # 自相关的结果只在谱峰法拿不到东西时才兜底。
        parts = _independent_partials(x, sr)
        f2, r2 = 0.0, 0.0
        if parts:
            fp = float(parts[0][1])
            if F0_MIN <= fp <= F0_MAX:
                f0 = fp
            if len(parts) >= 2 and parts[0][0] > 1e-12:
                f2 = float(parts[1][1])
                r2 = float(parts[1][0] / parts[0][0])
        rms = float(np.sqrt(np.mean(x ** 2)))
        blk = {
            "i": len(blocks),
            "t0": round(s["t0"], 4),
            "t1": round(s["t1"], 4),
            "f0": round(f0, 1),
            "f2": round(f2, 1),
            "midi": round(f0_to_midi(f0), 1) if f0 else 0.0,
            "name": midi_name(f0_to_midi(f0)) if f0 else "—",
            "rms": round(rms, 5),
            "peak": round(float(np.max(np.abs(x))), 4),
            "harm": round(harm, 3),
            "flat": round(_flatness(x), 4),
            "stack": round(r2, 3),
        }
        blocks.append(blk)

    diag = diagnose(blocks, sr)
    return {
        "ok": True,
        "path": path,
        "sr": sr,
        "dur": round(dur, 3),
        "channels": 1 if y.ndim == 1 else y.shape[1],
        "blocks": blocks,
        "diag": diag,
        "wave": _wave_peaks(mono, sr),
        "stats": {
            "blocks": len(blocks),
            "voiced": sum(1 for b in blocks if b["f0"] > 0),
            "rms_med": round(float(np.median([b["rms"] for b in blocks])) if blocks else 0.0, 5),
        },
    }


def _wave_peaks(mono, sr, buckets=1400):
    """给界面画波形用的峰值包络（每桶取最大绝对值）。"""
    n = mono.size
    if n == 0:
        return []
    k = max(1, n // buckets)
    m = (n // k) * k
    a = np.abs(mono[:m]).reshape(-1, k)
    return [round(float(v), 4) for v in a.max(axis=1)]


# ---------------------------------------------------------------------------
# 诊断：四类问题（+ 可疑空档）
# ---------------------------------------------------------------------------

def diagnose(blocks, sr=SR_WORK):
    """返回 {dead, weak, reverb, stack, clip, gap}。

    每条判据都写得"宁可漏报不可乱报"：诊断出来是要**建议用户改**的，
    把好好的音标成有问题，用户就得一个个去核对，比漏报更烦。
    """
    d = {"dead": [], "weak": [], "reverb": [], "stack": [], "clip": [], "gap": []}
    n = len(blocks)
    if not n:
        return d
    rms = np.array([b["rms"] for b in blocks], dtype=np.float64)
    # 邻域中位数：±3 块。用全曲中位数会误伤"整段副歌都轻"这种正常情况。
    for i, b in enumerate(blocks):
        lo, hi = max(0, i - 3), min(n, i + 4)
        med = float(np.median(rms[lo:hi]))
        rel = (b["rms"] / med) if med > 1e-9 else 1.0
        b["rel"] = round(rel, 3)
        if b["peak"] >= CLIP_LVL:
            d["clip"].append(i)
        if rel < RMS_DEAD and b["rms"] > 1e-4:
            d["dead"].append(i)
        elif rel < RMS_WEAK and b["rms"] > 1e-4:
            d["weak"].append(i)
        # 混响哑音：**有声但没清晰音高**。rms 不低（所以不是"没出声"），
        # 可谐波度塌了 + 频谱被抹平 ⇒ 听感就是"糊掉的一坨"。
        if b["harm"] < HARM_DEAD and b["flat"] > 0.02 and rel >= RMS_DEAD:
            d["reverb"].append(i)
        if b["f2"] > 0 and b["stack"] >= STACK_MIN:
            d["stack"].append(i)

    # 可疑空档：间隔比周围节奏明显长 ⇒ 这里八成掉了一个音（可以补）
    if n >= 4:
        ioi = [blocks[i + 1]["t0"] - blocks[i]["t1"] for i in range(n - 1)]
        med_ioi = float(np.median(ioi)) if ioi else 0.0
        onsets = [blocks[i + 1]["t0"] - blocks[i]["t0"] for i in range(n - 1)]
        med_on = float(np.median(onsets)) if onsets else 0.0
        for i in range(n - 1):
            g = blocks[i + 1]["t0"] - blocks[i]["t1"]
            if g >= GAP_MIN and med_on > 0 and g >= GAP_IOI * med_ioi \
                    and (blocks[i + 1]["t0"] - blocks[i]["t0"]) >= 1.7 * med_on:
                d["gap"].append({"after": i, "t0": round(blocks[i]["t1"], 3),
                                 "t1": round(blocks[i + 1]["t0"], 3),
                                 "dur": round(g, 3)})
    return d


# ---------------------------------------------------------------------------
# 自动修复：只生成 edits
# ---------------------------------------------------------------------------

def auto_fix(blocks, diag, kind, amt=1.0):
    """按 kind 生成一份 edits。绝不改音频，也不改 blocks。

    kind:
      dedead   哑音/偏弱 → 提增益到邻域中位数（够不到就 +12dB 封顶）
      refill   哑音 → 用音高最接近的邻近块补音（比提增益更像"真唱出来了"）
      dereverb 混响哑音 → 去混响 + 稍微提一点
      deoverlap 叠音/重叠 → 把叠在上面的那层压下去（duck）
      declip   爆音 → 压峰值
    """
    edits = []
    n = len(blocks)
    if not n:
        return edits
    rms = np.array([b["rms"] for b in blocks], dtype=np.float64)

    def _med(i):
        lo, hi = max(0, i - 3), min(n, i + 4)
        return float(np.median(rms[lo:hi]))

    if kind == "dedead":
        for i in diag.get("dead", []) + diag.get("weak", []):
            med = _med(i)
            cur = max(blocks[i]["rms"], 1e-6)
            target = max(med, cur)
            db = 20 * np.log10(target / cur)
            db = float(np.clip(db, 0.0, 12.0))     # 只提不压，且最多 +12dB
            if db >= 0.6:
                edits.append({"i": i, "gain": round(db, 1)})

    elif kind == "refill":
        for i in diag.get("dead", []):
            j = _best_donor(blocks, i)
            if j is None:
                continue
            edits.append({"i": i, "fill": j, "gain": 0.0})

    elif kind == "dereverb":
        for i in diag.get("reverb", []):
            med = _med(i)
            cur = max(blocks[i]["rms"], 1e-6)
            db = float(np.clip(20 * np.log10(max(med, cur) / cur), 0.0, 6.0))
            edits.append({"i": i, "dereverb": round(float(np.clip(amt, 0.1, 1.0)), 2),
                          "gain": round(db, 1)})

    elif kind == "deoverlap":
        for i in diag.get("stack", []):
            # 叠在上面那层（f2）压下去：主旋律留着，和声/残留让路
            edits.append({"i": i, "duck": round(float(np.clip(amt, 0.1, 1.0)), 2)})

    elif kind == "declip":
        for i in diag.get("clip", []):
            edits.append({"i": i, "limit": 0.97})
    return edits


def _best_donor(blocks, i, span=6):
    """给第 i 块找"最像它、可以用来补音"的那块。

    挑选顺序：① 有清晰音高（没音高的补过来还是哑的）
              ② 音高接近（差超过 5 个半音听起来就是跑调）
              ③ 离得近（同一句里的音更可能是同一个元音）
    找不到就返回 None —— **绝不随便抓一块凑数**（宁可拒绝不修）。
    """
    b = blocks[i]
    cands = []
    for j in range(max(0, i - span), min(len(blocks), i + span + 1)):
        if j == i:
            continue
        o = blocks[j]
        if o["f0"] <= 0 or o["harm"] < HARM_OK or o["rms"] <= 1e-5:
            continue
        if o.get("rel", 1.0) < 0.6:            # 它自己也是哑的，别拿它补
            continue
        d = 0.0 if b["f0"] <= 0 else abs(f0_to_midi(o["f0"]) - f0_to_midi(b["f0"]))
        if d > 5.0:
            continue
        cands.append((d + abs(j - i) * 0.35, j))
    if not cands:
        return None
    cands.sort()
    return int(cands[0][1])


# ---------------------------------------------------------------------------
# 变调 / 去混响（音块级 DSP）
# ---------------------------------------------------------------------------

def _pitch_shift(x, sr, semi):
    """半音平移。优先 librosa（相位声码器），没有就退回重采样。

    为什么整块平移而不是改 MIDI：这里的输入是**已经唱出来的音频**，
    不是音符。改 MIDI 得重新跑一次变声模型，而用户要的是"这一句低两个半音"
    这种当场就能听的微调 —— 那就只能动音频。
    """
    if abs(semi) < 1e-3:
        return x
    f = 2.0 ** (semi / 12.0)
    try:
        import librosa
        if x.ndim == 1:
            return librosa.effects.pitch_shift(
                x.astype(np.float32), sr=sr, n_steps=float(semi)).astype(np.float32)
        out = np.empty_like(x)
        for c in range(x.shape[1]):
            out[:, c] = librosa.effects.pitch_shift(
                x[:, c].astype(np.float32), sr=sr, n_steps=float(semi))
        return out.astype(np.float32)
    except Exception:
        pass
    # 退回：重采样改调 + 拉伸回原长（就是 ffmpeg 的 asetrate+atempo）
    n = x.shape[0]
    m = max(8, int(round(n / f)))
    src = np.linspace(0.0, 1.0, m, dtype=np.float64)
    dst = np.linspace(0.0, 1.0, n, dtype=np.float64)
    if x.ndim == 1:
        return np.interp(dst, src, x.astype(np.float64)).astype(np.float32)
    out = np.empty((n, x.shape[1]), dtype=np.float32)
    for c in range(x.shape[1]):
        out[:, c] = np.interp(dst, src, x[:, c].astype(np.float64))
    return out


def _stft(x, win=1024, hop=256):
    xp = np.pad(x, (0, win))
    nfr = 1 + (xp.size - win) // hop
    idx = np.arange(win)[None, :] + hop * np.arange(nfr)[:, None]
    return np.fft.rfft(xp[idx] * np.hanning(win)[None, :], axis=1)


def _ola(fr, hop, length):
    """重叠相加。帧起点间隔 hop，取每隔 win/hop 帧为一组 ⇒ 组内首尾相接不重叠，
    于是整个 OLA 变成 win/hop 次**向量**加法（不用 np.add.at，快得多）。"""
    nfr, win = fr.shape
    P = max(1, win // hop)
    out = np.zeros(length + win, dtype=np.float64)
    for p in range(P):
        flat = fr[p::P].ravel()
        if flat.size:
            out[p * hop: p * hop + flat.size] += flat
    return out[:length]


def _istft(S, win=1024, hop=256, length=None):
    w = np.hanning(win)
    fr = np.fft.irfft(S, axis=1) * w[None, :]
    n = length if length is not None else hop * (len(S) - 1) + win
    out = _ola(fr, hop, n)
    nrm = _ola(np.broadcast_to(w, (S.shape[0], win)).copy(), hop, n)
    return (out / np.maximum(nrm, 1e-9)).astype(np.float32)


def dereverb_1d(x, sr, amount=0.6):
    """谱减去混响。返回同长度 float32。

    噪声样本取**这一段的尾巴**（后 35%）：混响的特征就是"音已经停了、尾巴还在响"，
    所以尾巴那几帧的能量谱就是"该减掉的那层"。减完留 15% 底，不然会有
    "水下声"（音乐噪声）。
    """
    x = np.asarray(x, dtype=np.float32)
    if x.size < 2048 or amount <= 0:
        return x
    win, hop = 1024, 256
    S = _stft(x.astype(np.float64), win, hop)
    if S.shape[0] < 6:
        return x
    mag, ph = np.abs(S), np.angle(S)
    tail = mag[:, max(1, int(S.shape[1] * 0.65)):]
    if tail.shape[1] < 2:
        tail = mag[:, -2:]
    noise = np.median(tail, axis=1)
    mag2 = np.maximum(mag - float(amount) * noise[:, None], mag * 0.15)
    out = _istft(mag2 * np.exp(1j * ph), win, hop, x.size)
    # 补回原来的电平（谱减会整体掉音量，不然用户会觉得"修完反而小了"）
    e0 = float(np.sqrt(np.mean(x ** 2))) + 1e-9
    e1 = float(np.sqrt(np.mean(out ** 2))) + 1e-9
    return (out * min(e0 / e1, 4.0)).astype(np.float32)


def dereverb_nd(x, sr, amount=0.6):
    if x.ndim == 1:
        return dereverb_1d(x, sr, amount)
    out = np.empty_like(x)
    for c in range(x.shape[1]):
        out[:, c] = dereverb_1d(np.ascontiguousarray(x[:, c]), sr, amount)
    return out


def dereverb_whole(src, out, amount=0.6):
    """整轨去混响（分块做，避免一次 FFT 上千万点）。"""
    y, sr = read_audio(src, sr=SR_WORK)
    if y.ndim == 1:
        w = dereverb_1d(y, sr, amount)
    else:
        w = dereverb_nd(y, sr, amount)
    return write_wav(out, w, sr)


def _duck_1d(x, sr, amount=0.5):
    """压掉叠在上面的那层：高通之后按能量压。

    ★ 说清楚它到底是什么：**这不是把两个声音分开**（单声道混在一起的声音
      没法无损拆开），它是"把高频那层压小"。主唱在下面、和声/残留糊在上面时，
      听感上就是"打架"没了。真要完全分开请用 split_layers 导出两层自己摆。
    """
    if amount <= 0 or x.size < 512:
        return x
    win, hop = 1024, 256
    S = _stft(x.astype(np.float64), win, hop)
    mag = np.abs(S)
    half = mag.shape[1] // 2
    lo_e = float(np.mean(mag[:, :half] ** 2)) + 1e-12
    hi_e = float(np.mean(mag[:, half:] ** 2)) + 1e-12
    if hi_e <= lo_e * 1.2:
        return x                       # 高频本来就不突出 ⇒ 没什么可压的
    g = np.ones(mag.shape[1], dtype=np.float64)
    g[half:] = 1.0 - float(np.clip(amount, 0, 1)) * 0.85
    out = _istft(mag * g[None, :] * np.exp(1j * np.angle(S)), win, hop, x.size)
    return out.astype(np.float32)


# ---------------------------------------------------------------------------
# 渲染：把 edits 落到音频上
# ---------------------------------------------------------------------------

CTX = 0.12               # 变调时多取这么多上下文（相位声码器在块边界会糊）
FADE = 0.006             # 6ms 交叉淡化：直接拼接会"啪"一声


def render(src, blocks, edits, out, gain_db=0.0, normalize=True):
    """按 edits 渲染。返回输出路径。

    edits 每项 {"i": 块号, gain: dB, semi: 半音, shift: 秒, mute: bool,
                del: bool, fill: 源块号, dereverb: 0~1, duck: 0~1, limit: 峰值}
    ★ 没写到的块**原样保留**（连重采样都不做），所以"只改一句"不会让整首歌
      变得跟原来不一样。
    """
    y, sr = read_audio(src, sr=SR_WORK)
    out_arr = np.asarray(y, dtype=np.float32).copy()
    n = out_arr.shape[0]
    blocks = blocks or []

    for ed in (edits or []):
        try:
            i = int(ed.get("i", -1))
        except (TypeError, ValueError):
            continue
        if i < 0 or i >= len(blocks):
            continue
        b = blocks[i]
        a0 = int(round(b["t0"] * sr))
        a1 = min(n, int(round(b["t1"] * sr)))
        if a1 <= a0:
            continue

        mute = bool(ed.get("mute") or ed.get("del"))
        # ① 先把原位抹掉（两端淡出，不然会有咔哒）
        _erase(out_arr, a0, a1, sr)
        if mute:
            continue

        # ② 取源音频：补音就用别人的，否则用自己的
        src_i = ed.get("fill")
        if src_i is not None:
            try:
                j = int(src_i)
            except (TypeError, ValueError):
                j = i
            if 0 <= j < len(blocks) and j != i:
                sb = blocks[j]
                seg = _slice(y, sr, sb["t0"], sb["t1"])
                # 补过来的音要按目标音高对齐，不然就是跑调
                if b.get("f0") and sb.get("f0") and b["f0"] > 0 and sb["f0"] > 0:
                    seg = _pitch_shift(seg, sr,
                                       f0_to_midi(b["f0"]) - f0_to_midi(sb["f0"]))
                seg = _fit_len(seg, a1 - a0)
            else:
                seg = _slice(y, sr, b["t0"], b["t1"])
        else:
            c0 = max(0, a0 - int(CTX * sr))
            c1 = min(n, a1 + int(CTX * sr))
            seg_full = np.asarray(y[c0:c1])
            semi = float(ed.get("semi") or 0.0)
            if abs(semi) > 1e-3:
                seg_full = _pitch_shift(np.asarray(seg_full), sr, semi)
            # 去掉上下文，只留本体
            s0, s1 = a0 - c0, min(seg_full.shape[0], a1 - c0)
            seg = np.asarray(seg_full)[s0:s1]
            seg = _fit_len(seg, a1 - a0)

        seg = np.asarray(seg, dtype=np.float32)

        # ③ 逐条效果
        dv = float(ed.get("dereverb") or 0.0)
        if dv > 0:
            seg = dereverb_nd(seg, sr, dv)
        dk = float(ed.get("duck") or 0.0)
        if dk > 0:
            if seg.ndim == 1:
                seg = _duck_1d(seg, sr, dk)
            else:
                seg = np.stack([_duck_1d(np.ascontiguousarray(seg[:, c]), sr, dk)
                                for c in range(seg.shape[1])], axis=1)
        g = float(ed.get("gain") or 0.0)
        if abs(g) > 1e-3:
            seg = seg * np.float32(10.0 ** (g / 20.0))
        lim = float(ed.get("limit") or 0.0)
        if lim > 0:
            pk = float(np.max(np.abs(seg))) if seg.size else 0.0
            if pk > lim:
                seg = seg * np.float32(lim / pk)

        # ④ 放到目标位置（可移时）
        sh = float(ed.get("shift") or 0.0)
        t = a0 + int(round(sh * sr))
        _place(out_arr, seg, t, sr)

    if abs(gain_db) > 1e-3:
        out_arr = out_arr * np.float32(10.0 ** (gain_db / 20.0))
    if normalize:
        pk = float(np.max(np.abs(out_arr))) if out_arr.size else 0.0
        if pk > 0.99:                      # 只在真会削波时才动
            out_arr = out_arr * np.float32(0.99 / pk)
    return write_wav(out, out_arr, sr)


def _slice(y, sr, t0, t1):
    a, b = int(round(t0 * sr)), int(round(t1 * sr))
    b = min(b, y.shape[0])
    if b <= a:
        return np.zeros((1,), dtype=np.float32) if y.ndim == 1 else np.zeros((1, y.shape[1]), dtype=np.float32)
    return np.asarray(y[a:b], dtype=np.float32)


def _fit_len(seg, want):
    """把 seg 调成正好 want 个采样点（长了截，短了补静音）。"""
    seg = np.asarray(seg, dtype=np.float32)
    have = seg.shape[0]
    if have == want:
        return seg
    if have > want:
        return seg[:want]
    if seg.ndim == 1:
        return np.concatenate([seg, np.zeros(want - have, dtype=np.float32)])
    return np.concatenate([seg, np.zeros((want - have, seg.shape[1]), dtype=np.float32)])


def _erase(arr, a0, a1, sr):
    """把 [a0,a1) 抹成静音。

    ★ 这里**不做**边缘淡化：抹完要贴回去的新信号自己带淡入淡出（见 `_place`），
      在已经被抹成 0 的区域上再乘一遍斜坡等于什么都没做（写错的那一版就是这么
      来的 —— 先置零再乘 ramp，结果是 0*ramp=0，白算）。
      音块的边界本来就在能量低的地方（切块就是按能量门限切的），
      所以"外面有声、里面是 0"的台阶很小。
    """
    if arr.ndim == 1:
        arr[a0:a1] = 0.0
    else:
        arr[a0:a1, :] = 0.0


def _place(arr, seg, t, sr):
    """把 seg 加到位置 t 上（与已有内容相加，并做 6ms 淡入淡出）。"""
    n = arr.shape[0]
    if t >= n or t + seg.shape[0] <= 0:
        return
    f = max(1, int(FADE * sr))
    s0, s1 = max(0, t), min(n, t + seg.shape[0])
    g0, g1 = s0 - t, s1 - t
    piece = seg[g0:g1]
    if piece.size == 0:
        return
    if seg.ndim == 1:
        env = np.ones(piece.shape[0], dtype=np.float32)
    else:
        env = np.ones((piece.shape[0], 1), dtype=np.float32)
    m = min(f, piece.shape[0] // 2)
    if m > 0:
        up = np.linspace(0.0, 1.0, m, dtype=np.float32)
        dn = np.linspace(1.0, 0.0, m, dtype=np.float32)
        if seg.ndim == 1:
            env[:m], env[-m:] = up, dn
        else:
            env[:m, 0], env[-m:, 0] = up, dn
    piece = piece * env
    if arr.ndim == 1:
        arr[s0:s1] = arr[s0:s1] + piece
    else:
        arr[s0:s1, :] = arr[s0:s1, :] + piece


# ---------------------------------------------------------------------------
# 分层导出（手动分和声 / 处理重叠）
# ---------------------------------------------------------------------------

def split_layers(src, blocks, out_dir, n=2, names=None):
    """按音高把音块分成 n 层，每层导出一个 wav（不属于该层的部分静音）。

    这是"手动分和声"的工程实现：**按音高归层**，不是把叠在一起的两个声音
    无损拆开（单声道混好的声音做不到无损拆）。界面上必须照实说这句。

    分层用"排序后取最大的 n-1 个间隔切"（一维聚类的标准做法），
    比 k-means 稳：不会因为初始点随机而每次结果不一样。

    返回 [{"layer":0,"name":"…","path":"…","blocks":k}, ...]
    """
    y, sr = read_audio(src, sr=SR_WORK)
    n = max(2, min(int(n or 2), 4))
    voiced = [(i, b) for i, b in enumerate(blocks or []) if (b.get("f0") or 0) > 0]
    layer_of = {}
    edges = []
    if len(voiced) >= n:
        f0s = np.sort(np.array([b["f0"] for _, b in voiced], dtype=np.float64))
        cents = 12 * np.log2(f0s / 440.0)
        gaps = np.diff(cents)
        order = np.argsort(gaps)[::-1][:n - 1]
        edges = sorted([float((cents[i] + cents[i + 1]) / 2.0) for i in order])
        for i, b in voiced:
            c = 12 * np.log2(b["f0"] / 440.0)
            k = 0
            for e in edges:
                if c > e:
                    k += 1
            layer_of[i] = k
    else:
        for i, b in voiced:
            layer_of[i] = 0

    os.makedirs(out_dir, exist_ok=True)
    res = []
    base = os.path.splitext(os.path.basename(src))[0]
    default_names = ["主旋律（低）", "和声层（高）", "第三层", "第四层"][:n]
    names = names or default_names
    for k in range(n):
        arr = np.zeros_like(np.asarray(y, dtype=np.float32))
        cnt = 0
        for i, b in enumerate(blocks or []):
            if layer_of.get(i, 0 if (b.get("f0") or 0) > 0 else -1) != k:
                continue
            a0 = int(round(b["t0"] * sr))
            a1 = min(arr.shape[0], int(round(b["t1"] * sr)))
            if a1 <= a0:
                continue
            seg = np.asarray(y[a0:a1], dtype=np.float32)
            if arr.ndim == 1:
                arr[a0:a1] = seg
            else:
                arr[a0:a1, :] = seg
            cnt += 1
        p = os.path.join(out_dir, "%02d_%s.wav" % (k + 1, names[k]))
        write_wav(p, arr, sr)
        res.append({"layer": k, "name": names[k], "path": p, "blocks": cnt,
                    "cents_low": round(edges[k - 1], 1) if k > 0 and k - 1 < len(edges) else None,
                    "cents_high": round(edges[k], 1) if k < len(edges) else None})
    return res


# ---------------------------------------------------------------------------
# 命令行自检
# ---------------------------------------------------------------------------

def _main():
    if len(sys.argv) < 3:
        print("用法：python stages/finetune.py <音频> <输出json>")
        return 2
    d = analyze(sys.argv[1])
    with open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    print("音块 %d 个，时长 %.1fs" % (d["stats"]["blocks"], d["dur"]))
    for k in ("dead", "weak", "reverb", "stack", "clip"):
        print("  %-8s %d" % (k, len(d["diag"][k])))
    print("  可疑空档 %d" % len(d["diag"]["gap"]))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
