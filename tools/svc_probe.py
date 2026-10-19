#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""服务版本契约探针：门禁开跑前的第一道闸。

它只回答一个问题：**正在 8756 上跑的那个进程，接口够不够新？**

背景（真实事故，2026-10-19）：
    train_rounds_ui 红了，日志里是 /api/... 404。查了半天代码没动过 ——
    根因是门禁跑在**昨天起的旧服务**上。页面照样 200，接口照样能应答，
    只有新加的那一个端点 404。这种红最难分诊，因为它长得像产品坏了。
    "起服务 + 跑门禁写进同一条命令"解决了大半，但拦不住"服务早就起着"。
    所以加这道守卫：版本低于门禁期望就**直接拒绝跑**，并说清楚该干什么。

退出码是契约，shell 靠它决定放不放行：
    0 = 通过（版本达标）
    2 = 服务没起（不挡门禁：有些关自己拉服务，也有些关根本不需要服务）
    3 = **旧进程**（挡住，提示重启）—— 这是唯一会拒绝跑的情况
    4 = 探测本身出错（地址写错/超时）—— 也不挡，但要把话说出来

用法：
    python tools/svc_probe.py                    # 默认探 8756，期望值取 api_contract.API_MIN
    python tools/svc_probe.py --url http://127.0.0.1:8799 --quiet
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    from api_contract import API_MIN
except Exception:                                    # 契约文件没了我也不该崩
    API_MIN = 1


def probe(url, timeout=4.0):
    """返回 (kind, info)。kind ∈ ok / down / old / error。"""
    # ★ 显式不走代理：本机 http_proxy 指向 127.0.0.1:64693，而 GitHub 那条
    #   出口时好时坏。健康检查打的是 127.0.0.1，绕代理既是性能也是**正确性**
    #   —— 走代理会拿到 502 的假"服务挂了"，探针自己变成假红来源。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as r:
            raw = r.read(64 * 1024)
        d = json.loads(raw.decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return "error", {"detail": "HTTP %s" % e.code}
    except (urllib.error.URLError, OSError) as e:
        return "down", {"detail": str(getattr(e, "reason", e))}
    except Exception as e:
        return "error", {"detail": "%s: %s" % (type(e).__name__, e)}

    v = d.get("api_version")
    if v is None:
        # 老服务没有这个字段 —— 它一定是旧进程（新进程必然带）
        return "old", {"info": d, "api_version": None}
    try:
        v = int(v)
    except Exception:
        return "error", {"info": d, "detail": "api_version 不是数字：%r" % (v,)}
    return ("ok" if v >= API_MIN else "old"), {"info": d, "api_version": v}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:%s/api/health"
                    % os.environ.get("VOICESTUDIO_PORT", "8756"))
    ap.add_argument("--expect", type=int, default=API_MIN)
    ap.add_argument("--timeout", type=float, default=4.0)
    ap.add_argument("--quiet", action="store_true", help="通过时不打印")
    a = ap.parse_args()

    kind, info = probe(a.url, a.timeout)
    if kind == "ok":
        if not a.quiet:
            i = info["info"]
            print("✓ 服务契约 v%d（期望 ≥%d，pid %s，起于 %s）"
                  % (info["api_version"], a.expect, i.get("pid", "?"),
                     i.get("started_at", "?")))
        return 0
    if kind == "down":
        if not a.quiet:
            print("· 服务没起（%s）—— 不挡门禁，需要服务的关会自己拉起来"
                  % info.get("detail", "")[:120])
        return 2
    if kind == "old":
        got = info.get("api_version")
        print("★ 这是旧进程：服务接口 v%s，门禁要求 ≥v%d（%s）"
              % ("?" if got is None else got, a.expect, a.url))
        print("  页面能打开不代表接口是新的 —— 这次就是这么假红的。")
        print("  ⇒ 先关掉旧服务，双击 启动VoiceStudio.bat 重启，再跑门禁。")
        return 3
    print("⚠ 契约探针出错：%s" % info.get("detail", ""))
    return 4


if __name__ == "__main__":
    sys.exit(main())
