# -*- coding: utf-8 -*-
r"""门禁：唱前 / 唱后微调（stages/polish.py + /api/fine/polish* + 精修台微调条）。

为什么单开这一关
================
hf（高频）/ demetal（电音）/ presence（临场感）三关管的都是"频谱形状"。
这一关管的是另外三件**形状之外**的事：

  · 底噪 / 隆隆 / 削波 / 爆音 —— 干声本身脏（唱前）
  · 齿音 / 刺耳 / 高音刺 / 拼接咔哒 —— 唱出来之后难听（唱后）

它们踩空的方式跟别处都不一样 —— 代码全绿、接口全通，但：

  · 降噪把人声一起啃了（只盯着"噪声底降了多少"，没看人声掉了多少）
  · 修削波的插值锚点取在被削平的那一段里 ⇒ 补出来还是平的（等于没修）
  · 三次插值跨太长会飞 ⇒ 峰值冲到 10 倍，造出新的爆音
  · 高音柔化拿"自己的滑动中位线"当基线 ⇒ 持续高音永远是 0 ⇒ 一刀都下不去
  · 齿音只按亮度判 ⇒ 把 hi-hat 当齿音压（shimmer 用实测数据否掉过）
  · 拼接点检测没有"孤立"这一条 ⇒ 谐波的陡坡整片超标，真接缝反而被淹没
  · 交叉淡化用线性 ⇒ 接缝处掉 6dB（听感上"陷"一下）

四条硬契约（判据围绕它们写）
============================
  ① 每一步都必须**先量后修**：不达标就退回去，**不写那个 wav**
  ② 每一步都配一对数：该治的（gain）达标 **且** 不该碰的（harm）没动
  ③ SKIP 是正常结果，调用方不许拿不存在的路径当成品
  ④ 用户给的路径一律过 `_fine_path`（项目外的不许读）

负对照（每条都实测能让行为变，见末尾自检账）
============================================
  · --skip hp        ⇒ 隆隆不降            · --skip denoise ⇒ 噪声底不降
  · --skip declip    ⇒ 削波不动            · --skip declick ⇒ 爆音不减
  · --skip deess     ⇒ 齿音不降            · --skip belt    ⇒ 高音亮度不降
  · --skip heal      ⇒ 拼接点不减

用法（服务必须和门禁在同一条命令里，铁律 20）：
    <venv>/Scripts/python.exe verify_polish.py
"""
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
# ★ 每次跑都用**全新**的临时目录，不要复用同一个。
#   复用的话每一步都得先 os.remove 一遍才能断言"SKIP 时没写 wav" ——
#   那些删除会累计，撞穿本机的批量删除钩子（阈值 50）⇒ 门禁在半路被掐断，
#   看着像产品坏了（实测撞到：跑到 E2 负对照就断，一条判据都印不出来）。
#   全新目录 ⇒ 文件本来就不存在 ⇒ 一次都不用删。
TMP = tempfile.mkdtemp(prefix="vs_gate_polish_")
PORT = int(os.environ.get("VS_POLISH_PORT", "8904"))
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


# ---------------------------------------------------------------------------
# 素材：每一条"已知答案"的毛病都单独造，别指望真实音频凑齐
# ---------------------------------------------------------------------------

def _harm(f0, n=SR, nk=12, amp=0.3, sr=SR):
    import numpy as np
    ph = 2 * np.pi * np.cumsum(f0) / float(sr)
    y = np.zeros(n)
    for k in range(1, nk + 1):
        y += (1.0 / k) * np.sin(k * ph)
    return y * amp


def wav(name, y, sr=SR):
    import soundfile as sf
    p = os.path.join(TMP, name + ".wav")
    sf.write(p, y.astype("float32"), sr)
    return p


def mat_pre():
    """唱前素材：底噪 + 45Hz 隆隆 + 一段削波 + 两个爆音 + 有静音空档。"""
    import numpy as np
    rng = np.random.default_rng(11)
    n = int(SR * 4.0)
    t = np.arange(n) / float(SR)
    g = np.ones(n)
    for a, b in ((0.5, 0.75), (1.4, 1.6), (2.6, 2.8)):
        g[int(a * SR):int(b * SR)] = 0.0
    y = _harm(np.where(t < 2.0, 220.0, 330.0), n) * g
    # ★ 底噪 0.030 → 0.005（2026-10-26 二修）：0.030 会让整条素材的 rough（见
    #   polish._rough，"线性外推预测不了的那部分"）抬到 **0.0249**，而两份真实
    #   分离人声的 rough 中位数只有 **0.0014 / 0.0019**，p90 也才 0.0072 / 0.0137
    #   ⇒ 合成素材整条泡在真实素材"辅音段"的量级里，注入的 0.8 幅度爆音比值
    #   只有 29.7 / 22.4，卡在门限 32 下面 ⇒ 门禁假红。
    #   调到 0.005（rough≈0.0042，落在真实素材中位与 p90 之间）才是真实干声的样子。
    y = y + 0.005 * rng.standard_normal(n)          # 底噪
    y = y + 0.09 * np.sin(2 * np.pi * 45 * t)       # 隆隆
    y = np.clip(y, -1.0, 1.0)
    y[int(3.0 * SR):int(3.02 * SR)] = 0.995         # 削波
    for c in (1.0, 2.2):                            # 爆音（单采样尖峰）
        y[int(c * SR)] += 0.8
    return wav("pre_mat", y)


def mat_post():
    """唱后素材：高音段(f0 660) 的 2.5~6k 被抬起来 + 4 处齿音 + 3 个拼接台阶。"""
    import numpy as np
    from scipy.signal import butter, sosfilt
    rng = np.random.default_rng(13)
    n = int(SR * 4.0)
    t = np.arange(n) / float(SR)
    v = _harm(np.where(t < 2.0, 240.0, 660.0), n, nk=20, amp=0.26)
    sos = butter(4, [2500 / (SR / 2), 6000 / (SR / 2)], btype="band", output="sos")
    v = v + 2.2 * sosfilt(sos, v) * (t >= 2.0)      # 高音段的"刺"
    sb = np.zeros(n)
    for st in (0.6, 1.4, 2.2, 3.0):
        a, b = int(st * SR), int(st * SR) + int(0.10 * SR)
        tt = np.arange(b - a) / float(SR)
        sb[a:b] = 0.55 * (np.sin(2 * np.pi * 6200 * tt)
                          + np.sin(2 * np.pi * 8100 * tt)) * np.hanning(b - a) * 0.5
    v = v + sb + 0.005 * rng.standard_normal(n)
    for c in (1.0, 2.0, 3.0):
        v[int(c * SR):] += 0.45                     # 拼接台阶
    return wav("post_mat", (v / np.max(np.abs(v)) * 0.95))


