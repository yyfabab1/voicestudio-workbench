# -*- coding: utf-8 -*-
r"""
Music Source Separation Training（MSST）推理封装 —— 跟 uvr_cli.py 对称。

为什么要有它（2026-10-26）
------------------------------------------------------------------
项目一直在用 UVR5 里那套 BS-RoFormer（SDR vocals 10.87），是 2024 年的东西。
去 GitHub 把 MSST 的官方模型榜拉下来看了一遍（出处：ZFTurbo/Music-Source-
Separation-Training 的 docs/pretrained_models.md），vocals/other 二分类里
**本机已经躺着一个更强的**：

    KimberleyJensen MelBand Roformer   SDR vocals 10.98   ← 更强，本机已有
    BS Roformer viperx（现主力）        SDR vocals 10.87
    BS PolarFormer                     SDR vocals 11.00   ← 榜首，但要下 300MB

本机那份权重就在兄弟项目 YingMusic-Singer-Plus 的 ckpts/ 里（913MB，不外拷；
按候选路径找，也能用环境变量 VS_MSST_KJ_CKPT 指到别处）。MSST 的推理代码只有
1.2MB，连同它需要的 yaml 一起搬进 stages/ 自包含，避免别人升级仓库把我们带崩。

为什么要再包一层 CLI：两个框架的组织方式不一样。UVR5 那套只在整合包的 gradio
里有，所以当初写了 uvr_cli.py；MSST 原生有 inference.py，但它按 argparse 编排，
且 `utils` 这个包名会跟别人家的 `utils` 抢 —— 在别的目录下直接 import 会炸
ModuleNotFoundError。本脚本就是修这个的：摆好 sys.path，再去调它自带的
proc_folder(dict)（demix / TTA / 归一化一律走它的，不重复实现）。

★ 用哪个解释器：必须是 **VS_MSST_PY**（见 studio.py），也就是那个依赖齐全的
  venv。★ 别用整合包的 runtime\python.exe —— 试过了，它一路缺 loralib /
  ml_collections / …，这些包虽然小（39KB 起），但要凑齐就得把训练用的 demucs / asteroid / mamba_ssm 之类也拖进来（扫描过：缺 25 个第三
  方包，绝大多数只有训练才用得到）。这条路走过，别再试第二遍。

用法（cwd 就是项目根）：
    <VS_MSST_PY> -s stages\msst_cli.py <输入目录> <人声目录> <伴奏目录> [模型key]

例：
    <VS_MSST_PY> -s stages\msst_cli.py in out\vocal out\inst mel_band_roformer_kj

退出码：0 成功；1 有文件没处理成；2 参数或环境错误（权重不在、模型名不认识）。
"""
import json
import os
import shutil
import sys
import traceback

# ★ 注意层数：__file__ = <项目根>/stages/msst_cli.py
#   abspath → 文件名 → stages → <项目根>。两层 dirname 正好到项目根，别再往上一层。
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UP = os.path.dirname(HERE)          # 项目根的上一级
# 自包含的推理代码
MSST_DIR = os.path.join(HERE, "stages", "msst")
CKPT_DIR = os.path.join(HERE, "stages", "msst_ckpts")

# `list` = 自描述模式（门禁用它查模型在不在），不要求后面那些位置参数。
_LISTING = len(sys.argv) >= 2 and sys.argv[1] == "list"

if not _LISTING and len(sys.argv) < 4:
    print("参数不足。需要: 输入目录 人声目录 伴奏目录 [模型key]")
    sys.exit(2)

INP_DIR = VOCAL_DIR = INS_DIR = ""
MODEL_KEY = "mel_band_roformer_kj"
if not _LISTING:      # list 模式没有这几个位置参数，别去摸 argv[1:]
    INP_DIR = os.path.abspath(sys.argv[1])
    VOCAL_DIR = os.path.abspath(sys.argv[2])
    INS_DIR = os.path.abspath(sys.argv[3])
    if len(sys.argv) > 4:
        MODEL_KEY = sys.argv[4]


