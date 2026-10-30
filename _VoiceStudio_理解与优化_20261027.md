# VoiceStudio 理解与优化（2026-10-27）

> 只写**当下代码里真实存在**的东西：常量、阈值、顺序、角色 id、实测数字。
> 凡是拿不准的，写"未验证"，不写结论。

## 1. 它是什么

一个 Python 进程 + 原生前端的 AI 翻唱工作台：把视频/音频丢进去 → 分离人声 → 按声音分人 →
每个角色单独训练音色 → 让角色唱别人的歌、或说台词 → 出片。入口 `启动VoiceStudio.bat`
（服务 `127.0.0.1:8756`），AI/脚本入口是 `vs.py`（11 个子命令）。

| 模块 | 它实际做什么 | 入口 |
|---|---|---|
| 一键翻唱台 | 角色卡拖左栏、歌拖右栏，点一次出成品；没歌声音色会先训再唱 | `web/index.html#secStage`、`studio.sing_job` |
| 批量导入 + 自动认人 | 多视频先按声音分人，再拿声纹回库认"这是哪个已有角色" | `studio.ingest_job` |
| 切片 / 保真切片 | 按静音切段；48k 保真档可选 | `stages/slice_hifi.py` |
| ASR 与改词 | 自动识原词；新词句数对得上才给时间轴 | `stages/lyrics_asr.py`、`stages/lyric_bi.py` |
| RVC 歌声训练 | 预处理 → 提 F0 → 提 HuBERT → 底模校验 → 训 N 轮 → 建索引 | `stages/train_cli.py`、`studio.train_rvc` |
| 分离自动选优 | 多模型跑分离，按真值 SDR 打分挑最好的 | `studio.separate_song`、`stages/sep_eval.py` |
| 非歌声段保护 | 口哨 / 间奏不许被"唱"出来（keep/mute/off） | `stages/nonvocal_guard.py` |
| 说话（长台词） | GPT-SoVITS，长文本分段 + 短段批处理 | `studio.speak_job` |
| 做视频 | 成品渲染成 MP4：可视化画面 / 只换音轨 / 叠加可视化 | `viz.py`、`vizfx.py` |
| 声音市场 | 搜 HF 上公开音色模型，装完自动变中文名 | `modelhub.py`、`web/mkt.js` |
| 曲库 | 本地歌导入（只登记路径、不复制文件） | `local_lib.py`、`music_library.json` |
| 成品质检 | 把"难听"变成能对账的数 | `studio.qc_singing` |
| 任务控制 / 存储清理 | 暂停 / 继续 / 真取消；扫大文件删训练权重 | `studio.py`、`tools/cleanup.py` |

## 2. 唱歌链路（单角色）

1. 分离人声与伴奏（多候选先探针打分，`mel_band_roformer_kj` 进池，漏电超 `SEP_PROBE_LEAK_MAX=0.20` 一票否决）
2. 改词（可选，YingMusic-Singer-Plus，28 s 分块）
3. **唱前整轨微调**（`stages/polish.py pre`：declip / declick / hp / denoise / level；默认只动削波与爆音）
4. 音高自动估计（RMVPE 量原唱与角色基频中位数，`12·log2` 得半音数）
5. 索引解析（`_find_rvc_index`；缺索引时 index-rate **静默降级为 0**）
6. 音高体检（先拿 30 s 试唱几档，可改判音高；金属声判定看 HNR）
7. **RVC 变声**（`stages/rvc_infer.py`：20 s 分片、每 30 s 一个干净子进程、相位对齐、f0 护栏）
8. 高频补偿（`stages/hf_cli.py` auto：量 6~12k 缺口，候选分频从高到低，补完再量）
9. Seed-VC 精修（仅 `engine=="seedvc"` 时）
10. 去金属声（`stages/demetal_cli.py`：补气声 → 削 4~8k → 分频抖动，修完复测）
11. 临场感补偿（`stages/presence_cli.py`：2~5k 缺口，只加不减，**在 demetal 之后**）
12. **唱后整轨微调**（`stages/polish.py` post：deess / deharsh / belt / heal / limit）
13. 非歌声段保护（`stages/nonvocal_guard.py`：谱峰数 ≤3、最强峰 ≥0.72、2.5k 以上 ≤0.03）
14. 混音（`stages/mix_cli.py`：平滑噪声门 −45 dB、RMS 对齐、峰值 0.95）
15. 质检（30 s 窗口 + 整曲聚合，lag=0 量音准）
16. 存成品（含参数快照）

**顺序约束（都有实测代价，不许再颠）**：declip 在 denoise 之前；presence 在 demetal 之后；
唱前在变声之前、唱后在 presence 之后。

## 3. 唱歌参数面：真正生效的只有这 6 个

| 参数 | 当前值 | 在哪定 | 界面 |
|---|---|---|---|
| f0 提取 | `rmvpe` | `studio.RVC_F0_METHOD` | 无（写死） |
| index_rate | 0.50 | `studio.RVC_INDEX_RATE` + 角色页 `#inpIdx` | 有 |
| rms_mix_rate | 0.25 | `studio.RVC_RMS_MIX_RATE` | **只有 API** |
| protect | 0.33（必须 <0.5，0.5 = 关闭保护） | `studio.RVC_PROTECT` | **只有 API** |
| 分片长度 | 20 s | `studio.RVC_CHUNK_SEC` | 无（写死） |
| 变调 | 用户 −24..+24 | UI `#selPitch/#inpPitch` | 有 |

另外三个"看似该有、其实没有"的：`filter_radius` —— **这个 RVC 运行时（`infer/vc/pipeline.py`）
根本没有这个参数**，不是没接线；`resample_sr` 与 `speaker_id` 恒为 0；cluster ratio 单说话人用不上。

## 4. 模型与角色现状（截至 2026-10-27 18:00）

- `data/modelhub` 装了 5 个 MLP 合集：`MTNProductions_MyLittlePony_RVC2`（24 文件）、
  `MusicBox27/MyLittlePonyRVC`（66）、`therealvul/RVCv2`（68）、`therealvul/RVC`（7）、
  `Xhepyxopila/MyLittlePonyRVC`（7，基本没有 M6）；`PinksMlpAI/SunsetShimmerSinging`（0，空）。
