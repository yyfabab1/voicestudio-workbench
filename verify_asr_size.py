# -*- coding: utf-8 -*-
r"""歌词识别「档位」门禁（medium / large-v3 / singing 歌声专用）—— 离屏，不跑模型、不联网。

★ 为什么要为"一个下拉框"单开一关

  识别档位是**唯一决定原词准不准**的开关，而原词是要送进模型做音素化的：
  认错一个字，唱出来就错一个字。它有三处会悄悄坏掉，而且坏了之后
  **表面完全正常**（界面照出结果、进度照走完）—— 正是最该被钉死的那类：

    ① 换档位但缓存键没换 ⇒ 用精准档认完，读回来的还是标准档那份，
       用户以为"精准档也没变准"，实际一次都没跑过；
    ② 档位名没归一 ⇒ 前端传 "large-v3"、后端只认 "large"，静默退回默认档；
    ③ 缺文件却报"模型就绪" ⇒ 识别直接失败，或者更糟：半截权重跑出垃圾结果。

  这一关把三处各自钉死，全部只用标准库 + 临时目录，几秒钟跑完。

用法：python verify_asr_size.py     （退出码 0 = 全绿）
"""
import os
import shutil
import sys
import tempfile
import json

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name + (("  | " + detail) if detail else ""),
          flush=True)
    return cond