# ---------------------------------------------------------------------------
# 模型表
#
# ★ SDR 一列是 MSST 官方榜上抄下来的（Multisong 数据集），**不是本项目实测**。
#   本机实测的对比数字写在 studio.py 的 UVR_CANDIDATES 注释里 —— 别混为一谈，
#   也别拿榜单数字当"换上去就一定更好"的证据（详见 2026-10-26.md）。
#
# 权重 913MB，不外拷是为了省磁盘 ⇒ 按下面的顺序找第一个真实存在的：
#   没设环境变量时按下面顺序找第一个真实存在的：
#     ① 兄弟项目 YingMusic-Singer-Plus 里那份（本机实际所在）
#     ② 项目自己的 data/models/msst/（想彻底自包含就拷到这儿）
#
# ★★ 环境变量**显式给了就不再兜底**（2026-10-26 改的）：以前是"把这个路径排进
#    候选队列的第一个"，于是路径写错时会静默跑去用下一份候选 —— 用户以为自己
#    指定了 A，实际跑的是 B。这是"静默地做成了另一件事"，比直接报错糟得多
#    （而且它会让门禁的负对照失效：把权重指坏照样报就绪，看着像尺子是假的）。
#    ⇒ 显式指定就只认它，找不到就是配错了，照实报。
# ---------------------------------------------------------------------------
_KJ_FALLBACKS = [
    os.path.join(UP, "YingMusic-Singer-Plus-main", "ckpts", "MelBandRoformer.ckpt"),
    os.path.join(HERE, "data", "models", "msst", "MelBandRoformer.ckpt"),
]


def _pick_ckpt():
    env = (os.environ.get("VS_MSST_KJ_CKPT") or "").strip()
    if env:
        return env                        # 显式指定 ⇒ 不兜底，好让错误暴露出来
    for p in _KJ_FALLBACKS:
        if p and os.path.isfile(p):
            return p
    return _KJ_FALLBACKS[0] if _KJ_FALLBACKS else ""


def _pick_karaoke_ckpt():
    """becruily 的 karaoke 权重（1.64GB）—— 和 KJ 一样不随项目带。

    为什么用它：这个模型是在**卡拉OK伴奏**上训的（训练目标就是"把主唱从成品里
    拿掉、留下伴奏"），所以它的 instrumental 里**保留伴唱**、vocals 更接近主唱。
    对"原曲里垫唱/和声被角色一起唱掉"这个问题，它比通用的 vocals/other 模型
    更对口：垫唱留在伴奏侧，角色只唱主唱那一条。
    """
    env = (os.environ.get("VS_MSST_KARAOKE_CKPT") or "").strip()
    if env and os.path.isfile(env):
        return env
    for p in (os.path.join(HERE, "data", "models", "msst",
                           "mel_band_roformer_karaoke_becruily.ckpt"),
              os.path.join(HERE, "stages", "msst_ckpts",
                           "mel_band_roformer_karaoke_becruily.ckpt")):
        if os.path.isfile(p):
            return p
    return ""


MSST_MODELS = {
    "mel_band_roformer_kj": {
        # KimberleyJensen 版 MelBand Roformer：官方榜 vocal SDR **10.98**。
        # ★ 这个数字是 **MSST 榜（Multisong 数据集）**上的，不是本项目实测 ——
        #   本机 8 首歌实测它的平均分只有 56.43（现主力 55.95），但**夺冠 3/8**，
        #   在部分歌上明显强 ⇒ 让它进候选池交给实测去挑，但不设为默认主力。
        #   实测表见 studio.py 的 UVR_CANDIDATES 注释。
        "model_type": "mel_band_roformer",
        "config": os.path.join(CKPT_DIR, "config_vocals_mel_band_roformer_kj.yaml"),
        "ckpt": _pick_ckpt(),
        "sdr": 10.98,
    },
    "karaoke_bs_roformer_anvuew": {
        # anvuew karaoke（195MB）：bs_roformer，config 里 instruments=['Vocals','Instrumental']，
        # target_instrument=Vocals。它同样是在卡拉OK素材上训的 ⇒ instrumental 侧保着伴唱。
        "model_type": "bs_roformer",
        "config": os.path.join(HERE, "data", "models", "msst",
                               "karaoke_bs_roformer_anvuew.yaml"),
        "ckpt": os.path.join(HERE, "data", "models", "msst",
                             "karaoke_bs_roformer_anvuew.ckpt"),
        "sdr": None,
    },
    "mel_band_roformer_karaoke_bec": {
        # becruily karaoke：训练目标 = 卡拉OK伴奏（主唱被拿掉、伴唱留下）。
        # ⇒ vocals 更接近主唱、instrumental 里保着伴唱。用于「垫唱不许被角色唱」。
        # num_stems=2 / instruments=[Instrumental]，和 KJ 同一条推理路径。
        "model_type": "mel_band_roformer",
        "config": os.path.join(CKPT_DIR, "config_karaoke_becruily.yaml"),
        "ckpt": _pick_karaoke_ckpt(),
        "sdr": None,
    },
}


