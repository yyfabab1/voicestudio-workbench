r"""
Seed-VC（44.1kHz 歌声转换 + 角色微调）的**只读**胶水层。

这里只放「路径 / 档位清单 / 状态」这类纯查询，**不放任何跑进程的东西** ——
跑活是 studio.py 的事（它才有 job/暂停/取消那一套）。这样拆是为了避免
循环 import：studio 要 import 本模块拿路径，本模块绝不 import studio。

为什么要有 Seed-VC 这一层（实测结论，不是听说）：
  · RVC 的输出是 40kHz 声码器出的，换引擎后音色分数 0.7099 → **0.7674**（微调后）
  · 但**44.1k 只是容器提升**：16k 以上频段的能量只有 0.012（原唱是 0.25），
    有效带宽约 12k。所以它换来的是音色，不是"更宽的频响"，别在界面上吹过头。
  · 微调（1000 步那次实测）：ft600 是唯一「音色↑ / 原唱残留↓ / 咬字↑」三项全胜
    的档位；1000 步反而过拟合（咬字变差、亮度掉）。⇒ 默认 600 步。
"""
import glob
import json
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES_DB = os.path.join(HERE, "data", "roles.json")

SEEDVC_ROOT = r"D:\AHC\seed-vc-main"
SEEDVC_LIBS = os.path.join(SEEDVC_ROOT, "_svclibs")

# 推理/微调用的基础权重（SVC 专用 44.1k）。预设 yml 里写的是 _ft_ema.pth，
# 两者结构一致，但必须显式指定 v2 —— 用错不会报错，只会训出一个更差的模型。
BASE_CKPT = "checkpoints/DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth"
CONFIG = "configs/presets/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"

# 步数档位。600 是实测拐点（见文件头），1000 会过拟合，所以只给到 1000。
STEPS_CHOICES = (200, 400, 600, 1000)
DEFAULT_STEPS = 600
SAVE_EVERY = 200          # 每 200 步存一档，方便事后挑（阿塔尼斯就是这么比出来的）

# 推理参数（阿塔尼斯那次实测选出的一组）
DIFFUSION_STEPS = 50
CFG_RATE = 0.7

# 数据集/产出的落点：数据集放引擎目录下（train.py 只认相对/绝对路径），
# 训练产物在 runs/<name>/。
DATA_PREFIX = "data_vs_"
MIN_SEGS = 5              # 少于这个真训不出来（硬门槛）
MIN_SECS = 10.0
WARN_SEGS = 30            # 低于这个只能算"够用"，界面要提醒
WARN_SECS = 60.0

# ★★ 这三个必须跟 stages/seedvc_cli.py 里 prep 的 argparse 默认值**逐字一致**。
# 界面上写的「可用素材 N 段」就是按这个口径算的；跟真正喂给训练的口径对不上，
# 用户看到的就是"我有 300 条素材，界面说 0 段"这种鬼故事（真出过：state 只数
# 已生成的数据集目录，没跑过 prepare 就恒为 0）。改任何一边都要改另一边。
PREP_MIN_DUR = 2.0
PREP_MIN_VOICED = 0.60
PREP_MAX_SEGS = 400       # prep 按时长降序取前 N 段（碎段信息量少）


def enabled():
    """Seed-VC 这套东西在本机到底能不能用。缺什么说缺什么，别含糊。"""
    if not os.path.isdir(SEEDVC_ROOT):
        return False, "没找到 Seed-VC 目录：%s" % SEEDVC_ROOT
    if not os.path.isfile(os.path.join(SEEDVC_ROOT, BASE_CKPT)):
        return False, "缺 Seed-VC 基础权重：%s" % BASE_CKPT
    if not os.path.isfile(os.path.join(SEEDVC_ROOT, CONFIG)):
        return False, "缺配置文件：%s" % CONFIG
    return True, ""


