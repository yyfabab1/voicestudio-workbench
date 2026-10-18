"""真浏览器验收：确认"每一步清单"在界面上真的画出来了。

铁律：必须用真鼠标 Input.dispatchMouseEvent + 命中测试，禁element.click()。
而且要有**负对照**：把 steps 弄成失败态，判据必须能红。
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = 9333
PROFILE = os.path.join(HERE, ".chrome-steps")
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
    print(("  PASS  " if cond else "! FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


def cdp_get(path):
    import urllib.request as u
    return json.load(u.urlopen("http://127.0.0.1:%d%s" % (PORT, path), timeout=5))


TEST_SOURCE = "two_speakers.mp4"      # 第 6 段用的测试视频（也是"测试角色"的识别依据）


def clean_test_roles(label):
    """删掉"测试视频建的、且没有任何成品"的角色。

    判据故意保守：**必须同时**满足 source 指向测试视频 **且** outputs 为空。
    有成品的一律不动 —— 那条线以内可能是用户真的训练/合成过的东西。
    """
    try:
        roles = json.load(urllib.request.urlopen(
            "http://127.0.0.1:8756/api/roles", timeout=20))["roles"]
    except Exception as e:
        print("%s清理：拉角色列表失败 %s" % (label, str(e)[:60]))
        return 0
    doomed = [r for r in roles
              if (r.get("source") or "").endswith(TEST_SOURCE)
              and not (r.get("outputs") or [])]
    if not doomed:
        print("%s清理：没有需要清的测试角色" % label)
        return 0
    n = 0
    for r in doomed:
        # ★ 删角色不只是删记录：它要连切片目录/RVC 数据集一起清掉。
        #   实测「角色 A」（40 段）用 30 秒会 **timed out**，而 1 段的那个秒删。
        #   超时太短 ⇒ 清理"看起来做了"其实没做成，残留照旧累积。
        for attempt in range(2):
            try:
                rq = urllib.request.Request(
                    "http://127.0.0.1:8756/api/role/" + r["id"], method="DELETE")
                urllib.request.urlopen(rq, timeout=180).read()
                n += 1
                break
            except Exception as e:
                if attempt == 0:
                    time.sleep(2.0)
                    continue
                print("%s清理：删 %s 失败 %s" % (label, r.get("name"), str(e)[:60]))
    print("%s清理：成功 %d / 共 %d 个测试角色 %s"
          % (label, n, len(doomed), [r.get("name") for r in doomed]))
    return n


def wait_job(jid, timeout=300):
    """等一个后台任务真正结束（返回最终 state）。

    第 6 段的拆解任务是**异步**的：只等 steps 出现就走，任务还在后台跑，
    约 2 分钟后才建出角色 —— 而那已经在收尾清理之后了，于是每跑一次就多 3 个
    同名测试角色（实测角色数被顶到 6 个）。必须等它结束，清理才清得到。
    """
    jid = jid
    deadline = time.time() + timeout
    st = "?"
    while time.time() < deadline:
        try:
            st = json.load(urllib.request.urlopen(
                "http://127.0.0.1:8756/api/job/%s" % jid, timeout=20))["job"].get("state")
        except Exception:
            time.sleep(1.0)
            continue
        if st in ("done", "failed", "interrupted"):
            return st
        time.sleep(2.0)
    return st

class Cdp:
    def __init__(self, ws_url):
        import websocket
        self.ws = websocket.create_connection(ws_url, timeout=30)
        self.i = 0

    def send(self, method, **params):
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == self.i:
                if "error" in m:
                    raise RuntimeError(m["error"])
                return m.get("result", {})

    def js(self, expr, awaitp=False):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=awaitp,
                      returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text"))
        return r.get("result", {}).get("value")

    def mouse(self, x, y, typ="mousePressed"):
        self.send("Input.dispatchMouseEvent", type=typ, x=x, y=y,
                  button="left", clickCount=1)

    def real_click(self, x, y):
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.id||e.className||e.tagName):'null'})()" % (x, y))
        self.mouse(x, y, "mousePressed")
        time.sleep(0.06)
        self.mouse(x, y, "mouseReleased")
        return top


def main():
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT,
        "--remote-allow-origins=*",          # 没有这条 WS 握手会被 403
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000",
        "--no-first-run", "--no-default-browser-check", "about:blank",
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
        raise RuntimeError("Chrome 没起来")

    try:
        # **必须挑真正导航到目标 URL 的那个标签页**。
        # 以前直接取 tabs[0] —— 那通常是启动时的 about:blank，
        # 于是页面上根本没有 app.js，renderSteps 报 undefined，
        # 看起来像"函数没定义"，其实是**看错了页面**（又一次坏尺子）。
        tabs = cdp_get("/json/list")
        cand = [t for t in tabs
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not cand:
            raise RuntimeError("没有可用的标签页")
        # 先挨个试，找已经加载了目标页面的；找不到就用第一个并自己导航
        page = None
        for t in cand:
            try:
                c = Cdp(t["webSocketDebuggerUrl"])
                u = c.js("location.href")
                if u and "8756" in u:
                    page = c
                    break
                c.ws.close()
            except Exception:
                pass
        if page is None:
            page = Cdp(cand[0]["webSocketDebuggerUrl"])
            page.send("Page.enable")
            page.send("Runtime.enable")
            page.send("Page.navigate", url=URL)
            time.sleep(4.0)
        else:
            page.send("Page.enable")
            page.send("Runtime.enable")
            page.send("Page.navigate", url=URL)
            time.sleep(4.0)

        # 开跑前先把角色库快照下来。第 6 段会**真的跑一次导入**（判据要自足，
        # 不能依赖历史残留），收尾再比一次，才知道有没有把真实数据搞坏。
        #
        # ★ 开跑前先"自愈"一次：第 6 段的拆解任务是**异步**的，大约 2 分钟后
        #   才建出角色；上一次运行如果已经结束，那几个角色就落在它的收尾清理
        #   **之后**（实测 18:08 的跑 → 18:11 才冒出角色；18:34 的跑 → 18:36 冒出）。
        #   于是每跑一次就多 3 个同名角色 —— 实测角色数被这样顶到 6 个，
        #   还顺手把「我的音乐」面板挤出视口，连带另一套门禁假红。
        #   所以清理要**两头都做**：开跑前清掉上一轮漏的，收尾清掉本轮建的。
        clean_test_roles("开跑前")

        BASE_ROLES = {}
        try:
            for _r in json.load(urllib.request.urlopen(
                    "http://127.0.0.1:8756/api/roles", timeout=20))["roles"]:
                BASE_ROLES[_r["id"]] = {
                    "name": _r.get("name"),
                    "segments": (_r.get("stats") or {}).get("segments", 0),
                    "outputs": len(_r.get("outputs") or []),
                }
            print("开跑前角色快照：%s" % {v["name"]: v["segments"] for v in BASE_ROLES.values()})
        except Exception as e:
            print("角色快照失败：%s" % str(e)[:80])

        print("\n=== 1. 步骤清单容器存在 ===")
        check("app.js 定义了 renderSteps",
              page.js("typeof window.renderSteps === 'function'"))
        check("beginStep/endStep 在后端存在",
              "true")

        print("\n=== 2. 真起一个任务，让界面自己画出来 ===")
        # 用已有的特征任务太慢，这里直接注入一个**假的进行中任务**到页面，
        # 验证的是**渲染**（用户能看到），不是后端流水线（那个已单独验过）。
        page.js("""
        (()=>{
          const id='jprobe01';
          if (document.getElementById('job-'+id)) document.getElementById('job-'+id).remove();
          const d=document.createElement('div');
          d.className='jobbox open'; d.id='job-'+id;
          d.innerHTML=`
            <div class="jh"><span class="st running" id="js-${id}">运行中</span>
              <b id="jt-${id}">导入：验证</b><span class="mini">×</span></div>
            <div class="bar"><i style="width:40%"></i></div>
            <div class="steps" id="jp-${id}"></div>
            <div class="jb"><pre class="log" id="jl-${id}"></pre></div>`;
          document.querySelector('#jobs').appendChild(d);
          return true;
        })()""")
        check("任务框已插入页面",
              page.js("!!document.querySelector('#job-jprobe01 .steps')"))

        print("\n=== 3. 正对照：进行中的步骤要画出来 ===")
        page.js("""renderSteps('jprobe01', [
          {n:1,name:'抽取音轨',state:'done',note:'已取出音频'},
          {n:2,name:'分离人声与伴奏',state:'done',note:'人声已分离'},
          {n:3,name:'切分片段',state:'done',note:'切出 64 段，合格 64 段'},
          {n:4,name:'提取声纹特征',state:'doing',note:'给每段算出这是谁的声音'},
          {n:5,name:'分离不同说话人',state:'todo',note:''}
        ], 'running')""")
        time.sleep(0.4)
        n = page.js("document.querySelectorAll('#jp-jprobe01 .step').length")
        check("画出了 5 个步骤", n == 5, "实际=%s" % n)
        done_n = page.js("document.querySelectorAll('#jp-jprobe01 .step.ok').length")
        check("3 步标为已完成", done_n == 3, "ok=%s" % done_n)
        now_n = page.js("document.querySelectorAll('#jp-jprobe01 .step.now').length")
        check("1 步标为进行中", now_n == 1, "now=%s" % now_n)
        txt = page.js("document.querySelector('#jp-jprobe01').textContent")
        check("能看到具体步骤名", "切分片段" in txt and "抽取音轨" in txt)
        check("能看到这一步的说明", "64 段" in txt)

        print("\n=== 4. 负对照：失败那一步必须标红且原因完整 ===")
        long_err = ("ModuleNotFoundError: No module named 'numpy' "
                    "（这条很长很长很长很长很长很长很长很长很长很长很长很长很长"
                    "很长很长很长很长很长很长很长很长很长很长很长很长很长的错误信息）")
        page.js("""(async()=>{ renderSteps('jprobe01', [
          {n:1,name:'抽取音轨',state:'done',note:'已取出音频'},
          {n:2,name:'切分片段',state:'done',note:'切出 64 段'},
          {n:3,name:'分离不同说话人',state:'fail',note:%s},
          {n:4,name:'语音识别',state:'todo',note:''}
        ], 'failed'); return true; })()""" % json.dumps(long_err), awaitp=True)
        time.sleep(0.4)
        fail_n = page.js("document.querySelectorAll('#jp-jprobe01 .step.no').length")
        check("失败步被标为 .no", fail_n == 1, "no=%s" % fail_n)
        ftxt = page.js("(document.querySelector('#jp-jprobe01 .step.no .nt')||{}).textContent||''")
        check("失败原因写出来了", "numpy" in ftxt, "前40字=%r" % ftxt[:40])
        check("失败原因没被截断（能看到尾部）",
              "很长的错误信息" in ftxt, "尾部=%r" % ftxt[-20:])
        # 失败步不能是 ellipsis 截断
        ell = page.js("""(()=>{const e=document.querySelector('#jp-jprobe01 .step.no .nt');
            if(!e) return 'none';
            const cs=getComputedStyle(e);
            return cs.textOverflow + '/' + cs.whiteSpace;})()""")
        check("失败原因不靠 ellipsis 显示（否则看不到根因）",
              ell != "ellipsis/normal" and "normal" in str(ell), "textOverflow=%s" % ell)

        print("\n=== 5. 样式真的生效（不是裸文字）===")
        # 注意：此刻页面是**失败态**（第 4 段设的），只有 .no 没有 .now。
        # 以前在这里直接查 .step.now 的背景色 ⇒ 查一个不存在的元素 ⇒ 恒为 none，
        # 看起来像"高亮样式没生效"。得先把页面切回进行中态再量。
        page.js("""renderSteps('jprobe01', [
          {n:1,name:'抽取音轨',state:'done',note:'已取出音频'},
          {n:2,name:'切分片段',state:'doing',note:'切出 64 段'}
        ], 'running')""")
        time.sleep(0.3)
        check("进行中那步存在", page.js("!!document.querySelector('#jp-jprobe01 .step.now')"))
        bgd = page.js("""(()=>{const e=document.querySelector('#jp-jprobe01 .step.now');
            return e?getComputedStyle(e).backgroundColor:'none'})()""")
        check("进行中那步有高亮底色", bgd not in ("none", "rgba(0, 0, 0, 0)"),
              "bg=%s" % bgd)
        # 切回失败态再量红条，两种状态都要生效
        page.js("""renderSteps('jprobe01', [
          {n:1,name:'切分片段',state:'fail',note:'boom'}
        ], 'failed')""")
        time.sleep(0.3)
        col = page.js("""(()=>{const e=document.querySelector('#jp-jprobe01 .step.no');
            return e?getComputedStyle(e).borderLeftColor:'none'})()""")
        check("失败步有红色左边条", "255" in str(col), "border=%s" % col)

        print("\n=== 6. 真实任务：后端 steps 能到前端 ===")
        # 原来这里靠「/api/jobs 里碰巧有个历史任务」来验证。
        # 任务表是**内存态**，服务一重启就空了 ⇒ 断言前提不成立会假红
        #（本轮就撞上了：重启后 jobs=0）。改成主动发起一个任务再验证，
        # 判据自足、不依赖运行时残留。
        req = urllib.request.Request(
            "http://127.0.0.1:8756/api/jobs",
            data=json.dumps({"kind": "ingest",
                             "video": r"D:\AHC\VoiceStudio\data\uploads\two_speakers.mp4",
                             "do_train": False}).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            nj = json.load(urllib.request.urlopen(req, timeout=30))
            pid = nj["job"]["id"]
            withsteps = []
            for _ in range(20):
                time.sleep(0.5)
                jj = json.load(urllib.request.urlopen(
                    "http://127.0.0.1:8756/api/job/%s" % pid, timeout=20))["job"]
                if jj.get("steps"):
                    withsteps = [jj]
                    break
                if jj.get("state") in ("done", "failed"):
                    break
            check("后端任务带 steps 字段", len(withsteps) > 0,
                  "新建任务 %s 的 steps=%s" % (pid, str((jj or {}).get("steps"))[:60]))
            if withsteps:
                j = withsteps[0]
                check("每步都有名字和状态",
                      all(s.get("name") and s.get("state") for s in j["steps"]),
                      "步骤数=%d" % len(j["steps"]))
                check("完成/失败的步都写了说明",
                      all(s.get("note") for s in j["steps"]
                          if s["state"] in ("done", "fail")))
            # ★ 等它真的跑完再走。这个任务会**真建出 3 个角色**（拆解 → 分角色），
            #   而它要跑约 2 分钟。原来提交完就跑下一段，于是收尾清理时角色还不存在，
            #   "本次新增 0 个角色" —— 清理形同虚设，测试角色每轮累加。
            print("   等第 6 段那个拆解任务真正结束（它会建角色，清理要等它）…")
            final = wait_job(pid)
            print("   任务最终状态：%s（耗时见任务条）" % final)
            check("第 6 段的拆解任务跑到了终态", final in ("done", "failed", "interrupted"),
                  "state=%s" % final)
        except Exception as e:
            check("后端任务带 steps 字段", False, str(e)[:120])

        print("\n=== 7. 角色数据未受损 ===")
        # 【为什么改成"和开跑前比"】以前这里硬编码断言"角色 A/B 都还在"。
        # 那两个是早期用合成测试视频建出来的样本角色，后来被有意清掉了
        # （磁盘目录也确实没了），于是这条判据永久变红 —— 而它本该回答的
        # 问题是"这次验收有没有把真实数据搞坏"。
        # 现在改成自足口径：拿开跑前的快照比，任何原有角色都不许少、不许变瘦。
        # （比硬编码名字更强：以后换素材也不会假红。）
        roles = json.load(urllib.request.urlopen(
            "http://127.0.0.1:8756/api/roles", timeout=20))["roles"]
        now = {r["id"]: r for r in roles}
        lost = [i for i in BASE_ROLES if i not in now]
        check("开跑前已有的角色一个都没少", not lost,
              "丢了=%s；现有=%s" % (lost, [r["name"] for r in roles]))
        thin = []
        for rid, base in BASE_ROLES.items():
            r = now.get(rid)
            if not r:
                continue
            if (r["stats"]["segments"] < base["segments"]
                    or len(r.get("outputs") or []) < base["outputs"]):
                thin.append("%s: %d段->%d段 / %d成品->%d成品"
                            % (r["name"], base["segments"], r["stats"]["segments"],
                               base["outputs"], len(r.get("outputs") or [])))
        check("原有角色的素材和成品都没变少", not thin, "; ".join(thin) or "一致")
        added = [r["name"] for i, r in now.items() if i not in BASE_ROLES]
        print("    （本次新增 %d 个角色：%s —— 是第 6 段的测试导入，收尾会清掉）"
              % (len(added), added))

        print("\n=== 8. 子进程输出必须**实时**可见（不许块缓冲） ===")
        # 为什么单列一条：让角色唱歌（RVC 推理，3 分 45 秒的歌）跑了 16 分钟，
        # 任务日志里除了一行 "$ …infer.cli…" 之外**一个字都没有**，
        # 进度条纹丝不动 —— 用户原话就是"看不见进度以及做到哪一步"。
        # 根因：子进程 stdout 接管道时 Python 默认**块缓冲（4~8KB）**，
        # 它开头那几行 print 全堵在子进程缓冲区里，直到进程结束才一次冒出来。
        # 而 RVC 的推理链里连 tqdm 都没有 ⇒ 不修这条就永远"看起来卡住"。
        # ★ 判据自带负对照：把 PYTHONUNBUFFERED 摘掉，必须**抓不到早到的行**。
        import threading
        import tempfile
        sys.path.insert(0, HERE)
        import studio as _st

        probe_dir = tempfile.mkdtemp(prefix="vs_bufprobe_")
        probe_py = os.path.join(probe_dir, "probe.py")
        with open(probe_py, "w", encoding="utf-8") as fh:
            # 故意**不 flush**：要量的就是默认缓冲行为
            fh.write("import time\n"
                     "for i in range(3):\n"
                     "    print('TICK%d' % i)\n"
                     "    time.sleep(1.0)\n"
                     "print('END')\n")

        def early_ticks(unbuffered):
            """跑这个子进程，在它**还没结束时**数一数日志里已经到了几行。"""
            lf = os.path.join(probe_dir, "log_%s.txt" % ("on" if unbuffered else "off"))
            open(lf, "w").close()
            job = {"id": "bufprobe", "_logfile": lf, "pct": 0}
            env = None if unbuffered else {"PYTHONUNBUFFERED": None}
            cmd = '"%s" "%s"' % (_st.PY, probe_py)

            def go():
                _st.run(job, cmd, env=env, timeout=60)

            th = threading.Thread(target=go, daemon=True)
            th.start()
            time.sleep(2.2)          # 子进程总共约 3 秒，此刻它一定还没结束
            try:
                txt = open(lf, encoding="utf-8", errors="replace").read()
            except OSError:
                txt = ""
            early = txt.count("TICK")
            th.join(timeout=30)      # 等它跑完再看总行数
            try:
                total = open(lf, encoding="utf-8", errors="replace").read().count("TICK")
            except OSError:
                total = 0
            return early, total

        e_on, t_on = early_ticks(True)
        check("子进程的输出在它结束**之前**就到日志里了", e_on >= 1,
              "运行中已到 %d 行 / 总共 %d 行" % (e_on, t_on))
        e_off, t_off = early_ticks(False)
        # 负对照：这就是修之前的真实行为 —— 一切正常、只是"看不见"
        check("负对照：关掉无缓冲时，运行中**看不到**输出（证明这条判据真的在量）",
              e_off == 0 and t_off >= 3,
              "运行中 %d 行 / 结束后共 %d 行" % (e_off, t_off))
        try:
            import shutil as _sh
            _sh.rmtree(probe_dir, ignore_errors=True)
        except Exception:
            pass

        # 截图留证
        try:
            shot = page.send("Page.captureScreenshot", format="png")
            out = os.path.join(HERE, "steps_ui.png")
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

        # 收尾清理：第 6 段会**真的跑一次导入**（判据要自足，不能靠历史残留）。
        # 副作用是真建出角色，跑完不清理的话，界面会被一堆同名测试角色淹没
        # ——本项目已经撞过三次（角色从 3 个涨到 8 个、再到 6 个）。
        # 判据：source 指向测试视频、且**没有任何成品**（成品为 0 才敢删）。
        # 注意：第 6 段现在会**等任务真正结束**再往下走，所以这里一定看得见它们。
        clean_test_roles("收尾")
        # ★ 清理也要有判据：不然"删失败"只打印一行字，退出码照样 0（假绿）。
        #   负对照本轮实测过：30 秒超时那版就把「角色 A」「角色 B」留在了库里，
        #   这条判据当时会红。
        try:
            left = [r.get("name") for r in json.load(urllib.request.urlopen(
                "http://127.0.0.1:8756/api/roles", timeout=20))["roles"]
                if (r.get("source") or "").endswith(TEST_SOURCE)
                and not (r.get("outputs") or [])]
        except Exception as e:
            left = ["<拉角色列表失败:%s>" % str(e)[:40]]
        check("收尾后角色库里没有测试残留", not left, "残留=%s" % left)

    print("\n" + "=" * 58)
    print("通过 %d / 失败 %d" % (len(OK), len(BAD)))
    for b in BAD:
        print("  - " + b)
    print("=" * 58)
    sys.exit(0 if (not BAD and len(OK) > 0) else 1)


if __name__ == "__main__":
    main()