- RVC 运行时 `assets/weights` 只有 **9** 个权重（大帝自训 3 个 + 市场导入 6 个），`assets/indices` 9 个。
- `roles.json` 共 **7 个角色**：大帝（自训）、紫悦（RVCv2）、珍奇、柔柔、**苹果嘉儿 ×3 重复**
  （都来自 MusicBox27）。**M6 缺云宝、碧琪**。
- 索引命名规律：`<rid>_added_IVF{N}_Flat_nprobe_1_<rid>_v1.index`；市场导入的一律重命名成这个口径，
  否则 `_find_rvc_index` 认不出 ⇒ 静默降级。

## 5. 声音市场怎么工作

`modelhub.py` 只查 **HuggingFace API**（hf-mirror → huggingface.co），不查 weights.gg。
`/api/models?filter=<tag>&sort=downloads` → 记录 repo/name/author/**downloads/likes**/license/文件/packs/中文名
→ `POST /api/jobs {kind:"install_model", repo, file, files[], all, max_roles}`
→ `studio.job_install_model`：下载/解压 → 每个 .pth 建一个角色 → 权重复制成 `<rid>.pth`、
索引配对并重命名 → `rvc_ckpt_check.py` **真 torch.load 一次**（文件在 ≠ 能加载）→ 写 `rvc` 字段 + 中文名。

## 6. 门禁（`_run_regression.sh` 的 `ALL`，当前 65 关）

- **唱歌路径相关**：`verify_sing`（主链端到端）、`verify_sing_pitch`（八度错/抖动/f0 方法）、
  `verify_sing_hf`（高频补偿不一刀切）、`verify_sing_demetal`（去金属双边护栏 + 顺序）、
  `verify_presence`（2~5k 只加不减 + 顺序）、`verify_sep`（分离尺子 + 选优 + 否决权）、
  `verify_nonvocal`（非歌声守卫）、`verify_polish` / `verify_declick`（整轨微调 + 爆音判据）、
  `verify_harmony`、`verify_multising` / `verify_multichorus` / `verify_segonly`、
  `verify_seedvc`、`verify_yue2`、`verify_maxseg` / `verify_innercut` / `verify_align` / `verify_drift` / `verify_smooth`。
- **训练**：`verify_rvc_train`、`verify_train_quality`、`verify_train_rounds(+_ui)`、`verify_slice_hifi`、`verify_index_pick`。
- **UI**：`verify_nav`、`verify_market_ui`、`verify_multi_ui`、`verify_lyric_ui`、`verify_fine_ui`、
  `verify_frontend_static`、`verify_ui_ergonomics`、`verify_drag_ui`、`verify_yue2_ui`、`verify_qc_ruler`。
- **其他**：`verify_asr_size`、`verify_lyric*`、`verify_srt`、`verify_seg*`、`verify_modelhub`、
  `verify_viz`、`verify_ae`、`verify_disk`、`verify_storage`、`verify_jobctl`、`verify_gpu_gate`、
  `verify_apierr`、`verify_castlay`、`verify_speaker_match`、`verify_ability`、`verify_music`、`verify_tone`、
  `verify_speech_long`、`verify_ingest_batch`、`verify_steps`、`verify_ensemble`、`verify_train_rounds`。

## 7. 现状问题（来自 10-25..27 自己的报告）

| 问题 | 硬证据 | 状态 |
|---|---|---|
| 金属声 / 沙哑 | 转换在 **6~8k 高 20 dB**、4~8k 平坦度 0.228→0.346；现有 deess 只压 2 dB | **未解** |
| 3~10k 转换噪声没有补偿 | 三份独立报告同向：8–12k 只剩 5.3%、12–16k≈0、有效带宽 5.7 vs 11.5 kHz | **未做** |
| 高频补偿对 48k 模型"纯亏" | 苹果嘉儿 48k：补 5500 把 4-8k 换掉，音色距 0.1855→0.2240 | auto 会 SKIP，但保守度未验 |
| 分离探针曾只看中段 | 病在 150 s 之后，探针固定取 `(total−30)/2`，后半段从没被评估 | 已改 40%+80% 两段，**未跑全量回归** |
| 八度错只报告不修 | README 明说"不会自动修"（重跑十几分钟，没有可靠自动判据前不动手） | 设计如此 |
| 越唱越差 | 漏进来的高音乐器被当人声唱（伴奏谱峰与人声轨完全重合） | 已修（换分离 + 否决权），**未跑全量回归** |

## 8. 优化清单（按杠杆排序）

| # | 改什么 | 预期收益 | 风险 | 怎么验 |
|---|---|---|---|---|
| 1 | **3~10k 按"长期平均频谱差"做平滑补偿**（不是硬砍高频）：先量原唱 vs 成品 LTAS，只在有缺口的频段补、有噪声的频段收，剂量封顶，补完复测，没变好不写文件 | 高（直击"沙哑/闷"） | 中（怕把音色补跑） | 复用 `hf_cli`/`presence_cli` 的"量→补→再量→回退"闭环 + 新增 gate |
| 2 | 分离探针从"总分排序"改成"**最漏段排序**"（同曲不同段 leak 差 39%） | 中高 | 中（否决权可能过严） | `verify_sep` 的 p90 敏感性判据锚真值 |
| 3 | 给唱歌补一把**外部尺子**：频谱直判基频 vs RMVPE 帧级记账，不一致就记 | 中高（终结 RMVPE 自证） | 低 | 常驻日志 + 一个只读 gate |
| 4 | 把 `rms_mix_rate` / `protect` / `f0` / `chunk` 接进界面（现在只有 API 有） | 中（可调校） | 低 | `verify_sing` + `verify_frontend_static` |
| 5 | README/UI 默认值与代码对齐（已完成：34→65 关、f0 由 fcpe 改回 rmvpe） | 中（可信度） | 低 | 人工核对 + `verify_frontend_static` |

## 9. 这次改了什么

