# -*- coding: utf-8 -*-
r"""
「每一句的语气」——语气预设表 + 按语气挑参考切片。

这个文件被**服务进程直接 import**，所以只能用标准库 + numpy，
不许要 librosa / soundfile（那些在建索引时已经用过了，见 stages/tone_index.py）。

================================================================================
为什么「语气」= 「换参考音频」
================================================================================

GPT-SoVITS 合成一句话时，参考音频（ref_wav）决定的是**音色 + 韵律/语气**，
文本只决定"说什么"。所以语气这件事在它这里是**参考音频说了算**的。

实测验证（2026-10-16，角色=阿塔尼斯，同一句文本「我们必须立刻行动…」）：

    参考切片        参考 f0   →  合成 f0    参考起伏  →  合成起伏   成片时长
    低沉平缓         92 Hz        84 Hz        56         20        4.5 s
    中间常态        153 Hz       107 Hz        86         72       12.5 s
    高亢起伏        172 Hz       141 Hz       264         83        4.4 s
    当前默认        201 Hz       155 Hz       208         98        3.6 s

=== 音高跨度 70.7 Hz、起伏跨度 78.1 === 中间什么都不改，只换参考。
这就是本模块存在的证据。

================================================================================
怎么挑
================================================================================

两条**硬门槛**（不满足直接淘汰），两条是实测踩出来的：

  1. 时长必须在 3~10 秒。太长会带进一堆无关内容。
  2. ★ **语速不能低于 2.6 字/秒**。实测挑到一段平淡的报数式台词
     （「晶体矿不足，需要更多的晶体矿。」13 字拖了 9.4 秒 = 1.38 字/秒）
     当参考，同一句被合成成 **12.5 秒** —— 其他参考都是 3.6~4.5 秒，
     模型把参考里的拖沓一起学走了。

     ★★ 但这个门槛**第一版写成了「语速不低于候选池中位数」**，那太狠了：
     砍掉一半样本，结果「悲伤」「庄重」这些**想要慢**的语气反而挑不到慢素材，
     相关性直接废掉。实测分布（阿塔尼斯 211 条）：真正有害的是 <2.5 那一段，
     而正常区间是 3.2~4.5。所以改成绝对下限，既挡坑又不废功能。

剩下的按「加权距离」排序：每种语气声明自己在几个特征上要往哪个方向偏
（单位是候选池内的 z-score，-1 到 1），挑最贴近的那个。

要不要加随机？**不要**。同一次输入必须给同一个结果，否则用户点第二次
听到的东西不一样，会以为是坏了。
"""

import math
import os

SR_MIN = 2.6          # 语速下限（字/秒），低于此的参考会把句子合成成一字一顿


# ==============================================================================
# 长台词怎么切 —— 「太长台词会乱掉」的根因与对策
# ==============================================================================
#
# 老板的反馈：「说话太长台词会乱掉」。实测（_probe_long.py，角色=阿塔尼斯，
# 五档台词 11/26/51/91/147 字，逐条 ASR 后跟原文比字错误率 CER）：
#
#     字数      整段丢给模型              按 <=28 字切开再拼
#     11         CER  0.0%               CER 0.0%
#     26         CER  0.0%               CER 0.0%
#     51         CER  0.0%               CER 0.0%
#     91         CER 14.3%               CER 6.6%
#    147         CER 41.5%  ← 整块吞句    CER 8.2%
#
# 147 字那条整段合成时，秒/字从正常的 0.24 掉到 0.15 —— 模型不是在"念得慢"，
# 是在**跳字**：实测丢掉「可是你从来不肯站出来」「我等了那么久」「我连你的影子
# 都没再见过」三整块。切分之后这些块都回来了。
#
# ★ 切分之后剩下的丢字，**100% 落在合成单元的段首**（18 个片段逐条 ASR：
#   段首 18 字、段中 0 字、段尾 1 字）。而且它是**随机的**：
#   同一段、同一参考合成两次，一次完整一次丢头 6 字；换一个参考又能救回来
#   （5 个参考里 2 个成功）。文本层的守卫全试过、全部无效（段首补句号/逗号/
#   省略号/空格/换行、temperature 0.75/0.90、段尾补逗号）—— 所以在**文本**上
#   做文章没用，只能：① 别让"段首"落在一个短句上（下面是 SEG_MIN 的由来）；
#   ② 每段多跑一次取更完整的那次（见 stages/tts_batch.py 的 takes）。
#
# 还有一条更隐蔽的：**单独 6 个字的短句会合成出一段 0.24 秒的纯静音**
# （实测 rms 0.0001、静音占比 100%）。产品里这种会被出声体检拦下报失败，
# 不会静默交付 —— 但切分时把过短的字句并进相邻段能少踩几次。
SEG_BUDGET = 28       # 一个合成单元最多多少字（28 是实测 CER 最低那档）
SEG_MIN = 10          # 短于这个数的子句，不许默默黏在段首/段尾（要拆出来单独合成）
SEG_MIN_SAFE = 4      # 拆出来的短段本身不得短于这个数（再短就让它黏着，别制造碎片）
GAP_END = 0.26        # 句末停顿（秒）
GAP_MID = 0.14        # 句中停顿
GAP_CUT = 0.12        # 硬切处（没有标点可用）—— 别让两段连读成一个词

