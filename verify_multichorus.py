#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""门禁：**逐句指定「这一句独唱 / 这一句合唱」**（老板原话）。

要锁住的功能
------------
以前"谁唱哪一句"只有一种粒度：一段时间归一个人。老板要的是**逐句**选择
「这一句**独唱**（谁唱）」或「这一句**合唱**（所有人一起唱）」。

约定：**分配表里 role = -1 的那一段 = 合唱**（`multising.ROLE_ALL`）。
这个 -1 要在**四处**都成立，缺一处它就安静地不存在（且不报错）：

  ① `stages/multising.py`  —— 分侧时**不许被 `max(0, ...)` 夹成 0**；
                               拼接时按"主唱原音量 + 其余 ×0.55"混。
  ② `studio.py`            —— 拼接命令带上 `--cgain`（否则界面那根和声音量
                               滑块对逐句合唱完全无效）。
  ③ `web/app.js`           —— 有「合唱」这支笔、每行有这个选项、点下去送 -1。
  ④ `vizfx.py`             —— 画面上把合唱句写成「合唱」而不是某个人的名字。

★ 本关最重要的一条：**没有合唱段时，拼接必须逐样本与改动前完全一致。
  改混音最容易顺手改坏独唱那半（门禁量过的"等响 / 无缝"就是那半）。
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MULTI = os.path.join(HERE, "stages", "multising.py")
STUDIO = os.path.join(HERE, "studio.py")
SERVER = os.path.join(HERE, "server.py")
APPJS = os.path.join(HERE, "web", "app.js")
VIZFX = os.path.join(HERE, "vizfx.py")

N_PASS = 0
FAILS = []


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s" % name)
    else:
        FAILS.append(name)
        print("  [!!] %s\n        %s" % (name, detail or "无诊断信息"))


def load_multi():
    st = os.path.dirname(MULTI)
    if st not in sys.path:
        sys.path.insert(0, st)
    spec = importlib.util.spec_from_file_location("multising_chorus", MULTI)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _read(p):
    try:
        return open(p, encoding="utf-8").read()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# A. 源码形状：四处约定必须都在
# ---------------------------------------------------------------------------

def a_shape(mod):
    print("\nA. 源码形状：-1 = 合唱，四处都得认")
    src = _read(MULTI)
    check("ROLE_ALL 常量存在且为 -1",
          "ROLE_ALL = -1" in src and hasattr(mod, "ROLE_ALL")
          and int(mod.ROLE_ALL) == -1,
          "mod.ROLE_ALL=%r" % getattr(mod, "ROLE_ALL", None))
    # ★ 最阴的一种：无脑 max(0, ...) 把 -1 夹成 0（合唱 → 第一个人独唱）
    body = src[src.find("def assign_lines("):src.find("\ndef ", src.find("def assign_lines(") + 1)]
    check("manual 里 -1 不被夹成 0",
          "ROLE_ALL if v == ROLE_ALL" in body or "v == ROLE_ALL" in body)
    body2 = src[src.find("def takes_from_lines("):src.find("\ndef ", src.find("def takes_from_lines(") + 1)]
    check("时间轴划分里 -1 不被夹成 0",
          ("ROLE_ALL if int(who) < 0" in body2) and "max(0, int(who))" not in body2.split("# ★ 不许")[0],
          "还留着 max(0, int(who))？")
    st = _read(STUDIO)
    check("★ 拼接命令带 --cgain（逐句合唱也吃那根和声音量滑块）",
          st.count("--cgain") >= 2, "找到 %d 处（应 ≥2：首次拼接 + 重拼）" % st.count("--cgain"))
    j = _read(APPJS)
    check("前端有 MULTI_CHORUS = -1 且与后端同值",
          "MULTI_CHORUS = -1" in j)
    # ★ 这条盯的是「-1 走旁支、其余才夹」这个**结构**，不是某几个字符：
    #   把夹写成 `Math.max(0, Math.min(Number(k)||0, ...))` 之后，
    #   照着旧的字面串去比对就假红 —— 判据盯着字串，实现一改写法就报警，
    #   那是尺子坏了，不是产品坏了。
    m_cho = re.search(r"\(Number\(k\)\s*===\s*MULTI_CHORUS\)\s*\n\s*\?\s*MULTI_CHORUS\s*:\s*Math\.max\(0,\s*Math\.min\(",
                      j)
    check("前端把 -1 当合唱，不夹成 0（-1 走旁支，其余才夹）",
          bool(m_cho) and "Number(k) === MULTI_CHORUS" in j,
          "没匹配到「-1 ? -1 : clamp」这个结构")
    check("每行下拉里有「合唱（所有人）」选项",
          'value="${MULTI_CHORUS}"' in j and "合唱（所有人）" in j)
    v = _read(VIZFX)
    check("★ 画面把合唱句写成「合唱」而不是某个人的名字",
          "CHORUS_LABEL" in v and v.count("k < 0") >= 1,
          "vizfx 里没处理 role<0 ⇒ 合唱句会被标成最后那个人独唱")


