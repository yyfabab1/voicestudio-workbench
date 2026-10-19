r"""门禁：网易云 .ncm 解密（第 16 关）。

为什么单独一关：这个功能**坏起来几乎无声** ——
  · meta 的 AES key 抄错一个字节 ⇒ 密钥能解、元数据全乱码，很容易误判成
    "网易云改格式了"，于是整条功能被放弃（这次踩的就是这个）；
  · 音频起点少跳/多跳 4 字节 ⇒ 文件照样写出来、大小也对、任务也成功，
    但**内容是噪音**，只有真的去播/去量才发现（ffprobe 连时长都量不出）；
  · 没有 numpy 时的回退分支算错一个偏移 ⇒ 只在**另一个解释器**里出错，
    开发机上永远绿的那种 bug（实测踩过：managed python 没装 numpy）；
  · 解密产物重复生成 ⇒ 曲库里出现「曹操」「曹操 (2)」两条，硬盘翻倍。
既有 15 关一条都不会红 ⇒ 必须专门盯。

判据四段，★ 标记的都是"换成坏版本必须变红"的：
  A 模块层（纯 Python）：AES 向量自检 / keystream 恒等 / 两条解密路径逐字节一致
                        / ★ 四类坏文件必须报错且不留半截文件
  B local_lib 层：      ncm 能扫到（带真歌名）/ ★.qmc 仍然跳过 / 入库条目是解密产物
                        / 重复导入复用缓存
  C 接口层：            dirs 带出网易云目录 / scan 带 enc / decrypt 入库 / ★坏文件被拒
  D 浏览器（真鼠标）：  候选目录 chip → 真点 → 扫描结果标"网易云加密"
                        → 真点「解密并翻唱」→ 翻唱台拿到的是 .mp3，不是 .ncm

用法（venv 跑：浏览器段要 websocket）：
    <venv>/Scripts/python.exe -s verify_ncm.py
"""
import contextlib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "stages"))
import ncm  # noqa: E402
import local_lib  # noqa: E402
import studio  # noqa: E402

BASE = "http://127.0.0.1:8756"
OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def _ffprobe_dur(p):
    """能量就量，量不到返回 0（ffprobe 不是每台机器都在 PATH 上，
    所以时长判据一律配不依赖它的替代判据，不能只靠它）。"""
    for exe in ("ffprobe",):
        try:
            r = subprocess.run([exe, "-v", "quiet", "-show_entries",
                                "format=duration", "-of", "csv=p=0", p],
                               capture_output=True, timeout=30)
            t = (r.stdout or b"").decode("utf-8", "replace").strip()
            return float(t.split(",")[-1]) if t else 0.0
        except Exception:
            return 0.0
    return 0.0


def _real_ncm():
    """本机真实 ncm 文件（挑最小的那个，门禁要跑得快）。"""
    hits = []
    for d in local_lib.netease_dirs():
        hits += glob.glob(os.path.join(d["path"], "*.ncm"))
    hits += glob.glob(r"D:\CloudMusic\VipSongsDownload\*.ncm")
    hits = sorted(set(hits), key=lambda p: os.path.getsize(p))
    return hits[0] if hits else None


# ---------------------------------------------------------------- A 模块层

