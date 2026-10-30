# -*- coding: utf-8 -*-
r"""唱前 / 唱后微调（整轨级 DSP，不下载任何模型）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
为什么要有这一层（它跟 hf / demetal / presence 管的事不重叠）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
现有链路已经在管三件事：高频缺口（hf_cli）、电音 HNR（demetal_cli）、
临场感 2~5k（presence_cli）。剩下没人管的，正是老板这一轮点名的三块：

  ★ 底噪 / 低频隆隆  —— 喂给 RVC 之前的干声本来就脏，RVC 会把噪声当成音色
     一起学过去（RVC 社区和 E-SVS 论文都把它列为**唱前必修**）。
  ★ 高音刺耳         —— 升 12 半音后，谐波整排搬进 2~6kHz，正好是耳朵最敏感的
     "刺"区；齿音（4.5~10k）也被一起顶上来。
  ★ 衔接             —— 逐句 / 逐块拼回去的地方会"啪"一下。

GitHub 上这类活的分工（2026-10-26 查）：

    仓库                                 它管什么                    我们怎么用
    timsainb/noisereduce                 谱门降噪（平稳/非平稳）      → denoise 的算法骨架
    wimmerb/singing-quality-enhancement  FullSubNet 专门训唱歌降噪     → 记下：它证明"唱歌要单训"
                                          + 带参考信号的伴奏泄漏去除
    resemble-ai/resemble-enhance         降噪 + 生成式带宽扩展         → 记下：神经方案的天花板
    haoheliu/voicefixer                  一个模型修噪声/混响/低采样/削波 → 记下：削波修复的参照
    henricksmedia/shimmer                ★ 给我们最多可抄数字的那个
                                         —— 每个"修"都配了实测剂量：
                                         齿音：4.5~10kHz，中置全压、两侧半压，最多 −6.5dB
                                         刺耳：2~5kHz 动态 EQ，最多 −3.3dB
                                         闷：200~500Hz 动态 EQ，最多 −4.0dB
                                         → deess / deharsh 的频带和剂量照它定
    OpenTune(ishine)                     共振峰不变地移调              → 记下：治本方案（要声码器），
                                                                        本模块只做"事后柔化"

★ 为什么**不上神经模型**（resemble / voicefixer / FullSubNet）：
  ① 项目里 hf / demetal / presence 三兄弟全是 numpy DSP，跑在非 torch 解释器里，
     加模型就要换解释器 + 下几百 MB 权重；
  ② shimmer 给的数字证明"测量得当的 DSP"已经够用（齿音去掉 64%、代价 0.15 sones）；
  ③ 最重要的是：**DSP 能一块一块量给你看**，神经模型不能 —— 而这项目的铁律是"先量后改"。
  ⇒ 神经方案写进文件头当"将来想再往上走时的路"，不进代码。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
第二轮调研（2026-10-26 晚）：老板要"提升高音听感" + "边调边听"
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
高音听着假，根子往往**不是**齿音也不是刺 —— 是**共振峰被一起搬走了**：
  升调时基频和谐波整排上移，共振峰（喉咙和口腔的固定共振，人靠它认出"是谁"）
  也跟着上去 ⇒ 声音变小、变尖、发假（"捏着嗓子"/"花栗鼠"）。
  变调插件源码复刻那篇工程文把它讲透了："变调做完后如果只把基频和包络一起搬了，
  人声听起来就像换了个人；把包络峰值留在原位或单独搬，才是'同一个人的不同音高'。"

可抄的做法（同一篇 + jurihock/stftpitchshift 的 cepstral formant preservation）：
  ★ **频谱包络搬移**：log 幅度谱 → 移动平均提包络 → 把包络在频率轴上按 β 搬 →
    幅度谱 = exp(log幅度 − 原包络 + 搬后的包络)。谐波细节原样留着，只换包络位置。
  参数（人家实测给的，不是我们拍的）：
    bw（包络平滑带宽）  说话 300~500Hz，音乐可放到 800Hz ⇒ 取 600Hz 折中
    搬移区间            **20Hz~8kHz**，8k 以上不动 —— 那篇原话：
                        "低频分辨率不够，直接在高频叠加齿音会炸"，8k 以上做衰减保护
    β                   β<1 共振峰往低频搬（身材变大）／β>1 往高频搬（身材变小）
  商业级会用倒谱或 LPC 提包络；移动平均是轻量近似，但对本模块"能逐级量"的诉求够用。

★ β 怎么定 —— **测出来，不猜**（这是本项目一贯的路子）：
  拿高音帧的包络质心跟**低音帧**的比。低音段更接近原声，共振峰没被抬多少，
  拿它当基线（跟 belt 那一步取低音帧当基线是同一个道理 —— 跟自己比永远是 0）。
  全曲都在高音、没有低音帧 ⇒ 量不出来 ⇒ 照实不修，不硬编一个数。

"边调边听"的交互范式（GitHub 上人声工作站类项目的通行做法）：
  Tri-State A/B Audition（原声 / 处理后 / 整首混音 三态即时切换）
  + 0~100% 干湿混合滑条 + 分步 solo（只听链里某一步）
  ⇒ 落在本项目就是 `audition` 子命令 + 前端干湿交叉淡化（见 cmd_audition）。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
一条铁律：先量 → 修 → 复测 → 没改善就回滚，而且**不写那个 wav**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
跟 hf_cli / presence_cli 同一个约定：SKIP 时子进程**不写 wav**，调用方不许拿它当成品
（前辈踩过：拿了不存在的路径去混音，读空文件）。

每条"修"都配一对数：
    gain = 该治的指标真的好转了多少            （必须达标）
    harm = 不该碰的东西被动了多少              （必须没动）
两个都满足才落盘，否则退回上一版，并把"试了但没成"写进报告。

用法（cwd 随意，只要 numpy + scipy + soundfile）
------------------------------------------------
    python polish.py measure <wav> <out.json>
    python polish.py pre    <wav> <out.wav> <out.json> [--amount 0.8] [--skip denoise]
    python polish.py post   <wav> <out.wav> <out.json> [--amount 0.8] [--skip deess]
    python polish.py concat <out.wav> <a.wav> <b.wav> ... [--xfade 12]

stdout 只打一行机器可读结果（POLISH_*），跟 hf_cli 同约定。
"""

import contextlib
import json
import math
import os
import shutil
import sys

import numpy as np
import soundfile as sf
from scipy.ndimage import median_filter, uniform_filter1d
from scipy.signal import butter, istft, sosfiltfilt, stft

# ---------------------------------------------------------------------------
# 常量：每一个都写了"为什么是这个数"
# ---------------------------------------------------------------------------

NFFT = 1024                 # 46ms @44.1k：够细到能抓单个齿音，又不至于把谐波切碎
HOP = 256                   # 5.8ms 一帧，齿音的起落跟得上

# ---- 唱前 ----
HP_FC = 80.0                # 高通截止。RVC 社区一致口径是 60~100Hz：
                            # 切掉隆隆/直流/喷麦，防 f0 被低频拖偏
HP_ORDER = 2                # 2 阶够陡，4 阶在 80Hz 附近会有可闻的相位 ringing

QUIET_PCT = 10.0            # 拿最安静的 10% 帧当"噪声样本"。5% 太少会挑到个别奇点，
                            # 20% 会混进真唱的弱唱段 ⇒ 门限偏高、降不动
NOISE_MULT = 1.6            # 噪声样本 ×1.6（≈+4dB）当门限。1.0 会把谐波底噪一起啃掉
                            # 出"音乐噪声"，2.5 以上就基本不降了
DENOISE_MAX = 0.85          # prop_decrease 上限。1.0 = 门限以下全抹成 0 ⇒ 明显的
                            # "水下声"。留 15% 是 Audacity 那套做法的默认值附近

CLIP_THR = 0.985            # 判定"被削平了"的阈值（接近满刻度）
CLIP_MAX_RUN = 0.05         # 超过 50ms 的连续削波整段都废了，不修（修了也是假的）

# ---------------------------------------------------------------------------
# ★★ 2026-10-26 二修：唱前 declick 的判据整个换掉了。旧判据（"孤立陡跳"）
#    经实测是**方向就错**的，留下的唯一价值是当反面教材。病灶（取证脚本
#    tools/_clickprobe*.py，结论写死在 verify_declick.py 的判据里）：
#
#    ① 旧判据三条同时成立时对注入的真单采样脉冲召回 **0/30**，而只取前两条
#       是 30/30 —— 因为真脉冲必然产生**两个相邻**的大跳变（上去 + 下来），
#       而第②③条要求"比前/后一个采样间差大 3 倍"，两条同时成立时恰好把
#       真脉冲自己排除了。②③ 本是加来排"谐波陡坡"的，结构上和目标冲突。
#    ② 它实际报出的点全在辅音里：同一素材报 37 个/45秒，这些点的中值残差
#       中位数是 **0**，±5ms 内同量级点 **167 个**（真脉冲是 0）。
#
#    新判据换了个物理量 —— 不看"变化多大"（辅音本来就变化大，尺子架在被测物
#    上），看"**背景是不是可预测的**"：
#       dev   = |x − med7(x)|        7 点中值残差。中值滤波会把**孤立**尖峰整个
#                                    抹掉；窗口 7 能扛住 3 个连续异常（floor((W−1)/2)），
#                                    所以 1~3 采样的脉冲都留得下（med3 只能扛 1 个，
#                                    实测 2~3 采样召回 0/30）。
#       rough = 局部"线性外推预测不了的那部分"的 RMS（±12ms 内二阶差分之半，
#               取中值 ⇒ 不受孤立尖峰自己污染）。元音周期性强 ⇒ 总能量大但
#               rough 小；辅音是噪声 ⇒ 两者接近。
#    判据 dev / rough > CLICK_REL_ROUGH，再加上**段宽 ≤3 采样**和**±5ms 内
#    没有别的段**。两条实测（两份不同真实素材，45 秒）：
#       报点 13 / 8 个（2.9 / 1.8 个每 10 秒）
#       掉>3dB 的帧 0.00% / 0.02%，最狠一帧 −2.33 / −3.80 dB（旧判据 −6.20）
#       注入真脉冲召回 0.5*peak 27/30 与 26/30，0.8*peak 29/30 与 29/30
#       （旧判据一律 0/30）
#    ★ 分母从 local_rms 换成 rough 是关键：用总能量当分母，响的元音段里
#      真爆音会被压下去（召回只剩 22/30），因为元音能量大；rough 只看
#      "不可预测的那部分"，元音里它很小 ⇒ 爆音照样突出。
# ---------------------------------------------------------------------------
CLICK_MED_WIN = 7           # 中值滤波窗口。=7 才能扛住 3 采样的宽脉冲
CLICK_REL_ROUGH = 32.0      # dev / rough 的门限。见上面两张实测表
CLICK_WD_MAX = 3            # 段宽上限（采样）。真爆音 1~3 采样；辅音是一整片
CLICK_NB_MS = 5.0           # 孤立度窗口：±5ms 内不许有别的段
CLICK_ABS_REL = 0.002       # 绝对地板：|dev| 得大于 peak 的 0.2%（≈ −54dBFS），
                            # 免得在静音段拿几乎为零的 rough 当分母报出一堆鬼
