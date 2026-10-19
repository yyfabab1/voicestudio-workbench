/* VoiceStudio 前端。原生 JS，无框架 —— 页面逻辑不复杂，引框架反而更重。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let ROLES = [];
let SUMM = null;         // /api/roles 给的汇总（可说话/可唱歌各几个）
let CUR = null;          // 当前打开的角色
let pickedSong = null;   // 待转换的歌曲路径
// 【踩过的坑·第四处】这一行也曾整行不见。写入（`pickedVideo = j.path`）在
// 非严格模式下会**悄悄造个全局**所以不报错，于是"先点＋导入视频再选文件"这条
// 路是通的（点按钮时顺手把它创建出来了）；但只要是**拖拽**进来的视频
// （走 window 的 drop 处理器打开导入框，没经过那个按钮），点「开始拆解」就
// `ReferenceError: pickedVideo is not defined` —— 表现为"按钮点了没反应"。
// ★ 这就是"靠运气覆盖 = 没覆盖"：正常路径碰巧不报错，掩盖了声明缺失。
let pickedVideo = null;  // 待导入的视频路径
let pickerMode = "video";
let STAGE_ROLE = null;   // 翻唱台选中的角色
// 【踩过的坑】这一行曾经整行不见了，而 stageSetSong 里还在 `STAGE_SONG = path`。
// 非严格模式下"给未声明的名字赋值"会**悄悄造一个全局**，所以写入不报错；
// 但 stageRefreshBtn 第一句就**读**它 —— 读一个没声明过的名字直接
// ReferenceError。于是「⚡ 一键合成」按钮永远点不亮（按钮初始就是 disabled，
// 只有这个函数会点亮它），翻唱台整块用不了，而后端接口全是好的。
// 这类"删掉一行声明"的坏编辑语法检查抓不到，只有真跑页面才看得见。
let STAGE_SONG = null;   // 翻唱台选中的歌

const COLORS = ["#6c8cff", "#ff7a59", "#2fb8a8", "#c86dd7", "#e0b53c", "#4fc75a", "#ff6f91", "#5ec8f2"];

function toast(msg, kind) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast on" + (kind ? " " + kind : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => (t.className = "toast"), 2600);
}

async function api(path, opt) {
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("请求失败 " + r.status));
  return j;
}
const post = (p, body) => api(p, {
  method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {})
});

function media(p) { return "/media?path=" + encodeURIComponent(p); }

/* 懒加载播放器。
   为什么需要：素材/成品列表动辄几百条（大帝 357 段 + 110 条成品 = 467 个），
   一条一个 <audio controls> 实测让 DOM 从 1800 涨到 13321 个节点 ——
   浏览器要为**每一个**都建出完整的媒体控件 UI（播放键、进度条、时间、音量…），
   而且全都建在打开角色的同一帧里 ⇒ 点开角色就卡。
   改成先放一个轻量按钮，**点了才**建 audio：功能一个不少（照样能拖进度、
   调音量、看时长），没点过的那些一条节点都不占。
   ★ 铁律 5：这是降"一次性开销"，不是删功能。
*/
function lazyPlayer(src, cls, dur) {
  // 时长直接写在按钮上：数据本来就有（素材 s.dur / 成品 o.seconds），
  // 这样"还没点开"也能一眼看出这条多长，不至于把懒加载做成信息倒退。
  const d = dur ? ` <span class="dur">${Number(dur).toFixed(1)}s</span>` : "";
  return `<button class="mini" data-act="play" data-play="${esc(src)}"
          ${cls ? `data-cls="${esc(cls)}"` : ""}>▶ 播放${d}</button>`;
}

function swapInPlayer(btn) {
  const a = document.createElement("audio");
  a.controls = true;
  a.preload = "metadata";     // 都点了，顺手拿时长，让进度条有刻度
  if (btn.dataset.cls) a.className = btn.dataset.cls;
  a.src = btn.dataset.play;
  btn.replaceWith(a);
  a.play().catch(() => { /* 浏览器拦自动播放就让用户再点一下，不影响 */ });
}

/* ---------------------------------------------------------- 角色墙 */

async function loadRoles() {
  const j = await api("/api/roles");
  ROLES = j.roles;
  SUMM = j.summary || null;
  render();
}

function kindTag(r) {
  if (r.pending) return '<span class="tag pending">待归类</span>';
  if (r.kind === "singing") return '<span class="tag singing">唱歌</span>';
  if (r.kind === "speech") return '<span class="tag speech">说话</span>';
  return '<span class="tag none">未定</span>';
}

/* 能力徽章。
   【为什么要有】以前这里写的是「唱歌 · 未训练」——「未训练」说的是没有**说话**
   模型，但 17 个市场角色的歌声模型是现成能用的，用户看到这三个字只会以为整张卡
   是废的；而卡片上的「💬 TA 说话」对它们又是点了必然失败的。所以改成直接说人话：
   「🎤 可唱歌 · 💬 还不能说话」。状态由后端 ability 现算（见 studio.role_ability），
   前端只负责显示，判据只写一份。 */
function abilityTags(r) {
  const a = r.ability;
  if (!a) return "";
  const one = (key, icon) => {
    const s = a[key];
    const cls = s.state === "ready" ? "ok" : (s.state === "trainable" ? "mid" : "no");
    return `<span class="tag ${cls}" title="${esc(s.why)}">${icon} ${esc(s.label)}</span>`;
  };
  return one("sing", "🎤") + one("speak", "💬");
}

function render() {
  const g = $("#grid");
  // 汇总：装了十几个角色之后，"到底哪几个现在就能用"是第一个问题。
  $("#roleCount").textContent = ROLES.length
    ? `共 ${ROLES.length} 个` + (SUMM
        ? ` · 🎤 可唱歌 ${SUMM.can_sing} · 💬 可说话 ${SUMM.can_speak}`
        : "")
    : "";
  $("#emptyBox").style.display = ROLES.length ? "none" : "block";
  g.innerHTML = ROLES.map(r => {
    const st = r.stats || {};
    const color = r.color || COLORS[0];
    const initial = (r.name || "?").trim().slice(0, 1).toUpperCase();
    const a = r.ability || {};
    // 走不通的入口**弱化**而不是删掉：点进去能看到为什么（说话页会给出横幅），
    // 但一眼就知道它现在出不了东西，不用白填一段字再等一个失败任务。
    const spOff = a.speak ? a.speak.state !== "ready" : false;
    const sgOff = a.sing ? a.sing.state === "blocked" : false;
    return `
    <div class="card" draggable="true" data-role="${r.id}" onclick="openRole('${r.id}')">
      <div class="glow" style="background:${color}"></div>
      <span class="dragdot" title="按住这张卡可以拖 —— 拖到下面的「歌手」槽就能用它唱歌">⠿ 可拖</span>
      <div class="row1">
        <div class="avatar" style="background:linear-gradient(135deg,${color},${shade(color)})">${esc(initial)}</div>
        <div style="flex:1;min-width:0">
          <div class="nm">${esc(r.name)}</div>
          <div class="sub">${kindTag(r)} ${abilityTags(r)}</div>
        </div>
      </div>
      <div class="intro">${esc(r.intro || "（还没有介绍，点开写一句）")}</div>
      <div class="meta">
        <span class="chip">${st.segments || 0} 段</span>
        <span class="chip">${fmtSec(st.seconds || 0)}</span>
        ${(r.seedvc && r.seedvc.ckpt) ? `<span class="chip">音色微调 ${esc(r.seedvc.step ? ("ft" + r.seedvc.step) : "✓")}</span>` : ""}
        ${r.outputs && r.outputs.length ? `<span class="chip">成品 ${r.outputs.length}</span>` : ""}
      </div>
      <div class="acts">
        <button class="btn sm${spOff ? " off" : ""}" title="${esc(a.speak ? a.speak.why : "")}"
          onclick="event.stopPropagation();openRole('${r.id}','paneSpeak')">💬 TA 说话</button>
        <button class="btn sm${sgOff ? " off" : ""}" title="${esc(a.sing ? a.sing.why : "")}"
          onclick="event.stopPropagation();openRole('${r.id}','paneSing')">🎤 TA 唱歌</button>
      </div>
    </div>`;
  }).join("");
}

function shade(hex) {
  // 把主色压暗一点做渐变另一端。
  // 【踩过的坑】这里曾经写成 `const r = Math.round((n >> 16) * 0.55)`——
  // n 从来没定义过（应该是从 hex 解析出来的那个数）。语法检查查不出来，
  // 但 render() 第一次调用 shade() 就抛 ReferenceError，于是**角色墙一张卡
  // 都画不出来**，整个页面看起来像"后端没数据"。修的时候别只看报错行，
  // 要顺着调用栈确认是"这个函数坏了"还是"调用方坏了"。
  const n = parseInt(String(hex || "").replace("#", ""), 16) || 0;
  const r = Math.round(((n >> 16) & 255) * 0.55);
  const g = Math.round(((n >> 8) & 255) * 0.55);
  const b = Math.round((n & 255) * 0.55);
  return "#" + [r, g, b].map(v => v.toString(16).padStart(2, "0")).join("");
}

/* ---------------------------------------------------------- 详情 */

async function openRole(id, pane) {
  const j = await api("/api/role/" + id);
  CUR = j.role;
  CUR._paths = j.paths || {};
  CUR._rvcTrained = null;          // 还没问到，别把上一角色的轮数带过来
  $("#maskRole").classList.add("on");
  $("#drawer").classList.add("on");
  paintRole();
  if (pane) {
    $$(".tab").forEach(t => t.classList.toggle("on", t.dataset.pane === pane));
    $$(".pane").forEach(p => p.classList.toggle("on", p.id === pane));
  }
  // 问一次「歌声模型已经训到第几轮」。要起 RVC 的 python（约 3 秒），
  // 所以不 await —— 抽屉先开，答案回来再填进去。
  if (pane === "paneTrain") { refreshRvcState(); refreshSeedvc(); }
  // 换角色时先把上一角色的档位清掉：不然在新角色的面板上会看到上一人的 ft600，
  // 而且点"换档"会用错误的路径去改新角色。
  SV_STATE = null;
  if ($("#svState")) $("#svState").textContent = "";
  if ($("#svCkpt")) $("#svCkpt").innerHTML = "";
}
function closeDrawer() {
  $("#maskRole").classList.remove("on");
  $("#drawer").classList.remove("on");
  CUR = null;
}

function paintRole() {
  const r = CUR;
  const color = r.color || COLORS[0];
  const av = $("#dAvatar");
  av.textContent = (r.name || "?").trim().slice(0, 1).toUpperCase();
  av.style.background = `linear-gradient(135deg,${color},${shade(color)})`;
  $("#dName").value = r.name || "";
  $("#dIntro").value = r.intro || "";
  $("#dSub").innerHTML = `${kindTag(r)} · ${r.stats ? r.stats.segments : 0} 段 · ${fmtSec(r.stats ? r.stats.seconds : 0)} · ${esc(r.source || "手动创建")}`;
  $("#kindSpeech").classList.toggle("on", r.kind === "speech");
  $("#kindSinging").classList.toggle("on", r.kind === "singing");
  const g = r.kind_guess;
  $("#kindHint").textContent = g
    ? `自动判断：${g.kind === "singing" ? "唱歌" : "说话"}（置信度 ${g.conf}）—— ${g.why}。自动判断仅供参考，以你的选择为准。`
    : "";
  $("#colors").innerHTML = COLORS.map(c =>
    `<div class="mini ${c === color ? "on" : ""}" style="width:30px;height:30px;border-radius:9px;background:${c};padding:0;border-color:${c === color ? "#fff" : "transparent"}" onclick="setColor('${c}')"></div>`).join("");

  $("#segBadge").textContent = (r.segments || []).length ? `(${(r.segments || []).length})` : "";
  $("#outBadge").textContent = (r.outputs || []).length ? `(${(r.outputs || []).length})` : "";
  paintSegs();
  paintOuts();

  $("#modelInfo").textContent = r.model
    ? `当前权重：${r.model.ckpt}（${r.model.epochs} 轮，训练于 ${r.model.at}）`
    : "还没训练过。先准备好素材和文字，再点上面的按钮。";
  $("#rvcInfo").textContent = r.rvc
    ? `已有歌声模型：${r.rvc.model}（${r.rvc.at}${r.rvc.index ? "，含特征索引库" : "，无索引库"}）`
    : "还没有歌声模型。";
  $("#rvcState").textContent = "";
  $("#btnRvc").disabled = false;
  $("#trainHint").textContent = "";
  $("#speakHint").textContent = "";
  $("#songInfo").textContent = "";
  pickedSong = null;
  // 换了角色/刷新后没选歌，两个按钮就该是灰的（以前 btnSing 漏了这步，
  // 于是切到新角色时按钮还是亮的，点下去只会弹"先选一首歌"）。
  $("#btnSing").disabled = true;
  if ($("#btnPreview")) $("#btnPreview").disabled = true;
  paintAbilityBar(r);
}

/* 「现在能干什么」状态条（说话 / 唱歌各一条）。
   【为什么要有】这一段以前是 `if (r.kind === "speech" && !r.model)` —— 只在角色
   类型是「说话」时才提示。可市场装进来的角色 kind 全是 "singing"（它们确实是唱歌
   模型），于是**一句预警都没有**：用户在说话页填好字、点合成、看着任务跑起来、
   等几秒才收到一句红字「这个角色还没训练过」。入口通不通，必须在点之前就看得见。 */
function paintAbilityBar(r) {
  const a = r.ability || {};
  const sp = a.speak || { state: "blocked", why: "" };
  const sg = a.sing || { state: "blocked", why: "" };
  const nxt = a.next || { code: "import", hint: "" };
  const cls = (s) => (s.state === "ready" ? "ok" : (s.state === "trainable" ? "mid" : "no"));
  // ★ 类名带 abk- 前缀：`.ab` 这个短名字翻唱台分离轨道那边已经在用，
  //   重名会把那边的布局带坏（踩过，见 style.css 里的注释）。
  const bar = (s, head, extra) => `
    <div class="abk ${cls(s)}">
      <span class="abk-dot"></span>
      <div class="abk-tx"><b>${head}：${esc(s.label)}</b>
        <span>${esc(s.why)}</span>${extra || ""}</div>
    </div>`;

  // 说话：走不通就把合成按钮灰掉 —— 不然就是白填一段字、白等一次失败任务。
  $("#abSpeak").innerHTML = bar(sp, "说话",
    nxt.code !== "ready" && sp.state !== "ready"
      ? `<span class="abk-next">下一步：${esc(nxt.label)} —— ${esc(nxt.hint)}</span>` : "");
  $("#btnSpeak").disabled = sp.state !== "ready";

  $("#abSing").innerHTML = bar(sg, "唱歌",
    sg.state === "blocked"
      ? '<span class="abk-next">先去「简介」页点「＋ 追加素材」，导一段这个人的录音。</span>'
      : (sg.state === "trainable"
          ? '<span class="abk-next">第一次点转换会先训一个歌声模型，要多等几分钟。</span>' : ""));

  // 简介页：两件事 + 下一步一起说，打开角色第一眼就知道这个角色现在能干嘛。
  $("#abInfo").innerHTML = bar(sg, "唱歌", "") + bar(sp, "说话",
    sp.state !== "ready" && nxt.code !== "ready"
      ? `<span class="abk-next">下一步：${esc(nxt.label)} —— ${esc(nxt.hint)}</span>` : "");
}

function paintSegs() {
  const r = CUR;
  const ref = (r.ref || {}).file;
  const list = $("#segList");
  if (!r.segments || !r.segments.length) {
    list.innerHTML = '<div class="empty" style="padding:26px">这个角色还没有素材。导入视频后会自动分配，也可以手动新建。</div>';
    return;
  }
  /* 路径一律走 data-f，不进内联事件属性：Windows 路径里的 \ 在 HTML 属性
     里会被当成转义符吃掉（"D:\A\x.wav" → "D:Ax.wav"），表现是点了没反应、
     后端只回一句"文件不存在"。踩过一次，别再写回去。 */
  list.innerHTML = r.segments.map((s, i) => `
    <div class="seg ${s.file === ref ? "ref" : ""}">
      <div class="no">${i + 1}</div>
      <div class="txt">
        <input value="${esc(s.text || "")}" placeholder="（这句话还没识别出文字，可以自己填）"
               data-f="${esc(s.file)}">
        <div class="dur">${(s.dur || 0).toFixed(1)} 秒${s.file === ref ? " · 当前参考" : ""}</div>
      </div>
      <div class="op">
        ${lazyPlayer(media(roleSlice(s.file)), "", s.dur)}
        <button class="mini" data-act="ref" data-f="${esc(s.file)}">设为参考</button>
        <button class="mini" data-act="del" data-f="${esc(s.file)}">删除</button>
      </div>
    </div>`).join("");
  list.querySelectorAll("input[data-f]").forEach(el => {
    el.onchange = () => editSeg(el.dataset.f, el.value);
  });
  /* ★ 原来是 `act === "ref" ? setRef : delSeg` 的三元 —— 往里加第三种动作时，
       "play" 会掉进 else 分支被当成**删除素材**（数据直接没了）。
       所以这里一律改成显式 if/else if，只认白名单里的动作。 */
  list.querySelectorAll("button[data-act]").forEach(b => {
    b.onclick = () => {
      const act = b.dataset.act;
      if (act === "play") swapInPlayer(b);
      else if (act === "ref") setRef(b.dataset.f);
      else if (act === "del") delSeg(b.dataset.f);
    };
  });
}

