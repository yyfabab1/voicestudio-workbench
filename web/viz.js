/* 做视频：把成品（或任意音频）渲染成能直接发出去的 MP4。
 *
 * 和 loc.js / mkt.js 一样是独立一块，只依赖 app.js 里的 $ / esc / api / post / toast。
 *
 * 两条入口：
 *   ① 成品卡片上的「🎬 做视频」→ 预选那一条，滚到这里；
 *   ② 直接在这里挑音频（成品或本地曲库里的歌）。
 * 提交后走项目既有的任务机制（右下角任务面板有进度、失败有原因），
 * 做完自动刷新下面的视频列表。
 */
let VIZ_OPT = null;          // /api/viz/options 的结果
let VIZ_STYLE = "orbit";     // 当前选中的样式（默认手绘那套里最出片的）
let VIZ_BUSY = false;        // 正在提交（防连点）
let VIZ_STYLE_MAP = {};      // key -> style（拿 note 用）
let VIZ_MODES = {};          // key -> mode（画面来源）
let VIZ_VIDEO = "";          // 用我的视频当画面时，上传后落在服务端的路径
let VIZ_BG = { kind: "grad", color: "#101b2e", image: "" };   // 画面背景
let VIZ_BG_IMG = "";         // 上传后的背景图路径
let VIZ_LAST = null;         // 上一次成功提交的设置（「重出视频」照着它复原）

const VIZ_GROUPS = [
  ["draw", "精美 · 逐帧手绘", "画面最讲究，但要等几分钟"],
  ["fast", "快速 · 滤镜合成", "几秒钟出片，画面朴素些"],
  // AE 那一组：没装 AE 时后端根本不下发样式，`all.filter()` 会得到空数组，
  // 分组标题自动不渲染 —— 不需要前端再判断一遍。
  ["ae", "AE · 真渲染", "用本机 After Effects 出片，AE 要开着"],
];

/* ★ 合唱族判定：一律走后端给的 `needs_cast` 标记，**不许写 `key === "cast"`**。
 *
 * 合唱台不是一款样式，是一族构型（横排 / 对唱擂台 / 环列 / 舞台追光 …）。
 * 之前界面上有 5 处写死 `VIZ_STYLE === "cast"`，后果是每加一款新构型，
 * 它就静默少一份能力：选项面板不弹、字幕表不刷、提交时不带画面选项 ——
 * 不报错，只是"好像没生效"。标记由 /api/viz/options 一起下发。 */
function vizIsCast(k) {
  const key = (k === undefined || k === null) ? VIZ_STYLE : k;
  const s = VIZ_STYLE_MAP[key];
  return !!(s && s.needs_cast);
}

/* 合唱族里所有构型的 key，按样式表里的顺序（用来给多人成品自动挑一款）。 */
function vizCastKeys() {
  return Object.keys(VIZ_STYLE_MAP).filter((k) => vizIsCast(k));
}

/* 音频下拉里两种条目：
 *   out:<角色id>:<成品下标>  —— 角色的一条成品（自带 .lrc 时会有歌词）
 *   <绝对路径>               —— 本地曲库 / 上传的歌
 * 用前缀区分是因为"第 3 条成品"和"某个文件路径"在这一个 select 里得共存。
 */
const VIZ_OUT = "out:";

