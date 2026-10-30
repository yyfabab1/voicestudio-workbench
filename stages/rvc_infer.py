# -*- coding: utf-8 -*-
r"""RVC 推理 —— 自己调 VC，而不是走 `infer.cli`。

为什么不直接用整合包自带的 `python -m infer.cli`：
  1. 那个 CLI 的 `--f0-method` 只放开了 `pm / rmvpe`，**fcpe 被挡住了**
     （pipeline 本身是支持 fcpe 的，见 infer/vc/pipeline.py 的 get_f0）。
     FCPE 比 RMVPE 更准更快，是"唱得准不准"最直接的一块。
  2. CLI 不让我们把「没有可用索引」和「索引命中了但用不上」分开处理；
     这里要能拿到 status 文字，好在日志里说清到底用了哪个 f0 方法、有没有索引。
  3. 后面要加的「非歌声段保护」需要拿**同一份 f0 曲线**做段级判定 ——
     走自己的入口才拿得到中间量。

**不改 RVC 整合包的任何文件**：只 import 它的模块。整合包是用户自己装的第三方件，
改它等于把我们的版本和用户的版本绑死，下次他升级就炸。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器）：
    runtime\python.exe -s <本脚本> --model <模型.pth 或 文件名> --input <in.wav> \
        --output <out.wav> [--pitch 0] [--f0 rmvpe|fcpe|pm] [--index-rate 0.75] \
        [--index <index 路径>] [--rms-mix-rate 0.25] [--protect 0.33] \
        [--speaker-id 0] [--resample-sr 0]

退出码：0 成功；2 参数/环境错误；1 推理失败。
"""
import argparse
import atexit
import math
import os
import re
import shutil
import sys
import tempfile
import time
import traceback

if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
os.environ.setdefault("weight_root", os.path.join(os.getcwd(), "assets", "weights"))
os.environ.setdefault("index_root", os.path.join(os.getcwd(), "logs"))
os.environ.setdefault("outside_index_root", os.path.join(os.getcwd(), "assets", "indices"))

# ★ 启动时的工作目录要留一份：下面「每段起一个子进程」必须把它传给孙进程。
#   这几个 *_root 全是按 os.getcwd() 拼的，而孙进程继承的是**父进程的** cwd ——
#   只要最外层调用方没有把 cwd 设成 RVC 根目录（studio.py 的 run(..., cwd=RVC_ROOT)
#   一直是设的，但别的入口未必），孙进程就会去
#   `<别处>\assets\rmvpe\rmvpe.pt` 找模型，报一个跟"分段"八竿子打不着的
#   FileNotFoundError。踩过一次（2026-10-23 长歌探针），所以这里钉死并打印出来。
INIT_CWD = os.getcwd()
print("[dbg] cwd=%s | rmvpe_root=%s" % (INIT_CWD, os.environ.get("rmvpe_root")))


def _index_owner(index_name):
    """这个索引文件**属于哪个模型** —— 取名字里第一个 `_added_` 之前的部分。

    为什么需要它（实测踩到的真事故，2026-10-20）：
        train_index.py 的命名是
            <模型名>_added_IVF<n>_Flat_nprobe_1_<模型名>_v1.index
        所以 `split("_added_")[0]` 就是它服务的那个模型的 stem，一点不含糊。

    而 RVC 自带的 `get_index_path_from_model()` 认的是「名字里出现过实验名」，
    最后的 tie-break 是 **`-mtime`（最新的赢）**。于是同一个角色目录下只要
    多试过几个模型，就会出现：
        ra500fb92.pth（v1，256 维输出）
          → 被配上 ra500fb92_v2_klm48_..._v2.index（v2，768 维）
          → faiss 的 `assert d == self.d` 直接崩
          → 整首歌"歌声转换失败"，报错只有一行 AssertionError，根本看不出是索引配错。
    实测复现：`get_index_path_from_model("ra500fb92.pth", 0)` 确实返回 v2_klm48 的索引。
    这是"多模型共存"才会触发的 bug，而多试几个模型恰恰是正常用法。

    没有 `_added_` 的名字（合并索引 / 老命名）返回空串，交给原规则兜底。
    """
    stem = os.path.splitext(os.path.basename(index_name or ""))[0].lower()
    i = stem.find("_added_")
    if i <= 0:
        return ""
    return stem[:i]


def pick_index(model_name, speaker_id=0):
    """按「索引归属」给这个模型挑它自己的 added 索引，返回路径或空串。

    口径与 RVC 一致的地方（否则会出现"界面说有索引、推理说没有"）：
      · 跳过名字带 trained 的（那是合并索引，不是 added）；
      · 认 `<exp>_added_*`、含 `_<exp>_v1` / `_<exp>_v2` 的；
      · 模型名带 `_e<轮>_s<步>` 尾巴时，先去掉再比。
    比 RVC **更严**的地方：候选索引的"归属模型"必须就是本模型 ——
    仅仅名字里含实验名不算（那正是上面那个 bug）。
    """
    stem = os.path.splitext(os.path.basename(str(model_name or "")))[0]
    exp = re.sub(r"_e\d+_s\d+$", "", stem, flags=re.IGNORECASE).lower()
    if not exp:
        return ""
    stem_low = stem.lower()
    roots = [os.environ.get("outside_index_root"), os.environ.get("index_root")]
    best, best_mtime = "", -1.0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for name in files:
                low_name = name.lower()
                if not low_name.endswith(".index") or "trained" in low_name:
                    continue
                owner = _index_owner(name)
                if not owner:
                    continue
                # 归属必须是本模型：完整 stem，或去掉 _eN_sN 之后的实验名。
                if owner not in (stem_low, exp):
                    continue
                path = os.path.abspath(os.path.join(dirpath, name))
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                if mt > best_mtime:
                    best, best_mtime = path, mt
    return best


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--pitch", type=int, default=0, help="变调半音数")
    p.add_argument("--f0", dest="f0_method", default="rmvpe",
                   choices=["pm", "rmvpe", "fcpe"])
    p.add_argument("--index-rate", type=float, default=0.75)
    p.add_argument("--index", default="", help="不传就按 RVC 自己的规则自动找")
    p.add_argument("--rms-mix-rate", type=float, default=0.25)
    p.add_argument("--protect", type=float, default=0.33)
    p.add_argument("--speaker-id", type=int, default=0)
    p.add_argument("--resample-sr", type=int, default=0)
    p.add_argument("--maxseg", type=float, default=30.0,
                   help="外层分段：每段一个干净子进程，最长这么多秒（0=关掉）。"
                        "默认 30 —— 实测这份量得到的抖动最低，见 convert_segmented。")
    p.add_argument("--chunk", type=float, default=20.0,
                   help="分片秒数：把长音频切成这么大的片喂给模型（0 = 保持 RVC 自带的 60 秒）")
    p.add_argument("--no-align", dest="align", action="store_false",
                   help="关掉「把成品挪回源的时间轴」（默认开；实测值 7 个百分点，"
                        "见 align_phase）。只在排查对齐问题时才关。")
    p.add_argument("--no-f0guard", dest="f0_guard", action="store_false",
                   help="关掉最后那道「挪了到底涨没涨」的闸门（默认开）。"
                        "这道闸用 RMVPE 直接量音准，实测能挡下「三频带一致但挪过去"
                        "反而掉 4.8 点」那种段。只在做 A/B 时才关。")
    return p


