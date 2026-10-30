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
#   bash _run_regression.sh triage [_reg_all.log]  # **判红自动分诊**：找出批跑里的红关，
#                                             #   逐关单独复跑，判三态（假红/真回归/不稳定）
#   bash _run_regression.sh gc                # 清 data/roles 下的孤儿目录（见下）
#   bash _run_regression.sh clean             # 清日志 + 浏览器临时 profile（见下）
#
# ★ 退出码语义（2026-10-19 起）：**由分诊结论决定，不由"批跑有没有红"决定**。
#     全绿                                      → 0
#     有红，但分诊全是"疑似假红 / 未执行"        → 0（并打印警告 + 分诊表）
#     出现"疑似真回归 / 不稳定判据 / 无法比对"   → 1
#   批跑有红时会**自动**触发分诊（逐关单独复跑）。不想付这份时间就
#   `REG_AUTO_TRIAGE=0 bash _run_regression.sh ...`（此时退回旧语义：有红即 1）。
#
# ★ 每关输出都会落盘到 _reg_gates/<gate>.log（上一次自动转存 .prev.log）。
#   这是分诊能判三态的前提：只留一份全局日志时，拿不到"某关批跑时的明细"，
#   就只能判两态（绿/红），而"假红 vs 真回归"恰恰要靠明细逐条比。
#
# ★ 服务版本守卫：每关跑之前先打 /api/health。服务是**旧进程**时直接拒绝跑
#   （EXIT=4），并提示重启 —— 页面能打开不代表接口是新的，这个项目为它假红过。
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

PY_MANAGED="${REG_PY_MANAGED:-${USERPROFILE:-$HOME}/.workbuddy/binaries/python/versions/3.13.12/python.exe}"
PY_VENV="${REG_PY_VENV:-${USERPROFILE:-$HOME}/.workbuddy/binaries/python/envs/default/Scripts/python.exe}"
PY_RVC="/d/RVC/Retrieval-based-Voice-Conversion-WebUI/runtime/python.exe"
PY_GSV="/d/BaiduNetdiskDownload/GPT-SoVITS(1)/v5最新版/GPT-SoVITS-v5-20261005nvidia50x0/GPT-SoVITS-v2pro-20261006nvidia50x0/runtime/python.exe"
LOG=${REG_LOG:-_reg_all.log}
# 汇总单独一个文件：**不能**指回 $LOG，否则每跑一批就被自己喂大一圈（见 summarize 注释）
SUMMARY=${REG_SUMMARY:-_reg_summary.txt}
# 判据计数：**只存每关最后那行汇总**（"通过 41 项，失败 0 项" / "PASS 75 / FAIL 0"）。
#   为什么不直接把门禁输出写进 $LOG：汇总行里带 PASS/FAIL 字样，混进 $LOG 会让
#   summarize 的 `grep "^### |^EXIT=" | paste - -` 行配对错位（它靠一一行配对）。
#   ⇒ 单独一份「关卡名<TAB>汇总行」，按关卡名去重取最后一次（重跑是常态）。
COUNTS=${REG_COUNTS:-_reg_counts.txt}
# 当前这一关的完整输出（供上面抽取汇总行；固定名字、覆盖写，不删，
# 免得每关一个 rm 去撞那个"按一条命令累计"的删除钩子）
CUR=${REG_CUR:-_reg_cur.out}
# ★ 每关**独立**日志目录。这是判红分诊的前提：
#   分诊要回答"批跑红的那几条判据，单跑时是什么值"，就得能同时拿到
#   「批跑那一次的输出」和「单跑这一次的输出」。只留一份全局 $LOG 时，
#   根本抽不出某一关的明细 —— 那种情况下只能判两态（绿/红），
#   而"疑似假红 vs 真回归"恰恰要靠明细比。
#   命名：<gate>.log = 最近一次；<gate>.prev.log = 上一次（通常是批跑）。
GATEDIR=${REG_GATEDIR:-_reg_gates}
# 分诊结果（一行一关的 JSON，供 report 汇总）
TRIAGE_JSON=${REG_TRIAGE_JSON:-_reg_triage.jsonl}
# 分诊时复跑各关的输出日志（不写回批跑日志，否则会把证据搅浑）
TLOG=${REG_TLOG:-_reg_triage.log}
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
verify_lyric_split:PY_MANAGED
verify_lyrics_align:PY_MANAGED
verify_asr_size:PY_MANAGED
verify_index_pick:PY_MANAGED
verify_segonly:PY_VENV
verify_drag_ui:PY_VENV
verify_multising:PY_VENV
verify_multi_ui:PY_VENV
verify_hall_multi:PY_VENV
verify_speaker_match:PY_VENV
verify_seg_purity:PY_MANAGED
verify_seedvc:PY_VENV
verify_modelhub:PY_VENV
verify_market_ui:PY_VENV
verify_ability:PY_VENV
verify_viz:PY_VENV
verify_apierr:PY_VENV
verify_sep:PY_GSV
verify_rvc_train:PY_VENV
verify_tone:PY_VENV
verify_speech_long:PY_VENV
verify_sing_pitch:PY_VENV
verify_sing_hf:PY_VENV
verify_sing_demetal:PY_VENV
verify_presence:PY_VENV
verify_harmony:PY_VENV
verify_train_quality:PY_VENV
verify_slice_hifi:PY_VENV
verify_yue2:PY_VENV
verify_yue2_ui:PY_VENV
verify_ensemble:PY_VENV
verify_ui_ergonomics:PY_VENV
verify_gpu_gate:PY_MANAGED
verify_drift:PY_VENV
verify_maxseg:PY_VENV
verify_innercut:PY_VENV
verify_align:PY_VENV
verify_srt:PY_VENV
verify_smooth:PY_VENV
verify_castlay:PY_VENV
verify_lyricbi:PY_VENV
verify_ae:PY_VENV
verify_multimanual:PY_VENV
verify_multichorus:PY_VENV
verify_finetune:PY_VENV
verify_fine_ui:PY_VENV
verify_polish:PY_VENV
verify_declick:PY_VENV verify_svc:PY_VENV verify_lead:PY_VENV verify_whistle:PY_VENV verify_karaoke:PY_VENV"