# ---------------------------------------------------------------------------
# B. 行为：分配表（纯函数）
# ---------------------------------------------------------------------------

def b_plan(mod):
    print("\nB. 分配表：合唱句能一路走到时间轴")
    segs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0},
            {"start": 2.0, "end": 3.0}, {"start": 3.0, "end": 4.0}]
    lr = mod.assign_lines(segs, 2, mode="manual", manual=[0, -1, -1, 1])
    check("manual 里 -1 原样保留（不被夹成 0）", lr == [0, -1, -1, 1], str(lr))
    tk = mod.takes_from_lines(segs, lr, 4.0, 2)
    check("相邻的合唱句合成一段（不给自己缝一刀）",
          any(int(t[2]) < 0 and abs(t[1] - t[0] - 2.0) < 1e-6 for t in tk), str(tk))
    check("合唱段与独唱段之间留接缝（进出合唱要淡化）",
          sum(1 for t in tk if int(t[2]) < 0) == 1 and len(tk) == 3, str(tk))
    stt = mod.takes_stats(tk, 2)
    check("合唱秒数**不**算进任何人的独唱",
          all(abs(x["sec"] - 1.0) < 0.01 for x in stt), str(stt))
    check("合唱秒数单独报（每个人同一份）",
          all(abs(x["chorus_sec"] - 2.0) < 0.01 for x in stt), str(stt))
    # ★ 要量的是"合唱那段时间对每个角色都算开口"，不是"能切出一段 2 秒的区间"
    #   —— 合唱段（1~3s）和乙的独唱段（3~4s）首尾相接，本来就该被合并成 1~4s，
    #   按"必须正好 2 秒"去量是把正确行为判成错（本关第一版就栽在这）。
    def _covers(spans, a, b):
        for s, e in spans:
            if s <= a + 1e-6 and e >= b - 1e-6:
                return True
        return False
    for k in (0, 1):
        check("★ 逐人体检把合唱段算作出声（角色 %d 的区间覆盖 1~3s）" % k,
              _covers(mod.role_spans(tk, k), 1.0, 3.0),
              str(mod.role_spans(tk, k)))
    # 负对照：按改动前的写法（合唱段不算任何人的）角色 1 只剩 3~4s，覆盖不了 1~3s
    old_spans = [(t[0], t[1]) for t in tk if int(t[2]) == 1]
    check("尺子自检：改动前的写法覆盖不了合唱段 ⇒ 这条判据有牙",
          not _covers(old_spans, 1.0, 3.0), str(old_spans))


# ---------------------------------------------------------------------------
# C. 行为：混音（要 numpy）
# ---------------------------------------------------------------------------

def _sine(f, secs, sr, amp=0.5):
    import numpy as np
    t = np.arange(int(sr * secs), dtype=np.float32) / float(sr)
    return (np.sin(2 * np.pi * f * t) * amp).astype(np.float32)


def _peak_at(sig, sr, a, b, f):
    """窗口内某个频率的谱峰幅度（用 FFT，不靠猜）。"""
    import numpy as np
    seg = np.asarray(sig[int(a * sr):int(b * sr)], dtype=np.float32)
    if len(seg) < 8:
        return 0.0
    w = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    fr = np.fft.rfftfreq(len(seg), 1.0 / sr)
    i = int(np.argmin(np.abs(fr - f)))
    return float(w[max(0, i - 2):i + 3].max())


