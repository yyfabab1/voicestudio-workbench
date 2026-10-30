# -*- coding: utf-8 -*-
r"""门禁：精修台 —— 音块级微调（stages/finetune.py + /api/fine/* + 前端 #secFine）。

为什么单开这一关
================
分离和变声都是**整轨**动作：一次把整首歌过一遍模型。可真正难听的往往只是几处
（副歌第二句某个字哑掉、尾巴糊在混响里、和声跟主唱叠在一起打架）。精修台是这
个项目里第一个"对准某一个音下指令"的功能，它踩空的方式和别处都不一样 ——
代码全绿、接口全通，但：

  · 改第 3 句会顺带改掉第 4 句的第一个字（相邻块咬在一起）
  · 「一键修」直接把音频改了，没法反悔（契约是"只出清单"）
  · 只改一句，整首歌却跟原来不一样了（没写到的块被动了）
  · 「分和声」分出来的两层音高其实没分开（分层边界取错了间隔）

这些**只能**靠"已知答案的合成素材 + 真 HTTP"量出来，静态检查一条都看不见。

四条硬契约（判据围绕它们写）
============================
  ① 切出来的块**不许重叠**
  ② 一键修**只产出 edits**，绝不改 blocks、绝不碰源文件
  ③ 渲染时**没写到的块原样保留**
  ④ 分层是"按音高归层"，两层的音高区间必须真的分开

负对照（每条都实测能让行为变，见末尾自检账）
============================================
  · 摘掉 `a = max(a, prev_end)`        ⇒ 相邻块咬在一起
  · 摘掉 `_split_long` 的调用          ⇒ 长出好几秒的块（几个音黏一块）
  · 不让谱峰法优先                      ⇒ 叠音块被自相关的"虚拟基频"带跑（440→220）
  · 邻域中位数换成全曲中位数            ⇒ 整段都轻的一段被判成哑音（乱报）
  · auto_fix 里顺手改 blocks            ⇒ 契约②破（最容易被后来人顺手加的一行）
  · 摘掉 `_erase`                       ⇒ 改调变成"新旧叠加"，f0 量回原值
  · 分层取"最小的 n-1 个间隔"           ⇒ 两层音高不再分离
  · 路由里不走 `_fine_path`             ⇒ 项目外的路径被放行

用法（服务必须和门禁在同一条命令里，铁律 20）：
    <venv>/Scripts/python.exe verify_finetune.py
"""
import ast
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TMP = os.path.join(HERE, "_tmp", "gate_fine")
PORT = int(os.environ.get("VS_FINE_PORT", "8903"))
SR = 44100

N_PASS, N_FAIL = 0, 0
_SFAIL = [0]          # 负对照自检：坏版本**没**变红的条数


def check(name, ok, note=""):
    global N_PASS, N_FAIL
    if ok:
        N_PASS += 1
        print("  [ok]   %s%s" % (name, ("  — %s" % note) if note else ""))
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  — %s" % note) if note else ""))


def scheck(name, ok, note=""):
    """负对照自检：坏版本必须**真的**变红。它自己红的条数单独记账。"""
    if not ok:
        _SFAIL[0] += 1
    print("  [%s] 负对照 %s%s" % ("ok" if ok else "FAIL", name,
                                  ("  — %s" % note) if note else ""),
          flush=True)


def sec(t):
    print("\n== %s ==" % t, flush=True)


def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


def semis(a, b):
    """两个频率差多少半音（任一个是 0 ⇒ 无穷大）。"""
    if not a or not b:
        return 999.0
    return abs(12.0 * math.log(float(a) / float(b), 2))


# ---------------------------------------------------------------- 素材
def tone(f, dur, amp, sr=SR):
    import numpy as np
    t = np.arange(int(dur * sr)) / float(sr)
    x = np.sin(2 * np.pi * f * t)
    x = x + 0.35 * np.sin(2 * np.pi * 2 * f * t)
    x = x + 0.18 * np.sin(2 * np.pi * 3 * f * t)
    env = np.minimum(1.0, t / 0.02) * np.minimum(1.0, (dur - t) / 0.03)
    return (x * env * amp).astype(np.float32)


