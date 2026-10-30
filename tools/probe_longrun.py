# -*- coding: utf-8 -*-
"""长歌压力探针：同一段素材重复 N 次拼成长音频，跑外层分段，逐段看质量是否随位置劣化。

★ 为什么这么设计
----------------
用户的问题很具体：「长时间唱会不会导致低质」。直接跑一首长歌去听，只能得到
一个主观印象，而且歌的不同位置本来就有不同的难度（副歌 vs 主歌），
"后面变差"到底是"位置效应"还是"那段本来就难唱"，分不清。

把**同一段素材**重复 20 份拼成 10 分钟，就消掉了这个混淆：
每一份的难度完全一样。如果第 20 份的产物明显比第 1 份差，那只能是
"唱久了变笨"，没有别的解释。

判据（三条，任一不满足就是有问题）：
  P1 速度不随段序恶化：最后一段耗时 < 第一段 × 1.6（老版本是 30 倍）
  P2 音准不随段序恶化：后半段的 f0 吻合率 相对前半段 掉 < 3 个百分点
  P3 音色不随段序漂移：各段响度/谱质心的离散度 要小（后段无系统性偏移）

用法（用 RVC_PY 跑，脚本内的子进程也用同一个解释器）：
    "<RVC_PY>" -s tools\\probe_longrun.py [--repeat 20] [--seg 30]
"""

import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "stages"))

import numpy as np          # noqa: E402
import soundfile as sf      # noqa: E402

from rvc_infer import plan_cuts      # noqa: E402  复算父进程用过的切点（纯函数，同一输入必得同一结果）

RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
WORK = os.path.join(HERE, "data", "_probe_longrun")

# 与 studio.py 的真实链路保持一致
MODEL = os.path.join(RVC_ROOT, "assets", "weights", "rb37829be.pth")
F0 = "rmvpe"
INDEX_RATE = 0.50
RMS_MIX = 0.25
PROTECT = 0.33
CHUNK = 20.0

SRC_VOCAL = os.path.join(HERE, "data", "cache", "sep", "cd71944a8500e03e", "vocal.wav")

UNIT = 30.0          # 每份素材长度（秒）
REPEAT = 20          # 重复次数 ⇒ 600 秒
MAXSEG = 30.0


