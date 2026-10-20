# -*- coding: utf-8 -*-
"""唱歌链路验收：分离是否干净、成品里有没有原唱、暂停能不能真的停住。

核心判据（第 3 段）为什么必须配负对照：
  "成品里没有原唱"这件事，光看新链路的相关性低不算数 —— 也可能是这段音频
  本来就相关性低（比如人声被判据取错了位置）。所以这里**同时**用旧的做法
  （拿原曲当伴奏）混一遍，要求它的相关性必须**高**。两个数一起看才作数。
"""
import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402

SRC = os.path.join(HERE, "data", "uploads", "嘟比Dubi - 神耀（星际争霸同人歌）.mp3")
WORK = os.path.join(HERE, "data", "_vtest")
_MADE_ROLE = [None]      # 我们自己建的探针角色，收尾要删掉


def pick_role():
    """挑一个"能唱歌"的角色；库里没有就自己造一个绑到磁盘上现存的真实权重。

    ★ 判据要自足：以前写死 `RID = "r8c323d43"`，用户把那个角色删了之后本脚本
      直接 `Fail: 角色不存在` 崩在第 2 段 —— 崩在一个**数据前提**上，而不是
      "唱歌链路坏了"。但磁盘上那份训好的权重 `.pth` + 索引库其实还在，
      所以这里自己建一个探针角色绑上去，链路该怎么跑还怎么跑，跑完删掉角色
      （权重是别人的，不动）。
    """
    wd = studio._rvc_weights_dir()

    def usable(r):
        m = (r.get("rvc") or {}).get("model")
        if not m or m.startswith("_"):
            return False
        p = os.path.join(wd, m)
        # ★ 必须是**真权重**（RVC 的 G 通常 50~200MB）。门禁自己种过的探针
        #   权重只有几百 KB，torch.load 会直接报 legacy .tar 格式错误 ——
        #   不设这个门槛就会挑中上一轮验收留下的假权重，然后红在 RVC 报错上。
        return os.path.isfile(p) and os.path.getsize(p) >= 10 * 1024 * 1024

    db = studio.load_roles()
    roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
    roles = roles if isinstance(roles, list) else list(roles.values())
    for r in roles:
        if usable(r):
            return r["id"], False
    # 库里没有：找一份磁盘上真实存在的权重（带轮数尾巴的可能是中间存档，用裸名的）
    cands = [f for f in os.listdir(wd)
             if f.endswith(".pth") and not f.startswith("_")
             and os.path.isfile(os.path.join(wd, f))]
    if not cands:
        return None, False
    cands.sort(key=lambda f: -os.path.getsize(os.path.join(wd, f)))
    model = cands[0]
    role = studio.create_role(name="_singprobe(探针)")
    rid = role["id"]
    studio.mutate_role(rid, lambda r: r.__setitem__(
        "rvc", {"model": model, "epochs": 150, "at": studio._now(),
                "weight_epoch": 150, "pretrained": True,
                "index": bool(studio._find_rvc_index(model))}))
    _MADE_ROLE[0] = rid
    print("  （库里没有能唱歌的角色，已建探针角色 %s，绑到磁盘上现存的权重 %s）"
          % (rid, model))
    return rid, True


RID, RID_IS_PROBE = pick_role()

OK, BAD = [], []
CLEANUP_JOBS = []      # 本次验收建的任务目录，收尾要删掉（否则任务列表被测试记录淹没）


def mkjob(*a, **kw):
    j = studio.new_job(*a, **kw)
    CLEANUP_JOBS.append(j.get("dir"))
    j["state"] = "running"
    return j


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name + (("  | " + str(detail)) if detail else ""),
          flush=True)


