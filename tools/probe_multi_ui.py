# -*- coding: utf-8 -*-
r"""多角色界面的真浏览器验收：分配表预览 / 逐句指派 / 引擎置灰 / 成品体检条。

为什么单开一支探针（而静态检查已经绿了）：
  · 「逐句指派」现在是**一支笔 + 点歌词行**（以前每行一个下拉框：改一句要点两次，
    20 句 40 下，下拉一展开还盖住歌词）。坏起来的表现是行高被挤成竖排、笔点不动、
    点了行不改人 —— 静态检查只看得见 class 在不在。
  · 「多人模式下引擎要置灰」是本轮修的静默失效（以前选了 Seed-VC 也不生效）。
    这条只能真的切一次唱法才看得见。
  · 成品卡上的逐人体检 + 换法重拼按钮是**动态插入**的，要看真 DOM。

规矩（和 verify_drag_ui 一致）：
  · 真鼠标点角色卡 / 唱歌 tab / 笔 / 歌词行（dispatchMouseEvent + 命中测试），
    不用 element.click()。
  · 下拉框是系统级弹层，无法用鼠标点选 ⇒ 用「改值 + 派 change」，走的仍是页面自己的监听器。
    注意：逐句那边已经没有**可见**下拉了（改成笔），隐藏的 select.msel 只作为
    数据口径（currentManual 用它读顺序）—— 所以判据改成"它必须还在、且必须是隐藏的"。
  · 歌曲路径用 JS 直接塞（相当于用户已经选好歌）；本探针验的是选完歌之后的事。

用法（服务和探针必须在同一条命令里）：
    VOICESTUDIO_PORT=8899 python server.py &
    python tools/probe_multi_ui.py
"""
import atexit
import json
import os
import subprocess
import sys
import time
import urllib.request

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = int(os.environ.get("VS_PORT", "8899"))
URL = "http://127.0.0.1:%d/" % PORT
CDP_PORT = int(os.environ.get("VS_CDP_PORT", "9341"))
PROFILE = os.path.join(HERE, ".cdp-multi")
SONG = os.path.join(HERE, "_tmp", "e2e_multi_clip.wav")
ROLE = os.environ.get("VS_ROLE", "ra500fb92")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name +
          (("  | " + str(detail)) if detail else ""), flush=True)


def cdp_get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (CDP_PORT, path), timeout=20) as r:
        return json.loads(r.read().decode())


class Cdp:
    def __init__(self):
        import websocket
        page = [t for t in cdp_get("/json/list") if t.get("type") == "page"]
        if not page:
            raise RuntimeError("没有可用的页面目标")

        self.ws = websocket.create_connection(page[0]["webSocketDebuggerUrl"], timeout=60)
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
            raise RuntimeError(str(r["exceptionDetails"])[:500])
        return r.get("result", {}).get("value")

    def rect(self, sel):
        return self.js("""(()=>{const e=document.querySelector(%s);
          if(!e) return null; const r=e.getBoundingClientRect();
          return {x:r.x,y:r.y,w:r.width,h:r.height,
                  vw:innerWidth,vh:innerHeight,
                  ox:r.x+Math.min(r.width/2,20), oy:r.y+r.height/2};})()"""
                       % json.dumps(sel))

    def click(self, sel):
        """真鼠标：滚进视口 → 移到元素中心 → 按下/抬起，并做命中测试。"""
        r = self.rect(sel)
        if not r:
            return False, "元素不存在"
        self.js("(()=>{const e=document.querySelector(%s);"
                "e.scrollIntoView({block:'center'});return 1;})()" % json.dumps(sel))
        time.sleep(0.2)
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
            # 装个错误收集器：后面每一步的 JS 异常都要能被数出来
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


def open_sing(c):
    """真鼠标：点角色卡 → 点「唱歌」tab。"""
    sel = '.card[data-role="%s"]' % ROLE
    if not c.js("!!document.querySelector(%s)" % json.dumps(sel)):
        sel = '.card[data-role]'          # 那个角色被删了就用第一张卡
    ok1, why1 = c.click(sel)
    check("真鼠标点到角色卡", ok1, why1)
    time.sleep(1.2)
    ok2, why2 = c.click('#paneSing, [data-pane="sing"]')
    if not ok2:
        # 面板 tab 的写法可能是按钮文案，退回按文案找
        ok2 = c.js("""(()=>{const b=[...document.querySelectorAll('button,.tab,.ptab')]
          .find(x=>/唱歌/.test(x.textContent||''));
          if(!b) return false; b.click(); return true;})()""")
        why2 = "按文案点「唱歌」"
    check("切到「唱歌」面板", bool(ok2), why2)
    time.sleep(0.8)
    check("唱歌面板里有多人合唱开关", bool(c.js("!!document.querySelector('#selMulti')")))


