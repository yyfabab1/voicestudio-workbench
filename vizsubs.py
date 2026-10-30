# -*- coding: utf-8 -*-
"""视频字幕的「可改 + 中文对照」。

两件事，都很小，但都容易做错：

  ① **纯函数的合并**（`merge`）：用户改过的字幕按**行下标**盖回自动生成的那份。
     只盖文字和中文，**时间轴和分配表一个字节都不动** —— "谁在唱"是靠
     `multi_takes` 定的，改字幕绝不能把"谁亮"改乱（那是最难查的一类错）。

  ② **翻译**（`translate`）：英文歌词 → 简体中文。分两级，都在明处：
       · 一级：本地网关（OpenAI 兼容，默认 `http://127.0.0.1:8791/v1`）。
         它翻歌词比词典好得多（试过："I love you so" → "我是如此爱你"）；
       · 二级：在线词典（mymemory，逐句，免费、不要 key）。
     两级都读不到就**扔异常**，让上层说人话 —— 绝不返回一串空字符串
     假装翻好了（那会让用户以为"这首歌没有中文"，其实是我们没连上）。

为什么不把这段塞进 studio.py：它要 urllib、要超时、要重试，和 studio 那堆
本地文件逻辑不是一类东西。单独一个模块，门禁也能直接 import 进去测纯函数。
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# 不去碰系统代理：本机 http_proxy 是间歇性的（见 memory），走它反而更慢更不稳
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

LLM_BASE = (os.environ.get("VS_LLM_BASE") or "http://127.0.0.1:8791/v1").rstrip("/")
LLM_MODEL = os.environ.get("VS_LLM_MODEL") or "default-model"
MM_URL = "https://api.mymemory.translated.net/get"
UA = {"User-Agent": "Mozilla/5.0 (VoiceStudio subtitle)"}

BATCH = 40          # 一次请求多少行。整首 60 多句拆两三段，token 和时间都稳
MAX_LINE = 300      # 单行字幕最多留多少字（防止把整段贴进一行把画面撑爆）
LLM_TIMEOUT = 30    # 40 句实测 5.7 秒；30 秒还没回来就是那口网关不对，别让界面干等
DICT_TIMEOUT = 8


def _say(log, msg):
    """有 job 就写进任务日志，没有就写 stderr。

    ★ 以前这里是 `if log:` —— 从 HTTP 接口调进来时 log 是 None，于是一次翻译
      失败**一个字都不留**，只看到界面转圈。坏信号必须有坏后果（至少留条日志）。
    """
    if log:
        log(msg)
    else:
        sys.stderr.write("[vizsubs] %s\n" % msg)
        sys.stderr.flush()


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post_json(url, body, timeout=90):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=dict(UA, **{
        "Content-Type": "application/json"}))
    with _OPENER.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def merge(lines, saved):
    """把用户改过的字幕盖到自动生成的那些上。纯函数，返回新列表。

    `saved` 形如 [{"i": 3, "text": "...", "zh": "..."}]，缺字段就沿用原来的。
    **空字符串也算"用户清空了这一句"** —— 那是他的编辑意图，不许fallback 回去。
    """
    rows = list(lines or [])
    if not saved:
        return [dict(x) for x in rows]
    by_i = {}
    for k, row in enumerate(saved):
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i", k))
        except (TypeError, ValueError):
            i = k
        by_i[i] = row
    out = []
    for i, ln in enumerate(rows):
        cur = dict(ln)
        row = by_i.get(i)
        if row:
            for key in ("text", "zh"):
                v = row.get(key)
                if isinstance(v, str):
                    cur[key] = v.strip()[:MAX_LINE]
        out.append(cur)
    return out


def _llm_chunk(texts, log=None):
    sysm = ("你是歌词翻译。把每一行歌词译成简洁自然的简体中文，保留语气和分行。"
            "不要解释、不要合并、不要加引号、不要漏行。"
            "输入是一个 JSON 字符串数组，输出必须是与输入**等长**的 JSON 字符串数组，"
            "只输出数组本身。")
    body = {"model": LLM_MODEL, "temperature": 0.2, "max_tokens": 2000,
            "messages": [{"role": "system", "content": sysm},
                         {"role": "user", "content": json.dumps(texts, ensure_ascii=False)}]}
    d = _post_json(LLM_BASE + "/chat/completions", body, timeout=LLM_TIMEOUT)
    txt = (d.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    txt = txt.strip()
    txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt).strip()
    got = json.loads(txt)
    if not isinstance(got, list):
        raise ValueError("回来的是 %s，不是数组" % type(got).__name__)
    got = [str(x).strip() for x in got]
    if len(got) != len(texts):
        raise ValueError("行数对不上：要 %d 行，回来 %d 行" % (len(texts), len(got)))
    return got


def _by_llm(texts, log=None):
    out = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        try:
            out.extend(_llm_chunk(chunk, log))
        except Exception as e:
            _say(log, "本地翻译第 %d~%d 句没成：%s: %s"
                 % (i + 1, i + len(chunk), type(e).__name__, str(e)[:140]))
            return None
    return out


def _by_dict(texts, log=None):
    out, got_n, dead = [], 0, 0
    for t in texts:
        q = (t or "").strip()
        z = ""
        if q:
            try:
                d = _get_json("%s?q=%s&langpair=en|zh-CN" % (MM_URL, urllib.parse.quote(q)),
                              timeout=DICT_TIMEOUT)
                z = (((d.get("responseData") or {}).get("translatedText")) or "").strip()
                if not z or z.lower() == q.lower():
                    z = ""
                dead = 0
            except Exception as e:                       # noqa: BLE001
                z = ""
                dead += 1
                if dead >= 3:
                    # 连着三次不响 = 这个口也不通。别让用户对着转圈等 60 次超时。
                    _say(log, "在线词典连续 3 句没响（%s），放弃这一档" % type(e).__name__)
                    return None
            time.sleep(0.12)                    # 免费接口，别把人家打急了
        out.append(z)
        got_n += 1 if z else 0
    _say(log, "在线词典翻到 %d/%d 句" % (got_n, len(texts)))
    return out if got_n else None


def translate(texts, log=None):
    """英文歌词 → 中文。返回 (zh_list, how)。

    `how` 会原样显示给用户（"本地模型" / "在线词典"），因为两种质量不一样，
    用户有权知道他看的是哪一档。
    """
    texts = [str(t or "")[:MAX_LINE] for t in (texts or [])]
    if not any(t.strip() for t in texts):
        raise ValueError("没有可翻的内容（这些行都是空的）")
    zh = _by_llm(texts, log)
    if zh is not None:
        return zh, "本地模型"
    zh = _by_dict(texts, log)
    if zh is not None:
        return zh, "在线词典（逐句，质量一般，建议自己再看一遍）"
    raise ValueError("翻译没跑通：本地模型（%s）和在线词典都没响。"
                     "中文你可以在表格里自己填，或者用 VS_LLM_BASE 指一个能用的模型。"
                     % LLM_BASE)
