# -*- coding: utf-8 -*-
r"""把 **transformers 格式的 whisper 权重**转成 faster-whisper 能吃的 CTranslate2 目录。

为什么要有这个脚本：
  「歌声专用档」(Hengyuhan/whisper-medium-m4singer-zh-lyrics) 上游只发了
  transformers 格式（model.safetensors），而我们的识别链路跑的是
  faster-whisper（CTranslate2）——它快、省显存，而且**词级时间戳是现成的**，
  改词分块全靠那个时间戳。所以必须转。

两个坑（都踩过，写在这里免得下次再花一小时）：
  ① ctranslate2 4.8.1 的转换器会传 `dtype=` 给 from_pretrained，
     **transformers 4.51.3 不认**（它只认 `torch_dtype=`）⇒ 必须用 >=4.56。
     但 runtime 里装的 4.51.3 是别的环节在用的，不能动 ⇒ 用隔离目录里的新版。
  ② 有些微调权重带的 `tokenizer_config.json` 里有 `extra_special_tokens`
     且是 **list**，新版 transformers 要 **dict** ⇒ 直接把这个键删掉即可
     （官方 openai/whisper-medium 的配置里根本没这个键，删了不影响时间戳 token）。

用法：
    1) 装隔离依赖（只装进 .asr-lib，不碰 runtime）：
       runtime\python.exe -m pip install --target .asr-lib "transformers==4.57.6"
       —— 它会顺手拖一份 CPU 版 torch 进来（因为 --target 模式看不到已装包），
          装完记得把 .asr-lib/torch 挪走，否则会盖掉 GPU 版 torch。
    2) 跑本脚本：
       python tools/convert_whisper_ct2.py --src models/whisper-m4singer-zh \
           --dst models/faster-whisper-m4singer --asr-lib .asr-lib
    3) 补两个转换器不产出的文件（faster-whisper 加载时需要）：
       copy models\whisper-m4singer-zh\tokenizer.json  <dst>\
"""
import argparse
import os
import sys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="transformers 格式权重目录")
    ap.add_argument("--dst", required=True, help="输出的 CT2 目录")
    ap.add_argument("--asr-lib", default="",
                    help="装了新版 transformers 的隔离目录（没有就用当前环境）")
    ap.add_argument("--quant", default="float16")
    a = ap.parse_args()

    if a.asr_lib:
        a.asr_lib = os.path.abspath(a.asr_lib)
        if os.path.isdir(a.asr_lib):
            sys.path.insert(0, a.asr_lib)
        else:
            print("[warn] 隔离目录不存在：%s（用当前环境的 transformers）" % a.asr_lib)

    import transformers
    print("transformers %s" % transformers.__version__)
    if tuple(int(x) for x in transformers.__version__.split(".")[:2]) < (4, 56):
        print("[err] 需要 transformers>=4.56（转换器会传 dtype=，旧版不认）")
        return 2

    # ② 修掉那个会炸的旧格式字段（改的是本地副本，不动上游）
    import json
    tc = os.path.join(a.src, "tokenizer_config.json")
    if os.path.isfile(tc):
        with open(tc, encoding="utf-8") as f:
            d = json.load(f)
        es = d.get("extra_special_tokens")
        if isinstance(es, list):
            bak = tc + ".orig.json"
            if not os.path.isfile(bak):
                with open(bak, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
            d.pop("extra_special_tokens", None)
            with open(tc, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            print("已移除 list 格式的 extra_special_tokens（原文件留作 .orig.json）")

    from ctranslate2.converters import TransformersConverter
    print("转换 %s → %s（%s）" % (a.src, a.dst, a.quant))
    TransformersConverter(a.src).convert(a.dst, quantization=a.quant, force=True)

    # ③ 补 tokenizer.json（转换器只产出 vocabulary.json，faster-whisper 不够用）
    src_tok = os.path.join(a.src, "tokenizer.json")
    dst_tok = os.path.join(a.dst, "tokenizer.json")
    if os.path.isfile(src_tok) and not os.path.isfile(dst_tok):
        import shutil
        shutil.copy2(src_tok, dst_tok)
        print("已补 tokenizer.json")
    print("DONE %s" % a.dst)
    return 0


if __name__ == "__main__":
    sys.exit(main())
