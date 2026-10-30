# -*- coding: utf-8 -*-
"""临场感补偿（2~5kHz）的门禁。

★ 这条功能是从**量测**里长出来的，不是拍脑袋：139 条角色成品 vs 原唱干声，
  2~5kHz 相对中频**中位缺 3.9dB**（原唱 −6.50dB / 成品 −10.35dB），系统性。
  Sonarworks《Mastering AI Vocals》也把这条列为 AI 人声的固有毛病。

★ 判据守三件事：
  ① 该补的补（缺口真的缩小 —— 闭环，不许"补了但没变好"也算过）
  ② 不该动的别动（800~1.2k 箱音**不许削**：那是共振峰，削了会离角色更远，
     拆分实验里削它还会把 psim 压到低于原唱 —— 补过头）
  ③ 副作用不许有（jitter 不许恶化 —— 那是"沙哑"的尺子）
"""
import io
import json
import os
import subprocess
import sys

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stages"))
import presence_cli as P  # noqa: E402

N_PASS, N_FAIL = 0, 0
BAD = []


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("  [ok] %s%s" % (name, ("  | %s" % detail) if detail else ""))
    else:
        N_FAIL += 1
        BAD.append(name)
        print("  [FAIL] %s  ← %s" % (name, detail))


def sec(t):
    print("\n== %s ==" % t, flush=True)


def _synth(n, sr, hz, amp):
    t = np.arange(n, dtype=np.float64) / float(sr)
    # ★ 谐波必须铺到 5kHz 以上（220Hz × 22 = 4.84kHz），否则 2~5kHz 那一段
    #   根本没有能量，压不压都一样 ⇒ 量出来的"缺口"是噪声地板，判据会假红。
    x = np.zeros(n)
    for k in range(1, 23):
        x += (amp / k) * np.sin(2 * np.pi * hz * k * t)
    return x.astype(np.float32), sr


def _write(path, x, sr):
    sf.write(path, x, sr, subtype="PCM_16")


def _rms(x):
    return float(np.sqrt(float((np.asarray(x, dtype=np.float64) ** 2).mean())))