function paintOuts() {
  const r = CUR;
  const box = $("#outList");
  const outs = r.outputs || [];
  if (!outs.length) {
    box.innerHTML = '<div class="empty" style="padding:26px">还没有成品。去「说话」或「唱歌」页生成。</div>';
    return;
  }
  // ★ 列表是倒着画的（最新的在最上面），但**成品下标得用正的** —— 补歌词要告诉
  //   后端改第几条，传倒过来的下标就会改错一条（踩过这类"渲染顺序≠数据顺序"的坑）。
  box.innerHTML = outs.map((o, _i) => [o, outs.length - 1 - _i]).reverse().map(([o, i]) => `
    <div class="out">
      <div class="t">
        ${o.kind === "singing" ? '<span class="tag singing">唱</span> '
          : o.kind === "preview" ? '<span class="tag preview">试听样片</span> '
          : '<span class="tag speech">说</span> '}
        ${esc(o.text)}
        <div class="m">${o.at}${o.seconds ? " · " + o.seconds + " 秒" : ""}${o.similarity ? " · 音色相似度 " + o.similarity : ""}</div>
        ${qcBadge(o)}
      </div>
      ${lazyPlayer(media(o.wav), "", o.seconds)}
      <a class="btn sm" href="${media(o.wav)}" download>下载</a>
      ${o.lyric ? `<button class="btn sm" data-lyr="${i}">歌词</button>` : ""}
      ${o.lyric_lrc ? `<a class="btn sm" href="${media(o.lyric_lrc)}" download>下 .lrc</a>` : ""}
      ${!o.lyric && o.stems && o.stems.src
        ? `<button class="btn sm" data-needlyr="${i}">识别歌词</button>` : ""}
      ${o.wav ? `<button class="btn sm" data-viz="${i}"
        title="把这条成品配上频谱画面，渲染成能直接发出去的 MP4">🎬 做视频</button>` : ""}
      ${o.lyric ? `<div class="lyr" id="lyr${i}" style="display:none;white-space:pre-wrap;
           font-size:12px;line-height:1.7;margin-top:6px;color:var(--txt2);
           max-height:220px;overflow:auto">${esc(o.lyric)}</div>` : ""}
    </div>`).join("");
  // 歌词默认收着：成品列表一屏好几条，全展开就没法扫了
  // 懒加载播放器：点了才把按钮换成真正的 <audio>
  box.querySelectorAll("button[data-act='play']").forEach(b => {
    b.onclick = () => swapInPlayer(b);
  });
  box.querySelectorAll("button[data-lyr]").forEach(b => {
    b.onclick = () => {
      const el = $("#lyr" + b.dataset.lyr);
      if (!el) return;
      const on = el.style.display !== "none";
      el.style.display = on ? "none" : "block";
      b.textContent = on ? "歌词" : "收起歌词";
    };
  });
  // 「识别歌词」：跑语音识别把词补进这条成品。走 job（要一两分钟，得有进度条），
  // 不是同步请求 —— 同步的话界面就干等着转圈。
  box.querySelectorAll("button[data-needlyr]").forEach(b => {
    b.onclick = async () => {
      if (!CUR || b._busy) return;
      b._busy = true; b.disabled = true; b.textContent = "识别中…";
      try {
        const j = await post("/api/jobs", {kind: "output_lyric", role: CUR.id,
                                           index: Number(b.dataset.needlyr)});
        watchJob(j.job.id, async () => {
          const k = await api("/api/role/" + CUR.id);
          CUR = k.role;
          paintOuts();
        });
      } catch (e) {
        toast(e.message || "识别没跑起来", "bad");
        b._busy = false; b.disabled = false; b.textContent = "识别歌词";
      }
    };
  });
  // 「做视频」：把这条成品交给「做视频」区块 —— 那里选样式，这里只负责预选 + 带路。
  box.querySelectorAll("button[data-viz]").forEach(b => {
    b.onclick = () => {
      if (!CUR) return;
      vizFromOutput(CUR.id, Number(b.dataset.viz));
    };
  });
}

function roleSlice(f) {
  return (CUR && CUR._paths && CUR._paths[f]) || f;
}

/* ---------------------------------------------------------- 编辑 */

async function saveMeta() {
  await post("/api/role/" + CUR.id, {
    name: $("#dName").value.trim(), intro: $("#dIntro").value, kind: CUR.kind, color: CUR.color
  });
  toast("已保存", "good");
  await loadRoles();
  const j = await api("/api/role/" + CUR.id);
  CUR = j.role;
  paintRole();
}
function setKind(k) {
  CUR.kind = k;
  $("#kindSpeech").classList.toggle("on", k === "speech");
  $("#kindSinging").classList.toggle("on", k === "singing");
}
function setColor(c) {
  CUR.color = c;
  paintRole();
}
async function editSeg(file, text) {
  await post(`/api/role/${CUR.id}/segment-text`, { file, text });
  const s = CUR.segments.find(x => x.file === file);
  if (s) s.text = text;
  toast("文字已更新，下次训练会生效", "good");
}
async function delSeg(file) {
  if (!confirm("删除这段素材？对应的标注也会一起删掉。")) return;
  await post(`/api/role/${CUR.id}/segment-delete`, { file });
  const j = await api("/api/role/" + CUR.id);
  CUR = j.role;
  paintRole();
  loadRoles();
}
async function setRef(file) {
  await post(`/api/role/${CUR.id}/set-ref`, { file });
  const j = await api("/api/role/" + CUR.id);
  CUR = j.role;
  paintRole();
  toast("参考音频已更新", "good");
}
async function delRole() {
  if (!confirm("删除这个角色？它的素材和模型记录都会删掉（权重文件本身保留在整合包里）。")) return;
  await fetch("/api/role/" + CUR.id, { method: "DELETE" });
  closeDrawer();
  loadRoles();
  toast("已删除");
}

/* ---------------------------------------------------------- 任务 */

/* ---- Seed-VC（44.1kHz 音色微调）----------------------------------------
   它建立在 RVC 歌声模型之上：RVC 负责"唱得上、跟得住调"，Seed-VC 负责"更像这个人"。
   实测（阿塔尼斯 191 段 / 16.1 分钟）：音色 0.7099 → 0.7674，600 步是拐点。
   界面上必须把两件事说死，不许含糊：
     ① 44.1k 只是**容器**提升，有效带宽仍约 12k（16k 以上能量只有原唱的 1/20）；
     ② 微调**绑角色**，换角色要重训。                                        */

let SV_STATE = null;      // 当前角色的 Seed-VC 状态（面板打开时拉一次）

async function refreshSeedvc() {
  if (!CUR) return;
  const rid = CUR.id;
  const box = $("#svState"), sel = $("#svCkpt");
  if (box) box.textContent = "正在看微调状态…";
  let st;
  try {
    st = await api("/api/seedvc/state?role=" + encodeURIComponent(rid));
  } catch (e) {
    if (box) box.textContent = "读不到 Seed-VC 状态：" + (e.message || e);
    if (sel) sel.innerHTML = "";
    SV_STATE = null;
    return;
  }
  if (!CUR || CUR.id !== rid) return;      // 这期间换角色了，答案作废
  SV_STATE = st;
  paintSeedvc();
}

function paintSeedvc() {
  const st = SV_STATE;
  const box = $("#svState"), sel = $("#svCkpt");
  if (!st || !box) return;
  if (!st.ok) {
    box.innerHTML = `<b class="warn">Seed-VC 现在用不了：${esc(st.why)}</b>`;
    if (sel) sel.innerHTML = "";
    return;
  }
  const ds = st.dataset || {};
  const rows = st.ckpts || [];
  if (sel) {
    sel.innerHTML = rows.length
      ? rows.map(r => `<option value="${esc(r.path)}" ${st.selected && st.selected.path === r.path ? "selected" : ""}>${esc(r.label)} · ${esc(r.at)} · ${(r.size / 1073741824).toFixed(1)}GB</option>`).join("")
      : `<option value="">（还没微调过）</option>`;
    sel.disabled = !rows.length;
  }
  // ★ 这里显示的是**角色现有素材**里符合训练口径（时长≥2s、有声比例≥60%）的段数，
  // 不是"上次整理出来的数据集里有多少文件"。后者没跑过微调时恒为 0，
  // 会让"我有 300 多条素材"显示成 0 段（真出过这个 bug）。
  const av = ds.avail || {};
  const mat = `<b>${ds.segs || 0}</b> 段` +
              (ds.secs ? ` / ${(ds.secs / 60).toFixed(1)} 分钟` : "");
  let s;
  if (!rows.length) {
    s = `还没微调过。可用素材：${mat}` +
        (ds.built ? `（已整理过 ${ds.built} 段）` : "") +
        `　→　点「开始微调」训一版（本机约 5~8 分钟）。`;
  } else {
    const cur = st.selected;
    s = `已有 ${rows.length} 档（${rows.map(r => r.label).join(" / ")}）` +
        `，当前唱歌用 <b>${esc(cur ? cur.label : "（没选）")}</b>。` +
        `　可用素材 ${mat}`;
  }
  if (ds.segs && av.capped) {
    s += `　<span class="hint" style="display:inline;margin:0">（一次训练取其中最长的 ${av.used} 段）</span>`;
  }
  for (const w of (st.warn || [])) s += ` <b class="warn">⚠ ${esc(w)}</b>`;
  box.innerHTML = s;
}

async function pickSeedvc() {
  const sel = $("#svCkpt");
  if (!CUR || !sel || !sel.value) return;
  try {
    const j = await post("/api/role/" + CUR.id + "/seedvc-pick", { ckpt: sel.value });
    if (j && j.role) CUR = j.role;
    toast("已切换档位：" + (j.ckpt ? j.ckpt.label : ""), "good");
    await refreshSeedvc();
    loadRoles();
  } catch (e) { toast(e.message, "bad"); }
}

async function doSeedvc() {
  if (busyGuard($("#btnSeedvc"), "#svState")) return;
  const steps = parseInt($("#svSteps").value || "600", 10);
  const box = $("#svState");
  try {
    const j = await post("/api/jobs", { kind: "seedvc", role: CUR.id, steps });
    if (box) box.textContent = `已提交 ${steps} 步微调，看左下角进度…`;
    watchJob(j.job.id, () => {
      loadRoles();
      api("/api/role/" + CUR.id).then(x => { CUR = x.role; });
      refreshSeedvc();
    });
  } catch (e) {
    toast(e.message, "bad");
    refreshSeedvc();
  }
  if ($("#btnSeedvc")) $("#btnSeedvc").disabled = false;
}

async function doTrain() {
  const q = $("#inpQ2").value;
  if (busyGuard(null, "#trainHint")) return;
  const body = { kind: "train", role: CUR.id, quality: q };
  if (q === "custom") {
    const n = parseInt($("#eps2").value || "0", 10);
    if (!(n > 0)) { toast("先填一个大于 0 的轮数", "bad"); return; }
    body.epochs = Math.min(n, 200);
    if (n > 200) toast("说话模型最多 200 轮，已按 200 开始", "bad");
  }
  $("#trainHint").textContent = "已提交，训练完会自动出声体检，看左下角进度…";
  const j = await post("/api/jobs", body);
  watchJob(j.job.id, () => {
    $("#trainHint").textContent = "";
    loadRoles();
    api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); });
  });
}

/* ------------------------------------------------ 语气（在提示词里写）
   老板的写法是直接在句首加标签：[愤怒]你怎么敢这么做！
   所以界面要做的不是给每行配下拉，而是**把他写的标签认出来给他看** ——
   尤其是认不出来的那些，必须当场标红，否则他会以为生效了。

   解析一律问后端 /api/tone/parse，前端不复制规则（两份规则必然走偏）。 */

let TONE_OPT = null;
let TONE_TIMER = null;

async function toneLoad() {
  if (TONE_OPT) return TONE_OPT;
  try { TONE_OPT = await api("/api/tone"); } catch (e) { TONE_OPT = null; return null; }
  const box = $("#toneHelp");
  if (box) {
    const al = TONE_OPT.aliases || {};
    const groups = Object.keys(al).filter(k => k !== "auto").map(k => {
      const nm = ((TONE_OPT.tones || []).find(t => t.key === k) || {}).name || k;
      return `<span class="g"><b>${esc(nm)}</b>：${esc((al[k] || []).join(" / "))}</span>`;
    }).join("");
    box.innerHTML =
      `句首写方括号（中文括号、圆括号、六角括号都认）：<br>` +
      `<code>${esc((TONE_OPT.example || "").replace(/\n/g, "　　"))}</code>` +
      `<div class="tone-alias">${groups}</div>` +
      `<div style="margin-top:6px">句尾或句子中间的括号<b>不会</b>被当成语气 —— ` +
      `那是台词的一部分。多个标签可以叠着写：<code>[低沉][急促]快走</code></div>`;
  }
  return TONE_OPT;
}

function tonePreviewRefresh() {
  clearTimeout(TONE_TIMER);
  TONE_TIMER = setTimeout(tonePreviewDo, 260);
}

async function tonePreviewDo() {
  const box = $("#speakPreview");
  const raw = ($("#speakText") && $("#speakText").value) || "";
  if (!box) return;
  if (!raw.trim()) { box.innerHTML = ""; return; }
  let d;
  try {
    d = await api("/api/tone/parse?script=" + encodeURIComponent(raw));
  } catch (e) { box.innerHTML = ""; return; }
  const rows = (d.lines || []).map((it, i) => {
    let chip;
    if (it.unknown && it.unknown.length) {
      chip = `<span class="tp-tag bad" title="这个语气词不在表里">写的是「${esc(it.unknown.join("、"))}」…没认出来</span>`;
    } else if (it.tone) {
      chip = `<span class="tp-tag">${esc(it.tone_name || it.tone)}</span>`;
    } else {
      chip = `<span class="tp-tag none">平时语气</span>`;
    }
    return `<div class="tp-row"><span class="tp-n">${i + 1}</span>${chip}` +
           `<span class="tp-txt">${esc(it.text)}</span></div>`;
  }).join("");
  const bad = (d.lines || []).filter(x => (x.unknown || []).length).length;
  box.innerHTML = rows +
    (bad ? `<div class="hint" style="color:#e0483c">有 ${bad} 句写的语气没认出来，` +
           `这几句会按平时语气读。上面列表里是每个语气能怎么写。</div>` : "");
}

function speakRoleChanged() {
  toneLoad();
  tonePreviewDo();
}

async function doSpeak() {
  // ★ 把**整段原文**原样交给后端去解析语气，前端不预先剥标签 ——
  //   两边各剥一次的话，规则稍有出入就会拿到被啃过的文本，
  //   而且用户看到的和实际合成的还不一致，最难查。
  const script = $("#speakText").value || "";
  if (!script.trim()) { toast("先写点要说的话", "bad"); return; }
  $("#btnSpeak").disabled = true;
  $("#speakHint").textContent = "合成中…";
  try {
    const j = await post("/api/jobs", { kind: "speak", role: CUR.id, script });
    watchJob(j.job.id, () => {
      $("#speakHint").textContent = "";
      loadRoles();
      api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); });
    });
  } catch (e) {
    toast(e.message, "bad");
    $("#speakHint").textContent = "";
  }
  $("#btnSpeak").disabled = false;
}

/* 变调档位 -> 传给后端的值。
   "auto" 由后端用 RMVPE 量原唱和角色的基频中位数算出来 —— 不是查表猜的，
   所以原曲音域特别高/低时也能给对。 */
function pickedPitch() {
  const sel = $("#selPitch");
  if (!sel) return 0;
  const v = sel.value;
  if (v === "auto") return "auto";
  if (v === "custom") return parseInt($("#inpPitch").value || "0", 10) || 0;
  return parseInt(v, 10) || 0;
}

function onPitchPick() {
  const sel = $("#selPitch");
  const w = $("#pitchCustomWrap");
  if (sel && w) w.style.display = (sel.value === "custom") ? "" : "none";
}

async function doSing(preview) {
  if (!pickedSong) { toast("先选一首歌", "bad"); return; }
  const body = {
    kind: "sing", role: CUR.id, song: pickedSong,
    pitch: pickedPitch(),
    index_rate: parseFloat($("#inpIdx").value || "0.75"),
    engine: ($("#selEngine") && $("#selEngine").value) || "rvc",
  };
  // 试听小样：只跑一小段（后端会分离整首但有缓存），几十秒到一两分钟就能听。
  // 关键差别是它**不写进角色成品清单** —— 只是给耳朵听个响。
  if (preview) body.preview_secs = 30;
  try {
    const j = await post("/api/jobs", body);
    watchJob(j.job.id, () => {
      loadRoles();
      api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); });
    });
  } catch (e) { toast(e.message, "bad"); }
}

/* ---- 唱歌页的两个来源：翻唱 / 写新歌 ----
   切换只动 display：两个 block 都在 DOM 里，来回切不丢用户已经填的内容
   （填了半页歌词切过去看一眼再切回来，内容没了会让人想砸键盘）。 */
function showSingMode(m) {
  const cover = $("#singBlock"), nw = $("#newSongBlock");
  const bc = $("#smCover"), bn = $("#smNew");
  if (!cover || !nw) return;
  const isNew = (m === "new");
  cover.style.display = isNew ? "none" : "";
  nw.style.display = isNew ? "" : "none";
  if (bc) bc.classList.toggle("on", !isNew);
  if (bn) bn.classList.toggle("on", isNew);
  if (isNew) refreshYue2();
}

/* 引擎状态。**只读**：界面刷新一次不该顺手拉起一个引擎进程。 */
async function refreshYue2() {
  const box = $("#yue2State");
  if (!box) return;
  try {
    const s = await api("/api/yue2/status");
    if (s.ready) {
      box.innerHTML = "✅ " + esc(s.text) + " · 输出 " + esc((s.formats || []).join("/"));
      box.style.color = "var(--ok, #4caf50)";
    } else {
      box.innerHTML = "⚠️ " + esc(s.text) +
        (s.missing && s.missing.length
          ? "<br>缺件：" + esc(s.missing.join("；"))
          : "<br>第一次跑会自动拉起引擎，稍等十几秒。");
      box.style.color = "var(--warn, #e8a33d)";
    }
  } catch (e) {
    box.textContent = "查不到 YuE2 状态：" + e.message;
  }
}

async function doNewSong() {
  if (!CUR) { toast("先选一个角色", "bad"); return; }
  const style = ($("#nsStyle").value || "").trim();
  const lyrics = ($("#nsLyrics").value || "").trim();
  if (!style) { toast("风格不能空", "bad"); return; }
  if (!lyrics) { toast("歌词不能空 —— 没有词它只会哼", "bad"); return; }
  const body = {
    kind: "new_song", role: CUR.id,
    style: style, lyrics: lyrics,
    duration: parseFloat($("#nsDur").value || "120") || 120,
    pitch: parseInt($("#nsPitch").value || "0", 10) || 0,
    index_rate: parseFloat($("#nsIdx").value || "0.75"),
  };
  const btn = $("#btnNewSong");
  if (btn) { btn.disabled = true; btn.textContent = "提交中…"; }
  try {
    const j = await post("/api/jobs", body);
    toast("开始了：作曲 → 换声 → 混音", "ok");
    watchJob(j.job.id, () => {
      loadRoles();
      api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); });
    });
  } catch (e) {
    toast(e.message, "bad");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "写歌并用这个角色唱"; }
  }
}

/* ---- 歌声模型的「已经训到第几轮」 ----
   RVC 的轮数语义是「训到第 N 轮」而不是「再训 N 轮」，train.py 又会自动从
   logs/<exp>/G_*.pth 续训；所以「已经多少轮」是用户能不能接着训的关键信息。
   轮数写在 ckpt 里（-l 1 时文件名固定 G_2333333.pth，从名字读不出来），
   只有 RVC 自己的 python 打得开 —— 一次约 3 秒，所以只在打开训练页时问一次。 */

async function refreshRvcState() {
  if (!CUR) return;
  const rid = CUR.id;
  const box = $("#rvcState");
  const cached = RVC_TRAINED[rid] !== undefined;
  if (box && !cached) box.textContent = "正在查歌声模型已经训到第几轮…（要起一次 RVC 环境，约 3 秒）";
  let n = 0;
  try {
    n = await rvcTrainedOf(rid);
  } catch (e) {
    if (box) box.textContent = "查不到已训轮数（不影响训练，按没训过处理）。";
    return;
  }
  if (!CUR || CUR.id !== rid) return;      // 这期间用户换角色了，答案作废
  CUR._rvcTrained = n;
  paintRvcState();
}

/* 界面上当前选的歌声训练目标轮数。与 doRvc() 用同一套口径，
   免得"提示说的"和"真提交的"不是同一个数。 */
function rvcTargetRounds() {
  const q = $("#inpQ3").value;
  if (q !== "custom") return ({ fast: 50, standard: 150, perfect: 300 })[q] || 150;
  const v = parseInt($("#eps3").value || "0", 10);
  return Math.min(v > 0 ? v : 600, 5000);
}

function paintRvcState() {
  const box = $("#rvcState");
  if (!box) return;
  const n = CUR ? CUR._rvcTrained : null;
  if (n === null || n === undefined) return;
  const target = rvcTargetRounds();
  let s;
  if (n > 0) {
    s = `已训到 <b>${n}</b> 轮。RVC 能接着训：把轮数填到 ${n} 以上` +
        `（比如 ${Math.max(n + 50, n * 2)}），会从第 ${n} 轮往下练。`;
    if (target <= n) {
      s += ` <b class="warn">当前目标是 ${target} 轮 ≤ 已训的 ${n} 轮，` +
           `没有额外轮数可训，会被拦下。</b>`;
    }
  } else {
    s = "这个实验还没训过，会从头训。";
  }
  // ★ 没底模训出来的模型"不会跟旋律唱"——这是"唱出来没法听"最常见的根因。
  //   摆在界面上，用户才不会把它当成自己素材不行（那样他会白忙很久）。
  if (CUR && CUR.rvc && CUR.rvc.pretrained === false) {
    s += ` <b class="warn">⚠ 这个模型是没用底模、从零训的：输出不跟旋律、变调可能完全没反应。` +
         `勾选下面的「从头重训（换用底模）」可以修好它。</b>`;
  }
  box.innerHTML = s;
}

async function doRvc() {
  const q = $("#inpQ3").value;
  if (busyGuard(null, "#rvcInfo")) return;
  const body = { kind: "rvc", role: CUR.id, quality: q,
                 restart: !!($("#rvcRestart") && $("#rvcRestart").checked) };
  // 从零训的模型必须"从头重训"才能用上底模（RVC 只在没有旧断点时才加载 -pg）。
  // 这里先拦一道：等训练起了进程才报错的话，用户只会在几十分钟后才发现白等。
  if (CUR.rvc && CUR.rvc.pretrained === false && CUR._rvcTrained > 0 && !body.restart) {
    toast("这个模型是没用底模从零训的，直接续训底模依然不会生效。" +
          "请勾选「从头重训（换用底模）」，或取消训练。", "bad");
    const cb = $("#rvcRestart");
    if (cb) { cb.checked = true; cb.closest("label").classList.add("hl"); }
    return;
  }
  let rounds = 0;
  if (q === "custom") {
    const n = parseInt($("#eps3").value || "0", 10);
    if (!(n > 0)) { toast("先填一个大于 0 的轮数", "bad"); return; }
    rounds = Math.min(n, 5000);
    if (n > 5000) toast("最多 5000 轮，已按 5000 开始", "bad");
    body.epochs = rounds;
  } else {
    rounds = ({ fast: 50, standard: 150, perfect: 300 })[q] || 150;
  }
  // 已训轮数是问过的就直接在本地拦，省得等训练起进程再报错（而且报错文案
  // 藏在任务日志里，用户得自己去翻）。没好就放行，让后端兜底。
  const done = CUR._rvcTrained;
  if (done && rounds <= done) {
    const sug = Math.max(done + 50, done * 2);
    toast(`已经训到 ${done} 轮了，目标 ${rounds} 轮没有更多可训的轮数。`
          + ` 改用「自定义」填 ${sug} 就能接着往下训。`, "bad");
    $("#inpQ3").value = "custom";
    $("#epsWrap3").style.display = "";
    $("#eps3").value = String(sug);
    paintRvcState();
    return;
  }
  if (!confirm(`训练歌声模型 ${rounds} 轮通常要几十分钟到几小时，期间显卡会被占满。要开始吗？`)) return;
  $("#btnRvc").disabled = true;
  $("#rvcInfo").textContent = `已提交 ${rounds} 轮…`;
  try {
    const j = await post("/api/jobs", body);
    watchJob(j.job.id, () => {
      $("#rvcInfo").textContent = "";
      loadRoles();
      delete RVC_TRAINED[CUR.id];            // 刚训过，轮数变了
      api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); refreshRvcState(); });
    });
  } catch (e) { toast(e.message, "bad"); $("#rvcInfo").textContent = ""; }
  $("#btnRvc").disabled = false;
}

/* ---------------------------------------------------------- 任务面板 */

/* watching 是「本页面正在盯的任务」。
   它以前是纯内存 Map，页面一刷新就清空，而 tickJobs 开头 `if (!watching.size) return`
   会直接停掉 —— 于是刷新一下，正在跑的训练就从界面上彻底消失了，
   后端还在写 ckpt、GPU 还在转，用户却什么都看不到。
   现在把 id 记进 localStorage，页面加载时重新拉回来接上轮询。*/
const watching = new Map();

/* 每类任务主要吃哪种资源。
   为什么要标：用户提交的任务"等了很久"时，他看到的是一个不动的进度条，
   不知道是排队、还是卡死、还是被别的任务挤着。把"谁在占着什么"写出来，
   他至少知道该等谁。 */
const JOB_RES = {
  rvc: ["GPU 训练", "训练会把显存占满，这期间跑合成会明显变慢"],
  seedvc: ["GPU 微调", "训练会把显存占满，这期间跑合成会明显变慢"],
  train: ["GPU 训练", "训练会把显存占满，这期间跑合成会明显变慢"],
  sing: ["GPU 推理", "要跟训练抢显存"],
  speak: ["GPU 推理", "短，但也要排 GPU 的队"],
  lyrics: ["GPU 识别", "首次要下 1.5GB 模型"],
  new_song: ["GPU 作曲", "先作曲再换声，两头都吃显卡"],
  install_model: ["网络下载", "不吃显卡"],
  viz: ["CPU 渲染", "不吃显卡，但要占 CPU"],
  ingest: ["CPU+GPU 拆解", "分离要用显卡"],
};

function jobRes(kind) {
  return JOB_RES[kind] || null;
}
const LS_JOBS = "vs.watching";

function lsSave() {
  try { localStorage.setItem(LS_JOBS, JSON.stringify(Array.from(watching.keys()))); }
  catch (e) { /* 隐私模式下写不了，内存里照常用 */ }
}
function lsLoad() {
  try { return JSON.parse(localStorage.getItem(LS_JOBS) || "[]") || []; }
  catch (e) { return []; }
}
function lsForget(id) {
  try {
    const a = lsLoad().filter(x => x !== id);
    localStorage.setItem(LS_JOBS, JSON.stringify(a));
  } catch (e) { /* 同上 */ }
}

function watchJob(id, onDone) {
  watching.set(id, { off: 0, done: onDone || null, box: null, miss: 0, toasted: false, alive: true });
  lsSave();
  ensureJobUI(id);
  tickJobs();
}

/* 重新挂上所有未结束的任务。
   服务可能还在跑，也可能早就跑完了 —— 两种情况都要能显示，
   区别只在于状态文字，不在于看不看得见。*/
async function restoreJobs() {
  let jobs = [];
  /* ★ 这里原本是 `api("/api/jobs")` 拉全量再 `slice(0,30)`，有两个毛病：
       ① 为了"问一句哪些还活着"传了 819 KB（1030 条历史），只用得上几个；
       ② 更要命：下面"localStorage 里记着的、不在 jobs 里的任务要清掉"这句，
          比的是**截断后**的 30 条 ⇒ 一个还在跑、但排不进最近 30 条的老任务，
          刷新页面就被误判成死任务删掉，追踪直接断了。
       改成让后端按状态过滤，两个问题一起解决。 */
  try { jobs = (await api("/api/jobs?alive=1")).jobs || []; }
  catch (e) { return; }
  const alive = jobs;                       // 后端已过滤，且不受 30 条限制
  // localStorage 里记着的、后端已经不认的，清掉 —— 否则下次刷新还会去问一个死任务
  for (const id of lsLoad()) {
    if (!jobs.some(j => j.id === id)) {
      watching.delete(id);
      lsForget(id);
    }
  }
  for (const j of alive) {
    if (!watching.has(j.id)) {
      watching.set(j.id, { off: 0, done: null, box: null, miss: 0, toasted: false, alive: true });
    }
    ensureJobUI(j.id);
  }
  // ★ 历史任务**最多只补 2 张**，而且不合起来弹一串 toast。
  //   以前这里是「3 张 interrupted + 5 张其它」= 8 张卡一起糊在左下角：
  //   抽屉从屏幕底部往上堆到 y≈430，把角色卡、训练页签、曲库里的下载按钮
  //   全压在下面 —— 真鼠标按坐标点就打到了任务卡上（命中=step ok / meta），
  //   表现为"点了没反应"。这不是推测：所有失效的点击都在左下那一带。
  //   ⇒ 只把还在跑的接回来（那才是刷新时真正会担心"跑丢了"的东西），
  //     最多再留 2 张最近的已结束的当"刚才那次去哪了"的入口。
  // ★ 已结束的那两张必须**另外**再问一次，不能从 `alive` 里筛。
  //   后端 `alive=1` 只回 running/queued（当初就是为了不再传 800 KB 才改成按状态过滤的），
  //   于是下面这行的 `fin` 恒为空 ⇒ 刷新页面后"刚才那次跑哪了"的入口一张都没有
  //   （2026-10-19 实测：抽屉里 0 张已结束的卡，代码里写着要留 2 张却从来没生效过）。
  //   补一次 `limit=12` 的小请求（十几条，几 KB）专门拿最近已结束的。
  let fin = [];
  try {
    fin = ((await api("/api/jobs?limit=12")).jobs || [])
      .filter((x) => x.state !== "running" && x.state !== "queued");
  } catch (e) { fin = []; }
  // ★ 只补**最近那一条**（原为 2 条）。
  //   实测事故：补 2 张之后，左下角抽屉多出约 150px，把页面上同在左下区域的
  //   可点元素（市场里的作品 chip 就在那一带）盖住了 —— 真鼠标按坐标点下去，
  //   命中的是任务卡的头部（className 是 jh）或步骤说明（nt），表现为
  //   「点了没反应」。门禁 verify_market_ui 就是这么红的（4 条判据全是 0 张卡，
  //   因为 chip 根本没被点到，搜索压根没发起）。
  //   ⇒ 已结束的卡是「刚才那一次去哪了」的入口，价值在于**能找到**，
  //     不在于摊在面板上占地方；真的要找更多，顶部「任务」按钮里有完整列表。
  for (const j of fin.slice(0, 1)) {
    showJobOnce(j.id);
    const el = $("#job-" + j.id);
    if (el) el.dataset.finished = "1";
  }
  const nInter = fin.filter(x => x.state === "interrupted").length;
  if (nInter) {
    toast("有 " + nInter + " 个任务上次被服务重启打断了（可以点任务面板翻）", "bad");
  }
  paintJobBadge();
  if (watching.size) tickJobs();
}

/* 抽屉里已结束的卡片超过这么多张就自动撤掉最旧的。
   为什么要撤：它们不再提供任何信息（进度已停），却一直在占着屏幕，
   越积越高，最后把真正能点的东西压到下面去。 */
const JOBS_KEEP_FINISHED = 3;

function pruneJobCards() {
  const host = $("#jobs");
  if (!host) return;
  // DOM 顺序 = 时间顺序（新任务 appendChild 在后面），最旧的在前
  const fin = Array.from(host.querySelectorAll(".jobbox[data-finished='1']"));
  for (let i = 0; i < fin.length - JOBS_KEEP_FINISHED; i++) {
    const id = (fin[i].id || "").replace("job-", "");
    fin[i].remove();
    if (id && !watching.get(id)) continue;
    // 只有"确实结束了"的才从轮询里摘掉，避免删掉还在跑的任务
    if (id) { watching.delete(id); lsForget(id); }
  }
}

/* ---- 通用确认框 ----
   取消任务这种"点了就没了"的动作必须问一句。不用原生 confirm() 是因为
   它一弹出整个页面就冻住（正在刷的任务进度也跟着停），而且没法带
   "同时删掉中间产物"这种勾选项。自己画一个，按钮都是普通 DOM，
   验收脚本也能真的去点它。*/
function askConfirm(opt) {
  return new Promise((resolve) => {
    const m = $("#maskConfirm");
    if (!m) { resolve(!!window.confirm(opt.body || opt.title || "确定？")); return; }
    $("#cfTitle").textContent = opt.title || "确认";
    $("#cfBody").textContent = opt.body || "";
    const wrap = $("#cfChkWrap"), cb = $("#cfChk");
    if (opt.checkbox) {
      wrap.style.display = "";
      $("#cfChkText").textContent = opt.checkbox;
      cb.checked = !!opt.checkboxDefault;
    } else {
      wrap.style.display = "none";
      cb.checked = false;
    }
    const yes = $("#cfYes"), no = $("#cfNo");
    yes.textContent = opt.okText || "确定";
    yes.className = "btn sm " + (opt.danger === false ? "pri" : "danger");
    openMask("maskConfirm");

    const done = (v) => {
      yes.onclick = no.onclick = null;
      m.onclick = null;
      document.removeEventListener("keydown", onKey);
      closeMask("maskConfirm");
      resolve(v ? { ok: true, checked: cb.checked } : { ok: false, checked: false });
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(false);
      else if (e.key === "Enter") done(true);
    };
    yes.onclick = () => done(true);
    no.onclick = () => done(false);
    // 点遮罩空白处 = 取消（和关闭按钮一个意思，别让人非得做选择才能退出）
    m.onclick = (e) => { if (e.target === m) done(false); };
    document.addEventListener("keydown", onKey);
    // 把焦点给「取消」：手滑敲回车不至于直接把任务干掉
    if (no.focus) no.focus();
  });
}

/* 已结束的任务：也留一条在面板上（收起的），免得用户刷新后完全查不到上次结果。*/
function showJobOnce(id) {
  if ($("#job-" + id)) return;
  ensureJobUI(id, true);
  watching.set(id, { off: 0, done: null, box: null, miss: 0, toasted: true, keep: true, alive: false });
}

const JOB_STATE = {
  queued: "排队", running: "运行中", done: "完成",
  failed: "失败", interrupted: "已中断", cancelled: "已取消",
};

function ensureJobUI(id, collapsed) {
  if ($("#job-" + id)) return;
  const d = document.createElement("div");
  d.className = "jobbox" + (collapsed ? "" : " open");
  d.id = "job-" + id;
  d.innerHTML = `
    <div class="jh" onclick="this.parentNode.classList.toggle('open')">
      <span class="st queued" id="js-${id}">排队</span>
      <b id="jt-${id}">任务</b>
      <span class="jres" id="jr-${id}" title="这个任务主要吃哪种资源"></span>
      <span class="mini jpause" id="jpz-${id}" title="暂停（显卡会立刻空出来）"
            style="display:none"
            onclick="event.stopPropagation();togglePause('${id}')">⏸</span>
      <span class="mini jcancel" id="jcx-${id}" title="取消（终止进程，已经跑的部分不要了）"
            style="display:none"
            onclick="event.stopPropagation();askCancel('${id}')">✕</span>
      <span class="mini jhide" id="jm-${id}" title="收起（任务照常在后台跑，随时点标题能再看回来）"
            onclick="event.stopPropagation();minimizeJob('${id}')">－</span>
      <span class="mini jclose" title="从面板移除（不影响后台任务，刷新后仍可查）"
            onclick="event.stopPropagation();dismissJob('${id}')">×</span>
    </div>
    <div class="bar"><i id="jb-${id}" style="width:0%"></i></div>
    <div class="jmeta" id="jn-${id}"></div>
    <div class="steps" id="jp-${id}"></div>
    <div class="jb"><pre class="log" id="jl-${id}"></pre></div>`;
  const host = $("#jobs");
  host.appendChild(d);
  // 抽屉现在是可滚动的（任务一多就超出屏幕），新任务插在最后 ——
  // 不主动滚到底的话，用户会盯着一片旧任务，而刚提交的那个在下面看不见。
  host.scrollTop = host.scrollHeight;
  paintJobBadge();
}

/* 「×」以前直接 remove() 了 DOM，但 watching 里那条还在跑 ——
   结果是任务还在后台跑，界面上却再也找不回来。
   现在拆成两个动作：收起（还能点标题看回来），从面板移除（记进 localStorage 的忽略表，
   刷新后仍能在任务中心里找到）。两个都不影响后台任务。*/
const DISMISSED = new Set();
function minimizeJob(id) {
  const el = $("#job-" + id);
  if (el) el.classList.remove("open");
}
function dismissJob(id) {
  const el = $("#job-" + id);
  if (el) el.remove();
  watching.delete(id);
  DISMISSED.add(id);
  lsForget(id);
  paintJobBadge();
}
/* 一键把所有展开的任务卡收起来（入口只在真展开着的时候出现，见 .jobs .jall）。
   为什么要这个：任务面板是 fixed 浮层，展开两三张卡就能盖住半屏内容，
   想让它让开就得一张张点标题 —— 把"收起来"这个高频动作直接摆到手边。*/
function jobsCollapseAll() {
  document.querySelectorAll("#jobs .jobbox.open")
    .forEach((b) => b.classList.remove("open"));
}

/* ---- 暂停 / 继续 ----
   任务的重活全在子进程里（RVC 推理要占满显卡十几分钟）。暂停必须是"真的把它
   挂起"，不是"界面上不动了" —— 后端用 NtSuspendProcess 挂起整棵进程树，
   所以点下去之后显卡/CPU 会立刻空出来。
   按钮状态只信服务端回的 job.paused，不在前端自己猜（猜就会和真实状态打架）。*/
async function togglePause(id) {
  const w = watching.get(id);
  const nowPaused = !!(w && w.paused);
  const btn = $("#jpz-" + id);
  if (btn) btn.disabled = true;
  try {
    const d = await post(`/api/job/${id}/${nowPaused ? "resume" : "pause"}`, {});
    if (!d.ok) toast(d.msg || "操作没成功", "bad");
    else {
      if (w) w.paused = !nowPaused;
      toast(d.msg || (nowPaused ? "已继续" : "已暂停"), "good");
    }
    if (d.job) paintPauseBtn(id, d.job);
  } catch (e) { toast(e.message, "bad"); }
  finally { if (btn) btn.disabled = false; }
}

function paintPauseBtn(id, j) {
  const b = $("#jpz-" + id);
  const x = $("#jcx-" + id);
  const live = j.state === "running" || j.state === "queued";
  if (b) {
    b.style.display = live ? "" : "none";
    if (live) {
      b.textContent = j.paused ? "▶" : "⏸";
      b.title = j.paused
        ? "继续（子进程从停下的地方接着跑）"
        : "暂停（子进程连子孙一起挂起，显卡会立刻空出来）";
      b.classList.toggle("paused", !!j.paused);
    }
  }
  // 取消按钮和暂停按钮同生共死：只有活着（含暂停中）的任务才谈得上取消。
  if (x) x.style.display = live ? "" : "none";
}

/* ---- 取消 ----
   暂停是"挂起、还能接着跑"，取消是"彻底不要了"：进程树直接终止，
   已经跑出来的中间产物也不再保留。
   这两个按钮挨在一起、长得也像，所以点下去必须先把区别说清楚再动手 ——
   否则用户以为只是停一下，回来发现进度没了。*/
async function askCancel(id) {
  const w = watching.get(id);
  const name = (w && w.title) || ($("#jt-" + id) || {}).textContent || "这个任务";
  const a = await askConfirm({
    title: "取消这个任务？",
    body: `「${name}」\n\n` +
      "取消会**立刻终止**它的进程（连子进程一起），已经跑出来的部分不再保留。\n" +
      "这跟「暂停」不一样：暂停是挂起，随时能接着跑；取消是不要了，得从头再来。",
    checkbox: "同时删掉这个任务已经产生的中间文件（省空间）",
    okText: "取消任务",
  });
  if (!a.ok) return;
  const btn = $("#jcx-" + id);
  if (btn) btn.disabled = true;
  try {
    const d = await post(`/api/job/${id}/cancel`, { cleanup: !!a.checked });
    if (!d.ok) toast(d.msg || "没能取消", "bad");
    else toast(d.msg || "已取消", "good");
    if (d.job) {
      if (w) w.paused = false;
      paintPauseBtn(id, d.job);
      const st = $("#js-" + id);
      if (st) { st.textContent = JOB_STATE[d.job.state] || d.job.state; }
    }
  } catch (e) {
    toast("取消失败：" + e.message, "bad");
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* 顶部「任务」按钮上的角标：有在跑的就要看得见。
   否则用户把抽屉一关，只能靠左下角那个小角落找进度 —— 这就"关了就没法确认"。*/
function paintJobBadge() {
  const b = $("#btnJobs");
  if (!b) return;
  const n = Array.from(watching.values()).filter(w => !w.keep).length;
  b.style.display = watching.size ? "" : "none";
  $("#jobCount").textContent = n ? String(n) : "";
  b.title = n ? ("● " + n + " 个任务正在进行") : "查看全部任务";
}

async function openJobCenter() {
  const box = $("#jobCenter");
  const old = $(".poplist");
  if (old) old.remove();
  // 按需拉：以前靠 tickJobs 每 1.6 秒全量刷（822 KB/次），这里只是顺手用现成的。
  // 改成打开面板才拉一次，并且只取最近 30 条 —— 面板本来也只画这么多。
  // 变量名叫 jr 不叫 r：下面这个作用域里已经有 `const r = box.getBoundingClientRect()`，
  // 同名虽合法（块作用域遮蔽），但静态自检会判成"重复声明" ⇒ 别给自己找麻烦。
  try {
    const jr = await api("/api/jobs?limit=30");
    window.__lastJobs = jr.jobs || [];
  } catch (e) { /* 拉不到就沿用上一次的 */ }
  const jobs = window.__lastJobs || [];
  const rows = jobs.length ? jobs.map(j => `
    <div class="opt" data-jid="${j.id}">
      <span class="st ${j.state}">${esc(JOB_STATE[j.state] || j.state)}</span>
      ${esc(j.title || j.kind)}
      <span class="jm">${esc(j.detail || j.stage || "")}
        ${j.elapsed ? " · 用时 " + fmtSec(j.elapsed) : ""}
        ${j.eta ? " · 约剩 " + fmtSec(j.eta) : ""}</span>
    </div>`).join("")
    : '<div class="opt" style="opacity:.6">还没有任何任务</div>';
  const pop = document.createElement("div");
  pop.className = "poplist jc";
  pop.innerHTML = rows;
  const r = box.getBoundingClientRect();
  pop.style.cssText = `position:fixed;top:58px;right:18px;z-index:70;width:min(460px,calc(100% - 36px));` +
    `max-height:420px;overflow:auto`;
  document.body.appendChild(pop);
  pop.addEventListener("click", (e) => {
    const it = closestEl(e.target, "[data-jid]");
    if (!it) return;
    const id = it.dataset.jid;
    pop.remove();
    const el = $("#job-" + id);
    if (el) { el.classList.add("open"); el.scrollIntoView({ block: "nearest" }); }
    else { ensureJobUI(id); el = $("#job-" + id); if (el) el.classList.add("open"); }
    watching.delete(DISMISSED.has(id) ? id : "__none__");
    DISMISSED.delete(id);
    watching.set(id, { off: 0, done: null, box: null, miss: 0, toasted: true });
    lsSave();
    tickJobs();
  });
  setTimeout(() => document.addEventListener("click", function h(ev) {
    if (!pop.contains(ev.target)) { pop.remove(); document.removeEventListener("click", h); }
  }), 0);
}

/* 秒 → 中文时长。**先取整再拆分**——直接对小数取模再 round 会算出
   "4 分 60 秒"（299.6 秒 ⇒ floor(299.6/60)=4，round(299.6%60)=60）。
   【踩过的坑】这个函数曾经在同一个作用域里**声明了两遍**，后一份胜出、
   前一份是死代码 ⇒ 改前一份完全不生效。已删掉重份，只留这一处。*/
function fmtSec(s) {
  s = Math.round(Number(s) || 0);
  if (s < 60) return s + " 秒";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h) return h + " 小时 " + m + " 分";
  return m + " 分 " + (s % 60) + " 秒";
}

/* 每一段清单。用户要的就是这个：不再是一大段日志刷屏，
   而是一眼能看 —— 第几步在做、哪几步已完成、卡在哪一步、为什么。*/
function renderSteps(id, steps, state) {
  const box = $("#jp-" + id);
  if (!box) return;
  if (!steps || !steps.length) { box.innerHTML = ""; return; }
  // 四态，别把"还没开始"画成"正在进行" —— 那样用户会以为流程停在这一步了。
  const META = {
    done: { mk: "✓", cls: "ok" },
    fail: { mk: "✗", cls: "no" },
    doing: { mk: "●", cls: "now" },
    todo: { mk: "○", cls: "wait" },
    cancel: { mk: "✕", cls: "cancel" },
  };
  box.innerHTML = steps.map(s => {
    const m = META[s.state] || META.todo;
    // ★「黑盒等待」的解药：正在做的那一步要说人话 + 说还要多久。
    //   只给步骤名（"语音识别"）配一条不动的进度条，用户会以为卡住了。
    //   只给正在做的那一步 —— 全部都给的话，完成态那几行也跟着变长，反而找不着重点。
    const doing = s.state === "doing";
    const hint = doing && s.hint ? esc(s.hint) : "";
    const eta = doing && s.eta ? " · 约需 " + fmtSec(s.eta) : "";
    return `<div class="step ${m.cls}">
      <span class="mk">${m.mk}</span>
      <span class="nm">${esc(s.name)}</span>
      ${hint ? `<span class="sh">${hint}<b class="seta">${eta}</b></span>` : ""}
      ${s.note ? `<span class="nt">${esc(s.note)}</span>` : ""}
    </div>`;
  }).join("");
  // 正在做的那一步滚进视野
  // 【踩过的坑】这一行声明曾经整行不见，只剩下面那句 if (cur && ...)。
  // 于是 renderSteps 一被调用就 ReferenceError —— "每步清单"整块画不出来
  // （任务框是空的），而任务本身在后端跑得好好的。
  // 这是同一轮里第三处"短声明行被删"（另两处：shade 的 n、STAGE_SONG）。
  // 语法检查、后端接口、文件存在性全都是绿的 —— 只有真跑页面才看得见。
  const cur = box.querySelector(".step.now");
  if (cur && cur.scrollIntoView) cur.scrollIntoView({ block: "nearest" });
}

async function tickJobs() {
  if (!watching.size) { paintJobBadge(); return; }
  let active = 0;
  for (const [id, w] of Array.from(watching)) {
    let j;
    try { j = (await api("/api/job/" + id)).job; w.miss = 0; }
    catch (e) {
      // 连续拉不到状态（服务重启/断网）就把界面解锁，不能让人干等一个死任务；
      const meta = $("#jn-" + id);
      if (meta) {
        meta.textContent = w.miss >= 3
          ? "联系不上服务（可能重启过）。任务可能已被中断，刷新页面可看最终状态。"
          : "正在重连…（" + w.miss + "次）";
        meta.className = "jmeta warn";
      }
      if (w.miss >= 10) {
        try { if (w.done) w.done(); } catch (e2) { /* 回调自身出错就算了 */ }
        watching.delete(id);
        lsForget(id);
        const el = $("#job-" + id);
        if (el) el.classList.add("orphan");
      }
      continue;
    }
    // 【踩过的坑】这一行原本漏了第 4 个绑定 `meta = $("#jn-" + id)`，
    // 而下面 678 行写的是 `if (meta) { ... }`。于是 tickJobs 每轮跑到那里就
    // ReferenceError —— 而它**抛在"任务完成"分支之前**（698 行），后果是：
    //   ① 任务永远显示不出"完成"，watching 永不清空 ⇒ busyGuard 永久拦住新任务；
    //   ② 所有 watchJob 的完成回调（导歌填②号槽、训练完刷新角色卡）全都不触发。
    // 这是同一轮里第 5 处"短声明行被删"。语法检查、后端接口、文件存在性全绿，
    // 只有真跑页面 + 真的等一个任务结束才看得见（本轮就是靠盯②号槽才抓到）。
    const st = $("#js-" + id), pre = $("#jl-" + id), bar = $("#jb-" + id),
          meta = $("#jn-" + id);
    w.alive = (j.state === "running" || j.state === "queued");
    w.kind = j.kind; w.jtitle = j.title || j.kind;
    const jr = $("#jr-" + id), _res = jobRes(j.kind);
    if (jr) {
      // 只在任务还活着时才标资源 —— 跑完的任务再挂个"GPU 训练中"是自相矛盾。
      jr.textContent = (w.alive && _res) ? _res[0] : "";
      jr.title = _res ? _res[1] : "";
      jr.style.display = (w.alive && _res) ? "" : "none";
    }
    if (st) {
      // 暂停是"用户主动停的"，不能只显示"运行中" —— 否则他看着一个不动
      // 的进度条会以为卡死了，然后再点一次暂停（反而变成继续）。
      st.textContent = j.paused ? "已暂停" : (JOB_STATE[j.state] || j.state);
      st.className = "st " + (j.paused ? "paused" : j.state);
    }
    paintPauseBtn(id, j);
    if ($("#jt-" + id)) $("#jt-" + id).textContent = (j.title || j.kind) + (j.stage ? " · " + j.stage : "");
    if (bar) bar.style.width = (j.pct || (j.state === "done" ? 100 : 0)) + "%";
    // 已用时间 + 预计剩余。用户判断"还要等多久、是不是卡死了"全靠这两行。
    if (meta) {
      const bits = [];
      if (j.paused) bits.push("<b>已暂停</b>（子进程已挂起，显卡空出来了；点 ⏸ 变 ▶ 继续）");
      if (j.detail) bits.push(esc(j.detail));
      // 「已训到 N 轮 / 目标 M 轮」：只写"共 300 轮"的话，用户看不出还剩多少。
      if (j.epochs_total) {
        bits.push(j.epochs_done
          ? "已训到 <b>" + j.epochs_done + "</b> / " + j.epochs_total + " 轮"
          : "目标 " + j.epochs_total + " 轮");
      }
      if (j.elapsed) bits.push("已用 " + fmtSec(j.elapsed));
      if (j.eta) bits.push("预计还需 " + fmtSec(j.eta));
      // 训练面板的预计耗时旁，把已知限制如实说出来：
      // 用户看到"预计还需 40 分钟"，下一个问题一定是"能不能快点"。
      // 现在诚实的答案是"加速档还在验证"——而不是偷偷换档、拿质量换速度。
      if (j.epochs_total && j.eta) {
        bits.push("<span style='color:var(--txt3)'>当前为稳定档（FP32）；加速档在验证中</span>");
      }
      if (j.state === "interrupted") bits.push("服务重启，任务在这一步被中断");
      // 取消是"我不要了"，不是"它坏了" —— 说清楚才不会有人对着自己取消的
      // 任务去翻日志找 bug。
      if (j.state === "cancelled") bits.push("<b>已取消</b>（进程已终止，要再来一次得从头跑）");
      meta.innerHTML = bits.join(" · ");
      meta.className = "jmeta" + (j.state === "interrupted" ? " warn" : "");
    }
    // 每一步都画出来（这是"看见每一个步骤"的正身）
    renderSteps(id, j.steps, j.state);
    try {
      const l = await api(`/api/job/${id}/log?offset=${w.off}`);
      if (l.text) {
        pre.textContent += l.text;
        pre.parentNode.scrollTop = pre.parentNode.scrollHeight;
        w.off = l.offset;
      }
    } catch (e) { /* 日志拉不动不影响主流程 */ }
    // ★ cancelled 必须和 done/failed 一样算"终态"：不算的话，
    //   取消完的任务会一直挂在 watching 里被轮询，角标数字永远下不来。
    if (j.state === "done" || j.state === "failed" || j.state === "interrupted"
        || j.state === "cancelled") {
      active--;
      if (j.error && j.state !== "done" && j.state !== "cancelled") {
        // 取消不是失败：别把它当成"任务失败"弹红条
        if (!w.toasted) { pre.textContent += "\n失败原因：" + j.error + "\n"; toast("任务失败：" + j.error.slice(0, 60), "bad"); }
      }
      // 部分失败：任务整体跑完了，但有角色没训出来 —— 必须说清楚，不能只弹"任务完成"。
      else if (j.failed_roles && j.failed_roles.length) {
        const names = j.failed_roles.map(f => f.name).join("、");
        toast("跑完了，但 " + j.failed_roles.length + " 个角色训练失败：" + names, "bad");
      }
      else if (j.state === "done" && !w.toasted && !w.keep) toast("任务完成", "good");
      w.toasted = true;
      if (w.done) w.done();
      // 结束的任务*留在面板上*（收起来），不 delete 了 DOM。
      // 以前一结束就 watching.delete，界面上什么都不剩 —— 用户事后想查
      // "刚才那次训练到底第几步失败"就没得查了。
      // "刚才那次训练到底第几步失败"就没得查了。
      lsForget(id);
      const el = $("#job-" + id);
      if (el && !el.dataset.finished) {
        // ★ 只在"这一轮刚跑完"时收起一次，**不能每轮都收**。
        //   踩过的坑：以前这里是无条件 remove('open')，而 tickJobs 每 1.6 秒
        //   跑一轮 ⇒ 用户点开已结束的任务想看失败原因，最长 1.6 秒后又被
        //   收起来，来回闪、根本读不完（老板那张"错误详情"截图是抢在下一轮
        //   tick 之前截到的）。dataset.finished 同时承担两个语义：终态标记
        //   （pruneJobCards 靠它收旧卡）+ "已经自动收过一次了，别再动它"。
        el.classList.remove("open");
        el.dataset.finished = "1";
      }
      pruneJobCards();
    } else {
      active++;
    }
    window.__lastJobs = window.__lastJobs || {};
  }
  // ★ 这里原本每 1.6 秒无条件拉一次全量 /api/jobs（1022 条 ≈ 822 KB，
  //   实测 8 秒 5 次 = 4.1 MB 持续流量）。但紧接着调用的 paintJobBadge()
  //   **只读本地的 watching** —— 拉回来的 window.__lastJobs 在这一步压根没用上。
  //   纯浪费，删掉。任务中心的数据改成"打开时才拉"（见 openJobCenter）。
  paintJobBadge();
  setTimeout(tickJobs, 1600);
}

/* ---------------------------------------------------------- 上传 */

function pickUpload(mode) {
  pickerMode = mode || "video";
  const inp = $("#filePicker");
  inp.value = "";
  inp.accept = mode === "song" ? "audio/*,.wav,.mp3" : "video/*,audio/*,.mp4,.mkv,.wav,.mp3";
  inp.click();
}

$("#filePicker").addEventListener("change", async (e) => {
  const fs = Array.from(e.target.files || []);
  if (!fs.length) return;
  // ★ 逐个上传，但**都收进同一个队列**（而不是后来者覆盖前者）。
  //   以前只取 files[0]：一次选 5 个视频，最后只剩第 1 个生效，
  //   而用户看到的提示却是"上传完成" —— 他以为 5 个都进去了。
  if (pickerMode === "video" || pickerMode === "stageSong") {
    for (const f of fs) await uploadFile(f);
    if (pickerMode === "video" && fs.length > 1) {
      toast("已加入 " + fs.length + " 个文件，确认后点「开始拆解」", "good");
    }
  } else {
    await uploadFile(fs[0]);
  }
});

async function uploadFile(f) {
  toast("上传中…");
  const r = await fetch("/api/upload?name=" + encodeURIComponent(f.name), {
    method: "POST", body: f,
  });
  const j = await r.json();
  if (!r.ok) { toast(j.error || "上传失败", "bad"); return; }
  if (pickerMode === "song") {
    pickedSong = j.path;
    $("#songInfo").textContent = "已选：" + f.name;
    // 选了歌也未必能唱：没有歌声模型、素材又不够的角色，点下去 sing_job 会直接
    // 报「只有 0 段干声」。既然状态是已知的，就别让按钮亮着骗人。
    const singOk = !(CUR && CUR.ability && CUR.ability.sing
                     && CUR.ability.sing.state === "blocked");
    $("#btnSing").disabled = !singOk;
    if ($("#btnPreview")) $("#btnPreview").disabled = !singOk;
  } else if (pickerMode === "stageSong") {
    stageSetSong(j.path, f.name);
  } else {
    // 视频：进队列（可以好几个）。重复选同一个文件就更新它，不堆两行。
    const it = { name: f.name, path: j.path, size: j.size || 0 };
    const at = IMPORT_Q.findIndex(x => x.name === it.name);
    if (at >= 0) IMPORT_Q[at] = it; else IMPORT_Q.push(it);
    pickedVideo = it.path;              // 兼容只认单个视频的旧逻辑
    paintImportQueue();
    $("#btnStartImport").disabled = false;
  }
  toast("上传完成", "good");
}

/* 导入队列：一次选多个视频时，用户得看得见自己选了哪几个、能不能去掉一个。
   只显示"已选：xxx.mp4"是不够的 —— 选了 5 个他也只看到最后一个。 */
let IMPORT_Q = [];

function paintImportQueue() {
  const box = $("#impQueue");
  if (!box) return;
  const info = $("#fileInfo");
  if (info) {
    const n = IMPORT_Q.length;
    const mb = IMPORT_Q.reduce((a, b) => a + (b.size || 0), 0) / 1048576;
    info.textContent = n ? ("已选 " + n + " 个文件 · 共 " + mb.toFixed(1) + " MB") : "";
  }
  box.innerHTML = IMPORT_Q.map((it, i) =>
    '<div class="improw"><span class="nm" title="' + esc(it.name) + '">' + esc(it.name) +
    '</span><span class="sz">' + ((it.size || 0) / 1048576).toFixed(1) + ' MB</span>' +
    '<span class="x" data-rmq="' + i + '" title="从队列里去掉">✕</span></div>').join("");
  const btn = $("#btnStartImport");
  if (btn) btn.disabled = !IMPORT_Q.length;
}

$("#impQueue").addEventListener("click", (e) => {
  const x = e.target.closest("[data-rmq]");
  if (!x) return;
  IMPORT_Q.splice(parseInt(x.dataset.rmq, 10), 1);
  paintImportQueue();
});

async function startImport() {
  if (!IMPORT_Q.length && !pickedVideo) return;
  const vids = IMPORT_Q.length ? IMPORT_Q.map(x => x.path) : [pickedVideo];
  const mode = (document.querySelector('input[name="impMode"]:checked') || {}).value || "auto";
  const n = $("#inpN").value ? parseInt($("#inpN").value, 10) : null;
  const body = {
    kind: "ingest", videos: vids, mode: mode === "append" ? "merge" : mode,
    n_speakers: n,
    quality: $("#inpQ").value,
    uvr: $("#inpUvr").checked, do_train: $("#inpTrain").checked,
  };
  // 追加模式：素材并入那个角色，不新建角色
  if (mode === "append") {
    const rid = ($("#impAppendTo") || {}).value;
    if (!rid) { toast("先选一个要追加到的角色", "bad"); return; }
    body.append_to = rid;
  }
  // 导入框也支持自定义轮数（说话模型，上限 200）。不给的话这个口子就是
  // "只有角色页能填自定义"，同一个后端参数两个入口两种能力，说不过去。
  if (body.quality === "custom") {
    const n = parseInt($("#inpQeps").value || "0", 10);
    if (!(n > 0)) { toast("先填一个大于 0 的轮数", "bad"); return; }
    body.epochs = Math.min(n, 200);
    if (n > 200) toast("说话模型最多 200 轮，已按 200 开始", "bad");
  }
  closeMask("maskImport");
  const j = await post("/api/jobs", body);
  watchJob(j.job.id, loadRoles);
  toast(mode === "append" ? "正在把素材追加到这个角色"
        : mode === "match" ? "正在认人：是库里哪个角色就并进去" : "开始拆解", "good");
}

/* 「＋素材」：给一个已经存在的角色继续加料。
   这是"模型的素材可以一直攒"的入口 —— 第一次导入只是起点，
   后面看到他新的视频就再拖进来，segments 只增不减。 */
function openAppend(rid) {
  IMPORT_Q = [];
  pickedVideo = null;
  paintImportQueue();
  openMask("maskImport");
  const t = $("#impTitle");
  if (t) t.textContent = "给角色追加素材";
  const sel = $("#impAppendTo");
  if (sel) {
    sel.innerHTML = (ROLES || []).map(r =>
      '<option value="' + esc(r.id) + '"' + (r.id === rid ? " selected" : "") + '>' +
      esc(r.name) + '</option>').join("");
  }
  const wrap = $("#impAppendWrap");
  if (wrap) wrap.style.display = "";
  const r = document.querySelector('input[name="impMode"][value="append"]');
  if (r) { r.checked = true; }
}

/* ---------------------------------------------------------- 杂项 */

function closeMask(id) { $("#" + id).classList.remove("on"); }
function openMask(id) { $("#" + id).classList.add("on"); }

$("#btnNew").onclick = () => {
  IMPORT_Q = []; pickedVideo = null;
  paintImportQueue();
  const t = $("#impTitle"); if (t) t.textContent = "导入视频";
  const wrap = $("#impAppendWrap"); if (wrap) wrap.style.display = "none";
  const r = document.querySelector('input[name="impMode"][value="auto"]');
  if (r) r.checked = true;
  const info = $("#fileInfo"); if (info) info.textContent = "";
  openMask("maskImport");
};
$("#btnStartImport").onclick = startImport;
$("#btnNewRole").onclick = async () => {
  const name = prompt("给这个角色起个名字：", "新角色");
  if (name == null) return;
  const j = await post("/api/roles", { name: name.trim() || "新角色", kind: "speech" });
  await loadRoles();
  openRole(j.role.id);
};
$("#maskRole").onclick = closeDrawer;
$("#maskImport").onclick = (e) => { if (e.target.id === "maskImport") closeMask("maskImport"); };

$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.classList.toggle("on", x === t));
  $$(".pane").forEach(p => p.classList.toggle("on", p.id === t.dataset.pane));
  // 进训练页才去问「已训到第几轮」（要起 RVC 环境，约 3 秒），问过就不重复问。
  // Seed-VC 那一份只读目录，很便宜，每次进都重读（刚训完的档位要立刻出现）。
  if (t.dataset.pane === "paneTrain" && CUR) {
    if (CUR._rvcTrained == null) refreshRvcState();
    refreshSeedvc();
  }
});

// 拖拽上传
["drop", "dropSong"].forEach(id => {
  const el = $("#" + id);
  el.onclick = () => pickUpload(id === "dropSong" ? "song" : "video");
  el.ondragover = (e) => { e.preventDefault(); el.classList.add("on"); };
  el.ondragleave = () => el.classList.remove("on");
  el.ondrop = async (e) => {
    e.preventDefault(); el.classList.remove("on");
    const fs = Array.from(e.dataTransfer.files || []);
    if (!fs.length) return;
    pickerMode = id === "dropSong" ? "song" : "video";
    // ★ 视频是一次可以拖好几个的（同一人的几段素材合成一个模型）。
    //   只取 files[0] 的话，拖 3 个进去只认第 1 个 —— 用户以为都传了，
    //   实际上后两个被静默丢掉。歌曲槽仍只取第一个（右槽只能放一首）。
    const list = (pickerMode === "video") ? fs : fs.slice(0, 1);
    for (const f of list) await uploadFile(f);
    if (list.length > 1) toast("已加入 " + list.length + " 个文件，确认后点「开始拆解」", "good");
  };
});

(async function init() {
  try {
    const e = await api("/api/env");
    $("#env").innerHTML = `<span>GPU <b>${esc(e.gpu)}</b></span>`;
  } catch (err) { /* 环境信息拿不到不影响使用 */ }
  await loadRoles();
  // 没有任务在跑时才轮询角色列表，避免和任务刷新抢请求。
})();

/* ---------------------------------------------------------- 一键翻唱台 */

/* 「自定义…」档位才显示轮数输入框。三档快捷值留在 select 里，
   选自定义才能填任意轮数（说话上限 200、唱歌上限 5000）。 */
function wireCustomEpochs() {
  [["#inpQ2", "#epsWrap2"], ["#inpQ3", "#epsWrap3"], ["#sgQ", "#sgEps"],
   ["#inpQ", "#inpQepsWrap"]].forEach(([sel, target]) => {
    const s = $(sel), w = $(target);
    if (!s || !w) return;
    const sync = () => {
      w.style.display = s.value === "custom" ? "" : "none";
      if (sel === "#inpQ3") paintRvcState();          // 目标轮数变了，重算"够不够"
      if (sel === "#sgQ") stagePaintRoleSlot();       // 同上，翻唱台那一份
    };
    s.addEventListener("change", sync);
    sync();
  });
  // 自定义轮数输入框：边填边判断是不是还不如已经训过的轮数
  const e3 = $("#eps3");
  if (e3) e3.addEventListener("input", () => paintRvcState());
  const eSg = $("#sgEps");
  if (eSg) eSg.addEventListener("input", () => stagePaintRoleSlot());
}
/* 「说话」输入框：边写边把每句用**什么语气读**显示在下面。
   写错的标签当场标红 —— 不标的话用户会以为自己写了语气，
   其实那句话是被当成普通台词念出来的。 */
[["#speakText", tonePreviewRefresh]].forEach(([sel, fn]) => {
  const el = $(sel);
  if (el) el.addEventListener("input", fn);
});
toneLoad();
tonePreviewRefresh();

wireCustomEpochs();

/* 改词的句数提示。
   为什么要当场说：句数不一样时模型会把新词按**长度比例**摊到各段旋律上，
   唱出来就是"这句词塞不下 / 那句词拖长了"。用户填词时就能发现，
   比听成品才发现"词和调对不上"省事得多。 */
const LYRIC_HINT_IDLE = "两段都填了才会改词；只填一段或都不填 = 普通翻唱。"
  + "改词比变声慢得多（4 分钟的歌要多十几分钟），建议先试听小样确认。";
function stageLyricHint() {
  const h = $("#sgLyricHint");
  if (!h) return;
  const n = (s) => (s || "").split("|").map(x => x.trim()).filter(Boolean).length;
  const a = n($("#sgOrig") && $("#sgOrig").value);
  const b = n($("#sgNew") && $("#sgNew").value);
  if (!a && !b) { h.textContent = LYRIC_HINT_IDLE; return; }
  h.textContent = a === b
    ? `原词 ${a} 句 / 新词 ${b} 句 —— 句数一致，会逐句对上旋律。`
    : `原词 ${a} 句 / 新词 ${b} 句 —— 句数不一样，会按长度比例摊到旋律上，节奏可能偏。`;
}
[["#sgOrig"], ["#sgNew"]].forEach(([sel]) => {
  const el = $(sel);
  if (el) el.addEventListener("input", stageLyricHint);
});
stageLyricHint();

/* 「自动识别原歌词」：从歌里把人声认出来，按句填进原词栏。
   为什么需要它 —— 改词**必须**填原词（YingMusic 靠原词把新词对到原曲旋律上），
   而这一步以前是整条链上唯一还需要手工的：用户得自己把原词抄/搜出来。
   识别用的是 faster-whisper（不是整合包自带 ASR），因为它给**词级时间戳**，
   才能按乐句折成句 —— 句数配不上时长，改完词后段必然唱乱。 */
const _autoLyricBtn = $("#btnAutoLyric");
if (_autoLyricBtn) {
  _autoLyricBtn.addEventListener("click", async () => {
    if (!STAGE_SONG) {
      toast("先把要唱的歌拖进②号槽 —— 识别的是那首歌", "bad");
      return;
    }
    const hint = $("#autoLyricHint");
    _autoLyricBtn.disabled = true;
    if (hint) hint.textContent = "识别中（分离+识别，通常一两分钟）…";
    try {
      const r = await post("/api/jobs", { kind: "lyrics", song: STAGE_SONG, lang: "zh" });
      watchJob(r.job.id, async () => {
        _autoLyricBtn.disabled = false;
        let line = "";
        try {
          const st = await api("/api/job/" + r.job.id);
          line = ((st.job || {}).lyric_result || {}).line || "";
        } catch (e) { /* 拿不到就当没识别出来 */ }
        if (!line) {
          if (hint) hint.textContent = "没识别出内容 —— 可以手动填";
          toast("没识别出歌词，看看任务卡的说明", "bad");
          return;
        }
        const box = $("#sgOrig");
        if ((box.value || "").trim() && !confirm("原词栏已有内容，用识别结果覆盖？")) {
          if (hint) hint.textContent = "识别好了，但按你的选择没有覆盖";
          return;
        }
        box.value = line;
        stageLyricHint();
        const n = line.split("|").filter((s) => s.trim()).length;
        if (hint) hint.textContent = `已填入 ${n} 句 —— 识别难免有错字，请核对后再写新词`;
        toast(`已识别 ${n} 句并填入原词栏`, "ok");
      });
    } catch (e) {
      _autoLyricBtn.disabled = false;
      if (hint) hint.textContent = "提交失败：" + e.message;
      toast(e.message, "bad");
    }
  });
}

// 角色卡可以拖进歌手槽：拖动时把角色 id 放进 dataTransfer
document.addEventListener("dragstart", (e) => {
  const card = closestEl(e.target, '.card[data-role]');
  if (!card) return;
  e.dataTransfer.setData("text/role-id", card.dataset.role);
  e.dataTransfer.setData("text/plain", card.dataset.role);
  e.dataTransfer.effectAllowed = "copy";
  card.classList.add("dragging");
});
document.addEventListener("dragend", (e) => {
  const card = closestEl(e.target, '.card[data-role]');
  if (card) card.classList.remove("dragging");
});

/* 翻唱台「歌声训练」档位 -> 轮数。选「自定义」时读旁边的输入框，
   填不出合法值就退回标准 150（宁可少也别传 0 轮把训练跑废）。 */
/* 各角色「歌声模型已经训到第几轮」。训练页和翻唱台共用这一份缓存
   —— 后端问一次要 3 秒（要起 RVC 的 python），别每处各问一遍。
   值语义：undefined = 还没问过；0 = 问过了，从没训过；n>0 = 已训 n 轮。
   训练完要 delete 掉，否则会拿着旧轮数去判断。 */
const RVC_TRAINED = {};
function rvcTrainedOf(rid) {
  const v = RVC_TRAINED[rid];
  if (v !== undefined) return Promise.resolve(v);
  return api("/api/role/" + rid + "/rvc_state")
    .then(j => { RVC_TRAINED[rid] = j.epochs || 0; return RVC_TRAINED[rid]; })
    .catch(() => { RVC_TRAINED[rid] = 0; return 0; });
}

function sgRounds() {
  const q = $("#sgQ").value;
  if (q !== "custom") return ({ fast: 50, standard: 150, perfect: 300 })[q] || 150;
  const n = parseInt($("#sgEps") ? $("#sgEps").value : "0", 10);
  return Math.min(n > 0 ? n : 150, 5000);
}

function stageSetRole(rid) {
  const r = ROLES.find(x => x.id === rid);
  if (!r) return;
  STAGE_ROLE = r;
  stagePaintRoleSlot();
  stageRefreshBtn();
  // 没有歌声模型时，"已经训到第几轮"直接决定这次会不会被拦下，所以补问一次。
  if (!r.rvc) {
    rvcTrainedOf(rid).then(() => {
      if (STAGE_ROLE && STAGE_ROLE.id === rid) stagePaintRoleSlot();
    });
  }
}

/* 画歌手槽里那段说明。轮数口径与 doRvc/后端一致：RVC 的 N 是"训到第 N 轮"，
   所以已经训到 300 之后再选 300，是"没有更多可训的轮数"，要说清楚而不是等报错。 */
function stagePaintRoleSlot() {
  const r = STAGE_ROLE;
  if (!r) return;
  const slot = $("#slotSinger");
  const color = r.color || COLORS[0];
  const initial = (r.name || "?").trim().slice(0, 1).toUpperCase();
  const rounds = sgRounds();
  const n = RVC_TRAINED[r.id];
  let meta;
  if (r.rvc) {
    meta = "已有歌声模型 · 直接开唱";
  } else if (n === undefined) {
    meta = "还没歌声模型 · 会自动先训 " + rounds + " 轮";
  } else if (n === 0) {
    meta = "还没歌声模型 · 会从头训 " + rounds + " 轮";
  } else if (rounds <= n) {
    meta = `<b class="warn">实验已经训到 ${n} 轮，目标 ${rounds} 轮没有更多可训的轮数</b>`
         + ` · 请把「歌声训练」改成自定义并填 ${Math.max(n + 50, n * 2)} 以上`;
  } else {
    meta = `还没歌声模型 · 会从第 ${n} 轮接着训到 ${rounds} 轮`;
  }
  slot.classList.add("has");
  slot.innerHTML =
    `<div class="slot-ph">
       <div class="who"><span class="av" style="background:linear-gradient(135deg,${color},${shade(color)})">${esc(initial)}</span>
       ${esc(r.name)}</div>
        <div class="meta">${meta}</div>
        <div class="meta">${r.stats ? (r.stats.segments || 0) + " 段素材" : ""}</div>
     </div>`;
}

function stageSetSong(path, name) {
  STAGE_SONG = path;
  const slot = $("#slotSong");
  slot.classList.add("has");
  slot.innerHTML = `<div class="slot-ph"><div class="who">🎵 ${esc(name || path.split(/[\\/]/).pop())}</div>
    <div class="meta">已就位，点「一键合成」出成品</div></div>`;
  stageRefreshBtn();
}

function stageRefreshBtn() {
  const ok = !!(STAGE_ROLE && STAGE_SONG);
  $("#btnOneClick").disabled = !ok;
  $("#btnOneClick").textContent = STAGE_ROLE && STAGE_ROLE.rvc
    ? "⚡ 一键合成" : (STAGE_ROLE ? "⚡ 一键合成（会先训歌声模型）" : "⚡ 一键合成");
}

async function stagePickSong(f) {
  if (!f) return;
  toast("上传歌曲…");
  const r = await fetch("/api/upload?name=" + encodeURIComponent(f.name), { method: "POST", body: f });
  const j = await r.json();
  if (!r.ok) { toast(j.error || "上传失败", "bad"); return; }
  stageSetSong(j.path, j.from_video ? (f.name + " 的音轨") : f.name);
  // 视频要抽音轨，可能要几十秒 —— 不明说的话用户会以为卡住了
  toast(j.note ? j.note + "，歌已就位" : "歌已就位", "good");
}

async function stageOneClick() {
  if (!STAGE_ROLE || !STAGE_SONG) return;
  const needTrain = !STAGE_ROLE.rvc;
  const rounds = sgRounds();
  if (needTrain) {
    // 已经训过的话，"这次目标 ≤ 已训轮数"必须当场拦住：否则要等训练起进程、
    // 跑完预处理才报错，用户白白等几分钟还不知道为什么。
    const n = await rvcTrainedOf(STAGE_ROLE.id);
    if (n > 0 && rounds <= n) {
      const sug = Math.max(n + 50, n * 2);
      toast(`这个实验已经训到 ${n} 轮了，目标 ${rounds} 轮没有更多可训的轮数。`
            + `已把「歌声训练」改成自定义并填 ${sug}，会从第 ${n} 轮接着往下训。`, "bad");
      $("#sgQ").value = "custom";
      $("#sgEps").style.display = "";
      $("#sgEps").value = String(sug);
      stagePaintRoleSlot();
      return;
    }
    if (!confirm(`这个角色还没有歌声模型。\n\n` +
      `点「确定」会自动先用它的素材训一个（${n > 0 ? "从第 " + n + " 轮接着训到 " + rounds : rounds + " 轮"}），训完直接开唱。\n` +
      `显卡会被占满，可能要几十分钟。`)) return;
  }
  // 改词：两段都填了才算（只有新词而没有原词，模型无从知道旋律该对到哪一句）。
  const _ol = ($("#sgOrig") && $("#sgOrig").value || "").trim();
  const _nl = ($("#sgNew") && $("#sgNew").value || "").trim();
  const _edit = !!(_ol && _nl);
  if (!_edit && (_ol || _nl)) {
    toast("改词要「原歌词」和「新歌词」都填，只填了一段 —— 这次按普通翻唱跑", "bad");
  }
  if (busyGuard($("#btnOneClick"))) return;
  $("#btnOneClick").textContent = needTrain ? "⚡ 先训歌声模型…"
    : (_edit ? "⚡ 改词+转换中…" : "⚡ 转换中…");
  try {
    const body = {
      kind: "sing", role: STAGE_ROLE.id, song: STAGE_SONG,
      pitch: parseInt($("#sgPitch").value, 10),
      quality: $("#sgQ").value, auto_train: true,
      // index_rate 不再在前端写死：默认值只能有一处（studio.RVC_INDEX_RATE），
      // 前端再写一个数就等着两边漂 —— 以前这里硬编码 0.75，和后端实测选出的
      // 0.50 对不上，等于绕过了调优结论。
      nonvocal: ($("#sgNonvocal") && $("#sgNonvocal").value) || "keep",
      engine: ($("#sgEngine") && $("#sgEngine").value) || "rvc",
    };
    if (_edit) { body.orig_lyric = _ol; body.new_lyric = _nl; }
    if ($("#sgQ").value === "custom") body.epochs = rounds;
    const j = await post("/api/jobs", body);
    watchJob(j.job.id, () => {
      $("#btnOneClick").disabled = false;
      stageRefreshBtn();
      stageShowResult(STAGE_ROLE.id);
      loadRoles();
      // 这次可能刚训过，轮数缓存作废（下次问到的才是新值）
      if (needTrain) { delete RVC_TRAINED[STAGE_ROLE.id]; stagePaintRoleSlot(); }
    });
  } catch (e) {
    toast(e.message, "bad");
    $("#btnOneClick").disabled = false;
    stageRefreshBtn();
  }
}

// ---------------------------------------------------------------------------
// 多轨试听台
//
// 为什么值得加：用户说"唱出来的歌没法听"，但"没法听"可能有三种完全不同的原因 ——
//   ① 分离没分干净（伴奏里还有人声 / 人声里还有鼓）
//   ② 变声那一步不对（音高没生效、旋律被唱平、音色不像）
//   ③ 混音那一步不对（人声太大/太小、被压得发闷、和伴奏错位）
// 只给一条混好的成品，这三种听起来都只是"难听"，用户说不清、我们也只能猜。
// 把四条轨摊开 + 一条 A/B 对比（原曲↔成品同时放、拖动比），用户**自己**就能听出
// 是哪一步的锅，反馈才能落到具体环节上。
// 变速慢放用的是浏览器内置的 preservesPitch（慢放不变调），用来揪跑调和错位很有效。
const STEM_NAME = { mix: "成品", voice: "人声（干声）", inst: "伴奏", src: "原曲" };
let STEM_OUTS = [];
let STEM_SPEED = 1;

/* 成品旁那一行质检摘要。
   ★ 为什么放在成品边上、而不是只留在任务日志里：用户拿到一条成品要回答的第一个
     问题是"这版能不能用"，然后才决定要不要花几分钟听一遍。让他先听再猜，
     等于让他自己当质检员 —— 而这正是我们能量出来的东西。
   ★ 口径用 qc_whole（整曲铺满多窗）优先：单窗那个数是**抽签**抽出来的
     （同一份成品按窗口位置能报 3.5% 也能报 16.9%，见 studio.py 的实测表）。
     把抽签的数摆在成品边上，比不给更糟 —— 他会照着它决定要不要重唱。
   ★ 阈值不在这里另发明，抄 studio.py::describe_qc_whole 的那三条：
     八度错 >10%、宽松吻合 <75%、错位 >250ms 就是"这次别直接用"。 */
/* ---- 一句话结论：把"选哪版"从**比对任务**变成**阅读任务** ----
   现状：12 版历史，每版一行指标（音准 86% / 八度 2% / 抖动 24 音分…），
   用户还是得自己在脑子里排序。这不该是他的活 —— 指标是我们算的，
   结论也该我们给。
   约束：**只用已经算出来的指标**，不新增推理、不额外跑模型。
   口径仍抄 describe_qc_whole 的三条阈值（八度 >10%、宽松吻合 <75%、
   错位 >250ms），这样结论和上面的质检徽章不会互相打脸。
   综合分只用于**排序**（"哪版最稳"），阈值用于**说人话**（"哪里不行"）。 */
function takeMetrics(o) {
  const q = o.qc || {}, w = o.qc_whole || {};
  const pick = (a, b) => (a === null || a === undefined) ? b : a;
  const num = (v) => {
    if (v === null || v === undefined || v === "" || !isFinite(Number(v))) return null;
    return Number(v);
  };
  const mr = num(pick(w.match_ratio_loose,
                     pick(w.match_ratio, pick(q.match_ratio_loose, q.match_ratio))));
  if (mr === null) return null;              // 没有音准数，就别编结论
  const oer = num(pick(w.octave_err_ratio, q.octave_err_ratio)) || 0;
  const dev = num(pick(w.dev_iqr_cents, q.dev_iqr_cents)) || 0;
  const lag = num(pick(w.lag_ms, q.lag_ms)) || 0;
  const spread = num(pick(w.spread_ratio, q.spread_ratio));
  const cl = (x) => Math.max(0, Math.min(1, x));
  const score =
    45 * cl((mr - 0.70) / 0.25) +            // 音准吻合：0.70 以下 0 分，0.95 满分
    20 * (1 - cl(oer / 0.10)) +              // 八度错：10% 归零
    15 * (1 - cl((dev - 10) / 50)) +         // 音高抖动：10 音分内满分
    10 * (1 - cl(Math.abs(lag) / 250)) +     // 错位：250ms 归零
    10 * (spread === null ? 0.7 : 1 - cl(Math.abs(spread - 1) / 0.5));
  return { mr, oer, dev, lag, spread, score };
}

function takeRanking(outputs) {
  const arr = (outputs || []).map(o => ({ o, m: takeMetrics(o) })).filter(x => x.m);
  if (!arr.length) return { arr: [], best: null };
  const best = arr.reduce((b, x) => (x.m.score > b.m.score ? x : b), arr[0]);
  return { arr, best };
}

/* 在同歌的所有版本里定一句话。
   ★ 只写"这版最好"没信息量：每版都有长有短。所以能说出**哪一项最强**
     （相对 12 版里取得第一）就优先说它，再补一个真的没达标的短板。 */
function takeVerdict(m, peers, isBest, bestM) {
  if (!m) return "";
  if (isBest) return "这版最稳，建议直接用";
  // 84% 是"还能听"的下限：低于它咬字已经糊到影响识别，
  // 再往上差 1~2 个点只是"没那么准"，不值得用同一句重话。
  if (m.mr < 0.84) return "音准差一档，咬字偏糊";
  let con = "";
  if (m.oer > 0.05) con = "高音偶有跑八度";
  else if (m.dev > 40) con = "整体略飘";
  else if (Math.abs(m.lag) > 100) con = m.lag > 0 ? "节奏稍拖" : "节奏略抢";
  else if (m.spread !== null && Math.abs(m.spread - 1) > 0.3) {
    con = m.spread < 1 ? "动态偏平" : "动态偏冲";
  }
  let adv = "";
  if ((peers || []).length >= 3) {
    const top = (get, lower) => {
      const vals = peers.map(get).filter(v => isFinite(v));
      if (!vals.length) return false;
      const t = lower ? Math.min.apply(null, vals) : Math.max.apply(null, vals);
      if (Math.abs(get(m) - t) > 1e-6) return false;
      // ★ 并列第一不算"最"：12 版里有 5 版 lag 都是 0ms，
      //   给每一版都写"节奏最贴"等于什么都没说。只有**唯一**最优才算特长。
      return vals.filter(v => Math.abs(v - t) <= 1e-6).length === 1;
    };
    if (top(x => x.mr, false)) adv = "音最准";
    else if (top(x => x.oer, true)) adv = "高音最稳";
    else if (top(x => x.dev, true)) adv = "音高最稳";
    else if (top(x => Math.abs(x.lag), true)) adv = "节奏最贴";
    else if (top(x => (x.spread === null ? 9 : Math.abs(x.spread - 1)), true)) {
      adv = "动态最自然";
    }
  }
  if (adv && con) return adv + "，但" + con;
  if (adv) return adv + "，其余各项略逊最优版";
  if (con) return "音准达标，但" + con;
  // 没有硬伤、也没有单项第一。这时说"整体均衡"等于没说 ——
  // 把**差得最多的那一项**点出来，用户才知道拿它跟最优版比亏在哪。
  const b = bestM || null;
  if (b && b !== m && b.score > m.score) {
    if (b.mr - m.mr > 0.02) return "各项达标，但音准略逊一筹";
    if (m.oer - b.oer > 0.02) return "音准达标，但高音不如最优版稳";
    if (m.dev - b.dev > 5) return "音准达标，但音高比最优版略飘";
    if (Math.abs(m.spread - 1) - Math.abs(b.spread - 1) > 0.15) {
      return "各项达标，但动态不如最优版自然";
    }
    if (Math.abs(m.lag) - Math.abs(b.lag) > 60) return "各项达标，但节奏不如最优版贴";
  }
  return "整体均衡，没有明显短板";
}

function qcBadge(o) {
  const q = o.qc || {}, w = o.qc_whole || {};
  if (!Object.keys(q).length && !Object.keys(w).length) return "";
  const pick = (a, b) => (a === null || a === undefined) ? b : a;
  const mrRaw = pick(w.match_ratio_loose,
                     pick(w.match_ratio, pick(q.match_ratio_loose, q.match_ratio)));
  const mr = Math.round((mrRaw || 0) * 100);
  const oer = Math.round((pick(w.octave_err_ratio, q.octave_err_ratio) || 0) * 100);
  const hasLag = q.lag_ms !== null && q.lag_ms !== undefined;
  const lag = Math.round(q.lag_ms || 0);
  const src = w.windows ? "整曲 " + w.windows + " 窗" : "单窗";
  const okMr = mr >= 75, okOct = oer <= 10, okLag = !hasLag || Math.abs(lag) <= 250;
  const allOk = okMr && okOct && okLag;
  const it = (label, val, ok, unit) =>
    `<span class="qci ${ok ? "ok" : "no"}">${label} ${val}${unit || ""} ${ok ? "✓" : "⚠"}</span>`;
  const tip = w.worst_octave != null
    ? ` title="最差的一窗八度错 ${Math.round((w.worst_octave || 0) * 100)}%` +
      `（第 ${Math.round(w.worst_at || 0)} 秒附近）——听感由最差那段决定；` +
      `音高抖动 ${Math.round(w.dev_iqr_cents || 0)} 音分"`
    : "";
  return `<div class="qcs ${allOk ? "ok" : "bad"}"${tip}>` +
         `<span class="qct">${src}</span>` +
         it("音准吻合", mr, okMr, "%") +
         it("八度错", oer, okOct, "%") +
         (hasLag ? it("错位", lag, okLag, "ms") : "") +
         `<span class="qcv">${allOk ? "这版能直接用"
           : "有一项没达标，建议换一档音高重唱一版对比"}</span></div>`;
}

function stemsBar(o, idx) {
  const s = o.stems || {};
  const keys = ["mix", "voice", "inst", "src"];
  // 老产物没有 stems 字段（新加的）—— 那就退回一个普通播放器，
  // 不能让用户"升级之后反而听不了以前的成品"。
  if (!keys.some(k => s[k])) {
    return `<audio controls preload="none" src="${media(o.wav)}"></audio>`;
  }
  const btns = keys.map(k =>
    `<button class="stbtn${k === "mix" ? " on" : ""}${s[k] ? "" : " off"}"
       data-stem="${k}" ${s[k] ? "" : "disabled"}
       onclick="stemPick(this,'${k}')">${STEM_NAME[k]}</button>`).join("");
  // 0.75x / 1x / 1.25x：A/B 比对时慢一点才听得出咬字和音高，
  // 快一点是"过一遍确认没翻车"。0.5x 太慢没人用，1.5x 会把咬字糊掉。
  const speeds = [0.75, 1, 1.25].map(v =>
    `<button class="stbtn${v === 1 ? " on" : ""}" data-sp="${v}"
       onclick="stemSpeed(this,${v})">${v}×</button>`).join("");
  return `<div class="stems" data-ab="0" data-idx="${idx || 0}">
    <div class="strow">
      <span class="stk">分轨</span>${btns}
      <span class="stk">速度</span>${speeds}
    </div>
    <audio class="stau" controls preload="metadata" src="${media(o.wav)}"></audio>
    <div class="strow ab">
      <button class="stbtn abbtn" onclick="stemAB(this)">⇄ A/B 对比</button>
      <input class="abr" type="range" min="0" max="100" value="50"
             oninput="stemCross(this)" disabled>
      <span class="abhint">左 = 原曲　右 = 成品</span>
    </div>
  </div>`;
}

function _stemEls(btn) {
  const bar = btn.closest(".stems");
  return { bar, a: bar.querySelector(".stau"), r: bar.querySelector(".abr"),
           ab: bar.querySelector(".abbtn"),
           out: STEM_OUTS[Number(bar.dataset.idx || 0)] || {} };
}

function stemPick(btn, k) {
  const { bar, a, out } = _stemEls(btn);
  const p = (out.stems || {})[k];
  if (!p) return;
  a.src = media(p);
  a.playbackRate = STEM_SPEED;
  a.play().catch(() => {});
  bar.querySelectorAll(".stbtn[data-stem]").forEach(b =>
    b.classList.toggle("on", b.dataset.stem === k));
}

function stemSpeed(btn, v) {
  const { bar, a } = _stemEls(btn);
  STEM_SPEED = v;
  [a, bar._b].forEach(el => { if (el) el.playbackRate = v; });
  bar.querySelectorAll(".stbtn[data-sp]").forEach(x =>
    x.classList.toggle("on", Number(x.dataset.sp) === v));
}

function stemAB(btn) {
  const { bar, a, r, out } = _stemEls(btn);
  const src = (out.stems || {}).src;
  if (!src) { toast("这条产物没有留下原曲，做不了 A/B", "bad"); return; }
  const on = bar.dataset.ab !== "1";
  bar.dataset.ab = on ? "1" : "0";
  btn.classList.toggle("on", on);
  r.disabled = !on;
  if (on) {
    if (!bar._b) {
      const b = new Audio(media(src));
      b.preload = "metadata";
      bar._b = b;
      // 两条要一直贴着放，不然"对比"比的是两个时间点，反而误导人
      a.addEventListener("timeupdate", () => {
        if (bar.dataset.ab === "1" && Math.abs(b.currentTime - a.currentTime) > 0.25) {
          b.currentTime = a.currentTime;
        }
      });
    }
    const b = bar._b;
    b.currentTime = a.currentTime || 0;
    b.playbackRate = STEM_SPEED;
    stemCross(r);
    b.play().catch(() => {});
    if (a.paused) a.play().catch(() => {});
  } else if (bar._b) {
    bar._b.pause();
    a.volume = 1;
  }
}

function stemCross(r) {
  const bar = r.closest(".stems");
  const a = bar.querySelector(".stau");
  const b = bar._b;
  const t = Number(r.value) / 100;
  a.volume = t;
  if (b) b.volume = 1 - t;
}

// 把刚生成的成品摆出来：人声干声 + 成品混音轨，分开能取
async function stageShowResult(rid) {
  try {
    const j = await api("/api/role/" + rid);
    const singing = (j.role.outputs || []).filter(o => o.kind === "singing");
    window.__stageOutputs = j.role.outputs || [];   // 历史 take 的按钮要按原下标回传
    const outs = singing.slice(-2).reverse();
    if (!outs.length) return;
    STEM_OUTS = outs;
    // 结论要跟**同一首歌的所有版本**比，不是只跟这两条比
    const rank = takeRanking(j.role.outputs);
    const peers = rank.arr.map(x => x.m);
    const cur = outs.map((o, i) => {
      const url = media(o.wav);
      const name = o.wav.split(/[\\/]/).pop();
      const m = takeMetrics(o);
      const isMix = !o.track || String(o.track).startsWith("成品");
      const vd = isMix && m
        ? takeVerdict(m, peers, rank.best && m.score >= rank.best.m.score - 1e-9,
                       rank.best && rank.best.m)
        : "";
      return `<div class="outcard">
        <div class="tt">${esc(o.track || "成品")} · ${o.seconds || "?"} 秒</div>
        <div class="nm">${esc(name)}</div>
        ${qcBadge(o)}
        ${vd ? `<div class="vd">${esc(vd)}</div>` : ""}
        ${stemsBar(o, i)}
        <div class="row"><a class="btn sm" href="${url}" download>下载这条</a></div>
      </div>`;
    }).join("");
    $("#stageOut").innerHTML = cur + takeHistory(rid, j.role.outputs || []);
    $("#stageOut").classList.add("on");
  } catch (e) { /* 角色刚好被删 */ }
}

/* ---- 历史 take：同一首歌出的每一版都留着，带当时的参数和质检 ----
   为什么需要：先用 pitch=0 唱了一版，又试了 +2，过两天想退回第一版 ——
   以前只能靠文件名猜哪一版是哪一版（而文件名长得都差不多）。
   这里存的是**参数快照 + 质检指标 + 时间**，恢复时只是把参数填回输入框或
   把某条挪回列表末尾，**一次推理都不重跑**。
   只列「成品」轨（干声轨是同一版的两条记录之一，列出来就是重复）。 */
function takeHistory(rid, outputs) {
  const rows = [];
  outputs.forEach((o, i) => {
    if (o.kind !== "singing") return;
    if (o.track && !String(o.track).startsWith("成品")) return;
    rows.push([o, i]);
  });
  // 最新那条就是「当前成品」，剩下的才是历史
  const hist = rows.slice(0, -1).reverse().slice(0, 12);
  if (!hist.length) return "";
  // 12 版放在一起排一次序：结论里的"最稳/最飘"是相对这 12 版说的，
  // 不是凭空给个形容词。单版拿出来看时不存在"相对"，也就不该有排名口径。
  const rank = takeRanking(hist.map(([o]) => o));
  const peers = rank.arr.map(x => x.m);
  const body = hist.map(([o, i]) => {
    const p = o.params || {};
    const bits = [];
    if (p.pitch !== undefined && p.pitch !== null) {
      const ask = p.pitch_asked;
      const sg = (v) => (Number(v) > 0 ? "+" : "") + Number(v);
      bits.push("音高 " + sg(p.pitch) +
        (ask !== undefined && ask !== null && Number(ask) !== Number(p.pitch)
          ? `（原设 ${sg(ask)}，音高体检改判）` : ""));
    }
    if (p.index_rate !== undefined && p.index_rate !== null) {
      bits.push("音色比例 " + p.index_rate);
    }
    if (p.engine) bits.push("引擎 " + (p.engine === "rvc" ? "RVC" : p.engine));
    if (p.nonvocal && p.nonvocal !== "off") bits.push("非歌声段 " + p.nonvocal);
    if (p.model) bits.push("模型 " + esc(p.model));
    const m = takeMetrics(o);
    const vd = m
      ? takeVerdict(m, peers, rank.best && m.score >= rank.best.m.score - 1e-9,
                       rank.best && rank.best.m)
      : "";
    return `<div class="take">
      <div class="tt">${esc(o.at || "")}${o.seconds ? " · " + o.seconds + " 秒" : ""}</div>
      <div class="pp">${bits.length ? bits.join(" · ") : "（这一版没记参数）"}</div>
      ${qcBadge(o)}
      ${vd ? `<div class="vd">${esc(vd)}</div>` : ""}
      <div class="row">
        ${lazyPlayer(media(o.wav), "", o.seconds)}
        <button class="btn sm" data-takeload="${i}">载入参数</button>
        <button class="btn sm" data-takeuse="${i}">设为当前成品</button>
      </div>
    </div>`;
  }).join("");
  return `<details class="takes"${hist.length > 3 ? "" : " open"}>
    <summary>历史 take（${hist.length} 版 · 点开可载入参数或退回这一版）</summary>
    <div class="takelist">${body}</div>
  </details>`;
}

/* 把某一版的参数填回翻唱台。**只填界面，不跑任何推理** ——
   用户要的是"再试一次这个配置"，所以要等他点开始才算数。 */
function takeLoadParams(o) {
  const p = o.params || {};
  const set = (id, v) => { const el = $(id); if (el && v !== undefined && v !== null) el.value = String(v); };
  set("#sgPitch", p.pitch);
  set("#sgEngine", p.engine);
  set("#sgNonvocal", p.nonvocal);
  if ($("#sgPitchV")) $("#sgPitchV").textContent = String(p.pitch ?? 0);
  toast("这一版的参数已填回翻唱台（不会自动重跑，点「开始」才算）", "good");
}

async function takeUse(rid, idx, btn) {
  if (btn) btn.disabled = true;
  try {
    const d = await post(`/api/role/${rid}/output-use`, { index: Number(idx) });
    if (!d.ok) { toast(d.error || "没能设为当前成品", "bad"); return; }
    toast("已把这一版设为当前成品（文件没动，也没重跑）", "good");
    await stageShowResult(rid);
  } catch (e) {
    toast(e.message || "设为当前成品失败", "bad");
  } finally {
    if (btn) btn.disabled = false;
  }
}

/* 槽位上的落点提示：把"隐藏手势"变成能学的动作。
   光让槽位变亮，用户只知道"这儿能放"，不知道**放下去会发生什么** ——
   于是他会犹豫，然后松手松错地方。把结果直接写出来（松开 → 选定演唱者），
   他才敢松手。放错了也要当场说（这里要的是角色卡，不是文件）。*/
function slotHint(el, text, bad) {
  if (!el) return;
  let t = el.querySelector(".drophint");
  if (!t) {
    t = document.createElement("div");
    t.className = "drophint";
    el.appendChild(t);
  }
  t.textContent = text;
  t.className = "drophint" + (bad ? " bad" : "");
}

function slotClearHint(el) {
  const t = el && el.querySelector(".drophint");
  if (t) t.remove();
}

function slotClearHints() {
  document.querySelectorAll(".slot .drophint").forEach((t) => t.remove());
}

// 判断这次拖的是"角色卡"还是"文件"：角色卡带自定义 MIME text/role-id。
// 只看 types 里有没有 Files 是不够的 —— 拖角色卡时 types 里也可能有别的。
function dragPayloadKind(dt) {
  const types = Array.from((dt && dt.types) || []);
  const isRole = types.some((t) => String(t).toLowerCase() === "text/role-id");
  const isFile = types.includes("Files");
  return isRole ? "role" : (isFile ? "file" : "");
}

// 槽位交互：歌手槽点击 = 快速选角色；歌曲槽点击 = 选文件
function initStage() {
  const singer = $("#slotSinger"), song = $("#slotSong");
  const HINT = {
    singer: { role: "松开 → 选定演唱者", file: "这里要放角色卡，不是文件" },
    song: { file: "松开 → 用这首歌", role: "这里要放歌曲文件，角色卡放上面那个槽" },
  };
  document.addEventListener("dragover", (e) => {
    const el = closestEl(e.target, ".slot");
    if (!el) return;
    e.preventDefault();
    el.classList.add("on");
    const kind = dragPayloadKind(e.dataTransfer);
    if (!kind) return;
    if (el.id === "slotSinger") {
      slotHint(el, HINT.singer[kind] || "", kind !== "role");
    } else if (el.id === "slotSong") {
      slotHint(el, HINT.song[kind] || "", kind !== "file");
    }
  });
  document.addEventListener("dragleave", (e) => {
    const el = closestEl(e.target, ".slot");
    if (!el) return;
    el.classList.remove("on");
    slotClearHint(el);
  });
  document.addEventListener("drop", async (e) => {
    const el = closestEl(e.target, ".slot");
    if (!el) return;
    e.preventDefault();
    el.classList.remove("on");
    slotClearHints();
    const rid = e.dataTransfer.getData("text/role-id");
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (el.id === "slotSinger") {
      if (rid) stageSetRole(rid);
      else if (f) toast("这里要放角色卡，不是文件", "bad");
    } else if (el.id === "slotSong" && f) {
      await stagePickSong(f);
    }
  });

  singer.onclick = () => {
    const menu = ROLES.map(r =>
      `<div class="opt" data-rid="${r.id}">${esc(r.name)}${r.rvc ? " ✓" : ""}</div>`).join("");
    if (!menu) { toast("还没有角色，先导入一个视频", "bad"); return; }
    openList(singer, menu, (rid) => stageSetRole(rid));
  };
  song.onclick = () => { pickerMode = "stageSong"; $("#filePicker").click(); };
  $("#btnOneClick").onclick = stageOneClick;
  // 历史 take 的两个按钮。成品区是 innerHTML 整块重画的，所以只能事件委托，
  // 不能给按钮直接挂 onclick（重画一次就掉一次）。
  $("#stageOut").addEventListener("click", (e) => {
    if (!STAGE_ROLE) return;
    const b = closestEl(e.target, "[data-takeload],[data-takeuse]");
    if (!b) return;
    const idx = b.dataset.takeload !== undefined ? b.dataset.takeload : b.dataset.takeuse;
    const o = (window.__stageOutputs || [])[Number(idx)];
    if (!o) { toast("这条成品已经不在列表里了，刷新一下", "bad"); return; }
    if (b.dataset.takeload !== undefined) takeLoadParams(o);
    else takeUse(STAGE_ROLE.id, idx, b);
  });
  stageRefreshBtn();
}

// 注意 e.target 未必是元素（window/document 派发的合成事件就是），
const closestEl = (t, sel) => (t && typeof t.closest === "function" ? t.closest(sel) : null);

// 全局拖拽：视频/音频丢到页面任何地方都能用，不用瞄准那个小框
window.addEventListener("dragover", (e) => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
    e.preventDefault();
    document.body.classList.add("dropping");
  }
});
window.addEventListener("dragleave", (e) => {
  if (e.clientX <= 0 || e.clientY <= 0) document.body.classList.remove("dropping");
});
window.addEventListener("drop", async (e) => {
  const inSlot = closestEl(e.target, ".slot, .drop");
  if (inSlot) return;                      // 槽位/导入框自己会处理
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  e.preventDefault();
  document.body.classList.remove("dropping");
  const isAudio = /^audio\//.test(f.type) || /\.(wav|mp3|m4a|flac|aac|ogg)$/i.test(f.name);
  const isVideo = /^video\//.test(f.type) || /\.(mp4|mkv|avi|mov|flv|webm|m4v)$/i.test(f.name);
  if (isAudio) {
    if (STAGE_ROLE) { await stagePickSong(f); toast("歌已放到翻唱台", "good"); }
    else { pickerMode = "stageSong"; await uploadFile(f); }
  } else if (isVideo) {
    pickerMode = "video";
    await uploadFile(f);
    openMask("maskImport");
    toast("视频已放好，确认后点「开始拆解」", "good");
  } else {
    toast("这个文件类型不认识", "bad");
  }
});

