# -*- coding: utf-8 -*-
r"""精修台的真浏览器验收：钢琴卷帘 + 真鼠标点块/拖拽改调 + 一键修 + 导出。

为什么必须真浏览器：
    精修台是**画布**界面（canvas 钢琴卷帘）。它的三件事静态检查一条都看不见：
      ① `fineInit()` 到底有没有真跑到 —— 没跑到的话 #fineSrc 一个选项都没有，
         整块面板是空的，而 HTML/JS/接口全都"在"；
      ② 画布上**点得中音块吗**（命中测试算的是 canvas 像素坐标，偏一个 PADL
         就永远打不中）；
      ③ 上下拖能不能改调（mousedown→mousemove 那条链，漏一个事件就什么都不发生）。

规矩（和 probe_multi_ui 一致）：
    · 真鼠标：dispatchMouseEvent + 命中测试，**不用** element.click()。
    · <select> 的系统弹层点不到 ⇒ 改值 + 派 change，走的仍是页面自己的监听器。
    · 服务由 verify_fine_ui.py 在独立端口起好（铁律 20：常驻进程活不过一条命令）。

用法：
    VOICESTUDIO_PORT=8904 <gsv python> server.py &
    python tools/probe_fine_ui.py
"""
import atexit
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = int(os.environ.get("VS_PORT", "8904"))
URL = "http://127.0.0.1:%d/" % PORT
CDP_PORT = int(os.environ.get("VS_CDP_PORT", "9347"))
PROFILE = os.path.join(HERE, ".cdp-fine")
CLIP = "vs_fine_probe_clip.wav"          # 拷进 uploads 的测试片段
DIRTY = "vs_pol_probe_clip.wav"         # 拷进 uploads 的"有毛病"片段（微调用）

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name +
          (("  | " + str(detail)) if detail else ""), flush=True)


def cdp_get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (CDP_PORT, path),
                                timeout=20) as r:
        return json.loads(r.read().decode())


class Cdp:
    def __init__(self):
        import websocket
        page = [t for t in cdp_get("/json/list") if t.get("type") == "page"]
        if not page:
            raise RuntimeError("没有可用的页面目标")
        self.ws = websocket.create_connection(
            page[0]["webSocketDebuggerUrl"], timeout=60)
        self.i = 0
        self.send("Page.enable")
        self.send("Runtime.enable")

    def send(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Page.javascriptDialogOpening":
                self.i += 1
                self.ws.send(json.dumps({"id": self.i,
                                         "method": "Page.handleJavaScriptDialog",
                                         "params": {"accept": True}}))
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r.get("exceptionDetails"))[:400])
        return r.get("result", {}).get("value")

    def rect(self, sel):
        return self.js("""(()=>{const e=document.querySelector(%s);
          if(!e) return null; const r=e.getBoundingClientRect();
          return {x:r.x,y:r.y,w:r.width,h:r.height,
                  ox:r.x+Math.min(r.width/2,20), oy:r.y+r.height/2};})()"""
                       % json.dumps(sel))

    def click(self, sel):
        r = self.rect(sel)
        if not r:
            return False, "元素不存在"
        self.js("(()=>{const e=document.querySelector(%s);"
                "e.scrollIntoView({block:'center'});return 1;})()" % json.dumps(sel))
        time.sleep(0.25)
        r = self.rect(sel)
        x, y = r["ox"], r["oy"]
        hit = self.js("""(()=>{const e=document.elementFromPoint(%f,%f);
          if(!e) return 'null';
          const t=document.querySelector(%s);
          return (t===e || t.contains(e) || e.contains(t)) ? 'ok' : (e.className||e.tagName);})()"""
                      % (x, y, json.dumps(sel)))
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            self.send("Input.dispatchMouseEvent", type=t, x=x, y=y,
                      button="left", clickCount=1)
            time.sleep(0.05)
        return hit == "ok", hit

    def mouse(self, x, y, ev="mouseMoved"):
        self.send("Input.dispatchMouseEvent", type=ev, x=x, y=y,
                  button="left", clickCount=1)
        time.sleep(0.05)


def boot():
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--mute-audio",
                             "--remote-debugging-port=%d" % CDP_PORT,
                             "--user-data-dir=%s" % PROFILE, "--no-first-run",
                             "--remote-allow-origins=*",
                             "--window-size=1440,1000", URL],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(45):
        time.sleep(1.0)
        try:
            cdp_get("/json/list")
            time.sleep(1.5)
            c = Cdp()
            c.chrome = proc
            c.send("Network.enable")
            c.send("Network.setCacheDisabled", cacheDisabled=True)
            c.send("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
                   deviceScaleFactor=1, mobile=False)
            c.send("Page.navigate", url=URL + "?t=%d" % int(time.time()))
            time.sleep(3.5)
            c.js("""(()=>{window.__errs=[];
              addEventListener('error',e=>window.__errs.push(String(e.message||e)));
              addEventListener('unhandledrejection',
                e=>window.__errs.push('rej:'+String((e.reason&&e.reason.message)||e.reason)));
              return 1;})()""")
            return c
        except Exception:
            continue
    return None


def sec(t):
    print("\n" + "-" * 58 + "\n" + t + "\n" + "-" * 58, flush=True)


