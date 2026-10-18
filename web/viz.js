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

const VIZ_GROUPS = [
  ["draw", "精美 · 逐帧手绘", "画面最讲究，但要等几分钟"],
  ["fast", "快速 · 滤镜合成", "几秒钟出片，画面朴素些"],
];

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
  vizModeChange();
  vizListLoad();
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
    `<br>想把声音配进<b>你自己的视频</b>？上面「画面来源」换成「用我的视频当画面」。`;
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
}

/* 成品卡片上的按钮：预选这条，滚到「做视频」那一块。 */
async function vizFromOutput(rid, index) {
  await vizSrcFill(VIZ_OUT + rid + ":" + index);
  const sec = $("#secVideos");
  if (sec) sec.scrollIntoView({ behavior: "smooth", block: "start" });
  toast("已选中这条成品，挑个样式就能做视频");
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
