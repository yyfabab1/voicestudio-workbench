r"""
角色类型预判：这段素材是「说话」还是「唱歌」。

先说清楚这个模块的可靠度，免得被当成能自动决策的东西用：

  * 实测基线（data/exp_spkA.json 56 段说话 vs data/exp_sing.json 7 段歌声）：
        voiced（有声帧占比）  说话 p10=0.734 p50=0.804 p90=0.859
                             歌声 p10=0.402 p50=0.515 p90=0.704
    也就是「说话的 p10」还高于「歌声的 p90」，在这批样本上几乎不重叠，
    阈值放在 0.72 附近能分开。

  * 但这批歌声样本有硬伤：7 段全部来自同一段歌（原唱 + RVC 变调 5 份），
    不是 7 段独立素材。所以它证明的是「这个特征在这批数据上分得开」，
    不能证明「换一首歌也分得开」。

  * 原本还试了 F0 平稳度、音高跨度、最长持续音三个特征，
    实测唱歌和说话的数值几乎重合（longest_run 0.150 vs 0.120，f0_std 4.3 vs 4.2），
    没有区分度，所以没采用。

结论：只用它给一个「倾向」和置信度，真正拍板交给界面上的人。
角色类型最终以用户选择为准 —— 这也正是需求里「留下人为修改的空间」。
"""
SPEECH_VOICED = 0.72      # 高于它倾向说话，低于它倾向歌声


def guess(feats):
    """
    输入一批片段特征，返回 (类型, 置信度, 依据文字)。
    类型只有 'speech' / 'singing' 两种；置信度 0~1，越低越该人工确认。
    """
    if not feats:
        return "speech", 0.0, "没有素材，默认按说话处理"
    vs = [f.get("voiced", 0) for f in feats if f.get("dur", 0) >= 1.0]
    if not vs:
        return "speech", 0.0, "没有够长的片段，默认按说话处理"

    import statistics

    med = statistics.median(vs)
    if med >= SPEECH_VOICED:
        # 离阈值越远越笃定，最多算到 0.95，不给满（样本有限，留余地）
        conf = min(0.95, 0.5 + (med - SPEECH_VOICED) * 2.0)
        return "speech", round(conf, 2), "有声帧占比中位 %.2f（说话通常 > %.2f）" % (med, SPEECH_VOICED)
    conf = min(0.95, 0.5 + (SPEECH_VOICED - med) * 2.0)
    return "singing", round(conf, 2), "有声帧占比中位 %.2f（歌声在这批样本上 < %.2f）" % (med, SPEECH_VOICED)


if __name__ == "__main__":
    import json
    import sys

    feats = json.load(open(sys.argv[1], encoding="utf-8"))
    t, c, why = guess(feats)
    print("类型 %s  置信度 %.2f  依据 %s" % (t, c, why))