async function vizInit() {
  const stylesBox = $("#vizStyles");
  if (!stylesBox) return;
  try {
    VIZ_OPT = await api("/api/viz/options");
  } catch (e) {
    stylesBox.innerHTML =
      `<div class="viz-empty">读不到可用样式：${esc(e.message || e)}</div>`;
    return;
  }

  const note = $("#vizNote");
  if (!VIZ_OPT.ok) {
    // 这台机器没有能用的编码器 —— 照实说，并把每个候选"为什么不行"列出来，
    // 用户（或下一个接手的人）才有办法排查。
    note.className = "viz-note bad";
    note.textContent = "⚠ " + VIZ_OPT.reason;
    stylesBox.innerHTML = (VIZ_OPT.tested || []).map((t) => `
      <div class="viz-st" style="cursor:default">
        <div class="n">${esc(t.enc)}</div>
        <div class="d">${esc(t.why || "不可用")}</div>
      </div>`).join("");
    $("#btnVizGo").disabled = true;
    return;
  }
  // 样式卡：按引擎分两组。默认落在手绘那组 —— 用户抱怨的是"太难看"，
  // 一进来就该看到好看的那几个，而不是老的黑底频谱。
  const all = VIZ_OPT.styles || [];
  const known = {}; all.forEach((s) => { known[s.key] = s; });
  if (!known[VIZ_STYLE] && all.length) VIZ_STYLE = all[0].key;
  VIZ_STYLE_MAP = known;
  // data-i 是**全局序号**（跨分组连续）。分组标题也是 #vizStyles 的子元素，
  // 所以 `:nth-child(n)` 从此对不上样式卡了 —— 验收脚本要靠这个属性定位。
  let gi = 0;
  stylesBox.innerHTML = VIZ_GROUPS.map(([g, title, hint]) => {
    const list = all.filter((s) => s.group === g);
    if (!list.length) return "";
    return `<div class="viz-grp"><span>${esc(title)}</span><em>${esc(hint)}</em></div>` +
      list.map((s) => {
        const i = gi++;
        return `
      <div class="viz-st${s.key === VIZ_STYLE ? " on" : ""}" data-k="${esc(s.key)}" data-i="${i}">
        <div class="n">${esc(s.name)}${s.tag ? ` <em>${esc(s.tag)}</em>` : ""}</div>
        <div class="d">${esc(s.desc)}</div>
      </div>`;
      }).join("");
  }).join("");
  stylesBox.querySelectorAll(".viz-st").forEach((el) => {
    el.onclick = () => vizPickStyle(el.dataset.k);
  });

  // 画面来源：三条路，默认"生成可视化画面"（和以前一样）
  const modes = VIZ_OPT.modes || [];
  VIZ_MODES = {};
  modes.forEach((m) => { VIZ_MODES[m.key] = m; });
  const ms = $("#vizMode");
  if (ms) {
    ms.innerHTML = modes.map((m) =>
      `<option value="${esc(m.key)}" title="${esc(m.desc)}">${esc(m.name)}</option>`).join("");
    ms.value = "viz";
  }

  // 画幅 / 帧率
  $("#vizSize").innerHTML = (VIZ_OPT.sizes || []).map((s) =>
    `<option value="${esc(s.key)}" title="${esc(s.desc)}">${esc(s.name)} · ${esc(s.desc)}</option>`).join("");
  $("#vizFps").innerHTML = (VIZ_OPT.fps || [30]).map((f) =>
    `<option value="${f}">${f} fps${f === 30 ? "（快）" : "（更顺）"}</option>`).join("");

  await vizSrcFill();
  const srcSel = $("#vizSrc");
  // 手动换音频时也要重读字幕表（vizSrcFill 只在程序里换值时触发刷新）
  if (srcSel) srcSel.addEventListener("change", () => vizSubsRefresh());
  vizModeChange();
  vizBgChange();
  vizListLoad();
}

/* 「背景」那一条：选纯色就给取色器，选图片就给选图按钮。
 * ★ 只换音轨（mux）时整条收起来 —— 画面是用户自己的，换背景没有意义。 */
function vizBgChange() {
  const sel = $("#vizBg");
  const kind = (sel && sel.value) || "grad";
  const col = $("#vizBgColor");
  const btn = $("#btnVizBgImg");
  if (col) col.style.display = kind === "color" ? "" : "none";
  if (btn) btn.style.display = kind === "image" ? "" : "none";
  VIZ_BG.kind = kind;
  VIZ_BG.color = (col && col.value) || VIZ_BG.color || "#101b2e";
  VIZ_BG.image = kind === "image" ? VIZ_BG_IMG : "";
  vizNoteRefresh();
}

/* 合唱台那几个开关只在「合唱族」+ 生成画面时才有意义 */
function vizCastOptShow() {
  const box = $("#vizCastOpt");
  if (!box) return;
  const mode = ($("#vizMode") && $("#vizMode").value) || "viz";
  box.style.display = (vizIsCast() && mode !== "mux") ? "" : "none";
  const look = $("#vizLook");
  if (look) look.style.display = (mode === "mux") ? "none" : "";
}

function vizPickBgImage() {
  let inp = $("#vizBgPicker");
  if (!inp) {
    inp = document.createElement("input");
    inp.type = "file";
    inp.id = "vizBgPicker";
    inp.accept = "image/*,.png,.jpg,.jpeg,.webp";
    inp.style.display = "none";
    inp.addEventListener("change", async () => {
      const f = (inp.files || [])[0];
      if (f) await vizBgUpload(f);
      inp.value = "";
    });
    document.body.appendChild(inp);
  }
  inp.click();
}

async function vizBgUpload(f) {
  toast("上传中…");
  try {
    const r = await fetch("/api/upload?keep=1&name=" + encodeURIComponent(f.name),
                          { method: "POST", body: f });
    const j = await r.json();
    if (!r.ok || !j.path) throw new Error(j.error || "上传失败");
    VIZ_BG_IMG = j.path;
    VIZ_BG.image = j.path;
    const nm = $("#vizBgName");
    if (nm) nm.textContent = "已选：" + f.name;
    vizNoteRefresh();
    toast("背景图已选好", "good");
  } catch (e) {
    toast(e.message || "上传失败", "bad");
  }
}

