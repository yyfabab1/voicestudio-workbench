# -*- coding: utf-8 -*-
r"""SRT 字幕的读写 —— 刻意**只用标准库**。

为什么单独一个文件，而不是塞进 studio.py：这段是纯字符/纯时间轴运算，
门禁要在几秒内反复跑它（含负对照）。一旦挂上 torch / whisper 那种重依赖，
就只能在真环境里偶尔跑一次 —— 那等于没有门禁。

--------------------------------------------------------------------------
这个格式的三个坑（都实测过，不是看文档背下来的）

  ① **毫秒前面是逗号，不是点**：`00:00:10,160`。
     写成点就成了 WebVTT 的语法，不少播放器（含某些剪辑软件）整条都不显示，
     而且不报错 —— 症状是"字幕功能坏了"，查半天查不到是这一个小标点。

  ② **条目之间靠空行分隔**。少一个空行，两条会被吞成一条；
     多一堆空行，有的解析器会把空行当条目，序号就对不上了。

  ③ **时间轴不能倒挂或重叠**（end < start，或下一句 start < 上一句 end）。
     标准里没定义该怎么处理，各家行为不一样：有的直接跳过、有的叠着显示。
     ⇒ 所以 `from_segments` 里必须**先排序、再补尾、再裁重叠**，不能相信输入。

--------------------------------------------------------------------------
为什么"歌自带的字幕"要优先于"识别出来的字幕"

  老板要的是「**本来的** srt」—— 那首歌原本配的那份。识别（ASR）只是
  **没有自带字幕时的兜底**，不能反过来盖掉真货：
    · 原字幕是发行方校对过的，专有名词、断句都对；
    · ASR 在歌唱上必然有错字（这条踩过太多次了）。
  ⇒ 所以 `pick_source` 的优先级是：自带 > 成品里记的（校对过的）> 识别缓存。

命令行：
    python -s srt_cli.py to-srt   <歌词.json|.lrc> <输出.srt> [总时长秒]
    python -s srt_cli.py from-srt <输入.srt> <输出.json>
    python -s srt_cli.py selftest
退出码：0 成功；2 用法/解析失败。
"""
import json
import os
import re
import sys

MAX_CHARS = 18          # 中文一行放这么多字。太长一行会横穿画面、挡住脸
MIN_DUR = 0.30          # 一条字幕最短停留（秒）：再短人眼根本读不完
TAIL_PAD = 0.12         # 末尾多留这么久，别刚好卡在下一句起点上消失

# 时间戳：`00:00:10,160` 或 `00:00:10.160`（后者也收，见坑 ①）
_TS = re.compile(r"(\d{1,3}):([0-5]?\d):([0-5]?\d)[,.](\d{1,3})")
_ARROW = re.compile(r"\s*-->\s*")


def ts(sec):
    """秒 → `HH:MM:SS,mmm`。毫秒前必须是逗号（坑 ①）。"""
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60.0
    return "%02d:%02d:%s" % (h, m, ("%06.3f" % s).replace(".", ","))


def _sec(hh, mm, ss, ms):
    """时间戳 → 秒。`160` 是 160 毫秒，不是 16 毫秒（按三位补齐）。"""
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms.ljust(3, "0")) / 1000.0


def _wrap(text, n=MAX_CHARS):
    """把一句折成几行，别让字幕横穿画面。

    中文没有空格，只能按字数切；英文/法文按单词切，不然会把词从中间劈开。
    """
    text = (text or "").strip()
    if len(text) <= n:
        return [text]
    if re.search(r"[A-Za-zÀ-ÿ]", text):          # 有拉丁字母 ⇒ 按词折
        out, cur = [], ""
        for w in text.split():
            if cur and len(cur) + 1 + len(w) > n:
                out.append(cur)
                cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            out.append(cur)
        return out or [text]
    return [text[i:i + n] for i in range(0, len(text), n)]


