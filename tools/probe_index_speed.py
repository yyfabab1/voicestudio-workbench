# -*- coding: utf-8 -*-
r"""量一次：RVC 索引的「加载重建」和「检索」到底各占多少秒，换 IVF 结构能省多少。

为什么量（2026-10-22 晚）：
    rvc_infer 新加的计时显示：67 MB 的索引（ra500fb92，IVF1700）加载 8.9 秒、
    推理 5~6 秒（30 秒素材）。而 10-21 新建的这批角色索引是 **IVF1**、187 MB：

        rb37829be_added_IVF1_Flat_nprobe_1_...index     187 MB
        ra500fb92_v2_klm_added_IVF1700_Flat_nprobe_1...  209 MB（老角色）

    `IVF1` = 只有 1 个聚类中心 ⇒ `nprobe=1` 的检索等于**扫全部向量**；
    `IVF1700` 只扫 1/1700 的桶。RVC 的 pipeline 每帧都要 `index.search(..., k=8)`，
    198 秒的歌是 9900 帧 ⇒ 两者的检索成本可能差两个数量级。

    这条量法**不占显卡**（纯 CPU / faiss），所以随时能跑。

它输出三件事：
    ① read_index + reconstruct_n(0, ntotal) 各多少秒（就是每档 RVC 的"索引加载"）
    ② 原索引 IVF1 跑 9900 帧 × k=8 要多少秒
    ③ 同样的向量重建成 IVF1700 后要多少秒，以及 **top-8 召回重合率**
       （召回率决定了"换索引会不会改音色" —— <0.95 就别换）

用法：
    "D:/RVC/.../runtime/python.exe" tools/probe_index_speed.py [index_path ...]
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IDX_DIR = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI\assets\indices"
DEFAULT = [
    os.path.join(IDX_DIR, "rb37829be_added_IVF1_Flat_nprobe_1_rb37829be_v1.index"),
    os.path.join(IDX_DIR, "ra500fb92_v2_klm_added_IVF1700_Flat_nprobe_1_ra500fb92_v2_klm_v2.index"),
]
FRAMES = 9900       # 198 秒 × 50 Hz
K = 8               # RVC pipeline 里的 k


def main(argv):
    import faiss
    import numpy as np

    paths = argv[1:] or DEFAULT
    for p in paths:
        if not os.path.isfile(p):
            print("跳过（不存在）：%s" % p)
            continue
        mb = os.path.getsize(p) / 1048576.0
        print("\n===== %s  (%.0f MB) =====" % (os.path.basename(p), mb))
        t0 = time.time()
        idx = faiss.read_index(p)
        t_read = time.time() - t0
        n, dim = int(idx.ntotal), int(idx.d)
        metric = "IP(内积)" if getattr(idx, "metric_type", 0) == 1 else "L2"
        nlist = getattr(idx, "nlist", None)
        t0 = time.time()
        vec = idx.reconstruct_n(0, n)
        t_rec = time.time() - t0
        print("  向量 %d 条 × %d 维 | 度量 %s | nlist=%s" % (n, dim, metric, nlist))
        print("  read_index %.2fs  reconstruct_n %.2fs（内存里 %.0f MB）"
              % (t_read, t_rec, vec.nbytes / 1048576.0))

        # 造查询：用真实向量加点噪声（比纯随机更接近 hubert 特征的分布）
        rs = np.random.RandomState(0)
        pick = rs.randint(0, n, size=min(FRAMES, n))
        q = vec[pick].copy()
        q += rs.randn(*q.shape).astype("float32") * 0.05
        q = np.ascontiguousarray(q, dtype="float32")
        try:
            if metric.startswith("IP"):
                faiss.normalize_L2(q)
                vv = vec.copy()
                faiss.normalize_L2(vv)
            else:
                vv = vec
        except Exception:
            vv = vec

        # ① 原索引
        try:
            idx.nprobe = 1
        except Exception:
            pass
        t0 = time.time()
        D1, I1 = idx.search(q, K)
        t_old = time.time() - t0
        print("  原索引检索 %d 帧 × k=%d：%.2fs" % (len(q), K, t_old))

        # ② 重建成 IVF1700（nlist 取老角色的那个值；向量不够就退回 n//39）
        nlist_new = 1700 if n >= 1700 * 39 else max(1, n // 39)
        quant = (faiss.IndexFlatIP(dim) if metric.startswith("IP")
                 else faiss.IndexFlatL2(dim))
        new = faiss.IndexIVFFlat(quant, dim, nlist_new,
                                 faiss.METRIC_INNER_PRODUCT if metric.startswith("IP")
                                 else faiss.METRIC_L2)
        t0 = time.time()
        new.train(np.ascontiguousarray(vv, dtype="float32"))
        new.add(np.ascontiguousarray(vv, dtype="float32"))
        new.nprobe = 1
        t_build = time.time() - t0
        t0 = time.time()
        D2, I2 = new.search(q, K)
        t_new = time.time() - t0
        # 召回：原索引的 k 个近邻里，新索引命中了几个
        hit = 0.0
        for a, b in zip(I1, I2):
            sa = set(int(x) for x in a if x >= 0)
            sb = set(int(x) for x in b if x >= 0)
            if sa:
                hit += len(sa & sb) / float(len(sa))
        recall = hit / max(1, len(q))
        print("  重建 IVF%d：建库 %.1fs | 检索 %.2fs | top-%d 召回 %.2f%%"
              % (nlist_new, t_build, t_new, K, recall * 100))
        if t_old > 0.01:
            print("  ⇒ 检索快 %.1f 倍（%.2fs → %.2fs，按 %d 帧折算）"
                  % (t_old / max(t_new, 1e-6), t_old, t_new, FRAMES))
    print("\nALLDONE")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
