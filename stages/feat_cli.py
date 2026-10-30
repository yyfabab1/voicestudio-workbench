r"""
声纹 + 歌声特征批量提取器。

一次性把一批切片的两类特征算出来，供上层做「谁是谁」和「这是说话还是唱歌」两个判断：

  1) 声纹 embedding：用整合包自带的 eres2net（就是 GPT-SoVITS 里做说话人验证的那个），
     同一段音频算一个向量，音色相近的人向量夹角小。
  2) 歌声特征：用 librosa.yin 抽 F0，再统计「有声比例 / 音高平稳度 / 最长持续音」
     这几个只有唱歌才会同时出现的特征。

不用 pyworld / torchcrepe，环境里没有，也不想为一个判别任务再装东西。
librosa.yin 是 Cython 实现，5 秒片段大约 0.1~0.4 秒，够快。

用法（cwd 必须是整合包根目录）：
    runtime\python.exe -s <本脚本> <包根目录> <切片目录> <输出json> [文件名 ...]

    ★ 第 4 个参数传 `-` 时，结果 JSON 写到 **stdout**（状态行改走 stderr，不污染）。
    ★ 后面可以跟任意个文件名（basename），只处理这几个。
      为什么要这么设计：调用方常常只想对"抽样出来的 60 段"算声纹中心。老写法是把
      这 60 个文件 copy 到临时目录、跑完再 rmtree，还要再写一个清单文件、再删掉 ——
      一次调用就是几十个删除动作，而本机环境层有批量删除钩子（阈值 50/轮）。
      现在"名字走 argv、结果走 stdout" ⇒ **零拷贝、零临时文件、零删除**。

输出 JSON：
    [{"file": "xxx.wav", "dur": 4.8, "emb": [...], "sv": [...], "voiced": 0.9, ...}, ...]

★ 一份记录里存**两份**向量，别混：
    emb —— `compute_embedding3`（走 forward3）= 时间平均后的融合特征，20480 维。
           GPT-SoVITS 拿它做别的用途；`stages/diarize.py` 用它做声音分离。
    sv  —— 模型自带的说话人嵌入头（走 forward）= seg_1(stats)，192 维。
           这一层才是为"是不是同一个人"训练的。认人用这一份。
"""
import json
import os
import sys

import numpy as np

if len(sys.argv) < 4:
    print("参数不足。需要: 包根目录 切片目录 输出json")
    sys.exit(2)

PKG_ROOT = os.path.abspath(sys.argv[1])
SLICE_DIR = os.path.abspath(sys.argv[2])
# "-" = 结果写到 stdout（状态行走 stderr）。见文件头注释。
OUT_JSON = sys.argv[3] if sys.argv[3] == "-" else os.path.abspath(sys.argv[3])
# 第 4 个参数起是"只处理这几个文件"（basename）。空 = 全目录。
ONLY = [os.path.basename(a) for a in sys.argv[4:] if a.strip()]


def say(*a):
    """状态/进度一律走 **stderr**：stdout 要留给 JSON（OUT_JSON == "-" 的用法）。

    studio.run() 里子进程的 stderr 是合并进 stdout 的，所以任务日志照样看得到这些行。
    """
    print(*a, file=sys.stderr)

os.chdir(PKG_ROOT)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)
sys.path.append(os.path.join(os.getcwd(), "GPT_SoVITS", "eres2net"))
import kaldi as Kaldi   # noqa: E402  （eres2net 的 fbank 前端，sv.py 也是这么拿的）

import torch
import torchaudio

SR = 16000


def load16k(path):
    wav, sr = torchaudio.load(path)
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != SR:
        wav = torchaudio.functional.resample(wav, sr, SR)
    return wav.squeeze(0)