# ★ 试过又**撤回**的一条：加"dev > 0.5×peak"的绝对门限当"或"分支（想让有底噪的
#   素材也能抓到）。撤回的原因记在这里，免得有人再捡起来：
#     素材甲（peak .954）：dev 最大 0.4674，dev>0.5×peak 的点 **0 个**（很安全）
#     素材乙（peak .808）：dev 最大 **1.2588**，dev>0.5×peak 的点 **1049 个**
#   ⇒ 同一个"相对 peak"的门限，在两份真实素材上差了三个数量级。peak 不是个
#     稳健的尺度（素材乙里有比它自己峰值还大的采样间跳变）。绝对门限一律别用。
CLICK_HEAL_MS = 0.1         # 修复窗 ±0.1ms（≈ ±4 采样）。窄窗扫描见 git 历史，
                            # ±1.5ms 时最狠帧 −39.7dB，±0.1ms 时 −2.3dB
CLICK_K = 8.0               # ★ 只留给唱后 splice 检测用（见 _count_splices）
CLICK_WIN_MS = 12.0         # ★ 同上

# ★ 密度闸门：真爆音在一首歌里是**零星几个**，不可能几百个。
#   旧判据在干净素材上能报 292 个/200秒 ⇒ 报得越多越说明抓的不是爆音。
#   新判据实测干净素材只有 1.8~2.9 个/10秒，留 2 倍余量取 6.0。
#   超过就整步撤销并把理由写进报告（不猜、不硬修）。
CLICK_MAX_PER10S = 6.0

PRE_TARGET_DBFS = -3.0      # 唱前电平目标。RVC 吃 −6~−3dBFS 最稳（太大进去就过载）

SIG_LO, SIG_HI = 300.0, 1500.0     # "人声本体"的测量带。★ 唱前的每一步都用
                                   # 它当"不该被动"的那把尺子 —— 不能拿整轨 RMS：
                                   # 降噪本来就会让整轨变轻（噪声被拿走了，
                                   # 那是**收益**不是损失），拿 RMS 当 harm 会
                                   # 逼得降噪一步都不敢动（实测 loud 掉 1.2dB 就判红）。
                                   # 300~1500 是基频和头几个泛音的地盘，信噪比最高，
                                   # 它掉了才说明真把人声啃了。

# ★★ "咬字"（清辅音 s/sh/t/ch）的测量带。2026-10-26 才加，为什么非加不可：
#   以前**唯一的**内容护栏是上面的 300~1500Hz —— 而咬字住在 2~8kHz，
#   尺子压根不在被测物上 ⇒ 任何把辅音抹平的步骤，自动判据全部看不出来，
#   一路绿灯通过（declick 就是这样连 moderation 三遍都没人拦）。
#   老板的原话："丢词、丢音，甚至有点变调" —— 量出来正是这一带被啃。
# ⇒ 现在它是"内容不能被啃"的**第二条**硬指标，见 measure_pre.cons_db。
CONS_LO, CONS_HI = 2000.0, 8000.0
CONTENT_MAX_DROP_DB = 0.5   # 咬字/本体最多许掉这么多 dB；超了整步撤销。
                            # 为什么是 0.5： Aus: 人耳对稳定的宽带能量变化大约能
                            # 听出 1dB；0.5 是"留了一半余量"的取法。

# ---- 唱后 ----
DEESS_LO, DEESS_HI = 4500.0, 10000.0    # 齿音频带，照 shimmer（它实测 4.5~9.5k 才是
                                        # 辅音的"s/sh/t/ch"，再往上是镲片）
DEESS_MAX_DB = 6.5                      # 最多压 6.5dB，照 shimmer 实测上限
DEESS_THRESH_DB = 3.0                   # 超出"这首歌的平时平衡" 3dB 才动手
                                        # （shimmer 的血泪：只按亮度判会把 hi-hat 当齿音，
                                        #  必须相对**自己**的基线）
DEESS_SIDE_RATIO = 0.5                  # 两侧只压中间的一半（shimmer 实测做法）

HARSH_LO, HARSH_HI = 2000.0, 5000.0     # 刺耳带，照 shimmer
HARSH_MAX_DB = 3.3
HARSH_THRESH_DB = 2.5
HARSH_REF_LO, HARSH_REF_HI = 300.0, 1500.0   # 相对基准：低中频。用"相对"是因为
                                            # 整段变响不该触发 EQ

MUD_LO, MUD_HI = 200.0, 500.0           # 闷。★ 默认**关**：那一带贴着共振峰，
MUD_MAX_DB = 4.0                        # 削多了会离角色更远（presence_cli 里用数据
                                        # 否掉过同一件事，别再踩）

BELT_LO, BELT_HI = 2500.0, 6000.0       # 高音"刺"的落点：升调后谐波整排搬到这里
BELT_MAX_DB = 4.0
BELT_F0_LO, BELT_F0_HI = 380.0, 700.0   # f0 380Hz 以下不算高音，700Hz 以上算满
                                        # （G4≈392 / F5≈698，人声真声高音区）
BELT_REF_LO, BELT_REF_HI = 300.0, 2500.0

# 8~16k 只**量不修**：界面上显示"空气感 −38dB"让用户知道这条成品有多闷，
# 真要补走 hf_cli（它搬的是原唱的真高频）。这里没有对应的修法，别再找了。
AIR_LO, AIR_HI = 8000.0, 16000.0

# ---- 共振峰修正（第二轮加）--------------------------------------------------
# 频谱包络搬移：log 幅度谱 → 移动平均提包络 → 包络沿频率轴搬 β → 换回去。
# 谐波的**精细结构原样保留**，只换包络（共振峰）的位置。
FORMANT_BW = 600.0              # 包络平滑带宽(Hz)：说话 300~500、音乐可到 800 ⇒ 取折中
FORMANT_LO, FORMANT_HI = 20.0, 8000.0   # ★ 搬移只在这一段生效。8k 以上不动 ——
                                #   那篇工程文原话：低频分辨率不够，在高频叠加齿音会炸。
                                #   而且 4.5~10k 是 deess 的地盘，别互相踩。
FORMANT_MAX_SEMI = 12.0         # 最多补一个八度。再狠就不是"修"了，是换一个人

# ★★ formant **只做手动**，不进自动决策。为什么，用实测说话：
#
#  A. 自动判据做不出来（两版度量都被"尺子自检"证伪，已删）：
#     见下面"两把坏尺子"。
#  B. 搬移本身**对，但有音域边界**。用构造真值验的（共振峰真值已知，
#     看搬完之后各次谐波的幅度更贴合"搬回来了"还是"没搬"）：
#        基频 160 升 5 → f0 214  贴合'搬回' 0.955 vs '没搬' 0.936   OK
#        基频 200 升 5 → f0 267          0.974 vs 0.938             OK
#        基频 250 升 5 → f0 334          0.938 vs 0.899             OK
#        基频 160/200/250 升 7            全部 OK（方向一致）
#        基频 250 升 12 → f0 500         0.866 vs 0.900             ✗ 失效
#     失效率的那一行不是 bug，是**物理极限**：f0=500 时谐波间隔就是 500Hz，
#     跟 F1/F2 的间距同量级 ⇒ 共振峰根本估不出来（倒谱法同样卡在这里，
#     提升阶数会被谐波峰污染）。中低音域（f0 ≲ 400）可用，再高就别指望。
#  ⇒ 所以它只有手动档：用户给半音数，我们用耳朵（试听）验收。
#
# ★★ 两把坏尺子（都删了，别再捡）：
#  ① 包络质心（高低音帧之比 / 绝对质心）
#     证伪：升 12 半音的素材，质心只走了 1.81 半音；升 5 个走了 1.93；升 7 个走了
#     1.22 —— 完全不跟着共振峰走。原因：质心被**声源的频谱倾斜**（谐波 1/k² 那类
#     整体下坡）牵着走，而倾斜是声源属性、不是共振峰。
#  ② 对数频率轴上的包络对齐（去趋势后互相关，找最佳平移半音数）
#     证伪：升 12 半音只量出 0.25。原因更根本 —— **包络是从稀疏的谐波上估出来的**：
#     唱歌 f0 200~500Hz ⇒ 谐波间隔就是 200~500Hz，跟 600Hz 的平滑窗同量级，
#     一个窗里只落进 1~2 根谐波 ⇒ 估出来的"包络"其实是谐波的采样本身。
#     基频一变，采样点全变，两条包络就对不上了 —— 搬移明明生效了，尺子量不出。
#  ⇒ 结论：**这一步没有可靠的自动判据**，所以不进自动决策（见 cmd_post 里的处理）。
#     它改好不好听，只能由**耳朵**定 —— 而这正是"试听"存在的理由。

LIMIT_KNEE = 0.89           # −1dBFS 开始软拐。硬限会"咔"，tanh 软拐听不出来
LIMIT_CEIL = 0.985

SPLICE_K = 6.0              # 拼接点判定：相邻差 > 局部中位数 ×6 且是**孤立**的
                            # （比爆音松一点：拼接的台阶往往没那么尖）
SPLICE_WIN_MS = 12.0
# ★ 从 ±3ms 收到 ±0.5ms，理由和上面 declick 那条完全一样（同一套判据、
#   同一套插值技巧）：台阶得抬走几个采样即可，不需要抹平 ±132 个采样。
#   留得比 declick 宽一点：拼接的错位有时会横跨好几个采样。
HEAL_MS = 0.5
# 密度闸门，同上。给得比爆音宽：多角色拼接会**真的**产生不少接缝
# （一首 40 句的歌 ≈ 每 10 秒 1.5 个），但也不会到上百个。
SPLICE_MAX_PER10S = 6.0

# ★ 顺序有讲究：**先把"波形坏了的地方"补好，再去噪**。
#   实测把 declip 排在 denoise 后面，denoise 的谱门自己就把峰值压下来了
#   （0.995 → 0.90），clip_ratio 变成 0 ⇒ declip 判"没得修"直接跳过，
#   等于削波从来没被真正修过，只是被降噪顺手盖住了。
PRE_STEPS = ("declip", "declick", "hp", "denoise", "level")

# ★★ 唱前**默认只保命**：这一版之前，唱前链条默认是"全都试一遍"，
#   实测把歌啃到"丢词、丢音、还有点变调"（详见 verify_declick 文件头）。
#   现在改成三档，界面（大厅下拉 / 角色页逐步勾选）都从这里取默认值，
#   **不许前端另抄一份** —— 抄漏一步 = 那一步永远关不掉，没人会发现。
#
#   为什么"保命档"里只留 declip：
#     · declip 修的是**削波**（波形被压平），有明确的物理特征，修完能当场核对
#       （verify_declick 第 5 条：注入削波 → clip_ratio 22.15% → 1.25%，
#        MSE 降 5 倍）。它配得上"自动"。
#     · declick 那条判据不可信：45 秒干净素材报 292 个点（全是辅音），
#       注入 30 个真爆音反而 0/30。已经伤过一次，所以它不再参与自动，
#       要开由你自己开。
#     · hp（去隆隆）/ denoise（去底噪）在**实测**里对内容几乎没伤害
#       （0%），但它们改的是"好不好听"而不是"坏没坏"——那是耳朵的事，
#       不该替用户决定 ⇒ 默认交给开关。
# ★ "guard" = 只修**会导致破音**的那两样：削波 + 爆音（咔哒/毛刺）。
#   2026-10-26 二修后才敢把 declick 放进 guard —— 判据换掉之前它对真脉冲
#   召回 0/30（抓的全是辅音），放进默认档等于拿用户的咬字冒险。现在实测
#   召回 26~29/30、两份真实素材上掉>3dB 的帧只有 0.00~0.02%，才配进默认档。
PRE_MODE_STEPS = {
    "guard": ("declip", "declick"),                   # 只修会导致破音的东西
    "deep": ("declip", "declick", "hp", "denoise"),   # 全开（2026-10-26 之前的样子）
    "off": (),                                        # 唱前完全不动手
}
PRE_MODE_DEFAULT = "guard"

