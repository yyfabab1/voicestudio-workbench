r"""
VoiceStudio 的 HTTP 服务。

刻意用标准库 http.server 手写，不引 fastapi/uvicorn：
这套东西要跑在整合包自带的便携 python 里，多一个第三方依赖就多一个
「某天突然 import 不了」的风险，而这里要的功能（几个 JSON 接口 + 静态文件）
用标准库反而更短更可控。

只监听 127.0.0.1，不对外。
"""
import json
import os
import re
import sys
import threading
import traceback
import urllib.parse

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "web")
DATA = os.path.join(HERE, "data")
STAGES = os.path.join(HERE, "stages")
# **必须显式把项目根加进 sys.path**。
# 以前只加了 STAGES，能跑通纯属侥幸：托管 Python 会自动把脚本所在目录
# 放进 sys.path，而整合包的 python 不会（它按 -s 语义走）⇒ 换成整合包启动时
# 直接 ModuleNotFoundError: No module named 'studio'，服务根本起不来。
# 根因是"正确性押在解释器的默认行为上"，两种解释器行为不一致就崩。
sys.path.insert(0, HERE)
sys.path.insert(0, STAGES)

import studio  # noqa: E402
import music_src  # noqa: E402
import local_lib  # noqa: E402
import seedvc  # noqa: E402  Seed-VC（44.1k 精修 + 角色微调）的只读状态层
import modelhub  # noqa: E402  声音市场：搜/下别人训练好的模型

PORT = int(os.environ.get("VOICESTUDIO_PORT", "8756"))
HOST = "127.0.0.1"

MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    # ★ .mp4 以前**不在表里** —— 于是「🎬 做视频」出来的 MP4 走 /media 时
    #   被当成 application/octet-stream 发给 <video>，Chrome 对 video 不做内容
    #   嗅探，直接拒播 ⇒ 用户的原话就是「必须下载才能正常看」。
    #   （音频侥幸没暴露：浏览器对 <audio> 的宽容度比 <video> 高一点，
    #    但没有 Range 一样是"下完才能播"。两处一起修。)
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".mov": "video/quicktime",
    ".webm": "video/webm",
    ".mkv": "video/x-matroska",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".lrc": "text/plain; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".srt": "text/plain; charset=utf-8",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def json_response(h, obj, code=200):
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    h.send_response(code)
    h.send_header("Content-Type", "application/json; charset=utf-8")
    h.send_header("Content-Length", str(len(body)))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    h.wfile.write(body)


def read_json_body(h):
    n = int(h.headers.get("Content-Length") or 0)
    if not n:
        return {}
    raw = h.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return {}


def safe_under(path, roots):
    """防目录穿越：只允许读指定根目录下面的文件。"""
    p = os.path.abspath(path)
    for r in roots:
        r = os.path.abspath(r)
        if p == r or p.startswith(r + os.sep):
            return p
    return None


# ---- 存储扫描的缓存 ----
# 用锁而不是裸字典：ThreadingHTTPServer 是并发的，两个请求同时发现缓存过期
# 就会同时开扫（各自走 os.walk 十几 G），界面反而更卡。
_STORE_LOCK = threading.Lock()
_STORE = {"at": 0.0, "data": None}
_STORE_TTL = 120.0


