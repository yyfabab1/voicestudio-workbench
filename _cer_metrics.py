# -*- coding: utf-8 -*-
"""CER 拆解 + 复读检测：把"长句乱掉"从一句笼统的 CER 拆成可归因的几项。

为什么不能只报 CER
------------------
老板的原话是"说长台词会乱掉"。CER 一个数把三种完全不同的故障混在一起：

  · 漏字（del）  —— 合成吞词、跳字，表现为后半句直接没了
  · 多字（ins）  —— **复读**，同一句/同一个短语反复念
  · 错字（sub）  —— 咬字不清、念错音

过拟合的典型症状是 **多字/dt重复**，而"素材不够"的典型症状是**错字**。
只报 CER 的话，60 轮和 20 轮哪个 CER 低就选哪个，根本看不出病因，
下一次调参还是瞎猜。

所以这里给出五项：
    cer_del / cer_ins / cer_sub / repeat_ratio / inflate

  repeat_ratio = 被判为"复读"的字符数 / 识别总字符数
  inflate      = 识别字数 / 原文长度（>1 是膨胀，<1 是吞掉）

复读怎么判
----------
对 n = 6..24（长优先），若 `s[i:i+n]` 在 s 里 i+1 之后再次出现，则该段
两次出现都记为复读。

为什么下限取 6 字：中文里 2~4 字的重复太正常了（"谢谢""好的好的""慢慢"），
按 3 字判会把正常口语全打成复读，尺子就废了。6 字以上的完整重复在中文口语
里几乎必然是故障（"从很久以前就是这样"重复两遍，没有正常人这么说话）。

⚠ 上限 24 是因为长复读往往是整句重复（10~20 字）。再长就用整句级判据兜。
"""
import sys


def align_ops(ref, hyp):
    """编辑距离对齐，返回 (sub, del_, ins) 三个计数。

    标准 Levenshtein DP + 回溯。用滚动数组存 DP 值、用整表存方向，
    长句（几百字）也就是几百 KB，够用。

    术语按"以 ref 为基准"：
      del_ = ref 有的字在 hyp 里没了（**漏字**）
      ins  = hyp 多出来的字（**复读/多字**）
      sub  = 一对一对错（**念错**）
    """
    n, m = len(ref), len(hyp)
    if n == 0:
        return 0, 0, m
    if m == 0:
        return 0, n, 0

    # DP 表，同时记方向
    INF = 1 << 30
    prev = list(range(m + 1))
    # 方向表：0=对角(匹配/替换) 1=上(del) 2=左(ins)
    dirs = [[0] * (m + 1) for _ in range(n + 1)]
    for j in range(1, m + 1):
        dirs[0][j] = 2
    for i in range(1, n + 1):
        dirs[i][0] = 1

    for i in range(1, n + 1):
        cur = [INF] * (m + 1)
        cur[0] = i
        ri = ref[i - 1]
        for j in range(1, m + 1):
            same = ri == hyp[j - 1]
            diag = prev[j - 1] + (0 if same else 1)
            up = prev[j] + 1
            left = cur[j - 1] + 1
            best = diag
            d = 0
            if up < best:
                best, d = up, 1
            if left < best:
                best, d = left, 2
            cur[j] = best
            dirs[i][j] = d
        prev = cur

    # 回溯
    i, j = n, m
    sub = del_ = ins = 0
    while i > 0 or j > 0:
        d = dirs[i][j]
        if i > 0 and j > 0 and d == 0:
            if ref[i - 1] != hyp[j - 1]:
                sub += 1
            i -= 1
            j -= 1
        elif i > 0 and d == 1:
            del_ += 1
            i -= 1
        else:
            ins += 1
            j -= 1
    return sub, del_, ins