PRE_STEP_LABEL = {
    "declip": "修削波（防破音）",
    "declick": "修爆音/咔哒（防破音）",
    "hp": "去低频隆隆",
    "denoise": "去底噪",
    "level": "统一音量",
}


def pre_skip_for(mode=None, ui_skip=None):
    """把「档位」和「界面逐步勾选」合成真正的 skip 列表。

    ★ level **永远**排除：唱前干声的电平是后面混音那一步调的，这里改了等于把
      "整首歌的音量"悄悄挪一次（而且它会给整轨乘一个常数，让"每档都掉同样
      3dB"冒充成"内容被啃了"，量度量的第一版就是这么被骗的）。
    ★ ui_skip 不是 None 时以**界面为准**（界面已经逐项表达过意愿了），
      否则才有档位兜底 —— 否则会出现"用户勾开了某一步、后端却按档位跳过"
      这种点了没反应的鬼事。
    """
    skip = set()
    if ui_skip is None:
        m = str(mode or PRE_MODE_DEFAULT).strip().lower()
        keep = set(PRE_MODE_STEPS.get(m, PRE_MODE_STEPS[PRE_MODE_DEFAULT]))
        skip |= set(PRE_STEPS) - keep
    else:
        skip |= set(ui_skip)
    skip.add("level")
    skip &= set(PRE_STEPS)          # 白名单：拼错的名字一律忽略，不许偷偷跑起来
    return tuple(sorted(skip))
# ★ formant 排最前：它改的是"音色本体"（共振峰位置），后面 deess / deharsh / belt
#   都是在**已经摆正的音色**上再判"哪里冒出来了"。反过来的话，deess 会在一个
#   被抬上去的音色上修齿音，等于先修了错的那个。
POST_STEPS = ("formant", "deess", "deharsh", "belt", "heal", "limit")

# ★ 「补空气感（8~16k 搁架）」写了一半又**删掉了**，理由记在这里免得有人再捡起来：
#   ① 真正的高频缺口（RVC 在 5.7k 以上没有可信内容）已经由 hf_cli 管了 ——
#      它是把**原唱轨的真高频搬过来**，是真实内容，不是凭空抬；
#   ② 没有参照的"自己抬搁架"，能抬的东西只有本来就存在的那点高频 ——
#      而成品在那一段基本只剩底噪 ⇒ 带门就一步都动不了，不带门就是**放大嘶嘶声**
#      （实测：合成素材上缺口 21.4dB，带门的版本抬了 0.00dB）；
#   ③ 于是它只在"成品本来就有高频、只是少一点"时才有用 —— 那是极窄的一条缝，
#      为它留一个旋钮不值当（铁律 21：定义了没人用的东西，第一选择是删）。


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _db(v):
    return 10.0 * np.log10(max(float(v), 1e-20))


def _read(path):
    x, sr = sf.read(path, dtype="float32", always_2d=False)
    return np.asarray(x, dtype=np.float64), int(sr)


def _write(path, x, sr):
    sf.write(path, np.asarray(x, dtype=np.float32), int(sr), subtype="PCM_16")


def _dump(path, obj):
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False)
    except Exception as e:                                   # noqa: BLE001
        sys.stderr.write("POLISH 报告写不了：%s\n" % e)


def _mono(x):
    return x if x.ndim == 1 else x.mean(axis=1)


def _map1(x, fn):
    """把单声道函数 fn 映射到每个声道。

    ★★ 这里必须 copy，不能图省事写 np.asarray —— 2026-10-26 的血案：
      np.asarray(x, dtype=np.float64) 在 x **本来就是** float64 ndarray 时
      **不复制**，返回的是同一块内存。于是 declick / heal_splices 这类
      "在参数上原地插值"的函数会连调用方那份数组一起改掉。

      后果不是"多改了一点"，而是完全失控：
        ① `_ladder` 会试三档剂量（1.0 / 0.7 / 0.45），每调一次 fn 就在**同一个数组**
           上再糊一层 ⇒ 三步叠加，等于每步都按最猛剂量连做三遍；
        ② cmd_pre / cmd_post 里 judge 不通过时会写 "kept: False / ✗ 没采纳"，
           可底层数组早就被改了 ⇒ **报告全程撒谎**："这一步没生效"是假的。
      实测（真实分离人声 20 秒）：declick 让传入数组最大变化 1.67，heal_splices
      1.52 —— 原 wav 的 peak 才 0.89。这就是老板说的"同一个模型同一首歌，
      现在唱下来丢词、丢音、还有点变调"。
      取证脚本：tools/_inplace_probe.py（先把这段回归报警）。
    """
    arr = np.array(x, dtype=np.float64, copy=True)
    if arr.ndim == 1:
        return fn(arr)
    out = np.empty_like(arr)
    for c in range(arr.shape[1]):
        out[:, c] = fn(np.ascontiguousarray(arr[:, c]))
    return out


WIN = "hann_periodic"      # ★ 必须是 periodic：只有它满足 COLA，
                            #   hop=n/4 时能无损重建（默认的 'hann' 在 scipy 1.18
                            #   已经改名，且对称窗重建会有 1 帧的误差）


def _stft(v, sr, n=NFFT, hop=HOP):
    f, t, Z = stft(v, fs=float(sr), window=WIN, nperseg=n,
                   noverlap=n - hop, boundary="zeros", padded=True)
    return f, t, Z


def _istft(Z, sr, n=NFFT, hop=HOP, length=None):
    # ★ scipy 1.18 的 istft 没有 input_length 了，只能自己把两端补的
    #   nperseg//2 剪掉（stft 用了 boundary="zeros"+padded，所以各补了一半）。
    _, y = istft(Z, fs=float(sr), window=WIN, nperseg=n,
                 noverlap=n - hop, boundary=True)
    if length is None:
        return y
    pad = n // 2
    if len(y) >= length + pad:
        return y[pad:pad + length]
    if len(y) >= length:
        return y[:length]
    return np.concatenate([y, np.zeros(length - len(y))])


def _band_db(Z, f, lo, hi):
    m = (f >= lo) & (f < hi)
    if not m.any():
        return None
    e = (np.abs(Z[m, :]) ** 2).mean(axis=0)
    return 10.0 * np.log10(e + 1e-20)


def _frames_rms_db(v, sr, win_ms=50.0):
    """分帧 RMS（dB）。用 cumsum，长文件也不慢。"""
    v = np.asarray(_mono(v), dtype=np.float64)
    w = max(1, int(sr * win_ms / 1000.0))
    n = len(v)
    if n < w:
        return np.array([_db(float(np.sqrt((v ** 2).mean())))])
    c = np.cumsum(np.concatenate([[0.0], v ** 2]))
    r = np.sqrt(np.maximum(c[w:] - c[:-w], 0.0) / float(w))
    return 10.0 * np.log10(r + 1e-20)


def _rms_db(x):
    return _db(float(np.sqrt((np.asarray(x) ** 2).mean())))


def _peak(x):
    x = np.asarray(x)
    return float(np.max(np.abs(x))) if x.size else 0.0


def _sec_frames(sr, sec):
    return max(3, int(round(sec * float(sr) / HOP)))


def _excess(db, win_frames):
    """相对自己滑动中位线的超出量（dB）。

    ★ 为什么用"自己的中位线"而不是固定阈值：整首歌的亮度本来就在变，
      固定阈值会把"本来就亮的那句"整句压掉（shimmer 用亮度判齿音失败就是栽在这）。
    """
    return db - median_filter(db, size=(max(3, int(win_frames) | 1)))


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 量
# ---------------------------------------------------------------------------

def _clip_ratio(x):
    a = np.abs(np.asarray(x))
    if a.size == 0:
        return 0.0
    return float((a >= CLIP_THR).mean())


def _diff_outliers(v, sr, k, win_ms):
    """返回疑似"跳变点"的采样下标。

    ★ 判据是"**孤立的**陡跳"，三条同时满足才算：
        ① 比局部中位陡度大 k 倍
        ② 比**前一个**采样间差大 3 倍
        ③ 比**后一个**采样间差大 3 倍
      ②③ 是关键。第一版只写了 ①，实测把谐波的陡坡整片算成拼接点
      （合成素材上 4800/176400 个采样超标）⇒ 真正的接缝反而被淹没在噪声里，
      一条都没抓到。拼接点是**一个采样**的台阶，它的左右邻居都很平。
    """
    v = np.asarray(v, dtype=np.float64)
    n = len(v)
    if n < 16:
        return np.zeros(0, dtype=np.int64)
    d = np.abs(np.diff(v))
    w = max(3, int(sr * win_ms / 1000.0))
    loc = np.maximum(median_filter(d, size=(2 * w + 1)), 1e-9)
    dn = np.empty_like(d)
    dn[1:] = d[:-1]
    dn[0] = d[0]
    dp = np.empty_like(d)
    dp[:-1] = d[1:]
    dp[-1] = d[-1]
    floor = 0.01 * float(np.max(np.abs(v)) + 1e-9)
    bad = (d > loc * k) & (d > 3.0 * dn) & (d > 3.0 * dp) & (d > floor)
    return np.flatnonzero(bad) + 1


def _rough(v, sr, win_ms=CLICK_WIN_MS):
    """局部"不可预测度"：二阶差分之半的局部 RMS。

    ★ 用**中值**滤波做平滑，这样"被测的那个尖峰自己"不会把分母撑大
      （中值不看极值）。这是 dev/rough 能站住的前提。
    """
    n = len(v)
    if n < 3:
        return np.full(n, max(1e-12, float(np.max(np.abs(v)))))
    d2 = v[1:-1] - 0.5 * (v[:-2] + v[2:])
    d2 = np.concatenate(([d2[0]], d2, [d2[-1]]))
    w = max(3, int(sr * win_ms / 1000.0))
    return np.sqrt(np.maximum(median_filter(d2 ** 2, size=(2 * w + 1)), 1e-24))


def _spike_segments(v, sr, rel_thr=CLICK_REL_ROUGH, wd_max=CLICK_WD_MAX,
                    nb_ms=CLICK_NB_MS, abs_rel=CLICK_ABS_REL):
    """找"孤立尖峰"，返回 [(lo, hi)] 半开采样区间。

    三步（详见常量区那张 2026-10-26 二修的注释）：
      ① dev = |x − med_{CLICK_MED_WIN}(x)|      —— 中值滤波抹得掉孤立尖峰，
         抹不掉成片的辅音；窗口 7 才能扛住 3 采样宽的脉冲。
      ② dev / rough > rel_thr                   —— rough 是"线性外推预测不了的
         那部分"。元音总能量大但 rough 小 ⇒ 响段里的爆音照样突出；辅音两者
         接近 ⇒ 被压住。（分母若用总能量 RMS，响段召回会从 27/30 掉到 22/30）
      ③ 按**连通段**过滤：段宽 ≤ wd_max，且 ±nb_ms 内没有别的段。
         ★ 必须按段、不能按点：按点算"±5ms 内同量级点数 ≤1"会把 2~3 采样的
         宽脉冲自己排除掉（2026-10-26 踩过，召回直接归零）。
    """
    v = np.asarray(v, dtype=np.float64)
    n = len(v)
    if n < 64:
        return []
    mw = int(CLICK_MED_WIN)
    h = mw // 2
    sm = v.copy()
    sm[h:n - h] = median_filter(v, size=mw)[h:n - h]
    dev = np.abs(v - sm)
    rough = _rough(v, sr)
    gpk = float(np.max(np.abs(v))) + 1e-12
    rough = np.maximum(rough, gpk * 1e-6)          # 防除零
    mask = ((dev / rough) > rel_thr) & (dev > gpk * abs_rel)
    if not mask.any():
        return []
    d = np.diff(mask.astype(np.int8))
    starts = list(np.flatnonzero(d == 1) + 1)
    ends = list(np.flatnonzero(d == -1) + 1)
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(n)
    segs = list(zip(starts, ends))
    nb = max(1, int(sr * nb_ms / 1000.0))
    out = []
    for k, (lo, hi) in enumerate(segs):
        if hi - lo > wd_max:
            continue
        if k > 0 and lo - segs[k - 1][1] < nb:
            continue
        if k + 1 < len(segs) and segs[k + 1][0] - hi < nb:
            continue
        out.append((int(lo), int(hi)))
    return out