_END_PUNCT = "。！？!?；;…"
_MID_PUNCT = "，,、：:—～~"
_ALL_PUNCT = _END_PUNCT + _MID_PUNCT


# ---------------------------------------------------------------- 语气预设
#
# target：希望在 z-score 尺度上（-1 ~ +1）往哪偏。0 = 不关心。
#   可用的键：f0_mean（音高）f0_std（音高抖动）f0_range（起伏跨度）
#            rms_mean（音量）rms_std（轻重变化）voiced（连贯度）syl_rate（语速）
# weight：这一项在打分里占多大比重。不写就是 1.0。
# speed：合成语速倍数。> 1 更急，< 1 更慢更沉。
# temperature：GPT-SoVITS 采样温度。它对"有没有感情"的影响仅次于参考音频 ——
#              低了平得像念稿，高了起伏是有了但容易跑音；0.9 以上明显不稳。

TONES = [
    {
        "key": "auto", "name": "跟随默认", "tag": "不改",
        "pick": False, "speed": 1.0, "temperature": 0.6,
        "desc": "沿用这个角色当前的参考音频 —— 也就是你现在听到的那种语气。 "
                "其他选项都是在这一句临时换一段参考来改语气。",
    },
    {
        "key": "calm", "name": "平静", "tag": "叙事",
        "target": {"f0_std": -0.5, "f0_range": -0.4, "rms_std": -0.3},
        "weight": {"f0_std": 1.4, "f0_range": 1.0},
        "speed": 1.0, "temperature": 0.55,
        "desc": "起伏小、读得稳。适合旁白、说明、播报。",
    },
    {
        "key": "tender", "name": "温柔", "tag": "亲近",
        "target": {"f0_mean": 0.35, "f0_std": -0.35, "rms_mean": -0.25, "voiced": -0.2},
        "weight": {"f0_std": 1.2, "rms_mean": 1.0},
        "speed": 0.94, "temperature": 0.55,
        "desc": "略高的音调、放轻的音量、稍慢的节奏。适合安抚、告白、对晚辈说话。",
    },
    {
        "key": "solemn", "name": "庄重", "tag": "有分量",
        "target": {"f0_mean": -0.3, "syl_rate": -0.35, "voiced": 0.4, "f0_std": -0.2},
        "weight": {"f0_mean": 1.3, "syl_rate": 1.1},
        "speed": 0.92, "temperature": 0.5,
        "desc": "压低、慢、连续、少起伏。适合宣言、悼词、上位者开口。",
    },
    {
        "key": "low", "name": "低沉", "tag": "压抑",
        "target": {"f0_mean": -0.85, "f0_range": -0.6, "rms_std": -0.3},
        "weight": {"f0_mean": 2.0, "f0_range": 1.0},
        "speed": 0.9, "temperature": 0.5,
        "desc": "把音调压到这个角色能到的最低处。适合疲惫、威胁、内心独白。",
    },
    {
        "key": "firm", "name": "沉稳有力", "tag": "坚定",
        "target": {"rms_mean": 0.5, "f0_std": 0.15, "voiced": 0.45, "f0_mean": -0.1},
        "weight": {"rms_mean": 1.6, "voiced": 1.0},
        "speed": 1.03, "temperature": 0.6,
        "desc": "音量足、咬字连贯、不飘。适合下令、承诺、表态。",
    },
    {
        "key": "excited", "name": "激昂", "tag": "有能量",
        "target": {"f0_mean": 0.6, "f0_range": 0.7, "f0_std": 0.6, "rms_mean": 0.35},
        "weight": {"f0_range": 1.6, "f0_std": 1.2, "f0_mean": 1.0},
        "speed": 1.1, "temperature": 0.75,
        "desc": "音高抬起来、起伏拉大、说得更快。适合战前动员、欢呼、高潮段落。",
    },
    {
        "key": "angry", "name": "愤怒", "tag": "强烈",
        "target": {"f0_range": 0.85, "f0_std": 0.8, "rms_mean": 0.6, "syl_rate": 0.4},
        "weight": {"f0_range": 1.8, "rms_mean": 1.4, "f0_std": 1.0},
        "speed": 1.14, "temperature": 0.85,
        "desc": "起伏和音量都拉到顶、语速偏快。适合斥责、争执、爆发。",
    },
    {
        "key": "urgent", "name": "急促紧张", "tag": "赶时间",
        "target": {"syl_rate": 0.75, "voiced": 0.5, "f0_mean": 0.3},
        "weight": {"syl_rate": 2.0, "voiced": 1.2},
        "speed": 1.16, "temperature": 0.7,
        "desc": "连着说、不停顿、音调偏高。适合告警、逃亡、倒计时。",
    },
    {
        "key": "cheer", "name": "轻快", "tag": "明快",
        "target": {"f0_mean": 0.45, "syl_rate": 0.45, "f0_std": 0.25, "rms_std": 0.3},
        "weight": {"syl_rate": 1.5, "f0_mean": 1.2},
        "speed": 1.08, "temperature": 0.7,
        "desc": "音调稍高、节奏轻、有弹性。适合报喜、玩笑、日常对话。",
    },
    {
        "key": "sad", "name": "悲伤", "tag": "低落",
        "target": {"f0_mean": -0.45, "syl_rate": -0.5, "rms_mean": -0.35, "voiced": -0.35},
        "weight": {"syl_rate": 1.5, "f0_mean": 1.3, "rms_mean": 1.0},
        "speed": 0.9, "temperature": 0.55,
        "desc": "音调低、说得慢、常常停下来。适合告别、失落、说坏消息。",
    },
]

