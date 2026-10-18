r"""门禁：Seed-VC 角色微调 + 44.1k 精修（新增的那一整块）。

为什么单独一关：它**坏得特别安静** ——
  · 引擎目录不在，界面只写一句"用不了"，任务照样能提交别的、成品照样出；
  · 步数解析错一位，就把 ft800 当成 ft1000 给用户挑（模型照样能跑，只是选错了）；
  · 交叉合成低频/高频接反，成品**更糊**，但任务成功、文件在、时长对；
  · 素材过滤写松了，训练集里混进一堆静音碎片，微调训出来就是噪声。
既有 14 关没有一条会红 ⇒ 必须专门盯。

判据四段，**每段都配负对照**（★ 标记的就是"换坏版本必须变红"的那条）：
  A 状态层（纯 Python）：enabled / 步数解析 / 默认档 / 空角色
  B CLI（RVC 解释器跑 seedvc_cli.py）：prep 过滤 / prompt 硬顶 / cross 频带归属
  C 接口（连 8756 常驻服务）：状态 / 换档 / 拒绝不存在的档
  D 浏览器（真鼠标）：训练页档位下拉渲染；真鼠标点「开始微调」提交的确实是
    kind=seedvc + steps=600（fetch 被拦成假回包，**不会真占显卡**）

用法（必须 venv 跑：要 websocket）：
    <venv>/Scripts/python.exe -s verify_seedvc.py
"""
import contextlib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import wave

import numpy as np
import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import seedvc  # noqa: E402
import studio  # noqa: E402

RVC_PY = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI\runtime\python.exe"
CLI = os.path.join(HERE, "stages", "seedvc_cli.py")
BASE = "http://127.0.0.1:8756"
SR = 32000

OK, BAD = [], []

# 自己在库里种出来的探针角色（"没微调过但素材很多"）—— 跑完必须在 main 里删掉
PLANTED = []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


# ---------------------------------------------------------------- A 状态层

