# -*- coding: utf-8 -*-
r"""成品后处理：去**电音**（周期复印）+ 去**局部过冲**（破音）。

★ 2026-10-24 第四轮重写。原文件在一次脚本化编辑中被误清空（io.open(p,'w')
  截断后参数校验失败没写入，且无 git/备份），这里是重建版 —— 借重建把前三轮
  的结论和本轮的新证据一起固化进去。

为什么要有它
------------
老板连续三次反馈音质问题，三次的根因各不相同，这个模块是第三次和第四次的产物：

  第一次「明显电音」  → 量化出来了（artifact_cli + 体检指路），但只告警不修。
  第二次「还是有电音」→ 补上"修"：抖动 + 削高频。
  第三次「依旧有破音」→ 真因是**识别到了却修不动**（4-8k 没东西可削，
                        全带抖动压 8.9dB 会撞护栏 ⇒ 7 档全废）。
                        补上「分频抖动」（只抖 1500Hz 以上）。
  第四次「全过程沙哑 + 46 秒破音」← 分频抖动**就是沙哑的来源**
                        （jitter 0.0116 → 0.0172，+48%）。
  第五次「杂音很严重 + 开始乱叫」← ★ 本轮。补气声虽然**方向对了**，
                        但**下料下猛了**：为把 ΔHNR 从 12.61 压到 3.76，
                        往人声里塞了相当于它自身 RMS **83%** 的外来材料
                        —— 峰值（常态）形态不是气流，是**脉冲**（峰均比 19.2）。
                        详见下面的「第五轮」小节。

本轮最重要的两个发现（第四轮）
------------------------------
A. **「电音」和「破音」是两个独立的毛病**，混在一起处理必然互相拖累：

     电音 = 整首的**周期复印**（HNR 比原唱高 8~9dB、psim 0.83 vs 真人 0.72）
     破音 = **局部**高频尖峰。整首 hf_ratio 只 ×1.29（看着正常），
            但逐窗口看 ≥2 倍占 41.3%、≥5 倍占 23.7%、p90 = 16 倍、**p99 = 190 倍**，
            最狠的一处（114.11s）原唱 0.00009、成品 0.08858 —— **×950**。
            ⇒ 整首平均把局部尖峰稀释掉了 ⇒ 尺子说"没问题"，耳朵说"破"。
            ⇒ 而按整首统一去处理，对那 59% 本来就正常的窗口就是**过度处理**。

B. **降 HNR 有两条路，听感完全相反**：

     抖动      = **打散谐波**（乘性相位破坏）⇒ jitter 涨、psim 掉 ⇒ **沙哑**
     补气声    = **加回噪声**（加性）⇒ 谐波仍然相干 ⇒ **气声（柔）**

   物理上：真人 HNR 低是因为**有气声**，不是因为谐波散。所以要补的是噪声。
   实测（整首，过冲修正之后；REF = 原唱）：
        α(低通4k)  HNR     ΔHNR   psim    jitter   shimmer   4-8k
        0（只削）  19.20   +8.74  0.8311  0.01044  0.04821   0.01460
        3.0        14.58   +4.12  0.7431  0.01401  0.09382   0.01112
        ★ 4.0     13.17   +2.71  0.7132  0.01482  0.10619   0.00959
        6.0        10.34   -0.12  0.6627  0.01641  0.12260   0.00722
       （原唱      10.46    —      0.7225  0.01858  0.12489   0.01313）
   对照「抖动」落点：HNR 12.92、psim 0.7625、**jitter 0.01646**、shimmer 0.07896
    ⇒ α=4.0 达标（ΔHNR 2.71 < 4）而 **jitter 0.01482 比抖动版还低**，
     且 psim / shimmer 都更贴近真人。**补气声全面优于抖动。**
   ★ 但那首歌的 ΔHNR 只有 8.74。**方向对不等于剂量对**，见第五轮。

================================================================================
第五轮：「补气声」的**效率**和**上限**（2026-10-24 晚，老板第五次反馈）
================================================================================

老板的反馈原文：「杂音很严重 还开始乱叫了」。听的素材 ΔHNR = **12.61**
（角色碧琪唱《Everywhere You Look》，before HNR 19.66 / 原唱 7.05）。

上一轮只说了"补气声优于抖动"，却没说**要补多少**。第四轮拿**倍数**当剂量
（α=1.0/2.0/3.0…），可倍数**没有物理意义** —— 同样写 α=3.0，塞进去多少东西
完全取决于参考轨自己的能量，跟成品毫不相干。这一首系统上到了 α=3.0，实测：

    植入进成品的东西 = 人声自身 RMS 的 **83.0%**，峰均比 **19.2**（脉冲状）
    after = before×0.47 + 外来材料×0.47（峰值归一化把人声整体压掉一半）

⇒ 往人声里塞 83% 的外来材料，还能听得清才是怪事。**这就是"杂音"**；
  jitter 同时从 0.00828 涨到 0.01402（+69%）—— **这就是"乱叫"**。

标定实验（同一对素材，α 从 0.5 起扫到刚好达标 ΔHNR<4.0，横向比代价）：

    方案                          达标时植入%   该处 jitter   备注
    A 第四轮版（低通4k/原样）        83.0        0.01375     ← 老板听到的
    B 全带、原样                     86.2        0.01261
    C 低通4k + 去脉冲                45.6        0.01634
    D 全带 + 去脉冲                  46.1        0.01411
  ★ E 全带 + 去脉冲 + 跟成品包络     44.6        0.01369     ← 采用
    F E 之上再加「只填谐波间隙」      44.8        0.01148     见结论 2

结论三条：

1. **去脉冲让效率翻一倍**（同样植入 45%，E 能压到 ΔHNR 3.15，A 只到 5.87）。
   原理：孤立脉冲（齿音 / 爆破音 / UVR 分离残留的鼓点）本来是在各个频率同相
   叠加才形成的尖峰；把相位换成随机相位后它们摊平成高斯平稳过程 ——
   频谱形状和时变起伏都还在，尖峰没了。**掺进去的东西少了，活反而干了。**

2. **F（只填谐波间隙）被数据否掉**。按理论只要补 8%（把噪声占谐波的比例从
   1.08% 提到 9.1%），可"逐帧 f0 + 谐波位置挖谁(group)\) Statutory" 做下来，
   效率跟 E 打平（44.8% vs 44.6%）却要多跑一遍 f0 提取。
   ⇒ **只做能测出收益的**。
   （保留记录：这说明效率卡在 ~18% 附近不是 mask 没做对 —— parselmouth 认的
   "噪声"远少于我们掺进去的总能量，这个缺口靠堆花样补不上。）

3. ★★ **必须给植入量定上限**。它的物理依据不是猜的：
      真人的非谐波成分 / 谐波 ≈ 实测参考轨 P RMS 0.0367 / H RMS 0.1600 = **23%**。
      ⇒ 补进去的东西超过这个比例，就是**比真人还吵**。
      留一点余量 ⇒ BREATH_MAX_INSERT = 0.25。

   ⇒ 剂量单位也一起改了：**不再用倍数，直接用「植入百分比」**（相对成品 RMS）。
     这样"上限"就是剂量表的最后一档，结构上不可能超；
     而"倍数"这个单位没法设上限（同样 α，不同素材塞进去的能量能差一倍）。

--------------------------------------------------------------------------------
手段清单（按代价从小到大，剂量表就是这个顺序）
--------------------------------------------------------------------------------
  D. **补气声** —— 把原唱的非谐波成分做成平稳气声补回来。做三步：
       ① HPSS 取参考轨的 P 分量（自有正确的频谱形状和时变包络）；
       ② **去脉冲**：STFT 幅度沿时间平滑 + 随机相位重合成。
          实测这一步让效率翻倍（同样植入 45%，ΔHNR 能压到 3.15 vs 原来的 5.87）；
       ③ **乘成品的发声包络**：句间/间奏不补（参考轨在那儿的成分都是 UVR
          分离残漏，实测占比 4.8%，不该当成气声加进来）。
     ★ 单位改成「植入百分比」、并有硬上限 BREATH_MAX_INSERT = 0.25 —— 见第五轮。
     ★ 为什么不再低通 4k（第四轮的结论，被本轮数据推翻）：
         当初怕往 4-8k 加会把 hf_rel 判据顶超标。可实测低通**没提效**（A 83.0%
         vs 全带的 86.2%，差别在误差内），而 hf_rel 本来由 STEPS 循环里的判据
         兜底 —— 顶超标的档自动跳过。**加一道没被证明有用的限制是纯负债。**
     ★ 为什么不是白噪声（①号死路）：真人气声不是全带恒定的，它有跟共振峰同形的
       频谱形状和**时变包络**（只在出声时才有）。HPSS 取出的 P 自带这两样。
  E. **削局部过冲** —— 逐窗口算 4-8k 相对原唱的倍数，只削超标的窗口。
     ★ 零副作用（实测 jitter 0.01043 → 0.01062），不管电不电都做。
     ★ 必须在补气声**之前**做（顺序反了会互相抵消，见 cmd_auto 里的注释）。
  B. **削 4-8k**（整首架式）—— 只在 4-8k 单独超标时用。
     ★ 它会让 HNR **反升**（削掉高频后剩下的谐波更干净）⇒ 救不了 HNR 那条。
  C. **分频抖动** —— 只抖 1500Hz 以上的周期。降 HNR 很有效，但
     **打散谐波 = 沙哑**，所以降到**最后手段**，能不用就不用。

被数据否掉的路（别再走）
------------------------
  ① 掺白噪声压 HNR —— 要 35% 才够、HNR 掉到 6.4，等于把成品弄成沙沙响。
  ② 外部相位声码器变调（librosa pitch_shift）—— 4-8k 从 ×4.16 涨到 ×5.61，
     比 RVC 更糟；叠谱包络还原更闷更电（ΔHNR +11.94）。见 tools/probe_shiftpath.py。
  ③ 换 f0 提取器（pm/fcpe）想让周期"抖"起来 —— 三档 jitter 全在 0.0062~0.0069，
     "周期太规整"是声码器固有，不是 f0 曲线的锅。
  ④ psim（周期相似度）当判据 —— 所有 RVC 成品都是 0.84~0.92（真人 0.72），
     设阈值会让**每一首都判电**，没有区分度。只作诊断信息。
  ⑤ 「只削谐波峰、保留噪声底」的过冲修正 —— 宽带噪声在 STFT 域一样有随机
     起伏，照样被当成峰削掉，救不了补气声（实测无效）。

必须守住的约束
--------------
1. **不电就不动**（但过冲照修，它是另一回事）。
2. **从最小剂量开始试，达标就停**。
3. **修完必须再量一次**（闭环自验证）。
4. **所有剂量都压不下去就不动手**，宁可如实报"没修好"。
5. **HNR 是双面指标**：只设上限 ⇒ 系统一路往下压 ⇒ 压成沙哑。
   本轮栽过，所以现在是**双边区间** [HNR_FLOOR, HNR_DB)。
6. **测量窗口要按整首**：取"中间 30 秒"会不具代表性（实测同一条轨差 3.2dB，
   后果是系统为了数字好看多加了抖动）。
7. 抖动用固定随机种子：否则同一首歌每次修出来都不一样，A/B 没法对。
8. ★ **补气声有植入上限**（第五轮）：往成品里塞的外来材料不许超过成品自身
   RMS 的 BREATH_MAX_INSERT（0.25）。依据是实测参考轨的非谐波/谐波 = 23%
   —— 超过就是"比真人还吵"。到了上限还压不下去 ⇒ **承认修不动**，如实报，
   **不许为了让它达标而继续加料**（这一条的代价老板已经听过一次了：杂音）。

用法（cwd 必须是 RVC 根目录，用 RVC 自带解释器 —— 只有它有 parselmouth/librosa）：
    runtime\python.exe -s <本脚本> auto <成品.wav> <原唱.wav> <输出.wav> <输出.json>
                        [--hnr-db 4.0] [--hf-rel 2.0] [--steps ...] [--seed 1234]

stdout 只打一行机器可读结果（DEMETAL_APPLIED / DEMETAL_SKIP / DEMETAL_FAIL），
调用方只认这一行 —— 别把过程噪声混进去。
"""
import io
import json
import os
import sys

