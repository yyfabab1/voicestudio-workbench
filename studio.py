r"""
VoiceStudio 核心：角色库 + 流水线编排。

和上一版（AutoVoice 的 auto_voice.py）最大的区别是「角色」成了一等公民：
以前一个视频对应一个音色，跑完就结束；现在一个视频里的每个声音都会被拆出来
单独建一个角色，各自有自己的素材、标注、模型、成品和介绍，并且每个都还能
继续加料训练、说话、唱歌。

目录约定：
    data/roles.json              角色索引
    data/roles/<id>/             角色目录
        slices/                  属于这个角色的切片
        asr.list                 标注（人工可改）
        final/                   合成成品
        state.json               这一步跑到哪了
    data/jobs/<id>/              任务日志
    data/uploads/                上传的视频
"""
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import wave
import contextlib

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

DATA = os.path.join(HERE, "data")
ROLES_DIR = os.path.join(DATA, "roles")
JOBS_DIR = os.path.join(DATA, "jobs")
UPLOADS = os.path.join(DATA, "uploads")
ROLES_JSON = os.path.join(DATA, "roles.json")
STAGES = os.path.join(HERE, "stages")

GSV_ROOT = r"D:\BaiduNetdiskDownload\GPT-SoVITS(1)\v5最新版\GPT-SoVITS-v5-20261005nvidia50x0\GPT-SoVITS-v2pro-20261006nvidia50x0"
RVC_ROOT = r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI"
PY = os.path.join(GSV_ROOT, "runtime", "python.exe")
RVC_PY = os.path.join(RVC_ROOT, "runtime", "python.exe")
FFMPEG = os.path.join(GSV_ROOT, "runtime", "ffmpeg.exe")
FFPROBE = os.path.join(GSV_ROOT, "runtime", "ffprobe.exe")

# ---- 改词（YingMusic-Singer-Plus）------------------------------------
#
# 为什么必须独立成第三个环境：它是 py3.10 + torch 2.7.1(cu128)，
# 和上面两套 runtime 的 torch 版本都不一样。三个环境各跑各的子进程，
# 谁也别 import 谁 —— 混进同一个进程会崩在完全不相干的地方，
# 而报错信息只会指向那一行，根因（解释器/依赖）根本看不出来。
YING_ROOT = r"D:\AHC\YingMusic-Singer-Plus-main"
YING_PY = os.path.join(YING_ROOT, ".venv", "Scripts", "python.exe")
# 改词一次生成的块长（秒）。实测 28 秒一块在 8GB 显存上稳；
# 再长就容易出现整块跑偏，再短则块间拼接缝隙变多。
LYRIC_CHUNK_SEC = 28.0

S2G = "GPT_SoVITS/pretrained_models/gsv-v5-pretrained/s2Gv5turbo.pth"
GPT_WEIGHT_DIR = os.path.join(GSV_ROOT, "GPT_weights_v5turbo")

import seedvc  # noqa: E402  Seed-VC（44.1k 歌声精修 + 角色微调）的路径/档位，只读
# 可视化视频（把成品渲染成能直接发出去的 MP4）。这个模块**不反向 import 本文件**，
# 只依赖标准库 + ffmpeg，所以这里顶层 import 不会打结。
import viz  # noqa: E402

# 视频字幕：用户改过的文字 + 中文对照。同样不反向 import 本文件，
# 只依赖标准库（urllib 打本地网关 / 在线词典），顶层 import 不会打结。
import vizsubs  # noqa: E402

# ★ 逐帧手绘那套要用 numpy + PIL。运行时的 GPT-SoVITS python 和门禁 venv 里都有，
#   但**不是每个会 import studio 的解释器都有** —— `verify_disk` /
#   `verify_train_rounds` 跑的是精简的托管 python，那里没有 numpy。
#   顶层硬 import 会让那一关直接 `ModuleNotFoundError: numpy`，
#   看起来像"磁盘检查坏了"，其实是可视化模块把整个 studio 拖死了（实测踩过）。
#   所以这里降级成"没有就少一套引擎"，并在用的时候给出能看懂的原因。
try:
    import vizfx  # noqa: E402
    _VIZFX_ERR = ""
except Exception as _e:                                          # noqa: BLE001
    vizfx = None
    _VIZFX_ERR = "%s: %s" % (type(_e).__name__, _e)

# AE 引擎（真 After Effects 渲染）：只用标准库，所以可以硬 import。
# ★ 但"这台机器上有没有 AE"是**运行时**才确定的事 —— 装没装、装在哪，
#   每次问一次注册表不划算，所以 `aefx.find_ae()` 的结果在这里缓存一次，
#   由 `_ae_path()` 统一出。没装的话整组样式不下发（界面上就不出现这一组），
#   而不是下发一堆点了必然报错的卡。
try:
    import aefx  # noqa: E402
    _AEFX_ERR = ""
except Exception as _e2:                                         # noqa: BLE001
    aefx = None
    _AEFX_ERR = "%s: %s" % (type(_e2).__name__, _e2)

_AE_EXE = None          # None = 还没查过；"" = 查过，没有
_AE_NOTE = ""


def _ae_path():
    """这台机器上 After Effects 的绝对路径（没有就返回 ""）。只查一次。"""
    global _AE_EXE, _AE_NOTE
    if _AE_EXE is None:
        if aefx is None:
            _AE_EXE, _AE_NOTE = "", (_AEFX_ERR or "AE 模块没加载")
        else:
            try:
                _AE_EXE = aefx.find_ae() or ""
            except Exception as _e3:                             # noqa: BLE001
                _AE_EXE, _AE_NOTE = "", "%s: %s" % (type(_e3).__name__, _e3)
            if not _AE_EXE:
                _AE_NOTE = _AE_NOTE or "这台机器上没找到 After Effects"
    return _AE_EXE

# 「每一句的语气」：tone.py 只用标准库（math），所以可以硬 import ——
# 它不需要 numpy/librosa，那些重的活都在 stages/tone_index.py 里跑子进程。
import tone  # noqa: E402

# 切片参数，顺序对应 `tools/slice_audio.py`：
#   阈值dB / 每段最短 / 最短间隔 / 跳步 / 静音最多留 / 归一化峰值 / 混合系数 / 分片号 / 总分片
#
# ★ 中间那四个的单位是**毫秒**，不是帧（`tools/slicer2.py` 的 Slicer 内部自己按
#   sr 换算；它 argparse 的 help 原文写的就是 milliseconds）。
#   ⇒ 也就是：每段至少 **4 秒**、静音满 **0.3 秒**才切、静音最多留 **0.5 秒**。
#   望文生义当成"帧"再去乘一次采样率比例，会把 4 秒悄悄改成 6 秒（实测：3 段被并成
#   2 段）—— 这个坑由 `_probe_slice_hifi.py` 的"切分行为必须不变"判据挡着。
SLICE_ARGS = ["-34", "4000", "300", "10", "500", "0.9", "0.25", "0", "1"]

# ★★ 切片**输出**的采样率（2026-10-16 起）。为什么是 48000：
#   GPT-SoVITS 自带的 `tools/slice_audio.py` 三处写死 32000（`Slicer(sr=)` /
#   `load_audio()` / `wavfile.write()`），那是给**说话模型**用的，没错。
#   但我们同一份切片还拿去训 **RVC 歌声模型** ⇒ 素材被锁在 32k，奈奎斯特上限
#   **16 kHz**。实测：分离出的登阶人声是 44.1k stereo，切完 353 段全是 32k，
#   上面那 16k~22.05k 一段（齿音、气声、"像不像本人在唱"的那部分）当场没了。
#   文献对训练素材的要求是 **44.1k 或 48k 且全库统一**；RVC 的档位里 v1 只有
#   32k/40k/48k、v2 只有 32k/48k ⇒ **48k 是唯一两代都支持的最高档**，
#   且 48k 的奈奎斯特 24k > 44.1k 的 22.05k，从 44.1k 升上去**不丢任何信息**。
#   ⇒ 我们用自己的 `stages/slice_hifi.py` 切，不改第三方工具（见那个文件的头）。
#
# ⚠ 长度参数**一个字都不用改**：它们是毫秒，Slicer 内部按 sr 换算（见上）。
#   曾经在这里多乘了一次 sr/32000，把"每段至少 4 秒"变成 6 秒 ⇒ 段被合并。
#   现在有探针盯着：段数、每段起止秒数必须和 32k 那份**逐段对齐**。
#
# 生效范围：**只有新导入的素材**。老角色的切片已经是 32k，不会重切（重切也救不回
# 已经丢掉的高频）—— next import 才吃得到，这点在体检里会照实说。
SLICE_SR = 48000
PALETTE = ["#6c8cff", "#ff7a59", "#2fb8a8", "#c86dd7", "#e0b53c", "#4fc75a", "#ff6f91", "#5ec8f2"]

# ---- 人声分离（唱歌链路的第一步，也是"唱出来没法听"的根治点）------------
#
# 为什么唱歌前必须分离：RVC 是"人声变人声"的模型，它靠 f0（基频）曲线工作。
# 把整首歌（含鼓、贝斯、和声）直接喂进去，它会去给鼓点也编一条 f0，
# 输出就成了跟着节奏抖的一锅粥；更要命的是混音时如果再拿**原曲**当伴奏，
# 原唱人声会跟着角色一起唱 —— 实测成品波形与原曲相关系数 0.870、
# 原曲振幅占比 0.538，也就是成品里一大半是原唱。那当然"没法听"。
#
# 分离器优先用 Roformer（SDR 12.98，2024 年的 SOTA），失败自动回退 VR 的
# HP5 —— Roformer 需要配套 yaml，缺失时它连权重都读不了，不能让它把整条链带死。
SEP_CACHE = os.path.join(DATA, "cache", "sep")

# ★★ 2026-10-26 用 8 首歌实测重排了这一组（黑名单见 `_ab_sepkj.py` 的产出表，
#    明细在 _ab_sepkj_run8.json）。之前这块是照着**榜单 SDR**排的，而榜单
#    （MSST 的 Multisong 数据集）跟老板的歌根本不是一个分布：
#
#    | 模型                       | 8 首平均 | 夺冠 | 最差一首 | 单次耗时 |
#    |----------------------------|---------:|------:|---------:|---------:|
#    | HP2_all_vocals             |   65.90  |  4/8  |   51.27  |   8.1 s  |
#    | HP5_only_main_vocal        |   59.74  |  1/8  |   39.06  |   8.2 s  |
#    | mel_band_roformer_becruily |   56.98  |  0/8  |   32.35  |  13.3 s  |
#    | KJ-MelBandRoformer（新）    |   56.43  |  3/8  |   28.99  |  12.9 s  |
#    | model_bs_roformer_ep_317   |   55.95  |  1/8  |   26.57  |  15.7 s  |
#
#    三条结论：
#    ① 新模型 KJ 平均只比老主力高 0.5 分，但**夺冠 3/8 vs 1/8** —— 它在一部分
#       歌上明显强（最好的一首 86.96），值得进池子挑。
#    ② 冠军是 **HP2**：它平均最高、**最稳健**（最差一首 51.27，而老主力只有
#       26.57 —— 差的这首歌才是生产环境真正怕的事），而且**快一倍**。
#       它以前排在候选最后一位、只在"全部失败"时才轮到，纯属排错了。
#    ③ 没有万能模型：每首歌的最优都不同（8 首分布在 4 个模型上）。所以不是"换一个
#       更好的"，而是"把互补的都放进池子，继续让实测去挑"。
#
# ★ 默认是给**不走选优**的场合用的（调用方指定了模型、或探测全失败时的兜底），
#   取的是"这本批歌上平均最好 + 最不容易翻车"的那个，不是榜单 SDR 最高的。
UVR_MODEL_BEST = "HP2_all_vocals"
UVR_MODEL_SAFE = "HP5_only_main_vocal"

# ---- 分离选优（2026-10-12 加）------------------------------------------
#
# 以前是「固定用 Roformer，跑不通就退 HP5」——**跑通就算好**，质量从来没被量过。
# 于是"漏了一片伴奏进来"和"把人声削掉一半"在日志里长得一模一样。现在改成：
#   ① 先切 30 秒代表性片段，让**每个候选模型**各分一遍；
#   ② 用 stages/sep_eval.py 量化打分（leak / 动态范围 / 饱满度 / 高频）；
#   ③ 分最高的那个去跑全曲。
# 代价只有"多跑 2 段 30 秒"（实测 8~15 秒一个），换来的是每一首歌都用对了模型。
#
# ★ 评分权重是 `_probe_sep_calib.py` 用**合成真值**校准出来的，不是拍脑袋：
#   把已知的人声+伴奏重新混成一首歌（真值已知），各模型分一遍，再拿指标跟真 SDR
#   比排序。实测 SDR 9.21 / 5.49 / 3.40 对应 score 96.4 / 81.4 / 72.0 —— 同序。
#   floor（越安静越好）当时是**反的**（削到没声音当然安静），已经改成区间型。
# ★ v2 → v3（2026-10-26）：候选池重排 + 换默认模型 + 加入 MSST 新通路。
#   按约定**必须 +1**。因为自动选优时缓存键里的模型位是常量 "auto"，
#   前一次分离的结果会被直接命中、原样返回 ⇒ 不 +1 的话这一轮全白改
#   （老歌永远不会用上新候选）。代价是所有歌的第一遍要重算一次。
SEP_POLICY = "v4"                 # 改权重/候选就 +1，旧缓存自然失效重算
#   ↑ 2026-10-27 v3 → v4：探针从"只取正中间一段"改成"中段 + 后段两段"，
#     并且挑模型时加了漏电否决权。**必须 +1**：不加的话，"越到后面越差"那首歌
#     （以及所有老歌）的分离结果会直接命中 v3 的缓存、原样返回，
#     新逻辑一次也轮不上 —— 改了等于没改。代价是所有歌第一遍要重算一次。
# ★ 2026-10-22 实测（4 首歌 × 4 个模型，30 秒片段）后加了 mel-band：
#   | 模型 | 4 首平均分 |
#   | bs_roformer(旧主力) | 82.6 |
#   | mel_band_roformer_vocals_becruily（新） | 81.0（在第 2 首上赢，且 leak 最低 0.122）|
#   | HP5 / HP2（VR 老模型） | 78.1 / 78.0 |
#   逐首排名**真的会变**（第 3 首上 VR 比 roformer 低 18 分），所以"每次都试一遍"
#   不是为了好看 —— 它是真有 10~18 分的差别。
UVR_CANDIDATES = (UVR_MODEL_BEST, UVR_MODEL_SAFE,
                  "mel_band_roformer_vocals_becruily",
                  "mel_band_roformer_kj",
                  "model_bs_roformer_ep_317_sdr_12.9755")
SEP_PROBE_SEC = 30.0              # 先拿这么长一段去试各模型
SEP_MAX_CAND = 5                  # 最多试几个（实测一个约 8~16 秒）

# ★ 2026-10-27：探针**取两段，拼成 30 秒**（每段 15 秒，总量不变 ⇒ 不增加时间成本）。
#   位置取歌的 40% 和 80% 处 —— 后者是关键。
#   为什么非改不可（一整条实测因果链，不是推测）：
#     ① 原来只取歌曲**正中间**一段：`(total-30)/2`。一首 198 秒的歌探针落在
#        84.4 秒，而「越到后面唱得越差、突然跳八度」发生在 **150 秒之后**
#        ⇒ **后半段从来没被评估过**，模型是在没问题的那一段上挑出来的。
#     ② 把 150~180 秒单独切出来，让 5 个候选各分一遍比漏电，当前被选中的
#        `HP5_only_main_vocal` 恰恰是**漏得最狠的那个**（leak 0.2663，
#        最好的 mel_band_roformer_kj 只有 0.1919，差 39%）。它赢在 quiet
#        （0.769 vs 0.428），不是赢在漏电。
#     ③ 而"漏电"正是跳八度的来源：实测 161.05 秒，伴奏轨在 750~823Hz 有一簇
#        强峰，人声轨在**同样的频率**有一簇同样的峰（783Hz 48.6dB）—— 那不是
#        人声，是漏进来的高音乐器，RVC 把它当人声唱了出来。
#     ④ 换成漏得少的那份分离，同一段（150~180s）的 f0 >600Hz 占比
#        从 **13.1% 掉到 0.0%**、>400Hz 从 32.8% 掉到 0.4%，有声帧还多 56%
#        （人声没被削，是伴奏被去掉了）。
SEP_PROBE_AT = (0.40, 0.80)
# ★ 漏电**否决权**：探针漏电超过这个值的模型排到后面，哪怕它总分最高。
#   ⚠ 比的值必须用 **leak_worst**（"最漏的那一段"的 p90），不能用整段中位数：
#     实测同一份探针，五个模型的中位数全落在 0.164~0.194，看着都合格、否决权
#     一次都触发不了；换成 p90 立刻分出层次（0.1932 ~ 0.2576）。
#     中位数被干净段稀释 —— 这正是"越到后面越差"能被瞒过去的原因。
#   ⚠ 0.20 是照**生产口径**（中段 + 后段拼起来的 30 秒探针，3 秒窗的 p90）实测定的：
#     HP5 0.2078 / HP2 0.2576 / bs 0.2093 / becr 0.2387 / kj 0.1932
#     ⇒ 只有 kj 过关，而它正是后段漏得最少的那个（单独量后段 0.1919 vs 0.2663）。
#     ★ 别拿"单独后段"的数字（0.2663 那组）来定这个阈值 —— 两个口径不是一个
#       量级，照它定会失效（我第一版定 0.22 就是这么错的）。
#   ★ 全部超标时（第一关键字都是 1）排序自动退化回"按总分"，**不会没得选**。
SEP_PROBE_LEAK_MAX = 0.20
# ★ 2026-10-26 新增的第五个候选 **mel_band_roformer_kj**（MSST 官方榜 vocal SDR
#   10.98，权重本机已有）走**另一条路**：用 MSST 自带的推理代码（stages/msst/），
#   不经过 UVR 的 bsroformer 加载器 —— 它是 MSST 的模型格式，UVR 加载不了。
#   它依赖的 venv 不在项目里 ⇒ 每次用它前先 `_msst_available()` 探一下，
#   不在就跳过，其余四个照跑。**它是增益不是依赖**：缺了不影响任何人。
MSST_MODELS = ("mel_band_roformer_kj", "mel_band_roformer_karaoke_bec",
                "karaoke_bs_roformer_anvuew")
# ★ karaoke 那个**不进自动候选池**：它的目标函数不一样（把伴唱留在伴奏里），
#   不是"谁分得干净"能比的。只有用户显式选 sep_model="karaoke" 时才用它。
VS_MSST_PY = os.path.join(os.path.dirname(HERE),
                          "YingMusic-Singer-Plus-main", ".venv", "Scripts", "python.exe")
# ★ 早停：试够 SEP_PROBE_MIN 个之后，只要第一名领先第二名 ≥ SEP_PROBE_GAP 就收工。
#   ---------------------------------------------------------------------------
#   ★★ 这一版是 2026-10-22 拿 **193 次真实探测离线回放**定出来的（不是拍脑袋）。
#   上一版用的是"绝对分 ≥85"，回放一算才发现它几乎从不触发：590 个探测分里
#   ≥85 的只占 **9.3%**（中位 52.1、均值 60.1）⇒ 省时间的效果基本是虚的。
#
#   换成"领先第二名多少分"之后（同一批数据回放）：
#     | gap 阈值 | 平均试几个 | 命中最好的 | 平均损失 | 最大损失 |
#     |    1.0   |   2.06     |   93.8%    |  0.24    |  11.70   |
#     |    3.0   |   2.19     |   99.0%    |  0.08    |  11.70   |
#     |    4.0   |   2.35     |  100.0%    |  0.00    |   0.00   |
#     |    5.0   |   2.35     |  100.0%    |  0.00    |   0.00   |
#     |    6.0   |   2.35     |  100.0%    |  0.00    |   0.00   |
#     |    8.0   |   2.80     |  100.0%    |  0.00    |   0.00   |
#   全试是 3.03 个。取 **5.0**：落在无损区（4~6）中间，离"开始有损失的 3.0"
#   还有 2 分余量（历史只有 193 次，留点边防止过拟合）。
#   ⇒ 平均 3.03 → 2.35 个候选，每首省 9~12 秒，且 193 次里**一次都没选错**。
#
#   为什么"领先度"比"绝对分"对：分数随歌的难度整体浮动（难歌四个模型都只
#   有 50 分，简单歌都有 80 分）。绝对阈值等于"简单歌少试、难歌多试"，
#   而真正该问的是"再试一个还有没有可能翻盘"—— 那看的是**已试过的分差**。
SEP_PROBE_MIN = 2                 # 至少试几个（1 个太脆：单个模型偶然失手就选错了）
SEP_PROBE_GAP = 5.0               # 第一名领先第二名这么多分 ⇒ 不用再试了（见上表）
# ★ 再配一条"绝对分已经很高"的线，两条**满足一条就停**：
#   | 规则                       | 平均试几个 | 命中最好 | 最大损失 |
#   | 全试（老行为）              |   3.03    | 100.0%  |   0.00  |
#   | 只 gap≥5                  |   2.36    | 100.0%  |   0.00  |
#   | gap≥5 或 绝对≥85（采用）    |   2.17    |  98.5%  |   3.30  |
#   | gap≥5 或 绝对≥80          |   2.12    |  97.4%  |   3.30  |
#   绝对线单独用不划算（≥85 只占 9.3%，几乎不触发），但它专治**简单的歌**：
#   那种歌前两个就能到 89/90 分，后面两个纯属白跑 —— 实测一首 70 秒的歌上，
#   光这一条就省掉了 23 秒（两个候选 9s + 14s）。
#   取 85 而不是 80，是因为 80 会把命中率从 98.5% 拉到 97.4%，多换来的
#   0.05 个候选不值这个风险 —— 老板要的是质量，这里省的是时间。
SEP_PROBE_GOOD = 85.0
SEP_RETRY_LEAK = 0.15             # 泄漏超过这个值 ⇒ 考虑人声轨再分离一遍（去残留）
# ★ 光看漏多少不够：好歌也会漏一点（实测 4 分钟流行曲 leak=0.163、分 76.8）。
#   再加一道"第一遍真的不行"才重跑 —— 否则每首都要多花一整遍全曲时间。
SEP_RETRY_SCORE = 70.0            # 第一遍分数低于这个才做二次分离
SEP_PICK_STATS = os.path.join(DATA, "sep_pick_stats.json")

for d in (DATA, ROLES_DIR, JOBS_DIR, UPLOADS, SEP_CACHE):
    os.makedirs(d, exist_ok=True)


# ---------------------------------------------------------------- 小工具


def log(job, msg=""):
    line = str(msg)
    print("[%s] %s" % (job.get("id", "-"), line), flush=True)
    with open(job["_logfile"], "a", encoding="utf-8") as f:
        f.write(line + "\n")


# 视频后缀：丢进「歌」槽时先抽音轨再往下走。
# 参考了 ffmpeg.wasm / Mediabunny 那类"视频→音频"的思路，只是本机已经有 ffmpeg，
# 没必要在浏览器里再跑一遍 WASM（那对几百 MB 的视频也不现实）。
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".mov", ".flv", ".webm", ".m4v",
             ".ts", ".mpeg", ".mpg", ".wmv", ".vob", ".rmvb")


def is_video(path):
    return str(path or "").lower().endswith(VIDEO_EXT)


def extract_audio(src, dst=None, timeout=1800):
    """从视频里把音轨抽出来（ffmpeg），返回 (路径, 说明)。

    失败返回 (None, 原因) —— 不抛异常：抽不出来就让用户自己去转，
    别把上传这一步整个弄死。
    """
    src = str(src or "")
    if not os.path.isfile(src):
        return None, "文件不在：%s" % src
    base = os.path.splitext(os.path.basename(src))[0]
    dst = dst or os.path.join(UPLOADS, base + "_音轨.wav")
    try:
        # -vn 丢掉视频流；统一成 44.1k 立体声，后面分离/变声/混音都吃这个规格
        p = subprocess.run(
            [FFMPEG, "-y", "-v", "error", "-i", src, "-vn",
             "-ac", "2", "-ar", "44100", "-acodec", "pcm_s16le", dst],
            capture_output=True, timeout=timeout)
    except Exception as e:
        return None, "%s: %s" % (type(e).__name__, str(e)[:160])
    if p.returncode != 0 or not os.path.isfile(dst) or os.path.getsize(dst) < 1024:
        err = (p.stderr or b"").decode("utf-8", "ignore").strip().splitlines()
        return None, "抽不出音轨（这个视频可能没有音频流，或编码 ffmpeg 不认）：%s" % (
            err[-1][:160] if err else "退出码 %s" % p.returncode)
    secs = wav_seconds(dst)
    return dst, "已从视频抽出音轨（%.1f 秒）" % (secs or 0)


def wav_seconds(path):
    try:
        w = wave.open(path, "r")
        s = w.getnframes() / float(w.getframerate())
        w.close()
        return s
    except Exception:
        return 0.0


def wav_rms(path):
    import array

    try:
        w = wave.open(path, "r")
        raw = w.readframes(w.getnframes())
        w.close()
    except Exception:
        return 0.0
    try:
        a = array.array("h")
        a.frombytes(raw)
    except Exception:
        return 0.0
    if not a:
        return 0.0
    acc = 0.0
    step = max(1, len(a) // 20000)
    cnt = 0
    for i in range(0, len(a), step):
        v = a[i] / 32768.0
        acc += v * v
        cnt += 1
    return (acc / max(cnt, 1)) ** 0.5


def base_env():
    env = os.environ.copy()
    env["PATH"] = os.path.join(GSV_ROOT, "runtime") + os.pathsep + env.get("PATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # ★ 子进程的 stdout 接的是管道 ⇒ Python 默认**块缓冲（4~8KB）**，
    #   它的 print 会一直堵在子进程缓冲区里，直到进程结束或攒满 8KB。
    #   实测：让角色唱歌（RVC 推理）跑了 **15 分钟**，任务日志里除了一行
    #   "$ …infer.cli…" 之外**一个字都没有**，进度条纹丝不动 —— 用户的原话是
    #   "看不见进度以及做到哪一步"。而那个脚本明明开头就 print 了
    #   "当前设备：… | 推理精度：…"。根因就是这四个字：块缓冲。
    #   加这一个环境变量，所有走 run() 的子进程（拆解/训练/推理/混音）都立刻逐行可见。
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _tail(out, n=6):
    """取输出末尾几行 —— 报错时直接把真正的原因摆出来，
    而不是只说「失败了」。用户要能看见每一步卡在哪、为什么。"""
    if not out:
        return ""
    lines = [ln.rstrip() for ln in str(out).splitlines() if ln.strip()]
    return " / ".join(lines[-n:])[-400:]


_PROG_RE = re.compile(r"(\d{1,3})\s*%")


def _pct_from_line(job, line):
    """从子进程的一行里抠一个进度百分比；抠不到就什么都不做。

    为什么需要：进度条要动才让人安心。RVC 的推理链里**没有 tqdm**，
    多数行抠不到百分比 —— 那也不要紧（日志在动、步骤名在，用户就知道没卡死）；
    抠得到就是白赚。训练那类会打百分比的脚本因此也能直接受益。
    """
    m = _PROG_RE.search(line or "")
    if not m:
        return
    p = int(m.group(1))
    if not (0 <= p <= 100):
        return
    # 只前进不回退：有的子进程分几段各自从 0 开始，进度条来回跳
    # 比不动更让人以为坏了。
    if p > (job.get("pct") or 0):
        job["pct"] = p


# ---------------------------------------------------------------- 暂停 / 继续
#
# ★ 为什么是"挂起进程"而不是"在 Python 侧停手"：任务的全部重活都在子进程里
#   （ffmpeg / UVR 分离 / RVC 推理 / 训练）。Python 侧就算停下来不读管道，
#   子进程照样把显卡跑满、照样往磁盘写 —— 那不叫暂停，只是"界面不动了"。
#   Windows 上挂起整棵进程树用 ntdll 的 NtSuspendProcess（Process Explorer
#   同款做法），零依赖、能连孙进程一起停住（shell=True 会先起 cmd.exe，
#   只挂 cmd 而不管它下面的 python，等于没挂）。
#
#   CTRL 不能存进 job（job 要写 JSON），线程同步对象和句柄都不可序列化，
#   所以另开一张按 jid 索引的表。

CTRL = {}
CTRL_LOCK = threading.Lock()


def _ctrl(job):
    with CTRL_LOCK:
        c = CTRL.get(job["id"])
        if c is None:
            c = {"pause": threading.Event(), "handles": [], "paused": False,
                 "pids": [], "seen_pids": [], "cancelled": False}
            c["pause"].set()          # set = 允许继续跑
            CTRL[job["id"]] = c
        return c


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    PROCESS_SUSPEND_RESUME = 0x0800
    PROCESS_TERMINATE = 0x0001
    _kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateProcess.restype = wintypes.BOOL

    class _PROCESSENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_char * 260)]

    # ★ argtypes 必须显式声明：不声明时 ctypes 默认按 C int 传参，
    #   而快照句柄在 64 位下是 8 字节 —— 会被截断成垃圾值，Process32First
    #   直接失败，进程树就永远只有一个根节点。实测症状：按了暂停，
    #   计数还在涨（因为 python 子进程根本没被挂起）。
    _kernel32.Process32First.argtypes = [wintypes.HANDLE,
                                         ctypes.POINTER(_PROCESSENTRY32)]
    _kernel32.Process32First.restype = wintypes.BOOL
    _kernel32.Process32Next.argtypes = [wintypes.HANDLE,
                                        ctypes.POINTER(_PROCESSENTRY32)]
    _kernel32.Process32Next.restype = wintypes.BOOL


def _tree_pids(root):
    """root 自己 + 所有后代进程的 pid。非 Windows 或快照失败就只返回 root。"""
    if os.name != "nt":
        return [root]
    TH32CS_SNAPPROCESS = 0x00000002
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or int(snap) in (-1, 0xFFFFFFFFFFFFFFFF):
        return [root]
    try:
        pe = _PROCESSENTRY32()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        parent = {}
        ok = _kernel32.Process32First(snap, ctypes.byref(pe))
        while ok:
            parent[int(pe.th32ProcessID)] = int(pe.th32ParentProcessID)
            ok = _kernel32.Process32Next(snap, ctypes.byref(pe))
    finally:
        _kernel32.CloseHandle(snap)
    out = {root}
    changed = True
    while changed:
        changed = False
        for pid, ppid in parent.items():
            if ppid in out and pid not in out:
                out.add(pid)
                changed = True
    return sorted(out)


def _open_all(pids):
    hs = []
    if os.name != "nt":
        return hs
    for pid in pids:
        h = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, int(pid))
        if h:
            hs.append(h)
    return hs


def _suspend_pids(pids):
    """挂起这些 pid **以及它们的全部后代**，返回句柄列表（恢复时要用同一批）。

    ★ 必须连后代一起挂：run() 用 shell=True 起命令，Windows 会先拉一个
    cmd.exe，真正干活的是它下面的 python.exe —— 只挂 cmd 等于没挂。
    实测（探针 _probe_pause.py）：只挂根进程时，按了暂停计数照涨。
    """
    allp = set()
    for pid in pids or []:
        allp.update(_tree_pids(pid))
    hs = _open_all(sorted(allp))
    if os.name == "nt":
        for h in hs:
            with contextlib.suppress(Exception):
                _ntdll.NtSuspendProcess(h)
    return hs


def _resume_handles(hs):
    if os.name != "nt":
        return
    for h in hs or []:
        with contextlib.suppress(Exception):
            _ntdll.NtResumeProcess(h)
        with contextlib.suppress(Exception):
            _kernel32.CloseHandle(h)


def _kill_pids(pids):
    """终止这些 pid **以及它们的全部后代**，返回真正发出终止的个数。

    ★ 顺序有讲究：先杀子孙、最后杀根。反过来先杀 cmd.exe，它底下那个
      python 会变成孤儿继续占着显卡 —— 而"点了取消显卡还在跑"正是
      取消功能最不能出的洋相。

    ★ 绝不杀自己：pids 里混进当前进程 pid 的可能性不是零（进程树快照
      是全局的，同一台机器上跑着多个 python），杀掉自己等于服务直接没。
    """
    roots = set(int(p) for p in (pids or []) if p)
    allp = set()
    for pid in roots:
        allp.update(_tree_pids(pid))
    allp.discard(os.getpid())
    if os.name != "nt":
        n = 0
        for pid in sorted(allp):
            with contextlib.suppress(Exception):
                os.kill(pid, 9)
                n += 1
        return n
    n = 0
    # 根最后杀：sorted 里把 root 排到末尾
    order = sorted(allp - roots) + sorted(allp & roots)
    for pid in order:
        h = _kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            continue
        try:
            if _kernel32.TerminateProcess(h, 1):
                n += 1
        finally:
            _kernel32.CloseHandle(h)
    return n


def _paused_extra(job):
    """这个任务累计被暂停了多久（秒）。

    已用时长 / 预计剩余都必须扣掉它 —— 否则用户暂停十分钟再回来，
    会看到"已用 28 分钟"（实际只跑了 18 分钟），ETA 也跟着错。
    """
    t = job.get("_paused_total") or 0
    if job.get("_paused_at"):
        t += time.time() - job["_paused_at"]
    return t


def pause_job(jid):
    """挂起任务：连子进程带孙进程一起停住。返回 (ok, 说明)。"""
    job = get_job(jid)
    if not job:
        return False, "没有这个任务"
    # 只拒绝"已经结束"的任务。**不要**写成 `!= "running"` —— 那会把
    # queued（刚建好还没进 wrapper）这种边界状态一并拒掉，
    # 症状是"按了暂停没反应"，而真正的原因藏在返回值里没人看。
    if job.get("state") in ("done", "failed", "interrupted"):
        return False, "这个任务已经结束了"
    c = _ctrl(job)
    if c["paused"]:
        return True, "已经在暂停中"
    c["pause"].clear()
    c["paused"] = True
    c["handles"].extend(_suspend_pids(c.get("pids") or []))
    job["paused"] = True
    job["_paused_at"] = time.time()
    log(job, "|| 已暂停 —— 子进程已经挂起，显卡/CPU 会空出来，随时可以继续。")
    save_job(job)
    return True, "已暂停"


def resume_job(jid):
    job = get_job(jid)
    if not job:
        return False, "没有这个任务"
    c = _ctrl(job)
    if not c["paused"]:
        return True, "本来就在运行"
    _resume_handles(c["handles"])
    c["handles"] = []
    c["paused"] = False
    c["pause"].set()
    job["paused"] = False
    if job.get("_paused_at"):
        job["_paused_total"] = (job.get("_paused_total") or 0) + (time.time() - job["_paused_at"])
        job["_paused_at"] = None
    log(job, ">> 已继续（累计暂停 %.0f 秒）" % (job.get("_paused_total") or 0))
    save_job(job)
    return True, "已继续"


def cancel_job(jid, cleanup=False):
    """取消任务：杀掉整棵进程树，并把任务落成 cancelled。返回 (ok, 说明)。

    和暂停的区别要讲清楚，因为两者按钮挨在一起：
      暂停 = 挂起，随时能接着跑（进度和已跑出来的东西都还在）；
      取消 = 不要了，进程直接终止，已经跑出来的中间产物也不再有用。

    ★ 必须先"恢复"再杀：暂停时我们手里握着进程的挂起句柄，
      直接杀掉进程而句柄不释放，会一路泄漏到服务重启。
      另外挂起中的进程虽然能被 TerminateProcess 杀掉，
      但先恢复能让它把自己打开的文件句柄正常关掉，少留几个锁死的临时文件。

    cleanup=True 时，连这个任务已经产生的中间产物一起删（work 目录）。
    真正的删除**不在这一刻做** —— 线程可能还握着文件句柄、还在往里写，
    现在删要么删不掉要么把线程搞崩。改成记一个标记，等 wrapper 收尾时再删。
    """
    job = get_job(jid)
    if not job:
        return False, "没有这个任务"
    if job.get("state") in ("done", "failed", "cancelled", "interrupted"):
        return False, "这个任务已经结束了"
    c = _ctrl(job)
    c["cancelled"] = True
    c["pause"].set()          # 别让还在等"允许继续"的地方一直卡着
    if c["paused"]:
        _resume_handles(c["handles"])
        c["handles"] = []
        c["paused"] = False
        job["paused"] = False
        if job.get("_paused_at"):
            job["_paused_total"] = (job.get("_paused_total") or 0) + (
                time.time() - job["_paused_at"])
            job["_paused_at"] = None
    pids = list(c.get("pids") or []) + list(c.get("seen_pids") or [])
    killed = _kill_pids(pids)
    job["state"] = "cancelled"
    job["cancel_requested"] = True
    job["_cleanup_after"] = bool(cleanup)
    job["ended"] = _now()
    log(job, "✕ 已取消 —— 终止了 %d 个进程（含子进程）。已经跑出来的部分不再保留。"
        % killed)
    save_job(job)
    return True, "已取消（终止 %d 个进程）" % killed


def job_cancelled(job):
    """这个任务被取消了吗。和 job_paused 一样：只读 CTRL，绝不碰 JOBS_LOCK。"""
    if not isinstance(job, dict):
        job = get_job(job)
    if not job:
        return False
    with CTRL_LOCK:
        c = CTRL.get(job.get("id"))
    return bool(c and c.get("cancelled"))


def job_paused(job):
    """这个任务当前是否被暂停。

    ★ 只读 CTRL，**绝不**去 get_job —— 因为 job_public 会在持有 JOBS_LOCK 时
    被调用（list_jobs 就是 `with JOBS_LOCK: [job_public(j) ...]`），
    而 get_job 自己也要 JOBS_LOCK。threading.Lock 不可重入，于是直接死锁：
    整个服务卡死，连 POST /api/jobs 都不再响应（实测就是这么炸的，
    而服务日志一片安静，看起来像"请求没进来"）。
    参数可以是 job 字典，也可以是 jid。
    """
    if not isinstance(job, dict):
        job = get_job(job)
    if not job:
        return False
    # 只读、不创建：job_public 会遍历全部历史任务，用 _ctrl() 会给 58 个
    # 早就结束的任务都建一条 CTRL，纯属浪费。
    with CTRL_LOCK:
        c = CTRL.get(job.get("id"))
    return bool(c and c["paused"])



def run(job, cmd, cwd=GSV_ROOT, env=None, timeout=None, on_line=None,
        heartbeat=None):
    """跑一条命令，逐行把输出写进任务日志。

    on_line: 每行输出后的回调。训练/推理进度就靠它 —— 以前这里只管把行丢进日志，
    结果那几十分钟里 pct 一直是 0，界面上的进度条**纹丝不动**，
    用户完全没法确认"还在跑吗、跑到哪了"。

    env: 覆盖环境变量。**值为 None 表示删掉这个变量** —— 这样验收脚本才能
    干净地关掉 PYTHONUNBUFFERED 去做负对照（不用猜"空串算不算开"）。

    heartbeat=(预计秒数, 说明): 给"自己不打进度"的命令**估**一个进度。
    为什么需要：RVC 的推理链里连 tqdm 都没有，整首歌跑 18 分钟一句话不说，
    进度条永远是 0 —— 用户的原话是"看不见进度以及做到哪一步"。
    这里按"这首歌实测大约要多久"线性外推，**封顶 90%**（剩下的 10% 留给真正结束），
    并在 detail 里明说这是估算。估错也没关系：它只会让进度条动起来，
    真正作数的仍然是结束时的 100%。
    """
    e = base_env()
    if env:
        for k, v in env.items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
    log(job, "  $ " + (cmd if isinstance(cmd, str) else " ".join(cmd)))
    # ★ 取消检查（起进程之前）：上一步的进程刚被杀掉、这一步马上又要起一个 ——
    #   不在这里拦，用户点了取消任务照样往下跑（实测：取消了还在做分离）。
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    t0 = time.time()
    p = subprocess.Popen(
        cmd, cwd=cwd, env=e, shell=isinstance(cmd, str),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    hb_stop = threading.Event()
    c = _ctrl(job)
    # 把当前子进程登记进 CTRL：暂停/取消按钮点下来时，要动的正是它（以及它的子孙）。
    c["pids"] = [p.pid]
    # seen_pids 是**累积**的：任务被取消的那一刻可能正好卡在两步之间的空档
    # （pids 已被清成空），只杀 c["pids"] 就会漏掉刚跑完那一步留下的孤儿。
    c["seen_pids"] = sorted(set(c.get("seen_pids") or []) | {p.pid})
    if job_cancelled(job):
        # 用户在 Popen 之后、读输出之前按了取消 ⇒ 立刻终止，别等它跑完
        _kill_pids([p.pid])
        raise Cancelled("任务已被取消")
    if c["paused"]:
        # 用户在两步之间的空档按了暂停 ⇒ 这个刚起的子进程要**立刻**挂起，
        # 否则会出现"明明暂停了显卡还在跑"的鬼故事。
        c["handles"].extend(_suspend_pids([p.pid]))
    if heartbeat:
        exp, label = heartbeat

        def _hb():
            while not hb_stop.wait(15):
                if job.get("state") in ("done", "failed", "interrupted"):
                    return
                if c["paused"]:
                    # 暂停期间不推进进度、也不改 eta —— 否则"还剩几分钟"会一直
                    # 往下掉，恢复时变成一个不对的数。
                    continue
                el = time.time() - t0 - _paused_extra(job)
                pct = int(min(90, el / max(1.0, exp) * 100))
                if pct > (job.get("pct") or 0):
                    job["pct"] = pct
                job["eta"] = int(max(0, exp - el))
                job["detail"] = label

        threading.Thread(target=_hb, daemon=True).start()
    out = []
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            out.append(line)
            log(job, "    " + line)
            if on_line:
                try:
                    on_line(line)
                except Exception:
                    # 进度解析不该有能力弄崩训练本身。解析失败就当这行没进度。
                    pass
        p.wait(timeout=timeout)
    finally:
        hb_stop.set()
        c["pids"] = []
    log(job, "  -> 退出码 %d，用时 %.1fs" % (p.returncode, time.time() - t0))
    # ★ 取消检查（进程结束之后）：子进程是被我们杀掉的，退出码必然非 0。
    #   不在这里拦的话，各步骤会把"进程被杀"当成"命令失败"，
    #   于是用户取消一个任务，看到的是"失败：RVC 推理失败（退出码 1）"
    #   —— 他会对着自己亲手取消的任务去找 bug。
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    return p.returncode, "\n".join(out)


class Fail(Exception):
    pass


class Cancelled(Exception):
    """任务被用户取消。

    为什么不能只靠"杀进程"：流水线是一串步骤，每步自己起一个子进程。
    杀掉当前这一步的进程，下一步会**立刻再起一个**——用户点了取消，
    结果任务照跑（实测：取消后 UVR 分离接着开跑，显卡还是满的）。
    所以取消必须同时在两条线上生效：
      ① 杀掉当前进程树（连子孙，shell=True 下真正干活的是孙进程）；
      ② 在每个步骤边界抛出本异常，让 wrapper 把任务落成 cancelled。
    cancelled 和 failed 要分开：failed 意味着"去看日志排错"，
    cancelled 是"我不要了"，混在一起会让用户对着自己取消的任务去找 bug。
    """


# ---------------------------------------------------------------- 角色库


def _now():
    return time.strftime("%Y-%m-%d %H:%M")


def _roles_backup_path():
    return ROLES_JSON + ".bak"


def load_roles():
    """读角色库。

    **解析失败不再静默返回空列表** —— 这是真事故的根源：
    `except: return {"roles": []}` 会让下一次任何写入（建角色/改角色）
    把**空列表**原样存回磁盘，于是磁盘上的角色目录还在、元数据却全没了
    （实测角色 A/B 的切片、成品、训练数据都完好，只有索引被抹平）。
    ⇒ 解析失败时先自动备份坏文件，再从**磁盘上的角色目录**重建索引，
      并且**拒绝**返回一个"看起来正常"的空库。
    """
    if not os.path.exists(ROLES_JSON):
        return {"roles": []}
    try:
        db = json.load(open(ROLES_JSON, encoding="utf-8"))
        if isinstance(db, dict) and isinstance(db.get("roles"), list):
            return db
        raise ValueError("结构不对：不是 {roles:[...]}")
    except Exception as e:
        # 保留现场，别删——用户可能还要手工抢救
        try:
            bad = ROLES_JSON + ".corrupt-%d" % int(time.time())
            shutil.copy2(ROLES_JSON, bad)
            sys.stderr.write("[角色库] 解析失败(%s)，已备份到 %s，改为从磁盘重建\n"
                             % (e, bad))
        except Exception:
            pass
        rebuilt = rebuild_roles_from_disk()
        if rebuilt:
            return rebuilt
        # 重建不出来也不能悄悄给空库：宁可让调用方报错，也别覆盖真数据
        raise RuntimeError("角色库损坏且无法从磁盘重建：%s" % ROLES_JSON)


def rebuild_roles_from_disk():
    """扫描 data/roles/*/ 把每个角色目录重新登记成一条记录。

    切片、成品、训练数据都在磁盘上，只有索引丢了 —— 所以索引是**可重建**的。
    重建出的记录会从磁盘把段数/时长/成品数如实数出来，不编数据。
    """
    root = os.path.join(DATA, "roles")
    if not os.path.isdir(root):
        return None
    out = []
    for rid in sorted(os.listdir(root)):
        d = os.path.join(root, rid)
        if not os.path.isdir(d) or not rid.startswith("r"):
            continue
        slices = []
        sd = os.path.join(d, "slices")
        if os.path.isdir(sd):
            for fn in sorted(os.listdir(sd)):
                if not fn.lower().endswith((".wav", ".mp3", ".flac")):
                    continue
                fp = os.path.join(sd, fn)
                dur = 0.0
                try:
                    dur = _probe_dur_safe(fp)
                except Exception:
                    dur = 0.0
                slices.append({"file": fn, "dur": round(dur, 3),
                               "text": _asr_lookup(d, fn), "voiced": 1.0})
        finals = []
        fd = os.path.join(d, "final")
        if os.path.isdir(fd):
            for fn in sorted(os.listdir(fd)):
                if fn.lower().endswith((".wav", ".mp3", ".flac")):
                    finals.append({"wav": fn, "at": _now(),
                                   "text": os.path.splitext(fn)[0]})
        out.append({
            "id": rid,
            "name": "角色 %s" % rid[-4:],
            "intro": "",
            "kind": "speech",
            "kind_locked": True,      # 重建的不要被自动预判乱改
            "color": PALETTE[len(out) % len(PALETTE)],
            "source": "从磁盘重建",
            "created": _now(),
            "segments": slices,
            "ref": None,
            "model": None,
            "rvc": None,
            "outputs": finals,
            "stats": {"segments": len(slices),
                      "seconds": round(sum(s["dur"] for s in slices), 1)},
            "rebuilt": True,
        })
    if not out:
        return None
    db = {"roles": out}
    save_roles(db)
    return db


def _probe_dur_safe(path):
    """尽量量出时长：先试 wave（零依赖、秒回），不行再退 ffprobe。"""
    import contextlib
    try:
        with contextlib.closing(wave.open(path, "rb")) as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        pass
    import subprocess
    # ★ 必须用**全路径**：本机的机器级 PATH 被改坏过（%SystemRoot% 整组缺失），
    #   裸名调系统 exe 会 ENOENT，而这里 ENOENT 会被 except 吃掉 ⇒ 静默返回 0.0，
    #   症状是"mp3 的时长永远是 0"（分配表整片 0 秒、逐段指定没法用）。
    exe = FFPROBE if os.path.isfile(FFPROBE) else "ffprobe"
    try:
        r = subprocess.run([exe, "-v", "quiet", "-show_entries",
                            "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, timeout=20)
        return float((r.stdout or b"0").decode().strip() or 0)
    except Exception:
        return 0.0


def _song_total_secs(song_path, cached_dur=0.0, vocal_wav=None):
    """这首歌「到底多长」—— 三个来源取最大，任何一个读得到就不会是 0。

    ★ 为什么不能只用 `wav_seconds`（2026-10-27 实测踩到，用户的歌是 mp3）：
      它只走 `wave` 模块，**只认整型 PCM wav**。用户的歌大多是 mp3/m4a/flac，
      `wave.open()` 直接抛异常、被里面 `except: return 0.0` 吃掉 ⇒ 时长恒 0。
      症状不是"报个错"，是**多角色合唱第 2 步整条失败**：
        「排不出分配表：这首歌的时长不明（分离后的干声读不出来）」
      —— 而那句文案还说错了对象：干声明明是 197.504 秒、标准 16-bit PCM wav。
      同一首歌实测：`wav_seconds`=**0.0**，`_probe_dur_safe`=**197.544**。

    ★ 为什么是这三个来源：
      1) 原曲 —— 最贴近"这首歌多长"，但可能是 mp3（必须走 ffprobe）；
      2) 歌词缓存 —— 认过词就有，跟原曲格式无关；
      3) **分离后的干声** —— 最权威：它才是真正被唱的那条轨，而且它一定是
         分离器写出来的 PCM wav，必然量得出来。
    取 max 而不是"按优先级取第一个非 0"：三者本来就该相等，
    取 max 不怕某个来源偏短（比如尾部被截的干声）。

    返回 (秒数, 明细 dict)。明细是给日志和报错文案用的 —— 量不出时要说清楚
    **到底哪几个来源是 0**，不许含糊地说"读不出来"（上一版就是这么误导人的）。
    """
    def _d(p):
        if not p:
            return 0.0
        try:
            return float(_probe_dur_safe(p) or 0.0)
        except Exception:                                   # noqa: BLE001
            return 0.0

    detail = {
        "song": _d(song_path),
        "lyric": float(cached_dur or 0.0),
        "vocal": _d(vocal_wav),
    }
    return max(detail.values()), detail


def _asr_lookup(role_dir_path, fname):
    """从 asr.list 里取这段切片识别出来的文字。"""
    lst = os.path.join(role_dir_path, "asr.list")
    if not os.path.isfile(lst):
        return ""
    stem = os.path.splitext(fname)[0]
    try:
        for line in open(lst, encoding="utf-8"):
            parts = line.rstrip("\n").split("|", 1)
            if len(parts) == 2 and parts[0].strip() == stem:
                return parts[1].strip()
    except Exception:
        pass
    return ""


def save_roles(db):
    # 每次写之前先留一份上一版。没有这道保险，上面那个"空库覆盖真数据"
    # 就是**不可逆**的 —— 磁盘目录还在也救不回元数据（切片能重建，文字/模型信息不能）。
    try:
        if os.path.exists(ROLES_JSON) and os.path.getsize(ROLES_JSON) > 0:
            shutil.copy2(ROLES_JSON, _roles_backup_path())
    except Exception:
        pass
    tmp = ROLES_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, ROLES_JSON)


def get_role(rid):
    for r in load_roles()["roles"]:
        if r["id"] == rid:
            return r
    return None


# 「这个角色现在能干什么」。
#
# 为什么要有它：装了 17 个市场角色之后，卡片上一律写着「未训练」，而「未训练」这
# 三个字说的是「没有**说话**模型」—— 这些角色的歌声模型是现成能用的，用户看到
# 「未训练」只会以为整张卡都是废的。反过来，卡片上那个「💬 TA 说话」按钮对它们
# 又是**点了必然失败**的（speak_role 第一句就校验 model），用户要输入文字、点合成、
# 看着任务跑起来、等几秒才收到一句红字。入口能不能走通，必须在点之前就看得见。
#
# 两条铁律：
#   ① **不持久化**。能力是「当前状态的读数」，不是角色的属性 —— 素材一加、模型一训
#      就该变。写进 roles.json 就会出现过期的「可说话」（铁律 12d）。
#   ② 判据必须跟真正执行任务的那段代码**同一个**（speak_role 看 role["model"]、
#      sing_job 看 role["rvc"]["model"] 且素材 ≥10 段）。这里放宽一点，用户就会
#      看到一个绿色的「能用」然后点下去失败 —— 那比不显示更糟。
_ABILITY_MIN_SEG = 10          # 与 sing_job 的 10 段门槛保持一致


def role_ability(r):
    """实时算这个角色「现在能干什么 / 还差什么」。返回可直接给前端用的字典。"""
    segs = [s for s in (r.get("segments") or [])
            if float(s.get("dur", 0) or 0) >= 2.0]
    n = len(segs)
    has_speak = bool(r.get("model"))
    has_sing = bool((r.get("rvc") or {}).get("model"))
    # ★ 第二套引擎：一张卡只挂 SVC 权重时也能唱（卡名带「SVC」后缀的那几张）。
    #   这里不改的话，SVC 卡在界面上会被判成「还不能唱歌 / 没有歌声模型」——
    #   而实际上 sing_job + engine=svc 完全能跑（2026-10-27 实测整曲 75% 吻合）。
    has_svc = bool((r.get("svc") or {}).get("model"))
    enough = n >= _ABILITY_MIN_SEG

    if has_speak:
        speak = {"state": "ready", "label": "可说话", "why": "说话模型已经训好了"}
    elif enough:
        speak = {"state": "trainable", "label": "训练后可说话",
                 "why": "还没有说话模型；现在有 %d 段素材，够训一个（要几分钟）" % n}
    else:
        speak = {"state": "blocked", "label": "还不能说话",
                 "why": "还没有说话模型，素材也只有 %d 段（至少 %d 段）"
                        % (n, _ABILITY_MIN_SEG)}

    if has_sing:
        sing = {"state": "ready", "label": "可唱歌", "why": "已经有歌声模型，选首歌就能唱"}
    elif has_svc:
        sing = {"state": "ready", "label": "可唱歌（SVC）",
                "why": "这张卡挂的是第二套引擎（so-vits-svc 4.0）的权重，"
                       "唱歌时引擎选 SVC 就能唱，不需要 RVC 模型"}
    elif enough:
        sing = {"state": "trainable", "label": "可唱歌（先训一次）",
                "why": "还没有歌声模型；现在有 %d 段素材，第一次唱会先训一个" % n}
    else:
        sing = {"state": "blocked", "label": "还不能唱歌",
                "why": "没有歌声模型，素材也只有 %d 段（至少 %d 段）"
                       % (n, _ABILITY_MIN_SEG)}

    # 给一句「下一步该干嘛」——按「能拿到的结果最直接」排：
    # 已经能出东西就别再让用户去导素材。
    if speak["state"] == "ready" or sing["state"] == "ready":
        nxt = {"code": "ready", "label": "可以直接用",
               "hint": "这个角色现在就能出成品。"}
    elif enough:
        nxt = {"code": "train", "label": "去训练",
               "hint": "素材够了，跑一次训练就能说话 / 唱歌。"}
    else:
        nxt = {"code": "import", "label": "先导入素材",
               "hint": "导入一段这个人的视频或音频，切好片就能训练。"}

    return {
        "segments_ok": n, "need": _ABILITY_MIN_SEG,
        "speak": speak, "sing": sing, "next": nxt,
    }


def ability_summary(roles):
    """角色库顶部的汇总：一眼看出这一堆里哪些能立刻用。"""
    a = [role_ability(r) for r in roles]
    return {
        "total": len(roles),
        "can_speak": sum(1 for x in a if x["speak"]["state"] == "ready"),
        "can_sing": sum(1 for x in a if x["sing"]["state"] == "ready"),
        "need_data": sum(1 for x in a if x["next"]["code"] == "import"),
    }


def _next_color(db):
    return PALETTE[len(db["roles"]) % len(PALETTE)]


def role_dir(rid):
    return os.path.join(ROLES_DIR, rid)


def create_role(name=None, kind="speech", source=""):
    db = load_roles()
    rid = "r" + uuid.uuid4().hex[:8]
    d = role_dir(rid)
    for sub in ("slices", "final"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    role = {
        "id": rid,
        "name": name or ("角色 %d" % (len(db["roles"]) + 1)),
        "intro": "",
        "kind": kind,               # speech / singing
        "kind_locked": False,       # 人工改过之后就不再被自动预判覆盖
        "color": _next_color(db),
        "source": source,
        "created": _now(),
        "segments": [],             # [{file,dur,text,voiced,core}]
        "ref": None,                # {file,text}
        "model": None,              # {ckpt,epochs,at}
        "rvc": None,                # {model,at}
        "outputs": [],              # [{text,wav,at,kind}]
        "voice": None,              # 声纹中心 {center,n,dim,model,at}，用于"加素材时认人"
        "stats": {"segments": 0, "seconds": 0.0},
    }
    db["roles"].append(role)
    save_roles(db)
    return role


def mutate_role(rid, fn):
    """
    原子地「读出来 -> 改 -> 存回去」。

    之前的写法是 `role = get_role(rid); role['x']=1; save_roles(load_roles())`，
    最后那句 load_roles() 又从磁盘读了一遍，等于把刚改的东西原样覆盖掉 ——
    表现是改完不生效（角色段数存进去 63 段，读出来 0 段）。全部改走这里。
    """
    db = load_roles()
    hit = None
    for r in db["roles"]:
        if r["id"] == rid:
            fn(r)
            hit = r
            break
    if hit is None:
        return None
    save_roles(db)
    return hit


def mutate_roles(fn):
    db = load_roles()
    fn(db)
    save_roles(db)
    return db


AVATAR_PX = 512          # 头像边长。卡片上只有 40~64px，但视频里会放到 300px+


def set_role_avatar(rid, raw, ext=".png"):
    """收一张头像：居中裁方 → 缩到 512 → 存成 jpg 挂到角色上。

    ★ 为什么必须裁成方图：角色卡、歌手槽、合唱视频里都是**方形小格子**，
      非方图塞进去会被拉变形（脸变宽/变窄），而用户根本不会想到"是尺寸问题"，
      只会觉得"这个功能把我的图弄丑了"。裁中间那一块并当场告诉他，
      比他先自己裁好再上传省事得多。
    ★ 存 jpg 不存原图：一张手机拍的 4MB png / 一张 20MB 的 gif 挂到角色卡上，
      角色墙一滚就要解码几十张 —— 512 的 jpg 是 40~80KB，秒开。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在：%s" % rid)
    if not raw or len(raw) < 256:
        raise Fail("没收到图片数据（%d 字节）" % len(raw or b""))
    try:
        from PIL import Image
    except Exception as e:                       # noqa: BLE001
        raise Fail("这台机器上的 Python 没有 PIL，装不了头像：%s" % str(e)[:120])

    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception as e:                       # noqa: BLE001
        raise Fail("这不是一张能读的图片（%s）。\n支持 jpg / png / webp / bmp / gif。"
                   % str(e)[:100])

    w, h = im.size
    if w < 16 or h < 16:
        raise Fail("这张图太小了（%dx%d），做头像看不清。" % (w, h))
    side = min(w, h)
    im = im.convert("RGB")
    box = ((w - side) // 2, (h - side) // 2, (w - side) // 2 + side, (h - side) // 2 + side)
    im = im.crop(box)
    if side > AVATAR_PX:
        im = im.resize((AVATAR_PX, AVATAR_PX), Image.LANCZOS)

    d = role_dir(rid)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, "avatar.jpg")
    tmp = dst + ".part"
    im.save(tmp, "JPEG", quality=88, optimize=True)
    os.replace(tmp, dst)                         # 原子替换：写一半失败不会留半个头像

    def _set(r):
        r["avatar"] = dst
        r["avatar_at"] = _now()
    mutate_role(rid, _set)
    return {"ok": True, "avatar": dst, "px": im.size[0],
            "kb": round(os.path.getsize(dst) / 1024.0, 1),
            "cropped": (w != h)}


def clear_role_avatar(rid):
    """把头像撤掉，回到"首字母 + 渐变"那块。图本身也删，不留垃圾。"""
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在：%s" % rid)
    old = role.get("avatar")
    with contextlib.suppress(Exception):
        dst = os.path.join(role_dir(rid), "avatar.jpg")
        if os.path.isfile(dst):
            os.remove(dst)
    def _unset(r):
        r.pop("avatar", None)
        r.pop("avatar_at", None)
    mutate_role(rid, _unset)
    return {"ok": True, "removed": bool(old)}


def update_role(rid, patch):
    db = load_roles()
    for r in db["roles"]:
        if r["id"] == rid:
            for k in ("name", "intro", "kind", "color", "ref", "kind_locked"):
                if k in patch:
                    r[k] = patch[k]
                    # 用户亲手改了名字 ⇒ 这个名字不再是"自动译出来的"，
                    # 以后补译字典时**不许**再把它改回去（那是他说的算的）。
                    if k == "name":
                        r.pop("name_auto", None)
            if patch.get("kind") is not None:
                r["kind_locked"] = True
            r["updated"] = _now()
            save_roles(db)
            return r
    return None


def rvc_artifacts(rid):
    """这个角色在 **RVC 目录**里留下的全部家当：实验目录 / 权重 / 索引。

    注意 RVC 那边的命名：实验目录就是角色 id（`logs/<rid>/`），权重是
    `assets/weights/<rid>*.pth`，索引名里也含 id。三处都对得上，才好精确清。
    """
    out = {"exp": [], "weights": [], "indices": []}
    exp = os.path.join(RVC_ROOT, "logs", rid)
    if os.path.isdir(exp):
        out["exp"].append(exp)
    wdir = os.path.join(RVC_ROOT, "assets", "weights")
    if os.path.isdir(wdir):
        out["weights"] = [os.path.join(wdir, f) for f in os.listdir(wdir)
                          if f.startswith(rid) and f.endswith(".pth")]
    idir = os.path.join(RVC_ROOT, "assets", "indices")
    if os.path.isdir(idir):
        out["indices"] = [os.path.join(idir, f) for f in os.listdir(idir)
                          if rid in f]
    return out


def purge_rvc_artifacts(rid):
    """删角色时，把它在 RVC 那边留下的东西一起清掉。返回 (删掉的, 没删掉的)。

    ★ 不做这件事就是**真金白银的磁盘泄漏**：一个角色的 RVC 实验目录里躺着
      G_*.pth + D_*.pth 约 1 GB、权重 52 MB、索引几 MB；而以前 `delete_role()`
      只删**本项目**的角色目录 ⇒ 用户删 10 个角色，就有 10 GB 静静躺在 RVC 目录里，
      界面上看不见、也没有任何入口再去删它。

    删除失败**不许掀翻调用方**：本机那个批量删除钩子抛的是 `SystemExit`
    （`except Exception` 捕不到，实测掀翻过整个门禁进程），所以这里逐项
    `except BaseException`，最后把没删掉的照实报出来，交给上层/gc 接着清。
    """
    arts = rvc_artifacts(rid)
    removed, left = [], []
    for p in arts["weights"] + arts["indices"]:
        try:
            os.remove(p)
            removed.append(p)
        except BaseException as e:
            left.append((p, "%s: %s" % (type(e).__name__, str(e)[:80])))
    for d in arts["exp"]:
        try:
            shutil.rmtree(d)
            removed.append(d)
        except BaseException as e:
            left.append((d, "%s: %s" % (type(e).__name__, str(e)[:80])))
    if left:
        sys.stderr.write("[warn] 角色 %s 的 RVC 产物没删干净（%d 项）：%s\n"
                         % (rid, len(left),
                            "; ".join("%s (%s)" % (p, why) for p, why in left)))
    return removed, left


def delete_role(rid):
    db = load_roles()
    db["roles"] = [r for r in db["roles"] if r["id"] != rid]
    save_roles(db)
    d = role_dir(rid)
    shutil.rmtree(d, ignore_errors=True)
    # 顺手把 RVC 那边的断点和权重一起清掉（不清就是几个 G 的磁盘泄漏，见上）
    try:
        purge_rvc_artifacts(rid)
    except BaseException as e:                       # 钩子抛 SystemExit，必须这么接
        sys.stderr.write("[warn] 清理角色 %s 的 RVC 产物时出错：%s: %s\n"
                         % (rid, type(e).__name__, str(e)[:120]))
    # ★ `ignore_errors=True` 会**静默吞掉**删除失败（文件被占用、权限不够、或者被本机的
    #   安全策略拦下）。那种情况下角色从界面上消失了、文件却永远留在盘上 ——
    #   用户既看不见、也再没有入口去删它（`orphan_role_dirs()` 就是为此存在的）。
    #   所以这里必须**复核一次**，留残就明确报出来（stderr 会进服务日志）。
    if os.path.isdir(d):
        sys.stderr.write("[warn] 角色 %s 已从角色库移除，但磁盘目录没删干净，"
                         "成了孤儿目录：%s\n" % (rid, d))


def delete_roles(ids):
    """批量删角色：**逐个删、逐个收集失败**，绝不让一个出错把整批掀掉。

    返回 `(deleted, failed)`，failed 里是 `{"id","err"}`。

    为什么要把这个循环单独拿出来（而不是写在 HTTP 处理里）：
      ① 门禁可以**直接调它**做行为判据 —— 把 `delete_role` 换成一个会抛异常的
         假实现，就能验"一个失败不会连累其他、而且照实报出来"。写在
         `_route_post` 里就只能在门禁里查源码字符串，那是假尺子（rule 11c）。
      ② 前端不许循环发 N 个 DELETE：半路失败会留下"删了一半"却没人知道的状态。
         这里一次把完整清单交回去，界面才能照实说"删了 N 个、M 个没删干净"。
    """
    done, failed = [], []
    for rid in ids:
        try:
            delete_role(rid)
            done.append(rid)
        except BaseException as e:                  # 钩子会抛 SystemExit，必须这么接
            failed.append({"id": rid,
                           "err": "%s: %s" % (type(e).__name__, str(e)[:160])})
    return done, failed


def orphan_role_dirs():
    """列出 `data/roles/` 下**不在角色库里**的目录（删角色时 rmtree 失败留下的残骸）。

    为什么需要它：`delete_role` 是先摘登记、再删目录。万一删目录那一步失败（被占用 /
    权限 / 安全策略），角色就不在库里了 ⇒ 界面上看不见、清理面板也扫不到，
    但它占的磁盘还在。这些就是"幽灵目录"。返回 [{id,path,mb}]。
    """
    known = {r["id"] for r in load_roles()["roles"]}
    out = []
    if not os.path.isdir(ROLES_DIR):
        return out
    for n in sorted(os.listdir(ROLES_DIR)):
        p = os.path.join(ROLES_DIR, n)
        if n in known or not os.path.isdir(p):
            continue
        size = 0
        for dp, _dn, fns in os.walk(p):
            for f in fns:
                try:
                    size += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        out.append({"id": n, "path": p, "mb": round(size / 1024.0 ** 2, 1)})
    return out


def refresh_stats(rid):
    db = load_roles()
    for r in db["roles"]:
        if r["id"] == rid:
            segs = r.get("segments", [])
            r["stats"] = {
                "segments": len(segs),
                "seconds": round(sum(s.get("dur", 0) for s in segs), 1),
            }
            save_roles(db)
            return r["stats"]
    return None


def write_list(rid):
    """把角色当前的标注写成 GPT-SoVITS 要的 .list（人工改文本后靠它生效）。"""
    role = get_role(rid)
    d = role_dir(rid)
    rows = []
    for s in role.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text:
            continue
        rows.append("%s|%s|%s|%s" % (
            os.path.join(d, "slices", s["file"]), s["file"], "zh", text))
    p = os.path.join(d, "asr.list")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    return p, len(rows)


# ---------------------------------------------------------------- 任务


JOBS = {}
JOBS_LOCK = threading.Lock()


def new_job(role_id, kind, title, epochs_total=None):
    jid = "j" + uuid.uuid4().hex[:8]
    d = os.path.join(JOBS_DIR, jid)
    os.makedirs(d, exist_ok=True)
    job = {
        "id": jid, "role": role_id, "kind": kind, "title": title,
        "state": "queued", "stage": "", "pct": 0, "started": _now(),
        "ended": None, "error": None, "dir": d,
        # 训练类任务记录总轮数，前端才能画"还剩几轮"而不只是一条百分比
        "epochs_total": epochs_total, "epochs_done": None, "detail": None,
        # steps 让界面能画出「每一步」的清单：名称/状态/耗时/说明。
        # 以前只有一句 stage 文本在变，用户看到的是刷屏的日志，
        # 不知道走到第几步、哪步最慢、卡在哪 —— 这就是他要"看见每一步"的原因。
        "steps": [],
        "_step_idx": -1,
        "_logfile": os.path.join(d, "log.txt"),
    }
    with JOBS_LOCK:
        JOBS[jid] = job
    save_job(job)
    return job


def begin_step(job, name, note="", hint="", eta=None):
    """开始一步。同一批流程里重名的步骤会合并成一条（更新说明），不重复堆。

    hint / eta ——「黑盒等待」的解药。
      以前界面上只有一句干巴巴的步骤名（"语音识别"），配上一条不动的进度条，
      用户不知道这一步在干什么、还要多久，于是反复点、或者以为卡死了。
      hint  = 这一步正在做的事的人话版（"正在识别原词，第 2/5 段"）
      eta   = 这一步预计还要多少秒（循环里用 step_hint 边跑边更新）
    """
    # ★ 步骤边界也要拦取消：run() 只能拦住"正在起子进程"的那一段，
    #   而两步之间还有纯 Python 的活（拷文件、写清单、挑参考音频），
    #   那段要是跑起来，取消就只是"晚一点继续跑"而已。
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    with JOBS_LOCK:
        steps = job.setdefault("steps", [])
        job["_step_idx"] = job.get("_step_idx", -1) + 1
        steps.append({"n": job["_step_idx"] + 1, "name": name,
                      "state": "doing", "note": note,
                      "hint": hint or "", "eta": eta, "at": _now(), "secs": None,
                      # ★ 秒级时间戳：end_step 用它算出这一步真花了多久。
                      #   secs 这个字段前端老早就读了（`${(ds.secs/60)} 分钟`），
                      #   但从写下那天起就没人往里填过 —— 界面上那句永远是空的。
                      "ts": time.time()})
    save_job(job)
    log(job, "【第 %d 步】%s%s" % (job["_step_idx"] + 1, name,
                                  ("　" + note) if note else ""))


def step_hint(job, text, eta=None):
    """给**正在做的那一步**换一句人话 / 更新预计剩余。

    循环里每轮调一次，界面上就会出现「正在识别原词，第 3/12 段 · 约需 1 分 20 秒」。
    它是纯展示层：写不进去也不许影响流程（所以全程 try）。
    """
    try:
        with JOBS_LOCK:
            steps = job.setdefault("steps", [])
            i = job.get("_step_idx", -1)
            if 0 <= i < len(steps):
                if text:
                    steps[i]["hint"] = text
                if eta is not None:
                    steps[i]["eta"] = int(max(0, eta))
        save_job(job)
    except Exception:
        pass


def end_step(job, ok=True, note=""):
    with JOBS_LOCK:
        steps = job.setdefault("steps", [])
        i = job.get("_step_idx", -1)
        if 0 <= i < len(steps):
            steps[i]["state"] = "done" if ok else "fail"
            if note:
                steps[i]["note"] = note
            _stamp_secs(steps[i])
    save_job(job)
    log(job, "　└ %s%s%s" % ("完成" if ok else "失败",
                             ("：" + note) if note else "",
                             _secs_suffix(job)))


def _stamp_secs(step):
    """把这一步的耗时写进 step["secs"]（前端拿它显示"这一步 X 分钟"）。"""
    try:
        t0 = step.get("ts")
        if t0:
            step["secs"] = round(max(0.0, time.time() - float(t0)), 1)
    except Exception:
        pass


def _secs_suffix(job):
    """日志尾部那句"（用了 X 秒）"。纯展示，取不到就不显示。"""
    try:
        steps = job.get("steps") or []
        i = job.get("_step_idx", -1)
        if 0 <= i < len(steps) and steps[i].get("secs") is not None:
            return "（用了 %.1f 秒）" % steps[i]["secs"]
    except Exception:
        pass
    return ""


def fail_step(job, err):
    """失败时把原因**钉在这一步**上 —— 用户一眼能看出卡在哪、为什么。"""
    with JOBS_LOCK:
        steps = job.setdefault("steps", [])
        i = job.get("_step_idx", -1)
        if 0 <= i < len(steps):
            steps[i]["state"] = "fail"
            steps[i]["note"] = str(err)[:400]
            _stamp_secs(steps[i])
    save_job(job)
    log(job, "　└ 失败：%s%s" % (str(err)[:400], _secs_suffix(job)))


def job_public(job):
    d = {k: v for k, v in job.items() if k not in ("_logfile", "dir", "_step_idx", "_dirty")}
    # result 是任务自己塞的，可能带 Path / 其它不可序列化的东西。
    # 不做这层清洗的话，GET /api/job/<id> 会直接 500，前端就再也拿不到任务状态了 ——
    # 症状是"任务卡住不动"，根因却是序列化，跟任务本身无关。
    try:
        json.dumps(d.get("result"))
    except Exception:
        d["result"] = None
    # 自动歌词的结果同理（它是一堆 dict，字段一旦带 Path 就会让整个任务状态拿不到）
    if d.get("lyric_result") is not None:
        try:
            json.dumps(d["lyric_result"])
        except Exception:
            d["lyric_result"] = None
    # 已用时长 / 预计剩余：用户判断"还要等多久、是不是卡死了"全靠这两个数。
    # 以前只有 state 和 pct，训练几十分钟 pct 一直是 0（见 step_train 的进度解析），
    # 界面上等于一片死寂 —— 用户没法确认进度。
    d["elapsed"] = _job_elapsed(job)
    d["eta"] = _job_eta(job)
    # 暂停状态：界面要靠它把按钮切成「继续」、并把状态写成"已暂停"。
    d["paused"] = job_paused(job)
    return d


def _ts(s):
    """把 'YYYY-MM-DD HH:MM' 解析成时间戳；解析不了就返回 None（不猜）。"""
    if not s:
        return None
    try:
        return time.mktime(time.strptime(str(s), "%Y-%m-%d %H:%M"))
    except Exception:
        return None


def _job_elapsed(job):
    a = _ts(job.get("started"))
    b = _ts(job.get("ended")) or time.time()
    if a is None:
        return None
    # 扣掉暂停时间：用户要的是"实际跑了多久"，暂停的十几分钟不该算进去。
    return max(0, int(b - a - _paused_extra(job)))


def _job_eta(job):
    """按已用时间 / 已完成比例外推剩余秒数。样本不足就不给，别显示一个假数字。"""
    if job.get("state") != "running":
        return None
    pct = job.get("pct") or 0
    el = _job_elapsed(job)
    # 少于 8% 的样本外推误差太大，宁可不说 —— 宁可没 ETA，也不要一个看着精确的假 ETA
    if not el or pct < 8:
        return None
    return int(el * (100.0 - pct) / pct)


# ---------------------------------------------------------------- 任务落盘
#
# JOBS 以前是纯内存 dict：服务一重启，正在跑的任务和全部历史一起消失。
# 训练动辄几十分钟，重启一次用户就彻底失明、也没法查上次到底跑到哪。
# 现在每个任务在 data/jobs/<id>/job.json 留一份，服务启动时重建索引。

def _job_file(d):
    return os.path.join(d, "job.json")


def save_job(job):
    """把任务写回磁盘。原子写 + 失败只记不抛 —— 落盘不该拖垮正在跑的训练。"""
    d = {
        "id": job["id"], "role": job.get("role"), "kind": job.get("kind"),
        "title": job.get("title"), "state": job.get("state"),
        "stage": job.get("stage"), "pct": job.get("pct") or 0,
        "detail": job.get("detail"),
        "started": job.get("started"), "ended": job.get("ended"),
        "error": job.get("error"), "steps": job.get("steps") or [],
        "epochs_total": job.get("epochs_total"),
        "epochs_done": job.get("epochs_done"),
        # 暂停状态也落盘：刷新页面/重开界面时按钮得是「继续」而不是「暂停」。
        # （注意：_paused_at 不落盘 —— 服务重启后子进程早就没了，
        #  把"正在暂停中"带回来只会让它永远停在那，所以恢复时一律当未暂停。）
        "paused": bool(job.get("paused")),
        "_paused_total": job.get("_paused_total") or 0,
    }
    try:
        p = _job_file(job["dir"])
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
    except Exception as e:
        log(job, "  [warn] 任务落盘失败：%s" % e)


def load_jobs():
    """启动时把磁盘上的任务读回内存。

    重启前是 running/queued 的，进程已经死了 —— 必须**标成中断**，
    不能让它继续显示"训练中"。否则用户会对着一个永远不会结束的进度条干等，
    这正是"看不见进度"最糟的一种形式：看着在跑，其实在几小时前就死了。
    """
    out = []
    try:
        names = sorted(os.listdir(JOBS_DIR))
    except Exception:
        return out
    for n in names:
        p = os.path.join(JOBS_DIR, n, "job.json")
        if not os.path.isfile(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(d, dict) or not d.get("id"):
            continue
        if d.get("state") in ("running", "queued"):
            d["state"] = "interrupted"
            d["error"] = "服务在 %s 重启，这个任务被中断了" % _now()
            d["ended"] = d.get("ended") or _now()
            # 步骤清单里最后一条"doing"要落到失败态，否则界面上永远挂着一个转圈
            for s in reversed(d.get("steps") or []):
                if s.get("state") == "doing":
                    s["state"] = "fail"
                    s["note"] = "服务重启，任务中断在这一步"
                    break
        job = dict(d)
        job["dir"] = os.path.join(JOBS_DIR, job["id"])
        job["_logfile"] = os.path.join(job["dir"], "log.txt")
        job["_step_idx"] = len(job.get("steps") or []) - 1
        out.append(job)
    return out


def restore_jobs():
    with JOBS_LOCK:
        for j in load_jobs():
            JOBS[j["id"]] = j
    return len(JOBS)


def list_jobs():
    with JOBS_LOCK:
        return [job_public(j) for j in JOBS.values()]


def get_job(jid):
    with JOBS_LOCK:
        return JOBS.get(jid)


def job_log(jid, offset=0):
    job = get_job(jid)
    if not job:
        return "", 0
    p = job["_logfile"]
    if not os.path.exists(p):
        return "", 0
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        txt = f.read()
    return txt[offset:], len(txt)


def _cleanup_job_work(job):
    """删掉这个任务自己产生的中间产物，返回释放字节数。

    只删内容、**保留 job.json 和 log.txt**：用户取消完还想回来看一眼
    "上次取消的是哪一个、跑到哪一步"，把整个任务目录端掉等于把证据烧了，
    任务中心里那条也会变成打不开的死链接。
    """
    d = job.get("dir")
    if not d or not os.path.isdir(d):
        return 0
    keep = {"job.json", "log.txt"}
    freed = 0
    for name in sorted(os.listdir(d)):
        if name in keep or name.startswith("."):
            continue
        p = os.path.join(d, name)
        try:
            sz = _path_size(p)
            _rm_path(p)
            freed += sz
        except Exception as e:
            log(job, "  [warn] 中间产物没清干净（%s）：%s" % (name, e))
    return freed


def run_in_thread(job, fn):
    def wrapper():
        job["state"] = "running"
        save_job(job)
        try:
            # 任务函数的返回值存进 job["result"]：前端要靠它拿到"到底产出了什么"
            #（比如导歌的真实落盘路径），否则只能靠猜 —— 猜就是"靠运气覆盖"。
            job["result"] = fn()
            job["state"] = "done"
            job["pct"] = 100
        except Cancelled as e:
            # ★ 取消不是失败。写成 failed 的话，用户会对着一个自己亲手取消的
            #   任务去翻日志找原因，而日志最后一行就是他自己点的那次取消。
            job["state"] = "cancelled"
            job["error"] = str(e) or "已被用户取消"
            log(job, "")
            log(job, "✕ 任务已取消。")
        except Fail as e:
            job["state"] = "failed"
            job["error"] = str(e)
            log(job, "")
            log(job, "! " + str(e))
        except Exception as e:
            import traceback

            job["state"] = "failed"
            job["error"] = "%s: %s" % (type(e).__name__, e)
            log(job, traceback.format_exc())
        job["ended"] = _now()
        # 最后一条还在 doing 的步骤必须落成终态 —— 否则界面永远挂着一个"正在做"
        with JOBS_LOCK:
            for s in reversed(job.get("steps") or []):
                if s.get("state") == "doing":
                    if job.get("state") == "cancelled":
                        s["state"] = "cancel"
                        if not s.get("note"):
                            s["note"] = "在这一步被取消"
                    else:
                        s["state"] = "fail"
                        if not s.get("note"):
                            s["note"] = (job.get("error") or "任务结束在这一步")[:400]
                    break
        # 取消时要求"连中间产物一起删"的，到这里才真删：此刻线程已经收尾，
        # 文件句柄都放开了，不会出现"删不掉"或"把还在写的目录端掉"。
        if job.get("_cleanup_after"):
            freed = _cleanup_job_work(job)
            if freed:
                log(job, "✕ 中间产物已清掉，释放 %s。" % _humansize(freed))
        save_job(job)
        # 收尾后把 CTRL 里这条丢掉：它还握着 pause Event 和一堆 pid，
        # 留着只会让 job_paused 对一个早就结束的任务返回奇怪的值。
        with CTRL_LOCK:
            CTRL.pop(job.get("id"), None)

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    return job


# ---------------------------------------------------------------- 流水线各步


def step_audio(job, video, out_wav):
    job["stage"] = "抽取音频"
    rc, out = run(job, '"%s" -i "%s" -vn -acodec pcm_s16le -ac 2 -ar 44100 "%s" -y'
                  % (FFMPEG, video, out_wav), timeout=1800)
    if rc != 0 or not os.path.exists(out_wav):
        raise Fail("抽音频失败，视频可能没有音轨或格式不认识。")
    secs = wav_seconds(out_wav)
    log(job, "  音频 %.1f 秒" % secs)
    if secs < 10:
        raise Fail("音频只有 %.1f 秒，太短了，训不出音色（至少要 1 分钟以上）。" % secs)
    return out_wav


def step_uvr(job, audio_wav, work, enabled=True, model=None):
    """导入链里的分离：跟唱歌链**同一套选优**（以前这里写死 HP5，而唱歌链用
    Roformer —— 同一份素材两条链分出来的质量不一样，训练出来的模型也跟着飘）。
    """
    job["stage"] = "分离人声"
    vocal_dir = os.path.join(work, "02_vocal")
    os.makedirs(vocal_dir, exist_ok=True)
    if not enabled:
        shutil.copy(audio_wav, os.path.join(vocal_dir, "vocal.wav"))
        return vocal_dir
    in_dir = os.path.join(work, "_uvr_in")
    inst_dir = os.path.join(work, "02_inst")
    for d in (in_dir, inst_dir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    shutil.copy(audio_wav, os.path.join(in_dir, "source.wav"))

    chosen = model or UVR_MODEL_BEST
    if not model:
        rows = _sep_probe(job, audio_wav, work, _sep_candidates())
        if rows:
            chosen = rows[0]["model"]
            log(job, "  ⇒ 用 %s（得分 %.1f%s）"
                % (chosen[:28], rows[0]["score"], _pick_chosen_note(rows)))
    step_hint(job, "已选定 %s，正在分离整条音轨（这一步最久）" % chosen[:28])
    v, i, why = _sep_run(job, chosen, in_dir, vocal_dir, inst_dir)
    if not v:
        for m in _sep_candidates():
            if m == chosen:
                continue
            step_hint(job, "首选模型没跑通，正在换 %s 重试" % m[:28])
            v, i, why = _sep_run(job, m, in_dir, vocal_dir, inst_dir)
            if v:
                chosen = m
                break
    if not v:
        raise Fail("人声分离没产出文件。\n%s" % str(why)[:600])
    log(job, "  用 %s 分离完成" % chosen[:28])
    return vocal_dir


# ------------------------------------------------ 人声分离（唱歌链路的第 0 步）


def _sep_cache_key(song_path, model=None):
    """按「原曲路径 + 大小 + 修改时间 + 选优策略版本」算缓存键。

    为什么把 mtime 也算进去：同一个路径可能被换掉内容（重新下载/剪辑），
    只看路径会拿旧缓存去唱新歌 —— 那种错很难看出来（"我换歌了怎么没变"）。

    ★ 键里**不再带模型名**（v2 起）：模型是**选出来的**，不是指定的。带进去的话
    换了一天选到另一个模型就会缓存 miss 再跑一遍全曲；现在把选中的模型写进
    `meta.json`，命中缓存时直接复用并如实说"上次选的是谁、多少分"。
    """
    import hashlib

    st = os.stat(song_path)
    raw = "%s|%d|%d|%s|%s" % (os.path.abspath(song_path), st.st_size,
                              int(st.st_mtime), model or "auto", SEP_POLICY)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _sep_stats_load():
    try:
        return json.load(open(SEP_PICK_STATS, encoding="utf-8"))
    except Exception:
        return {}


def _sep_stats_add(model, score):
    """记住每个模型的历史平均分：**好用的先试**，省下的都是显卡时间。

    这不是"针对某一首歌调参"（那是过拟合），而是跨所有歌的统计 —— 模型强弱
    本来就是普遍规律，跟具体哪首歌没关系。
    """
    d = _sep_stats_load()
    row = d.get(model) or {"n": 0, "sum": 0.0, "best": 0.0}
    row["n"] = int(row.get("n") or 0) + 1
    row["sum"] = float(row.get("sum") or 0.0) + float(score or 0)
    row["avg"] = round(row["sum"] / max(row["n"], 1), 2)
    row["best"] = max(float(row.get("best") or 0), float(score or 0))
    d[model] = row
    try:
        tmp = SEP_PICK_STATS + ".tmp"
        json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        os.replace(tmp, SEP_PICK_STATS)
    except Exception:
        pass


def _msst_available():
    """MSST 那条路现在能不能用。

    它是**增益不是依赖**：那个 venv 可能被搬走、被删、被升级搞坏。没了它，
    剩下的四个 UVR 候选照跑，功能一点不少 —— 只是少了"池子里的第五个选择"。
    """
    return bool(VS_MSST_PY) and os.path.isfile(VS_MSST_PY)


def _msst_run(job, model, in_dir, vdir, idir, timeout=3600):
    """跑一次 MSST 分离，返回 (vocal, inst, 错误)。"""
    for d in (vdir, idir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    with gpu_slot(job, "分离人声（MSST）"):
        rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" %s'
                      % (VS_MSST_PY, os.path.join(STAGES, "msst_cli.py"),
                         in_dir, vdir, idir, model), timeout=timeout)
    vs = [f for f in os.listdir(vdir) if f.lower().endswith((".wav", ".flac"))]
    ins = [f for f in os.listdir(idir) if f.lower().endswith((".wav", ".flac"))]
    if rc == 0 and vs and ins:
        return os.path.join(vdir, vs[0]), os.path.join(idir, ins[0]), None
    return None, None, "退出码 %d，%s" % (rc, _tail(out, 3))


def _sep_run(job, model, in_dir, vdir, idir, timeout=3600):
    """跑一次分离——按模型走 UVR 还是走 MSST。返回 (vocal, inst, 错误)。

    上层一律调这个，别再直接调 `_uvr_run` / `_msst_run`：分离器的实现随时可能
    再加一条路（还在看的：BS PolarFormer，榜首 11.00），加了以后调用点一行都不用改。
    """
    if model in MSST_MODELS:
        if not _msst_available():
            return None, None, ("MSST 解释器不在：%s（这是可选的增强，"
                                "其余分离器不受影响）" % VS_MSST_PY)
        return _msst_run(job, model, in_dir, vdir, idir, timeout=timeout)
    return _uvr_run(job, model, in_dir, vdir, idir, timeout=timeout)


def _uvr_run(job, model, in_dir, vdir, idir, timeout=3600):
    """跑一次分离，返回 (vocal, inst, 错误)。"""
    for d in (vdir, idir):
        shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    # ★ 显卡闸门：分离也是 cuda 推理，一样不许和别人抢（见 gpu_slot 处的事故）。
    with gpu_slot(job, "分离人声（UVR）"):
        rc, out = run(job, '"%s" -s "%s" "%s" %s "%s" "%s" "%s" 10 wav cuda True'
                      % (PY, os.path.join(STAGES, "uvr_cli.py"), GSV_ROOT, model,
                         in_dir, vdir, idir), timeout=timeout)
    vs = [f for f in os.listdir(vdir) if f.lower().endswith((".wav", ".flac"))]
    ins = [f for f in os.listdir(idir) if f.lower().endswith((".wav", ".flac"))]
    if rc == 0 and vs and ins:
        return os.path.join(vdir, vs[0]), os.path.join(idir, ins[0]), None
    return None, None, "退出码 %d，%s" % (rc, _tail(out, 3))


def _sep_score(job, mix_wav, vocal_wav, inst_wav):
    """给一次分离打分（0~100，越高越好）。打分失败返回 None（不拦流程）。"""
    rc, out = run(job, '"%s" -s "%s" score "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "sep_eval.py"),
                     mix_wav, vocal_wav, inst_wav), timeout=900)
    m = re.search(r"SEP_SCORE (.+)", out or "")
    if rc != 0 or not m:
        return None
    d = {}
    for kv in m.group(1).split():
        if "=" in kv:
            k, v = kv.split("=", 1)
            try:
                d[k] = float(v)
            except Exception:
                d[k] = v
    return d or None


def _sep_champion(cands):
    """候选里**历史最稳**的那个（试过 ≥3 次、平均分最高）。没有就返回 None。

    注意它和"排第一"不是一回事：`_sep_candidates()` 给没攒够数据的新模型发
    乐观先验，先验值取的正是 max(已知 avg)，所以新模型可能和历史最优并列、
    再靠配置顺序挤到前面去。早停只试前两个的话，历史最优是有可能被跳过的
    —— 而它是拿两百多次实测数据证明过自己的那个，不该靠"恰好排第几"来决定
    要不要试。所以单独把它拎出来，让早停额外保它一次。
    """
    st = _sep_stats_load()
    best, bv = None, -1.0
    for m in cands:
        row = st.get(m) or {}
        if (row.get("n") or 0) < 3:
            continue
        a = row.get("avg") or 0
        if a > bv:
            bv, best = a, m
    return best


def _probe_should_stop(rows, tried_idx, total, champ=None):
    """探测要不要早停（抽成纯函数 —— 门禁能直接测，不用真跑一遍模型）。

    三条**同时**满足才停：
      ① 已经试够 SEP_PROBE_MIN 个。只试 1 个太脆：那一个偶然失手（这首歌上
         恰好漏一片鼓）就没有对照了，会直接选错。
      ② "再试也没意义"了 —— 满足**任一条**即可：
           · 第一名领先第二名 ≥ SEP_PROBE_GAP（分出高下了）
           · 或者最高的分已经 ≥ SEP_PROBE_GOOD（歌太简单，没必要再找）
         ★ 两条都是拿 193 次真实探测**离线回放**定出来的，不是拍脑袋。
      ③ 历史最优（champ）已经试过了 —— 没试过就不能停。省时间不能省在
         "把最有把握的那个跳过"上。champ 为 None（还没攒出稳定数据）时不拦。
    另外只在**还有剩下的**时才叫"停"（最后一个试完本来就该结束）。
    """
    if len(rows) < SEP_PROBE_MIN:
        return False
    sc = sorted((r.get("score") or 0) for r in rows)
    lead = (sc[-1] - sc[-2]) if len(sc) >= 2 else 0.0
    if lead < SEP_PROBE_GAP and sc[-1] < SEP_PROBE_GOOD:
        return False
    if champ and champ not in set(r.get("model") for r in rows):
        return False
    return tried_idx + 1 < total


def _probe_clip(total):
    """探测片段怎么取（纯函数，门禁能直接测）。返回 (parts, dur)。

    parts = [(start, sec), ...] —— 取几段、各从哪开始、各多长；拼起来当探针。
    dur = None 表示"用整首"（歌本身就短，不切）。

    ★ 2026-10-27：从"取正中间一段"改成"取 SEP_PROBE_AT 那几处，各 SEP_PROBE_SEC/n 秒"。
      为什么：原来 `(total-30)/2` 永远落在歌曲正中间，而用户抱怨的"越到后面越差"
      发生在后 1/4 ——**后半段从来没被评估过**。实测同一首歌，把 150~180 秒单独
      拿出来比，被选中的模型恰恰是五个候选里漏伴奏最狠的（0.2663 vs 0.1919），
      而漏进来的伴奏高音乐器就是"突然跳八度"的来源。完整数据见 SEP_PROBE_AT 注释。

    ★ 时长读不到时**绝不能退化成整首**：实测一个模型跑一首 4 分钟的歌要 128 秒，
      而 30 秒片段只要 16 秒 —— 拿整首当探测片，等于每个候选都跑一遍全曲，
      4 个候选就是 8 分钟，"先试一小段"这件事本身就废了。
      这个坑是真跑出来的：传 mp3 进来时读不到时长，探测默默跑了 204 秒才出结果。
    """
    n = max(1, len(SEP_PROBE_AT))
    p = SEP_PROBE_SEC / float(n)          # 每段多长（几段加起来仍是 30 秒）
    if total and total > SEP_PROBE_SEC * 1.5:
        parts = []
        for frac in SEP_PROBE_AT:
            s = total * frac - p / 2.0
            s = min(max(0.0, s), max(0.0, total - p))
            parts.append((round(s, 2), p))
        return parts, SEP_PROBE_SEC
    if total:
        return [], None                   # 歌本身就短，整首试也不慢
    return [(0.0, p)], p                  # 时长未知：只取开头


# ★ 这里曾经写过一个"一次进程跑完所有候选"的批量分离，写完算完账又删了：
#   单次子进程只省 3.4 秒（Python 0.5s + import torch 2.9s），但批量就**没法早停**
#   —— 必须把 4 个全跑完。算总账：逐个+早停平均 28 秒，批量全跑 38 秒。
#   省启动不如少跑模型，所以留逐个模式。（批量脚本 uvr_cli.py batch 已写好但没人调，
#   按"定义了没人用就删"的规矩，等真用得上再加回来。）

def _probe_filter(parts):
    """把几段拼成一条 ffmpeg 滤镜串（纯函数，门禁能直接测）。

    为什么不用 `-ss/-t` 切一段了事：2026-10-27 起探针是**多段**（覆盖中段 + 后段），
    必须拼成一条音频才能喂给分离器（分离器的输入目录里只认一个 source.wav）。
    """
    segs = []
    for k, (s, d) in enumerate(parts):
        segs.append("[0:a]atrim=start=%.3f:end=%.3f,asetpts=PTS-STARTPTS[s%d]"
                    % (s, s + d, k))
    segs.append("".join("[s%d]" % k for k in range(len(parts)))
                + "concat=n=%d:v=0:a=1[o]" % len(parts))
    return ";".join(segs)


def _karaoke_model():
    """挑一个**已经下好**的 karaoke 分离模型（伴唱优先用）。

    为什么不在自动候选池里：它的目标函数和"谁分得干净"不是一回事 ——
    它要的是「伴唱留在伴奏侧」。所以只由用户显式选择（sep_model="karaoke"）触发。
    """
    cand = os.path.join(HERE, "data", "models", "msst")
    if os.path.isfile(os.path.join(cand, "karaoke_bs_roformer_anvuew.ckpt")):
        return "karaoke_bs_roformer_anvuew"
    if os.path.isfile(os.path.join(cand, "mel_band_roformer_karaoke_becruily.ckpt")):
        return "mel_band_roformer_karaoke_bec"
    return "karaoke_bs_roformer_anvuew"      # 没下好也让它去报错，比悄悄退回标准分离清楚


def _sep_probe(job, src_wav, work, cands):
    """拿一段探针音频，让各候选模型各分一遍，返回 [(模型, 分数, 漏电), ...]。

    为什么只试 30 秒：全曲跑三个模型要多花 4~6 分钟，而"哪个模型更适合这首歌"
    这件事，30 秒的有代表性的片段基本就定下来了（实测三个模型的分数差 15 分，
    不是噪声级别的差异）。省下来的时间留给变声，那才是大头。

    ★ 2026-10-27：这 30 秒不再是一整段，而是**歌的 40% 和 80% 两处各 15 秒拼起来**。
      原因写在 SEP_PROBE_AT 的注释里：只取中间一段时，后半段的漏伴奏从来没被
      评估过，而漏进来的伴奏高音乐器正是"越到后面越差、突然跳八度"的来源。
    """
    try:
        total = wav_seconds(src_wav)
    except Exception:
        total = 0.0
    probe = os.path.join(work, "probe.wav")
    parts, dur = _probe_clip(total)
    if parts:
        rc, out = run(job, '"%s" -y -v error -i "%s" -filter_complex "%s" -map "[o]"'
                      ' -ac 2 -ar 44100 "%s"'
                      % (FFMPEG, src_wav, _probe_filter(parts), probe), timeout=600)
        if rc != 0 or not os.path.isfile(probe):
            log(job, "  试听片段拼不出来，改用整首去试（会慢不少）")
            probe = src_wav
        else:
            # 说清楚探针取自哪几秒 —— 后半段漏伴奏是"跳八度"的根因，
            # 用户（和下一次排查的人）有权知道模型是在哪一段上被挑出来的。
            log(job, "  探针取自 %s 秒处（覆盖中段 + 后段）"
                % " / ".join("%.0f~%.0f" % (s, s + d) for s, d in parts))
    else:
        probe = src_wav
    pin = os.path.join(work, "probe_in")
    shutil.rmtree(pin, ignore_errors=True)
    os.makedirs(pin, exist_ok=True)
    shutil.copyfile(probe, os.path.join(pin, "source.wav"))

    rows = []
    _cands = cands[:SEP_MAX_CAND]
    _champ = _sep_champion(_cands)
    if _champ:
        log(job, "  历史最优：%s（早停不会跳过它）" % _champ[:28])
    for _k, m in enumerate(_cands):
        # ★ 让用户看见"我在做选择"，不是干等：这一步本来就是拿 30 秒片段
        #   让几个模型各分一遍、按得分挑最优，界面上就该这么写。
        step_hint(job, "正在比较 %d 个分离模型，按 SDR 挑最优（第 %d/%d 个）"
                  % (len(_cands), _k + 1, len(_cands)))
        t0 = time.time()
        v, i, why = _sep_run(job, m, pin,
                             os.path.join(work, "pv_" + m[:20]),
                             os.path.join(work, "pi_" + m[:20]), timeout=1200)
        if not v:
            log(job, "  候选 %s 没跑通（%.0fs）：%s" % (m[:28], time.time() - t0, why))
            continue
        sc = _sep_score(job, os.path.join(pin, "source.wav"), v, i)
        if not sc:
            log(job, "  候选 %s 分不出来（跳过）" % m[:28])
            continue
        rows.append({"model": m, "score": sc.get("score", 0.0), "leak": sc.get("leak"),
                     "leak_worst": sc.get("leak_worst"),
                     "secs": round(time.time() - t0, 1)})
        log(job, "  候选 %-28s 得分 %5.1f（漏伴奏 %.3f，%.0fs）"
            % (m[:28], sc.get("score", 0.0), sc.get("leak") or 0, time.time() - t0))
        _sep_stats_add(m, sc.get("score", 0.0))

        # ---- 早停：已经够好就别再试了（省下来的时间是真金白银）----
        if _probe_should_stop(rows, _k, len(_cands), _champ):
            left = _cands[_k + 1:]
            best_now = max(r["score"] for r in rows)
            avg_sec = sum(r["secs"] for r in rows) / float(len(rows))
            sc = sorted(r["score"] for r in rows)
            why = ("第一名领先 %.1f 分（≥ %.0f）" % (sc[-1] - sc[-2], SEP_PROBE_GAP)
                   if sc[-1] - sc[-2] >= SEP_PROBE_GAP
                   else "已经 %.1f 分（≥ %.0f，够好了）" % (sc[-1], SEP_PROBE_GOOD))
            log(job, "  %s，剩下 %d 个不试了（省约 %.0f 秒）"
                % (why, len(left), avg_sec * len(left)))
            break
    return _probe_rank(rows)


def _leak_of(r):
    """漏电到底取哪个值（纯函数，门禁能直接测）。

    优先 `leak_worst`（最漏的那一段的 p90，见 sep_eval._corr_worst），拿不到再退回整段中位数。

    ★ 为什么非抽成这一个函数不可：**判据**和**日志**必须用同一个口径。
      第一版分开写，日志出来是「总分更高但漏电 0.167 > 0.20」—— 0.167 明明没超
      0.20，自己跟自己打架（那时判据看的是 leak_worst 0.2078、日志写的是中位数
      0.1674）。用户看到这种话只会更糊涂，还不如不说。
    """
    return max(r.get("leak_worst") or 0.0, r.get("leak") or 0.0)


def _probe_rank(rows):
    """探针结果排序（纯函数，门禁能直接测）。

    ★ 排序带"漏电否决权"：漏电超过 SEP_PROBE_LEAK_MAX 的排到后面，哪怕它总分更高。
      漏电 = 伴奏残留在人声轨里；RVC 会把残留的**高音乐器**当人声唱出来
      ⇒ 听感就是"突然跳八度"。实测数据见 SEP_PROBE_LEAK_MAX 那条注释。

    ★ 为什么不写在 _sep_probe 里就算了：这条规则挡的是用户能直接听见的毛病，
      必须有一个门禁能直接调用、且**改坏就立刻变红**的落点。内联的 lambda
      测不到，下次有人动评分权重就会悄悄失效。

    ★ 用哪个漏电值：优先 `leak_worst`（最漏的那一段，见 sep_eval._corr_worst），
      它拿不到再退回整段中位数 `leak`。
      为什么不能只用中位数（2026-10-27 实测踩到）：探针改成"中段 + 后段"两段之后，
      五个模型的中位数全落在 0.16~0.19，看着都合格、否决权一次都没触发；
      可把后段单独拎出来量，漏得最狠的那个是 0.2663。**中位数被干净段稀释了。**

    ★ 全部超标时第一关键字都是 1，排序自动退化回"按总分" ⇒ 不会没得选。
    """
    return sorted(rows, key=lambda r: (
        1 if _leak_of(r) > SEP_PROBE_LEAK_MAX else 0,
        -(r.get("score") or 0)))


def _pick_chosen_note(rows):
    """选中理由的人话补充（纯函数，门禁能直接测）。返回 "" 或一段"；……"。

    ★ 为什么单独抽出来：批量探测和正式分离两处要写同一句话，分开写早晚写歪。
      更重要的是——**"漏电高的模型总分再高也不选"这件事必须有判据盯着**，
      不然下次有人调评分权重就悄悄改没了，而它挡的正是"突然跳八度"。
    """
    if not rows or len(rows) < 2:
        return ""
    top, second = rows[0], rows[1]
    if (second.get("score") or 0) <= (top.get("score") or 0):
        return ""
    return ("；%s 总分更高（%.1f）但它最漏的那一段漏电 %.4f > %.2f，"
            "伴奏会漏进人声轨、被当成高音唱出来 ⇒ 不用它"
            % (second["model"][:20], second.get("score") or 0,
               _leak_of(second), SEP_PROBE_LEAK_MAX))


def _sep_candidates():
    """候选顺序：历史平均分高的先试（省时间），没历史就按经验顺序。

    ★ 新加的模型历史是空的（avg=0）—— 直接按 0 排会**永远排在最后、永远轮不到**
      （`SEP_MAX_CAND` 一截就没了），于是它永远攒不到数据，永远进不了视野。
      所以给"试过不到 3 次"的模型一个**乐观先验**（当前已知最高平均分），
      让它先被试几次，之后自然按真实数据排回去。
    """
    st = _sep_stats_load()
    known = [(r.get("avg") or 0) for r in st.values() if (r.get("n") or 0) >= 3]
    prior = max(known) if known else 1.0

    def key(m):
        row = st.get(m) or {}
        n = row.get("n") or 0
        eff = (row.get("avg") or 0) if n >= 3 else prior
        return (-eff, UVR_CANDIDATES.index(m) if m in UVR_CANDIDATES else 99)
    return sorted(UVR_CANDIDATES, key=key)


def ensure_playable_audio(job, src, out_dir=None):
    """确认这首歌是**能直接读的普通音频**，返回路径。

    只接受普通音频（wav / mp3 / flac / m4a / aac / ogg / wma）。加密容器
    （.ncm 等）不支持 —— 请先用别的工具导出成 mp3/wav 再放进来。

    out_dir 参数保留是为了兼容既有调用点（上传入口会传 uploads/），
    但这一步已经不再产出任何副本：传进来的路径原样返回。
    """
    src = str(src or "")
    if not os.path.isfile(src):
        return src
    ext = os.path.splitext(src)[1].lower()
    if ext in (".ncm", ".qmc", ".mflac", ".mgg", ".kgg", ".kgm"):
        raise Fail("这是一个加密容器（%s），不能直接当音频用。\n"
                   "请先用导出工具转成 mp3/wav，再重新导入。" % ext)
    return src


def separate_song(job, song_path, model=None, use_cache=True, keep=None):
    """把一首歌拆成「人声轨」和「伴奏轨」，返回 (vocal_wav, inst_wav)。

    唱歌**必须**先做这一步，否则：
      * 整首歌进 RVC ⇒ 鼓和贝斯也会被当成"唱"，输出跟着节奏抖成一团；
      * 混音时只能拿原曲当伴奏 ⇒ 原唱和角色一起唱（实测成品波形与原曲
        相关系数 0.870、原曲占比 0.538，就是"没法听"的主因）。

    缓存按「原曲 + 分离模型」做键：分离一首 4 分钟的歌要一两分钟，而用户改一个
    参数（音高/相似度）就要重跑一次，每次都重新分离纯属浪费。

    keep：分离完要不要再**按中文文件夹另存一份**（`分离结果/<歌名>/01_人声（原唱）/`
    这种），方便用户自己拿去改。None = 跟着「存储设置」里的开关走。
    """
    # 先确认拿到的是能直接读的普通音频，再算缓存键、往下走。
    # 放最前面是有意的：唱歌 / 识别歌词 / 试听小样三条链路都经过这里，
    # 兜在这一点，任何入口送进来的加密歌都不会再走到 ffmpeg 那一步去撞墙。
    song_path = ensure_playable_audio(job, song_path)

    key = _sep_cache_key(song_path, model)
    cache_dir = os.path.join(SEP_CACHE, key)
    vocal = os.path.join(cache_dir, "vocal.wav")
    inst = os.path.join(cache_dir, "inst.wav")
    meta_path = os.path.join(cache_dir, "meta.json")

    if use_cache and os.path.isfile(vocal) and os.path.isfile(inst):
        who = ""
        try:
            mt = json.load(open(meta_path, encoding="utf-8"))
            who = "（上次选的是 %s，得分 %.1f）" % (
                str(mt.get("chosen") or "")[:28], float(mt.get("score") or 0))
        except Exception:
            pass
        log(job, "  复用上次分离好的「人声 + 伴奏」%s" % who)
        return vocal, inst

    work = os.path.join(cache_dir, "_work")
    in_dir = os.path.join(work, "in")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(in_dir, exist_ok=True)
    # 统一转成 2 声道 44.1kHz —— UVR5 原版就要求这个格式，视频抽出来的
    # 音频/各种码率的 mp3 直接喂进去有的模型会炸。
    src = os.path.join(in_dir, "source.wav")
    rc, out = run(job, '"%s" -y -v error -i "%s" -ac 2 -ar 44100 "%s"'
                  % (FFMPEG, song_path, src), timeout=1800)
    if rc != 0 or not os.path.exists(src):
        raise Fail("原曲转码失败，可能是文件损坏或格式不认识。\n%s" % _tail(out, 3))

    vdir = os.path.join(work, "vocal")
    idir = os.path.join(work, "inst")

    # ---- 选模型：先试 30 秒片段打分，再让分最高的跑全曲 ----
    chosen, rows = (model or UVR_MODEL_BEST), []
    if not model:
        log(job, "  先拿 %.0f 秒片段试几个分离器，谁分高用谁" % SEP_PROBE_SEC)
        rows = _sep_probe(job, src, work, _sep_candidates())
        if rows:
            chosen = rows[0]["model"]
            log(job, "  ⇒ 用 %s（得分 %.1f%s）跑完这一首"
                % (chosen[:28], rows[0]["score"], _pick_chosen_note(rows)))
        else:
            chosen = UVR_MODEL_BEST
            log(job, "  一个候选都没跑通，按经验用 %s" % UVR_MODEL_BEST)

    v, i, why = _sep_run(job, chosen, in_dir, vdir, idir)
    if not v and chosen != UVR_MODEL_SAFE:
        # Roformer 需要配套的 .yaml，整合包不一定带 ⇒ 读不到权重会直接失败。
        # 不能让它把整条链带死 —— 回退到训练时一直在用的 VR 模型。
        log(job, "  %s 全曲没跑通：%s" % (chosen[:28], why))
        chosen = UVR_MODEL_SAFE
        log(job, "  改用常规分离器 %s（质量略低但稳定）" % UVR_MODEL_SAFE)
        v, i, why = _sep_run(job, chosen, in_dir, vdir, idir)
    if not v:
        # 最后一个兜底：每个候选都试一遍全曲，绝不因为"选错了"就整条链挂掉
        for m in _sep_candidates():
            if m == chosen:
                continue
            log(job, "  再试 %s …" % m[:28])
            v, i, why = _sep_run(job, m, in_dir, vdir, idir)
            if v:
                chosen = m
                break
    if not v:
        raise Fail("人声分离失败，所有分离器都没跑通。\n最后一个错误：%s" % why)

    # ---- 二次分离：漏伴奏还多的话，把人声轨再分离一遍（去残留）----
    # 这是 UVR 社区的标准操作：第一遍剩下的伴奏，第二遍往往能再刮掉一层。
    # ★ 2026-10-22 修了两个坑（历史 27 次触发、**一次都没被采纳**）：
    #   ① 比较口径错了 —— 以前拿"第一遍的人声轨"当混音参考去给第二遍打分，
    #      那个分数天生比第一遍低 30 分（实测 76.8 → 51.6），第二遍永远赢不了。
    #      现在两遍都对着**原曲**打分，才是同一把尺子。
    #   ② 采纳时会连伴奏一起换掉 —— `i` 被换成"人声里刮下来的残留"，
    #      成品会既没人声也没伴奏（因为①永远不采纳，这个雷一直没炸）。
    #      现在只换人声，伴奏保持第一遍的。
    # ★ 另外加了闸门：第一遍分数够好（>= SEP_RETRY_SCORE）就不做，省一整遍全曲时间。
    sc1 = _sep_score(job, src, v, i)
    if ((not model) and sc1
            and (sc1.get("leak") or 0) > SEP_RETRY_LEAK
            and (sc1.get("score") or 0) < SEP_RETRY_SCORE):
        log(job, "  第一遍漏伴奏还不少（leak=%.3f，分 %.1f），把人声轨再分离一遍"
            % (sc1["leak"], sc1.get("score") or 0))
        v2dir = os.path.join(work, "vocal2")
        i2dir = os.path.join(work, "inst2")
        v_in = os.path.join(work, "vin")
        shutil.rmtree(v_in, ignore_errors=True)
        os.makedirs(v_in, exist_ok=True)
        shutil.copyfile(v, os.path.join(v_in, "source.wav"))
        v2, i2, why2 = _sep_run(job, chosen, v_in, v2dir, i2dir, timeout=1800)
        if v2:
            # 跟第一遍用**同一个混音参考**（原曲），分数才可比
            sc2 = _sep_score(job, src, v2, i)
            if sc2 and (sc2.get("score") or 0) > (sc1.get("score") or 0):
                log(job, "  第二遍更好（%.1f → %.1f），换它的人声轨（伴奏不动）"
                    % (sc1["score"], sc2["score"]))
                v, sc1 = v2, sc2
            else:
                log(job, "  第二遍没更好（%.1f → %s），保留第一遍"
                    % (sc1["score"], sc2 and sc2.get("score")))
        else:
            log(job, "  第二遍没跑通（%s），保留第一遍" % why2)

    # 原子落盘：先写 .part 再 replace，避免半个文件被下次当成有效缓存复用。
    os.makedirs(cache_dir, exist_ok=True)
    for s, d in ((v, vocal), (i, inst)):
        tmp = d + ".part"
        shutil.copyfile(s, tmp)
        os.replace(tmp, d)
    try:
        json.dump({"chosen": chosen, "score": (sc1 or {}).get("score"),
                   "leak": (sc1 or {}).get("leak"), "rows": rows,
                   "ver": SEP_POLICY, "at": _now()},
                  open(meta_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    shutil.rmtree(work, ignore_errors=True)
    log(job, "  人声轨 %.1f 秒 / 伴奏轨 %.1f 秒（%s，得分 %s）"
        % (wav_seconds(vocal), wav_seconds(inst), chosen[:28],
           ("%.1f" % sc1["score"]) if sc1 else "未评分"))

    # ---- 要不要另外留一份「中文文件夹」的分轨，交给用户自己改 ----
    # 老板（2026-10-25）：「分离原唱文件时，应该给我自己决定要不要保留，
    #   都挨个分好中文文件夹，方便我修改」。
    # 以前只有 `data/cache/sep/<hash>/vocal.wav` 这一份 —— hash 目录名，
    # 打开就是一堆看不懂的十六进制，想改也找不到。
    if keep is None:
        keep = bool(storage_settings().get("keep_stems"))
    if keep:
        try:
            export_stems(job, song_path, vocal, inst)
        except BaseException as e:      # 含批量删除钩子的 SystemExit
            log(job, "  ⚠ 分轨另存一份没成功（不影响唱歌）：%s" % str(e)[:120])
    return vocal, inst


# ---------------------------------------------------------------------------
# 分轨另存：中文文件夹
# ---------------------------------------------------------------------------

# ★ 为什么放在项目根、不放在 data/ 下：这是**给用户看的**东西，他要能直接
#   双击打开、拖进别的软件改。藏在 data/cache 里等于没给。
STEM_ROOT = os.path.join(HERE, "分离结果")

# (键, 中文文件夹名, 里面的文件名)
STEM_LAYOUT = (
    ("vocal", "01_人声（原唱）", "人声.wav"),
    ("inst", "02_伴奏", "伴奏.wav"),
)

STEM_README = """这一层是「{song}」分离出来的分轨，每一轨单独一个文件夹。

  01_人声（原唱）/人声.wav   只有原唱的人声，没有伴奏
  02_伴奏/伴奏.wav           只有伴奏，没有人声

怎么用：
  · 想自己修某几个字（去哑音 / 补音 / 改调 / 拆和声）⇒ 打开「精修台」，
    选 01_人声（原唱）/人声.wav，改完导出，再拿去翻唱。
  · 想让 AI 翻唱得更干净 ⇒ 先把人声轨里的气声/齿音/哑音修掉再喂给模型，
    比整首重跑有效得多（模型学的是这一轨）。
  · 伴奏轨可以直接当混音底，也可以单独降噪声用。

改坏了不要紧：原始分离缓存在 data/cache/sep 下，删掉这里的文件夹
不会影响翻唱功能，重跑一次分离会再生成一份。
生成时间：{at}
"""


def stems_dir_of(song_path):
    """这首歌的分轨文件夹在哪（不管有没有真的生成过）。"""
    return os.path.join(STEM_ROOT, _safe_name(
        os.path.splitext(os.path.basename(song_path or ""))[0], "未命名"))


def export_stems(job, song_path, vocal=None, inst=None):
    """把分离结果按**中文文件夹**逐个另存一份，返回 {dir, items}。

    为什么是"另存"而不是"改存"：`data/cache/sep` 那份是**缓存**，按
    「原曲+模型」hash 命名，流水线全程靠它复用（换别的模型跑会换键）。
    给用户改的这份必须在**稳定、看得懂**的路径上，两边谁也不影响谁。
    """
    if (not vocal) or (not inst):
        key = _sep_cache_key(ensure_playable_audio(job, song_path))
        d = os.path.join(SEP_CACHE, key)
        vocal = vocal or os.path.join(d, "vocal.wav")
        inst = inst or os.path.join(d, "inst.wav")
    if not os.path.isfile(vocal) or not os.path.isfile(inst):
        raise Fail("这首歌还没分离过，先跑一次分离再来另存分轨。")

    root = stems_dir_of(song_path)
    # ★ 只清自己这层的 wav，**不删整个 root**：用户可能自己在里面放了别的
    #   （比如他从精修台导出的和声层文件夹）。
    items = []
    for key, folder, fname in STEM_LAYOUT:
        src = vocal if key == "vocal" else inst
        fd = os.path.join(root, folder)
        os.makedirs(fd, exist_ok=True)
        dst = os.path.join(fd, fname)
        tmp = dst + ".part"
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)             # 原子写：半个 wav 会被别的软件读崩
        items.append({"key": key, "folder": folder, "file": fname,
                      "path": dst, "bytes": os.path.getsize(dst),
                      "secs": wav_seconds(dst)})
    try:
        with open(os.path.join(root, "读我.txt"), "w", encoding="utf-8") as f:
            f.write(STEM_README.format(
                song=os.path.basename(root), at=_now()))
    except Exception:
        pass
    log(job, "  分轨已另存到 %s（%s）"
        % (root, "、".join(i["folder"] for i in items)))
    return {"dir": root, "items": items}


def stems_status(song_path):
    """这首歌有没有已经另存过分轨（给界面显示「打开文件夹 / 删掉」）。"""
    root = stems_dir_of(song_path)
    items = []
    for key, folder, fname in STEM_LAYOUT:
        p = os.path.join(root, folder, fname)
        if os.path.isfile(p):
            items.append({"key": key, "folder": folder, "file": fname,
                          "path": p, "bytes": os.path.getsize(p),
                          "secs": wav_seconds(p)})
    extra = []
    if os.path.isdir(root):
        for n in sorted(os.listdir(root)):
            d = os.path.join(root, n)
            if not os.path.isdir(d) or n in set(f for _, f, _ in STEM_LAYOUT):
                continue
            for f in sorted(os.listdir(d)):
                if f.lower().endswith(".wav"):
                    extra.append({"key": "extra", "folder": n, "file": f,
                                  "path": os.path.join(d, f),
                                  "bytes": os.path.getsize(os.path.join(d, f)),
                                  "secs": wav_seconds(os.path.join(d, f))})
    return {"dir": root, "exists": bool(items), "items": items, "extra": extra}


def stems_clear(song_path):
    """删掉这首歌另存的那份分轨（不动缓存，翻唱照常能跑）。"""
    root = stems_dir_of(song_path)
    n = 0
    if os.path.isdir(root):
        # ★ 只删 STEM_LAYOUT 那几个文件夹 + 读我.txt；用户在里面自己放的
        #   和声层等文件夹**一律不动**（那是他手动导出的成果）。
        for _, folder, _f in STEM_LAYOUT:
            d = os.path.join(root, folder)
            if os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        try:
            os.remove(os.path.join(root, "读我.txt"))
        except OSError:
            pass
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except OSError:
            pass
    return {"ok": True, "removed": n, "dir": root}


# ---------------------------------------------------------------------------
# 精修台：音块级微调（去哑音 / 补音 / 改调 / 拆和声）
# ---------------------------------------------------------------------------

FINE_DIR = os.path.join(HERE, "精修成品")

AUDIO_EXT = (".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg")


def _fine_mod():
    """按需导入 stages.finetune（它只吃 numpy + soundfile，导入很轻）。"""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from stages import finetune
    return finetune


def fine_sources(limit=200):
    """精修台能选的音频：角色成品 / 已另存的分轨 / 曲库 / 上传 / 以往精修产物。

    按「改它的可能性」分组：成品（最常要修）→ 分轨 → 曲库 → 上传 → 旧产物。

    ★ 每类**各自**先截 PER_KIND 条再合并：直接按总 limit 截的话，成品一多
      （实测 200 条封顶全是成品）后面几类**一条都露不出来**，而"分轨"恰恰是
      用户最想修的东西之一。分类截完再拼，才能保证每一类都在列表里。
    """
    PER_KIND = 40
    seen = set()
    buckets = {}          # kind -> [item]

    def add(path, kind, label, extra=None):
        p = os.path.abspath(path)
        k = p.lower()
        if k in seen or not os.path.isfile(p):
            return
        if not p.lower().endswith(AUDIO_EXT):
            return
        seen.add(k)
        try:
            mb = round(os.path.getsize(p) / 1048576.0, 1)
        except OSError:
            mb = 0.0
        it = {"path": p, "kind": kind, "label": label, "mb": mb}
        if extra:
            it.update(extra)
        b = buckets.setdefault(kind, [])
        if len(b) < PER_KIND:
            b.append(it)

    # ① 角色成品：最常被要求"修一下"的就是它
    if os.path.isdir(ROLES_DIR):
        for rid in sorted(os.listdir(ROLES_DIR)):
            fd = os.path.join(ROLES_DIR, rid, "final")
            if not os.path.isdir(fd):
                continue
            nm = rid
            try:
                rj = json.load(open(os.path.join(ROLES_DIR, rid, "role.json"),
                                    encoding="utf-8"))
                nm = rj.get("name") or rid
            except Exception:
                pass
            for fn in sorted(os.listdir(fd), reverse=True):
                if fn.lower().endswith(AUDIO_EXT):
                    add(os.path.join(fd, fn), "成品",
                        "%s · %s" % (nm, os.path.splitext(fn)[0]))

    # ② 已另存的分轨（中文文件夹）
    if os.path.isdir(STEM_ROOT):
        for song in sorted(os.listdir(STEM_ROOT)):
            sd = os.path.join(STEM_ROOT, song)
            if not os.path.isdir(sd):
                continue
            for folder in sorted(os.listdir(sd)):
                fd = os.path.join(sd, folder)
                if not os.path.isdir(fd):
                    continue
                for fn in sorted(os.listdir(fd)):
                    if fn.lower().endswith(AUDIO_EXT):
                        add(os.path.join(fd, fn), "分轨",
                            "%s · %s" % (song, folder))

    # ③ 曲库
    try:
        d = json.load(open(os.path.join(DATA, "music_library.json"),
                           encoding="utf-8"))
        for s in (d.get("songs") or []):
            if s.get("path"):
                add(s["path"], "曲库", s.get("title") or os.path.basename(s["path"]))
    except Exception:
        pass

    # ④ 上传
    if os.path.isdir(UPLOADS):
        for fn in sorted(os.listdir(UPLOADS), reverse=True):
            add(os.path.join(UPLOADS, fn), "上传", fn)

    # ⑤ 以前精修出来的
    if os.path.isdir(FINE_DIR):
        for root, _ds, fs in os.walk(FINE_DIR):
            for fn in sorted(fs, reverse=True):
                if fn.lower().endswith(AUDIO_EXT):
                    add(os.path.join(root, fn), "精修产物", fn)

    out = []
    for kind in ("成品", "分轨", "曲库", "上传", "精修产物"):
        out += buckets.get(kind) or []
    return out[:limit]


def fine_analyze(path):
    """切块 + 量特征 + 诊断。给界面画钢琴卷帘。"""
    if not path or not os.path.isfile(path):
        raise Fail("文件不存在：%s" % path)
    return _fine_mod().analyze(path)


# 试听片段放这儿：**不进成品目录**（它只是给你听的，不是产物），
# 又必须在 FINE_ROOTS 里（/media 有路径守卫，不在白名单里前端放不出来）。
AUDITION_DIR = os.path.join(DATA, "cache", "polish_audition")


def polish_audition(path, side="post", amount=1.0, only="", sec=8.0, at=-1.0,
                    semi=0.0):
    """切一小段，出"修前 / 修后"两条 wav，给界面做 A/B 干湿对比。

    ★ 这两条**不是成品**：不进 精修成品/，不进可选清单，纯粹是给你听的。
    ★ 没达标时 wet 会是 dry 的拷贝（两条听起来一样），并在 reason 里照实说
      —— 不许让用户拖了半天滑条才发现"根本没修"。
    """
    if side not in POLISH_SIDES:
        raise Fail("不认识的微调方向：%s" % side)
    if not path or not os.path.isfile(path):
        raise Fail("文件不存在：%s" % path)
    try:
        amount = max(0.0, min(1.0, float(amount or 1.0)))
    except (TypeError, ValueError):
        amount = 1.0
    try:
        sec = max(2.0, min(20.0, float(sec or 8.0)))
    except (TypeError, ValueError):
        sec = 8.0
    try:
        at = float(at if at is not None else -1.0)
    except (TypeError, ValueError):
        at = -1.0
    try:
        semi = float(semi or 0.0)
    except (TypeError, ValueError):
        semi = 0.0
    only = str(only or "").strip()
    # ★ 分步 solo 只放行本方向**真实存在**的步骤 —— 传个瞎名字进来会被当成
    #   "除了它全跳过"，等于一步都不跑还假装成功了（跟 skip 参数同一个坑）。
    if only and only not in (POLISH_STEPS.get(side) or ()):
        raise Fail("没有这一步：%s（%s 只有 %s）"
                   % (only, "唱前" if side == "pre" else "唱后",
                      "、".join(POLISH_STEPS.get(side) or ())))

    os.makedirs(AUDITION_DIR, exist_ok=True)
    tag = "%s_%s" % (side, only or "all")
    dry = os.path.join(AUDITION_DIR, "试听_%s_修前.wav" % tag)
    wet = os.path.join(AUDITION_DIR, "试听_%s_修后.wav" % tag)
    rep = os.path.join(AUDITION_DIR, "_aud_%s.json" % tag)
    for p in (dry, wet):
        with contextlib.suppress(OSError):
            os.remove(p)
    args = ['audition', '"%s"' % path, '"%s"' % dry, '"%s"' % wet, '"%s"' % rep,
            '--side %s' % side, '--amount %s' % amount, '--sec %s' % sec,
            '--at %s' % at, '--semi %s' % semi]
    if only:
        args.append('--only %s' % only)
    rc, txt = _polish_cli(args, timeout=900)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    if not info or not info.get("ok"):
        raise Fail("试听没做出来：%s" % _tail(txt, 2))
    if not os.path.isfile(dry) or not os.path.isfile(wet):
        raise Fail("试听片段没写出来")
    info["dry"] = dry
    info["wet"] = wet
    info["label"] = POLISH_STEP_LABEL.get(only, "") if only else ""
    return info


def _fine_out_path(src_path, suffix="精修"):
    """产物路径。同名加序号，**不覆盖**上一版（用户要留着 A/B 对比）。"""
    base = _safe_name(os.path.splitext(os.path.basename(src_path))[0], "audio")
    d = os.path.join(FINE_DIR, base)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "%s_%s.wav" % (base, suffix))
    i = 2
    while os.path.exists(p):
        p = os.path.join(d, "%s_%s_%d.wav" % (base, suffix, i))
        i += 1
    return p


def fine_render(path, blocks, edits, name=None, gain_db=0.0,
                dereverb=0.0, limit=None):
    """按 edits 渲染一版成品。返回 {"path","url","name"}。"""
    ft = _fine_mod()
    if not path or not os.path.isfile(path):
        raise Fail("文件不存在：%s" % path)
    if dereverb and dereverb > 0:
        tmp = os.path.join(tempfile.gettempdir(),
                           "vs_fine_dv_%s.wav" % uuid.uuid4().hex[:10])
        try:
            ft.dereverb_whole(path, tmp, float(dereverb))
            path = tmp
            blocks = ft.analyze(path).get("blocks") or blocks
        except Exception:
            pass          # 整轨去混响失败不该挡住逐块编辑
    out = _fine_out_path(name or path)
    ft.render(path, blocks, edits, out, gain_db=float(gain_db or 0.0))
    if limit:
        try:
            _prune_dir(os.path.dirname(out), int(limit))
        except Exception:
            pass
    return {"path": out, "name": os.path.basename(out),
            "mb": round(os.path.getsize(out) / 1048576.0, 2)}


def _prune_dir(d, keep):
    """一个目录里只留最新的 keep 个 wav（精修产物会越攒越多）。"""
    fs = [os.path.join(d, f) for f in os.listdir(d)
          if f.lower().endswith(".wav") and os.path.isfile(os.path.join(d, f))]
    if len(fs) <= keep:
        return 0
    fs.sort(key=lambda p: os.path.getmtime(p))
    n = 0
    for p in fs[:len(fs) - keep]:
        try:
            os.remove(p)
            n += 1
        except BaseException:
            pass
    return n


def fine_split(path, n=2, sub=None):
    """按音高把音块分层导出 —— 「手动分和声」。

    ★ 界面上必须照实写：这是**按音高归层**，不是把叠在一起的两个声音
      无损拆开（单声道混好的声音做不到）。主唱和和声如果音区本来就分开，
      归层的结果就能直接当两条轨用；音区重叠的地方得自己手动调。
    """
    ft = _fine_mod()
    if not path or not os.path.isfile(path):
        raise Fail("文件不存在：%s" % path)
    base = _safe_name(os.path.splitext(os.path.basename(path))[0], "audio")
    d = os.path.join(FINE_DIR, base, "和声分层")
    if sub:
        d = os.path.join(d, _safe_name(sub))
    shutil.rmtree(d, ignore_errors=True)
    blocks = ft.analyze(path).get("blocks") or []
    items = ft.split_layers(path, blocks, d, n=n)
    return {"dir": d, "items": items, "blocks": len(blocks)}


# ---------------------------------------------------------------------------
# 微调（唱前 / 唱后）：stages/polish.py 的壳
#
# 跟音块级编辑（finetune）的区别：那边是**逐个音块**改，这里是**整轨**的量+修。
# 分工理由：底噪、齿音、拼接点都是**整条轨的性质**，切成块反而看不出来
# （一块里只有 200ms，量不出噪声底，也量不出"这首歌平时的亮度"）。
#
# ★ 两边都遵守同一条约定：修之前先量，修完复测，没改善就**不写那个 wav**
#   （hf / demetal / presence 三兄弟都是这个约定，前人踩过"拿了不存在的
#   路径去混音"的坑）。
# ---------------------------------------------------------------------------

POLISH_SIDES = ("pre", "post")
POLISH_LABEL = {
    "pre": "唱前 —— 喂给模型之前：修削波 / 修爆音 / 去隆隆 / 去底噪 / 电平规整",
    "post": "唱后 —— 唱出来之后：共振峰 / 去齿音 / 去刺耳 / 高音柔化 / 修拼接 / 软限幅",
}
POLISH_STEPS = {
    "pre": ("declip", "declick", "hp", "denoise", "level"),
    "post": ("formant", "deess", "deharsh", "belt", "heal", "limit"),
}
# 每一步的"人话"，界面上要能把"为什么动了 / 为什么没动"说清楚
POLISH_STEP_LABEL = {
    "declip": "修削波", "declick": "修爆音", "hp": "去隆隆（80Hz 以下）",
    "denoise": "去底噪", "level": "电平规整",
    "formant": "共振峰修正（把被升调带跑的音色搬回来）",
    "deess": "去齿音（4.5~10k）", "deharsh": "去刺耳（2~5k）",
    "belt": "高音柔化（2.5~6k，只在高音段）", "heal": "修拼接（咔哒）",
    "limit": "软限幅",
}
# ★★ 2026-10-26：把"有哪几步"变成**一份权威清单**，界面据此画开关。
#   以前这份顺序只活在 stages/polish.py 里，界面要列开关就得手抄一遍 ——
#   两边迟早对不上（抄漏一步 = 那一步永远关不掉，用户会以为开关坏了）。
#   ⇒ /api/fine/polish/steps 把它吐给前端，前端不许再写死。
def _polish_mod():
    """拿 stages/polish.py 的权威常量（顺序/标签/默认档都从那儿来）。

    为什么现用现 import 而不是模块级：polish.py 顶层要拉 scipy/numpy，
    而 studio 有的入口（比如纯查任务的那些）根本用不到它。
    """
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    from stages import polish as _p
    return _p


POLISH_PRE_STEPS = ("declip", "declick", "hp", "denoise", "level")
# ★ 顺序必须跟 stages/polish.py 的 POST_STEPS **一模一样**（formant 在最前），
#   否则界面上的勾选框顺序会跟实际执行顺序相反 —— 用户按界面顺序理解"链条"会理解错。
#   门禁 verify_polish 有一条专门比对这两份，漂了就红。
POLISH_POST_STEPS = ("formant", "deess", "deharsh", "belt", "heal", "limit")
# ★ formant 需要知道"这条音频被升了多少半音" —— 它只能按半音数搬，不猜。
#   主链路把自己的 pitch 传进来；精修台里由用户在界面上给（不给就不动）。
POLISH_FORMANT_DEFAULT_SEMI = 0.0


def _polish_cli(args, timeout=1800):
    """跑 stages/polish.py。精修台里的操作**不挂任务**，所以不走 run(job)。"""
    import subprocess
    cmd = '"%s" -s "%s" %s' % (PY, os.path.join(STAGES, "polish.py"), " ".join(args))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, cwd=HERE, shell=True)
    except Exception as e:                                   # noqa: BLE001
        return 1, "polish 起不来：%s" % e
    return p.returncode, ((p.stdout or "") + "\n" + (p.stderr or ""))


def polish_measure(path):
    """量一条音频"脏在哪、刺在哪、接缝有几个"。**只量不动手**。"""
    if not path or not os.path.isfile(path):
        raise Fail("文件不存在：%s" % path)
    rep = os.path.join(tempfile.gettempdir(),
                       "vs_polish_m_%s.json" % uuid.uuid4().hex[:10])
    rc, txt = _polish_cli(['measure', '"%s"' % path, '"%s"' % rep], timeout=600)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(OSError):
        os.remove(rep)
    if not info or not info.get("ok"):
        raise Fail("量不出来：%s" % _tail(txt, 2))
    return info["measure"]


def polish_apply(path, side="post", amount=1.0, skip=()):
    """跑一遍微调，返回产物（没达标时**返回原路径**，applied=False）。"""
    if side not in POLISH_SIDES:
        raise Fail("不认识的微调方向：%s" % side)
    if not path or not os.path.isfile(path):
        raise Fail("文件不存在：%s" % path)
    out = _fine_out_path(path, "唱前微调" if side == "pre" else "唱后微调")
    rep = os.path.join(tempfile.gettempdir(),
                       "vs_polish_a_%s.json" % uuid.uuid4().hex[:10])
    args = [side, '"%s"' % path, '"%s"' % out, '"%s"' % rep,
            "--amount %s" % float(amount or 1.0)]
    if skip:
        args.append('--skip %s' % ",".join(str(s) for s in skip))
    rc, txt = _polish_cli(args)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(OSError):
        os.remove(rep)
    if rc != 0 or not os.path.isfile(out):
        # ★ 跟 hf / demetal / presence 同一个约定：没修 = 没写 wav。
        #   这时**不许**把 out 当成品返回（它不存在，界面会读空文件）。
        if info is not None and not info.get("applied"):
            return {"applied": False, "path": path, "side": side,
                    "reason": info.get("reason") or "没有一步达标",
                    "steps": info.get("steps") or []}
        raise Fail("微调没跑通：%s" % _tail(txt, 2))
    return {"applied": True, "path": out, "side": side,
            "name": os.path.basename(out),
            "mb": round(os.path.getsize(out) / 1048576.0, 2),
            "steps": (info or {}).get("steps") or [],
            "before": (info or {}).get("before"),
            "after": (info or {}).get("after")}


# ★ 主链路里这两步的开关（"off" 就整段跳过）。界面开关是下一步的事 ——
#   先把"它到底有没有用"用实测证明，再谈要不要给用户一个按钮。
POLISH_PRE_MODE = "auto"
POLISH_POST_MODE = "auto"


def polish_stage(job, side, wav, out_wav, skip=(), amount=1.0, semi=0.0):
    """主链路里的一步：跑 polish，把每一步"动了 / 没动"写进任务日志。

    semi = 这条音频被**升**了多少半音（正=升）。只有 formant 那一步用得上，
    而且它**只认这个数** —— 不给就一步不动（不猜）。

    返回 (要用的 wav, info 或 None)。跑不通**不抛异常** —— 修不修得动是
    "这一版的性质"，不该把整首歌拖死（hf / demetal / presence 都是这个调子）。
    """
    if not wav or not os.path.isfile(wav):
        log(job, "  [warn] 微调（%s）找不到输入，跳过" % side)
        return wav, None
    rep = os.path.join(os.path.dirname(out_wav) or ".", "_polish_%s.json" % side)
    args = '%s "%s" "%s" "%s" --amount %s' % (side, wav, out_wav, rep, amount)
    try:
        args += " --semi %s" % float(semi or 0.0)
    except (TypeError, ValueError):
        pass
    if skip:
        args += " --skip %s" % ",".join(str(s) for s in skip)
    rc, txt = run(job, '"%s" -s "%s" %s'
                  % (PY, os.path.join(STAGES, "polish.py"), args), timeout=1800)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(OSError):
        os.remove(rep)
    if rc != 0 or not os.path.isfile(out_wav):
        # ★ 没修（SKIP）时子进程**不写 wav** —— 这时拿 out_wav 当成品会读空文件。
        if info is not None and not info.get("applied"):
            log(job, "  %s微调：%s"
                % ("唱前" if side == "pre" else "唱后", info.get("reason") or "没修"))
            return wav, None
        log(job, "  [warn] 微调（%s）没跑通（退出码 %d）：%s"
            % (side, rc, _tail(txt, 2)))
        return wav, None
    for s in (info or {}).get("steps") or []:
        nm = POLISH_STEP_LABEL.get(s.get("name"), s.get("name"))
        if s.get("skipped"):
            log(job, "    · %s：跳过（%s）" % (nm, s.get("reason") or "没要求"))
        elif s.get("kept"):
            if s.get("name") in ("level", "limit"):
                # 这两步不是"修毛病"，是收尾 —— 报"好转多少 dB"没有意义
                log(job, "    ✔ %s：峰值收到 %.3f" % (nm, s.get("peak")))
            else:
                log(job, "    ✔ %s：已修（好转 %s dB，副作用 %s dB）"
                    % (nm, s.get("gain"), s.get("harm")))
        else:
            log(job, "    · %s：%s" % (nm, s.get("reason") or "试过但没达标 ⇒ 不动"))
    return out_wav, info


def step_slice(job, vocal_dir, out_dir):
    job["stage"] = "切分片段"
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    # ★ 先走保真切片（48k，见 SLICE_SR 的注释）；它跟自带工具切出的是**同一批段**，
    #   只是采样率更高。真出问题时**退化**到自带那份 32k —— 增强功能失败不许比
    #   改之前更差，这条边界必须现场兜住，不能靠"它应该不会失败"。
    hifi = os.path.join(HERE, "stages", "slice_hifi.py")
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" %d %s'
                  % (PY, hifi, vocal_dir, out_dir, SLICE_SR, " ".join(SLICE_ARGS)),
                  env={"GSV_ROOT": GSV_ROOT}, timeout=1800)
    files = [f for f in os.listdir(out_dir) if f.lower().endswith(".wav")]
    if not files:
        log(job, "  保真切片（%d Hz）没切出东西，退回自带切片工具（32k）" % SLICE_SR)
        rc, out = run(job, '"%s" -s tools/slice_audio.py "%s" "%s" %s'
                      % (PY, vocal_dir, out_dir, " ".join(SLICE_ARGS)), timeout=1800)
        files = [f for f in os.listdir(out_dir) if f.lower().endswith(".wav")]
    if not files:
        raise Fail("一个片段都没切出来，可能人声轨是空的。")
    good = []
    for f in files:
        p = os.path.join(out_dir, f)
        s = wav_seconds(p)
        r = wav_rms(p)
        if s >= 1.0 and s <= 15.0 and r > 0.005:
            good.append((f, s, r))
    log(job, "  切出 %d 段，合格 %d 段" % (len(files), len(good)))
    if len(good) < 5:
        raise Fail("只有 %d 段可用（至少 5 段）。" % len(good))
    return out_dir, [f for f, _, _ in good]


def step_feat(job, slice_dir, out_json):
    job["stage"] = "提取声纹"
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "feat_cli.py"), GSV_ROOT, slice_dir, out_json),
                  timeout=3600)
    if not os.path.exists(out_json):
        raise Fail("特征提取失败。")
    return json.load(open(out_json, encoding="utf-8"))


def step_split_speakers(job, feats, n_speakers=None):
    job["stage"] = "分离不同声音"
    # 必须**用整合包的 python 起子进程**跑，不能在服务进程里 import diarize。
    #
    # 事故记录：服务是用零依赖的托管Python 起的（不是整合包那个），
    # `import diarize` 当场ModuleNotFoundError: No module named 'numpy'，
    # 整个导入流程在第 6 步崩掉 —— 而前 5 步（抽音/UVR/切片/特征）全部正常，
    # 所以界面上看是"跑到一半 Training failed"，很难想到是缺依赖。
    #
    # 教训：**依赖 numpy 的模块不能在服务进程里 import**，服务用什么解释器
    # 启动是可变的（bat 用整合包、手动起可能用别的）⇒ 不能把正确性押在它上面。
    # 跟其它步骤统一走子进程，就彻底与"服务用什么 python"解耦了。
    work = os.path.join(job["dir"], "work")
    os.makedirs(work, exist_ok=True)
    fin = os.path.join(work, "feats_for_split.json")
    fout = os.path.join(work, "split_result.json")
    with open(fin, "w", encoding="utf-8") as f:
        json.dump(feats, f, ensure_ascii=False)
    n_arg = "" if n_speakers is None else str(int(n_speakers))
    # 这里踩过一个很坑的错：写成
    #     run(job, '"%s" -s "%s" "%s" %s' % (PY, ..., fin, fout, n_arg))
    # 报TypeError: not all arguments converted during string formatting。
    # 看着像"格式化语法错"，其实**报错来自 run() 内部那行 log()**——
    # 命令行里带了% 字符（本机路径就有 %USERPROFILE% 之类），
    # 而 run() 里对命令做了 % 格式化/百分号处理。教训：**带 % 的路径
    # 不能进 %格式化**。所以这里改用 str.format，占位符用 {}。
    cmd = '"{}" -s "{}" "{}" {}'.format(
        PY, os.path.join(STAGES, "diarize.py"), fin, fout, n_arg)
    rc, out = run(job, cmd)
    if rc != 0 or not os.path.exists(fout):
        raise Fail("说话人分离失败（退出码 %s）。%s" % (rc, _tail(out)))
    res = json.load(open(fout, encoding="utf-8"))

    for t in res.get("trace", []):
        log(job, "    k=%s %s —— %s" % (t[0], "成立" if t[1] else "不采纳", t[2]))
    groups = {}
    for f, lab in zip(feats, res["labels"]):
        groups.setdefault(int(lab), []).append(f)
    log(job, "  分成 %d 组：%s" % (res["k"], {k: len(v) for k, v in sorted(groups.items())}))
    return groups, res


# ------------------------------------------------------------ 声纹认人
#
# 需求原话：「可以在添加素材的情况下自动区分这个素材是谁，把属于这个角色的素材加进去，
# 不会被其他角色影响」。以前只有两条路：全新建一个角色（同一个人的新素材会散成好几个
# 角色），或者用户在界面上手动指定要塞给谁。这里补第三种：**自动认人**。
#
# 判据 = 「新素材每一段到某个角色中心向量的余弦距离」的**中位数**。
# 为什么不是"两个中心之间的余弦距离"（第一版就是这么写的，被实测否掉）：
#   中心对中心主要量的是"每侧有多少段"（抽样噪声），不是"是不是同一个人"。
#   实测：同一个角色切 4 份（每份 53 段）中心距离最大 0.131，而随机分半（每份
#   106 段）只有 0.004 —— 差两个数量级；换成"异人"侧更是直接重叠
#   （同人 0.030~0.131 vs 异人 0.066~0.264）。
#   改用逐段对中心取中位数后：段少也不会把中心带跑，少数分错的段也挪不动中位数。
#
# ★ 用哪一份向量：`sv`，**不是** `emb`。feat_cli 一次存两份（见 stages/feat_cli.py）：
#     emb = `compute_embedding3()` 走 `forward3`，是**时间平均后的融合特征**（20480 维）。
#           GPT-SoVITS 拿它做别的用途，不是给"是不是同一个人"训练的。
#     sv  = 模型自带的说话人嵌入头 `forward` → `seg_1(stats)`（**192 维**）。
#           eres2net 这份预训练权重本来就是为说话人验证训的，这一层才是真嵌入。
#   实测（`_probe_speaker_combo.py`：真实素材，40 正 / 40 负）：
#     阈值        emb 收进率 / 误收率      sv 收进率 / 误收率
#     0.20        68% / 0%                70% / 0%
#     0.22        88% / **2%**           85% / 0%
#     0.23        88% / **10%**          92% / 0%
#     0.25        98% / **15%**          92% / **8%**
#   ⇒ `emb` 的"零误收安全带"只有 0.20~0.21（宽 0.01），一放宽立刻漏；
#     `sv` 的安全带是 0.20~0.24（宽 0.04），同一档上收进率还更高。
#   ⇒ 附带的实惠：192 维 vs 20480 维 —— 角色声纹缓存小 100 倍、比对快 100 倍。
#   ★ 还量过"两个向量 AND 投票"（要两道闸都放行才并）：**没有收益**。
#     异人侧样本两道闸拦的是同一批（"只有 emb 拦下" 0 个、"只有 sv 拦下" 0 个），
#     所以 AND 只会被弱的那道（emb）拖住 —— 同人收进率从 92% 掉到 78%，误收没有更少。
#     ⇒ 用 sv 单路。
#
# ★★ **薄中心**才是真正的坑（这一条是被一条"红得有道理"的门禁判据逼出来的）。
#   门禁里那个基准角色的中心只用 **17 段**算出来，结果"别人的素材"到它只有 0.2103
#   —— 低于当时的阈值 0.23 ⇒ 库里要是没有那个人自己的角色卡，这段素材就会被并进来。
#   一开始我猜是"中心太糊 ⇒ 什么都离它近"，被实测**否掉**：同人自比几乎不随段数变
#   （0.180 → 0.163）。真正的原因是异人侧的**左尾**被压低了。40 次抽样量出来
#   （`_probe_center_quality.py`；★ 每档必须抽够 40 次 —— 只抽 8 次时 p10 取到的是
#   最小值，结果在 0.198~0.272 之间乱跳，看着像"没规律"，其实是样本不够）：
#     中心用 k 段 | 同人 p95（越高越危险） | 异人 p25（越低越危险） | 异人 p10
#       8        | **0.3151**            | 0.2517                | 0.1976
#       12       | 0.1981                | 0.2566                | 0.2355
#       17       | 0.2007                | 0.2579                | 0.2404
#       22       | 0.1973                | 0.2496                | 0.2266
#       30       | 0.1897                | 0.2652                | 0.2379
#       45       | 0.1891                | 0.2684                | 0.2371
#       60       | 0.1801                | 0.2614                | 0.2453
#       120      | 0.1800                | 0.2624                | 0.2521
#   ⇒ 段数 < 30 时异人 p10 落到 0.2266~0.2404（**低于 0.23 阈值** ⇒ 会误收），
#     而 ≥30 段时异人 p25 稳定在 0.26 以上、同人 p95 在 0.19 以下，余量有 0.07。
#   ⇒ 两条修正：① 中心段数 < MIN_CENTER_SEGS 的角色**不作为认人候选**（薄中心直接出局）；
#              ② 阈值从 0.23 收到 **0.21**。
#     在 ≥30 段中心的条件下：同人 p95 0.19 < **0.21** < 异人 p25 0.26。
#
# 阈值实测（`_probe_speaker_calib.py` + `_probe_speaker_combo.py` + `_probe_center_quality.py`）：
#   sv 同人 p10/med/p90 = 0.144 / 0.178 / 0.228；异人 = 0.266 / 0.281 / 0.317。
#   ⚠ **样本只有 1 个异人**（另一个视频里的人声）。"误收 0%"只能说是"在这一个人
#     身上没漏"，不等于在所有人身上都不漏。所以取向定死：**宁可认不出（新建角色），
#     也不许错并** —— 错并是静默污染训练集，认不出只是多建一个角色、用户手动一并就好。
VOICE_KIND = "sv"        # 声纹向量种类；换模型/换向量时改这里 ⇒ 老缓存自动失效重算
VOICE_DIM_REF = 192      # 仅供文档/门禁参考；真正的判据是"两侧维度相等"
MATCH_DIST = 0.21        # 逐段到中心距离的**中位数**门槛（见上；≥30 段中心下安全带 0.19~0.26）
MIN_CENTER_SEGS = 30     # 中心至少用这么多段才算"实心"，否则不作为候选（见上，薄中心会误收）
MATCH_MIN_SEGS = 6       # 新素材少于 6 段，中位数不稳 ⇒ 宁可不认（认错比认不出更糟）
# ★ 维度**不写死**。第一版我按文档写了 VOICE_DIM=192，第一次跑就翻车：feat_cli 存下来的
#   `emb` 不是 eres2net 那一层 192 维的 embedding，而是 `compute_embedding3` 出来的
#   多层拼接（实测 20480 维）。真正要守的性质不是"等于 192"，而是"**两侧维度必须一致，
#   不一致就不许比**"（长度不一样的向量 zip 起来只会静默截断，得出一个看起来正常的错数）。
#   所以维度只记在角色记录里（voice.dim），比之前先核对。
ROLE_VOICE_SEGS = 60     # 一个角色的声纹中心最多抽多少段来算（稳定 vs 耗时）
VOICE_MIN_DUR = 2.0      # 和 diarize.py 的质量门控保持一致
VOICE_MIN_VOICED = 0.30

# --------------------------------------------------------------------
# 段级纯净度（2026-10-19 新增）
#
# 老板的原话：「不会把其他声音切进去当素材……会自检，把不属于这个声音的音频剔除」。
#
# ★ 先量清楚问题在哪，别照直觉改（`_probe_segpurity*.py` 的实测）：
#   半段(2.5s) 口径下量了三组对照——
#       同一个人换句话（同人拼）       p50 = 0.313
#       换了一个人（异人拼）           p50 = 0.614
#       **真实切片自己的前半 vs 后半** p50 = 0.232
#   ⇒ 真实段内比"同一个人换句话"还小 ⇒ **段内本来就没什么混音**。
#     所以"把段切得更碎"是错的：会把完整的句子切烂，还捞不出东西。
#     真问题是**段与段之间**：一堆素材里可能混着别人的**整段**，而簇级只取中位数，
#     中位数照样过阈值 ⇒ 那些段被一并并进去，训练集被静默污染。
#
# ★ 于是判据落在"整段"这个和认人同口径的层上。
#
# ★★ 标定踩过一个坑，记下来别再犯（`_probe_segthr.py`）：
#   第一版是"拿大帝 120 段算中心，再拿**这同一批**去比" ⇒ 同人 p95 只有 0.134，
#   看着安全带 0.13~0.33 宽得很，于是阈值定了 0.25。
#   **这是自比，等于拿尺子量它自己** —— 真实场景是「**新素材** vs **已有角色中心**」，
#   两批内容不同、中心本身还是个抽样估计，距离必然更大。
#   端到端一试（`verify_speaker_match` 当场红）：**20 段同人素材被剔掉 2 段（10%）**。
#   改成**跨批**（A 批 60 段算中心，B 批另外 60 段当新素材，两批不重叠）重量：
#       跨批同人  p50 = 0.092  p90 = 0.142  p95 = 0.182  **max = 0.253**
#       异人      p05 = 0.374  p10 = 0.391  p50 = 0.467
#   阈值扫描（跨批口径）：
#       0.25 → 误杀同人 1.7%（正是上面那 10% 的来源）
#       0.28 / 0.30 / 0.32 / 0.35 → **误杀 0% 且漏放 0%**
#       0.38 → 开始漏放异人 5.4%
#   ⇒ 取 **0.30**：同人 max 0.253 与异人 p05 0.374 的中点，两头各留 ~0.07 余量。
#
# ⚠ 另外注意：簇级 MATCH_DIST = 0.21 比这个值**小**，不是写反了。
#   簇级取的是「一堆段距离的中位数」（稳定，分布窄），段级是**单个段**（噪声大，
#   分布宽得多）。同一个人在两个层上的水位本来就不同，别拿一个去套另一个。
#
# ⚠ 和 MATCH_DIST 一样，样本里**只有 1 个异人**（大帝 vs 阿塔尼斯）。"漏放 0%"
#   只能说是在这一个人身上没漏。取向仍然定死：**宁可多剔，也不错并** ——
#   错并是静默污染训练集，多剔只是少几段素材、而且剔掉的进了「待归类」还能救回来。
SEG_MATCH_DIST = 0.30    # 单段到角色中心的门槛（跨批口径：同人 max 0.253 / 异人 p05 0.374）
SEG_MIN_DUR = 2.0        # 比这更短的段声纹不可信，段级复核直接跳过（留给簇级判）

# 簇内离群剔除（`purge_outliers`）—— 管的是**没认出来、要新建角色**的那一堆。
# 那一堆里可能混着杂音或另一个人的段，没有角色中心可对照，只能拿这一堆自己的
# 中心当尺子，用 中位数 + MAD 把明显离群的挑出去。
#
# ★ 实测（`_probe_outlier.py`，大帝 120 段当纯净堆；再混 25 段阿塔尼斯当脏堆）：
#     med + K·MAD **没有绝对下限**时，纯净堆会被砍掉 **4.2%**（K=3.5）——
#     同一批素材的正常尾巴也被当成离群，白损失素材。加上 floor 之后归零：
#       K=3.5 floor=0.30 → 纯净堆剔除 **0/120**；脏堆抓到异人 16/25（64%），误杀 0
#       K=4.0 floor=0.35 → 纯净堆 0/120；抓到 9/25（36%）—— 更保守，召回更低
#   ⇒ 取 K=3.5 + floor=0.30：误杀 0%，召回 64%。
#     召回只有六成是故意的：剩下的异人段和大帝挨得太近，硬抓就会开始误杀。
#     取向仍然是"宁可少剔，也不错杀"——错杀是真素材没了，漏掉的还能靠段级复核补。
OUTLIER_K = 3.5          # MAD 倍数
OUTLIER_FLOOR = 0.30     # ★ 绝对下限，缺了它纯净堆会被砍（见上）
OUTLIER_MIN_SEGS = 10    # 段太少时 MAD 不稳，不做簇内剔除


def _emb_center(embs):
    """一组 embedding 的均值，再 L2 归一化。返回 list（好写进 json）。"""
    import math
    if not embs:
        return None
    n, d = len(embs), len(embs[0])
    c = [0.0] * d
    for e in embs:
        for i, v in enumerate(e):
            c[i] += v
    c = [v / n for v in c]
    norm = math.sqrt(sum(v * v for v in c)) or 1e-12
    return [v / norm for v in c]


def _emb_dist(a, b):
    """余弦距离（0 = 同一个向量）。两条都得是先归一化过的中心。

    ★ 长度不一致时**返回 None，绝不上截断**：zip 会静默按短的那个截断，
      然后算出一个"看起来很正常"的距离 —— 两个不同模型的特征被这么比过，
      结论是没有意义的（踩过的同类坑：坐标系不一样就该显式拒绝）。
    """
    import math
    if not a or not b or len(a) != len(b):
        return None
    num = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1e-12
    nb = math.sqrt(sum(x * x for x in b)) or 1e-12
    return 1.0 - num / (na * nb)


def _voice_pick(files, limit=ROLE_VOICE_SEGS, segs=None):
    """从一批切片里挑出用来算声纹中心的段：先按质量门控筛，再等间隔抽样。

    等间隔（而不是取前 N 个）是因为切片是按时间排的，取前面 60 段可能全是同一段
    副歌/同一句话，中心会偏向那一段的音色；等间隔能铺满整条素材。
    """
    ok = []
    meta = {s.get("file"): s for s in (segs or [])}
    for f in files:
        m = meta.get(os.path.basename(f)) or {}
        dur = float(m.get("dur") or 0)
        voiced = float(m.get("voiced") or 0)
        # 没带元信息时（老角色）只能先收进来，靠 feat 结果再筛
        if meta and (dur < VOICE_MIN_DUR or voiced < VOICE_MIN_VOICED):
            continue
        ok.append(f)
    if not ok:
        return []
    if len(ok) <= limit:
        return ok
    step = len(ok) / float(limit)
    return [ok[int(i * step)] for i in range(limit)]


def _feat_subprocess(files, tag):
    """让 feat_cli 只算 `files` 这几段，返回它输出的记录列表（失败返回 []）。

    必须走子进程：feat_cli 依赖 numpy/torch，而服务用什么解释器起是可变的
    （见 step_split_speakers 上面那段事故记录，同一个坑不再踩第二次）。

    ★ **名字走 argv、结果走 stdout ⇒ 零拷贝、零临时文件、零删除。**
      这版之前是"mkdtemp + copy 60 个 wav + 跑完 rmtree"，更早一版是"写一个清单文件、
      跑完删掉" —— 两种写法一次调用都是几十个删除动作，而**本机环境层有批量删除钩子**
      （阈值 50/轮）。被它掐断的样子是"整个脚本一行输出都没有"，非常难查。
      顺带还省掉了 60 个 4 秒 wav 的复制（每次导入都要拷一遍）。
    ★ Windows 命令行有长度上限（约 32K），名字太多时宁可不认人，也不塞爆。
    ★ 这里**不能**借 run(None, ...)：run 里第一句就是 log(job, ...)，而 log 直接
      job.get(...) —— 传 None 当场 AttributeError。认人这一步可能在"没有任务上下文"
      的时候被调到（比如给老角色补算中心），所以自己起子进程、自己收输出。
    """
    if not files:
        return []
    dirs = {os.path.dirname(f) for f in files}
    if len(dirs) != 1:
        # 只支持"同一个目录里的若干文件"；跨目录宁可不认，别猜
        return []
    names = [os.path.basename(f) for f in files]
    if sum(len(n) + 1 for n in names) > 20000:
        return []
    cmd = [PY, "-s", os.path.join(STAGES, "feat_cli.py"),
           GSV_ROOT, dirs.pop(), "-"] + names
    try:
        r = subprocess.run(cmd, cwd=GSV_ROOT, env=base_env(),
                           capture_output=True, text=True, timeout=1800)
    except Exception as e:
        sys.stderr.write("[warn] 声纹提取(%s)起不来：%s\n" % (tag, e))
        return []
    if r.returncode != 0 or not (r.stdout or "").strip():
        sys.stderr.write("[warn] 声纹提取(%s)失败：rc=%s %s\n"
                         % (tag, r.returncode, (r.stderr or "")[-200:]))
        return []
    try:
        return json.loads(r.stdout)
    except Exception as e:
        sys.stderr.write("[warn] 声纹提取(%s)的输出不是 JSON：%s\n" % (tag, e))
        return []


def build_role_voice(rid, job=None, force=False):
    """算并落盘一个角色的声纹中心。返回 voice dict（失败返回 None）。

    结果缓存在角色记录的 `voice` 字段里（跟 segments 一起进 roles.json）。
    素材变了（追加/重拆）必须用 `force=True` 重算 —— 调用点就是 job_ingest 的建档循环
    （那边每建/更一次角色都 force 一次）。不重算的话下一次认人用的还是老中心，
    而这个角色已经多了一批素材，严重时会把别人认成它。
    （注：这里**没有**单独的"标脏"函数，别去找 `refresh_role_voice` —— 不存在，
      历史上只是文档里提过一句，实际做法一律是就地 force 重算。）
    ★ 缓存带 `kind`（用的哪一路向量）。`kind` 和当前 VOICE_KIND 不一致 ⇒ 视为**陈旧**，
      即使不 force 也重算 —— 否则从 emb 换到 sv 之后，老角色的 20480 维中心会一直
      被当成有效缓存：维度对不上 ⇒ 每次认人它都被 skip ⇒ 表现成"这个角色永远认不出"，
      而且**不报错**。这类"看着正常实则是废数据"的缓存必须靠版本号淘汰。
    """
    role = get_role(rid)
    if not role:
        return None
    v = role.get("voice") or {}
    if v.get("center") and v.get("kind") == VOICE_KIND and not force:
        return role["voice"]
    sd = os.path.join(role_dir(rid), "slices")
    if not os.path.isdir(sd):
        return None
    files = [os.path.join(sd, f) for f in sorted(os.listdir(sd))
             if f.lower().endswith(".wav")]
    picked = _voice_pick(files, segs=role.get("segments"))
    if len(picked) < MATCH_MIN_SEGS:
        return None
    vecs = [x[VOICE_KIND] for x in _feat_subprocess(picked, rid)
            if x.get(VOICE_KIND)]
    if len(vecs) < MATCH_MIN_SEGS:
        return None
    dims = {len(e) for e in vecs}
    if len(dims) != 1:
        # 同一批里维度都不一致，说明提取那边出了状况 —— 宁可不认
        raise Fail("同一批声纹的维度不一致：%s" % sorted(dims))
    c = _emb_center(vecs)
    voice = {"center": c, "n": len(vecs), "dim": len(c), "kind": VOICE_KIND,
             "model": "eres2net/feat_cli", "at": _now()}
    mutate_role(rid, lambda r: r.__setitem__("voice", voice))
    if job:
        log(job, "  角色 %s 的声纹中心：用了 %d 段（%s，%d 维）"
            % (rid, len(vecs), VOICE_KIND, len(c)))
    return voice


def ensure_role_voices(job=None, rids=None):
    """把还缺声纹中心的角色补算出来（认人之前调一次）。

    为什么要这一步：这个功能是后加的，**老角色记录里没有 voice 字段**
    （比如用户以前导入的素材）。不补算的话，第一次用"自动认人"时会发现
    "库里一个中心都没有"，于是全部新建角色 —— 功能看起来像没生效。
    补算一次就缓存在角色记录里，之后每次导入都直接复用。
    ★ 换了声纹向量（`kind` 变了）也算"缺"：老中心维度对不上，留着只会静默失效。

    返回 (算好了几个, 跳过几个)。跳过 = 切片目录不在或段数太少。
    """
    made = skipped = 0
    for r in load_roles()["roles"]:
        if rids and r["id"] not in rids:
            continue
        v = r.get("voice") or {}
        # 有中心**而且是对的那一路向量**才算有效缓存；kind 变了要重算（见 build_role_voice）
        if v.get("center") and v.get("kind") == VOICE_KIND:
            continue
        if r.get("pending"):
            continue
        try:
            if build_role_voice(r["id"], None, force=True):
                made += 1
                if job:
                    log(job, "  补算声纹中心：%s（%s）" % (r.get("name"), r["id"]))
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            if job:
                log(job, "  [warn] %s 的声纹中心算不出：%s" % (r.get("name"), e))
    return made, skipped


def _known_voices():
    """库里**有资格**参与认人的声纹中心。返回 (known, stale, thin)。

    known = [(rid, name, center, dim), ...]

    两种角色直接出局，理由都是实测过的（见 MATCH_DIST 上面的注释）：
      · kind 不是当前 VOICE_KIND —— 换了向量种类之后的老缓存，维度对不上，
        比起来会静默截断（长度不一样的向量 zip 一下只比对到短的那截）。
      · 中心段数 < MIN_CENTER_SEGS —— **薄中心是真正的坑**：实测 <30 段时
        "异人"侧 p10 会掉到 0.2266~0.2404，低于阈值 ⇒ 别人的素材会被并进来。
    """
    known = []
    stale = thin = 0
    for r in load_roles()["roles"]:
        v = r.get("voice")
        if not (v and v.get("center")):
            continue
        if v.get("kind") != VOICE_KIND:
            # 老缓存（换了向量种类）—— 上层应该已经用 ensure_role_voices 补算过；
            # 还在这儿说明补算失败/被跳过。绝不拿它比（维度对不上会静默截断）。
            stale += 1
            continue
        # ★ 中心段数不够 ⇒ 不出任候选。这类角色仍然留着中心（素材攒够了自己会变
        #   "实心"），只是这次不参与认人。
        if int(v.get("n") or 0) < MIN_CENTER_SEGS:
            thin += 1
            continue
        known.append((r["id"], r.get("name") or r["id"], v["center"], v.get("dim")))
    return known, stale, thin


def match_clusters(job, groups, feats):
    """给每个簇找「这是谁」。返回 ({簇标签: 角色id}, [给人看的说明])。

    只认**过了阈值**的；认不出的返回 None，由上层新建角色。
    ★ 判据是"这一堆里的每一段到角色中心距离的**中位数**"，不是"两个中心之间的距离"
      —— 原因和实测数据见 MATCH_DIST 上面的注释（中心对中心会被抽样噪声带跑）。
    ★ 这一层**只管整堆**。堆里混着别人的整段时中位数照样能过阈值，
      那些段要靠 `refine_segments()` 逐段复核挑出来 —— 两层各管一件事，别合并。
    """
    meta = {f["file"]: f for f in feats}
    known, stale, thin = _known_voices()
    skipped_dim = 0
    if not known:
        bits = []
        if stale:
            bits.append("%d 个是旧格式声纹（%s 之前算的）" % (stale, VOICE_KIND))
        if thin:
            bits.append("%d 个的声纹中心段数不足 %d（薄中心会误收，不许当候选）"
                        % (thin, MIN_CENTER_SEGS))
        return {}, ["角色库里没有可用的声纹中心%s（这次全部新建角色）"
                    % ("：" + "、".join(bits) if bits else "（第一次导入时这是正常的）")]

    assign, notes = {}, []
    if thin:
        notes.append("有 %d 个角色的声纹中心段数不足 %d，这次不当候选"
                     "（薄中心会把别人认成它）" % (thin, MIN_CENTER_SEGS))
    for lab in sorted(groups, key=lambda x: str(x)):
        try:
            if int(lab) < 0:            # 没挂上的杂音，不参与认人
                continue
        except (TypeError, ValueError):
            pass
        segs = groups[lab]
        vecs = [meta[f["file"]][VOICE_KIND] for f in segs
                if meta.get(f["file"], {}).get(VOICE_KIND)]
        if len(vecs) < MATCH_MIN_SEGS:
            notes.append("第 %s 堆只有 %d 段，太短不认人（宁可不认，也不乱认）"
                         % (lab, len(vecs)))
            continue
        d_new = len(vecs[0])
        # 每一段各自和每个角色的中心比，取中位数当这一堆的"像不像"
        scored = []
        for kid, kn, kc, kdim in known:
            if kdim != d_new:
                skipped_dim += 1
                continue
            ds = sorted(x for x in (_emb_dist(e, kc) for e in vecs)
                        if x is not None)
            if not ds:
                continue
            scored.append((ds[len(ds) // 2], kid, kn))
        if not scored:
            notes.append("第 %s 堆的声纹维度（%d）和库里所有角色都对不上，"
                         "这次不认人（换了声纹模型时必须重新建档）"
                         % (lab, d_new))
            continue
        scored.sort(key=lambda t: t[0])
        d, kid, kn = scored[0]
        if d <= MATCH_DIST:
            assign[lab] = kid
            notes.append("第 %s 堆（%d 段）认成「%s」：中位距离 %.3f ≤ 阈值 %.2f"
                         % (lab, len(segs), kn, d, MATCH_DIST))
        else:
            second = ("；第二近的「%s」%.3f" % (scored[1][2], scored[1][0])
                      if len(scored) > 1 else "")
            notes.append("第 %s 堆（%d 段）谁都不像：最近的「%s」中位距离 %.3f > 阈值 %.2f%s"
                         " ⇒ 新建角色"
                         % (lab, len(segs), kn, d, MATCH_DIST, second))
    return assign, notes


def refine_segments(job, groups, feats, assign):
    """一堆素材认成某人（或决定新建）之后，**逐段**再核一次。

    为什么需要这一层（实测依据见 SEG_MATCH_DIST 的注释）：簇级只取中位数，
    一堆里混进别人的**整段**时中位数照样过阈值 ⇒ 那些段被一并并进角色，
    训练集被静默污染。这一层按段判，能把它们挑出来。

    三种结果：
      · 这一段离某个角色 ≤ SEG_MATCH_DIST   ⇒ **归它**（可能是从原堆改判过来的）
      · 谁都不像，但原堆已经认成了某个角色  ⇒ **剔除**（不像还塞进去就是污染）
      · 谁都不像，且原堆本来就要新建角色    ⇒ 留在新建那堆里（它本来就是个新的人）

    返回 (owner, rejected, notes)：
      owner    = {file: rid} —— 明确归属某个角色的段（上层拿它重新分堆）
      rejected = {file, ...} —— 明确判为"谁都不像、却混在被认出的堆里"⇒ 该剔除的段
      两者都不在的段 = 段级没表态（太短 / 没提着声纹），**留在原堆**，不许当成剔除
      —— 这个区分很要紧：混为一谈会把"太短的段"也一并剔掉，白白损失素材。
    """
    meta = {f["file"]: f for f in feats}
    known, _stale, _thin = _known_voices()
    if not known:
        return {}, set(), ["库里没有可用声纹中心，跳过段级复核（这次按簇级结果走）"]

    owner, rejected = {}, set()
    kept = moved = dropped = skipped = 0
    # 逐段比对这一段可能要跑几百段，界面上得能看见"走到第几段了"：
    # 每 25 段更新一次 hint（太频繁会一直在写任务文件，没必要）。
    _tot = sum(len(g or []) for g in groups.values())
    _done = 0
    for lab in sorted(groups, key=lambda x: str(x)):
        try:
            if int(lab) < 0:      # 没挂上的杂音堆：本来就要进待归类，不复核
                continue
        except (TypeError, ValueError):
            pass
        target = assign.get(lab)
        for f in groups[lab]:
            _done += 1
            if _tot and _done % 25 == 0:
                step_hint(job, "正在逐段确认归属，把不属于这个声音的挑出去"
                               "（第 %d/%d 段）" % (_done, _tot))
            name = f.get("file")
            if not name:
                continue
            v = (meta.get(name) or {}).get(VOICE_KIND)
            if not v:
                continue
            # ★ 太短的段声纹不可信（和 diarize 的 MIN_DUR 同口径）：拿噪声去判归属
            #   只会乱剔，这种段交回簇级结果，这一层不动它。
            if float(f.get("dur") or 0) < SEG_MIN_DUR:
                skipped += 1
                continue
            scored = []
            for rid, rname, cen, dim in known:
                if dim != len(v):
                    continue
                d = _emb_dist(v, cen)
                if d is not None:
                    scored.append((d, rid, rname))
            if not scored:
                continue
            scored.sort(key=lambda t: t[0])
            d, rid, rname = scored[0]
            if d <= SEG_MATCH_DIST:
                owner[name] = rid
                # rid != target 就有两种：从"认成张三"改判给李四；或原堆本要新建、
                # 结果这段其实是库里已有的李四。两种都算"改判"。
                if rid != target:
                    moved += 1
                else:
                    kept += 1
            elif target:
                # 谁都不像，但这堆被认成了某个人 ⇒ 这段是混进来的，剔掉。
                # 若 target 为空（这堆本来就要新建），则什么都不做 = 留在新角色里。
                rejected.add(name)
                dropped += 1
    notes = ["段级复核：归位 %d 段，改判给更近的角色 %d 段，剔除 %d 段（不像却混在被认出的堆里）"
             % (kept, moved, dropped)]
    if skipped:
        notes.append("另有 %d 段短于 %.1f 秒，声纹不可信不参与段级复核（仍按簇级走）"
                     % (skipped, SEG_MIN_DUR))
    return owner, rejected, notes


def purge_outliers(job, segs, feats):
    """没认出来、准备新建角色的那一堆里，把**明显不属于这一堆**的段挑出去。

    和 `refine_segments` 的区别，别混着用：
      · refine_segments —— 有角色中心可对照，判"这段像不像库里那个人"（认人场景）
      · purge_outliers  —— **没有**角色中心可对照，只能拿这一堆自己的中心当尺子
                           （新建角色场景：这一堆里混了杂音/别人的段）

    返回 (keep, outliers, note)：
      keep     —— 留下的（含"段级没法评估"的段，那些一律留着不乱动）
      outliers —— 判为离群的段，由上层送进「待归类」，不丢弃
    """
    meta = {f["file"]: f for f in feats}
    pool = []
    for f in segs:
        v = (meta.get(f.get("file")) or {}).get(VOICE_KIND)
        # 太短的段声纹不可信（和 SEG_MIN_DUR 同口径），不参与统计
        if v and float(f.get("dur") or 0) >= SEG_MIN_DUR:
            pool.append((f, v))
    if len(pool) < OUTLIER_MIN_SEGS:
        return segs, [], ("这一堆只有 %d 段可评估（<%d），MAD 不稳，跳过簇内剔除"
                          % (len(pool), OUTLIER_MIN_SEGS))

    import statistics
    c = _emb_center([v for _, v in pool])
    ds = []
    for f, v in pool:
        d = _emb_dist(v, c)
        ds.append(d if d is not None else 0.0)
    m = statistics.median(ds)
    mad = statistics.median([abs(x - m) for x in ds])
    thr = max(m + OUTLIER_K * mad, OUTLIER_FLOOR)

    evaluated = {}
    keep, out = [], []
    for (f, _v), d in zip(pool, ds):
        evaluated[f.get("file")] = True
        (out if d > thr else keep).append(f)
    # 没参与评估的段（太短 / 没声纹）：一律留下，不许因为"没表态"就被剔掉
    for f in segs:
        if not evaluated.get(f.get("file")):
            keep.append(f)
    note = ("这一堆 %d 段：簇内中位 %.3f、MAD %.3f ⇒ 门槛 %.3f，剔出 %d 段离群"
            % (len(pool), m, mad, thr, len(out))) if out else \
           ("这一堆 %d 段：簇内中位 %.3f、MAD %.3f ⇒ 门槛 %.3f，没有离群段"
            % (len(pool), m, mad, thr))
    return keep, out, note


def step_asr(job, slice_dir, out_dir, lang="zh"):
    job["stage"] = "语音识别"
    os.makedirs(out_dir, exist_ok=True)
    # ASR 是"整批丢进去、跑完才出结果"，子进程中途一句进度都不说。
    # 这里按段数先估一个用时，让进度条动起来、hint 里写清"共几段"，
    # 并在 detail 里如实标"估算" —— 不编"第 3/12 段"这种拿不出来的数。
    try:
        n = len([f for f in os.listdir(slice_dir)
                 if f.lower().endswith((".wav", ".flac", ".mp3"))])
    except Exception:
        n = 0
    hb = None
    if n:
        est = int(min(3600, max(30, n * 2.5)))
        hb = (est, "识别原词 · 共 %d 段（估算）" % n)
        step_hint(job, "正在识别每句说的是什么字，共 %d 段" % n, eta=est)
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" %s'
                  % (PY, os.path.join(STAGES, "asr_cli.py"), GSV_ROOT, slice_dir, out_dir, lang),
                  timeout=3600, heartbeat=hb)
    list_path = os.path.join(out_dir, "%s.list" % os.path.basename(slice_dir))
    if not os.path.exists(list_path):
        raise Fail("语音识别没产出标注文件。")
    texts = {}
    for l in open(list_path, encoding="utf-8"):
        p = l.rstrip("\n").split("|")
        if len(p) >= 4:
            texts[os.path.basename(p[0].replace("\\", "/"))] = p[3].strip()
    return texts


def _disk_free(path):
    """返回该路径所在磁盘的剩余字节数；拿不到就返回 None（不当成 0）。"""
    try:
        import shutil
        p = path if os.path.isdir(path) else os.path.dirname(os.path.abspath(path))
        while p and not os.path.isdir(p):
            p = os.path.dirname(p)
        return shutil.disk_usage(p or ".").free
    except Exception:
        return None


def step_check_disk(job, need_gb=6.0):
    """训练前检查磁盘空间。

    以前是**跑到第 4 个epoch 存 ckpt 时才炸** No space left on device：
    GPU 已经空转了几十分钟，用户看到的是一堆 traceback，完全不知道是磁盘满了。
    这里提前量一次，不够就当场说清楚缺多少、缺在哪个盘。

    need_gb 的来由：一次 v5turbo 训练保存的 ckpt 单个约 890MB，
    lightning 默认留 topk=1 + last 一份，加上 hubert/wav32k/semantic 中间产物，
    按 6GB 留余量是实测过的保守值。
    """
    free = _disk_free(GSV_ROOT)
    if free is None:
        return
    need = need_gb * 1024 ** 3
    if free >= need:
        log(job, "  磁盘可用 %.1f GB，够用" % (free / 1024.0 ** 3))
        return
    # 光说"请清理磁盘"等于没说：用户不知道该删什么、也不敢乱删。
    # 所以这里直接告诉他顶栏有「存储」面板，并且报出那里现在能腾出多少 ——
    # 这两个数字并排，他才知道"清一下就够了"还是要另想办法。
    try:
        reclaim = scan_storage().get("reclaimable") or 0
    except Exception:
        reclaim = 0
    tip = ("\n顶栏「🧹 存储」里现在能腾出 %.1f GB%s。"
           % (reclaim / 1024.0 ** 3,
              "，清掉就够这次训练了" if reclaim >= need - free else
              "，还不够，需要再腾 %.1f GB" % ((need - free - reclaim) / 1024.0 ** 3))
           if reclaim else "")
    raise Fail(
        "磁盘空间不足：训练产物目录所在磁盘只剩 %.1f GB，至少需要 %.0f GB。\n"
        "训练会在保存模型权重时崩掉（No space left on device），"
        "而且那时 GPU 已经空转了几十分钟。\n"
        "请先清理磁盘后重试。%s" % (free / 1024.0 ** 3, need_gb, tip))


def step_prep(job, exp_name, list_path, slice_dir):
    job["stage"] = "格式化数据集"
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "prep_all.py"), GSV_ROOT, exp_name, list_path, slice_dir),
                  timeout=3600)
    if rc != 0:
        raise Fail("数据集格式化失败（退出码 %d）。" % rc)


def step_train(job, exp_name, epochs, batch, version="v5turbo",
               pct_from=0, pct_to=88):
    job["stage"] = "训练音色"
    job["epochs_total"] = epochs
    job["epochs_done"] = None
    step_check_disk(job)
    # batch 先按大的试，显存不够再往下退 —— 8GB 卡上 batch 8 通常比 4 快一截
    for b in ([batch] if batch else [8, 4, 2]):
        log(job, "  尝试 batch=%d" % b)
        _t_start = time.time()

        def _track(line, _b=b):
            """从 lightning 的进度行里读出"第几轮 / 共几轮"。

            实测那行的样子是：
                Epoch 14/14 ---------------- 15/15 0:00:01 • 10.66it/s v_num: 1.000
            它用 \r 刷新、同一行反复覆盖，所以按 \n 切出来的"日志行"里
            **只有最后一条留存**（真实日志里 grep "Epoch" 只有 1 处就是证明）。
            ⇒ 必须边跑边解析，等跑完再回头看日志是拿不到中间进度的。

            区间用 pct_from→pct_to 而不是写死 0→88：一次导入可能要连着训
            多个角色，每个角色都得在自己那一段里推进度，否则第二个角色会
            直接继承前一个的 88%，进度条在多角色场景下彻底失真。
            """
            m = re.search(r"Epoch\s+(\d+)\s*/\s*(\d+)", line)
            if not m:
                return
            done, total = int(m.group(1)) + 1, int(m.group(2))
            # 不封顶的话进度条会在最后一个 epoch 冲到区间顶端然后卡住不动 ——
            # 那比不显示更像坏了。剩下的留给体检环节。
            span = max(pct_to - pct_from, 1)
            job["epochs_done"] = done
            job["pct"] = max(job.get("pct") or 0,
                             int(pct_from + span * done / max(total, 1)))
            job["detail"] = "第 %d / %d 轮" % (done, total)
            job["stage"] = "训练音色 · 第 %d/%d 轮" % (done, total)
            # ★「已训到 N 轮 / 目标 M 轮 · 还要多久」—— 训练是几十分钟到几小时的活，
            #   只给一条百分比的话用户既不知道跑到第几轮，也不知道还要等多久。
            #   这里用"已跑时间 ÷ 已跑轮数"外推剩余（轮耗时在训练中基本恒定）。
            try:
                _el = time.time() - _t_start
                _left = int(_el / max(done, 1) * max(total - done, 0))
                job["eta"] = _left
                with JOBS_LOCK:
                    _st = job.get("steps") or []
                    _i = job.get("_step_idx", -1)
                    if 0 <= _i < len(_st):
                        _st[_i]["hint"] = "已训到 %d / %d 轮" % (done, total)
                        _st[_i]["eta"] = _left
            except Exception:
                pass
            save_job(job)

        rc, out = run(job, '"%s" -s "%s" "%s" %s %s %d %d 2 5'
                      % (PY, os.path.join(STAGES, "train_cli.py"), GSV_ROOT, exp_name, version, b, epochs),
                      timeout=7200, on_line=_track)
        if rc == 0:
            break
        o = out.lower()
        if "out of memory" in o or "cuda oom" in o or "allocation" in o:
            log(job, "  显存不够，降到更小的 batch 重试")
            continue
        # 磁盘满的报错长得像一堆系统调用栈，用户看不出真正原因 —— 单独识别出来。
        if "no space left on device" in o or "errno 28" in o:
            raise Fail(
                "磁盘空间不足：训练跑到一半存不下模型权重了。\n"
                "产物目录所在磁盘只剩 %.1f GB。\n"
                "请先清理磁盘再重试。"
                % ((_disk_free(GSV_ROOT) or 0) / 1024.0 ** 3))
        raise Fail("训练失败（退出码 %d）。\n%s" % (rc, out[-1000:]))
    else:
        raise Fail("所有 batch 都因显存不足失败，请关掉其它占显存的程序后重试。")

    cks = sorted([f for f in os.listdir(GPT_WEIGHT_DIR)
                  if f.startswith(exp_name + "-e") and f.endswith(".ckpt")]) \
        if os.path.isdir(GPT_WEIGHT_DIR) else []

    def ep(f):
        try:
            return int(f.split("-e")[-1].split(".")[0])
        except Exception:
            return -1

    if not cks:
        raise Fail("训练说跑完了，但没找到权重文件。")
    best = max(cks, key=ep)
    log(job, "  权重: %s" % ", ".join(cks))
    return os.path.join("GPT_weights_v5turbo", best), ep(best)


REF_VOICE_TOPN = 12   # 参考音频声纹复核最多比这么多段（再多只是拖慢每次"说话"）
# ★ 声纹复核的总开关。留成开关而不是写死，是因为它**实测会让合成变不稳定**
#   （2026-10-16）：大帝身上，开着它"换语气带来的音高差"从 65 Hz 掉到 30.9 Hz，
#   而"同一句重复合成的抖动"从 0.2 Hz 涨到 21.3 Hz ⇒ 语气控制的信噪比 318x → 1.4x，
#   `verify_tone` 的负对照当场判红。默认值见下，改这一个常量就能切换做 A/B。
REF_USE_VOICE = False

# ---- 参考音频的合法时长：3~10 秒（**GPT-SoVITS 的硬性要求，不是我们的偏好**）----
# 出处：GPT_SoVITS/TTS_infer_pack/TTS.py:917 —— 16k 采样点数必须落在
#   48000 ~ 160000 之间（= 3.0~10.0 秒），否则直接
#   `raise OSError("参考音频在3~10秒范围外，请更换！")`，**没有自动裁剪**。
#
# ★ 这一条被踩过两次（2026-10-18，`_probe_gsv_epochs_ab` 跑长台词时暴露）：
#   · pick_reference 原写法：`dur < 2.0` 才排除，3~10 秒只是"打分降权"；
#   · _alt_refs 原写法：`1.0 <= dur <= 14.0` 才算候选。
#   两个区间都比下游**宽**，于是 1.6 秒的段被当成合法参考选进去。后果不是"效果差"，
#   是**整条链路抛异常**：
#     长台词每段固定合成两遍（takes=2），第 1 遍可疑 → 第 2 遍换参考 → 换到
#     1.60 秒那段 → OSError → 而"换参考重试"本来是长句丢字**唯一的兜底手段**
#     （见 _alt_refs 的注释：八个旋钮全试过，只有换参考能改变结果）。
#   ⇒ 兜底等于不存在，而且异常还会把整个任务带崩（`dict(None,...)` 那条）。
#
# 教训：护栏的区间必须**等于或严于**下游，宽一点都不行；宽出来的部分不是"宽容"，
#       是把下游的硬报错请到上游来。
REF_MIN_SEC = 3.0
REF_MAX_SEC = 10.0


def ref_dur_ok(dur):
    """这段音频能不能直接当 GPT-SoVITS 的参考（3~10 秒）。拿不到时长算不合格。"""
    try:
        dv = float(dur)
    except (TypeError, ValueError):
        return False
    return REF_MIN_SEC <= dv <= REF_MAX_SEC


def pick_reference(role, slices_dir):
    """
    挑参考音频：时长 3~10 秒、响度适中，挑其中**最靠近 5 秒且响度在舒适区**的那段。

    ★★ 关于「用声纹挑最典型的段」：这个想法**实测被否决了**（2026-10-16），
       代码留着但默认关（REF_USE_VOICE=False）。过程记在这里免得有人再来一遍：

       想法看着很对 —— 时长/响度合格的段里仍可能混着带笑/带口音/带噪音的片段，
       拿它当参考整句都会偏；声纹距角色中心越近＝越典型＝越能代表这个人。

       实测（`verify_tone` 的负对照，大帝）：
                        换语气带来的音高差   同一句重复合成的抖动   信噪比
          声纹复核 开        30.9 Hz              21.3 Hz           1.4x
          声纹复核 关        55.1 Hz               1.7 Hz          32.5x
       ⇒ 挑"最典型"的那段反而让合成**明显变不稳定**：抖动从 1.7 Hz 涨到 21.3 Hz
         （12 倍），换语气能拉开的差距反而从 55.1 Hz 掉到 30.9 Hz。
         推测原因：最"平均"的那段往往也是最平淡、起伏最小的段，
         拿它当参考，模型对语气的响应就钝了。
       ⇒ **文献/直觉说了不算，实测说了算。** 默认关；想再试就把 REF_USE_VOICE 改 True。
    """
    cands = []
    for s in role.get("segments", []):
        if not (s.get("text") or "").strip():
            continue
        p = os.path.join(slices_dir, s["file"])
        if not os.path.exists(p):
            continue
        dur = s.get("dur", 0)
        # ★ 先按 3~10 秒硬过滤。这不是"偏好"，是下游的硬要求（见 REF_MIN_SEC 注释）：
        #   以前这里是 `dur < 2.0` 才排除、3~10 秒只做降权，于是 2~3 秒的段仍可能
        #   被选中当主参考 ⇒ 每一句合成都在 GPT-SoVITS 内部抛 OSError。
        if not ref_dur_ok(dur):
            continue
        r = wav_rms(p)
        if r <= 0.005:
            continue
        # 合法区间内，越接近 5 秒越稳（太短音色信息不够，太长容易带换气/静音）
        score = abs(dur - 5.0)
        if not (0.02 <= r <= 0.35):
            score += 2.0
        cands.append((score, p, s["file"], s["text"]))
    if not cands:
        # 退化前再补一次：segments 里的 dur 是切片时记的，可能和磁盘上的真实文件
        # 不一致（后续被替换/重剪过）。实量一遍再判，别因为一份旧元数据把角色判死。
        for s in role.get("segments", []):
            if not (s.get("text") or "").strip():
                continue
            p = os.path.join(slices_dir, s["file"])
            if not os.path.exists(p):
                continue
            if not ref_dur_ok(wav_seconds(p)):
                continue
            if wav_rms(p) > 0.01:
                cands.append((9.0, p, s["file"], s["text"]))
    if not cands:
        # 一段合法的都没有 = 硬约束无解。不许"凑合用"（凑合的结果是下游报一个
        # 让人一头雾水的 OSError），把真实时长分布打出来，告诉人该怎么修。
        durs = []
        for s in role.get("segments") or []:
            if not (s.get("text") or "").strip():
                continue
            dd = float(s.get("dur") or 0) or wav_seconds(os.path.join(slices_dir, s["file"]))
            if dd:
                durs.append(dd)
        durs.sort()
        raise Fail(
            "这个角色没有一段切片落在 %.0f~%.0f 秒之间，而这是 GPT-SoVITS 对参考音频的"
            "硬性要求（超了它直接报「参考音频在3~10秒范围外」，不会自动裁剪）。\n"
            "现有 %d 段切片：最短 %.2f 秒 / 中位 %.2f 秒 / 最长 %.2f 秒。\n"
            "要么重新切片让每段落在 %.0f~%.0f 秒，要么补更长的素材再切。"
            % (REF_MIN_SEC, REF_MAX_SEC, len(durs),
               durs[0] if durs else 0,
               durs[len(durs) // 2] if durs else 0,
               durs[-1] if durs else 0, REF_MIN_SEC, REF_MAX_SEC))
    cands.sort(key=lambda x: x[0])
    _, path, fname, text = cands[0]

    # ★ 声纹复核：在上面"时长/响度都合格"的候选里，再挑**最像这个人平常状态**的那段。
    #   这一段以前只写在函数头的注释里、代码并没做（实测对过：只有时长+响度打分）。
    #   为什么要这一步：参考音频直接决定合成的音色和韵律，而时长/响度合格的段里
    #   仍然可能混着"带笑 / 带口音 / 带噪音 / 情绪很偏"的那几段 —— 拿它当参考，
    #   整句都会跟着偏。声纹距角色中心越近 = 越典型 = 越能代表这个人。
    #   ★ 只对前 REF_VOICE_TOPN 个候选算：声纹要起 GSV 的子进程，全量算会明显拖慢
    #     每次"说话"，而排后面的候选本来就不该被选中。
    center = (role.get("voice") or {}).get("center")
    if REF_USE_VOICE and center and len(cands) > 1:
        try:
            top = cands[:REF_VOICE_TOPN]
            recs = _feat_subprocess([c[1] for c in top], "ref")
            dist = {}
            for r in recs:
                v = r.get(VOICE_KIND)
                if not v:
                    continue
                d = _emb_dist(v, center)
                if d is not None:
                    dist[os.path.basename(r.get("file") or "")] = d
            if dist:
                # 距离优先；距离量不出来（dist 里没有）的保持原来的座次
                top.sort(key=lambda c: (dist.get(c[2], 9.9), c[0]))
                path, fname, text = top[0][1], top[0][2], top[0][3]
        except Exception:
            # 声纹算不出来就退回"只按时长响度"的结果，绝不能让说话跑不起来
            pass
    return path, text


# ---------------------------------------------------------------- 完整流程


def _set_pending(rid):
    """把这个角色标成「待归类」（来源不明的一堆片段）。"""
    db = load_roles()
    for r in db["roles"]:
        if r["id"] == rid:
            r["pending"] = True
    save_roles(db)


def _attach_segments(rid, seg_records, sources, replace=False):
    """把一批片段挂到角色上，返回**真正新增了几段**。

    replace=False（追加模式）：老片段全部保留，同名跳过（不重复计数）；
    replace=True （首次建档）：整份替换 —— 重拆同一个视频时不能越拆越多。

    ★ 已经有人手工校对过的文本一律不覆盖：追加素材时把整 rebuilding 一遍的话，
      用户改过的错别字会被 ASR 的识别结果冲掉，而他自己也未必记得改过哪些。
    """
    names = [os.path.splitext(s)[0] for s in (sources or [])]
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import singing

    def _apply(r):
        old = {s["file"]: s for s in (r.get("segments") or [])}
        merged = [] if replace else list(r.get("segments") or [])
        added = 0
        for s in seg_records:
            key = s["file"]
            if key in old:
                # 同名 = 同一段素材又来了一遍。追加模式下跳过（不重复），
                # 但**保留老的那一条**（里面可能有人工改过的文本）。
                if replace:
                    o = old[key] or {}
                    merged.append(dict(s, text=o.get("text") or s.get("text", "")))
                    added += 1
                continue
            merged.append(s)
            added += 1
        r["segments"] = merged
        if not r.get("kind_locked"):
            kind, conf, why = singing.guess(merged)
            r["kind"] = kind
            r["kind_guess"] = {"kind": kind, "conf": conf, "why": why}
        r["source"] = "、".join(sources or [])
        # intro 是"这张卡是谁"的唯一线索，素材变多了就要跟着更新
        secs = sum(float(s.get("dur") or 0) for s in merged)
        if not (r.get("intro") or "").strip() or replace:
            src_txt = "《%s》" % "》《".join(names[:3]) + ("…" if len(names) > 3 else "")
            r["intro"] = "来自%s的一个声音 · %d 段素材 · %s" % (
                src_txt, len(merged), _humantime(secs))
        else:
            # 已有简介的（可能人工改过）：只把素材数字刷新掉，简介保留
            r["intro"] = re.sub(r"·\s*\d+\s*段素材\s*·[^·]*$",
                                "· %d 段素材 · %s" % (len(merged), _humantime(secs)),
                                r["intro"]) or r["intro"]
        r["split_info"] = {
            "segments": len(merged),
            "seconds": round(secs, 1),
        }
        r["_added"] = added

    role = mutate_role(rid, _apply)
    return (role or {}).get("_added", 0)


def job_ingest(job, video=None, n_speakers=None, uvr=True, lang="zh", epochs=None,
               do_train=True, quality="standard", videos=None, mode="auto",
               append_to=None):
    """视频 -> 角色（可一个视频拆多个，也可多个视频合成一个）。

    videos: 视频文件列表。选了多个文件时，它们通常**都是同一个人在说话**
             （比如同一个 UP 主的几期视频），用户要的自然是"合成一个模型"。
    mode:
            merge     —— 全部合成**一个**角色（不管里面有几个人在说话）
            auto      —— 把多个文件的片段倒进同一个池子里做声纹聚类，
                         同一个人的素材**跨文件自动合到一起**（这才是"多个视频
                         一个模型"的正解），不同的人仍然分开建档。
            match     —— **自动认人**：先像 auto 一样按人分堆，再把每一堆拿去和
                         库里已有角色的**声纹中心**比。认得出的直接并进那个角色
                         （追加，不新建），认不出的照 auto 新建。这样"同一个人的
                         新素材"不会散成好几个角色，别人的素材也不会被混进来。
            per_video —— 每个视频各自建一个角色（素材差别很大时用）
    append_to: 指定一个已有角色的 id —— 新素材**追加**到它身上，而不是新建角色。
            这是"后续还能加素材"的入口：追加完可以顺便重训。
    """
    vids = [v for v in list(videos or []) if v]
    if video:
        vids = [video] + vids          # 兼容以前的单文件调用方式
    if not vids:
        raise Fail("没有要处理的视频。")
    for v in vids:
        if not os.path.isfile(v):
            raise Fail("文件不存在：%s" % v)
    many = len(vids) > 1
    if append_to and not get_role(append_to):
        raise Fail("要追加到的角色不存在：%s" % append_to)
    # 追加就意味着"这些都是同一个人"，再去按人分组就自相矛盾了
    if append_to:
        mode = "merge"

    work = os.path.join(job["dir"], "work")
    os.makedirs(work, exist_ok=True)
    # ★ 多个视频的片段最终要倒进**同一个池子**里一起聚类。
    #   如果各切各的、各自聚类再合并，那同一个人在两个视频里就是两个角色 ——
    #   用户拿到的还是两个模型，等于没合并。
    all_slices = os.path.join(work, "slices_all")
    os.makedirs(all_slices, exist_ok=True)

    sources = []
    total_segs = 0
    for i, v in enumerate(vids):
        tag = " · 第 %d/%d 个《%s》" % (i + 1, len(vids),
                                        os.path.basename(v)) if many else ""
        sources.append(os.path.basename(v))
        begin_step(job, "抽取音轨" + tag, "从视频里取出声音",
                   hint="正在把画面丢掉、只留下声音")
        audio = step_audio(job, v, os.path.join(work, "v%d_audio.wav" % i))
        end_step(job, True, "已取出音频")

        begin_step(job, "分离人声与伴奏" + tag, "去掉背景音乐和杂音",
                   hint="正在挑分离模型（先试听 30 秒再决定用哪个）" if uvr
                        else "这一步跳过了（你选了不分离）")
        vocal = step_uvr(job, audio, work, enabled=uvr)
        end_step(job, True, "人声已分离")

        begin_step(job, "切分片段" + tag, "把连续人声切成一句一句",
                   hint="正在按停顿把人声切段，太短太吵的会丢掉")
        slice_dir, good = step_slice(job, vocal, os.path.join(work, "v%d_slices" % i))
        end_step(job, True, "切出 %d 段，合格 %d 段"
                 % (len(os.listdir(slice_dir)), len(good)))

        # 只把合格的片段留下来参与后续，噪音片段直接丢
        keep = set(good)
        for f in os.listdir(slice_dir):
            if f not in keep:
                try:
                    os.remove(os.path.join(slice_dir, f))
                except Exception:
                    pass
        # 拷进总池：名字前面加 v<i>_ 前缀，这样多个视频里同名的片段
        # （切片脚本按时间命名，不同视频必然重名）不会互相覆盖。
        for f in sorted(keep):
            shutil.copy(os.path.join(slice_dir, f),
                        os.path.join(all_slices, "v%d_%s" % (i, f)))
        total_segs += len(keep)
        log(job, "  《%s》贡献 %d 段" % (os.path.basename(v), len(keep)))

    slice_dir = all_slices

    job["stage"] = "汇总多段素材"
    begin_step(job, "提取声纹特征", "给每段算出「这是谁的声音」",
               hint="正在给每段算声纹（这段声音的「指纹」）")
    feats = step_feat(job, slice_dir, os.path.join(work, "feat.json"))
    end_step(job, True, "算出 %d 段特征" % len(feats))

    if mode == "merge":
        begin_step(job, "合并素材", "按你的选择，全部合成一个人")
        groups = {0: list(feats)}
        res = {"k": 1}
        end_step(job, True, "%d 段素材全部归到同一个角色" % len(feats))
    elif mode == "per_video":
        begin_step(job, "按视频分别建档", "每个视频一个角色")
        groups = {}
        for f in feats:
            v_i = str(f.get("file", "")).split("_", 1)[0]
            groups.setdefault(v_i, []).append(f)
        res = {"k": len(groups)}
        end_step(job, True, "分成 %d 个角色：%s"
                 % (len(groups), {k: len(v) for k, v in sorted(groups.items())}))
    else:
        begin_step(job, "分离不同说话人",
                   ("你指定了 %d 个人" % n_speakers) if n_speakers
                   else "自动判断有几个人在说话",
                   hint="正在把声纹相近的段聚成同一个人")
        groups, res = step_split_speakers(job, feats, n_speakers)
        end_step(job, True, "分成 %d 组：%s"
                 % (res["k"], {k: len(v) for k, v in sorted(groups.items())}))

    # ---- 认人（mode=match）：这几堆素材分别是库里哪一个角色？----
    # 认得出的直接并进去，认不出的照原样新建。只有 match 模式做这件事：
    # auto 是"这次导入里怎么分人"，match 是"这些人在我库里是谁"，两个问题不能混。
    assign = {}
    if mode == "match" and not append_to:
        begin_step(job, "认人", "看看这几堆素材分别是谁的声音",
                   hint="正在跟库里已有角色的声纹逐个比对")
        made_n, skip_n = ensure_role_voices(job)
        if made_n or skip_n:
            log(job, "  声纹中心：补算 %d 个，跳过 %d 个（切片不在或段太少）"
                % (made_n, skip_n))
        assign, notes = match_clusters(job, groups, feats)
        for t in notes:
            log(job, "  " + t)
        end_step(job, True, ("认出了 %d 堆（并到已有角色），其余新建"
                             % len(assign)) if assign
                 else "一堆都没认出来，全部新建角色")

        # ---- 段级纯净度复核：一堆认成某人之后，还要逐段确认 ----
        # 簇级只取中位数，堆里混着别人的**整段**时中位数照样过阈值
        # （实测依据见 SEG_MATCH_DIST 的注释）。这一层按段判，并把结果
        # **反写回 groups**：改判给别人的段单独成堆、剔掉的段丢进待归类，
        # 后面「建立角色」那段循环完全不用改。
        begin_step(job, "段级复核", "逐段确认归属，把不属于这个声音的挑出去")
        owner, rejected, rnotes = refine_segments(job, groups, feats, assign)
        for t in rnotes:
            log(job, "  " + t)
        if owner or rejected:
            moved_out, dropped_out = {}, []
            kept_groups = {}
            for lab in list(groups.keys()):
                target = assign.get(lab)
                keep = []
                for f in groups[lab]:
                    nm = f.get("file")
                    if nm in rejected:
                        dropped_out.append(f)                    # 明确判不像 ⇒ 剔出
                        continue
                    o = owner.get(nm)
                    if o is None or o == target:
                        keep.append(f)                           # 没表态 或 本来就是它
                    else:
                        moved_out.setdefault(o, []).append(f)    # 改判给更近的那个角色
                kept_groups[lab] = keep
            groups = kept_groups
            for rid, segs in moved_out.items():
                # 用一个字符串 key 单独成堆 + 登记进 assign ⇒ 后面那段循环
                # 会走"并到已有角色"那条路，不用为它另写一套分支。
                key = "seg:" + str(rid)
                groups[key] = segs
                assign[key] = rid
                log(job, "  %d 段其实更像「%s」：并到那个角色，不留在原堆"
                    % (len(segs), (get_role(rid) or {}).get("name") or rid))
            if dropped_out:
                # ★ 剔出来的段**不丢弃**：丢进「待归类的片段」，用户还能救回来。
                groups.setdefault(-1, [])
                groups[-1].extend(dropped_out)
                log(job, "  %d 段谁都不像：放进「待归类的片段」（没丢，可随时归位）"
                    % len(dropped_out))
        end_step(job, True, "段级复核完成（总 %d 堆）" % len(groups))

    # ---- 簇内离群剔除：没认出来的堆（要新建角色）里可能还混着杂音或别人的段 ----
    # 那一堆没有角色中心可对照，只能拿它自己的中心当尺子（见 purge_outliers）。
    # 已认人的堆**不在这儿动** —— 它们有角色中心，段级复核那一层判得更准。
    if not append_to:
        kept2, dropped2 = {}, []
        for lab in list(groups.keys()):
            if assign.get(lab):
                kept2[lab] = groups[lab]
                continue
            try:
                if int(lab) < 0:              # 杂音堆本来就要进待归类，不重复剔
                    kept2[lab] = groups[lab]
                    continue
            except (TypeError, ValueError):
                pass
            keep, out, note = purge_outliers(job, groups[lab], feats)
            kept2[lab] = keep
            if out:
                dropped2.extend(out)
                log(job, "  " + note)
        groups = kept2
        if dropped2:
            groups.setdefault(-1, [])
            groups[-1].extend(dropped2)
            log(job, "  簇内离群：%d 段不像这一堆的主流声音 ⇒ 放进「待归类的片段」（没丢）"
                % len(dropped2))

    begin_step(job, "语音识别", "听出每句说了什么字",
               hint="正在识别每句说的是什么字（训练文本用，不影响音色）")
    texts = step_asr(job, slice_dir, os.path.join(work, "04_asr"), lang)
    end_step(job, True, "识别了 %d 句" % len(texts))

    begin_step(job, "建立角色", "按说话人分别建档",
               hint="正在把每堆素材写成一个角色（把片段拷进它的目录）")
    # singing.py 只用标准库（statistics），不需要 numpy，
    # 所以可以安全地在本进程 import —— 与 diarize 不同，别一律改子进程。
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import singing

    db = load_roles()
    made = []
    names = [os.path.splitext(s)[0] for s in sources]
    for gi, lab in enumerate(sorted(groups, key=lambda x: str(x))):
        segs = groups[lab]
        # ★ 段级复核可能把这一堆的段全都挪走（改判给别人 / 剔进待归类）。
        #   这时候不许再建一个空角色出来 —— 空卡片在界面上是个"幽灵"。
        if not segs:
            continue
        # ★ lab 有两种形状：auto 模式是聚类给的**整数**标签（-1 = 没挂上的
        #   杂音），per_video 模式是我自己起的 "v0"/"v1" 这种**字符串**键。
        #   直接写 `lab < 0` 在 per_video 上会 TypeError（str 和 int 比不了）——
        #   两种形状混在同一个循环里，就得先问清楚它是哪一种。
        try:
            pending = int(lab) < 0
        except (TypeError, ValueError):
            pending = False
        if pending and not append_to:
            # 没挂上的片段单独放一个「待归类」角色，让用户自己决定
            rid = None
            for r in db["roles"]:
                if r.get("pending") and r.get("source") == sources[0]:
                    rid = r["id"]
            if rid is None:
                role = create_role(name="待归类的片段", source=sources[0])
                _set_pending(role["id"])
                db = load_roles()
                rid = role["id"]
            else:
                role = get_role(rid)
        elif append_to:
            rid = append_to
            role = get_role(rid)
        elif assign.get(lab):
            # ★ 认人认出来了：这些素材是库里已有的那个人 ⇒ 并进去（追加语义），
            #   不新建角色。这正是需求里的"把属于这个角色的素材加进去"。
            rid = assign[lab]
            role = get_role(rid)
            log(job, "  第 %s 堆并到已有角色「%s」（%s）"
                % (lab, role.get("name"), rid))
        else:
            nm = "角色 %s" % (chr(ord("A") + gi))
            role = create_role(name=nm, source="、".join(sources))
            rid = role["id"]

        # 这一轮对这个角色到底是"追加"还是"首次建档"：
        # 认人认出来的那几堆也是追加 —— 用 replace=True 会把老素材整份换掉，
        # 那就等于把用户既有的角色清空了（踩不起）。
        is_append = bool(append_to) or bool(assign.get(lab))

        d = role_dir(rid)
        sd = os.path.join(d, "slices")
        os.makedirs(sd, exist_ok=True)
        seg_records = []
        for f in segs:
            src = os.path.join(slice_dir, f["file"])
            # ★ 目标文件已存在且大小一致时**不要覆盖**：同一个视频被重复追加时
            #   重算一遍再拷回去毫无意义，反而可能把用户已经校对过的内容弄脏。
            dst = os.path.join(sd, f["file"])
            if not os.path.exists(dst):
                shutil.copy(src, dst)
            seg_records.append({
                "file": f["file"],
                "dur": f.get("dur", 0),
                "voiced": f.get("voiced", 0),
                "text": texts.get(f["file"], ""),
            })

        added = _attach_segments(rid, seg_records, sources, replace=not is_append)
        refresh_stats(rid)
        role = get_role(rid)
        log(job, "  角色 %s：累计 %d 段（本次新增 %d 段），%.1f 秒"
            % (rid, len(role.get("segments") or []), added,
               (role.get("stats") or {}).get("seconds", 0.0)))
        # 素材变了，声纹中心必须重算 —— 不重算的话下一次认人用的还是老中心，
        # 而这个角色已经多了一批素材（严重的话会把别人认成它）。
        try:
            build_role_voice(rid, job, force=True)
        except Exception as e:
            # 认人是个增强功能，算不出中心不该把整条导入链带死
            log(job, "  [warn] 声纹中心没算出来（不影响建档）：%s" % e)
        made.append(rid)

    end_step(job, True, "建好 %d 个角色：%s"
             % (len(made), "、".join((get_role(r) or {}).get("name", r)
                                     for r in made)))

    failed = []
    if do_train:
        todo = [r for r in made if not get_role(r).get("pending")]
        total = len(todo)
        for i, rid in enumerate(made):
            if get_role(rid).get("pending"):
                continue
            nm = get_role(rid)["name"]
            begin_step(job, "训练音色：%s" % nm, "第 %d/%d 个角色" % (i + 1, max(total, 1)),
                       hint="正在训练音色模型（显卡会被占满）")
            # 每个角色在 60%→97% 里切自己的一段。写死单点进度的话，
            # 第二个角色开始训练时进度条会从上一个角色的高位跳回去 —— 用户看着像倒退。
            lo = 60 + int(37 * i / max(total, 1))
            hi = 60 + int(37 * (i + 1) / max(total, 1))
            job["stage"] = "训练 %s (%d/%d)" % (nm, i + 1, total)
            job["pct"] = max(job.get("pct") or 0, lo)
            save_job(job)
            try:
                train_role(job, rid, epochs, batch=None, quality=quality,
                           pct_from=lo, pct_to=hi)
                end_step(job, True, "%s 已训好" % nm)
            except Exception as e:
                # 以前只 log 一句 [warn] 就继续 ⇒ 任务整体"成功"，
                # 用户看到的是"跑完了"但某个角色其实没训出来，**失败被吞掉了**。
                # 现在：既标红这一步，也记进 failed，最后一并如实上报。
                fail_step(job, e)
                log(job, "  [失败] 角色 %s（%s）训练失败：%s" % (nm, rid, e))
                failed.append({"role": rid, "name": nm, "err": str(e)})
    job["result_roles"] = made
    if failed:
        # 不能假装整体成功。任务标成"部分失败"，把每个失败的角色说清楚。
        job["failed_roles"] = failed
        end_step(job, False, "%d/%d 个角色训练失败：%s"
                 % (len(failed), max(total, 1),
                    "；".join("%s（%s）" % (f["name"], f["err"][:80]) for f in failed)))
        log(job, "完成，但有 %d 个角色训练失败（详见上面标红的那一步）" % len(failed))
    else:
        log(job, "完成，共建/更新 %d 个角色：%s" % (len(made), ", ".join(made)))


def _humantime(sec):
    m, s = divmod(int(round(sec)), 60)
    if m:
        return "%d 分 %d 秒" % (m, s)
    return "%d 秒" % s


def _fetch_index(job, repo, pick, dst):
    """把跟这个 .pth **同目录**的 .index 一起下下来（有就下，没有就跳过）。

    ★ RVC 的 .index 是音色检索库（`index_rate` 全靠它），从来不算"权重的一部分"，
      所以分享者习惯给它起个完全不相干的名字：
          AppleBloomSingingv2/Apple_Bloom.pth
          AppleBloomSingingv2/added_IVF207_Flat_nprobe_1_Apple Bloom_v2.index
      以前这一步整个没做，装完的角色 `index: false` —— 听感上是"不太像那个人"，
      而日志只写一句"没有索引（也能唱，检索弱一点）"，看的人根本分不清是
      **这个模型本来就没发索引**，还是**我们漏下了**。
      实测 therealvul/RVCv2：54 个 .pth 配着 54 个 .index，一个都没被下载。

    找不到索引不算失败：有些作者确实只发 .pth，那种模型照样能唱。
    """
    import modelhub

    d0 = os.path.dirname(pick)
    stem = os.path.splitext(os.path.basename(pick))[0].lower()
    try:
        files = modelhub.file_list(repo)      # 完整清单（detail 的 files 截断到 40）
    except Exception as e:
        log(job, "  [warn] 拿不到仓库的完整文件清单（%s），索引这次跳过" % str(e)[:80])
        return None
    same = [f for f in files
            if f.lower().endswith(".index") and os.path.dirname(f) == d0]
    if not same:
        log(job, "  这个目录里没有 .index —— 作者没发检索库，这个模型照样能唱（音色细节少一点）")
        return None
    want = None
    for f in same:                            # 优先同名，其次取第一个
        if os.path.splitext(os.path.basename(f))[0].lower() == stem:
            want = f
            break
    want = want or same[0]
    dst_idx = os.path.join(os.path.dirname(dst), os.path.basename(want))
    if os.path.isfile(dst_idx) and os.path.getsize(dst_idx) > 1024:
        return dst_idx
    url = modelhub.file_url(repo, want)
    rc, out = run(job, '"%s" -u "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "dl_cli.py"), url, dst_idx), timeout=3600)
    if rc != 0 or not os.path.isfile(dst_idx):
        log(job, "  [warn] 索引没下成（%s）—— 这个角色能唱，但音色检索会弱一点"
            % _tail(out, 2))
        return None
    log(job, "  索引已下载：%s（%.1f MB）"
        % (os.path.basename(want), os.path.getsize(dst_idx) / 1048576.0))
    return dst_idx


def _install_one_pack(job, repo, pick, name=None, budget=8):
    """下载**一个**包（zip 或单个 .pth），把里面的权重逐个变成角色。

    返回建好的角色列表。包里某个权重加载不了就跳过（文件删干净、角色卡片撤回，
    不留半截），一个都没成才由调用方统一报错 —— 单个包失败不该把整批拖死。
    """
    import zipfile

    import modelhub

    url = modelhub.file_url(repo, pick)
    # ★ 落盘必须**保留子目录**：`Scootaloo/Scootaloo.pth` 与
    #   `ScootalooSingingv2/Scootaloo.pth` 的 basename 一模一样，压平之后
    #   第二个会命中下面那句"已经下过了，直接用" —— 于是装出来两个角色，
    #   其实是**同一个模型**（这个仓库里实测有两对这样的同名文件）。
    #   保留子目录还有个附带好处：同目录的 .index 天然落在权重旁边，
    #   建角色时的"同目录找索引"就能对上。
    rel = [p.strip() for p in pick.split("/") if p.strip()]
    dst = os.path.join(modelhub.download_dir(repo), *rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
        log(job, "  这个文件已经下过了，直接用（%.1f MB）" % (os.path.getsize(dst) / 1048576.0))
    else:
        begin_step(job, "下载模型", "%s　来自 %s" % (os.path.basename(pick), repo))
        rc, out = run(job, '"%s" -u "%s" "%s" "%s"'
                      % (PY, os.path.join(STAGES, "dl_cli.py"), url, dst), timeout=7200)
        if rc != 0 or not os.path.isfile(dst):
            end_step(job, False, "下载失败")
            raise Fail("下载失败（镜像有时候会超时，可以再点一次）。\n%s" % _tail(out, 4))
        end_step(job, True, "%.1f MB" % (os.path.getsize(dst) / 1048576.0))

    # ---- 解开 / 找出里面的权重 ----
    cands = []
    if pick.lower().endswith(".zip"):
        begin_step(job, "解开 %s" % os.path.basename(pick)[:28], "看看里面装了几个角色")
        ex = os.path.join(os.path.dirname(dst),
                          "x_" + re.sub(r"[^0-9A-Za-z_\-]+", "_", os.path.splitext(os.path.basename(pick))[0]))
        shutil.rmtree(ex, ignore_errors=True)
        os.makedirs(ex, exist_ok=True)
        try:
            with zipfile.ZipFile(dst) as z:
                z.extractall(ex)
        except Exception as e:
            end_step(job, False, "解不开")
            raise Fail("解不开这个压缩包：%s" % e)
        for root, _, fs in os.walk(ex):
            for f in fs:
                if f.lower().endswith(".pth"):
                    cands.append(os.path.join(root, f))
        end_step(job, True, "里面 %d 个 .pth" % len(cands))
    elif pick.lower().endswith(".pth"):
        cands = [dst]
        # 权重拿到手就顺手把同目录的检索库也拿下来（理由见 _fetch_index）
        _fetch_index(job, repo, pick, dst)
    else:
        raise Fail("先不处理这个格式：%s（RVC 要 .pth，或装着 .pth 的 .zip）"
                   % os.path.splitext(pick)[1])
    if not cands:
        raise Fail("里面没找到 .pth（这个包里不是 RVC 权重）")
    cands.sort(key=lambda p: -os.path.getsize(p))
    if len(cands) > budget:
        log(job, "  里面有 %d 个权重，先装最大的 %d 个" % (len(cands), budget))
        cands = cands[:budget]

    wdir = _rvc_weights_dir()
    idir = os.path.join(RVC_ROOT, "assets", "indices")
    os.makedirs(wdir, exist_ok=True)
    os.makedirs(idir, exist_ok=True)

    made = []
    for pth in cands:
        raw = modelhub.clean_name(name or os.path.basename(pth))
        # ★ 角色卡上写**中文名**（本地字典，不联网不花钱）；原名塞进简介，
        #   用户随时能对着原名去核对（万一字典里那条翻错了，他一眼能看出来）。
        base = modelhub.to_cn(raw) or raw
        begin_step(job, "装上「%s」" % base, "放进 RVC 并建档")
        # ★ 先建档拿 id：权重/索引的文件名都按这个 id 来（跟自训模型同口径）。
        #   反过来（先落盘再问 id）就会多一次改名，改名一旦漏了索引，
        #   症状是"唱得出来但音色检索没生效"，非常难查。
        role = create_role(name=base, kind="singing", source="modelhub:%s" % repo)
        rid = role["id"]

        # 1) 权重
        wp = os.path.join(wdir, rid + ".pth")
        shutil.copyfile(pth, wp)
        # 2) 索引：同目录下找 .index（优先跟权重同名的那个）
        src_idx = None
        stem = os.path.splitext(os.path.basename(pth))[0].lower()
        for f in os.listdir(os.path.dirname(pth)):
            if f.lower().endswith(".index"):
                if os.path.splitext(f)[0].lower() == stem:
                    src_idx = os.path.join(os.path.dirname(pth), f)
                    break
                src_idx = src_idx or os.path.join(os.path.dirname(pth), f)
        idx_ok = False
        if src_idx:
            shutil.copyfile(src_idx, os.path.join(
                idir, "%s_added_IVF1_Flat_nprobe_1_%s_v1.index" % (rid, rid)))
            idx_ok = True
        # 3) 真的加载一次：文件在 ≠ 能加载
        rc2, out2 = run(job, '"%s" -s "%s" "%s"'
                        % (RVC_PY, os.path.join(STAGES, "rvc_ckpt_check.py"), wp),
                        cwd=RVC_ROOT, timeout=900)
        if rc2 != 0:
            end_step(job, False, "这个权重不能用")
            for p in (wp, os.path.join(idir, "%s_added_IVF1_Flat_nprobe_1_%s_v1.index" % (rid, rid))):
                with contextlib.suppress(Exception):
                    os.remove(p)
            with contextlib.suppress(Exception):
                delete_role(rid)
            log(job, "  %s：%s（跳过）" % (base, _tail(out2, 2)))
            continue
        end_step(job, True, "%s，索引%s" % (base, "已对上" if idx_ok else "没有（也能唱，检索弱一点）"))

        # ★ 默认参数绑定（_p=rid 这种写法）：闭包里的循环变量会在下一轮被改写，
        #   不绑定的话角色卡上会串成最后一个角色的信息。
        def _set(r, _p=rid, _repo=repo, _pick=pick, _base=base, _raw=raw):
            r["rvc"] = {"model": _p + ".pth", "epochs": 0, "at": _now(),
                        "index": idx_ok, "imported": True, "repo": _repo,
                        "file": os.path.basename(_pick)}
            r["intro"] = "来自声音市场的模型（%s）" % _repo
            if _base != _raw:
                # 卡片上是中文名 ⇒ 简介里必须留着原名，不然用户没法核对
                r["intro"] = "原名 %s · 来自声音市场（%s）" % (_raw, _repo)
                r["alias"] = _raw
            r["kind"] = "singing"
            r["kind_locked"] = True

        mutate_role(rid, _set)
        made.append({"id": rid, "name": base, "raw": raw, "index": idx_ok})
        log(job, "  ✔ 角色「%s」已建好，可以直接拿去唱歌" % base)

    return made


def _market_stem(r):
    """市场安装时记下来的**原始压缩包名**（Applejack_Talking_Singing_e320_s8000.zip）。

    同一个角色装了两份的时候，alias 是一样的、只有文件名不同 —— 这是唯一有据可查
    的区分依据。撞名时的后缀一律从这儿取，**绝不现编**。
    """
    f = ((r.get("rvc") or {}).get("file") or "").strip()
    if not f:
        return ""
    return re.sub(r"\.(zip|rar|7z|pth|onnx|tar(\.gz)?)$", "", f, flags=re.I).strip()


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _rest_after(stem, raw):
    """文件名里"去掉角色名之后剩下的那截"，保留原始写法（大小写/分隔符）好读。

      Applejack_Talking_Singing_e320_s8000 − Applejack → _Talking_Singing_e320_s8000
      AdagioDazzleV3 − AdagioDazzle                   → V3
      CozyGlow V2    − CozyGlow                       → " V2"
    """
    ns, nr = _norm(stem), _norm(raw)
    if not nr or not ns.startswith(nr):
        return ""
    cnt = 0
    for i, ch in enumerate(stem):
        if ch.isalnum():
            cnt += 1
            if cnt == len(nr):
                return stem[i + 1:]
    return ""


def _dup_tag(raw, stem, maxc=20):
    """撞名时的后缀。来源按"哪个信息量最大"排序，**全都是真实存在的字符串**：

      ① 文件名比角色名多出来的那截   Applejack…s8000 → "Talking Singing e320"
      ② 文件名整体（①取空但文件名≠原名）  AriaBlazeV3   → "V3"
      ③ 原名里的版本尾巴                 AdagioDazzleV2 → "V2"
      ④ 原名本身（拼错的那个，原名就是区分点）AddagioDazzle → "AddagioDazzle"

    全都取不到才返回空串，让调用方退回「第N份」。
    """
    rest = _rest_after(stem, raw)
    toks = [t for t in re.split(r"[^A-Za-z0-9]+", rest) if t]
    if toks:
        out = ""
        for t in toks:
            cand = (out + " " + t).strip() if out else t
            if len(cand) > maxc:
                break
            out = cand
        return out
    if stem and _norm(stem) != _norm(raw):
        return stem[:maxc]
    m = re.search(r"(?i)\s*(v\d+|\d+)$", raw)
    if m:
        return m.group(1).upper()
    return raw[:maxc]


def retranslate_imported(all_roles=False):
    """给**已经装进来**的市场角色把英文名补译成中文名。返回 [{'id','from','to'}]。

    为什么需要它：字典是慢慢补的。2026-10-13 之前装的那批角色，卡片上还是一水
    英文（当时字典里没有那条名字），用户不该为了享受后来补的字典去重装一遍。

    四条规矩（每一条都得守住，不然就是偷偷改用户的东西）：
      ① 默认**只动来自声音市场**的角色（source 以 `modelhub:` 开头）—— 自己建的
         角色叫什么名字是用户说了算，不许替他改；
      ② 用户自己改过的名字不动。判断依据：现在这个名字**是不是我们译出来的**
         （等于译名、或译名+我们加的后缀）。是才允许重新取名，不是就跳过；
      ③ 原名**永远留着**：写进 alias 和简介「原名 xxx · 来自声音市场（repo）」，
         万一哪条译错了他一眼能对着原名核对；
      ④ 撞名时的后缀只从**市场原始文件名 / 原名**里取，绝不现编一个（见 _dup_tag）。

    2026-10-13 第二轮改了发名字的顺序：原来按角色表顺序先到先得，实测干净名
    「阿达吉奥·达佐」被 V2 那个包拿走了、正主反倒挂着后缀。现在先按 name_rank
    排座次（字典精确命中 > 剥掉版本尾才命中 > difflib 纠错 > 只能音译）。
    """
    # modelhub 只在需要的时候 import（它联网那部分依赖重、启动时不该拖住）
    import modelhub
    _cjk = re.compile(r"[\u4e00-\u9fff]")
    db = load_roles()

    # ---- 第一步：挑候选 ----------------------------------------------------
    cands = []
    for r in db["roles"]:
        src = r.get("source") or ""
        market = src.startswith("modelhub:")
        if not (market or all_roles):
            continue
        raw = (r.get("alias") or (r.get("name") or "")).strip()
        if not raw:
            continue
        cn = modelhub.to_cn(raw)
        if not cn:
            continue
        cur = (r.get("name") or "").strip()
        # 名字里已经有中文 ⇒ 只在我们"这名字本来就是自动译出来的"时才重新取名。
        # 用户自己改过的名字（哪怕改成了别的中文）一律不动 —— 那是他说的算的。
        if cur and _cjk.search(cur) and not (cur == cn or cur.startswith(cn + " (")
                                             or cur.startswith(cn + "·")):
            continue
        cands.append((modelhub.name_rank(raw), r.get("created") or "", r, raw, cn, cur))

    # ---- 第二步：排座次，谁更正谁拿干净名 ----------------------------------
    # 不排座次就是按角色表顺序先到先得 —— 实测把干净名「阿达吉奥·达佐」发给了
    # V2 那个包，正主反倒被挂上后缀。现在字典精确命中的排最前，只能音译的排最后。
    cands.sort(key=lambda t: (t[0], t[1]))

    # 已经用掉的名字（同一个仓库常常同时有 AriaBlaze / AriaBlazeV2 / AriaBlazeV3，
    # 都翻成「阿里亚·布雷兹」的话三张卡长得一样，用户没法挑）
    used = set((x.get("name") or "").strip() for x in db["roles"])
    for _, _, r, raw, cn, cur in cands:
        used.discard(cur)                      # 先把候选自己占的旧名腾出来，免得跟自己撞

    done, nth = [], {}
    for _, _, r, raw, cn, cur in cands:
        name = cn
        if name in used:
            tag = _dup_tag(raw, _market_stem(r))
            if not tag:
                n = nth.get(cn, 1) + 1
                nth[cn] = n
                tag = "第%d份" % n
            name = "%s (%s)" % (cn, tag)
            while name in used:
                name = name + "·"
        used.add(name)
        if name == cur:
            continue
        src = r.get("source") or ""
        repo = src.split(":", 1)[1] if src.startswith("modelhub:") else ""
        r["name"] = name
        r["alias"] = raw
        r["name_auto"] = True        # 记一笔"这名字是我们译的"，以后用户手改就作废
        r["intro"] = ("原名 %s · 来自声音市场（%s）" % (raw, repo)
                      if repo else "原名 %s" % raw)
        done.append({"id": r["id"], "from": cur, "to": name})
    if done:
        save_roles(db)
    return done


def job_install_model(job, repo, fname=None, name=None, max_roles=8, all_packs=False,
                      files_wanted=None):
    """从**声音市场**装别人训练好的模型，装完直接变成能唱的角色。

    为什么要把"下载 → 解压 → 放进 RVC 目录 → 对上索引 → 建角色卡片"一口气做完：
    这几步少做一步，症状都是"模型加载失败"或者"能选但唱出来是哑的"，而用户
    根本无从判断是**模型本身有问题**还是**我少放了一个文件**。约定死在这里：
      · 权重必须落进 RVC 的 assets/weights，且命名 = 角色 id（跟自训模型同口径）；
      · 索引必须落进 assets/indices，且命名必须能被 `_find_rvc_index()` 认出来
        （`<rid>_added_..._<rid>_v1.index`）—— 不这么取名，index_rate 会被白降级成 0；
      · 装之前先用 `rvc_ckpt_check.py` 真的 torch.load 一次：文件在 ≠ 能加载。

    ★★ all_packs：一个仓库里往往躺着**一整个作品的角色**（小马宝莉那几个合集
    仓库实测有 14~16 个 zip）。只装第一个文件的话，用户点一次「安装」拿到的是
    文件清单最靠前那个（常常是边角角色），而他要的是全角色。所以默认整批装，
    每个包一步进度；某个包坏了跳过它继续下一个，全部失败才报错。
    """
    import modelhub

    job["stage"] = "读仓库"
    info = modelhub.detail(repo)
    if info.get("error"):
        raise Fail("读不到这个仓库：%s" % info["error"])
    files = info.get("files") or []
    if not files:
        raise Fail("这个仓库里一个文件都没有")
    packs = [p["file"] for p in (info.get("packs") or [])]
    if not packs:
        raise Fail("这个仓库里没有权重文件（要 .pth，或装着 .pth 的 .zip）")
    # ★ 判断"这个文件在不在仓库里"必须对着 **packs** 比，不能对着 `info["files"]` ——
    #   后者为了卡片预览被截到前 40 条。踩过：一个 110 文件的仓库里，第 41 个之后的
    #   文件传进来会被判成"不在仓库里"，然后**悄悄退回装第一个包**，
    #   用户点的是"碧琪"，装回来的是"阿普尔"。
    pack_set = set(packs)
    if files_wanted:
        # 一次装指定的一批（去重后的角色清单就是这么进来的）
        picks = [f for f in files_wanted if f in pack_set]
        lost = [f for f in files_wanted if f not in pack_set]
        if lost:
            log(job, "  ⚠ 有 %d 个文件在这个仓库里找不到，跳过：%s%s"
                % (len(lost), "、".join(lost[:3]), " 等" if len(lost) > 3 else ""))
        if not picks:
            raise Fail("指定的那批文件一个都不在这个仓库里 —— 多半是仓库更新过，"
                       "名字变了。回市场重新打开这个仓库看看。")
    elif fname and fname in pack_set:
        picks = [fname]                     # 指定了就只装这一个
    elif all_packs:
        picks = packs
    else:
        picks = packs[:1]
    log(job, "这个仓库里有 %d 个可装的包，本次装 %d 个%s"
        % (len(packs), len(picks),
           "" if len(picks) == 1 else "（只想装某一个的话，展开卡片上的包列表，点那一个「装这个」）"))

    job["stage"] = "下载模型"
    made = []
    for i, pick in enumerate(picks):
        if len(made) >= max_roles:
            log(job, "  已经装好 %d 个，到这次的上限了（还想装可以再点一次）" % len(made))
            break
        if len(picks) > 1:
            log(job, "【第 %d/%d 个包】%s" % (i + 1, len(picks), os.path.basename(pick)))
        try:
            made += _install_one_pack(job, repo, pick, name=name,
                                      budget=max_roles - len(made))
        except Fail as e:
            # 单个包没装成（下不动 / 里面没有 .pth）不该把整批拖死
            log(job, "  ✘ 这个包没装成：%s（跳过，继续下一个）" % str(e).splitlines()[0])
        except Exception as e:
            log(job, "  ✘ 这个包出问题了：%s: %s（跳过）" % (type(e).__name__, str(e)[:120]))

    if not made:
        raise Fail("一个都没装成 —— 上面每一步都写了原因，最常见的是这些权重 RVC 加载不了。")
    job["stage"] = "完成"
    log(job, "共装好 %d 个角色：%s" % (len(made), "、".join(m["name"] for m in made)))
    # ★ 顺手把库里**还挂着英文名的老角色**也补译成中文：字典是后来一点点补的，
    #   用户不该为了享受新字典去重装一遍，也不该看着一半中文一半英文。
    #   （只动 source 是 modelhub: 的那些 —— 自己建的角色不许替他改名。）
    fixed = retranslate_imported()
    if fixed:
        log(job, "顺手把 %d 个还写着英文名的老角色译成了中文：%s"
            % (len(fixed), "、".join("%s→%s" % (f["from"], f["to"]) for f in fixed)))
    return {"installed": made, "repo": repo,
            "file": (picks[0] if len(picks) == 1 else ""), "count": len(made)}


def train_role(job, rid, epochs=None, batch=None, version="v5turbo", quality="standard",
               pct_from=0, pct_to=88):
    """训练（或继续训练）一个角色。会先按当前标注重写 list。

    quality 档位 -> 训练轮数：fast=15 / standard=30 / perfect=60
    （GPT 侧官方配置 s1longer-v2.yaml 的默认是 20，这三档是对照官方值定的）。
    轮数越多音色越像，但过头会过拟合（合成开始复读/漏字）——所以训练完会
    逐个体检 ckpt（拿真实判据合成一句话），不行就自动降回更早的权重。

    这里训的是 **GPT 侧**（stages/train_cli.py → s1_train.py），产物在
    GPT_weights_v5turbo/；v5turbo 不训 SoVITS 侧。详见下面「轮数体检」的注释。

    pct_from/pct_to: 本角色训练在整条任务进度条里占的区间。一次导入会连着
    训多个角色，不给区间的话第二个角色会继承前一个的百分比。
    """
    if not epochs:
        epochs = QUALITY_PRESETS.get(quality, 30)
    # ---- 轮数体检：先分清"训的是哪一侧" ----
    # 本项目的训练路径是 stages/train_cli.py → GPT_SoVITS/s1_train.py，也就是
    # **GPT 侧（文本→语义）**；v5turbo 不训 SoVITS。产物落在 GPT_weights_v5turbo/。
    #
    # 两个轮数完全不是一个量级，别混：
    #   · SoVITS 侧（s2_train 的 sovits_epoch）：官方默认 **8**（v1/v2/v2Pro/v2ProPlus），
    #     v3/v4 是 2 —— 这一侧本项目根本不训；
    #   · GPT 侧（s1_train 的 epochs，**我们实际传的就是这个**）：官方配置
    #     GPT_SoVITS/configs/s1longer-v2.yaml 实测默认 **20**
    #     （同目录 s1.yaml / s1big*.yaml 是 300，s1mq.yaml 是 100）。
    # 早先这里把 8 当成"我们该用的轮数"，并推出"大帝 60 轮 = 官方 7.5 倍"，是错的：
    # 60 轮对 20 轮是 3.0 倍；150 轮（阿塔尼斯现役）才是 7.5 倍。
    #
    # 过拟合的症状（复读 / 漏字 / 长句乱掉）本身成立，所以提示保留，但基准对齐 20。
    # ⚠ 这里**只提示不动默认值**：改默认会影响所有角色，必须先有同素材 A/B 数据
    #   （`_probe_gsv_epochs_ab.py`）。拿到数据再改，不许凭文献直接改。
    if int(epochs) >= 20:
        log(job, "  [提示] 这次训 %d 轮。我们训的是 **GPT 侧**，官方配置 "
                 "s1longer-v2.yaml 的默认轮数是 **20**"
                 "（常被引用的 8 轮是 SoVITS 侧的 sovits_epoch，v5turbo 不训它）。"
                 "轮数明显超出的常见症状是**复读 / 漏字 / 长句乱掉**。"
                 "训完会逐个体检 ckpt 并自动降档；听感发飘就用 20 轮重训一版对比。"
                 % int(epochs))
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在: %s" % rid)
    d = role_dir(rid)
    list_path, n = write_list(rid)
    if n < 3:
        raise Fail("可用标注只有 %d 条（至少要 3 条）。先到角色页补几句文本，或重新上传素材。" % n)

    step_prep(job, rid, list_path, os.path.join(d, "slices"))
    ckpt, ep = step_train(job, rid, epochs, batch, pct_from=pct_from, pct_to=pct_to)

    # ---- 训练后体检：合成一句话过判据，不过就降档到更早的 ckpt ----
    # 体检阶段接上区间尾部。体检要真跑推理合成，每句 30 秒上下，
    # 不给反馈的话这里又是一段"看不出动静"的等待。
    job["stage"] = "出声体检"
    job["detail"] = "训练完成，正在验证音色"
    job["pct"] = max(job.get("pct") or 0, pct_to)
    save_job(job)
    try:
        ref_file, ref_text = _ensure_ref(role, rid, d, job)
        ref_wav = os.path.join(d, "slices", ref_file)
        cks = sorted([f for f in os.listdir(GPT_WEIGHT_DIR)
                      if f.startswith(rid + "-e") and f.endswith(".ckpt")],
                     key=lambda f: _ckpt_ep(f), reverse=True)
        chosen, vinfo = ckpt, None
        cand_list = cks[:4]
        vspan = max(pct_to - 88, 1) if pct_to <= 88 else 11
        vbase = 88 if pct_to <= 88 else pct_to
        for ci, f in enumerate(cand_list):   # 最多体检 4 个，避免无限拖
            cand = os.path.join("GPT_weights_v5turbo", f)
            log(job, "  体检 %s ..." % f)
            job["pct"] = vbase + int(vspan * ci / max(len(cand_list), 1))
            job["detail"] = "验证第 %d 个权重：%s" % (ci + 1, f)
            save_job(job)
            ok, info = verify_ckpt(job, rid, cand, ref_wav, ref_text)
            if ok:
                chosen, vinfo, ep = cand, info, _ckpt_ep(f)
                log(job, "  体检通过（语速 %.2f 秒/字，相似度 %s）"
                    % (info.get("sec_per_char") or -1, info.get("similarity")))
                break
            log(job, "  体检没过：%s，试更早的权重" % info.get("error"))
        if vinfo is None:
            log(job, "  [warn] 所有 ckpt 体检都没过，先用最高轮数的，建议减少轮数重训")
        else:
            # 体检通过的这段音频直接留作试听样本 —— 训练完立刻能听，
            # 不用再手动去「说话」页敲一句才知道像不像
            sample = vinfo.get("out")

            def _sample(r, _p=sample, _ep=ep):
                outs = r.setdefault("outputs", [])
                outs[:] = [o for o in outs
                           if not (o.get("kind") == "speech" and o.get("text") == VERIFY_TEXT
                                   and o.get("epoch") == _ep)]
                if _p and os.path.exists(_p):
                    outs.append({
                        "text": VERIFY_TEXT, "wav": _p, "at": _now(), "kind": "speech",
                        "track": "体检样本 · %d 轮" % _ep,
                        "seconds": vinfo.get("seconds"),
                        "similarity": vinfo.get("similarity"),
                    })

            mutate_role(rid, _sample)
        ckpt = chosen
    except Fail as e:
        log(job, "  [warn] 体检环节出错（%s），直接用最高轮数权重" % e)

    def _apply(r):
        r["model"] = {"ckpt": ckpt.replace("\\", "/"), "epochs": ep, "at": _now(),
                      "quality": quality, "verified": bool(vinfo)}
        if not r.get("ref"):
            try:
                p, t = pick_reference(r, os.path.join(d, "slices"))
                r["ref"] = {"file": os.path.basename(p), "text": t}
            except Fail as e:
                log(job, "  [warn] %s" % e)

    role = mutate_role(rid, _apply)
    log(job, "  角色 %s 训练完成：%s（%d 轮）" % (rid, ckpt, ep))
    # 训练完顺手把这一轮的中间存档收一收（只留最近几份）。
    # 不做的话：每轮 155MB × 每训一次十几份，训三四次就把盘吃掉好几个 G，
    # 而用户没有任何入口去删 —— 这就是「存储清理」面板存在的理由。
    try:
        r = prune_gsv_weights(rid)
        if r.get("freed"):
            log(job, "  已顺手清掉 %d 份旧存档，释放 %s"
                % (len(r.get("deleted") or []), r.get("freed_s")))
    except Exception as e:
        log(job, "  [warn] 清理旧存档时出错（不影响本次训练）：%s" % e)
    return role["model"]


# 体检用的固定句子：字数固定，语速判据（秒/字）才有意义
VERIFY_TEXT = "今天天气不错，我们一起去公园散散步，顺便聊聊最近发生的事情。"
QUALITY_PRESETS = {"fast": 15, "standard": 30, "perfect": 60}
RVC_QUALITY_PRESETS = {"fast": 50, "standard": 150, "perfect": 300}

# 训练轮数上限：GPT-SoVITS 说话模型和 RVC 唱歌模型的上限不一样。
# GPT-SoVITS 过头会过拟合（合成发疯），60 轮已接近可用极限，卡到 200 只是兜底；
# RVC 轮数少会沙哑、**越多越稳**，官方默认就是 20000 轮，所以这里给 5000。
MAX_TRAIN_EPOCHS = {"gsv": 200, "rvc": 5000}

# ---------------------------------------------------------------------------
# RVC 架构（v1 / v2）与采样率（2026-10-16 新增 v2 支持）
#
# 官方 FAQ 给的 v1/v2 对比：
#     v1 —— 256 维特征（9 层 HuBERT）、1 个判别器、采样率 32k/40k/48k、训练更快
#     v2 —— **768 维特征（12 层 HuBERT）、4 个判别器、采样率 32k/48k、质量更好**
#   原文："v2 generally produces better results but requires more GPU memory
#          during training."
#
# ★ 两条硬约束（都不是我们选的，是 RVC 自己的）：
#   ① **v2 没有 40k** —— configs/v2/ 下面只有 32k.json 和 48k.json。
#      所以 v2 必须配 48k（或 32k），不能沿用我们一直在用的 40k。
#   ② **换架构/采样率必须从头重训**。而且 logs/<exp>/config.json 会被新架构覆盖，
#      旧断点是旧架构、读进来必然 size mismatch。⇒ v2 走**独立的 exp 名**
#      （<rid>_v2），和 v1 的断点、索引、权重彻底分开，互不干扰。
#
# ★ 默认仍是 v1/40k：**不改动现有行为**，大帝现有的 300 轮模型不受影响。
#   v2 是一项"下一回重训时可以选的更好架构"，不是偷偷把默认值换掉。
# ---------------------------------------------------------------------------
RVC_ARCH_DEFAULT = "v1"
RVC_ARCH_SR = {"v1": "40k", "v2": "48k"}          # 每版配套的采样率
RVC_ARCH_SRS = {"v1": ("32k", "40k", "48k"),       # 每版支持的采样率（硬约束）
                "v2": ("32k", "48k")}
RVC_ARCH_EXP_SUFFIX = {"v1": "", "v2": "_v2"}      # v2 用独立 exp，见约束 ②
# 批大小：v1/40k 在本机 8GB 卡上 bs=4 一直很稳；v2 官方说更吃显存 ⇒ 往下让。
# ★★ **这个数是真跑出来的，不是猜的**（2026-10-16，`_probe_v2_train.py`）：
#    用大帝 353 段素材真跑 v2/48k、bs=2、2 轮，**全流程通过**：
#      · 底模 `f0G48k.pth` / `f0D48k.pth` 加载报 `<All keys matched successfully>`
#      · 提特征落到 `3_feature768`（v2 的 768 维，索引训练确认 `特征形状：(66300, 768)`）
#      · 训练 2 轮正常保存，索引建成 `..._ra500fb92_v2_v2.index`
#      · **显存只用了 3039 MiB，还剩 4853 MiB**
#    ⇒ bs=2 在 8GB 卡上绰绰有余。想更快可以往 3 / 4 调，但那两档**没实测过**，
#      爆了就把这里改回 2（改这一个常量就行，不用动训练命令）。
RVC_ARCH_BS = {"v1": 4, "v2": 2}
# 采样率对显存的额外要求（2026-10-18 实测新增）。
# v2 的 bs 是照 **32k** 定的。48k 的生成器上采样是 12,10,2,2（32k 是 10,8,2,2），
# 判别器也要在 48k 上工作，同样 bs 下显存明显更高。
# 实测症状（这台 8GB 卡、v2/48k、bs=2、**显存已被用户程序占走 4.2GB 时**）：
# 开训后 GPU 利用率 99%、显存 7755/8151 MiB，
# 但 **tfevents 十分钟一个字没写、轮次一行都没出** —— 是显存打满后的空转，
# 不是"跑得慢"。这种卡死比报 OOM 更坏：没有报错，只能干等。
# 所以 48k 默认降到 1，宁可慢一点也不要卡住。
# ⚠ 这是**显存压力**护栏，不是 48k 的固有属性：同一台机器上显存只剩 1.8GB 被占时，
#   32k 的 bs=2 跑得很顺（95 秒/轮）。48k 想用 bs=2 就先腾显存，再把这里改开。
# ⚠ 这是**显存压力**护栏，不是 48k 的固有属性：同一台机器上显存只剩 1.8GB 被占时，
#   48k 的 bs=2 只要 ~90 秒/轮（实测 15 轮 22 分钟跑完）；32k 同样很顺。
#   所以真正该看的是「还剩多少显存」，不是「采样率多少」—— 下面这个阈值配合
#   `_vram_free_mb()` 做自适应，显存够就放回默认批大小（否则白慢 5 倍）。
RVC_SR_BS_CAP = {"48k": 1}
# 48k + 默认批大小（v2 是 2）大约要这么多空闲显存才安全。
# 实测：被占 4.2GB（只剩 ~3.9GB）时假死；被占 1.8GB（剩 ~6.3GB）时全程 5951 MiB 跑完。
# 取 4800 留约 1GB 余量（判别器那部分峰值比稳态高）。
RVC_48K_BS2_FREE_MB = 4800

# ---------------------------------------------------------------------------
# 自定义底模（2026-10-18 新增）——「唱歌专用底模」这条路
#
# 为什么：RVC 官方底模（assets/pretrained*/*）是在**语音/朗读**语料上训的。
# 社区一致反映：拿它训出来的模型去唱歌会"像念歌词"，因为底模本身没见过
# sustained tone（持续音）/ 颤音 / 滑音。底模决定的是**初始化先验** ——
# 先验里没有"唱歌"这件事，后面拿多少台词素材都补不回来。
# 这正是本项目 2026-10-18 实测确认的瓶颈：全库零唱歌素材，模型只能用台词训。
#
# KLM-X-10（SeoulStreamingStation / 韩国 AI Hub 社区）：语料是多语种剧本 +
# 男女歌手**歌唱数据**，模型卡标签明确写 `singing-voice-conversion`。
#   · **只有 32k 版**（作者说 40k/48k 在追加训练中）⇒ 必须配 v2/32k
#     （v2 支持 32k；用 v2 是因为 768 维特征、官方说质量更好）
#   · 语言标签 ko/jp/en —— **没有中文**，这是风险点，必须用本库素材实测
#   · 许可证 cc-by-nc-2.0（非商用）
#
# 实测状态：2026-10-18 已下载，并用 `rvc_pretrain_check.py` 在 v2/32k 下
# 验过 `missing=0 / unexpected=0` —— 真能加载，不是"文件在就算数"。
# ---------------------------------------------------------------------------
RVC_CUSTOM_PRETRAINS = {
    "klm": {
        "sub": "pretrained_custom",
        "g": "KLMX10_f0G32k.pth",
        "d": "KLMX10_f0D32k.pth",
        "arch": "v2",
        "sr": "32k",
        "label": "KLM-X-10 唱歌底模",
    },
    # 同一系列（SeoulStreamingStation 的 KLM 歌唱语料）的 48k 版，见 KLM49_HFG 仓库。
    # 为什么值得多一个 48k：32k 的天花板是 16kHz —— 齿音/气声那一层**生成不出来**，
    # 而"AI 感重 / 咬字不清"的抱怨正好落在这一层。官方 lj1995 的 f0G48k 是**说话**
    # 底模，所以"48k + 歌唱语料"这个组合只有 KLM 家族有。
    # ⚠ 这个仓库**没有 model card、没有语言列表、没有许可证**（KLM-X-10 至少写了
    #   cc-by-nc-2.0 + ko/jp/en）。所以它只能算**待验证候选**，采用与否看实测。
    "klm48": {
        "sub": "pretrained_custom",
        "g": "G_KLM_HFG_48k.pth",
        "d": "D_KLM_HFG_48k.pth",
        "arch": "v2",
        "sr": "48k",
        "label": "KLM-HFG 唱歌底模（48k）",
    },
}

# ---------------------------------------------------------------------------
# 开训前的素材体检（2026-10-16 新增）
#
# 为什么要有这一步：多篇 RVC 训练实践（含官方仓库 FAQ 的 "How do I improve
# training quality?"）在数据质量上给的标准高度一致 —— 而且一致认为
# **数据质量 > 数据数量 > 训练轮数**。以前我们开训前只看"够不够 10 段"，
# 响度一不一致、底噪干不干净、采样率够不够，全都没量过。
#
# 阈值来源（每一条都写清是文献给的还是本机实测的）：
#   采样率  44.1k/48k 且全库统一    —— 文献
#   单段    3~15 秒（5~10 最佳）     —— 文献
#   总时长  10~50 分钟               —— 文献
#   底噪    <-60 优秀 / <-50 合格 / >-40 不合格  —— 文献
#   响度一致 ±3 dB 优秀 / ±6 dB 合格 —— 文献
#
# ★ 本机实测校准（`_probe_traindata.py`，大帝 353 段，2026-10-16）：
#     32k 统一 / 中位 4.87s / 29.5 分钟 / 底噪中位 **-91.2 dB** / 响度跨度 **3.0 dB**
#     ⇒ 除了采样率，大帝的素材全部压在"优秀"线上。
#   ⚠ 所以 TRAIN_SR_MIN **不能**按文献写 44100 —— 本机切片用的是 GPT-SoVITS 自带的
#     `tools/slice_audio.py`，它写死 `load_audio(inp_path, 32000)` 再按 32000 写出
#     （那是给说话模型用的），**现有角色的切片因此全是 32k**。按 44.1k 判会满屏报警
#     而用户一点办法都没有（切片已经切完了）。⇒ 采样率只报"信息"级，并说清
#     "高频上限被锁在 16 kHz、下次导入新素材时会保住原采样率"。
# ---------------------------------------------------------------------------
TRAIN_SEG_MIN = 3.0            # 单段最短（短于它信息量不足）
TRAIN_SEG_MAX = 15.0           # 单段最长
TRAIN_TOTAL_MIN = 600.0        # 总时长下限 10 分钟
TRAIN_TOTAL_MAX = 3000.0       # 总时长上限 50 分钟（再多边际收益很小）
TRAIN_FLOOR_BAD = -40.0        # 底噪高于此 ⇒ 这一段不合格
TRAIN_FLOOR_WARN = -50.0       # 介于 warn~bad ⇒ 合格但不够好
TRAIN_LOUD_SPAN_GOOD = 3.0     # 响度 p10~p90 跨度；超过 GOOD 提醒、超过 WARN 警告
TRAIN_LOUD_SPAN_WARN = 6.0
TRAIN_SR_MIN = 32000           # 见上：按本机切片工具的实际情况定，不按文献的 44.1k
TRAIN_AUDIT_MAX = 150          # 体检最多抽这么多段（够统计了，别把开训拖慢）


def clamp_epochs(n, kind="gsv"):
    """把用户填的自定义轮数收进合理范围。返回 (轮数, 提示或 None)。"""
    hi = MAX_TRAIN_EPOCHS.get(kind, 200)
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 0
    if n <= 0:
        return 0, None
    if n > hi:
        return hi, "你要的 %d 轮超过上限，已按 %d 轮开始训。" % (n, hi)
    return n, None


_RVC_STATE_CACHE = {}      # exp -> (G文件mtime, 轮数)


def _rvc_state_stamp(exp):
    """用来判断缓存是否过期：logs/<exp>/G_*.pth 的 mtime。"""
    d = os.path.join(RVC_ROOT, "logs", exp)
    try:
        gs = [os.path.join(d, f) for f in os.listdir(d)
              if f.startswith("G_") and f.endswith(".pth")]
    except OSError:
        return None
    if not gs:
        return None
    return max(os.path.getmtime(p) for p in gs)


def rvc_trained_epochs(exp, use_cache=True):
    """这个 RVC 实验之前训到第几轮（从没训过返回 0）。

    走 stages/rvc_state.py —— 读 logs/<exp>/G_*.pth 里的 epoch 字段（该字段
    名叫 iteration 但存的是 epoch，见该脚本注释）。那必须用 RVC 自己的 python
    打开（torch 是整合包里的），且 cwd 必须是 RVC 根目录，所以放在子进程里跑，
    不能在这个进程里 import torch。读不出来就当 0（没训过），宁可多跑一轮，
    也不要因为探测失败拦住用户。

    起步一次约 3 秒（大头是 torch 的 import），而界面每打开一次角色详情都会问，
    所以按 G 文件 mtime 缓存：文件没动就直接回缓存值。
    """
    stamp = _rvc_state_stamp(exp)
    if stamp is None:
        return 0
    hit = _RVC_STATE_CACHE.get(exp)
    if use_cache and hit and hit[0] == stamp:
        return hit[1]
    try:
        out = subprocess.run(
            '"%s" -s "%s" %s' % (RVC_PY, os.path.join(STAGES, "rvc_state.py"), exp),
            cwd=RVC_ROOT, capture_output=True, timeout=180)
        tail = (out.stdout or b"").decode("utf-8", "ignore").strip().split("\n")[-1]
        n = int(tail.strip())
    except Exception:
        return (hit[1] if hit else 0)
    _RVC_STATE_CACHE[exp] = (stamp, n)
    return n


# 兼容旧名字：老代码/脚本按 _rvc_trained_epochs 调用
_rvc_trained_epochs = rvc_trained_epochs


def _rvc_weight_epoch(f):
    """从 `<exp>_e150_s1200.pth` 里读出轮数；裸 `<exp>.pth`（最终权重）返回 None。"""
    m = re.search(r"_e(\d+)_s\d+\.pth$", f)
    return int(m.group(1)) if m else None


def pick_rvc_weight(names, base_dir=None):
    """在候选权重里挑「训练得最充分」的那一份。

    规则：带轮数后缀的按轮数数值取最大；裸 `<exp>.pth` 是 train.py 训完时
    通过 savee() 另存的最终权重，永远优先于任何中间快照。

    names 里的元素可以是文件名，也可以是绝对路径（两处调用方给的不一样）。
    base_dir 给的是「文件名相对谁」——裸文件名用 os.path.getmtime 会拿 CWD
    去解析（训练时 CWD 是 RVC 根，不是 weights 目录），结果全变 0，
    于是"取最新的"退化成"取第一个"。所以调用方要给 base_dir。
    多个裸权重时按**文件 mtime** 取最新 —— 注释一直这么写，代码却是 max()
    按字典序，名不副实；字典序在这里本来就是个坑（`e150` < `e90`）。
    """
    def _base(f):
        return os.path.basename(f)

    def _mtime(f):
        p = f
        if not os.path.isabs(p) and base_dir:
            p = os.path.join(base_dir, p)
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    named = [f for f in names if _rvc_weight_epoch(_base(f)) is not None]
    bare = [f for f in names if _rvc_weight_epoch(_base(f)) is None]
    if bare:
        # 多个裸权重时取最后修改的（最新的那次训练）
        return max(bare, key=_mtime)
    if named:
        return max(named, key=lambda f: _rvc_weight_epoch(_base(f)))
    # 走到这里说明候选是空的（每个文件名都必然能落进上面两支之一）。
    # 旧代码是 `sorted(names)[-1]`，空列表会直接 IndexError。
    return max(names, key=_mtime) if names else None


# ------------------------------------------------ RVC 底模（预训练模型）
#
# ★ 这是「唱出来的歌没法听」的**头号原因**，而且它坏得**一点声音都没有**：
#   RVC 的训练脚本 -pg / -pd（pretrainG / pretrainD）默认是空字符串，
#   不传就从随机初始化开始训。从零训出来的模型**根本学不会跟着旋律唱**
#   —— 它只会模仿训练素材的平均音色，于是：
#       · 输出死死卡在角色自己的音区（实测原唱 268Hz、角色输出 106Hz）
#       · --pitch 从 -12 到 +12 扫一遍，输出音高**纹丝不动**
#       · 听起来就是一坨糊，谁都只会说"难听"，没人知道是模型没底子
#   本机实测：logs/r8c323d43/train.log 里 'pretrainG': ''，66 条素材、300 轮，
#   整条链从分离到混音全对，成品照样没法听 —— 因为模型本身是废的。
#
#   官方 WebUI 的取名规则（webui.py 的 get_pretrained_models）：
#       assets/pretrained{f0}...  —— v1 用 assets/pretrained/，v2 用 assets/pretrained_v2/
#       文件名 {f0}G{sr}.pth（生成器） / {f0}D{sr}.pth（判别器）
#   v2 的 40k 底模也能喂 v1 吗？不能 —— config 的 hidden_channels 不一样（192 vs 768），
#   加载会直接报尺寸不匹配。所以版本必须对上。

RVC_PRETRAIN_HOSTS = (
    # hf-mirror 在国内能直连；hf 官方本机实测连不上（HTTP 000），放第二候选。
    "https://hf-mirror.com/lj1995/VoiceConversionWebUI/resolve/main",
    "https://huggingface.co/lj1995/VoiceConversionWebUI/resolve/main",
)


def rvc_pretrained_paths(version="v1", sr="40k", custom=None):
    """这一版（version + 采样率）对应的底模应该在哪。

    custom 传 `RVC_CUSTOM_PRETRAINS` 的键（如 "klm"）时改用自定义底模。
    """
    c = RVC_CUSTOM_PRETRAINS.get(custom) if custom else None
    if c:
        d = os.path.join(RVC_ROOT, "assets", c["sub"])
        return (os.path.join(d, c["g"]), os.path.join(d, c["d"]))
    sub = "pretrained_v2" if str(version).lower() == "v2" else "pretrained"
    d = os.path.join(RVC_ROOT, "assets", sub)
    return (os.path.join(d, "f0G%s.pth" % sr), os.path.join(d, "f0D%s.pth" % sr))


def rvc_pretrained_here(version="v1", sr="40k", custom=None):
    """本机有没有这一版的底模。返回 (G, D)，缺哪个哪个是 None。"""
    gp, dp = rvc_pretrained_paths(version, sr, custom)
    ok = lambda p: os.path.isfile(p) and os.path.getsize(p) > 10 * 1024 * 1024
    return (gp if ok(gp) else None, dp if ok(dp) else None)


def check_rvc_pretrained(job, version="v1", sr="40k", custom=None):
    """开训前体检一次底模：真的能加载进要训的那个架构吗。

    为什么非验不可：底模"文件在"不等于"用得上"。版本/采样率选错时文件照样在位，
    但键名和形状对不上 —— 训练跑得欢，底模却等于没用，最后又训出一个不会跟调
    的模型，而日志里只有一屏 size mismatch，没人看得懂。
    返回 (是否通过, 说明)。
    """
    gp, dp = rvc_pretrained_paths(version, sr, custom)
    rc, out = run(job, '"%s" -s "%s" %s %s "%s" "%s"'
                  % (RVC_PY, os.path.join(STAGES, "rvc_pretrain_check.py"),
                     version, sr, gp, dp),
                  cwd=RVC_ROOT, timeout=900)
    tail = [ln for ln in (out or "").splitlines() if ln.strip().startswith("PRETRAIN_")]
    msg = tail[-1] if tail else ("体检脚本没输出（退出码 %d）" % rc)
    return rc == 0, msg


def ensure_rvc_pretrained(job, version="v1", sr="40k", custom=None):
    """保证底模在位，不在就下载。返回 (G, D)；实在拿不到返回 (None, None)。

    拿不到时**不抛异常** —— 由调用方决定是硬失败还是继续（比如离线场景），
    但调用方必须把"没有底模"这件事说给用户听。

    ★ 自定义底模**不自动下载**：它们的下载地址不在 RVC_PRETRAIN_HOSTS 里
      （那是官方仓库的固定目录结构），硬塞进去只会得到 404 + 一屏重试日志。
      缺了就立刻返回 None，让调用方明说"这个底模要先放好"。
    """
    gp, dp = rvc_pretrained_here(version, sr, custom)
    if gp and dp:
        return gp, dp
    if custom:
        if job:
            # ★ 不能用 rvc_pretrained_here 的返回值：缺件时它是 (None, None)，
            #   os.path.basename(None) 会直接 TypeError 崩掉（门禁 G 段抓到的）。
            #   要用 paths（它永远给出路径字符串，不管文件在不在）。
            _gp, _dp = rvc_pretrained_paths(version, sr, custom)
            log(job, "  [warn] 自定义底模 %s 不在位（%s / %s）—— 需要手工放好，"
                     "不会自动下载" % (custom, os.path.basename(_gp),
                                       os.path.basename(_dp)))
        return (None, None)
    sub = "pretrained_v2" if str(version).lower() == "v2" else "pretrained"
    os.makedirs(os.path.dirname(rvc_pretrained_paths(version, sr)[0]), exist_ok=True)
    if job:
        log(job, "  本机没有 RVC 底模，正在下载（约 150MB，一次就好）…")
    for name, dst in (("f0G%s.pth" % sr, gp), ("f0D%s.pth" % sr, dp)):
        if dst:                       # 已有一个就别重复下
            continue
        dst = rvc_pretrained_paths(version, sr)[0 if name.startswith("f0G") else 1]
        got = False
        for host in RVC_PRETRAIN_HOSTS:
            url = "%s/%s/%s" % (host, sub, name)
            if job:
                log(job, "  尝试 %s" % url)
            rc, out = run(job, '"%s" -u "%s" "%s" "%s"'
                          % (PY, os.path.join(STAGES, "dl_cli.py"), url, dst),
                          timeout=3600)
            if rc == 0 and os.path.isfile(dst):
                got = True
                break
            if job:
                log(job, "  失败（退出码 %d）：%s" % (rc, _tail(out, 2)))
        if not got:
            if job:
                log(job, "  [warn] %s 下载不下来" % name)
    return rvc_pretrained_here(version, sr)


# ------------------------------------------------ RVC 音质参数
#
# ★ 这几个默认值是**实测选出来的**，不是抄教程。方法：拿用户自己的角色 +
#   自己的歌，切同一段 19 秒干声，跑参数矩阵。两轮测法不一样，都要看：
#
#   第一轮（只量音准）—— 后来发现这一轮的量具坏了，结论作废，见下面第 ③ 条。
#     组                                 match  contour  devIQR  起伏比
#     rmvpe ir0.75 rms0.25 p0.33（旧默认） 0.860   0.968     29     0.93
#     fcpe  ir0.75 rms0.25 p0.33          0.880   0.966     23     0.90
#     fcpe  ir0.50 rms0.25 p0.50          0.891   0.965     23     0.95
#     fcpe  ir0.75 rms0.50 p0.33          0.833   0.963     34     0.98   ← 掉得最狠
#
#   第二轮（量具修好后重测 protect；音准 + 咬字一起看）：
#     protect   吻合(±50)  宽松(±150)  轮廓   八度错   **ASR 字错误率**
#       0.50      87.5%     97.2%     0.968   0.3%       70.5%   ← 咬字最差
#       0.40      87.4%     97.4%     0.968   0.3%       54.1%
#       0.33      88.8%     97.8%     0.968   0.3%       54.1%
#       0.20      88.9%     97.8%     0.968   0.3%       52.5%
#
#   结论：
#   ① 换 FCPE 让逐帧偏差从 29 音分收紧到 23（这一条两轮都成立）。
#   ② rms-mix-rate **必须**留在 0.25 —— 网上有教程说 0.9「保留音量起伏」，
#      实测是反的（RVC 文档：越接近 1 越是恒定响度），照抄那篇会掉 3 个百分点。
#   ③ **protect 必须小于 0.5**。RVC 源码里是 `if protect < 0.5` 才启用清辅音
#      保护，所以 0.50 = **完全不保护**，清辅音也会被模型"唱"一遍 ⇒ 咬字糊。
#      第一轮曾把它改成 0.50（当时只量音准、且量具本身有 bug），第二轮用
#      **ASR 字错误率**当咬字代理重测：0.50 的字错误率 70.5%，比开启保护的
#      52~54% 高出一大截。音准则几乎不受影响（87~89%，都很好）。
#      0.33 / 0.40 / 0.20 三者差异在噪声内 ⇒ 取 RVC 社区默认的 0.33。
#   ④ index-rate 取 0.50：太高会吞字，太低音色不稳。
RVC_RMS_MIX_RATE = 0.25   # 保留原唱的音量起伏（0=全跟随原唱，1=恒定响度）
RVC_PROTECT = 0.33        # 必须 <0.5，否则等于关闭清辅音保护（见 ③）
RVC_INDEX_RATE = 0.50     # 检索强度；太高会吞字，太低音色不稳
#   ★★ 0.50 不是拍的，是 **2026-10-16 实测**出来的权衡拐点（`_probe_indexrate.py`
#      + `_probe_irate_qc.py`）。同一份人声（登阶 201s）、同模型、pitch −12、
#      f0/rms/protect 全固定，**只改 index_rate**，三把尺子一起量：
#
#        index_rate   音色(距角色中心，越小越像)   八度错    音准吻合   咬字(高频占比)
#          0.30              0.2645                8.1%      79.1%       0.37x
#          0.50  ←现值       0.2559                9.9%      76.9%       0.37x
#          0.70              0.2450 ←音色最好      11.2%     75.0%       0.36x
#          0.85              0.2494                11.5%     74.8%       0.35x
#
#      ⇒ **音色和音准是反的**：index_rate 越高音色越像角色（0.30→0.70 单调改善），
#        但**八度错单调恶化**（8.1%→11.5%）、吻合单调下降（79.1%→74.8%）。
#        0.70 音色最好，代价是八度错 +1.3 个百分点、吻合 −1.9 个百分点。
#      ⇒ 0.50 落在两条曲线的交叉区：音色已经比 0.30 好一截，音准还没开始明显掉。
#        官方 FAQ 推荐 0.5~0.75，实测 0.50 在"两者都不差"的位置。
#      ⚠ 别只看音色就往上调：0.70 的八度错 11.2% 已经越过音高体检的坏档线
#        （AUDITION_OCTAVE_BAD=0.11），会让体检更频繁地判"这一档不达标"。
#      ⚠ 咬字（4~8kHz 高频占比）四档只从 0.37x 掉到 0.35x —— "太高会吞字"在
#        这首歌上**没被证实**（相对变化 5%，远小于音色和音准的变化）。
# ★★ f0（基频）提取方法 —— **2026-10-17 实测改成了 rmvpe**，别再改回去。
#
#   文献口径（多篇一致）：rmvpe 是"六边形战士/2026 无脑首选"，fcpe 是"最快"、
#   pm 是"最快但容易哑音"。但**文献说了不算，本机实测说了算**
#   （`_probe_f0ab.py`：大帝模型 + 同一段 30 秒人声，pitch −12、index_rate 0.50、
#   protect 0.33 全部固定，只换 f0 方法）：
#
#     f0     推理用时   八度错   吻合(宽松)   轮廓相关   音高抖动(IQR)
#     fcpe    39.8 s    5.1 %     90.1 %      +0.606     31.7 cents   ← 又慢又抖
#     rmvpe   23.8 s    3.3 %     93.0 %      +0.714     17.1 cents   ★ 又快又准
#     pm      19.4 s    4.5 %     88.2 %        —           —
#
#   ⇒ rmvpe **同时**赢了速度和精度：八度错降 35%，音高抖动**直接减半**
#     （31.7 → 17.1 cents；1 个半音 = 100 cents，31.7 相当于三分之一半音的飘移）。
#     这条正对着老板说的"唱歌越唱越差 / 音听着飘" —— 地基（f0 曲线）本身在抖，
#     后面模型再好也唱不稳。
#   ⚠ 实测结果和资料说法**相反**：资料说 fcpe 最快，本机实测 fcpe 反而最慢
#     （39.8 s vs rmvpe 23.8 s）。所以换这个不花时间，是**白捡的收益**。
#   ⚠ 咬字（4~8kHz 高频占比）三档完全一样（都是输入的 0.44x）—— 换 f0 不影响咬字。
#   ⚠ Crepe 测不了：本机这份 `infer/vc/pipeline.py` 只认 pm / rmvpe / fcpe
#     （`get_f0` 第一行就 `if f0_method not in ("pm","rmvpe","fcpe"): raise`）。
RVC_F0_METHOD = "rmvpe"

# RVC 分片长度（秒/片）。★ 这是「整首转换十几分钟」的开关，别随便调回 60。
#   实测（本机 5070 Laptop / 8G，历史任务日志里的分项耗时）：
#     30 秒以内的轨：合成 1.7 秒；RVC 自带的 60 秒一片：合成 905 秒。
#   时长 2 倍、耗时 500 倍 ⇒ 长序列上显存/计算退化，不是线性慢。
#   20 秒 + 前后各 3 秒 pad = 26 秒一片，落在快的一侧，多出来的 ~30% 重叠
#   计算换来的是整首从 905 秒降到十几秒。
#   改它**不影响音色**：f0 / hubert / 索引检索 / 整体 change_rms 全部照旧，
#   只有「一次前向喂多少帧」变了（详见 stages/rvc_infer.py 的 apply_chunk）。
RVC_CHUNK_SEC = 20.0

# 耗时估算（进度条/提示只用它，实际以结束为准）。★ 这两个数来自 2026-10-21 的实测，
# 不是拍的：60 秒人声轨（大帝 ra500fb92 + 索引，20 秒分片）墙钟 102.8 秒，
# 其中进程启动/加载约 48 秒，推理 55 秒 ⇒ 推理约 0.92 倍实时。
# 分片之前是 17 分钟量级（5 倍实时），那时写的 `×5 + 60` 现在会把进度条拖成
# "明明跑完了还在 40%"，所以跟着改。
RVC_EST_FIXED = 60          # 进程启动 + torch + 模型 + hubert + 索引读
RVC_EST_PER_SEC = 1.2       # 每 1 秒人声轨的墙钟秒数（留了余量，实测 0.92）


def rvc_est_seconds(voice_secs):
    """RVC 一步大概要多少秒 —— 两处估算（单角色 / 多角色）共用一份口径。"""
    try:
        s = float(voice_secs or 0)
    except (TypeError, ValueError):
        s = 0.0
    return int(RVC_EST_FIXED + max(0.0, s) * RVC_EST_PER_SEC)


# ---------------------------------------------------------------------------
# 显卡闸门：同一时刻只允许**一个**任务真占显卡（RVC 推理 / UVR 分离）。
#
# ★ 为什么必须有这道闸门（2026-10-22 真事故，不是推演）：
#   同一首歌（198 秒）被提交两次，两个 RVC 进程同时跑同一张 8G 卡 ——
#     · 显存 7789 / 8151 MiB（96%），日志里出现 `CUDA error: an illegal memory access`
#       （+12 半音那次体检直接崩了，退出码 1）；
#     · 两个进程工作集 5.7G + 7.0G，把 31.6G 物理内存吃到只剩 0.2G ⇒ 整机进 swap；
#     · 结果：按 RVC_EST 本该 ~5 分钟（298 秒）的一步，跑了 26 分钟还没出来。
#   串行之后各跑各的，总时长**更短**，而且不会再出现 illegal memory access。
#
# 闸门只卡"真占显卡的那一段"，不阻塞任务创建，也不影响任何纯 CPU 步骤；
# 排队期间任务可照常取消（取消时立刻抛 Cancelled，不占着队列）。
GPU_GATE_LOCK = threading.RLock()    # 只保护下面这三个变量
_GPU_GATE = threading.RLock()        # 真正的闸门：拿到它才准碰显卡
#   （用 RLock 不用 Lock：万一将来某个占卡步骤里又套了一层占卡调用，
#    Lock 会把自己锁死；RLock 允许同一线程重入，跨线程仍然是互斥的。）
_GPU_HOLDER = {"jid": None, "at": 0.0, "what": ""}
_GPU_QUEUE = []                      # 正在等闸门的 jid，先进先出


def gpu_gate_stats():
    """给界面 / 日志看的：现在谁在占卡、几个人在排队。"""
    with GPU_GATE_LOCK:
        return {"holder": _GPU_HOLDER.get("jid"),
                "what": _GPU_HOLDER.get("what") or "",
                "since": _GPU_HOLDER.get("at") or 0.0,
                "waiting": list(_GPU_QUEUE)}


class _GpuSlot(object):
    """`with gpu_slot(job, "歌声转换（RVC）"):` —— 进不去就排队等（可取消）。

    等待期间把任务写成"排队等显卡"并写明前面还有几个：界面上要是只转圈
    不说话，用户就会以为又卡死、再点一次，于是又多一个抢卡的 —— 这是
    这次事故的自我强化环节，必须在提示里掐断。
    """

    def __init__(self, job, what):
        self.job = job
        self.what = what
        self.got = False

    def __enter__(self):
        job, jid = self.job, (self.job or {}).get("id")
        with GPU_GATE_LOCK:
            if jid not in _GPU_QUEUE:
                _GPU_QUEUE.append(jid)
        try:
            while True:
                if _GPU_GATE.acquire(False):
                    self.got = True
                    break
                if job is not None and job_cancelled(job):
                    raise Cancelled()
                if job is not None:
                    with GPU_GATE_LOCK:
                        pos = _GPU_QUEUE.index(jid) if jid in _GPU_QUEUE else 0
                        holder = _GPU_HOLDER.get("what") or "另一个任务"
                    job["stage"] = "排队等显卡"
                    # 位置说成人话："第 1 个在等的" = 下一个就是你；
                    # 光说"排在第 1 位"会被理解成"正在跑的是我"。
                    job["detail"] = ("显卡正被另一个任务占着（%s），你是第 %d 个在等的"
                                     "（前面还有 %d 个）。两个任务抢同一张卡会**一起**变慢"
                                     "（实测 5 倍以上，还会炸 CUDA），排队反而更快 —— "
                                     "轮到你会自动开始。" % (holder, pos + 1, pos))
                    step_hint(job, "等显卡：前面还有 %d 个任务（排队比抢卡快，"
                                   "实测抢卡慢 5 倍）" % pos)
                time.sleep(0.5)
        finally:
            with GPU_GATE_LOCK:
                if jid in _GPU_QUEUE:
                    _GPU_QUEUE.remove(jid)
        with GPU_GATE_LOCK:
            _GPU_HOLDER.update({"jid": jid, "at": time.time(), "what": self.what})
        return self

    def __exit__(self, *exc):
        if self.got:
            _GPU_GATE.release()
            with GPU_GATE_LOCK:
                _GPU_HOLDER.update({"jid": None, "at": 0.0, "what": ""})
        return False


def gpu_slot(job, what):
    """占显卡的步骤统一走这里拿名额。见上面的事故说明。"""
    return _GpuSlot(job, what)

# 非歌声段保护：口哨 / 纯音被 RVC 当"唱"会变成高两个八度的吼（实测见
# stages/nonvocal_guard.py 的文件头）。三种模式，默认保留原声。
NONVOCAL_MODES = ("keep", "mute", "off")


def _rvc_quality_args(rms_mix_rate=None, protect=None):
    """拼 RVC 推理的音质参数。

    ★ 为什么要显式传：这两个参数**不传就是用 RVC 自己的默认值**，而以前一个
    都没传，等于把音质交给了别人的默认值：
      --rms-mix-rate 默认 **1.0** —— 输出的响度包络 100% 抄输入音频；RVC 官方
        WebUI 的默认是 0.25。对干净人声输入，1.0 会把原唱的气息/音量起伏
        整条搬过来，听感一顿一顿、句尾突然掉下去。0.25 让模型自己控响度。
      --protect 默认 0.33 —— **必须小于 0.5**：RVC 源码里是 `if protect < 0.5`
         才启用清辅音保护，取 0.50 等于**完全不保护**，清辅音会被模型"唱"过
         一遍，咬字发糊（实测 ASR 字错误率 70.5% vs 开启保护的 52~54%）。
         详见上面参数表的第 ③ 条。
    """
    r = RVC_RMS_MIX_RATE if rms_mix_rate is None else float(rms_mix_rate)
    p = RVC_PROTECT if protect is None else float(protect)
    return "--rms-mix-rate %.2f --protect %.2f" % (max(0.0, min(1.0, r)),
                                                   max(0.0, min(0.5, p)))


# ------------------------------------------------ 高频补偿（"闷"的解药）
#
# ★ 这一组默认值是**实测**出来的（2026-10-20，大帝模型 + 同一段 5 秒人声，
#   同一套参数 pitch 0 / rmvpe / index 0.50 / rms 0.25 / protect 0.33，
#   只改这一个变量；指标 = 99% 累积能量的截止频率、4~8k 频段能量占比、
#   谱质心，以及用 eres2net 的 sv 向量算的"距角色中心"余弦距离）：
#
#     版本            带宽99   4-8k    谱质心    音色距角色中心
#     原唱            11.5k   0.1498   1964Hz     0.4878   ← 本来就该不像
#     RVC 纯输出       5.7k   0.0178     435Hz     0.2022   ← 最像角色，也最闷
#     ＋补偿 fc=5500   9.5k   0.0303     670Hz     0.3015
#     ＋补偿 fc=4500   9.5k   0.0447     735Hz     0.3316
#     ＋补偿 fc=3200   9.4k   0.0590     829Hz     0.3637   ← 最清楚，也最不像
#
#   机理（这才是"AI 感太重 / 咬字糊"的物理来源，调参救不了）：
#     RVC 的声码器在 5.7kHz 以上几乎没有能量，而齿音（s/sh/ts）主要在 4~8kHz ——
#     输出里这一段的能量只剩原唱的 **1/10**。所以听起来"闷、糊、不像真人在唱"。
#     这不是 protect/index 能调的：那两个参数管的是音高和清辅音保护，
#     管不了"上面根本没有东西"。
#
#   手段 = 交叉合成（stages/seedvc_cli.py cross）：
#     fc 以下取 RVC（音色 / 共振峰），fc 以上取原唱（辅音 / 气声）。
#   代价写在表里：补得越狠越清楚，但原唱的气声也带进来，音色会离角色远一些。
#
#   ★★ 2026-10-23 重测（tools/probe_bandwidth.py + probe_svdist.py，204 秒真歌）：
#     档位        8-12k 绝对电平   谱质心   声纹漂移（占"到原唱"距离）
#     关          +13.0dB         5398Hz   0%（基准）
#     轻 fc5500   +19.1dB         6109Hz   26%
#     中 fc4500   +19.5dB         6105Hz   42%
#     强 fc3200   +19.6dB         6029Hz   64%
#     原唱        +20.8dB         5484Hz   100%
#   ⇒ **「轻」是拐点**：拿到全部 7.8dB 潜力里的 6.1dB（78%），代价 26%。
#   ⇒ **「强」是纯亏**：只比「轻」多 0.5dB，漂移却翻 2.5 倍 —— 已撤掉这一档。
#   ⇒ 因此默认改成 **「轻」**：以前默认「关」等于把已经做好的能力关掉了。
#
#   ⚠ 上面那几个"音色距离"常数（0.3015/0.3316/0.3637）是新测之前留下的，
#     和这次实测的**口径不同**（那是"距角色中心"绝对值，这是"相对不补偿的漂移"），
#     别直接拿来互证 —— 以这次实测的三列为准。
#
#   ★★★ 2026-10-24 第三次重测（三首真歌 / 三个角色，**推翻了"固定档位"**）：
#
#     角色       成品采样率  8-12k缺口  模型
#     大帝 40k    40000     14.3dB     v1 老模型（声码器有效带宽只有 ~12k）
#     珍奇 48k    48000      8.7dB     v2
#     苹果嘉儿 48k 48000     -1.8dB     v2（本来就留住了高频，一点不缺）
#
#     ⇒ **缺口根本不在一个量级上**，因为它是**模型自己的采样率**决定的：
#       40k 老模型缺 14dB，48k 新模型缺 0dB。给 48k 模型做补偿是**纯亏** ——
#       高频一点没多，声纹反而被原唱的高频拉走（实测 fc5500 让"距角色"从
#       0.1855 涨到 0.2240，白付 11% 的漂移）。
#
#     另一件事：**分频点越高，代价越小**（同样补满 8-12k）：
#       档位       距角色   漂移占比（占"到原唱"距离）  4-8k 是否被动
#       不补       0.1855    0%（基准）                 —
#       fc=9000   0.1747   -3.1%（比不补**更像角色**）   没动（17.8→17.8）
#       fc=7000   0.1884   +0.8%                        没动
#       fc=5500   0.2240  +11.1%                        14.7→13.2（被动了）
#     ⇒ 声纹靠中低频共振峰，7k 以上根本不进那把尺子；把分频点往高处放，
#       就能"只补缺口、不碰音色"。旧的「轻 = fc5500」白白多付了 11%。
#
#   ⇒ 所以档位从"固定三档"改成 **auto 为默认**：先量缺口，不缺就不补，
#     缺就从最保守的分频点（9000）往下试，够用就停。实现在 stages/hf_cli.py。
HF_FREQ = {"off": 0, "auto": 0, "light": 5500, "mid": 4500}
HF_LEVELS = ("auto", "off", "light", "mid")
HF_LABEL = {
    "auto": "自动 —— 先量缺口再补，不缺就不补（默认）",
    "off": "关 —— 一律不补，最像角色但老模型会闷",
    "light": "轻 —— 固定 5500Hz，老模型够用但声纹多漂 11%",
    "mid": "中 —— 固定 4500Hz，补偿更足、声纹漂得更多",
}


def hf_brighten(job, rvc_wav, src_vocal, out_wav, level):
    """把 RVC 干声的高频补回来。失败就原样返回（绝不拖死整首歌）。

    为什么用"原唱的高频"而不是自己合成：RVC 在 5.7k 以上没有可信内容，
    谐波外推出来的是"电子味"；而原唱那条轨的高频是**真的**辅音和气声 ——
    只是它带着原唱音色，所以必须限幅（只取 fc 以上那一段），并且给用户开关。

    auto 档（默认）：交给 stages/hf_cli.py —— 它先量缺口，**不缺就不补**，
    缺就从最保守的分频点往下试，并且补完再量一次（闭环）。
    固定档（off/light/mid）保持老行为，方便对比和手动兜底。
    """
    if level == "auto":
        return _hf_auto(job, rvc_wav, src_vocal, out_wav)
    fc = HF_FREQ.get(level) or 0
    if not fc:
        return rvc_wav, None
    if not src_vocal or not os.path.isfile(src_vocal):
        log(job, "  [warn] 找不到原始人声轨，跳过高频补偿")
        return rvc_wav, None
    rc, txt = run(job, '"%s" -s "%s" cross "%s" "%s" "%s" %d 1200'
                  % (RVC_PY, os.path.join(STAGES, "seedvc_cli.py"),
                     rvc_wav, src_vocal, out_wav, fc),
                  cwd=RVC_ROOT, timeout=1800)
    if rc != 0 or not os.path.isfile(out_wav):
        log(job, "  [warn] 高频补偿失败（%s），用未补偿的干声继续" % _tail(txt, 2))
        return rvc_wav, None
    return out_wav, {"level": level, "fc": fc}


def _hf_auto(job, rvc_wav, src_vocal, out_wav):
    """自动档：量缺口 → 按缺口补 → 补完再量一次（三步都在子进程里）。

    ★ 为什么"补完再量"这一步不能省：这是本项目反复栽过的地方 ——
      按预测改完不看结果，等于蒙着眼开车（别处就有"测出来 15.04ms、
      三频带一致，照着挪过去反而掉 5 个点"的先例）。这里补完复测是
      唯一能证明"确实补到了"的手段，跳过的数字不能算数。
    """
    if not src_vocal or not os.path.isfile(src_vocal):
        log(job, "  [warn] 找不到原始人声轨，跳过高频补偿")
        return rvc_wav, None
    rep = os.path.join(os.path.dirname(out_wav) or ".", "_hfauto.json")
    rc, txt = run(job, '"%s" -s "%s" auto "%s" "%s" "%s" "%s"'
                  % (RVC_PY, os.path.join(STAGES, "hf_cli.py"),
                     rvc_wav, src_vocal, out_wav, rep),
                  cwd=RVC_ROOT, timeout=1800)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(rep)
    # ★ 关键：没补（HF_SKIP）时 hf_cli **不会写那个 wav**。这时不能拿
    #   out_wav 去当成品 —— 它压根不存在，混音会读空文件。
    if rc != 0 or not os.path.isfile(out_wav):
        if info is not None and not info.get("applied"):
            log(job, "  高频自动档：%s" % (info.get("reason") or "不需要补"))
        else:
            log(job, "  [warn] 高频补偿失败（%s），用未补偿的干声继续" % _tail(txt, 2))
        return rvc_wav, None
    if not info:
        info = {}
    fc = int(info.get("fc") or 0)
    log(job, "  高频自动档：缺口 %.1f dB → %.1f dB（分频 %s Hz），"
             "谱质心恢复到原唱的 %.0f%%"
        % (info.get("deficit_db") or 0.0,
           info.get("after_deficit_db") or 0.0,
           "{:,}".format(fc) if fc else "?",
           100.0 * (info.get("after") or {}).get("cent_ratio", 0.0)))
    return out_wav, {"level": "auto", "fc": fc, "auto": info}


# ------------------------------------------------ 去金属声（"明显电音"的修复）
#
# ★ 定位：上一轮做的是「体检时告诉你这一档会电」，但那只是**告警**，
#   真唱出来照样电 —— 老板第二次反馈「还是有电音」就是这么来的。
#   这一节补上"修"：**只在判电时动手，修完复测**，实现在 stages/demetal_cli.py。
#
# 实测基线（同一首歌、同一模型、只改音高；REF = 原唱真人声）：
#       REF           HNR 11.42   4-8k 占比 0.00393   jitter 0.01126
#       RVC +0        HNR 16.79   4-8k ×1.87          jitter 0.01127
#       RVC +12       HNR 21.93   4-8k ×4.16          jitter 0.00667
#   ⇒ 成品的 jitter / shimmer 全都**低于真人**（比真人还规整），这就是"AI 感"
#     的物理形态；而变调会把 HNR 和 4-8k 一起推上去。
#
# 两个手段，各管一条判据（谁也替不了谁，见 demetal_cli.py 文件头）：
#     手段              ΔHNR           4-8k 倍数        代价
#     只削 4-8k −4dB   +10.51→+11.25  ×4.16→×1.68    质心 −5%
#       ↑ 削高频会让 HNR **反升**：剩下的谐波更干净。所以它救不了 HNR 那条。
#     只抖动 0.006      +10.51→+3.24   ×3.85（不动）   jitter 抬到真人 1.27 倍
#     0.006 + −4dB      →+3.97 ✓       →×1.56 ✓       2-4k 基本不动（−0.35dB）
#
# 被数据否掉、不要再走的路（都记在 demetal_cli.py 文件头）：
#   掺白噪声压 HNR（要 35%、HNR 掉到 6.4，等于沙沙响）；
#   外部相位声码器变调（4-8k 反而 ×5.61，比 RVC 更糟）；
#   换 f0 提取器 pm/fcpe（三档 jitter 全在 0.0062~0.0069，是声码器固有，
#   不是 f0 曲线的锅）。
DEMETAL_MODES = ("auto", "off", "force")
DEMETAL_LABEL = {
    "auto": "自动 —— 先量电不电，电才修（默认）",
    "off": "关 —— 一律不修",
    "force": "强制 —— 不管判据，一律按固定剂量修（做 A/B 对比用）",
}


def demetal(job, voice_wav, src_vocal, out_wav, mode="auto"):
    """把干声里的金属声（电音）修掉。失败就原样返回（绝不拖死整首歌）。

    auto（默认）：交给 stages/demetal_cli.py —— 先量，不电就不动，
    电就从最小剂量往上试，达标就停，修完再量一次（闭环）。
    force：跳过判定，按固定剂量修 —— 只用于做 A/B 对比（老板要先听一版）。
    """
    if mode not in DEMETAL_MODES:
        mode = "auto"
    if mode == "off":
        return voice_wav, None
    if not src_vocal or not os.path.isfile(src_vocal):
        log(job, "  [warn] 找不到原始人声轨，跳过去金属声")
        return voice_wav, None
    rep = os.path.join(os.path.dirname(out_wav) or ".", "_demetal.json")
    args = ('--hnr-db %s --hf-rel %s'
            % (AUDITION_METAL_HNR_DB, AUDITION_METAL_HF_REL))
    if mode == "force":
        # ★ 判据阈值由 studio 下发到子进程，保证"只有一处定义"；
        #   force 是给"老板的耳朵听到了电、但尺子判不出来"准备的 —— 它只跳过
        #   「判不判电」这一步，副作用护栏在子进程里照旧生效。
        # ★ 别给参数加单引号：Windows 的 cmd 不认单引号，它会原样传给 python，
        #   于是 argv 里是 '0.006,-4'，float() 直接抛错（值里没有空格，不必引号）。
        # ★★ 2026-10-24 第五轮：以前这里还会把剂量一并钉死（--steps 0,0,0,1.0）。
        #    实测**不同素材的安全植入量能差 2.5 倍**，钉死的数在有些歌上直接
        #    压过头（合成素材 0.15 ⇒ ΔHNR −8.71，一档都不可用）。
        #    ⇒ 不再钉剂量，让它自己从最轻那档往上试。手动指定剂量是
        #      demetal_cli 自己的 --steps，不该由这里越权替它决定。
        args += " --force"
    rc, txt = run(job, '"%s" -s "%s" auto "%s" "%s" "%s" "%s" %s'
                  % (RVC_PY, os.path.join(STAGES, "demetal_cli.py"),
                     voice_wav, src_vocal, out_wav, rep, args),
                  cwd=RVC_ROOT, timeout=1800)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(rep)
    if rc != 0 or not os.path.isfile(out_wav):
        # ★ 没修（SKIP）时子进程**不会写那个 wav**。这时绝不能拿 out_wav 当成品
        #   —— 它压根不存在，混音会读空文件（这个坑在高频自动档踩过一次）。
        if info is not None and not info.get("applied"):
            log(job, "  去金属声：%s" % (info.get("reason") or "没修"))
        else:
            log(job, "  [warn] 去金属声没跑通（退出码 %d），用未处理的干声继续"
                % rc)
        return voice_wav, None
    if not info:
        info = {}
    return out_wav, {"mode": mode, "auto": info}


def presence(job, voice_wav, src_vocal, out_wav, mode="auto"):
    """把干声缺的「临场感」（2~5kHz）补回来。失败就原样返回（绝不拖死整首歌）。

    ★ 为什么单独有这一步（2026-10-25 第六轮实测）：
      139 条角色成品 vs 原唱干声，2~5kHz 相对中频**中位缺 3.9dB**，是系统性的，
      不是某一首的偶然。Sonarworks 把这条列为 AI 人声的固有毛病（"AI vocals
      often lack forward presence"）。补回来的实测收获：
        psim（周期相似度）0.872 → 0.8443，而**原唱是 0.8448** —— 正好落在真人位置
        HNR 17.27 → 16.06（原唱 13.73）｜shimmer / hf_ratio 也都朝原唱靠
        jitter 基本不动 ⇒ 不会变沙哑

    ★★ 为什么**只补不削**（另一条"800~1.2k 箱音凸出 +2.8dB"没做）：
      那一带是共振峰 —— "像不像这个角色"的载体，削多了会离角色更远；而且它是
      宽带能量分布差异、不是窄峰，窄带 EQ 削不动（实测减 4dB 平均只掉 1.2dB）；
      拆分实验里削它只多给一点点收益，却把 psim 压到**低于**原唱（补过了）。
      ⇒ 只补不削是数据支撑的选择，不是偷懒。详见 stages/presence_cli.py 文件头。

    ★ 位置：必须在 demetal **之后**。demetal 会削"造出来的假高频"，先补再削
      等于白补（这个顺序坑在第五轮"过冲修正 vs 补气声"上栽过一次）。
    """
    if mode not in ("auto", "off"):
        mode = "auto"
    if mode == "off":
        return voice_wav, None
    if not src_vocal or not os.path.isfile(src_vocal):
        log(job, "  [warn] 找不到原始人声轨，跳过去临场感补偿")
        return voice_wav, None
    rep = os.path.join(os.path.dirname(out_wav) or ".", "_presence.json")
    rc, txt = run(job, '"%s" -s "%s" auto "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "presence_cli.py"),
                     voice_wav, src_vocal, out_wav, rep),
                  timeout=900)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(rep)
    if rc != 0 or not os.path.isfile(out_wav):
        # ★ 与 hf / demetal 同一个约定：没修（SKIP）时子进程**不写 wav**，
        #   这时拿 out_wav 当成品会读空文件（这个坑踩过）。
        if info is not None and not info.get("applied"):
            log(job, "  临场感：%s" % (info.get("reason") or "没补"))
        else:
            log(job, "  [warn] 临场感补偿没跑通（退出码 %d），用未处理的干声继续"
                % rc)
        return voice_wav, None
    if not info:
        info = {}
    return out_wav, {"mode": mode, "auto": info}


# ------------------------------------------------ 转换噪声补偿（3~10kHz）
#
# 为什么单独一步、而不是再加一条固定 shelf：项目自己的三份报告指向同一件事，
# 而它们是**两个相反的方向** —— 4~8k 平坦度 0.228→0.346（噪声化）、8~12k 只剩
# 原唱的 5.3%、12~16k≈0（缺空气感）。同一条 EQ 不可能同时「收噪声」和「补空气」，
# 所以这里量的是**原唱 vs 成品**的长期平均频谱差，逐 1/3 倍频程给一条平滑曲线：
# 比原唱高的带收（封顶 6dB）、比原唱低很多的带小幅补（封顶 3dB）。
#
# 实测（2026-10-27，珍奇唱 Welcome To New York，同一首同一变调）：
#   RVC 原始输出  各带平均差 10.44 dB（比原唱**低**，说明这首主要是缺高频）
#   → 高频补偿 6.88 → 去金属 6.65 → 临场感 4.97 → 唱后微调 3.09
#   → **这一步 3.09 → 2.19，最大剂量只有 2.39 dB**
# ⇒ 已有链路把大头修掉了，这一步只是「最后那条形状曲线」，剂量必须小。
#
# 位置：唱后微调**之后**、非歌声段保护之前。理由：deess/deharsh 还会动 4~10k，
#   放在它前面会被覆盖；非歌声段保护只换时间轴上的段，与频谱整形正交。
LTAS_MODES = ("auto", "off")
LTAS_MEND_MODE = "auto"


def ltas_mend(job, voice_wav, src_vocal, out_wav, mode="auto"):
    """按原唱的长期平均频谱，把 3~10kHz 的形状差收一收。失败就原样返回。

    与 hf / demetal / presence 同一个约定：SKIP 时子进程**不写 wav**，
    调用方不许拿 out_wav 当成品（这个坑踩过）。
    """
    if mode not in LTAS_MODES:
        mode = "auto"
    if mode == "off":
        return voice_wav, None
    if not src_vocal or not os.path.isfile(src_vocal):
        log(job, "  [warn] 找不到原始人声轨，跳过转换噪声补偿")
        return voice_wav, None
    rep = os.path.join(os.path.dirname(out_wav) or ".", "_ltas.json")
    rc, txt = run(job, '"%s" -s "%s" auto "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "ltas_mend.py"),
                     voice_wav, src_vocal, out_wav, rep),
                  timeout=1200)
    info = None
    with contextlib.suppress(Exception):
        with open(rep, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(rep)
    if rc != 0 or not os.path.isfile(out_wav):
        if info is not None and not info.get("applied"):
            log(job, "  转换噪声补偿：%s" % (info.get("reason") or "没修"))
        else:
            log(job, "  [warn] 转换噪声补偿没跑通（退出码 %d），用上一步的干声继续"
                % rc)
        return voice_wav, None
    if not info:
        info = {}
    return out_wav, {"mode": mode, "auto": info}


# ------------------------------------------------ 非歌声段保护（"吹口哨就吼"的正解）
#
# 机制（实测，不是推测）：RVC 认不出"口哨"和"唱"的区别 —— 分离后的人声轨里，
# 口哨是一条极高极纯的持续音（实测神耀：f0 662Hz、谱平坦度 0.0013，正常歌唱段
# 是 f0 262Hz、平坦度 0.19，差一个数量级）。RVC 把它当"唱"，用角色嗓音重发出来：
# 男声角色把 662Hz 降 19 半音得到 220Hz —— 用假声持续吼。这一段调参救不了。
# 所以做法是：把这些段判出来，**不用 RVC 的输出，换回原始人声轨**。

def guard_nonvocal(job, src_vocal, rvc_voice, out_wav, mode="keep"):
    """把 RVC 输出里的「非歌声段」（口哨/纯音）换回原始人声。

    返回 (要用的 wav 路径, 说明 dict 或 None)。跑不通时**不抛异常** ——
    退回 RVC 原样输出（音质差一点，但不能把整条链带死）。
    """
    if mode not in NONVOCAL_MODES:
        mode = "keep"
    if mode == "off":
        return rvc_voice, None
    report = os.path.join(os.path.dirname(out_wav) or ".", "_nonvocal.json")
    rc, out = run(job,
                  '"%s" -s "%s" --src "%s" --rvc "%s" --out "%s" --mode %s --report "%s"'
                  % (RVC_PY, os.path.join(STAGES, "nonvocal_guard.py"),
                     src_vocal, rvc_voice, out_wav, mode, report),
                  cwd=RVC_ROOT, timeout=1800)
    if rc != 0 or not os.path.exists(out_wav):
        log(job, "  [warn] 非歌声段保护没跑通（退出码 %d），按 RVC 原样输出：%s"
            % (rc, _tail(out, 3)))
        return rvc_voice, None
    info = None
    with contextlib.suppress(Exception):
        with open(report, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(report)
    for line in (out or "").splitlines():
        if (line.strip().startswith("全曲谱平坦度")
                or line.strip().startswith("判定为非歌声")
                # ★ 这行是"少声音"修复的核对量：只按平坦度会圈多少、纯度判据筛掉多少。
                #   留在任务日志里，老板重跑时能看到这次到底有没有误伤。
                or line.strip().startswith("只按谱平坦度")):
            log(job, "  " + line.strip())
    return out_wav, info


# ------------------------------------------------ 非主唱段保护（伴唱 / 和声不许被唱）
#
# 用户 2026-10-27 报的问题：原曲里有些段本来不是主唱唱的（伴唱/和声/垫唱），
# 但分离出来的人声轨把它们和主唱放在一条轨里，RVC 分不出谁在唱 ⇒ 全都被角色
# 唱了一遍，叠起来就是"很多人一起唱、发闷发哑"。
#
# 判据与三道闸门写在 stages/lead_guard.py 文件头：**相对这首歌自己的主唱基线**
# （立体声宽度 + L/R 相关性），而不是绝对阈值 —— 实测同一绝对阈值下有的歌圈 0.3%、
# 有的歌圈 60%，那种东西一默认开启就会把主唱静音。
LEAD_GUARD_MODE = "auto"          # auto(=keep) / keep / mute / off


def guard_lead(job, src_vocal, rvc_voice, out_wav, mode="auto"):
    """把原曲里「不是主唱唱」的段换回原始人声。跑不通就原样返回，绝不动主唱。"""
    if mode in (None, "", "auto"):
        mode = "keep"
    if mode not in ("keep", "mute", "off"):
        mode = "keep"
    if mode == "off":
        return rvc_voice, None
    report = os.path.join(os.path.dirname(out_wav) or ".", "_lead.json")
    rc, out = run(job,
                  '"%s" -s "%s" --src "%s" --rvc "%s" --out "%s" --mode %s --report "%s"'
                  % (PY, os.path.join(STAGES, "lead_guard.py"),
                     src_vocal, rvc_voice, out_wav, mode, report),
                  cwd=HERE, timeout=1800)
    info = None
    with contextlib.suppress(Exception):
        with open(report, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(report)
    if rc != 0 or not os.path.exists(out_wav):
        log(job, "  [warn] 非主唱段保护没跑通（退出码 %d），按上一步输出继续：%s"
            % (rc, _tail(out, 3)))
        return rvc_voice, None
    return out_wav, info


# ------------------------------------------------ 口哨专用保护（高基频 + 无谐波 + 稳定 + 短段）
#
# 用户 2026-10-28：「口哨等等还在被唱」。查过 nonvocal_guard 之后确认**不能放宽它**：
# 那 21 秒"低平坦度"候选拿去量，全是干净高音延长（f0 中位 327 Hz、0% 的帧 >500 Hz、
# 抖动和正常唱歌一样）—— 放宽就是"少声音"事故重演。
# 口哨要独立证据：基频 ≥550 Hz + **几乎无二次谐波** + 极稳 + 短段，见
# stages/whistle_guard.py 文件头（四条判据 + 三条安全闸门）。
WHISTLE_GUARD_MODE = "auto"       # auto(=keep) / keep / mute / off


def guard_whistle(job, src_vocal, rvc_voice, out_wav, mode="auto"):
    """把口哨这类"不是唱"的高频纯音换回原始人声。跑不通就原样返回。"""
    if mode in (None, "", "auto"):
        mode = "keep"
    if mode not in ("keep", "mute", "off"):
        mode = "keep"
    if mode == "off":
        return rvc_voice, None
    report = os.path.join(os.path.dirname(out_wav) or ".", "_whistle.json")
    rc, out = run(job,
                  '"%s" -s "%s" --src "%s" --rvc "%s" --out "%s" --mode %s --report "%s"'
                  % (PY, os.path.join(STAGES, "whistle_guard.py"),
                     src_vocal, rvc_voice, out_wav, mode, report),
                  cwd=HERE, timeout=1800)
    info = None
    with contextlib.suppress(Exception):
        with open(report, encoding="utf-8") as f:
            info = json.load(f)
    with contextlib.suppress(Exception):
        os.remove(report)
    if rc != 0 or not os.path.exists(out_wav):
        # ★ 与 hf/presence 同一个约定：SKIP 时**不写 wav**，这不是故障。
        #   以前这里一律打 [warn] 没跑通 —— 用户看到"没跑通"会以为功能坏了，
        #   其实只是这首歌没有口哨段（2026-10-28 实测日志里就是这么误导的）。
        if info is not None and info.get("ok") and not info.get("protected_secs"):
            log(job, "  口哨段：%s"
                % (info.get("reject") or "没找到口哨段（正常结果）"))
            return rvc_voice, info
        log(job, "  [warn] 口哨专用保护没跑通（退出码 %d），按上一步输出继续：%s"
            % (rc, _tail(out, 3)))
        return rvc_voice, None
    return out_wav, info


# ------------------------------------------------ 成品客观质检（把"没法听"变成数）
#
# 为什么值得单独做一步：以前"唱得好不好"只能靠耳朵，出了问题也说不出是哪一种。
# 这一步用 RMVPE（和推理链同一个 f0 提取器）把**原唱人声轨**和**角色变声后的
# 人声轨**逐帧比基频，给出四个能对账的数：
#   shift_semitones  实际整体平移了几个半音 —— 跟设定的 pitch 对得上吗？
#   match_ratio      对齐后音高落在同一个音上的比例（±50 音分内）
#   contour_corr     旋律轮廓的相关性（先在 ±1.5 秒里找最像的位置再比）
#   spread_ratio     角色人声的音高起伏 ÷ 原唱的起伏 —— ≈1 忠实地跟；≈0 唱成平线
# spread_ratio 这一项是**专门补 match_ratio 的盲**：一条完全不动的平线，
# 去掉整体偏移后每帧偏差都是 0，吻合率反而 100%，看着完美。
#
# 判据本身（够不够好）不放在 stage 里，而是放在这里写成人话 —— stage 只负责量，
# 这样在验收里可以放心地对"量出来的数"下断言，而不用去猜 stage 的判断逻辑。

def qc_singing(job, src_vocal, dst_voice, expect_pitch=None, start=None, secs=None,
               skip=None):
    """跑一次客观质检，返回五元组或 None（跑不通不抛异常，不能把整条链带死）。

    skip 是**绝对时间**的 [(起, 止), ...] 秒区间：被非歌声段保护换回原声的那几段
    要排除掉 —— 它们故意没变调，算进音准会得出"整首歌都跑调"的假结论
    （实测不排除时吻合 58.9%/偏差 IQR 1905 音分，排除后才是真实水平）。
    """
    tag = "qc_%s.json" % time.strftime("%m%d_%H%M%S")
    out_json = os.path.join((job or {}).get("dir") or DATA, tag)
    cmd = '"%s" -s "%s" "%s" "%s" "%s"' % (
        RVC_PY, os.path.join(STAGES, "qc_cli.py"), src_vocal, dst_voice, out_json)
    # ★ 试听小样那条路 start 就是 0.0 —— 原先写的是 `if start:`，0.0 为假，
    #   于是窗口参数被整个跳过，后面拼上来的"跳过段"就顶到了 argv[4]，
    #   报的是 "could not convert string to float: '16.27-17.70,...'"
    #   （看着像段格式错了，其实是有没有传窗口）。所以这里必须判 `is not None`。
    has_win = (start is not None and secs)
    if has_win:
        cmd += " %.3f %.3f" % (float(start), float(secs))
    if skip and has_win:
        st, en = float(start), float(start) + float(secs)
        parts = []
        for a, b in skip:
            a2, b2 = max(st, float(a)), min(en, float(b))
            if b2 - a2 > 0.05:
                parts.append("%.3f-%.3f" % (a2 - st, b2 - st))
        if parts:
            cmd += ' "%s"' % ",".join(parts)
    rc, _out = run(job, cmd, cwd=RVC_ROOT, timeout=1800)
    if rc != 0 or not os.path.isfile(out_json):
        return None
    try:
        with open(out_json, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


QC_WHOLE_WIN = 30.0     # 整曲质检每窗长度（和单窗质检同一口径，好对照）
QC_WHOLE_MAX = 8        # 最多几窗 —— 歌再长也不让质检时间无限涨
# ★ 为什么还要一条"整曲"质检（单窗那条已经跑了为什么不就完了）：
#   单窗那条取的是 `_qs = (总长-30)*0.35` 固定位置的一段。而八度错在**同一首歌内部**
#   变化极大 —— 实测登阶那一版（201 秒）按 30 秒窗口**等距扫七窗**：
#       窗口起点   0s    28s    57s    85s   114s   142s   171s
#       八度错   12.7%   8.2%  14.7%  16.1%   3.5%   5.6%  16.9%
#   同一份成品，报 3.5% 也对、报 16.9% 也对 —— **报出来的数取决于取到哪一段**。
#   给用户看一个抽签出来的数，比不给更糟：他会照着这个数决定要不要重唱。
#   ⇒ 整曲多窗：把整首铺满（等距铺开、含头含尾），再聚合成一个数，并单独报出
#     「最差那一窗」—— 听感由最差的那段决定，不是平均值。


def _whole_windows(total, win=QC_WHOLE_WIN, max_win=QC_WHOLE_MAX):
    """把 [0, total] 铺满：返回 [(起点, 长度)]，等距铺开且**含头含尾**。"""
    if total <= win or max_win <= 1:
        return [(0.0, total)]
    # ceil(total/win) 用整数算术写，避免为这一处去 import math
    n = int(total // win) + (1 if (total % win) > 1e-6 else 0)
    n = min(int(max_win), max(2, n))
    step = (total - win) / float(n - 1)
    return [(round(i * step, 3), win) for i in range(n)]


def qc_singing_whole(job, src_vocal, dst_voice, win=QC_WHOLE_WIN,
                     max_win=QC_WHOLE_MAX, skip=None):
    """整曲多窗质检：铺满整首逐窗量，聚合成一个能代表"整首"的数。

    聚合口径（每条都写清为什么）：
      · 八度错 / 宽松吻合 / 起伏比 —— 按**有效帧数加权**平均（唱得多的窗更代表整首）
      · 轮廓相关 —— 各窗**简单平均**（相关系数不是比例，不能加权）
      · 另报 worst_octave / worst_at —— **最差那一窗**。听感由最差那段决定：
        整首平均 12% 但有一段 21%，人耳记住的是那段。
    任何一窗失败都不影响其余 —— 拿不到就不聚合它，全拿不到才返回 None。
    返回 dict 或 None（跑不通不抛异常，不能把整条链带死）。
    """
    total = wav_seconds(src_vocal) or 0.0
    if total < 3.0:
        return None
    per = []
    for (s0, sec) in _whole_windows(total, win, max_win):
        d = qc_singing(job, src_vocal, dst_voice, start=s0, secs=min(sec, total - s0),
                       skip=skip)
        if d:
            d["_at"] = round(s0, 1)
            per.append(d)
    if not per:
        return None
    tot_f = sum(int(d.get("aligned_frames") or 0) for d in per)

    def wavg(k):
        if tot_f <= 0:
            vals = [d.get(k) for d in per if d.get(k) is not None]
            return (sum(vals) / len(vals)) if vals else None
        num = sum(float(d.get(k) or 0.0) * int(d.get("aligned_frames") or 0) for d in per)
        return num / tot_f

    def avg(k):
        vals = [d.get(k) for d in per if d.get(k) is not None]
        return (sum(vals) / len(vals)) if vals else None

    worst = max(per, key=lambda d: float(d.get("octave_err_ratio") or 0.0))
    return {
        "windows": len(per),
        "win_secs": round(win, 1),
        "tot_secs": round(total, 1),
        "match_ratio": wavg("match_ratio"),
        "match_ratio_loose": wavg("match_ratio_loose"),
        "octave_err_ratio": wavg("octave_err_ratio"),
        "spread_ratio": wavg("spread_ratio"),
        "contour_corr": avg("contour_corr"),
        # ★ 音高抖动（cents，1 个半音 = 100）—— 2026-10-17 加进来。
        #   它量的是"唱出来的音在自己该在的位置附近晃多厉害"，对应老板说的
        #   "唱歌越唱越差 / 音听着飘"：**八度错是偶尔翻八度，抖动是全程都在晃**，
        #   后者才是"不像在唱歌"的日常观感。之前这个数只躺在 qc_cli 的 JSON 里，
        #   返回值里没有 ⇒ 谁想用都得去翻日志（第一版门禁就因为读不到它假红过一次）。
        #   实测对照（大帝 + 30 秒人声，只换 f0 方法）：fcpe 31.7 / rmvpe 17.1。
        "dev_iqr_cents": avg("dev_iqr_cents"),
        "shift_semitones": avg("shift_semitones"),
        "worst_octave": float(worst.get("octave_err_ratio") or 0.0),
        "worst_at": worst.get("_at"),
        "worst_match": worst.get("match_ratio_loose"),
        "per_window": [{"at": d.get("_at"),
                        "octave_err_ratio": d.get("octave_err_ratio"),
                        "match_ratio_loose": d.get("match_ratio_loose"),
                        "contour_corr": d.get("contour_corr")} for d in per],
    }


def describe_qc_whole(w):
    """把整曲质检翻成人话，返回 (是否需要提醒, 一行摘要)。"""
    if not w:
        return False, ""
    oer = float(w.get("octave_err_ratio") or 0.0)
    mrl = float(w.get("match_ratio_loose") or 0.0)
    worst = float(w.get("worst_octave") or 0.0)
    line = ("整曲 %d 窗：音准吻合 %.1f%%、八度跳 %.1f%%、起伏比 %.2f（最差一窗 %.1f%% @%.0fs）"
            % (w.get("windows") or 0, mrl * 100, oer * 100,
               float(w.get("spread_ratio") or 0.0), worst * 100,
               float(w.get("worst_at") or 0.0)))
    warn = []
    if oer > 0.10:
        warn.append("整首有 %.1f%% 的帧在八度上跳（听感：飘、像两个人在唱）" % (oer * 100))
    if worst > 0.15:
        warn.append("最差的一段到 %.1f%%（第 %.0f 秒附近）"
                    % (worst * 100, float(w.get("worst_at") or 0.0)))
    if mrl < 0.75:
        warn.append("整首音准吻合只有 %.1f%%" % (mrl * 100))
    if warn:
        return True, (line + " —— " + "；".join(warn)
                      + "。可以换一档音高（相邻八度 / 0 半音）重唱一版对比")
    return False, line


def describe_qc(d, expect_pitch=None):
    """把质检的数翻成人话，返回 (是否通过, 给用户看的一行, 详细行列表)。

    ★ 阈值不是拍脑袋定的，是拿**负对照**量出来的（_probe_ruler_negctl.py，
    同一段人声做出已知真值的四个变体，实测）：
        dst = src 本身          宽松吻合 100.0%  轮廓相关 +1.000  八度错  0.0%
        dst = src 倒放          宽松吻合  13.0%  轮廓相关 -0.658  八度错 19.4%
        dst = src 按 1 秒乱序   宽松吻合  32.2%  轮廓相关 +0.112  八度错  8.2%
        dst = 后一半降 5 半音   宽松吻合  65.4%  轮廓相关 +0.758  八度错  7.1%
        真实的 RVC 输出          宽松吻合  95.6%  轮廓相关 +0.897  八度错  1.0%
    于是：宽松吻合 <0.70（会把"半对半错"判红、真实产物判绿）；
         轮廓相关 <0.50（把倒放/乱序判红）。
    ★ 为什么主判据用「宽松口径 ±150 音分」而不是 ±50：±50 在人声上**没有判别力**
    —— 把原唱精确变调（数学上偏差恒 0，理论上该读 100%）也只有 47.2%，因为颤音
    和滑音让逐帧基频本来就在动。用 ±50 会把好录制报成失败（实测误报过一次）。
    """
    if not d:
        return None, "客观质检没跑出来（不影响成品，成品仍可用）", []
    det = []
    mr = d.get("match_ratio") or 0.0
    mrl = d.get("match_ratio_loose")
    if mrl is None:
        mrl = mr                       # 兼容没有新字段的旧质检记录
    sp = d.get("spread_ratio") or 0.0
    cc = d.get("contour_corr") or 0.0
    oer = d.get("octave_err_ratio") or 0.0
    lag = d.get("lag_ms") or 0.0
    shift = d.get("shift_semitones") or 0.0

    det.append("音准吻合 %.1f%%（宽松口径 %.1f%%）、旋律轮廓相关 %+.2f、音高起伏比 %.2f、"
               "八度跳 %.1f%%、时间错位 %+.0fms"
               % (mr * 100, mrl * 100, cc, sp, oer * 100, lag))
    bad = []
    if abs(lag) > 250:
        bad.append("人声和伴奏差了 %.0fms（对齐有问题）" % lag)
    if oer > 0.15:
        bad.append("有 %.0f%% 的帧在八度上跳（旋律会突然翻高/翻低）" % (oer * 100))
    if sp < 0.5:
        bad.append("角色唱得太平：音高起伏只有原唱的 %.0f%%，旋律基本没跟"
                   % (sp * 100))
    elif mrl < 0.70:
        bad.append("音准吻合只有 %.0f%%，旋律跟得不稳" % (mrl * 100))
    if cc < 0.50:
        bad.append("旋律轮廓相关只有 %+.2f（跟走调/错位一个量级）" % cc)
    if expect_pitch is not None:
        want = float(expect_pitch)
        det.append("音高设定 %+d 半音，实测 %+.1f 半音" % (int(want), shift))
        if abs(shift - want) > 2.0:
            bad.append("音高设定没生效：设了 %+d，实测只有 %+.1f" % (int(want), shift))
    if (d.get("clip_ratio") or 0) > 0.001:
        bad.append("有 %.2f%% 的采样削幅（会破音）" % ((d.get("clip_ratio") or 0) * 100))
    if bad:
        return False, "客观质检发现问题：" + "；".join(bad), det
    return True, ("客观质检通过：音准吻合 %.0f%%（宽松口径 %.0f%%），音高起伏比 %.2f"
                  % (mr * 100, mrl * 100, sp)), det


def _role_ref_dir(rid):
    """角色的「音域参考」素材目录。

    优先 rvc_dataset（就是这个歌声模型的训练音源，音域口径最一致），
    退回手工切片 slices。
    """
    d = role_dir(rid)
    for sub in ("rvc_dataset", "slices"):
        p = os.path.join(d, sub)
        if os.path.isdir(p) and any(f.lower().endswith(".wav") for f in os.listdir(p)):
            return p
    return None


def estimate_pitch(job, vocal_wav, role):
    """量原唱与角色的基频中位数，返回 (半音, 依据文字)。

    拿不到就返回 (0, 原因) —— 宁可回到「不变调」这个保守默认，也不要瞎给一个
    数让整首歌报废。所以这里所有的失败路径都不抛异常。
    """
    rid = role.get("id")
    ref_dir = _role_ref_dir(rid)
    if not ref_dir:
        return 0, "这个角色没有可用的干声素材，先按不变调（0）来"
    d = role_dir(rid)
    out_json = os.path.join(d, "final", "_pitch_%d.json" % int(time.time()))
    os.makedirs(os.path.dirname(out_json), exist_ok=True)
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" 8'
                  % (RVC_PY, os.path.join(STAGES, "pitch_cli.py"),
                     vocal_wav, ref_dir, out_json), cwd=RVC_ROOT, timeout=900)
    data = None
    if rc == 0 and os.path.exists(out_json):
        try:
            data = json.load(open(out_json, encoding="utf-8"))
        except Exception:
            data = None
    with contextlib.suppress(Exception):
        os.remove(out_json)
    if not data:
        return 0, "音高估计没跑通（退出码 %d），先按不变调（0）来：%s" % (rc, _tail(out, 2))
    p = int(data.get("semitones", 0))
    why = ("原唱基频中位 %.0f Hz，角色 %.0f Hz ⇒ %+d 半音"
           % (data.get("src_med", 0), data.get("ref_med", 0), p))
    if abs(p) >= 10:
        why += "（差了大半个八度，通常说明原唱和角色的音域/性别不同）"
    if str(role.get("kind")) == "speech" and abs(p) >= 6:
        # 说话素材的基频中位数**低于**同一个人唱歌时的音域，所以按它算出来的
        # 降幅会偏大。这是方法本身的偏差，得说出来，不能让用户以为它是准的。
        why += "；角色参考是说话素材，唱歌音域通常更高，觉得闷就往正数再调 2~4"
    return p, why


def _find_rvc_index(model_name):
    """按 RVC 自己的规则找到这个模型能用的 added 索引库，返回路径或 None。

    以前这里是 `os.path.exists(assets/indices/<stem>.index)`，这个拼法**永远
    不成立**：train_index.py 产出的名字是
        <exp>_added_IVF<n>_Flat_nprobe_1_<exp>_v1.index
    而 stem 还是 `<exp>_e150_s1200` 之类的带轮数后缀，两边对不上。
    结果就是自训模型也被判定成「没有索引库」，index_rate 被白降级成 0，
    明明刚建好的索引一次没用上。

    RVC 推理侧 infer/vc/utils.py 的 get_index_path_from_model() 才是权威：
      - 先把模型名里的 `_e<轮>_s<步>` 尾巴去掉，剩下实验名
      - 跳过名字里带 trained 的（那是合并索引，不是 added）
      - 接受 `<exp>_added_*` / 含 `_<exp>_v1` / 含 `_<exp>_v2` / 名字里直接
        含完整模型名的
    这里照同样的口径实现一份，避免为一个纯查文件的判断去起 RVC 的 torch 运行时。
    """
    stem = os.path.splitext(os.path.basename(model_name or ""))[0]
    exp = re.sub(r"_e\d+_s\d+$", "", stem, flags=re.IGNORECASE)
    if not exp:
        return None
    root = os.path.join(RVC_ROOT, "assets", "indices")
    if not os.path.isdir(root):
        return None
    le, ls = exp.lower(), stem.lower()
    # ★ 归属判定（2026-10-20 修的真 bug）：索引名 `<模型>_added_...` 里
    #   「第一个 _added_ 之前」就是它服务的那个模型。原来的写法用
    #   `ls in low` / `_<exp>_v2` 这类"名字里出现过"的宽松判定 + 第一个命中就
    #   return，导致同一个角色下多试过几个模型之后：
    #       ra500fb92.pth（v1 / 256 维）配上了 ra500fb92_v2_klm48 的索引（768 维）
    #   ⇒ RVC 里 faiss 的 `assert d == self.d` 崩 ⇒ 整首歌"歌声转换失败"。
    #   RVC 自己的 get_index_path_from_model 有同一个毛病（它的 tie-break 是
    #   -mtime，最新的赢），所以这里必须比它更严，而且要和 stages/rvc_infer.py
    #   的 pick_index() 保持同一口径 —— 否则界面说"用索引"，推理那边却在降级。
    def _owner(name):
        s = os.path.splitext(name)[0].lower()
        i = s.find("_added_")
        return s[:i] if i > 0 else ""

    best, best_mt = None, -1.0
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(".index") or "trained" in name.lower():
                continue
            own = _owner(name)
            low = os.path.splitext(name)[0].lower()
            if own:
                if own not in (ls, le):
                    continue
            elif not (low.startswith(le + "_added_")
                      or ("_" + le + "_v1") in low
                      or ("_" + le + "_v2") in low):
                continue
            p = os.path.join(dirpath, name)
            try:
                mt = os.path.getmtime(p)
            except OSError:
                continue
            if mt > best_mt:
                best, best_mt = p, mt
    return best


def _ckpt_ep(f):
    try:
        return int(f.split("-e")[-1].split(".")[0])
    except Exception:
        return -1


def _ensure_ref(role, rid, d, job):
    """角色没有参考音频时挑一个并保存。返回 (ref_file, ref_text)。"""
    ref_file = (role.get("ref") or {}).get("file")
    ref_text = (role.get("ref") or {}).get("text", "")
    if not ref_file:
        p, t = pick_reference(role, os.path.join(d, "slices"))
        ref_file, ref_text = os.path.basename(p), t
        mutate_role(rid, lambda r: r.__setitem__("ref", {"file": ref_file, "text": ref_text}))
    return ref_file, ref_text


def verify_ckpt(job, rid, ckpt, ref_wav, ref_text, lang="zh"):
    """
    拿一个 ckpt 合成一句体检文本，过出声判据才算数。
    返回 (ok, info)：info 是合成结果（ok 时）或失败原因（不 ok 时）。
    """
    tasks = [{"text": VERIFY_TEXT,
              "out": os.path.join(job["dir"], "verify_%s.wav" % os.path.basename(ckpt))}]
    tj = os.path.join(job["dir"], "verify_tasks.json")
    rj = os.path.join(job["dir"], "verify_result.json")
    json.dump(tasks, open(tj, "w", encoding="utf-8"), ensure_ascii=False)

    # 必须先清旧结果：合成失败时它不会更新，读到的会是上一次的「合格」，
    # 坏权重就蒙混过关了（实测抓到过）
    if os.path.exists(rj):
        os.remove(rj)

    lang_name = {"zh": "中文", "ja": "日文", "en": "英文", "ko": "韩文", "yue": "粤语"}.get(lang, "中文")
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "tts_batch.py"), GSV_ROOT,
                     ckpt.replace("\\", "/"), S2G, ref_wav, ref_text.replace('"', ""),
                     lang_name, tj, rj), timeout=1800)
    if rc != 0:
        return False, {"error": "合成失败（退出码 %d）：%s" % (rc, (out or "")[-200:])}
    results = json.load(open(rj, encoding="utf-8")) if os.path.exists(rj) else []
    if not results:
        return False, {"error": "合成没产出结果（退出码 %d）" % rc}
    r = results[0]
    return bool(r.get("ok")), r


def tone_index_path(rid):
    return os.path.join(role_dir(rid), "tone_index.json")


# ---------------------------------------------------------------- 长台词
#
# 老板的反馈：「说话太长台词会乱掉」。根因和实测数据记在 tone.py 顶部（那里是
# 切分器所在）与 stages/tts_batch.py 顶部（那里管"多跑一次取更完整的那次"）。
# 这里只放两个数：
SPEAK_TAKES = 3        # 每段最多合成几次（第一次可疑才跑第二次，以此类推）
#   ★ 为什么不是"永远跑两次"：正常段第一次就合格，多跑纯属浪费；而可疑段
#     （秒/字掉到基准 85% 以下 = 有字没念出来）多跑一次能救回来一半以上
#     （实测同一段同一参考两次结果不同，换参考后 5 个里 2 个成功）。
#   ★ 为什么给到 3：门禁真跑时撞见过"换了参考还是丢"的段
#     （「哪怕只是一句话也好。我等了那么久，」两次都只念出前半句），
#     第三个参考能把这类的成功率再抬一截。代价只有那几段多跑 2 秒。
SPEAK_SPC_CALIB = 0.9  # 素材的"秒/字"要打折才等于合成产物的正常值
#   ★ 这个 0.9 是量出来的，不是拍的：阿塔尼斯 212 段素材的中位是 0.271 秒/字，
#     而同一角色合成出来的正常段是 0.20~0.27（中位 0.24）—— 产物比素材快约 11%。
#     不校正的话，"正常段"会被系统性地误判成"偏短"，每段都白跑一次第二次合成。


def _base_spc_from_role(role):
    """这个角色「一个字念多久」的粗基准 —— 只用元数据，不碰音频、不依赖语气索引。

    为什么需要它：判断"这一段是不是被吞了字"唯一的廉价信号就是**时长**。
    没有基准的话，0.19 秒/字到底是"念得快"还是"少了六个字"根本分不清。
    """
    v = []
    for s in role.get("segments") or []:
        n = len([c for c in (s.get("text") or "") if c.strip()])
        dur = float(s.get("dur") or 0)
        if n < 6 or not (1.0 <= dur <= 14.0):
            continue
        spc = dur / n
        if 0.12 <= spc <= 0.7:
            v.append(spc)
    if not v:
        return 0.0
    v.sort()
    # 返回**素材**的秒/字，不含校准系数 —— 校准统一在 speak_role 里乘，
    # 免得"用索引算的基准"和"用元数据算的基准"两条路各乘一次、越乘越偏。
    return round(v[len(v) // 2], 4)


def _alt_refs(role, d, items, tone_key, used_wav, n=2):
    """给「这一段没合成好、重来一次」准备的备选参考。

    为什么重试要**换参考**而不是原地重跑：段首丢字虽然随机，但同一段参考跑两次
    实测给出的是同一个坏结果（等于掷同一个骰子）；换一段参考才有机会绕开它。

    ★ 而且换的时候要**往"说得快"的方向换**（_probe_long5.py，同一段文本）：
        参考 0.264 秒/字（正常偏快） → 完整念出 ✓
        参考 0.224 秒/字（偏快）     → 完整念出 ✓
        参考 0.172 秒/字（偏慢）     → 照样吞掉 ✘
        默认参考（0.21 附近，拖）     → 吞掉 ✘
      语速/top_k/top_p/温度八个旋钮全试过，一点用都没有 —— 能改变结果的只有
      **换参考**。所以这里不是"随便抓一段别的"，是按每秒字数从快到慢往下挑。

    `items` 没有索引（角色没建过）时退回切片元数据算每秒字数，照样能挑快到慢。
    """
    used = set([os.path.basename(used_wav or "")])
    picks = []

    # file -> 时长。语气索引（items）里不带 dur，只能从切片元数据反查；
    # 反查不到或不可信时再实量文件（成本只落在个别文件上，不会每句都读盘）。
    dur_of = {}
    for s in role.get("segments") or []:
        if s.get("file"):
            dur_of[os.path.basename(s["file"])] = float(s.get("dur") or 0)

    def _dur(f):
        dd = dur_of.get(f)
        if ref_dur_ok(dd):
            return dd
        return wav_seconds(os.path.join(d, "slices", f))

    def fast_first(rows):
        """rows: [(spc, file, text)] —— spc 小的（说得快的）排前面。

        ★ 时长校验放在这里，是因为两个候选来源（语气索引 / 切片元数据）最后
          都从这一个出口出去 —— 闸在这里就闸住了全部出口。
          为什么必须闸：下游 GPT-SoVITS 对参考音频是硬性 3~10 秒，超了直接
          raise OSError。以前这里写的是 `1.0 <= dur <= 14.0`，于是大帝备选里
          那段 **1.60 秒**的切片被选中，长台词每段第 2 遍重试必抛异常 ——
          而"换参考"是长句丢字唯一的兜底（见上面八个旋钮的实测）。
          换句话说：护栏宽了一点，兜底就整个不存在了。
        """
        for spc, f, t in sorted(rows, key=lambda x: x[0]):
            if f in used or not t:
                continue
            if not ref_dur_ok(_dur(f)):
                continue        # 宁可少给备选，也不给一个下游必然报错的
            picks.append((f, t))
            used.add(f)
            if len(picks) >= n:
                break

    if items:
        rows = []
        for it in items:
            f = it.get("file")
            t = (it.get("text") or "").strip()
            sr = float(it.get("syl_rate") or 0)
            if f and t and sr > 0:
                rows.append((1.0 / sr, os.path.basename(f), t))
        fast_first(rows)
    if len(picks) < n:
        rows = []
        for s in role.get("segments") or []:
            f = s.get("file")
            t = (s.get("text") or "").strip()
            dur = float(s.get("dur") or 0)
            nc = len([c for c in t if c.strip()])
            if f and t and nc >= 6 and dur > 0:
                rows.append((dur / nc, os.path.basename(f), t))
        fast_first(rows)

    slices = os.path.join(d, "slices")
    return [{"ref_wav": os.path.join(slices, f), "ref_text": t} for f, t in picks]


def ensure_tone_index(rid, job=None):
    """拿到角色的声学索引；没有就现场建一个。

    建索引要用 librosa，那套在 GSV runtime 里才有，所以走子进程 ——
    不能把 librosa 塞进服务进程（同 torch schedule 冲突的教训见 tts_batch 顶部）。

    返回 (items 列表, 备注)。失败也不抛 —— 没有索引就退化成「只能用参数控语气」，
    不该让整个「说话」功能挂掉。
    """
    p = tone_index_path(rid)
    if os.path.isfile(p):
        try:
            return json.load(open(p, encoding="utf-8")).get("items") or [], ""
        except Exception as e:                                       # noqa: BLE001
            pass
    d = role_dir(rid)
    if not os.path.isdir(os.path.join(d, "slices")):
        return [], "没有切片"
    if job:
        log(job, "第一次用语气功能 —— 先把这个角色的 212 段语音量一遍（约 1 分钟，只做一次）")
    rc, out = run(job, '"%s" -s "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "tone_index.py"), d, p),
                  timeout=1800)
    if rc != 0 or not os.path.isfile(p):
        return [], "建索引失败：%s" % ((out or "")[-200:] or "退出码 %s" % rc)
    try:
        return json.load(open(p, encoding="utf-8")).get("items") or [], ""
    except Exception as e:                                           # noqa: BLE001
        return [], "索引读不出来：%s" % e


def speak_role(job, rid, texts, lang="zh"):
    """用角色音色说几句话。

    `texts` 可以是字符串列表（老写法），也可以是词典列表 —— 词典写法能带
    `tone`（语气 key）和 `raw`（原始一行），后者用来就地认 `[愤怒]` 这种标签。

    为什么要支持「在提示词里写语气」而不是给每行配个下拉：
    老板就是这么写词的—— "[愤怒]你怎么敢这么做！" —— 让他再去点表单是倒退。
    """
    role = get_role(rid)
    if not role or not role.get("model"):
        raise Fail("这个角色还没训练过，先点「训练」再说。")
    d = role_dir(rid)
    final = os.path.join(d, "final")
    os.makedirs(final, exist_ok=True)

    ref_file = (role.get("ref") or {}).get("file")
    ref_text = (role.get("ref") or {}).get("text", "")
    if not ref_file:
        p, t = pick_reference(role, os.path.join(d, "slices"))
        ref_file, ref_text = os.path.basename(p), t
        mutate_role(rid, lambda r: r.__setitem__("ref", {"file": ref_file, "text": ref_text}))
    ref_wav = os.path.join(d, "slices", ref_file)

    # ---------------------------------------------------------------- 拆句 + 认语气
    # 每一行都可能是「[愤怒]你怎么敢」这种带标签的写法，也可能是干净的台词。
    lines = []
    for it in texts:
        if isinstance(it, dict):
            raw = str(it.get("raw") or it.get("text") or "")
            forced = it.get("tone")
        else:
            raw = str(it or "")
            forced = None
        text, key, tags, unknown = tone.split_tone(raw)
        if forced and not tags:
            key = forced
        text = (text or "").strip()
        if not text:
            continue
        lines.append({"text": text, "tone": key, "tags": tags, "unknown": unknown})
    if not lines:
        raise Fail("没有要说的文本。")

    want_tone = [L for L in lines if L["tone"] and L["tone"] != "auto"]
    items, idx_err = ([], "")
    if want_tone:
        items, idx_err = ensure_tone_index(rid, job)
        if idx_err:
            log(job, "  （语气索引没建起来：%s —— 这次先用参数的办法）" % idx_err)

    # ★ 这个角色的"正常秒/字"。判"这一段是不是被吞了字"只有时长这一个廉价信号，
    #   没有基准分不清"念得快"和"少了六个字"。有语气索引就用它的语速中位，
    #   没有就退回切片元数据（时长/字数）—— 保证没建过索引的角色也能用上。
    raw_spc = tone.median_spc(items) or _base_spc_from_role(role)
    base_spc = round(raw_spc * SPEAK_SPC_CALIB, 4) if raw_spc else 0.0

    stamp = time.strftime("%m%d_%H%M%S")
    seg_dir = os.path.join(final, "_seg")
    tasks, notes, meta = [], [], []
    for i, L in enumerate(lines):
        # ★ 一句话切成几个"合成单元"（整段合成 147 字实测会丢三大块，见 tone.py 顶部）
        segs = tone.split_segments(L["text"])
        if not segs:
            continue
        joinp = os.path.join(final, "say_%s_%02d.wav" % (stamp, i + 1))
        key = L["tone"]
        rw, rt, spd, temp = "", "", 1.0, 0.6
        if key and items:
            got = tone.pick(items, key)
            t = tone.TONES_BY_KEY.get(key)
            nm = tone.TONES_BY_KEY.get(key, {}).get("name", key)
            if got:
                rw = os.path.join(d, "slices", got["file"])
                rt = got.get("text", "")
                spd = float(t.get("speed", 1.0))
                temp = float(t.get("temperature", 0.6))
                notes.append("第 %d 句【%s】→ 参考「%s」"
                             % (i + 1, nm, (got.get("text") or "")[:16]))
            else:
                notes.append("第 %d 句【%s】没挑到合适的参考，用默认音色" % (i + 1, nm))
        elif key:
            # 没有索引时，参数（语速/温度）还是要给 —— 不然用户写了标签却听不出区别
            t = tone.TONES_BY_KEY.get(key)
            if t:
                spd = float(t.get("speed", 1.0))
                temp = float(t.get("temperature", 0.6))
        for u in L["unknown"]:
            notes.append("★ 第 %d 句写的「%s」不在语气表里，当成普通文字处理了"
                         % (i + 1, u))
        if len(segs) > 1:
            notes.append("第 %d 句 %d 字 → 按换气点切成 %d 段合成后拼回一句（段长 %s）"
                         % (i + 1, len(L["text"]), len(segs),
                            "/".join(str(len(s["text"])) for s in segs)))

        alts = _alt_refs(role, d, items, key, rw, n=SPEAK_TAKES - 1)
        for j, sg in enumerate(segs):
            one = (len(segs) == 1)
            task = {"text": sg["text"],
                    "out": joinp if one else os.path.join(
                        seg_dir, "say_%s_%02d_seg%02d.wav" % (stamp, i + 1, j + 1))}
            if base_spc or alts:
                task["takes"] = SPEAK_TAKES
            if rw:
                task["ref_wav"] = rw
                task["ref_text"] = rt
                task["speed"] = spd
                task["temperature"] = temp
            if alts:
                task["alt_refs"] = alts
            if base_spc:
                task["base_spc"] = base_spc
            if not one:
                task["join"] = joinp
                task["gap"] = sg["gap"]
            tasks.append(task)
            meta.append({"line": i, "seg": j, "segs": len(segs), "join": joinp})
    if not tasks:
        raise Fail("没有要说的文本。")

    if base_spc:
        notes.append("每段合成两遍取更完整的那次（这个角色的正常语速 %.3f 秒/字；"
                     "整段直接合成会在长台词里跳字）" % base_spc)
    for n in notes:
        log(job, "  " + n)
    # ★ 认不出的标签**必须说出来**。静默吞掉的话，用户以为自己标了语气，
    #   实际是被当成台词念出来 —— 这种"没生效但不知道"比报错难受得多。

    tj = os.path.join(job["dir"], "tts_tasks.json")
    rj = os.path.join(job["dir"], "tts_result.json")
    json.dump(tasks, open(tj, "w", encoding="utf-8"), ensure_ascii=False)

    lang_name = {"zh": "中文", "ja": "日文", "en": "英文", "ko": "韩文", "yue": "粤语"}.get(lang, "中文")
    job["stage"] = "合成语音"
    # ★★ 跑之前先删掉上一次的结果文件，跑完**必须检查退出码**。
    #   原来的写法是：
    #       rc, out = run(...)
    #       results = json.load(open(rj)) if os.path.exists(rj) else []
    #   tts_batch 失败时不会写新的 result.json，于是这里**静默读到上一次的成品** ——
    #   上层看到"成功"、拿到成品路径、算出指标，而用户听到的是**上一次的音频**。
    #   这是最坏的一类假绿：这一步什么都没干，报表却全绿。
    #
    #   实测（2026-10-18 说话轮数 A/B）：B 臂三次测量全部指向 A 臂的 wav ——
    #   k=0 因 weight.json 指向被挪走的旧权重而失败（退出码 2），
    #   k=1 / k=2 读到上一轮留下的 result.json，于是"20 轮 vs 60 轮"
    #   看起来一模一样（CER 极差 0.0），实际上 **20 轮一次都没测到**。
    #   同一个 job 目录被多次复用（探针、重跑）时必然踩到。
    if os.path.exists(rj):
        try:
            os.remove(rj)
        except OSError:
            pass
    rc, out = run(job, '"%s" -s "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s" "%s"'
                  % (PY, os.path.join(STAGES, "tts_batch.py"), GSV_ROOT,
                     role["model"]["ckpt"], S2G, ref_wav, ref_text.replace('"', ""),
                     lang_name, tj, rj), timeout=3600)
    if rc != 0:
        _tail = "\n".join((out or "").strip().splitlines()[-6:])
        raise Fail("合成子进程失败（退出码 %d）：\n%s" % (rc, _tail or "（没有输出）"))
    if not os.path.exists(rj):
        raise Fail("合成子进程没产出结果文件（退出码 0 但 %s 不存在）"
                   % os.path.basename(rj))
    results = json.load(open(rj, encoding="utf-8"))

    # 每句用了什么语气、切了几段，跟着成品一起存 —— 过几天回听时得知道当时怎么写的。
    # ★ 一句话可能由多段拼成（join），产物清单里**只列拼好的那一句**，
    #   段文件留在盘上给复听用；否则用户看到"我说了 3 句"却列出 11 个文件。
    idx_line = {}
    for k, m in enumerate(meta):
        idx_line[k] = m["line"]
    groups = {}
    for k, m in enumerate(meta):
        groups.setdefault(m["join"], []).append(k)

    def _add(r):
        for jp, ids in groups.items():
            rs = [x for x in results if x.get("index") in ids]
            good = [x for x in rs if x.get("ok")]
            if not good:
                continue
            li = idx_line.get(ids[0], 0)
            L = lines[li] if li < len(lines) else {"text": "", "tone": ""}
            sims = [x.get("similarity") for x in good if x.get("similarity")]
            warns = [x["warn"] for x in rs if x.get("warn")]
            jsec = next((x.get("join_seconds") for x in rs
                         if x.get("join_seconds")), None)
            r.setdefault("outputs", []).append({
                "text": L.get("text", ""), "wav": jp, "at": _now(), "kind": "speech",
                "seconds": jsec if jsec is not None else good[0].get("seconds"),
                "similarity": round(min(sims), 4) if sims else None,
                "tone": L.get("tone") or "",
                "ref_clip": good[0].get("ref_wav", ""),
                "segs": len(ids),
                "warn": "；".join(warns),
            })

    mutate_role(rid, _add)
    for x in results:
        if x.get("warn"):
            log(job, "  ⚠ 第 %d 句：%s" % (idx_line.get(x.get("index"), 0) + 1, x["warn"]))
    bad = [r for r in results if not r.get("ok")]
    if bad:
        # ★ 报段号，不能报句号：一句话被切成 8 段时，说"第 1 句没通过"等于什么都没说
        #   （实测就是这么误报的 —— 8 段都属于第 1 句，报"第 1 句"让人以为是整句）。
        b = bad[0]
        seg_no = (b.get("seg_no") or 0) + 1
        seg_cnt = b.get("seg_count") or 1
        where = ("第 %d 段（这一句共 %d 段）" % (seg_no, seg_cnt)) if seg_cnt > 1 else "这一句"
        raise Fail("%s 没通过出声体检：%s" % (where, b.get("error")))
    return results


# ------------------------------------------------ 自动歌词（不想再让用户自己去抄原词）
#
# 改词链路里「原歌词」是硬需求：YingMusic 靠它把新词对到原曲的旋律上。但以前这一步
# 得用户自己抄一遍 —— 现在点一下：分离人声 → 识别 → 按标点/时长折成句 → 填进界面。
#
# 为什么用 faster-whisper 而不是整合包自带的 ASR（tools/asr）：
#   ① 那个对**歌唱**的中文识别率更低；
#   ② 它只给「整首拼成一行」的文本，**没有时间戳** —— 没有时间戳就没法按时值分句，
#      而句数配不上时长正是"改完词后段唱乱"的根因（踩过）。
# 模型走 ModelScope（国内可直连），只下 1.5GB 一次。

# 歌词识别的可选档位。**medium 是默认**：1.5 GB、8G 显存稳、几秒钟出结果。
# large-v3 是「精准」档：3.1 GB，中文/歌声上的错字明显更少（Whisper 系列里
# 规模带来的收益在歌唱这种"发音被拉长"的场景尤其明显 —— medium 常把
# 一个字听成形近字，large-v3 的声学建模更扛得住）。代价是慢 2~3 倍、多吃显存，
# 所以做成**可选**而不是一刀切换掉。
#
# singing 是「歌声专用」档：whisper-medium 在 **M4Singer**（中文歌唱语料，
# 20 歌手 / 419 首 / 29.7 小时）上全参数微调的版本。
#   ★ 它不是"更准"，是"**错的地方不一样**" —— 实测（同一条人声轨 224.9 秒，
#     16 个人工可确认的歌词片段做锚点）：medium 97.8% / singing 89.7% /
#     large 88.8%。singing 在「捍卫神族的荣誉」「狂热的信徒」「腐蚀的灵魂」
#     这些 medium 会听错成容誉/心度的位置上更稳，但在「誓死冲锋」上反而不如
#     medium。⇒ 它存在的意义是**多一次机会**：这档认不出来换那档，
#     而不是宣称全面更好。
ASR_SIZES = {"medium": "faster-whisper-medium",
             "large": "faster-whisper-large-v3",
             "singing": "faster-whisper-m4singer"}
ASR_DEFAULT = "medium"
LYRIC_CACHE = os.path.join(DATA, "cache", "lyrics")


def asr_size_norm(s):
    """把前端/命令行传来的各种写法归一到内部档位名。

    未知写法一律退回默认档 —— 宁可少一个功能，也别让一个拼错的参数
    把整条识别链路搞崩（这类"参数写错就崩"的坑这个项目踩过不止一次）。
    """
    k = (s or "").strip().lower().replace("_", "-")
    if k in ("large", "large-v3", "largev3", "big", "精准"):
        return "large"
    if k in ("singing", "m4singer", "sing", "歌声", "歌声专用", "专用"):
        return "singing"
    return ASR_DEFAULT


def whisper_dir(size=None):
    return os.path.join(HERE, "models", ASR_SIZES[asr_size_norm(size)])


def whisper_ready(size=None):
    d = whisper_dir(size)
    return all(os.path.isfile(os.path.join(d, f))
               for f in ("model.bin", "config.json", "tokenizer.json"))


def ensure_whisper(job, size=None):
    sz = asr_size_norm(size)
    if whisper_ready(sz):
        return True
    if sz == "singing":
        # ★ 歌声专用档**不做自动下载**：它在上游只发布了 transformers 格式
        #   （safetensors），要先用 ctranslate2 转成 faster-whisper 能吃的目录，
        #   而转换器要 transformers>=4.56（runtime 里是 4.51.3，别的环节在用）。
        #   本机已经转好放在 models/faster-whisper-m4singer；万一丢了，
        #   照 tools/convert_whisper_ct2.py 里的步骤重来一次即可。
        #   与其让它在后台自动跑一条我测不全的链路，不如把话说清楚。
        log(job, "  [warn] 歌声专用档的权重不在：%s" % whisper_dir(sz))
        return False
    mb = "3.1 GB（精准档，识别更准但要下一会儿）" if sz == "large" else "1.5 GB"
    log(job, "  第一次用「自动歌词」，要下语音识别模型（约 %s，走国内镜像），只下一次" % mb)
    # dl_whisper.py 里 large 档用的是 HF 仓库名 large-v3，这里要传真名
    rc, out = run(job, '"%s" -s "%s" --dir "%s" --size %s'
                  % (PY, os.path.join(STAGES, "dl_whisper.py"),
                     os.path.join(HERE, "models"),
                     "large-v3" if sz == "large" else "medium"),
                  timeout=7200)
    if rc != 0 or not whisper_ready(sz):
        log(job, "  [warn] 识别模型没准备好：%s" % _tail(out, 3))
        return False
    return True


def recognize_lyrics(job, song_path, lang="zh", sep_model=None, use_cache=True,
                     ref_text=None, asr_size=None):
    """自动识别一首歌的歌词。返回 {"line": "...", "segments": [...], ...}。

    失败抛 Fail —— 这个功能是用户主动点的，失败了要让他看见原因
    （而不是安静地返回空字符串，那样他会以为"这歌没词"）。

    ref_text = 用户粘进来的**正确歌词**。给了它就有两重作用：
      ① 当 whisper 的提示词，让识别往上靠；
      ② 认完之后拿它把错字换掉（时间戳不变）。
    给了参考歌词就**不复用缓存** —— 要重新认一次才能和参考对上，
    但认完会写回同一个缓存文件，所以之后没参考的那次也用的是订正过的版本。

    asr_size = 识别档位（medium / large）。**它进缓存键**：换档位就是换模型，
    拿上一档的结果来顶替会让人以为"精准档也没变准"。
    """
    sz = asr_size_norm(asr_size)
    if not song_path or not os.path.isfile(song_path):
        raise Fail("歌曲文件不存在：%s" % song_path)
    if not ensure_whisper(job, sz):
        raise Fail("语音识别模型没准备好。可以手动把 %s 放到：\n  %s"
                   % (ASR_SIZES[sz], whisper_dir(sz)))

    job["stage"] = "识别歌词"
    begin_step(job, "分离人声", "歌词得从人声里认；顺手复用翻唱那份分离缓存，不白跑。")
    vocal, _inst = separate_song(job, song_path, model=sep_model)
    end_step(job, True, "人声轨就绪")

    os.makedirs(LYRIC_CACHE, exist_ok=True)
    # 缓存键复用分离那套（路径+大小+mtime+模型），额外带上**语种和识别档位** ——
    # 换了语种/换了模型都要重认（上一次的结论对不上）。
    out_json = lyric_cache_path(song_path, lang, sz)
    if use_cache and not (ref_text or "").strip() and os.path.isfile(out_json):
        with contextlib.suppress(Exception):
            with open(out_json, encoding="utf-8") as f:
                d = json.load(f)
            log(job, "  复用上次的识别结果（%d 句）" % len(d.get("segments") or []))
            return d

    begin_step(job, "识别歌词（faster-whisper %s）" % ("精准档" if sz == "large" else "标准档"),
               "按词级时间戳，把这首歌在唱什么折成一句一句 —— 直接填进改词台的「原歌词」栏。"
               "第一次要加载模型（十几秒），之后很快。")
    tmp = os.path.join(LYRIC_CACHE, "_asr_in.wav")
    rc, o1 = run(job, '"%s" -y -v error -i "%s" -ac 1 -ar 16000 "%s"'
                 % (FFMPEG, vocal, tmp), timeout=900)
    if rc != 0:
        end_step(job, False, "人声轨转码失败")
        raise Fail("人声轨转码失败：%s" % _tail(o1, 2))
    # 参考歌词走文件传：中文 + 引号 + 换行进 Windows 命令行会被转义搞坏
    # （改词那边的歌词就是这么传的，同一个坑不踩第二次）。
    ref_arg = ""
    if (ref_text or "").strip():
        try:
            fp = os.path.join(LYRIC_CACHE, "_ref.txt")
            with open(fp, "w", encoding="utf-8") as f:
                f.write(ref_text)
            ref_arg = ' --ref-file "%s"' % fp
            log(job, "  带上了你给的歌词（%d 字）当参照 —— 识别完会按它订正错字"
                % len(ref_text.strip()))
        except Exception:
            ref_arg = ""
    rc, out = run(job, '"%s" -s "%s" --wav "%s" --out "%s" --model "%s" --lang %s%s'
                  % (PY, os.path.join(STAGES, "lyrics_asr.py"), tmp, out_json,
                     whisper_dir(sz), lang, ref_arg), timeout=3600,
                  on_line=lambda ln: _pct_from_line(job, ln))
    with contextlib.suppress(Exception):
        os.remove(tmp)
    if rc != 0 or not os.path.isfile(out_json):
        end_step(job, False, "识别失败")
        raise Fail("歌词识别没跑通（退出码 %d）：\n%s" % (rc, _tail(out, 4)))
    # 记住"这份内容 = 这个文件" ⇒ 这首歌的其它副本直接复用，不用再认一遍
    _lyric_alias_write(song_path, out_json, lang, sz)
    with contextlib.suppress(Exception):
        with open(out_json, encoding="utf-8") as f:
            d = json.load(f)
    end_step(job, True, "识别出 %d 句" % len(d.get("segments") or []))
    return d


def lyrics_job(job, song_path, lang="zh", sep_model=None, ref_text=None,
               asr_size=None):
    """「自动歌词」的任务体：把识别结果挂到 job 上，前端完成后回填输入框。"""
    sz = asr_size_norm(asr_size)
    d = recognize_lyrics(job, song_path, lang=lang, sep_model=sep_model,
                         ref_text=ref_text, asr_size=sz)
    # 把档位记进结果里：界面要能显示"这份是精准档认的"，
    # 用户才好判断"要不要再花几分钟用精准档重认一次"。
    with contextlib.suppress(Exception):
        p = lyric_cache_path(song_path, lang, sz)
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                dd = json.load(f)
            dd["asr_size"] = sz
            with open(p, "w", encoding="utf-8") as f:
                json.dump(dd, f, ensure_ascii=False, indent=1)
            d["asr_size"] = sz
    job["lyric_result"] = {
        "line": d.get("line") or "",
        "segments": d.get("segments") or [],
        "duration": d.get("duration") or 0,
        "song": os.path.basename(song_path),
        "ref": d.get("ref") or None,
        "asr_size": sz,
    }
    log(job, "  识别结果：%s" % ((d.get("line") or "")[:160]))
    return d


def lyric_cache_path(song_path, lang="zh", size=None):
    """歌词缓存文件。**档位进键**：medium 和 large 是两套结果，不能互相顶替。"""
    return os.path.join(LYRIC_CACHE,
                        _sep_cache_key(song_path, "lyrics|%s|%s"
                                       % (lang, asr_size_norm(size))) + ".json")


def _song_content_id(song_path):
    """这首歌的**内容**指纹（不看路径、不看 mtime）。

    ★★ 为什么非有它不可（这是"逐句指定"一直用不起来的真因之一）：
       `_sep_cache_key` 把 **mtime 算进键里**（防"路径没变内容换了"）。
       但用户手上有好几个**字节完全一样**的副本 —— 同一首 Rather Be 在
       `data/music/` 和 `data/uploads/` 各躺一份（大小都是 9644738），
       `cp` 出来 mtime 必变 ⇒ 两个键 ⇒ 认过的词在另一个副本上**不算数**。
       表现：切到另一个副本，逐句指定的列表变成"按时间切的 20 段"（每段
       12 秒、文本为空）⇒ 用户根本无从下手决定"哪一句谁唱"，只能看着
       一排 `第 1 段 / 第 2 段…`。
       内容指纹才是"同一首歌"的正确定义 —— 复制、改名、移动都不该当新歌。
    """
    import hashlib
    try:
        st = os.stat(song_path)
    except OSError:
        return ""
    try:
        h = hashlib.sha1()
        h.update(b"sz%d" % st.st_size)
        with open(song_path, "rb") as f:
            h.update(f.read(262144))            # 头 256K
            if st.st_size > 262144:
                try:
                    f.seek(st.st_size - 262144)  # 尾 256K
                except OSError:
                    f.seek(0)
                h.update(f.read(262144))
        return h.hexdigest()[:16]
    except OSError:
        return ""


def _lyric_alias_path(cid, lang="zh", size=None):
    """内容指纹 → 缓存文件名的登记表（按语种+档位分开，别互相顶替）。"""
    return os.path.join(LYRIC_CACHE, "_bycontent", "%s_%s_%s.json"
                        % (cid or "_", lang, asr_size_norm(size)))


def _lyric_alias_write(song_path, cache_path, lang="zh", size=None):
    """记住"这份内容是按这个缓存文件认的"。"""
    cid = _song_content_id(song_path)
    if not cid:
        return
    try:
        os.makedirs(os.path.join(LYRIC_CACHE, "_bycontent"), exist_ok=True)
        with open(_lyric_alias_path(cid, lang, size), "w", encoding="utf-8") as f:
            json.dump({"file": os.path.basename(cache_path),
                       "src": os.path.basename(song_path),
                       "at": time.strftime("%Y-%m-%d %H:%M:%S")},
                      f, ensure_ascii=False, indent=1)
    except Exception:
        # 登记表写坏不影响主流程：下次查不到就按没认过处理
        pass


def lyric_cache_file(song_path, lang="zh", size=None):
    """这首歌的歌词缓存**在哪个文件里**（没有就说 None）。

    先按"路径+mtime"找，找不到再按内容指纹找 —— 副本之间共享一份结果。
    """
    try:
        p = lyric_cache_path(song_path, lang, size)
    except OSError:
        p = ""       # _sep_cache_key 要 stat 这首歌；路径不在就当没有缓存
    if p and os.path.isfile(p):
        return p
    cid = _song_content_id(song_path)
    if not cid:
        return None
    try:
        with open(_lyric_alias_path(cid, lang, size), encoding="utf-8") as f:
            q = (json.load(f) or {}).get("file") or ""
    except Exception:
        q = ""
    if not q:
        return None
    # ★ 只认 basename：登记表里存的是文件名，拼回来防路径穿越
    ap = os.path.join(LYRIC_CACHE, os.path.basename(q))
    return ap if os.path.isfile(ap) else None


def load_lyric_cache(song_path, lang="zh", size=None):
    """读出这首歌的歌词缓存（识别结果 / 校对结果），没有就返回 None。

    兼容：2026-10-20 之前只有一档模型，缓存键里没有档位。那份产物就是
    medium 认的，所以**只有默认档**允许回退去读旧文件 —— 已经认过的歌
    不会因为这次升级就被判成"没识别过"，得重花一两分钟。
    """
    if not song_path:
        return None
    sz = asr_size_norm(size)
    p = lyric_cache_file(song_path, lang, sz)
    if not p and sz == ASR_DEFAULT:
        old = os.path.join(LYRIC_CACHE,
                           _sep_cache_key(song_path, "lyrics|%s" % lang) + ".json")
        if os.path.isfile(old):
            p = old
    if not p:
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_lyric_segments(song_path, segments, lang="zh", line=None, size=None):
    """把**用户校对过**的分句写回缓存，返回写好的那份。

    为什么要能改：原词是要送进模型做音素化的，认错一个字就唱错一个字，
    而 whisper 在歌唱上的错字是肉眼可见的。用户自己改一句，比让整首歌
    带着错字重唱一遍便宜得多 —— 所以这一份必须能存下来，
    而且**存的就是改词时要用的那一份**（lyric_segments_for 直接读它）。

    校验（宁可拒、不可存坏数据）：
      · 时间必须递增且非负，end 不早于 start；
      · 至少一句有文本；
      · 文本里不能有 `|`（它是句子分隔符，混进来会让句数算错）。
    """
    d = load_lyric_cache(song_path, lang, size)
    if d is None:
        d = {"lang": lang, "duration": 0, "words": 0}
    if not isinstance(segments, list) or not segments:
        raise Fail("没有可保存的句子")
    # 清洗规则放在 lyric_plan 里（纯逻辑），门禁才能几秒钟验一遍，
    # 而不是"改完还得真起一次服务才知道有没有写坏"。
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    from lyric_plan import clean_segments
    clean, dropped = clean_segments(segments)
    if not clean:
        raise Fail("没有一句是有效的（要有文本，且时间是数字）")
    d["segments"] = clean
    d["line"] = (line if isinstance(line, str) and line.strip()
                 else "|".join(x["text"] for x in clean))
    d["proofread"] = True
    d["proofread_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["asr_size"] = asr_size_norm(size)
    os.makedirs(LYRIC_CACHE, exist_ok=True)
    with open(lyric_cache_path(song_path, lang, size), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    _lyric_alias_write(song_path, lyric_cache_path(song_path, lang, size),
                       lang, size)
    return d


def lyric_segments_for(song_path, orig_lyric, lang="zh", offset=0.0, dur=None,
                       size=None):
    """找回「原歌词」每句的**时间戳**（同一首歌上次识别的那份）。

    为什么改词非它不可：改词是**分块**生成的（约 28 秒一块），每一块只有拿到
    "这块真正在唱哪几句"，新词才对得上乐句。没有时间戳就只能按句数均分，
    而歌里有前奏和间奏（实测一份 25 句的识别结果里夹着 9 秒和 18 秒的间奏），
    均分必然把间奏后面的词算到前面的块上 —— 模型拿到的 ref_text 和这一块
    实际在唱的东西对不上，听感就是"词和调错位 / 乱说词"，间奏段还会被硬唱成乱吼。

    offset/dur：给**试听小样**用。小样是从整首的第 35% 处裁一段出来唱的，
    而时间戳是整首的绝对时间 —— 不挪过来，片段里一句都对不上，
    改词在小样上就**等于没做**（踩过：小样听着还是原词，查下来是时间戳没裁）。

    只在"框里的词就是识别出来的那份"时才敢用时间戳（否则宁可退回均分）。
    """
    if not song_path or not orig_lyric:
        return None
    # 没指定档位就把两档都找一遍：用户可能是用精准档认的，而唱歌入口
    # （改词那张表单）并没有"你这份词是哪档认的"这个字段 —— 与其让他
    # 因为档位对不上而拿不到时间戳（退回去按句数均分 ⇒ 又变成乱吼），
    # 不如两份都看一眼。
    order = [asr_size_norm(size)] if size else [ASR_DEFAULT, "large"]
    norm = lambda s: (s or "").replace("\n", "|").replace(" ", "").replace("\u3000", "")
    segs = None
    for sz in order:
        p = lyric_cache_path(song_path, lang, sz)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        got = [x for x in (d.get("segments") or [])
               if isinstance(x, dict) and x.get("text") and x.get("start") is not None]
        if got and norm(d.get("line")) == norm(orig_lyric):
            segs = got
            break
    if not segs:
        return None
    if (offset and offset > 0) or dur:
        if STAGES not in sys.path:
            sys.path.insert(0, STAGES)
        from lyric_plan import slice_segments
        segs, outside = slice_segments(segs, offset, dur or 0.0)
        if not segs:
            return None
    return segs


def lyric_segments_bilingual(song_path, lang="zh"):
    """这首歌**校对过**的分句（可能带 `zh` 中文），没有就 None。

    ★ 为什么只看 proofread 的那份：只有用户改过 / 导入过的才配叫"最终稿"。
      机器认的那份随时可能换个档位重认，拿它当权威会让人以为"我改的词没了"。
    """
    if not song_path:
        return None
    for sz in (ASR_DEFAULT, "large", "singing"):
        p = lyric_cache_path(song_path, lang, sz)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if not d.get("proofread"):
            continue
        segs = [x for x in (d.get("segments") or [])
                if isinstance(x, dict) and (x.get("text") or "").strip()]
        if segs:
            return segs
    return None


def _write_zh_back(song_path, lang, segs):
    """把补翻出来的中文写回这首歌的歌词缓存。只写 `zh`，别的字段一个不动。

    ★ 为什么要写回：预览接口每次重排都会调，不落盘就每次都要翻一遍
      （一首歌十几秒），界面会卡。写回之后第二次就是读文件。

    ★ 为什么敢写：只加 `zh` 这一个键。`text`（原文）、`proofread`（是否校对过）
      都不碰 —— 校对稿是用户改过的，机器不该在他的稿子上动别的。
      写失败静默返回：少一份中文不影响功能，不能让它把预览接口搞挂。
    """
    try:
        p = lyric_cache_file(song_path, lang)
        if not p or not os.path.isfile(p):
            return False
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
        old = (d.get("segments") or [])
        if len(old) != len(segs):
            return False     # 句数对不上（重认过词）⇒ 宁可不写，别写错行
        n = 0
        for i, s in enumerate(segs):
            z = (s.get("zh") or "").strip()
            if z and isinstance(old[i], dict):
                old[i]["zh"] = z
                n += 1
        if not n:
            return False
        d["segments"] = old
        with open(p, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def lyric_bi_import(song_path, text, lang="zh", asr_size=None, layout="auto"):
    """**一次性导入「原文 + 中文」双语歌词，自己对着歌贴到该在的秒数。**

    用户要的动作就一步：粘进来 → 每一句自己落到第几秒。
    这里把这件事做完并**直接写进这首歌的歌词缓存**（= 校对过的那份），
    所以后面做视频、跑合唱拿的都是这一份。

    返回 (segments, report, parsed, need_asr)
      need_asr=True：这份词没有时间戳（不是 LRC），而这首歌还没认过
                     ⇒ 得先跑一次识别。调用方据此决定要不要起任务。

    ★ 用户给了 LRC 时间戳时**一次模型都不跑**：既不用识别，也不用分离
      （recognize_lyrics 内部要先分离人声）。一首 4 分钟的歌省下约 4 分钟。
    """
    if not song_path or not os.path.isfile(song_path):
        raise Fail("歌曲文件不存在：%s" % song_path)
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import lyric_bi as _lb
    parsed = _lb.parse_bilingual(text, layout=layout)
    if not parsed.get("pairs"):
        raise Fail("没从你粘的内容里读出歌词（%s）"
                   % (parsed.get("note") or "内容看起来是空的"))
    # LRC：时间本来就在文本里 ⇒ 不用识别，也不用分离
    segs, rep = _lb.place([], parsed["pairs"])
    if not rep.get("ok"):
        asr, _dur = lyric_segments_any(song_path, lang)
        if not asr:
            return [], rep, parsed, True
        segs, rep = _lb.place(asr, parsed["pairs"])
        if not rep.get("ok"):
            raise Fail("一句都没贴上：%s" % _lb.describe(rep))
    save_lyric_segments(song_path, segs, lang=lang, size=asr_size)
    return segs, rep, parsed, False


def job_lyric_bi(job, song_path, text, lang="zh", asr_size=None, layout="auto"):
    """「粘贴双语歌词」的任务体：需要识别时才起任务（LRC 那条路是瞬时的）。"""
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import lyric_bi as _lb
    sz = asr_size_norm(asr_size)
    try:
        segs, rep, parsed, need = lyric_bi_import(
            song_path, text, lang=lang, asr_size=sz, layout=layout)
    except BaseException as e:                 # 含批量删除钩子的 SystemExit
        raise
    if need:
        log(job, "  这份词没有时间戳 —— 先识别一次这首歌，才能把每句贴到第几秒")
        d = recognize_lyrics(job, song_path, lang=lang, asr_size=sz)
        asr = (d or {}).get("segments") or []
        if not asr:
            raise Fail("这首歌没认出歌词，贴不了时间。可以换「LRC」排版（自带时间戳）")
        p2 = _lb.parse_bilingual(text, layout=layout)
        segs, rep = _lb.place(asr, p2["pairs"])
        if not rep.get("ok"):
            raise Fail("一句都没贴上：%s" % _lb.describe(rep))
        save_lyric_segments(song_path, segs, lang=lang, size=sz)
        parsed = p2
    log(job, "  %s" % _lb.describe(rep))
    for ln in (rep.get("unplaced") or [])[:8]:
        log(job, "    [没贴上] %s" % ln)
    res = {
        "song": os.path.basename(song_path),
        "mode": parsed.get("mode"),
        "placed": rep.get("placed", 0),
        "unplaced": rep.get("unplaced") or [],
        "low_conf": rep.get("low_conf", 0),
        "split": rep.get("split", 0),
        "lines": [{"start": x["start"], "end": x["end"],
                   "text": x.get("text") or "", "zh": x.get("zh") or ""}
                  for x in (segs or [])],
    }
    # 任务函数的返回值会被存进 job["result"] —— 前端靠它把贴好的表画回去。
    job["lyric_bi"] = res
    return res


def rewrite_lyrics(job, vocal_wav, orig_lyric, new_lyric, out_wav, nfe=32,
                   orig_segments=None):
    """
    改词：把人声轨**重新唱一遍** —— 旋律照原曲，词换成用户写的新词。

    为什么必须有这一步、为什么它只能待在链路的这个位置：
      RVC 是音色转换器，它输出唱的是什么词，100% 由喂进去的音频决定，
      给它任何文本参数都没用。所以「改词唱」必须在变声**之前**
      先把「唱着新词的干声」造出来。造出来之后，后面的变声和混音
      一行都不用改 —— 这正是把它插在这里的理由。

    歌词走**文件**传给子进程，不拼进命令行：中文、引号、换行直接进 Windows
    命令行会被转义搞坏，而且用户写的一个引号就能把命令截断（等于歌词能注入命令）。
    """
    if not os.path.isfile(YING_PY):
        raise Fail("改词要用的环境不在了：%s\n"
                   "改词依赖本机已部署的 YingMusic-Singer-Plus（D 盘那个独立目录）。"
                   % YING_PY)
    fd = tempfile.mkdtemp(prefix="lyric_")
    fo = os.path.join(fd, "orig.txt")
    fn = os.path.join(fd, "new.txt")
    # 留着不删：改词出问题时，用户/我能直接看当时真正送进去的是哪两段词。
    with open(fo, "w", encoding="utf-8") as f:
        f.write(orig_lyric or "")
    with open(fn, "w", encoding="utf-8") as f:
        f.write(new_lyric or "")

    # 时间戳（有就给，子进程按时间把词对到每一块；没有它会退回均分并打警告）
    lrc_arg = ""
    if orig_segments:
        fl = os.path.join(fd, "orig.lrc.json")
        try:
            with open(fl, "w", encoding="utf-8") as f:
                json.dump(orig_segments, f, ensure_ascii=False)
            lrc_arg = ' --orig-lrc "%s"' % fl
            log(job, "  改词带上了 %d 句的时间戳（按时间对乐句，不按句数均分）"
                % len(orig_segments))
        except Exception:
            lrc_arg = ""
    else:
        log(job, "  ⚠ 没拿到原词的时间戳，改词会按句数均分 —— "
                 "歌里若有长间奏，词可能对错乐句（先点「识别歌词」能避免）")

    secs = wav_seconds(vocal_wav) or 0
    n = max(1, int(round(secs / LYRIC_CHUNK_SEC))) if secs else 4
    # 实测：加载模型约 50 秒（一次性），之后每块 28 秒音频约 90 秒。
    est = int(60 + n * 90)
    cmd = ('"%s" "%s" --vocal "%s" --orig-file "%s" --new-file "%s" '
           '--out "%s" --nfe %d --chunk %.0f%s'
           % (YING_PY, os.path.join(STAGES, "lyric_edit.py"),
              vocal_wav, fo, fn, out_wav, int(nfe), LYRIC_CHUNK_SEC, lrc_arg))
    rc, out = run(job, cmd, timeout=5400,
                  on_line=lambda ln: _pct_from_line(job, ln),
                  heartbeat=(est, "改词中：%d 块，本机约 %d 分钟（估算，以结束为准）"
                             % (n, round(est / 60.0))))
    if rc != 0 or not os.path.exists(out_wav):
        raise Fail("改词失败（退出码 %d）。\n%s" % (rc, _tail(out, 6)))
    for ln in (out or "").splitlines():
        if "⚠" in ln or "完成：" in ln or "注意：" in ln:
            log(job, "  " + ln.strip())
    return out_wav


# ---------------------------------------------------------------- Seed-VC
#
# 为什么在 RVC 之外还要这一层（全是实测，不是听说）：
#   · RVC 那一步负责"把音高/音域对到角色身上"，输出是 40k 声码器的声音；
#     Seed-VC（44.1k DiT + BigVGAN）再过一遍，音色分数 0.7099 → **0.7674**（微调后）。
#   · ★ **44.1k 只是容器提升，不是带宽提升**：16k 以上频段的能量只有 0.012
#     （原唱是 0.25），有效带宽约 12k。它换来的是**音色**，不是更宽的频响 ——
#     界面上别把"44.1k"吹成"高频更好"。
#   · 微调（阿塔尼斯 191 段 / 16.1 分钟 / 1000 步）：**600 步是唯一「音色↑、
#     原唱残留↓、咬字↑」三项全胜的档位**；1000 步反而过拟合（咬字变差、亮度掉）。
#     ⇒ 默认 600 步。官方警告的"微调会升高 WER"在本例没发生。
#
# 三个函数：prepare（整理素材）/ finetune（微调）/ polish（唱歌时精修）。
# 引擎的**路径和档位清单**都在 seedvc.py 里（只读，不 import studio，避免循环）。

SEEDVC_MIN_SEGS = 5        # 硬门槛：再少真学不出音色
SEEDVC_MIN_SECS = 10.0


def seedvc_env(with_libs=True):
    """跑 Seed-VC 的环境。

    PYTHONPATH 挂两处：引擎根目录（它自己的模块）+ _svclibs（缺的依赖，
    装在这个隔离目录里，不动 RVC 本体）。torch 一律不装，用运行时的 2.7.1
    —— 50 系显卡只有 torch≥2.7 能跑，官方钉的 2.2 在这台机器上起不来。
    """
    if with_libs:
        return {"PYTHONPATH": seedvc.SEEDVC_ROOT + os.pathsep + seedvc.SEEDVC_LIBS}
    return {"PYTHONPATH": seedvc.SEEDVC_ROOT}


def seedvc_prepare(job, rid):
    """把角色素材整理成 Seed-VC 的训练集目录。

    只**新增/覆盖**、不删旧文件：切片文件名是稳定的，重跑一次自然就是
    "把新素材补进去"；整目录重建要批量删除，而误删素材是不可逆的。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    ok, why = seedvc.enabled()
    if not ok:
        raise Fail("Seed-VC 用不了：%s" % why)
    out = seedvc.dataset_dir(role)
    begin_step(job, "整理微调素材",
               "挑出时长 ≥2 秒、有声比例 ≥60% 的素材段，拷成 Seed-VC 的训练集。"
               "碎段和静音段只会教模型学噪声，所以要滤掉。")
    rc, txt = run(job, '"%s" -s "%s" prep --vs "%s" --role "%s" --out "%s"'
                  % (RVC_PY, os.path.join(STAGES, "seedvc_cli.py"), HERE, rid, out),
                  cwd=RVC_ROOT, timeout=1800)
    m = re.search(r"^PREP n=(\d+) secs=([\d.]+)", txt or "", re.M)
    if rc != 0 or not m:
        end_step(job, False, "素材整理失败")
        raise Fail("整理微调素材失败（退出码 %d）。\n%s" % (rc, _tail(txt, 4)))
    n, secs = int(m.group(1)), float(m.group(2))
    if n < SEEDVC_MIN_SEGS or secs < SEEDVC_MIN_SECS:
        end_step(job, False, "素材不够")
        raise Fail("符合要求的素材只有 %d 段 / %.1f 秒，太少（至少要 %d 段 / %.0f 秒）。\n"
                   "微调是拿角色的干声教模型「这个声音是谁」，素材太少只会学到噪声。"
                   % (n, secs, SEEDVC_MIN_SEGS, SEEDVC_MIN_SECS))
    end_step(job, True, "%d 段 / %.1f 分钟" % (n, secs / 60.0))
    log(job, "  训练集目录：%s" % out)

    def _set(r):
        sv = dict(r.get("seedvc") or {})
        sv.update({"dataset": seedvc.dataset_name(role), "run": seedvc.run_name(role),
                   "segs": n, "secs": secs, "at": _now()})
        r["seedvc"] = sv

    mutate_role(rid, _set)
    return {"segs": n, "secs": secs, "dir": out}


def seedvc_finetune(job, rid, steps=None):
    """用这个角色的干声去微调 Seed-VC（让它更会发这个人的声音）。

    每次都从**基础权重**重新微调（不是接着上次的微调再训）：实测那条路
    （1000 步一次跑完、每 200 步存一档）就是从基础权重开始的，中间档是为了
    挑步数用的；接着旧档训等于换个起点重来，没法复现 600 步那个拐点。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    steps = int(steps or seedvc.DEFAULT_STEPS)
    if steps not in seedvc.STEPS_CHOICES:
        steps = min(seedvc.STEPS_CHOICES, key=lambda c: abs(c - steps))
    info = seedvc_prepare(job, rid)

    # 每存一档约 2.2GB（DiT + 优化器状态），600 步默认存 3 档 ⇒ 按 8GB 留余量。
    step_check_disk(job, need_gb=8.0)

    job["stage"] = "微调"
    begin_step(job, "微调 %d 步" % steps,
               "拿上面那些素材教模型「这个人的声音长什么样」。本机实测 1000 步 "
               "7 分 43 秒（8GB 卡峰值 6.1GB），600 步通常五分钟内。\n"
               "每 200 步存一档，训完可以在下面挑档位 —— 步数不是越多越好，"
               "实测 600 步最好，1000 步会过拟合（咬字变糊）。")
    # 实测 0.46 秒/步（1000 步 7m43s，含约 1 分钟加载）
    est = int(steps * 0.6 + 120)

    def _track(line):
        # train.py 每 log_interval 步打一行：epoch 3, step 600, loss: 0.55
        m = re.search(r"step\s+(\d+)", line or "")
        if not m:
            return
        s = int(m.group(1))
        job["epochs_done"] = s
        job["epochs_total"] = steps
        pct = int(min(90, s * 100.0 / max(steps, 1)))
        if pct > (job.get("pct") or 0):
            job["pct"] = pct
        job["detail"] = "第 %d / %d 步" % (s, steps)
        job["stage"] = "微调 · 第 %d/%d 步" % (s, steps)

    cmd = ('"%s" train.py --config %s --pretrained-ckpt %s --dataset-dir "%s" '
           '--run-name %s --batch-size 1 --max-steps %d --max-epochs 10 '
           '--save-every %d --num-workers 0'
           % (seedvc.py_path(), seedvc.CONFIG, seedvc.BASE_CKPT, info["dir"],
              seedvc.run_name(role), steps, min(seedvc.SAVE_EVERY, steps)))
    rc, txt = run(job, cmd, cwd=seedvc.SEEDVC_ROOT, env=seedvc_env(True),
                  timeout=7200, on_line=_track,
                  heartbeat=(est, "微调中（本机约 0.5 秒/步，%d 步预计 %d 分钟）"
                             % (steps, round(est / 60.0))))
    if rc != 0:
        end_step(job, False, "微调失败")
        raise Fail("Seed-VC 微调失败（退出码 %d）。\n%s" % (rc, _tail(txt, 6)))
    rows = seedvc.ckpts(role)
    if not rows:
        end_step(job, False, "没找到权重")
        raise Fail("训练说跑完了，但 runs/%s 下面没找到权重文件。" % seedvc.run_name(role))
    # 默认挑 600 步（实测拐点）；没有就取最接近 600 的那一档
    pick = min(rows, key=lambda r: (abs(r["step"] - seedvc.DEFAULT_STEPS), r["final"]))
    for r in rows:
        log(job, "  档位 %s（%s，%.1f GB）" % (r["label"], r["at"], r["size"] / 1024.0 ** 3))

    def _set(r):
        sv = dict(r.get("seedvc") or {})
        sv.update({"ckpt": pick["path"], "step": pick["step"], "at": _now(),
                   "steps": steps})
        r["seedvc"] = sv

    mutate_role(rid, _set)
    end_step(job, True, "已出 %d 档，默认用 %s" % (len(rows), pick["label"]))
    return {"steps": steps, "ckpts": [r["label"] for r in rows], "selected": pick["label"]}


def seedvc_polish(job, rid, src_vocal, rvc_vocal, out_wav):
    """把 RVC 干声再过一遍 Seed-VC（44.1k），抬音色。返回成品路径。

    ★ source 用**交叉合成**的结果，不是直接拿 RVC 干声：
      直接 RVC 干声 → 糊（RVC 声码器把谐波做得过于规整，辅音和气声没了）；
      直接原唱     → 咬字清晰但原唱残留重。
      低频取 RVC（音色/共振峰）、高频取原唱（辅音/气声），两头都保住 ——
      这是实测比出来的，不是拍脑袋。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    ok, why = seedvc.enabled()
    if not ok:
        raise Fail("Seed-VC 用不了：%s" % why)
    ck = seedvc.selected(role)
    if not ck:
        raise Fail("这个角色还没有 Seed-VC 微调档位 —— 先去「训练」页跑一次微调。")
    ckpt = seedvc.ckpt_abs(ck["path"])
    if not os.path.isfile(ckpt):
        raise Fail("选中的档位文件已经不在了：%s\n可能是在「存储」里被清掉了，"
                   "重新微调或换一个档位。" % ckpt)
    final = os.path.dirname(out_wav)
    base, _ = os.path.splitext(out_wav)
    prompt = os.path.join(final, "seedvc_prompt.wav")
    cross_wav = base + "_cross.wav"
    outdir = base + "_sv"

    begin_step(job, "准备音色参考",
               "从这个角色的素材里拼一条长参考（硬顶 25 秒：whisper 编码器最多 "
               "3000 帧，超了会崩），推理时它决定「唱出来是谁的声音」。")
    rc, txt = run(job, '"%s" -s "%s" prompt --vs "%s" --role "%s" --out "%s" --max-sec 25'
                  % (RVC_PY, os.path.join(STAGES, "seedvc_cli.py"), HERE, rid, prompt),
                  cwd=RVC_ROOT, timeout=900)
    if rc != 0:
        end_step(job, False, "参考没拼出来")
        raise Fail("拼音色参考失败（退出码 %d）。\n%s" % (rc, _tail(txt, 4)))
    end_step(job, True, "音色参考已就绪")

    begin_step(job, "交叉合成",
               "低频取 RVC 干声（音色与共振峰），高频取原唱（辅音与气声）—— "
               "直接拿 RVC 干声当输入会糊，直接拿原唱当输入会带原唱味。")
    rc, txt = run(job, '"%s" -s "%s" cross "%s" "%s" "%s" 3200 1200'
                  % (RVC_PY, os.path.join(STAGES, "seedvc_cli.py"),
                     rvc_vocal, src_vocal, cross_wav), cwd=RVC_ROOT, timeout=1800)
    if rc != 0 or not os.path.isfile(cross_wav):
        end_step(job, False, "交叉合成失败")
        raise Fail("交叉合成失败（退出码 %d）。\n%s" % (rc, _tail(txt, 4)))
    end_step(job, True, "已合成待转换的干声")

    secs = wav_seconds(cross_wav) or 0
    # 实测 225 秒的歌 2 分 10 秒（RTF 0.37），加约 1 分钟加载模型
    est = int(secs * 0.6 + 90) if secs else 600
    begin_step(job, "音色精修（Seed-VC 44.1k）",
               "用 %s 档位把上面那条干声重唱一遍，输出 44.1kHz。本机约 %d 分钟。"
               % (ck["label"], round(est / 60.0)))
    cmd = ('"%s" inference.py --source "%s" --target "%s" --output "%s" '
           '--diffusion-steps %d --length-adjust 1.0 --inference-cfg-rate %s '
           '--f0-condition True --auto-f0-adjust False --semi-tone-shift 0 '
           '--checkpoint "%s" --config "%s" --fp16 True'
           % (seedvc.py_path(), cross_wav, prompt, outdir,
              seedvc.DIFFUSION_STEPS, seedvc.CFG_RATE, ckpt,
              os.path.join(seedvc.SEEDVC_ROOT, seedvc.config_path(role))))
    rc, txt = run(job, cmd, cwd=seedvc.SEEDVC_ROOT, env=seedvc_env(True),
                  timeout=3600, on_line=lambda ln: _pct_from_line(job, ln),
                  heartbeat=(est, "44.1k 重唱中（本机约 %d 分钟）" % round(est / 60.0)))
    wavs = sorted(glob.glob(os.path.join(outdir, "*.wav")))
    if rc != 0 or not wavs:
        end_step(job, False, "精修失败")
        raise Fail("Seed-VC 精修失败（退出码 %d）。\n%s" % (rc, _tail(txt, 6)))
    shutil.copyfile(wavs[0], out_wav)
    end_step(job, True, "已出 44.1k 精修人声")
    return out_wav


# ============================ 音高体检（整首开唱之前先量一小段） ============================
#
# ★ 这一段是「唱歌越唱越差」的根治点，来龙去脉必须写清楚，否则后人会把阈值当拍脑袋的数字。
#
# 【现象】老板的原话：「唱歌也是越唱越差」。盘上同一个角色（大帝 ra500fb92）从 10-13
#   到 10-16 一共 16 版成品。按成品记录里的质检数排成时间线看着像在退化
#   （前半均值 90.8% → 后半 83.0%），**但那是假的** —— 那 16 版其实是 7 首歌，
#   早期几首本来就「好唱」。必须按**同一首歌**才比得出来。
#
# 【控制变量对照】Three Days Grace - Pain，202.9 秒：
#   **同一个 --input**（cache/sep/18a93a7e6ae28125/vocal.wav）、同一个模型
#   ra500fb92.pth、同一套参数（index-rate 0.50 / rms-mix 0.25 / protect 0.33）、
#   同一段质检窗口（studio 的 _qs=(202.9-30)*0.35 ≈ 60.5s 起 30 秒）。
#   只改 --pitch：
#         pitch  +0  → 宽松吻合 93.0%  八度错  3.4%
#         pitch -12  → 宽松吻合 81.2%  八度错 17.2%     ← 整八度这一档炸了
#         pitch  +3  → 宽松吻合 96.8%  八度错  1.4%
#   所以**不是代码随时间退化**（同一首歌前后 6 小时，同模型同参数）。
#
# 【但不能推广成「−12 一定坏」】换个歌就翻过来了 —— 同样是 pitch −12：
#        丹阳子（a4db67eb80575a06，193s）  → 单窗  2.1% / 整曲  8.3%
#        登阶（b772c3d0cf5cf4d3，201s）    → 单窗 18.4% / 整曲 10.6%
#        登阶(2)（be13d29deea8523c，201s） → 单窗 17.2% / 整曲 11.5%
#   ⚠ **两个口径会给同一首歌相反的名次**：丹阳子 −12 单窗只 2.1%（看着最干净），
#   整曲却是 8.3%、最差一窗 37.5%（比登阶还糟）；登阶 −12 单窗 18.4%（看着最糟），
#   整曲只 10.6%。**单窗那个数只回答「会不会在某一段炸」，不回答「整首有多坏」**。
#   整曲真值一律用 qc_singing_whole 多窗算（出处 `_probe_whole_qc_truth.py`，可重跑）。
#   同一个角色、同一个音高设定，整曲从 8.3% 到 11.5%。**逐首不同，只能实测。**
#   —— 这正是要「体检」而不是「定规则」的理由。
#
# 【为什么不在推理入口直接修】八度错的来源在 RVC 自己的 pipeline 里（f0 量化成 256 档
#   coarse pitch 交给模型，模型在低音区/谐波强的段落会翻八度）。f0 是在 pipeline
#   内部提的，**我们的入口拿不到、也不该去 patch 整合包**（见 rvc_infer.py 文件头）。
#   ⇒ 能做、且立刻有效的是：**开唱前把这一档音高实测一遍**，坏了就换一档（或者至少说清楚）。
#
# 【成本】一次体检最多 4 条候选 × 30 秒 RVC + 4 次质检。相对「整首十几分钟唱完才发现坏了」，
#   这是把试错成本从十几分钟压到一两分钟（和「试听小样」同一个思路，只是自动做掉）。
#
# 【★ 体检素材为什么要"从三个位置各取一段拼起来"】—— 真跑把我第一版打掉了：
#   第一版按"全曲能量最高"挑一段 24 秒。实测在登阶上它挑到第 124~148 秒，
#   那一段的 −12 只有 **2.9%** 八度错 ⇒ 判"没问题"；而产品自己那段（59.7~89.7 秒）
#   是 **18.4%**。**漏判**。根因是八度错在**同一首歌内部**变化极大 —— 同一版输出按
#   30 秒窗口扫，从 3.5% 一路到 16.9%（完整七窗数据见 AUDITION_PARTS 旁的注释）。
#   ⇒ 换成"把整首按位置等分成三段、每段取一小段拼成一条素材"：开销一样（每个候选
#     还是只起一次 RVC），但取样覆盖整首，结论不再取决于"碰巧挑到哪一段"。
#   教训归到铁律里：**判据的取样方式本身就是判据的一部分，取样不代表性 = 判据不成立**。
AUDITION = True             # 整首开唱前先体检；关掉就回到"设什么唱什么"
AUDITION_SECS = 30.0        # 体检素材总长 = 几个片段拼起来
AUDITION_PARTS = 3          # 从**几个不同位置**各取一段拼成体检素材（必须 >= 2，理由见下）
#   ★ 为什么必须多段拼、而不是挑一段：八度错在**同一首歌内部**变化极大。实测登阶
#     那一版（201 秒整首输出）按 30 秒窗口**等距扫七窗**：
#         窗口起点   0s    28s    57s    85s   114s   142s   171s
#         八度错   12.7%   8.2%  14.7%  16.1%   3.5%   5.6%  16.9%   （整曲加权 10.6%）
#     —— 同一首歌里从 3.5% 变到 16.9%。第一版按"全曲能量最高"挑单段，正好挑到
#     124~148 秒（那一带只有 2.9%），把一档**实际会炸**的音高判成"没问题"（漏判，
#     真跑当场打脸）。等分取样把整首盖住，结论才不取决于"挑到哪一段"。
#     （这串数的出处：`_probe_whole_qc_truth.py`，可重跑复现。）
AUDITION_MIN_SONG = 75.0    # 人声短于这个就不体检 —— 本来就只有一段，直接唱
AUDITION_OCTAVE_BAD = 0.11  # 八度错超过这个比例 ⇒ 这一档音高算"不达标"
#   ★ 0.11 比的是**体检量出来的数**（不是产品单窗数，也不是整曲数，三个口径都不一样！）。
#     体检口径实测（三段等分取样，每档起一次 RVC + 质检；同一档的区间是多次真跑的范围）：
#        丹阳子   −24→2.9~3.5%   −12→3.2~3.3%   +0→1.1~1.4%    （三档都不坏）
#        登阶     −24→13.2~16.1% −12→7.6~8.3%   +0→4.0~4.6%    （−24 明显坏）
#     好档最大观测 8.3%、坏档最小观测 13.2% ⇒ 阈值取两者之间、偏向居中的 11%。
#   ★ 为什么不是 12%（原来就是 12）：**体检值自己会抖**。同一份素材、同一套取样点，
#     登阶 −24 三次真跑分别量出 16.1% / 15.0% / 13.2% —— 跨度近 3 个百分点。
#     12% 离最坏观测（13.2%）只剩 1.2 个百分点，随时可能把"明显坏的档"漏成"没问题"
#     （漏判 = 用户白唱十几分钟，正是本功能要防的事）。11% 给坏档留 2.2、
#     给好档留 2.7，两侧都站得住。
#   ★ 整曲多窗真值（studio.qc_singing_whole，`_probe_whole_qc_truth.py` 出）另给一套，
#     用来核对体检有没有把方向判反：
#        丹阳子 +3→1.7%   −12→8.3%;   登阶 −12→10.6%;   登阶(2) −12→11.5%
#     ⚠ 注意：整曲口径下"好/坏"两簇离得比单窗口径**近得多**（11.5% vs 16.1%），
#     所以阈值余量没那么宽裕 —— 不要再引用"坏的全 ≥17%、隔着一个数量级"那种话，
#     那是**单窗**口径的说辞，已作废（详见 verify_sing_pitch.py 文件头的口径警告）。
AUDITION_MATCH_DROP = 0.05  # 改判后的音准吻合**不许比原来低超过 5 个百分点**
#   为什么除了八度错还要卡这一条：否则可能挑到"不翻八度、但整体唱得明显更差"的一档
#   （八度错是"翻八度"的专项指标，不代表整体音准好）。
AUDITION_BETTER = 0.6       # 设定档达标、但对照档八度错不到它的这个比例 ⇒ 值得提示
#   ★ 为什么要这条"软提示"：硬闸（0.12）只拦"明显坏"。实测登阶设定 −12 时体检量到
#     8.0%，没过 12% 的闸 ⇒ 不改判；可同一张表里 +0 只有 4.0%（好一倍），用户
#     永远看不到。软提示**不改音高**（那是用户的艺术决定），只在理由文字里点一句
#     "旁边那档更好，想更稳可以换它重唱对比"。
AUDITION_MAX_ABS = 24       # 候选音高的绝对值上限（再远就不合常理了）

# ---- 体检早停（2026-10-22 晚加）----
# 为什么：体检要逐档跑真 RVC + 真质检，每档约 23~24 秒（实测：推理 17.0s + 质检 6.7s）。
#   一首歌默认试 3 档（你设的那档 + ±12）⇒ 光体检就 70~71 秒，占了整条链路的一成多。
#   但绝大多数歌第 1 档就是好的（实测 +0 档：八度错 1.0~1.1%、宽松吻合 97.7%），
#   后面两档纯属"保险"——而这份保险在"设定档本来就好"的情况下买不到任何东西：
#   pick_audition 的规则是"设定档达标就不改判"，只可能附一句"旁边那档更稳"的提示，
#   而旁边那档要**八度错不到设定档的 0.6 倍**才配提（1.0% 时即 <0.6%，几乎不可能）。
# 所以只在"好到不可能被推翻"时才停，两条同时满足（缺一条都不停）：
#   ① 八度错 ≤ 阈值的一半（0.11 / 2 = 5.5%）—— 离坏档线还有一倍余量，
#      也远小于实测抖动（同一素材三次 13.2~16.1% 的那种抖，量级不同）；
#   ② 宽松吻合 ≥ 95% —— 光"不翻八度"不够，还得整体音准确实跟着原唱。
# ★ 只停**第 1 档之后**：第 1 档就是你设的那档。要是设定档不达标（比如 −12 量到 8%），
#   这条永远不触发，照旧试满 ±12 去挑达标档（登阶那个"−12 8.0% 而 +0 4.0%"就是靠它）。
AUDITION_EARLY_OCTAVE = 0.5   # ①的系数：八度错 ≤ 阈值 × 这个比例
AUDITION_EARLY_MATCH = 0.95   # ②的绝对线：宽松吻合 ≥ 这个值

# ---- 体检的"电音"判据（2026-10-24 加）--------------------------------------
# ★ 为什么必须加这一条：老板反馈「明显电音」两次，而老体检**只量音准**。
#   八度错对变调近乎纯平移 —— 你把 +12 设上去，音准照样 89.5%，体检说"没问题"；
#   可整八度会让 4-8k 能量翻 3.6 倍、谐波噪声比（HNR）从 14.9 涨到 19.0，
#   听感就是金属声。**量不出电音的体检等于替错误档位背书。**
#   实测（同一首歌、同一模型、只改音高）：
#       档位        HNR（原唱 9.14）  4-8k 占比（原唱 0.76%）
#       大帝 +3      10.86            0.95%     ← 正常
#       苹果嘉儿 +0  14.90            0.95%     ← 偏高
#       苹果嘉儿 +12 18.95            3.45%     ← 电音（老板听的就是这版）
#   ⇒ 两条判据各自独立成立：气声被抹掉（HNR 涨）和 高频堆积（4-8k 涨）。
#     任一条超线就判"这一档有金属声"，宁可多说一句，也别再替它背书。
AUDITION_METAL_HNR_DB = 4.0   # HNR 比原唱高这么多 dB ⇒ 判电
AUDITION_METAL_HF_REL = 2.0   # 4-8k 占比达到原唱的这么多倍 ⇒ 判电


def _metal_batch(job, wavs):
    """一次量几条轨的电音指标（HNR / 4-8k 占比）。返回 [{file, hnr, hf}, ...]。

    ★ 为什么"一次量全部"而不是逐条量：这几条数只有**放在同一次调用里横着比**
      才有意义（取样窗口必须相同）—— 不同次调用的数字不能互证，等于没量。
    """
    out_json = os.path.join(os.path.dirname(os.path.abspath(wavs[0])) or DATA,
                            "_metal.json")
    try:
        rc, _t = run(job, '"%s" -s "%s" "%s" 0 999 %s'
                     % (RVC_PY, os.path.join(STAGES, "artifact_cli.py"),
                        out_json, " ".join('"%s"' % w for w in wavs)),
                     cwd=RVC_ROOT, timeout=900)
        if rc != 0 or not os.path.isfile(out_json):
            return []
        with open(out_json, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    finally:
        with contextlib.suppress(Exception):
            os.remove(out_json)
    return [d for d in (data or []) if d.get("ok")]


def metal_verdict(base, cand):
    """纯逻辑：这一档音高"电不电"。返回 (是否电, HNR 差 dB, 4-8k 倍数)。

    抽成纯函数是为了门禁能直接喂数字验分支，不用真跑 RVC。
    """
    try:
        dh = float(cand.get("hnr_db")) - float(base.get("hnr_db"))
    except (TypeError, ValueError):
        dh = None
    try:
        rb = float(base.get("hf_ratio"))
        rc = float(cand.get("hf_ratio"))
        rel = (rc / rb) if rb > 1e-9 else None
    except (TypeError, ValueError):
        rel = None
    # ★ 4-8k 那条不能单独成立：实测降八度（−12）时 HNR 反而比原唱**低** 4.4dB
    #   （气声没被抹掉），可因为低频能量整体下移，4-8k 的占比照样翻 3 倍 ——
    #   只看占比会把"降调"误判成"电音"。所以高频那条必须**配合**一点气声丢失
    #   （HNR 至少也涨了 1dB）才判电。
    bad = ((dh is not None and dh >= AUDITION_METAL_HNR_DB)
           or (rel is not None and rel >= AUDITION_METAL_HF_REL
               and dh is not None and dh >= 1.0))
    return bool(bad), dh, rel


def _audition_should_stop(rows, i, total, requested):
    """体检能不能早停：试完第 1 档（且第 1 档就是你设的）且它好到不可能被推翻。

    单独抽成函数是为了门禁能直接测（不必真跑 RVC 才能验这条规则）。
    """
    try:
        if i != 0 or int(total) <= 1:
            return False
        r = (rows or [{}])[0]
        if int(r.get("pitch")) != int(requested):
            return False            # 第 1 档不是设定档 ⇒ 这条规则不适用
        o, m = r.get("octave"), r.get("match")
        if o is None or m is None:
            return False            # 没量出来不算"好"，宁可继续试
        # ★ 这一档**会电**就不能早停：早停的前提是"好到不可能被推翻"，
        #   而会电的档恰恰会被"不电的档"推翻（实测 +12 音准 98.6% 却电得厉害）。
        #   停在这一档 = 永远找不到能推荐给用户的替代档，指路功能直接作废。
        if r.get("metal"):
            return False
        return (float(o) <= AUDITION_OCTAVE_BAD * AUDITION_EARLY_OCTAVE
                and float(m) >= AUDITION_EARLY_MATCH)
    except (TypeError, ValueError):
        return False


# ---- 大跨度变调的失真提醒（2026-10-24 加）----
# 为什么：用户自己在界面上把音高设成 +12，唱出来"明显电音"，而系统全程只说
#   "体检通过：八度错 0.0%、宽松吻合 98.9%"—— 那个体检只量**音高准不准**，
#   而 RVC 的变调对 f0 来说几乎就是纯平移，任何一档都能轻轻松松过。它压根
#   没有"听起来电不电"这一维，于是 +12 这种必然失真的档位被判成"没问题"。
#
# 实测依据（苹果嘉儿 rbe0d5b48 + Old Town Road；同一模型、同一套
#   index 0.50 / rms 0.25 / protect 0.33 / f0 rmvpe，**只改 pitch**）：
#      档位      谐波噪声比   相邻周期相似度   逐周期微扰   高频(4~8k)占比
#      原唱真人     9.56 dB       0.692         9.92%        0.97%
#      pitch  +0   14.90 dB       0.900         6.29%        0.95%
#      pitch +12   18.95 dB       0.907         5.16%        3.45%   ← 3.6 倍
#   ⇒ 一个八度把 4~8kHz 的能量推到 3.6 倍，周期相似度也从真人的 0.69 拉到 0.91
#     （波形被"周期复印"）。这就是金属声 / 电音的客观形态。
#   ⇒ 而且**改参数救不回来**：同一 материал 上 index-rate 0.50→0.20→0.00 三条
#     分别是 19.84 / 20.67 / 20.69 dB（更糟），protect 0.33→0.45 也没差别。
PITCH_WARN_FROM = 6     # |半音| ≥ 这个开始有失真（实测： greens result）
PITCH_BAD_FROM = 11     # |半音| ≥ 这个是整整一个八度，必然明显失真


def pitch_shift_warn(pitch):
    """返回 (等级, 文案)。等级是 "" / "warn" / "bad"。

    抽成函数而不是直接写在 Sing Songs 流程里：门禁要能不跑 RVC 就验这条规则
    （见 verify_sing_pitch 里新加的判据）。
    """
    try:
        v = abs(int(pitch))
    except (TypeError, ValueError):
        return "", ""
    if v >= PITCH_BAD_FROM:
        return "bad", (
            "这一档是整整 %d 个八度。实测同一素材：高频能量会变成不变调时的 3.6 倍，"
            "相邻周期相似度从真人的 0.69 拉到 0.91（波形被周期复印）—— 听上去就是明显"
            "的金属声/电音，而且**调参数救不回来**（index-rate 0.50→0 反而更糟）。"
            "这首歌和这个角色的音区本来就差这么多：换一个音区接近的角色，"
            "比硬拉八度好得多。" % (v // 12))
    if v >= PITCH_WARN_FROM:
        return "warn", ("跨度 %d 半音已超过半个八度，齿音和金属感会从这里开始冒头。" % v)
    return "", ""


def _rms_blocks(path, block_sec=1.0, max_blocks=1200):
    """按秒算粗略能量包络（标准库，不依赖 numpy）。

    为什么不用 wav_rms：它把**整个文件**读进内存，200 秒立体声要 70MB 字节；
    这里只按块累加，读一块丢一块。
    返回 [(块起点秒, 块内平均绝对值)]，读不了就返回 []。
    """
    import array

    try:
        w = wave.open(path, "r")
    except Exception:
        return []
    try:
        ch = w.getnchannels()
        sw = w.getsampwidth()
        fr = w.getframerate()
        if sw != 2 or ch < 1:       # 只处理 16-bit PCM；其余交给调用方降级
            return []
        per = max(1, int(fr * block_sec))
        out = []
        idx = 0
        while len(out) < max_blocks:
            raw = w.readframes(per)
            if not raw:
                break
            a = array.array("h")
            try:
                a.frombytes(raw)
            except Exception:
                break
            if not a:
                break
            acc = 0
            step = max(1, len(a) // 4000)
            cnt = 0
            for i in range(0, len(a), step):
                acc += abs(a[i])
                cnt += 1
            out.append((idx * block_sec, (acc / max(cnt, 1)) / 32768.0))
            idx += 1
    finally:
        with contextlib.suppress(Exception):
            w.close()
    return out


def _pick_excerpt(path, secs, block_sec=1.0):
    """在整首里挑一段「最能代表这一段区间」的片段：给定区间内能量最高的连续窗口。

    ★ 为什么按能量挑、而不是像试听小样那样固定取 35%：高能量处通常是副歌，音高最
      明确、也最容易被听出来；静音/留白段量不出音准。
    返回 [(起点秒, 该窗口平均能量)]，整首返回一个元素；估不出来返回 []。
    """
    blocks = _rms_blocks(path, block_sec)
    if not blocks:
        return []
    n = len(blocks)
    nb = max(1, int(round(secs / block_sec)))
    if n <= nb:
        return [(0.0, sum(e for _, e in blocks) / max(1, n))]
    best, best_e = 0, -1.0
    for s in range(0, n - nb + 1):
        e = sum(v for _, v in blocks[s:s + nb]) / float(nb)
        if e > best_e:
            best, best_e = s, e
    return [(best * block_sec, best_e)]


def _montage_starts(path, total_secs, parts):
    """把整首按位置等分成 parts 个区，每个区里挑能量最高的一小段 —— 返回起点秒列表。

    ★ 这是"一段代表不了整首"的解法（踩坑记录见 AUDITION_PARTS 的注释）：
      等分保证**每一段歌曲都被看到**，而不是被"全曲能量最高处"一个点代表。
      每个区内再挑能量最高处，是为了避开区内的留白/气口 —— 那里量不出音准。
    """
    blocks = _rms_blocks(path, 1.0)
    if not blocks:
        return []
    n = len(blocks)
    part_secs = total_secs / float(parts)
    nb = max(1, int(round(part_secs)))
    lo, hi = int(n * 0.05), int(n * 0.95)
    if hi - lo < nb * parts:                 # 太短，退回"整首挑一段"
        p = _pick_excerpt(path, total_secs)
        return [s for s, _ in p]
    zone = (hi - lo) / float(parts)
    starts = []
    for k in range(parts):
        z0 = int(lo + k * zone)
        z1 = int(min(hi, lo + (k + 1) * zone))
        last = max(z0, z1 - nb)
        best, best_e = z0, -1.0
        for s in range(z0, last + 1):
            e = sum(v for _, v in blocks[s:s + nb]) / float(nb)
            if e > best_e:
                best, best_e = s, e
        starts.append(float(best))
    return starts


def _montage(vocal_wav, out_wav, total_secs, parts, fade_ms=20.0):
    """把等分取样出来的几段拼成**一条**体检素材（带 20ms 淡入淡出，接缝不出咔哒）。

    为什么拼成一条、而不是分别跑：每个候选音高只起一次 RVC、一次质检，
    取样覆盖了整首，而开销和"只跑一段"一样。
    只用标准库（wave+array）：切片素材/分离出来的人声都是 16-bit PCM。
    返回 (起点秒列表, 说明)；读不了就返回 ([], 原因)，由调用方退化成单段裁剪。
    """
    import array

    starts = _montage_starts(vocal_wav, total_secs, parts)
    if not starts:
        return [], "读不了能量包络（不是 16-bit PCM？）"
    part_secs = total_secs / float(parts)
    try:
        w = wave.open(vocal_wav, "r")
    except Exception as e:                       # noqa: BLE001
        return [], "打不开干声：%s" % e
    try:
        ch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        if sw != 2 or ch < 1:
            return [], "不是 16-bit PCM（sw=%d ch=%d）" % (sw, ch)
        nf = max(1, int(sr * fade_ms / 1000.0))
        with wave.open(out_wav, "w") as o:
            o.setnchannels(ch)
            o.setsampwidth(2)
            o.setframerate(sr)
            for s0 in starts:
                w.setpos(int(s0 * sr))
                raw = w.readframes(int(part_secs * sr))
                a = array.array("h")
                a.frombytes(raw)
                n = len(a) // ch
                k = min(nf, n // 2)
                for i in range(k):               # 淡入 / 淡出，接缝不咔哒
                    g = i / float(k)
                    for c in range(ch):
                        a[i * ch + c] = int(a[i * ch + c] * g)
                        j = (n - 1 - i) * ch + c
                        a[j] = int(a[j] * g)
                o.writeframes(a.tobytes())
    finally:
        with contextlib.suppress(Exception):
            w.close()
    return starts, "取 %d 段 × %.0f 秒" % (len(starts), part_secs)


def pick_audition(cands, requested):
    """纯逻辑：从体检结果里决定**用哪一档音高整首唱**。返回 (音高, 是否改判, 理由)。

    cands 是 [{"pitch": int, "match": float|None, "octave": float|None}, ...]，
    match = 宽松吻合率(0~1)，octave = 八度错比例(0~1)；没跑通的那档是 None。

    ★ 为什么抽成纯函数：这是整段功能里唯一有"判断"的地方，抽出来才能用构造数据
      在门禁里把每条分支逐条走一遍（真跑一遍 RVC 要几十秒，走不了几条分支）。
    ★ 规则故意只有三步：
      ① 用户设的那档**达标**（八度错 ≤ AUDITION_OCTAVE_BAD）⇒ 原样用，不改判。
      ② 但达标**不等于好听**：同一档可能电音超标（见 AUDITION_METAL_* 的实测），
         这时必须点出来，并把"不电的那一档"指给用户 —— 仍然不改判，只指路。
      ③ 不达标 ⇒ 在"达标"的档里挑**离用户设定最近**的那一档（且不电的优先）；
         一个达标的都没有，就维持原设定，并把最好的一档报出来让用户自己决定。
    ★ 为什么"最近"而不是"最好"：用户设 −12 是想把声音压低。候选里 −12 炸了、
      0 和 +3 都好，那 0 离他的意图更近（就是 −12 的正上方一个八度），+3 更远。
      按"八度错最低"去挑会挑到 +3，等于把用户想要的"压低"改成"升调" ——
      那是替用户做艺术决定，不该由我们做。
      （实测 Pain 单窗：−12 是 17.2%、0 是 3.4%、+3 是 1.4%。按"最好"会挑 +3。）
    ★ 为什么不抛异常、什么情况都返回一个音高：体检是锦上添花，任何一档没量出来
      都必须能退化成"按用户设的唱"，绝不能让唱歌失败。
    """
    if not cands:
        return int(requested), False, "没有拿到任何体检数据，按你设的 %+d 唱" % int(requested)
    by = {}
    for c in cands:
        with contextlib.suppress(Exception):
            by[int(c.get("pitch"))] = c
    base = by.get(int(requested))
    if base is None or base.get("octave") is None:
        return int(requested), False, "你设的 %+d 这一档没量出来，按它唱" % int(requested)
    bo = float(base["octave"])
    bm = float(base.get("match") or 0.0)
    if bo <= AUDITION_OCTAVE_BAD:
        # 设定档达标 ⇒ **一定不改判**（用户设 −12 是想压低，替他把音高抬起来是越权）。
        # 但若旁边有档明显更稳（八度错不到它 AUDITION_BETTER 倍），值得点出来 ——
        # 实测登阶：设定 −12 体检 8.0%（达标），可同一张表里 +0 只有 4.0%。
        # 不提示的话用户永远看不到那一档。只写进理由文字，音高照旧按他设的。
        better = []
        for c in cands:
            if c.get("octave") is None:
                continue
            try:
                p, o = int(c.get("pitch")), float(c["octave"])
            except (TypeError, ValueError):
                continue
            if p == int(requested) or abs(p) > AUDITION_MAX_ABS:
                continue
            if o > bo * AUDITION_BETTER:
                continue
            if c.get("match") is not None and float(c["match"]) < bm - AUDITION_MATCH_DROP:
                continue
            better.append((abs(p - int(requested)), p, o))
        # ★★ 设定档音准达标、但**电音超标** ⇒ 必须点出来。
        #    以前这里直接回"这一档没问题"，于是 +12（八度错 0.0% 却电得厉害）
        #    被体检背书，用户完全不知道是音高的问题 —— 这是最贵的一类静默。
        #    仍然**不改判**（用户设 −12 是想压低，替他改是越权），但要把
        #    "不电的那一档"指出来，让他一按就能换。
        if base.get("metal"):
            clean, weaker = [], []
            for c in cands:
                if c.get("metal") or c.get("octave") is None:
                    continue
                try:
                    p, o = int(c.get("pitch")), float(c["octave"])
                except (TypeError, ValueError):
                    continue
                if p == int(requested) or abs(p) > AUDITION_MAX_ABS:
                    continue
                if o > AUDITION_OCTAVE_BAD:
                    continue
                if c.get("match") is not None and float(c["match"]) < bm - AUDITION_MATCH_DROP:
                    # ★ 不电但音准掉太多：不推荐，但要**照实说**（否则文案会变成
                    #   "试过的几档都会电"，可明明有一档不电 —— 那就是骗人）。
                    weaker.append((abs(p - int(requested)), p,
                                   float(c["match"]), o))
                    continue
                clean.append((abs(p - int(requested)), p, o))
            dh = base.get("hnr_delta")
            rel = base.get("hf_rel")
            what = []
            if dh is not None and dh >= AUDITION_METAL_HNR_DB:
                what.append("气声被抹掉（HNR 比原唱高 %.1f dB）" % dh)
            if rel is not None and rel >= AUDITION_METAL_HF_REL:
                what.append("4-8k 能量是原唱的 %.1f 倍" % rel)
            if clean:
                clean.sort(key=lambda x: (x[0], x[2]))
                _, cp, co = clean[0]
                return int(requested), False, (
                    "音准没问题，但**这一档会有金属声**：%s。"
                    "想不电就换 %+d（八度错 %.1f%%、不电）—— 没替你改，仍按 %+d 唱"
                    % ("、".join(what) or "指标超线", cp, co * 100, int(requested)))
            if weaker:
                weaker.sort(key=lambda x: (x[0], -x[2]))
                _, wp, wm, wo = weaker[0]
                return int(requested), False, (
                    "音准没问题，但**这一档会有金属声**：%s。"
                    "不电的只有 %+d，可它的吻合会从 %.1f%% 掉到 %.1f%%（掉 %.1f 个点）——"
                    "要不要换你自己权衡（没替你改，仍按 %+d 唱）"
                    % ("、".join(what) or "指标超线", wp, bm * 100, wm * 100,
                       (bm - wm) * 100, int(requested)))
            return int(requested), False, (
                "音准没问题，但**这一档会有金属声**：%s。"
                "试过的几档要么也电、要么音准掉太多，只能换音区更近的角色（仍按 %+d 唱）"
                % ("、".join(what) or "指标超线", int(requested)))
        if better:
            better.sort(key=lambda x: (x[0], x[2]))
            _, bp2, bo2 = better[0]
            return int(requested), False, (
                "八度错 %.1f%%（阈值 %.0f%%）—— 这一档没问题；不过 %+d 只有 %.1f%%，"
                "想更稳可以换它重唱一版对比（没替你改，仍按你设的 %+d 唱）"
                % (bo * 100, AUDITION_OCTAVE_BAD * 100, bp2, bo2 * 100, int(requested)))
        return int(requested), False, (
            "八度错 %.1f%%（阈值 %.0f%%）—— 这一档没问题" % (bo * 100, AUDITION_OCTAVE_BAD * 100))
    pool, pool_metal = [], []
    for c in cands:
        if c.get("octave") is None:
            continue
        try:
            p, o = int(c.get("pitch")), float(c["octave"])
        except (TypeError, ValueError):
            continue
        if p == int(requested) or abs(p) > AUDITION_MAX_ABS or o > AUDITION_OCTAVE_BAD:
            continue
        # 吻合不许比原来差太多（原来没量出来就不卡这条）
        if c.get("match") is not None and float(c["match"]) < bm - AUDITION_MATCH_DROP:
            continue
        # ★ 改判时**优先挑不电的档**：音准同等达标的情况下，会电的那一档
        #   听感差得多（实测 +12 与 +0 音准都在 89% 上下，听感天差地别）。
        (pool if not c.get("metal") else pool_metal).append(c)
    if not pool:
        # 试过的档都会电 ⇒ 退而求其次，按音准挑（总比跑调强）
        pool = pool_metal
    if not pool:
        others = [c for c in cands
                  if c.get("octave") is not None and int(c.get("pitch")) != int(requested)]
        if others:
            b = min(others, key=lambda c: float(c["octave"]))
            return int(requested), False, (
                "你设的 %+d 八度错 %.1f%% 偏高，但对照档都没达标（最好的是 %+d：%.1f%%）"
                " ⇒ 仍按 %+d 唱，你可以自己换一档再试"
                % (int(requested), bo * 100, int(b["pitch"]), float(b["octave"]) * 100,
                   int(requested)))
        return int(requested), False, (
            "你设的 %+d 八度错 %.1f%% 偏高，但没有可用的对照档" % (int(requested), bo * 100))
    best = sorted(pool, key=lambda c: (abs(int(c["pitch"]) - int(requested)),
                                       float(c["octave"]),
                                       -float(c.get("match") or 0.0)))[0]
    bp = int(best["pitch"])
    return bp, True, (
        "你设的 %+d 有 %.1f%% 的帧翻八度（不达标）；离它最近且达标的 %+d 只有 %.1f%%，"
        "吻合 %.1f%% → %.1f%% ⇒ 已改用 %+d"
        % (int(requested), bo * 100, bp, float(best["octave"]) * 100,
           bm * 100, float(best.get("match") or 0.0) * 100, bp))


def _audition_candidates(requested):
    """要试哪几档音高：你设的那档 + 相邻八度(±12) + 0，去重、限幅。"""
    out = [int(requested)]
    for p in (int(requested) - 12, int(requested) + 12, 0):
        if abs(p) <= AUDITION_MAX_ABS and p not in out:
            out.append(p)
    return out


def audition_pitch(job, vocal_wav, requested, rvc_model, idx, index_rate,
                   rms_mix_rate=None, protect=None):
    """开唱前的音高体检：拿一小段真唱几档音高，量出来再决定整首用哪档。

    返回 dict（给日志/产物用）：
      {"start","secs","cands":[{pitch,match,octave}],"chosen","changed","reason","skipped"}
    任何一步不通都不抛异常 —— 体检失败最多退化成"按你设的唱"，不能把唱歌带死。
    """
    res = {"start": 0.0, "starts": [], "secs": 0.0, "montage": "", "cands": [],
           "chosen": int(requested), "changed": False, "reason": "未体检", "skipped": None}
    try:
        total = wav_seconds(vocal_wav) or 0.0
        if not AUDITION:
            res["skipped"] = "体检已关闭"
            return res
        if total < AUDITION_MIN_SONG:
            res["skipped"] = ("人声只有 %.0f 秒（< %.0f 秒），直接唱不体检"
                              % (total, AUDITION_MIN_SONG))
            return res
        secs = min(AUDITION_SECS, total)
        parts = max(1, int(AUDITION_PARTS))
        while parts > 1 and secs / parts < 4.0:     # 每段至少 4 秒才有意义
            parts -= 1
        work = os.path.join((job or {}).get("dir") or DATA, "_audition")
        os.makedirs(work, exist_ok=True)
        clip = os.path.join(work, "probe.wav")
        starts, why = _montage(vocal_wav, clip, secs, parts)
        if not starts:
            # 退化：拼不了就退回"裁一段"（旧路）。体检宁可弱一点，也不能整个不跑。
            pick = _pick_excerpt(vocal_wav, secs)
            s0 = pick[0][0] if pick else 0.0
            s0 = max(0.0, min(s0, max(0.0, total - secs)))
            ok, _ = clip_audio(job, vocal_wav, clip, s0, secs)
            if not ok:
                res["skipped"] = "体检素材裁剪失败"
                return res
            starts, why = [s0], "退化成单段（%s）" % why
        res["starts"] = [round(x, 1) for x in starts]
        res["start"] = res["starts"][0]
        res["secs"] = secs
        res["montage"] = why

        cands = _audition_candidates(requested)
        for p in cands:
            c_out = os.path.join(work, "cand_%+d.wav" % p)
            cmd = ('"%s" -s "%s" --model "%s" --input "%s" --output "%s" '
                   '--f0 %s --pitch %d --index-rate %s %s --index "%s"'
                   % (RVC_PY, os.path.join(STAGES, "rvc_infer.py"), rvc_model,
                      clip, c_out, RVC_F0_METHOD, int(p), index_rate,
                      _rvc_quality_args(rms_mix_rate, protect), idx or ""))
            rc, _out = run(job, cmd, cwd=RVC_ROOT, timeout=1800)
            row = {"pitch": int(p), "match": None, "octave": None}
            if rc != 0 or not os.path.exists(c_out):
                log(job, "    体检 %+d 半音：推理没跑通（退出码 %d）" % (p, rc))
                res["cands"].append(row)
                continue
            # ★ 质检要拿**裁好的片段**（clip）跟候选产物比，不能再传 start/secs：
            #   候选产物**本身就是那一段**（24 秒），而 qc_cli 会把 start/secs 同时套到
            #   两条轨上 —— 再套一次 33~57 秒，切出来是空的，qc_cli 直接报
            #   「音频太短（0.00 秒）」退出码 1（实测踩到）。
            #   两条轨都是同一段、时间轴都从 0 开始，所以整段比就是对的。
            d = qc_singing(job, clip, c_out)
            if d:
                row["match"] = d.get("match_ratio_loose")
                row["octave"] = d.get("octave_err_ratio")
                row["contour"] = d.get("contour_corr")
            res["cands"].append(row)
            # ★ 电音必须**在这里量**，不能挪到循环结束之后统一量：
            #   早停判断就在下面两行，而"会电就不能早停"这条规则要看 metal 字段。
            #   挪到后面 ⇒ 早停时 metal 还是 None ⇒ 规则永远不触发 ⇒
            #   只试了设定档就停下，指路功能直接作废（实测踩到：+12 之后没再试任何档）。
            base_m, cand_m = None, None
            for m in _metal_batch(job, [clip, c_out]):
                if os.path.basename(m.get("file") or "") == os.path.basename(clip):
                    base_m = m
                else:
                    cand_m = m
            if base_m and cand_m:
                bad, dh, rel = metal_verdict(base_m, cand_m)
                row.update({"metal": bad, "hnr": cand_m.get("hnr_db"),
                            "hnr_delta": (round(dh, 2) if dh is not None else None),
                            "hf_rel": (round(rel, 2) if rel is not None else None)})
            log(job, "    体检 %+d 半音：八度错 %s，宽松吻合 %s%s"
                % (p,
                   ("%.1f%%" % (row["octave"] * 100)) if row["octave"] is not None else "-",
                   ("%.1f%%" % (row["match"] * 100)) if row["match"] is not None else "-",
                   ("，气声 HNR 比原唱高 %s dB、4-8k 是原唱的 %s 倍%s"
                    % (row.get("hnr_delta"), row.get("hf_rel"),
                       " ⇒ **有金属声**" if row.get("metal") else ""))
                   if row.get("hnr_delta") is not None else ""))
            # ★ 早停：第 1 档好到不可能被推翻 ⇒ 剩下几档不试了。
            #   必须**说出来**省了多少，否则用户只会觉得"体检怎么这么快"（或者以为跳过了）。
            if _audition_should_stop(res["cands"], len(res["cands"]) - 1,
                                     len(cands), requested):
                left = len(cands) - len(res["cands"])
                log(job, "    体检早停：第 1 档 %+d 八度错 %.1f%%（不到阈值 %.0f%% 的一半）、"
                         "吻合 %.1f%%，剩下的 %d 档不试了（省约 %d 秒）"
                    % (int(p), row["octave"] * 100, AUDITION_OCTAVE_BAD * 100,
                       row["match"] * 100, left, int(round(left * 23.7))))
                break
        chosen, changed, why = pick_audition(res["cands"], requested)
        res.update({"chosen": chosen, "changed": changed, "reason": why})
        return res
    except Exception as e:      # 体检是"锦上添花"，绝不能把唱歌带死
        res["skipped"] = "体检异常（已忽略，按你设的唱）：%s" % e
        return res


def rvc_convert(job, model_name, idx, input_wav, out_wav, pitch, index_rate,
                rms_mix_rate=None, protect=None, heartbeat=None,
                chunk=None):
    """跑一次 RVC 推理。翻唱 / 多角色对唱 / 试听都走这里 —— **只此一份**。

    为什么要抽出来：下面这条命令行的第 ② 点很容易在复制时丢掉，而两处各自
    维护一份，早晚会漂（漂了以后症状是"某条链路明明能用、另一条报索引错"）。

    ★ 走自己的入口（stages/rvc_infer.py）而不是整合包自带的 `infer.cli`：
      ① CLI 的 --f0-method 只放开 pm / rmvpe，**FCPE 被挡在外面**；
         而实测 FCPE 把逐帧音高偏差从 29 音分收紧到 23（见文件头的参数表）。
      ② 需要显式指定 index，才能在"索引真不可用"时明确降级、说清原因。
    语义与 CLI 一致：输出已存在就覆盖 —— 小样用的是固定名 preview_latest，
    不覆盖的话第二次试听必然失败（CLI 原版遇到已存在文件是直接退出码 1）。
    """
    c = ('"%s" -s "%s" --model "%s" --input "%s" --output "%s" '
         '--f0 %s --pitch %d --index-rate %s %s --index "%s" --chunk %s'
         % (RVC_PY, os.path.join(STAGES, "rvc_infer.py"), model_name,
            input_wav, out_wav, RVC_F0_METHOD, int(pitch), index_rate,
            _rvc_quality_args(rms_mix_rate, protect), idx or "",
            RVC_CHUNK_SEC if chunk is None else float(chunk)))
    # ★ 显卡闸门：RVC 推理整段独占显卡（同一时刻只允许一个，理由见 gpu_slot 处）。
    with gpu_slot(job, "歌声转换（RVC）"):
        return run(job, c, cwd=RVC_ROOT, timeout=3600,
                   on_line=lambda ln: _pct_from_line(job, ln), heartbeat=heartbeat)


def svc_convert(job, svc, input_wav, out_wav, pitch=0, device=None):
    """第二套引擎：so-vits-svc 4.0 推理（子进程走 stages/svc_cli.py）。

    为什么返回口径要和 rvc_convert 一样：上层 sing_role 里那套"失败降级/步骤条/
    质检"逻辑不该为两套引擎各写一份（两份早晚会漂）。所以这里同样返回 (rc, 文本)。

    为什么权重路径放在角色对象里（role["svc"]）：同一张角色卡可以同时挂
    RVC 权重和 SVC 权重，用 engine 选哪套唱 —— 这正是"两套并存"要的形态，
    而不是给同一个角色建两张卡。
    """
    svc = svc or {}
    model = svc.get("model") or ""
    cfg = svc.get("config") or ""
    km = svc.get("kmeans") or ""
    if not model or not os.path.isfile(model):
        return 1, "这个角色没有 SVC 权重（engine=svc 时必须先挂上）"
    if not cfg or not os.path.isfile(cfg):
        return 1, "这个角色的 SVC 配置不在：%s" % cfg

    # ---- ① RVC 打底（有就用）：先把音准做对，再换音色 ----
    # ★★ 2026-10-27 实测（同一段 15 秒、同一角色）：
    #     纯 SVC  吻合 66.1% / 八度错 20.3% / 起伏比 2.36
    #     纯 RVC  吻合 91.8% / 八度错  2.1% / 起伏比 1.01
    #     RVC→SVC 吻合 87.9% / 八度错  2.3% / 起伏比 0.76  ← 现在默认这条
    #   纯 SVC 的模型自己跟不准旋律（八度乱跳）—— 那正是「杂乱、吵闹」的来源。
    src = input_wav
    base = (svc.get("rvc_base") or "").strip()
    mid = os.path.join(os.path.dirname(out_wav) or ".", "_svcbase.wav")
    if base and os.path.isfile(os.path.join(RVC_ROOT, "assets", "weights", base)):
        log(job, "  SVC 引擎：先用 RVC 打好音准底（实测八度错 20.3% → 2.3%），"
                 "再用 SVC 转音色 —— 纯 SVC 自己跟不准旋律，那就是「杂乱」的来源")
        rc0, out0 = rvc_convert(job, base, svc.get("rvc_index") or "",
                                input_wav, mid, pitch, RVC_INDEX_RATE,
                                RVC_RMS_MIX_RATE, RVC_PROTECT)
        if rc0 == 0 and os.path.isfile(mid):
            src = mid
        else:
            log(job, "  [warn] RVC 打底没成功（退出码 %d），这次退回纯 SVC（音准会差一截）"
                % rc0)
    else:
        log(job, "  [提示] 这张卡没挂 RVC 打底权重 ⇒ 纯 SVC 演唱，"
                 "音准（八度错约 20%）会明显不如 RVC 版")

    # ★★ 移调只能吃一次：RVC 打底那一步已经把 pitch 唱进去了，SVC 这一步必须给 0，
    #   否则 +12 会变成 +24（实测才发现 —— 音高体检挑的也是同一档，会一起翻倍）。
    svc_pitch = 0 if src != input_wav else int(pitch)
    c = ('"%s" -s "%s" --model "%s" --config "%s" %s --input "%s" --output "%s" '
         '--pitch %d --device %s --report "%s"'
         % (PY, os.path.join(STAGES, "svc_cli.py"), model, cfg,
            ('--kmeans "%s"' % km) if km else "",
            src, out_wav, svc_pitch, device or "cuda:0",
            os.path.join(os.path.dirname(out_wav) or ".", "_svc.json")))
    if svc_pitch != int(pitch):
        log(job, "  SVC 这步不再移调（移调已由 RVC 打底完成：%+d 半音）" % int(pitch))
    # ★ 与 RVC 共用同一个显卡闸门：两套引擎都是独占整卡的推理，同时开必然爆显存。
    with gpu_slot(job, "歌声转换（SVC）"):
        rc, txt = run(job, c, cwd=HERE, timeout=3600)
    if src != input_wav:
        with contextlib.suppress(Exception):
            os.remove(mid)
    return rc, txt



def sing_role(job, rid, song_path, pitch=0, index_rate=None, model=None, mix=True,
              sep_model=None, rms_mix_rate=None, protect=None, prefix="sing",
              save=True, gate=True, pre_sep=None, output_kind="singing", label=None,
              qc=True, lyric=None, nonvocal="keep", engine="rvc",               lyric_nfe=None,
              lyric_seg_offset=0.0, lyric_seg_dur=None, hf="auto",
              demetal_mode="auto", presence_mode="auto",
              pre_skip=None, post_skip=None, pre_mode=None, lead=None,
              whistle=None):
    """
    让角色唱歌：**先分离人声** -> RVC 只对干声变声 -> 用伴奏轨混音。

    lyric_seg_offset/dur = 改词时间戳的偏移（试听小样用，见 lyric_segments_for）。

    ★ 为什么必须先分离（「唱出来没法听」的根治点，不是调参能救的）：
      RVC 是「人声 → 人声」的模型，靠 f0（基频）曲线工作。把整首歌直接喂进去，
      鼓和贝斯也会被它当成"唱"，输出的音高跟着鼓点乱跑；更要命的是混音时
      如果拿**原曲**当伴奏，原唱人声会和角色一起唱 —— 实测成品波形与原曲的
      相关系数 0.870、原曲振幅占比 0.538，也就是成品里一大半是原唱。
      正确顺序：原曲 →(UVR5)→ 人声轨 + 伴奏轨；RVC 只对**人声轨**变声；
      再用**伴奏轨**（不含原唱）混合。

    角色自己没训过 RVC 模型时，不会偷偷拿别人的模型冒充 —— 会直接停下来说明原因，
    因为「用了别人的音色」这种事必须让用户知道。

    pitch 可以是整数半音，也可以是 "auto"（按原唱与角色的音域差自动估计）。
    rms_mix_rate / protect 是 RVC 的音质参数，见 _rvc_quality_args() 的说明。

    输出两条轨，分开存：
      voice —— 变声后的人声（干声），后续还能重新编曲/混音
      mix   —— 伴奏 + 角色人声的成品，直接能听能发
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    if index_rate is None:
        index_rate = RVC_INDEX_RATE
    index_rate = float(index_rate)
    rvc_model = model or (role.get("rvc") or {}).get("model")
    if not rvc_model and engine != "svc":
        raise Fail(
            "这个角色还没有歌声模型，没法唱歌。\n"
            "歌声模型（RVC）和说话模型（GPT-SoVITS）是两套东西：说话模型只能照着文本念，"
            "唱不出来。需要先用这个角色的干声素材去训一个 RVC 模型。")
    # ★ 没底模训出来的模型"不会跟调"，这是"唱出来没法听"最常见的根因。
    #   在这里明说，用户才不会把它当成自己素材的问题（那样他会白忙很久）。
    if (role.get("rvc") or {}).get("pretrained") is False:
        log(job, "  [注意] 这个歌声模型是**没有底模、从零训**的：它学不会跟着旋律唱，"
                 "输出会卡在角色自己的音区、变调也可能完全没反应。"
                 "如果成品听着不对，优先用底模重训一次（不是素材的问题）。")
    d = role_dir(rid)
    final = os.path.join(d, "final")
    os.makedirs(final, exist_ok=True)
    # prefix == "preview" 时用固定名（preview_latest）：小样只保留一份，
    # 每试听一次就覆盖上一条，不会在成品清单里堆出一排样片。
    stamp = "latest" if prefix == "preview" else time.strftime("%m%d_%H%M%S")
    tag = "%s_%s" % (prefix, stamp)
    out_wav = os.path.join(final, "voice_%s.wav" % tag)

    # ★ 同 `_multi_sing` 里那处：`wav_seconds` 只认整型 PCM wav，mp3/m4a 恒返回 0。
    #   这条路目前还靠后面的 `wav_seconds(vocal_wav) or song_secs` 兜着，
    #   所以症状没暴露 —— 但根源一样，先修掉，别留一颗地雷。
    try:
        song_secs = float(_probe_dur_safe(song_path) or 0.0)
    except Exception:
        song_secs = 0.0

    # ---- 第 0 步：分离人声 / 伴奏（见 separate_song 的说明，这步不能省）----
    if pre_sep:
        # 试听小样那条路已经分离过了，这里不重复跑（分离是这一步里最贵的）。
        vocal_wav, inst_wav = pre_sep
        log(job, "  用已经分离好的轨，跳过分离")
    else:
        job["stage"] = "分离人声"
        begin_step(job, "分离人声与伴奏",
                   "把原曲拆成「人声轨」和「伴奏轨」。变声只能对着人声做 —— "
                   "对着整首歌做，鼓和贝斯的音高也会被当成「唱」，输出跟着节奏乱跑；"
                   "混音也只能用伴奏，否则原唱会跟着角色一起唱。")
            # 「伴唱优先」：用 karaoke 模型分离 —— 伴唱被留在伴奏轨，角色只唱主唱那条。
        if sep_model == "karaoke":
            sep_model = _karaoke_model()
            log(job, "  分离方式：**伴唱优先**（karaoke 模型 —— 垫唱/和声留在伴奏里，"
                     "只把主唱交给人声轨）")
        vocal_wav, inst_wav = separate_song(job, song_path, model=sep_model)
        end_step(job, True, "人声轨 + 伴奏轨已就绪")

    # ---- 改词（可选）：把人声轨换成「唱新词」的版本，旋律不变 ----
    # 放在这里、而不是变声之后，是因为 RVC 改不了词（见 rewrite_lyrics 的说明）。
    # 换掉 vocal_wav 之后，下面的变声 / 混音 / 质检全都不用改一行 ——
    # 它们本来就只认 vocal_wav 这个名字。
    if lyric:
        orig_lyric, new_lyric = lyric
        job["stage"] = "改词"
        begin_step(job, "改写歌词（保留原曲旋律）",
                   "先把人声重新唱一遍：旋律照原曲，词换成你写的新词。"
                   "长歌会切成约 %d 秒一块逐块生成，每块都会对齐回原来的长度，"
                   "免得人声和伴奏错开。这一步比变声慢，是整个链路里第二耗时的一段。"
                   % int(LYRIC_CHUNK_SEC))
        # 采样步数：官方示例用 100（质量优先），HF Space 默认 32。
        # 这里默认 32，想要更稳可以传更高（代价是每块时间线性变长）。
        _segs = lyric_segments_for(song_path, orig_lyric,
                                   offset=lyric_seg_offset or 0.0,
                                   dur=lyric_seg_dur)
        if (lyric_seg_offset or 0.0) > 0 and not _segs:
            log(job, "  ⚠ 这一段里没有原词的时间戳，改词会按句数均分 —— "
                     "小样段太靠后/太短时会出现，换个更长的试听长度更准")
        vocal_wav = rewrite_lyrics(job, vocal_wav, orig_lyric, new_lyric,
                                   os.path.join(final, "lyric_%s.wav" % tag),
                                   nfe=int(lyric_nfe or 32),
                                   orig_segments=_segs)
        end_step(job, True, "已唱出新词的人声轨")

    # ---- 唱前微调：把**喂给模型的那条干声**弄干净 ----
    # ★ 必须排在变声之前：RVC 会把底噪、隆隆、削波当成音色一起学过去
    #   （RVC 社区和 E-SVS 论文都把它列为唱前必修）。
    # ★ 排在建词**之后**：改词会重新合成一条干声，先清理再合成等于白清。
    # ★ 不跑 "level"：干声的电平是后面混音那一步调的，这里改了会把
    #   "整首歌的音量"悄悄挪一次，而这不是用户点的东西。
    # ★ "唱前要跑哪几步"由 pre_mode + 界面逐步勾选合成，规则只有一份
    #   （polish.pre_skip_for），不许在这里另写一遍。
    _pre_how = _polish_mod().pre_skip_for(pre_mode, pre_skip)
    if POLISH_PRE_MODE != "off" and len(_pre_how) < len(POLISH_PRE_STEPS):
        job["stage"] = "唱前微调"
        _why = {"guard": "只修会导致破音的削波",
                "deep": "去底噪 / 去隆隆 / 修削波 / 修爆音",
                "off": "完全不动手"}.get(str(pre_mode or "").strip().lower(), "")
        begin_step(job, "唱前微调（%s）" % (_why or "自定义"),
                   "干声脏，模型会把脏东西当成音色一起学走 —— 这是「唱出来有底噪、"
                   "有电音」最常见的源头。先量这一版干声：有没有削波、有没有低频"
                   "隆隆、噪声底有多高；哪一项真的有问题才修那一项，修完复测，"
                   "没好转就退回去（不会硬改）。"
                   "★ 哪一步会动是你在界面上定的（默认只修会导致破音的削波）—— "
                   "想更干净就自己去开，别让它替你决定。")
        vocal_wav, _pp = polish_stage(job, "pre", vocal_wav,
                                      os.path.join(final, "voice_pre_%s.wav" % tag),
                                      skip=_pre_how)
        end_step(job, True, "唱前微调完成" if _pp else "量出来不需要修，没动")

    # ---- 音高：可以是固定半音，也可以是 "auto" ----
    if isinstance(pitch, str) and pitch.strip().lower() == "auto":
        job["stage"] = "估音高"
        begin_step(job, "估算音高（自动）",
                   "比对原唱人声与角色音域的基频中位数，算该升/降几个半音。"
                   "男声唱女声原曲通常要 -12，女声唱男声原曲通常要 +12。")
        p, why = estimate_pitch(job, vocal_wav, role)
        pitch = p
        end_step(job, True, "自动音高 %+d 半音（%s）" % (p, why))
        log(job, "  自动音高：%+d 半音 —— %s" % (p, why))
    else:
        pitch = int(pitch)

    # 大跨度失真提醒 —— 自动估的和手填的都要说。
    # 为什么非要在这里补一句：后面的「音高体检」只量**音高准不准**（八度错、
    # 吻合率），而变调对 f0 几乎就是纯平移，任何一档都能轻轻松松过。也就是说
    # 体检永远不会告诉你"这一档听着电"。这条缺口实测坑过用户一次 ——
    # 他把音高设成 +12，体检报告"这一档没问题"，成品却是明显的金属声。
    _plv, _ptxt = pitch_shift_warn(pitch)
    if _plv:
        log(job, "  [%s] 音高设的是 %+d 半音：%s"
                 % ("明显失真提醒" if _plv == "bad" else "失真提醒",
                    int(pitch), _ptxt))

    # 特征索引库缺失时不能用 index-rate>0（RVC 会直接报错退出）。
    # 自训模型会带索引；用户从别处拿来的模型通常不带 —— 这里自动降级，
    # 少一点音色细节，但不会整首歌失败。
    #
    # 判断必须走 _find_rvc_index()（= RVC 自己的匹配规则），不能自己拼
    # `<stem>.index`：索引真名带 IVF 参数且模型名带 _eN_sN 尾巴，拼不出来。
    idx = _find_rvc_index(rvc_model)
    if index_rate > 0 and not idx:
        log(job, "  这个模型没有特征索引库，改用 index-rate 0 演唱")
        index_rate = 0
    elif index_rate > 0:
        log(job, "  用特征索引库：%s" % os.path.basename(idx))

    # ---- 音高体检：整首开唱**之前**先拿一小段实测几档音高 ----
    # ★ SVC 引擎跳过体检：体检内部用的是 RVC 推理（probe/cand 都是 rvc_infer），
    #   对 SVC 没有意义；而且那几档试唱纯属白烧显卡。
    # ★ 记住"用户原本设的是哪个"：体检可能改判，产物里要能看出改判前后的差别。
    _pitch_asked = int(pitch)
    # 为什么值得多花这一两分钟：整首 RVC 是本链路最贵的一步
    #   （本机实测 60 秒人声轨 103 秒墙钟，见 RVC_EST_FIXED 处的记录）。
    # 而同一首歌、同模型同参数、只改音高，八度错能从 3.4% 蹦到 17.2%（见文件头对照表）；
    # 换个歌又翻回来（丹阳子 −12 单窗只有 2.1%、整曲却有 8.3%）。既然逐首不同、
    # 又只能靠实测知道，那就把它量在小段上 —— 唱完十几分钟才发现音高坏了，代价太高。
    # ★ SVC 也要体检：男声原唱 + 女声角色、音高却设 0 时，模型会被逼在男声音域里唱，
    #   听感就是"完全坏掉 / 特别嘈杂"（2026-10-28 用户报的 Summer Vibe 正是这个）。
    #   体检用这张卡挂的 **RVC 打底权重** 试唱（SVC 那层之前会跑它，所以成本一致），
    #   挑出来的音高对混合链路同样有效。
    _aud_model, _aud_idx = rvc_model, idx
    if engine == "svc":
        _svc0 = role.get("svc") or {}
        _base0 = (_svc0.get("rvc_base") or "").strip()
        if _base0 and os.path.isfile(os.path.join(RVC_ROOT, "assets", "weights", _base0)):
            _aud_model = _base0
            _aud_idx = _find_rvc_index(_base0)
        else:
            _aud_model = None
    audition = None
    if _aud_model and AUDITION and (wav_seconds(vocal_wav) or 0.0) >= AUDITION_MIN_SONG:
        job["stage"] = "音高体检"
        begin_step(job, "音高体检（先试唱一小段）",
                   "整首开唱前，先从**整首歌的三个不同位置**各取一小段拼成约 %.0f 秒，"
                   "把「你设的 %+d 半音」和「相邻八度」各试唱一遍，量八度错和音准吻合。"
                   "整八度是听感差别最大的一档，而八度错在同一首歌内部能从 3%% 变到 20%%，"
                   "先量出来，免得整首白唱。"
                   % (AUDITION_SECS, int(pitch)))
        audition = audition_pitch(job, vocal_wav, pitch, _aud_model, _aud_idx, index_rate,
                                 rms_mix_rate=rms_mix_rate, protect=protect)
        if audition.get("skipped"):
            end_step(job, True, "没体检（%s）" % audition["skipped"])
            log(job, "  音高体检跳过：%s" % audition["skipped"])
        else:
            log(job, "  音高体检（%s；取样自第 %s 秒；全曲 %.0f 秒）："
                % (audition.get("montage") or "-",
                   "、".join("%.0f" % x for x in (audition.get("starts") or [])),
                   wav_seconds(vocal_wav) or 0))
            for c in audition["cands"]:
                log(job, "    %+3d 半音：八度错 %s，宽松吻合 %s"
                    % (c["pitch"],
                       ("%5.1f%%" % (c["octave"] * 100)) if c.get("octave") is not None else "  -  ",
                       ("%5.1f%%" % (c["match"] * 100)) if c.get("match") is not None else "  -  "))
            log(job, "    ⇒ %s" % audition["reason"])
            end_step(job, True, audition["reason"])
            if audition["changed"]:
                # ★ 改判**必须响亮**：用户在界面上设的是 A，我们按 B 唱了 ——
                #   悄悄改等于骗他。所以日志、步骤条、产物参数三处都要留下。
                log(job, "  [注意] 你设的是 %+d 半音，体检后**改用 %+d 半音**整首演唱。"
                         "想坚持原设定，把音高改回来再唱一次即可。" % (int(pitch), audition["chosen"]))
                pitch = int(audition["chosen"])

    # ★ 这一步**必须**有步骤条：RVC 的推理整条链里没有任何 tqdm，
    #   只有开头几行 print，所以"进度百分比"根本无从谈起。
    #   能做的、也最有用的是：把"现在做到哪一步"明确摆出来，并把**用时**顶上去
    #   ——用户要回答的其实是"还在跑吗、跑到哪了"。
    voice_secs = wav_seconds(vocal_wav) or song_secs
    job["stage"] = "歌声转换"
    begin_step(job, "歌声转换（%s）" % ("SVC" if engine == "svc" else "RVC"),
               "按这个角色的音色重唱（输入是分离好的人声轨%s，音高 %+d 半音）。"
               "这一步没有细粒度百分比，看下面的「已用」和日志；%s"
               % (("，%.0f 秒" % voice_secs) if voice_secs else "", pitch,
                  # ★ 这个数必须和进度条用的 RVC_EST 同一口径：以前这里写"实测要十几分钟"
                  #   （那是 20 秒分片之前的旧数），而进度条按 5 分钟走 —— 同一步给出两个
                  #   差 3 倍的预期，用户只能按慢的那个等，看着就像卡死。
                  "本机实测约 %.1f 分钟（显卡会被占满）；这一步没有百分比，"
                  "看下面的「已用」和日志；超过预估 3 倍还没完，多半是有别的任务在抢卡"
                  % (rvc_est_seconds(voice_secs) / 60.0)))
    step_hint(job, "正在用这个角色的音色重唱 —— 显卡会被占满，长歌约几分钟")
    # on_line：RVC 会打「当前设备/选择模型/选择索引」这类行 —— 有百分比就顺手用，
    # 没有也不影响（进度条不动，但日志在动、步骤名在，用户就知道没卡死）。
    # heartbeat：只按**人声轨**的长度估（间奏的静音段 RVC 很快就过），
    # 免得进度条在干等时一动不动。
    est = rvc_est_seconds(voice_secs) if voice_secs else 900
    hb = (est, "人声转换中（按本机速率估算，约 %d 分钟；实际以结束为准）" % round(est / 60.0))

    def _rvc(input_wav, ir):
        if engine == "svc":
            # ★ 两套引擎在这里分流：SVC 不吃 RVC 那套参数（index_rate / rms_mix /
            #   protect 都是 RVC 的东西），变调与设备交给 svc_cli 那一层。
            return svc_convert(job, role.get("svc") or {}, input_wav, out_wav, pitch)
        return rvc_convert(job, rvc_model, idx, input_wav, out_wav, pitch, ir,
                           rms_mix_rate, protect, hb)

    rc, out = _rvc(vocal_wav, index_rate)
    # 兜底：带索引失败就去掉索引再试一次。
    # ★ 原来只认 "added index" 这个字串，可索引配错的真实症状是 faiss 的
    #   `assert d == self.d`（维度不符），日志里根本没有 "added index" ——
    #   于是整首歌直接判失败。改成"只要带索引失败就降级重试"，理由：
    #   没有索引只是音色细节少一点，唱不出来才是真的不能用。
    if rc != 0 and index_rate > 0 and engine != "svc":
        log(job, "  带索引唱歌失败，去掉索引重试一次（音色细节会少一点）")
        rc, out = _rvc(vocal_wav, 0)
    if rc != 0 or not os.path.exists(out_wav):
        end_step(job, False, "歌声转换失败")
        raise Fail("歌声转换失败（退出码 %d）。\n%s" % (rc, _tail(out, 4)))
    end_step(job, True, "已生成变声人声")

    # ---- 高频补偿（可选，见 HF_FREQ 上面那张实测表）----
    # 放在 Seed-VC 之前、且 Seed-VC 那条路不走这里：seedvc_polish 内部自己会做
    # 一次交叉合成（fc 写死 3200），再叠一层就是补两遍，齿音会炸。
    hf_info = None
    if engine != "seedvc" and hf and hf != "off":
        job["stage"] = "高频补偿"
        # ★ auto 档的说明必须说清"可能不补"，否则界面上"正在高频补偿"跑完
        #   什么都没做，用户只会以为功能坏了（这类"设了没生效"的静默最难查）。
        if hf == "auto":
            _desc = ("老模型（40k）的声码器有效带宽只有 12k，8-12k 比原唱低 14dB —— "
                     "这就是「闷 / 咬字糊」的来源。自动档先量这一首到底缺多少："
                     "不缺就跳过（48k 新模型本来就留住了高频，补了只是白付声纹代价），"
                     "缺就从最保守的分频点往下试，够用就停，补完再量一次确认。")
        else:
            _desc = ("RVC 的声码器在 5.7kHz 以上几乎没有能量，齿音只剩原唱的 1/10 —— "
                     "这就是「闷 / 咬字糊」的来源。这一步把 %s Hz 以上换成原唱的真实"
                     "辅音与气声。代价：原唱气声也带进来一点，音色会离角色远一些。"
                     % ("{:,}".format(HF_FREQ.get(hf, 0))))
        begin_step(job, "高频补偿（%s）" % HF_LABEL.get(hf, hf).split(" —— ")[0], _desc)
        hf_out = os.path.join(final, "voice_hf_%s.wav" % tag)
        new_wav, hf_info = hf_brighten(job, out_wav, vocal_wav, hf_out, hf)
        if hf_info:
            out_wav = new_wav
            # ★ 这里以前打的是几个写死的"音色距离"常数（0.3015/0.3316/0.3637）。
            #   2026-10-23 实测发现它们和新测的口径对不上（那边是"距角色中心"，
            #   这边能拿到的是"相对不补偿的漂移"）。宁可报实测口径，也不报没验证的数。
            #   auto 档更没有常数可打 —— 它每一次都是现算的。
            if hf == "auto":
                _a = hf_info.get("auto") or {}
                log(job, "  高频补偿（自动）：缺口 %.1f dB → %.1f dB，分频 %s Hz"
                    % (_a.get("deficit_db") or 0.0,
                       _a.get("after_deficit_db") or 0.0,
                       "{:,}".format(hf_info["fc"]) if hf_info.get("fc") else "?"))
            else:
                log(job, "  高频补偿：分频 %d Hz（固定档，实测口径的漂移不再写死）"
                    % hf_info["fc"])
            end_step(job, True, "已补高频（分频 %s Hz）"
                     % ("{:,}".format(hf_info["fc"]) if hf_info.get("fc") else "?"))
        else:
            # auto 档"量出来不缺"是**正常且正确**的结果，不是失败。
            end_step(job, True, "量出来不缺高频，没有补（这是自动档的正常结果）"
                     if hf == "auto" else "高频补偿没做成，用未补偿的干声")

    # ---- Seed-VC 精修（可选）：44.1k 再过一遍，抬音色 ----
    # 放在**口哨保护之前**：保护那一步会把"不是唱"的段换回原声，先精修再保护，
    # 保护换回去的那几段才不会被 Seed-VC 又唱一遍（那就白保护了）。
    if engine == "seedvc":
        job["stage"] = "音色精修"
        sv_out = os.path.join(final, "voice_sv_%s.wav" % tag)
        try:
            out_wav = seedvc_polish(job, rid, vocal_wav, out_wav, sv_out)
        except Fail as e:
            # 精修失败不该把整首歌赔进去：RVC 那条干声本身是好的，降级继续。
            # 但**必须写进日志和步骤条** —— 悄悄降级等于骗用户"这就是精修版"。
            log(job, "  [warn] 音色精修没做成，继续用 RVC 干声出成品：%s" % e)
            end_step(job, False, "精修失败，已改用 RVC 干声（见日志）")

    # ---- 去金属声（电音）：放在**最后一道干声处理** ----
    # 为什么在这个位置：不管走 RVC 还是 Seed-VC 精修，出来的干声都要过这一道，
    # 两条引擎的产物才有一致的质量下限。放在非歌声段保护**之前**，是因为保护
    # 之后那几段已经换回原声（原声不电），再判会稀释掉指标。
    if demetal_mode and demetal_mode != "off":
        job["stage"] = "去金属声"
        begin_step(job, "去金属声（电音）",
                   "RVC 的声码器会把波形「周期复印」—— 相邻周期一模一样、气声被抹掉，"
                   "听感就是金属 / 电子味；音高跨度越大越明显（实测 +12 时谐波噪声比"
                   "比原唱高 10.5dB、4-8k 能量翻 4.2 倍）。这一步先量：不电就不动，"
                   "电了才从最小剂量往上试（削掉造出来的假高频 + 补回真人的周期微扰），"
                   "修完再量一次确认。")
        step_hint(job, "正在量这一版有没有电音")
        dm_out = os.path.join(final, "voice_dm_%s.wav" % tag)
        new_wav, dm_info = demetal(job, out_wav, vocal_wav, dm_out, demetal_mode)
        if dm_info:
            out_wav = new_wav
            _a = dm_info.get("auto") or {}
            # ★ 把「用的哪一路手段」写出来：只抖高频 vs 削高频 vs 两个都用，
            #   听感差别很大（只抖高频不动中频/音色），排查时这一点比数字更关键。
            _band = _a.get("band") or 0.0
            _jit = _a.get("jitter") or 0.0
            _eq = _a.get("eq_db") or 0.0
            _br = _a.get("breath") or 0.0
            _how = []
            if _br > 0:
                # ★ 补气声是首选手段（加性噪声，不动谐波相干性 ⇒ 不会沙哑）
                #   ★ 单位已经是**植入百分比**了（2026-10-24 第五轮改的），直接
                #     说"补进去了成品自身能量的百分之几" —— 这才是"会不会变杂音"
                #     的直尺子；以前说"几倍"，同一个倍数代价能差一倍。
                _how.append("补回真人的气声（植入 %.0f%%）" % (_br * 100.0))
            if _band > 0 and _jit > 0:
                _how.append("只抖 %dHz 以上的周期（抖动 %.3f）" % (int(_band), _jit))
            elif _jit > 0:
                _how.append("全带周期微扰（抖动 %.3f）" % _jit)
            if _eq:
                _how.append("削 4-8k %.0fdB" % _eq)
            _ov = _a.get("overshoot") or {}
            if _ov.get("applied"):
                _how.append("削掉 %d 个局部高频尖峰（最多 %.0fdB）"
                            % (int(_ov.get("active_pct") or 0),
                               _ov.get("max_atten_db") or 0))
            _how = " + ".join(_how) or "未记录手段"
            _dh_after = _a.get("after_dhnr")
            # ★★ 电音没修下去 ≠ 修好了。以前这两句统一写成"已去电音"，
            #    老板看到的成品上一个字没提"还有电"，而他听出来就再来一轮反馈。
            #    ⇒ 达到闸门限值还是要**照实说**，并告诉他下一步怎么办。
            _still = _dh_after is not None and _dh_after >= (AUDITION_METAL_HNR_DB - 0.05)
            _why = None
            if _still:
                _why = (_a.get("reason") or "")
            if _why:
                log(job, "  [warn] 这一步没能修干净：%s" % _why)
                end_step(job, True, _why.replace("**", ""))
            else:
                log(job, "  去金属声：%s；ΔHNR %.1f → %.1f dB，4-8k ×%.2f → ×%.2f"
                    % (_how, _a.get("dhnr") or 0.0, _a.get("after_dhnr") or 0.0,
                       _a.get("hf_rel") or 0.0, _a.get("after_hf_rel") or 0.0))
                end_step(job, True, "已去电音：%s（ΔHNR %.1f → %.1f dB）"
                         % (_how, _a.get("dhnr") or 0.0, _a.get("after_dhnr") or 0.0))
        else:
            # ★ "量出来不电 / 所有剂量都过不了副作用护栏" 都是**正常结果**，不是失败。
            end_step(job, True, "这一版不电（或没有安全剂量），没有改动")

    # ---- 临场感补偿（2~5kHz）：放在 demetal **之后**（先补再削等于白补）----
    if presence_mode != "off":
        job["stage"] = "临场感补偿"
        begin_step(job, "临场感补偿（2~5kHz）",
                   "AI 人声在 2~5kHz 这一带比真人**系统性缺一截**（139 条成品实测"
                   "中位缺 3.9dB）—— 就是听着「闷、不靠前、不像在唱歌」的那一段。"
                   "先量这一版缺多少：不缺就不动，缺了按缺口补，补完再量一次确认。")
        pr_out = os.path.join(final, "voice_pr_%s.wav" % tag)
        new_wav, pr_info = presence(job, out_wav, vocal_wav, pr_out, presence_mode)
        if pr_info:
            out_wav = new_wav
            _a = pr_info.get("auto") or {}
            log(job, "  临场感：%s" % (_a.get("reason") or "已补"))
            end_step(job, True, "已补临场感（+%.1f dB）" % (_a.get("dose_db") or 0.0))
        else:
            end_step(job, True, "量出来不缺临场感，没有补（这是自动档的正常结果）")

    # ---- 唱后微调：齿音 / 刺耳 / 高音柔化 / 拼接 / 软限幅 ----
    # ★ 排在 presence **之后**：presence 补的是 2~5k，先柔化再补等于白补
    #   （"先补后削"的顺序坑在第五轮栽过一次，别再踩）。
    if POLISH_POST_MODE != "off":
        job["stage"] = "唱后微调"
        begin_step(job, "唱后微调（去齿音 / 去刺耳 / 高音柔化 / 修衔接）",
                   "升调会把谐波整排搬进耳朵最敏感的 2~6kHz —— 那就是「高音刺」；"
                   "齿音（4.5~10k）也会被一起顶上来。逐句拼回去的地方还可能有"
                   "「啪」一下的接缝。这一步**先量后修**：只压冒出来的那一下"
                   "（不是常驻 EQ，常驻会把整首压闷），修完复测，"
                   "没好转或副作用太大就退回去。")
        new_wav, _po = polish_stage(job, "post", out_wav,
                                    os.path.join(final, "voice_po_%s.wav" % tag),
                                    skip=(tuple(post_skip) if post_skip is not None
                                          else ()),
                                    # ★ semi = 这次实际升了多少半音。formant 那一步
                                    #   只认这个数（不给就不动，绝不猜）。
                                    semi=pitch or 0.0)
        if _po:
            out_wav = new_wav
            _b, _a2 = _po.get("before") or {}, _po.get("after") or {}
            log(job, "  唱后微调：齿音超出 %.1f → %.1f dB，拼接点 %d → %d"
                % (_b.get("sib_excess_db") or 0.0, _a2.get("sib_excess_db") or 0.0,
                   _b.get("splices") or 0, _a2.get("splices") or 0))
            end_step(job, True, "唱后微调已完成")
        else:
            end_step(job, True, "量出来不需要修，没动（这是自动档的正常结果）")

    # ---- 转换噪声补偿（3~10kHz）：唱后微调之后、非歌声段保护之前 ----
    if LTAS_MEND_MODE != "off" and "ltas" not in (post_skip or ()):
        job["stage"] = "转换噪声补偿"
        begin_step(job, "转换噪声补偿（3~10kHz）",
                   "RVC 出来的高频**形状**不像人声：4~8k 是造出来的宽带毛刺、"
                   "8~12k 却只剩原唱的一小截。固定 EQ 修不了这两个相反的毛病 —— "
                   "这一步拿**原唱的长期平均频谱**当靶子，逐 1/3 倍频程给一条"
                   "平滑曲线（多了收、缺了小幅补），修完复测，"
                   "没更接近原唱就不动（不留更差的文件）。")
        lm_out = os.path.join(final, "voice_lm_%s.wav" % tag)
        new_wav, lm_info = ltas_mend(job, out_wav, vocal_wav, lm_out,
                                     LTAS_MEND_MODE)
        if lm_info:
            out_wav = new_wav
            _lm = lm_info.get("auto") or {}
            log(job, "  转换噪声补偿：%s" % (_lm.get("reason") or "已修"))
            end_step(job, True, "已修 3~10k 的频谱形状")
        else:
            end_step(job, True, "形状已经够像原唱，没动（自动档的正常结果）")

    # ---- 非歌声段保护：口哨 / 纯音段换回原始人声 ----
    # 为什么放在变声之后、混音之前：判据只有拿到 f0 和谱特征才能算（要起 torch），
    # 而"换回原声"必须对**已经变过声**的那条轨做 —— 否则会把没变的整段都换掉。
    voice_wav = out_wav
    guard_info = None
    if nonvocal != "off":
        job["stage"] = "非歌声段保护"
        begin_step(job, "非歌声段保护（口哨 / 纯音）",
                   "口哨、纯音这类**不是唱**的声音，RVC 会当成「唱」用角色嗓音再发一遍："
                   "实测男声角色会把 662Hz 的口哨降成 220Hz 的持续音 —— 那就是「吼」。"
                   "这一步把它们判出来，在那几段换回原始人声（口哨不体现角色音色，"
                   "翻唱里保留原版是常规做法）。")
        step_hint(job, "正在把口哨、纯音这类不是唱的段换回原声")
        g_out = os.path.join(final, "voiceg_%s.wav" % tag)
        voice_wav, guard_info = guard_nonvocal(job, vocal_wav, out_wav, g_out, nonvocal)
        if guard_info and guard_info.get("protected_secs"):
            end_step(job, True, "已保护 %.1f 秒非歌声段（%d 段，模式 %s）"
                     % (guard_info.get("protected_secs", 0),
                        len(guard_info.get("segments") or []), nonvocal))
        else:
            end_step(job, True, "这首歌里没检测到口哨/纯音段")


    # ---- 非主唱段保护（伴唱 / 和声 / 垫唱）----
    # 为什么放在非歌声段保护之后：那一步替换的是"不是唱"的段（口哨），
    # 这一步替换的是"是别人在唱"的段；两者都在往原生轨换，用上一步的产物当输入即可。
    lead_info = None
    lead_mode = LEAD_GUARD_MODE if lead is None else lead
    if lead_mode and lead_mode != "off":
        job["stage"] = "非主唱段保护"
        begin_step(job, "非主唱段保护（伴唱 / 和声）",
                   "原曲的人声轨里除了主唱，还有伴唱、和声、垫唱 —— RVC 分不出谁在唱，"
                   "会把它们也当成主唱再唱一遍，叠在一起就是「很多人一起唱、发闷发哑」。"
                   "这一步按**这首歌自己的主唱基线**（立体声宽度 + 左右相关性）找出那几段，"
                   "换回原始人声：伴唱还是原版伴唱，只是不再由角色唱。"
                   "判据不可靠（圈得太多）时整段撤销 —— 宁可不动，也不许把主唱静音。")
        step_hint(job, "正在找出原曲里不是主唱唱的那几段")
        l_out = os.path.join(final, "voicel_%s.wav" % tag)
        new_wav, lead_info = guard_lead(job, vocal_wav, voice_wav, l_out, lead_mode)
        if lead_info and lead_info.get("protected_secs"):
            voice_wav = new_wav
            log(job, "  非主唱段：换回原声 %.1f 秒（%d 段；基线相关 %.2f / 侧比 %.2f）"
                % (lead_info.get("protected_secs") or 0,
                   len(lead_info.get("spans") or []),
                   lead_info.get("base_corr") or 0.0,
                   lead_info.get("base_smr") or 0.0))
            end_step(job, True, "已保护 %.1f 秒非主唱段"
                     % (lead_info.get("protected_secs") or 0))
        elif lead_info and lead_info.get("reject"):
            end_step(job, True, "判据在这首歌上不可靠，没动（%s）"
                     % str(lead_info.get("reject"))[:40])
        else:
            end_step(job, True, "没找到「不是主唱唱」的段（或人声轨是单声道）")


    # ---- 口哨专用保护（高基频 / 无谐波 / 稳定 / 短段）：非歌声段保护之后 ----
    whistle_info = None
    whistle_mode = WHISTLE_GUARD_MODE if whistle is None else whistle
    if whistle_mode and whistle_mode != "off":
        job["stage"] = "口哨专用保护"
        begin_step(job, "口哨专用保护（高基频纯音）",
                   "口哨/高频纯音不是唱，RVC 却会用角色嗓音再发一遍（听感：尖锐的怪音、"
                   "杂乱）。普通「非歌声段保护」不敢放宽 —— 它一放松就会把干净的高音延长"
                   "也换回原声（角色嗓子消失）。这里用**独立证据**判：基频 ≥550 Hz、"
                   "几乎无二次谐波、极稳、段长 0.2~3 秒；圈得太多会自动整段撤销。")
        step_hint(job, "正在找口哨这类高频纯音")
        w_out = os.path.join(final, "voicew_%s.wav" % tag)
        new_wav, whistle_info = guard_whistle(job, vocal_wav, voice_wav, w_out,
                                              whistle_mode)
        if whistle_info and whistle_info.get("protected_secs"):
            voice_wav = new_wav
            log(job, "  口哨段：换回原声 %.2f 秒（%d 段）"
                % (whistle_info.get("protected_secs") or 0,
                   len(whistle_info.get("spans") or [])))
            end_step(job, True, "已保护 %.2f 秒口哨段"
                     % (whistle_info.get("protected_secs") or 0))
        elif whistle_info and whistle_info.get("reject"):
            end_step(job, True, "判据不可靠，没动（%s）" % str(whistle_info.get("reject"))[:40])
        else:
            end_step(job, True, "没找到口哨段（正常结果）")

    tracks = [{"label": "人声（干声）", "wav": voice_wav}]
    # 先占位：mix=False 时下面不会赋值，而写产物清单时要读它（否则 NameError）
    mix_wav = None

    # 成品轨：**伴奏轨** + 变声人声。
    # ★ 这里绝对不能用原曲 —— 原曲里带着原唱，混出来就是「原唱 + 角色」
    #   两个人在唱（实测相关系数 0.870）。这正是之前"没法听"的主因。
    # 用自带的混音脚本而不是 ffmpeg —— 整合包的 ffmpeg 是老版，
    # amix 不支持 normalize 选项会直接报错。
    if mix:
        mix_wav = os.path.join(final, "mix_%s.wav" % tag)
        job["stage"] = "混合成品"
        begin_step(job, "混合成品",
                   "伴奏轨 + 角色人声，自动对齐响度、把残留底噪压掉，"
                   "出可直接听/发的成品")
        step_hint(job, "正在把人声和伴奏合到一起，并对齐响度")
        rc2, out2 = run(job, '"%s" -s "%s" "%s" "%s" "%s" auto auto %s'
                        % (PY, os.path.join(STAGES, "mix_cli.py"),
                           inst_wav, voice_wav, mix_wav,
                           "-45" if gate else "off"), timeout=1200)
        if rc2 == 0 and os.path.exists(mix_wav):
            for line in reversed((out2 or "").splitlines()):
                if line.strip().startswith("MIX_DONE"):
                    log(job, "  混合轨：%s" % line.strip())
                    break
            tracks.insert(0, {"label": "成品（人声+伴奏）", "wav": mix_wav})
            end_step(job, True, "成品轨已生成")
        else:
            log(job, "  [warn] 混合轨没做成（退出码 %d），人声干轨仍然可用：%s"
                % (rc2, _tail(out2, 3)))
            end_step(job, True, "混合轨没做成，人声干轨仍可用（已说明）")

    # ---- 客观质检：拿原唱人声轨和角色人声轨逐帧比基频 ----
    # 放在这里（而不是等用户去听）是因为"唱得对不对"以前没有一把尺子：
    # 音高没生效、旋律被唱平、人声和伴奏错位，听起来都只是"难听"，
    # 用户说不清、我也猜不准。量出来之后至少能指名道姓。
    qc_result = None
    if qc and wav_seconds(vocal_wav) > 3:
        job["stage"] = "客观质检"
        begin_step(job, "客观质检",
                   "用 RMVPE 逐帧比「原唱人声」和「角色人声」的基频："
                   "音准吻合率、旋律轮廓、音高起伏比、人声与伴奏是否对齐。"
                   "只量不修 —— 至少让「难听」有个具体的说法。")
        step_hint(job, "正在逐帧比对原唱与角色的音高，判断唱准了没")
        # ★ 质检**只取一段**（30 秒），不整首比。
        #   整首比会出错：一首歌的旋律本来就大量重复，在一两百秒里搜"最像的位置"
        #   很容易锁到另一段副歌上 —— 实测整首比给出"对齐差 1200ms"，
        #   而同一段 12 秒窗口里对齐是 0ms。窗口短、重复少，结果才可信。
        _tot = wav_seconds(vocal_wav) or 0
        _qs = max(0.0, (_tot - 30.0) * 0.35) if _tot > 32 else 0.0
        _qd = min(30.0, _tot) if _tot else 0.0
        _qc_skip = list((guard_info or {}).get("segments") or []) + \
            list((lead_info or {}).get("spans") or [])
        qc_result = qc_singing(job, vocal_wav, voice_wav, expect_pitch=pitch,
                               start=_qs, secs=_qd,
                               skip=_qc_skip)
        ok, line, det = describe_qc(qc_result, expect_pitch=pitch)
        for dd in det:
            log(job, "  " + dd)
        if qc_result and qc_result.get("skipped_secs"):
            log(job, "  质检已排除 %.1f 秒非歌声段 —— 那几段是**故意保留的原声**"
                     "（口哨不跟着变调），按角色音高去比会得出假结论"
                % qc_result["skipped_secs"])
        if qc_result is not None:
            log(job, "  %s" % line)
            end_step(job, ok, line)
        else:
            end_step(job, True, "客观质检没跑出来（不影响成品）")

    # ---- 整曲质检：上面那个数只代表**一段**，这里把整首铺满再给一个数 ----
    # 为什么必须再量一遍：上面单窗取的是 (总长-30)*0.35 那一段，而八度错在同一首歌
    # 内部能从 3.5% 变到 16.9%（七窗实测，见 qc_singing_whole 上方注释）。
    # 用户照着"抽签出来的数"决定要不要重唱，比不给数更糟。
    qc_whole = None
    if qc and (wav_seconds(vocal_wav) or 0.0) > QC_WHOLE_WIN * 1.5:
        job["stage"] = "整曲质检"
        begin_step(job, "整曲质检（铺满整首，不只量一段）",
                   "把整首歌从第 0 秒铺到结尾，每 %.0f 秒量一窗，再聚合成一个能代表"
                   "「整首」的数，并单独报出**最差那一窗** —— 听感由最差的那段决定。"
                   "只量一段的话，同一份成品可能报 3%% 也可能报 20%%。" % QC_WHOLE_WIN)
        step_hint(job, "正在把整首歌从头到尾量一遍（不只抽一段）")
        qc_whole = qc_singing_whole(job, vocal_wav, voice_wav,
                                    skip=_qc_skip)
        w_warn, w_line = describe_qc_whole(qc_whole)
        if qc_whole:
            log(job, "  %s" % w_line)
            for w in (qc_whole.get("per_window") or []):
                log(job, "    第 %5.0f 秒：八度跳 %5.1f%%，吻合 %5.1f%%"
                    % (w.get("at") or 0.0,
                       (w.get("octave_err_ratio") or 0.0) * 100,
                       (w.get("match_ratio_loose") or 0.0) * 100))
            end_step(job, not w_warn, w_line)
        else:
            end_step(job, True, "整曲质检没跑出来（不影响成品）")

    if save:
        def _add(r):
            if output_kind == "preview":
                # 小样只留一条：试听几次就把成品清单淹了，用户会找不到正片。
                # 每次覆盖上一条（文件名也是固定的 preview_latest）。
                r["outputs"] = [o for o in (r.get("outputs") or [])
                                if o.get("kind") != "preview"]
            # 歌词跟着成品走：改过词就用新词；没改词但这首歌之前识别过，用缓存那份
            # （绝不为了唱歌去触发识别 —— 那要下一 1.5GB 的模型，见 _cached_lyric）。
            # 时间轴只在"句数对得上"时才给，对不上宁可不给（均分出来的假时间轴更坑）。
            _lyr = _lyric_text(lyric)
            _segs = ((job.get("lyric_result") or {}).get("segments") or []) if _lyr else []
            if not _lyr:
                _lyr, _segs = _cached_lyric(song_path)
            for t in tracks:
                r.setdefault("outputs", []).append({
                    "text": label or os.path.basename(song_path), "wav": t["wav"],
                    "at": _now(),
                    "kind": output_kind, "track": t["label"],
                    "seconds": round(wav_seconds(t["wav"]), 2),
                    "lyric": _lyr,
                    "lyric_lrc": _write_lrc(t["wav"], _lyr, _segs),
                    # ★ 字幕跟着成品走：自带 .srt 原样带过来，没有就用识别出的
                    #   歌词时间轴生成一份（没有时间轴就不硬凑，见 _write_srt）。
                    "lyric_srt": _write_srt(t["wav"], _lyr, _segs, song_path),
                    # ★ 把这条成品用到的四条轨都记下来，前端才能做「多轨试听 / A-B 对比」：
                    #   出问题时用户能自己把伴奏、干声单独听一遍，定位到底是哪一步坏了
                    #   （只给一条混好的成品，用户只能说"难听"，说不出难听在哪）。
                    "stems": {
                        "mix": t["wav"] if t["label"].startswith("成品") else mix_wav,
                        "voice": voice_wav,
                        "inst": inst_wav,
                        "src": song_path,
                    },
                    "qc": qc_result,
                    # 整曲质检（铺满整首的那个数）—— 单窗那个 qc 只代表一段，
                    # 这个才能回答"整首到底有多少帧在翻八度"。
                    "qc_whole": qc_whole,
                    # 把"保护了多久"记进产物：用户过几天回头看这条成品，
                    # 能知道当时口哨段是怎么处理的（而不是只看到一个成品文件）。
                    "nonvocal": ({"mode": nonvocal,
                                  "protected_secs": (guard_info or {}).get("protected_secs", 0),
                                  "segments": (guard_info or {}).get("segments") or []}
                                 if nonvocal != "off" else None),
                    # ★ 把「这一版到底是拿什么参数唱的」记进产物。
                    #   为什么要补这个：查「唱歌越唱越差」时，成品记录里只有质检数，
                    #   **没有音高、没有用的是哪个 RVC 模型** —— 于是看到"某版八度错 18%"
                    #   根本没法归因，只能回头去翻 data/jobs/*/log.txt 里那行 `--pitch`。
                    #   16 版成品全靠人工对日志才对上账（对照表见文件头「音高体检」一段）。
                    "params": {
                        "pitch": int(pitch),              # 实际用的音高（体检改判后是改判值）
                        "pitch_asked": int(_pitch_asked),  # 用户在界面上设的那个
                        "model": os.path.basename(rvc_model or ""),
                        "f0": RVC_F0_METHOD,
                        "index_rate": float(index_rate),
                        "rms_mix_rate": (None if rms_mix_rate is None else float(rms_mix_rate)),
                        "protect": (None if protect is None else float(protect)),
                        "engine": engine,
                        "hf": (hf if engine != "seedvc" else "off"),
                        "hf_fc": (hf_info or {}).get("fc", 0),
                        "nonvocal": nonvocal,
                        "audition": audition,             # 体检表（含每档的八度错/吻合）
                    },
                })

        mutate_role(rid, _add)
    return tracks


# ------------------------------------------------ 多角色唱同一首歌（对唱 / 分段合唱）
# 分配方式的中文名。放在这里而不是前端：日志、产物、界面三处用的是同一份，
# 前端自己写一套就会跟这里漂（"按乐句"在前端叫"按词组"这种事只会让人猜）。
MULTI_MODES = {
    "rotate": "逐句轮流",
    "phrase": "按乐句轮流",
    "section": "按段落轮流",
    "manual": "手动指定",
    "chorus": "叠唱（主唱 + 和声，同时唱）",
}
# ★ 上限 8（2026-10-21 从 4 放开）。为什么不再往上：
#   · 时间：每个角色要把**整首**唱一遍，N 个人就是 N 倍推理（见 sing_multi 的说明）；
#   · 听感：一句一句轮下去，超过 8 个人同一首歌里每人平均不到两句，
#     "谁唱哪句"反而不容易听出来。想要更多人，**叠唱**才是那个按钮
#     （叠唱不受这个上限影响吗 —— 受，但它是所有人同时唱，听感不会碎）。
#   ★ 这个数必须和前端 app.js 的 STAGE_MAX_CASTS 是同一个 —— 门禁会比对，
#     漂了当场报红（以前两边各写一个数，改一边就是"选择第 5 个人时突然报错"）。
MULTI_MAX_ROLES = 8
# 叠唱时"和声"那几层的音量。0.55 是"听得见但压得住主唱"的量：
# 太高（>0.8）就成了几个人抢主旋律，太低（<0.35）等于没叠、白花 N 倍时间。
MULTI_CHORUS_GAIN = 0.55
# 逐人体检的取样窗上限。整曲比没意义（大部分时间这个人没在唱），
# 但只取一两秒又抖得没法用 —— 30 秒与单窗质检同一个口径，好对照。
MULTI_QC_SAMPLE = 30.0
MULTI_QC_MIN = 3.0      # 短于 3 秒的段量不出可信的数，直接说"段太短"
# 多角色"每人只转自己分到的那几句"。**2026-10-22 实测后默认开**：
#   60 秒真实干声，同一角色跑两遍（A=整首、B=只转分到的 30 秒），在自己唱的
#   两段上比 —— 幅度谱相关 0.941 / 0.910（相位无关，比波形相关可靠：同一段
#   音频跑两次 RVC，波形相关只有 -0.24~0.45，那是相位抖动不是音色差）；
#   四个频段能量占比差 <3 个百分点；RMS 差 0.03 dB；
#   而且 **B 与源干声的谱相关 = A 与源干声的谱相关**（0.628/0.628、0.469/0.485），
#   说明它一点没比整首转换更"跑偏"。
#   省多少由 stages/segonly.should_use 现场算，省不了的人自动退回整首。
MULTI_SEG_ONLY = True


def ensure_lyric_times(job, song_path, sep_model=None, auto_lyric=True, lang="zh"):
    """拿这首歌的**分句时间戳**；没认过就顺手认一次。

    返回 (segments, duration, 来源)，来源 ∈ cache / asr / off / failed。

    ★ 为什么要自动认：多角色分句全靠时间戳。以前没认过就**静默按时间均分** ——
      切点落在句子中间，两个人各唱半句，听起来像"抢词"，而用户完全不知道
      该去哪认歌词。现在要么真分句，要么明说为什么没分成。
    ★ 为什么不复用「歌词」页那次的缓存键：lyric_segments_any 已经把三档都试了，
      只要有任意一档认过就直接用（分句不关心用哪档）。
    """
    segs, dur = lyric_segments_any(song_path, lang)
    if segs:
        return segs, dur, "cache"
    if not auto_lyric:
        return [], 0.0, "off"
    log(job, "这首歌还没认过歌词 —— 分句要靠它，先自动识别一次（约 1 分钟）")
    try:
        d = recognize_lyrics(job, song_path, lang=lang, sep_model=sep_model,
                             asr_size=ASR_DEFAULT)
    except BaseException as e:                  # 含批量删除钩子的 SystemExit
        log(job, "  [warn] 自动识别歌词没成功：%s" % str(e)[:200])
        return [], 0.0, "failed"
    segs = (d or {}).get("segments") or []
    dur = float((d or {}).get("duration") or 0.0)
    if not segs:
        return [], dur, "failed"
    log(job, "  识别出 %d 句 —— 分句按真实换气位置切" % len(segs))
    return segs, dur, "asr"


def multi_qc_roles(job, vocal_wav, role_wavs, names, pitches, takes, duration,
                   sample=MULTI_QC_SAMPLE):
    """**逐人取样体检**：每个人只拿自己那条轨、在自己最长的那段里比。

    ★ 这不是"多角色整曲质检"，也不是"不做质检"：
      · 混好的成品整曲比 —— 没意义（A 的段会按 B 的音高去比）；
      · 每个角色整曲比 —— 大部分时间它没在唱，等于抽签；
      · 拿各自那条轨 + 只在它负责的窗口里比 —— 等价于把多角色还原成 N 次
        单角色质检，这个数才对得上"这个人这段唱得准不准"。
    窗口会明写进结果（`from` / `secs`），不假装是整首。

    返回 [{"name","rid","pitch","from","secs","match","octave","shift",
           "spread","note"}, ...]；量不出来的一项给 None + note，不抛异常。
    """
    try:
        if STAGES not in sys.path:
            sys.path.insert(0, STAGES)
        import multising as _ms
    except Exception as e:
        log(job, "  [warn] 分句模块不可用，跳过逐人体检：%s" % e)
        return []
    out = []
    for k, wav in enumerate(role_wavs):
        item = {"name": names[k] if k < len(names) else ("#%d" % k),
                "pitch": int(pitches[k]) if k < len(pitches) else 0,
                "from": None, "secs": 0.0, "match": None, "octave": None,
                "shift": None, "spread": None, "note": ""}
        try:
            st, secs = _ms.longest_span(takes, k, sample)
        except Exception:
            st, secs = 0.0, 0.0
        if secs < MULTI_QC_MIN or not wav or not os.path.isfile(wav):
            item["note"] = "分到的段太短（%.1f 秒），没量" % secs
            out.append(item)
            continue
        step_hint(job, "正在体检「%s」：取 %.0f~%.0f 秒那段（%.0f 秒）"
                  % (item["name"], st, st + secs, secs))
        d = qc_singing(job, vocal_wav, wav, start=st, secs=secs)
        if not d:
            item["note"] = "这段没提取到人声基频，没量出来"
            out.append(item)
            continue
        item.update({
            "from": st, "secs": round(secs, 1),
            "match": d.get("match_ratio_loose"),
            "octave": d.get("octave_err_ratio"),
            "shift": d.get("shift_semitones"),
            "spread": d.get("spread_ratio"),
        })
        out.append(item)
    return out


def describe_multi_qc(items):
    """把逐人体检翻成人话（日志 + 成品卡共用同一份口径）。"""
    lines = []
    for it in items or []:
        nm = it.get("name") or "?"
        if it.get("match") is None:
            lines.append("  %s：%s" % (nm, it.get("note") or "没量出来"))
            continue
        pct = int(round(float(it["match"]) * 100))
        oct_ = int(round(float(it.get("octave") or 0.0) * 100))
        bits = ["音准吻合 %d%%" % pct, "八度错 %d%%" % oct_]
        sh = it.get("shift")
        if sh is not None:
            # ★ 这一句专门抓"设了没生效"：pitch 设 -5，量出来是 0，就是设置没落地。
            try:
                sh_f = float(sh)
                bits.append("实际变调 %+.0f 半音" % sh_f)
                if abs(sh_f - float(it.get("pitch") or 0)) > 1.0:
                    bits.append("⚠ 与设定 %+d 不符" % int(it.get("pitch") or 0))
            except (TypeError, ValueError):
                pass
        lines.append("  %s（取样 %.0f~%.0f 秒）：%s"
                     % (nm, float(it.get("from") or 0),
                        float(it.get("from") or 0) + float(it.get("secs") or 0),
                        " · ".join(bits)))
    return lines


def _multi_mix(job, inst_wav, voice_wav, mix_wav):
    """伴奏 + 一条人声 → 成品。返回 (成品路径或 None, 失败尾巴)。

    ★ `job` 可以是 None：那是"用户点了某个角色的独唱版，现混一次"的场景 ——
      它不在任何任务里跑，自然没有任务日志可写。命令与参数是同一份，
      先后差别只在"要不要把输出写进任务日志"，两边**必须**用同一条命令，
      否则会出现"任务里混出来的和用户点的听起来不一样"这种没人查得动的鬼故事。
    """
    cmd = '"%s" -s "%s" "%s" "%s" "%s" auto auto -45' % (
        PY, os.path.join(STAGES, "mix_cli.py"),
        inst_wav, voice_wav, mix_wav)
    if job is None:
        try:
            p = subprocess.run(cmd, cwd=GSV_ROOT, timeout=1200,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            rc, out = p.returncode, (p.stdout or b"").decode("utf-8", "replace")
        except BaseException as e:            # 含批量删除钩子的 SystemExit
            return None, "%s: %s" % (type(e).__name__, e)
    else:
        rc, out = run(job, cmd, timeout=1200)
    if rc == 0 and os.path.exists(mix_wav):
        return mix_wav, ""
    return None, _tail(out, 3)


def lyric_segments_any(song_path, lang="zh"):
    """这首歌认过的**歌词时间戳**（不管当时用的哪档，也不要求文本对得上）。

    和 lyric_segments_for 的区别：改词必须确认"框里的词就是识别出来的那份"
    （词错了就会照着错的音素唱），而分配角色只要"每句在第几秒到第几秒"。
    所以这里宽松：哪档有就用哪档，也不比对文本。

    返回 (segments, 缓存里记的时长)。没认过就 (None, 0)。
    """
    if not song_path:
        return None, 0.0
    for sz in (ASR_DEFAULT, "large", "singing"):
        # ★ 用 lyric_cache_file（带内容指纹回退），不是 lyric_cache_path：
        #   同一首歌的几个副本字节一样但 mtime 不同 ⇒ 路径键落空，
        #   靠内容指纹才能拿到在另一个副本上认出来的那份。
        p = lyric_cache_file(song_path, lang, sz)
        if not p:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        segs = (d or {}).get("segments") or []
        if segs:
            return segs, float((d or {}).get("duration") or 0.0)
    return None, 0.0


# ---------------------------------------------------------------- 分句来源
# ★ 老板（2026-10-25）：「自选句子不完整 / 我要中英双语 / 不要压缩综合 /
#   按照本地歌词文件的分段 / 我依次决定谁唱歌」。
#
#   以前那一列是 ASR 认出来的 60~86 句，再按换气**合并成乐句**（合完 21 句
#   左右，实测 13 首平均 61.9 → 21.2）。副作用有两个，都是致命的：
#     · 他数着的句子少了三分之二 ⇒ "不完整"；
#     · 合并后一句里塞着两三句，"这一句给谁"根本没法点 ⇒ "依次决定谁唱"落空。
#
#   现在三档，找到就停，**任何一档都不合并**：
#     ① 歌曲目录里同名 .lrc —— 一句一行（按它的分段，不做合并）；
#     ② ASR 认出来的句（实测 63.2 句 ≈ lrc 61.9 句，粒度本来就是同一档，
#        所以"合并成乐句"那一步纯粹是多余的，直接不合并）；
#     ③ 都没有才按时间均分。
#
#   ★ 预览（multi_plan_preview）和开跑（sing_multi / sing_multi_resplit）
#     **必须走同一个 plan_segs**，否则"我点的是这份、唱的是那份"就又回来了
#     （verify_multimanual 的 F 段就是守这个的）。
_LRC_SCAN_DIRS = [os.path.expandvars(r"%USERPROFILE%\Music"),
                  os.path.expandvars(r"%USERPROFILE%\Downloads")]
_LRC_MIN_LINES = 6          # 少于这个数当没找到（有的歌只挂了个空壳）
# 元信息行：.lrc 偶尔把「作词 : XXX」也带成一句带时间戳的行
_LRC_META_RE = re.compile(
    r"^\s*(作词|作曲|编曲|制作|混音|母带|录音|监制|出品|和声|吉他|贝斯)"
    r"\s*[:：]?\s*\S*$")
_LRC_ZH_PATH = os.path.join(DATA, "cache", "lrc_zh.json")
# ★ 老板（2026-10-25）：「要我可以自己导入」—— 自动找不到的、找错的，
#   他得能自己贴一份（从别处复制的、或者自己打的）。
LRC_IMPORT_DIR = os.path.join(DATA, "cache", "lrc_import")
_LRC_TS_RE = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_LRC_ZH_FAIL_TTL = 600.0    # 翻失败后 10 分钟内不再重试（预览会被连着调好几次）
_LIB_CACHE = None
_LIB_CACHE_AT = None


def _music_library():
    """曲库（我的音乐）。带 mtime 缓存 —— 预览接口会被连着调好几次。"""
    global _LIB_CACHE, _LIB_CACHE_AT
    p = os.path.join(DATA, "music_library.json")
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return _LIB_CACHE or {"songs": []}
    if _LIB_CACHE is not None and mt == _LIB_CACHE_AT:
        return _LIB_CACHE
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:                                        # noqa: BLE001
        return _LIB_CACHE or {"songs": []}
    _LIB_CACHE = d if isinstance(d, dict) else {"songs": []}
    _LIB_CACHE_AT = mt
    return _LIB_CACHE


def _stem_key(name):
    """文件名比对用的键：逗号/下划线/点都当事，大小写不管。

    曲库里的副本叫 `A_B_C - X.mp3`，原文件可能是 `A,B,C - X.mp3` ——
    不归一就永远对不上，明明有词却拿不到。
    """
    s = os.path.splitext(os.path.basename(name or ""))[0].lower()
    return re.sub(r"\s+", " ", re.sub(r"[,._\-]+", " ", s)).strip()


def _lrc_rows_from_text(raw):
    """文本 → [(秒, 一行歌词)]。跟 viz.parse_lrc 同一套规则，只是吃字符串。

    ★ 解析只有这一处：自动找到的和用户导入的走**同一把尺**，
      否则两边对 `[00:12.3]` / `[00:12.34]` 的宽容度不一样，
      会出现"自动那份 42 句、粘贴同样的内容只有 30 句"这种见鬼的差别。
    """
    out = []
    for line in (raw or "").splitlines():
        stamps = _LRC_TS_RE.findall(line)
        if not stamps:
            continue
        txt = _LRC_TS_RE.sub("", line).strip()
        if not txt or txt.startswith("["):        # [ti:] 这类元信息
            continue
        for mm, ss in stamps:
            try:
                out.append((int(mm) * 60 + float(ss.replace(":", ".")), txt))
            except ValueError:
                continue
    out.sort(key=lambda x: x[0])
    return out


def lrc_import_path(song_path):
    """这首歌**自己导入**的那份歌词存哪（不管有没有导过）。"""
    if not song_path:
        return ""
    try:
        import hashlib
        h = hashlib.md5(os.path.normcase(
            os.path.abspath(song_path)).encode("utf-8", "replace")).hexdigest()
    except Exception:                                        # noqa: BLE001
        return ""
    return os.path.join(LRC_IMPORT_DIR, h[:16] + ".lrc")


def lrc_import_save(song_path, text, min_lines=None):
    """存一份**自己导入**的歌词（老板：「要我可以自己导入」）。

    返回 (句数, 存到哪)。

    ★ 必须有时间轴：分句全靠它。没带 `[mm:ss]` 的纯文本不是歌词稿 ——
      第几秒唱哪句我们根本不知道，收下来只会变成一份对不上的表。
      宁可当场说明拒收，也不许存一份"看着有词其实对不上"的。
    """
    if not song_path:
        raise Fail("先选一首歌，再导入歌词")
    n_min = _LRC_MIN_LINES if min_lines is None else int(min_lines)
    rows = _lrc_rows_from_text(text or "")
    if len(rows) < n_min:
        raise Fail(
            "这份只有 %d 行带时间轴的歌词（至少要 %d 行）。\n"
            "没写成 `[00:12.34]歌词` 这种形式的行不会被当成一句 ——\n"
            "要的是**带时间轴**的歌词（一行一句，行首有 [分:秒]）。\n"
            "（现在这份里共 %d 行文字。）"
            % (len(rows), n_min, len((text or "").splitlines())))
    p = lrc_import_path(song_path)
    if not p:
        raise Fail("这首歌的路径读不出来，没法存")
    try:
        os.makedirs(LRC_IMPORT_DIR, exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text or "")
        os.replace(tmp, p)
    except OSError as e:
        raise Fail("歌词没存上：%s" % e)
    return len(rows), p


def lrc_import_clear(song_path):
    """删掉自己导入的那份 ⇒ 回到自动找的那份（同目录 .lrc / ASR）。"""
    p = lrc_import_path(song_path)
    if p and os.path.isfile(p):
        try:
            os.remove(p)
            return True
        except OSError:
            return False
    return False


def lrc_status(song_path):
    """界面那条「歌词来源」要说的话：现在用的哪一份、几句、能不能换。

    src: import（自己导入的）/ lrc（同目录 .lrc）/ asr（识别出来的）
         / none（没有）/ import_bad（导入的那份解析不出句子来）
    """
    imp = lrc_import_path(song_path) or ""
    has_imp = bool(imp) and os.path.isfile(imp)
    p = local_lrc_path(song_path)
    n = 0
    src = "none"
    if p:
        s, _ = lrc_segs(song_path)
        n = len(s)
        same = has_imp and os.path.normcase(os.path.abspath(p)) == \
            os.path.normcase(os.path.abspath(imp))
        if n:
            src = "import" if same else "lrc"
        else:
            src = "import_bad" if same else "lrc"
    if not n:
        a, _d = lyric_segments_any(song_path)
        if a:
            src, n = "asr", len(a)
        elif src == "none":
            src = "none"
    return {"src": src, "lines": n, "path": p or "",
            "has_import": has_imp, "import_path": imp or ""}


def local_lrc_path(song_path, lib=None):
    """这首歌可用的 .lrc。找不到返回 ""。

    找法按优先级，**找到就停**：
      ① **自己导入的那份**（老板要的"我可以自己导入" —— 他的话最大）；
      ② 歌曲文件自己旁边同名的 .lrc（手动拖进曲库的歌也会有）；
      ③ 歌曲旁边同名的 .lrc；
      ④ 曲库里另一条目跟这首同名（不同副本）⇒ 用它的 .lrc；
      ⑤ 常见音乐目录里按歌名找一份。
    """
    if not song_path:
        return ""
    imp = lrc_import_path(song_path)
    if imp and os.path.isfile(imp):
        return imp

    def _sib(p):
        if not p:
            return ""
        q = os.path.splitext(p)[0] + ".lrc"
        return q if os.path.isfile(q) else ""

    s = _sib(song_path)
    if s:
        return s
    lib = lib if lib is not None else _music_library()
    songs = lib.get("songs") or []
    key = os.path.normcase(os.path.abspath(song_path))
    # 曲库里同名的另一份（例如曲库副本）旁边如果有同名 .lrc，也算找到。
    stem = _stem_key(song_path)
    for it in songs:
        if not isinstance(it, dict):
            continue
        p = it.get("path") or ""
        if p and _stem_key(p) == stem:
            s = _sib(p)
            if s:
                return s
    for d in _LRC_SCAN_DIRS:
        try:
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if fn.lower().endswith(".lrc") and _stem_key(fn) == stem:
                    return os.path.join(d, fn)
        except OSError:
            continue
    return ""


def lrc_segs(song_path, lib=None, dur=0.0):
    """本地 .lrc → 句表。**一句一行，不合并**。

    返回 (segs, lrc路径)；找不到（或行数太少）返回 ([], "")。
    每句的 end = 下一句的 start（最后一句用时长兜底）—— 句与句之间不留缝，
    否则"句间空隙归谁"又要扯皮。
    """
    p = local_lrc_path(song_path, lib)
    if not p:
        return [], ""
    try:
        with open(p, encoding="utf-8-sig", errors="replace") as f:
            raw = f.read()
    except OSError:
        return [], ""
    rows = _lrc_rows_from_text(raw)
    rows = [(float(a), str(b or "").strip()) for a, b in rows
            if str(b or "").strip()]
    rows = [r for r in rows if not _LRC_META_RE.match(r[1])]
    if len(rows) < _LRC_MIN_LINES:
        return [], ""
    out = []
    for i, (st, tx) in enumerate(rows):
        en = rows[i + 1][0] if i + 1 < len(rows) else \
            max(float(dur or 0.0), st + 4.0)
        out.append({"start": round(float(st), 3),
                    "end": round(max(float(en), float(st) + 0.4), 3),
                    "text": tx, "zh": ""})
    return out, p


def _lrc_zh_db():
    try:
        with open(_LRC_ZH_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:                                        # noqa: BLE001
        return {}


def _lrc_zh_put(db):
    try:
        os.makedirs(os.path.dirname(_LRC_ZH_PATH), exist_ok=True)
        tmp = _LRC_ZH_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False)
        os.replace(tmp, _LRC_ZH_PATH)
    except Exception:                                        # noqa: BLE001
        pass


def lrc_zh_for(lrc_path, segs, log=None):
    """给 .lrc 的每一句配中文（老板要"中英双语"）。翻一次按文件指纹存下，下次秒回。

    返回 (zh 列表, how)。翻不动就给等长的空串 —— **没有中文只是这一列空着**，
    不许因此让整个预览挂掉。刚翻失败过的 10 分钟内不再重试（预览会被连着调
    好几次，每次都等一遍会把界面拖死）。
    """
    n = len(segs or [])
    empty = [""] * n
    if not lrc_path or not n:
        return empty, ""
    try:
        sig = [int(os.path.getsize(lrc_path)), int(os.path.getmtime(lrc_path))]
    except OSError:
        return empty, ""
    key = os.path.normcase(os.path.abspath(lrc_path))
    db = _lrc_zh_db()
    rec = db.get(key)
    if isinstance(rec, dict) and rec.get("sig") == sig and rec.get("n") == n:
        zh = rec.get("zh") or []
        if len(zh) == n:
            return [str(x or "") for x in zh], (rec.get("how") or "缓存")
        if rec.get("fail") and \
                (time.time() - float(rec.get("at") or 0)) < _LRC_ZH_FAIL_TTL:
            return empty, ""
    zh, how = [], ""
    try:
        import vizsubs
        texts = [(s.get("text") or "").strip() for s in segs]
        got, how = vizsubs.translate(texts, log=log)
        zh = [str(x or "").strip() for x in (got or [])]
    except Exception as e:                                   # noqa: BLE001
        if log:
            log("  歌词翻译没成（中文那列会空着）：%s: %s"
                % (type(e).__name__, str(e)[:120]))
    if not zh or len(zh) != n:
        db[key] = {"sig": sig, "n": n, "fail": 1, "at": time.time()}
        _lrc_zh_put(db)
        return empty, ""
    db[key] = {"sig": sig, "n": n, "zh": zh, "how": how or "", "at": time.time()}
    _lrc_zh_put(db)
    return zh, how or ""


def plan_segs(song_path, lang="zh", dur=0.0, log=None, lib=None):
    """★ 分句的**唯一入口**：预览和开跑都走它 ⇒ 两边必然是同一份句。

    返回 (segs, src, lrc路径)：
      src = "import"（自己导入的那份）/ "lrc"（同目录 .lrc 的分段）
          / "asr"（识别出来的句）/ ""（都没有）
    ★ 导入的和自动找的**必须分开报**：界面要说"你用的是哪一份"，
      把导入的那份说成"同目录 .lrc 的分段"等于撒谎（他据此判断要不要重导）。
    `lib` 只在门禁里传（造一份假曲库，不去动用户那份）。
    """
    segs, lp = lrc_segs(song_path, lib=lib, dur=dur)
    if segs:
        _src = "lrc"
        _imp = lrc_import_path(song_path)
        if _imp and lp and os.path.normcase(os.path.abspath(_imp)) == \
                os.path.normcase(os.path.abspath(lp)):
            _src = "import"
        zhs, how = lrc_zh_for(lp, segs, log=log)
        for i, z in enumerate(zhs):
            if i < len(segs):
                segs[i]["zh"] = z or ""
        return segs, _src, lp
    a, _d = lyric_segments_any(song_path, lang)
    if a:
        a = [dict(x) for x in a]
        if not any((s.get("zh") or "").strip() for s in a):
            try:
                import vizsubs
                _zhs, _how = vizsubs.translate(
                    [(s.get("text") or "") for s in a], log=log)
                if _zhs and len(_zhs) == len(a):
                    for _i, _z in enumerate(_zhs):
                        a[_i]["zh"] = (_z or "").strip()
                    _write_zh_back(song_path, lang, a)
            except Exception:                                # noqa: BLE001
                pass
        return a, "asr", ""
    return [], "", ""


def _segs_brief(segs):
    """把分句压成能长期存进成品的最短形状：`[[秒, 词], …]`。

    只留这两个字段，是因为成品里要用的只有"第几秒到第几秒唱哪句"：
    概率、词级时间戳、语言标签这些重算一遍就有，塞进 roles.json 只会让它变肥。
    空词的段直接丢掉（它当不了字幕，留下来只会多出一行空白）。

    ★ 第三格 `zh` 只有**这句自带中文**时才写（长度 3），没有就还是 2 格 ——
      老成品一个字节都不变，读的人按长度判断（见 viz_cast_of）。
    """
    out = []
    for s in (segs or []):
        if not isinstance(s, dict):
            continue
        t = (s.get("text") or "").strip()
        if not t:
            continue
        try:
            row = [round(float(s.get("start") or 0), 3), t]
        except (TypeError, ValueError):
            continue
        zh = (s.get("zh") or "").strip()
        if zh:
            row.append(zh)
        out.append(row)
    return out


# ------------------------------------------------ 谁在唱：**以音频为准**
WHO_SR = 16000          # 判人用的采样率（共振峰都在，够分人；再高只是白算）
WHO_WIN = 1.0           # 一窗 1 秒：短了抓不住一句，长了会把两个人糊在一窗里
WHO_STEP = 0.5          # 每 0.5 秒判一次
WHO_MIN = 0.6           # 短于 0.6 秒的切换不算（防换气时闪一下）
WHO_QUIET = 0.06        # 窗内能量不到整首的 6% ⇒ 当没声，不判


def _np():
    """拿 numpy。没有就是 None —— **studio 必须能在没 numpy 的解释器里 import**
       （`verify_disk` / `verify_train_rounds` 跑的是精简的托管 python），
       所以 numpy 只能**函数内**软拿，绝不能出现在模块顶层。
       拿不到时这一整条"逐窗认人"降级退回记录，功能不丢。
    """
    try:
        import numpy as _n
        return _n
    except BaseException:                                # noqa: BLE001
        return None


def _pcm16(path, tmp):
    """整首解成 16k 单声道 float32。读不出来/没有 numpy 就返回 (None, 0)。"""
    np = _np()
    if np is None:
        return None, 0
    if not (path and os.path.isfile(path)):
        return None, 0
    dst = os.path.join(tmp, "p.wav")
    try:
        subprocess.run([FFMPEG, "-y", "-v", "error", "-i", path,
                        "-ac", "1", "-ar", str(WHO_SR), dst],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False, timeout=900)
        sr = 0
        with wave.open(dst) as f:
            sr = f.getframerate()
            a = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2")
        return a.astype(np.float32) / 32768.0, int(sr)
    except BaseException:                                # noqa: BLE001
        return np.zeros(1, dtype=np.float32), 0


def _ncc(x, y):
    """归一化互相关：只看波形形状，不看谁音量大。"""
    x = x - float(x.mean())
    y = y - float(y.mean())
    sx, sy = float(x.std()), float(y.std())
    if sx < 1e-6 or sy < 1e-6:
        return -9.9
    return float((x * y).mean() / (sx * sy))


def multi_who_scan(voice, solos):
    """逐窗判"这会儿是谁在唱"，返回 `[[起, 止, k], …]`；判不了就 None。

    ★ 为什么信波形不信记录（老板报的「亮的人和唱的人对不上」就出在这）：
      `multi_takes` 是**拼接时顺手写下的**。它和成品对不上的时候（后来改过
      分配但没重拼 / 换法重排留下的旧表 / 手动改过成品），画面就会"亮 A 唱 B"
      —— 那种片子比不做还糟，而且是"看着处处对、就是听着错"那一类。
      波形是成品自己身上的东西：成品就是从某个人的独唱轨**原样剪出来**的，
      采样级一模一样（实测匹配轨 1.000，次高只有 0.15~0.41），不会说谎。
    ★ 顺带解决粒度：记录是按段存的（老成品最长一段 80 秒），
      这里每 0.5 秒判一次，"谁唱谁亮"才真的跟着人走。
    """
    if not (voice and os.path.isfile(voice)):
        return None
    if not solos or not all(os.path.isfile(s) for s in solos):
        return None
    np = _np()
    if np is None:
        return None
    tmp = tempfile.mkdtemp(prefix="who_")
    try:
        mix, sr = _pcm16(voice, tmp)
        if sr <= 0 or mix is None:
            return None
        refs = [_pcm16(s, tmp)[0] for s in solos]
        n = min(len(mix), min(len(r) for r in refs))
        if n < sr * 3:
            return None
        win, step = int(sr * WHO_WIN), int(sr * WHO_STEP)
        lvl = float(np.abs(mix[:n]).mean()) or 1e-6
        seq = []
        for t0 in range(0, n - win + 1, step):
            x = mix[t0:t0 + win]
            if float(np.abs(x).mean()) < lvl * WHO_QUIET:
                continue                       # 没声的窗不判（拿噪声投出来的票是乱的）
            best, bs = -1, -1.0
            for j, rf in enumerate(refs):
                v = _ncc(x, rf[t0:t0 + win])
                if v > bs:
                    bs, best = v, j
            if best >= 0:
                seq.append((t0 / float(sr), best))
        if not seq:
            return None
        # 相邻同类并成段
        segs = []
        for t, k in seq:
            if segs and segs[-1][2] == k:
                segs[-1][1] = t + WHO_WIN
            else:
                segs.append([t, t + WHO_WIN, k])
        # 太短的段吞回上一段（换气 / 咬字那一下不该让画面换人）
        fixed = []
        for s in segs:
            if fixed and (s[2] == fixed[-1][2] or (s[1] - s[0]) < WHO_MIN):
                fixed[-1][1] = s[1]
                continue
            fixed.append(list(s))
        if not fixed:
            return None
        fixed[0][0] = 0.0
        fixed[-1][1] = n / float(sr)
        return _who_tidy(fixed)
    except BaseException:                                # noqa: BLE001
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _who_tidy(who):
    """把段表规整一遍（幂等）：相邻段不许重叠，且必须首尾盖满。

    ★ 为什么读的时候还要再规整一次：第一次存进去的是**窗直接并出来**的表，
      相邻段会重叠半秒，而 `_cast_who_at` 取的是第一个命中的段 ⇒
      换人整整晚半秒（实测：11.0 秒处旧表亮碧琪、规整后亮紫悦，耳朵说紫悦）。
      已经躺在 roles.json 里的旧数据靠这个自动修好，不用重算音频。
    """
    out = []
    for a, b, k in (who or []):
        if out and int(out[-1][2]) == int(k):
            out[-1][1] = max(float(out[-1][1]), float(b))
            continue
        out.append([float(a), float(b), int(k)])
    for i in range(len(out) - 1):
        if out[i + 1][0] < out[i][1]:
            out[i][1] = out[i + 1][0]
    if out:
        out[0][0] = min(0.0, out[0][0])
    return [[round(float(a), 3), round(float(b), 3), int(k)] for a, b, k in out]


def _who_disagree(who, takes):
    """算出来的表 vs 记录下来的表，差多少（占比 + 差的秒数）。"""
    if not takes:
        return 0.0, 0.0
    bad = 0
    for a, b, k in who:
        mid = (float(a) + float(b)) / 2.0
        rec = None
        for x, y, kk in takes:
            if float(x) <= mid < float(y):
                rec = int(kk)
                break
        if rec is not None and rec != int(k):
            bad += (float(b) - float(a))
    total = sum(float(b) - float(a) for a, b, _k in who) or 1.0
    return bad / total, bad


def multi_who_of(rid, index, log=None, force=False):
    """这条成品的「谁在唱」时间轴 —— **以音频为准**。算过一次就存进成品。

    返回 None = 这条算不出来（缺独唱轨原料 / 不是多人成品），调用方退回原记录。
    """
    r = get_role(rid)
    if not r:
        return None
    outs = r.get("outputs") or []
    try:
        i = int(index)
    except (TypeError, ValueError):
        return None
    if not (0 <= i < len(outs)):
        return None
    p = outs[i].get("params") or {}
    if not force and (p.get("multi_who") or []):
        return _who_tidy(p["multi_who"])     # 旧数据靠这个自动规整，不用重算
    if not (p.get("multi_takes") and p.get("multi_roles")):
        return None
    final = os.path.join(role_dir(rid), "final")
    tr = p.get("multi_tracks") or []
    if not tr:
        return None
    solos = [os.path.join(final, b) for b in tr]
    voice = (outs[i].get("stems") or {}).get("voice")
    who = multi_who_scan(voice, solos)
    if not who:
        return None
    ratio, secs = _who_disagree(who, p.get("multi_takes") or [])
    if log:
        log("  逐窗认人：%d 段（记录 %d 段）" % (len(who), len(p.get("multi_takes") or [])))
        if ratio > 0.05:
            log("  ⚠ 记录里的「谁在唱」和音频对不上（%.0f%% / %.0f 秒）——"
                "画面已按音频走" % (ratio * 100, secs))
    mutate_role(rid, lambda rr: _who_save(rr, i, who, ratio))
    return who


def _who_save(rr, i, who, ratio):
    outs = rr.get("outputs") or []
    if not (0 <= i < len(outs)):
        return
    outs[i].setdefault("params", {})["multi_who"] = who
    outs[i].setdefault("params", {})["multi_who_fix"] = round(float(ratio), 4)


def sing_multi(job, song_path, casts, mode="rotate", manual=None,
               sep_model=None, index_rate=None, rms_mix_rate=None,
               protect=None, prefix="multi", mix=True, save=True,
               hf="auto", auto_lyric=True, qc=True, chorus_gain=None,
               seg_only=None, demetal_mode="auto", presence_mode="auto",
               harmony_mode="auto"):
    """**多角色唱同一首歌**：对唱 / 分段合唱 / 手动分句。

    casts: [{"rid": 角色id, "pitch": 0 或 "auto" 或半音, "model": 可选}, ...]，
    至少两个、最多 MULTI_MAX_ROLES 个。

    链路：
        分离一次 → 拿分句时间戳（没认过就自动认一次）→ 排分配表
        → 每个角色各跑一遍**整首** RVC（可选高频补偿）→ 按分配表交叉淡化拼接
        → 混伴奏 → 逐人取样体检

    ★ 为什么每个角色要跑整首（不是只跑分给它的那几句）：
      RVC 的 hubert 特征要上下文，两三秒的碎片音色会飘、首尾还得额外留白；
      而每条轨都等于原曲长度，拼接时**不用对轨** —— 对轨才是这类功能最容易
      出岔子的地方（差 20ms 就整段错位，而且听不出是错位，只听到"糊"）。
      代价是 N 倍推理时间，这句代价会明写在步骤条上，不让用户白等。

    seg_only（默认 MULTI_SEG_ONLY=True，2026-10-22 A/B 实测通过后翻的）：
      开=True 时改成每人只转自己分到的那几句（前后各留 1.5 秒上下文，取用时
      不带上下文），输出仍是**整首长度的轨**，所以拼接/拉平/混音一行都不用改。
      省多少由 stages/segonly.should_use 现场算，省不了的人自动退回整首。
      叠唱模式不适用（本来就是每人唱整首）。

    hf    高频补偿档位（与单角色同源的 HF_LEVELS 四档）。多角色以前**没接**
          这个开关 —— 界面上选着"中"，多角色照样不补，"设了没生效"。
    qc    逐人取样体检（multi_qc_roles）：每个人只在自己最长那段里比，
          不是拿混好的成品整曲比（那种数没有意义，见那里的说明）。
    chorus_gain  叠唱时和声层音量，None = MULTI_CHORUS_GAIN。
    """
    if not casts or len(casts) < 2:
        raise Fail("多角色合唱至少要选两个角色")
    if len(casts) > MULTI_MAX_ROLES:
        raise Fail("最多 %d 个角色（再多，推理时间与接缝都不值当）"
                   % MULTI_MAX_ROLES)
    if index_rate is None:
        index_rate = RVC_INDEX_RATE
    index_rate = float(index_rate)
    if hf not in HF_LEVELS:
        hf = "auto"
    if demetal_mode not in DEMETAL_MODES:
        demetal_mode = "auto"
    try:
        layer = MULTI_CHORUS_GAIN if chorus_gain is None else float(chorus_gain)
    except (TypeError, ValueError):
        layer = MULTI_CHORUS_GAIN
    layer = max(0.10, min(1.0, layer))
    # 叠唱（chorus）每个人本来就唱整首 —— 没有"分到的那几句"，开关对它无效。
    use_seg = bool(MULTI_SEG_ONLY if seg_only is None else seg_only)
    if use_seg and mode == "chorus":
        use_seg = False
        log(job, "  叠唱是每人唱整首，「只唱分到的几句」对它无效 ⇒ 按整首跑")

    # ---- 先校验每个人都能唱：不要唱到一半才发现第三个角色没模型 ----
    roles = []
    for c in casts:
        rid = (c or {}).get("rid")
        r = get_role(rid)
        if not r:
            raise Fail("角色不存在：%s" % rid)
        m = (c or {}).get("model") or (r.get("rvc") or {}).get("model")
        sv = (r.get("svc") or {}).get("model")
        # ★ 第二套引擎：这张卡只挂 SVC 权重也能进合唱（每个角色按自己卡上的引擎唱）。
        if not m and not sv:
            raise Fail("「%s」还没有歌声模型，唱不了。\n"
                       "多角色里**每个**角色都得有一套歌声模型（RVC 或 SVC 权重都行）—— "
                       "说话模型只念不唱。" % (r.get("name") or rid))
        roles.append({"rid": rid, "role": r, "model": m, "svc": sv,
                      "pitch": (c or {}).get("pitch", 0)})

    rid0 = roles[0]["rid"]
    final = os.path.join(role_dir(rid0), "final")
    os.makedirs(final, exist_ok=True)
    tag = "%s_%s" % (prefix, time.strftime("%m%d_%H%M%S"))
    names = [x["role"].get("name") or x["rid"] for x in roles]
    names_s = " / ".join(names)
    log(job, "多角色合唱：%s（%d 个角色，%s）"
        % (names_s, len(roles), MULTI_MODES.get(mode, mode)))

    # ★ 不能用 `wav_seconds(song_path)`：它只走 `wave` 模块，**只认整型 PCM wav**。
    #   用户的歌大多是 mp3/m4a/flac ⇒ 时长恒 0 ⇒ 第 2 步直接失败
    #   （2026-10-27 用户报的就是这个：mp3，wav_seconds=0.0 / 真实 197.544 秒）。
    #   预览那条路（`multi_plan_preview`）早就换成 `_probe_dur_safe` 了，
    #   这里一直没跟上 —— **两条路不同源**正是本项目反复踩的坑。
    try:
        song_secs = float(_probe_dur_safe(song_path) or 0.0)
    except Exception:
        song_secs = 0.0

    # ---- 第 0 步：分离（只做一次，所有人共用同一条干声）----
    job["stage"] = "分离人声"
    begin_step(job, "分离人声与伴奏",
               "把原曲拆成「人声轨」和「伴奏轨」。这一步只做一次 —— 对唱只是"
               "「谁来唱这几句」不同，所有角色唱的是同一条干声。")
    vocal_wav, inst_wav = separate_song(job, song_path, model=sep_model)
    end_step(job, True, "人声轨 + 伴奏轨已就绪")

    # ---- 分句时间戳：没认过就顺手认一次 ----
    # ★ 必须放在"排分配表"这一步**之前、之外**：recognize_lyrics 自己会
    #   begin_step/end_step，套进别的步骤里会让步骤条错位（内层结束时把
    #   内层标完成，外层那条永远停在 doing）。
    # ★ 和预览走**同一个** plan_segs：同目录 .lrc 优先，任何一档都不合并。
    #   两边不同源 ⇒ "我点了这一句却唱的是另一句"（verify_multimanual F 段守的）。
    _asr0, cached_dur = lyric_segments_any(song_path)
    segs, _seg_src, _lrc_used = plan_segs(
        song_path, dur=float(cached_dur or 0.0), log=lambda m: log(job, m))
    lyric_src = {"lrc": "lrc", "asr": "cache"}.get(_seg_src, "")
    if _seg_src == "lrc":
        log(job, "  分句用**本地 .lrc 歌词**：%d 句（一句没合并）" % len(segs))
    if mode != "chorus" and not segs:
        job["stage"] = "识别歌词（分句要用）"
        segs, cached_dur, lyric_src = ensure_lyric_times(
            job, song_path, sep_model=sep_model, auto_lyric=auto_lyric)
        if lyric_src == "failed":
            log(job, "  [warn] 没认成 ⇒ 只能按时间均分，切点会落在句子中间")
        elif lyric_src == "off":
            log(job, "  这首歌还没认过词，而自动识别被关掉了 ⇒ 按时间均分")

    # ---- 第 1 步：排分配表 ----
    job["stage"] = "排分配表" if mode != "chorus" else "准备叠唱"
    begin_step(job, "排分配表（谁唱哪几句）" if mode != "chorus" else "准备叠唱（谁垫在底下）",
               ("按歌词的**时间戳**把句子分给各个角色（%s）。句与句之间的空隙"
                "归前一句那个人 —— 这样拼起来没有缝，尾音也不会被切断。"
                % MULTI_MODES.get(mode, mode)) if mode != "chorus" else
               ("叠唱不分句：所有人同时把**整首**唱一遍，第一个人当主唱，"
                "其余角色按 %.0f%% 的音量垫在底下当和声（不做归一化 —— "
                "归一化会把主唱稀释成两个人各占一半，那就不是和声了）。"
                % (layer * 100)))
    # ★ 三个来源取最大：原曲 / 歌词缓存 / **分离后的干声**。
    #   干声最权威 —— 它才是真正被唱的那条轨，而且必然是分离器写出的 PCM wav，
    #   一定量得出来。任何一个能读到就不会再出现"时长不明"。
    total, _dur_src = _song_total_secs(song_path, cached_dur, vocal_wav)
    log(job, "  时长：%.2f 秒（原曲 %.2f / 歌词缓存 %.2f / 干声 %.2f）"
        % (total, _dur_src["song"], _dur_src["lyric"], _dur_src["vocal"]))
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import multising as _ms
    harmony_plan = []
    if mode == "chorus" and len(roles) >= 2 and harmony_mode != "off" \
            and inst_wav and os.path.isfile(inst_wav):
        # ---- 叠唱：给每个和声层挑一个**不跑调**的音高 ----
        # ★ 这一步是"多个人一起唱 ≠ 简单拼接"的关键（老板 BV1PyYReMEFL 那条要求）。
        #   第七轮的 chorus_layer 让和声层不再是主唱的复制品（相关系数 1.00 → 0.24），
        #   但它们**全在同一个音高上** —— 那还是齐唱，不是合唱。真正让它变成和声的
        #   是音高。做法不是后处理移调，而是把 --pitch 偏移加进 RVC 推理里
        #   （同一个模型的一遍转换，零额外成本，音色由模型自己合成）。
        #   算哪个音程合适：把哼唱 chroma 滚动 N 格跟伴奏比对，再用乐理先验
        #   挡掉小二度/三全音/同度 —— 详见 stages/harmony_cli.py 文件头。
        job["stage"] = "设计和声"
        begin_step(job, "设计和声声部",
                   "叠唱不等于「同一个旋律叠几遍」——那还是一个人唱得很厚。"
                   "这里按这首歌的**调**和**伴奏和声**，给每个人挑一个不会跑调的"
                   "音高：分别是主唱上/下的三度、五度、六度一类。"
                   "❖ 移调交给歌声模型自己做（把音高偏移加进推理），不是把成品"
                   "整体拉高拉低 —— 所以音质跟原来一样，不多花时间。")
        step_hint(job, "正在按这首歌调性和伴奏设计和声")
        _hrep = os.path.join(final, "_harmony.json")
        _rc, _out = run(job, '"%s" -s "%s" pick "%s" "%s" "%s" --layers %d'
                        % (PY, os.path.join(STAGES, "harmony_cli.py"),
                           vocal_wav, inst_wav, _hrep, len(roles) - 1),
                        timeout=900)
        _hd = None
        with contextlib.suppress(Exception):
            with open(_hrep, encoding="utf-8") as f:
                _hd = json.load(f)
        with contextlib.suppress(Exception):
            os.remove(_hrep)
        if _hd and _hd.get("ok") and _hd.get("plan"):
            harmony_plan = _hd["plan"]
            _desc = "、".join(
                "%s %+d 半音（%s）" % (names[p["layer"]], p["semitone"],
                                     p["name"])
                for p in harmony_plan
                if p["layer"] < len(names))
            log(job, "  调 = %s（人声中位 %.0f Hz）⇒ %s"
                % (_hd.get("key") or "未知", _hd.get("f0_med") or 0.0, _desc))
            if _hd.get("underfull"):
                # ★ 照实说：不够分就是不够分，不许给一份看着正常的脏和声。
                log(job, "  ⚠ 这首歌的可用音域只够分 %d 个不同声部（要 %d 个）"
                         " ⇒ 剩下的人唱原调 —— 再往下就是不协和的音程了，"
                         "不如让他们跟着主唱一起唱。"
                    % (_hd.get("got_layers") or 0, _hd.get("layers") or 0))
            end_step(job, True, "%d 个和声声部已设计" % len(harmony_plan))
        else:
            log(job, "  [warn] 和声设计没跑通（%s），这次是齐唱 —— 不影响成品"
                % ((_hd or {}).get("reason") or "退出码 %s" % _rc)[:80])
            end_step(job, True, "和声设计没跑通，这次按齐唱叠（不影响成品）")

    if mode == "chorus":
        # 叠唱：整首给第一个人（分配表只用来记时长与"主唱是谁"）
        takes, degraded = [(0.0, total, 0)], False
        log(job, "  叠唱：%s 主唱，%s 按 %.0f%% 音量垫底"
            % (names[0], "、".join(names[1:]), layer * 100))
    else:
        _gi = {}
        try:
            takes, _line_roles, degraded = _ms.plan_takes(
                segs or [], total, len(roles), mode=mode, manual=manual, info=_gi)
        except ValueError as e:
            # ★ 保底：现在的 assign_lines 对"份数对不上"是**按时间对齐**（不抛），
            #   这里只剩"连时间都量不出来"这种极端情况才会走到。
            #   对齐不是为了静默 —— 对齐过这件事在下面会当场说出来。
            log(job, "  [warn] %s" % e)
            end_step(job, False, "逐句指定对不上")
            raise Fail(str(e))
        # ★★ 这两条必须**当场说出来**，否则用户拿到的是一份"看着正常"的表，
        #   却不知道它到底是自己点的还是系统替他排的（2026-10-24 第五轮）。
        if _gi.get("remap"):
            log(job, "  ⚠ 你点的是 %d 段，这首歌现在切出 %d 句（多半是点完之后"
                     "又认了一次词）⇒ 已按**时间**对齐：你点的那几段落在同样的"
                     "秒数上，指定的意图没变。"
                % (_gi["remap"].get("from"), _gi["remap"].get("to")))
        elif _gi.get("manual_empty"):
            log(job, "  ⚠ 选了「逐句指定」但一句都还没点 ⇒ 先按**逐句轮流**排了"
                     "一份（每个人都唱得到）。想按你的来，回面板点几行再跑。")
        # ★ 「组数 < 人数」是真实会发生的：16 秒片段 4 句、句间空档都不到 2 秒
        #   ⇒ group_lines 只给出 1 组 ⇒ 组数 1 < 人数 2 ⇒ 第二个人一句都分不到。
        #   以前只在末尾写一句"× 一句都没分到"，用户听到独唱根本不会去看日志。
        #   现在 multising 那边已经会自动把组拆开（切点仍在句间），这里把"拆过"
        #   和"拆不动"都当场说出来。
        if _gi.get("split"):
            log(job, "  ⚠ 这首歌只分出 %d 个乐句组，少于 %d 个人 ⇒ 已按句子对半拆成 "
                     "%d 组（不然「%s」一句都唱不到）。切点仍然落在句与句之间。"
                % (_gi.get("groups_before"), len(roles), _gi.get("groups"),
                   names[1] if len(names) > 1 else "后面的人"))
        elif _gi.get("short"):
            log(job, "  ⚠ 这首歌只有 %d 句、却有 %d 个人 —— 拆到不能再拆，"
                     "注定有人一句都唱不到。想每个人都出声，请用逐句轮流或手动指定。"
                % (len(segs or []), len(roles)))
    # ★ 这两件事必须**分开报**。上一版是一句话盖住两种情况：
    #   「时长不明（分离后的干声读不出来）」—— 用户实测干声明明是 197.504 秒、
    #   标准 16-bit PCM，真正读不出来的是 mp3 原曲。文案指错方向，用户会去
    #   重新分离（没用）而不是查原曲格式。⇒ 量不出就**逐一点名**哪个来源是 0。
    if total <= 0:
        raise Fail("排不出分配表：量不出这首歌的时长。三个来源都是 0 ——\n"
                   "  · 原曲（%s）：%s\n"
                   "  · 分离后的干声（%s）：%s\n"
                   "  · 歌词缓存：没有\n"
                   "干声是分离器刚写出来的 PCM wav，它都量不出来说明分离产物本身"
                   "坏了（或磁盘上已被删）⇒ 请把这首歌重新分离一次；"
                   "原曲那一项为 0 多半是格式读不了（mp3/m4a 要走 ffprobe，"
                   "检查 FFPROBE 是否还在）。"
                   % (os.path.basename(song_path or "(空)"),
                      "读不出" if not _dur_src.get("song") else "%.2f 秒" % _dur_src["song"],
                      os.path.basename(vocal_wav or "(空)"),
                      "读不出" if not _dur_src.get("vocal") else "%.2f 秒" % _dur_src["vocal"]))
    if not takes:
        raise Fail("排不出分配表：分不出任何一段（时长已知 %.2f 秒，但分句与分配全空）"
                   % total)
    if degraded:
        log(job, "  ⚠ 拿不到每句的时间戳 ⇒ 只能按时间均分，"
                 "切点会落在句子中间。想要真分句，先去「歌词」里认一次这首歌。")
    elif mode != "chorus":
        sw = sum(1 for i in range(1, len(takes)) if int(takes[i][2]) != int(takes[i - 1][2]))
        log(job, "  分句：%d 句 → %d 段，角色切换 %d 次" % (len(segs), len(takes), sw))
    _stt = _ms.takes_stats(takes, len(roles)) if mode != "chorus" else []
    for stt in _stt:
        log(job, "    %s：%.1f 秒 / %d 段"
            % (names[stt["role_index"]], stt["sec"], stt["parts"]))
    # ★ 一个人一句都没分到 = 这次"合唱"其实只有一个声音。这不是错误（按段落轮流
    #   遇到只有一段的歌就会这样），但必须**当场说出来** —— 否则用户听到"怎么
    #   还是一个人"，却以为是音色没换成功。
    _zero = [names[st["role_index"]] for st in _stt if st["sec"] < 0.5]
    if _zero:
        log(job, "  ⚠ 「%s」一句都没分到（这种分配方式下只有别人在唱）。"
                 "想让它出声：换「逐句轮流」，或用「手动指定」把几句点给它。"
                 % "」「".join(_zero))
    plan_path = os.path.join(final, "plan_%s.json" % tag)
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"duration": total, "takes": takes, "mode": mode,
                   "lines": len(segs or []),
                   "roles": [{"rid": x["rid"], "name": names[i]}
                             for i, x in enumerate(roles)]},
                  f, ensure_ascii=False, indent=1)
    end_step(job, True, "%d 段已分配（%s）" % (len(takes), MULTI_MODES.get(mode, mode)))

    # ---- 第 2 步：每个角色各唱一整首 ----
    est_each = rvc_est_seconds(wav_seconds(vocal_wav) or song_secs or 0)
    outs = []
    for k, x in enumerate(roles):
        nm = names[k]
        # ★ 人声轨在**共享缓存**里（data/cache/sep/<key>），不是这个任务独占。
        #   实测（2026-10-20）：两个服务同时活着、其中一个在清理时，第一个角色
        #   唱完后那份 vocal.wav 被删掉，第二个角色当场报 `[err] 输入不存在` ——
        #   这句话读起来像"角色模型坏了"，其实是缓存没了、模型一点事没有。
        #   ⇒ 提前判一次，把话说成人话，并指明"重跑就行"。
        if not os.path.isfile(vocal_wav):
            end_step(job, False, "人声轨不见了")
            raise Fail(
                "分离好的人声轨不见了：%s\n这条路用的是共享缓存（同一首歌的多个任务"
                "共用一份），很可能被同时运行的清理动作删掉了。\n"
                "角色模型没问题 —— 重新跑一次就会重新分离。" % vocal_wav)
        job["stage"] = "歌声转换 %d/%d" % (k + 1, len(roles))
        begin_step(job, "歌声转换 %d/%d（%s）" % (k + 1, len(roles), nm),
                   "用「%s」的音色唱一遍。默认**只唱分到的那几句**（前后各留 1.5 秒"
                   "上下文，转完放回原位、其余静音）—— 出来的仍是整首长度的轨，"
                   "音色与整首转换实测一致（谱相关 0.94），但快得多。"
                   "分到超过全曲一半、或段太碎，会自动退回整首并在日志里写清原因。"
                   "每个角色都要跑一遍，总时间约是单角色的 %d 倍。"
                   "（RVC 内部按 20 秒一片推理，片长是从本机实测挑的 —— "
                   "片太长会退化，详见 stages/rvc_infer.py）"
                   % (nm, len(roles)))
        p = x["pitch"]
        if isinstance(p, str) and p.strip().lower() == "auto":
            step_hint(job, "正在估算「%s」该升/降几个半音" % nm)
            p, why = estimate_pitch(job, vocal_wav, x["role"])
            log(job, "  %s：自动音高 %+d 半音（%s）" % (nm, p, why))
        p = int(p)
        # ★ 和声声部：第 2 个人起，在各自音高上再加一个**和声音程**。
        #   这不是把成品拉高拉低，是让模型按这个音高重唱一遍 ⇒ 音色不变、
        #   出来的是真和声。零额外推理（本来每人就要跑一遍）。
        _hs = 0
        for _hp in harmony_plan:
            if int(_hp.get("layer") or 0) == k:
                _hs = int(_hp.get("semitone") or 0)
        if _hs:
            p += _hs
            x["harmony_semitone"] = _hs
            log(job, "  %s：和声声部 %+d 半音（%s）"
                % (nm, _hs, next((_q["name"] for _q in harmony_plan
                                  if int(_q.get("layer") or 0) == k), "")))
        idx = _find_rvc_index(x["model"]) if x.get("model") else ""
        ir = index_rate
        if ir > 0 and not idx:
            log(job, "  %s 的模型没有特征索引库，改用 index-rate 0" % nm)
            ir = 0
        out_wav = os.path.join(final, "voice_%s_r%d.wav" % (tag, k))
        # ---- 只转自己分到的那几句？（seg-only）----
        # 现场算一笔账：省不了多少的人自动退回整首（见 stages/segonly.should_use）。
        # ★ 送进 RVC 的是"这几句 + 前后各 1.5 秒上下文"拼成的短轨，转完再**放回
        #   原时间轴**，其余置零 ⇒ 出来的仍是整首长度的轨，下游一行都不用改。
        seg_map, seg_src, this_seg = None, None, False
        if use_seg:
            try:
                if STAGES not in sys.path:
                    sys.path.insert(0, STAGES)
                import segonly as _so
            except ImportError:
                _so = None
                use_seg = False
                log(job, "  [warn] 找不到 segonly 模块 ⇒ 全部按整首跑")
            if _so is not None:
                ok, why, dd = _so.should_use(takes, k, total)
                this_seg = bool(ok)
                log(job, "  %s：%s" % (nm, why))
                if ok:
                    _ws = _so.plan_windows(
                        _so.merge_segments(_so.takes_of(takes, k)), total)
                    seg_src = os.path.join(final, "seg_%s_r%d_in.wav" % (tag, k))
                    seg_map = _so.build_input(vocal_wav, _ws, seg_src)
                    if not (seg_map or {}).get("wins"):
                        this_seg, seg_map, seg_src = False, None, None
                        log(job, "  %s：拼不出可转的片段 ⇒ 按整首跑" % nm)
        conv_in = seg_src if this_seg else vocal_wav
        conv_out = (os.path.join(final, "seg_%s_r%d_conv.wav" % (tag, k))
                    if this_seg else out_wav)
        _est = (rvc_est_seconds((seg_map or {}).get("dur") or 0)
                if this_seg else est_each)
        _what = "只唱分到的那几句" if this_seg else "重唱整首"
        hb = (_est, "「%s」正在%s（按本机速率约 %d 分钟）"
              % (nm, _what, round(_est / 60.0)))
        step_hint(job, "「%s」正在%s —— 显卡会被占满" % (nm, _what))
        # ★ 引擎分流（和单角色同一条入口）：只有 SVC 权重的卡走第二引擎，
        #   千万别先拿空模型调一次 rvc_convert —— 那样会白跑一次、日志里还会
        #   多一行「歌声转换（RVC）」失败，看着像模型坏了。
        #   后面所有后处理（高频/去金属/临场感/唱后微调/3~10k 补偿/保护/混音）完全共用。
        if x.get("svc") and not x.get("model"):
            rc, out = svc_convert(job, x["role"].get("svc") or {},
                                  conv_in, conv_out, p)
            if rc == 0 and not os.path.exists(conv_out):
                rc = 1
        else:
            rc, out = rvc_convert(job, x["model"], idx, conv_in, conv_out, p, ir,
                                  rms_mix_rate, protect, hb)
        if rc != 0 and ir > 0 and x.get("model"):
            log(job, "  %s 带索引失败，去掉索引重试一次（音色细节会少一点）" % nm)
            rc, out = rvc_convert(job, x["model"], idx, conv_in, conv_out, p, 0,
                                  rms_mix_rate, protect, hb)
        if rc != 0 or not os.path.exists(conv_out):
            end_step(job, False, "「%s」转换失败" % nm)
            raise Fail("「%s」的歌声转换失败（退出码 %d）。\n%s"
                       % (nm, rc, _tail(out, 4)))
        x["pitch_used"] = p
        x["index_rate"] = ir
        # ---- 高频补偿（与单角色同一个函数、同一套档位）----
        # ★ 多角色以前完全没接这个开关：界面上选着"中"，多角色照样不补，
        #   用户听不出来是"没生效"还是"这档没用"。现在按同一档补，并把
        #   实际分频点记进任务参数。
        # ★ seg-only 时参考轨也要换成**那条拼起来的源轨**：补偿是"拿原唱的高频
        #   补回去"，两边必须是同一段时间轴；用整首去补一条只有几句的轨，
        #   静音段会凭空冒出原唱的气声。
        post = conv_out
        # ★ 参考轨在这里就算好：高频补偿和去金属都要用**同一条**（时间轴一致）。
        #   以前它定义在 `if hf != "off"` 里面，那么 hf=关 时去金属会拿到一个
        #   没定义的名字 —— 直接 NameError 把整首歌带死。
        _ref = seg_src if this_seg else vocal_wav
        if hf != "off":
            step_hint(job, "「%s」正在补高频（%s）"
                      % (nm, HF_LABEL.get(hf, hf).split(" —— ")[0]))
            hf_out = os.path.join(final, "voice_%s_r%d_hf.wav" % (tag, k))
            post2, hf_info = hf_brighten(job, conv_out, _ref, hf_out, hf)
            if hf_info:
                post = post2
                x["hf_fc"] = hf_info.get("fc")
                log(job, "  %s：已补高频（分频 %d Hz）" % (nm, hf_info["fc"]))
        # ---- 去金属声：和高频补偿用**同一条参考轨**（时间轴必须一致）----
        # 放在 scatter 之前：那时它还是"只含自己那几句"的短轨，参考轨也正好
        # 是裁过的 seg_src，两边对齐。scatter 之后再处理的话，整轨里大半是静音，
        # 静音会把 HNR / jitter 的统计拉坏（praat 给静音打 -200 标记）。
        if demetal_mode != "off":
            dm_out = os.path.join(final, "voice_%s_r%d_dm.wav" % (tag, k))
            post3, dm_info = demetal(job, post, _ref, dm_out, demetal_mode)
            if dm_info:
                post = post3
                _a = dm_info.get("auto") or {}
                log(job, "  %s：已去电音（ΔHNR %.1f → %.1f dB）"
                    % (nm, _a.get("dhnr") or 0.0, _a.get("after_dhnr") or 0.0))
        # ---- 临场感补偿：同样放在 demetal 之后（先补再削 = 白补）----
        # 用的还是 _ref（跟 hf / demetal 同一条参考轨，时间轴一致）。
        if presence_mode != "off":
            pr_out = os.path.join(final, "voice_%s_r%d_pr.wav" % (tag, k))
            post4, pr_info = presence(job, post, _ref, pr_out, presence_mode)
            if pr_info:
                post = post4
                _a = pr_info.get("auto") or {}
                log(job, "  %s：已补临场感（+%.1f dB，缺口 %.1f → %.1f）"
                    % (nm, _a.get("dose_db") or 0.0,
                       (_a.get("measure") or {}).get("deficit_db") or 0.0,
                       _a.get("after_deficit_db") or 0.0))
        # ---- 放回原时间轴（seg-only），或直接就是成品轨 ----
        used_wav = out_wav
        if this_seg and seg_map:
            _so.scatter(post, seg_map, total, out_wav)
            # 中间那条"拼接轨"用完就删：一首 3 人合唱能省几百 MB，
            # 而且它留着会被后面的清理逻辑当成孤儿文件。
            for _f in (seg_src, conv_out):
                try:
                    if _f and os.path.isfile(_f):
                        os.remove(_f)
                except OSError:
                    pass
        else:
            used_wav = post
        x["wav"] = used_wav
        outs.append(used_wav)
        end_step(job, True, "「%s」已唱完（音高 %+d%s%s）"
                 % (nm, p, "，已补高频" if x.get("hf_fc") else "",
                    "，只转了自己那几句" if this_seg else ""))

    # ---- 第 3 步：按分配表拼接 ----
    job["stage"] = "叠加和声" if mode == "chorus" else "拼接多角色"
    if mode == "chorus":
        begin_step(job, "叠加和声",
                   "把主唱和几层和声叠在一起。各角色先拉平响度（不拉平就是"
                   "「另一个人在吼」），和声层按 %.0f%% 的音量垫在底下；"
                   "叠到接近削顶就整体缩放，不硬削。" % (layer * 100))
        step_hint(job, "正在叠加，并检查会不会削顶")
    else:
        begin_step(job, "拼接（按分配表交叉淡化）",
                   "把几条整首的轨按分配表拼成一条人声轨。接缝处做 20 毫秒交叉淡化"
                   "（不然是一个样本跳满幅，听见的是「啪」），并先把各角色的响度"
                   "拉到同一水平 —— 不拉平就是一句大一句小。")
        step_hint(job, "正在按分配表拼接并拉平各角色响度")
    voice_wav = os.path.join(final, "voice_%s.wav" % tag)
    rep_path = os.path.join(final, "multi_%s.json" % tag)
    q = lambda s: '"%s"' % s
    if mode == "chorus":
        cmd = "%s -s %s --layer %.2f --out %s --report %s %s" % (
            q(PY), q(os.path.join(STAGES, "multising.py")),
            layer, q(voice_wav), q(rep_path),
            " ".join("--track %s" % q(w) for w in outs))
    else:
        # ★ --cgain：分配表里**逐句指定的合唱**（role=-1）也要听这个和声音量。
        #   以前只有整首叠唱（mode=chorus）才传 ⇒ 逐句合唱那段会用模块默认值，
        #   而界面上那个"和声音量"滑块对它完全无效 —— 调了没反应。
        cmd = "%s -s %s --plan %s --out %s --report %s --cgain %.2f %s" % (
            q(PY), q(os.path.join(STAGES, "multising.py")), q(plan_path),
            q(voice_wav), q(rep_path), layer,
            " ".join("--track %s" % q(w) for w in outs))
    rc4, out4 = run(job, cmd, timeout=1800)
    if rc4 != 0 or not os.path.exists(voice_wav):
        end_step(job, False, "拼接失败")
        raise Fail("多角色拼接失败（退出码 %d）。\n%s" % (rc4, _tail(out4, 5)))
    blend_info = {}
    try:
        with open(rep_path, encoding="utf-8") as f:
            blend_info = json.load(f)
    except Exception:
        pass
    for ln in (out4 or "").splitlines():
        if ln.strip().startswith("轨 ") or ln.strip().startswith("角色 "):
            log(job, "  " + ln.strip())
    end_step(job, True,
             ("和声叠加完成（峰值 %.2f）" % (blend_info.get("peak") or 0))
             if mode == "chorus" else
             ("%d 段拼成一条（%d 次接缝）"
              % (blend_info.get("takes", len(takes)), blend_info.get("seams", 0))))

    # ---- 第 4 步：逐人取样体检 ----
    # 放在混伴奏之前：混音失败也还能拿到体检结果，不用重跑 N 倍推理。
    qc_items = []
    if qc:
        job["stage"] = "逐人体检"
        begin_step(job, "逐人取样体检（每人一段）",
                   "把每个角色**自己那条轨**在它负责的那一段里和原唱比 —— "
                   "等价于 N 次单角色质检。不拿混好的成品整曲比：那样 A 的段会"
                   "按 B 的音高去比，得出的数是假的。取样窗会写在结果里。")
        step_hint(job, "正在逐人比对音高")
        try:
            qc_takes = takes if mode != "chorus" else \
                [(0.0, total, k) for k in range(len(roles))]
            qc_items = multi_qc_roles(job, vocal_wav, outs, names,
                                      [x.get("pitch_used", 0) for x in roles],
                                      qc_takes, total)
        except BaseException as e:      # 体检失败绝不能把成品带死
            log(job, "  [warn] 逐人体检没跑通（%s），成品照常出" % str(e)[:160])
            qc_items = []
        for ln in describe_multi_qc(qc_items):
            log(job, ln)
        good = sum(1 for it in qc_items if it.get("match") is not None)
        end_step(job, True, "%d/%d 人量出结果" % (good, len(roles)))

    # ---- 第 5 步：混伴奏 ----
    mix_wav = None
    if mix:
        mix_wav = os.path.join(final, "mix_%s.wav" % tag)
        job["stage"] = "混合成品"
        begin_step(job, "混合成品", "伴奏轨 + 拼好的人声，自动对齐响度、压掉残留底噪。")
        step_hint(job, "正在把人声和伴奏合到一起")
        mix_wav, tail5 = _multi_mix(job, inst_wav, voice_wav, mix_wav)
        if mix_wav:
            end_step(job, True, "成品轨已生成")
        else:
            log(job, "  [warn] 混合轨没做成，人声干轨仍可用：%s" % tail5)
            end_step(job, True, "混合轨没做成，人声干轨仍可用（已说明）")


    tracks = [{"label": "人声（干声）", "wav": voice_wav}]
    if mix_wav:
        tracks.insert(0, {"label": "成品（人声+伴奏）", "wav": mix_wav})

    # ★ 每个人的**整首独唱**：这几个文件在第 2 步就已经存在了（每个角色把整首
    #   唱了一遍），以前只是中间产物，拼完就静静躺在 final 目录里 —— 用户想单独
    #   听某个人的版本只能去翻硬盘。现在把它们登记成"可选音轨"，界面上点一下
    #   就能听、能下载，**一次推理都不用多跑**。
    #   mix=None 表示"带伴奏版还没做"：混音本身只要几秒，但每个版本都是一整首歌
    #   的大小（8 个人就是几百 MB），所以**按需生成**，不预先占盘。
    solo_tracks = [{"rid": x["rid"], "name": names[i],
                    "dry": outs[i], "mix": None}
                   for i, x in enumerate(roles)]

    # ★ 多人合唱也是"一首歌"，同样要带本来的字幕。
    #   这里 `segs` 是**整首**的分句（分配表是 `takes`），所以拼出来就是原词全文，
    #   跟单角色那条路一致 —— 字幕不带"谁唱的"，那是视频字幕（vizsubs）的活。
    _lyr = "\n".join(str(s.get("text") or "").strip()
                     for s in (segs or [])
                     if str(s.get("text") or "").strip())

    if save:
        def _add(r):
            for t in tracks:
                r.setdefault("outputs", []).append({
                    "text": "多人合唱：%s" % names_s, "wav": t["wav"],
                    "at": _now(), "kind": "singing", "track": t["label"],
                    "seconds": round(wav_seconds(t["wav"]), 2),
                    "lyric": _lyr,
                    "lyric_srt": _write_srt(t["wav"], _lyr, segs, song_path),
                    "stems": {"mix": mix_wav, "voice": voice_wav,
                              "inst": inst_wav, "src": song_path},
                    # ★ 每个人的音高都要记：下次听到"某一句明显跑调"，
                    #   能看出是那个角色的音高设错了，还是拼接错了。
                    "params": {
                        "multi_mode": mode,
                        "multi_tag": tag,
                        "multi_lines": len(segs or []),
                        # ★ 分句**存进成品自己身上**，不只留在歌词缓存里。
                        #   实测教训：缓存是按「路径+大小+mtime」命名的临时文件，
                        #   一被清理（清缓存 / 换机器 / 别的门禁顺手删），这条成品的
                        #   字幕就永久变空 —— 界面上只能让人"重新认词"，白花两分钟。
                        #   成品自带一份，"谁在第几秒唱哪句"就永远查得回来。
                        "multi_segs": _segs_brief(segs),
                        "multi_degraded": bool(degraded),
                        "multi_lyric_src": lyric_src or "off",
                        "multi_duration": round(float(total or 0), 2),
                        "multi_roles": [
                            {"rid": x["rid"], "name": names[i],
                             "pitch": x.get("pitch_used", 0),
                             "model": os.path.basename(x["model"] or ""),
                             "index_rate": x.get("index_rate", 0),
                             "hf_fc": x.get("hf_fc")}
                            for i, x in enumerate(roles)],
                        # 每人整首独唱（上面那段有说明）：dry 一定有，mix 按需生成
                        "solo_tracks": solo_tracks,
                        "seams": blend_info.get("seams", 0),
                        "gains": blend_info.get("gains") or [],
                        # 叠唱专用：和声音量 + 有没有为防削顶整体缩过
                        "chorus_gain": (layer if mode == "chorus" else None),
                        "peak_guard": blend_info.get("peak_guard"),
                        # ★ 下面这几项是「换分配方式重拼」（multi_resplit）的原料：
                        #   重拼只重排 + 重拼 + 重混，**一次推理都不跑**。
                        #   没记它们的话，用户想换一种分配方式就得再等 N 倍时间。
                        "multi_takes": [[round(float(t[0]), 3), round(float(t[1]), 3),
                                         int(t[2])] for t in takes],
                        "multi_tracks": [os.path.basename(w) for w in outs],
                        "hf": hf,
                        # 逐人取样体检（每人一段，窗口写在里面）
                        "multi_qc": qc_items,
                    },
                })
        mutate_role(rid0, _add)
        log(job, "  成品记在「%s」名下（%d 个角色共用这一条）" % (names[0], len(roles)))
    return tracks


# ------------------------------------------------ 多角色：分配表预览 / 换法重拼
def multi_plan_preview(song_path, casts, mode="phrase", manual=None, lang="zh"):
    """**排一张分配表但不唱**：给界面"手动指定每一句"和"这次谁唱多少"用。

    返回的 lines 是逐句的（起止秒 + 文本 + 默认归谁）；takes 是拼好的时间轴。
    ★ 一次推理都不跑，纯算时间轴 —— 所以界面可以随时重排、随便点。
    ★ 没认过歌词时 lines 是空的、lyric_ready=False，但**仍然给出一份均分表**：
      让用户先看见"这首歌会被切成几段"，而不是空白一片。
    """
    if not song_path or not os.path.isfile(song_path):
        raise Fail("歌曲文件不存在：%s" % song_path)
    names = []
    for c in casts or []:
        r = get_role((c or {}).get("rid"))
        if not r:
            raise Fail("角色不存在：%s" % (c or {}).get("rid"))
        names.append(r.get("name") or r.get("id"))
    n = max(1, len(names))
    _asr_segs, cached_dur = lyric_segments_any(song_path, lang)
    # ★ 以前这里用 `wav_seconds()` —— 它只走 `wave` 模块，**只认 wav**。
    #   用户的歌大多是 mp3/m4a ⇒ 时长恒 0 ⇒ 分配表整片 0 秒、逐段指定也没有段。
    #   改用 `_probe_dur_safe()`（wave 不行就退 ffprobe）。
    try:
        dur = float(_probe_dur_safe(song_path) or 0.0)
    except Exception:
        dur = 0.0
    dur = max(dur, float(cached_dur or 0.0))
    if mode not in MULTI_MODES:
        mode = "phrase"

    # ★★ 没认过词时也要能**逐句指定**（老板：「我希望可以自己决定哪一句谁唱」）。
    #   以前这里 segs 一空，plan_takes 第一句就是 `if not segs: return takes_even(...)`
    #   —— 直接把 manual 丢掉，用户点了一份分配表、点开会唱，结果还是均分。
    #   现在把"按时间均分"的分段当成伪句子（text 留空），manual 才有落脚点。
    #   `lyric_ready` 仍然报**真歌词**有没有，界面据此换文案（按时间切的段 ≠ 句子）。
    _real_lyric = bool(_asr_segs)

    # ---- ★ 分句（2026-10-25 第十轮）----------------------------------
    # 老板：「自选句子不完整 / 我要中英双语 / 不会压缩综合 / 按歌词文件的分段」
    # ⇒ 有同目录 .lrc 就用它的分段，一句一行；没有就退回 ASR 的句。
    # **两档都不合并** —— 以前那步"按换气把 60 句合成 21 个乐句"正是
    # "句子不完整"和"这句该给谁点不了"的根因（实测 13 首平均 61.9 → 21.2）。
    # 中文在 plan_segs 里补齐（翻一次按指纹存下，下次秒回）。
    # ★ 只认 ASR 缓存来判"这首歌认过词没有"：lrc 是现成的，不代表认过。
    segs, seg_src, lrc_used = plan_segs(song_path, lang=lang, dur=dur)
    _real_lyric = bool(segs)
    _n_asr = len(_asr_segs or [])

    if not segs and dur > 0 and mode != "chorus":
        if STAGES not in sys.path:
            sys.path.insert(0, STAGES)
        import multising as _ms0
        segs = [{"start": float(_s), "end": float(_e), "text": ""}
                for _s, _e, _r in _ms0.takes_even(dur, n)]

    if mode == "chorus":
        takes, line_roles, degraded = [(0.0, dur, 0)], [], False
        hint2 = ""
    else:
        if STAGES not in sys.path:
            sys.path.insert(0, STAGES)
        import multising as _ms
        _gi2 = {}
        takes, line_roles, degraded = _ms.plan_takes(
            segs or [], dur, n, mode=mode, manual=manual, info=_gi2)
        # ★ 这里**不能** log(job, ...)：预览是纯函数（没有 job，跑在
        #   /api/multi/plan 的请求线程里），写 job 日志会当场 NameError，
        #   整个预览接口 500。既然它的活儿是"先让用户看见"，就把话放进返回值里。
        #   文案要**短**：预览区在页面上只有两行高，一条长句会把整块挤到 90px 以上
        #   （门禁 B 段就是量这个高度，实测 93px 报红）。
        # ★★ 逐句指定（manual）的两件事必须**说出来**，不然界面上就是一份
        #   "看着正常"的表，用户不知道自己到底指定了什么（2026-10-24 第五轮）。
        _rm = _gi2.get("remap")
        if _rm:
            # 认一次词句数就变了（老板这次：14 段 → 86 句）。以前是直接报错、
            # 任务失败、他的 14 次点击全废；现在按**时间**对齐，必须告诉他。
            hint2 = ("你点的 %d 段已按**时间**对齐到现在的 %d 句"
                     "（认完词句数变了，时间段没变）"
                     % (_rm.get("from"), _rm.get("to")))
        elif _gi2.get("manual_empty"):
            hint2 = "还没指定 ⇒ 先按**逐句轮流**排了一份，点下面的行就换成你的"
        elif _gi2.get("split"):
            hint2 = ("只有 %d 个乐句组（少于 %d 人）⇒ 已按句拆成 %d 组，"
                     "不然有人唱不到" % (_gi2.get("groups_before"), n,
                                        _gi2.get("groups")))
        elif _gi2.get("short"):
            hint2 = ("%d 句 < %d 人，拆不动：注定有人唱不到"
                     "（改用逐句轮流）" % (len(segs or []), n))
        else:
            hint2 = ""
        # ★ 句子是哪来的必须说出来（2026-10-25 第十轮）：老板要"按歌词文件的
        #   分段、一句都不合并"。界面上写清楚来源和句数，他才知道这一列
        #   是不是他心里那份。放在分支链**之后**拼 —— 前面那些分支都是给
        #   hint2 首次赋值，放前面会 UnboundLocalError。
        if seg_src == "import":
            _mg = "用**你导入的那份歌词**：%d 句（原文 + 中文，一句没合并）" \
                  % len(segs or [])
        elif seg_src == "lrc":
            _mg = "按**本地 .lrc 的分段**：%d 句（原文 + 中文，一句没合并）" \
                  % len(segs or [])
        elif seg_src == "asr":
            _mg = "按识别出来的 %d 句（原文 + 中文，一句没合并）" % len(segs or [])
        else:
            _mg = ""
        if _mg:
            hint2 = (_mg + "；" + hint2) if hint2 else _mg
        stats = _ms.takes_stats(takes, n)
        # ★ 有人**一句独唱都没有**也要说：统计里他显示 0 秒，但那多半不是
        #   "没分到"，是他那份表里没给自己点独唱句（铁律 22：不许让一份 0 秒的
        #   统计默默待着 —— 它跟"没分到他"长得一模一样，而这次是两回事）。
        _zz = [names[st["role_index"]] for st in stats
               if st["role_index"] < len(names)
               and float(st.get("sec") or 0.0) < 0.5]
        _cs0 = float((stats[0] if stats else {}).get("chorus_sec") or 0.0)
        if _zz:
            _txt = ("「%s」没有独唱句（%s）—— 想让他独唱，就把几句点给他"
                    % ("」「".join(_zz),
                       ("只在这 %.1f 秒合唱里出声" % _cs0) if _cs0 > 0.05
                       else "整首一次都没出声"))
            hint2 = (hint2 + "；" + _txt) if hint2 else _txt
        elif _cs0 > 0.05:
            # 合唱时长要**单独说出来**：它不算进任何人的独唱秒数（否则"谁唱了
            # 多少"加起来会超过曲长），但用户必须知道"这几句是大家一起唱的"。
            hint2 = (hint2 + "；" if hint2 else "") + \
                    "其中 %.1f 秒是**合唱**（这几句所有人一起唱）" % _cs0
    if mode == "chorus":
        stats = [{"role_index": 0, "sec": round(dur, 2), "parts": 1}] + \
                [{"role_index": k, "sec": round(dur, 2), "parts": 1}
                 for k in range(1, n)]
    lines = []
    for i, s in enumerate(segs or []):
        lines.append({"i": i,
                      "start": round(float(s.get("start") or 0), 2),
                      "end": round(float(s.get("end") or 0), 2),
                      "text": (s.get("text") or "").strip(),
                      # ★ 中文：界面上直接显示，省得他再对着英文猜是哪一句
                      "zh": (s.get("zh") or "").strip(),
                      # ★ 这一句是由 ASR 的几句合来的（没合并就是 1）
                      "n_sub": int(s.get("n_sub") or 1),
                      "role": int(line_roles[i]) if i < len(line_roles) else 0})
    return {
        "duration": round(dur, 2),
        "lyric_ready": _real_lyric,
        "degraded": bool(degraded),
        "mode": mode,
        "names": names,
        "lines": lines,
        "takes": [[round(float(t[0]), 3), round(float(t[1]), 3), int(t[2])]
                  for t in takes],
        # ★ chorus_sec 必须一起带出去：界面靠它说"另含合唱 N 秒"。
        #   只拷 name/sec/parts（第一版就是这样）⇒ 前端拿到的 per_role 里
        #   根本没有这个键，判据和界面两边都看不到合唱 —— 而分配表本身是对的。
        "per_role": [{"name": names[st["role_index"]] if st["role_index"] < len(names)
                      else "#%d" % st["role_index"],
                      "sec": st["sec"], "parts": st["parts"],
                      "chorus_sec": round(float(st.get("chorus_sec") or 0.0), 2)}
                     for st in stats],
        "can_manual": bool(lines),
        # ★ 句子是哪来的（lrc = 同目录 .lrc 的分段 / asr = 认出来的 /
        #   even = 按时间均分的伪句子）+ 中文齐不齐 —— 界面要照实写出来，
        #   否则他不知道这一列能不能信（老板要"中英双语、按歌词文件的分段"）。
        "line_src": seg_src if segs else "even",
        "zh_ready": bool(any((ln.get("zh") or "").strip() for ln in lines)),
        "lrc": lrc_used or "",
        # 「谁唱多少」之外还要说清"有没有人被挤掉了"：界面拿 hint 直接显示。
        "hint": hint2,
        "split": bool(_gi2.get("split")) if mode != "chorus" else False,
        "short": bool(_gi2.get("short")) if mode != "chorus" else False,
    }


def sing_multi_resplit(job, rid, index=None, mode=None, manual=None,
                       chorus_gain=None, save=True):
    """**换一种分配方式重拼**：不重跑推理，只重排 + 重拼 + 重混。

    为什么值得单独做：多角色的贵在"每个角色各唱一整首"（N 倍时间）。
    「逐句轮流」听腻了想换「按段落轮流」，本质上只是换一张分配表 ——
    重跑一遍等于把最贵的那步白跑一次。这里直接拿上一版留下的 N 条角色轨，
    重新拼接，几秒钟出结果。

    ★ 只对**新版**成品有效（params 里得有 multi_tracks）。旧成品没留分轨，
      会明确说"重跑一次才能换分配方式"，而不是给一个空成品。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在：%s" % rid)
    outs_all = role.get("outputs") or []
    src = None
    if index is not None:
        try:
            src = outs_all[int(index)]
        except (IndexError, ValueError, TypeError):
            src = None
        if not src:
            raise Fail("找不到第 %s 条成品" % index)
    else:
        for o in reversed(outs_all):
            if (o.get("params") or {}).get("multi_tracks"):
                src = o
                break
    if not src:
        raise Fail("这个角色名下没有多角色成品")
    p0 = src.get("params") or {}
    if not p0.get("multi_tracks"):
        raise Fail("这一版是旧版本做的，没留下每个人的分轨 ⇒ 换不了分配方式。\n"
                   "重跑一次多角色合唱（同样的角色和歌），之后就能秒换。")
    if p0.get("multi_mode") == "chorus":
        raise Fail("叠唱版本没有「分配表」可换 —— 它本来就是所有人同时唱整首。")

    final = os.path.join(role_dir(rid), "final")
    names = [x.get("name") or x.get("rid") for x in (p0.get("multi_roles") or [])]
    n = len(p0["multi_tracks"])
    tracks = [os.path.join(final, os.path.basename(w)) for w in p0["multi_tracks"]]
    miss = [t for t in tracks if not os.path.isfile(t)]
    if miss:
        raise Fail("上一版的分轨被清理掉了（%s）⇒ 得重跑一次多角色。"
                   % os.path.basename(miss[0]))
    stems = src.get("stems") or {}
    inst_wav, song_path = stems.get("inst"), stems.get("src")
    if not inst_wav or not os.path.isfile(inst_wav):
        raise Fail("找不到上一版的伴奏轨，没法重混 ⇒ 得重跑一次多角色。")
    mode = mode or p0.get("multi_mode") or "phrase"
    if mode not in MULTI_MODES or mode == "chorus":
        raise Fail("不认识的分配方式：%s" % mode)

    try:
        layer = MULTI_CHORUS_GAIN if chorus_gain is None else float(chorus_gain)
    except (TypeError, ValueError):
        layer = MULTI_CHORUS_GAIN
    layer = max(0.10, min(1.0, layer))

    duration = float(p0.get("multi_duration") or 0.0)
    if duration <= 0:
        try:
            duration = float(wav_seconds(tracks[0]) or 0.0)
        except Exception:
            duration = 0.0
    if duration <= 0:
        raise Fail("读不出上一版时长，没法重排")

    log(job, "换分配方式重拼：%s → %s（不重跑推理）"
        % (MULTI_MODES.get(p0.get("multi_mode"), p0.get("multi_mode")), MULTI_MODES[mode]))
    job["stage"] = "重排分配表"
    begin_step(job, "重排分配表",
               "拿这 %d 个人的**已有**角色轨，按新的分配方式重新排一遍。"
               "推理一次都不跑 —— 换的是「谁唱哪几句」，不是音色。" % n)
    # ★ 同一份 plan_segs：重排出来的表必须和界面上那一列是同一批句。
    segs, _seg_src3, _lrc3 = plan_segs(
        song_path or "", dur=duration, log=lambda m: log(job, m))
    cached_dur = duration
    if STAGES not in sys.path:
        sys.path.insert(0, STAGES)
    import multising as _ms
    _gi3 = {}
    try:
        takes, _lr, degraded = _ms.plan_takes(
            segs or [], max(duration, cached_dur or 0),
            n, mode=mode, manual=manual, info=_gi3)
    except ValueError as e:
        log(job, "  [warn] %s" % e)
        end_step(job, False, "逐句指定对不上")
        raise Fail(str(e))
    if not takes:
        end_step(job, False, "排不出分配表")
        raise Fail("排不出分配表：这首歌没有可用的分句时间戳")
    if _gi3.get("split"):
        log(job, "  ⚠ 只分出 %d 个乐句组（少于 %d 人）⇒ 已按句拆成 %d 组，"
                 "保证每个人都唱得上" % (_gi3.get("groups_before"), n,
                                        _gi3.get("groups")))
    elif _gi3.get("short"):
        log(job, "  ⚠ 句数少于人数（%d 句 / %d 人），拆不动 —— 会有人唱不到"
            % (len(segs or []), n))
    if degraded:
        log(job, "  ⚠ 这首歌没有分句时间戳 ⇒ 按时间均分；切点会落在句子中间")
    _st2 = _ms.takes_stats(takes, n)
    for st in _st2:
        log(job, "    %s：%.1f 秒 / %d 段"
            % (names[st["role_index"]] if st["role_index"] < len(names)
               else "#%d" % st["role_index"], st["sec"], st["parts"]))
    _z2 = [names[st["role_index"]] for st in _st2
           if st["sec"] < 0.5 and st["role_index"] < len(names)]
    if _z2:
        log(job, "  ⚠ 「%s」一句都没分到 —— 换「逐句轮流」或用手动指定才会出声"
            % "」「".join(_z2))
    tag = "multi_%s" % time.strftime("%m%d_%H%M%S")
    plan_path = os.path.join(final, "plan_%s.json" % tag)
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump({"duration": duration, "takes": takes, "mode": mode,
                   "lines": len(segs or []), "resplit_from": p0.get("multi_tag"),
                   "roles": [{"name": x} for x in names]},
                  f, ensure_ascii=False, indent=1)
    end_step(job, True, "%d 段（%s）" % (len(takes), MULTI_MODES[mode]))

    job["stage"] = "重拼"
    begin_step(job, "按新分配表重拼", "交叉淡化 + 按各自分到的段拉平响度。")
    voice_wav = os.path.join(final, "voice_%s.wav" % tag)
    rep_path = os.path.join(final, "multi_%s.json" % tag)
    q = lambda s: '"%s"' % s
    # 同上：重拼也要带上逐句合唱的和声音量（不然重拼一次合唱就变回默认值）
    cmd = "%s -s %s --plan %s --out %s --report %s --cgain %.2f %s" % (
        q(PY), q(os.path.join(STAGES, "multising.py")), q(plan_path),
        q(voice_wav), q(rep_path), layer,
        " ".join("--track %s" % q(t) for t in tracks))
    rc, out = run(job, cmd, timeout=1800)
    if rc != 0 or not os.path.exists(voice_wav):
        end_step(job, False, "重拼失败")
        raise Fail("重拼失败（退出码 %d）。\n%s" % (rc, _tail(out, 5)))
    info = {}
    with contextlib.suppress(Exception):
        with open(rep_path, encoding="utf-8") as f:
            info = json.load(f)
    end_step(job, True, "%d 段拼成一条（%d 次接缝）"
             % (info.get("takes", len(takes)), info.get("seams", 0)))

    job["stage"] = "重混"
    begin_step(job, "重混伴奏", "伴奏轨 + 新拼好的人声。")
    mix_wav, tail = _multi_mix(job, inst_wav, voice_wav,
                               os.path.join(final, "mix_%s.wav" % tag))
    if mix_wav:
        end_step(job, True, "成品轨已生成")
    else:
        log(job, "  [warn] 混音没做成（%s），人声干轨仍可用" % tail)
        end_step(job, True, "混音没做成，人声干轨仍可用（已说明）")

    outs = [{"label": "人声（干声）", "wav": voice_wav}]
    if mix_wav:
        outs.insert(0, {"label": "成品（人声+伴奏）", "wav": mix_wav})
    # ★ 重排只是换"谁唱哪句"，歌词还是这一首歌的 ⇒ 字幕照带（跟 sing_multi 一致）
    _lyr = "\n".join(str(s.get("text") or "").strip()
                     for s in (segs or [])
                     if str(s.get("text") or "").strip())
    params = dict(p0)
    params.update({
        "multi_mode": mode, "multi_tag": tag,
        "multi_takes": [[round(float(t[0]), 3), round(float(t[1]), 3), int(t[2])]
                        for t in takes],
        "multi_lines": len(segs or []),
        "multi_segs": _segs_brief(segs),
        "multi_duration": round(duration, 2),
        "multi_resplit_from": p0.get("multi_tag"),
        "seams": info.get("seams", 0),
        "gains": info.get("gains") or [],
        "chorus_gain": None,
        "peak_guard": None,
    })
    if save:
        def _add(r):
            for t in outs:
                r.setdefault("outputs", []).append({
                    "text": "多人合唱（重排）：%s" % " / ".join(names),
                    "wav": t["wav"], "at": _now(), "kind": "singing",
                    "track": t["label"],
                    "seconds": round(wav_seconds(t["wav"]), 2),
                    "lyric": _lyr,
                    "lyric_srt": _write_srt(t["wav"], _lyr, segs, song_path),
                    "stems": dict(stems, mix=mix_wav or voice_wav, voice=voice_wav),
                    "params": dict(params),
                })
        mutate_role(rid, _add)
        log(job, "  新版本已登记（旧版本仍留在历史里）")
    return outs


def multi_solo_mix(rid, index, role_index, job=None):
    """给一条多角色成品的**某一个**角色，混出「他一个人把整首唱完」的带伴奏版。

    为什么要单独做成一次调用而不是跑成品时就全混好：见 sing_multi 里
    solo_tracks 的说明 —— 每个版本都是一整首歌的大小，8 个人就是几百 MB，
    而用户往往只想听其中一两个人。混音本身只要几秒钟。

    `index`      这条成品在 `role["outputs"]` 里的下标
    `role_index` 第几个人（0 起）

    返回 {"ok": True, "mix": 路径, "name": 角色名, "cached": 是不是已经有了}
    """
    r = get_role(rid)
    if not r:
        raise Fail("角色不存在：%s" % rid)
    outs = r.get("outputs") or []
    if not (0 <= index < len(outs)):
        raise Fail("这条成品已经不在历史里了（下标 %d / 共 %d 条）。刷新一下页面。"
                   % (index, len(outs)))
    o = outs[index]
    params = o.get("params") or {}
    solos = params.get("solo_tracks") or []
    if not solos:
        raise Fail("这条成品没有记下各角色的独唱轨 —— 它多半是**旧版本**跑出来的"
                   "（这个功能上线之前）。重新唱一次就有独唱了。")
    if not (0 <= role_index < len(solos)):
        raise Fail("没有第 %d 个演唱者（这条成品一共 %d 人）"
                   % (role_index + 1, len(solos)))
    s = solos[role_index]
    old = s.get("mix")
    if old and os.path.isfile(old):
        return {"ok": True, "mix": old, "name": s.get("name"), "cached": True}

    dry, inst = s.get("dry"), (o.get("stems") or {}).get("inst")
    # ★ 干声没了要说清楚**（这里最容易误判成"功能坏了"）：独唱轨放在主角色的
    #   final 目录里，清理缓存的时候可能被带走 —— 那时重试也没用，只能重唱。
    if not dry or not os.path.isfile(dry):
        raise Fail("「%s」的独唱轨文件不见了：%s\n"
                   "它存在主角色目录的 final 里，多半是被清理缓存带走了。"
                   "重新跑一次多人合唱就能再拿到。" % (s.get("name"), dry))
    if not inst or not os.path.isfile(inst):
        raise Fail("这条成品的伴奏轨不见了：%s\n"
                   "独唱也要跟伴奏合在一起才好听 —— 请重新跑一次这条成品。"
                   % inst)

    dst = os.path.splitext(dry)[0] + "_withbg.wav"
    mx, tail = _multi_mix(job, inst, dry, dst)
    if not mx:
        raise Fail("「%s」的独唱版混音失败：%s" % (s.get("name"), tail))

    def _patch(rr):
        try:
            (rr["outputs"][index]["params"]["solo_tracks"][role_index]["mix"]) = mx
        except (KeyError, IndexError, TypeError):
            return
    mutate_role(rid, _patch)
    if job:                      # 界面上现混时没有 job，那就没什么可记的
        log(job, "  已混好「%s」的独唱版：%s" % (s.get("name"), os.path.basename(mx)))
    return {"ok": True, "mix": mx, "name": s.get("name"), "cached": False}


# ------------------------------------------------ 凭空写歌（YuE2 作曲 + 我们的音色）
def make_original_song(job, rid, style, lyrics, duration=120, pitch=0,
                       index_rate=None, sep_model=None, rms_mix_rate=None,
                       protect=None, steps=32, prefix="new", **kw):
    """写一首**世界上还不存在的歌**，用角色的音色唱出来。

    和 sing_role 的关系：这不是另一条管线，而是**给它换一个源头**。

        翻唱：  现成的歌 ─────────────┐
        写歌：  风格+歌词 → YuE2 出曲 ─┴→ 分离 → 换声 → 混音 → 质检 → 登记成品

    ⇒ 第 0 步（分离）之后的全部逻辑一行都不用重写，因为 sing_role 本来就
      只认「一首歌的文件路径」，它不关心那首歌是怎么来的。

    为什么要 YuE2 而不是直接用角色说话：RVC 只能"换音色"，不能"从无到有
    编曲演唱"。YuE2 会作曲、配器、唱，但**克隆不了具体音色**（只能靠 LoRA
    微调）；我们正好相反 —— 有 26 个角色模型和声纹中心，但唱不了新歌。
    两边各补对方的短板，这就是"取长补短"的落点。

    返回 (tracks, meta)：tracks 与 sing_role 的一致，meta 是 YuE2 的元信息
    （含它写的 ABC 乐谱，以后想改旋律用得上）。
    """
    from stages import yue2 as _y2

    role = get_role(rid)
    if not role:
        raise Fail("没有这个角色：%s" % rid)

    log(job, "写歌：风格「%s」" % (style or "")[:70])
    log(job, "      歌词 %d 行，目标 %.0f 秒" % (len([l for l in (lyrics or "").splitlines()
                                                    if l.strip()]), duration))

    job["stage"] = "作曲演唱"
    begin_step(job, "作曲演唱（YuE2）",
               "用 YuE2 按「风格 + 歌词」凭空写一首歌并唱出来（48 kHz）。"
               "这一步产出的是**占位人声**：旋律、编曲、节奏都是它的，"
               "音色还不是角色的 —— 下一步换声才把音色换成角色。")

    meta = {}
    try:
        audio, meta = _y2.synth(job, style, lyrics, duration=duration, steps=steps,
                                say=lambda m: log(job, m))
    except _y2.NotReady as e:
        end_step(job, False, str(e))
        raise Fail("YuE2 没就绪：%s\n"
                   "（它是可选组件 —— 装好后会自动拉起，见 stages/yue2.py 的说明）" % e)
    except _y2.Yue2Fail as e:
        end_step(job, False, str(e))
        raise Fail("YuE2 作曲失败：%s" % e)

    secs = 0.0
    try:
        secs = wav_seconds(audio)
    except Exception:
        pass
    end_step(job, True, "曲子做好了：%.1f 秒（%s）"
             % (secs, os.path.basename(audio)))

    # ---- 从这里开始完全交给 sing_role（分离 / 换声 / 混音 / 质检 / 登记）----
    tracks = sing_role(job, rid, audio, pitch=pitch, index_rate=index_rate,
                       sep_model=sep_model, rms_mix_rate=rms_mix_rate,
                       protect=protect, prefix=prefix, **kw)

    # 把"这歌是谁写的"记进成品：过几天回头看，能分清哪些是翻唱、哪些是原创。
    # ★ 用 sing_role 返回的 tracks 里的**真实文件路径**做匹配，不要靠文件名前缀猜：
    #   实测成品登记的名字是 `voiceg_<tag>.wav`（gate 处理后的那份，多一个 g），
    #   而 sing_role 里生成的是 `voice_<tag>.wav` —— 按 `voice_<prefix>` 去前缀匹配
    #   会一条都命中不了，症状是"跑完了但 origin 一个都没写"。tracks 是权威来源。
    want = set(os.path.basename(t.get("wav") or "") for t in (tracks or [])
               if t.get("wav"))
    marked = [0]

    def _mark(role_doc):
        for o in role_doc.get("outputs") or []:
            if os.path.basename(o.get("wav") or "") in want:
                o["origin"] = {
                    "kind": "yue2-new-song",
                    "style": style,
                    "lyrics": lyrics,
                    "duration": round(duration, 1),
                    # ★ 两个 id 都要记，缺一个就断链：
                    #   job     = 本项目的任务 id（能回溯到 data/jobs/<id>/ 里的质检报告）
                    #   song_id = YuE2 引擎的曲子 id（对应 yue2_<id>.wav）
                    # 第一版只记了引擎的曲子 id，还叫 job_id —— 于是拿它去 data/jobs/
                    # 下找质检报告一条都找不到（统计里"新歌链路 0 样本"就是这么来的）。
                    "job": (job or {}).get("id"),
                    "song_id": meta.get("_job_id"),
                    "elapsed": meta.get("_elapsed"),
                    "abc": meta.get("abc") or "",
                }
                marked[0] += 1
        return marked[0]
    try:
        mutate_role(rid, _mark)
    except Exception as e:
        # 不吞：标不上来源不是要命的事，但必须留痕，否则以后看到成品没有 origin
        # 会以为是"没走这条路"，实际是写失败了。
        log(job, "  [warn] 来源标记没写进成品：%s: %s" % (type(e).__name__, e))
    if not marked[0]:
        log(job, "  [warn] 有成品生成，但没匹配到可标记的登记项（tracks=%d）"
            % len(tracks or []))
    return tracks, meta


# ------------------------------------------------ 成品带歌词（唱的是哪版词，得跟着成品走）
def _lyric_text(lyric):
    """从 (原词, 新词) 里挑出**实际唱的那份**：改过词就是新词。

    为什么要存进成品：过几天回头听这条，光有音频根本不知道当时唱的是原词还是
    改过的那版 —— 想再改一版、或者想跟着唱，都对不上。
    """
    if not lyric:
        return ""
    if isinstance(lyric, (tuple, list)):
        orig, new = (list(lyric) + ["", ""])[:2]
        return (new or orig or "").strip()
    return str(lyric).strip()


def _cached_lyric(song_path, lang="zh"):
    """这首歌**之前**识别过歌词吗？只读缓存，绝不触发识别。

    ★ 为什么不在唱歌流程里自动跑 ASR：那玩意儿第一次要下一 1.5GB 的语音识别模型。
      用户只是想唱首歌，不该被拖着等一个 G 多的下载 —— 而且他根本没要求识别歌词。
      所以：识别过（有缓存）就顺手带上；没识别过就在成品上留个「识别歌词」按钮，
      他自己点，点了才跑，进度也能看见。
    """
    try:
        p = os.path.join(LYRIC_CACHE,
                         _sep_cache_key(song_path, "lyrics|%s" % lang) + ".json")
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            return (d.get("line") or "").strip(), (d.get("segments") or [])
    except Exception:
        pass
    return "", []


def job_output_lyric(job, rid, index, lang="zh"):
    """给**某一条已有成品**补歌词：识别 → 写回这条成品 → 旁边生成 .lrc。

    为什么要单独开一个任务（而不是唱歌时顺手做）：见 `_cached_lyric` 的说明 ——
    识别要下模型，那是用户该自己决定要不要等的事。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    outs = role.get("outputs") or []
    if index < 0 or index >= len(outs):
        raise Fail("没有第 %d 条成品" % (index + 1))
    o = outs[index]
    src = ((o.get("stems") or {}).get("src")) or None
    if not src or not os.path.isfile(src):
        raise Fail("这条成品记的原曲不在了（%s），没法识别" % (src or "没记"))
    d = recognize_lyrics(job, src, lang=lang)
    line = (d.get("line") or "").strip()
    if not line:
        raise Fail("没识别出歌词（可能是纯音乐，或者识别语言不对）")
    segs = d.get("segments") or []

    def _set(r):
        r["outputs"][index]["lyric"] = line
        r["outputs"][index]["lyric_lrc"] = _write_lrc(r["outputs"][index]["wav"], line, segs)
        r["outputs"][index]["lyric_srt"] = _write_srt(r["outputs"][index]["wav"],
                                                      line, segs, src)

    mutate_role(rid, _set)
    job["stage"] = "完成"
    log(job, "  歌词已写进第 %d 条成品（%d 句）" % (index + 1, len(segs)))
    return {"index": index, "lines": len(segs), "lyric": line}


# ------------------------------------------------ 可视化视频（成品 → 能直接发的 MP4）
#
# 为什么要有这一块：老板做完翻唱，成品是个 wav —— 想发出去还得自己找软件配画面。
# 这里把「音频 + 歌词 + 标题」直接渲染成 MP4，一步到位。
#
# 全部用项目里**已有的那个 ffmpeg**（GPT-SoVITS runtime 里那个），不装任何新依赖：
# 滤镜（showcqt / showfreqs / showwaves / avectorscope）、字幕（libass）、
# 硬件编码（qsv / mf）它都有。这个 ffmpeg 的三个坑写在 `viz.py` 顶部的注释里。

VIZ_DIR = os.path.join(DATA, "videos")


VIZ_MODES = [
    {
        "key": "viz", "name": "生成可视化画面", "needVideo": False,
        "desc": "画面完全由程序生成。输入只有声音 —— 所以它压根没有「本来的画面」"
                "可以遮挡，适合把一整首歌做成一支能直接发的 MV。",
    },
    {
        "key": "mux", "name": "用我的视频当画面 · 只换音轨", "needVideo": True,
        "desc": "画面一个像素都不动，只把声音换成这条成品。几秒钟出片，"
                "**绝不会遮挡**原来的画面。想要「配上我唱的歌、画面还是我的画面」就用这个。",
    },
    {
        "key": "blend", "name": "我的视频 + 可视化叠加", "needVideo": True,
        "desc": "画面内容全都还在，可视化的光按「屏幕混合」叠上去 —— "
                "黑的地方不动、亮的地方更亮。想要一点氛围、又不想挡住画面时用这个。",
    },
]


def viz_options():
    """给前端：有哪些样式 / 画幅，以及**编码器在这台机器上到底能不能用**。

    编码器要真探（真编一帧），不能只看 `-encoders` 列表 —— 本机
    `h264_nvenc` 列得出来但真跑必炸，照列表选就是"每个用户都出不了片"。
    """
    enc, tested = viz.probe_encoder(FFMPEG, os.path.join(DATA, "cache"))
    # ★ AE 那条路**不需要** ffmpeg 的 H.264 编码器（AE 自己出片），
    #   所以"没有编码器"不再等于"做不了视频" —— 有 AE 就还能做这一组。
    ae_ok = bool(_ae_path())
    return {
        "ok": bool(enc) or ae_ok,
        "encoder": enc,
        "encoderName": {"h264_qsv": "Intel 核显加速", "h264_mf": "系统编码（MediaFoundation）",
                        "h264_nvenc": "NVIDIA 显卡加速", "h264_amf": "AMD 显卡加速"}.get(enc, enc or ""),
        "tested": tested,
        "reason": "" if (enc or ae_ok) else
                  ("这台机器上既没有能用的 H.264 编码器、也没装 After Effects，做不了视频。"
                   if not ae_ok else "这台机器上没有能用的 H.264 编码器。"
                                     "「AE 真渲染」那一组还能用，另外两组做不了。"),
        "ae": {"ok": ae_ok, "exe": _ae_path(),
               "note": "" if ae_ok else (_AE_NOTE or "没找到 After Effects")},
        "styles": viz_all_styles(),
        "modes": VIZ_MODES,
        "sizes": viz.SIZES,
        "fps": viz.FPS_CHOICES,
        "dir": VIZ_DIR,
    }


def viz_all_styles():
    """三套引擎的样式合到一起给前端。

    · `py`（手绘逐帧，vizfx）—— 好看，但 3 分钟的歌要渲 3~5 分钟；
    · `ffmpeg`（内置滤镜，viz）—— 几秒出片，画面朴素一点（但有渐变背景 + 暗角撑着）；
    · `ae`（真 After Effects 渲染，aefx）—— 用 AE 自己的合成/效果渲，
      频谱是 AE 的 Audio Spectrum（跟着真音频跳），几十秒出一条，
      但**必须本机装着并且开着 AE**：脚本是投递给正在跑的那个 AE 进程的。

    每组给一句实话告诉用户"要等多久 / 有什么前提"，别让他在那儿猜是不是卡死了。
    """
    out = []
    if vizfx is not None:
        for s in vizfx.STYLES:
            out.append({"key": s["key"], "name": s["name"], "tag": s.get("tag", ""),
                        "desc": s["desc"], "engine": "py", "group": "draw",
                        # ★ 前端靠这俩标记判断"这是合唱族哪一款"，不许再写 `key == "cast"` ——
                        #   那样每加一款新构型，界面上就多一处静默失效（面板不弹/字幕不刷）。
                        "needs_cast": bool(s.get("needs_cast")),
                        "cast_lay": s.get("cast_lay") or "",
                        "note": "逐帧手绘，画面最精致；一首歌要等几分钟。"})
    for s in viz.STYLES:
        out.append({"key": s["key"], "name": s["name"], "tag": s.get("tag", ""),
                    "desc": s["desc"], "engine": "ffmpeg", "group": "fast",
                    "needs_cast": bool(s.get("needs_cast")),
                    "cast_lay": s.get("cast_lay") or "",
                    "note": "用 ffmpeg 内置滤镜，几秒钟就出片。"})
    # ★ 没装 AE 就**整个这一组都不下发**：下发一堆点了必然报"找不到 AE"的卡
    #   比不给更坑（用户会以为是自己的操作错了）。
    if _ae_path():
        for s in aefx.STYLES:
            out.append({"key": s["key"], "name": s["name"], "tag": s.get("tag", ""),
                        "desc": s["desc"], "engine": "ae", "group": "ae",
                        "needs_cast": bool(s.get("needs_cast")),
                        "cast_lay": s.get("cast_lay") or "",
                        "note": "真 After Effects 渲染：几十秒出一条，"
                                "但 AE 得装着并且开着（脚本投给它跑）。"})
    return out


def viz_style_any(key):
    """按 key 找样式，三个引擎都找。返回 (样式, 引擎)。"""
    if vizfx is not None:
        s = vizfx.style_by_key(key)
        if s:
            return s, "py"
    if aefx is not None:
        s = aefx.style_by_key(key)
        if s:
            return s, "ae"
    s = viz.style_by_key(key)
    if s:
        return s, "ffmpeg"
    return None, None


def _need_vizfx():
    """手绘引擎不可用时，给一句人能看懂的话（而不是 AttributeError）。"""
    if vizfx is None:
        raise Fail("这台机器上的 Python 没有 numpy/PIL，手绘那套样式做不了"
                   "（换「快速」那组的样式，或者用运行 voice studio 的那个解释器）。\n%s"
                   % _VIZFX_ERR)
    return vizfx


def _need_ae():
    """AE 引擎不可用时的报错。分三种情况，用户才知道该干什么。"""
    if aefx is None:
        raise Fail("AE 渲染模块没加载起来：%s" % (_AEFX_ERR or "未知原因"))
    if not _ae_path():
        raise Fail("这台机器上没找到 After Effects（%s）。\n"
                   "这一组样式要用 AE 真渲染，换别的组，或者先装 AE。"
                   % (_AE_NOTE or "没找到可执行文件"))
    return aefx


def _job_viz_ae(job, audio, style, size, fps, start, duration, out_name,
                title, subtitle, lrc_path, cast, total):
    """「AE 真渲染」这一组样式的出片路径。

    和另外两组最大的区别：**画面是 AE 自己画的**（合成 + AE 自带效果），
    所以
      · 不需要 ffmpeg 的 H.264 编码器（AE 走自己的输出模块出片）；
      · 歌词是 AE 里的文本层，**不烧 .ass**（烧了就是两行字叠一起）；
      · AE 必须装着并且开着 —— 脚本是投递给正在跑的那个 AE 进程的，
        渲完由脚本自己把建的东西删干净（不动用户自己的素材）。
    """
    _need_ae()
    begin_step(job, "检查 After Effects", "装没装、开着没有")
    end_step(job, True, os.path.basename(_ae_path()))

    # ---- 歌词：成品里那份（带中文）优先，没有再用 .lrc
    src = []
    for ln in (((cast or {}).get("lines")) or []):
        try:
            src.append((float(ln.get("start") or 0), float(ln.get("end") or 0),
                        (ln.get("text") or "").strip(), (ln.get("zh") or "").strip()))
        except Exception:                                        # noqa: BLE001
            continue
    if not src and lrc_path and os.path.isfile(lrc_path):
        _pl = viz.parse_lrc(lrc_path) or []
        for i, (_t0, _txt) in enumerate(_pl):
            _t1 = _pl[i + 1][0] if i + 1 < len(_pl) else _t0 + 4.0
            src.append((_t0, _t1, _txt, ""))
    ls = [[a, b, txt, zh] for a, b, txt, zh in src]

    # ---- 谁唱哪一段 + 封面
    cs = []
    cover = ""
    if style.get("needs_cast"):
        members = (style.get("cast") or {}).get("members") or []
        for a, b, k in ((style.get("cast") or {}).get("takes") or []):
            try:
                k = int(k)
            except Exception:                                    # noqa: BLE001
                continue
            m = members[k] if 0 <= k < len(members) else {}
            cs.append({"start": float(a), "end": float(b),
                       "name": (m.get("name") or "?").strip(),
                       "color": m.get("color") or "#8899ff",
                       "avatar": m.get("avatar") or ""})
        for m in members:
            av = (m.get("avatar") or "").strip()
            if av and os.path.isfile(av):
                cover = av
                break

    dst = out_name or _viz_out_path(title or os.path.basename(audio), style["key"])
    begin_step(job, "AE 渲染",
               "%s · %dx%d · %dfps" % (style["name"], size["w"], size["h"], fps))
    ok, msg = aefx.render(
        audio, dst, style, size, fps,
        lines=ls, cast=cs, title=title or "", subtitle=subtitle or "",
        cover=cover, start=start, duration=duration, duration_total=total,
        log=lambda m: log(job, m),
        timeout=max(900, int((duration or total or 0) * 10) + 900))
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    if not ok:
        job["error"] = msg
        raise Fail(msg)
    # ★ 产物照样量一遍：AE 那边"渲染完成"也可能吐个空壳
    _out = viz.media_info(FFPROBE, dst)
    if not _out or not _out.get("video") or (_out.get("dur") or 0) < 0.3:
        try:
            os.remove(dst)
        except OSError:
            pass
        raise Fail("AE 渲出来的东西不对（%.1f KB / 时长 %.2f 秒），已删掉。"
                   % (os.path.getsize(dst) / 1024.0 if os.path.exists(dst) else 0,
                      (_out or {}).get("dur") or 0.0))
    if not _out.get("audio"):
        log(job, "  [warn] 成品里没有音轨 —— 声音可能没接回去，听一下确认。")
    end_step(job, True, msg)

    job["stage"] = "完成"
    log(job, "  视频已生成：%s" % os.path.basename(dst))
    return {"path": dst, "name": os.path.basename(dst),
            "mb": round(os.path.getsize(dst) / 1048576.0, 1),
            "style": style["key"], "style_name": style["name"],
            "size": "%dx%d" % (size["w"], size["h"]), "fps": fps,
            "lyric_lines": len(ls), "encoder": "AE",
            "cast_opt": style.get("cast_opt") or None,
            "bg": {"kind": "grad"}}


def _safe_name(s, fallback="video"):
    """把标题变成能当文件名的东西（Windows 不允许 \\ / : * ? " < > |）。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", str(s or "")).strip(" .")
    s = re.sub(r"\s+", " ", s)
    return (s[:80] or fallback)


def _viz_out_path(name, style_key):
    """产物路径。同名就加序号，**不覆盖**用户已经做好的片。"""
    os.makedirs(VIZ_DIR, exist_ok=True)
    base = "%s_%s" % (_safe_name(name), style_key)
    p = os.path.join(VIZ_DIR, base + ".mp4")
    i = 2
    while os.path.exists(p):
        p = os.path.join(VIZ_DIR, "%s_%d.mp4" % (base, i))
        i += 1
    return p


def viz_cast_of(rid, index):
    """从一条多角色成品里取出「谁在第几秒唱」+ 歌词 —— 给「合唱台」那款视频用。

    ★ 为什么不让画法自己去猜"这句是谁"：这条成品生成时就已经把分配表算好并
      存进 `multi_takes` 了。重新从音频猜一遍，既慢又可能猜得和实际听到的不一样
      —— 那种"画面上亮的是 A、唱的是 B"的片子比不做还糟。
    取不到就返回 None，让调用方明说"这条没有分配表"，而不是画一排永远不亮的头像。
    """
    r = get_role(rid)
    if not r:
        return None
    outs = r.get("outputs") or []
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return None
    if not (0 <= idx < len(outs)):
        return None
    o = outs[idx]
    p = o.get("params") or {}
    # ★「谁在唱」以**音频**为准（老板报的"亮的人和唱的人对不上"就是记录
    #   与成品脱节；波形不会脱节）。算不出来才退回拼接时记的那张表。
    _rec = p.get("multi_takes") or []
    who = multi_who_of(rid, idx)
    takes, who_src = (who, "wave") if who else (_rec, "record" if _rec else "none")
    who_fix = round(_who_disagree(who, _rec)[0], 3) if who else 0.0
    roles = p.get("multi_roles") or []
    if not takes or not roles:
        return None

    members = []
    for x in roles:
        rr = get_role(x.get("rid")) or {}
        members.append({"name": x.get("name") or rr.get("name") or "",
                        "avatar": rr.get("avatar") or "",
                        "color": rr.get("color") or ""})

    # 歌词优先级（从最权威往下）：
    #   ① 成品自己的 .lrc（校对过的最终稿）
    #   ② **这首歌校对过的双语缓存** —— 就是「粘贴歌词+中文」导入的那份；
    #      它同样是用户亲手给的，而且自带中文，所以排在成品自带分句之前
    #   ③ 成品自带的分句 `multi_segs`（生成那一刻认的，和 multi_takes 同批）
    #   ④ 这首歌的识别缓存（可能不是当初那份，也可能已被清掉）
    # ★ ②为什么能压过③：③ 是机器认的、没有中文；用户导入过就等于"我改过词"，
    #   和 .lrc 一个性质。不改这一条的话，导入完在字幕表里根本看不见 ——
    #   那这个功能等于没做。
    src = (o.get("stems") or {}).get("src")
    raw = []          # [(秒, 原文, 中文)]
    lrc = o.get("lyric_lrc")
    if lrc and os.path.isfile(lrc):
        raw = [(float(a), str(b or "").strip(), "") for a, b in viz.parse_lrc(lrc)]
    if not raw:
        bi = lyric_segments_bilingual(src) if src else None
        if bi:
            raw = [(float(s.get("start") or 0), (s.get("text") or "").strip(),
                    (s.get("zh") or "").strip())
                   for s in bi if (s.get("text") or "").strip()]
    if not raw:
        for r2 in (p.get("multi_segs") or []):
            if not isinstance(r2, (list, tuple)) or len(r2) < 2:
                continue
            txt = str(r2[1] or "").strip()
            if txt:
                # 第三格是 2026-10-21 起才有的中文，老数据只有两格
                raw.append((float(r2[0] or 0), txt,
                            str(r2[2] or "").strip() if len(r2) > 2 else ""))
    if not raw:
        segs, _dur = lyric_segments_any(src) if src else (None, 0.0)
        raw = [(float(s.get("start") or 0), (s.get("text") or "").strip(),
                (s.get("zh") or "").strip())
               for s in (segs or []) if (s.get("text") or "").strip()]
    lines = []
    for i, (ts, txt, zh) in enumerate(raw):
        nxt = raw[i + 1][0] if i + 1 < len(raw) else float(ts) + 4.0
        lines.append({"start": round(float(ts), 3),
                      "end": round(max(float(nxt), float(ts) + 0.4), 3),
                      "text": txt,
                      "zh": zh or ""})
    # ★ 用户改过的字幕（文字 / 中文）盖上去。时间轴和分配表**一个字不动**，
    #   所以"谁在唱"永远是成品自己记下的那份，改字幕改不乱它。
    lines = vizsubs.merge(lines, p.get("viz_lines"))
    # 歌名（视频顶上那行标题用）。成品自己的 text 是系统起的名字
    # （"多人合唱：云宝 / 碧琪 / …"），写在视频上等于没信息 —— 歌名才有。
    _src = (o.get("stems") or {}).get("src") or ""
    song = os.path.splitext(os.path.basename(_src))[0] if _src else ""
    return {"members": members,
            "takes": [[float(a), float(b), int(k)] for a, b, k in takes],
            "lines": lines,
            "song": song,
            # 原曲路径：界面「粘贴歌词」要用它当歌词缓存的键（歌 ↔ 词是一一对应的）
            "src": _src,
            "title": o.get("text") or "",
            # ★「谁在唱」这份数据是哪来的 —— 界面要能原话告诉用户：
            #   wave   = 逐窗对着音频认出来的（最可信）
            #   record = 独唱轨不在了，退回拼接时记的那张表（可能和音频脱节）
            "who_src": who_src,
            # 记录与音频对不上的比例（>0 就该让用户知道"你那份记录是错的"）
            "who_fix": who_fix,
            "has_zh": any((ln.get("zh") or "").strip() for ln in lines)}


def viz_lines_save(rid, index, rows):
    """把用户改过的字幕存进这条成品（存 params.viz_lines，按行下标对齐）。

    ★ 为什么要落盘而不是只放在浏览器内存里：他改的是一首歌几十句的词，
      关掉页面就没了 —— 而且下次做视频（换个尺寸、换个样式）还得从头再改一遍。
      存进成品，等于"这条成品有自己的字幕"。
    """
    r = get_role(rid)
    if not r:
        raise Fail("找不到这个角色")
    outs = r.get("outputs") or []
    try:
        idx = int(index)
    except (TypeError, ValueError):
        raise Fail("成品序号不对：%r" % (index,))
    if not (0 <= idx < len(outs)):
        raise Fail("这条成品不存在（第 %s 条，共 %d 条）" % (index, len(outs)))
    clean = []
    for k, row in enumerate(rows or []):
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i", k))
        except (TypeError, ValueError):
            i = k
        clean.append({"i": i,
                      "text": str(row.get("text") or "")[:vizsubs.MAX_LINE],
                      "zh": str(row.get("zh") or "")[:vizsubs.MAX_LINE]})

    def fn(rr):
        o = rr["outputs"][idx]
        p = o.get("params")
        if not isinstance(p, dict):
            p = o["params"] = {}
        p["viz_lines"] = clean

    mutate_role(rid, fn)
    return {"saved": len(clean)}


def translate_viz_lines(rid, index, job=None, only_empty=True):
    """把这条件成的歌词翻成中文，存回去。返回 {zh, how, filled}。

    `only_empty` = 只补还没翻的那些（保住用户手改过的行），默认开。
    """
    cast = viz_cast_of(rid, index)
    if not cast:
        raise Fail("这条成品没有字幕可用 —— 要多角色跑出来的成品才有分配表和歌词")
    lines = cast.get("lines") or []
    if not lines:
        raise Fail("这条成品没有歌词。先在成品卡上点「识别歌词」，再来做视频")
    texts = [(ln.get("text") or "").strip() for ln in lines]
    try:
        zh, how = vizsubs.translate(
            texts, log=(lambda m: log(job, m)) if job else None)
    except ValueError as e:
        # 两级都没通：这是"现在做不了"，不是"服务器炸了" —— 走 Fail（HTTP 400），
        # 前端直接把原话显示出来。
        raise Fail(str(e))
    rows, filled = [], 0
    for i, ln in enumerate(lines):
        old = (ln.get("zh") or "").strip()
        new = "" if (only_empty and old) else (zh[i] if i < len(zh) else "")
        if new and not old:
            filled += 1
        rows.append({"i": i, "text": ln.get("text") or "", "zh": new or old})
    viz_lines_save(rid, index, rows)
    return {"zh": [r["zh"] for r in rows], "how": how, "filled": filled}


def _hex3(s):
    """`#rrggbb` → (r, g, b)。给不出来就 None（调用方回落到默认渐变）。"""
    s = str(s or "").strip()
    if s.startswith("#") and len(s) >= 7:
        try:
            return (int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16))
        except ValueError:
            return None
    return None


def _under(path, roots):
    """路径是否在给定的几个根目录之内（用来挡住"随便传个系统路径"）。"""
    try:
        ap = os.path.abspath(path)
    except (TypeError, ValueError):
        return False
    for r in roots:
        try:
            root = os.path.abspath(r)
        except (TypeError, ValueError):
            continue
        if ap == root or ap.startswith(root + os.sep):
            return True
    return False


def job_viz(job, audio, title="", subtitle="", lrc_path=None, style_key="cqt",
            size_key="landscape", fps=30, start=None, duration=None,
            out_name=None, bg_video=None, mode="viz", cast=None, lines=None,
            cast_opt=None, bg=None):
    """把一段音频做成视频。

    `audio`   成品 wav 或任意音频文件的绝对路径
    `lrc_path` 带时间轴的 .lrc；给了才会烧歌词，没给就只有画面 + 标题
    `start`/`duration` 不给就是整首

    ★ 三种做法，取决于**画面从哪来**（老板问「加进去是不是遮挡了本来的画面」
      之后加的）：
        `mode="viz"`   （默认）画面 100% 生成 —— 输入本来就只有声音，
                        没有"本来的画面"这回事，也就谈不上遮挡；
        `mode="mux"`   用 `bg_video` 当画面，**只换音轨**，画面一个像素都不动。
                        想要"配上我的歌、画面还是我的画面"就用这个，几秒钟出片；
        `mode="blend"` `bg_video` 当底，可视化的光按**屏幕混合**叠上去 ——
                        画面内容全在，只是让亮的地方更亮，要氛围又不想全挡住时用。
    """
    if not audio or not os.path.isfile(audio):
        raise Fail("找不到要渲染的音频：%s" % (audio or "没给"))
    mode = (mode or "viz").strip().lower()
    if mode not in ("viz", "mux", "blend"):
        raise Fail("不认识这个做法：%s" % mode)
    if mode != "viz" and (not bg_video or not os.path.isfile(bg_video)):
        raise Fail("「用我的视频当画面」要先选一个视频文件（现在给的是：%s）"
                   % (bg_video or "没选"))

    # ★ 起始秒数先对着**真实时长**核一遍。超出长度时 ffmpeg 不报错，
    #   照样吐一个几百字节的空壳 mp4（实测 262 字节）：任务显示"完成"、
    #   视频库里也列出来，用户点开一片漆黑 —— 比直接告诉他"截错了"坑得多。
    _info = viz.media_info(FFPROBE, audio)
    _total = (_info or {}).get("dur") or 0.0
    if _total <= 0.0:
        raise Fail("读不出这段音频的长度（文件可能不是音频，或者已经坏了）：%s"
                   % os.path.basename(audio))
    if start is not None and start >= _total - 0.05:
        raise Fail("从第 %.0f 秒开始，但这首歌一共才 %.1f 秒 —— 截出来会是空的。"
                   "把「起始」改小一点（或干脆不勾「只做一段」做整首）。"
                   % (start, _total))
    if duration is not None and duration <= 0.05:
        raise Fail("要做的时长太短了（%.2f 秒），至少给 1 秒。" % duration)

    style, engine = viz_style_any(style_key)
    if not style:
        raise Fail("不认识这个样式：%s" % style_key)
    # ★ 只要带了"画面选项"（背景 / 合唱台那几项），就必须**先浅拷贝**再往上写。
    #   STYLES 是模块级列表，直接改它等于给这个进程后面所有渲染都下了料：
    #   下一个单角色视频会莫名顶着一张三小时前的背景图。
    # ★ 判据是 `needs_cast` 这个**标记**，不是 `key == "cast"`。
    #   合唱台现在有四套构型（row / duet / ring / stage），写死 key 的话
    #   新加的三套永远拿不到分配表 —— 画面一排空位，任务还显示"成功"。
    #   这就是"装了但从没走到"那一类，报错都不会报。
    if style.get("needs_cast") or cast_opt or bg:
        style = dict(style)
    if cast_opt:
        style["cast_opt"] = dict(cast_opt) if isinstance(cast_opt, dict) else {}
    if bg:
        _bg = dict(bg) if isinstance(bg, dict) else {}
        kind = (_bg.get("kind") or "grad").strip().lower()
        if kind == "color":
            rgb = _hex3(_bg.get("color"))
            if rgb:
                style["bg_color"] = rgb
        elif kind == "image":
            p = (_bg.get("image") or "").strip()
            # 只认自己地盘里的图：随便给个 C:\Windows\... 也去读，等于开了个读文件的口子
            if p and os.path.isfile(p) and _under(p, [UPLOADS, DATA, VIZ_DIR]):
                style["bg_image"] = p
            elif p:
                log(job, "  [warn] 背景图不在允许的目录里，忽略：%s" % p)
    if style.get("needs_cast"):
        if not cast or not (cast.get("members") or []):
            raise Fail(
                "「%s」要用一条**多角色**成品的分配表 —— "
                "这条成品是单角色跑的，没有「谁唱哪句」。\n"
                "换别的样式，或者先跑一次多人合唱再来做视频。" % style.get("name"))
        # 标题取歌名（不是"多人合唱：A / B"那种系统名）。用户显式给了 title 就听他的。
        _t = title if title else (cast.get("song") or cast.get("title") or "")
        style["cast"] = dict(cast, title=_t, subtitle=subtitle or "")
        # ★ 界面上现改的字幕（还没保存的）也要能直接用：盖在成品里存的那份之上。
        #   空列表不算"有"，免得界面没加载完就把已存的字幕清空。
        if lines:
            style["cast"]["lines"] = vizsubs.merge(style["cast"].get("lines") or [], lines)
            style["cast"]["has_zh"] = any(
                (x.get("zh") or "").strip() for x in style["cast"]["lines"])
        # ★ 截片段时字幕和分配表都要跟着**平移**。歌词里存的是整首歌的绝对时间，
        #   画面里的 t 是从 0 开始数的 —— 不平移就一句都命中不了，出来是一整段
        #   空着"· · ·"的画面（而且看起来像"歌词没了"）。这里和 .lrc 那条路
        #   用的是同一个口径（build_ass 的 offset）。
        if start:
            off = float(start)
            style["cast"]["lines"] = [
                dict(ln, start=float(ln.get("start") or 0) - off,
                     end=float(ln.get("end") or 0) - off)
                for ln in (style["cast"].get("lines") or [])]
            style["cast"]["takes"] = [
                [float(a) - off, float(b) - off, int(k)]
                for a, b, k in (style["cast"].get("takes") or [])]
    size = viz.size_by_key(size_key)
    if not size:
        raise Fail("不认识这个画幅：%s" % size_key)
    fps = 60 if int(fps or 30) >= 60 else 30

    # ★ AE 这一组走完全不同的路（不需要 ffmpeg 编码器、歌词不烧 ass），
    #   在这里分出去，别让它挤在另外两组的流程里靠一堆 if 扭。
    if engine == "ae":
        if mode != "viz":
            raise Fail("「%s」是 AE 真渲染，画面 100% 由 AE 生成 —— "
                       "配不上「用我的视频当画面」这两条路（它们是要留住你的画面的）。"
                       "想留住自己的画面就换「快速」那组的样式。" % style["name"])
        return _job_viz_ae(job, audio, style, size, fps, start, duration, out_name,
                           title, subtitle, lrc_path,
                           style.get("cast") or cast, _total)

    begin_step(job, "检查编码器", "这台机器能不能做 H.264")
    enc, tested = viz.probe_encoder(FFMPEG, os.path.join(DATA, "cache"))
    if not enc:
        # 把每个候选"为什么不行"照实报出来 —— 只说"编码器不可用"用户没法排查
        why = "；".join("%s：%s" % (t["enc"], t["why"] or "不可用")
                        for t in tested if not t["ok"])
        raise Fail("这台机器上没有能用的 H.264 编码器，做不了视频。\n%s" % why)
    end_step(job, True, "用 %s" % enc)

    # ---- 字幕
    ass = None
    n_line = 0
    begin_step(job, "准备字幕", "标题 + 歌词时间轴")
    if lrc_path and os.path.isfile(lrc_path):
        lines = viz.parse_lrc(lrc_path)
        if lines:
            ass = os.path.join(job["dir"], "viz.ass")
            # ★ 截片段（start/duration）时时间轴要一起平移：.lrc 里是**原曲的绝对时间**，
            #   不平移的话截出来的片子里歌词会全错位（而且看起来"像识别坏了"）。
            n_line = viz.build_ass(ass, lines, size, title=title, subtitle=subtitle,
                                   offset=(start or 0.0), duration=duration)
    if not ass:
        # 没歌词也要有标题，不能因为没歌词就变成一个没信息的光板
        ass = os.path.join(job["dir"], "viz.ass")
        viz.build_ass(ass, [], size, title=title, subtitle=subtitle,
                      offset=0.0, duration=duration)
    end_step(job, True, ("%d 句歌词" % n_line) if n_line else "只有标题（这首没时间轴）")

    # 视频比要配的声短 ⇒ 循环播放铺满整首。不循环的话歌还在放、画面早就黑了。
    loop = False
    if mode != "viz":
        _vdur = (viz.media_info(FFPROBE, bg_video) or {}).get("dur") or 0.0
        _want = duration if duration else max(0.0, _total - (start or 0.0))
        loop = bool(_vdur > 0.5 and _vdur < _want - 0.5)
        if loop:
            log(job, "  你的视频只有 %.1f 秒，比这段声音短 —— 会自动循环播放铺满。"
                % _vdur)

    def _render_layer(dst_layer, ass_file):
        """渲一层画面（viz 模式直接渲到产物；blend 模式渲到中间文件）。

        `ass_file` 分开传是因为：blend 模式**这一步不能烧字幕** ——
        先烧再叠的话字幕会被 screen 提亮成一团白，得叠完再烧。
        """
        if engine == "py":
            _need_vizfx()
            # ★「合唱台」这一族（row / duet / ring / stage）自己会画歌词
            #   （它要让歌词跟"谁在唱"走），再烧一遍 ass 就是两行字叠在一起。
            use_ass = None if style.get("needs_cast") else ass_file
            # 手绘逐帧：几分钟。进度每一小段报一次，用户知道在动；
            # `should_stop` 让「取消」能立刻停 —— 不然要等整首画完。
            return vizfx.render_fx(
                FFMPEG, enc, audio, dst_layer, style, size, fps, ass=use_ass,
                duration=duration, start=start, log=lambda m: log(job, m),
                should_stop=lambda: job_cancelled(job))
        # 背景图：铺在滤镜产物底下的渐变。没有它画面就是个死黑的洞。
        # 生成失败不算致命（顶多回到黑底），所以不抛异常。
        bg = None
        if vizfx is not None:
            try:
                bg = vizfx.make_bg_png(os.path.join(job["dir"], "viz_bg.png"),
                                       size["w"], size["h"], vizfx.FFMPEG_BG)
            except Exception as e:                           # noqa: BLE001
                log(job, "  （背景图没生成出来，退回黑底：%s）" % e)
        return viz.render(FFMPEG, enc, audio, dst_layer, style, size, fps,
                          ass=ass_file, duration=duration, start=start, bg=bg,
                          log=lambda m: log(job, m))

    # 产物名里带的是"怎么做"的标记：配音那条路没有样式可言，写样式名会误导
    _tag = {"viz": style_key, "mux": "配音", "blend": style_key + "叠加"}[mode]
    dst = out_name or _viz_out_path(title or os.path.basename(audio), _tag)
    begin_step(job, "渲染视频",
               "%s · %dx%d · %dfps"
               % ({"mux": "只换音轨（画面不动）"}.get(mode, style["name"]),
                  size["w"], size["h"], fps))

    if mode == "mux":
        # 只换音轨：画面一个像素都不动。不渲染、不等，几秒钟出片。
        ok, msg = viz.mux_video(FFMPEG, bg_video, audio, dst, size, fps=fps,
                                ass=ass, encoder=enc, duration=duration,
                                start=start, loop=loop,
                                log=lambda m: log(job, m))
    elif mode == "blend":
        lyr = os.path.join(job["dir"], "viz_layer.mp4")
        ok, msg = _render_layer(lyr, None)
        if ok and not job_cancelled(job):
            ok, msg = viz.blend_video(FFMPEG, lyr, bg_video, audio, dst, size,
                                      fps=fps, ass=ass, encoder=enc,
                                      duration=duration, start=start, loop=loop,
                                      log=lambda m: log(job, m))
    else:
        ok, msg = _render_layer(dst, ass)
    if job_cancelled(job):
        raise Cancelled("任务已被取消")
    if not ok:
        job["error"] = msg
        raise Fail(msg)
    # ★ 产物也要量一遍：ffmpeg 返回 0 不代表出的是视频
    _out = viz.media_info(FFPROBE, dst)
    if not _out or not _out.get("video") or (_out.get("dur") or 0) < 0.3:
        try:
            os.remove(dst)                 # 空壳别留在库里恶心人
        except OSError:
            pass
        raise Fail("渲染出来的是个空壳（%.1f KB / 时长 %.2f 秒），已删掉。"
                   "多半是「起始」秒数截到了音频外面。"
                   % (os.path.getsize(dst) / 1024.0 if os.path.exists(dst) else 0,
                      (_out or {}).get("dur") or 0.0))
    end_step(job, True, msg)

    mb = os.path.getsize(dst) / 1048576.0
    job["stage"] = "完成"
    log(job, "  视频已生成：%s" % os.path.basename(dst))
    return {"path": dst, "name": os.path.basename(dst), "mb": round(mb, 1),
            "style": style["key"], "style_name": style["name"],
            "size": "%dx%d" % (size["w"], size["h"]), "fps": fps,
            "lyric_lines": n_line, "encoder": enc,
            # 这次用的画面选项：界面上「用这次的字幕重出」要照着它复原，
            # 也好让门禁核对"点下去的参数真的进到了渲染里"。
            "cast_opt": style.get("cast_opt") or None,
            "bg": ({"kind": "image", "image": style.get("bg_image")}
                   if style.get("bg_image") else
                   ({"kind": "color", "color": "#%02x%02x%02x" % tuple(style["bg_color"])}
                    if style.get("bg_color") else {"kind": "grad"}))}


def viz_library():
    """已经做好的视频（按时间倒序）。前端「做视频」页列出来给下载/预览。"""
    out = []
    if os.path.isdir(VIZ_DIR):
        for n in os.listdir(VIZ_DIR):
            if not n.lower().endswith(".mp4"):
                continue
            p = os.path.join(VIZ_DIR, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out.append({"name": n, "path": p, "mb": round(st.st_size / 1048576.0, 1),
                        "at": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))})
    out.sort(key=lambda x: x["at"], reverse=True)
    return out


def _lrc_ts(sec):
    m, s = divmod(max(0.0, float(sec)), 60.0)
    return "%02d:%05.2f" % (int(m), s)


def _write_lrc(wav, text, segs=None):
    """成品旁边写一份 .lrc。时间轴对得上就带，对不上就只写文本（照样能看能复制）。

    ★ 两条不做的：
      ① **不按行数均分时间** —— 均分出来的时间轴看着像真的，其实一句都对不上，
         播放器照着错的时间滚，用户会以为识别坏了，比干脆不给时间轴更坑；
      ② 改过词之后行数变了就**不给时间轴**（识别到的是原词的时间，套不上去）。
    """
    if not text:
        return ""
    p = os.path.splitext(wav)[0] + ".lrc"
    try:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        timed = segs and len(segs) == len(lines)
        with open(p, "w", encoding="utf-8-sig") as f:      # BOM：不然有些播放器中文乱码
            for i, line in enumerate(lines):
                if timed:
                    f.write("[%s]%s\n" % (_lrc_ts(segs[i].get("start", 0)), line))
                else:
                    f.write(line + "\n")
        return p
    except Exception:
        return ""


def _write_srt(wav, text, segs=None, src=None):
    """成品旁边写一份 .srt —— 这首歌**本来的**字幕。

    和 .lrc 是同一份歌词、同一条时间轴，只是换个容器：
    .lrc 给播放器滚歌词用，.srt 给剪辑软件 / 压制视频用。

    ★ 来源优先级（"本来"两个字就落在这里）：
      ① 这首歌**自带**的字幕（源音频旁边有同名 .srt）⇒ 原样复制，一字不改。
         原字幕是发行方校对过的，专有名词和断句都对；ASR 识别出来的必然带错字，
         让它盖掉真货是纯粹的倒退。
      ② 成品里记的 / 缓存里识别过的歌词时间轴 ⇒ 现生成一份。
      ③ 都没有 ⇒ 不写，返回空。
         ★★ 绝不擅自去跑 ASR：那要下一 1.5GB 的模型。自动替用户下，
            等于拿他的带宽和耐心赌 —— 这是他该自己按按钮的事（见 `_cached_lyric`）。

    ★ 编码**不带 BOM**：.lrc 写 utf-8-sig 是为了照顾老播放器，但 SRT 的主流
      实现按「无 BOM 的 UTF-8」解析，带 BOM 时有些工具会把第一行的序号读成乱码。
    """
    stem = ""
    try:
        if src:
            stem = os.path.splitext(os.path.abspath(str(src)))[0]
    except Exception:                                        # noqa: BLE001
        stem = ""

    # ① 自带字幕：原样搬过去，不重新生成、不翻译、不重排时间
    for ext in (".srt", ".SRT"):
        p = stem + ext
        if p and os.path.isfile(p):
            dst = os.path.splitext(wav)[0] + ".srt"
            try:
                shutil.copyfile(p, dst)
                return dst
            except OSError as e:
                print("[srt] 自带字幕复制失败（%s）⇒ 退回用识别歌词生成" % e)
            break

    # ②③ 用歌词时间轴生成；没时间轴就宁可不给（均分出来的假时间轴更坑）
    if not text or not segs:
        return ""
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    try:
        from stages import srt_cli
    except Exception as e:                                   # noqa: BLE001
        print("[srt] 加载字幕模块失败（%s）" % e)
        return ""
    try:
        dur = wav_seconds(wav)
    except Exception:                                        # noqa: BLE001
        dur = None
    rows = srt_cli.from_segments(segs, dur)
    if not rows:
        return ""
    dst = os.path.splitext(wav)[0] + ".srt"
    try:
        with open(dst, "w", encoding="utf-8") as f:
            f.write(srt_cli.dump(rows))
        return dst
    except OSError as e:
        print("[srt] 写字幕失败（%s）" % e)
        return ""


def clip_audio(job, src, dst, start_sec, secs):
    """裁一段音频，统一输出 44.1kHz 立体声 wav。"""
    rc, out = run(job, '"%s" -y -v error -ss %.3f -t %.3f -i "%s" -ac 2 -ar 44100 "%s"'
                  % (FFMPEG, max(0.0, start_sec), secs, src, dst), timeout=600)
    return (rc == 0 and os.path.exists(dst)), out


def _sing_preview(job, rid, song_path, secs, pitch, index_rate, rms_mix_rate,
                  protect, sep_model, lyric=None, nonvocal="keep", engine="rvc",
                  lyric_nfe=None, hf="auto", demetal_mode="auto"):
    """试听小样：整首链路照走，但只对其中一小段跑 RVC。

    为什么值得单独做一条路：正式的 RVC 变声是这首歌里**唯一**的长耗时步骤
    （本机 3分45秒 → 18 分钟）。用户改一个音高就要重等 18 分钟才知道对不对，
    效率极低。这里用「分离照做（有缓存）+ 只转换一段（几十秒）」把试错成本
    从十几分钟压到一两分钟。小样会以 kind="preview" 记进角色成品清单，但
    **永远只留一条**（下一次试听就把上一条摘掉），不会把正片挤下去。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    # ★ 与 sing_role 同一个口径：engine=svc 的卡本来就只挂 SVC 权重，
    #   没有 RVC 模型是正常的，不能在这里拦下来（否则「SVC 卡不能试听」）。
    if engine != "svc" and not (role.get("rvc") or {}).get("model"):
        raise Fail(
            "这个角色还没有歌声模型，没法试听。\n"
            "歌声模型（RVC）和说话模型（GPT-SoVITS）是两套东西：说话模型只能照着"
            "文本念。先把这个角色的 RVC 歌声模型训出来再试听。")
    if engine == "svc" and not (role.get("svc") or {}).get("model"):
        raise Fail("这个角色没有 SVC 权重，没法用 svc 引擎试听。")

    d = role_dir(rid)
    final = os.path.join(d, "final")
    os.makedirs(final, exist_ok=True)
    # 小样用固定文件名（preview_latest）：每次覆盖上一条，界面上永远只有一份
    # "当前样片"，不会试听几次就堆出一排。
    stamp = "latest"

    job["stage"] = "分离人声"
    begin_step(job, "分离人声与伴奏",
               "试听小样也要走这一步 —— 分离结果会缓存，正式跑全曲时直接复用，"
               "不会白做。")
    vocal, inst = separate_song(job, song_path, model=sep_model)
    end_step(job, True, "人声轨 + 伴奏轨已就绪（已缓存，正式跑直接复用）")

    # ★ 同 `sing_multi`：原曲可能是 mp3，`wav_seconds` 对它恒返回 0。
    #   这条路以前靠 `or wav_seconds(vocal)` 侥幸没错，但根源是同一个，一并走统一入口。
    total = _song_total_secs(song_path, 0.0, vocal)[0]
    secs = int(secs)
    if total:
        secs = max(8, min(secs, int(total)))
    # 从 35% 处起：避开前奏，通常已经进主歌/副歌，更能代表整首。
    start = max(0.0, (total - secs) * 0.35) if total else 0.0
    clip_v = os.path.join(final, "_pv_voice_%s.wav" % stamp)
    clip_i = os.path.join(final, "_pv_inst_%s.wav" % stamp)
    ok1, _ = clip_audio(job, vocal, clip_v, start, secs)
    ok2, _ = clip_audio(job, inst, clip_i, start, secs)
    if not (ok1 and ok2):
        raise Fail("试听片段裁剪失败。")
    log(job, "  试听片段：第 %.0f 秒起 %.0f 秒（整首 %.0f 秒）" % (start, secs, total))

    tracks = sing_role(
        job, rid, clip_v, pitch=pitch, index_rate=index_rate, prefix="preview",
        rms_mix_rate=rms_mix_rate, protect=protect, save=True, pre_sep=(clip_v, clip_i),
        output_kind="preview", label=os.path.basename(song_path), lyric=lyric,
        nonvocal=nonvocal, engine=engine, lyric_nfe=lyric_nfe,
        # ★ 人声裁了，歌词时间戳也得跟着裁 —— 不然片段里一句都对不上，
        #   改词在小样上等于没做（踩过：小样听着还是原词）。
        lyric_seg_offset=start, lyric_seg_dur=secs, hf=hf,
        demetal_mode=demetal_mode)

    # 裁出来的中间片段用完了就删；小样本身留在 final 里给用户听。
    for p in (clip_v, clip_i):
        with contextlib.suppress(Exception):
            os.remove(p)
    return tracks


def sing_job(job, rid, song_path, pitch=0, index_rate=None, quality="standard",
             auto_train=True, model=None, epochs=None, preview_secs=0,
             rms_mix_rate=None, protect=None, sep_model=None, lyric=None,
             nonvocal="keep", engine="rvc", lyric_nfe=None, hf="auto",
             demetal_mode="auto", presence_mode="auto",
             pre_skip=None, post_skip=None, pre_mode=None, lead=None,
             whistle=None):
    """
    「一键唱」入口：缺歌声模型就先把模型训出来，再直接开唱。

    用户只想拖一个歌进来，不想先关心模型有没有 —— 所以把训练和唱歌串成一次任务，
    中间不用他点第二次。训练用档位轮数（默认标准 150 轮）。

    epochs 给了就按自定义轮数训（翻唱台选「自定义」时传进来），
    能训到 300 轮以上 —— 档位里的 300 只是推荐值，不是上限。

    preview_secs > 0 时走「试听小样」那条路（见 _sing_preview）。
    """
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    # ★ 只有 SVC 权重的卡：没显式给引擎时自动走 svc。
    #   否则会被下面"没 RVC 权重就先训一个"的分支拦下来，报一句
    #   「这个角色只有 0 段干声」—— 与真实原因完全无关（2026-10-28 实测踩到）。
    if engine == "rvc" and not (role.get("rvc") or {}).get("model") \
            and (role.get("svc") or {}).get("model"):
        engine = "svc"
        log(job, "  这张卡只有 SVC 权重 ⇒ 自动用 SVC 引擎（RVC 打底 + SVC 音色）")
    if preview_secs:
        return _sing_preview(job, rid, song_path, int(preview_secs), pitch,
                             index_rate, rms_mix_rate, protect, sep_model,
                             lyric=lyric, nonvocal=nonvocal, engine=engine,
                             lyric_nfe=lyric_nfe, hf=hf,
                             demetal_mode=demetal_mode)
    # ★ engine=svc 时**不**走"没 RVC 权重就先训一个"这条路：SVC 卡本来就只有 SVC 权重，
    #   它不需要 RVC 模型。不排除这一支的话，SVC 卡会被当成"缺模型要训练"，
    #   报出一句"这个角色只有 0 段干声（至少要 10 段）"，与真实原因完全无关。
    if (not model and not (role.get("rvc") or {}).get("model")
            and auto_train and engine != "svc"):
        n = len([s for s in role.get("segments", []) if s.get("dur", 0) >= 2.0])
        if n < 10:
            raise Fail(
                "这个角色只有 %d 段干声（至少要 10 段）可以直接训歌声模型。\n"
                "可以先用它的说话音色唱不了歌 —— 请再导入这个人的更多素材。" % n)
        log(job, "  这个角色还没有歌声模型，先训一个（%s）。慢一点，但省得你手动操作。"
            % ("自定义 %d 轮" % epochs if epochs
               else {"fast": "快速 50 轮", "standard": "标准 150 轮",
                     "perfect": "完美 300 轮"}.get(quality, quality)))
        train_rvc(job, rid, epochs=epochs, quality=quality)
    return sing_role(job, rid, song_path, pitch=pitch, index_rate=index_rate,
                     lead=lead, whistle=whistle,
                     model=model, mix=True, sep_model=sep_model,
                     rms_mix_rate=rms_mix_rate, protect=protect, lyric=lyric,
                     nonvocal=nonvocal, engine=engine, lyric_nfe=lyric_nfe,
                     # ★ 这三个以前**没透传**：界面上「临场感」「功能开关」
                     #   选了也白选（典型的"设了没生效"）。现在与精修台同源。
                     hf=hf, demetal_mode=demetal_mode, presence_mode=presence_mode,
                     pre_skip=pre_skip, post_skip=post_skip,
                     pre_mode=pre_mode)


def _rvc_spec_race(out):
    """RVC 是不是崩在**它自己**那个音频缓存的写竞争上。

    `train/data_utils.py` 在 DataLoader worker 里「首次用到才写」`*.spec.pt`，
    既没锁也不是原子写；4 个 worker 一起跑时，Windows 上会偶发

        PermissionError: [Errno 13] Permission denied: ...\\0_gt_wavs\\14_2.spec.pt

    训练于是**在第 1 轮就挂掉**，甩给用户一句看不懂的英文 —— 他会以为是自己的
    素材坏了。这是 RVC 自己的锅：`num_workers=4` 在 `train/train.py` 里写死，
    配置文件和命令行都改不了。我们能做的只有**认出它、自动重来一次**：
    崩过之后大部分 spec 缓存已经落盘，重跑时竞争窗口小得多（实测重试一次就好）。

    ★ 只在**同时**出现 PermissionError/Errno 13 和 .spec.pt 时才认，
      真正的训练错误（底模不对、显存不够…）绝不能被这个重试悄悄吞掉。
    """
    s = out or ""
    return ("PermissionError" in s or "Errno 13" in s) and ".spec.pt" in s


def _wav_quality(path):
    """标准库读一段 wav，量出体检要的几个数。读不动返回 None（不抛）。

    返回 dict：{sr, dur, rms_db, floor_db, peak}
      rms_db   整段响度（含间隙）
      floor_db **底噪地板** = 能量最低的 10% 帧的响度 —— 那几帧就是"这个人没在唱"
                的时候，量出来的就是环境底噪/电流声。整段 rms 会被歌声本身抬上去，
                看不出底噪，所以必须单独取静音帧。
    """
    try:
        import wave
        import array
        import math
        w = wave.open(path, "rb")
        sr, ch, sw, n = (w.getframerate(), w.getnchannels(),
                         w.getsampwidth(), w.getnframes())
        raw = w.readframes(n)
        w.close()
        if sw == 2:
            a = array.array("h")
            a.frombytes(raw)
            scale = 32768.0
        elif sw == 4:
            a = array.array("i")
            a.frombytes(raw)
            scale = 2147483648.0
        else:
            return None
        if ch > 1:
            a = a[::ch]      # 只取左声道：立体声的两路一样，算两遍没意义
        if not len(a):
            return None
        tot = 0.0
        peak = 0.0
        for v in a:
            f = float(v)
            tot += f * f
            if f < 0:
                f = -f
            if f > peak:
                peak = f
        rms = math.sqrt(tot / len(a)) / scale
        # 20ms 分帧，取能量最低的 10% 帧当底噪
        fl = max(1, int(sr * 0.02))
        fr = []
        i = 0
        while i + fl <= len(a):
            s = 0.0
            for v in a[i:i + fl]:
                s += float(v) * float(v)
            fr.append(math.sqrt(s / fl) / scale)
            i += fl
        if not fr:
            return None
        fr.sort()
        k = max(1, len(fr) // 10)
        floor = sum(fr[:k]) / k
        def _db(x):
            return 20.0 * math.log10(x) if x > 0 else -120.0

        return {"sr": sr, "dur": len(a) / float(sr), "rms_db": _db(rms),
                "floor_db": _db(floor), "peak": peak / scale}
    except Exception:
        return None


def audit_train_data(rid, segs=None):
    """开训前给素材做一次体检。**只读、不改任何数据、不抛异常。**

    返回 dict（跑不通返回 None）—— 体检失败不该把训练挡住，这是"给个提醒"不是"设卡"。

    判据全部来自 TRAIN_* 那组常量旁的文献依据与本机实测。
    结果里 `issues` 是给人看的 [(级别, 说明)]，级别 ∈ bad / warn / info：
      bad  = 文献明确判"不合格"
      warn = 合格但不在最佳区间
      info = 只陈述事实（采样率那条就是，因为用户此刻改不了）
    """
    try:
        role = get_role(rid) or {}
        if segs is None:
            segs = [s for s in (role.get("segments") or []) if (s.get("dur") or 0) >= 2.0]
        sd = os.path.join(role_dir(rid), "slices")
        if not segs or not os.path.isdir(sd):
            return None
        # 抽样：段数多时等距抽（不是取前 N 段 —— 素材是按时间排的，前 N 段不代表整批）
        if len(segs) > TRAIN_AUDIT_MAX:
            step = len(segs) / float(TRAIN_AUDIT_MAX)
            idx = [int(i * step) for i in range(TRAIN_AUDIT_MAX)]
            picked = [segs[i] for i in idx]
        else:
            picked = list(segs)
        rows = []
        for s in picked:
            p = os.path.join(sd, s.get("file") or "")
            if not os.path.isfile(p):
                continue
            q = _wav_quality(p)
            if q:
                q["voiced"] = s.get("voiced")
                rows.append(q)
        if not rows:
            return None

        def _pct(xs, p):
            xs = sorted(xs)
            return xs[max(0, min(len(xs) - 1, int((len(xs) - 1) * p)))]

        durs = [r["dur"] for r in rows]
        rms = [r["rms_db"] for r in rows]
        floors = [r["floor_db"] for r in rows]
        srs = {}
        for r in rows:
            srs[r["sr"]] = srs.get(r["sr"], 0) + 1
        total = sum(durs) / float(len(durs)) * len(segs)   # 按抽样均值外推整批
        span = _pct(rms, .9) - _pct(rms, .1)

        issues = []
        # ① 采样率
        top_sr = max(srs.items(), key=lambda kv: kv[1])[0]
        if len(srs) > 1:
            issues.append(("bad", "采样率不统一（%s），混着训会互相拖累。"
                                  % "、".join("%dHz×%d" % (k, v) for k, v in sorted(srs.items()))))
        if top_sr < 44100:
            issues.append(("info", "切片是 %d Hz（高频上限 %.1f kHz）。文献推荐 44.1k/48k；"
                                   "这是切片工具为说话模型降的，下次导入新素材时会保住原采样率。"
                                   % (top_sr, top_sr / 2000.0)))
        # ② 总时长
        if total < TRAIN_TOTAL_MIN:
            issues.append(("warn", "素材合计 %.1f 分钟，文献建议的下限是 %.0f 分钟 —— "
                                   "偏少时模型容易沙哑、音色不稳。"
                                   % (total / 60.0, TRAIN_TOTAL_MIN / 60.0)))
        elif total > TRAIN_TOTAL_MAX:
            issues.append(("info", "素材合计 %.1f 分钟，超过文献说的 %.0f 分钟；"
                                   "再多边际收益很小，训练时间却线性变长。"
                                   % (total / 60.0, TRAIN_TOTAL_MAX / 60.0)))
        # ③ 单段时长
        #   ★ 门槛定 10% 而不是"有就报"：体检是**抽样**的（段数多时等距抽 150 段），
        #     占比在百分之几量级时会随抽样抖动 —— 大帝实测 2.5%~3%，报出来是噪声。
        #     真正要提醒的是"这批素材里有一大截是碎片"。
        short = sum(1 for d in durs if d < TRAIN_SEG_MIN)
        long_ = sum(1 for d in durs if d > TRAIN_SEG_MAX)
        if short and 100.0 * short / len(durs) >= 10.0:
            issues.append(("warn", "有 %.0f%% 的段短于 %.0f 秒（信息量不足，"
                                   "文献建议 3~15 秒）" % (100.0 * short / len(durs), TRAIN_SEG_MIN)))
        if long_ and 100.0 * long_ / len(durs) >= 10.0:
            issues.append(("warn", "有 %.0f%% 的段长于 %.0f 秒" % (100.0 * long_ / len(durs), TRAIN_SEG_MAX)))
        # ④ 底噪
        bad = sum(1 for f in floors if f > TRAIN_FLOOR_BAD)
        meh = sum(1 for f in floors if TRAIN_FLOOR_WARN < f <= TRAIN_FLOOR_BAD)
        if bad:
            issues.append(("bad", "%d 段底噪高于 %.0f dB（文献判「不合格」），"
                                  "这类段会把环境噪音教给模型。" % (bad, TRAIN_FLOOR_BAD)))
        elif meh:
            issues.append(("warn", "%d 段底噪在 %.0f~%.0f dB 之间（合格但不够干净）。"
                                   % (meh, TRAIN_FLOOR_WARN, TRAIN_FLOOR_BAD)))
        # ⑤ 响度一致
        #   ★ 只在越过"合格"线（±6 dB）时才报。±3 dB 是**优秀**线 —— 大帝实测
        #     3.0~3.1 dB 正好骑在这条线上，而体检是抽样的，每次跑会在 3.0/3.1 之间跳，
        #     报出来的是抖动不是问题（铁律：不许把间歇性抖动伪装成能力变化）。
        if span > TRAIN_LOUD_SPAN_WARN:
            issues.append(("warn", "各段响度跨度 %.1f dB（文献：±3 dB 优秀 / ±6 dB 合格），"
                                   "忽大忽小会让模型学到音量起伏而不是音色。" % span))
        # ⑥ 削波
        clip = sum(1 for r in rows if r["peak"] >= 0.999)
        if clip:
            issues.append(("bad", "%d 段音量顶到头（削波失真），这种段教出来的是爆音。" % clip))

        verdict = "bad" if any(lv == "bad" for lv, _ in issues) else (
            "warn" if any(lv == "warn" for lv, _ in issues) else "ok")
        return {"n": len(rows), "sampled": len(rows) < len(segs), "total_secs": total,
                "sr": srs, "top_sr": top_sr,
                "dur_med": _pct(durs, .5), "rms_med": _pct(rms, .5),
                "rms_span": span, "floor_med": _pct(floors, .5),
                "issues": issues, "verdict": verdict}
    except Exception:
        return None


def describe_train_audit(a):
    """把体检结果压成一行给人看的话（用户不看表格，看结论）。"""
    if not a:
        return "素材体检没跑成（不影响训练）"
    head = ("%d 段 / %.1f 分钟 / %dHz / 响度 %.1f dB（跨度 %.1f）/ 底噪 %.0f dB"
            % (a["n"], a["total_secs"] / 60.0, a["top_sr"],
               a["rms_med"], a["rms_span"], a["floor_med"]))
    if a["verdict"] == "ok":
        return head + " —— 素材没问题"
    bad = [m for lv, m in a["issues"] if lv == "bad"]
    warn = [m for lv, m in a["issues"] if lv == "warn"]
    tail = []
    if bad:
        tail.append("**要紧的**：" + "；".join(bad))
    if warn:
        tail.append("可以更好：" + "；".join(warn))
    return head + " —— " + "。".join(tail)


def recommend_rvc_epochs(total_secs, n_segs):
    """按素材量推荐训练轮数。返回 (建议轮数, 一句为什么)。

    ★ 为什么需要这个：轮数不是越多越好 —— 多篇实践一致指出**过训会出机械感/金属声**
      （官方 FAQ 原话："Over-training causes robotic artifacts… roll back to an
      earlier checkpoint"）。而"多少算多"取决于素材量：
        · 素材少 ⇒ 每轮见到的样本少 ⇒ 需要**更多轮**才收敛
          （官方 FAQ："Train longer, 500+ epochs for small datasets"）
        · 素材足 ⇒ 更少轮就够，**继续训只是在背训练集的噪音**

    分档（按总时长，段数只用于兜底）：

    2026-10-18 按**社区实测表**重标 —— 旧分档整体偏高，尤其"素材越多训得越多"
    那几档反了（素材多反而该少训）。三份独立来源给出同一张表：
      · RVC 2.3 实测文章（aixplore.cc）：<3min 不建议正式训练 / 3~5min 350~500 /
        5~10min 280~450 / 10~20min 150~280 / 20~40min 80~160 / 40~60min 50~100 /
        60min+ 30~80
      · voicechanger.live 训练指南：over-training ⇒ 机械/金属声，"best checkpoint
        is rarely the last one"，经验甜点在 200~400 轮（对应 10~20 分钟素材）
      · Applio 文档 / 社区配置：10 分钟以上素材用 200 轮附近，40 分钟以上不必
        机械地训几百轮
    取每档**偏保守的下半段**（宁可少训再说，因为过训的代价是机械感，无法通过
    推理参数救回来；欠训只是音色不够稳，退回去续训即可）：

        < 3 分钟    400  数据太少，怎么训都是过拟合，该做的是补素材
        3~5 分钟    350
        5~10 分钟   300
        10~20 分钟  200   ← 社区甜点区
        20~40 分钟  120   ← 大帝 29.6 分钟落这档（旧表给 200，实际训了 300）
        40~60 分钟   75
        > 60 分钟    55
    纯函数，不抛异常。
    """
    try:
        t = float(total_secs or 0) / 60.0
    except (TypeError, ValueError):
        t = 0.0
    if t <= 0 and n_segs:
        t = n_segs * 5.0 / 60.0      # 时长量不出来就用段数估（一段约 5 秒）
    if t < 3:
        return 400, "素材不到 3 分钟 —— 社区共识是这量级**不适合正式训练**，怎么训都在背训练集；该做的是补素材"
    if t < 5:
        return 350, "素材 %.1f 分钟，属小数据集 ⇒ 要训到 350 轮以上才收敛（社区表 350~500）" % t
    if t < 10:
        return 300, "素材 %.1f 分钟，偏少 ⇒ 300 轮上下（社区表 280~450）" % t
    if t < 20:
        return 200, "素材 %.1f 分钟，正落社区甜点区 ⇒ 200 轮上下就够（社区表 150~280）" % t
    if t < 40:
        return 120, "素材 %.1f 分钟，很充裕 ⇒ 120 轮上下（社区表 80~160）；再多是在背训练集的噪音，容易出机械感" % t
    if t < 60:
        return 75, "素材 %.1f 分钟，量很大 ⇒ 75 轮上下（社区表 50~100），不必机械地训几百轮" % t
    return 55, "素材 %.1f 分钟，量极大 ⇒ 55 轮上下（社区表 30~80），按 step 和试听结果停，不是轮数越多越好" % t


def _vram_free_mb():
    """显卡现在还剩多少 MB 显存（拿不到返回 None）。

    为什么要有它：48k 的批大小限制本质是**显存压力**，不是采样率属性。
    实测同一台机器：被占 4.2GB 时 48k/bs=2 假死（GPU 99% 但一轮都推不动）；
    被占 1.8GB 时 48k/bs=2 全程 5951 MiB、~90 秒/轮。写死 bs=1 等于在显存充裕时
    白慢 5 倍。拿不到就返回 None，调用方退回保守值 —— 探测失败不许改变结论。
    """
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free",
                              "--format=csv,noheader,nounits"],
                             capture_output=True, timeout=15)
        lines = (out.stdout or b"").decode("utf-8", "ignore").strip().splitlines()
        return int(lines[0].strip()) if lines else None
    except Exception:
        return None


def _assert_exp_sr(exp, sr):
    """开训前确认 `logs/<exp>` 里的旧断点和这次的采样率一致。

    为什么单独抽成函数：exp 名只带**架构**不带**采样率**
    （`<rid>` / `<rid>_v2`），所以 v2/32k 和 v2/48k 会落进同一个实验目录。
    32k 生成器的上采样是 10,8,2,2，48k 是 12,10,2,2 —— 直接在 32k 断点上续
    48k 会在加载权重时撞 size mismatch，报错里只有一屏张量尺寸和一个退出码，
    用户完全猜不到原因是"换了采样率"。

    抽出来还有个好处：验收能**直接调它**做负对照（见第 30 关 G 段），
    不用为了验这道护栏去真跑一次训练。
    """
    cfg = os.path.join(RVC_ROOT, "logs", exp, "config.json")
    if not os.path.isfile(cfg):
        return
    try:
        with open(cfg, encoding="utf-8") as f:
            old_sr = int((json.load(f).get("data") or {}).get("sampling_rate") or 0)
    except Exception:
        return          # 读不出来就不拦（不能因为一份坏 config 把训练卡死）
    s = str(sr).strip().lower()
    try:
        want = int(float(s[:-1]) * 1000) if s.endswith("k") else int(s)
    except Exception:
        return
    if old_sr and want and old_sr != want:
        raise Fail(
            "这个实验目录已经有 **%d Hz** 的训练断点（logs/%s），"
            "这次要训的是 **%d Hz**。\n"
            "两者不能共用一个实验目录：生成器的上采样配置不一样，"
            "直接续训会在加载权重时撞 size mismatch（报错里只有一堆张量尺寸）。\n"
            "要选一个：\n"
            "  · 换成和旧断点一样的采样率（%d Hz）；\n"
            "  · 或者换一个底模 —— 自定义底模会带独立的实验名，天然不冲突；\n"
            "  · 或者把旧目录改名留档，让这次从空目录开始：\n"
            "      logs\\%s  →  logs\\%s_sr%d\n"
            "    （改名不会删你原来的模型，想回退把名字改回来即可。）"
            % (old_sr, exp, want, old_sr, exp, exp, old_sr))


def train_rvc(job, rid, epochs=None, sr=None, quality="standard", restart=False,
              arch=None, pretrain=None, bind=True):
    """
    用角色的干声素材训一个 RVC 歌声模型。

    RVC 训练是三段式：切分+预处理 -> 提特征 -> 训练+建索引。
    和 GPT-SoVITS 那套完全独立，所以这里要跨到 RVC 的目录和 python 去跑。

    quality 档位 -> 训练轮数：fast=50 / standard=150 / perfect=300。
    RVC 和说话模型不一样，轮数少了会沙哑，宁可多训。默认 150。

    arch = "v1"（默认）/ "v2"：v2 是官方说质量更好的架构（768 维特征 + 4 判别器），
    但**只能配 32k/48k**（没有 40k），且必须从零重训 ⇒ 用独立的 exp 名，见
    RVC_ARCH_* 那组常量旁的注释。

    pretrain = None（官方语音底模）/ "klm" 等 `RVC_CUSTOM_PRETRAINS` 的键。
    ★ 指定自定义底模时，**架构和采样率由底模自己决定**（KLM 只有 32k ⇒ v2/32k），
      传别的会被忽略 —— 底模和架构对不上是"训练照跑、底模全废"的根源。

    bind = True 时训完把模型绑到角色上；False 只训练、产物留在
    `assets/weights/` 供对比，**不动角色现有模型**（做对照实验用）。
    """
    # ---- 自定义底模：它锁死架构和采样率，且必须走独立 exp ----
    pretrain = (str(pretrain).strip().lower() if pretrain else "") or None
    if pretrain:
        cp = RVC_CUSTOM_PRETRAINS.get(pretrain)
        if not cp:
            raise Fail("没有这个自定义底模：%s（可选：%s）"
                       % (pretrain, " / ".join(RVC_CUSTOM_PRETRAINS) or "无"))
        gp0, dp0 = rvc_pretrained_here(cp["arch"], cp["sr"], pretrain)
        if not (gp0 and dp0):
            raise Fail(
                "自定义底模「%s」没放齐：\n  %s / %s\n"
                "这两个文件**不会自动下载**（不在官方仓库的固定目录结构里），"
                "先手工放好再训。"
                % (cp["label"], os.path.basename(cp["g"]), os.path.basename(cp["d"])))
        arch, sr = cp["arch"], cp["sr"]

    # ---- 架构与采样率：两件事一起定，因为它们是配套的 ----
    arch = str(arch or RVC_ARCH_DEFAULT).lower()
    if arch not in RVC_ARCH_SR:
        arch = RVC_ARCH_DEFAULT
    if not sr:
        sr = RVC_ARCH_SR[arch]
    sr = str(sr).lower()
    if sr not in RVC_ARCH_SRS[arch]:
        raise Fail("采样率 %s 和 %s 架构不配套 —— %s 只能配 %s。"
                   % (sr, arch, arch, " / ".join(RVC_ARCH_SRS[arch])))
    if not epochs:
        epochs = RVC_QUALITY_PRESETS.get(quality, 150)
    role = get_role(rid)
    if not role:
        raise Fail("角色不存在")
    segs = [s for s in role.get("segments", []) if s.get("dur", 0) >= 2.0]
    if len(segs) < 10:
        raise Fail(
            "训歌声模型需要至少 10 段、合计 1 分钟以上的干净干声，"
            "这个角色只有 %d 段。素材不够硬训只会得到一个沙哑的模型。" % len(segs))

    d = role_dir(rid)
    # ★ v2 用独立 exp 名：同一个 exp 下 config.json 会被新架构覆盖，
    #   旧断点（v1 架构）读进来必然 mismatch。分开就两边都安全。
    exp = rid + RVC_ARCH_EXP_SUFFIX.get(arch, "")
    # ★ 换了自定义底模就必须换 exp：RVC 靠 logs/<exp>/G_*.pth 判断"要不要续训"，
    #   而同名下已有断点时会**跳过底模加载**（见下面 was_scratch 那段）。跟官方
    #   底模的模型共用 exp，等于新底模永远不生效 —— 而且会把你原来的断点覆盖掉。
    if pretrain:
        exp += "_" + pretrain
    # ★ exp 名只带**架构**不带**采样率**（`ra500fb92` / `ra500fb92_v2`），
    #   所以 v2/32k 和 v2/48k 会落在同一个 exp 目录里。这不是理论风险：同一批
    #   特征目录名（3_feature768）和同一个 G_*.pth 会被两种采样的训练共用，
    #   48k 的生成器上采样率是 12,10,2,2，32k 是 10,8,2,2 ⇒ load_state_dict 直接
    #   size mismatch，用户看到的是一屏张量尺寸和一个退出码，完全猜不到是"换了采样率"。
    #   所以在开训前把这件事说明白，而不是让他去读报错。
    _assert_exp_sr(exp, sr)
    if arch != RVC_ARCH_DEFAULT:
        log(job, "  架构 %s / %s（v2 用独立的实验名 %s，不会动你原来的模型）"
                 % (arch, sr, exp))
    dataset = os.path.join(d, "rvc_dataset")
    os.makedirs(dataset, exist_ok=True)
    src = os.path.join(d, "slices")
    n = 0
    for s in segs:
        p = os.path.join(src, s["file"])
        if os.path.exists(p):
            shutil.copy(p, os.path.join(dataset, "%03d.wav" % n))
            n += 1
    log(job, "  准备了 %d 段干声" % n)

    # ---- 开训前：给素材做一次体检 + 按素材量给轮数建议 ----
    # ★ 两条都**只报不拦**：体检跑不通、或者结论不好看，都不该把训练挡住
    #   （用户可能就是想硬训一版听听）。这是"给个提醒"，不是"设卡"。
    audit = None
    try:
        audit = audit_train_data(rid, segs)
        if audit:
            log(job, "  素材体检：%s" % describe_train_audit(audit))
            for lv, m in audit["issues"]:
                if lv == "bad":
                    log(job, "    [要紧] %s" % m)
                elif lv == "warn":
                    log(job, "    [可以更好] %s" % m)
    except Exception:
        # 体检是增强，绝不能把训练带死（铁律：增强功能失败要退化，不是崩）
        audit = None
    try:
        _base = audit.get("total_secs") if audit else sum(s.get("dur", 0) for s in segs)
        rec, why = recommend_rvc_epochs(_base, n)
        # 1.3 而不是 1.5：大帝 29.5 分钟推荐 200、perfect 档正是 300（=1.5 倍），
        # 卡在等号上就永远提示不出来 —— 这是"提醒"不是判据，宁可早说。
        if int(epochs) > int(rec) * 1.3:
            log(job, "  [提示] 这次训 %d 轮，但按素材量推荐的是 %d 轮（%s）。"
                     "轮数不是越多越好 —— 训过头会出机械感/金属声，"
                     "文献给的做法是退回早一点的存档试听对比。" % (int(epochs), rec, why))
        elif int(epochs) < int(rec) * 0.5:
            log(job, "  [提示] 这次训 %d 轮，按素材量推荐的是 %d 轮（%s）。"
                     "轮数偏少时模型容易沙哑、音色不稳。" % (int(epochs), rec, why))
    except Exception:
        pass

    # RVC 的训练脚本都在 train/ 下面，必须用 `-m` 以模块方式跑：
    # 直接 `python train/preprocess.py` 时 sys.path[0] 是 train/ 目录，
    # 里面的 train.py 会抢先被当成 train 包，导致 from train import utils 循环导入
    # （ModuleNotFoundError / partially initialized module 两种报错，实测都踩过）。
    # PYTHONPATH 里也**不能**再加 RVC_ROOT/train，否则同样把 train 指向 train.py。
    rvc_env = {"PYTHONPATH": RVC_ROOT}

    # RVC 的 -te 是「训到第几轮」而不是「再训几轮」，而且 train.py 会自动从
    # logs/<exp>/G_*.pth 续训。已经训到 N 轮再传 -te N，循环一次都不跑，
    # 末尾 `if epoch >= hps.total_epoch` 引用未绑定的 epoch 直接 NameError。
    # 所以这里先把「已经多少轮」问清楚：不够就往下续，够了就明说而不是崩。
    # ---- 能不能用上底模：这条决定了「唱出来能不能听」----
    # RVC 的 train.py 只在**没有历史断点**时才去加载 -pg（见 train.py 的
    # try load_checkpoint / except 里才 load_pretrained_generator）。
    # 也就是说：只要 logs/<exp>/G_*.pth 还在，续训就**根本不会**用底模。
    # 所以「手上有个从零训的废模型」这种情况，光加 -pg 是救不回来的，
    # 必须先把断点挪走、从头训一次。这里把这件事摆到台面上，别让用户白等
    # 几十分钟，训完发现还是不跟调。
    rvc_exp0 = os.path.join(RVC_ROOT, "logs", exp)
    ckpts = sorted(glob.glob(os.path.join(rvc_exp0, "[GD]_*.pth")))
    was_scratch = (role.get("rvc") or {}).get("pretrained") is False
    if ckpts and was_scratch and not restart:
        raise Fail(
            "这个角色现有的歌声模型是**没用底模、从零训**的（输出不跟旋律、"
            "变调无效，就是「唱出来没法听」最常见的原因）。\n"
            "而 RVC 只在没有历史断点时才会用底模 —— 直接在旧模型上续训，"
            "底模依然不会被加载，训完还是老样子。\n"
            "要修好它，请勾选「从头重训（换用底模）」再开始。\n"
            "（旧的断点会挪到 logs/%s/_从零训的旧断点/ 保留，不会丢；"
            "预处理好的特征会复用，不用重新提。）" % exp)
    if restart and ckpts:
        bak = os.path.join(rvc_exp0, "_从零训的旧断点")
        os.makedirs(bak, exist_ok=True)
        for p in ckpts:
            # 移动而不是复制：这两个文件加起来 1G 多，复制一份可能把盘撑满
            shutil.move(p, os.path.join(bak, os.path.basename(p)))
        log(job, "  已挪走 %d 个旧断点到 %s，这次会用底模从头训。" % (len(ckpts), bak))
    # ★ 光挪断点不够：`assets/weights/<exp>_e<N>_s<M>.pth` 是**每一轮存档**留下的
    #   中间权重，上一轮训到 300 的话那里就躺着 e165…e300 一堆。不挪走的话：
    #     · 界面/接口问"训到第几轮"时会被这些旧文件带偏（实测：ckpt 读到 150，
    #       而权重文件名里最大是 300，对不上）；
    #     · 万一最终权重没写成，挑权重的逻辑还可能挑到旧的那一份 —— 那就是
    #       "重训了却还在用废模型"，比不重训更坑。
    if restart:
        wdir = os.path.join(RVC_ROOT, "assets", "weights")
        stale = [os.path.join(wdir, f) for f in os.listdir(wdir)
                 if f.startswith(exp) and f.endswith(".pth")] if os.path.isdir(wdir) else []
        if stale:
            wbak = os.path.join(rvc_exp0, "_从零训的旧断点")
            os.makedirs(wbak, exist_ok=True)
            for p in stale:
                # 同理用移动；同名的新权重随后会被训练覆写回来
                shutil.move(p, os.path.join(wbak, os.path.basename(p)))
            log(job, "  已挪走 %d 个上一轮的中间权重到 %s（避免和新训的混在一起）"
                % (len(stale), wbak))
    if restart and was_scratch:
        log(job, "  旧模型是从零训的，这次会换成「底模 + 你的素材」重训。")

    done = 0 if restart else rvc_trained_epochs(rid)
    if done and done >= epochs:
        # 以前的文案让用户「去加素材」，这是错的 —— RVC 本来就支持续训，
        # 缺的只是「把目标调高」。用户问的正是"训完一次还能不能接着训"，
        # 所以这里必须直接告诉他能怎么做，而不是把他支走。
        raise Fail(
            "这个角色的歌声模型已经训到 %d 轮了，你要的 %d 轮没有更多可训的轮数。\n"
            "RVC 的轮数是「训到第几轮」，不是「再训几轮」——同一份素材接着训是有效的，\n"
            "但目标必须比已经训过的轮数大。想把音色继续练细：\n"
            "  · 把「训练质量」选「自定义…」，轮数填 %d（或更高，上限 5000），\n"
            "    会从第 %d 轮接着往下训；\n"
            "  · 轮数不变的话，重训得到的是同一水平的模型，不会有额外提升。\n"
            "（素材本身没变时，提升主要来自更干净的干声 / 更多素材。）"
            % (done, epochs, max(done * 2, done + 50), done))

    rvc_exp = os.path.join(RVC_ROOT, "logs", exp)
    os.makedirs(os.path.join(rvc_exp, "0_gt_wavs"), exist_ok=True)

    job["stage"] = "RVC 预处理"
    job["pct"] = 12
    save_job(job)
    rc, out = run(job, '"%s" -m train.preprocess "%s" %s 4 "%s" True 3.7'
                  % (RVC_PY, dataset, int(sr[:-1]) * 1000,
                     os.path.join(RVC_ROOT, "logs", exp)),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        raise Fail("RVC 预处理失败（退出码 %d）。\n%s" % (rc, out[-800:]))

    # 提 F0（基频）：这一版 RVC 的脚本在 train/dataset/ 下，签名是
    #   extract_f0.py cuda <卡数> <第几张卡> <卡号> <exp目录> <is_half>
    # 之前写成 train/extract_f0_print.py 是照老版本的记忆，那个脚本早没了。
    n_p = max(1, min(os.cpu_count() or 4, 4))
    job["stage"] = "RVC 提取基频"
    job["pct"] = 32
    save_job(job)
    rc, out = run(job, '"%s" train/dataset/extract_f0.py cuda 1 0 0 "%s" True'
                  % (RVC_PY, os.path.join(RVC_ROOT, "logs", exp)),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        raise Fail("RVC 提取基频失败（退出码 %d）。\n%s" % (rc, out[-800:]))

    # 提 HuBERT 特征：extract_hubert_feature.py <device> <卡数> <第几张> <卡号> <exp> <version> <is_half>
    job["stage"] = "RVC 提取音色特征"
    job["pct"] = 52
    save_job(job)
    rc, out = run(job, '"%s" train/dataset/extract_hubert_feature.py cuda 1 0 0 "%s" %s True'
                  % (RVC_PY, os.path.join(RVC_ROOT, "logs", exp), arch),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        raise Fail("RVC 提取音色特征失败（退出码 %d）。\n%s" % (rc, out[-800:]))

    # ---- 训练前置：WebUI 在开训前会写 config.json 和 filelist.txt，
# 直接调 train.py 是绕不过这两份文件的（utils.get_hparams 强制读 config.json），
# 所以这里照 RVC WebUI 的同一套规则生成，不能自己发挥。
# ----
    exp_dir = os.path.join(RVC_ROOT, "logs", exp)
    cfg_src = os.path.join(RVC_ROOT, "configs", arch, "%s.json" % sr)
    with open(cfg_src, encoding="utf-8") as f:
        cfg = json.load(f)
    cfg.pop("speaker_info", None)          # 单说话人模式不需要
    with open(os.path.join(exp_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

    gt = os.path.join(exp_dir, "0_gt_wavs")
    # ★ 特征目录名跟着架构变（RVC 自己的规则，见 extract_hubert_feature.py）：
    #     v1 → 3_feature256（256 维）/ v2 → 3_feature768（768 维）
    fea = os.path.join(exp_dir, "3_feature256" if arch == "v1" else "3_feature768")
    f0 = os.path.join(exp_dir, "2a_f0")
    f0nsf = os.path.join(exp_dir, "2b-f0nsf")
    names = sorted(os.path.splitext(x)[0] for x in os.listdir(gt) if x.endswith(".wav"))
    lines, skipped = [], 0
    for n in names:
        if not (os.path.exists(os.path.join(fea, n + ".npy"))
                and os.path.exists(os.path.join(f0, n + ".wav.npy"))
                and os.path.exists(os.path.join(f0nsf, n + ".wav.npy"))):
            skipped += 1
            continue
        lines.append("%s/%s.wav|%s/%s.npy|%s/%s.wav.npy|%s/%s.wav.npy|0" % (
            gt.replace("\\", "\\\\"), n, fea.replace("\\", "\\\\"), n,
            f0.replace("\\", "\\\\"), n, f0nsf.replace("\\", "\\\\"), n))
    if not lines:
        raise Fail("特征文件不完整（%d 段有音频但缺特征），提特征那一步没成功。" % skipped)
    with open(os.path.join(exp_dir, "filelist.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(job, "  训练清单：%d 条可用%s"
        % (len(lines), "（%d 段缺特征已跳过）" % skipped if skipped else ""))

    # 训练：这版 train.py 自己 parse 命令行（train/utils.py 的 get_hparams），
    # 必填：-se 保存间隔 -te 总轮数 -bs 批 -e 实验名 -sr 采样率 -v 版本；
    # -sw=1 才会把权重存下来（不存就没有 .pth），-l 存 latest，-c 缓存进显存（8G 卡关掉省显存）
    job["stage"] = "RVC 训练"
    job["epochs_total"] = epochs
    job["epochs_done"] = None

    def _rvc_track(line):
        """把 RVC 的轮次进度实时读出来 —— 150 轮要十几分钟，pct 不动的话
        和"卡死了"没区别（用户已经为这个提过一次意见）。

        ★ 这一版 RVC 打印的**不是** `Epoch: 37/150`，而是中文的
          `INFO:r8c323d43:====> 轮次：50 [时间] | (耗时)`
        只有开头有一行 `训练轮次：1 [0%]`。**只看英文 Epoch 的话进度条会一直
        卡在开训那一刻**（实测：跑到第 50 轮了，pct 还停在 58），
        所以两种写法都得认，总数取我们自己传的 -te。
        """
        s = line
        m = re.search(r"Epoch[:\s]+(\d+)\s*/\s*(\d+)", s)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
        else:
            m2 = re.search(r"轮次[：:]\s*(\d+)", s)
            if not m2:
                return
            done = int(m2.group(1))
            total = int(epochs or 0) or int(job.get("epochs_total") or 0)
        if not total:
            return
        if done <= (job.get("epochs_done") or 0):
            # 开训那行 `训练轮次：1 [0%]` 也会被"轮次"正则命中，
            # 不挡住的话进度会被拽回第 1 轮。同一轮的重复输出同理。
            return
        job["epochs_done"] = done
        job["pct"] = max(job.get("pct") or 0, 58 + int(34 * done / max(total, 1)))
        job["stage"] = "RVC 训练 · 第 %d/%d 轮" % (done, total)
        job["detail"] = "第 %d / %d 轮" % (done, total)
        save_job(job)

    # ---- 底模：没有它，训出来的模型不会跟调（详见上面的长注释）----
    # 放在开训**之前**，因为下载要一两分钟，用户得看见它在干什么。
    job["stage"] = "准备 RVC 底模"
    job["pct"] = max(job.get("pct") or 0, 58)
    save_job(job)
    pg, pd = ensure_rvc_pretrained(job, arch, sr, pretrain)
    if not (pg and pd):
        # 硬失败而不是"没底模也接着训"：从零训出来的模型听起来就是"没法听"，
        # 而且用户完全没法自己定位原因（他会以为是自己素材不好）。
        # 宁可挡在这里让他知道，也不要交给他一个废模型。
        raise Fail(
            "RVC 底模（预训练模型）没准备好，训练不能开始。\n"
            "没有底模就是从随机初始化开始训，训出来的歌声模型**不会跟着旋律唱**"
            "（输出卡在角色自己的音区、变调完全无效）——这正是「唱出来的歌没法听」"
            "最主要的原因，而且事后极难排查。\n"
            "请手动下载这两个文件放到：\n"
            "  %s\n"
            "  %s\n"
            "下载地址（任选一个源）：\n%s"
            % (rvc_pretrained_paths(arch, sr, pretrain)[0],
               rvc_pretrained_paths(arch, sr, pretrain)[1],
               "\n".join("  %s/pretrained%s/f0%s%s.pth"
                         % (h, "_v2" if arch == "v2" else "", x, sr)
                         for h in RVC_PRETRAIN_HOSTS for x in "GD")))
    # 光"文件在"不够 —— 版本/采样率选错时文件照样在位，但加载不进架构，
    # 训练会静默退化成"没底模"。所以开训前必验一次（详见 check_rvc_pretrained）。
    begin_step(job, "准备 RVC 底模", "歌声模型要从一个预训练过的底模上继续练，"
                                     "否则学不会跟旋律")
    ok, msg = check_rvc_pretrained(job, arch, sr, pretrain)
    if not ok:
        end_step(job, False, "底模加载不上：%s" % msg)
        raise Fail(
            "RVC 底模和要训的架构对不上，训练不能开始。\n%s\n"
            "常见原因是版本或采样率选错（%s/%s 要用 assets/pretrained%s/ 下的 "
            "f0G%s.pth + f0D%s.pth）。换成对得上的底模再试。"
            % (msg, arch, sr, "_v2" if arch == "v2" else "", sr, sr))
    log(job, "  底模就绪：%s / %s（%s）" % (os.path.basename(pg), os.path.basename(pd), msg))
    end_step(job, True, "已就位且加载正常（%s）" % os.path.basename(pg))
    used_pretrained = True
    job["stage"] = "RVC 训练"

    # 训练：这版 train.py 自己 parse 命令行（train/utils.py 的 get_hparams），
    # 必填：-se 保存间隔 -te 总轮数 -bs 批 -e 实验名 -sr 采样率 -v 版本；
    # -sw=1 才会把权重存下来（不存就没有 .pth），-l 存 latest，-c 缓存进显存（8G 卡关掉省显存）
    # ★ -pg/-pd 是底模，没有它就从随机初始化训 —— 见上面「RVC 底模」的长注释。
    # ★ -bs 跟着架构走：v2 是 768 维特征 + 4 个判别器，官方明说"更吃显存"，
    #   本机是 8GB 卡，v1/40k 用 4 跑得很稳，v2 就得往下让（见 RVC_ARCH_BS）。
    # ★ 再叠一层采样率上限（见 RVC_SR_BS_CAP）：48k 在**显存不够**时会静默卡死
    #   （GPU 99% + tfevents 十分钟不写），比报 OOM 更难查。
    #   但这层限制本质是显存，所以先问一句"现在还剩多少"，够就放回默认批大小。
    _cap = RVC_SR_BS_CAP.get(str(sr).lower())
    if _cap:
        _free = _vram_free_mb()
        if _free is not None and _free >= RVC_48K_BS2_FREE_MB:
            log(job, "  显存还剩 %d MB（够 48k 用默认批大小）⇒ 不再压到 %d。"
                     % (_free, _cap))
            _cap = None
        else:
            log(job, "  显存只剩 %s MB，48k 用默认批大小约需 %d MB ⇒ 压到 bs=%d。"
                     "关掉其它占显存的程序再训会快很多（实测能快 5 倍）。"
                     % (_free if _free is not None else "?", RVC_48K_BS2_FREE_MB, _cap))
    _bs = min(RVC_ARCH_BS.get(arch, 4), _cap or 99)
    _train_cmd = ('"%s" -m train.train -e "%s" -sr %s -f0 1 -bs %d -te %d -se 15 '
                  '-g 0 -sw 1 -l 1 -c 0 -v %s -pg "%s" -pd "%s"'
                  % (RVC_PY, exp, sr, _bs, epochs, arch, pg, pd))
    if _bs < 4:
        log(job, "  %s/%s 比 v1 吃显存，批大小已按 %d 跑（v1/40k 是 4）；"
                 "如果中途报显存不够，再往下调。" % (arch, sr, _bs))
    rc, out = run(job, _train_cmd, cwd=RVC_ROOT, env=rvc_env,
                  timeout=21600, on_line=_rvc_track)
    # ★ 见 _rvc_spec_race：这是 RVC 自己的偶发写竞争，而且重试一次基本就好。
    #   不接住的话，用户看到的是第 1 轮就崩 + 一句英文 PermissionError。
    for _retry in (1, 2):
        if rc == 0 or not _rvc_spec_race(out):
            break
        log(job, "  [提示] RVC 在准备音频缓存时撞上文件占用（它自己的老问题，"
                 "Windows 上偶发），自动重来一次（第 %d 次）" % _retry)
        # 进度条得从头算：不清掉的话 _rvc_track 会把重跑的第 1 轮当成"没前进"给挡掉
        job["epochs_done"] = None
        job["stage"] = "RVC 训练"
        save_job(job)
        time.sleep(3)
        rc, out = run(job, _train_cmd, cwd=RVC_ROOT, env=rvc_env,
                      timeout=21600, on_line=_rvc_track)
    if rc != 0:
        raise Fail("RVC 训练失败（退出码 %d）。\n%s" % (rc, out[-1500:]))

    # 建特征索引库：train_index.py <实验名> <版本> <索引输出目录> <CPU数>
    # 没有索引时唱歌会自动降级 index-rate 0，有索引音色细节更足
    index_root = os.path.join(RVC_ROOT, "assets", "indices")
    os.makedirs(index_root, exist_ok=True)
    job["stage"] = "RVC 建立索引"
    job["pct"] = max(job.get("pct") or 0, 92)
    job["detail"] = "训练完成，正在建立索引"
    save_job(job)
    rc, out = run(job, '"%s" train/train_index.py "%s" %s "%s" %d'
                  % (RVC_PY, exp, arch, index_root,
                     max(1, min(os.cpu_count() or 4, 4))),
                  cwd=RVC_ROOT, env=rvc_env, timeout=3600)
    if rc != 0:
        log(job, "  [warn] 索引没建成（退出码 %d），唱歌时会自动用 index-rate 0：%s"
            % (rc, (out or "")[-200:]))

    weights = os.path.join(RVC_ROOT, "assets", "weights")
    os.makedirs(weights, exist_ok=True)
    found = [f for f in os.listdir(weights) if f.startswith(exp) and f.endswith(".pth")]
    if not found:
        raise Fail("RVC 训练结束了但没找到权重文件。")
    # 选权重必须**按轮数数值排序**，不能按文件名字典序。
    #
    # RVC 每次到 save_every 存一份 `<exp>_e<轮数>_s<步数>.pth`，训完再存一份不带
    # 轮数的 `<exp>.pth`（那才是真正的最终权重，process_ckpt.py 的 savee 用
    # hps.name 直接命名）。以前这里写的是 `sorted(...)[-1]` —— 字典序：
    #     "..._e150_..." < "..._e90_..."      e150 排在 e90 前面！
    #     "..._e300_..." < "..._e90_..."     e300 也排在 e90 前面！
    # 所以训满 300 轮，角色绑定的却是 e90 那份，白训 210 轮。
    # 这不是理论问题：本机 data/roles.json 里 r8c323d43 就是 epochs=300
    # 却绑着 r8c323d43_e90_s1620.pth。
    best = pick_rvc_weight(found, base_dir=weights)
    if not best:
        raise Fail("RVC 训练结束了但没能从权重里挑出可用的一份：%s"
                   % ", ".join(sorted(found)[:6]))
    # 记下"这次训的时候有没有底模"：以后唱歌时就能直接说
    # "你手上的模型是从零训的，唱出来不跟调是正常的，重训一次就好"
    # —— 否则用户只会觉得是自己的素材不行，永远找不到真正的原因。
    if not bind:
        # 对照实验模式：产物留在 assets/weights/ 供 A/B，**不动角色现有模型**。
        # 为什么需要这个模式：换底模 / 换轮数这类实验要能跑完还能退回去。
        # 一旦绑了角色，"实验版效果更差"就得手工改 roles.json 恢复。
        log(job, "  [实验模式] 不绑定到角色 —— 产物：%s" % best)
        log(job, "  声音模型（未绑定）：%s（%d 轮）%s"
            % (best, _rvc_weight_epoch(best) or epochs,
               "（含索引库）" if _find_rvc_index(best) else "（无索引库）"))
        job["experiment_model"] = best
        return best
    role = mutate_role(rid, lambda r: r.__setitem__(
        "rvc", {"model": best, "epochs": epochs, "at": _now(),
                "weight_epoch": _rvc_weight_epoch(best),
                "pretrained": bool(used_pretrained),
                "index": bool(_find_rvc_index(best))}))
    if role["rvc"].get("weight_epoch") not in (None, epochs):
        log(job, "  [warn] 绑定的权重实际是 %s 轮（不是刚训的 %d 轮）"
            % (role["rvc"]["weight_epoch"], epochs))
    log(job, "  歌声模型：%s（%d 轮）%s"
        % (best, role["rvc"].get("weight_epoch") or epochs,
           "（含索引库，音色细节更足）" if role["rvc"]["index"]
           else "（无索引库，唱歌时自动用 index-rate 0）"))
    # 训练完顺手收掉这一轮的中间存档（只留最近几份）。
    #
    # ★ 不只是为了省空间：RVC 下一轮续训时，上一轮的 `<exp>_e<N>_s<M>.pth`
    #   还赖在 assets/weights 里，问"训到第几轮"会被它们带偏 ——
    #   已经踩过一次：断点读到 150 轮，权重文件名里最大却是 300，对不上。
    try:
        r = prune_rvc_archives(exp)
        if r.get("freed"):
            log(job, "  已顺手清掉 %d 份中间存档，释放 %s"
                % (len(r.get("deleted") or []), r.get("freed_s")))
    except Exception as e:
        log(job, "  [warn] 清理中间存档时出错（不影响本次训练）：%s" % e)
    return role["rvc"]


def music_fetch_job(job, item_id, title=None):
    """下载一首歌到本地曲库并做时长体检，返回落盘信息。"""
    import music_src

    job["stage"] = "解析下载地址"
    job["pct"] = 10
    log(job, "解析：%s" % (title or item_id))
    try:
        res = music_src.fetch(item_id, timeout=300)
    except Exception as e:
        raise Fail("下载失败：%s" % e)

    job["stage"] = "校验音频"
    job["pct"] = 60
    dur = music_src.probe_duration(res["path"])
    log(job, "已下载 %s（%.1f MB）" % (res["path"], res["size"] / 1048576.0))
    if dur <= 0:
        # 时长量不出来只警告不失败：ffmpeg 缺失不该阻断导歌。
        log(job, "  [warn] 量不到时长（ffmpeg 不可用），仍按可用处理")
    else:
        log(job, "  时长：%.1f 秒" % dur)

    song = {
        "title": title or os.path.basename(res["path"]),
        "path": res["path"],
        "size": res["size"],
        "dur": round(dur, 1),
        "src": res["src"],
        "item": item_id,
        "at": _now(),
    }
    add_library_song(song)
    job["pct"] = 100
    log(job, "已加入本地曲库：%s" % song["title"])
    return song


LIBRARY_JSON = os.path.join(DATA, "music_library.json")
_LIBRARY_LOCK = threading.Lock()


def load_library():
    try:
        with open(LIBRARY_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"songs": []}


def save_library(db):
    with _LIBRARY_LOCK:
        tmp = LIBRARY_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LIBRARY_JSON)


def add_library_song(song):
    db = load_library()
    # 同一个曲目（item id）只留一条：文件路径带时间戳，光按 path 去重拦不住重复下载。
    item = song.get("item")
    keep = [s for s in db["songs"]
            if not (item and s.get("item") == item)
            and s.get("path") != song.get("path")]
    db["songs"] = keep
    db["songs"].insert(0, song)
    db["songs"] = db["songs"][:200]
    save_library(db)
    return song

# ================================================================ 存储管理
#
# 为什么要有这一段：训一次歌声模型会在两个地方各留下一串 55MB 的权重
# （RVC 的 assets/weights 每 15 轮一份 + logs/<exp> 里的 G/D 断点），
# 每拆一个视频又会在 data/jobs/<id>/work/ 下留下 250MB 中间产物，
# 传上来的原片更是动辄 700MB。跑十几次就是十几 G —— 但**用户没有任何入口
# 去删它们**，只能自己进资源管理器翻三层目录猜哪个能删；删错（把角色正在
# 用的那一份删了）模型当场就废。
#
# 所以这段做的事：把散在几个根目录的占用全扫出来，按「删了会怎样」分类，
# 把**正在被角色引用的那一份锁死**，剩下的才允许勾，删完再留一份清单。
#
# ★ 这里刻意不做「移到回收站」：那只是换个地方占着，用户要的是"别占位置"。
#   所以默认真删 —— 代价是不可撤销，所以才必须先分类 + 上锁 + 二次确认，
#   而不是给用户一个「一键清空」。
STORAGE_JSON = os.path.join(DATA, "storage.json")
TRASH_LOG_DIR = os.path.join(DATA, "_trash_log")

# 训练完自动保留几个中间存档（0 = 只留最终权重）。
# 为什么要留而不是全删：留最近 2 份，"最新那份反而更差"时还能退回去 ——
# 这不是假设，RVC 训到后期偶尔会过拟合变尖。
KEEP_RVC_ARCHIVES = 2
# 分离缓存上限（GB）。一首 4 分钟的歌约 100MB，20 首就 2G。
CACHE_MAX_GB = 3.0

# RVC 训练中间产物里"重训时会自动重建"的那几个目录。
# 不能写 logs/<exp> 整体：那里面还躺着 G_/D_ 断点，删了就没法续训了。
RVC_PREP_DIRS = ("0_gt_wavs", "1_16k_wavs", "2a_f0", "2b-f0nsf",
                 "3_feature256", "5_wav32")


def _humansize(n):
    n = float(n or 0)
    if abs(n) < 1024.0:
        return "%d B" % int(n)
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024.0
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
    return "%.1f PB" % n


def _path_size(p):
    """文件或目录的真实占用（只看 st_size，不读内容）。"""
    if os.path.isfile(p):
        try:
            return os.path.getsize(p)
        except Exception:
            return 0
    total = 0
    try:
        for root, _dirs, files in os.walk(p):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _mt_s(p):
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(p)))
    except Exception:
        return "-"


def storage_settings():
    d = {"keep_archives": KEEP_RVC_ARCHIVES, "auto_prune": True,
         "cache_max_gb": CACHE_MAX_GB,
         # 分离完自动按中文文件夹另存一份分轨（默认关：多数人用不上，
         # 一首歌两份 wav 就是几十 MB，D 盘本来就不宽裕）。
         "keep_stems": False}
    try:
        with open(STORAGE_JSON, "r", encoding="utf-8") as f:
            d.update(json.load(f))
    except Exception:
        pass
    return d


def save_storage_settings(patch):
    d = storage_settings()
    d.update(patch or {})
    os.makedirs(DATA, exist_ok=True)
    tmp = STORAGE_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STORAGE_JSON)
    return d


def _rvc_weights_dir():
    return os.path.join(RVC_ROOT, "assets", "weights")


def _rvc_logs_dir():
    return os.path.join(RVC_ROOT, "logs")


def _running_job_ids():
    """正在跑/排队/暂停的任务 —— 它们的目录正在被写，删了任务会炸。"""
    return set(j.get("id") for j in list_jobs()
               if j.get("state") in ("running", "queued", "paused"))


def _protected_map():
    """不可删路径 -> {"why": 原因, "kind": 锁的类型}。

    ★ 最容易漏的就是「角色正在用的那份权重」：删了它，界面上角色还在、唱歌
      按钮还能点，一跑才报"找不到模型"。用户不会想到是自己刚勾掉的那项造成的。
      所以必须在这里显式锁住，并写清是谁在用。

    ★ kind 决定这把锁能不能被"强制解锁"：
        role       —— 只是某个角色在用，用户想连模型一起删是可以的（他会重训）
        pretrained —— 底模，删了要重新下载（本机 huggingface 直连 000），永远不解锁
        job        —— 任务正在跑，删了任务会炸，永远不解锁

    ★ 值写成 {"why","kind"} 而不是光一个字符串，就是为了上面这件事。
      但外面（门禁的负对照等）传进来的可能还是老的字符串形式，
      所以读的地方一律兼容两种：字符串一律按"硬锁"处理（不给解锁）。
    """
    prot = {}

    def _lock(path, why, kind):
        prot[os.path.abspath(path)] = {"why": why, "kind": kind}

    db = load_roles()
    roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
    roles = roles if isinstance(roles, list) else list(roles.values())
    for r in roles:
        name = r.get("name") or r.get("id")
        rv = r.get("rvc") or {}
        if rv.get("model"):
            _lock(os.path.join(_rvc_weights_dir(), rv["model"]),
                  "角色「%s」正在用它唱歌" % name, "role")
            # 同名索引库：RVC 靠 _find_rvc_index 匹配，名字带 IVF 参数拼不出来
            ix = _find_rvc_index(rv["model"])
            if ix:
                _lock(ix, "角色「%s」的歌声索引库" % name, "role")
        # ★ 说话模型在 r["model"]["ckpt"]，**不是** r["gsv"]["weight"]。
        #   读错字段的后果已经发生过一次：保护表压根没锁住它，清理时
        #   角色正在用的那份 ckpt 被当成"没人用的旧存档"删掉了，
        #   于是角色卡片照样显示"已训练"，一点朗读就崩在找不到模型。
        #   而且 ckpt 记的是**带目录的相对路径**（"GPT_weights_v5turbo/xxx.ckpt"），
        #   直接 join(GPT_WEIGHT_DIR, ckpt) 会拼出两层重复目录 ⇒ 锁到空气上。
        #   所以两种拼法都锁（多锁几个不存在的路径没有副作用，
        #   少锁一个就又是上面那个事故）。
        ck = (r.get("model") or {}).get("ckpt")
        if ck:
            ck = ck.replace("\\", "/")
            cands = [os.path.join(GSV_ROOT, ck.replace("/", os.sep)),
                     os.path.join(GPT_WEIGHT_DIR, os.path.basename(ck))]
            if os.path.isabs(ck):
                cands = [ck]
            for p in cands:
                _lock(p, "角色「%s」正在用它说话" % name, "role")
    # 底模：所有歌声模型都从它上面长出来，删了以后每次重训都要重新下载
    # （而且不一定下得动 —— huggingface.co 在本机直接 HTTP 000）。
    for v in ("v1", "v2"):
        for s in ("40k", "32k", "48k"):
            gp, dp = rvc_pretrained_paths(v, s)
            for p in (gp, dp):
                _lock(p, "RVC 底模（所有歌声模型的起点）", "pretrained")
    for jid in _running_job_ids():
        _lock(os.path.join(JOBS_DIR, jid), "任务正在运行中", "job")
    return prot


def _lock_kind(path, prot):
    """这把锁是什么类型（role / pretrained / job）。没锁返回空串。

    只有 kind == "role" 的才允许被"强制解锁"—— 因为那只是"删了要重训"，
    而底模和正在跑的任务删了是真的救不回来。
    """
    ap = _nc(path)
    for p, v in (prot or {}).items():
        if _nc(p) == ap or (os.path.isdir(p) and _under(ap, p)):
            return (v or {}).get("kind", "hard") if isinstance(v, dict) else "hard"
    return ""


def _nc(p):
    """Windows 下路径比较要忽略大小写，否则 D:\\a 和 d:\\A 会被当成两个路径。"""
    return os.path.normcase(os.path.abspath(p))


def _under(child, parent):
    try:
        return os.path.commonpath([_nc(parent), _nc(child)]) == _nc(parent)
    except Exception:
        return False


def _lock_for(path, prot):
    """某个路径被保护吗？返回原因（空串=没被保护）。

    ★ 必须查「祖先」：受保护的常常是**目录**（比如正在运行的任务目录
      data/jobs/<id>），而要删的是它下面的 work/。只比对全等的话，
      那一项会被标成"可删"，用户勾了却在真正删的时候才被拒 ——
      界面上就成了"点了没反应"，比一开始标灰更糟。
    """
    ap = _nc(path)
    for p, v in (prot or {}).items():
        if _nc(p) == ap or (os.path.isdir(p) and _under(ap, p)):
            # 兼容老的字符串形式（门禁的负对照会直接塞字符串）：
            # 认不出类型的一律当硬锁，不给解锁。
            return (v or {}).get("why", "") if isinstance(v, dict) else (v or "")
    return ""


def _mk_item(group, path, kind, name=None, note="", lock="", parts=None, exp=""):
    ap = os.path.abspath(path)
    return {
        "id": "%s:%s" % (group, _nc(ap)),
        "group": group,
        "kind": kind,
        # exp：属于哪个训练实验。界面/清理策略要按它分组判断
        # （"这个实验还有没有对应角色" 决定了它的断点能不能删）。
        # 不能靠路径现猜：预处理那一项的 path 就是 logs/<exp> 本身，
        # 取 dirname 会拿到 "logs"，而断点那一项取 dirname 才是 <exp> ——
        # 两种形状混在一个组里，靠猜必然错一次。
        "exp": exp,
        "name": name or os.path.basename(ap.rstrip("\\/")) or ap,
        "path": ap,
        # parts 非空时**只删 parts**（用于"一个目录里的某几个子目录"这种项），
        # 避免为了清预处理特征把整个实验目录（含断点）端掉。
        "parts": [os.path.abspath(x) for x in (parts or [])],
        "size": _path_size(ap) if not parts else sum(_path_size(x) for x in parts),
        "mtime_s": _mt_s(ap),
        "note": note,
        "lock": lock,
        "safe": not lock,
        # suggest = 「一键勾选安全项」会选上它：只选删了绝不心疼的那些
        "suggest": (not lock) and kind in (
            "archive", "ckpt", "prep", "bak", "jobwork", "cache", "trash"),
    }


def _scan_rvc_weights(prot):
    d = _rvc_weights_dir()
    if not os.path.isdir(d):
        return []
    by_exp = {}
    for f in sorted(os.listdir(d)):
        if not f.endswith(".pth"):
            continue
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        m = re.match(r"^(.*?)(?:_e(\d+)_s(\d+))?\.pth$", f)
        exp = m.group(1) if m else f[:-4]
        ep = int(m.group(2)) if (m and m.group(2)) else -1
        by_exp.setdefault(exp, []).append((f, p, ep))
    items = []
    for exp, lst in by_exp.items():
        # 轮数最高的几份算"新的"，其余才是真正的旧存档
        newest = sorted({e for _, _, e in lst if e >= 0}, reverse=True)[:KEEP_RVC_ARCHIVES]
        for _f, p, ep in lst:
            lock = _lock_for(p, prot)
            if ep < 0:
                items.append(_mk_item("rvc_weight", p, "final", lock=lock,
                                      exp=exp,
                                      note="最终权重（角色唱歌用的就是它）"))
            elif ep in newest:
                items.append(_mk_item("rvc_weight", p, "archive", lock=lock,
                                      exp=exp,
                                      note="中间存档 · 第 %d 轮（较新，留着可退回）" % ep))
            else:
                items.append(_mk_item("rvc_weight", p, "archive", lock=lock,
                                      exp=exp,
                                      note="中间存档 · 第 %d 轮（已有更新的，删了没影响）" % ep))
    return sorted(items, key=lambda x: -x["size"])


def _scan_rvc_logs(prot):
    root = _rvc_logs_dir()
    if not os.path.isdir(root):
        return []
    items = []
    for exp in sorted(os.listdir(root)):
        ed = os.path.join(root, exp)
        if not os.path.isdir(ed):
            continue
        for f in sorted(os.listdir(ed)):
            if re.match(r"^[GD]_\d+\.pth$", f):
                p = os.path.join(ed, f)
                items.append(_mk_item("rvc_ckpt", p, "ckpt", exp=exp,
                                      name="%s / %s" % (exp, f),
                                      note="训练断点：删了不能续训，已训好的模型不受影响",
                                      lock=_lock_for(p, prot)))
        prep = [os.path.join(ed, x) for x in RVC_PREP_DIRS
                if os.path.isdir(os.path.join(ed, x))]
        if prep:
            items.append(_mk_item(
                "rvc_ckpt", ed, "prep", parts=prep, exp=exp,
                name="%s / 预处理特征（%d 项）" % (exp, len(prep)),
                note="重训时会自动重新生成，可以放心删",
                lock=_lock_for(ed, prot)))
        bak = os.path.join(ed, "_从零训的旧断点")
        if os.path.isdir(bak):
            items.append(_mk_item("rvc_ckpt", bak, "bak", exp=exp,
                                  name="%s / _从零训的旧断点" % exp,
                                  note="换底模重训时挪走的旧模型，确认新模型能用就可以删",
                                  lock=_lock_for(bak, prot)))
    return sorted(items, key=lambda x: -x["size"])


def _scan_gsv_weights(prot):
    d = GPT_WEIGHT_DIR
    if not os.path.isdir(d):
        return []
    items = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".ckpt"):
            continue
        p = os.path.join(d, f)
        if not os.path.isfile(p):
            continue
        lock = _lock_for(p, prot)
        items.append(_mk_item("gsv_ckpt", p, "ckpt", lock=lock,
                              note="说话模型权重%s" % ("" if lock else "（角色没在用）")))
    return sorted(items, key=lambda x: -x["size"])


def _scan_jobs(prot):
    if not os.path.isdir(JOBS_DIR):
        return []
    items = []
    for jid in sorted(os.listdir(JOBS_DIR)):
        d = os.path.join(JOBS_DIR, jid)
        if not os.path.isdir(d):
            continue
        meta = {}
        try:
            with open(os.path.join(d, "job.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
        st = meta.get("state") or "?"
        title = (meta.get("title") or jid)[:26]
        lock = _lock_for(d, prot)
        work = os.path.join(d, "work")
        if os.path.isdir(work):
            note = ("中间产物：成品早就拷进角色目录了，删了不影响已有成品"
                    if st != "failed" else "失败任务的中间产物，没产出任何成品")
            items.append(_mk_item("job", work, "jobwork",
                                  name="%s · %s [%s]" % (jid, title, st),
                                  note=note, lock=_lock_for(work, prot) or lock))
        elif not lock:
            # 只剩日志的任务目录：很小，但也给个入口（默认不勾）
            it = _mk_item("job", d, "joblog",
                          name="%s · %s [%s]" % (jid, title, st),
                          note="任务日志：删了就看不回当时每一步干了什么")
            it["suggest"] = False
            items.append(it)
    return sorted(items, key=lambda x: -x["size"])


def _scan_uploads(prot):
    if not os.path.isdir(UPLOADS):
        return []
    # 曲库里被引用的不能删：删了条目还在，点播放就是空的
    used = set()
    try:
        for s in load_library().get("songs", []):
            if s.get("path"):
                used.add(_nc(s["path"]))
    except Exception:
        pass
    items = []
    for f in sorted(os.listdir(UPLOADS)):
        p = os.path.join(UPLOADS, f)
        if not os.path.isfile(p):
            continue
        lock = _lock_for(p, prot)
        if _nc(p) in used:
            lock = lock or "曲库里还有这首歌，删了就播不了"
        items.append(_mk_item("upload", p, "upload", lock=lock,
                              note="上传的原片 / 原曲"))
    return sorted(items, key=lambda x: -x["size"])


def _scan_cache(prot):
    if not os.path.isdir(SEP_CACHE):
        return []
    items = []
    for k in sorted(os.listdir(SEP_CACHE)):
        p = os.path.join(SEP_CACHE, k)
        if not os.path.isdir(p):
            continue
        items.append(_mk_item("cache", p, "cache",
                              name="分离缓存 %s" % k[:8],
                              note="下次唱同一首歌能直接复用（重算要 1~2 分钟）",
                              lock=_lock_for(p, prot)))
    return sorted(items, key=lambda x: -x["size"])


def _scan_trash(prot):
    d = os.path.join(DATA, "_trash")
    if not os.path.isdir(d):
        return []
    return sorted(
        [_mk_item("trash", os.path.join(d, f), "trash", note="早先留下的残留")
         for f in sorted(os.listdir(d))],
        key=lambda x: -x["size"])


def _scan_pretrained(prot):
    """底模单独一组：摆出来说明它占多少，但**锁死不给删**。

    用户看到两个几百 MB 的 .pth 一定会想删，而删了以后每次重训都得重新下载
    （还不一定下得动）。与其让他删完再来问"为什么训出来的不跟调"，
    不如一开始就写明白。
    """
    items = []
    seen = set()
    for v in ("v1", "v2"):
        gp, dp = rvc_pretrained_paths(v, "40k")
        for p in (gp, dp):
            if os.path.isfile(p) and _nc(p) not in seen:
                seen.add(_nc(p))
                items.append(_mk_item(
                    "pretrained", p, "pretrained",
                    note="RVC 底模：没有它训出来的模型不会跟旋律",
                    lock="底模不能删（所有歌声模型都从它开始训）"))
    return items


STORAGE_GROUPS = [
    {"id": "rvc_weight", "name": "歌声模型权重", "root": _rvc_weights_dir,
     "desc": "RVC 每 15 轮存一份 55MB，训完再存一份最终的。除了角色正在用的和最近两份，其余都是纯占地方。"},
    {"id": "rvc_ckpt", "name": "训练断点与预处理特征", "root": _rvc_logs_dir,
     "desc": "断点用于续训（删了不能续训，但已训好的模型不受影响）；预处理特征重训时会自动重算。"},
    {"id": "gsv_ckpt", "name": "说话模型权重", "root": lambda: GPT_WEIGHT_DIR,
     "desc": "GPT-SoVITS 每轮一份，一份 155MB。角色正在用的那份会被锁住。"},
    {"id": "job", "name": "任务中间产物", "root": lambda: JOBS_DIR,
     "desc": "每拆一个视频约 250MB。成品早就拷进角色目录了，这里只是过程文件。"},
    {"id": "upload", "name": "上传的原片", "root": lambda: UPLOADS,
     "desc": "原始视频/音频，最大的一项。曲库里还在用的会被锁住。"},
    {"id": "cache", "name": "人声分离缓存", "root": lambda: SEP_CACHE,
     "desc": "换了模型或原曲就失效，清掉只是下次重算一遍（1~2 分钟）。"},
    {"id": "pretrained", "name": "底模（不可删）", "root": lambda: os.path.join(RVC_ROOT, "assets"),
     "desc": "所有歌声模型的起点，锁死。"},
    {"id": "trash", "name": "回收站残留", "root": lambda: os.path.join(DATA, "_trash"),
     "desc": "以前留下的残留文件。"},
]

_SCANNERS = {
    "rvc_weight": _scan_rvc_weights,
    "rvc_ckpt": _scan_rvc_logs,
    "gsv_ckpt": _scan_gsv_weights,
    "job": _scan_jobs,
    "upload": _scan_uploads,
    "cache": _scan_cache,
    "pretrained": _scan_pretrained,
    "trash": _scan_trash,
}


def _storage_allowed_roots():
    """删除白名单的根。不在这里面的一律拒绝 —— 这是安全底线：
    万一前端被改坏或有人手搓请求，也删不到 data/roles 那种地方。"""
    roots = []
    for g in STORAGE_GROUPS:
        try:
            r = os.path.abspath(g["root"]())
        except Exception:
            continue
        if os.path.exists(r):
            roots.append(r)
    return roots


def _dangling_refs():
    """角色记录里指向的权重文件**已经不在**的那些 —— 返回给人看的提示列表。

    为什么会到这一步：文件被手工删了、被上一版有 bug 的清理删了、
    或者整个 GPT-SoVITS 目录挪过位置。共同点是角色卡片依然写着"已训练"，
    一点朗读/唱歌才报"找不到模型" —— 用户只会以为功能坏了。
    这里把它列出来，界面上就能直接说"这个要重训"，而不是让人去猜。
    """
    out = []
    try:
        db = load_roles()
        roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
        roles = roles if isinstance(roles, list) else list((roles or {}).values())
    except Exception as e:
        return ["角色库读不出来，没法检查模型是否还在：%s" % e]
    for r in roles or []:
        nm = r.get("name") or r.get("id")
        rv = r.get("rvc") or {}
        if rv.get("model") and not os.path.isfile(
                os.path.join(_rvc_weights_dir(), rv["model"])):
            out.append("角色「%s」的歌声模型 %s 已经不在磁盘上了，唱歌前需要重训。"
                       % (nm, rv["model"]))
        sv = r.get("seedvc") or {}
        if sv.get("ckpt") and not os.path.isfile(seedvc.ckpt_abs(sv["ckpt"])):
            # 微调档位被删掉时，界面上还写着"用 ft600 唱歌"，点下去才发现唱不了
            # ⇒ 和歌声模型一样，在这里当面说出来（唱歌时会降级回 RVC 并写进日志）。
            out.append("角色「%s」的音色微调档 %s 已经不在磁盘上了，"
                       "可以换一档或重新微调。" % (nm, sv["ckpt"]))
        g = r.get("model") or {}
        if g.get("ckpt"):
            p = os.path.join(GSV_ROOT, str(g["ckpt"]).replace("\\", "/")
                             .replace("/", os.sep))
            if not os.path.isfile(p):
                p2 = os.path.join(GPT_WEIGHT_DIR, os.path.basename(str(g["ckpt"])))
                if not os.path.isfile(p2):
                    out.append("角色「%s」的说话模型 %s 已经不在磁盘上了，朗读前需要重训。"
                               % (nm, g["ckpt"]))
    return out


def scan_storage():
    """扫一遍全部可清理项，返回 {groups, total, reclaimable, free, settings}。

    扫十几个 G 的文件元数据要几秒，所以调用方会做缓存，别每帧都调。
    """
    prot = _protected_map()
    out_groups, total, reclaim = [], 0, 0
    for g in STORAGE_GROUPS:
        fn = _SCANNERS.get(g["id"])
        desc = g.get("desc", "")
        try:
            items = fn(prot) if fn else []
        except Exception as e:
            items = []
            desc += "（扫描失败：%s）" % e
        for it in items:
            # 锁的类型要给到界面：只有 role 锁能被"强制解锁"，
            # 界面得知道哪一格勾上会动到正在用的模型，才好把风险写在那一格上。
            it["lock_kind"] = _lock_kind(it["path"], prot) if it.get("lock") else ""
            total += it.get("size") or 0
            if it.get("safe"):
                reclaim += it.get("size") or 0
        out_groups.append({
            "id": g["id"], "name": g["name"], "desc": desc,
            "size": sum(i.get("size") or 0 for i in items),
            "size_s": _humansize(sum(i.get("size") or 0 for i in items)),
            "count": len(items),
            "items": items,
        })
    free = 0
    try:
        import ctypes
        n = ctypes.c_ulonglong()
        ctypes.windll.kernel32.GetDiskFreeSpaceExW(
            ctypes.c_wchar_p(HERE), None, None, ctypes.byref(n))
        free = n.value
    except Exception:
        pass
    return {
        "at": time.time(),
        "groups": out_groups,
        # 悬空引用：角色记着"我有模型"，但那个文件已经不在磁盘上了。
        # 这种状态最坑人 —— 界面显示已训练，一点就崩。必须在这里喊出来，
        # 否则用户只会以为"唱歌功能坏了"。
        "warnings": _dangling_refs(),
        "total": total,
        "total_s": _humansize(total),
        "reclaimable": reclaim,
        "reclaimable_s": _humansize(reclaim),
        "free": free,
        "free_s": _humansize(free) if free else "",
        "settings": storage_settings(),
    }


def _delete_block_reason(ap, prot, force=False):
    """空串=可删，否则返回不可删的原因。

    force=True 只放行一种锁：kind == "role"（"某个角色正在用这份模型"）。
    用户明确要求"连之前训练的模型一起删"时，这是他的选择 —— 删完角色会
    如实变回"还没训练"，重训一次就好。
    底模（pretrained）和正在跑的任务（job）**任何情况下都不解锁**：
    前者删了要重新下载而本机 huggingface 直连 000，后者删了任务当场崩。
    """
    if not os.path.exists(ap):
        return "已经不在了"
    why = _lock_for(ap, prot)
    if why and not (force and _lock_kind(ap, prot) == "role"):
        return why
    apn = _nc(ap)
    if not any(_under(apn, r) for r in _storage_allowed_roots()):
        return "不在允许清理的目录里"
    return ""


def _rm_path(p):
    if os.path.isdir(p):
        shutil.rmtree(p)
    else:
        os.remove(p)


def _weight_candidates(rel, base):
    """角色记录里的权重名 -> 它可能在磁盘上的全部路径（归一化后的集合）。

    ★ 必须和 _protected_map 里用的是同一套规则，否则会出现
      "锁的时候锁在 A，删完断引用时去比对 B" —— 文件删掉了、引用还留着，
      角色继续显示"已训练"，一点就崩。两处共用这一个函数就不会再走偏。

    之所以要产生多个候选：说话模型记的是带目录的相对路径
    （"GPT_weights_v5turbo/xxx.ckpt"），而歌声模型记的是纯文件名，
    join 的基准目录也不一样。
    """
    out = set()
    if not rel:
        return out
    rel = str(rel).replace("\\", "/")
    if os.path.isabs(rel):
        out.add(_nc(rel))
        return out
    out.add(_nc(os.path.join(base, rel.replace("/", os.sep))))
    out.add(_nc(os.path.join(base, os.path.basename(rel))))
    return out


def _purge_role_refs(paths):
    """删掉模型文件之后，把角色记录里指向它的字段一起断掉。返回的条目会显示给用户。

    ★ 不做这一步的后果很具体（已经发生过一次）：角色卡片照样显示
      "已训练 · 第 15 轮"，朗读/唱歌按钮照样能点，一跑才崩在"找不到模型文件"；
      而用户不会把这个错和自己半小时前在存储面板勾掉的那一格联系起来。
      ⇒ 删了什么就把引用断在哪，界面立刻如实变回"还没训练，去训一次"。
    """
    gone = set(_nc(p) for p in (paths or []) if p)
    if not gone:
        return []
    hit = []

    def _fix(db):
        roles = db["roles"] if isinstance(db, dict) and "roles" in db else db
        for r in roles or []:
            nm = r.get("name") or r.get("id")
            rv = r.get("rvc")
            if isinstance(rv, dict) and rv.get("model") and \
                    (_weight_candidates(rv["model"], _rvc_weights_dir()) & gone):
                rv["model"] = None
                rv["index"] = False
                rv["weight_epoch"] = None
                hit.append("角色「%s」的歌声模型已删除，需要重训才能再唱歌" % nm)
            g = r.get("model")
            if isinstance(g, dict) and g.get("ckpt") and \
                    (_weight_candidates(g["ckpt"], GPT_WEIGHT_DIR) & gone):
                r["model"] = None
                hit.append("角色「%s」的说话模型已删除，需要重训才能再朗读" % nm)
        return db

    try:
        mutate_roles(_fix)
    except Exception as e:
        # 清引用失败不能把"删除"这件事回滚 —— 文件已经没了。
        # 但要留句话，让人知道还有个尾巴没收拾。
        hit.append("（警告：角色记录没能同步更新：%s）" % e)
    return hit


def storage_delete(targets, dry=False, force=False):
    """真删。targets 里每项可以是路径字符串，也可以是 {"path","parts"}。

    返回 {deleted, failed, freed, freed_s, log, refs}。
    force=True 时允许删掉"角色正在用的模型"（见 _delete_block_reason）。

    ★ 不做"移到回收站"：那只是换个地方占着。不可撤销 ⇒ 每一步都先过
      _delete_block_reason，删完写一份清单（纯文本几 KB，真出问题至少
      知道删过什么）。

    ★ 删完必须过一遍 _purge_role_refs：删掉的若是某个角色正在用的权重，
      角色记录里的引用要一起断掉，否则它永远显示"已训练"，
      点下去才崩在找不到模型。
    """
    prot = _protected_map()
    res = {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B", "log": "",
           "refs": []}
    for t in (targets or []):
        ap = os.path.abspath(t["path"] if isinstance(t, dict) else t)
        parts = [os.path.abspath(x) for x in (t.get("parts") or [])] \
            if isinstance(t, dict) else []
        why = _delete_block_reason(ap, prot, force=force)
        if not why and parts:
            for q in parts:
                why = _delete_block_reason(q, prot, force=force)
                if why:
                    why = "%s -> %s" % (os.path.basename(q), why)
                    break
        if why:
            res["failed"].append({"path": ap, "why": why})
            continue
        todo = parts or [ap]
        sz = sum(_path_size(x) for x in todo)
        if dry:
            res["deleted"].append({"path": ap, "size": sz, "dry": True})
            res["freed"] += sz
            continue
        try:
            for q in todo:
                if os.path.exists(q):
                    _rm_path(q)
        except Exception as e:
            res["failed"].append({"path": ap, "why": str(e)})
            continue
        left = [q for q in todo if os.path.exists(q)]
        if left:
            res["failed"].append({"path": ap, "why": "删了但还在（可能被别的程序占用）"})
            continue
        res["deleted"].append({"path": ap, "size": sz})
        res["freed"] += sz
    res["freed_s"] = _humansize(res["freed"])
    if res["deleted"] and not dry:
        # 只拿**确认删掉**的那些路径去断引用：没删成的不能算，
        # 否则会出现"文件还在、角色记录却被清空"这种凭空的数据丢失。
        res["refs"] = _purge_role_refs([d["path"] for d in res["deleted"]])
    if res["deleted"] and not dry:
        try:
            os.makedirs(TRASH_LOG_DIR, exist_ok=True)
            lg = os.path.join(TRASH_LOG_DIR,
                              "deleted_%s.txt" % time.strftime("%Y%m%d_%H%M%S"))
            with open(lg, "w", encoding="utf-8") as f:
                f.write("# VoiceStudio 清理记录 %s\n"
                        % time.strftime("%Y-%m-%d %H:%M:%S"))
                f.write("# 共 %d 项，释放 %s\n"
                        % (len(res["deleted"]), res["freed_s"]))
                for d in res["deleted"]:
                    f.write("%s\t%s\n" % (_humansize(d["size"]), d["path"]))
            res["log"] = lg
        except Exception:
            pass
    return res


def prune_rvc_archives(exp, keep=None, dry=False):
    """训练结束后瘦身：只留最终权重 + 最近 keep 份中间存档。

    为什么必须做：RVC 每 15 轮存一份 55MB，训 300 轮就是 20 份 ≈ 1.1G，
    而且**下一轮续训时它们还赖在那儿**，界面问"训到第几轮"会被带偏
    （已经踩过一次：ckpt 读到 150，权重文件名里最大却是 300）。
    """
    keep = KEEP_RVC_ARCHIVES if keep is None else int(keep)
    d = _rvc_weights_dir()
    if not os.path.isdir(d):
        return {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B"}
    prot = _protected_map()
    arcs = []
    for f in sorted(os.listdir(d)):
        m = re.match(r"^%s_e(\d+)_s(\d+)\.pth$" % re.escape(exp), f)
        if not m:
            continue
        p = os.path.join(d, f)
        if os.path.isfile(p):
            arcs.append((int(m.group(1)), p))
    arcs.sort()
    if keep > 0 and len(arcs) > keep:
        arcs = arcs[:len(arcs) - keep]
    elif keep <= 0:
        pass
    else:
        arcs = []
    # 双保险：正在被角色用的那份，就算它在名单里也不删
    targets = [p for _, p in arcs if _nc(p) not in [_nc(k) for k in prot]]
    return storage_delete(targets, dry=dry)


def prune_gsv_weights(rid, keep=2, dry=False):
    """说话模型瘦身：只留最近 keep 份中间存档（角色正在用的那份永远不删）。

    GPT-SoVITS 每 5 轮存一份 155MB，训 30 轮就 6 份 ≈ 1G。它的 ckpt 名是
    `<rid>-e<轮数>.ckpt`，跟 RVC 那套命名不一样，所以单独写一份。
    """
    d = GPT_WEIGHT_DIR
    if not os.path.isdir(d):
        return {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B"}
    prot = _protected_map()
    arcs = []
    for f in sorted(os.listdir(d)):
        m = re.match(r"^%s-e(\d+)\.ckpt$" % re.escape(rid), f)
        if not m:
            continue
        p = os.path.join(d, f)
        if os.path.isfile(p):
            arcs.append((int(m.group(1)), p))
    arcs.sort()
    if keep > 0 and len(arcs) > keep:
        arcs = arcs[:len(arcs) - keep]
    elif keep > 0:
        arcs = []
    protn = set(_nc(k) for k in prot)
    return storage_delete([p for _, p in arcs if _nc(p) not in protn], dry=dry)


def prune_cache(max_gb=None, dry=False):
    """分离缓存 LRU：超上限就从最旧的开始删。"""
    st = storage_settings()
    lim = float(max_gb if max_gb is not None else st.get("cache_max_gb", CACHE_MAX_GB))
    if not os.path.isdir(SEP_CACHE):
        return {"deleted": [], "failed": [], "freed": 0, "freed_s": "0 B"}
    dirs = [os.path.join(SEP_CACHE, k) for k in os.listdir(SEP_CACHE)
            if os.path.isdir(os.path.join(SEP_CACHE, k))]
    dirs.sort(key=lambda p: os.path.getmtime(p))
    lim_b = lim * 1024.0 ** 3
    total = sum(_path_size(p) for p in dirs)
    targets = []
    for p in dirs:
        if total <= lim_b:
            break
        total -= _path_size(p)
        targets.append(p)
    return storage_delete(targets, dry=dry)