# ---------------------------------------------------------------------------
# A 静态：接线 / 常量 / 守卫
# ---------------------------------------------------------------------------

def a_wiring():
    sec("A 静态：接线与守卫")
    mod = os.path.join(HERE, "stages", "polish.py")
    src = read(mod)
    sv = read(os.path.join(HERE, "server.py"))
    js = read(os.path.join(HERE, "web", "app.js"))
    html = read(os.path.join(HERE, "web", "index.html"))
    st = read(os.path.join(HERE, "studio.py"))

    check("stages/polish.py 存在", os.path.isfile(mod))
    for k in ("PRE_STEPS", "POST_STEPS", "SIG_LO", "DEESS_LO", "BELT_F0_LO",
              "NOISE_MULT", "DENOISE_MAX", "LIMIT_KNEE"):
        check("常量 %s 在文件里" % k, (k + " ") in src or (k + "=") in src or
              (k + ",") in src or (k + " =") in src)

    check("三条路由都在（polish / apply / audition）",
          '/api/fine/polish"' in sv and '/api/fine/polish/apply"' in sv
          and '/api/fine/polish/audition"' in sv)
    # ★ 铁律 13：路由层"装了但没走到"的守卫 —— 两条新接口必须都过 _fine_path
    a = sv.index('/api/fine/polish"')
    b = sv.index('/api/fine/polish/apply"')
    seg1 = sv[a:sv.index("if path ==", a + 10)]
    seg2 = sv[b:sv.index("if path ==", b + 10)]
    check("★ /api/fine/polish 走了 _fine_path（不是直接拿用户给的路径）",
          "_fine_path" in seg1)
    check("★ /api/fine/polish/apply 走了 _fine_path", "_fine_path" in seg2)
    check("apply 只接受 pre / post 两个方向", "POLISH_SIDES" in seg2)
    check("apply 的 skip 只放行本方向真实存在的步骤（不让传野名字）",
          "POLISH_STEPS.get(side" in seg2)

    check("studio 里有 polish_measure / polish_apply / polish_stage",
          "def polish_measure(" in st and "def polish_apply(" in st
          and "def polish_stage(" in st)
    check("★ 主链路接了唱前（在变声之前）",
          'polish_stage(job, "pre"' in st)
    check("★ 主链路接了唱后（在 presence 之后）",
          'polish_stage(job, "post"' in st)
    i1 = st.index('polish_stage(job, "pre"')
    i2 = st.index('polish_stage(job, "post"')
    check("唱前确实排在唱后之前（顺序不能反）", i1 < i2)

    check("前端有微调条（#finePolish）", 'id="finePolish"' in html)
    check("前端有两个方向的按钮", "finePolish('pre')" in html
          and "finePolish('post')" in html)
    check("前端有力度滑块", 'id="finePolAmt"' in html)
    check("前端载入音频后把微调条显示出来",
          '$("#finePolish").style.display' in js)
    check("前端把'没修=正常'说清楚（不许让用户以为坏了）",
          "没有一项达标" in js)
    check("前端修完会再量一遍（闭环要看得见）", "finePolMeasure" in js
          and "await finePolMeasure()" in js)

    # ---- 试听（干湿 A/B）----
    seg3 = sv[sv.index('/api/fine/polish/audition"'):]
    seg3 = seg3[:seg3.index("if path ==", 10)]
    check("★ /api/fine/polish/audition 走了 _fine_path", "_fine_path" in seg3)
    check("★ audition 的 only 只放行本方向真实步骤（野名字不能当'全跳过'）",
          "POLISH_STEPS.get(side" in seg3)
    check("studio 里有 polish_audition", "def polish_audition(" in st)
    check("★ 试听片段不进成品目录（它只是给你听的，不是产物）",
          "polish_audition" in st and "AUDITION_DIR" in st
          and "FINE_DIR" not in st[st.index("def polish_audition("):
                                   st.index("def _fine_out_path(")])
    check("★ 主链路把升调半音数传给唱后（formant 只认这个数）",
          'semi=pitch' in st)
    check("★ POST_STEPS 里有 formant 且排在最前（先摆正音色再修残留）",
          'POST_STEPS = ("formant"' in src)
    check("★ formant 不给半音数就一步不动（不许猜）",
          "没给升调半音数" in src)

    check("前端有试听条（#fineAudit）", 'id="fineAudit"' in html)
    check("★ 前端有干/湿滑条（拖动即时出声，不重算音频）", 'id="audMix"' in html)
    check("★ 干湿滑条只改音量（不是重新请求后端）",
          "function audCross()" in js and "AUD.wet.volume = t" in js
          and "AUD.dry.volume = 1.0 - t" in js)
    check("★ 两条音频要贴着放（不然比的是两个时间点）",
          "other.currentTime = a.currentTime" in js)
    check("前端有分步 solo 下拉", "AUD_STEPS" in js and "audFillSteps" in js)
    check("★ 换歌会把上一条试听停掉（不能放着上一首）",
          "audStop()" in js and '$("#fineAudit").style.display' in js)
    check("★ 换步骤/改力度会重跑试听且带防抖（拖滑条不能每格都算）",
          "function fineAuditRerun()" in js and "setTimeout" in js)


# ---------------------------------------------------------------------------
# B 唱前
# ---------------------------------------------------------------------------

