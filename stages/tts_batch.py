r"""
批量 TTS：加载一次权重，连续合成多句，逐句出声体检。

原来每合成一句就起一个新进程，第一句 25.9s、第二句 23.1s —— 里面大部分是
重新 import torch、重新加载 s2G/GPT 权重的时间，真正合成只有几秒。
改成一次加载、多句复用，第二句起就是纯推理耗时。

顺带把说话人相似度也并进来了：原来每句还要单独起一个 sim_cli 进程（3.7~3.8s），
现在 SV 模型和 TTS 权重一起加载，一句都不用额外起进程。

用法（cwd = 整合包根目录）：
    runtime\python.exe -s <本脚本> <包根目录> <GPT权重> <SoVITS权重> \
        <参考音频> <参考文本> <语种> <任务json> <结果json>

任务 json: [{"text": "要说的第一句", "out": "绝对路径.wav"}, ...]

================================================================================
长台词：为什么要"多跑一次取更完整的那次"（takes / alt_refs / base_spc）
================================================================================

实测（_probe_long.py / _probe_long2.py / _probe_long4.py，角色=阿塔尼斯）：
整段 147 字直接合成，识别 CER 41.5%（整块跳句）；上游按 <=28 字切好之后降到 8.2%。
但**切完剩下的丢字 100% 落在段的开头**（18 个片段：段首 18 字、段中 0 字、段尾 1 字），
而且是**随机的**：同一段同一参考跑两次，一次完整一次丢头 6 字；换个参考又能救回来。
文本层的守卫（段首补句号/逗号/省略号/空格/换行、temperature 0.75/0.90）**全部无效**。

那就只剩一条路：**同一段多跑几次，挑更完整的那一次**。判据用「秒/字」——
丢字必然让音频变短，而正常段的秒/字只在一个窄带里抖（实测 0.20~0.27）。
所以任务里可以带：

    "base_spc": 0.24      这个角色的正常秒/字（studio 从声学索引的中位语速算）
    "takes": 2            这一段最多试几次（默认 1 = 老行为，一个字节都不变）
    "alt_refs": [{"ref_wav":..., "ref_text":...}, ...]   第 2 次起换着用的参考

挑法：第一次合格（秒/字 >= 0.86 基准、且无静音/削幅/NaN）就直接用，不浪费第二次；
可疑才继续试，最后取「秒/字最接近基准」的那一次。**换参考而不是原地重掷** ——
同一段参考重跑实测给出的是同一个坏结果（掷同一个骰子）。

段拼回整句（join / gap）：
    上游把一句台词切成 N 段后，每段是个任务，都带同一个 "join" 路径；
    本脚本把同一 join 的段按任务顺序、按各段声明的 gap 补上气口，拼成一个文件。
    ★ 拼接必须在这里做：本进程已经有 numpy/soundfile，再起一个进程做这件事
      等于把整段音频写盘读盘两遍；而且拼接要用到每段的**原始采样**，
      只有这里拿得到。段文件仍留在磁盘上（用户想单听某一句也行）。

相似度只打印和记录，不判成败 —— 实测训练过的模型和没训的底模只差 0.03，
它分不出「像不像」，只能发现「音色完全跑偏」这种事故。
"""
import json
import math
import os
import sys
import time
import traceback

import numpy as np
import soundfile as sf

if len(sys.argv) < 9:
    print("参数不足")
    sys.exit(2)

PKG_ROOT = os.path.abspath(sys.argv[1])
GPT_PATH = sys.argv[2]
SOVITS_PATH = sys.argv[3]
REF_WAV = sys.argv[4]
REF_TEXT = sys.argv[5]
LANG = sys.argv[6]
TASKS = os.path.abspath(sys.argv[7])
RESULT = os.path.abspath(sys.argv[8])

os.chdir(PKG_ROOT)
if PKG_ROOT not in sys.path:
    sys.path.insert(0, PKG_ROOT)