def c_audio(mod):
    print("\nC. 混音：合唱段两条声音都得在，独唱段只准有一条")
    try:
        import numpy as np
    except ImportError:
        check("跑得到 numpy（本段需要）", False)
        return
    sr = 16000
    x0 = _sine(220, 6, sr)
    x1 = _sine(440, 6, sr)
    # 0-1 甲独唱｜1-3 合唱｜3-4 乙独唱｜4-6 甲独唱
    takes = [(0, 1, 0), (1, 3, -1), (3, 4, 1), (4, 6, 0)]
    out, st = mod.blend([x0, x1], takes, sr)

    solo_a = _peak_at(out, sr, 0.05, 0.95, 220)
    solo_a2 = _peak_at(out, sr, 0.05, 0.95, 440)
    cho_a = _peak_at(out, sr, 1.05, 2.95, 220)
    cho_b = _peak_at(out, sr, 1.05, 2.95, 440)
    solo_b1 = _peak_at(out, sr, 3.05, 3.95, 220)
    solo_b2 = _peak_at(out, sr, 3.05, 3.95, 440)

    check("独唱段只有那一个人的声音",
          solo_a > 100 and solo_a2 < solo_a * 0.02,
          "甲窗 220=%.1f 440=%.1f" % (solo_a, solo_a2))
    check("★ 合唱段两个人的声音**都在**",
          cho_a > 100 and cho_b > 100,
          "合唱窗 220=%.1f 440=%.1f" % (cho_a, cho_b))
    check("★ 合唱段非主唱按 other_gain（0.55）垫底，不是一半一半",
          abs(cho_b / cho_a - 0.55) < 0.06,
          "比值 %.3f（期望 ≈0.55）" % (cho_b / cho_a,))
    check("乙独唱段只有乙", solo_b2 > 100 and solo_b1 < solo_b2 * 0.02,
          "乙窗 220=%.1f 440=%.1f" % (solo_b1, solo_b2))
    check("统计里报出了合唱", st.get("chorus") is True
          and abs(st.get("chorus_sec") - 2.0) < 0.01, str(st.get("chorus_sec")))

    # ---- ★★ 没有合唱段时必须逐样本与旧实现一致 -------------------------
    tk2 = [(0, 3, 0), (3, 6, 1)]
    out2, st2 = mod.blend([x0, x1], tk2, sr)
    # 旧算法（改动前的写法）在门里原样复算一遍，逐样本比
    xs = [np.asarray(x[:len(out2)], dtype=np.float32) for x in (x0, x1)]
    masks = []
    for i in range(2):
        m = np.zeros(len(out2), dtype=np.float32)
        for t in tk2:
            if int(t[2]) != i:
                continue
            a = max(0, int(round(t[0] * sr)))
            b = min(len(out2), int(round(t[1] * sr)))
            if b > a:
                m[a:b] = 1.0
        masks.append(m)
    gains, _ = mod._gains(xs, masks, sr)
    xs = [x * g for x, g in zip(xs, gains)]
    sm = [mod._smooth(m, sr, 20.0) for m in masks]
    tot = sm[0] + sm[1]
    tot = np.where(tot < 1e-6, 1.0, tot)
    ref = xs[0] * (sm[0] / tot) + xs[1] * (sm[1] / tot)
    d = float(np.abs(out2 - ref).max())
    check("★★ 没有合唱段时逐样本与改动前一致（独唱那半没被改坏）",
          d < 1e-6, "最大偏差 %.3e" % d)


# ---------------------------------------------------------------------------
# D. 尺子自检：三种坏版本必须被抓到
# ---------------------------------------------------------------------------

