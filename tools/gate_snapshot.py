#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把这一轮全量回归的结果写成一份**给人看**的快照（data/gate_snapshot.json）。

为什么要有这份东西：34 道门禁是本项目最被低估的资产 —— 它们每天挡住的是
"改了一个地方、另一个地方悄悄坏了"这类最难发现的回归。但它们只活在
终端输出里，用户（包括一周后的自己）根本不知道"这个功能到底被验证过没有"。
把最近一次全绿的**时间 / 覆盖的判据数 / 跑在什么机器上**摆到界面底部，
不是炫耀，是回答一个正当的问题：我能不能信这个按钮。

由 `_run_regression.sh` 在收尾时调用，参数由 shell 从关卡日志里 grep 出来：
    python tools/gate_snapshot.py --checks 1234 --bad 0 --gates 34 --gates-ok 34 --rc 0

只做一件事：写 JSON。绝不修改任何产品数据。
"""

import argparse
import json
import os
import platform
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "gate_snapshot.json")


def read_counts(path):
    """每关判据数：`<gate>\\t通过 41 项，失败 0 项` -> {gate: {checks, bad}}

    ★ 为什么快照不只存总数 1332：
      某关从 48 条掉到 40 条，可能是**删了 8 条判据**（合理），也可能是
      **某个分支被跳过、少注册了 8 条**（危险）—— 总数不变时这两件事在
      快照上长得一模一样。每关存一份，下一轮就能自己比出来。
      这不是"更细的统计"，是**判据本身的存在性被守护**。
    """
    per = {}
    if not path or not os.path.isfile(path):
        return per
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            name, sep, rest = line.partition("\t")
            if not sep or not name.strip():
                continue
            nums = re.findall(r"\d+", rest)
            if len(nums) < 2:
                continue
            # ★ 存**注册数**（通过 + 失败），不是"通过数"。
            #   两者在绿的时候相等，一红就不一样了：某关 82 → 「通过 77 / 失败 4」，
            #   若拿通过数比，diff 会报「82→77 掉 5 条」，看着像**删了 5 条判据**，
            #   其实一条没删。判据数快照要守的是"注册了多少条"，不是"过了多少条"。
            per[name.strip()] = {"checks": int(nums[0]) + int(nums[1]),
                                 "bad": int(nums[1])}
    return per


def diff_snap(old, per, total):
    """跟上一份快照比每关判据数。返回给人看的结论 + 机器可读的明细。"""
    prev_at = (old or {}).get("at")
    op = (old or {}).get("per_gate")
    if not isinstance(op, dict) or not op:
        return {
            "prev_at": prev_at,
            "changes": [],
            "dropped": [],
            "checks_delta": None,
            "text": ("首次建立每关基线（上一份快照没有明细）" if old
                     else "首次建立基线（没有上一份可对比）"),
            "warn": False,
        }

    changes, dropped = [], []
    for g in sorted(op):
        was = op[g].get("checks", 0) if isinstance(op[g], dict) else 0
        now = per.get(g)
        if now is None:
            # 关卡整个消失 —— 比掉数更严重：它可能连跑都没跑
            changes.append({"gate": g, "kind": "gone", "from": was, "to": None,
                            "delta": -was})
            dropped.append("%s %d→（本关消失）" % (g, was))
        elif now["checks"] < was:
            changes.append({"gate": g, "kind": "drop", "from": was,
                            "to": now["checks"], "delta": now["checks"] - was})
            dropped.append("%s %d→%d" % (g, was, now["checks"]))
        elif now["checks"] > was:
            changes.append({"gate": g, "kind": "rise", "from": was,
                            "to": now["checks"], "delta": now["checks"] - was})
    for g in sorted(per):
        if g not in op:
            changes.append({"gate": g, "kind": "new", "from": 0,
                            "to": per[g]["checks"], "delta": per[g]["checks"]})

    # ★ 改名配对：关卡改名会让 diff 同时冒"消失 + 新增"两条告警，看着像丢了一整关。
    #   判定口径：一个关消失、同时一个新关出现，且判据数相当（差 ≤ max(2, 10%)）
    #   ⇒ 标成"疑似改名"，而不是两条独立告警。
    #   为什么是"疑似"：判据数相同也可能是巧合（删了 30 条又加了 30 条）。
    #   所以它只降噪，不改变"掉数"的告警 —— 真有下降时，下降那条照旧红着。
    gone_c = [c for c in changes if c["kind"] == "gone"]
    new_c = [c for c in changes if c["kind"] == "new"]
    pairs = []
    for gc in gone_c:
        slack = max(2, round(gc["from"] * 0.10))
        for nc in new_c:
            d = abs(nc["to"] - gc["from"])
            if d <= slack:
                pairs.append((d, gc["gate"], nc["gate"], gc["from"], nc["to"]))
    pairs.sort()                      # 先配最接近的一对，避免多个改名互相错配
    used, renames = set(), []
    for d, gname, nname, was, now in pairs:
        if gname in used or nname in used:
            continue
        used.add(gname); used.add(nname)
        renames.append({"gate": gname, "new_gate": nname, "kind": "rename",
                        "from": was, "to": now, "delta": now - was})
    if renames:
        changes = [c for c in changes
                   if not (c["kind"] in ("gone", "new") and c["gate"] in used)]
        changes += renames
        dropped = [x for x in dropped if x.split(" ")[0] not in used]

    prev_total = (old or {}).get("checks")
    delta = (total - int(prev_total)) if isinstance(prev_total, int) else None
    grows = [c for c in changes if c["kind"] in ("rise", "new")]
    if dropped:
        text = "⚠ 判据数下降：%s（共 %d 关掉数）" % (
            "；".join(dropped[:4]) + ("…" if len(dropped) > 4 else ""), len(dropped))
    elif grows:
        text = "判据数较上次 +%d（%d 关有新增）" % (
            sum(c["delta"] for c in grows), len(grows))
    else:
        text = "判据数较上次 0 变化"
    if renames:
        rtxt = "疑似改名：%s" % "；".join(
            "%s→%s（%d→%d）" % (r["gate"], r["new_gate"], r["from"], r["to"])
            for r in renames[:3])
        # "0 变化"是个结论，不能跟"疑似改名"并列摆着 —— 那会自相矛盾。
        text = rtxt if text == "判据数较上次 0 变化" else text + "；" + rtxt
    return {"prev_at": prev_at, "changes": changes, "dropped": dropped,
            "checks_delta": delta, "text": text, "warn": bool(dropped)}


def read_triage(path):
    """分诊结论计数：{verdict: n}。没有就返回空 dict。

    ★ 为什么快照要记这个：退出码可能因为"全是假红"而变成 0，
      但那样快照上只剩"全绿"两个字 —— 把"这一轮批跑其实红过"抹掉了。
      记下来，界面才能说人话："3 关批跑红过，分诊判为假红"。
    """
    cnt = {}
    if not path or not os.path.isfile(path):
        return cnt
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            v = r.get("verdict")
            if v:
                cnt[v] = cnt.get(v, 0) + 1
    return cnt


def gpu_name():
    """显卡名。拿不到就返回空串 —— 快照是给人看的，不许因为它失败。"""
    for cmd in (["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                ["wmic", "path", "win32_VideoController", "get", "name"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        lines = [l.strip() for l in (p.stdout or "").splitlines() if l.strip()]
        # wmic 的第一行是表头 "Name"
        lines = [l for l in lines if l.lower() != "name"]
        if lines:
            return lines[0]
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--checks", type=int, default=0, help="判据总数")
    ap.add_argument("--bad", type=int, default=0, help="判红条数")
    ap.add_argument("--gates", type=int, default=0, help="关卡总数")
    ap.add_argument("--gates-ok", type=int, default=0, help="EXIT=0 的关卡数")
    ap.add_argument("--rc", type=int, default=0, help="整轮退出码（0 = 全绿）")
    ap.add_argument("--gates-failed", default="", help="没过的关卡名，逗号分隔")
    ap.add_argument("--counts-file", default="", help="每关汇总行（gate<TAB>通过 N 失败 M）")
    ap.add_argument("--triage-json", default="", help="分诊结果 JSONL（_reg_triage.jsonl）")
    ap.add_argument("--out", default=OUT, help="快照写到哪里（默认 data/gate_snapshot.json）")
    args = ap.parse_args()

    out = args.out
    per_gate = read_counts(args.counts_file)
    triage = read_triage(args.triage_json)
    old = None
    try:
        if os.path.isfile(out):
            with open(out, encoding="utf-8") as f:
                old = json.load(f)
    except Exception as e:
        # 旧快照坏了不该阻断"写新快照" —— 但必须说出来，否则会静默丢掉 diff
        print("[warn] 旧快照读不了，diff 从这一份重新建立：%s" % e)
        old = None

    snap = {
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "ts": int(time.time()),
        "gates": args.gates,
        "gates_ok": args.gates_ok,
        "checks": args.checks,
        "checks_bad": args.bad,
        "pass": (args.rc == 0 and args.bad == 0),
        "failed": [x for x in args.gates_failed.split(",") if x],
        # 每关判据数：让"某关少了 8 条"这种沉默的损耗下一次就能被看见
        "per_gate": per_gate,
        "diff": diff_snap(old, per_gate, args.checks),
        # 分诊结论：退出码可能因为"全是假红"而放行，这里留一份"红过"的证据
        "triage": triage,
        "platform": {
            "os": platform.platform(),
            "python": platform.python_version(),
            "gpu": gpu_name(),
        },
    }
    try:
        os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
        tmp = out + ".tmp"
        # 原子写：写一半被中断的话，界面上拿到的是半个 JSON（比没有更糟——
        # 它会让"门禁快照"这一块直接空白，看着像功能坏了）。
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=1)
        if os.path.exists(out):
            os.remove(out)
        os.rename(tmp, out)
    except Exception as e:
        print("[warn] 门禁快照没写成：%s" % e)
        return 1
    print("门禁快照已写入 %s：%d 关 / %d 判据 / 全绿=%s"
          % (os.path.relpath(out, HERE), args.gates, args.checks, snap["pass"]))
    d = snap["diff"]
    print("  " + d["text"])
    if triage:
        okn = triage.get("false_red", 0) + triage.get("blocked", 0) + triage.get("clean", 0)
        badn = sum(v for k, v in triage.items() if k not in ("false_red", "blocked", "clean"))
        print("  分诊：放行 %d 关 / 阻塞 %d 关（明细见 _reg_triage.jsonl）" % (okn, badn))
    if d.get("warn"):
        print("  ★ 判据数下降不一定是坏事，但**必须**有人看一眼："
              "删了判据，还是某个分支被跳过、少注册了？")
    return 0


if __name__ == "__main__":
    sys.exit(main())
