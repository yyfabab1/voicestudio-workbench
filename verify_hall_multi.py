# -*- coding: utf-8 -*-
r"""门禁 41：大厅多人唱 + 角色内读 VIP 音乐。

为什么单开一关（而不是并进 verify_multi_ui）：
  这两件事是**用户在界面上走不通**，不是某个函数算错 ——
  verify_multi_ui 盯的是"多角色面板里的控件"，本关盯的是"从大厅和从角色内
  这两条入口能不能走到那条链路上"。入口坏了，面板再对也没用。

两层判据（缺一不可）：
  ① **源码接线**（不用浏览器，快）：大厅的多人分支真的发 kind=sing_multi、
     角色内的选歌真的接到曲库/解密链路、两条路共用同一段收尾、唱法档位不漂。
     带 --selftest：把源码改坏一遍，判据必须变红（尺子自检）。
  ② **真浏览器**（probe_hall_multi）：真鼠标点开①号槽凑两个人、真点歌曲、
     真点「解密并用」。源码层看不见"点下去发的是什么"。

★ 服务自己起在 8900，不碰用户的 8756；拉起与跑完在同一条命令里（铁律 20）。
★ 探针**绝不真跑任务**：/api/jobs 的 POST 在页面里被拦下记 body 再回 400，
  所以这一关不占 GPU、也不花 N 倍推理时间。
"""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request

os.environ["no_proxy"] = "*"
os.environ["NO_PROXY"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, "tools")
sys.path.insert(0, HERE)
sys.path.insert(0, TOOLS)

PORT = int(os.environ.get("VS_HALL_PORT", "8900"))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
N_PASS, N_FAIL = 0, 0
# 尺子自检时把 PASS/FAIL 行**静音**：那一段本来就会"故意红"，
# 原样打出来会让日志里出现一堆 [FAIL]，看日志的人会以为产品坏了。
# （判据账仍然照记，自检结论单独一行。）
QUIET = False


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if not QUIET:
        if cond:
            print("  [ok] %s" % name, flush=True)
        else:
            print("  [FAIL] %s%s" % (name, ("  ← " + str(detail)) if detail else ""),
                  flush=True)
    if cond:
        N_PASS += 1
    else:
        N_FAIL += 1


def read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def func_src(src, name):
    """按 `function name(` 截到下一个顶层 `}` —— 只看这个函数里的接线。"""
    at = src.find("function %s(" % name)
    if at < 0:
        at = src.find("async function %s(" % name)
    if at < 0:
        return ""
    out = []
    for line in src[at:].splitlines():
        out.append(line)
        if line == "}":
            break
    return "\n".join(out)


def listen(port, host="127.0.0.1", timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def health(port=None):
    p = port or PORT
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % p,
                                    timeout=5) as r:
            return json_load(r.read())
    except Exception:
        return {}


def json_load(b):
    import json
    try:
        return json.loads(b.decode("utf-8"))
    except Exception:
        return {}


def start_service():
    import studio
    if not os.path.isfile(studio.PY):
        return None, "找不到 GSV 运行时：%s" % studio.PY
    if listen(PORT):
        return None, "端口 %d 已被占用（换 VS_HALL_PORT）" % PORT
    env = dict(os.environ)
    env["VOICESTUDIO_PORT"] = str(PORT)
    logp = os.path.join(HERE, "_tmp", "svc_%d.log" % PORT)
    os.makedirs(os.path.dirname(logp), exist_ok=True)
    f = open(logp, "wb")
    p = subprocess.Popen([studio.PY, "-u", os.path.join(HERE, "server.py")],
                         cwd=HERE, env=env, stdout=f, stderr=subprocess.STDOUT)
    for _ in range(60):
        time.sleep(0.7)
        if health().get("ok"):
            return p, logp
    return p, logp


