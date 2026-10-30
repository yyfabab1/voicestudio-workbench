# -*- coding: utf-8 -*-
r"""
UVR5 命令行封装。

GPT-SoVITS 整合包里的 UVR5 只有 gradio 界面（tools/uvr5/webui.py），没法在
无人值守的流水线里调用。本脚本复用它底层同一个类（vr.AudioPre /
vr.AudioPreDeEcho / mdxnet.MDXNetDereverb / bsroformer.Roformer_Loader），
把「输入目录 -> 人声目录 + 伴奏目录」做成一条命令。

用法（cwd 必须是整合包根目录）：
    runtime\python.exe -s <本脚本> <包根目录> <模型名> <输入目录> \
        <人声输出目录> <伴奏输出目录> <agg> <格式> <设备> <是否半精度>

例：
    runtime\python.exe -s stages\uvr_cli.py . HP5_only_main_vocal \
        in\audio out\vocal out\inst 10 wav cuda True

退出码：0 全部成功；1 有文件失败；2 参数或环境错误。
"""
import os
import sys
import traceback

if len(sys.argv) < 10:
    print("参数不足。需要: 包根目录 模型名 输入目录 人声目录 伴奏目录 agg 格式 设备 is_half")
    sys.exit(2)

PKG_ROOT = os.path.abspath(sys.argv[1])
MODEL_NAME = sys.argv[2]
INP_DIR = os.path.abspath(sys.argv[3])
VOCAL_DIR = os.path.abspath(sys.argv[4])
INS_DIR = os.path.abspath(sys.argv[5])
AGG = int(sys.argv[6])
FORMAT0 = sys.argv[7]
DEVICE = sys.argv[8]
IS_HALF = sys.argv[9].strip().lower() in ("true", "1", "yes")

# uvr5 内部用相对路径找权重，且要 import vr / bsroformer / mdxnet
os.chdir(PKG_ROOT)
for p in (PKG_ROOT, os.path.join(PKG_ROOT, "tools", "uvr5")):
    if p not in sys.path:
        sys.path.insert(0, p)

WEIGHT_ROOT = os.path.join("tools", "uvr5", "uvr5_weights")

# ★ ffmpeg 必须**给绝对路径**，不能靠 PATH：
#   这台机器的机器级 PATH 被人改坏过（%SystemRoot% 整组缺失），裸名调 exe 会
#   ENOENT；而整合包自己带着一份 ffmpeg（runtime/ffmpeg.exe），用它最稳。
#   症状长得很像"音频坏了"：ffmpeg 转码失败 → 分离 0 成功 → 整条链挂掉，
#   而报错里只有一句看不清编码的"不是内部或外部命令"。
FFMPEG = os.path.join(PKG_ROOT, "runtime", "ffmpeg.exe")
if not os.path.isfile(FFMPEG):
    FFMPEG = os.environ.get("FFMPEG_BIN") or "ffmpeg"


def build_predictor():
    """按模型名选择对应的分离器，返回 (实例, 备注列表)。"""
    name = MODEL_NAME
    infos = []
    if name == "onnx_dereverb_By_FoxJoy":
        from mdxnet import MDXNetDereverb

        return MDXNetDereverb(15), infos

    if "roformer" in name.lower():
        from bsroformer import Roformer_Loader

        ckpt = os.path.join(WEIGHT_ROOT, name + ".ckpt")
        cfg = os.path.join(WEIGHT_ROOT, name + ".yaml")
        if not os.path.exists(cfg):
            infos.append(
                "[warn] 缺少 %s 的配置文件，将使用默认配置，不保证该模型能跑通。" % (name + ".yaml")
            )
        return (
            Roformer_Loader(model_path=ckpt, config_path=cfg, device=DEVICE, is_half=IS_HALF),
            infos,
        )

    from vr import AudioPre, AudioPreDeEcho

    cls = AudioPreDeEcho if "DeEcho" in name else AudioPre
    path = os.path.join(WEIGHT_ROOT, name + ".pth")
    if not os.path.exists(path):
        raise FileNotFoundError("找不到权重: %s" % os.path.abspath(path))
    return cls(agg=AGG, model_path=path, device=DEVICE, is_half=IS_HALF), infos


