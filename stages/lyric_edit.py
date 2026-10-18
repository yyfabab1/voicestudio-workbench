# -*- coding: utf-8 -*-
r"""
改词（保留原曲旋律，只把歌词换成新的）—— 走本机已部署的 YingMusic-Singer-Plus。

为什么必须是独立脚本 + 子进程：
  YingMusic 那套依赖 py3.10 + torch 2.7.1(cu128)，而 VoiceStudio / GPT-SoVITS 是
  另一套 runtime。两个环境的 torch 不能在同一个进程里 import，所以只能跨进程调。
  （踩过的坑：把依赖重的模块塞进服务进程，前面几步全正常、崩在完全不相干的一步，
  根因却只是启动方式。）

用法（由 VoiceStudio 调用）：
  <YingMusic 的 venv python> <本脚本> \
      --vocal 人声轨.wav --orig "原词|分句" --new "新词|分句" \
      --out 输出.wav [--nfe 16] [--chunk 28] [--seed 1234]

两个必须守的点，都是实测出来的：
  1. **必须分块**。整首歌（3~5 分钟）一次性喂进去显存扛不住，而且一次生成的
     时长越长越容易跑偏。28 秒一块是本机稳的值（沿用 run_jlc_split.py 的经验）。
  2. **每块必须对齐回输入块的长度**。模型会按新歌词的长短自己决定输出多长，
     直接 cat 会让总时长逐块漂移 —— 人声和伴奏**错位**是听感上最致命的问题，
     比音准差更要命。所以这里强制 crop / pad，宁可句尾少一点尾音。
     每块的 in/out 时长都会打出来，漂了多少是看得见的，不藏着。
"""
import argparse
import os
import sys
import time

YING_ROOT = r"D:\AHC\YingMusic-Singer-Plus-main"
_ESP = r"D:\AHC\espeak\eSpeak NG"


def _setup_env():
    """把 YingMusic 那套的运行环境配好 —— 顺序不能变：
    必须先在 import torch / local_infer 之前把 cwd 和 espeak 的环境变量设上，
    因为 local_infer 里用的是**相对路径** "ckpts/model.safetensors"。
    """
    os.environ.setdefault("HF_HOME", r"D:\AHC\hf")
    os.environ.setdefault("HF_HUB_CACHE", r"D:\AHC\hf")
    os.environ.setdefault("MPLCONFIGDIR", r"D:\AHC\tmp")
    os.environ.setdefault("TEMP", r"D:\AHC\tmp")
    os.environ.setdefault("TMP", r"D:\AHC\tmp")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    if os.path.isdir(_ESP):
        os.environ["PATH"] = _ESP + os.pathsep + os.environ.get("PATH", "")
        os.environ.setdefault("ESPEAK_DATA_PATH", os.path.join(_ESP, "espeak-ng-data"))
        os.environ.setdefault("PHONEMIZER_ESPEAK_LIBRARY",
                              os.path.join(_ESP, "libespeak-ng.dll"))
    os.chdir(YING_ROOT)
    sys.path.insert(0, YING_ROOT)


_setup_env()

import torch  # noqa: E402
import torchaudio  # noqa: E402

import local_infer as LI  # noqa: E402


def say(msg):
    print("[改词] %s" % msg, flush=True)


def split_lines(s):
    return [x.strip() for x in (s or "").replace("\n", "|").split("|") if x.strip()]


def fit_len(x, n):
    """把一维波形硬对齐到 n 个采样点：长了裁、短了补零。

    为什么宁可裁：总时长一旦漂，人声就和伴奏对不上，那是"整首都没法听"；
    而裁掉句尾几十毫秒只是"这一句收得急一点"。两害相权取前者。
    """
    c = x.shape[-1]
    if c == n:
        return x
    if c > n:
        return x[..., :n]
    pad = x.new_zeros(*x.shape[:-1], n - c)
    return torch.cat([x, pad], dim=-1)


