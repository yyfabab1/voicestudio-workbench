# -*- coding: utf-8 -*-
r"""门禁：多角色唱一首歌 —— 分配表必须完整无缝，拼接必须等响、不塌陷、无阶跃。

为什么单开一关：
    多角色对唱是"先各唱一整首、再按分配表拼起来"。听起来只是切片拼接，
    但有三处错了以后**听不出来、只能靠量**：

      ① 分配表留缝 —— 句与句之间的空隙没人认领，拼接时权重全是 0，
         那一段直接变成静音。人声断在换气处，听感是"被剪了一刀"。
      ② 不交叉淡化 —— 两条轨在接缝处硬切，波形一个样本跳满幅，
         出来的就是"啪"一声。
      ③ 不归一化 / 不等响 —— 两个角色 RVC 输出的音量本来就不一样，
         不拉平就是一句大一句小。

    这三件事静态检查全绿（字都在、类都在、语法也对），只有真的造两条
    已知信号拼一次才现形。所以本关用**合成信号**做硬指标：
    · DC ±1 测阶跃（段内差分为 0，接缝跳变无处可藏）
    · 同相正弦测"权重和为 1"（相位一致，交叉区若塌陷立刻掉 RMS）
    · 不同幅度正弦测响度对齐

本关不跑 RVC（不起显卡），只验数学与 CLI 冒烟。

退出码：0 全部通过；1 有失败。
"""
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, "stages")
sys.path.insert(0, HERE)
sys.path.insert(0, STAGES)

import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import multising as M  # noqa: E402

N_PASS = 0
N_FAIL = 0
FAILS = []


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("  [ok] %s" % name)
    else:
        N_FAIL += 1
        FAILS.append(name)
        print("  [FAIL] %s%s" % (name, ("  ← " + detail) if detail else ""))


def sec(t):
    print("\n" + t)


def rms(x):
    return float(np.sqrt(float(np.mean(np.asarray(x, dtype=np.float64) ** 2))))


# ---------------------------------------------------------------- 测试数据
# 25 句，中间夹一段 9 秒间奏（照真实识别结果的样子造的：真实那份就是
# 第 3 句结束 28.6s、第 4 句开始 37.9s）。用合成数据而不是读用户的缓存 ——
# 门禁不能依赖某个用户恰好认过某首歌。
def _fake_segs():
    # ★ 间隔必须**三档都有**，否则 phrase 和 section 判不出区别（踩过：
    #   第一版只有 0.15/0.6/9.0，结果两种模式都分成同样多的组，
    #   "section 不比 phrase 碎" 这条判据形同虚设）。
    #     0.15~0.6 秒 = 换气（两种都不该断）
    #     3.5 秒     = 乐句之间（phrase 断、section 不断）
    #     9.0 秒     = 间奏（两种都断）
    segs, t = [], 8.0
    for i in range(25):
        dur = 5.0 + (i % 4)
        segs.append({"start": round(t, 2), "end": round(t + dur, 2),
                     "text": "第%d句" % i})
        t += dur
        k = i % 5
        t += 9.0 if k == 4 else (3.5 if k == 2 else (0.6 if k == 1 else 0.15))
    return segs, round(t + 3.0, 2)


SEGS, DUR = _fake_segs()

SR = 44100


def _synth(n, hz, amp):
    t = np.arange(n, dtype=np.float32) / float(SR)
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def _dc(n, v):
    return np.full(n, v, dtype=np.float32)