def load16k(path, ss=None, t=None):
    out = os.path.join(WORK, "tmp_%d.wav" % abs(hash((path, ss, t))))
    cmd = [studio.FFMPEG, "-y", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", "%.3f" % ss]
    if t is not None:
        cmd += ["-t", "%.3f" % t]
    cmd += ["-i", path, "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", out]
    subprocess.run(cmd, capture_output=True)
    if not os.path.exists(out):
        return np.zeros(1)
    w = wave.open(out, "rb")
    a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64) / 32768.0
    w.close()
    return a


def corr(x, y):
    n = min(len(x), len(y))
    if n < 1000:
        return 0.0
    x, y = x[:n] - x[:n].mean(), y[:n] - y[:n].mean()
    d = np.sqrt((x ** 2).sum()) * np.sqrt((y ** 2).sum())
    return float((x * y).sum() / d) if d > 1e-12 else 0.0


def rms(x):
    return float(np.sqrt((np.asarray(x) ** 2).mean())) if len(x) else 0.0


def band_ratio(x, sr, lo, hi):
    win = 8192
    P = []
    for i in range(0, max(0, len(x) - win), win):
        P.append(np.abs(np.fft.rfft(x[i:i + win] * np.hanning(win))) ** 2)
    if not P:
        return 0.0
    P = np.mean(np.array(P), axis=0)
    f = np.fft.rfftfreq(win, 1.0 / sr)
    return float(P[(f >= lo) & (f < hi)].sum()) / (float(P.sum()) + 1e-12)


def probe_pause():
    """第 4 段：暂停必须真的停住子进程（判据是输出停了又回来）。"""
    ticker = os.path.join(WORK, "ticker.py")
    with open(ticker, "w", encoding="utf-8") as f:
        f.write("import sys, time\nn=0\nwhile True:\n    n+=1\n"
                "    print('TICK %d' % n)\n    sys.stdout.flush()\n    time.sleep(0.1)\n")
    job = mkjob(None, "probe", "暂停探针")
    lines = []
    lock = threading.Lock()

    def on_line(ln):
        with lock:
            lines.append(ln)

    t = threading.Thread(target=lambda: studio.run(
        job, '"%s" -u "%s"' % (studio.PY, ticker), timeout=120, on_line=on_line), daemon=True)
    t.start()
    time.sleep(1.6)
    with lock:
        n1 = len(lines)
    ok1, msg1 = studio.pause_job(job["id"])
    time.sleep(2.5)
    with lock:
        n2 = len(lines)
    ok2, msg2 = studio.resume_job(job["id"])
    time.sleep(1.6)
    with lock:
        n3 = len(lines)
    frozen = (n2 - n1) <= 1
    resumed = (n3 - n2) >= 6
    # 收尾：把 ticker 子进程连树杀掉。不杀的话它还在往日志写输出，而任务目录
    # 在 finally 里会被删掉 —— 那个读输出线程就会抛 FileNotFoundError
    # （实测就是这么炸的：判据全绿，日志末尾却挂着一大段红栈，很误导人）。
    c = studio.CTRL.get(job["id"]) or {}
    for pid in list(c.get("pids") or []):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True)
    time.sleep(0.8)
    try:
        studio.CTRL.pop(job["id"], None)
    except Exception:
        pass
    return ok1, msg1, ok2, msg2, n1, n2, n3, frozen, resumed


