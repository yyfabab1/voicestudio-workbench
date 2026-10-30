# -*- coding: utf-8 -*-
r"""tools/probe_f0_range.py —— 验证「唱歌越唱越差」是不是**音域外推**造成的。

背景（老板的原话）："越唱越差，特别是破音后"。
上一轮 `probe_degrade.py` 已经把另一个假设否掉了：成品的谐波噪声比全程比源**高**
1.4 dB、零削波、没有劣化拐点 ⇒ **不是失真/撕裂 artifacts**。

本探针验证剩下的那个假设：
    歌往后走音变高（副歌），而这个角色的模型是**拿说话素材训的**（history：全库零唱歌
    素材）。目标音高一冲出模型见过的范围，就出现「抖动变大 / 旋律轮廓对不上 / 破音」。
    这个假设预言的形状非常具体：**质量必须随目标音高单调恶化**，而不是随时间恶化。
    —— 如果是随时间（比如分片累积误差），按音高分桶就不会有这么干净的单调关系。

怎么做（全是已经存在的口径，不自创）：
    1. 用 RVC 自带的 RMVPE（推理链同一个提取器）提 src/dst 逐帧 f0，thred=0.03，
       16k 单声道，和 stages/qc_cli.py 完全一致。
    2. 错位纠正在 lag=0（qc_cli 注释里解释过为什么必须钉死在 0）。
    3. 逐帧算 cents 偏差 = 1200*log2(dst / (src * 2**(pitch/12)))。
    4. **按 dst 的真实 Hz 分桶**，每桶报 帧数 / 中位绝对偏差 / IQR / 八度错。

顺带的免费午餐：换 pitch 档位会怎样，**是纯算术**（目标 Hz 乘 2^(Δ/12)），
不需要真跑显卡。所以下面会输出一张「如果改成 pitch=X，最高那几个音会掉到哪、
按现在的对应关系预期 IQR 是多少」的预测表 —— 用来决定要不要重唱、降到几。

用法（★ cwd 必须是 RVC 根目录，用 RVC 自带解释器；和 qc_cli.py 一样的原因）：
    cd /d/RVC/Retrieval-based-Voice-Conversion-WebUI
    runtime\python.exe -s D:\AHC\VoiceStudio\tools\probe_f0_range.py \
        <源vocal.wav> <成品voice.wav> <当前pitch> [输出.json]

退出码：0 成功；2 参数/环境错误；1 有效帧不足。
"""
import json
import os
import sys

SRC = sys.argv[1]
DST = sys.argv[2]
PITCH = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0
OUT = sys.argv[4] if len(sys.argv) > 4 else ""

os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")   # ★ 纯 CPU：不占老板的显卡
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import librosa  # noqa: E402
import numpy as np  # noqa: E402
import torch  # noqa: E402

from infer.rmvpe import RMVPE  # noqa: E402

DEVICE = "cpu"
FT = os.path.join(os.environ["rmvpe_root"], "rmvpe.pt")
if not os.path.exists(FT):
    print("找不到 RMVPE 权重：%s" % FT)
    sys.exit(2)


def load16(path):
    y, _ = librosa.load(path, sr=16000, mono=True)
    return np.asarray(y, dtype=np.float32)


MODEL = RMVPE(FT, is_half=False, device="cpu")

xs, xd = load16(SRC), load16(DST)
n = int(min(xs.size, xd.size))
if n < 16000:
    print("音频太短（%.2f 秒）" % (n / 16000.0))
    sys.exit(1)
xs, xd = xs[:n], xd[:n]

print("提 f0…（%.1f 秒，CPU）" % (n / 16000.0))
sys.stdout.flush()
f0s = np.asarray(MODEL.infer_from_audio(xs, thred=0.03), dtype=np.float64).reshape(-1)
f0d = np.asarray(MODEL.infer_from_audio(xd, thred=0.03), dtype=np.float64).reshape(-1)
m = int(min(f0s.size, f0d.size))
f0s, f0d = f0s[:m], f0d[:m]

hop_ms = (n / 16000.0) * 1000.0 / m
vs = f0s > 1.0
vd = f0d > 1.0
both = vs & vd
print("有效帧 %d / 双方都在唱 %d（hop %.2f ms）" % (m, int(both.sum()), hop_ms))
if int(both.sum()) < 200:
    print("有效帧太少，量不出东西")
    sys.exit(1)

