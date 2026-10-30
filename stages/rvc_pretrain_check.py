# -*- coding: utf-8 -*-
r"""体检 RVC 底模：文件在不在、能不能真的加载进要训的那个架构。

为什么要这个脚本：底模对不上架构时，RVC 的 load_state_dict 会报一堆
size mismatch（strict=True 时直接抛），但更坑的是**版本/采样率选错**这种
"文件在、也能建模型、但键名对不上"的情况 —— 训练照样跑，底模却等于没用，
于是又训出一个不会跟调的模型。所以它必须在开训**之前**被单独验一次。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器）：
    runtime\python.exe -s <本脚本> [版本 v1|v2] [采样率 40k|48k]

输出最后一行：
    PRETRAIN_OK <G路径>|<D路径>|missing=<n>|unexpected=<n>
退出码：0 通过；2 底模不在/参数错；3 加载不上（缺键或多余键）。
"""
import os
import sys

VER = sys.argv[1] if len(sys.argv) > 1 else "v1"
SR = sys.argv[2] if len(sys.argv) > 2 else "40k"
# 后两个参数可选：显式指定 G/D 路径。
# 存在的意义是让**验收**能拿一个故意搞错的文件来验"这把尺子会红"——
# 不然负对照就得去动真底模（改坏了整条训练链都得停）。
OVR_G = sys.argv[3] if len(sys.argv) > 3 else None
OVR_D = sys.argv[4] if len(sys.argv) > 4 else None

os.environ.setdefault("rmvpe_root", os.path.join(os.getcwd(), "assets", "rmvpe"))
if os.getcwd() not in sys.path:
    sys.path.insert(0, os.getcwd())

import torch  # noqa: E402

# 官方的取名规则（webui.py 的 get_pretrained_models）：
#   v1 -> assets/pretrained/     v2 -> assets/pretrained_v2/
#   文件名 f0G<sr>.pth / f0D<sr>.pth
SUB = "pretrained_v2" if str(VER).lower() == "v2" else "pretrained"
D = os.path.join("assets", SUB)
GP = OVR_G or os.path.join(D, "f0G%s.pth" % SR)
DP = OVR_D or os.path.join(D, "f0D%s.pth" % SR)
for p, who in ((GP, "生成器"), (DP, "判别器")):
    if not os.path.isfile(p):
        print("PRETRAIN_MISSING 底模%s不在：%s" % (who, os.path.abspath(p)))
        sys.exit(2)

# 用 configs/v1/<sr>.json 里那份架构来建模型 —— 和训练时用的是同一份 config，
# 这样验出来的"能不能加载"才是真的。
import json  # noqa: E402

cfg_path = os.path.join("configs", VER, "%s.json" % SR)
if not os.path.isfile(cfg_path):
    # 例如 v2 只有 32k/48k 的 config，没有 40k —— 那就明说"这一版没有"，
    # 而不是去猜另一个路径（猜错会拿 v1 的架构去套 v2 的底模，报一堆
    # 看不懂的 size mismatch）。
    print("PRETRAIN_MISSING 没有这一版的训练配置：%s" % os.path.abspath(cfg_path))
    sys.exit(2)
with open(cfg_path, encoding="utf-8") as f:
    hp = json.load(f)

if str(VER).lower() == "v2":
    from infer.module.models import SynthesizerTrnMs768NSFsid as Net
else:
    from infer.module.models import SynthesizerTrnMs256NSFsid as Net

# ★ 必须照 train/train.py 里的方式构造（**hps.model 关键字展开**），
#   不要自己按顺序拼位置参数 —— 我曾经把 spk_embed_dim 和 gin_channels 写反，
#   结果建出来的 emb_g 形状是 (256,109) 而底模是 (109,256)，
#   报了一屏 size mismatch，看着像"底模坏了"，其实是我拼错了。
try:
    net = Net(
        hp["data"]["filter_length"] // 2 + 1,
        hp["train"]["segment_size"] // hp["data"]["hop_length"],
        **hp["model"],
        is_half=False,
        sr=hp.get("sample_rate") or hp["data"]["sampling_rate"],
    )
except TypeError as e:
    print("PRETRAIN_CFGERR 建不出模型（config 与架构对不上）：%s" % str(e)[:200])
    sys.exit(3)

saved = torch.load(GP, map_location="cpu", weights_only=False)
# 官方 pretrain 文件是 {"model": ..., "iteration":..., "learning_rate":...}
state = saved["model"] if isinstance(saved, dict) and "model" in saved else saved
cur = net.state_dict()
# emb_g 的说话人数量可能不一样（底模是多人、我们的实验是 109）：
# RVC 自己的 load_pretrained_generator 会把能拷的行拷过去，这里照做，
# 否则 109 ≠ 底模的行数时会误报成"加载失败"。
ek = "emb_g.weight"
if ek in state and ek in cur and state[ek].shape != cur[ek].shape:
    if state[ek].dim() == cur[ek].dim() and state[ek].shape[1:] == cur[ek].shape[1:]:
        exp = cur[ek].clone()
        rows = min(state[ek].shape[0], cur[ek].shape[0])
        exp[:rows].copy_(state[ek][:rows])
        state[ek] = exp
r = net.load_state_dict(state, strict=False)
missing = [k for k in r.missing_keys if k != ek]
unexp = [k for k in r.unexpected_keys if k != ek]
if missing or unexp:
    print("PRETRAIN_MISMATCH 缺 %d 个键、多 %d 个键（例：%s / %s）"
          % (len(missing), len(unexp), (missing or ["-"])[:2], (unexp or ["-"])[:2]))
    sys.exit(3)
dsaved = torch.load(DP, map_location="cpu", weights_only=False)
if not (isinstance(dsaved, dict) and "model" in dsaved):
    print("PRETRAIN_MISMATCH 判别器底模格式不对（没有 model 键）")
    sys.exit(3)
print("PRETRAIN_OK %s|%s|missing=0|unexpected=0" % (os.path.abspath(GP), os.path.abspath(DP)))