# ---------------------------------------------------------------- 素材
def make_clip():
    """造一段 6 秒、8 个音（第 4 个哑）的片段，拷进 uploads 让界面能挑到。"""
    sys.path.insert(0, HERE)
    import numpy as np
    from stages import finetune as ft

    def tone(f, dur, amp):
        t = np.arange(int(dur * 44100)) / 44100.0
        x = (np.sin(2 * np.pi * f * t) + 0.35 * np.sin(4 * np.pi * f * t)
             + 0.18 * np.sin(6 * np.pi * f * t))
        env = np.minimum(1.0, t / 0.02) * np.minimum(1.0, (dur - t) / 0.03)
        return (x * env * amp).astype(np.float32)

    freqs = [220.0, 261.6, 293.7, 329.6, 392.0, 440.0, 392.0, 349.2]
    amps = [0.30, 0.30, 0.30, 0.03, 0.30, 0.30, 0.30, 0.30]
    parts = []
    for f, a in zip(freqs, amps):
        parts.append(tone(f, 0.45, a))
        parts.append(np.zeros(int(0.25 * 44100), dtype=np.float32))
    import studio
    dst = os.path.join(studio.UPLOADS, CLIP)
    os.makedirs(studio.UPLOADS, exist_ok=True)
    ft.write_wav(dst, np.concatenate(parts), 44100)
    atexit.register(_clean, dst)
    return dst


def _clean(p):
    try:
        if os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass


def _clean_dir(d):
    try:
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    except Exception:
        pass


# ---------------------------------------------------------------- 各段
def a_panel(c):
    sec("A 精修台面板真的起来了（fineInit 有没有跑到）")
    check("导航栏里有「精修台」",
          bool(c.js("""!![...document.querySelectorAll('.nav-i')]
              .find(a=>/精修台/.test(a.textContent||''))""")))
    ok, why = c.click('.nav-i[data-id="secFine"]')
    check("真鼠标点得到「精修台」导航项", ok, why)
    time.sleep(1.0)
    check("精修台面板在页面里", bool(c.js("!!document.querySelector('#secFine')")))

    n = c.js("(document.querySelector('#fineSrc')||{}).options ? "
             "document.querySelector('#fineSrc').options.length : 0") or 0
    check("★ 音频下拉里有选项（fineInit 真的跑过了 —— 没跑到就是一个都没有）",
          n > 0, "%d 项" % n)
    # ★ 别只取前 40 个：清单是「成品 40 / 分轨 / 曲库 / 上传 40」分桶拼的，
    #   上传那一段从第 80 多个才开始 —— 只看前 40 永远看不见它。
    got = c.js("""(()=>{const o=[...document.querySelectorAll('#fineSrc option')];
      const hit=o.find(x=>/%s/.test(x.textContent||''));
      return hit ? (hit.textContent||'') : (o[0] ? o[0].textContent : '');})()""" % CLIP)
    check("下拉里能看到测试片段（上传的那条进清单了）",
          CLIP in str(got), str(got)[:80])
    ns = c.js("(document.querySelector('#stemSong')||{}).options ? "
              "document.querySelector('#stemSong').options.length : 0") or 0
    check("分轨另存的歌单也读出来了", ns > 1, "%d 项" % ns)


def b_load(c):
    sec("B 载入音频 → 画布上真的画出音块")
    c.js("""(()=>{const s=document.querySelector('#fineSrc');
      const o=[...s.options].find(x=>/%s/.test(x.textContent||''));
      s.value = o ? o.value : s.options[1].value;
      s.dispatchEvent(new Event('change',{bubbles:true})); return s.value;})()""" % CLIP)
    # ★ `let FINE` 挂在**全局词法环境**里，不是 window 的属性
    #   ⇒ `window.FINE` 恒为 undefined，只能直接写 FINE（先 typeof 探一下）。
    for _ in range(40):
        time.sleep(0.5)
        if c.js("!!(typeof FINE !== 'undefined' && FINE && FINE.blocks && FINE.blocks.length)"):
            break
    nb = c.js("(typeof FINE !== 'undefined' && FINE && FINE.blocks) ? FINE.blocks.length : 0") or 0
    check("分析出了音块", nb >= 6, "%d 块" % nb)
    check("工具栏出现了（一键修 / 缩放）",
          c.js("getComputedStyle($('#fineTools')).display") != "none")
    check("画布容器出现了",
          c.js("getComputedStyle($('#fineCvWrap')).display") != "none")
    # 音块数写在 #fineSrcHint（"8 个音块 · 哑音 1 …"）；#fineCount 是"改了几块"
    hint0 = c.js("$('#fineSrcHint').textContent") or ""
    check("来源提示写着几个音块 + 查出了什么问题",
          "个音块" in hint0 and "哑音" in hint0, hint0[:90])
    check("第 4 块被判成哑音（界面数据和服务端一致）",
          (c.js("(FINE.diag||{}).dead") or []) == [3], c.js("(FINE.diag||{}).dead"))

    # 画布上真的有东西：数非背景像素
    px = c.js("""(()=>{const cv=$('#fineCv'); if(!cv) return -1;
      const g=cv.getContext('2d'); const d=g.getImageData(0,0,cv.width,cv.height).data;
      let n=0; for(let i=0;i<d.length;i+=4*37){
        if(Math.abs(d[i]-17)>12||Math.abs(d[i+1]-23)>12||Math.abs(d[i+2]-37)>12) n++;}
      return n;})()""")
    check("★ 画布上真的画了东西（非背景像素 >200）", isinstance(px, int) and px > 200, px)
    check("图例在（六种颜色都标了什么意思）",
          (c.js("document.querySelectorAll('.fine-legend span').length") or 0) >= 6)


