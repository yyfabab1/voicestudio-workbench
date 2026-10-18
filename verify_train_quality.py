# -*- coding: utf-8 -*-
"""第 30 关：训练质量（开训前素材体检 / 轮数推荐 / v2 架构 / 参考音频挑选）

由头（老板 2026-10-16）：「继续优化提升，参考文献等等提升训练质量和唱歌说话质量」。

本关钉的是**训练侧**那一半。判断来源全部写清，方便后人复核：

  · 数据质量 > 数据数量 > 训练轮数 —— RVC 官方 FAQ "How do I improve training
    quality?" 与多篇实践一致。具体阈值见 studio.py 里 TRAIN_* 那组常量。
  · 轮数不是越多越好 —— 过训会出机械感（官方 FAQ 原话 "Over-training causes
    robotic artifacts… roll back to an earlier checkpoint"）。
  · v2 架构质量更好但更吃显存，且**只有 32k/48k**（没有 40k）。

★ 本关所有素材都是**现场合成**的（标准库 wave，不依赖运行时残留），
  所以每次跑都能真红也能真绿 —— 负对照不靠外部数据（铁律）。
★ 跑了真角色的部分只有 D 段的"v2 底模能不能加载"，且它会先检查文件在不在，
  不在就跳过而不是判红（底模是可选下载物，不该让没下的人跑门禁必红）。
"""
import os
import sys
import math
import wave
import array
import random
import shutil
import tempfile
import json

sys.path.insert(0, os.getcwd())
import studio

PASS = 0
FAILS = []


def sec(t):
    print("\n" + "-" * 62)
    print(t)
    print("-" * 62)


def check(name, ok, detail=""):
    global PASS
    if ok:
        PASS += 1
        print("  PASS  %s%s" % (name, ("  — %s" % detail) if detail else ""))
    else:
        FAILS.append(name)
        print("  FAIL  %s%s" % (name, ("  — %s" % detail) if detail else ""))


# ---------------------------------------------------------------- 造素材
def make_wav(path, secs=5.0, sr=16000, sig=0.15, noise=0.00003, clip=False,
             seed=1):
    """合成一段干声：前 30% 只有噪声（模拟句间空隙），后面是歌声。

    ★ 为什么要留那 30% 的空隙：体检里的"底噪地板"取的是**能量最低的 10% 帧**，
      也就是"这个人没在唱"的时候。整段都在唱的话，量出来的不是底噪而是歌声的
      low 段 —— 那这条判据就没有分辨力了（铁律：尺子要能量得出差异）。
    ★ 为什么默认 16k 而不是 32k：抽样那条用例要造 190 段，纯 Python 逐样本合成
      在 32000Hz 下要跑将近一分钟。16k 对"时长 / 响度 / 底噪 / 削波"这些判据
      毫无影响（我们造的是 220Hz 正弦），却能把整关快一倍。
    """
    rnd = random.Random(seed)
    n = int(sr * secs)
    out = array.array("h")
    for i in range(n):
        if i < n * 0.3:
            v = noise * (rnd.random() * 2 - 1) * 2
        else:
            v = sig * math.sin(2 * math.pi * 220.0 * i / sr)
            v += noise * (rnd.random() * 2 - 1) * 2
        if clip:
            v *= 8.0
        iv = int(max(-32767, min(32767, v * 32768)))
        out.append(iv)
    w = wave.open(path, "wb")
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(sr)
    w.writeframes(out.tobytes())
    w.close()


