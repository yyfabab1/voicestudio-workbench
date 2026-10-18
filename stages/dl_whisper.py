# -*- coding: utf-8 -*-
r"""下载 faster-whisper 模型（走 ModelScope，国内可直连）。

为什么要单独一个脚本而不是在 studio.py 里拼 `python -c`：
  下载代码里有引号、有反斜杠路径、还有列表字面量 —— 塞进命令行会被转义搞坏
  （这个项目已经因为"长中文/带引号的代码块塞命令行"栽过，见 build 期的教训）。

用法（用 GPT-SoVITS 的 runtime 跑）：
    runtime\python.exe -s <本脚本> --dir <目标目录> [--size medium]

成功时最后一行打印 DL_OK <目录>。
"""
import argparse
import os
import sys
import traceback

REPO = "XXXXRT/faster-whisper"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="模型落地的目录（模型放在其下的 faster-whisper-<size>）")
    ap.add_argument("--size", default="medium")
    ap.add_argument("--cache", default="")
    a = ap.parse_args()

    sub = "faster-whisper-%s" % a.size
    os.makedirs(a.dir, exist_ok=True)
    if a.cache:
        os.environ.setdefault("MODELSCOPE_CACHE", a.cache)
        os.makedirs(a.cache, exist_ok=True)

    target = os.path.join(a.dir, sub)
    need = ("model.bin", "config.json", "tokenizer.json")
    if all(os.path.isfile(os.path.join(target, f)) for f in need):
        print("已经下载过：%s" % target)
        print("DL_OK %s" % target)
        return 0

    from modelscope import snapshot_download

    print("从 ModelScope 下载 %s/%s …" % (REPO, sub))
    sys.stdout.flush()
    try:
        p = snapshot_download(REPO, local_dir=a.dir, allow_patterns=["%s/*" % sub])
    except Exception:
        traceback.print_exc()
        print("[err] 下载失败（ModelScope 不通？）")
        return 1

    got = os.path.join(a.dir, sub)
    if not all(os.path.isfile(os.path.join(got, f)) for f in need):
        print("[err] 下载完成但文件不齐：%s" % got)
        return 1
    print("DL_OK %s" % got)
    return 0


if __name__ == "__main__":
    sys.exit(main())
