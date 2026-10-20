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
import re
import sys
import traceback

if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())
os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
os.environ.setdefault("weight_root", os.path.join(os.getcwd(), "assets", "weights"))
os.environ.setdefault("index_root", os.path.join(os.getcwd(), "logs"))
os.environ.setdefault("outside_index_root", os.path.join(os.getcwd(), "assets", "indices"))


def _index_owner(index_name):
    """这个索引文件**属于哪个模型** —— 取名字里第一个 `_added_` 之前的部分。

    为什么需要它（实测踩到的真事故，2026-10-20）：
        train_index.py 的命名是
            <模型名>_added_IVF<n>_Flat_nprobe_1_<模型名>_v1.index
        所以 `split("_added_")[0]` 就是它服务的那个模型的 stem，一点不含糊。

    而 RVC 自带的 `get_index_path_from_model()` 认的是「名字里出现过实验名」，
    最后的 tie-break 是 **`-mtime`（最新的赢）**。于是同一个角色目录下只要
    多试过几个模型，就会出现：
        ra500fb92.pth（v1，256 维输出）
          → 被配上 ra500fb92_v2_klm48_..._v2.index（v2，768 维）
          → faiss 的 `assert d == self.d` 直接崩
          → 整首歌"歌声转换失败"，报错只有一行 AssertionError，根本看不出是索引配错。
    实测复现：`get_index_path_from_model("ra500fb92.pth", 0)` 确实返回 v2_klm48 的索引。
    这是"多模型共存"才会触发的 bug，而多试几个模型恰恰是正常用法。

    没有 `_added_` 的名字（合并索引 / 老命名）返回空串，交给原规则兜底。
    """
    stem = os.path.splitext(os.path.basename(index_name or ""))[0].lower()
    i = stem.find("_added_")
    if i <= 0:
        return ""
    return stem[:i]


def pick_index(model_name, speaker_id=0):
    """按「索引归属」给这个模型挑它自己的 added 索引，返回路径或空串。

    口径与 RVC 一致的地方（否则会出现"界面说有索引、推理说没有"）：
      · 跳过名字带 trained 的（那是合并索引，不是 added）；
      · 认 `<exp>_added_*`、含 `_<exp>_v1` / `_<exp>_v2` 的；
      · 模型名带 `_e<轮>_s<步>` 尾巴时，先去掉再比。
    比 RVC **更严**的地方：候选索引的"归属模型"必须就是本模型 ——
    仅仅名字里含实验名不算（那正是上面那个 bug）。
    """
    stem = os.path.splitext(os.path.basename(str(model_name or "")))[0]
    exp = re.sub(r"_e\d+_s\d+$", "", stem, flags=re.IGNORECASE).lower()
    if not exp:
        return ""
    stem_low = stem.lower()
    roots = [os.environ.get("outside_index_root"), os.environ.get("index_root")]
    best, best_mtime = "", -1.0
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for name in files:
                low_name = name.lower()
                if not low_name.endswith(".index") or "trained" in low_name:
                    continue
                owner = _index_owner(name)
                if not owner:
                    continue
                # 归属必须是本模型：完整 stem，或去掉 _eN_sN 之后的实验名。
                if owner not in (stem_low, exp):
                    continue
                path = os.path.abspath(os.path.join(dirpath, name))
                try:
                    mt = os.path.getmtime(path)
                except OSError:
                    continue
                if mt > best_mtime:
                    best, best_mtime = path, mt
    return best


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
    if index_path and not os.path.isfile(index_path):
        print("[warn] 指定的索引不存在：%s" % index_path)
        index_path = ""
    if a.index_rate > 0 and not index_path:
        # 先按「归属」精确挑，挑不到才回落到 RVC 的原规则（兼容老布局）。
        index_path = pick_index(model_name, a.speaker_id)
        if index_path:
            print("索引归属匹配：%s" % os.path.basename(index_path))
        else:
            from infer.vc.utils import get_index_path_from_model
            index_path = get_index_path_from_model(model_name, a.speaker_id)
    if a.index_rate > 0 and index_path and not os.path.isfile(index_path):
        print("[warn] 索引不可读：%s" % index_path)
        index_path = ""
    if a.index_rate > 0 and not index_path:
        print("[warn] 没有可用的 added 索引，按 index-rate 0 推理")
        a.index_rate = 0.0
        index_path = ""
    print("索引 %s" % (os.path.basename(index_path) if index_path else "（未使用）"))

    status, result = vc.vc_single(
        a.speaker_id, a.input, a.pitch, a.f0_method, index_path, a.index_rate,
        a.resample_sr, a.rms_mix_rate, a.protect)
    # ★ 兜底：索引被配错（维度不符）时 RVC 只会抛 faiss 的 assert，返回空结果，
    #   而 `_rvc` 那边按 "added index" 匹配降级 —— 这句话里没有它，于是整首
    #   歌直接判"转换失败"。这里自己救：有索引却失败，就去掉索引再试一次。
    #   少一点音色细节，总比整首唱不出来强。（2026-10-20 实测踩到）
    if (not result or result[0] is None or result[1] is None) and a.index_rate > 0:
        print("[warn] 带索引推理失败（多半是索引与模型维度不符：%s）"
              % (os.path.basename(index_path) if index_path else "-"))
        print("[warn] 去掉索引重试一次")
        a.index_rate = 0.0
        status, result = vc.vc_single(
            a.speaker_id, a.input, a.pitch, a.f0_method, "", 0.0,
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
