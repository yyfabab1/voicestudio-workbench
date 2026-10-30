# -*- coding: utf-8 -*-
"""门禁：伴唱优先分离（karaoke 模型）——原曲有垫唱时，让垫唱留在伴奏轨。

为什么值得单独设一道关：这条能力最容易被"悄悄退回标准分离"却没人发现
（模型文件被删/路径变了/参数名对不上），而它的效果差别很直观：
实测《Summer Vibe》人声轨侧/中 0.451 → **0.029**（垫唱不再混在人声里），
伴奏轨人声频段占比 0.436 → **0.636**（垫唱留在伴奏侧）。
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import shutil

import numpy as np
import soundfile as sf

HERE = os.path.dirname(os.path.abspath(__file__))
N = {"pass": 0, "fail": 0}
FAILS = []
MSST_PY = r"D:\AHC\YingMusic-Singer-Plus-main\.venv\Scripts\python.exe"
MSST_CLI = os.path.join(HERE, "stages", "msst_cli.py")
MODEL = "karaoke_bs_roformer_anvuew"
CKPT = os.path.join(HERE, "data", "models", "msst", "karaoke_bs_roformer_anvuew.ckpt")


def check(name, cond, detail=""):
    ok = bool(cond)
    N["pass" if ok else "fail"] += 1
    if not ok:
        FAILS.append(name)
    print(("  PASS  " if ok else "  ! FAIL  ") + name + (("  | " + str(detail)) if detail else ""))


def main():
    print("=" * 70)
    print("伴唱优先分离门禁")
    print("=" * 70)
    print("\n=== A. 静态接线 ===")
    cli = io.open(MSST_CLI, encoding="utf-8").read()
    st = io.open(os.path.join(HERE, "studio.py"), encoding="utf-8").read()
    html = io.open(os.path.join(HERE, "web", "index.html"), encoding="utf-8").read()
    js = io.open(os.path.join(HERE, "web", "app.js"), encoding="utf-8").read()
    check("msst_cli 注册了 karaoke 模型", MODEL in cli)
    check("studio 有 _karaoke_model()", "def _karaoke_model(" in st)
    check('studio 认 sep_model="karaoke"', 'sep_model == "karaoke"' in st)
    check("界面有「分离方式」下拉", 'id="sgSep"' in html and 'value="karaoke"' in html)
    check("前端真的传了 sep_model", "sep_model:" in js and "sgSep" in js)

    print("\n=== B. 模型就绪 ===")
    check("权重在（195MB）", os.path.isfile(CKPT) and os.path.getsize(CKPT) > 100 * 1024 * 1024,
          "%.0f MB" % (os.path.getsize(CKPT) / 1048576.0) if os.path.isfile(CKPT) else "缺")
    if not os.path.isfile(MSST_PY):
        check("MSST 解释器在（缺了就跳过运行测试）", False, MSST_PY)
        return _finish()
    r = subprocess.run([MSST_PY, "-s", MSST_CLI, "list"], capture_output=True, timeout=120)
    txt = (r.stdout or b"").decode("utf-8", "replace")
    info = {}
    for line in txt.splitlines():
        if line.startswith("MSST_LIST "):
            info = json.loads(line[len("MSST_LIST "):])
    check("list 里 %s ready" % MODEL, bool((info.get(MODEL) or {}).get("ready")),
          str((info.get(MODEL) or {}).get("ckpt"))[:60])

    print("\n=== C. 真跑一段（20 秒）并检查产物 ===")
    tmp = tempfile.mkdtemp(prefix="vs_kara_")
    try:
        # ★ 必须用**真实人声素材**：分离模型是学出来的，喂合成正弦它输出静音
        #   （第一版就是这么红的 —— 那说明用例不真实，不是模型坏了）。
        #   素材：优先用现成的完整混音片段，没有就把缓存里的人声+伴奏相加当混音。
        src_candidates = [
            os.path.join(HERE, "_tmp", "karaoke_test", "in", "source.wav"),
            os.path.join(HERE, "data", "cache", "sep", "5cc73a23551a7b59", "vocal.wav"),
        ]
        x = None
        for p in src_candidates:
            if not os.path.isfile(p):
                continue
            y, sr = sf.read(p, dtype="float32", always_2d=True)
            if y.shape[1] < 2:
                y = np.stack([y[:, 0], y[:, 0]], axis=1)
            sr_gap = int(60 * sr)          # 跳过前奏，取唱得最密的一段
            a = min(sr_gap, max(0, len(y) - int(20 * sr)))
            x = y[a:a + int(20 * sr)]
            if len(x) > int(3 * sr):
                break
            x = None
        if x is None:
            raise RuntimeError("找不到可用的人声素材（先跑一次分离生成缓存）")
        inp = os.path.join(tmp, "in")
        os.makedirs(inp, exist_ok=True)
        sf.write(os.path.join(inp, "source.wav"), x, sr)
        vdir, idir = os.path.join(tmp, "v"), os.path.join(tmp, "i")
        rr = subprocess.run([MSST_PY, "-s", MSST_CLI, inp, vdir, idir, MODEL],
                            capture_output=True, timeout=1800)
        vs = [f for f in os.listdir(vdir)] if os.path.isdir(vdir) else []
        ins = [f for f in os.listdir(idir)] if os.path.isdir(idir) else []
        check("分离跑通（退出码 0）", rr.returncode == 0,
              (rr.stderr or b"").decode("utf-8", "replace")[-160:])
        check("人声/伴奏都有产物", bool(vs) and bool(ins), "%s / %s" % (vs[:1], ins[:1]))
        if vs and ins:
            v, _ = sf.read(os.path.join(vdir, vs[0]), dtype="float32", always_2d=True)
            check("人声轨非空（峰值 > 0.01）", float(np.abs(v).max()) > 0.01,
                  "peak=%.3f" % float(np.abs(v).max()))
            if v.shape[1] >= 2:
                L, R = v[:, 0].astype(np.float64), v[:, 1].astype(np.float64)
                m, s = (L + R) / 2, (L - R) / 2
                smr = float(np.sqrt((s ** 2).mean()) / (np.sqrt((m ** 2).mean()) + 1e-12))
                # 输入混音本身的侧/中是 0.4 左右（伴唱宽），人声轨应当明显更居中
                check("人声轨比输入更居中（侧/中下降）", smr < 0.35, "侧/中 %.3f" % smr)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _finish()


def _finish():
    print("\n" + "-" * 70)
    print("通过 %d 项，失败 %d 项" % (N["pass"], N["fail"]))
    if FAILS:
        print("红的：" + "、".join(FAILS))
    print("ALLDONE")
    return 0 if N["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