# ---------------------------------------------------------------- A. 分配表
def a_plan():
    sec("A. 分配表（纯计算，不碰音频）")
    check("测试数据：25 句", len(SEGS) == 25, "%d" % len(SEGS))

    for mode in ("rotate", "phrase", "section"):
        takes, lr, deg = M.plan_takes(SEGS, DUR, 2, mode)
        cov = sum(t[1] - t[0] for t in takes)
        gap = sum(max(0.0, takes[i + 1][0] - takes[i][1])
                  for i in range(len(takes) - 1))
        check("%s：覆盖整条时间轴（%.2f / %.2f）" % (mode, cov, DUR),
              abs(cov - DUR) < 0.01, "差 %.4f" % (DUR - cov))
        check("%s：段间不留缝" % mode, gap < 1e-6, "缝 %.4f 秒" % gap)
        check("%s：不是退化方案" % mode, deg is False)
        check("%s：角色下标合法" % mode,
              all(0 <= int(t[2]) <= 1 for t in takes))

    # rotate 就是逐句轮流
    lr = M.assign_lines(SEGS, 2, "rotate")
    check("rotate：逐句轮流", lr == [i % 2 for i in range(len(SEGS))],
          str(lr[:8]))

    # phrase / section：组内不换人
    gs = M.group_lines(SEGS, M.GAP_PHRASE)
    gs2 = M.group_lines(SEGS, M.GAP_SECTION)
    lrp = M.assign_lines(SEGS, 2, "phrase")
    check("phrase：分出了组（%d 组 < %d 句）" % (len(gs), len(SEGS)),
          len(gs) < len(SEGS))
    check("phrase：一组之内不换人",
          all(len(set(lrp[i] for i in g)) == 1 for g in gs))
    check("phrase：组间轮流", len(set(lrp[g[0]] for g in gs)) == 2)
    check("section 的分组比 phrase 更整（%d < %d）" % (len(gs2), len(gs)),
          len(gs2) < len(gs), "section=%d phrase=%d" % (len(gs2), len(gs)))

    # ★ 必须走 plan_takes 那条路再测一遍：阈值是在 plan_takes 里选的，
    #   只测 assign_lines（用默认 gap）的话，把阈值改坏照样全绿（踩过一次）。
    _, lr_pt, _ = M.plan_takes(SEGS, DUR, 2, "phrase")
    _, lr_st, _ = M.plan_takes(SEGS, DUR, 2, "section")
    sw = lambda a: sum(1 for i in range(1, len(a)) if a[i] != a[i - 1])
    sw_pt, sw_st = sw(lr_pt), sw(lr_st)
    check("plan_takes(phrase) 与 assign_lines 同口径", lr_pt == lrp)
    check("plan_takes：phrase 换人 %d 次，远少于逐句的 %d 次"
          % (sw_pt, len(SEGS) - 1), sw_pt < (len(SEGS) - 1) // 2, "%d" % sw_pt)
    check("plan_takes：section 换人比 phrase 更少（%d < %d）" % (sw_st, sw_pt),
          sw_st < sw_pt, "section=%d phrase=%d" % (sw_st, sw_pt))

    # manual
    man = [0] * 25
    man[3] = man[4] = 1
    lrm = M.assign_lines(SEGS, 2, "manual", manual=man)
    check("manual：按给的下标", lrm[3] == 1 and lrm[4] == 1 and lrm[0] == 0)
    check("manual：越界下标被夹住",
          M.assign_lines(SEGS, 2, "manual", manual=[99] * 25)[0] == 1)
    # ★★ 契约又改了（2026-10-24 第五轮）：给短了**按时间对齐**，不是拒绝。
    #   上一版（2026-10-23）的规则是"必须拒绝"，理由是旧行为会静默补齐
    #   （后半首一个人独唱、不报错）。但拒绝被老板的真实场景推翻了：
    #   他按 14 段点好一份表，认完词变成 86 句 ⇒ 任务直接失败、14 次点击全废，
    #   还得在 86 行里重点一遍。**拒绝不是"不静默"，是把他赶走。**
    #   他点的是"那**一段时间**归谁" —— 句数变了，时间没变 ⇒ 照时间落回去，
    #   他点的"第 3 段合唱"仍然落在 00:24–00:37（实测 23.9–35.7 秒）。
    #   ⇒ 这里只留"不许静默补齐"那条底线：**最后一份**给 1、其余给 0，
    #     对齐后拿到 1 的必须是**时间上靠后**的那几句；
    #     老的补齐写法会让第 22 份之后全部沿用 1 ⇒ 中间那些句也被染成 1 ⇒ 红。
    _short = [0] * 5
    _short[-1] = 1
    _lrs = M.assign_lines(SEGS, 2, "manual", manual=_short)
    _mids = [(s["start"] + s["end"]) / 2.0 for s in SEGS]
    _hi = min(s["start"] for s in SEGS) + 0.8 * (
        max(s["end"] for s in SEGS) - min(s["start"] for s in SEGS))
    _got = [i for i, v in enumerate(_lrs) if int(v) == 1]
    check("manual：给短了按**时间**对齐（不许拒绝、不许用最后一个补齐）",
          bool(_got) and all(_mids[i] >= _hi - 1e-6 for i in _got),
          "拿到 1 的是第 %s 句（时间 %s）"
          % (_got[:6], [round(_mids[i], 1) for i in _got][:6]))
    _inf = {}
    M.assign_lines(SEGS, 2, "manual", manual=_short, info=_inf)
    check("manual：对齐这件事记进 info（调用方要说出来）",
          (_inf.get("remap") or {}).get("from") == 5, str(_inf.get("remap")))
    # ★ 没给 ⇒ **不许**全给第一个人（铁律 22）。
    #   旧行为 `[0]*n` 让预览显示「苹果嘉儿 174.46 秒 / 1 段，云宝 0 秒 / 0 段」，
    #   而界面上一句都还没点 —— 一份看着正常的独唱表，是最坏的失效形态。
    _lr0 = M.assign_lines(SEGS, 2, "manual", manual=None)
    check("manual：没给就退回**逐句轮流**（不许整首一个人）",
          set(_lr0) == {0, 1}, "实际 %s" % sorted(set(_lr0)))
    _i0 = {}
    M.assign_lines(SEGS, 2, "manual", manual=None, info=_i0)
    check("manual：没给要记进 info（调用方要说清这是替他排的）",
          _i0.get("manual_empty") is True, str(_i0))

    # 边界
    check("单角色：只一段且覆盖完整",
          len(M.plan_takes(SEGS, DUR, 1, "rotate")[0]) == 1)
    t0 = M.plan_takes(SEGS, DUR, 1, "rotate")[0]
    check("单角色：覆盖 = 时长",
          abs(sum(x[1] - x[0] for x in t0) - DUR) < 0.01)
    _, _, deg0 = M.plan_takes([], DUR, 2, "rotate")
    check("没歌词时标记为退化方案", deg0 is True)
    tev = M.plan_takes([], DUR, 2, "rotate")[0]
    check("退化方案也覆盖完整",
          abs(sum(x[1] - x[0] for x in tev) - DUR) < 0.01)
    check("退化方案也轮流", len(set(int(x[2]) for x in tev)) == 2)
    check("时长 0 → 空表", M.plan_takes(SEGS, 0, 2, "rotate")[0] == [])

    # 统计
    st = M.takes_stats([(0.0, 2.0, 0), (2.0, 4.0, 1), (4.0, 5.0, 0)], 2)
    check("统计：角色 0 共 3.0 秒 / 2 段",
          abs(st[0]["sec"] - 3.0) < 1e-6 and st[0]["parts"] == 2,
          str(st[0]))
    check("统计：角色 1 共 2.0 秒 / 1 段",
          abs(st[1]["sec"] - 2.0) < 1e-6 and st[1]["parts"] == 1,
          str(st[1]))


# ---------------------------------------------------------------- B. 拼接
TAKES = [(0.0, 2.0, 0), (2.0, 4.0, 1), (4.0, 5.0, 0)]


def b_blend():
    sec("B. 拼接：无缝 / 等响 / 不塌陷")
    n = int(SR * 5.0)

    # ---- B1. DC ±1：段内差分恒为 0，接缝跳变无处可藏 ----
    dc_a, dc_b = _dc(n, 1.0), _dc(n, -1.0)
    y_ok, st_ok = M.blend([dc_a, dc_b], TAKES, SR, 20.0)
    y_no, _ = M.blend([dc_a, dc_b], TAKES, SR, 0.001)
    d_ok = np.abs(np.diff(y_ok))
    d_no = np.abs(np.diff(y_no))
    i1 = int(2.0 * SR)
    i2 = int(4.0 * SR)
    j_ok = max(d_ok[i1 - 600:i1 + 600].max(), d_ok[i2 - 600:i2 + 600].max())
    j_no = max(d_no[i1 - 600:i1 + 600].max(), d_no[i2 - 600:i2 + 600].max())
    check("DC：段内完全平坦（最大差分 %.6f）" % d_ok[:int(1.9 * SR)].max(),
          d_ok[:int(1.9 * SR)].max() < 1e-6)
    check("DC：接缝无阶跃（%.5f < 0.02）" % j_ok, j_ok < 0.02, "%.5f" % j_ok)
    check("DC：负对照——不淡化时接缝跳满幅（%.3f > 1.0）" % j_no, j_no > 1.0,
          "%.5f" % j_no)
    check("DC：淡化把跳变压了 %.0f 倍（>50）" % (j_no / max(1e-9, j_ok)),
          j_no / max(1e-9, j_ok) > 50)

    # ---- B2. 同相正弦：交叉区不该塌陷（权重和恒为 1）----
    sa = _synth(n, 440.0, 0.5)
    y2, _ = M.blend([sa, sa.copy()], TAKES, SR, 20.0)
    inner = rms(y2[int(0.4 * SR):int(1.6 * SR)])
    cross = rms(y2[int(2.0 * SR) - 200:int(2.0 * SR) + 200])
    check("同相正弦：交叉区 RMS 与段内持平（%.5f vs %.5f）" % (cross, inner),
          abs(cross / max(1e-9, inner) - 1.0) < 0.02,
          "比值 %.4f" % (cross / max(1e-9, inner)))

    # ---- B3. 不同响度：段内必须被拉平 ----
    sb = _synth(n, 220.0, 1.0)
    y3, st3 = M.blend([sa, sb], TAKES, SR, 20.0)
    ra = rms(y3[int(0.3 * SR):int(1.7 * SR)])
    rb = rms(y3[int(2.3 * SR):int(3.7 * SR)])
    check("不同幅度：两段被拉平（%.5f vs %.5f）" % (ra, rb),
          abs(ra / max(1e-9, rb) - 1.0) < 0.03,
          "比值 %.4f" % (ra / max(1e-9, rb)))
    check("响度增益算对了（0.5→2.0，1.0→1.0）",
          st3["gains"][0] > 1.9 and abs(st3["gains"][1] - 1.0) < 0.01,
          str(st3["gains"]))

    # ★ 这个用例专门用来区分「按各自分到的那几段对齐」和「按整轨对齐」：
    #   B 轨绝大部分很响（0.9），只在它被分配的那一秒很轻（0.1）。
    #   整轨法会把 B 的 loud 部分当成它的"音量水平"，于是把它压得几乎听不见；
    #   按区间法才知道"它要唱的那一秒本来就是轻的"。真实数据里就是这样：
    #   同一个模型两个音高，整轨 RMS 只差 7.9%，各自负责的半段差了 46%。
    n2 = int(SR * 5.0)
    qa = _synth(n2, 440.0, 0.25)                      # A 全程 0.25，唱 0~4s
    qb = np.concatenate([_synth(int(SR * 4.0), 220.0, 0.9),
                         _synth(n2 - int(SR * 4.0), 220.0, 0.1)])   # B 唱 4~5s
    y4, st4 = M.blend([qa, qb], [(0.0, 4.0, 0), (4.0, 5.0, 1)], SR, 20.0)
    ra4 = rms(y4[int(0.5 * SR):int(3.5 * SR)])
    rb4 = rms(y4[int(4.2 * SR):int(4.8 * SR)])
    check("响度按「各自分到的那几段」对齐（%.4f vs %.4f）" % (ra4, rb4),
          abs(ra4 / max(1e-9, rb4) - 1.0) < 0.10,
          "比值 %.2f（按整轨对齐会到 8 倍）" % (ra4 / max(1e-9, rb4)))

    # ---- B4. 基本契约 ----
    check("长度守恒", len(y3) == n, "%d vs %d" % (len(y3), n))
    check("统计里采样率正确", st3["sr"] == SR)
    check("统计里接缝数 = 2", st3["seams"] == 2, str(st3["seams"]))
    check("统计里有每个角色的秒数",
          len(st3["per_role"]) == 2 and abs(st3["per_role"][0]["sec"] - 3.0) < 0.01)


# ---------------------------------------------------------------- C. 负对照
def c_negative():
    sec("C. 负对照：证明「完整划分」这个约束不是空话")
    n = int(SR * 5.0)
    sa = _synth(n, 440.0, 0.5)
    sb = _synth(n, 220.0, 1.0)

    # 完整划分（正常）
    y_ok, _ = M.blend([sa, sb], TAKES, SR, 20.0)
    # 人为留缝：2.0~2.5 谁都不唱
    gap_takes = [(0.0, 2.0, 0), (2.5, 5.0, 1)]
    y_gap, _ = M.blend([sa, sb], gap_takes, SR, 20.0)

    win = slice(int(2.1 * SR), int(2.4 * SR))
    r_ok, r_gap = rms(y_ok[win]), rms(y_gap[win])
    inner = rms(y_ok[int(0.3 * SR):int(1.7 * SR)])
    check("完整划分：缝隙窗口有声音（%.5f）" % r_ok, r_ok > 0.5 * inner,
          "%.5f vs 段内 %.5f" % (r_ok, inner))
    check("负对照：留缝那段塌成静音（%.6f）" % r_gap, r_gap < 0.01 * inner,
          "%.6f" % r_gap)
    check("两者相差 %.0f 倍（>20）" % (r_ok / max(1e-9, r_gap)),
          r_ok / max(1e-9, r_gap) > 20)


# ---------------------------------------------------------------- D. CLI 冒烟
def d_cli():
    sec("D. CLI 冒烟（真实调用路径：studio 也是这么调的）")
    py = os.path.join(r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版"
                      r"\GPT-SoVITS-v5-20261005nvidia50x0"
                      r"\GPT-SoVITS-v2pro-20261006nvidia50x0",
                      "runtime", "python.exe")
    if not os.path.isfile(py):
        check("找到 GSV runtime 解释器", False, py)
        return
    tmp = tempfile.mkdtemp(prefix="vms_")
    n = int(SR * 4.0)
    sf.write(os.path.join(tmp, "a.wav"), _synth(n, 440.0, 0.5), SR)
    sf.write(os.path.join(tmp, "b.wav"), _synth(n, 220.0, 1.0), SR)
    plan = {"duration": 4.0, "takes": [(0.0, 2.0, 0), (2.0, 4.0, 1)]}
    pj = os.path.join(tmp, "plan.json")
    json.dump(plan, open(pj, "w"))
    out = os.path.join(tmp, "out.wav")
    rp = os.path.join(tmp, "rep.json")
    r = subprocess.run([py, "-s", os.path.join(STAGES, "multising.py"),
                        "--plan", pj, "--out", out, "--report", rp,
                        "--track", os.path.join(tmp, "a.wav"),
                        "--track", os.path.join(tmp, "b.wav")],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=300)
    check("CLI 退出码 0", r.returncode == 0, (r.stdout or "")[-300:])
    check("CLI 打印了 MULTI_DONE", "MULTI_DONE" in (r.stdout or ""))
    if os.path.isfile(out):
        y, sr = sf.read(out, dtype="float32")
        check("CLI 产物时长 4.0 秒", abs(len(y) / float(sr) - 4.0) < 0.01,
              "%.3f" % (len(y) / float(sr)))
    else:
        check("CLI 产物存在", False)
    if os.path.isfile(rp):
        rep = json.load(open(rp, encoding="utf-8"))
        check("report 字段齐全",
              all(k in rep for k in ("sr", "secs", "tracks", "seams",
                                     "gains", "per_role")),
              str(sorted(rep.keys())))
    else:
        check("report 生成", False)
    # 单声道输入不能把内存炸掉（踩过：x 是 (n,1)，乘 mask 广播成 (n,n)）
    check("拼接产物是一维", y.ndim == 1, "ndim=%d" % y.ndim)


def _func_src(src, name):
    """取某个**模块级函数**的源码文本。找不到返回空串。

    为什么不用 "字串在不在"：上一关被一个 `if False:` 的变异当场骗过 ——
    判据只查字串时，"把逻辑删掉但注释还留着"也能绿。取函数体就干净得多。
    """
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return ""
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n) or ""
    return ""