TONES_BY_KEY = dict((t["key"], t) for t in TONES)

FEATURES = ("f0_mean", "f0_std", "f0_range", "rms_mean", "rms_std",
            "voiced", "syl_rate")

# ---------------------------------------------------------------- 别名
#
# 老板的写法是「在提示词里写语气」，比如：
#     [愤怒]你怎么敢这么做！
#     别怕，我在这里。（温柔）
# 所以他不会记得住英文 key。**别名表就是把中文口语映射到 key 的那层**。
#
# 为什么每种语气要配这么多叫法：写词的人是想到什么写什么
# （有人写"愤怒"、有人写"暴怒"、有人写"生气"），认不出来是最伤的失败 ——
# 用户以为自己写了语气，其实被当成普通文本念了出来。

ALIASES = {
    "auto": ("默认", "常规", "正常", "原样", "默认语气", "跟随默认", "auto"),
    "calm": ("平静", "冷静", "平稳", "淡定", "平和", "叙事", "旁白", "播音",
             "淡然", "安详", "念稿"),
    "tender": ("温柔", "轻柔", "亲切", "温和", "宠溺", "和蔼", "轻声", "哄",
               "关爱", "缱绻"),
    "solemn": ("庄重", "庄严", "严肃", "郑重", "肃穆", "沉稳", "威严", "典礼"),
    "low": ("低沉", "低声", "压抑", "沉闷", "沙哑", "压抑感", "阴沉", "疲惫地",
            "有气无力"),
    "firm": ("坚定", "有力", "沉稳有力", "果断", "肯定", "笃定", "掷地有声",
             "命令", "下令", "强硬"),
    "excited": ("激昂", "激动", "兴奋", "慷慨", "高昂", "热血", "振奋", "鼓舞",
                "呐喊", "激情"),
    "angry": ("愤怒", "生气", "暴怒", "气愤", "恼怒", "咬牙切齿", "怒吼", "震怒"),
    "urgent": ("急促", "紧张", "急迫", "焦急", "慌张", "抢", "十万火急", "喘息",
               "上气不接下气"),
    "cheer": ("轻快", "明快", "欢快", "开心", "愉快", "活泼", "雀跃", "笑着",
              "打趣", "调皮"),
    "sad": ("悲伤", "难过", "低落", "失落", "沮丧", "哽咽", "沉痛", "哀伤",
            "带着哭腔", "泣"),
}