def _list_models():
    """打印 MSST_LIST {json}：每个可选模型的配置/权重路径 + 是否就绪。

    ready=False 不表示"配错了"，只表示"这条增强现在用不了" —— studio 那边会
    优雅跳过它，其余 UVR 候选照常工作。
    """
    out = {}
    for k, v in MSST_MODELS.items():
        out[k] = {
            "model_type": v["model_type"],
            "sdr_official": v.get("sdr"),
            "config": v["config"],
            "ckpt": v["ckpt"],
            "ready": os.path.isfile(v["config"]) and os.path.isfile(v["ckpt"]),
        }
    print("MSST_LIST %s" % json.dumps(out, ensure_ascii=False))


def _fail(msg):
    sys.stdout.write("MSST_FAIL %s\n" % msg)
    sys.exit(2)


# ★ 自描述要赶在 main() 之前处理掉：模型表已经定义好了，但还没碰任何音频。
if _LISTING:
    # 门禁用它来查"候选模型是不是真的可用"。
    # ★ 让载体自己报告，而不是让门禁猜权重该在哪个目录 —— 这条路上的模型
    #   根本不在 UVR 的 weights 目录里，靠猜必错（实测第一次就是这么红的）。
    _list_models()
    sys.exit(0)


def main():
    spec = MSST_MODELS.get(MODEL_KEY)
    if not spec:
        _fail("不认识的模型 %s，可选：%s"
              % (MODEL_KEY, "、".join(sorted(MSST_MODELS))))
    for k in ("config", "ckpt"):
        if not os.path.isfile(spec[k]):
            _fail("找不到 %s：%s（权重太大没随项目带，用环境变量指向它）"
                  % ("配置文件" if k == "config" else "权重", spec[k]))

    # ★ 必须插到最前面：整合包的 _pth 把 tools/ 也塞进 sys.path 了，那里有同名
    #   的 `utils`（不是包），不抢占的话 MSST 的 `from utils.xxx import` 会炸。
    sys.path.insert(0, MSST_DIR)
    for mod in [m for m in sys.modules if m == "utils" or m.startswith("utils.")]:
        del sys.modules[mod]

    try:
        import inference as msst
    except Exception as e:                                   # noqa: BLE001
        sys.stderr.write(traceback.format_exc())
        _fail("导入 MSST 推理失败：%s" % e)

    waves = [f for f in sorted(os.listdir(INP_DIR))
             if f.lower().endswith((".wav", ".flac", ".mp3", ".m4a", ".ogg"))]
    if not waves:
        _fail("输入目录里没有音频：%s" % INP_DIR)

    store = os.path.join(os.path.dirname(VOCAL_DIR) or ".", "_msst_store")
    shutil.rmtree(store, ignore_errors=True)

    for d in (VOCAL_DIR, INS_DIR):
        os.makedirs(d, exist_ok=True)

    ok, bad = 0, 0
    try:
        msst.proc_folder(dict(
            model_type=spec["model_type"],
            config_path=spec["config"],
            start_check_point=spec["ckpt"],
            input_folder=INP_DIR,
            store_dir=store,
            # 这个模型是单 stem（只出 vocals），伴奏靠"原曲 − 人声"得到
            extract_instrumental=True,
            force_cpu=False,
            disable_detailed_pbar=True,
            draw_spectro=0,
        ))
    except Exception as e:                                   # noqa: BLE001
        sys.stderr.write(traceback.format_exc())
        _fail("推理没跑完：%s" % e)

    for name in waves:
        stem = os.path.splitext(name)[0]
        got_v = os.path.join(store, stem, "vocals.wav")
        got_i = os.path.join(store, stem, "instrumental.wav")
        if not (os.path.isfile(got_v) and os.path.isfile(got_i)):
            print("  ! %s 没产出齐全（人声=%s 伴奏=%s）"
                  % (name, os.path.isfile(got_v), os.path.isfile(got_i)))
            bad += 1
            continue
        # 命名对齐 UVR 那套（xxx_vocals.wav / xxx_other.wav），
        # 这样分开上层"取目录里第一个 wav"的逻辑两边通用。
        shutil.copyfile(got_v, os.path.join(VOCAL_DIR, stem + "_vocals.wav"))
        shutil.copyfile(got_i, os.path.join(INS_DIR, stem + "_other.wav"))
        print("  -> %s 完成" % name)
        ok += 1

    shutil.rmtree(store, ignore_errors=True)
    print("=" * 60)
    print("MSST 成功 %d 个，失败 %d 个" % (ok, bad))
    print("人声目录: %s" % VOCAL_DIR)
    print("伴奏目录: %s" % INS_DIR)
    print("=" * 60)
    sys.exit(0 if (ok and not bad) else 1)


if __name__ == "__main__":
    main()
