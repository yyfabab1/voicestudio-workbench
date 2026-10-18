r"""第二十一关：接口不许掉连接 —— 服务端炸了必须变成看得见的中文错误。

为什么单开这一关：
  2026-10-13 用户点「训练（Seed-VC 微调）」界面只有一句 NetworkError，
  后端日志里躺着 UnboundLocalError：create_job 的 seedvc 分支引用了只在
  train 分支赋值的 rid_obj。handler 里一个 try/except 都没有 ⇒ 异常把连接
  拦腰掐断 ⇒ 前端连个 HTTP 状态码都拿不到，只能报「网络错误」。
  用户看到的是"网坏了"，真相是"服务端代码写错了"。

  更要命的是：之前的 verify_seedvc 把 fetch 拦成了假回包（为了不占显卡），
  请求**根本没打到服务端** —— 这类"I/O 之前那两行 Python"从没人测过，
  所以它能堂而皇之地绿到第 20 关。本关的原则：加工部晚班的活可以有假回包，
  路由和取参那段必须真打过去。

覆盖：
  A 自家起一个服务实例（端口随机，不碰 8756），把 **每一种 kind** 从 HTTP 打进去，
    断言回的是 JSON、带着正确的 kind —— create_job 里任何一处引用到没绑的变量，
    这一段立刻变红（2026-10-13 的 rid_obj 就是这样）。
  B 兜底：handler 内部抛异常 ⇒ 必须回 JSON 500 且带上原因，不许掐连接。
  C 负对照：①一个**没有兜底**的裸 handler，证明本关真能分辨"连接被掐断"
    （尺子自检）；②非法请求必须是 400 且带原因，不能被当成 500 混过去。

为什么执行体要打桩：本关验的是"路由层能不能把每种请求都收下来"，
不验 GPU —— 真跑一次训练要占显卡十几分钟，那一摊有 sing/seedvc/steps 三关管。
就地 stub studio 里的九个干活函数，被打桩的只有"干活"，new_job/写 job.json 是真的。

用法：<venv>/Scripts/python.exe -s verify_apierr.py
"""
import json
import os
import shutil
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TMP = os.path.join(HERE, "_apierr_tmp")

import server                                            # noqa: E402
import studio                                            # noqa: E402

OK, BAD = [], []
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 期望“被明确拒绝”（400 带原因），而不是创建任务
Refuse = object()

# 会被打桩的"干活函数"：本关只验路由层，不让它们真跑
WORKERS = ["job_ingest", "train_role", "lyrics_job", "job_output_lyric",
           "speak_role", "sing_job", "job_install_model", "seedvc_finetune",
           "train_rvc"]


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""))


# ------------------------------------------------------------ 小工具

def make_wav(path, secs=0.4, sr=16000):
    """写一段真能读的 wav（不是改名文件）—— 路由层 os.path.isfile 就够，
    后面真读它的函数是 stub，写个合规的以防将来有人把桩拆了。"""
    import math
    os.makedirs(os.path.dirname(path), exist_ok=True)
    n = int(sr * secs)
    frames = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 300 * i / sr)))
                      for i in range(n))
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(frames)
    return path


def start(srv):
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return t


def req(port, method, path, body=None, timeout=15):
    """返回 (code, text, err)。code==0 表示**连接层就没回包**（浏览器只会看到
    NetworkError）—— 这正是本关要抓的东西。"""
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = (json.dumps(body, ensure_ascii=False).encode("utf-8")
            if body is not None else None)
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with OPENER.open(r, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), ""
    except urllib.error.HTTPError as e:
        try:
            txt = e.read().decode("utf-8", "replace")
        except Exception:
            txt = ""
        return e.code, txt, ""
    except Exception as e:
        return 0, "", "%s: %s" % (type(e).__name__, e)


class Raw(BaseHTTPRequestHandler):
    """负对照用的裸 handler：没有任何兜底（就是修之前 server.Handler 的样子）。"""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        raise RuntimeError("这里没人兜底")

    def log_message(self, *a):
        pass


# ------------------------------------------------------------ 本体

