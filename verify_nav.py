# -*- coding: utf-8 -*-
r"""页内导航 + 网易云 VIP 歌可见性（第 24 关）。

这一关护的是一句很具体的抱怨：**「我不能看见网易云 VIP 的歌」**。

先把诊断结论摆在这里，因为它决定了判据长什么样 ——

  后端**一直是好的**：`netease_dirs()` 探得到 `D:\CloudMusic\VipSongsDownload`、
  排在候选目录第一位、3 首 .ncm 扫得出来、解密也通。
  真因在**界面上根本够不着**：整页实测 3990px / 视口 904px ≈ 4.4 屏，
  而「💿 我的音乐」在 **1809px** 处 —— 被 1328px 高的角色网格推到了第二屏，
  全页**一条导航都没有**。功能没坏，是入口看不见。

顺手在这一关里还挖出两个**潜伏很久的真缺陷**（都是加完导航才暴露的）：

  1. `html,body{height:100%}` 把 body 高度钉死成一屏（904px），
     内容 3990px 溢出到 body 之外 ⇒ `.top` 的 `position:sticky`
     **从来没生效过**（sticky 只在包含块范围内粘）。以前顶栏只有 logo
     和几个按钮，滚走了没人发现；有了导航条，滚两屏导航就消失。
  2. `.toast` 只有 `opacity:0`、**没有 `pointer-events:none`** ⇒
     那条看不见的提示条（z-index 60、横在页面顶部正中）一直压在导航条上，
     真鼠标点「我的音乐/在线曲库/声音市场」命中的是它，点了没反应。

还有两个**探测盲区**（这才是"歌看不见"的另一半）：
  3. `netease_dirs()` 原本只探 `VipSongsDownload` 子目录 ⇒
     `D:\CloudMusic` 根目录那 2 首真歌（普通下载的）一个入口都没有。
  4. 探测用 `recursive=False` ⇒ 客户端按歌手/专辑分目录存的歌整片漏掉。
  ⇒ 改成"根目录 + 常见子目录、一律递归扫、父子目录去重"。
     递归之后必须带 `skip_dirs=PROG_DIRS`：整棵 walk 会钻进程序目录捞出
     `listenTogetherDialing.wav` 这种**客户端提示音**（能播，但不是歌）。

判据四段：

  A 源码对账（零浏览器）：区块 id / navInit 调用 / NAV_ITEMS 与 id 同源 /
                        三条样式在 / body 是 min-height / toast 不吃事件
  B 后端探测（纯 Python）：VIP 目录排首位 / **探测数与扫出数逐条相等**
                        （盯死"chip 写 5 首点开 9 首"）/ 提示音被剪掉
                        / 父子目录去重
  C 浏览器（真鼠标）：    5 项都渲染且可点 / sticky 在任意滚动位置都粘住
                        / 真点每个导航项都跳到位且标题不被顶栏压住
                        / 从导航进「我的音乐」真的能扫出 VIP 歌
  D 负对照（实测能红）：  把 toast 改回吃事件 ⇒ 点击必须打空
                        / 把 body 改回固定高 ⇒ sticky 必须失效
                        / 摘掉一个区块 id ⇒ navInit 必须少渲染一项

用法（venv：浏览器段要 websocket）：
    <venv>/Scripts/python.exe -s verify_nav.py
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

# ★ 本机设了 http_proxy，urllib 连 127.0.0.1 也走代理 ⇒ 拿回来的是代理的 502，
#   看起来像"浏览器起不来"。直接把代理关掉。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "stages"))

BASE = "http://127.0.0.1:8756"
WEB = os.path.join(HERE, "web")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9351                      # 跟其它 UI 关错开，别抢同一个调试端口
PROFILE = os.path.join(HERE, ".cdp-nav")

# ★ 这份名单是**故意硬钉**的：加一个区块就必须同步改这里，让这条判据先红一次，
#   逼着改的人想清楚"新区块要不要进导航" —— 而不是悄悄多一个没人看得见的入口
#   （第 23 关整关的由头就是"功能做对了但用户看不见"）。
#   2026-10-14 加「做视频」（secVideos，排在「我的音乐」之后）：新增区块 ⇒ 这里跟着加。
NAV_IDS = ["secStage", "secRoles", "locPanel", "secVideos", "libPanel", "mktPanel"]

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def cdp_get(path, port=PORT):
    return json.load(urllib.request.urlopen(
        "http://127.0.0.1:%d%s" % (port, path), timeout=8))


# ---------------------------------------------------------------- A 源码对账

def test_source():
    print("\n=== A. 源码对账（零浏览器） ===")
    html = open(os.path.join(WEB, "index.html"), encoding="utf-8").read()
    css = open(os.path.join(WEB, "style.css"), encoding="utf-8").read()
    app = open(os.path.join(WEB, "app.js"), encoding="utf-8").read()

    # A1 每个导航目标的区块 id 都得在 index.html 里真存在（导航跳过去的前提）
    missing = [i for i in NAV_IDS if ('id="%s"' % i) not in html]
    check("%d 个导航目标区块在 index.html 里都真的有 id" % len(NAV_IDS), not missing,
          "缺：" + ",".join(missing) if missing else "全部齐备")

    # A2 navInit 真的被调用了（以前 restoreJobs 就是"只定义没调用"的死代码）
    check("index.html 里真的调用了 navInit()", "navInit()" in html,
          "只定义不调用 = 没有这个功能")

    # A3 NAV_ITEMS 里的 id 与 index.html 的 id 必须同源。
    #    探针**不许**自己抄一份区块名单 —— 抄了就会随产品改表悄悄错开。
    m = re.search(r"const NAV_ITEMS\s*=\s*\[(.*?)\];", app, re.S)
    in_app = re.findall(r'id:\s*"([^"]+)"', m.group(1)) if m else []
    check("app.js 的 NAV_ITEMS 与这块验收认定的区块名单逐条相同",
          in_app == NAV_IDS, "app.js=%s / 验收=%s" % (in_app, NAV_IDS))

    # A4 三条样式都在（缺 .nav-i.on 就是"能点但看不出在哪一屏"）
    for sel in (".nav{", ".nav-i{", ".nav-i.on"):
        check("style.css 里有 %s 这条规则" % sel, sel in css)

    # A5 body 高度：这条是 sticky 的命脉，写成固定高导航就消失了。
    #    ★ 尺子踩过一次坑：写成 `'"height:100%" not in decl'` —— 而
    #      `min-height:100%` **含** `height:100%` 这个子串 ⇒ 修好了也判红。
    #      判定"独立的 height 声明"必须锚到行首或分号，不能裸搜子串。
    m = re.search(r"html,body\{([^}]*)\}", css)
    decl = m.group(1) if m else ""
    has_fixed = re.search(r"(?:^|;)\s*height\s*:\s*100\s*%", decl)
    has_min = re.search(r"(?:^|;)\s*min-height\s*:", decl)
    check("html,body 用的是 min-height（不是把高度钉死成一屏）",
          bool(has_min) and not has_fixed, decl.strip())

    # A6 toast 不能吃鼠标事件（隐形提示条压导航条那一坑）
    m = re.search(r"\.toast\{([^}]*)\}", css)
    tdecl = m.group(1) if m else ""
    check(".toast 关掉了 pointer-events（opacity:0 的元素照样吃点击）",
          "pointer-events:none" in tdecl, tdecl.strip()[:90])

    # A7 探测函数必须用上剪枝名单（不然客户端提示音又回来了）
    ll = open(os.path.join(HERE, "stages", "local_lib.py"),
              encoding="utf-8").read()
    check("netease_dirs() 扫的时候带了 skip_dirs=PROG_DIRS",
          "skip_dirs=PROG_DIRS" in ll)
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    check("scan 接口也带同一份剪枝（否则 chip 写 5 首、点开 9 首）",
          "skip_dirs=local_lib.PROG_DIRS" in srv)

    # A8 曲库"提示音"这条路：判定函数 / 卡片标记 / 清理入口 / 样式，四样都得在。
    #    静态对账只是**下限**（防"代码根本没接上"），行为由 B6 与 D 段钉。
    lib = open(os.path.join(WEB, "lib.js"), encoding="utf-8").read()
    check("local_lib 里有 is_prog_sound 判定 + 提示音前缀名单",
          "def is_prog_sound" in ll and "PROG_SOUND_PREFIXES" in ll)
    check("lib.js 渲染时真看了 prog_sound 标记（不看就等于没标）",
          "s.prog_sound" in lib)
    check("lib.js 有 prog 卡片钩子 + 一键清理入口",
          "lib-card " in lib and "libProgClean" in lib)
    check("style.css 有 .prog-bar / .lib-card.prog 的样式",
          ".prog-bar" in css and ".lib-card.prog" in css)


# ---------------------------------------------------------------- B 后端探测

def test_probe():
    print("\n=== B. 后端探测（纯 Python） ===")
    import local_lib

    dirs = local_lib.netease_dirs()
    check("探测得到网易云目录", bool(dirs),
          [d["path"] for d in dirs])
    if not dirs:
        return

    # B1 VIP 目录排最前（用户就是来找它的）
    check("含 .ncm 的目录排在第一位", (dirs[0].get("ncm") or 0) > 0,
          "%s ncm=%s" % (dirs[0]["path"], dirs[0].get("ncm")))

    # B2 ★ 探测数 == 扫出数。这条直接盯死"chip 写 5 首、点开 9 首"。
    bad = []
    for d in dirs:
        r = local_lib.scan_dir(d["path"], recursive=True, limit=3000,
                               skip_dirs=local_lib.PROG_DIRS)
        if len(r["items"]) != d.get("song_count"):
            bad.append("%s: chip说%s 实际%d"
                       % (d["path"], d.get("song_count"), len(r["items"])))
    check("★ 候选目录上写的首数 = 点进去真扫出来的首数（同一个数只许一个来源）",
          not bad, "; ".join(bad) if bad else "逐条对得上")

    # B3 客户端提示音必须被剪掉（它们能播，但不是歌）
    noise = []
    for d in dirs:
        r = local_lib.scan_dir(d["path"], recursive=True, limit=3000,
                               skip_dirs=local_lib.PROG_DIRS)
        for it in r["items"]:
            if re.search(r"listenTogether|showListenTogether", it["name"], re.I):
                noise.append(it["name"])
    check("★ 客户端自带提示音被剪掉了（能播但不是歌，列出来就是骗人）",
          not noise, "漏出来的：" + ",".join(noise) if noise else "一条都没有")

    # B4 父子目录去重：同一个地方不许出现两个入口
    pairs = []
    for a in dirs:
        for b in dirs:
            if a is not b and local_lib._is_under(a["path"], b["path"]):
                pairs.append("%s ⊂ %s" % (a["path"], b["path"]))
    check("探到的目录两两之间没有父子关系（否则一个地方两个入口）",
          not pairs, "; ".join(pairs) if pairs else "无重叠")

    # B5 VIP 目录里的 .ncm 真的带得出来歌名（不是空壳）
    top = dirs[0]
    r = local_lib.scan_dir(top["path"], recursive=True, limit=3000,
                           skip_dirs=local_lib.PROG_DIRS)
    ncm = [i for i in r["items"] if i.get("enc") == "ncm"]
    check("VIP 目录里的 .ncm 读得出真歌名（不是只剩文件名）",
          bool(ncm) and all((i.get("name") or "").strip() for i in ncm),
          [i["name"] for i in ncm][:4])

    # B6 is_prog_sound 是纯函数，判据能自足（不靠真库里有提示音）
    #    负对照故意挑"很像但不是"的：同名文件放在别的目录下、真歌放在
    #    CloudMusic 下 —— 前者不判、后者不判，才说明判定不是按文件名一刀切。
    yes = [
        r"D:\CloudMusic\resource\listenTogetherDialing.wav",
        r"D:\CloudMusic\resource\listenTogetherMemberEnter.wav",
        r"D:\CloudMusic\resource\listenTogetherLeave.wav",
        r"D:\CloudMusic\resource\showListenTogetherEmoticon.wav",
    ]
    no = [
        r"D:\CloudMusic\嘟比Dubi - 神耀（星际争霸同人歌）.mp3",
        r"D:\CloudMusic\VipSongsDownload\someone - song.ncm",
        r"D:\AHC\VoiceStudio\data\music\林俊杰 - 曹操.mp3",
        # 负对照：同名文件但**不在**客户端 resource 下 ⇒ 不许判成提示音
        r"D:\MyMusic\resource\listenTogetherDialing.wav",
        r"D:\CloudMusic\music\listenTogetherDialing.wav",
    ]
    wrong = [p for p in yes if not local_lib.is_prog_sound(p)]
    wrong += [p for p in no if local_lib.is_prog_sound(p)]
    check("★ 提示音判定：4 条客户端提示音全认出，5 条负对照（含两条同名异目录）全不误判",
          not wrong, "判错：" + "; ".join(wrong) if wrong else "9/9 全对")


# ---------------------------------------------------------------- C 浏览器

class Cdp:
    def __init__(self, port=PORT):
        import websocket  # noqa
        tabs = [t for t in cdp_get("/json/list", port)
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not tabs:
            raise RuntimeError("没有可用的页面目标")
        self.ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"],
                                              timeout=60)
        self.i = 0
        self.send("Page.enable")

    def send(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def click_at(self, x, y):
        """真鼠标：press + release，返回按下前那一刻的命中元素。"""
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.tagName+'|'+(e.id||'')+'|'+(e.className||''))"
                      ":'null'})()" % (x, y))
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                  button="left", clickCount=1)
        time.sleep(0.05)
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                  button="left", clickCount=1)
        return top

    def click_nav(self, nav_id, tag):
        """给导航项打临时 id 再真点（:nth-of-type 在 #navBar 里靠不住）。"""
        got = self.js(
            "(()=>{const a=document.querySelector('#navBar .nav-i[data-id=\"%s\"]');"
            "if(!a) return false; a.id='__navhit'; return true;})()" % nav_id)
        if not got:
            return None
        xy = self.js("(()=>{const e=document.getElementById('__navhit');"
                     "const r=e.getBoundingClientRect();"
                     "return {x:r.left+r.width/2,y:r.top+r.height/2,"
                     "w:r.width,h:r.height};})()")
        if not xy or xy["w"] <= 0:
            return {"hit": "NO_BOX"}
        hit = self.click_at(xy["x"], xy["y"])
        time.sleep(1.6)
        self.js("(()=>{const e=document.getElementById('__navhit');"
                "if(e) e.removeAttribute('id'); return true;})()")
        return {"hit": hit, "xy": xy}

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def test_ui():
    print("\n=== C. 浏览器（真鼠标） ===")
    if not os.path.isfile(CHROME):
        check("本机有 Chrome 可以跑浏览器段", False, CHROME)
        return
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = None
    try:
        for _ in range(60):
            try:
                cdp_get("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Chrome 没起来")
        c = Cdp()
        c.send("Runtime.enable")
        c.send("Page.navigate", url="http://127.0.0.1:8756/")
        time.sleep(6.0)

        # C1 导航条渲染齐全
        items = c.js("""(()=>Array.from(
            document.querySelectorAll('#navBar .nav-i')).map(a=>({
              id:a.dataset.id, w:Math.round(a.getBoundingClientRect().width),
              h:Math.round(a.getBoundingClientRect().height)})))()""")
        check("导航条渲染出 %d 项且都有尺寸" % len(NAV_IDS),
              bool(items) and len(items) == len(NAV_IDS)
              and all(i["w"] > 0 and i["h"] > 0 for i in items),
              json.dumps(items, ensure_ascii=False))
        check("导航项的顺序与 NAV_ITEMS 一致",
              [i["id"] for i in (items or [])] == NAV_IDS,
              [i["id"] for i in (items or [])])

        # C2 ★ sticky：滚到任何位置顶栏都得粘在顶上
        tops = []
        for y in (0, 900, 1800, 2700):
            c.js("window.scrollTo({top:%d,behavior:'instant'})" % y)
            time.sleep(0.5)
            tops.append(c.js("Math.round(document.querySelector('.top')"
                             ".getBoundingClientRect().top)"))
        check("★ 顶栏在任意滚动位置都粘在顶部（sticky 真的生效）",
              all(abs(t) <= 2 for t in tops), "各位置 top=" + str(tops))

        # C3 真点每个导航项：跳到位 + 标题没被顶栏压住 + 高亮跟着走
        for nav_id in NAV_IDS:
            c.js("window.scrollTo({top:0,behavior:'instant'})")
            time.sleep(0.4)
            r = c.click_nav(nav_id, nav_id)
            if not r:
                check("真鼠标点导航「%s」" % nav_id, False, "找不到这一项")
                continue
            st = c.js("""(()=>{
              const el=document.getElementById('%s');
              const r=el.getBoundingClientRect();
              const barH=document.querySelector('.top').getBoundingClientRect().height;
              const on=document.querySelector('#navBar .nav-i.on');
              return {top:Math.round(r.top), barH:Math.round(barH),
                      on:on?on.dataset.id:'无'};})()""" % nav_id)
            check("真点「%s」跳到位且标题没被顶栏压住" % nav_id,
                  st["top"] >= st["barH"] - 2 and st["on"] == nav_id,
                  "命中=%s 区块top=%s 顶栏高=%s 高亮=%s"
                  % (r.get("hit"), st["top"], st["barH"], st["on"]))

        # C4 ★ 隐形 toast 不许再吃点击：命中必须落在导航项自己身上
        r = c.click_nav("locPanel", "locPanel")
        check("★ 点导航命中的是导航项自己（隐形 toast 不再挡路）",
              bool(r) and "nav-i" in (r.get("hit") or ""), r.get("hit"))

        # C5 把两条诉求串起来：从导航进「我的音乐」⇒ 真点 VIP chip ⇒ 扫出 ncm
        c.js("""(()=>{const a=Array.from(
            document.querySelectorAll('#locDirs .loc-dir'))
            .find(x=>x.textContent.indexOf('网易云')>=0);
            if(a) a.id='__vipchip'; return !!a;})()""")
        chip = c.js("""(()=>{const e=document.getElementById('__vipchip');
            if(!e) return null; e.scrollIntoView({block:'center'});
            const r=e.getBoundingClientRect();
            return {x:r.left+r.width/2,y:r.top+r.height/2,
                    inView:r.top>=0&&r.bottom<=innerHeight};})()""")
        if chip and chip.get("inView"):
            c.click_at(chip["x"], chip["y"])
        else:
            check("真鼠标点 VIP 目录 chip", False, "chip 不在视口内")
        rows = 0
        for _ in range(30):
            rows = c.js("document.querySelectorAll('#locOut .loc-row').length") or 0
            if rows:
                break
            time.sleep(0.6)
        check("从导航进「我的音乐」⇒ 点 VIP 目录 ⇒ 真的扫出歌", rows > 0,
              "行数=%s" % rows)
        check("扫出来的行里标了「网易云加密」（VIP 歌看得见了）",
              bool(c.js("""(()=>Array.from(
                  document.querySelectorAll('#locOut .loc-row'))
                  .some(r=>r.textContent.indexOf('网易云加密')>=0))()""")),
              c.js("""(()=>Array.from(document.querySelectorAll('#locOut .loc-row'))
                  .slice(0,3).map(r=>r.querySelector('.nm').textContent))()"""))

        # ---------------- D 负对照（必须实测能红） ----------------
        print("\n--- D. 负对照（改坏了必须红） ---")

        # D1 把 toast 改回吃事件 ⇒ 点导航必须打空
        # ★ 踩过的坑：只改 `pointerEvents='auto'` 是不够的。
        #   平时 toast 是**空的**（实测宽 38px、高 22px，压在页面顶部正中），
        #   它和导航项中心（实测 x≈643, y≈32）几何上**根本不重叠** ⇒ 注入前后
        #   命中完全一样（都是 A|-|nav-i），这条负对照就退化成"恒红"，
        #   既说明不了产品坏了，也说明不了产品没坏（铁律 3：判据必须实测能红）。
        #   ⇒ 破坏时要连带把 toast 撑到真的压住导航条，注入才算生效。
        c.js("window.__toastSaved = document.getElementById('toast').getAttribute('style') || '';"
             "window.__toastCls = document.getElementById('toast').className;")
        try:
            c.js("""(()=>{const t=document.getElementById('toast');
                t.style.pointerEvents='auto';
                t.style.left='0'; t.style.top='0'; t.style.transform='none';
                t.style.width='100vw'; t.style.maxWidth='100vw';
                t.style.height='160px'; t.classList.add('on');
                return true;})()""")
            c.js("window.scrollTo({top:0,behavior:'instant'})")
            time.sleep(0.4)
            r = c.click_nav("libPanel", "libPanel(负对照)")
            hit = r.get("hit") if r else "NO_ITEM"
            check("★ 负对照：toast 改回吃事件后，点导航必须打不到导航项",
                  "nav-i" not in str(hit), "命中=%s" % hit)
        finally:
            c.js("document.getElementById('toast').setAttribute('style',"
                 " window.__toastSaved);"
                 "document.getElementById('toast').className = window.__toastCls;")
        check("负对照已还原（toast 的 pointer-events 回到原状）",
              c.js("(()=>getComputedStyle(document.getElementById('toast'))"
                   ".pointerEvents==='none')()") is True)

        # D2 把 body 改回固定高 ⇒ sticky 必须失效
        c.js("window.__bodySaved = document.body.style.height")
        try:
            c.js("document.body.style.height='904px'")
            c.js("window.scrollTo({top:1800,behavior:'instant'})")
            time.sleep(0.6)
            t = c.js("Math.round(document.querySelector('.top')"
                     ".getBoundingClientRect().top)")
            check("★ 负对照：body 改回固定高后，sticky 必须失效（顶栏滚走）",
                  t < -50, "滚到 1800 时顶栏 top=%s" % t)
        finally:
            c.js("document.body.style.height=window.__bodySaved")
            c.js("window.scrollTo({top:0,behavior:'instant'})")
        time.sleep(0.5)
        c.js("window.scrollTo({top:1800,behavior:'instant'})")
        time.sleep(0.6)
        t2 = c.js("Math.round(document.querySelector('.top')"
                  ".getBoundingClientRect().top)")
        check("负对照已还原（sticky 重新生效）", abs(t2) <= 2, "顶栏 top=%s" % t2)

        # D3 尺子自检：摘掉一个区块 id ⇒ navInit 必须少渲染一项
        #    还原走**元素引用**，不用 querySelectorAll 按索引猜 ——
        #    索引会随页面区块增减漂移，还原到错误的元素上等于没还原。
        c.js("window.__secEl = document.getElementById('libPanel');"
             "window.__secSaved = window.__secEl.id;")
        try:
            c.js("window.__secEl.removeAttribute('id'); navInit();")
            n = c.js("document.querySelectorAll('#navBar .nav-i').length")
            check("★ 尺子自检：摘掉一个区块的 id 后，navInit 必须少渲染一项",
                  n == len(NAV_IDS) - 1, "渲染了 %s 项（期望 %s）"
                  % (n, len(NAV_IDS) - 1))
        finally:
            c.js("window.__secEl.id = window.__secSaved; navInit();")
        n2 = c.js("document.querySelectorAll('#navBar .nav-i').length")
        check("尺子自检已还原（导航项回到 %d 项）" % len(NAV_IDS),
              n2 == len(NAV_IDS), "现在 %s 项" % n2)
    finally:
        if c:
            c.close()
        proc.terminate()