def _calls_in(src, name):
    """某个模块级函数体里**真的调用了**哪些函数（只算 ast.Call，不算注释/字符串）。

    必须这样：上一版用 `"qc_singing" not in 函数源码` 来判断"有没有做质检"，
    结果 sing_multi 的 docstring 里写着"这版**不做** qc_singing"——
    判据把这句话也当成了调用，直接误报。判据要查行为，不查文字。
    """
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    out = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            for c in ast.walk(n):
                if isinstance(c, ast.Call):
                    f = c.func
                    if isinstance(f, ast.Name):
                        out.add(f.id)
                    elif isinstance(f, ast.Attribute):
                        out.add(f.attr)
    return out


def b2_overlay():
    sec("B2. 叠唱（主唱 + 和声）")
    n = int(SR * 5.0)
    lead = _synth(n, 440.0, 0.5)           # 主唱
    harm = _synth(n, 440.0, 0.5)           # 同相和声，方便验"叠了几倍"
    y, stt = M.overlay([lead, harm], SR, 0.55)
    check("叠唱：时长守恒", len(y) == n, "%d" % len(y))
    check("叠唱：模式标成 chorus", stt.get("mode") == "chorus")
    # ★★ 2026-10-24 第五轮：这条判据的前提被推翻了。
    #   "叠完正好 1 + 0.55 = 1.55 倍"只在**两层完全同相**时成立 —— 那意味着
    #   和声层就是主唱的复制品，也就是老板说的「简单的拼接」。
    #   加了时间错位之后，两层不再完全相关，总能量必然小于 1.55：
    #     完全同相 ⇒ 1.55 ｜ 完全不相关 ⇒ sqrt(1+0.55²) = 1.14
    #   实测 1.408 —— 正好落在中间，说明"叠上去了、但不是复制品"。
    #   ⇒ 判据改成守这个区间，并且要求它**明显低于 1.55**。
    ratio = rms(y) / max(1e-9, rms(lead))
    check("★ 叠唱：倍数落在「叠上去了、但不是复制」的区间"
          "（完全同相才是 1.55 —— 那等于把主唱复制一遍；实测 %.3f）" % ratio,
          1.20 <= ratio <= 1.50, "%.4f（1.20~1.50）" % ratio)
    check("叠唱：不归一化（主唱没被稀释）", ratio > 1.2, "%.3f" % ratio)
    # ★ 直接量"两层的相关性"：这才是"是不是复制品"的真正尺子。
    #   完全同相 = 1.0；错位之后必然掉下来。
    _a = (lead - lead.mean()) / max(1e-12, np.std(lead))
    _b = (y - y.mean()) / max(1e-12, np.std(y))
    _c = float(abs(np.dot(_a, _b) / max(1e-12, len(_a))))
    check("★★ 和声层不是主唱的复制品（相关系数 %.3f < 0.97）" % _c,
          _c < 0.97, "%.4f（越接近 1 越像复制）" % _c)

    # 峰值保护：两个满幅信号叠加会超 1，必须整体缩而不是削顶
    loud = np.full(n, 0.9, dtype=np.float32)
    y2, st2 = M.overlay([loud, loud], SR, 0.55)
    check("叠唱：接近削顶时整体缩放（原始 %.2f → 缩放后 %.3f < 0.99）"
          % (st2.get("peak", 0), float(np.abs(y2).max())),
          float(np.abs(y2).max()) < 0.995, "%.4f" % float(np.abs(y2).max()))
    check("叠唱：真的触发了峰值保护", st2.get("peak_guard") is True)
    check("叠唱：缩放是等比的（波形没变形）",
          abs(float(np.abs(y2).max()) - 0.99) < 0.01, "%.4f" % float(np.abs(y2).max()))

    # 负对照：两条 0.7 幅度的轨直接相加 = 1.4，必削顶（证明峰值保护不是摆设）
    hot = _synth(n, 440.0, 0.7)
    raw = hot + hot
    check("负对照：不加和声音量直接相加会超 1（证明峰值保护不是摆设）",
          float(np.abs(raw).max()) > 1.0, "%.3f" % float(np.abs(raw).max()))