# --------------------------------------------------------------- 源码判据
def src_checks(app, html, loc, studio_src=""):
    # ---- A. 大厅多人 ----
    one = func_src(app, "stageOneClick")
    check("大厅：点合成时先判人数（有 2 人就走多角色分支）",
          "STAGE_CASTS.length >= 2" in one and "stageMultiClick" in one,
          [l.strip()[:60] for l in one.splitlines() if "STAGE_CASTS" in l][:3])
    multi = func_src(app, "stageMultiClick")
    check("大厅：多人分支发的是 kind=sing_multi（不是单人 sing）",
          '"sing_multi"' in multi, "没找到" if not multi else "")
    check("大厅：casts 里带 rid（后端靠它找人）", "rid: r.id" in multi)
    check("大厅：缺模型的人会被点名拦下（不等到后端 Fail）",
          "lack" in multi and "还没有歌声模型" in multi)
    check("大厅：确认框把「N 倍时间」说清楚", "倍" in multi and "confirm(" in multi)

    setsrc = func_src(app, "stageSetRole")
    check("名单去重（同一个人不会占两个位置）", "some(c => c.rid === rid)" in setsrc)
    check("名单有上限（与后端一致）", "STAGE_MAX_CASTS" in setsrc)
    check("★ 重复加人不是当作错误静默失败，而是明说「已经在名单里」",
          "已经在名单里了" in setsrc)

    osc = func_src(app, "openCastList")
    check("名单是**多选**的：点完不关（不然选第 2 个人要重新点开槽）",
          "paint()" in osc and "菜单**不关**" in osc)
    check("名单里标出「无歌声模型」的人（不然点了合成才被拦）",
          "无歌声模型" in osc)

    paint = func_src(app, "stagePaintRoleSlot")
    check("槽上画出每个人的胶囊（谁在里面一眼可见）", "castbox" in paint and "cast" in paint)
    check("主唱有标记（成品登记在它名下）", '"主"' in paint or "主</b>" in paint)
    check("★ 缺模型的人在槽上就标出来", "缺模型" in paint)
    check("唱法下拉在槽里（大厅自己能选怎么分）", "sgMultiMode" in paint)
    check("★ 多人时引擎置灰并写清为什么（sing_multi 只走 RVC）",
          "eng.disabled = true" in paint and "Seed-VC" in paint)
    check("单人时引擎恢复可用（别把单人也弄坏了）",
          "eng.disabled = false" in paint)

    # 唱法档位不许和角色内/后端那两份漂
    mm = re.search(r'id="selMultiMode"[^>]*>(.*?)</select>', html, re.S)
    front = set(re.findall(r'<option value="(\w+)"', mm.group(1) if mm else ""))
    stage_modes = set(re.findall(r'\{ v: "(\w+)", t:', app))
    need = {"rotate", "phrase", "section", "manual", "chorus"}
    check("大厅的唱法档位覆盖五种分配方式", need <= stage_modes, sorted(stage_modes))
    # 参照系取后端 studio.MULTI_MODES 的键 —— 不是门禁里另抄一份名单，
    # 否则"漂了"这件事永远只能靠人眼看。
    back = set()
    if studio_src:
        i = studio_src.find("MULTI_MODES = {")
        if i >= 0:
            blk = studio_src[i:i + 400]
            blk = blk[:blk.find("}")]
            back = set(re.findall(r'"(\w+)":', blk))
    check("★ 大厅的档位值都在后端 MULTI_MODES 里（前端不许自造档位）",
          bool(back) and stage_modes <= back,
          "后端=%s 大厅=%s" % (sorted(back), sorted(stage_modes)))
    check("★ 角色内那份下拉也在同一集合里（两处不漂）",
          bool(back) and front <= back and "manual" in front,
          "页面 option=%s 后端=%s" % (sorted(front), sorted(back)))
    # ★ 这一条以前是**反过来的**（"大厅不提供手动指定，给了就是死按钮"）。
    #   那是把缺口当设计留了下来：大厅是主路径，缺了它用户想指定每句谁唱
    #   只能绕到角色页（点开角色 → 开多人 → 选歌）。所以现在反过来钉：
    #   选项和编辑器必须**同时**在，只有一个等于死按钮。
    check("★ 大厅提供「手动指定」（主路径不能缺这一档）",
          "manual" in stage_modes, sorted(stage_modes))
    for eid in ("sgPlan", "sgPlanSum", "sgPlanHint", "sgLines", "btnSgPlan"):
        check("★ 大厅有逐句编辑器容器 #%s（只给选项 = 死按钮）" % eid,
              'id="%s"' % eid in html)
    sload = func_src(app, "stageLoadPlan")
    check("★ 换歌/加人/减人/换唱法都会重排（句数与人数变了手上那份是废的）",
          all(x in app for x in ("stageSetSong", "stageSetRole", "stageRemoveRole",
                                 "stageSetMode"))
          and "stageLoadPlan()" in app,
          "重排调用 %d 处" % app.count("stageLoadPlan()"))
    rp = func_src(app, "stageRenderPlan")
    check("★ 大厅的笔和歌词行与角色页共用同一份（不许抄成两份）",
          "manualPensHTML(" in rp and "manualRowsHTML(" in rp and
          "wireManualList(" in rp,
          "共用到 %d 处" % app.count("manualRowsHTML("))
    check("★ 大厅有「整首都给 TA」和合唱笔（不是只能一句一句点）",
          "stageAssignAll" in rp and "SG_PEN" in rp)
    check("★ 没认过词时大厅也有「先认一次词」入口（不是让人走不存在的路）",
          "btnSgLyricFirst" in rp and "onLyricFirst(" in rp)
    mc = func_src(app, "stageMultiClick")
    check("★ 大厅提交时会把逐句指定带给后端（不然点了白点）",
          "body.manual = sgManual()" in mc, "")
    mc2 = mc.replace(" ", "")
    check("★ 一句都没安排时不许发（空数组会被后端当成没指定）",
          "还没有句子" in mc and "man.length" in mc2,
          "")
    check("★ 歌还没认过词时要先拦下（按时间切的段不是句子，点了也白点）",
          "lyric_ready" in mc and "还没认过词" in mc, "")
    check("★ 和声音量在手动指定下也显示（逐句合唱要用到它）",
          'STAGE_MODE === "manual"' in func_src(app, "stageSetMode")
          and 'STAGE_MODE === "manual"' in paint)
    check("★ 大厅的两份状态各自独立（SG_* 不共用 MULTI_*，否则换笔会串台）",
          "SG_PEN" in rp and "MULTI_PEN" not in rp)
    if studio_src:
        m = re.search(r"MULTI_MAX_ROLES\s*=\s*(\d+)", studio_src)
        n = re.search(r"STAGE_MAX_CASTS\s*=\s*(\d+)", app)
        check("前端的名单上限与后端 MULTI_MAX_ROLES 一致",
              bool(m and n) and m.group(1) == n.group(1),
              "后端=%s 前端=%s" % (m and m.group(1), n and n.group(1)))

    # ---- B. 角色内读 VIP ----
    check("角色内有「从我的音乐里选」按钮", 'id="btnSongFromLib"' in html)
    check("按钮接到选歌器（不是又调一次文件选择器）",
          "songPickOpen" in func_src(app, "pickSongFromLib"))
    check("选歌器弹层存在", 'id="maskSongPick"' in html and 'id="spkBody"' in html)
    check("选歌器里有目录 chips 与扫描按钮",
          'id="spkDirs"' in html and 'id="btnSpkScan"' in html)

    pick = func_src(app, "pickUpload")
    # ★ 加密容器不再支持：文件选择器与上传入口都只认普通音频。
    check("文件选择器的接受列表里没有加密容器扩展名",
          ".ncm" not in pick, pick.strip().splitlines()[0] if pick else "没找到")
    scan = func_src(loc, "songPickScan")
    check("扫描用**同一个**接口（不另写一套口径，否则两处数字会打架）",
          "/api/music/local/scan" in scan)
    check("扫描失败要说出来（不是静默空列表）", "扫描失败" in scan)

    share = app.count("songPickedIntoRole(")
    check("★ 三条选歌路径（上传 / 曲库 / 解密）共用同一段收尾（>=3 处）",
          share >= 3, share)
    check("共用收尾会按角色能力决定按钮是否可点", "singOk" in func_src(app, "songPickedIntoRole"))


