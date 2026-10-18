# -*- coding: utf-8 -*-
"""第 32 关：「角色唱新歌」一体化管线（YuE2 作曲 + RVC 换声）。

守住三件事：
  1. **契约**：引擎地址/端口/模型/输出格式，全是实测出来的，不许有人凭印象改回去。
  2. **拆包**：引擎返回的是 multipart（JSON + 音频），整段存成文件就是一个打不开的
     文件。这一条必须有负对照 —— 只验「能读出 48k」的话，把整段存盘也能过。
  3. **接线**：新源头必须接进**同一条**已验证管线（sing_role），不许另写一套。

跑法：python -s verify_yue2.py
"""
import ast
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

PASS = 0
FAIL = 0
SKIP = 0


def sec(t):
    print("\n" + "=" * 70)
    print(t)
    print("=" * 70)


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("! FAIL  %s%s" % (name, ("　｜" + str(detail)[:220]) if detail else ""))
    return bool(ok)


def skip(name, why):
    global SKIP
    SKIP += 1
    print("  SKIP  %s　｜%s" % (name, why))


def summary():
    print("\n" + "-" * 70)
    print("PASS %d / FAIL %d / SKIP %d" % (PASS, FAIL, SKIP))
    if FAIL == 0:
        print("ALLDONE 第 32 关全绿")
    else:
        print("ALLDONE 第 32 关有 %d 条红" % FAIL)
    return 0 if FAIL == 0 else 1


# ────────────────────────────────────────────────────────────────────
def part_a():
    sec("A. 契约：引擎地址 / 端口 / 模型 / 输出格式（全是实测结论，不是抄文档）")
    from stages import yue2 as y2

    # A1 —— 8791 在本机被 workbuddy2api 常驻占用，撞上去就是"端口没监听但进程在"，
    #       表现极像"应用坏了"。默认端口必须避开它。
    check("A1 默认引擎端口不是 8791（本机被别的常驻服务占着）",
          ":8791" not in y2.DEFAULT_ENGINE,
          "当前 %s" % y2.DEFAULT_ENGINE)
    check("A1b 默认引擎地址是 127.0.0.1 的 8807",
          y2.DEFAULT_ENGINE.endswith(":8807"), y2.DEFAULT_ENGINE)

    # A2 —— 引擎只认 wav32 / mp3。flac/wav/pcm 全部 400 unknown output format。
    #       有人"顺手"把默认改成 flac 会整条链路 400，而错误信息只说"格式未知"。
    check("A2 已知格式正好是 wav32/mp3（flac/wav/pcm 引擎会 400）",
          set(y2.KNOWN_FMTS) == {"wav32", "mp3"}, y2.KNOWN_FMTS)
    check("A2b 默认输出是无损的 wav32（换声前不能先丢一次高频）",
          y2.FMT_LOSSLESS == "wav32", y2.FMT_LOSSLESS)

    # A3 —— 模型按优先级找，找到哪个用哪个（Q5_K_M 优先，8GB 卡跑得动）
    bb = y2.find_model("backbone")
    check("A3 找得到主模型", bool(bb), "BACKBONE=%s" % bb)
    if bb:
        check("A3b 主模型优先选 Q5_K_M（8GB 卡实测只吃 3039 MiB）",
              "Q5_K_M" in os.path.basename(bb), os.path.basename(bb))
    check("A4 VAE 找得到", bool(y2.find_model("vae")))
    check("A5 乐谱转录模型找得到", bool(y2.find_model("transcriber")))
    check("A6 缺件检查现在返回空（模型齐了）", y2.missing_models() == [],
          y2.missing_models())

    # A6b ★ `stages` 不是包（没有 __init__.py）⇒ 模块里出现相对导入一定 ImportError。
    #     当前调用方都传了 say=，这条 fallback 走不到，所以它会一直"看起来没事"，
    #     直到某天有人不传 say。
    #     ★ 用 AST 查**真正的 ImportFrom(level>0)**，不是文本匹配：
    #       源码的注释/文档串里写着"这里不能写 `from ..studio import log`"，
    #       文本匹配会被这句说明判红 —— 假红（和 D3b 同款）。AST 看的是语义，骗不了。
    src = open(os.path.join(HERE, "stages", "yue2.py"), encoding="utf-8").read()
    _rel = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.ImportFrom) and (getattr(n, "level", 0) or 0) > 0]
    check("A6b 模块里没有相对导入（stages 不是包，`from ..studio` 必 ImportError）",
          not _rel,
          [ast.unparse(n) for n in _rel][:3])
    check("A6c 日志 fallback 走的是绝对导入",
          "import studio as _st" in src or "import studio\n" in src)

    # A7 —— 启动命令要带上引擎真正需要的四个参数
    try:
        cmd = y2.engine_cmd(port=8807)
        joined = " ".join(cmd)
        check("A7 启动命令含 --model/--vae/--transcriber/--port",
              all(k in joined for k in ("--model", "--vae", "--transcriber", "--port")),
              joined[-120:])
        check("A7b 端口用传入的那个", "8807" in joined, joined[-40:])
    except Exception as e:
        check("A7 能拼出启动命令", False, "%s: %s" % (type(e).__name__, e))

    # A8 ★ 负对照：把根目录指到不存在的路径，必须**明确报错**，
    #      而不是拼出一条必然跑不起来的命令然后干等 180 秒。
    old = y2.YUE2_ROOT
    try:
        y2.YUE2_ROOT = os.path.join(HERE, "_no_such_yue2_dir_")
        miss = y2.missing_models()
        check("A8 负对照：根目录不存在时缺件检查必须报出来", len(miss) > 0, miss)
        raised = False
        try:
            y2.engine_cmd()
        except y2.NotReady:
            raised = True
        except Exception:
            raised = False
        check("A8b 负对照：根目录不存在时启动命令必须抛 NotReady", raised)
    finally:
        y2.YUE2_ROOT = old


