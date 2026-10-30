# -*- coding: utf-8 -*-
r"""拖拽罩子 / 任务卡步骤行 的真浏览器验收。

为什么要有这一关：
  这两处坏掉时**任何静态检查都是绿的** ——
  · `body.dropping` 是全屏 72% 黑罩 + 虚线框 + 中间一行提示字（CSS ::after）。
    `drop` 处理里 `if (inSlot) return;` 写在了清 class **之前**，于是往槽位里
    放一次文件，罩子就永久卡住：整页发暗、那行字擦不掉，用户报的就是"UI 又坏了"。
  · 任务卡的 step 是一行 flex：「步骤名 + 人话 + 详细说明」挤在 430px 里，
    基础宽为 0 的 .sh 被 nowrap 的 .nt 挤成 0 宽，而它允许换行 ⇒ 每个字一行，
    一条 step 实测高 446px（正常 29px），把整张卡撑成一列竖排的字。
  两者都只有**真跑页面 + 真派事件**才看得见。

规矩：
  · 真事件：用 DragEvent 走页面自己的监听器，不用 element.click() 之类。
  · 判据必须能红：C 段把 CSS 改回旧口径，B 段的几何判据必须当场变红。
  · 不依赖运行时残留：注入一个临时 .jobbox 造场景，跑完删掉；
    不要求真有一个任务在跑（否则任务一结束判据就"通过"了，等于没验）。
"""
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

HERE = os.path.dirname(os.path.abspath(__file__))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:8756/"
PORT = 9339
PROFILE = os.path.join(HERE, ".cdp-drag")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name +
          (("  | " + str(detail)) if detail else ""), flush=True)


def cdp_get(path, port=PORT):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=20) as r:
        return json.loads(r.read().decode())


class Cdp:
    def __init__(self, port=PORT):
        import websocket
        page = [t for t in cdp_get("/json/list", port) if t.get("type") == "page"]
        if not page:
            raise RuntimeError("没有可用的页面目标")
        self.ws = websocket.create_connection(page[0]["webSocketDebuggerUrl"], timeout=60)
        self.i = 0
        self.send("Page.enable")

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
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=False)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:400])
        return r.get("result", {}).get("value")


def boot():
    """★ 进程活不过一条命令，拉起来和用必须同一个进程里。"""
    os.makedirs(PROFILE, exist_ok=True)
    subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--mute-audio",
                      "--remote-debugging-port=%d" % PORT,
                      "--user-data-dir=%s" % PROFILE, "--no-first-run",
                      "--remote-allow-origins=*",
                      "--window-size=1440,1000", URL],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(45):
        time.sleep(1.0)
        try:
            cdp_get("/json/list")
            time.sleep(2.0)
            c = Cdp()
            c.send("Network.enable")
            c.send("Network.setCacheDisabled", cacheDisabled=True)
            c.send("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
                   deviceScaleFactor=1, mobile=False)
            c.send("Page.navigate", url=URL + "?t=%d" % int(time.time()))
            time.sleep(3.5)
            return c
        except Exception:
            continue
    return None


def make_file_dt():
    return """(()=>{const dt=new DataTransfer();
      dt.items.add(new File([new Blob(['x'])],'probe.wav',{type:'audio/wav'}));
      return dt;})()"""


def sec(t):
    print("\n" + "-" * 58 + "\n" + t + "\n" + "-" * 58)


