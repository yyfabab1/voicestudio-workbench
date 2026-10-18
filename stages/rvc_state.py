r"""
报告某个 RVC 实验已经训到第几轮。

用法（cwd = RVC 根目录，用 RVC 自己的 python 跑）：
    runtime\python.exe -s stages\rvc_state.py <实验名>

打印一行整数：已经训到的轮数（从没训过则打印 0）。失败也打印 0。

为什么需要它：RVC 的 train.py 会自动从 logs/<实验名>/G_*.pth 续训，循环是
    for epoch in range(epoch_str, hps.train.epochs + 1)
所以 `-te N` 的含义是「训到第 N 轮」，不是「再训 N 轮」。已经训到 300 之后
再传 `-te 300`，循环一次都不执行，末尾那句 `if epoch >= hps.total_epoch`
会引用未绑定的 epoch 变量直接 NameError 崩掉 —— 用户看到的是一句莫名其妙的
Python 报错。调用方先问清楚「已经多少轮」，就能把这个坑挡在提交之前。
"""
import os
import sys

if len(sys.argv) < 2:
    print(0)
    sys.exit(0)

EXP = sys.argv[1]
model_dir = os.path.join("logs", EXP)

best = 0
try:
    import torch

    d = model_dir
    cands = []
    if os.path.isdir(d):
        cands = [os.path.join(d, f) for f in os.listdir(d)
                 if f.startswith("G_") and f.endswith(".pth")]
    if cands:
        # -l 1 时 train.py 把 G/D 都固定写成 G_2333333.pth，但老实验可能留有
        # 带真实步数的文件名，所以按文件名里的数字排序取最大的那个。
        def rank(p):
            digits = "".join(ch for ch in os.path.basename(p) if ch.isdigit())
            return int(digits) if digits else 0
        p = max(cands, key=rank)
        try:
            # mmap 只映射不读全量张量，实测 0.05s vs 0.28s；这个函数会被界面
            # 反复调用（打开角色详情就问一次），差别是"点一下等一下"和"瞬间出来"。
            ck = torch.load(p, map_location="cpu", weights_only=False, mmap=True)
        except TypeError:      # 老 torch 不认 mmap
            ck = torch.load(p, map_location="cpu", weights_only=False)
        # train/utils.save_checkpoint() 的第三个位置参数名叫 iteration，但传进去的
        # 是 epoch（train.py: utils.save_checkpoint(net_g, optim_g, lr, epoch,
        # ".../G_2333333.pth")）。而且 -l 1 时文件名固定写死 2333333，
        # **文件名里读不出轮数**，只能开 ckpt 看这个字段。
        best = int(ck.get("iteration") or 0)
except Exception as e:
    sys.stderr.write("[rvc_state] %s\n" % e)

print(best)