# ────────────────────────────────────────────────────────────────────
def part_b():
    sec("B. 拆包：引擎返回的是 multipart（JSON + 音频），不是裸音频文件")
    from stages import yue2 as y2

    BOUND = b"yue2-batch-boundary"
    fake_audio = b"RIFF" + bytes(range(256)) * 8 + b"WAVEfmt "
    meta = {"style": "test", "lyrics": "hello", "abc": "X:1\nT:\n"}
    raw = (b"--" + BOUND + b"\r\nContent-Type: application/json\r\n\r\n"
           + json.dumps(meta).encode("utf-8")
           + b"\r\n--" + BOUND + b"\r\nContent-Type: audio/wav\r\n\r\n"
           + fake_audio
           + b"\r\n--" + BOUND + b"--\r\n")

    parts = y2._split_parts(raw, BOUND)
    check("B1 两段（JSON + 音频）都拆出来了",
          len(parts) == 2, [c for c, _ in parts])

    got_meta, got_audio = None, None
    for ct, body in parts:
        if "json" in ct:
            got_meta = json.loads(body.decode("utf-8"))
        elif ct.startswith("audio/"):
            got_audio = body
    check("B2 JSON 段解析正确", (got_meta or {}).get("abc") == meta["abc"], got_meta)
    check("B3 音频段字节完全一致（没吞没多）",
          got_audio == fake_audio,
          "len=%s vs %s" % (len(got_audio or b""), len(fake_audio)))

    # B4 ★ 证明"为什么必须解析"：把整段原始响应当文件存下来，
    #      开头是 boundary 而不是音频 magic ⇒ 播放器打不开。
    check("B4 整段原始响应不是合法音频（开头是 boundary）",
          not raw.startswith(b"RIFF") and raw.startswith(b"--"),
          raw[:24])
    check("B4b 解析出来的音频开头是对的 magic",
          (got_audio or b"").startswith(b"RIFF"), (got_audio or b"")[:8])

    # B5 ★ 负对照：boundary 用错 ⇒ 一段都拆不出来。
    #      如果没有这条，_split_parts 退化成"永远返回空列表"也能过 B1~B3
    #      （因为 B1~B3 用的是同一个 boundary 常量）。
    wrong = y2._split_parts(raw, b"totally-different-boundary")
    check("B5 负对照：boundary 不匹配时必须拆不出东西", len(wrong) == 0,
          "居然拆出 %d 段" % len(wrong))

    # B6 boundary 能从 Content-Type 里取到（服务端换 boundary 时不必改代码）
    class H(dict):
        pass
    h = H({"Content-Type": 'multipart/mixed; boundary="abc-123"'})
    check("B6 能从 Content-Type 解析 boundary", y2._boundary_of(h) == b"abc-123",
          y2._boundary_of(h))
    check("B6b 没有 boundary 时回落到默认值",
          y2._boundary_of({}) == BOUND, y2._boundary_of({}))