def b_pre(P, path):
    sec("B 唱前：噪声底 / 隆隆 / 削波 / 爆音")
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    x = x.astype("float64")
    m0 = P.measure_pre(x, sr)

    y = P.highpass(x, sr)
    m1 = P.measure_pre(y, sr)
    check("★ 去隆隆：20~80Hz 掉了 %.1f dB（要 ≥3）" % (m0["rumble_db"] - m1["rumble_db"]),
          m0["rumble_db"] - m1["rumble_db"] >= 3.0)
    check("  去隆隆没动人声本体（|Δ| %.2f dB ≤ 0.3）" % abs(m1["sig_db"] - m0["sig_db"]),
          abs(m1["sig_db"] - m0["sig_db"]) <= 0.3)

    y2 = P.denoise(x, sr, 1.0)
    m2 = P.measure_pre(y2, sr)
    check("★ 去底噪：噪声底降了 %.1f dB（要 ≥2）" % (m0["floor_db"] - m2["floor_db"]),
          m0["floor_db"] - m2["floor_db"] >= 2.0)
    check("  去底噪没把人声啃掉（|Δ| %.2f dB ≤ 1.5）" % abs(m2["sig_db"] - m0["sig_db"]),
          abs(m2["sig_db"] - m0["sig_db"]) <= 1.5)
    check("  信噪比真的变好了（%.1f → %.1f）" % (m0["snr_db"], m2["snr_db"]),
          m2["snr_db"] > m0["snr_db"] + 1.0)

    y3 = P.declip(x, sr)
    m3 = P.measure_pre(y3, sr)
    check("★ 修削波：削波占比 %.5f → %.5f" % (m0["clip_ratio"], m3["clip_ratio"]),
          m0["clip_ratio"] > 1e-4 and m3["clip_ratio"] < m0["clip_ratio"] * 0.2)
    check("  修完不制造新的削波（峰值 %.3f ≤ 0.999）" % m3["peak"], m3["peak"] <= 0.999)
    check("  修削波没动人声本体（|Δ| %.2f）" % abs(m3["sig_db"] - m0["sig_db"]),
          abs(m3["sig_db"] - m0["sig_db"]) <= 0.5)

    y4 = P.declick(x, sr)
    m4 = P.measure_pre(y4, sr)
    check("★ 修爆音：%d → %d 个" % (m0["clicks"], m4["clicks"]),
          m0["clicks"] > 0 and m4["clicks"] < m0["clicks"])
    check("  修爆音没动人声本体（|Δ| %.2f）" % abs(m4["sig_db"] - m0["sig_db"]),
          abs(m4["sig_db"] - m0["sig_db"]) <= 0.5)

    # 干净素材不许乱动（"没毛病就别治"是这套修法的底线）
    clean = _harm(np.full(int(SR * 1.5), 250.0), int(SR * 1.5), amp=0.25)
    mc0 = P.measure_pre(clean, sr)
    mc1 = P.measure_pre(P.declip(clean, sr), sr)
    check("★ 干净素材上 declip 一动不动（不许乱修）",
          mc1["clip_ratio"] == 0.0 and abs(mc1["sig_db"] - mc0["sig_db"]) < 1e-6,
          "sig %.4f → %.4f" % (mc0["sig_db"], mc1["sig_db"]))
    return m0


# ---------------------------------------------------------------------------
# C 唱后
# ---------------------------------------------------------------------------

def c_post(P, path):
    sec("C 唱后：齿音 / 刺耳 / 高音柔化 / 拼接 / 限幅")
    import numpy as np
    import soundfile as sf
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    x = x.astype("float64")
    m0 = P.measure_post(x, sr)

    y = P.deess(x, sr, 1.0)
    m1 = P.measure_post(y, sr)
    check("★ 去齿音：超出量 %.1f → %.1f dB（要降 ≥2）"
          % (m0["sib_excess_db"], m1["sib_excess_db"]),
          m0["sib_excess_db"] - m1["sib_excess_db"] >= 2.0)
    check("  去齿音没动中低基准（|Δ| %.2f ≤ 0.5）" % abs(m1["mid_db"] - m0["mid_db"]),
          abs(m1["mid_db"] - m0["mid_db"]) <= 0.5)

    # 刺耳：单独造（上面那条素材的 2~5k 没有突出来）
    n = int(SR * 3.0)
    t = np.arange(n) / float(SR)
    v = _harm(np.full(n, 240.0), n, nk=12, amp=0.25)
    from scipy.signal import butter, sosfilt
    sos = butter(4, [2000 / (SR / 2), 5000 / (SR / 2)], btype="band", output="sos")
    # ★ 爆发的时长必须**短于基线窗口的一半**（0.3s ⇒ 要 <0.15s）：
    #   比它长的话，滑动中位线自己就把爆发吃进去了 ⇒ 超出量算出来是 0
    #   ⇒ 判据假红（第一版写了 0.25s，量出来只有 0.1dB）。
    burst = np.zeros(n)
    for st in (0.5, 0.9, 1.3, 1.7, 2.1, 2.5):
        a, b = int(st * SR), int(st * SR) + int(0.07 * SR)
        burst[a:b] = 1.0
    v = v + 3.5 * sosfilt(sos, v) * burst + 0.004 * np.random.default_rng(5).standard_normal(n)
    h0 = P.measure_post(v, sr)
    h1 = P.measure_post(P.deharsh(v, sr, 1.0), sr)
    # ★ 阈值取 0.8 而不是 1.5：剂量是按"带中心 3.3dB"给的，而衰减窗是余弦的
    #   （硬边会出金属声）⇒ **带内平均**只吃到中心的一半。拿带平均当尺子
    #   就只有 ~0.95dB。这不是修得不够，是尺子的口径 —— 写 1.5 会假红。
    check("★ 去刺耳：超出量 %.1f → %.1f dB（要降 ≥0.8）"
          % (h0["harsh_excess_db"], h1["harsh_excess_db"]),
          h0["harsh_excess_db"] - h1["harsh_excess_db"] >= 0.8)
    check("  去刺耳没动中低基准（|Δ| %.2f ≤ 0.5）" % abs(h1["mid_db"] - h0["mid_db"]),
          abs(h1["mid_db"] - h0["mid_db"]) <= 0.5)

    y2 = P.belt_soften(x, sr, 1.0)
    m2 = P.measure_post(y2, sr)
    check("★ 高音柔化：高音段亮度 %.1f → %.1f dB（要降 ≥0.8）"
          % (m0["belt_rel_db"], m2["belt_rel_db"]),
          m0["belt_rel_db"] - m2["belt_rel_db"] >= 0.8)
    check("  高音柔化没动高音段的基准（|Δ| %.2f ≤ 0.5）"
          % abs(m2["belt_mid_db"] - m0["belt_mid_db"]),
          abs(m2["belt_mid_db"] - m0["belt_mid_db"]) <= 0.5)
    check("  低音段没被误伤（f0 < 380Hz 的帧不动）",
          abs(m2["mid_db"] - m0["mid_db"]) <= 0.5,
          "mid %.2f → %.2f" % (m0["mid_db"], m2["mid_db"]))

    y3 = P.heal_splices(x, sr)
    m3 = P.measure_post(y3, sr)
    check("★ 修拼接：%d → %d 个" % (m0["splices"], m3["splices"]),
          m0["splices"] > 0 and m3["splices"] < m0["splices"])
    check("  修拼接没动中低基准（|Δ| %.2f ≤ 0.5）" % abs(m3["mid_db"] - m0["mid_db"]),
          abs(m3["mid_db"] - m0["mid_db"]) <= 0.5)

    loud = x / max(1e-9, np.max(np.abs(x))) * 1.6
    out = P.soft_limit(loud, sr)
    check("★ 软限幅把 %.3f 的峰值压回 %.3f（≤0.985）"
          % (float(np.max(np.abs(loud))), float(np.max(np.abs(out)))),
          float(np.max(np.abs(out))) <= 0.985)
    check("  限幅用的是软拐（不是硬削 ⇒ 不制造新高频）",
          "tanh" in read(os.path.join(HERE, "stages", "polish.py")))
    return m0