def dump(rows, wrap=True):
    """[(start, end, text), ...] → SRT 文本。

    空行分隔（坑 ②）；结尾留一个换行，否则最后一条有些播放器不收。
    """
    out = []
    for i, row in enumerate(rows, 1):
        try:
            s, e, t = row[0], row[1], (row[2] if len(row) > 2 else "")
        except (TypeError, IndexError):
            continue
        s = max(0.0, float(s))
        e = float(e)
        if e < s:
            e = s
        t = "" if t is None else str(t)
        lines = _wrap(t) if wrap and t else ([t] if t else [])
        if not lines:
            continue                              # 没词就别占一条
        out.append(str(i))
        out.append("%s --> %s" % (ts(s), ts(e)))
        out.extend(lines)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def parse(text):
    """SRT 文本 → [(start, end, text), ...]。读不出来就返回空。

    ★ 容忍现实里的脏文件（都是遇到过的）：BOM、CRLF、序号错乱或干脆没有、
      时间戳用点分隔、条目之间空行数量不固定。
    ★ 但**不静默吃掉时间轴错误**：条目能认出时间戳却没有一条合法的时间轴，
      说明这不是 SRT，返回空让上层去说人话（铁律 12e）。
    """
    if text is None:
        return []
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig", errors="replace")
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")

    rows = []
    seen_ts = 0
    # 按「含 --> 的那一行」切块：那一行才是可靠的结构锚点，靠空行切会踩坑 ②
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        m = _TS.match(lines[i].strip())
        if m and _ARROW.search(lines[i]):
            a = _TS.match(lines[i].strip())
            tail = _ARROW.split(lines[i].strip(), 1)[1]
            b = _TS.match(tail.strip())
            seen_ts += 1
            if a and b:
                st = _sec(a.group(1), a.group(2), a.group(3), a.group(4))
                en = _sec(b.group(1), b.group(2), b.group(3), b.group(4))
                # 收文本：直到下一个时间戳行 / 空行
                buf = []
                j = i + 1
                while j < len(lines):
                    s = lines[j].strip()
                    if not s:
                        break
                    if _TS.match(s) and _ARROW.search(s):
                        break
                    buf.append(lines[j])
                    j += 1
                rows.append((st, en, "\n".join(buf).strip()))
                i = j
                continue
        i += 1
    if seen_ts and not rows:
        return []
    rows.sort(key=lambda r: r[0])
    return rows


# ---------------------------------------------------------------- 生成
def from_segments(segs, dur=None):
    """歌词识别结果 `segments` → SRT 行。

    segments 形如 `[{"start": 10.16, "end": 12.42, "text": "..."}]`，
    但**别假设字段都齐全**（历史缓存里就有缺 end 的），所以：
      · 缺 end  ⇒ 用下一句的 start 补；最后一句用整首时长；都拿不到就 +2 秒；
      · 倒挂   （end <= start）⇒ 同上补；
      · 重叠   （本句 start < 上一句 end）⇒ 把上一句的 end 裁到本句 start，
                若裁完不够 MIN_DUR 就把这一整条丢掉（宁缺勿错）；
      · 空文本 ⇒ 丢掉，不占一条。
    """
    out = []
    for s in (segs or []):
        if not isinstance(s, dict):
            continue
        # ★ 没起点的条目**直接丢掉**，不许当成 0 秒。
        #   写成 `s.get("start", 0.0) or 0.0` 的话，缺 start 的脏条目会被静默
        #   安到 0 秒上 —— 那就是在编造时间轴，跟"按行数均分"是同一类错，
        #   而且更隐蔽（数字看着很正常，只是这条字幕从头开始一直挂着）。
        #   注意 `{"start": 0}`（真的从 0 秒开始）要保留：判的是 `is None`，不是假值。
        if s.get("start") is None:
            continue
        try:
            st = float(s.get("start"))
        except (TypeError, ValueError):
            continue
        t = str(s.get("text") or "").strip()
        if not t:
            continue
        try:
            en = float(s.get("end") or 0.0) or None
        except (TypeError, ValueError):
            en = None
        if en is None or en <= st:
            en = None                                  # 交给下面的「补尾」
        out.append([st, en, t])

    if not out:
        return []
    out.sort(key=lambda r: r[0])

    # 补尾：优先用下一句起点，让字幕首尾相接（唱词之间不留缝）
    for i, r in enumerate(out):
        if r[1] is None:
            if i + 1 < len(out):
                r[1] = out[i + 1][0]
            elif dur and dur > r[0]:
                r[1] = float(dur)
            else:
                r[1] = r[0] + 2.0

    # 裁重叠：从前往后推，保证 end 单调不减
    for i in range(len(out) - 1):
        if out[i][1] > out[i + 1][0]:
            out[i][1] = out[i + 1][0]
    if dur:
        for r in out:
            if r[0] > float(dur):
                r[0] = float(dur)
            if r[1] > float(dur):
                r[1] = float(dur)

    keep = []
    for r in out:
        if r[1] - r[0] >= MIN_DUR - 1e-9:
            keep.append((round(r[0], 3), round(r[1], 3), r[2]))
    return keep


def from_lrc(pairs, dur=None):
    """[(秒, 文本), ...]（viz.parse_lrc 的输出）→ SRT 行。"""
    segs = [{"start": a, "end": None, "text": b} for a, b in (pairs or [])]
    return from_segments(segs, dur)


def pick_source(song_path, lyric_text="", segs=None):
    """决定这条成品的字幕从哪来，返回 (来源说明, 行数)。

    优先级：**歌自带 > 成品里记的（校对过的）> 没有**。
    只做判断、不偷跑识别（下 1.5GB 模型是用户该自己按按钮的事）。
    """
    try:
        stem = os.path.splitext(os.path.abspath(str(song_path or "")))[0]
    except Exception:
        stem = ""
    if stem:
        for ext in (".srt", ".SRT"):
            if os.path.isfile(stem + ext):
                return ("自带字幕", len(parse(open(stem + ext, encoding="utf-8-sig",
                                                 errors="replace").read())))
    if lyric_text and segs:
        return ("识别歌词", len(segs))
    if lyric_text:
        return ("仅歌词无时间轴", 0)
    return ("无歌词", 0)