import numpy as np
import soundfile as sf

import artifact_cli

# ---- 判据阈值（默认值；产品调用时用命令行把 studio.py 的权威值传下来）--------
# ★ 为什么默认值要能被覆盖：判据必须**只有一处定义**（studio.py 的
#   AUDITION_METAL_*），本模块只是执行者。让调用方下发，才不会出现"两把尺子"。
HNR_DB = 4.0     # HNR 比原唱高这么多 dB ⇒ 判电
# ★★ HNR 是**双面指标**：太高 = 电（没有气声），太低 = 沙哑（噪声太多）。
#    2026-10-24 第四轮栽的坑：只设上限，等于鼓励一路往下压，压到老板反馈
#    「全过程沙哑」。⇒ 必须有下限，落在区间里才算好。
HNR_FLOOR = -1.0      # ΔHNR 不许低于这个（补气声补过头 = 沙沙响，也是沙哑）
JIT_MAX_REL = 1.6     # jitter_local 不许超过参考的这个倍数（打散谐波的直接后果）
JIT_ABS_MAX = 0.010
#   ★ 相对倍数 + 绝对下限，**两条满足其一就算合格**（or，不是 and）。
#     只有相对倍数那条时，在"本来极干净"的素材上会失效：门禁的合成素材
#     jitter 只有 5e-05（比真人小两个数量级，本来就是完美周期信号），
#     补一点点气声相对就涨到 2.2 倍 ⇒ 被判沙哑 ⇒ 所有档全废。
#     可它的绝对值 0.00025 离沙哑差十万八千里 —— 沙哑是**听得出来**的，
#     正常人 jitter 约 0.5%~1%，低于 1% 无论如何不算沙哑。
#     ⇒ 真实素材上仍然靠相对那条把关（实测 0.01081 ≤ 0.00828×1.6 ✓ 放行、
#       抖动档 0.01796 两条都不满足 ⇒ 拦下），不会因为加了这条就放行。
HF_REL = 2.0     # 4-8k 占比达到原唱的这么多倍（且 HNR 也涨了 ≥1dB）⇒ 判电
HF_REL_MIN_DH = 1.0   # 4-8k 那条单独不能成立时，HNR 至少要涨这么多

# ---- 剂量表四元组：**(分频 Hz, 抖动比, 4-8k 增益 dB, 补气声倍率)** ----------
#   分频 0 = 不做分频抖动；抖动比 0 = 不抖；增益 0 = 不削；补气声 0 = 不补。
# ★ 排序原则：**副作用从小到大**。
#     1) 补气声是加性噪声，不动谐波相干性 ⇒ 代价最小，排最前；
#     2) 补气声若把 4-8k 顶高了，配一点整首削（B）；
#     3) 分频抖动（C）是**打散谐波 = 沙哑的直接来源**，降到最后手段。
STEPS = (
    # ↑ 补气声优先，按植入百分比从轻往上，最后一档就是 BREATH_MAX_INSERT。
    #   ★ 低端要密：不同素材"补多少才够"能差 2.5 倍（实测合成素材 5~8% 就够了，
    #     真实素材要到 25% 往上）。粒度不够就会在「没达标」和「压过头」之间来回跳。
    (0.0, 0.000, 0.0, 0.02),
    (0.0, 0.000, 0.0, 0.05),
    (0.0, 0.000, 0.0, 0.08),
    (0.0, 0.000, 0.0, 0.12),
    (0.0, 0.000, 0.0, 0.16),
    (0.0, 0.000, 0.0, 0.20),
    (0.0, 0.000, 0.0, 0.25),
    # ↓ 只削 4-8k：补气声压不动时（4-8k 那一条单独超标）只能靠它。
    #   ★ 注意它会让 HNR **反升**（削掉高频后剩下的谐波更干净），
    #     所以"HNR 高"这条它救不了 —— 排在补气声之后就是这个原因。
    (0.0, 0.000, -4.0, 0.0),
    (0.0, 0.000, -4.0, 0.15),
    (1500.0, 0.008, 0.0, 0.15),
    (1500.0, 0.010, 0.0, 0.20),
    (1500.0, 0.012, -4.0, 0.25),
)
BAND_EDGE = 600.0    # 分频过渡带（Hz）。硬切会在时域振铃，等于换一种怪声
SEED = 1234          # 固定种子：同一首歌每次修出来必须一样，否则没法 A/B
CUT_HZ = 600.0       # 抖动带宽。实测 600Hz 最接近真人（150/300 太慢像颤音，
#                      1200 已接近逐周期，会开始像加噪声）
EQ_LO, EQ_HI, EQ_EDGE = 4000.0, 8000.0, 1200.0   # 4-8k 架式的范围与过渡带

