# -*- coding: utf-8 -*-
r"""RVC 推理 —— 自己调 VC，而不是走 `infer.cli`。

为什么不直接用整合包自带的 `python -m infer.cli`：
  1. 那个 CLI 的 `--f0-method` 只放开了 `pm / rmvpe`，**fcpe 被挡住了**
     （pipeline 本身是支持 fcpe 的，见 infer/vc/pipeline.py 的 get_f0）。
     FCPE 比 RMVPE 更准更快，是"唱得准不准"最直接的一块。
  2. CLI 不让我们把「没有可用索引」和「索引命中了但用不上」分开处理；
     这里要能拿到 status 文字，好在日志里说清到底用了哪个 f0 方法、有没有索引。
  3. 后面要加的「非歌声段保护」需要拿**同一份 f0 曲线**做段级判定 ——
     走自己的入口才拿得到中间量。

**不改 RVC 整合包的任何文件**：只 import 它的模块。整合包是用户自己装的第三方件，
改它等于把我们的版本和用户的版本绑死，下次他升级就炸。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器）：
    runtime\python.exe -s <本脚本> --model <模型.pth 或 文件名> --input <in.wav> \
        --output <out.wav> [--pitch 0] [--f0 rmvpe|fcpe|pm] [--index-rate 0.75] \
        [--index <index 路径>] [--rms-mix-rate 0.25] [--protect 0.33] \
        [--speaker-id 0] [--resample-sr 0]

退出码：0 成功；2 参数/环境错误；1 推理失败。
"""
import argparse
import os
import sys
import traceback

if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
os.environ.setdefault("weight_root", os.path.join(os.getcwd(), "assets", "weights"))
os.environ.setdefault("index_root", os.path.join(os.getcwd(), "logs"))
os.environ.setdefault("outside_index_root", os.path.join(os.getcwd(), "assets", "indices"))


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--pitch", type=int, default=0, help="变调半音数")
    p.add_argument("--f0", dest="f0_method", default="rmvpe",
                   choices=["pm", "rmvpe", "fcpe"])
    p.add_argument("--index-rate", type=float, default=0.75)
    p.add_argument("--index", default="", help="不传就按 RVC 自己的规则自动找")
    p.add_argument("--rms-mix-rate", type=float, default=0.25)
    p.add_argument("--protect", type=float, default=0.33)
    p.add_argument("--speaker-id", type=int, default=0)
    p.add_argument("--resample-sr", type=int, default=0)
    return p


def main():
    a = build_parser().parse_args()
    if not os.path.isfile(a.input):
        print("[err] 输入不存在：%s" % a.input)
        return 2

    model_path = a.model
    if not os.path.isfile(model_path):
        cand = os.path.join(os.environ["weight_root"], a.model)
        if not os.path.isfile(cand):
            print("[err] 模型不存在：%s" % a.model)
            return 2
        model_path = cand
    # VC.get_vc() 只吃文件名（它在 weight_root 里找），所以把 root 指到模型所在目录
    os.environ["weight_root"] = os.path.dirname(os.path.abspath(model_path))
    model_name = os.path.basename(model_path)

    import numpy as np  # noqa: E402
    import soundfile as sf  # noqa: E402

    # Config() 内部是 argparse，会吃 sys.argv —— 必须先把我们的参数摘掉，
    # 否则它会把 --model 当成自己的未知参数直接退出（cli.py 里也是这么护的）。
    from configs.config import Config
    from infer.vc.modules import VC

    original_argv = sys.argv[:]
    sys.argv = [sys.argv[0]]
    try:
        config = Config()
    finally:
        sys.argv = original_argv

    print("设备 %s | 精度 %s" % (config.device, config.dtype))
    print("模型 %s | pitch %+d | f0 %s | index-rate %.2f | rms-mix %.2f | protect %.2f"
          % (model_name, a.pitch, a.f0_method, a.index_rate, a.rms_mix_rate, a.protect))

    vc = VC(config)
    vc.get_vc(model_name)

    index_path = a.index
    if a.index_rate > 0 and not index_path:
        from infer.vc.utils import get_index_path_from_model
        index_path = get_index_path_from_model(model_name, a.speaker_id)
    if a.index_rate > 0 and not (index_path and os.path.isfile(index_path)):
        print("[warn] 没有可用的 added 索引，按 index-rate 0 推理")
        a.index_rate = 0.0
        index_path = ""
    print("索引 %s" % (os.path.basename(index_path) if index_path else "（未使用）"))

    status, result = vc.vc_single(
        a.speaker_id, a.input, a.pitch, a.f0_method, index_path, a.index_rate,
        a.resample_sr, a.rms_mix_rate, a.protect)
    print(status)
    if not result or result[0] is None or result[1] is None:
        print("[err] 推理失败")
        return 1

    sr, audio = result
    audio = np.asarray(audio).reshape(-1)
    # ★ 这里有个会毁掉整首歌的坑（实测踩到）：RVC 的 pipeline 返回的是 **int16**
    #   （±32767），不是 ±1 的浮点。如果顺手 `astype("float32")` 再交给 soundfile，
    #   soundfile 会把它当浮点数据写 PCM_16 ⇒ 99.4% 的采样被削平，输出变成刺耳的
    #   方波失真，而 RVC 自己不会报任何错。整合包自带的 infer.cli 之所以没这个问题，
    #   是因为它**原样**把数组交给 soundfile（int16 走 int16 的路）。
    #   ⇒ 整型保持整型，浮点才转 float32。
    if not np.issubdtype(audio.dtype, np.integer):
        audio = audio.astype("float32", copy=False)
    print("[dbg] dtype=%s shape=%s min=%.4f max=%.4f rms=%.4f"
          % (audio.dtype, audio.shape, float(audio.min()), float(audio.max()),
             float(np.sqrt((np.asarray(audio, dtype="float64") ** 2).mean()))))
    os.makedirs(os.path.dirname(os.path.abspath(a.output)), exist_ok=True)
    sf.write(a.output, audio, sr)
    print("[ok] %s  %.2f 秒 @ %d Hz" % (a.output, len(audio) / float(sr), sr))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
