# -*- coding: utf-8 -*-
r"""门禁：非歌声段保护（「一吹口哨就开始吼」那条链）。

为什么单独一关：这一步是这一版新加的，而它**错了也不会让任何既有判据变红**
—— 成品照样生成、任务照样成功，只是那几秒里角色在假声吼。所以必须有一关
专门盯着"判得准不准、三种模式各自对不对"。

**判据自足**：不依赖用户库里恰好有一首带口哨的歌 —— 素材现场合成：
    唱 3 秒 → 口哨 2 秒 → 唱 3 秒
"唱"用 12 次谐波堆叠 + 噪声（谱平坦度高），"口哨"用 662 Hz 纯正弦（谱平坦度极低），
这正是 nonvocal_guard 用来区分的物理量。RVC 侧则模拟"把口哨也当唱了"：
口哨那 2 秒换成降 19 半音的谐波音、且幅度放大（真实实测是 2.97 倍）。

断言（每条都能红）：
  1. keep 模式：口哨段来自**原始人声**（与 src 相关 > 0.9），唱段仍来自 RVC
  2. mute 模式：口哨段被压到接近静音（< 唱段的 5%）
  3. off  模式：输出与 RVC 输出**逐样本相同**（旧行为没被破坏）
  4. 判出的段落在口哨的真实位置上（±0.3 秒）
  5. ★ 负对照：全曲都是唱（没有口哨）时，判定段数必须是 **0**
     —— 不红就说明这条判据只是在"凡事先保护一下"
  6. ★★ 负对照 B：**干净的高音延音**（不加噪声的谐波串，平坦度 0.0009）
     必须判 0 段；同时断言"旧判据确实会圈它"。
     —— 第 5 条抓不到这个 bug（素材带噪声、平坦度 0.49），第 6 条才抓得到。
        这就是 2026-10-26 修的"少声音"：55 段真歌声被当口哨换回原唱。

用法（用 RVC 自带解释器跑，它有 numpy/soundfile）：
    runtime\python.exe -s verify_nonvocal.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
GUARD = os.path.join(HERE, "stages", "nonvocal_guard.py")
SR = 16000
OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name + (("  | " + detail) if detail else ""),
          flush=True)


def sing(dur, f0=220.0, amp=0.5, seed=7):
    """合成"唱"：带颤音的谐波堆叠 + 一点噪声（噪声是为了让谱平坦度落在
    正常歌唱的区间，纯谐波堆叠太平坦，会被当成纯音）。"""
    n = int(dur * SR)
    t = np.arange(n) / float(SR)
    f = f0 * (1.0 + 0.03 * np.sin(2 * np.pi * 5.5 * t))
    ph = 2 * np.pi * np.cumsum(f) / SR
    y = np.zeros(n, dtype=np.float64)
    for k in range(1, 13):
        y += (1.0 / k) * np.sin(k * ph)
    y = y / max(1e-9, np.max(np.abs(y))) * amp
    y += 0.05 * np.random.RandomState(seed).randn(n)
    # 峰值压到 0.95 以内：写 PCM_16 时超 1.0 会被削幅，读回来就和原数组对不上，
    # 会把"逐样本相同"那条判据误判成红（实测踩到过）。
    return np.clip(y, -0.95, 0.95).astype(np.float32)


def whistle(dur, f0=662.0, amp=0.35):
    """合成口哨：纯正弦（谱平坦度极低）。"""
    n = int(dur * SR)
    t = np.arange(n) / float(SR)
    return (amp * np.sin(2 * np.pi * f0 * t)).astype(np.float32)


def clean_high(dur=2.5, f0=350.0, amp=0.5, nh=10):
    """干净的高音延音：谐波串、**不加噪声**、只有一点自然颤音。

    ★ 这是"少声音"那个 bug 的靶子（2026-10-26）。实测：
        平坦度中位 0.0009（!!）  谱峰数 10  基频占能量 0.34  2.5k以上 0.12
    旧判据只看"平坦度 < 0.05 且 f0 ≥ 300" ⇒ 整段被当成口哨，换回原唱。
    分离后的真人人声就是这样（实测那首歌 55 段、平坦度 0.028）。
    """
    n = int(dur * SR)
    t = np.arange(n) / float(SR)
    f = f0 * (1.0 + 0.004 * np.sin(2 * np.pi * 5.0 * t))
    ph = 2 * np.pi * np.cumsum(f) / SR
    y = np.zeros(n)
    for k in range(1, nh + 1):
        y += (1.0 / k) * np.sin(k * ph)
    y = y / max(1e-9, np.max(np.abs(y))) * amp
    return np.clip(y, -0.95, 0.95).astype(np.float32)


def run_guard(src, rvc, out, mode, report=""):
    cmd = [sys.executable, "-s", GUARD, "--src", src, "--rvc", rvc, "--out", out,
           "--mode", mode]
    if report:
        cmd += ["--report", report]
    p = subprocess.run(cmd, cwd=RVC_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def corr(a, b):
    a = a - a.mean()
    b = b - b.mean()
    d = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / d) if d > 1e-9 else 0.0


def main():
    tmp = tempfile.mkdtemp(prefix="vs_nonvocal_")
    try:
        # ---- 素材：唱 3s | 口哨 2s | 唱 3s ----
        s1, w, s2 = sing(3.0), whistle(2.0), sing(3.0, f0=233.0, seed=11)
        src = np.concatenate([s1, w, s2])
        # RVC 侧：口哨段被"唱"成了降 19 半音的谐波音（幅度放大，模拟实测 2.97×）
        rvc = np.concatenate([sing(3.0, f0=98.0), sing(2.0, f0=98.0, amp=0.9, seed=3),
                              sing(3.0, f0=104.0, seed=13)])
        p_src = os.path.join(tmp, "src.wav")
        p_rvc = os.path.join(tmp, "rvc.wav")
        sf.write(p_src, src, SR)
        sf.write(p_rvc, rvc, SR)
        print("素材：唱 3.0s | 口哨 2.0s（3.0~5.0）| 唱 3.0s，共 %.1f 秒\n"
              % (len(src) / float(SR)))

        # ---- 1. keep ----
        rep = os.path.join(tmp, "keep.json")
        o_keep = os.path.join(tmp, "keep.wav")
        rc, log = run_guard(p_src, p_rvc, o_keep, "keep", rep)
        if rc != 0:
            print(log[-800:])
        check("keep 模式跑通", rc == 0 and os.path.isfile(o_keep), "rc=%d" % rc)
        info = json.load(open(rep, encoding="utf-8")) if os.path.isfile(rep) else {}
        segs = info.get("segments") or []
        print("      判定段：%s  保护 %.2f 秒  平坦度中位 %.4f"
              % (segs, info.get("protected_secs", 0), info.get("flat_med", 0)))
        check("判出且只判出 1 段", len(segs) == 1, "段数 %d：%s" % (len(segs), segs))
        if segs:
            a0, b0 = segs[0]
            check("判定位置落在真实口哨段上（3.0~5.0 ±0.3s）",
                  abs(a0 - 3.0) <= 0.3 and abs(b0 - 5.0) <= 0.3,
                  "判出 %.2f~%.2f" % (a0, b0))

        yk, _ = sf.read(o_keep, dtype="float32")
        yk = yk[:len(src)]
        # 取段中间 1 秒比，避开交叉淡化区
        m0, m1 = int(3.6 * SR), int(4.6 * SR)
        rk = corr(yk[m0:m1], src[m0:m1])
        check("keep：口哨段换回了原始人声（相关 > 0.9）", rk > 0.9, "相关 %.3f" % rk)
        c0, c1 = int(0.6 * SR), int(1.6 * SR)
        rv = corr(yk[c0:c1], rvc[c0:c1])
        check("keep：唱段仍然是 RVC 的输出（相关 > 0.99）", rv > 0.99, "相关 %.3f" % rv)

        # ---- 2. mute ----
        o_mute = os.path.join(tmp, "mute.wav")
        rc, log = run_guard(p_src, p_rvc, o_mute, "mute")
        check("mute 模式跑通", rc == 0 and os.path.isfile(o_mute), "rc=%d" % rc)
        ym, _ = sf.read(o_mute, dtype="float32")
        ym = ym[:len(src)]
        rms_whistle = float(np.sqrt(np.mean(ym[m0:m1] ** 2)))
        rms_sing = float(np.sqrt(np.mean(ym[c0:c1] ** 2)))
        check("mute：口哨段被静音（< 唱段的 5%）",
              rms_whistle < 0.05 * rms_sing,
              "口哨 rms %.5f / 唱段 rms %.5f" % (rms_whistle, rms_sing))

        # ---- 3. off（旧行为必须原样保留）----
        o_off = os.path.join(tmp, "off.wav")
        rc, log = run_guard(p_src, p_rvc, o_off, "off")
        yo, _ = sf.read(o_off, dtype="float32")
        yo = yo[:len(rvc)]
        # 容差 1e-3：素材落地是 PCM_16，量化误差在 ±3e-5 量级，留足余量。
        same = bool(np.allclose(yo, rvc[:len(yo)], atol=1e-3))
        check("off：输出与 RVC 输出逐样本相同", rc == 0 and same,
              "最大差 %.2e" % (float(np.max(np.abs(yo - rvc[:len(yo)]))) if len(yo) else -1))

        # ---- 5. ★ 负对照：全曲都是唱，必须判 0 段 ----
        p_src2 = os.path.join(tmp, "src_all_sing.wav")
        p_rvc2 = os.path.join(tmp, "rvc_all_sing.wav")
        sf.write(p_src2, np.concatenate([sing(3.0), sing(2.0, f0=247.0, seed=5), sing(3.0, f0=233.0, seed=11)]), SR)
        sf.write(p_rvc2, np.concatenate([sing(3.0, f0=98.0), sing(2.0, f0=110.0, seed=5),
                                         sing(3.0, f0=104.0, seed=13)]), SR)
        rep2 = os.path.join(tmp, "neg.json")
        rc, log = run_guard(p_src2, p_rvc2, os.path.join(tmp, "neg.wav"), "keep", rep2)
        info2 = json.load(open(rep2, encoding="utf-8")) if os.path.isfile(rep2) else {}
        segs2 = info2.get("segments") or []
        check("★ 负对照：全曲都是唱 → 判 0 段（判据能红）", len(segs2) == 0,
              "段数 %d：%s | 平坦度中位 %.4f"
              % (len(segs2), segs2, info2.get("flat_med", 0)))

        # ---- 6. ★★ 负对照 B：干净的高音延音（"少声音"那个 bug 的靶子）----
        # 上面那条负对照**抓不到**这个 bug：它的素材带 0.05 噪声，平坦度 0.49，
        # 旧判据本来就不会圈。真出事的是"分离后的干净人声"——平坦度低到 0.0009~0.028。
        # 所以这里刻意用**不加噪声**的谐波串当素材，并同时断言两件事：
        #   ① 新判据不许圈它（圈了就是把角色嗓子换回原唱 = 老板听到的"少声音"）
        #   ② 旧判据（只看平坦度）必须**确实会圈它** —— 否则这条对照就是空的
        p_src3 = os.path.join(tmp, "src_clean_high.wav")
        p_rvc3 = os.path.join(tmp, "rvc_clean_high.wav")
        sf.write(p_src3, clean_high(2.5, 350.0), SR)
        sf.write(p_rvc3, sing(2.5, f0=175.0), SR)      # RVC 侧：降八度的"唱"
        rep3 = os.path.join(tmp, "clean_high.json")
        rc, log = run_guard(p_src3, p_rvc3, os.path.join(tmp, "clean_high.wav"),
                            "keep", rep3)
        info3 = json.load(open(rep3, encoding="utf-8")) if os.path.isfile(rep3) else {}
        segs3 = info3.get("segments") or []
        check("★ 负对照 B：干净的高音延音 → 判 0 段（不许把角色嗓子换回原唱）",
              len(segs3) == 0,
              "段数 %d：%s | 保护 %.2f 秒"
              % (len(segs3), segs3, info3.get("protected_secs", 0)))
        old_secs = float(info3.get("flat_only_secs") or 0)
        check("★ 负对照 B 有料可量：旧判据（只看平坦度）确实会圈住这段",
              old_secs >= 1.5, "旧判据会圈 %.2f 秒 / 共 2.50 秒" % old_secs)

        print("-" * 62)
        print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
        if BAD:
            print("!! 失败项：" + "、".join(BAD))
        else:
            print("结论：非歌声段保护门禁全绿")
        return 0 if not BAD else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