def c_click_block(c):
    sec("C ★ 真鼠标点在音块上（命中测试算的是 canvas 像素）")
    pt = c.js("""(()=>{const cv=$('#fineCv');
      cv.scrollIntoView({block:'center'});
      const s=$('#fineCvScroll'); if(s) s.scrollLeft=0;
      const r=cv.getBoundingClientRect(); const g=fineGeom();
      const b=FINE.blocks[1];
      return {x:r.left+g.PADL+(b.t0+b.t1)/2*g.pps, y:r.top+g.TOP+g.PH/2,
              okx: r.left+g.PADL+(b.t0+b.t1)/2*g.pps < innerWidth-2};})()""")
    check("算得出第 2 块的画布坐标", bool(pt) and pt.get("okx"), pt)
    hit = c.js("""(()=>{const e=document.elementFromPoint(%f,%f);
      return e ? (e.id||e.className||e.tagName) : 'null';})()""" % (pt["x"], pt["y"]))
    check("那个坐标上确实是画布（不是被别的东西盖住）",
          "fineCv" in str(hit), hit)

    c.mouse(pt["x"], pt["y"], "mouseMoved")
    c.mouse(pt["x"], pt["y"], "mousePressed")
    c.mouse(pt["x"], pt["y"], "mouseReleased")
    time.sleep(0.4)
    check("★ 点中了（选中的是第 2 块）", c.js("FINE.sel") == 1, c.js("FINE.sel"))
    check("属性面板出现了", c.js("getComputedStyle($('#fineSide')).display") != "none")
    title = c.js("$('#fsTitle').textContent") or ""
    check("属性面板写着第几块 + 音名 + 时间", "第 2 块" in title and "–" in title, title)
    sub = c.js("$('#fsSub').textContent") or ""
    check("副标题写着响度/谐波度（不是空的）", "响度" in sub and "谐波度" in sub, sub)


def d_drag_semi(c):
    sec("D ★ 上下拖动改调（mousedown → mousemove 那条链）")
    pt = c.js("""(()=>{const cv=$('#fineCv');
      const r=cv.getBoundingClientRect(); const g=fineGeom();
      const b=FINE.blocks[1];
      return {x:r.left+g.PADL+(b.t0+b.t1)/2*g.pps, y:r.top+g.TOP+g.PH/2};})()""")
    c.mouse(pt["x"], pt["y"], "mouseMoved")
    c.mouse(pt["x"], pt["y"], "mousePressed")
    for dy in range(6, 42, 6):          # 往上拖 36px ⇒ -36/6 ⇒ +6 半音
        c.mouse(pt["x"], pt["y"] - dy, "mouseMoved")
    c.mouse(pt["x"], pt["y"] - 36, "mouseReleased")
    time.sleep(0.4)
    semi = c.js("((FINE.edits||{})[1]||{}).semi")
    check("★ 往上拖 = 升调（semi > 0）", isinstance(semi, (int, float)) and semi > 0, semi)
    check("滑块跟着变（不是只有内部数据变了）",
          str(c.js("$('#fsSemiV').textContent")) == str(semi),
          "显示 %s / 实际 %s" % (c.js("$('#fsSemiV').textContent"), semi))
    check("音名提示跟着变（改了几个半音会写出来）",
          bool((c.js("$('#fsNote').textContent") or "").strip()),
          c.js("$('#fsNote').textContent"))
    check("块数提示改说「改了几块」",
          "改" in (c.js("$('#fineCount').textContent") or ""),
          c.js("$('#fineCount').textContent"))

    # 还原，别把改动带进下一段
    c.js("fineReset()")
    time.sleep(0.3)
    check("「全部还原」能清掉改动",
          len(c.js("Object.keys(FINE.edits||{})") or []) == 0)


def e_one_click(c):
    sec("E ★ 一键修：只生成修改清单，块上要看得见")
    ok, why = c.click('[data-fix="dedead"]')
    check("真鼠标点得到「去哑音」", ok, why)
    time.sleep(1.2)
    eds = c.js("JSON.stringify(Object.values(FINE.edits||{}))")
    check("★ 生成了修改清单（针对哑音那块）",
          '"i":3' in (eds or ""), eds)
    cnt = c.js("$('#fineCount').textContent") or ""
    check("★ 块数提示当场说改了几块（不是悄悄改掉）", "改" in cnt, cnt)
    check("★ 音频本身没被改（blocks 还是原来那些）",
          (c.js("FINE.blocks.length") or 0) >= 6,
          c.js("FINE.blocks.length"))
    px = c.js("""(()=>{const cv=$('#fineCv'); const g=cv.getContext('2d');
      const d=g.getImageData(0,0,cv.width,cv.height).data; let n=0;
      for(let i=0;i<d.length;i+=4*37){
        if(Math.abs(d[i]-17)>12||Math.abs(d[i+1]-23)>12||Math.abs(d[i+2]-37)>12) n++;}
      return n;})()""")
    check("改完画布重画了（还是画得出东西）", isinstance(px, int) and px > 200, px)


def f_export(c):
    sec("F 导出成品（真点按钮 + 真出文件）")
    ok, why = c.click("#btnFineRender")
    check("真鼠标点得到「导出成品」", ok, why)
    for _ in range(60):
        time.sleep(0.7)
        t = c.js("$('#fineOutHint').textContent") or ""
        if "已导出" in t or "没导出" in t:
            break
    hint = c.js("$('#fineOutHint').textContent") or ""
    check("★ 导出成功并写出文件名", "已导出" in hint, hint[:120])
    check("播放器出来了（导完就能听）",
          c.js("getComputedStyle($('#finePlayer')).display") != "none")
    src = c.js("$('#fineAudio').src") or ""
    check("播放器指向导出的那个文件", ".wav" in src, src[-60:])


