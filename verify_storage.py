r"""存储清理的判据自检（真浏览器 + 真接口，正/负对照齐全）。

要证明的四件事：
  1. 扫描**真的扫到了**磁盘上的东西（不是永远返回空的壳）；
  2. 「角色正在用的权重」**真的被锁住**，而且锁来自角色引用（负对照：把
     保护表清空后，同一项必须解锁 —— 否则说明 lock 是写死的假象）；
  3. 删除准入**真的在拦**（上锁的、越界的都必须删不掉）；
     负对照：把准入换成永远放行，同一请求必须放行 —— 没红过 = 没验过；
  4. 界面上真的能勾、能二次确认、能取消、点了才真删（真鼠标）。

★ 全程用 dry=True 或几 KB 的探针文件验证"拦得住/删得掉"，
  **绝不在判据里真删用户的权重** —— 真删由人去点界面。

用法：<venv>/Scripts/python.exe verify_storage.py
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

import websocket

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402

PORT = 9335
PROFILE = os.path.join(HERE, ".chrome-store")
URL = "http://127.0.0.1:8756/"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
if not os.path.isfile(CHROME):
    for c in (r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
              r"C:\Users\yyfabab\AppData\Local\Google\Chrome\Application\chrome.exe"):
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
          return {x:x,y:y,w:r.width,h:r.height,vw:innerWidth,vh:innerHeight,
                  inView:(r.top>=0&&r.bottom<=innerHeight&&r.left>=0&&r.right<=innerWidth)};
        })()""" % json.dumps(sel))

    def click(self, sel, label=""):
        b = self.box(sel)
        self.last = b
        if b.get("err"):
            check("能点到 %s" % (label or sel), False, b["err"])
            return False
        if not b["inView"]:
            check("能点到 %s" % (label or sel), False,
                  "rect=%sx%s 视口=%sx%s -> 视口外，真鼠标必然打空"
                  % (b["w"], b["h"], b["vw"], b["vh"]))
            return False
        self.mouse(b["x"], b["y"], "mousePressed")
        time.sleep(0.06)
        self.mouse(b["x"], b["y"], "mouseReleased")
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


# ---------------------------------------------------------------- 探针
PROBE_UP = os.path.join(studio.UPLOADS, "_vs_probe_store.bin")
RVCW = studio._rvc_weights_dir()
PROBE_W = [os.path.join(RVCW, "_vsprobe_e%d_s%d.pth" % (e, e * 8))
           for e in (15, 30, 45, 60, 75)]
PROBE_CACHE = os.path.join(studio.SEP_CACHE, "_vsprobe_a")


def probe_up():
    with open(PROBE_UP, "wb") as f:
        f.write(b"\0" * (1 << 20))      # 1 MB，够看得见大小又不会拖慢扫描


def probe_weights():
    os.makedirs(RVCW, exist_ok=True)
    for p in PROBE_W:
        with open(p, "wb") as f:
            f.write(b"\0" * 4096)


def probe_cache():
    os.makedirs(PROBE_CACHE, exist_ok=True)
    with open(os.path.join(PROBE_CACHE, "vocal.wav"), "wb") as f:
        f.write(b"\0" * 4096)


# ---------------------------------------------------------- 自足探针角色 --
# 「角色正在用的权重必须被锁」这条判据，需要一个**绑了歌声模型的角色**。
# 以前指望库里恰好有一个（用户把角色删了就红在数据前提上，而不是测到了什么）——
# 判据要自足，所以这里自己种一个：探针角色 + 探针权重文件，跑完清掉。
PROBE_EXP = "_storeprobe"
PROBE_ROLE_ID = [None]
PROBE_PTHS = []


