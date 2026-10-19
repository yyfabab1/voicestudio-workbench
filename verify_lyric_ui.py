# -*- coding: utf-8 -*-
r"""翻唱台「改词」那块 UI 的真浏览器验收。

规矩（踩过多次的坑，别再犯）：
· 真鼠标：用 Input.dispatchMouseEvent + 命中测试，**禁 element.click()**。
  element.click() 绕过命中测试，"根本点不到的东西"也会报成功。
· 判据必须能红：脚本末尾跑负对照（把句数改成不一致），
  断言提示文字**变了** —— 没红过的判据等于没验。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

# ★ 本机设了 http_proxy，urllib/websocket 默认会连 127.0.0.1:9334 也走代理
#   ⇒ 拿回来的是代理的 502，看起来像"浏览器起不来"。这里直接把代理关掉。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:8756/"
PORT = 9335
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, ".cdp-lyric")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name +
          (("  | " + detail) if detail else ""), flush=True)


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
        # 页面弹 confirm/alert 时一律"确定"（改词台的原词覆盖确认要接受，
        # 否则我们测不到回填）。
        self.accept_dialog = True
        # ★ 必须开 Page 域，否则收不到 javascriptDialogOpening 事件。
        self.send("Page.enable")

    def send(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            # ★ 页面弹出 confirm/alert 时必须**立刻**用 CDP 处理掉。
            #   不处理的后果（踩过）：页面主线程一直挂在对话框上，之后每个
            #   Runtime.evaluate 都得不到响应，60 秒后报
            #   WebSocketTimeoutException —— 看着像"浏览器断了"，其实是等对话框。
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
        """先滚进视口再真鼠标点（视口外的点必然打空）。"""
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
    """用原生 setter + dispatchEvent 触发 input —— 模拟真实输入的监听路径，
    直接改 .value 不会触发 app.js 里绑的 input 回调。"""
    c.js("""(()=>{const e=document.querySelector(%s);
        const s=Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype,'value').set;
        s.call(e, %s); e.dispatchEvent(new Event('input',{bubbles:true})); return true;})()"""
         % (json.dumps(sel), json.dumps(text)))
    time.sleep(0.25)


def main():
    # 先试连已经在跑的那个（重复跑脚本时不用每次都新开一个浏览器）；
    # 连不上再自己拉起来。chrome 不会自己建 user-data-dir，先建好。
    c = None
    try:
        cdp_get("/json/list", PORT)
        c = Cdp(PORT)
    except Exception as e:
        print("  [debug] 复用已有浏览器失败：%s: %s" % (type(e).__name__, str(e)[:200]))
    if c is None:
        os.makedirs(PROFILE, exist_ok=True)
        subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu",
             "--remote-debugging-port=%d" % PORT,
             "--user-data-dir=%s" % PROFILE, "--no-first-run",
             "--remote-allow-origins=*",
             "--window-size=1440,1000", URL],
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
    print("翻唱台改词 UI 验收")
    print("=" * 62)

    # ★ 每次都在**干净的页面**上跑。复用已有浏览器时，页面还留着上一轮的
    #   状态（填过的词、展开的折叠区、上一次识别出来的结果），断言会被污染 ——
    #   实测第二次跑就因为这些残留而出现假红。
    try:
        c.send("Page.navigate", url=URL)
        time.sleep(3.0)
    except Exception as e:
        print("  [debug] 刷新页面失败：%s" % str(e)[:120])

    # --- 1. 元素在不在 ---
    for sel, name in [("#sgLyricBox", "改词折叠区"), ("#sgOrig", "原歌词输入框"),
                      ("#sgNew", "新歌词输入框"), ("#sgLyricHint", "句数提示")]:
        check(name + " 存在", bool(c.js("!!document.querySelector(%s)" % json.dumps(sel))))

    # --- 2. 默认收起（不增加常规翻唱的视觉负担）---
    check("默认收起", c.js("document.querySelector('#sgLyricBox').open") is False,
          "open=%s" % c.js("document.querySelector('#sgLyricBox').open"))

    # --- 3. 真鼠标点开 ---
    hit = c.click_el("#sgLyricBox summary")
    time.sleep(0.4)
    opened = c.js("document.querySelector('#sgLyricBox').open")
    check("真鼠标能点开", opened is True, "命中 %s" % hit)

    # 展开后两个框都要在视口里、且有面积 —— 否则用户根本看不见/点不到
    for sel, name in [("#sgOrig", "原词框"), ("#sgNew", "新词框")]:
        box = c.js("""(()=>{const e=document.querySelector(%s); if(!e) return null;
            const r=e.getBoundingClientRect();
            return {x:r.left,y:r.top,w:r.width,h:r.height,vh:innerHeight,vw:innerWidth};})()"""
                   % json.dumps(sel))
        vis = bool(box and box["w"] > 40 and box["h"] > 20
                   and 0 <= box["y"] <= box["vh"] and 0 <= box["x"] <= box["vw"])
        check(name + " 可见且有面积", vis, json.dumps(box) if box else "null")

    # --- 4. 提示随句数变化（4 句 vs 4 句）---
    set_val(c, "#sgOrig", "一句词|两句词|三句词|四句词")
    set_val(c, "#sgNew", "新的一|新的二|新的三|新的四")
    h1 = c.js("document.querySelector('#sgLyricHint').textContent") or ""
    check("句数一致时提示「会逐句对上」", "句数一致" in h1, h1[:60])

    # --- 5. 负对照：句数改成 4 vs 3，提示必须变 ---
    set_val(c, "#sgNew", "新的一|新的二|新的三")
    h2 = c.js("document.querySelector('#sgLyricHint').textContent") or ""
    check("句数不一致时提示「节奏可能偏」", "句数不一样" in h2, h2[:60])
    check("★ 负对照：提示确实变了（判据能红）", h1 != h2,
          "一致=[%s] 不一致=[%s]" % (h1[:24], h2[:24]))

    # --- 6. 清空回到默认文案 ---
    set_val(c, "#sgOrig", "")
    set_val(c, "#sgNew", "")
    h3 = c.js("document.querySelector('#sgLyricHint').textContent") or ""
    check("清空后回到默认文案", "普通翻唱" in h3, h3[:60])

    # --- 7. 「口哨段」下拉（这一轮为"一吹口哨就开始吼"加的处理方式开关）---
    nv = c.js("""(()=>{const e=document.querySelector('#sgNonvocal'); if(!e) return null;
        return {ops:[...e.options].map(o=>o.value), val:e.value};})()""")
    check("口哨段下拉三档齐全且默认 keep",
          bool(nv) and nv["ops"] == ["keep", "mute", "off"] and nv["val"] == "keep",
          json.dumps(nv, ensure_ascii=False))

    # --- 8. 「自动识别原歌词」---
    check("自动识别按钮存在", bool(c.js("!!document.querySelector('#btnAutoLyric')")))

    # ★ 负对照 A：**没选歌**就点 —— 必须只提示、不进入识别态。
    #   它是"判据能红"的基准：识别态的文案与禁用状态都必须与它不同。
    c.js("if (typeof STAGE_SONG !== 'undefined') { STAGE_SONG = null; }")
    set_val(c, "#sgOrig", "")
    time.sleep(0.3)
    hint_a = c.js("document.querySelector('#autoLyricHint').textContent") or ""
    dis_a = c.js("document.querySelector('#btnAutoLyric').disabled")
    hit_a = c.click_el("#btnAutoLyric")
    time.sleep(1.0)
    hint_b = c.js("document.querySelector('#autoLyricHint').textContent") or ""
    dis_b = c.js("document.querySelector('#btnAutoLyric').disabled")
    check("★ 负对照：未选歌时点按钮 → 不进识别态",
          (hint_b == hint_a) and (dis_a is False) and (dis_b is False),
          "命中 %s | hint 保持=[%s] | disabled %s→%s"
          % (hit_a, hint_b[:28], dis_a, dis_b))

    # 正例：把歌放进②号槽，再用**真鼠标**点
    SONG = r"D:\CloudMusic\嘟比Dubi - 神耀（星际争霸同人歌）.mp3"
    # ★ 先清掉歌词缓存，让这次识别**真的跑一遍**。两个理由：
    #   ① 缓存命中时任务一秒就结束，我们观察不到"识别中"这个中间态，
    #      判据会假红（实测就是这样）；
    #   ② 验收不该依赖运行时残留 —— 缓存里有没有东西取决于之前谁跑过。
    _lc = os.path.join(HERE, "data", "cache", "lyrics")
    if os.path.isdir(_lc):
        for _f in os.listdir(_lc):
            if _f.endswith(".json"):
                try:
                    os.remove(os.path.join(_lc, _f))
                except OSError:
                    pass
        print("  [info] 已清空歌词缓存，本次识别会真跑")
    c.js("stageSetSong(%s, %s);" % (json.dumps(SONG), json.dumps("神耀.mp3")))
    time.sleep(0.4)
    hit_c = c.click_el("#btnAutoLyric")
    time.sleep(1.5)
    hint_c = c.js("document.querySelector('#autoLyricHint').textContent") or ""
    dis_c = c.js("document.querySelector('#btnAutoLyric').disabled")
    check("真鼠标点得到自动识别按钮",
          not str(hit_c).startswith(("NOT_FOUND", "NO_BOX", "OUT_OF")), "命中 %s" % hit_c)
    check("点击后进入识别态（提示变化 + 按钮禁用）",
          ("识别中" in hint_c) and dis_c is True,
          "hint=[%s] disabled=%s" % (hint_c[:40], dis_c))

    # 等回填（分离有缓存时整条约 15 秒）。
    # ★ 2026-10-14 踩到一次：上一版只等 60×2s = 120 秒，而**服务刚重启后第一次**
    #   识别（模型冷加载 + 分离无缓存）实测会超过 120 秒 ⇒ 三条判据一起红，
    #   看起来像"回填坏了"，其实只是没等够。给到 90×2s = 180 秒，
    #   并且超时时把**页面上的真实状态**打出来 —— 真失败要能一眼看出不是超时。
    line = ""
    t0 = time.time()
    for _ in range(90):
        time.sleep(2)
        try:
            line = (c.js("document.querySelector('#sgOrig').value") or "").strip()
        except Exception as e:      # 单次读取失败不该让整场验收崩掉，继续等
            print("  [debug] 轮询读取失败：%s" % str(e)[:120], flush=True)
            line = ""
        if line:
            break
    used = time.time() - t0
    if not line:
        try:
            stuck = (c.js("document.querySelector('#autoLyricHint').textContent")
                     or "")[:60]
        except Exception:                                    # noqa: BLE001
            stuck = "读不到"
        print("  [debug] 等了 %.0f 秒仍没回填，页面状态：%s" % (used, stuck), flush=True)
    check("识别结果已回填到「原歌词」栏", len(line) >= 20,
          "等了 %.0f 秒；长度 %d，前 60 字：%s" % (used, len(line), line[:60]))
    # Whisper 的中文原生输出是**繁体**，功能必须已经转成简体 ——
    # 否则用户还得自己转一遍，"不用自己搞"就没兑现。
    TRAD = "風雲來個為與時鐘聲後裏隻們這點開關戰"
    bad_trad = [ch for ch in TRAD if ch in line]
    check("回填文本已转简体（不含常见繁体字）", not bad_trad,
          "命中繁体字：%s" % ("".join(bad_trad) if bad_trad else "无"))
    # 分句不能有超长句（没有时间戳式分句时，整段间奏会被算进一句，实测出现过 25 秒的句）
    segs = [s for s in line.split("|") if s.strip()]
    check("识别出的句数合理（>=8）", len(segs) >= 8, "句数 %d" % len(segs))
    h_fin = c.js("document.querySelector('#autoLyricHint').textContent") or ""
    check("提示显示已填入句数", "已填入" in h_fin, h_fin[:60])

    # --- 9. 截图 ---
    shot = os.path.join(HERE, "lyric_ui.png")
    data = c.send("Page.captureScreenshot", format="png")["data"]
    import base64
    with open(shot, "wb") as f:
        f.write(base64.b64decode(data))
    check("截图已存", os.path.getsize(shot) > 5000, shot)

    print("-" * 62)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 失败项：" + "、".join(BAD))
    else:
        print("结论：改词 UI 验收全绿")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