def _count_clicks(x, sr):
    """疑似爆音的**个数**（按段算，不是按采样算 —— 一段就是一个咔哒）。"""
    return int(len(_spike_segments(_mono(x), sr)))


def _count_splices(x, sr):
    # ★ 唱后"拼接点"仍用旧判据：拼接点是**台阶**（从左边界一直错到段末），
    #   物理特征和"孤立尖峰"不是一回事，别顺手一起换。
    return int(_diff_outliers(_mono(x), sr, SPLICE_K, SPLICE_WIN_MS).size)


def measure_pre(x, sr):
    v = _mono(np.asarray(x, dtype=np.float64))
    fr = _frames_rms_db(v, sr, 50.0)
    floor_db = float(np.percentile(fr, QUIET_PCT))
    loud_db = float(np.percentile(fr, 90.0))
    rumble = None
    sig_db = None
    Z = None
    if len(v) > NFFT * 2:
        f, t, Z = _stft(v, sr)
        rumble = float(np.mean(_band_db(Z, f, 20.0, 80.0)))
        m = (f >= SIG_LO) & (f < SIG_HI)
        if m.any():
            # ★ 量"该频带里**最强的那一根**"（= 基频/强泛音），不是整带平均：
            #   整带平均会被"谐波之间那些空格子"稀释 —— 降噪把空格子压下去，
            #   平均值就掉一大截，可那是**噪声该去的地方**，不是人声被啃了。
            #   实测同一段：平均法量出 −1.79dB，峰值法量出 −1.12dB，
            #   而理论上把噪声全去掉也只该掉 0.13dB ⇒ 平均法在虚报。
            # ★ 挑帧也只按"这一带自己的响度"排第 90 百分位，不按整轨能量：
            #   按整轨挑的话，去掉隆隆声之后"哪 50% 帧算响的"会换一批，
            #   于是同一段人声量出 +1.19dB 的变化 —— 那是指标在漂，不是音频变了。
            pw = np.abs(Z[m, :]) ** 2
            pk = 10.0 * np.log10(pw.max(axis=0) + 1e-20)
            sig_db = float(np.percentile(pk, 90.0))

    def _peak_db(Zz, ff, lo, hi):
        """带内**最强那一根**的 p90 —— 与 sig_db 完全同口径。

        ★ 为什么取"最强那一根"而不是整带平均：理由就写在上面 sig_db 那段注释里。
          简单说：整带平均会被"谐波之间的空格子"稀释，空格子里全是噪声，
          压噪声会把平均值拉低一大截 ⇒ **虚报**成人声被啃了。
        """
        mm = (ff >= lo) & (ff < hi)
        if not mm.any():
            return None
        pw = np.abs(Zz[mm, :]) ** 2
        pk = 10.0 * np.log10(pw.max(axis=0) + 1e-20)
        return round(float(np.percentile(pk, 90.0)), 2)

    return {
        "floor_db": round(floor_db, 2),
        "loud_db": round(loud_db, 2),
        "snr_db": round(loud_db - floor_db, 2),
        "rumble_db": round(rumble, 2) if rumble is not None else None,
        "sig_db": round(sig_db, 2) if sig_db is not None else None,
        "cons_db": _peak_db(Z, f, CONS_LO, CONS_HI) if Z is not None else None,
        "clip_ratio": round(_clip_ratio(x), 5),
        "clicks": _count_clicks(x, sr),
        "peak": round(_peak(x), 4),
    }


def _f0_track(Z, f, sr):
    """每帧的粗 f0（Hz）：取 120~900Hz 里最强的那根。

    ★ 只要"高不高"，不要准 —— 判断"这段是不是高音"用最大值法就够，
      上自相关（finetune._autocorr_f0 那套）是逐块跑的，放这里太慢。
      代价：泛音比基频强时会高估一个八度 ⇒ 所以阈值取 380Hz 起，
      高估也只是"更算高音"，不会漏掉真正的高音（宁可多柔一点）。
    """
    m = (f >= 120.0) & (f <= 900.0)
    if not m.any():
        return np.zeros(Z.shape[1], dtype=np.float64)
    sub = np.abs(Z[m, :])
    return f[m][np.argmax(sub, axis=0)]


def measure_post(x, sr):
    v = _mono(np.asarray(x, dtype=np.float64))
    out = {"sib_excess_db": None, "harsh_excess_db": None, "belt_rel_db": None,
           "belt_frames": 0, "air_db": None, "mid_db": None,
           "splices": _count_splices(x, sr), "peak": round(_peak(x), 4)}
    if len(v) < NFFT * 2:
        return out
    out["loud_db"] = round(_rms_db(v), 2)
    f, t, Z = _stft(v, sr)
    # 同 measure_pre：唱后也要能看见"咬字被动了没"。heal / limit 这类直接改波形
    # 的步骤会把辅音抹平，而 mid_db 那把尺子住在 300~1500Hz —— 看不见就等于没护栏。
    mc = (f >= CONS_LO) & (f < CONS_HI)
    out["cons_db"] = round(float(np.percentile(
        10.0 * np.log10((np.abs(Z[mc, :]) ** 2).max(axis=0) + 1e-20), 90.0)), 2) \
        if mc.any() else None
    mid = _band_db(Z, f, HARSH_REF_LO, HARSH_REF_HI)
    sib = _band_db(Z, f, DEESS_LO, DEESS_HI)
    hrs = _band_db(Z, f, HARSH_LO, HARSH_HI)
    bb = _band_db(Z, f, BELT_LO, BELT_HI)
    br = _band_db(Z, f, BELT_REF_LO, BELT_REF_HI)
    air = _band_db(Z, f, AIR_LO, AIR_HI)
    wf = _sec_frames(sr, 0.3)
    if mid is None:
        return out
    out["mid_db"] = round(float(np.mean(mid)), 2)
    if sib is not None:
        out["sib_excess_db"] = round(float(np.percentile(_excess(sib - mid, wf), 95.0)), 2)
    if hrs is not None:
        out["harsh_excess_db"] = round(float(np.percentile(_excess(hrs - mid, wf), 95.0)), 2)
    f0 = _f0_track(Z, f, sr)
    hi = f0 >= BELT_F0_LO
    out["belt_frames"] = int(hi.sum())
    if bb is not None and br is not None and hi.any():
        out["belt_rel_db"] = round(float(np.mean((bb - br)[hi])), 2)
        out["belt_mid_db"] = round(float(np.mean(br[hi])), 2)
    if air is not None:
        out["air_db"] = round(float(np.mean(air - mid)), 2)
    return out


# ---------------------------------------------------------------------------
# 唱前（pre）
# ---------------------------------------------------------------------------

def highpass(x, sr, fc=HP_FC):
    nyq = 0.5 * float(sr)
    fc = min(float(fc), nyq * 0.45)
    sos = butter(HP_ORDER, fc / nyq, btype="highpass", output="sos")
    return _map1(x, lambda v: sosfiltfilt(sos, v))


def denoise(x, sr, amount=1.0):
    """谱门降噪：拿全曲最安静的 10% 帧当噪声样本，逐频带定门限。

    ★ 为什么不用"滑动最小值"（noisereduce 的非平稳档）：唱歌几乎全程有声，
      滑窗里的最小值会被弱唱段拉高 ⇒ 门限跟着涨 ⇒ 真唱一起被啃掉。
      用"全曲最静的几帧"当样本是 noisereduce 的**平稳**档，对干声更稳。
    ★ 为什么不整个抹掉：抹成 0 会出现"音乐噪声"（一坨一坨的残留），
      所以 prop_decrease 留 15%，这是 Audacity 那套的默认附近。
    """
    if len(x) < NFFT * 2:
        return np.asarray(x, dtype=np.float64)
    prop = max(0.0, min(DENOISE_MAX, DENOISE_MAX * float(amount)))

    def _one(v):
        f, t, Z = _stft(v, sr)
        M = np.abs(Z)
        e = (M ** 2).mean(axis=0)
        k = max(1, int(round(M.shape[1] * QUIET_PCT / 100.0)))
        qi = np.argsort(e)[:k]
        prof = M[:, qi].mean(axis=1)
        thr = prof[:, None] * NOISE_MULT
        g = np.clip((M - thr) / (M + 1e-12), 0.0, 1.0)
        # 平滑：频带方向抹掉"音乐噪声"，时间方向只轻抹（重了会跟着唱一起抽）
        g = median_filter(g, size=(7, 1))
        g = uniform_filter1d(g, size=3, axis=1)
        g = 1.0 - prop * (1.0 - g)
        y = _istft(Z * g, sr, length=len(v))
        return y[:len(v)]

    return _map1(x, _one)


CUBIC_MAX_MS = 8.0           # 超过 8ms 的窟窿改用直线补 —— 三次曲线跨太长会飞
                             # （实测补一段 20ms 削波，峰值冲到 10.4，等于造了个新的爆音）


def _cubic_fill(v, a, b):
    """补 [a,b) 这一段：短的走三次插值（接得顺），长的走直线（不飞）。

    ★ 锚点必须在**待补区外面**：写过一版把锚点取在削波区的两端，
      两端值都是 0.995、两端斜率都是 0 ⇒ 插值出来是一条平的 0.995，
      等于没修（实测 clip_ratio 一动不动）。所以调用方要给外扩的区间。
    ★ 补完一律夹回 ±0.999：补出来的东西不许再制造新的削波。
    """
    n = len(v)
    if b - a < 4:
        return False
    x0, x1 = float(a), float(b - 1)
    y0, y1 = float(v[a]), float(v[b - 1])
    if (b - a) > CUBIC_MAX_MS * 0.001 * 44100:
        v[a:b] = np.linspace(y0, y1, b - a)
    else:
        sl = float(v[a + 1] - v[a]) if a + 1 < n else 0.0
        sr_ = float(v[b - 1] - v[b - 2]) if b - 2 >= 0 else 0.0
        A = np.array([
            [x0 ** 3, x0 ** 2, x0, 1.0],
            [x1 ** 3, x1 ** 2, x1, 1.0],
            [3 * x0 ** 2, 2 * x0, 1.0, 0.0],
            [3 * x1 ** 2, 2 * x1, 1.0, 0.0],
        ], dtype=np.float64)
        try:
            co = np.linalg.solve(A, np.array([y0, y1, sl, sr_]))
        except np.linalg.LinAlgError:
            v[a:b] = np.linspace(y0, y1, b - a)
            return True
        v[a:b] = np.polyval(co, np.arange(a, b))
    np.clip(v[a:b], -0.999, 0.999, out=v[a:b])
    return True


