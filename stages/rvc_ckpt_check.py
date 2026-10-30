# -*- coding: utf-8 -*-
r"""验一个 RVC 歌声模型（.pth）**是不是真的能用**。

为什么要单独验：从网上下的权重可能是
  · 训练到一半的（只有 G 没有 D，或者根本没存 config）；
  · 别的分支/别的采样率训的（32k / 40k / 48k 混着）；
  · 干脆是个压缩包里带错的文件（几十 KB 的 README 也敢叫 .pth）。
区别对待很重要：文件在 ≠ 能加载 ≠ 能唱。装进曲库/角色之前先量一次，
免得用户点「唱歌」时炸在一个看不懂的 torch 报错上。

这里**只做 torch.load 和看键**，不建完整架构：建架构要挑 version/采样率，
挑错就会报一堆 size mismatch，那是"我的猜测错了"而不是"这个文件坏了"；
而看键能干净地回答"它到底是什么"。

用法（RVC 的解释器，cwd 随意）：
    runtime\python.exe -s <本脚本> <权重路径>
输出：CKPT_OK {"keys":[...], "sr":..., "f0":..., "version":..., "emb":[...]}
退出码：0 能读；2 参数/文件不在；3 读不出来（不是 torch 权重）。
"""
import json
import os
import sys

if len(sys.argv) < 2:
    print("CKPT_ERR 没给权重路径")
    sys.exit(2)
P = sys.argv[1]
if not os.path.isfile(P):
    print("CKPT_ERR 文件不在：%s" % P)
    sys.exit(2)
if os.path.getsize(P) < 50 * 1024:
    # RVC v2 的 40k 权重最小也在 60MB 上下；几十 KB 的绝不是权重
    print("CKPT_ERR 只有 %d 字节，不是 RVC 权重（可能是说明文件或下载失败的残页）"
          % os.path.getsize(P))
    sys.exit(2)

try:
    import torch

    d = torch.load(P, map_location="cpu")
except Exception as e:
    print("CKPT_ERR 读不出来（%s）：%s" % (type(e).__name__, str(e)[:160]))
    sys.exit(3)

if not isinstance(d, dict):
    print("CKPT_ERR 不是 RVC 的权重格式（顶层不是 dict，是 %s）" % type(d).__name__)
    sys.exit(3)

w = d.get("weight") or {}
if not isinstance(w, dict) or not w:
    print("CKPT_ERR 里面没有 weight（键只有 %s），不是 RVC 权重" % list(d.keys())[:6])
    sys.exit(3)

emb = w.get("emb_g.weight")
shape = list(emb.shape) if hasattr(emb, "shape") else None
info = {"keys": list(d.keys())[:10], "n_weight": len(w),
        "sr": d.get("sr"), "f0": d.get("f0"), "version": d.get("version"),
        "emb": shape, "mb": round(os.path.getsize(P) / 1048576.0, 1)}
print("CKPT_OK " + json.dumps(info, ensure_ascii=False))
sys.exit(0)
