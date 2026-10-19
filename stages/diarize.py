r"""
声音分离：把一堆切片按「是谁的声音」分成若干角色。

用整合包自带的 eres2net 声纹做余弦距离 + 层次聚类，不额外装东西。

两个关键设计，都是被实验数据逼出来的：

1) 质量门控。
   实测：56 段同人素材里只有一段 0.97 秒的短片段，它一个人贡献了 34 对
   「同人却距离 >0.6」和 2 对「异人却距离 <0.15」，把分离裕度从可用打成 0.028。
   短片段的声纹本身就不稳定（不足 1 秒的音频 eres2net 提取出来的向量几乎是噪声），
   让它参与聚类等于往里扔随机数。所以先按「时长 + 有声比例」筛出可信片段做聚类，
   剩下的片段最后再按最近的中心挂上去，挂不上就标为待定，不硬塞。

2) 阈值自适应。
   不同素材的距离尺度不一样，写死阈值迟早翻车。段数够多时用 silhouette
   在 2..min(8, n//4) 个簇里挑最合理的那个；段数太少就没得选，用实验标定的默认值。

用法：被上层 import，也可以直接跑来看结果：
    runtime\python.exe -s diarize.py <特征json> [输出json]
"""
import json
import os
import sys

import numpy as np

DEFAULT_THRESHOLD = 0.51
MIN_DUR = 2.0          # 短于 2 秒的片段声纹不可信，不参与聚类
MIN_VOICED = 0.30      # 几乎没声音的片段同理

# 分裂判据的两个门槛，都是量出来的不是猜的（data/exp_split_gap.py）：
#   55 段同一个人强行分 2 类 -> 簇大小 [54,1]，两中心距离 0.1502
#   55+55 段两个人分 2 类    -> 簇大小 [55,55]，两中心距离 0.4786
# 也就是说「同一个人的内部分裂」表现为：某个簇只有一两段（离群点自己一堆），
# 且两堆中心挨得很近。真两个人则是：两堆规模相当、中心离得远。
# 所以同时要求「最小簇够大」和「中心离得够远」，两条都满足才承认这是一次真分裂。
MIN_CENTER_DIST = 0.30     # 上面两个数的中点附近，取偏保守的一侧
MIN_CLUSTER_FRAC = 0.03    # 任一角色至少占 3%
MIN_CLUSTER_SIZE = 3       # 且至少 3 段
# 相邻切片相似度高于它才并成一组（时间聚合的门槛）。
# 0.82 是在端到端数据上扫出来的：0.72 -> 准确率 0.846，0.82 -> 0.962，0.85 -> 分不出人。
# 注意这是个窄峰，左右各 0.03 就明显变差，说明它对数据比较敏感 ——
# 所以界面上留了「手动指定人数」的口子，不把自动判断当唯一答案。
GROUP_SIM = 0.82


def _norm(E):
    E = np.asarray(E, dtype=np.float64)
    return E / (np.linalg.norm(E, axis=1, keepdims=True) + 1e-12)


def quality_ok(r):
    return r.get("dur", 0) >= MIN_DUR and r.get("voiced", 0) >= MIN_VOICED


def split_is_real(E, lab, weights=None):
    """
    判断这一次分裂是不是真的分出了两个不同的人。

    不接受两种假分裂：
      1) 某个簇只有一两段 —— 那是离群片段自己抱团，不是第二个说话人；
      2) 两堆中心挨得太近 —— 那是同一个人的内部波动。

    weights 是每条样本代表几个原始片段（时间聚合之后一组可能含多段），
    判「簇够不够大」时按片段数算，而不是按组数算。
    """
    ks = sorted(set(int(x) for x in lab))
    if len(ks) < 2:
        return False, "只有一个簇"
    n = len(lab)
    w = list(weights) if weights is not None else [1] * n
    sizes = [int(sum(w[i] for i in range(n) if lab[i] == c)) for c in ks]
    floor = max(MIN_CLUSTER_SIZE, int(MIN_CLUSTER_FRAC * n))
    if min(sizes) < floor:
        return False, "最小簇 %d 段 < 门槛 %d 段（簇大小 %s）" % (min(sizes), floor, sizes)

    cs = []
    for c in ks:
        v = E[lab == c].mean(axis=0)
        cs.append(v / (np.linalg.norm(v) + 1e-12))
    C = np.asarray(cs)
    D = 1.0 - C @ C.T
    iu = np.triu_indices(len(ks), 1)
    dmin = float(D[iu].min())
    if dmin < MIN_CENTER_DIST:
        return False, "最近的两簇中心距离 %.4f < %.2f" % (dmin, MIN_CENTER_DIST)
    return True, "簇大小 %s，最近两簇中心距离 %.4f" % (sizes, dmin)