def ensure_probe_role():
    WD = studio._rvc_weights_dir()
    GD = studio.GPT_WEIGHT_DIR
    os.makedirs(WD, exist_ok=True)
    # 5 份：一份"最终权重"（无轮数后缀）+ 4 份中间存档（_e<n>_s<m>），
    # 正好覆盖扫描器的两种形状（final / archive），也让"≥5 个"这条判据有东西可数。
    want = [PROBE_EXP + ".pth"] + [
        "%s_e%d_s%d.pth" % (PROBE_EXP, e, e * 10) for e in (10, 30, 60, 90)]
    for f in want:
        p = os.path.join(WD, f)
        if not os.path.isfile(p):
            with open(p, "wb") as fh:
                fh.write(b"\0" * (256 * 1024))
        PROBE_PTHS.append(p)
    ck = os.path.join(GD, PROBE_EXP + "-e5.ckpt")
    os.makedirs(GD, exist_ok=True)
    if not os.path.isfile(ck):
        with open(ck, "wb") as fh:
            fh.write(b"\0" * (256 * 1024))
    PROBE_PTHS.append(ck)
    role = studio.create_role(name="_storeprobe(探针)")
    rid = role["id"]
    PROBE_ROLE_ID[0] = rid
    studio.mutate_role(rid, lambda r: r.__setitem__(
        "rvc", {"model": PROBE_EXP + ".pth", "epochs": 90, "at": studio._now(),
                "weight_epoch": 90, "pretrained": True, "index": False}))
    studio.mutate_role(rid, lambda r: r.__setitem__(
        "model", {"ckpt": "GPT_weights_v5turbo/" + PROBE_EXP + "-e5.ckpt",
                  "epochs": 5, "at": studio._now()}))
    return rid


def cleanup_probe_role():
    rid = PROBE_ROLE_ID[0]
    if rid:
        try:
            studio.delete_role(rid)
        except Exception:
            pass
        PROBE_ROLE_ID[0] = None
    for p in list(PROBE_PTHS):
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass
    PROBE_PTHS[:] = []


def cleanup_probes():
    for p in [PROBE_UP] + PROBE_W:
        try:
            if os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass
    try:
        if os.path.isdir(PROBE_CACHE):
            shutil.rmtree(PROBE_CACHE, ignore_errors=True)
    except Exception:
        pass
    # 瘦身探针会在 data/_trash_log 留下清单，几 KB，顺手清掉免得堆一堆
    lg = getattr(studio, "TRASH_LOG_DIR", None)
    if lg and os.path.isdir(lg):
        for f in os.listdir(lg):
            if f.startswith("deleted_"):
                try:
                    os.remove(os.path.join(lg, f))
                except Exception:
                    pass


def item_by_name(snap, needle):
    for g in snap.get("groups", []):
        for it in g.get("items", []):
            if needle in it.get("name", ""):
                return it
    return None


# ------------------------------------------------------- 1~7：接口与判据
def section_scan():
    print("\n=== 1. 扫描：真的扫到了磁盘上的东西 ===")
    snap = studio.scan_storage()
    tot = snap.get("total") or 0
    check("扫描返回了分组且总量 > 0", len(snap.get("groups", [])) > 0 and tot > 0,
          "分组 %d 个，共 %s" % (len(snap.get("groups", [])), snap.get("total_s")))
    ids = [g["id"] for g in snap["groups"] if g.get("count")]
    check("扫到了 rvc_weight / job / upload / pretrained 这几类",
          all(k in ids for k in ("rvc_weight", "job", "upload", "pretrained")),
          "实际有：%s" % ",".join(ids))
    g = [x for x in snap["groups"] if x["id"] == "rvc_weight"][0]
    # 探针自己种了 5 份权重（1 份最终 + 4 份中间存档）：扫到了才说明扫描器真在读盘
    check("歌声模型权重那一组能扫到磁盘上的真实文件（≥ 5 个）", g["count"] >= 5,
          "%d 个 / %s" % (g["count"], g["size_s"]))
    names = {os.path.basename(it["path"]) for it in g["items"]}
    want = {os.path.basename(p) for p in PROBE_PTHS if p.endswith(".pth")}
    check("种下的 5 份探针权重一份不落地出现在扫描结果里（证明是按目录读的）",
          want <= names, "缺：%s" % sorted(want - names))
    # 负对照：把所有扫描器换成返回空 —— 总量必须变 0，否则说明
    # "总量"这个数字根本不是从扫描结果算出来的（写死的假象）
    real = dict(studio._SCANNERS)
    try:
        studio._SCANNERS.clear()
        empty = studio.scan_storage()
        check("负对照：扫描器全换成空壳后总量必须是 0（证明数字来自真实扫描）",
              (empty.get("total") or 0) == 0, "得到 %s" % empty.get("total_s"))
    finally:
        studio._SCANNERS.update(real)
    return snap


