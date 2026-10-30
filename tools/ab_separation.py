# -*- coding: utf-8 -*-
r"""分离器比稿：**换更强的分离器到底值不值**（2026-10-26 起的常驻工具）。

什么时候用它：GitHub/榜单上出了新的分离模型、或者想复核"现在的主力还是不是最好"。
拿**老板自己的歌**（不是榜单数据集）+ 项目自己的尺子 sep_eval 打分，排相对顺序。
★ 别直接照榜单 SDR 换模型：2026-10-26 实测榜单最强的 KJ（SDR 10.98）在 8 首歌上
  平均 56.43，被一直被当垫底的 HP2（65.90）压着 —— 分布不同，榜单不管用。

------------------------------------------------------------------ v1 为什么作废
v1 的结果不能用：第 2 首四个模型拿了一模一样的分（leak=0.0000、floor=0.00），
换算下来那段根本是**静音**（35 秒处落在歌里的空档）。模型在没内容的素材上
输出的都是零，分数自然完全相同——那不是"英雄所见略同"，那是尺子没东西可量。
① 自动挑**最响的** 30 秒（滑窗找 RMS 最大），把静音段挡在门外；
② 把挑中的起点和 RMS 打出来，方便事后复核"这一段到底有没有东西"；
③ 样本从 4 首扩到 8 首，且混了「用户录的音轨」和「真歌 mp3」两类。

------------------------------------------------------------------ 尺子，以及它的边界
只用 sep_eval.score（0~100，不需要真值，权重经合成真值校准）。
两把尺子的事：本项目没有带分轨（stems）的素材，所以拿某个模型的输出当
"真值"去算 SDR，必然偏向那个模型自己（`_ab_sepens.py` 里踩过）。
★ 因此**这里的 score 只能用来排同一批歌上的相对顺序**，不能宣称绝对值。
若结论要写进流水线，**再用耳朵复核一遍**（真值在老板耳朵里）。

用法：
    <VS_MSST_PY> -X utf8 tools/ab_separation.py
输出：控制台表格 + _ab_separation.json（明细，可事后复核每首每模型的分）
"""
import atexit
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import numpy as np
import soundfile as sf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # 仓库根（本脚本住在 tools/）
GSV_ROOT = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0"
PY_GSV = os.path.join(GSV_ROOT, "runtime", "python.exe")
PY_MSST = os.path.join(os.path.dirname(ROOT),
                       "YingMusic-Singer-Plus-main", ".venv", "Scripts", "python.exe")
FFMPEG = os.path.join(GSV_ROOT, "runtime", "ffmpeg.exe")

SONGS = [
    r"data\uploads\20261011-072442_音轨.wav",
    r"data\uploads\20261012-155226_音轨.wav",
    r"data\uploads\20261011-172838_音轨.wav",
    r"data\uploads\20261011-091410_音轨.wav",
    r"data\uploads\20261011-172145_音轨.wav",
    r"data\uploads\20261012-155427_音轨.wav",
    r"data\uploads\林俊杰 - 曹操.mp3",
    r"data\uploads\嘟比Dubi - 神耀（星际争霸同人歌）.mp3",
]
PROBE_SEC = 30.0
STEP_SEC = 10.0          # 挑片段的滑窗步长
MAX_SCAN = 180.0         # 最多往前扫这么远找响的位置

# (标签, 走哪条路, 模型名)
CANDIDATES = [
    ("HP2_all_vocals", "uvr", "HP2_all_vocals"),
    ("HP5_only_main_vocal", "uvr", "HP5_only_main_vocal"),
    ("KJ-MelBand[新]", "msst", "mel_band_roformer_kj"),
]

TMP = tempfile.mkdtemp(prefix="ab_sepkj_")
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))
rows = []


def _run(argv, timeout=1800):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=ROOT)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -9, "TIMEOUT"


def _rms(path):
    try:
        x, _sr = sf.read(path, dtype="float32")
        return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))
    except Exception:
        return 0.0


def pick_clip(song, out_wav):
    """来回扫几个候选起点，挑**最响**的那一段（静音段一律不要）。"""
    trial = os.path.join(TMP, "_trial.wav")
    best, best_rms = 0.0, -1.0
    t = 0.0
    while t < MAX_SCAN:
        rc, _ = _run([FFMPEG, "-y", "-v", "error", "-ss", "%.1f" % t,
                      "-t", str(PROBE_SEC), "-i", song, "-ac", "2", "-ar", "44100",
                      trial], timeout=300)
        if rc == 0 and os.path.isfile(trial):
            try:
                total = sf.info(trial).duration
            except Exception:
                total = 0.0
            if total < PROBE_SEC * 0.9:      # 已经到歌尾巴了，够不着整段
                break
            r = _rms(trial)
            if r > best_rms:
                best, best_rms = t, r
        t += STEP_SEC
    if best_rms < 0.02:                       # 整首都是安静的 ⇒ 没法定 dj suction
        return None, None, best_rms
    rc, _ = _run([FFMPEG, "-y", "-v", "error", "-ss", "%.1f" % best,
                  "-t", str(PROBE_SEC), "-i", song, "-ac", "2", "-ar", "44100",
                  out_wav], timeout=300)
    if rc != 0 or not os.path.isfile(out_wav):
        return None, None, best_rms
    return best, out_wav, best_rms


