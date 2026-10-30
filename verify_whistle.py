# -*- coding: utf-8 -*-
"""门禁：口哨专用保护（高基频 + 无谐波 + 稳定 + 短段）。

为什么要有它：`nonvocal_guard` 的"少声音"事故说明，**只放松平坦度**一定会误伤高音延长。
这一关把那条红线钉死：
  · 合成口哨（900Hz 纯音）⇒ 必须圈出；
  · 合成假声（500Hz + 颤音 + 二次谐波）⇒ 必须圈 0；
  · 合成"带强二次谐波的高频音"（像合成器 lead）⇒ 必须圈 0；
  · 真实歌曲（无口哨）⇒ 圈 0（不许误报）；
  · keep 模式：圈出的段逐样本等于原声，其余逐样本等于变声轨。
"""
import io
import json
import os
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(HERE, "stages", "whistle_guard.py")
PY = sys.executable
N = {"pass": 0, "fail": 0}
FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    N["pass" if ok else "fail"] += 1
    if not ok:
        FAILS.append(name)
    print(("  PASS  " if ok else "  ! FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


def synth(kind, sr=16000, dur=30.0):
    t = np.arange(int(sr * dur)) / sr
    x = 0.15 * np.sin(2 * np.pi * 300 * t)
    a, b = int(2.0 * sr), int(3.2 * sr)
    if kind == "whistle":
        x[a:b] = 0.5 * np.sin(2 * np.pi * 900 * t[a:b])
    elif kind == "falsetto":
        f = 500 * (1 + 0.02 * np.sin(2 * np.pi * 6 * t[a:b]))
        ph = 2 * np.pi * np.cumsum(f) / sr
        x[a:b] = 0.4 * np.sin(ph) + 0.12 * np.sin(2 * ph)
    elif kind == "harm_whistle":
        x[a:b] = 0.4 * np.sin(2 * np.pi * 900 * t[a:b]) + 0.2 * np.sin(2 * np.pi * 1800 * t[a:b])
    return x.astype(np.float32)


def run_stage(src, rvc, out, rep, mode="keep"):
    return subprocess.run([PY, "-s", STAGE, "--src", src, "--rvc", rvc,
                           "--out", out, "--mode", mode, "--report", rep],
                          capture_output=True, timeout=900)


def main():
    print("=" * 70)
    print("口哨专用保护门禁")
    print("=" * 70)
    print("\n=== A. 静态接线 ===")
    check("stages/whistle_guard.py 在", os.path.isfile(STAGE))
    st = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("studio 有 WHISTLE_GUARD_MODE", "WHISTLE_GUARD_MODE" in st)
    check("studio 有 guard_whistle()", "def guard_whistle(" in st)
    check("sing_role 真调了它", "guard_whistle(job," in st)
    check("质检 skip 合并了口哨段", "whistle_info" in st and "_qc_skip" in st)

    tmp = tempfile.mkdtemp(prefix="vs_whistle_")
    try:
        print("\n=== B. 正例：合成口哨必须圈出 ===")
        src = os.path.join(tmp, "whistle.wav")
        sf.write(src, synth("whistle"), 16000)
        rvc = os.path.join(tmp, "rvc.wav")
        sf.write(rvc, np.full(int(16000 * 30.0), 0.05, dtype=np.float32), 16000)
        rep = os.path.join(tmp, "r.json")
        out = os.path.join(tmp, "out.wav")
        r = run_stage(src, rvc, out, rep)
        txt = (r.stdout or b"").decode("utf-8", "replace")
        d = {}
        try:
            d = json.load(io.open(rep, encoding="utf-8"))
        except Exception:
            pass
        check("跑通（WHISTLE_DONE）", "WHISTLE_DONE" in txt, txt.strip()[:100])
        spans = d.get("spans") or []
        check("圈出 1 段", len(spans) == 1, "%d 段" % len(spans))
        if spans:
            s0, e0 = spans[0]
            check("圈出位置落在口哨段（2.0~3.2s）", 1.8 <= s0 <= 3.3, "%.2f" % s0)
            check("圈出时长约 1.2 秒", 0.6 <= (e0 - s0) <= 2.0, "%.2f" % (e0 - s0))

        print("\n=== C. 负对照：假声（有颤音+谐波）必须圈 0 ===")
        f = os.path.join(tmp, "falsetto.wav")
        sf.write(f, synth("falsetto"), 16000)
        d2 = {}
        r2 = run_stage(f, rvc, os.path.join(tmp, "o2.wav"), os.path.join(tmp, "r2.json"))
        try:
            d2 = json.load(io.open(os.path.join(tmp, "r2.json"), encoding="utf-8"))
        except Exception:
            pass
        check("假声 ⇒ 0 段", not (d2.get("spans") or []),
              "spans=%s" % (d2.get("spans") or []))

        print("\n=== D. 负对照：带强二次谐波的高频音必须圈 0 ===")
        h = os.path.join(tmp, "harm.wav")
        sf.write(h, synth("harm_whistle"), 16000)
        d3 = {}
        run_stage(h, rvc, os.path.join(tmp, "o3.wav"), os.path.join(tmp, "r3.json"))
        try:
            d3 = json.load(io.open(os.path.join(tmp, "r3.json"), encoding="utf-8"))
        except Exception:
            pass
        check("带谐波的高频音 ⇒ 0 段（不是纯音就不算口哨）",
              not (d3.get("spans") or []), "spans=%s" % (d3.get("spans") or []))

        print("\n=== E. 真实歌曲（没有人吹口哨）不许误报 ===")
        cands = [p for p in [
            os.path.join(HERE, "data", "cache", "sep", "9b21356845fb84c2", "vocal.wav"),
            os.path.join(HERE, "data", "cache", "sep", "5cc73a23551a7b59", "vocal.wav")]
            if os.path.isfile(p)]
        for p in cands[:1]:
            d4 = {}
            run_stage(p, rvc[:int(len(p))] if False else rvc, os.path.join(tmp, "o4.wav"),
                      os.path.join(tmp, "r4.json"))
            try:
                d4 = json.load(io.open(os.path.join(tmp, "r4.json"), encoding="utf-8"))
            except Exception:
                pass
            check("真实歌曲 %s ⇒ 0 段" % os.path.basename(os.path.dirname(p)),
                  not (d4.get("spans") or []), "spans=%s" % len(d4.get("spans") or []))

        print("\n=== F. keep 模式：段内=原声，其余=变声轨（逐样本）===")
        if spans:
            y, sr = sf.read(out, dtype="float32", always_2d=False)
            s, _ = sf.read(src, dtype="float32", always_2d=False)
            rv, _ = sf.read(rvc, dtype="float32", always_2d=False)
            n = min(len(y), len(s), len(rv))
            marg = int(0.1 * sr)
            prot = np.zeros(n, dtype=bool)
            near = np.zeros(n, dtype=bool)
            for a, b in spans:
                ia, ib = int(a * sr), min(n, int(b * sr))
                prot[ia + marg:max(ia + marg, ib - marg)] = True
                near[max(0, ia - marg):min(n, ib + marg)] = True
            others = ~near
            if prot.any() and others.any():
                dp = float(np.abs(y[:n][prot] - s[:n][prot]).max())
                do = float(np.abs(y[:n][others] - rv[:n][others]).max())
                check("保护段 = 原始人声", dp < 2e-3, "最大差 %.6f" % dp)
                check("其余段 = 变声轨", do < 2e-3, "最大差 %.6f" % do)
            else:
                check("有保护段可供对比", False)
        else:
            check("有保护段可供对比", False)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "-" * 70)
    print("通过 %d 项，失败 %d 项" % (N["pass"], N["fail"]))
    if FAILS:
        print("红的：" + "、".join(FAILS))
    print("ALLDONE")
    return 0 if N["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
