# -*- coding: utf-8 -*-
r"""真浏览器验证「大跨度变调提醒」并在 Command 显示为红色（一次性工具，跑完删）。

为什么必须真跑浏览器：静态检查只能证明字串在源码里，证明不了它真的会显示、
也没法证明 JS 没报错。这一条提醒的价值全在于**用户拉到 ±12 的那一瞬间能不能看见**，
所以它必须在真 DOM 里验一次。

连已经在跑的服务（默认 8756）—— 本工具不自己起服务。
★ 记住：工具里起的常驻进程活不过一条 Bash 命令，所以 chrome 的起、操作、关
  必须全在这同一条命令里完成（见 env-notes）。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("VOICESTUDIO_PORT") or 8756)
CDP_PORT = int(os.environ.get("SHOT_CDP") or 9347)
PROFILE = os.path.join(HERE, ".cdp-pitch")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
URL = "http://127.0.0.1:%d/" % PORT
OUT = os.path.join(HERE, "data", "_preview", "pitch_warn.png")


def svc_ok():
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/health" % PORT)
        req.add_header("User-Agent", "vs")
        with urllib.request.urlopen(req, timeout=4) as r:
            return bool(json.loads(r.read().decode()).get("ok"))
    except Exception:
        return False


def main():
    if not svc_ok():
        print("服务没在 %d 上跑 —— 先启动VoiceStudio.bat" % PORT)
        return 1
    try:
        import websocket  # noqa: F401
    except ImportError:
        print("要用项目 venv 跑（websocket 只装在那边）")
        return 1

    os.system("rm -rf %s" % PROFILE)
    os.makedirs(PROFILE, exist_ok=True)
    clog = os.path.join(HERE, "_tmp", "shot_pitch_chrome.log")
    os.makedirs(os.path.dirname(clog), exist_ok=True)
    cf = open(clog, "wb")
    chrome = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--mute-audio",
         "--remote-debugging-port=%d" % CDP_PORT,
         "--user-data-dir=%s" % PROFILE, "--no-first-run",
         "--remote-allow-origins=*", "--window-size=1500,1000", URL],
        stdout=cf, stderr=subprocess.STDOUT)

    ws = None
    tabs = None
    for _ in range(40):
        time.sleep(0.6)
        if chrome.poll() is not None:
            break
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/json" % CDP_PORT, timeout=3) as r:
                tabs = [t for t in json.loads(r.read().decode())
                        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            if tabs:
                break
        except Exception:
            pass
    if not tabs:
        tail = open(clog, "rb").read().decode("utf-8", "replace")[-500:]
        print("浏览器没起来：chrome 退出码=%s\n%s" % (chrome.poll(), tail))
        return 1

    ws = websocket.create_connection(tabs[0]["webSocketDebuggerUrl"], timeout=40)
    mid = [0]

    def js(expr, wait=0.8):
        time.sleep(wait)
        mid[0] += 1
        ws.send(json.dumps({"id": mid[0], "method": "Runtime.evaluate",
                            "params": {"expression": expr,
                                       "returnByValue": True,
                                       "awaitPromise": True}}))
        for _ in range(120):
            msg = json.loads(ws.recv())
            if msg.get("id") == mid[0]:
                if "result" in msg and msg["result"].get("exceptionDetails"):
                    return {"error": str(msg["result"]["exceptionDetails"])}
                try:
                    return msg["result"]["result"].get("value")
                except Exception:
                    return None
        return None

    js("1", 2.0)
    print("页面标题：%s" % js("document.title"))

    # ---- 先把两处控件拉到 ±12，看警告有没有真的出来 ----
    # 注意：这是真 DOM 操作（改值 + 派事件），跟用户用鼠标拉滑块是同一条路径。
    res = js("""(() => {
      const out = {};
      const sel = document.querySelector('#selPitch');
      if (sel) {
        sel.value = '12';
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        const w = document.querySelector('#pitchWarn');
        out.role = {txt: (w && w.textContent || '').slice(0, 90),
                    color: w ? (getComputedStyle(w).color) : null,
                    shown: w ? (w.offsetParent !== null && w.style.display !== 'none') : null};
      } else out.role = null;
      const sg = document.querySelector('#sgPitch');
      if (sg) {
        sg.value = '12';
        sg.dispatchEvent(new Event('input', {bubbles: true}));
        const wb = document.querySelector('#sgPitchWarnBox');
        out.stage = {txt: (wb && wb.textContent || '').slice(0, 90),
                     color: wb ? (getComputedStyle(wb).color) : null,
                     shown: wb ? (wb.offsetParent !== null && wb.style.display !== 'none') : null};
      } else out.stage = null;
      // 中间档：±6 应该只给黄牌，不是红牌
      if (sel) {
        sel.value = 'custom';
        sel.dispatchEvent(new Event('change', {bubbles: true}));
        const inp = document.querySelector('#inpPitch');
        if (inp) { inp.value = '7'; inp.dispatchEvent(new Event('input', {bubbles:true})); }
        const w2 = document.querySelector('#pitchWarn');
        out.mid7 = (w2 && w2.textContent || '').slice(0, 60);
      }
      return out;
    })()""", 1.5)
    print("控件状态：\n%s" % json.dumps(res, ensure_ascii=False, indent=1))

    # ---- 回到 ±12 并截图 ----
    js("""(() => {
      const sel = document.querySelector('#selPitch');
      if (sel) { sel.value = '12'; sel.dispatchEvent(new Event('change', {bubbles:true})); }
      const el = document.querySelector('#pitchWarn');
      if (el && el.scrollIntoView) el.scrollIntoView({block: 'center'});
      const b = document.querySelector('#staleBar'); if (b) b.remove();
      return 1;
    })()""", 1.0)
    mid[0] += 1
    ws.send(json.dumps({"id": mid[0], "method": "Page.captureScreenshot",
                        "params": {"format": "png"}}))
    shot = None
    for _ in range(180):
        msg = json.loads(ws.recv())
        if msg.get("id") == mid[0] and "result" in msg:
            shot = msg["result"].get("data")
            break
    if not shot:
        print("截图失败")
        return 1
    import base64
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "wb") as f:
        f.write(base64.b64decode(shot))
    print("=> %s (%.0f KB)" % (OUT, os.path.getsize(OUT) / 1024.0))

    ws.close()
    chrome.terminate()
    try:
        chrome.wait(timeout=10)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