// 防重复提交：显卡一次只能训一个，重复排队纯属浪费
function busyGuard(btn, tipSel) {
  // ★ 只数**真的还在跑**的。
  //   以前是 watching.size > 0，而 watching 里也会为"刚才跑完的任务"留一条
  //   （为了让用户还能翻回去看）——于是刚做完一件事、界面上还挂着那张卡，
  //   再点任何训练都会被这句"还有任务在跑"挡回来，而其实什么都没在跑。
  const w = Array.from(watching.values()).find(x => x.alive);
  if (w) {
    // ★ 说清楚"谁在占着、占的是什么"，而不是一句"还有任务在跑"。
    //   用户真正的疑问是"为什么我的任务要等" —— 答不上来他就会以为卡死了。
    const r = jobRes(w.kind);
    toast(`「${w.jtitle || "上一个任务"}」还在跑${r ? "（" + r[0] + "）" : ""}，` +
          `等它完再来。${r && r[0].indexOf("训练") >= 0
            ? "训练会把显存占满，硬塞一个合成进去两个都会变慢。" : ""}`, "bad");
    return true;
  }
  if (btn) btn.disabled = true;
  if (tipSel && $(tipSel)) $(tipSel).textContent = "提交中…";
  return false;
}

// 轻量下拉：贴在目标元素下方
function openList(anchor, html, onPick) {
  const old = $(".poplist");
  if (old) old.remove();
  const pop = document.createElement("div");
  pop.className = "poplist";
  pop.innerHTML = html;
  const r = anchor.getBoundingClientRect();
  pop.style.cssText = `position:fixed;left:${r.left}px;top:${r.bottom + 6}px;z-index:70;` +
    `background:var(--card2);border:1px solid var(--line2);border-radius:10px;padding:6px;` +
    `min-width:180px;box-shadow:var(--sh);max-height:280px;overflow:auto`;
  document.body.appendChild(pop);
  pop.addEventListener("click", (e) => {
    const it = closestEl(e.target, "[data-rid]");
    if (it) { onPick(it.dataset.rid); pop.remove(); }
  });
  setTimeout(() => document.addEventListener("click", function h(ev) {
    if (!pop.contains(ev.target)) { pop.remove(); document.removeEventListener("click", h); }
  }), 0);
}