def e_contract():
    sec("E. 源码契约（入口真的接上了，而不是只写了个函数没人调）")
    st = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    sv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()

    check("studio.MULTI_MODES 四种分配方式都在",
          all(k in st for k in ('"rotate"', '"phrase"', '"section"', '"manual"')))

    sm = _func_src(st, "sing_multi")
    calls = _calls_in(st, "sing_multi")
    check("studio.sing_multi 存在", bool(sm.strip()))
    check("sing_multi 调了共用的 RVC 推理入口", "rvc_convert" in calls, str(sorted(calls))[:200])
    check("sing_multi 真的调了拼接脚本", "multising.py" in sm)
    check("sing_multi 用了歌词时间戳", "lyric_segments_any" in calls)
    check("sing_multi 有「没歌词就退化」的明确提示（degraded）", "degraded" in sm)
    check("sing_multi 至少要求两个角色", "至少要选两个角色" in sm)
    # ★ 这一条的口径在 2026-10-20 改过，理由写清楚：
    #   以前是"多角色**不做**音准质检"，因为混在一起比 f0 没意义。现在改成
    #   **逐人取样**（每人只在自己负责的那段里比），这个数是有意义的 ——
    #   所以新的约束不是"不许质检"，而是"不许拿混好的成品整曲比"。
    check("sing_multi 没拿混好的成品整曲比 f0",
          "qc_singing_whole" not in calls and "qc_singing" not in calls,
          str(sorted(calls))[:200])
    check("sing_multi 调了逐人取样体检", "multi_qc_roles" in calls)
    check("sing_multi 接上了高频补偿（与单角色同源）", "hf_brighten" in calls)
    check("sing_multi 会先把分句时间戳补上（没认过就自动认）",
          "ensure_lyric_times" in calls)
    check("hf 传了非法值按关处理", "if hf not in HF_LEVELS" in sm)
    check("MULTI_MODES 有叠唱这一档", '"chorus"' in st)
    check("叠唱走 --layer（不是拿分配表硬拼）", "--layer" in sm and "MULTI_CHORUS_GAIN" in sm)
    check("叠唱不分句（chorus 时不给 plan_takes 传 mode）",
          'mode == "chorus"' in sm)

    mq = _func_src(st, "multi_qc_roles")
    mcalls = _calls_in(st, "multi_qc_roles")
    check("逐人体检确实调了单窗质检", "qc_singing" in mcalls, str(sorted(mcalls))[:200])
    check("逐人体检传的是**取样窗口**，不是整曲",
          "start=st" in mq and "secs=secs" in mq)
    check("逐人体检会说明「段太短没量」而不是编个数", "段太短" in mq)
    check("逐人体检失败不许把成品带死", "except BaseException" in sm)

    check("成品参数里存了分轨清单（换法重拼的原料）", '"multi_tracks"' in sm)
    check("成品参数里存了任务 tag（找回分轨用）", '"multi_tag"' in sm)
    check("成品参数里存了分配表（能复现）", '"multi_takes"' in sm)
    check("成品参数里存了逐人体检结果", '"multi_qc"' in sm)

    sr_ = _func_src(st, "sing_role")
    check("sing_role 也走同一个 rvc_convert（不再自己拼命令行）",
          "rvc_convert(" in sr_ and "rvc_infer.py" not in sr_,
          "sing_role 里还有 %d 处 rvc_infer.py" % sr_.count("rvc_infer.py"))
    check("rvc_convert 是模块级函数（两处才能共用）",
          bool(_func_src(st, "rvc_convert").strip()))

    check("server 有 sing_multi 分支", 'kind == "sing_multi"' in sv)
    check("server 挡了不认识的分配方式",
          "MULTI_MODES" in sv and "不认识的分配方式" in sv)
    check("server 挡了少于两个角色", "至少要选两个角色" in sv)

    check("界面有唱法开关", 'id="selMulti"' in html)
    check("界面有分配方式下拉", 'id="selMultiMode"' in html)
    check("界面有角色勾选区", 'id="multiRoles"' in html)
    check("前端调的是 sing_multi 接口", 'kind: "sing_multi"' in js)
    check("前端只列能唱的角色（有 RVC 模型）", "r.rvc && r.rvc.model" in js)
    # ★ 2026-10-22 改法：原来查的是**文案原文**「多人合唱不做小样」，理由写的是
    #   "要 N 倍整首推理" —— 改成每人只唱分到的那几句之后，这个理由已经不成立，
    #   文案跟着改，于是这条判据假红了。判据该守的是**行为**（multi 分支遇到
    #   preview 直接拦回、不做），不是某句话怎么措辞。
    import re as _re
    no_preview = _re.search(
        r"if \(preview\)\s*\{\s*toast\([^;]*?小样[^;]*?;\s*return;\s*\}\s*\n\s*return doSingMulti",
        js)
    check("前端不给多人合唱做小样（multi 分支遇到 preview 直接拦回，没往下走）",
          no_preview is not None,
          "没找到「if (preview) {... toast(...小样...); return;} + return doSingMulti」")


