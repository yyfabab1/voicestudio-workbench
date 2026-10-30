# -*- coding: utf-8 -*-
r"""门禁：RVC 歌声模型训练**真跑通**（不是只在接口层验过）。

★ 为什么要单开这一关，而且必须真跑：
   歌声模型这条链是**跨到 RVC 的目录和 python** 去跑的（train.preprocess →
   extract_f0 → extract_hubert_feature → train.train → train_index），中间任何一步
   脚本改名 / 参数改签名 / 底模对不上，都不会报"功能坏了"，只会表现为
   「唱出来没法听」或者「训练卡在某一步」。RVC 那边的代码不在本项目里，
   本项目改不动它、也监听不到它变 —— 只有真跑一遍才知道链条还通不通。

   上一轮的 21 关里，RVC 训练**只在接口层验过**（verify_apierr 把重的活都打桩了），
   所以"点训练按钮之后到底跑不跑得完"这件事一直没有人回答过。本关就是来回答它的。

★ 三条硬规矩：
   ① **自足**：自己种角色、自己硬链接素材，不依赖用户库里恰好有某个角色；
   ② **不碰用户的东西**：只训这个临时角色，用户已有角色的权重/断点/索引一个字节不动；
   ③ **每一条都配负对照**：尤其是"文件在 ≠ 能用"这条（junk .pth 必须被判不可用）。

用法（venv）：
    <venv>/Scripts/python.exe -s verify_rvc_train.py

⚠ 本关会真占 GPU 两分钟左右，并在 RVC 的 logs/ 下临时产生约 1 GB 产物（跑完自己清）。
   清理如果被本机的批量删除钩子挡下，接着跑一次 `bash _run_regression.sh gc`，
   并按本关末尾打印的残留清单手工清 RVC 侧的目录。
"""
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import studio  # noqa: E402

RVC_PY = studio.RVC_PY
CKPT = os.path.join(HERE, "stages", "rvc_ckpt_check.py")
R = studio.RVC_ROOT
EPOCHS = 15                       # -se 是 15 ⇒ 刚好拿到一份存档，时间也可控
N_SLICE = 24
SRC_RID = os.environ.get("VS_RVC_SRC_ROLE", "ra500fb92")   # 大帝：干声够多

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %-6s %s%s" % ("PASS" if cond else "FAIL", name,
                           ("  | %s" % detail) if detail else ""))


def _plant(name, n_slice):
    """种一个临时角色，素材从 SRC_RID **硬链接**过来（不占额外磁盘）。"""
    role = studio.create_role(name=name, kind="singing",
                              source="verify:rvc-true-train")
    rid = role["id"]
    d = studio.role_dir(rid)
    os.makedirs(os.path.join(d, "slices"), exist_ok=True)
    db = studio.load_roles()
    src = next((x for x in db["roles"] if x["id"] == SRC_RID), None)
    if not src:
        return rid, 0, 0.0, "角色库里找不到素材源 %s" % SRC_RID
    segs = [s for s in (src.get("segments") or [])
            if float(s.get("dur", 0) or 0) >= 3.0][:n_slice]
    src_dir = os.path.join(studio.role_dir(SRC_RID), "slices")
    dst_dir = os.path.join(d, "slices")
    linked = 0
    for s in segs:
        p = os.path.join(src_dir, s["file"])
        if not os.path.exists(p):
            continue
        dst = os.path.join(dst_dir, s["file"])
        try:
            os.link(p, dst)          # 硬链接：同一卷上不额外占空间
        except OSError:
            shutil.copy2(p, dst)
        linked += 1
    # ★ update_role() 只放行 name/intro/kind/color/ref/kind_locked，segments 会被
    #   静默丢掉（踩过一次，种出来 0 段）⇒ 必须绕开它直接写 roles.json。
    for x in db["roles"]:
        if x["id"] == rid:
            x["segments"] = segs
    studio.save_roles(db)
    return rid, linked, sum(float(s.get("dur", 0) or 0) for s in segs), ""


def _purge(rid):
    """尽力清，删多少算多少。

    ★ 本机那个批量删除钩子抛的是 `SystemExit`（`except Exception` **捕不到**，
      实测掀翻过整个门禁进程 ⇒ 那一轮连汇总行都打不出来）。所以这里逐项
      `suppress(BaseException)`，剩下的照实报出来，交给 `gc` 收尾 —— 这正是
      `_run_regression.sh` 顶部写明的规矩：门禁自己不可能清干净。
    """
    with contextlib.suppress(BaseException):
        studio.delete_role(rid)          # 现在它自己就会清 RVC 侧（见 purge_rvc_artifacts）
    with contextlib.suppress(BaseException):
        studio.purge_rvc_artifacts(rid)  # 再兜一次：钩子可能只拦下了一部分
    with contextlib.suppress(BaseException):
        shutil.rmtree(os.path.join(HERE, "data", "roles", rid))