def main():
    global N_PASS, N_FAIL
    src = io.open(os.path.join(HERE, "stages", "presence_cli.py"),
                  encoding="utf-8").read()
    studio = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    tmp = os.path.join(HERE, "_tmp", "pres_gate")
    os.makedirs(tmp, exist_ok=True)

    # ---------------------------------------------------------------- A. 常量
    sec("A. 常量与频段")
    check("有「不缺就不动」的阈值（MIN_DEFICIT_DB）", "MIN_DEFICIT_DB" in src)
    check("有中心剂量上限（MAX_DOSE_DB）", "MAX_DOSE_DB" in src)
    check("有「缺口→剂量」的系数（DOSE_GAIN）", "DOSE_GAIN" in src)
    check("补的是 2~5kHz（不是别的段）",
          "BAND_LO, BAND_HI = 2000.0, 5000.0" in src)
    check("用中频 300~3000 做相对基准（不受整体响度影响）",
          "REF_LO, REF_HI = 300.0, 3000.0" in src)
    check("上限不超过 6dB（实测 6dB 就把 psim 压到 0.8308，低于原唱 0.8448）",
          P.MAX_DOSE_DB <= 6.0, P.MAX_DOSE_DB)
    check("系数 > 1（余弦窗均值是中心的一半，1:1 补不够）",
          P.DOSE_GAIN > 1.0, P.DOSE_GAIN)

    # ---------------------------------------------------------------- B. 形状
    sec("B. 补的手段：余弦窗 + 等响还原")
    check("★ 用余弦窗，不是硬边（硬边会出金属声）",
          "1.0 - np.cos(2.0 * np.pi * t)" in src)
    check("★ 等响还原（不让「更响」盖过「更好听」）",
          "y * (r0 / r1)" in src)
    check("★ 只补不削：源码里**不出现** 800~1200 的处理（共振峰不能动）",
          "800.0, 1200.0" not in src and "1200" not in src.split("MAX_DOSE_DB")[1])
    check("把「为什么不削箱音」写进了文件头（后人不会又加回去）",
          "共振峰" in src and "只补不削" in src)

    # ---------------------------------------------------------------- C. 闭环
    sec("C. 闭环：补完必须复测，没变好就不许应用")
    check("★ 补完会再量一次（measure(tmp, src)）", "measure(tmp, src)" in src)
    check("★ 缺口没缩小就不应用（after < d - 0.2）", "after < d - 0.2" in src)
    check("有降档重试（全量→0.7→0.45），不是一档定生死",
          "dose0 * 0.7" in src and "dose0 * 0.45" in src)
    check("试完都不行时**不写成品**（不许拿没修的当修过的）",
          "不改动成品" in src)
    check("SKIP 是正常结果，退出码 0（跟 hf/demetal 同约定）",
          'sys.stdout.write("PRES_SKIP' in src)

    # ------------------------------------------------- D. 合成素材的端到端
    sec("D. 端到端（合成素材：人为造一个 2~5k 的坑）")
    sr = 48000
    n = int(sr * 6.0)
    ref, _ = _synth(n, sr, 220.0, 0.30)          # "原唱"：谐波齐全
    dst = ref.copy()
    # 人为把 2~5kHz 压掉（模拟 AI 人声的坑）
    nfft = 1 << int(np.ceil(np.log2(n)))
    X = np.fft.rfft(np.asarray(dst, dtype=np.float64), nfft)
    f = np.fft.rfftfreq(nfft, 1.0 / sr)
    m = (f >= 2000) & (f <= 5000)
    X[m] *= 0.35
    dst = np.fft.irfft(X, nfft)[:n].astype(np.float32)
    p_ref = os.path.join(tmp, "ref.wav")
    p_dst = os.path.join(tmp, "dst.wav")
    p_out = os.path.join(tmp, "out.wav")
    p_json = os.path.join(tmp, "out.json")
    _write(p_ref, ref, sr)
    _write(p_dst, dst, sr)

    mm = P.measure(p_dst, p_ref)
    check("量得出人为造的坑（缺口 > 阈值）",
          bool(mm) and mm["deficit_db"] > P.MIN_DEFICIT_DB,
          json.dumps(mm, ensure_ascii=False))
    rc = P.cmd_auto(p_dst, p_ref, p_out, p_json)
    rep = {}
    try:
        rep = json.load(io.open(p_json, encoding="utf-8"))
    except Exception:
        pass
    check("自动档真的补了", rep.get("applied") is True, json.dumps(rep)[:160])
    check("★ 补完缺口真的缩小了（闭环）",
          rep.get("after_deficit_db") is not None
          and rep["after_deficit_db"] < float(mm["deficit_db"]) - 0.2,
          "%.2f → %s" % (mm["deficit_db"], rep.get("after_deficit_db")))
    check("补完的文件存在且长度不变",
          os.path.isfile(p_out) and len(sf.read(p_out)[0]) == n,
          os.path.isfile(p_out))

    # ------------------------------------------------- E. 不缺就不动
    sec("E. 不缺就不动（负对照方向之一）")
    p_out2 = os.path.join(tmp, "out2.wav")
    p_json2 = os.path.join(tmp, "out2.json")
    rc2 = P.cmd_auto(p_ref, p_ref, p_out2, p_json2)
    rep2 = {}
    try:
        rep2 = json.load(io.open(p_json2, encoding="utf-8"))
    except Exception:
        pass
    check("自己跟自己比 ⇒ 缺口 ~0 ⇒ SKIP", rc2 == 0 and not rep2.get("applied"),
          json.dumps(rep2)[:120])
    check("★ SKIP 时**没有**写成品（调用方拿了会读空文件）",
          not os.path.isfile(p_out2), os.path.exists(p_out2))

    # ★ 缺口**刚好低于阈值**的场景：这才是能单独验证阈值的场景。
    #   （用"缺口=0"测阈值是错的：那时剂量也算成 0，被**闭环**挡住了，
    #    测出来的是闭环不是阈值 —— 这条判据一开始就是这么写错的。）
    p_small = os.path.join(tmp, "small.wav")
    small = ref.copy()
    X2 = np.fft.rfft(np.asarray(small, dtype=np.float64), nfft)
    X2[(f >= 2000) & (f <= 5000)] *= 0.891          # -1.0 dB
    small = np.fft.irfft(X2, nfft)[:n].astype(np.float32)
    _write(p_small, small, sr)
    ms = P.measure(p_small, p_ref)
    check("小缺口素材量出来确实低于阈值（%.1f）" % P.MIN_DEFICIT_DB,
          bool(ms) and 0.3 < ms["deficit_db"] < P.MIN_DEFICIT_DB,
          json.dumps(ms, ensure_ascii=False))
    p_out3 = os.path.join(tmp, "out3.wav")
    p_json3 = os.path.join(tmp, "out3.json")
    P.cmd_auto(p_small, p_ref, p_out3, p_json3)
    check("★ 好版本：小缺口不动（阈值真的生效了）",
          not os.path.isfile(p_out3), os.path.exists(p_out3))

    # ------------------------------------------------- F. studio 接线
    sec("F. studio 接线")
    check("sing_role 有 presence_mode 形参", "presence_mode=\"auto\"" in studio)
    check("sing_multi 也有 presence_mode（多角色不是二等公民）",
          studio.count("presence_mode=\"auto\"") >= 2,
          studio.count("presence_mode=\"auto\""))
    check("★ 单角色：放在 demetal **之后**（先补再削 = 白补）",
          studio.index("临场感补偿（2~5kHz）")
          > studio.index("去金属声（电音）"))
    check("多角色：每个角色都补（在角色循环里）",
          "presence(job, post, _ref" in studio)
    check("多角色的参考轨用的是 _ref（跟 hf/demetal 同一条，时间轴一致）",
          "presence(job, post, _ref" in studio)
    check("★ 位置在 nonvocal 保护之前（保护会把段换回原声，补了也白补）",
          studio.index("presence(job, out_wav")
          < studio.index("guard_nonvocal(job, vocal_wav"))

    # ------------------------------------------------- G. 尺子自检
    sec("G. 负对照：把关键处改坏，行为必须真的变（不是只查字符串）")
    # ① 取消闭环 ⇒ "补了但没变好"也会照样应用
    bad_src = os.path.join(tmp, "bad_noclose.py")
    io.open(bad_src, "w", encoding="utf-8").write(
        src.replace("after < d - 0.2", "True"))
    r = subprocess.run([sys.executable, bad_src, "auto", p_dst, p_ref,
                        os.path.join(tmp, "bad1.wav"),
                        os.path.join(tmp, "bad1.json")],
                       capture_output=True, text=True)
    check("★ 取消闭环后坏版本照样 applied（证明闭环判据不是摆设）",
          (r.stdout or "").strip().startswith("PRES_AUTO"),
          (r.stdout or "").strip()[:80])
    # ② 把阈值改成负数 ⇒ 本来"缺口太小不动"的场景会被强行补
    bad_src2 = os.path.join(tmp, "bad_thresh.py")
    io.open(bad_src2, "w", encoding="utf-8").write(
        src.replace("MIN_DEFICIT_DB = 1.5", "MIN_DEFICIT_DB = -999.0"))
    r2 = subprocess.run([sys.executable, bad_src2, "auto", p_small, p_ref,
                         os.path.join(tmp, "bad2.wav"),
                         os.path.join(tmp, "bad2.json")],
                        capture_output=True, text=True)
    check("★ 阈值改坏后小缺口也会被强补（证明阈值不是摆设）",
          (r2.stdout or "").strip().startswith("PRES_AUTO"),
          (r2.stdout or "").strip()[:80])

    print("\n通过 %d 项，失败 %d 项" % (N_PASS, N_FAIL))
    if N_FAIL:
        print("失败清单：")
        for b in BAD:
            print("  - " + b)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