def section_a():
    print("\n=== A. 状态层（纯 Python） ===")
    ok, why = seedvc.enabled()
    check("本机 Seed-VC 可用（引擎目录 + 基础权重 + 配置都在）", ok and not why, why)

    # ★ 尺子自检：把根目录换成不存在的目录，enabled 必须变 False。
    # 这条红了才说明「ok=True」是在真的检查，而不是永远点头的摆设。
    old = seedvc.SEEDVC_ROOT
    try:
        seedvc.SEEDVC_ROOT = os.path.join(HERE, "_no_such_seedvc_dir")
        ok2, why2 = seedvc.enabled()
        check("★ 负对照：引擎目录不存在 → enabled=False 且给出原因（尺子会红）",
              (not ok2) and bool(why2), "ok=%s why=%s" % (ok2, why2[:60]))
    finally:
        seedvc.SEEDVC_ROOT = old

    db = studio.load_roles()
    role = None
    for r in db["roles"]:
        if (r.get("seedvc") or {}).get("ckpt"):
            role = r
            break
    if role is None:
        check("库里至少有一个做过微调的角色（否则后面的判据无从谈起）", False)
        return None
    rows = seedvc.ckpts(role)
    check("档位清单非空", len(rows) >= 1, "档位数=%d" % len(rows))

    want = [r for r in rows if r["step"] == 600 and not r["final"]]
    check("步数解析：DiT_epoch_00003_step_00600.pth → 600 步",
          bool(want) and want[0]["file"] == "DiT_epoch_00003_step_00600.pth",
          [r["file"] for r in rows])

    # ft_model.pth 是训练结束时那个模型，它的步数 = 这次训练的 max_steps，
    # **不能**拿 DiT 档里最大的那个猜（1000 步那次最后一个 DiT 档是 800）。
    fin = [r for r in rows if r["final"]]
    dit_steps = [r["step"] for r in rows if not r["final"]]
    check("★ 末档（ft_model.pth）的步数取自这次训练的总步数，不是 DiT 档里的最大值",
          bool(fin) and dit_steps and fin[0]["step"] == 1000 and max(dit_steps) == 800,
          "末档=%s DiT档=%s" % ([r["step"] for r in fin], dit_steps))

    sel = seedvc.selected(role)
    check("默认档 = 角色显式选的那一档（ft600）",
          bool(sel) and sel["step"] == 600, sel and sel["label"])

    fake = {"id": "r_deadbeef", "name": "没有微调过的角色", "seedvc": None}
    check("没微调过的角色：档位为空、默认档为 None",
          seedvc.ckpts(fake) == [] and seedvc.selected(fake) is None)
    check("没微调过的角色：数据集目录名按角色 id 生成（data_vs_<rid>）",
          os.path.basename(seedvc.dataset_dir(fake)) == "data_vs_r_deadbeef",
          seedvc.dataset_dir(fake))

    # warn 也要能量出来：段数少时必须提醒，段数够了就不能乱提醒。
    # ★ 段数现在是从**角色现有素材**算的（以前读的是「上次整理出来的数据集」，
    #   没跑过微调就恒为 0 —— 于是"我有 300 多条素材"在界面上显示成 0 段）。
    #   所以这里的假角色必须**自带 segments**，只塞 seedvc.segs 是骗不过去的。
    def _mk(n, dur=3.0, voiced=0.90):
        return {"id": "r_probe_segs", "name": "probe", "seedvc": None,
                "segments": [{"file": "s%04d.wav" % i, "dur": dur, "voiced": voiced}
                             for i in range(n)]}

    thin3, rich120 = seedvc.state(_mk(3)), seedvc.state(_mk(120))
    check("★ 素材太少时给出提醒（3 段必须提醒，120 段不能提醒）",
          bool(thin3["warn"]) and not rich120["warn"],
          "3段=%s 120段=%s" % (thin3["warn"], rich120["warn"]))
    check("段数是从角色素材里算出来的（3 段 / 120 段各自对得上）",
          thin3["dataset"]["segs"] == 3 and rich120["dataset"]["segs"] == 120,
          "3→%s 120→%s" % (thin3["dataset"]["segs"], rich120["dataset"]["segs"]))
    check("★ 负对照：同一个角色把素材清空 → 段数回到 0 且给出提醒（数字不是写死的）",
          seedvc.state(_mk(0))["dataset"]["segs"] == 0
          and bool(seedvc.state(_mk(0))["warn"]),
          "0段=%s" % seedvc.state(_mk(0))["warn"])

    # 正面对照（本次修的那个 bug）：库里**没微调过**但素材很多的那个角色，
    # 界面上必须显示它的真实素材数，而不是 0。
    fresh = None
    for r in db["roles"]:
        if not (r.get("seedvc") or {}).get("ckpt") and len(r.get("segments") or []) >= 30:
            fresh = r
            break
    if fresh is None:
        fresh = _plant_untrained()
    # 以前这里写的是"库里得有一个没微调过、素材 ≥30 段的角色，没有就红"——
    # 判据挂在用户的数据长什么样上面：2026-10-13 给大帝真跑了一次微调，
    # 库里就再没有符合条件的角色了，门禁红在"数据前提"，跟代码好坏没关系。
    # 现在改成**自己种一个**（硬链接复用别人已有的切片，不占额外磁盘），跑完删。
    if fresh is None:
        check("★ 没微调过、素材 ≥30 段的角色可用（自己没种出来就说明判据失效）", False)
    else:
        st_f = seedvc.state(fresh)
        want = len([s for s in fresh["segments"]
                    if (s.get("dur") or 0) >= seedvc.PREP_MIN_DUR
                    and (s.get("voiced") or 0) >= seedvc.PREP_MIN_VOICED])
        check("★ 没微调过的角色：显示的素材数 = 现有素材里符合训练口径的段数（不是 0）",
              st_f["dataset"]["segs"] == want and want > 0,
              "界面=%s 期望=%s（角色 %s）" % (st_f["dataset"]["segs"], want, fresh.get("name")))
    return role, fresh


# ---------------------------------------------------------------- B CLI