def section_lock(snap):
    print("\n=== 2. 保护：角色正在用的权重必须被锁 ===")
    db = studio.load_roles()
    roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
    roles = roles if isinstance(roles, list) else list(roles.values())
    used = [r for r in roles if (r.get("rvc") or {}).get("model")]
    check("库里至少有一个角色绑了歌声模型（否则锁无从验证）", bool(used),
          "绑了模型的角色 %d 个" % len(used))
    if not used:
        return
    model = used[0]["rvc"]["model"]
    it = item_by_name(snap, model)
    check("界面能看见这份权重（%s）" % model, it is not None, "")
    check("它被标记为不可删（lock 非空）", bool(it and it.get("lock")),
          (it or {}).get("lock"))
    check("勾选框不该被预选（safe=False 且 suggest=False）",
          bool(it) and it["safe"] is False and it["suggest"] is False,
          "safe=%s suggest=%s" % ((it or {}).get("safe"), (it or {}).get("suggest")))
    pt = item_by_name(snap, "f0G40k.pth")
    check("底模也被锁住", bool(pt and pt.get("lock")), (pt or {}).get("lock"))
    # 负对照：把保护表掏空，同一项必须解锁 —— 证明 lock 来自真实引用
    real = studio._protected_map
    try:
        studio._protected_map = lambda: {}
        snap2 = studio.scan_storage()
        it2 = item_by_name(snap2, model)
        check("负对照：保护表掏空后它必须解锁（证明锁来自角色引用，不是写死）",
              bool(it2) and not it2.get("lock"), "lock=%r" % (it2 or {}).get("lock"))
    finally:
        studio._protected_map = real


def section_block(snap):
    print("\n=== 3. 删除准入：上锁的 / 越界的必须删不掉（dry，不真删）===")
    db = studio.load_roles()
    roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
    roles = roles if isinstance(roles, list) else list(roles.values())
    model = None
    for r in roles:
        if (r.get("rvc") or {}).get("model"):
            model = r["rvc"]["model"]
            break
    if model:
        p = os.path.join(studio._rvc_weights_dir(), model)
        res = studio.storage_delete([p], dry=True)
        check("删角色正在用的权重 -> 被拒，且理由非空",
              len(res["failed"]) == 1 and bool(res["failed"][0]["why"]),
              (res["failed"] or [{}])[0].get("why"))
        check("被拒之后文件当然还在", os.path.isfile(p), p)
    out = studio.storage_delete([os.path.join(HERE, "data", "roles.json")], dry=True)
    check("删白名单外的文件（data/roles.json）-> 被拒",
          len(out["failed"]) == 1 and "允许清理" in (out["failed"][0]["why"] or ""),
          (out["failed"] or [{}])[0].get("why"))
    # 负对照：把准入换成永远放行 —— 上面那条必须变成"放行"。
    # 没红过 = 没验过：如果这里也"拦截"，说明拦它的是别的东西（或判据写反了）。
    real = studio._delete_block_reason
    try:
        # ★ 签名要带上 force —— 后端那个函数多了这个参数（"允许连正在用的
        #   模型一起删"），这里替换的替身必须同签名，否则一调用就 TypeError，
        #   看起来像产品炸了，其实是替身自己没跟上。
        studio._delete_block_reason = lambda ap, prot, force=False: ""
        ok = studio.storage_delete([os.path.join(HERE, "data", "roles.json")], dry=True)
        check("负对照：准入换成永远放行后，同一条必须放行（证明拦它的是准入判断）",
              len(ok["failed"]) == 0 and len(ok["deleted"]) == 1,
              "failed=%d deleted=%d" % (len(ok["failed"]), len(ok["deleted"])))
    finally:
        studio._delete_block_reason = real
    check("还原后仍然拦得住（没被负对照污染）",
          len(studio.storage_delete([os.path.join(HERE, "data", "roles.json")],
                                    dry=True)["failed"]) == 1)