def section_a(src):
    print("\n=== A. 模块层（纯 Python） ===")
    check("AES-128 实现过 FIPS-197 向量（没装 Crypto 也要能对）",
          ncm.aes_self_test() is True)
    if not src:
        check("本机有真实的 .ncm 可以做判据", False, "没找到 ncm 文件")
        return None

    check("is_ncm 认得出真 ncm", ncm.is_ncm(src) is True)
    tmp = tempfile.mkdtemp(prefix="ncmA_")
    mp3 = os.path.join(tmp, "plain.mp3")
    shutil.copyfile(os.path.join(HERE, "data", "music",
                                 os.listdir(os.path.join(HERE, "data", "music"))[0]),
                    mp3)
    check("is_ncm 不会把普通 mp3 认成 ncm", ncm.is_ncm(mp3) is False)

    info = ncm.probe(src)
    check("probe 读出真歌名/歌手/时长（不解密音频，只读头）",
          info.get("ok") is True and info.get("title")
          and info.get("dur", 0) > 30 and info.get("fmt") == "mp3",
          "%s / %s / %.1fs / %s" % (info.get("title"), info.get("artist"),
                                    info.get("dur", 0), info.get("fmt")))

    outd = os.path.join(tmp, "out")
    try:
        r = ncm.decrypt(src, outd)
    except Exception as e:
        check("真 ncm 能解密", False, "%s: %s" % (type(e).__name__, e))
        return None
    head = open(r["path"], "rb").read(4)
    check("解密产物是标准 mp3（ID3 头，不是噪音）", head[:3] == b"ID3",
          "头 4 字节=%s" % head.hex())
    h = ncm.read_header(src)
    sz = os.path.getsize(r["path"])
    check("解密产物大小 == 加密音频区长度（一个字节不多、一个字节不少）",
          sz == h["audio_len"], "产物=%d 音频区=%d" % (sz, h["audio_len"]))
    dur = _ffprobe_dur(r["path"])
    if dur:
        check("ffprobe 量得出生效时长，且与元数据对得上",
              abs(dur - h["dur_ms"] / 1000.0) < 3.0,
              "实测=%.2fs 元数据=%.2fs" % (dur, h["dur_ms"] / 1000.0))
    else:
        print("    [提示] 这台机器 PATH 上没有 ffprobe，时长判据跳过"
              "（已有大小判据兜着）")

    # keystream 恒等：拿真文件的密文和明文反推真实密钥流，跟实现逐字节对
    with open(src, "rb") as f:
        f.seek(h["audio_start"])
        enc_head = f.read(4096)
    dec_head = open(r["path"], "rb").read(4096)
    ks_true = bytes(enc_head[i] ^ dec_head[i] for i in range(256))
    ks_impl = ncm._keystream(h["key"])
    check("★ keystream 与真文件反推出来的逐字节相同（错一位整首都是噪音）",
          ks_true == ks_impl,
          "前 8 字节 真=%s 实现=%s" % (ks_true[:8].hex(), ks_impl[:8].hex()))
    ok_all = all(dec_head[i] ^ ks_impl[i % 256] == enc_head[i]
                 for i in range(len(dec_head)))
    check("★ 整个音频区都能用这条 keystream 还原回密文（周期 256 没算错）", ok_all)

    # 两条路径必须一致：有 numpy 的向量化 vs 逐字节回退
    saved = ncm._NP
    try:
        ncm._NP = False
        r2 = ncm.decrypt(src, os.path.join(tmp, "out2"))
    finally:
        ncm._NP = saved
    same = open(r["path"], "rb").read() == open(r2["path"], "rb").read()
    check("★ 没有 numpy 时的回退路径与向量化路径逐字节一致"
          "（这个分支错一个偏移，只在没装 numpy 的机器上炸）", same)

    # ---- 四类坏文件：必须报错，且**不许留下半截文件** ----
    bad_cases = []
    fake = os.path.join(tmp, "fake.ncm")
    shutil.copyfile(mp3, fake)
    bad_cases.append(("把普通 mp3 改名成 .ncm", fake, "不是 ncm"))
    trunc = os.path.join(tmp, "trunc.ncm")
    with open(src, "rb") as f:
        open(trunc, "wb").write(f.read(200))
    bad_cases.append(("截断到 200 字节的 ncm", trunc, "截断"))
    corrupt = os.path.join(tmp, "corrupt.ncm")
    raw = bytearray(open(src, "rb").read())
    for i in range(20, 60):          # 把密钥区搅坏
        raw[i] ^= 0xFF
    open(corrupt, "wb").write(bytes(raw))
    bad_cases.append(("密钥区被改坏的 ncm", corrupt, ""))
    empty = os.path.join(tmp, "empty.ncm")
    open(empty, "wb").write(b"")
    bad_cases.append(("0 字节的 ncm", empty, ""))

    for label, path, kw in bad_cases:
        d = os.path.join(tmp, "bad_" + os.path.basename(path))
        os.makedirs(d, exist_ok=True)
        err = ""
        try:
            ncm.decrypt(path, d)
        except Exception as e:
            err = str(e)
        left = os.listdir(d)
        check("★ 负对照：%s → 报错，且不产出半截文件" % label,
              bool(err) and not left,
              "err=%s 残留=%s" % (err[:60], left))

    shutil.rmtree(tmp, ignore_errors=True)
    return src