sys.path.append(os.path.join(PKG_ROOT, "GPT_SoVITS", "eres2net"))

for tag, p in (("GPT权重", GPT_PATH), ("SoVITS权重", SOVITS_PATH), ("参考音频", REF_WAV)):
    if not os.path.exists(p):
        print("[err] %s 不存在: %s" % (tag, p))
        sys.exit(2)

# ★★ 必须在 import 之前把真正要用的权重告诉 inference_webui ★★
# 原因：GPT_SoVITS/inference_webui.py 有**模块级副作用** —— 导入那一刻就执行
#     gpt_path = os.environ.get("gpt_path",
#                               weight_data.get("GPT", {}).get(version, GPT_names[-1]))
#     change_gpt_weights(gpt_path)          # ← 真的 torch.load 一个 148MB 权重
# 也就是说：不设这个环境变量，导入阶段会加载 **weight.json 里上次用过的那个模型**
# （可能是别的角色、别的轮数），然后我们再用 change_gpt_weights 换成正确的 ——
# 白加载一次。更糟的是它把导入**变成了可能失败的一步**：
#   那个文件一旦被删/改名/挪走，tts_batch 在 import 阶段就
#   FileNotFoundError 退出，报的是「[err] 无法导入推理模块」——
#   完全看不出跟权重路径有关，看起来像"推理模块坏了"。
# 实测（2026-10-18，说话轮数 A/B 把旧 e60 挪走时暴露）：
#   B 臂第一次调用 23.8 秒后退出码 2，根因就是 weight.json 还指着被挪走的 e60，
#   而这一次要用的 e20 明明存在且合法。
# 真实场景同样会踩：重训后清理旧 ckpt / 手动整理权重目录，都会让说话功能整体瘫痪。
os.environ["gpt_path"] = GPT_PATH

import torch
import torchaudio

try:
    from GPT_SoVITS.inference_webui import change_gpt_weights, change_sovits_weights, get_tts_wav
except Exception:
    traceback.print_exc()
    print("[err] 无法导入推理模块")
    sys.exit(2)


def load16k(path):
    wav, sr = torchaudio.load(path)
    if wav.shape[0] > 1:
        wav = wav.mean(dim=0, keepdim=True)
    if sr != 16000:
        wav = torchaudio.functional.resample(wav, sr, 16000)
    return wav.squeeze(0)


# ---- 参考音频的合法时长：3~10 秒（GPT-SoVITS 的硬性要求）----
# 出处：GPT_SoVITS/TTS_infer_pack/TTS.py:917，16k 采样点数必须落在
# 48000~160000，否则直接 raise OSError("参考音频在3~10秒范围外")，**没有自动裁剪**。
#
# 上游（studio.pick_reference / studio._alt_refs）已经按这个区间硬过滤了。
# 这里再挡一次是**纵深防御**，因为 alt_refs 也可能来自历史任务或外部调用：
#   · 一条非法参考 ⇒ get_tts_wav 抛异常 ⇒ 这一次尝试作废；
#   · 若它是第 2/3 次（换参考那几次），兜底就等于没生效；
#   · 更坏的是历史上 `rec["tries"]` 那行会因 {"m": None} 直接 TypeError，
#     把整个任务带崩（2026-10-18 实测，已在下游修掉）。
# 代价只是一次尝试作废，收益是不让单条脏参考打断整句/整篇。
REF_MIN_SEC, REF_MAX_SEC = 3.0, 10.0


def ref_seconds(path):
    """参考音频时长（秒）。读不出来给 0.0（按不合格处理）。"""
    try:
        info = sf.info(path)
        if info.samplerate:
            return info.frames / float(info.samplerate)
    except Exception:
        pass
    return 0.0


