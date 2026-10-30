# -*- coding: utf-8 -*-
r"""第二套引擎的入口：so-vits-svc 4.0（fork）推理。

为什么需要它
------------
小马宝莉 M6 那套 **likes 最高**（155）的权重是 so-vits-svc 4.0 格式，RVC 链路跑不了。
用户要求"两套并存" ⇒ 唱歌时可以用 `engine="svc"` 走这套，其余后处理
（高频补偿 / 去金属 / 临场感 / 3~10k 补偿 / 非歌声保护 / 混音）**完全复用**，
这样两套引擎的成品质量下限一致。

为什么用 fork 而不是原版
------------------------
原版 `svc-develop-team/so-vits-svc` 已归档，推理要 `fairseq` —— Windows + py3.12 装不上。
`voicepaw/so-vits-svc-fork`（4.2.30）基于 4.0 v1，README 明说「4.0 (v1) 的模型兼容」，
且不依赖 fairseq（HuBERT 走 transformers 的 `content-vec-best`）。

环境（实测，2026-10-27）
------------------------
  · `D:\AHC\svc-fork-venv`：py3.10.21 + **复用** YingMusic 那套 torch 2.7.1+cu128
    （用 .pth 挂过去，省掉 2.4 GB 的 torch 下载）；`so-vits-svc-fork==4.2.30` 装进去。
  · 两个必须的开关：
      `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1` —— torch≥2.6 把 torch.load 默认改成
        weights_only=True，而 so-vits 的 kmeans 是 numpy pickle，会直接 UnpicklingError。
      `HF_ENDPOINT=https://hf-mirror.com` —— HuBERT 走 transformers 下载，必须指镜像。

用法
----
    <svc-python> stages/svc_cli.py --model G_45600.pth --config config.json \
        --kmeans kmeans_10000.pt --input in.wav --output out.wav \
        [--pitch 0] [--f0 dio] [--device cuda:0] [--report r.json]

stdout 只打一行机器可读结果（SVC_OK / SVC_FAIL）；**没有 SVC_OK 就不许当成品用**
—— 与 hf_cli / presence_cli 同一个约定。
"""

import argparse
import json
import os
import subprocess
import sys
import time
import wave

SVC_PY_DEFAULT = r"D:\AHC\svc-fork-venv\Scripts\python.exe"
F0_CHOICES = ("dio", "crepe", "crepe-tiny", "parselmouth", "harvest")


def _limit_peak(path, ceiling=0.95):
    """输出峰值超过 0.99 就整体缩到 ceiling，别让 PCM16 硬削顶。

    为什么必须有：fork 用 soundfile 直接写 PCM16，模型输出一旦超 1.0 就被**硬削顶**。
    实测一条对唱里 SVC 轨峰值正好 1.000 —— 削顶在听感上就是「哑 / 破 / 白嗓」，
    而且它发生在我们的后处理之前，后面再怎么修都救不回来。
    返回 (原峰值, 是否缩放)。
    """
    try:
        import numpy as np
        import soundfile as sf
    except Exception:
        return 0.0, False
    try:
        x, sr = sf.read(path, dtype="float32", always_2d=False)
    except Exception:
        return 0.0, False
    pk = float(np.abs(x).max()) if len(x) else 0.0
    if pk <= 0.99:
        return round(pk, 4), False
    y = x * (ceiling / pk)
    sf.write(path, y.astype(np.float32), sr, subtype="PCM_16")
    return round(pk, 4), True


def svc_py():
    return os.environ.get("VS_SVC_PY") or SVC_PY_DEFAULT


