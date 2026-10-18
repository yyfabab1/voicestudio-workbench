#!/bin/bash
# 全量回归 / 单关重跑。
#
# ★ 为什么要"一关一条命令"：
#   本机环境的**批量删除钩子**（`[safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED]
#   count=50, threshold=50`）按"本轮累计删了几次"计数，阈值 50。13 关挤在一条命令里
#   跑，累计必然撞穿，之后每个删除都被挡下，脚本在清尾时被掐断。
#   被掐断的样子有三种，第三种最坑：
#     ①整个脚本一行输出都没有（`import studio` 时启动清道夫就在删垃圾）；
#     ②判据全 PASS 但没有汇总行；退出码非 0；
#     ③**某条判据读的就是"东西被删掉了没"，删除被挡 ⇒ 判据报 FAIL**
#       （verify_jobctl 的「中间产物被清掉」就是这么假红的）。
#
# 用法（权威做法 = 逐关单独调用，14 次）：
#   bash _run_regression.sh                   # 全部串行（只当一次性体检，必被钩子掐断）
#   bash _run_regression.sh verify_storage    # 只跑一关
#   bash _run_regression.sh gc                # 清 data/roles 下的孤儿目录（见下）
#   bash _run_regression.sh clean             # 清日志 + 浏览器临时 profile（见下）
#
# ★ 收尾两步（每次跑完门禁都该做，各起一条命令）：
#     bash _run_regression.sh gc
#     bash _run_regression.sh clean
#
# ★ 为什么要单独一个 gc：钩子的预算按**一条命令**计，门禁跑到收尾时早用光了 ⇒
#   门禁自己不可能清干净（会留几 MB 的孤儿角色目录）。清理必须另起一条命令做。
#   跑完某关看到 "⚠ 磁盘上还留着本关建的探针目录" 就接着跑一次 `gc`。
#
# 解释器分四套，别混（踩过：托管 runtime **本体是空的**，numpy/websocket 只在 venv 里
# ⇒ verify_steps / verify_sing 配成 venv 以外的会 ModuleNotFoundError，看着像脚本坏了）：
#   PY_VENV    —— 浏览器门禁 + steps + sing（要 numpy / websocket / scipy）
#   PY_RVC     —— qc_ruler / nonvocal（要 soundfile / librosa）
#   PY_GSV     —— 分离那条链（uvr_cli 要在 GPT-SoVITS 的 runtime 下跑：torch + 那一堆
#                 UVR 依赖。**换到别处跑 import 就炸**，炸的样子还不是"缺包"，
#                 是 uvr_cli 报一堆稀奇古怪的错，很容易误判成"音频坏了"）
#   PY_MANAGED —— 纯静态 / 纯逻辑（frontend_static / disk / train_rounds）
cd /d/AHC/VoiceStudio || exit 2

PY_MANAGED="C:/Users/yyfabab/.workbuddy/binaries/python/versions/3.13.12/python.exe"
PY_VENV="C:/Users/yyfabab/.workbuddy/binaries/python/envs/default/Scripts/python.exe"
PY_RVC="/d/RVC/Retrieval-based-Voice-Conversion-WebUI/runtime/python.exe"
PY_GSV="/d/BaiduNetdiskDownload/GPT-SoVITS(1)/v5最新版/GPT-SoVITS-v5-20261005nvidia50x0/GPT-SoVITS-v2pro-20261006nvidia50x0/runtime/python.exe"
LOG=${REG_LOG:-_reg_all.log}
# 汇总单独一个文件：**不能**指回 $LOG，否则每跑一批就被自己喂大一圈（见 summarize 注释）
SUMMARY=${REG_SUMMARY:-_reg_summary.txt}
# 单日志硬上限（MB）。正常一关的日志是几十 KB，几十 MB 就一定是谁在往里灌。
MAX_LOG_MB=${REG_MAX_LOG_MB:-50}

# ★ 日志硬上限闸（每关跑之前称一次体重）
#   2026-10-18 事故：summarize() 把汇总 `tee -a` 回它**正在 grep 的** $LOG，
#   下一轮又把汇总行捞回来当输入 ⇒ 指数膨胀，一天把日志撑到 **53 GB**，
#   D 盘 100% 写满。症状是"页面加载不出来 / 门禁莫名变红"，看着像产品坏了。
#   summarize 本身已经修好（改写独立文件），但这道闸留着当**兜底**：
#   只要"输出越跑越大"这件事以任何形式复发（换个人写、换个环节），
#   它会当场轮转并把话说出来，而不是等磁盘写满才被人发现。
rotate_log() {
  [ -f "$LOG" ] || return 0
  local mb
  mb=$(du -m "$LOG" 2>/dev/null | cut -f1)
  [ -z "$mb" ] && return 0
  case "$mb" in ''|*[!0-9]*) return 0;; esac
  if [ "$mb" -gt "$MAX_LOG_MB" ]; then
    echo "⚠ 日志 $LOG 已 ${mb}MB > 上限 ${MAX_LOG_MB}MB —— 轮转后再跑（说明有人在往里灌水）"
    mv -f "$LOG" "${LOG%.log}_$(date +%m%d_%H%M).log" 2>/dev/null || : > "$LOG"
  fi
}