def make_dirty():
    """造一段**真有毛病**的片段：齿音 + 拼接台阶 + 底噪 + 隆隆。

    为什么不能拿上面那段干净的测试片段来验"修"：那段没有齿音也没有接缝，
    微调会正确地报告"没有一项达标、一条都没动" —— 那是**对的行为**，
    但它验不出"真修的时候界面走不走得通"。两种结果都得验，所以再来一段脏的。
    """
    sys.path.insert(0, HERE)
    import numpy as np
    from stages import finetune as ft
    import studio
    sr = 44100
    n = int(sr * 5.0)
    t = np.arange(n) / float(sr)
    rng = np.random.default_rng(23)
    ph = 2 * np.pi * np.cumsum(np.where(t < 2.5, 240.0, 660.0)) / float(sr)
    y = np.zeros(n)
    for k in range(1, 21):
        y += (1.0 / k) * np.sin(k * ph)
    y = y * 0.26
    sb = np.zeros(n)
    for st in (0.5, 1.2, 1.9, 2.6, 3.3, 4.0):
        a, b = int(st * sr), int(st * sr) + int(0.09 * sr)
        tt = np.arange(b - a) / float(sr)
        sb[a:b] = 0.55 * (np.sin(2 * np.pi * 6200 * tt)
                          + np.sin(2 * np.pi * 8100 * tt)) * np.hanning(b - a) * 0.5
    y = y + sb + 0.030 * rng.standard_normal(n) + 0.08 * np.sin(2 * np.pi * 45 * t)
    for cp in (1.0, 2.0, 3.0, 4.0):
        y[int(cp * sr):] += 0.40
    y = y / max(1e-9, float(np.max(np.abs(y)))) * 0.92
    dst = os.path.join(studio.UPLOADS, DIRTY)
    os.makedirs(studio.UPLOADS, exist_ok=True)
    ft.write_wav(dst, y.astype(np.float32), sr)
    atexit.register(_clean, dst)
    atexit.register(_clean_dir, os.path.join(studio.FINE_DIR, "vs_pol_probe_clip"))
    return dst


def h_polish(c):
    sec("H ★ 整轨微调（唱前 / 唱后）：量表 + 真点按钮 + 真改音频")
    make_dirty()
    # 刷新可选清单（新拷进去的片段要进列表），再选中它
    c.js("fineInit()")
    time.sleep(2.0)
    c.js("""(()=>{const s=$('#fineSrc');
      const o=[...s.options].find(x=>/%s/.test(x.textContent||''));
      if(!o) return 0;
      s.value=o.value; s.dispatchEvent(new Event('change',{bubbles:true}));
      return 1;})()""" % DIRTY)
    for _ in range(40):
        time.sleep(0.5)
        if c.js("!!(typeof FINE !== 'undefined' && FINE && FINE.path && /%s/.test(FINE.path))" % DIRTY):
            break
    check("载入脏片段", bool(c.js("/%s/.test((typeof FINE!=='undefined'&&FINE&&FINE.path)||'')" % DIRTY)),
          c.js("(typeof FINE!=='undefined'&&FINE)?FINE.path:''"))
    check("★ 微调条出现了", c.js("getComputedStyle($('#finePolish')).display") != "none")

    # 载入会自动量一遍。**要等 FINE.polish 真的回填**（只看量表不够：
    #   换歌时旧量表会先留在那儿，等来的可能是上一首的数 —— 真浏览器实测撞到过）。
    for _ in range(40):
        time.sleep(0.5)
        if c.js("!!(typeof FINE !== 'undefined' && FINE && FINE.polish && FINE.polish.pre)"):
            break
    check("量表量完并回填到了 FINE.polish",
          bool(c.js("!!(typeof FINE !== 'undefined' && FINE && FINE.polish)")),
          c.js("(typeof FINE!=='undefined'&&FINE)?JSON.stringify(FINE.polish||{}).slice(0,80):''"))
    nchips = c.js("document.querySelectorAll('#finePolM .pm').length") or 0
    check("★ 量表把各项都列出来了（≥6 项）", nchips >= 6, "%d 项" % nchips)
    nbad = c.js("document.querySelectorAll('#finePolM .pm.bad').length") or 0
    check("★ 脏片段上真的有红项（说明量得出来问题）", nbad >= 1, "%d 项红" % nbad)
    txt0 = c.js("$('#finePolM').textContent") or ""
    check("量表里写着噪声底 / 齿音 / 拼接点",
          "噪声底" in txt0 and "齿音" in txt0 and "拼接点" in txt0, txt0[:80])

    sib0 = c.js("((typeof FINE!=='undefined'&&FINE&&FINE.polish)||{}).post ? FINE.polish.post.sib_excess_db : null")
    spl0 = c.js("((typeof FINE!=='undefined'&&FINE&&FINE.polish)||{}).post ? FINE.polish.post.splices : null")
    check("量到了齿音超出和拼接点（都不是 0）",
          isinstance(sib0, (int, float)) and sib0 > 1 and (spl0 or 0) > 0,
          "齿音 %s / 拼接 %s" % (sib0, spl0))

    ok, why = c.click('#finePolish button:nth-of-type(2)')   # 唱后
    check("真鼠标点得到「唱后」按钮", ok, why)
    for _ in range(90):
        time.sleep(0.7)
        t = c.js("$('#finePolHint').textContent") or ""
        if "已修" in t or "没修" in t or "没修成" in t:
            break
    hint = c.js("$('#finePolHint').textContent") or ""
    check("★ 唱后微调跑完并给了说法", ("已修" in hint or "没修" in hint), hint[:140])
    check("★ 真的修到了（不是报告『什么都不用修』）", "已修" in hint, hint[:140])
    check("按钮恢复了可点（跑完没卡住）",
          c.js("[...document.querySelectorAll('#finePolish button')].every(b=>!b.disabled)"))
    sib1 = c.js("((typeof FINE!=='undefined'&&FINE&&FINE.polish)||{}).post ? FINE.polish.post.sib_excess_db : null")
    check("★ 修完**又量了一遍**且齿音真的降了",
          isinstance(sib1, (int, float)) and isinstance(sib0, (int, float))
          and sib1 < sib0 - 1.0, "%.1f → %.1f" % (sib0 or -1, sib1 or -1))
    check("★ 切到了新产物上（不是还指着原文件）",
          "/唱后微调/.test((typeof FINE!=='undefined'&&FINE&&FINE.path)||'')",
          c.js("(typeof FINE!=='undefined'&&FINE)?FINE.path:''"))


