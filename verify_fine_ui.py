# -*- coding: utf-8 -*-
r"""门禁：精修台**界面**（真浏览器 + 独立端口服务）。

和 verify_finetune 的分工：
  · verify_finetune —— 切块 / 基频 / 诊断 / 渲染 / 分层 的数学 + 真 HTTP 接口；
  · 本关           —— 真页面上的画布、真鼠标点块、拖拽改调、一键修、导出。

为什么必须是真浏览器：精修台是一块 **canvas 钢琴卷帘**。它最典型的三种坏法，
静态检查和接口测试一条都抓不到：
  ① `fineInit()` 没跑到 ⇒ 下拉一个选项都没有，整块面板空着（HTML/JS/接口全"在"）；
  ② 画布的命中测试算的是 canvas 像素（PADL + 时间×pps），偏一个常量就永远点不中；
  ③ 拖拽改调靠 mousedown→mousemove→mouseup 三个事件，漏一个就什么都不发生。

★ 自己起一份独立端口的服务（不碰用户正在用的 8756）：
  常驻进程活不过一条命令（铁律 20），所以"拉起来 + 跑探针 + 收掉"写在同一个 main 里。

用法：
    <venv>/Scripts/python.exe verify_fine_ui.py
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")
sys.path.insert(0, HERE)
sys.path.insert(0, TOOLS)

PORT = int(os.environ.get("VS_FINE_UI_PORT", "8904"))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
N_PASS, N_FAIL = 0, 0


def listen(port, host="127.0.0.1", timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT,
                                    timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("  [ok] %s" % name, flush=True)
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  ← %s" % detail) if detail else ""),
              flush=True)


def start_service():
    import studio
    if not os.path.isfile(studio.PY):
        return None, "找不到 GSV 运行时：%s" % studio.PY
    if listen(PORT):
        return None, "端口 %d 已被别的进程占用（换 VS_FINE_UI_PORT）" % PORT
    env = dict(os.environ)
    env["VOICESTUDIO_PORT"] = str(PORT)
    logp = os.path.join(HERE, "_tmp", "svc_%d.log" % PORT)
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    f = open(logp, "wb")
    p = subprocess.Popen([studio.PY, "-u", os.path.join(HERE, "server.py")],
                         cwd=HERE, env=env, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(0.7)
        if health().get("ok"):
            return p, logp
    return p, logp


def main():
    print("精修台界面门禁：独立端口 %d + 真 Chrome" % PORT, flush=True)
    if not os.path.isfile(CHROME):
        check("找到 Chrome", False, CHROME)
        return 1

    proc, info = start_service()
    if not proc:
        check("在 %d 起一份独立服务" % PORT, False, info)
        return 1
    try:
        h = health()
        check("服务活着", h.get("ok") is True, h)
        fresh = float(h.get("code_mtime") or 0) <= float(h.get("started_ts") or 0) + 2
        check("服务跑的是当前代码（code_mtime <= started_ts）", fresh,
              "code=%s started=%s" % (h.get("code_mtime"), h.get("started_ts")))

        os.environ["VS_PORT"] = str(PORT)
        import probe_fine_ui as P
        rc = P.main()
        check("真浏览器探针全过（0 失败）", rc == 0,
              "探针返回 %s，见上面的 FAIL 行" % rc)
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=15)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    print("\n通过 %d 项，失败 %d 项" % (N_PASS, N_FAIL), flush=True)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
