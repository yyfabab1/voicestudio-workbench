# -*- coding: utf-8 -*-
r"""翻唱台「写一首新歌」入口的真浏览器验收。

规矩（和 verify_lyric_ui 一致，踩过的坑别再犯）：
· 真鼠标：Input.dispatchMouseEvent + 命中测试，**禁 element.click()**。
· 判据必须能红：末尾用负对照把页面改成"两个面板同时显示"，断言判据变红。

验的是"用户能不能真的点到、填进去、发出去"，不是"函数存不存在"。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

# ★ 本机设了 http_proxy，连 127.0.0.1 也会走代理 ⇒ 拿回 502，看着像浏览器起不来。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8756"
URL = BASE + "/"
PORT = 9337
HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, ".cdp-yue2ui")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name +
          (("  | " + detail) if detail else ""), flush=True)
    return bool(cond)


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
        if y < 0 or y > self.js("innerHeight") or x < 0 or x > self.js("innerWidth"):
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
    """用原生 setter + dispatchEvent —— 直接改 .value 不会触发 app.js 的监听。"""
    c.js("""(()=>{const e=document.querySelector(%s); if(!e) return false;
        const proto = (e.tagName==='TEXTAREA') ? window.HTMLTextAreaElement.prototype
                                               : window.HTMLInputElement.prototype;
        const s=Object.getOwnPropertyDescriptor(proto,'value').set;
        s.call(e, %s); e.dispatchEvent(new Event('input',{bubbles:true})); return true;})()"""
         % (json.dumps(sel), json.dumps(text)))
    time.sleep(0.2)


def visible(c, sel):
    """元素可见（有面积、在视口内、不是 display:none）。"""
    return c.js("""(()=>{const e=document.querySelector(%s); if(!e) return null;
        const r=e.getBoundingClientRect(); const st=getComputedStyle(e);
        return {w:r.width,h:r.height,x:r.left,y:r.top,
                shown:(st.display!=='none' && st.visibility!=='hidden'),
                vh:innerHeight, vw:innerWidth};})()""" % json.dumps(sel))


def is_shown(box):
    return bool(box and box["shown"] and box["w"] > 10 and box["h"] > 10
                and box["y"] < box["vh"] and box["y"] + box["h"] > 0)


def latest_job():
    """data/jobs 下最新的任务目录名。"""
    d = os.path.join(HERE, "data", "jobs")
    if not os.path.isdir(d):
        return None
    items = []
    for n in os.listdir(d):
        p = os.path.join(d, n)
        if os.path.isdir(p):
            try:
                items.append((os.path.getmtime(p), n))
            except Exception:
                pass
    items.sort()
    return items[-1][1] if items else None


def job_info(jid):
    try:
        with urllib.request.urlopen("%s/api/job/%s" % (BASE, jid), timeout=15) as r:
            d = json.loads(r.read().decode())
            return d.get("job") or d
    except Exception:
        return {}


def main():
    c = None
    try:
        cdp_get("/json/list", PORT)
        c = Cdp(PORT)
    except Exception:
        pass
    if c is None:
        os.makedirs(PROFILE, exist_ok=True)
        subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu",
             "--remote-debugging-port=%d" % PORT,
             "--user-data-dir=%s" % PROFILE, "--no-first-run",
             "--remote-allow-origins=*",
             "--window-size=1440,1100", URL],
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
    if c is None:
        print("! FAIL  起不了浏览器")
        return 1

    # 每轮都在干净页面上跑（复用浏览器会带着上一轮的填写内容）
    try:
        c.send("Page.navigate", url=URL)
        time.sleep(3.0)
    except Exception as e:
        print("  [debug] 刷新失败：%s" % str(e)[:120])

    # ---- 0. 先走到「唱歌」页 ----
    # ★ 打开页面落在角色列表，`#paneSing` 还没显示 ⇒ 里面所有元素的
    #   getBoundingClientRect 都是 0×0，判据会全红（第一版就是这么红的：
    #   11 绿 7 红，红项全是 w=0 h=0）。真用户也要点两下才到得了这里，
    #   所以这两下必须用真鼠标点过去。
    n_card = c.js("document.querySelectorAll('.card[data-role]').length")
    check("角色列表里有卡片", (n_card or 0) > 0, "n=%s" % n_card)
    hit_card = c.click_el(".card[data-role]")
    time.sleep(1.4)
    check("真鼠标点角色卡片能进详情页", bool(c.js("!!document.querySelector('#paneSing')")),
          "命中 %s" % hit_card)
    hit_tab = c.click_el('.tab[data-pane="paneSing"]')
    time.sleep(0.9)
    check("真鼠标点「唱歌」标签能切到唱歌页", is_shown(visible(c, "#paneSing")),
          "命中 %s｜%s" % (hit_tab, json.dumps(visible(c, "#paneSing"))))

    # ---- 1. 元素都在 ----
    for sel, name in [("#smCover", "「翻唱现成的歌」按钮"),
                      ("#smNew", "「写一首新歌」按钮"),
                      ("#singBlock", "翻唱面板"),
                      ("#newSongBlock", "写歌面板"),
                      ("#nsStyle", "风格输入"),
                      ("#nsLyrics", "歌词输入"),
                      ("#nsDur", "时长输入"),
                      ("#btnNewSong", "提交按钮")]:
        check(name + " 存在", bool(c.js("!!document.querySelector(%s)" % json.dumps(sel))))

    # ---- 2. 默认是翻唱，写歌面板收起 ----
    check("默认显示翻唱面板", is_shown(visible(c, "#singBlock")),
          json.dumps(visible(c, "#singBlock")))
    check("默认收起写歌面板", not is_shown(visible(c, "#newSongBlock")),
          json.dumps(visible(c, "#newSongBlock")))

    # ---- 3. 真鼠标点「写一首新歌」 ----
    hit = c.click_el("#smNew")
    time.sleep(0.6)
    nb = visible(c, "#newSongBlock")
    sb = visible(c, "#singBlock")
    check("真鼠标能点开写歌面板", is_shown(nb), "命中 %s" % hit)
    check("切换后翻唱面板收起（不会两个叠着显示）", not is_shown(sb),
          json.dumps(sb))

    # ---- 4. 输入框可见且有面积（用户真的能填）----
    for sel, name in [("#nsStyle", "风格框"), ("#nsLyrics", "歌词框"),
                      ("#btnNewSong", "提交按钮")]:
        check(name + " 可见且有面积", is_shown(visible(c, sel)),
              json.dumps(visible(c, sel)))

    # ---- 5. 填了内容再切走切回，**不能丢** ----
    marker = "测试歌词ABC-不应丢失"
    set_val(c, "#nsLyrics", marker)
    c.click_el("#smCover")
    time.sleep(0.4)
    c.click_el("#smNew")
    time.sleep(0.5)
    kept = c.js("document.querySelector('#nsLyrics').value")
    check("切走再切回，已填的歌词还在（不丢用户输入）", kept == marker,
          "实际 %r" % (str(kept)[:40],))

    # ---- 6. 真点提交：必须真的建出一个 new_song 任务 ----
    before = latest_job()
    set_val(c, "#nsLyrics",
            "[Verse]\ngate check line one\n\n[Chorus]\ngate check line two")
    set_val(c, "#nsDur", "15")
    hit2 = c.click_el("#btnNewSong")
    time.sleep(2.5)
    after = latest_job()
    created = after and after != before
    check("真鼠标点提交后真的建了任务", bool(created),
          "命中 %s｜job %s → %s" % (hit2, before, after))
    if created:
        j = job_info(after)
        check("新任务的类型是 new_song", j.get("kind") == "new_song", j.get("kind"))
        check("新任务的标题是「新歌：…」",
              str(j.get("title") or "").startswith("新歌："), j.get("title"))
        # 立刻取消：这一关只验"点得动、发得出去"，不该真的跑一首 3 分钟的歌
        try:
            req = urllib.request.Request(
                "%s/api/job/%s/cancel" % (BASE, after), data=b"{}",
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=20).read()
            time.sleep(1.5)
            j2 = job_info(after)
            check("取消之后任务不是 running 了", j2.get("state") != "running",
                  j2.get("state"))
        except Exception as e:
            check("能取消测试任务", False, "%s: %s" % (type(e).__name__, str(e)[:80]))

    # ---- 7. ★ 负对照：把两个面板都强行显示出来，上面的判据必须变红 ----
    c.js("document.querySelector('#singBlock').style.display='';"
         "document.querySelector('#newSongBlock').style.display='';")
    time.sleep(0.3)
    both_shown = is_shown(visible(c, "#singBlock")) and is_shown(visible(c, "#newSongBlock"))
    check("负对照：两个面板同时显示时，「切换后翻唱面板收起」这条确实会红",
          both_shown is True, "脚本的自检前提成立")
    # 复原
    c.js("document.querySelector('#singBlock').style.display='';"
         "document.querySelector('#newSongBlock').style.display='none';")

    print("\n" + "-" * 70)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("红项：%s" % BAD)
        print("ALLDONE 第 33 关有 %d 条红" % len(BAD))
        return 1
    print("ALLDONE 第 33 关全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