def _residue(rid):
    out = []
    if os.path.isdir(os.path.join(R, "logs", rid)):
        out.append("RVC logs/%s" % rid)
    if os.path.isdir(os.path.join(HERE, "data", "roles", rid)):
        out.append("data/roles/%s" % rid)
    wdir = os.path.join(R, "assets", "weights")
    out += [f for f in (os.listdir(wdir) if os.path.isdir(wdir) else [])
            if f.startswith(rid) and f.endswith(".pth")]
    idir = os.path.join(R, "assets", "indices")
    out += [f for f in (os.listdir(idir) if os.path.isdir(idir) else []) if rid in f]
    return out


print("=== A. 素材门槛：不够就得挡住，不能硬训出个沙哑模型 ===")
rid_few, n_few, _, err0 = _plant("RVC素材不足验收（临时）", 5)
if err0:
    check("素材源可用（角色库里找得到能借干声的角色）", False, err0)
    print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
    sys.exit(1)
_err = ""
try:
    studio.train_rvc(studio.new_job(rid_few, "train_rvc", "素材不足验收"),
                     rid_few, epochs=EPOCHS)
except Exception as e:
    _err = "%s" % e
check("★ 负对照：只有 %d 段素材时明确拒绝训练（而不是硬训一个废模型）" % n_few,
      bool(_err) and "10 段" in _err, (_err or "没报错 ⇒ 门槛形同虚设")[:160])
_purge(rid_few)

print("\n=== A2. 重试只认那一种错（不能把真错误也吞掉）===")
# RVC 的 DataLoader 会偶发崩在 .spec.pt 的写竞争上（见 studio._rvc_spec_race），
# 那条路径会**自动重跑一次**。重试认错认宽了，就会把显存不够、底模不对这类
# 真错误也重跑一遍 —— 用户干等十几分钟，最后看到同一句报错。
check("★ spec 缓存写竞争被认出来（该重试）",
      studio._rvc_spec_race(
          "PermissionError: [Errno 13] Permission denied: "
          "D:\\\\RVC\\\\logs\\\\r12345678\\\\0_gt_wavs/14_2.spec.pt"))
check("★ 负对照：显存不够**不许**被当成写竞争（那不是重跑能解决的）",
      not studio._rvc_spec_race("RuntimeError: CUDA out of memory. Tried to allocate 2.00 GiB"))
check("★ 负对照：别的 PermissionError 也不许被吞（只认 .spec.pt 那一种）",
      not studio._rvc_spec_race("PermissionError: [Errno 13] Permission denied: 'config.json'"))
check("★ 负对照：训练正常结束（输出里没这些字）时不触发重试",
      not studio._rvc_spec_race("训练已完成，正在保存最终模型\n退出码 0"))

print("\n=== B. 真跑一次完整训练（预处理 → 提特征 → 训练 → 建索引）===")
rid, linked, secs, _ = _plant("RVC真训验收（临时）", N_SLICE)
check("自足种出可用素材（硬链接 %d 段 / %.1f 秒）" % (linked, secs),
      linked >= 10 and secs >= 60.0, "段=%d 秒=%.1f" % (linked, secs))
if linked < 10:
    _purge(rid)
    print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
    sys.exit(1)

job = studio.new_job(rid, "train_rvc", "RVC 真训验收 %d 轮" % EPOCHS,
                     epochs_total=EPOCHS)
err = None
try:
    studio.train_rvc(job, rid, epochs=EPOCHS, sr="40k", quality="standard")
except Exception as e:
    err = "%s: %s" % (type(e).__name__, e)
check("★ 训练整条链跑完没有异常", err is None, (err or "")[:300])

print("\n=== C. 产物：文件在 ≠ 能用 ===")
wdir = os.path.join(R, "assets", "weights")
ws = sorted(f for f in (os.listdir(wdir) if os.path.isdir(wdir) else [])
            if f.startswith(rid) and f.endswith(".pth"))
check("权重落盘了（assets/weights/%s*.pth）" % rid, bool(ws), str(ws))
if ws:
    big = max(os.path.getsize(os.path.join(wdir, f)) for f in ws)
    check("权重大小正常（>10 MB，不是空壳）", big > 10 * 1048576,
          "最大 %.1f MB" % (big / 1048576.0))
    rc = subprocess.run([RVC_PY, "-s", CKPT, os.path.join(wdir, ws[0])],
                        cwd=R, capture_output=True, text=True,
                        timeout=600).returncode
    check("★ 权重**真能加载**（torch 读得进去，不是只有个文件名）", rc == 0,
          "rc=%d" % rc)
# ★ 负对照：垃圾文件必须被判不可用 —— 一个永远返回 0 的尺子等于没尺子
junk = os.path.join(wdir, "_vs_junk_rvc.pth")
with open(junk, "wb") as fh:
    fh.write(b"this is definitely not a torch checkpoint" * 100)
