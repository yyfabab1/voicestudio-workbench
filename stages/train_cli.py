r"""
GPT 训练命令行封装。

等价于在 WebUI 的 1B-微调训练 页点「开启GPT训练」，但参数可以从命令行传，
供无人值守的流水线调用。v5 不需要训 SoVITS，只训 GPT。

用法（cwd = 整合包根目录）：
    runtime\python.exe -s <本脚本> <包根目录> <实验名> <版本> <batch> <轮数> <workers> <每N轮存>

例：
    runtime\python.exe -s stages\train_cli.py . myvoice v5turbo 4 15 2 5
"""
import os
import shutil
import subprocess
import sys
import time

import yaml

if len(sys.argv) < 8:
    print("参数不足。需要: 包根目录 实验名 版本 batch 轮数 workers 每N轮存")
    sys.exit(2)

PKG_ROOT = os.path.abspath(sys.argv[1])
EXP_NAME = sys.argv[2]
VERSION = sys.argv[3]
BATCH_SIZE = int(sys.argv[4])
TOTAL_EPOCH = int(sys.argv[5])
NUM_WORKERS = int(sys.argv[6])
SAVE_EVERY = int(sys.argv[7])

os.chdir(PKG_ROOT)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)

IS_HALF = True
PRETRAINED_S1 = "GPT_SoVITS/pretrained_models/s1v3.ckpt"
GPT_WEIGHT_ROOT = {
    "v1": "GPT_weights",
    "v2": "GPT_weights_v2",
    "v3": "GPT_weights_v3",
    "v4": "GPT_weights_v4",
    "v5dev": "GPT_weights_v5dev",
    "v5turbo": "GPT_weights_v5turbo",
    "v2Pro": "GPT_weights_v2Pro",
    "v2ProPlus": "GPT_weights_v2ProPlus",
}

# 三个必须存在的前置产物，缺一个就说明数据集没准备好，别浪费时间跑训练
s1_dir = os.path.join("logs", EXP_NAME)
required = [
    os.path.join(s1_dir, "6-name2semantic.tsv"),
    os.path.join(s1_dir, "2-name2text.txt"),
    PRETRAINED_S1,
]
missing = [r for r in required if not os.path.exists(r)]
if missing:
    print("[err] 缺少训练前置产物，请先跑完 1A/1B/1C:")
    for m in missing:
        print("   -", m)
    sys.exit(2)

tmp = os.path.join(PKG_ROOT, "TEMP")
os.makedirs(tmp, exist_ok=True)
os.makedirs(os.path.join(s1_dir, "logs_s1"), exist_ok=True)

with open("GPT_SoVITS/configs/s1longer-v2.yaml", "r", encoding="utf-8") as f:
    data = yaml.load(f.read(), Loader=yaml.FullLoader)

if not IS_HALF:
    data["train"]["precision"] = "32"
    BATCH_SIZE = max(1, BATCH_SIZE // 2)

data["train"]["batch_size"] = BATCH_SIZE
data["train"]["epochs"] = TOTAL_EPOCH
# Windows 下 spawn 的 dataloader worker 各自占内存，而 lightning 存 ckpt 时
# 会先把整个状态字典写进内存 BytesIO 再落盘，峰值约 2 倍。worker 太多会
# 在存盘那一刻 MemoryError（实测踩过）。
data["data"]["num_workers"] = NUM_WORKERS
data["pretrained_s1"] = PRETRAINED_S1
data["train"]["save_every_n_epoch"] = SAVE_EVERY
data["train"]["if_save_every_weights"] = True
data["train"]["if_save_latest"] = True
data["train"]["if_dpo"] = False
data["train"]["half_weights_save_dir"] = GPT_WEIGHT_ROOT[VERSION]
data["train"]["exp_name"] = EXP_NAME
data["train_semantic_path"] = "%s/6-name2semantic.tsv" % s1_dir
data["train_phoneme_path"] = "%s/2-name2text.txt" % s1_dir
data["output_dir"] = "%s/logs_s1_%s" % (s1_dir, VERSION)

# lightning 启动时，如果 output_dir 里已经躺着上次的 last.ckpt，它会试图从那里续训；
# 而 torch 2.6 之后 torch.load 默认 weights_only=True，不接受 pathlib.WindowsPath，
# 于是任何一次「重跑」都会以 UnpicklingError 崩掉。本流水线每次都是从头训，不需要续训。
#
# 这里只删 ckpt 目录里的 .ckpt（lightning 认的就是它），**保留目录结构**。
# 以前是把整个目录 shutil.move 成 *_bak_<时间戳>，看着"安全"，实际是纯浪费：
# 一个 epoch=4-step=15.ckpt 就889MB，而最终权重另存在 GPT_weights_v5turbo/
# （每个 155MB，真正要用的在那里）。备份这些中间产物既救不了场，又能把磁盘吃满——
# 磁盘满之后训练恰好又死在写 ckpt 上，等于自己给自己下套。
#
# 目录名必须是 **ckpt**，不是 checkpoints：s1_train.py 里写的是
#     ckpt_dir = output_dir / "ckpt"
#     my_model_ckpt(..., dirpath=ckpt_dir)
# 早先这里找的是 checkpoints，所以清理**从来没生效过**，每次重训都把上一轮的
# 888MB ckpt 留在原地（日志目录里那些 ckpt/ 就是这么攒出来的）。顺手也认
# checkpoints，万一以后 lightning 改了名字，两个都清。
output_dir = "%s/logs_s1_%s" % (s1_dir, VERSION)
if os.path.isdir(output_dir):
    n = 0
    freed = 0
    for ck_name in ("ckpt", "checkpoints"):
        ck_dir = os.path.join(output_dir, ck_name)
        if not os.path.isdir(ck_dir):
            continue
        for f in os.listdir(ck_dir):
            if f.endswith(".ckpt"):
                p = os.path.join(ck_dir, f)
                try:
                    freed += os.path.getsize(p)
                    os.remove(p)
                    n += 1
                except OSError:
                    pass
    if n:
        print("[info] 清掉 %d 个旧 ckpt 中间产物，释放 %.1f MB"
              % (n, freed / 1048576.0))
        print("[info] （最终权重保存在 GPT_weights_v5turbo/，不受影响）")
os.makedirs(output_dir, exist_ok=True)

os.environ["_CUDA_VISIBLE_DEVICES"] = "0"
os.environ["hz"] = "25hz"

tmp_config_path = os.path.join(tmp, "tmp_s1.yaml")
with open(tmp_config_path, "w", encoding="utf-8") as f:
    f.write(yaml.dump(data, default_flow_style=False))

print("=" * 60)
print("实验名      :", EXP_NAME)
print("版本        :", VERSION)
print("batch_size  :", BATCH_SIZE)
print("epochs      :", TOTAL_EPOCH)
print("num_workers :", NUM_WORKERS)
print("权重输出目录:", GPT_WEIGHT_ROOT[VERSION])
print("=" * 60)
sys.stdout.flush()

cmd = '"%s" -s GPT_SoVITS/s1_train.py --config_file "%s"' % (sys.executable, tmp_config_path)
print("CMD:", cmd)
sys.stdout.flush()

rc = subprocess.call(cmd, shell=True)
print("=" * 60)
print("s1_train.py 退出码:", rc)
sys.exit(rc)
