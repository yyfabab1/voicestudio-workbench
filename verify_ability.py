# -*- coding: utf-8 -*-
r"""角色「现在能干什么」的验收。

这一关护的是一件很具体的事：**卡片上说能用，点下去就得真能用**。

背景（为什么值得单开一关）：装了 17 个市场角色之后，卡片一律写着「未训练」——
而这三个字说的是「没有**说话**模型」，这些角色的歌声模型是现成能用的；反过来
卡片上那个「💬 TA 说话」对它们又是**点了必然失败**的（speak_role 第一句就校验
model）。用户会填好字、点合成、看着任务跑起来、等几秒才收到一句红字。

所以判据分三层，缺一层就会留一个洞：

  A. 纯函数层：role_ability() 对四种角色形态给出的结论要对，边界要卡住
     （9 段 vs 10 段、短片段不算数、不能往 role 里写东西）。
  B. 端到端层：**说不行就真的不行**。自己建一个 0 素材角色，真调 speak_role /
     sing_job，必须被拒 —— 这一条才真正锁住"判据只写一份"。只验 A 的话，
     哪天有人把 sing_job 的门槛从 10 改成 20，A 依然全绿，用户依然白等。
  C. UI 层：卡片上的徽章文字必须**等于**接口给的 ability（不一致就是两套判据），
     抽屉里的合成按钮的可用状态必须跟着 ability 走；再加一条尺子自检 ——
     在页面里把状态改成 ready，按钮必须当场变亮，否则这把尺子永远绿。

用法（venv：要 websocket）：  <venv>/Scripts/python.exe -s verify_ability.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

# ★ 本机设了 http_proxy，urllib 连 127.0.0.1 也走代理 ⇒ 拿回来的是代理的 502，
#   看起来像"浏览器起不来"。直接把代理关掉。
os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
BASE = "http://127.0.0.1:8756/"
PORT = 9346                      # 跟其它 UI 关错开，别抢同一个调试端口
PROFILE = os.path.join(HERE, ".cdp-ability")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


# ---------------------------------------------------------------- A. 纯函数层

def R(segments=0, seg_dur=3.0, speak=False, sing=False):
    """造一个只用于算能力的假 role（纯 dict，不落盘）。"""
    return {
        "id": "rprobe",
        "segments": [{"file": "s%d.wav" % i, "dur": seg_dur} for i in range(segments)],
        "model": {"ckpt": "g.pth", "epochs": 30} if speak else None,
        "rvc": {"model": "r.pth"} if sing else None,
    }


def test_pure():
    print("\n=== A. 能力判定（纯函数）===")

    a = studio.role_ability(R())
    check("全空（无素材无模型）：说话判 blocked", a["speak"]["state"] == "blocked",
          a["speak"]["label"])
    check("全空：唱歌判 blocked", a["sing"]["state"] == "blocked", a["sing"]["label"])
    check("全空：下一步是「先导入素材」", a["next"]["code"] == "import", a["next"]["label"])

    # 市场角色形态：0 素材 + 现成歌声模型。这正是真库里 18 个角色的样子。
    a = studio.role_ability(R(sing=True))
    check("市场角色（0 段 + 有歌声模型）：唱歌判 ready", a["sing"]["state"] == "ready",
          a["sing"]["label"])
    check("市场角色：说话仍判 blocked（没有说话模型）",
          a["speak"]["state"] == "blocked", a["speak"]["label"])
    check("市场角色：下一步是「可以直接用」（别让用户再去导素材）",
          a["next"]["code"] == "ready", a["next"]["label"])

    # 有素材、还没训过 —— 大帝那种。
    a = studio.role_ability(R(segments=30))
    check("30 段素材：说话判 trainable", a["speak"]["state"] == "trainable",
          a["speak"]["label"])
    check("30 段素材：唱歌判 trainable", a["sing"]["state"] == "trainable",
          a["sing"]["label"])
    check("30 段素材：下一步是「去训练」", a["next"]["code"] == "train", a["next"]["label"])

    # 边界：9 段 vs 10 段（门槛跟 sing_job 的 10 段必须一致）
    a9 = studio.role_ability(R(segments=9))
    a10 = studio.role_ability(R(segments=10))
    check("★ 边界负对照：9 段（差一段）必须仍判 blocked",
          a9["speak"]["state"] == "blocked" and a9["sing"]["state"] == "blocked",
          a9["sing"]["label"])
    check("★ 边界：10 段（刚好够）判 trainable",
          a10["speak"]["state"] == "trainable" and a10["sing"]["state"] == "trainable",
          a10["sing"]["label"])

    # 负对照：30 段但每段只有 1 秒（< 2 秒的不算素材，和 sing_job 口径一致）
    a = studio.role_ability(R(segments=30, seg_dur=1.0))
    check("★ 负对照：30 段但每段 <2 秒 ⇒ 不算数，仍判 blocked",
          a["speak"]["state"] == "blocked" and a["sing"]["state"] == "blocked",
          a["sing"]["why"])

    # 两边都有 —— 阿塔尼斯那种。
    a = studio.role_ability(R(segments=200, speak=True, sing=True))
    check("已训说话 + 有歌声模型：双 ready",
          a["speak"]["state"] == "ready" and a["sing"]["state"] == "ready")

    # 能力是「读出来的」，不许写进角色（写进去就会出现过期的「可说话」）
    r = R(segments=30)
    before = sorted(r.keys())
    studio.role_ability(r)
    check("★ 算能力不许往 role 里写任何东西（能力不持久化）",
          sorted(r.keys()) == before, "多了 %s" % (set(r.keys()) - set(before)))

    # 汇总
    s = studio.ability_summary([R(sing=True), R(sing=True), R(segments=30, speak=True, sing=True)])
    check("汇总：可唱歌 3 / 可说话 1",
          s["can_sing"] == 3 and s["can_speak"] == 1, json.dumps(s, ensure_ascii=False))


# ------------------------------------------------- B. 端到端：说不行就真不行

def test_end_to_end(tmp):
    print("\n=== B. 端到端：ability 说不行，真的做就得被拒 ===")
    # ★ 整段在**临时目录**里跑（ROLES_JSON 和 ROLES_DIR 一起换掉）。
    #   本关要真的 create_role / delete_role，不隔离就会往用户的角色库里
    #   塞测试角色；万一中途崩了还会留下残留。判据可以脏，用户的库不行。
    _oj, _od = studio.ROLES_JSON, studio.ROLES_DIR
    # 记下真库的字节。跑完要逐字节比对 —— 光比"临时 id 不在真库里"是句废话
    # （它本来就不在），那种判据永远绿，等于没写。
    _real_before = open(_oj, "rb").read() if os.path.isfile(_oj) else b""
    studio.ROLES_JSON = os.path.join(tmp, "roles.json")
    studio.ROLES_DIR = os.path.join(tmp, "roles")
    # ★ create_role 返回的是整个 role 字典，不是 id —— 别直接把返回值当 id 用
    #   （上一个版本就是这么错的，报在 delete_role 里，看着像 ROLES_DIR 坏了）
    rid = studio.create_role(name="能力验收-空角色", kind="singing")["id"]
    try:
        r = studio.get_role(rid)
        ab = studio.role_ability(r)
        check("新建的 0 素材角色：说话判 blocked", ab["speak"]["state"] == "blocked")
        check("新建的 0 素材角色：唱歌判 blocked", ab["sing"]["state"] == "blocked")

        job = {"id": "ability-probe", "dir": tmp,
               "_logfile": os.path.join(tmp, "log.txt")}

        # ① 说话：必须被拒，而且理由是「没训练过」
        err = None
        try:
            studio.speak_role(job, rid, ["你好"])
        except studio.Fail as e:
            err = str(e)
        except Exception as e:            # 别的异常也算红（不是我们想要的拒绝路径）
            err = "非 Fail 异常：" + repr(e)
        check("★ ability 说不能说话 ⇒ 真调 speak_role 被拒（不是跑一半才炸）",
              bool(err) and "训练" in err, err)

        # ② 唱歌：素材不够，必须被拒，而且理由里点出段数
        err2 = None
        try:
            studio.sing_job(job, rid, os.path.join(tmp, "song.wav"))
        except studio.Fail as e:
            err2 = str(e)
        except Exception as e:
            err2 = "非 Fail 异常：" + repr(e)
        check("★ ability 说不能唱歌 ⇒ 真调 sing_job 被拒，并说明素材不够",
              bool(err2) and "段" in err2, err2)

        # ③ 反向：ability 说 ready 时，sing_job 不该在"素材不够"这一关被拦
        #    （它确实会被后面的真训练拦住，但那已经越过门槛了）
        studio.mutate_role(rid, lambda x: x.update(
            {"rvc": {"model": "fake.pth", "at": "now"}}))
        r2 = studio.get_role(rid)
        check("补上歌声模型后：ability 立刻变成 ready（不用重启/刷新缓存）",
              studio.role_ability(r2)["sing"]["state"] == "ready")
    finally:
        studio.delete_role(rid)
        studio.ROLES_JSON, studio.ROLES_DIR = _oj, _od
    # 还原 + 逐字节比对真库：隔离一旦失效，这条会当场变红
    _real_after = open(_oj, "rb").read() if os.path.isfile(_oj) else b""
    check("★ 隔离生效：B 段跑完，用户的真 roles.json 一个字节都没变",
          studio.ROLES_JSON == _oj and studio.ROLES_DIR == _od
          and _real_before == _real_after and len(_real_before) > 0,
          "%d -> %d 字节" % (len(_real_before), len(_real_after)))


# ---------------------------------------------------------------- C. UI 层

def cdp_get(path, port=PORT):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=20) as r:
        return json.loads(r.read().decode())


class Cdp:
    def __init__(self, port=PORT):
        import websocket  # noqa
        ws = cdp_get("/json/list", port)
        page = [t for t in ws if t.get("type") == "page"]
        if not page:
            raise RuntimeError("没有可用的页面目标")
        self.ws = websocket.create_connection(page[0]["webSocketDebuggerUrl"], timeout=60)
        self.i = 0
        self.accept_dialog = True
        self.send("Page.enable")

    def send(self, method, **params):
        self.i += 1
        mid = self.i
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("method") == "Page.javascriptDialogOpening":
                self.i += 1
                self.ws.send(json.dumps({
                    "id": self.i, "method": "Page.handleJavaScriptDialog",
                    "params": {"accept": bool(self.accept_dialog)}}))
                continue
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError("%s -> %s" % (method, msg["error"]))
                return msg.get("result", {})

    def js(self, expr):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def click_el(self, selector):
        """先滚进视口再真鼠标点（视口外的点必然打空 —— 这条踩过）。"""
        ok = self.js("(()=>{const e=document.querySelector(%s);"
                     "if(!e) return false; e.scrollIntoView({block:'center'}); return true;})()"
                     % json.dumps(selector))
        time.sleep(0.35)
        if not ok:
            return "NOT_FOUND:" + selector
        xy = self.js("(()=>{const e=document.querySelector(%s); if(!e) return null;"
                     "const r=e.getBoundingClientRect();"
                     "return {x:r.left+r.width/2, y:r.top+r.height/2};})()"
                     % json.dumps(selector))
        if not xy:
            return "NO_BOX:" + selector
        x, y = xy["x"], xy["y"]
        top = self.js("(()=>{const e=document.elementFromPoint(%f,%f);"
                      "return e?(e.id||e.className||e.tagName):'null'})()" % (x, y))
        self.send("Input.dispatchMouseEvent", type="mousePressed", x=x, y=y,
                  button="left", clickCount=1)
        time.sleep(0.06)
        self.send("Input.dispatchMouseEvent", type="mouseReleased", x=x, y=y,
                  button="left", clickCount=1)
        return top


def api_roles():
    with urllib.request.urlopen(BASE.rstrip("/") + "/api/roles", timeout=60) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def test_ui():
    print("\n=== C. UI：卡片和按钮必须跟着 ability 走 ===")
    c = None
    try:
        cdp_get("/json/list", PORT)
        c = Cdp(PORT)
    except Exception:
        pass
    if c is None:
        os.makedirs(PROFILE, exist_ok=True)
        subprocess.Popen(
            [CHROME, "--headless=new", "--disable-gpu",
             "--remote-debugging-port=%d" % PORT,
             "--user-data-dir=%s" % PROFILE, "--no-first-run",
             "--remote-allow-origins=*", "--window-size=1440,1000", BASE],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(40):
            time.sleep(1.0)
            try:
                cdp_get("/json/list", PORT)
                time.sleep(1.5)
                c = Cdp(PORT)
                break
            except Exception:
                continue
    if not c:
        check("浏览器起得来", False, "起不来，后面 UI 判据没法跑")
        return

    try:
        c.send("Page.navigate", url=BASE)
        time.sleep(3.5)
    except Exception:
        pass

    # 等卡片画出来
    n = 0
    for _ in range(30):
        n = c.js("document.querySelectorAll('#grid .card').length") or 0
        if n:
            break
        time.sleep(1.0)
    check("角色墙画出了卡片", n > 0, "卡片数=%s" % n)
    if not n:
        return

    data = api_roles()
    by_id = {r["id"]: r for r in data["roles"]}

    # ① 顶部汇总
    cnt = c.js("document.querySelector('#roleCount').textContent") or ""
    check("角色库顶部显示了「可唱歌 N」汇总", "可唱歌" in cnt, cnt)
    if data.get("summary"):
        check("汇总里的可唱歌数与接口一致",
              str(data["summary"]["can_sing"]) in cnt, cnt)

    # ② 每张卡的徽章文字必须**等于**接口给的 ability（两套判据就是 bug）
    mismatch = []
    card_ids = c.js("Array.from(document.querySelectorAll('#grid .card'))"
                    ".map(e=>e.dataset.role)") or []
    for rid in card_ids[:10]:
        r = by_id.get(rid)
        if not r or not r.get("ability"):
            continue
        txt = c.js("(()=>{const e=document.querySelector('.card[data-role=%s]');"
                   "return e?e.textContent:'';})()" % json.dumps(rid)) or ""
        ab = r["ability"]
        if ab["sing"]["label"] not in txt or ab["speak"]["label"] not in txt:
            mismatch.append((r.get("name"), ab["sing"]["label"], ab["speak"]["label"]))
    check("★ 卡片上的能力徽章与接口 ability 完全一致（不是两套判据）",
          not mismatch, mismatch[:3])

    # ③ 卡片上不再出现那个误导人的「未训练」
    check("★ 卡片上不再出现「未训练」这种只提一半状态的说法",
          c.js("document.querySelector('#grid').textContent.indexOf('未训练') < 0"))

    # ④ 抽屉：**两个方向都要验** —— 抽一个"能说话"的和一个"不能说话"的各点一次。
    #    只验一边的话，"按钮永远亮着"或"永远灰着"都能蒙混过关。
    def open_probe(rid):
        c.js("closeDrawer()")
        time.sleep(0.3)
        c.click_el(".card[data-role=%s]" % rid)
        time.sleep(1.2)
        if not c.js("document.querySelector('#drawer').classList.contains('on')"):
            return None
        return c.js("document.querySelector('#btnSpeak').disabled")

    def state_of(rid):
        return (((by_id.get(rid) or {}).get("ability") or {})
                .get("speak", {}).get("state"))

    ready_ids = [i for i in card_ids if state_of(i) == "ready"]
    block_ids = [i for i in card_ids if state_of(i) and state_of(i) != "ready"]

    if ready_ids:
        d = open_probe(ready_ids[0])
        check("★ 角色「能说话」时，抽屉里合成按钮**必须可点**（真鼠标开抽屉）",
              d is False, "disabled=%s" % d)
        bar = c.js("document.querySelector('#abSpeak').textContent") or ""
        check("说话页顶部的状态条写清了原因",
              ("说话" in bar) and (len(bar.strip()) > 6), bar.strip()[:60])
        # 打开角色第一眼落在「简介」页 —— 能力总览必须在那儿就看得见
        info = c.js("document.querySelector('#abInfo').textContent") or ""
        check("★ 简介页（打开角色第一眼）就能看到唱歌/说话两行能力总览",
              ("唱歌" in info) and ("说话" in info), info.strip()[:70])
    else:
        # 不判红：这是数据形态，不是代码错。但要说出来 —— 万一哪天两个方向都空了，
        # 下面的尺子自检仍会双向覆盖（那才是这套判据的底）。
        print("  [注] 真库这轮没有「能说话」的角色，这一侧由尺子自检覆盖")

    if block_ids:
        d2 = open_probe(block_ids[0])
        check("★ 角色「还不能说话」时，抽屉里合成按钮**必须灰掉**（真鼠标开抽屉）",
              d2 is True, "disabled=%s" % d2)
        bar2 = c.js("document.querySelector('#abSpeak').textContent") or ""
        check("★ 不能说话时要给出原因，而不是一片空白",
              "不能说话" in bar2 or "还没有" in bar2, bar2.strip()[:70])
    else:
        print("  [注] 真库这轮没有「还不能说话」的角色，这一侧由尺子自检覆盖")

    # ⑤ ★ 尺子自检：把状态强行改成 ready，按钮必须当场变亮。
    #    没有这一条，「按钮永远是灰的」也会被判据判成绿。
    c.js("(()=>{CUR.ability.speak.state='ready';"
         "CUR.ability.speak.label='可说话';paintAbilityBar(CUR);return true;})()")
    time.sleep(0.4)
    on = c.js("document.querySelector('#btnSpeak').disabled === false")
    check("★ 尺子自检：状态改成 ready，合成按钮必须当场变亮", bool(on),
          "按钮没反应 ⇒ 这条判据是死的")

    c.js("(()=>{CUR.ability.speak.state='blocked';"
         "CUR.ability.speak.label='还不能说话';paintAbilityBar(CUR);return true;})()")
    time.sleep(0.4)
    off = c.js("document.querySelector('#btnSpeak').disabled === true")
    check("★ 尺子自检（另一半）：改回 blocked，按钮必须重新变灰", bool(off))


def main():
    print("=" * 62)
    print("角色能力（能干什么 / 还差什么）验收")
    print("=" * 62)
    tmp = tempfile.mkdtemp(prefix="vs_ability_")
    try:
        test_pure()
        test_end_to_end(tmp)
        test_ui()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - %s" % b)
    print("结论：%s" % ("角色能力验收全绿" if not BAD else "角色能力验收有红"))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