def section_real_delete():
    print("\n=== 4. 真删：探针文件删掉后文件必须消失，释放量对得上 ===")
    probe_up()
    sz = os.path.getsize(PROBE_UP)
    snap = studio.scan_storage()
    it = item_by_name(snap, "_vs_probe_store.bin")
    check("新造的探针文件能被扫到（说明扫描是每次现算，不是陈年缓存）", it is not None,
          "大小 %d" % sz if it else "没扫到")
    res = studio.storage_delete([PROBE_UP])
    check("删除成功且释放量 = 文件真实大小",
          len(res["deleted"]) == 1 and res["freed"] == sz,
          "freed=%d 实际=%d" % (res.get("freed", 0), sz))
    check("文件真的不在了（不是只改了界面上的数字）", not os.path.exists(PROBE_UP))
    check("留了一份删除清单（真删错了至少知道删过什么）",
          bool(res.get("log")) and os.path.isfile(res["log"] or ""), res.get("log"))
    # 负对照：不存在的路径 -> 必须报"已经不在了"，而不是假装成功
    res2 = studio.storage_delete([PROBE_UP])
    check("负对照：删一个已经不存在的路径 -> 必须报失败而不是假装成功",
          len(res2["failed"]) == 1 and "不在" in (res2["failed"][0]["why"] or ""),
          (res2["failed"] or [{}])[0].get("why"))


def section_prune_weights():
    print("\n=== 5. 训练后瘦身：只留最近 N 份中间存档 ===")
    probe_weights()
    try:
        r = studio.prune_rvc_archives("_vsprobe", keep=2)
        left = sorted(os.path.basename(p) for p in PROBE_W if os.path.isfile(p))
        check("keep=2 时删掉 3 份、留 2 份（e60/e75）",
              len(r["deleted"]) == 3 and left == ["_vsprobe_e60_s480.pth",
                                                  "_vsprobe_e75_s600.pth"],
              "删了 %d，剩 %s" % (len(r["deleted"]), left))
        check("释放量 = 3 × 4096", r["freed"] == 3 * 4096, r.get("freed_s"))
    finally:
        pass
    # 负对照：keep 给一个比总数还大的值 -> 一份都不该删。
    # 这条同时防住"瘦身把**全部**存档都删了却没人发现"。
    for p in PROBE_W:
        if not os.path.isfile(p):
            with open(p, "wb") as f:
                f.write(b"\0" * 4096)
    r2 = studio.prune_rvc_archives("_vsprobe", keep=99)
    check("负对照：keep=99 时一份都不删（证明删多少真的由 keep 决定）",
          len(r2["deleted"]) == 0 and len(r2["failed"]) == 0,
          "deleted=%d" % len(r2["deleted"]))
    # 上锁的那一份永远不删：把 e15 假装成"角色在用"
    real = studio._protected_map
    try:
        studio._protected_map = lambda: {studio._nc(PROBE_W[0]): "角色「探针」正在用它唱歌"}
        r3 = studio.prune_rvc_archives("_vsprobe", keep=0)
        check("上锁的那一份即使 keep=0 也不删（双保险）",
              os.path.isfile(PROBE_W[0])
              and all(not os.path.isfile(p) for p in PROBE_W[1:]),
              "e15 还在 = %s" % os.path.isfile(PROBE_W[0]))
    finally:
        studio._protected_map = real
        for p in PROBE_W:
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except Exception:
                    pass