- README：门禁数 34 → **65**（两处）；`--f0-method` 由 **fcpe** 改回 **rmvpe**，并补上 2026-10-17 的实测依据。
- M6 模型：六个角色全部装好并注册（见下节）。
- **新增 `stages/ltas_mend.py`**（3~10k 转换噪声按原唱频谱补偿），已接进 `sing_role`：
  位置在**唱后微调之后、非歌声段保护之前**（deess/deharsh 还会动 4~10k，放前面会被覆盖）；
  常量 `LTAS_MEND_MODE="auto"`，`post_skip` 里给 `ltas` 可单独关掉。
- 市场 UI：新增排序下拉（推荐 / 喜欢最多 / 下载最多 / 最新 / 体积最小）与
  「小马宝莉 M6」固定筛选 chip —— 纯前端，不发新请求、不改服务端顺序。
- **修 `_start_server.py` 的编码 bug**：它把子进程 stdout **重定向到文件**，Python 于是
  按 locale（中文 Windows = cp936）编码；项目日志文案里满是 `⇒` / `×` / `「」` 这类
  GBK 编不出来的字符 ⇒ 任务线程当场 `UnicodeEncodeError`，整首歌失败（2026-10-27 实测
  复现，症状是「唱前微调」阶段 33 秒就 failed，错误里只有一句 gbk can't encode）。
  修法：拉起子进程时显式给 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8`。
  ★ 走 `启动VoiceStudio.bat` 时 stdout 是控制台（Python 用 Unicode 控制台 API），
  所以那条路一直没暴露这个坑 —— 换句话说：**只有用 `_start_server.py` 重启才会踩**。
- **补上产品里原本没有的「角色重命名」接口**（`POST /api/role/<rid>/rename`，body `{name}`，≤24 字）。
  为什么需要：市场批量装模型时同一个角色会被装成好几张卡（实测 3 个「苹果嘉儿」来自
  同一个 MusicBox27 仓库的不同权重），用户既分辨不出哪张是哪张，也没法给自建角色起名。
  只改显示名，不碰权重/索引/素材。

### 9.1 ltas_mend 的实测依据（2026-10-27，珍奇唱 Welcome To New York，pitch −4）

对同一首、同一变调的链路**中间产物**逐段量「3~10k 各带相对原唱的平均差」：

| 阶段 | 各带平均差 (dB) |
|---|---|
| RVC 原始输出 | **10.44** |
| + 高频补偿 | 6.88 |
| + 去金属声 | 6.65 |
| + 临场感补偿 | 4.97 |
| + 唱后微调 | **3.09** |
| **+ 本次新增的 3~10k 补偿** | **2.19**（最大剂量仅 2.39 dB） |

两个反直觉的结论，写下来免得后人重踩：

1. **这首歌的主症是「缺高频」（−10.4 dB），不是「高频过多」。** 报告里「6~8k 高 20 dB」
   出自另一首歌 / 另一个角色，不能当通则。所以任何固定 shelf 一定错 —— 必须**按原唱量**。
2. 已有链路（hf → presence → polish）已经把 10.4 dB 收到 3.09 dB，**新阶段只该做最后那条
   形状曲线**：剂量上限 6 dB（收）/ 3 dB（补）、带域滑动平均 3 带、两端余弦斜坡进出，
   并且复测没变好就**不写文件**（`LTAS_SKIP`，与 hf/presence 同一个约定）。

## 10. M6 唱歌模型选型（HF 为唯一可访问评分源；weights.gg 直连被封）

| 角色 | 选用（repo / 文件） | 热度 | 形态 |
|---|---|---|---|
| 紫悦 | `therealvul/RVCv2` · `TwilightSingingv2.1/TwilightSingingv2.pth` | repo 4 likes | RVC v2 + index |
| 云宝 | `therealvul/RVCv2` · `RainbowDashSingingv2/RainbowDashSingingv2.pth` | 同上 | RVC v2 + index |
| 碧琪 | `therealvul/RVCv2` · `PinkiePieSingingv2/PinkiePieSingingv2.pth` | 同上 | RVC v2 + index |
| 珍奇 | `therealvul/RVCv2` · `RaritySingingv2/RaritySingingv2.pth` | 同上 | RVC v2 + index |
| 苹果嘉儿 | `therealvul/RVCv2` · `ApplejackSingingv2/ApplejackSingingv2.pth` | 同上 | RVC v2 + index |
| 柔柔 | `therealvul/RVCv2` · `FluttershySingingv2/FluttershySingingv2.pth` | 同上 | RVC v2 + index |

为什么不是"likes 最高那个"：likes 最高的是 `therealvul/so-vits-svc-4.0`（155 likes），
但它是 **so-vits-svc 4.0 格式，本工作台的 RVC 链路跑不了**。RVC 格式里，
`MusicBox27/MyLittlePonyRVC`（6 likes）只有 3 个角色带 `Talking_Singing` 权重，
其余是说话向的 V2；`therealvul/RVCv2` 是**六角色齐全、且带 Singingv2 与配对索引**的那一套。

### 10.1 已装好并注册的角色（2026-10-27 18:33 装，18:34 备份，19:10 改名）

| 角色 | role id | 权重 | 索引 | 来源 |
|---|---|---|---|---|
| 紫悦 | `r83a844e1` | 54.9 MB | 178.7 MB | modelhub:therealvul/RVCv2 |
| 云宝 | `r628ade1b` | 54.9 MB | 167.9 MB | 同上 |
| 碧琪 | `r9ba60614` | 54.9 MB | 134.1 MB | 同上 |
| 珍奇 | `r395ccc46` | 54.9 MB | 73.7 MB | 同上 |
| 苹果嘉儿 | `r7cf2b27b` | 54.9 MB | 65.0 MB | 同上 |
| 柔柔 | `r4a1d8706` | 54.9 MB | 62.1 MB | 同上 |

装的时候走的是产品自己的 `install_model`：下载 → 每个 .pth 建一张角色卡 → 索引配对并重命名成
`<rid>_added_...` 口径 → `rvc_ckpt_check.py` 真 `torch.load` 一次 → 写中文名。
六张卡全部"索引已对上"。

**重复角色已改名区分**（原本 4 张卡都叫「苹果嘉儿」，界面上分不出谁是谁）：
`紫悦（旧）`= rb37829be、`珍奇（旧）`= rd36e14ba、`柔柔（旧）`= r902459f7、
`苹果嘉儿（旧A/旧B/旧C）`= re2550d52 / r64b149c3 / r576320f5 —— 旧的都是 MusicBox27 那套，
留作 A/B 对照；新的六个用原名。备份：`roles.json.bak-before-m6-20261027_1834`、
`roles.json.bak-after-m6-20261027_1910`。

### 10.2 模型级 A/B（同一首歌、同一变调 −4、同一套参数）

| | 旧（MusicBox27 珍奇） | 新（therealvul RaritySingingv2） |
|---|---|---|
| 音高体检结论 | HNR 比原唱高 14.1 dB（会电） | HNR 比原唱高 14.4 dB（同样会电） |
| 高频补偿 | — | 9,000 Hz |
| 去金属声 | — | ΔHNR 11.5 → 5.5（未达 4.0） |
| 临场感 | — | +5.2 dB |
| 整曲音准吻合 | 96.4%（另一版 89%） | **96.1%** |
| 八度跳 | 1.2% | **1.2%** |
| LTAS 3~10k 各带平均差 | 3.09 dB（有 hf 档） | **2.78 dB（带新补偿）** |

⇒ 两个模型在**音准**上打平；差别在音色，这只能人耳判断（四轨试听台可直接对比
`data\roles\rd36e14ba\final\mix_sing_1027_170112.wav` 与
`data\roles\r395ccc46\final\mix_sing_1027_185912.wav`）。**金属声是这两个模型的共同短板**，
根源是"气声被抹掉"，不是参数没调好 —— 项目自己的结论也是"换音区更近的角色才是根治"。

## 11. 第二套引擎：so-vits-svc 4.0（进行中）

### 11.1 为什么选它、为什么不是别的

- M6 那套 **likes=155** 的权重（`therealvul/so-vits-svc-4.0`）是 so-vits-svc 4.0 格式，
  RVC 链路跑不了；用户要求"两套并存"，所以把它作为第二个引擎接进来。
- **原版 `svc-develop-team/so-vits-svc` 仓库已归档**，且推理要 `fairseq`（Windows + py3.12 装不上）。
  所以走**维护中的 fork `voicepaw/so-vits-svc-fork`**（9328 stars，2026-10-27 仍在推送，
  PyPI 4.2.30）；其 README 明确：「Based on branch 4.0 (v1) … the models are compatible，
  **4.1 models are not supported**」。我们这套正好是 4.0。
- 依赖是完整 GUI 栈（torch≥2.8 / torchaudio / transformers≥4.56 / lightning / pyworld /
  praat-parselmouth / torchcrepe / fastapi / PySimpleGUI …）⇒ **必须独立 venv**，不能塞进
  RVC 或 GPT-SoVITS 的 runtime（那两套 torch 版本不同，混装必崩）。

### 11.2 成本（实测数字，不是估）

| 项 | 数量 |
|---|---|
| M6 权重（每个角色 G_ 517.6 MB + D_ 535.1 MB + kmeans + config） | **≈ 1.05 GB/角色，六个 6.3 GB** |
| 独立环境 | py3.10.21 standalone（`D:\AHC\uv-python` 已有）+ `D:\AHC\svc-fork-venv` |
| 新增代码 | `stages/svc_cli.py`（子进程入口）+ `studio.py` engine 分流 + 一个门禁 |
| 磁盘 | D 盘 145 GB 可用，够 |

### 11.3 推进顺序

1. 建环境（py3.10 + torch cu128 + `so-vits-svc-fork`）；
2. 先只下**紫悦**一套（1.05 GB）跑通单角色推理 —— 跑不通就不扩，避免白下 6.3 GB；
3. 单角色跑通 → 扩到六个 → 注册成角色（与 RVC 同一套 roles.json 结构，engine 字段区分）；
4. 接进 `sing_role`：`engine="svc"` 时用它出干声，其余后处理（去金属/临场感/3~10k 补偿/保护/混音）
   **全部复用**，这样两套引擎的成品质量下限一致；
5. 加门禁：`verify_svc.py`（含负对照：模型/配置缺失必须硬失败，不许静默退回 RVC）。

### 11.4 已知风险

- HF 镜像**限流**（实测 429）⇒ 下载脚本必须带退避 + 断点续传，不能一把梭。
- fork 在 Windows 上 `pyworld` / `praat-parselmouth` 可能需要编译器；装不上就先只做推理路径
  （pitch 用 torchcrepe），并在文档里写清缺哪一块。
- 4.0 模型与 fork 的兼容性**必须在真机上实测**，不能只信 README。

### 11.5 实测结果（2026-10-27 20:3x，单角色已验证）

**环境（省了 2.4 GB 下载）**：不装 fork 要求的 torch≥2.8，而是新建 py3.10 venv 后用 `.pth`
**复用** YingMusic 那套 `torch 2.7.1+cu128`（CUDA 可用），再 `pip install --no-deps
so-vits-svc-fork==4.2.30` + 补 5 个小包（praat-parselmouth / pyworld / pebble / sounddevice /
cm-time）。导入通过，版本不符只是警告。

**两个必须的环境开关**（踩过才知道）：
| 开关 | 不加会怎样 |
|---|---|
| `TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1` | torch≥2.6 的 `weights_only=True` 踢掉 kmeans 的 numpy pickle ⇒ `UnpicklingError`，模型加载都过不去 |
| `HF_ENDPOINT=https://hf-mirror.com` | HuBERT（`content-vec-best`）走 transformers 下载，不指镜像就卡住 |

