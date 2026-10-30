# -*- coding: utf-8 -*-
r"""量「听起来像不像这个角色」：eres2net 声纹的余弦距离。

和项目里其余地方**同一把尺子**（stages/feat_cli.py 里那份 192 维的 `sv`），
区别只是这里一次只比两条轨，不用先切一堆片。

为什么要有它：
  高频补偿的代价（"更清楚" vs "更像角色"）在文档里是几个写死的常数，
  而常数会过期 —— 换一首歌、换一个角色，那几个数字就不作数了。
  所以这里每次都**现算**，并且把「离角色素材中心」和「离原唱」两个数一起报，
  光看一个会看走眼：补得越狠，离角色越远、离原唱越近，两条曲线一起看才知道
  拐点在哪一档。

用法（cwd 必须是整合包根目录）：
    <py> -s tools/probe_svdist.py <角色参考wav> <待测wav...> [--src <原唱wav>]
"""
import os
import sys


def emb_of(model, path, sr=16000):
    import numpy as np
    import torchaudio
    w, s = torchaudio.load(path)
    if w.shape[0] > 1:
        w = w.mean(0, keepdim=True)
    if s != sr:
        w = torchaudio.functional.resample(w, s, sr)
    # 长音频切开算再平均：eres2net 是按段设计的，整首灌进去会把不同段落混在一个向量里
    seg = sr * 8
    n = w.shape[1]
    outs = []
    for st in range(0, max(1, n - seg + 1), seg):
        piece = w[:, st: st + seg]
        if piece.shape[1] < sr * 2:
            continue
        with __import__("torch").no_grad():
            outs.append(model(piece.cuda()).squeeze().cpu().numpy())
    if not outs:
        return None
    v = np.mean(np.stack(outs), axis=0)
    return v / (float(np.linalg.norm(v)) + 1e-9)


def cos(a, b):
    return 1.0 - float(a @ b)


def main():
    args, src = [], None
    av = sys.argv[1:]
    i = 0
    while i < len(av):
        if av[i] == "--src" and i + 1 < len(av):
            src = av[i + 1]
            i += 2
        else:
            args.append(av[i])
            i += 1
    if len(args) < 2:
        raise SystemExit("用法： probe_svdist.py <角色参考wav> <待测wav...> [--src 原唱]")
    # ★ 必须在 chdir **之前**把路径转绝对：下面要切到 GPT-SoVITS 根目录才能
    #   import 它的 sv 模块，切过去之后相对路径就全部失效了（踩到才知道）。
    args = [os.path.abspath(p) for p in args]
    if src:
        src = os.path.abspath(src)

    # ★ eres2net 装在 **GPT-SoVITS 那边**，不在 RVC 根目录下。
    #   在 RVC 根下 import 会 ModuleNotFoundError: speakerlab —— 而错误信息
    #   长得像"eres2net 没装好"，其实只是找错了根目录（和 studio.py 的 GSV_ROOT 对齐即可）。
    ROOT = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0"
    os.chdir(ROOT)
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)
    sys.path.append(os.path.join(ROOT, "GPT_SoVITS", "eres2net"))
    import kaldi as Kaldi       # noqa: E402  eres2net 的 fbank 前端
    from GPT_SoVITS.sv import SV

    device = "cuda"
    sv = SV(device, True)       # 内部自己加载 pretrained_eres2netv2w24s4ep4.ckpt

    def _one(path):
        import numpy as np
        import torch
        import torchaudio
        w, s = torchaudio.load(path)
        if w.shape[0] > 1:
            w = w.mean(0, keepdim=True)
        if s != 16000:
            w = torchaudio.functional.resample(w, s, 16000)
        # 长音频切 8 秒段再平均：整首灌进去会把不同段落混进一个向量
        seg = 16000 * 8
        vs = []
        for st in range(0, max(1, w.shape[1] - seg + 1), seg):
            p = w[:, st: st + seg]
            if p.shape[1] < 16000 * 2:
                continue
            with torch.no_grad():
                # ★ fbank 要的是 (1, N)。p 已经是 (1, N)（前面 mean 后没降维），
                #   再 unsqueeze(0) 就成 (1,1,N) —— 它按第 0 维取 len ⇒ 报
                #   "choose a window size 400 that is [2, 1]"，看着像窗长设错了，
                #   其实是把一个三维张量塞进去了。
                _pad = torch.nn.functional.pad(p, (0, max(0, seg - p.shape[1])))
                # fbank 收 (1, N) 吐 (T, F)；模型要 (1, T, F) ⇒ 还得 unsqueeze 一次。
                _f = Kaldi.fbank(_pad.reshape(1, -1),
                                 num_mel_bins=80, sample_frequency=16000, dither=0
                                 ).unsqueeze(0)
                if sv.is_half:
                    _f = _f.half()
                vs.append(sv.embedding_model.forward(_f.to(device)))
        if not vs:
            return None
        v = np.mean(np.stack([x.float().cpu().numpy().reshape(-1) for x in vs]), axis=0)
        return v / (float(np.linalg.norm(v)) + 1e-9)

    ref = _one(args[0])
    srcv = _one(src) if src else None
    print("%-40s %10s %10s" % ("文件", "→角色", "→原唱"))
    for p in args[1:]:
        v = _one(p)
        if v is None:
            print("%-40s  （太短，量不出来）" % os.path.basename(p)[:40])
            continue
        f1 = "%.4f" % cos(ref, v)
        f2 = ("%.4f" % cos(srcv, v)) if srcv is not None else "  -   "
        print("%-40s %10s %10s" % (os.path.basename(p)[:40], f1, f2))


if __name__ == "__main__":
    main()