/* 切「画面来源」：要不要选视频、还要不要挑样式，都跟着变。
 * ★ 「只换音轨」这条路没有样式可言（画面是用户自己的），把样式区收起来，
 *   不然用户会以为选了样式就会作用到他的视频上 —— 那是骗人。 */
function vizModeChange() {
  const sel = $("#vizMode");
  if (!sel) return;
  const mode = sel.value || "viz";
  const need = !!(VIZ_MODES[mode] || {}).needVideo;
  if ($("#btnVizVideo")) $("#btnVizVideo").style.display = need ? "" : "none";
  const nm = $("#vizVideoName");
  if (nm) {
    nm.style.display = need ? "" : "none";
    if (!need) nm.textContent = "";
  }
  if (!need) VIZ_VIDEO = "";
  const box = $("#vizStyles");
  if (box) box.style.display = (mode === "mux") ? "none" : "";
  // 只换音轨时画面是用户自己的，谈不上"字幕表" —— 收起，别让人以为改了会生效
  if (mode === "mux") {
    VIZ_SUBS = { role: "", index: -1, rows: [] };
    vizSubsShow(false);
  }
  vizCastOptShow();
  vizNoteRefresh();
}

/* 选一段自己的视频当画面。
 * ★ 这里**不能**复用 app.js 的 #filePicker：那个 change 监听会按 pickerMode
 *   把文件送去「拆解/上传」，服务端一见视频就当场抽音轨 —— 画面正好被丢掉。
 *   所以自己建一个 input，并且上传时带 keep=1（服务端据此原样保留视频）。 */
function vizPickVideo() {
  let inp = $("#vizVideoPicker");
  if (!inp) {
    inp = document.createElement("input");
    inp.type = "file";
    inp.id = "vizVideoPicker";
    inp.accept = "video/*,.mp4,.mov,.mkv,.avi,.webm";
    inp.style.display = "none";
    inp.addEventListener("change", async () => {
      const f = (inp.files || [])[0];
      if (f) await vizVideoUpload(f);
      inp.value = "";
    });
    document.body.appendChild(inp);
  }
  inp.click();
}

async function vizVideoUpload(f) {
  toast("上传中…");
  try {
    const r = await fetch("/api/upload?keep=1&name=" + encodeURIComponent(f.name),
                          { method: "POST", body: f });
    const j = await r.json();
    if (!r.ok || !j.path) throw new Error(j.error || "上传失败");
    VIZ_VIDEO = j.path;
    const nm = $("#vizVideoName");
    if (nm) nm.textContent = "已选：" + f.name;
    vizNoteRefresh();
    toast("已选好视频：" + f.name, "good");
  } catch (e) {
    toast(e.message || "上传失败", "bad");
  }
}

function vizPickStyle(k) {
  VIZ_STYLE = k;
  document.querySelectorAll("#vizStyles .viz-st").forEach((el) => {
    el.classList.toggle("on", el.dataset.k === k);
  });
  vizNoteRefresh();
  vizSubsRefresh();
  vizCastOptShow();
}

/* ------------------------------------------------------------ 字幕表
 * 老板的原话：「我应该可以自己输入文字 修改视频的文字 英语的下面是小的中文」。
 * 所以这里给一张表：每句一行，左边时间、中间那句大字（英语）、右边小号中文。
 * 两格都能改；「自动填中文」只补还空着的格子（保住手改过的那些）。
 *
 * ★ 只对「合唱台」+ 多角色成品出现：别的样式是按 .lrc 烧字幕的，没有"每句是谁"，
 *   给一张表反而让人以为改了会生效。
 */
let VIZ_SUBS = { role: "", index: -1, src: "", rows: [] };

function vizSubsShow(on) {
  const box = $("#vizSubs");
  if (box) box.style.display = on ? "" : "none";
}

function fmtSubT(sec) {
  const s = Math.max(0, Number(sec) || 0);
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
}

function vizSubsPaint(msg) {
  const box = $("#subsList");
  if (!box) return;
  if (msg) {
    box.innerHTML = `<div class="viz-empty">${esc(msg)}</div>`;
    return;
  }
  box.innerHTML = VIZ_SUBS.rows.map((r) => `
    <div class="srow">
      <span class="st">${fmtSubT(r.t0)}</span>
      <input class="inp sen" data-i="${r.i}" value="${esc(r.text)}" placeholder="（这一句没词，可以自己写）">
      <input class="inp szh" data-i="${r.i}" value="${esc(r.zh)}" placeholder="中文（可留空）">
    </div>`).join("");
}

function vizSubsCollect() {
  const rows = VIZ_SUBS.rows.map((r) => ({ i: r.i, text: r.text, zh: r.zh }));
  document.querySelectorAll("#subsList .srow").forEach((el) => {
    const a = el.querySelector(".sen"), b = el.querySelector(".szh");
    if (!a) return;
    const i = Number(a.dataset.i);
    const row = rows.find((x) => x.i === i);
    if (!row) return;
    row.text = a.value;
    row.zh = b ? b.value : "";
  });
  return rows;
}