# 反查：任意别名 -> key。同音表述要归一 —— 全小写 + 去掉标点区别不大，
# 但中文没有大小写，所以直接建表最快也最可控。
_ALIAS2KEY = {}
for _k, _names in ALIASES.items():
    for _n in _names:
        _ALIAS2KEY[_n] = _k
    _ALIAS2KEY[_k] = _k

# 标签的括号形态：中英文方括号、圆括号、六角括号都认。
# 写作者手边有什么就打什么，不该为格式报错。
_TAG_OPEN = "[（(【「『"
_TAG_CLOSE = "]）)】」』"


def _strip_one(s):
    """从字符串开头剥掉一层语气标签。

    返回 **(剩余字符串, 标签原文)** —— **没有标签时标签原文是 None**，
    有标签但整行只剩标签时剩余是空字符串 ""。这两个必须区分得开：

    ★ 踩过：早期版本用 `if not rest: break` 判"没标签"，结果「[温柔]」这种
      只写了标签没写词的半截提示词，被原样当成台词念了出来 —— 用户以为自己在
      标语气，实际听到的是一串方括号。现在改成判 `inner is None`。

    只认**行首**的标签 —— 句子中间的括号是普通标点，剥掉会把台词咬坏。
    """
    i, n = 0, len(s)
    while i < n and s[i] in " \t　":
        i += 1
    if i >= n or s[i] not in _TAG_OPEN:
        return None, None
    opener = s[i]
    closer = _TAG_CLOSE[_TAG_OPEN.index(opener)]
    j = s.find(closer, i + 1)
    if j < 0:
        return None, None
    inner = s[i + 1:j].strip()
    # 标签内容里不许再有括号（那是没写好的输入，宁可当正文也别瞎猜）
    if not inner or any(c in _TAG_OPEN + _TAG_CLOSE for c in inner):
        return None, None
    if len(inner) > 12:
        return None, None          # 12 字以上多半是一整句话，不是语气标记
    return s[j + 1:].lstrip(), inner


def split_tone(line):
    """把一行文本拆成「要说的词」+「语气」。

    返回 (text, tone_key, tags, unknown)
      text     剥掉所有语气标签后、真正要合成的话（可能为空）
      tone_key 最终生效的语气；没写标签就是 None（调用方该用默认）
      tags     写出来的标签原文列表（给用户回看，让他知道认出来了没有）
      unknown  写了但没匹配到任何预设的标签 —— **不静默吞掉**，由调用方报给用户

    写多个标签（「[低沉][急促]快走」）会把两张票合起来：目标是加权平均，
    语速/温度也取平均。这样组合出「又快又沉」是可能的。
    """
    s = (line or "").strip()
    tags, unknown = [], []
    keys = []
    for _ in range(4):                       # 最多连续剥 4 层，防死循环
        rest, inner = _strip_one(s)
        if inner is None:                    # ★ 判「没有标签」，不是判剩余为空
            break
        k = _ALIAS2KEY.get(inner)
        tags.append(inner)
        if k:
            keys.append(k)
        else:
            unknown.append(inner)
        s = rest
    if not tags:
        return s, None, [], []
    key = None
    if keys:
        key = _merge(keys)
    return s, key, tags, unknown