**推理**：`stages/svc_cli.py` 封装。紫悦单角色 15 秒素材实测 `SVC_OK secs=15.00`，
输出 44.1 kHz、峰值 0.902；负对照（缺权重）正确给出 `SVC_FAIL`。
**4–12 kHz 能量占比 0.024（输入 0.134）** ⇒ SVC 和 RVC 一样"缺高频"，所以后处理链
（高频补偿/去金属/临场感/3~10k 补偿/保护/混音）**整套复用**，两套引擎的质量下限一致。

**产品侧已接好**：`vs.py sing <rid> --engine svc` 全链跑通（313 秒，步骤条显示
「歌声转换（SVC）」→ 高频补偿 → 去金属 → 临场感 → 唱后微调 → 3~10k 补偿 → 保护 → 混音 → 质检）。
SVC 引擎**跳过音高体检**（那几步内部是 RVC 推理，对 SVC 没意义、纯属白烧显卡）。

**已发现的问题（下一步）**：SVC 的系统性移调偏移 —— 第一次 `--pitch 0` 实测输出比原唱低
**5.7 个半音**（音准吻合 46%、轮廓相关 +0.18）。这是 so-vits-svc 的固有特性（模型音区与
原唱音区不同，必须移调），不是 bug；RVC 那条链用 `estimate_pitch` 自动算，而市场角色没有
参考音频 ⇒ 自动估计拿不到。正在用 `--pitch 6` 复测；若确认是固定偏移，就在
`svc_convert` 里加"角色级音高校准"字段（一次测出、存进 role["svc"]["semitones"]）。