def wav_secs(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def _dump(path, obj):
    if not path:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception as e:
        sys.stderr.write("SVC 报告写不了：%s\n" % e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, help="G_*.pth（生成器；D_ 判别器推理不需要）")
    ap.add_argument("--config", required=True, help="该模型的 config.json（4.0 v1）")
    ap.add_argument("--kmeans", default="", help="kmeans_*.pt（没有就不做聚类检索）")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--pitch", type=int, default=0, help="变调半音数")
    ap.add_argument("--f0", default="harvest", choices=F0_CHOICES,
                    help="基频提取：实测 harvest 最好（15 秒素材：吻合 66.1%% / 八度错 20.3%% / "
                         "轮廓相关 +0.589；dio 55.1%%/26.8%%/+0.498；crepe 59.3%%/29.6%%/+0.058）")
    ap.add_argument("--device", default="cuda:0", help="cuda:0 或 cpu")
    ap.add_argument("--cluster-ratio", type=float, default=0.0,
                    help="聚类检索强度（0=不检索；>0 需要 --kmeans）")
    ap.add_argument("--report", default="")
    a = ap.parse_args()

    res = {"ok": False, "engine": "svc", "pitch": a.pitch, "f0": a.f0}
    # ---- 前置检查：缺件要说清是哪一件，不许等到 torch.load 才炸 ----
    for label, p in (("权重", a.model), ("配置", a.config)):
        if not p or not os.path.isfile(p):
            res["reason"] = "缺%s文件：%s" % (label, p)
            _dump(a.report, res)
            sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
            return 1
    if a.kmeans and not os.path.isfile(a.kmeans):
        res["reason"] = "缺 kmeans 文件：%s" % a.kmeans
        _dump(a.report, res)
        sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
        return 1
    py = svc_py()
    if not os.path.isfile(py):
        res["reason"] = "svc 环境不在：%s" % py
        _dump(a.report, res)
        sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
        return 1
    if not os.path.isfile(a.input):
        res["reason"] = "输入不存在：%s" % a.input
        _dump(a.report, res)
        sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
        return 1
    if os.path.abspath(a.input) == os.path.abspath(a.output):
        res["reason"] = "输入输出是同一个文件（会原地覆盖）"
        _dump(a.report, res)
        sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
        return 1

    out_dir = os.path.dirname(os.path.abspath(a.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(os.devnull, "w"):
        pass
    try:
        os.remove(a.output)          # 同名残留会让"到底写没写"看不出来
    except OSError:
        pass

    env = dict(os.environ)
    env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"   # 见文件头：kmeans 是 numpy pickle
    env.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    env["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"

    cmd = [py, "-m", "so_vits_svc_fork", "infer", a.input,
           "-o", a.output, "-m", a.model, "-c", a.config,
           # ★ 必须关掉 auto_predict_f0（= -na）：fork 默认 `-a`，此时 f0 由**模型自己
           #   预测**，`-t/--transpose` 会被直接忽略。实测：--pitch 0 出来低 5.7 个半音，
           #   --pitch 6 出来低 5.4 个半音 —— 移调根本没生效，就是吃了这个默认值。
           #   关掉之后 f0 跟随输入（和 RVC 的语义一致），变调才真的算数。
           "-na",
           "-t", str(a.pitch), "-fm", a.f0, "-d", a.device]
    if a.kmeans:
        cmd += ["-k", a.kmeans, "-r", str(a.cluster_ratio)]
    t0 = os.times().elapsed
    try:
        pr = subprocess.run(cmd, capture_output=True, env=env, timeout=3600)
    except subprocess.TimeoutExpired:
        res["reason"] = "推理超时（3600 秒）"
        _dump(a.report, res)
        sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
        return 1
    dt = os.times().elapsed - t0

    if pr.returncode != 0 or not os.path.isfile(a.output):
        tail = []
        for stream in (pr.stdout, pr.stderr):
            if stream:
                tail += stream.decode("utf-8", "replace").strip().splitlines()[-4:]
        res["reason"] = "退出码 %d：%s" % (pr.returncode, " | ".join(tail)[:400])
        res["cmd"] = " ".join(cmd)
        _dump(a.report, res)
        sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
        return 1

    got, want = wav_secs(a.output), wav_secs(a.input)
    pk, limited = _limit_peak(a.output)
    res.update({"ok": True, "secs": round(got, 2), "in_secs": round(want, 2),
                "elapsed": round(dt, 1), "output": a.output,
                "peak": pk, "peak_limited": bool(limited)})
    if want > 0.5 and abs(got - want) > max(0.4, want * 0.05):
        # 长度对不上 = 后面按时间放回原位/混音会整体错位，必须当失败处理
        res["ok"] = False
        res["reason"] = ("输出时长 %.2fs 与输入 %.2fs 差太多（>5%%）—— "
                         "直接拼回去会让整首歌错位" % (got, want))
        _dump(a.report, res)
        sys.stdout.write("SVC_FAIL %s\n" % res["reason"])
        return 1
    _dump(a.report, res)
    sys.stdout.write("SVC_OK secs=%.2f pitch=%d f0=%s elapsed=%.1fs peak=%.3f%s out=%s\n"
                     % (got, a.pitch, a.f0, dt, pk,
                        " peak_limited=1" if limited else "",
                        os.path.basename(a.output)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