initStage();

/* ================================================================== 存储清理
 *
 * 为什么要有这个面板：训练一次歌声模型会在两个地方各留一串 55MB 的权重，
 * 拆一个视频留 250MB 中间产物，传上来的原片动辄 700MB —— 跑十几次就是十几 G，
 * 而用户除了自己进资源管理器翻三层目录猜哪个能删，没有任何入口。
 * 猜错的代价是角色当场不能唱歌（正在用的那份权重被删了）。
 *
 * 所以这里只做三件事：把占用全列出来、把正在用的锁住、剩下的让人勾着删。
 * 删除是**真删**（不进回收站）—— 因为用户要的是腾地方，不是换个地方占着；
 * 不可撤销 ⇒ 才需要先分类、上锁、二次确认。
 */
let STORE = null;              // 最近一次扫描结果
let STORE_ARMED = false;       // 「删除选中」是否已进入二次确认
let STORE_FORCE = false;       // 🔓 是否连"角色正在用的模型"一起删
const STORE_PICK = new Set();  // 勾中的 item.id
const STORE_FOLD = new Set();  // 折叠起来的分组

/* 这一格能不能勾。
   默认：凡是有锁的都锁死（底模、正在跑的任务、角色在用的模型）。
   开了 🔓：只有 kind=="role" 的放开 —— 那是"删了要重训"的代价，
   用户自己担得起；底模和正在跑的任务删了是救不回来的，永远不开。 */