async function vizSubsRefresh() {
  const sel = $("#vizSrc");
  const src = (sel && sel.value) || "";
  if (!vizIsCast() || !src.startsWith(VIZ_OUT)) {
    VIZ_SUBS = { role: "", index: -1, rows: [] };
    vizSubsShow(false);
    return;
  }
  const [, rid, idx] = src.split(":");
  const index = parseInt(idx, 10) || 0;
  if (VIZ_SUBS.role === rid && VIZ_SUBS.index === index && VIZ_SUBS.rows.length) {
    vizSubsShow(true);
    return;
  }
  vizSubsShow(true);
  vizSubsPaint("正在读这条成品的歌词…");
  try {
    const d = await api(`/api/viz/cast?role=${encodeURIComponent(rid)}&index=${index}`);
    const cast = (d && d.cast) || {};
    vizWhoNote(cast);
    VIZ_SUBS = {
      role: rid, index, src: cast.src || VIZ_SUBS.src || "",
      rows: (cast.lines || []).map((ln, k) => ({
        i: k, t0: Number(ln.start) || 0, text: ln.text || "", zh: ln.zh || "",
      })),
    };
    if (!VIZ_SUBS.rows.length) {
      vizSubsPaint("这条成品没有歌词。先在成品卡上点「识别歌词」，认完词再来改字幕。");
      return;
    }
    vizSubsPaint();
    const hasZh = VIZ_SUBS.rows.some((r) => (r.zh || "").trim());
    const hint = $("#subsHint");
    if (hint) {
      hint.textContent = hasZh
        ? `${VIZ_SUBS.rows.length} 句，其中 ${VIZ_SUBS.rows.filter((r) => (r.zh || "").trim()).length} 句有中文`
        : `${VIZ_SUBS.rows.length} 句；中文还空着 —— 点「自动填中文」，或者自己填`;
    }
  } catch (e) {
    vizSubsPaint((e && e.message) || "读不到这条成品的字幕");
  }
}

/* 「谁唱谁亮」依据的是**音频**还是**记录** —— 这条要说人话。
   ★ 为什么必须显示：老板报的正是"亮的人和唱的人对不上"。
     逐窗认人是拿音频算的，所以永远对；但**算不出来**的时候（独唱轨被删了），
     画面就只能退回拼接时记的那张表 —— 那种情况下它和音频脱节是可能的，
     用户有权知道自己在看哪一种，而不是拿到一个看着一样的东西。 */
function vizWhoNote(cast) {
  const el = $("#whoNote");
  if (!el) return;
  const src = cast.who_src || "";
  const fix = Number(cast.who_fix || 0);
  if (src === "wave") {
    el.textContent = fix > 0.05
      ? `⚠ 成品里记的「谁在唱」和音频对不上（${Math.round(fix * 100)}%）—— 画面已按音频走`
      : "「谁唱谁亮」是按音频逐窗认的";
    el.className = fix > 0.05 ? "hint warn" : "hint";
  } else if (src === "record") {
    el.textContent = "⚠ 这条算不出逐窗结果（独唱轨原料不在了），画面按成品记的那张表亮人"
      + " —— 要是觉得对不上，重跑一次合唱就有了";
    el.className = "hint warn";
  } else {
    el.textContent = "";
  }
}

/**
 * 一次性粘贴「原词 + 中文」→ 系统对着歌把每一句贴到第几秒。
 *
 * ★ 两条不能糊弄的地方：
 *   1. 贴不上的句子必须**列出来**，不能塞个看着合理的时间假装贴好了；
 *   2. 需要跑识别时（这份词没时间戳）走任务 + 进度条，不让界面干等。
 */
async function vizBiImport() {
  const ta = $("#biText");
  const txt = (ta && ta.value) || "";
  if (!txt.trim()) { toast("先把歌词粘进下面的框", "bad"); if (ta) ta.focus(); return; }
  if (!VIZ_SUBS.role) { toast("先在上面选一条多角色成品", "bad"); return; }
  if (!VIZ_SUBS.src) { toast("这条成品查不到原曲路径，贴不了", "bad"); return; }
  const btn = $("#btnBiGo");
  const hint = $("#biHint");
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "对位中…"; }
  if (hint) hint.textContent = "正在把每一句贴到歌里…";
  try {
    const d = await post("/api/lyrics/import", {
      path: VIZ_SUBS.src, text: txt, lang: "zh",
      layout: ($("#biLayout") && $("#biLayout").value) || "auto",
    });
    if (d && d.need_asr && d.job) {
      if (hint) hint.textContent = d.note || "先识别这首歌…";
      watchJob(d.job.id, async () => {
        try {
          const j = await api("/api/job/" + d.job.id);
          const r = (j && j.job && j.job.result) || {};
          await vizBiApply(r.lines || [], r);
        } catch (e) {
          if (hint) hint.textContent = "识别完但没读回结果，刷新一下字幕表";
        } finally {
          if (btn) { btn.disabled = false; btn.textContent = old || "对位并贴上"; }
        }
      });
      toast("这首歌还没认过词 —— 先识别一次（约一分钟），认完自动贴上", "good");
      return;
    }
    await vizBiApply((d && d.lines) || [], d);
  } catch (e) {
    if (hint) hint.textContent = "没贴上";
    toast((e && e.message) || "歌词没导入", "bad");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || "对位并贴上"; }
  }
}

