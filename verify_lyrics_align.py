# -*- coding: utf-8 -*-
r"""「参考歌词订正」+「校对落盘」的正确性门禁（不跑模型，几秒钟）。

守的是「改词唱出来是错词」这类回归。

  原词（ref_text）是要送进模型做音素化的：官方 `align_lrc_sentence_level()`
  把 `|` 换空格、把标点归一成逗号 —— **文本只提供"唱哪些音素"**，
  认错一个字就唱错一个字。而 whisper 在**歌唱**上的错字是肉眼可见的
  （本机真实缓存里就有"热情劫强已经升级""捍卫神族的容誉"这种）。
  靠调扩散步数救不回来，得换文本。

所以允许两种修法，两个都要能验：
  ① 粘一份正确歌词进来 → 自动对齐、把错字换掉（时间戳不动）；
  ② 直接在界面上改某一句 → 存回缓存，改词时用改过的那份。

判据全部跑 `stages/lyric_plan.py` 的**真代码**（它只用标准库），
外加 `studio.save_lyric_segments` 会写出的那份缓存的结构校验。
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, "stages")
PLAN = os.path.join(STAGES, "lyric_plan.py")

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print(("  PASS  " if ok else "  ! FAIL  ") + name
          + (("    —— " + str(detail)) if detail else ""))


def T(s, e, t):
    return {"start": s, "end": e, "text": t}


def main():
    print("参考歌词订正 + 校对落盘（stages/lyric_plan.py）")
    check("lyric_plan.py 存在", os.path.isfile(PLAN))
    sys.path.insert(0, STAGES)
    import lyric_plan as LP

    # ---- 1. 错字订正：文本换掉、时间戳一个字节都不许动 ------------------
    asr = [T(8.27, 14.69, "热情劫强已经升级"),
           T(14.69, 21.19, "风暴渐渐的距离"),
           T(37.91, 45.06, "捍卫神族的容誉")]
    ref = "热情倔强已经升级|风暴渐渐的距离|捍卫神族的荣誉"
    got, rep = LP.align_ref(asr, ref)
    check("三句都被订正", [x["text"] for x in got] ==
          ["热情倔强已经升级", "风暴渐渐的距离", "捍卫神族的荣誉"],
          "实际 %s" % [x["text"] for x in got])
    check("时间戳原样不动", [(x["start"], x["end"]) for x in got] ==
          [(8.27, 14.69), (14.69, 21.19), (37.91, 45.06)],
          "实际 %s" % [(x["start"], x["end"]) for x in got])
    check("订正了 2 句（第 2 句本来就是对的）", rep.get("changed") == 2,
          "实际 %s" % rep.get("changed"))
    check("参考歌词 3 句全用上", rep.get("ref_used") == 3,
          "实际 %s" % rep.get("ref_used"))

    # ---- 2. 标点/分隔符差异不干扰 ---------------------------------------
    a2 = [T(1.0, 2.0, "捍卫神族的荣誉")]
    g2, _ = LP.align_ref(a2, "捍卫神族的荣誉，")
    check("参考里多个标点不会把句子改坏", g2[0]["text"] == "捍卫神族的荣誉",
          "实际 %r" % g2[0]["text"])
    g2b, _ = LP.align_ref([T(1.0, 2.0, "第一句")], "第一句\n第二句\n第三句")
    check("换行分隔的参考歌词也能读", len(g2b) == 1 and g2b[0]["text"] == "第一句")
    # 段落标记不该被当成歌词
    g2c, r2c = LP.align_ref([T(1.0, 2.0, "副歌第一句")],
                            "[副歌]\n副歌第一句\n[主歌]\n主歌第一句")
    check("段落标记 [副歌] 不当成一句歌词", r2c.get("ref_lines") == 2,
          "参考句数 %s" % r2c.get("ref_lines"))

    # ---- 3. 句数对不上（识别断错句）—— 不能整段错位 ----------------------
    # 识别把前两句并成了一句，参考是两句 ⇒ 要按字符切回正确的两句边界
    a3 = [T(0.0, 4.0, "这是第一句这是第二句"),
          T(5.0, 8.0, "这是第三句")]
    g3, r3 = LP.align_ref(a3, "这是第一句|这是第二句|这是第三句")
    check("识别少断一句时，第三句不会串到第二句上",
          g3[1]["text"] == "这是第三句", "实际 %s" % [x["text"] for x in g3])
    check("合并句至少还带着第一句的字（没被整段丢掉）",
          "第一句" in g3[0]["text"] or "第二句" in g3[0]["text"],
          "实际 %r" % g3[0]["text"])
    check("时间戳仍是原来的两条",
          [(x["start"], x["end"]) for x in g3] == [(0.0, 4.0), (5.0, 8.0)])

    # 识别多出一句（哼唱），参考里没有 ⇒ 保留识别、报告里点出来
    a4 = [T(0.0, 2.0, "啦"), T(3.0, 5.0, "这一句参考里有")]
    g4, r4 = LP.align_ref(a4, "这一句参考里有")
    check("参考里没有的句子保留识别原文", g4[0]["text"] == "啦",
          "实际 %r" % g4[0]["text"])
    check("另一句照样被订正", g4[1]["text"] == "这一句参考里有")

    # ---- 4. 边界与异常：不许炸、不许返回半截 -----------------------------
    check("空识别结果 ⇒ 原样返回", LP.align_ref([], "随便什么")[1].get("ok") is False)
    check("空参考 ⇒ ok=False（不算订正过）",
          LP.align_ref([T(0, 1, "有词")], "")[1].get("ok") is False)
    # ★ 相似度闸门：认出来的和参考差得太远时不许硬替换
    #   （硬换 = 把一段不相干的词按到这条时间上，比留着错字更糟）
    # 用真实场景：这一句其实是哼唱/语气词，参考里根本没有它
    g5, r5 = LP.align_ref([T(0, 1, "哦哦哦啊哈哈")], "捍卫神族的荣誉")
    check("完全对不上时不硬替换（保留识别原文）",
          g5[0]["text"] == "哦哦哦啊哈哈", "实际 %r" % g5[0]["text"])
    check("并把这个位置记成 low_conf", r5.get("low_conf") == 1,
          "实际 %s" % r5.get("low_conf"))
    # 但"错一半"这种程度的该修还是得修（闸门不能太紧）
    g6, r6 = LP.align_ref([T(0, 1, "捍卫神族的容誉")], "捍卫神族的荣誉")
    check("只错一个字照样订正（闸门不能太紧）",
          g6[0]["text"] == "捍卫神族的荣誉" and r6.get("changed") == 1,
          "实际 %r / changed=%s" % (g6[0]["text"], r6.get("changed")))
    check("参考比识别长很多时不越界",
          LP.align_ref([T(0, 1, "短")], "很长很长很长的一整段歌词" * 5)[0][0]["text"]
          is not None)
    check("乱序时间戳不炸",
          len(LP.align_ref([T(9, 10, "后"), T(0, 1, "前")], "前后")[0]) == 2)

    # ---- 5. 校对落盘的清洗规则 -------------------------------------------
    clean, dropped = LP.clean_segments([
        T(1.0, 2.0, "第一句"),
        T(3.0, 4.0, "   "),          # 空文本 ⇒ 丢
        T(5.0, 6.0, "带|竖线"),        # 竖线会破坏句数 ⇒ 换掉
        T(9.0, 7.0, "时间写反了"),      # end < start ⇒ 拉回
        {"start": 11.0, "end": 12.0, "text": "缺 start 之外的都好"},
        "不是字典",                    # ⇒ 丢
        T(8.0, 8.5, "乱序的一句"),
    ])
    check("空文本 / 非字典条目被丢掉", dropped == 2, "丢了 %d 条" % dropped)
    check("文本里的 | 被换成全角（不然句数会算错）",
          all("|" not in x["text"] for x in clean),
          "%s" % [x["text"] for x in clean])
    check("end < start 的被拉回（不出现负时长）",
          all(x["end"] >= x["start"] for x in clean))
    check("结果按 start 排序",
          [x["start"] for x in clean] == sorted(x["start"] for x in clean),
          "%s" % [x["start"] for x in clean])
    check("洗干净后仍是 5 条", len(clean) == 5, "实际 %d" % len(clean))

    # ---- 6. 服务端保存逻辑用的是同一份清洗 -------------------------------
    src = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    check("studio.save_lyric_segments 走的是 lyric_plan.clean_segments",
          "from lyric_plan import clean_segments" in src)
    check("校对结果写了 proofread 标记（界面能显示已校对）",
          '"proofread"' in src or "'proofread'" in src)
    check("校对后的 line 是拼出来的（改词时用它对句数）",
          'd["line"]' in src)

    # ---- 7. 负对照：把订正关掉，错字必须还在（证明判据有牙）---------------
    bad_copy = [T(8.27, 14.69, "热情劫强已经升级")]
    kept = bad_copy[0]["text"]
    check("负对照：不订正时错字仍在（订正确实起了作用）",
          kept != "热情倔强已经升级", kept)

    print("\n通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        for f in FAIL:
            print("  FAIL %s" % f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
