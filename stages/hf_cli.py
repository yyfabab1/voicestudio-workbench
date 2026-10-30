# -*- coding: utf-8 -*-
r"""高频补偿的**自动档**：先量缺口 → 按缺口补 → 补完再量一次。

为什么要有它（2026-10-24 实测，推翻了旧的固定档位）
--------------------------------------------------
旧做法是给三个固定分频点（关 / 轻 5500 / 中 4500）让用户自己挑。实测三首
成品之后发现**一刀切是错的**，缺口根本不在一个量级上：

  角色        成品采样率   8-12k（关）   8-12k（原唱）   缺口
  大帝 40k    40000        -2.7dB       +13.8dB       16.5dB   ← 真的缺
  珍奇 48k    48000       +11.2dB       +18.7dB        7.5dB   ← 缺一些
  苹果嘉儿 48k 48000       +13.8dB       +13.8dB        0.0dB   ← 一点不缺

根因是**模型自己的采样率**：老模型（v1 / 40k）的声码器有效带宽只有 ~12k，
新模型（v2 / 48k）本来就把高频留住了。所以对 48k 模型做补偿是**纯亏** ——
高频一点没多，声纹反而被原唱的高频拉走（实测 fc5500 让"距角色"从 0.1855
涨到 0.2240，白付 11% 的漂移）。

而同一批实测还发现另一件事：**分频点越高，代价越小**。同样的满额补偿，
fc=5500 要付 11% 漂移，fc=7000 只付 0.8%（fc=8000 甚至是负的，比不补更像
角色）—— 因为声纹靠的是中低频共振峰，7k 以上根本不进那把尺子。
⇒ 所以正确做法不是"挑一个折中档"，而是**从最保守的 fc 开始试，够用就停**。

三个必须守住的约束
------------------
1. **不缺就不补**。这是自动档存在的全部理由（否则和固定档没区别）。
2. **先保守后激进**。候选 fc 从高到低，取第一个补够的 —— 拿最小的代价。
3. **补完必须再量一次**（闭环自验证）。只按预测补、不看结果，跟蒙着眼开车
   没区别：这一批素材里就有"测出来很合理、照着补反而更糟"的先例。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器 —— 只有它有 scipy/soundfile）：
    runtime\python.exe -s <本脚本> measure <成品.wav> <原唱人声.wav> <输出.json>
    runtime\python.exe -s <本脚本> auto <成品.wav> <原唱人声.wav> <输出.wav> <输出.json>

stdout 只打一行机器可读的结果（HF_AUTO / HF_SKIP / HF_FAIL），
调用方只认这一行 —— 别把过程噪声混进去。
"""
import io
import json
import os
import sys

import numpy as np
import soundfile as sf

_HERE = os.path.dirname(os.path.abspath(__file__))
_TOOLS = os.path.join(os.path.dirname(_HERE), "tools")
if _TOOLS not in sys.path:
    # ★ 复用 tools/probe_bandwidth.py 那把尺子，不自己再写一份：
    #   两把尺子量出来的数不能互证，等于没有尺子。
    sys.path.insert(0, _TOOLS)

import probe_bandwidth as bw
import seedvc_cli as sc


# ---- 阈值（每一个都是实测定的，改之前先看文件头那张表）----------------------
MIN_DB = 3.0       # 缺口小于这个就**不补**（48k 模型落在这里）
OK_DB = 2.0        # 补到缺口小于这个算达标
CENT_CAP = 1.03    # 补完的谱质心不许超过原唱的这个倍数（超了就整个不补）
CENT_STOP = 1.00   # 已经补到跟原唱一样亮 ⇒ 停手，别再往下试更激进的档
#   为什么要有两档：实测珍奇那首（221 秒、48k）分频点从 9000 一路降到 4500，
#   谱质心从 1.026 只爬到 1.027，缺口却只从 3.11dB 降到 2.54dB —— 多试五档、
#   多付过亮的风险，换 0.6dB。亮度一旦到顶就该停，再补就是刺耳不是清楚。
# 候选分频点：**从高到低** = 从保守到激进（fc 越高换掉的频段越少、代价越小）
CANDIDATES = (9000.0, 8000.0, 7000.0, 6000.0, 5000.0, 4500.0)
FB = 1200.0        # 过渡带宽度（跟 seedvc_cli 的默认值一致）