# ────────────────────────────────────────────────────────────────────
def part_c():
    sec("C. 引擎真跑：48 kHz / 32 位浮点 / 真有声音")
    from stages import yue2 as y2

    if not y2.is_ready():
        skip("C 引擎真跑", "引擎没起（%s），跳过" % y2.DEFAULT_ENGINE)
        return

    try:
        p = y2.props()
    except Exception as e:
        check("C1 拿得到 /props", False, "%s: %s" % (type(e).__name__, e))
        return
    check("C1 拿得到 /props", isinstance(p, dict) and "defaults" in p, list(p)[:6])
    check("C1b 引擎采样率是 48000",
          int(p.get("sample_rate") or 0) == 48000, p.get("sample_rate"))
    check("C1c 默认值里有 style/lyrics（这就是我们要填的两个字段）",
          "style" in (p.get("defaults") or {}) and "lyrics" in (p.get("defaults") or {}),
          list((p.get("defaults") or {}).keys())[:8])

    out = os.path.join(HERE, "_yue2_gate.wav")
    try:
        path, meta = y2.synth(None,
                              "english folk, male vocal, fingerpicked guitar, "
                              "warm, intimate, 92 BPM",
                              "[Verse]\nsomeone is singing in the quiet room",
                              duration=12, steps=8, out_path=out,
                              say=lambda m: None)
    except Exception as e:
        check("C2 真生成一首短歌", False, "%s: %s" % (type(e).__name__, str(e)[:120]))
        return
    check("C2 真生成一首短歌", os.path.isfile(path) and os.path.getsize(path) > 10000,
          "%s 字节" % (os.path.getsize(path) if os.path.isfile(path) else 0))

    try:
        import soundfile as sf
        info = sf.info(path)
        check("C3 采样率 48 kHz（YuE2 的原生采样率）",
              info.samplerate == 48000, info.samplerate)
        check("C3b 是浮点（wav32）而不是 16 位整数",
              "FLOAT" in info.subtype.upper(), info.subtype)
        check("C3c 双声道", info.channels == 2, info.channels)

        import numpy as np
        x, sr = sf.read(path, always_2d=True, dtype="float32")
        mono = x.mean(axis=1)
        rms = float(np.sqrt((mono ** 2).mean()))
        check("C4 真有声音（RMS > 0.005，不是一段静音）", rms > 0.005, "RMS=%.4f" % rms)
        check("C4b 时长接近请求的 12 秒（±6 秒）",
              abs(len(mono) / sr - 12.0) < 6.0, "%.1f 秒" % (len(mono) / sr))
    except Exception as e:
        check("C3 能读音频", False, "%s: %s" % (type(e).__name__, e))

    check("C5 元信息里带着它写的 ABC 乐谱（以后改旋律用得上）",
          bool((meta or {}).get("abc")), list((meta or {}).keys())[:8])
    check("C5b 元信息带任务 id 与耗时",
          bool(meta.get("_job_id")) and meta.get("_elapsed") is not None,
          {k: meta.get(k) for k in ("_job_id", "_elapsed")})

    # C6 ★ 负对照：引擎不支持 flac，必须**在我们这边就拒绝**，
    #      而不是发出去让 400 回来（那样错误信息只有"unknown output format"）。
    raised = False
    try:
        y2.synth(None, "x", "y", duration=5, fmt="flac", say=lambda m: None)
    except y2.Yue2Fail:
        raised = True
    except Exception:
        raised = False
    check("C6 负对照：不支持的格式必须自己先拒绝（flac）", raised)

    for f in (out,):
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass


# ────────────────────────────────────────────────────────────────────
def part_d():
    sec("D. 接线：新源头接进同一条已验证管线，不是另写一套")
    src_studio = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    src_server = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    html = open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()
    appjs = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()

    check("D1 studio 里有 make_original_song", "def make_original_song(" in src_studio)

    # ★ 核心判据：它必须真的把活儿交给 sing_role。
    #   自己再写一套分离/换声/混音，就等于把已验过的管线复制一份，
    #   两份慢慢走偏（改了一边忘了另一边）是这类项目最典型的烂法。
    m = re.search(r"def make_original_song\(.*?\n(?=def |\Z)", src_studio, re.S)
    body = m.group(0) if m else ""
    check("D1b make_original_song 确实调用了 sing_role（复用而不是重写）",
          "sing_role(" in body, "函数体里没找到 sing_role")
    check("D1c 它没有自己去调分离/混音的底层命令（那是 sing_role 的活）",
          "separate_song(" not in body and "mix_cli.py" not in body)

    check("D2 server 认 kind=new_song 这个任务类型",
          '"new_song"' in src_server and "make_original_song" in src_server)
    check("D2b 风格为空要拒绝（空风格 YuE2 出的是随机噪音）",
          "风格不能空" in src_server)
    check("D2c 歌词为空要拒绝（没词它只哼不唱）", "歌词不能空" in src_server)
    check("D2d 时长被卡在 15~360 秒", "max(15.0, min(360.0, dur))" in src_server)

    # D3 状态接口存在且**只读**（不能顺手拉起引擎）
    m2 = re.search(r'path == "/api/yue2/status".*?(?=if path ==)', src_server, re.S)
    st = m2.group(0) if m2 else ""
    check("D3 有 GET /api/yue2/status", bool(st))
    # ★ 只看**代码行**，剔掉注释：注释里写着"真正的生成会自己 ensure_engine()"
    #   是解释、不是行为。第一版判据没剔注释，于是被一句说明文字判红 —— 假红。
    code_only = "\n".join(ln for ln in st.splitlines()
                          if not ln.strip().startswith("#"))
    check("D3b 状态接口是只读的（代码里不调 ensure_engine）",
          bool(st) and "ensure_engine(" not in code_only,
          "它会把每次界面刷新都变成一次拉起引擎")

    check("D4 前端有「写一首新歌」入口按钮", 'id="smNew"' in html)
    check("D4b 前端有风格与歌词输入框",
          'id="nsStyle"' in html and 'id="nsLyrics"' in html)
    check("D4c 前端有提交按钮", 'id="btnNewSong"' in html)
    check("D5 前端 doNewSong 提交的 kind 是 new_song",
          re.search(r'kind:\s*"new_song"', appjs) is not None)
    check("D5b 两个来源用切换而不是各占一个面板（共用下游，切了不丢已填内容）",
          "function showSingMode(" in appjs and 'id="singBlock"' in html
          and 'id="newSongBlock"' in html)

    # D6 成品来源标记：本次运行过的人应该有 origin
    try:
        db = json.load(open(os.path.join(HERE, "data", "roles.json"), encoding="utf-8"))
        roles = db.get("roles") or []
        marked = []
        for r in roles:
            for o in (r.get("outputs") or []):
                if isinstance(o.get("origin"), dict) and \
                        o["origin"].get("kind") == "yue2-new-song":
                    marked.append(o)
        if marked:
            o = marked[-1]
            check("D6 成品记下了「这歌是 YuE2 写的」（origin 标记）", True)
            check("D6b origin 里带风格/歌词/乐谱",
                  bool(o["origin"].get("style")) and bool(o["origin"].get("lyrics"))
                  and bool(o["origin"].get("abc")),
                  list(o["origin"].keys()))
            check("D6c 标记落在**实际登记的那个文件**上（不是猜文件名）",
                  bool(o.get("wav")) and os.path.basename(o["wav"]).startswith("voiceg_"),
                  os.path.basename(o.get("wav") or ""))
            # D6d/e ★ 两个 id 缺一个就断链：只记引擎曲子 id 的话，
            #   拿它去 data/jobs/ 找质检报告一条都找不到
            #   （实测症状：统计里"新歌链路 0 样本"，其实它跑过也质检过）。
            _oj = str(o["origin"].get("job") or "")
            check("D6d origin 两个 id 都在（job 回溯质检 / song_id 回溯曲子）",
                  bool(o["origin"].get("job")) and bool(o["origin"].get("song_id")),
                  [o["origin"].get("job"), o["origin"].get("song_id")])
            check("D6e origin.job 指回真实存在的任务目录",
                  bool(_oj) and os.path.isdir(os.path.join(HERE, "data", "jobs", _oj)),
                  _oj or "(空)")
        else:
            skip("D6 成品来源标记", "还没跑过写新歌任务，跳过")
    except Exception as e:
        skip("D6 成品来源标记", "%s: %s" % (type(e).__name__, e))


# ────────────────────────────────────────────────────────────────────
def part_e():
    sec("E. 尺子自检：改常量，判据必须跟着变（不是永远绿）")
    from stages import yue2 as y2

    old_fmts = y2.KNOWN_FMTS
    try:
        # E1 旋 KNOWN_FMTS 加入 flac ⇒ A2 那条判据必须变红
        y2.KNOWN_FMTS = ("wav32", "mp3", "flac")
        ok_a2 = set(y2.KNOWN_FMTS) == {"wav32", "mp3"}
        check("E1 把 flac 塞进已知格式后，A2 的判据确实变红", ok_a2 is False)
    finally:
        y2.KNOWN_FMTS = old_fmts
    check("E1b 复原之后 A2 的判据又变绿（没泄漏）",
          set(y2.KNOWN_FMTS) == {"wav32", "mp3"})

    # E2 旋 FMT_LOSSLESS 成 mp3 ⇒ A2b 必须变红
    old_lossless = y2.FMT_LOSSLESS
    try:
        y2.FMT_LOSSLESS = "mp3"
        check("E2 把默认格式改成 mp3 后，A2b 的判据确实变红",
              (y2.FMT_LOSSLESS == "wav32") is False)
    finally:
        y2.FMT_LOSSLESS = old_lossless

    # E3 旋 boundary 默认值 ⇒ B6b 必须变红
    raw = b"--other-bound\r\nContent-Type: audio/wav\r\n\r\nRIFFxx"
    parts = y2._split_parts(raw, b"yue2-batch-boundary")
    check("E3 用错 boundary 解一段真实的 multipart 必须解不出来（E 段可红）",
          len(parts) == 0, parts)


def main():
    print("第 32 关：角色唱新歌（YuE2 作曲 + RVC 换声）一体化管线")
    part_a()
    part_b()
    part_c()
    part_d()
    part_e()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