def py_path():
    """跑 Seed-VC 用哪个解释器。

    用 **RVC 的 runtime**：Seed-VC 要 torch≥2.7 才能在这台 50 系卡上跑，
    官方钉的 torch 2.2 在这台机器上根本起不来；RVC 的 runtime 是
    py3.12 + torch 2.7.1+cu128，并且已经有 numpy/scipy/soundfile ——
    Seed-VC 额外缺的库都装在 SEEDVC_LIBS 里，靠 PYTHONPATH 挂上去。
    """
    return r"D:\RVC\Retrieval-based-Voice-Conversion-WebUI\runtime\python.exe"


def _clean(rid):
    """角色 id 里的 ascii 部分。

    用角色 id 而不是角色名：中文目录名在 Windows 上没问题，但这条路径要进
    命令行参数，编码问题会以"看起来像模型坏了"的样子出现，排查成本极高。
    """
    return re.sub(r"[^0-9A-Za-z_]", "", str(rid or "")) or "role"


def slug(rid):
    """训练 run 名。"""
    return "vs_" + _clean(rid)


def dataset_name(role):
    # ★ 前缀里已经含 vs_，别再套一层 slug（写成 data_vs_vs_xxx 看着像笔误，
    #   实则会让"数据集目录"和"run 目录"对不上，排错时非常费劲）。
    return ((role.get("seedvc") or {}).get("dataset")
            or (DATA_PREFIX + _clean(role.get("id") or "")))


def dataset_dir(role):
    n = dataset_name(role)
    return n if os.path.isabs(n) else os.path.join(SEEDVC_ROOT, n)


def run_name(role):
    return (role.get("seedvc") or {}).get("run") or slug(role.get("id") or "")


def run_dir(role):
    return os.path.join(SEEDVC_ROOT, "runs", run_name(role))


def config_path(role):
    """训练产物里的 yml 优先：它是这次训练**实际用的**配置（train.py 会拷一份）。"""
    p = os.path.join(run_dir(role), os.path.basename(CONFIG))
    if os.path.isfile(p):
        return os.path.relpath(p, SEEDVC_ROOT)
    return CONFIG


def _step_of(fname):
    """从文件名读出步数：DiT_epoch_00003_step_00600.pth -> 600；ft_model.pth -> 最后一步。"""
    m = re.search(r"step_(\d+)\.pth$", fname)
    if m:
        return int(m.group(1))
    return None


def ckpts(role):
    """列出这个角色已经训出来的档位（按步数升序）。"""
    d = run_dir(role)
    if not os.path.isdir(d):
        return []
    rows = []
    for f in sorted(glob.glob(os.path.join(d, "DiT_epoch_*_step_*.pth"))):
        st = _step_of(os.path.basename(f))
        if st is None:
            continue
        rows.append({"step": st, "file": os.path.basename(f),
                     "path": os.path.relpath(f, SEEDVC_ROOT),
                     "size": os.path.getsize(f),
                     "at": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(f))),
                     "label": "ft%d" % st, "final": False})
    # ft_model.pth 是训练结束时存的那个模型（只存 net，体积小得多）。
    # 它的步数 = 这次训练的 max_steps —— **不能**拿 DiT_*.pth 里最大的那个猜：
    # 1000 步那次每 200 步存一档，最后一个 DiT 档是 800，而 ft_model 是 1000 步
    # 的模型，按 800 标就是错的（实测 ft800 和 ft1000 差着咬字一档）。
    fm = os.path.join(d, "ft_model.pth")
    if os.path.isfile(fm):
        mx = int((role.get("seedvc") or {}).get("steps") or 0) \
            or max([r["step"] for r in rows], default=0)
        rows.append({"step": mx, "file": "ft_model.pth",
                     "path": os.path.relpath(fm, SEEDVC_ROOT),
                     "size": os.path.getsize(fm),
                     "at": time.strftime("%m-%d %H:%M", time.localtime(os.path.getmtime(fm))),
                     "label": "ft%d（末档·精简）" % mx if mx else "ft_model",
                     "final": True})
    rows.sort(key=lambda r: (r["step"], r["final"]))
    return rows


def ckpt_abs(rel):
    return rel if os.path.isabs(rel) else os.path.join(SEEDVC_ROOT, rel)