def measure(audio, sr, text):
    """把一段音频量成几个数：时长、秒/字、rms、静音占比、削幅、NaN。

    ★ 归一化这一步不能省：get_tts_wav 返回的是 int16，直接算 RMS 会得到
      几千这种假数字（第一版就是这么错的）。
    """
    raw = np.asarray(audio)
    if np.issubdtype(raw.dtype, np.integer):
        d = raw.astype("float64") / float(np.iinfo(raw.dtype).max)
    else:
        d = raw.astype("float64")
    if d.ndim > 1:
        d = d.mean(axis=1)
    rms = float(np.sqrt(np.mean(d ** 2))) if d.size else 0.0
    nan = int(np.sum(~np.isfinite(d)))
    clip = int(np.sum(np.abs(d) > 0.999))
    secs = len(d) / float(sr)
    silent = float(np.mean(np.abs(d) < 1e-3)) if d.size else 1.0
    n_char = len([c for c in text if c.strip()])
    return {
        "sr": sr, "seconds": round(secs, 2), "rms": round(rms, 5),
        "rms_db": round(20 * math.log10(rms), 1) if rms > 0 else -999.0,
        "nan": nan, "clip": clip, "silent_ratio": round(silent, 3),
        "sec_per_char": round(secs / n_char, 3) if n_char else 0.0,
    }


def synth(text, ref_wav, ref_text, speed, temperature, top_p, top_k=20):
    gen = get_tts_wav(
        ref_wav_path=ref_wav,
        prompt_text=ref_text,
        prompt_language=LANG,
        text=text,
        text_language=LANG,
        speed=speed,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
    )
    return list(gen)[-1]


def trust(m, base_spc):
    """这一次合成能不能直接用（不能就再试）？

    两条：① 明显坏掉的（静音 / 削幅 / NaN / 时长离谱）直接不信；
         ② 带基准时，秒/字明显低于基准 = 有字没念出来 —— 也得再试。
    ★ 下限为什么是 0.85：实测丢 6/28 个字时秒/字掉到基准的 0.79，正常段则在
      0.74~1.05 之间抖 —— 两条区间有重叠，所以判"可疑"的门槛只能取在中间。
      多跑一次的代价是两三秒；漏掉一次丢字的代价是用户听到半句话没了。
      宁可多跑。（误跑的第二次也**不会**改变结果：见 pick_best 的 8% 规则。）
    """
    if m["nan"] or m["rms"] < 0.001 or m["silent_ratio"] > 0.5:
        return False
    if m["sec_per_char"] <= 0 or m["sec_per_char"] > 1.0:
        return False
    if base_spc and m["sec_per_char"] < base_spc * 0.85:
        return False
    return True


def pick_best(tries, base_spc):
    """在几次尝试里挑最完整的那一次。

    有基准就取「秒/字最接近基准」的（既拦短的也拦莫名拖长的）；
    没基准就取最长的 —— 丢字总是更短。

    ★ 但**两次差得不多时用第一次**：重试是为了救"丢了半句"这种事故，
      不是为了在正常范围里换个说法。差 8% 以内换成另一次，只会让同一句
      写两遍听起来都不一样 —— 用户点两次得到两条不同的话，那是倒退。
    """
    first = tries[0]
    if len(tries) == 1:
        return first
    if base_spc:
        best = min(tries, key=lambda t: abs(t["m"]["sec_per_char"] - base_spc))
    else:
        best = max(tries, key=lambda t: t["m"]["seconds"])
    if best is first:
        return first
    a, b = first["m"]["seconds"], best["m"]["seconds"]
    if a > 0 and abs(b - a) / a <= 0.08:
        return first
    return best