def main():
    os.makedirs(TMP, exist_ok=True)
    wav = make_wav(os.path.join(TMP, "probe.wav"))

    # ---- 起一个真的服务实例（端口随机），和 8756 上那个互不打扰 ----
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = srv.server_address[1]
    start(srv)
    print("  本机服务实例：127.0.0.1:%d（端口随机，不碰 8756 上那个）" % port)

    raw_srv = ThreadingHTTPServer(("127.0.0.1", 0), Raw)
    raw_port = raw_srv.server_address[1]
    start(raw_srv)

    # ---- 干活函数打桩：只掐掉"占显卡/占网络"的那一段，new_job 还是真的 ----
    saved = {}
    calls = []

    def _stub(name):
        def _f(*a, **k):
            calls.append(name)
            return None
        return _f

    for w in WORKERS:
        if hasattr(studio, w):
            saved[w] = getattr(studio, w)
            setattr(studio, w, _stub(w))

    # ---- 一个自带 rvc 的角色：seedvc 分支必须先过 "(rvc) 存在" 这关，
    #      否则它早在到达容易出错的那一行之前就 400 走了 —— 尺子会假绿。
    role = None
    for r in studio.load_roles().get("roles", []):
        if r.get("rvc"):
            role = r
            break
    created_role = None
    if role is None:
        role = studio.create_role(name="接口巡视·临时角色", kind="speech")
        studio.update_role(role["id"], {"rvc": {"weight": "fake.pth"}})
        created_role = role["id"]
        role = studio.get_role(created_role)
    rid = role["id"]
    check("存在自带 RVC 的角色（seedvc 那条分支才会走到真正出错的那一行）",
          bool(role.get("rvc")), rid)

    made = []
    try:
        # ---- A：每一种 kind 都真打一遍 ----
        cases = [
            ("训练音色 train", {"kind": "train", "role": rid}, "train"),
            ("训练歌声模型 rvc", {"kind": "rvc", "role": rid}, "rvc"),
            ("★ Seed-VC 微调 seedvc（2026-10-13 在这儿炸过）",
             {"kind": "seedvc", "role": rid, "steps": 600}, "seedvc"),
            ("拆素材 ingest", {"kind": "ingest", "video": wav}, "ingest"),
            ("识别歌词 lyrics", {"kind": "lyrics", "song": wav}, "lyrics"),
            ("成品补歌词 output_lyric",
             {"kind": "output_lyric", "role": rid, "index": 0}, "output_lyric"),
            # 这一支以前单独认 "rid"，别的支认 "role" —— 同一个东西两种键名，
            # 谁也记不住。统一之后，旧键名必须被明确拒绝，不许悄悄兼容。
            ("成品补歌词只认 role（旧键 rid 会被拒）",
             {"kind": "output_lyric", "rid": rid, "index": 0}, Refuse),
            ("说话 speak", {"kind": "speak", "role": rid, "texts": ["你好"]}, "speak"),
            ("唱歌 sing", {"kind": "sing", "role": rid, "song": wav}, "sing"),
            ("装模型 install_model",
             {"kind": "install_model", "repo": "someone/repo"}, "install_model"),
        ]
        for name, body, want in cases:
            code, txt, err = req(port, "POST", "/api/jobs", body)
            why = ""
            try:
                why = json.loads(txt).get("error", "") if txt else ""
            except Exception:
                why = ""
            if want is Refuse:
                check("400 带原因：%s" % name, code == 400 and bool(why),
                      "code=%s body=%s" % (code, txt[:160]))
                continue
            j = None
            try:
                j = json.loads(txt).get("job") if txt else None
            except Exception:
                j = None
            ok = (code == 200 and isinstance(j, dict) and j.get("kind") == want)
            check("回 JSON 200 且 kind=%s：%s" % (want, name), ok,
                  "code=%s err=%s body=%s" % (code, err, txt[:180]))
            if isinstance(j, dict) and j.get("id"):
                made.append(j["id"])

        # 任务真的落盘了（不然上面"回 200"可能是空头支票）
        time.sleep(0.6)
        alive = [i for i in made
                 if os.path.isfile(os.path.join(studio.JOBS_DIR, i, "job.json"))]
        check("九种任务都真的落了 job.json（不是空头回包）",
              len(alive) == len(made) >= 9, "%d/%d" % (len(alive), len(made)))
        check("九个干活函数都被唤起过（打桩收到了调用）",
              len(set(calls)) >= 8, sorted(set(calls)))

        # ---- C②：非法请求要 400 带原因，不能被当成 500 混过去 ----
        for name, body in [("不认识的 kind", {"kind": "nope"}),
                           ("seedvc 不给角色", {"kind": "seedvc"}),
                           ("lyrics 给不存在的歌", {"kind": "lyrics", "song": "D:/__no__.mp3"}),
                           ("install_model 不给仓库", {"kind": "install_model"})]:
            code, txt, err = req(port, "POST", "/api/jobs", body)
            why = ""
            try:
                why = json.loads(txt).get("error", "")
            except Exception:
                why = ""
            check("非法请求回 400 且带中文原因：%s" % name,
                  code == 400 and bool(why), "code=%s body=%s" % (code, txt[:120]))

        # ---- B：handler 内部炸掉能不能被兜成 JSON 500 ----
        real_get_role = studio.get_role

        def _boom(*a, **k):
            raise RuntimeError("哨兵：这里应该被兜底接住")
        studio.get_role = _boom
        try:
            code, txt, err = req(port, "GET", "/api/role/" + rid)
        finally:
            studio.get_role = real_get_role
        why = ""
        try:
            why = json.loads(txt).get("error", "")
        except Exception:
            why = ""
        check("handler 里抛异常 ⇒ 回 JSON 500 带原因（不许掐连接）",
              code == 500 and "哨兵" in why,
              "code=%s err=%s body=%s" % (code, err, txt[:180]))

        # ---- C①：尺子自检 —— 没有兜底的样子必须被本关判红 ----
        code, txt, err = req(raw_port, "GET", "/api/role/" + rid)
        check("★ 负对照：没有兜底的 handler ⇒ 本关判得出'连接被掐断'（code=0）",
              code == 0 and bool(err), "code=%s err=%s" % (code, err))
        code2, txt2, err2 = req(port, "GET", "/api/role/" + rid)
        check("对照：同一个请求打到有兜底的服务 ⇒ 正常回 200 不是掉连接",
              code2 == 200 and txt2.startswith("{"), "code=%s" % code2)
    finally:
        srv.shutdown()
        raw_srv.shutdown()
        for w, f in saved.items():
            setattr(studio, w, f)
        for jid in made:
            shutil.rmtree(os.path.join(studio.JOBS_DIR, jid), ignore_errors=True)
        if created_role:
            try:
                studio.delete_role(created_role)
            except Exception:
                pass
        shutil.rmtree(TMP, ignore_errors=True)

    print("-" * 62)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 失败项：" + "、".join(BAD))
    else:
        print("结论：接口层不再掉连接（含枚得到的负对照）")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