def section_prune_cache():
    print("\n=== 6. 缓存 LRU：超上限从最旧的开始删 ===")
    probe_cache()
    # 上限压到 ~1KB，缓存必然超标。
    # 删几个是不定的（曲库里可能本来就有别的缓存目录，LRU 会从最旧的开始一路删），
    # 所以这里只断言"探针被删 + 至少删了一个"，不断言精确个数。
    r = studio.prune_cache(max_gb=0.000001)
    check("超限的缓存目录被删掉", len(r["deleted"]) >= 1 and not os.path.isdir(PROBE_CACHE),
          "deleted=%d，探针还在=%s" % (len(r["deleted"]), os.path.isdir(PROBE_CACHE)))
    check("目录真的没了", not os.path.isdir(PROBE_CACHE))
    probe_cache()
    r2 = studio.prune_cache(max_gb=50)           # 负对照：上限给很大 -> 不删
    check("负对照：上限 50GB 时什么都不删（证明删是由上限触发的）",
          len(r2["deleted"]) == 0 and os.path.isdir(PROBE_CACHE),
          "deleted=%d" % len(r2["deleted"]))
    shutil.rmtree(PROBE_CACHE, ignore_errors=True)


# ------------------------------------------------------- 8：真浏览器
def section_ui(page):
    print("\n=== 7. 界面：真鼠标打开面板、勾一项、二次确认、真的删掉 ===")
    check("顶栏有「存储」按钮", bool(page.js("!!document.querySelector('#btnStore')")))
    if not page.click("#btnStore", "顶栏「存储」按钮"):
        return
    ok = poll(page, "document.querySelectorAll('#storeList .store-it').length > 3")
    nrow = page.js("document.querySelectorAll('#storeList .store-it').length")
    check("面板打开后列表真的渲染出来了（> 3 行）", ok, "行数=%s" % nrow)
    stat = page.js("""({
      free:(document.querySelector('#stFree')||{}).textContent||'',
      total:(document.querySelector('#stTotal')||{}).textContent||'',
      rec:(document.querySelector('#stReclaim')||{}).textContent||''})""")
    check("统计条三个数字都有值", all(v and v != "-" for v in stat.values()), str(stat))
    nlock = page.js("document.querySelectorAll('#storeList .store-it.locked').length")
    check("被锁的项真的渲染成 locked 且勾选框 disabled（用户看得出来删不了）",
          bool(nlock) and page.js(
              "[...document.querySelectorAll('#storeList .store-it.locked')]"
              ".every(r=>r.querySelector('input').disabled)"),
          "锁住 %s 行" % nlock)

    # 点被锁的行 -> 不能进选择（这是"锁"在界面上唯一的作用）
    page.js("""(()=>{const r=document.querySelector('#storeList .store-it.locked');
                     if(!r) return false; r.setAttribute('data-lockprobe','1');
                     return true;})()""")
    if page.js("!!document.querySelector('[data-lockprobe]')"):
        page.click("[data-lockprobe]", "一个被锁住的行")
        time.sleep(0.3)
        check("真鼠标点被锁的行 -> 不会把它选进去",
              "0 项" in (page.js("(document.querySelector('#stPicked')||{}).textContent") or ""),
              page.js("(document.querySelector('#stPicked')||{}).textContent"))
        page.js("document.querySelector('[data-lockprobe]').removeAttribute('data-lockprobe')")

    # ---- 一键勾选 / 清空 ----
    page.click("#stPickSafe", "「勾选安全项」")
    time.sleep(0.5)
    picked_txt = page.js("(document.querySelector('#stPicked')||{}).textContent") or ""
    check("点「勾选安全项」后选中的项数 > 0",
          bool(picked_txt) and not picked_txt.startswith("0 项"), picked_txt)
    check("删除按钮随之可用", not page.js("document.querySelector('#stDel').disabled"))
    page.click("#stClear", "「清空选择」")
    time.sleep(0.4)
    check("点「清空选择」后归零，按钮重新变灰",
          (page.js("(document.querySelector('#stPicked')||{}).textContent") or "").startswith("0 项")
          and page.js("document.querySelector('#stDel').disabled"))

    # ---- 造一个探针文件，走完整的「勾 -> 二次确认 -> 删」----
    probe_up()
    page.js("storeScan(true)", True)
    # 这条表达式在下面"删完之后"还要再用一次（want=False）。提到这里定义，
    # 保证前后量的是**同一把尺子**，而不是两处各写一遍、写着写着就不一样了。
    _rows = ("[...document.querySelectorAll('#storeList .store-it')]")
    _has_probe = "!!(%s.find(r=>r.textContent.indexOf('_vs_probe_store.bin')>=0))" % _rows
    ok = poll(page, _has_probe)
    probe_was_listed = bool(ok)      # 留给末尾当负对照：证明这把尺子会变 True
    check("新造的探针文件出现在列表里（界面扫的是此刻的磁盘）", ok)
    if not ok:
        return
    page.js("""(()=>{const r=[...document.querySelectorAll('#storeList .store-it')]
                     .find(x=>x.textContent.indexOf('_vs_probe_store.bin')>=0);
                     r.setAttribute('data-probe','1'); return true;})()""")
    page.click("[data-probe='1']", "探针那一行")
    time.sleep(0.4)
    check("真鼠标点上那一行 -> 计数变成 1 项",
          (page.js("(document.querySelector('#stPicked')||{}).textContent") or "").startswith("1 项"),
          page.js("(document.querySelector('#stPicked')||{}).textContent"))

    # ---- 二次确认：先取消，确认没删 ----
    page.click("#stDel", "「删除选中」")
    time.sleep(0.4)
    check("第一次点「删除选中」只弹二次确认，不直接删",
          page.js("!!document.querySelector('#stDel2')")
          and os.path.isfile(PROBE_UP), "文件还在 = %s" % os.path.isfile(PROBE_UP))
    page.click("#stCancel", "「取消」")
    time.sleep(0.4)
    check("点「取消」后确认条收起，且文件没被删",
          not page.js("!!document.querySelector('#stDel2')")
          and os.path.isfile(PROBE_UP), "取消生效")

    # ---- 二次确认：真删 ----
    page.click("#stDel", "「删除选中」")
    time.sleep(0.3)
    page.click("#stDel2", "「确定删除」")
    ok = poll(page, "((document.querySelector('#storeTip')||{}).textContent||'').indexOf('已删除')>=0")
    tip = page.js("(document.querySelector('#storeTip')||{}).textContent") or ""
    check("确认后真的删了，且提示写明删了几项、释放多少", ok, tip[:120])
    check("文件确实从磁盘上消失了", not os.path.exists(PROBE_UP))
    # ★ 这里必须 poll，不能断言完就查（踩过：一度是假红）。
    #   前端 storeDelete() 是先写"已删除 N 项"这条成功提示、**再** await storeScan()
    #   重扫（app.js:1880 -> 1890）。所以"提示出现"不代表"列表已经重扫完"，
    #   抢在这中间查列表会命中旧行。判据要量的是"重扫之后"，就得等重扫。
    #   另加一条护栏：want=False 的 poll 在列表被清空时也会为真，
    #   所以还要确认列表里**仍有别的行**——否则这条判据量的是"空"而不是"没有它"。
    gone = poll(page, _has_probe, want=False)
    nrows = page.js("%s.length" % _rows) or 0
    check("删完自动重扫，列表里不再有它", gone and nrows > 0,
          "剩余 %s 行（>0 才算重扫真的渲染过，不是列表被清空）" % nrows)
    # ★ 负对照：同一个表达式在文件还在时读到的是 True（上面 probe_was_listed）。
    #   没有这一条，"删完不再有它" 用一句恒 False 的表达式也能蒙过去。
    check("★ 负对照：同一把尺子在文件还在时是 True（证明它量的就是这一行）",
          probe_was_listed, "删前=%s / 删后=%s" % (probe_was_listed, not gone))


