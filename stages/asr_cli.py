r"""
ASR 命令行封装（离线优先）。

整合包自带的 tools/asr/funasr_asr.py 默认走 fun-asr-nano 后端，那个模型要联网
从 HuggingFace 拉，而本机 HF 主站不通。本脚本改走**已经在包里躺着的本地模型**
（tools/asr/models 下的 paraformer-large + fsmn-vad + punc），全程不需要联网。

用法（cwd = 整合包根目录）：
    runtime\python.exe -s <本脚本> <包根目录> <输入目录> <输出目录> <语言>

输出文件固定为 <输出目录>/<输入目录名>.list
"""
import os
import sys
import traceback


def patch_distutils():
    """
    整合包用的是便携式 Python，python312._pth 让它跑在隔离模式：site 模块不加载，
    .pth 不生效，而 Python 3.12 已经删掉了标准库 distutils。
    结果 funasr 有二十多个模型（含标点模型 CTTransformer）注册失败，
    最后报 "model 'CTTransformer' is not registered" —— 看起来像模型坏了，
    实际是少了个库。这里把 setuptools 自带的 _distutils 挂到 sys.modules 上补回去。
    必须在 import funasr 之前调用。
    """
    try:
        import distutils.version

        return
    except Exception:
        pass
    try:
        import setuptools
        import setuptools._distutils

        sys.modules["distutils"] = setuptools._distutils
        sys.modules["distutils.version"] = setuptools._distutils.version
    except Exception:
        traceback.print_exc()


patch_distutils()

if len(sys.argv) < 5:
    print("参数不足。需要: 包根目录 输入目录 输出目录 语言")
    sys.exit(2)

PKG_ROOT = os.path.abspath(sys.argv[1])
INP_DIR = os.path.abspath(sys.argv[2])
OUT_DIR = os.path.abspath(sys.argv[3])
LANG = sys.argv[4]

os.chdir(PKG_ROOT)
for p in (PKG_ROOT, os.path.join(PKG_ROOT, "tools", "asr")):
    if p not in sys.path:
        sys.path.insert(0, p)

# 关掉 modelscope 的联网下载，逼它只用本地缓存。
# 如果本地模型真的缺文件，这里会立刻报错，而不是卡在网络上半小时。
os.environ["MODELSCOPE_DOWNLOAD_MODE"] = "local"

try:
    from funasr_asr import execute_asr
except Exception:
    traceback.print_exc()
    print("[err] 无法导入 funasr_asr，请确认 cwd 是整合包根目录")
    sys.exit(2)

if not os.path.isdir(INP_DIR):
    print("[err] 输入目录不存在: %s" % INP_DIR)
    sys.exit(2)

wavs = [f for f in sorted(os.listdir(INP_DIR)) if f.lower().endswith((".wav", ".flac", ".mp3"))]
print("待识别音频: %d 个  语言: %s" % (len(wavs), LANG))
if not wavs:
    print("[err] 输入目录里没有音频文件")
    sys.exit(2)

os.makedirs(OUT_DIR, exist_ok=True)

try:
    out_path = execute_asr(
        input_folder=INP_DIR,
        output_folder=OUT_DIR,
        model_size="large",
        language=LANG,
        backend="local",  # 不是 fun-asr-nano / sensevoice，就会走本地 paraformer
    )
except Exception:
    traceback.print_exc()
    print("[err] ASR 执行失败")
    sys.exit(1)

print("=" * 60)
if out_path and os.path.exists(out_path):
    n = sum(1 for line in open(out_path, encoding="utf-8") if line.strip())
    empty = 0
    for line in open(out_path, encoding="utf-8"):
        if line.strip():
            parts = line.strip().split("|")
            if len(parts) < 4 or not parts[3].strip():
                empty += 1
    print("ASR 完成: %s" % out_path)
    print("  有标注行数: %d   空文本行: %d" % (n, empty))
    # 空文本太多说明模型没跑起来或音频全是静音，直接判失败
    if n == 0:
        print("[err] 一条都没识别出来")
        sys.exit(1)
    if empty > n * 0.5:
        print("[err] 超过一半是空文本，ASR 结果不可用")
        sys.exit(1)
    sys.exit(0)

print("[err] 没找到输出文件")
sys.exit(1)