def build(name, freqs, amps, stacks=None, gap=0.12, dur=0.35, clip=()):
    """拼一段"已知答案"的音频，返回路径。

    ★ 幅度一律压在 0.31 以下：write_wav 只在峰值 >1.0 时整体缩放，
      一旦触发，连没动过的块都会被缩放一点点 —— "原样保留"这条判据会因此假红。
    """
    import numpy as np
    import stages.finetune as ft
    parts = []
    for k, f in enumerate(freqs):
        seg = tone(f, dur, amps[k])
        if stacks and k in stacks:
            seg = seg + tone(stacks[k], dur, amps[k] * 0.87)
        if k in clip:
            seg = np.clip(seg * 6.0, -1.0, 1.0).astype(np.float32)
        parts.append(seg)
        parts.append(np.zeros(int(gap * SR), dtype=np.float32))
    p = os.path.join(TMP, name + ".wav")
    ft.write_wav(p, np.concatenate(parts), SR)
    return p


def load_mod(src_text, name):
    """把（可能被改坏的）finetune 源码当独立模块加载。"""
    import importlib.util
    p = os.path.join(TMP, "_ftmod_%s.py" % name)
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(src_text)
    spec = importlib.util.spec_from_file_location("ftmod_" + name, p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load(path, sr=SR):
    import numpy as np
    import stages.finetune as ft
    y, _ = ft.read_audio(path, sr)
    y = np.asarray(y, dtype=np.float64)
    return y if y.ndim == 1 else y.mean(axis=1)


def rms_at(path, b, sr=SR):
    import numpy as np
    y = _load(path, sr)
    a, z = max(0, int(b["t0"] * sr)), min(y.size, int(b["t1"] * sr))
    if z <= a:
        return 0.0
    return float(np.sqrt(np.mean(y[a:z] ** 2)))


def f0_at(path, b, sr=SR, mod=None):
    import stages.finetune as ft
    ft = mod or ft
    y = _load(path, sr)
    seg = y[max(0, int(b["t0"] * sr)):min(y.size, int(b["t1"] * sr))]
    if seg.size < 256:
        return 0.0
    parts = ft._independent_partials(seg, sr)
    return float(parts[0][1]) if parts else 0.0


def max_overlap(blocks):
    worst = 0.0
    for i in range(len(blocks) - 1):
        ov = blocks[i]["t1"] - blocks[i + 1]["t0"]
        if ov > worst:
            worst = ov
    return worst


# ================================================================ A 接线
def a_wiring():
    sec("A 接线（源码静态）")
    ft_src = read(os.path.join(HERE, "stages", "finetune.py"))
    sv = read(os.path.join(HERE, "server.py"))
    html = read(os.path.join(HERE, "web", "index.html"))
    js = read(os.path.join(HERE, "web", "app.js"))
    css = read(os.path.join(HERE, "web", "style.css"))

    check("stages/finetune.py 在", os.path.isfile(os.path.join(HERE, "stages", "finetune.py")))

    # 铁律 27b：阈值先数一遍。/api/fine/* 现在 12 条（3 GET + 9 POST）——
    # 2026-10-26 加了 polish / polish/apply 两条（唱前唱后微调），8→10；
    # 同一天又加了 polish/audition（试听），10→11；
    # 同一天再加 polish/steps（主链路「功能开关」的清单，GET），11→12。
    # ★ 加路由时这里必须跟着改，否则它会红得像产品坏了（跟 NAV_IDS 同一个坑；
    #   这个项目为它红过两次）。
    routes = set(re.findall(r'path == "(/api/fine/[a-z/]+)"', sv))
    check("12 条 /api/fine/* 路由都在（先 grep 数过是 12）",
          len(routes) == 12, sorted(routes))

    kinds = ("dedead", "refill", "dereverb", "deoverlap", "declip")
    m = re.search(r"kind not in \(([^)]*)\)", sv)
    whitelist = m.group(1) if m else ""
    check("服务端认全部 5 种一键修（%s）" % ", ".join(kinds),
          all(('"%s"' % k) in whitelist for k in kinds), whitelist[:90])
    btn = len(re.findall(r'data-fix="([a-z]+)"', html))
    check("界面上 5 个一键修按钮都在", btn == 5, "%d 个" % btn)

    check("前端有 #secFine 面板", 'id="secFine"' in html)
    check("启动脚本里调了 fineInit()（不调 = 面板永远空着）",
          "fineInit();" in html)
    check("导航栏里有「精修台」", 'id: "secFine"' in js)
    check("精修台的样式在（.fine-bar）", ".fine-bar{" in css)

    # 铁律 24e：只依赖 numpy + soundfile，librosa 必须是"可选加速"而不是硬依赖
    tree = ast.parse(ft_src)
    top = set()
    for nd in tree.body:
        if isinstance(nd, ast.Import):
            for a in nd.names:
                top.add(a.name.split(".")[0])
        elif isinstance(nd, ast.ImportFrom) and nd.module:
            top.add(nd.module.split(".")[0])
    check("顶层没 import librosa/soundfile（它们只能在函数里按需 import）",
          "librosa" not in top and "soundfile" not in top, sorted(top))
    check("顶层 import 了 numpy", "numpy" in top, sorted(top))
    check("auto_fix 的 docstring 明说了「绝不改音频」",
          "绝不改音频" in ft_src)


# ================================================================ B 切块
def b_segment(mod_good, src):
    sec("B 切块（已知答案：7 个音）")
    p = build("main",
              [220.0, 261.6, 293.7, 329.6, 392.0, 440.0, 523.3],
              [0.30, 0.30, 0.30, 0.03, 0.30, 0.30, 0.30],
              stacks={5: 659.3})
    r = mod_good.analyze(p)
    bl = r["blocks"]
    check("切出 7 个音块", len(bl) == 7, "%d 个" % len(bl))
    check("块按时间递增，且都在音频长度内",
          all(bl[i]["t1"] <= bl[i + 1]["t0"] + 1e-6 for i in range(len(bl) - 1))
          and all(0 <= b["t0"] < b["t1"] <= r["dur"] + 0.05 for b in bl))
    ov = max_overlap(bl)
    check("★ 相邻块不许重叠（改一块不能带动邻居）", ov <= 1e-6,
          "最大重叠 %.4fs" % ov)

    # 负对照 1：起音往前补之后不顶回 prev_end
    bad = load_mod(src.replace("        a = max(a, prev_end)", "        a = a"), "ov")
    bl2 = bad.analyze(p)["blocks"]
    scheck("摘掉 `a = max(a, prev_end)` ⇒ 相邻块咬在一起",
           max_overlap(bl2) > 0.01, "最大重叠 %.4fs" % max_overlap(bl2))

    # 长块：一段 3 秒连音必须在能量谷处被切开
    pl = build("long", [220.0], [0.30], gap=0.0, dur=3.0)
    bl3 = mod_good.analyze(pl)["blocks"]
    mx = max((b["t1"] - b["t0"]) for b in bl3) if bl3 else 99.0
    check("3 秒的连音会被切成好几块（每块 ≤ 1.25s）",
          len(bl3) > 2 and mx <= 1.25, "%d 块 / 最长 %.2fs" % (len(bl3), mx))

    bad3 = load_mod(src.replace(
        "            out += _split_long(x, sr, t0, t1, max_blk, min_blk)",
        "            out.append((t0, t1))"), "nosplit")
    bl4 = bad3.analyze(pl)["blocks"]
    mx4 = max((b["t1"] - b["t0"]) for b in bl4) if bl4 else 0.0
    scheck("摘掉 `_split_long` ⇒ 长出 >2s 的块（几个音黏一块，基频跟着瞎）",
           mx4 > 2.0, "最长 %.2fs" % mx4)

    # 负对照：摘掉门限上限 ⇒ 整条都在响的音频（p10 落在响段上）一个块都切不出来
    bad5 = load_mod(src.replace(
        "    gate = min(gate, float(env.max()) * 0.45)", "    pass"), "nocap")
    bl5 = bad5.analyze(pl)["blocks"]
    scheck("摘掉门限上限 ⇒ 整条都在响的音频静默变成 0 块（界面只显示「0 个音块」）",
           len(bl5) == 0, "%d 块" % len(bl5))
    return p, r


# ================================================================ C 基频
def c_f0(mod_good, src, p, r):
    sec("C 基频 / 音名")
    bl = r["blocks"]
    truth = [220.0, 261.6, 293.7, 329.6, 392.0, 440.0, 523.3]
    names = ["A3", "C4", "D4", "E4", "G4", "A4", "C5"]
    off = [semis(bl[i]["f0"], truth[i]) for i in range(min(7, len(bl)))]
    check("7 块的基频都在 ±0.35 半音内",
          len(bl) == 7 and max(off) < 0.35,
          "最大偏差 %.3f 半音" % max(off))
    check("音名对得上（%s）" % " ".join(names),
          [b["name"] for b in bl] == names, [b["name"] for b in bl])

    # 负对照 2：不让谱峰法优先 ⇒ 叠音块（A4 上叠 E5）被自相关的虚拟基频带跑
    bad = load_mod(src.replace("            if F0_MIN <= fp <= F0_MAX:",
                               "            if False:"), "nospec")
    bl2 = bad.analyze(p)["blocks"]
    got = bl2[5]["f0"] if len(bl2) > 5 else 0.0
    scheck("不让谱峰法优先 ⇒ 叠音那块掉到虚拟基频（220 一带）",
           got < 300.0, "f0=%.1f（正常版 %.1f）" % (got, bl[5]["f0"]))


# ================================================================ D 诊断
def d_diag(mod_good, src, p, r):
    sec("D 诊断（宁可漏报不可乱报）")
    d = r["diag"]
    check("第 4 块（音量压到 1/10）判成哑音", d.get("dead") == [3], d.get("dead"))
    check("第 6 块（A4 上叠 E5）判成叠音", d.get("stack") == [5], d.get("stack"))
    check("别的块没被误判",
          not (set(d.get("dead", [])) | set(d.get("stack", []))) - {3, 5},
          {k: v for k, v in d.items() if v})

    pc = build("clip", [220.0, 261.6, 293.7, 329.6],
               [0.30, 0.30, 0.30, 0.30], clip=(1,))
    dc = mod_good.analyze(pc)["diag"]
    check("削波那块判成爆音", dc.get("clip") == [1], dc.get("clip"))

    # 负对照 3：邻域中位数 → 全曲中位数。12 块里中间 6 块整体轻一半，
    #           这在真实歌里就是"整段副歌都轻"，不是哑音。
    half = ([0.30] * 3) + ([0.05] * 6) + ([0.30] * 3)
    ph = build("half",
               [220.0, 247.0, 262.0, 277.0, 294.0, 311.0, 330.0,
                349.0, 370.0, 392.0, 415.0, 440.0], half)
    dh = mod_good.analyze(ph)["diag"]
    check("★ 整段都轻（12 块中间 6 块）不算哑音 —— 用邻域中位数，不是全曲中位数",
          not dh.get("dead"), dh.get("dead"))
    bad = load_mod(src.replace("        med = float(np.median(rms[lo:hi]))",
                               "        med = float(np.median(rms))"), "glob")
    dh2 = bad.analyze(ph)["diag"]
    scheck("换成全曲中位数 ⇒ 整段都轻的那 6 块被判成哑音（乱报）",
           len(dh2.get("dead") or []) >= 4, dh2.get("dead"))


# ================================================================ E 一键修只出清单
def e_autofix(mod_good, src, p, r):
    sec("E ★ 一键修只产出 edits，绝不改音频")
    bl = r["blocks"]
    d = r["diag"]
    before = json.dumps(bl, sort_keys=True, ensure_ascii=False)
    stat0 = (os.path.getsize(p), int(os.path.getmtime(p)))
    with io.open(p, "rb") as f:
        head0 = f.read(4096)

    eds = mod_good.auto_fix(bl, d, "dedead")
    check("去哑音生成了 1 条修改（第 4 块 +12dB 封顶内）",
          len(eds) == 1 and eds[0]["i"] == 3 and 0 < eds[0]["gain"] <= 12.0, eds)
    check("★ blocks 一个字节都没被改",
          json.dumps(bl, sort_keys=True, ensure_ascii=False) == before)
    check("★ 源文件没被动过（大小/时间戳/前 4KB）",
          (os.path.getsize(p), int(os.path.getmtime(p))) == stat0
          and io.open(p, "rb").read(4096) == head0)

    # 五种修法都得有输出，且不认识的种类返回空（不抛）
    out = {}
    for k in ("dedead", "refill", "dereverb", "deoverlap", "declip"):
        try:
            out[k] = mod_good.auto_fix(bl, d, k)
        except Exception as e:      # noqa: BLE001
            out[k] = "EXC:%s" % e
    check("5 种修法都跑得通（不抛异常）",
          all(isinstance(v, list) for v in out.values()),
          {k: (v if not isinstance(v, list) else len(v)) for k, v in out.items()})
    check("哑音块能补音（refill 找得到 donor）",
          any(e.get("fill") is not None for e in out["refill"]), out["refill"])
    check("不认识的修法返回空清单（不抛）",
          mod_good.auto_fix(bl, d, "不存在的修法") == [])

    # 负对照 4：后来人在 auto_fix 里"顺手"改一下 blocks
    bad = load_mod(src.replace('                edits.append({"i": i, "gain": round(db, 1)})',
                               '                blocks[i]["rms"] = 0.5\n'
                               '                edits.append({"i": i, "gain": round(db, 1)})'),
                   "mut")
    bl2 = json.loads(before)
    bad.auto_fix(bl2, d, "dedead")
    scheck("auto_fix 里顺手改 blocks ⇒ 契约被打破（判据抓得到）",
           json.dumps(bl2, sort_keys=True, ensure_ascii=False) != before,
           "第 4 块 rms 被改成 %s" % bl2[3]["rms"])


# ================================================================ F 渲染
def f_render(mod_good, src, p, r):
    sec("F 渲染（改哪块只动哪块）")
    bl = r["blocks"]
    d = r["diag"]
    eds = mod_good.auto_fix(bl, d, "dedead")

    out1 = os.path.join(TMP, "out_dedead.wav")
    mod_good.render(p, bl, eds, out1)
    r0, r1 = rms_at(p, bl[3]), rms_at(out1, bl[3])
    check("★ 哑音块真的被提上来了（≥2.5×）", r1 > r0 * 2.5,
          "%.5f → %.5f" % (r0, r1))

    diffs = []
    for i in range(len(bl)):
        if i == 3:
            continue
        a = _load(p)[int(bl[i]["t0"] * SR):int(bl[i]["t1"] * SR)]
        b = _load(out1)[int(bl[i]["t0"] * SR):int(bl[i]["t1"] * SR)]
        n = min(a.size, b.size)
        if n:
            diffs.append(float(max(abs(a[:n] - b[:n]))))
    check("★ 没写到的块原样保留（最大差 <1e-3）",
          max(diffs) < 1e-3, "最大差 %.2e" % max(diffs))

    # 改调：给第 2 块 +3 半音
    out2 = os.path.join(TMP, "out_semi.wav")
    mod_good.render(p, bl, [{"i": 1, "semi": 3}], out2)
    f_orig = f0_at(p, bl[1])
    f_new = f0_at(out2, bl[1])
    sd = semis(f_new, f_orig)
    check("改调 +3 半音后基频真的上去了（2.6~3.4 半音内）",
          2.6 < sd < 3.4, "%.1f → %.1f（差 %.2f 半音）" % (f_orig, f_new, sd))

    bad = load_mod(src.replace("        _erase(out_arr, a0, a1, sr)", "        pass"),
                   "noerase")
    out3 = os.path.join(TMP, "out_noerase.wav")
    bad.render(p, bl, [{"i": 1, "semi": 3}], out3)
    f_bad = f0_at(out3, bl[1])
    scheck("摘掉 `_erase` ⇒ 改调变成新旧叠加，基频量回原值（<1 半音）",
           semis(f_bad, f_orig) < 1.0,
           "%.1f（正常版 %.1f）" % (f_bad, f_new))

    # 静音 / 删除
    out4 = os.path.join(TMP, "out_mute.wav")
    mod_good.render(p, bl, [{"i": 2, "mute": True}], out4)
    check("静音那块变成静音", rms_at(out4, bl[2]) < 1e-4,
          "%.6f" % rms_at(out4, bl[2]))

    # 补音
    out5 = os.path.join(TMP, "out_fill.wav")
    fill = [e for e in mod_good.auto_fix(bl, d, "refill")]
    mod_good.render(p, bl, fill, out5)
    f_fill = f0_at(out5, bl[3])
    check("补音后那块有清晰音高、且落在目标音高上（±1 半音）",
          fill and semis(f_fill, bl[3]["f0"]) < 1.0,
          "%.1f vs 目标 %.1f" % (f_fill, bl[3]["f0"]))

    # 时长不许变
    d_src = mod_good.analyze(p)["dur"]
    d_out = mod_good.analyze(out1)["dur"]
    check("渲染前后总时长不变（±30ms）", abs(d_out - d_src) < 0.03,
          "%.3f → %.3f" % (d_src, d_out))


# ================================================================ G 分层
def g_split(mod_good, src, p, r):
    sec("G 分层导出（手动分和声）")
    bl = r["blocks"]
    dd = os.path.join(TMP, "layers")
    items = mod_good.split_layers(p, bl, dd, n=2)
    check("分出 2 层，每层文件都在", len(items) == 2
          and all(os.path.isfile(x["path"]) for x in items),
          [(x["name"], x["blocks"]) for x in items])
    check("每层都分到了块（有人没分到 = 静悄悄少一层）",
          all(x["blocks"] > 0 for x in items),
          [(x["name"], x["blocks"]) for x in items])

    lo = [bl[i]["f0"] for i, b in enumerate(bl)
          if b["f0"] > 0 and b["f0"] < 380]
    hi = [bl[i]["f0"] for i, b in enumerate(bl) if b["f0"] >= 380]
    check("★ 两层的音高区间真的分开了（低层最高 < 高层最低）",
          lo and hi and max(lo) < min(hi),
          "低层 max %.1f / 高层 min %.1f" % (max(lo), min(hi)))

    bad = load_mod(src.replace("order = np.argsort(gaps)[::-1][:n - 1]",
                               "order = np.argsort(gaps)[:n - 1]"), "badsplit")
    items2 = bad.split_layers(p, bl, os.path.join(TMP, "layers_bad"), n=2)
    cnt2 = [x["blocks"] for x in items2]
    scheck("分层取「最小的 n-1 个间隔」⇒ 分层结果变了（尺子咬得住）",
           cnt2 != [x["blocks"] for x in items] or len(items2) != len(items),
           "坏版本 %s / 正常版 %s" % (cnt2, [x["blocks"] for x in items]))

    items4 = mod_good.split_layers(p, bl, os.path.join(TMP, "layers4"), n=4)
    check("要 4 层就给 4 层", len(items4) == 4, len(items4))


# ================================================================ H 真素材
def h_real(mod_good):
    sec("H 真素材（合成素材过了不代表真歌过）")
    import studio
    srcs = studio.fine_sources()
    check("能列出可修音频", len(srcs) > 0, "%d 条" % len(srcs))
    kinds = {}
    for it in srcs:
        kinds[it["kind"]] = kinds.get(it["kind"], 0) + 1
    check("清单里至少 3 类（成品/曲库/上传…），每类都露得出来",
          len(kinds) >= 3, kinds)
    check("每条都带 path / kind / label",
          all(x.get("path") and x.get("kind") and x.get("label") for x in srcs[:20]))

    pick = next((x for x in srcs if x["kind"] == "成品" and x["path"]), srcs[0])
    try:
        rr = mod_good.analyze(pick["path"])
    except Exception as e:                                   # noqa: BLE001
        check("真实成品能分析出音块", False, "%s" % e)
        return
    nb = len(rr.get("blocks") or [])
    check("真实成品能分析出音块", nb > 0,
          "%d 块 / %.1fs · %s" % (nb, rr.get("dur", 0), pick["label"][:40]))
    if nb:
        voiced = sum(1 for b in rr["blocks"] if b["f0"] > 0)
        check("真歌里大部分块量得出基频（>60%）",
              voiced > nb * 0.6, "%d/%d" % (voiced, nb))
        out = os.path.join(TMP, "real_copy.wav")
        mod_good.render(pick["path"], rr["blocks"], [], out)
        check("空清单渲染 = 原样复制（时长差 <80ms）",
              os.path.isfile(out)
              and abs(mod_good.analyze(out)["dur"] - rr["dur"]) < 0.08,
              "%.3f → %.3f" % (rr["dur"], mod_good.analyze(out)["dur"]))
    check("文件不存在时报 Fail（不是 500 / 不是静默）",
          _expect_fail(studio.fine_analyze, os.path.join(TMP, "不存在.wav")))


def _expect_fail(fn, *a):
    try:
        fn(*a)
        return False
    except Exception:
        return True


# ================================================================ I HTTP
def _listen(port, timeout=0.4):
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _http(path, body=None, timeout=180):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_err(path, body=None):
    """期望被明确拒绝：返回 (状态码, 原因)。"""
    try:
        _http(path, body)
        return 0, "竟然放行了"
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode("utf-8"))
        except Exception:
            j = {}
        return e.code, str(j.get("error") or "")[:80]
    except Exception as e:                                   # noqa: BLE001
        return -1, str(e)[:80]