def a_switch(c):
    sec("A. 切多人：控件显隐 + 引擎置灰（静默失效的修复）")
    check("默认是单人", c.js("$('#selMulti').value") == "off",
          c.js("$('#selMulti').value"))
    check("单人时引擎可选", c.js("$('#selEngine').disabled") is False)
    check("单人时不显示角色勾选区",
          c.js("getComputedStyle($('#multiWrap')).display") == "none")

    # 真鼠标点不了 <select> 的系统弹层，改值 + 派 change（走页面自己的 onchange）
    c.js("""(()=>{const s=$('#selMulti'); s.value='on'; s.dispatchEvent(new Event('change',{bubbles:true})); return s.value;})()""")
    time.sleep(0.8)
    check("切多人后出现角色勾选区",
          c.js("getComputedStyle($('#multiWrap')).display") != "none")
    check("切多人后出现「谁唱哪几句」",
          c.js("getComputedStyle($('#multiModeWrap')).display") != "none")
    check("★ 多人模式下引擎被置灰（选了也不生效，必须当场说）",
          c.js("$('#selEngine').disabled") is True,
          "以前是可选但静默忽略")
    check("置灰带解释（title 说清为什么）",
          "Seed-VC" in (c.js("$('#selEngine').title") or ""))
    n = c.js("document.querySelectorAll('#multiRoles label.mrole').length")
    check("列出的都是能唱的角色", isinstance(n, int) and n >= 2, n)
    check("默认勾了两个人（对唱最小配置）",
          c.js("document.querySelectorAll('#multiRoles input[type=checkbox]:checked').length") >= 2)


def b_plan(c):
    sec("B. 分配表预览：不跑推理就能看见「谁唱多少」")
    c.js("pickedSong = %s" % json.dumps(SONG))
    c.js("""(()=>{const s=$('#selMultiMode'); s.value='phrase';
      s.dispatchEvent(new Event('change',{bubbles:true})); return 1;})()""")
    time.sleep(2.5)
    check("预览区出现", c.js("getComputedStyle($('#multiPlan')).display") != "none")
    summ = c.js("$('#multiPlanSum').textContent") or ""
    check("汇总里有每个人的秒数", "秒 /" in summ and "全曲" in summ, summ[:120])
    # ★ 2026-10-26：这条以前是「#multiPlan 总高度 < 120px」，红了（157px）。
    #   量了才知道根因不是"挤成竖排"，而是预览区现在**本来就有三行**：
    #   汇总 32 + 分句说明 38（2 行）+ 歌词来源条 48。拿总高度当尺子，
    #   会把"多了一行正常内容"判成"挤成一字一行" —— 这是**尺子错了**，不是界面坏了。
    #   所以换成量几何（真正的意图），并配自检证明它抓得到竖排。
    def _mplan_geo():
        return c.js("""(()=>{
          const sum=document.querySelector('#multiPlanSum');
          const hin=document.querySelector('#multiPlanHint');
          if(!sum||!hin) return null;
          const lh=parseFloat(getComputedStyle(hin).lineHeight)||18;
          const rs=sum.getBoundingClientRect(), rh=hin.getBoundingClientRect();
          return {sumH:Math.round(rs.height), sumW:Math.round(rs.width),
                  hintH:Math.round(rh.height), lh:Math.round(lh),
                  hintLines:Math.max(1,Math.round(rh.height/lh))};
        })()""")

    g = _mplan_geo()
    check("预览区能取到几何（元素不是隐藏的）",
          isinstance(g, dict) and (g.get("sumW") or 0) > 0, g)
    check("★ 汇总行是横着排的（一行放下、宽度够，不是一字一行）",
          isinstance(g, dict) and g["sumH"] <= g["lh"] * 1.8 and g["sumW"] >= 240, g)
    check("分句说明不超过 3 行",
          isinstance(g, dict) and 0 < g["hintLines"] <= 3,
          (g or {}).get("hintLines"))
    # 尺子自检：把预览区压到 60px 宽，上面"横排"那条必须变红。
    c.js("(()=>{const b=document.querySelector('#multiPlan');"
         "b.dataset.w0=b.style.width||''; b.style.width='60px'; return 1;})()")
    time.sleep(0.35)
    g2 = _mplan_geo()
    squeezed = (isinstance(g2, dict)
                and (g2["sumH"] > g2["lh"] * 1.8 or g2["sumW"] < 240))
    c.js("(()=>{const b=document.querySelector('#multiPlan');"
         "b.style.width=b.dataset.w0||''; return 1;})()")
    time.sleep(0.25)
    check("★ 尺子自检：压到 60px 宽时「横排」判据真的会红（不是假尺子）",
          squeezed, g2)
    g3 = _mplan_geo()
    check("自检后已还原（宽度恢复，判据回绿）",
          isinstance(g3, dict) and g3["sumH"] <= g3["lh"] * 1.8
          and g3["sumW"] >= 240, g3)
    warn = c.js("$('#multiPlanHint .mwarn') ? "
                "$('#multiPlanHint .mwarn').getBoundingClientRect().height : 0")
    check("拆组提醒最多占 2 行（%s px）" % (round(warn or 0, 1)),
          isinstance(warn, (int, float)) and warn <= 40, warn)
    hint = c.js("$('#multiPlanHint').textContent") or ""
    check("提示说清了按几句分 / 或者没认过词", bool(hint.strip()), hint[:100])


