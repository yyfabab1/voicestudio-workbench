# -*- coding: utf-8 -*-
r"""加密歌（.ncm）能走完「分离人声/伴奏」+ 任务面板不挡画面（第 25 关）。

这一关护的是老板报的两个具体问题：

  【问题 1】VIP 歌分离失败
      $ ffmpeg.exe -i "...\uploads\Côme - La gloire à mes genoux.ncm" ...
      ...ncm: Invalid data found when processing input
      -> 退出码 1，用时 0.4s
      ! 原曲转码失败，可能是文件损坏或格式不认识。

    根因**不是**文件损坏：`.ncm` 是网易云的**加密容器**，ffmpeg 当然读不了。
    真缺陷在 `handle_upload` —— 它只对**视频**抽音轨，`.ncm` 就原样落进
    `data/uploads/` 被当歌用，一路走到分离那步才炸；而炸出来的文案
    （"文件损坏或格式不认识"）把人往**错的方向**引，用户只会去怀疑自己的文件。
    ⇒ 两处修：① `separate_song` 开头兜底 `ensure_playable_audio`（唱歌 /
      识别歌词 / 试听小样三条链路都经过它，一处修三处通）；② 上传入口
      当场解密，和视频抽音轨一个道理 —— 入口处一次做对。

  【问题 2】任务面板挡画面
    真凶之一是个**隐蔽的可用性 bug**：`tickJobs` 每 1.6 秒轮询一次，终态分支
    里**无条件** `classList.remove("open")` ⇒ 用户点开已结束的任务卡想看失败
    原因，最长 1.6 秒后又被收起来，来回闪、根本读不完。老板那张"错误详情"
    截图是抢在下一轮 tick 之前截到的。
    ⇒ 改成"只在刚跑完那一轮收起一次"（`dataset.finished` 兼作"已收过"标记）。
    顺手把展开态的高度压下来：实测展开一张卡占 **52% 视口高**（469/900），
    步骤区与原始日志各限高 140px（原 210/230），面板上限收到 58vh。

判据五段：

  A 源码对账（零浏览器）：separate_song 真的调了兜底 / 兜底按**魔数**判而不是
                        按后缀 / 上传入口有 .ncm 分支且解到稳定位置 /
                        文案不再把人往"文件损坏"上引
  B 纯函数（零浏览器）：真 .ncm ⇒ 解成普通音频且可复用 / 负对照：普通 mp3 原样
                        返回、不存在的文件原样返回、真魔数坏文件必须**明确抛错**
  C 端到端（真跑）：    真 .ncm ⇒ 解密 ⇒ 切片 ⇒ **真的分离出人声轨 + 伴奏轨**
                        （指定模型跳过选优，跑在临时缓存里，不碰真缓存）
  D 真鼠标 + 几何：     点标题能展开 / **跨两轮轮询仍保持展开**（本轮修的核心）/
                        展开高度 ≤ 50vh / 「收起全部」能看到且真点有效 /
                        取消展开后该按钮必须消失（尺子自检：不是恒显示）
                        ★ 这一段用的是**页面上恢复出来的历史卡**，而且先查
                          「服务认得它 + 它是终态」两条 —— 踩过的坑：一开始
                          用 studio.new_job 现造一条，那是只写磁盘，而 studio.JOBS
                          是内存字典 ⇒ `/api/job/<id>` 404 ⇒ tickJobs 走的是
                          "连不上服务"的 except 分支，**永远不进终态分支** ⇒
                          "轮询后仍展开"会假绿（永不收起也能判过）。
  E 尺子自检（实测能红）：把兜底换成恒等函数（= 修复前的行为）⇒ 分离必须失败

用法（venv：浏览器段要 websocket）：
    <venv>/Scripts/python.exe -s verify_ncm_sep.py
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

# ★ 本机设了 http_proxy，urllib 连 127.0.0.1 也走代理 ⇒ 拿回来的是代理的 502，
#   看起来像"服务没起"。直接把代理关掉。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "stages"))

import local_lib  # noqa: E402
import studio  # noqa: E402

BASE = "http://127.0.0.1:8756"
WEB = os.path.join(HERE, "web")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 9359                       # 跟其它 UI 关错开，别抢同一个调试端口
PROFILE = os.path.join(HERE, ".cdp-ncmsep")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def cdp_get(path, port=PORT):
    return json.load(urllib.request.urlopen(
        "http://127.0.0.1:%d%s" % (port, path), timeout=8))


def api_get(path):
    return json.load(urllib.request.urlopen(BASE + path, timeout=15))


def _mkjob(tmp):
    """造一个能喂给 studio.log 的最小 job（门禁自己用，不进任务面板）。"""
    lf = os.path.join(tmp, "_job.log")
    open(lf, "w").close()
    return {"id": "ncmsep-gate", "_logfile": lf}


def _real_ncm():
    """本机真实 ncm（挑最小的 —— 门禁要跑得快）。"""
    hits = []
    for d in local_lib.netease_dirs():
        hits += glob.glob(os.path.join(d["path"], "*.ncm"))
    hits += glob.glob(r"D:\CloudMusic\VipSongsDownload\*.ncm")
    hits += glob.glob(os.path.join(HERE, "data", "uploads", "*.ncm"))
    hits = sorted(set(hits), key=lambda p: os.path.getsize(p))
    return hits[0] if hits else None


def _mp3():
    """曲库里的普通音频，用来做"不该被动"的负对照。"""
    d = os.path.join(HERE, "data", "music")
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if f.lower().endswith((".mp3", ".flac", ".wav", ".m4a")):
            return os.path.join(d, f)
    return None


def _dur(path):
    """读 wav 秒数（不依赖 ffprobe）。"""
    import wave
    try:
        w = wave.open(path, "r")
        s = w.getnframes() / float(w.getframerate() or 1)
        w.close()
        return s
    except Exception:
        return 0.0


# ---------------------------------------------------------------- A 源码对账

def test_source():
    print("\n=== A. 源码对账（零浏览器） ===")
    stu = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    srv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()

    # A1 兜底真的被接进了分离链路
    check("separate_song 里真的调用了 ensure_playable_audio（不是只定义）",
          "song_path = ensure_playable_audio(job, song_path)" in stu,
          "只定义不调用 = 这个功能不存在")

    # A2 按**魔数**判而不是按后缀 —— 后缀谁都会改，魔数改不了
    blk = stu[stu.find("def ensure_playable_audio"):]
    blk = blk[:blk.find("\ndef separate_song")]
    check("兜底按魔数判（_ncm_mod().is_ncm(src)），不是按文件后缀猜",
          "_ncm_mod().is_ncm(src)" in blk,
          "看后缀会把「改名成 .mp3 的 ncm」漏掉")

    # A3 上传入口有 .ncm 分支（老板那条就是从这儿进来的）
    up = srv[srv.find("def handle_upload"):]
    up = up[:up.find("def role_action")]
    check("handle_upload 有 .ncm 分支（以前只有视频，.ncm 原样落盘被当歌用）",
          '.ncm"' in up and "ensure_playable_audio" in up)

    # A4 上传场景必须解到稳定位置：曲库会长期记这个路径，
    #    解到分离缓存区的话，一清缓存曲库那条就变"文件丢失"。
    check("上传的 .ncm 解密产物落在 UPLOADS（稳定位置），不是分离缓存区",
          "out_dir=studio.UPLOADS" in up)

    # A5 文案不许再把人往「文件损坏」上引。
    #    只看 raise 那一句 —— 函数上方 docstring 里**引用**了旧文案
    #    （"可能是文件损坏"）来说明当年错在哪，那是注释不是给用户看的话。
    i = blk.find('raise Fail("这首是网易云加密的 .ncm')
    raise_txt = blk[i:i + 260] if i >= 0 else ""
    check("解密失败的文案说清是「加密歌解密失败」，不再写「文件损坏」",
          "解密失败" in raise_txt and "文件损坏" not in raise_txt,
          raise_txt.split("\\n")[0][:60])


# ---------------------------------------------------------------- B 纯函数

def test_pure(tmp):
    print("\n=== B. 兜底函数（纯 Python） ===")
    ncm = _real_ncm()
    job = _mkjob(tmp)

    if not ncm:
        check("本机有真实的 .ncm 可以做判据", False, "一个都没找到")
        return None
    check("找到本机真 .ncm 做素材", True, os.path.basename(ncm))

    decoded = studio.ensure_playable_audio(job, ncm)
    check("B1 真 .ncm ⇒ 返回的是另一个文件（真的解出来了）",
          decoded != ncm and os.path.isfile(decoded), decoded)
    check("B2 解出来的是普通音频（mp3/flac/wav/m4a）",
          os.path.splitext(decoded)[1].lower() in (".mp3", ".flac", ".wav", ".m4a"),
          os.path.splitext(decoded)[1])
    check("B3 解出来的文件不是空壳（>1MB）",
          os.path.getsize(decoded) > 1048576,
          "%.1f MB" % (os.path.getsize(decoded) / 1048576))

    again = studio.ensure_playable_audio(job, ncm)
    check("B4 再解一次复用同一份（用户多点几次不该堆文件）",
          again == decoded)

    mp3 = _mp3()
    if mp3:
        check("B5 负对照：普通 mp3 原样返回（不许多余动作）",
              studio.ensure_playable_audio(job, mp3) == mp3, os.path.basename(mp3))

    gone = os.path.join(tmp, "根本没这个文件.ncm")
    check("B6 负对照：不存在的文件原样返回（不抛异常）",
          studio.ensure_playable_audio(job, gone) == gone)

    # 真魔数 + 垃圾内容 ⇒ 必须**明确抛错**，不能静默返回一个假路径
    bad = os.path.join(tmp, "坏掉的.ncm")
    with open(bad, "wb") as f:
        f.write(b"CTENFDAM" + b"\x00" * 40)
    try:
        r = studio.ensure_playable_audio(job, bad)
        check("B7 负对照：真魔数的坏 ncm 必须明确抛错", False,
              "没抛错，返回了 %s" % r)
    except Exception as e:
        check("B7 负对照：真魔数的坏 ncm 必须明确抛错", True, str(e)[:60])
        check("B8 抛出来的原因说清是解密问题（不是误导成「文件损坏」）",
              "解密" in str(e), str(e)[:70])

    # 上传场景：产物必须落在指定目录
    updir = os.path.join(tmp, "uploads_like")
    os.makedirs(updir, exist_ok=True)
    up = studio.ensure_playable_audio(job, ncm, out_dir=updir)
    check("B9 上传场景：产物落在指定的 out_dir（曲库要长期引用它）",
          os.path.dirname(os.path.abspath(up)) == os.path.abspath(updir),
          os.path.dirname(up))
    return ncm


# ---------------------------------------------------------------- C 端到端分离

def test_e2e(tmp, ncm):
    print("\n=== C. 端到端（真跑分离） ===")
    if not ncm:
        check("有素材才能跑端到端分离", False, "跳过")
        return
    if not os.path.isfile(studio.FFMPEG):
        check("本机有 ffmpeg", False, studio.FFMPEG)
        return

    # ★ 把分离缓存挪到临时目录：门禁跑出来的东西不许留在真缓存里
    #   （真缓存是用户下次唱歌要复用的，混进一个 10 秒的测试片段会污染）。
    real_cache, real_dec = studio.SEP_CACHE, studio.DECODED_DIR
    tcache = os.path.join(tmp, "cache")
    studio.SEP_CACHE = tcache
    studio.DECODED_DIR = os.path.join(tcache, "_decoded")
    os.makedirs(studio.DECODED_DIR, exist_ok=True)
    try:
        job = _mkjob(tmp)
        audio = studio.ensure_playable_audio(job, ncm)
        check("C1 .ncm 先被解成普通音频（ffmpeg 读得了）",
              os.path.splitext(audio)[1].lower() != ".ncm"
              and os.path.isfile(audio))

        # 切 12 秒片段：分离整首要好几分钟，判据只需要证明"这条路通了"。
        # ★ **从第 40 秒开始**，不能从 0 秒 —— 流行歌开头十秒常常是纯前奏，
        #   人声轨本来就该是静音（实测从 0 秒切：人声 rms=0.0000、分离得分 6.7，
        #   判据"人声轨不是静音"会红，但那是**素材选得不对**，不是产品坏了）。
        clip = os.path.join(tmp, "clip.wav")
        p = subprocess.run(
            [studio.FFMPEG, "-y", "-v", "error", "-ss", "40", "-t", "12",
             "-i", audio, "-ac", "2", "-ar", "44100", clip],
            capture_output=True, timeout=300)
        check("C2 能从解密结果切出片段（说明解出来的真是可解码音频）",
              p.returncode == 0 and os.path.isfile(clip)
              and _dur(clip) > 5, "%.1f 秒" % _dur(clip))

        # 指定模型 ⇒ 跳过"试 3 个模型选优"（那段是为质量，不是为通路）
        t0 = time.time()
        try:
            v, i = studio.separate_song(
                job, clip, model=studio.UVR_MODEL_SAFE, use_cache=False)
        except Exception as e:
            check("C3 加密歌能真的分离出人声轨", False, "%s: %s"
                  % (type(e).__name__, str(e)[:120]))
            return
        check("C3 加密歌能真的分离出人声轨", os.path.isfile(v),
              "%s（%.0fs）" % (os.path.basename(v), time.time() - t0))
        check("C4 加密歌能真的分离出伴奏轨", os.path.isfile(i),
              os.path.basename(i))

        dv, di = _dur(v), _dur(i)
        check("C5 人声轨时长与片段对得上（不是半个空文件）",
              abs(dv - _dur(clip)) < 1.0, "人声 %.1fs / 片段 %.1fs"
              % (dv, _dur(clip)))
        check("C6 伴奏轨时长与片段对得上", abs(di - _dur(clip)) < 1.0,
              "伴奏 %.1fs" % di)
        check("C7 人声轨不是静音（分离真跑了，不是产出全 0）",
              studio.wav_rms(v) > 0.0005, "rms=%.4f" % studio.wav_rms(v))
        check("C8 伴奏轨不是静音", studio.wav_rms(i) > 0.0005,
              "rms=%.4f" % studio.wav_rms(i))
    finally:
        studio.SEP_CACHE, studio.DECODED_DIR = real_cache, real_dec


# ---------------------------------------------------------------- D 任务面板

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

    def js(self, expr, await_promise=False):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=await_promise)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def click_at(self, x, y):
        """真鼠标：press + release，返回按下前那一刻的命中元素。"""
        hit = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.tagName+'|'+(e.id||'')+'|'+(e.className||''))"
                      ":'null'})()" % (x, y))
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                  button="left", clickCount=1)
        time.sleep(0.05)
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                  button="left", clickCount=1)
        return hit

    def box(self, sel):
        return self.js("(()=>{const e=document.querySelector(%s);"
                       "if(!e) return null; const r=e.getBoundingClientRect();"
                       "return {x:r.left+r.width/2,y:r.top+r.height/2,"
                       "w:r.width,h:r.height,top:r.top,bottom:r.bottom};})()"
                       % json.dumps(sel))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def test_panel():
    print("\n=== D. 任务面板：不挡画面（真鼠标 + 几何） ===")
    if not os.path.isfile(CHROME):
        check("本机有 Chrome 可以跑浏览器段", False, CHROME)
        return
    # ★ 必须用**服务自己认得**的任务卡。
    #   踩过的坑：一开始是用 studio.new_job + save_job 现造一条 —— 那是只写了
    #   磁盘，而 studio.JOBS 是**内存字典**，get_job() 只从内存读 ⇒
    #   `/api/job/<id>` 返回 404 ⇒ tickJobs 走的是"连不上服务"那个 except 分支，
    #   **根本不会进终态分支** ⇒ "轮询后仍展开"会假绿（永不收起也能判过）。
    #   改用页面上已经恢复出来的历史卡：它们在服务内存里、也在页面的 watching 里。
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1280,900",
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
            check("Chrome 能起来", False, "起不来")
            return
        c = Cdp()
        c.send("Runtime.enable")
        c.send("Emulation.setDeviceMetricsOverride", width=1280, height=900,
               deviceScaleFactor=1, mobile=False)
        c.send("Page.navigate", url=BASE + "/")
        time.sleep(7.5)

        # ---- D1 面板里有已结束的任务卡（restoreJobs 恢复出来的） ----
        raw = c.js("""(()=>{const out=[];
          document.querySelectorAll('#jobs .jobbox').forEach(function(b){
            const t=b.querySelector('.jh b');
            out.push({id:(b.id||'').replace('job-',''),
                      title:t?t.textContent:''});});
          return JSON.stringify(out);})()""")
        cards = json.loads(raw or "[]")
        check("D1 任务面板里有已结束的任务卡可测", bool(cards),
              "%d 张" % len(cards))
        if not cards:
            return

        # ---- D2 挑一张"服务确实认得 + 真是终态"的卡 ----
        #   两条都必须查：① 服务不认得 ⇒ tickJobs 走 404 分支，测不到终态逻辑；
        #   ② 还在跑的任务 ⇒ 永远不进终态分支，"不被收起"同样会假绿。
        jid, why = None, []
        for cd in cards:
            try:
                j = api_get("/api/job/" + cd["id"]).get("job") or {}
            except Exception as e:
                why.append("%s→%s" % (cd["id"], type(e).__name__))
                continue
            if j.get("state") in ("failed", "interrupted", "cancelled", "done"):
                jid = cd["id"]
                break
            why.append("%s→仍在跑(%s)" % (cd["id"], j.get("state")))
        check("D2 其中至少一张是终态且服务认得它"
              "（否则轮询分支根本不会跑到，判据会假绿）",
              bool(jid), jid or ("; ".join(why[:3]) or "没有终态卡"))
        if not jid:
            return

        # ---- 真鼠标点标题展开（精确到这一张，别点到别的卡上） ----
        b = c.js("""(()=>{const box=document.getElementById('job-%s');
          if(!box) return null; const jh=box.querySelector('.jh');
          if(!jh) return null; const r=jh.getBoundingClientRect();
          const cx=r.left+r.width/2, cy=r.top+r.height/2;
          const hit=document.elementFromPoint(cx,cy);
          return {x:cx,y:cy,w:r.width,h:r.height,top:r.top,bottom:r.bottom,
                  命中:hit?(hit.tagName+'|'+(hit.id||'')+'|'+(hit.className||'')):'null',
                  在标题内: !!(hit && jh.contains(hit))};})()""" % jid)
        check("D3 卡片标题在视口内（门禁不许点视口外的东西）",
              bool(b) and b["w"] > 0 and b["h"] > 0 and 0 <= b["x"] <= 1280
              and 0 <= b["y"] <= 900, b)
        if not b:
            return
        hit = c.click_at(b["x"], b["y"])
        # 命中的会是标题里的 <b>（文字节点）—— 它在 .jh 内部就算点对了标题。
        # 一开始判据写的是 `"jh" in hit`，把这类正常命中判成了红（尺子太严）。
        check("D4 真鼠标命中的就是这张卡的标题（不是别人）",
              bool(b["在标题内"]), "命中=%s 在标题内=%s" % (hit, b["在标题内"]))
        time.sleep(0.6)
        op = c.js("document.querySelectorAll('#job-%s.jobbox.open').length" % jid)
        check("D5 真鼠标点标题 ⇒ 卡片展开", op == 1, "open=%s" % op)

        # ---- 核心：跨轮询不被自动收起 ----
        #   修复前 tickJobs 的终态分支无条件 remove('open')，而它每 1.6 秒跑一轮，
        #   用户点开看失败原因最多 1.6 秒就被收起来。
        c.js("tickJobs()", await_promise=True)
        time.sleep(0.3)
        still = c.js("document.querySelectorAll('#job-%s.jobbox.open').length" % jid)
        check("★ D6 过一轮轮询后仍然展开着（修复前这里会被自动收起）",
              still == 1, "open=%s" % still)
        c.js("tickJobs()", await_promise=True)
        time.sleep(0.3)
        still2 = c.js("document.querySelectorAll('#job-%s.jobbox.open').length" % jid)
        check("★ D7 连过两轮轮询仍保持展开（用户能读完失败原因）",
              still2 == 1, "open=%s" % still2)

        # ---- 展开高度 ----
        g = c.js("(()=>{const j=document.getElementById('jobs');"
                 "const b=document.getElementById('job-%s');"
                 "const rj=j.getBoundingClientRect();"
                 "const rb=b.getBoundingClientRect();"
                 "return {panel:Math.round(rj.height),card:Math.round(rb.height),"
                 "vh:innerHeight,"
                 "pct:Math.round(rj.height/innerHeight*100)};})()" % jid)
        check("D8 展开态面板不超过半个视口高（不然就是「挡画面」）",
              g and g["pct"] <= 50, g)

        # ---- 「收起全部」 ----
        jall = c.box("#jall")
        check("D9 有卡片展开时「收起全部」可见（高频操作要摆到手边）",
              bool(jall) and jall["w"] > 0 and jall["h"] > 0, jall)
        if jall:
            hit2 = c.click_at(jall["x"], jall["y"])
            check("D10 真鼠标命中的是「收起全部」本身", hit2 and "jall" in hit2, hit2)
            time.sleep(0.5)
            left = c.js("document.querySelectorAll('#jobs .jobbox.open').length")
            check("D11 真点「收起全部」⇒ 展开的卡全收起来", left == 0,
                  "还剩 %s 张展开" % left)

        # ---- 尺子自检：没有展开卡时，这个按钮必须消失 ----
        vis2 = c.js("(()=>{const e=document.getElementById('jall');"
                    "if(!e) return 'MISSING';"
                    "return getComputedStyle(e).display;})()")
        check("★ D12 尺子自检：没有展开卡时「收起全部」必须不显示"
              "（否则「永远显示」也会被判绿）", vis2 == "none", vis2)

        # ---- 负对照：把 dataset.finished 摘掉，模拟"还没收过"的任务 ----
        c.js("(()=>{const e=document.getElementById('job-%s');"
             "delete e.dataset.finished; return 1;})()" % jid)
        op2 = c.js("(()=>{const e=document.getElementById('job-%s');"
                   "e.classList.add('open');"
                   "return document.querySelectorAll('#job-%s.jobbox.open').length;})()"
                   % (jid, jid))
        c.js("tickJobs()", await_promise=True)
        time.sleep(0.3)
        op3 = c.js("document.querySelectorAll('#job-%s.jobbox.open').length" % jid)
        check("★ D13 尺子自检：没有 finished 标记的卡，轮询必须把它收起"
              "（证明 D6/D7 不是恒绿）", op2 == 1 and op3 == 0,
              "展开 %s → 轮询后 %s" % (op2, op3))
    except Exception as e:
        check("面板段自身异常", False, str(e)[:200])
    finally:
        if c:
            c.close()
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass


# ---------------------------------------------------------------- E 尺子自检

def test_ruler(tmp, ncm):
    print("\n=== E. 尺子自检：把修复拿掉，判据必须变红 ===")
    if not ncm:
        check("有素材才能做尺子自检", False, "跳过")
        return
    real_cache, real_dec = studio.SEP_CACHE, studio.DECODED_DIR
    tcache = os.path.join(tmp, "cache2")
    studio.SEP_CACHE = tcache
    studio.DECODED_DIR = os.path.join(tcache, "_decoded")
    os.makedirs(studio.DECODED_DIR, exist_ok=True)
    real_fn = studio.ensure_playable_audio
    try:
        # 换回"修复前"的行为：兜底啥也不做，.ncm 原样往下走
        studio.ensure_playable_audio = lambda job, src, out_dir=None: src
        job = _mkjob(tmp)
        try:
            studio.separate_song(job, ncm, model=studio.UVR_MODEL_SAFE,
                                 use_cache=False)
            check("★ 把兜底换成恒等函数后，.ncm 分离必须失败"
                  "（不变红说明判据没钉在解密上）", False, "居然成功了？")
        except Exception as e:
            msg = str(e)
            check("★ 把兜底换成恒等函数后，.ncm 分离必须失败"
                  "（不变红说明判据没钉在解密上）",
                  ("转码失败" in msg or "Invalid data" in msg or "损坏" in msg),
                  msg.replace("\n", " ")[:110])
    finally:
        studio.ensure_playable_audio = real_fn
        studio.SEP_CACHE, studio.DECODED_DIR = real_cache, real_dec


# ---------------------------------------------------------------- main

def main():
    print("=" * 62)
    print("加密歌（.ncm）分离 + 任务面板不挡画面 验收")
    print("=" * 62)
    tmp = tempfile.mkdtemp(prefix="ncmsep_")
    ncm = None
    try:
        test_source()
        ncm = test_pure(tmp)
        test_e2e(tmp, ncm)
        test_panel()
        test_ruler(tmp, ncm)
    except Exception as e:
        check("探针自身异常中断（后续断言未执行）", False, str(e)[:200])
        import traceback
        traceback.print_exc()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - %s" % b)
    print("结论：%s" % ("加密歌分离与任务面板全绿" if not BAD else "有红"))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