def plan_slices(dur_sec, chunk_sec, tail=5.0):
    """这段音频会切成几片（1 = 不分片，一次跑完）。

    口径照抄 Pipeline.pipeline() 的切片循环：
      · 比 `chunk + tail` 短的音频**根本不进**分片分支（`audio_pad.shape[0] > t_max`）；
      · 进了就每 chunk 一个切点，最后剩的那截算一片。
    页数写进日志，好在"到底切没切"这件事上有据可查（不然十几分钟 vs 十几秒
    的差别在日志里是看不出来的）。
    """
    try:
        dur = float(dur_sec or 0)
    except (TypeError, ValueError):
        return 1
    if dur <= 0:
        return 1
    if not chunk_sec or chunk_sec <= 0:
        return 1
    if dur <= chunk_sec + tail:
        return 1
    n = int(dur // chunk_sec)
    if dur - n * chunk_sec > 1e-6:
        n += 1
    return max(1, n)


def align_duration(y, want, sr):
    """把音频按长度拉伸/压缩到正好 `want` 个样本（在 sr 采样率下）。

    为什么需要它 —— 这是 2026-10-22 查出来的「越唱越差」的根因，证据链：
      · 同一段素材单独跑：偏差 IQR 7.1 cents、与源对齐 0.0 ms；
        同一段放进 204 秒整首里跑：IQR 54.0 cents、对齐 **-70 ms**（7.6 倍）。
      · tools/probe_drift.py 逐窗量对齐：0s 处 +4.1ms → 180s 处 -57.0ms，
        位置 vs 滞后 rho = **-0.969**、斜率 -0.311 ms/秒、相邻窗跳变中位仅 2.9 ms
        ⇒ 是**均匀拉伸**（时长比例错了一点），不是分片边界跳变。
      · 源 204.080 秒 / 成品 204.000 秒 —— 成品整体短了 **80 ms**。
    这个误差随位置线性累积 ⇒ 混音时人声相对伴奏越来越"抢"，到结尾差 70 ms，
    听感就是「越唱越差、后面发飘/发破」，但它不涉及音色，所以八度错、谐波性
    这类常规指标全部正常 —— 只看质检数会发现不了，得专门去量对齐。

    缩放比例只有 ~1.0004 ⇒ 音高改变 = 1200*log2(1.0004) ≈ **0.7 音分**，
    远低于人耳可辨的 5~10 音分，所以校正是安全的。

    返回 (新数组, 实际漂移毫秒)；不需要动或无法处理时原样返回。
    """
    have = int(len(y))
    if want <= 0 or have <= 0:
        return y, 0.0
    import numpy as _np            # ★ 模块顶部刻意不 import numpy（延迟加载），这里局部引入
    if abs(have - want) <= max(1, int(sr * 0.002)):   # 2 ms 以内不动，避免无意义的重采样
        return y, 0.0
    src = _np.asarray(y, dtype="float64")
    try:
        from scipy.signal import resample          # FFT 重采样：任意长度比，质量最好
        out = resample(src, want)
    except Exception:
        xi = _np.linspace(0.0, have - 1.0, want)   # 兜底线性插值
        out = _np.interp(xi, _np.arange(have, dtype="float64"), src)
    if _np.issubdtype(_np.asarray(y).dtype, _np.integer):
        info = _np.iinfo(_np.asarray(y).dtype)
        out = _np.clip(_np.rint(out), info.min, info.max).astype(_np.asarray(y).dtype)
    else:
        out = out.astype(_np.asarray(y).dtype, copy=False)
    return out, (have - want) / float(sr) * 1000.0


def plan_cuts(y16, maxseg, search=3.0, minseg=6.0):
    """把一段 16k 音频切成若干 <=maxseg 秒的段，返回内部的切点（秒）。

    为什么要在外面再切一层 —— RVC 自己的 Pipeline 虽然也会切片（见 apply_chunk），
    但**整段从头到尾是一次 `pipeline()` 调用**，而实测「一次调用喂多长」本身
    就是质量与速度的双杀，而且不是线性变差：

        总输入  30s(2片)  60s(3片)  90s(5片)  200s(10片)
        推理    8.5s      14.4s     150.2s    784.6s     ← 每片成本 4.3→78 秒
        同一素材 X 的逐帧偏差 IQR   17.2 / 19.2 / 24.4 / 25.4 音分

    把 X 单独跑是 17.2，塞进 200 秒里就变成 25.4 —— **素材没变，是调用太长**。
    所以这里在 RVC 的切片之上再加一层：每次 pipeline 只吃 <=maxseg 秒。

    切点为什么取能量最小处：直接等分会把音符从中间劈开，接回去那段会有可听的
    接缝。在 ±search 秒的窗里挑最安静的位置（和 RVC 自己 opt_ts 一个思路），
    能把切口藏在换气/齿音后面。

    ★ 2026-10-23 加的硬约束（此前这里有个真 bug）
    ------------------------------------------------
    老实现只在 `c±search` 里找能量最低点，**没有管段长** ⇒ 相邻切点间距可以是
    `step ± 2*search`，实际段长能到 maxseg+6（实测 204 秒歌出现 32.85 秒的段）。
    段长失控带来两个后果：
      ① 段长超过 RVC 的 `t_max`（= chunk+5）时，它会**在段内再切一刀**，而那一刀
         的搜索窗被段末尾截断，argmin 会被末尾静音吸走 —— 实测切出过
         **1.76 秒的碎片**（`tools/probe_innercut.py` 可复现）。
      ② 更要命的是分片本身：RVC 每片输出都比应有长度多 `window/sr` = 10 ms，
         片与片串起来是**累积错位**，而我们的 `align_duration` 只拉总长、不修片间
         相对偏移。同一段 26 秒素材，内部分 2 片 vs 不分片实测：
             吻合 84.9% → 93.5%   偏差 IQR 25.9 → 12.4 音分   lag 20 → 10 ms
         这就是「分段后第 1 段掉 11 个点」的真正来源 —— 与外部分段无关，
         是 RVC 内部那一刀切的。
    所以这里现在**保证每段严格落在 [minseg, maxseg] 内**（用约束传播：既要本段
    装得下，也要给后面剩下的段数留够位置），外面再把 chunk 顶到 maxseg+5，
    RVC 内部就永远不会进分片分支。
    """
    import numpy as np          # ★ 顶层的 np 是在 main() 里才 import 的，这里补局部的

    sr = 16000
    total = len(y16) / float(sr)
    if maxseg <= 0 or total <= maxseg:
        return []
    ms = float(maxseg)
    mn = float(minseg)
    if ms < 2.0 * mn:
        mn = ms / 2.0                      # 下限不能大到把上限挤没
    win = int(0.05 * sr)                   # 50 ms 的短时能量窗

    def try_n(n):
        """按 n 段切一遍；只要有任何一处窗口被挤到没法挑，就整个方案作废。"""
        step = total / n
        cuts = []
        prev = 0.0
        for i in range(1, n):
            c = i * step
            left = n - i                   # 后面还剩几段
            # ★ 约束传播：本段 ≥ mn 且 ≤ ms；同时剩下的 left 段每段也要 ∈ [mn, ms]
            lo = max(prev + mn, total - left * ms)
            hi = min(prev + ms, total - left * mn)
            a = int(max(lo, c - search) * sr)
            b = int(min(hi, c + search) * sr)
            if hi - lo < 0.5 or b - a < win * 2:
                return None                # 这个 n 装不下，换个段数
            fr = np.abs(y16[a:b]).astype("float64")
            k = max(1, (len(fr) - win) // win)
            e = (fr[:k * win].reshape(k, win) ** 2).mean(axis=1)
            cuts.append(round((a + int(np.argmin(e)) * win) / float(sr), 3))
            prev = cuts[-1]
        return cuts

    # ★ 段数从「刚好够」起步，装不下就多切一段 —— 而不是退化成等分。
    #   为什么不能直接退化：约束把窗口挤成零宽时（素材周期性太强、总长恰好整除
    #   maxseg 时就会撞上），等分切点必然落在音符中间，接回去那一下是可听的接缝。
    #   多切一段会多起一个子进程（约 18 秒），但切点重新有了 ±3 秒的挑选余地。
    n0 = max(1, int(math.ceil(total / ms)))
    for n in range(n0, n0 + 4):
        r = try_n(n)
        if r is not None:
            return [c for c in r if 0.5 < c < total - 0.5]
    # 兜底：极端素材下（比如整条都是满能量、且时长卡得极死）退回纯等分，
    # 宁可接缝难看点，也不能不切（不切就会退化成几百秒一次 pipeline 调用）。
    return [round(i * total / n0, 3) for i in range(1, n0)]


def apply_chunk(vc, chunk_sec):
    """把 RVC 的切片长度调小 —— 这是「整首转换要十几分钟」的真正开关。

    ★ 实测证据（本机 RTX 5070 Laptop / 8G，从历史任务日志里挖的分项耗时）：
        30 秒以内的人声轨：特征 0.4s | F0 3s | **合成 1.7s**
        3 分钟整首（RVC 自己切成 60 秒一片）：
                            特征 70s | F0 8s | **合成 905s**
      时长 6 倍、合成时间 530 倍 —— 这不是线性慢，是**长序列上退化**
      （60 秒一片 ⇒ 一次前向要撑住 6000 帧 × 48k 输出的中间激活，8G 显存放不下）。

    ★ 为什么是改这几个数字、而不是自己切音频：
      Pipeline 的切片、切点选择（窗口内取能量最小的点）、前后 pad 重叠、
      丢弃边界、整体 change_rms —— 全是 RVC 自己的代码，我们不复制一份。
      只把「每片多长」调小，其余一条不动 ⇒ **f0 曲线、hubert 特征、索引检索、
      响度对齐全部与原来逐字节相同**，音色没有变化的来源；变的只是
      「一次前向喂多少帧」，而那正是退化的根因。

    返回实际采用的片长（秒）；0 表示这个 RVC 版本没有 Pipeline，无从调。
    """
    p = getattr(vc, "pipeline", None)
    if p is None:
        return 0.0
    sr = float(getattr(p, "sr", 16000) or 16000)
    if not chunk_sec or chunk_sec <= 0:
        return round(float(p.t_center) / sr, 2)
    # ★ 下限保护：片不能短到「前后 pad + 切点搜索窗口」塞不进去，
    #   否则 opt_ts 会算出越界的切点（`t - t_query` 变负 ⇒ 切片顺序乱掉）。
    pad_s = float(p.t_pad) / sr
    lo = max(6.0, 2.0 * pad_s + 4.0)
    sec = float(chunk_sec)
    if sec < lo:
        sec = lo
    p.t_center = int(sr * sec)
    p.t_max = int(sr * (sec + 5.0))
    # 查询窗口跟着收：片短了以后窗口比片还宽没有意义
    q_cap = int(sr * min(10.0, sec / 3.0))
    if p.t_query > q_cap:
        p.t_query = q_cap
    return round(sec, 2)


def _env_lag(src, dst, sr, cutoff):
    """低通到 cutoff 之后做互相关，返回 (滞后毫秒, 相关峰)。

    为什么是「低通后的波形」而不是包络或 f0 ——
      · 包络：分辨率受窗长限制，20 ms 窗 ⇒ 精度就是 ±20 ms（第一版就栽在这）；
      · f0：一步一帧 ≈ 10 ms，还是不够，而且清音段根本没 f0；
      · 低通波形：< cutoff 只剩基频和前几个谐波，而 RVC 转换**基频是不变的**
        （变的只是共振峰/音色，那些在中高频）。低通后两条轨高度同形，
        相关峰尖，配合抛物线插值能稳定到 0.05 ms 级，而且每一帧都有值。
    """
    # ★ numpy 是**延迟加载**的（模块顶层刻意不 import，见 main），这里必须自己引；
    #   漏了的话下面整段会抛 NameError，而外面套着 except ⇒ 静默返回 (0,0)，
    #   表现为「功能装了但从不生效」，且日志里没有任何痕迹。
    #   （2026-10-23 真的踩了一次，端到端日志缺 [align] 行才发现的。）
    import numpy as np
    try:
        from scipy.signal import butter, sosfiltfilt
        sos = butter(4, cutoff / (sr * 0.5), btype="low", output="sos")
        a = sosfiltfilt(sos, np.asarray(src, dtype="float64"))
        b = sosfiltfilt(sos, np.asarray(dst, dtype="float64"))
    except Exception as e:
        print("[align] 低通/互相关失败（%s）⇒ 不校正" % e)
        return 0.0, 0.0
    n = min(len(a), len(b))
    if n < int(sr * 0.5):
        return 0.0, 0.0
    a = a[:n]
    b = b[:n]
    na = float(np.sqrt((a * a).sum()))
    nb = float(np.sqrt((b * b).sum()))
    if na < 1e-9 or nb < 1e-9:
        return 0.0, 0.0
    a = a / na
    b = b / nb
    N = 1
    while N < 2 * n:
        N *= 2
    cc = np.fft.irfft(np.fft.rfft(a, N).conj() * np.fft.rfft(b, N), N)
    cc = np.concatenate([cc[-(n - 1):], cc[:n]])
    mid = len(cc) // 2
    kmax = int(0.05 * sr)                       # 只搜 ±50 ms，别让它跑到别的周期上
    seg = cc[max(0, mid - kmax):mid + kmax + 1]
    if len(seg) == 0:
        return 0.0, 0.0
    i = int(np.argmax(seg)) + max(0, mid - kmax)
    peak = float(seg.max())
    d = 0.0
    if 0 < i < len(cc) - 1:
        den = cc[i - 1] - 2 * cc[i] + cc[i + 1]
        d = float(np.clip(0.5 * (cc[i - 1] - cc[i + 1]) / den, -1.0, 1.0)) \
            if abs(den) > 1e-12 else 0.0
    return ((i - mid) + d) / sr * 1000.0, peak


_F0_MODEL = None        # RMVPE（闸门用），**只加载一次**
_F0_ERR = ""            # 加载失败的原因（报一次就够，别每段刷屏）


def _res16(v, vsr):
    """统一降到 16 kHz —— lag 是时间量，与采样率无关，16k 足够。"""
    from math import gcd
    # ★ numpy 在这个模块里是**刻意不顶层 import** 的（顶层只留标准库，
    #   重依赖都在 main() 或用到时再引），这里照着来，别去动模块顶层。
    import numpy as np
    from scipy.signal import resample_poly
    v = np.asarray(v, dtype="float64")
    if int(vsr) == 16000:
        return v
    g = gcd(16000, int(vsr))
    return resample_poly(v, 16000 // g, int(vsr) // g)


def _f0_tight(s16, y16, out, sr):
    """用 RMVPE 量「音准吻合率」（±50 音分口径，与 qc_cli 的 match_ratio 同口径）。

    返回 (不校正的分, 校正后的分, 失败原因)。失败时前两项是 None。

    ★ 为什么敢在推理进程里直接提 f0：RMVPE 这条链**本来就在用**
      （`from infer.rmvpe import RMVPE`），模型加载一次即可，实测 8 段总共 12 秒；
      而调 qc_cli 子进程是「一次一重载」，16 次要 2.5 分钟（每首歌 +60% 时间）。
    ★ 这把尺子有没有判别力不是我说了算：`tools/probe_f0pick.py` 逐段比过，
      7/7 段复现 qc_cli 的排序（±50 口径数值都一模一样）。
    """
    global _F0_MODEL, _F0_ERR
    import numpy as np
    if _F0_ERR:
        return None, None, _F0_ERR
    try:
        if _F0_MODEL is None:
            import os
            from infer.rmvpe import RMVPE
            import torch
            root = os.environ.get("rmvpe_root") or os.path.join(os.getcwd(), "assets", "rmvpe")
            ft = os.path.join(root, "rmvpe.pt")
            if not os.path.exists(ft):
                _F0_ERR = "找不到 RMVPE 权重 %s" % ft
                return None, None, _F0_ERR
            dev = "cuda" if torch.cuda.is_available() else "cpu"
            _F0_MODEL = RMVPE(ft, is_half=(dev != "cpu"), device=dev)
    except Exception as e:                                    # noqa: BLE001
        _F0_ERR = "%s: %s" % (type(e).__name__, e)
        return None, None, _F0_ERR

    def _f(v):
        # ★★ RVC 的 vc_single 返回的是 **int16 量级**的 PCM（实测 peak 24715、
        #   rms 3128），不是 [-1,1] 的浮点。直接喂给 RMVPE ⇒ 一条基频都提不出来
        #   （实测：源 862 帧有声、成品 **0 帧**有声）⇒ 闸门静默失效，
        #   日志上只留一句「量不出来」，看着像模型没装好。
        #   源那条轨是 soundfile 读出来的（本来就是 [-1,1]），所以**两边量级不一样**，
        #   必须各自判断，不能统一除。
        v = np.asarray(v, dtype="float32")
        if v.size and float(np.abs(v).max()) > 1.5:
            v = v / 32768.0
        return np.asarray(
            _F0_MODEL.infer_from_audio(v, thred=0.03),
            dtype="float64").reshape(-1)

    def _t(x, yy):
        m = int(min(x.size, yy.size))
        if m < 20:
            return None
        both = (x[:m] > 1.0) & (yy[:m] > 1.0)
        if int(both.sum()) < 20:
            return None
        c = 1200.0 * np.log2(yy[:m][both]) - 1200.0 * np.log2(x[:m][both])
        dev = c - float(np.median(c))
        return float(np.mean(np.abs(dev) < 50.0))
    try:
        fs = _f(s16)
        return _t(fs, _f(y16)), _t(fs, _f(_res16(out, sr))), ""
    except Exception as e:                                    # noqa: BLE001
        return None, None, "%s: %s" % (type(e).__name__, e)


def align_phase(src_path, y, sr, max_ms=40.0, tol_ms=4.0, f0_guard=True):
    """把成品挪回与源**同一条时间轴**上，返回 (新数组, 滞后毫秒)。

    ★ 为什么要这一步（2026-10-23 的实测）
    --------------------------------------
    RVC 的分片输出比应有长度多 `window/sr` = 160/16000 = **10 ms**，而
    `align_duration` 只把**总长**拉回，**不修片与片之间（以及成品与源之间）的
    相对偏移**。结果就是成品整体比源晚 10~20 ms。别小看这 10 ms：f0 是一帧
    10 ms 算的，错开一帧，音高变化快的地方逐帧偏差直接翻几倍：

        同一段 26 秒素材   lag 0 ms（整首处理）⇒ 吻合 96.0%  IQR  7.5 音分
                          lag 10~20 ms（分段）⇒ 吻合 89.2%  IQR 17.6 音分

    更麻烦的是它会让质检数字**抖**：输入长度差 0.01 秒（160 样本）就能让同一段
    内容从 93.6% 掉到 90.0%。上一轮长测里「第 20 段 30.050 秒掉到 86.3%、
    裁成 30.000 秒回到 93.2%」也是这一个东西 —— 当时以为是「末尾多了一点」，
    其实是**切片长度碰巧落在了不同的 10 ms 网格上**。

    实测校正收益（tools/probe_segalign.py）：吻合 89.2% → 96.5%，
    IQR 17.6 → 9.1 音分。

    ★ 怎么保证不会「校正错了反而更糟」
    ----------------------------------
    用两个不同的低通截止各算一次，只有两者**互相印证**（差 < tol_ms）且峰值
    相关不为 0 时才采纳；偏移量超过 max_ms 视为测飞了，也跳过。
    测不准就**原样返回**（宁可不修，也不许乱修）。

    ★★★ 三道闸，缺一不可（2026-10-23 补的第三道）
    ------------------------------------------
      ① 三频带互证（900/1200/1500 Hz，取峰最高的当基准，至少两条咬合）
      ② 闭环自验证（挪完再测一次，残余 > 阈值就回滚）
      ③ **真指标闸门**：用 RMVPE 直接量「音准吻合率」，挪完反而更差就回滚

    ①② 都量的是「有没有挪到我预测的位置」，**不是**「挪了之后质量涨没涨」。
    实测第 8 段：三频带一致读出 15.06 ms，闭环残余 +0.00 ms（①② 都说对），
    挪过去吻合度却从 94.3% 掉到 89.5% —— 只有③抓得住。
    """
    # ★ 这个模块顶层刻意不做重依赖导入（numpy / soundfile 都在 main 或用到时
    #   才引），所以这里两个都得自己补 —— 漏了会抛 NameError，而外面套着
    #   except ⇒ 静默跳过，表现为「功能装了但从不生效」。
    import numpy as np
    import soundfile as sf
    try:
        s, ssr = sf.read(src_path, always_2d=False)
    except Exception as e:
        print("[align] 读源失败（%s）⇒ 不校正" % e)
        return y, 0.0
    s = np.asarray(s, dtype="float64")
    if s.ndim > 1:
        s = s.mean(axis=1)
    m = min(int(len(s) / ssr * sr), len(y))
    if m < int(sr * 0.5):
        return y, 0.0
    yv = np.asarray(y[:m], dtype="float64")

    # ★ 两边统一降到 16k 再测：源（16k）和成品（48k）采样率常常不一样，而
    #   lag 是个**时间量**，与采样率无关；16k 对只看 <1.2 kHz 的低通互相关也
    #   绰绰有余。（第一版直接拿 16k 源对 48k 成品比，采样率不等 ⇒ 永远跳过，
    #   功能等于没做，是端到端日志里没有 [align] 行才发现的。）
    MSR = 16000

    def _to16(v, vsr):
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(MSR, int(vsr))
        return resample_poly(np.asarray(v, dtype="float64"), MSR // g, int(vsr) // g)

    try:
        s16 = s if int(ssr) == MSR else _to16(s, ssr)
    except Exception as e:
        print("[align] 重采样失败（%s）⇒ 不校正" % e)
        return y, 0.0

    def measure(yv):
        """测 yv 相对源的偏移。返回 (lag_ms, 可信等级, 各频带读数串)。

        可信等级：2 = 多个频带互相咬合；1 = 只有一个频带可用（**待闭环验证**）；
                  0 = 测不出来。
        """
        try:
            y16 = yv if int(sr) == MSR else _to16(yv, sr)
        except Exception:
            return 0.0, False, "重采样失败"
        m = min(len(s16), len(y16))
        if m < MSR // 2:
            return 0.0, False, "太短"
        # ★ 频带要卡在「基频 + 前几次谐波」这一带
        #   ① 太低不行：截止频率一贴近基频（这段素材 f0≈396 Hz），低通出来几乎
        #      是纯正弦，周期性极强，互相关假峰一抓一把（700 Hz 读出 15.6 ms，
        #      而 1000/1400 Hz 读 9.4/8.2 ms）。
        #   ② 太高也不行：2 kHz 以上是共振峰的地盘，而**RVC 改的恰恰就是共振峰**，
        #      两边不同形，测出来的「延迟」是音色差异不是时间错位 —— 实测
        #      2000/2800/3600 Hz 一致读出 18.5 ms，跟基频那带的 8~9 ms 差一倍，
        #      且不是整数倍（不是周期误配，是**频率相关相位失真**）。
        #      ⇒ 一把尺子修不好所有频段；质检看的是 f0，那就以 f0 为准。
        #   ③ 单频带不保险（会跳到隔壁峰）：测三个，取相关峰最高的当基准，
        #      并要求至少还有一个跟它咬合在 tol_ms 内。
        reads = []
        for c in (900.0, 1200.0, 1500.0):
            l, p = _env_lag(s16[:m], y16[:m], MSR, c)
            if p > 0.02:                   # 峰太低 = 两条轨根本不同形
                reads.append((l, p, c))
        detail = ", ".join("%.0fHz:%.1fms" % (c, l) for l, p, c in reads)
        if not reads:
            return 0.0, 0, detail or "无有效频带"

        # ★★ 投票前必须**先剔掉负读数**，而不是等排序之后在主流程里挡。
        #   物理上 RVC 只会让成品变晚（前后 pad + 每片多 10 ms），负 lag 一定是
        #   误配到隔壁周期的假峰。老实现让它参与排序，于是「1200/1500 Hz 都读
        #   11.0、900 Hz 读 −5.6」这种局面会因为 −5.6 的峰更高而当上基准 ⇒
        #   11.0 那两个反被判成"不一致" ⇒ 整段白扔。
        #   实测（Fight Song 第 4 段）就是这么丢掉 3.6 个点的。
        # ★ 门槛是 −0.5 而不是 +0.5：这个函数**也用来做闭环复核**，对齐成功时
        #   残余就是 −0.01 这种值。第一版按 +0.5 卡，等于把"完美对齐"判成
        #   "全负、测不出来" ⇒ 每一段都被回滚（门禁当场抓住：12 ms 的合成样本
        #   读出 0.00 ms）。留 0.5 ms 的余量给测量噪声。
        pos = [t for t in reads if t[0] >= -0.5]
        if not pos:
            # 全是负的：那是"已经比源早"的特征（挪过头了 / 全错峰），
            # 把最可信的那个负值原样报上去，让调用方去判（它会拒绝）。
            reads.sort(key=lambda t: -t[1])
            return float(reads[0][0]), 0, detail + "｜全负"
        pos.sort(key=lambda t: -t[1])      # 峰高的排前面
        best_l = pos[0][0]
        agree = [l for l, p, c in pos if abs(l - best_l) < tol_ms]
        if len(agree) >= 2:
            return float(np.median(agree)), 2, detail
        # ★ 只剩一个可用读数时**不要直接扔**：交给闭环验证去判。
        #   实测第 2 段（900Hz:18.6, 1200Hz:−1.9, 1500Hz:−1.9）：负值剔掉之后就
        #   只剩 18.6 一个。老实现到这里就 return False，白扔一整段（88.2%）。
        #   "挪完再测一次"本来就能识破假峰（真对齐残余必然 ≈ 0），
        #   那就让它去判，而不是靠"必须有两条读数一致"这种代理判据。
        return float(np.median([l for l, p, c in pos])), 1, detail + "｜单频带"

    lag, level, detail = measure(yv)
    if level == 0:
        print("[align] 测不出可信偏移（%s）⇒ 不校正" % detail)
        return y, 0.0
    # ★ 物理约束：RVC 只会让成品**变晚**（前后 pad + 每片多 10 ms），不可能提前。
    #   读出负值 ⇒ 一定是误配到基频隔壁的假峰上了。
    #   （实测：某段读出 −12.7 ms，照它挪过去吻合度从 96.7% 掉到 89.4%。）
    # ★ 上限从 20 ms 放到 35 ms：RVC 内部不分片时每段只多一个 window（10 ms），
    #   但外层分段后**段首段尾的 pad 会叠加**，实测有段的真实偏移到 25.5 ms
    #   （三个频带读数差 0.09 ms，可信度极高，却被老的 20 ms 上限挡掉）。
    #   放宽是安全的 —— 下面那道闭环会识破假峰，真错值藏不住。
    cap = min(float(max_ms), 35.0)
    if lag < 1.0 or lag > cap:
        print("[align] 偏移 %.2f ms 不在合理区间 [1, %.0f]（%s）⇒ 不校正"
              % (lag, cap, detail))
        return y, 0.0

    d = lag / 1000.0 * sr                  # 正=成品更晚 ⇒ 往前挪
    n = len(y)
    # ★ 索引上界必须是 **m（源与成品的重合长度）**，不能是 n（成品长度）：
    #   源比成品短一点点时 m < n，按 n 取值会 IndexError 崩掉整段
    #   （实测 60 秒素材第 2 段：index 960767 out of bounds for size 960767）。
    #   多出来的那几个尾巴样本取 yv 末值，和下面 tail 补齐是一个口径。
    top = max(0, m - 1)
    idx = np.arange(n, dtype="float64") + d
    lo = np.clip(np.floor(idx).astype("int64"), 0, top)
    hi = np.clip(lo + 1, 0, top)
    frac = idx - np.floor(idx)
    out = yv[lo] * (1.0 - frac) + yv[hi] * frac
    # 挪完末尾空出来的那一小截（≤ 20 ms）用末端值补齐：切点本来就选在换气处，
    # 这比循环延拓或插零都干净，也绝不会引入新的频率成分。
    tail = int(np.ceil(abs(d)))
    if 0 < tail < n:
        out[-tail:] = yv[-1]

    # ★★ 闭环自验证：挪过去之后**再测一次**，残余必须归零。
    #   这是最后一道保险。第一次测的时候完全可能误配到基频隔壁那个峰上，
    #   读出一个数值看起来很合理的错值（实测某段读出 15.04 ms，挪过去之后
    #   吻合度反而从 94% 掉到 89%）。挪完再测，误差就藏不住了 —— 真对齐了
    #   残余必然 ≈ 0，否则说明第一次测的是假峰，回滚。
    again, lv2, detail2 = measure(out)
    # ★ 闸门松紧跟着"第一次有多可信"走：多条频带咬合的（level 2）给 1.2 ms；
    #   只有一条读数的（level 1）收紧到 0.8 ms —— 单读数本来就更容易是假峰，
    #   换来的放宽必须用更严的复核去抵。
    need = 1.2 if level >= 2 else 0.8
    if lv2 == 0 or abs(again) > need:
        print("[align] 挪过去之后仍残余 %.2f ms（%s，阈值 %.1f）⇒ 回滚不校正"
              % (again, detail2, need))
        return y, 0.0

    # ★★★ 第三道闸：拿**真指标**判「这次挪到底涨没涨」。
    #   闭环那道闸量的是「有没有挪到我预测的那个位置」，**不是**「挪了之后
    #   音准变好没有」。这两件事在测错的时候会分道扬镳 —— 实测第 8 段：
    #   900/1200/1500 Hz 三个频带一致读出 15.06 ms、闭环残余 +0.00 ms，
    #   挪过去吻合度却从 94.3% 掉到 89.5%（tools/probe_segab.py 受控 A/B）。
    #   ⇒ 最后这一道必须直接量音准本身，而不是再找一个代理量。
    #   成本：RMVPE 在本进程里本来就 import 得到，模型只加载一次，实测 8 段
    #   总共 12 秒（换成调 qc_cli 子进程是 2.5 分钟，每首歌多花 60% 时间）。
    if f0_guard:
        t_raw, t_out, why = _f0_tight(s16, yv if int(sr) == MSR else _to16(yv, sr), out, sr)
        if t_raw is None or t_out is None:
            print("[align] f0 闸门量不出来（%s）⇒ 按闭环结论照挪" % why)
        elif t_out < t_raw:
            print("[align] ★ 挪过去音准反而更差（%.1f%% → %.1f%%）⇒ 回滚不校正"
                  % (t_raw * 100, t_out * 100))
            return y, 0.0
        else:
            print("[align] 音准 %.1f%% → %.1f%%（+%.1f 点）⇒ 采纳"
                  % (t_raw * 100, t_out * 100, (t_out - t_raw) * 100))
    return out.astype(np.asarray(y).dtype, copy=False), lag


def convert_one(vc, a, in_path, expect_sec, index_path):
    """对一段音频跑一次 vc_single，并把它的时长校正到 expect_sec。

    抽成函数是为了「外层分段」能复用同一条路径 —— 每段必须和整段走**完全相同**
    的处理（索引兜底、dtype 保持、时长校正、相位对齐），否则"分段 vs 不分段"
    的差异里就混进了实现差异，A/B 就没得比了。

    返回 (sr, audio, drift_ms)；失败返回 (0, None, 0.0)。
    """
    import numpy as np          # ★ 同 plan_cuts：np 在 main() 里才顶层 import

    status, result = vc.vc_single(
        a.speaker_id, in_path, a.pitch, a.f0_method,
        index_path if a.index_rate > 0 else "", a.index_rate,
        a.resample_sr, a.rms_mix_rate, a.protect)
    # ★ 兜底：索引被配错（维度不符）时 RVC 只会抛 faiss 的 assert，返回空结果，
    #   而 `_rvc` 那边按 "added index" 匹配降级 —— 这句话里没有它，于是整首
    #   歌直接判"转换失败"。这里自己救：有索引却失败，就去掉索引再试一次。
    #   少一点音色细节，总比整首唱不出来强。（2026-10-20 实测踩到）
    if (not result or result[0] is None or result[1] is None) and a.index_rate > 0:
        print("[warn] 带索引推理失败（多半是索引与模型维度不符：%s）"
              % (os.path.basename(index_path) if index_path else "-"))
        print("[warn] 去掉索引重试一次")
        a.index_rate = 0.0
        status, result = vc.vc_single(
            a.speaker_id, in_path, a.pitch, a.f0_method, "", 0.0,
            a.resample_sr, a.rms_mix_rate, a.protect)
    print(status)
    if not result or result[0] is None or result[1] is None:
        print("[err] 推理失败")
        return 0, None, 0.0

    sr, audio = result
    audio = np.asarray(audio).reshape(-1)
    # ★ 这里有个会毁掉整首歌的坑（实测踩到）：RVC 的 pipeline 返回的是 **int16**
    #   （±32767），不是 ±1 的浮点。如果顺手 `astype("float32")` 再交给 soundfile，
    #   soundfile 会把它当浮点数据写 PCM_16 ⇒ 99.4% 的采样被削平，输出变成刺耳的
    #   方波失真，而 RVC 自己不会报任何错。整合包自带的 infer.cli 之所以没这个问题，
    #   是因为它**原样**把数组交给 soundfile（int16 走 int16 的路）。
    #   ⇒ 整型保持整型，浮点才转 float32。
    if not np.issubdtype(audio.dtype, np.integer):
        audio = audio.astype("float32", copy=False)

    drift_ms = 0.0
    if expect_sec > 0 and sr > 0:
        audio, drift_ms = align_duration(audio, int(round(expect_sec * sr)), sr)
    # ★ 时长拉齐之后，还要把「整体晚了多少」挪回去（见 align_phase 的实测数据：
    #   这一步值 7 个百分点的吻合度）。先对齐长度再对齐相位，顺序不能反 ——
    #   长度不对时互相关会被拉伸搅浑，测出来的偏移不可信。
    if getattr(a, "align", True) and sr > 0:
        audio, lag_ms = align_phase(in_path, audio, sr,
                                    f0_guard=getattr(a, "f0_guard", True))
        if lag_ms:
            print("[align] 成品比源晚 %.2f ms ⇒ 已挪回（f0 是一帧 10 ms，错一帧"
                  "逐帧偏差就翻倍）" % lag_ms)
    return sr, audio, drift_ms


def eff_chunk(a):
    """RVC 内部实际该用的片长。

    ★ 外层分段开着时（maxseg>0），段长已经被 plan_cuts 卡在 ≤ maxseg 了，
      这里再把内部片长顶到 maxseg+5 ⇒ t_max = 片长+5 ≥ maxseg+10 > 段长，
      Pipeline 那个 `audio_pad.shape[0] > t_max` 的分支**永远进不去**，
      段内就只有一片。为什么非得是一片，见 plan_cuts 里的 A/B 数据：
      同一段素材内部分 2 片 vs 不分片，吻合 84.9% → 93.5%、偏差 IQR
      25.9 → 12.4 音分，而且不分片推理还更快（7.9s vs 8.7s）。
    """
    c = float(getattr(a, "chunk", 0) or 0)
    m = float(getattr(a, "maxseg", 0) or 0)
    return max(c, m + 5.0) if m > 0 else c


def child_cmd(a, index_path, in_path, out_path):
    """构造「跑单段」的子进程命令行。参数原样传，只把 --maxseg 钉成 0（子不该再切）。

    ★ 索引要用**父进程挑完的那个**（index_path），不能用 a.index：
      没传 --index 时索引是 pick_index 按归属挑出来的，a.index 是空字符串，
      照它传下去子进程会退化成「没有可用索引」，音色比父进程自己跑的差一档，
      而且差得很隐蔽（不报错，只是听起来淡）。

    ★ --chunk 要传**顶过之后的** eff_chunk(a)，不能原样传 a.chunk：
      子进程拿到的是 --maxseg 0，它自己不会再顶 ⇒ 若这里传原始 chunk（20），
      子进程就会在这段 ≤30 秒的音频里再切一刀（实测 29.6 秒 ⇒ 2 片），
      等于顶层那段优化被绕过，白改。（2026-10-23 端到端日志里抓到的）
    """
    # ★ 开关类参数（action="store_false" 那几个）**必须显式往下传**：
    #   它们没有默认值之外的字符串形式，漏传 ⇒ 子进程按默认 True 走，
    #   于是「父进程关掉了、子进程照做」——不报错，只是开关静默失效。
    #   （--no-align 就是这个毛病：分段模式下每段都是子进程跑的，
    #    实测父进程传 --no-align 根本没用。）
    cmd = [sys.executable, "-s", os.path.abspath(__file__),
           "--model", str(a.model), "--input", in_path, "--output", out_path,
           "--pitch", str(a.pitch), "--f0", str(a.f0_method),
           "--index-rate", str(a.index_rate), "--index", str(index_path or ""),
           "--rms-mix-rate", str(a.rms_mix_rate), "--protect", str(a.protect),
           "--speaker-id", str(a.speaker_id), "--resample-sr", str(a.resample_sr),
           "--chunk", str(eff_chunk(a)), "--maxseg", "0"]
    if not getattr(a, "align", True):
        cmd.append("--no-align")
    if not getattr(a, "f0_guard", True):
        cmd.append("--no-f0guard")
    return cmd


def convert_segmented(a, index_path):
    """把长音频切成若干段，**每段起一个独立子进程**去跑 convert_one。

    ★★ 为什么必须跨进程（本轮最硬的实测，2026-10-22 晚）
    --------------------------------------------------
    一开始我以为是「一次调用喂太长」导致退化，于是写成同进程分段。结果：

        [段 1/3]  0.0~ 30.9 秒   用了  7.4 秒
        [段 2/3] 30.9~ 60.9 秒   用了  4.0 秒
        [段 3/3] 60.9~ 90.0 秒   用了 119.5 秒      ← 同样 30 秒，慢了 30 倍

    而第 3 段的内容（同一素材 X）**单独丢给一个进程只要 8.5 秒**。
    ⇒ 决定快慢的不是「这一段有多长」，而是**这个进程之前已经干了多少活**
      （显存碎片 / 缓存分配器 / CUDA graph 重捕获之类的东西在那儿累积）。
    所以每切成一段就必须换一个干净进程，父进程自己**连模型都不许加载**。
    """
    import subprocess

    import librosa
    import numpy as np
    import soundfile as sf

    y, _ = librosa.load(a.input, sr=16000, mono=True)
    y = np.asarray(y, dtype="float32")
    total = len(y) / 16000.0
    cuts = plan_cuts(y, a.maxseg)
    bounds = [0.0] + cuts + [total]
    n = len(bounds) - 1
    print("外层分段（每段一个干净子进程）：%.1f 秒 ⇒ %d 段；切点 %s"
          % (total, n, "、".join("%.1fs" % c for c in cuts) or "（无，直接整段跑）"))
    if n <= 1:
        return None          # 切不动，交给调用者走单段路径

    # 临时目录 mkdtemp + atexit：段数是个位数，撞不到环境层的批量删除钩子。
    tmp = tempfile.mkdtemp(prefix="rvcseg")
    atexit.register(shutil.rmtree, tmp, ignore_errors=True)

    outs, sr_out = [], 0
    t_all_seg = time.time()
    for i in range(n):
        s, e = bounds[i], bounds[i + 1]
        seg = y[int(round(s * 16000)):int(round(e * 16000))]
        if len(seg) < 1600:                      # 短于 0.1 秒的碎片丢弃
            continue
        pin = os.path.join(tmp, "in%03d.wav" % i)
        pout = os.path.join(tmp, "out%03d.wav" % i)
        sf.write(pin, seg, 16000)

        # ★ stdout 必须**实时转发**，不能 subprocess.run 吞到最后一次性打印：
        #   外层每一段要跑将近 20 秒，期间父进程一句话都不说，Studio 那边看到的就是
        #   "日志停了二十秒" —— 跟卡死没区别（而且 _pct_from_line 靠子进程的行更新
        #   进度条，不转发的话进度条也一直不动）。
        t0 = time.time()
        tail = []
        proc = subprocess.Popen(child_cmd(a, index_path, pin, pout),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                bufsize=1, errors="replace",
                                # ★ 钉死工作目录：见上面 INIT_CWD 的注释。
                                #   孙进程要靠它找到 assets/rmvpe、assets/weights。
                                cwd=INIT_CWD)
        for ln in proc.stdout:
            ln = ln.rstrip("\r\n")
            if not ln:
                continue
            print(ln)
            tail.append(ln)
            if len(tail) > 20:
                tail.pop(0)
        proc.stdout.close()
        rc = proc.wait()
        dt = time.time() - t0
        # 给 Studio 的进度条一口吃的：_PROG_RE 认的是 `NN%`，
        #   按"已完成几段"折算成百分比；它内部本来就只前进不回退，不用担心倒着走。
        print("[段 %d/%d] %.1f~%.1f 秒  子进程用了 %.1f 秒  （整首已转 %d%%）"
              % (i + 1, n, s, e, dt, int((i + 1) * 100.0 / n)))
        sys.stdout.flush()
        if rc != 0 or not os.path.isfile(pout):
            print("[err] 第 %d/%d 段失败（退出码 %d，%.1f~%.1f 秒）"
                  % (i + 1, n, rc, s, e))
            for ln in tail[-8:]:
                print("        | %s" % ln)
            return 0, None
        ai, si = sf.read(pout, always_2d=False)
        if ai.ndim > 1:
            ai = ai.mean(axis=1)
        outs.append(np.asarray(ai))
        sr_out = si


    if not outs:
        return 0, None
    print("[t] 分段合计 %.1f 秒（%d 个子进程，含各自的模型加载）"
          % (time.time() - t_all_seg, len(outs)))
    return sr_out, np.concatenate(outs)


def finalize(a, sr, audio):
    """写盘前的最后一关：把成品时长对齐到源，再落盘。"""
    import numpy as np
    import soundfile as sf

    try:
        info_in = sf.info(a.input)
        n_in, sr_in = int(info_in.frames), int(info_in.samplerate)
    except Exception:
        n_in, sr_in = 0, 0
    if n_in > 0 and sr_in > 0 and sr > 0:
        want = int(round(n_in * float(sr) / float(sr_in)))
        audio, drift_ms = align_duration(audio, want, sr)
        if drift_ms:
            print("[t] 时长校正：成品比源%s %.1f ms（随位置累积 ⇒ 越到后面越抢拍），"
                  "已按比例修正 %.3f → %.3f 秒"
                  % ("短" if drift_ms < 0 else "长", abs(drift_ms),
                     len(audio) / float(sr) - drift_ms / 1000.0, len(audio) / float(sr)))
        else:
            print("[t] 时长对齐 OK（漂移 %.1f ms，在 2 ms 内，未改动）"
                  % ((len(audio) - want) / float(sr) * 1000.0))

    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    sf.write(a.output, audio, sr)
    print("[ok] %s  %.2f 秒 @ %d Hz" % (a.output, len(audio) / float(sr), sr))
    return 0


def main():
    a = build_parser().parse_args()
    if not os.path.isfile(a.input):
        print("[err] 输入不存在：%s" % a.input)
        return 2

    model_path = a.model
    if not os.path.isfile(model_path):
        cand = os.path.join(os.environ["weight_root"], a.model)
        if not os.path.isfile(cand):
            print("[err] 模型不存在：%s" % a.model)
            return 2
        model_path = cand
    # VC.get_vc() 只吃文件名（它在 weight_root 里找），所以把 root 指到模型所在目录
    os.environ["weight_root"] = os.path.dirname(os.path.abspath(model_path))
    model_name = os.path.basename(model_path)

    import numpy as np  # noqa: E402
    import soundfile as sf  # noqa: E402

    # ★ 加载 / 推理分开计时（2026-10-22 晚加，纯观测、不改行为）。
    #   为什么需要：一首 198 秒的歌整步估 298 秒，其中"进程启动 + 模型 + hubert +
    #   索引读"算 60 秒 —— 但这 60 秒从来没被单独量过。索引有 187 MB，RVC 加载时会
    #   `reconstruct_n(0, ntotal)` 全量重建进内存，代价随索引线性涨；不量出来就
    #   没法判断"该不该优化索引"。这行日志就是给下一次判断用的。
    t_all = time.time()

    # Config() 内部是 argparse，会吃 sys.argv —— 必须先把我们的参数摘掉，
    # 否则它会把 --model 当成自己的未知参数直接退出（cli.py 里也是这么护的）。
    from configs.config import Config
    from infer.vc.modules import VC

    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        config = Config()
    finally:
        sys.argv = original_argv

    print("设备 %s | 精度 %s" % (config.device, config.dtype))
    print("模型 %s | pitch %+d | f0 %s | index-rate %.2f | rms-mix %.2f | protect %.2f"
          % (model_name, a.pitch, a.f0_method, a.index_rate, a.rms_mix_rate, a.protect))

    # ★★ 要不要走「子进程分段」—— 判断必须放在**加载模型之前**：
    #    父进程一旦把模型/hubert/索引载进来，就占住一份显存不放；子进程能分到的
    #    变少，全都被拖慢（而且这份显存对父进程毫无用处：它只是编排者）。
    try:
        _dur = float(sf.info(a.input).duration)
    except Exception:
        _dur = 0.0
    # 索引归属挑选不需要加载模型，所以提到这里 —— 分段时要把它传给子进程。
    index_path = a.index
    if index_path and not os.path.isfile(index_path):
        print("[warn] 指定的索引不存在：%s" % index_path)
        index_path = ""
    if a.index_rate > 0 and not index_path:
        # 先按「归属」精确挑，挑不到才回落到 RVC 的原规则（兼容老布局）。
        index_path = pick_index(model_name, a.speaker_id)
        if index_path:
            print("索引归属匹配：%s" % os.path.basename(index_path))
        else:
            from infer.vc.utils import get_index_path_from_model
            index_path = get_index_path_from_model(model_name, a.speaker_id)
    if a.index_rate > 0 and index_path and not os.path.isfile(index_path):
        print("[warn] 索引不可读：%s" % index_path)
        index_path = ""
    if a.index_rate > 0 and not index_path:
        print("[warn] 没有可用的 added 索引，按 index-rate 0 推理")
        a.index_rate = 0.0
        index_path = ""
    print("索引 %s%s" % (os.path.basename(index_path) if index_path else "（未使用）",
                        ("（%.0f MB）" % (os.path.getsize(index_path) / 1048576.0))
                        if index_path and os.path.isfile(index_path) else ""))

    # ★★ 要不要走「子进程分段」—— 判断必须放在**加载模型之前**：
    #    父进程一旦把模型/hubert/索引载进来，就占住一份显存不放；子进程能分到的
    #    变少，全都被拖慢（而且这份显存对父进程毫无用处：它只是编排者，不跑推理）。
    #
    #    门槛取 maxseg 的 1.5 倍：刚刚过线一点点就切开是亏的 —— 比如体检那 30.02 秒
    #    的片段，切成两段的收益远不值多付那一次子进程加载（~9 秒 + 一次 hubert）。
    try:
        _dur = float(sf.info(a.input).duration)
    except Exception:
        _dur = 0.0
    if a.maxseg > 0 and _dur > a.maxseg * 1.5:
        sr, audio = convert_segmented(a, index_path)
        if audio is None:
            return 1
        return finalize(a, sr, audio)

    vc = VC(config)
    vc.get_vc(model_name)
    # ★ 外层分段开着的时候，把内部片长顶到「段长 + 5」，让 RVC **永远不进分片分支**
    #   （A/B 见 plan_cuts / eff_chunk：同一段素材内部不分片
    #     吻合 84.9%→93.5%、偏差 IQR 25.9→12.4 音分，而且推理还更快）。
    #   plan_cuts 已保证每段 ≤ maxseg，所以 t_max = 片长+5 ≥ maxseg+10 一定容得下。
    used_chunk = apply_chunk(vc, eff_chunk(a))
    if used_chunk:
        try:
            info = sf.info(a.input)
            dur = float(info.duration)
        except Exception:
            dur = 0.0
        print("分片 %.2f 秒/片，这段 %.1f 秒 ⇒ %d 片（RVC 自己切，切点取窗口内能量最小处）"
              % (used_chunk, dur, plan_slices(dur, used_chunk)))
    else:
        print("[warn] 这个 RVC 版本没有 Pipeline 对象，分片参数没生效（按它自带的 60 秒跑）")

    t_ready = time.time()
    sr, audio, _drift = convert_one(vc, a, a.input, _dur, index_path)
    if audio is None:
        return 1
    print("[t] 加载 %.1fs（含模型/hubert/索引读）| 推理 %.1fs | 合计 %.1fs"
          % (t_ready - t_all, time.time() - t_ready, time.time() - t_all))
    print("[dbg] dtype=%s shape=%s min=%.4f max=%.4f rms=%.4f"
          % (audio.dtype, audio.shape, float(audio.min()), float(audio.max()),
             float(np.sqrt((np.asarray(audio, dtype="float64") ** 2).mean()))))

    # ★★ 时长对齐（2026-10-22 加：这是「越唱越差 / 后面发飘发破」的根因）
    #   RVC 的输出会比输入短一点点（实测 204.080s → 204.000s，-80ms），而这点误差
    #   是**按比例**铺在整条时间轴上的 ⇒ 错位随位置线性累积，到结尾攒到 70ms。
    #   混音时人声相对伴奏越跑越前，听感就是"越唱越差"。它不动音色（八度错、谐波性
    #   全正常），所以常规质检看不出来，必须在写盘前按长度校正掉。
    #   缩放比例 ~1.0004 ⇒ 音高只变 0.7 音分（听不出来），代价可以忽略。
    try:
        info_in = sf.info(a.input)
        n_in, sr_in = int(info_in.frames), int(info_in.samplerate)
    except Exception:
        n_in, sr_in = 0, 0
    if n_in > 0 and sr_in > 0 and sr > 0:
        want = int(round(n_in * float(sr) / float(sr_in)))
        audio, drift_ms = align_duration(audio, want, sr)
        if drift_ms:
            print("[t] 时长校正：成品比源%s %.1f ms（随位置累积 ⇒ 越到后面越抢拍），"
                  "已按比例修正 %.3f → %.3f 秒"
                  % ("短" if drift_ms < 0 else "长", abs(drift_ms),
                     len(audio) / float(sr) - drift_ms / 1000.0, len(audio) / float(sr)))
        else:
            print("[t] 时长对齐 OK（漂移 %.1f ms，在 2 ms 内，未改动）"
                  % ((len(audio) - want) / float(sr) * 1000.0))

    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    sf.write(a.output, audio, sr)
    print("[ok] %s  %.2f 秒 @ %d Hz" % (a.output, len(audio) / float(sr), sr))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
