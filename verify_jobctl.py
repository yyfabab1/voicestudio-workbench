r"""第九关：任务控制（暂停/取消）与「删除之前训好的模型」的真浏览器验收。

为什么要单独一关：
  暂停只解决"腾出显卡"，取消才是"我不要了"。两者按钮挨在一起、长得也像，
  一旦取消没真的杀掉进程，用户会看着一个"已取消"的任务继续占着显卡 ——
  而这类错误在后端日志里是**安静**的。
  同理，"删掉之前训好的模型"必须能删、且删完角色要如实变回"还没训练"，
  否则就是本机已经发生过的那次事故：文件没了、卡片还写着已训练、一点就崩。

覆盖：
  1. 取消真杀进程（运行中的、暂停中的两种），状态是 cancelled 不是 failed
  2. cleanup=True 清中间产物，但 job.json / log.txt 留着（还能回查）
  3. 负对照：不取消的任务照常跑完；已结束/不存在的任务取消被拒；不会杀掉自己
  4. 强制删除：默认删不动「正在用的模型」，开 force 能删且**角色引用同步断掉**
  5. 负对照：底模开了 force 也删不掉
  6. 真浏览器：任务卡 ✕ → 确认框 → 选「取消」不动手、选「取消任务」真生效
  7. 真浏览器：存储面板 🔓 开关只放开「正在用的模型」，底模永远锁着
  8. 真浏览器：悬空引用（模型文件没了但角色还记着）会当面说出来

用法：<venv>/Scripts/python.exe verify_jobctl.py
"""
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio                                    # noqa: E402

PORT = 9339
PROFILE = os.path.join(HERE, ".chrome-jobctl")
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


# ---------------------------------------------------------------- 后端


def alive(pid):
    """这个 pid 还活着吗。"""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except Exception:
            return False
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k.GetExitCodeProcess.restype = wintypes.BOOL
    h = k.OpenProcess(0x1000, False, int(pid))     # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = wintypes.DWORD()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code))
    k.CloseHandle(h)
    return (not ok) or code.value == 259           # 259 = STILL_ACTIVE


def spawn(sleep_sec=60, tag=""):
    """起一个会跑 sleep_sec 秒的真实任务（走和工作任务完全同一条 run_in_thread）。"""
    job = studio.new_job(None, "probe", "取消探针" + tag)
    cmd = '"%s" -c "import time;time.sleep(%d)"' % (sys.executable, sleep_sec)
    studio.run_in_thread(job, lambda: studio.run(job, cmd, timeout=sleep_sec + 60))
    time.sleep(3.0)
    return job


def pids_of(job):
    c = studio._ctrl(job)
    return sorted(set((c.get("pids") or []) + (c.get("seen_pids") or [])))