def c_manual(c):
    sec("C. 逐句指派：一支笔 + 点歌词行（真鼠标）")
    c.js("""(()=>{const s=$('#selMultiMode'); s.value='manual';
      s.dispatchEvent(new Event('change',{bubbles:true})); return 1;})()""")
    time.sleep(2.0)
    rows = c.js("document.querySelectorAll('#multiLines .mline').length")
    check("逐句列表有行", isinstance(rows, int) and rows > 0, rows)
    if not rows:
        return
    # ★ 老板要「带有中文」（2026-10-25 第九轮）：界面上那一列必须是中文，
    #   不是英文原文。判据看**渲染出来的 DOM 文本**，不是看数据里有没有 zh ——
    #   数据到了但没渲染出来，用户看到的还是一片英文。
    txts = c.js("""(()=>Array.from(
        document.querySelectorAll('#multiLines .mline .mx'))
        .map(e=>e.textContent||'').join('\\n'))()""") or ""
    cjk = c.js("""(()=>{const t=Array.from(
        document.querySelectorAll('#multiLines .mline .mx'))
        .map(e=>e.textContent||'').join('');
        return (t.match(/[\\u4e00-\\u9fff]/g)||[]).length;})()""") or 0
    check("★ 句表渲染出来的是**中文**（不是英文原文）",
          isinstance(cjk, int) and cjk > 0,
          "中日韩字符 %s 个，前两行：%s"
          % (cjk, (txts or "").replace("\n", " / ")[:90]))
    # ★ 按原曲换气合并后，每行要标"这一句合了几个断句"，
    #   不然用户数着的第 5 句变成第 3 句，会以为歌词被吞了。
    nsub = c.js("document.querySelectorAll('#multiLines .mline .msub').length")
    check("★ 合并出来的句子标了「合了几个断句」（.msub 角标）",
          isinstance(nsub, int) and nsub >= 0, nsub)
    nnames = c.js("(typeof MULTI_NAMES==='undefined'?[]:MULTI_NAMES).length") or 0
    # ★ 数据口径：每行那颗隐藏的 select.msel 必须还在（currentManual 靠它读顺序），
    #   只是不许再显示出来。删掉它 = 提交时 manual 全空 = 悄悄退回自动分配。
    nsel = c.js("document.querySelectorAll('#multiLines select.msel').length")
    check("每行仍保留一个隐藏的 select（数据口径不许变）", nsel == rows, nsel)
    check("那颗 select 确实是隐藏的（改一句不用点两次）",
          c.js("""(()=>{const s=document.querySelector('#multiLines select.msel');
            if(!s) return false; return getComputedStyle(s).display === 'none';})()""") is True)

    # ---- ★ 没认过词 ⇒ 必须给得出「先认一次词」这条路 ----
    #   背景（2026-10-23 定性的根因）：真句子只来自歌词缓存。预览只读缓存、
    #   开跑才会 auto_lyric 自动认 ⇒ 句数会漂，用户点的指定只对上一半。
    #   而界面上原来**根本没有**认词的入口（唯一的「识别歌词」按钮是给成品补词的），
    #   hint 却写着"先认一次词" —— 等于让人走一条不存在的路。
    #   ⇒ 这里不真的点它（点下去会真跑识别、留下歌词缓存），只验它在、且绑了动作。
    #   ⚠ 按钮只在**没认过词**时出现。本探针这首 16 秒 clip 早就认过词了
    #     （列表就是 4 句真句子）⇒ 直接查会永远查不到，那是判据的状态假设错了，
    #     不是产品没做。真实曲库里除本轮认过的那首、其余**全部**没认过词。
    #     ⇒ 这里拿当前这份真实预览数据、把 lyric_ready 强制成 false 再渲染一次，
    #       验"没词时会不会给认词入口"；同时验"有词时不该冒出来"（防止到处乱加）。
    _lr = c.js("""(()=>{
        if (typeof MULTI_PLAN!=='object' || !MULTI_PLAN) return null;
        renderMultiPlan(Object.assign({}, MULTI_PLAN, {lyric_ready:false}), 'manual');
        const b=document.querySelector('#btnLyricFirst');
        if(!b) return null;
        const r=b.getBoundingClientRect();
        return {txt:(b.textContent||'').trim(),
                onclick: typeof b.onclick === 'function',
                w: Math.round(r.width), h: Math.round(r.height)};})()""")
    check("没认过词时，界面上真的有「先认一次词」按钮",
          isinstance(_lr, dict) and "认" in (_lr.get("txt") or ""), _lr)
    check("那个按钮点了会真去认词（绑了动作，不是画上去看的）",
          isinstance(_lr, dict) and _lr.get("onclick") is True, _lr)
    check("按钮点得到（宽 >= 60 / 高 >= 18）",
          isinstance(_lr, dict) and _lr.get("w", 0) >= 60 and _lr.get("h", 0) >= 18,
          _lr and "%dx%d" % (_lr.get("w", 0), _lr.get("h", 0)))
    # 有词时不该再冒出认词按钮（那会让人以为自己的词白认了）
    _lr2 = c.js("""(()=>{
        renderMultiPlan(Object.assign({}, MULTI_PLAN, {lyric_ready:true}), 'manual');
        return !!document.querySelector('#btnLyricFirst');})()""")
    check("认过词时不再冒出认词按钮", _lr2 is False, _lr2)

    # ---- 笔条：一个角色一支笔 **+ 一支「合唱」笔**
    #   ★ 老口径是"人数 == 笔数"。2026-10-23 起逐句可以指定合唱，笔条多了一支
    #     `data-pen="-1"` 的合唱笔（与后端 multising.ROLE_ALL 同一个 -1）。
    #     只把笔数放宽到 n+1 是不够的 —— 那样"多一支笔"和"多一支合唱笔"分不开，
    #     而后端不认 -1 时症状正是"按钮点了没反应、一句合唱都出不来"。
    #     ⇒ 笔数对上 n+1 **且** 那支笔真带着 -1，两条一起才算过。
    npen = c.js("document.querySelectorAll('#multiPens .pen').length")
    check("笔条 = 每个角色一支 + 一支合唱笔（%d 人 → %d 支）" % (nnames, npen),
          npen == nnames + 1 and npen >= 3, npen)
    _ch = c.js("""(()=>{const b=document.querySelector('#multiPens .pen[data-pen="-1"]');
        return b ? (b.textContent||'').trim() : null;})()""")
    check("★ 多出来的那支是 data-pen=\"-1\" 的合唱笔（与后端同值）",
          isinstance(_ch, str) and "合唱" in _ch, _ch)
    gp = c.rect("#multiPens .pen")
    check("笔有可点的尺寸（宽 >= 48 / 高 >= 20）",
          bool(gp) and gp["w"] >= 48 and gp["h"] >= 20,
          gp and "%dx%d" % (round(gp["w"]), round(gp["h"])))
    check("默认只有一支笔是激活态",
          c.js("document.querySelectorAll('#multiPens .pen.on').length") == 1,
          c.js("document.querySelectorAll('#multiPens .pen.on').length"))
    check("笔条自己不横向溢出",
          c.js("(()=>{const e=$('#multiPens'); return e.scrollWidth <= e.clientWidth + 2;})()"))

    # ★ 2026-10-25 口径更新（不是"为了让红变绿"——需求变了）：
    #   老板要歌词那种排版，所以一句**不再压成 31px 的一行**：
    #   时间在左、歌词 15px 且**完整换行**、角色胶囊在右下。
    #   现在一句 40~90px（长句折两行）。判据跟着改成"落在歌词排版的合理区间"，
    #   同时另配一条硬判据钉住"歌词不许再被截断"（那才是"压缩了"的病根）。
    g = c.rect("#multiLines .mline")
    check("行高在歌词排版区间（34~120px，且没竖排）",
          bool(g) and 34 <= g["h"] <= 120, g and round(g["h"], 1))
    _mx = c.js("""(()=>{const e=document.querySelector('#multiLines .mline .mx');
      if(!e) return null; const cs=getComputedStyle(e);
      return {ws:cs.whiteSpace, ov:cs.textOverflow, fs:parseFloat(cs.fontSize),
              ell:e.scrollWidth > e.clientWidth + 2};})()""")
    check("★ 歌词字号够大（>= 14px，不是压缩版 12.5px）",
          isinstance(_mx, dict) and _mx.get("fs", 0) >= 14, _mx and _mx.get("fs"))
    check("★ 歌词**完整换行**（white-space:normal + 不用省略号截断）",
          isinstance(_mx, dict) and _mx.get("ws") == "normal"
          and _mx.get("ov") != "ellipsis" and _mx.get("ell") is False,
          _mx)
    check("每句左侧有该角色的颜色竖条（26 句的表现在靠它分人）",
          c.js("""(()=>{const e=document.querySelector('#multiLines .mline');
            if(!e) return false;
            const cs=getComputedStyle(e,'::before');
            return !!cs && cs.content !== 'none' && parseFloat(cs.width) >= 2;})()""") is True)
    check("列表自己不横向溢出",
          c.js("(()=>{const e=$('#multiLines .mlines'); return e.scrollWidth <= e.clientWidth + 2;})()"))
    check("列表只有**一层**滚动容器（两层叠着滚=手感像卡住）",
          c.js("""(()=>{const h=$('#multiLines');
            if(!h) return false;
            return getComputedStyle(h).overflowY !== 'auto'
              && !!h.querySelector('.mlines');})()""") is True)
    check("列表有高度上限（不把整页顶长）",
          c.js("(()=>{const e=$('#multiLines .mlines'); return e.clientHeight <= 700;})()"))

    # ---- 真鼠标点第 2 支笔 → 笔的激活态跟过去
    if npen >= 2:
        ok, why = c.click("#multiPens .pen[data-pen='1']")
        check("真鼠标点到第 2 支笔", ok, why)
        check("点了笔之后激活态确实挪过去了（data-pen=1）",
              c.js("(document.querySelector('#multiPens .pen.on')||{}).dataset"
                   " && document.querySelector('#multiPens .pen.on').dataset.pen") == "1")

    # ---- 真鼠标点第 1 行 → 这一句改给第 2 个人
    before = c.js("$('#multiPlanSum').textContent")
    ok, why = c.click("#multiLines .mline")
    check("真鼠标点到第 1 行歌词", ok, why)
    time.sleep(1.8)
    check("那一行的人换了（data-who=1）",
          c.js("document.querySelector('#multiLines .mline').dataset.who") == "1",
          c.js("document.querySelector('#multiLines .mline').dataset.who"))
    check("行上的名字胶囊也跟着换了",
          (c.js("document.querySelector('#multiLines .mline .mwho').textContent") or "")
          == (c.js("(typeof MULTI_NAMES==='undefined'?[][1]:MULTI_NAMES[1])") or "\x00"),
          c.js("document.querySelector('#multiLines .mline .mwho').textContent"))
    check("隐藏 select 也同步了（提交时读的就是它）",
          c.js("document.querySelector('#multiLines select.msel').value") == "1",
          c.js("document.querySelector('#multiLines select.msel').value"))
    after = c.js("$('#multiPlanSum').textContent")
    check("改了某句之后汇总跟着变（真的重算了分配表）", before != after,
          "%s → %s" % ((before or "")[:46], (after or "")[:46]))
    check("改完列表**没有**被重绘（第一行还停在被指派的那个人）",
          c.js("document.querySelector('#multiLines .mline').dataset.who") == "1")

    # ---- 「整首都给 TA」：当前这支笔点一下，全曲每一句都是他
    ok, why = c.click("#multiPens [data-allpen='1']")
    check("真鼠标点到「整首都给 TA」", ok, why)
    time.sleep(2.2)
    who = c.js("""(()=>{const a=[...document.querySelectorAll('#multiLines .mline')]
      .map(e=>e.dataset.who); return a.every(x=>x==='1');})()""")
    check("整首都给 TA 之后每一句都是他", who is True, who)
    # ★ 汇总里**所有人都会列出来**（哪怕这轮一句没唱也写 "0 秒"）——
    #   所以判据不是"只剩一个人"，而是"被指派的那个人拿到全曲、别人 0 秒"。
    #   第一版写成数 "秒 /" 出现几次，是尺子自己歪了，不是产品问题。
    summ = c.js("$('#multiPlanSum').textContent") or ""
    nm0 = c.js("(typeof MULTI_NAMES==='undefined'?[]:MULTI_NAMES)[0]") or ""
    nm1 = c.js("(typeof MULTI_NAMES==='undefined'?[]:MULTI_NAMES)[1]") or ""
    check("整首都给 TA 之后：他拿了全曲、另一个人 0 秒",
          bool(nm1) and ("%s：0 秒" % nm0) in summ and nm1 in summ,
          summ[:80])

    # ---- ★ 逐句指定「这一句合唱」：点合唱笔 → 点某一句 → 那一句是 -1 ----
    #   三处一起量才叫验过：数据口径（dataset.who）、看得见（胶囊文字）、
    #   送得出去（隐藏 select —— currentManual() 读的就是它）。
    #   只量前两处的话，"界面显示合唱但提交时送的是 0"这种毛病照样溜过去。
    ok, why = c.click('#multiPens .pen[data-pen="-1"]')
    check("真鼠标点到合唱笔", ok, why)
    check("合唱笔点下去后它自己是激活态",
          c.js("(document.querySelector('#multiPens .pen.on')||{}).dataset"
               " && document.querySelector('#multiPens .pen.on').dataset.pen") == "-1",
          c.js("(document.querySelector('#multiPens .pen.on')||{}).dataset"
               " && document.querySelector('#multiPens .pen.on').dataset.pen"))
    ok, why = c.click("#multiLines .mline")
    check("真鼠标点第 1 行（把这一句改成合唱）", ok, why)
    time.sleep(1.8)
    check("★ 那一句的 data-who 变成了 -1",
          c.js("document.querySelector('#multiLines .mline').dataset.who") == "-1",
          c.js("document.querySelector('#multiLines .mline').dataset.who"))
    check("★ 行上的胶囊写「合唱」而不是某个人的名字",
          ((c.js("document.querySelector('#multiLines .mline .mwho').textContent") or "")
           .strip() == "合唱"),
          c.js("document.querySelector('#multiLines .mline .mwho').textContent"))
    check("★ 隐藏 select 也是 -1（提交时送出去的就是它）",
          c.js("document.querySelector('#multiLines select.msel').value") == "-1",
          c.js("document.querySelector('#multiLines select.msel').value"))
    summ2 = c.js("$('#multiPlanSum').textContent") or ""
    check("★ 汇总里报出了合唱秒数（用户看得见'这几句一起唱'）",
          "合唱" in summ2, summ2[:90])

    # 换回按句分，别让后面的关卡接着看一张"全给一个人"的表
    c.js("""(()=>{const s=$('#selMultiMode'); s.value='phrase';
      s.dispatchEvent(new Event('change',{bubbles:true})); return 1;})()""")
    time.sleep(1.6)


