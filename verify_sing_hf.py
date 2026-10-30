# -*- coding: utf-8 -*-
r"""门禁：高频补偿不许「一刀切」。

================================================================================
这一关在防什么
================================================================================

老做法是给三个固定分频点（关 / 轻 5500 / 中 4500）让用户自己挑。
2026-10-24 在三首真歌上重测后，**固定档位是错的**，而且两种错法方向相反：

  角色         成品采样率  8-12k 缺口   说明
  大帝 40k      40000     14.3dB      v1 老模型，声码器有效带宽只有 ~12k
  珍奇 48k      48000      8.7dB      v2
  苹果嘉儿 48k   48000     -1.8dB      v2，本来就把高频留住了

缺口是**模型自己的采样率**决定的，不是"补多补少"的口味问题。于是：
  · 对 40k 老模型：固定「轻」档补得到，但**白付 11% 的声纹漂移**
    （距角色 0.1855 → 0.2240）—— 因为 5500 把本来不缺的 4-8k 也换了。
  · 对 48k 新模型：高频一点没多，只付代价 —— **纯亏**。

同时实测出另一件事：分频点越高代价越小（同样补满 8-12k）：
  fc=9000 → 距角色 0.1747（比不补的 0.1855 **更像角色**，4-8k 一动不动）
  fc=5500 → 距角色 0.2240（4-8k 从 14.7dB 被改成 13.2dB）
因为声纹靠中低频共振峰，7k 以上根本不进那把尺子。

⇒ 正确做法不是"挑一个折中档"，而是**先量、不缺就不补、缺就从最保守的
  分频点往下试、够用就停、补完再量一次**。这一关守的就是这五步。

================================================================================
判据的真在哪
================================================================================

A/B 段是源码对账（快，只证明"东西都在"）；
**C 段真跑** stages/hf_cli.py —— 造一对"确实缺"和一对"一点不缺"的素材，
断言前者补了、后者没补；
**D 段是尺子自检**：把阈值旋到顶、把候选顺序反过来、把达标线设成不可能，
判定的结果**必须跟着变**。不变就说明那条判据根本没在判，只是恰好没出错
（★ 挂了"负对照"标签却不会变红的尺子是假尺子，比没有更坏 —— 它会误导后来人）。

用法（venv 或托管 python 都行；hf_cli 走 RVC 的子进程）：
    <python> -s verify_sing_hf.py
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

WORK = os.path.join(HERE, "_hfprobe")

import studio  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + detail) if detail else ""), flush=True)


def sec(t):
    print("\n=== %s ===" % t, flush=True)


def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def _write_wav(path, y, sr):
    """纯标准库写 16bit 单声道 wav（venv 里不一定有 soundfile）。"""
    import struct
    import wave
    y = y / max(1e-9, float(abs(y).max())) * 0.8
    pcm = (y * 32767.0).astype("int64")
    pcm = pcm.clip(-32768, 32767)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(b"".join(struct.pack("<h", int(v)) for v in pcm))


def make_pair(work):
    """造两对素材：一对确实缺高频，一对一点不缺。

    ★ 为什么自己合成而不用真歌：`data/cache/sep` 是**运行时缓存**，会被清，
      门禁依赖它就是"今天绿明天红"（本项目栽过）。
    """
    import numpy as np
    sr = 44100
    t = np.linspace(0.0, 8.0, int(sr * 8.0), endpoint=False)
    rng = np.random.RandomState(20261024)
    # "原唱"：基频 220Hz 的谐波堆（到 12k）+ 一点宽带气声
    y = np.zeros_like(t)
    for k in range(1, 55):
        y += (1.0 / k) * np.sin(2 * np.pi * 220.0 * k * t + rng.uniform(0, 6.28))
    y += 0.15 * rng.randn(t.size)
    src = os.path.join(work, "src.wav")
    _write_wav(src, y, sr)
    # "成品"：7k 以上平滑滚降（模拟老模型声码器的有效带宽，实测大帝 8-12k 缺
    # 14dB）。★ 别用硬置零 —— 那会把 5-9k 也变成零，逼得最保守的那一档补不回来，
    # 于是"先试最省代价的"这条规则在测试素材上根本走不到，判据就成了摆设。
    Y = np.fft.rfft(y)
    fr = np.fft.rfftfreq(y.size, 1.0 / sr)
    Y = Y / np.sqrt(1.0 + (fr / 7000.0) ** 8)
    dst = os.path.join(work, "dst.wav")
    _write_wav(dst, np.fft.irfft(Y, y.size), sr)
    return src, dst


def run_cli(script, args):
    cmd = [studio.RVC_PY, "-s", script] + [str(a) for a in args]
    p = subprocess.run(cmd, cwd=studio.RVC_ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    out = (p.stdout or b"").decode("utf-8", "replace")
    return p.returncode, out


def parse(line):
    """解析 HF_AUTO / HF_SKIP 那一行的 key=value。"""
    d = {}
    for m in re.finditer(r"(\w+)=(-?[\d.]+|nan)", line or ""):
        d[m.group(1)] = float(m.group(2))
    d["_raw"] = (line or "").strip()
    d["_kind"] = (line or "").split()[0] if line else ""
    return d


def main():
    os.makedirs(WORK, exist_ok=True)
    print("高频补偿门禁 | RVC_PY=%s" % studio.RVC_PY, flush=True)

    st = read(os.path.join(HERE, "studio.py"))
    sv = read(os.path.join(HERE, "server.py"))
    hf = os.path.join(HERE, "stages", "hf_cli.py")
    hfs = read(hf) if os.path.isfile(hf) else ""
    html = read(os.path.join(HERE, "web", "index.html"))
    appjs = read(os.path.join(HERE, "web", "app.js"))

    # ----------------------------- A. 契约 -----------------------------
    sec("A. 契约：auto 档存在，而且是默认档")
    check("HF_LEVELS 里有 auto", "auto" in studio.HF_LEVELS,
          str(studio.HF_LEVELS))
    check("auto 排在第一位（它就是默认档）",
          studio.HF_LEVELS and studio.HF_LEVELS[0] == "auto",
          "第一位是 %r" % (studio.HF_LEVELS[0] if studio.HF_LEVELS else None))
    check("HF_FREQ 里有 auto 键", "auto" in studio.HF_FREQ, str(list(studio.HF_FREQ)))
    check("HF_LABEL 里有 auto（界面要显示人话）", "auto" in studio.HF_LABEL)
    check("固定档仍在（off/light/mid 没被删 —— 简化不许等于删功能）",
          all(k in studio.HF_LEVELS for k in ("off", "light", "mid")),
          str(studio.HF_LEVELS))
    n_def = st.count('hf="auto"')
    check("studio 里高频补偿的默认值是 auto（≥4 处入口）", n_def >= 4, "%d 处" % n_def)
    check("server 两处入口的兜底值是 auto",
          sv.count('studio.HF_LEVELS else "auto"') >= 2,
          "%d 处" % sv.count('studio.HF_LEVELS else "auto"'))
    check("界面下拉有 auto 且默认选中",
          re.search(r'<option value="auto"[^>]*selected', html) is not None)
    check("界面仍保留关/轻/中三档", all(
        re.search(r'<option value="%s"' % k, html) for k in ("off", "light", "mid")))
    check("app.js 的兜底默认值是 auto",
          appjs.count('|| "auto"') >= 2 and 'hf: "auto"' in appjs,
          '|| "auto" %d 处' % appjs.count('|| "auto"'))

    # ----------------------------- B. 实现 -----------------------------
    sec("B. 实现：先量 → 按缺口补 → 补完再量")
    check("stages/hf_cli.py 存在", os.path.isfile(hf))
    check("有 measure 子命令", "measure" in hfs and '"measure"' in hfs)
    check("有 auto 子命令", 'a[0] == "auto"' in hfs)
    m = re.search(r"^MIN_DB\s*=\s*([\d.]+)", hfs, re.M)
    check("有不补的阈值 MIN_DB（不缺就不补这条闸）", m is not None,
          m.group(0) if m else "没找到")
    check("MIN_DB 是个合理的小缺口（0~6 dB）",
          m is not None and 0 < float(m.group(1)) <= 6.0,
          m.group(1) if m else "?")
    m2 = re.search(r"^OK_DB\s*=\s*([\d.]+)", hfs, re.M)
    check("有达标线 OK_DB", m2 is not None, m2.group(0) if m2 else "没找到")
    m3 = re.search(r"^CANDIDATES\s*=\s*\(([^)]*)\)", hfs, re.M)
    cands = [float(x) for x in (m3.group(1).replace(",", " ").split() if m3 else [])]
    check("候选分频点数量 > 1（不是写死一档）", len(cands) > 1, str(cands))
    check("★ 候选是**从高到低**（先试最保守的，够用就停）",
          len(cands) > 1 and all(cands[i] > cands[i + 1] for i in range(len(cands) - 1)),
          str(cands))
    check("最保守的一档 ≥ 8000Hz（别一上来就把中频换了）",
          bool(cands) and cands[0] >= 8000.0, str(cands[0]) if cands else "?")
    check("★ 补完会再量一次（闭环自验证，不是按预测补完就走）",
          "measure(out_wav, src)" in hfs)
    check("★ 两条轨走**同一个**测量函数（两把尺子量出来的数不能互证）",
          re.search(r"a, b = _one\(dst\), _one\(src\)", hfs) is not None)
    check("测量函数内部做了峰值归一（否则比的是响度不是频响）",
          re.search(r"def _one\(.*?_norm_peak", hfs, re.S) is not None)
    check("★ 没补时**不拿空文件当成品**（SKIP 不写 wav，调用方必须守）",
          "os.path.isfile(out_wav)" in read(os.path.join(HERE, "studio.py")))
    check("studio 走了 auto 分支", "if level == \"auto\"" in st
          or "if level == 'auto'" in st)
    check("日志里不再写死漂移常数（26%/42% 是过期口径）",
          '"26%"' not in st and '"42%"' not in st)

    # ----------------------------- C. 真跑 -----------------------------
    sec("C. 真跑：不缺就不补，缺就补到够")
    src, dst = make_pair(WORK)
    hf_dir = os.path.join(WORK, "bin")
    os.makedirs(hf_dir, exist_ok=True)

    # 情形 1：成品 = 原唱本身 ⇒ 一点不缺
    o1 = os.path.join(hf_dir, "same.wav")
    j1 = os.path.join(hf_dir, "same.json")
    rc1, out1 = run_cli(hf, ["auto", src, src, o1, j1])
    l1 = [x for x in out1.splitlines() if x.startswith("HF_")]
    r1 = parse(l1[-1]) if l1 else {}
    check("不缺的一对：跑通", rc1 == 0, out1[-200:])
    check("★ 不缺的一对：判定为不补（HF_SKIP）", r1.get("_kind") == "HF_SKIP",
          r1.get("_raw"))
    check("★ 不缺的一对：没有产出补偿文件（不许拿空文件当成品）",
          not os.path.isfile(o1))
    d1 = json.load(io.open(j1, encoding="utf-8")) if os.path.isfile(j1) else {}
    check("不缺的一对：报告里写明为什么不补", bool(d1.get("reason")),
          (d1.get("reason") or "")[:40])

    # 情形 2：成品被砍掉 5k 以上 ⇒ 真的缺
    o2 = os.path.join(hf_dir, "cut.wav")
    j2 = os.path.join(hf_dir, "cut.json")
    rc2, out2 = run_cli(hf, ["auto", dst, src, o2, j2])
    l2 = [x for x in out2.splitlines() if x.startswith("HF_")]
    r2 = parse(l2[-1]) if l2 else {}
    check("缺的一对：跑通", rc2 == 0, out2[-200:])
    check("★ 缺的一对：真的补了（HF_AUTO）", r2.get("_kind") == "HF_AUTO",
          r2.get("_raw"))
    check("★ 缺的一对：产出文件存在", os.path.isfile(o2))
    d2 = json.load(io.open(j2, encoding="utf-8")) if os.path.isfile(j2) else {}
    before = (d2.get("before") or {}).get("deficit_db")
    after = d2.get("after_deficit_db")
    check("★ 缺口真的补回来了（降 ≥5 dB）",
          before is not None and after is not None and (before - after) >= 5.0,
          "%.1f → %.1f dB" % (before or -1, after or -1))
    check("★ 补完还剩的缺口在达标线内（确实补够了，不是补了个寂寞）",
          after is not None and after <= 2.0, "剩 %.1f dB" % (after or -1))
    db4 = ((d2.get("before") or {}).get("dst") or {}).get("db4_8")
    db4b = ((d2.get("after") or {}).get("dst") or {}).get("db4_8")
    # ★ 为什么判"变化比例"而不是"4-8k 不许动"：cross 最后会按峰值归一，
    #   高频补上去之后峰值变大，整条轨被等比压小 —— 4-8k 的**绝对值**一定会
    #   跟着降一点（实测大帝干声是 0.0dB、合成素材是 2.1dB）。那是归一化的
    #   副作用，不是"中频被换了"。真要看的是：**改动主要落在高频，没有落在中频**。
    d_mid = abs((db4b or 0) - (db4 or 0))
    d_hi = abs((after or 0) - (before or 0))
    check("★ 改动主要落在高频——中频的变化不到高频变化的三分之一（只补缺口，不碰音色）",
          d_hi > 0 and d_mid <= d_hi / 3.0,
          "中频变 %.1f dB / 高频变 %.1f dB" % (d_mid, d_hi))
    tried = d2.get("tried") or []
    fcs = [t.get("fc") for t in tried]
    check("★ 是从最保守那一档开始、按顺序往下试的（不是一上来就用最猛的）",
          bool(fcs) and fcs == list(cands)[:len(fcs)], str(fcs))
    check("采用的正是最后一个试过的档（够用就停，没多试一档）",
          bool(fcs) and d2.get("fc") == fcs[-1],
          "采用 %s / 试过 %s" % (d2.get("fc"), fcs))

    # ----------------------------- D. 负对照 -----------------------------
    sec("D. 负对照：把尺子拧歪，判定必须跟着变")
    # ★ 三条负对照都走**命令行覆盖**，不复制源码另存一份。
    #   复制件的相对路径（找 tools/probe_bandwidth）会随目录失效 —— 那样测的是
    #   一份坏掉的副本，绿了也说明不了产品是对的。
    # D1：把"不补"的阈值抬到天上 ⇒ 连真缺的那一对也必须跳过
    o3 = os.path.join(hf_dir, "n1.wav")
    j3 = os.path.join(hf_dir, "n1.json")
    _, out3 = run_cli(hf, ["auto", dst, src, o3, j3, "--min-db", "999"])
    l3 = [x for x in out3.splitlines() if x.startswith("HF_")]
    check("★ 阈值抬到 999dB 后，真缺的那一对也被判成不补（闸真在起作用）",
          (parse(l3[-1]).get("_kind") if l3 else "") == "HF_SKIP",
          (l3[-1] if l3 else out3[-120:]))
    check("★ 那条闸**真的拦住了**：换回默认阈值时同一对素材必须补",
          d2.get("applied") is True, "applied=%r" % d2.get("applied"))

    # D2：把候选顺序反过来（先激进后保守）⇒ 选到的分频点必须不一样
    rev = ",".join(str(int(c)) for c in reversed(cands))
    o4 = os.path.join(hf_dir, "n2.wav")
    j4 = os.path.join(hf_dir, "n2.json")
    _, out4 = run_cli(hf, ["auto", dst, src, o4, j4, "--candidates", rev])
    d4 = json.load(io.open(j4, encoding="utf-8")) if os.path.isfile(j4) else {}
    check("★ 候选顺序反过来后，选到的分频点变了（顺序真在起作用）",
          d4.get("fc") is not None and d4.get("fc") != d2.get("fc"),
          "正序 %s / 反序 %s" % (d2.get("fc"), d4.get("fc")))
    check("★ 反序拿到的是更激进（更低）的分频点 —— 正好证明正序更省代价",
          d4.get("fc") is not None and d2.get("fc") is not None
          and d4["fc"] < d2["fc"],
          "%s < %s" % (d4.get("fc"), d2.get("fc")))

    # D3：把达标线设成不可能 ⇒ 必须一路试到最激进的一档（证明达标判定有效）
    o5 = os.path.join(hf_dir, "n3.wav")
    j5 = os.path.join(hf_dir, "n3.json")
    _, out5 = run_cli(hf, ["auto", dst, src, o5, j5, "--ok-db", "-999"])
    d5 = json.load(io.open(j5, encoding="utf-8")) if os.path.isfile(j5) else {}
    check("★ 达标线设成不可能后，会一路试到最激进的一档（达标判定真在起作用）",
          d5.get("fc") == cands[-1], "fc=%s，最激进=%s" % (d5.get("fc"), cands[-1]))

    print("\n" + "=" * 78, flush=True)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)), flush=True)
    for x in BAD:
        print("  FAIL: %s" % x, flush=True)
    if not BAD:
        print("高频补偿：全绿", flush=True)
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