def choose_k(E, max_k=6, weights=None):
    """
    枚举簇数，取「分裂站得住脚」的最大 k。

    一开始用的是 silhouette 自动选，结果把同一个人也拆成了 2 个角色
    （silhouette 只看簇内紧不紧，不看两堆差多少，离群片段单独成簇它反而觉得挺好）。
    改成现在的做法：逐一检查分裂是否真实，取能站住的最大分裂数。
    """
    from sklearn.cluster import AgglomerativeClustering

    n = E.shape[0]
    if n < 4:
        return 1, []

    trace = []
    best_k = 1
    upper = min(max_k, n // 4 if n >= 8 else 2)
    for k in range(2, upper + 1):
        try:
            lab = AgglomerativeClustering(n_clusters=k, metric="cosine", linkage="average").fit_predict(E)
        except Exception as e:
            trace.append((k, False, "聚类失败 %s" % e))
            continue
        ok, why = split_is_real(E, lab, weights=weights)
        trace.append((k, ok, why))
        if ok:
            best_k = k
        # 一旦某个 k 分裂不成立，更大的 k 只会切得更碎，不用再试
        else:
            break
    return best_k, trace


def parse_start(fname):
    """
    切片文件名里带着它在原音频里的采样点偏移，例如
        vocal_xxx_0000000000_0000158080.wav
    取出来就能知道这段在时间轴上排第几。取不到就返回 None（交给文件名排序兜底）。
    """
    import re

    m = re.findall(r"_(\d+)_(\d+)\.(?:wav|flac|mp3)$", fname.lower())
    return int(m[0][0]) if m else None


def temporal_groups(E, order, sim_thr=None):
    # 默认参数在定义时就绑定了 GROUP_SIM 当时的值，后面改模块变量不会生效
    # （踩过：扫参数时 11 个阈值跑出完全一样的结果）。所以留 None，进来再取。
    """
    把时间上相邻、听起来又是同一个人的切片并成一组。

    为什么要这一步：走完真实链路（有损压缩 + 人声分离）之后，
    单个切片的声纹噪声明显变大 —— 实测同一人的两两距离能到 0.56，
    而两个人的中心距离只有 0.32，单看一个切片根本分不清。
    但视频里同一个人通常是连着说好几段，把相邻且相似的片段平均一下，
    噪声就被压下去，两个人的中心也重新拉开。

    合并条件是「相邻两段的声纹相似度 >= sim_thr」，所以两个人交替说话时
    不会错误地被并到一起（不同音色相似度低于这个阈值就断开）。
    """
    if sim_thr is None:
        sim_thr = GROUP_SIM
    groups = []
    cur = [order[0]]
    cur_v = E[order[0]].copy()
    for i in order[1:]:
        if float(E[i] @ cur_v) >= sim_thr:
            cur.append(i)
            cur_v = E[cur].mean(axis=0)
            cur_v /= (np.linalg.norm(cur_v) + 1e-12)
        else:
            groups.append(list(cur))
            cur = [i]
            cur_v = E[i].copy()
    groups.append(list(cur))
    return groups


def cluster(feats, n_speakers=None, min_dur=MIN_DUR, min_voiced=MIN_VOICED, use_time=True):
    """
    输入：feat_cli.py 产出的特征列表（每项要有 emb / dur / voiced）
    输出：dict
        labels      每个片段的角色编号，-1 = 没参与聚类（低质量且挂不上任何角色）
        centers     每个角色的平均声纹（归一化）
        core        每个片段是否是参与聚类的可信片段
        k           分出的角色数
        trace       簇数判定过程（调试用）
    """
    E = _norm([r["emb"] for r in feats])
    n = len(feats)

    core = [r.get("dur", 0) >= min_dur and r.get("voiced", 0) >= min_voiced for r in feats]
    idx = [i for i in range(n) if core[i]]
    labels = [-1] * n

    if len(idx) < 2:
        # 可信片段太少，分不了，全都归给角色 0
        return dict(labels=[0] * n, centers=[list(E.mean(axis=0))], core=core, k=1, trace=[])

    Ec = E[idx]
    from sklearn.cluster import AgglomerativeClustering

    # 时间轴上排序：优先用文件名里的采样点偏移，取不到就退回文件名排序
    starts = [parse_start(feats[i]["file"]) for i in idx]
    if use_time and len(idx) >= 6 and any(s is not None for s in starts):
        order = sorted(range(len(idx)),
                       key=lambda j: (starts[j] if starts[j] is not None else 1 << 60,
                                      feats[idx[j]]["file"]))
        groups = temporal_groups(Ec, order)
    else:
        groups = [[j] for j in range(len(idx))]

    GE = []
    for g in groups:
        v = Ec[g].mean(axis=0)
        GE.append(v / (np.linalg.norm(v) + 1e-12))
    GE = np.asarray(GE)
    weights = [len(g) for g in groups]

    if n_speakers and n_speakers >= 2:
        k = int(n_speakers)
        trace = [("人工指定", True, "界面上指定了 %d 个角色" % k)]
    else:
        k, trace = choose_k(GE, weights=weights)

    if GE.shape[0] < 2:
        # ★ 时间聚合把全部可信片段并成了**一组** —— 也就是「这素材里只有一个人」。
        #   这是单人视频最常见的样子，可 sklearn 的 AgglomerativeClustering 硬要求
        #   n_samples >= 2，这里传 1 行会
        #   ValueError: Found array with 1 sample(s) ... while a minimum of 2 is required
        #   ⇒ 整个导入当场失败（症状：阶段「分离不同声音」报 退出码 1）。
        #   只有一个人时根本不需要聚类，直接全归 0 号。
        glab = np.zeros(GE.shape[0], dtype=int)
        k = 1
        trace.append(("时间聚合", True,
                      "%d 个片段并成 1 组（全是一个人），跳过聚类" % len(idx)))
    else:
        glab = AgglomerativeClustering(n_clusters=k, metric="cosine",
                                       linkage="average").fit_predict(GE)
        k = int(len(set(glab)))
        trace.append(("时间聚合", True, "%d 个片段并成 %d 组后再聚类（组内最小 %d 段 / 最大 %d 段）"
                      % (len(idx), len(groups), min(weights), max(weights))))

    for gi, c in enumerate(glab):
        for j in groups[gi]:
            labels[idx[j]] = int(c)

    centers = []
    for c in range(k):
        m = glab == c
        v = GE[m].mean(axis=0)
        centers.append(list(v / (np.linalg.norm(v) + 1e-12)))

    # 低质量片段：挂到最近的中心，但太远就宁可不挂（标 -1，交给人工处理）
    Cm = np.asarray(centers)
    for i in range(n):
        if core[i]:
            continue
        sim = E[i] @ Cm.T
        j = int(np.argmax(sim))
        if sim[j] >= 0.80:      # 余弦相似度 0.8 以下不认，避免把噪音塞进角色
            labels[i] = j
        # 否则保持 -1

    return dict(labels=labels, centers=centers, core=core, k=k, trace=trace)


def main():
    if len(sys.argv) < 2:
        print("需要: 特征json [输出json] [指定说话人数]")
        sys.exit(2)
    feats = json.load(open(sys.argv[1], encoding="utf-8"))
    # 第三个参数是"用户已知的说话人数"。以前这里写成 cluster(feats) 把它**丢了**，
    # 于是上层明明传了 n_speakers也没人接 —— 用户说"这是 2 个人"完全不生效。
    n_speakers = None
    if len(sys.argv) > 3 and sys.argv[3].strip():
        try:
            n_speakers = int(sys.argv[3])
        except ValueError:
            n_speakers = None
    res = cluster(feats, n_speakers=n_speakers)
    from collections import Counter

    cnt = Counter(res["labels"])
    print("分出 %d 个角色（-1 表示没挂上的待定片段）" % res["k"])
    for k in sorted(cnt):
        print("  角色 %s: %d 段" % (k, cnt[k]))
    if len(sys.argv) > 2:
        json.dump(res, open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)


if __name__ == "__main__":
    main()