# ---------------------------------------------------------------- F. 逐人取样
def f_sampling():
    sec("F. 逐人取样：质检只看「这个人负责的那一段」")
    t1 = [(0.0, 2.0, 0), (2.0, 4.0, 1), (4.0, 5.0, 0)]

    sp0 = M.role_spans(t1, 0)
    check("role_spans：同一角色的相邻段合并（2 段，不是 3 段）", len(sp0) == 2, str(sp0))
    check("role_spans：区间对得上", sp0 == [(0.0, 2.0), (4.0, 5.0)], str(sp0))
    # ★ 上面那两句**证明不了"合并"**：t1 里角色 0 的两段本来就隔着 2 秒。
    #   真正的合并要用首尾相接的碎段来测（踩过：变异成"永不合并"照样全绿）。
    t2 = [(0.0, 1.0, 0), (1.0, 2.0, 0), (2.0, 3.0, 1), (3.0, 4.0, 0)]
    sp2 = M.role_spans(t2, 0)
    check("role_spans：首尾相接的碎段被合并成一段", sp2 == [(0.0, 2.0), (3.0, 4.0)], str(sp2))
    check("role_spans：不合并的话角色 0 会是 3 段（负对照）",
          len([t for t in t2 if t[2] == 0]) == 3 and len(sp2) == 2,
          "原始 %d 段 → 合并后 %d 段"
          % (len([t for t in t2 if t[2] == 0]), len(sp2)))

    sk0 = M.role_skips(t1, 0, 5.0)
    check("role_skips：角色 0 只跳过 [2,4]", sk0 == [(2.0, 4.0)], str(sk0))
    sk1 = M.role_skips(t1, 1, 5.0)
    check("role_skips：角色 1 跳过首尾两段",
          sk1 == [(0.0, 2.0), (4.0, 5.0)], str(sk1))
    # 补集必须"不重不漏"：跳过段 + 自己唱的时间 = 全曲
    own = sum(e - s for s, e in M.role_spans(t1, 0))
    skip = sum(e - s for s, e in sk0)
    check("role_skips：跳过 + 自己唱 = 全曲（%.2f + %.2f = 5.00）" % (skip, own),
          abs(own + skip - 5.0) < 0.01, "%.3f" % (own + skip))
    check("role_skips：全部段都归同一个人时没有跳过段",
          M.role_skips([(0.0, 5.0, 0)], 0, 5.0) == [])
    check("role_skips：没被分配到的角色整曲都跳过",
          M.role_skips(t1, 7, 5.0) == [(0.0, 5.0)])

    st0, secs0 = M.longest_span(t1, 0)
    check("longest_span：取最长那段（2.0 秒，不是 1.0）",
          abs(secs0 - 2.0) < 1e-6 and abs(st0 - 0.0) < 1e-6,
          "%.2f @ %.2f" % (secs0, st0))
    check("longest_span：长度封顶（60 秒的段只取 30 秒）",
          abs(M.longest_span([(0.0, 60.0, 0)], 0)[1] - 30.0) < 1e-6)
    check("longest_span：没唱 → (0, 0)", M.longest_span(t1, 9) == (0.0, 0.0))


# ---------------------------------------------------------------- G. 高频 / 歌词
def g_pipeline():
    sec("G. 两个「设了没生效」的坑：高频补偿与分句时间戳")
    import ast
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    sv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    sm = _func_src(src, "sing_multi")
    calls = _calls_in(src, "sing_multi")

    check("studio 有独立的 hf 档位表（与单角色同一份）",
          "HF_LEVELS" in src and "HF_FREQ" in src)
    check("多角色转换后会补高频（hf_brighten 真的被调）",
          "hf_brighten" in calls, str(sorted(calls))[:200])
    # ★ 别绑字面量：`hf_brighten(job, out_wav, vocal_wav` 这种写法，一有人把
    #   第一个参数换成变量（seg-only 模式要传转换输出）它就红 —— 红的是尺子
    #   不是回归。这里守的是**语义**：第 3 个参数必须是源干声（vocal_wav，
    #   或 seg-only 下的那条拼接源轨 _ref），绝不能是 RVC 自己的输出。
    #   （_ref 具体指向谁，由 verify_segonly 的接线判据守。）
    #   ★★ 光看**变量名**会造出假尺子：调用处写的是 `_ref`，可 `_ref` 指向谁
    #   不看赋值根本不知道（实测：`_ref = conv_out` 时这条判据照样全绿 ——
    #   拿 RVC 自己的输出去补高频，静音段会凭空冒出原唱气声，它一声不吭）。
    #   ⇒ 变量就追到赋值那一行，确认它只能是源干声。
    import re as _re_hf
    _m = _re_hf.search(r"hf_brighten\(job,\s*(\w+),\s*(\w+),", sm)
    _ok = bool(_m) and _m.group(1) != _m.group(2)
    if _ok and _m.group(2) == "_ref":
        _a = _re_hf.search(r"_ref\s*=\s*([^\n]+)", sm)
        _ok = bool(_a) and "seg_src" in _a.group(1) and "vocal_wav" in _a.group(1)
        _why = ("_ref 指向 %s" % _a.group(1).strip()) if _a else "找不到 _ref 赋值"
    elif _ok:
        _ok = _m.group(2) == "vocal_wav"
        _why = "第 3 参数是 %s" % _m.group(2)
    else:
        _why = ("实际 %s" % sm[_m.start():_m.end()]) if _m else "找不到 hf_brighten 调用"
    check("补高频用的是原唱干声（不是自己合成高频）", _ok, _why)
    check("补高频失败不会让整首歌失败（失败降级有日志）",
          "已补高频" in sm or "未补偿" in sm or "hf_fc" in sm)
    # ★ 判据不写死兜底值（以前写死 "off"，一改默认档就假红 —— 判据里的字面量
    #   就是技术债）。改成：透传次数够 + 兜底的那个值必须是真档位。
    _n = sv.count('hf=(body.get("hf") if body.get("hf") in studio.HF_LEVELS else')
    check("server 把 hf 透传给多角色（不是只给单人）", _n >= 2, "出现 %d 次" % _n)
    _i = src.find("HF_LEVELS = (")
    _lv = src[_i:src.find(")", _i)] if _i >= 0 else ""
    _fb = [x for x in ("auto", "off", "light", "mid") if 'else "%s"' % x in sv]
    check("★ 兜底档位是真档位（写错就永远是那一档，用户改不动）",
          bool(_fb) and all('"%s"' % x in _lv for x in _fb),
          "兜底 %s / 档位表 %s" % (_fb, _lv[:60]))
    check("server 透传了 auto_lyric（能关）", 'body.get("auto_lyric"' in sv)

    el = _func_src(src, "ensure_lyric_times")
    check("有「没认过就自动认一次」的函数", bool(el.strip()))
    check("认不出来时返回 failed（不是静默当空）", '"failed"' in el)
    check("自动识别失败不抛（多角色还能按均分跑）", "except BaseException" in el)
    check("sing_multi 里 degraded 时明说按时间均分", "按时间均分" in sm)