# ------------------------------------------------- D 曲库不许把提示音当歌

# mock 脚本：页面脚本跑起来**之前**就把 /api/music/library 换掉。
# 开关挂在 window.__mkProg 上，好让 D6 的尺子自检原地关掉提示音再渲染一遍
# （不用重新导航 —— 重新导航时 addScriptToEvaluateOnNewDocument 会再跑一遍，
#  开关会被重置回去，等于检了个寂寞）。
MOCK_LIB_JS = r"""
(() => {
  window.__mkProg = true;
  const real = window.fetch.bind(window);
  window.fetch = function (u, o) {
    const url = (typeof u === 'string') ? u : ((u && u.url) || '');
    if (url.indexOf('/api/music/library') >= 0) {
      const songs = [
        {title: '嘟比Dubi - 神耀（星际争霸同人歌）',
         path: 'D:\\CloudMusic\\嘟比Dubi - 神耀.mp3',
         size: 3451900, dur: 224.9, src: 'local', item: 'mk_real',
         prog_sound: false},
      ];
      if (window.__mkProg) {
        songs.push({title: 'listenTogetherDialing',
          path: 'D:\\CloudMusic\\resource\\listenTogetherDialing.wav',
          size: 40960, dur: 0.6, src: 'local', item: 'mk_prog',
          prog_sound: true});
      }
      return Promise.resolve(new Response(JSON.stringify({songs: songs}),
        {status: 200, headers: {'Content-Type': 'application/json'}}));
    }
    return real(u, o);
  };
})();
"""


