# -*- coding: utf-8 -*-
"""分段对齐的**受控 A/B**：同一段成品音频，「校正后」vs「把校正退回去」各打一次分。

为什么需要它
------------
端到端整首对比有一个绕不开的干扰：两次推理的相位本来就随机，
`match_ratio` 会有 ±0.8 点的抖动（实测同一输入连跑 3 次的极差）。
所以「整首均值 92.91 → 92.49」这种差距**根本分不清是对齐变好还是变坏**。

这一支探针把干扰彻底消掉：**成品音频是同一份**，只是把 align_phase 挪过去的
那几毫秒再挪回来（roll 整数样本），两边都拿同一段源去打分。
⇒ 差多少，就是那一步校正本身值多少，不含任何推理噪声。

判据：
  R1 多数段的校正**确实涨分**（涨的段数 > 跌的段数）
  R2 平均涨幅 > 0（校正是正收益，不是负收益）
  R3 跌分最狠的那一段要被点名（> 2 点 ⇒ 那一段的校正值得怀疑）

用法：
    <rvc py> -s tools\\probe_segab.py
lag 表从 data/_e2e_new.log 里读（[align] 成品比源晚 X ms ⇒ 已挪回）。
"""

import glob
import os
import re
import subprocess
import sys

import numpy as np
import soundfile as sf

RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
RVC_PY = os.path.join(RVC_ROOT, "runtime", "python.exe")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(HERE, "data", "_probe_realseg")
LOG = os.path.join(HERE, "data", "_e2e_new.log")
SR = 16000


def read_lags(n_seg):
    """从端到端日志里把每段挪了多少毫秒抠出来，**按段号落位**。

    ★ 不能按出现顺序往下配对：日志里 `align` 行在该段的 `[段 N/8]` 行**之前**，
      而且被保守拒绝的段**没有** `成品比源晚` 那行（只有「回滚不校正」）。
      按出现顺序配的话，第 6 段一缺，后面整串错位一位 ——
      实测把第 7 段的 13.89ms 安到了第 6 段、第 8 段直接漏掉。
    """
    lags = [0.0] * n_seg
    if os.path.exists(LOG):
        pend = 0.0
        for ln in open(LOG, encoding="utf-8", errors="replace"):
            m = re.search(r"\[align\] 成品比源晚 ([\d.]+) ms", ln)
            if m:
                pend = float(m.group(1))
                continue
            m2 = re.search(r"\[段 (\d+)/\d+\]", ln)
            if m2:
                i = int(m2.group(1)) - 1
                if i < n_seg:
                    lags[i] = pend
                pend = 0.0     # ★ 用掉就清掉：下一段没 align 行 ⇒ 0（被拒绝）
    return lags


def main():
    src = sorted(glob.glob(os.path.join(WORK, "in0*.wav")))
    seg = sorted(glob.glob(os.path.join(WORK, "sg0*.wav")))
    n = min(len(src), len(seg))
    if not n:
        print("没有切片：先跑一次 tools/probe_real_seg.py")
        return 2
    lags = read_lags(n)
    print("源切片 %d 段，成品切片 %d 段；每段 lag(ms)：%s"
          % (len(src), len(seg), ", ".join(
              ("%.2f" % x) if x > 0 else "—" for x in lags)))

    def qc(path):
        j = os.path.splitext(path)[0] + ".json"
        r = subprocess.run([RVC_PY, "-s", os.path.join(HERE, "stages", "qc_cli.py"),
                            pin, path, j], cwd=RVC_ROOT, capture_output=True,
                           text=True, encoding="utf-8", errors="replace")
        try:
            import json
            return json.loads(r.stdout.strip().splitlines()[-1])
        except Exception:
            return {}

    import tempfile
    tmp = tempfile.mkdtemp(prefix="segab_")
    rows = []
    print("\n段  lag(ms)   校正后%   退回后%    差   IQR(校正/退回)")
    for i in range(n):
        pin = src[i]
        y, _ = sf.read(seg[i], dtype="float32")
        if y.ndim > 1:
            y = y.mean(1)
        pa = os.path.join(tmp, "a%02d.wav" % i)
        sf.write(pa, y, SR)
        lag = lags[i] if i < len(lags) else 0.0
        pb = os.path.join(tmp, "b%02d.wav" % i)
        if lag > 0:
            # 校正 = 把成品**提前** lag ⇒ 退回去就是**延后** lag
            sf.write(pb, np.roll(y, int(round(lag * SR / 1000.0))), SR)
        else:
            sf.write(pb, y, SR)
        if lag <= 0:
            print(" %d  （未校正）  —" % (i + 1))
            continue
        qa, qb = qc(pa), qc(pb)
        ma, mb = qa.get("match_ratio"), qb.get("match_ratio")
        if not (ma and mb):
            print(" %d  %.2f    打分失败" % (i + 1, lag))
            continue
        rows.append((i + 1, lag, ma, mb))
        print(" %d  %6.2f    %6.1f    %6.1f  %+5.1f   %.1f / %.1f"
              % (i + 1, lag, ma * 100, mb * 100, (ma - mb) * 100,
                 qa.get("dev_iqr_cents", 0), qb.get("dev_iqr_cents", 0)))

    if len(rows) < 3:
        print("可比段落太少")
        return 2
    d = np.array([(a - b) * 100 for _, _, a, b in rows])
    up = int((d > 0.5).sum())
    dn = int((d < -0.5).sum())
    worst = int(np.argmin(d))
    print("\nR1 校正后涨分的段 %d 个 / 跌分的段 %d 个 ⇒ %s"
          % (up, dn, "OK" if up > dn else "★ 校正整体是负收益"))
    print("R2 平均涨幅 %+.2f 点 ⇒ %s" % (d.mean(), "OK" if d.mean() > 0 else "★ 校正没带来收益"))
    print("R3 跌得最狠：第 %d 段 %+.1f 点（lag %.2f ms）⇒ %s"
          % (rows[worst][0], d[worst], rows[worst][1],
             "OK" if d[worst] > -2.0 else "★ 这一段的校正值得怀疑"))
    ok = (up > dn) and (d.mean() > 0) and (d[worst] > -2.0)
    print("\n结论：%s" % ("分段相位校正是正收益" if ok else "★ 分段相位校正有问题"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