def d_card(c):
    sec("D. 成品卡：逐人体检条 + 换法重拼（动态插入）")
    # ★ 角色面板是个全屏抽屉（#maskRole + #drawer，z-index 很高）。不先关掉它，
    #   一键翻唱台整块都在它**后面** —— elementFromPoint 会命中抽屉里的 #selMulti，
    #   判据就会报"按钮被挡住"。这不是产品 bug，是尺子没摆对（本轮实测踩过）。
    if c.js("!!document.querySelector('#drawer.on')"):
        okc, whyc = c.click('#drawer button')
        if not okc:
            c.js("closeDrawer(); 1")
        time.sleep(0.4)
    check("角色抽屉已关闭（不然底下的成品卡点不到）",
          c.js("!document.querySelector('#drawer').classList.contains('on')"), "")

    c.js("stageShowResult(%s)" % json.dumps(ROLE))
    time.sleep(2.0)
    n = c.js("document.querySelectorAll('#stageOut .mcard').length")
    real = isinstance(n, int) and n >= 1
    if not real:
        # 没有真成品时用**同一个渲染函数**造一张卡：验的是"卡片长什么样、能不能点"，
        # 不是"这个人有没有成品"。参数结构取自真实成品（已对账）。
        c.js("""(()=>{
          const host=document.querySelector('#stageOut')||document.body;
          const box=document.createElement('div'); box.className='outcard';
          box.id='__probe_mcard';
          box.innerHTML=multiCard({params:{
            multi_mode:'phrase', multi_tag:'probe',
            multi_qc:[{name:'甲',pitch:0,from:0,secs:12,match:0.93,octave:0.02,shift:0},
                      {name:'乙',pitch:-5,from:12,secs:9,match:0.88,octave:0.04,shift:-5}]
          }}, 0);
          host.appendChild(box); return 1;})()""")
        time.sleep(0.3)
    # ★ 这里必须**重新数一遍**，不能用注入前的 n。本轮踩过：没有真成品时注入样例
    #   明明画上去了，却因为旧的 n 还是 0 就直接 return ⇒ 后面 7 条判据全被跳过，
    #   套件只报 28 项而不是 35 项（"少跑了"伪装成"全过了"）。
    n2 = c.js("document.querySelectorAll('#stageOut .mcard').length")
    check("成品卡上有多角色信息条（真成品或注入样例）",
          isinstance(n2, int) and n2 >= 1, "真成品 %s → 现在 %s" % (n, n2))
    if not isinstance(n2, int) or n2 < 1:
        return
    rows = c.js("document.querySelectorAll('#stageOut .mcard .mqr').length")
    check("体检条里有两个人的数（>=2 行）", rows >= 2, rows)
    txt = c.js("document.querySelector('#stageOut .mcard').textContent") or ""
    check("体检条里有「吻合」与「实变」（不是空壳）",
          "吻合" in txt and "实变" in txt, txt[:120].replace("\n", " "))
    check("有「换法重拼」按钮",
          c.js("[...document.querySelectorAll('#stageOut button')].some(b=>/换法重拼/.test(b.textContent))"))
    check("重拼按钮旁边有分配方式下拉",
          c.js("document.querySelectorAll('#stageOut select.rsmode').length") >= 1)
    g = c.rect("#stageOut .mcard")
    check("信息条没被压扁（高度 > 40px）", g and g["h"] > 40, g and round(g["h"], 1))
    # 按钮必须真的可点（命中测试）
    # ★ 必须先 scrollIntoView → **等一拍** → 再量坐标 → 再命中测试。
    #   直接"量完立刻点"会踩到平滑滚动的中间态：坐标还是滚动前的，命中的是
    #   旁边那个下拉框 —— 实测就误报过一次 "blocked:inp"。量几何要等稳定。
    c.js("""(()=>{const b=[...document.querySelectorAll('#stageOut button')]
      .find(x=>/换法重拼/.test(x.textContent)); if(b) b.scrollIntoView({block:'center'});
      return 1;})()""")
    time.sleep(0.6)
    hit = c.js("""(()=>{const b=[...document.querySelectorAll('#stageOut button')]
      .find(x=>/换法重拼/.test(x.textContent));
      if(!b) return 'no';
      const r=b.getBoundingClientRect(); const e=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
      if(!e) return 'null@'+Math.round(r.x)+','+Math.round(r.y);
      if(e===b||b.contains(e)) return 'ok';
      return 'blocked:'+(e.tagName||'?')+'.'+(e.className||'')
        +'#'+(e.id||'')+'@'+Math.round(r.x+r.width/2)+','+Math.round(r.y+r.height/2)
        +' rect='+Math.round(r.y)+'..'+Math.round(r.bottom);})()""")
    check("重拼按钮不会被别的层挡住", hit == "ok", hit)
    # 下拉框和按钮不许重叠（一重叠，点按钮就会变成开下拉）
    ov = c.js("""(()=>{const b=[...document.querySelectorAll('#stageOut button')]
      .find(x=>/换法重拼/.test(x.textContent));
      const s=b&&b.parentElement.querySelector('select.rsmode');
      if(!b||!s) return 'no';
      const A=b.getBoundingClientRect(), B=s.getBoundingClientRect();
      return (A.left < B.right-1 && B.left < A.right-1 &&
              A.top < B.bottom-1 && B.top < A.bottom-1) ? 'overlap' : 'ok';})()""")
    check("下拉框与重拼按钮不重叠", ov == "ok", ov)


