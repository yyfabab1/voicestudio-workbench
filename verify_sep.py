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
import io
import json
import re
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


def _open(path):
    try:
        with io.open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:                                    # noqa: BLE001
        return ""


def _msst_py():
    """MSST 那条路用的解释器路径；不在盘上返回 None。

    单独抽出来是因为有两个地方要问同一件事（`_py_list` 要拿它跑 list，
    D2 段要拿它决定"真跑还是跳过"）。两处各写一遍路径必迟早对不上。
    """
    py = os.path.join(os.path.dirname(HERE), "YingMusic-Singer-Plus-main",
                      ".venv", "Scripts", "python.exe")
    return py if os.path.isfile(py) else None


def _py_list():
    """问 MSST 那条路：你的模型清单是什么、各自就绪吗。

    ★ 让载体自己报告（msst_cli list），不要在门禁里猜权重该在哪个目录 ——
      那条路的权重不在 UVR 的 weights 目录，猜必错（实测第一次就红了）。
    """
    py = _msst_py()
    if not py:
        return False, "MSST 解释器不在：%s" % os.path.join(
            os.path.dirname(HERE), "YingMusic-Singer-Plus-main", ".venv",
            "Scripts", "python.exe")
    try:
        p = subprocess.run([py, "-s", os.path.join(HERE, "stages", "msst_cli.py"),
                            "list"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=120, cwd=HERE)
    except Exception as e:                               # noqa: BLE001
        return False, "跑 msst_cli list 失败：%s" % e
    m = re.search(r"MSST_LIST (\{.*\})", (p.stdout or ""))
    if not m:
        return False, "没拿到清单：%s" % (p.stdout or p.stderr or "")[-200:]
    try:
        return True, json.loads(m.group(1))
    except Exception as e:                               # noqa: BLE001
        return False, "清单不是合法 JSON：%s" % e


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


def _read(name):
    try:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def e_wiring():
    """选模 / 二次分离的接线 —— 纯静态检查，不跑模型，几毫秒。

    为什么非要查：这两个地方各有一个"看着在工作、其实永远不生效"的坑，
    而且都不会报错、只会悄悄多花一倍时间（或悄悄不生效）。
    """
    print("\n=== E. 选模与二次分离接线（静态）===")
    S = _read("studio.py")

    # ① 二次分离的比较口径：两遍必须对着**同一个混音**打分
    check("二次分离两遍用同一个混音参考（否则第二遍天生低 30 分，永远赢不了）",
          "_sep_score(job, src, v2, i)" in S
          and "_sep_score(job, v, v2, i2)" not in S,
          "第二遍的评分参考不是原曲")
    # ② 采纳第二遍时**只换人声**，伴奏保持第一遍的
    check("采纳第二遍时只换人声轨（伴奏不许换成「人声里刮下来的残留」）",
          "v, sc1 = v2, sc2" in S and "v, i, sc1 = v2, i2, sc2" not in S,
          "伴奏被换掉了 —— 成品会既没人声也没伴奏")
    # ③ 闸门：第一遍够好就别重跑（省一整遍全曲时间）
    check("二次分离有「第一遍真的不行才做」的闸门",
          "SEP_RETRY_SCORE" in S
          and "sc1.get(\"score\") or 0) < SEP_RETRY_SCORE" in S,
          "没有闸门 = 每首都要多跑一遍全曲")

    # ④ 候选模型必须**真的装着**：配了名字但没权重，探测会全失败再退回默认
    #   ★ 别用正则去抠源码里的元组 —— 它是多行、里面还有常量名（实测第一版
    #     就是这么错的：把 `UVR_MODEL_BEST` 当成模型名去查文件）。直接读真值。
    sys.path.insert(0, HERE)
    import studio  # noqa: E402  （studio 只依赖标准库，能安全导入）
    W = os.path.join(GSV_ROOT, "tools", "uvr5", "uvr5_weights")
    msst = set(getattr(studio, "MSST_MODELS", ()) or ())
    cands = list(studio.UVR_CANDIDATES)
    check("候选模型清单读得到（至少 3 个，才谈得上「挑」）",
          len(cands) >= 3, "%s" % cands)

    missing = []
    for m in cands:
        if m in msst:
            continue          # MSST 那条路的模型不在 UVR 目录，下面单独验
        if not (os.path.isfile(os.path.join(W, m + ".pth"))
                or os.path.isfile(os.path.join(W, m + ".ckpt"))):
            missing.append(m)
        elif "mel_band" in m.lower() and not os.path.isfile(
                os.path.join(W, m + ".yaml")):
            # bs_roformer 那几只有加载器内置的默认配置（dim=512/12 层）；
            # mel_band 每个模型维度都不一样（这个 dim=384/6 层），没 yaml 必炸。
            missing.append(m + "（mel_band 缺 yaml，默认配置对不上维度）")
    check("UVR 候选模型权重都真的在盘上（配了没装 = 每次探测白跑一遍）",
          not missing, "缺：%s" % missing)

    # ④a MSST 那条路上的候选，由它**自己**报告 readiness（msst_cli list）。
    #    以前门禁统一去 UVR 的 weights 目录找，加了 mel_band_roformer_kj 之后
    #    这条判据立刻红 —— 不是产品坏了，是尺子管错人了：那条路的权重根本不在
    #    那个目录里（913MB 不外拷，见 msst_cli 的文件头）。让载体自己说话。
    msst_ok, msst_why = True, "本轮没配 MSST 候选"
    if msst:
        ok_lst, out_lst = _py_list()
        if not ok_lst:
            msst_ok, msst_why = False, out_lst
        else:
            notready = [k for k, v in (out_lst or {}).items() if not v.get("ready")]
            if notready:
                msst_ok = False
                msst_why = "这些模型没就绪：%s" % notready
            else:
                msst_why = "就绪 %d 个：%s" % (len(out_lst or {}), sorted(out_lst or {}))
    check("MSST 候选由它自己的加载器报告可用（不在就不能混进探测列表白跑）",
          msst_ok, msst_why)
    # ④a-负对照：把权重指到一个不存在的地方，这条判据**必须变红**。
    #     否则它就是摆设（门禁里从来只验 happy path 最容易出这种假尺子）。
    #     用环境变量改，不动真配置、跑完自然还原。
    old_env = os.environ.get("VS_MSST_KJ_CKPT")
    try:
        os.environ["VS_MSST_KJ_CKPT"] = os.path.join(HERE, "_没有这个权重.ckpt")
        ok2, out2 = _py_list()
        red2 = (not ok2) or any(not v.get("ready") for v in (out2 or {}).values())
    finally:
        if old_env is None:
            os.environ.pop("VS_MSST_KJ_CKPT", None)
        else:
            os.environ["VS_MSST_KJ_CKPT"] = old_env
    check("★ 负对照：权重不在时这条判据必须变红（不然它是假尺子）",
          bool(red2), "权重指坏之后竟然还是绿的，说明没真的去查")

    # ④b 它是**增益不是依赖**：解释器不在时，separate 必须优雅跳过而不是挂死。
    #    负对照：把 VS_MSST_PY 指到一个不存在的地方，_sep_run 得返回失败且
    #    报错里挑明"这是可选的增强"（少了它就该退化，不该报错我以为产品坏了）。
    old_py = studio.VS_MSST_PY
    try:
        studio.VS_MSST_PY = os.path.join(HERE, "_不存在的解释器.exe")
        _v, _i, why = studio._sep_run({"_logfile": os.devnull, "id": "t"},
                                      (sorted(msst) or ["x"])[0], "", "", "")
    finally:
        studio.VS_MSST_PY = old_py
    check("MSST 不在时优雅跳过（其余分离器照跑，不能把整条链拖死）",
          _v is None and "可选" in (why or ""), why)
    # ④c 「MSST 真的能分出东西」**不在这里验** —— 那是行为，只能真跑。
    #    ★ 这一条原来是 grep 源码里有没有 `"ready": os.path.isfile` 这个字符串，
    #      注释却写着"必须真的能分出东西"。两回事：把 ready 恒写成 True 它照样绿
    #      （假尺子）。已搬到 main() 的 D2 段用同一份真实混音真跑一遍，并配负对照。
    # ④b 新模型不会被历史平均分饿死 —— **用假统计验行为**，不是查源码字符串
    #     （第一版查的是 "prior" 这个变量名，把它改成 0.0 照样全绿 —— 假尺子）
    #     ★ 光看"够不够用"也没用：现在 4 个候选、4 个名额，怎么排都进得去，
    #       所以必须**模拟"候选比名额多"**才能验出先验在不在（实做：把名额减 1）。
    fake = {studio.UVR_MODEL_BEST: {"n": 50, "avg": 80.0},
            "HP5_only_main_vocal": {"n": 50, "avg": 70.0},
            "HP2_all_vocals": {"n": 50, "avg": 60.0}}
    cold = [m for m in cands if m not in fake]
    old_load, old_cap = studio._sep_stats_load, studio.SEP_MAX_CAND
    try:
        studio._sep_stats_load = lambda: fake
        studio.SEP_MAX_CAND = max(1, len(cands) - 1)     # 故意留一个进不来
        order = studio._sep_candidates()
        cap = studio.SEP_MAX_CAND
    finally:
        studio._sep_stats_load, studio.SEP_MAX_CAND = old_load, old_cap
    check("候选比名额多时，冷启动模型仍排得进探测名单（否则它永远攒不到数据）",
          bool(cold) and all(m in order[:cap] for m in cold),
          "顺序 %s，只探测前 %d 个，冷启动 %s" % (order, cap, cold))
    # ⑤ 主力模型必须有文件，否则整条链第一个环节就退到 VR 备胎
    check("主力模型权重存在", os.path.isfile(os.path.join(W, MODEL + ".ckpt"))
          or os.path.isfile(os.path.join(W, MODEL + ".pth")), MODEL)
    # ⑥ 被探测的模型数不能小于候选数，否则排在最后的永远轮不到
    check("SEP_MAX_CAND 不小于候选数（不然最后那几个等于没配）",
          int(getattr(studio, "SEP_MAX_CAND", 0)) >= len(cands),
          "SEP_MAX_CAND=%s 候选 %d 个"
          % (getattr(studio, "SEP_MAX_CAND", None), len(cands)))

    # ⑦ 探测早停：够好了就别再试（省时间），但绝不能只试 1 个就停
    #    ★ 判据读的是**行为**（调那个纯函数），不是查源码里有没有这个字符串 ——
    #      查字符串那种判据，改个变量名就绿了，等于没配。
    f = getattr(studio, "_probe_should_stop", None)
    check("早停判据是独立函数（门禁能直接测，不必真跑一遍模型）", callable(f))
    if callable(f):
        # ★ 判的是"第一名领先第二名多少"，不是"绝对分数够不够"。
        #   依据：193 次真实探测离线回放（gap=5 ⇒ 平均试 2.35 个、命中最好 100%、
        #   损失 0；而老的"绝对分≥85"在 590 个探测分里只占 9.3%，几乎从不触发）。
        gap = float(getattr(studio, "SEP_PROBE_GAP", 0) or 0)
        lead = [{"score": 80.0}, {"score": 80.0 - gap - 1.0}]     # 领先 > 阈值
        close = [{"score": 80.0}, {"score": 80.0 - gap + 1.0}]    # 领先 < 阈值
        check("★ 第一名领先够多 ⇒ 早停（回放：这样停 193 次都没选错）",
              f(lead, 1, 4) is True, "%s" % f(lead, 1, 4))
        check("★ 分已经很高 ⇒ 也停（简单的歌不必把 4 个都试一遍）",
              f([{"score": 90.0}, {"score": 89.5}], 1, 4) is True,
              "%s" % f([{"score": 90.0}, {"score": 89.5}], 1, 4))
        check("★ 前两名咬得紧、分也不高 ⇒ 继续试（第 3 个可能翻盘）",
              f(close, 1, 4) is False, "%s" % f(close, 1, 4))
        check("★ 只试了 1 个不许停（那一个偶然失手就没有对照，会直接选错）",
              f([{"score": 99.0}], 0, 4) is False)
        # 负对照：两条线都调到天上 ⇒ 永远不该停（否则说明判据根本没接上）
        old_gap, old_good = studio.SEP_PROBE_GAP, studio.SEP_PROBE_GOOD
        try:
            studio.SEP_PROBE_GAP, studio.SEP_PROBE_GOOD = 999.0, 999.0
            check("  负对照：两条线都调到 999 后，领先再多也不停（判据真的接上了）",
                  f([{"score": 99.0}, {"score": 1.0}], 1, 4) is False)
        finally:
            studio.SEP_PROBE_GAP, studio.SEP_PROBE_GOOD = old_gap, old_good
        # 负对照 2：两条线都降到 0 ⇒ 该停就得停（上一条不是"永远不停"）
        try:
            studio.SEP_PROBE_GAP, studio.SEP_PROBE_GOOD = 0.0, 0.0
            check("  负对照：两条线都降到 0 后，试 2 个就停（不是永远不停）",
                  f([{"score": 50.0}, {"score": 49.0}], 1, 4) is True)
            check("  负对照：两条线都降到 0 后，试 1 个仍不停（MIN 这道闸真的在拦）",
                  f([{"score": 50.0}], 0, 4) is False)
        finally:
            studio.SEP_PROBE_GAP, studio.SEP_PROBE_GOOD = old_gap, old_good
    check("早停三个常量都配了（MIN≥2，GAP 4~6，GOOD 80~90）",
          int(getattr(studio, "SEP_PROBE_MIN", 0)) >= 2
          and 4.0 <= float(getattr(studio, "SEP_PROBE_GAP", 0)) <= 6.0
          and 80.0 <= float(getattr(studio, "SEP_PROBE_GOOD", 0)) <= 90.0,
          "MIN=%s GAP=%s GOOD=%s" % (getattr(studio, "SEP_PROBE_MIN", None),
                                     getattr(studio, "SEP_PROBE_GAP", None),
                                     getattr(studio, "SEP_PROBE_GOOD", None)))
    check("★ 绝对分那条线是「或」不是「且」（单独用 ≥85 只占 9.3%，几乎不触发）",
          hasattr(studio, "SEP_PROBE_GOOD") and hasattr(studio, "SEP_PROBE_GAP")
          and callable(f)
          # 咬得紧但分数高 ⇒ 仍该停（证明两条是"或"）
          and f([{"score": 90.0}, {"score": 89.9}], 1, 4) is True,
          "GAP=%s GOOD=%s" % (hasattr(studio, "SEP_PROBE_GAP"),
                              hasattr(studio, "SEP_PROBE_GOOD")))

    # ⑧ 时长读不到时，探测片段**不能退化成整首**
    #    实测：真跑时传了个 mp3，读不到时长 ⇒ 拿整首去探测 ⇒ 一个模型 128 秒
    #    （30 秒片段只要 16 秒），4 个候选就是 8 分钟。
    g = getattr(studio, "_probe_clip", None)
    check("探测取段是独立函数（门禁能直接测）", callable(g))
    if callable(g):
        p_long, d_long = g(200.0)
        _p_zero, d_zero = g(0.0)
        _p_short, d_short = g(20.0)
        check("★ 长歌只取一小段（不是整首）",
              d_long is not None and 0 < d_long <= 60 and bool(p_long),
              "parts=%s dur=%s" % (p_long, d_long))
        # ★ 2026-10-27 加：探针必须覆盖到歌的**后 1/4**。
        #   原来 `(total-30)/2` 永远落在歌曲正中间，而漏伴奏最狠的恰恰是后段 ——
        #   "越到后面越差、突然跳八度"就出在那儿，它**从来没被评估过**。
        #   实测（198 秒的歌）：后段 150~180s 五个候选漏电 0.1919~0.2663，
        #   被选中的那个是漏得最狠的 0.2663，它靠 quiet 高才赢的。
        deep = max([(s + d) / 200.0 for s, d in p_long], default=0.0)
        check("★ 探针必须覆盖到歌的后 1/4（漏伴奏的重灾区）",
              deep >= 0.75, "最深只到全曲 %.0f%%" % (deep * 100))
        tot = sum(d for _, d in p_long)
        check("  探针总量仍是 SEP_PROBE_SEC（多取一段不许变慢）",
              abs(tot - getattr(studio, "SEP_PROBE_SEC", 30.0)) < 1e-6,
              "合计 %.1f 秒" % tot)
        check("★ 时长读不到时仍只取一小段（绝不拿整首去试，见上一条实测）",
              d_zero is not None and 0 < d_zero <= 60,
              "dur=%s" % (d_zero,))
        check("  负对照：歌本身就短时才用整首（上一条不是「永远只切一段」）",
              d_short is None, "dur=%s" % (d_short,))

    # ⑧b 多段要能拼成一条（分离器的输入目录里只认一个 source.wav）
    pf = getattr(studio, "_probe_filter", None)
    check("多段拼接是独立函数（门禁能直接测）", callable(pf))
    if callable(pf):
        s = pf([(10.0, 5.0), (100.0, 5.0)])
        check("两段的拼接滤镜：段数、起止、concat 都得对上",
              s.count("atrim=") == 2 and "concat=n=2" in s
              and "start=10.000:end=15.000" in s
              and "start=100.000:end=105.000" in s, s[:90])

    # ⑧c 漏电否决权：漏伴奏 → RVC 把残留的高音乐器当人声唱 → 听感"跳八度"
    lkf = getattr(studio, "_leak_of", None)
    rk = getattr(studio, "_probe_rank", None)
    check("漏电取值是独立函数（判据与日志共用一个口径）", callable(lkf))
    check("探针排序是独立函数（门禁能直接测）", callable(rk))
    if callable(rk) and callable(lkf):
        lim = getattr(studio, "SEP_PROBE_LEAK_MAX", 0.20)
        rows = [{"model": "A", "score": 80.0, "leak": 0.10, "leak_worst": 0.12},
                {"model": "B", "score": 90.0, "leak": 0.20, "leak_worst": lim + 0.05}]
        out = rk(list(rows))
        check("★ 漏电超标的模型，总分再高也排到后面",
              out[0]["model"] == "A" and out[1]["model"] == "B",
              "第一名=%s（B 总分 %.0f 更高）" % (out[0]["model"], rows[1]["score"]))
        # ★ 负对照：证明上面那条不是白拿的 —— 纯按总分确实会选到漏电超标的那个
        by_score = sorted(rows, key=lambda r: -(r.get("score") or 0))
        check("  负对照：同样这份数据，纯按总分会选到漏电超标的那个",
              by_score[0]["model"] == "B" and out[0]["model"] != by_score[0]["model"],
              "纯按总分选 %s / 我们选 %s" % (by_score[0]["model"], out[0]["model"]))
        all_over = [{"model": "A", "score": 80.0, "leak": 0.50, "leak_worst": 0.60},
                    {"model": "B", "score": 90.0, "leak": 0.50, "leak_worst": 0.55}]
        check("  全部超标时退化回按总分（不会没得选）",
              rk(list(all_over))[0]["model"] == "B")
        # ★ 口径一致：判据看 leak_worst，日志就必须是同一个数。
        #   第一版这里写成了中位数，日志变成「漏电 0.167 > 0.20」—— 自相矛盾，
        #   用户看到只会更糊涂。这条就是盯它的。
        check("★ 判据与日志用同一个漏电值（不许一个看 p90、一个写中位数）",
              abs(lkf(rows[1]) - (lim + 0.05)) < 1e-9,
              "leak_of=%s（若等于 leak 0.20 就是口径又分家了）" % lkf(rows[1]))

    pn = getattr(studio, "_pick_chosen_note", None)
    check("选中理由是独立函数（门禁能直接测）", callable(pn))
    if callable(pn) and callable(rk):
        lim = getattr(studio, "SEP_PROBE_LEAK_MAX", 0.20)
        out = rk([{"model": "A", "score": 80.0, "leak": 0.10, "leak_worst": 0.12},
                  {"model": "B", "score": 90.0, "leak": 0.20, "leak_worst": lim + 0.05}])
        note = pn(out)
        check("★ 没选总分更高的那个时，日志要写明是被漏电拦下的",
              "B" in note and "漏电" in note, note or "(空)")
        check("  负对照：都没超漏电时不许乱写理由",
              pn(rk([{"model": "A", "score": 80.0, "leak": 0.10, "leak_worst": 0.10},
                     {"model": "B", "score": 90.0, "leak": 0.15, "leak_worst": 0.15}])) == "")

    # ⑧d「最漏的那一段」必须比中位数敏感 —— 否则局部漏电照样被瞒过去
    #
    # ★ 造数据的形状要跟真病一模一样（2026-10-27 的实测）：
    #   一整首里**只有一两段**漏得厉害，其余都很干净。中位数问"典型的那一帧"，
    #   于是把那两段稀释没了（实测 0.1674，而最漏的那段 0.2078）。
    #
    # ★ 门槛不许拍（踩过一次：当初写 `worst > med + 0.3`，而 50/50 双峰数据上
    #   实测 worst-med = 0.2939 —— 差 0.006 变红，判据自己在刀尖上，绿了也不作数）。
    #   所以下面两条都**锚在同一份数据里漏的那两段的实测值 c_leak 上**，
    #   不跟任何绝对常数比：c_leak 是"漏的那两段到底有多漏"的真值。
    cw = getattr(sep_eval, "_corr_worst", None)
    check("最漏段计算是独立函数（门禁能直接测）", callable(cw))
    if callable(cw):
        rng = np.random.default_rng(7)
        NFR, NBN, WIN = 320, 64, 32                        # 10 个窗，每窗 32 帧
        A = np.abs(rng.standard_normal((NFR, NBN))) + 0.5
        B = np.abs(rng.standard_normal((NFR, NBN))) + 0.5  # 整体：与人声轨无关
        LEAK_WIN = (6, 7)                                  # 只让第 7、8 两个窗漏电
        for w in LEAK_WIN:
            s = w * WIN
            B[s:s + WIN] = (0.5 * A[s:s + WIN]
                            + 0.5 * (np.abs(rng.standard_normal((WIN, NBN))) + 0.5))
        lo = LEAK_WIN[0] * WIN
        hi = (LEAK_WIN[-1] + 1) * WIN
        c_leak = sep_eval._corr_median(A[lo:hi], B[lo:hi])  # 真值：漏的那两段有多漏
        med = sep_eval._corr_median(A, B)
        worst = cw(A, B, win=WIN)
        check("★ 最漏段必须真的落在漏的那一段上（不许被干净的窗带低）",
              worst >= c_leak - 0.05,
              "最漏段 %.3f / 漏段真值 %.3f" % (worst, c_leak))
        check("★ 中位数必须明显看不到它（这正是要补 p90 的原因）",
              med <= worst - 0.25,
              "中位数 %.3f / 最漏段 %.3f" % (med, worst))

        # 负对照：换成"不分窗、直接整体中位数"的假实现 ⇒ 上面两条必须至少一条变红。
        # （没有这条，⑧d 只是"跟自己比"，改坏了也可能照样绿）
        def _corr_worst_flat(a, b, win=WIN):               # noqa: ARG001
            return sep_eval._corr_median(a, b)

        w_flat = _corr_worst_flat(A, B)
        check("  负对照：不分窗的假实现必须被这两条抓住",
              not (w_flat >= c_leak - 0.05),
              "假实现最漏段 %.3f / 漏段真值 %.3f（抓不住就是假尺子）" % (w_flat, c_leak))

    # ⑨ 早停不许把"历史最优"跳过去
    #    背景：候选排序给没攒够数据的新模型发乐观先验（先验值 = max 已知 avg），
    #    新模型因此可能和历史最优并列、再靠配置顺序挤到前面。早停只试前两个的话，
    #    最有把握的那个（两百多次实测证明过自己）可能压根没被试 —— 省时间不该
    #    省在这儿。所以早停额外保它一次。
    champ_f = getattr(studio, "_sep_champion", None)
    check("历史最优是独立函数（门禁能直接测）", callable(champ_f))
    if callable(champ_f) and callable(f):
        cands4 = list(getattr(studio, "UVR_CANDIDATES", []))[:4] or ["A", "B", "C"]
        old_load = studio._sep_stats_load
        try:
            # 造假统计：B 试过很多次、平均分最高 ⇒ 它是冠军；
            #           D 只试过 2 次却分很高 ⇒ 不许算冠军（数据不够，那是运气）
            studio._sep_stats_load = lambda: {
                "A": {"n": 10, "avg": 60.0},
                "B": {"n": 200, "avg": 72.0},
                "C": {"n": 50, "avg": 55.0},
                "D": {"n": 2, "avg": 99.0},
            }
            got = champ_f(["A", "B", "C", "D"])
        finally:
            studio._sep_stats_load = old_load
        check("★ 历史最优 = 试够次数里平均分最高的那个",
              got == "B", "拿到 %s" % (got,))
        check("★ 只试过 2 次的高分模型不算冠军（那是运气不是实力）",
              got != "D", "拿到 %s" % (got,))

        # ★ 下面三条的样例必须**先满足"领先 ≥ GAP"**，否则不管 champ 怎么样
        #   都会被 GAP 那条拦下 ⇒ 判据测不到 champ，变成"恒绿的假尺子"。
        #   （这一版第一次就踩了：样例分差只有 0.5，三条里有两条假绿。）
        gap = float(getattr(studio, "SEP_PROBE_GAP", 5.0) or 5.0)
        no_champ = [{"model": "A", "score": 90.0}, {"model": "C", "score": 90.0 - gap - 2}]
        has_champ = [{"model": "A", "score": 90.0}, {"model": "B", "score": 90.0 - gap - 2}]
        check("★ 历史最优还没试 ⇒ 不许早停（省时间不能省在它身上）",
              f(no_champ, 1, 4, "B") is False, "%s" % f(no_champ, 1, 4, "B"))
        check("  历史最优试过了 ⇒ 该停还是停（上一条不是「永远不停」）",
              f(has_champ, 1, 4, "B") is True, "%s" % f(has_champ, 1, 4, "B"))
        # 负对照：champ 为 None（冷启动，谁都没攒够数据）时不拦 ——
        #         如果实现写成"champ 没传就一律不停"，早停在冷启动时就废了。
        check("  负对照：没有历史最优时照常早停（冷启动不该被这条卡住）",
              f(no_champ, 1, 4, None) is True, "%s" % f(no_champ, 1, 4, None))


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
                check("真分离出来的比「漏 60% 伴奏」分高",
                      sc["score"] > sc_rbad["score"],
                      "真分离=%.1f 漏60%%=%.1f" % (sc["score"], sc_rbad["score"]))
                check("真分离的真值 SDR 是正的（分离有效，不是把人声丢了）",
                      sd > 0.0, "%.2f dB" % sd)

            # ---------------- D2 MSST 那条路：真跑一遍 ----------------
            # ★ 为什么非得真跑：上面 ④c 原来只 grep 源码里有没有
            #   `"ready": os.path.isfile` 这个字符串，注释却写着"必须真的能分出
            #   东西" —— 把 ready 恒写成 True 它照样绿（假尺子）。这条路是
            #   2026-10-26 第一次进生产，还没有任何判据证明它**到底出不出音频**，
            #   而"能走到"和"能消费"是两件事。所以用同一份真实混音真跑一遍。
            if not _msst_py():
                print("    （跳过 MSST 真跑：解释器不在 —— 它是增益不是依赖）")
            else:
                sys.path.insert(0, HERE)
                import studio  # noqa: E402  （延迟导入：这关其余部分不需要它）
                t1 = time.time()
                mv, mi, mwhy = studio._sep_run(
                    {"_logfile": os.devnull, "id": "t"}, studio.MSST_MODELS[0],
                    pin, os.path.join(tmp, "m_out_v"),
                    os.path.join(tmp, "m_out_i"))
                if not (mv and mi):
                    check("MSST 那条路真跑出人声/伴奏两轨", False, str(mwhy)[-300:])
                else:
                    msc = sep_eval.score(p_rm, mv, mi)
                    msd = sep_eval.sdr(p_rv, mv)
                    print("    MSST 用时 %.0fs，score=%.1f leak=%.3f floor=%.1f，"
                          "真值 SDR=%.2f dB" % (time.time() - t1, msc["score"],
                                                msc["leak"], msc["floor_db"], msd))
                    check("MSST 那条路真跑出人声/伴奏两轨", True,
                          "%.0fs 两轨都在" % (time.time() - t1))
                    check("MSST 分离比「没分离」分高（模型真的在干活）",
                          msc["score"] > sc_rnone["score"],
                          "MSST=%.1f 没分离=%.1f" % (msc["score"], sc_rnone["score"]))
                    check("MSST 分离比「漏 60% 伴奏」分高（不能靠漏一端拿分）",
                          msc["score"] > sc_rbad["score"],
                          "MSST=%.1f 漏60%%=%.1f" % (msc["score"], sc_rbad["score"]))
                    check("MSST 分离的真值 SDR 是正的（没把人声丢了）", msd > 0.0,
                          "%.2f dB" % msd)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------- E 选模 / 二次分离的接线（纯静态，不占显卡）----------------
    e_wiring()

    print("-" * 62)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 失败项：" + "、".join(BAD))
    else:
        print("结论：分离质量门禁全绿")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