def a_drag(c):
    sec("A. 拖拽罩子不许卡住（body.dropping）")
    # 先确认目标元素存在：.slot 在翻唱台上
    check("页面有拖拽落点 .slot", bool(c.js("!!document.querySelector('.slot')")),
          "没有的话这一关测不到真路径")
    check("初始没有 dropping", "dropping" not in (c.js("document.body.className") or ""))

    # dragover 带 Files ⇒ 上罩子（这是设计行为，必须能上）
    c.js("""(()=>{const dt=%s;
      window.dispatchEvent(new DragEvent('dragover',
        {bubbles:true,cancelable:true,dataTransfer:dt}));
      return document.body.className;})()""" % make_file_dt())
    time.sleep(0.25)
    check("dragover 时罩子出现", "dropping" in (c.js("document.body.className") or ""))

    # ★ 本轮修的 bug：往 .slot 里 drop 之后，罩子必须没
    c.js("""(()=>{
      const d=document.createElement('div');
      d.className='slot'; d.id='__probe_slot';
      d.style.cssText='position:fixed;left:8px;top:8px;width:120px;height:50px;z-index:9999';
      document.body.appendChild(d);
      const dt=%s;
      d.dispatchEvent(new DragEvent('drop',
        {bubbles:true,cancelable:true,dataTransfer:dt}));
      return true;})()""" % make_file_dt())
    time.sleep(0.35)
    check("drop 到槽位后罩子必须清掉", "dropping" not in (c.js("document.body.className") or ""),
          "残留 = 整页发暗、中间那行提示字擦不掉")

    # 拖到空白处（没有文件载荷）
    c.js("""(()=>{document.body.classList.add('dropping');
      window.dispatchEvent(new DragEvent('drop',
        {bubbles:true,cancelable:true,dataTransfer:new DataTransfer()}));
      return document.body.className;})()""")
    time.sleep(0.25)
    check("drop 无文件时罩子也要清掉", "dropping" not in (c.js("document.body.className") or ""))

    # 拖出窗口：relatedTarget 为 null 才是"真的出窗"
    c.js("""(()=>{document.body.classList.add('dropping');
      window.dispatchEvent(new DragEvent('dragleave',
        {bubbles:true,cancelable:true,clientX:5,clientY:5,relatedTarget:null}));
      return document.body.className;})()""")
    time.sleep(0.25)
    check("拖出窗口（relatedTarget=null）后罩子清掉",
          "dropping" not in (c.js("document.body.className") or ""))

    # 拖到别的程序 / Esc：浏览器只发 dragend
    c.js("""(()=>{document.body.classList.add('dropping');
      window.dispatchEvent(new DragEvent('dragend',{bubbles:true,cancelable:true}));
      return document.body.className;})()""")
    time.sleep(0.2)
    check("dragend（被系统取消）后罩子清掉",
          "dropping" not in (c.js("document.body.className") or ""))

    c.js("window.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}))")
    time.sleep(0.15)
    check("Esc 之后罩子也不许留", "dropping" not in (c.js("document.body.className") or ""))

    # 元素之间移动时**不能**清（否则拖过任何元素都闪一下）
    c.js("""(()=>{document.body.classList.add('dropping');
      window.dispatchEvent(new DragEvent('dragleave',
        {bubbles:true,cancelable:true,clientX:400,clientY:300,
         relatedTarget:document.body}));
      return document.body.className;})()""")
    time.sleep(0.2)
    check("窗口内元素间移动时不闪（负对照）",
          "dropping" in (c.js("document.body.className") or ""))
    c.js("document.body.classList.remove('dropping')")