def repeat_mask(s, min_n=6, max_n=24):
    """返回布尔列表：True 表示该字符属于"重复出现的长片段"。"""
    n_chars = len(s)
    marked = [False] * n_chars
    if n_chars < min_n * 2:
        return marked
    hi = min(max_n, n_chars // 2)
    for n in range(hi, min_n - 1, -1):      # 长的优先，短的不抢
        for i in range(0, n_chars - n + 1):
            sub = s[i:i + n]
            j = s.find(sub, i + 1)
            if j < 0:
                continue
            for k in range(i, i + n):
                marked[k] = True
            for k in range(j, j + n):
                marked[k] = True
    return marked


def repeat_ratio(s, min_n=6, max_n=24):
    """复读字符占比（0~1）。"""
    if not s:
        return 0.0
    return sum(repeat_mask(s, min_n, max_n)) / float(len(s))


def longest_repeat(s, min_n=6, max_n=24):
    """把**重复单元**取出来给人看（不是整段标记区）。

    ★ 为什么不能直接返回整段标记区：三遍重复首尾相接，标记区就是整句全长，
    打出来的"罪证"等于把识别结果重抄一遍，人根本看不出重复的是什么。
    （这条是自检抓出来的 —— 第一版就是这么错的。）
    正确做法：在标记区里找最小周期 p，让 seg 由 seg[:p] 反复拼接而成，
    返回 seg[:p] —— 那才是"被反复念的那一句"。
    """
    mk = repeat_mask(s, min_n, max_n)
    best, cur, start, bstart = 0, 0, 0, 0
    for i, v in enumerate(mk):
        if v:
            if cur == 0:
                start = i
            cur += 1
            if cur > best:
                best, bstart = cur, start
        else:
            cur = 0
    if not best:
        return ""
    seg = s[bstart:bstart + best]
    # 找最小周期：seg 必须是 seg[:p] 的整数次（或末尾截断）重复
    for p in range(min_n, len(seg) // 2 + 1):
        unit = seg[:p]
        if (unit * (len(seg) // p + 1))[:len(seg)] == seg:
            return unit
    # 不是严格周期（只是两处相同片段），退回打印这一段，太长就截断
    return seg if len(seg) <= 24 else seg[:24] + "…"


def score(ref, hyp):
    """一次算全。ref/hyp 都应是归一化后的文本（去标点空格）。"""
    sub, del_, ins = align_ops(ref, hyp)
    n = max(1, len(ref))
    R = ref if ref else ""
    H = hyp if hyp else ""
    return {
        "want_len": len(R),
        "hyp_len": len(H),
        "sub": sub, "del": del_, "ins": ins,
        "cer": (sub + del_ + ins) / float(n),
        "cer_sub": sub / float(n),
        "cer_del": del_ / float(n),
        "cer_ins": ins / float(n),
        "inflate": (len(H) / float(len(R))) if R else 0.0,
        "repeat_ratio": repeat_ratio(H),
        "worst_repeat": longest_repeat(H),
    }


def _selftest():
    """尺子自检：**必须能红**。绿得可疑的尺子比没尺子更坏。"""
    fails = []

    def ck(name, cond, got=""):
        print(("  [OK] " if cond else "  [FAIL] ") + name + ("" if cond else "  实际=%s" % (got,)))
        if not cond:
            fails.append(name)

    ref = "我知道你一直都在看着我，从很久以前就是这样，从来没有变过。"

    # 1) 完全一致 ⇒ 全 0
    r = score(ref, ref)
    ck("完全一致时 CER=0", r["cer"] == 0.0, r["cer"])

    # 2) 漏字：删掉后半句 ⇒ del 大量
    half = ref[:len(ref) // 2]
    r = score(ref, half)
    ck("漏半句 ⇒ cer_del 显著 > 0", r["cer_del"] > 0.2, r["cer_del"])
    ck("漏半句 ⇒ cer_ins 必须约等于 0（不能把漏判成多）", r["cer_ins"] < 0.05, r["cer_ins"])

    # 3) ★ 复读：整句重复 3 遍 ⇒ ins 大 + repeat_ratio 高
    dup = ref + ref + ref
    r = score(ref, dup)
    ck("★ 整句重复 3 遍 ⇒ cer_ins 显著 > 0.5", r["cer_ins"] > 0.5, r["cer_ins"])
    ck("★ 整句重复 3 遍 ⇒ repeat_ratio 高（>0.5）", r["repeat_ratio"] > 0.5, r["repeat_ratio"])
    ck("★ 复读时能指出罪证片段", r["worst_repeat"] == ref, r["worst_repeat"])

    # 4) 负对照：正常口语的短重复**不能**被判成复读
    normal = "好的好的，我知道了，谢谢你谢谢你，慢慢来吧。"
    ck("★ 负对照：短重复口语(2~3字)不算复读", repeat_ratio(normal) < 0.05, repeat_ratio(normal))

    # 5) 负对照：正常长句（无重复）repeat_ratio 必须是 0
    plain = "这个世界的规则从来都很简单，强者生存，弱者被时代碾过去。"
    ck("★ 负对照：正常长句 repeat_ratio=0", repeat_ratio(plain) == 0.0, repeat_ratio(plain))

    # 6) 部分长复读：念完一遍又念半遍
    part = ref + "从很久以前就是这样"
    r = score(ref, part)
    ck("半句复读 ⇒ repeat_ratio > 0 且 ins>0", r["repeat_ratio"] > 0 and r["ins"] > 0,
       "%s/%s" % (r["repeat_ratio"], r["ins"]))

    # 7) 错字：等长替换
    wrong = ref.replace("看着", "喊声")
    r = score(ref, wrong)
    ck("等长错字 ⇒ cer_sub>0 且 del/ins 约 0",
       r["cer_sub"] > 0 and r["cer_del"] < 0.05 and r["cer_ins"] < 0.05,
       "%s/%s/%s" % (r["cer_sub"], r["cer_del"], r["cer_ins"]))

    # 8) 三项之和必须等于总 CER（不漏账）
    r = score(ref, "我明道你一在看着我从很久以前")
    ck("CER = sub+del+ins（三项之和守恒）",
       abs(r["cer"] - (r["cer_sub"] + r["cer_del"] + r["cer_ins"])) < 1e-9,
       r["cer"])

    print("-" * 60)
    print("自检：%d 失败" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_selftest())
