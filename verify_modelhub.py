# -*- coding: utf-8 -*-
r"""门禁：声音市场（modelhub.py + studio.job_install_model）。

为什么单开一关：市场的失败方式**特别安静**，而且全都是"看起来成功了"——
  · 搜 "Adam" 第一个结果是个情感分类 LLM（文件名 safetensors + tokenizer.json）；
  · 角色名带一串语种标记（Alastor-Hazbin-hotel-ita），直接建档卡片上就是一长串；
  · 权重落盘了但索引没对上 ⇒ 唱得出来、检索没生效，谁也看不出来；
  · 坏权重 torch.load 时才炸，而"文件在"这件事没人怀疑过。
这四类都不是报错，是**结果错**，只能靠判据抓。所以本关的判据围绕两件事：
  ① 过滤规则（脏东西进不来）  ② 装完之后的东西真的能用（不是"放在那儿"）。

★ 每一段都配负对照（换坏版本必须变红），尤其是"过滤"这类规则：
  只验"该收的收进来了"是不够的 —— 一个永远返回 True 的过滤器也能过。

用法（venv：要 websocket 之外的通用环境；安装那段会起 GSV/RVC 子进程）：
    <venv>/Scripts/python.exe -s verify_modelhub.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import modelhub  # noqa: E402
import studio  # noqa: E402

# ★ 本机设了 http_proxy，urllib 默认会把请求发给代理。这里只连外网 HF 镜像，
#   本地 8756 那台服务本关不碰（API 接线在 verify_market_ui 里验）。
RVC_PY = studio.RVC_PY
CKPT = os.path.join(HERE, "stages", "rvc_ckpt_check.py")
# 装谁：这个仓库实测装得通（51MB 的 zip，里头 1 个 .pth + 配套 .index），
# 而且下过一次就留在 data/modelhub 里 ⇒ 重复跑门禁不会再下一遍。
REPO = "Loren85/Alastor-Hazbin-hotel-ita"

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""), flush=True)


def _cleanup(made, wd):
    """把本关装出来的测试角色连文件带卡片一起删干净。

    不清理的话，每跑一次回归就多几个同名角色 + 每份 50MB 上下的权重 ——
    而且它们会被 /api/models/installed 列出来，把界面上的"已装"越堆越长。
    """
    for m in made:
        rid = m["id"]
        for p in (os.path.join(wd, rid + ".pth"), studio._find_rvc_index(rid + ".pth")):
            with __import__("contextlib").suppress(Exception):
                if p and os.path.isfile(p):
                    os.remove(p)
        with __import__("contextlib").suppress(Exception):
            studio.delete_role(rid)
    if made:
        print("  （已清掉本关建的 %d 个测试角色）" % len(made))


def main():
    print("=" * 62)
    print("声音市场验收（搜索过滤 / 名字清洗 / 权重校验 / 真装 / 合集整批装）")
    print("=" * 62)

    # ---------------------------------------------------------------- A. 作品预设
    print("\n=== A. 作品预设：要覆盖到用户点名的那几部 ===")
    ws = modelhub.works()
    keys = [w["key"] for w in ws]
    check("作品表至少有 10 部", len(ws) >= 10, "%d 部" % len(ws))
    check("★ 小马宝莉 / 地狱客栈 都在预设里",
          "mlp" in keys and "hazbin" in keys, ",".join(keys[:12]))
    for k, need in (("mlp", 10), ("hazbin", 10)):
        w = modelhub.work_of(k)
        check("《%s》角色清单 >= %d 个（要的是全角色，不是一两个）"
              % (w["name"], need), w is not None and len(w["chars"]) >= need,
              len(w["chars"]) if w else None)
    check("★ 未知作品 key 返回 None（不是随手造一个）",
          modelhub.work_of("__no_such_work__") is None)
    check("★ 作品 key 不重复（界面 chip 会重影）", len(keys) == len(set(keys)))

    # ---------------------------------------------------------------- B. 过滤规则
    print("\n=== B. 过滤：脏东西必须进不来 ===")
    check(".pth 仓库被认成 rvc 权重",
          "rvc" in modelhub._file_kind(["Alastor.pth", "Alastor.index"]))
    # ★ 这条是本关最重要的负对照：safetensors 绝大多数是 LLM，照收就等于把
    #   HF 上的扩散模型全塞给用户（实测搜 "Adam" 第一结果是 tinybert）。
    check("★ 纯 safetensors 仓库不认成权重（防 LLM）",
          modelhub._file_kind(["model.safetensors", "config.json"]) == [])
    check("★ 带 tokenizer.json 的不算音色模型",
          modelhub._looks_like_voice_model("AdamCodd/tinybert", ["tokenizer.json",
                                                                 "pytorch_model.bin"]) is False)
    # ★ 这两条是"放宽规则之后"必须补的：放宽是因为只认 rvc/voice 字样会把整批
    #   角色仓库漏掉，但放宽绝不能把同名 LLM 放进来 —— 所以负对照要换成真实例子。
    check("★ .gguf 仓库不算（放宽后仍挡得住）",
          modelhub._looks_like_voice_model("atorsvn/alastor-gemma-3-1b-gguf",
                                           ["alastor.gguf", "config.json"]) is False)
    check("★ safetensors 的画图模型不算",
          modelhub._looks_like_voice_model("John6666/my-little-pony-v10-sdxl",
                                           ["pony.safetensors"]) is False)
    # 放宽的目的：只发 zip、名字里不写 rvc/voice 的角色仓库必须能进来
    # （Loren85 那批地狱客栈角色全是这个形状，不改的话自由搜索一个都出不来）
    check("只发 zip 的角色仓库算音色模型",
          modelhub._looks_like_voice_model("Loren85/Alastor-Hazbin-hotel-ita",
                                           ["Alastor Hazbin hotel ita.zip"]) is True)

    # ---------------------------------------------------------------- C. 挑文件
    print("\n=== C. 挑该下哪个文件 ===")
    f, k = modelhub.pick_weight(["readme.md", "Alastor.zip", "x.pth"])
    check("压缩包优先（通常权重+索引齐全）", k == "pack", f)
    f2, k2 = modelhub.pick_weight(["tokenizer.json", "config.json"])
    check("★ 没有权重文件时返回空（不硬挑一个）", f2 is None, f2)

    # ---------------------------------------------------------------- D. 名字清洗
    print("\n=== D. 角色名清洗（分享者爱把语种/作品塞进文件名）===")
    for src, want in [
            ("Alastor-Hazbin-hotel-ita.zip", "Alastor"),
            ("Charlie-Hazbin-Hotel-Ita-V2", "Charlie"),
            ("Emily-Ita-300.zip", "Emily"),
            ("Twilight Sparkle (Español Latino) - Weights Model.zip", "Twilight Sparkle")]:
        got = modelhub.clean_name(src)
        check("清洗 %s" % src, got == want, "得到 %r" % got)

    # ★ 负对照：下面三条**必须真的能变红**。
    #   有的作者用下划线连的是**角色名本身**（therealvul 的 Apple_Bloom /
    #   Sweetie_Belle），而上面那条"取第一段"的规矩会把它们砍成 Apple / Sweetie，
    #   卡片上就成了「阿普尔」「斯韦蒂」—— 字典里明明写着 苹果丽丽 / 甜贝儿。
    #   ⇒ 这三条就是那个 bug 的尺子：把 clean_name 里"救多词名"那段删掉，
    #     它们会**当场变红**（实测过，见 2026-10-21）。
    for src, want, cn in [
            ("Apple_Bloom.pth", "Apple Bloom", "苹果丽丽"),
            ("Sweetie_Belle.pth", "Sweetie Belle", "甜贝儿")]:
        got = modelhub.clean_name(src)
        check("★ 负对照·下划线连起来的角色名不许被切成第一段：%s" % src,
              got == want, "得到 %r（不是 %r）" % (got, want))
        got_cn = modelhub.to_cn(got) or got
        check("★ 卡片上写的是字典里的正名：%s" % src,
              got_cn == cn, "卡片会写 %r（正名是 %r）" % (got_cn, cn))

    # ★ 这三条**不列入负对照**：MrsCake 里没有 -/_ 分隔符，clean_name 走到的是
    #   "没有分隔符就保留整串"那条路 —— 把上面那段救援代码删掉它也照样绿。
    #   （实测过：负对照跑下来它 2/2 全绿。）放在这里是为了**提醒后人别把它当负对照看**：
    #   它守的是另一件事（驼峰连写的名字不许被拆成 Mrs / Cake），
    #   真正盯着"下划线救援代码还在不在"的只有上面那 4 条。
    for src, want, cn in [("MrsCake.pth", "MrsCake", "蛋糕夫人")]:
        got = modelhub.clean_name(src)
        check("驼峰连写的名字不许被切成一半：%s" % src,
              got == want, "得到 %r（不是 %r）" % (got, want))
        got_cn = modelhub.to_cn(got) or got
        check("卡片上写的是字典里的正名：%s" % src,
              got_cn == cn, "卡片会写 %r（正名是 %r）" % (got_cn, cn))

    # ---------------------------------------------------------------- E. 联网搜索
    print("\n=== E. 真搜一次（联网）===")
    try:
        res = modelhub.search("Hazbin Hotel", limit=12)
        items = res.get("items") or []
        check("搜《地狱客栈》有结果", len(items) > 0, res.get("note"))
        if items:
            # ★ 下面两条只在"确实有结果"时才作数：空集合上 all(...) 恒为真，
            #   那是假绿 —— 搜索一挂，这两条反而绿得最漂亮。
            check("每条结果都确实带权重文件",
                  all(it.get("kinds") for it in items),
                  ",".join(it["repo"] for it in items[:3]))
            check("★ 结果里没有一条是明显的非音色模型",
                  all(modelhub._looks_like_voice_model(it["repo"], it.get("files") or [])
                      for it in items))
        else:
            check("每条结果都确实带权重文件", False, "没有结果可验（空集判据不算数）")
            check("★ 结果里没有一条是明显的非音色模型", False, "同上")
    except Exception as e:
        check("搜《地狱客栈》有结果", False, "%s: %s" % (type(e).__name__, str(e)[:120]))

    # ★ 负对照：乱码关键词必须 0 结果。没有这条，"过滤器永远放行"也能全绿。
    try:
        junk = modelhub.search("zzqwxvbn-9981-not-a-voice-model", limit=12)
        check("★ 搜乱码 ⇒ 0 结果（证明不是啥都收）",
              not (junk.get("items") or []), junk.get("note"))
    except Exception as e:
        check("★ 搜乱码 ⇒ 0 结果（证明不是啥都收）", False, str(e)[:120])

    # ★ detail() 的报错不能撒谎：踩过 —— 明明仓库在、文件也在，它却说"读不到这个仓库"
    #   （真实原因是被"像不像音色模型"的规则挡了）。两种"没有"得说清楚，
    #   因为下一步完全不同：读不到 ⇒ 重试；没权重 ⇒ 换一个仓库。
    d1 = modelhub.detail(REPO)
    check("挑中的仓库 detail 读得到（安装前要读它）",
          not d1.get("error"), d1.get("error") or d1.get("repo"))
    d2 = modelhub.detail("__no__/__such__repo__")
    e2 = d2.get("error") or ""
    # 判据要的是"说清楚是仓库本身不存在"，不是某一个具体措辞。
    # （措辞改过一次：原来写"读不到"，后来拆成「不存在/没公开」和「网络不通」两种，
    #   因为它们的下一步完全不同 —— 重试 vs 换仓库。）
    check("★ 不存在的仓库说清是「不存在/没公开」（不是拿「没权重」糊弄）",
          ("不存在" in e2 or "没公开" in e2) and "没有能装的权重" not in e2, e2)

    # ---------------------------------------------------------------- F. 作品浏览
    print("\n=== F. 按作品浏览（含「没搜到的角色」必须照实列）===")
    try:
        w = modelhub.search_work("hazbin", limit=40)
        check("《地狱客栈》能浏览到仓库", len(w.get("items") or []) > 0, w.get("note"))
        check("missing 是列表（缺哪些角色要照实说）",
              isinstance(w.get("missing"), list), "%d 个没搜到" % len(w.get("missing") or []))
    except Exception as e:
        check("《地狱客栈》能浏览到仓库", False, str(e)[:120])
    bad = modelhub.search_work("__no_such_work__")
    check("★ 未知作品 ⇒ 空结果且说明", not bad.get("items")
          and bad.get("note") == "没有这个作品", bad.get("note"))

    # ------------------------------------------------------------- F2. 标签浏览
    print("\n=== F2. 按标签浏览 + 翻页（这才是「找到更多」的主力通道）===")
    try:
        b = modelhub.browse("rvc", limit=24)
        tot = b.get("total") or 0
        it1 = b.get("items") or []
        check("★ 按标签一次能列出上百个候选（关键词搜索只有十来个）",
              tot >= 100, "total=%d" % tot)
        check("第一页有结果且都带权重文件", len(it1) >= 10 and all(i.get("files") for i in it1),
              "%d 个" % len(it1))
        check("标签浏览带 qid（界面靠它翻页）", bool(b.get("qid")))
        # ★ 承重判据：第二页必须是**没见过**的仓库。没有这条，"翻页"把同一页
        #   再返回一遍也能绿 —— 用户看到的是"加载更多"点了没反应。
        p2 = modelhub.page(b.get("qid"), b.get("offset") or 0, 24)
        it2 = p2.get("items") or []
        r1 = set(i["repo"] for i in it1)
        r2 = set(i["repo"] for i in it2)
        check("★ 翻页出来的是新仓库（跟上一页不重叠）",
              len(it2) > 0 and not (r1 & r2),
              "第1页 %d / 第2页 %d / 重叠 %d" % (len(r1), len(r2), len(r1 & r2)))
        check("翻页后还报得出有没有下一页", isinstance(p2.get("has_more"), bool),
              "has_more=%s" % p2.get("has_more"))
        # 候选池很大 ⇒ 必然还有下一页（只翻了一页就到底说明池没建起来）
        check("候选上百个时，翻一页之后肯定还有", p2.get("has_more") is True,
              "has_more=%s total=%d" % (p2.get("has_more"), tot))
        # ★ 尺子自检：offset 归零必须拿回**第一页那批**。要是这条不成立，
        #   说明上面"翻页不重叠"是靠"每次随缘返回一批"混绿的，不是真的在翻。
        p0 = modelhub.page(b.get("qid"), 0, 24)
        r0 = set(i["repo"] for i in (p0.get("items") or []))
        check("★ 尺子自检：offset 归零 ⇒ 拿回第一页那批（证明 offset 真在起作用）",
              len(r0 & r1) >= max(1, len(r1) // 2),
              "跟第1页重叠 %d / 第1页 %d" % (len(r0 & r1), len(r1)))
    except Exception as e:
        check("★ 按标签一次能列出上百个候选（关键词搜索只有十来个）", False,
              "%s: %s" % (type(e).__name__, str(e)[:120]))

    # ★ 负对照：坏 qid / 不存在的标签，必须明确报错，不能返回"空列表"假装成功
    badtag = modelhub.browse("__no_such_tag__")
    check("★ 不存在的标签 ⇒ 明确报错（不是空列表假装成功）",
          bool(badtag.get("error")) and not badtag.get("items"), badtag.get("error"))
    badpage = modelhub.page("deadbeef00", 0, 24)
    check("★ 过期/假 qid ⇒ 明确说过期了（不是空列表）",
          bool(badpage.get("error")) and not badpage.get("items"), badpage.get("error"))
    check("★ 空 qid 也不会崩", not (modelhub.page("", 0, 24).get("items")), "ok")

    # ------------------------------------------------------------- F3. 中文名
    print("\n=== F3. 入库自动翻译成中文角色名（纯本地字典，不联网不花钱）===")
    cases = [("Alastor", "阿拉斯托"), ("Twilight Sparkle", "紫悦"),
             ("tokai teio", "东海帝王"), ("SatoruGojo", "五条悟"),
             ("Hatsune Miku", "初音未来"), ("Derpy", "小呆"),
             # 2026-10-13 补的两件事：**连写的驼峰**（以前整批漏）+ **尾部版本**
             ("TwilightSparkle", "紫悦"), ("RainbowDash", "云宝"),
             ("StarlightGlimmer", "星光熠熠"), ("AppleBloom", "苹果丽丽"),
             ("FlurryHeart", "风雪之心"), ("CozyGlow", "和煦光流"),
             ("AdagioDazzleV2", "阿达吉奥·达佐"), ("AriaBlazeV2", "阿里亚·布雷兹"),
             # 分享者拼错了一个字母，也要能救回来（本地 difflib，不是猜身份）
             ("AddagioDazzle", "阿达吉奥·达佐"),
             ("Tirek", "提雷克"), ("Tempest Shadow", "狂风将军"),
             ("Storm King", "风暴大王"), ("QueenChrysalis", "虫茧女王"),
             ("PrincessCadance", "音韵公主"), ("NightmareMoon", "梦魇之月")]
    for src, want in cases:
        got = modelhub.to_cn(src)
        check("翻得出来：%s → %s" % (src, want), got == want, "实际 %r" % got)

    # ★ 负对照：**不像人名的一律不翻**。压缩包名、版本号、一堆辅音挤在一起的乱串
    #   一律保留英文 —— 翻出来的东西跟「这是谁」没关系，那就是噪音。
    for junk in ["zzqwxvbn nobody", "some-random-voice-9021", "",
                 "my_voice_model_v3", "rvc weights pack 4"]:
        check("★ 不像人名就不翻：%r" % junk, modelhub.to_cn(junk) == "",
              "实际 %r" % modelhub.to_cn(junk))

    # ★ 承重负对照：**子串不能当命中**。踩过 —— Frieren（葬送的芙莉莲）里含
    #   `eren` 子串，被整串包含匹配翻成了「艾伦」（进击的巨人里的另一个角色）。
    #   一个像真的、其实是错的中文名，比留英文糟得多，这条就是钉住它的。
    check("★ 子串不算命中：Frieren 是「芙莉莲」，绝不许成「艾伦」（里面含 eren）",
          modelhub.to_cn("Frieren") == "芙莉莲"
          and modelhub.to_cn("Frieren") != modelhub.to_cn("eren"),
          "实际 %r" % modelhub.to_cn("Frieren"))
    check("★ 但驼峰切段要算：SatoruGojo 要能翻出「五条悟」",
          modelhub.to_cn("SatoruGojo") == "五条悟",
          "实际 %r" % modelhub.to_cn("SatoruGojo"))
    check("★ 字典是本地常量（不联网、不调接口）",
          isinstance(getattr(modelhub, "CN", None), dict) and len(modelhub.CN) > 50,
          "%d 条" % len(modelhub.CN))

    # ------------------------------------------------ 音译兜底（字典也没有的时候）
    #
    # 为什么要有这一层：2026-10-13 用户装了 16 个小马宝莉，卡片上一半还是英文 ——
    # 字典追不上别人上传的速度。对人名，翻成中文本来就是按读音译（Adagio Dazzle
    # → 阿达吉奥·达佐），所以补一层**只用拼写**的音译。
    # 它的安全边界：**输出只能是中文，且不能是字典里任何一个已有角色**
    # （"凭空指定身份"是事故，"读不出汉字"顶多难看）。
    _cjk = re.compile(r"[\u4e00-\u9fff]")
    dict_vals = set(modelhub.CN.values())
    for src in ["BabsSeed", "Button Mash", "Daybreaker"]:
        got = modelhub.to_cn(src)
        check("音译兜底：%s → 中文且不冒充字典里的角色" % src,
              bool(got) and all(_cjk.match(c) or c in "·() " for c in got)
              and got not in dict_vals,
              "实际 %r" % got)
    # ★ 负对照：同一串必须每次都一样（是个函数，不是随机数/网络结果的缓存）
    check("★ 音译是纯函数（同一输入两次结果一致、且不联网）",
          modelhub.to_cn("BabsSeed") == modelhub.to_cn("BabsSeed")
          and modelhub.transliterate("BabsSeed") == modelhub.transliterate("BabsSeed"),
          modelhub.transliterate("BabsSeed"))
    # ★ 负对照：字典词条优先于音译 —— 有确定译名时绝不能输出按读音拼的那个
    check("★ 确定译名压住音译：Alastor 是「阿拉斯托」而不是逐字拼出来的别的样子",
          modelhub.to_cn("Alastor") == "阿拉斯托"
          and modelhub.transliterate("Alastor") != ""
          and modelhub.to_cn("Alastor") != modelhub.transliterate("Alastor"),
          "字典=%r 音译=%r" % (modelhub.to_cn("Alastor"),
                            modelhub.transliterate("Alastor")))

    # ------------------------------------------------ 音译层的四个坑（第二轮补）
    #
    # 上一轮只验了"音译层能出中文、不冒充字典里的角色"，**没验译得对不对**。
    # 于是同一次装出来的名字里就混着这些：Octavia→奥克塔维（尾音被整个吞掉）、
    # Rosie→罗塞、Charlie→克哈尔莱、CarmillaCarmine→空（驼峰连写被判"不像人名"）。
    #
    # ★ 每一条都配**尺子自检**：把引擎改回出 bug 的那个样子，判据必须变红。
    #   只验"现在是对的"不够 —— 一根永远绿的尺子等于没尺子。
    for src, want in [("Octavia", "奥克塔维亚"), ("Rosie", "罗西"),
                      ("Charlie", "查尔利"), ("Moxxie", "莫克西")]:
        got = modelhub.transliterate(src)
        check("★ 音译要译对：%s → %s（不只是要求「能出中文」）" % (src, want),
              got == want, "实际 %r" % got)
    _cc = modelhub.transliterate("CarmillaCarmine")
    check("★ 驼峰连写不再被当成超长单词：CarmillaCarmine 必须出中文，不能是空串",
          bool(_cc) and all(_cjk.match(c) or c in "·() " for c in _cc),
          "实际 %r" % _cc)
    check("★ 只写 Pinkie 也是碧琪（分享者常常只写名不写姓）",
          modelhub.to_cn("Pinkie") == "碧琪", "实际 %r" % modelhub.to_cn("Pinkie"))

    # ---- 尺子自检：把引擎改回出 bug 的样子，上面那些判据必须变红 ----
    _nuc2 = set(modelhub._NUC2)
    modelhub._NUC2.add("ia")                  # 模拟"连续元音整团吞掉、余下的丢掉"
    _bad_ia = modelhub.transliterate("Octavia")
    modelhub._NUC2 = _nuc2
    check("★ 尺子自检·吞尾音：把 ia 塞回复合元音表后 Octavia 必须译错（否则判据是死的）",
          _bad_ia != "奥克塔维亚", "坏版本=%r" % _bad_ia)

    _ie = modelhub._IE_MIN
    modelhub._IE_MIN = 99                     # 模拟"词尾 -ie 当复合元音 ei"
    _bad_ie = modelhub.transliterate("Rosie")
    modelhub._IE_MIN = _ie
    check("★ 尺子自检·词尾 ie：关掉 -ie 削音后 Rosie 必须译错",
          _bad_ie != "罗西", "坏版本=%r" % _bad_ie)

    _cv = dict(modelhub._CV)
    modelhub._CV.pop("ch", None)              # 模拟"复辅音一律拆成单字母声母"
    _bad_ch = modelhub.transliterate("Charlie")
    modelhub._CV.clear()
    modelhub._CV.update(_cv)
    check("★ 尺子自检·长声母：把 ch 从声母表里拿掉后 Charlie 必须译错",
          _bad_ch != "查尔利", "坏版本=%r" % _bad_ch)

    _wo = modelhub._words_of
    modelhub._words_of = lambda s: [p for p in re.split(r"[\s_]+", str(s or "").strip()) if p]
    _bad_camel = modelhub.transliterate("CarmillaCarmine")
    modelhub._words_of = _wo
    check("★ 尺子自检·驼峰：不切驼峰时 CarmillaCarmine 必须返回空串（旧 bug 就是这个症状）",
          _bad_camel == "", "坏版本=%r" % _bad_camel)

    # ------------------------------------------------ 撞名时谁拿干净名、后缀从哪来
    #
    # 上一轮是"按角色表顺序先到先得"发名字，实测同一个角色装三份时，干净名
    # 「阿达吉奥·达佐」被 V2 那个包抢走、正主反倒挂着后缀；两个 Applejack 还并列
    # 成两张一模一样的「苹果嘉儿」，用户根本分不清谁是谁。
    #
    # ★ 整段在**临时目录**里跑（ROLES_JSON 和 ROLES_DIR 一起换掉）——
    #   上一版这条判据直接改用户真库里的角色名，跑一次门禁动一次用户数据。
    _tmpd = tempfile.mkdtemp(prefix="vs_mh_names_")
    _oj, _od = studio.ROLES_JSON, studio.ROLES_DIR
    studio.ROLES_JSON = os.path.join(_tmpd, "roles.json")
    studio.ROLES_DIR = os.path.join(_tmpd, "roles")
    try:
        # 故意把"最正的写法"种在**最后一个**：谁拿干净名要看座次，不是种植顺序
        planted = []
        for alias, fn in [("AdagioDazzleV2", "AdagioDazzleV2.zip"),
                          ("AddagioDazzle", "AddagioDazzle.zip"),
                          ("AdagioDazzle", "AdagioDazzleV3.zip")]:
            r = studio.create_role(name=alias, kind="singing",
                                   source="modelhub:MusicBox27/MyLittlePonyRVC")
            db = studio.load_roles()
            for x in db["roles"]:
                if x["id"] == r["id"]:
                    x["rvc"] = {"file": fn, "imported": True}
            studio.save_roles(db)
            planted.append(r["id"])
        studio.retranslate_imported()
        got = [(studio.get_role(i) or {}).get("name") for i in planted]
        check("★ 干净名给最正的写法（它种在最后一个 ⇒ 靠座次不是靠顺序）",
              got[2] == "阿达吉奥·达佐", "实际 %r" % (got,))
        check("★ 三份同名角色三个名字全不一样（用户能挑出谁是谁）",
              len(set(got)) == 3, "实际 %r" % (got,))
        check("★ 后缀只从市场文件名 / 原名里取，绝不现编",
              got[0] == "阿达吉奥·达佐 (V2)"
              and got[1] == "阿达吉奥·达佐 (AddagioDazzle)", "实际 %r" % (got,))
        # 负对照：真的重复安装（同名同文件）也得能区分开，不能两张卡一模一样
        r = studio.create_role(name="AdagioDazzle", kind="singing",
                               source="modelhub:MusicBox27/MyLittlePonyRVC")
        db = studio.load_roles()
        for x in db["roles"]:
            if x["id"] == r["id"]:
                x["rvc"] = {"file": "AdagioDazzleV3.zip", "imported": True}
        studio.save_roles(db)
        studio.retranslate_imported()
        fourth = (studio.get_role(r["id"]) or {}).get("name")
        check("★ 负对照：同名同文件的重复安装也要能区分开（不能并成两张一样的卡）",
              fourth not in got and fourth != "阿达吉奥·达佐",
              "第四份=%r 已有=%r" % (fourth, got))
    finally:
        studio.ROLES_JSON, studio.ROLES_DIR = _oj, _od
        shutil.rmtree(_tmpd, ignore_errors=True)

    # ------------------------------------------------ 已经装进来的老角色补译
    # 用户不该为了享受后来补的字典去重装一遍 ⇒ 提供 retranslate_imported()。
    # ★ 判据自足：自己种一个 modelhub: 来源的英文角色，翻完自己删。
    # ★ 也整段搬进临时目录：ROLES_JSON **和 ROLES_DIR 一起换掉**，
    #   否则 create_role / delete_role 还会在用户真库里建目录、写索引。
    _tmpd2 = tempfile.mkdtemp(prefix="vs_mh_retr_")
    _oj2, _od2 = studio.ROLES_JSON, studio.ROLES_DIR
    studio.ROLES_JSON = os.path.join(_tmpd2, "roles.json")
    studio.ROLES_DIR = os.path.join(_tmpd2, "roles")
    probe = studio.create_role(name="Zecora", kind="singing",
                              source="modelhub:MusicBox27/MyLittlePonyRVC")
    try:
        done = studio.retranslate_imported()
        after = studio.get_role(probe["id"]) or {}
        check("★ 老角色能补译：modelhub 来源的英文名被翻成中文",
              after.get("name") == "泽科拉",
              "改前=Zecora 改后=%r" % after.get("name"))
        check("★ 补译后原名还在（alias + 简介里都留着，能核对）",
              after.get("alias") == "Zecora"
              and "Zecora" in (after.get("intro") or ""),
              "alias=%r intro=%r" % (after.get("alias"), after.get("intro")))
        # ★ 负对照：**自己建的角色不许被偷偷改名**（那是用户自己起的名字）
        mine = studio.create_role(name="Bob", kind="speech", source="")
        try:
            studio.retranslate_imported()          # 默认不带 all=True
            check("★ 负对照：不带 all 时，用户自建角色的名字不会被改",
                  (studio.get_role(mine["id"]) or {}).get("name") == "Bob",
                  (studio.get_role(mine["id"]) or {}).get("name"))
        finally:
            studio.delete_role(mine["id"])
        check("补译清单里带着改了哪几个", any(d["id"] == probe["id"] for d in done),
              str(done)[:120])
    finally:
        studio.delete_role(probe["id"])
        studio.ROLES_JSON, studio.ROLES_DIR = _oj2, _od2
        shutil.rmtree(_tmpd2, ignore_errors=True)
    check("★ 门禁不该动用户真库：跑完这段后 roles.json 的指向已经还原",
          studio.ROLES_JSON == _oj2 and studio.ROLES_DIR == _od2
          and not os.path.exists(_tmpd2),
          "ROLES_JSON=%s" % studio.ROLES_JSON)
    # 装进去的角色名要是中文，且**原名得留在简介里**（翻错了一眼能看出来）
    check("包列表里带中文名字段",
          all("cn" in p for p in (modelhub.list_packs(
              ["Alastor Hazbin hotel ita.zip", "Derpy.zip"]) or [])),
          str(modelhub.list_packs(["Alastor Hazbin hotel ita.zip"])))

    # ---------------------------------------------------------------- G. 权重校验尺子
    print("\n=== G. 权重能不能加载（文件在 ≠ 能加载）===")
    junk_pth = os.path.join(HERE, "_mh_junk.pth")
    with open(junk_pth, "wb") as fh:
        fh.write(b"this is definitely not a torch checkpoint" * 100)
    try:
        rc = subprocess.run([RVC_PY, "-s", CKPT, junk_pth], cwd=studio.RVC_ROOT,
                            capture_output=True, text=True, timeout=600).returncode
        check("★ 垃圾文件被判为不可用（退出码非 0）", rc != 0, "rc=%d" % rc)
    finally:
        with __import__("contextlib").suppress(Exception):
            os.remove(junk_pth)
    # 反向对照：尺子不能是"一律拒绝"。拿一个自训的真权重。
    real = None
    wd = studio._rvc_weights_dir()
    for fn in sorted(os.listdir(wd)):
        if fn.endswith(".pth") and not fn.startswith("r040361d2"):
            real = os.path.join(wd, fn)
            break
    if real:
        rc = subprocess.run([RVC_PY, "-s", CKPT, real], cwd=studio.RVC_ROOT,
                            capture_output=True, text=True, timeout=900).returncode
        check("真权重被判为可用（尺子不是一律拒绝）", rc == 0,
              "rc=%d %s" % (rc, os.path.basename(real)))
    else:
        check("真权重被判为可用（尺子不是一律拒绝）", False, "assets/weights 里没有 .pth")

    # ---------------------------------------------------------------- H. 真装一遍
    print("\n=== H. 真装一个模型（下载→解压→落盘→对索引→建档→能加载）===")
    made = []
    try:
        job = studio.new_job(None, "install_model", "门禁·声音市场")
        out = studio.job_install_model(job, REPO, max_roles=1)
        made = out.get("installed") or []
        check("装出至少一个角色", len(made) >= 1, json.dumps(made, ensure_ascii=False))

        for m in made:
            rid = m["id"]
            wp = os.path.join(wd, rid + ".pth")
            check("权重落在 RVC 的 assets/weights/<id>.pth",
                  os.path.isfile(wp) and os.path.getsize(wp) > 1024 * 1024,
                  "%.1f MB" % (os.path.getsize(wp) / 1048576.0) if os.path.isfile(wp) else "不存在")
            # ★ 索引对不上是最静的失败：唱得出来，但 index_rate 被白降级成 0
            ix = studio._find_rvc_index(rid + ".pth")
            check("索引被 _find_rvc_index 认出来（否则检索白降级）",
                  bool(ix) and os.path.isfile(ix), os.path.basename(ix) if ix else None)
            role = studio.get_role(rid) or {}
            rvc = role.get("rvc") or {}
            check("角色卡标了 imported 且来源是 modelhub",
                  rvc.get("imported") is True
                  and str(role.get("source", "")).startswith("modelhub:"),
                  "%s / %s" % (rvc.get("imported"), role.get("source")))
            check("角色名被洗干净了（不是一长串文件名）",
                  0 < len(role.get("name", "")) <= 40, role.get("name"))
    except Exception as e:
        check("装出至少一个角色", False, "%s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        # 装出来的是**本关的测试数据**（成品数 0），跑完必须收干净
        _cleanup(made, wd)

    # ---------------------------------------------------------------- I. 合集整批装
    #
    # ★ 这一段是冲着用户那句"要小马宝莉/地狱客栈的**全角色**"去的：
    #   HF 上这类作品的模型几乎都是**合集仓库**（实测小马宝莉那个合集有 54 个
    #   角色包 / 3.5GB）。只装文件清单第一个的话，用户点一次「安装」拿到的是个
    #   边角角色，"全角色"就成了一句空话。所以必须能整批装、且每个角色都真的能用。
    print("\n=== I. 合集仓库：一次装出一整批角色 ===")
    PACK_REPO = "Xhepyxopila/MyLittlePonyRVC"
    made2 = []
    try:
        d = modelhub.detail(PACK_REPO, sizes=True)
        packs = d.get("packs") or []
        check("合集仓库能列出多个可装的包", len(packs) >= 2,
              "%d 个 / 共 %s MB" % (len(packs), d.get("total_mb")))
        check("★ 体积问得出来（否则用户没法决定下不下）",
              bool(d.get("total_mb")) and all(p.get("size") for p in packs[:3]),
              "%s MB" % d.get("total_mb"))
        check("★ 每个包都洗出了角色名（不是一长串文件名）",
              all(0 < len(p.get("name") or "") <= 40 for p in packs),
              "、".join(p["name"] for p in packs[:4]))
        job = studio.new_job(None, "install_model", "门禁·合集整批装")
        out = studio.job_install_model(job, PACK_REPO, max_roles=2, all_packs=True)
        made2 = out.get("installed") or []
        check("整批装出**多个**角色（不是只装第一个）", len(made2) >= 2,
              json.dumps([m["name"] for m in made2], ensure_ascii=False))
        check("每个角色的权重都真的落盘了",
              all(os.path.isfile(os.path.join(wd, m["id"] + ".pth"))
                  and os.path.getsize(os.path.join(wd, m["id"] + ".pth")) > 1024 * 1024
                  for m in made2),
              "、".join(m["name"] for m in made2))
    except Exception as e:
        check("整批装出**多个**角色（不是只装第一个）", False,
              "%s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        _cleanup(made2, wd)

    # ------------------------------------------------------- J. 只装其中一个
    #
    # ★ 这一段是冲着"我只要合集里的某一个角色"去的：以前卡片上只有「装全部 /
    #   只装第一个」两个按钮，想要中间那个（比如只要 Twilight Sparkle）就只能把
    #   整个合集下来，或者拿第一个自己根本不认识的角色。
    print("\n=== J. 合集里只装**指定那一个**（不是整批、也不是第一个）===")
    made3 = []
    try:
        d = modelhub.detail(PACK_REPO)
        packs = d.get("packs") or []
        if len(packs) < 2:
            check("合集里挑一个装", False, "这个仓库只有 %d 个包，测不了" % len(packs))
        else:
            # 专挑**第二个**：如果代码其实是"永远装第一个"，这一条立刻就红
            want = packs[1]
            job = studio.new_job(None, "install_model", "门禁·只装一个")
            out = studio.job_install_model(job, PACK_REPO, fname=want["file"],
                                           name=want.get("name"), all_packs=False)
            made3 = out.get("installed") or []
            check("★ 只装一个 ⇒ 就只建一个角色（不是整批都装了）",
                  len(made3) == 1, "%d 个：%s" % (len(made3),
                                                 "、".join(str(m.get("name")) for m in made3)))
            if made3:
                got = (made3[0].get("raw") or made3[0].get("name") or "").lower()
                exp = (want.get("name") or "").lower()
                check("★ 装到的正是指定的那个包（不是第一个）",
                      got == exp or exp in got or got in exp,
                      "要 %s / 装到 %s" % (want.get("name"), got))
                check("★ 明确避开了第一个包",
                      (packs[0].get("name") or "").lower() != got,
                      "第一个是 %s，装到的是 %s" % (packs[0].get("name"), got))
    except Exception as e:
        check("★ 只装一个 ⇒ 就只建一个角色（不是整批都装了）", False,
              "%s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        _cleanup(made3, wd)

    # ---------------------------------------------------------------- 收尾
    leftover = [m["id"] for m in (made + made2 + made3)
                if os.path.isfile(os.path.join(wd, m["id"] + ".pth"))]
    check("本关建的临时角色已清干净", not leftover, leftover)

    print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
    if BAD:
        print("!! 红的：" + "；".join(BAD))
        return 1
    print("结论：声音市场门禁全绿")
    return 0


if __name__ == "__main__":
    sys.exit(main())