# ---------------------------------------------------------------- H. 手动分配
def h_manual():
    sec("H. 手动指定（界面能真的逐句点人，而不是只有个 API）")
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    sv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
    html = open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()

    pv = _func_src(src, "multi_plan_preview")
    pcalls = _calls_in(src, "multi_plan_preview")
    check("有分配表预览函数（不跑推理）", bool(pv.strip()))
    check("预览真的走 plan_takes（与正式跑同一套分句）", "plan_takes" in pcalls)
    check("预览返回逐句的角色（manual 编辑器要用）",
          '"role"' in pv and '"text"' in pv)
    check("预览在没歌词时也给出可看的表（lyric_ready=False）",
          '"lyric_ready"' in pv)
    check("没歌词时明说「不能手动排」", '"can_manual"' in pv)

    check("server 有 /api/multi/plan（同步接口，不建任务）",
          'path == "/api/multi/plan"' in sv)
    check("server 校验分配方式", "不认识的分配方式" in sv)
    check("手动模式在界面的下拉里", '<option value="manual"' in html)
    check("界面有逐句列表容器", 'id="multiLines"' in html)
    check("界面有分配表汇总（谁唱多少秒）", 'id="multiPlanSum"' in html)
    check("前端会去拉分配表", "/api/multi/plan" in js)
    check("手动模式逐句收集下标（select.msel）", "select.msel" in js)
    check("手动模式没句子时明确拦下（不许静默全给第一个人）",
          "要先把歌词认出来" in js)
    check("「重排看看」按钮存在", 'id="btnMultiPlan"' in html)


# ---------------------------------------------------------------- I. 换法重拼
def i_resplit():
    sec("I. 换一种分配方式重拼（不重跑推理）")
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    sv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()

    rs = _func_src(src, "sing_multi_resplit")
    rc_ = _calls_in(src, "sing_multi_resplit")
    check("有重拼函数", bool(rs.strip()))
    check("重拼**不调** RVC 推理（不然就不省时间了）",
          "rvc_convert" not in rc_, str(sorted(rc_))[:200])
    check("重拼用的是 multising 的拼接", 'os.path.join(STAGES, "multising.py")' in rs)
    check("重拼会重新混伴奏", "_multi_mix" in rc_)
    check("重拼要求上一版留了分轨（旧成品给明确说法）",
          "没留下每个人的分轨" in rs)
    check("重拼不接受叠唱（它没有分配表）", "叠唱版本没有" in rs)
    check("重拼的成品记着「从哪一版重排的」", "multi_resplit_from" in rs)
    check("server 有 multi_resplit 分支", 'kind == "multi_resplit"' in sv)
    check("前端有多角色的成品卡（逐人体检 + 重拼入口）", "function multiCard" in js)
    check("前端有「换法重拼」按钮", "换法重拼" in js)
    check("前端重拼走 multi_resplit", 'kind: "multi_resplit"' in js)