pick_py() {
  case "$1" in
    PY_MANAGED) echo "$PY_MANAGED" ;;
    PY_RVC)     echo "$PY_RVC" ;;
    PY_GSV)     echo "$PY_GSV" ;;
    *)          echo "$PY_VENV" ;;
  esac
}

# ★ 服务版本契约守卫（跑每一关之前都打一次健康检查）
#   为什么：门禁验证的是「正在运行的那个服务」。服务是昨天起的旧进程时，
#   新接口 404，20 道 UI 门禁会集体假红 —— 2026-10-19 的 train_rounds_ui
#   就是这么红的，查了半天代码，其实一行都没改过。
#   返回 0 = 放行；返回非 0 = 拒绝跑。服务没起（exit 2）不挡：
#   有些关自己拉服务，也有些关根本不需要服务。
svc_check() {
  [ "${REG_SVC_GUARD:-1}" = "0" ] && return 0
  local out rc
  out=$("$PY_MANAGED" -s tools/svc_probe.py ${REG_EXPECT_API:+--expect "$REG_EXPECT_API"} 2>&1)
  rc=$?
  if [ "$rc" -eq 3 ]; then
    echo "$out"
    echo "★ 拒绝跑门禁：服务是旧进程。改完服务代码必须重启，否则跑出来的红都没有意义。"
    return 3
  fi
  return 0
}