def _write_wav(path, y, sr=SR):
    """用标准库写 16bit PCM。

    为什么不用 soundfile：本关要在**托管 venv** 下跑（浏览器段要 websocket），
    而 venv 里没有 soundfile。种子命令本身跑在 RVC 解释器下（那边有），
    判据这边只需要能写能读 16bit wav —— 标准库够，别为一个写文件去装依赖。
    """
    y16 = (np.clip(np.asarray(y, dtype=np.float64), -1.0, 1.0) * 32000.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(y16.tobytes())


def _read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    return a / 32768.0, sr


def _tone(freq, dur, amp=0.4, sr=SR, noise=0.0, seed=3):
    n = int(dur * sr)
    t = np.arange(n) / float(sr)
    y = amp * np.sin(2 * np.pi * freq * t)
    if noise:
        y = y + noise * np.random.RandomState(seed).randn(n)
    return y.astype(np.float32)


def _stack(f0, dur, extra_hz=0.0, amp=0.35, sr=SR):
    """谐波堆叠（像人声），可外加一个高频分量。"""
    n = int(dur * sr)
    t = np.arange(n) / float(sr)
    y = np.zeros(n, dtype=np.float64)
    for k in range(1, 11):
        y += (1.0 / k) * np.sin(2 * np.pi * f0 * k * t)
    if extra_hz:
        y += 0.5 * np.sin(2 * np.pi * extra_hz * t)
    m = np.max(np.abs(y)) or 1.0
    return (y / m * amp).astype(np.float32)


def _bp(y, lo, hi, sr=SR):
    """FFT 掩膜带通（判据只用相对关系，不做听感处理）。"""
    X = np.fft.rfft(np.asarray(y, dtype=np.float64))
    f = np.fft.rfftfreq(len(y), 1.0 / sr)
    X[(f < lo) | (f >= hi)] = 0.0
    return np.fft.irfft(X, n=len(y))


def _at(y, hz, half=60, sr=SR):
    """某个频率附近的谱峰高度（用来问"这一根谱线在不在"）。"""
    X = np.abs(np.fft.rfft(np.asarray(y, dtype=np.float64) * np.hanning(len(y))))
    f = np.fft.rfftfreq(len(y), 1.0 / sr)
    m = (f >= hz - half) & (f < hz + half)
    return float(X[m].max()) if m.any() else 0.0


def _spec_corr(y, x, lo, hi, sr=SR):
    """**幅度谱**在某一频段上的相关。

    为什么判"高频跟谁"要看谱而不是看波形：交叉合成的语义就是
    「幅度取 B、相位取 A」。A、B 是两段不相干的合成信号时，输出波形跟谁都
    不会是 1.0（相位不是任何一方的），那是**测试信号的性质**；
    而幅度谱能干净地回答"这一段频谱是谁给的"。真实素材里 A、B 是同一首歌，
    相位本来就是相干的。
    """
    Y = np.abs(np.fft.rfft(np.asarray(y, dtype=np.float64) * np.hanning(len(y))))
    X = np.abs(np.fft.rfft(np.asarray(x, dtype=np.float64) * np.hanning(len(x))))
    f = np.fft.rfftfreq(len(y), 1.0 / sr)
    m = (f >= lo) & (f < hi)
    a = Y[m]
    b = X[m]
    n = min(len(a), len(b))
    a, b = a[:n], b[:n]
    if n < 10 or np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _corr(a, b):
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)[:len(a)]
    a = a[:len(b)]
    if len(a) < 10 or np.std(a) < 1e-12 or np.std(b) < 1e-12:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _band(y, lo, hi, sr=SR):
    """某频段内的平均能量（dB 无所谓，只用来比相对大小）。"""
    X = np.abs(np.fft.rfft(y * np.hanning(len(y)))) ** 2
    f = np.fft.rfftfreq(len(y), 1.0 / sr)
    m = (f >= lo) & (f < hi)
    return float(np.mean(X[m])) if m.any() else 0.0