def declip(x, sr):
    """削波重建：被削平的那段用三次插值补回来（voicefixer / iZotope 同一招）。

    ★ 只修短段（<50ms）。整段都削了说明录音本身就爆了，插值补出来的是假的，
      补了也不好听 ⇒ 照实不修（铁律：宁可拒绝不修，也不许乱修）。
    """
    def _one(v):
        v = np.asarray(v, dtype=np.float64)
        bad = np.abs(v) >= CLIP_THR
        if not bad.any():
            return v
        y = v.copy()
        n = len(v)
        maxrun = max(2, int(CLIP_MAX_RUN * sr))
        d = np.diff(np.concatenate([[0], bad.view(np.int8), [0]]))
        st = np.flatnonzero(d == 1)
        en = np.flatnonzero(d == -1)
        for s, e in zip(st, en):
            if (e - s) > maxrun or (e - s) < 1:
                continue
            # ★ 补的区间要**外扩**到削波区之外：锚点取在被削平的那一段里
            #   的话，两端值都是满刻度、斜率为 0 ⇒ 插出来还是平的（等于没修）。
            l = max(0, int(s) - 8)
            r = min(n, int(e) + 8)
            if r - l < 6:
                continue
            _cubic_fill(y, l, r)
        return y
    return _map1(x, _one)


def declick(x, sr):
    return _map1(x, lambda v: _heal_spikes(v, sr, CLICK_HEAL_MS))


def _heal_spikes(v, sr, half_ms=CLICK_HEAL_MS, passes=2):
    """按 _spike_segments 找到的孤立尖峰做窄窗三次插值。

    ★ 两遍的理由和 _heal_points 一样：第一遍补完，边界上会留下新的小台阶。
    ★ `np.array(..., copy=True)` 是硬要求 —— `np.asarray` 在同 dtype 时**不复制**，
      会把调用方的数组就地改掉（2026-10-26 的血案：剂量阶梯三档叠加生效，
      报告却写"没采纳"）。
    """
    y = np.array(v, dtype=np.float64, copy=True)
    n = len(y)
    h = max(2, int(sr * half_ms / 1000.0))
    for _ in range(passes):
        segs = _spike_segments(y, sr)
        if not segs:
            break
        last = -10 ** 9
        for lo, hi in segs:
            c = (lo + hi) // 2
            a, b = max(0, c - h), min(n, c + h)
            if b - a < 6 or a < last:
                continue
            _cubic_fill(y, a, b)
            last = b
    return y


def heal_splices(x, sr):
    """修拼接点（"啪"一下的那种）：检测到硬台阶就插值抹平。"""
    return _map1(x, lambda v: _heal_points(v, sr, SPLICE_K, SPLICE_WIN_MS, HEAL_MS))


def _heal_points(v, sr, k, win_ms, half_ms, passes=2):
    """抹掉检测到的跳变点。

    ★ 为什么要跑两遍：第一遍补完，补的边界上会留下新的小台阶
      （三次插值的端点斜率是用一阶差分估的，有误差）⇒ 第二遍把它收干净。
      实测：合成素材 3 个硬台阶，一遍只剩 2 个，两遍才归零。
      再多跑没意义 —— 第二遍之后检测已经抓不到东西了。
    """
    y = np.asarray(v, dtype=np.float64)
    n = len(y)
    h = max(2, int(sr * half_ms / 1000.0))
    for _ in range(passes):
        idx = _diff_outliers(y, sr, k, win_ms)
        if idx.size == 0:
            break
        last = -10 ** 9
        for i in idx:
            a, b = max(0, int(i) - h), min(n, int(i) + h)
            if b - a < 6 or a < last:
                continue
            _cubic_fill(y, a, b)
            last = b
    return y


def level(x, sr, target_dbfs=PRE_TARGET_DBFS):
    pk = _peak(x)
    if pk <= 0:
        return np.asarray(x, dtype=np.float64)
    g = (10.0 ** (float(target_dbfs) / 20.0)) / pk
    return np.asarray(x, dtype=np.float64) * g


# ---------------------------------------------------------------------------
# 唱后（post）
# ---------------------------------------------------------------------------

def _env_smooth(cut, sr, attack_ms=5.0, release_ms=60.0):
    """快起慢落包络。起快了才压得住齿音那一下，落慢了才不抽。"""
    a = max(1.0, sr * attack_ms / 1000.0 / HOP)
    r = max(1.0, sr * release_ms / 1000.0 / HOP)
    env = np.zeros_like(cut)
    cur = 0.0
    for i in range(len(cut)):
        c = float(cut[i])
        tau = a if c < cur else r
        cur += (1.0 - np.exp(-1.0 / tau)) * (c - cur)
        env[i] = cur
    return env


def _dyn_band_cut(x, sr, lo, hi, ref_lo, ref_hi, thresh_db, max_db,
                  amount=1.0, release_ms=60.0, side_ratio=None):
    """动态带衰减的通用骨架（deess / deharsh 走它，belt 另有一版因为要绑 f0）。

    只在"这一帧该频带比它自己的平时水平突出"时压 ⇒ 常驻 EQ 会把整首歌压闷，
    动态 EQ 只压冒出来的那一下（shimmer 用实测数据确认了这条）。
    """
    if len(x) < NFFT * 2:
        return np.asarray(x, dtype=np.float64)
    cap = max_db * max(0.0, min(1.0, float(amount)))

    def _one(v, scale=1.0):
        f, t, Z = _stft(v, sr)
        band = _band_db(Z, f, lo, hi)
        ref = _band_db(Z, f, ref_lo, ref_hi)
        if band is None or ref is None:
            return v
        exc = _excess(band - ref, _sec_frames(sr, 0.3))
        over = np.clip(exc - thresh_db, 0.0, None)
        cut = -cap * scale * np.clip(over / max(thresh_db, 0.5), 0.0, 1.0)
        env = _env_smooth(cut, sr, 5.0, release_ms)
        m = (f >= lo) & (f <= hi)
        tt = (f[m] - lo) / max(hi - lo, 1e-9)
        shape = 0.5 * (1.0 - np.cos(2.0 * np.pi * tt))   # 余弦窗：硬边会出金属声
        Z2 = Z.copy()
        Z2[m, :] = Z[m, :] * (10.0 ** ((env[None, :] * shape[:, None]) / 20.0))
        y = _istft(Z2, sr, length=len(v))
        return y[:len(v)]

    if side_ratio is not None and np.asarray(x).ndim == 2 and np.asarray(x).shape[1] == 2:
        # M/S：中间全压、两侧半压（shimmer 实测：辅音主要在中置）
        x = np.asarray(x, dtype=np.float64)
        mid = 0.5 * (x[:, 0] + x[:, 1])
        sid = 0.5 * (x[:, 0] - x[:, 1])
        mid_o = _one(mid, 1.0)
        sid_o = _one(sid, side_ratio)
        return np.stack([mid_o + sid_o, mid_o - sid_o], axis=1)

    return _map1(x, lambda v: _one(v, 1.0))


def deess(x, sr, amount=1.0):
    return _dyn_band_cut(x, sr, DEESS_LO, DEESS_HI, HARSH_REF_LO, HARSH_REF_HI,
                         DEESS_THRESH_DB, DEESS_MAX_DB, amount,
                         release_ms=60.0, side_ratio=DEESS_SIDE_RATIO)


def deharsh(x, sr, amount=1.0):
    return _dyn_band_cut(x, sr, HARSH_LO, HARSH_HI, HARSH_REF_LO, HARSH_REF_HI,
                         HARSH_THRESH_DB, HARSH_MAX_DB, amount, release_ms=80.0)


def belt_soften(x, sr, amount=1.0):
    """高音柔化：只在 f0 真的高的那些帧上，压 2.5~6kHz。

    ★ 为什么必须绑 f0：不绑的话，中低音的 2.5~6k 是**亮度**（要留着），
      只有升调把谐波搬上来之后的那部分才是刺。绑了 f0 才敢下这一刀。
    ★ 这是"事后补救"不是治本：治本是移调时保共振峰（OpenTune 走的路），
      那要声码器。这一刀能救多少，由复测说了算（不达标就不落盘）。
    """
    if len(x) < NFFT * 2:
        return np.asarray(x, dtype=np.float64)
    cap = BELT_MAX_DB * max(0.0, min(1.0, float(amount)))

    def _one(v):
        f, t, Z = _stft(v, sr)
        bb = _band_db(Z, f, BELT_LO, BELT_HI)
        br = _band_db(Z, f, BELT_REF_LO, BELT_REF_HI)
        if bb is None or br is None:
            return v
        f0 = _f0_track(Z, f, sr)
        beltness = np.clip((f0 - BELT_F0_LO) / (BELT_F0_HI - BELT_F0_LO), 0.0, 1.0)
        # ★ 基线必须取"**低音**帧"的亮度，不能取自己的滑动中位线：
        #   高音是**持续**亮的，跟自己比永远是 0 ⇒ 一刀都下不去
        #   （实测：合成素材上 belt_rel 只动了 0.01dB）。要比的是
        #   "这句高音比这首歌平时亮了多少"。
        rel = bb - br
        lo = f0 < BELT_F0_LO
        base = float(np.median(rel[lo])) if lo.sum() >= 8 else float(np.median(rel))
        exc = np.clip(rel - base, 0.0, None)
        cut = -cap * beltness * np.clip(exc / 2.0, 0.0, 1.0)
        # 慢平滑：高音是"整段刺"，不是"一下一下"，快起快落会抽
        env = _env_smooth(cut, sr, 120.0, 120.0)
        m = (f >= BELT_LO) & (f <= BELT_HI)
        tt = (f[m] - BELT_LO) / max(BELT_HI - BELT_LO, 1e-9)
        shape = 0.5 * (1.0 - np.cos(2.0 * np.pi * tt))
        Z2 = Z.copy()
        Z2[m, :] = Z[m, :] * (10.0 ** ((env[None, :] * shape[:, None]) / 20.0))
        y = _istft(Z2, sr, length=len(v))
        return y[:len(v)]

    return _map1(x, _one)


def _log_env(log_mag, sr, n=NFFT, bw=FORMANT_BW):
    """log 幅度谱 → 频谱包络。log 域做移动平均，等效于"包络只跟 bw 尺度的大起伏"。

    bw 越大包络越粗（只跟共振峰那种大包），越小越贴谐波（会把谐波当包络）。
    """
    bin_w = max(1, int(round(bw * n / float(sr))))
    if bin_w <= 1:
        return log_mag
    return uniform_filter1d(log_mag, size=bin_w, axis=0, mode="nearest")


