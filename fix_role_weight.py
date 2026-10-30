"""Re-bind existing roles to their real final RVC weight.

Every role trained before the pick_rvc_weight() fix recorded the wrong weight:
`found[-1]` sorted by name picked `..._e90_s1620.pth` over the actual final
`<exp>.pth`, so roles recorded epochs=300 but were bound to a 90-epoch model.
Re-resolve from the weights directory using the corrected rule。

这个脚本可以重复跑（幂等）：每次都从权重目录重新解析一遍，改对了就写回。
"""
import json
import os
import shutil
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"D:\AHC\VoiceStudio")
import studio

RJ = studio.ROLES_JSON
backup = RJ + ".bak-before-weight-fix"
if not os.path.exists(backup):
    shutil.copy(RJ, backup)
data = json.load(open(RJ, encoding="utf-8"))

wdir = os.path.join(studio.RVC_ROOT, "assets", "weights")
if not os.path.isdir(wdir):
    sys.exit("权重目录不存在：%s" % wdir)

changed = 0
for r in data["roles"]:
    rvc = r.get("rvc") or {}
    old = rvc.get("model")
    if not old:
        continue
    cands = [f for f in os.listdir(wdir)
             if f.startswith(r["id"]) and f.endswith(".pth")]
    # base_dir 必须给：候选是裸文件名，不给的话 getmtime 会拿 CWD 去解析，
    # 全变 0，"取最新的"就退化成"取第一个"。
    best = studio.pick_rvc_weight(cands, base_dir=wdir)
    if not best:
        print("%s  [skip] 权重目录里没有 %s*.pth，保持原样（%s）"
              % (r["id"], r["id"], old))
        continue
    idx = studio._find_rvc_index(best)
    rvc["model"] = best
    rvc["index"] = bool(idx)
    we = studio._rvc_weight_epoch(best)
    if we is not None:
        rvc["weight_epoch"] = we
    if best != old:
        changed += 1
    print("%s  %s -> %s | index=%s" % (
        r["id"], old, best, os.path.basename(idx) if idx else "none"))

json.dump(data, open(RJ, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("roles.json updated（改绑 %d 个，备份留在 %s）" % (changed, os.path.basename(backup)))