# ---- 逐帧 cents 偏差（对的是"这个 pitch 下**应该**落在哪个音"）----
exp = f0s * (2.0 ** (PITCH / 12.0))
ratio = np.divide(f0d, np.where(exp > 1.0, exp, np.nan))
cents = 1200.0 * np.log2(np.where(ratio > 0, ratio, np.nan))
cents = np.nan_to_num(cents, nan=0.0, posinf=0.0, neginf=0.0)

act = np.abs(cents[both])
dst = f0d[both]

# ---- 按目标音高分桶 ----
EDGES = list(range(120, 700, 40))       # Hz
print("\n%-14s %7s %10s %10s %9s" % ("目标音高(Hz)", "帧数", "|偏差|中位", "IQR(cents)", "八度错"))
print("-" * 60)
rows = []
for i in range(len(EDGES) - 1):
    a, b = EDGES[i], EDGES[i + 1]
    sel = (dst >= a) & (dst < b)
    if int(sel.sum()) < 30:
        continue
    v = act[sel]
    q75, q25 = np.percentile(v, 75), np.percentile(v, 25)
    oct_err = float(np.mean(np.abs(cents[both][sel]) > 550))   # 偏 >5.5 半音 ≈ 认错八度
    rows.append({"lo": a, "hi": b, "n": int(sel.sum()),
                 "med": float(np.median(v)), "iqr": float(q75 - q25),
                 "oct": oct_err})
    print("%-14s %7d %10.1f %10.1f %8.1f%%" % ("%d-%d" % (a, b), int(sel.sum()),
                                               np.median(v), q75 - q25, oct_err * 100))

# ---- 单调性检验：把桶按音高排序，看 IQR 是不是一路往上 ----
if len(rows) >= 4:
    iqs = [r["iqr"] for r in rows]
    order = sorted(range(len(iqs)), key=lambda i: iqs[i])
    # Spearman 秩相关：目标音高分位 vs 抖动分位
    lo = sorted(range(len(rows)), key=lambda i: rows[i]["lo"])
    ra = list(range(len(rows)))
    rb = [0] * len(rows)
    for rank, idx in enumerate(sorted(range(len(rows)), key=lambda i: rows[i]["iqr"])):
        rb[idx] = rank
    da = np.asarray(ra, dtype=float)
    db = np.asarray(rb, dtype=float)
    rho = float(np.corrcoef(da, db)[0, 1])
    print("\n★ 音高 vs 抖动 的 Spearman 秩相关 rho = %+.3f" % rho)
    if rho > 0.7:
        print("  ⇒ **强单调**：目标音高越高，抖动越大。这是「音域外推不足」的典型形状，")
        print("     不是随时间劣化（如果随时间，按音高分桶不会这么整齐）。")
    elif rho < -0.7:
        print("  ⇒ 反向：音越高反而越稳 ⇒ 假设不成立，问题在别处。")
    else:
        print("  ⇒ 没有清晰单调关系 ⇒ 假设不成立，别照这个方向改。")

    hi = max(rows, key=lambda r: r["iqr"])
    lw = min(rows, key=lambda r: r["iqr"])
    print("  最稳的一档 %d-%d Hz：IQR %.1f cents（n=%d）" % (lw["lo"], lw["hi"], lw["iqr"], lw["n"]))
    print("  最飘的一档 %d-%d Hz：IQR %.1f cents（n=%d）" % (hi["lo"], hi["hi"], hi["iqr"], hi["n"]))

