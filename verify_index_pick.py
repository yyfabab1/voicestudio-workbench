# -*- coding: utf-8 -*-
r"""门禁：RVC 的「索引归属」必须选对，选错也不许把整首歌搞崩。

为什么单开一关（真事故，2026-10-20）：
    同一个角色目录下多试过几个模型之后，用**老模型**唱歌会直接失败：

        ra500fb92.pth（v1，256 维输出）
          → 被配上 ra500fb92_v2_klm48_..._v2.index（v2，768 维）
          → faiss 里 `assert d == self.d` 崩
          → 整首判"歌声转换失败"，日志只有一行 AssertionError

    根因有两层，两层都得钉住：
      ① RVC 自带的 `get_index_path_from_model()` 认的是"名字里出现过实验名"，
         最后的 tie-break 是 `-mtime`（最新的赢）⇒ 老模型永远配最新的索引。
         实测：`get_index_path_from_model("ra500fb92.pth", 0)` 返回的确实是
         v2_klm48 的索引。
      ② `studio.py` 的降级条件写的是 `"added index" in out`，而错配的症状是
         faiss 的 assert，字串对不上 ⇒ 不降级 ⇒ 整首失败。
    而"多试几个模型"恰恰是正常用法，不是边界情况。

本关只做**静态 + 隔离目录**的检查（不跑推理），几秒钟跑完：
    A. 索引名 → 归属模型的解析规则
    B. 在隔离的索引目录里挑：只挑自己的、多个时挑最新的自己的、别人的不挑
    C. 源码保证：错配时必须降级重试，不能直接判失败
    D. 负对照：用"旧规则"（最新文件优先）跑同一组数据，必须**选错**
       —— 这一条保证 A/B 不是在自说自话：同样的数据换成旧逻辑就该红。

退出码：0 全部通过；1 有失败。
"""
import ast
import os
import re
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, "stages")
sys.path.insert(0, HERE)
sys.path.insert(0, STAGES)

import rvc_infer  # noqa: E402

N_PASS = 0
N_FAIL = 0
FAILS = []


def check(name, cond, detail=""):
    global N_PASS, N_FAIL
    if cond:
        N_PASS += 1
        print("  [ok] %s" % name)
    else:
        N_FAIL += 1
        FAILS.append(name)
        print("  [FAIL] %s%s" % (name, ("  ← " + detail) if detail else ""))


def sec(t):
    print("\n== %s ==" % t)


def old_rule_pick(root, model_stem):
    """复刻 RVC 原规则（含它的缺陷），只用来做负对照。

    和 infer/vc/utils.py 的 get_index_path_from_model 同口径：
      · 跳过 trained；
      · 认 `<exp>_added_*` / 含 `_<exp>_v1` / 含 `_<exp>_v2` / 名字里含完整模型名；
      · 最后的 tie-break 是 **-mtime（最新赢）**。
    """
    exp = re.sub(r"_e\d+_s\d+$", "", model_stem, flags=re.IGNORECASE)
    le, ls = exp.lower(), model_stem.lower()
    cands = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(".index") or "trained" in name.lower():
                continue
            low = os.path.splitext(name)[0].lower()
            if (low.startswith(le + "_added_")
                    or ("_" + le + "_v1") in low
                    or ("_" + le + "_v2") in low
                    or ls in low):
                p = os.path.join(dirpath, name)
                cands.append((os.path.getmtime(p), p))
    if not cands:
        return ""
    return min(cands, key=lambda x: (-x[0], x[1].lower()))[1]


def a_owner():
    sec("A. 索引名 → 归属模型")
    cases = [
        ("ra500fb92_added_IVF1700_Flat_nprobe_1_ra500fb92_v1.index", "ra500fb92"),
        ("ra500fb92_v2_klm48_added_IVF1700_Flat_nprobe_1_ra500fb92_v2_klm48_v2.index",
         "ra500fb92_v2_klm48"),
        ("MyRole_e150_s1200_added_IVF100_Flat_x_v1.index", "myrole_e150_s1200"),
        ("merged_trained_IVF100.index", ""),
        ("plainname.index", ""),
        ("", ""),
    ]
    for name, want in cases:
        got = rvc_infer._index_owner(name)
        check("归属解析 %-28s → %r" % (name[:28], want), got == want, "实际 %r" % got)
    # 大小写：RVC 产出的名字可能是大小写混合，归属比较必须忽略大小写
    check("归属解析忽略大小写",
          rvc_infer._index_owner("ABC_ADDED_x.index") == "abc")


def _mk(root, name, age=0.0):
    p = os.path.join(root, name)
    with open(p, "wb") as f:
        f.write(b"\0")
    t = time.time() - age
    os.utime(p, (t, t))
    return p


