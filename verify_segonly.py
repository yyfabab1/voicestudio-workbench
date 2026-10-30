# -*- coding: utf-8 -*-
"""「只转自己分到的那几句」门禁。

全程**不跑模型**：用合成音频（锯齿波，逐样本可辨）做往返比对。
最硬的三条判据：
  ① 往返一致性 —— 转换若是恒等变换，放回原位后必须与原音频**逐样本相同**；
  ② 非本人段落必须严格为 0（漏一点点就是"别人的声音漏进来了"）；
  ③ 输出长度必须等于全曲长度（否则下游拼接整段错位）。

变异测试：临时脚本 _m_mut_seg.py（改坏 → 跑一遍 → 数红 → 还原，用完即删），
12 个变异 12 个全抓住；做法与结果记在 .workbuddy/memory/proj-vs.md。
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
STAGES = os.path.join(ROOT, "stages")
if STAGES not in sys.path:
    sys.path.insert(0, STAGES)

PASS, FAIL = [], []

try:
    import numpy as np           # 模块级：各个小节都要用
    import soundfile as sf
except ImportError:
    np = None
    sf = None

try:
    import segonly as S          # 模块级：各个小节都要用
except ImportError:
    S = None


def check(name, ok, info=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s%s" % ("[ok]" if ok else "[FAIL]", name,
                         ("  —— %s" % info) if (info and not ok) else ""))


def main():
    print("只转分到的那几句（seg-only）门禁：不跑模型，只验几何与接线")
    if np is None or sf is None:
        print("  缺 numpy / soundfile，跑不了")
        return 1
    if S is None:
        print("  导入 segonly 失败")
        return 1

    tmp = tempfile.mkdtemp(prefix="segchk_")
    sr = 44100
    dur = 60.0
    x = (np.arange(int(dur * sr), dtype="float32") % 997) / 997.0 * 2 - 1
    src = os.path.join(tmp, "src.wav")
    sf.write(src, x, sr, subtype="PCM_16")

    # takes: 0 号角色唱 [0,3] [6,9] [20,23] [40,44]；1 号唱 [3,6]
    takes = [(0.0, 3.0, 0), (3.0, 6.0, 1), (6.0, 9.0, 0),
             (20.0, 23.0, 0), (40.0, 44.0, 0)]
    segs = S.takes_of(takes, 0)

    # ---- A. 取段 ----
    a_segs(segs)
    # ---- B. 合并 ----
    b_merge()
    # ---- C. 窗口几何 ----
    c_windows(S, dur)
    # ---- D. 拼接 + 往返（最硬的三条）----
    d_roundtrip(S, src, x, segs, dur, sr, tmp)
    # ---- E. 采样率与偏差 ----
    e_resample(S, src, segs, dur, tmp, np, sf)
    # ---- F. 该不该省 ----
    f_should(S, takes, dur)
    # ---- G. 后端/前端接线 ----
    g_wiring()
    # ---- H. 对齐（RVC 输出固定早 10~20ms 那个坑）----
    h_align(S, src, x, segs, dur, sr, tmp)

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    for f in FAIL:
        print("  FAIL %s" % f)
    return 1 if FAIL else 0


def a_segs(segs):
    print("\nA. 取段")
    check("只取指定角色那几段", segs == [[0.0, 3.0], [6.0, 9.0], [20.0, 23.0],
                                     [40.0, 44.0]], "%s" % segs)
    check("按时间排序", segs == sorted(segs, key=lambda z: z[0]), "%s" % segs)
    dirty = [(5.0, 6.0, 0), None, (1.0, 2.0, 0), ("a", 3.0, 0), (7.0, 7.0, 0),
             (8.0, 4.0, 0)]
    check("脏数据（None/非数字/倒序/零长）跳过而不是崩",
          S.takes_of(dirty, 0) == [[1.0, 2.0], [5.0, 6.0]],
          "%s" % S.takes_of(dirty, 0))
    check("没人分到 ⇒ 空列表", S.takes_of(takes := [(0.0, 1.0, 1)], 0) == [])
    check("takes 为 None ⇒ 空列表", S.takes_of(None, 0) == [])


def b_merge():
    print("\nB. 合并相邻段")
    m = S.merge_segments([[0.0, 3.0], [4.0, 7.0], [20.0, 23.0]], 2.0)
    check("相隔 1 秒（< 2）⇒ 并成一段", m == [[0.0, 7.0], [20.0, 23.0]], "%s" % m)
    m2 = S.merge_segments([[0.0, 3.0], [6.0, 9.0]], 2.0)
    check("相隔 3 秒（>= 2）⇒ 不并", m2 == [[0.0, 3.0], [6.0, 9.0]], "%s" % m2)
    check("空 ⇒ 空", S.merge_segments([], 2.0) == [])


def c_windows(S, dur):
    print("\nC. 窗口几何")
    w = S.plan_windows([[0.0, 3.0], [6.0, 9.0], [20.0, 23.0]], dur, ctx=1.5)
    check("段前后各留 1.5 秒上下文",
          w[0]["win"] == [0.0, 10.5],
          "%s" % [z["win"] for z in w])
    check("加上下文后重叠的两个窗口并成一个（parts 也跟着合）",
          len(w) == 2 and w[0]["parts"] == [[0.0, 3.0], [6.0, 9.0]],
          "%s" % w)
    # 边界裁剪
    w2 = S.plan_windows([[0.2, 1.0], [59.0, 60.0]], dur, ctx=1.5)
    check("左边裁到 0（不许负）", w2[0]["win"][0] == 0.0, "%s" % w2[0]["win"])
    check("右边裁到 duration（不许超）", w2[-1]["win"][1] == dur,
          "%s" % w2[-1]["win"])
    # 段完全在曲子外面
    check("整段都在曲子外 ⇒ 丢掉",
          S.plan_windows([[70.0, 80.0]], dur, ctx=1.5) == [],
          "%s" % S.plan_windows([[70.0, 80.0]], dur, ctx=1.5))
    check("duration <= 0 ⇒ 空", S.plan_windows([[0.0, 1.0]], 0) == [])
    # ★ 窗口只多不少：必须完整盖住 parts
    ok = all(min(p[0] for p in z["parts"]) >= z["win"][0] - 1e-6
             and max(p[1] for p in z["parts"]) <= z["win"][1] + 1e-6
             for z in w)
    check("窗口完整盖住它服务的每一段（不许多也不许少）", ok, "%s" % w)


def d_roundtrip(S, src, x, segs, dur, sr, tmp):
    print("\nD. 拼接 → 放回（最硬的三条）")
    wins = S.plan_windows(S.merge_segments(segs), dur)
    cat = os.path.join(tmp, "cat.wav")
    m = S.build_input(src, wins, cat)
    check("拼出来的长度 = 各窗口之和 + 缝",
          abs(m["dur"] - (sum(w["win"][1] - w["win"][0] for w in wins)
                          + 0.5 * (len(m["wins"]) - 1))) < 0.01,
          "%.3f" % m["dur"])
    check("采样率跟着源走", m["sr"] == sr, "%s" % m["sr"])

    out = os.path.join(tmp, "out.wav")
    r = S.scatter(cat, m, dur, out)          # 恒等变换：conv 就是输入
    NF = max(1, int(round(float(getattr(S, "FADE_SEC", 0.005)) * sr)))
    y, sr2 = sf_read(out)
    check("输出长度 == 全曲长度（差 < 1 帧）",
          abs(len(y) / float(sr2) - dur) < 1.0 / sr, "%.4f" % (len(y) / float(sr2)))

    bad = [s for s in segs
           if not _close(y[int(s[0] * sr) + NF:int(s[1] * sr) - NF],
                         x[int(s[0] * sr) + NF:int(s[1] * sr) - NF])]
    check("① 往返逐样本一致（转换若恒等，放回原位必须一模一样；"
          "首尾 5ms 是刻意淡入淡出，不参与比对）",
          not bad, "%d 段对不上" % len(bad))

    mask = np.ones(len(y), bool)
    for s in segs:
        mask[int(s[0] * sr):int(s[1] * sr)] = False
    check("② 非本人段落严格为 0（漏一点就是别人的声音进来了）",
          float(np.abs(y[mask]).max()) == 0.0,
          "最大 %.6f" % float(np.abs(y[mask]).max()))
    check("③ 本人段落有声音（别把要唱的也清成 0）",
          float(np.abs(y[~mask]).max()) > 0.5,
          "最大 %.6f" % float(np.abs(y[~mask]).max()))

    # ---- 接缝：每段首尾必须落在 0 上（否则就是一声"咔"）----
    edge_bad = []
    for s in segs:
        pa, pb = int(s[0] * sr), int(s[1] * sr)
        if abs(float(y[pa])) > 1e-6 or abs(float(y[pb - 1])) > 1e-6:
            edge_bad.append((s, float(y[pa]), float(y[pb - 1])))
    check("④ 每段首尾样本严格为 0（淡入淡出没做 = 接缝会响一声）",
          not edge_bad, "%s" % edge_bad[:2])
    check("淡入淡出只作用在「整段有声区」的边界（不是每句都切一刀）",
          r.get("runs") == 4, "实际 %s 段（应 4 段）" % r.get("runs"))
    # ★ 这一条防的是"用样本非零去判连续段"那个错法：音频本身到处过零，
    #   那样的实现会把这一段锯齿波切成几百段，然后逐段淡入淡出（内部全是坑）。
    inner = y[int(20.0 * sr) + NF:int(23.0 * sr) - NF]
    check("有声区内部没有任何淡入淡出（逐点等于原音）",
          _close(inner, x[int(20.0 * sr) + NF:int(23.0 * sr) - NF]),
          "内部被改动过")

    # 一句都没分到 ⇒ 整条静音，长度照旧
    sil = os.path.join(tmp, "sil.wav")
    S.silent_track(dur, sil, sr)
    sy, _ = sf_read(sil)
    check("一句没分到 ⇒ 同长静音轨（下游拼接照常能跑）",
          abs(len(sy) / float(sr) - dur) < 0.01 and float(np.abs(sy).max()) == 0.0,
          "%.3f 秒 / max %.6f" % (len(sy) / float(sr), float(np.abs(sy).max())))


def e_resample(S, src, segs, dur, tmp, np, sf):
    print("\nE. 采样率不同 + RVC 丢样本")
    wins = S.plan_windows(S.merge_segments(segs), dur)
    cat = os.path.join(tmp, "cat2.wav")
    m = S.build_input(src, wins, cat)
    # 模拟 RVC：输出 40kHz（跟输入不同），且因为切片丢了 0.2% 的样本
    yc, _ = sf_read(cat)
    n2 = int(len(yc) * 40000.0 / 44100 * 0.998)
    conv = os.path.join(tmp, "conv40.wav")
    sf.write(conv, np.interp(np.linspace(0, len(yc) - 1, n2),
                             np.arange(len(yc)), yc).astype("float32"),
             40000, subtype="PCM_16")
    out = os.path.join(tmp, "out40.wav")
    r = S.scatter(conv, m, dur, out)
    yo, sro = sf_read(out)
    check("输出采样率 = 转换结果的采样率（不是源的）", sro == 40000, "%s" % sro)
    check("长度仍按**全曲**算（用 conv 的采样率）",
          abs(len(yo) / float(sro) - dur) < 0.01, "%.3f" % (len(yo) / float(sro)))
    check("丢样本：比例校正后 drift 如实报出来（不藏着）",
          abs(r.get("drift", 0)) > 0.001 and 0.99 < r.get("scale", 1) < 1.0,
          "scale=%s drift=%s" % (r.get("scale"), r.get("drift")))
    # 位置仍然对：段内要有声音、段外仍为 0
    mask = np.ones(len(yo), bool)
    for s in segs:
        mask[int(s[0] * sro):int(s[1] * sro)] = False
    check("换了采样率也照样：段外严格为 0",
          float(np.abs(yo[mask]).max()) == 0.0,
          "%.6f" % float(np.abs(yo[mask]).max()))
    check("换了采样率也照样：段内有声音",
          float(np.abs(yo[~mask]).max()) > 0.3,
          "%.6f" % float(np.abs(yo[~mask]).max()))


def f_should(S, takes, dur):
    print("\nF. 该不该省（省不了就退回整首）")
    ok, why, d = S.should_use(takes, 0, dur)
    check("正常分配 ⇒ 省（实唱 13 秒 / 全曲 60 秒）",
          ok and d["sing"] == 13.0 and d["conv"] < 30.0, "%s / %s" % (ok, d))
    ok2, why2, _ = S.should_use(takes, 0, dur)
    check("理由里带具体数字（用户能自己判断值不值）",
          "秒" in why2 and "%" in why2, why2)
    ok3, why3, _ = S.should_use([(0.0, 55.0, 0), (55.0, 60.0, 1)], 0, dur)
    check("分到 55 秒 / 全曲 60 秒（>55%）⇒ 不省", not ok3, why3)
    ok4, why4, _ = S.should_use([(0.0, 1.0, 9)], 0, dur)
    check("一句都没分到 ⇒ 不省（并说清楚）", not ok4 and "没分到" in why4, why4)
    # 段太碎：20 段各 1 秒、彼此隔 5 秒 ⇒ 全是上下文在充数
    bits = [(i * 6.0, i * 6.0 + 1.0, 0) for i in range(8)]
    ok5, why5, d5 = S.should_use(bits, 0, 48.0)
    check("段太碎（要转的 > 实唱 3 倍）⇒ 不省", not ok5 and "碎" in why5,
          "%s / %s" % (ok5, d5))
    check("明细给出实唱/要转/窗口数（写进日志用）",
          all(k in d5 for k in ("sing", "conv", "wins")), "%s" % d5)


def g_wiring():
    print("\nG. 接线（后端默认 / 参数 / 前端开关）")
    S_studio = read("studio.py")
    S_server = read("server.py")
    S_app = read("web/app.js")
    S_html = read("web/index.html")
    check("默认开（MULTI_SEG_ONLY = True，2026-10-22 A/B 实测后才翻的）",
          "MULTI_SEG_ONLY = True" in S_studio, "常量不见了")
    check("默认值旁边写清了实测依据（谱相关/频段/RMS 都要在注释里）",
          all(k in S_studio for k in ("0.94", "频段", "RMS")),
          "注释里没有实测数字 —— 后人不知道为什么敢默认开")
    check("sing_multi 收 seg_only 参数", "seg_only=None" in S_studio, "缺参数")
    check("叠唱强制关掉（叠唱本来就是每人唱整首）",
          'if use_seg and mode == "chorus"' in S_studio, "没有这条判断")
    check("server 透传 seg_only（缺省=None ⇒ 用默认）",
          "seg_only=(bool(body.get(\"seg_only\"))" in S_server
          and "if body.get(\"seg_only\") is not None else None" in S_server,
          "服务端没透传或缺省处理不对")
    # ★ 高频补偿的参考轨：seg 模式下必须是那条拼接源轨，否则静音段会冒原唱气声
    check("seg 模式下高频补偿用**拼接源轨**做参考（不是整首）",
          "_ref = seg_src if this_seg else vocal_wav" in S_studio,
          "参考轨用错了 —— 静音段会凭空冒出原唱气声")
    check("转换结果放回原时间轴后才交给下游",
          "_so.scatter(post, seg_map, total, out_wav)" in S_studio, "没散射")
    check("中间那条拼接轨用完删掉（省磁盘、不被清理逻辑当孤儿）",
          "seg_%s_r%d_in.wav" in S_studio and "os.remove(_f)" in S_studio,
          "中间文件没清")
    check("前端有开关且**默认勾上**（和后端默认一致）",
          'id="chkSegOnly" checked' in S_html, "界面上没有勾选框 / 不是默认勾选")
    check("前端一定传布尔值（只传 true 的话用户取消勾选就传不出去）",
          'body.seg_only = !!$("#chkSegOnly").checked' in S_app, "前端没接线")
    check("开关上写明了实测依据（不再写「还没做 A/B」）",
          "2026-10-22 实测" in S_html and "还没做过整曲" not in S_html,
          "界面文案没跟上默认值")
    # ★ 步骤条文案最容易和默认值脱节（实测就脱过一次：默认已经改成"只唱几句"，
    #   步骤条还写着"默认跑整首"，用户看了会以为开关没生效）
    check("步骤条文案与默认值一致（不许再说「默认跑整首」）",
          "默认跑**整首**" not in S_studio
          and "默认**只唱分到的那几句**" in S_studio,
          "步骤条文案和实际默认对不上")


def h_align(S, src, x, segs, dur, sr, tmp):
    print("\nH. 对齐（RVC 输出比输入固定早 10~20ms）")
    wins = S.plan_windows(S.merge_segments(segs), dur)
    cat = os.path.join(tmp, "cat_align.wav")
    m = S.build_input(src, wins, cat)
    cy, _ = sf_read(cat)
    NF = max(1, int(round(float(getattr(S, "FADE_SEC", 0.005)) * sr)))
    # 人造一个"晚 10ms"的转换结果（实测那个偏差的反向），对齐必须能把它拉回来
    sh = int(round(0.010 * sr))
    late = os.path.join(tmp, "cat_late.wav")
    sf.write(late, np.concatenate([np.zeros(sh, dtype="float32"), cy]), sr,
             subtype="PCM_16")
    out_a = os.path.join(tmp, "align_on.wav")
    ra = S.scatter(late, m, dur, out_a)
    shifts = ra.get("shifts") or []
    check("对齐开着：每个窗口都报出约 +10ms 的偏移",
          bool(ra.get("aligned")) and len(shifts) == len(m["wins"])
          and all(0.005 <= abs(s) <= 0.02 for s in shifts),
          "shifts=%s（窗口数 %d）" % (shifts, len(m["wins"])))
    ya, _ = sf_read(out_a)
    ok = all(_close(ya[int(a * sr) + NF:int(b * sr) - NF],
                    x[int(a * sr) + NF:int(b * sr) - NF]) for a, b in segs)
    check("对齐开着：被推迟过的转换结果照样落回原位（逐点一致）", ok, "对不上")
    # ★ 负对照：关掉对齐**必须**歪。不歪说明素材没造出偏移，这条判据就是假的
    out_b = os.path.join(tmp, "align_off.wav")
    S.scatter(late, m, dur, out_b, align=False)
    yb, _ = sf_read(out_b)
    ok_off = all(_close(yb[int(a * sr) + NF:int(b * sr) - NF],
                        x[int(a * sr) + NF:int(b * sr) - NF]) for a, b in segs)
    check("★ 负对照：关掉对齐就必须歪（否则这条判据在骗人）", not ok_off,
          "关掉对齐居然也对得上 —— 素材没造出偏移")
    # 近乎静音的一段：不许硬掰（掰错了比不掰更糟）
    quiet = (np.zeros(int(3.0 * sr), dtype="float32") + 1e-7)
    s0, c0 = S.best_shift(quiet, quiet, sr)
    check("静音段不对齐（相关太低就返回 0，不硬掰）",
          s0 == 0.0, "shift=%s corr=%s" % (s0, c0))
    out_c = os.path.join(tmp, "align_nosrc.wav")
    rc2 = S.scatter(late, dict(m, src=os.path.join(tmp, "no_such.wav")),
                    dur, out_c)
    check("源轨读不到 ⇒ 跳过对齐且不崩（aligned=False）",
          rc2.get("aligned") is False, "aligned=%s" % rc2.get("aligned"))


def read(name):
    try:
        with open(os.path.join(ROOT, name), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _close(a, b):
    import numpy as np
    n = min(len(a), len(b))
    return n > 0 and bool(np.allclose(a[:n], b[:n], atol=2e-3))


def sf_read(p):
    import soundfile as sf
    import numpy as np
    y, sr = sf.read(p, dtype="float32", always_2d=False)
    return (y if y.ndim == 1 else y.mean(axis=1)), sr


if __name__ == "__main__":
    sys.exit(main())