def main():
    print("=" * 70)
    print("唱歌链路验收")
    print("=" * 70)
    os.makedirs(WORK, exist_ok=True)

    if not RID:
        print("! 无法开始：库里既没有能唱歌的角色，磁盘上也没有任何 RVC 权重")
        print("  第 2/3/5/6/7 段需要一个真能推理的歌声模型，"
              "先训一个角色（或用界面导入一段素材）再跑本脚本。")
        return 1

    if not os.path.isfile(SRC):
        print("! 跳过：找不到测试用的原曲（%s）" % SRC)
        print("  这条验收需要一首**有人声**的歌，项目里原本有一首，"
              "被清掉就没法验证「成品里没有原唱」。")
        sys.exit(0)

    # ---------------- 1. 分离质量 ----------------
    print("\n=== 1. 人声分离：人声轨里不许有伴奏 ===")
    job = mkjob(None, "vtest", "验收：分离")
    t0 = time.time()
    vocal, inst = studio.separate_song(job, SRC)
    print("  分离用时 %.1fs" % (time.time() - t0))
    V = load16k(vocal)
    I = load16k(inst)
    A = load16k(SRC)
    lf_v, lf_a = band_ratio(V, 16000, 0, 150), band_ratio(A, 16000, 0, 150)
    check("人声轨的低频（0-150Hz，伴奏鼓/贝斯的驻留区）显著低于原曲",
          lf_v < 0.10 < lf_a, "人声轨 %.1f%% vs 原曲 %.1f%%" % (lf_v * 100, lf_a * 100))
    check("人声轨与伴奏轨几乎不相关（分离干净）",
          abs(corr(V, I)) < 0.30, "corr=%+.3f" % corr(V, I))
    check("伴奏轨保留了大量低频（真的是伴奏，不是空轨）",
          band_ratio(I, 16000, 0, 150) > 0.30,
          "%.1f%%" % (band_ratio(I, 16000, 0, 150) * 100))

    # ---------------- 2. 唱歌链路（真跑一段） ----------------
    print("\n=== 2. 真跑一段：分离好的干声 -> 变声 -> 用伴奏混音 ===")
    total = studio.wav_seconds(SRC)
    SECS = 25.0
    START = max(0.0, (total - SECS) * 0.35)
    cv = os.path.join(WORK, "clip_voice.wav")
    ci = os.path.join(WORK, "clip_inst.wav")
    studio.clip_audio(job, vocal, cv, START, SECS)
    studio.clip_audio(job, inst, ci, START, SECS)
    check("裁出了人声/伴奏各一段用于验证", os.path.isfile(cv) and os.path.isfile(ci),
          "第 %.0f~%.0f 秒" % (START, START + SECS))

    tracks = studio.sing_role(
        job, RID, cv, pitch=0, index_rate=0.75, prefix="vtest", save=False,
        pre_sep=(cv, ci))
    labels = [t["label"] for t in tracks]
    mix = next((t["wav"] for t in tracks if "成品" in t["label"]), None)
    voice = next((t["wav"] for t in tracks if "人声" in t["label"]), None)
    check("产出了成品轨", bool(mix and os.path.isfile(mix)), labels)
    check("产出了人声干轨", bool(voice and os.path.isfile(voice)))
    if not mix:
        print("\n成品没出来，后面的判据没法做 —— 直接收尾。")
        return summary()
    d_mix = studio.wav_seconds(mix)
    check("成品时长与原曲片段一致（人声短了补零，不许整首短一截）",
          abs(d_mix - SECS) < 1.5, "%.1fs vs %.1fs" % (d_mix, SECS))

    # ---------------- 3. 核心：成品里有没有原唱（含负对照） ----------------
    print("\n=== 3. 成品里有没有原唱（这一条是「没法听」的根治判据）===")
    Vc = load16k(vocal, START, SECS)     # 原唱人声
    Ic = load16k(inst, START, SECS)      # 伴奏
    Ac = load16k(SRC, START, SECS)       # 原曲（原唱 + 伴奏）
    Mn = load16k(mix, None, SECS)        # 新成品
    r_new = corr(Mn - Ic, Vc)
    check("新链路：成品里的人声成分与原唱无关（< 0.25）",
          abs(r_new) < 0.25, "corr(成品−伴奏, 原唱)=%+.3f" % r_new)
    check("新链路：成品里的人声不是空的（残差有能量）",
          rms(Mn - Ic) > 0.02, "残差 RMS %.4f" % rms(Mn - Ic))

    # 负对照：按**旧做法**混一遍（拿原曲当伴奏）—— 判据必须能红。
    # ★ 必须用**裁好的原曲片段**，不能用整曲：整曲长度和 25 秒的人声对不上，
    #   混出来的东西时间轴错位，比出来的相关系数毫无意义。
    ca = os.path.join(WORK, "clip_src.wav")
    studio.clip_audio(job, SRC, ca, START, SECS)
    old_like = os.path.join(WORK, "old_like.wav")
    studio.run(job, '"%s" -s "%s" "%s" "%s" "%s" 0.7 0.9'
               % (studio.PY, os.path.join(studio.STAGES, "mix_cli.py"),
                  ca, voice, old_like), timeout=600)
    if os.path.isfile(old_like):
        Mo = load16k(old_like, None, SECS)
        r_old = corr(Mo - Ic, Vc)
        check("负对照：拿原曲当伴奏混（旧做法）时，判据必须**红**",
              abs(r_old) > 0.30, "corr=%+.3f（越高越说明里面有原唱）" % r_old)
        check("新链路比旧做法明显更干净",
              abs(r_new) < abs(r_old) - 0.15, "新 %+.3f vs 旧 %+.3f" % (r_new, r_old))
    else:
        check("负对照跑得起来", False, "旧做法混音没产出文件")

    # ---------------- 4. 暂停 / 继续 ----------------
    print("\n=== 4. 暂停必须真的停住子进程 ===")
    ok1, msg1, ok2, msg2, n1, n2, n3, frozen, resumed = probe_pause()
    check("暂停接口返回成功", ok1, msg1)
    check("暂停期间子进程输出**停止增长**（真挂起，不是界面不动）",
          frozen, "暂停前 %d 行 -> 暂停 2.5 秒后 %d 行" % (n1, n2))
    check("继续接口返回成功", ok2, msg2)
    check("继续后子进程输出恢复增长",
          resumed, "%d -> %d（增长 %d）" % (n2, n3, n3 - n2))

    # ---------------- 5. 自动音高 ----------------
    print("\n=== 5. 自动音高（用 RMVPE 量，不是查表）===")
    ref = studio._role_ref_dir(RID)
    if ref is None:
        # 探针角色没有 rvc_dataset / slices（它只是绑了一份现成权重，没有素材）。
        # 音高估计要的是"这个声音的音域"，拿刚分离出来的干声顶上是合法的 ——
        # 不然这一段就红在"角色没素材"这个数据前提上，而不是"音高量得准不准"。
        sd = os.path.join(studio.role_dir(RID), "slices")
        os.makedirs(sd, exist_ok=True)
        shutil.copy(vocal, os.path.join(sd, "probe_ref.wav"))
        ref = studio._role_ref_dir(RID)
        print("    （探针角色没有音域参考素材，已用分离出的干声顶上：%s）" % ref)
    pj = os.path.join(WORK, "pitch.json")
    rc2, out2 = studio.run(job, '"%s" -s "%s" "%s" "%s" "%s" 8'
                           % (studio.RVC_PY, os.path.join(studio.STAGES, "pitch_cli.py"),
                              vocal, ref, pj), cwd=studio.RVC_ROOT, timeout=900)
    got = None
    if rc2 == 0 and os.path.isfile(pj):
        try:
            got = json.load(open(pj, encoding="utf-8"))
        except Exception:
            got = None
    check("音高估计脚本跑通", got is not None, "退出码 %d" % rc2)
    if got:
        check("量出了两边的基频中位数（原唱 > 0，角色 > 0）",
              got.get("src_med", 0) > 0 and got.get("ref_med", 0) > 0,
              "原唱 %.1f Hz / 角色 %.1f Hz" % (got.get("src_med", 0), got.get("ref_med", 0)))
        check("给出的半音数在合理范围（-24 ~ +24）",
              -24 <= got.get("semitones", 999) <= 24, got.get("semitones"))
    else:
        check("有一个能读的半音结果", False, "拿不到 pitch.json")

    # ---------------- 5b. 成品带歌词 ----------------
    print("\n=== 5b. 成品要带歌词（.lrc + 存进成品记录）===")
    # 改过词 ⇒ 成品带的是**新词**（用户听到的就是新词，存原词等于存错）
    check("改过词就用新词", studio._lyric_text(("原词一", "新词二")) == "新词二",
          studio._lyric_text(("原词一", "新词二")))
    check("没改词就用原词", studio._lyric_text(("原词一", "")) == "原词一",
          studio._lyric_text(("原词一", "")))
    check("★ 没给歌词就是空（不拿歌名糊弄）", studio._lyric_text(None) == "")
    # 时间轴：句数对得上才给，对不上宁可不给
    _lw = os.path.join(WORK, "t_one.wav")
    segs = [{"start": 1.5, "end": 3.0, "text": "a"}, {"start": 4.0, "end": 6.0, "text": "b"}]
    p1 = studio._write_lrc(_lw, "一句\n两句", segs)
    txt1 = open(p1, encoding="utf-8-sig").read() if p1 else ""
    check("句数对得上 ⇒ .lrc 带时间轴",
          p1 and txt1.count("[00:0") == 2, txt1.replace("\n", " | ")[:80])
    p2 = studio._write_lrc(_lw, "一句\n两句\n三句", segs)
    txt2 = open(p2, encoding="utf-8-sig").read() if p2 else ""
    check("★ 句数对不上 ⇒ 不给时间轴（绝不按行数均分时间）",
          "[00:" not in txt2 and "三句" in txt2, txt2.replace("\n", " | ")[:80])
    check("中文不乱码（写了 BOM）", open(p1, "rb").read(3) == b"\xef\xbb\xbf",
          repr(open(p1, "rb").read(3)))
    check("★ 没歌词就不生成文件（不留一个空的 .lrc 糊弄人）",
          studio._write_lrc(_lw, "", segs) == "")

    # ---------------- 6. 缓存 ----------------
    print("\n=== 6. 分离缓存（同一首歌第二次不该重跑）===")
    t1 = time.time()
    v2, i2 = studio.separate_song(job, SRC)
    dt = time.time() - t1
    check("第二次调用直接命中缓存（秒回，且是同两个文件）",
          dt < 3.0 and v2 == vocal and i2 == inst, "用时 %.2fs" % dt)

    # ---------------- 7. 小样连跑两次 ----------------
    print("\n=== 7. 「先听 30 秒样片」连点两次都要能出声 ===")
    # ★ 这是本轮真踩到的坑：小样用**固定文件名** preview_latest（界面上永远只留
    #   一份），而 RVC 的 cli 在输出文件已存在时**直接退出码 1**
    #   （"Output already exists; use --overwrite"），根本不看输入变没变。
    #   于是第一次试听正常、第二次必然「歌声转换失败（退出码 1）」——
    #   而用户恰恰是在刚听完一遍、想换个音高再听的时候去点第二次。
    #   ⇒ 这条判据必须**连跑两次**，只跑一次是永远抓不到的。
    outs_before = json.loads(json.dumps(studio.get_role(RID).get("outputs") or []))

    def _one_preview(tag):
        j = mkjob(None, "sing", "验收：试听小样 %s" % tag)
        try:
            return studio._sing_preview(j, RID, SRC, 12, 0, 0.75, None, None, None), None
        except Exception as e:
            return None, "%s: %s" % (type(e).__name__, str(e)[:220])

    trA, errA = _one_preview("A")
    check("第 1 次试听小样能出声", trA is not None, errA or "")
    trB, errB = _one_preview("B")
    check("第 2 次试听小样也能出声（同名输出不许把它顶掉）",
          trB is not None, errB or "")

    pv_dir = os.path.join(studio.role_dir(RID), "final")
    pv_mix = os.path.join(pv_dir, "mix_preview_latest.wav")
    pv_voice = os.path.join(pv_dir, "voice_preview_latest.wav")
    check("小样落在约定的固定名上（界面按这个名字取，不能每次换名）",
          os.path.isfile(pv_mix) and os.path.isfile(pv_voice),
          "mix=%.2fMB voice=%.2fMB" % (
              os.path.getsize(pv_mix) / 1e6 if os.path.isfile(pv_mix) else -1,
              os.path.getsize(pv_voice) / 1e6 if os.path.isfile(pv_voice) else -1))

    # 负对照：把 --overwrite 去掉、输出仍指向那个**已存在**的文件 —— 必须真的红。
    # 不红就说明"同名会被顶掉"这个坑根本不存在，上面两条判据是在量空气。
    rc_neg, out_neg = studio.run(
        mkjob(None, "probe", "验收：负对照（去掉 overwrite）"),
        '"%s" -m infer.cli --model "%s" --input "%s" --output "%s" '
        '--f0-method rmvpe --pitch 0 --index-rate 0'
        % (studio.RVC_PY, studio.get_role(RID)["rvc"]["model"], cv, pv_voice),
        cwd=studio.RVC_ROOT, timeout=300)
    tail_neg = [ln for ln in (out_neg or "").splitlines() if ln.strip()]
    check("负对照：不带 --overwrite 时 RVC 会拒绝写同名输出（必须红）",
          rc_neg != 0 and "already exists" in (out_neg or "").lower(),
          "退出码 %d / %s" % (rc_neg, tail_neg[-1][:90] if tail_neg else ""))

    # ------------------------------------------------------------------
    print("\n=== 8. RVC 底模：在位、能加载、尺子会红 ===")
    gp, dp = studio.rvc_pretrained_here("v1", "40k")
    check("v1/40k 底模在本机（没有它就训不出会跟调的模型）",
          bool(gp and dp),
          "%s / %s" % (os.path.basename(gp) if gp else "缺",
                       os.path.basename(dp) if dp else "缺"))
    # 负对照 1：没下过的版本必须报「没有」—— 否则上面这条是恒真的。
    g2, d2 = studio.rvc_pretrained_here("v2", "40k")
    check("负对照：没下过的 v2 底模必须报「没有」（函数不许恒真）",
          not g2 and not d2)
    if gp and dp:
        # studio.run 里每一步都要 log(job)，job 不能是 None（否则 AttributeError）
        ok8, msg8 = studio.check_rvc_pretrained(
            mkjob(None, "probe", "验收：底模体检"), "v1", "40k")
        check("底模真能加载进要训的架构（0 缺键 0 多键）", ok8, msg8[:110])
        # 负对照 2：拿判别器底模冒充生成器 —— 必须报"对不上"（退出码 3）。
        # 用显式路径传，不动真底模文件。
        rc_b, out_b = studio.run(
            mkjob(None, "probe", "验收：底模负对照"),
            '"%s" -s "%s" v1 40k "%s" "%s"'
            % (studio.RVC_PY, os.path.join(studio.STAGES, "rvc_pretrain_check.py"),
               dp, dp),
            cwd=studio.RVC_ROOT, timeout=600)
        check("负对照：底模对不上架构时必须红（缺键/多键都要报出来）",
              rc_b != 0 and "PRETRAIN_MISMATCH" in (out_b or ""),
              "退出码 %d" % rc_b)
    # 静态守卫：训练命令行必须带 -pg/-pd。改回去是很容易发生的（有人觉得
    # "不就是两个参数吗"），而它的后果是静默训出一个废模型。
    with open(os.path.join(HERE, "studio.py"), encoding="utf-8") as f:
        src_studio = f.read()
    check("训练命令行里固定带 -pg / -pd（不许再退回从零训）",
          '-pg "%s"' in src_studio and '-pd "%s"' in src_studio)
    check("没有底模时训练会硬失败，而不是静默训个废模型",
          "底模（预训练模型）没准备好" in src_studio)

    # ------------------------------------------------------------------
    print("\n=== 9. 视频可以直接当歌用（自动抽音轨）===")
    vid = next((os.path.join(studio.UPLOADS, f) for f in os.listdir(studio.UPLOADS)
                if studio.is_video(f)), None) if os.path.isdir(studio.UPLOADS) else None
    if not vid:
        print("  （跳过）uploads 里没有视频，没法实测")
    else:
        check("能认出视频后缀（音频不会被误判）",
              studio.is_video(vid) and not studio.is_video(SRC))
        audio, why = studio.extract_audio(vid)
        check("视频能抽出音轨（后面分离/变声/混音都吃音频，不吃视频）",
              bool(audio) and os.path.isfile(audio), why[:90])
        if audio:
            secs = studio.wav_seconds(audio) or 0
            check("抽出来的音轨有时长（不是空文件）", secs > 1.0,
                  "%.1f 秒 / %.1f MB" % (secs, os.path.getsize(audio) / 1e6))
            try:
                os.remove(audio)
            except OSError:
                pass
        # 负对照：后缀是视频但没有音频流 —— 必须报失败，不能把垃圾文件放行下去
        fake = os.path.join(studio.UPLOADS, "_probe_fake.mp4")
        with open(fake, "wb") as f:
            f.write(b"not a video" * 200)
        fa, fwhy = studio.extract_audio(fake)
        check("负对照：没有音频流的假视频必须报失败",
              not fa and bool(fwhy), (fwhy or "")[:80])
        try:
            os.remove(fake)
        except OSError:
            pass

    # 收尾：把验收造出来的样片从角色成品清单里摘掉（用户自己的成品一条都不能动）。
    studio.mutate_role(RID, lambda r: r.update({"outputs": outs_before}))
    now_outs = [o.get("wav") for o in (studio.get_role(RID).get("outputs") or [])]
    check("验收造的样片没有留在角色成品清单里",
          now_outs == [o.get("wav") for o in outs_before],
          "now=%d was=%d" % (len(now_outs), len(outs_before)))

    return summary()