def section_cancel():
    print("\n=== 1. 取消：真的终止进程 ===")
    j1 = spawn(60, "·运行中")
    p1 = pids_of(j1)
    check("任务起出了子进程", len(p1) > 0, "pids=%s" % p1)
    check("取消前这些进程都活着", all(alive(p) for p in p1), p1)
    ok, msg = studio.cancel_job(j1["id"])
    check("取消接口返回成功", ok, msg)
    time.sleep(2.5)
    check("取消后进程**真的没了**（不是界面上不动了）",
          not any(alive(p) for p in p1), p1)
    check("状态是 cancelled，不是 failed（取消不是失败）",
          j1.get("state") == "cancelled", "state=%s" % j1.get("state"))
    try:
        lg = open(j1["_logfile"], encoding="utf-8", errors="replace").read()
    except Exception:
        lg = ""
    check("日志里留了取消记录", "已取消" in lg,
          (lg.strip().splitlines() or [""])[-1][:60])
    check("步骤清单里没有卡在 doing 的那一步",
          not any(s.get("state") == "doing" for s in (j1.get("steps") or [])),
          [s.get("state") for s in (j1.get("steps") or [])])

    print("\n=== 2. 暂停中也能取消 ===")
    j2 = spawn(60, "·暂停中")
    p2 = pids_of(j2)
    okp, msgp = studio.pause_job(j2["id"])
    time.sleep(1.0)
    check("先暂停成功", okp, msgp)
    ok2, msg2 = studio.cancel_job(j2["id"])
    check("暂停中照样能取消", ok2, msg2)
    time.sleep(2.5)
    check("挂起的进程也被杀掉了", not any(alive(p) for p in p2), p2)
    check("取消后 paused 标记被清掉", not j2.get("paused"),
          "paused=%s" % j2.get("paused"))
    check("状态是 cancelled", j2.get("state") == "cancelled",
          "state=%s" % j2.get("state"))

    print("\n=== 3. cleanup=True：清中间产物，但留证据 ===")
    j3 = spawn(60, "·清产物")
    work = os.path.join(j3["dir"], "work")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, "big.bin"), "wb") as f:
        f.write(b"\0" * (2 * 1024 * 1024))
    studio.cancel_job(j3["id"], cleanup=True)
    # ★ 原来是固定 `sleep(3.0)`：清理发生在任务线程收尾里，要等"杀进程 → run()
    #   返回 → wrapper 删目录"整条链走完。实测这一步常常要 3.7s 左右 ⇒ 卡在阈值
    #   边缘，于是同一份代码时红时绿，看起来像"清理坏了"，其实只是没等够。
    #   改成轮询等（判据一字未改：目录必须真的没了）。
    _gone = False
    for _ in range(40):
        time.sleep(0.25)
        if not os.path.isdir(work):
            _gone = True
            break
    check("中间产物被清掉", _gone, work)
    check("job.json 留着（事后还能查这个任务）",
          os.path.isfile(os.path.join(j3["dir"], "job.json")))
    check("log.txt 留着", os.path.isfile(j1["_logfile"]))

    print("\n=== 4. 负对照：不取消的任务照常跑完 ===")
    j4 = spawn(2, "·跑完")
    time.sleep(6.0)
    check("没被取消的任务正常完成", j4.get("state") == "done",
          "state=%s" % j4.get("state"))
    check("它跑完时进程自然退出", not any(alive(p) for p in pids_of(j4)), pids_of(j4))

    print("\n=== 5. 负对照：不该被取消的，取消不了 ===")
    ok5, msg5 = studio.cancel_job(j4["id"])
    check("对已完成的任务取消 → 被拒", not ok5, msg5)
    ok6, msg6 = studio.cancel_job("j_not_exist")
    check("对不存在的任务取消 → 被拒", not ok6, msg6)
    # ★ 这条判据要小心：直接在**本进程**上调 _kill_pids([自己的pid]) 看似无害，
    #   但它会先把进程树展开 —— 本进程的子进程（我们刚起的 Chrome）会被一起杀掉
    #   （实测 killed=8，浏览器当场没了，后面的界面判据就只能对着空气验）。
    #   所以改在**独立子进程**里验：那个进程没有子进程，副作用关得住。
    me = os.getpid()
    check("进程树展开里包含自己（说明「排除自己」这一步确实会被走到）",
          me in studio._tree_pids(me), "pid=%s" % me)
    check("空 pid 列表不会误杀任何进程", studio._kill_pids([]) == 0)
    code = ("import sys;sys.path.insert(0,%r);import studio,os;"
            "studio._kill_pids([os.getpid()]);print('ALIVE')" % HERE)
    try:
        pr = subprocess.run([sys.executable, "-c", code], capture_output=True,
                            text=True, timeout=90)
        out = (pr.stdout or "") + (pr.stderr or "")
    except Exception as e:
        out = "EXC:%s" % e
    check("_kill_pids(自己的 pid) 不会把自己杀掉（服务不会自杀）",
          "ALIVE" in out, out.strip()[-120:])

    for j in (j1, j2, j3, j4):
        try:
            import shutil
            shutil.rmtree(j["dir"], ignore_errors=True)
        except Exception:
            pass