def stale_checks(app, server_src):
    """「改了没生效」这条提示本身也要准 —— 它会被用户当事实引用。

    ★ 背景（2026-10-24 真事）：19:08 改好的多角色分句，服务 16:58 起的，
      一次都没加载 ⇒ 老板看到的现象是「还是没修好」，其实是没重启。
      加了提示之后又发现第二种错法：把 stages/ 和 web/ 也算进「没生效」，
      可它们是**子进程现读 / 每次请求从盘读**，改完根本不用重启 ⇒ 提示在撒谎。
      ⇒ 判据同时守两头：该报的要报，不该报的别报。
    """
    print("\n== C. 「代码改了要重启」提示：只报真没生效的那几个文件 ==", flush=True)
    check("server 里明确了「哪些文件必须重启才生效」（有这份名单）",
          "_RESTART_FILES" in server_src)
    import re as _re
    m = _re.search(r"_RESTART_FILES\s*=\s*\(([^)]*)\)", server_src)
    names = _re.findall(r'"([^"]+)"', m.group(1)) if m else []
    check("名单里只有 studio.py / server.py（真正被常驻进程 import 的两个）",
          names == ["studio.py", "server.py"], names)
    check("★ 过清单时按文件名过滤（stages/ 与 web/ 不进「没生效」名单）",
          "os.path.basename(p) in _RESTART_FILES" in server_src)
    check("把「为什么不算 stages / web」写进了注释（后人不会又加回去）",
          "子进程" in server_src and "从磁盘读" in server_src)
    fresh = func_src(app, "checkServiceFresh")
    check("★ 清单为空时**不弹**横幅（改个 stage 也弹 = 假警报，弹多了没人看）",
          "if (!sf0.length) return;" in fresh, fresh.strip().splitlines()[-1] if fresh else "")
    check("不再只靠 code_mtime 判断要不要弹（那样 stages 改动会误报）",
          "h.code_mtime - h.started_ts < 60" not in fresh)