def selected(role):
    """当前唱歌用的是哪一档；没选过就按「实测最优 600 步」自动挑。"""
    sv = role.get("seedvc") or {}
    rows = ckpts(role)
    if not rows:
        return None
    rel = sv.get("ckpt")
    if rel:
        for r in rows:
            if r["path"] == rel or os.path.basename(r["path"]) == os.path.basename(rel):
                return r
    # 没显式选过：优先 600 步（实测拐点），没有就取最接近 600 的那一档
    if rows:
        return min(rows, key=lambda r: (abs(r["step"] - DEFAULT_STEPS), r["final"]))
    return None


def _segments_of(role):
    """这个角色的切片清单。

    正常情况下 role dict 自己就带着 segments（server 传进来的就是整条角色记录）。
    带空/没带时**回落去读 roles.json** —— 要不界面上就会显示"0 段素材"，
    而用户明明有几百段（这个假象害过一次，见 PREP_* 那段注释）。
    """
    segs = role.get("segments")
    if isinstance(segs, list) and segs:
        return segs
    rid = role.get("id")
    if not rid:
        return []
    try:
        with open(ROLES_DB, encoding="utf-8") as f:
            db = json.load(f)
    except Exception:
        return []
    for r in db.get("roles", []):
        if r.get("id") == rid:
            return r.get("segments") or []
    return []


def avail(role):
    """**现在**能拿来微调的素材有多少（按 prep 的过滤口径算，不落盘）。

    以前这里数的是「已经生成的数据集目录里有多少 wav」——没跑过 prepare 就是 0，
    于是"我有 300 多条素材"在界面上显示成 0 段。正确答案是**从角色素材直接算**，
    数据集目录只是"上次整理出来的产物"。
    """
    segs = [s for s in _segments_of(role)
            if (s.get("dur") or 0) >= PREP_MIN_DUR
            and (s.get("voiced") or 0) >= PREP_MIN_VOICED]
    secs = sum(float(s.get("dur") or 0.0) for s in segs)
    n = len(segs)
    used = n if PREP_MAX_SEGS <= 0 else min(n, PREP_MAX_SEGS)
    return {"segs": n, "secs": round(secs, 1), "used": used,
            "capped": n > used,
            "min_dur": PREP_MIN_DUR, "min_voiced": PREP_MIN_VOICED}


def state(role):
    """给接口用的完整状态：能不能用 / 素材够不够 / 有哪些档位 / 现在用哪个。"""
    ok, why = enabled()
    sv = role.get("seedvc") or {}
    ds = dataset_dir(role)
    n_files = 0
    if os.path.isdir(ds):
        n_files = len([1 for f in os.listdir(ds) if f.lower().endswith(".wav")])
    rows = ckpts(role)
    sel = selected(role)
    av = avail(role)
    segs, secs = av["segs"], av["secs"]
    warn = []
    if ok and segs < MIN_SEGS:
        warn.append("可用素材只有 %d 段（至少要 %d 段才能训），先去角色页多加点素材"
                    % (segs, MIN_SEGS))
    elif ok and segs < WARN_SEGS:
        warn.append("可用素材只有 %d 段（建议 30 段以上），微调效果会打折" % segs)
    if ok and secs and secs < WARN_SECS:
        warn.append("素材合计 %.1f 分钟（建议 1 分钟以上）" % (secs / 60.0))
    return {
        "ok": ok, "why": why,
        "dataset": {"dir": ds, "segs": segs, "secs": round(float(secs), 1),
                    "on_disk": n_files, "built": int(sv.get("segs") or n_files),
                    "avail": av},
        "ckpts": rows, "selected": sel,
        "run": run_name(role),
        "steps_choices": list(STEPS_CHOICES), "default_steps": DEFAULT_STEPS,
        "warn": warn,
        # 每档权重约 2.2GB（DiT + 优化器状态），界面要提前说清，别等磁盘满了才报
        "note": "每存一档约 2.2GB；600 步默认存 3 档。",
    }