def b_layout(c):
    sec("B. 任务卡「正在做的那一步」不许竖排")
    # 注入一个临时 .jobbox 造场景（不依赖真有任务在跑）
    c.js("""(()=>{
      const old=document.getElementById('__probe_jobs'); if(old) old.remove();
      const host=document.createElement('div');
      host.className='jobs'; host.id='__probe_jobs';
      host.style.cssText='position:fixed;left:0;top:0;width:430px;height:auto;z-index:1';
      host.innerHTML='<div class="jobbox open"><div class="steps">'+
        '<div class="step now" id="__probe_step">'+
          '<span class="mk">●</span>'+
          '<span class="nm">歌声转换（RVC）</span>'+
          '<span class="sh">正在用这个角色的音色重唱 —— 显卡会被占满，长歌十几分钟'+
            '<b class="seta"> · 约需 12 分钟</b></span>'+
          '<span class="nt">按这个角色的音色重唱（输入是分离好的人声轨，242 秒，'+
            '音高 -12 半音）。这一步没有细粒度百分比，看下面的「已用」和日志</span>'+
        '</div></div></div>';
      document.body.appendChild(host);
      return true;})()""")
    time.sleep(0.4)
    g = c.js("""(()=>{
      const st=document.getElementById('__probe_step');
      if(!st) return null;
      const sh=st.querySelector('.sh'), nt=st.querySelector('.nt');
      const r=st.getBoundingClientRect(), rs=sh.getBoundingClientRect();
      const ss=getComputedStyle(st), cs=getComputedStyle(sh), cn=getComputedStyle(nt);
      return {h:Math.round(r.height), w:Math.round(r.width),
              shw:Math.round(rs.width), shh:Math.round(rs.height),
              wrap:ss.flexWrap, flexBasis:cs.flexBasis, order:cs.order,
              ntBasis:cn.flexBasis};})()""")
    check("注入的探针 step 挂上了", bool(g))
    if g:
        check("步骤行不算高（< 100px）", g["h"] < 100, "实测 %spx（坏的时候 446px）" % g["h"])
        check("「人话」那一栏有宽度（> 100px）", g["shw"] > 100,
              "实测 %spx（坏的时候 0px，逐字换行）" % g["shw"])
        check("「人话」只占 1~2 行（< 60px）", g["shh"] < 60, "实测 %spx" % g["shh"])
        check("步骤行允许换行（flex-wrap:wrap）", g["wrap"] == "wrap", g["wrap"])
        check("「人话」独占一行（flex-basis:100%）", g["flexBasis"] in ("100%",),
              g["flexBasis"])
        check("「人话」排到最后（order:1）", str(g["order"]) == "1", g["order"])
        check("详细说明可压缩（basis 0）", g["ntBasis"] in ("0%", "0px"), g["ntBasis"])
    return g


def c_negctl(c):
    sec("C. 负对照（把 CSS 改回旧口径，B 段必须变红）")
    old = c.js("""(()=>{
      const st=document.getElementById('__probe_step');
      if(!st) return 'NO_STEP';
      const sh=st.querySelector('.sh'), nt=st.querySelector('.nt');
      st.style.flexWrap='nowrap';
      sh.style.flex='1 1 0%';      /* 旧口径：basis 0，抢不到宽度 */
      sh.style.order='0';
      sh.style.marginLeft='0';
      nt.style.flex='0 1 auto';    /* 旧口径：nowrap 内容撑住，把 .sh 挤没 */
      const r=st.getBoundingClientRect(), rs=sh.getBoundingClientRect();
      return {h:Math.round(r.height), shw:Math.round(rs.width)};})()""")
    check("负对照场景注入成功", isinstance(old, dict), old)
    if isinstance(old, dict):
        print("    旧口径实测：step 高 %spx · .sh 宽 %spx" % (old["h"], old["shw"]))
        check("旧口径下几何判据确实会红（证明尺子有效）",
              old["h"] >= 100 or old["shw"] <= 100,
              "高 %spx / 宽 %spx" % (old["h"], old["shw"]))
    c.js("(()=>{const e=document.getElementById('__probe_jobs'); if(e) e.remove();"
         "const s=document.getElementById('__probe_slot'); if(s) s.remove();"
         "document.body.classList.remove('dropping'); return true;})()")


def settle(c, tries=24):
    """等滚动停稳再量。

    ★ 踩过：自动滚屏是 22px/帧，上一版在"还在滚"的时候就去量 y1，
      紧接着的负对照必然多出几十像素 —— 判据本身没错，是**量得太急**
      （典型的时间竞态：不是产品问题，也不是判据没牙）。
    """
    last = None
    for _ in range(tries):
        y = c.js("Math.round(window.scrollY)") or 0
        if last is not None and abs(y - last) <= 1:
            return y
        last = y
        time.sleep(0.15)
    return c.js("Math.round(window.scrollY)") or 0


