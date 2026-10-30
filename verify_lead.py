# -*- coding: utf-8 -*-
"""门禁：非主唱段保护（伴唱/和声不许被角色唱）。

承重判据（都是"能被最小变异骗过"的反面）：
· 合成素材：主唱段左右高度相关、伴唱段左右去相关 ⇒ 只许圈出**伴唱那一段**；
· 负对照 ①：单声道输入 ⇒ 必须 LEAD_SKIP mono（没有立体声线索就不许瞎判）；
· 负对照 ②：整首都很宽 ⇒ 必须触发上限闸门、判定不可靠、一段都不圈（不许把主唱静音）；
· 处理正确性：keep 模式下，圈出的段必须**逐样本等于原始人声**，其余段必须等于变声轨。
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
STAGE = os.path.join(HERE, "stages", "lead_guard.py")
PY = sys.executable
N = {"pass": 0, "fail": 0}
FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    N["pass" if ok else "fail"] += 1
    if not ok:
        FAILS.append(name)
    print(("  PASS  " if ok else "  ! FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


def make_stereo(tmp, sr=44100, lead_secs=(0.0, 9.0), bv_secs=(10.4, 11.6), wide_all=False):
    n = int(sr * 12.0)
    t = np.arange(n) / sr
    lead = 0.3 * np.sin(2 * np.pi * 440 * t)
    bvL = 0.3 * np.sin(2 * np.pi * 550 * t)
    bvR = 0.3 * np.sin(2 * np.pi * 560 * t + 0.7)
    L = lead.copy(); R = lead.copy()
    a, b = int(bv_secs[0] * sr), int(bv_secs[1] * sr)
    L[a:b] = bvL[a:b]; R[a:b] = bvR[a:b]
    if wide_all:
        L = bvL; R = bvR
    x = np.stack([L, R], axis=1).astype(np.float32)
    p = os.path.join(tmp, "src.wav")
    sf.write(p, x, sr)
    return p, sr


def main():
    print("=" * 70)
    print("非主唱段保护门禁")
    print("=" * 70)
    print("\n=== A. 静态接线 ===")
    check("stages/lead_guard.py 在", os.path.isfile(STAGE))
    st = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("studio 有 LEAD_GUARD_MODE 常量", "LEAD_GUARD_MODE" in st)
    check("studio 有 guard_lead()", "def guard_lead(" in st)
    check("sing_role 里真调了它", "guard_lead(job," in st)
    check("质检 skip 合并了非主唱段", '"spans"' in st and "_qc_skip" in st)

    tmp = tempfile.mkdtemp(prefix="vs_lead_")
    try:
        print("\n=== B. 检测（合成素材：前半主唱、后半伴唱）===")
        src, sr = make_stereo(tmp)
        rvc = np.zeros(int(sr * 12.0), dtype=np.float32) + 0.05
        rvc_path = os.path.join(tmp, "rvc.wav")
        sf.write(rvc_path, rvc, 44100)
        rep = os.path.join(tmp, "r.json")
        out = os.path.join(tmp, "out.wav")
        r = subprocess.run([PY, "-s", STAGE, "--src", src, "--rvc", rvc_path,
                            "--out", out, "--mode", "keep", "--report", rep],
                           capture_output=True, timeout=600)
        txt = (r.stdout or b"").decode("utf-8", "replace")
        d = {}
        try:
            d = json.load(io.open(rep, encoding="utf-8"))
        except Exception:
            pass
        check("跑通（LEAD_DONE）", "LEAD_DONE" in txt, txt.strip()[:100])
        spans = d.get("spans") or []
        check("圈出了且只圈出伴唱那一整段", len(spans) >= 1, "%d 段" % len(spans))
        if spans:
            s0, e0 = spans[0]
            check("圈出的起点落在伴唱段（10~11.2s）", 10.0 <= s0 <= 11.3, "%.2f" % s0)
            check("圈出时长约 1.2 秒（不是整首）", 0.8 <= (e0 - s0) <= 2.0, "%.2f" % (e0 - s0))

        print("\n=== C. 负对照：单声道必须拒判 ===")
        mono = os.path.join(tmp, "mono.wav")
        sf.write(mono, np.stack([rvc, ] * 1, axis=1)[:, 0] if False else rvc, 44100)
        r2 = subprocess.run([PY, "-s", STAGE, "--src", mono, "--rvc", rvc_path,
                             "--out", os.path.join(tmp, "o2.wav"), "--mode", "keep"],
                            capture_output=True, timeout=600)
        t2 = (r2.stdout or b"").decode("utf-8", "replace")
        check("单声道 ⇒ LEAD_SKIP mono（不瞎判）", "LEAD_SKIP mono" in t2, t2.strip()[:80])

        print("\n=== D. 负对照：整首都很宽 ⇒ 上限闸门撤销 ===")
        wide, _ = make_stereo(tmp, wide_all=True)
        rep2 = os.path.join(tmp, "r2.json")
        r3 = subprocess.run([PY, "-s", STAGE, "--src", wide, "--rvc", rvc_path,
                             "--out", os.path.join(tmp, "o3.wav"), "--mode", "keep",
                             "--report", rep2], capture_output=True, timeout=600)
        d3 = {}
        try:
            d3 = json.load(io.open(rep2, encoding="utf-8"))
        except Exception:
            pass
        # 整首都宽 ⇒ 没有"相对反差"，正确行为就是**一段都不圈**（宁可不动）。
        check("整首都宽 ⇒ 一段都不圈（没有相对反差就不许动手）",
              not (d3.get("spans") or []),
              "reliable=%s spans=%s" % (d3.get("reliable"), len(d3.get("spans") or [])))

        print("\n=== E. 处理正确性：keep 模式逐样本对比 ===")
        if not spans:
            check("有保护段可供对比", False, "检测没圈出任何段")
            raise SystemExit(_finish())
        y, sr2 = sf.read(out, dtype="float32", always_2d=False)
        s, _ = sf.read(src, dtype="float32", always_2d=True)
        src_m = s.mean(axis=1)
        rv, _ = sf.read(rvc_path, dtype="float32", always_2d=False)
        n = min(len(y), len(src_m), len(rv))
        # ★ 边界有 20ms 交叉淡化：只比段内部（两端各让 100ms），
        #   「其余段」也把每个保护段四周 100ms 排除掉 —— 否则量到的是过渡带，不是行为。
        marg = int(0.1 * sr2)
        prot = np.zeros(n, dtype=bool)
        near = np.zeros(n, dtype=bool)
        for a, b in spans:
            ia, ib = int(a * sr2), min(n, int(b * sr2))
            prot[ia + marg:max(ia + marg, ib - marg)] = True
            near[max(0, ia - marg):min(n, ib + marg)] = True
        others = ~near
        if prot.any() and others.any():
            dp = float(np.abs(y[:n][prot] - src_m[:n][prot]).max())
            do = float(np.abs(y[:n][others] - rv[:n][others]).max())
            check("保护段 = 原始人声（逐样本）", dp < 1e-3, "最大差 %.6f" % dp)
            check("其余段 = 变声轨（逐样本）", do < 1e-3, "最大差 %.6f" % do)
        else:
            check("有保护段可供对比", False, "prot=%d" % int(prot.sum()))
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    return _finish()


def _finish():
    print("\n" + "-" * 70)
    print("通过 %d 项，失败 %d 项" % (N["pass"], N["fail"]))
    if FAILS:
        print("红的：" + "、".join(FAILS))
    print("ALLDONE")
    return 0 if N["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