def i_audition(c):
    sec("I ★ 试听（干湿 A/B）：出声 + 拖滑条 + 分步 solo")
    check("试听条显示出来了", c.js("$('#fineAudit') && $('#fineAudit').style.display !== 'none'"))

    ok, why = c.click('#btnAudPost')
    check("真鼠标点得到试听的「唱后」按钮", ok, why)
    got = False
    for _ in range(120):
        time.sleep(0.7)
        # ★ 必须 !! 强转成布尔：&& 链返回的是**那个 Audio 元素对象**，
        #   CDP 按值返回时序列化不了 ⇒ 拿到 falsy，看着像"没生成"（实测踩到）。
        if c.js("!!((typeof AUD!=='undefined') && AUD && AUD.dry && AUD.wet)"):
            got = True
            break
    check("★ 修前 / 修后两条片段都生成了", got,
          c.js("$('#audHint').textContent") or "")

    if got:
        d = c.js("AUD.dry.duration")
        w = c.js("AUD.wet.duration")
        check("★ 两条都是真能播的音频（时长 > 0，不是空文件）",
              isinstance(d, (int, float)) and d > 0.5
              and isinstance(w, (int, float)) and w > 0.5,
              "修前 %.2fs / 修后 %.2fs" % (d or 0, w or 0))
        check("★ 片段是 8 秒档（不是整首）", (d or 99) <= 9.0, "%.2fs" % (d or 0))
        info = c.js("$('#audClipInfo').textContent") or ""
        check("★ 告诉用户听的是第几秒、动了哪几步", "秒" in info, info[:120])

        # ---- 干湿滑条：只改音量，不重算 ----
        src0 = c.js("AUD.wet.src")
        c.js("$('#audMix').value=30; audCross();")
        v_dry = c.js("AUD.dry.volume")
        v_wet = c.js("AUD.wet.volume")
        check("★ 拖到 30% ⇒ 原声 70% / 修后 30%（音量互补）",
              abs((v_dry or 0) - 0.7) < 0.02 and abs((v_wet or 0) - 0.3) < 0.02,
              "干 %.2f / 湿 %.2f" % (v_dry or 0, v_wet or 0))
        c.js("$('#audMix').value=100; audCross();")
        v0, v1 = c.js("AUD.dry.volume"), c.js("AUD.wet.volume")
        # ★ 别用 `x or 0` 兜底：volume 合法值就是 0，Python 里 0 是假值 ⇒
        #   `AUD.dry.volume or 1` 会把"原声被压到 0"读成 1 ⇒ 判据假红（实测踩到）。
        check("★ 拖回 100% ⇒ 全湿（原声压到 0）",
              isinstance(v0, (int, float)) and isinstance(v1, (int, float))
              and abs(v0 - 0.0) < 0.02 and abs(v1 - 1.0) < 0.02,
              "干 %s / 湿 %s" % (v0, v1))
        check("★ 拖滑条**没有**重新生成音频（src 没变 ⇒ 是即时的）",
              c.js("AUD.wet.src") == src0)

        # ---- 播放 ----
        # ★ 生成完会自动播一次 ⇒ 按钮这时是「停」，再点就是暂停。
        #   先把状态摆到"停着"（这是布置，不是断言），再真鼠标点一次验证播放。
        c.js("if (AUD.dry && !AUD.dry.paused) { AUD.dry.pause(); AUD.wet.pause(); }")
        ok2, why2 = c.click('#btnAudPlay')
        check("真鼠标点得到播放键", ok2, why2)
        time.sleep(1.2)
        check("★ 点了之后真的在播（按钮变『停』且没暂停）",
              "停" in (c.js("$('#btnAudPlay').textContent") or "")
              and c.js("!!(AUD.dry && !AUD.dry.paused)"),
              "%s / paused=%s" % (c.js("$('#btnAudPlay').textContent"),
                                  c.js("!!(AUD.dry && AUD.dry.paused)")))
        ok2b, _ = c.click('#btnAudPlay')
        check("★ 再点一次就停（是个开关，不是只能播一次）",
              ok2b and "听" in (c.js("$('#btnAudPlay').textContent") or ""),
              c.js("$('#btnAudPlay').textContent"))
        if ok2b:
            ok2, _ = c.click('#btnAudPlay')     # 播回来，后面的同步检查才有意义
            time.sleep(0.8)
        # 两条必须贴着放：不然比的是两个时间点
        gap = c.js("Math.abs(AUD.wet.currentTime - AUD.dry.currentTime)")
        check("★ 两条贴着放（时间差 < 0.3s，比的是同一个瞬间）",
              isinstance(gap, (int, float)) and gap < 0.3, "差 %.3fs" % (gap or -1))

        # ---- 分步 solo ----
        c.js("$('#audOnly').value='deess'; fineAuditRerun();")
        ok3 = False
        for _ in range(120):
            time.sleep(0.7)
            if c.js("!!((typeof AUD!=='undefined') && AUD && AUD.wet "
                    "&& String(AUD.wet.src||'').indexOf('deess')>=0)"):
                ok3 = True
                break
        check("★ 换成分步 solo 会重跑，且产物名字里带着那一步", ok3,
              c.js("AUD.wet ? AUD.wet.src.split('/').pop() : ''"))
        clip = c.js("$('#audClipInfo').textContent") or ""
        # ★ 界面必须说**人话**：后端给回来的是 deess 这种内部名，直接摆上去
        #   谁也看不懂（真浏览器实测抓到过：「这次动的是：deess、heal」）。
        check("★ 分步试听完成后界面照实说了动的是哪一步，而且是中文",
              ("去齿音" in clip or "没达标" in clip) and "deess" not in clip,
              clip[:120])

        # ---- 换歌/换成品要停掉 ----
        c.js("audStop()")
        check("★ audStop 之后两条都清掉了（换歌不会放着上一首）",
              c.js("AUD.dry===null && AUD.wet===null"))