def section_ui_negative(page):
    print("\n=== 8. 界面负对照：窄窗口下按钮仍然点得到 ===")
    # 「按钮被挤出视口 => 真鼠标必然打空」这类坑已经栽过两次（暂停按钮、
    # 音乐库搜索按钮）。这里模拟 380×680 的小窗，断言删除按钮仍在视口内。
    page.send("Emulation.setDeviceMetricsOverride", width=380, height=680,
              deviceScaleFactor=1, mobile=False)
    time.sleep(0.6)
    b = page.box("#stDel")
    check("窗口压到 380×680，删除按钮仍在视口内（工具条不随列表滚走）",
          bool(b.get("inView")), "rect y=%s h=%s 视口=%sx%s"
          % (round(b.get("y", -1)), round(b.get("h", -1)), b.get("vw"), b.get("vh")))
    b2 = page.box("#btnStore")
    check("窄窗口下顶栏「存储」按钮也还在视口内", bool(b2.get("inView")),
          "x=%s y=%s" % (round(b2.get("x", -1)), round(b2.get("y", -1))))
    page.send("Emulation.clearDeviceMetricsOverride")
    time.sleep(0.4)
    # 另一条负对照：把数据里所有的 lock 抹掉再重渲染 —— 「有锁住的行」那条
    # 必须跟着消失。证明界面上的锁是**数据驱动**的，而不是写死的 CSS 类。
    # （注意不能只 patch python 侧的 _protected_map：界面数据来自服务端进程，
    #   在验收进程里 patch 根本影响不到它 —— 那是"看起来在验、其实没验到"。）
    page.js("""(()=>{ (STORE.groups||[]).forEach(g=>(g.items||[]).forEach(it=>{
                 it.lock=''; it.safe=true; }));
                 storeRender(); return true;})()""")
    time.sleep(0.5)
    n = page.js("document.querySelectorAll('#storeList .store-it.locked').length")
    check("负对照：数据里的 lock 抹掉后界面上不再有锁住的行"
          "（证明「锁」这条判据量的是数据，不是写死的样式）", n == 0, "还剩 %s 行" % n)
    page.js("storeScan(true)", True)
    time.sleep(1.5)
    n2 = page.js("document.querySelectorAll('#storeList .store-it.locked').length")
    check("重新扫描后又出现锁住的行（没被负对照污染）", n2 > 0, "锁住 %s 行" % n2)


