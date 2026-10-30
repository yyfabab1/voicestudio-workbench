#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""给老板看一眼：大厅里「手动指定」长什么样（真浏览器截图，不是画出来的）。

为什么留着它：「我能不能决定哪一句谁唱」是**界面上的**事，
判据只能证明"点了那一行 data-who 变了"，证明不了"它摆在那里像不像个能用的东西"。
一张图比一堆 PASS 行有用 —— 老板扫一眼就知道这一版要不要改。

用法（项目根目录）：
    python tools/shot_hall_manual.py
产物：data/_preview/hall_manual.png

★ 服务和浏览器必须在**同一条命令**里起：工具里起的常驻进程活不过一条命令。
★ 这里用 element.click() 只是为了**摆个样子拍照**，不作为"能点"的证据 ——
  真鼠标（dispatchMouseEvent + 命中测试）的验收在 tools/probe_hall_multi.py。
★ 要用项目 venv 的解释器跑（要 websocket）。
"""
import base64
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
sys.path.insert(0, HERE)

PORT = int(os.environ.get("VS_SHOT_PORT", "8902"))
CDP_PORT = int(os.environ.get("VS_CDP_PORT", "9344"))
PROFILE = os.path.join(HERE, ".cdp-shot")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:%d/" % PORT
OUT = os.path.join(HERE, "data", "_preview", "hall_manual.png")

# 苹果嘉儿 / 云宝：两个都有歌声模型，且是真的角色（不是门禁里的甲乙丙）
CASTS = ["rbe0d5b48", "r3167b075"]
SONG = os.path.join("data", "music", "Clean Bandit_Jess Glynne - Rather Be.mp3")
# 排出来的样子：前几句独唱轮流、副歌那几句标成合唱，才看得出这面板能干什么
PLAN = "solo_solo_cho_cho_solo_solo"


def cdp_get(path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (CDP_PORT, path),
                                timeout=20) as r:
        return json.loads(r.read().decode())


class Cdp(object):
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
        self.ws.send(json.dumps({"id": mid, "method": method,
                                 "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Page.javascriptDialogOpening":
                self.i += 1
                self.ws.send(json.dumps({
                    "id": self.i, "method": "Page.handleJavaScriptDialog",
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


def start_service():
    import studio
    if not os.path.isfile(studio.PY):
        raise RuntimeError("找不到运行时：%s" % studio.PY)
    env = dict(os.environ)
    env["VOICESTUDIO_PORT"] = str(PORT)
    logp = os.path.join(HERE, "_tmp", "shot_svc_%d.log" % PORT)
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    f = open(logp, "wb")
    p = subprocess.Popen([studio.PY, "-u", os.path.join(HERE, "server.py")],
                         cwd=HERE, env=env, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(0.7)
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/api/health" % PORT, timeout=3) as r:
                if json.loads(r.read().decode()).get("ok"):
                    return p
        except Exception:
            pass
    return p


def main():
    # ★ 前置检查：websocket 只装在项目的 venv 里，拿纯解释器跑会在
    #   「连浏览器」那一步才炸，报出来却像"浏览器没起来"（2026-10-23 实测
    #   照这条假线查了半天，真因是解释器选错）。先说清楚，别让人查错方向。
    try:
        import websocket  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "这个工具要用 websocket 连浏览器，请用项目 venv 跑：\n"
            "  ~/.workbuddy/binaries/python/envs/default/Scripts/python.exe "
            "tools/shot_hall_manual.py")
    svc = start_service()
    os.makedirs(PROFILE, exist_ok=True)
    # ★ 别把 chrome 的输出丢进 DEVNULL：它起不来的时候那是最有价值的线索
    #   （实测 2026-10-23：报"浏览器没起来"但 chrome 其实是被别的东西挡住的）
    clog = os.path.join(HERE, "_tmp", "shot_chrome_%d.log" % CDP_PORT)
    cf = open(clog, "wb")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--mute-audio",
         "--remote-debugging-port=%d" % CDP_PORT,
         "--user-data-dir=%s" % PROFILE, "--no-first-run",
         "--remote-allow-origins=*", "--window-size=1440,1100", URL],
        stdout=cf, stderr=subprocess.STDOUT)
    c = None
    try:
        for _ in range(45):
            time.sleep(1.0)
            try:
                cdp_get("/json/list")
                time.sleep(1.5)
                c = Cdp()
                break
            except Exception as e:
                last = e
        if c is None:
            # 把能拿到的都摆出来：服务活着吗、chrome 还活着吗、它最后说了什么
            svc_ok = "?"
            try:
                with urllib.request.urlopen(
                        "http://127.0.0.1:%d/api/health" % PORT, timeout=3) as r:
                    svc_ok = "ok" if json.loads(r.read().decode()).get("ok") \
                        else "not-ok"
            except Exception as e:
                svc_ok = "dead(%s)" % type(e).__name__
            cf.flush()
            try:
                tail = open(clog, "rb").read()[-600:].decode("utf-8", "replace")
            except OSError:
                tail = ""
            raise RuntimeError(
                "连不上浏览器 CDP（不是浏览器没起）：服务=%s chrome退出码=%s\n"
                "最后一条错误：%s\n%s"
                % (svc_ok, chrome.poll(), last, tail))
        c.send("Emulation.setDeviceMetricsOverride", width=1440, height=1100,
               deviceScaleFactor=1, mobile=False)
        c.send("Page.navigate", url=URL + "?t=%d" % int(time.time()))
        time.sleep(4.0)

        song = SONG.replace("\\", "/")
        c.js("stageSetRole(%s)" % json.dumps(CASTS[0]))
        time.sleep(0.3)
        c.js("stageSetRole(%s)" % json.dumps(CASTS[1]))
        time.sleep(0.3)
        c.js("stageSetSong(%s, %s)"
             % (json.dumps(os.path.join(HERE, *song.split("/"))),
                json.dumps(os.path.basename(song))))
        time.sleep(0.5)
        c.js("""(()=>{const s=$('#sgMultiMode'); s.value='manual';
          s.dispatchEvent(new Event('change',{bubbles:true})); return s.value;})()""")
        # 等 /api/multi/plan 回来：40 句的分句要走一次磁盘
        rows = 0
        for _ in range(40):
            time.sleep(0.5)
            rows = c.js("document.querySelectorAll('#sgLines .mline').length") or 0
            if rows:
                break
        print("分句行数：%s  lyric_ready=%s"
              % (rows, c.js("!!(SG_PLAN && SG_PLAN.lyric_ready)")))
        if not rows:
            raise RuntimeError("逐句面板没画出东西")

        # 排一个能看出门道的样子：前 10 句两人轮流（各 5 句）+ 11~12 句合唱。
        # 只排开头两句的话汇总会显示"云宝 3 秒"，看着像这功能不能用。
        c.js("""(()=>{
          const rows=Array.from(document.querySelectorAll('#sgLines .mline'));
          const pens=Array.from(document.querySelectorAll('#sgPens .pen'));
          const cho=pens[pens.length-1];
          rows.slice(0,10).forEach((r,i)=>{ (i%2?pens[1]:pens[0]).click(); r.click(); });
          cho.click();
          rows.slice(10,12).forEach(r=>r.click());
          pens[0].click();
          return 1;})()""")
        time.sleep(1.5)
        # 那条「服务是旧进程」的提醒条在这种自起自用的场合必然出现，
        # 它跟这次要看的东西无关（它自己在别的门禁里有判据）
        c.js("(()=>{const b=$('#staleBar'); if(b) b.remove(); return 1;})()")
        # 左下角那叠任务卡是历史任务（比如上次被中断的下载），会挡住角色区 ——
        # 截图要看的是「逐句面板」，不是任务队列，收起来
        c.js("""(()=>{const j=$('#jobs');
          if(j) j.style.display='none'; return 1;})()""")
        c.js("window.scrollTo({top:0,behavior:'instant'})")
        time.sleep(0.6)
        print("逐句安排：%s" % c.js("JSON.stringify(sgManual())"))
        print("汇总：%s" % (c.js("$('#sgPlanSum').textContent") or ""))
        shot = c.send("Page.captureScreenshot", format="png")
        with open(OUT, "wb") as f:
            f.write(base64.b64decode(shot["data"]))
        print("已出图：%s  %.0f KB" % (OUT, os.path.getsize(OUT) / 1024.0))
        return 0
    finally:
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(chrome.pid)],
                           capture_output=True)
        except Exception:
            pass
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(svc.pid)],
                           capture_output=True)
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