# ---- 按时间窗走一遍：回答「是不是越唱越差」（随时间劣化）----
# 为什么还要这一段：上面按音高分桶已经证明「越高越崩」**不成立**（rho 负）。
# 那就换唯一的另一个自变量 —— 时间。这一段的口径和 studio.qc_singing_whole 一致
# （30 秒窗、含头含尾铺满），好处是能跟服务端报出来的数直接对账。
print("\n" + "=" * 60)
print("沿时间走（30 秒窗，铺满整首；口径同 qc_singing_whole）")
print("=" * 60)
TSTEP = 10.0                       # 细一点，能定位到「第几秒开始崩」
WINSEC = 30.0
total_sec = n / 16000.0
t_rows = []
t0 = 0.0
while t0 + WINSEC <= total_sec + 1e-6:
    i0 = int(t0 * 1000.0 / hop_ms)
    i1 = int((t0 + WINSEC) * 1000.0 / hop_ms)
    seg_slice = slice(i0, min(i1, m))
    sel = both[seg_slice]
    if int(sel.sum()) >= 50:
        v = np.abs(cents[seg_slice][sel])
        q75, q25 = np.percentile(v, 75), np.percentile(v, 25)
        # 「破音帧」= 偏了 5.5 个半音以上（≈ 认错八度 / 撕裂）
        brk = int(np.sum(v > 550))
        # ★★ 素材难度：源这一段的旋律**本身**有多难（相邻帧的音高跳幅）。
        #   要分开两种完全不同的病因 ——
        #     · 源的旋律越到后面跳得越凶 ⇒ 是**歌变难了**，模型跟不上很正常；
        #     · 源难度不变、成品偏差却涨 ⇒ 才是**模型/管线越跑越坏**。
        #   不量这一列，这两件事在数据上长得一模一样，只能靠猜。
        sv = f0s[seg_slice][sel]
        sv = sv[sv > 1.0]
        if sv.size >= 20:
            sc = 1200.0 * np.log2(sv[1:] / np.maximum(sv[:-1], 1e-6))
            src_step = float(np.percentile(np.abs(sc), 75))
        else:
            src_step = None
        # 该窗 src/dst 的 f0 轮廓相关（Melodic corr，看跟着走了没有）
        ss = np.asarray(f0s[seg_slice], dtype=np.float64)
        dd = np.asarray(f0d[seg_slice], dtype=np.float64)
        cc = None
        if np.count_nonzero((ss > 1.0) & (dd > 1.0)) >= 50:
            a = np.where(ss > 1.0, 1200.0 * np.log2(ss / max(float(np.median(ss[ss > 1.0])), 1e-6)), 0.0)
            b = np.where(dd > 1.0, 1200.0 * np.log2(dd / max(float(np.median(dd[dd > 1.0])), 1e-6)), 0.0)
            if a.std() > 1e-6 and b.std() > 1e-6:
                cc = float(np.corrcoef(a, b)[0, 1])
        t_rows.append({"at": round(t0, 1), "n": int(sel.sum()),
                       "iqr": float(q75 - q25), "med": float(np.median(v)),
                       "oct": float(np.mean(v > 550)), "brk": brk,
                       "src_step": src_step, "corr": cc})
    t0 += TSTEP

print("%-7s %6s %9s %9s %8s %7s %10s %8s" % (
    "at(s)", "帧数", "|偏差|中位", "IQR(cents)", "破音帧%", "破音帧", "源跳幅Q3", "轮廓corr"))
print("-" * 74)
for r in t_rows:
    print("%-7s %6d %9.1f %9.1f %7.1f%% %7d %10s %8s"
          % (r["at"], r["n"], r["med"], r["iqr"], r["oct"] * 100, r["brk"],
             ("%.1f" % r["src_step"]) if r["src_step"] is not None else "-",
             ("%.3f" % r["corr"]) if r["corr"] is not None else "-"))

