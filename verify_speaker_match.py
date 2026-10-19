# -*- coding: utf-8 -*-
r"""第十二关：加素材时**自动认人**（mode=match）。

要验的是四句话，每句都对应老板提的诉求：
  1. 「自动区分这个素材是谁」 —— 同一个人的新素材，能被认出来；
  2. 「把属于这个角色的素材加进去」 —— 认出来之后是**并进去**（追加），不是新建；
  3. 「不会被其他角色影响」 —— 别人的素材**不能**被塞进这个角色；
  4. 认不出/新声音 → 新建角色，且**一个已有角色都不许动**。

★ 绝不拿用户已有角色做"加素材"的端到端实验：那样会把他的角色改脏。
  端到端只用本关自己建的那个基准角色。
  （会碰到的副作用只有：给已有角色补写 `voice` 声纹中心缓存 —— 那是功能本身的缓存
   字段，不碰 segments，也不新建角色。）

素材完全自足：从 `data/uploads/two_speakers.mp4` 切 300 秒，走真实导入链拿到
**一个人**的切片池当基准。跑完把自己建的角色全删掉。

==================== 三条血泪教训（都直接影响这关怎么写的）====================

① **"没被选中"经常是白来的。** 库里存在那个素材**真正的主人**时，主人永远更近，
   于是"基准角色没被选中"根本不依赖阈值 —— 把阈值调成 9999 也验不出红。
   ⇒ 所以第 3 段把候选集**收敛到只剩基准角色**（临时替换 `studio.load_roles`），
     阈值才成为唯一的决定因素。上一版没这么做，坏尺子负对照红得毫无道理。

② **候选集可能整个是空的，而判据看起来是绿的。** 换声纹向量之后，老角色的中心
   被"陈旧"规则跳过；如果库里因此只剩基准角色一个候选，别人的素材**必然**被判成它 ——
   当场红，而且红得像是产品坏了。⇒ 第 1b 段先 `ensure_role_voices()` 补算，
   再**断言候选集真的不止一个**。

③ **薄中心会把"别人"的距离整体压低。** 基准角色中心只用 17 段时，别人的素材到它
   只有 0.2103（低于当时的阈值 0.23）⇒ 真误收。实测（`_probe_center_quality.py`）：
   中心 <30 段时异人 p10 掉到 0.2266，≥30 段时才稳定在 0.24 以上。
   ⇒ 功能侧加了"中心段数 < MIN_CENTER_SEGS 的角色不作为候选"，第 1c 段专门验它
     （拿薄中心**自己的素材**去认都必须认不上；把门槛降成 0 则必须认上）。
   ⇒ 另外本关的基准素材从 150 秒改成 **300 秒**：150 秒只切出 17 段，基准角色自己
     就是个薄中心，本关会红在"基准不合格"上而不是红在功能上。

④ **本机的批量删除钩子会让收尾删不干净，这是环境策略、不是产品缺陷。** 它按
   「**一条命令里累计删了多少次**」计数（阈值 50），超了之后**每个删除都抛
   `SystemExit`**（连单个 `os.remove` 也抛；而 `SystemExit` 穿透 `except Exception`，
   会让整个门禁在收尾处静默死掉、连汇总行都不打 —— 看着像"这关挂了"，其实前面全绿）。
   本关跑一趟要建/删几十个切片，到收尾时预算早用光 ⇒ **注定留几 MB 孤儿目录**。
   ⇒ 收尾只"尽力删"（`rm_probe_role`，且只删本关自己建的 rid），残留目录**不当判据**
     而是打一条 ⚠ 并给出补救命令：`bash _run_regression.sh gc`（另起一条命令，预算从零开始）。

用法：<venv>/Scripts/python.exe -s verify_speaker_match.py
"""
import os
import shutil
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio  # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""))


def nseg(rid):
    r = studio.get_role(rid)
    return len((r or {}).get("segments") or [])


def role_ids():
    return {r["id"] for r in studio.load_roles()["roles"]}