def build_role(tmp, specs, name="体检素材"):
    """在临时目录里建一个角色 + 它的切片。specs = [(secs, sig, noise, clip), ...]

    返回 (rid, slices_dir)。
    """
    rdir = os.path.join(tmp, "roles")
    os.makedirs(rdir, exist_ok=True)
    rid = "rq_probe"
    rd = os.path.join(rdir, rid)
    sd = os.path.join(rd, "slices")
    os.makedirs(sd, exist_ok=True)
    segs = []
    for i, (secs, sig, noise, clip) in enumerate(specs):
        fn = "s%03d.wav" % i
        make_wav(os.path.join(sd, fn), secs=secs, sig=sig, noise=noise,
                 clip=clip, seed=i)
        segs.append({"file": fn, "dur": secs, "voiced": 0.8, "text": "测试"})
    db = {"roles": [{"id": rid, "name": name, "segments": segs,
                     "voice": {"center": None, "n": 0}}]}
    with open(os.path.join(tmp, "roles.json"), "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False)
    return rid, sd


def audit_with(tmp, specs):
    """在隔离目录里跑一次体检，返回 audit dict。"""
    _oj, _od = studio.ROLES_JSON, studio.ROLES_DIR
    studio.ROLES_JSON = os.path.join(tmp, "roles.json")
    studio.ROLES_DIR = os.path.join(tmp, "roles")
    try:
        rid, _ = build_role(tmp, specs)
        return studio.audit_train_data(rid)
    finally:
        studio.ROLES_JSON, studio.ROLES_DIR = _oj, _od


def has(a, keyword, level=None):
    if not a:
        return False
    for lv, m in a["issues"]:
        if keyword in m and (level is None or lv == level):
            return True
    return False


# ---------------------------------------------------------------- A 常量
def a_consts():
    sec("A. 常量对账：阈值要有出处，且和本机对得上")
    check("单段时长区间符合文献（3~15 秒）",
          studio.TRAIN_SEG_MIN == 3.0 and studio.TRAIN_SEG_MAX == 15.0,
          "%.0f~%.0f" % (studio.TRAIN_SEG_MIN, studio.TRAIN_SEG_MAX))
    check("总时长区间符合文献（10~50 分钟）",
          studio.TRAIN_TOTAL_MIN == 600.0 and studio.TRAIN_TOTAL_MAX == 3000.0,
          "%.0f~%.0f 分钟" % (studio.TRAIN_TOTAL_MIN / 60, studio.TRAIN_TOTAL_MAX / 60))
    check("底噪判据符合文献（<-50 合格 / >-40 不合格）",
          studio.TRAIN_FLOOR_WARN == -50.0 and studio.TRAIN_FLOOR_BAD == -40.0,
          "%.0f / %.0f dB" % (studio.TRAIN_FLOOR_WARN, studio.TRAIN_FLOOR_BAD))
    # ★ 这条最容易被人"照文献改成 44100"，然后所有老角色满屏报警
    check("★ 采样率阈值按**本机实测**校准，不是文献的 44100",
          studio.TRAIN_SR_MIN == 32000,
          "%.0f（本机切片工具写死输出 32k，按 44100 判是对着改不了的东西报警）"
          % studio.TRAIN_SR_MIN)
    check("体检有抽样上限（别把开训拖慢）", studio.TRAIN_AUDIT_MAX >= 30,
          "%d 段" % studio.TRAIN_AUDIT_MAX)

    sec("A2. v2 架构的硬约束（不是我们选的，是 RVC 自己的）")
    check("v2 **没有 40k**（configs/v2 下只有 32k/48k）",
          "40k" not in studio.RVC_ARCH_SRS["v2"],
          "v2 可用：%s" % ", ".join(studio.RVC_ARCH_SRS["v2"]))
    check("v1 仍然支持 40k（现有模型不受影响）",
          "40k" in studio.RVC_ARCH_SRS["v1"])
    check("v2 配套采样率是 48k", studio.RVC_ARCH_SR["v2"] == "48k")
    check("★ v2 用**独立 exp 名**（否则 config.json 被覆盖、旧断点必 mismatch）",
          studio.RVC_ARCH_EXP_SUFFIX.get("v2") and
          studio.RVC_ARCH_EXP_SUFFIX.get("v1") == "",
          "v2 后缀 %r" % studio.RVC_ARCH_EXP_SUFFIX.get("v2"))
    check("v2 的批比 v1 小（官方说 v2 更吃显存，本机 8GB）",
          studio.RVC_ARCH_BS.get("v2", 9) < studio.RVC_ARCH_BS.get("v1", 0),
          "v1=%d v2=%d" % (studio.RVC_ARCH_BS.get("v1"), studio.RVC_ARCH_BS.get("v2")))
    check("默认架构仍是 v1（不偷偷改掉现有行为）",
          studio.RVC_ARCH_DEFAULT == "v1", studio.RVC_ARCH_DEFAULT)


# ---------------------------------------------------------------- B 体检逻辑
def b_audit():
    sec("B. 素材体检：每一类问题都要能被量出来（合成素材，逐类验证）")
    tmp = tempfile.mkdtemp(prefix="_vq_")
    try:
        # ① 干净素材：应该没问题
        #   ★ 段数必须凑够 10 分钟（121×5s=605s）：否则"素材太少"自己就会报 warn，
        #     把"干净素材应该 ok"这条判据污染掉（第一版就栽在这，看着像实现有问题）。
        good = audit_with(tmp, [(5.0, 0.15, 0.00003, False)] * 121)
        check("干净素材（5 秒 / 响度一致 / 底噪 -90dB / 10 分钟以上）判 ok",
              good and good["verdict"] == "ok",
              (good or {}).get("verdict"))
        if good:
            check("  底噪地板量得准（造的是 -90dB 量级）",
                  good["floor_med"] < -60,
                  "%.0f dB" % good["floor_med"])

        # ② 底噪不合格
        noisy = audit_with(tmp, [(5.0, 0.15, 0.03, False)] * 12)
        check("★ 底噪高的素材必须判 bad",
              noisy and noisy["verdict"] == "bad" and has(noisy, "底噪", "bad"),
              "verdict=%s" % (noisy or {}).get("verdict"))
        check("  且底噪地板量出来确实 > -40dB",
              noisy and noisy["floor_med"] > studio.TRAIN_FLOOR_BAD,
              "%.0f dB" % (noisy or {}).get("floor_med", 0))

        # ③ 响度忽大忽小
        #    前一半 sig=0.02（很轻）、后一半 sig=0.5（很响）⇒ 跨度远超 6dB
        mixed = [(5.0, 0.02, 0.00003, False)] * 6 + [(5.0, 0.5, 0.00003, False)] * 6
        loud = audit_with(tmp, mixed)
        check("★ 响度忽大忽小必须判出来", loud and has(loud, "响度跨度", "warn"),
              "跨度 %.1f dB" % (loud or {}).get("rms_span", 0))

        # ④ 削波
        clipped = audit_with(tmp, [(5.0, 0.15, 0.00003, True)] * 6 +
                                  [(5.0, 0.15, 0.00003, False)] * 6)
        check("★ 削波（音量顶到头）必须判 bad",
              clipped and has(clipped, "削波", "bad"))

        # ⑤ 总时长太少
        few = audit_with(tmp, [(5.0, 0.15, 0.00003, False)] * 3)
        check("素材太少（不到 10 分钟）要提醒", few and has(few, "分钟"),
              "%.1f 分钟" % ((few or {}).get("total_secs", 0) / 60.0))

        # ⑥ 短段占比高
        #   ★ 段长必须落在 2.0~3.0 之间：audit_train_data 入口有 `dur >= 2.0` 的
        #     硬过滤（那是"能不能训"的门槛），低于 2 秒的段根本进不了体检，
        #     造 1.5 秒的段就永远测不到这条判据（第一版就造错了）。
        short = [(2.5, 0.15, 0.00003, False)] * 10 + [(5.0, 0.15, 0.00003, False)] * 2
        sh = audit_with(tmp, short)
        check("★ 短段占比高要提醒（门槛 10%，防抽样抖动误报）",
              sh and has(sh, "短于"), "占比 %.0f%%" %
              (100.0 * 10 / 12 if sh else 0))

        # ⑦ 低于 44.1k 的素材只报 info，不报 bad（老角色改不了，不该报警）
        check("★ 采样率偏低只报 info（不是 bad）—— 改不了的东西不许报警",
              good and has(good, "高频上限", "info")
              and not has(good, "高频上限", "bad"))

        # ⑧ 抽样
        many = audit_with(tmp, [(5.0, 0.15, 0.00003, False)] * (studio.TRAIN_AUDIT_MAX + 40))
        check("段数超过上限时**抽样**（否则开训被拖慢）",
              many and many["sampled"] and many["n"] <= studio.TRAIN_AUDIT_MAX,
              "n=%s sampled=%s" % ((many or {}).get("n"), (many or {}).get("sampled")))
        check("  抽样后总时长是**外推**的（不是只算抽到的那几段）",
              many and many["total_secs"] > (many["n"] * 5.0) * 1.2,
              "%.0f 秒（抽到 %d 段 × 5 秒 = %d 秒）"
              % ((many or {}).get("total_secs", 0), (many or {}).get("n", 0),
                 (many or {}).get("n", 0) * 5))

        # ⑨ 坏输入不抛
        _oj, _od = studio.ROLES_JSON, studio.ROLES_DIR
        studio.ROLES_JSON = os.path.join(tmp, "roles.json")
        studio.ROLES_DIR = os.path.join(tmp, "roles")
        try:
            check("角色不存在时返回 None（不抛、不把训练挡住）",
                  studio.audit_train_data("no_such_role") is None)
        finally:
            studio.ROLES_JSON, studio.ROLES_DIR = _oj, _od
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- C 轮数推荐
def c_epochs():
    sec("C. 轮数推荐：素材越多轮数越要少（社区实测表：过训会出机械感）")
    r = studio.recommend_rvc_epochs
    # ★ 2026-10-18 按**社区实测表**重标（旧表整体偏高，且"素材多反而训得多"那几档
    #   是反的）。三份独立来源给出同一张表：RVC 2.3 实测文章 / voicechanger.live
    #   训练指南 / Applio 社区配置。代码注释里有完整引用。
    #   ⚠ 这是**规格变更**（判据跟着规格走），不是"改断言迁就实现" —— 依据写在
    #     `recommend_rvc_epochs` 的 docstring 里，来路可查。
    cases = [
        (120, 30, 400),      # 2 分钟   <3min 档
        (600, 120, 200),     # 10 分钟  10~20min 甜点区
        (1500, 300, 120),    # 25 分钟  20~40min（大帝实测 29.6 分钟就在这档）
        (2700, 540, 75),     # 45 分钟  40~60min
    ]
    for secs, n, want in cases:
        got, why = r(secs, n)
        check("%.0f 分钟 / %d 段 → 推荐 %d 轮" % (secs / 60.0, n, want),
              got == want, "实际 %d（%s）" % (got, why))
    # 分档边界的另一半：这几档只有边界才验得到，光测中间的样本会漏
    for secs, want in ((240, 350), (900, 200), (3600, 55)):
        got, _ = r(secs, 0)
        check("%.0f 分钟 → %d 轮（边界档）" % (secs / 60.0, want), got == want,
              "实际 %d" % got)
    check("★ 素材越多推荐轮数越少（单调）",
          all(r(t, 10)[0] >= r(t * 2, 10)[0] for t in (120, 600, 1500, 2700)))
    # 负对照：把表改回旧值必须变红，否则这条判据量不出东西
    _old = [(1500, 200), (2700, 150)]
    stale = [w for (s, w) in _old if r(s, 0)[0] == w]
    check("★ 负对照：旧表的值（25min→200 / 45min→150）不会被当成新表",
          not stale, "仍在用旧值：%s" % stale)
    check("时长量不出来时能用段数兜底（不返回 0）", r(0, 100)[0] > 0,
          "%d 轮" % r(0, 100)[0])
    check("坏输入不抛", r(None, 0)[0] > 0 and r("x", 0)[0] > 0)


# ---------------------------------------------------------------- D v2 底模
def d_v2():
    sec("D. v2/48k 底模：真加载一遍（「文件在」不等于「用得上」）")
    gp, dp = studio.rvc_pretrained_paths("v2", "48k")
    here = studio.rvc_pretrained_here("v2", "48k")
    if not (here[0] and here[1]):
        print("  SKIP  v2 底模没下载（%s）⇒ 跳过真加载，不判红" %
              os.path.basename(gp))
        return
    ok, msg = studio.check_rvc_pretrained({"dir": ".", "id": "vq",
                                           "_logfile": "_vq_pt.log"}, "v2", "48k")
    check("★ v2/48k 底模能真的加载进 v2 架构",
          ok and "missing=0" in msg and "unexpected=0" in msg, msg[:120])
    check("v2 底模放在 pretrained_v2/（不是 v1 的 pretrained/）",
          "pretrained_v2" in gp)

    # ---- 48k 的批大小必须比 32k 更小 ----
    # 实测（本机 8GB、v2/48k、bs=2）：开训后 GPU 99%、显存 7755/8151 MiB，
    # 但 tfevents **十分钟一个字没写**、轮次一行都没出 —— 显存打满后的空转。
    # 这类卡死没有报错，只能干等，所以批大小必须按采样率收紧。
    cap = getattr(studio, "RVC_SR_BS_CAP", {})
    check("★ 48k 有独立的批上限且小于 v2 默认（否则显存不够时会静默卡死而不是 OOM）",
          cap.get("48k") is not None
          and cap["48k"] < studio.RVC_ARCH_BS.get("v2", 2),
          "48k→%s / v2→%s" % (cap.get("48k"), studio.RVC_ARCH_BS.get("v2")))
    # 但**写死 bs=1 是错的**：48k 的限制本质是显存压力。实测同一台机器
    # 被占 1.8GB 时 48k/bs=2 只要 90 秒/轮（15 轮 22 分钟），写死 1 就是白慢 5 倍。
    # 所以必须有「先量剩余显存、够就放回默认」这一步。
    check("★ 48k 的批上限是**自适应**的（显存够就放回默认，不写死）",
          hasattr(studio, "_vram_free_mb")
          and getattr(studio, "RVC_48K_BS2_FREE_MB", 0) > 0,
          "阈值=%s MB" % getattr(studio, "RVC_48K_BS2_FREE_MB", None))
    try:
        _v = studio._vram_free_mb()
        _vok = (_v is None) or isinstance(_v, int)
        _vmsg = str(_v)
    except Exception as e:
        _vok, _vmsg = False, "%s: %s" % (type(e).__name__, str(e)[:60])
    check("显存探测：有数就给 int、拿不到给 None，都不许抛异常（探测失败不能带死训练）",
          _vok, _vmsg)
    _src2 = open("studio.py", encoding="utf-8").read()
    _i2 = _src2.find("def train_rvc")
    _b2 = _src2[_i2:_i2 + 22000] if _i2 >= 0 else ""
    check("训练命令的 -bs 真的用了「架构与采样率取小」（不是只定义没用）",
          "min(RVC_ARCH_BS.get(arch, 4), _cap or 99)" in _b2)
    check("★ 显存够时会把采样率上限放开（否则自适应等于没写）",
          "_free >= RVC_48K_BS2_FREE_MB" in _b2 and "_cap = None" in _b2)


# ---------------------------------------------------------------- E 参考音频
def e_ref():
    sec("E. 参考音频挑选：声纹复核**默认关**，且关它是有实测依据的")
    src = open("studio.py", encoding="utf-8").read()
    i = src.find("def pick_reference")
    # ★ 按**函数边界**取体，不要用固定的 `src[i:i+4000]`：
    #   那个魔法数字会随着函数里加注释而悄悄截断，于是"try/except 兜住"这类
    #   往后才出现的断言会莫名其妙判红（2026-10-18 就这么红过一次 ——
    #   给 pick_reference 加了参考时长护栏的注释，函数变长，except 被挤出窗口）。
    #   判据读到的是"窗口"，不是"函数"，这种红跟产品无关，纯属尺子脆。
    _j = src.find("\ndef ", i + 1) if i >= 0 else -1
    body = src[i:_j] if i >= 0 and _j > i else (src[i:] if i >= 0 else "")

    # ★ 这条判据的期望值被实测**反过来过一次**，别照"看起来对"改回去：
    #   原注释承诺"用声纹挑最典型的段"，补上实现后 verify_tone 的负对照当场判红
    #   （抖动 1.7→21.3 Hz、语气差 55.1→30.9 Hz）。所以现在是"必须默认关"。
    check("★ 声纹复核**默认关**（实测它让合成明显变不稳定）",
          studio.REF_USE_VOICE is False,
          "开=抖动 21.3Hz/信噪比 1.4x，关=抖动 1.7Hz/信噪比 32.5x")
    check("★ 关掉的原因写进了源码注释（后人不会再来一遍）",
          "实测被否决" in body and "21.3" in body,
          "注释里带实测对照表")
    check("实现仍在且受开关控制（想再试能一行打开）",
          "_feat_subprocess" in body and "_emb_dist" in body
          and "REF_USE_VOICE" in body)
    check("声纹复核有候选上限（别把每次说话拖慢）",
          studio.REF_VOICE_TOPN >= 3, "%d 段" % studio.REF_VOICE_TOPN)
    check("声纹算不出来时退回时长排序（try/except 兜住）",
          "except Exception" in body)
    check("★ 挑出来的参考确实落在 3~10 秒（文献的参考音频长度）",
          3.0 <= _ref_dur() <= 10.0, "%.2f 秒" % _ref_dur())


def _ref_dur():
    """量一次真实角色挑出来的参考音频有多长（跑不通返回 -1，不判红）。"""
    try:
        role = studio.get_role("ra500fb92")
        if not role:
            return -1.0
        sd = os.path.join(studio.role_dir("ra500fb92"), "slices")
        if not os.path.isdir(sd):
            return -1.0
        p, _ = studio.pick_reference(role, sd)
        return studio.wav_seconds(p) or -1.0
    except Exception:
        return -1.0


# ---------------------------------------------------------------- F 尺子自检
def f_selfcheck():
    sec("F. 尺子自检：把阈值旋掉，判据**必须跟着变红**（否则是永远绿的尺子）")
    tmp = tempfile.mkdtemp(prefix="_vq_")
    try:
        # ① 底噪判据：把 TRAIN_FLOOR_BAD 旋到 +10 ⇒ 没有段会超 ⇒ 不该再报 bad
        #   ★ 方向别搞反：阈值是"高于它算不合格"，旋**低**会让所有段都超标，
        #     旋**高**才会让告警消失。第一版旋到 -200，结果 bad 更多了。
        _bad = studio.TRAIN_FLOOR_BAD
        studio.TRAIN_FLOOR_BAD = 10.0
        try:
            a = audit_with(tmp, [(5.0, 0.15, 0.03, False)] * 12)
        finally:
            studio.TRAIN_FLOOR_BAD = _bad
        check("底噪阈值旋到 +10dB ⇒ 底噪告警必须消失（证明阈值真在起作用）",
              a and not has(a, "底噪", "bad"),
              "verdict=%s" % (a or {}).get("verdict"))

        # ② 响度判据：旋大 ⇒ 忽大忽小也不报
        _lw = studio.TRAIN_LOUD_SPAN_WARN
        studio.TRAIN_LOUD_SPAN_WARN = 999.0
        try:
            b = audit_with(tmp, [(5.0, 0.02, 0.00003, False)] * 6 +
                                [(5.0, 0.5, 0.00003, False)] * 6)
        finally:
            studio.TRAIN_LOUD_SPAN_WARN = _lw
        check("响度阈值旋到 999dB ⇒ 响度告警必须消失",
              b and not has(b, "响度跨度"))

        # ③ 短段门槛：旋到 0（等于没有短段）⇒ 短段告警消失
        _sm = studio.TRAIN_SEG_MIN
        studio.TRAIN_SEG_MIN = 0.0
        try:
            c = audit_with(tmp, [(1.5, 0.15, 0.00003, False)] * 10 +
                                [(5.0, 0.15, 0.00003, False)] * 2)
        finally:
            studio.TRAIN_SEG_MIN = _sm
        check("短段门槛旋到 0 ⇒ 短段告警必须消失", c and not has(c, "短于"))

        # ④ 复原点：旋回来之后必须恢复原判（防止上面的改动泄漏）
        d = audit_with(tmp, [(5.0, 0.15, 0.03, False)] * 12)
        check("阈值复原 ⇒ 判据回到原样（改动没泄漏）",
              d and has(d, "底噪", "bad"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def g_custom():
    sec("G. 唱歌专用底模（KLM 32k / KLM-HFG 48k）：架构锁死 / 路径隔离 / 缺件不瞎下载 / 采样率护栏")
    cp = studio.RVC_CUSTOM_PRETRAINS.get("klm")
    check("注册了 klm 唱歌底模", bool(cp))
    if not cp:
        return
    # ① 架构锁死：KLM 只有 32k，配 40k/48k 会"文件在、加载不上"，训练照跑底模全废
    check("KLM 锁 v2/32k（底模与架构对不上=训练照跑但底模全废）",
          cp.get("arch") == "v2" and cp.get("sr") == "32k",
          "%s/%s" % (cp.get("arch"), cp.get("sr")))

    # ② 路径隔离：自定义底模不能混进官方目录（否则下次 ensure 会把它当官方文件）
    gp, dp = studio.rvc_pretrained_paths("v2", "32k", "klm")
    gs, ds = studio.rvc_pretrained_paths("v2", "32k")
    check("自定义底模走 pretrained_custom/，不污染官方目录",
          "pretrained_custom" in gp and "pretrained_custom" in dp, gp)
    check("负对照：custom=None 仍指向官方 pretrained_v2/（没被带歪）",
          "pretrained_custom" not in gs and "pretrained_v2" in gs, gs)
    check("自定义与官方是两组不同文件", gp != gs and dp != ds)

    # ③ 缺件时不许去官方仓库瞎下 —— 官方仓库没有 KLM 的固定路径，只会 404 刷屏，
    #    用户看到一屏 "尝试 https://.../pretrained_v2/KLMX10_f0G32k.pth" 完全没法理解
    saved_handler = None
    try:
        studio.RVC_CUSTOM_PRETRAINS["__probe_missing__"] = {
            "sub": "pretrained_custom", "g": "___no_such_g.pth",
            "d": "___no_such_d.pth", "arch": "v2", "sr": "32k", "label": "探针",
        }
        p = os.path.join(tempfile.gettempdir(), "_vq_klm_missing.log")
        with open(p, "w", encoding="utf-8") as f:
            f.write("")
        job = {"dir": ".", "id": "vqg", "_logfile": p}
        got = studio.ensure_rvc_pretrained(job, "v2", "32k", "__probe_missing__")
        check("★ 缺件时直接返回 (None,None)，不去官方仓库瞎下载",
              got == (None, None), str(got))
        txt = open(p, encoding="utf-8").read()
        check("缺件时说清「不会自动下载」（别让用户以为在下）",
              "不会自动下载" in txt, txt[-100:].replace("\n", " "))
        check("缺件日志里没有去请求官方 URL",
              "hf-mirror.com" not in txt and "huggingface.co" not in txt,
              txt[:100].replace("\n", " "))
    finally:
        studio.RVC_CUSTOM_PRETRAINS.pop("__probe_missing__", None)

    # ④ 真加载（文件在才验；不在就 SKIP，不判红 —— 底模是可选资产）
    if studio.rvc_pretrained_here("v2", "32k", "klm")[0]:
        lp = os.path.join(tempfile.gettempdir(), "_vq_klm_real.log")
        ok, msg = studio.check_rvc_pretrained(
            {"dir": ".", "id": "vqg", "_logfile": lp}, "v2", "32k", "klm")
        check("★ KLM 真能加载进 v2/32k（「文件在」不等于「用得上」）",
              ok and "missing=0" in msg and "unexpected=0" in msg, msg[:130])
    else:
        print("  SKIP  KLM 底模没下载 ⇒ 跳过真加载，不判红")

    # ⑤ 静态：这几条错了会**静默污染线上模型**，必须钉住
    src = open(os.path.join(os.getcwd(), "studio.py"), encoding="utf-8").read()
    i = src.find("def train_rvc")
    # 窗口要够长：train_rvc 有 6000+ 字符（数据准备/体检/底模/训练/索引/绑定都在里面），
    # 切 5000 会把最后的绑定逻辑漏掉 —— 第一版就是这么假红的。
    body = src[i:i + 22000] if i >= 0 else ""
    check("train_rvc 接受 pretrain 参数", "pretrain=None" in body)
    check("★ 指定底模时架构跟着底模走（arch, sr = cp[...]）",
          'arch, sr = cp["arch"], cp["sr"]' in body)
    check("★ 自定义底模用独立 exp（同 exp 有旧断点时底模会被跳过）",
          'exp += "_" + pretrain' in body)
    check("实验模式 bind=False 不绑定角色（对比实验不能动线上模型）",
          "if not bind:" in body and "实验模式" in body)

    # ⑥ 48k 歌唱底模（KLM-HFG）：它存在的理由是 32k 的天花板只有 16kHz，
    #    齿音/气声那一层生成不出来 —— 而"AI 感重、咬字不清"正好落在那一层。
    cp48 = studio.RVC_CUSTOM_PRETRAINS.get("klm48")
    check("注册了 klm48（48k 歌唱底模）", bool(cp48))
    if cp48:
        check("klm48 锁 v2/48k（48k 底模配 32k 会 size mismatch，报错是一屏张量尺寸）",
              cp48.get("arch") == "v2" and cp48.get("sr") == "48k",
              "%s/%s" % (cp48.get("arch"), cp48.get("sr")))
        g48, d48 = studio.rvc_pretrained_paths("v2", "48k", "klm48")
        check("klm48 与 klm 是两组不同文件（同 sub 目录但不同文件名）",
              g48.endswith("48k.pth") and g48 not in (gp, gs), g48)
        # ★ 采样率不参与 exp 命名 ⇒ 32k 与 48k 官方底模会共用 `logs/<rid>_v2`，
        #   直接在 32k 断点上续 48k 会 size mismatch（报错只有一屏张量尺寸）。
        #   护栏抽成了 `_assert_exp_sr`，所以这里能**直接拿它做正反两面的实测**，
        #   不用为了验它去真跑一次训练。
        _tmp_exp = os.path.join(studio.RVC_ROOT, "logs", "__vq_sr_guard__")
        try:
            os.makedirs(_tmp_exp, exist_ok=True)
            with open(os.path.join(_tmp_exp, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"data": {"sampling_rate": 32000}}, f)
            # 正：同采样率必须放行
            try:
                studio._assert_exp_sr("__vq_sr_guard__", "32k")
                _same_ok = True
            except Exception as e:
                _same_ok = "%s: %s" % (type(e).__name__, str(e)[:80])
            check("同采样率续训放行（护栏不能把正常续训也拦了）", _same_ok is True, _same_ok)
            # 负：换采样率必须拦住，而且要说清是采样率
            try:
                studio._assert_exp_sr("__vq_sr_guard__", "48k")
                _blocked, _msg = False, "没拦住"
            except studio.Fail as e:
                _blocked, _msg = True, str(e)
            except Exception as e:
                _blocked, _msg = False, "%s: %s" % (type(e).__name__, str(e)[:120])
            check("★ 负对照：32k 目录里要训 48k ⇒ 当场拦住并说清是采样率",
                  _blocked and "采样率" in _msg, _msg[:90].replace("\n", " "))
            # 负对照之二：目录不存在时必须安静放行（不能把首次训练拦下来）
            try:
                studio._assert_exp_sr("__vq_no_such_exp__", "48k")
                _new_ok = True
            except Exception as e:
                _new_ok = "%s: %s" % (type(e).__name__, str(e)[:80])
            check("从没训过的 exp 放行（首次训练不能被护栏挡住）", _new_ok is True, _new_ok)
        finally:
            shutil.rmtree(_tmp_exp, ignore_errors=True)
        if studio.rvc_pretrained_here("v2", "48k", "klm48")[0]:
            lp48 = os.path.join(tempfile.gettempdir(), "_vq_klm48_real.log")
            ok48, msg48 = studio.check_rvc_pretrained(
                {"dir": ".", "id": "vqg", "_logfile": lp48}, "v2", "48k", "klm48")
            check("★ klm48 真能加载进 v2/48k（实测 missing=0/unexpected=0）",
                  ok48 and "missing=0" in msg48 and "unexpected=0" in msg48,
                  msg48[:130])
        else:
            print("  SKIP  klm48 底模没下载 ⇒ 跳过真加载，不判红")


def main():
    print("=" * 62)
    print("第 30 关：训练质量（素材体检 / 轮数推荐 / v2 架构 / 参考音频 / 唱歌底模）")
    print("=" * 62)
    a_consts()
    b_audit()
    c_epochs()
    d_v2()
    e_ref()
    g_custom()
    f_selfcheck()
    print("\n" + "=" * 62)
    print("PASS %d / FAIL %d" % (PASS, len(FAILS)))
    if FAILS:
        for f in FAILS:
            print("  红：%s" % f)
    print("ALLDONE")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
