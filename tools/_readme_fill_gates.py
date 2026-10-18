# -*- coding: utf-8 -*-
"""一次性脚本：把 README 门禁表格里缺失的 7 个门禁条目补到表格末尾。

只做一件事：在「以 | `verify_ncm_sep.py` 开头」那一行**之后**插入 7 行，
不动文件里任何其他内容。插入前逐条断言，锚点歪了就当场 exit。
"""
import io
import sys

PATH = r"D:\AHC\VoiceStudio\README.md"

NEW_ROWS = [
    "| `verify_sing_pitch.py` | **唱歌音高不许「翻八度还不知道」**：八度错误率、音高抖动（cents）、"
    "f0 方法选型都用机器钉住 —— 错了不会报错，只会静默烧掉十几分钟机时 |",
    "| `verify_slice_hifi.py`（第 31 关） | **保真切片 48k**：歌声训练素材不再被锁在 16 kHz"
    "（GPT-SoVITS 自带切片工具三处写死 32000，那是给说话模型用的） |",
    "| `verify_speech_long.py` | **长台词不许乱**：长文本分段 + 短段批处理，丢字必须能被 CER 量出来"
    "（实测 41.5% → 2.7%）|",
    "| `verify_tone.py` | **每一句的语气**：提示词里写「[愤怒]你怎么敢这么做！」，"
    "标签必须真被认出来且真生效（最阴的是「写了但没被认出来」，谁都不知道） |",
    "| `verify_train_quality.py`（第 30 关） | **训练质量**：开训前素材体检 / 轮数推荐 / "
    "v2 架构 / 参考音频挑选；素材全部现场合成，不依赖运行时残留 |",
    "| `verify_yue2.py`（第 32 关） | **「角色唱新歌」一体化管线**：YuE2 作曲演唱 → 复用 "
    "`sing_role` 换声；钉住契约 / 拆包 / 接线三层 |",
    "| `verify_yue2_ui.py`（第 33 关） | **翻唱台「写一首新歌」入口的真浏览器验收**："
    "真鼠标点得到、填得进、发得出（禁 `element.click()`） |",
]


def main():
    with io.open(PATH, encoding="utf-8") as fh:
        text = fh.read()
    lines = text.split("\n")

    idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("| `verify_ncm_sep.py`"):
            idx = i
            break
    if idx is None:
        sys.stderr.write("✗ 找不到 verify_ncm_sep.py 表格行，未改动\n")
        return 3

    # 断言：锚点行是完整表格行，且下一行不是表格行（说明它确实是最后一行）
    if not lines[idx].rstrip().endswith("|"):
        sys.stderr.write("✗ 锚点行不是完整表格行，未改动\n")
        return 4
    nxt = lines[idx + 1] if idx + 1 < len(lines) else ""
    if nxt.startswith("|"):
        sys.stderr.write("✗ 锚点行之后还有表格行，中止\n")
        return 5

    for r in NEW_ROWS:
        if not (r.startswith("| `verify_") and r.rstrip().endswith("|")):
            sys.stderr.write("✗ 新行格式不对: %r\n" % r[:40])
            return 6

    lines[idx + 1:idx + 1] = NEW_ROWS
    with io.open(PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    print("✔ 已在第 %d 行后插入 %d 行门禁条目" % (idx + 1, len(NEW_ROWS)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
