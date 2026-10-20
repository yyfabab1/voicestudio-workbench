# -*- coding: utf-8 -*-
r"""下载 faster-whisper 模型。

为什么要单独一个脚本而不是在 studio.py 里拼 `python -c`：
  下载代码里有引号、有反斜杠路径、还有列表字面量 —— 塞进命令行会被转义搞坏
  （这个项目已经因为"长中文/带引号的代码块塞命令行"栽过，见 build 期的教训）。

两条来源，各自有理由：
  · medium（默认档）走 **ModelScope** —— 国内直连，1.5 GB；
  · large-v3（精准档）走 **hf-mirror** —— ModelScope 上没有现成的
    CTranslate2 版 large-v3，而 huggingface.co 在本机恒 HTTP 000；
    hf-mirror 上有 Systran/faster-whisper-large-v3 的 CT2 权重，按文件直拉。
    （不自己转 ct2：那要先装 torch+transformers+ctranslate2、再下 3 GB 的
      PyTorch 权重，多花一倍时间还得配 CUDA 版本，纯粹给自己找麻烦。）

用法（用 GPT-SoVITS 的 runtime 跑）：
    runtime\python.exe -s <本脚本> --dir <目标目录> [--size medium|large-v3]

成功时最后一行打印 DL_OK <目录>。
"""
import argparse
import contextlib
import os
import sys
import time
import traceback
import urllib.request

REPO = "XXXXRT/faster-whisper"
HF_MIRROR = ("https://hf-mirror.com/Systran/faster-whisper-large-v3"
             "/resolve/main/")
# CT2 权重目录里必须有这三个才能加载；多下两个是为了以后重新量化/改配置不用再联网
HF_FILES = ["config.json", "preprocessor_config.json", "tokenizer.json",
            "vocabulary.json", "model.bin"]
NEED = ("model.bin", "config.json", "tokenizer.json")
RETRIES = 3


def _get(url, dst):
    """带重试地把一个文件拉下来。每 25 MB 打一行进度 —— 3 GB 的文件
    十分钟没动静，用户会以为卡死了（这个项目吃过这个亏）。"""
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "VoiceStudio"})
            with urllib.request.urlopen(req, timeout=120) as r:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                nxt = 25 << 20
                t0 = time.time()
                with open(dst, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                        if got >= nxt:
                            nxt += 25 << 20
                            print("  … %.0f / %.0f MB（%.0f 秒）"
                                  % (got / 1048576.0, total / 1048576.0,
                                     time.time() - t0), flush=True)
            return True
        except Exception as e:                                # noqa: BLE001
            last = e
            print("  [warn] 第 %d 次下载失败：%s" % (attempt + 1, str(e)[:120]),
                  flush=True)
            time.sleep(3 * (attempt + 1))
    print("[err] 下载失败：%s —— %s" % (url, str(last)[:200]))
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True,
                    help="模型落地的目录（模型放在其下的 faster-whisper-<size>）")
    ap.add_argument("--size", default="medium")
    ap.add_argument("--cache", default="")
    a = ap.parse_args()

    sub = "faster-whisper-%s" % a.size
    os.makedirs(a.dir, exist_ok=True)
    if a.cache:
        os.environ.setdefault("MODELSCOPE_CACHE", a.cache)
        os.makedirs(a.cache, exist_ok=True)

    target = os.path.join(a.dir, sub)
    if all(os.path.isfile(os.path.join(target, f)) for f in NEED):
        print("已经下载过：%s" % target)
        print("DL_OK %s" % target)
        return 0

    if a.size == "large-v3":
        os.makedirs(target, exist_ok=True)
        print("从 hf-mirror 下载 faster-whisper-large-v3（约 3.1 GB）…", flush=True)
        for name in HF_FILES:
            dst = os.path.join(target, name)
            if os.path.isfile(dst) and os.path.getsize(dst) > 1024:
                print("  已有 %s，跳过" % name, flush=True)
                continue
            print("  拉取 %s …" % name, flush=True)
            if not _get(HF_MIRROR + name, dst):
                with contextlib.suppress(Exception):
                    os.remove(dst)
                return 1
    else:
        print("从 ModelScope 下载 %s/%s …" % (REPO, sub))
        sys.stdout.flush()
        try:
            from modelscope import snapshot_download
            snapshot_download(REPO, local_dir=a.dir, allow_patterns=["%s/*" % sub])
        except Exception:
            traceback.print_exc()
            print("[err] 下载失败（ModelScope 不通？）")
            return 1

    if not all(os.path.isfile(os.path.join(target, f)) for f in NEED):
        print("[err] 下载完成但文件不齐：%s" % target)
        return 1
    print("DL_OK %s" % target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