def section_b(fresh=None):
    print("\n=== B. seedvc_cli.py（跑在 RVC 解释器下） ===")
    tmp = tempfile.mkdtemp(prefix="vs_seedvc_")
    vs = os.path.join(tmp, "vs")
    rid = "r_probe1234"
    slices = os.path.join(vs, "data", "roles", rid, "slices")
    os.makedirs(os.path.join(vs, "data"), exist_ok=True)
    os.makedirs(slices, exist_ok=True)
    try:
        # 3 段：2 段合格（3.0s / 2.5s，有声比例 0.9），1 段太短（0.8s）应被滤掉
        segs = []
        for i, (dur, voiced) in enumerate(((3.0, 0.90), (2.5, 0.80), (0.8, 0.90))):
            name = "seg%02d.wav" % i
            _write_wav(os.path.join(slices, name), _tone(220.0 + 30 * i, dur))
            segs.append({"file": name, "dur": dur, "voiced": voiced, "text": "x"})
        json.dump({"roles": [{"id": rid, "name": "probe", "segments": segs}]},
                  open(os.path.join(vs, "data", "roles.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)

        dout = os.path.join(tmp, "ds")
        rc, out = _cli(["prep", "--vs", vs, "--role", rid, "--out", dout])
        m = None
        for ln in (out or "").splitlines():
            if ln.startswith("PREP "):
                m = ln
        n_prep = len(glob.glob(os.path.join(dout, "*.wav"))) if os.path.isdir(dout) else 0
        check("prep：只收时长≥2s 且有声比例≥60% 的段（3 段里留 2 段）",
              rc == 0 and "n=2" in (m or "") and n_prep == 2,
              "rc=%s 输出=%s 落盘=%d" % (rc, m, n_prep))

        # ★ 负对照：全是不合格的素材 → 必须报错退出，而不是"训了个空集"
        bad = os.path.join(tmp, "ds_bad")
        json.dump({"roles": [{"id": rid, "name": "probe",
                              "segments": [{"file": "seg00.wav", "dur": 0.5, "voiced": 0.9}]}]},
                  open(os.path.join(vs, "data", "roles.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        rc2, out2 = _cli(["prep", "--vs", vs, "--role", rid, "--out", bad])
        check("★ 负对照：素材全不合格 → 报错退出（过滤不是摆设）",
              rc2 != 0 and "PREP_ERR" in (out2 or ""), "rc=%s %s" % (rc2, (out2 or "")[:80]))

        # prompt：硬顶 25 秒（whisper 编码器最多 3000 帧，超了会崩）
        json.dump({"roles": [{"id": rid, "name": "probe", "segments": segs}]},
                  open(os.path.join(vs, "data", "roles.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        pwav = os.path.join(tmp, "prompt.wav")
        rc3, out3 = _cli(["prompt", "--vs", vs, "--role", rid, "--out", pwav, "--max-sec", "6"])
        dur_sec = 0.0
        if os.path.isfile(pwav):
            _pa, _psr = _read_wav(pwav)
            dur_sec = len(_pa) / float(_psr)
        check("prompt：拼出的参考不超过给定上限（6 秒）",
              rc3 == 0 and 0.5 < dur_sec <= 6.5, "rc=%s 时长=%.2fs" % (rc3, dur_sec))

        # cross：低频必须来自 A（RVC 干声），高频必须来自 B（原唱）。
        # ★ 判据用**相关性**而不是能量绝对值：A、B 两边的相位本就不相干，
        #   取 |B| 配 A 的相位时，高频分量的叠加不是完全同相的（真实素材里两边
        #   是同一首歌所以相干，这里合成的两个独立信号不相干）⇒ 幅度会偏低。
        #   幅度低是**测试信号的性质**，不是实现的 bug —— 恒等性（cross(a,a)
        #   逐样本还原）另有一条判据守着。相关性不受幅度影响，才量的准。
        pa = os.path.join(tmp, "a.wav")
        pb = os.path.join(tmp, "b.wav")
        psil = os.path.join(tmp, "sil.wav")
        py1 = os.path.join(tmp, "y1.wav")
        py2 = os.path.join(tmp, "y2.wav")
        py3 = os.path.join(tmp, "y3.wav")
        # A：220Hz 的谐波堆（代表 RVC 干声）+ 一个 6k 分量
        # B：330Hz 的谐波堆（代表原唱）+ 一个 8k 分量
        # 两边**低频和高频都不一样**，才能判别输出到底跟了谁。
        _write_wav(pa, _stack(220.0, 2.0, extra_hz=6000.0))
        _write_wav(pb, _stack(330.0, 2.0, extra_hz=8000.0))
        _write_wav(psil, np.zeros(int(2.0 * SR)))
        rc4, _ = _cli(["cross", pa, pb, py1, "3200", "1200"])
        rc5, _ = _cli(["cross", pa, psil, py2, "3200", "1200"])
        rc6, _ = _cli(["cross", pa, pa, py3, "3200", "1200"])
        if rc4 == 0 and rc5 == 0 and rc6 == 0:
            a, _ = _read_wav(pa)
            b, _ = _read_wav(pb)
            y1, _ = _read_wav(py1)
            y2, _ = _read_wav(py2)
            y3, _ = _read_wav(py3)
            # 前置：A、B 在两个频段上确实不一样，否则下面两条是白量的
            sep_lo = _corr(_bp(a, 200, 1000), _bp(b, 200, 1000))
            sep_hi = _corr(_bp(a, 5000, 10000), _bp(b, 5000, 10000))
            check("前置：A、B 在低频和高频上都确实不同（判据有判别力）",
                  abs(sep_lo) < 0.5 and abs(sep_hi) < 0.5,
                  "低频相关=%.3f 高频相关=%.3f" % (sep_lo, sep_hi))
            c_lo_a = _spec_corr(y1, a, 200, 1000)
            c_lo_b = _spec_corr(y1, b, 200, 1000)
            check("cross：低频（200~1k）的频谱来自 A",
                  c_lo_a > 0.9 and c_lo_a > c_lo_b + 0.2,
                  "谱相关 与A=%.3f 与B=%.3f" % (c_lo_a, c_lo_b))
            # 高频用「各自独有的那个分量在不在」来判，不用整段谱相关：
            # B 的高频段几乎是**一根谱线**（8k），一根谱线与一根谱线的相关性
            # 上限本来就不是 1（实测 0.74），拿它当门槛只能逼人去松口径。
            # 而"A 独有的 6k 有没有被抹掉 / B 独有的 8k 有没有被搬进来"是干净的是非题。
            e6_a, e8_a = _at(a, 6000), _at(a, 8000)
            e6_b, e8_b = _at(b, 6000), _at(b, 8000)
            e6_y, e8_y = _at(y1, 6000), _at(y1, 8000)
            check("cross：B 独有的 8k 分量进了输出（高频取的就是 B 的谱）",
                  e8_y >= 0.2 * e8_b,
                  "B=%.4g 输出=%.4g（%.0f%%）" % (e8_b, e8_y, 100.0 * e8_y / max(e8_b, 1e-12)))
            check("cross：A 独有的 6k 分量被抹掉（高频不是 A 的）",
                  e6_y <= 0.01 * e6_a,
                  "A=%.4g 输出=%.4g" % (e6_a, e6_y))
            # 恒等性：A=B 时输出必须**逐样本**还原 A（这一条守住实现本身没写错）
            n = min(len(a), len(y3))
            same = float(np.max(np.abs(a[:n] - y3[:n])))
            check("cross：A=B 时逐样本还原（STFT/ISTFT 归一化没错）",
                  same < 2e-3 and _corr(a[:n], y3[:n]) > 0.999,
                  "最大差=%.2e 相关=%.4f" % (same, _corr(a[:n], y3[:n])))
            # ★ 负对照：B 换成静音 → 高频必须掉光（否则说明高频是泄漏来的）
            hi_b = _band(b, 5000, 10000)
            check("★ 负对照：B 是静音时高频掉光（高频真的来自 B，不是泄漏）",
                  _band(y2, 5000, 10000) <= 0.02 * hi_b,
                  "B=%.3g 静音版输出=%.3g" % (hi_b, _band(y2, 5000, 10000)))
        else:
            check("cross 跑通", False, "rc=%s/%s/%s" % (rc4, rc5, rc6))

        # ★ 界面口径 vs 训练口径**实跑对账**。
        # 界面写「可用素材 N 段」，真正喂给训练的是 prep 数出来的那份 —— 两边
        # 只要有一个过滤条件漂移（时长门槛 / 有声比例 / 截断），用户看到的就是
        # 一个跟实际不符的数字。所以这里拿一个**真实角色**真跑一遍 prep 来比。
        if fresh:
            dreal = os.path.join(tmp, "ds_real")
            rc7, out7 = _cli(["prep", "--vs", HERE, "--role", fresh["id"],
                              "--out", dreal])
            m7 = re.search(r"PREP n=(\d+)", out7 or "")
            n_cli = int(m7.group(1)) if m7 else -1
            n_ui = seedvc.state(fresh)["dataset"]["segs"]
            check("★ 界面显示的段数 = prep 真正整理出来的段数（口径没漂移）",
                  rc7 == 0 and n_cli > 0 and n_cli == n_ui,
                  "prep=%s 界面=%s（角色 %s）" % (n_cli, n_ui, fresh.get("name")))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _cli(args):
    p = subprocess.run([RVC_PY, "-s", CLI] + [str(a) for a in args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ---------------------------------------------------------------- C 接口

def _get(path):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=10))
    except Exception as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"_err": str(e)}


def _post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=10))
    except Exception as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"_err": str(e)}


def section_c(role, fresh=None):
    print("\n=== C. 接口（连的是 8756 常驻服务，改了 server.py 要重启才测得到） ===")
    if not role:
        check("有可做判据的角色", False)
        return
    if fresh:
        st_f = _get("/api/seedvc/state?role=" + fresh["id"])
        check("★ 接口：没微调过的角色也报出真实素材数（不是 0 段）",
              (st_f.get("dataset") or {}).get("segs", 0) > 0,
              "segs=%s 角色=%s" % ((st_f.get("dataset") or {}).get("segs"),
                                   fresh.get("name")))
    rid = role["id"]
    st = _get("/api/seedvc/state?role=" + rid)
    check("GET /api/seedvc/state 返回可用状态 + 档位 + 当前档",
          st.get("ok") is True and len(st.get("ckpts") or []) >= 1
          and (st.get("selected") or {}).get("step"),
          "ok=%s 档位=%d 当前=%s" % (st.get("ok"), len(st.get("ckpts") or []),
                                     (st.get("selected") or {}).get("label")))
    check("★ 负对照：不给 role → 404（不是什么请求都回 200）",
          "error" in _get("/api/seedvc/state"), _get("/api/seedvc/state").get("error"))

    # 换档：切到 400，验证落盘，再切回原来的档（不许把用户的角色留在中间态）
    orig = ((role.get("seedvc") or {}).get("ckpt"))
    r1 = _post("/api/role/%s/seedvc-pick" % rid, {"step": 400})
    after1 = (studio.get_role(rid) or {}).get("seedvc") or {}
    check("换档：按步数切到 ft400 生效",
          r1.get("ok") is True and after1.get("step") == 400
          and "step_00400" in (after1.get("ckpt") or ""),
          "ckpt=%s" % after1.get("ckpt"))
    r2 = _post("/api/role/%s/seedvc-pick" % rid, {"ckpt": orig})
    after2 = (studio.get_role(rid) or {}).get("seedvc") or {}
    check("换档：切回原档（测试不留中间态）",
          r2.get("ok") is True and after2.get("ckpt") == orig,
          "ckpt=%s 期望=%s" % (after2.get("ckpt"), orig))
    bad = _post("/api/role/%s/seedvc-pick" % rid, {"ckpt": "runs/artanis/ft_不存在的档.pth"})
    check("★ 负对照：不存在的档位被拒（400），角色档位没被改坏",
          bool(bad.get("error")) and ((studio.get_role(rid) or {}).get("seedvc") or {}).get("ckpt") == orig,
          bad.get("error"))


# ---------------------------------------------------------------- D 浏览器

PORT = 9341
PROFILE = os.path.join(HERE, ".chrome-seedvc")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
if not os.path.isfile(CHROME):
    for c in (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Users\yyfabab\AppData\Local\Google\Chrome\Application\chrome.exe"):
        if os.path.isfile(c):
            CHROME = c
            break


def cdp_get(path):
    return json.load(urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=5))


class Cdp:
    """最小 CDP 客户端：真鼠标（Input.dispatchMouseEvent）+ 命中测试。

    禁 element.click() —— 它绕过命中测试，屏幕外的东西也能"点成功"，
    那样"按钮被挡住点不到"这类真 bug 永远测不出来。
    """
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.i = 0
        self.events = []

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})
            self.events.append(m)

    def js(self, expr, awaitp=False):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=awaitp,
                      returnByValue=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError("%s @ %s:%s" % (
                ((d.get("exception") or {}).get("description") or d.get("text")),
                d.get("url"), d.get("lineNumber")))
        return r.get("result", {}).get("value")

    def js_errors(self):
        out = []
        for m in self.events:
            if m.get("method") == "Runtime.exceptionThrown":
                d = m["params"].get("exceptionDetails", {})
                out.append("%s @ %s:%s" % (
                    (d.get("exception") or {}).get("description") or d.get("text"),
                    d.get("url"), d.get("lineNumber")))
        return out

    def box(self, sel):
        return self.js("""(()=>{
          const el=document.querySelector(%s);
          if(!el) return {err:'no element'};
          el.scrollIntoView({block:'center',inline:'center'});
          const r=el.getBoundingClientRect();
          const x=r.left+r.width/2, y=r.top+r.height/2;
          const hit=document.elementFromPoint(x,y);
          return {x:x,y:y,inView:(r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth),
                  hit:hit?(hit.id||hit.className||hit.tagName):null,
                  hitIsSelf:!!(hit&&(hit===el||el.contains(hit)||hit.contains(el)))};
        })()""" % json.dumps(sel))

    def click(self, sel, label=""):
        b = self.box(sel)
        if b.get("err"):
            check("能点到 %s" % (label or sel), False, b["err"])
            return False
        if not b.get("inView"):
            check("能点到 %s" % (label or sel), False, "不在视口内，真鼠标必然打空")
            return False
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.id||e.className||e.tagName):'null'})()" % (b["x"], b["y"]))
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=b["x"], y=b["y"],
                  button="left", clickCount=1)
        top2 = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                       "return e?(e.id||e.className||e.tagName):'null'})()" % (b["x"], b["y"]))
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=b["x"], y=b["y"],
                  button="left", clickCount=1)
        if top2 != top:
            print("    [诊断] ⚠ press→release 落点变了：%s → %s（click 事件会丢）" % (top, top2))
        check("真鼠标点得到 %s（命中测试 PASS）" % (label or sel),
              bool(b.get("hitIsSelf")), "落点=%s 命中=%s" % (top, b.get("hit")))
        return True