def section_force():
    print("\n=== 6. 删除「正在用的模型」：默认锁、开 force 才能删 ===")
    WD = studio._rvc_weights_dir()
    GD = studio.GPT_WEIGHT_DIR
    EXP = "_jctlprobe"
    PROBE_PTH = os.path.join(WD, EXP + ".pth")
    PROBE_CKPT = os.path.join(GD, EXP + "-e5.ckpt")
    role = None
    try:
        role = studio.create_role(name="_jctlprobe(探针)")
        rid = role["id"]
        for p in (PROBE_PTH, PROBE_CKPT):
            with open(p, "wb") as f:
                f.write(b"\0" * (512 * 1024))
        studio.mutate_role(rid, lambda r: r.__setitem__(
            "rvc", {"model": EXP + ".pth", "epochs": 5, "at": studio._now(),
                    "weight_epoch": 5, "pretrained": True, "index": False}))
        # ★ 说话模型记在 r["model"]["ckpt"]，而且是**带目录的相对路径** ——
        #   这条以前读成了 r["gsv"]["weight"]，保护压根没生效，
        #   于是本机真实发生过一次"角色在用的 ckpt 被当成旧存档删掉"。
        studio.mutate_role(rid, lambda r: r.__setitem__(
            "model", {"ckpt": "GPT_weights_v5turbo/" + EXP + "-e5.ckpt",
                      "epochs": 5, "at": studio._now()}))
        prot = studio._protected_map()
        pn = set(studio._nc(k) for k in prot)
        check("歌声那份被锁住", studio._nc(PROBE_PTH) in pn)
        check("说话那份也被锁住（这条以前是漏的）", studio._nc(PROBE_CKPT) in pn)
        check("两把锁的类型都是 role（所以允许被强制解锁）",
              studio._lock_kind(PROBE_PTH, prot) == "role"
              and studio._lock_kind(PROBE_CKPT, prot) == "role",
              "%s / %s" % (studio._lock_kind(PROBE_PTH, prot),
                           studio._lock_kind(PROBE_CKPT, prot)))

        r = studio.storage_delete([PROBE_PTH, PROBE_CKPT])
        check("负对照：不开 force 时一件都删不掉",
              len(r["deleted"]) == 0 and len(r["failed"]) == 2,
              "deleted=%d failed=%d" % (len(r["deleted"]), len(r["failed"])))
        check("负对照：文件确实还在",
              os.path.isfile(PROBE_PTH) and os.path.isfile(PROBE_CKPT))
        check("拒绝原因写清了是谁在用",
              all("正在用" in (f.get("why") or "") for f in r["failed"]),
              [f.get("why") for f in r["failed"]])

        r2 = studio.storage_delete([PROBE_PTH, PROBE_CKPT], force=True)
        check("开 force 后能删掉", len(r2["deleted"]) == 2 and len(r2["failed"]) == 0,
              "deleted=%d failed=%d" % (len(r2["deleted"]), len(r2["failed"])))
        check("文件真的没了",
              not os.path.exists(PROBE_PTH) and not os.path.exists(PROBE_CKPT))
        check("返回了「删完要重训」的提示（代价必须当场说清）",
              len(r2.get("refs") or []) == 2, r2.get("refs"))
        after = studio.get_role(rid)
        check("角色记录里的歌声模型引用被同步断掉",
              not ((after.get("rvc") or {}).get("model")), after.get("rvc"))
        check("角色记录里的说话模型引用被同步断掉",
              not after.get("model"), after.get("model"))
        check("悬空引用检测里不再有这个探针",
              not any(EXP in w for w in studio._dangling_refs()))
    finally:
        for p in (PROBE_PTH, PROBE_CKPT):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        if role:
            try:
                studio.delete_role(role["id"])
            except Exception as e:
                print("  [warn] 探针角色没删干净：%s" % e)

    print("\n=== 7. 负对照：底模开了 force 也删不掉 ===")
    gp, _dp = studio.rvc_pretrained_paths("v1", "40k")
    if os.path.isfile(gp):
        r3 = studio.storage_delete([gp], force=True)
        check("底模即使 force=True 也删不掉",
              len(r3["deleted"]) == 0 and len(r3["failed"]) == 1,
              [f.get("why") for f in r3["failed"]])
        check("底模文件还在", os.path.isfile(gp))
    else:
        check("底模文件存在（否则底模锁这条无从验证）", False, gp)


