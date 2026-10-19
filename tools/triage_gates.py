#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""判红分诊：把「批跑红、单跑绿」这件事从体力活变成一行结论。

为什么要它：
    2026-10-19 那一轮全量回归里，"批跑红、单独跑绿"出现了 4 次，
    其中 3 次是假红（资源竞争 / 删除钩子 / 旧服务），1 次是真回归。
    但每一次的判断成本都一样：翻日志、找到红项、手动单跑、再比一遍红项。
    这活机器干得又快又一致 —— 而且它**不会因为"上次也是假红"就先入为主**。

三态（这是给**人**看的结论，不是自动化放行）：
    疑似假红       单跑绿了。红项在单跑里全部通过。
    疑似真回归     单跑还红，且红项与批跑一致（归一化后同一批判据）。
    不稳定判据     单跑还红，但红项变了（批跑红 A、单跑红 B）——
                   这类最危险：说明尺子本身在抖，不能当成产品回归去改代码。

判红项的比对口径：
    判据**文本骨架**（去掉数字/标点/空白），不是整行原文。
    因为同一道判据在两次跑里的**实测值本来就不同** —— 批跑"信噪比 1.3x"、
    单跑"信噪比 6.2x"，如果按原文比，会把"同一条判据"判成"两条不同的"，
    假红会被误判成"不稳定"。数值单独作为**对账证据**展示，不参与身份判定。

用法：
    python tools/triage_gates.py --gate verify_tone \
        --batch _reg_gates/verify_tone.prev.log \
        --single _reg_gates/verify_tone.log --rc 0