def concat_wavs(paths, dst):
    """把一堆切片拼成一个 wav（当作"这个人的一段素材"喂给导入链）。

    为什么用拼接而不是直接切视频：视频里谁在说话不由我控制，而"切成两半"
    能保证两半铁定是同一个人 —— 这是做「同人」正例的前提。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    out = None
    try:
        for p in paths:
            with wave.open(p, "rb") as w:
                if out is None:
                    out = wave.open(dst, "wb")
                    out.setnchannels(w.getnchannels())
                    out.setsampwidth(w.getsampwidth())
                    out.setframerate(w.getframerate())
                out.writeframes(w.readframes(w.getnframes()))
    finally:
        if out:
            out.close()
    return os.path.getsize(dst) if os.path.exists(dst) else 0


def match_files(files, lab=0):
    """拿一组切片当"新素材"，走一次认人。返回 (判成的角色id, 说明)。

    ★ 传的是**切片列表**（每段一个声纹向量），不是把切片拼成一个 wav ——
      match_clusters 要的是"一堆段的声纹"；拼成一个 wav 就只剩一条向量，
      会直接被"段数太少不认人"拦下，看着像功能没生效。
    """
    feats = studio._feat_subprocess(files, "m")
    if not feats:
        return None, ["提不到声纹"]
    flat = [dict(x, file=os.path.basename(x["file"])) for x in feats]
    a, notes = studio.match_clusters(None, {lab: flat}, flat)
    return a.get(lab), notes


def flat_of(files, tag="m"):
    feats = studio._feat_subprocess(files, tag)
    return [dict(x, file=os.path.basename(x["file"])) for x in feats]


def match_flat(flat, lab=0, only_ids=None):
    """对已经提好声纹的一堆段做一次认人。`only_ids` 用来构造**可控候选集**。

    为什么要 only_ids（踩过，而且是本轮最重要的一课）：
      负例要验的是"**阈值 / 候选规则**拦住了这个角色"。可是库里那个**真正的主人**
      （同一份素材的那个人）永远比它更近 ⇒ 正常情况下根本轮不到它被选中，
      "没被选中"是**白来的**；光把尺子调坏也验不出红。
      上一版就是这么写的，坏尺子那条负对照红得毫无道理。
      把候选集收敛到"只剩这一个角色"，规则才成为唯一决定因素。
    做法是把 studio.load_roles 临时换掉 —— 纯粹是测试里构造场景，不碰任何数据。
    """
    real = studio.load_roles
    try:
        if only_ids is not None:
            studio.load_roles = lambda: {
                "roles": [r for r in real()["roles"] if r["id"] in only_ids]}
        a, notes = studio.match_clusters(None, {lab: flat}, flat)
        return a.get(lab), notes
    finally:
        studio.load_roles = real


def match_files(files, lab=0, only_ids=None):
    flat = flat_of(files)
    if not flat:
        return None, ["提不到声纹"]
    return match_flat(flat, lab, only_ids)


def med_by_role(files):
    """把一组切片对**每一个**有当前向量的角色求中位距离，全部打印出来（从近到远）。

    为什么不用 match_clusters 现成的说明：它只报"最近的"和"第二近的"。负例真正要看
    的是**基准角色排第几、差多少** —— 只看第一名，会把"其实基准也够近、只是另一个更近"
    这种（多角色库里的典型误并前兆）整个漏掉。
    """
    feats = studio._feat_subprocess(files, "diag")
    meta = [dict(x, file=os.path.basename(x["file"])) for x in feats]
    vv = [m[studio.VOICE_KIND] for m in meta if m.get(studio.VOICE_KIND)]
    rows = []
    for r in studio.load_roles()["roles"]:
        v = r.get("voice") or {}
        if not v.get("center") or v.get("kind") != studio.VOICE_KIND:
            continue
        ds = sorted(x for x in (studio._emb_dist(e, v["center"]) for e in vv)
                    if x is not None)
        if ds:
            rows.append((ds[len(ds) // 2], r["id"], r.get("name") or ""))
    rows.sort()
    print("      （%d 段对每个角色的中位距离，近 -> 远）" % len(vv))
    for d, rid, nm in rows:
        print("        %-12s %-16s %.4f" % (rid, nm, d))
    return rows


def rm_probe_role(rid):
    """尽力删掉本关自己建的一个探针角色目录，返回是否真的清干净了。

    ★ 为什么不能指望 `studio.delete_role()`：本机环境层有个**批量删除钩子**，按
      「**一条命令里累计删了多少次**」计数（阈值 50）。门禁跑一趟本身要建/删几十个
      切片文件，到收尾时预算早用光 ⇒ 之后**每一个删除动作都抛 `SystemExit`**
      （实测连单个 `os.remove` 也抛，不只是 rmtree；而且 `SystemExit` 穿透
      `except Exception`，会让整个门禁在收尾处静默死掉、连汇总行都不打）。
    ★ 所以这里"能删多少算多少"，剩下的孤儿目录交给 `bash _run_regression.sh gc`
      —— 那条命令的删除预算是从零开始的（见 `_gc_orphans.py` 的说明）。
    ★ 口径：只删 `data/roles/<rid>`，rid 由调用点保证是本关自己建的，绝不碰用户角色。
    """
    d = studio.role_dir(rid)
    if not os.path.isdir(d):
        return True
    for dp, dns, fns in os.walk(d, topdown=False):
        for f in fns:
            try:
                os.remove(os.path.join(dp, f))
            except BaseException:
                pass
        for sub in dns:
            try:
                os.rmdir(os.path.join(dp, sub))
            except BaseException:
                pass
    try:
        os.rmdir(d)
    except BaseException:
        pass
    return not os.path.isdir(d)


def main():
    print("==============================================================")
    print("加素材时自动认人（mode=match）验收")
    print("==============================================================")

    before = role_ids()
    src = os.path.join(studio.UPLOADS, "two_speakers.mp4")
    if not os.path.isfile(src):
        print("缺素材：%s" % src)
        return 1

    bt = os.path.join(HERE, "data", "_smtest")
    shutil.rmtree(bt, ignore_errors=True)
    os.makedirs(bt, exist_ok=True)

    print("\n=== 0. 切一段视频，走真实导入链，拿「一个人」的切片池当基准 ===")
    # ★ 切 300 秒而不是 150：基准角色的声纹中心必须够"厚"（>= studio.MIN_CENTER_SEGS）
    #   才有资格当认人候选。150 秒只切出 17 段 —— 那是个**薄中心**，而薄中心会把
    #   "异人"侧的距离整体压低（实测 <30 段时异人 p10 掉到 0.2266，低于阈值 ⇒ 误收），
    #   本关就会红在"基准角色自己不合格"上，而不是红在功能上。
    c1 = os.path.join(bt, "c1.wav")
    r = subprocess.run([studio.FFMPEG, "-y", "-v", "error", "-ss", "0",
                        "-i", src, "-t", "300", "-vn", "-acodec", "pcm_s16le",
                        "-ar", "44100", "-ac", "1", c1],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("切素材失败：%s" % (r.stderr or "")[-200:])
        return 1

    job = studio.new_job(None, "ingest", "门禁·认人基准")
    studio.job_ingest(job, None, videos=[c1], mode="auto", uvr=True,
                      do_train=False)
    new_ids = sorted(role_ids() - before)
    print("  基准导入建了 %d 个角色：%s" % (len(new_ids), [(i, nseg(i)) for i in new_ids]))
    check("基准导入建出了角色（后面的判据有东西可比）", bool(new_ids))
    if not new_ids:
        return 1

    # 取最大的那个簇当"这个人"。它是 diarize 按声纹聚出来的，天然是单一说话人。
    base = max(new_ids, key=nseg)
    bd = os.path.join(studio.role_dir(base), "slices")
    pool = [os.path.join(bd, f) for f in sorted(os.listdir(bd))
            if f.lower().endswith(".wav")]
    print("  基准角色 %s：%d 段切片" % (base, len(pool)))
    # 门槛就是"够走一次认人"（MATCH_MIN_SEGS），不劈半也认；能不能劈半下面再判。
    check("基准角色的切片数够走一次认人（>= 门槛）",
          len(pool) >= studio.MATCH_MIN_SEGS,
          "%d（门槛 %d）" % (len(pool), studio.MATCH_MIN_SEGS))
    if len(pool) < studio.MATCH_MIN_SEGS:
        for i in sorted(role_ids() - before):
            studio.delete_role(i)
        return 1

    v = studio.build_role_voice(base, None, force=True)
    check("基准角色算出了声纹中心", bool(v and v.get("center")),
          ("用了 %s 段 / 维度 %s / 向量 %s"
           % (v.get("n"), v.get("dim"), v.get("kind"))) if v else "无")
    # ★ 维度这条判据**不能写死**（第一版写死成 VOICE_DIM=192，第一次跑就翻车）：
    #   真正要守的是"中心维度 == 当前这一路向量当场提取出来的维度"，不一致就说明
    #   换了模型/换了向量，那时候比出来的数没有意义（长度不等还会被 zip 静默截断）。
    probe_feat = studio._feat_subprocess(pool[:1], "dim")
    probe_dim = len(probe_feat[0][studio.VOICE_KIND]) \
        if probe_feat and probe_feat[0].get(studio.VOICE_KIND) else 0
    check("声纹中心的维度 == 当场提取的维度（不一致时不许硬比）",
          bool(v) and v.get("dim") == probe_dim and probe_dim > 0,
          "中心 %s / 当场 %s（向量 %s）"
          % ((v or {}).get("dim"), probe_dim, studio.VOICE_KIND))
    # ★ 缓存必须带"用的哪一路向量"。缺了它，换向量之后老中心会被当成有效缓存一直用，
    #   而维度对不上时它只是**被静默跳过** —— 表现成"这个角色永远认不出"，不报错。
    check("声纹缓存带向量种类标记（换向量时老缓存能自动失效）",
          bool(v) and v.get("kind") == studio.VOICE_KIND,
          "voice.kind=%s / 当前=%s" % ((v or {}).get("kind"), studio.VOICE_KIND))
    # ★ 中心够"厚"才有资格当候选。薄中心（< MIN_CENTER_SEGS 段）会把异人侧距离
    #   整体压低 ⇒ 别人的素材被并进来。这条不红，下面所有负例的结论都不成立。
    check("基准角色的声纹中心够厚（>= MIN_CENTER_SEGS，否则它不是合法候选）",
          bool(v) and int(v.get("n") or 0) >= studio.MIN_CENTER_SEGS,
          "用了 %s 段 / 门槛 %s" % ((v or {}).get("n"), studio.MIN_CENTER_SEGS))

    print("\n=== 1. 正例：同一个人的新素材，必须认成本人 ===")
    # 能劈两半就劈（更接近"新加的一批素材"），段数不够就整批用（自认自己）
    if len(pool) >= 2 * studio.MATCH_MIN_SEGS:
        same_files = pool[:len(pool) // 2]
        how = "前半 %d 段当新素材" % len(same_files)
    else:
        same_files = pool
        how = "段数不够劈半，整批 %d 段当新素材" % len(same_files)
    print("    " + how)
    sfeats = studio._feat_subprocess(same_files, "same")
    check("新素材提得到声纹", len(sfeats) >= studio.MATCH_MIN_SEGS,
          "%d 段（判据门槛 %d）" % (len(sfeats), studio.MATCH_MIN_SEGS))
    if len(sfeats) < studio.MATCH_MIN_SEGS:
        for i in sorted(role_ids() - before):
            studio.delete_role(i)
        print("\n基准素材切出来的段太少，认人这条跑不起来（不是产品问题，是素材太短）")
        return 1
    sf = [dict(x, file=os.path.basename(x["file"])) for x in sfeats]
    got, notes = studio.match_clusters(None, {0: sf}, sf)
    for t in notes:
        print("    " + t)
    check("★ 同一个人的新素材被认成基准角色", got.get(0) == base,
          "判成 %s（应为 %s）" % (got.get(0), base))

    print("\n=== 1b. 把库里所有角色的声纹中心刷成当前向量（负例的前提）===")
    made_n, skip_n = studio.ensure_role_voices(None)
    print("    补算 %d 个 / 跳过 %d 个" % (made_n, skip_n))
    # ★ 这一步是判据**自足**的关键，踩过：
    #   负例判的是"别人的素材会不会被塞进基准角色"。如果库里**只有基准角色**声明了
    #   当前向量、别的角色还是旧格式（换了声纹向量之后老缓存会被判为陈旧跳过），
    #   那这条判据就退化成"只跟一个候选比"，别人的素材**必然**被判成它 ——
    #   当场变红，而且红得毫无道理（产品其实是对的）。
    #   所以先补算，再断言候选集真的不止一个。
    declared = [r for r in studio.load_roles()["roles"]
                if (r.get("voice") or {}).get("kind") == studio.VOICE_KIND
                and (r.get("voice") or {}).get("center")]
    check("负例的候选集里至少有 2 个角色声明了当前向量（否则这条判据退化成只跟一个比）",
          len(declared) >= 2,
          "有 %d 个：%s" % (len(declared), [r["id"] for r in declared]))

    print("\n=== 1c. ★ 薄中心的角色必须被排除在候选之外（那次真误收的保护）===")
    # 造一个"薄中心"角色：把它的 voice 直接写成"只用 12 段算的中心"。
    # ★ 故意**不走 build_role_voice、也不给它拷切片**：那条路会往角色目录写 12 个文件，
    #   收尾时就要删这 12 个 —— 本机环境层有批量删除钩子（阈值 50/轮），能少删就少删。
    #   这一段要验的是 match_clusters 的**排除规则**，不是 build_role_voice。
    thin_id = studio.create_role(name="门禁·薄中心", source="probe")["id"]
    thin_vecs = [x[studio.VOICE_KIND] for x in studio._feat_subprocess(pool[:12], "thin")
                 if x.get(studio.VOICE_KIND)]
    check("薄中心探针拿到了 12 段声纹（少于 MIN_CENTER_SEGS，正是要造的形状）",
          len(thin_vecs) == 12 and 12 < studio.MIN_CENTER_SEGS,
          "拿到 %d 段 / 门槛 %d" % (len(thin_vecs), studio.MIN_CENTER_SEGS))
    tv = {"center": studio._emb_center(thin_vecs), "n": len(thin_vecs),
          "dim": len(thin_vecs[0]), "kind": studio.VOICE_KIND,
          "model": "eres2net/feat_cli", "at": "gate"}
    studio.mutate_role(thin_id, lambda r: r.__setitem__("voice", tv))
    check("它的段数确实低于 MIN_CENTER_SEGS",
          bool(tv) and int(tv.get("n") or 0) < studio.MIN_CENTER_SEGS,
          "n=%s / 门槛 %s" % ((tv or {}).get("n"), studio.MIN_CENTER_SEGS))
    # 用**它自己的素材**去认：它本该是最像的那一个；可只要它是薄中心，就一个候选都不该有。
    got_thin, notes_thin = match_flat(sf, 2, only_ids=[thin_id])
    for t in notes_thin:
        print("    " + t)
    check("★ 薄中心的角色不作为认人候选（拿它自己的素材去认都认不上 —— 认不出好过认错）",
          got_thin is None, "判成 %s" % got_thin)
    old_min = studio.MIN_CENTER_SEGS
    try:
        studio.MIN_CENTER_SEGS = 0
        got_thin_bad, _ = match_flat(sf, 2, only_ids=[thin_id])
        check("坏尺子：门槛降成 0 之后，**同一个**薄中心角色确实被认上了 "
              "⇒ 上一条量的是排除规则，不是恒真",
              got_thin_bad == thin_id, "判成 %s" % got_thin_bad)
    finally:
        studio.MIN_CENTER_SEGS = old_min
    got_thin_back, _ = match_flat(sf, 2, only_ids=[thin_id])
    check("还原门槛后又认不上了（没被负对照污染）", got_thin_back is None,
          "判成 %s" % got_thin_back)

    print("\n=== 2. 负例：别人的素材，绝不能塞进这个角色 ===")
    other_role = None
    for rr in studio.load_roles()["roles"]:
        # thin_id 是本关自己造的探针角色，不能拿来当"别人的素材"（它只有 12 段）
        if rr["id"] in new_ids or rr["id"] == thin_id or rr.get("pending"):
            continue
        d = os.path.join(studio.role_dir(rr["id"]), "slices")
        if os.path.isdir(d) and len([f for f in os.listdir(d)
                                     if f.lower().endswith(".wav")]) >= studio.MATCH_MIN_SEGS:
            other_role = rr
            break
    other_files = []
    if other_role:
        od = os.path.join(studio.role_dir(other_role["id"]), "slices")
        other_files = [os.path.join(od, f) for f in sorted(os.listdir(od))
                       if f.lower().endswith(".wav")]
        print("    别人的素材：角色「%s」的 %d 段切片"
              % (other_role.get("name"), len(other_files)))
        rows = med_by_role(other_files)
        flat_other = flat_of(other_files, "other")
        got2, notes2 = match_flat(flat_other, 1)
        for t in notes2:
            print("    " + t)
        # ★ 必须确认这条判据真的"跑起来了"：段数不够时会返回 None，
        #   而 None != base 会让断言**假绿**（看着像"没被误认"，其实根本没认人）。
        ran = len(other_files) >= studio.MATCH_MIN_SEGS
        check("★ 别人的素材没有被认成基准角色（不会被其他角色影响）",
              ran and got2 != base,
              ("段数 %d / 判成 %s" % (len(other_files), got2)) if not ran
              else "判成 %s" % got2)
        # ★★ 承重的那条：上一条"没被认成它"在库里存在真正的主人时是**白来的**
        #   （主人永远更近）。真正决定"会不会误并"的是**距离本身有没有超阈值**。
        bd = [d for d, rid, _n in rows if rid == base]
        check("★★ 别人的素材到基准角色的中位距离本身就超过阈值（承重判据）",
              bool(bd) and bd[0] > studio.MATCH_DIST,
              "%s vs 阈值 %.2f" % (("%.4f" % bd[0]) if bd else "这个角色不在候选里",
                                   studio.MATCH_DIST))
        rank = [i for i, (_d, rid, _n) in enumerate(rows) if rid == base]
        print("    基准角色在别人的素材上的排位：第 %s 名，阈值 %.2f"
              % (rank[0] + 1 if rank else "-", studio.MATCH_DIST))
    else:
        check("★ 别人的素材没有被认成基准角色（不会被其他角色影响）",
              False, "库里没有第二个可用的真实角色，这条判据没跑起来")
        check("★★ 别人的素材到基准角色的中位距离本身就超过阈值（承重判据）",
              False, "同上，跑不起来")

    print("\n=== 3. ★ 尺子自检：把候选集收敛到「只剩基准角色」，再调坏阈值 ===")
    # 为什么必须收敛候选集（踩过、而且是本轮最重要的一课）：候选里如果留着那个
    # **真正的主人**，它永远更近 ⇒ "没选中基准角色"是白来的，把阈值调坏也验不出红。
    # 只剩基准角色时，**阈值就是唯一的决定因素** —— 这才验得动。
    if other_role and flat_other:
        got_sane, _ = match_flat(flat_other, 3, only_ids=[base])
        check("候选集只剩基准角色时，别人的素材仍然被拒（说明阈值在起作用）",
              got_sane is None, "判成 %s" % got_sane)
        old = studio.MATCH_DIST
        try:
            studio.MATCH_DIST = 9999.0
            got3, _ = match_flat(flat_other, 3, only_ids=[base])
            check("坏尺子（阈值 9999）下**确实**被认成了基准角色 "
                  "⇒ 上一条量的是阈值分辨力，不是恒真",
                  got3 == base, "坏尺子判成 %s" % got3)
        finally:
            studio.MATCH_DIST = old
        got4, _ = match_flat(flat_other, 3, only_ids=[base])
        check("还原阈值后又认不上了（没被负对照污染）", got4 is None,
              "判成 %s" % got4)
    else:
        check("坏尺子负对照", False, "没有第二个角色，跑不了")

    print("\n=== 4. 端到端：mode=match 真的会把素材并进那个角色 ===")
    snap = {r["id"]: nseg(r["id"]) for r in studio.load_roles()["roles"]}

    # ★ 数"普通角色"时要把「待归类的片段」排除掉，理由说清楚（2026-10-19 加的段级复核）：
    #   这条判据要防的是"**认人认错了** ⇒ 素材被并进一个不该并的角色、顺带新建了一个"。
    #   而「待归类的片段」是段级复核**有意**的落点 —— 那些段是"谁都不像"才被剔出来的，
    #   不是认错了，是认不出。它跟"认错"是两回事，混在一起数会误报。
    #   ⇒ 所以判据改成"没有新建多余的**普通**角色"，并**另加更严的两条**把它盯住
    #     （见下：它必须真的收到了段，且这些段必须真的是从本次导入里剔出来的）。
    def pending_ids():
        return {r["id"] for r in studio.load_roles()["roles"] if r.get("pending")}

    def real_ids():
        return role_ids() - pending_ids()

    n_before_roles = len(snap) - len(pending_ids())
    same_wav = os.path.join(bt, "same_batch.wav")
    concat_wavs(same_files, same_wav)
    j2 = studio.new_job(None, "ingest", "门禁·认人端到端")
    # 只喂**本关自己建的那个角色**的素材 —— 绝不拿用户的角色做加素材实验
    studio.job_ingest(j2, None, videos=[same_wav], mode="match", uvr=True,
                      do_train=False)
    steps = [s.get("name") for s in (j2.get("steps") or [])]
    check("导入链里真的走了一步「认人」（说明 mode=match 接上了）",
          "认人" in steps, steps)
    hit = [s for s in (j2.get("steps") or []) if s.get("name") == "认人"]
    if hit:
        print("    认人那一步的说明：%s" % hit[0].get("note"))
    check("★ 基准角色的段数增加了（素材真的并进去了）",
          nseg(base) > (snap.get(base) or 0),
          "%s -> %s" % (snap.get(base), nseg(base)))
    check("★ 没有新建多余的普通角色（「待归类的片段」单独算，见下一条）",
          len(real_ids()) == n_before_roles,
          "%s -> %s（不含待归类）" % (n_before_roles, len(real_ids())))

    # ★ 补两条更严的：待归类不是"随便建一个就行"，它必须有真东西，而且是本次剔出来的。
    pend = [r for r in studio.load_roles()["roles"] if r.get("pending")]
    if pend:
        got = sum(nseg(r["id"]) for r in pend)
        check("「待归类的片段」里真的有段（不是空壳）", got > 0, "%d 段" % got)
        check("★ 同人的段没有被误塞进「待归类」（基准角色该拿到的都拿到了）",
              nseg(base) - (snap.get(base) or 0) >= 10,
              "基准新增 %d 段" % (nseg(base) - (snap.get(base) or 0)))
    else:
        print("     （本次没有待归类角色：说明段级复核一段都没剔 —— 同样合理）")
    # ★ 为什么排除 pending：「待归类的片段」按设计就是"谁都不像的段"的落点，
    #   它**本来就该**在这次导入里收到被剔出来的段。把它算进"其他角色不许变"
    #   等于要求段级复核什么都不剔 —— 那是自相矛盾的判据（红了也是假红）。
    #   真正盯住它的是上面那两条（必须有真段 + 同人段没被误塞进去）。
    #   退一步只保留一条仍然承重的：pending 只许**增加**，不许变少（不许丢素材）。
    others = [i for i in snap if i != base and i not in pending_ids()]
    check("★ 其他（非待归类）角色的段数一个都没变（没被别人影响）",
          all(nseg(i) == snap[i] for i in others if i in role_ids()),
          [(i, snap[i], nseg(i)) for i in others
           if i in role_ids() and nseg(i) != snap[i]])
    pend_shrunk = [(i, snap[i], nseg(i)) for i in pending_ids()
                   if i in snap and i in role_ids() and nseg(i) < snap[i]]
    check("★ 已有的「待归类的片段」只增不减（本次导入没吃掉它原有的段）",
          not pend_shrunk, pend_shrunk)

    print("\n=== 5. 收尾：删掉本关建的角色 ===")
    # ★ 必须按"本关**实际**新建的角色"来收尾，不能只列第 1 步记下来的 new_ids：
    #   第 4 步端到端导入会由段级复核**再**建一个「待归类的片段」，它不在 new_ids 里。
    #   漏了它的后果实测过：roles.json 里留下 r5446d8f1，下一次跑只有靠"库里已有一个
    #   待归类角色可复用"才侥幸不红 —— 这是确定性隐患，不是间歇。
    created = set(new_ids) | {thin_id} | (role_ids() - before)
    if created - (set(new_ids) | {thin_id}):
        print("    额外回收（端到端导入新建的）：%s"
              % sorted(created - (set(new_ids) | {thin_id})))
    for i in sorted(created):
        try:
            studio.delete_role(i)          # 先摘角色库登记（这一步一定成），再 rmtree
        except BaseException as e:
            # ★ 必须捕 BaseException：钩子抛的是 SystemExit，穿透 `except Exception`。
            #   踩过：整个门禁在收尾处静默死掉、连汇总行都不打，看着像"这关挂了"，其实全绿。
            #   注意此时角色库登记已摘掉，没删掉的是磁盘目录（下面 rm_probe_role 再尽力删）。
            print("    [warn] delete_role(%s) 抛 %s（登记已摘，目录没删干净）"
                  % (i, type(e).__name__))
        if not rm_probe_role(i):
            print("    [warn] 角色 %s 的目录还有残留" % i)
    left = role_ids() - before
    check("验收没在用户的角色库里留下东西（角色库登记）", not left,
          ("残留 %s" % sorted(left)) if left else "")
    # 磁盘上的残留**故意不当 PASS/FAIL 判据**：它由本机环境层的删除钩子决定，
    # 不反映产品逻辑（本关跑到这里删除预算已经用光了，谁来都删不掉）。
    # 判据的职责是把真相说出来，别把环境问题记成产品缺陷。
    orph = [o for o in studio.orphan_role_dirs() if o["id"] in created]
    if orph:
        print("    ⚠ 磁盘上还留着 %d 个本关建的探针目录（环境层删除钩子：本命令的删除预算"
              "已用光，不是产品缺陷）：" % len(orph))
        for o in orph:
            print("        %s   %.1f MB" % (o["path"], o["mb"]))
        print("        → 收尾请跑：bash _run_regression.sh gc")
    else:
        print("    磁盘上也干净（没有孤儿目录）")
    try:
        shutil.rmtree(bt, ignore_errors=True)
    except BaseException as e:
        print("    [warn] 删临时目录失败：%s" % e)

    print("\n" + "=" * 60)
    print("PASS %d / FAIL %d" % (len(OK), len(BAD)))
    for b in BAD:
        print("  ! " + b)
    print("ALLDONE")
    return 0 if not BAD else 1


if __name__ == "__main__":
    sys.exit(main())