def j_group_split():
    sec("J. 组数 < 人数：不许有人一句都唱不到（真浏览器实测出来的缺陷）")
    # 真实场景（端到端跑出来的）：16 秒片段、4 句、句间空档都不到 2 秒
    # ⇒ group_lines 只给出 **1 组** ⇒ 按"一组一个人"分 ⇒ 第二个人 0 秒，
    #   用户明明选了对唱，听到的却是独唱。这不是理论问题，是成品里量出来的。
    s4 = [{"start": i * 4.0, "end": i * 4.0 + 3.6} for i in range(4)]
    for mode in ("phrase", "section"):
        info = {}
        takes, lr, deg = M.plan_takes(s4, 16.0, 2, mode=mode, info=info)
        per = {}
        for a, b, k in takes:
            per[int(k)] = per.get(int(k), 0.0) + (b - a)
        check("%s：两个人都分到时间了（各 %.1f / %.1f 秒）"
              % (mode, per.get(0, 0), per.get(1, 0)),
              per.get(0, 0) > 1.0 and per.get(1, 0) > 1.0, str(per))
        check("%s：info 说清了「原本只有 1 组、已拆成 2 组」" % mode,
              info.get("groups_before") == 1 and info.get("groups") == 2
              and info.get("split") is True, str(info))
        # 切点必须落在句边界上：句子的起点/终点 + 首尾
        _edges = [0.0, 16.0] + [g["start"] for g in s4] + [g["end"] for g in s4]
        _bx = [t[0] for t in takes] + [t[1] for t in takes]
        check("%s：拆的切点落在句与句之间（没把一句话劈开）" % mode,
              all(any(abs(x - v) < 1e-6 for v in _edges) for x in _bx),
              str(takes))
    # 负对照：组本来够，就不许动它（"总是对半拆"同样是错的）
    s8 = [{"start": i * 7.0, "end": i * 7.0 + 3.0} for i in range(8)]
    info = {}
    takes8, _lr, _d = M.plan_takes(s8, 60.0, 2, mode="phrase", info=info)
    check("组数已经够时**不拆**（8 组还是 8 组）",
          info.get("groups") == 8 and info.get("split") is False, str(info))
    # 拆不动的情形：句数 < 人数 —— 物理上就是有人唱不到，必须**照实报**
    s2 = [{"start": 0.0, "end": 3.6}, {"start": 4.0, "end": 7.6}]
    info = {}
    takes2, _lr, _d = M.plan_takes(s2, 8.0, 3, mode="phrase", info=info)
    check("句数 < 人数：拆不动就照实标 short（不伪造第三组）",
          info.get("short") is True and info.get("groups") == 2, str(info))
    check("句数 < 人数：第三个人确实是 0 段（数据不撒谎）",
          all(int(t[2]) != 2 for t in takes2), str(takes2))
    # rotate / manual 不受影响
    info = {}
    tr_, lr_, _d = M.plan_takes(s4, 16.0, 2, mode="rotate", info=info)
    check("rotate 仍然是逐句轮流（4 段，两人交替）",
          len(tr_) == 4 and [int(t[2]) for t in tr_] == [0, 1, 0, 1], str(tr_))
    check("rotate 时 info 不声称拆过分组", info.get("split") is False, str(info))

    # ---- 源码契约：这条修复是真的接进了链路，不是只加了个函数 ----
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    msrc = open(os.path.join(HERE, "stages", "multising.py"), encoding="utf-8").read()
    al = _func_src(msrc, "assign_lines")
    check("assign_lines 走 groups_for_roles（不是裸 group_lines）",
          "groups_for_roles" in al)
    sm = _func_src(src, "sing_multi")
    # 光有日志文案不够：得确认它**真的把 info 传进去了**，否则那段日志永远不触发
    # （文案还在、条件永假 —— 这种"看起来修好了"最容易骗过字串判据）。
    import ast as _ast2

    def _passes_info(snippet):
        try:
            tree = _ast2.parse(snippet)
        except SyntaxError:
            return False
        for n in _ast2.walk(tree):
            if isinstance(n, _ast2.Call) and isinstance(n.func, _ast2.Attribute) \
                    and n.func.attr == "plan_takes":
                if any(k.arg == "info" for k in n.keywords):
                    return True
        return False

    check("sing_multi 真的给 plan_takes 传了 info（日志才会触发）",
          _passes_info(sm))
    check("sing_multi 拿到分组信息并**当场说出来**（拆过组）",
          "已按句子对半拆成" in sm)
    check("sing_multi 对「拆不动」也有话说（不静默）", "拆到不能再拆" in sm)
    rs = _func_src(src, "sing_multi_resplit")
    check("换法重拼同样会说明拆组（不然换个分配方式又变独唱）",
          "保证每个人都唱得上" in rs)
    pv = _func_src(src, "multi_plan_preview")
    check("预览把「拆过组 / 有人唱不到」放进返回值（界面才看得见）",
          '"hint": hint2' in pv and '"short"' in pv)
    # ★ 预览是**纯函数**（没有 job 参数，跑在请求线程里）。它一旦写 job 日志就
    #   当场 NameError ⇒ /api/multi/plan 直接 500，界面预览永远是空的。
    #   本轮改这里时真的差点这么写，判据留着挡下一次。
    #   判据按 **ast 里的真调用** 判，不按字串：字串会被注释里的解释骗过
    #   （我自己那句"不能 log(job…)"就当场把这条判据弄红了 —— 尺子歪，不是产品坏）。
    import ast as _ast
    try:
        _tree = _ast.parse(pv)
        _logc = [_ast.unparse(n)[:60] for n in _ast.walk(_tree)
                 if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
                 and n.func.id == "log"]
        _okparse = True
    except SyntaxError as e:
        _logc, _okparse = [str(e)], False
    check("预览函数里**不许**真的调用 log(job, ...)（它没有 job）",
          _okparse and not _logc, str(_logc))


def k_shared_cache():
    sec("K. 人声轨在**共享缓存**里：被别人删掉时要说人话（不是「模型坏了」）")
    # 实测（2026-10-20）：两个服务同时活着时，第一个角色唱完后那份
    # data/cache/sep/<key>/vocal.wav 被另一个进程清掉，第二个角色当场
    # `[err] 输入不存在` —— 这句话读起来像角色模型坏了，实际模型一点事没有。
    # 这条判据盯的是"守卫在不在 + 话有没有说成人话"。
    src = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    sm = _func_src(src, "sing_multi")

    def has_guard(text):
        return ("for k, x in enumerate(roles):" in text
                and "if not os.path.isfile(vocal_wav):" in text)

    check("每个角色开跑前检查人声轨还在（共享缓存可能被别的进程清掉）",
          has_guard(sm))
    check("报错说清「模型没问题、重跑就行」（不然像模型坏了）",
          "模型没问题" in sm and "共享缓存" in sm)
    check("失败会标记这一步（任务中心里看得见是哪一步断的）",
          'end_step(job, False, "人声轨不见了")' in sm)
    # 负对照：把守卫去掉，has_guard 必须返回假 —— 否则这条判据只是"字符串在不在"
    bad = sm.replace("if not os.path.isfile(vocal_wav):", "if False:")
    check("负对照：守卫被去掉后 has_guard 会返回假",
          has_guard(bad) is False)