# ---- 削局部过冲（手段 E）----
OS_TARGET = 2.0        # 逐窗口 4-8k 相对原唱的倍数压到这个以内
OS_MAX_ATTEN = 18.0    # 单个窗口最多削这么多 dB
OS_WIN, OS_HOP = 0.050, 0.0125
OS_ATTACK, OS_RELEASE = 0.030, 0.120   # 增益下降快、回升慢（跟压缩器同理，防抽水）
OS_VOICE_PCT = 40      # 能量低于这个分位的窗口不参与（静音段的比值没有意义）
OS_PEAK_HZ = 300.0     # 判「峰 vs 谷」的半宽（只削谐波尖峰、尽量留噪声底）

# ---- 补气声（手段 D）----
# ★ 剂量单位 = **植入百分比**（补进去的材料 RMS ÷ 成品 RMS），**不是倍数**。
#   倍数量纲依赖参考轨能量，同一句 "3.0" 在不同素材上塞进去的东西能差一倍，
#   没法设上限；百分比是听感的直接代理，上限天然就是剂量表的最后一档。
BREATH_MAX_INSERT = 0.25
#   依据（实测，不是猜的）：参考轨 HPSS 的 P RMS 0.0367 / H RMS 0.1600 = 23%
#   —— 真人的非谐波成分大约是谐波的 23%。补得比这还多 = 比真人还吵。
#   老板第五次反馈「杂音很严重」的那一版，实测植入 **83%**（超上限 3.3 倍）。
HPSS_MARGIN = 2.0        # HPSS 分离谐波/非谐波的余量
BREATH_SMOOTH_SEC = 0.15  # 去脉冲的幅度平滑窗（秒）
# ...
# ★ 补的气声**一概低通到 BREATH_LP_HZ 以下**（无条件，不做自适应）。
#   它在第五轮经历了一次"删掉又加回来"，把两次的依据都记在这里：
#     删时的依据（错）：扫描结果显示低通4k达标要植入 83.0%、全带 86.2%
#       —— 差别在误差内，看着像纯负债。
#     加回来的依据（对）：那次扫描是在**去脉冲之前**做的。加了去脉冲之后重测，
#       结论反转了 —— 真实素材 4k 6.50dB vs 全带 6.98dB（4k 反而更好），
#       而 hf_rel 这一项差别是决定性的：
#           4-8k 超标素材（hf_rel 6.51）全带补 ⇒ hf_rel 涨到 **12.15**（翻倍）
#                                       低通4k ⇒ 6.54（纹丝不动）
#           4-8k 宽裕素材（hf_rel 0.62）两者都安全
#       ⇒ 低通在 ΔHNR 上不劣、在 hf_rel 上明显更稳，没有理由分素材开关。
#   ★ 教训：改了手段（去脉冲）就必须**重测**原来的结论，不能沿用旧数据。
BREATH_LP_HZ = 4000.0
#   ★ 平滑窗的作用是"把孤立脉冲摊平"，所以它的量纲要跟一个瞬态同量级：
#     太小（<50ms）摊不平爆破音，太大（>400ms）会把气声本身的起伏也抹掉。
BREATH_GATE_DB = -24.0   # 成品低于自己峰值这么多 dB ⇒ 视为没在唱，不补
BREATH_KNEE_DB = 18.0    # 从"没在唱"过渡到"全量补"的宽度
# ---- 次优采纳：达不了标时，要不要给"改善了但没修好"的那一档 ----
PARTIAL_MIN_GAIN = 3.0
#   实测依据（2026-10-24 第五轮）：这首 Schmidt ΔHNR 12.47，补气声到上限
#   25% 只能压到 6.43 —— 降了 6dB（电音感减半），而 jitter 1.30×（离沙哑线
#   1.6× 还远）。放着不给＝老板听到的还是 12.47 的电音；给了＝6.43。
#   ⇒ 只要副作用全部合格、且**实打实降了 3dB 以上**，就给这一档，并照实报。
#   低于 3dB 的不值得为此掺东西（听不出来，平白多一道处理）。

# ---- 副作用护栏：修可以，不许修成另一个毛病 ---------------------------------
# ★ 基准是「**处理前的成品**」，不是原唱。护栏管的是"我的处理弄坏了什么"，
#   不是"成品离原唱还差多少"—— 后者是 RVC 自己的账，不该由这一步来背。
#   实测踩过：原唱 2-4k 本来就比 RVC 输出高 2.4dB，拿原唱当基准的话
#   **每一档**都会被判"中频掉了"（连没处理的那一档也是），护栏成了摆设。
CENT_DROP_MAX = 0.10  # 谱质心最多掉这么多（相对处理前；掉了就是闷）
MF_LO, MF_HI = 2000.0, 4000.0     # 中频：咬字的主战场
MF_DROP_DB = 2.0     # 中频最多许掉这么多 dB（相对处理前；掉多了就是糊）

WIN_SEC = 300.0      # 测量上限（秒）。超过就只取前这么多 —— 见 _win() 的说明。


