# -*- coding: utf-8 -*-
r"""门禁：多角色**界面**（真浏览器 + 独立端口服务）。

和 verify_multising 的分工：
  · verify_multising —— 分配表/拼接的数学 + 源码接线（不碰浏览器）；
  · 本关           —— 真页面上的显隐、置灰、逐句下拉、成品卡（不碰 GPU 推理）。

为什么必须是真浏览器：这一轮的三个毛病静态检查全绿 ——
  ① 多人模式下「唱歌引擎」选了也**不生效**（静默失效）；
  ② 「手动指定」没有逐句列表（后端有 manual，界面根本传不上去）；
  ③ 成品卡的逐人体检 + 换法重拼是**动态插入**的。
只有真 DOM + 真鼠标能看见它们。

★ 它**自己**在 8899 起一个服务，不碰用户正在用的 8756：
  进程活不过一条命令（memory 铁律 20），所以"拉起来 + 跑 + 收掉"写在同一段里。
★ 素材（16 秒片段 + 它的歌词缓存）只准备一次：歌词缓存的键含 mtime，
  每次重切就等于每次重认歌词（一分钟），没必要。
"""
import json
import os
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

PORT = int(os.environ.get("VS_UI_PORT", "8899"))
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
CLIP = os.path.join(HERE, "_tmp", "e2e_multi_clip.wav")
N_PASS, N_FAIL = 0, 0


def listen(port, host="127.0.0.1", timeout=0.4):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % PORT,
                                    timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return {}


def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path),
                                 data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def start_service():
    """在独立端口起一份服务。返回 (proc, 说明)。"""
    import studio
    if not os.path.isfile(studio.PY):
        return None, "找不到 GSV 运行时：%s" % studio.PY
    if listen(PORT):
        return None, "端口 %d 已被别的进程占用（换 VS_UI_PORT）" % PORT
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


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("  [ok] %s" % name, flush=True)
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  ← " + str(detail)) if detail else ""),
              flush=True)


def ensure_lyrics():
    """片段有歌词缓存吗？没有就跑一次真的识别（走 /api/jobs，和用户点按钮一样）。"""
    import studio
    segs, _dur = studio.lyric_segments_any(CLIP)
    if segs:
        print("  歌词缓存已有（%d 句），不用重认" % len(segs), flush=True)
        return True, ""
    print("  片段还没认过歌词 → 跑一次识别（约 1 分钟，之后会复用）", flush=True)
    try:
        j = post("/api/jobs", {"kind": "lyrics", "song": CLIP})["job"]
    except Exception as e:
        return False, "建识别任务失败：%s" % e
    t0 = time.time()
    while time.time() - t0 < 600:
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/job/%s"
                                        % (PORT, j["id"]), timeout=10) as r:
                jj = json.loads(r.read().decode("utf-8"))["job"]
        except Exception:
            time.sleep(2)
            continue
        if jj.get("state") in ("done", "error", "cancelled"):
            return jj.get("state") == "done", (jj.get("error") or "")[:200]
        time.sleep(2)
    return False, "识别超时"


def main():
    print("多角色界面门禁：独立端口 %d + 真 Chrome" % PORT, flush=True)
    if not os.path.isfile(CHROME):
        check("找到 Chrome", False, CHROME)
        return 1
    import probe_multi_ui as P
    check("测试素材（16 秒片段）就绪", P.prep() == 0, CLIP)

    proc, info = start_service()
    if not proc:
        check("在 %d 起一份独立服务" % PORT, False, info)
        return 1
    try:
        h = health()
        check("服务活着", h.get("ok") is True, h)
        # ★ 旧进程守卫：跑的不是当前代码，后面的红全是假红
        fresh = float(h.get("code_mtime") or 0) <= float(h.get("started_ts") or 0) + 2
        check("服务跑的是当前代码（code_mtime <= started_ts）", fresh,
              "code=%s started=%s" % (h.get("code_mtime"), h.get("started_ts")))
        ok, why = ensure_lyrics()
        check("片段有歌词时间戳（手动分句要用）", ok, why)

        rc = P.main()
        check("真浏览器探针全过（%s）" % "0 失败", rc == 0,
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