def _merge(keys):
    """多个语气标签合成一个：注册成 "a_b" 的临时语气，并写进 TONES_BY_KEY。

    为什么不干脆返回第一个：写「[低沉][急促]」的人是想要两者的和，
    只取一个等于另一个白写了。
    """
    keys = [k for k in keys if k in TONES_BY_KEY]
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    sk = "_" .join(sorted(dict.fromkeys(keys)))
    if sk not in TONES_BY_KEY:
        base = [TONES_BY_KEY[k] for k in keys]
        allkeys = []
        for b in base:
            allkeys += list((b.get("target") or {}).keys())
        target, weight = {}, {}
        for k in dict.fromkeys(allkeys):
            vals = [b["target"][k] for b in base if k in (b.get("target") or {})]
            target[k] = sum(vals) / len(vals)
            w = [b.get("weight", {}).get(k, 1.0) for b in base
                 if k in (b.get("target") or {})]
            weight[k] = max(w)
        TONES_BY_KEY[sk] = {
            "key": sk, "name": "+".join(b["name"] for b in base),
            "target": target, "weight": weight,
            "speed": sum(b.get("speed", 1.0) for b in base) / len(base),
            "temperature": sum(b.get("temperature", 0.6) for b in base) / len(base),
            "desc": "组合语气：" + "、".join(b["name"] for b in base),
        }
    return sk


def parse_script(raw):
    """把一整段脚本拆成 [{text, tone, tags, unknown}]。

    按行拆，也认 `|` 分隔（老界面的写法）。空行丢掉。
    """
    out = []
    for ln in (raw or "").replace("|", "\n").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        text, key, tags, unk = split_tone(ln)
        if not text:
            continue
        out.append({"text": text, "tone": key, "tags": tags, "unknown": unk})
    return out


def _zstats(items):
    """算候选池里每个特征的均值/标准差，用来把绝对值换成 z-score。

    为什么必须 z-score：不同特征的量纲差太远了 —— f0 是几百，rms 是零点几。
    不归一化的话，「要看重哪一项」根本没法通过权重表达。
    """
    st = {}
    for k in FEATURES:
        v = [float(it.get(k, 0.0)) for it in items if it.get(k) is not None]
        if not v:
            st[k] = (0.0, 1.0)
            continue
        n = len(v)
        mean = sum(v) / n
        var = sum((x - mean) ** 2 for x in v) / n
        sd = math.sqrt(var)
        st[k] = (mean, sd if sd > 1e-9 else 1.0)
    return st


def _eligible(items):
    """硬门槛过滤：能拿来当参考的候选。

    两条硬门槛：
      · 时长 3~10 秒（参考音频太长会带进无关内容，太短信息不够）
      · 语速 >= SR_MIN（挡拖沓参考；为什么不挡更多见上面的 ★★ 说明）
    """
    good = [it for it in items
            if 3.0 <= float(it.get("dur", 0)) <= 10.0
            and (it.get("text") or "").strip()
            and float(it.get("rms_mean", 0)) > 0.01
            and float(it.get("syl_rate", 0) or 0) >= SR_MIN]
    if len(good) < 3:
        # 素材太少的角色（只有几段）别因为门槛把功能整个废掉，
        # 放宽到"只要有文本、有声音"就行 —— 挑得糙一点好过不能用。
        good = [it for it in items
                if 2.0 <= float(it.get("dur", 0)) <= 14.0
                and (it.get("text") or "").strip()
                and float(it.get("rms_mean", 0)) > 0.005]
    return good