# ---------------------------------------------------------------- B local_lib

def section_b(src):
    print("\n=== B. 本地曲库层 ===")
    if not src:
        check("有真 ncm 可做判据", False)
        return
    d = os.path.dirname(src)
    r = local_lib.scan_dir(d)
    ncm_items = [i for i in r["items"] if i.get("enc") == "ncm"]
    check("扫描把 .ncm 算进可导入列表（不再是一律跳过）",
          len(ncm_items) == len(glob.glob(os.path.join(d, "*.ncm"))),
          "items=%d 其中 ncm=%d" % (len(r["items"]), len(ncm_items)))
    hit = [i for i in ncm_items if os.path.abspath(i["path"]) == os.path.abspath(src)]
    check("ncm 条目带的是**真歌名**，不是文件名（元数据没白解）",
          bool(hit) and hit[0].get("name") and hit[0].get("dur", 0) > 30,
          hit and "%s / %.1fs" % (hit[0]["name"], hit[0]["dur"]))

    # ★ 负对照：QQ 音乐的加密格式仍然必须跳过（别顺手把别的加密也放行）
    tmp = tempfile.mkdtemp(prefix="ncmB_")
    qmc = os.path.join(tmp, "假歌.qmc")
    open(qmc, "wb").write(b"\x00" * 4096)
    r2 = local_lib.scan_dir(tmp)
    check("★ 负对照：.qmc（QQ音乐加密）仍然被跳过并写明原因",
          not r2["items"] and any(".qmc" in s["name"] for s in r2["skipped"]),
          [s["why"] for s in r2["skipped"]])
    shutil.rmtree(tmp, ignore_errors=True)

    # 入库：产物是标准音频，且记下来历
    def _n_files():
        return len(glob.glob(os.path.join(studio.DATA, "music", "*")))

    out = local_lib.add_files([src])
    mid = _n_files()          # ★ 基准要取"第一次导入之后"：
    added = out["added"]      #   第一次当然会多一个文件，那是解密产物，本来就该有
    if added:
        a = added[0]
        head = open(a["path"], "rb").read(3)
        check("导入 .ncm：入库的是解密后的标准音频",
              a["path"].lower().endswith(".mp3") and head == b"ID3"
              and os.path.isfile(a["path"]),
              "%s 头=%s" % (os.path.basename(a["path"]), head.hex()))
        check("曲库条目记了来历（from_ncm 指向原 .ncm）",
              os.path.abspath(a.get("from_ncm") or "") == os.path.abspath(src),
              a.get("from_ncm"))
        check("标题用的是真实「歌手 - 歌名」",
              bool(a.get("title")) and len(a["title"]) >= 2, a.get("title"))
    else:
        check("导入 .ncm：入库的是解密后的标准音频", False,
              out["bad"][0]["why"] if out["bad"] else "没导入")

    # 再导一次：必须复用上次的产物，不许又写一份（否则曲库里会出现"曹操 (2)"）
    out2 = local_lib.add_files([src])
    after = _n_files()
    same_path = bool(out2["added"]) and bool(added) \
        and out2["added"][0]["path"] == added[0]["path"]
    check("重复导入同一首：复用已解密的那份，不再多写一份到硬盘",
          same_path and after == mid,
          "首次后=%d 重复导入后=%d 路径相同=%s" % (mid, after, same_path))
    if out2["added"]:
        studio.load_library()  # 不删条目：这是用户曲库里正常的一首歌

    dirs = local_lib.netease_dirs()
    check("能探测到网易云客户端把歌下在哪（VIP 目录不在 Music 下）",
          bool(dirs) and any(x["ncm"] > 0 for x in dirs),
          [(x["name"], x["ncm"]) for x in dirs])


