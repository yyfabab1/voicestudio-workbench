r"""门禁：人声分离的**质量尺子** + 自动选优（stages/sep_eval.py / studio 选模）。

为什么单开一关：分离是整条链的**第一道**，它坏了后面全坏，而且坏得极安静 ——
日志里永远是"人声轨 xx 秒 / 伴奏轨 xx 秒"，看不出漏了多少伴奏、削了多少人声。
以前根本没有量化，所以"漏一片鼓进来"和"把人声削掉一半"长得一模一样。

★ 本关的核心是**尺子自检**（每一段都配"换坏版本必须变红"的负对照）：
  A 真值 SDR：算得对不对（完全相同 → 高分；换个信号 → 低分；音量差不影响分）
  B 评分排序：拿**合成真值**验证 score 跟真 SDR 同序（这是权重的出处）
  C 坏分离必须得低分：没分离 / 削过头 / 漏一堆伴奏，三种都要被压下去
  D 真模型跑一遍：得分必须高于"随便给一份"的坏版本（证明整条链是通的）

用法（GSV 的 python：要 numpy / soundfile，且 uvr_cli 要在那个环境下跑）：
    <GSV>/runtime/python.exe -s verify_sep.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "stages"))
import sep_eval  # noqa: E402

GSV_ROOT = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0"
PY = os.path.join(GSV_ROOT, "runtime", "python.exe")
UVR = os.path.join(HERE, "stages", "uvr_cli.py")
MODEL = "model_bs_roformer_ep_317_sdr_12.9755"
SR = 44100
DUR = 12.0

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def _wav(path, y, sr=SR, scale=1.0):
    """★ 绝不在这里做"峰值归一化"。

    每个文件各自归一化会把**增益关系**抹掉：人声轨的峰值因数（约 8dB）比含鼓的
    混音（约 13dB）低得多，各自归一化之后人声轨的 RMS 反而比混音还大，
    body_ratio 算出 1.74 ⇒ 触发"根本没分离"的惩罚 ⇒ 干净版被压到 12 分，
    比"没分离"还低（实测出过这个假红）。真实链路里这几个文件是同一次分离的
    输出，增益本来就是对齐的，所以这里一律按**同一个** scale 写。
    """
    y = np.asarray(y, dtype=np.float64)
    sf.write(path, (y * scale).astype(np.float32), sr, subtype="PCM_16")


def vocal(dur=DUR, f0=220.0):
    """合成一段"人声"：基频 + 谐波 + 颤音 + 音节包络（有停顿 ⇒ 有静音段）。"""
    t = np.arange(int(dur * SR)) / float(SR)
    y = np.zeros_like(t)
    # 4 个"乐句"，句间留 0.35 秒空白 —— 静音段是后面判"漏没漏伴奏"的关键
    seg = dur / 4.0
    for i in range(4):
        s = i * seg
        e = s + seg - 0.35
        m = (t >= s) & (t < e)
        tt = t[m] - s
        vib = 1.0 + 0.02 * np.sin(2 * np.pi * 5.0 * tt)
        ph = 2 * np.pi * f0 * vib * tt
        y[m] = sum((1.0 / k) * np.sin(k * ph) for k in range(1, 8))
        # 音节起伏：让包络不是一条直线（否则"动态范围"这个指标失去意义）
        y[m] *= (0.45 + 0.55 * np.abs(np.sin(2 * np.pi * 2.3 * tt)))
    return y


def inst(dur=DUR):
    """合成一段"伴奏"：低频 bass + 和弦 + 高频打击（跟人声**完全不相干**）。"""
    t = np.arange(int(dur * SR)) / float(SR)
    y = 0.5 * np.sin(2 * np.pi * 80.0 * t)
    for f in (196.0, 246.9, 293.7):
        y = y + 0.25 * np.sin(2 * np.pi * f * t + np.random.rand())
    # 打击：每 0.5 秒一下，带高频（漏进来时会被 hf 指标抓到）
    for k in range(int(dur / 0.5)):
        i = int(k * 0.5 * SR)
        n = int(0.05 * SR)
        env = np.exp(-np.linspace(0, 6, n))
        y[i:i + n] += 0.6 * env * np.random.RandomState(k).randn(n)
    return y


def real_vocal(secs=12.0):
    """从角色素材里拼一段**真实干声**（用于端到端验证）。

    返回 44100Hz 的一维数组；没有素材返回 None。
    """
    import glob

    from scipy.signal import resample_poly

    picks = []
    for d in sorted(glob.glob(os.path.join(HERE, "data", "roles", "*", "slices"))):
        fs = [f for f in sorted(os.listdir(d)) if f.lower().endswith(".wav")]
        picks += [os.path.join(d, f) for f in fs[:60]]
    if not picks:
        return None
    out, total = [], 0.0
    for p in picks:
        if total >= secs:
            break
        try:
            a, sr = sf.read(p)
        except Exception:
            continue
        if a.ndim > 1:
            a = a.mean(axis=1)
        if sr != SR:
            g = __import__("math").gcd(int(sr), SR)
            a = resample_poly(a, SR // g, int(sr) // g)
        out.append(np.asarray(a, dtype=np.float64))
        out.append(np.zeros(int(0.2 * SR)))     # 句间留白：真实素材本来就有停顿
        total += len(a) / float(SR) + 0.2
    if not out:
        return None
    y = np.concatenate(out)
    return y[:int(secs * SR)]


def main():
    os.chdir(HERE)
    tmp = tempfile.mkdtemp(prefix="vs_sep_")
    try:
        p_v = os.path.join(tmp, "v.wav")
        p_i = os.path.join(tmp, "i.wav")
        p_m = os.path.join(tmp, "m.wav")
        v, i = vocal(), inst()
        # 按 0 dB SNR 混（真值已知：人声就是 v）
        rv, ri = np.sqrt(np.mean(v * v)), np.sqrt(np.mean(i * i))
        m = v + i * (rv / ri)
        sc = 0.8 / (np.max(np.abs(m)) or 1.0)
        _wav(p_v, v, scale=sc)
        _wav(p_i, i, scale=sc)
        _wav(p_m, m, scale=sc)

        # ---------------- A 真值 SDR ----------------
        print("\n=== A. 真值 SDR（尺子本身对不对）===")
        s_same = sep_eval.sdr(p_v, p_v)
        check("完全一样 ⇒ SDR 很高（>40dB）", s_same > 40.0, "%.1f dB" % s_same)
        p_half = os.path.join(tmp, "v_half.wav")
        _wav(p_half, v * 0.25, scale=sc)
        s_half = sep_eval.sdr(p_v, p_half)
        check("★ 只是音量小 4 倍 ⇒ SDR 不该掉（算的是质量不是音量）",
              s_half > 40.0, "%.1f dB" % s_half)
        s_other = sep_eval.sdr(p_v, p_i)
        check("★ 换成完全不相干的信号 ⇒ SDR 必须很低或为负",
              s_other < 0.0, "%.1f dB" % s_other)

        # ---------------- B 评分 vs 真值排序 ----------------
        print("\n=== B. 评分跟真值同序（权重的出处）===")
        # 三个"候选分离结果"：干净 / 漏一点 / 漏很多
        p_g = os.path.join(tmp, "g.wav")          # 干净：只有 3% 伴奏漏进来
        p_s = os.path.join(tmp, "s.wav")          # 漏 25%
        p_b = os.path.join(tmp, "b.wav")          # 漏 60%
        _wav(p_g, v + 0.03 * i, scale=sc)
        _wav(p_s, v + 0.25 * i, scale=sc)
        _wav(p_b, v + 0.60 * i, scale=sc)
        g = (sep_eval.score(p_m, p_g, p_i), sep_eval.sdr(p_v, p_g))
        s = (sep_eval.score(p_m, p_s, p_i), sep_eval.sdr(p_v, p_s))
        b = (sep_eval.score(p_m, p_b, p_i), sep_eval.sdr(p_v, p_b))
        print("    干净 score=%.1f sdr=%.2f | 漏25%% score=%.1f sdr=%.2f | "
              "漏60%% score=%.1f sdr=%.2f" % (g[0]["score"], g[1], s[0]["score"],
                                              s[1], b[0]["score"], b[1]))
        check("漏得越多，真值 SDR 越低（前提：真值这把尺子有判别力）",
              g[1] > s[1] > b[1], "%.2f > %.2f > %.2f" % (g[1], s[1], b[1]))
        check("★ 漏得越多，评分越低（score 与真值同序 —— 权重就是这么定出来的）",
              g[0]["score"] > s[0]["score"] > b[0]["score"],
              "%.1f > %.1f > %.1f" % (g[0]["score"], s[0]["score"], b[0]["score"]))
        check("★ 漏伴奏那一项（leak）也跟着单调上升（它是主权重，必须单调）",
              g[0]["leak"] < s[0]["leak"] < b[0]["leak"],
              "%.3f < %.3f < %.3f" % (g[0]["leak"], s[0]["leak"], b[0]["leak"]))

        # ---------------- C 三种坏分离必须低分 ----------------
        print("\n=== C. 坏分离必须被压下去（负对照）===")
        p_none = os.path.join(tmp, "none.wav")     # 压根没分离：直接把原曲当人声
        p_kill = os.path.join(tmp, "kill.wav")     # 削过头：人声只剩 6%
        shutil.copyfile(p_m, p_none)
        _wav(p_kill, v * 0.06, scale=sc)
        sc_none = sep_eval.score(p_m, p_none, p_i)
        sc_kill = sep_eval.score(p_m, p_kill, p_i)
        check("★ 没分离（直接拿原曲当人声）⇒ 分明显低于干净版",
              sc_none["score"] < g[0]["score"] - 5,
              "没分离=%.1f 干净=%.1f" % (sc_none["score"], g[0]["score"]))
        check("★ 削过头（人声只剩 6%%）⇒ 分明显低于干净版",
              sc_kill["score"] < g[0]["score"] - 5,
              "削过头=%.1f 干净=%.1f" % (sc_kill["score"], g[0]["score"]))
        check("★ body 兜底生效：削过头时 body 分被拉下来（不是满分）",
              sc_kill["body"] < 0.6, "body=%.3f" % sc_kill["body"])

        # ---------------- D 真跑一个模型（必须用**真实人声**）----------------
        # ★ 为什么 D 段不能接着用上面那段合成"人声"：RoFormer 是在真实歌声上训的，
        #   纯正弦谐波堆在它眼里压根不是人声 —— 实测它把整段合成人声当伴奏丢了，
        #   真值 SDR = -41dB。那是**测试信号不像人**，不是模型坏了；拿它去判
        #   "模型有没有在干活"只会得到一个假红。所以这里换成角色素材里的真干声。
        print("\n=== D. 真跑一遍分离器（用真实人声，整条链是通的）===")
        rvoc = real_vocal(secs=12.0)
        if rvoc is None:
            check("角色素材里有真实干声可用来做端到端验证", False,
                  "data/roles/*/slices 下没有 wav")
        else:
            i2 = inst(dur=len(rvoc) / float(SR))
            rv2, ri2 = np.sqrt(np.mean(rvoc ** 2)), np.sqrt(np.mean(i2 ** 2))
            m2 = rvoc + i2 * (rv2 / ri2)
            sc2 = 0.8 / (np.max(np.abs(m2)) or 1.0)
            p_rv = os.path.join(tmp, "rv.wav")
            p_ri = os.path.join(tmp, "ri.wav")
            p_rm = os.path.join(tmp, "rm.wav")
            p_rnone = os.path.join(tmp, "rnone.wav")
            p_rbad = os.path.join(tmp, "rbad.wav")
            _wav(p_rv, rvoc, scale=sc2)
            _wav(p_ri, i2, scale=sc2)
            _wav(p_rm, m2, scale=sc2)
            shutil.copyfile(p_rm, p_rnone)
            _wav(p_rbad, rvoc + 0.60 * i2, scale=sc2)
            sc_rnone = sep_eval.score(p_rm, p_rnone, p_ri)
            sc_rbad = sep_eval.score(p_rm, p_rbad, p_ri)
            pin = os.path.join(tmp, "in")
            pv = os.path.join(tmp, "out_v")
            pi = os.path.join(tmp, "out_i")
            os.makedirs(pin, exist_ok=True)
            shutil.copyfile(p_rm, os.path.join(pin, "source.wav"))
            t0 = time.time()
            p = subprocess.run('"%s" -s "%s" %s %s "%s" "%s" "%s" 10 wav cuda True'
                               % (PY, UVR, GSV_ROOT, MODEL, pin, pv, pi),
                               shell=True, cwd=GSV_ROOT, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
            vs = [f for f in os.listdir(pv) if f.lower().endswith(".wav")] if os.path.isdir(pv) else []
            ins = [f for f in os.listdir(pi) if f.lower().endswith(".wav")] if os.path.isdir(pi) else []
            if not vs or not ins:
                check("分离器跑通（真实人声那一路）", False, (p.stdout or "")[-200:])
            else:
                vv, ii = os.path.join(pv, vs[0]), os.path.join(pi, ins[0])
                sc = sep_eval.score(p_rm, vv, ii)
                sd = sep_eval.sdr(p_rv, vv)
                print("    用时 %.0fs，score=%.1f leak=%.3f floor=%.1f body=%.3f，"
                      "真值 SDR=%.2f dB" % (time.time() - t0, sc["score"], sc["leak"],
                                            sc["floor_db"], sc["body_ratio"], sd))
                check("真分离出来的比「没分离」分高（模型真的在干活）",
                      sc["score"] > sc_rnone["score"],
                      "真分离=%.1f 没分离=%.1f" % (sc["score"], sc_rnone["score"]))
                check("真分离出来的比「漏 60%% 伴奏」分高",
                      sc["score"] > sc_rbad["score"],
                      "真分离=%.1f 漏60%%=%.1f" % (sc["score"], sc_rbad["score"]))
                check("真分离的真值 SDR 是正的（分离有效，不是把人声丢了）",
                      sd > 0.0, "%.2f dB" % sd)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 62)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 失败项：" + "、".join(BAD))
    else:
        print("结论：分离质量门禁全绿")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