def main():
    tasks = json.load(open(TASKS, encoding="utf-8"))
    t0 = time.time()
    change_gpt_weights(gpt_path=GPT_PATH)
    change_sovits_weights(sovits_path=SOVITS_PATH)
    print("权重加载完成 %.1fs" % (time.time() - t0))
    sys.stdout.flush()

    # SV 只用来给个参考分，加载失败不影响合成
    sv = None
    try:
        from GPT_SoVITS.sv import SV

        sv = SV("cuda" if torch.cuda.is_available() else "cpu", True)
        ref_emb = None
        w = load16k(REF_WAV)
        with torch.no_grad():
            ref_emb = sv.compute_embedding3(w.unsqueeze(0).cuda()).float().cpu().numpy()[0]
    except Exception:
        print("[warn] 说话人模型加载失败，跳过相似度")
        sv = None

    # 参考音频的说话人向量缓存：换一次参考就要重算一次 SV embedding，
    # 但同一段参考被多句复用时不该反复算（一句 30~40ms，几十句就看得出来了）。
    emb_cache = {}

    results = []
    joins = {}          # join 路径 -> [(task_index, audio, sr, gap)]
    for i, task in enumerate(tasks):
        text = str(task.get("text", "")).strip()
        out_wav = os.path.abspath(task["out"])
        if not text:
            continue
        # ★ 每句可以有自己的参考音频 / 参数（「按句控语气」用）。没给就用全局那套，
        #   老调用方的行为一个字节都不许变。
        ref_wav = str(task.get("ref_wav") or REF_WAV)
        ref_text = str(task.get("ref_text") or REF_TEXT)
        speed = float(task.get("speed") or 1.0)
        temperature = float(task.get("temperature") or 0.6)
        top_p = float(task.get("top_p") or 0.6)
        top_k = int(task.get("top_k") or 20)      # 20 = GPT-SoVITS 自己的默认值
        # ★ 长台词那套：最多试几次 + 备选参考 + 这个角色的正常秒/字
        takes = max(1, int(task.get("takes") or 1))
        base_spc = float(task.get("base_spc") or 0.0)
        alts = [a for a in (task.get("alt_refs") or []) if a and a.get("ref_wav")]
        rec = {"index": i, "text": text, "out": out_wav, "ok": False,
               "ref_wav": os.path.basename(ref_wav)}
        print("")
        print("[%d/%d] %s" % (i + 1, len(tasks), text))
        if ref_wav != REF_WAV or speed != 1.0 or temperature != 0.6:
            print("    参考 %s  语速 x%.2f  温度 %.2f"
                  % (os.path.basename(ref_wav), speed, temperature))
        sys.stdout.flush()

        tries = []
        for k in range(takes):
            rw, rt = ref_wav, ref_text
            if k > 0 and alts:
                a = alts[(k - 1) % len(alts)]
                rw, rt = str(a["ref_wav"]), str(a.get("ref_text") or "")
            # ★ 纵深防御：非法时长的参考直接跳过这一次，别让 get_tts_wav 抛异常。
            #   宁可少试一次（效果略差），也不让一条脏参考把整句/整篇带崩。
            _rd = ref_seconds(rw)
            if not (REF_MIN_SEC <= _rd <= REF_MAX_SEC):
                print("    第 %d 次：跳过 —— 参考 %s 时长 %.2fs 不在 %.0f~%.0f 秒"
                      % (k + 1, os.path.basename(rw), _rd, REF_MIN_SEC, REF_MAX_SEC))
                sys.stdout.flush()
                tries.append({"m": None, "audio": None,
                              "ref": os.path.basename(rw),
                              "error": "参考时长 %.2fs 不在 %.0f~%.0f 秒"
                                       % (_rd, REF_MIN_SEC, REF_MAX_SEC)})
                continue
            try:
                t1 = time.time()
                syn_sr, audio = synth(text, rw, rt, speed, temperature, top_p, top_k)
                m = measure(audio, syn_sr, text)
                m["synth_sec"] = round(time.time() - t1, 2)
                tries.append({"m": m, "audio": audio, "ref": os.path.basename(rw)})
            except Exception:
                traceback.print_exc()
                tries.append({"m": None, "audio": None, "ref": os.path.basename(rw)})
                continue
            good = trust(m, base_spc)
            tag = "OK" if good else "可疑"
            print("    第 %d 次：%.2fs  %.3f 秒/字  参考 %s  %s"
                  % (k + 1, m["seconds"], m["sec_per_char"],
                     os.path.basename(rw), tag))
            sys.stdout.flush()
            if good:
                break

        live = [t for t in tries if t["m"] is not None]
        if not live:
            # 把**具体原因**带出来。以前这里只有一句"合成失败"，用户拿不到任何线索，
            # 而真实原因（参考时长越界 / 合成抛异常）就在手里。
            errs = [t.get("error") for t in tries if t.get("error")]
            rec["error"] = "合成失败" + ("：" + str(errs[0]) if errs else "")
            results.append(rec)
            continue
        best = pick_best(live, base_spc)
        sr, audio, m = best["m"]["sr"], best["audio"], best["m"]
        if len(tries) > 1:
            rec["takes_used"] = len(tries)
            rec["picked"] = best["ref"]
            if best is not tries[0]:
                print("    ★ 换了参考重来一次：用「%s」这一次" % best["ref"])
                sys.stdout.flush()
        rec.update(m)
        rec["ref_wav"] = best["ref"]
        if len(tries) > 1:
            # ★ 必须过滤掉**失败的那几次尝试**：异常分支塞进来的是
            #   {"m": None, "audio": None, ...}，而 `dict(None, ref=...)` 会抛
            #   TypeError: 'NoneType' object is not iterable。
            # 触发条件很窄但很致命：至少一次异常 + 至少一次成功 + 总尝试 > 1。
            # 而 tries>1 只在长台词/换参考重试路径上开启 ⇒ 短句永远碰不到，
            # **长句一旦"第一次可疑或失败、第二次成功"就整个任务崩掉**，
            # 连成品 wav 都不写（用户看到的是"没有拼好的成品"）。
            rec["tries"] = [
                dict(t["m"], ref=t["ref"]) if t["m"] is not None
                else {"ref": t["ref"], "ok": False,
                      "error": "这一次合成抛异常（已记录，未参与选优）"}
                for t in tries]

        os.makedirs(os.path.dirname(out_wav), exist_ok=True)
        sf.write(out_wav, audio, sr)

        jp = task.get("join")
        gap = float(task.get("gap") or 0.0)
        if jp:
            jp = os.path.abspath(jp)
            joins.setdefault(jp, []).append((i, audio, sr, gap))

        if sv is not None:
            try:
                # ★ 相似度必须跟**这一句自己的参考音频**比。
                #   用全局那个的话，换了参考的句子相似度会假性变低，
                #   看着像"音色跑了"，其实只是比对对象不对。
                #   重试换过参考时，要比的是**最后真正用的那一段**。
                cands = [task.get("ref_wav") or REF_WAV] + \
                        [a.get("ref_wav") for a in alts]
                key = next((p for p in cands
                            if os.path.basename(p or "") == best["ref"]), cands[0])
                if key not in emb_cache:
                    w0 = load16k(key)
                    with torch.no_grad():
                        emb_cache[key] = sv.compute_embedding3(
                            w0.unsqueeze(0).cuda()).float().cpu().numpy()[0]
                r0 = emb_cache[key]
                w = load16k(out_wav)
                with torch.no_grad():
                    e = sv.compute_embedding3(w.unsqueeze(0).cuda()).float().cpu().numpy()[0]
                a = r0 / (np.linalg.norm(r0) + 1e-12)
                b = e / (np.linalg.norm(e) + 1e-12)
                rec["similarity"] = round(float(np.dot(a, b)), 4)
            except Exception:
                pass

        reasons = []
        if m["nan"] > 0:
            reasons.append("含 NaN %d 个" % m["nan"])
        if m["rms"] < 0.001:
            reasons.append("近乎静音 (RMS %.5f)" % m["rms"])
        if m["silent_ratio"] > 0.5:
            reasons.append("静音占比 %.1f%% 过高" % (m["silent_ratio"] * 100))
        if m["sec_per_char"] > 1.0:
            reasons.append("语速异常 %.3f 秒/字（正常 0.15~0.5），多半是训练轮数不够"
                           % m["sec_per_char"])
        if base_spc and m["sec_per_char"] < base_spc * 0.85:
            # ★ 试过几次还是偏短 —— 记 warn，但**不判失败**。
            #   为什么：静音/NaN/削幅是"这段音频坏了"，判失败理所当然；
            #   偏短只是"可能有字没念出来"，音频本身是好的、能听能用。
            #   判失败等于整句话都不交付 —— 用户会得到"什么都没有"，
            #   比"少半句但能听"更糟。实测「我等了那么久」这 6 个字在多个上下文里
            #   都会稳定被吞（换参考/改温度/改语速都救不回），把它变成硬失败
            #   只会让"长台词"这个功能整个不可用。
            #   诚实的做法是交付 + 明说 —— studio 会把这条汇总到日志里。
            rec["warn"] = ("语速 %.3f 秒/字，比这个角色的正常值 %.3f 低 %.0f%%，"
                           "可能有字没念出来（已换参考试过 %d 次）"
                           % (m["sec_per_char"], base_spc,
                              100 * (1 - m["sec_per_char"] / base_spc), len(tries)))
        if m["clip"] > 0:
            _w = "有 %d 个采样点削幅" % m["clip"]
            rec["warn"] = (rec.get("warn") + "；" + _w) if rec.get("warn") else _w
        if reasons:
            rec["error"] = "；".join(reasons)
        else:
            rec["ok"] = True
        verdict = "OK" if rec["ok"] else "FAIL: " + rec["error"]
        if rec.get("warn"):
            verdict += "  ⚠ " + rec["warn"]
        print("    %.2fs  RMS %.5f(%s dB)  削幅 %d  %.3f 秒/字  相似度 %s  %s"
              % (m["seconds"], m["rms"], m["rms_db"], m["clip"], m["sec_per_char"],
                 rec.get("similarity", "-"), verdict))
        sys.stdout.flush()
        results.append(rec)

    # ---------------- 段拼回整句 ----------------
    for jp, items in joins.items():
        items.sort(key=lambda x: x[0])
        sr0 = items[0][2]
        blocks, gaps = [], []
        for _idx, au, sr, gap in items:
            d = np.asarray(au)
            if np.issubdtype(d.dtype, np.integer):
                d = d.astype("float64") / float(np.iinfo(d.dtype).max)
            else:
                d = d.astype("float64")
            if d.ndim > 1:
                d = d.mean(axis=1)
            blocks.append(d)
            gaps.append(float(gap or 0.0))
            if gap and gap > 0:
                blocks.append(np.zeros(int(sr0 * float(gap)), dtype="float64"))
        y = np.concatenate(blocks) if blocks else np.zeros(1, dtype="float64")
        peak = float(np.max(np.abs(y))) if y.size else 0.0
        if peak > 1.0:                       # 只在真的会削幅时才缩放，别动正常音量
            y = y / peak
        os.makedirs(os.path.dirname(jp), exist_ok=True)
        sf.write(jp, y.astype("float32"), sr0)
        total = len(y) / float(sr0)
        print("")
        print("拼回整句：%s  %d 段  %.2f 秒（段间气口 %s）"
              % (jp, len(items), total, "/".join("%.2f" % g for g in gaps)))
        for seg_no, (idx, au, sr, gap) in enumerate(items):
            for r in results:
                if r["index"] == idx:
                    # 每条段结果都指回整句文件 —— 上游按 join_path 去重后，
                    # 对外就只看到"一句话一个文件"，段文件留在盘上是给复听的。
                    r["join_path"] = jp
                    r["join_seconds"] = round(total, 2)
                    r["seg_no"] = seg_no
                    r["seg_count"] = len(items)
        sys.stdout.flush()

    json.dump(results, open(RESULT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    bad = [r for r in results if not r["ok"]]
    print("")
    print("TTS_DONE %d 句，成功 %d，失败 %d" % (len(results), len(results) - len(bad), len(bad)))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