def score(mix, vocal, inst):
    rc, out = _run([PY_MSST, os.path.join(ROOT, "stages", "sep_eval.py"),
                    "score", mix, vocal, inst], timeout=600)
    if rc != 0:
        return None, "score 失败: %s" % out[-200:]
    m = re.search(r"SEP_SCORE (.+)", out)
    if not m:
        return None, "没解析出分数: %s" % out[-200:]
    d = {}
    for kv in m.group(1).split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                d[k] = float(v)
            except ValueError:
                d[k] = v
    return d, None


def separate(how, model, in_dir, vdir, idir):
    for d in (vdir, idir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    t0 = time.time()
    if how == "uvr":
        argv = [PY_GSV, "-s", os.path.join(ROOT, "stages", "uvr_cli.py"),
                GSV_ROOT, model, in_dir, vdir, idir, "10", "wav", "cuda", "True"]
    else:
        argv = [PY_MSST, "-s", os.path.join(ROOT, "stages", "msst_cli.py"),
                in_dir, vdir, idir, model]
    rc, out = _run(argv)
    dt = time.time() - t0
    if rc != 0:
        return None, None, dt, "退出码 %d：%s" % (rc, out[-260:])
    vs = [f for f in os.listdir(vdir) if f.lower().endswith(".wav")]
    ins = [f for f in os.listdir(idir) if f.lower().endswith(".wav")]
    if not vs or not ins:
        return None, None, dt, "没产出（人声%d 伴奏%d）%s" % (len(vs), len(ins), out[-260:])
    return os.path.join(vdir, vs[0]), os.path.join(idir, ins[0]), dt, None


def main():
    print("临时目录:", TMP)
    used = []
    for si, rel in enumerate(SONGS):
        song = os.path.join(ROOT, rel)
        if not os.path.isfile(song):
            print("!! 找不到 %s" % rel)
            continue
        mix_dir = os.path.join(TMP, "in%d" % si)
        os.makedirs(mix_dir, exist_ok=True)
        mix = os.path.join(mix_dir, "source.wav")
        start, _p, rms = pick_clip(song, mix)
        if start is None or not os.path.isfile(mix):
            print("!! %s 挑不出有内容的片段（rms=%.4f），跳过" % (rel, rms))
            continue
        name = os.path.basename(rel)
        used.append(name)
        print("\n" + "=" * 78)
        print("第 %d 首：%s   起点 %.0fs  rms=%.4f" % (len(used), name, start, rms))
        print("=" * 78)
        for label, how, model in CANDIDATES:
            tag = "%d_%s" % (si, re.sub(r"\W", "", model)[:10])
            v, i, dt, err = separate(how, model, mix_dir,
                                     os.path.join(TMP, "v_" + tag),
                                     os.path.join(TMP, "i_" + tag))
            if err:
                print("  %-24s ✗ %s" % (label, err[:100]))
                rows.append({"song": name, "label": label, "ok": False,
                             "err": err[:260], "sec": round(dt, 1)})
                sys.stdout.flush()
                continue
            sc, serr = score(mix, v, i)
            if serr:
                print("  %-24s 分离 OK 但%s" % (label, serr[:100]))
                rows.append({"song": name, "label": label, "ok": False,
                             "err": serr[:260], "sec": round(dt, 1)})
                sys.stdout.flush()
                continue
            rows.append({"song": name, "label": label, "ok": True,
                         "score": sc.get("score"), "leak": sc.get("leak"),
                         "floor": sc.get("floor_db"), "hf": sc.get("hf"),
                         "body": sc.get("body"), "sec": round(dt, 1)})
            print("  %-24s score=%6.2f  leak=%.4f  floor=%8.2f  hf=%.2f  body=%.2f  (%4.1fs)"
                  % (label, sc.get("score") or 0, sc.get("leak") or 0,
                     sc.get("floor_db") or 0, sc.get("hf") or 0,
                     sc.get("body") or 0, dt))
            sys.stdout.flush()

    print("\n" + "=" * 78)
    print("汇总（score 越高越好；score/leak 都是无真值代理指标）")
    print("=" * 78)
    songs = used
    hdr = "%-24s %s" % ("模型", "".join("%9s" % ("第%d首" % (i + 1)) for i in range(len(songs))))
    print(hdr)
    best = {}
    for s in songs:
        hit = [r for r in rows if r["song"] == s and r["ok"]]
        if hit:
            best[s] = max(hit, key=lambda r: r["score"])
    for label, _h, _m in CANDIDATES:
        cells, tot, n, wins = [], 0.0, 0, 0
        for s in songs:
            hit = [r for r in rows if r["song"] == s and r["label"] == label and r["ok"]]
            if hit:
                mark = "★" if best.get(s) and best[s]["label"] == label else " "
                cells.append("%8.2f%s" % (hit[0]["score"], mark))
                tot += hit[0]["score"]
                n += 1
                if best.get(s) and best[s]["label"] == label:
                    wins += 1
            else:
                cells.append("%9s" % "✗")
        print("%-24s %s 平均 %6.2f (n=%d, 夺冠 %d/%d)"
              % (label, "".join(cells), tot / n if n else 0, n, wins, len(songs)))
    print("\n★ = 这首歌上分数最高的那个")
    with open(os.path.join(ROOT, "_ab_separation.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1)
    print("明细已写 _ab_separation.json")


if __name__ == "__main__":
    main()