def _norm_peak(x):
    """峰值归一到 1.0。

    ★ 为什么必须归一：band_db 是**绝对能量和**，两条轨响度不一样时，
      量出来的差是"响度差"不是"频响差" —— 那会让它对每条安静的成品
      都报一个巨大的缺口，然后往死里补。
    """
    m = float(np.abs(x).max())
    return x / m if m > 1e-9 else x


def _one(path):
    y, sr = sf.read(path, dtype="float64")
    if y.ndim > 1:
        y = y.mean(1)
    y = _norm_peak(np.asarray(y, dtype=np.float64))
    # 只看前 60 秒：主体够了，长音频的尾部静音会把谱拉平
    fr, mag = bw.spec(y[: int(min(y.size, sr * 60))], sr)
    if fr is None:
        return None
    return {
        "sr": sr,
        "db8_12": bw.band_db(fr, mag, 8000.0, 12000.0),
        "db4_8": bw.band_db(fr, mag, 4000.0, 8000.0),
        "cent": bw.centroid(fr, mag),
        "fl8_12": bw.flatness(fr, mag, 8000.0, 12000.0),
        # ★ 判别"真泛音 vs 声码器噪声"要看 12-20k，不是 8-12k：
        #   实测原唱在 8-12k 本身就是平的（气声/齿音是宽带成分，平坦度 0.99），
        #   拿它当尺子会把"补对了"读成"补坏了"。12-20k 才是稀疏谐波区。
        "fl12_20": bw.flatness(fr, mag, 12000.0, 20000.0),
        "rolloff": bw.rolloff(fr, mag),
    }


def measure(dst, src):
    """量成品相对原唱的高频缺口。两条轨各自峰值归一后再比。"""
    a, b = _one(dst), _one(src)
    if not a or not b:
        return None
    return {
        "dst": a,
        "src": b,
        "deficit_db": round(b["db8_12"] - a["db8_12"], 2),
        "cent_ratio": round(a["cent"] / max(b["cent"], 1e-9), 3),
    }


class _NS(object):
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _cross(dst, src, out, fc):
    return sc.cmd_cross(_NS(a=dst, b=src, out=out, fc=float(fc), fb=FB))


def _dump(path, obj):
    with io.open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, indent=1))