def formant_shift(x, sr, amount=1.0, semitones=None):
    """把被升调一起带跑的共振峰搬回来（只搬包络，谐波精细结构原样留着）。

    ★ semitones = **这条音频被升了多少半音**（正=升）。它是这一步唯一的驱动量，
      而且**必须外面给** —— 不猜，猜不出来就一步不动。
      为什么不拿"高音段 vs 低音段的包络质心比"自己算（第一版就是这么写的，错了）：
      那是**相对**量，而搬移是**全局**的 —— 高低音两头一起搬，比值根本不变。
      实测把 beta 从 1.0 扫到 0.75，那个比值在 0.92~1.82 之间乱跳，一点不单调。
      ⇒ 能当驱动量的只有"这次升了多少"，那个只有调用方知道。
    ★ amount = 补偿多少（1=全补，0.5=补一半）。全补有时候反而不自然，
      所以留了这一档，实际用哪一档由复测 + 用户耳朵说了算。
    ★ 搬完把**响度拉回原样** —— 这一步改的是频谱形状，不该顺手改音量。
    """
    try:
        st = float(semitones or 0.0)
    except (TypeError, ValueError):
        st = 0.0
    if abs(st) < 1e-6:
        return np.asarray(x, dtype=np.float64)
    # 最多补 FORMANT_MAX_SEMI 个半音：再狠就不是"修"了，是换一个人
    st = max(-FORMANT_MAX_SEMI, min(FORMANT_MAX_SEMI, st))
    beta = 2.0 ** (-st * max(0.0, min(1.0, float(amount))) / 12.0)
    return _map1(x, lambda v: _formant_shift_one(v, sr, beta))


def _formant_shift_one(v, sr, beta):
    v = np.asarray(v, dtype=np.float64)
    if len(v) < NFFT * 2 or abs(beta - 1.0) < 1e-4:
        return v
    f, t, Z = _stft(v, sr)
    log_mag = np.log(np.abs(Z) + 1e-8)
    log_env = _log_env(log_mag, sr, Z.shape[0])
    fine = log_mag - log_env                     # 谐波的精细结构，原样保留
    n = log_mag.shape[0]
    src = np.clip(np.round(np.arange(n) / beta).astype(int), 0, n - 1)
    warped = log_env[src]
    # ★ 只在 20Hz~8kHz 这一段搬。8k 以上原样 —— 那里是齿音的地盘，动它会炸。
    m = ((f >= FORMANT_LO) & (f <= FORMANT_HI))[:, None]
    out_env = np.where(m, warped, log_env)
    # 安全帽：搬完的包络可能比原来高出一截，任由它去会冲出削波 ⇒ 卡在
    # "原谱最高点 +1 nat（≈8.7dB）"，正常素材根本碰不到这个盖。
    cap_hi = float(log_mag.max()) + 1.0
    cap_lo = float(log_mag.min()) - 1.0
    mag2 = np.exp(np.clip(fine + out_env, cap_lo, cap_hi))

    y = _istft(mag2 * np.exp(1j * np.angle(Z)), sr, length=len(v))[:len(v)]
    r0 = float(np.sqrt(np.mean(v ** 2)))
    r1 = float(np.sqrt(np.mean(y ** 2)))
    if r0 > 1e-9 and r1 > 1e-9:
        y = y * (r0 / r1)
    return np.clip(y, -0.999, 0.999)


def soft_limit(x, sr, ceil=LIMIT_CEIL):
    """软拐限幅：knee 以上走 tanh，听不出"咔"。

    ★ 为什么不用硬削：硬削会产生新的高频（削出来的方波边沿）⇒ 正好是
      "刺"的来源，等于前面白修。
    """
    x = np.asarray(x, dtype=np.float64)
    t = LIMIT_KNEE
    out = x.copy()
    a = np.abs(x)
    m = a > t
    if not m.any():
        return out
    over = (a[m] - t) / max(1.0 - t, 1e-9)
    y = t + (float(ceil) - t) * np.tanh(over)
    out[m] = np.sign(x[m]) * y
    return out


# ---------------------------------------------------------------------------
# 拼接（等功率交叉淡化 + 零交叉对齐）
# ---------------------------------------------------------------------------

def _snap_zero(v, i, sr):
    """把下标 i 挪到最近的零交叉点（±3ms 内）。波形在零点切断才不"啪"。"""
    w = max(4, int(0.003 * sr))
    lo = max(0, i - w)
    hi = min(len(v), i + w)
    if hi - lo < 4:
        return i
    seg = v[lo:hi]
    z = np.flatnonzero(np.diff(np.signbit(seg)))
    if z.size == 0:
        return i
    return lo + int(z[np.argmin(np.abs(z - (i - lo)))])


def concat(parts, sr, xfade_ms=12.0):
    """把若干段拼成一条，接缝处等功率交叉淡化。

    ★ 为什么是"等功率"（sqrt）不是线性：线性交叉淡化在中间会**掉 6dB**
      （两段各 0.5 相加只有 0.5，不是 1）⇒ 听感上接缝处"陷"一下。
      sqrt 的两段平方和为 1，能量连续。这是拼接的老规矩。
    ★ 零交叉对齐：接缝落在波形非零点 ⇒ 一个台阶 ⇒ "啪"。
    """
    parts = [np.asarray(p, dtype=np.float64) for p in parts if len(p)]
    if not parts:
        return np.zeros(0, dtype=np.float64)
    if parts[0].ndim == 2:
        chs = [concat([p[:, c] for p in parts], sr, xfade_ms)
               for c in range(parts[0].shape[1])]
        n = min(len(c) for c in chs)
        return np.stack([c[:n] for c in chs], axis=1)
    out = parts[0].copy()
    xf = max(1, int(sr * float(xfade_ms) / 1000.0))
    for nxt in parts[1:]:
        xf2 = min(xf, len(out) - 1, len(nxt) - 1)
        if xf2 <= 1:
            out = np.concatenate([out, nxt])
            continue
        i = max(0, min(_snap_zero(out, len(out) - xf2, sr), len(out) - 2))
        j = max(1, min(_snap_zero(nxt, xf2, sr), len(nxt) - 1))
        ov = min(len(out) - i, j)
        if ov > 1:
            w = np.linspace(0.0, 1.0, ov)
            mid = out[i:i + ov] * np.sqrt(1.0 - w) + nxt[j - ov:j] * np.sqrt(w)
            out = np.concatenate([out[:i], mid, nxt[j:]])
        else:
            out = np.concatenate([out[:i], nxt[j:]])
    return out


# ---------------------------------------------------------------------------
# auto：量 → 修 → 复测 → 不达标就回滚（每一步都留证据）
# ---------------------------------------------------------------------------

def _ladder(fn, x, sr, amounts=(1.0, 0.7, 0.45)):
    """按剂量从猛到轻试，把每一档的结果 yield 出去由调用方判"要不要"。

    ★ 每一档都拿**同一份快照**去算：万一某个 fn 又犯了"就地改参数"的毛病
      （2026-10-26 的 declick / heal_splices 就这么干过），脏的只是这一份临时副本，
      不会把上一档的结果污染给下一档 —— 否则三档叠加 = 每步按最猛剂量做三遍。
    """
    base = np.array(x, dtype=np.float64, copy=True)
    for a in amounts:
        yield a, fn(base, sr, a)