# ---------------------------------------------------------------------------
# D 拼接
# ---------------------------------------------------------------------------

def d_concat(P):
    sec("D 拼接：等功率交叉淡化 + 零交叉对齐")
    import numpy as np
    a = _harm(np.full(int(SR * 0.5), 220.0), int(SR * 0.5), amp=0.3)
    b = _harm(np.full(int(SR * 0.5), 330.0), int(SR * 0.5), amp=0.3) + 0.30
    hard = P.measure_post(np.concatenate([a, b]), SR)
    soft = P.measure_post(P.concat([a, b], SR, 12.0), SR)
    check("★ 硬拼有接缝、交叉淡化没有（%d → %d）"
          % (hard["splices"], soft["splices"]),
          hard["splices"] > 0 and soft["splices"] == 0)
    src = read(os.path.join(HERE, "stages", "polish.py"))
    check("交叉淡化用的是等功率（sqrt），不是线性（线性接缝会掉 6dB）",
          "np.sqrt(1.0 - w)" in src and "np.sqrt(w)" in src)
    check("接缝做了零交叉对齐", "_snap_zero" in src)
    st = np.concatenate([a[:, None], (a * 0.8)[:, None]], axis=1)
    z = P.concat([st, st], SR, 12.0)
    check("立体声也能拼（形状不丢）", z.ndim == 2 and z.shape[1] == 2, str(z.shape))


# ---------------------------------------------------------------------------
# E CLI 闭环 + 负对照
# ---------------------------------------------------------------------------

def _cli(args, timeout=900):
    import studio
    cmd = '"%s" -s "%s" %s' % (studio.PY, os.path.join(HERE, "stages", "polish.py"),
                               " ".join(args))
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout, cwd=HERE, shell=True)
    return p.returncode, (p.stdout or ""), (p.stderr or "")