**门禁**：新增 `verify_svc.py`（静态接线 + 真跑一次 + 三个负对照 + 产品侧"没挂 SVC 必须报错"），
已加入 `_run_regression.sh` 的 `ALL`（65 → **66 关**）。

### 11.6 关键发现：`-na` 才是移调生效的开关（附 f0 方法实测）

第一次 `--pitch 6` 复测**依然低 5.4 个半音**（和 `--pitch 0` 的 −5.7 在噪声内）⇒ 说明**移调
根本没生效**，不是"固定偏移需要补偿"。根因在 fork 的默认值：

> `infer` 默认 `--auto-predict-f0`（= `-a`）。这个档位下 f0 由**模型自己预测**，
> `-t/--transpose` 被直接忽略。必须传 `-na`（跟随输入音高）变调才真的算数。

改 `-na` 后同一段 15 秒素材（`stages/qc_cli.py` 实测，lag=0）：

| 配置 | 平移 | 吻合 ±50 | 宽松 ±150 | 八度错 | 轮廓相关 | 起伏比 |
|---|---|---|---|---|---|---|
| 默认（auto，`-a`） | **−5.06 半音** | 39.0% | 59.8% | 18.4% | +0.255 | 0.58 |
| `-na` + dio | −0.06 | 55.1% | 62.3% | 26.8% | +0.498 | 2.42 |
| `-na` + crepe | +0.04 | 59.3% | 63.2% | 29.6% | +0.058 | 0.24 |
| **`-na` + harvest（选它当默认）** | −0.02 | **66.1%** | **71.0%** | **20.3%** | **+0.589** | 2.36 |

⇒ 四条一起看：**`-na` 解决的是"系统性偏低"，harvest 解决的是"跟得准不准"**。
两者都改完，吻合从 39% 抬到 66%。

**但必须说实话**：SVC 的**八度错 20%** 远高于 RVC 那条链的 1–2%，起伏比 2.36（原唱 1.0）
说明它的音高曲线更"野"。所以现状是：**两套并存、RVC 仍是默认**；SVC 适合"要那个音色"的
场合，音准要靠后处理/换更干净的模型再压。这条写进文档，避免以后有人以为 SVC 是升级。

### 11.7 当前完成度

| 项 | 状态 |
|---|---|
| 环境（py3.10 + 复用 torch + fork 4.2.30） | ✔ |
| `stages/svc_cli.py`（含 `-na` / harvest 默认 / 长度校验 / 负对照） | ✔ |
| `studio.svc_convert` + `sing_role` 引擎分流 + 共用显卡闸门 | ✔ |
| `vs.py --engine svc` | ✔ |
| `verify_svc.py` 门禁（14 项，含 3 个负对照） | ✔ 全过 |
| `verify_sing.py`（RVC 主链，改动后复跑） | ✔ 41/0 |
| SVC 权重：紫悦 / 云宝 / 珍奇 / 苹果嘉儿 / 柔柔 / 碧琪 | ✔ 6/6 下完并挂到角色 |
| 产品级 SVC 成品（harvest + `-na`） | ✔ 整曲音准吻合 **75.2%**、八度跳 10.2%（修之前 49.6%） |
| SVC 试听小样（`--preview-secs`，只跑一小段） | ✔ 断点修好：`_sing_preview` 原来也硬性要求 RVC 权重，SVC 卡会被拒；现在按 engine 分流（碧琪 SVC 10 秒小样 54 秒跑完） |

### 11.8 角色卡按引擎拆开（用户 2026-10-27 要求）

用户要求："下载的 svc 和你刚找到的 rvc 模型，单独改名成角色名 + rvc 或者 svc"。
⇒ 一张卡只代表一个引擎，名字带后缀：

| 卡 | 挂什么 | 状态 |
|---|---|---|
| `紫悦 RVC` / `云宝 RVC` / `碧琪 RVC` / `珍奇 RVC` / `苹果嘉儿 RVC` / `柔柔 RVC` | 只挂 RVC 权重（`role["rvc"]`） | ✔ 6 张 |
| `紫悦 SVC` / `云宝 SVC` | 只挂 SVC 权重（`role["svc"]`） | ✔ 2 张 |
| `碧琪 SVC` / `珍奇 SVC` / `苹果嘉儿 SVC` / `柔柔 SVC` | 同上 | ✔ 已建（权重下完后跑 `_tmp/split_engine_roles.py` 自动补齐） || `紫悦（旧）` / `珍奇（旧）` / `柔柔（旧）` / `苹果嘉儿（旧A/B/C）` | MusicBox27 那批，留作 A/B 对照 | ✔ |

配套改了两处代码（否则 SVC 卡点不动）：
1. `sing_role`：`rvc_model` 为空时**只有 engine≠svc 才报错** —— SVC 卡本来就没有 RVC 权重。
2. `sing_job`：engine=svc 时**跳过"没权重就先自动训"**分支 —— 不跳过的话，SVC 卡会被当成
   "缺模型要训练"，报出一句"这个角色只有 0 段干声（至少要 10 段）"，与真实原因完全无关
   （实测踩到，job 3 秒就 failed）。