def cmd_pre(wav, out_wav, out_json, amount=1.0, skip=()):
    try:
        x, sr = _read(wav)
    except Exception as e:                                   # noqa: BLE001
        _dump(out_json, {"ok": False, "reason": "读不了音频：%s" % e})
        sys.stdout.write("POLISH_FAIL 读不了音频\n")
        return 1
    m0 = measure_pre(x, sr)
    rep = {"ok": True, "applied": False, "before": m0, "steps": [], "amount": amount}
    cur = x
    changed = False
    _dur_sec = max(0.1, len(x) / float(sr))     # 给密度闸门用（见 CLICK_MAX_PER10S）
    _reasons = []                               # 被拒的理由，最后并进 rep

    def _try(name, fn, check):
        nonlocal cur, changed
        if name in skip:
            rep["steps"].append({"name": name, "skipped": True})
            return
        base = measure_pre(cur, sr)
        for a, y in _ladder(fn, cur, sr):
            m2 = measure_pre(y, sr)
            # ★ 把**两段音频本身**也给 check：算"内容掉了多少"必须拿它们在
            #   同一批帧上比（见 _content_harm_at 的注释），只比标量会在漂。
            g, harm, ok = check(base, m2, cur, y)
            rep["steps"].append({"name": name, "amount": round(float(a), 2),
                                 "gain": g, "harm": harm, "kept": bool(ok)})
            if ok:
                cur = y
                changed = True
                return
        rep["steps"].append({"name": name, "kept": False,
                             "reason": "试了所有剂量都没达标 ⇒ 不动"})

    def _band_drop(b, m2, key):
        """某个内容指标掉了多少 dB（正 = 掉了）。量不出来返回 None。"""
        bv, mv = _f(b.get(key)), _f(m2.get(key))
        if bv is None or mv is None:
            return None
        return abs(mv - bv)

    def _content_harm_at(base_v, y_v):
        """★ "内容掉了多少"的**正确算法**：两版在同一批帧上比，且用中位数。

        为什么不能直接拿 measure_pre 里那两个数相减：那两个是**各自**在自己那条
        音频上按 p90 挑帧算的 —— 处理之后能量排序会变 ⇒ 挑出来的**不是同一批帧**
        ⇒ 差值里混进了"换了一批评委"，看着像人声被啃了。
        实测：一段带 +0.8 冲击的素材，denoise 让噪声底好转 8.19dB，这个口径
        同时报出"人声本体掉了 8.45dB"；可是把 before/after 两个数摆在一起看，
        sig_db 和 cons_db **一个数都没动**（−19.77→−19.77 / −36.42→−36.42）。
        ⇒ 那是尺子在漂，跟文件里 SIG_LO 那条注释记的是同一类坑，这次轮到挑帧。

        为什么取中位数而不是 p90：素材里只要有一个瞬态（冲击、换气），p90 就被
        它牵着走；中位数对孤立点不敏感，量出来的才是"常态内容到底少了没有"。
        """
        try:
            if base_v is None or y_v is None:
                return (None, None)
            bv = _mono(np.asarray(base_v, dtype=np.float64))
            yv = _mono(np.asarray(y_v, dtype=np.float64))
            if len(bv) < NFFT * 2 or len(yv) < NFFT * 2:
                return (None, None)
            f, t, Zb = _stft(bv, sr)
            _, _, Zy = _stft(yv, sr)
            n = min(Zb.shape[1], Zy.shape[1])
            if n < 4:
                return (None, None)
            Zb, Zy = Zb[:, :n], Zy[:, :n]
            dbv = 10.0 * np.log10((np.abs(Zb) ** 2).mean(axis=0) + 1e-20)
            voiced = dbv > (np.percentile(dbv, 90.0) - 40.0)
            if not voiced.any():
                return (None, None)
            out = []
            for lo, hi in ((SIG_LO, SIG_HI), (CONS_LO, CONS_HI)):
                mm = (f >= lo) & (f < hi)
                if not mm.any():
                    out.append(None)
                    continue
                a = np.median((np.abs(Zb[mm, :]) ** 2).max(axis=0)[voiced])
                c = np.median((np.abs(Zy[mm, :]) ** 2).max(axis=0)[voiced])
                out.append(round(float(10.0 * np.log10((a + 1e-20) / (c + 1e-20))), 2))
            return (out[0], out[1])
        except Exception:                                        # noqa: BLE001
            return (None, None)

    def _content_harm(base_v, y_v, b=None, m2=None):
        """内容护栏：本体（300~1500）和**咬字**（2~8k）各被动了多少，取最坏的报。

        为什么非要把咬字也拉进来：以前只有本体这一把尺子，而咬字住在 2~8kHz
        ⇒ 只要某一步把辅音抹平，全部自动判据**都看不见**，照样绿灯通过。
        2026-10-26 的 declick 就是这样（45 秒里误判 292 个点，每点抹平 132 个
        采样），老板听到的"丢词、丢音"就是从这条缝里漏出去的。

        bv, yv 优先：有具体音频就走"同一批帧 + 中位数"的正确口径（见上），
        量不出来才退回比标量。
        """
        if base_v is not None and y_v is not None:
            hs, hc = _content_harm_at(base_v, y_v)
            if hs is not None or hc is not None:
                vals = [abs(v) for v in (hs, hc) if v is not None]
                return round(max(vals), 2) if vals else 0.0
        if b is None or m2 is None:
            return 0.0
        hs = _band_drop(b, m2, "sig_db")
        hs = 0.0 if hs is None else hs
        hc = _band_drop(b, m2, "cons_db")
        if hc is None:
            return round(hs, 2)
        return round(max(hs, hc), 2)

    def _hp_check(b, m2, bv=None, yv=None):
        if b.get("rumble_db") is None or m2.get("rumble_db") is None:
            return 0.0, 0.0, False
        g = round(b["rumble_db"] - m2["rumble_db"], 2)
        h = round(_content_harm(bv, yv, b, m2), 2)
        return g, h, bool(g >= 3.0 and h <= 0.3)

    def _dn_check(b, m2, bv=None, yv=None):
        g = round(b["floor_db"] - m2["floor_db"], 2)
        h = round(_content_harm(bv, yv, b, m2), 2)
        # ★ harm 上限 1.5dB 而不是 0.5：降噪本来就要动到谐波之间的东西，
        #   最猛那档实测让谐波峰掉 1.1dB（人耳几乎听不出），卡 0.5 的话
        #   降噪一步都迈不出去 —— 那就等于没这个功能。
        return g, h, bool(g >= 2.0 and h <= 1.5)

    def _clip_check(b, m2, bv=None, yv=None):
        g = round(b["clip_ratio"] - m2["clip_ratio"], 5)
        h = round(_content_harm(bv, yv, b, m2), 2)
        return g, h, bool(b["clip_ratio"] > 1e-4
                          and m2["clip_ratio"] < b["clip_ratio"] * 0.2 and h <= 0.5)

    def _click_check(b, m2, bv=None, yv=None):
        g = b["clicks"] - m2["clicks"]
        h = round(_content_harm(bv, yv, b, m2), 2)
        # ★ harm 上限 CONTENT_MAX_DROP_DB：见常量区。这条是"少了一点内容"
        #   就整步撤销 —— 爆音本来就该由更窄的修复窗（CLICK_HEAL_MS）去解决，
        #   不该靠放宽护栏来硬补。
        # ★ 密度闸门：报点太多 = 判据在这条素材上把辅音当成了爆音（见常量区），
        #   此时连试都不该试 —— 292 个点 × 每个都插值一次，窗再窄也照样伤字。
        dens = b["clicks"] / max(0.1, _dur_sec) * 10.0
        if dens > CLICK_MAX_PER10S:
            _reasons.append("疑似爆音 %d 个（每 10 秒 %.1f 个）—— 真爆音没这么多，"
                            "这是把咬字当成爆音了 ⇒ 这一步不动手"
                            % (b["clicks"], dens))
            return 0.0, h, False
        return g, h, bool(b["clicks"] > 0 and g > 0 and h <= CONTENT_MAX_DROP_DB)

    _try("declip", lambda z, s, a: declip(z, s), _clip_check)
    _try("declick", lambda z, s, a: declick(z, s), _click_check)
    _try("hp", lambda z, s, a: highpass(z, s), _hp_check)
    _try("denoise", lambda z, s, a: denoise(z, s, a * amount), _dn_check)

    if "level" not in skip and _peak(cur) > 0:
        y = level(cur, sr)
        if abs(_rms_db(y) - _rms_db(cur)) < 12.0:
            cur = y
            changed = True
            rep["steps"].append({"name": "level", "kept": True,
                                 "peak": round(_peak(cur), 4)})

    rep["after"] = measure_pre(cur, sr)
    if _reasons:
        rep["notes"] = list(_reasons)
        for t in _reasons:
            sys.stdout.write("  [%s] %s\n" % ("唱前不动手", t))
    if not changed:
        rep["reason"] = "没有一步达标 ⇒ 不改动音频（原样返回）"
        _dump(out_json, rep)
        sys.stdout.write("POLISH_PRE_SKIP steps=%d\n" % len(rep["steps"]))
        return 0
    _write(out_wav, cur, sr)
    rep["applied"] = True
    _dump(out_json, rep)
    # ★ 头条报**噪声底**不报信噪比：snr 的"分子"是整轨响度，去掉隆隆之后
    #   整轨变轻 ⇒ snr 反而**下降**（实测一段带 45Hz 隆隆的素材：8.3 → 0.7）。
    #   那不是变差，是尺子的分子里混进了该去掉的东西。噪声底才是唱前要盯的数。
    sys.stdout.write("POLISH_PRE_OK steps=%d floor=%.1f->%.1f snr=%.1f->%.1f\n"
                     % (len(rep["steps"]), m0["floor_db"], rep["after"]["floor_db"],
                        m0["snr_db"], rep["after"]["snr_db"]))
    return 0


def cmd_post(wav, out_wav, out_json, amount=1.0, skip=(), semi=0.0):
    try:
        semi = float(semi or 0.0)
    except (TypeError, ValueError):
        semi = 0.0
    try:
        x, sr = _read(wav)
    except Exception as e:                                   # noqa: BLE001
        _dump(out_json, {"ok": False, "reason": "读不了音频：%s" % e})
        sys.stdout.write("POLISH_FAIL 读不了音频\n")
        return 1
    m0 = measure_post(x, sr)
    rep = {"ok": True, "applied": False, "before": m0, "steps": [],
           "amount": amount, "semi": semi}
    cur = x
    changed = False
    _pdur_sec = max(0.1, len(x) / float(sr))    # 给密度闸门用（见 SPLICE_MAX_PER10S）
    _preasons = []

    def _try(name, fn, check):
        nonlocal cur, changed
        if name in skip:
            rep["steps"].append({"name": name, "skipped": True})
            return
        base = measure_post(cur, sr)
        for a, y in _ladder(fn, cur, sr):
            m2 = measure_post(y, sr)
            g, harm, ok = check(base, m2)
            rep["steps"].append({"name": name, "amount": round(float(a), 2),
                                 "gain": g, "harm": harm, "kept": bool(ok)})
            if ok:
                cur = y
                changed = True
                return
        rep["steps"].append({"name": name, "kept": False,
                             "reason": "所有剂量都没达标 ⇒ 不动"})

    def _mid_harm(b, m2):
        bm, mm = _f(b.get("mid_db")), _f(m2.get("mid_db"))
        h = 99.0 if (bm is None or mm is None) else abs(mm - bm)
        # 同 cmd_pre：把"咬字"也纳入 harm，谁砍了辅音谁就别想通过。
        bc, mc2 = _f(b.get("cons_db")), _f(m2.get("cons_db"))
        if bc is not None and mc2 is not None:
            h = max(h, abs(mc2 - bc))
        return h

    def _deess_check(b, m2):
        b0, m1 = _f(b.get("sib_excess_db")), _f(m2.get("sib_excess_db"))
        if b0 is None or m1 is None:
            return 0.0, _mid_harm(b, m2), False
        g = round(b0 - m1, 2)
        h = round(_mid_harm(b, m2), 2)
        return g, h, bool(b0 >= 2.0 and g >= 2.0 and h <= 0.5)

    def _harsh_check(b, m2):
        b0, m1 = _f(b.get("harsh_excess_db")), _f(m2.get("harsh_excess_db"))
        if b0 is None or m1 is None:
            return 0.0, _mid_harm(b, m2), False
        g = round(b0 - m1, 2)
        h = round(_mid_harm(b, m2), 2)
        return g, h, bool(b0 >= 1.5 and g >= 1.5 and h <= 0.5)

    def _belt_check(b, m2):
        b0, m1 = _f(b.get("belt_rel_db")), _f(m2.get("belt_rel_db"))
        if b0 is None or m1 is None:
            return 0.0, 0.0, False
        g = round(b0 - m1, 2)
        bb = _f(b.get("belt_mid_db")) or _f(b.get("mid_db"))
        mm = _f(m2.get("belt_mid_db")) or _f(m2.get("mid_db"))
        h = round(abs(mm - bb), 2) if (bb is not None and mm is not None) else 99.0
        return g, h, bool((b.get("belt_frames") or 0) >= 8 and g >= 0.8 and h <= 0.5)

    def _heal_check(b, m2):
        g = b["splices"] - m2["splices"]
        h = round(_mid_harm(b, m2), 2)
        # 密度闸门：与唱前 declick 同一个道理（见 CLICK_MAX_PER10S 那条注释）。
        # 实测同一段干净人声上报出 908 个"拼接点"，而 ±3ms 的修复窗会让每
        # 30.2% 的帧咬字掉超 3dB、f0 一致率掉到 63% —— 那正是"变调"的来源。
        dens = b["splices"] / max(0.1, _pdur_sec) * 10.0
        if dens > SPLICE_MAX_PER10S:
            _preasons.append("疑似拼接点 %d 个（每 10 秒 %.1f 个）—— 真接缝没这么多，"
                             "这是把咬字当成接缝了 ⇒ 这一步不动手" % (b["splices"], dens))
            return 0.0, h, False
        return g, h, bool(b["splices"] > 0 and g > 0 and h <= 0.5)

    # ★ formant **不做"有没有变好听"的判断** —— 那把尺子做不出来（两版都被
    #   自检证伪，见常量区的记录）。所以这里只有一道**"别搬破"**的守门：
    #   响度不许被改、不许搬出削波。搬完之后好不好听，由用户在试听里用耳朵定
    #   —— 界面上也是这么写的，不能假装我们量过"改善了"。
    def _formant_safe(b, m2):
        h = round(abs((_f(m2.get("loud_db")) or 0.0) - (_f(b.get("loud_db")) or 0.0)), 2)
        return 0.0, h, bool(h <= 0.5 and (_f(m2.get("peak")) or 0.0) <= 0.999)

    if abs(semi) >= 1e-6:
        _try("formant", lambda z, s, a: formant_shift(z, s, a, semitones=semi),
             _formant_safe)
    else:
        # ★ 没给半音数 ⇒ 一步不动，并把原因写进报告（不猜、不静默）。
        rep["steps"].append({"name": "formant", "skipped": True,
                             "reason": "没给升调半音数 —— 这一步只能按半音数搬，不猜"})
    _try("deess", lambda z, s, a: deess(z, s, a * amount), _deess_check)
    _try("deharsh", lambda z, s, a: deharsh(z, s, a * amount), _harsh_check)
    _try("belt", lambda z, s, a: belt_soften(z, s, a * amount), _belt_check)

    _try("heal", lambda z, s, a: heal_splices(z, s), _heal_check)

    # limit：只在真的会破的时候动手
    if "limit" not in skip and _peak(cur) > LIMIT_CEIL:
        y = soft_limit(cur, sr)
        h = round(_mid_harm(measure_post(cur, sr), measure_post(y, sr)), 2)
        if h <= 0.5:
            cur = y
            changed = True
            rep["steps"].append({"name": "limit", "kept": True,
                                 "peak": round(_peak(cur), 4)})
        else:
            rep["steps"].append({"name": "limit", "kept": False, "harm": h})

    rep["after"] = measure_post(cur, sr)
    if _preasons:
        rep["notes"] = list(_preasons)
        for t in _preasons:
            sys.stdout.write("  [%s] %s\n" % ("唱后不动手", t))
    if not changed:
        rep["reason"] = "没有一步达标 ⇒ 不改动音频（原样返回）"
        _dump(out_json, rep)
        sys.stdout.write("POLISH_POST_SKIP steps=%d\n" % len(rep["steps"]))
        return 0
    _write(out_wav, cur, sr)
    rep["applied"] = True
    _dump(out_json, rep)
    sys.stdout.write("POLISH_POST_OK steps=%d\n" % len(rep["steps"]))
    return 0