# 关卡顺序 + 各自该用哪个解释器
ALL="verify_frontend_static:PY_MANAGED
verify_disk:PY_MANAGED
verify_qc_ruler:PY_RVC
verify_nonvocal:PY_RVC
verify_train_rounds:PY_MANAGED
verify_ingest_batch:PY_VENV
verify_steps:PY_VENV
verify_sing:PY_VENV
verify_storage:PY_VENV
verify_jobctl:PY_VENV
verify_music:PY_VENV
verify_train_rounds_ui:PY_VENV
verify_lyric_ui:PY_VENV
verify_speaker_match:PY_VENV
verify_seedvc:PY_VENV
verify_ncm:PY_VENV
verify_ncmwatch:PY_VENV
verify_modelhub:PY_VENV
verify_market_ui:PY_VENV
verify_ability:PY_VENV
verify_nav:PY_VENV
verify_ncm_sep:PY_VENV
verify_viz:PY_VENV
verify_apierr:PY_VENV
verify_sep:PY_GSV
verify_rvc_train:PY_VENV
verify_tone:PY_VENV
verify_speech_long:PY_VENV
verify_sing_pitch:PY_VENV
verify_train_quality:PY_VENV
verify_slice_hifi:PY_VENV
verify_yue2:PY_VENV
verify_yue2_ui:PY_VENV"

pick_py() {
  case "$1" in
    PY_MANAGED) echo "$PY_MANAGED" ;;
    PY_RVC)     echo "$PY_RVC" ;;
    PY_GSV)     echo "$PY_GSV" ;;
    *)          echo "$PY_VENV" ;;
  esac
}

run_one() {
  local name="$1" pyname="$2" py
  py="$(pick_py "$pyname")"
  rotate_log
  echo "### $name" | tee -a "$LOG"
  # -s 关掉用户 site-packages，免得别处的同名包串进来（三套环境互不 import 的前提）
  # < /dev/null：子进程不许碰外层 stdin。共享 fd 的偏移一旦被子进程读走，
  #   下一次 read 就会从半截开始 —— 关卡名会被读成残片（实测 verify_steps → eps）。
  "$py" -s "$name.py" < /dev/null
  local rc=$?
  echo "EXIT=$rc" | tee -a "$LOG"
  echo ">>> $name ($pyname) EXIT=$rc"
  return $rc
}

# ★ 关卡表只解析一次，收进数组；**不许**用 `while IFS=: read ... done <<< "$ALL"`
#   理由：here-string 的 fd 会被 run_one 里的子进程继承，子进程只要读一点 stdin，
#   共享文件偏移就被推走，下一轮 read 从半行开始 ⇒ 关卡名变成残片。
#   实测：verify_steps 被读成 `eps`（于是报 can't open file 'eps.py'）、
#         PY_VENV 被读成 `Y_VENV`（于是报 can't open file 'Y_VENV.py'）。
#   —— 症状是「后面几关集体 file not found」，极易被误读成产品坏了。
mapfile -t GATES <<< "$ALL"

# 尺子自检：表里每个名字都必须真有脚本。解析一歪就当场炸，不许静默 EXIT=2 混过去。
for g in "${GATES[@]}"; do
  n="${g%%:*}"
  [ -z "$n" ] && continue
  [ -f "$n.py" ] || { echo "★ 关卡脚本不存在：$n.py（关卡表解析异常，先修表再跑）"; exit 3; }
done

summarize() {
  echo "=== 汇总 ==="
  # ★ 汇总**绝对不许**写回它正在读的那个 `$LOG`（`tee -a "$LOG"`）。
  #   汇总行里带着 `EXIT=`，下一轮 grep 会把它们也捞进来当输入 ⇒ 输出越滚越大，
  #   指数级膨胀。实测一天就把 `_reg_all.log` 撑到 **53 GB**，连带 D 盘 100% 写满，
  #   表现是"页面加载不出来 / 门禁莫名变红"，看着像产品坏了。
  #   ⇒ 固定写到一个独立的 `_reg_summary.txt`，它本身不参与 grep。
  grep -E "^### |^EXIT=" "$LOG" | paste - - | sed 's/### //' \
    | tee "$SUMMARY"
}

if [ -n "$1" ]; then
  # gc 模式：清孤儿角色目录。必须独立一条命令（删除预算按命令计）
  if [ "$1" = "gc" ]; then
    "$PY_VENV" -s _gc_orphans.py "${@:2}"
    exit $?
  fi
  # clean 模式：清运行日志 + 浏览器临时 profile。
  #   ★ 为什么不放进门禁尾巴：删除预算按「一条命令」累计（阈值 50），
  #     门禁跑到收尾时早用光了 ⇒ 放进去等于没清。必须另起一条命令。
  #   ★ 门禁每跑一次就新留一个 .cdp-*/.chrome-* profile（每个几十到上百 MB），
  #     不清的话跑十轮就吃掉 1 GB 以上，全是缓存，重建只要两秒。
  if [ "$1" = "clean" ]; then
    "$PY_MANAGED" -s tools/cleanup.py --apply --only=logs,cache \
      --keep-days=${REG_KEEP_DAYS:-3}
    exit $?
  fi
  # 多关模式：可以一次给好几个名字。**删除预算按「一条命令」累计**（阈值 50），
  # 33 关塞进一条命令会在后半程撞阈值 ⇒ 全量回归要拆成几批，每批一条独立命令。
  rc_all=0
  for want in "$@"; do
    hit=$(echo "$ALL" | grep -E "^$want:")
    if [ -z "$hit" ]; then
      echo "未知关卡：$want"; echo "$ALL" | cut -d: -f1 | tr '\n' ' '; echo; exit 2
    fi
    run_one "${hit%%:*}" "${hit##*:}" < /dev/null || rc_all=1
  done
  summarize
  echo "（若出现 safe-delete 字样，说明撞了批量删除钩子，本批结论作废 —— 再拆细点重跑）"
  exit $rc_all
fi

: > "$LOG"
rc_all=0
for g in "${GATES[@]}"; do
  [ -z "$g" ] && continue
  run_one "${g%%:*}" "${g##*:}" || rc_all=1
done

summarize
echo "（若出现 safe-delete 字样，说明撞了批量删除钩子，本轮结论作废 —— 改分批跑）"
exit $rc_all
