# -*- coding: utf-8 -*-
r"""声音市场那块 UI 的**真浏览器**验收。

规矩（跟其它 UI 关一致，踩过的坑别再犯）：
· 真鼠标：Input.dispatchMouseEvent + 命中测试，**禁 element.click()**。
  element.click() 绕过命中测试，"根本点不到的东西"也会报成功。
· 判据必须能红：每段都配负对照（搜乱码必须"没搜到"、空关键词不该发起搜索、
  不存在的仓库不能 500）。没红过的判据等于没验。
· 页面状态会残留（上一轮搜出来的卡片还挂着），所以每次先 Page.navigate 重新加载。

用法（venv：要 websocket）：  <venv>/Scripts/python.exe -s verify_market_ui.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

# ★ 本机设了 http_proxy，urllib 默认连 127.0.0.1 也走代理 ⇒ 拿回来的是代理的 502，
#   看起来像"浏览器起不来"。这里直接把代理关掉。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8756/"
PORT = 9339          # 跟其它 UI 关错开，别抢同一个调试端口
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, ".cdp-market")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def cdp_get(path, port=PORT):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=20) as r:
        return json.loads(r.read().decode())


class Cdp:
    def __init__(self, port=PORT):
        import websocket  # noqa
        ws = cdp_get("/json/list", port)
        page = [t for t in ws if t.get("type") == "page"]
        if not page:
            raise RuntimeError("没有可用的页面目标")
        self.ws = websocket.create_connection(page[0]["webSocketDebuggerUrl"], timeout=60)
        self.i = 0
        self.accept_dialog = True
        self.send("Page.enable")

    def send(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            # 页面弹对话框必须立刻处理掉，否则主线程挂住 ⇒ 之后每个 evaluate 都超时
            if msg.get("method") == "Page.javascriptDialogOpening":
                self.i += 1
                self.ws.send(json.dumps({
                    "id": self.i, "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": bool(self.accept_dialog)}}))
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def click_el(self, selector):
        """先滚进视口再真鼠标点（视口外的点必然打空 —— 这条踩过）。"""
        ok = self.js("(()=>{const e=document.querySelector(%s);"
                     "if(!e) return false; e.scrollIntoView({block:'center'}); return true;})()"
                     % json.dumps(selector))
        time.sleep(0.35)
        if not ok:
            return "NOT_FOUND:" + selector
        xy = self.js("(()=>{const e=document.querySelector(%s); if(!e) return null;"
                     "const r=e.getBoundingClientRect();"
                     "return {x:r.left+r.width/2, y:r.top+r.height/2};})()"
                     % json.dumps(selector))
        if not xy:
            return "NO_BOX:" + selector
        x, y = xy["x"], xy["y"]
        off = self.js("(()=>{return (%f<0||%f>innerHeight||%f<0||%f>innerWidth);})()"
                      % (y, y, x, x))
        if off:
            return "OUT_OF_VIEWPORT"
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.id||e.className||e.tagName):'null'})()" % (x, y))
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                  button="left", clickCount=1)
        time.sleep(0.06)
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                  button="left", clickCount=1)
        return top


def set_val(c, sel, text):
    """用原生 setter + dispatchEvent 触发 input —— 直接改 .value 不会触发监听。"""
    c.js("""(()=>{const e=document.querySelector(%s);
        const s=Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype,'value').set;
        s.call(e, %s); e.dispatchEvent(new Event('input',{bubbles:true})); return true;})()"""
         % (json.dumps(sel), json.dumps(text)))
    time.sleep(0.25)


def api_get(path):
    with urllib.request.urlopen(BASE.rstrip("/") + path, timeout=60) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


def wait_cards(c, timeout=150.0):
    """等卡片出现（市场要逐个仓库问文件清单，慢）。返回 (卡片数, 文本)。"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        n = c.js("document.querySelectorAll('#mktGrid .lib-card').length") or 0
        txt = c.js("document.querySelector('#mktGrid').textContent") or ""
        if n or ("没搜到" in txt) or ("失败" in txt):
            return n, txt
        time.sleep(2.0)
    return 0, c.js("document.querySelector('#mktGrid').textContent") or ""