async function vizBiApply(lines, d) {
  const hint = $("#biHint");
  if (!lines || !lines.length) {
    if (hint) hint.textContent = "一句都没贴上 —— 换一种排版试试（中文歌请选「隔行」）";
    toast("一句都没贴上", "bad");
    return;
  }
  VIZ_SUBS.rows = lines.map((ln, k) => ({
    i: k, t0: Number(ln.start) || 0, text: ln.text || "", zh: ln.zh || "",
  }));
  vizSubsPaint();
  const rep = (d && d.report) || {};
  const bits = [`贴上 ${lines.length} 句`];
  if (rep.split) bits.push(`${rep.split} 句共用一个识别段，已按字数分了时间`);
  if (rep.low_conf) bits.push(`${rep.low_conf} 句词和听到的对不上（时间多半是对的，词你看一眼）`);
  if ((rep.unplaced || []).length) {
    bits.push(`★ ${rep.unplaced.length} 句没贴上：${rep.unplaced.slice(0, 3).join("、")}`
      + (rep.unplaced.length > 3 ? "…" : ""));
  }
  if (hint) hint.textContent = bits.join("；");
  // ★ 服务端那条「看着是中文在左」的提醒必须显示出来：它是在告诉用户"顺序可能
  //   反了"，而我们**故意不自动调换**（中文歌的原文自己就是汉字，代猜会整首错位）。
  //   不显示的话，用户看到的是一份"词都贴上了但全反了"的结果，无从下手。
  if (d && d.note && hint) hint.textContent += "；⚠ " + d.note;
  toast(`贴好了 ${lines.length} 句 —— 看一眼，要改直接在上面改，再点「保存到成品」`, "good");
}

async function vizSubsSave() {
  if (!VIZ_SUBS.role) return;
  const btn = $("#btnSubsSave");
  if (btn) btn.disabled = true;
  try {
    const rows = vizSubsCollect();
    await post("/api/viz/lines", {
      role: VIZ_SUBS.role, index: VIZ_SUBS.index, lines: rows,
    });
    VIZ_SUBS.rows = rows;
    toast(`字幕已存进这条成品（${rows.length} 句）—— 下次做视频还是你改过的这版`, "good");
  } catch (e) {
    toast((e && e.message) || "字幕没存上", "bad");
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function vizSubsTranslate() {
  if (!VIZ_SUBS.role) return;
  const btn = $("#btnSubsZh");
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "翻译中…"; }
  try {
    // 先把表格里还没保存的改动收上去，翻完再画回来 —— 否则用户刚打的字会被覆盖
    const cur = vizSubsCollect();
    VIZ_SUBS.rows = cur;
    const d = await post("/api/viz/translate", {
      role: VIZ_SUBS.role, index: VIZ_SUBS.index, only_empty: true,
    });
    (d.zh || []).forEach((z, k) => {
      if (VIZ_SUBS.rows[k] && !(VIZ_SUBS.rows[k].zh || "").trim()) VIZ_SUBS.rows[k].zh = z || "";
    });
    vizSubsPaint();
    const hint = $("#subsHint");
    if (hint) hint.textContent = `${d.how || "已翻译"}：补了 ${d.filled} 句中文（改过的行没动）`;
    toast("中文填好了，看一眼有不对的直接改", "good");
  } catch (e) {
    toast((e && e.message) || "翻译没跑通", "bad");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = old || "自动填中文"; }
  }
}

/* 顶部那句提示：跟着选中的样式走，把"大概要等多久"写在明处。
 * 手绘那套一首歌要几分钟 —— 不提前说的话，用户会以为卡死了。 */