def _reformat_py(inp_path, out_path):
    """不依赖 ffmpeg 的转码兜底（soundfile + scipy）。

    为什么要兜底：转码这一步以前**只**靠 ffmpeg 命令行，一旦它没跑通，症状是
    「UVR5 成功 0 个」—— 看不出是音频坏了、格式不对、还是**ffmpeg 本身**没起来
    （本机 PATH 被改坏过，裸名调 exe 会 ENOENT）。多一条纯 Python 的路，
    这类"环境问题"就不再伪装成"素材问题"。
    """
    import numpy as np
    import soundfile as sf
    from scipy.signal import resample_poly

    a, sr = sf.read(inp_path)
    if a.ndim == 1:
        a = np.stack([a, a], axis=1)
    elif a.shape[1] == 1:
        a = np.repeat(a, 2, axis=1)
    elif a.shape[1] > 2:
        a = a[:, :2]
    if sr != 44100:
        g = __import__("math").gcd(int(sr), 44100)
        a = resample_poly(a, 44100 // g, int(sr) // g, axis=0)
    sf.write(out_path, a, 44100, subtype="PCM_16")
    return os.path.isfile(out_path)


def reformat_if_needed(inp_path, tmp_dir):
    """UVR5 原版要求输入是 2 声道 44100Hz，不是就先转一次。

    两条路：先用 ffmpeg（跟原版一致），它没跑通就用纯 Python 转（见 _reformat_py）。
    两条都失败才返回 None —— 并且把 ffmpeg 的错误输出**打出来**，否则排查时
    只能看到一句"转码失败"。
    """
    need = 1
    try:
        import ffmpeg

        info = ffmpeg.probe(inp_path, cmd="ffprobe")
        st = info["streams"][0]
        if st["channels"] == 2 and st["sample_rate"] == "44100":
            need = 0
    except Exception:
        need = 1

    if need == 0:
        return inp_path

    tmp_path = os.path.join(tmp_dir, os.path.basename(inp_path) + ".reformatted.wav")
    cmd = '"%s" -i "%s" -vn -acodec pcm_s16le -ac 2 -ar 44100 "%s" -y' % (FFMPEG, inp_path, tmp_path)
    try:
        import subprocess

        p = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if p.returncode == 0 and os.path.exists(tmp_path):
            return tmp_path
        print("  [warn] ffmpeg 转码没成（%s），改用纯 Python 转码：%s"
              % (FFMPEG, ((p.stderr or p.stdout or "")[-160:]).replace("\n", " ")))
    except Exception as e:
        print("  [warn] ffmpeg 调不起来（%s），改用纯 Python 转码" % str(e)[:80])
    try:
        if _reformat_py(inp_path, tmp_path):
            return tmp_path
    except Exception as e:
        print("[err] 纯 Python 转码也失败：%s" % str(e)[:160])
    return None


def main():
    if not os.path.isdir(INP_DIR):
        print("[err] 输入目录不存在: %s" % INP_DIR)
        sys.exit(2)

    tmp_dir = os.path.join(os.environ.get("TEMP", "."), "_ahc_uvr")
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(VOCAL_DIR, exist_ok=True)
    os.makedirs(INS_DIR, exist_ok=True)

    try:
        pre_fun, infos = build_predictor()
    except Exception:
        print("[err] 分离器加载失败:")
        traceback.print_exc()
        sys.exit(2)

    for msg in infos:
        print(msg)

    names = sorted(os.listdir(INP_DIR))
    ok, fail = [], []

    for name in names:
        src = os.path.join(INP_DIR, name)
        if not os.path.isfile(src):
            continue
        print("处理: %s" % name)
        sys.stdout.flush()
        try:
            target = reformat_if_needed(src, tmp_dir)
            if target is None:
                fail.append((name, "ffmpeg 转码失败"))
                continue
            pre_fun._path_audio_(target, INS_DIR, VOCAL_DIR, FORMAT0, "HP3" in MODEL_NAME)
            ok.append(name)
            print("  -> 完成")
        except Exception:
            fail.append((name, traceback.format_exc()))
            print("  -> 失败")
        sys.stdout.flush()

    try:
        del pre_fun
    except Exception:
        pass
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass

    print("=" * 60)
    print("UVR5 成功 %d 个，失败 %d 个" % (len(ok), len(fail)))
    print("人声目录: %s" % VOCAL_DIR)
    print("伴奏目录: %s" % INS_DIR)
    for n, why in fail:
        print("  [失败] %s : %s" % (n, str(why).splitlines()[-1] if str(why) else ""))
    print("=" * 60)

    sys.exit(0 if not fail else 1)


if __name__ == "__main__":
    main()