def storage_snapshot(force=False, ttl=_STORE_TTL):
    import time

    now = time.time()
    with _STORE_LOCK:
        if _STORE["data"] and not force and (now - _STORE["at"]) < ttl:
            d = dict(_STORE["data"])
            # ★ 悬空引用警告**不跟着缓存走**，每次实时算。它只读 roles.json 加
            #   几个 os.path.exists，微秒级；但它是最要紧的安全信息 —— 用户删了
            #   模型文件再打开面板，看到"还正常"比看到"磁盘数字旧了两分钟"糟得多。
            #   （实测踩过：前端缓存了旧快照，悬空警告永远不出现。）
            try:
                d["warnings"] = studio._dangling_refs()
            except Exception:
                pass
            d["cached"] = True
            return d
    try:
        data = studio.scan_storage()
    except Exception as e:
        return {"error": "扫描失败：%s" % e, "groups": []}
    data["cached"] = False
    with _STORE_LOCK:
        _STORE["at"] = time.time()
        _STORE["data"] = data
    return data


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # ------------------------------------------------------- 兜底
    # ★ 为什么三个入口都得套这一层：
    #   handler 里任何一个没接住的异常，都会让连接**拦腰掐断** —— 没有状态码、
    #   没有 body，浏览器只会报一句 "NetworkError when attempting to fetch
    #   resource"，用户以为是网络坏了，实际是服务端某一行 Python 写错了。
    #   2026-10-13 的 rid_obj（create_job 的 seedvc 分支引用了 train 分支才赋值
    #   的变量）就是这么藏了很久：界面只有 NetworkError，日志里才有 UnboundLocalError。
    #   有了这层兜底，最差情况也是一个**带着中文原因的 JSON 500**，界面当场可见。
    def _guarded(self, fn):
        try:
            return fn()
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            # 对面已经断开了，再写回包只会再炸一次
            raise
        except Exception as e:
            traceback.print_exc()
            try:
                json_response(self, {"error": "服务端内部错误：%s" % e}, 500)
            except Exception:
                pass

    def do_GET(self):
        return self._guarded(self._route_get)

    def do_HEAD(self):
        # 媒体播放器开工前常先发一个 HEAD 问"这玩意多大 / 支不支持 Range"。
        # 以前没有这个入口 ⇒ 501，播放器会把服务端当成"不认识 Range"，
        # 于是退回"整文件下完再播"。跟 206 一起修，两条缺一条效果就打折。
        return self._guarded(self._route_get)

    def do_POST(self):
        return self._guarded(self._route_post)

    def do_DELETE(self):
        return self._guarded(self._route_delete)

    # ------------------------------------------------------------- GET

    def _route_get(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)

        if path == "/" or path == "/index.html":
            return self.serve_file(os.path.join(WEB, "index.html"))
        if path.startswith("/static/"):
            return self.serve_file(os.path.join(WEB, path[len("/static/"):]))

        if path == "/api/tone":
            # 「每一句的语气」给界面用的清单：有哪些语气、每种还能怎么叫。
            # 别名要给全 —— 界面上要能把「可以写「愤怒」「暴怒」「气愤」」显示出来，
            # 不然用户只能猜自己写的那个词认不认。
            return json_response(self, {
                "tones": studio.tone.options(),
                "aliases": dict((k, list(v)) for k, v in studio.tone.ALIASES.items()),
                "example": "[愤怒]你怎么敢这么做！\n（温柔）别怕，我在这里。\n[低沉][急促]快走，他们要来了！",
            })
        if path == "/api/tone/parse":
            # 给界面的实时预览用：**解析规则只有服务端这一份**。
            # 前端再实现一遍 split_tone 的话，两边规则必然慢慢走偏 ——
            # 界面显示"识别成愤怒"、实际合成却是别的，这种 bug 最难查。
            qs = ""
            if "?" in self.path:
                qs = self.path.split("?", 1)[1]
            q = urllib.parse.parse_qs(qs)
            script = (q.get("script") or [""])[0]
            lines = studio.tone.parse_script(script)
            for it in lines:
                t = studio.tone.TONES_BY_KEY.get(it.get("tone") or "")
                it["tone_name"] = (t or {}).get("name", "")
            return json_response(self, {"lines": lines})
        if path == "/api/yue2/status":
            # YuE2（凭空写歌的引擎）现在什么状态。**只读**：不拉起、不改任何东西 ——
            # 界面刷新一次就顺手拉起一个引擎进程，那是很讨厌的行为。
            # 真正要跑的时候 make_original_song 会自己 ensure_engine()。
            try:
                from stages import yue2 as _y2
                return json_response(self, {
                    "ready": _y2.is_ready(),
                    "text": _y2.status_text(),
                    "missing": _y2.missing_models(),
                    "root": _y2.YUE2_ROOT,
                    "engine": _y2.DEFAULT_ENGINE,
                    "formats": list(_y2.KNOWN_FMTS),
                })
            except Exception as e:
                return json_response(self, {
                    "ready": False, "text": "YuE2 模块不可用：%s" % e,
                    "missing": [], "root": "", "engine": "", "formats": []})

        if path == "/api/roles":
            db = studio.load_roles()
            # 附上「现在能干什么」。**浅拷贝**着附：能力是每次读时现算的读数，
            # 绝不能因为一次列表请求就写进 roles.json（铁律 12d）。
            roles = [dict(r, ability=studio.role_ability(r)) for r in db["roles"]]
            return json_response(self, {"roles": roles,
                                        "summary": studio.ability_summary(db["roles"])})
        if path.startswith("/api/role/") and path.endswith("/rvc_state"):
            rid = path[len("/api/role/"):-len("/rvc_state")]
            if not studio.get_role(rid):
                return json_response(self, {"error": "没有这个角色"}, 404)
            # 问 RVC「这个实验已经训到第几轮」。要起一次 RVC 的 python（约 3 秒），
            # 所以界面只在打开角色详情时问一次，不做进 /api/roles 的轮询里。
            n = studio.rvc_trained_epochs(rid)
            return json_response(self, {"epochs": n, "exp": rid})
        if path.startswith("/api/role/") and not path[len("/api/role/"):].count("/"):
            rid = path[len("/api/role/"):]
            role = studio.get_role(rid)
            if not role:
                return json_response(self, {"error": "没有这个角色"}, 404)
            # 顺带把素材的真实路径给前端，省得它自己拼（拼错就是 404 的音频）
            d = studio.role_dir(rid)
            paths = {}
            for s in role.get("segments", []):
                p = os.path.join(d, "slices", s["file"])
                if os.path.isfile(p):
                    paths[s["file"]] = p
            return json_response(self, {"role": dict(role, ability=studio.role_ability(role)),
                                        "dir": d, "paths": paths})
        if path == "/api/seedvc/state":
            # Seed-VC 的面板状态：能不能用 / 训练集多少 / 有哪几档 / 现在用哪档。
            # 全是读目录和 roles.json，毫秒级，不做缓存（缓存过一次就会
            # 出现"刚训完档位不出现"的假象）。
            rid = (q.get("role") or [""])[0].strip()
            role = studio.get_role(rid) if rid else None
            if not role:
                return json_response(self, {"error": "没有这个角色"}, 404)
            try:
                return json_response(self, seedvc.state(role))
            except Exception as e:
                return json_response(self, {"error": "读取 Seed-VC 状态失败：%s" % e}, 500)

        if path == "/api/jobs":
            # limit：只回最近 N 条。历史任务会一直堆（实测 1022 条 = 822 KB），
            # 而调用方常常只看前 30 条 ⇒ 不加 limit 就是每轮白传 800 KB。
            # 前端 tickJobs 曾经每 1.6 秒无条件拉一次全量，光这一项就
            # 4 MB / 8 秒的持续流量，界面跟着周期性卡顿。
            lim = 0
            try:
                lim = int((q.get("limit") or ["0"])[0])
            except Exception:
                lim = 0
            alive = (q.get("alive") or [""])[0] in ("1", "true", "yes")
            jobs = studio.list_jobs()[::-1]   # 最新的在前
            # alive：只回还没跑完的。前端 restoreJobs() 每次刷新都要问一遍
            # "哪些任务还活着"，以前为了这个拉全量（1030 条 ≈ 819 KB），
            # 而真正的活跃任务通常就几个 ⇒ 白传八百多 KB。
            if alive:
                jobs = [j for j in jobs
                        if j.get("state") in ("running", "queued")]
            if lim > 0:
                jobs = jobs[:lim]
            return json_response(self, {"jobs": jobs})
        if path.startswith("/api/job/") and path.endswith("/log"):
            jid = path[len("/api/job/"):-len("/log")]
            off = int((q.get("offset") or ["0"])[0])
            txt, total = studio.job_log(jid, off)
            return json_response(self, {"text": txt, "offset": total})
        if path.startswith("/api/job/"):
            jid = path[len("/api/job/"):]
            job = studio.get_job(jid)
            if not job:
                return json_response(self, {"error": "没有这个任务"}, 404)
            return json_response(self, {"job": studio.job_public(job)})

        # ---- 声音市场：搜别人训练好的模型，挑中哪个下哪个 ----
        if path == "/api/models/works":
            return json_response(self, {"works": modelhub.works()})
        if path == "/api/models/tags":
            return json_response(self, {"tags": modelhub.tags()})
        if path == "/api/models/browse":
            # 按标签浏览（热门）。这才是"找到更多"的入口：按标签一次能列出几百个
            # 仓库，比按关键词搜一次十来个宽得多。
            tag = (q.get("tag") or ["rvc"])[0].strip()
            try:
                off = max(0, int((q.get("offset") or ["0"])[0]))
                lim = min(48, max(1, int((q.get("limit") or ["24"])[0])))
            except Exception:
                off, lim = 0, 24
            try:
                return json_response(self, modelhub.browse(tag, limit=lim)
                                     if off == 0 else
                                     modelhub.page((q.get("qid") or [""])[0], off, lim))
            except Exception as e:
                return json_response(self, {"error": "浏览失败（网络可能不通）：%s" % e}, 502)
        if path == "/api/models/page":
            # 翻页：带上一次返回的 qid，只问这一页的文件清单
            qid = (q.get("qid") or [""])[0].strip()
            if not qid:
                return json_response(self, {"error": "没带 qid（先搜一次）"}, 400)
            try:
                off = max(0, int((q.get("offset") or ["0"])[0]))
                lim = min(48, max(1, int((q.get("limit") or ["24"])[0])))
            except Exception:
                off, lim = 0, 24
            try:
                return json_response(self, modelhub.page(qid, off, lim))
            except Exception as e:
                return json_response(self, {"error": "翻页失败：%s" % e}, 502)
        if path == "/api/models/search":
            kw = (q.get("q") or [""])[0].strip()
            if not kw:
                return json_response(self, {"error": "请输入关键词"}, 400)
            try:
                limit = min(48, max(1, int((q.get("limit") or ["24"])[0])))
            except Exception:
                limit = 24
            try:
                # pool=True：返回第一页 + qid（界面底部"加载更多"靠它翻）
                return json_response(self, modelhub.search(
                    kw, limit=limit, work_key=(q.get("work") or [None])[0], pool=True))
            except Exception as e:
                return json_response(self, {"error": "搜索失败（网络可能不通）：%s" % e}, 502)
        if path == "/api/models/work":
            key = (q.get("key") or [""])[0].strip()
            if not key:
                return json_response(self, {"error": "没说是哪个作品"}, 400)
            try:
                return json_response(self, modelhub.search_work(key))
            except Exception as e:
                return json_response(self, {"error": "搜索失败（网络可能不通）：%s" % e}, 502)
        if path == "/api/models/detail":
            repo = (q.get("repo") or [""])[0].strip()
            if not repo:
                return json_response(self, {"error": "没说哪个仓库"}, 400)
            # sizes=True：顺手把每个包多大问出来。合集仓库动辄几十个包、几个 GB，
            # 用户要"自己决定下载"，就得先把代价摆出来。
            d = modelhub.detail(repo, sizes=True)
            if d.get("error"):
                # ★ 用 404，不用 502：仓库不存在/没公开是"请求里这个 repo 不对"，
                #   不是"服务端或上游坏了"。混成 5xx 的后果是前端把所有情况都显示
                #   成"网络不通"，用户会以为再点一次就好了 —— 实际再点一百次也没有。
                return json_response(self, d, 404)
            # 顺手把"该下哪个文件"挑好：前端不用自己猜 zip 里头有什么
            d["suggest"] = modelhub.pick_weight(d.get("files") or [])[0]
            return json_response(self, d)
        if path == "/api/models/installed":
            # 已装的 = 角色库里来源是 modelhub 的那些
            rows = []
            for r in studio.load_roles().get("roles", []):
                src = r.get("source") or ""
                if src.startswith("modelhub:"):
                    rows.append({"id": r["id"], "name": r.get("name"),
                                 "repo": src.split(":", 1)[1],
                                 "rvc": r.get("rvc") or {},
                                 "outputs": len(r.get("outputs") or [])})
            return json_response(self, {"installed": rows[::-1]})

        if path == "/api/music/sources":
            return json_response(self, {"sources": music_src.source_list()})

        if path == "/api/music/search":
            keyword = (q.get("q") or [""])[0].strip()
            if not keyword:
                return json_response(self, {"error": "请输入搜索关键词"}, 400)
            # 单页上限 29（ccmixter 的 PHP 内存限制，limit>=30 会返回
            # 200 + 空body，看起来成功其实什么都没有），所以"多要歌"走 deep。
            try:
                limit = min(29, max(1, int((q.get("limit") or ["20"])[0])))
            except Exception:
                limit = 20
            try:
                deep = min(8, max(0, int((q.get("deep") or ["0"])[0])))
            except Exception:
                deep = 0
            try:
                offset = max(0, int((q.get("offset") or ["0"])[0]))
            except Exception:
                offset = 0
            lic = (q.get("lic") or ["by"])[0]
            only_ok = (q.get("only_ok") or ["0"])[0] in ("1", "true", "yes")
            try:
                res = music_src.search_all(keyword, limit=limit,
                                           only_ok=only_ok, lic=lic,
                                           deep=deep, offset=offset)
            except Exception as e:
                return json_response(self, {"error": "搜索失败: %s" % e}, 502)
            res["query"] = keyword
            res["deep"] = deep
            res["offset"] = offset
            return json_response(self, res)

        if path == "/api/music/library":
            return json_response(self, {"songs": music_src.local_library()})

        if path == "/api/music/local/check":
            # 死代码复活：check_library 之前写了但全库零调用方，等于没有这功能。
            songs, stat = local_lib.check_library()
            return json_response(self, {"songs": songs, "stat": stat})

        if path == "/api/music/local/dirs":
            return json_response(self, {"dirs": local_lib.candidate_dirs()})

        if path == "/api/music/local/scan":
            d = (q.get("dir") or [""])[0].strip()
            if not d:
                return json_response(self, {"error": "请给出目录路径"}, 400)
            if not os.path.isdir(os.path.expanduser(d)):
                return json_response(self, {"error": "目录不存在：%s" % d}, 400)
            try:
                # ★ 剪枝规则必须与 netease_dirs()/candidate_dirs() 完全一致。
                #   踩过：候选目录 chip 上写「5 首」（那边传了 skip_dirs、剪掉了
                #   客户端提示音），点进来扫出「9 首」（这边没传）⇒ 同一个目录
                #   两个数字。用户会以为自己的歌丢了。**同一个目录只许一个数。**
                res = local_lib.scan_dir(
                    d, need_dur=(q.get("dur") or ["0"])[0] == "1",
                    skip_dirs=local_lib.PROG_DIRS)
            except Exception as e:
                return json_response(self, {"error": "扫描失败：%s" % e}, 500)
            # 扫描结果可能很大，只回前 2000 条，界面够用
            res["items"] = res["items"][:2000]
            return json_response(self, res)

        if path == "/api/env":
            return json_response(self, {
                "pkg": studio.GSV_ROOT,
                "rvc": studio.RVC_ROOT,
                "gpu": gpu_name(),
                "uploads": studio.UPLOADS,
            })

        # ---- 存储扫描 ----
        # 扫十几个 G 的文件元数据要几秒，而且用户会开着面板反复刷新 ⇒ 做缓存。
        # 但缓存不能无限期：刚清完一堆东西，界面上还写着"可释放 17G"会让人
        # 以为没删掉。所以 TTL 给 2 分钟，且 ?refresh=1 强制重扫。
        if path == "/api/storage":
            want_refresh = (q.get("refresh") or ["0"])[0] in ("1", "true", "yes")
            data = storage_snapshot(force=want_refresh)
            return json_response(self, data)

        if path == "/api/viz/options":
            return json_response(self, studio.viz_options())

        if path == "/api/viz/library":
            return json_response(self, {"videos": studio.viz_library()})

        if path == "/media":
            p = (q.get("path") or [""])[0]
            real = safe_under(p, [DATA, studio.GSV_ROOT, studio.RVC_ROOT, studio.UPLOADS])
            if not real or not os.path.isfile(real):
                return json_response(self, {"error": "文件不可读: %s" % p}, 404)
            return self.serve_file(real)

        return json_response(self, {"error": "接口不存在: " + path}, 404)

    # ------------------------------------------------------------- POST

    def _route_post(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        q = urllib.parse.parse_qs(u.query)

        if path == "/api/upload":
            return self.handle_upload(q)

        # **必须和 /api/upload 放在一起，在 read_json_body 之前**。
        # 这条路由收的是二进制音频流，不是 JSON；放在 read_json_body 之后，
        # body 已经被当 JSON 读走 ⇒ 后面self.rfile 读不到数据 ⇒ 请求挂死。
        # （症状是上传永远转圈/超时，而后端日志只有一堆 ConnectionAborted，
        #   看起来像网络问题，实际是路由顺序写错了。）
        if path == "/api/music/local/upload":
            return self.music_local_upload()

        body = read_json_body(self)

        if path == "/api/roles":
            role = studio.create_role(
                name=body.get("name"), kind=body.get("kind", "speech"), source=body.get("source", ""))
            return json_response(self, {"role": role})

        if path == "/api/roles/retranslate":
            # 把**已经装进来**的市场角色里的英文名补译成中文（本地字典 + 音译，
            # 不联网不花钱）。默认只动 source=modelhub: 的那些；
            # all=true 时才连自己建的角色一起翻（默认是关的 —— 那种名字是用户自己起的）。
            try:
                done = studio.retranslate_imported(all_roles=bool(body.get("all")))
            except Exception as e:
                return json_response(self, {"error": "补译没做成：%s" % e}, 500)
            return json_response(self, {"renamed": done, "count": len(done)})

        if path.startswith("/api/role/") and "/" in path[len("/api/role/"):]:
            rest = path[len("/api/role/"):]
            rid, action = rest.split("/", 1)
            return self.role_action(rid, action, body)

        if path.startswith("/api/role/"):
            rid = path[len("/api/role/"):]
            r = studio.update_role(rid, body)
            if not r:
                return json_response(self, {"error": "没有这个角色"}, 404)
            return json_response(self, {"role": r})

        if path == "/api/jobs":
            return self.create_job(body)

        # 暂停 / 继续。放在 /api/jobs 之后、404 之前。
        if path.startswith("/api/job/") and "/" in path[len("/api/job/"):]:
            rest = path[len("/api/job/"):]
            jid, action = rest.split("/", 1)
            if action in ("pause", "resume"):
                fn = studio.pause_job if action == "pause" else studio.resume_job
                ok, msg = fn(jid)
                j = studio.get_job(jid)
                return json_response(
                    self, {"ok": ok, "msg": msg,
                           "job": studio.job_public(j) if j else None},
                    200 if ok else 400)
            if action == "cancel":
                # 取消 = 终止进程树 + 落 cancelled。cleanup 是"连中间产物一起删"，
                # 默认不开：用户可能只是想停手，但想留着半成品看一眼。
                ok, msg = studio.cancel_job(jid, cleanup=bool(body.get("cleanup")))
                j = studio.get_job(jid)
                return json_response(
                    self, {"ok": ok, "msg": msg,
                           "job": studio.job_public(j) if j else None},
                    200 if ok else 400)

        if path == "/api/music/fetch":
            return self.music_fetch(body)

        if path == "/api/music/local/add":
            return self.music_local_add(body)

        if path == "/api/music/local/decrypt":
            # 把网易云 .ncm 解密成标准音频并登记进曲库。
            # 跟"整目录导入"分开的原因：目录导入是批量的、走后台任务；
            # 这里通常是一首歌，用户下一步就是"用它翻唱"，同步返回路径最顺手。
            paths = body.get("paths")
            if isinstance(paths, str):
                paths = [paths]
            paths = [p for p in (paths or []) if p]
            if not paths:
                return json_response(self, {"error": "没有给出要解密的文件"}, 400)
            try:
                out = local_lib.add_files(paths)
            except Exception as e:
                return json_response(self, {"error": "解密失败：%s" % e}, 500)
            return json_response(self, out)

        if path == "/api/music/local/relink":
            r = local_lib.relink(body.get("item"), body.get("path"))
            return json_response(self, r, 200 if r.get("ok") else 400)

        if path == "/api/music/local/forget":
            r = local_lib.forget(body.get("item"))
            return json_response(self, r, 200 if r.get("ok") else 400)

        # ---- 存储清理 ----
        # dry=1 只算不删（界面用它做"确认前先看看能释放多少"）。
        # 真删是**不可撤销**的，所以每一步的准入判断都在 studio.storage_delete
        # 里（白名单根目录 + 角色在用锁 + 运行中任务锁），这里只是转发。
        if path == "/api/storage/delete":
            items = body.get("items") or []
            if not items:
                return json_response(self, {"error": "没有勾选任何东西"}, 400)
            if len(items) > 2000:
                return json_response(self, {"error": "一次最多删 2000 项"}, 400)
            dry = bool(body.get("dry"))
            try:
                res = studio.storage_delete(items, dry=dry)
            except Exception as e:
                return json_response(self, {"error": "删除失败：%s" % e}, 500)
            # 删完缓存立刻失效：不然界面上还挂着已经不存在的条目，
            # 用户再点一次就是"删了还在"的假象。
            with _STORE_LOCK:
                _STORE["at"] = 0.0
                _STORE["data"] = None
            res["ok"] = not res["failed"]
            return json_response(self, res)

        if path == "/api/storage/settings":
            try:
                s = studio.save_storage_settings(body.get("patch") or {})
            except Exception as e:
                return json_response(self, {"error": "保存失败：%s" % e}, 500)
            with _STORE_LOCK:
                _STORE["at"] = 0.0
            return json_response(self, {"settings": s})

        if path == "/api/storage/prune":
            """一键瘦身：按设置把「RVC 中间存档」和「分离缓存」压回上限。"""
            st = studio.storage_settings()
            out = {"deleted": [], "failed": [], "freed": 0}
            exps = set()
            wd = studio._rvc_weights_dir()
            if os.path.isdir(wd):
                import re
                for f in os.listdir(wd):
                    m = re.match(r"^(.*?)_e\d+_s\d+\.pth$", f)
                    if m:
                        exps.add(m.group(1))
            for exp in sorted(exps):
                r = studio.prune_rvc_archives(exp, keep=st.get("keep_archives", 2))
                out["deleted"] += r.get("deleted", [])
                out["failed"] += r.get("failed", [])
                out["freed"] += r.get("freed", 0)
            r = studio.prune_cache(max_gb=st.get("cache_max_gb", 3.0))
            out["deleted"] += r.get("deleted", [])
            out["failed"] += r.get("failed", [])
            out["freed"] += r.get("freed", 0)
            out["freed_s"] = studio._humansize(out["freed"])
            with _STORE_LOCK:
                _STORE["at"] = 0.0
                _STORE["data"] = None
            return json_response(self, out)

        return json_response(self, {"error": "接口不存在: " + path}, 404)

    def _route_delete(self):
        u = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(u.path)
        if path.startswith("/api/role/"):
            rid = path[len("/api/role/"):]
            studio.delete_role(rid)
            return json_response(self, {"ok": True})
        return json_response(self, {"error": "接口不存在"}, 404)

    # -------------------------------------------------------------

    def handle_upload(self, q):
        name = (q.get("name") or [""])[0] or ("upload_%d.bin" % int(__import__("time").time()))
        name = os.path.basename(name).replace("/", "_").replace("\\", "_")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return json_response(self, {"error": "没有收到数据"}, 400)
        dst = os.path.join(studio.UPLOADS, name)
        os.makedirs(studio.UPLOADS, exist_ok=True)
        written = 0
        with open(dst, "wb") as f:
            while written < n:
                chunk = self.rfile.read(min(1 << 20, n - written))
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        # ★ `keep=1` = **别动这个文件，原样留着**。
        #   「做视频」要拿用户的视频当画面时就得走这条：下面的逻辑会把视频
        #   当场抽成音轨，而那一步恰恰会把画面丢掉。
        if (q.get("keep") or [""])[0] in ("1", "true", "yes"):
            return json_response(self, {"path": dst, "size": written,
                                        "kept": True, "note": None})

        # ★ 视频直接当歌用：歌这条链路（分离 / 变声 / 混音）只认音频，
        #   拿一个 mp4 进去每一步都会以奇怪的方式失败。所以这里**当场**抽音轨，
        #   一步到位，界面拿到的就已经是能用的音频了。
        note = None
        from_video = False
        if studio.is_video(dst):
            audio, why = studio.extract_audio(dst)
            if not audio:
                return json_response(
                    self, {"error": "这是视频文件，但没能从里面抽出音轨。\n%s\n"
                                    "可以先在本地转成 mp3/wav 再拖进来。" % why}, 400)
            note = why
            dst = audio
            from_video = True
        elif os.path.splitext(dst)[1].lower() == ".ncm":
            # ★ 网易云 VIP 歌下载下来是 .ncm —— 它是**加密容器**，不是音频。
            #   以前这里只处理视频，.ncm 就原样落进 uploads/ 被当歌用，直到
            #   分离那一步 ffmpeg 才报 "Invalid data found"（界面还说"文件损坏"），
            #   用户根本不知道要先去客户端导出。和视频一个道理：入口处一次做对。
            try:
                # 解到 uploads/（不是分离缓存区）：曲库会长期记住这个路径，
                # 放缓存区一旦清缓存，曲库里那条就变成"文件丢失"了。
                decoded = studio.ensure_playable_audio(None, dst,
                                                       out_dir=studio.UPLOADS)
            except Exception as e:
                return json_response(self, {"error": str(e)}, 400)
            if decoded == dst:
                return json_response(
                    self, {"error": "这是 .ncm 文件，但没能解密成音频。\n"
                                    "可以在网易云客户端里导出成 mp3 再拖进来。"}, 400)
            note = "已自动解密成音频：" + os.path.basename(decoded)
            dst = decoded
        return json_response(self, {"path": dst, "size": written,
                                    "from_video": from_video,
                                    "from_ncm": bool(note and not from_video),
                                    "note": note})

    def role_action(self, rid, action, body):
        role = studio.get_role(rid)
        if not role:
            return json_response(self, {"error": "没有这个角色"}, 404)

        if action == "segment-text":
            fname = body.get("file")
            text = body.get("text", "")

            def _set(r):
                for s in r["segments"]:
                    if s["file"] == fname:
                        s["text"] = text

            studio.mutate_role(rid, _set)
            studio.write_list(rid)
            return json_response(self, {"ok": True})

        if action == "segment-delete":
            fname = body.get("file")
            studio.mutate_role(
                rid, lambda r: r.__setitem__(
                    "segments", [s for s in r["segments"] if s["file"] != fname]))
            try:
                os.remove(os.path.join(studio.role_dir(rid), "slices", fname))
            except Exception:
                pass
            studio.write_list(rid)
            studio.refresh_stats(rid)
            return json_response(self, {"ok": True})

        if action == "seedvc-pick":
            # 换用微调出来的某一档。档位是**按步数**选的（ft200/400/600…），
            # 因为步数就是实测里唯一有判别力的那个维度（600 步最好，1000 步过拟合）。
            rows = seedvc.ckpts(role)
            if not rows:
                return json_response(
                    self, {"error": "这个角色还没有微调档位，先跑一次微调"}, 400)
            want = str(body.get("ckpt") or "").strip()
            step = body.get("step")
            hit = None
            for r in rows:
                if want and (r["path"] == want
                             or os.path.basename(r["path"]) == os.path.basename(want)):
                    hit = r
                    break
            if hit is None and step not in (None, ""):
                try:
                    st = int(step)
                except Exception:
                    st = -1
                hit = next((r for r in rows if r["step"] == st and not r["final"]), None)
            if hit is None:
                return json_response(self, {"error": "没有这一档：%s" % (want or step)}, 400)
            if not os.path.isfile(seedvc.ckpt_abs(hit["path"])):
                return json_response(
                    self, {"error": "这一档的文件已经不在磁盘上了：%s" % hit["path"]}, 400)

            def _set(r):
                sv = dict(r.get("seedvc") or {})
                sv.update({"ckpt": hit["path"], "step": hit["step"],
                           "at": studio._now()})
                r["seedvc"] = sv

            studio.mutate_role(rid, _set)
            return json_response(self, {"ok": True, "ckpt": hit,
                                        "role": studio.get_role(rid)})

        if action == "set-ref":
            fname = body.get("file")
            text = ""
            for s in role["segments"]:
                if s["file"] == fname:
                    text = s.get("text", "")
            studio.update_role(rid, {"ref": {"file": fname, "text": text}})
            return json_response(self, {"ok": True})

        return json_response(self, {"error": "不支持的动作: " + action}, 404)

    def music_fetch(self, body):
        """下载一首歌到本地曲库，返回真实落盘结果（带时长体检）。

        这里同步下载：翻唱台的"用它翻唱"是紧跟下一步的动作，
        异步会让前端多一套状态机。搜索已经够慢了，下载走后台线程更好，
        所以这里起线程并立刻返回 job。
        """
        item_id = (body.get("id") or "").strip()
        if not item_id:
            return json_response(self, {"error": "缺少曲目 id"}, 400)
        title = body.get("title") or item_id
        job = studio.new_job(None, "music", "下载歌曲：%s" % title)
        studio.run_in_thread(job, lambda: studio.music_fetch_job(
            job, item_id, title=title))
        return json_response(self, {"job": studio.job_public(job)})

    def music_local_add(self, body):
        """把本地文件/整目录登记进曲库。

        同步执行：本地文件不需要下载，通常 <2 秒，异步反而让用户多点一次。
        坏文件在 local_lib.add_files 里就会被拒，这里把拒绝原因原样回传 ——
        绝不能"看起来导入了"其实塞了个假音频进去。
        """
        paths = body.get("paths")
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            one = (body.get("dir") or "").strip()
            if not one:
                return json_response(self, {"error": "缺少文件路径或目录"}, 400)
            if not os.path.isdir(os.path.expanduser(one)):
                return json_response(self, {"error": "目录不存在：%s" % one}, 400)
            job = studio.new_job(None, "music", "导入目录：%s" % os.path.basename(one))
            def _work():
                job["stage"] = "扫描目录"
                job["pct"] = 10
                studio.log(job, "扫描：%s" % one)
                r = local_lib.scan_dir(one)
                if not r["ok"]:
                    raise studio.Fail("目录不存在：%s" % one)
                studio.log(job, "找到 %d 个音频，%d 个被跳过"
                           % (len(r["items"]), len(r["skipped"])))
                for s in r["skipped"][:40]:
                    studio.log(job, "  跳过 %s —— %s" % (s["name"], s["why"]))
                job["stage"] = "登记曲库"
                job["pct"] = 60
                out = local_lib.add_files([i["path"] for i in r["items"]])
                for b in out["bad"][:40]:
                    studio.log(job, "  拒绝 %s —— %s" % (b["path"], b["why"]))
                job["pct"] = 100
                studio.log(job, "已导入 %d 首" % len(out["added"]))
                return {"added": out["added"], "bad": out["bad"],
                        "skipped": r["skipped"], "root": r["root"]}
            studio.run_in_thread(job, _work)
            return json_response(self, {"job": studio.job_public(job)})

        try:
            out = local_lib.add_files(paths)
        except Exception as e:
            return json_response(self, {"error": "导入失败：%s" % e}, 500)
        return json_response(self, out)

    def music_local_upload(self):
        """把浏览器上传的音频收进曲库。

        为什么需要它：浏览器的 File 对象**没有真实磁盘路径**（只有 name/size），
        所以"拖一个mp3 进来就登记原路径"在纯前端里做不到。
        但 File **有内容** —— 所以正确做法是：把内容收到 data/music/ 再登记。
        这跟"只登记路径不复制"是两种不同语义，各有用处：
          · 整目录导入 → 登记路径（省硬盘，原文件留在原地可分轨编辑）
          · 单文件拖入 → 复制一份（用户不必先知道文件在哪）
        两条路都在，界面上说清楚区别，不假装浏览器能给路径。

        **落盘后必须过 add_files 那套校验**（魔数 + 最小体积 + 时长），
        否则这里就是个绕过质量闸门的后门：拖个改名成 .mp3 的 txt 就能进库。
        """
        q = urllib.parse.urlparse(self.path)
        qq = urllib.parse.parse_qs(q.query)
        name = (qq.get("name") or [""])[0] or "upload.bin"
        name = os.path.basename(name).replace("/", "_").replace("\\", "_")
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return json_response(self, {"ok": False, "err": "没有收到数据"}, 400)

        # 曲库目录的唯一定义在 music_src.MUSIC_DIR（studio 里没有这个常量，
        # 之前想当然写 studio.MUSIC_DIR 会 AttributeError —— 先查再用）
        os.makedirs(music_src.MUSIC_DIR, exist_ok=True)
        # 落盘名带上时间戳，避免同名互相覆盖（覆盖 = 悄悄毁掉用户已导入的歌）
        stem, ext = os.path.splitext(name)
        # .ncm 先落在 uploads（临时），add_files 会把它解密到曲库目录，
        # 之后这份副本会被删掉 —— 没必要在曲库目录里同时留一份解不开的原件。
        tmp_ncm = ext.lower() in local_lib.DECRYPTABLE
        dst_dir = studio.UPLOADS if tmp_ncm else music_src.MUSIC_DIR
        os.makedirs(dst_dir, exist_ok=True)
        dst = os.path.join(dst_dir,
                           "%s__up_%s%s" % (studio._now().replace(":", "").replace(" ", "_"),
                                             stem[:40], ext))
        if os.path.exists(dst):
            return json_response(self, {"ok": False, "err": "同名文件已存在，换个名字再试"}, 409)

        written = 0
        try:
            with open(dst, "wb") as f:
                while written < n:
                    chunk = self.rfile.read(min(1 << 20, n - written))
                    if not chunk:
                        break
                    f.write(chunk)
                    written += len(chunk)
        except Exception as e:
            if os.path.exists(dst):
                try: os.remove(dst)
                except Exception: pass
            return json_response(self, {"ok": False, "err": "保存失败：%s" % e}, 500)

        if written != n:
            try: os.remove(dst)
            except Exception: pass
            return json_response(self, {"ok": False, "err": "文件没传完整（收到 %d/%d 字节）" % (written, n)}, 400)

        # 关键：走和目录导入**同一条**校验链，不给上传开后门
        out = local_lib.add_files([dst])
        if not out["added"]:
            why = out["bad"][0]["why"] if out["bad"] else "未知原因"
            try: os.remove(dst)
            except Exception: pass
            return json_response(self, {"ok": False, "err": "这个文件不能入库：" + why}, 400)

        song = out["added"][0]
        # .ncm 已经解密到曲库目录了，uploads 里那份副本没用了 —— 删掉，
        # 免得同一首歌在硬盘上留两份（原件还在用户自己机器上，动都不动）。
        if tmp_ncm:
            try:
                os.remove(dst)
            except Exception:
                pass
            song["from_ncm_name"] = name

        # 落盘名带了时间戳（防同名覆盖），但**给用户看的标题要用原文件名**，
        # 否则曲库里会出现「2026-10-10_1234__up_七里香」这种没法读的条目。
        # 目录导入没这个问题（登记的是原路径），所以这里单独修一次。
        clean = os.path.splitext(name)[0]
        # .ncm 的标题已经在解密时拿到真实歌名了（比文件名准），别用文件名盖掉
        if tmp_ncm and song.get("from_ncm"):
            clean = song.get("title") or clean
        if song.get("title") != clean:
            song["title"] = clean
            db = studio.load_library()
            for s in db.get("songs", []):
                if s.get("path") == song.get("path"):
                    s["title"] = clean
            studio.save_library(db)
        return json_response(self, {"ok": True, "songs": out["added"],
                                    "decrypted": bool(tmp_ncm)})

    def create_job(self, body):
        kind = body.get("kind")
        rid = body.get("role")
        if kind == "ingest":
            # ★ 支持一次传多个视频（bulk）：它们通常是同一个人的几段素材，
            #   界面上有三种处理方式（合成一个 / 自动分人 / 每视频一个）。
            raw = body.get("videos")
            vids = [v for v in raw if v] if isinstance(raw, list) else []
            if not vids and body.get("video"):
                vids = [body["video"]]
            vids = [v for v in vids if v and os.path.isfile(v)]
            if not vids:
                return json_response(
                    self, {"error": "没有可用的视频文件：%s"
                           % (body.get("video") or body.get("videos"))}, 400)
            mode = body.get("mode") or "auto"
            if mode not in ("auto", "merge", "per_video", "match"):
                return json_response(self, {"error": "不认识的导入方式: %s" % mode}, 400)
            append_to = body.get("append_to")
            if append_to and not studio.get_role(append_to):
                return json_response(
                    self, {"error": "要追加到的角色不存在: %s" % append_to}, 400)
            title = ("追加素材到：%s" % (studio.get_role(append_to) or {}).get("name", append_to)) \
                if append_to else (
                    "拆解 %d 个视频：%s" % (len(vids), "、".join(
                        os.path.basename(v) for v in vids[:2])
                        + ("…" if len(vids) > 2 else ""))
                    if len(vids) > 1 else "拆解视频：%s" % os.path.basename(vids[0]))
            job = studio.new_job(None, "ingest", title)
            # 导入框里也能填自定义轮数，所以这里跟 /api/jobs?kind=train 走同一套
            # 上限收口。**不收口就是两个入口两种口径**：角色页被卡在 200，
            # 而导入这条路可以随便传——上限就形同虚设。
            in_eps = None
            if body.get("epochs"):
                in_eps = studio.clamp_epochs(body["epochs"], "gsv")[0] or None
            studio.run_in_thread(job, lambda: studio.job_ingest(
                job, None,
                videos=vids,
                mode=mode,
                append_to=append_to,
                n_speakers=body.get("n_speakers"),
                uvr=bool(body.get("uvr", True)),
                lang=body.get("lang", "zh"),
                epochs=in_eps,
                quality=body.get("quality", "standard"),
                do_train=bool(body.get("do_train", True)),
            ))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "train":
            rid_obj = studio.get_role(rid) or {}
            q = body.get("quality", "standard")
            eps = int(body["epochs"]) if body.get("epochs") else studio.QUALITY_PRESETS.get(q, 30)
            if body.get("epochs"):
                eps = studio.clamp_epochs(eps, "gsv")[0] or studio.QUALITY_PRESETS.get(q, 30)
                q = "custom"
            job = studio.new_job(rid, "train", "训练音色：%s" % rid_obj.get("name", rid),
                                 epochs_total=eps)
            studio.run_in_thread(job, lambda: studio.train_role(
                job, rid,
                epochs=eps,
                batch=body.get("batch"),
                quality=q))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "lyrics":
            # 「自动歌词」：读一首歌 → 识别 → 把结果挂在任务上，前端识别完自动回填
            # 改词台的「原歌词」栏。走 job 而不是同步 HTTP，是因为分离+识别要一两分钟；
            # 同步请求会让界面干等还没有进度，而这个项目所有长活都必须能看到进度。
            song = body.get("song")
            if not song or not os.path.isfile(song):
                return json_response(self, {"error": "歌曲文件不存在: %s" % song}, 400)
            job = studio.new_job(None, "lyrics", "识别歌词：%s" % os.path.basename(song))
            studio.run_in_thread(job, lambda: studio.lyrics_job(
                job, song, lang=(body.get("lang") or "zh"),
                sep_model=body.get("sep_model")))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "output_lyric":
            # 给**某一条已有成品**补歌词。单独开任务，不在唱歌时顺手做：识别要下
            # 一 1.5GB 的语音识别模型，那该是用户自己点、自己看着进度等的事。
            # 角色 id 的取法整个 create_job 只有这一处（别的分支都用同一个 rid），
            # 以前这里写成 body.get("rid") ⇒ 同一个字段两种键名，别的分支认 role、
            # 这一支认 rid，谁改谁踩 ⇒ 2026-10-13 统一成 role（前端同步改）。
            if not rid:
                return json_response(self, {"error": "没说哪个角色"}, 400)
            try:
                idx = int(body.get("index") or 0)
            except Exception:
                return json_response(self, {"error": "index 得是个数字"}, 400)
            job = studio.new_job(rid, "output_lyric", "给第 %d 条成品补歌词" % (idx + 1))
            studio.run_in_thread(job, lambda: studio.job_output_lyric(
                job, rid, idx, lang=(body.get("lang") or "zh")))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "viz":
            # 做视频：把**一条成品**（或任意音频）渲染成能直接发出去的 MP4。
            # 两种来源 —— 给了 role+index 就取那条成品（连它的 .lrc 一起带上，
            # 出来就是带歌词的卡拉OK；只给 audio 就渲染那个文件。
            src = (body.get("audio") or "").strip()
            title = (body.get("title") or "").strip()
            subtitle = ""
            lrc = None
            if not src:
                if not rid:
                    return json_response(self, {"error": "没说清要给哪条成品做视频"}, 400)
                role = studio.get_role(rid)
                if not role:
                    return json_response(self, {"error": "角色不存在"}, 400)
                outs = role.get("outputs") or []
                try:
                    idx = int(body.get("index") or 0)
                except Exception:
                    return json_response(self, {"error": "index 得是个数字"}, 400)
                if idx < 0 or idx >= len(outs):
                    return json_response(self, {"error": "没有第 %d 条成品" % (idx + 1)}, 400)
                o = outs[idx]
                src = o.get("wav")
                lrc = o.get("lyric_lrc") or None
                subtitle = role.get("name") or ""
                if not title:
                    title = (o.get("text") or "").strip() or os.path.basename(src or "")
            elif not os.path.isfile(src):
                return json_response(self, {"error": "音频文件不存在: %s" % src}, 400)
            else:
                # ★ 直接给音频文件时也要有个标题。不补的话 .ass 里一条 Dialogue
                #   都没有 —— 出来是一段没有任何文字的画面，用户会以为字幕坏了。
                #   用文件名（去掉扩展名）当标题最自然。
                if not title:
                    title = os.path.splitext(os.path.basename(src))[0]

            def _num(key):
                """秒数参数：空串 / 0 / 负数一律当"没给"。"""
                try:
                    v = float(body.get(key))
                except (TypeError, ValueError):
                    return None
                return v if v and v > 0 else None

            label = title or os.path.basename(src or "音频")
            job = studio.new_job(rid, "viz", "做视频：%s" % label[:40])
            studio.run_in_thread(job, lambda: studio.job_viz(
                job, src, title=title, subtitle=subtitle, lrc_path=lrc,
                style_key=body.get("style") or "orbit",
                size_key=body.get("size") or "landscape",
                fps=body.get("fps") or 30,
                start=_num("start"), duration=_num("duration"),
                # 「用我的视频当画面」：mode 决定画面从哪来，bg_video 是那段视频
                mode=(body.get("mode") or "viz"),
                bg_video=(body.get("bg_video") or "").strip() or None))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "speak":
            # ★ 「在提示词里写语气」这条路：前端把**整段原文**丢过来，由后端自己
            #   分清哪部分是语气标签、哪部分是要说的话。
            #   不在前端就把 [愤怒] 剥掉 —— 那等于替用户决定了他写的是什么，
            #   而且一旦两边剥离规则不一致，服务端会拿到被啃过的文本。
            #   也**不**接受 texts / script 两个数组做按位配对：行数对不齐时
            #   zip 会静默错位，第 3 句套上第 4 句的语气都发现不了。
            script = body.get("script")
            lines = studio.tone.parse_script(script) if script else []
            if not lines:
                old = body.get("texts") or []
                if isinstance(old, str):
                    old = [t for t in old.replace("\n", "|").split("|") if t.strip()]
                lines = [{"text": str(t).strip(), "tone": None, "tags": [], "unknown": []}
                         for t in old if str(t).strip()]
            if not lines:
                return json_response(self, {"error": "没有要说的话"}, 400)
            texts = lines
            job = studio.new_job(rid, "speak", "合成 %d 句" % len(texts))
            studio.run_in_thread(job, lambda: studio.speak_role(
                job, rid, texts, lang=body.get("lang", "zh")))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "new_song":
            # 「角色唱新歌」：风格 + 歌词 → YuE2 凭空作曲演唱 → 再走唱歌那条管线换声。
            # 它和 kind=sing 只差「歌从哪来」：sing 要用户给一个现成文件，
            # 这里一个文件都不用给。
            if not rid or not studio.get_role(rid):
                return json_response(self, {"error": "要指定一个角色"}, 400)
            style = (body.get("style") or "").strip()
            lyrics = (body.get("lyrics") or "").strip()
            if not style:
                return json_response(
                    self, {"error": "风格不能空 —— YuE2 靠它决定曲风、乐器、人声类型和 BPM"}, 400)
            if not lyrics:
                return json_response(
                    self, {"error": "歌词不能空 —— 没有词它会只哼不唱"}, 400)
            # duration 是歌的目标长度：太短唱不完词，太长会灌水。卡在 15~360 秒。
            try:
                dur = float(body.get("duration") or 120)
            except (TypeError, ValueError):
                dur = 120.0
            dur = max(15.0, min(360.0, dur))
            try:
                steps = int(body.get("steps") or 32)
            except (TypeError, ValueError):
                steps = 32
            steps = max(8, min(64, steps))
            title = "新歌：%s" % (body.get("title") or style[:28])
            job = studio.new_job(rid, "new_song", title)
            p = body.get("pitch", 0)
            if not isinstance(p, str):
                p = int(p)
            _ir = body.get("index_rate")
            studio.run_in_thread(job, lambda: studio.make_original_song(
                job, rid, style, lyrics, duration=dur, pitch=p, steps=steps,
                index_rate=(float(_ir) if _ir not in (None, "") else None),
                rms_mix_rate=body.get("rms_mix_rate"),
                protect=body.get("protect"),
                sep_model=body.get("sep_model"),
                nonvocal=(body.get("nonvocal") or "keep"),
                prefix="new"))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "sing":
            song = body.get("song")
            if not song or not os.path.isfile(song):
                return json_response(self, {"error": "歌曲文件不存在: %s" % song}, 400)
            sq = body.get("quality", "standard")
            # 自定义轮数只在「还没有歌声模型、要先自动训」时才用得上
            sqe = body.get("epochs") if sq == "custom" else None
            if sqe:
                sqe = studio.clamp_epochs(sqe, "rvc")[0] or None
                sq = "custom"
            # 试听小样：只做一小段，不写进角色成品清单
            pv = int(body.get("preview_secs") or 0)
            # pitch 允许是数字，也允许 "auto"（界面上那一档「自动」）
            p = body.get("pitch", 0)
            if not isinstance(p, str):
                p = int(p)
            # 改词：两段都给了才算「要改词」，只填一段是没意义的
            # （原词用来定位旋律，新词才是要唱的，缺一个就无从对齐）。
            _ol = (body.get("orig_lyric") or "").strip()
            _nl = (body.get("new_lyric") or "").strip()
            lyric = (_ol, _nl) if (_ol and _nl) else None
            title = ("试听小样：%s" % os.path.basename(song)) if pv else \
                    ("一键唱：%s" % os.path.basename(song))
            if lyric:
                title = title.replace("一键唱", "改词唱").replace("试听小样", "改词试听")
            job = studio.new_job(rid, "sing", title)
            # index_rate / nonvocal 不传就用 studio 里实测选出来的默认值
            # （不要在这里写死数字 —— 默认值只能有一处，否则两边会漂）。
            _ir = body.get("index_rate")
            studio.run_in_thread(job, lambda: studio.sing_job(
                job, rid, song, pitch=p,
                index_rate=(float(_ir) if _ir not in (None, "") else None),
                quality=sq, epochs=sqe,
                auto_train=bool(body.get("auto_train", True)) and not pv,
                model=body.get("model"),
                preview_secs=pv,
                rms_mix_rate=body.get("rms_mix_rate"),
                protect=body.get("protect"),
                sep_model=body.get("sep_model"),
                lyric=lyric,
                nonvocal=(body.get("nonvocal") or "keep"),
                # 引擎：rvc（默认，老链路）/ seedvc（44.1k 精修，音色更高，
                # 需要先给这个角色跑过 Seed-VC 微调）
                engine=(body.get("engine") or "rvc")))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "install_model":
            # 装一个（或一批）市场里的模型：下载 → 解压 → 放进 RVC → 建角色。
            repo = (body.get("repo") or "").strip()
            if not repo:
                return json_response(self, {"error": "没说要装哪个仓库"}, 400)
            # all=true ⇒ 把仓库里**所有**可装的包都装上（合集仓库一装就是一批
            #   角色：小马宝莉那几个仓库里躺着 14~16 个）。不传就只装一个。
            job = studio.new_job(None, "install_model",
                                 "装模型：%s" % repo.split("/")[-1])
            studio.run_in_thread(job, lambda: studio.job_install_model(
                job, repo, fname=body.get("file"), name=body.get("name"),
                max_roles=int(body.get("max_roles") or 16),
                all_packs=bool(body.get("all"))))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "seedvc":
            # Seed-VC 角色微调：整理素材 + 训 N 步。跟 RVC 那次训练是两回事 ——
            # RVC 负责"能唱、跟得上调"，Seed-VC 负责"更像这个人"（实测音色
            # 0.7099 → 0.7674）。所以它必须建立在已经有 RVC 模型的基础上。
            if not rid:
                return json_response(self, {"error": "先选一个角色"}, 400)
            # ★ 角色对象在这里取，不要去引用别的分支里才赋值的同名变量：
            #   以前这里写的是 rid_obj —— 它只在 kind=train 那一支里赋值，
            #   seedvc 这一支跑到时它还没绑 ⇒ UnboundLocalError ⇒ 连接被掐断，
            #   界面上只剩一句 NetworkError（2026-10-13 的真事故）。
            role_obj = studio.get_role(rid) or {}
            steps = body.get("steps")
            try:
                steps = int(steps) if steps not in (None, "") else seedvc.DEFAULT_STEPS
            except Exception:
                steps = seedvc.DEFAULT_STEPS
            if steps not in seedvc.STEPS_CHOICES:
                steps = min(seedvc.STEPS_CHOICES, key=lambda c: abs(c - steps))
            if not role_obj.get("rvc"):
                return json_response(
                    self, {"error": "这个角色还没有歌声模型（RVC）。\n"
                                    "Seed-VC 微调只是在 RVC 唱出来的基础上再抬音色，"
                                    "它自己不会唱 —— 先训歌声模型。"}, 400)
            job = studio.new_job(rid, "seedvc",
                                 "Seed-VC 微调 %d 步：%s"
                                 % (steps, role_obj.get("name", rid)),
                                 epochs_total=steps)
            studio.run_in_thread(job, lambda: studio.seedvc_finetune(
                job, rid, steps=steps))
            return json_response(self, {"job": studio.job_public(job)})

        if kind == "rvc":
            rq = body.get("quality", "standard")
            req = body.get("epochs")
            if req:
                rq = "custom"
            reps = studio.clamp_epochs(req, "rvc")[0] if req else 0
            if not reps:
                reps = studio.RVC_QUALITY_PRESETS.get(rq, 150)
            else:
                rq = "custom"
            if body.get("restart"):
                title = "训练歌声模型（从头重训，用底模）"
            else:
                title = "训练歌声模型"
            job = studio.new_job(rid, "rvc", title, epochs_total=reps)
            # 底模选择：空 = 官方语音底模（默认，行为不变）；
            # "klm" = KLM 唱歌底模（见 studio.RVC_CUSTOM_PRETRAINS）。
            # ★ 架构/采样率由底模自己锁死，所以这里**不接受**用户传 sr/arch，
            #   传了也没用 —— 底模和架构对不上是"训练照跑、底模全废"的根源。
            _pt = (body.get("pretrain") or "").strip().lower() or None
            if _pt and _pt not in studio.RVC_CUSTOM_PRETRAINS:
                return json_response(
                    self, {"error": "未知底模：%s（可选：%s）"
                           % (_pt, " / ".join(studio.RVC_CUSTOM_PRETRAINS) or "无")}, 400)
            if _pt:
                _cp = studio.RVC_CUSTOM_PRETRAINS[_pt]
                if not all(studio.rvc_pretrained_here(_cp["arch"], _cp["sr"], _pt)):
                    return json_response(self, {"error":
                        "底模「%s」没放齐（%s / %s）。它不会自动下载，"
                        "先把两个 .pth 放到 assets/%s/ 再试。"
                        % (_cp["label"], _cp["g"], _cp["d"], _cp["sub"])}, 400)
            studio.run_in_thread(job, lambda: studio.train_rvc(
                job, rid, epochs=reps, quality=rq,
                restart=bool(body.get("restart")), pretrain=_pt))
            return json_response(self, {"job": studio.job_public(job)})

        return json_response(self, {"error": "不支持的任务类型: %s" % kind}, 400)

    # 流式读的时候一次搬多大。**不要**改回 open().read()：一条成品 54MB、
    #   视频 151MB，几十个并发请求一起 read() 就是几个 G 的常驻内存，
    #   攒够一次就崩，而且崩的样子是"页面卡住"不是 OOM。
    SERVE_CHUNK = 256 * 1024

    def _parse_range(self, rng, size):
        """把 `Range: bytes=a-b` 解成 [start, end]（闭区间）。

        返回 None 表示"这个 Range 不该按 206 处理"，调用方按整文件 200 发。
        覆盖三种合法写法：
          bytes=0-1023   前 1024 字节（浏览器拿 metadata 就是这个）
          bytes=1024-    到结尾（Chrome 播媒体最常用）
          bytes=-500     最后 500 字节
        多段（bytes=0-9,20-29）**刻意不支持**：FFmpeg 产出的 MP4 走多段容易被
        前端媒体栈当成"服务端很聪明"从而发并发多段请求，单机服务得不偿失 ⇒
        直接回整文件，浏览器照样能播。
        """
        if not rng or size <= 0:
            return None
        m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
        if not m:
            return None
        a, b = m.group(1), m.group(2)
        if a == "" and b == "":
            return None
        if a == "":                       # bytes=-500
            n = int(b)
            if n <= 0:
                return None
            return (max(0, size - n), size - 1)
        start = int(a)
        end = int(b) if b != "" else size - 1
        if start > end or start >= size:
            return "UNSATISFIABLE"
        return (start, min(end, size - 1))

    def serve_file(self, path):
        """发一个文件。支持 HTTP Range（206）—— 这条是**能不能边下边播**的分界线。

        以前这里只有一句 `body = open(path,'rb').read()` + 200：
          · 浏览器要 seek / 要拿 metadata，服务端一律回 200 全量
            ⇒ <audio> <video> 只能等整个文件下完才出声，进度条也拖不动；
          · 前端列表里几百个 <audio> 一渲染就是几百个全量请求（实测 467 个），
            用户看到的就是"角色加载转半天"。

        ★ 这里**刻意没有** download 参数。原来签名是 `serve_file(path, download=True)`，
          但那个参数**从来没被实现过**（没有 Content-Disposition），而 `/` 和
          `/static/*` 都走默认 True。后来有人照着参数名补发了 Content-Disposition，
          结果首页被浏览器当附件下载 ⇒ 整页白屏（CP§138）。
          "下载"这个动作是前端 `<a href="..." download>` 完成的，服务端不要插手。
        """
        if not os.path.isfile(path):
            return json_response(self, {"error": "文件不存在: %s" % path}, 404)
        st = os.stat(path)
        size = st.st_size
        ext = os.path.splitext(path)[1].lower()
        ctype = MIME.get(ext, "application/octet-stream")
        etag = '"%x-%x"' % (int(st.st_mtime), size)
        head_only = (self.command == "HEAD")

        # ---- 条件请求：内容没变就回 304，一个字节都不用重传。
        #     （mix_preview_latest.wav 这种名字固定的"最新成品"，内容一变
        #       mtime 就变 ⇒ ETag 跟着变，所以 304 不会挡住新内容。）
        inm = self.headers.get("If-None-Match")
        if inm and inm.strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return

        # ---- Range
        rng = self.headers.get("Range")
        want = self._parse_range(rng, size)
        if want == "UNSATISFIABLE":
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % size)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = want if want else (0, size - 1)
        length = end - start + 1
        partial = (start != 0 or end != size - 1)

        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("ETag", etag)
        self.send_header("Last-Modified", self.date_time_string(int(st.st_mtime)))
        if partial:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        # ★ 这里**不许**发 Content-Disposition。
        #   serve_file 的 download 参数默认 True，而 `/` 和 `/static/*` 两个调用点
        #   都没传 download=False —— 也就是说这个默认值是个**遗留的、从来没被
        #   实现过的开关**（原实现里压根没用到它）。一旦照着它发 attachment，
        #   首页 index.html 就会被浏览器当成附件下载而不是渲染 ⇒ 整页白屏、
        #   location.href 停在 about:blank，看起来像"服务挂了"。
        #   真正的"下载"是前端 `<a href="..." download>` 干的，服务端不需要这个头。
        #   允许缓存，但每次回源问一句（no-cache），命中 ETag 就 304 ——
        #   以前是 no-store ⇒ 切一下页签整屏音频重下一遍。
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if head_only:
            return
        try:
            with open(path, "rb") as f:
                # 大文件：seek 到 start 再一路搬，别 read() 整个文件
                if start:
                    f.seek(start)
                left = length
                while left > 0:
                    buf = f.read(min(self.SERVE_CHUNK, left))
                    if not buf:
                        break
                    self.wfile.write(buf)
                    left -= len(buf)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # 用户点了下一条 / 拖了进度条，上一个请求被掐断 —— 完全正常，别刷屏
            pass


def gpu_name():
    try:
        import subprocess

        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=20)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else "未知"
    except Exception:
        return "未知"


def main():
    url = "http://%s:%d/" % (HOST, PORT)
    print("VoiceStudio 启动: %s" % url)
    print("角色库: %s" % studio.ROLES_JSON)
    # 先把磁盘上的任务读回来再开服务：不然用户刷新一下页面，
    # 上一次训练的历史和"服务重启中断了"这个事实就全没了。
    n = studio.restore_jobs()
    if n:
        print("已恢复任务记录: %d 条" % n)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