def synth_one(model, path, orig, new, nfe, seed):
    a, sr = model(
        ref_audio_path=path,
        melody_audio_path=path,
        ref_text=orig,
        target_text=new,
        lrc_align_mode="sentence_level",
        sil_len_to_end=0.3,
        t_shift=0.5,
        nfe_step=nfe,
        cfg_strength=3.0,
        seed=seed,
    )
    return a.detach().to("cpu"), sr


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--vocal", required=True, help="分离好的人声干声")
    # 歌词既能直接传，也能传文件。**走文件是给用户用的那条路**：
    # 歌词里有中文、引号、换行，塞进 Windows 命令行会被转义搞坏，
    # 而且引号还能顺带把命令截断（等于让用户写的歌词能注入命令行）。
    ap.add_argument("--orig", default="", help="原歌词，| 分句")
    ap.add_argument("--new", default="", help="新歌词，| 分句")
    ap.add_argument("--orig-file", default="", help="原歌词文件（utf-8）")
    ap.add_argument("--new-file", default="", help="新歌词文件（utf-8）")
    ap.add_argument("--out", required=True)
    ap.add_argument("--nfe", type=int, default=16)
    ap.add_argument("--chunk", type=float, default=28.0)
    ap.add_argument("--seed", type=int, default=1234)
    a = ap.parse_args()

    if not os.path.isfile(a.vocal):
        say("人声轨不存在：%s" % a.vocal)
        return 2

    def read_lyric(inline, fpath):
        if fpath:
            if not os.path.isfile(fpath):
                say("歌词文件不存在：%s" % fpath)
                raise SystemExit(2)
            with open(fpath, "r", encoding="utf-8") as f:
                return f.read()
        return inline

    orig_lines = split_lines(read_lyric(a.orig, a.orig_file))
    new_lines = split_lines(read_lyric(a.new, a.new_file))
    if not orig_lines or not new_lines:
        say("原词或新词是空的，没东西可改")
        return 2
    if len(new_lines) != len(orig_lines):
        # 不硬性拦下：句数不等时按**比例**分配，宁可唱出来节奏略偏，
        # 也比直接失败好（用户往往是照着原词改几句，多一句少一句很常见）。
        say("注意：原词 %d 句 / 新词 %d 句，句数不一样，按长度比例分配到各块"
            % (len(orig_lines), len(new_lines)))

    t0 = time.time()
    wav, sr = torchaudio.load(a.vocal)
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    total = wav.shape[1]
    dur = total / float(sr)
    say("输入 %.1f 秒 @ %d Hz" % (dur, sr))

    n_chunks = max(1, int(round(dur / a.chunk))) if a.chunk > 0 else 1
    say("分 %d 块（每块约 %.0f 秒）" % (n_chunks, dur / n_chunks))

    tmpdir = os.path.join(r"D:\AHC\tmp", "lyric_edit")
    os.makedirs(tmpdir, exist_ok=True)

    model = None
    outs = []
    for ci in range(n_chunks):
        s = int(total * ci / n_chunks)
        e = int(total * (ci + 1) / n_chunks)
        if e - s < int(sr * 1.0):
            say("块 %d 太短（%.2f 秒），跳过" % (ci, (e - s) / float(sr)))
            continue
        seg = wav[:, s:e]
        sp = os.path.join(tmpdir, "c%d.wav" % ci)
        torchaudio.save(sp, seg, sr)

        # 歌词按块的**时间比例**切 —— 和旋律块一一对应，
        # 否则新词会被唱到别的乐句上去（听起来就是"词和调对不上"）。
        def take(lines):
            ls = int(len(lines) * ci / n_chunks)
            le = int(len(lines) * (ci + 1) / n_chunks)
            if le <= ls:
                le = min(len(lines), ls + 1)
            return "|".join(lines[ls:le]) or (lines[min(ci, len(lines) - 1)])

        o_sec = take(orig_lines)
        n_sec = take(new_lines)
        say("块 %d/%d  %.1f 秒  原[%s] -> 新[%s]"
            % (ci + 1, n_chunks, (e - s) / float(sr), o_sec, n_sec))

        if model is None:
            say("加载模型（第一次要几十秒，之后每块复用）")
            model = LI.load_model()
        a_out, sr_out = synth_one(model, sp, o_sec, n_sec, a.nfe, a.seed + ci)
        if sr_out != sr:
            a_out = torchaudio.functional.resample(a_out, sr_out, sr)
        if a_out.shape[0] > 1:
            a_out = a_out.mean(dim=0, keepdim=True)
        got = a_out.shape[-1]
        want = e - s
        if abs(got - want) > int(sr * 0.4):
            say("  ⚠ 这块输出 %.2f 秒 vs 输入 %.2f 秒（差 %+.2f 秒），已强制对齐"
                % (got / float(sr), want / float(sr), (got - want) / float(sr)))
        outs.append(fit_len(a_out, want))
        # 带 % 号：VoiceStudio 的进度条是从行里抠 `(\d{1,3})\s*%` 的，
        # 不带百分号它就抠不到（进度条会一直停在 0，看着像卡死）。
        print("LYRIC_PCT %d%%" % int(100.0 * (ci + 1) / n_chunks), flush=True)

    if not outs:
        say("一块都没跑出来")
        return 2

    cat = torch.cat(outs, dim=-1)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    torchaudio.save(a.out, cat, sr)
    print("LYRIC_DONE %.2f" % (cat.shape[-1] / float(sr)), flush=True)
    say("完成：%s  %.2f 秒（输入 %.2f 秒）  用时 %.0f 秒"
        % (a.out, cat.shape[-1] / float(sr), dur, time.time() - t0))
    if torch.cuda.is_available():
        say("峰值显存 %.0f MB" % (torch.cuda.max_memory_allocated() / 2 ** 20))
    return 0


if __name__ == "__main__":
    sys.exit(main())