function vizNoteRefresh() {
  const note = $("#vizNote");
  if (!note || !VIZ_OPT || !VIZ_OPT.ok) return;
  const s = VIZ_STYLE_MAP[VIZ_STYLE] || {};
  const enc = esc(VIZ_OPT.encoderName || VIZ_OPT.encoder);
  const mode = ($("#vizMode") && $("#vizMode").value) || "viz";
  note.className = "viz-note";

  // ★ 「加进去是不是遮挡了本来的画面」—— 这句话就是给这个问题的正面回答，
  //   按选的那条路说清楚，别让用户自己猜。
  if (mode === "mux") {
    note.innerHTML = `用 <b>${enc}</b> 编码。画面<b>一个像素都不会动</b>，` +
      `只是把声音换成这条成品 —— 几秒钟出片，<b>不会遮挡</b>原来的画面。` +
      `视频比这段声音短的话会自动循环播放铺满。`;
    return;
  }
  if (mode === "blend") {
    note.innerHTML = `用 <b>${enc}</b> 编码。先渲一层可视化，再按「屏幕混合」` +
      `叠到你的视频上：<b>黑的地方不动、亮的地方更亮</b> —— 原来画面里的内容` +
      `全都还在，不会被盖住。` + (s.note ? esc(s.note) : "") +
      `字幕在叠完之后才烧，所以字是清楚的。`;
    return;
  }
  note.innerHTML =
    `用 <b>${enc}</b> 编码。` + (s.note ? `${esc(s.note)}` : "") +
    `成品上带歌词的会自动配上中文歌词，没有歌词就只出画面和标题。` +
    `<br>想把声音配进<b>你自己的视频</b>？上面「画面来源」换成「用我的视频当画面」。` +
    vizWhatLine();
}

/* 「这次会做成什么样」——把勾了的那几项翻译成人话。
 * 不写的话，用户勾了「逐句扫色」却不知道画面会多什么，只能靠试。 */
function vizWhatLine() {
  const bits = [];
  const sz = ($("#vizSize") && $("#vizSize").value) || "landscape";
  if (vizIsCast()) {
    // 每一款构型的画面差别很大，「这次会做成什么样」得说各自那句，别共用一句。
    const lay = (VIZ_STYLE_MAP[VIZ_STYLE] || {}).cast_lay || "row";
    const LNOTE = {
      row: sz === "vertical" ? "竖屏：头像排两列、字放大" : "一排人横着站，谁唱谁亮",
      duet: "左右两列对垒，中间一条分界线",
      ring: "一圈人围坐，中心跟着整首的能量鼓动",
      stage: "舞台台面 + 追光柱，唱到谁照谁",
      // mv 是独立画法（不走 cast 那条路）：黑底、角色格、色差字、故障抖动
      mv: "MV 片头：纯黑底 + 顶部角色格 + 粗斜体色差字，低音重时会抖动",
    };
    if (LNOTE[lay]) bits.push(LNOTE[lay]);
    const k = $("#vizKaraoke"), m = $("#vizMotion");
    if (k && k.checked) bits.push("当前这句从白扫到黄");
    if (m && m.checked) bits.push("谁唱谁轻轻起伏 + 电平条");
  }
  const bg = ($("#vizBg") && $("#vizBg").value) || "grad";
  if (bg === "color") bits.push("纯色背景");
  if (bg === "image") bits.push("用你的图当背景（会模糊压暗，不然歌词看不清）");
  return bits.length ? `<br>这次：<b>${bits.join(" · ")}</b>` : "";
}

/* 音频下拉：角色成品在前（这些才有歌词），本地曲库在后。
 * ★ 自己拉一次角色列表，不依赖 app.js 里 ROLES 的加载时机 ——
 *   那种"谁先加载"的隐式依赖，换个调用顺序就静默变空下拉。
 */
async function vizSrcFill(keep) {
  const sel = $("#vizSrc");
  if (!sel) return;
  const want = keep || sel.value;
  let roles = [];
  try { roles = (await api("/api/roles")).roles || []; } catch (e) { /* 拉不到就只列曲库 */ }
  const outs = [];
  roles.forEach((r) => {
    (r.outputs || []).forEach((o, i) => {
      // 只要真能听的：说话和唱歌的成品
      if (!o.wav || (o.kind !== "singing" && o.kind !== "speech")) return;
      outs.push({
        v: VIZ_OUT + r.id + ":" + i,
        t: `${r.name} · ${o.text || "成品"}${o.lyric_lrc ? "（带歌词）" : ""}`,
      });
    });
  });
  const lib = ((typeof LOC_SCAN !== "undefined" && LOC_SCAN.items) || [])
    .map((it) => ({ v: it.path, t: it.name || it.path }));

  let html = "";
  if (outs.length) {
    html += '<optgroup label="角色成品（带歌词）">' +
      outs.map((o) => `<option value="${esc(o.v)}">${esc(o.t)}</option>`).join("") +
      "</optgroup>";
  }
  if (lib.length) {
    html += '<optgroup label="本地曲库">' +
      lib.map((o) => `<option value="${esc(o.v)}">${esc(o.t)}</option>`).join("") +
      "</optgroup>";
  }
  sel.innerHTML = html ||
    '<option value="">（还没有可用的音频：先去唱一首，或到「我的音乐」导一首）</option>';
  if (want && html) {
    sel.value = want;
    if (!sel.value) sel.selectedIndex = 0;   // 原来选中那条没了就回到第一条
  }
  // 换了音频 ⇒ 字幕表可能整张都要换（这只在合唱台样式下才有内容）
  vizSubsRefresh();
}