def poll(page, expr, want=True, secs=20, step=0.4):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if bool(page.js(expr)) == want:
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def section_d(role):
    print("\n=== D. 浏览器（真鼠标） ===")
    if not role:
        check("有可做判据的角色", False)
        return
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        for _ in range(40):
            try:
                cdp_get("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Chrome 没起来")
        tabs = [t for t in cdp_get("/json/list")
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        page = Cdp(tabs[0]["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Log.enable")
        page.send("Page.navigate", url="http://127.0.0.1:8756/")
        time.sleep(5.0)

        # ★ 把 /api/jobs 拦成假回包：下面要真点「开始微调」，
        #   真提交会把显卡占住五分钟。判据量的是"点了之后发出去的到底是什么"，
        #   不是"任务有没有跑完" —— 所以拦下来不影响判据成立。
        page.js("""(()=>{
          window.__posts=[];
          const of = window.fetch;
          window.fetch = function(u, o){
            try {
              const url=String(u);
              if (url.indexOf('/api/jobs')>=0 && o && typeof o.body==='string') {
                window.__posts.push(JSON.parse(o.body));
                return Promise.resolve({ok:true, status:200,
                  json:()=>Promise.resolve({job:{id:'jfake',state:'done',pct:100,steps:[]}})});
              }
            } catch(e) {}
            return of.apply(window, arguments);
          };
          return true;})()""")

        rid = role["id"]
        # 用 IIFE 包起来：裸 `return true;` 在 Runtime.evaluate 里是
        # "Illegal return statement"，整段 JS 一行都不会执行（页面看着没事，
        # 其实判据后面全在测一个没打开的抽屉）。
        page.js("(()=>{window.__posts=[]; openRole(%s, 'paneTrain'); return true;})()"
                % json.dumps(rid))
        time.sleep(1.5)

        # 真鼠标点「训练」页签（页签是可点的，别用 JS 切 pane 蒙过去）
        page.click('.tab[data-pane="paneTrain"]', "「训练」页签")
        ok_opts = poll(page, "document.querySelectorAll('#svCkpt option').length > 0", True, 25)
        n_opts = page.js("document.querySelectorAll('#svCkpt option').length")
        n_api = len((_get("/api/seedvc/state?role=" + rid).get("ckpts") or []))
        check("训练页的档位下拉渲染出来了，选项数与接口一致",
              ok_opts and n_opts == n_api and n_api >= 1,
              "界面=%s 接口=%s" % (n_opts, n_api))
        check("档位下拉选中的就是当前在用的那一档",
              page.js("(()=>{const s=document.querySelector('#svCkpt');"
                      "return s.selectedOptions[0].textContent})()").find("ft600") >= 0,
              page.js("(()=>{const s=document.querySelector('#svCkpt');"
                      "return s.selectedOptions[0]?s.selectedOptions[0].textContent:''})()"))
        # ★ 等状态行**真的写完**再断言，别读完上一轮就走：点页签会再触发一次
        #   refreshSeedvc，它先把状态行写成"正在看微调状态…"，此时下拉里还是
        #   上一次的选项 ⇒ 立刻读就会读到那句占位文案（间歇性红，第一次跑是绿的）。
        ok_state = poll(page, "(document.querySelector('#svState').textContent||'').indexOf('ft600')>=0",
                        True, 25)
        check("状态行写清了当前用哪一档（不是一句「读不到」）", ok_state,
              (page.js("document.querySelector('#svState').textContent") or "")[:110])

        # 真鼠标点「开始微调」：必须提交 kind=seedvc + steps=600
        page.js("window.__posts=[]")
        page.click("#btnSeedvc", "「开始微调」")
        time.sleep(1.0)
        posts = page.js("window.__posts") or []
        hit = [p for p in posts if p.get("kind") == "seedvc"]
        check("真鼠标点「开始微调」提交的是 kind=seedvc",
              bool(hit), "提交体=%s" % json.dumps(posts, ensure_ascii=False)[:160])
        check("默认步数是 600（实测最优那一档，不是随手给的数）",
              bool(hit) and int(hit[0].get("steps") or 0) == 600,
              hit and hit[0].get("steps"))
        check("★ 不会误提交成其它任务类型（拦截期间只发了这一条）",
              len(posts) == len(hit), "共 %d 条" % len(posts))

        errs = page.js_errors()
        check("全程没有 JS 异常", not errs, ("; ".join(errs))[:220] or "干净")
    finally:
        with contextlib.suppress(Exception):
            page.ws.close()
        proc.kill()


def _plant_untrained():
    """自己种一个「没微调过、但素材很多」的探针角色，跑完由 main 删掉。

    为什么不直接指着一个已有角色：这条判据要的是"没跑过微调的那个"，
    用户哪天顺手给所有角色都跑过微调（2026-10-13 就整理了大帝），它就无处谈起。
    切片用**硬链接**指向别人已有的文件 —— 不额外占磁盘，但 prep 真能读到音频；
    硬链接建不起来（FAT/exFAT 之类）就退回真拷贝，只是在下载得起的范围内拷前 N 段。
    """
    src = None
    for r in studio.load_roles()["roles"]:
        segs = [s for s in (r.get("segments") or [])
                if (s.get("dur") or 0) >= seedvc.PREP_MIN_DUR
                and (s.get("voiced") or 0) >= seedvc.PREP_MIN_VOICED]
        if len(segs) >= 30:
            src = (r, segs[:36])
            break
    if not src:
        return None
    srole, segs = src
    probe = studio.create_role(name="门禁·未微调探针", kind="speech")
    pid = probe["id"]
    dsrc = os.path.join(studio.role_dir(srole["id"]), "slices")
    ddst = os.path.join(studio.role_dir(pid), "slices")
    os.makedirs(ddst, exist_ok=True)
    ok = 0
    for s in segs:
        a = os.path.join(dsrc, s["file"])
        b = os.path.join(ddst, s["file"])
        if not os.path.isfile(a):
            continue
        try:
            if os.path.exists(b):
                os.remove(b)
            os.link(a, b)
        except Exception:
            try:
                shutil.copyfile(a, b)
            except Exception:
                continue
        ok += 1
    if ok < 30:
        print("  [warn] 探针角色只种出 %d 段（<30），判据无效" % ok)
        return None
    # ★ 素材只能直接写回 roles.json：`update_role` 是个**白名单**，只放行
    #   name/intro/kind/color/ref/kind_locked（防止界面被伪造的内层字段污染），
    #   往它这里传 segments 会被静默丢掉 —— 种出来就是个"有目录没素材"的空角色。
    db = studio.load_roles()
    hit = None
    for r in db["roles"]:
        if r["id"] == pid:
            hit = r
            break
    if hit is None:
        return None
    hit["segments"] = segs[:ok]
    studio.save_roles(db)
    PLANTED.append(pid)
    print("  种了一个没微调过的探针角色 %s（%d 段，来自 %s）"
          % (pid, ok, srole.get("name")))
    return studio.get_role(pid)


def main():
    os.chdir(HERE)
    print("门禁：Seed-VC 微调 / 44.1k 精修")
    role, fresh = section_a()
    section_b(fresh)
    section_c(role, fresh)
    try:
        section_d(role)
    except Exception as e:
        check("浏览器段跑完（没崩）", False, "%s: %s" % (type(e).__name__, e))
    finally:
        # 自己种的自己收：探针角色 + 它的目录都得走人（留着就是角色库里的垃圾）
        for pid in PLANTED:
            with contextlib.suppress(Exception):
                studio.delete_role(pid)
            shutil.rmtree(studio.role_dir(pid), ignore_errors=True)
        PLANTED.clear()
    print("-" * 62)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 失败项：" + "、".join(BAD))
    else:
        print("结论：Seed-VC 门禁全绿")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