def test_lib_honesty():
    """曲库面板的诚实性：客户端提示音必须被点破，且不许给「导进翻唱台」。

    **这段用 mock 数据**（页面脚本跑起来之前先把 /api/music/library 的响应换掉）。
    为什么不直接用真库：判据要自足。真库里有没有提示音，取决于用户清没清过 ——
    哪天用户把那 4 条清掉了，靠真数据的判据就 0 命中、静默变绿（§3 的假绿）。
    mock 里固定 1 条提示音 + 1 条真歌，判据才永远咬得住。
    """
    print("\n=== D. 曲库不骗人（mock 数据 + 真鼠标） ===")
    if not os.path.isfile(CHROME):
        check("本机有 Chrome 可以跑浏览器段", False, CHROME)
        return
    profile = os.path.join(HERE, ".cdp-nav-lib")
    os.makedirs(profile, exist_ok=True)
    port = PORT + 1
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % port, "--remote-allow-origins=*",
        "--user-data-dir=" + profile, "--window-size=1400,1000",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = None
    try:
        for _ in range(60):
            try:
                cdp_get("/json/version", port)
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Chrome 没起来")
        c = Cdp(port)
        c.send("Runtime.enable")
        # 必须在 navigate **之前**注入 —— 页面一跑，fetch 就已经被换掉了
        c.send("Page.addScriptToEvaluateOnNewDocument", source=MOCK_LIB_JS)
        c.send("Page.navigate", url="http://127.0.0.1:8756/")
        time.sleep(6.5)

        # D1 前置：mock 真进了页面。没有这条，后面"没数据"也可能一路绿。
        n = c.js("document.querySelectorAll('#libMine .lib-card').length")
        check("★ 前置：mock 的曲库数据真的进了页面（应渲染 2 张卡）", n == 2,
              "渲染了 %s 张" % n)
        if n != 2:
            return

        # D2 警示条：必须出现，且点明是"提示音"、不是歌
        bar = c.js("(()=>{const e=document.querySelector('#libMine .prog-bar');"
                   "return e?e.textContent.replace(/\\s+/g,' ').trim():null})()")
        check("★ 曲库顶部有警示条，且写明「提示音（不是歌）」",
              bool(bar) and "提示音" in bar and "不是歌" in bar,
              (bar or "(无警示条)")[:110])

        # D3 提示音卡片：打了 prog 标、说明原因、**收掉**「导进翻唱台」
        pc = c.js("""(()=>{const e=document.querySelector('#libMine .lib-card.prog');
          if(!e) return null;
          return {txt:e.textContent.replace(/\\s+/g,' ').trim(),
                  use:!!e.querySelector('button[data-act="use"]'),
                  forget:!!e.querySelector('button[data-act="forget"]'),
                  preview:!!e.querySelector('button[data-act="preview"]')};})()""")
        check("★ 提示音卡片被标出来（prog 类 + 文案点明它不是歌曲）",
              bool(pc) and "不是歌曲" in pc["txt"],
              (pc or {}).get("txt", "(没有 .prog 卡)")[:120])
        check("★ 提示音卡片**不给**「导进翻唱台」（拿音效去翻唱是白费功夫）",
              bool(pc) and pc["use"] is False,
              "有导进翻唱台按钮=%s" % (pc or {}).get("use"))
        check("提示音卡片仍保留「试听」（一点就知道它就是那声提示音）",
              bool(pc) and pc["preview"] is True,
              "有试听按钮=%s" % (pc or {}).get("preview"))
        check("提示音卡片留着「从曲库移除」这条路",
              bool(pc) and pc["forget"] is True,
              "有移除按钮=%s" % (pc or {}).get("forget"))

        # D4 真歌必须**照旧**能用（修提示音不许误伤真歌）
        real_use = c.js("""(()=>{const cs=[...document.querySelectorAll('#libMine .lib-card')];
          const r=cs.find(e=>!e.classList.contains('prog'));
          return r?!!r.querySelector('button[data-act="use"]'):null})()""")
        check("★ 真歌卡片照旧有「导进翻唱台」（修提示音不许把真歌一起阉了）",
              real_use is True, "真歌卡有导进翻唱台=%s" % real_use)

        # D5 一键清理入口：存在，且写明要清几条
        cl = c.js("(()=>{const b=document.getElementById('libProgClean');"
                  "return b?b.textContent.replace(/\\s+/g,' ').trim():null})()")
        check("★ 有一键清理入口，且写明要清几条", bool(cl) and "1" in cl,
              cl or "(没有清理按钮)")

        # D6 ★ 尺子自检：把 mock 里的提示音拿掉 ⇒ 警示条与 .prog 卡必须一起消失。
        #     没有这条，"警示条写死永远显示" 也会被判绿。
        c.js("window.__mkProg = false; loadLibrary();")
        time.sleep(1.5)
        gone = c.js("!document.querySelector('#libMine .prog-bar')"
                    " && !document.querySelector('#libMine .lib-card.prog')")
        check("★ 尺子自检：拿掉提示音后，警示条与 .prog 卡必须一起消失",
              gone is True, "还在=%s" % (not gone))
        c.js("window.__mkProg = true; loadLibrary();")
        time.sleep(1.5)
        back = c.js("!!document.querySelector('#libMine .prog-bar')")
        check("尺子自检已还原（提示音数据放回后警示条回来）", back is True,
              "警示条回来了=%s" % back)
    finally:
        if c:
            c.close()
        proc.terminate()


def main():
    print("=" * 62)
    print("页内导航 + 网易云 VIP 歌可见性 验收")
    print("=" * 62)
    try:
        test_source()
        test_probe()
        test_ui()
        test_lib_honesty()
    except Exception as e:
        # 探针自己崩了也要落一条可见的 ❌，别让"红退出码 + 零失败"骗人
        check("探针自身异常中断（后续断言未执行）", False, str(e)[:200])
        import traceback
        traceback.print_exc()

    print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - %s" % b)
    print("结论：%s" % ("导航与 VIP 歌可见性全绿" if not BAD else "有红"))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