/* 成品卡片上的按钮：预选这条，滚到「做视频」那一块。 */
async function vizFromOutput(rid, index) {
  await vizSrcFill(VIZ_OUT + rid + ":" + index);
  // ★ 多角色成品默认切到合唱族：用户点的是"给这条**对唱**做视频"，
  //   而默认样式是光环 —— 他得自己去列表里找那一款，多半根本不知道有。
  //   单角色成品不动（合唱族那几款没有分配表，画不出人）。
  //   族里有多款时默认落「横排」那一款（最通用），没有就落第一款。
  const role = (typeof CUR !== "undefined" && CUR && CUR.id === rid) ? CUR : null;
  const o = role ? (role.outputs || [])[index] : null;
  const p = (o && o.params) || {};
  const isMulti = ((p.multi_takes || []).length > 0) && ((p.multi_roles || []).length >= 2);
  if (isMulti) {
    const keys = vizCastKeys();
    if (keys.length) {
      const pick = keys.indexOf("cast") >= 0 ? "cast" : keys[0];
      vizPickStyle(pick);
      const nm = (VIZ_STYLE_MAP[pick] || {}).name || "合唱台";
      toast(`这条是多人合唱 —— 已自动选「${nm}」，点开始就行` +
            (keys.length > 1 ? `（旁边还有 ${keys.length - 1} 款合唱排版可以换）` : ""));
    }
  }
  const sec = $("#secVideos");
  if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
  if (!isMulti) toast("已选中这条成品，挑个样式就能做视频");
}

function vizPartToggle() {
  const on = $("#vizPart").checked;
  $("#vizStart").disabled = !on;
  $("#vizDur").disabled = !on;
}

async function vizGo() {
  const btn = $("#btnVizGo");
  if (VIZ_BUSY || !VIZ_OPT || !VIZ_OPT.ok) return;
  const src = $("#vizSrc").value;
  if (!src) return toast("先挑一个音频", "bad");
  const mode = ($("#vizMode") && $("#vizMode").value) || "viz";
  if (mode !== "viz" && !VIZ_VIDEO) {
    return toast("先点「选我的视频…」挑一段要当画面的视频", "bad");
  }

  const body = {
    kind: "viz",
    mode,
    bg_video: mode === "viz" ? "" : VIZ_VIDEO,
    style: VIZ_STYLE,
    size: $("#vizSize").value,
    fps: parseInt($("#vizFps").value, 10) || 30,
  };
  if (src.startsWith(VIZ_OUT)) {
    const [, rid, idx] = src.split(":");
    body.role = rid;
    body.index = parseInt(idx, 10) || 0;
  } else {
    body.audio = src;
  }
  if ($("#vizPart").checked) {
    body.start = parseFloat($("#vizStart").value) || 0;
    body.duration = parseFloat($("#vizDur").value) || 0;
  }
  // 字幕表里现改的字（没点保存也认）：跟着这次渲染一起发过去。
  // 只在这条成品正好是表里那条时才带，免得把 A 歌的字幕烧进 B 歌。
  if (VIZ_SUBS.role && VIZ_SUBS.rows.length &&
      src === `${VIZ_OUT}${VIZ_SUBS.role}:${VIZ_SUBS.index}`) {
    body.lines = vizSubsCollect().map((r) => ({ i: r.i, text: r.text, zh: r.zh }));
  }
  // 合唱族的画面选项：扫色 / 动效 / 排版 / 片头片尾（族里每一款都要带上，
  // 少带一款 = 那一款的所有开关都静默失效，UI 上完全看不出来）
  if (vizIsCast()) {
    const g = (id) => $(id);
    body.cast_opt = {
      karaoke: !!(g("#vizKaraoke") || {}).checked,
      motion: !!(g("#vizMotion") || {}).checked,
      layout: (g("#vizLayout") && g("#vizLayout").value) || "bottom",
      intro: parseFloat((g("#vizIntro") || {}).value) || 0,
      outro: parseFloat((g("#vizOutro") || {}).value) || 0,
    };
  }
  body.bg = {
    kind: VIZ_BG.kind || "grad",
    color: ($("#vizBgColor") && $("#vizBgColor").value) || "",
    image: (VIZ_BG.kind === "image" ? VIZ_BG_IMG : ""),
  };
  // 记住这次用的设置：「重出视频」靠它还原来一份一模一样的（字幕另取当前表格）
  VIZ_LAST = {
    style: VIZ_STYLE, size: body.size, fps: body.fps, mode,
    cast_opt: body.cast_opt || null, bg: body.bg, src,
    start: body.start, duration: body.duration,
  };

  VIZ_BUSY = true;
  btn.disabled = true;
  btn.textContent = "制作中…";
  try {
    const j = await post("/api/jobs", body);
    const jid = (j.job || {}).id;
    toast("开始做视频（右下角有进度）");
    // 用项目既有的 watchJob：跑完自动回调，不用自己写轮询
    // （自己再写一套轮询 = 两套判"任务结束"的口径，迟早漂）。
    watchJob(jid, async () => {
      VIZ_BUSY = false;
      const b2 = $("#btnVizGo");
      b2.disabled = false;
      b2.textContent = "开始制作";
      try {
        const k = await api("/api/job/" + jid);
        if (k.job && k.job.state === "done") {
          toast("视频做好了，下面可以直接播放或下载");
          await vizListLoad();
        } else if (k.job && k.job.state === "failed") {
          toast("做视频失败：" + (k.job.error || "看任务面板"), "bad");
        }
      } catch (e) { await vizListLoad(); }
    });
  } catch (e) {
    toast("提交失败：" + (e.message || e), "bad");
    VIZ_BUSY = false;
    btn.disabled = false;
    btn.textContent = "开始制作";
  }
}