def main():
    global N_PASS, N_FAIL
    app = read(os.path.join(HERE, "web", "app.js"))
    html = read(os.path.join(HERE, "web", "index.html"))
    loc = read(os.path.join(HERE, "web", "loc.js"))
    studio_src = read(os.path.join(HERE, "studio.py"))
    server_src = read(os.path.join(HERE, "server.py"))

    print("== A/B 源码接线判据 ==", flush=True)
    src_checks(app, html, loc, studio_src)
    stale_checks(app, server_src)

    print("\n== 尺子自检：把三处关键接线改坏，判据必须变红 ==", flush=True)
    bad_app = app.replace('kind: "sing_multi"', 'kind: "sing"')
    bad_loc = loc.replace("locDecrypt([path])", "Promise.resolve([])")
    bad_html = html.replace('id="btnSongFromLib"', 'id="btnSongFromLibOff"')
    bad_srv = server_src.replace("os.path.basename(p) in _RESTART_FILES", "True")
    bad_app2 = app.replace("if (!sf0.length) return;", "")
    global QUIET
    before = (N_PASS, N_FAIL)
    QUIET = True
    src_checks(bad_app, bad_html, bad_loc, studio_src)
    stale_checks(bad_app2, bad_srv)
    QUIET = False
    delta_fail = N_FAIL - before[1]
    N_PASS, N_FAIL = before          # 自检的账不算进正式判据
    print("  改坏后红了 %d 条（期望 >= 3）" % delta_fail, flush=True)
    check("负对照：把三处关键接线改坏后判据真的会红", delta_fail >= 3,
          "红了 %d 条" % delta_fail)

    print("\n== 真浏览器（独立端口 %d） ==" % PORT, flush=True)
    if not os.path.isfile(CHROME):
        check("找到 Chrome", False, CHROME)
    else:
        proc, info = start_service()
        if not proc:
            check("在 %d 起一份独立服务" % PORT, False, info)
        else:
            try:
                h = health()
                check("服务活着", h.get("ok") is True, h)
                fresh = float(h.get("code_mtime") or 0) <= float(h.get("started_ts") or 0) + 2
                check("服务跑的是当前代码（code_mtime <= started_ts）", fresh,
                      "code=%s started=%s" % (h.get("code_mtime"), h.get("started_ts")))
                os.environ["VS_PORT"] = str(PORT)
                os.environ["VS_CDP_PORT"] = os.environ.get("VS_HALL_CDP", "9342")
                import probe_hall_multi as P
                rc = P.main()
                # 把探针自己的判据并进门禁的账（它是这一关的主体）
                N_PASS += len(P.OK)
                N_FAIL += len(P.BAD)
                check("真浏览器探针全过（A 大厅多人 + B 角色内选 VIP 歌）", rc == 0,
                      "探针返回 %s，见上面的 FAIL 行" % rc)
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=15)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

    print("\n通过 %d 项，失败 %d 项" % (N_PASS, N_FAIL), flush=True)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
