# -*- coding: utf-8 -*-
r"""门禁：工作台**操作体验**（静态接线 + 真浏览器行为）。

守四件在用户手上会直接影响效率的事：
  ① 找角色 —— 角色只会越来越多（现在 39 个，市场还在加）。没有搜索时只能
     一排排目视扫；搜不到还必须**说清楚**，静默空白会被当成"角色丢了"。
  ② 点击区 —— 实测全页 235 个控件短边 <32px（小按钮 28 / 小输入框 30），
     手一抖就点空。统一抬到 32，且不许把布局挤坏。
  ③ 首屏密度 —— 合成台标题区原来 110 字常驻，把真正要紧那句挤得不好找。
  ④ 文案是否**还成立** —— 2026-10-22 改成"每人只唱分到的那几句"之后，
     前端还写着"每个角色都会把整首唱一遍""每人各唱一遍整首"。
     这类话不会让任何判据变红，但会让人**不敢用多人**（以为要等 N 倍时间）。

为什么要真浏览器那一半：搜索"有没有反应"、点击区"实际多高"、折叠"点得开吗"
都只有渲染后才知道。CSS 里写 32px 被别的规则覆盖成 28px 是常事。

★ 这一关自己在 8897 起服务（不碰用户的 8756），端口和 profile 都与
  verify_multi_ui（8899/9341）错开，两关可以先后跑。
"""
import json
import os
import re
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

PORT = int(os.environ.get("VS_ERGO_PORT", "8897"))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
N_PASS, N_FAIL = 0, 0

# 前端里**已经不再成立**的说法（每条都写清为什么）。
# 加新的一条之前先想清楚：它是不是真的错了？只是"可以更好"不算。
STALE = [
    ("每个角色都会把整首唱一遍",
     "2026-10-22 起每人只唱分到的那几句，不再整首各唱一遍"),
    ("每人各唱一遍整首",
     "独唱轨是整首长度，但只有 TA 分到的那几句有声"),
    ("各把整首唱一遍",
     "同上：分句唱之后这条描述不成立了"),
]


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("  [ok] %s" % name, flush=True)
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  <- " + str(detail)) if detail else ""),
              flush=True)


def read(p):
    with open(os.path.join(HERE, p), encoding="utf-8") as f:
        return f.read()


def stale_hits(text):
    """纯函数：返回文本里命中的过时说法。门禁直接调它测负对照。"""
    return [why for phrase, why in STALE if phrase in text]


def strip_comments(text):
    """剥掉注释再扫。

    ★ 为什么必须剥：这条判据守的是「**用户看得见**的话还成不成立」。
      而注释里提到旧说法是**正常的工程记录**（"这里原来写的是 XX，改成 YY 了"），
      第一版没剥注释，于是把这种说明判成了红 —— 那是尺子错，不是文案错。
      （假红的代价很实在：以后没人敢在注释里写"以前是怎么错的"。）
    ★ 行内 `//` 不动：它可能是 URL 或字符串里的一段，硬剥会把正文一起砍掉。
      只剥**整行**注释，保守但足够 —— 我们自己写的注释都是整行的。
    """
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)      # HTML 注释
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)       # 块注释
    text = re.sub(r"(?m)^\s*//.*$", " ", text)               # 整行注释
    return text


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


def start_service():
    import studio
    if not os.path.isfile(studio.PY):
        return None, "找不到 GSV 运行时：%s" % studio.PY
    if listen(PORT):
        return None, "端口 %d 已被占用（换 VS_ERGO_PORT）" % PORT
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