def d_teeth(mod):
    """**这一整段是尺子自检**，不是产品判据：三种坏实现必须各自变红。"""
    print("\nD. 尺子自检：三种坏版本必须被上面的判据抓到")
    try:
        import numpy as np
    except ImportError:
        check("跑得到 numpy", False)
        return

    # 坏版本 ①：把 -1 夹成 0（合唱 → 第一个人独唱）
    def bad_clamp(v, n):
        return max(0, min(n - 1, int(v)))
    segs = [{"start": 0.0, "end": 1.0}, {"start": 1.0, "end": 2.0}]
    bad_lr = [bad_clamp(v, 2) for v in [0, -1]]
    check("坏版本①（夹成 0）确实把合唱变成了第一个人独唱",
          bad_lr == [0, 0], str(bad_lr))
    good_lr = mod.assign_lines(segs, 2, mode="manual", manual=[0, -1])
    check("好版本与坏版本①结论相反 ⇒ B 段有牙",
          good_lr == [0, -1] and good_lr != bad_lr, str(good_lr))

    # 坏版本 ②：合唱段做"归一化"（两个人各占一半）—— 那就不是和声了
    sr = 16000
    x0 = _sine(220, 3, sr)
    x1 = _sine(440, 3, sr)
    half = (x0 + x1) / 2.0                       # 归一化：各占一半
    lay = (x0 + x1 * 0.55)                       # 正解：主唱原音量 + 0.55 垫底
    r_half = _peak_at(half, sr, 0.2, 2.8, 440) / _peak_at(half, sr, 0.2, 2.8, 220)
    r_lay = _peak_at(lay, sr, 0.2, 2.8, 440) / _peak_at(lay, sr, 0.2, 2.8, 220)
    check("坏版本②（各占一半）的比值明显不是 0.55 ⇒ C 段有牙",
          abs(r_half - 0.55) > 0.15 and abs(r_lay - 0.55) < 0.06,
          "归一化 %.3f / 正解 %.3f" % (r_half, r_lay))

    # 坏版本 ③：前端把 -1 夹成 0（界面点了合唱，送出去的还是第 1 个人）
    def bad_js(k, n):
        return Math.max(0, Math.min(k, n - 1))    # noqa: F821  ← 故意的伪代码

    class Math(object):                            # 就地给它一个最小实现
        @staticmethod
        def max(a, b):
            return a if a > b else b

        @staticmethod
        def min(a, b):
            return a if a < b else b

    check("坏版本③（前端无脑夹）把合唱笔变成了第 0 个人",
          bad_js(-1, 2) == 0, str(bad_js(-1, 2)))
    j = _read(APPJS)
    check("好版本前端显式放行合唱笔 ⇒ A 段那条形判据有牙",
          "Number(k) === MULTI_CHORUS" in j)


# ---------------------------------------------------------------------------
# E. 接线：界面点得到 + 服务端送得出
# ---------------------------------------------------------------------------

def e_wiring():
    print("\nE. 接线：用户点得到的每一个环节")
    j = _read(APPJS)
    check("manual 模式下和声音量滑块也显示（否则合唱没法调）",
          'mode === "chorus" || mode === "manual"' in j)
    check("★ 开跑时 manual 也把和声音量送出去（不送就是模块默认，滑块白调）",
          'mode === "chorus" || mode === "manual"' in j
          and "body.chorus_gain" in j)
    check("汇总里把合唱秒数单独显示（不然各人相加超过曲长）",
          "chorus_sec" in j)
    st = _read(STUDIO)
    check("预览里提示'这几句所有人一起唱'", "合唱**（这几句所有人一起唱）" in st
          or "其中 %.1f 秒是**合唱**" in st)
    sv = _read(SERVER)
    check("接口只校验 manual 是数组（不校验取值范围 ⇒ -1 过得去）",
          '"manual 必须是数组"' in sv)