def b_pick():
    sec("B. 隔离目录里的挑选（真实调用 pick_index）")
    tmp = tempfile.mkdtemp(prefix="vsidx_")
    idx_dir = os.path.join(tmp, "indices")
    os.makedirs(idx_dir)
    old_out = os.environ.get("outside_index_root")
    old_in = os.environ.get("index_root")
    try:
        # 自己的索引：老一点
        mine = _mk(idx_dir, "ra500fb92_added_IVF1700_Flat_nprobe_1_ra500fb92_v1.index", age=900)
        # 同角色另一个模型（v2_klm48）的索引：新得多 —— 这是会骗过旧规则的诱饵
        bait = _mk(idx_dir,
                   "ra500fb92_v2_klm48_added_IVF1700_Flat_nprobe_1_ra500fb92_v2_klm48_v2.index",
                   age=10)
        # 完全无关的模型
        _mk(idx_dir, "someoneelse_added_IVF100_Flat_x_v1.index", age=5)

        os.environ["outside_index_root"] = idx_dir
        os.environ.pop("index_root", None)

        got = rvc_infer.pick_index("ra500fb92.pth", 0)
        check("v1 模型选中自己的索引", got and os.path.basename(got) == os.path.basename(mine),
              "实际 %s" % os.path.basename(got or "-"))
        check("v1 模型**不**被更新的 v2 索引骗走", bool(got) and "klm48" not in os.path.basename(got))
        check("不挑别的模型的索引", bool(got) and "someoneelse" not in os.path.basename(got))

        got2 = rvc_infer.pick_index("ra500fb92_v2_klm48.pth", 0)
        check("v2_klm48 模型选中自己的索引",
              got2 and os.path.basename(got2) == os.path.basename(bait),
              "实际 %s" % os.path.basename(got2 or "-"))

        # D. 负对照：**在加"同名新份"之前**、用同一组初始数据喂给旧规则，必须选错。
        #   （顺序很重要：如果先加了更新的同名索引，旧规则会挑那一个 —— 那是"巧合选对"，
        #     会让负对照变成假绿。原来就写错了一版，被它自己抓出来了。）
        sec("D. 负对照（旧规则在同一组数据上必须选错）")
        old = old_rule_pick(idx_dir, "ra500fb92")
        check("旧规则会挑走诱饵那一份（证明诱饵真的能骗人）",
              bool(old) and "klm48" in os.path.basename(old),
              "旧规则选了 %s" % os.path.basename(old or "-"))
        check("旧规则的选择 ≠ 新规则的选择（两套逻辑确实不同）",
              os.path.basename(old or "-") != os.path.basename(got or ""))

        # 同名多份：拿最新的那份自己的
        newer = _mk(idx_dir, "ra500fb92_added_IVF3000_Flat_nprobe_1_ra500fb92_v1.index", age=1)
        got3 = rvc_infer.pick_index("ra500fb92.pth", 0)
        check("同名多份取最新的一份", got3 and os.path.basename(got3) == os.path.basename(newer),
              "实际 %s" % os.path.basename(got3 or "-"))

        check("没有索引的模型返回空", rvc_infer.pick_index("nobody.pth", 0) == "")
        check("空模型名返回空", rvc_infer.pick_index("", 0) == "")
    finally:
        if old_out is None:
            os.environ.pop("outside_index_root", None)
        else:
            os.environ["outside_index_root"] = old_out
        if old_in is not None:
            os.environ["index_root"] = old_in
        shutil.rmtree(tmp, ignore_errors=True)


def _call_names(node):
    """这个节点里出现的所有函数调用名（最后一段）。"""
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _uses(node, name):
    """这个节点的表达式里有没有出现某个名字 / 属性（如 a.index_rate）。"""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == name:
            return True
        if isinstance(n, ast.Attribute) and n.attr == name:
            return True
    return False