def main():
    cleanup_probes()
    cleanup_probe_role()
    ensure_probe_role()          # 判据自足：自己种一个"绑了歌声模型的角色"
    snap = section_scan()
    section_lock(snap)
    section_block(snap)
    section_real_delete()
    section_prune_weights()
    section_prune_cache()

    os.makedirs(PROFILE, exist_ok=True)
    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu",
        "--remote-debugging-port=%d" % PORT,
        "--remote-allow-origins=*",
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
        tabs = cdp_get("/json/list")
        cand = [t for t in tabs if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        page = Cdp(cand[0]["webSocketDebuggerUrl"])
        page.send("Page.enable")
        page.send("Runtime.enable")
        page.send("Log.enable")
        page.send("Page.navigate", url=URL)
        time.sleep(5.0)
        errs = page.js_errors()
        check("页面加载没有 JS 异常", not errs, ("; ".join(errs))[:240] or "干净")
        check("存储面板相关的函数都在",
              page.js("['storeOpen','storeScan','storeRender','storeDelete',"
                      "'storePrune','storeArm','hsize']"
                      ".every(k=>typeof window[k]==='function')"))
        section_ui(page)
        section_ui_negative(page)
        errs2 = page.js_errors()
        check("整轮界面操作之后仍然没有 JS 异常", not errs2, ("; ".join(errs2))[:240] or "干净")
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
        cleanup_probes()
    cleanup_probe_role()

    print("\n" + "=" * 56)
    print("通过 %d 项，失败 %d 项" % (len(OK), len(BAD)))
    for n in BAD:
        print("  ! %s" % n)
    print("ALLDONE")
    sys.exit(1 if BAD else 0)


if __name__ == "__main__":
    main()