# ---------------------------------------------------------------------------
# 试听（audition）：切一小段，出"修前 / 修后"两条，给用户用耳朵判
# ---------------------------------------------------------------------------
# 为什么必须有这一段：上面每一步都配了"gain / harm"两个数，但**数字说不了
# 好不好听**。老板的原话是"希望能时刻听见我调整的内容 来确认我调整是否对"——
# 验收权在耳朵，数字只是帮你定位到该听哪一段。
#
# 交互范式照 GitHub 上人声工作站类项目的通行做法：
#   Tri-State A/B（原声 / 处理后 即时切换）+ 0~100% 干湿混合滑条 + 分步 solo。
#   干湿滑条由前端用两个 <audio> 同步播放 + 音量互补实现（拖动即刻出声，
#   不用重新算），后端只负责把两条片段准备好。

AUDITION_SEC = 8.0      # 试听片段长度。8 秒够听清一句又不用等太久
AUDITION_MAX_SEC = 20.0

# 每一步对应"该听哪一段"的定位指标。**值越大 = 问题越重**（挑最重的那段给用户听）
STEP_METRIC = {
    "declip": "clip_ratio", "declick": "clicks", "hp": "rumble_db",
    "denoise": "floor_db", "level": None,
    "formant": None,
    "deess": "sib_excess_db", "deharsh": "harsh_excess_db",
    "belt": "belt_rel_db", "heal": "splices", "limit": "peak",
}


def _seg_score(seg, sr, side, only=""):
    """给一小段打分：**分越高 = 问题越重** ⇒ 该挑给用户听。"""
    m = measure_pre(seg, sr) if side == "pre" else measure_post(seg, sr)
    k = STEP_METRIC.get(only) if only else None
    if k:
        v = m.get(k)
        return None if v is None else float(v)
    s, any_ = 0.0, False
    for st in (PRE_STEPS if side == "pre" else POST_STEPS):
        kk = STEP_METRIC.get(st)
        if not kk:
            continue
        v = m.get(kk)
        if v is None:
            continue
        s += float(v)
        any_ = True
    if any_:
        return s
    v = m.get("loud_db")          # 兜底：挑最响的那一段（主唱段）
    return None if v is None else float(v)


def pick_seg(x, sr, side="post", sec=AUDITION_SEC, only=""):
    """挑"问题最集中的那一小段"给用户试听。返回 (起点采样, 长度采样)。

    为什么不是固定取开头/中间：开头常常是前奏或气口，听不出齿音和高音刺；
    挑最重的那一段，用户一耳朵就能听出差别。
    """
    n = len(x)
    w = int(max(1.0, min(sec, AUDITION_MAX_SEC)) * sr)
    if n <= w:
        return 0, n
    hop = max(1, w // 2)
    bi, bs = 0, None
    for i in range(0, n - w + 1, hop):
        s = _seg_score(x[i:i + w], sr, side, only)
        if s is None:
            continue
        if bs is None or s > bs:
            bs, bi = s, i
    return bi, min(w, n - bi)


def cmd_audition(wav, dry_wav, wet_wav, out_json, side="post", amount=1.0,
                 skip=(), semi=0.0, sec=AUDITION_SEC, only="", at=-1.0):
    """切一段 → 出"修前(dry) / 修后(wet)"两条 wav + 一份对比报告。"""
    try:
        x, sr = _read(wav)
    except Exception as e:                                   # noqa: BLE001
        _dump(out_json, {"ok": False, "reason": "读不了音频：%s" % e})
        sys.stdout.write("POLISH_FAIL 读不了音频\n")
        return 1
    n = len(x)
    w = int(max(1.0, min(sec, AUDITION_MAX_SEC)) * sr)
    if at is not None and float(at) >= 0.0:
        st = int(max(0.0, float(at)) * sr)
    else:
        st, w = pick_seg(x, sr, side, sec, only)
    st = min(st, max(0, n - 1))
    w = min(w, n - st)
    seg = x[st:st + w]
    if len(seg) < int(0.2 * sr):
        _dump(out_json, {"ok": False, "reason": "这段太短了，没法试听"})
        sys.stdout.write("POLISH_FAIL 片段太短\n")
        return 1

    _write(dry_wav, seg, sr)
    # ★ 分步 solo 的语义别写反了：only 为空 ⇒ **一步都不 skip**（跑整条链）；
    #   only 给了 ⇒ 除它以外全 skip。第一版写成"only 为空就全 skip"，
    #   结果整条试听永远什么都不干（门禁实测：修前 = 修后，拖滑条一点差别没有）。
    if only:
        skips = tuple(s for s in (PRE_STEPS if side == "pre" else POST_STEPS)
                      if s != only)
    else:
        skips = ()
    try:
        if side == "pre":
            rc = cmd_pre(dry_wav, wet_wav, out_json + ".tmp", amount=amount, skip=skips)
        else:
            rc = cmd_post(dry_wav, wet_wav, out_json + ".tmp", amount=amount,
                          skip=skips, semi=semi)
    except Exception as e:                                   # noqa: BLE001
        rc, rep = 1, {"ok": False, "reason": "跑微调时出错：%s" % e}
    rep = None
    with contextlib.suppress(Exception):
        with open(out_json + ".tmp", encoding="utf-8") as f:
            rep = json.load(f)
    with contextlib.suppress(OSError):
        os.remove(out_json + ".tmp")
    rep = rep if isinstance(rep, dict) else {}

    # ★ 没达标时 wet 那个文件**不存在**（cmd_* 的约定：不写 wav）⇒
    #   这时把 dry 复制过去，让前端永远有两条能放，并照实说"没修，两条一样"。
    applied = bool(rep.get("applied"))
    if not os.path.isfile(wet_wav):
        applied = False
        with contextlib.suppress(Exception):
            shutil.copyfile(dry_wav, wet_wav)
    out = {"ok": True, "side": side, "only": only or "", "amount": amount,
           "semi": semi, "from": round(st / float(sr), 2),
           "sec": round(w / float(sr), 2),
           "applied": applied,
           "reason": rep.get("reason") or ("" if applied else "这一步没达标 ⇒ 没修，两条听起来会一样"),
           "steps": rep.get("steps") or [],
           "before": rep.get("before"), "after": rep.get("after")}
    _dump(out_json, out)
    sys.stdout.write("POLISH_AUDITION_OK side=%s only=%s from=%.1f sec=%.1f applied=%d\n"
                     % (side, only or "-", out["from"], out["sec"], 1 if applied else 0))
    return 0 if rc == 0 else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _opts(argv):
    o = {"amount": 1.0, "skip": (), "xfade": 12.0, "semi": 0.0, "sec": 0.0,
         "only": "", "at": -1.0}
    i = 0
    while i < len(argv):
        k = argv[i]
        if k == "--amount" and i + 1 < len(argv):
            try:
                o["amount"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif k == "--semi" and i + 1 < len(argv):
            # 这条音频被**升**了多少半音（正=升）。formant 那一步唯一的驱动量。
            try:
                o["semi"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif k == "--sec" and i + 1 < len(argv):
            try:
                o["sec"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif k == "--at" and i + 1 < len(argv):
            try:
                o["at"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        elif k == "--only" and i + 1 < len(argv):
            o["only"] = str(argv[i + 1]).strip()
            i += 2
        elif k == "--skip" and i + 1 < len(argv):
            o["skip"] = tuple(x.strip() for x in argv[i + 1].split(",") if x.strip())
            i += 2
        elif k == "--xfade" and i + 1 < len(argv):
            try:
                o["xfade"] = float(argv[i + 1])
            except ValueError:
                pass
            i += 2
        else:
            i += 1
    return o


def main():
    a = sys.argv[1:]
    if len(a) >= 3 and a[0] == "measure":
        try:
            x, sr = _read(a[1])
        except Exception as e:                               # noqa: BLE001
            _dump(a[2], {"ok": False, "reason": str(e)})
            sys.stdout.write("POLISH_FAIL 读不了音频\n")
            return 1
        o = _opts(a[3:])
        m = {"sr": sr, "pre": measure_pre(x, sr), "post": measure_post(x, sr)}
        _dump(a[2], {"ok": True, "measure": m})
        sys.stdout.write("POLISH_MEASURE sr=%d floor=%.1f snr=%.1f sib=%.1f "
                         "harsh=%.1f belt=%.1f splices=%d\n"
                         % (sr, m["pre"]["floor_db"], m["pre"]["snr_db"],
                            m["post"].get("sib_excess_db") or -1.0,
                            m["post"].get("harsh_excess_db") or -1.0,
                            m["post"].get("belt_rel_db") or -1.0,
                            m["post"]["splices"]))
        return 0
    if len(a) >= 4 and a[0] == "pre":
        o = _opts(a[4:])
        return cmd_pre(a[1], a[2], a[3], amount=o["amount"], skip=o["skip"])
    if len(a) >= 4 and a[0] == "post":
        o = _opts(a[4:])
        return cmd_post(a[1], a[2], a[3], amount=o["amount"], skip=o["skip"],
                        semi=o["semi"])
    if len(a) >= 5 and a[0] == "audition":
        # audition <wav> <dry.wav> <wet.wav> <out.json> [--side post] [--only deess] ...
        o = _opts(a[5:])
        side = "pre"
        for i, k in enumerate(a[5:]):
            if k == "--side" and i + 1 < len(a[5:]):
                side = str(a[5:][i + 1]).strip()
        if side not in ("pre", "post"):
            side = "post"
        return cmd_audition(a[1], a[2], a[3], a[4], side=side, amount=o["amount"],
                            semi=o["semi"], sec=o["sec"] or AUDITION_SEC,
                            only=o["only"], at=o["at"])
    if len(a) >= 3 and a[0] == "concat":
        o = _opts(a[3:])
        srcs = [p for p in a[2:] if not p.startswith("--")]
        try:
            parts, sr0 = [], None
            for p in srcs:
                v, s = _read(p)
                parts.append(v)
                sr0 = sr0 or s
        except Exception as e:                               # noqa: BLE001
            sys.stdout.write("POLISH_FAIL 读不了音频：%s\n" % e)
            return 1
        y = concat(parts, sr0 or 44100, o["xfade"])
        _write(a[1], y, sr0 or 44100)
        sys.stdout.write("POLISH_CONCAT_OK n=%d sr=%d\n" % (len(parts), sr0 or 44100))
        return 0
    sys.stdout.write(
        "usage: polish.py measure <wav> <out.json> [--ref 原唱.wav]\n"
        "       polish.py pre  <wav> <out.wav> <out.json> [--amount N] [--skip a,b]\n"
        "       polish.py post <wav> <out.wav> <out.json> [--amount N] [--skip a,b] [--semi N]\n"
        "       polish.py audition <wav> <dry.wav> <wet.wav> <out.json>\n"
        "              [--side pre|post] [--only STEP] [--amount N] [--sec 8] [--at 秒]\n"
        "       polish.py concat <out.wav> <a.wav> <b.wav> ... [--xfade 12]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
