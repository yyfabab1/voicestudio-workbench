# -*- coding: utf-8 -*-
"""门禁：多角色合唱的「真·和声声部」（stages/harmony_cli.py + studio 接线）。

为什么单独一道门：
    上一轮回 7th 做出来的和声层"不是主唱的复制品"（相关系数 1.00 → 0.24），
    但它们全在同一个音高上 —— 那还是齐唱。这一步跨出去的是**音高**。
    判据因此围绕三件事：
      ① 挑出来的音程**跟这首歌的伴奏协和**（数据，不是写死的）
      ② 挑出来的音程**彼此协和**、**跟主唱协和**（这两个各自踩过一次坑）
      ③ 失败要优雅降级成齐唱，不许拖死整首歌

负对照（每条都实测能让行为变）：
    - 去掉音乐先验 ⇒ +2（大二度）会被当成最优解
    - 摘掉主唱（0）的协和校验 ⇒ 第三层会挑到 −1（相对主唱是小二度）
    - 允许 0 进和声池 ⇒ 层数不够时拿"同度"兜底（等于白做）
      ⚠ 这条负对照**已经删了**：实测发现 0 有三重防线（pool 过滤 / _fits 的同度
      检查 / prior 把它压到 0.0136 排最末），而且 +12 会在它之前先被选中 ——
      怎么改都混不进来 ⇒ 测不到东西 = 假尺子。详见 D 节末尾那段注释。
    - 取消音质护栏 ⇒ −8（实测 HNR −4.06 dB，太厚）会被放行
"""

import ast
import io
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
N_PASS, N_FAIL = 0, 0
QUIET = False


def check(name, ok, note=""):
    global N_PASS, N_FAIL
    if ok:
        N_PASS += 1
        print("  [ok]   %s%s" % (name, ("  — %s" % note) if note else ""))
    else:
        N_FAIL += 1
        print("  [FAIL] %s%s" % (name, ("  — %s" % note) if note else ""))


def sec(t):
    print("\n== %s ==" % t, flush=True)


def read(p):
    with io.open(p, encoding="utf-8") as f:
        return f.read()


