r"""
「训练轮数 / 接着训」这条链路的验收。

用户在界面上真正会问的三件事，每件都对应下面一组判据：
  · 可以反复训练吗           -> clamp_epochs（上限收口）+ 训练入口接受自定义轮数
  · 训完一次再训一次有效吗   -> rvc_trained_epochs 读得准 + train_rvc 的闸门
  · 只能 300 轮吗            -> 上限是 5000，300 只是"完美档"的推荐值

另外覆盖本轮修掉的三个坑：
  · pick_rvc_weight 裸权重按 mtime（旧写法 max() 是字典序，注释和代码不符）
  · pick_rvc_weight 空列表（旧写法 sorted()[-1] 会 IndexError）
  · _find_rvc_index 带轮数尾巴的模型名也要能找到索引

★ 每条新判据都配负对照：把实现换回旧写法，判据必须**真的变红**。
   没红过 = 没验过。运行：python verify_train_rounds.py
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import studio  # noqa: E402

BASE = "http://127.0.0.1:8756"

OK = []
NG = []


def check(name, cond, detail=""):
    (OK if cond else NG).append(name)
    print("%s %s%s" % ("  ✓" if cond else "  ✗", name,
                       ("   " + detail) if detail else ""))
    return cond


def section(t):
    print("\n== %s" % t)


# --------------------------------------------------------------- 1. 选权重
section("1. pick_rvc_weight：挑「训得最充分」的那一份")

named_and_bare = ["rX_e90_s1620.pth", "rX_e150_s1200.pth", "rX_e300_s5400.pth", "rX.pth"]
get = studio.pick_rvc_weight(named_and_bare)
check("有最终权重（裸名）时优先用它", get == "rX.pth", "得到 %s" % get)

only_named = ["rX_e90_s1620.pth", "rX_e150_s1200.pth", "rX_e300_s5400.pth"]
get = studio.pick_rvc_weight(only_named)
check("没有最终权重时，按轮数**数值**取最大（不是字典序）",
      get == "rX_e300_s5400.pth", "得到 %s（字典序会给出 e90）" % get)

check("空候选不再 IndexError", studio.pick_rvc_weight([]) is None)


def old_pick(names):
    """负对照：改动之前的实现（注释说按 mtime，代码却是字典序；空列表会炸）。"""
    named = [f for f in names if studio._rvc_weight_epoch(os.path.basename(f)) is not None]
    bare = [f for f in names if studio._rvc_weight_epoch(os.path.basename(f)) is None]
    if bare:
        return max(bare)
    if named:
        return max(named, key=lambda f: studio._rvc_weight_epoch(os.path.basename(f)))
    return sorted(names)[-1]


try:
    old_pick([])
    neg_ok = False
except IndexError:
    neg_ok = True
check("负对照：旧实现的空候选会 IndexError（说明上面那条不是废话）", neg_ok)

# 多个裸权重 -> 按 mtime。构造字典序与 mtime 相反的两个文件，才能区分两种实现：
#   "rX_a.pth" 字典序更小但**更新** -> 新实现选它，旧实现(max 字典序)选 rX_z.pth
tmpw = tempfile.mkdtemp(prefix="vs_w_")
try:
    pa = os.path.join(tmpw, "rX_a.pth")
    pz = os.path.join(tmpw, "rX_z.pth")
    open(pz, "w").write("old")
    time.sleep(0.05)
    open(pa, "w").write("new")
    got = studio.pick_rvc_weight([pz, pa], base_dir=tmpw)
    check("多个裸权重时按 mtime 取最新（不是字典序 max）",
          os.path.basename(got) == "rX_a.pth",
          "得到 %s" % os.path.basename(got))
    neg = old_pick([pz, pa])
    check("负对照：旧实现同样输入给出 rX_z.pth（两种实现结论确实不同）",
          os.path.basename(neg) == "rX_z.pth", "旧实现得到 %s" % os.path.basename(neg))
    # base_dir 的作用：不给 base_dir 时裸文件名拿 CWD 解析，mtime 全变 0
    norel = studio.pick_rvc_weight(["rX_a.pth", "rX_z.pth"])
    check("裸文件名 + 不给 base_dir 时不会崩（退化路径）", norel in ("rX_a.pth", "rX_z.pth"))
finally:
    shutil.rmtree(tmpw, ignore_errors=True)


# --------------------------------------------------- 2. 已训轮数（真读 ckpt）
section("2. rvc_trained_epochs：读得准 + 按 mtime 缓存")

PROBE_EXP = "_trprobe"
PROBE_MADE = []


def plant_probe_exp(n):
    """自己种一个能读出轮数的 RVC 实验，让这一段不依赖"库里恰好有个训好的角色"。

    rvc_state.py 是**真的 torch.load 开 ckpt 读 iteration 字段**（不是看文件名），
    所以探针也必须是真的 ckpt：用 RVC 自己的 python 存一个只有几个字节的小字典。
    torch 只 import 一次，几秒钟。
    """
    ed = os.path.join(studio.RVC_ROOT, "logs", PROBE_EXP)
    os.makedirs(ed, exist_ok=True)
    gp = os.path.join(ed, "G_%d.pth" % n)
    wp = os.path.join(studio.RVC_ROOT, "assets", "weights",
                      "%s_e%d_s%d.pth" % (PROBE_EXP, n, n * 10))
    os.makedirs(os.path.dirname(wp), exist_ok=True)
    code = ("import torch,sys;"
            "torch.save({'iteration': %d}, r'%s');"
            "torch.save({'iteration': %d}, r'%s')" % (n, gp, n, wp))
    r = studio.subprocess.run('"%s" -s -c "%s"' % (studio.RVC_PY, code),
                              cwd=studio.RVC_ROOT, capture_output=True, timeout=180)
    if r.returncode != 0 or not os.path.isfile(gp):
        print("  ! 探针 ckpt 造不出来：%s"
              % (r.stderr or b"").decode("utf-8", "ignore")[-160:])
        return None
    PROBE_MADE.extend([ed, wp])   # 用 extend 不重新绑定：函数里 `+=` 会把它当局部变量
    print("  （本机 %s 这个实验读不到轮数，已种下探针实验 %s = %d 轮来验证读法本身）"
          % (EXP, PROBE_EXP, n))
    return PROBE_EXP


EXP = "r8c323d43"
studio._RVC_STATE_CACHE.clear()
try:
    if studio.rvc_trained_epochs(EXP) <= 0:
        EXP = plant_probe_exp(100) or EXP
except Exception:
    EXP = plant_probe_exp(100) or EXP
if not EXP or not os.path.isdir(os.path.join(studio.RVC_ROOT, "logs", EXP)):
    print("  ! 跳过：本机没有 %s 这个 RVC 实验（先训一个角色再跑本脚本）" % EXP)
else:
    studio._RVC_STATE_CACHE.clear()
    t0 = time.time()
    n1 = studio.rvc_trained_epochs(EXP)
    t1 = time.time()
    check("能从 ckpt 读出已训轮数", n1 > 0, "%d 轮（耗时 %.1fs）" % (n1, t1 - t0))
    # 用权重文件里的最大轮数交叉验证：G_*.pth 的 epoch 应等于 assets/weights 里
    # 最大的 _e<N>，或者等于最终权重对应的轮数（两者必须一致）
    wdir = os.path.join(studio.RVC_ROOT, "assets", "weights")
    eps = [studio._rvc_weight_epoch(f) for f in os.listdir(wdir) if f.startswith(EXP)]
    eps = [e for e in eps if e]
    check("与权重文件名里的最大轮数一致", (not eps) or max(eps) == n1,
          "文件名最大 %s / ckpt 读到 %s" % (max(eps) if eps else "-", n1))

    # 缓存：第二次调用必须快，而且把子进程掐掉也照样给对答案
    t2 = time.time()
    n2 = studio.rvc_trained_epochs(EXP)
    t3 = time.time()
    check("第二次调用走缓存（明显更快）", n2 == n1 and (t3 - t2) < 0.5,
          "%.3fs" % (t3 - t2))

    real_run = studio.subprocess.run

    def boom(*a, **k):
        raise AssertionError("缓存没生效，又去起子进程了")

    studio.subprocess.run = boom
    try:
        n3 = studio.rvc_trained_epochs(EXP)
    finally:
        studio.subprocess.run = real_run
    # 注意这条**不**用来证明"走了缓存"：把子进程掐掉时，实现里"读失败就回上一
    # 次的值"这条兜底也会给出同一个答案，所以它绿了不等于缓存生效。
    # 真正区分缓存的是上面那条计时断言（实测：禁掉缓存后它从 0.000s 变 2.7s）。
    # 这条只证明用户看得见的那个性质：RVC 环境临时起不来，也不该让界面瞎报。
    check("子进程临时起不来时仍能给出上次问到的值（兜底，不证明缓存）",
          n3 == n1, "得到 %s" % n3)

    studio._RVC_STATE_CACHE.clear()
    studio.subprocess.run = boom
    try:
        n4 = studio.rvc_trained_epochs(EXP)
    finally:
        studio.subprocess.run = real_run
    check("负对照：清掉缓存后同样条件下拿不到值，返回 0", n4 == 0, "得到 %s" % n4)
    studio._RVC_STATE_CACHE.clear()


# --------------------------------------------------------------- 3. 找索引
section("3. _find_rvc_index：带轮数尾巴的模型名也要能找到索引")


class _FakeIdx(object):
    """临时造一个 indices 目录，避免依赖本机真实训练产物。

    目录结构要照抄真实的：<root>/assets/indices/<x>.index，
    而 <root> 必须是**自己新建的一层**（不能是系统临时目录本身，
    否则 __exit__ 里的 rmtree 会把整个 Temp 目录端掉）。
    """
    def __init__(self, files):
        self.base = tempfile.mkdtemp(prefix="vs_idx_")
        self.idxdir = os.path.join(self.base, "assets", "indices")
        os.makedirs(self.idxdir, exist_ok=True)
        for f in files:
            open(os.path.join(self.idxdir, f), "w").write("x")
        self._old = studio.RVC_ROOT

    def __enter__(self):
        studio.RVC_ROOT = self.base
        return self

    def __exit__(self, *a):
        studio.RVC_ROOT = self._old
        shutil.rmtree(self.base, ignore_errors=True)


with _FakeIdx(["rr_added_IVF10_Flat_nprobe_1_rr_v1.index",
               "trained_IVF10_Flat_nprobe_1_rr_v1.index"]):
    a = studio._find_rvc_index("rr.pth")
    b = studio._find_rvc_index("rr_e150_s1200.pth")
    check("最终权重 rr.pth 能找到索引", bool(a) and a.endswith("rr_added_IVF10_Flat_nprobe_1_rr_v1.index"),
          str(a))
    check("带轮数尾巴 rr_e150_s1200.pth 也能找到同一份索引", a == b, str(b))
    check("名字里带 trained 的合并索引要跳过（不是 added）",
          bool(a) and "trained" not in os.path.basename(a))

with _FakeIdx(["trained_IVF10_Flat_nprobe_1_rr_v1.index"]):
    c = studio._find_rvc_index("rr.pth")
    check("负对照：只有 trained 索引时返回 None（上面那条不是蒙对的）", c is None, str(c))


# ------------------------------------------------------------- 4. 上限收口
section("4. clamp_epochs：上限收口（说话 200 / 唱歌 5000，300 不是天花板）")

check("唱歌 300 轮原样通过（300 是推荐值不是上限）",
      studio.clamp_epochs(300, "rvc")[0] == 300)
check("唱歌 3000 轮原样通过", studio.clamp_epochs(3000, "rvc")[0] == 3000)
check("唱歌 6000 轮被收进 5000", studio.clamp_epochs(6000, "rvc")[0] == 5000)
check("说话 300 轮被收进 200", studio.clamp_epochs(300, "gsv")[0] == 200)
check("说话 200 轮原样通过", studio.clamp_epochs(200, "gsv")[0] == 200)
check("0 / 负数 / 非数字都当成「没填」", (
    studio.clamp_epochs(0)[0] == 0
    and studio.clamp_epochs(-5)[0] == 0
    and studio.clamp_epochs("abc")[0] == 0))
check("超上限时有给用户看的说明", bool(studio.clamp_epochs(6000, "rvc")[1]))


# ------------------------------------------------- 5. train_rvc 的「已训满」闸门
section("5. train_rvc 的闸门：已训满要拦得住，没训满不能拦")

# 用临时 ROLES_DIR / ROLES_JSON / JOBS_DIR / RVC_ROOT 做隔离，绝不碰真实数据
tmp = tempfile.mkdtemp(prefix="vs_role_")
old = {k: getattr(studio, k) for k in
       ("ROLES_DIR", "ROLES_JSON", "JOBS_DIR", "RVC_ROOT")}
old_state, old_run = studio.rvc_trained_epochs, studio.run
try:
    studio.ROLES_DIR = os.path.join(tmp, "roles")
    studio.ROLES_JSON = os.path.join(tmp, "roles.json")
    studio.JOBS_DIR = os.path.join(tmp, "jobs")
    # RVC_ROOT 也要换掉：闸门没拦住时会真的去 logs/<exp> 建目录，
    # 不换就会在 RVC 的真实目录树里留下垃圾。
    studio.RVC_ROOT = os.path.join(tmp, "rvc")
    for d in (studio.ROLES_DIR, studio.JOBS_DIR, studio.RVC_ROOT):
        os.makedirs(d, exist_ok=True)
    os.makedirs(os.path.join(studio.ROLES_DIR, "rTEST", "slices"), exist_ok=True)
    # 10 段以上的假素材（train_rvc 要求 >=10 段且合计 >=1 分钟）
    segs = [{"file": "s%02d.wav" % i, "dur": 8.0, "text": "x", "voiced": 0.5}
            for i in range(12)]
    role = {"id": "rTEST", "name": "t", "kind": "speech", "segments": segs,
            "outputs": [], "source": "verify"}
    with open(studio.ROLES_JSON, "w", encoding="utf-8") as f:
        json.dump({"roles": [role]}, f, ensure_ascii=False)

    job = studio.new_job("rTEST", "rvc", "t")

    class Sentinel(Exception):
        pass

    def sentinel_run(*a, **k):
        raise Sentinel()

    # (a) 已经训到 300 轮，又要 300 轮 -> 必须拦住，而且**一步子进程都不许起**
    studio.rvc_trained_epochs = lambda rid, use_cache=True: 300
    studio.run = sentinel_run
    msg = ""
    try:
        studio.train_rvc(job, "rTEST", epochs=300)
        blocked = False
    except Sentinel:
        blocked, msg = False, "闸门没拦住，直接开跑了"
    except studio.Fail as e:
        blocked, msg = True, str(e)
    check("已训 300 又要 300：拦得住且不起训练进程", blocked, msg.split("\n")[0][:70])

    check("拦截文案要给出「接着往下训」的可操作做法（不是叫用户去加素材）",
          ("自定义" in msg) and ("接着" in msg) and ("加素材" not in msg.split("（")[0]),
          "含自定义/接着=%s" % (("自定义" in msg) and ("接着" in msg)))

    # (b) 负对照：没训过（done=0）时，闸门**不该**触发 —— 应该走到 run 被叫到
    studio.rvc_trained_epochs = lambda rid, use_cache=True: 0
    studio.run = sentinel_run
    passed = False
    try:
        studio.train_rvc(job, "rTEST", epochs=300)
    except Sentinel:
        passed = True
    except studio.Fail as e:
        passed = False
        msg = str(e)
    check("负对照：没训过时闸门不触发（说明拦住的是 done>=epochs，不是别的原因）", passed)
finally:
    for k, v in old.items():
        setattr(studio, k, v)
    studio.rvc_trained_epochs, studio.run = old_state, old_run
    shutil.rmtree(tmp, ignore_errors=True)


# -------------------------------------------------------------- 6. HTTP 接口
section("6. HTTP /api/role/<id>/rvc_state")

def http_get(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=30) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))
    except Exception as e:
        return None, {"error": str(e)}


try:
    with urllib.request.urlopen(BASE + "/api/roles", timeout=5) as r:
        roles = json.loads(r.read().decode("utf-8"))["roles"]
except Exception as e:
    roles = []
    print("  ! 服务没在 %s 上跑（%s），HTTP 段跳过" % (BASE, e))

if roles:
    rid = roles[0]["id"]
    st, body = http_get("/api/role/%s/rvc_state" % rid)
    check("GET rvc_state 返回 200", st == 200, "status=%s" % st)
    check("返回里带 epochs 且是整数",
          isinstance(body.get("epochs"), int), str(body.get("epochs")))
    st2, _ = http_get("/api/role/no_such_role/rvc_state")
    check("不存在的角色返回 404", st2 == 404, "status=%s" % st2)
    st3, _ = http_get("/api/role/%s" % rid)
    check("负对照：/api/role/<id> 本身仍正常（没被新路由抢走）", st3 == 200, "status=%s" % st3)


# 探针实验用完就清：留着会污染"存储面板"和"已训到第几轮"的真实读数
for p in PROBE_MADE:
    try:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif os.path.isfile(p):
            os.remove(p)
    except Exception:
        pass
studio._RVC_STATE_CACHE.clear()


# ------------------------------------------------------------------ 汇总
print("\n" + "=" * 56)
print("通过 %d 项，失败 %d 项" % (len(OK), len(NG)))
if NG:
    print("失败项：")
    for n in NG:
        print("  ✗ %s" % n)
print("ALLDONE")
sys.exit(1 if NG else 0)