def f_segonly_chorus(mod):
    """★★ seg_only × 合唱 的交叉 bug：合唱段必须**每个人都转**。

    老板原话：「一起唱全算在一个人身上」。机制是两个功能各自都对、合起来才错：
      · 合唱段在分配表里是 `ROLE_ALL`(-1)，**不属于任何一个人**；
      · `segonly.takes_of` 以前只认 `role == who` ⇒ 开着「只转自己分到的那几句」
        时，合唱段**没有任何角色去转换**，那条轨在合唱段是静音；
      · 而 `blend` 的合唱混音是 `主唱轨 + 其余×0.55` ⇒ 静音的那些轨贡献 0
        ⇒ 只剩"恰好退回整首转换"的那个人有声。所有人 seg_only 时更惨：
        合唱段只剩伴奏。

    所以这里不只查函数返回值，还**按它的语义造两条轨、真混一遍听频率**：
    合唱窗里 220Hz 和 440Hz 必须同时存在，且非主唱是主唱的 0.55 倍。
    再拿旧语义造一遍做负对照 —— 必须复现"只剩一个人"。
    """
    print("\nF. seg_only × 合唱：合唱段必须每个人都转")
    st = os.path.join(HERE, "stages")
    if st not in sys.path:
        sys.path.insert(0, st)
    import numpy as np
    try:
        import segonly as SO
    except Exception as e:                                  # noqa: BLE001
        check("能加载 segonly", False, str(e)[:200])
        return

    takes = [(0.0, 2.0, 0), (2.0, 4.0, -1), (4.0, 6.0, 1)]
    for k in (0, 1):
        got = SO.takes_of(takes, k)
        check("角色 %d 的待转窗口包含合唱段 [2,4]" % k,
              [2.0, 4.0] in got, str(got))
    # 中间那段是合唱 ⇒ 两个人**都要**转它；独唱段只归自己
    check("独唱段没被乱塞给别人（角色 0 不该拿到 [4,6]）",
          [4.0, 6.0] not in SO.takes_of(takes, 0),
          str(SO.takes_of(takes, 0)))
    check("★ 负对照：with_chorus=False 时不含合唱段（证明是这个参数在起作用）",
          [2.0, 4.0] not in SO.takes_of(takes, 0, with_chorus=False),
          str(SO.takes_of(takes, 0, with_chorus=False)))

    sr, dur = 16000, 6.0

    def track(f, wins):
        x = np.zeros(int(sr * dur), dtype=np.float32)
        w = _sine(f, dur, sr)
        for a, b in wins:
            x[int(a * sr):int(b * sr)] = w[int(a * sr):int(b * sr)]
        return x

    # 按**修好后**的语义造轨：每个人只在自己的窗口（含合唱段）有声
    good = [track(220.0, SO.takes_of(takes, 0)),
            track(440.0, SO.takes_of(takes, 1))]
    out, st2 = mod.blend(good, takes, sr)
    a220 = _peak_at(out, sr, 2.15, 3.85, 220.0)
    a440 = _peak_at(out, sr, 2.15, 3.85, 440.0)
    check("合唱段两个人的声音都在（220 与 440 同时出现）",
          a220 > 0.05 and a440 > 0.05, "220=%.3f 440=%.3f" % (a220, a440))
    check("合唱段非主唱是主唱的 0.55 倍（没被归一化压成一半）",
          abs(a440 / max(a220, 1e-6) - 0.55) < 0.12,
          "比值=%.3f" % (a440 / max(a220, 1e-6)))
    # 独唱窗里不该有另一个人
    check("独唱窗里只有一个人（角色 1 唱的时候没有 220）",
          _peak_at(out, sr, 4.15, 5.85, 220.0) < a220 * 0.05,
          "%.4f" % _peak_at(out, sr, 4.15, 5.85, 220.0))

    # ★★ 负对照：按**旧**语义（不含合唱段）造轨 ⇒ 合唱段没有人声
    #   实测有**两种**坏形态，取决于 `segonly.should_use` 的判定：
    #     ① 两个人都 seg_only（常见）⇒ 合唱段两条轨全静音 ⇒ **只剩伴奏**；
    #     ② 其中一人"省不了"退回整首 ⇒ 合唱段**只剩那一个人**在唱
    #        （这才是老板听到的"一起唱全算在一个人身上"）。
    #   这里复现的是 ①（可以稳定造出来）；② 是它的另一种落点，同一处根因。
    bad = [track(220.0, SO.takes_of(takes, 0, with_chorus=False)),
           track(440.0, SO.takes_of(takes, 1, with_chorus=False))]
    out_b, _ = mod.blend(bad, takes, sr)
    b220 = _peak_at(out_b, sr, 2.15, 3.85, 220.0)
    b440 = _peak_at(out_b, sr, 2.15, 3.85, 440.0)
    check("★ 负对照：旧语义下合唱段没有人声（两条轨都静音 ⇒ 只剩伴奏）",
          max(b220, b440) < 1e-3,
          "旧语义 220=%.4f 440=%.4f（都不该有值）" % (b220, b440))

    # 接线：sing_multi 调它时不许显式关掉合唱
    src = _read(STUDIO)
    check("sing_multi 调 takes_of 没把合唱排除在外",
          "takes_of(takes, k)" in src and
          "takes_of(takes, k, with_chorus=False)" not in src)


def main():
    print("门禁：逐句指定「合唱 / 独唱 + 谁唱」")
    mod = load_multi()
    a_shape(mod)
    b_plan(mod)
    c_audio(mod)
    d_teeth(mod)
    e_wiring()
    f_segonly_chorus(mod)
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  FAIL %s" % f)
    print("EXIT=%d" % (1 if FAILS else 0))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
