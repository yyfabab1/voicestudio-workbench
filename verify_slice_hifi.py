# -*- coding: utf-8 -*-
r"""第 31 关：保真切片（48k）—— 歌声训练素材不再被锁在 16 kHz

由头（老板 2026-10-16「继续优化提升，参考文献等等提升训练质量和唱歌说话质量」）：

  GPT-SoVITS 自带的 `tools/slice_audio.py` 三处写死 32000（`Slicer(sr=)` /
  `load_audio()` / `wavfile.write()`）。那是给**说话模型**用的，没错；但同一份
  切片我们还拿去训 **RVC 歌声模型** ⇒ 素材奈奎斯特上限 **16 kHz**。
  实测（2026-10-16）：分离出的登阶人声是 44.1k stereo，切完 353 段全是 32k ——
  16k~22.05k 那一段（齿音、气声、"像不像本人在唱"的部分）当场没了。
  文献对训练素材的要求是 **44.1k 或 48k 且全库统一**。

⇒ 我们用自己的 `stages/slice_hifi.py` 切（不改第三方工具），目标 48k。

★ 这一关真正的价值不在"输出是不是 48k"，而在下面两条 —— 它们是**踩过的坑**：

  C 段：切分行为必须**逐段不变**。长度参数（4000/300/10/500）的单位是**毫秒**不是
    帧，`Slicer` 内部自己按 sr 换算。第一版改动望文生义多乘了一次 sr/32000，
    把"每段至少 4 秒"变成 6 秒 ⇒ 3 段被并成 2 段。**只验采样率的话这条一路绿灯**。
  D 段：高频必须**真的还在**，而不是"参数写的是 48000 所以应该还在"。
    做法是在输入里放一段 20 kHz 成分（32k 的奈奎斯特之上）：32k 那份必须**没了**，
    48k 那份必须**还在**。

★ 每条判据都配负对照（换坏版本必须变红），且负对照**每次都跑**，不是摆设。
★ 素材现场合成，不依赖运行时残留（铁律）。
★ 切片那一步要 scipy + GSV 的 `slicer2` / `tools.my_utils`，所以** subprocess 调
  GSV 解释器**跑（跟 `_probe_slice_hifi.py` 同一口径）；本关自己用 venv 跑。

用法：  <venv>/Scripts/python.exe -s verify_slice_hifi.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
from scipy.io import wavfile
from scipy.signal import resample_poly  # noqa: F401  探针口径：确认环境齐

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402

GSV_ROOT = studio.GSV_ROOT
PY = studio.PY
SLICE_ARGS = list(studio.SLICE_ARGS)
SRC_SR = 44100
HI_TONE = 20000        # 20 kHz：32k 采样率必然丢，48k 必然留

PASS = 0
FAILS = []


def sec(t):
    print("\n" + "-" * 62)
    print(t)
    print("-" * 62)


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  | %s" % detail) if detail else ""))
    else:
        FAILS.append(name)
        print("  FAIL  %s%s" % (name, ("  | %s" % detail) if detail else ""))


# ---------------------------------------------------------------- 造素材
def make_src(path, n_seg=10):
    """44.1k 的「带停顿的歌声」：n_seg 段 1.8 秒 + 中间 0.6 秒静音，每段掺 20 kHz。

    ★ 为什么默认 10 段：切片工具要求每段 ≥4 秒，所以 1.8 秒的段会被**两两合并**
      ⇒ 10 段进去只有 5 段出来。而 `studio.step_slice` 默认"不足 5 段可用就报错"。
      F 段要真调 step_slice，素材不够会**假红**（它的报错是"只有 3 段可用"，
      看着像退化失败，其实退化好得很）—— 这条踩过一次，别再填进去。
    """
    rng = np.random.default_rng(7)
    seg = int(SRC_SR * 1.8)
    gap = int(SRC_SR * 0.6)
    parts = []
    for i in range(n_seg):
        t = np.arange(seg) / float(SRC_SR)
        f0 = 220.0 + 22.0 * i
        x = np.sin(2 * np.pi * f0 * t) * 0.5
        x += np.sin(2 * np.pi * f0 * 3 * t) * 0.12
        x += np.sin(2 * np.pi * HI_TONE * t) * 0.10     # ★ 高频探针
        x += rng.normal(0, 0.01, seg)
        env = np.ones(seg)
        k = int(SRC_SR * 0.05)
        env[:k] = np.linspace(0, 1, k)
        env[-k:] = np.linspace(1, 0, k)
        parts.append(x * env)
        parts.append(np.zeros(gap))
    y = np.concatenate(parts)
    y = y / np.abs(y).max() * 0.8
    wavfile.write(path, SRC_SR, (y * 32767).astype(np.int16))
    return len(y) / float(SRC_SR)


def band_ratio(path, lo, hi):
    sr, x = wavfile.read(path)
    x = x.astype(np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    n = 1 << int(np.ceil(np.log2(max(len(x), 2))))
    sp = np.abs(np.fft.rfft(x, n=n))
    fr = np.fft.rfftfreq(n, 1.0 / sr)
    tot = (sp ** 2).sum()
    if tot <= 0:
        return 0.0
    return float((sp[(fr >= lo) & (fr <= hi)] ** 2).sum() / tot)


# ---------------------------------------------------------------- 跑工具
def run_old(src, out):
    """自带工具（32k）。必须在 GSV 根目录下跑，它 import 的是 tools.my_utils。"""
    e = os.environ.copy()
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUNBUFFERED"] = "1"
    p = subprocess.run([PY, "-s", "tools/slice_audio.py", src, out] + SLICE_ARGS,
                       cwd=GSV_ROOT, env=e, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def run_hifi(src, out, sr=48000, args=None):
    """保真工具。GSV_ROOT 靠环境变量带进去（工具靠它找 slicer2 / tools.my_utils）。"""
    e = os.environ.copy()
    e["PYTHONIOENCODING"] = "utf-8"
    e["PYTHONUNBUFFERED"] = "1"
    e["GSV_ROOT"] = GSV_ROOT
    p = subprocess.run([PY, "-s", os.path.join(HERE, "stages", "slice_hifi.py"),
                        src, out, str(sr)] + (args or SLICE_ARGS),
                       cwd=GSV_ROOT, env=e, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def collect(d):
    """[(起秒, 止秒, 时长, 采样率)]，按时间排。"""
    out = []
    for f in sorted(os.listdir(d)):
        if not f.lower().endswith(".wav"):
            continue
        sr, x = wavfile.read(os.path.join(d, f))
        stem = f[:-4].rsplit("_", 2)
        try:
            s, e2 = int(stem[-2]), int(stem[-1])
        except ValueError:
            s, e2 = 0, len(x)
        out.append((s / float(sr), e2 / float(sr), len(x) / float(sr), int(sr)))
    out.sort()
    return out


def hi_median(d):
    rs = [band_ratio(os.path.join(d, f), 16000, 22000)
          for f in sorted(os.listdir(d)) if f.lower().endswith(".wav")]
    return float(np.median(rs)) if rs else 0.0


# ---------------------------------------------------------------- A 常量
def a_consts():
    sec("A. 常量对账（改了一个忘了另一个会当场红）")
    check("studio.SLICE_SR 是 48000", studio.SLICE_SR == 48000, "SLICE_SR=%s" % studio.SLICE_SR)
    check("保真切片工具在盘上",
          os.path.isfile(os.path.join(HERE, "stages", "slice_hifi.py")))
    check("自带工具没被我们改过（仍然写死 32000）", _old_tool_untouched(),
          "第三方目录保持原样")
    # 48k 是 RVC 两代都支持的最高档 —— 这个事实变了这里要跟着改
    check("48k 在 v1/v2 的可用档位里都有",
          "48k" in studio.RVC_ARCH_SRS["v1"] and "48k" in studio.RVC_ARCH_SRS["v2"],
          "v1=%s v2=%s" % (studio.RVC_ARCH_SRS["v1"], studio.RVC_ARCH_SRS["v2"]))


def _old_tool_untouched():
    p = os.path.join(GSV_ROOT, "tools", "slice_audio.py")
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            s = f.read()
    except OSError:
        return False            # 读不到就不敢说"没改过"
    return ("load_audio(inp_path, 32000)" in s and "wavfile.write(" in s
            and "32000," in s)


# ---------------------------------------------------------------- B/C/D 切片对照
def bcd(tmp, src):
    old_d = os.path.join(tmp, "old")
    new_d = os.path.join(tmp, "new")
    for d in (old_d, new_d):
        os.makedirs(d, exist_ok=True)

    rc_o, out_o = run_old(src, old_d)
    rc_n, out_n = run_hifi(src, new_d, 48000)

    sec("B. 采样率（保真切片输出必须是 48k）")
    check("自带工具跑通（对照基准）", rc_o == 0, out_o[-120:])
    check("保真工具跑通", rc_n == 0, out_n[-160:])
    o, n = collect(old_d), collect(new_d)
    srs_o = sorted(set(x[3] for x in o))
    srs_n = sorted(set(x[3] for x in n))
    check("自带那份确实是 32k（否则本关的对照就没意义了）", srs_o == [32000], str(srs_o))
    check("保真那份是 48k", srs_n == [48000], str(srs_n))

    # 负对照：让保真工具按 32k 跑 ⇒ 上面那条必须变红（证明它量的是采样率，不是写死）
    neg_d = os.path.join(tmp, "neg_sr")
    os.makedirs(neg_d, exist_ok=True)
    run_hifi(src, neg_d, 32000)
    srs_neg = sorted(set(x[3] for x in collect(neg_d)))
    check("★ 负对照：按 32k 跑时输出就是 32k（判据跟着变，不是写死）",
          srs_neg == [32000], str(srs_neg))

    sec("C. ★ 切分行为必须逐段不变（挡住「多乘一次采样率比例」那个坑）")
    same_n = len(o) == len(n) and len(o) > 0
    check("段数与自带工具一致", same_n, "自带 %d 段 / 保真 %d 段" % (len(o), len(n)))
    worst = 0.0
    for a, b in zip(o, n):
        worst = max(worst, abs(a[0] - b[0]), abs(a[1] - b[1]), abs(a[2] - b[2]))
    check("每段起止/时长逐段对齐（±0.05 秒）", worst <= 0.05, "最大偏差 %.4f s" % worst)

    # 负对照：把 min_length 从 4000 ms 改到 6000 ms（就是当初踩的那个坑的效果）
    # ⇒ 段数或时长必须变化，否则说明 C 段这条尺子根本量不出东西。
    neg2 = os.path.join(tmp, "neg_len")
    os.makedirs(neg2, exist_ok=True)
    bad = list(SLICE_ARGS)
    bad[1] = "6000"                       # min_length ×1.5
    run_hifi(src, neg2, 48000, bad)
    n2 = collect(neg2)
    changed = (len(n2) != len(o)) or any(
        abs(a[2] - b[2]) > 0.05 for a, b in zip(o, n2))
    check("★ 负对照：长度参数被改动后，切分结果确实变了（尺子有分辨力）",
          changed, "改后 %d 段（原 %d 段）" % (len(n2), len(o)))

    sec("D. ★ 高频必须真的还在（不是「参数写了 48k 所以应该在」）")
    mo, mn = hi_median(old_d), hi_median(new_d)
    check("32k 那份把 16~22k 的成分切没了", mo < 0.001, "中位 %.4f%%" % (mo * 100))
    check("48k 那份保住了 16~22k 的成分", mn > 0.005, "中位 %.4f%%" % (mn * 100))
    check("提升幅度显著（≥5 倍）", mn > mo * 5 or (mo == 0 and mn > 0),
          "%.4f%% -> %.4f%%" % (mo * 100, mn * 100))
    # 负对照：32k 跑保真工具 ⇒ 高频也必须没了 ⇒ 上面那条会红
    mneg = hi_median(neg_d)
    check("★ 负对照：保真工具按 32k 跑时高频照样没了（判据量的是采样率）",
          mneg < 0.001, "中位 %.4f%%" % (mneg * 100))


# ---------------------------------------------------------------- E 文件名格式
def e_names(tmp, src):
    sec("E. 文件名格式与自带工具一致（下游靠它认段）")
    d1 = os.path.join(tmp, "fmt1")
    d2 = os.path.join(tmp, "fmt2")
    os.makedirs(d1, exist_ok=True)
    os.makedirs(d2, exist_ok=True)
    run_old(src, d1)
    run_hifi(src, d2, 48000)
    f1 = sorted(x for x in os.listdir(d1) if x.endswith(".wav"))
    f2 = sorted(x for x in os.listdir(d2) if x.endswith(".wav"))
    check("两边都切出了文件", bool(f1) and bool(f2), "%d / %d" % (len(f1), len(f2)))

    def shape(names):
        """文件名形状：`原名_%010d_%010d.wav` ⇒ 抽出来看是不是两段 10 位数字。"""
        out = []
        for s in names:
            parts = s[:-4].rsplit("_", 2)
            out.append((len(parts) == 3 and len(parts[1]) == 10 and len(parts[2]) == 10
                        and parts[1].isdigit() and parts[2].isdigit(),
                        parts[0] if len(parts) == 3 else s))
        return out

    s1, s2 = shape(f1), shape(f2)
    check("保真那份的文件名形状与自带工具一致",
          bool(s2) and all(a for a, _ in s2), str(s2[:2]))
    check("两边用的是同一批源文件名（前缀一致）",
          bool(s1) and bool(s2) and s1[0][1] == s2[0][1],
          "%s vs %s" % (s1[0][1] if s1 else "-", s2[0][1] if s2 else "-"))


# ---------------------------------------------------------------- F 退化
def f_fallback(tmp):
    sec("F. ★ 保真切片挂了要退化到自带工具（增强不许比改之前更差）")
    vocal = os.path.join(tmp, "vocal")
    os.makedirs(vocal, exist_ok=True)
    make_src(os.path.join(vocal, "v.wav"))
    out_dir = os.path.join(tmp, "fb_out")
    job = {"id": "jvh_slice", "stage": "", "_logfile": os.path.join(tmp, "job.log"),
           "state": "running", "pct": 0, "title": "门禁·切片退化"}
    try:
        _, good = studio.step_slice(job, vocal, out_dir)
        ok = bool(good)
        detail = "%d 段" % len(good)
    except Exception as ex:                       # noqa: BLE001
        ok, detail = False, "%s: %s" % (type(ex).__name__, str(ex)[:120])
    check("正常情况下切得出片段", ok, detail)

    srs = sorted(set(x[3] for x in collect(out_dir))) if os.path.isdir(out_dir) else []
    check("正常路径用的是 48k", srs == [48000], str(srs))

    # 负对照：把工具路径指到一个不存在的地方 ⇒ step_slice 必须**仍然切得出东西**
    orig = studio.HERE
    try:
        studio.HERE = os.path.join(tmp, "__no_such_dir__")
        out2 = os.path.join(tmp, "fb_out2")
        try:
            _, good2 = studio.step_slice(dict(job), vocal, out2)
            ok2 = bool(good2)
            d2 = "%d 段" % len(good2)
        except Exception as ex:                   # noqa: BLE001
            ok2, d2 = False, "%s: %s" % (type(ex).__name__, str(ex)[:120])
        check("★ 负对照：保真工具不在时仍切得出片段（退化生效，不是直接失败）", ok2, d2)
        srs2 = sorted(set(x[3] for x in collect(out2))) if os.path.isdir(out2) else []
        check("退化路径回到 32k（证明走的就是自带工具）", srs2 == [32000], str(srs2))
    finally:
        studio.HERE = orig


# ---------------------------------------------------------------- G 尺子自检
def g_selfcheck():
    sec("G. 尺子自检（量频段的那个函数本身对不对）")
    p = os.path.join(tempfile.mkdtemp(prefix="vs_sc_"), "x.wav")
    sr = 48000
    t = np.arange(int(sr * 1.0)) / float(sr)
    y = np.sin(2 * np.pi * 20000 * t) * 0.5
    wavfile.write(p, sr, (y * 32767).astype(np.int16))
    r_hi = band_ratio(p, 16000, 22000)
    check("20k 纯音在 16~22k 窗里占比很高", r_hi > 0.5, "%.4f" % r_hi)
    y2 = np.sin(2 * np.pi * 200 * t) * 0.5
    wavfile.write(p, sr, (y2 * 32767).astype(np.int16))
    r_lo = band_ratio(p, 16000, 22000)
    check("200 Hz 纯音在同一个窗里占比极低", r_lo < 0.001, "%.6f" % r_lo)
    shutil.rmtree(os.path.dirname(p), ignore_errors=True)


def main():
    tmp = tempfile.mkdtemp(prefix="vs_slice_")
    src = os.path.join(tmp, "src.wav")
    try:
        dur = make_src(src)
        print("输入：44.1 kHz / %.1f 秒，含 20 kHz 成分（16~22k 占比 %.3f%%）"
              % (dur, band_ratio(src, 18000, 22000) * 100))
        a_consts()
        bcd(tmp, src)
        e_names(tmp, src)
        f_fallback(tmp)
        g_selfcheck()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 62)
    print("PASS %d / FAIL %d" % (PASS, len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("   ! " + f)
        print("结论：保真切片门禁有红项")
    else:
        print("结论：保真切片门禁全绿（切分行为不变 + 高频真的保住 + 挂了会退化）")
    print("ALLDONE")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