def cmd_auto(dst, src, out_wav, out_json, min_db=None, ok_db=None,
             candidates=None):
    """自动档主流程。阈值都可以从命令行覆盖 —— 不是为了方便调参，
    是为了**让门禁能测真代码**：如果负对照只能靠"复制一份改坏再跑"，
    那测的是副本不是产品（本项目踩过：副本里的相对路径换了个目录就失效了）。
    """
    min_db = MIN_DB if min_db is None else float(min_db)
    ok_db = OK_DB if ok_db is None else float(ok_db)
    cands = CANDIDATES if candidates is None else tuple(float(c) for c in candidates)
    res = {"ok": False, "applied": False, "tried": []}
    m0 = measure(dst, src)
    res["before"] = m0
    if not m0:
        res["reason"] = "量不出有效频谱（有声部分太少？）"
        _dump(out_json, res)
        sys.stdout.write("HF_FAIL 量不出有效频谱\n")
        return 1
    res["deficit_db"] = m0["deficit_db"]
    res["cent_ratio"] = m0["cent_ratio"]

    # ---- 闸 1：不缺就不补 ----
    if m0["deficit_db"] < min_db:
        res["ok"] = True
        res["reason"] = ("成品 8-12k 与原唱只差 %.1f dB（阈值 %.1f），"
                         "这个模型本来就把高频留住了，补上去只是白付声纹代价"
                         % (m0["deficit_db"], min_db))
        _dump(out_json, res)
        sys.stdout.write("HF_SKIP deficit=%.1f cent=%.3f fl12_20=%.3f\n"
                         % (m0["deficit_db"], m0["cent_ratio"],
                            m0["dst"]["fl12_20"]))
        return 0

    # ---- 闸 2：从最保守的 fc 开始试，够用就停 ----
    chosen = None
    over = False
    for i, fc in enumerate(cands):
        # ★ 临时文件名的扩展名必须是 .wav：写成 out.wav.try0.wav 的话
        #   libsndfile 认不出格式（demetal_cli 上实测抛 LibsndfileError），
        #   而且一旦中途被打断就会在 final/ 里留下一堆 .tryN.wav 垃圾。
        _b, _e = os.path.splitext(out_wav)
        trial = "%s_try%d%s" % (_b, i, _e or ".wav")
        if _cross(dst, src, trial, fc) != 0 or not os.path.isfile(trial):
            res["tried"].append({"fc": fc, "err": "cross 失败"})
            continue
        m = measure(trial, src)
        if not m:
            with _quiet():
                os.remove(trial)
            res["tried"].append({"fc": fc, "err": "量不出来"})
            continue
        rec = {"fc": fc, "deficit_db": m["deficit_db"],
               "cent_ratio": m["cent_ratio"],
               "fl12_20": m["dst"]["fl12_20"], "db4_8": m["dst"]["db4_8"]}
        res["tried"].append(rec)
        over = m["cent_ratio"] > CENT_CAP
        if over:
            # 连最保守的一档都补过头 ⇒ 这一首不需要补（再往下试只会更过头）
            with _quiet():
                os.remove(trial)
            break
        if m["deficit_db"] <= ok_db or m["cent_ratio"] >= CENT_STOP:
            # 补够了，或者亮度已经到顶（再补是刺耳不是清楚）—— 都是收手的时候
            chosen = fc
            with _quiet():
                os.replace(trial, out_wav)
            break
        with _quiet():
            os.remove(trial)
    else:
        # 全试完都没补够，但也没过头 ⇒ 用最激进的一档（比不补强）
        if res["tried"] and not over:
            chosen = cands[-1]
            _cross(dst, src, out_wav, chosen)

    if chosen is None:
        res["ok"] = True
        res["reason"] = ("最保守的一档（%d Hz）已经让谱质心超过原唱 %.2f 倍，"
                         "再补只会变刺耳 —— 按不补处理"
                         % (int(cands[0]), CENT_CAP))
        _dump(out_json, res)
        c0 = res["tried"][0].get("cent_ratio") if res["tried"] else None
        sys.stdout.write("HF_SKIP over_bright cent=%s\n"
                         % ("%.3f" % c0 if c0 else "?"))
        return 0

    # ---- 闸 3：闭环 —— 对最终落盘的那个文件再量一次 ----
    after = measure(out_wav, src)
    res["ok"] = True
    res["applied"] = True
    res["fc"] = chosen
    res["after"] = after
    res["after_deficit_db"] = after["deficit_db"] if after else None
    res["gain_db"] = (round(m0["deficit_db"] - after["deficit_db"], 2)
                      if after else None)
    _dump(out_json, res)
    sys.stdout.write(
        "HF_AUTO applied=1 fc=%d deficit=%.1f->%.1f cent=%.3f->%.3f "
        "fl12_20=%.3f->%.3f rolloff=%.0f->%.0f db4_8=%.1f->%.1f\n"
        % (int(chosen), m0["deficit_db"],
           after["deficit_db"] if after else float("nan"),
           m0["cent_ratio"], after["cent_ratio"] if after else float("nan"),
           m0["dst"]["fl12_20"], after["dst"]["fl12_20"] if after else float("nan"),
           m0["dst"]["rolloff"], after["dst"]["rolloff"] if after else 0.0,
           m0["dst"]["db4_8"], after["dst"]["db4_8"] if after else 0.0))
    return 0


class _quiet(object):
    """删临时文件时别因为"文件正被占用"把整步带死。"""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


def _opts(argv):
    """挑出 --xxx 形式的覆盖项（只认这几个，别的当没看见）。"""
    d, i = {}, 0
    while i < len(argv):
        k = argv[i]
        if k in ("--min-db", "--ok-db") and i + 1 < len(argv):
            d[k[2:].replace("-", "_")] = float(argv[i + 1])
            i += 2
        elif k == "--candidates" and i + 1 < len(argv):
            d["candidates"] = [x for x in argv[i + 1].split(",") if x.strip()]
            i += 2
        else:
            i += 1
    return d


def main():
    a = sys.argv[1:]
    if len(a) >= 4 and a[0] == "measure":
        m = measure(a[1], a[2])
        _dump(a[3], {"ok": bool(m), "measure": m})
        if not m:
            sys.stdout.write("HF_FAIL 量不出有效频谱\n")
            return 1
        sys.stdout.write("HF_MEASURE deficit=%.1f cent=%.3f\n"
                         % (m["deficit_db"], m["cent_ratio"]))
        return 0
    if len(a) >= 5 and a[0] == "auto":
        return cmd_auto(a[1], a[2], a[3], a[4], **_opts(a[5:]))
    sys.stdout.write("usage: hf_cli.py measure <成品> <原唱> <out.json>\n"
                     "       hf_cli.py auto <成品> <原唱> <out.wav> <out.json>"
                     " [--min-db N] [--ok-db N] [--candidates 9000,8000,...]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