def f0_features(w16k):
    """
    抽 F0 并统计唱歌相关的几个量。

    voiced      有声帧占比。唱歌几乎全程带调，说话有很多辅音和停顿。
    f0_std      相邻帧音高变化（半音）的标准差。唱歌连贯，说话抖。
    longest_run 音高基本不变（±0.6 半音）的最长连续时长。唱歌会拖长音。
    f0_range    音高跨度（半音）。唱歌通常跨得更开。
    """
    import librosa

    y = w16k.cpu().numpy().astype(np.float64)
    if y.size < SR * 0.3:
        return dict(voiced=0.0, f0_std=0.0, longest_run=0.0, f0_range=0.0, f0_med=0.0)

    try:
        f0 = librosa.yin(
            y,
            fmin=float(librosa.note_to_hz("C2")),
            fmax=float(librosa.note_to_hz("C7")),
            sr=SR,
            frame_length=1024,
            hop_length=160,
        )
    except Exception:
        return dict(voiced=0.0, f0_std=0.0, longest_run=0.0, f0_range=0.0, f0_med=0.0)

    # yin 会把没检测到基频的地方填成 fmax，先按能量粗筛一遍
    valid = (f0 > 40) & (f0 < 900)
    # 中值附近的离群点剔除：yin 偶尔会跳一个八度
    if valid.sum() >= 5:
        med = np.median(f0[valid])
        valid &= (f0 > med / 2.2) & (f0 < med * 2.2)
    voiced = float(valid.mean())
    if valid.sum() < 5:
        return dict(voiced=voiced, f0_std=0.0, longest_run=0.0, f0_range=0.0, f0_med=0.0)

    f0v = f0[valid]
    semi = 12.0 * np.log2(np.maximum(f0v, 1e-6))
    d = np.diff(semi)
    # 只统计小跳变，大跳变是切分边界不是抖动
    d_small = np.abs(d[d < 12])
    f0_std = float(np.std(d_small)) if d_small.size else 0.0
    f0_range = float(np.percentile(semi, 95) - np.percentile(semi, 5))
    f0_med = float(np.median(f0v))

    # 最长持续音必须在「完整时间轴」上算。
    # 第一版踩了坑：semi 是从 valid 帧里抽出来的，静音帧被删掉了，
    # 于是游程会跨过停顿继续连下去，唱歌和说话都被算成 0.12 秒，完全没区分度。
    # 现在按帧走完整时间轴，遇到静音帧就断。
    hop_s = 160.0 / SR
    semi_full = np.where(valid, 12.0 * np.log2(np.maximum(f0, 1e-6)), np.nan)
    best = 0
    cur = 0
    ref = np.nan
    for i in range(len(semi_full)):
        v = semi_full[i]
        if np.isnan(v):
            best = max(best, cur)
            cur = 0
            ref = np.nan
            continue
        if np.isnan(ref):
            ref = v
            cur = 1
            continue
        if abs(v - ref) <= 0.6:
            cur += 1
        else:
            best = max(best, cur)
            ref = v
            cur = 1
    best = max(best, cur)
    return dict(
        voiced=voiced,
        f0_std=f0_std,
        longest_run=float(best * hop_s),
        f0_range=f0_range,
        f0_med=f0_med,
    )


