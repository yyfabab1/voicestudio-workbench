# -*- coding: utf-8 -*-
r"""门禁：去金属声（电音）/ 去破音（局部过冲），且**不许修成沙哑**。

================================================================================
这一关在防什么
================================================================================

老板连续四次反馈音质问题，四次根因各不相同，这一关是第三、四轮的产物：

  第一次「明显电音」  → 量化出来了（体检会告诉你"这一档会电"），但只告警不修。
  第二次「还是有电音」→ 补上"修"：抖动 + 削高频。
  第三次「依旧有破音」→ 真因是**识别到了却修不动** ⇒ 补上「分频抖动」。
  第四次「全过程沙哑 + 46 秒破音」← 最关键的一轮：**分频抖动就是沙哑的来源**
                       （jitter 0.0116 → 0.0172，+48%）。
                       ⇒ 沙哑不是"没修好"，是**修法本身错了**：
                         抖动 = 打散谐波（乘性）⇒ 沙哑；
                         补气声 = 加回噪声（加性）⇒ 气声（柔）。
                       真人 HNR 低是因为**有气声**，不是因为谐波散。

所以它守的是 stages/demetal_cli.py 的四条：

  1. 先量，不电就不动（但**过冲/破音照样修** —— 那是独立的毛病）。
  2. 电了才动手，从**耳朵代价最小**的剂量往上试，达标就停。
  3. 修完**再量一次**（闭环）。复测还判电就回退，不许装作修好了。
  4. 所有剂量都压不下去（或过不了护栏）⇒ **不动手**，如实报"没修好"。
     糊掉声音换一个好看的数字，比如实报"没修好"更糟。
  5. ★★ HNR 是**双面指标**：太高 = 电，太低 = 沙哑。只设上限 ⇒ 系统一路
     往下压 ⇒ 压成沙哑（第四轮就是这么栽的）。两侧都得有墙。

其中最容易被写坏的是第 1 条和第 4 条：它们都是"什么都不做"的分支，
一旦写错（比如把不存在的 out.wav 当成品交出去），症状是**混音读空文件**，
而日志上一片正常 —— 这种静默最难查。

================================================================================
判据的真在哪
================================================================================

A/B 段是源码对账（快，只证明"东西都在"）；
**C 段真跑** stages/demetal_cli.py：造一对"确实电"和一对"不电"的素材，
断言前者修了、后者没修、且没修时**根本不写 wav**；
**D 段是尺子自检**：把判据阈值旋到顶、把剂量表换成"假剂量"、只留纯削波档，
判定结果**必须跟着变**。不变就说明那条判据没在判，只是恰好没出错
（★ 挂了"负对照"标签却不会变红的尺子是假尺子，比没有更坏）；
**D2 段守第四轮的墙**：补过头（ΔHNR 掉破下限）/ 抖过头（jitter 超参考 1.6 倍）
都要被拒 —— 这两条做过尺子自检：把 HNR_FLOOR / JIT_MAX_REL 旋到 999 之后
**实测两条都红**（75/2），还原后 77/0。

★ 素材**全部现场合成**，不依赖 data/cache 或任何运行时残留 —— 门禁依赖
  运行时产物就是"今天绿明天红"（本项目栽过）。

用法（venv 或托管 python 都行；demetal_cli 走 RVC 的子进程）：
    <python> -s verify_sing_demetal.py
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

WORK = os.path.join(HERE, "_demetalprobe")

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
    import struct
    import wave
    y = y / max(1e-9, float(abs(y).max())) * 0.8
    pcm = (y * 32767.0).astype("int64").clip(-32768, 32767)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(b"".join(struct.pack("<h", int(v)) for v in pcm))


def make_pair(work):
    """造两对素材：一对"确实电"，一对"不电"。

    电音的两个物理特征都可以直接合成（见 demetal_cli.py 文件头的实测表）：
      · 原唱：谐波 + **宽带气声** ⇒ HNR 低。
      · RVC：谐波堆、**没有气声**（周期复印）⇒ HNR 高；
             高次谐波衰减慢 ⇒ 4-8k 占比高。
    所以"电"的那份 = 高次谐波强 + 不加噪声；"不电"的那份 = 加噪声 + 高次衰减快。
    """
    import numpy as np
    sr = 44100
    t = np.linspace(0.0, 8.0, int(sr * 8.0), endpoint=False)
    rng = np.random.RandomState(20261024)

    def stack(decay, nh):
        """谐波堆 + **共振峰**。

        ★ 共振峰不能省：纯谐波堆的能量全压在前几次谐波上，谱质心只有 ~590Hz
          （真素材是 ~1000Hz）。而"削 4-8k 会让质心掉百分之几"是**除以质心**
          算的 —— 质心越低的素材掉得越狠，于是合成素材会被副作用护栏一律挡下，
          "修成功"这条分支永远走不到，判据等于摆设。真人声质心高，是因为
          1~2.5kHz 有两个共振峰把中频抬起来了。
        """
        y = np.zeros_like(t)
        for k in range(1, nh):
            f = 220.0 * k
            a = 1.0 / (k ** decay)
            a *= (1.0 + 3.0 * np.exp(-((f - 1500.0) / 600.0) ** 2)
                  + 2.0 * np.exp(-((f - 2600.0) / 900.0) ** 2))
            if f > 6000.0:                      # 6k 以上滚降（真人也不会无限高）
                a *= np.exp(-(f - 6000.0) / 3000.0)
            y += a * np.sin(2 * np.pi * f * t + rng.uniform(0, 6.28))
        return y

    # ★★ 配方要点：噪声必须**小**，ΔHNR 靠"噪声比例差"造出来，不能靠大噪声。
    #    白噪声在 4-8k 的占比是 50%（均匀分布），噪声一大，hf_ratio 的绝对值
    #    就被撑到真人的好几倍（真人只有 0.39%），于是削 4-8k 时谱质心暴跌、
    #    副作用护栏永远不过 ⇒ 门禁只能测到"不修"那条分支（这一坑在 hf_cli 上踩过）。
    #    真人那两个数是这么共存的：**谐波能量压倒性大**，噪声只占千分之几。

    # ---- 原唱（参考）：谐波衰减快（4-8k 只剩一点点）+ 少量气声 ----
    src = stack(1.6, 44) + 0.05 * rng.randn(t.size)
    p_src = os.path.join(work, "ref.wav")
    _write_wav(p_src, src, sr)

    # ---- 成品：电 —— 高次谐波多且衰减慢（4-8k 堆起来）+ 气声被抹得更干净 ----
    #   44 次谐波到 9680Hz ⇒ 4-8k 有真能量；噪声从 0.05 降到 0.015
    #   ⇒ ΔHNR ≈ 20·log10(0.05/0.015) ≈ 10.5dB，正好是实测 +12 那一版的量级。
    p_hot = os.path.join(work, "hot.wav")
    _write_wav(p_hot, stack(1.0, 44) + 0.015 * rng.randn(t.size), sr)

    # ---- 成品：不电 —— 跟原唱一样"毛"（衰减快 + 同量级气声）----
    p_ok = os.path.join(work, "ok.wav")
    _write_wav(p_ok, stack(1.6, 44) + 0.05 * rng.randn(t.size), sr)
    return p_src, p_hot, p_ok


def run_cli(script, args):
    cmd = [studio.RVC_PY, "-s", script] + [str(a) for a in args]
    p = subprocess.run(cmd, cwd=studio.RVC_ROOT, stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT)
    out = (p.stdout or b"").decode("utf-8", "replace")
    return p.returncode, out


def _last(out):
    """取 stdout 里那行 DEMETAL_*（只认这一行，别把过程噪声混进来）。"""
    for ln in (out or "").splitlines()[::-1]:
        if ln.startswith("DEMETAL_"):
            return ln.strip()
    return ""


def main():
    os.makedirs(WORK, exist_ok=True)
    print("去金属声门禁 | RVC_PY=%s" % studio.RVC_PY, flush=True)

    st = read(os.path.join(HERE, "studio.py"))
    sv = read(os.path.join(HERE, "server.py"))
    cli = os.path.join(HERE, "stages", "demetal_cli.py")
    clis = read(cli) if os.path.isfile(cli) else ""
    html = read(os.path.join(HERE, "web", "index.html"))
    appjs = read(os.path.join(HERE, "web", "app.js"))

    # ----------------------------- A. 契约 -----------------------------
    sec("A. 契约：默认档是 auto，四条链路都接上了")
    check("DEMETAL_MODES 存在且 auto 排第一",
          hasattr(studio, "DEMETAL_MODES") and studio.DEMETAL_MODES
          and studio.DEMETAL_MODES[0] == "auto", str(getattr(studio, "DEMETAL_MODES", None)))
    check("DEMETAL_MODES 含 off / force（A/B 要能强制关和强制开）",
          "off" in studio.DEMETAL_MODES and "force" in studio.DEMETAL_MODES,
          str(studio.DEMETAL_MODES))
    check("sing_role 默认 demetal_mode=auto", 'demetal_mode="auto"' in st)
    check("sing_multi 默认 demetal_mode=auto",
          re.search(r"def sing_multi\(.*?demetal_mode=\"auto\"\)", st, re.S) is not None)
    check("sing_job 默认 demetal_mode=auto",
          re.search(r"def sing_job\(.*?demetal_mode=\"auto\"\)", st, re.S) is not None)
    check("_sing_preview 默认 demetal_mode=auto",
          re.search(r"def _sing_preview\(.*?demetal_mode=\"auto\"\)", st, re.S) is not None)
    check("sing_multi 把 demetal_mode 传下去（多角色以前漏接过开关）",
          "demetal_mode=demetal_mode" in st
          and st.count("demetal_mode=demetal_mode") >= 1)
    n_dm = len(re.findall(
        r'demetal_mode\s*=\s*\(\s*body\.get\("demetal"\)\s*'
        r'if body\.get\("demetal"\)\s*in\s*studio\.DEMETAL_MODES\s*'
        r'else\s*"auto"\s*\)', sv))
    check("server.py 两处都按 DEMETAL_MODES 兜底到 auto（不是 off）",
          n_dm >= 2, "匹配 %d 处" % n_dm)
    check("界面有 selDemetal 下拉且默认选中 auto",
          'id="selDemetal"' in html and 'value="auto" selected' in html)
    check("app.js 两处都把 demetal 传上去（单角色 + 多角色）",
          appjs.count('$("#selDemetal")') >= 2,
          "出现 %d 处" % appjs.count('$("#selDemetal")'))
    check("判据阈值由 studio 下发给子进程（只有一处定义，不许两把尺子）",
          "--hnr-db %s --hf-rel %s" in st or ("--hnr-db" in st and "--hf-rel" in st))
    # 去金属必须在非歌声段保护**之前**：保护之后那几段已换回原声（原声不电），
    # 混进去会把整轨的指标稀释掉。
    i_dm = st.find('dm_out = os.path.join(final, "voice_dm_')
    i_guard = st.find('g_out = os.path.join(final, "voiceg_')
    check("去金属声在「非歌声段保护」之前（否则指标被原声段稀释）",
          0 < i_dm < i_guard, "dm@%s guard@%s" % (i_dm, i_guard))

    # ----------------------------- B. 实现 -----------------------------
    sec("B. 实现：先量 → 不电不动 → 电才修 → 修完再量")
    for k in ("HNR_DB", "HNR_FLOOR", "JIT_MAX_REL", "HF_REL", "STEPS", "SEED",
              "CUT_HZ", "CENT_DROP_MAX", "MF_DROP_DB", "BREATH_MAX_INSERT",
              "OS_TARGET"):
        check("demetal_cli 有常量 %s" % k,
              re.search(r"^%s\s*=" % k, clis, re.M) is not None)
    # ★★ 2026-10-24 第四轮：剂量表从三元组变**四元组**
    #      (分频Hz, 抖动比, 4-8k增益dB, 补气声倍率)，排序原则也被实测推翻了。
    #   老前提："头一档是分频抖动，因为它中频/质心代价最小"。
    #   被推翻的经过：抖动确实便宜（中频 0.00dB、质心 −1.0%），但它**打散谐波**
    #     ⇒ jitter 涨 48% ⇒ 老板听到的是「全过程沙哑」。便宜不等于代价小 ——
    #     它便宜在"中频/质心"这两把尺子上，而耳朵听的那把（jitter/psim）它最贵。
    #   现在的排序原则：**按耳朵的代价排**
    #     1) 补气声（加性噪声，不动谐波相干性）→ 最前；
    #     2) 削 4-8k（会让 HNR 反升，救不了 HNR 那条）→ 其次；
    #     3) 分频抖动（打散谐波 = 沙哑）→ 最后手段。
    _steps = re.search(r"^STEPS\s*=\s*\((.*?)^\)", clis, re.S | re.M)
    _rows = []
    if _steps:
        for m in re.finditer(
                r"\(\s*(-?[0-9.]+)\s*,\s*(-?[0-9.]+)\s*,\s*(-?[0-9.]+)\s*,\s*"
                r"(-?[0-9.]+)\s*\)", _steps.group(1)):
            _rows.append((float(m.group(1)), float(m.group(2)),
                          float(m.group(3)), float(m.group(4))))
    check("★ 剂量表第一档是**补气声**（不是抖动：抖动的代价是打散谐波 = 沙哑）",
          bool(_rows) and _rows[0][3] > 0.0
          and _rows[0][1] == 0.0 and _rows[0][2] == 0.0,
          "第一档 %s" % ((_rows[0] if _rows else None),))
    _br_only = [i for i, r in enumerate(_rows)
                if r[3] > 0.0 and r[1] == 0.0 and r[2] == 0.0]
    _eq_i = [i for i, r in enumerate(_rows) if r[2] < 0.0]
    _jit_i = [i for i, r in enumerate(_rows) if r[1] > 0.0]
    check("★ 抖动排在所有补气声档之后（沙哑就是上一轮抖出来的）",
          bool(_br_only) and bool(_jit_i) and max(_br_only) < min(_jit_i),
          "补气声档 %s / 抖动档 %s" % (_br_only, _jit_i))
    check("★ 削 4-8k 也排在补气声档之后（补气声代价更小）",
          bool(_br_only) and bool(_eq_i) and max(_br_only) < min(_eq_i),
          "补气声档 %s / 削波档 %s" % (_br_only, _eq_i))
    # 组内仍然从轻到重：补气声倍率递增、抖动比递增（不许"先用重剂量"）
    _br_asc = all(_rows[_br_only[i]][3] <= _rows[_br_only[i + 1]][3] + 1e-9
                  for i in range(len(_br_only) - 1))
    _jit_asc = all(_rows[_jit_i[i]][1] <= _rows[_jit_i[i + 1]][1] + 1e-9
                   for i in range(len(_jit_i) - 1))
    check("★ 剂量表组内从轻到重（补气声递增、抖动递增）",
          len(_rows) >= 3 and _br_asc and _jit_asc,
          "%d 档：%s" % (len(_rows), [(r[1], r[2], r[3]) for r in _rows]))
    check("★ 剂量表里保留「只削 4-8k」那条路（不抖也不补；4-8k 顶超标时靠它）",
          any(r[1] == 0.0 and r[2] < 0 and r[3] == 0.0 for r in _rows),
          "有只削档 = %s"
          % any(r[1] == 0.0 and r[2] < 0 and r[3] == 0.0 for r in _rows))
    check("★ 剂量表里有「分频抖动 + 削 4-8k」的兜底组合档（都救不动时）",
          any(r[0] > 1000.0 and r[1] > 0 and r[2] < 0 for r in _rows),
          "有组合档 = %s"
          % any(r[0] > 1000.0 and r[1] > 0 and r[2] < 0 for r in _rows))
    check("固定随机种子（同一首歌每次修出来必须一样，否则没法 A/B）",
          "default_rng(seed)" in clis and "SEED = " in clis)
    check("不电时**不写 wav**就返回（DEMETAL_SKIP not_metal）",
          "DEMETAL_SKIP not_metal" in clis)
    check("压不下去时也不动手（DEMETAL_SKIP no_safe_dose）",
          "DEMETAL_SKIP no_safe_dose" in clis)
    check("闭环：落盘后再量一次（after = artifact_cli.measure(out_wav",
          "artifact_cli.measure(out_wav" in clis)
    check("★ 复测还判电就把成品删掉、不算修好（不许装作成功）",
          re.search(r"recheck_still_metal", clis) is not None
          and re.search(r"res\[.applied.\]\s*=\s*False", clis) is not None)
    check("副作用护栏用「处理前」当基准（不是原唱）",
          "base_cent = centroid(y, sr)" in clis
          and "base_mid = band_rel(y, sr" in clis)
    # ---- 第四轮（沙哑）这次落地的四件事，各留一条结构判据 ----
    #   这些都是"源码对账"，快；真正会红的证明在 D 段（双边判据实测拒用）。
    check("★ HNR 是**双边区间**（有 HNR_FLOOR 下限：只设上限会把声音压成沙哑）",
          re.search(r"^HNR_FLOOR\s*=", clis, re.M) is not None
          and "floor_ok" in clis and "hoarse" in clis)
    check("★ jitter 有上限护栏 JIT_MAX_REL（打散谐波的直接后果）",
          re.search(r"^JIT_MAX_REL\s*=", clis, re.M) is not None
          and "jit_ok" in clis and "hoarse = " in clis)
    # ★★ 2026-10-24 第五轮的真 bug：注释里白纸黑字写着"基准是处理前的成品"，
    #    代码却写成 base.get(...)（base = **原唱参考轨**）。后果：
    #      ref jitter 0.02197 × 1.6 = 0.0351 ⇒ 抖动档的 0.01796 轻松过关
    #    ⇒ 系统甩开补气声去用抖动 ⇒ jitter 0.00828 → 0.01796（+117%），
    #      比老板投诉的那版还糟。**说了却没做的事，最需要门禁盯着。**
    check("★★ jitter 护栏的基准是**处理前的成品 before**，不是参考轨 base"
          "（取错会让上限放宽 2.6 倍 ⇒ 抖动过关 ⇒ 沙哑）",
          re.search(r"base_jit\s*=\s*before\.get\(\"jitter_local\"\)",
                    clis) is not None,
          "源码里 base_jit 的取值 = %s"
          % ((re.search(r"base_jit\s*=\s*[^\n]+", clis).group(0))
             if re.search(r"base_jit\s*=", clis) else "没找到"))
    check("★ jitter 护栏同时留了**绝对下限**（只用相对倍数时，在本来就极干净的"
          "素材上会把正常处理误判成沙哑 —— 合成素材 jitter 5e-05，补一点就 ×2.2）",
          re.search(r"^JIT_ABS_MAX\s*=", clis, re.M) is not None
          and " JIT_ABS_MAX" in clis)
    # ★★ 2026-10-24 第五轮：这一条的前提被数据推翻了 —— 当初怕补气声把 4-8k
    #   顶超标才加了 4k 低通。实测**低通没带来提效**（低通 4k 达标要植入 83.0%，
    #   全带是 86.2%，差别在误差内），而 hf_rel 本来就有判据兜底。
    #   ⇒ 加一道没被证明有用的限制是纯负债，删掉。
    #   替换成下面两条更硬的：**植入上限** 和 **剂量单位**。
    check("★ 补气声有**植入上限** BREATH_MAX_INSERT（老板第五次反馈的「杂音」"
          "就是没有上限导致的：实测塞了 83%）",
          re.search(r"^BREATH_MAX_INSERT\s*=\s*([0-9.]+)", clis, re.M) is not None
          and 0.0 < float(re.search(r"^BREATH_MAX_INSERT\s*=\s*([0-9.]+)",
                                    clis, re.M).group(1)) <= 0.40,
          "上限 = %s" % (re.search(r"^BREATH_MAX_INSERT\s*=\s*([0-9.]+)",
                                   clis, re.M).group(1)
                         if re.search(r"^BREATH_MAX_INSERT\s*=", clis, re.M)
                         else None))
    check("★ 超过植入上限的档会被直接跳过（上限不只是注释里的一句话）",
          "超过植入上限" in clis, "源码里没有对 br > BREATH_MAX_INSERT 的处理")
    check("★ 剂量单位是**植入百分比**而不是倍数（倍数没法设上限：同样一句 α，"
          "不同素材塞进去的能量能差一倍）",
          "br * base_rms" in clis,
          "没找到 (br * base_rms) —— 那剂量就还是倍数语义")
    check("★ 记下了每档的 insert（真金白银塞进去多少；排查「杂音」唯一用得上的数）",
          '"insert"' in clis and "insert=" in clis)
    check("★ 去脉冲：补的气声要摊掉孤立脉冲（股噪比 19.2 的 P 分量直接加进去就是"
          "「杂音」；实测去脉冲让效率翻倍）",
          # ★ 不能只查 "uniform_filter1d" 这个字符串 —— 光留着 import、把调用删掉
          #   它也照样为真（第五轮尺子自检时亲测：这么写它就是一条假尺子）。
          #   要盯着它**真的作用在 STFT 幅度上**。
          re.search(r"mag\s*=\s*uniform_filter1d\(", clis) is not None
          and re.search(r"phase\s*=\s*np\.exp\(1j \*|np\.exp\(1j \* rng\.uniform",
                        clis) is not None,
          "没找到去脉冲（STFT 幅度时间平滑 + 随机相位重合成）")
    check("★ 只补在成品出声的地方（参考轨句间的成分都是分离残漏，不是气声）",
          "voice_env" in clis and "dst=" in clis)
    # ★ 顺序判据不写死函数签名（2026-10-24 第五轮：写了 `breath_of(yref_al, sr)`，
    #   函数一加参数就假红）。认函数名即可。
    #   ★ 而且要从 cmd_auto 内部开始找 —— 直接 find 会先命中**函数定义**
    #   （def 写在前面），顺序就永远是"定义序"而不是"调用序"（又一个假尺子）。
    _i_cmd = clis.find("def cmd_auto")
    _i_ov = clis.find("de_overshoot(", _i_cmd if _i_cmd > 0 else 0)
    _i_br = clis.find("breath_of(", _i_cmd if _i_cmd > 0 else 0)
    check("★ 削过冲（修破音）在补气声**之前**（顺序反了两者互相抵消）",
          0 <= _i_ov < _i_br or (_i_ov > 0 and _i_br < 0),
          "de_overshoot@%d / breath_of@%d" % (_i_ov, _i_br))
    check("★ 参考轨采样率不同先重采样（否则逐窗比值比的是两个时间轴）",
          "librosa.resample(yref_al" in clis)
    check("★ 测量按整首、从头取（取中间段会让系统为数字好看多加抖动）",
          re.search(r"def _win\(.*?return 0\.0", clis, re.S) is not None)
    check("★ 测量窗口均匀铺满整首（只取开头 0.17 秒会让护栏误判）",
          "step = max(1, (n - W)" in clis)
    check("★ 质心下限参考原唱（成品质心本就偏高，压回去是往真人修，不是闷）",
          "min(cands)" in clis)

    # ---- verdict 纯函数：四条分支 ----
    sys.path.insert(0, os.path.join(HERE, "stages"))
    try:
        import demetal_cli as dc
    except Exception as e:
        dc = None
        check("能 import demetal_cli（门禁要直接测它的判据）", False,
              "%s: %s" % (type(e).__name__, e))
    if dc:
        b = {"hnr_db": 11.42, "hf_ratio": 0.0039}
        v = dc.verdict(b, {"hnr_db": 21.93, "hf_ratio": 0.0163}, 4.0, 2.0)
        check("判电：HNR 涨 10.5dB（实测 +12 那版）", v[0] is True, str(v))
        v = dc.verdict(b, {"hnr_db": 15.4, "hf_ratio": 0.0040}, 4.0, 2.0)
        check("不判电：HNR 只涨 4.0dB、4-8k 没翻（大帝 +3 那版）", v[0] is False, str(v))
        # ★ 降八度（−12）时 HNR 反而**低** 4.6dB，但 4-8k 占比照样翻 3 倍 ——
        #   这条不能判电，否则"降调"会被误判成"电音"。
        v = dc.verdict(b, {"hnr_db": 6.8, "hf_ratio": 0.0116}, 4.0, 2.0)
        check("★ 不判电：HNR 反而降了但 4-8k 翻 3 倍（降八度不能被误判成电音）",
              v[0] is False, str(v))
        v = dc.verdict(b, {"hnr_db": 12.5, "hf_ratio": 0.0116}, 4.0, 2.0)
        check("判电：HNR 涨 1.1dB + 4-8k 翻 3 倍（两条凑齐才判）", v[0] is True, str(v))

    # ---- 与 studio 的判据必须同源 ----
    sec("B2. 判据只有一处定义（studio 与 demetal_cli 对得上）")
    if dc:
        check("HNR 阈值与 studio.AUDITION_METAL_HNR_DB 一致",
              abs(float(dc.HNR_DB) - float(studio.AUDITION_METAL_HNR_DB)) < 1e-9,
              "%s vs %s" % (dc.HNR_DB, studio.AUDITION_METAL_HNR_DB))
        check("4-8k 阈值与 studio.AUDITION_METAL_HF_REL 一致",
              abs(float(dc.HF_REL) - float(studio.AUDITION_METAL_HF_REL)) < 1e-9,
              "%s vs %s" % (dc.HF_REL, studio.AUDITION_METAL_HF_REL))
        same = True
        det = []
        for cand in ({"hnr_db": 21.93, "hf_ratio": 0.0163},
                     {"hnr_db": 15.4, "hf_ratio": 0.0040},
                     {"hnr_db": 6.8, "hf_ratio": 0.0116},
                     {"hnr_db": 12.5, "hf_ratio": 0.0116}):
            a = bool(dc.verdict(b, cand, 4.0, 2.0)[0])
            c = bool(studio.metal_verdict(b, cand)[0])
            if a != c:
                same = False
                det.append("%s: cli=%s studio=%s" % (cand, a, c))
        check("★ 四组输入上两边结论完全一致（不是两把尺子）", same, "; ".join(det))

    # ----------------------------- C. 真跑 -----------------------------
    sec("C. 真跑 stages/demetal_cli.py")
    ref, hot, ok = make_pair(WORK)

    # ---- C0：先自检素材本身，别让判据在"假素材"上静默全绿 ----
    # 为什么单独列一段：素材一旦退化（比如某天有人把衰减改陡了），症状是
    # "修成功"那几条全红，但**看不出是素材的锅还是产品的锅**。这一段把两件事分开。
    if dc:
        def _hfr(p):
            """4-8k 能量 / 0-8k 能量（与 artifact_cli.hf_ratio 同一定义）。"""
            import numpy as _np
            y, s = dc._read(p)
            n = min(len(y), s * 30)
            sp = _np.abs(_np.fft.rfft(y[:n] * _np.hanning(n))) ** 2
            fr = _np.fft.rfftfreq(n, 1.0 / s)
            tot = float(sp[(fr > 0) & (fr < 8000)].sum())
            hi = float(sp[(fr >= 4000) & (fr < 8000)].sum())
            return (hi / tot) if tot > 0 else None

        ct_h = dc.centroid(*dc._read(hot)[::1])
        ct_r = dc.centroid(*dc._read(ref)[::1])
        check("★ 合成成品的谱质心在真素材量级（真素材 ~1000Hz，"
              "质心太低会让副作用护栏一律挡下，判据就走不到'修成功'）",
              ct_h is not None and 700.0 <= ct_h <= 1500.0,
              "hot=%s ref=%s" % (ct_h, ct_r))
        _h, _r = _hfr(hot), _hfr(ref)
        check("★ 合成素材的 4-8k 占比在真素材量级（真人 0.39% / RVC +12 1.63%）",
              _h is not None and 0.005 <= _h <= 0.030,
              "hot=%.4f ref=%.4f" % (_h or 0, _r or 0))
    d = os.path.join(WORK, "run")
    os.makedirs(d, exist_ok=True)

    o1 = os.path.join(d, "hot.wav")
    j1 = os.path.join(d, "hot.json")
    rc1, out1 = run_cli(cli, ["auto", hot, ref, o1, j1,
                              "--hnr-db", "4.0", "--hf-rel", "2.0"])
    r1 = json.load(io.open(j1, encoding="utf-8")) if os.path.isfile(j1) else {}
    check("电的素材被判电（判据在真素材上真的会红）",
          r1.get("metal") is True,
          "dhnr=%s hf_rel=%s" % (r1.get("dhnr"), r1.get("hf_rel")))
    check("电的素材 ⇒ DEMETAL_APPLIED", _last(out1).startswith("DEMETAL_APPLIED"),
          _last(out1) or out1[-200:])
    check("修完的文件真的落盘了", os.path.isfile(o1))
    # ★★ 这条判据的前提在第五轮被推翻了：「修完必须达标」不再总是成立。
    #   现在允许 **partial**（副作用全合格、但复测仍判电的最好的那一档）——
    #   因为「再加料」的代价是杂音（老板第五次反馈），而 partial 是明知+已声明的。
    #   ⇒ 判据改成二选一，并且 partial 时必须**照实写进 reason**（不许装作成功）。
    _partial = bool(r1.get("partial"))
    check("★ 修完要么 ΔHNR 掉到判据线以内，要么明确标记为 partial 并说明原因",
          (r1.get("after_dhnr") is not None and r1["after_dhnr"] < 4.0)
          or (_partial and "没压干净" in (r1.get("reason") or "")),
          "%.2f → %.2f partial=%s reason=%r"
          % (r1.get("dhnr") or 0, r1.get("after_dhnr") or 0, _partial,
             (r1.get("reason") or "")[:60]))
    check("★ partial 采档**不许**超过植入上限（ushima = 杂音）",
          not _partial or float(r1.get("breath") or 0.0)
          <= float(dc.BREATH_MAX_INSERT) + 1e-9 if dc else True,
          "breath=%s 上限=%s" % (r1.get("breath"),
                                 getattr(dc, "BREATH_MAX_INSERT", "?")
                                 if dc else "?"))
    # ★ 老写法要求"4-8k 至少降 1 个倍数"，前提是"修复必然要削高频"。加了分频抖动
    #   之后，落点可能选的是「只抖不削」—— 那时 4-8k 本来就不降（实测 6.51 → 7.20，
    #   因为抖动让高频谐波失相干、能量略散）。真正该守的是"不许越修越糟"，
    #   而不是"必须靠削高频达标"（后者由下面那条「只削档能降 4-8k」单独验）。
    check("修完 4-8k 没有明显变差（≤ 修前 ×1.15）",
          r1.get("after_hf_rel") is not None
          and r1["after_hf_rel"] <= (r1.get("hf_rel") or 0) * 1.15 + 1e-9,
          "%.2f → %.2f" % (r1.get("hf_rel") or 0, r1.get("after_hf_rel") or 0))
    # ★ 用**产品自己的判据**复算，而不是门禁另立一条更严的：
    #   4-8k 那条必须"配合 HNR 也涨了"才成立（否则降调会被误判成电音），
    #   所以修完 rel 还大于 2 是完全可能的、且是正确的 —— 只要 verdict 说不电。
    if dc:
        still = dc.verdict(r1.get("ref") or {}, r1.get("after") or {}, 4.0, 2.0)[0]
        # partial 时判电是**预期结果**（它就是在"没达标但副作用合格"里挑的那一档），
        #   只要落盘了、reason 说清楚了就对；其余情况必须真的不判电。
        check("★ 用产品判据复算：修完不判电（闭环；partial 档除外，它怕的是"
              "「把杂音当成功」不是「数字没到」）",
              (not still) or _partial,
              "dhnr=%s hf_rel=%s partial=%s"
              % (r1.get("after_dhnr"), r1.get("after_hf_rel"), _partial))
    check("确实动过手（补气声 / 抖动 / 削波至少用了一个）",
          (r1.get("breath") or 0) > 0 or (r1.get("jitter") or 0) > 0
          or (r1.get("eq_db") or 0) < 0,
          "breath=%s jitter=%s eq=%s"
          % (r1.get("breath"), r1.get("jitter"), r1.get("eq_db")))
    # ★ 沙哑在信号上就是 jitter 涨。修完 jitter 必须**朝参考值靠拢**，不能背离
    #   （上一轮用抖动：jitter 0.0116 → 0.0172，+48% ⇒ 老板听出「全过程沙哑」）。
    _jb = (r1.get("before") or {}).get("jitter_local")
    _ja = (r1.get("after") or {}).get("jitter_local")
    _jr = (r1.get("ref") or {}).get("jitter_local")
    _closer = (None if (None in (_jb, _ja, _jr))
               else abs(_ja - _jr) <= abs(_jb - _jr) + 1e-12)
    check("★ 修完 jitter 朝参考值**靠拢**（沙哑 = jitter 涨；背离就是修坏了）",
          _closer is True,
          "before=%s after=%s ref=%s" % (_jb, _ja, _jr))
    # 副作用护栏
    tried1 = r1.get("tried") or []
    last_ok = [t for t in tried1 if not t.get("metal")
               and t.get("cent_ok") and t.get("mid_ok")]
    check("★ 采用的那一档过了副作用护栏（不闷不糊）", bool(last_ok),
          json.dumps(last_ok[:1], ensure_ascii=False))
    if r1.get("mid_before") is not None and last_ok:
        drop = float(r1["mid_before"]) - float(last_ok[-1].get("mid") or 0)
        check("中频（2-4k）没被削掉太多", drop <= float(dc.MF_DROP_DB) + 0.01
              if dc else True, "掉了 %.2f dB（上限 %s）" % (drop, dc.MF_DROP_DB if dc else "?"))

    # ---- 不电的素材 ----
    o2 = os.path.join(d, "ok.wav")
    j2 = os.path.join(d, "ok.json")
    rc2, out2 = run_cli(cli, ["auto", ok, ref, o2, j2,
                              "--hnr-db", "4.0", "--hf-rel", "2.0"])
    r2 = json.load(io.open(j2, encoding="utf-8")) if os.path.isfile(j2) else {}
    check("不电的素材被判不电", r2.get("metal") is False,
          "dhnr=%s hf_rel=%s" % (r2.get("dhnr"), r2.get("hf_rel")))
    check("不电 ⇒ DEMETAL_SKIP", _last(out2).startswith("DEMETAL_SKIP"), _last(out2))
    check("★ 不电时**不写 wav**（调用方拿不到空文件去混音）",
          not os.path.isfile(o2), "存在=%s" % os.path.isfile(o2))
    check("跳过的退出码是 0（不是失败）", rc2 == 0, "rc=%d" % rc2)

    # ---- 固定种子：两次跑出来必须字节一致 ----
    o3 = os.path.join(d, "hot2.wav")
    j3 = os.path.join(d, "hot2.json")
    run_cli(cli, ["auto", hot, ref, o3, j3, "--hnr-db", "4.0", "--hf-rel", "2.0"])
    same_bytes = False
    if os.path.isfile(o1) and os.path.isfile(o3):
        with open(o1, "rb") as f1, open(o3, "rb") as f3:
            same_bytes = f1.read() == f3.read()
    check("★ 固定种子：同一对素材跑两次字节一致（A/B 才对得上）", same_bytes)

    # ----------------------------- D. 负对照 -----------------------------
    sec("D. 负对照：判据旋一下，结果必须跟着变")
    # ★ 全部走命令行覆盖，**不复制源码另存一份再改** —— 副本里的相对路径会失效，
    #   那样测的是副本不是产品（本项目在 hf_cli 上踩过）。
    o4 = os.path.join(d, "n1.wav")
    j4 = os.path.join(d, "n1.json")
    _, out4 = run_cli(cli, ["auto", hot, ref, o4, j4, "--hnr-db", "99",
                            "--hf-rel", "99"])
    r4 = json.load(io.open(j4, encoding="utf-8")) if os.path.isfile(j4) else {}
    # ★ 2026-10-24：「不写 wav」这条前提变了。过冲（破音）是**独立的毛病**，
    #   不管判不判电都要修，所以阈值旋到顶之后仍然会写 wav（mode=overshoot）。
    #   该守的是"**电音那一步没动手**"：metal=False 且没有采用任何剂量。
    check("★ 阈值旋到顶 ⇒ 同一对素材不再判电，且**不进电音修复**（判据真在起作用）",
          r4.get("metal") is False and r4.get("chosen") is None,
          "metal=%s mode=%s chosen=%s | %s"
          % (r4.get("metal"), r4.get("mode"), r4.get("chosen"), _last(out4)))

    # 只留"假剂量"（啥也不做的一档）⇒ 修不出效果 ⇒ 不许动手
    o5 = os.path.join(d, "n2.wav")
    j5 = os.path.join(d, "n2.json")
    _, out5 = run_cli(cli, ["auto", hot, ref, o5, j5, "--steps", "0,0"])
    r5 = json.load(io.open(j5, encoding="utf-8")) if os.path.isfile(j5) else {}
    check("★ 剂量表换成「啥也不做」⇒ 没有一档被采用（不许糊掉声音换数字）",
          r5.get("chosen") is None and r5.get("mode") == "overshoot_only",
          "mode=%s chosen=%s | %s"
          % (r5.get("mode"), r5.get("chosen"), _last(out5)))
    check("  试档记录里每一档都真的试过", len(r5.get("tried") or []) >= 1,
          "tried=%d" % len(r5.get("tried") or []))

    # 只留纯削波档 ⇒ 对"HNR 高"这条**救不了**（实测削高频会让 HNR 反升）
    o6 = os.path.join(d, "n3.wav")
    j6 = os.path.join(d, "n3.json")
    _, out6 = run_cli(cli, ["auto", hot, ref, o6, j6, "--steps", "0,-4;0,-6"])
    r6 = json.load(io.open(j6, encoding="utf-8")) if os.path.isfile(j6) else {}
    _t6 = r6.get("tried") or []
    check("★ 只给削波档 ⇒ 压不下去（ΔHNR 反而被顶高）⇒ 一档都不采用",
          r6.get("chosen") is None and bool(_t6)
          and all(t.get("metal") for t in _t6),
          json.dumps([(t.get("eq_db"), t.get("dhnr")) for t in _t6],
                     ensure_ascii=False))

    # force 档：跳过"判不判电"，但副作用护栏照旧
    # ★ 剂量用**产品真正下发的那一条**（studio.demetal 给 force 传的是 0,0,0,0.15
    #   —— 第五轮改的，以前是 0,0,0,1.0，那个是"倍数"语义，现在单位是植入百分比）。
    #   老判据用的是"0.006,-4"（抖动 + 削波）—— 那条会被双边判据**判沙哑拒掉**
    #   （实测 ΔHNR −9.38、jitter 超 1.6 倍），于是 no_safe_dose，看着像产品坏了。
    # ★ 2026-10-24 第五轮：studio 不再给 force 钉死剂量了（不同素材的安全植入量
    #   能差 2.5 倍，钉死的数在有些歌上直接压过头 ⇒ 一档都不可用）。
    #   ⇒ force 现在只下发 `--force`，剂量交给子进程从最轻那档往上试。
    #     这里守的是"产品也没敢偷偷给它带 steps"。
    check("★ force 不再越权钉死剂量（不同素材差 2.5 倍，钉死必翻车）",
          "--force" in st and "--force --steps" not in st,
          "studio.py 里还有 --force --steps")
    o7 = os.path.join(d, "n4.wav")
    j7 = os.path.join(d, "n4.json")
    _, out7 = run_cli(cli, ["auto", ok, ref, o7, j7, "--force"])
    r7 = json.load(io.open(j7, encoding="utf-8")) if os.path.isfile(j7) else {}
    check("★ force 档在**不电**的素材上也动手（证明它真的跳过了判定）",
          _last(out7).startswith("DEMETAL_APPLIED") and os.path.isfile(o7),
          _last(out7))
    check("  ★ force 用的是**补气声**（不许偷偷拿抖动凑效果 —— 抖动＝沙哑）",
          (r7.get("breath") or 0) > 0 and (r7.get("jitter") or 0) == 0.0,
          "breath=%s jitter=%s eq=%s"
          % (r7.get("breath"), r7.get("jitter"), r7.get("eq_db")))
    check("  force 档仍守副作用护栏（强制 ≠ 允许把声音弄坏）",
          bool([t for t in (r7.get("tried") or [])
                if t.get("cent_ok") and t.get("mid_ok")]),
          json.dumps((r7.get("tried") or [])[:1], ensure_ascii=False))

    # ------------------- D2. 双边判据：不许把声音压成沙哑 -------------------
    # ★ 第四轮的核心。老逻辑只有"ΔHNR 太高 ⇒ 修"这一条上限，等于鼓励一路往下压
    #   ⇒ 压到老板反馈「全过程沙哑」。现在两侧都有墙，这两条验墙**真的会挡**：
    #     对照 A：breath=4.0 达标被采用（C 段已验）；
    #     下面 breath=40 ⇒ ΔHNR 掉破下限 ⇒ 拒用。同一条轴，量变到质变。
    sec("D2. 双边判据：补过头 / 抖过头都要被拒（防沙哑）")
    #   对照 A：合成素材上一个合适的档被采用（C 段已验）；
    #   下面贴着**植入上限**跑 ⇒ ΔHNR 掉破下限 ⇒ 拒用。同一条轴，量变到质变。
    #   ★ 第五轮改了剂量单位：不能再用 40 这种"倍数"了 —— 40% 会被"超过植入上限"
    #     直接拦掉（那条路径压根不到 judging），测不到 HNR_FLOOR。
    _hi = str(getattr(dc, "BREATH_MAX_INSERT", 0.25)) if dc else "0.25"
    o9 = os.path.join(d, "n6.wav")
    j9 = os.path.join(d, "n6.json")
    _, out9 = run_cli(cli, ["auto", hot, ref, o9, j9, "--force",
                            "--steps", "0,0,0,%s" % _hi])
    r9 = json.load(io.open(j9, encoding="utf-8")) if os.path.isfile(j9) else {}
    _t9 = r9.get("tried") or []
    check("★ 补气声补到上限 ⇒ ΔHNR 掉破 HNR_FLOOR ⇒ 判沙哑 ⇒ 拒用"
          "（HNR 是双面指标，只设上限会把声音压成沙哑）",
          r9.get("chosen") is None and bool(_t9)
          and any(t.get("floor_ok") is False for t in _t9),
          json.dumps([(t.get("breath"), t.get("dhnr"), t.get("floor_ok"))
                      for t in _t9], ensure_ascii=False) or _last(out9))
    # ★ 植入上限本身也要验：超过它的档**连 Kroon 都不会**（直接拦掉，
    #   而不是等 measure 判出来）—— 上限是结构性的，不是事后检查。
    ob = os.path.join(d, "n8.wav")
    jb = os.path.join(d, "n8.json")
    _, outb = run_cli(cli, ["auto", hot, ref, ob, jb, "--force",
                            "--steps", "0,0,0,0.9"])
    rb2 = json.load(io.open(jb, encoding="utf-8")) if os.path.isfile(jb) else {}
    check("★★ 超过植入上限的档被直接拦掉（上限是硬闸，不是靠判据事后发现）",
          rb2.get("chosen") is None
          and any("超过植入上限" in (t.get("err") or "")
                  for t in (rb2.get("tried") or [])),
          json.dumps([(t.get("breath"), t.get("err"))
                      for t in (rb2.get("tried") or [])],
                     ensure_ascii=False) or _last(outb))

    oa = os.path.join(d, "n7.wav")
    ja = os.path.join(d, "n7.json")
    _, outa = run_cli(cli, ["auto", hot, ref, oa, ja, "--force",
                            "--steps", "1500,0.05,0,0"])
    ra = json.load(io.open(ja, encoding="utf-8")) if os.path.isfile(ja) else {}
    _ta = ra.get("tried") or []
    check("★ 抖动抖过头 ⇒ jitter 超参考的 JIT_MAX_REL 倍 ⇒ 判沙哑 ⇒ 拒用"
          "（这就是上一轮「全过程沙哑」的直接来源）",
          ra.get("chosen") is None and bool(_ta)
          and any(t.get("jit_ok") is False for t in _ta),
          json.dumps([(t.get("jitter"), t.get("jit"), t.get("jit_ok"))
                      for t in _ta], ensure_ascii=False) or _last(outa))

    # ★ 上面那条只是"表里还有只削档"的结构判据。这里验它**真能干活**：
    #   4-8k 单独超标（HNR 不高）时，剂量表里只有削高频这一条路能救。
    #   force 钉死只削档来跑，看 4-8k 是不是真下去了。
    o8 = os.path.join(d, "n5.wav")
    j8 = os.path.join(d, "n5.json")
    _, out8 = run_cli(cli, ["auto", hot, ref, o8, j8, "--force",
                            "--steps", "0,0,-4"])
    r8 = json.load(io.open(j8, encoding="utf-8")) if os.path.isfile(j8) else {}
    hf0 = (r8.get("before") or {}).get("hf_ratio")
    hf1 = (r8.get("after") or {}).get("hf_ratio")
    check("★ 只削档（−4dB）真能把 4-8k 压下去（4-8k 单独超标时只能靠它）",
          bool(hf0 and hf1) and hf1 < hf0 * 0.75,
          "hf_ratio %.5f → %.5f (%s)" % (hf0 or 0, hf1 or 0, _last(out8)))

    print("\n" + "=" * 78, flush=True)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)), flush=True)
    for x in BAD:
        print("  FAIL: %s" % x, flush=True)
    if not BAD:
        print("去金属声：全绿", flush=True)
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