def d_scroll(c):
    """拖拽时必须能滚过页面。

    【为什么单独立一段】角色墙在页面下半部分，歌手槽在**最上面**；而 Chrome
    在原生拖拽过程中**忽略滚轮**，只有把指针顶到视口边缘才滚（还很慢）。
    用户报的"拖不上去、翻不过页面"就是这个。
    ★ 判据是**几何**的（scrollY 真的变了），不是"代码里有没有 rAF"。
    """
    sec("D. 拖角色卡时页面要能滚（否则拖不到最上面的歌手槽）")
    h = c.js("window.innerHeight") or 800
    total = c.js("document.documentElement.scrollHeight") or 0
    check("页面本身够长（能滚才有意义）", total > h + 300,
          "文档高 %s / 视口 %s" % (total, h))

    # 先到底部：角色墙在那里
    c.js("window.scrollTo(0, document.documentElement.scrollHeight)")
    y0 = settle(c)
    check("已经滚到页面底部", y0 > 100, "scrollY=%s" % y0)

    # 真派 dragstart（卡片上），再持续把指针顶在上边缘
    c.js("""(()=>{const card=document.querySelector('.card[data-role]');
      if(!card) return 'NO_CARD';
      const dt=new DataTransfer();
      card.dispatchEvent(new DragEvent('dragstart',
        {bubbles:true,cancelable:true,dataTransfer:dt,clientX:300,clientY:900}));
      return true;})()""")
    c.js("""(()=>{let n=0;const tick=()=>{
        window.dispatchEvent(new DragEvent('dragover',
          {bubbles:true,cancelable:true,clientY:4,dataTransfer:new DataTransfer()}));
        if(++n>36) clearInterval(tmr);};
      const tmr=setInterval(tick,25); tick(); return true;})()""")
    time.sleep(0.9)
    y1 = settle(c)
    check("★ 指针顶在上边缘时页面真的往上滚了", y1 < y0 - 100,
          "%s -> %s（%+d）" % (y0, y1, y1 - y0))

    # ★ 拖拽"消失"（dragend 一直没来，比如拖到别的程序）时，滚动必须**自己停**。
    #   否则它就是一个永远在偷偷滚页面的定时器 —— 手一松页面还在动。
    time.sleep(1.3)
    y1b = settle(c)
    check("拖拽消失后滚动自己停住（不会变成偷偷滚页面的定时器）",
          abs(y1b - y1) <= 8, "%s -> %s" % (y1, y1b))

    # 负对照：指针在视口正中 → 不许滚（否则正常拖拽时页面会自己乱飘）
    c.js("document.dispatchEvent(new DragEvent('dragend',{bubbles:true,cancelable:true}))")
    c.js("""(()=>{let n=0;const mid=()=>{
        window.dispatchEvent(new DragEvent('dragover',
          {bubbles:true,cancelable:true,clientY:Math.round(window.innerHeight/2),
           dataTransfer:new DataTransfer()}));
        if(++n>28) clearInterval(tmr);};
      const tmr=setInterval(mid,25); mid(); return true;})()""")
    time.sleep(1.1)
    y2 = settle(c)
    check("负对照：指针在视口中间时不滚", abs(y2 - y1b) <= 8,
          "%s -> %s" % (y1b, y2))
    c.js("window.scrollTo(0,0)")


def click_sel(c, sel):
    """真鼠标点：滚动到可见 -> 取几何中心 -> Input.dispatchMouseEvent。"""
    box = c.js("""(()=>{const e=document.querySelector(%r); if(!e) return null;
      e.scrollIntoView({block:'center'});
      const r=e.getBoundingClientRect();
      return {x:Math.round(r.left+r.width/2), y:Math.round(r.top+r.height/2),
              w:Math.round(r.width), h:Math.round(r.height)};})()""" % sel)
    if not box or box["w"] < 8 or box["h"] < 8:
        return None
    for t in ("mousePressed", "mouseReleased"):
        c.send("Input.dispatchMouseEvent", type=t, x=box["x"], y=box["y"],
               button="left", clickCount=1)
    time.sleep(0.35)
    return box