/* 「用这次的字幕重出视频」：把上次成功提交的那套设置恢复回去，再跑一次。
 *
 * ★ 为什么不直接重发上次那份 body：字幕表里的是**现在**的字（可能刚改过还没保存），
 *   重发旧 body 会把用户刚打的那几个字丢掉 —— 那恰好是这颗按钮存在的理由。
 */
function vizRedo() {
  if (!VIZ_LAST) return toast("还没用这套设置出过片 —— 先点一次「开始制作」", "bad");
  const L = VIZ_LAST;
  const g = (id) => $(id);
  if (g("#vizSrc") && L.src) g("#vizSrc").value = L.src;
  if (VIZ_STYLE_MAP[L.style]) vizPickStyle(L.style);
  if (g("#vizSize")) g("#vizSize").value = L.size;
  if (g("#vizFps")) g("#vizFps").value = String(L.fps);
  if (g("#vizMode")) { g("#vizMode").value = L.mode; vizModeChange(); }
  if (L.cast_opt) {
    if (g("#vizKaraoke")) g("#vizKaraoke").checked = !!L.cast_opt.karaoke;
    if (g("#vizMotion")) g("#vizMotion").checked = !!L.cast_opt.motion;
    if (g("#vizLayout")) g("#vizLayout").value = L.cast_opt.layout || "bottom";
    if (g("#vizIntro")) g("#vizIntro").value = L.cast_opt.intro || 0;
    if (g("#vizOutro")) g("#vizOutro").value = L.cast_opt.outro || 0;
  }
  if (L.bg) {
    if (g("#vizBg")) g("#vizBg").value = L.bg.kind || "grad";
    if (g("#vizBgColor") && L.bg.color) g("#vizBgColor").value = L.bg.color;
    if (L.bg.image) {
      VIZ_BG_IMG = L.bg.image;
      const nm = g("#vizBgName");
      if (nm) nm.textContent = "已选（上次那张图）";
    }
    vizBgChange();
  }
  if (L.start !== undefined && L.start !== null) {
    if (g("#vizPart")) g("#vizPart").checked = true;
    vizPartToggle();
    if (g("#vizStart")) g("#vizStart").value = L.start;
    if (g("#vizDur")) g("#vizDur").value = L.duration;
  }
  toast("按上次的设置重做，字幕用表里现在这份");
  vizGo();
}

async function vizListLoad() {
  const box = $("#vizList");
  if (!box) return;
  let vids = [];
  try { vids = (await api("/api/viz/library")).videos || []; } catch (e) { /* 列不出来就别刷 */ }
  if (!vids.length) {
    box.innerHTML = '<div class="viz-empty">还没有做过视频。选个样式，点「开始制作」。</div>';
    return;
  }
  box.innerHTML = vids.map((v) => `
    <div class="viz-card">
      <video controls preload="none" src="${media(v.path)}"></video>
      <div class="b">
        <div class="t" title="${esc(v.path)}">${esc(v.name)}</div>
        <div class="m"><span>${esc(v.at)}</span><span>${v.mb} MB</span></div>
        <div class="r">
          <a class="btn sm" href="${media(v.path)}" download>下载</a>
          <button class="btn sm ghost" data-cp="${esc(v.path)}">复制路径</button>
        </div>
      </div>
    </div>`).join("");
  // 「复制路径」而不是「打开文件夹」：项目里没有打开资源管理器的接口，
  // 挂一个点了没反应的按钮比不放更糟。
  box.querySelectorAll("button[data-cp]").forEach((b) => {
    b.onclick = async () => {
      try {
        await navigator.clipboard.writeText(b.dataset.cp);
        toast("路径已复制");
      } catch (e) {
        toast("复制不了，手动从文件名上取吧", "bad");
      }
    };
  });
}