def main():
    print("歌词识别档位（medium 标准档 / large-v3 精准档 / singing 歌声专用档）")
    import studio

    # ---------------------------------------------------------------- 1 归一化
    print("\n[1] 档位名归一化 —— 前端/命令行怎么写都得认")
    for raw, want in (("large", "large"), ("large-v3", "large"), ("large_v3", "large"),
                      ("LARGE-V3", "large"), ("  large  ", "large"),
                      ("medium", "medium"), ("", "medium"), (None, "medium"),
                      ("不存在的档位", "medium"),
                      # 歌声专用档的各种写法（前端可能写 singing / m4singer / 中文）
                      ("singing", "singing"), ("m4singer", "singing"),
                      ("SINGING", "singing"), ("歌声专用", "singing"),
                      ("歌声", "singing"), ("singing_v2", "medium"),
                      # large-v2 是另一个模型，本机没有 ⇒ 必须退回默认档而不是硬着头皮去加载
                      ("large-v2", "medium")):
        got = studio.asr_size_norm(raw)
        check("asr_size_norm(%r) == %s" % (raw, want), got == want, "实际 %r" % got)

    # ---------------------------------------------------------------- 2 目录
    print("\n[2] 三档是三个目录（不是同一个 weight 换名字）")
    dm, dl = studio.whisper_dir("medium"), studio.whisper_dir("large")
    ds = studio.whisper_dir("singing")
    check("medium / large 目录不同", dm != dl, "%s vs %s" % (dm, dl))
    check("singing 是第三个目录", ds not in (dm, dl), ds)
    check("medium 目录名正确", os.path.basename(dm) == "faster-whisper-medium", dm)
    check("large 目录名正确", os.path.basename(dl) == "faster-whisper-large-v3", dl)
    check("singing 目录名正确", os.path.basename(ds) == "faster-whisper-m4singer", ds)
    check("默认档 = medium", studio.whisper_dir(None) == dm)

    # ---------------------------------------------------------------- 3 就绪判定
    print("\n[3] 「模型就绪」不许谎报")
    check("标准档权重在本机，就绪", studio.whisper_ready("medium") is True, dm)
    # 歌声专用档是转好 CT2 后放进来的（上游只发 transformers 格式）。
    # 本机有 ⇒ 必须报就绪；没有 ⇒ 必须报 False，**且不许去自动下载**
    # （它的转换要 transformers>=4.56，runtime 里是 4.51.3，自动链路不靠谱）。
    check("歌声专用档权重在本机，就绪", studio.whisper_ready("singing") is True, ds)
    tmp_s = tempfile.mkdtemp(prefix="vs_asrsing_")
    saved_s = studio.ASR_SIZES["singing"]
    try:
        studio.ASR_SIZES["singing"] = tmp_s           # 空目录
        check("歌声专用档缺权重 ⇒ 就绪=False", studio.whisper_ready("singing") is False, tmp_s)
    finally:
        studio.ASR_SIZES["singing"] = saved_s
        shutil.rmtree(tmp_s, ignore_errors=True)
    with open(os.path.join(HERE, "studio.py"), encoding="utf-8") as f:
        sst = f.read()
    check("歌声专用档不做自动下载（缺了就明说，别在后台跑没测全的链路）",
          'sz == "singing"' in sst and "不做自动下载" in sst)
    check("转换步骤固化成工具了（否则没人知道下次怎么重来）",
          os.path.isfile(os.path.join(HERE, "tools", "convert_whisper_ct2.py")))

    tmp = tempfile.mkdtemp(prefix="vs_asrsz_")
    saved = studio.ASR_SIZES["large"]
    try:
        studio.ASR_SIZES["large"] = tmp           # 空目录：一个权重文件都没有
        check("目录为空 ⇒ 就绪=False", studio.whisper_ready("large") is False, tmp)
        # 只放两个小文件、缺 model.bin —— 最容易骗过检查的就是"看起来下过"
        for f in ("config.json", "tokenizer.json"):
            with open(os.path.join(tmp, f), "w", encoding="utf-8") as fh:
                fh.write("{}")
        check("缺 model.bin ⇒ 就绪=False（下了一半不能算好）",
              studio.whisper_ready("large") is False, tmp)
        with open(os.path.join(tmp, "model.bin"), "wb") as fh:
            fh.write(b"\0" * 16)
        check("三个文件齐了 ⇒ 就绪=True", studio.whisper_ready("large") is True, tmp)
    finally:
        studio.ASR_SIZES["large"] = saved
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 4 缓存键
    print("\n[4] 缓存键必须带上档位（换档 = 换模型 = 换结果）")
    song = os.path.join(HERE, "studio.py")     # _sep_cache_key 只要求文件存在
    pm = studio.lyric_cache_path(song, "zh", "medium")
    pl = studio.lyric_cache_path(song, "zh", "large")
    ps = studio.lyric_cache_path(song, "zh", "singing")
    check("同歌同语种，两档的缓存文件不同", pm != pl, "%s vs %s" % (pm, pl))
    check("歌声专用档也不撞键（换档必须换结果）",
          ps not in (pm, pl), "%s vs %s / %s" % (ps, pm, pl))
    check("三档互不相同", len({pm, pl, ps}) == 3, "%s" % sorted({pm, pl, ps}))
    # 缓存键是哈希后的文件名，没法从名字上看出档位 —— 所以要直接问键函数：
    # 忘记把 size 拼进 tag，两档的键就会撞车（症状是"换档位没变化"）。
    km = studio._sep_cache_key(song, "lyrics|zh|medium")
    kl = studio._sep_cache_key(song, "lyrics|zh|large")
    check("档位真的进了缓存键（忘拼 size 两档会撞车）", km != kl,
          "%s vs %s" % (km, kl))
    check("语种也进了缓存键",
          studio._sep_cache_key(song, "lyrics|en|medium") != km)
    check("换语种也要换键", studio.lyric_cache_path(song, "en", "medium") != pm)
    check("同参数两次调用一致（键必须稳定）",
          studio.lyric_cache_path(song, "zh", "large") == pl)

    # ---------------------------------------------------------------- 5 兼容回退
    print("\n[5] 升级前的旧缓存不能白认（只有默认档允许回退）")
    cache = tempfile.mkdtemp(prefix="vs_lyrcache_")
    saved_cache = studio.LYRIC_CACHE
    studio.LYRIC_CACHE = cache
    try:
        # 2026-10-20 之前的键里没有档位，那份产物就是 medium 认的
        old_key = studio._sep_cache_key(song, "lyrics|zh") + ".json"
        old_path = os.path.join(cache, old_key)
        segs = [{"start": 1.0, "end": 3.0, "text": "旧缓存里的句子"}]
        with open(old_path, "w", encoding="utf-8") as f:
            json.dump({"line": "旧缓存里的句子", "segments": segs}, f, ensure_ascii=False)

        got = studio.load_lyric_cache(song, "zh", "medium")
        check("默认档能读到升级前的旧缓存",
              bool(got) and (got.get("line") == "旧缓存里的句子"),
              "实际 %r" % (got or {}).get("line"))
        check("精准档不许拿标准档的结果顶替",
              studio.load_lyric_cache(song, "zh", "large") is None)

        # 新键一旦存在就以新键为准（不能反过来被旧文件盖住）
        with open(pl and os.path.join(cache, studio._sep_cache_key(
                song, "lyrics|zh|medium") + ".json"), "w", encoding="utf-8") as f:
            json.dump({"line": "新键里的句子", "segments": segs}, f, ensure_ascii=False)
        got2 = studio.load_lyric_cache(song, "zh", "medium")
        check("新旧都在时以新键为准",
              bool(got2) and got2.get("line") == "新键里的句子",
              "实际 %r" % (got2 or {}).get("line"))

        # ------------------------------------------------------------ 6 落盘
        print("\n[6] 校对结果要写到「当前档位」那份，不能串档")
        studio.save_lyric_segments(song, [
            {"start": 0.0, "end": 2.0, "text": "第一句"},
            {"start": 2.5, "end": 4.0, "text": "第二句"}], lang="zh", size="large")
        p_large = os.path.join(cache, studio._sep_cache_key(
            song, "lyrics|zh|large") + ".json")
        check("校对写进了 large 那份", os.path.isfile(p_large), p_large)
        d = studio.load_lyric_cache(song, "zh", "large")
        check("large 那份能读回来", bool(d) and len(d.get("segments") or []) == 2,
              "实际 %r" % (d or {}).get("segments"))
        check("存下来的档位标记是 large", (d or {}).get("asr_size") == "large",
              "实际 %r" % (d or {}).get("asr_size"))
        dmed = studio.load_lyric_cache(song, "zh", "medium")
        check("medium 那份没被串改",
              (dmed or {}).get("line") == "新键里的句子",
              "实际 %r" % (dmed or {}).get("line"))
    finally:
        studio.LYRIC_CACHE = saved_cache
        shutil.rmtree(cache, ignore_errors=True)

    # ---------------------------------------------------------------- 7 静态
    print("\n[7] 下载脚本与前端确实接上了精准档")
    src = os.path.join(HERE, "stages", "dl_whisper.py")
    with open(src, encoding="utf-8") as f:
        dl = f.read()
    check("large 档走 hf-mirror（huggingface.co 在本机恒 000）",
          "hf-mirror.com" in dl and "large-v3" in dl)
    check("下载文件清单里有 model.bin（不然永远就绪不了）", '"model.bin"' in dl)

    html = os.path.join(HERE, "web", "index.html")
    with open(html, encoding="utf-8") as f:
        ih = f.read()
    check("界面上有档位下拉", 'id="sgAsr"' in ih)
    check("默认档是 medium（不让人一上来就等 3 GB）",
          'value="medium" selected' in ih)
    check("下拉里也给了精准档", 'value="large"' in ih)
    check("下拉里也给了歌声专用档", 'value="singing"' in ih)

    js = os.path.join(HERE, "web", "app.js")
    with open(js, encoding="utf-8") as f:
        aj = f.read()
    check("识别请求带上了档位", 'ref: refTxt, asr });' in aj or "asr," in aj or "asr }" in aj)
    check("校对面板也按档位取（否则刷新就读回另一档）",
          '"&lang=zh&asr="' in aj or "&asr=" in aj)

    print("\n通过 %d / %d" % (len(OK), len(OK) + len(BAD)))
    if BAD:
        print("失败：")
        for b in BAD:
            print("  - " + b)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