★ 踩坑记录：改名脚本第一版**先走 API 改名、最后却用脚本开头读到的旧 `db` 覆盖写回**，
名字又被改回去了（API 明明返回 ok）。现在改成"db 里也改"，并单独留了一份
`roles.json.bak-rename2-*`。这类"读早了再写回去"的覆盖，在这个项目里已经踩过一次
（`mutate_role` 的注释写的就是同一件事）。

### 11.10 修「SVC 卡显示不能唱歌」（2026-10-27 用户反馈）

用户看到 SVC 卡在界面上被判成"不能唱歌"。三个原因叠在一起，全都修了：

| 原因 | 位置 | 修法 |
|---|---|---|
| 能力判定只看 `role["rvc"]` | `studio.role_ability` | 加 `has_svc` 分支：SVC 权重在 ⇒ `state=ready`、标签「可唱歌（SVC）」，`why` 里说明"引擎选 SVC 就能唱" |
| 界面上的"能不能唱"判定只看 `r.rvc` | `web/app.js` 十几处 | 加统一 `canSing(r)`（RVC **或** SVC 有其一），单人路径全部改用它 |
| **引擎下拉里根本没有 svc 选项** | `web/index.html` 两个 select | 加 `<option value="svc">so-vits-svc（第二引擎）</option>`，并在打开 SVC 卡时**自动选中** |

★ 一个关键取舍：**多人（对唱/叠唱）那条链路只走 RVC**（`sing_multi` 内部就是 RVC），
所以多人名单/拦截那 4 处**保留"必须 RVC"**，只有单人路径放宽到 `canSing`。
否则 SVC 卡会被放进对唱名单，点了才报错 —— 那是最差的一种交互。

验证：12 张引擎卡全部 `ability.sing.state = ready`（6 张「可唱歌」+ 6 张「可唱歌（SVC）」）；
`/static/app.js` 实际下发内容里 `canSing` 与 svc 默认值都在；`verify_frontend_static` **10/0**。

### 11.12 修「原曲里不是主唱唱的段也被唱了」（2026-10-27 用户反馈）

用户原话："原歌曲本来的一些段不是主唱唱的，但是还是唱了"。

**机制**：分离出来的人声轨是"所有人声"（主唱 + 伴唱 + 和声 + 垫唱），
RVC 只认 f0 曲线，分不出谁在唱 ⇒ 伴唱段也被角色唱一遍 ⇒ 叠起来就是
"很多人一起唱、发闷发哑"。

**判据**（新增 `stages/lead_guard.py`）：人声轨是立体声，主唱通常**居中**
（L/R 高相关、侧能量小），伴唱/和声通常**变宽**（去相关、侧能量大）。
但实测同一绝对阈值下：歌 A 圈 0.3%、歌 C 圈 **60.4%** ⇒ 绝对阈值一定翻车。
所以用**这首歌自己的中位数当基线**：

    判为「非主唱」= 相关 < 基线−0.35 且 侧/中 > 基线×1.8 且 侧/中 > 0.15
    持续 ≥0.6 秒，并掉 <0.4 秒的缝

三道闸门（宁可不动，也不许误伤主唱）：段要成片、至少 0.6 秒；圈出时长 >
有声时长 35% 或全曲 30% ⇒ **判据失效，整段撤销**并写清原因；人声轨是单声道
⇒ 直接跳过（没有立体声线索，绝不瞎判）。

**行为**：判出的段**换回原始人声**（伴唱还是原版伴唱，只是不再由角色唱），
边界 20ms 交叉淡化；质检的 skip 也会把这几段排除（它们没变调，算进音准会得出
"整首跑调"的假结论）。模式 `auto/keep/mute/off`，常量 `LEAD_GUARD_MODE="auto"`，
请求里传 `lead` 可覆盖。

**实测（Taylor Swift - Welcome To New York，紫悦 RVC）**：
- 判出 **12.4 秒 / 13 段**（这首歌人声轨基线：相关 0.47、侧比 0.60）；
- 保护段与原始人声的能量包络相对差 **0.586 → 0.000**（等于原声），
  其余段 0.343 → 0.349（基本不动 ⇒ 没有误伤）；
- 质检照旧 89% / 整曲 96.4%（那几段已被 skip 排除，不会拉低音准数）。

**门禁**：新增 `verify_lead.py` **13 项全过**（合成素材只圈伴唱段；单声道拒判；
整首都宽 ⇒ 一段不圈；keep 模式段内逐样本等于原声、其余段逐样本等于变声轨）。
`ALL` 66 → **67 关**，README 同步。

### 11.13 顺手修掉的两个"哑"的硬原因（2026-10-27）

| 问题 | 实测证据 | 修法 |
|---|---|---|
| 对唱混音**峰值 1.33 却写 PCM16** | 旧产物 `voice_multi_1027_220045.wav` 峰值 1.000、**35 个样点削顶**；stats 里 `peak: 1.33, peak_guard: false`（峰值保护只对叠唱生效，独唱分支没有） | `blend()` 两条分支都过 `_peak_guard`；单测：增益 4.0×尖峰 3.6 → 缩到 0.990 |
| 叠唱**每层各乘 0.55** | 8 个人 = 1+7×0.55 = **4.85 倍**叠加，只能整体下压 ⇒ 主唱被压扁 | 改成"整堆能量对齐"：所有层之和的 RMS = 主唱的 55%，人多人少都是 55%（单层与旧行为数值一致） |
| **和声设计永远退化成齐唱** | `stages/harmony_cli.py` 只在 stdout 打了 `ok:true`，写进 JSON 的 `rep["ok"]` 一直是 `False` ⇒ 产品每次判"没跑通"⇒ 所有人唱同一个音高（最糊） | `rep["ok"]=True`；实测同一首歌现在给出 `-4 / +3` 半音两个声部 |

### 11.14 SVC 引擎改「RVC 打底 + SVC 转音色」（2026-10-28）

用户反馈"口哨等等依旧在唱、非常杂乱吵闹"，一查最近几次单角色翻唱**用的都是 SVC 卡**。
同一段 15 秒素材、同一角色的实测对比：