def start_service():
    import studio
    if not os.path.isfile(studio.PY):
        return None, "找不到 GSV 运行时：%s" % studio.PY
    if _listen(PORT):
        return None, "端口 %d 已被占用（换 VS_FINE_PORT）" % PORT
    env = dict(os.environ)
    env["VOICESTUDIO_PORT"] = str(PORT)
    logp = os.path.join(HERE, "_tmp", "svc_%d.log" % PORT)
    f = open(logp, "wb")
    p = subprocess.Popen([studio.PY, "-u", os.path.join(HERE, "server.py")],
                         cwd=HERE, env=env, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(0.7)
        try:
            if _http("/api/health").get("ok"):
                return p, logp
        except Exception:
            pass
    return p, logp


def i_http(mod_good):
    sec("I 真 HTTP（自起独立端口 %d，不碰用户正在用的 8756）" % PORT)
    proc, info = start_service()
    if not proc:
        check("在 %d 起一份独立服务" % PORT, False, info)
        return None
    made = []
    try:
        h = _http("/api/health")
        check("服务活着", h.get("ok") is True, "pid=%s" % h.get("pid"))
        fresh = float(h.get("code_mtime") or 0) <= float(h.get("started_ts") or 0) + 2
        check("服务跑的是当前代码（code_mtime <= started_ts）", fresh,
              "code=%s started=%s" % (h.get("code_mtime"), h.get("started_ts")))

        srcs = _http("/api/fine/sources").get("items") or []
        check("GET /api/fine/sources 有声源", len(srcs) > 0, "%d 条" % len(srcs))

        p = build("http_src", [220.0, 261.6, 293.7, 329.6, 392.0],
                  [0.30, 0.30, 0.03, 0.30, 0.30])
        a = _http("/api/fine/analyze", {"path": p})
        bl = a.get("blocks") or []
        check("POST /api/fine/analyze 出音块", len(bl) == 5, "%d 块" % len(bl))
        check("analyze 把第 3 块判成哑音",
              (a.get("diag") or {}).get("dead") == [2], (a.get("diag") or {}).get("dead"))

        fx = _http("/api/fine/fix", {"path": p, "kind": "dedead",
                                     "blocks": bl, "diag": a.get("diag")})
        eds = fx.get("edits") or []
        check("POST /api/fine/fix 生成修改清单",
              len(eds) == 1 and eds[0]["i"] == 2, eds)

        rr = _http("/api/fine/render", {"path": p, "blocks": bl, "edits": eds,
                                        "name": "_ft_http_src"})
        made.append(rr.get("path"))
        check("POST /api/fine/render 导出成功",
              bool(rr.get("path")) and os.path.isfile(rr["path"]), rr.get("path"))
        check("★ 导出的是新版（不覆盖上一版，用户要 A/B）",
              rr.get("path") and "精修" in (rr.get("name") or ""), rr.get("name"))
        if rr.get("path"):
            r0 = rms_at(p, bl[2])
            r1 = rms_at(rr["path"], bl[2])
            check("★ 导出文件里哑音块真的提上来了", r1 > r0 * 2.5,
                  "%.5f → %.5f" % (r0, r1))

        sp = _http("/api/fine/split", {"path": p, "n": 2})
        its = sp.get("items") or []
        made += [x["path"] for x in its]
        check("POST /api/fine/split 分出 2 层",
              len(its) == 2 and all(os.path.isfile(x["path"]) for x in its),
              [(x["name"], x["blocks"]) for x in its])

        code, why = _http_err("/api/fine/analyze",
                              {"path": r"C:\Windows\System32\drivers\etc\hosts"})
        check("★ 项目外的路径被挡住（400 带原因）", code == 400 and bool(why),
              "%s %s" % (code, why))
        code2, why2 = _http_err("/api/fine/fix", {"path": p, "kind": "乱写的修法"})
        check("不认识的修法被挡住（400 带原因）", code2 == 400 and bool(why2),
              "%s %s" % (code2, why2))
        code3, why3 = _http_err("/api/fine/render", {"path": p, "edits": "不是数组"})
        check("edits 不是数组被挡住（400 带原因）", code3 == 400 and bool(why3),
              "%s %s" % (code3, why3))
        code4, why4 = _http_err("/api/fine/analyze", {"path": ""})
        check("不给路径被挡住（400 带原因）", code4 == 400 and bool(why4),
              "%s %s" % (code4, why4))

        # 铁律 13：路由层"装了但没走到"的守卫 —— 四条写接口必须都过 _fine_path。
        # 切片方式：按 `if path == "` 切开，一段 = 一条路由（到下一条为止）。
        import server
        sv = read(os.path.join(HERE, "server.py"))
        parts = [x for x in sv.split('if path == "')[1:]
                 if x.startswith("/api/fine/")]
        got = [x for x in parts if x.split('"')[0] in
               ("/api/fine/analyze", "/api/fine/fix",
                "/api/fine/render", "/api/fine/split")]
        n_guard = sum(1 for x in got if "_fine_path" in x)
        check("★ 4 条写接口都走了 _fine_path（不是把用户给的路径直接拿去用）",
              len(got) == 4 and n_guard == 4, "%d/%d" % (n_guard, len(got)))
        check("_fine_path 放项目内的路径、挡项目外的",
              bool(server._fine_path(p))
              and server._fine_path(r"C:\Windows\System32\drivers\etc\hosts") is None)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        # 收尾：这一关的产物不在用户目录里，HTTP 那两个落在 精修成品/ 下要清掉
        for pth in made:
            try:
                if pth and os.path.isfile(pth):
                    os.remove(pth)
            except Exception:
                pass
        d = os.path.join(HERE, "精修成品", "_ft_http_src")
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    return True


# ================================================================ main
def main():
    print("精修台门禁：stages/finetune.py + /api/fine/* + 前端 #secFine", flush=True)
    os.makedirs(TMP, exist_ok=True)
    import stages.finetune as ft

    src = read(os.path.join(HERE, "stages", "finetune.py"))
    a_wiring()
    p, r = b_segment(ft, src)
    c_f0(ft, src, p, r)
    d_diag(ft, src, p, r)
    e_autofix(ft, src, p, r)
    f_render(ft, src, p, r)
    g_split(ft, src, p, r)
    h_real(ft)
    i_http(ft)

    sec("负对照自检账")
    print("（自检账不计入正式判据）")
    check("★ 所有负对照都真的改变了行为（有一条没变 = 那条是假尺子）",
          _SFAIL[0] == 0, "没通过的：%d 条" % _SFAIL[0])

    print("\n" + "=" * 60)
    print("精修台门禁：%d 通过 / %d 失败" % (N_PASS, N_FAIL))
    if N_FAIL:
        print("★ 有判红项")
    print("=" * 60)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