def summary():
    print("\n" + "=" * 70)
    print("通过 %d 项，失败 %d 项" % (len(OK), len(BAD)))
    for b in BAD:
        print("  ! " + b)
    print("ALLDONE" if not BAD else "HASFAIL")
    print("=" * 70)
    return 1 if BAD else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        # 收尾：删掉本次验收的临时目录、角色目录里的 vtest 产物、以及测试任务记录。
        # 不清理的话任务中心会被一堆 "验收：分离" 淹掉，用户找不到自己的任务。
        #
        # ★★ 整段必须吞掉 **BaseException**（2026-10-20 实测）：
        #    本机环境层有批量删除钩子（一条命令累计 50 次），超了之后**每一个**删除
        #    动作都抛 SystemExit。而 `rmtree(ignore_errors=True)` 只接 OSError、
        #    `contextlib.suppress(Exception)` 也接不住 SystemExit —— 于是清理把
        #    SystemExit 抛到顶层，整关被判成"中断（一条判据都没跑到）"。
        #    实测：本关判据 41/0 全绿，退出码却是 1，分诊因此 block。
        #    清理收不干净只是留垃圾，**不该影响验收结论**。
        try:
            shutil.rmtree(WORK, ignore_errors=True)
            for p in glob.glob(os.path.join(studio.role_dir(RID), "final", "*vtest*")):
                with contextlib.suppress(BaseException):
                    os.remove(p)
            for d in CLEANUP_JOBS:
                if d:
                    shutil.rmtree(d, ignore_errors=True)
            # 探针角色是本次验收造的，删掉；权重和索引是磁盘上的真实产物，不动
            if _MADE_ROLE[0]:
                with contextlib.suppress(BaseException):
                    studio.delete_role(_MADE_ROLE[0])
        except BaseException as e:
            print("[warn] 收尾清理被打断（%s: %s）—— 不影响验收结论，"
                  "残留请用 `bash _run_regression.sh gc` 另起一条命令清"
                  % (type(e).__name__, e))
    sys.exit(code or 0)
