"""磁盘预检的判据自检（正+ 负对照）。

要证明的是：**新加的磁盘检查真的在起作用**，不是写了个永远 return 的空壳。

判定逻辑（注意别写反）：
- 注入缺陷（把预检换成永远放行）后，明显不够的场景**必须拦不住** ——
  这才说明原来的预检确实在拦。
- 还原后，同样场景**必须拦得住**，而够用的场景**必须放行**。
三件事同时成立 ⇒ 判据有效。任何一条不符 ⇒ 判据失效。
"""
import sys

sys.path.insert(0, ".")
import studio  # noqa: E402


def try_check(need_gb):
    job = studio.new_job(None, "train", "判据自检")
    try:
        studio.step_check_disk(job, need_gb=need_gb)
        return False, ""
    except studio.Fail as e:
        return True, str(e).split("\n")[0]


print("=== 第1 步：注入缺陷（预检换成永远放行）—— 期望拦不住 ===")
_real = studio.step_check_disk
studio.step_check_disk = lambda job, need_gb=6.0: None
blk1, _ = try_check(500.0)
print("  磁盘只剩 %.0f GB 却要求 500 GB -> %s（期望：放行）"
      % ((studio._disk_free(studio.GSV_ROOT) or 0) / 1024.0 ** 3,
         "放行" if not blk1 else "拦截"))

print()
print("=== 第 2 步：还原真实实现 ===")
studio.step_check_disk = _real
blk2, msg2 = try_check(500.0)
print("  磁盘不足却要求 500 GB -> %s（期望：拦截）" % ("拦截" if blk2 else "放行"))
if blk2:
    print("      提示语: %s" % msg2[:76])

blk3, _ = try_check(6.0)
print("  磁盘充足却要求 6 GB   -> %s（期望：放行）" % ("拦截" if blk3 else "放行"))

print()
# 三条同时成立才算有效：注入后放行(判据确实在拦) + 还原后拦截 + 充足时放行
ok = (not blk1) and blk2 and (not blk3)
print("结论:", "判据有效" if ok else "★判据失效")
# 汇总行（供 `_run_regression.sh snapshot` 统计判据数）：三条，见上面的注释。
print("通过 %d 项，失败 %d 项"
      % (int(not blk1) + int(blk2) + int(not blk3),
         int(blk1) + int(not blk2) + int(blk3)))
sys.exit(0 if ok else 1)