def section_warnings():
    print("\n=== 8. 悬空引用检测 ===")
    w = studio._dangling_refs()
    check("_dangling_refs 返回的是列表", isinstance(w, list), type(w).__name__)
    print("     当前真实悬空引用：%s" % (w or "无"))


# ---------------------------------------------------------------- 浏览器


def cdp_get(path):
    return json.load(urllib.request.urlopen(
        "http://127.0.0.1:%d%s" % (PORT, path), timeout=5))


class Cdp:
    def __init__(self, ws_url):
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.i = 0
        self.events = []

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})
            self.events.append(m)

    def js_errors(self):
        out = []
        for m in self.events:
            if m.get("method") == "Runtime.exceptionThrown":
                d = m["params"].get("exceptionDetails", {})
                out.append("%s @ %s:%s" % (
                    (d.get("exception") or {}).get("description") or d.get("text"),
                    d.get("url"), d.get("lineNumber")))
        return out

    def js(self, expr, awaitp=False):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=awaitp,
                      returnByValue=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError("%s @ %s:%s" % (
                (d.get("exception") or {}).get("description") or d.get("text"),
                d.get("url"), d.get("lineNumber")))
        return r.get("result", {}).get("value")

    def mouse(self, x, y, typ="mousePressed"):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                  button="left", clickCount=1)

    def box(self, sel):
        return self.js("""(()=>{
          const el=document.querySelector(%s);
          if(!el) return {err:'no element'};
          el.scrollIntoView({block:'center',inline:'center'});
          const r=el.getBoundingClientRect();
          const x=r.left+r.width/2, y=r.top+r.height/2;
          const hit=document.elementFromPoint(x,y);
          return {x:x,y:y,w:r.width,h:r.height,vw:innerWidth,vh:innerHeight,
                  inView:(r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth),
                  hitIsSelf:!!(hit&&(hit===el||el.contains(hit)||hit.contains(el)))};
        })()""" % json.dumps(sel))

    def click(self, sel, label=""):
        """真鼠标点：先量几何，视口外直接判红（真鼠标必然打空）。"""
        b = self.box(sel)
        self.last = b
        if b.get("err"):
            check("能点到 %s" % (label or sel), False, b["err"])
            return None
        if not b["inView"]:
            check("能点到 %s" % (label or sel), False,
                  "几何 rect=%sx%s 视口=%sx%s → 在视口外"
                  % (b["w"], b["h"], b["vw"], b["vh"]))
            return None
        if not b.get("hitIsSelf"):
            check("能点到 %s" % (label or sel), False,
                  "命中测试打到了别的东西上（被盖住了）")
            return None
        self.mouse(b["x"], b["y"], "mousePressed")
        time.sleep(0.06)
        self.mouse(b["x"], b["y"], "mouseReleased")
        time.sleep(0.25)
        return True


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