function storeUnlockable(it) {
  return !!it.lock && STORE_FORCE && it.lock_kind === "role";
}
function storeCanPick(it) {
  return !it.lock || storeUnlockable(it);
}

function hsize(n) {
  n = Number(n || 0);
  if (n < 1024) return n + " B";
  const u = ["KB", "MB", "GB", "TB"];
  let i = -1;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(1) + " " + u[i];
}

async function storeOpen() {
  openMask("maskStore");
  storeArm(false);
  // ★ 每次打开都重新拉（走后端的 2 分钟 TTL 缓存，不贵）。以前是"有缓存就
  //   直接渲染旧的"——用户删了模型文件再打开面板，看到的是上一次的快照，
  //   「这个角色要重训」的悬空警告永远不出现（实测踩过，见 server.py storage_snapshot）。
  await storeScan();
}

async function storeScan(force, keepTip) {
  const box = $("#storeList");
  if (box && !STORE) box.innerHTML = '<div class="hint" style="padding:20px 2px">正在扫描…</div>';
  const tip = $("#storeTip");
  if (tip && !keepTip) { tip.className = "store-tip"; tip.textContent = "扫描中…"; }
  try {
    STORE = await api("/api/storage" + (force ? "?refresh=1" : ""));
  } catch (e) {
    if (tip) { tip.className = "store-tip err"; tip.textContent = "扫描失败：" + e; }
    return;
  }
  if (STORE.error && tip) {
    tip.className = "store-tip err";
    tip.textContent = STORE.error;
  }
  // ★ 扫描完必须把「扫描中…」收掉。以前漏了这一步，于是：
  //   删完 -> 提示"已删除 N 项" -> 立刻自动重扫 -> 提示被改成"扫描中…"
  //   -> 重扫结束也没人再改回来 ⇒ 条子上永远挂着"扫描中…"，
  //   用户看到的是"好像还在忙"，而"删掉了多少"这个最关键的数字被冲掉了。
  // keepTip=true 时（删完/瘦身完的自动重扫）不动它，保住刚写的成功提示。
  if (tip && !keepTip && !STORE.error) { tip.className = "store-tip"; tip.textContent = ""; }
  // 已经不存在的勾选要丢掉：删完再刷新时，留着它们会让"已勾选"虚高
  const live = new Set();
  (STORE.groups || []).forEach(g => (g.items || []).forEach(it => live.add(it.id)));
  Array.from(STORE_PICK).forEach(id => { if (!live.has(id)) STORE_PICK.delete(id); });
  // 悬空引用：角色记着"我有模型"但文件已经不在了（手工删过 / 目录挪过 /
  // 被早期版本的清理误删）。界面必须喊出来，否则用户只会以为"唱歌坏了"。
  const wn = $("#storeWarn");
  if (wn) {
    const ws = (STORE.warnings || []).filter(Boolean);
    wn.innerHTML = ws.length
      ? "⚠ " + ws.map(w => "<div>" + esc(w) + "</div>").join("")
      : "";
  }
  storeRender();
  storeBadge();
}

