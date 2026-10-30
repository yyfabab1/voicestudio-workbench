r"""
在线曲库：搜索可以合法导歌的开放音乐。

只接**授权可查、可整曲下载**的源。为什么这么挑：
翻唱是要把歌整首拉下来喂给 RVC 的，所以「能搜到」不够，必须「能下整曲 + 授权写在数据里」。
很多知名曲库（Jamendo / iTunes / FMA / Pixabay）实测下来要么必须申请密钥，
要么只给 30 秒试听，要么整站被 Cloudflare 挡，纯 HTTP 抓不到 —— 一律不接，
免得 UI 上摆个搜不出东西的死源。

实测结论（2026-10，VoiceStudio 本机 curl 逐个验过）：

  ccmixter   ✅ 搜索 / 许可字段 / 文件页 / 整曲下载 全链路通（唯一主力源）
  jamendo    ❌ v3 强制 client_id，无 key 直接 code:5
  itunes     ❌ 只给 30 秒 previewUrl，且全是商业版权曲
  freemusicarchive ❌ 搜索页纯 JS 渲染，HTML 里连 /music/ 链接都没有
  pixabay    ❌ 整站 403
  musopen    ❌ 403
  ear5(耳聆网) ❌ Cloudflare「Just a moment...」挑战
  openverse  ❌ 本机网络不通（DNS 通、连接不返回）
  archive.org ❌ 同上

所以注册表里目前只有 ccmixter 一个 enabled 的源，但结构是多源的：
以后哪个源通了（换网络环境、加密钥、站点改版），照 SCHEMA 加一条即可，
前端会自动多出一个标签，不用改 UI 代码。

**许可红线**：默认只放行 CC0 / PD / CC-BY / CC-BY-SA。
CC-BY-NC（禁商用）和 CC-BY-ND（禁改写）一律不下 —— 前者翻唱发布可能算商业用途，
后者"改编" 本身就是禁的。NC/ND 曲目不会消失，而是置灰显示原因，让人自己决定。
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar

HERE = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(HERE, "..", "data", "music")
os.makedirs(MUSIC_DIR, exist_ok=True)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 许可判定：True=可放心用；False=有红线；None=查不到（按红线处理）
OK = "ok"
BLOCKED = "blocked"
UNKNOWN = "unknown"

_ALLOW_TOKENS = ("cc0", "public domain", "publicdomain", "pd-")
_BLOCK_TOKENS = ("-nc", "noncommercial", "non-commercial", " noncom", "-nd", "noderiv", "no-deriv")


def classify_license(text, url=""):
    """把各站的许可写法归一成 (状态, 中文说明, 许可页)。"""
    s = (text or "").strip().lower()
    u = (url or "").lower()
    full = s + " " + u

    if not s and not u:
        return UNKNOWN, "授权不明 —— 不建议使用", ""
    for t in _BLOCK_TOKENS:
        if t in full:
            if "-nc" in full or "noncommercial" in full:
                return BLOCKED, "CC-BY-NC 禁止商用，翻唱发布有风险", url
            return BLOCKED, "CC-BY-ND 禁止改编，而翻唱正是改编", url
    for t in _ALLOW_TOKENS:
        if t in full:
            if "cc0" in full:
                return OK, "CC0 公共领域，署名都不用", url
            return OK, "公共领域（Public Domain）", url
    if "attribution" in full or "/by/" in full or "/by-sa/" in full:
        return OK, "CC-BY 需署名", url
    return UNKNOWN, "无法识别的授权：%s" % (text or url or "空"), url


# ---------------------------------------------------------------- HTTP

_last_hit = {}


def http(url, timeout=20, referer=None, data=None, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        h["Referer"] = referer
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h, data=data)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.read(), dict(r.headers), r.getcode()
    except urllib.error.HTTPError as e:
        return b"", dict(e.headers or {}), e.code
    except Exception as e:
        raise IOError(str(e))


# 起因：ccmixter 会把**整份 JSON 结果**复制一份塞进响应头 X-JSON 里。
# limit=20 时这个头单行就有 79319 字节，而 http.client 把"读一行响应头"的长度
# 硬编码成 _MAXLINE=65536，超了就抛：
#     got more than 65536 bytes when reading header line
# 所以小结果集（limit<=8）好好的，一放大就全挂 —— 症状极具迷惑性。
#
# 之前误判成"cookie 太多"，加了 CookieJar 也没用（实测 lim=8 过、lim=20 照样挂），
# 这才定位到真凶。CookieJar 留着（本身合理，少一轮握手），但关键是把 _MAXLINE 抬过
# 80KB。这里改 http.client 的模块级常量，是标准库里少数能安全改的全局。
# 放到 1MB：够 ccmixter 用，又不至于让真·超大响应把内存吃穿。
try:
    import http.client as _hc

    if getattr(_hc, "_MAXLINE", 0) < 1024 * 1024:
        _hc._MAXLINE = 1024 * 1024
except Exception:  # pragma: no cover
    pass

_OPENER = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(CookieJar()))


def get_json(url, timeout=20, referer=None):
    raw, _, code = http(url, timeout=timeout, referer=referer)
    if code != 200 or not raw:
        raise IOError("HTTP %s" % code)
    return json.loads(raw.decode("utf-8", "replace"))


def get_text(url, timeout=20, referer=None):
    raw, _, code = http(url, timeout=timeout, referer=referer)
    if code != 200 or not raw:
        raise IOError("HTTP %s" % code)
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------- ccmixter

CC_API = "http://ccmixter.org/api/query"
CC_HOME = "https://ccmixter.org/"


def _cc_tags(q):
    """ccmixter 只按 tags 搜，且标签是单词组合；拆词做 AND 近似。"""
    words = re.findall(r"[A-Za-z0-9一-鿿]+", q or "")
    return "+".join(words[:4]) if words else "instrumental"


_PAGE_CACHE = {}


def cc_search(q, limit=24, lic="by", offset=0):
    """ccmixter 搜索。

    三个实测出来的硬约束，写在这里免得下次再踩：

    1. `limit<=29`。到 30 就返回 HTTP 200 + **空 body**（不是报错！），
       站点 PHP 内存上限所致。单页 29 是安全区。
    2. **翻页参数是 `offset`，不是 `skip` 也不是 `page`** ——
       skip / page / start 实测全部被忽略，返回的还是第一页（first_id 恒为 71182）；
       只有 offset=20 时 first_id 变成 69624，offset=1000 才开始返回空。
    3. `lic` 只有 by / open / all 有效。`lic=by-sa`、`lic=cc0`、`lic=remix`
       会返回空结果（站点没这些枚举值），别拿它们当过滤条件用。
    """
    tags = _cc_tags(q)
    per = min(int(limit), 29)
    common = "f=json&limit=%d&tags=%s" % (per, urllib.parse.quote(tags))
    if lic and lic != "all":
        # lic=by 实测能筛掉 NC，只留可商用的 CC-BY / CC-BY-SA。
        # 默认走这个：翻唱常会发布，默认给 NC 等于默认给一堆不能用的卡。
        common += "&lic=%s" % urllib.parse.quote(lic)
    if int(offset) > 0:
        common += "&offset=%d" % int(offset)

    main = get_json("%s?%s" % (CC_API, common), timeout=20)
    if not main:
        # offset 翻过头了（实测 >500 之后返回空），如实返回空而不是报错
        return []
    try:
        links = get_json("%s?%s&dataview=links" % (CC_API, common), timeout=20)
    except Exception:
        links = []
    # links 视图只给 file_page_url + upload_name，**没有 upload_id**
    # （实测确认）。所以只能从 URL 尾部反解 id —— 不能靠顺序对齐，那是运气。
    link_by_id = {}
    for l in links:
        m = re.search(r"/(\d+)/?$", l.get("file_page_url") or "")
        if m:
            link_by_id[int(m.group(1))] = l["file_page_url"]

    out = []
    for m in main:
        uid = m.get("upload_id")
        extra = m.get("upload_extra") or {}
        lic_text = m.get("license_name") or ""
        lic_url = m.get("license_url") or ""
        status, note, lpage = classify_license(lic_text, lic_url)
        sys_t = (extra.get("systags") or "").lower()
        # 站点自己打的标比 license_name 更可信：命中就压过 license_name 的结论
        if "non_commercial" in sys_t:
            status = BLOCKED
            note = "站点标记 non_commercial（禁商用）"
        elif "nsfw" in sys_t:
            status = BLOCKED
            note = "站点标记 nsfw（不适宜内容）"
        elif extra.get("nsfw"):
            status = BLOCKED
            note = "站点标记 nsfw（不适宜内容）"

        item_id = "ccmixter:%s" % uid
        page = link_by_id.get(int(uid), "") if str(uid).isdigit() else ""
        if page:
            _PAGE_CACHE[item_id] = page

        out.append({
            "id": item_id,
            "src": "ccmixter",
            "title": m.get("upload_name") or "未命名 %s" % uid,
            "artist": m.get("user_name") or "",
            "page": page,
            "license": lic_text,
            "license_url": lpage,
            "status": status,
            "note": note,
            "tags": (extra.get("usertags") or "").split(",")[:10],
            "dur": 0,
            "dur_raw": extra.get("duration") or "",
            "bpm": extra.get("bpm") or 0,
            "preview": None,
        })
    return out


_MP3_RE = re.compile(r'https?://[^\s"\'<>]+\.mp3', re.I)


def cc_resolve(item_id):
    """把 ccmixter 曲目 id 解析成可直接下载的 mp3 直链。

    API 默认视图不给 file_url（实测确认），必须再抓一次 files 页 —— 这一步
    不能省，省了就是一堆下载不了的死卡片。
    files 页路径里的用户名来自 links 查询，这里优先用搜索时缓存的整条 URL；
    缓存没命中（服务重启后直接调 resolve）就退回 API 重查一次。
    """
    if not item_id.startswith("ccmixter:"):
        raise ValueError("不是 ccmixter 的 id: %s" % item_id)
    uid = item_id.split(":", 1)[1]
    page = _PAGE_CACHE.get(item_id)
    if not page:
        page = _cc_lookup_page(uid)
    if not page:
        raise IOError("找不到该曲目的文件页（upload_id=%s）" % uid)
    html = get_text(page, timeout=25, referer=CC_HOME)
    m = _MP3_RE.search(html)
    if not m:
        raise IOError("文件页里没找到 mp3 直链（upload_id=%s）" % uid)
    return m.group(0)


def _cc_lookup_page(uid):
    """缓存没命中时，用 dataview=links 反查文件页。

    links 视图不带 upload_id，所以这里按 upload_name 反查 —— 而 upload_name
    在默认视图里是有的。若上传者改过名字就查不到，此时如实报错，不猜。
    """
    try:
        main = get_json(
            "%s?f=json&limit=40&upload_id=%s" % (CC_API, urllib.parse.quote(str(uid))),
            timeout=20)
    except Exception:
        return ""
    names = set()
    for x in main or []:
        if str(x.get("upload_id")) == str(uid):
            n = x.get("upload_name")
            if n:
                names.add(n)
    if not names:
        return ""
    try:
        links = get_json(
            "%s?f=json&limit=40&dataview=links" % CC_API, timeout=20)
    except Exception:
        return ""
    for l in links or []:
        if l.get("upload_name") in names and l.get("file_page_url"):
            return l["file_page_url"]
    return ""


# ---------------------------------------------------------------- FMA
# Free Music Archive。上一轮我判它"搜索页纯 JS 渲染、抓不到"，那次是错的 ——
# 错因是在沙箱里跑，网络被拦。现在无沙箱实测：
#   搜索页 https://freemusicarchive.org/search/?adv=1&quicksearch=xxx
#     → 纯 HTTP 就能抓到 /music/<artist>/<album>/ 链接（实测 63 条）
#     （页面靠无限滚动，dump-dom 只拿到骨架，但首屏 HTML 里确实有链接）
#   详情页 → 许可可读（creativecommons.org/licenses/by-nc-nd/4.0/ 这种）
#   详情页 → mp3 直链存在，但被 JSON 转义成 https:\/\/files.freemusicarchive.org\/
#            藏在播放器数据里；先把 \/ 还原成 / 再正则才能提取到。
#            实测下载成功：9.86MB、ID3 魔数、合法 mp3。
# 注意 FMA 没有 JSON API，只有 adv=1 的 HTML 搜索页 —— 所以只能解析 HTML。

FMA_HOME = "https://freemusicarchive.org/"
# 真实结果 URL 是**四层**：/music/<artist>/<album>/<track>/
# 而它不出现在 href 里 —— 藏在 play-item 的 data-track-info 属性中：
#   data-track-info='{"id":"49729","handle":"On_Your_Way_Down",
#     "url":"https://freemusicarchive.org/music/Trombone_Shorty/KEXP_Live_Feb_2011/On_Your_Way_Down/",
#     "title":"On Your Way Down (Live @ KEXP)","artistName":"Trombone Shorty", ...}'
# 只用三层 /music/<artist>/<album>/ 去拼 URL 会大面积 404（实测 9 个里 8 个挂），
# 且更坑的是**标题/艺名本来就在 data 里**，不解析就拿不到真正的曲名。
_FMA_TRACK_RE = re.compile(
    r'data-track-info="\{&quot;id&quot;:&quot;\d+&quot;,&quot;handle&quot;:'
    r'&quot;([^&]+)&quot;,&quot;url&quot;:&quot;([^&]+)&quot;,'
    r'&quot;title&quot;:&quot;([^&]+)&quot;,&quot;artistName&quot;:&quot;([^&]*)&quot;')
_FMA_ALBUM_RE = re.compile(r'/music/([A-Za-z0-9_.%-]+)/([A-Za-z0-9_.%-]+)/')
_FMA_SKIP = {"charts", "artistShuffle", "genre", "curator", "label", "browse"}
# FMA 的 mp3 直链藏在播放器 JSON 里，斜杠被转义成 \/ —— 先还原再匹配
_FMA_MP3_RE = re.compile(r'https://files\.freemusicarchive\.org[^\s"\\<>]+\.mp3', re.I)

# --- 搜索必须走 headless Chrome -------------------------------------------
# 这个坑很大，值得写清楚：FMA 的搜索**纯 HTTP 拿不到真实结果**。
# 实测 `?quicksearch=X` 和 `?q=X` 都不生效 —— 用无意义词 zzzqqqxx123 去搜，
# 返回的专辑指纹和搜 piano **完全相同**（md5 d9137e5b9c，20 条一模一样）。
# 也就是说纯 HTTP 抓回来的是"推荐流"，不是搜索结果，只是它长得像搜索。
#
# 上游正因如此第一轮判它"抓不到"，那个结论本身没错，但真正的原因是：
# 搜索要跑 JS。用 headless Chrome --dump-dom 渲染后，指纹就变了：
#   quicksearch=trombone → 7fc22b2dcb，首条 Trombone_Shorty/KEXP_Live_Feb_2011
#   quicksearch=piano    → 2183ae734a，首条 Piano__Elektron/2015021275957958
#   quicksearch=trumpet  → 4450d8707d，首条 Earth_Trumpet/Earth_Trumpet
# 三次指纹互不相同 = 搜索真的按词生效。
#
# 另外 FMA 的授权实测以 NC/ND 为主（piano 前 20 首：by-nc-nd 9 / by-nc-sa 4 /
# by-nc 1 / 无声明 6，可商用 0 首），所以许可必须在搜索阶段核实并如实标注。
CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def _find_chrome():
    for p in CHROME_CANDIDATES:
        if p and os.path.isfile(p):
            return p
    return ""


_DOM_CACHE = {}
_dom_lock = __import__("threading").Lock()


def _render_dom(url, timeout=60, retries=2):
    """用 headless Chrome 渲染页面后取 DOM。

    实测 Chrome 偶发返回**空页面**（同一 URL 第 1 次空、重试就有内容），
    所以必须重试 —— 只试一次的话这条源会随机失败，
    而"偶尔能搜到"比"稳定搜不到"更坏，用户会以为是自己网络的问题。

    渲染一次 6-8 秒，所以加进程内缓存：同一个查询串 10 分钟内直接复用。
    """
    key = url
    with _dom_lock:
        hit = _DOM_CACHE.get(key)
    if hit and (time.time() - hit[0]) < 600:
        return hit[1]

    import subprocess

    chrome = _find_chrome()
    if not chrome:
        raise IOError("找不到 Chrome/Edge，无法渲染 FMA 搜索")
    last = ""
    for attempt in range(retries + 1):
        args = [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
                "--virtual-time-budget=20000", "--dump-dom", url]
        try:
            p = subprocess.run(args, capture_output=True, timeout=timeout)
            html = (p.stdout or b"").decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            last = "渲染超时"
            continue
        # 判"有效"而不是判"非空"：渲染骨架里没有 data-track-info 说明没真加载出结果
        if "data-track-info" in html:
            with _dom_lock:
                _DOM_CACHE[key] = (time.time(), html)
            return html
        last = "渲染出空页面（无结果节点）"
    raise IOError(last or "渲染失败")


def _fma_unescape(html):
    return html.replace("\\/", "/")


def fma_search(q, limit=24, lic="by", offset=0, only_ok_lic=False):
    """FMA 搜索（headless Chrome 渲染 HTML 搜索页 + 并发补全许可）。

    必须渲染：纯 HTTP 抓回来的不是搜索结果（详见 _render_dom 上方的实测记录）。
    参数：
      - `?quicksearch=<词>` 是真实搜索（渲染后首条随词变化，已实测）。
      - 翻页用 `?page=2`；`start=` / `offset=` 实测不生效。
    渲染一次约 6-8 秒，所以并发补许可（8 路）不能叠加在渲染里，否则总耗时翻倍。
    """
    words = re.findall(r"[A-Za-z0-9一-鿿]+", q or "")
    tags = "+".join(words[:4]) if words else "instrumental"
    page_no = 1 + int(offset) // 20
    url = "%ssearch/?quicksearch=%s" % (FMA_HOME, urllib.parse.quote(tags))
    if page_no > 1:
        url += "&page=%d" % page_no

    html = _fma_unescape(_render_dom(url, timeout=75))

    # 优先从 data-track-info 取（带真曲名、真艺名、真四层 URL）；
    # 取不到再退回三层 /music/<artist>/<album>/ 匹配（会 404，当兜底用）。
    seen = set()
    rows = []
    for m in _FMA_TRACK_RE.finditer(html):
        handle, url, title, artist = m.group(1), m.group(2), m.group(3), m.group(4)
        url = url.replace("&amp;", "&")
        key = url
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "id": "fma:%s" % url.replace(FMA_HOME, "").strip("/"),
            "title": title.replace("&quot;", '"'),
            "artist": artist.replace("&quot;", '"'),
            "page": url,
            "handle": handle,
        })
    if not rows:
        for m in _FMA_ALBUM_RE.finditer(html):
            artist, album = m.group(1), m.group(2)
            if artist.lower() in _FMA_SKIP:
                continue
            key = "%s/%s" % (artist, album)
            if key in seen:
                continue
            seen.add(key)
            rows.append({
                "id": "fma:%s" % key,
                "title": album.replace("_", " "),
                "artist": artist.replace("_", " "),
                "page": "%smusic/%s/" % (FMA_HOME, key),
                "handle": "",
            })

    out = []
    for r in rows[:limit]:
        item = {
            "id": r["id"],
            "src": "fma",
            "title": r["title"],
            "artist": r["artist"],
            "page": r["page"],
            "license": "",
            "license_url": "",
            "status": UNKNOWN,
            "note": "正在核实授权…",
            "tags": words[:5],
            "dur": 0,
            "dur_raw": "",
            "bpm": 0,
            "preview": None,
        }
        out.append(item)

    _fma_fill_license(out)
    if only_ok_lic:
        out = [i for i in out if i["status"] == OK]
    return out


def _fma_fill_license(items, workers=8):
    """并发进详情页补全许可。就地改写 items。

    并发是因为每首要一次 HTTP（实测单次约 1.5s），20 首串行要 30s，
    用户会以为卡死了。8 路并发实测 20 首约 5s。
    """
    import threading

    def work(it):
        try:
            info = fma_resolve(it["id"])
            key = info.get("lic_key") or ""
            url = info.get("lic_url") or ""
            it["license"] = key
            it["license_url"] = url
            st, note, _ = classify_license(key, url)
            if not key:
                # 详情页里找不到任何 CC 声明：如实说不知道，不猜
                st, note = UNKNOWN, "该专辑未声明 CC 授权，不建议使用"
            it["status"] = st
            it["note"] = note
            it["_mp3"] = info.get("url") or ""
        except Exception as e:
            it["status"] = BLOCKED if "HTTP 4" in str(e) else UNKNOWN
            it["note"] = "授权核实失败：%s" % str(e)[:50]

    ths = [threading.Thread(target=work, args=(it,), daemon=True) for it in items]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout=25)


_ITEM_CACHE = {}


def fma_resolve(item_id):
    """FMA 详情页 → (mp3直链, 许可标识, 许可URL)。

    FMA 的一个专辑页对应多首 mp3（实测 8 条），取第一条作为该专辑的下载物。
    许可从详情页的 creativecommons.org 链接读 —— 这是 FMA 唯一的权威写法。
    """
    if not item_id.startswith("fma:"):
        raise ValueError("不是 FMA 的 id: %s" % item_id)
    cached = _ITEM_CACHE.get(item_id)
    if cached and cached.get("url"):
        return cached
    page = "%smusic/%s" % (FMA_HOME, item_id.split(":", 1)[1])
    html = _fma_unescape(get_text(page, timeout=30, referer=FMA_HOME))
    m = _FMA_MP3_RE.search(html)
    if not m:
        raise IOError("详情页里没找到 mp3 直链（%s）" % item_id)
    lic, lic_url = "", ""
    lm = re.search(r'(creativecommons\.org/licenses/([a-z0-9-]+)/[\d.]+/)', html)
    if lm:
        lic_url = lm.group(1)
        lic = lm.group(2).replace("-", " ")
    out = {"url": m.group(0), "lic_key": lic, "lic_url": lic_url}
    _ITEM_CACHE[item_id] = out
    return out


# ---------------------------------------------------------------- 源注册表

SCHEMA_HINT = "每条 = {key,name,home,search(q,limit)->[item],resolve(id)->mp3_url}"


def _src_ccmixter():
    return {
        "key": "ccmixter",
        "name": "ccMixter",
        "home": CC_HOME,
        "desc": "开放授权混音社区，全站 CC 授权、可下整曲",
        "enabled": True,
        "note": "主力源，可深翻页到 500+ 首/标签",
    }


def _src_fma():
    return {
        "key": "fma",
        "name": "Free Music Archive",
        "home": FMA_HOME,
        "desc": "CC 授权曲库，规模大；按单曲组织，授权以 NC/ND 为主",
        "enabled": os.environ.get("VS_FMA_ON") == "1",
        "note": "已实现但默认关闭：需渲染 JS 且授权多被红线拦（实测可商用 0/160），"
                "连续搜索会被站点限流封禁。设环境变量 VS_FMA_ON=1 可开启",
    }


SOURCES = [_src_ccmixter(), _src_fma()]


def _dispatch(src, action, *a, **kw):
    if src == "ccmixter":
        if action == "search":
            return cc_search(*a, **kw)
        if action == "resolve":
            return cc_resolve(*a, **kw)
    if src == "fma":
        if action == "search":
            return fma_search(*a, **kw)
        if action == "resolve":
            return fma_resolve(*a, **kw)
    raise ValueError("未知源或动作: %s/%s" % (src, action))


def search_all(q, limit=20, only_ok=False, lic="by", offset=0, deep=0):
    """多源并行搜索。任一源失败不影响其它源 —— 失败的那源如实报出来。

    约定：每个源的 search 都签名 (q, limit, lic=..., offset=...)，
    加新源时必须遵守，别搞成位置参数。

    `deep`：额外翻多少页。ccmixter 单页上限 29（站点 PHP 内存限制），
    所以"多要歌"靠多次翻页聚合，不是把 limit 调大 —— 实测 limit=30
    会返回 HTTP 200 + 空 body，看起来像成功其实什么都没有。
    """
    import threading

    results = {s["key"]: None for s in SOURCES}
    errs = {s["key"]: None for s in SOURCES}
    depth = max(0, int(deep))

    def work(src):
        key = src["key"]
        if not src.get("enabled"):
            results[key] = []
            errs[key] = "该源未启用"
            return
        try:
            rows = []
            for pg in range(depth + 1):
                # 翻页按 offset 递进。ccmixter 单页上限 29，FMA 每页固定 20 条，
                # 所以 offset 用**页号 × 各自页宽**推进 —— 直接传 pg 会让
                # FMA 永远停在第 1 页（offset=1 仍算出 page_no=1）。
                step = 29 if key == "ccmixter" else 20
                kw = {"lic": lic, "offset": offset + pg * step}
                # FMA 需要"只保留授权可用"的额外开关。**不能无脑传给所有源**：
                # cc_search 没有这个形参，TypeError 会被下面的兜底吞掉，
                # 表现是"ccmixter 突然 0 首"—— 极难定位（实测踩过一次）。
                # 每个源需要的开关，显式列出来，不靠异常探测。
                if key == "fma":
                    kw["only_ok_lic"] = (lic == "by")
                got = _dispatch(key, "search", q, limit, **kw)
                if not got:
                    break
                rows.extend(got)
                if len(got) < limit and key != "fma":
                    break
            results[key] = rows
        except Exception as e:
            # 这里必须兜住所有异常：漏一个就会让整条线程死掉，
            # 而 join 后 results[key] 仍是 None —— 表现成"超时无响应"，
            # 那是假的诊断，比直接报错更坑人。
            results[key] = []
            errs[key] = "%s: %s" % (type(e).__name__, e)

    ths = [threading.Thread(target=work, args=(s,), daemon=True) for s in SOURCES]
    for t in ths:
        t.start()
    # FMA 要起 headless Chrome 渲染（实测 6-8s/页），深翻页会成倍拉长，
    # 所以这里给足时间；超时的源按"超时无响应"如实报出，不假装成功。
    for t in ths:
        t.join(timeout=40 + 90 * depth)

    items, status = [], []
    for s in SOURCES:
        key = s["key"]
        got = results[key]
        if got is None:
            items.extend([])
            status.append({"src": key, "name": s["name"], "ok": False,
                           "err": "超时无响应", "n": 0})
            continue
        if errs[key]:
            status.append({"src": key, "name": s["name"], "ok": False,
                           "err": errs[key], "n": 0})
            continue
        n0 = len(got)
        # 跨页聚合可能带进重复条目（同一专辑在多页都出现）——
        # 界面上出现两张一样的卡比少给更糟，按 id 去重。
        seen_ids = set()
        uniq = []
        for g in got:
            if g["id"] in seen_ids:
                continue
            seen_ids.add(g["id"])
            uniq.append(g)
        if len(uniq) != n0:
            errs[key] = None  # 去重不是错误，别污染状态
        got = uniq
        if only_ok:
            got = [g for g in got if g["status"] == OK]
        items.extend(got)
        status.append({"src": key, "name": s["name"], "ok": True,
                       "err": None, "n": n0})
    return {"items": items, "sources": status}


# ---------------------------------------------------------------- 下载落盘

MAGIC = {
    b"ID3": ".mp3", b"\xff\xfb": ".mp3", b"\xff\xf3": ".mp3", b"\xff\xf2": ".mp3",
    b"MThd": ".mid", b"OggS": ".ogg", b"fLaC": ".flac",
}


def sniff_ext(raw):
    for sig, ext in MAGIC.items():
        if raw[:len(sig)] == sig:
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
        return ".wav"
    if raw[:4] == b"\x00\x00\x00\x18" or raw[:4] == b"\x00\x00\x00\x1c":
        return ".m4a"
    return ""


def _safe_name(s, maxlen=70):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", s or "").strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s[:maxlen] or "track")


def fetch(item_id, timeout=180):
    """下载整曲到本地曲库，返回真实落盘路径（并做真实性校验）。"""
    src = item_id.split(":", 1)[0]
    resolved = _dispatch(src, "resolve", item_id)
    # ccmixter 的 resolve 返回字符串，FMA 返回 dict（多带许可信息）。
    # 这里归一成两种都能用的形状，别让下游到处 if。
    if isinstance(resolved, dict):
        url = resolved["url"]
        lic_text = resolved.get("lic_key") or ""
        lic_url = resolved.get("lic_url") or ""
    else:
        url, lic_text, lic_url = resolved, "", ""

    # 许可二次核验放在**下载之前**：FMA 的许可只有进详情页才拿得到，
    # 而详情页可能已下架/改授权。与其下完再发现不能用，不如直接拒。
    if lic_text:
        st, note, _ = classify_license(lic_text, lic_url)
        if st == BLOCKED:
            raise IOError("授权不允许翻唱：%s" % note)

    referer = None
    if src == "ccmixter":
        referer = CC_HOME  # 实测：不带 Referer 一律 403，带了才给
    elif src == "fma":
        referer = FMA_HOME  # 实测同源要求

    dst_dir = MUSIC_DIR
    os.makedirs(dst_dir, exist_ok=True)
    # 文件名里必须**带上 item_id**，否则同一首歌下两次会得到两个不同路径，
    # 曲库按 path 去重拦不住，界面上出现两张一样的卡。
    # （第一版把 id 只用来做"前缀判断"，可文件名以时间戳开头 ⇒ 判断永远不成立，
    #   去重形同虚设；这个坑记在这里，别再改成只做判断。）
    tagid = _safe_name(item_id, 40)
    # 必须用**实际写进文件名的那个形式**（_safe_name 会把 ':' 换成 '_'）来匹配，
    # 否则拿 ':' 去比对永远匹配不上 —— 去重又成了摆设。
    hit_tag = "__%s__" % tagid
    for old in os.listdir(dst_dir):
        if hit_tag in old and old.lower().endswith(
                (".mp3", ".wav", ".ogg", ".flac", ".m4a")):
            keep = os.path.join(dst_dir, old)
            return {"path": os.path.abspath(keep),
                    "size": os.path.getsize(keep),
                    "url": url, "src": src, "item": item_id, "cached": True}

    base = "%s__%s__%s" % (time.strftime("%Y%m%d_%H%M%S"), tagid,
                           _safe_name(item_id.split(":", 1)[1], 50))
    tmp = os.path.join(dst_dir, base + ".part")
    dst = os.path.join(dst_dir, base + ".mp3")

    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    try:
        # 同 http()：走 _OPENER，避免同一个超大响应头问题在下载时再炸一次
        with _OPENER.open(req, timeout=timeout) as r, open(tmp, "wb") as f:
            total = 0
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                total += len(chunk)
                if total > 300 * 1024 * 1024:
                    raise IOError("文件过大（>300MB），已中止")
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise

    if total < 20000:
        os.remove(tmp)
        raise IOError("下载内容太小（%d 字节），多半是错误页而不是音频" % total)

    with open(tmp, "rb") as f:
        head = f.read(16)
    ext = sniff_ext(head)
    if not ext:
        os.remove(tmp)
        raise IOError("下载到的不是可识别的音频（开头 %r）" % head[:8])

    dst = os.path.join(dst_dir, base + ext)
    os.replace(tmp, dst)
    size = os.path.getsize(dst)
    return {
        "path": os.path.abspath(dst),
        "size": size,
        "url": url,
        "src": src,
        "item": item_id,
    }


def probe_duration(path):
    """用 ffmpeg 量真实时长 —— 体检用，缺 ffmpeg 就返回 0。"""
    import subprocess
    ffmpeg = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0\runtime\ffmpeg.exe"
    if not os.path.isfile(ffmpeg):
        return 0.0
    try:
        out = subprocess.run([ffmpeg, "-i", path], capture_output=True, text=True,
                             timeout=40)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out.stderr or "")
        if not m:
            return 0.0
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:
        return 0.0

def local_library():
    """本地曲库（已下载/已登记的歌）。这里代理 studio，避免 stages 反向依赖主模块。

    **绝不静默丢弃条目。** 曾经写成`if os.path.isfile(p)` 才 append，
    后果实测：把文件挪走 → 曲库条数从 1 变 0，那首歌在界面上直接人间蒸发，
    用户既不知道丢了哪首，也没法找回（库里其实还记着路径）。
    现在改成：条目一律返回，文件不在就地标missing + 写明原因，
    由前端如实显示，用户自己决定重新定位还是移除。
    """
    import sys as _s

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in _s.path:
        _s.path.insert(0, root)
    import studio

    db = studio.load_library()
    import local_lib
    songs = []
    for s in db.get("songs", []):
        p = s.get("path") or ""
        if not p:
            continue
        s = dict(s)
        if not os.path.isfile(p):
            s["missing"] = True
            s["why"] = "文件已不在这个位置（被移动或删除）"
        # 历史误登记：剪枝规则上线之前被扫进来的客户端提示音。
        # 读时算、不落盘（用户把文件挪走了标记就自动消失）。
        s["prog_sound"] = local_lib.is_prog_sound(p)
        songs.append(s)
    return songs