def l_phrase():
    """L. 原曲分句（2026-10-25 第九轮）

    老板：「多角色指定句子是按照原曲的分句来 并且带有中文」
    界面那份表原来直接是 ASR 的碎句（58~86 句）—— 它是按呼吸和标点切的，
    不是"一句歌词"。合并成乐句之后，界面对的和内部分的就是同一份句。
    """
    sec("L. 按原曲换气分句（不是 ASR 的碎句）+ 带中文")
    src = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()

    def _tone(n, sr, hz=220.0, amp=0.4):
        t = np.arange(n, dtype=np.float64) / float(sr)
        return (amp * (np.sin(2 * np.pi * hz * t)
                       + 0.5 * np.sin(2 * np.pi * hz * 3 * t))).astype(np.float32)

    def _quiet(n):
        return np.zeros(n, dtype=np.float32)

    sr = 16000
    tmp = os.path.join(tempfile.gettempdir(), "vs_phrase")
    os.makedirs(tmp, exist_ok=True)
    # 造一条"唱 3 秒 → 停 0.8 秒 → 唱 3 秒 → 停 0.8 秒 → 唱 3 秒"的人声
    blocks = []
    t = 0.0
    for k in range(3):
        blocks.append(_tone(int(3.0 * sr), sr))
        if k < 2:
            blocks.append(_quiet(int(0.8 * sr)))
    y = np.concatenate(blocks)
    wav = os.path.join(tmp, "vocal.wav")
    sf.write(wav, y, sr)

    # ASR 碎句：每 1.5 秒一句（6 句），横跨了那两个 0.8 秒的静音
    segs = [{"start": 0.0 + i * 1.5, "end": 1.5 + i * 1.5,
             "text": "sentence %d" % i, "zh": "第 %d 句" % i}
            for i in range(6)]
    # 注意：发声段是 0~3 / 3.8~6.8 / 7.6~10.6
    #   ASR 句边界在 1.5/3.0/4.5/6.0/7.5/9.0 —— 其中 3.0 和 7.5 落在静音里

    got = M.phrase_segs(segs, wav, min_gap=0.45)
    check("★ 按原曲的静音断句：6 个 ASR 碎句被合成 2~3 句（不是 6 句原样返回）",
          2 <= len(got) <= 4, "%d 句" % len(got))
    check("★ 断句点落在真的静音上（不是硬切）",
          all(abs(g["start"] - 3.8) < 0.9 or abs(g["start"] - 7.6) < 0.9
              or g["start"] < 0.1 for g in got[1:]),
          str([round(g["start"], 2) for g in got]))
    check("★ 中文会跟着拼起来（老板要「带有中文」）",
          all("第" in (g.get("zh") or "") for g in got),
          str([g.get("zh") for g in got]))
    check("★ 原文也会拼起来（界面 title 里要能看到整句）",
          all("sentence" in (g.get("text") or "") for g in got))
    check("★ 每句记了它由几个碎句合来（界面要标，不然用户以为吞了句子）",
          all(int(g.get("n_sub") or 0) >= 1 for g in got)
          and sum(int(g.get("n_sub") or 0) for g in got) == len(segs),
          str([g.get("n_sub") for g in got]))
    check("★ 句与句之间不留缝（上一句 end = 下一句 start 的组边界连续）",
          all(g["end"] > g["start"] for g in got))

    # 没有人声轨 ⇒ 退化成按句间时间间隔，不能崩、也不能合并错
    g2 = M.phrase_segs(segs, None)
    check("拿不到人声轨时退化成按间隔分句（不崩，且仍然是合法句表）",
          bool(g2) and all(g["end"] > g["start"] for g in g2),
          "%d 句" % len(g2))

    # ---- 负对照：长句必须被拆 ----
    long_segs = [{"start": float(i), "end": float(i) + 0.9,
                  "text": "s%d" % i, "zh": ""} for i in range(30)]   # 30 秒连唱
    ly = _tone(int(30.0 * sr), sr)
    lw = os.path.join(tmp, "long.wav")
    sf.write(lw, ly, sr)
    gl = M.phrase_segs(long_segs, lw, min_gap=0.45, max_sec=14.0)
    check("★ 人声连成一片时，长句会被拆开（否则整首并成 1~2 大块）",
          len(gl) >= 2, "%d 句" % len(gl))
    check("★ 拆完每句不超过 max_sec（+1 秒容差）",
          all((g["end"] - g["start"]) <= 15.0 for g in gl),
          str([round(g["end"] - g["start"], 1) for g in gl]))

    # 负对照（真跑一遍坏版本）：把 _split_long 摘掉 ⇒ 长句不该再被拆
    # ★ 改的是 **multising.py**（函数在那儿），不是 studio.py。
    msrc = io.open(os.path.join(HERE, "stages", "multising.py"),
                   encoding="utf-8").read()
    bad = msrc.replace("    split = []\n    for g in groups:\n"
                       "        split.extend(_split_long(g, segs, _gap_between, max_sec))",
                       "    split = groups", 1)
    ok_bad = ("    split = groups" in bad
              and "split.extend(_split_long" not in bad)
    if ok_bad:
        # ★ 必须叫 **multising.py** 且放进一个干净的目录：子进程靠 import 名字
        #   找它，写成 bad_multising.py 的话 import 的还是原版 ⇒ 负对照永远
        #   "跟好版本一样"，那是假尺子。
        bdir = os.path.join(tmp, "badms")
        os.makedirs(bdir, exist_ok=True)
        io.open(os.path.join(bdir, "multising.py"), "w",
                encoding="utf-8").write(bad)
        r = subprocess.run([sys.executable, "-c",
                            "import sys; sys.path.insert(0, %r);"
                            "import multising as M;"
                            "print(len(M.phrase_segs("
                            "[{'start': float(i), 'end': float(i)+0.9,"
                            "'text':'s'} for i in range(30)], %r,"
                            "min_gap=0.45, max_sec=14.0)))" % (bdir, lw)],
                           capture_output=True, text=True)
        n_bad = (r.stdout or "").strip()
        if not n_bad:
            n_bad = "ERR:" + ((r.stderr or "").strip().splitlines() or ["?"])[-1][:70]
        # 断言写成"坏版本句数 < 好版本"而不是"坏版本 == 1"：合并阶段本身也可能
        #   断出一两处（取决于素材边界），但**拆分这一步多切出来的**才是要守的。
        check("负对照：摘掉长句拆分后，句数明显变少（证明它不是摆设）",
              n_bad.isdigit() and int(n_bad) < len(gl),
              "坏版本 %s 句 vs 好版本 %d 句" % (n_bad or "?", len(gl)))
    else:
        check("负对照没能造出坏版本（锚点对不上 ⇒ 上面那条是假尺子）",
              False, "锚点失效")

    # ---- 界面怎么显示这一列（★ 2026-10-25 改过口径，别照旧判据推理）----
    # 旧要求：「有中文就显示中文、原文挂 title」+「×N 合了几段」+「说了合并多少句」。
    # 老板当场推翻：「我要的是中英双语」「不会压缩综合」「按歌词文件的分段」
    # ⇒ 现在是**双语两行**、**一句都不合并**（所以 ×N 角标和"合并了多少句"
    #   那句话都该**不存在**）。判据跟着换，不是"改口径让它绿"。
    app = io.open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
    check("★ 界面**中英双语都显示**（原文 .mor + 中文 .mzh，不是二选一）",
          'class="mor"' in app and 'class="mzh"' in app)
    check("  不再用「有中文就中文、没有才原文」那种二选一写法",
          "esc(zh || ln.text" not in app,
          "还在写 `zh || ln.text` ⇒ 老板永远只看见一半")
    css = io.open(os.path.join(HERE, "web", "style.css"), encoding="utf-8").read()
    check("★ 不再有「×N 合了几段」的角标（已经一句都不合并了）",
          ".msub" not in app and ".msub" not in css,
          "还在画 ×N ⇒ 用户会以为系统还在把他的句子并起来")
    check("★ 预览会说清**句子是哪来的**（不是『合并了多少句』）",
          ('"line_src"' in src and "一句没合并" in src),
          "line_src / 一句没合并 没在 multi_plan_preview 里")
    check("  而且不再提「按原曲换气合并」（那步已经删了）",
          "按原曲的换气把 %d 个断句合成了 %d 句" not in src)


def main():
    print("多角色合唱门禁：分配表完整无缝 · 拼接等响不塌陷 · 接缝无阶跃 · 逐人体检 · 换法重拼")
    a_plan()
    b_blend()
    b2_overlay()
    c_negative()
    d_cli()
    e_contract()
    f_sampling()
    g_pipeline()
    h_manual()
    i_resplit()
    j_group_split()
    k_shared_cache()
    l_phrase()
    print("\n通过 %d 项，失败 %d 项" % (N_PASS, N_FAIL))
    if FAILS:
        print("失败清单：")
        for f in FAILS:
            print("  - %s" % f)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
