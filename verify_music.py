# -*- coding: utf-8 -*-
r"""在线曲库的真浏览器验收。

规矩（踩过 4 次的坑，别再犯）：
· 验收一律用 Input.dispatchMouseEvent + 命中测试，**禁 element.click()**。
  element.click() 绕过命中测试，会让"根本点不到的东西"也报成功。
· 断言要能红。脚本末尾跑一遍负对照，实测红过才算数。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:8756/"
PORT = 9333
PROFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cdp-music")
# 本地导入验收用的目录：里面**故意混了 5 个坏文件**（纯文本/伪WAV/随机/
# 截断MP3/加密ncm），用来验证"坏文件绝不能被当成可用音频列出来"。
LIB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".testlib")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name + (("  | " + detail) if detail else ""),
          flush=True)
    return cond


def cdp_get(path, port=PORT):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=20) as r:
        return json.loads(r.read().decode())


class Cdp:
    def __init__(self, port=PORT):
        import websocket  # noqa
        self.port = port
        ws = cdp_get("/json/list", port)
        page = [t for t in ws if t.get("type") == "page"]
        if not page:
            raise RuntimeError("没有可用的页面目标")
        import websocket
        self.ws = websocket.create_connection(page[0]["webSocketDebuggerUrl"],
                                              timeout=60)
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr, awaitp=False):
        r = self.send("Runtime.evaluate", expression=expr,
                      returnByValue=True, awaitPromise=awaitp)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def mouse(self, x, y, typ="mousePressed"):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y, button="left",
                  clickCount=1)

    def real_click(self, x, y):
        """真鼠标：必须先命中测试，确认该点最顶层的元素就是目标。

        视口外的点直接拒掉并说清原因 —— 真鼠标按的是**视口坐标**，
        对屏幕外的东西点下去必然打空（返回 'null'）。以前这里照点不误，
        于是断言红起来时看起来像"功能坏了"，实际是测试没先确认元素可见。
        这个坑本轮踩了两次（本地歌的「用它翻唱」和搜索结果的「导进翻唱台」）。
        """
        off = self.js("""(()=>{const vh=innerHeight, vw=innerWidth;
            if(%f<0||%f>vh||%f<0||%f>vw) return `视口(${Math.round(vw)}x${Math.round(vh)})外`;
            return '';})()""" % (y, y, x, x))
        if off:
            return "OUT_OF_VIEWPORT:" + off
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);return e?(e.id||e.className||e.tagName):'null'})()"
                      % (x, y))
        self.mouse(x, y, "mousePressed")
        time.sleep(0.06)
        self.mouse(x, y, "mouseReleased")
        return top

    def click_el(self, selector, note=""):
        """按选择器点一个元素：先滚进视口、再真鼠标点，返回命中的元素名。

        这样每个调用点都不用各自处理"元素可能在屏幕外"这件事。
        """
        ok = self.js("""(()=>{const e=document.querySelector(%s);
            if(!e) return false; e.scrollIntoView({block:'center'}); return true;})()"""
                     % json.dumps(selector))
        time.sleep(0.35)
        if not ok:
            return "NOT_FOUND:" + selector
        xy = self.js("""(()=>{const e=document.querySelector(%s);
            if(!e) return null; const r=e.getBoundingClientRect();
            return {x:r.left+r.width/2, y:r.top+r.height/2};})()"""
                     % json.dumps(selector))
        if not xy:
            return "NO_BOX:" + selector
        return self.real_click(xy["x"], xy["y"])


def main():
    import websocket  # 确认依赖

    # 样本目录必须先造好：坏文件是用来验"拒识"的，
    # 目录不存在会让那些断言**假红**，比不验还糟。
    if os.path.isdir(LIB_DIR):
        need = ["notes.mp3", "broken.wav", "rand.flac", "trunc.mp3", "enc.ncm"]
        for fn in need:
            p = os.path.join(LIB_DIR, fn)
            if not os.path.isfile(p):
                raise SystemExit("验收样本缺失：%s（请先造好 %s 下的 5 个坏文件）" % (p, LIB_DIR))

    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--remote-debugging-port=%d" % PORT,
        # 没有这条，Chrome 会以 403 拒掉 WebSocket 握手（origin 校验）
        "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000", "--no-first-run",
        "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    page = None
    for _ in range(40):
        try:
            cdp_get("/json/version")
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Chrome 调试端口没起来")

    try:
        page = Cdp()
        page.send("Page.enable")
        page.send("Runtime.enable")
        # ★ 在页面脚本之前挂一个全局错误收集器（本类 Cdp 的 send() 会把事件丢掉，
        #   所以不能靠 Runtime.exceptionThrown 订阅，必须在页面里自己记）。
        #   为什么非要这一条：本轮那个 `meta is not defined` 让 tickJobs 每轮都抛，
        #   后果是"任务永远显示不出完成"——它不报在任何按钮上，
        #   上面每一条判据都可能照样绿。只有"全程零异常"才抓得住这一类。
        #   负对照已实测：把 tickJobs 里 meta 的声明去掉，这里立刻红。
        page.send("Page.addScriptToEvaluateOnNewDocument", source=(
            "window.__errs=[];"
            "window.addEventListener('error',function(e){"
            "window.__errs.push('ERR: '+(e.message||String(e)));});"
            "window.addEventListener('unhandledrejection',function(e){"
            "window.__errs.push('REJ: '+String(e.reason));});"
            # 顺带把 toast 记录下来：导歌/训练这类"完成回调"驱动的动作，
            # 最直接的证据就是它到底说了什么（"没弹过那句"比"没报错"强得多）。
            "window.__toasts=[];"
            "window.addEventListener('DOMContentLoaded',function(){"
            "  var t=window.toast;"
            "  if(typeof t==='function'){window.toast=function(m,k){"
            "    window.__toasts.push(String(k||'')+':'+String(m));"
            "    return t.apply(this,arguments);};}});"
        ))
        page.send("Page.navigate", url=URL)
        time.sleep(3.5)

        print("\n=== 1. 页面与曲库面板加载 ===")
        check("曲库面板存在", page.js("!!document.querySelector('#libPanel')"))
        check("搜索框存在", page.js("!!document.querySelector('#libQ')"))
        check("结果网格存在", page.js("!!document.querySelector('#libGrid')"))
        check("lib.js 已加载(libSearch 是函数)",
              page.js("typeof window.libSearch === 'function'"))
        check("已导入区初始有引导文案",
              page.js("!!document.querySelector('#libMine') && "
                      "document.querySelector('#libMine').textContent.length > 0"))

        print("\n=== 1b. 本地音乐导入：面板、扫描、真进翻唱台 ===")
        check("本地导入面板存在", page.js("!!document.querySelector('#locPanel')"))
        check("目录输入框存在", page.js("!!document.querySelector('#locPath')"))
        check("扫描按钮存在", page.js("!!document.querySelector('#btnLocScan')"))
        check("loc.js 已加载(locScan 是函数)",
              page.js("typeof window.locScan === 'function'"))
        dirsN = page.js("document.querySelectorAll('#locDirs .loc-dir').length")
        check("列出了本机常见音乐目录", (dirsN or 0) >= 3, "目录数=%s" % dirsN)

        # 本轮的判据要能**重复跑**，所以先清掉上一轮留下的测试样本。
        # 原来直接扫，于是 notes.mp3 在上一轮已入曲库 ⇒ 这次扫描被当成
        # 「已在库里」而不列出 ⇒ #locOut 里没有可点的行，②号槽填不上，
        # 「本地歌真的进了翻唱台」两条恒红。这不是功能坏了，是判据依赖了
        # 上一次运行的历史状态（同一个坑，本轮撞了第二次）。
        try:
            _ls = json.load(urllib.request.urlopen(
                "http://127.0.0.1:8756/api/music/library", timeout=20))["songs"]
            for s in _ls:
                if LIB_DIR in (s.get("path") or ""):
                    _rq = urllib.request.Request(
                        "http://127.0.0.1:8756/api/music/local/forget",
                        data=json.dumps({"item": s["item"]}).encode("utf-8"),
                        headers={"Content-Type": "application/json"}, method="POST")
                    urllib.request.urlopen(_rq, timeout=20).read()
        except Exception:
            pass

        # 真键盘填路径 + 真鼠标点扫描
        page.js("""(()=>{const p=document.querySelector('#locPath');
            p.value=%s; p.dispatchEvent(new Event('input'));})()"""
                % json.dumps(LIB_DIR, ensure_ascii=False))
        # ★ 必须走 click_el（先 scrollIntoView 再点）。原来这里直接取
        #   getBoundingClientRect 再 real_click，而「我的音乐」面板在角色墙下面：
        #   角色数从 3 涨到 6 之后，扫描按钮落到 y≈960，而视口只有 904 ——
        #   真鼠标按视口坐标点必然打空（real_click 会直接回 OUT_OF_VIEWPORT）。
        #   后果不只是这一条红：locScan() 没被执行 ⇒ 一行都没扫出来 ⇒
        #   下面"列出本地音频""用它翻唱""进②号槽"连着 5 条全红。
        #   同一个坑在这个文件里已经是第二次（另一次是「用它翻唱」按钮），
        #   凡是"用户要点的东西"统一走 click_el。
        top = page.click_el("#btnLocScan")
        check("扫描按钮可被真鼠标命中",
              (not str(top).startswith(("NOT_FOUND", "NO_BOX", "OUT_OF_VIEWPORT")))
              and "btnLocScan" in str(top), "top=%s" % top)
        rows = 0
        for _ in range(40):
            time.sleep(1)
            rows = page.js("document.querySelectorAll('#locOut .loc-row').length") or 0
            if rows:
                break
        check("扫描真的列出了本地音频", (rows or 0) > 0, "行数=%s" % rows)
        # 假头文件/纯文本必须不能被列成可用（这是本轮修的真实缺陷）
        badNames = ["notes.mp3", "broken.wav", "rand.flac", "trunc.mp3", "enc.ncm"]
        listed = page.js("""(()=>{const s=new Set();
            document.querySelectorAll('#locOut .loc-row .nm').forEach(e=>s.add(e.textContent.trim()));
            return Array.from(s);})()""") or []
        leaked = [b for b in badNames if any(b in x for x in listed)]
        check("坏文件没被当成可用音频列出", not leaked, "混进来=%s" % leaked)
        check("跳过的文件写明了原因",
              page.js("!!document.querySelector('#locOut .loc-skip')"))

        # 真鼠标点第一行的「用它翻唱」，验②号槽真的被填上
        use_xy = page.js("""(()=>{const b=document.querySelector('#locOut .loc-row .btn');
            if(!b) return null; const r=b.getBoundingClientRect();
            return {x:r.left+r.width/2,y:r.top+r.height/2};})()""")
        # 诊断信息直接印出来：这两条断言已经反复红了，
        # 光看"行数=0"猜不出到底是没有行、行里没按钮，还是按钮被禁用。
        dbg_rows = page.js("""(()=>{const rs=document.querySelectorAll('#locOut .loc-row');
            return Array.from(rs).map(r=>({nm:r.querySelector('.nm')?.textContent||'',
                btns:Array.from(r.querySelectorAll('.btn')).map(b=>
                    (b.textContent||'').trim()+(b.disabled?'(禁用)':''))}))})()""")
        dbg_skip = page.js("""(()=>{const s=document.querySelector('#locOut .loc-skip');
            return s?(s.textContent||'').trim().slice(0,160):''})()""")
        print("      [诊断] .loc-row=%d 内容=%s跳过区=%s"
              % (len(dbg_rows or []), dbg_rows, dbg_skip[:80]))
        # 顺便量按钮的真实几何位置 —— 「按钮在 DOM 里但真鼠标点不到」这种问题，
        # 只有把坐标/视口/被谁盖住一起打出来才看得见（top=null 就是这么定位的）。
        dbg_geo = page.js("""(()=>{const b=document.querySelector('#locOut .loc-row .btn');
            if(!b) return null; const r=b.getBoundingClientRect();
            const cx=r.left+r.width/2, cy=r.top+r.height/2;
            const top=document.elementFromPoint(cx,cy);
            return {rect:[Math.round(r.left),Math.round(r.top),Math.round(r.width),Math.round(r.height)],
                    点:[Math.round(cx),Math.round(cy)],
                    视口:[innerWidth,innerHeight],
                    命中:(top?(top.className||top.tagName):'null'),
                    滚出视口:(r.bottom<0||r.top>innerHeight)};})()""")
        print("      [几何] %s" % dbg_geo)
        # 关键修正：这个按钮默认排在 y≈1042，而视口只有 904 高 —— **它在屏幕外**。
        # 真鼠标按视口坐标点，必然打空（top=null），于是"点了没反应"被误判成
        # "功能坏了"。真鼠标只能点**用户真正看得到**的东西，所以必须先滚进视口。
        # 教训：断言"能点到"之前，先量元素在不在视口里（记忆铁律 #2）。
        # 断言必须要求命中的就是那个按钮本身；"点到了别的东西"不算通过。
        # （原来的 bool(top) 太松——真鼠标点空时它也能 PASS）
        top = page.click_el("#locOut .loc-row .btn")
        check("『用它翻唱』可被真鼠标命中", "btn" in str(top), "top=%s" % top)
        time.sleep(1.2)
        check("本地歌真的进了翻唱台②号槽",
              page.js("!!document.querySelector('#slotSong.has')"))
        check("槽位显示了本地歌名",
              page.js("""(()=>{const e=document.querySelector('#slotSong .who');
                 return e && e.textContent.trim().length>0})()"""))

        # 1b-2 真正**导入曲库**（1b 只点了"用它翻唱"，那不进曲库）。
        # 这里直接打后端接口，避开界面上的 confirm 弹窗 —— 弹窗是浏览器原生
        # 对话框，headless 下要点两次才过，测试里不值得为此写状态机。
        try:
            # 用 notes.mp3（真音频样本）。原来这里写的是 fake_mp3.mp3，
            # 而样本文件实际叫 notes.mp3 ⇒ 请求打在一个不存在的路径上，
            # 于是"导入/时长/签名"三条一起红 —— 是断言指错文件名，不是功能坏了。
            req = urllib.request.Request(
                "http://127.0.0.1:8756/api/music/local/add",
                data=json.dumps({"paths": [os.path.join(LIB_DIR, "notes.mp3")]}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            r = json.loads(urllib.request.urlopen(req, timeout=40).read().decode())
            check("正对照：真音频能被导入曲库",
                  len(r.get("added", [])) == 1,
                  "added=%d bad=%s" % (len(r.get("added", [])), r.get("bad")))
            added = (r.get("added") or [{}])[0]
            check("导入的歌量到了真实时长(>5秒)",
                  (added.get("dur") or 0) > 5, "%.1f 秒" % (added.get("dur") or 0))
            check("导入的歌记下了原文件签名",
                  bool(added.get("sig")), "sig=%s" % (added.get("sig"),))
        except Exception as e:
            check("正对照：真音频能被导入曲库", False, str(e)[:120])

        print("\n=== 2. 真键盘输入 + 搜到真结果 ===")
        page.js("document.querySelector('#libQ').focus()")
        page.send("Input.insertText", text="piano")
        page.js("document.querySelector('#libQ').dispatchEvent(new Event('input'))")
        typed = page.js("document.querySelector('#libQ').value")
        check("关键词真的进了输入框", typed == "piano", "value=%r" % typed)

        # 用真鼠标点「搜索」按钮：先取坐标，再命中测试，再点
        btn_xy = page.js("""(()=>{const b=document.querySelector('#btnLibSearch');
            if(!b) return null; const r=b.getBoundingClientRect();
            return {x:r.left+r.width/2, y:r.top+r.height/2};})()""")
        check("搜索按钮可定位", bool(btn_xy))
        if btn_xy:
            # ★ 用 scrollIntoView，不要 window.scrollTo(0, scrollHeight)。
            #   踩过：原来这里"滚到页面底部"再量坐标 —— 那个假设一旦有人在曲库
            #   **下面**再加一个面板（声音市场就是这么加的），页面变长，滚到底时
            #   曲库的搜索按钮正好被顶到视口上方 ⇒ 真鼠标报 OUT_OF_VIEWPORT，
            #   一连 7 条判据全红，而它们红的其实是"测试脚本的几何假设"，
            #   不是功能坏了。滚到元素本身才是"用户会怎么做"。
            page.js("""(()=>{const b=document.querySelector('#btnLibSearch');
                if(b) b.scrollIntoView({block:'center'}); return true;})()""")
            time.sleep(0.4)
            btn_xy = page.js("""(()=>{const b=document.querySelector('#btnLibSearch');
                const r=b.getBoundingClientRect();
                return {x:r.left+r.width/2, y:r.top+r.height/2};})()""")
            top = page.real_click(btn_xy["x"], btn_xy["y"])
            check("真鼠标点在搜索按钮上(命中测试)", "btnLibSearch" in str(top), "top=%s" % top)

        print("   等待搜索返回…")
        got = False
        for _ in range(50):
            time.sleep(1)
            n = page.js("document.querySelectorAll('#libGrid .lib-card').length")
            if n and n > 0:
                got = True
                break
        n = page.js("document.querySelectorAll('#libGrid .lib-card').length")
        check("搜索真的返回了卡片", got, "卡片数=%s" % n)

        srcs = page.js("document.querySelectorAll('#libSrcs .lib-src').length")
        check("源状态条有内容", (srcs or 0) >= 1, "源数=%s" % srcs)
        check("源状态显示为可用(绿)",
              page.js("!!document.querySelector('#libSrcs .lib-src.ok')"))

        print("\n=== 3. 授权判定真的在起作用 ===")
        okN = page.js("document.querySelectorAll('#libGrid .lib-card:not(.blocked)').length")
        check("有可用的卡(非置灰)", (okN or 0) > 0, "ok=%s" % okN)
        check("可用的卡都有授权徽章",
              page.js("""Array.from(document.querySelectorAll('#libGrid .lib-card:not(.blocked)'))
                 .every(c=>!!c.querySelector('.lic.ok'))"""))
        hasBtn = page.js("""Array.from(document.querySelectorAll('#libGrid .lib-card:not(.blocked)'))
                 .some(c=>{const b=c.querySelector('.acts .btn');
                   return b && !b.disabled && b.textContent.includes('导进翻唱台')})""")
        check("可用的卡有可点的『导进翻唱台』按钮", bool(hasBtn))

        print("\n=== 4. 置灰的真红对照（禁商用必须挡住）===")
        # ★ 负对照必须**自足**，不能指望"线上这次恰好返回一首不可商用的歌"。
        #   踩过：某一轮搜回来 87 首全是 CC-BY ⇒ blocked=0，这条就红了，
        #   而产品其实一点问题没有 —— 红的是外部数据，不是代码。
        #   所以这里改用页面自己的渲染函数 libCardHtml 造一条 status!=ok 的条目，
        #   断言它一定是置灰 + 写明原因 + 按钮禁用。这样每次都能真红也能真绿。
        # 一次造**两条**：不可商用 + 可商用。断言它们结论相反 ——
        # 这样"置灰"这件事是被**对比**出来的，不是单方面看一眼 class 名；
        # 万一哪天所有卡都被置灰（或都不置灰），这里照样会红。
        r4 = page.js("""(()=>{
            const box = document.querySelector('#libGrid');
            box.insertAdjacentHTML('afterbegin',
              libCardHtml({id:'__probe_nc__', title:'验收构造·禁商用', artist:'probe',
                status:'blocked', license:'CC-BY-NC',
                note:'禁商用（CC-BY-NC），不能用于翻唱发布'})
              + libCardHtml({id:'__probe_ok__', title:'验收构造·可商用', artist:'probe',
                status:'ok', license:'CC-BY'}));
            const cs = box.querySelectorAll('.lib-card');
            const c = cs[0], okc = cs[1];
            const w = c.querySelector('.why');
            const b = c.querySelector('.acts .btn');
            const ob = okc.querySelector('.acts .btn');
            return {cls: c.className, okCls: okc.className,
                    hasWhy: !!(w && w.textContent.trim().length > 0),
                    btnDisabled: !!(b && b.disabled),
                    noFetch: !(b && b.getAttribute('onclick')),
                    okNotBlocked: !/\\bblocked\\b/.test(okc.className),
                    okHasFetch: !!(ob && ob.getAttribute('onclick'))};
        })()""")
        check("成对对照：可商用的那条**不**置灰且能导（说明置灰不是一律加的）",
              bool(r4) and r4.get("okNotBlocked") and r4.get("okHasFetch"),
              json.dumps(r4, ensure_ascii=False)[:120] if r4 else "?")
        check("自足负对照：不可商用的条目被置灰",
              bool(r4) and "blocked" in (r4.get("cls") or ""),
              json.dumps(r4, ensure_ascii=False)[:120] if r4 else "?")
        check("置灰卡写明了原因（不是只变个颜色）", bool(r4) and r4.get("hasWhy"))
        check("置灰卡的按钮是禁用的", bool(r4) and r4.get("btnDisabled"))
        check("置灰卡的按钮不带导歌动作（点不动才是真的挡住）",
              bool(r4) and r4.get("noFetch"))

        # 线上真实数据里的置灰卡（如果这次搜到了）也要满足同样的三条。
        # 搜不到就不作数 —— 那是源这次没给 NC 歌，不是产品坏了。
        page.js("""(()=>{const s=document.querySelector('#libLic');
            s.value='all'; s.dispatchEvent(new Event('change'));})()""")
        page.js("libSearch()")
        blockedN = 0
        for _ in range(240):
            time.sleep(1)
            blockedN = page.js(
                "document.querySelectorAll('#libGrid .lib-card.blocked').length") or 0
            if blockedN > 0 and page.js("typeof LIB_BUSY==='undefined'?true:!LIB_BUSY"):
                break
        if blockedN > 0:
            check("线上搜到的置灰卡同样写明原因且按钮禁用",
                  page.js("""Array.from(document.querySelectorAll('#libGrid .lib-card.blocked'))
                     .every(c=>{const w=c.querySelector('.why'),b=c.querySelector('.acts .btn');
                       return w && w.textContent.trim().length>0 && b && b.disabled;})"""),
                  "blocked=%s" % blockedN)
        else:
            print("  （说明）这次线上没返回不可商用的歌，跳过「线上置灰卡」这一条"
                  "—— 它取决于源，不是产品行为；上面自足的那四条才是硬判据。")

        print("\n=== 4b. 深挖翻页：数量、唯一性、增量 ===")
        # 切回可商用 + 最大档位，用真实鼠标点搜索按钮（不用 .click()）
        page.js("""(()=>{const l=document.querySelector('#libLic'); l.value='by';
            l.dispatchEvent(new Event('change'));
            const d=document.querySelector('#libDeep'); d.value='4';
            d.dispatchEvent(new Event('change'));})()""")
        # 同上：先滚到按钮本身再量坐标（结果卡片一多，按钮会被顶出视口）
        page.js("""(()=>{const b=document.querySelector('#btnLibSearch');
            if(b) b.scrollIntoView({block:'center'}); return true;})()""")
        time.sleep(0.4)
        bxy = page.js("""(()=>{const b=document.querySelector('#btnLibSearch');
            const r=b.getBoundingClientRect();
            return {x:r.left+r.width/2,y:r.top+r.height/2};})()""")
        if bxy:
            top = page.real_click(bxy["x"], bxy["y"])
            check("深挖搜索按钮可被真鼠标命中", bool(top) and "btn" in str(top),
                  "命中=%s" % top)
        # ★ 等待条件以前是 `deepN > 40 and "搜索" in btnTxt`。
        #   这是**假绿/假红的双重来源**：按钮空闲时文案就是"搜索"、忙时是"搜索中…"，
        #   `"搜索" in` 两种状态都成立 ⇒ 它一看到**上一次搜索残留**的卡片就退出，
        #   量的根本不是这次深挖的结果（实测因此报过 total=87 uniq=22，
        #   而服务端同一档位返回 87 条、零重复）。
        #   正确信号是"搜索中"消失 + LIB_BUSY 为假，再要求计数稳定。
        for _ in range(30):
            time.sleep(0.3)
            if page.js("typeof LIB_BUSY!=='undefined' && LIB_BUSY===true"):
                break
        deepN, prevN, stable = 0, -1, 0
        for _ in range(300):
            time.sleep(1)
            deepN = page.js("document.querySelectorAll('#libGrid .lib-card').length") or 0
            btnTxt = page.js("document.querySelector('#btnLibSearch').textContent") or ""
            if (page.js("typeof LIB_BUSY==='undefined'?true:!LIB_BUSY")
                    and "搜索中" not in btnTxt and deepN > 0):
                if deepN == prevN:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
            prevN = deepN
        check("深挖档位返回了远多于单页的卡片",
              (deepN or 0) > 60, "卡片数=%s（单页上限29，>60 才说明翻页真生效）" % deepN)
        # 唯一性：翻页聚合最容易出的 bug 就是同一首出现两次，
        # 界面上"看着多"其实没多 —— 所以这条必须查真实的 id 集合。
        # 用 data-lib-id（卡片自带的权威 id）而不是去解析 onclick：
        # 置灰卡的按钮没有 onclick，会被漏掉，看着像"有重复"。
        uniq = page.js("""(()=>{const ids=new Set(); let noId=0;
            const cards=document.querySelectorAll('#libGrid .lib-card');
            cards.forEach(c=>{const d=c.getAttribute('data-lib-id');
              if(d) ids.add(d); else noId++;});
            return {total:cards.length, uniq:ids.size, noId:noId};})()""")
        check("深挖结果无重复卡片（id 唯一）",
              bool(uniq) and uniq["total"] == uniq["uniq"] and uniq["total"] > 0,
              "total=%s uniq=%s 无id=%s" % (uniq.get("total") if uniq else "?",
                                            uniq.get("uniq") if uniq else "?",
                                            uniq.get("noId") if uniq else "?"))
        check("每张卡都带 id（没有 id 的卡根本导不进来）",
              bool(uniq) and uniq.get("noId") == 0)
        check("提供了『再挖一批』的继续入口",
              page.js("!!Array.from(document.querySelectorAll('#libGrid button')).find(b=>b.textContent.includes('再挖一批'))"))

        print("\n=== 5. 导歌真下载 + 真的进翻唱台②号槽 ===")
        page.js("""(()=>{const s=document.querySelector('#libLic');
            s.value='by'; s.dispatchEvent(new Event('change'));})()""")
        # ★ 必须把「深挖档位」显式降回 1。
        #   第 4b 段为了验翻页把 #libDeep 设成了 '4'，而 libSearch() 每次都会读这个
        #   下拉框 —— 于是这一段本来只想"搜一下、点下载"，实际发的却是 5 页的深挖。
        #   本机实测：deep=8 要 154s、deep=4 约 95s，而下面的等待只有 90s
        #   ⇒ 网格还停在 spinner（0 张卡）时循环就到期了，报成"可点的下载按钮可定位
        #   FAIL"。这是**判据的耐心不够**，不是产品坏了：19:16 那轮就是绿的，
        #   网络快一点就绿、慢一点就红 —— 典型的间歇性红（时序不稳），单轮绿不算数。
        #   本段要验的是"下载 + 进②号槽"，不是翻页，所以档位降到 1 才是它该有的前提；
        #   同时把耐心提到 240s，给慢网留余量。判据本身一个字没改。
        page.js("""(()=>{const d=document.querySelector('#libDeep');
            if(d){d.value='1'; d.dispatchEvent(new Event('change'));}})()""")
        page.js("libSearch()")
        # 先等搜索真的跑完（LIB_BUSY 是"同时只允许一次搜索"的闸）再等卡片出现。
        # 不等的话会读到上一轮的网格 —— 上一轮可能是"深挖 87 首"那种很慢的请求，
        # 结果就是判据时机不对，红得莫名其妙。
        for _ in range(240):
            time.sleep(1)
            if (page.js("typeof LIB_BUSY==='undefined' ? true : !LIB_BUSY")
                    and (page.js("document.querySelectorAll('#libGrid .lib-card').length") or 0) > 0):
                break
        # 真鼠标点第一张可用卡的「导进翻唱台」
        # 必须先滚进视口：搜索能出 80+ 张卡，下载按钮的位置取决于卡片怎么排布，
        # 完全可能在视口外（视口高 904，按钮常落在 y≈1000+）。真鼠标按视口坐标点，
        # 对屏幕外的东西必然打空 —— 这个坑本轮踩了两次（本地歌、下载按钮）。
        sel = "#libGrid .lib-card:not(.blocked) .acts .btn.pri"
        n_ok = page.js("document.querySelectorAll(%s).length" % json.dumps(sel)) or 0
        n_all = page.js("document.querySelectorAll('#libGrid .lib-card').length") or 0
        n_blocked = page.js("document.querySelectorAll('#libGrid .lib-card.blocked').length") or 0
        # 红了要能自诊断：把网格当下的文案也带上 —— 如果它写着「正在搜索…」，
        # 一眼就知道是"还没搜完"，而不是"搜到了 0 首"（两者都是卡片=0，但原因完全不同）。
        grid_txt = (page.js("((document.querySelector('#libGrid')||{}).textContent||'')"
                            ".trim().slice(0,60)") or "").replace("\n", " ")
        busy = page.js("typeof LIB_BUSY==='undefined' ? 'undefined' : LIB_BUSY")
        check("可点的下载按钮可定位", n_ok > 0,
              "可用按钮=%s / 卡片=%s（含 blocked %s）| 网格文案=%r | LIB_BUSY=%s"
              % (n_ok, n_all, n_blocked, grid_txt, busy))
        # ★★ 1b 段已经用本地歌把②号槽填上过 ⇒ 槽位此刻**本来就是 has**。
        #   所以"点完看 #slotSong.has"会**立刻为真**、根本不等这次下载
        #   —— 判据靠上一段的残留状态恒绿（同一个坑：rule 12i）。
        #   现在记下当前文案，要求这次下载把它**换成另一首**才算数。
        slot_before = (page.js("(document.querySelector('#slotSong')||{}).textContent||''")
                       or "").strip()
        top = page.click_el(sel)
        # ★ 这里原来写的是 `"btn" in str(top)` —— 恒真！
        #   click_el 找不到元素时返回的是 "NOT_FOUND:<选择器>"，而选择器里
        #   就带 ".btn"，于是"没找到"被当成"点中了"。判据自己坏掉，比没有判据更坏。
        hit_ok = (not str(top).startswith(("NOT_FOUND", "NO_BOX", "OUT_OF_VIEWPORT"))
                  and "btn" in str(top))
        check("真鼠标点在下载按钮上(命中测试)", hit_ok, "top=%s" % top)

        print("   等待下载完成(可能几十秒)…")
        done = False
        songNow = ""
        for _ in range(120):
            time.sleep(1.5)
            songNow = (page.js("""(()=>{const s=document.querySelector('#slotSong');
                if(!s || !s.classList.contains('has')) return '';
                return (s.textContent||'').trim();})()""") or "").strip()
            if songNow and songNow != slot_before:
                done = True
                break
        check("②号槽被这次下载换成了另一首(不是靠上一段的残留)",
              done, "之前=%r 现在=%r" % (slot_before[:28], songNow[:28]))
        # ★ 这条原来写的是 `textContent 非空`，而占位文案
        #   「② 歌拖 wav / mp3 / 视频进来」本身就非空 —— 槽位根本没填上也绿。
        #   本轮实测到了这个假绿：同一轮里"②号槽真的被填上了"是 FAIL，
        #   而这条却 PASS，还打印着占位文案当"歌名"。
        #   判据必须要求文案是 stageSetSong 写的那一版（带"已就位"）。
        songTxt = page.js("(document.querySelector('#slotSong')||{}).textContent||''")
        check("槽位显示了歌名(真的就位、不是占位文案)",
              "已就位" in songTxt and "拖 wav" not in songTxt, songTxt.strip()[:50])
        # 最直接的证据：完成回调到底弹了哪句话。
        # 「任务完成」这一条正是本轮那个 meta 漏声明 bug 的照妖镜 ——
        # 它一红，任务就永远走不到 done，这两句 toast 一句都不会出现。
        tst = page.js("JSON.stringify(window.__toasts||[])") or "[]"
        check("下载任务真的走到「任务完成」", "任务完成" in tst, tst[:160])
        check("完成回调真的把歌放进了②号槽（toast 为证）",
              "已导进翻唱台" in tst, tst[:160])

        mineN = page.js("document.querySelectorAll('#libMine .lib-card').length")
        check("已导入区出现了这首歌", (mineN or 0) > 0, "已导入=%s" % mineN)

        # 落盘的必须是真音频：让后端量时长 > 0
        try:
            libj = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:8756/api/music/library", timeout=20).read().decode())
            songs = libj.get("songs") or []
            check("曲库里确有记录", len(songs) > 0)
            # 必须按来源挑，不能拿 songs[0]：曲库是按时间倒序的，
            # 而 1b 节的本地测试歌比在线导的歌更新，会把断言串味
            # （踩过：songs[0] 是本地样本，0.5MB/0秒，害得在线导歌误判失败）。
            online = [s for s in songs if s.get("src") != "local"]
            local = [s for s in songs if s.get("src") == "local"]
            s0 = online[0] if online else {}
            check("在线导的歌在曲库里", bool(s0), "在线=%d 本地=%d" % (len(online), len(local)))
            if s0:
                check("落盘文件真实存在", os.path.isfile(s0.get("path", "")),
                      s0.get("path", "")[-46:])
                check("文件大小合理(>200KB)", (s0.get("size") or 0) > 200000,
                      "%.2f MB" % ((s0.get("size") or 0) / 1048576))
                check("在线歌时长量到了(>5秒)", (s0.get("dur") or 0) > 5,
                      "%.1f 秒" % (s0.get("dur") or 0))
            # 本地导入的歌也必须有真实时长 —— 这里曾恒为 0（真 bug）
            check("本地导入的歌也量到了时长",
                  bool(local) and (local[0].get("dur") or 0) > 5,
                  "dur=%s" % (local[0].get("dur") if local else "无本地歌"))
            check("本地歌记录了原文件签名(用于检测被移动)",
                  bool(local) and bool(local[0].get("sig")))
        except Exception as e:
            check("曲库接口可读", False, str(e))

        print("\n=== 6. 负对照：必须实测能红 ===")
        # 6a 不存在的曲 id 必须报错，不能静默成功
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                "http://127.0.0.1:8756/api/music/fetch",
                data=json.dumps({"id": "ccmixter:999999999"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST"), timeout=20)
            jid = json.loads(r.read().decode())["job"]["id"]
            failed = False
            # ★ 耐心必须够：这个任务的失败发生在**远端 ccmixter 页面 404** 之后，
            #   也就是要等一次网络往返。本机实测 26.3s —— 而这里原来只等 30s，
            #   等于把判据架在网络的抖动上（快 4 秒就绿，慢 4 秒就红）。
            #   19:16 那轮绿、这轮红，差的就是这点网络时间，不是代码。
            for _ in range(150):
                time.sleep(1)
                jj = json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:8756/api/job/" + jid, timeout=20).read().decode())["job"]
                if jj.get("state") in ("failed", "done"):
                    failed = (jj["state"] == "failed")
                    break
            check("负对照：伪造曲 id 必须失败", failed,
                  "终态=%s（等了 %ds）" % (jj.get("state"), _ + 1))
        except Exception as e:
            check("负对照：伪造曲 id 必须失败", False, str(e))

        # 6b 搜索接口对空关键词必须 400，不能瞎返回一堆
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:8756/api/music/search?q=", timeout=20)
            check("负对照：空关键词必须被拒", False, "居然返回 200")
        except urllib.error.HTTPError as e:
            check("负对照：空关键词必须被拒", e.code == 400, "HTTP %s" % e.code)

        # 6b-2 本地导入接口的负对照：目录不存在 / 未给目录，都必须 400。
        # 这两条要是放过，界面上就会"扫了个空目录还显示成功"。
        for label, url in [
            ("本地扫描：目录不存在必须被拒",
             "http://127.0.0.1:8756/api/music/local/scan?dir=C:/nope/nothere"),
            ("本地扫描：未给目录必须被拒",
             "http://127.0.0.1:8756/api/music/local/scan?dir="),
        ]:
            try:
                urllib.request.urlopen(url, timeout=20)
                check("负对照：" + label, False, "居然返回 200")
            except urllib.error.HTTPError as e:
                check("负对照：" + label, e.code == 400, "HTTP %s" % e.code)

        # 6b-3 后端直调：坏文件绝不能进曲库（真红，不是靠 UI 遮挡）
        #
        # 口径修正（原先这条一直红，但**不是功能坏了**）：旧断言把真样本
        # notes.mp3 和 3 个坏样本一起送进去，然后要求 added==0 —— 而 notes.mp3
        # 是真音频，被正确导入才是对的。实测 added=1(bad=3)，恰好说明
        # "真样本进、坏样本全拒" 正常工作，是断言自己把两类混在一起数了。
        # 现在拆成两件事分别断言：坏样本必须全拒，真样本必须能进。
        try:
            bad_files = ["broken.wav", "rand.flac", "trunc.mp3"]
            req = urllib.request.Request(
                "http://127.0.0.1:8756/api/music/local/add",
                data=json.dumps({"paths":
                                 [os.path.join(LIB_DIR, f) for f in bad_files]}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())
            check("负对照：坏文件被后端直接拒绝",
                  len(r.get("added", [])) == 0 and len(r.get("bad", [])) == len(bad_files),
                  "送了%d个坏文件 added=%d bad=%d" % (len(bad_files),
                                                    len(r.get("added", [])),
                                                    len(r.get("bad", []))))
            check("负对照：拒绝原因已写明",
                  all(b.get("why") for b in r.get("bad", [])),
                  str(r.get("bad", [])[:1]))
            # 正对照：真音频必须能进（这条钉住"别把好文件也误杀"）
            req2 = urllib.request.Request(
                "http://127.0.0.1:8756/api/music/local/add",
                data=json.dumps({"paths": [os.path.join(LIB_DIR, "notes.mp3")]}).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            r2 = json.loads(urllib.request.urlopen(req2, timeout=30).read().decode())
            check("正对照：真音频不会被误杀",
                  len(r2.get("added", [])) == 1,
                  "added=%d" % len(r2.get("added", [])))
        except Exception as e:
            check("负对照：坏文件被后端直接拒绝", False, str(e)[:120])

        # 6c 源码层面：置灰判定不是写死的
        src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "web", "lib.js"), encoding="utf-8").read()
        check("置灰不是写死的(按 status 判定)",
              "status !== \"ok\"" in src and "blocked" in src)

        print("\n=== 7. 曲库不静默丢歌（新增回归）===")
        # 这段专门盯本轮修的洞：文件一被移走，那首歌以前会从列表里**人间蒸发**
        # （后端 local_library() 用 os.path.isfile 过滤掉了）。
        # 验收方式：真把曲库里某个文件挪走 → 刷新 → 必须还能看到它、且写明原因。
        import shutil as _sh
        songs = page.js("""(async()=>{const j=await (await fetch('/api/music/library')).json();
            return j.songs;})()""", awaitp=True) or []
        moved_path = None
        if songs:
            # 挑一个曲库里已登记、且真实在磁盘上的文件
            for s in songs:
                p = (s.get("path") or "").replace("\\", "\\\\")
                if p and os.path.isfile(p):
                    moved_path = p
                    break
        if moved_path:
            bak = moved_path + ".vprobe"
            _sh.move(moved_path, bak)
            try:
                page.js("loadLibrary()")
                time.sleep(1.2)
                n_all = page.js("document.querySelectorAll('#libMine .lib-card').length")
                n_lost = page.js("document.querySelectorAll('#libMine .lib-card.lost').length")
                why_txt = page.js("(document.querySelector('#libMine .lostwhy')||{}).textContent||''")
                check("文件被移走后条目仍在列表里（不静默消失）",
                      (n_all or 0) >= len(songs), "卡片=%s 曲库=%s" % (n_all, len(songs)))
                check("丢失条目被标为 lost", (n_lost or 0) >= 1, "lost=%s" % n_lost)
                check("写明了丢失原因",
                      bool(why_txt) and ("不在" in why_txt or "移动" in why_txt),
                      "文案=%r" % (why_txt or "")[:40])
                check("丢失条目提供『重新定位』入口",
                      page.js("!!Array.from(document.querySelectorAll('#libMine button')).find(b=>b.textContent.includes('重新定位'))"))
                check("丢失条目提供『从曲库移除』入口",
                      page.js("!!Array.from(document.querySelectorAll('#libMine button')).find(b=>b.textContent.includes('从曲库移除'))"))

                # 负对照：check 接口要能报出 missing，且文件回来后自动清零
                st = page.js("""(async()=>{const j=await (await fetch('/api/music/local/check')).json();
                    return j.stat;})()""", awaitp=True) or {}
                check("check 接口报出 missing（死代码已接通）",
                      (st.get("missing") or 0) >= 1, "stat=%s" % st)
            finally:
                _sh.move(bak, moved_path)
            page.js("loadLibrary()")
            time.sleep(1.2)
            st2 = page.js("""(async()=>{const j=await (await fetch('/api/music/local/check')).json();
                return j.stat;})()""", awaitp=True) or {}
            check("文件放回来后 missing 自动清零（不能永久记账）",
                  (st2.get("missing") or 0) == 0, "stat=%s" % st2)
            check("恢复后不再显示为 lost",
                  (page.js("document.querySelectorAll('#libMine .lib-card.lost').length") or 0) == 0)
        else:
            check("曲库里有可用来做丢失实验的文件", False, "曲库为空，跳过丢失实验")

        print("\n=== 8. 候选目录显示真实歌数（不误导）===")
        page.js("locLoadDirs()")
        time.sleep(2.5)
        dirs = page.js("""(async()=>{const j=await (await fetch('/api/music/local/dirs')).json();
            return j.dirs;})()""", awaitp=True) or []
        check("候选目录都带song_count",
              bool(dirs) and all(("song_count" in d) for d in dirs),
              "字段齐=%s" % all(("song_count" in d) for d in dirs))
        # 关键：0 首的目录必须标出来，不能让用户点进空目录白跑一趟
        zero_dirs = [d for d in dirs if d.get("exists") and d.get("song_count") == 0]
        marked = page.js("""(()=>{
            const spans=Array.from(document.querySelectorAll('#locDirs .loc-dir'));
            return spans.filter(s=>s.textContent.includes('没扫到歌')).length;})()""")
        check("扫不到歌的目录被标注出来",
              (marked or 0) >= len(zero_dirs),
              "0首目录=%s 已标注=%s" % (len(zero_dirs), marked))
        # 负对照：网易云程序目录（实测零歌曲）不该再被当成主力入口
        netease = [d for d in dirs if "Netease" in d["path"] and "Local" in d["path"]]
        check("网易云程序目录不再被标成主要入口",
              all(d.get("song_count") != 0 or "(多加密)" in d["name"] for d in netease),
              "本机实测该目录 0 首，应标为加密缓存")

        print("\n=== 9. 上传入库不是校验后门 ===")
        up = page.js("""(async()=>{
            // 假音频：纯文本改名 .mp3
            const blob=new Blob(['definitely not audio, just plain text here'],{type:'audio/mpeg'});
            const r=await fetch('/api/music/local/upload?name=evil_probe.mp3',{method:'POST',body:blob});
            const j=await r.json();
            return {status:r.status, ok:j.ok, err:j.err||''};
        })()""", awaitp=True) or {}
        check("上传假音频被拒", up.get("ok") is False and up.get("status") == 400,
              "resp=%s" % up)
        check("拒绝原因写明是音频识别失败",
              "音频" in (up.get("err") or ""), "err=%r" % (up.get("err") or "")[:40])
        # 上传路由曾因放在 read_json_body 之后而**永久挂死**，这条钉住它
        check("上传路由不再挂死（有明确状态码返回）",
              isinstance(up.get("status"), int) and up.get("status") > 0)

        print("\n=== 10. 已有功能未被破坏 ===")
        check("角色墙仍在", page.js("!!document.querySelector('#grid')"))
        check("翻唱台按钮存在", page.js("!!document.querySelector('#btnOneClick')"))
        check("app.js 关键函数都在",
              page.js("typeof window.stageSetSong==='function' && "
                      "typeof window.loadRoles==='function'"))
        rolesN = page.js("document.querySelectorAll('#grid .card').length")
        check("角色卡仍能渲染", (rolesN or 0) > 0, "角色数=%s" % rolesN)

        # ★★ 全程零 JS 异常。这一条是本轮补上的"抓这一类 bug 的网"：
        #   上面每一条判据都是"按功能"的，而本轮那个 `meta is not defined`
        #   （tickJobs 每轮抛错 ⇒ 任务永远显示不出"完成"、所有完成回调失效）
        #   一条都不违反 —— 它只让"任务完成"这件事永远不发生。
        #   负对照已实测：把声明去掉，这一条立刻红（REJ: ReferenceError: meta is not defined）。
        #   放在最后跑：前面的段已经把页面用了一遍，能收集到全部异常。
        print("\n=== 11. 全程零 JS 异常（抓「任务永不完成」这一类） ===")
        errs = page.js("JSON.stringify(window.__errs||[])") or "[]"
        try:
            err_list = json.loads(errs)
        except Exception:
            err_list = [str(errs)]
        check("全程没有未捕获的 JS 异常", len(err_list) == 0,
              ("；".join(err_list))[:300])
        # 哨兵：光看"没有异常"不算数 —— 收集器如果是死的，这里永远绿。
        # 先读空、再故意抛一个，必须**真的被抓到**，这条判据才配被相信。
        page.js("setTimeout(()=>{throw new Error('__sentinel_must_be_caught__');},0)")
        time.sleep(0.8)
        after = page.js("JSON.stringify(window.__errs||[])") or "[]"
        check("错误收集器真的在收（哨兵：故意抛一个必须被抓到）",
              "__sentinel_must_be_caught__" in after, after[:200])

        # 截图留证
        try:
            shot = page.send("Page.captureScreenshot", format="png")
            import base64
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "music_library.png")
            open(out, "wb").write(base64.b64decode(shot["data"]))
            print("\n截图: %s" % out)
        except Exception as e:
            print("截图失败: %s" % e)

    finally:
        try:
            page and page.ws.close()
        except Exception:
            pass
        proc.kill()

    print("\n" + "=" * 60)
    print("通过 %d / 失败 %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - " + b)
    print("=" * 60)
    # 判据：真红失败计数为 0 且有实际通过项
    sys.exit(0 if (not BAD and len(OK) > 0) else 1)


if __name__ == "__main__":
    main()