"""

import argparse
import json
import os
import re
import sys

# 判红行：各门禁统一是 `  ! FAIL  <name>  | <detail>`（check() 的输出）。
# 行首锚定很重要：汇总行 "PASS 41 / FAIL 0" 里也有 FAIL，但那不是判红项。
RE_FAIL = re.compile(r"^\s*!?\s*FAIL\s+(.+?)\s*$")
RE_PASS = re.compile(r"^\s*PASS\s+(.+?)\s*$")
# 汇总行，例如 "PASS 41 / FAIL 0"、"通过 30 项，失败 0 项"
RE_SUM = re.compile(r"^(PASS|通过)\s*(\d+).*?(FAIL|失败)\s*(\d+)")

VERDICT = {
    "false_red": "疑似假红",
    "real_regression": "疑似真回归",
    "unstable": "不稳定判据",
    "blocked": "未执行（服务契约不达标）",
    "died": "中断（一条判据都没跑到）",
    "clean": "本关没有红项",
    "unknown": "无法比对（缺批跑日志）",
}

# 终端里的一个字符标记：扫一眼就知道这关要不要管。
MARK = {"false_red": "≈", "real_regression": "✗", "unstable": "!",
        "blocked": "⊘", "died": "⊗", "clean": "✓", "unknown": "?"}

# 这些结论可以放行：**单独复跑没有复现批跑的红**，或门禁根本没跑（版本守卫）。
# 其余（真回归 / 不稳定 / 无法比对）一律阻塞 —— "我不知道"不是"没问题"。
OK_VERDICTS = ("false_red", "blocked", "clean")


def split_item(s):
    """`name  | detail` -> (name, detail)。detail 里的 `|` 属于正文。"""
    if "  | " in s:
        n, d = s.split("  | ", 1)
        return n.strip(), d.strip()
    if " | " in s:
        n, d = s.split(" | ", 1)
        return n.strip(), d.strip()
    return s.strip(), ""


def skeleton(s):
    """判据身份：去掉数字与标点，只留文字骨架。

    例："★ 负对照：…的 2 倍 | 抖动 1.1 / 换语气 47.5，信噪比 1.3x"
        -> "★负对照…的倍抖动换语气信噪比x"
    两次跑里唯一会变的就是数字，所以它必须被抹掉。
    """
    s = re.sub(r"[-+]?\d+(?:\.\d+)?", "", s)
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[|｜:：,，。.;；()（）\[\]【】<>《》/\\'\"`~!！?？+*=×x%-]", "", s)
    return s


def read_lines(path):
    if not path or not os.path.isfile(path):
        return []
    for enc in ("utf-8", "gbk", "utf-16"):
        try:
            with open(path, encoding=enc, errors="strict") as f:
                return f.read().splitlines()
        except (UnicodeDecodeError, UnicodeError):
            continue
        except OSError:
            return []
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().splitlines()


def collect(lines):
    """返回 (fails, by_key)。

    fails: [{name, detail, raw, nums}]
    by_key: 骨架 -> 该判据在**这一份日志**里的最后一行（PASS 或 FAIL）+ 状态
    """
    fails, by_key = [], {}
    for raw in lines:
        m = RE_FAIL.match(raw)
        if m:
            name, detail = split_item(m.group(1))
            item = {"name": name, "detail": detail, "raw": raw.strip(),
                    "nums": numbers(detail)}
            fails.append(item)
            by_key[skeleton(name)] = {"state": "FAIL", "raw": raw.strip(),
                                      "name": name, "detail": detail,
                                      "nums": item["nums"]}
            continue
        m = RE_PASS.match(raw)
        if m and not RE_SUM.match(raw):     # 汇总行 "PASS 41 / FAIL 0" 不算判据
            name, detail = split_item(m.group(1))
            by_key[skeleton(name)] = {"state": "PASS", "raw": raw.strip(),
                                      "name": name, "detail": detail,
                                      "nums": numbers(detail)}
    return fails, by_key


def numbers(s):
    """抽实测值片段，给人对账用。保留单位（x / % / ms / Hz / 音分）。
    `信噪比 1.3x` -> ["1.3x"]；`-10ms / 4%` -> ["-10ms", "4%"]。"""
    out = []
    for m in re.finditer(r"[-+]?\d+(?:\.\d+)?\s*(?:x|×|%|ms|Hz|hz|音分|dB)?", s or ""):
        t = m.group(0).strip()
        if t and t not in out:
            out.append(t)
    return out


def last_exit(lines):
    """批跑日志里这一关的退出码（一行 "EXIT=n"）。没有就返回 None。"""
    for line in reversed(lines):
        m = re.match(r"^EXIT=(\d+)$", line.strip())
        if m:
            return int(m.group(1))
    return None


def triage(gate, batch_path, single_path, rc):
    b_lines = read_lines(batch_path)
    s_lines = read_lines(single_path)
    b_fails, b_by_key = collect(b_lines)
    s_fails, s_by_key = collect(s_lines)
    b_exit = last_exit(b_lines)

    # 批跑日志缺了 / 没有判红项 ⇒ 只能判两态（单跑绿=假红，单跑红=真回归）
    have_batch = bool(b_lines) and bool(b_fails)
    if rc == 4 and not s_fails:
        # 门禁**根本没跑**（版本守卫拦下的）。这不是"假红"，也不是"真回归"，
        # 说成任何一态都会让人去查错东西。
        verdict = "blocked"
    elif rc != 0 and not s_fails and not b_fails:
        # 两次跑都**一条判据都没输出**就退出。这不是"判据红了"，
        # 是门禁根本没跑起来（撞 safe-delete 钩子 / import 就崩 / 被杀）。
        # 实测：batch 2 里 verify_sing、verify_storage 就是被钩子掐在
        # 第一条判据之前的，日志里只剩一行 safe-delete。
        # ★ 这一态必须**阻塞**：启动即崩的关如果放行，等于把"跑都没跑"当"通过"。
        verdict = "died"
    elif rc == 0:
        verdict = "false_red" if (b_fails or b_exit == 4) else "clean"
    elif not have_batch:
        verdict = "unknown"
    else:
        bk = {skeleton(x["name"]) for x in b_fails}
        sk = {skeleton(x["name"]) for x in s_fails}
        verdict = "real_regression" if bk == sk else "unstable"

    # 单跑实测值：按批跑红项的骨架，去单跑日志里找同一条判据
    evidence = []
    for x in b_fails:
        k = skeleton(x["name"])
        hit = s_by_key.get(k)
        evidence.append({
            "check": x["name"],
            "batch_detail": x["detail"],
            "batch_nums": x["nums"],
            "single_state": hit["state"] if hit else "MISSING",
            "single_detail": hit["detail"] if hit else "",
            "single_nums": hit["nums"] if hit else [],
        })

    # 单跑新冒出来的红项（批跑里没有）—— "不稳定"的关键证据
    bk = {skeleton(x["name"]) for x in b_fails}
    new_red = [{"check": x["name"], "detail": x["detail"]}
               for x in s_fails if skeleton(x["name"]) not in bk]

    # 对账时最容易被误读的一点：批跑 EXIT=4 是"被版本守卫拒绝"，不是"跑了但红"。
    note = ""
    if b_exit == 4:
        note = "批跑时服务是旧进程，门禁被版本守卫拒绝（没有判据明细）"
    elif verdict == "died":
        note = ("两次跑都没有任何判据输出 —— 不是判据红了，是门禁没跑起来"
                "（撞 safe-delete 钩子 / 启动即崩）。看日志里的 safe-delete 行")
    elif b_exit is None and not have_batch:
        note = "没有可用的批跑独立日志，只能判两态"

    return {
        "gate": gate,
        "verdict": verdict,
        "verdict_text": VERDICT.get(verdict, verdict),
        "note": note,
        "batch_exit": b_exit,
        "single_exit": rc,
        "batch_red_count": len(b_fails),
        "single_red_count": len(s_fails),
        "batch_red": [x["name"] for x in b_fails],
        "single_red": [x["name"] for x in s_fails],
        "evidence": evidence,
        "new_red_in_single": new_red,
        "have_batch_log": have_batch,
    }


def load_jsonl(path):
    """读一行一关的分诊结果。坏行跳过 —— 一行写一半不该让整张表消失。"""
    rows = []
    if not path or not os.path.isfile(path):
        return rows
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def report(path):
    """把一行一关的 JSON 汇成一屏结论。
    格式刻意做成"关口 + 三态 + 批跑红数/单跑红数 + 关键实测值"，
    因为人最后要做的动作只有两个：假红 → 忽略；真回归 → 去查；
    不稳定 → 先修尺子（改判据），别急着改产品。"""
    rows = load_jsonl(path)
    if not rows:
        print("=== 判红分诊 ===\n没有红关，也没有分诊记录。")
        return 0
    cnt = {}
    for r in rows:
        cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
    print("=== 判红分诊 ===")
    print("%d 个红关：疑似假红 %d · 疑似真回归 %d · 不稳定判据 %d · 无法比对 %d"
          % (len(rows), cnt.get("false_red", 0), cnt.get("real_regression", 0),
             cnt.get("unstable", 0), cnt.get("unknown", 0)))
    print("（这些结论会决定回归脚本的退出码：假红/未执行放行，"
          "真回归/不稳定/无法比对阻塞 —— 见下面的「分诊决定退出码」）")
    for r in rows:
        print("\n%s %-28s %s   批跑 EXIT=%s 红 %d / 单跑 EXIT=%s 红 %d"
              % (MARK.get(r["verdict"], "?"), r["gate"], r["verdict_text"],
                 "-" if r.get("batch_exit") is None else r["batch_exit"],
                 r["batch_red_count"],
                 "-" if r.get("single_exit") is None else r["single_exit"],
                 r["single_red_count"]))
        if r.get("note"):
            print("    — %s" % r["note"])
        for e in r["evidence"][:4]:
            was = " ".join(e["batch_nums"]) or (e["batch_detail"] or "")[:60] \
                or "（没带数字）"
            if e["single_state"] == "MISSING":
                got = "（单跑里没找到这条判据）"
            else:
                got = (" ".join(e["single_nums"]) or (e["single_detail"] or "")[:60]
                       or "（这条判据没有实测数字）")
            print("    · %s" % e["check"][:66])
            print("        批跑 %s  →  单跑 %s [%s]"
                  % (was, got, e["single_state"]))
        for x in r["new_red_in_single"][:4]:
            print("    · 单跑新红：%s | %s" % (x["check"][:56], x["detail"][:70]))
    return 0


def decide(path):
    """把分诊结论变成**退出码**。

    为什么要有这一步：分诊原先是"跑完人看"，所以 `_run_regression.sh` 的退出码
    仍然由"批跑有没有红"决定 ⇒ 一次假红就把 CI / 自动流程堵死；反过来，
    如果有人为了不被堵就把红关当假红手工放过，那是另一种失守。
    让机器按分诊结论决定放不放，两件事同时成立：
        疑似假红 / 未执行  → 放行（exit 0），但**大声说**这一轮批跑确实红过
        真回归 / 不稳定 / 无法比对 → 阻塞（exit 1）

    口径上的取舍：
      · `clean` 也算放行（这一关单独复跑是绿的，批跑红项在明细里不存在）；
      · `unknown`（缺批跑日志）**算阻塞** —— 无法比对就不该放行，
        "我不知道"永远不是"没问题"。
    """
    rows = load_jsonl(path)
    if not rows:
        print("=== 分诊决定退出码 ===")
        print("本轮没有红关 → 放行（exit 0）")
        print("DECISION=pass")
        return 0

    bad = [r for r in rows if r.get("verdict") not in OK_VERDICTS]
    ok = [r for r in rows if r.get("verdict") in OK_VERDICTS]
    print("=== 分诊决定退出码 ===")
    if not bad:
        cnt = {}
        for r in ok:
            cnt[r["verdict"]] = cnt.get(r["verdict"], 0) + 1
        bits = "、".join("%s %d 关" % (VERDICT.get(k, k), n) for k, n in cnt.items())
        print("放行（exit 0）：%d 个红关全部判为 %s" % (len(ok), bits))
        print("⚠ 放行 ≠ 全绿：批跑时这 %d 关**确实红过**，只是单独复跑没能复现。"
              % len(ok))
        print("  明细在上面那张分诊表里；若同一关反复假红，要去查资源竞争，"
              "别习惯了就当没事。")
        print("DECISION=pass")
        return 0
    print("阻塞（exit 1）：%d 个红关里有 %d 个不能当假红放过" % (len(rows), len(bad)))
    for r in bad:
        print("  %s %-28s %s（单跑红 %d 项）"
              % (MARK.get(r.get("verdict"), "?"), r.get("gate", "?"),
                 VERDICT.get(r.get("verdict"), r.get("verdict")),
                 r.get("single_red_count", 0)))
    if any(r.get("verdict") == "unstable" for r in bad):
        print("  · 不稳定：批跑红项和单跑红项不是同一批 ⇒ 先修判据（尺子），"
              "别急着改产品。")
    if any(r.get("verdict") == "real_regression" for r in bad):
        print("  · 真回归：同一批判据两次都红 ⇒ 去查这次改了什么。")
    if any(r.get("verdict") == "unknown" for r in bad):
        print("  · 无法比对：缺批跑独立日志 ⇒ 无法排除，按阻塞处理。")
    if any(r.get("verdict") == "died" for r in bad):
        print("  · 中断：一条判据都没跑到（多半是 safe-delete 钩子把删除挡了）"
              "⇒ 拆细重跑，别当成产品坏了。")
    print("DECISION=block")
    return 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gate", default="")
    ap.add_argument("--batch", default="", help="批跑时这一关的独立日志")
    ap.add_argument("--single", default="", help="刚单独复跑的日志")
    ap.add_argument("--rc", type=int, default=0, help="单跑退出码")
    ap.add_argument("--json", action="store_true", help="只输出一行 JSON")
    ap.add_argument("--report", default="", help="把 JSONL 汇总成一屏结论")
    ap.add_argument("--decide", default="", help="按 JSONL 决定退出码（0 放行 / 1 阻塞）")
    a = ap.parse_args()

    if a.decide:
        return decide(a.decide)
    if a.report:
        return report(a.report)
    if not a.gate or not a.single:
        ap.error("要么给 --gate + --single，要么给 --report")

    r = triage(a.gate, a.batch, a.single, a.rc)
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
        return 0

    print("%s %s —— %s（批跑红 %d 项 / 单跑红 %d 项）"
          % (MARK.get(r["verdict"], "?"), r["gate"], r["verdict_text"],
             r["batch_red_count"], r["single_red_count"]))
    if r["verdict"] == "unknown":
        print("    ⚠ 没有可用的批跑独立日志，只能判两态 —— 见 _reg_gates/ 是否落盘")
    for e in r["evidence"]:
        got = " ".join(e["single_nums"]) or e["single_detail"]
        if not got:
            # 判据存在但没带数字（例如"存在/可见"这类），别说成"没找到" ——
            # 那是两件完全不同的事，会把人往错的方向带。
            got = ("（这条判据没有实测数字）"
                   if e["single_state"] in ("PASS", "FAIL")
                   else "（单跑里没找到这条判据）")
        was = " ".join(e["batch_nums"]) or e["batch_detail"] or "（没带数字）"
        print("    · %s" % e["check"][:70])
        print("        批跑：%s" % was)
        print("        单跑：%s [%s]" % (got, e["single_state"]))
    for x in r["new_red_in_single"]:
        print("    · 单跑新红：%s  | %s" % (x["check"][:60], x["detail"][:80]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
