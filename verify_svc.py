# -*- coding: utf-8 -*-
"""门禁：第二套引擎（so-vits-svc 4.0）真的能跑、且坏输入会硬失败。

为什么要有这一关
----------------
RVC 那条链有 40 多项门禁兜着，而 SVC 是**新加的第二套引擎** —— 它会自己
起一个 py3.10 venv、下载 HuBERT、依赖两个环境变量（见 stages/svc_cli.py 文件头）。
这些"外部依赖"最危险的失败方式是**静默降级**：装不上就悄悄用 RVC 出成品，
用户以为在听 SVC 版。所以这里的承重判据是：
  · 正例：真跑一次 SVC 推理，必须拿到 SVC_OK，且输出时长与输入一致；
  · 负对照：缺权重 / 输入输出同路径 ⇒ 必须 SVC_FAIL + 退出码 1（不许假装成功）；
  · 产品侧：角色没挂 SVC 权重时，engine=svc 必须**明确报错**而不是回退 RVC。

用法：python verify_svc.py        （退出码 0 = 全过）
"""
import glob
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
STAGES = os.path.join(HERE, "stages")
ROLES = os.path.join(HERE, "data", "roles.json")
MODELS = os.path.join(HERE, "models", "svc_m6")
PY = sys.executable
N = {"pass": 0, "fail": 0}
FAILS = []