def e_pick(c):
    """多选删除：进得去、全选只勾当前列表、多选态点卡片不许开详情。"""
    sec("E. 多选删除（全选 / 计数 / 点卡片=勾选）")
    n = c.js("document.querySelectorAll('.card[data-role]').length") or 0
    check("角色墙有卡片（少于 2 张这段测不出东西）", n >= 2, "%d 张" % n)
    if n < 2:
        return

    c.js("pickToggle()")
    time.sleep(0.3)
    check("进入多选：卡片都带上 pickmode", (c.js("document.querySelectorAll('.card.pickmode').length") or 0) == n,
          "%s / %d" % (c.js("document.querySelectorAll('.card.pickmode').length"), n))
    check("每张卡出现勾选框", (c.js("document.querySelectorAll('.card .pickbox').length") or 0) == n)
    check("多选态下卡片不许再拖（否则点选和拖拽抢同一个按下）",
          c.js("document.querySelector('.card[data-role]').getAttribute('draggable')") == "false")

    c.js("pickAll(true)")
    time.sleep(0.25)
    check("全选后计数 = 卡片数", c.js("PICKED.size") == n, "%s" % c.js("PICKED.size"))
    check("界面上的数字跟着变（不是只改了内存）", str(c.js("$('#pickN').textContent")) == str(n),
          c.js("$('#pickN').textContent"))
    check("勾中的卡片有视觉标记", (c.js("document.querySelectorAll('.card.picked').length") or 0) == n)
    check("删除按钮变成可点", c.js("!$('#btnPickDel').disabled"))

    # ★ 负对照：多选态点卡片 = 勾选，**绝不能**打开详情抽屉
    first = c.js("document.querySelector('.card[data-role]').dataset.role") or ""
    click_sel(c, ".card[data-role]")
    check("★ 多选态点卡片不会打开详情抽屉",
          not c.js("!!document.querySelector('#drawer.on')"),
          "抽屉开了 = 点选和详情抢了同一个动作")
    check("点一下 = 取消这一张的勾选", c.js("PICKED.size") == n - 1,
          "%s（勾了 %d 张）" % (c.js("PICKED.size"), n))
    check("点的那张确实从勾选里拿掉了", c.js("!PICKED.has(%r)" % first))

    # 全选只作用于**当前列表**（搜索过滤生效时不能把没显示的也勾上）
    q = c.js("(ROLES[0]&&ROLES[0].name||'').slice(0,2)") or ""
    if q:
        c.js("$('#roleFind').value = %s; roleFind()" % json.dumps(q))
        time.sleep(0.4)
        shown = c.js("document.querySelectorAll('.card[data-role]').length") or 0
        c.js("pickAll(true)")
        time.sleep(0.25)
        got = c.js("PICKED.size")
        check("搜『%s』后全选只勾搜到的 %d 个（不能把没显示的也删）" % (q, shown),
              got == shown and shown <= n, "勾了 %s / 显示 %s / 总共 %d" % (got, shown, n))
        c.js("roleFindClear()")
        time.sleep(0.35)

    c.js("pickExit()")
    time.sleep(0.3)
    check("退出多选：勾选清空、卡片恢复可拖",
          c.js("PICKED.size") == 0 and c.js("PICK_ON") is False
          and c.js("document.querySelector('.card[data-role]').getAttribute('draggable')") == "true")
    check("退出后没有任何勾选框残留",
          (c.js("document.querySelectorAll('.card .pickbox').length") or 0) == 0)