def main():
    c = None
    try:
        cdp_get("/json/list", PORT)
        c = Cdp(PORT)
    except Exception as e:
        print("  [debug] 复用已有浏览器失败：%s: %s" % (type(e).__name__, str(e)[:160]))
    if c is None:
        os.makedirs(PROFILE, exist_ok=True)
        subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu",
             "--remote-debugging-port=%d" % PORT,
             "--user-data-dir=%s" % PROFILE, "--no-first-run",
             "--remote-allow-origins=*",
             "--window-size=1440,1000", BASE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(1.0)
            try:
                cdp_get("/json/list", PORT)
                time.sleep(1.5)
                c = Cdp(PORT)
                break
            except Exception:
                continue
    if not c:
        print("!! 浏览器起不来")
        return 2

    print("=" * 62)
    print("声音市场 UI 验收")
    print("=" * 62)

    # ★ 每次都在干净页面上跑：上一轮搜出来的卡片、展开的状态会污染断言
    try:
        c.send("Page.navigate", url=BASE)
        time.sleep(3.5)
    except Exception as e:
        print("  [debug] 刷新页面失败：%s" % str(e)[:120])

    # ---------------------------------------------------------------- A. 元素与文案
    print("\n=== A. 元素在不在 / 边界写没写清楚 ===")
    for sel, name in [("#mktPanel", "市场面板"), ("#mktQ", "搜索框"),
                      ("#btnMktSearch", "搜索按钮"), ("#mktWorks", "作品 chip 区"),
                      ("#mktGrid", "结果网格"), ("#mktNote", "说明行"),
                      ("#mktMine", "已装列表")]:
        check(name + " 存在", bool(c.js("!!document.querySelector(%s)" % json.dumps(sel))))

    head = c.js("document.querySelector('#mktPanel').textContent") or ""
    check("界面上写明「不破解任何付费内容」", "不破解任何付费内容" in head)
    check("界面上写明 license 要自己看", "license" in head.lower() or "许可" in head)

    # ---------------------------------------------------------------- B. 作品 chip
    print("\n=== B. 作品 chip（用户点名的两部必须在）===")
    chips = c.js("Array.from(document.querySelectorAll('#mktWorks button[data-work]'))"
                 ".map(b=>b.textContent.trim())") or []
    check("作品 chip 渲染出来了", len(chips) >= 10, "%d 个" % len(chips))
    check("★ 有「小马宝莉」", any("小马宝莉" in t for t in chips))
    check("★ 有「地狱客栈」", any("地狱客栈" in t for t in chips))

    # 真鼠标点「地狱客栈」—— 这是用户点名要的，点不动等于没有
    hit = c.click_el("#mktWorks button[data-work='hazbin']")
    time.sleep(0.5)
    check("真鼠标能点上「地狱客栈」chip", not str(hit).startswith(("NOT_FOUND", "NO_BOX",
                                                                 "OUT_OF_VIEWPORT")), hit)
    n, txt = wait_cards(c)
    check("点了作品之后出结果（卡片或明确的「没搜到」）",
          n > 0 or "没搜到" in txt, "%d 张卡 / %s" % (n, txt[:80]))
    note = c.js("document.querySelector('#mktNote').textContent") or ""
    check("说明行写清了搜到几个（或为什么没有）", len(note) > 0, note[:90])

    # ----------------------------------------------------------- B2. 按标签逛
    print("\n=== B2. 按标签逛 + 加载更多（「找到更多」的入口）===")
    tagchips = c.js("Array.from(document.querySelectorAll('#mktTags button[data-tag]'))"
                    ".map(b=>b.textContent.trim())") or []
    check("标签 chip 渲染出来了", len(tagchips) >= 3, "%d 个" % len(tagchips))
    hit = c.click_el("#mktTags button[data-tag='rvc']")
    check("真鼠标能点上标签 chip", not str(hit).startswith(("NOT_FOUND", "NO_BOX",
                                                           "OUT_OF_VIEWPORT")), hit)
    n1, _ = wait_cards(c)
    check("★ 按标签逛能出一大片（≥10 张卡）—— 关键词搜索一次只有十来个",
          n1 >= 10, "%d 张" % n1)
    # ★ 按**卡片**取仓库，不能按按钮：合集卡片合法地有两个按钮（装全部 / 只装
    #   第一个），按按钮去重的话，有 N 张合集卡就必然"多出" N 个重复 ——
    #   这一条实测就是这么假红过一次（45 张卡 65 个按钮 ⇒ 20 张合集卡，完全对得上）。
    REPOS_JS = ("Array.from(document.querySelectorAll('#mktGrid .lib-card'))"
                ".map(c=>{const b=c.querySelector('button[data-install]');"
                "return b?b.dataset.install:''}).filter(Boolean)")
    seen1 = set(c.js(REPOS_JS) or [])
    has_more = c.js("!!document.querySelector('#btnMktMore')")
    check("底下有「加载更多」按钮", bool(has_more))
    if has_more:
        hit2 = c.click_el("#btnMktMore")
        check("真鼠标能点上「加载更多」",
              not str(hit2).startswith(("NOT_FOUND", "NO_BOX", "OUT_OF_VIEWPORT")), hit2)
        n_now = 0
        for _ in range(120):
            time.sleep(1)
            n_now = c.js("document.querySelectorAll('#mktGrid .lib-card').length") or 0
            if n_now > n1:
                break
        check("★ 点「加载更多」卡片真的变多了（翻页不是摆设）",
              n_now > n1, "%d -> %d 张" % (n1, n_now))
        now = c.js(REPOS_JS) or []
        fresh = [r for r in now if r not in seen1]
        # ★ 承重判据：翻出来必须是**没见过**的仓库，且整屏不出现重复仓库。
        #   没有这条，"加载更多"把同一页再画一遍也能变绿（实测这就是最容易糊弄过去的地方）。
        check("★ 翻出来的是新仓库，且整屏没有重复的",
              len(fresh) > 0 and len(set(now)) == len(now),
              "新增 %d / 共 %d 张卡 / 去重后 %d" % (len(fresh), len(now), len(set(now))))

    # ---------------------------------------------------------------- C. 自由搜索
    print("\n=== C. 搜索框（真鼠标点按钮）===")
    set_val(c, "#mktQ", "Alastor")
    hit = c.click_el("#btnMktSearch")
    time.sleep(0.5)
    check("真鼠标能点上搜索按钮", not str(hit).startswith(("NOT_FOUND", "NO_BOX",
                                                         "OUT_OF_VIEWPORT")), hit)
    n2, txt2 = wait_cards(c)
    check("搜角色名出结果", n2 > 0 or "没搜到" in txt2, "%d 张卡 / %s" % (n2, txt2[:80]))
    if n2:
        btns = c.js("document.querySelectorAll('#mktGrid button[data-install]').length") or 0
        check("卡片上有「安装」按钮（能下载是硬需求）", btns > 0, "%d 个" % btns)
        # 搜 Alastor ⇒ 卡片上得显示中文名（入库翻译也要在界面上能看见）
        txt = c.js("document.querySelector('#mktGrid').textContent") or ""
        check("搜 Alastor ⇒ 卡片上显示中文名「阿拉斯托」", "阿拉斯托" in txt,
              txt[:70].replace("\n", " "))
    # 合集要能**单独挑一个**装：卡片上得有「装这个」，且能把包列表展开
    packs_btns = c.js("Array.from(document.querySelectorAll('#mktGrid button[data-install]'))"
                      ".filter(b=>b.textContent.indexOf('装这个')>=0).length") or 0
    check("合集卡片上有「装这个」（不用整包全下）", packs_btns > 0 or
          c.js("!!document.querySelector('#mktGrid button[data-expand]')"),
          "装这个 %d 个 / 有展开按钮 %s" % (packs_btns,
                                        c.js("!!document.querySelector('#mktGrid button[data-expand]')")))

    # ---------------------------------------------------------------- D. 负对照
    print("\n=== D. 负对照：坏输入不能假装成功 ===")
    # ★ 搜乱码：必须明确"没搜到"，既不能报错也不能凭空出卡片。
    #   没有这条，"接口挂了"和"真的没有"在界面上长得一模一样。
    set_val(c, "#mktQ", "zzqwxvbn9981nothing")
    c.click_el("#btnMktSearch")
    n3, txt3 = wait_cards(c)
    check("★ 搜乱码 ⇒ 明确显示「没搜到」（不是报错、也不是空卡片堆）",
          n3 == 0 and ("没搜到" in txt3), "%d 张卡 / %s" % (n3, txt3[:80]))

    # ★ 空关键词：不该发起搜索（界面保持原样）。以前这种"点了个寂寞"最容易做成
    #   转圈半天然后报一句看不懂的错。
    before = c.js("document.querySelector('#mktGrid').textContent") or ""
    set_val(c, "#mktQ", "")
    c.click_el("#btnMktSearch")
    time.sleep(2.5)
    after = c.js("document.querySelector('#mktGrid').textContent") or ""
    busy = c.js("document.querySelector('#btnMktSearch').disabled")
    check("★ 空关键词不发起搜索（按钮没卡在「搜索中」）", busy is False,
          "disabled=%s" % busy)

    # ---------------------------------------------------------------- E. 已装列表
    print("\n=== E. 已装角色区 ===")
    mine = c.js("document.querySelector('#mktMine').textContent") or ""
    check("已装区有内容（列表或「还没装过」的提示）", len(mine.strip()) > 0, mine[:60])

    # ---------------------------------------------------------------- F. 接口接线
    print("\n=== F. 接口接线（界面之外，接口本身也不能崩）===")
    try:
        st, j = api_get("/api/models/works")
        check("/api/models/works 通且非空", st == 200 and len(j.get("works") or []) >= 10,
              "HTTP %d / %d 部" % (st, len(j.get("works") or [])))
    except Exception as e:
        check("/api/models/works 通且非空", False, str(e)[:120])
    # ★ 不存在的仓库不能返回 5xx："查无此仓库"是请求本身不对（4xx），不是服务端
    #   或上游坏了。混成 5xx 之后前端一律显示"网络不通"，用户会以为再点一次就有。
    #   （实测这里还挂过 60 秒 —— 那种"转圈半天"比报错更难查，所以顺带验耗时。）
    t0 = time.time()
    try:
        st2, j2 = api_get("/api/models/detail?repo=__no__/__such__repo__")
        code, body = st2, json.dumps(j2, ensure_ascii=False)[:80]
    except urllib.error.HTTPError as e:
        code, body = e.code, (e.read()[:80].decode("utf-8", "replace")
                              if hasattr(e, "read") else "")
    except Exception as e:
        code, body = -1, str(e)[:120]
    dt = time.time() - t0
    check("★ 不存在的仓库返回 4xx（不是 5xx，也不是挂住）",
          400 <= code < 500, "HTTP %s / %.1fs / %s" % (code, dt, body))
    check("★ 不存在的仓库几秒内就有答复（不能转圈半分钟）", dt < 20, "%.1fs" % dt)

    print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 红的：" + "；".join(BAD))
        return 1
    print("结论：声音市场 UI 门禁全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
