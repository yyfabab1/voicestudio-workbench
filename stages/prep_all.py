r"""
数据集格式化（1A / 1B / 1C）合并版。

原来这三步是三个独立进程，每起一次都要重新 import 一次 torch 再初始化 CUDA，
实测各步耗时 16.7s / 20.0s / 11.1s，其中相当一部分是重复的启动开销而不是在算东西。
三个脚本本身只是在模块顶层读 os.environ 然后干活（没有 main / 没有 sys.exit），
所以可以用 runpy 在同一个进程里依次跑完，模型加载各自只做一次。

合并后还要补一件事：1A/1B/1C 只产出分片文件（-0 结尾），
「把分片并成一个完整文件」这一步原本是 WebUI 干的，命令行没有，必须自己补。

用法（cwd = 整合包根目录）：
    runtime\python.exe -s <本脚本> <包根目录> <实验名> <标注list> <切片目录>

退出码 0 = 三步都通过且体检合格。
"""
import glob
import os
import runpy
import sys
import traceback
import wave

if len(sys.argv) < 5:
    print("参数不足。需要: 包根目录 实验名 标注list 切片目录")
    sys.exit(2)

PKG_ROOT = os.path.abspath(sys.argv[1])
EXP_NAME = sys.argv[2]
LIST_PATH = os.path.abspath(sys.argv[3])
SLICE_DIR = os.path.abspath(sys.argv[4])

os.chdir(PKG_ROOT)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

OPT_DIR = os.path.join(PKG_ROOT, "logs", EXP_NAME)
os.makedirs(OPT_DIR, exist_ok=True)

COMMON = {
    "inp_text": LIST_PATH,
    "inp_wav_dir": SLICE_DIR,
    "exp_name": EXP_NAME,
    "opt_dir": OPT_DIR,
    "i_part": "0",
    "all_parts": "1",
    "_CUDA_VISIBLE_DEVICES": "0",
    "is_half": "True",
}


def merge_shards(pattern, final_name, header=None):
    shards = sorted(glob.glob(os.path.join(OPT_DIR, pattern)))
    if not shards:
        return 0
    buf = []
    if header:
        buf.append(header)
    for s in shards:
        with open(s, "r", encoding="utf-8") as f:
            txt = f.read().strip("\n")
        if txt:
            buf += txt.split("\n")
        os.remove(s)
    with open(os.path.join(OPT_DIR, final_name), "w", encoding="utf-8") as f:
        f.write("\n".join(buf) + "\n")
    return len([b for b in buf if b.strip()])


def wav_seconds(path):
    try:
        w = wave.open(path, "r")
        s = w.getnframes() / float(w.getframerate())
        w.close()
        return s
    except Exception:
        return 0.0


def run_step(tag, script, extra):
    print("")
    print("=" * 60)
    print("  %s  %s" % (tag, script))
    print("=" * 60)
    sys.stdout.flush()
    os.environ.update(COMMON)
    os.environ.update(extra)
    try:
        runpy.run_path(script, run_name="__not_main__")
    except SystemExit as e:
        if e.code not in (0, None):
            raise
    except Exception:
        traceback.print_exc()
        print("[err] %s 失败" % tag)
        sys.exit(1)


def main():
    # ---- 1A：文本特征 ----
    run_step("1A 文本特征", "GPT_SoVITS/prepare_datasets/1-get-text.py",
             {"bert_pretrained_dir": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large"})
    n_text = merge_shards("2-name2text-*.txt", "2-name2text.txt")
    bert_dir = os.path.join(OPT_DIR, "3-bert")
    n_bert = len(os.listdir(bert_dir)) if os.path.isdir(bert_dir) else 0
    print("  文本条目 %d，bert 特征 %d" % (n_text, n_bert))
    if n_text == 0:
        print("[err] 1A 没产出文本特征")
        sys.exit(1)
    if n_bert < n_text:
        print("[err] bert 特征数 %d 少于文本条目 %d，1A 产物不完整" % (n_bert, n_text))
        sys.exit(1)

    # ---- 1B：SSL 特征 + 32k 音频 ----
    run_step("1B SSL特征", "GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py",
             {"cnhubert_base_dir": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
              "sv_path": "GPT_SoVITS/pretrained_models/sv/pretrained_eres2netv2w24s4ep4.ckpt"})
    hub = os.path.join(OPT_DIR, "4-cnhubert")
    w32 = os.path.join(OPT_DIR, "5-wav32k")
    n_hub = len(os.listdir(hub)) if os.path.isdir(hub) else 0
    n_w32 = len(os.listdir(w32)) if os.path.isdir(w32) else 0
    print("  cnhubert 特征 %d，32k 音频 %d" % (n_hub, n_w32))
    if n_hub == 0 or n_w32 == 0:
        print("[err] 1B 没产出特征/音频")
        sys.exit(1)
    if n_hub < n_text or n_w32 < n_text:
        print("[err] 1B 产物不足（特征 %d / 音频 %d，应有 %d）" % (n_hub, n_w32, n_text))
        sys.exit(1)

    # ---- 1C：语义 token ----
    run_step("1C 语义token", "GPT_SoVITS/prepare_datasets/3-get-semantic.py",
             {"pretrained_s2G": "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth",
              "s2config_path": "GPT_SoVITS/configs/s2.json"})
    n_sem = merge_shards("6-name2semantic-*.tsv", "6-name2semantic.tsv",
                         header="item_name\tsemantic_audio")
    print("  语义条目 %d" % n_sem)
    if n_sem == 0:
        print("[err] 1C 没产出语义 token")
        sys.exit(1)

    # ---- 体检：token 数应该约等于 音频时长 x 25Hz ----
    # 差太远说明 token 抽错了（比如模型没加载对），继续训练只会浪费时间。
    total_dur = 0.0
    if os.path.isdir(w32):
        for f in os.listdir(w32):
            total_dur += wav_seconds(os.path.join(w32, f))
    tok = 0
    with open(os.path.join(OPT_DIR, "6-name2semantic.tsv"), encoding="utf-8") as f:
        for l in f:
            p = l.rstrip("\n").split("\t")
            if len(p) == 2:
                tok += len(p[1].split())
    expect = total_dur * 25
    ratio = tok / expect if expect > 0 else 0
    print("  总 token %d，按 25Hz 应约 %d，比例 %.3f" % (tok, int(expect), ratio))
    if not (0.85 <= ratio <= 1.15):
        print("[err] 语义 token 数量对不上（比例 %.3f，正常 0.85~1.15）" % ratio)
        sys.exit(1)

    print("")
    print("PREP_OK %d 段，%.1f 秒，token 比例 %.3f" % (n_text, total_dur, ratio))
    sys.exit(0)


if __name__ == "__main__":
    main()