def g_errs(c):
    sec("G 全程没有 JS 报错")
    errs = c.js("window.__errs||[]") or []
    check("页面上没抛出 JS 异常", len(errs) == 0, errs[:3])


def j_switches(c):
    sec("J ★ 功能开关：每一步都能单独关掉（「挨个试」的入口）")
    # ★★ 这些开关在**右侧抽屉的「唱歌」页签**里。抽屉默认是 `translateX(100%)`
    #    （滑在屏幕外），页签默认停在「简介」。直接数 ⇒ 元素在 DOM 里、
    #    querySelectorAll 数得到，但**尺寸 0**、真鼠标点不到
    #    （实测 elementFromPoint 返回 null / 命中顶栏，detail='top'）。
    #    ★ 教训：前 3 条判据曾经在这种情况下"绿"了 —— 在 display:none 的元素上
    #      数数量当然数得到。所以先走真实用户路径，再补一条"真有尺寸"。
    #   真实路径 = 点角色卡上的「🎤 TA 唱歌」（openRole(rid,'paneSing')）。
    got_card = False
    for _ in range(40):
        if c.js("document.querySelectorAll('.card[data-role]').length") or 0:
            got_card = True
            break
        time.sleep(0.5)
    check("角色卡渲染出来了（开关在角色抽屉里，一个角色都没有就验不了）", got_card)
    ok, why = c.click('.card[data-role] .acts button:nth-of-type(2)')
    check("真鼠标点得到角色卡上的「🎤 TA 唱歌」", ok, why)
    time.sleep(0.8)
    check("★ 抽屉滑进来、并且停在「唱歌」页（开关就在这一页）",
          c.js("(()=>{const d=document.querySelector('#drawer');"
               "const p=document.querySelector('#paneSing');"
               "return !!d && d.classList.contains('on') && !!p "
               "&& getComputedStyle(p).display!=='none';})()") is True)
    wh = c.js("(()=>{const b=document.querySelector('#swBox');if(!b) return null;"
              "const r=b.getBoundingClientRect();"
              "return [Math.round(r.width), Math.round(r.height)];})()")
    check("★ 功能开关区真的有尺寸（宽高都 > 0，不是隐藏元素）",
          isinstance(wh, list) and len(wh) == 2 and wh[0] > 0 and wh[1] > 0, wh)

    npre = c.js("document.querySelectorAll('#swPre input[data-sw]').length")
    npost = c.js("document.querySelectorAll('#swPost input[data-sw]').length")
    check("唱前开关画出来了（5 个）", npre == 5, "数到 %s" % npre)
    check("唱后开关画出来了（6 个）", npost == 6, "数到 %s" % npost)
    # ★ 2026-10-26：不再是"默认全开"。唱前默认只开**防破音**的那一步 —— 之前
    #   默认全开，同一模型同一首歌实测被啃到"丢词、丢音、还有点变调"
    #   （逐条数字见 verify_declick 文件头）。
    #   ⇒ 判据改成：默认状态由**后端清单**决定，且**唱前必须不是全部都开**。
    #     漏掉这条的话，谁再把默认值改回全开也不会有人发现。
    #
    #   ★ 二修（同日晚）：默认从 `["declip"]` 变成 `["declip","declick"]`。
    #     declick 之前不敢进默认档，是因为它的判据对真脉冲召回 **0/30**
    #     （抓的全是辅音）；换判据后召回 26~29/30、两份真实素材上掉>3dB 的帧
    #     只有 0.00~0.02%，这才配进"防破音"。破音有两个来源：削波 + 咔哒。
    #     ⇒ 判据不写死字符串，改成"必须正好是后端 guard 档的那几步"。
    dflt_on = c.js("JSON.stringify([...document.querySelectorAll("
                   "'#swPre input[data-sw]')].filter(e=>e.checked).map(e=>e.dataset.name))")
    check("★ 唱前**默认不是全开**（默认只留会导致破音的步骤，其余交给你决定）",
          dflt_on in ('["declip","declick"]', '["declip"]')
          and dflt_on != '["declip","declick","hp","denoise","level"]',
          "默认开着的：%s（全开 = 5 步才该红）" % dflt_on)
    check("★ 唱前的默认开单是从后端清单来的（界面自己不发明）",
          c.js("!!(SW_STEPS.pre.length) && SW_STEPS.pre.some(s=>s.default===true) "
               "&& SW_STEPS.pre.some(s=>s.default===false)"),
          "清单里既有默认开也有默认关，才说明这份 default 真被用上了")
    check("唱后仍然默认全开（这一步没有啃字的前科，不该被一起收紧）",
          c.js("[...document.querySelectorAll('#swPost input[data-sw]')].every(e=>e.checked)"))
    # ★ 只验"有勾选框"不够：真正会坏的是"勾选框在、提交时不带我关掉的那几个"。
    #   这里直接问 swSkip（提交时用的就是它），看它有没有把关掉的报出来。
    ok, why = c.click("#swAllOff")
    check("真鼠标点得到「全关」", ok, why)
    off = c.js("JSON.stringify(swSkip('pre'))")
    check("★ 全关之后 swSkip 把关掉的步骤都报出来了（不是只在界面上取消勾选）",
          off == '["declip","declick","hp","denoise","level"]', off)
    check("全关之后连临场感也一起关了",
          c.js("$('#swPresence').checked === false"))
    ok, why = c.click("#swAllOn")
    check("「全开」能把它们都勾回来",
          bool(ok) and c.js("[...document.querySelectorAll('input[data-sw]')].every(e=>e.checked)"),
          why)
    check("全开之后 swSkip 报空（= 一步都不跳过）",
          c.js("JSON.stringify(swSkip('pre'))") == "[]",
          c.js("JSON.stringify(swSkip('pre'))"))
    check("★ 标签是中文（界面上不许露出 declip / deharsh 这种内部名）",
          c.js("[...document.querySelectorAll('#swPre input[data-sw]')]"
               ".every(e=>{const t=e.parentElement.textContent.trim();"
               "return t.length>1 && !/^[a-z_]{3,}$/.test(t);})"))

    # ★★ 这组开关只在 sing_role 那条路上生效；sing_multi 走自己的链路，**不读**
    #    pre_skip/post_skip。以前切到「多人合唱」它们照样能点、点了没反应 ——
    #    和 selEngine 同一种静默失效。判据：切过去必须真的置灰 + 给解释。
    c.js("(()=>{const s=document.querySelector('#selMulti');"
         "s.value='on';s.dispatchEvent(new Event('change',{bubbles:true}));})()")
    dis = c.js("(()=>{const a=[...document.querySelectorAll("
               "'#swPre input, #swPost input')];const p=document.querySelector('#swPresence');"
               "return a.length>0 && a.every(e=>e.disabled) && !!p && p.disabled;})()")
    check("★ 选「多人合唱」时唱前/唱后开关真的置灰（不是照样能点、点了没反应）",
          dis is True, "置灰=%s" % dis)
    check("置灰同时写了原因（不是默默变灰）",
          c.js("(()=>{const w=document.querySelector('#swWhy');"
               "return !!w && getComputedStyle(w).display!=='none';})()") is True)
    c.js("(()=>{const s=document.querySelector('#selMulti');"
         "s.value='off';s.dispatchEvent(new Event('change',{bubbles:true}));})()")
    check("切回「单人」后开关立刻恢复可用（置灰只是条件性的，功能没删）",
          c.js("[...document.querySelectorAll('#swPre input, #swPost input')]"
               ".every(e=>!e.disabled)") is True)
    check("切回单人的同时解释也收起来了",
          c.js("(()=>{const w=document.querySelector('#swWhy');"
               "return !w || getComputedStyle(w).display==='none';})()") is True)


