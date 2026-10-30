# -*- coding: utf-8 -*-
r"""零显卡端到端：验证「每首成品旁边都有本来的 .srt」三条路径真的通。

跑法（cwd = VoiceStudio）：
    <venv python> -s tools/probe_srt_e2e.py

三条路：
  ① 歌自带字幕（源旁边有同名 .srt）⇒ 原样复制，一个字都不改；
  ② 识别过歌词（有 segments）⇒ 生成；
  ③ 没有歌词 ⇒ 不写文件（★ 绝不按行数均分时间轴）。
"""
import os
import shutil
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import studio                                            # noqa: E402

SR = 44100
N_OK = 0
FAILS = []


def check(name, ok, detail=""):
    global N_OK
    if ok:
        N_OK += 1
        print("  [ok] %s%s" % (name, ("  " + str(detail)) if detail else ""))
    else:
        FAILS.append(name)
        print("  [!!] %s  %s" % (name, detail))


def make_wav(path, secs=12.0):
    n = int(secs * SR)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(b"\x00\x00" * n)


def main():
    work = tempfile.mkdtemp(prefix="vs_srt_")
    try:
        # ---------- ① 自带字幕：原样复制 ----------
        print("\n① 歌自带字幕 ⇒ 原样复制（一个字都不改）")
        src = os.path.join(work, "my_song.mp3")
        open(src, "wb").write(b"ID3fake")
        own = os.path.join(work, "my_song.srt")
        body = "1\n00:00:01,000 --> 00:00:02,500\n自带的第一句\n\n" \
              "2\n00:00:02,500 --> 00:00:04,000\n自带的第二句\n"
        with open(own, "w", encoding="utf-8") as f:
            f.write(body)
        wav1 = os.path.join(work, "out_a.wav")
        make_wav(wav1, 12.0)
        got = studio._write_srt(wav1, "识别出来的词\n随便两句",
                                [{"start": 0.0, "end": 1.0, "text": "x"}], src)
        dst1 = os.path.join(work, "out_a.srt")
        check("生成了 .srt", got == dst1 and os.path.isfile(dst1), got)
        if os.path.isfile(dst1):
            same = open(dst1, encoding="utf-8").read() == body
            check("内容就是自带那份（没被识别结果盖掉）", same)
            check("自带字幕优先于识别歌词", "自带的第一句" in open(dst1, encoding="utf-8").read())

        # ---------- ② 有识别歌词 ⇒ 生成 ----------
        print("\n② 没有自带字幕 ⇒ 用识别出的歌词时间轴生成")
        wav2 = os.path.join(work, "out_b.wav")
        make_wav(wav2, 12.0)
        # ★ ②③ 必须换一个**旁边没有自带字幕**的源，否则会走 ① 的复制分支
        #   （早先三条共用一个 src，②③ 全被复制成自带字幕，看着像优先级坏了，
        #     其实是测试没隔离 —— 优先级本身是对的）。
        src2 = os.path.join(work, "no_sub_song.mp3")
        open(src2, "wb").write(b"ID3fake")
        segs = [{"start": 1.0, "end": 3.0, "text": "第一句"},
                {"start": 3.0, "end": 5.5, "text": "第二句"},
                {"start": 6.0, "end": 9.0, "text": "第三句"}]
        got2 = studio._write_srt(wav2, "第一句\n第二句\n第三句", segs, src2)
        dst2 = os.path.join(work, "out_b.srt")
        check("生成了 .srt", got2 == dst2 and os.path.isfile(dst2), got2)
        if os.path.isfile(dst2):
            txt = open(dst2, encoding="utf-8").read()
            check("毫秒用逗号", ",000" in txt and "." not in txt.split("\n")[1])
            check("条目数对", txt.strip().endswith("第三句"), repr(txt[-30:]))
            sys.path.insert(0, os.path.join(HERE, "stages"))
            import srt_cli
            rows = srt_cli.parse(txt)
            check("解析回来 3 条", len(rows) == 3, len(rows))
            check("时间不重叠",
                  all(rows[i][1] <= rows[i + 1][0] + 1e-9 for i in range(len(rows) - 1)))

        # ---------- ③ 没歌词 ⇒ 不写 ----------
        print("\n③ 既没自带字幕也没歌词 ⇒ 不写文件（不许硬凑）")
        wav3 = os.path.join(work, "out_c.wav")
        make_wav(wav3, 12.0)
        got3 = studio._write_srt(wav3, "", [], src2)
        check("返回空", got3 == "", repr(got3))
        check("没有生成 .srt 文件",
              not os.path.exists(os.path.join(work, "out_c.srt")))

        # ---------- ④ 负对照：自带字幕存在时，识别歌词不许盖上去 ----------
        print("\n④ 负对照：自带字幕在场，识别结果不许盖掉它")
        wav4 = os.path.join(work, "out_d.wav")
        make_wav(wav4, 12.0)
        studio._write_srt(wav4, "识别AB\n识别CD",
                          [{"start": 0.0, "end": 1.0, "text": "识别AB"},
                           {"start": 1.0, "end": 2.0, "text": "识别CD"}], src)
        t4 = open(os.path.join(work, "out_d.srt"), encoding="utf-8").read()
        check("自带字幕没被盖掉", "自带的第一句" in t4 and "识别AB" not in t4)

    finally:
        shutil.rmtree(work, ignore_errors=True)

    print("\n=== 通过 %d 项，失败 %d 项 ===" % (N_OK, len(FAILS)))
    for f in FAILS:
        print("  - %s" % f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
