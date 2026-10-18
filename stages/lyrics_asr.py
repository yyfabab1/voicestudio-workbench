# -*- coding: utf-8 -*-
r"""从音频里自动识别歌词（**带时间戳 + 分句**）。

用途：改词台里的「原歌词」栏以前得用户自己抄一遍。现在点一下就填好，
用户只需要写新词。

为什么不用整合包自带的 ASR（`tools/asr/*`）：
  · 它输出的是「整首歌拼成一行的文本」，**没有时间戳**；而改词必须按时值分句
    —— 句数和音频时长配不上，YingMusic 会把后段唱乱（这条是踩过的坑）。
  · 它识别中文时走 FunASR，在**歌唱**上的识别率不如 Whisper。
这里用 faster-whisper，拿词级时间戳，再按标点折成「句」（约等于换气点）。

用法（用 **GPT-SoVITS 的 runtime** 跑 —— faster_whisper 装在那套里）：
    runtime\python.exe -s <本脚本> --wav <人声轨.wav> --out <结果.json> \
        [--model <faster-whisper 模型目录>] [--lang zh] [--max-sec 14]

输出 JSON：
    {"lang":"zh","segments":[{"start":12.3,"end":15.8,"text":"..."}],
     "line":"第一句|第二句|...",   ← 直接能填进「原歌词」框
     "duration":225.0}

退出码：0 成功；2 参数/模型缺失；1 识别失败。
"""
import argparse
import json
import os
import re
import sys
import traceback

# 句末/句中能断句的标点（中文 + 英文）
_BREAK = "。！？!?；;…"
_COMMA = "，,、：:　 "

# Whisper 的中文输出默认是**繁体**（实测："腐蝕的靈魂 黃金的戰神"）。
# 给用户填进改词框的东西必须是简体，否则他得先自己转一遍 —— 那就失去意义了。
_CC = None


def to_simplified(text):
    global _CC
    if not text:
        return text
    try:
        if _CC is None:
            from opencc import OpenCC
            _CC = OpenCC("t2s")
        return _CC.convert(text)
    except Exception:
        return text


def _mk_line(words, s, e):
    """把一串词拼成一句；纯标点/空白返回 None。"""
    txt = "".join(x["w"] for x in words).strip().strip("".join(_BREAK + _COMMA))
    if not txt or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", txt):
        return None
    return {"start": round(float(s), 3), "end": round(float(e), 3),
            "text": to_simplified(txt)}


def split_lines(words, max_sec=14.0, max_chars=22, max_gap=1.0):
    """把词级时间戳折成「句」。

    规则：遇到句末标点立即断；遇到逗号类标点且已经攒了几个字也断；
    单句过长（超过 max_sec 或 max_chars）**强制**断 —— 否则一句被唱成
    30 秒，改词时新词会被摊到整段旋律上，节奏必然乱。

    ★ 还有一条**必须**的：词与词之间的**空洞**超过 max_gap 秒也断句。
    这条是踩坑补上的 —— 实测整曲跑出三句分别是 17.1 / 25.1 / 24.1 秒，
    源头是三个间奏（`86.9 → 102.4` 隔 18.5 秒、`28.6 → 37.9` 隔 9.3 秒、
    `148.5 → 166.7` 隔 18.1 秒）。原实现只按"当前词的 end − 句首"算时长，
    于是**整段间奏被算进了句子**，直到间奏后的第一个词出现才触发超长断句
    —— 断是断了，可断出来的句子时长是假的（25 秒里 18 秒是没人唱的）。
    间奏/长换气本来就是天然的句界，在这里断句既修时长、也更贴近"一句"的含义。
    """
    lines = []
    cur_w = []
    cur_s = None
    prev_e = None
    for it in words:
        # ① 先看空洞：上一句已经唱完，先把手里这串收尾（end 用上一个词的结束）
        if cur_w and prev_e is not None and (it["s"] - prev_e) > max_gap:
            ln = _mk_line(cur_w, cur_s, prev_e)
            if ln:
                lines.append(ln)
            cur_w, cur_s = [], None
        # ② 正常累积
        if cur_s is None:
            cur_s = it["s"]
        cur_w.append(it)
        txt = "".join(x["w"] for x in cur_w)
        dur = it["e"] - cur_s
        hit_end = it["w"] and it["w"][-1] in _BREAK
        hit_comma = it["w"] and it["w"][-1] in _COMMA and len(txt) >= 6
        too_long = dur >= max_sec or len(txt) >= max_chars
        if hit_end or hit_comma or too_long:
            ln = _mk_line(cur_w, cur_s, it["e"])
            if ln:
                lines.append(ln)
            cur_w, cur_s = [], None
        prev_e = it["e"]
    if cur_w:
        ln = _mk_line(cur_w, cur_s, cur_w[-1]["e"])
        if ln:
            lines.append(ln)
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", required=True, help="faster-whisper 模型目录")
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--max-sec", type=float, default=14.0)
    ap.add_argument("--dump-words", action="store_true",
                    help="把词级时间戳明细也写进 JSON（排查分句问题时用）")
    a = ap.parse_args()

    if not os.path.isfile(a.wav):
        print("[err] 音频不存在：%s" % a.wav)
        return 2
    if not os.path.isdir(a.model):
        print("[err] 模型目录不存在：%s" % a.model)
        return 2

    from faster_whisper import WhisperModel

    try:
        device = "cuda" if __import__("torch").cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"
    ct = "float16" if device == "cuda" else "int8"
    print("加载 faster-whisper：%s（%s / %s）" % (a.model, device, ct))
    sys.stdout.flush()
    model = WhisperModel(a.model, device=device, compute_type=ct)

    lang = None if a.lang in ("auto", "") else a.lang
    print("识别中…")
    sys.stdout.flush()
    segments, info = model.transcribe(
        a.wav, language=lang, beam_size=5, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        word_timestamps=True, condition_on_previous_text=False)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            if w.word:
                words.append({"w": w.word, "s": float(w.start), "e": float(w.end)})
    if not words:
        print("[err] 一个词都没识别出来")
        return 1

    lines = split_lines(words, max_sec=a.max_sec)
    out = {
        "lang": getattr(info, "language", a.lang),
        "duration": round(float(getattr(info, "duration", 0.0)), 2),
        "words": len(words),
        "segments": lines,
        "line": "|".join(l["text"] for l in lines),
    }
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    if a.dump_words:
        wp = os.path.splitext(a.out)[0] + ".words.json"
        with open(wp, "w", encoding="utf-8") as f:
            json.dump([{"w": w["w"], "s": round(w["s"], 2), "e": round(w["e"], 2)}
                       for w in words], f, ensure_ascii=False, indent=0)
        print("词级明细 → %s" % wp)

    print("LYRICS_DONE %d 句 / %d 词，语种 %s" % (len(lines), len(words), out["lang"]))
    for l in lines[:12]:
        print("  %7.2f~%7.2f  %s" % (l["start"], l["end"], l["text"]))
    if len(lines) > 12:
        print("  …（共 %d 句）" % len(lines))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