# ---------------------------------------------------------------- C 接口

def _get(path):
    try:
        return json.load(urllib.request.urlopen(BASE + path, timeout=15))
    except Exception as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"_err": str(e)}


def _post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except Exception as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"_err": str(e)}


def section_c(src):
    print("\n=== C. 接口（连的是 8756 常驻服务，改了 server.py 要重启才测得到） ===")
    dirs = _get("/api/music/local/dirs").get("dirs") or []
    check("候选目录里带出网易云目录，并标了有几首是加密的",
          any(d.get("ncm") for d in dirs),
          [(d.get("name"), d.get("ncm")) for d in dirs if d.get("ncm")])
    if not src:
        check("有真 ncm 可做判据", False)
        return
    d = os.path.dirname(src)
    sc = _get("/api/music/local/scan?dir=" + urllib.request.quote(d))
    items = sc.get("items") or []
    check("扫描接口把 ncm 标成 enc=ncm 返回给前端（前端靠它换按钮）",
          any(i.get("enc") == "ncm" for i in items),
          "items=%d" % len(items))
    dec = _post("/api/music/local/decrypt", {"paths": [src]})
    check("解密接口：返回入库条目（不是报错）",
          bool(dec.get("added")),
          (dec.get("bad") or [{}])[0].get("why") or "ok")
    tmp = tempfile.mkdtemp(prefix="ncmC_")
    fake = os.path.join(tmp, "其实是mp3.ncm")
    with open(os.path.join(HERE, "data", "music",
                           os.listdir(os.path.join(HERE, "data", "music"))[0]), "rb") as f:
        open(fake, "wb").write(f.read())
    bad = _post("/api/music/local/decrypt", {"paths": [fake]})
    check("★ 负对照：假冒的 .ncm 被拒，并把原因说清楚（不是 500、不是静默跳过）",
          not bad.get("added") and bool(bad.get("bad"))
          and "解不开" in (bad["bad"][0].get("why") or ""),
          (bad.get("bad") or [{}])[0].get("why"))
    shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- D 浏览器

PORT = 9347
PROFILE = os.path.join(HERE, ".chrome-ncm")
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
if not os.path.isfile(CHROME):
    for c in (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              os.path.expandvars(r"%USERPROFILE%\AppData\Local\Google\Chrome\Application\chrome.exe")):
        if os.path.isfile(c):
            CHROME = c
            break


def cdp_get(path):
    return json.load(urllib.request.urlopen("http://127.0.0.1:%d%s" % (PORT, path),
                                            timeout=5))


