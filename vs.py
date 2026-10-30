# -*- coding: utf-8 -*-
"""VoiceStudio 的命令行入口 —— 给 AI（dsh / 其它 agent）直接调用的那一层。

为什么要有它：
    服务端的 HTTP 接口是人机界面用的（要前端、要点按钮、进度要在页面上看）。
    AI 没有浏览器，它需要的是「一条命令下去，等它结束，拿到成品路径」。
    这一层就是那条命令：参数 → job → 轮询等结束 → 打印成品在哪 → 顺带告诉你该跑哪关自检。

三条硬规矩（改这个文件之前先读）：
  1. 不要在这里写死任何默认值（轮数 / index_rate / 引擎 / 音高）。默认值只能有一处，
     在 studio.py 里。这里只负责「用户/AI 给了就透传，没给就别传」。
  2. 服务没起就**明确报错并告诉他去双击 bat**，绝不在这里偷偷拉起服务 ——
     工具进程起的常驻进程活不过一条命令，拉起来也是秒死，症状是「明明报成功了，
     下一次调用又连不上」，比直接报错难查十倍。
  3. `check` 一次只跑一关。本机有个批量删除钩子按「一条命令累计删除次数」计数
     （阈值 50），多条门禁挤在一条命令里必然撞穿，之后删除全被挡下，
     表现是「判据全绿但退出码非 0」。要跑一组就逐条单独调。

用法：
    python vs.py status
    python vs.py roles
    python vs.py ingest D:\\a.mp4 D:\\b.mp4 --wait
    python vs.py sing ra500fb92 --song D:\\x.mp3 --pitch auto --wait
    python vs.py check seg_purity
    python vs.py check --for ingest          # 列出这项操作该跑的门禁 + 逐条命令
全局开关 --json 让输出结构化，方便 AI 解析。
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BASE = os.environ.get("VS_BASE", "http://127.0.0.1:8756")

# ★ 本机 http_proxy 会劫持 127.0.0.1 的请求（这项目踩过一次：接口明明在跑却连不通），
#   所以这里显式装一个空的 ProxyHandler，不让环境代理插手。
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 任务终态：不在「还在跑」集合里就算结束。不写死具体字符串 —— 服务端加了新状态也不会漏。
RUNNING = {"running", "queued", "pending", "starting"}


# ---------------------------------------------------------------- HTTP


def call(path, body=None, timeout=60):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def alive():
    try:
        call("/api/roles", None, timeout=8)
        return True
    except Exception:
        return False


def need_server():
    """服务没起就死在这儿，并把怎么起服务说清楚。"""
    if alive():
        return
    sys.stderr.write(
        "连不上 VoiceStudio 服务（%s）。\n"
        "它不是常驻的：请双击 VoiceStudio 目录下的「启动VoiceStudio.bat」，\n"
        "等窗口里出现端口后再回来跑这条命令。\n"
        "注意：不要指望 AI 自己把它拉起来 —— 工具进程起的常驻进程活不过一条命令，\n"
        "拉起来也是秒死，症状是「这次报成功、下次又连不上」。\n" % BASE)
    sys.exit(3)


# ---------------------------------------------------------------- 任务等待


def wait_job(jid, timeout=7200.0, quiet=False):
    """轮询到终态，返回 (job, 耗时秒)。打印进度是为了让人看着它没死。"""
    t0 = time.time()
    last = ""
    while True:
        try:
            j = call("/api/job/" + jid, None, timeout=30).get("job") or {}
        except Exception as e:
            if time.time() - t0 > timeout:
                raise SystemExit("等 %s 超时（%.0fs），最后一次查询失败：%s" % (jid, timeout, e))
            time.sleep(3)
            continue
        st = j.get("state")
        line = "%s %s%% %s" % (st, j.get("pct"), j.get("stage") or "")
        if line != last:
            if not quiet:
                print("  [%5.0fs] %s" % (time.time() - t0, line))
            last = line
        if st not in RUNNING:
            return j, time.time() - t0
        if time.time() - t0 > timeout:
            raise SystemExit("等 %s 超时（%.0fs），停在了 %s" % (jid, timeout, st))
        time.sleep(3)


def finish(jid, j, secs, as_json):
    """任务结束后的统一收尾：说清楚结果、成品在哪、失败原因、下一步自检。"""
    st = j.get("state")
    ok = (st == "done")
    out = j.get("outputs") or j.get("out") or []
    payload = {
        "job": jid, "state": st, "seconds": round(secs, 1),
        "ok": ok, "error": j.get("error"), "outputs": out,
        "next_check": GATES.get(j.get("kind"), []),
    }
    if as_json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("任务 %s：%s（%.0fs）" % (jid, "完成" if ok else "结束于 " + str(st), secs))
        if not ok and j.get("error"):
            print("  失败原因：%s" % j["error"])
        for o in (out if isinstance(out, list) else []):
            print("  成品：%s" % (o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)))
        if ok and payload["next_check"]:
            print("  该跑的自检：%s" % "、".join(payload["next_check"]))
    sys.exit(0 if ok else 4)


# ---------------------------------------------------------------- 自检适配表
#
# 「做了什么 → 该跑哪几关」。这是给 AI 的：做完一步别急着交差，跑对应门禁。
# 只列真相关的，不搞「全都跑一遍」—— 全量 34 关要跑好几个小时，而且
# 挤在一条命令里会撞删除钩子（见文件头第 3 条）。
# ★ 写**全名**（verify_xxx），跟 _run_regression.sh 的关卡名逐字一致。
#   别在这里用裸名再拼前缀 —— 两处命名一漂移，AI 就会跑去跑一关不存在的门禁。
GATES = {
    "ingest":  ["verify_seg_purity", "verify_speaker_match", "verify_ingest_batch",
                "verify_steps", "verify_sep", "verify_slice_hifi", "verify_nonvocal"],
    "train":   ["verify_train_rounds", "verify_train_quality", "verify_rvc_train"],
    "sing":    ["verify_sing", "verify_sing_pitch", "verify_tone"],
    "speak":   ["verify_speech_long", "verify_tone"],
    "seedvc":  ["verify_seedvc"],
    "viz":     ["verify_viz"],
    "new_song": ["verify_yue2", "verify_yue2_ui"],
    "market":  ["verify_modelhub", "verify_market_ui"],
    "ui":      ["verify_frontend_static", "verify_nav", "verify_apierr", "verify_jobctl",
                "verify_lyric_ui", "verify_train_rounds_ui", "verify_yue2_ui"],
    "storage": ["verify_storage", "verify_disk"],
}

def all_gates():
    """从 _run_regression.sh 读权威清单，不在这里抄一份（抄了就会漂）。
    清单里每行是 `verify_xxx:PY_xxx`，统一剥成裸名，跟 GATES 里的写法对齐。"""
    p = os.path.join(HERE, "_run_regression.sh")
    if not os.path.isfile(p):
        return sorted({g for v in GATES.values() for g in v})
    import re
    m = re.search(r'ALL="(.*?)"', open(p, encoding="utf-8").read(), re.S)
    if not m:
        return sorted({g for v in GATES.values() for g in v})
    out = []
    for l in m.group(1).strip().splitlines():
        name = l.split(":")[0].strip()
        if name.endswith(".py"):
            name = name[:-3]
        if name:
            out.append(name)
    return out


def find_bash():
    """找 Git Bash。★ 裸 `bash` 排在最后：Windows 的 C:\\Windows\\System32\\bash.exe
    是 wsl 启动器（沙箱会拦，而且它跑不了 .sh），先试它会拿到「脚本压根没跑、退出码 1」
    这种最难查的症状。每个候选都要校验输出里真有 GNU bash，光看退出码不够。"""
    cands = [r"C:\Program Files\Git\bin\bash.exe",
             r"C:\Program Files\Git\usr\bin\bash.exe",
             r"C:\Program Files (x86)\Git\bin\bash.exe",
             "bash"]
    for b in cands:
        try:
            r = subprocess.run([b, "--version"], capture_output=True, timeout=20)
            if b"GNU bash" in (r.stdout or b""):
                return b
        except Exception:
            continue
    return None


def _decode(b):
    for enc in ("utf-8", "gbk", "mbcs"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", "replace")


# ---------------------------------------------------------------- 子命令


def cmd_status(a):
    if not alive():
        need_server()
    roles = call("/api/roles").get("roles") or []
    env = {}
    try:
        env = call("/api/env")
    except Exception as e:
        env = {"error": str(e)}
    payload = {"server": "up", "base": BASE, "roles": len(roles), "env": env}
    if a.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print("服务在跑：%s（角色 %d 个）" % (BASE, len(roles)))
        for r in roles:
            print("  %s  %-16s 段=%-4d 模型=%s"
                  % (r.get("id"), r.get("name"), len(r.get("segments") or []),
                     bool(r.get("model") or r.get("rvc"))))
    return 0


def cmd_roles(a):
    need_server()
    roles = call("/api/roles").get("roles") or []
    if a.json:
        if a.full:
            print(json.dumps(roles, ensure_ascii=False))
        else:
            # 默认精简：完整记录带每段的文字和时长，实测 357 段能到 186KB ——
            # AI 多数时候只想知道「有谁、各多少段」，不该被这堆明细淹掉。
            light = [{"id": r.get("id"), "name": r.get("name"),
                      "segments": len(r.get("segments") or []),
                      "outputs": len(r.get("outputs") or []),
                      "pending": bool(r.get("pending")),
                      "has_model": bool(r.get("model") or r.get("rvc"))}
                     for r in roles]
            print(json.dumps(light, ensure_ascii=False))
        return 0
    if not roles:
        print("角色库是空的 —— 先导入一段视频才有得做。")
        return 0
    for r in roles:
        print("%s\t%s\t段=%d\t成品=%d\t待归类=%s"
              % (r.get("id"), r.get("name"), len(r.get("segments") or []),
                 len(r.get("outputs") or []), bool(r.get("pending"))))
    return 0


def cmd_jobs(a):
    need_server()
    jobs = call("/api/jobs").get("jobs") or []
    jobs = jobs[:a.limit]
    if a.json:
        print(json.dumps(jobs, ensure_ascii=False))
        return 0
    for j in jobs:
        print("%s\t%-10s\t%-9s\t%s" % (j.get("id"), j.get("kind"), j.get("state"),
                                       (j.get("title") or "")[:40]))
    return 0


def cmd_wait(a):
    need_server()
    j, s = wait_job(a.job, timeout=a.timeout)
    finish(a.job, j, s, a.json)


def _run(a, body, wait, label):
    need_server()
    r = call("/api/jobs", body, timeout=120)
    if r.get("error"):
        sys.stderr.write("服务端拒了：%s\n" % r["error"])
        sys.exit(2)
    j = r.get("job") or {}
    jid = j.get("id")
    if a.json and not wait:
        print(json.dumps({"job": jid, "state": j.get("state"), "kind": body.get("kind")},
                         ensure_ascii=False))
        return 0
    print("%s 已开始：%s（%s）" % (label, jid, j.get("title") or ""))
    if not wait:
        print("要等它跑完就加 --wait，或者稍后：python vs.py wait %s" % jid)
        return 0
    j2, s = wait_job(jid, timeout=a.timeout)
    finish(jid, j2, s, a.json)


def cmd_ingest(a):
    # ★ 一律转成绝对路径再发给服务端：服务端进程的 cwd 不保证等于调用方，
    #   传相对路径会让它「文件检查过了、真跑 ffmpeg 时却找不到」——
    #   症状是「抽音频失败，视频可能没有音轨或格式不认识」，极像素材坏了。
    vids = [os.path.abspath(v) for v in a.videos if v and os.path.isfile(v)]
    if not vids:
        sys.stderr.write("这些文件一个都不存在：%s\n" % ", ".join(a.videos))
        sys.exit(2)
    body = {"kind": "ingest", "videos": vids, "mode": a.mode,
            "lang": a.lang, "uvr": not a.no_uvr, "do_train": not a.no_train}
    if a.append_to:
        body["append_to"] = a.append_to
    if a.n_speakers:
        body["n_speakers"] = a.n_speakers
    if a.epochs:
        body["epochs"] = a.epochs
    return _run(a, body, a.wait, "导入")


def cmd_train(a):
    body = {"kind": "train", "role": a.rid}
    if a.epochs:
        body["epochs"] = a.epochs
    if a.quality:
        body["quality"] = a.quality
    return _run(a, body, a.wait, "训练")


def cmd_sing(a):
    if not os.path.isfile(a.song):
        sys.stderr.write("歌曲文件不存在：%s\n" % a.song)
        sys.exit(2)
    # 同上：路径一律转绝对，别把 cwd 的差异带进服务端。
    body = {"kind": "sing", "role": a.rid, "song": os.path.abspath(a.song),
            "auto_train": not a.no_auto_train}
    if a.pitch is not None:
        body["pitch"] = a.pitch
    if a.engine:
        body["engine"] = a.engine
    if a.preview_secs:
        body["preview_secs"] = a.preview_secs
    if a.orig_lyric and a.new_lyric:
        body["orig_lyric"] = a.orig_lyric
        body["new_lyric"] = a.new_lyric
    return _run(a, body, a.wait, "翻唱")


def cmd_speak(a):
    script = a.script or (sys.stdin.read() if not sys.stdin.isatty() else "")
    if not script.strip():
        sys.stderr.write("没有要说的话（用 --script 或从 stdin 喂）\n")
        sys.exit(2)
    body = {"kind": "speak", "role": a.rid, "script": script}
    return _run(a, body, a.wait, "合成台词")


def cmd_newsong(a):
    body = {"kind": "new_song", "role": a.rid, "style": a.style, "lyrics": a.lyrics}
    if a.duration:
        body["duration"] = a.duration
    return _run(a, body, a.wait, "写新歌")


def cmd_viz(a):
    body = {"kind": "viz"}
    if a.audio:
        if not os.path.isfile(a.audio):
            sys.stderr.write("音频文件不存在：%s\n" % a.audio)
            sys.exit(2)
        body["audio"] = os.path.abspath(a.audio)
    else:
        if not a.rid:
            sys.stderr.write("要么给 --audio，要么给 --rid + --index\n")
            sys.exit(2)
        body["role"] = a.rid
        body["index"] = a.index
    if a.title:
        body["title"] = a.title
    if a.style:
        body["style"] = a.style
    return _run(a, body, a.wait, "做视频")


def cmd_check(a):
    if a.list:
        for g in all_gates():
            print(g)
        return 0
    if a.for_kind:
        gates = GATES.get(a.for_kind)
        if not gates:
            sys.stderr.write("没给 %s 配门禁。已知：%s\n" % (a.for_kind, "、".join(sorted(GATES))))
            sys.exit(2)
        print("做了「%s」之后该跑的自检（**逐条单独跑**，别一次全塞进一条命令）：" % a.for_kind)
        for g in gates:
            print("  python vs.py check %s" % g)
        return 0
    if not a.gate:
        sys.stderr.write("要跑哪一关？用 --list 看全部，或 --for <操作> 看推荐的\n")
        sys.exit(2)
    known = set(all_gates())
    if a.gate not in known:
        sys.stderr.write("没有 %s 这一关。已知的：%s\n" % (a.gate, "、".join(sorted(known))))
        sys.exit(2)
    bash = find_bash()
    if not bash:
        sys.stderr.write("找不到 Git Bash，跑不了 _run_regression.sh\n")
        sys.exit(3)
    # 一次只跑一关：删除钩子按「一条命令累计删除次数」计数，多关同批必撞线。
    r = subprocess.run([bash, "_run_regression.sh", a.gate], cwd=HERE,
                       capture_output=True)
    out = _decode(r.stdout or b"")
    err = _decode(r.stderr or b"")
    if out.strip():
        print(out.rstrip())
    if err.strip():
        sys.stderr.write(err.rstrip() + "\n")
    if r.returncode != 0:
        sys.stderr.write("门禁 %s 没过（退出码 %d）。别改判据口径去凑绿，去查产品。\n"
                         % (a.gate, r.returncode))
    return r.returncode


# ---------------------------------------------------------------- 参数


def main():
    ap = argparse.ArgumentParser(
        prog="vs.py", description="VoiceStudio 的命令行入口（给 AI 调用）")
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="服务在不在、有哪些角色").set_defaults(fn=cmd_status)
    p = sub.add_parser("roles", help="列角色")
    p.add_argument("--full", action="store_true",
                   help="--json 时给出完整记录（含每段文字/时长），默认只给摘要")
    p.set_defaults(fn=cmd_roles)

    p = sub.add_parser("jobs", help="最近的任务")
    p.add_argument("--limit", type=int, default=15)
    p.set_defaults(fn=cmd_jobs)

    p = sub.add_parser("wait", help="等一个任务结束")
    p.add_argument("job")
    p.add_argument("--timeout", type=float, default=7200)
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("ingest", help="导入视频/音频 → 分人 → 切片 → 建角色")
    p.add_argument("videos", nargs="+")
    p.add_argument("--mode", default="auto",
                   choices=["auto", "merge", "per_video", "match"])
    p.add_argument("--append-to", help="追加到已有角色（给角色 id）")
    p.add_argument("--n-speakers", type=int)
    p.add_argument("--lang", default="zh")
    p.add_argument("--epochs", type=int)
    p.add_argument("--no-uvr", action="store_true", help="不做人声分离")
    p.add_argument("--no-train", action="store_true", help="导完不自动训练")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=7200)
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("train", help="训练某个角色的音色")
    p.add_argument("rid")
    p.add_argument("--epochs", type=int)
    p.add_argument("--quality", choices=["fast", "standard", "high"])
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=7200)
    p.set_defaults(fn=cmd_train)

    p = sub.add_parser("sing", help="让角色翻唱一首歌")
    p.add_argument("rid")
    p.add_argument("--song", required=True)
    p.add_argument("--pitch")
    p.add_argument("--engine", choices=["rvc", "seedvc", "svc"])
    p.add_argument("--preview-secs", type=int, help="只做一小段试听")
    p.add_argument("--orig-lyric")
    p.add_argument("--new-lyric")
    p.add_argument("--no-auto-train", action="store_true")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=7200)
    p.set_defaults(fn=cmd_sing)

    p = sub.add_parser("speak", help="让角色说台词")
    p.add_argument("rid")
    p.add_argument("--script")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=7200)
    p.set_defaults(fn=cmd_speak)

    p = sub.add_parser("newsong", help="角色唱一首凭空写的新歌（YuE2）")
    p.add_argument("rid")
    p.add_argument("--style", required=True)
    p.add_argument("--lyrics", required=True)
    p.add_argument("--duration", type=float)
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=7200)
    p.set_defaults(fn=cmd_newsong)

    p = sub.add_parser("viz", help="把音频/成品渲染成视频")
    p.add_argument("--audio")
    p.add_argument("--rid")
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--title")
    p.add_argument("--style")
    p.add_argument("--wait", action="store_true")
    p.add_argument("--timeout", type=float, default=7200)
    p.set_defaults(fn=cmd_viz)

    p = sub.add_parser("check", help="跑自检门禁（一次只跑一关）")
    p.add_argument("gate", nargs="?")
    p.add_argument("--for", dest="for_kind", help="列出某类操作该跑的门禁")
    p.add_argument("--list", action="store_true")
    p.set_defaults(fn=cmd_check)

    # 全局开关允许写在子命令后面（`vs.py sing … --json` 和 `vs.py --json sing …` 都认）。
    # 只定义在主 parser 上的话，写在后面会被当成未知参数 —— 这是最常见的误用。
    argv = sys.argv[1:]
    if "--json" in argv:
        argv = ["--json"] + [x for x in argv if x != "--json"]

    a = ap.parse_args(argv)
    if not getattr(a, "fn", None):
        ap.print_help()
        return 2
    return a.fn(a) or 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        sys.exit(130)