def pick(items, tone_key, seed_text=None):
    """按语气挑一段参考切片。返回 item（dict）或 None。

    `items` 是 stages/tone_index.py 建出来的索引里的 items。
    `tone_key` 不在预设里、或者角色压根没索引，都返回 None —— 调用方
    看到 None 就该退回角色默认参考，而不是报错。
    """
    t = TONES_BY_KEY.get(tone_key or "auto")
    if not t or t.get("pick") is False:
        return None
    cands = _eligible(items or [])
    if not cands:
        return None
    target = t.get("target") or {}
    weight = t.get("weight") or {}
    if not target:
        return None
    st = _zstats(cands)

    def score(it):
        s = 0.0
        for k, want in target.items():
            if k not in st:
                continue
            mean, sd = st[k]
            z = (float(it.get(k, 0.0)) - mean) / sd
            z = max(-3.0, min(3.0, z))       # 掐掉野值，免得一个怪样本通吃
            s += float(weight.get(k, 1.0)) * abs(z - want)
        return s

    cands.sort(key=score)
    return cands[0]


def options():
    """给前端的语气清单（把内部字段剥掉）。"""
    out = []
    for t in TONES:
        out.append({"key": t["key"], "name": t["name"], "tag": t.get("tag", ""),
                    "desc": t.get("desc", "")})
    return out


# ---------------------------------------------------------------- 长台词切分
def _clauses(text):
    """按所有标点把文本折成子句，标点留在前一个子句尾巴上。

    为什么标点要留着：段的末尾是什么标点，决定它后面该补多长的停顿
    （句号 0.26 秒 / 逗号 0.14 秒）。把标点丢了就只能全给同一个值，听起来
    像顿号念到底。
    """
    out, buf = [], ""
    for ch in (text or ""):
        if ch == "\n":
            if buf.strip():
                out.append(buf.strip())
            buf = ""
            continue
        buf += ch
        if ch in _ALL_PUNCT:
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def _hard_split(piece, budget):
    """一个子句本身就超预算（用户写了一长串没有标点）—— 只能等分硬切。"""
    n = len(piece)
    k = int(math.ceil(n / float(budget))) if budget > 0 else 1
    step = int(math.ceil(n / float(k)))
    return [piece[i:i + step] for i in range(0, n, step)]


def gap_for(piece):
    """这一段后面该补多长的气口。"""
    if not piece:
        return GAP_CUT
    last = piece[-1]
    if last in _END_PUNCT:
        return GAP_END
    if last in _MID_PUNCT:
        return GAP_MID
    return GAP_CUT


def split_segments(text, budget=SEG_BUDGET, min_len=SEG_MIN):
    """把一句台词切成若干「合成单元」。返回 [{"text":..., "gap":...}, ...]。

    规则（每一条都对着实测数据）：
      · 累积到不超 budget 就继续攒 —— 段越多，段首丢字的机会越多；
      · 封段时若**下一段的开头是个短子句**，宁可本段超一点也把它一起收进来
        （短子句单独当段首时，模型容易起不来，实测 6 字的段直接出静音）；
      · 段尾标点决定气口长度。

    budget 调成很大（比如 100000）就等于「不切」，门禁拿它当负对照。
    """
    text = (text or "").strip()
    if not text:
        return []
    cl = []
    for c in _clauses(text):
        if budget > 0 and len(c) > budget:
            cl.extend(_hard_split(c, budget))
        else:
            cl.append(c)
    # 纯标点（比如用户写了个孤零零的「。」）不是台词，别送进合成
    cl = [c for c in cl if c.strip(_ALL_PUNCT + " \t")]
    if not cl:
        return []

    segs, cur = [], ""
    for c in cl:
        if cur and len(cur) + len(c) > budget:
            segs.append(cur)
            cur = c
        else:
            cur += c
    if cur:
        segs.append(cur)

    # ★ 段首/段尾的短子句 —— **拆出来独立成段**，不要让它黏在长句旁边。
    #   实测（_probe_long5.py，同一段文本、同一套权重，只换形状）：
    #     「哪怕只是一句话也好。我等了那么久，」黏在一起 → 那 6 个字被吞，
    #        重试两次都吞（秒/字 0.151 vs 正常 0.244）
    #     「我等了那么久，」**带标点独立成段** → 完整念出 ✓
    #   顺带把第一版"两头搬迁"的死结也解了：那种写法下短句被推到下一段开头、
    #   又被推回上一段末尾，来回搬运最后回到原样。
    #
    #   两条保险，缺一不可：
    #     · 只拆**足够长**的段（>= budget*0.6）。不设这条，"你好。世界。再见。"
    #       这种本来就全是短句的台词会被拆成一堆 3 字段，比不拆更糟。
    #     · 拆出来的短段本身不能太短（>= SEG_MIN_SAFE）—— 实测**不带标点**的
    #       6 字（「我等了那么久」）会合成出 0.24 秒纯静音；带标点的没问题。
    out = []
    for s in segs:
        c0 = _clauses(s)
        if len(s) >= budget * 0.6 and len(c0) >= 2:
            keep = list(c0)
            if len(keep[-1]) < min_len and len(keep[-1]) >= SEG_MIN_SAFE:
                tail = keep.pop()
                out.append("".join(keep))
                out.append(tail)
                keep = []
            elif len(keep[0]) < min_len and len(keep[0]) >= SEG_MIN_SAFE:
                head = keep.pop(0)
                out.append(head)
                out.append("".join(keep))
                keep = []
            if keep:
                out.append("".join(keep))
        else:
            out.append(s)
    segs = [s for s in out if s.strip(_ALL_PUNCT + " \t")]

    # 末段太短就并回上一段（"孤儿尾"同样容易合成失败）
    if len(segs) > 1 and len(segs[-1]) < min_len:
        segs[-2] = segs[-2] + segs[-1]
        segs.pop()
    return [{"text": s, "gap": round(gap_for(s), 3)} for s in segs]