try:
    rc_j = subprocess.run([RVC_PY, "-s", CKPT, junk], cwd=R,
                          capture_output=True, text=True, timeout=600).returncode
    check("★ 负对照：垃圾 .pth 被判不可用（退出码非 0）", rc_j != 0, "rc=%d" % rc_j)
finally:
    with contextlib.suppress(Exception):
        os.remove(junk)

idir = os.path.join(R, "assets", "indices")
idxf = [f for f in (os.listdir(idir) if os.path.isdir(idir) else []) if rid in f]
check("★ 索引建成，且命名能被 _find_rvc_index 认出来（否则检索静默降级成 0）",
      bool(idxf) and bool(studio._find_rvc_index(rid)),
      "%s / _find_rvc_index=%s" % (idxf, studio._find_rvc_index(rid)))

print("\n=== D. 训练结果写回角色卡 ===")
rvc = (studio.get_role(rid) or {}).get("rvc") or {}
check("角色卡拉到了歌声模型（model + 轮数）",
      bool(rvc.get("model")) and (rvc.get("epochs") or 0) >= EPOCHS,
      json.dumps(rvc, ensure_ascii=False))
check("★ 底模真的用上了（pretrained=True）—— 没用底模就是「唱出来不跟调」",
      rvc.get("pretrained") is True, "pretrained=%r" % rvc.get("pretrained"))
check("★ 进度条真的在动（epochs_done 被推进过，不是停在开训那一刻）",
      (job.get("epochs_done") or 0) >= EPOCHS,
      "epochs_done=%r（RVC 打的是中文「轮次：N」，只认英文 Epoch 会一直卡住）"
      % job.get("epochs_done"))

print("\n=== E. 续训语义：目标轮数不比已训轮数大就要说清楚 ===")
# 诊断：闸门靠的就是这个数。它一偏小，闸门就形同虚设 —— 用户已训 300 轮再要 150 轮
# 时，RVC 的循环 range(300,151) 一次都不跑，末尾引用未绑定的 epoch 直接 NameError。
_d_now = studio.rvc_trained_epochs(rid, use_cache=False)
print("  [诊断] 训练后读到的已训轮数：%r（缓存值 %r）"
      % (_d_now, studio._RVC_STATE_CACHE.get(rid)))
check("★ 刚训完就问「已训到第几轮」，必须答得出 %d（答 0 的话续训闸门是死的）"
      % EPOCHS, (_d_now or 0) >= EPOCHS, "读到 %r" % _d_now)
_err2 = ""
try:
    studio.train_rvc(studio.new_job(rid, "train_rvc", "续训验收"),
                     rid, epochs=EPOCHS)      # 已经训到 15，还只要 15
except Exception as e:
    _err2 = "%s" % e
check("★ 负对照：已训到 %d 轮再要 %d 轮时明确拒绝并说明怎么接着训"
      % (EPOCHS, EPOCHS),
      bool(_err2) and "没有更多可训的轮数" in _err2, (_err2 or "没拒绝")[:200])

print("\n=== F. 产物清单要列得全（列不全 ⇒ 以后就清不干净）===")
arts = studio.rvc_artifacts(rid)
check("★ rvc_artifacts 把三类产物都列出来了（日志目录 / 权重 / 索引）",
      bool(arts["exp"]) and bool(arts["weights"]) and bool(arts["indices"]),
      "exp=%d weights=%d indices=%d" % (len(arts["exp"]), len(arts["weights"]),
                                        len(arts["indices"])))
check("★ 只列**本角色**的东西（不牵连别人的权重）",
      all(os.path.basename(p).startswith(rid) for p in arts["weights"])
      and all(rid in os.path.basename(p) for p in arts["indices"]),
      "%s" % [os.path.basename(p) for p in arts["weights"] + arts["indices"]])

# ★ 汇总和退出码**先于清理**打出来：清理会被本机的批量删除钩子掀翻
#   （钩子抛 SystemExit，except Exception 捕不到）。判据的结论不该被环境
#   的删除预算绑架 —— 清理结果按 _run_regression.sh 的既定规矩，交给 gc 收尾。
print("\nPASS %d / FAIL %d" % (len(OK), len(BAD)))
if BAD:
    print("失败项：")
    for b in BAD:
        print("  - %s" % b)
print("结论：%s" % ("RVC 真训门禁全绿" if not BAD else "RVC 真训门禁有红"))
rc = 1 if BAD else 0

print("\n=== G. 清理（尽力而为，剩下的交给 gc）===")
_purge(rid)
left = _residue(rid)
if left:
    print("⚠ 磁盘上还留着本关建的东西：%s" % left)
    print("  这是本机的批量删除钩子在拦（一条命令累计删除超 50 就抛 SystemExit），")
    print("  不是判据失败。另起一条命令清：")
    print("    bash _run_regression.sh gc")
    print("    rm -rf \"%s/logs/%s\"" % (R, rid))
else:
    print("本关建的临时角色和 RVC 产物都清干净了。")
sys.exit(rc)