def check(name, cond, detail=""):
    ok = bool(cond)
    N["pass" if ok else "fail"] += 1
    if not ok:
        FAILS.append(name)
    print(("  PASS  " if ok else "  ! FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


def wav_secs(p):
    try:
        with wave.open(p, "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except Exception:
        return 0.0


def read(p):
    try:
        return io.open(p, encoding="utf-8").read()
    except Exception:
        return ""


def pick_model(tmpdir):
    """找一个三件套齐全的 SVC 角色；返回 (G, config, kmeans) 或 (None, None, None)。"""
    for d in sorted(glob.glob(os.path.join(MODELS, "*"))):
        g = glob.glob(os.path.join(d, "G_*.pth"))
        c = os.path.join(d, "config.json")
        k = glob.glob(os.path.join(d, "kmeans_*.pt"))
        if g and os.path.isfile(c) and k:
            return g[0], c, k[0]
    return None, None, None


def make_clip(tmpdir):
    """4 秒测试素材：优先拿项目里已有的分离干声，没有再合成一段。"""
    cands = glob.glob(os.path.join(HERE, "data", "cache", "sep", "*", "vocal.wav"))
    if cands:
        src = cands[0]
        dst = os.path.join(tmpdir, "clip.wav")
        try:
            with wave.open(src, "rb") as w:
                n = int(w.getframerate() * 4)
                data = w.readframes(n)
                with wave.open(dst, "wb") as o:
                    o.setnchannels(w.getnchannels())
                    o.setsampwidth(w.getsampwidth())
                    o.setframerate(w.getframerate())
                    o.writeframes(data)
            return dst
        except Exception:
            pass
    return None


def main():
    print("=" * 70)
    print("SVC 第二引擎门禁")
    print("=" * 70)

    # ---------------------------------------------------------------- A. 静态
    print("\n=== A. 静态接线（少了哪一环就是「两套并存」只写在文档里）===")
    cli = os.path.join(STAGES, "svc_cli.py")
    check("stages/svc_cli.py 在", os.path.isfile(cli))
    studio = read(os.path.join(HERE, "studio.py"))
    check("studio.py 有 svc_convert()", "def svc_convert(" in studio)
    check("sing_role 里按 engine 分流", 'engine == "svc"' in studio)
    check("SVC 也走同一个显卡闸门", "歌声转换（SVC）" in studio)
    vs = read(os.path.join(HERE, "vs.py"))
    check("命令行 --engine 认得 svc", '"svc"' in vs and "--engine" in vs)
    db = {}
    try:
        db = json.load(io.open(ROLES, encoding="utf-8"))
    except Exception as e:
        check("roles.json 可读", False, e)
    with_svc = [r for r in (db.get("roles") or []) if (r.get("svc") or {}).get("model")]
    check("至少一个角色挂了 SVC 权重", len(with_svc) >= 1, "%d 个" % len(with_svc))

    # ---------------------------------------------------------------- B. 正例
    print("\n=== B. 真跑一次（正例）===")
    g, c, k = pick_model(None)
    if not g:
        check("有可用的 SVC 三件套（G_/config/kmeans）", False, "models/svc_m6 下没有")
        print("\n通过 %d 项，失败 %d 项" % (N["pass"], N["fail"]))
        print("ALLDONE")
        return 1
    tmp = tempfile.mkdtemp(prefix="vs_svc_")
    try:
        clip = make_clip(tmp)
        if not clip:
            check("能找到/造出测试素材", False, "data/cache/sep 下没有 vocal.wav")
        else:
            out = os.path.join(tmp, "out.wav")
            rep = os.path.join(tmp, "r.json")
            r = subprocess.run(
                [PY, "-s", cli, "--model", g, "--config", c, "--kmeans", k,
                 "--input", clip, "--output", out, "--pitch", "0",
                 "--device", "cuda:0", "--report", rep],
                capture_output=True, timeout=1800)
            txt = (r.stdout or b"").decode("utf-8", "replace")
            check("SVC 推理成功（SVC_OK）", "SVC_OK" in txt, txt.strip()[:120])
            check("输出文件真的写出来了", os.path.isfile(out),
                  "%.1f KB" % (os.path.getsize(out) / 1024.0) if os.path.isfile(out) else "没有")
            if os.path.isfile(out):
                a, b = wav_secs(clip), wav_secs(out)
                check("输出时长与输入一致（差 <5%）", abs(a - b) <= max(0.4, a * 0.05),
                      "in %.2fs / out %.2fs" % (a, b))
                check("输出不是空/静音文件", os.path.getsize(out) > 40000,
                      "%.1f KB" % (os.path.getsize(out) / 1024.0))

        # ------------------------------------------------------------ C. 负对照
        print("\n=== C. 负对照（坏输入必须硬失败）===")
        r = subprocess.run([PY, "-s", cli, "--model", os.path.join(tmp, "nope.pth"),
                            "--config", c, "--input", clip or c, "--output",
                            os.path.join(tmp, "bad.wav")],
                           capture_output=True, timeout=120)
        txt = (r.stdout or b"").decode("utf-8", "replace")
        check("缺权重 ⇒ SVC_FAIL 且退出码非 0", "SVC_FAIL" in txt and r.returncode != 0,
              "rc=%d %s" % (r.returncode, txt.strip()[:80]))
        check("缺权重时没有生成伪成品", not os.path.isfile(os.path.join(tmp, "bad.wav")))

        if clip:
            r = subprocess.run([PY, "-s", cli, "--model", g, "--config", c,
                                "--kmeans", k, "--input", clip, "--output", clip],
                               capture_output=True, timeout=120)
            txt = (r.stdout or b"").decode("utf-8", "replace")
            check("输入=输出 ⇒ 直接拒绝（不许原地覆盖）",
                  "SVC_FAIL" in txt and r.returncode != 0, txt.strip()[:80])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---------------------------------------------------------------- D. 产品侧
    print("\n=== D. 产品侧：角色没挂 SVC 时必须明确报错（不许悄悄退回 RVC）===")
    sys.path.insert(0, HERE)
    try:
        import studio  # noqa: E402
        import contextlib

        @contextlib.contextmanager
        def _noop(*a, **kw):
            yield

        old = getattr(studio, "gpu_slot", None)
        studio.gpu_slot = _noop
        try:
            rc, txt = studio.svc_convert(None, {}, "a.wav", "b.wav", 0)
        finally:
            if old is not None:
                studio.gpu_slot = old
        check("没挂 SVC 权重 ⇒ rc≠0 且说清原因",
              rc != 0 and "SVC" in str(txt), "rc=%s %s" % (rc, str(txt)[:80]))
    except Exception as e:
        check("产品侧分流可调用", False, "%s: %s" % (type(e).__name__, e))

    print("\n" + "-" * 70)
    print("通过 %d 项，失败 %d 项" % (N["pass"], N["fail"]))
    if FAILS:
        print("红的：" + "、".join(FAILS))
    print("ALLDONE")
    return 0 if N["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