| 引擎 | 音准吻合(±50) | 八度错 | 轮廓相关 | 起伏比 |
|---|---|---|---|---|
| 纯 SVC | 66.1% | **20.3%** | +0.589 | 2.36 |
| 纯 RVC | 91.8% | 2.1% | +0.921 | 1.01 |
| **RVC→SVC（现在默认）** | **87.9%** | **2.3%** | +0.853 | 0.76 |

⇒ 纯 SVC 的模型自己跟不准旋律（八度乱跳），这就是"杂乱"的根。
改法：SVC 卡上挂一个 `svc.rvc_base`（同角色的 RVC 权重 + 索引），
`svc_convert` 先用 RVC 把**音准**打好底，再把这条已经唱准的干声交给 SVC 转**音色** ——
两套引擎各干自己擅长的事，音准几乎不掉（八度错 20.3% → 2.3%）。
没挂打底权重时退回纯 SVC，并在日志里明说"音准会差一截"。

**自动引擎**：只有 SVC 权重的卡，不带 `--engine` 时自动走 svc（否则会被
"没 RVC 权重就先训"的分支拦下，报一句"这个角色只有 0 段干声"，与真实原因无关）。

### 11.15 非主唱段保护收紧（2026-10-28）

实测《Summer Vibe》一版圈了 **47.0 秒**（198 秒歌的 24%）原声 —— 太多，听感就是
"原唱插进来了"。收紧为：相关对比 0.35→**0.45**、侧比 1.8→**2.2**、最短段 0.6→**1.0 秒**、
上限 35%→**12%**（全曲 30%→10%）。收紧后 17 首歌的圈出量：

- 多数歌 **0 ~ 6 秒**；
- 《Summer Vibe》《8c2624…》这类"整首都宽"的直接 **reliable=False、0 段**（自动撤销）。

### 11.16 口哨漏检为什么不能简单放宽（2026-10-28 实测）

用户说"口哨还在被唱"。查 `nonvocal_guard` 在《Summer Vibe》上的表现：
平坦度判据本来能圈 **21.4 秒**，但"单分音"纯度判据筛掉 16.3 秒，最终只剩 **0.18 秒**。

但把这些候选帧拿去量，发现它们**不是口哨**：
f0 中位 327 Hz（口哨通常 >500 Hz，这里 **0% 的帧 >500 Hz**）、f0 抖动与正常歌唱一样，
连续段加起来只有 2.9 秒 —— 它们是**干净的高音延长**。
⇒ 纯度判据在这里是对的，放宽就会重演"少声音"事故（保护 35 秒、角色嗓子消失）。
**结论：口哨要走"高基频 + 稳定"这条独立证据**，不能靠放松平坦度。已确认
`我们的雷霆` 那首歌里有 7 秒 f0>450 Hz 的段（其中 2 秒 >550 Hz）—— 这条线索留作下一步。

### 11.17 口哨专用保护（2026-10-28，已实现）

新增 `stages/whistle_guard.py`，四条判据**同时成立**才算口哨：

| 判据 | 阈值 | 为什么 |
|---|---|---|
| ① 基频高 | 主峰 550~3000 Hz | 实测那批"高音延长"只有 490~520 Hz，天然分开 |
| ② **无二次谐波** | h2/h1 < 0.05 | 口哨是一根正弦；假声/高音一定有二次谐波 —— 这是最硬的一条 |
| ③ 够纯 | 平坦度 <0.06、主峰占比 >0.55、2.5k 以上 <0.02 | 排除宽带噪声 |
| ④ 够稳 | 段内主频 std < 25 音分 | 口哨极稳，颤音/滑音更抖 |

外加三条安全闸门：段长 **0.2~3.0 秒**、总量 ≤ 有声时长 **6%** 且 ≤ 全曲 **5%**（超了判
"不可靠"，一段都不动）。三种模式 `auto/keep/mute/off`，与另外两个 guard 同一套语义；
质检 skip 同样合并这几段。

**实测（合成 + 真实素材）**：

| 素材 | 结果 |
|---|---|
| 900 Hz 纯音（口哨） | ✔ 圈出 2.00~3.15 s |
| 500 Hz + 颤音 + 二次谐波（假声） | ✔ 0 段（不误伤） |
| 900 Hz 但带强二次谐波（像合成器 lead） | ✔ 0 段 |
| 真实歌曲 ×3（无口哨） | ✔ 0 段（零误报） |

**门禁**：`verify_whistle.py` **14 项全过**（含四条负对照 + keep 模式逐样本校验）；
`ALL` 67 → **68 关**，README 同步。

⚠ 一个必须说清的事实：在**目前这三首真实歌的人声轨里，检测器一段口哨都没找到** ——
所有 >450 Hz 的段都是高音/假声（f0 490~520 Hz、平坦度 0.14~0.27、有明显谐波）。
所以如果你还听到"口哨在唱"，很可能：① 口哨在**伴奏轨**里（那轨本来就不变声，听到的是原版）；
② 你指的是**气声/杂音/破音**而不是口哨；③ 是另一首歌。需要具体歌名 + 大致时间点才能定位。

**产品级验证（同一首歌）**：步骤条出现「口哨专用保护（高基频纯音）→ 没找到口哨段
（正常结果）」，整曲音准吻合 **94.6% / 八度跳 1.1%**（SVC 卡，RVC 打底生效），
`verify_sing` 复跑 **41/0**。跳过时不再误报 `[warn] 没跑通`（与 hf/presence 同一约定）。

### 11.18 伴唱优先分离（karaoke 模型，2026-10-28，用户选的方案）

用户原话：「前面的 1 2 3 4 Eh-oh, eh-oh, eh-oh bop bop away-o 还是唱了，导致特别嘈杂」
—— 那是**群声/口播**，和主唱一起躺在那条人声轨里，能量/音高/宽度都分不开。

**装了什么**：`anvuew/karaoke_bs_roformer`（195MB，bs_roformer，`instruments=['Vocals','Instrumental']`，
在卡拉OK素材上训 ⇒ instrumental 侧保着伴唱）。注册进 `stages/msst_cli.py` 的
`MSST_MODELS`；**不进自动候选池**（目标函数不同），由 `sep_model="karaoke"` 显式触发，
`_karaoke_model()` 自动挑已下好的那个。界面新增「分离方式：标准 / 伴唱优先」。

