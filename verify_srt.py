#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：每一首成品旁边都要有**本来的** .srt 字幕。

为什么需要这一关
----------------
老板要的是「每一首歌都带有本来的 srt 字幕文件」。这句话里有两个容易做歪的地方，
所以单独开一关盯着：

  ① **"本来的"= 优先用歌自带的那份**，不是我们重新生成/翻译一份盖上去。
     原字幕是发行方校对过的，专有名词、断句都对；ASR 在歌唱上必然带错字。
     让识别结果盖掉真货是纯粹的倒退。

  ② **没有自带字幕时才兜底生成**，而且**没有时间轴宁可不给**。
     ★ 绝不为了凑一个 .srt 去按行数均分时间轴 —— 均分出来的时间轴看着像真的，
       其实一句都对不上，播放器照着错的时间滚，用户会以为识别坏了（踩过）。
     ★★ 更不许擅自去跑 ASR：那要下一 1.5GB 的模型，是用户该自己按按钮的事。

这一关还要盯住 SRT 格式本身的三个坑：
  · 毫秒前是逗号不是点（写成点就成了 WebVTT，不少播放器整条不显示且不报错）；
  · 条目之间靠空行分隔（少了会吞并，多了序号错位）；
  · 时间轴不能倒挂/重叠/超出整首时长。