class _quiet(object):
    """吞掉 os.remove 的异常（收尾清理不该让主流程挂掉）。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return True


# ---------------------------------------------------------------- 基础工具
def _read(path):
    y, sr = sf.read(path, dtype="float64")
    if y.ndim > 1:
        y = y.mean(1)
    return np.asarray(y, dtype=np.float64), int(sr)


def _write(path, y, sr):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    sf.write(path, np.asarray(y, dtype=np.float32), sr)


def _norm_peak(x):
    pk = float(np.abs(x).max())
    return x / pk if pk > 1e-12 else x


def _win(n, sr):
    """测量窗口（起点秒, 时长秒）。

    ★★ 2026-10-24 第四轮：这里原来是「取中间 WIN_SEC 秒」，被实测推翻了。
       同一条轨、同一个处理：
         整首口径   breath=1.0 → ΔHNR 3.69（达标，不用抖动）
         中间30秒   breath=1.0 → ΔHNR 6.89（判电 ⇒ 逼着系统再加抖动）
       差 3.2dB —— 中间那 30 秒的气声恰好偏少，不具代表性。
       后果很实在：**判据窗口不具代表性 ⇒ 系统为了数字好看多加了抖动
       ⇒ 老板听到的就是"沙哑"**。
       ⇒ 改成从头取、按整首量（只有超长音频才截断）。老板是听整首给反馈的。
    """
    total = n / float(sr)
    if total <= WIN_SEC:
        return 0.0, total
    return 0.0, float(WIN_SEC)


def _smooth_noise(n, sr, cutoff_hz, rng):
    """带宽受限的随机游走（做 wow/flutter 的延迟轨迹用）。"""
    win = max(1, int(round(float(sr) / max(1.0, cutoff_hz))))
    raw = rng.standard_normal(n + 2 * win)
    k = np.ones(win, dtype=np.float64) / float(win)
    s = np.convolve(raw, k, mode="same")[:n]
    sd = float(np.std(s))
    return s / sd if sd > 1e-12 else np.zeros(n)


def _frames(y, nseg=16, W=8192):
    """把整条轨切成 nseg 个 8192 窗口（**均匀铺满整首**）。

    ★ 2026-10-24 第四轮：原来只取 y[:8192] —— 那是开头 0.17 秒，
      很可能是前奏静音 ⇒ 量出来的"中频/质心"跟整首毫无关系，
      护栏因此把达标的那档（补气声 α=4.0，ΔHNR 2.67）误判成"中频掉了"。
    """
    n = len(y)
    if n < W:
        return []
    step = max(1, (n - W) // max(nseg, 1))
    return [y[i:i + W] for i in range(0, max(1, n - W + 1), step)[:nseg]]


def band_db(y, sr, lo, hi):
    """[lo,hi) 频段的能量，dB（整首多窗口平均）。"""
    fr = np.fft.rfftfreq(8192, 1.0 / sr)
    m = (fr >= lo) & (fr < hi)
    if not m.any():
        return None
    es = []
    for seg in _frames(y):
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        e = float(sp[m].sum())
        if e > 0:
            es.append(e)
    if not es:
        return None
    return 10.0 * np.log10(float(np.mean(es)))


def band_rel(y, sr, lo, hi):
    """[lo,hi) 频段能量**占总能量的比例**（dB）。

    ★ 护栏必须用这个而不是 band_db 的绝对值：补气声会把峰值顶大，
      峰值归一化之后**所有频段一起下降** —— 那是整体电平变化，
      不是"中频被削掉了"。用绝对 dB 会把达标的那档误判（实测踩到）。
    """
    a = band_db(y, sr, lo, hi)
    b = band_db(y, sr, 80.0, min(16000.0, sr / 2.0 * 0.98))
    if a is None or b is None:
        return None
    return a - b


def centroid(y, sr):
    """谱质心（Hz，整首多窗口平均）。"""
    fr = np.fft.rfftfreq(8192, 1.0 / sr)
    cs = []
    for seg in _frames(y):
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        tot = float(sp.sum())
        if tot > 0:
            cs.append(float((sp * fr).sum() / tot))
    if not cs:
        return None
    return float(np.mean(cs))


# ---------------------------------------------------------------- 手段 C：抖动
def wow_flutter(y, sr, jitter_ratio, rng, f0_hz):
    """时变延迟（周期微扰 / wow-flutter）。

    用带宽 CUT_HZ 的平滑随机游走当延迟轨迹，再按它重采样 ⇒ 相邻周期不再等长。
    ★ 这就是"打散谐波"：相位偏移 2π·k·f0·τ 正比于谐波次数 k，所以高次谐波
      先被毁。副作用是 jitter 涨 ⇒ 听感偏沙哑。能不用就不用。
    """
    n = len(y)
    if n < 16 or jitter_ratio <= 0:
        return y
    period = sr / max(float(f0_hz), 1.0)
    tau = _smooth_noise(n, sr, CUT_HZ, rng) * jitter_ratio * period
    idx = np.arange(n, dtype=np.float64) - tau
    np.clip(idx, 0.0, n - 1.0, out=idx)
    return np.interp(idx, np.arange(n, dtype=np.float64), y)


def band_wow(y, sr, split_hz, jitter_ratio, rng, f0_hz):
    """**只抖 split_hz 以上的**周期微扰（分频抖动）。

    全带做会把基频也一起抖，共振峰跟着糊（中频掉、质心掉）⇒ 护栏会挡。
    按频率切开只抖上半段：
      < split = 基频 + 低次谐波 = 音高 + 共振峰 = 音色/声纹 → 不动
      > split = 高次谐波，HNR 的主战场                     → 抖它
    ★ 它比全带抖动便宜，但仍然是"打散谐波"，所以排在补气声之后（最后手段）。
    """
    import librosa
    if split_hz <= 0 or jitter_ratio <= 0:
        return wow_flutter(y, sr, jitter_ratio, rng, f0_hz)
    n_fft, hop = 2048, 512
    S = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    fr = np.fft.rfftfreq(n_fft, 1.0 / sr)
    lo_g = np.ones(len(fr), dtype=np.float64)
    up = (fr > split_hz - BAND_EDGE) & (fr < split_hz)
    lo_g[up] = 0.5 * (1.0 + np.cos(
        np.pi * (fr[up] - (split_hz - BAND_EDGE)) / BAND_EDGE))
    lo_g[fr >= split_hz] = 0.0
    hi_g = 1.0 - lo_g
    lo = librosa.istft(S * lo_g[:, None], hop_length=hop, length=len(y))
    hi = librosa.istft(S * hi_g[:, None], hop_length=hop, length=len(y))
    return lo + wow_flutter(hi, sr, jitter_ratio, rng, f0_hz)


# ---------------------------------------------------------------- 手段 B：削 4-8k
def shelf_48k(y, sr, gain_db, n_fft=4096, hop=1024):
    """4-8k 架式增益（gain_db 为负 = 削）。过渡带用 EQ_EDGE 平滑。

    ★ 注意它会让 HNR **反升**：削掉高频后剩下的谐波更干净 ⇒ 救不了 HNR 那条。
    """
    import librosa
    if not gain_db:
        return y
    S = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    fr = np.fft.rfftfreq(n_fft, 1.0 / sr)
    g = np.zeros(len(fr), dtype=np.float64)
    for lo, hi, rising in ((EQ_LO - EQ_EDGE, EQ_LO, True),
                           (EQ_HI, EQ_HI + EQ_EDGE, False)):
        m = (fr >= lo) & (fr < hi)
        if not m.any():
            continue
        u = (fr[m] - lo) / max(hi - lo, 1e-9)
        g[m] = u if rising else (1.0 - u)
    g[(fr >= EQ_LO) & (fr < EQ_HI)] = 1.0
    S *= (10.0 ** (gain_db * g / 20.0))[:, None]
    return librosa.istft(S, hop_length=hop, length=len(y))


# ---------------------------------------------------------------- 手段 D：补气声
def voice_env(y, sr, hop=512, floor_db=BREATH_GATE_DB, knee_db=BREATH_KNEE_DB):
    """成品的发声包络（0~1 的逐样本曲线）：在唱 = 1，句间/间奏 = 0。

    ★ 为什么补气声要乘它：参考轨（UVR 分离出的人声）在**没有歌唱**的地方
      也不是纯静音 —— 那里面装着分离残漏的鼓/贝斯（这首歌 leak=0.19，
      实测参考轨最安静的 20% 里，P 分量还占参考轨能量的 4.8%）。
      原样把这些加进成品 ⇒ 句间凭空多出底噪。
    ★ 0dB 参考取 **99 分位**而不是最大值：一个爆音会把整首的比例拉塌。
    """
    import librosa
    r = librosa.feature.rms(y=y, frame_length=2048, hop_length=hop)[0]
    if len(r) == 0:
        return np.ones(len(y))
    r_db = 20.0 * np.log10(np.maximum(r, 1e-12))
    pk = np.percentile(r_db, 99)
    env = np.clip((r_db - pk - floor_db) / max(1e-6, knee_db), 0.0, 1.0)
    idx = np.arange(len(env)) * hop
    return np.interp(np.arange(len(y)), idx, env)


def breath_of(y, sr, dst=None, margin=HPSS_MARGIN,
              smooth_sec=BREATH_SMOOTH_SEC, seed=SEED):
    """把参考轨的非谐波成分做成**平稳气声**，归一化到 RMS=1 后返回。

    为什么要做下面这三步（每一步都是被老板的反馈逼出来的）：

      ① HPSS 取 P 分量
         比白噪声好的原因（①号死路）：真人气声不是全带恒定的，它有跟共振峰
         同形的频谱形状和**时变包络**（只在出声时才有）。HPSS 自带这两样。

      ② **去脉冲**（本轮新增，效率翻倍）
         孤立脉冲（齿音 / 爆破音 / UVR 分离残留的鼓点）是各频率**同相叠加**
         才形成的尖峰；换成随机相位后它们摊平成高斯平稳过程 ——
         频谱形状、时变起伏都还在，尖峰没了。
         ★ 实测：同样植入 45%，去脉冲版 ΔHNR 压到 3.15，原样版只到 5.87。
           老板说的「杂音」，就是原样版那个 19.2 的峰均比。

      ③ **乘成品发声包络**（本轮新增）
         句间 / 间奏不补 —— 那段参考轨里的东西是分离残漏，不是气声。

      ④ **低通到 BREATH_LP_HZ 以下**（去脉冲之后重新验证，理由见常量注释）

    返回 RMS=1 的气声 ⇒ 调用方用 ``dst_rms * ratio`` 当剂量，语义就是
    「补进去的材料占成品百分之多少」（BREATH_MAX_INSERT 直接比得上）。
    """
    import librosa
    from scipy.ndimage import uniform_filter1d

    n_fft, hop = 2048, 512
    D = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    _H, P = librosa.decompose.hpss(D, margin=margin)
    mag = np.abs(P)

    # ② 去脉冲：幅度沿时间平滑，相位换成随机相位
    k = max(1, int(smooth_sec * sr / hop))
    mag = uniform_filter1d(mag, size=k, axis=1, mode="nearest")
    rng = np.random.default_rng(seed)
    phase = np.exp(1j * rng.uniform(-np.pi, np.pi, mag.shape))
    b = librosa.istft(mag * phase, hop_length=hop, length=len(y))

    # ④ 低通：不许把 4-8k 抬上去（那是 hf_rel 判据的地盘）
    if BREATH_LP_HZ and BREATH_LP_HZ < sr / 2.0:
        try:
            from scipy.signal import butter, filtfilt
            _b, _a = butter(8, BREATH_LP_HZ / (sr / 2.0), btype="low")
            b = filtfilt(_b, _a, b)
        except Exception:
            pass

    # ③ 只补在成品出声的地方
    if dst is not None and len(dst) >= len(b):
        n = min(len(b), len(dst))
        b = b[:n] * voice_env(dst[:n], sr, hop=hop)

    r = float(np.sqrt((b ** 2).mean()))
    return b / r if r > 1e-12 else b


# ---------------------------------------------------------------- 手段 E：削过冲
def _hf_ratio_track(y, sr, win=OS_WIN, hop=OS_HOP):
    """逐窗 4-8k / 80-8k 的能量比。返回 (时刻, 比值, RMS)。"""
    W = int(win * sr)
    H = int(hop * sr)
    if W < 64 or len(y) < W:
        return np.zeros(1), np.zeros(1), np.zeros(1)
    ff = np.fft.rfftfreq(W, 1.0 / sr)
    m_hi = (ff >= EQ_LO) & (ff < EQ_HI)
    m_tot = (ff > 80.0) & (ff < EQ_HI)
    ts, hrs, rms = [], [], []
    for i in range(0, max(1, len(y) - W), H):
        seg = y[i:i + W]
        sp = np.abs(np.fft.rfft(seg * np.hanning(W))) ** 2
        tot = float(sp[m_tot].sum())
        ts.append(i / float(sr))
        rms.append(float(np.sqrt((seg ** 2).mean())))
        hrs.append((float(sp[m_hi].sum()) / tot) if tot > 0 else 0.0)
    return np.asarray(ts), np.asarray(hrs), np.asarray(rms)


def _smooth_env(g, hop_s, attack, release):
    """一阶 IIR：下降快（attack）回升慢（release）—— 跟压缩器同理，防抽水。"""
    a_a = np.exp(-hop_s / max(attack, 1e-6))
    a_r = np.exp(-hop_s / max(release, 1e-6))
    out = np.empty_like(g)
    v = g[0] if len(g) else 0.0
    for i, x in enumerate(g):
        a = a_a if x < v else a_r
        v = a * v + (1.0 - a) * x
        out[i] = v
    return out


def de_overshoot(y, sr, yref, target=OS_TARGET, max_atten=OS_MAX_ATTEN):
    """削「4-8k 局部远超原唱」的窗口（修破音），其余窗口原样通过。

    返回 (处理后, 增益曲线 dB)。

    ★ 为什么必须**逐窗口**而不是整首统一：整首平均会把局部尖峰稀释掉
      （实测整首只 ×1.29，但逐窗 p99 = 190 倍）。按整首统一处理，
      对本来正常的窗口就是过度处理 ⇒ 全曲沙哑。
    ★ 为什么在 STFT 域逐 bin 施加：试过 butter 8 阶分频，标称削 12dB
      实际只掉 5.6dB —— 过渡带太宽，4k 附近的能量一半还留在低通里。
    """
    import librosa
    ts, hr_c, rms_c = _hf_ratio_track(y, sr)
    _, hr_r, rms_r = _hf_ratio_track(yref, sr)
    m = min(len(ts), len(hr_r))
    if m < 8:
        return y, np.zeros(max(len(ts), 1))
    hr_c, hr_r = hr_c[:m], hr_r[:m]
    rms_c, rms_r = rms_c[:m], rms_r[:m]
    ts = ts[:m]

    thr_c = np.percentile(rms_c, OS_VOICE_PCT)
    thr_r = np.percentile(rms_r, OS_VOICE_PCT)
    voiced = (rms_c >= thr_c) & (rms_r >= thr_r)

    ratio = np.where(voiced, hr_c / np.maximum(hr_r, 1e-6), 0.0)
    # ★ 只削到「原唱的 target 倍」为止，绝不削到低于原唱（削过头就闷了）
    # ★ np.where 会**两边都算**：ratio=0（静音窗）也进 log10 ⇒ -inf + RuntimeWarning
    #   刷屏，看着像出错。先夹一下底。
    over = np.where(ratio > target,
                    10.0 * np.log10(np.maximum(ratio, 1e-12) / target), 0.0)
    need = np.clip(over, 0.0, max_atten)
    need = _smooth_env(need, OS_HOP, OS_ATTACK, OS_RELEASE)
    if float(need.max()) < 0.5:
        return y, need

    n_fft, hop = 2048, 512
    S = librosa.stft(y, n_fft=n_fft, hop_length=hop)
    fr = np.fft.rfftfreq(n_fft, 1.0 / sr)
    m_band = (fr >= EQ_LO) & (fr < EQ_HI)
    ft = (np.arange(S.shape[1]) * hop) / float(sr)
    g_db = np.interp(ft, ts, need, left=need[0], right=need[-1])
    g = 10.0 ** (-g_db / 20.0)

    # 只削「谐波尖峰」、尽量留「噪声底」（气声住在噪声底里）。
    # ★ 实测：这一层对**宽带噪声**也一样有效（噪声在 STFT 域有随机起伏），
    #   所以救不了"补气声被削"的问题 —— 真正的解法是顺序（见 cmd_auto 注释）。
    try:
        from scipy.ndimage import minimum_filter1d
        mag = np.abs(S[m_band, :])
        ph = S[m_band, :] / np.maximum(mag, 1e-12)
        nbin = max(3, int(round(OS_PEAK_HZ / (sr / float(n_fft)))))
        floor = minimum_filter1d(mag, size=nbin, axis=0, mode="nearest")
        peak = np.clip(mag - floor, 0.0, None)
        S[m_band, :] = (floor + peak * g[None, :]) * ph
    except Exception:
        S[m_band, :] *= g[None, :]
    out = librosa.istft(S, hop_length=hop, length=len(y))
    return out, need


# ---------------------------------------------------------------- 判据
def verdict(base, cand, hnr_db, hf_rel, min_dh=HF_REL_MIN_DH):
    """判「电不电」：两条独立判据，各自成立都算。

    hnr 那条：ΔHNR ≥ hnr_db。
    hf  那条：4-8k ≥ 原唱的 hf_rel 倍**且** ΔHNR ≥ min_dh。
      ★ 为什么要"且"：实测降八度时 HNR 反而低 4.6dB，但 4-8k 照样翻 2.9 倍
        —— 那是正常的低频堆积，不是电。单独用 4-8k 会误判。
    """
    b_h = base.get("hnr_db")
    c_h = cand.get("hnr_db")
    dh = None
    if b_h is not None and c_h is not None:
        try:
            dh = float(c_h) - float(b_h)
        except (TypeError, ValueError):
            dh = None
    b_f = base.get("hf_ratio")
    c_f = cand.get("hf_ratio")
    rel = None
    if b_f and c_f:
        try:
            rel = float(c_f) / float(b_f)
        except (TypeError, ValueError, ZeroDivisionError):
            rel = None
    bad = False
    if dh is not None and dh >= hnr_db:
        bad = True
    if rel is not None and rel >= hf_rel and dh is not None and dh >= min_dh:
        bad = True
    return bad, dh, rel


# ---------------------------------------------------------------- 主流程
def cmd_auto(cand, ref, out_wav, out_json, hnr_db=None, hf_rel=None,
             steps=None, seed=None, force=False):
    hnr_db = HNR_DB if hnr_db is None else float(hnr_db)
    hf_rel = HF_REL if hf_rel is None else float(hf_rel)
    seed = SEED if seed is None else int(seed)
    if steps is None:
        seq = STEPS
    else:
        seq = []
        for tok in str(steps).split(";"):
            tok = tok.strip()
            if not tok:
                continue
            parts = [s.strip() for s in tok.split(",")]
            # 兼容老写法：三元组 "1500,0.010,0"、二元组 "0.006,-4"
            if len(parts) >= 4:
                seq.append((float(parts[0]), float(parts[1]),
                            float(parts[2]), float(parts[3])))
            elif len(parts) == 3:
                seq.append((float(parts[0]), float(parts[1]),
                            float(parts[2]), 0.0))
            else:
                seq.append((0.0, float(parts[0]), float(parts[1]), 0.0))
        if not seq:
            seq = STEPS

    res = {"ok": False, "applied": False, "tried": [], "chosen": None}
    try:
        y, sr = _read(cand)
        yref, srref = _read(ref)
    except Exception as e:
        res["reason"] = "读不了音频：%s: %s" % (type(e).__name__, e)
        _dump(out_json, res)
        sys.stdout.write("DEMETAL_FAIL read_error\n")
        return 1

    # ---- 参考轨对齐：采样率不同必须先重采样 --------------------------------
    #   ★ 实测踩到：原唱是分离出来的 44.1k、成品是 RVC 出的 48k。
    #     不重采样的话「逐窗口比值」比的是两个不同时间轴 ⇒ 全是错的
    #     （症状就是过冲修正静默跳过，只留下补气声那条路）。
    yref_al = np.asarray(yref, dtype=np.float64)
    if srref != sr:
        try:
            import librosa
            yref_al = librosa.resample(yref_al, orig_sr=srref, target_sr=sr)
            res["ref_resampled"] = "%d -> %d" % (srref, sr)
        except Exception as e:
            res["ref_resample_err"] = "%s: %s" % (type(e).__name__, e)
    _n = min(len(y), len(yref_al))
    yref_al = yref_al[:_n]

    t0, dur = _win(min(len(y), len(yref)), sr)
    t0r, durr = _win(len(yref), srref)
    # 两轨取样窗口必须一致，否则量出来的差是"取样差"不是"音色差"
    t0, dur = min(t0, t0r), min(dur, durr)

    base = artifact_cli.measure(ref, t0, dur)
    before = artifact_cli.measure(cand, t0, dur)
    if not base.get("ok") or not before.get("ok"):
        res["reason"] = "量不出电音指标（有声部分太少？）"
        res["before"] = before
        _dump(out_json, res)
        sys.stdout.write("DEMETAL_FAIL measure_error\n")
        return 1

    bad, dh0, rel0 = verdict(base, before, hnr_db, hf_rel)
    res.update({"ok": True, "before": before, "ref": base,
                "dhnr": dh0, "hf_rel": rel0, "metal": bad, "force": bool(force)})
    # ★ force 只是**跳过"判不判电"这一步**（老板听到了电、但尺子说不电时用），
    #   副作用护栏照旧生效 —— "强制"不是"允许把声音弄坏"。

    pk0 = float(np.abs(y).max()) or 1.0
    f0 = before.get("f0_med_hz") or 200.0
    # 护栏基准 = 处理前的成品自己（不是原唱，理由见上面的常量注释）
    base_cent = centroid(y, sr)
    # ★ 中频护栏用**相对比例**（band_rel），不用绝对 dB —— 理由见 band_rel 的注释
    base_mid = band_rel(y, sr, MF_LO, MF_HI)
    # ★★ 基准必须是 **before（处理前的成品）**，不能是 base（原唱参考轨）。
    #   2026-10-24 第五轮实测：写成 base.get(...) 时，这一句等于
    #     ref jitter 0.02197 × 1.6 = 0.0351 ⇒ 抖动档的 0.01796 轻松过关
    #   护栏形同虚设，于是系统甩开补气声去用抖动（它能达标），
    #   代价是 jitter 0.00828 → 0.01796（**+117%**）—— 比老板投诉的那版还糟。
    #   JIT_MAX_REL=1.6 这个数本身有依据（第四轮实测：沙哑的抖动版是 1.58×、
    #   合格档 1.42×），但那个 1.6 是**相对成品自己**量出来的。
    base_jit = before.get("jitter_local")
    base_rms = float(np.sqrt((y ** 2).mean())) or 1.0
    res["cent_before"] = round(base_cent, 0) if base_cent else None
    res["mid_before"] = base_mid
    # ★ 质心的**下限要参考原唱**：RVC 输出的质心本来就比原唱高一大截
    #   （实测 2628 vs 原唱 1836 —— 那个"亮"就是电音/刺耳的一部分）。
    #   补气声把质心压回 2036 是**往真人的方向修**，不是"闷"。
    #   只用"相对处理前不许掉 10%"当护栏的话，会把达标的那档（ΔHNR 2.67）
    #   误判成闷掉 ⇒ 系统转去用抖动 ⇒ 老板听到的就是沙哑。
    ref_cent = centroid(yref_al, sr)
    res["cent_ref"] = round(ref_cent, 0) if ref_cent else None
    cent_floor = None
    if base_cent or ref_cent:
        cands = [c for c in (base_cent, ref_cent) if c]
        cent_floor = min(cands) * (1.0 - CENT_DROP_MAX)

    # ---- 过冲修正（修「破音」）：必须在补气声**之前**做 ------------------
    # ★ 顺序反了会互相抵消，实测踩到：
    #     先补气声再削过冲 ⇒ breath=1.0 的 ΔHNR 只有 6.69（不达标）
    #     先削过冲再补气声 ⇒ 同一档 ΔHNR 3.69（达标）
    #   原因：过冲修正是**按 hf_ratio 相对原唱削的**。补气声会把 4-8k 抬上去
    #   （气声本来就含高频），于是削得更多 —— 把刚补进去的气声又削掉了，白补。
    ov_applied = False
    y_ov = y
    try:
        y_ov, ov_gain = de_overshoot(y, sr, yref_al)
        ov_applied = bool(float(np.abs(ov_gain).max()) >= 0.5)
        res["overshoot"] = {
            "applied": ov_applied,
            "max_atten_db": round(float(np.max(ov_gain)), 2),
            "active_pct": round(float((ov_gain > 0.5).mean() * 100), 1),
        }
    except Exception as e:
        res["overshoot"] = {"applied": False,
                            "err": "%s: %s" % (type(e).__name__, e)}
    z_base = y_ov if ov_applied else y

    # 补气声：参考轨的非谐波成分做成的平稳气声（RMS=1），只算一次。
    # ★ dst 传 z_base（过冲修正后的成品）：包络要跟着**最终要加到的那条轨**走。
    breath = None
    if any(s[3] > 0 for s in seq):
        try:
            breath = breath_of(yref_al, sr, dst=z_base, seed=int(seed))
            if len(breath) > len(z_base):
                breath = breath[:len(z_base)]
            elif len(breath) < len(z_base):
                breath = np.pad(breath, (0, len(z_base) - len(breath)))
        except Exception as e:
            res["breath_err"] = "%s: %s" % (type(e).__name__, e)

    # ---- 不电：电音那一步不动手；但**过冲（破音）照样修** ----------------
    #   ★ 两者是独立的毛病。老板 2026-10-24 反馈的「46 秒那边破音」就属于后者，
    #     而整首判据说"不电" —— 只判电的话会漏掉它。
    if not bad and not force:
        if not ov_applied:
            res["reason"] = ("这一版不电（ΔHNR %+.2f dB < %.1f；4-8k ×%.2f < %.1f），"
                             "也没有局部过冲，没动"
                             % (dh0 or 0.0, hnr_db, rel0 or 0.0, hf_rel))
            _dump(out_json, res)
            sys.stdout.write("DEMETAL_SKIP not_metal dhnr=%.2f hf_rel=%.2f\n"
                             % (dh0 or 0.0, rel0 or 0.0))
            return 0
        _write(out_wav, _norm_peak(y_ov) * pk0, sr)
        after = artifact_cli.measure(out_wav, t0, dur)
        _, dh2, rel2 = verdict(base, after, hnr_db, hf_rel)
        # ★ 这两个字段必须补上：studio 就是拿它们写步骤条的。以前只有"正常路径"
        #   补写，这两个分支漏了 ⇒ 步骤条落到 `_how` 为空 ⇒ 打印成
        #   **「已去电音：未记录手段」** —— 干了对自己都说不清的事。
        #   （实测《Everywhere You Look》《Summer Vibe》两首都打出过这句。）
        res.update({"applied": True, "mode": "overshoot", "after": after,
                    "after_dhnr": dh2, "after_hf_rel": rel2,
                    "band": 0.0, "jitter": 0.0, "eq_db": 0.0, "breath": 0.0})
        _dump(out_json, res)
        sys.stdout.write(
            "DEMETAL_APPLIED applied=1 mode=overshoot dhnr=%.2f hf_rel=%.2f "
            "max_atten=%.1f active_pct=%.1f\n"
            % (dh0 or 0.0, rel0 or 0.0,
               (res.get("overshoot") or {}).get("max_atten_db") or 0.0,
               (res.get("overshoot") or {}).get("active_pct") or 0.0))
        return 0

    chosen = None
    # 次优解：**副作用合格但没达标**的那一档里最好的（见下面的次优采纳）。
    best = None
    for i, (band, jr, gdb, br) in enumerate(seq):
        rng = np.random.default_rng(seed)
        z = z_base
        if br > 0:
            if breath is None:
                res["tried"].append({"breath": br, "err": "取不出气声成分"})
                continue
            if br > BREATH_MAX_INSERT + 1e-9:
                # ★ 上限是**结构性**的：用百分比当剂量之后，"超上限"这件事
                #   在调用方就等价于"给了一个不在允许范围内的档"。这里再拦一次，
                #   免得以后有人手改 STEPS 时把 0.3 写进去而谁都没发现。
                res["tried"].append({"breath": br, "err": "超过植入上限 %.2f"
                                     % BREATH_MAX_INSERT})
                continue
            z = z + (br * base_rms) * breath
        if jr > 0:
            z = (band_wow(z, sr, band, jr, rng, f0) if band > 0
                 else wow_flutter(z, sr, jr, rng, f0))
        if gdb:
            z = shelf_48k(z, sr, gdb)
        z = _norm_peak(z) * pk0
        # ★ 临时文件名的扩展名必须是 .wav：写成 out.wav.try0.wav 的话
        #   libsndfile 认不出格式，直接抛 LibsndfileError（实测踩到）。
        _b, _e = os.path.splitext(out_wav)
        trial = "%s_try%d%s" % (_b, i, _e or ".wav")
        try:
            _write(trial, z, sr)
        except Exception as e:
            res["tried"].append({"band": band, "jitter": jr, "eq_db": gdb,
                                 "breath": br,
                                 "err": "写文件失败 %s" % type(e).__name__})
            continue
        m = artifact_cli.measure(trial, t0, dur)
        if not m.get("ok"):
            with _quiet():
                os.remove(trial)
            res["tried"].append({"band": band, "jitter": jr, "eq_db": gdb,
                                 "breath": br, "err": "量不出来"})
            continue

        # ---- 判据：这一档修完之后，还判不判电？ ----
        still, dh, rel = verdict(base, m, hnr_db, hf_rel)

        # ---- 双边判据：既不许「电」，也不许「沙哑」--------------------------
        #   2026-10-24 第四轮实测：只设上限 ⇒ 系统一路把 HNR 往下压，压到老板
        #   反馈「全过程沙哑」。HNR 是双面指标，必须落在区间里。
        floor_ok = True if dh is None else (dh >= HNR_FLOOR)
        jit_now = m.get("jitter_local")
        jit_ok = True
        if base_jit and jit_now:
            # ★ 相对（不许比处理前涨太多）+ 绝对（小到一定程度就不算沙哑）
            jit_ok = (float(jit_now) <= float(base_jit) * JIT_MAX_REL
                      or float(jit_now) <= JIT_ABS_MAX)
        hoarse = (not floor_ok) or (not jit_ok)
        # ---- 副作用护栏：不电了但闷了/糊了，这一档也不算数 ----
        ct = centroid(z, sr)
        mid = band_rel(z, sr, MF_LO, MF_HI)
        cent_ok = True
        mid_ok = True
        if cent_floor and ct:
            cent_ok = ct >= cent_floor
        if base_mid is not None and mid is not None:
            mid_ok = (base_mid - mid) <= MF_DROP_DB
        # ★ insert = 这一档实际塞进成品多少外来材料（占成品 RMS 的比例）。
        #   它是"会不会变成杂音"的**直接代理**，必须记下来 —— 只看 ΔHNR 的话，
        #   系统会为了达标一路加料，加到 83% 老板就听到了（第五轮）。
        _ins = round(br, 4) if br > 0 else 0.0
        rec = {"band": band, "jitter": jr, "eq_db": gdb, "breath": br,
               "insert": _ins,
               "dhnr": round(dh, 2) if dh is not None else None,
               "hf_rel": round(rel, 2) if rel is not None else None,
               "metal": still, "hoarse": hoarse,
               "floor_ok": floor_ok, "jit": jit_now, "jit_ok": jit_ok,
               "cent": round(ct, 0) if ct else None,
               "cent_ok": cent_ok, "mid": mid, "mid_ok": mid_ok}
        res["tried"].append(rec)
        # ★★ force 的语义就在这一句：**跳过"达没达标"，副作用护栏一条不少**。
        #   ⇒ "达标"这条不看了，但 hoarse / cent_ok / mid_ok 全保留。
        #   以前 force 是把 still 硬改成 False，效果上等于"第一档就用"——
        #   于是紧紧贴着最小剂量走，跟 auto 找最优解完全两条路；更要命的是
        #   studio 会给 force 钉死一个剂量（0.15），而不同素材的安全剂量能差
        #   2.5 倍 ⇒ 在有些素材上直接压过头（合成素材 0.15 ⇒ ΔHNR −8.71）。
        #   ⇒ 现在 force 就是"相信你的耳朵、但仍然不许修坏"。
        _ok = ((not hoarse) and cent_ok and mid_ok
               and (force or not still))
        if _ok:
            # auto：第一个达标且副作用全合格的档。
            # force：第一个**副作用合格**的档就停（从最轻的那档开始试）。
            chosen = (band, jr, gdb, br)
            res["chosen"] = [band, jr, gdb, br]
            with _quiet():
                os.replace(trial, out_wav)
            break
        # ---- 没过关。但"没过关"有好几种，得分开看 ----------------------
        #   ★ 只有「只是没达标、副作用全部合格」的那种才配当**次优解**：
        #     达标是为了数字好看，护栏是为了耳朵。耳朵过了、数字没到的那一档，
        #     比"什么都不做"更接近老板想要的（实测 12.47 → 6.43，电音感减半，
        #     而 jitter 只到 1.30×，离沙哑线的 1.6× 还很远）。
        #     ⇒ 留着，最后要是没找到真正达标的，就给这一档。
        _all_ok = (not hoarse) and cent_ok and mid_ok and (dh is not None)
        if _all_ok and still:
            if best is None or dh < best[0]:
                if best is not None:
                    with _quiet():
                        os.remove(best[1])
                best = (dh, trial, rec, (band, jr, gdb, br))
            else:
                with _quiet():
                    os.remove(trial)
        else:
            with _quiet():
                os.remove(trial)

    # ---- 次优采纳：一档都没真正达标，但有一档"副作用合格且压下去不少" ----
    if chosen is None and best is not None and dh0 is not None:
        gain = dh0 - best[0]
        if gain >= PARTIAL_MIN_GAIN:
            with _quiet():
                os.replace(best[1], out_wav)
            chosen = best[3]
            res["chosen"] = list(chosen)
            res["partial"] = True

    if chosen is None:
        # 电音修不动，但**过冲还是能修**（它是另一回事，零副作用）。
        # ⇒ 如实报"电音没修好"，同时把能修的修掉，别两个都放弃。
        if not ov_applied:
            # ★ 所有剂量都压不下去（或压下去的代价过不了护栏）⇒ **不动手**。
            #   糊掉声音换一个好看的数字，比如实报"没修好"更糟。
            # ★ 这条是最容易往外走的一条路，措辞必须让老板知道**下一步该怎么办**
            #   —— 光说"没修好"，他只会再点一次开跑（实测第五轮就是这样轮回来的）。
            _need = ((dh0 or 0.0) - hnr_db)
            res["reason"] = (
                "这一版的电音太重（ΔHNR %+.1f dB，要再压 %.1f dB 才算修好），"
                "在不把它弄成杂音的前提下**修不动**："
                "补气声最多只能补到成品自身能量的 %d%%（再多就比真人还吵 —— "
                "「杂音」就是这么来的），到顶也只能压到 ΔHNR %+.1f。"
                "不是这一轮没调好：**换音区更接近的角色**才是有效的办法"
                "（音高体检已经把各档都试过了，会直接告诉你该换谁）。"
                % (dh0 or 0.0, _need if _need > 0 else 0.0,
                   int(BREATH_MAX_INSERT * 100),
                   min((t.get("dhnr") or 99.0)
                       for t in res["tried"] if t.get("dhnr") is not None))
                if res["tried"] else
                "这一版的电音太重（ΔHNR %+.1f dB），没有可用剂量。" % (dh0 or 0.0))
            _dump(out_json, res)
            sys.stdout.write("DEMETAL_SKIP no_safe_dose dhnr=%.2f hf_rel=%.2f "
                             "tried=%d\n" % (dh0 or 0.0, rel0 or 0.0, len(seq)))
            return 0
        _write(out_wav, _norm_peak(y_ov) * pk0, sr)
        after = artifact_cli.measure(out_wav, t0, dur)
        _, dh2, rel2 = verdict(base, after, hnr_db, hf_rel)
        res.update({"applied": True, "mode": "overshoot_only",
                    "after": after, "after_dhnr": dh2, "after_hf_rel": rel2,
                    "band": 0.0, "jitter": 0.0, "eq_db": 0.0, "breath": 0.0,
                    "reason": ("电音没找到安全剂量（试了 %d 档），只修了局部过冲"
                               % len(seq))})
        _dump(out_json, res)
        sys.stdout.write("DEMETAL_APPLIED applied=1 mode=overshoot_only "
                         "dhnr=%.2f->%.2f tried=%d\n"
                         % (dh0 or 0.0, dh2 or 0.0, len(seq)))
        return 0

    # ---- 闭环：对最终落盘的那个文件再量一次 ----
    after = artifact_cli.measure(out_wav, t0, dur)
    ok2, dh2, rel2 = verdict(base, after, hnr_db, hf_rel)
    res["applied"] = True
    res["after"] = after
    res["band"] = chosen[0]
    res["jitter"] = chosen[1]
    res["eq_db"] = chosen[2]
    res["breath"] = chosen[3]
    res["after_dhnr"] = dh2
    res["after_hf_rel"] = rel2
    res["dhnr_gain"] = (round(dh0 - dh2, 2)
                        if (dh0 is not None and dh2 is not None) else None)
    res["hf_gain"] = (round(rel0 - rel2, 2)
                      if (rel0 is not None and rel2 is not None) else None)
    # ★ reason 会被 studio 拿去当步骤提示给老板看，force 档没回退就**不许写"已回退"**
    #   （实测：force + 只削档跑出来 wav 还在，提示却说"已按不修回退" —— 这句是假的）。
    if res.get("partial"):
        # ★ partial（次优采纳）：它骗不过自己的尺子 —— 复测必定还判电。
        #   这时**不许回退**也**不许写成"已去电音"**：回退＝老板听到原来那个
        #   12.47dB 的电音，写"已去电音"＝骗人。照实说压到哪儿、为什么不再往下。
        #   （2026-10-24 第五轮：以前这两种出口统一写成"已按不修回退"，
        #    于是成品上一个字没提"还有电"，老板只能靠耳朵发现，再来一轮反馈。）
        res["reason"] = (
            "电音**压下去了一半，没压干净**：ΔHNR %.1f → %.1f dB（目标 %.1f 以内）。"
            "再往下只剩两条路：继续加气声会超过成品自身能量的 %d%%（那就成了"
            "比真人还吵的杂音 —— 「杂音」正是这么来的）；改用抖动则会把 jitter "
            "顶上去（超过 %.1f 倍就是沙哑）。两条都没走。"
            "**换音区更接近的角色**才是根治的办法，音高体检会告诉你该换谁。"
            % (dh0 or 0.0, dh2 or 0.0, hnr_db, int(BREATH_MAX_INSERT * 100),
               JIT_MAX_REL))
    elif ok2 and force:
        res["reason"] = "修完了，但复测仍判电（force 档按你的要求保留，没回退）"
    elif ok2:
        res["reason"] = "修完了但复测仍判电（已按不修回退）"
    else:
        res["reason"] = "修完了"
    # ★ partial 是**唯一允许"复测还电却保留"**的情况：它是明知的结果、
    #   并且已经在 reason 里说清楚了。force 同理（用户明确要求照修）。
    if ok2 and not force and not res.get("partial"):
        # 复测还判电说明我们看走了眼 —— 这时候不能装作修好了，回退原轨。
        # （force 档例外：那是用户明确要求"照修"，修不到位也如实留下并写进报告。）
        res["applied"] = False
        with _quiet():
            os.remove(out_wav)
        sys.stdout.write("DEMETAL_SKIP recheck_still_metal dhnr=%.2f hf_rel=%.2f\n"
                         % (dh2 or 0.0, rel2 or 0.0))
        _dump(out_json, res)
        return 0
    _dump(out_json, res)
    # ★ 打印里带上 insert（真金白银塞进去多少）：这一行是排查"杂音"时唯一
    #   用得上的东西 —— 光看 breath 的倍数是看不出代价的（同一句 α=3.0，
    #   在不同素材上植入量能从 40% 到 83%，差一倍）。
    sys.stdout.write(
        "DEMETAL_APPLIED applied=1%s band=%.0f jitter=%.4f eq_db=%.0f breath=%.2f "
        "insert=%.1f%% dhnr=%.2f->%.2f hf_rel=%.2f->%.2f jitter_local=%s->%s\n"
        % (" mode=partial" if res.get("partial") else "",
           chosen[0], chosen[1], chosen[2], chosen[3],
           100.0 * float(chosen[3] or 0.0),
           dh0 or 0.0, dh2 or 0.0, rel0 or 0.0, rel2 or 0.0,
           before.get("jitter_local"), after.get("jitter_local")))
    return 0


def _jsonable(o):
    """numpy 标量 json.dumps 不认 —— 报告里全是 np.float64。

    ★ 为什么这条很要紧（2026-10-24 第四轮重建时踩到）：dumps 抛 TypeError 会被
      下面的 except 吞掉 ⇒ **留一个 0 字节的 json**。调用方（studio.demetal）
      读到空文件会当成"没跑通"，于是明明修好了也被判成没修。
      ⇒ 兜底转换 + 失败时至少写个能解析的最小报告，绝不留下空文件。
    """
    try:
        return float(o)
    except Exception:
        pass
    try:
        return int(o)
    except Exception:
        pass
    return str(o)


def _dump(path, obj):
    try:
        d = os.path.dirname(path)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        with io.open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1,
                               default=_jsonable))
    except Exception:
        try:
            with io.open(path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"ok": False, "applied": False,
                                    "reason": "报告序列化失败"},
                                   ensure_ascii=False))
        except Exception:
            pass


def _opts(argv):
    d = {}
    i = 0
    while i < len(argv):
        k = argv[i]
        if k == "--hnr-db" and i + 1 < len(argv):
            d["hnr_db"] = argv[i + 1]
            i += 2
        elif k == "--hf-rel" and i + 1 < len(argv):
            d["hf_rel"] = argv[i + 1]
            i += 2
        elif k == "--seed" and i + 1 < len(argv):
            d["seed"] = argv[i + 1]
            i += 2
        elif k == "--steps" and i + 1 < len(argv):
            d["steps"] = argv[i + 1]
            i += 2
        elif k == "--force":
            d["force"] = True
            i += 1
        else:
            i += 1
    return d


def main():
    a = sys.argv[1:]
    if len(a) >= 5 and a[0] == "auto":
        return cmd_auto(a[1], a[2], a[3], a[4], **_opts(a[5:]))
    sys.stdout.write("usage: demetal_cli.py auto <成品> <原唱> <out.wav> <out.json>"
                     " [--hnr-db 4.0] [--hf-rel 2.0] [--seed 1234] [--force]"
                     " [--steps '0,0,0,1.0;1500,0.010,0,1.5']"
                     "   （每档四段：分频Hz,抖动比,4-8k增益dB,补气声倍率；"
                     "分频 0 = 不抖；补气声 0 = 不补）\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