# ---------------------------------------------------------------- 自检
def selftest():
    """尺子自检：这些数字必须能动，否则下面所有结论都不算数。"""
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        if not cond:
            ok = False
        print("  [%s] %s%s" % ("ok" if cond else "!!", name,
                               ("  " + extra) if extra else ""))

    # ① 逗号不是点
    chk("毫秒分隔符是逗号", ts(10.16) == "00:00:10,160", ts(10.16))
    chk("超过一小时", ts(3661.5) == "01:01:01,500", ts(3661.5))

    # ② 往返一致
    rows = [(0.0, 1.5, "第一句"), (1.5, 3.0, "第二句"), (3.0, 5.0, "第三句")]
    txt = dump(rows)
    back = parse(txt)
    chk("dump → parse 往返一致",
        len(back) == 3 and abs(back[1][0] - 1.5) < 1e-6 and back[2][2] == "第三句")

    # ③ 容忍脏输入：CRLF + BOM + 序号错乱 + 点分隔
    dirty = ("\ufeff1\r\n00:00:00.000 --> 00:00:01.500\r\naaa\r\n\r\n"
             "99\r\n00:00:01.500 --> 00:00:03.000\r\nbbb\r\n")
    chk("容忍 CRLF/BOM/错序号/点分隔", len(parse(dirty)) == 2)

    # ④ 负对照：完全不是 SRT 的文本 ⇒ 返回空（不许瞎解析出东西）
    chk("负对照：乱文本 ⇒ 空", parse("这不是字幕，随便写点什么。\n没有时间戳。\n") == [])

    # ⑤ 补尾 + 裁重叠
    bad = [{"start": 5.0, "end": None, "text": "缺尾"},
           {"start": 2.0, "end": 9.0, "text": "倒序且超长"},
           {"start": 3.0, "end": 4.0, "text": "被上一句吃掉"},
           {"start": 10.0, "end": 11.0, "text": "正常"}]
    got = from_segments(bad, dur=12.0)
    chk("乱序输入会排序", [g[0] for g in got] == sorted(g[0] for g in got),
        str([g[0] for g in got]))
    chk("时间戳不重叠", all(got[i][1] <= got[i + 1][0] + 1e-9 for i in range(len(got) - 1)),
        str([(g[0], g[1]) for g in got]))
    chk("不超出整首时长", all(g[1] <= 12.0 + 1e-9 for g in got))
    chk("太短的会被丢掉", all(g[1] - g[0] >= 0.3 - 1e-9 for g in got))

    # ⑥ 负对照：空输入 ⇒ 空，绝不产出空条目
    chk("负对照：空 segments ⇒ 空", from_segments([], dur=10) == [])
    chk("负对照：全是空文本 ⇒ 空",
        from_segments([{"start": 0, "end": 1, "text": "  "}], dur=10) == [])

    # ⑦ 折行
    chk("长句会折行", len(_wrap("一" * 40)) > 1)
    chk("短句不折行", len(_wrap("短")) == 1)

    print("  自检：%s" % ("通过" if ok else "★ 有红"))
    return ok


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "selftest":
        return 0 if selftest() else 1
    if cmd == "to-srt" and len(argv) >= 4:
        src, dst = argv[2], argv[3]
        dur = float(argv[4]) if len(argv) > 4 else None
        try:
            with open(src, encoding="utf-8-sig", errors="replace") as f:
                raw = f.read()
        except OSError as e:
            print("读不了 %s：%s" % (src, e))
            return 2
        st = raw.lstrip()
        if st.startswith("{"):                       # 识别结果 json
            try:
                d = json.loads(st)
            except ValueError as e:
                print("这不是歌词 json：%s" % e)
                return 2
            raws = d.get("segments") or []
            rows = from_segments(raws, dur or d.get("duration"))
        else:                                        # 纯文本（每行一句）
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            rows = from_segments([{"start": None, "end": None, "text": l}
                                  for l in lines], dur)
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                f.write(dump(rows))
        except OSError as e:
            print("写不了 %s：%s" % (dst, e))
            return 2
        print("SRT_DONE %d 行 %s" % (len(rows), dst))
        return 0
    if cmd == "from-srt" and len(argv) >= 4:
        src, dst = argv[2], argv[3]
        try:
            with open(src, encoding="utf-8-sig", errors="replace") as f:
                rows = parse(f.read())
        except OSError as e:
            print("读不了 %s：%s" % (src, e))
            return 2
        try:
            os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
            with open(dst, "w", encoding="utf-8") as f:
                json.dump({"segments": [{"start": a, "end": b, "text": c}
                                        for a, b, c in rows]}, f,
                          ensure_ascii=False, indent=2)
        except OSError as e:
            print("写不了 %s：%s" % (dst, e))
            return 2
        print("OK %d 行" % len(rows))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