def api_post(path, payload):
    req = urllib.request.Request("http://127.0.0.1:8756" + path,
                                 data=json.dumps(payload).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def api_get(path):
    return json.load(urllib.request.urlopen(
        "http://127.0.0.1:8756" + path, timeout=30))


def section_ui(page):
    print("\n=== 9. 真浏览器：任务卡上的「取消」===")
    # ★ 这一段的前提是"有一个能唱歌的角色"。没有就读不到可跑的任务，
    #   后面的按钮、确认框、cancelled 状态全都是对着空气验 —— 那就如实跳过，
    #   **不**把它算成 PASS（SKIP 就是 SKIP，冒充绿最害人）。
    song = os.path.join(HERE, "data", "uploads",
                        "嘟比Dubi - 神耀（星际争霸同人歌）.mp3")
    rid = None
    try:
        roles = api_get("/api/roles")["roles"]
        for r in roles:
            if (r.get("rvc") or {}).get("model") and os.path.isfile(
                    os.path.join(studio._rvc_weights_dir(), r["rvc"]["model"])):
                rid = r["id"]
                break
    except Exception as e:
        roles = []
        print("     读角色列表失败：%s" % e)
    if not rid:
        print("     SKIP：库里没有「已绑歌声模型、且模型文件还在」的角色 ——")
        print("     任务卡的取消验证需要它来起一个真任务。等角色训好后重跑这一关。")
        return
    if not os.path.isfile(song):
        check("能找到用来起任务的歌曲", False, song)
        return
    # 45 秒的小样：够我们走完"点开 → 先选取消（负对照）→ 再真的取消"这一串动作。
    # 用 10 秒的话任务会在按钮还没点完之前就跑完了，判据就变成了"对一个
    # 已经结束的任务点取消"—— 看起来绿，其实什么都没验到。
    res = api_post("/api/jobs", {"kind": "sing", "role": rid,
                                 "song": song, "preview_secs": 45,
                                 "pitch": 0, "quality": "fast"})
    jid = ((res.get("job") or {}).get("id"))
    check("起出了一个真实任务（45 秒试听小样）", bool(jid), res.get("job", {}).get("title"))
    if not jid:
        return
    # 刷新页面：restoreJobs 会把服务端正在跑的任务挂到界面上
    page.send("Page.navigate", url=URL)
    time.sleep(5.0)
    # ★ 这条同时盯着一件更要紧的事：刷新页面之后，正在跑的任务**必须自己回到
    #   界面上**（靠 restoreJobs 把服务端 running 的任务接回来）。
    #   以前 restoreJobs 只定义、从没被调用，于是刷新一下任务就"消失"了，
    #   界面上连取消按钮都没有 —— 用户只能干看着显卡转。
    got = poll(page, "!!document.querySelector('#jcx-%s') && "
                     "getComputedStyle(document.querySelector('#jcx-%s')).display!=='none'"
               % (jid, jid), True, secs=30)
    check("刷新页面后任务卡自动回来了，✕ 取消按钮可见", got,
          "页面里任务卡数=%s，服务端状态=%s"
          % (page.js("document.querySelectorAll('#jobs .jobbox').length"),
             api_get("/api/job/" + jid)["job"]["state"]))

    # ---- 负对照：点 ✕ 之后选「取消」—— 任务必须还在跑 ----
    if got:
        before = api_get("/api/job/" + jid)["job"]["state"]
        check("开始点之前这个任务确实还在跑（否则下面几条都是在验空气）",
              before in ("running", "queued"), "state=%s" % before)
        page.click("#jcx-" + jid, "任务卡的 ✕")
        check("点了 ✕ 弹出确认框（不是直接就干掉）",
              bool(page.js("document.querySelector('#maskConfirm').classList.contains('on')")))
        body = page.js("document.querySelector('#cfBody').textContent") or ""
        check("确认框里说明了「取消」和「暂停」的区别（否则用户会以为只是停一下）",
              ("取消" in body and "暂停" in body), body.strip()[:80].replace("\n", " / "))
        page.click("#cfNo", "确认框的「取消」")
        time.sleep(1.2)
        check("负对照：选了「取消」之后任务**还在跑**（没被误杀）",
              api_get("/api/job/" + jid)["job"]["state"] == before,
              "取消前=%s 现在=%s" % (before, api_get("/api/job/" + jid)["job"]["state"]))

        # ---- 真取消 ----
        page.click("#jcx-" + jid, "任务卡的 ✕")
        check("第二次点 ✕ 确认框又出来了",
              bool(page.js("document.querySelector('#maskConfirm').classList.contains('on')")))
        page.click("#cfYes", "确认框的「取消任务」")
        cancelled = False
        t0 = time.time()
        while time.time() - t0 < 25:
            try:
                if api_get("/api/job/" + jid)["job"]["state"] == "cancelled":
                    cancelled = True
                    break
            except Exception:
                pass
            time.sleep(0.6)
        check("点「取消任务」之后，服务端这个任务真的成了 cancelled", cancelled,
              "state=%s" % (api_get("/api/job/" + jid)["job"]["state"],))
        gone = poll(page, "(()=>{const b=document.querySelector('#jcx-%s');"
                          "return !b || getComputedStyle(b).display==='none'})()" % jid,
                    True, secs=15)
        check("取消之后按钮自己收起来了（不能让人对着死任务再点一次）", gone)
        txt = poll(page, "(()=>{const s=document.querySelector('#js-%s');"
                         "return s && s.textContent.indexOf('已取消')>=0})()" % jid,
                   True, secs=15)
        check("状态文字变成「已取消」，而不是「失败」", txt,
              page.js("(document.querySelector('#js-%s')||{}).textContent" % jid))

    print("\n=== 10. 真浏览器：存储面板的 🔓 解锁开关 ===")
    page.click("#btnStore", "顶栏的「存储」")
    check("存储面板打开了",
          bool(page.js("document.querySelector('#maskStore').classList.contains('on')")))
    # ★ 判据里不能写 window.STORE：STORE 是 let 声明的，它不挂在 window 上，
    #   `window.STORE` 永远 undefined ⇒ 这条会**永远绿不了**（假红）。
    #   用 typeof 直接在全局词法作用域里取。
    ready = poll(page, "(typeof STORE!=='undefined') && !!STORE && !!STORE.groups",
                 True, secs=120)
    check("扫描完成了（最多等 90 秒）", ready)
    if not ready:
        return
    n_all = page.js("document.querySelectorAll('#storeList .store-it').length")
    check("列表里真的有东西", bool(n_all), "%s 行" % n_all)
    n_locked = page.js("document.querySelectorAll('#storeList .store-it.locked').length")
    check("「正在用的模型」默认是锁着的（带 .locked）", n_locked > 0, "锁住 %s 行" % n_locked)
    check("默认状态下这些锁着的格子勾不动",
          page.js("Array.from(document.querySelectorAll('#storeList .store-it.locked'))"
                  ".every(r=>r.querySelector('input[type=checkbox]').disabled)"))
    # 底模：无论开不开 🔓 都要锁着
    n_pre = page.js("""(()=>{
      const gs=Array.from(document.querySelectorAll('#storeList .store-g'));
      const g=gs.find(x=>x.dataset.g==='pretrained');
      return g? g.querySelectorAll('.store-it input[type=checkbox]:not(:disabled)').length : -1;
    })()""")
    check("底模那一行现在一个都勾不动", n_pre == 0, "可勾 %s 个" % n_pre)

    page.js("""(()=>{const c=document.querySelector('#stForce');
      c.checked=true; c.dispatchEvent(new Event('change')); return true})()""")
    time.sleep(0.8)
    n_unlocked = page.js("document.querySelectorAll('#storeList .store-it.unlocked').length")
    check("打开 🔓 之后，「正在用的模型」变成了可勾（.unlocked）", n_unlocked > 0,
          "解锁 %s 行" % n_unlocked)
    check("解锁后这些格子真的能勾了",
          page.js("Array.from(document.querySelectorAll('#storeList .store-it.unlocked'))"
                  ".every(r=>!r.querySelector('input[type=checkbox]').disabled)"))
    check("开了 🔓 之后底模**依然**一个都勾不动（这条必须是硬的）",
          page.js("""(()=>{
            const g=Array.from(document.querySelectorAll('#storeList .store-g'))
                     .find(x=>x.dataset.g==='pretrained');
            return g? g.querySelectorAll('.store-it input[type=checkbox]:not(:disabled)').length===0 : false;
          })()"""))
    tip = page.js("document.querySelector('#storeTip').textContent") or ""
    check("🔓 打开时把代价写在了提示里（删了要重训）",
          "重训" in tip, tip.strip()[:70])

    page.js("""(()=>{const c=document.querySelector('#stForce');
      c.checked=false; c.dispatchEvent(new Event('change')); return true})()""")
    time.sleep(0.8)
    check("🔓 关掉之后，那些格子重新锁回去",
          page.js("document.querySelectorAll('#storeList .store-it.unlocked').length") == 0)

    print("\n=== 11. 真浏览器：悬空引用会当面说出来 ===")
    # ★ 判据**自足**：以前这条依赖"库里恰好有悬空引用"（实际是 storage 关的探针
    #   删了一半留下的）—— 那个残留一被清掉，if 分支就走不到，等于没验。
    #   现在自己种一个：角色记着"我有模型"，但那个模型文件根本不存在。
    #   ★ 等待式断言：#storeWarn 是前端 fetch /api/store 才填的，读早了是空 ——
    #     实测同一份代码一次红一次绿（间歇红 = 时序竞态）。所以要**重新打开面板**
    #     触发一次新扫描，然后 poll 等 #storeWarn 非空，等不到才红。
    role = studio.create_role(name="_dangling(探针)")
    rid = role["id"]
    try:
        studio.mutate_role(rid, lambda r: r.__setitem__(
            "rvc", {"model": "_dangling_probe_不存在.pth", "epochs": 1,
                    "at": studio._now()}))
        check("探针悬空引用已种下（判据前提成立，不是空转）",
              any("_dangling_probe" in w for w in studio._dangling_refs()))
        page.js("closeMask('maskStore')")
        page.click("#btnStore", "顶栏的「存储」")
        ok = poll(page, """(typeof STORE!=='undefined') && !!STORE && !!STORE.groups
                  && !!(document.querySelector('#storeWarn').textContent.trim())""",
                  want=True, secs=30)
        wn = page.js("document.querySelector('#storeWarn').textContent") or ""
        check("角色记着、但文件已经不在的模型，界面上喊出来了",
              ok and "_dangling_probe" in wn and "重训" in wn,
              ("等了 30s #storeWarn 还是空的（fetch 没回来或前端没渲染）" if not ok
               else wn.strip()[:100]))
    finally:
        studio.delete_role(rid)
        check("探针清干净了（不给用户库留悬空引用）",
              not any("_dangling_probe" in w for w in studio._dangling_refs()))
    page.js("closeMask('maskStore')")


def main():
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT,
        "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000",
        # ★ 这三个不加的话，页面在后台待一会儿就被 Chrome 节流/冻结，
        #   CDP 连接会**静默断开**（表现为 recv 抛 ConnectionClosed）。
        #   实测：后端几段跑了一分多钟后回来点页面，连接已经没了。
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
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

        print("\n=== 0. 页面加载 ===")
        errs = page.js_errors()
        check("加载期间没有 JS 异常", not errs, ("; ".join(errs))[:240] or "干净")
        check("新增的函数都在（askConfirm / askCancel / storeCanPick）",
              page.js("['askConfirm','askCancel','storeCanPick','storeUnlockable',"
                      "'storeSelected'].every(k=>typeof window[k]==='function')"))

        # ★ 浏览器那段必须**先跑**：后端几段要花一分多钟（真的在起进程、
        #   真的在等它们被杀掉），这段时间页面一直在后台 —— 等回来再点，
        #   CDP 连接已经断了（实测就是这么炸的）。
        section_ui(page)
        section_cancel()
        section_force()
        section_warnings()

        errs2 = page.js_errors()
        check("全程没有 JS 异常", not errs2, ("; ".join(errs2))[:240] or "干净")
    finally:
        try:
            if page:
                page.ws.close()
        except Exception:
            pass
        proc.kill()

    print("\n" + "=" * 60)
    print("PASS %d  FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        for b in BAD:
            print("  红：" + b)
    print("EXIT=%d" % (1 if BAD else 0))
    sys.exit(1 if BAD else 0)


if __name__ == "__main__":
    main()