function storeBadge() {
  const b = $("#storeBadge");
  if (!b || !STORE) return;
  const gb = (STORE.reclaimable || 0) / 1024 ** 3;
  b.textContent = gb >= 0.1 ? "· 可清 " + gb.toFixed(1) + "G" : "";
}

function storeAllItems() {
  const out = [];
  (STORE && STORE.groups || []).forEach(g => (g.items || []).forEach(it => out.push(it)));
  return out;
}

function storeRender() {
  if (!STORE) return;
  const box = $("#storeList");
  const free = $("#stFree"), tot = $("#stTotal"), rec = $("#stReclaim");
  if (free) free.textContent = STORE.free_s || "-";
  if (tot) tot.textContent = STORE.total_s || "0 B";
  if (rec) rec.textContent = STORE.reclaimable_s || "0 B";
  const st = STORE.settings || {};
  const k = $("#stKeep"), c = $("#stCacheMax"), f = $("#stForce");
  if (k && document.activeElement !== k) k.value = st.keep_archives ?? 2;
  if (c && document.activeElement !== c) c.value = st.cache_max_gb ?? 3;
  // 🔓 开关不在重渲染的 DOM 里（它在固定的工具条上），但它的状态决定了
  // 每一格能不能勾 —— 重渲染时必须把它同步回来，否则界面会出现
  // "开关是开的、格子却是灰的"这种自相矛盾。
  if (f) f.checked = STORE_FORCE;

  let html = "";
  (STORE.groups || []).forEach(g => {
    if (!g.count) return;
    const fold = STORE_FOLD.has(g.id) ? " fold" : "";
    html += '<div class="store-g' + fold + '" data-g="' + esc(g.id) + '">' +
      '<div class="store-gh" data-fold="' + esc(g.id) + '">' +
      '<span class="caret">▼</span><span>' + esc(g.name) + '</span>' +
      '<span class="sz">' + esc(g.size_s) + ' · ' + g.count + ' 项</span></div>' +
      '<p class="store-gd">' + esc(g.desc || "") + '</p><div class="store-gb">';
    (g.items || []).forEach(it => {
      const picked = STORE_PICK.has(it.id);
      // 解锁开关打开后，role 锁那一格会变成"可勾但带风险标记"，
      // 用 .unlocked 把它跟普通可勾项区分开 —— 一眼能看出哪一格动了正在用的模型。
      const unlk = storeUnlockable(it);
      const cls = "store-it" + (picked ? " picked" : "") +
        (it.lock ? (unlk ? " unlocked" : " locked") : "");
      html += '<label class="' + cls + '" data-id="' + esc(it.id) + '">' +
        '<input type="checkbox" data-pick="' + esc(it.id) + '"' +
        (picked ? " checked" : "") + (storeCanPick(it) ? "" : " disabled") + '>' +
        '<span class="nm" title="' + esc(it.name) + '">' + esc(it.name) + '</span>' +
        '<span class="pth" title="' + esc(it.path) + '">' + esc(it.path) + '</span>' +
        '<span class="note">' + esc(it.note || "") + '</span>' +
        '<span class="szz">' + hsize(it.size) + '</span>' +
        (it.lock ? '<span class="lk" title="' + esc(it.lock) + '">' +
          (unlk ? "⚠ " : "🔒 ") + esc(String(it.lock).slice(0, 16)) + '</span>' : '') +
        '</label>';
    });
    html += '</div></div>';
  });
  if (!html) html = '<div class="hint" style="padding:20px 2px">没有扫到任何东西。</div>';
  box.innerHTML = html;
  storeUpdatePick();
}