# ---------------------------------------------------------------- 素材
def synth_pair(tmp, sr=22050, secs=14.0):
    """造一对「人声 + 伴奏」，伴奏明确协和于某个和弦（C 大三和弦）。

    人声用 C4 附近的音（261.6 / 293.7 / 329.6）在三个音之间来回，
    伴奏固定铺 C-E-G。这样"移调后重不重合"是**可以算出来的**：
      +4（= E→G# 之类）会踩空，+0/+12/+7 会重合得很好。
    """
    import numpy as np
    import soundfile as sf
    n = int(sr * secs)
    t = np.arange(n, dtype=np.float64) / float(sr)
    notes = [261.63, 293.66, 329.63]          # C4 D4 E4
    y = np.zeros(n, dtype=np.float64)
    for i in range(0, n, int(sr * 1.5)):
        j = min(n, i + int(sr * 1.4))
        f0 = notes[(i // int(sr * 1.5)) % len(notes)]
        seg = np.arange(j - i, dtype=np.float64) / float(sr)
        y[i:j] = sum((0.5 / k) * np.sin(2 * np.pi * f0 * k * seg)
                     for k in range(1, 7))
    # 加一点噪声 ⇒ f0 提取有东西可提取、HNR 也不会是无穷大
    rng = np.random.RandomState(7)
    y = y + 0.010 * rng.randn(n)
    env = np.minimum(1.0, np.arange(n) / (sr * 0.02))
    y = y * env
    sf.write(os.path.join(tmp, "vocal.wav"), (y * 0.25).astype(np.float32), sr)

    acc = np.zeros(n, dtype=np.float64)
    for f0 in (261.63, 329.63, 392.00, 130.81):     # C E G C3
        acc += np.sin(2 * np.pi * f0 * t)
    acc = acc * 0.30 + 0.004 * rng.randn(n)
    sf.write(os.path.join(tmp, "inst.wav"), acc.astype(np.float32), sr)
    return os.path.join(tmp, "vocal.wav"), os.path.join(tmp, "inst.wav")


def run_cli(tmp, src, vocal, inst, n_layers, extra=None):
    """用一段（可能被改坏的）源码跑一次 pick，返回 (报告 dict, 返回码)。"""
    p = os.path.join(tmp, "harmony_%d.py" % (abs(hash(src)) % 99999))
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(src)
    out = os.path.join(tmp, "h.json")
    with open(out, "w", encoding="utf-8") as f:
        f.write("{}")
    r = subprocess.run([sys.executable, p, "pick", vocal, inst, out,
                        "--layers", str(n_layers)],
                       capture_output=True, text=True, cwd=HERE)
    rep = {}
    try:
        with open(out, encoding="utf-8") as f:
            rep = json.load(f)
    except Exception as e:
        rep = {"_err": str(e)}
    if extra:
        rep["_stdout"] = (r.stdout or "").strip()
    return rep, r.returncode


def ring(a, b):
    d = abs((int(a) - int(b)) % 12)
    return min(d, 12 - d)


def all_consonant(semis):
    """任意两层之间、以及每层与主唱之间，都不能是小二度/大二度/七度/三全音/同度。"""
    allv = [0] + [int(s) for s in semis]
    for i in range(len(allv)):
        for j in range(i + 1, len(allv)):
            d = ring(allv[i], allv[j])
            if d < 3 or d == 6:
                return False, (allv[i], allv[j], d)
    return True, None


def main():
    src_path = os.path.join(HERE, "stages", "harmony_cli.py")
    src = read(src_path)
    studio_src = read(os.path.join(HERE, "studio.py"))
    server_src = read(os.path.join(HERE, "server.py"))
    tmp = tempfile.mkdtemp(prefix="harm_gate_")

    sec("A. 源码：这个模块自身站得住吗")
    try:
        tree = ast.parse(src)
        check("语法能被解析", True)
    except SyntaxError as e:
        check("语法能被解析", False, str(e))
        tree = None

    if tree is not None:
        # ★★ 这一条是为我自己栽的两个坑写的（铁律 24e）：延迟加载的模块里，
        #    每个用到 np 的函数都必须自己 import numpy —— 漏了不报错、只是
        #    "功能装了但从不生效"，而且日志里连 NameError 都看不到。
        missing = []
        for fn in [n for n in tree.body if isinstance(n, ast.FunctionDef)]:
            uses = any(isinstance(x, ast.Attribute)
                       and isinstance(x.value, ast.Name) and x.value.id == "np"
                       for x in ast.walk(fn))
            imps = any(isinstance(x, ast.Import)
                       and any(a.name == "numpy" for a in x.names)
                       for x in ast.walk(fn))
            if uses and not imps:
                missing.append(fn.name)
        check("★ 每个用到 np 的函数都自己 import 了 numpy（漏了不报错但功能从不生效）",
              not missing, str(missing) if missing else "0 处遗漏")

    check("PRIOR 把同度压到最低一档（我们要的就是不要齐唱）",
          "0: 0.02" in src.replace(" ", "") or "0: 0.02" in src)
    check("三全音、小二、大二也在先验雷区里",
          "6: 0.05" in src and "1: 0.01" in src)
    check("★「至少小三度间隔」写成明文的协和判据 `_fits`", "def _fits(" in src)
    check("★ 主唱 0 在挑和声层时先放进池子（不放进池子会挑出相对主唱的小二度）",
          "take = [0]" in src)
    check("和声层不许出现 0 本身（漏了层数不够时会拿同度兜底 = 白做）",
          "if int(s) != 0" in src)
    check("音质护栏：|ΔHNR| 上限存在且来自实测拟合", "MAX_PREDICT_DHNR = 3.0" in src)
    check("向下给轻微加权（实测向下 HNR 更低 = 不电），但不能喧宾夺主",
          "DOWN_BONUS = 1.06" in src and "1.06" in src)
    check("文件头记下了『向下 ΔHNR 0.51/半音、向上 1.325+0.085s』的实测公式",
          "0.51" in src and "1.325" in src)
    check("f0 音域闸门存在（太低/太高的档直接废掉）",
          "F0_LO" in src and "F0_HI" in src)
    check("★ 帧数对齐：voiced_mask 与 chroma 同为 1+len/hop（off-by-one 会崩）",
          "n = 1 + len(y) // hop" in src and "vmask = voiced_mask(yv, sr)[:t]" in src)

    sec("B. 端到端：合成素材上真的挑得出协和的音程")
    vocal, inst = synth_pair(tmp)
    rep, rc = run_cli(tmp, src, vocal, inst, 3)
    check("能跑通（退出码 0）", rc == 0, "rc=%d %s" % (rc, rep.get("reason", "")))
    check("识别出调性", bool(rep.get("key")), str(rep.get("key")))
    check("★ 挑出了和声层", bool(rep.get("plan")), str(rep.get("reason", "")))
    semis = [p["semitone"] for p in rep.get("plan", [])]
    check("★ 挑出来的每个都不超过 |12| 半音",
          all(abs(s) <= 12 for s in semis), str(semis))
    ok, bad = all_consonant(semis)
    check("★★ 挑出来的音程两两协和、且与主唱协和（≥ 小三度，不含三全音）",
          ok, str(semis) if ok else "冲突 %s" % str(bad))
    check("★ 没有把「同度」当和声层（那等于白做）", 0 not in semis, str(semis))
    check("每个和声层都报了预计 ΔHNR（有护栏才有得谈副作用）",
          all("dhnr" in p for p in rep.get("plan", [])), str(semis))
    check("预测 ΔHNR 全部在护栏里（|Δ| ≤ 3.0 dB）",
          all(abs(p["dhnr"]) <= 3.0 for p in rep.get("plan", [])),
          str([p["dhnr"] for p in rep.get("plan", [])]))
    check("报告里带了全部候选的分数（事后能查为什么挑它）",
          len(rep.get("candidates") or {}) >= 5,
          str(len(rep.get("candidates") or {})))
    check("★ 层数给不够时如实标 underfull（不是伪造一份看着正常的脏和声）",
          "underfull" in rep,
          "layers=%s got=%s underfull=%s"
          % (rep.get("layers"), rep.get("got_layers"), rep.get("underfull")))

    rep1, _ = run_cli(tmp, src, vocal, inst, 1)
    s1 = [p["semitone"] for p in rep1.get("plan", [])]
    check("只要 1 层时正好给 1 层", len(s1) == 1, str(s1))
    ok1, bad1 = all_consonant(s1)
    check("1 层时那一个也与主唱协和", ok1, str(s1) if ok1 else str(bad1))

    sec("C. studio / server 接线")
    check("sing_multi 多了 harmony_mode 形参", "harmony_mode=\"auto\"" in studio_src)
    check("★ 只在叠唱（mode=chorus）且 ≥2 人时设计和声",
          'mode == "chorus" and len(roles) >= 2 and harmony_mode != "off"' in studio_src)
    check("★ 入口：把音程加进 RVC 的 pitch（而不是后处理移调整条成品）",
          "_hs = int(_hp.get(\"semitone\") or 0)" in studio_src
          and "p += _hs" in studio_src)
    check("第 1 个人永远不加和声（他就是主唱）",
          'if int(_hp.get("layer") or 0) == k' in studio_src)
    check("★ 跑不通时降级成齐唱、并进日志（不许拖死整首歌）",
          "和声设计没跑通" in studio_src)
    check("★ 给不够时会照实说「这首歌只够分 N 个声部」",
          "可用音域只够分" in studio_src)
    check("server 把前端的 harmony 透传给 studio",
          'body.get("harmony")' in server_src and 'harmony_mode=' in server_src)
    check("「设计和声声部」是一个独立步骤条（老板看得见）",
          '"设计和声声部"' in studio_src)

    sec("D. 混凝土的负对照：把这些地方改坏，行为必须变（不能只是代码像）")
    # ★ 自检单独记账：负对照本来就该是红的，混进正式判据就是假红了。
    _sPass = [0]
    _sFail = [0]

    def scheck(name, ok, note=""):
        if ok:
            _sPass[0] += 1
            print("  [ok]   %s%s" % (name, ("  — %s" % note) if note else ""))
        else:
            _sFail[0] += 1
            print("  [FAIL] %s%s" % (name, ("  — %s" % note) if note else ""))

    bad_np = src.replace("    import numpy as np\n    import librosa\n",
                         "    import librosa\n", 1)
    r_np, _ = run_cli(tmp, bad_np, vocal, inst, 2)
    scheck("负对照：chroma_of 漏 import numpy ⇒ 真的跑不出 plan（上面那条静态判据不是摆设）",
           not r_np.get("plan"), str(r_np.get("reason", ""))[:70])

    # ★ 先验的作用层面是"排名"，不是"拦住" —— 真正把脏音程挡在外面的是 `_fits`。
    #   所以这条只能验到"分数被引爆"，验不到"plan 里出现脏音程"。
    #   （先前我写成"plan 里必须出现脏音程"，那条根本红不了 —— 见文件头铁律 11b。）
    def _cscore(rep, semi):
        c = rep.get("candidates") or {}
        for k, v in c.items():
            if int(k) == int(semi):
                return v.get("score")
        return None

    good_trit = _cscore(rep, 6)            # 三全音
    bad_prior = src.replace("return PRIOR.get(abs(int(semi)), 0.05)",
                            "return 1.0", 1)
    r_pr, _ = run_cli(tmp, bad_prior, vocal, inst, 3)
    bad_trit = _cscore(r_pr, 6)
    scheck("负对照：去掉音乐先验 ⇒ 三全音那个候选的分数被引爆（先验真的在压排名）",
           bad_trit is not None and good_trit is not None
           and bad_trit > good_trit * 5,
           "有先验 %.4f → 无先验 %.4f" % (good_trit or -1, bad_trit or -1))

    # ★ `if int(s) != 0` 被摘掉之后行为**不变** —— `_fits` 里那条 `t == s` 同度检查
    #   会接着把 0 挡掉。所以这条**不配叫负对照**（负对照下都不变红的东西，
    #   挂着这个标签会误导后人，铁律 11b），它只是"两道防线互为备份"的证据。
    bad_pool = src.replace("pool = [s for s in sc if int(s) != 0]",
                           "pool = list(sc)", 1)
    r_pool, _ = run_cli(tmp, bad_pool, vocal, inst, 4)
    scheck("（备份，不叫负对照）只摘 pool 过滤不改行为 ⇒ 证明 _fits 那道同度检查真在工作",
           0 not in [p["semitone"] for p in r_pool.get("plan", [])],
           str([p["semitone"] for p in r_pool.get("plan", [])]))

    # ★★ 试过把 pool 过滤和 _fits 的同度检查**一起**摘掉，0 仍然混不进来：
    #    第三道防线是 prior（0 的系数 0.02 ⇒ score 只有 0.0136，永远排最末），
    #    而且 +12（同一个 pitch class 但明确是"高八度"）会在 0 之前被选中。
    #    ⇒ 那条负对照**根本测不到东西**（挂上去就是假尺子），所以删掉了，
    #      这里只留一段话说清为什么删 —— 免得后人又捡回来当"没覆盖到"去补。

    bad_lead = src.replace("take = [0]", "take = []", 1)
    r_lead, _ = run_cli(tmp, bad_lead, vocal, inst, 3)
    s_lead = [p["semitone"] for p in r_lead.get("plan", [])]
    ok_lead, _b = all_consonant(s_lead)
    scheck("负对照：不把主唱放进协和池 ⇒ 会挑出相对主唱的小二度/大二度",
           bool(s_lead) and not ok_lead,
           str(s_lead) if not ok_lead else "摘掉主唱居然还全协和")

    bad_dh = src.replace("MAX_PREDICT_DHNR = 3.0", "MAX_PREDICT_DHNR = 99.0", 1)
    r_dh, _ = run_cli(tmp, bad_dh, vocal, inst, 4)
    worse = [p["dhnr"] for p in r_dh.get("plan", []) if abs(p["dhnr"]) > 3.0]
    scheck("负对照：放宽音质护栏 ⇒ 会放行 |ΔHNR| > 3dB 的档（护栏不是摆设）",
           bool(worse), str([p["semitone"] for p in r_dh.get("plan", [])]))

    bad_off = src.replace("n = 1 + len(y) // hop", "n = len(y) // hop", 1)
    r_off, _ = run_cli(tmp, bad_off, vocal, inst, 2)
    scheck("负对照：voiced_mask 帧数 off-by-one ⇒ 直接跑不出结果（IndexError）",
           not r_off.get("plan"), str(r_off.get("reason", ""))[:70])

    print("（自检账不计入正式判据）")
    check("★ 所有负对照都真的改变了行为（有一条没变 = 那条是假尺子）",
          _sFail[0] == 0, "没通过的：%d 条" % _sFail[0])

    print("\n" + "=" * 60)
    print("和声声部门禁：%d 通过 / %d 失败" % (N_PASS, N_FAIL))
    if N_FAIL:
        print("★ 有判红项")
    print("=" * 60)
    return 1 if N_FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