**实测《Summer Vibe》（同一首歌，标准 vs 伴唱优先）**：

| 指标 | 标准分离 | 伴唱优先 |
|---|---|---|
| 人声轨侧/中（宽度，越大越像有垫唱） | 0.451 | **0.029** |
| 伴奏轨人声频段(200–4k)占比 | 0.436 | **0.636（+46%）** |
| 开头 0–12s「1 2 3 4 / Eh-oh…」人声 RMS | 0.1038（会被角色唱） | **0.0067（降 15 倍，归到伴奏）** |
| 主歌 60–72s 人声 RMS（主唱还在吗） | 0.1180 | 0.0871 ✓ |

端到端（柔柔 RVC + 伴唱优先）：音高体检八度错 **0.1%**；整曲音准 **98.3%**、
八度跳 **0.4%**、起伏比 1.00（对比：标准分离 + RVC 是 95.3%/2.1%；SVC 卡 82%）。

**门禁**：`verify_karaoke.py` **11 项全过**（静态接线 5 + 模型就绪 2 + 真跑 20 秒素材 4，
含"人声轨比输入更居中"这条承重性质）。`ALL` 68 → **69 关**，README 同步。

### 11.11 修「合唱里 SVC 卡显示缺模型」（2026-10-27 用户反馈）

用户把 SVC 卡加进对唱名单，界面标「缺模型」。这是上一步**故意留的边界**（多人只走 RVC），
但用户要的就是"两套都能合唱" ⇒ 把 `sing_multi` 也做成**按每张卡自己的引擎分流**：

| 改动 | 位置 |
|---|---|
| 校验：有 RVC **或** SVC 权重即可进合唱 | `studio.sing_multi` 的 roles 组装 |
| 转换：`svc` 卡走 `svc_convert`，其余走 `rvc_convert`（**不再先拿空模型调一次**） | 同上，歌声转换那一步 |
| 索引：`_find_rvc_index` 只在有 RVC 权重时调用 | 同上 |
| 前端：多人名单/缺模型标记/拦截文案改成 `canSing`，引擎选择框的提示改成"每个角色按自己卡上的引擎唱" | `web/app.js` |

边界仍然保留：**和声/叠唱（chorus）里所有人的后处理完全相同**（高频/去金属/临场感/
唱后微调/3~10k 补偿/非歌声保护/混音），所以两套引擎混着唱也不会出现"某个人的成品没修"。

验证：`紫悦 RVC` + `紫悦 SVC` 同一首歌的混合对唱任务跑通（见任务日志里
「歌声转换 1/2」走 RVC、「歌声转换 2/2」走 SVC，日志里有 `SVC_OK`）。

### 11.9 同一角色两套引擎的 A/B（紫悦，Welcome To New York，pitch 0）

| 指标 | `紫悦 RVC` | `紫悦 SVC` |
|---|---|---|
| 整曲音准吻合 | **96.4%** | 75.0% |
| 八度跳 | **1.2%** | 10.2% |
| 音高起伏比 | **1.01** | 1.34 |
| 单窗吻合（宽松） | **92%（96%）** | 60%（74%） |
| 4–8 kHz 能量占比 | 0.0188 | **0.0292** |
| 8–12 kHz 能量占比 | **0.0248** | 0.0178 |

⇒ 结论很干净：**音准 RVC 完胜**（八度跳 1.2% vs 10.2%，这正是"飘、像两个人在唱"的来源）；
音色分布上 SVC 的 4–8k 更多、8–12k 更少，听着会比 RVC 更"厚/闷"一点。
所以：**RVC 保持默认**，SVC 作为"想要那套 so-vits 音色"时的第二选择 —— 这两套并存是
产品能力，不是二选一的替代。

## 12. 变更清单（2026-10-27）

| 文件 | 类型 | 改了什么 |
|---|---|---|
| `stages/ltas_mend.py` | 新增 | 3~10k 按原唱长期平均频谱补偿（量→补→复测→不达标不写文件） |
| `stages/svc_cli.py` | 新增 | so-vits-svc 4.0 第二引擎入口（含 `-na`、harvest 默认、长度校验、SVC_OK/FAIL 约定） |
| `verify_svc.py` | 新增 | 第二引擎门禁（14 项：静态接线 + 真跑 + 3 个负对照 + 产品侧报错） |
| `_VoiceStudio_理解与优化_20261027.md` | 新增 | 本文件 |
| `studio.py` | 改 | ①`ltas_mend` 助手 + 接进 `sing_role`（唱后微调之后）②`svc_convert` + 引擎分流 ③`sing_job`/`_sing_preview` 的 SVC 守卫 ④`sing_role` 不再强制 RVC 权重 |
| `server.py` | 改 | 新增角色重命名接口 `/api/role/<rid>/rename` |
| `vs.py` | 改 | `--engine` 白名单加 `svc` |
| `_start_server.py` | 改 | 子进程显式 UTF-8（`PYTHONUTF8`/`PYTHONIOENCODING`），修"日志里的 ⇒ 打挂任务线程" |
| `web/mkt.js`、`web/index.html` | 改 | 市场排序下拉（喜欢/下载/最新/体积）+ 「小马宝莉 M6」固定筛选 chip |
| `_run_regression.sh` | 改 | `ALL` 加入 `verify_svc`（65 → 66 关） |
| `README.md` | 改 | 门禁数 34→66；f0 `fcpe`→`rmvpe`（附实测）；补真实 13 步链路与顺序约束；能力表加「第二引擎（SVC）」行 |
| `data/roles.json` | 数据 | 6 张 `<角色> RVC` + 6 张 `<角色> SVC` + 旧卡改名；共 19 个角色；每次改动前都有 `.bak-*` |

**门禁读数（本次实测）**：`verify_sing` 41/0（两次，含所有改动后）、`verify_svc` 14/0、
`verify_apierr` 22/0、`verify_frontend_static` 10/0、`verify_market_ui` 32/1
（唯一红的是「合集卡片有装这个」——它搜 `Alastor` 现在返回单包仓库，属于 HF 数据变化，
`mkt.js` 里的功能代码仍在，与本次改动无关）。