def a_static():
    """静态：接线在不在、文案过没过时、样式抬没抬。"""
    print("\n---------- A 静态接线 ----------", flush=True)
    html = read("web/index.html")
    app = read("web/app.js")
    css = read("web/style.css")

    check("角色区有搜索框（id=roleFind，type=search）",
          'id="roleFind"' in html and 'type="search"' in html)
    check("搜索框接在 oninput 上（不是摆设）",
          re.search(r'id="roleFind"[^>]*oninput="roleFind\(\)"', html) is not None
          or re.search(r'oninput="roleFind\(\)"[^>]*id="roleFind"', html) is not None)
    check("有清空按钮 roleFindX", 'id="roleFindX"' in html and "roleFindClear()" in html)
    check("有「没找到」提示块，且回显用户输入的词",
          'id="findEmpty"' in html and 'id="findEmptyQ"' in html)
    check("app.js 定义 roleFind / roleFindClear / roleMatch",
          all(("function %s(" % n) in app for n in ("roleFind", "roleFindClear", "roleMatch")))
    check("render() 真的用搜索词过滤（不是只改了计数行）",
          re.search(r"const list = q \? ROLES\.filter", app) is not None)
    check("计数行会显示「找到 N 个」", "找到 ${list.length} 个" in app)
    check(".btn.sm 抬到 min-height:32px",
          re.search(r"\.btn\.sm\{[^}]*min-height:32px", css) is not None)

    # 过时文案：负对照先行，证明这个函数抓得住
    probe = "这里写着：每个角色都会把整首唱一遍，所以很慢。"
    check("负对照：过时文案能被抓出来（尺子有牙）",
          len(stale_hits(probe)) > 0, stale_hits(probe))
    check("负对照：改对之后的说法不该被抓", not stale_hits("每人只唱分到自己那几句"))
    # 剥离器自检：注释里的旧话不该被抓，正文里的必须被抓。
    # 少了这一对，剥注释很容易连正文一起剥掉 —— 那样整条判据就永远绿了。
    cmt = '<!-- 以前这里写的是：每个角色都会把整首唱一遍 --><b>正文</b>'
    check("负对照：剥注释后，注释里的旧话不再误判",
          not stale_hits(strip_comments(cmt)))
    live = '<b>每个角色都会把整首唱一遍</b>'
    check("★ 剥注释不会连正文一起剥掉（正文里的旧话仍然被抓）",
          len(stale_hits(strip_comments(live))) > 0)
    hits = []
    for f in ("web/index.html", "web/app.js", "web/lib.js", "web/loc.js",
              "web/mkt.js", "web/viz.js"):
        h = stale_hits(strip_comments(read(f)))
        if h:
            hits.append((f, h))
    check("前端没有残留的过时说法（整首各唱一遍那套）", not hits, hits[:2])


def b_browser():
    """真浏览器：把探针跑掉，并解析它自己的计数（不然汇总只剩 1 条）。"""
    print("\n---------- B 真浏览器 ----------", flush=True)
    env = dict(os.environ)
    env["VS_PORT"] = str(PORT)
    env["VS_CDP_PORT"] = "9342"
    r = subprocess.run([sys.executable, "-X", "utf8",
                        os.path.join(TOOLS, "probe_ergonomics.py")],
                       cwd=HERE, env=env, capture_output=True)
    out = (r.stdout or b"").decode("utf-8", "replace")
    err = (r.stderr or b"").decode("utf-8", "replace").strip()
    for line in out.splitlines():
        print("  " + line if line.strip() else line, flush=True)
    if err:
        print("  [stderr] " + err[-600:], flush=True)
    m = re.findall(r"通过 (\d+) 项，失败 (\d+) 项", out)
    if not m:
        check("探针跑出了结论（有汇总行）", False, (err or out)[-200:])
        return
    p, f = int(m[-1][0]), int(m[-1][1])
    global N_PASS, N_FAIL
    N_PASS += p
    N_FAIL += f
    # ★ 解析到计数就**不再**单独 check 退出码：探针里每一条已经各自算过分，
    #   外面再补一条"退出码非 0"会把同一件事扣两遍（第一版就是这样，
    #   1 条真红显示成 3 条红，看着像坏了三处）。
    print("  [ok] 探针跑完（%d 项真浏览器判据，失败 %d）"
          % (p, f) if f == 0 else
          "  [!!] 探针跑完（%d 项，失败 %d —— 明细见上）" % (p, f), flush=True)


def main():
    print("操作体验门禁：静态 + 真 Chrome（独立端口 %d）" % PORT, flush=True)
    a_static()

    if not os.path.isfile(CHROME):
        check("找到 Chrome", False, CHROME)
    else:
        proc, info = start_service()
        if not proc:
            check("在 %d 起一份独立服务" % PORT, False, info)
        else:
            try:
                h = health()
                check("服务活着", h.get("ok") is True)
                fresh = float(h.get("code_mtime") or 0) <= float(h.get("started_ts") or 0) + 2
                check("服务跑的是当前代码（不是旧进程）", fresh,
                      "code=%s started=%s" % (h.get("code_mtime"), h.get("started_ts")))
                if h.get("ok") and fresh:
                    b_browser()
                else:
                    check("服务是新代码，才跑真浏览器", False, "旧进程会给出假红")
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
    print("ALLDONE", flush=True)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