def c_source():
    sec("C. 源码保证：错配必须降级，不许直接判失败")
    src = open(os.path.join(STAGES, "rvc_infer.py"), encoding="utf-8").read()
    tree = ast.parse(src)

    # ★ 用语法树查"条件里真的用了 index_rate 且分支里真的重跑 vc_single"，
    #   不是查"文件里有没有这句话" —— 第一版就是查字串，被一个
    #   `if False:` 的变异当场骗过（判据自己假绿）。
    retry = []
    for n in ast.walk(tree):
        if not isinstance(n, ast.If):
            continue
        if not _uses(n.test, "index_rate"):
            continue
        if "vc_single" in _call_names(n):
            retry.append(n)
    check("rvc_infer 有「带索引失败 → 去掉索引重试」分支（语法树级）", bool(retry))
    if retry:
        txt = ast.get_source_segment(src, retry[0].test) or ""
        check("重试条件是「结果为空 且 index_rate > 0」",
              "result" in txt and "index_rate" in txt, "实际条件：%s" % txt)
        body = ast.get_source_segment(src, retry[0]) or ""
        check("重试时确实把 index_rate 归零", "index_rate = 0.0" in body)

    check("rvc_infer 优先用归属匹配，回落到 RVC 原规则",
          "pick_index(" in src and "get_index_path_from_model" in src)

    st = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    stree = ast.parse(st)
    i = st.find("def _find_rvc_index")
    j = st.find("\ndef ", i + 1) if i >= 0 else -1
    body = st[i:j] if i >= 0 and j > i else ""
    check("studio._find_rvc_index 用归属规则（_owner）", "_owner" in body and "_added_" in body)

    # studio 的降级：条件里要有 rc 与 index_rate，分支里要重跑 _rvc(..., 0)
    studio_retry = []
    for n in ast.walk(stree):
        if not isinstance(n, ast.If):
            continue
        if not (_uses(n.test, "rc") and _uses(n.test, "index_rate")):
            continue
        if "_rvc" in _call_names(n):
            studio_retry.append(n)
    check("studio 有「带索引失败就重跑一遍」的分支（语法树级）", bool(studio_retry))
    if studio_retry:
        seg = ast.get_source_segment(st, studio_retry[0]) or ""
        check("studio 的降级不再依赖 'added index' 字串",
              "added index" not in seg, "分支里仍有该字串")
        check("studio 重跑时把 index_rate 传 0", re.search(r"_rvc\([^)]*,\s*0\s*\)", seg) is not None)


def d_consistency():
    sec("E. 两边口径一致（否则界面说有索引、推理在降级）")
    # studio 的 _owner 与 rvc_infer._index_owner 对同一批名字要给同一个答案
    names = [
        "ra500fb92_added_IVF1700_Flat_nprobe_1_ra500fb92_v1.index",
        "ra500fb92_v2_klm48_added_IVF1700_Flat_nprobe_1_ra500fb92_v2_klm48_v2.index",
        "merged_trained_IVF100.index",
    ]
    st = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    # studio 里的 _owner 是内嵌函数，这里按同样的规则独立复算一遍做对照
    def same_rule(name):
        s = os.path.splitext(name)[0].lower()
        k = s.find("_added_")
        return s[:k] if k > 0 else ""
    ok = True
    for n in names:
        if rvc_infer._index_owner(n) != same_rule(n):
            ok = False
    check("两处归属规则同解", ok)
    check("studio 源码里的 _owner 结构存在", "def _owner(name)" in st)


def f_defaults():
    sec("F. 界面默认值必须与后端实测默认值同源")
    # 事故背景：界面把索引强度写成 0.75，而后端实测选的默认是 0.50。
    # 用户不动那个框 ⇒ 一直用 0.75 ⇒ 八度错 9.9% 升到 11.5%，正好越过
    # 音高体检的坏档线。默认值只能有一处"数字"，另一处必须跟它一致。
    html = open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()
    js = open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
    st = open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()

    m = re.search(r"^RVC_INDEX_RATE\s*=\s*([\d.]+)", st, re.M)
    check("studio 里有 RVC_INDEX_RATE", bool(m))
    want = float(m.group(1)) if m else -1

    mh = re.search(r'id="inpIdx"[^>]*value="([\d.]+)"', html)
    check("界面有索引强度输入框且带默认值", bool(mh))
    if mh and m:
        check("界面默认值 == studio.RVC_INDEX_RATE", abs(float(mh.group(1)) - want) < 1e-9,
              "界面 %s vs 后端 %s" % (mh.group(1), want))

    mj = re.search(r'index_rate:\s*parseFloat\(\$\("#inpIdx"\)\.value\s*\|\|\s*"([\d.]+)"\)', js)
    check("app.js 的兜底值存在", bool(mj))
    if mj and m:
        check("app.js 兜底值 == studio.RVC_INDEX_RATE", abs(float(mj.group(1)) - want) < 1e-9,
              "兜底 %s vs 后端 %s" % (mj.group(1), want))

    # 高频补偿：四档齐全、默认关、前端要真的把选择发出去
    for lv in ("off", "light", "mid", "strong"):
        check("界面有高频补偿档位 %s" % lv, ('value="%s"' % lv) in html)
    check("高频补偿默认关（不替用户决定清晰度 vs 音色）",
          bool(re.search(r'id="selHf"[\s\S]{0,400}?value="off"\s+selected', html)))
    check("app.js 把 hf 发给后端", re.search(r'hf:\s*\(\$\("#selHf"\)', js) is not None)
    sv = open(os.path.join(HERE, "server.py"), encoding="utf-8").read()
    check("server 只接受已知档位（乱传当关处理）",
          'studio.HF_LEVELS' in sv and 'hf=(body.get("hf")' in sv)


def main():
    print("索引归属门禁：RVC 多模型共存时不许配错索引")
    a_owner()
    b_pick()
    c_source()
    d_consistency()
    f_defaults()
    print("\n通过 %d / 失败 %d" % (N_PASS, N_FAIL))
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print("  - %s" % f)
    print("ALLDONE")
    return 0 if N_FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