def main():
    from GPT_SoVITS.sv import SV

    device = "cuda" if torch.cuda.is_available() else "cpu"
    sv = SV(device, True)

    files = sorted(
        f for f in os.listdir(SLICE_DIR) if f.lower().endswith((".wav", ".flac", ".mp3"))
    )
    if ONLY:
        want = set(ONLY)
        before = len(files)
        files = [f for f in files if f in want]
        say("[only] 按名字筛：%d -> %d 个文件" % (before, len(files)))
    out = []
    wavs = []
    feats = []
    # 先读全部，再一次性过 GPU：一段一段送的话，kernel 启动开销比算的时间还多
    for f in files:
        p = os.path.join(SLICE_DIR, f)
        try:
            w = load16k(p)
        except Exception as e:
            print("[skip] %s 读不了: %s" % (f, e))
            continue
        if w.numel() < SR * 0.3:
            print("[skip] %s 太短" % f)
            continue
        wavs.append((f, w, float(w.numel()) / SR))
        feats.append(f0_features(w))

    B = 8
    embs = []
    # ★ sv_flat 必须**与 wavs 一一对应**（每个元素是 192 维向量，或 None）。
    #   别"按批"存：svs 按批 append（每项是 (批,192) 的二维数组），而下面的输出循环
    #   按**文件**下标取 —— 两个错一叠就同时出现「错位」和「把二维数组喂 float()」，
    #   抛 TypeError: only length-1 arrays can be converted to Python scalars，
    #   而且崩在写 JSON 之前 ⇒ 整个输出文件都不生成，看着像"没跑过"。
    sv_flat = []
    for i in range(0, len(wavs), B):
        chunk = [w for _, w, _ in wavs[i : i + B]]
        # 长短不齐时补零；eres2net 吃的是 fbank，尾部零帧影响很小
        maxlen = max(c.numel() for c in chunk)
        padded = torch.stack([torch.nn.functional.pad(c, (0, maxlen - c.numel())) for c in chunk])
        with torch.no_grad():
            e = sv.compute_embedding3(padded.cuda())
            # ★ 同一批特征**再走一次模型自带的说话人嵌入头**（pool + seg_2），存成 `sv`。
            #   为什么要多算这一份：`compute_embedding3` 走的是 forward3，它是
            #   "时间平均后的融合特征"（20480 维），GPT-SoVITS 拿它做别的用途，
            #   **判别力差得多** —— 实测拿它做"认人"时同人/异人两侧分布几乎贴在一起
            #   （同人 p90 0.206 / 异人 p10 0.227，只差 2%）。
            #   模型里本来就有一个训练好的嵌入头（forward / seg_2），那个才是给
            #   "是不是同一个人"训练的。
            #   `emb` 字段**原样不动**：diarize 用它，改了会动到已经验证过的"声音分离"。
            try:
                # ★ fbank 用 float32 算完再转半精度送进模型。
                #   直接把 half 张量喂 Kaldi.fbank 会炸 "Unsupported dtype Half"
                #   （踩过：整段退回 forward3，sv 字段全空，看着像"模型没有嵌入头"）。
                _f = torch.stack([
                    Kaldi.fbank(w0.unsqueeze(0), num_mel_bins=80,
                                sample_frequency=16000, dither=0)
                    for w0 in padded])
                if sv.is_half:
                    _f = _f.half()
                s2 = sv.embedding_model.forward(_f.cuda())
            except Exception as _ex:
                print("[warn] 说话人嵌入头算不出，认人只能退回 forward3：%s" % _ex)
                s2 = None
        embs.append(e.float().cpu().numpy())
        # 拍平成"每条切片一行"再按文件顺序填。s2 是 (批,192)（two_emb_layer=False
        # 时 seg_2 是 Identity，forward 直接返回 seg_1 那层 192 维说话人嵌入）。
        # 万一某批算不出（s2 is None），也必须按批长补同样多个 None，
        # 否则后面的切片会整体前移一位、张冠李戴。
        if s2 is not None:
            for row in s2.float().cpu().numpy().reshape(len(chunk), -1):
                sv_flat.append(row)
        else:
            sv_flat.extend([None] * len(chunk))
    E = np.concatenate(embs, axis=0) if embs else np.zeros((0, 1))

    sv_dim = len(sv_flat[0]) if sv_flat and sv_flat[0] is not None else 0
    for i, ((f, w, dur), ft, e) in enumerate(zip(wavs, feats, E)):
        rec = dict(file=f, dur=round(dur, 3))
        rec.update({k: round(float(v), 4) for k, v in ft.items()})
        rec["emb"] = [round(float(x), 5) for x in e]
        if i < len(sv_flat) and sv_flat[i] is not None:
            rec["sv"] = [round(float(x), 5) for x in sv_flat[i]]
        out.append(rec)

    if OUT_JSON == "-":
        json.dump(out, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        with open(OUT_JSON, "w", encoding="utf-8") as fp:
            json.dump(out, fp, ensure_ascii=False)
    n_sv = sum(1 for r in out if r.get("sv"))
    say("FEAT_DONE %d  emb=%d维  sv=%d项/%s维" % (
        len(out), len(out[0]["emb"]) if out else 0, n_sv, sv_dim or "-"))
    # 顺便把歌声特征的中位数打出来，标阈值时要用
    for k in ("voiced", "f0_std", "longest_run", "f0_range"):
        vals = [r[k] for r in out]
        if vals:
            say("STAT %s p10=%.3f p50=%.3f p90=%.3f" % (k, np.percentile(vals, 10), np.percentile(vals, 50), np.percentile(vals, 90)))


if __name__ == "__main__":
    main()