function storeUpdatePick() {
  const items = storeAllItems();
  const sel = storeSelected();
  const bytes = sel.reduce((a, b) => a + (b.size || 0), 0);
  const el = $("#stPicked");
  if (el) el.textContent = sel.length + " 项 · " + hsize(bytes);
  const btn = $("#stDel");
  if (btn) {
    btn.disabled = sel.length === 0;
    btn.textContent = STORE_ARMED ? "确认删除 " + sel.length + " 项" : "删除选中";
  }
}

function storeArm(on, sel) {
  STORE_ARMED = !!on;
  const tip = $("#storeTip");
  if (!tip) return;
  if (!on) { tip.className = "store-tip"; tip.textContent = ""; storeUpdatePick(); return; }
  const bytes = sel.reduce((a, b) => a + (b.size || 0), 0);
  // 里面有几项是"角色正在用的模型"—— 这个代价必须在按下之前说清楚，
  // 不能等删完才在结果里提一句（那时东西已经没了）。
  const risky = sel.filter(s => s.lock).length;
  tip.className = "store-tip err";
  tip.innerHTML =
    "<b>确定删掉这 " + sel.length + " 项（共 " + hsize(bytes) + "）吗？删了找不回来。</b>" +
    (risky ? "<div style='margin-top:4px'>⚠ 其中 <b>" + risky +
      " 项是角色正在用的模型</b>：删掉后对应角色会变回「还没训练」，" +
      "要再唱歌/朗读得重训一次。</div>" : "") +
    "<div style='margin-top:6px;display:flex;gap:8px;align-items:center;flex-wrap:wrap'>" +
    "<button class='btn sm danger' id='stDel2'>确定删除</button>" +
    "<button class='btn sm ghost' id='stCancel'>取消</button>" +
    "<span class='hint'>前几项：" + sel.slice(0, 3).map(s => esc(s.name)).join("、") +
    (sel.length > 3 ? " …" : "") + "</span></div>";
}