if len(t_rows) >= 4:
    iqs = [r["iqr"] for r in t_rows]
    ra = list(range(len(iqs)))
    rb = [0] * len(iqs)
    for rank, idx in enumerate(sorted(range(len(iqs)), key=lambda i: iqs[i])):
        rb[idx] = rank
    rho_t = float(np.corrcoef(np.asarray(ra, float), np.asarray(rb, float))[0, 1])
    print("\n★ 时间 vs 抖动 的 Spearman 秩相关 rho = %+.3f" % rho_t)
    print("  第 1 窗 %.1f cents → 最后一窗 %.1f cents（%.1f 倍）"
          % (iqs[0], iqs[-1], (iqs[-1] / iqs[0]) if iqs[0] > 0 else float("nan")))
    if rho_t > 0.7:
        print("  ⇒ **随时间单调劣化坐实**：这就是「越唱越差」。")
        # 定位拐点：第一个超过全曲中位数 1.5 倍的窗
        med_all = float(np.median(iqs))
        k = next((i for i, q in enumerate(iqs) if q > med_all * 1.5), None)
        if k is not None:
            print("  ⇒ 首次明显变飘：第 %.0f 秒（IQR %.1f，全曲中位 %.1f）"
                  % (t_rows[k]["at"], iqs[k], med_all))
    else:
        print("  ⇒ 没有清晰的随时间劣化 ⇒ 别按这个方向改。")

    # ★★ 定性：源的难度变了吗？这是决定"该怪谁"的那一列。
    steps = [r["src_step"] for r in t_rows if r.get("src_step") is not None]
    if len(steps) >= 6:
        A = np.mean(steps[:len(steps) // 2])
        B = np.mean(steps[len(steps) // 2:])
        grow_src = (B / A) if A > 1e-6 else float("nan")
        grow_iqr = (np.mean(iqs[len(iqs) // 2:]) / np.mean(iqs[:len(iqs) // 2])) if np.mean(iqs[:len(iqs) // 2]) > 1e-6 else float("nan")
        print("\n★ 归因（前半 vs 后半）：")
        print("    成品偏差 IQR     %.1f → %.1f（×%.2f）"
              % (np.mean(iqs[:len(iqs) // 2]), np.mean(iqs[len(iqs) // 2:]), grow_iqr))
        print("    源旋律跳幅 Q3    %.1f → %.1f（×%.2f）"
              % (A, B, grow_src))
        if grow_iqr > 1.5 and grow_src > 1.4 and (grow_src > grow_iqr * 0.6):
            print("    ⇒ 素材本身变难了（后半旋律跳幅涨 %.0f%%），成品跟着涨 %.0f%%"
                  % ((grow_src - 1) * 100, (grow_iqr - 1) * 100))
            print("      —— 这是**歌的难度曲线**，不是模型跑坏了。换参数收益有限。")
        elif grow_iqr > 1.5:
            print("    ⇒ 源难度基本没变，成品却明显变飘 ⇒ 真的是**逐个时间段在劣化**，")
            print("      要去查管线（分片 / 状态累积 / 响度），不是换音高档能解决的。")
        else:
            print("    ⇒ 前后差异不大。")

    # 「破音之后是否回不来」—— 用户原话「特别是破音后」
    brk_at = [i for i, r in enumerate(t_rows) if r["oct"] > 0.02]
    if brk_at:
        k = brk_at[0]
        print("\n★ 破音：首次出现在第 %.0f 秒（该窗 %.1f%% 的帧偏 >5.5 半音，%d 帧）"
              % (t_rows[k]["at"], t_rows[k]["oct"] * 100, t_rows[k]["brk"]))
        if k + 1 < len(t_rows):
            B = [r["iqr"] for r in t_rows[k + 1:]]
            A = [r["iqr"] for r in t_rows[:k + 1]]
            print("  破音前 %d 窗均值 IQR %.1f → 破音后 %d 窗均值 IQR %.1f（+%.1f）"
                  % (len(A), np.mean(A), len(B), np.mean(B), np.mean(B) - np.mean(A)))
    else:
        print("\n★ 没有窗的破音率超过 2% ⇒ 不存在集中的「破音」时段")

# ---- 换 pitch 档的预测（纯算术，不重跑显卡）----
print("\n" + "=" * 60)
print("如果换个 pitch 档会怎样（纯算术预测：**目标音高整体平移**，")
print("并按上面量到的「音高→抖动」对应关系去查表，得到各档预期 IQR）")
print("=" * 60)
if len(rows) >= 4:
    tbl = [(r["lo"], r["hi"], r["iqr"]) for r in rows]
    def iqr_at(hz):
        for a, b, q in tbl:
            if a <= hz < b:
                return q
        return None
    base = np.percentile(dst, [50, 90, 99])
    print("%-10s %10s %10s %10s %12s" % ("pitch", "目标中位", "目标P90", "目标P99", "预期IQR(P99)"))
    for p in [8, 5, 3, 0, -2, -5, -8, -12]:
        scale = 2.0 ** ((p - PITCH) / 12.0)
        p50, p90, p99 = base * scale
        q = iqr_at(float(p99))
        print("%-10s %10.1f %10.1f %10.1f %12s"
              % ("%+d" % p, p50, p90, p99, ("%.1f" % q) if q is not None else "-"))
    print("\n（当前 pitch %+d，目标 P99 = %.1f Hz）" % (int(PITCH), float(base[2])))

if OUT:
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"pitch": PITCH, "hop_ms": hop_ms, "frames": int(both.sum()),
                   "buckets": rows}, f, ensure_ascii=False, indent=1)
    print("\nJSON 已写：%s" % OUT)
print("DONE")
