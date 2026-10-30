r"""第十关：批量导入（多个视频合成一个模型）与「给角色追加素材」。

要验的是四条，每条都对应一句用户能听懂的话：
  1. 一次选两个视频 → 可以合成**一个**角色一个模型（不是两个模型）
  2. 也可以选择"每个视频一个角色"（mode=per_video）—— 两种口径不能混
  3. 之后还能往这个角色里加新素材（append_to），原来的片段和人工改过的文本都还在
  4. 同一个素材加两遍不会变成两倍（去重）；追加到不存在的角色要当场报错

用法：<venv>/Scripts/python.exe verify_ingest_batch.py
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import studio                                    # noqa: E402

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  PASS  " if cond else "! FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""))


BT = os.path.join(HERE, "data", "_bt")
SRC_A = os.path.join(BT, "srcA.wav")
SRC_B = os.path.join(BT, "srcB.wav")
SRC_C = os.path.join(BT, "srcC.wav")


def make_sources():
    """造三段 40 秒的测试源（从同一个双人视频里切不同位置）。

    不从验收素材里挑短的：那两个 5 秒的切不出 5 段合格片段（step_slice 直接报错），
    而 316 秒的原片跑一遍要十几分钟 —— 40 秒既能切出段，又跑得动。
    """
    os.makedirs(BT, exist_ok=True)
    jobs = [(SRC_A, "0"), (SRC_B, "60"), (SRC_C, "120")]
    src = os.path.join(studio.UPLOADS, "two_speakers.mp4")
    for dst, ss in jobs:
        if os.path.isfile(dst):
            continue
        cmd = [studio.FFMPEG, "-y", "-v", "error", "-ss", ss,
               "-i", src, "-t", "40", "-vn", "-acodec", "pcm_s16le",
               "-ar", "44100", "-ac", "1", dst]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("造测试源失败：%s" % r.stderr[-200:])
    return [p for p, _ in jobs]


def ingest(videos, mode="auto", append_to=None):
    """跑一次导入（不训练，省掉几分钟的 GPU 时间）。返回 (job, 角色id列表)。"""
    job = studio.new_job(None, "ingest", "门禁·%s" % mode)
    studio.job_ingest(job, None, videos=videos, mode=mode, append_to=append_to,
                      uvr=False, do_train=False)
    return job


def nseg(rid):
    r = studio.get_role(rid)
    return len((r or {}).get("segments") or [])


def main():
    print("=== 0. 准备素材 ===")
    make_sources()
    for p in (SRC_A, SRC_B, SRC_C):
        check("测试源可用（40 秒）：%s" % os.path.basename(p),
              os.path.isfile(p) and studio.wav_seconds(p) > 30,
              "%.1f 秒" % studio.wav_seconds(p))

    made = []
    try:
        print("\n=== 1. 两个视频 + per_video：各自一个角色 ===")
        j1 = ingest([SRC_A, SRC_B], mode="per_video")
        r1 = j1.get("result_roles") or []
        check("两个视频 → 建出 2 个角色", len(r1) == 2, r1)
        n1 = [nseg(x) for x in r1]
        made += r1
        check("每个角色都有片段", all(n > 0 for n in n1), n1)
        per_a, per_b = (n1[0], n1[1]) if len(n1) == 2 else (0, 0)

        print("\n=== 2. 同样的两个视频 + merge：合成**一个**角色 ===")
        j2 = ingest([SRC_A, SRC_B], mode="merge")
        r2 = j2.get("result_roles") or []
        check("合并模式只建 1 个角色（这正是「多个视频一个模型」）",
              len(r2) == 1, r2)
        made += r2
        rid = r2[0] if r2 else None
        n_merge = nseg(rid) if rid else 0
        check("这个角色的片段数 = 两个视频各自片段数之和（一份都没丢）",
              n_merge == per_a + per_b, "%d == %d + %d" % (n_merge, per_a, per_b))
        r = studio.get_role(rid) if rid else {}
        check("角色来源写清了是哪些视频",
              "srcA" in (r.get("source") or "") and "srcB" in (r.get("source") or ""),
              r.get("source"))
        # 切片名带 v0_/v1_ 前缀：两个文件里同名的片段不会互相覆盖
        segs = r.get("segments") or []
        check("两个视频的片段都在（名字带 v0_ / v1_ 前缀，不会重名覆盖）",
              any(s["file"].startswith("v0_") for s in segs)
              and any(s["file"].startswith("v1_") for s in segs),
              [s["file"] for s in segs[:3]])

        print("\n=== 3. 追加素材：给已有角色加第三段 ===")
        # 先手工改一句文本 —— 追加时它必须还在（人工校对过的不能被 ASR 冲掉）
        segs = (studio.get_role(rid) or {}).get("segments") or []
        if not segs:
            check("角色里有片段，才能验「文本不被覆盖」", False, "segments 为空")
            return
        keep_file = segs[0]["file"]
        studio.mutate_role(rid, lambda r: r["segments"][0].__setitem__(
            "text", "门禁手工校对过的文本"))
        before = nseg(rid)
        j3 = ingest([SRC_C], mode="merge", append_to=rid)
        check("追加没有新建角色（还是原来那一个）",
              (j3.get("result_roles") or []) == [rid], j3.get("result_roles"))
        after = nseg(rid)
        check("片段数增加了（新素材真的加进来了）", after > before,
              "%d -> %d" % (before, after))
        segs2 = (studio.get_role(rid) or {}).get("segments") or []
        hit = [s for s in segs2 if s["file"] == keep_file]
        check("原来那段人工校对过的文本没被覆盖",
              bool(hit) and hit[0].get("text") == "门禁手工校对过的文本",
              (hit or [{}])[0].get("text"))
        check("新加进来的片段名字带 v0_ 前缀（来自这一个新源）",
              any(s["file"].startswith("v0_") and s not in segs for s in segs2),
              "")

        print("\n=== 4. 负对照：同一份素材追加两遍不会翻倍 ===")
        n_before = nseg(rid)
        ingest([SRC_C], mode="merge", append_to=rid)
        check("再追加一次，片段数不变（同名片段不重复计数）",
              nseg(rid) == n_before, "%d -> %d" % (n_before, nseg(rid)))

        print("\n=== 5. 负对照：追加到不存在的角色必须当场报错 ===")
        try:
            ingest([SRC_C], mode="merge", append_to="r_not_exist")
            check("追加到不存在的角色 -> 报错", False, "居然没报错")
        except Exception as e:
            check("追加到不存在的角色 -> 报错（而不是造出一个野角色）",
                  "不存在" in str(e), str(e)[:80])

        print("\n=== 6. 负对照：没有素材时不能启动 ===")
        try:
            studio.job_ingest(studio.new_job(None, "ingest", "门禁·空"),
                              None, videos=[], uvr=False, do_train=False)
            check("空列表 -> 报错", False, "居然没报错")
        except Exception as e:
            check("空列表 -> 报错", "没有要处理的视频" in str(e), str(e)[:60])
        try:
            studio.job_ingest(studio.new_job(None, "ingest", "门禁·缺文件"),
                              None, videos=[os.path.join(BT, "nope.wav")],
                              uvr=False, do_train=False)
            check("文件不存在 -> 报错", False, "居然没报错")
        except Exception as e:
            check("文件不存在 -> 报错", "文件不存在" in str(e), str(e)[:60])

    finally:
        # 收尾：把门禁造的角色全删掉，别留在角色墙里误导人
        for rid in made:
            try:
                studio.delete_role(rid)
            except Exception:
                pass
        shutil.rmtree(BT, ignore_errors=True)

    print("\n" + "=" * 60)
    print("PASS %d  FAIL %d" % (len(OK), len(BAD)))
    for b in BAD:
        print("  红：" + b)
    print("EXIT=%d" % (1 if BAD else 0))
    sys.exit(1 if BAD else 0)


if __name__ == "__main__":
    main()