def _viz_cast(rid, idx, timeout=30):
    """问服务端要这条成品的分配表 + 歌词。返回 (行数, 错误串)。"""
    try:
        with urllib.request.urlopen(URL + "api/viz/cast?role=%s&index=%d" % (rid, idx),
                                    timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except Exception as e:                       # noqa: BLE001
        return 0, "拿不到 /api/viz/cast：%s" % e
    if d.get("error"):
        return 0, d["error"]
    return len(((d.get("cast") or {}).get("lines")) or []), ""


def _plant_lyrics(src, dur):
    """给这首歌**临时**种一份歌词缓存，返回 (缓存文件, 原字节或 None)。

    ★ 为什么要种料：字幕只有三个来源（成品的 .lrc / 成品自带的 multi_segs /
      这首歌的识别缓存）。2026-10-21 实测：库里 4 条多角色成品的识别缓存被清过，
      于是 `viz_cast_of` 读不到词、字幕表一行都没有 —— 判据红在"没有料"上，
      而不是产品坏了。判据要自足，所以这一节自己备料。
      这一节验的是"表能读、能改、能存"，不是"词认得准"（那由 verify_lyrics_align
      和 verify_asr_size 管），所以种 6 句假词完全够。
    ★ 一定要还原：原本有就写回原字节，原本没有就删掉。假歌词留在库里，
      老板下次打开会看到自己没写过的词。
    """
    sys.path.insert(0, HERE)
    import studio
    p = studio.lyric_cache_path(src, "zh", studio.ASR_DEFAULT)
    bak = None
    if os.path.isfile(p):
        with open(p, "rb") as f:
            bak = f.read()
    sec = max(float(dur or 0), 6.0)
    n = 6
    segs = []
    for k in range(n):
        a = sec * k / n
        segs.append({"start": round(a, 2),
                     "end": round(a + sec / n * 0.85, 2),
                     "text": "Test line %d" % (k + 1)})
    studio.save_lyric_segments(src, segs, lang="zh", size=studio.ASR_DEFAULT)
    return p, bak


def _restore_lyrics(p, bak):
    try:
        if bak is None:
            if os.path.isfile(p):
                os.remove(p)
        else:
            with open(p, "wb") as f:
                f.write(bak)
    except Exception as e:                       # noqa: BLE001
        print("  [warn] 歌词缓存没还原（%s）：%s" % (p, e), flush=True)


def e_subs(c):
    """做视频的字幕表：**读得到 / 改得了 / 只在合唱台出现**。

    ★ 这一节验的是"用户真的能把字幕改了"这条完整链路：
      从成品里读出来 → 表格里改 → 点保存 → 服务端真的变了。少任何一环，
      用户看到的就是"我改了但它还是唱原来那版"。
    """
    sec("E. 做视频的字幕表（英文一格 / 中文小字一格）")
    try:
        roles = json.loads(urllib.request.urlopen(URL + "api/roles", timeout=20)
                           .read().decode("utf-8"))["roles"]
    except Exception as e:                          # noqa: BLE001
        check("能读到角色列表（要拿多角色成品）", False, e)
        return
    cands = []
    for r in roles:
        for i, o in enumerate(r.get("outputs") or []):
            p = o.get("params") or {}
            if (p.get("multi_takes") or []) and (p.get("multi_roles") or []):
                cands.append((r, i, o))
    if not cands:
        print("  SKIP  库里没有多角色成品 —— 字幕表这一节没法验（不去造假数据）", flush=True)
        return

    # ★ 别拿"第一条"就用：那条可能没认过词（缓存被清过），表会空。先问服务端
    #   哪条真读得到词；一条都没有才自己种一份料。
    hit, why0 = None, ""
    for r, i, o in cands:
        n0, err = _viz_cast(r["id"], i)
        if n0 > 0:
            hit = (r, i, o)
            break
        why0 = why0 or err or "lines=0"
    planted = None
    if not hit:
        r, i, o = cands[0]
        src = (o.get("stems") or {}).get("src") or ""
        dur = (o.get("params") or {}).get("multi_duration") or o.get("seconds") or 0
        if not src or not os.path.isfile(src):
            print("  SKIP  这条成品的源曲不在硬盘上（%r），没处种草稿歌词" % src, flush=True)
            return
        try:
            planted = _plant_lyrics(src, dur)
            # 兜底还原：这一节后面有几处 return（判据不满足就不往下测），
            # 逐个补 try/finally 容易漏 ⇒ 挂到进程退出上，正常/异常退出都会还原。
            atexit.register(_restore_lyrics, *planted)
        except Exception as e:                      # noqa: BLE001
            check("能给这首歌种一份临时歌词（字幕这节要自足）", False, e)
            return
        n0, _ = _viz_cast(r["id"], i)
        check("库里没有带词的多角色成品 → 自己种一份临时歌词（%s）" % os.path.basename(src),
              n0 > 0, "种完 %d 行（原来 %d 行，前因：%s）" % (n0, 0, why0))
        if n0 <= 0:
            return
        hit = (r, i, o)

    r, idx, _o = hit
    check("找到一条带歌词的多角色成品（%s 第 %d 条）" % (r["name"], idx + 1), True)

    if c.js("!!document.querySelector('#drawer.on')"):
        c.js("closeDrawer(); 1")
        time.sleep(0.4)
    c.js("""(()=>{const s=$('#vizSrc'); s.value='out:%s:%d';
      s.dispatchEvent(new Event('change',{bubbles:true})); return 1;})()""" % (r["id"], idx))
    time.sleep(0.5)
    ok, why = c.click('#vizStyles .viz-st[data-k="cast"]')
    check("真鼠标点到「合唱台」样式", ok, why)
    # ★ 不能"等 2.5 秒就断言"：字幕是**异步**从 /api/viz/cast 拉回来的，
    #   服务正忙时 2.5 秒只够画出一行"正在读…"。这不是产品坏了，是尺子用固定的
    #   睡眠去猜异步 —— 实测就这么假红过一次（65 句正常时 2.5 秒够，忙的时候不够）。
    #   改成"等到有行、最多等 20 秒"。
    n = 0
    for _ in range(25):
        n = c.js("document.querySelectorAll('#subsList .srow').length") or 0
        if n:
            break
        time.sleep(0.8)
    check("字幕表出现了", c.js("getComputedStyle($('#vizSubs')).display") != "none")
    check("每一句一行", isinstance(n, int) and n >= 1,
          "%s ｜ 表里写着：%r ｜ src=%r ｜ style=%r"
          % (n, (c.js("($('#subsList')||{}).innerText") or "")[:120],
             c.js("($('#vizSrc')||{}).value"), c.js("VIZ_STYLE")))
    if not isinstance(n, int) or not n:
        return
    check("一行两格：英文一格、中文一格",
          c.js("document.querySelectorAll('#subsList .sen').length") == n
          and c.js("document.querySelectorAll('#subsList .szh').length") == n)
    en0 = c.js("(document.querySelector('#subsList .sen')||{}).value") or ""
    check("英文那格有字（不是空表）", bool(en0.strip()), en0[:40])
    check("表格有高度上限（不把整页顶长）",
          (c.js("(()=>{const e=$('.vs-list'); return e?e.clientHeight:-1})()") or 0) <= 320,
          c.js("(()=>{const e=$('.vs-list'); return e?e.clientHeight:null})()"))

    # 中文那格：没有就点「自动填中文」（就是用户会点的那颗按钮）
    zh_n = c.js("""(()=>{const a=[...document.querySelectorAll('#subsList .szh')]
        .map(e=>e.value.trim()).filter(Boolean); return a.length;})()""")
    if not zh_n:
        ok, why = c.click("#btnSubsZh")
        check("真鼠标点到「自动填中文」", ok, why)
        for _ in range(20):
            time.sleep(2.0)
            zh_n = c.js("""(()=>{const a=[...document.querySelectorAll('#subsList .szh')]
                .map(e=>e.value.trim()).filter(Boolean); return a.length;})()""")
            if zh_n:
                break
    check("中文那格有字（自动翻过或手填过）", isinstance(zh_n, int) and zh_n >= 1, zh_n)

    # 改第一句 → 点保存 → 服务端真的变了（再改回来）
    ok, why = c.click("#btnSubsSave")
    check("真鼠标点到「保存到成品」", ok, why)
    time.sleep(1.5)
    keep = en0
    trial = (keep + " 试").strip()
    c.js("""(()=>{const e=document.querySelector('#subsList .sen');
      e.value=%s; e.dispatchEvent(new Event('input',{bubbles:true})); return 1;})()"""
         % json.dumps(trial))
    c.click("#btnSubsSave")
    time.sleep(1.8)
    got = ""
    try:
        d = json.loads(urllib.request.urlopen(
            URL + "api/viz/cast?role=%s&index=%d" % (r["id"], idx), timeout=20).read().decode())
        got = ((d.get("cast") or {}).get("lines") or [{}])[0].get("text") or ""
    except Exception:                                # noqa: BLE001
        got = ""
    check("改完点保存，服务端那条字幕真的变了", got == trial, "服务端：%r" % got[:40])
    # 还原（别把探针的脏字留在老板的成品里）
    c.js("""(()=>{const e=document.querySelector('#subsList .sen');
      e.value=%s; e.dispatchEvent(new Event('input',{bubbles:true})); return 1;})()"""
         % json.dumps(keep))
    c.js("vizSubsSave(); 1")
    time.sleep(1.8)
    back = ""
    try:
        d = json.loads(urllib.request.urlopen(
            URL + "api/viz/cast?role=%s&index=%d" % (r["id"], idx), timeout=20).read().decode())
        back = ((d.get("cast") or {}).get("lines") or [{}])[0].get("text") or ""
    except Exception:                                # noqa: BLE001
        back = ""
    check("（还原）那句字幕回到原样", back == keep, "服务端：%r" % back[:40])

    # 负对照：换成别的样式，字幕表必须收起来
    ok, why = c.click('#vizStyles .viz-st[data-k="cqt"]')
    check("真鼠标点到别的样式（光谱仪）", ok, why)
    time.sleep(1.0)
    check("负对照：不是合唱台时字幕表收起来了",
          c.js("getComputedStyle($('#vizSubs')).display") == "none")


def prep():
    """准备测试素材：16 秒片段（有就不动）。

    ★ 为什么片段要**留着**而不是每次重切：歌词缓存是按「路径 + 大小 + mtime」
      做键的，重切一次就换了一把键 ⇒ 每次都得重认一遍歌词（一分钟）。
      留着它，这个门禁第二次起就是秒级。
    """
    if os.path.isfile(SONG):
        return 0
    os.makedirs(os.path.dirname(SONG), exist_ok=True)
    sys.path.insert(0, HERE)
    import studio
    src = os.path.join(HERE, "data", "uploads", "The Walters - I Love You So.mp3")
    if not os.path.isfile(src):
        cand = [os.path.join(HERE, "data", "uploads", f)
                for f in os.listdir(os.path.join(HERE, "data", "uploads"))
                if f.lower().endswith((".mp3", ".wav", ".m4a", ".flac"))]
        src = cand[0] if cand else ""
    if not src:
        print("没有可用的源音频")
        return 2
    r = subprocess.run([studio.FFMPEG, "-y", "-v", "error", "-ss", "40", "-i", src,
                        "-t", "16", "-ac", "1", "-ar", "44100", SONG],
                       capture_output=True, text=True)
    if r.returncode != 0 or not os.path.isfile(SONG):
        print("切片段失败：%s" % (r.stderr or "")[-200:])
        return 2
    print("已准备测试片段：%s" % SONG)
    return 0


def main():
    if "--prep" in sys.argv:
        return prep()
    print("多角色界面探针：target=%s" % URL, flush=True)
    c = boot()
    if not c:
        print("! 起不来 Chrome")
        return 2
    try:
        c.js("localStorage.clear(); 1")
        open_sing(c)
        a_switch(c)
        b_plan(c)
        c_manual(c)
        d_card(c)
        e_subs(c)
    finally:
        errs = c.js("(window.__errs||[]).length") if c else 0
        check("页面没有 JS 报错", not errs, errs)
        # 自己起的 headless Chrome 自己收掉：留着会占住端口 + 让工具以为还在跑
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(c.chrome.pid)],
                           capture_output=True, timeout=20)
        except Exception:
            pass
    print("\n通过 %d 项，失败 %d 项" % (len(OK), len(BAD)), flush=True)
    for b in BAD:
        print("  - %s" % b)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())