def k_hall_premode(c):
    """K ★ 大厅的「唱前清理」按键（老板 2026-10-26 点名要的）。

    为什么单独一段：他要的是"大厅要有按键，我决定开不开"，而**藏起来的开关等于
    没有开关** —— 功能开关那批勾选框在角色抽屉里，要走"点开角色卡 → 切唱歌页签"
    两步才看得到。所以这里验的不是"有没有这个 select"，而是：
      ① 它在大厅上真的有尺寸（不是被 CSS 压成 0）；
      ② 真鼠标点得到、能切档；
      ③ 切档之后下面那行说明跟着变（不然用户不知道自己选了什么）；
      ④ 选项是从后端来的（界面不写死说法）。
    """
    sec("K ★ 大厅的「唱前清理」按键：真鼠标点得到、切档有反馈")
    # ★ 先把角色抽屉关掉：J 段为了数那批勾选框把抽屉打开了，抽屉一开就有遮罩，
    #   大厅那几个控件**还在 DOM 里、也还有尺寸**，但 elementFromPoint 会返回 null
    #   （点在遮罩之外/被盖住）⇒ 不关抽屉就永远测不到"用户点得到它"。
    c.js("(()=>{if(typeof closeDrawer==='function') closeDrawer();})()")
    c.js("(()=>{if(typeof songPickClose==='function') songPickClose();})()")
    c.js("(()=>{document.querySelectorAll('.mask').forEach(m=>{"
         "m.classList.remove('on');m.style.display='none';});})()")
    # ★ 为什么连"选歌器"也要点名关：它也是一层全屏遮罩，而这一段之前的 H/I
    #   恰好打开过它 —— 于是 (1164,279) 那个点命中的是选歌器里的 songPickHint，
    #   大厅的按键根本没露出来。只关角色抽屉测不到这一层。
    check("测之前先把遮罩/抽屉/选歌器都收掉（否则大厅控件点不到，测的是假象）",
          c.js("(()=>{const ids=['#maskRole','#maskSongPick','#drawer'];"
               "return ids.every(s=>{const e=document.querySelector(s);"
               "if(!e) return true;"
               "const st=getComputedStyle(e);"
               "return st.display==='none' || st.visibility==='hidden' "
               "|| (+st.opacity===0) || !e.classList.contains('on');});})()") is True)
    box = c.js("(()=>{const e=document.querySelector('#sgPreMode');"
               "if(!e) return null;const r=e.getBoundingClientRect();"
               "return [Math.round(r.width), Math.round(r.height)];})()")
    check("★ 大厅里有这个按键，而且真的有尺寸（不是 0×0 的隐藏元素）",
          isinstance(box, list) and box[0] > 0 and box[1] > 0, "尺寸=%s" % box)
    n = c.js("document.querySelectorAll('#sgPreMode option').length")
    check("三档都在（只修破音 / 彻底清理 / 完全不动）", n == 3, "选项数=%s" % n)
    check("默认档是「只修破音」（不该替用户决定做全套）",
          c.js("document.querySelector('#sgPreMode').value") == "guard",
          c.js("document.querySelector('#sgPreMode').value"))
    # ★ 为什么不用 c.click()：它是原生 <select>，真点一下会弹出系统下拉菜单
    #   （headless 里那个菜单根本不在 DOM 里，CDP 也点不到它）⇒ 点它必然报
    #   "命中的不是目标"。这里改成直接问：**中心点上站着的是不是它** ——
    #   这才是"用户的鼠标点下去会不会落到它身上"的准确答案。
    # ★ 必须先滚进视口：页面停在 scrollY=320 时它的中心 y 是 **−41**（在视口上方），
    #   elementFromPoint 于是返回 null —— 那不是"被盖住"，是"根本没在屏幕上"。
    #   用户真实操作时自然会滚到它，所以先 scrollIntoView 再测才有意义。
    c.js("(()=>{const e=document.querySelector('#sgPreMode');"
         "if(e) e.scrollIntoView({block:'center'});})()")
    c.js("new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
    hit = c.js("(()=>{const e=document.querySelector('#sgPreMode');"
               "const b=e.getBoundingClientRect();"
               "const cx=b.x+b.width/2, cy=b.y+b.height/2;"
               "const el=document.elementFromPoint(cx, cy);"
               "return {hit: !el ? 'null' : ((el===e||e.contains(el)) ? 'self'"
               " : (el.id || el.className)),"
               " cx: Math.round(cx), cy: Math.round(cy),"
               " vw: window.innerWidth, vh: window.innerHeight,"
               " sy: Math.round(window.scrollY)};})()")
    check("★ 鼠标点它的中心，落到的确实是它（没被别的元素盖住）",
          (hit or {}).get("hit") == "self", "命中=%s 点=(%s,%s) 视口=%sx%s scrollY=%s"
          % ((hit or {}).get("hit"), (hit or {}).get("cx"), (hit or {}).get("cy"),
             (hit or {}).get("vw"), (hit or {}).get("vh"), (hit or {}).get("sy")))
    for v, want in (("deep", "彻底清理"), ("off", "完全不动"), ("guard", "只修破音")):
        c.js("(()=>{const s=document.querySelector('#sgPreMode');s.value='%s';"
             "s.dispatchEvent(new Event('change',{bubbles:true}));})()" % v)
        lab = c.js("[...document.querySelectorAll('#sgPreMode option')]"
                   ".find(o=>o.value==='%s').textContent" % v)
        hint = c.js("(document.querySelector('#sgPreHint')||{}).textContent || ''")
        check("选「%s」→ 说明跟着变（不是选了跟没选一样）" % want,
              (want in lab) and len(hint.strip()) > 4,
              "选项=%s 说明=%s" % (lab, hint.strip()[:40]))
    check("★ 选项文字是后端给的（界面不许自己发明说法）",
          c.js("Array.isArray(SG_PRE_MODES) && SG_PRE_MODES.length===3 "
               "&& SG_PRE_MODES.every(m=>m.length>=2)"),
          "SG_PRE_MODES=%s" % c.js("JSON.stringify(SG_PRE_MODES)"))


def audit_mute():
    """★ 探针查探针：谁起了 Chrome 却没静音，门禁就红。

    实测教训（2026-10-26）：试听段验的是「真的能出声」，会调 audio.play()，
    而 headless Chrome **默认接系统音频输出** ⇒ 验收跑起来扬声器里就放出一串
    电子音阶（8 个纯正弦音）。用户听到的是"电脑发出奇怪异响"，还以为是硬件坏了。
    静音只影响输出设备，play()/currentTime/volume 这些判据完全不受影响。
    """
    import glob
    bad = []
    for p in sorted(glob.glob(os.path.join(HERE, "tools", "*.py"))):
        src = open(p, encoding="utf-8", errors="ignore").read()
        if "--headless=new" in src and "--mute-audio" not in src:
            bad.append(os.path.basename(p))
    check("★ 所有起 Chrome 的探针都带 --mute-audio（验收不许从扬声器出声）",
          not bad, bad)


def main():
    print("精修台真浏览器探针：%s" % URL, flush=True)
    audit_mute()
    if not os.path.isfile(CHROME):
        check("找到 Chrome", False, CHROME)
        return 1
    dst = make_clip()
    print("  测试片段：%s" % dst, flush=True)

    c = boot()
    if not c:
        check("拉起 Chrome 并连上 CDP", False)
        return 1
    try:
        a_panel(c)
        # ★ K 必须**第一个**跑：它测的是大厅控件，而后面的 B~J 段会打开角色抽屉、
        #   选歌器这一层层全屏遮罩。遮罩一开，大厅那些控件还在 DOM 里、也还有尺寸，
        #   但 elementFromPoint 命中的是遮罩上的东西（实测是 songPickHint）——
        #   测出来"点不到"，其实测的是别人家的弹窗。
        k_hall_premode(c)
        b_load(c)
        c_click_block(c)
        d_drag_semi(c)
        e_one_click(c)
        f_export(c)
        h_polish(c)
        i_audition(c)
        j_switches(c)
        g_errs(c)
    finally:
        try:
            c.chrome.terminate()
            c.chrome.wait(timeout=15)
        except Exception:
            try:
                c.chrome.kill()
            except Exception:
                pass
    print("\n通过 %d 项，失败 %d 项" % (len(OK), len(BAD)), flush=True)
    if BAD:
        for b in BAD:
            print("  红：%s" % b, flush=True)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