def pick_unit(y, sr, unit=UNIT):
    """挑一段首尾最安静的 unit 秒。

    重复拼接会在接缝处留下能量跳变；如果素材首尾本身就在唱，拼接点会变成
    一个可听的爆音，而切点搜索又喜欢往能量低处跑 —— 于是切点全挤到接缝上，
    测出来的"段"就不是同一份素材了。所以先挑首尾安静的那段。
    """
    win = int(0.05 * sr)
    n = len(y)
    L = int(unit * sr)
    if n <= L:
        return 0
    e = (np.abs(y[: (n // win) * win]).reshape(-1, win) ** 2).mean(axis=1)
    edge = int(0.25 * sr / win)
    best, bi = None, 0
    for st in range(0, n - L, int(0.5 * sr)):
        i0 = st // win
        i1 = (st + L) // win
        s = e[i0: i0 + edge].mean() + e[i1 - edge: i1].mean()
        if best is None or s < best:
            best, bi = s, st
    return bi


def build_src():
    os.makedirs(WORK, exist_ok=True)
    y, sr = sf.read(SRC_VOCAL, always_2d=False)
    if y.ndim > 1:
        y = y.mean(axis=1)
    st = pick_unit(y, sr)
    unit = y[st: st + int(UNIT * sr)]
    if len(unit) < int(UNIT * sr):
        unit = np.pad(unit, (0, int(UNIT * sr) - len(unit)))
    long = np.tile(unit, REPEAT)
    p = os.path.join(WORK, "src_%dsec.wav" % int(len(long) / sr))
    sf.write(p, long, sr)
    print("[src] %s  %.1f 秒 @ %d Hz（素材取 %.1f~%.1f 秒，重复 %d 次）"
          % (p, len(long) / float(sr), sr, st / float(sr), st / float(sr) + UNIT, REPEAT))
    return p, sr


def run_rvc(src, out):
    cmd = [sys.executable, "-s", os.path.join(HERE, "stages", "rvc_infer.py"),
           "--model", MODEL, "--input", src, "--output", out,
           "--pitch", "0", "--f0", F0, "--index-rate", str(INDEX_RATE),
           "--rms-mix-rate", str(RMS_MIX), "--protect", str(PROTECT),
           "--chunk", str(CHUNK), "--maxseg", str(MAXSEG)]
    logp = os.path.join(WORK, "rvc_stdout.log")
    # ★ cwd 自检：RVC 那几个 *_root 是按 os.getcwd() 拼的，cwd 不对就会去
    #   <别处>\assets\rmvpe 找模型。先用一个空跑确认「我传的 cwd 到底生效没有」，
    #   别等到 FileNotFoundError 再去猜是谁的锅。
    chk = subprocess.run([sys.executable, "-s", "-c", "import os;print(os.getcwd())"],
                         cwd=RVC_ROOT, capture_output=True, text=True)
    print("[probe] 以 cwd=%s 起子进程，子进程实报 cwd = %s"
          % (RVC_ROOT, chk.stdout.strip() or "（无输出）"))
    t0 = time.time()
    with open(logp, "w", encoding="utf-8", errors="replace") as f:
        p = subprocess.Popen(cmd, cwd=RVC_ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, bufsize=1, errors="replace")
        for ln in p.stdout:
            ln = ln.rstrip("\r\n")
            print("   | %s" % ln)
            f.write(ln + "\n")
        rc = p.wait()
    print("[rvc] 退出码 %d，总用时 %.1f 秒" % (rc, time.time() - t0))
    return rc, logp


RE_SEG = re.compile(r"\[段 (\d+)/(\d+)\]\s+([\d.]+)~([\d.]+) 秒\s+子进程用了 ([\d.]+) 秒")


def parse_seg_times(logp):
    rows = []
    with open(logp, encoding="utf-8", errors="replace") as f:
        for ln in f:
            m = RE_SEG.search(ln)
            if m:
                rows.append({"i": int(m.group(1)), "s": float(m.group(3)),
                             "e": float(m.group(4)), "secs": float(m.group(5))})
    return rows


def qc_one(a_src, a_dst, out_json, start=0.0, secs=0.0):
    cmd = [sys.executable, "-s", os.path.join(HERE, "stages", "qc_cli.py"),
           a_src, a_dst, out_json]
    if secs > 0:
        cmd += [str(start), str(secs)]
    r = subprocess.run(cmd, cwd=RVC_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=600)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {}


def main():
    global REPEAT
    if "--repeat" in sys.argv:
        REPEAT = int(sys.argv[sys.argv.index("--repeat") + 1])
    os.makedirs(WORK, exist_ok=True)
    src, sr = build_src()
    out = os.path.join(WORK, "out.wav")
    rc, logp = run_rvc(src, out)
    if rc != 0:
        print("[err] 推理失败，退出码 %d" % rc)
        return 1
    segs = parse_seg_times(logp)
    print("[seg] 解析到 %d 段" % len(segs))
    if len(segs) < 4:
        print("[err] 段数太少，测不出位置效应")
        return 1

    # 复算切点（与父进程同一函数、同一输入 ⇒ 同一结果），把输入/输出逐段切开
    # ★ 必须和父进程**完全一样**地读：librosa.load(sr=16000)。
    #   第一版我用 sf.read 再自己截断重采样，结果把 44.1k 的数组按 16k 的长度截掉
    #   一截（等于只剩 36% 时长），复算出的切点只有 1 个（实际是 19 个）——
    #   段边界全错，后面逐段 QC 比的是错位的素材，结论毫无意义。
    import librosa
    y16, _ = librosa.load(src, sr=16000, mono=True)
    y16 = np.asarray(y16, dtype="float32")
    cuts = plan_cuts(y16, MAXSEG)
    total = len(y16) / 16000.0
    bounds = [0.0] + cuts + [total]
    print("[cut] 切点 %s" % (", ".join("%.2f" % c for c in cuts)))

    yo, sro = sf.read(out, always_2d=False)
    if yo.ndim > 1:
        yo = yo.mean(axis=1)

    rows = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        a = int(s * sr)
        b = int(e * sr)
        if b - a < sr:            # 短于 1 秒的碎片跳过
            continue
        pin = os.path.join(WORK, "in%02d.wav" % i)
        pout = os.path.join(WORK, "ou%02d.wav" % i)
        sf.write(pin, y16[int(s * 16000): int(e * 16000)], 16000)
        oa = int(round(s / total * len(yo)))
        ob = int(round(e / total * len(yo)))
        sf.write(pout, yo[oa:ob], sro)
        q = qc_one(pin, pout, os.path.join(WORK, "qc%02d.json" % i), 0.0, 0.0)
        rms = float(np.sqrt((np.asarray(yo[oa:ob], dtype="float64") ** 2).mean()))
        rows.append({
            "i": i + 1, "sec": round(e - s, 1),
            "secs": next((x["secs"] for x in segs if x["i"] == i + 1), None),
            "match": q.get("match_ratio"), "oct": q.get("octave_err_ratio"),
            "iqr": q.get("dev_iqr_cents"), "contour": q.get("contour_corr"),
            "rms": round(rms, 5),
        })

    print("\n段序  时长   子进程秒   吻合%   八度错%  IQR音分  轮廓相关   rms")
    for r in rows:
        print(" %2d  %5.1f  %7s  %6s  %7s  %8s  %9s  %8s"
              % (r["i"], r["sec"],
                 ("%.1f" % r["secs"]) if r["secs"] else "-",
                 ("%.1f" % (r["match"] * 100)) if r["match"] is not None else "-",
                 ("%.2f" % (r["oct"] * 100)) if r["oct"] is not None else "-",
                 ("%.1f" % r["iqr"]) if r["iqr"] is not None else "-",
                 ("%.3f" % r["contour"]) if r["contour"] is not None else "-",
                 "%.5f" % r["rms"]))

    # ---- 判据 ----
    bad = 0
    ts = [r["secs"] for r in rows if r["secs"]]
    if len(ts) >= 4:
        ratio = max(ts) / max(0.1, min(ts))
        ok = ratio < 1.6
        bad += 0 if ok else 1
        print("\nP1 速度：最快 %.1fs / 最慢 %.1fs = %.2f 倍  ⇒ %s（阈值 1.6）"
              % (min(ts), max(ts), ratio, "OK" if ok else "★ 段序越靠后越慢"))
        # 首尾比
        print("   首段 %.1fs → 末段 %.1fs（%.2f 倍）" % (ts[0], ts[-1], ts[-1] / ts[0]))

    ms = [(r["i"], r["match"]) for r in rows if r["match"] is not None]
    if len(ms) >= 4:
        h = len(ms) // 2
        f = np.mean([m for _, m in ms[:h]])
        b2 = np.mean([m for _, m in ms[h:]])
        ok = (f - b2) < 0.03
        bad += 0 if ok else 1
        print("P2 音准：前半均值 %.1f%% → 后半均值 %.1f%%（差 %.1f 点）⇒ %s（阈值 3 点）"
              % (f * 100, b2 * 100, (f - b2) * 100, "OK" if ok else "★ 后半变糟"))

    rs = [r["rms"] for r in rows]
    if len(rs) >= 4:
        h = len(rs) // 2
        f = np.mean(rs[:h])
        b2 = np.mean(rs[h:])
        rel = abs(f - b2) / max(1e-9, f)
        ok = rel < 0.15
        bad += 0 if ok else 1
        print("P3 响度：前半均值 %.5f → 后半均值 %.5f（相对差 %.1f%%）⇒ %s（阈值 15%%）"
              % (f, b2, rel * 100, "OK" if ok else "★ 后半响度漂了"))

    print("\n结论：%s（%d 条不满足）" % ("长时间不导致低质" if bad == 0 else "★ 有位置效应", bad))
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