/* 当前真正会被删掉的那些项（勾选 ∩ 允许删）。
   三处（统计、按钮、删除）都从这里取 —— 以前各写一遍 `&& !it.lock`，
   解锁开关一加上就会漏掉一处，表现为"按钮上写着 3 项，实际删 2 项"。 */
function storeSelected() {
  return storeAllItems().filter(it => STORE_PICK.has(it.id) && storeCanPick(it));
}

async function storeDelete() {
  const sel = storeSelected();
  if (!sel.length) return;
  if (!STORE_ARMED) { storeArm(true, sel); return; }
  const payload = sel.map(it => ({ path: it.path, parts: it.parts || [] }));
  storeArm(false);
  const force = sel.some(it => !!it.lock);   // 只有动了"正在用"的才需要解锁
  const tip = $("#storeTip");
  if (tip) { tip.className = "store-tip"; tip.textContent = "正在删除…"; }
  let res;
  try {
    res = await post("/api/storage/delete", { items: payload, force: force });
  } catch (e) {
    if (tip) { tip.className = "store-tip err"; tip.textContent = "删除失败：" + e; }
    return;
  }
  const nfail = (res.failed || []).length;
  if (tip) {
    tip.className = nfail ? "store-tip err" : "store-tip ok";
    let t = "已删除 " + (res.deleted || []).length + " 项，释放 " +
      (res.freed_s || "0 B") + (nfail ? "；" + nfail + " 项没删成：" +
        (res.failed[0] || {}).why : "") +
      (res.log ? "（清单：" + res.log + "）" : "");
    // 删掉了角色在用的模型 ⇒ 角色记录已同步清空，必须告诉用户代价是什么
    const refs = res.refs || [];
    if (refs.length) t += "\n" + refs.join("\n");
    tip.textContent = t;
  }
  STORE_PICK.clear();
  await storeScan(true, true);
  toast("清理完成，释放 " + (res.freed_s || "0 B"), nfail ? "bad" : "good");
}

async function storePrune() {
  const btn = $("#stPrune");
  if (btn) btn.disabled = true;
  const tip = $("#storeTip");
  if (tip) { tip.className = "store-tip"; tip.textContent = "正在按设置瘦身…"; }
  try {
    const r = await post("/api/storage/prune", {});
    if (tip) {
      tip.className = (r.failed || []).length ? "store-tip err" : "store-tip ok";
      tip.textContent = "瘦身完成：删掉 " + (r.deleted || []).length + " 项，释放 " +
        (r.freed_s || "0 B");
    }
    toast("瘦身完成，释放 " + (r.freed_s || "0 B"), "good");
  } catch (e) {
    if (tip) { tip.className = "store-tip err"; tip.textContent = "瘦身失败：" + e; }
  }
  if (btn) btn.disabled = false;
  STORE_PICK.clear();
  await storeScan(true, true);
}

async function storeSaveSettings() {
  const keep = parseInt(($("#stKeep") || {}).value || "2", 10);
  const cg = parseFloat(($("#stCacheMax") || {}).value || "3");
  try {
    await post("/api/storage/settings", {
      patch: { keep_archives: isNaN(keep) ? 2 : keep,
               cache_max_gb: isNaN(cg) ? 3 : cg }
    });
    toast("已保存", "good");
  } catch (e) {
    toast("保存失败：" + e, "bad");
  }
}

(function storeWire() {
  const on = (sel, ev, fn) => { const e = $(sel); if (e) e.addEventListener(ev, fn); };
  on("#btnStore", "click", () => storeOpen());
  on("#stRescan", "click", () => storeScan(true));
  on("#stPrune", "click", () => storePrune());
  on("#stDel", "click", () => storeDelete());
  on("#stPickSafe", "click", () => {
    // 「安全项」永远不含"正在用的模型"—— 就算开了 🔓 也不替用户勾，
    // 一键勾选必须是闭着眼按下去也不会后悔的那种。
    storeAllItems().forEach(it => { if (it.suggest && !it.lock) STORE_PICK.add(it.id); });
    storeRender();
    toast("已勾选所有「删了不心疼」的项", "good");
  });
  on("#stClear", "click", () => { STORE_PICK.clear(); storeRender(); });
  on("#stForce", "change", (e) => {
    STORE_FORCE = !!e.target.checked;
    // 关掉开关时，刚才靠它勾上的那些要松开 —— 否则按钮上写着"3 项"，
    // 一点下去只删 1 项（另外两项被服务端拒了），用户看到的是"删不动"。
    if (!STORE_FORCE) {
      storeAllItems().forEach(it => { if (it.lock) STORE_PICK.delete(it.id); });
    }
    storeRender();
    const tip = $("#storeTip");
    if (tip) {
      tip.className = STORE_FORCE ? "store-tip err" : "store-tip";
      tip.textContent = STORE_FORCE
        ? "🔓 已解锁：可以勾「角色正在用的模型」了。删掉后那个角色会变回「还没训练」，需要重训一次。"
        : "";
    }
  });
  on("#stKeep", "change", storeSaveSettings);
  on("#stCacheMax", "change", storeSaveSettings);

  // 列表内的勾选 / 折叠：用事件委托，因为每次渲染都会整块重建 DOM
  const list = $("#storeList");
  if (list) list.addEventListener("change", (e) => {
    const cb = e.target.closest("[data-pick]");
    if (!cb) return;
    const id = cb.dataset.pick;
    if (cb.checked) STORE_PICK.add(id); else STORE_PICK.delete(id);
    const row = cb.closest(".store-it");
    if (row) row.classList.toggle("picked", cb.checked);
    storeUpdatePick();
  });
  if (list) list.addEventListener("click", (e) => {
    const h = e.target.closest("[data-fold]");
    if (!h) return;
    const gid = h.dataset.fold;
    if (STORE_FOLD.has(gid)) STORE_FOLD.delete(gid); else STORE_FOLD.add(gid);
    const g = h.closest(".store-g");
    if (g) g.classList.toggle("fold", STORE_FOLD.has(gid));
  });

  // 二次确认里的两个按钮：它们是运行时插进 #storeTip 的，只能委托
  const tip = $("#storeTip");
  if (tip) tip.addEventListener("click", (e) => {
    if (e.target.closest("#stDel2")) storeDelete();
    else if (e.target.closest("#stCancel")) storeArm(false);
  });
})();

// 顶栏角标：页面加载后静默扫一次。放在 3 秒后，别和首屏的渲染抢。
setTimeout(() => { storeScan(false); }, 3000);

/* ---------------------------------------------------------- 页内导航
 *
 * 为什么要这个：整页实测 **3990px / 视口 904px ≈ 4.4 屏**，而「💿 我的音乐」
 * 在 **1809px** 处 —— 它被 1328px 高的角色网格推到了第二屏。页面原先
 * **一条导航都没有**，要找到它就只能凭手感往下滚。
 * 后果是真实的：网易云 VIP 歌的入口其实一直排在候选目录第一位、扫描也正常，
 * 老板的反馈却是「不能看见网易云 VIP 的歌」—— **不是功能坏了，是入口看不见**。
 *
 * 三条纪律：
 *  · 不删任何区块，只加跳转（"简化"= 挪到手边，不是砍掉）
 *  · 跳转要扣掉 sticky 顶栏的高度，否则标题正好被压在顶栏底下
 *  · 事件一律在 JS 里绑，不写内联 onclick（路径/引号转义有坑，见 loc.js）
 */

/* 图标与文字分开存：emoji 在 UTF-16 里占两个码元，
   写成 "🎤 翻唱台" 再 slice(2) 取文字会切出半个代理对（还会带个前导空格）。 */
const NAV_ITEMS = [
  { id: "secStage", e: "🎤", t: "翻唱台" },
  { id: "secRoles", e: "👥", t: "角色" },
  { id: "locPanel", e: "💿", t: "我的音乐" },
  { id: "secVideos", e: "🎬", t: "做视频" },
  { id: "libPanel", e: "🎵", t: "在线曲库" },
  { id: "mktPanel", e: "🎭", t: "声音市场" },
];

function navBarH() {
  const b = document.querySelector(".top");
  return b ? b.getBoundingClientRect().height : 0;
}

function navGo(id) {
  const el = document.getElementById(id);
  if (!el) return;
  const y = el.getBoundingClientRect().top + window.scrollY - navBarH() - 12;
  window.scrollTo({ top: Math.max(0, y), behavior: "smooth" });
}

/* 高亮：取"已经越过顶栏、但还没被下一个区块挤掉"的那一个。
   判据是绝对文档坐标 <= 顶栏底边 —— 跟跳转用的是同一把尺子，
   否则会出现"跳过去了但高亮停在上一项"。 */
function navSync() {
  const probe = window.scrollY + navBarH() + 24;
  let cur = NAV_ITEMS[0].id;
  const live = NAV_ITEMS.filter((it) => document.getElementById(it.id));
  for (const it of live) {
    const el = document.getElementById(it.id);
    if (el && (el.getBoundingClientRect().top + window.scrollY) <= probe) {
      cur = it.id;
    }
  }
  // ★ 滚到底时最后一项永远高亮不上 —— 实测（2026-10-19）：市场区块只有 514px，
  //   比视口 904px 还矮，页面滚到底时它的绝对位置 4314 仍然大于探测线 4059，
  //   于是点了「市场」导航却还停在「曲库」上。区块比视口矮 ⇒ 它这辈子够不到探测线，
  //   跟页面多长无关。所以到底了就把"当前区块"判成最后一项 —— 用户眼睛看到的
  //   最后一个区块本来就是它。
  const de = document.documentElement;
  if (window.innerHeight + window.scrollY >= de.scrollHeight - 2 && live.length) {
    cur = live[live.length - 1].id;
  }
  const box = document.getElementById("navBar");
  if (!box) return;
  box.querySelectorAll(".nav-i").forEach((a) => {
    a.classList.toggle("on", a.dataset.id === cur);
  });
}

function navInit() {
  const box = document.getElementById("navBar");
  if (!box) return;
  box.innerHTML = NAV_ITEMS.filter((it) => document.getElementById(it.id))
    .map((it) => `<a class="nav-i" data-id="${it.id}" href="javascript:void(0)"
        title="跳到「${it.t}」">${it.e}&nbsp;${it.t}</a>`)
    .join("");
  box.querySelectorAll(".nav-i").forEach((a) => {
    a.addEventListener("click", (e) => {
      e.preventDefault();
      navGo(a.dataset.id);
    });
  });
  window.addEventListener("scroll", navSync, { passive: true });
  window.addEventListener("resize", navSync);
  navSync();
}

/* ---- 门禁快照：把「这套东西被验证过没有」摆在页面底部 ----
   34 道回归门禁平时只活在终端里。用户（以及一周后的自己）看到一个新按钮时，
   有权知道它有没有被判据覆盖过。这里只**读** data/gate_snapshot.json，
   绝不在页面刷新时触发跑门禁 —— 一轮全量要跑很久。 */
async function loadGateSnapshot() {
  const box = $("#gateSnap");
  if (!box) return;
  let d;
  try { d = await api("/api/gate-snapshot"); } catch (e) { return; }
  const s = d && d.snap;
  if (!s) return;                       // 还没跑过全量：宁可什么都不显示，也不编一个数
  const pf = s.platform || {};
  const ok = !!s.pass;
  const bits = [
    `<b>${esc(s.at)}</b>`,
    `${s.gates_ok}/${s.gates} 关通过`,
    `${s.checks} 条判据${s.checks_bad ? "（<b class='no'>" + s.checks_bad + " 条红</b>）" : "全绿"}`,
  ];
  if (pf.gpu) bits.push(esc(pf.gpu));
  if (pf.os) bits.push(esc(pf.os));
  // 每关判据数跟上一份快照比。为什么要把这句摆出来：
  // 1332 这个数字本身不可信 —— 某关从 48 掉到 40、总数不变时，
  // 快照看着一切正常，实际有一整个分支没跑。让"被守护"这件事可见。
  const df = s.diff || {};
  const dfText = df.text ? `<span class="gsdiff${df.warn ? " warn" : ""}">${esc(df.text)}</span>` : "";
  const triaged = ok && !!(s.failed && s.failed.length);
  box.innerHTML = `<span class="gsd ${ok ? "ok" : "bad"}" title="最近一次全量回归（_run_regression.sh）的结果。
不是实时跑的：数据来自 data/gate_snapshot.json，重跑一遍门禁才会更新。
退出码由判红分诊决定：全绿 0；全是假红/未执行也放行 0（会标注）；真回归/不稳定 1。">` +
    `<span class="gsm">${triaged ? "✓ 回归门禁通过（分诊放行）" : (ok ? "✓ 回归门禁全绿" : "⚠ 最近一次回归有没过的关")}</span>` +
    bits.join(" · ") +
    (s.failed && s.failed.length
      ? (ok
        // 放行但批跑红过：退出码由分诊结论决定，这里必须把这层说清楚，
        // 否则"全绿"两个字会把"这一轮其实红过 3 关"抹掉。
        ? ` · 批跑时 ${s.failed.length} 关曾红（${s.failed.map(esc).join("、")}），分诊判为假红/未执行`
        : ` · 没过：${s.failed.map(esc).join("、")}`)
      : "") +
    dfText +
    `</span>`;
  box.style.display = "";
}