class Cdp:
    """最小 CDP 客户端：真鼠标（Input.dispatchMouseEvent）+ 命中测试。

    禁 element.click() —— 它绕过命中测试，屏幕外的东西也能"点成功"。
    """
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

    def js(self, expr, awaitp=False):
        r = self.send("Runtime.evaluate", expression=expr, awaitPromise=awaitp,
                      returnByValue=True)
        if r.get("exceptionDetails"):
            d = r["exceptionDetails"]
            raise RuntimeError("%s @ %s:%s" % (
                ((d.get("exception") or {}).get("description") or d.get("text")),
                d.get("url"), d.get("lineNumber")))
        return r.get("result", {}).get("value")

    def js_errors(self):
        out = []
        for m in self.events:
            if m.get("method") == "Runtime.exceptionThrown":
                d = m["params"].get("exceptionDetails", {})
                out.append("%s @ %s:%s" % (
                    (d.get("exception") or {}).get("description") or d.get("text"),
                    d.get("url"), d.get("lineNumber")))
        return out

    def box(self, sel):
        return self.js("""(()=>{
          const el=document.querySelector(%s);
          if(!el) return {err:'no element'};
          el.scrollIntoView({block:'center',inline:'center'});
          const r=el.getBoundingClientRect();
          const x=r.left+r.width/2, y=r.top+r.height/2;
          const hit=document.elementFromPoint(x,y);
          return {x:x,y:y,inView:(r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth),
                  hit:hit?(hit.id||hit.className||hit.tagName):null,
                  hitIsSelf:!!(hit&&(hit===el||el.contains(hit)||hit.contains(el)))};
        })()""" % json.dumps(sel))

    def click(self, sel, label=""):
        b = self.box(sel)
        if b.get("err"):
            check("能点到 %s" % (label or sel), False, b["err"])
            return False
        if not b.get("inView"):
            check("能点到 %s" % (label or sel), False, "不在视口内，真鼠标必然打空")
            return False
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.id||e.className||e.tagName):'null'})()" % (b["x"], b["y"]))
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=b["x"], y=b["y"],
                  button="left", clickCount=1)
        top2 = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                       "return e?(e.id||e.className||e.tagName):'null'})()" % (b["x"], b["y"]))
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=b["x"], y=b["y"],
                  button="left", clickCount=1)
        if top2 != top:
            print("    [诊断] ⚠ press→release 落点变了：%s → %s" % (top, top2))
        check("真鼠标点得到 %s（命中测试 PASS）" % (label or sel),
              bool(b.get("hitIsSelf")), "落点=%s 命中=%s" % (top, b.get("hit")))
        return True


def poll(page, expr, want=True, secs=25, step=0.4):
    t0 = time.time()
    while time.time() - t0 < secs:
        try:
            if bool(page.js(expr)) == want:
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


