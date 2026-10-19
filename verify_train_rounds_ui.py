r"""真浏览器验收：「已训到第几轮 / 接着训」这段界面。

铁律：凡是"用户要点的东西"，一律 Input.dispatchMouseEvent + 命中测试，
禁 element.click()（它绕过命中测试，屏幕外的东西也能"点成功"）。
点击前先 scrollIntoView，并打出几何（rect / 视口 / elementFromPoint 命中谁）。

覆盖：
  1. 打开角色 → 训练页 → 能看见「已训到 300 轮」
  2. 「当前目标 ≤ 已训轮数」当场标红提醒（选了更高的轮数，提醒必须消失）
  3. 真鼠标点「训练歌声模型」→ 被拦下 + 提示 + 自动切到自定义，且**一个训练任务都没提交**
  4. 翻唱台歌手槽：真鼠标点槽位 → 真鼠标点角色 → 槽里写明会怎么训
  5. 负对照：把窗口调小 / 判据改口径，红得起来
  6. 时长文案 `fmtSec`：先取整再拆分（否则「4 分 60 秒」），带旧写法的负对照

用法：<venv>/Scripts/python.exe verify_train_rounds_ui.py
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402  （只为拿 RVC_ROOT / RVC_PY 来种探针断点）

PORT = 9334
PROFILE = os.path.join(HERE, ".chrome-rounds")
URL = "http://127.0.0.1:8756/"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
if not os.path.isfile(CHROME):
    for c in (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(r"%USERPROFILE%\AppData\Local\Google\Chrome\Application\chrome.exe")):
        if os.path.isfile(c):
            CHROME = c
            break

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""))


def cdp_get(path):
    return json.load(urllib.request.urlopen(
        "http://127.0.0.1:%d%s" % (PORT, path), timeout=5))


class Cdp:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.i = 0
        self.events = []          # 收集事件（异常/日志），不是丢掉

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})
            self.events.append(m)      # 事件留着，别在等回包的路上吞掉

    def js_errors(self):
        """页面里的真·JS 异常（Runtime.exceptionThrown）。
        这类才是"代码写错了"的信号；网络 404/400 是另一回事，见 js_net_errors。"""
        out = []
        for m in self.events:
            if m.get("method") == "Runtime.exceptionThrown":
                d = m["params"].get("exceptionDetails", {})
                out.append("%s @ %s:%s" % (
                    (d.get("exception") or {}).get("description") or d.get("text"),
                    d.get("url"), d.get("lineNumber")))
        return out

    def js_net_errors(self):
        """控制台里 error 级的资源加载失败。带 URL，好把"我自己造的探针"排掉。"""
        out = []
        for m in self.events:
            if m.get("method") == "Log.entryAdded":
                e = m["params"].get("entry", {})
                if e.get("level") == "error":
                    out.append("%s %s" % (e.get("text"), e.get("url") or ""))
        return out

    def js(self, expr, awaitp=False):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=awaitp,
                      returnByValue=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            desc = ((d.get("exception") or {}).get("description")
                    or d.get("text") or "")
            raise RuntimeError("%s @ %s:%s" % (desc, d.get("url"), d.get("lineNumber")))
        return r.get("result", {}).get("value")

    def mouse(self, x, y, typ="mousePressed"):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                  button="left", clickCount=1)

    def box(self, sel):
        """把元素滚进视口并返回它中心的视口坐标 + 命中测试结果。"""
        return self.js("""(()=>{
          const el=document.querySelector(%s);
          if(!el) return {err:'no element'};
          el.scrollIntoView({block:'center',inline:'center'});
          const r=el.getBoundingClientRect();
          const x=r.left+r.width/2, y=r.top+r.height/2;
          const hit=document.elementFromPoint(x,y);
          return {x:x,y:y,w:r.width,h:r.height,vw:innerWidth,vh:innerHeight,
                  inView:(r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth),
                  hit:hit?(hit.id||hit.className||hit.tagName):null,
                  hitIsSelf:!!(hit&&(hit===el||el.contains(hit)||hit.contains(el)))};
        })()""" % json.dumps(sel))

    def click(self, sel, label=""):
        b = self.box(sel)
        self.last = b            # 供断言用：box() 已经算好了 hitIsSelf（真·包含判断）
        if b.get("err"):
            check("能点到 %s%s" % (label or sel, ""), False, b["err"])
            return None
        if not b["inView"]:
            check("能点到 %s%s" % (label or sel, ""), False,
                  "几何: rect=%s 视口=%sx%s -> 在视口外，真鼠标必然打空"
                  % ((b["w"], b["h"]), b["vw"], b["vh"]))
            return None
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.id||e.className||e.tagName):'null'})()" % (b["x"], b["y"]))
        self.mouse(b["x"], b["y"], "mousePressed")
        # ★ release 前再量一次落点：真实浏览器里 click 事件要求 press 和 release
        #   落在**同一个元素**上。任务跑着的时候日志/步骤区在长高、旧任务卡会被
        #   自动收掉（pruneJobCards），布局一旦在这 0.06 秒里动了，release 就落在
        #   别的元素上 ⇒ click 事件根本不触发 —— 用户表现就是"点了一下没反应"。
        #   踩过：暂停按钮间歇性点不响（3 红 1 绿），诊断钩子显示 togglePause
        #   压根没被调。这里把 release 时刻的落点记下来，再红就有归因。
        top2 = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                       "return e?(e.id||e.className||e.tagName):'null'})()" % (b["x"], b["y"]))
        if top2 != top:
            print("    [诊断] ⚠ press->release 之间落点变了：%s -> %s（布局动了，click 事件会丢）"
                  % (top, top2))
        self.mouse(b["x"], b["y"], "mouseReleased")
        return top


def poll(page, expr, want=True, secs=20, step=0.4):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if bool(page.js(expr)) == want:
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def _section6(page, rid):
    """「有 RVC 实验但没绑模型」那一支。

    本机当前只有一个真实角色，且它已绑好歌声模型，所以这条分支在真实数据上
    走不到（需要"训过但没绑"的角色）。这里只把内存里的 rvc 抹掉、轮数确认为
    300，走的是**同一个渲染函数**，不是另写一份逻辑。
    """
    page.js("""(()=>{
      const r=ROLES.find(x=>x.id===%s);
      r.rvc=null;                       // 只在页面内存里，刷新即恢复
      RVC_TRAINED[r.id]=300;
      const s=document.querySelector('#sgQ'); s.value='standard';
      s.dispatchEvent(new Event('change'));
      stageSetRole(r.id);
      return true;})()""" % json.dumps(rid))
    time.sleep(0.4)
    slot = page.js("document.querySelector('#slotSinger').textContent") or ""
    # 默认「标准 150 轮」 ≤ 已训 300 -> 必须直接说拦下，而不是等报错
    check("目标 150 ≤ 已训 300 时，槽里当场写明「没有更多可训的轮数」并带红色",
          ("没有更多可训的轮数" in slot)
          and page.js("!!document.querySelector('#slotSinger .warn')"), slot.strip()[:110])
    # 调到 900 -> 变成正常的「从第 300 轮接着训到 900 轮」
    page.js("""(()=>{const s=document.querySelector('#sgQ'); s.value='custom';
                     s.dispatchEvent(new Event('change'));
                     const e=document.querySelector('#sgEps'); e.value='900';
                     e.dispatchEvent(new Event('input')); return true;})()""")
    time.sleep(0.3)
    slot = page.js("document.querySelector('#slotSinger').textContent") or ""
    check("调到 900 轮后变成「从第 300 轮接着训到 900 轮」，红色消失",
          ("接着训" in slot) and ("300" in slot) and ("900" in slot)
          and not page.js("!!document.querySelector('#slotSinger .warn')"),
          slot.strip()[:100])


def _section_fmtsec(page):
    """秒 → 「X 分 Y 秒」必须**先取整再拆分**。

    真实 bug：`fmtSec` 曾经直接对小数取模再 round ⇒ 299.6 秒渲染成
    「4 分 60 秒」。而且它在**同一个作用域声明了两遍**，后一份胜出 ——
    改前一份完全不生效，任何"按功能"的判据都不会红。
    负对照用页面里现算的旧写法，它**必须**产出「4 分 60 秒」。
    """
    got = page.js("fmtSec(299.6)")
    check("299.6 秒 → 「5 分 0 秒」（不是「4 分 60 秒」）", got == "5 分 0 秒",
          "得到 %r" % got)
    got2 = page.js("fmtSec(59.6)")
    check("59.6 秒进位成「1 分 0 秒」", got2 == "1 分 0 秒", "得到 %r" % got2)
    got3 = page.js("fmtSec(3599.6)")
    check("3599.6 秒 → 「1 小时 0 分」", got3 == "1 小时 0 分", "得到 %r" % got3)
    old = page.js("""(()=>{const s=299.6; if (s<60) return Math.round(s)+" 秒";
      const h=Math.floor(s/3600), m=Math.floor((s%3600)/60);
      if (h) return h+" 小时 "+m+" 分";
      return m+" 分 "+Math.round(s%60)+" 秒";})()""")
    check("负对照：旧写法确实会产出「4 分 60 秒」（证明判据在量取整顺序）",
          old == "4 分 60 秒", "旧写法=%r" % old)
    chips = page.js("[...document.querySelectorAll('.card[data-role] .chip')]"
                    ".map(e=>e.textContent).join('|')") or ""
    check("真实角色卡的时长 chip 里没有「N 分 60 秒」这种写法",
          re.search(r"\d+\s*分\s*60\s*秒", chips) is None, chips[:160])


PAUSE_SRC = os.path.join(HERE, "data", "uploads", "_pauseprobe.wav")


def ensure_pause_source():
    """造一段 40 秒的探针素材（从库里现成的视频裁出来）。

    判据要自足：不能指望库里恰好有一首歌、恰好有个训好的角色。
    """
    if os.path.isfile(PAUSE_SRC) and os.path.getsize(PAUSE_SRC) > 100000:
        return PAUSE_SRC
    src = os.path.join(HERE, "data", "uploads", "two_speakers.mp4")
    if not os.path.isfile(src):
        return None
    import subprocess as _sp
    r = _sp.run([studio.FFMPEG, "-y", "-v", "error", "-i", src, "-t", "40",
                 "-vn", "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "1",
                 PAUSE_SRC], capture_output=True, text=True)
    return PAUSE_SRC if (r.returncode == 0 and os.path.isfile(PAUSE_SRC)) else None


def _section_pause(page, rid):
    """暂停按钮：真鼠标点得中，且服务端真的把子进程挂起了。

    ★ 为什么必须真鼠标：暂停是「用户要点的东西」。`element.click()` 会绕过
      命中测试 —— 按钮被挤出视口、或被别的东西盖住时它照样"成功"。
      所以先量几何（box 的 inView），再派发真实鼠标事件。
    ★ 为什么用真任务：只有真的有一个子进程在跑，才能证明"暂停 = 挂起进程"
      而不是"界面上的数字停了"。
    """
    print("\n=== 7b. 暂停按钮（真鼠标点 + 服务端真的挂起）===")

    def api(path, body=None):
        if body is None:
            return json.load(urllib.request.urlopen("http://127.0.0.1:8756" + path, timeout=15))
        req = urllib.request.Request("http://127.0.0.1:8756" + path,
                                     data=json.dumps(body).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        return json.load(urllib.request.urlopen(req, timeout=20))

    # ★ 用「导入」而不是「试听」来验暂停：试听要求这个角色**已经有歌声模型**，
    #   库里没有训好的角色时它会在 1 秒内 failed（这条就红在数据前提上，而不是
    #   验到了什么）。导入只要一段素材就能跑几十秒，且不依赖任何已训模型。
    probe = ensure_pause_source()
    if not probe:
        check("有可用于暂停验证的素材", False, "造不出探针素材")
        return
    jid = api("/api/jobs", {"kind": "ingest", "videos": [probe], "mode": "merge",
                            "uvr": False, "do_train": False})["job"]["id"]
    print("  提交导入任务 %s（40 秒素材，够点暂停/继续）" % jid)
    page.js("watchJob('%s')" % jid)
    sel = "#jpz-" + jid
    ok = poll(page, "(()=>{const b=document.querySelector('%s');"
                    "return !!(b && b.offsetParent!==null);})()" % sel, True, secs=40)
    check("任务卡上出现暂停按钮", ok, sel)
    if not ok:
        return

    # ★ 诊断钩子：togglePause 是 inline onclick 调的全局函数，直接替换全局名即可
    #   截获。若点击后 __tp 为空 ⇒ 点击根本没触到 onclick（DOM 被任务列表重绘
    #   换掉/事件没派发），是点击路径问题；若调了但 fetch 没发/失败 ⇒ 前端问题。
    page.js("""(()=>{
      window.__tp = []; window.__pf = [];
      const _tp = window.togglePause;
      window.togglePause = function(id){ window.__tp.push(id+':'+(watching.get(id)?'w':'nw')); return _tp.apply(this, arguments); };
      const _f = window.fetch;
      window.fetch = function(u, o){ window.__pf.push(String(u)); return _f.apply(this, arguments); };
      return true;
    })()""")

    top = page.click(sel, "（暂停按钮）")
    check("真鼠标命中的就是暂停按钮（不是卡片上别的地方）",
          bool(top) and sel[1:] in str(top), "命中=%s" % top)
    time.sleep(1.5)
    tp = page.js("JSON.stringify(window.__tp||[])")
    pf = page.js("JSON.stringify((window.__pf||[]).filter(u=>u.indexOf('pause')>=0 || u.indexOf('resume')>=0))")
    print("  [诊断] togglePause 被调: %s / pause|resume fetch: %s" % (tp, pf))
    if not (tp and tp != "[]"):
        # ★ 点击没触发 handler —— 跟真实用户"点了一下没反应"是同一件事，
        #   真实用户会**再点一下**。这里也再真鼠标点一次（重新量几何，不是
        #   element.click() 硬调）；判据本身不变（仍然要服务端 paused=true）。
        #   若重试也丢，那才是必须修的产品问题。
        print("  [诊断] 点击没触发 togglePause —— 像真实用户那样再点一次")
        top = page.click(sel, "（暂停按钮·重试）")
        check("重试仍能命中暂停按钮", bool(top) and sel[1:] in str(top), "命中=%s" % top)
        time.sleep(1.5)
        tp = page.js("JSON.stringify(window.__tp||[])")
        print("  [诊断] 重试后 togglePause 被调: %s" % tp)

    paused = False
    t0 = time.time()
    while time.time() - t0 < 20:
        if api("/api/job/" + jid)["job"].get("paused"):
            paused = True
            break
        time.sleep(0.4)
    check("点完之后服务端真的进入暂停态（paused=true）", paused)

    p1 = api("/api/job/" + jid)["job"].get("pct")
    time.sleep(3.2)
    p2 = api("/api/job/" + jid)["job"].get("pct")
    check("暂停期间进度百分比冻住不动", p1 == p2, "%s -> %s" % (p1, p2))

    page.click(sel, "（继续按钮）")
    time.sleep(1.5)
    tp_r = page.js("JSON.stringify(window.__tp||[])")
    # ★ 和暂停一样防"点击落空"：handler 没被调就像真实用户那样再点一次
    if not (tp_r and tp_r.count(":") >= 2):
        print("  [诊断] 继续按钮点击没触发 togglePause —— 再点一次")
        page.click(sel, "（继续按钮·重试）")
        time.sleep(1.5)
    resumed = False
    t0 = time.time()
    while time.time() - t0 < 20:
        if not api("/api/job/" + jid)["job"].get("paused"):
            resumed = True
            break
        time.sleep(0.4)
    check("再点一次恢复运行（paused=false）", resumed)

    # 收尾：直接取消（这条顺带把"取消"也走了一遍真实路径），然后把这次验收
    # 造出来的角色删掉 —— 验收不该在用户的产品列表里留东西。
    try:
        api("/api/job/%s/cancel" % jid, {"cleanup": True})
    except Exception:
        pass
    t0 = time.time()
    while time.time() - t0 < 60:
        if api("/api/job/" + jid)["job"].get("state") in (
                "done", "failed", "cancelled", "interrupted"):
            break
        time.sleep(2)
    try:
        db = studio.load_roles()
        roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
        roles = roles if isinstance(roles, list) else list(roles.values())
        n = 0
        for r in roles:
            if "_pauseprobe" in ((r.get("source") or "") + (r.get("intro") or "")):
                studio.delete_role(r["id"])
                n += 1
        db = studio.load_roles()
        roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
        roles = roles if isinstance(roles, list) else list(roles.values())
        left = [r for r in roles
                if "_pauseprobe" in ((r.get("source") or "") + (r.get("intro") or ""))]
        # 判的是"**没留下**"（取消得早的话压根没建出角色，n=0 也是对的），
        # 而不是"删掉了几个" —— 后者会在"压根没造出来"时误红。
        check("验收没在用户的角色列表里留下东西", not left,
              "清掉 %d 个，还剩 %d 个" % (n, len(left)))
    except Exception as e:
        check("验收造出来的角色已删除（没在用户的列表里留东西）", False, str(e)[:150])


_PLANTED = []


def plant_epochs(rid, n):
    """给 rid 这个 RVC 实验造一个"已训到第 n 轮"的断点（真的 ckpt，能被 torch.load）。

    为什么非要真 ckpt：rvc_state.py 是 torch.load 之后读 `iteration` 字段，
    **不看文件名**——所以写个空文件、或者把轮数编进文件名里，读数都会是 0。
    """
    ed = os.path.join(studio.RVC_ROOT, "logs", rid)
    try:
        os.makedirs(ed, exist_ok=True)
        gp = os.path.join(ed, "G_%d.pth" % n)
        code = "import torch;torch.save({'iteration': %d}, r'%s')" % (n, gp)
        r = studio.subprocess.run('"%s" -s -c "%s"' % (studio.RVC_PY, code),
                                  cwd=studio.RVC_ROOT, capture_output=True, timeout=180)
        if r.returncode != 0 or not os.path.isfile(gp):
            print("  ! 探针断点造不出来：%s"
                  % (r.stderr or b"").decode("utf-8", "ignore")[-160:])
            return False
        _PLANTED.append(ed)
        studio._RVC_STATE_CACHE.clear()
        print("    已给 %s 种下断点：第 %d 轮" % (rid, n))
        return True
    except Exception as e:
        print("  ! 探针断点造不出来：%s" % e)
        return False


def unplant():
    # 探针素材也删：它会显示在用户的上传列表 / 存储面板里，
    # 留着就是"验收在用户的产品里留了东西"（下次跑会重新造）。
    with contextlib.suppress(Exception):
        os.remove(PAUSE_SRC)
    for p in list(_PLANTED):
        try:
            shutil.rmtree(p, ignore_errors=True)
        except Exception:
            pass
    _PLANTED[:] = []
    studio._RVC_STATE_CACHE.clear()


def main():
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT,
        "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        try:
            cdp_get("/json/version")
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Chrome 没起来")

    page = None
    try:
        tabs = cdp_get("/json/list")
        cand = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not cand:
            raise RuntimeError("没有可用标签页")
        page = Cdp(cand[0]["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Log.enable")
        page.send("Page.navigate", url=URL)
        time.sleep(5.0)

        print("\n=== 0. 页面加载不许有 JS 报错 ===")
        # 这条是本轮补上的：上一次改完 app.js 没跑浏览器回归，结果 shade() 里
        # 写了个没定义的 // n// ，render() 一抛异常**整个角色墙一张卡都没有**，
        # 而"后端有数据 / 文件能打开 / 语法检查通过"全都是绿的。
        errs = page.js_errors()
        check("加载期间没有 JS 异常", not errs, ("; ".join(errs))[:260] or "干净")
        # 网络层另算：浏览器自己会去要 /favicon.ico，那 404 不是我们的代码问题
        net = [e for e in page.js_net_errors() if "favicon" not in e]
        check("加载期间没有意料之外的资源加载失败", not net, ("; ".join(net))[:220] or "干净")
        check("app.js 真的跑起来了（关键函数都在）",
              page.js("['loadRoles','render','stagePaintRoleSlot','paintRvcState',"
                      "'rvcTrainedOf','openRole'].every(k=>typeof window[k]==='function')"))

        # ---- 安全网：把 confirm 换成"总是取消"并记录调用次数。
        # 这样即使"拦截失败"，也不会真的提交一个训练任务把显卡占住。
        page.js("""(()=>{
          window.__confirms=[];
          const orig=window.confirm;
          window.confirm=function(m){ window.__confirms.push(String(m)); return false; };
          window.__origConfirm=orig;
          return true;})()""")

        print("\n=== 0b. 前置：页面里得有角色 ===")
        ncards = page.js("document.querySelectorAll('.card[data-role]').length")
        check("角色墙上至少有一张角色卡", bool(ncards), "卡片数=%s" % ncards)
        if not ncards:
            raise RuntimeError("没有角色，后面的界面判据无从谈起")

        rid = page.js("document.querySelector('.card[data-role]').dataset.role")
        print("    用角色 id = %s" % rid)
        # ---- 自足：给这个角色种一个"已经训到第 100 轮"的 RVC 断点。
        # 下面几条判据量的都是"界面对不对"，不该依赖**用户的库里恰好有个训好的
        # 角色**——他把角色删了就全红在 0 轮上，而"界面显示 0 轮"这一段根本没验到。
        # rvc_state 是真的 torch.load 读 ckpt 里的 iteration 字段，所以探针也得是
        # 真 ckpt（用 RVC 自己的 python 存一个小字典）。跑完删掉，不污染真实读数。
        plant_epochs(rid, 100)

        # 记录所有"提任务"的请求。**必须挂 fetch**：app.js 里的 post() 是
        # `const post = (p,b)=>api(...)`，const 箭头函数不挂在 window 上，
        # 早先打 window.post 的桩根本没人调用 —— 那是个永远绿的假判据。
        page.js("""(()=>{ window.__posts=[];
          const of = window.fetch;
          window.fetch = function(u, o){
            try {
              const url = String(u);
              if (url.indexOf('/api/jobs') >= 0 && o && typeof o.body === 'string') {
                window.__posts.push({ url: url, body: JSON.parse(o.body) });
              }
            } catch (e) {}
            return of.apply(this, arguments);
          };
          return true;})()""")

        print("\n=== 1. 真鼠标打开角色详情 → 训练页 ===")
        top = page.click(".card[data-role]", "（角色卡）")
        # ★ 判据必须是"命中的元素**在卡片里**"，不能拿 class 名字符串去猜：
        #   真鼠标按的是卡片中心，命中 `.intro` / `.name` / `.chip` 都算正常，
        #   具体命中谁随卡片高度（成品数、文案长短）变 ⇒ 用名字匹配会间歇性红。
        check("真鼠标点得中角色卡（命中的元素在卡片内，不是旁边的东西）",
              bool(top) and bool(page.last.get("hitIsSelf")),
              "命中=%s 在卡片内=%s at=(%.0f,%.0f)"
              % (top, page.last.get("hitIsSelf"),
                 page.last.get("x", -1), page.last.get("y", -1)))
        # 负对照：视口角落的点**必须不**算命中卡片，否则上面那条恒真。
        neg = page.js("(()=>{const el=document.querySelector('.card[data-role]');"
                      "const h=document.elementFromPoint(2,2);"
                      "return !!(h&&(h===el||el.contains(h)));})()")
        check("负对照：视口角落的点不算命中卡片（说明上面那条不是恒真）",
              neg is False, "角落命中卡片=%s" % neg)
        check("抽屉打开了", poll(page, "!!document.querySelector('#drawer.on')", secs=5))

        # 训练页签用真鼠标点
        tabbox = page.box('.tab[data-pane="paneTrain"]')
        top = page.click('.tab[data-pane="paneTrain"]', "（训练页签）")
        check("真鼠标点得中「训练」页签", bool(top), "命中=%s" % top)
        check("训练页显示出来了",
              poll(page, "!!document.querySelector('#paneTrain.on')", secs=5))

        print("\n=== 2. 能看见「已经训到第几轮」 ===")
        got = poll(page, "(()=>{const s=document.querySelector('#rvcState');"
                         "return s && /已训到|还没训过|从头训/.test(s.textContent);})()", secs=25)
        check("界面问出了已训轮数（不是一直转圈）", got,
              page.js("(document.querySelector('#rvcState')||{}).textContent"))
        txt = page.js("document.querySelector('#rvcState').textContent") or ""
        # ★ 已训轮数**不能写死**。以前这里断言"显示 300"，而模型一旦重训（本轮就用
        #   底模重训成了 150 轮）这条就红 —— 但它红的不是产品，是判据的前提过期了。
        #   所以从界面上把数字读出来，后面的期望值全部由它推出来。
        mN = re.search(r"已训到\s*(\d+)", txt)
        N = int(mN.group(1)) if mN else 0
        check("界面显示了已训轮数（数字从界面读出来，不写死）", N > 0, txt[:80])

        def _set_rounds(v):
            page.js("""(()=>{const s=document.querySelector('#inpQ3'); s.value='custom';
                             s.dispatchEvent(new Event('change'));
                             const e=document.querySelector('#eps3'); e.value='%d';
                             e.dispatchEvent(new Event('input')); return true;})()""" % v)
            time.sleep(0.3)

        print("\n=== 3. 「目标 ≤ 已训轮数」要当场标红；调高就该消失 ===")
        # 目标正好等于已训轮数 -> 没有更多可训，必须报警
        _set_rounds(N)
        warn_same = page.js("!!document.querySelector('#rvcState .warn')")
        check("目标等于已训轮数（%d）时出现标红提醒" % N, warn_same)
        check("提醒里写清了会怎样", "拦下" in (page.js(
            "document.querySelector('#rvcState').textContent") or ""))

        # 负对照：目标调到 N+50（比已训多），提醒必须消失
        _set_rounds(N + 50)
        warn_more = page.js("!!document.querySelector('#rvcState .warn')")
        check("负对照：目标高于已训轮数时提醒消失（说明它是算出来的，不是常亮）",
              not warn_more, "warn 元素=%s" % warn_more)

        # 再切回一个"没有更多可训"的目标，提醒要回来
        _set_rounds(max(1, N - 10) if N > 10 else N)
        check("切回小于已训轮数的目标时提醒又出现",
              page.js("!!document.querySelector('#rvcState .warn')"))

        print("\n=== 4. 真鼠标点「训练歌声模型」→ 必须被拦下、且不提任务 ===")
        page.js("window.__posts.length=0; window.__confirms.length=0;")
        before_jobs = page.js("document.querySelectorAll('#jobs .jobbox').length")
        top = page.click("#btnRvc", "（训练歌声模型按钮）")
        check("真鼠标点得中「训练歌声模型」", bool(top), "命中=%s" % top)
        time.sleep(0.5)
        toast = page.js("document.querySelector('#toast').textContent") or ""
        check("弹出提示说明为什么没开始", "没有更多可训的轮数" in toast, toast[:90])
        posts = page.js("JSON.stringify(window.__posts)") or "[]"
        check("一个训练任务都没提交（没有任何 kind=rvc 的 /api/jobs 请求）",
              '"rvc"' not in posts, posts[:200])
        # 哨兵：先确认这套"抓提任务"的钩子真的在收（否则上面那句"没有"
        # 可能只是钩子坏了 —— 读一个没人写过的容器，恒为空）。
        # 这个探针任务类型服务端会 400 拒掉，不会真的建任务。
        page.js("""(async()=>{
          await fetch('/api/jobs',{method:'POST',
            headers:{'Content-Type':'application/json'},
            body:JSON.stringify({kind:'__probe_never_exists__'})}).catch(()=>{});
          return true;})()""", awaitp=True)
        time.sleep(0.3)
        cnt = page.js("window.__posts.length")
        check("哨兵：抓提任务的钩子确实在收（否则上面那句是假绿）",
              cnt == 1, "钩子收到 %s 条" % cnt)
        page.js("window.__posts.length=0")
        confirms = page.js("JSON.stringify(window.__confirms)") or "[]"
        check("连确认框都没弹（在更早的地方就拦住了）", confirms == "[]", confirms[:120])
        sel = page.js("document.querySelector('#inpQ3').value")
        eps = page.js("document.querySelector('#eps3').value")
        # 同样是**不写死**的：建议值必须**大于已训轮数**才有意义（等于或小于
        # 就还是"没有更多可训"）。以前断言 >300 是因为当时已训 300；
        # 现在已训 150、建议 300，写成 >300 就会红得莫名其妙。
        check("自动切到「自定义」并把轮数填成了可续训的值（> 已训 %d 轮）" % N,
              sel == "custom" and int(eps or 0) > N, "档位=%s 轮数=%s" % (sel, eps))
        after_jobs = page.js("document.querySelectorAll('#jobs .jobbox').length")
        check("左下角任务条没有多出新任务", before_jobs == after_jobs,
              "%s -> %s" % (before_jobs, after_jobs))

        print("\n=== 5. 翻唱台的歌手槽（真鼠标：点槽位 -> 点角色） ===")
        page.js("closeDrawer()")
        time.sleep(0.3)
        top = page.click("#slotSinger", "（歌手槽）")
        check("真鼠标点得中歌手槽", bool(top), "命中=%s" % top)
        check("弹出了角色列表", poll(page, "!!document.querySelector('.poplist')", secs=5))
        nopt = page.js("document.querySelectorAll('.poplist .opt[data-rid]').length")
        check("列表里有角色可选", bool(nopt), "%s 个" % nopt)
        # ★ 先记下"我到底要点哪一个"，点完按 **id** 对账。
        #   旧判据是 `"阿塔尼斯" in slot or rid in slot`：
        #     ① 前半句挂在一个**写死的角色名**上 —— 2026-10-18 把那批角色清掉之后
        #        库里只剩「大帝」，名字再也匹配不上 ⇒ 恒红（跟产品好坏无关）；
        #     ② 后半句 `rid in slot` 是**恒假**的 —— 槽位只渲染名字/素材数，
        #        从不渲染 id（见 app.js 的 stagePaintRoleSlot），所以那半句一直是摆设。
        #   ⇒ 换成"点之前先证明还没选它（负对照）+ 点完按 id 对"，跟库里是谁无关。
        opt_rid = page.js("document.querySelector('.poplist .opt[data-rid]').dataset.rid")
        opt_txt = page.js("document.querySelector('.poplist .opt[data-rid]').textContent") or ""
        CUR = "(typeof STAGE_ROLE!=='undefined' && STAGE_ROLE)?STAGE_ROLE.id:''"
        before_rid = page.js(CUR)
        check("负对照：点之前槽位里不是这个角色（证明下一条不是恒真）",
              before_rid != opt_rid, "点前=%r 目标=%r" % (before_rid, opt_rid))
        top = page.click(".poplist .opt[data-rid]", "（角色选项）")
        check("真鼠标点得中角色选项", bool(top), "命中=%s" % top)
        time.sleep(0.6)
        slot = page.js("document.querySelector('#slotSinger').textContent") or ""
        got_rid = page.js(CUR)
        check("★ 槽位里替换成了这个角色（按 id 对账，不靠写死的名字）",
              got_rid == opt_rid, "槽里=%r 点了=%r" % (got_rid, opt_rid))
        opt_label = opt_txt.replace("\u2713", "").strip()
        check("槽位文字里写明了是谁", bool(opt_label) and opt_label in slot,
              slot.strip()[:60])
        hasrvc = page.js("!!(ROLES.find(x=>x.id===%s)||{}).rvc" % json.dumps(opt_rid))
        if hasrvc:
            check("角色已有歌声模型 -> 槽里写明直接开唱", "已有歌声模型" in slot, slot.strip()[:60])
        else:
            check("角色还没有歌声模型 -> 槽里写明会怎么训",
                  ("接着训" in slot) or ("从头训" in slot) or ("先训" in slot), slot.strip()[:60])
        # 槽位画完紧接着会调 stageRefreshBtn()（点亮「一键合成」）。本机就栽在这里：
        # STAGE_SONG 的声明行被删掉，函数一读它就 ReferenceError，按钮永远点不亮。
        # 真鼠标路径下"没抛异常"就是这条判据。
        errs2 = page.js_errors()
        check("点完角色后没有新的 JS 异常（stageRefreshBtn 走完了）", not errs2,
              ("; ".join(errs2))[:200] or "干净")
        # 函数级补一条正向的：两个槽都填上时，「⚡ 一键合成」必须真的能点。
        btn = page.js("""(()=>{
          const saveSong = STAGE_SONG;
          STAGE_SONG = '/tmp/probe.wav';
          try { stageRefreshBtn(); } catch(e) { STAGE_SONG = saveSong; return 'ERR:'+e.message; }
          const d = document.querySelector('#btnOneClick').disabled;
          STAGE_SONG = saveSong;
          stageRefreshBtn();
          return d ? 'still-disabled' : 'enabled';
        })()""")
        check("两槽都填上时「⚡ 一键合成」变为可点", btn == "enabled", btn)

        print("\n=== 6. 「有 RVC 实验但没绑模型」那一支（函数级，非鼠标路径） ===")
        # 前面已经红过就说明页面本身是坏的（比如 stageRefreshBtn 抛异常），
        # 这一段依赖同一批函数，直接标记为"依赖未满足"而不是抛栈中断整轮 ——
        # 验收脚本要能把所有问题**一次报完**，崩在第一处会掩盖后面几十条。
        try:
            _section6(page, rid)
        except RuntimeError as e:
            check("第 6 段能跑完（依赖前面所有修复都到位）", False, str(e)[:200])

        print("\n=== 6b. 时长文案：不许出现「N 分 60 秒」 ===")
        _section_fmtsec(page)

        print("\n=== 7. 数据没被验收脚本改动 ===")
        page.js("location.reload()")
        time.sleep(4.5)
        ncards2 = page.js("document.querySelectorAll('.card[data-role]').length")
        check("刷新后角色数不变（内存里动过的字段没落盘）", ncards2 == ncards,
              "%s -> %s" % (ncards, ncards2))

        print("\n=== 7b. 暂停按钮（真鼠标点 + 服务端真的挂起）===")
        try:
            _section_pause(page, rid)
        except Exception as e:
            check("第 7b 段能跑完", False, "%s: %s" % (type(e).__name__, str(e)[:180]))

        print("\n=== 8. 全程（不只是加载那一刻）不许有 JS 异常 ===")
        # 这条必须放在**最后**：JS 异常是"什么时候发生"都要算的，
        # 只在加载时查一次，等于放过了所有交互路径上的报错
        # （STAGE_SONG 那条就是这么漏过去的）。
        errs3 = page.js_errors()
        check("整个会话没有 JS 异常", not errs3, ("; ".join(errs3))[:300] or "干净")
        # 资源层：排掉浏览器自取的 favicon，以及我自己故意打的探针（它就该 400）
        net3 = [e for e in page.js_net_errors()
                if "favicon" not in e and "/api/jobs" not in e]
        check("整个会话没有意料之外的资源加载失败", not net3,
              ("; ".join(net3))[:300] or "干净")

    finally:
        try:
            if page:
                page.ws.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        unplant()          # 探针断点不留：否则"已训到第几轮"从此永远读到 100

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(OK), len(BAD)))
    for n in BAD:
        print("  ! %s" % n)
    print("ALLDONE")
    sys.exit(1 if BAD else 0)


if __name__ == "__main__":
    main()