def f_drop_role(c):
    """把角色卡真的拖进歌手槽 —— 这才是用户那条路。

    【为什么单独立一段】D 段只证明"能滚到上面"，但滚到了不等于放得进去。
    这里用**同一个 DataTransfer** 连接 dragstart 与 drop（浏览器真实拖拽也是这么传的），
    再看歌手槽里到底有没有这个人。
    ★ 负对照：把 dataTransfer 换成空的（模拟"拖的不是角色卡"），
      槽里不许变 —— 否则说明判据是白送的。
    """
    sec("F. 角色卡拖进歌手槽（真 DataTransfer 走完整条路）")
    rid = c.js("document.querySelector('.card[data-role]') && "
               "document.querySelector('.card[data-role]').dataset.role") or ""
    nm = c.js("(ROLES.find(x=>x.id===%r)||{}).name || ''" % rid) if rid else ""
    check("拿到一张角色卡", bool(rid) and bool(nm), "%s / %s" % (rid, nm))
    if not rid:
        return

    # 清空名单：从已经有人的状态开始，会看不清"这次到底加没加上"
    c.js("""(()=>{ while(STAGE_CASTS.length) stageRemoveRole(STAGE_CASTS[0].rid); return true;})()""")
    time.sleep(0.4)
    check("先把演唱名单清空（否则'加上了'可能是上一个留下的）",
          c.js("STAGE_CASTS.length") == 0)

    ok = c.js("""(()=>{
      const card=document.querySelector('.card[data-role]');
      const slot=document.getElementById('slotSinger');
      if(!card||!slot) return 'NO_EL';
      const dt=new DataTransfer();
      card.dispatchEvent(new DragEvent('dragstart',
        {bubbles:true,cancelable:true,dataTransfer:dt,clientX:300,clientY:900}));
      slot.dispatchEvent(new DragEvent('dragover',
        {bubbles:true,cancelable:true,dataTransfer:dt}));
      slot.dispatchEvent(new DragEvent('drop',
        {bubbles:true,cancelable:true,dataTransfer:dt}));
      return 'OK';})()""")
    check("派事件没报错", ok == "OK", ok)
    time.sleep(0.6)
    check("★ 拖进歌手槽后，这个人真的进了演唱名单",
          c.js("STAGE_CASTS.some(x=>x.rid===%r)" % rid),
          "STAGE_CASTS=%s" % c.js("JSON.stringify(STAGE_CASTS.map(x=>x.rid))"))
    slot_txt = c.js("document.querySelector('#slotSinger').textContent") or ""
    check("歌手槽上显示出了这个名字", bool(nm) and nm in slot_txt,
          "槽里是「%s」" % slot_txt.strip()[:50])

    # 负对照：载荷里没有 text/role-id（比如拖的是个文件）⇒ 不许往名单里塞人
    n_before = c.js("STAGE_CASTS.length")
    c.js("""(()=>{
      const slot=document.getElementById('slotSinger');
      const dt=new DataTransfer();
      dt.setData('text/plain','not-a-role-id');
      slot.dispatchEvent(new DragEvent('drop',
        {bubbles:true,cancelable:true,dataTransfer:dt}));
      return true;})()""")
    time.sleep(0.5)
    check("负对照：拖的不是角色卡时名单不变（说明判据不是白送的）",
          c.js("STAGE_CASTS.length") == n_before,
          "%s -> %s" % (n_before, c.js("STAGE_CASTS.length")))
    c.js("""(()=>{ while(STAGE_CASTS.length) stageRemoveRole(STAGE_CASTS[0].rid); return true;})()""")


def main():
    c = boot()
    if c is None:
        print("!! 浏览器起不来")
        return 2
    print("=" * 58)
    print("拖拽罩子 / 任务卡步骤行 真浏览器验收")
    print("=" * 58)
    a_drag(c)
    b_layout(c)
    c_negctl(c)
    d_scroll(c)
    e_pick(c)
    f_drop_role(c)
    print("\n" + "=" * 58)
    print("通过 %d / 失败 %d" % (len(OK), len(BAD)))
    if BAD:
        for n in BAD:
            print("  FAIL: " + n)
    print("=" * 58)
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