def _rep(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def e_cli(pre_path, post_path):
    sec("E CLI 闭环：先量 → 修 → 复测 → 不达标不写 wav")
    import numpy as np

    out = os.path.join(TMP, "pre_out.wav")
    rep = os.path.join(TMP, "pre_out.json")
    rc, so, se = _cli(['pre', '"%s"' % pre_path, '"%s"' % out, '"%s"' % rep])
    r = _rep(rep)
    check("pre 退出码 0 且写了成品", rc == 0 and os.path.isfile(out),
          (so.strip() or se.strip())[:90])
    # ★ 盯噪声底，不盯信噪比：snr 的分子是整轨响度，去掉隆隆之后整轨变轻
    #   ⇒ snr 会**下降**（实测 8.3 → 0.7），那不是变差。噪声底才是唱前要的。
    check("★ pre 真的把噪声底压下去了（%.1f → %.1f dB）"
          % ((r or {}).get("before", {}).get("floor_db", 0),
             (r or {}).get("after", {}).get("floor_db", 0)),
          bool(r) and r["after"]["floor_db"] < r["before"]["floor_db"] - 2.0)
    names = [s.get("name") for s in (r or {}).get("steps", []) if s.get("kept")]
    check("pre 至少动了 3 项（%s）" % "、".join(x for x in names if x),
          len(names) >= 3)
    check("★ 每一步都记了 gain 和 harm（不是只记一句『修过了』）",
          all(("gain" in s and "harm" in s)
              for s in (r or {}).get("steps", []) if s.get("kept")
              and s.get("name") != "level"))

    out2 = os.path.join(TMP, "post_out.wav")
    rep2 = os.path.join(TMP, "post_out.json")
    rc, so, se = _cli(['post', '"%s"' % post_path, '"%s"' % out2, '"%s"' % rep2])
    r2 = _rep(rep2)
    check("post 退出码 0 且写了成品", rc == 0 and os.path.isfile(out2),
          (so.strip() or se.strip())[:90])
    b, a = (r2 or {}).get("before", {}), (r2 or {}).get("after", {})
    check("★ post 把齿音超出压下去了（%.1f → %.1f）"
          % (b.get("sib_excess_db", 0), a.get("sib_excess_db", 0)),
          a.get("sib_excess_db", 0) < b.get("sib_excess_db", 0) - 1.5)
    check("★ post 把拼接点修掉了（%d → %d）" % (b.get("splices", 0), a.get("splices", 0)),
          a.get("splices", 0) < b.get("splices", 0))

    # ---- 干净素材：一步都不该动，且**不写 wav** ----
    clean = wav("clean_mat", _harm(np.full(int(SR * 1.5), 250.0),
                                   int(SR * 1.5), amp=0.25))
    out3 = os.path.join(TMP, "clean_out.wav")
    rep3 = os.path.join(TMP, "clean_out.json")
    rc, so, se = _cli(['post', '"%s"' % clean, '"%s"' % out3, '"%s"' % rep3])
    r3 = _rep(rep3)
    check("★ 干净素材上 post 报告 applied=False", bool(r3) and not r3.get("applied"),
          (r3 or {}).get("reason"))
    check("★ 没修就**不写 wav**（调用方拿它当成品会读空文件，前辈踩过）",
          not os.path.isfile(out3))
    check("  没修也算成功（退出码 0，SKIP 是正常结果）", rc == 0, so.strip()[:60])

    sec("E2 负对照：**只留那一步**跑，指标才好转 —— 证明好转是它干的")
    #
    # ★★ 为什么不是"摘掉那一步 ⇒ 指标不变"：第一版就是这么写的，结果 4 条里
    #    4 条没变红。查下来是**真问题**，不是尺子坏了：唱前这几步的效果
    #    **互相重叠** —— 谱门降噪顺带把 45Hz 隆隆也压下去了（rumble 降 17dB），
    #    也把削波的峰值压下来了（clip_ratio 归零）。于是"摘掉 hp"之后隆隆照样降，
    #    看起来像 hp 没干活，其实是被 denoise 替它干了。
    #    ⇒ 那把尺子**量不出归因**，是假尺子（铁律 11b），必须换。
    #
    # 换成的尺子：**只留那一步**（其余全 skip）。这时指标若好转，只可能是
    # 它干的 —— 归因是干净的。再配一条"全 skip"当底：一步都不许跑。
    #
    # ★ 2026-10-26 又加了一类期望："blocked" —— 这一步在这份素材上**必须被拦下**。
    #   为什么会有这种期望：现在唱前每一步都过了"内容护栏"（内容掉了就整步撤销）
    #   和"密度闸门"（报点离谱就不动手）。有两步在这份素材上**确实会伤内容**：
    #     · denoise：它的谱门在频率方向做了 7-bin 的中值滤波 —— @44.1kHz 每 bin
    #       43Hz，而这份素材基频 220Hz（谐波间隔 ≈ 5 个 bin）⇒ 谐波峰在 7 个 bin
    #       的窗里是少数派，**被当成噪声抹掉**。实测内容掉 9.02dB，护栏拦住了它。
    #     · declick（**2026-10-26 二修前**）：45 秒干净素材报 292 个点这种事
    #       （详见 verify_declick 文件头），这份 4 秒素材报 8 个点 = 每 10 秒
    #       20 个，密度闸门拦住它。
    #   ⇒ 对它们改断言"必须不许动手"比"必须改善"更诚实，而且一样硬：
    #     谁要是把护栏放宽让它闯过去，这里立刻红。
    #
    #   ★ 二修后 declick 从 blocked 换回 improve：判据换掉之后它在这份素材上
    #     只报 **2 个点**（就是素材里真注入的那两个 0.8 幅度尖峰）、harm=0.00、
    #     点击数 2 → 0。"它必须被拦下"的前提已经不成立了 —— 拦它的理由（报点
    #     离谱）消失了。现在该断言的是**它真把爆音修掉了且没伤内容**，这比
    #     "不许动手"更硬：谁把判据换回旧的，这里会因为报点暴涨而红。
    steps_of = {"pre": ("declip", "declick", "hp", "denoise", "level"),
                "post": ("deess", "deharsh", "belt", "heal", "limit")}
    cases = [
        ("pre", "hp", "rumble_db", lambda b, a: b - a >= 3.0, "improve"),
        ("pre", "denoise", "floor_db", lambda b, a: b - a >= 2.0, "blocked"),
        ("pre", "declip", "clip_ratio", lambda b, a: b > 1e-4 and a < b * 0.2, "improve"),
        ("pre", "declick", "clicks", lambda b, a: b > 0 and a < b, "improve"),
        ("post", "deess", "sib_excess_db", lambda b, a: b - a >= 2.0, "improve"),
        ("post", "belt", "belt_rel_db", lambda b, a: b - a >= 0.8, "improve"),
        ("post", "heal", "splices", lambda b, a: b > 0 and a < b, "improve"),
    ]
    for side, step, key, better, expect in cases:
        p_in = pre_path if side == "pre" else post_path
        others = ",".join(s for s in steps_of[side] if s != step)
        o1 = os.path.join(TMP, "only_%s.wav" % step)
        j1 = os.path.join(TMP, "only_%s.json" % step)
        _cli([side, '"%s"' % p_in, '"%s"' % o1, '"%s"' % j1, "--skip %s" % others])
        r1 = _rep(j1)
        b1 = (r1 or {}).get("before", {}).get(key)
        a1 = (r1 or {}).get("after", {}).get(key)
        kept = [s.get("name") for s in (r1 or {}).get("steps", []) if s.get("kept")]
        if expect == "improve":
            ok = bool(r1) and b1 is not None and a1 is not None and better(b1, a1)
            scheck("%-8s 只留它时 %s 好转（%s → %s）" % (step, key, b1, a1), ok)
            check("%-8s 的账里确实只有它被记为已修" % step, kept == [step], str(kept))
        else:
            # ★ 必须不许动手，而且理由得写进报告（静默跳过不算）
            check("%-8s 必须被护栏拦下（它会伤内容，不许闯过去）" % step,
                  step not in kept, "被记为已修的：%s" % (kept or "（没有）"))
            notes = " / ".join((r1 or {}).get("notes") or [])
            harm = [s.get("harm") for s in (r1 or {}).get("steps", [])
                    if s.get("name") == step and s.get("harm") is not None]
            check("%-8s 拦下的理由要写在报告里（不许静默不动）" % step,
                  bool(notes) or bool(harm),
                  "harm=%s notes=%s" % (harm[:1], notes[:80] or "（空）"))

    # 底：一步都不许跑
    for side, p_in in (("pre", pre_path), ("post", post_path)):
        o0 = os.path.join(TMP, "none_%s.wav" % side)
        j0 = os.path.join(TMP, "none_%s.json" % side)
        _cli([side, '"%s"' % p_in, '"%s"' % o0, '"%s"' % j0,
              "--skip %s" % ",".join(steps_of[side])])
        r0 = _rep(j0)
        scheck("%s 全部 skip ⇒ 一步都没修、也没写 wav" % side,
               bool(r0) and not r0.get("applied") and not os.path.isfile(o0),
               (r0 or {}).get("reason"))


# ---------------------------------------------------------------------------
# G 试听（干湿 A/B）+ 共振峰
# ---------------------------------------------------------------------------

def g_audition(P, post_path, pre_path):
    sec("G 试听（切一小段出修前/修后）+ 共振峰")
    import numpy as np
    import soundfile as sf

    dry = os.path.join(TMP, "aud_dry.wav")
    wet = os.path.join(TMP, "aud_wet.wav")
    rep = os.path.join(TMP, "aud.json")

    # ---- G1 正常：两条都要出来，而且真的不一样 ----
    rc, so, se = _cli(['audition', '"%s"' % post_path, '"%s"' % dry,
                       '"%s"' % wet, '"%s"' % rep, '--side post', '--sec 4'],
                      timeout=900)
    r = _rep(rep)
    check("audition 退出码 0", rc == 0, (so.strip() or se.strip())[:90])
    check("★ 修前 / 修后两条都写出来了（前端要有两条才做得了 A/B）",
          os.path.isfile(dry) and os.path.isfile(wet))
    if os.path.isfile(dry) and os.path.isfile(wet):
        a, _ = sf.read(dry)
        b, _ = sf.read(wet)
        same = len(a) == len(b) and float(np.abs(a - b).max()) < 1e-9
        check("★ 这一条素材上修前 ≠ 修后（不然拖干湿滑条听不出差别）", not same,
              "长度 %d/%d" % (len(a), len(b)))
        check("★ 片段长度确实是 8 秒档（不是整首）",
              abs(len(a) / float(SR) - (r or {}).get("sec", 0)) < 0.1,
              "%.2fs" % (len(a) / float(SR)))
        check("★ 报告里带了 from（告诉用户听的是第几秒）",
              bool(r) and r.get("from") is not None)
    if r:
        check("★ 报告里带了 applied 和 steps（界面要照实说修没修）",
              "applied" in r and "steps" in r)

    # ---- G2 分步 solo：只跑那一步 ----
    d2 = os.path.join(TMP, "aud2_dry.wav")
    w2 = os.path.join(TMP, "aud2_wet.wav")
    r2 = os.path.join(TMP, "aud2.json")
    _cli(['audition', '"%s"' % post_path, '"%s"' % d2, '"%s"' % w2,
          '"%s"' % r2, '--side post', '--sec 4', '--only deess'], timeout=900)
    j2 = _rep(r2)
    kept = [s.get("name") for s in (j2 or {}).get("steps", []) if s.get("kept")]
    skipped = [s.get("name") for s in (j2 or {}).get("steps", [])
               if s.get("skipped")]
    check("★ --only deess ⇒ 只有 deess 有机会跑（别的都记成 skipped）",
          bool(j2) and set(skipped) >= {"deharsh", "belt", "heal"},
          "skipped=%s kept=%s" % (skipped, kept))
    check("★ --only 传进去之后报告里会原样带回来（界面要用）",
          bool(j2) and j2.get("only") == "deess", str((j2 or {}).get("only")))
    # ★ 只跑那一步 ⇒ 账里"有机会跑"的不能出现别的步骤（kept 只能是 deess）
    check("★ 分步试听：账里被记为已修的只能是指定的那一步",
          bool(j2) and set(kept) <= {"deess"}, "kept=%s" % kept)

    # ---- G3 挑段：问题集中在尾部时，片段起点不能落在开头 ----
    # ★ 素材有讲究，踩过两次坑：
    #   ① 干净段在 4.5k 以上**一点能量都没有** ⇒ 只剩浮点噪声，dB 抖得比真齿音
    #      还厉害（实测干净段 3.37、齿音段 2.17 —— 完全反了）⇒ 必须垫一层底噪。
    #   ② 齿音不能铺成 2 秒的长音：它比 0.3 秒的基线窗还长 ⇒ **被自己的基线吃掉**
    #      （跟上一轮去刺耳那条同一个坑）。真齿音是一下一下的 ⇒ 用一小簇短爆发。
    n = int(SR * 12.0)
    rng3 = np.random.default_rng(5)
    v = _harm(np.full(n, 240.0), n, nk=10, amp=0.2) + 0.003 * rng3.standard_normal(n)
    for st in (9.0, 9.4, 9.8, 10.2, 10.6):
        a0, b0 = int(st * SR), int(st * SR) + int(0.08 * SR)
        tt = np.arange(b0 - a0) / float(SR)
        v[a0:b0] += (0.7 * (np.sin(2 * np.pi * 6200 * tt)
                            + np.sin(2 * np.pi * 8100 * tt)) * np.hanning(b0 - a0))
    p3 = wav("aud_pick", v / max(1e-9, np.abs(v).max()) * 0.8)
    d3 = os.path.join(TMP, "aud3_dry.wav")
    w3 = os.path.join(TMP, "aud3_wet.wav")
    r3 = os.path.join(TMP, "aud3.json")
    _cli(['audition', '"%s"' % p3, '"%s"' % d3, '"%s"' % w3,
          '"%s"' % r3, '--side post', '--sec 3', '--only deess'], timeout=900)
    j3 = _rep(r3)
    fr = (j3 or {}).get("from")
    check("★ 试听挑的是'问题最重'的那一段（齿音在 9~11 秒 ⇒ 起点不能落在 0）",
          fr is not None and fr >= 6.0,
          "from=%s" % fr)

    # ---- G4 没达标时：wet 必须还在（前端永远有两条能放），并且照实说 ----
    d4 = os.path.join(TMP, "aud4_dry.wav")
    w4 = os.path.join(TMP, "aud4_wet.wav")
    r4 = os.path.join(TMP, "aud4.json")
    clean = _harm(np.full(int(SR * 4.0), 240.0), int(SR * 4.0), nk=10, amp=0.3)
    p4 = wav("aud_clean", clean)
    _cli(['audition', '"%s"' % p4, '"%s"' % d4, '"%s"' % w4,
          '"%s"' % r4, '--side post', '--sec 3'], timeout=900)
    j4 = _rep(r4)
    check("★ 没达标时 wet 文件照样在（不然前端一条都放不了）", os.path.isfile(w4))
    check("★ 没达标时 applied=false 且有 reason（不许让用户白拖滑条）",
          bool(j4) and j4.get("applied") is False and bool(j4.get("reason")),
          str((j4 or {}).get("reason"))[:70])

    # ---- G5 共振峰：不猜 ----
    v5 = _harm(np.full(int(SR * 3.0), 240.0), int(SR * 3.0), nk=14, amp=0.3)
    y0 = P.formant_shift(v5, SR, amount=1.0, semitones=None)
    y1 = P.formant_shift(v5, SR, amount=1.0, semitones=0)
    y2 = P.formant_shift(v5, SR, amount=1.0, semitones=5)
    y3 = P.formant_shift(v5, SR, amount=1.0, semitones="瞎写")
    check("★ 共振峰：没给半音数 ⇒ 一个采样都不动", float(np.abs(y0 - v5).max()) == 0.0)
    check("★ 共振峰：半音数 0 ⇒ 不动", float(np.abs(y1 - v5).max()) == 0.0)
    check("★ 共振峰：半音数给瞎值 ⇒ 不动（不能抛异常也不能乱搬）",
          float(np.abs(y3 - v5).max()) == 0.0)
    check("★ 共振峰：给了半音数 ⇒ 真的动了", float(np.abs(y2 - v5).max()) > 0.0)
    m1 = P.measure_post(v5, SR)
    m2 = P.measure_post(y2, SR)
    check("★ 共振峰搬完响度不许被改（改的是形状，不是音量）",
          abs((m2.get("loud_db") or 0) - (m1.get("loud_db") or 0)) <= 0.5,
          "%.2f → %.2f" % (m1.get("loud_db") or 0, m2.get("loud_db") or 0))
    check("★ 共振峰搬完不许搬出削波", (m2.get("peak") or 0) <= 0.999,
          "峰值 %.3f" % (m2.get("peak") or 0))

    # ---- G6 负对照：post 不给 semi ⇒ formant 那一步必须被记成 skipped ----
    o6 = os.path.join(TMP, "g6.wav")
    j6 = os.path.join(TMP, "g6.json")
    _cli(['post', '"%s"' % post_path, '"%s"' % o6, '"%s"' % j6], timeout=900)
    r6 = _rep(j6)
    fm = [s for s in (r6 or {}).get("steps", []) if s.get("name") == "formant"]
    check("★ 主链路不给半音数 ⇒ formant 记成 skipped 且说明了原因",
          bool(fm) and fm[0].get("skipped") is True and bool(fm[0].get("reason")),
          str((fm or [{}])[0].get("reason"))[:60])


# ---------------------------------------------------------------------------
# F 真 HTTP
# ---------------------------------------------------------------------------

def _http(path, body=None, timeout=180):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_err(path, body=None):
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


def _listen(port, timeout=0.4):
    import socket
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def start_service():
    import studio
    if not os.path.isfile(studio.PY):
        return None, "找不到 GSV 运行时：%s" % studio.PY
    if _listen(PORT):
        return None, "端口 %d 已被占用（换 VS_POLISH_PORT）" % PORT
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


def f_http(pre_path, post_path):
    sec("F 真 HTTP（自起独立端口 %d，不碰用户正在用的 8756）" % PORT)
    proc, info = start_service()
    if not proc:
        check("在 %d 起一份独立服务" % PORT, False, info)
        return
    made = []
    try:
        h = _http("/api/health")
        check("服务活着", h.get("ok") is True, "pid=%s" % h.get("pid"))
        fresh = float(h.get("code_mtime") or 0) <= float(h.get("started_ts") or 0) + 2
        check("服务跑的是当前代码（code_mtime <= started_ts）", fresh,
              "code=%s started=%s" % (h.get("code_mtime"), h.get("started_ts")))

        # 功能开关的清单接口：界面照它画勾选框，所以它必须真的在、而且和
        # 真正执行的步骤表一致（漂了会"少一个勾选框 / 多一个勾了没反应的"）。
        st = _http("/api/fine/polish/steps")
        pre = [x.get("name") for x in (st.get("pre") or [])]
        post = [x.get("name") for x in (st.get("post") or [])]
        real = _steps_from_polish_py()
        check("GET /api/fine/polish/steps 给出唱前/唱后清单",
              bool(pre) and bool(post), "pre=%d post=%d" % (len(pre), len(post)))
        check("接口清单与 polish.py 完全一致（含顺序）",
              tuple(pre) == real.get("pre") and tuple(post) == real.get("post"),
              "pre=%s post=%s" % (pre, post))

        import studio
        up = studio.UPLOADS
        os.makedirs(up, exist_ok=True)
        tag = "vs_pol_gate"
        src_p = os.path.join(up, "%s_post.wav" % tag)
        shutil.copyfile(post_path, src_p)
        made.append(src_p)

        m = _http("/api/fine/polish", {"path": src_p})
        check("POST /api/fine/polish 量得出数", bool((m or {}).get("measure")),
              str((m or {}).get("measure", {}).get("post", {}).get("sib_excess_db")))
        mm = (m or {}).get("measure") or {}
        check("量出来的 pre / post 两段都在",
              bool(mm.get("pre")) and bool(mm.get("post")))

        code, why = _http_err("/api/fine/polish", {"path": ""})
        check("★ 空路径被挡（%s）" % code, code == 400, why)
        code, why = _http_err("/api/fine/polish",
                              {"path": r"C:\Windows\System32\drivers\etc\hosts"})
        check("★ 项目外的路径被挡（%s）" % code, code == 400, why)
        code, why = _http_err("/api/fine/polish/apply",
                              {"path": src_p, "side": "瞎写"})
        check("★ 不认识的方向被挡（%s）" % code, code == 400, why)

        j = _http("/api/fine/polish/apply", {"path": src_p, "side": "post",
                                             "amount": 1.0})
        check("POST /api/fine/polish/apply 出成品",
              bool(j.get("path")) and os.path.isfile(j["path"] or ""),
              j.get("name"))
        if j.get("path"):
            made.append(j["path"])
            d = os.path.dirname(j["path"])
            if os.path.basename(d) == tag + "_post":
                made.append(d)
        check("★ 成品不覆盖上一版（名字里带『唱后微调』）",
              "唱后微调" in (j.get("name") or ""), j.get("name"))
        check("apply 把每一步的账都带回来了（界面要能说清动了什么）",
              bool(j.get("steps")), "%d 条" % len(j.get("steps") or []))

        # ---- 试听接口 ----
        au = _http("/api/fine/polish/audition",
                   {"path": src_p, "side": "post", "sec": 4}, timeout=300)
        check("POST /api/fine/polish/audition 出两条片段",
              bool(au.get("dry")) and os.path.isfile(au.get("dry") or "")
              and bool(au.get("wet")) and os.path.isfile(au.get("wet") or ""),
              "from=%s sec=%s applied=%s" % (au.get("from"), au.get("sec"),
                                             au.get("applied")))
        check("★ 试听片段不进成品目录（放在 cache 下，不是 精修成品/）",
              "polish_audition" in (au.get("dry") or ""),
              (au.get("dry") or "").split(os.sep)[-1])
        for bad in ({"path": "", "side": "post"},
                    {"path": src_p, "side": "瞎写"},
                    {"path": src_p, "side": "post", "only": "没有这一步"}):
            c, w = _http_err("/api/fine/polish/audition", bad)
            check("★ 试听的坏参数被挡（%s / %s）" % (bad.get("side"), bad.get("only")),
                  c == 400, w)
        au2 = _http("/api/fine/polish/audition",
                    {"path": src_p, "side": "post", "only": "deess", "sec": 4},
                    timeout=300)
        kept2 = [s.get("name") for s in (au2 or {}).get("steps", []) if s.get("kept")]
        check("★ 分步试听只跑那一步（报告里 only 原样带回来，且账里只有它）",
              (au2 or {}).get("only") == "deess" and set(kept2) <= {"deess"},
              "only=%s kept=%s" % ((au2 or {}).get("only"), kept2))
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        for p in made:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.isfile(p):
                    os.remove(p)
            except BaseException:
                pass


# ---------------------------------------------------------------------------

def _steps_from_polish_py():
    """从 stages/polish.py 里读出真正的步骤表（权威来源是它，不是界面）。"""
    txt = io.open(os.path.join(HERE, "stages", "polish.py"), encoding="utf-8").read()
    out = {}
    for side, name in (("pre", "PRE_STEPS"), ("post", "POST_STEPS")):
        m = re.search(r"^%s\s*=\s*\(([^)]*)\)" % name, txt, re.M)
        out[side] = tuple(re.findall(r'"([a-z_]+)"', m.group(1))) if m else ()
    return out


def h_switch_list():
    """H 段：界面「功能开关」那份清单，必须跟真正执行的步骤表一模一样。

    ★ 为什么专门立一条：清单是**手写两份**（polish.py 执行用、studio 给界面画勾选框用）。
      两边漂了不会报错，只会：① 界面少一个勾选框 ⇒ 那一步用户永远关不掉；
      ② 界面多一个不存在的名字 ⇒ 勾了没反应（用户以为功能坏了）。
      两种都极安静，只能靠判据抓。
    """
    sec("H 功能开关的清单（界面画勾选框用的那一份）")
    sys.path.insert(0, HERE)
    import studio  # noqa: E402
    real = _steps_from_polish_py()
    check("能从 stages/polish.py 读出步骤表（读不到说明格式变了）",
          bool(real.get("pre")) and bool(real.get("post")), real)
    check("唱前清单一致（含顺序）",
          tuple(studio.POLISH_PRE_STEPS) == real.get("pre"),
          "studio=%s polish=%s" % (list(studio.POLISH_PRE_STEPS), list(real.get("pre") or ())))
    check("唱后清单一致（含顺序）",
          tuple(studio.POLISH_POST_STEPS) == real.get("post"),
          "studio=%s polish=%s" % (list(studio.POLISH_POST_STEPS), list(real.get("post") or ())))

    allp = tuple(studio.POLISH_PRE_STEPS) + tuple(studio.POLISH_POST_STEPS)
    no_lab = [s for s in allp
              if not studio.POLISH_STEP_LABEL.get(s)
              or studio.POLISH_STEP_LABEL.get(s) == s]
    check("每一步都有中文标签（界面上不许露出 declip 这种内部名）",
          not no_lab, "缺标签：%s" % no_lab)
    # ★ 上面那条得能红：随便编一个不存在的步骤名，标签表**必须**给不出东西。
    #   （否则"每步都有标签"这句话是空的 —— 表里查不到时若兜底等于原名，判据就永远绿。）
    check("★ 自检：编一个假步骤名，标签表必须给不出来（否则上面那条是假绿）",
          not studio.POLISH_STEP_LABEL.get("declip_不存在的步"),
          studio.POLISH_STEP_LABEL.get("declip_不存在的步"))


def main():
    print("唱前/唱后微调门禁：stages/polish.py + /api/fine/polish* + 精修台微调条",
          flush=True)
    os.makedirs(TMP, exist_ok=True)
    try:
        import stages.polish as P
    except Exception as e:                                   # noqa: BLE001
        print("  [FAIL] 导入 stages.polish 失败：%s" % e)
        return 1

    a_wiring()
    pre_path = mat_pre()
    post_path = mat_post()
    b_pre(P, pre_path)
    c_post(P, post_path)
    d_concat(P)
    e_cli(pre_path, post_path)
    g_audition(P, post_path, pre_path)
    f_http(pre_path, post_path)
    h_switch_list()

    sec("负对照自检账")
    print("（自检账不计入正式判据）")
    check("★ 所有负对照都真的改变了行为（有一条没变 = 那条是假尺子）",
          _SFAIL[0] == 0, "没通过的：%d 条" % _SFAIL[0])

    print("\n" + "=" * 60)
    print("微调门禁：%d 通过 / %d 失败" % (N_PASS, N_FAIL))
    if N_FAIL:
        print("★ 有判红项")
    print("=" * 60)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