def _rank(items, tone_key):
    """按语气打分排序的候选（pick 与 pick_alts 共用一份打分，不许两处各写一套）。"""
    t = TONES_BY_KEY.get(tone_key or "auto")
    if not t or t.get("pick") is False:
        return []
    cands = _eligible(items or [])
    if not cands:
        return []
    target = t.get("target") or {}
    weight = t.get("weight") or {}
    if not target:
        return []
    st = _zstats(cands)

    def score(it):
        s = 0.0
        for k, want in target.items():
            if k not in st:
                continue
            mean, sd = st[k]
            z = (float(it.get(k, 0.0)) - mean) / sd
            z = max(-3.0, min(3.0, z))       # 掐掉野值，免得一个怪样本通吃
            s += float(weight.get(k, 1.0)) * abs(z - want)
        return s

    return sorted(cands, key=score)


def pick(items, tone_key, seed_text=None):
    """按语气挑一段参考切片。返回 item（dict）或 None。

    `items` 是 stages/tone_index.py 建出来的索引里的 items。
    `tone_key` 不在预设里、或者角色压根没索引，都返回 None —— 调用方
    看到 None 就该退回角色默认参考，而不是报错。
    """
    r = _rank(items, tone_key)
    return r[0] if r else None


def pick_alts(items, tone_key, exclude=(), n=2):
    """再挑几段备选参考（**不和已选的那段重复**），给"这次没合成好、重来一次"用。

    为什么不是随便抓一段：段首丢字是随机的，但**换一段参考能改变它**（实测
    5 个参考里 2 个成功、3 个丢字）。重试要是还用同一段参考，等于掷同一个骰子
    —— 所以这里必须排除已经用过的那几个文件。

    `items` 没有索引（角色没建过）时返回 []，调用方就退化成"用同一个参考重跑",
    仍然比不重试强（时长择优那一层还在）。
    """
    used = set(os.path.basename(x) for x in (exclude or []) if x)
    r = _rank(items, tone_key)
    out = []
    for it in r:
        if os.path.basename(it.get("file") or "") in used:
            continue
        out.append(it)
        if len(out) >= n:
            break
    return out


def median_spc(items):
    """角色的「一秒几个字」基准（秒/字），用来判断某一段是不是被吞了字。

    拿索引里的 syl_rate 取中位（只统计有文本的条目）。取中位不取均值：
    样本里混着"喂，嗯"这种短促条目，均值会被拽偏。
    """
    v = []
    for it in (items or []):
        try:
            r = float(it.get("syl_rate") or 0.0)
        except (TypeError, ValueError):
            r = 0.0
        if r >= SR_MIN and (it.get("text") or "").strip():
            v.append(r)
    if not v:
        return 0.0
    v.sort()
    return round(1.0 / v[len(v) // 2], 4)