def section_d(src):
    print("\n=== D. 浏览器（真鼠标） ===")
    if not src:
        check("有真 ncm 可做判据", False)
        return
    if not os.path.isfile(CHROME):
        check("本机有 Chrome 可以跑浏览器段", False, CHROME)
        return
    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT, "--remote-allow-origins=*",
        "--user-data-dir=" + PROFILE, "--window-size=1400,1000",
        "--no-first-run", "--no-default-browser-check", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    page = None
    try:
        for _ in range(40):
            try:
                cdp_get("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        else:
            raise RuntimeError("Chrome 没起来")
        tabs = [t for t in cdp_get("/json/list")
                if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        page = Cdp(tabs[0]["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Log.enable")
        page.send("Page.navigate", url="http://127.0.0.1:8756/")
        time.sleep(5.0)

        # 候选目录里必须出现网易云入口（用户就是靠它找到 VIP 歌的）
        ok_chip = poll(page,
                       "(()=>{const a=Array.from(document.querySelectorAll('#locDirs .loc-dir'));"
                       "return a.some(e=>e.textContent.indexOf('网易云')>=0)})()", True, 25)
        check("我的音乐面板里出现「网易云」入口（VIP 歌以前根本找不到）",
              ok_chip,
              page.js("(()=>Array.from(document.querySelectorAll('#locDirs .loc-dir'))"
                      ".map(e=>e.textContent.trim()).join(' | '))()")[:160])

        # 真点那个 chip → 自动扫描。
        # 给它打 id 再点：直接用 :nth-of-type 会在 #locDirs 里按标签算序号，
        # 于是永远选不到那个 span（CDP 报 no element，看着像"页面没渲染"）。
        got = page.js("""(()=>{const a=Array.from(document.querySelectorAll('#locDirs .loc-dir'));
          const e=a.find(x=>x.textContent.indexOf('网易云')>=0);
          if(!e) return false; e.id='__ncmdir'; return true;})()""")
        if got:
            page.click("#__ncmdir", "网易云目录 chip")
        else:
            check("真鼠标点网易云目录 chip", False, "没找到那个 chip")
        filled = poll(page,
                      "((document.querySelector('#locPath').value||'').length>0)", True, 20)
        check("点了就把目录填进输入框（不是只给你看一眼）", filled,
              page.js("document.querySelector('#locPath').value"))
        rows = poll(page, "document.querySelectorAll('#locOut .loc-row').length > 0",
                    True, 40)
        check("扫描结果里有 ncm 歌曲行", rows,
              "行数=%s" % page.js("document.querySelectorAll('#locOut .loc-row').length"))
        check("ncm 那一行标了「网易云加密」，按钮是「解密并翻唱」而不是「用它翻唱」",
              bool(page.js("""(()=>{
                 const rows=Array.from(document.querySelectorAll('#locOut .loc-row'));
                 const r=rows.find(x=>x.textContent.indexOf('网易云加密')>=0);
                 return r ? (r.querySelector('button').textContent.indexOf('解密')>=0) : false;})()""")),
              page.js("""(()=>{const r=Array.from(document.querySelectorAll('#locOut .loc-row'))
                 .find(x=>x.textContent.indexOf('网易云加密')>=0);
                 return r?r.textContent.trim():'没找到 ncm 行'})()""")[:160])

        # 真点「解密并翻唱」：翻唱台拿到的一定是 .mp3，不能是 .ncm。
        # ★ 取证方式：给 stageSetSong 打桩记录实参。
        #   别去读 window.STAGE_SONG —— 它是脚本作用域里的 let，不是 window
        #   属性，读出来永远是 undefined（会假红，还会把真 bug 掩盖掉）。
        #   这条判据同时还盯着另一件事：路径在 DOM 里有没有被转义吃掉
        #   （内联 onclick 塞 Windows 路径 ⇒ 反斜杠没了 ⇒ 后端回"文件不存在"）。
        hooked = page.js("""(()=>{if(typeof window.stageSetSong!=='function') return false;
          window.__set=[]; const o=window.stageSetSong;
          window.stageSetSong=function(p,n){window.__set.push([String(p),String(n||'')]);
            return o.apply(this,arguments);}; return true;})()""")
        check("能给翻唱台的选歌函数打桩（取证手段本身有效）", hooked is True)
        btn = page.js("""(()=>{const rows=Array.from(document.querySelectorAll('#locOut .loc-row'));
          const r=rows.find(x=>x.textContent.indexOf('网易云加密')>=0);
          if(!r) return null; r.id='__ncmbtn'; r.querySelector('button').id='__ncmbtn2'; return true;})()""")
        if not btn:
            check("真鼠标点「解密并翻唱」", False, "没找到那一行")
        else:
            page.click("#__ncmbtn2", "「解密并翻唱」按钮")
            got = poll(page, "(()=>{const a=window.__set||[]; return a.length>0})()",
                       True, 60)
            arg = (page.js("(()=>{const a=window.__set||[]; return a.length?a[0][0]:''})()") or "")
            check("★ 真点「解密并翻唱」后，翻唱台拿到的是解密后的 .mp3（不是 .ncm）",
                  got and arg.lower().endswith(".mp3"), "stageSetSong 收到=%s" % arg)
            check("★ 路径在 DOM 里没被转义吃掉（盘符和反斜杠都在）",
                  bool(re.match(r"^[A-Za-z]:[\\/]", arg)), arg[:80])
            check("翻唱台②号槽显示了歌名",
                  bool(page.js("""(()=>{const t=(document.querySelector('#slotSong')||{}).textContent||'';
                     return t.indexOf('已就位')>=0 || t.indexOf('🎵')>=0})()""")),
                  (page.js("((document.querySelector('#slotSong')||{}).textContent||'').trim()") or "")[:80])

        errs = page.js_errors()
        check("全程没有 JS 异常", not errs, ("; ".join(errs))[:220] or "干净")
    finally:
        with contextlib.suppress(Exception):
            page.ws.close()
        proc.kill()


def main():
    os.chdir(HERE)
    print("门禁：网易云 .ncm 解密")
    src = _real_ncm()
    print("  用真文件做判据：%s" % (src or "（没找到）"))
    src = section_a(src)
    section_b(src)
    section_c(src)
    try:
        section_d(src)
    except Exception as e:
        check("浏览器段跑完（没崩）", False, "%s: %s" % (type(e).__name__, e))
    print("-" * 62)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 失败项：" + "、".join(BAD))
    else:
        print("结论：ncm 解密门禁全绿")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
