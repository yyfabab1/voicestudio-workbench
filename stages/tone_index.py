# -*- coding: utf-8 -*-
r"""
把角色所有语音切片的**声学特征**算出来存成 JSON，供「按句控语气」挑参考用。

为什么要单独存一份离线索引：
  「这一句要什么语气」的本质是「这一句用哪段参考音频」。而挑参考要看的是
  音高/起伏/能量/语速这些声学量 —— 它们不随要合成的文本变，只跟素材有关，
  所以算一次存着就行。运行时只需要排序打分，不必每次都跑一遍 librosa。

为什么必须独立脚本 + 子进程：
  这一层依赖 librosa + soundfile，而 VoiceStudio 服务进程没有（也不该有 ——
  服务进程要能用一个精简解释器起来）。跨进程调用是这个项目的一贯做法
  （见 stages/tts_batch.py 顶部的说明）。

用法：
  <带 librosa 的 python> -s tone_index.py <角色目录> <输出json>

输入：角色目录下的 slices/*.wav，以及 roles.json 里这个角色的 segments（要文本）。
      没有文本的切片直接跳过 —— GPT-SoVITS 合成时必须知道参考音频说了什么。

落盘：原子写（先写 .tmp 再 os.replace），避免读到写了一半的 JSON。
"""
import io
import json
import os
import sys
import time

import numpy as np


def _log(m):
    print("[语气索引] %s" % m, flush=True)


def analyze(path, sr_target=16000):
    """提取一句语音的声学特征。返回 dict，分析不出来返回 None。

    几个字段为什么这么算：
      f0_range 用 10~90 分位而不是 max-min —— 极个别帧的野值会把 max 拉飞，
      实测同一句能差出 80Hz；分位稳得多。
      voiced 是有声帧占比 —— 它低意味着这人说话停顿多（慢吞吞/犹豫），
      用作语气的负向指标很灵。
    """
    try:
        import soundfile as sf
        import librosa
    except ImportError as e:
        raise SystemExit("缺少依赖：%s（这个脚本要在带 librosa 的解释器里跑）" % e)

    x, sr = sf.read(path)
    if x.ndim > 1:
        x = x.mean(axis=1)
    x = np.asarray(x, dtype=np.float64)
    if x.size < sr * 0.3:
        return None
    if sr != sr_target:
        x = librosa.resample(x, orig_sr=sr, target_sr=sr_target)
        sr = sr_target

    hop = int(sr * 0.020)
    win = int(sr * 0.050)
    frames = librosa.util.frame(x, frame_length=win, hop_length=hop)
    rms = np.sqrt((frames ** 2).mean(axis=0) + 1e-12)
    voiced = rms > (rms.max() * 0.08 + 1e-9)
    if int(voiced.sum()) < 5:
        return None
    rv = rms[voiced]

    try:
        f0, vf, vp = librosa.pyin(x, fmin=60, fmax=400, sr=sr,
                                  frame_length=win, hop_length=hop)
        f0v = f0[(vf | (vp > 0.7)) & np.isfinite(f0)]
    except Exception:
        f0v = np.array([])
    if len(f0v) < 10:
        return None

    dur = len(x) / float(sr)
    return {
        "dur": round(dur, 2),
        "rms_mean": round(float(rv.mean()), 5),
        "rms_std": round(float(rv.std()), 5),
        "f0_mean": round(float(np.mean(f0v)), 1),
        "f0_std": round(float(np.std(f0v)), 1),
        "f0_range": round(float(np.percentile(f0v, 90) - np.percentile(f0v, 10)), 1),
        "voiced": round(float(voiced.mean()), 3),
    }


def build(role_dir, segs, out_path, log=_log):
    """角色的 slices 逐个分析，和 segments（提供文本）拼起来写出去。

    `segs` = roles.json 里这个角色的 segments 列表，每项至少要有 file / text。
    """
    sdir = os.path.join(role_dir, "slices")
    if not os.path.isdir(sdir):
        return None, "没有 slices 目录"
    txt = {}
    for s in (segs or []):
        t = (s.get("text") or "").strip()
        if t and s.get("file"):
            txt[s["file"]] = t
    if not txt:
        return None, "没有任何带文本的切片（先做一次 ASR 识别）"

    items = []
    files = sorted([f for f in os.listdir(sdir) if f.lower().endswith(".wav")])
    t0 = time.time()
    for i, f in enumerate(files):
        t = txt.get(f)
        if not t:
            continue                      # 没文本就没法当参考
        try:
            r = analyze(os.path.join(sdir, f))
        except Exception:
            r = None
        if not r:
            continue
        r["file"] = f
        r["text"] = t
        # 语速（字/秒）：它是"这一句读得急不急"最直接的量。
        # ★ 实测踩过：挑到一段平淡的报数式台词当参考，同一句文本被合成成 12.5 秒
        #   （其他参考都是 3.6~4.5 秒）——模型把参考里的拖沓一起学走了。
        r["syl_rate"] = round(len([c for c in t if c.strip()]) / max(0.5, r["dur"]), 3)
        items.append(r)
        if (i + 1) % 50 == 0:
            log("  已分析 %d/%d（有效 %d）" % (i + 1, len(files), len(items)))

    if not items:
        return None, "所有切片都分析不出有效声学特征"
    data = {"built": time.strftime("%Y-%m-%d %H:%M:%S"),
            "role": os.path.basename(role_dir.rstrip("\\/")),
            "items": items}
    tmp = out_path + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, out_path)            # 原子写
    log("有效 %d 条，用时 %.1fs" % (len(items), time.time() - t0))
    return data, None


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    role_dir, out_path = sys.argv[1], sys.argv[2]
    rid = os.path.basename(role_dir.rstrip("\\/"))
    # roles.json 的位置不要写死：它在 <data>/roles.json，而角色在 <data>/roles/<id>/。
    # 往上找两级，找到为止 —— 写"反正就在 ../../"这种假设，换个部署就断。
    segs = []
    pj = None
    d = os.path.abspath(role_dir)
    for _ in range(4):
        cand = os.path.join(d, "roles.json")
        if os.path.isfile(cand):
            pj = cand
            break
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    if not pj:
        _log("没找到 roles.json（从 %s 往上 4 级都没有）" % role_dir)
    else:
        try:
            db = json.load(io.open(pj, encoding="utf-8"))
            rl = db["roles"] if isinstance(db, dict) and "roles" in db else db
            rl = list(rl.values()) if isinstance(rl, dict) else rl
            hit = [x for x in rl if x.get("id") == rid]
            if hit:
                segs = hit[0].get("segments") or []
            else:
                _log("roles.json 里没有 id=%s 的角色" % rid)
        except Exception as e:
            _log("读 roles.json 失败：%s" % e)
    data, err = build(role_dir, segs, out_path)
    if err:
        print("[语气索引] 失败：%s" % err)
        sys.exit(3)


if __name__ == "__main__":
    main()