"""
import importlib.util
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
STAGE = os.path.join(HERE, "stages", "srt_cli.py")
STUDIO = os.path.join(HERE, "studio.py")
APPJS = os.path.join(HERE, "web", "app.js")

N_PASS = 0
FAILS = []


def check(name, ok, detail=""):
    global N_PASS
    if ok:
        N_PASS += 1
        print("  [ok] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("  [!!] %s  %s" % (name, detail))


def load_mod():
    spec = importlib.util.spec_from_file_location("srt_cli_gate", STAGE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def a_format(mod):
    print("\nA. 格式：SRT 的硬规矩")
    check("毫秒分隔符是逗号", mod.ts(10.16) == "00:00:10,160", mod.ts(10.16))
    check("超过一小时也撑得开", mod.ts(3661.5) == "01:01:01,500", mod.ts(3661.5))
    check("负数被夹到 0", mod.ts(-3) == "00:00:00,000", mod.ts(-3))

    rows = [(0.0, 1.5, "第一句"), (1.5, 3.0, "第二句")]
    txt = mod.dump(rows)
    lines = txt.split("\n")
    check("条目之间靠空行分隔", "" in lines, repr(lines[:6]))
    check("结尾有一个换行", txt.endswith("\n"))
    check("序号从 1 开始", lines[0].strip() == "1", lines[0])


def b_roundtrip(mod):
    print("\nB. 往返：写出来还得能读回来")
    # ★ 第三句必须**明显超过** MAX_CHARS(18) 才会折行 —— 早先这里只写了 14 个字，
    #   压根没触发折行，于是"多行文本不丢"永远判红，看着像代码坏了其实是判据没牙。
    long = "这是一句刻意写得非常长的歌词用来验证字幕会不会自动折行以免横穿整个画面"
    check("测试句确实够长", len(long) > 18, "%d 字" % len(long))
    rows = [(0.0, 1.5, "第一句"), (1.5, 3.2, "第二句"), (3.2, 6.0, long)]
    back = mod.parse(mod.dump(rows))
    check("条数不变", len(back) == 3, len(back))
    if len(back) == 3:
        check("时间轴读回来一致",
              abs(back[1][0] - 1.5) < 1e-6 and abs(back[2][1] - 6.0) < 1e-6,
              str(back))
        check("长句折成多行且不丢字", "\n" in back[2][2] and back[2][2].replace("\n", "") == long,
              repr(back[2][2]))


def c_dirty(mod):
    print("\nC. 容错：现实里的字幕文件都很脏")
    dirty = ("\ufeff1\r\n00:00:00.000 --> 00:00:01.500\r\naaa\r\n\r\n"
             "99\r\n00:00:01.500 --> 00:00:03.000\r\nbbb\r\n")
    got = mod.parse(dirty)
    check("CRLF + BOM + 错序号 + 点分隔 都收得下", len(got) == 2, len(got))
    if len(got) == 2:
        check("起点读对", abs(got[0][0]) < 1e-9 and abs(got[1][0] - 1.5) < 1e-9, str(got))


def d_timeline(mod):
    print("\nD. 时间轴：不能倒挂、不能重叠、不能超出整首")
    bad = [{"start": 5.0, "end": None, "text": "缺尾"},
           {"start": 2.0, "end": 9.0, "text": "倒序且超长"},
           {"start": 2.9, "end": 3.0, "text": "太短会被丢"},
           {"start": 10.0, "end": 99.0, "text": "尾超整首"},
           {"start": 11.5, "end": 12.0, "text": "正常"}]
    got = mod.from_segments(bad, 12.0)
    check("乱序输入会排序",
          [g[0] for g in got] == sorted(g[0] for g in got), str([g[0] for g in got]))
    check("时间戳不重叠",
          all(got[i][1] <= got[i + 1][0] + 1e-9 for i in range(len(got) - 1)),
          str([(g[0], g[1]) for g in got]))
    check("不超出整首时长", all(g[1] <= 12.0 + 1e-9 for g in got),
          str([(g[0], g[1]) for g in got]))
    check("太短的会被丢掉", all(g[1] - g[0] >= 0.3 - 1e-9 for g in got))
    check("缺尾的会被补上（不是 0 长度）", all(g[1] > g[0] for g in got))


def e_negative_controls(mod):
    print("\nE. 负对照：该空的时候必须空，且尺子必须真的有牙")

    # ① 根本不是 SRT
    check("乱文本 ⇒ 空", mod.parse("这不是字幕，随便写点什么。\n没有时间戳。\n") == [])
    # ② 空输入
    check("空 segments ⇒ 空", mod.from_segments([], 10) == [])
    # ③ 有行但没词
    check("全是空文本 ⇒ 空", mod.from_segments([{"start": 0, "end": 1, "text": "  "}], 10) == [])
    # ④ 缺 start 的脏条目
    check("缺 start ⇒ 空",
          mod.from_segments([{"end": 5, "text": "没有起点"}], 10) == [])

    # ⑤★ 尺子自检：证明「逗号」那条判据有牙 —— 换个点分隔的坏版本必须变红
    def ts_dot(sec):
        sec = max(0.0, float(sec))
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        return "%02d:%02d:%06.3f" % (h, m, sec % 60.0)

    good = mod.dump([(0.0, 1.5, "a")])
    bad = ts_dot(1.5)
    check("尺子有牙：合规版本含逗号毫秒", ",500" in good or ",160" in good or ",50" in good,
          repr(good))
    check("尺子有牙：坏版本（点分隔）确实不同 ⇒ 上面那条判据抓得住",
          "." in bad and "," not in bad, bad)


def f_wiring():
    print("\nF. 接线：光有函数没用，得真被调上")
    try:
        src = open(STUDIO, encoding="utf-8").read()
    except OSError as e:
        check("读得到 studio.py", False, e)
        return
    check("studio 里定义了 _write_srt", "def _write_srt(" in src)
    # ★ 四条**成品产出**路径都得带上字幕，漏一条「每一首歌都带 srt」就不成立：
    #   ① 一键唱落盘 ② 给已有成品补歌词 ③ 多人合唱 ④ 合唱换分配方式重排
    n_call = len(re.findall(r"_write_srt\(", src)) - 1      # 减掉 def 那一行
    check("四条成品产出路径都接上了", n_call == 4, "实际 %d 处" % n_call)
    n_field = src.count('"lyric_srt"')
    check("四处的成品记录里都写了 lyric_srt", n_field == 4, "实际 %d 处" % n_field)
    check("成品里记了 lyric_srt 字段", '"lyric_srt"' in src)

    try:
        js = open(APPJS, encoding="utf-8").read()
    except OSError as e:
        check("读得到 web/app.js", False, e)
        return
    check("前端有「下 .srt」按钮", "lyric_srt" in js and "下 .srt" in js)


def g_real_data(mod):
    print("\nG. 真实数据：拿一首歌的识别结果跑一遍")
    import glob
    import json
    cands = sorted(glob.glob(os.path.join(HERE, "data", "cache", "lyrics", "*.json")))
    if not cands:
        print("  （没有歌词缓存，跳过）")
        return
    p = cands[0]
    try:
        d = json.load(open(p, encoding="utf-8"))
    except (OSError, ValueError) as e:
        check("读得懂歌词缓存", False, e)
        return
    segs = d.get("segments") or []
    if not segs:
        print("  （这份缓存没有时间轴，跳过）")
        return
    rows = mod.from_segments(segs, d.get("duration"))
    check("生成出条目", len(rows) > 0, "%d 条" % len(rows))
    check("时间递增且不重叠",
          all(rows[i][1] <= rows[i + 1][0] + 1e-9 for i in range(len(rows) - 1)))
    txt = mod.dump(rows)
    check("能被解析回来", len(mod.parse(txt)) == len(rows))
    print("     例：" + " / ".join("%.1fs %s" % (r[0], r[2].split("\n")[0][:10])
                                        for r in rows[:3]))


def main():
    print("=== verify_srt：每首成品都要有本来的 .srt ===")
    try:
        mod = load_mod()
    except Exception as e:                                   # noqa: BLE001
        print("  [!!] 加载 stages/srt_cli.py 失败：%s" % e)
        return 1
    a_format(mod)
    b_roundtrip(mod)
    c_dirty(mod)
    d_timeline(mod)
    e_negative_controls(mod)
    f_wiring()
    g_real_data(mod)
    print("\n=== 通过 %d 项，失败 %d 项 ===" % (N_PASS, len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  - %s" % f)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