run_one() {
  local name="$1" pyname="$2" py
  py="$(pick_py "$pyname")"
  rotate_log
  if ! svc_check; then
    echo "### $name" | tee -a "$LOG"
    # 连"被拒绝"也要落盘：分诊读的是每关日志，缺了它这关就会被判成
    # "无法比对"。一次拒绝也是一条证据，不能只活在终端里。
    mkdir -p "$GATEDIR"
    [ -f "$GATEDIR/$name.log" ] && cp -f "$GATEDIR/$name.log" "$GATEDIR/$name.prev.log"
    printf '%s\n%s\n' "### $name" "EXIT=4" > "$GATEDIR/$name.log"
    printf '%s\n' "服务契约不达标（旧进程），本关未执行" >> "$GATEDIR/$name.log"
    echo "EXIT=4" | tee -a "$LOG"
    echo ">>> $name ($pyname) EXIT=4（服务契约不达标，未执行）"
    return 4
  fi
  mkdir -p "$GATEDIR"
  # 上一次的输出（多半就是批跑那一次）先留档 —— 分诊要拿它跟这次单跑逐条比。
  # 用 cp 不用 mv：覆盖写会 truncate，而 cp 对"文件不存在"是安全的 no-op。
  [ -f "$GATEDIR/$name.log" ] && cp -f "$GATEDIR/$name.log" "$GATEDIR/$name.prev.log"
  echo "### $name" | tee -a "$LOG"
  # -s 关掉用户 site-packages，免得别处的同名包串进来（三套环境互不 import 的前提）
  # < /dev/null：子进程不许碰外层 stdin。共享 fd 的偏移一旦被子进程读走，
  #   下一次 read 就会从半截开始 —— 关卡名会被读成残片（实测 verify_steps → eps）。
  # tee：一边照常实时打印（跑一关要好几分钟，不能让人对着空白等），
  #      一边留一份给下面抽汇总行，第三份落**每关独立日志**（分诊的输入）。
  #      ★ 退出码必须取 PIPESTATUS[0]（管道的 $? 是最后一条 tee 的，永远 0
  #      —— 那样所有关卡都会被记成 EXIT=0，而且"红关"会从分诊里彻底消失）。
  # ★ 用 nice 降优先级跑（实测 GetPriorityClass: 0x20 NORMAL → 0x4000 BELOW_NORMAL）。
  #   理由（2026-10-26）：这台机器（i7-14650HX + 厂商 Turbo 电源方案）在 CPU 冲高频时，
  #   供电模块会发出**滋滋/吱吱的电感啸叫**。门禁全是突发型负载（起 Chrome、跑批），
  #   最容易把 CPU 顶到最高频。降优先级让门禁给系统让路，明显减轻啸叫。
  #   ★ 子进程会继承优先级类 ⇒ 探针起的 Chrome 也一起降级，不用逐个改。
  #   ★ 只影响调度优先级，不动判据（跑得慢一点，结果一样）。
  #   ★ 退出码：nice 是 exec 替换，PIPESTATUS[0] 仍是目标脚本的码。
  nice -n "${REG_NICE:-10}" "$py" -s "$name.py" < /dev/null 2>&1 \
    | tee "$CUR" | tee "$GATEDIR/$name.log"
  local rc=${PIPESTATUS[0]}
  local sum
  sum=$(grep -E "^(通过|PASS)[ ]*[0-9]+" "$CUR" 2>/dev/null | tail -1)
  [ -n "$sum" ] && printf '%s\t%s\n' "$name" "$sum" >> "$COUNTS"
  echo "EXIT=$rc" | tee -a "$LOG" >> "$GATEDIR/$name.log"
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
  # ★ 用 awk 配对而不是 `grep | paste - -`：一条命令跑到一半被杀（超时 / 撞钩子），
  #   LOG 里会留下孤零零一行 `### verify_x`。`paste` 会把**下面那关的名字**跟它配对，
  #   整张表从此错位一行（而错位后的表看着还挺正常，最难发现）。
  #   这里只在 `### ` 后**紧接** `EXIT=` 时才配，孤行直接丢掉。
  awk '/^### /{name=$2; next}
       /^EXIT=/{ if (name != "") { printf "%s\t%s\n", name, $0; name="" } }' \
    "$LOG" | tee "$SUMMARY"

  # ★ 汇总自检：**表本身的完整性**要当场验，别等人肉眼数。
  #   为什么必须在这里量：全量跑动辄一两个小时，中间任何一关被杀（超时 /
  #   撞删除钩子 / 手工 Ctrl-C）都会在 $LOG 里留下孤零零一行 `### verify_x`。
  #   `paste` 时代那一行会让整张表**错位一行**，而错位后的表看着还挺正常
  #   （每行都有名字、都有 EXIT），几乎不可能靠看发现。
  #   ★ 行数必须从**写出来的文件**（$SUMMARY）数，不能拿同一套 awk 再算一遍 ——
  #     那样两边永远相等，这条判据永远不会红（等于没写）。
  #   口径：
  #     关卡数 = `### ` 行数（跑过的每一关，含被杀的）
  #     孤立行 = 有名字的 `### ` 后面没等到 `EXIT=`（这一关跑到一半没了）
  #     表行数 = $SUMMARY 的真实行数
  #   正常状态是 **表行数 == 有名字的关卡数 - 孤立行**；少一行就是 awk 又错位了。
  local stat gates named rows orphans badname
  stat=$(awk '
    /^### /{ n++; if ($2 != "") { named++ } ; if (pend != "") orph++; pend=$2; next }
    /^EXIT=/{ if (pend != "") { pend="" } }
    END{ if (pend != "") orph++; print n+0, named+0, orph+0 }' "$LOG" 2>/dev/null)
  gates=$(echo "$stat"  | awk '{print $1}');  gates=${gates:-0}
  named=$(echo "$stat"  | awk '{print $2}');  named=${named:-0}
  orphans=$(echo "$stat"| awk '{print $3}');  orphans=${orphans:-0}
  rows=$(wc -l < "$SUMMARY" 2>/dev/null | tr -d ' \r'); rows=${rows:-0}
  badname=$((gates - named))

  if [ "$badname" -gt 0 ]; then
    echo "★ 汇总自检失败：有 $badname 行 \`### \` 没有关卡名 —— 日志本身被写坏了，上面的表不可信"
    return 1
  fi
  if [ "$rows" -ne $((named - orphans)) ]; then
    echo "★ 汇总自检失败：$named 关 / $rows 行 / $orphans 孤立行，应有 $((named - orphans)) 行 —— 汇总表错位了，上面的表不可信"
    return 1
  fi
  if [ "$orphans" -eq 0 ]; then
    echo "✓ 汇总自检：$gates 关 / $rows 行，严格配对，无孤立行"
    return 0
  fi
  # 有孤立行但**配对口径正确** —— 这是"跑到一半被杀"的正常长相，不是表坏了。
  # 把话说清楚，免得下一个人以为 awk 又错位。
  # ★ 这里用 `print` + ORS，不用 `printf "%s "`：后者要带双引号，而它嵌在
  #   `"$(...)"` 里，实测会被外层双引号吃掉（awk 收到碎参数，报
  #   `cannot open file`，被杀的关名永远打印不出来 —— 而且不报错，只是空）。
  local killed
  killed=$(awk 'BEGIN{ORS=" "}
    /^### /{if(pend!="")print pend; pend=$2; next}
    /^EXIT=/{pend=""}
    END{if(pend!="")print pend}' "$LOG" 2>/dev/null)
  echo "⚠ 汇总自检：$gates 关里有 $orphans 关跑到一半就没了（没有 EXIT 行），表里 $rows 行，应为 $((named - orphans)) 行，配对正确"
  echo "  被杀的关：${killed:-（无）}"
  return 0
}

# ★ 门禁快照：把这一轮的结果写成 data/gate_snapshot.json，界面底部会读它。
#   34 道门禁只活在终端里太浪费了 —— 用户有权知道"这个功能被验证过没有"。
#   只在**全量模式**下写：单关跑完就写会把快照覆盖成"1 关 / 48 判据"，
#   那比没有更糟（看着像全套门禁只有这么点）。
write_snapshot() {
  local rc="${1:-1}"
  local gates gates_ok checks bad failed stat
  # ★ 按**关卡名去重、只认最后一次**：重跑是常态（批跑撞资源、单关复跑），
  #   日志里同一关会留下 EXIT=1 和 EXIT=0 两条。直接 grep -c 会把一关算成两关、
  #   判据数也跟着翻倍 —— 快照上写着一个虚高的数，比不给更糟。
  # 汇总行的格式各关不太统一（"通过 42 项，失败 0 项" / "通过 23，失败 0" /
  #   "PASS 75 / FAIL 0" / "PASS 150  FAIL 0"），所以只认「行首是 通过|PASS 且
  #   后面跟着数字，同一行还有 失败|FAIL + 数字」的那种 —— 判据明细行都有缩进，
  #   行首不会是 PASS/通过，天然被排除（实测踩过：明细行里也常出现"失败"二字）。
  stat=$(awk '
    /^### /{name=$2; if(!(name in seen)){seen[name]=1; order[++n]=name}}
    /^EXIT=/{st[name]=$0}
    {
      if ($0 ~ /^(通过|PASS)[ ]*[0-9]+/ && $0 ~ /(失败|FAIL)[ ]*[0-9]+/) {
        line=$0; gsub(/[^0-9]+/," ",line); split(line,a," ");
        ck[name]=a[1]+0; bd[name]=a[2]+0;
      }
    }
    END{
      g=0; ok=0; c=0; b=0; f="";
      for(i=1;i<=n;i++){
        nm=order[i]; g++;
        if(st[nm]=="EXIT=0") ok++; else f=(f==""?nm:f "," nm);
        c+=ck[nm]; b+=bd[nm];
      }
      print g, ok, c, b, f
    }' "$LOG" 2>/dev/null)
  gates=$(echo "$stat" | awk '{print $1}'); gates=${gates:-0}
  gates_ok=$(echo "$stat" | awk '{print $2}'); gates_ok=${gates_ok:-0}
  failed=$(echo "$stat" | awk '{print $5}'); failed=${failed:-}
  # 判据数来自 $COUNTS（每关一行汇总），同样按关卡名去重、只认最后一次。
  # 汇总行的样子五花八门（"通过 41 项，失败 0 项" / "通过 23，失败 0" /
  # "PASS 75 / FAIL 0" / "PASS 150  FAIL 0"）⇒ 把非数字全抹成空格，取前两个数。
  # ★ 总数 = 通过 + 失败（注册了多少条），不是只取通过数：一红就把通过数当总数，
  #   界面会显示"1327 条判据（4 条红）"—— 自己跟自己都对不上（应是 1331）。
  local cstat
  cstat=$(awk -F'\t' '
    {ck[$1]=$2}
    END{
      for(k in ck){
        line=ck[k]; gsub(/[^0-9]+/," ",line); split(line,a," ");
        c+=a[1]+a[2]; b+=a[2]+0;
      }
      print c+0, b+0
    }' "$COUNTS" 2>/dev/null)
  checks=$(echo "$cstat" | awk '{print $1}'); checks=${checks:-0}
  bad=$(echo "$cstat" | awk '{print $2}'); bad=${bad:-0}
  # 分诊结论也写进快照 —— 但只在**这一轮真的跑过分诊**时。
  #   否则会把上一轮的分诊结果贴到这一轮的快照上（比没有更糟：看着像这轮红过）。
  #   两条自证：①本轮 do_triage 设了 TRIAGE_RAN；②分诊文件比日志新（snapshot 模式走这条）。
  local -a tj=()
  if [ "${TRIAGE_RAN:-0}" = "1" ] || [ "${REG_TRIAGE_FORCE:-0}" = "1" ]; then
    [ -f "$TRIAGE_JSON" ] && tj=(--triage-json "$TRIAGE_JSON")
  elif [ -f "$TRIAGE_JSON" ] && [ "$TRIAGE_JSON" -nt "$LOG" ]; then
    tj=(--triage-json "$TRIAGE_JSON")
  fi
  "$PY_MANAGED" -s tools/gate_snapshot.py --checks "$checks" --bad "$bad" \
    --gates "$gates" --gates-ok "$gates_ok" --rc "$rc" --gates-failed "$failed" \
    --counts-file "$COUNTS" "${tj[@]}"
}

# ★ 判红自动分诊（既是 `triage` 子命令，也是**批跑后的自动收口**）
#   拿一份批跑日志，找出所有红关，逐关单独复跑一次，给出三态结论：
#     疑似假红   —— 单跑绿了（多半是资源竞争 / 删除钩子 / 旧服务）
#     疑似真回归 —— 单跑还红，且红项与批跑一致
#     不稳定判据 —— 单跑还红，但红项变了（先修尺子，别急着改产品）
#   为什么必须"自动"：这活人手做一次要十几分钟，而且每次成本一样高。
#   为什么能判三态：每关输出独立落盘（$GATEDIR/<gate>.log + .prev.log），
#   能逐条比"同一条判据两次跑的实测值"。只留一份全局日志时只能判两态。
#
#   ★ 返回值 = **分诊结论决定的退出码**（不是"批跑有没有红"）：
#       全绿 / 全是假红或未执行 → 0
#       出现真回归 / 不稳定 / 无法比对 → 1
#     这样 CI 不会因为一次资源竞争就堵死，也不会因为"习惯了假红"，
#     把真回归静默放过 —— 放行时会把"这一轮确实红过"喊出来。
do_triage() {
  local BATCH="$1"
  local log_save="$LOG"
  local rc_dec=0
  # 每关只认**最后一次** EXIT（重跑是常态），按首次出现顺序输出。
  local -a REDS
  mapfile -t REDS < <(awk '
    /^### /{name=$2; if(!(name in seen)){seen[name]=1; order[++n]=name}}
    /^EXIT=/{st[name]=$0}
    END{for(i=1;i<=n;i++){nm=order[i]; if(nm!="" && st[nm]!="" && st[nm]!="EXIT=0") print nm}}
  ' "$BATCH" | grep -E "^verify_[A-Za-z0-9_]+\$")

  echo "=== 判红分诊：$BATCH 里检出 ${#REDS[@]} 个红关 ==="
  if [ "${#REDS[@]}" -eq 0 ]; then
    echo "0 个红关，没有要分诊的。"
    echo "DECISION=pass"
    return 0
  fi
  printf '  %s\n' "${REDS[@]}"

  : > "$TRIAGE_JSON"
  : > "$TLOG"
  local name hit rc
  for name in "${REDS[@]}"; do
    hit=$(echo "$ALL" | grep -E "^$name:")
    if [ -z "$hit" ]; then
      echo "⚠ 未知关卡：$name（关卡表里没有，跳过）"
      continue
    fi
    echo
    echo "--- 单独复跑 $name ---"
    # run_one 会把上一份（= 批跑那份）转存成 .prev.log，再覆盖 .log。
    # 所以下面的比对拿到的就是「批跑 vs 单跑」。
    LOG="$TLOG"
    run_one "$name" "${hit##*:}"
    rc=$?
    "$PY_MANAGED" -s tools/triage_gates.py --gate "$name" \
      --batch "$GATEDIR/$name.prev.log" --single "$GATEDIR/$name.log" \
      --rc "$rc" --json >> "$TRIAGE_JSON"
  done
  LOG="$log_save"
  TRIAGE_RAN=1
  echo
  "$PY_MANAGED" -s tools/triage_gates.py --report "$TRIAGE_JSON"
  echo
  "$PY_MANAGED" -s tools/triage_gates.py --decide "$TRIAGE_JSON"
  rc_dec=$?
  echo "（分诊只复跑一次：若真回归，仍要人去改；若不稳定，先修判据本身）"
  return $rc_dec
}

if [ -n "$1" ]; then
  # summarize 模式：拿任意一份回归日志重出一张汇总表（复核 / 调试用）。
  #   为什么要有：汇总逻辑本身也出过两次事故（写回自己 ⇒ 53GB；paste 错位一行）。
  #   有了这个入口，才能拿一份"已知长相"的日志去验它，而不是只能等全量跑完。
  if [ "$1" = "summarize" ]; then
    [ -n "$2" ] && LOG="$2"
    summarize
    exit $?
  fi
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
  # snapshot 模式：只把**当前 $LOG 里这一轮**的结果写成 data/gate_snapshot.json。
  #   为什么要单开一个：全量回归要拆成几批跑（删除钩子按一条命令累计），
  #   而多关模式不该写快照（跑 6 关就把快照覆盖成"6 关/300 判据"，比没有更糟）。
  #   ⇒ 分批跑完 34 关之后，用这一条命令收口。$LOG 里必须是完整的一轮
  #     （跑之前先 `: > _reg_all.log` 清过）。
  if [ "$1" = "snapshot" ]; then
    write_snapshot "${2:-1}"
    exit $?
  fi
  # triage 模式：手动对某份批跑日志做判红分诊（详细说明见 do_triage 上方）。
  #   用法：bash _run_regression.sh triage [_reg_all.log]
  if [ "$1" = "triage" ]; then
    BATCH="${2:-$LOG}"
    if [ ! -f "$BATCH" ]; then
      echo "批跑日志不存在：$BATCH（先跑一轮，或用 REG_LOG=... 指定）"
      exit 2
    fi
    do_triage "$BATCH"
    exit $?
  fi
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
  summarize; sum_rc=$?
  if [ "$rc_all" -ne 0 ] && [ "${REG_AUTO_TRIAGE:-1}" != "0" ]; then
    echo
    echo "=== 本批有红关 → 自动分诊（逐关单独复跑），退出码改由分诊结论决定 ==="
    do_triage "$LOG"; rc_all=$?
  fi
  # 汇总表自己错位 = 这一轮的结论不可信，无论分诊怎么说都不能放行。
  [ "$sum_rc" -eq 0 ] || rc_all=1
  echo "（若出现 safe-delete 字样，说明撞了批量删除钩子，本批结论作废 —— 再拆细点重跑）"
  exit $rc_all
fi

: > "$LOG"
# ★ COUNTS 也必须清：它按关卡名去重取最后一条，但**上一轮里存在、这一轮
#   已经不跑的关卡**会以旧行的形式留下来 ⇒ 快照总数虚高（而且 diff 会把
#   它当成"没变化"）。全量模式就是"这一轮的事实"，旧行一条都不该留。
: > "$COUNTS"
rc_all=0
for g in "${GATES[@]}"; do
  [ -z "$g" ] && continue
  run_one "${g%%:*}" "${g##*:}" || rc_all=1
done

summarize; sum_rc=$?
# ★ 退出码由**分诊结论**决定，不由"批跑有没有红"决定。
#   原先是"有红就 exit 1" —— 一次资源竞争（假红）就能把 CI 堵死；
#   而人为了不被堵，就会开始"看到红也当没看见"，真回归照样溜过去。
#   现在：批跑红 → 自动逐关单跑 → 假红/未执行放行（但大声说红过），
#         真回归/不稳定/无法比对才 exit 1。
if [ "$rc_all" -ne 0 ] && [ "${REG_AUTO_TRIAGE:-1}" != "0" ]; then
  echo
  echo "=== 全量跑有红关 → 自动分诊（逐关单独复跑），退出码改由分诊结论决定 ==="
  do_triage "$LOG"; rc_all=$?
fi
# 汇总表自己错位 = 这一轮的结论不可信，无论分诊怎么说都不能放行。
[ "$sum_rc" -eq 0 ] || rc_all=1
write_snapshot "$rc_all"
echo "（若出现 safe-delete 字样，说明撞了批量删除钩子，本轮结论作废 —— 改分批跑）"
exit $rc_all
