/* VoiceStudio 前端。原生 JS，无框架 —— 页面逻辑不复杂，引框架反而更重。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let ROLES = [];
let SUMM = null;         // /api/roles 给的汇总（可说话/可唱歌各几个）
let CUR = null;          // 当前打开的角色

/* 这个角色现在能不能唱歌：RVC 或 SVC 有一套权重就行。
   ★ 为什么需要这个函数：SVC 卡（卡名带「SVC」）本来就没有 RVC 权重，
     而界面上十几处判定写的是 `r.rvc && r.rvc.model` ⇒ 那些卡全被判成「没模型」，
     连按钮都是灰的。能不能唱只能看「有没有任意一套引擎的权重」。 */
function canSing(r) {
  return !!(r && ((r.rvc && r.rvc.model) || (r.svc && r.svc.model)));
}
let pickedSong = null;   // 待转换的歌曲路径
// 【踩过的坑·第四处】这一行也曾整行不见。写入（`pickedVideo = j.path`）在
// 非严格模式下会**悄悄造个全局**所以不报错，于是"先点＋导入视频再选文件"这条
// 路是通的（点按钮时顺手把它创建出来了）；但只要是**拖拽**进来的视频
// （走 window 的 drop 处理器打开导入框，没经过那个按钮），点「开始拆解」就
// `ReferenceError: pickedVideo is not defined` —— 表现为"按钮点了没反应"。
// ★ 这就是"靠运气覆盖 = 没覆盖"：正常路径碰巧不报错，掩盖了声明缺失。
let pickedVideo = null;  // 待导入的视频路径
let pickerMode = "video";
let STAGE_ROLE = null;   // 翻唱台选中的角色（= STAGE_CASTS[0]，老代码全按单角色写的）
// 【踩过的坑】这一行曾经整行不见了，而 stageSetSong 里还在 `STAGE_SONG = path`。
// 非严格模式下"给未声明的名字赋值"会**悄悄造一个全局**，所以写入不报错；
// 但 stageRefreshBtn 第一句就**读**它 —— 读一个没声明过的名字直接
// ReferenceError。于是「⚡ 一键合成」按钮永远点不亮（按钮初始就是 disabled，
// 只有这个函数会点亮它），翻唱台整块用不了，而后端接口全是好的。
// 这类"删掉一行声明"的坏编辑语法检查抓不到，只有真跑页面才看得见。
let STAGE_SONG = null;   // 翻唱台选中的歌
/* 翻唱台的**演唱名单**（1~4 人）。
   为什么要有它：以前大厅一次只能放一个角色，想对唱必须点开角色卡 →
   「唱歌」→ 把唱法切成多人合唱 —— 站在"大厅"里根本做不到。
   现在拖多张角色卡进①号槽就进名单，第 1 个人是主角色（STAGE_ROLE），
   其余人只在开跑时一起送进 sing_multi。
   ★ 单人时**完全走原来的路**（kind=sing）：大厅的"一键"能力（自动先训歌声
     模型、试听小样、改词）一点都不能因为这次改动而丢。 */
let STAGE_CASTS = [];
let STAGE_MODE = "phrase";   // 多人时的分配方式；重画槽位不能把它弄丢
let STAGE_GAIN = 0.55;       // 叠唱时的和声音量

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
  if (!r.ok) {
    // ★ 错误也要把服务端给的那份数据带上：像"这个已经在唱了"（409）这种，
    //   服务端会一并回 `job`，调用处才能直接把用户带到那个任务上，
    //   而不是只丢一句话让他自己去找。
    const e = new Error(j.error || ("请求失败 " + r.status));
    e.payload = j;
    e.status = r.status;
    throw e;
  }
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

/* 找角色。ROLE_Q 是全局的：刷新 / 新建角色 / 训练完回来都会重新 render()，
   搜索词得活过这些重绘，否则输一半被清掉，等于没法用。
   只匹配**屏幕上看得见**的两个字段（名字、介绍）—— 用户输的就是他看到的字，
   拿 id 或路径去匹配只会让人以为搜到了看不见的东西。 */
let ROLE_Q = "";

function roleMatch(r, q) {
  return ((r.name || "") + " " + (r.intro || "")).toLowerCase().indexOf(q) >= 0;
}

function roleFind() {
  const e = $("#roleFind");
  ROLE_Q = e ? String(e.value || "").trim().toLowerCase() : "";
  render();
}

function roleFindClear() {
  ROLE_Q = "";
  const e = $("#roleFind");
  if (e) e.value = "";
  render();
}

/* ------------------------------------------------------ 多选删除
   【为什么要有】角色只会越来越多（市场还在加），一个一个点进抽屉再删太慢；
   而"换一套角色重来"是真实会做的事 —— 那就得能全选一次删掉。

   【三条规矩】
     · 多选模式下卡片**不再打开详情、也不能拖**：点选和拖拽抢的是同一个按下动作，
       同时开只会互相打架（拖一下就变成勾了一下）。
     · 「全选」只作用于**当前列表**（受搜索过滤），不是"不管筛了什么都勾上" ——
       搜出 3 个却把 39 个全删了，是没法撤销的事故。
     · 删除走批量接口，**逐个报告失败**：文件被占用时 rmtree 会静默失败，
       界面绝不能假装全成功（否则角色没了、磁盘还占着，用户再没有入口去删）。 */
let PICK_ON = false;
const PICKED = new Set();

function pickToggle() { PICK_ON = !PICK_ON; PICKED.clear(); render(); }
function pickExit() { PICK_ON = false; PICKED.clear(); render(); }

/* 当前列表 = 屏幕上真正画出来的那些（搜索词生效时就是搜索结果的子集） */
function pickVisible() {
  const q = ROLE_Q;
  return q ? ROLES.filter(r => roleMatch(r, q)) : ROLES;
}

function pickAll(on) {
  PICKED.clear();
  if (on) pickVisible().forEach(r => PICKED.add(r.id));
  render();
}

function pickHit(rid) {
  if (PICKED.has(rid)) PICKED.delete(rid);
  else PICKED.add(rid);
  render();
}

async function pickDelete() {
  const ids = Array.from(PICKED);
  if (!ids.length) { toast("还没勾选角色", "bad"); return; }
  const names = ROLES.filter(r => PICKED.has(r.id)).map(r => r.name || r.id);
  const show = names.slice(0, 6).join("、") +
               (names.length > 6 ? ` 等 ${names.length} 个` : "");
  if (!confirm(`确定删除这 ${ids.length} 个角色？\n\n${show}\n\n`
               + "它们的素材和模型记录都会删掉（权重文件本身保留在整合包里）。")) return;
  const btn = $("#btnPickDel");
  const btnHtml = btn ? btn.innerHTML : "";    // 里面有个「已勾选几个」的计数，一会儿要还回去
  if (btn) { btn.disabled = true; btn.textContent = "删除中…"; }
  let r;
  try {
    r = await post("/api/roles/delete", { ids: ids });
  } catch (e) {
    toast("删除失败：" + (e.message || e), "bad");
    if (btn) { btn.disabled = false; btn.innerHTML = btnHtml; paintPickBar(); }
    return;
  }
  const n = (r && r.deleted || []).length;
  const bad = (r && r.failed) || [];
  PICKED.clear();
  PICK_ON = false;
  await loadRoles();
  if (bad.length) {
    toast(`删了 ${n} 个，${bad.length} 个没删干净（${bad[0].id}：${bad[0].err}）`, "bad");
  } else {
    toast(`已删除 ${n} 个角色`, "good");
  }
}

function render() {
  const g = $("#grid");
  const q = ROLE_Q;
  const list = q ? ROLES.filter(r => roleMatch(r, q)) : ROLES;
  // 汇总：装了十几个角色之后，"到底哪几个现在就能用"是第一个问题。
  $("#roleCount").textContent = ROLES.length
    ? `共 ${ROLES.length} 个`
      + (q ? ` · 找到 ${list.length} 个` : "")
      + (SUMM && !q
        ? ` · 🎤 可唱歌 ${SUMM.can_sing} · 💬 可说话 ${SUMM.can_speak}`
        : "")
    : "";
  $("#emptyBox").style.display = ROLES.length ? "none" : "block";
  // 搜不到必须**说清楚**：空白会被当成"角色丢了"。
  const fe = $("#findEmpty");
  if (fe) {
    const show = !!q && ROLES.length > 0 && list.length === 0;
    fe.style.display = show ? "block" : "none";
    if (show && $("#findEmptyQ")) $("#findEmptyQ").textContent = q;
  }
  const fx = $("#roleFindX");
  if (fx) fx.style.display = q ? "inline-flex" : "none";
  // 多选态：先清掉"已经不存在的 id"（删完 / 别处删过），否则计数会越删越大
  if (PICK_ON) {
    const alive = new Set(ROLES.map(r => r.id));
    Array.from(PICKED).forEach(id => { if (!alive.has(id)) PICKED.delete(id); });
  }
  g.innerHTML = list.map(r => {
    const st = r.stats || {};
    const color = r.color || COLORS[0];
    const initial = (r.name || "?").trim().slice(0, 1).toUpperCase();
    const a = r.ability || {};
    // 走不通的入口**弱化**而不是删掉：点进去能看到为什么（说话页会给出横幅），
    // 但一眼就知道它现在出不了东西，不用白填一段字再等一个失败任务。
    const spOff = a.speak ? a.speak.state !== "ready" : false;
    const sgOff = a.sing ? a.sing.state === "blocked" : false;
    // 多选态下卡片换一套行为：点=勾选、不拖、不打开详情
    const picked = PICK_ON && PICKED.has(r.id);
    const cls = "card" + (PICK_ON ? " pickmode" : "") + (picked ? " picked" : "");
    const drag = PICK_ON ? "false" : "true";
    const click = PICK_ON ? ("pickHit('" + r.id + "')") : ("openRole('" + r.id + "')");
    const corner = PICK_ON
      ? '<span class="pickbox">✓</span>'
      : '<span class="dragdot" title="按住这张卡可以拖 —— 拖到上面的「歌手」槽就能用它唱歌">⠿ 可拖</span>';
    // ★ 这里**故意只有两个按钮**。试过加第三个「🎬 加入翻唱」，实测：卡内一行
    //   只有 259px，三个按钮要 291px ⇒ 必然换行、卡高 256→296（人体工学关当场红）。
    //   而"点一下就能把人放进歌手槽"其实**本来就有**——点歌手槽弹演唱名单（openCastList）；
    //   拖拽的真正病根（拖到边缘不滚、跨不了几屏）已经由下面的 dragScrollStep 解决。
    //   ⇒ 别为了一个已经有入口的功能，把每张卡都撑高。
    const acts = PICK_ON ? "" : `
      <div class="acts">
        <button class="btn sm${spOff ? " off" : ""}" title="${esc(a.speak ? a.speak.why : "")}"
          onclick="event.stopPropagation();openRole('${r.id}','paneSpeak')">💬 TA 说话</button>
        <button class="btn sm${sgOff ? " off" : ""}" title="${esc(a.sing ? a.sing.why : "")}"
          onclick="event.stopPropagation();openRole('${r.id}','paneSing')">🎤 TA 唱歌</button>
      </div>`;
    return `
    <div class="${cls}" draggable="${drag}" data-role="${r.id}" onclick="${click}">
      <div class="glow" style="background:${color}"></div>
      ${corner}
      <div class="row1">
        ${avatarHtml(r)}
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
      </div>${acts}
    </div>`;
  }).join("");
  paintPickBar();
}

/* 工具栏随多选态换形：不在多选时只有一个「☑ 多选」，进去之后才出现全选/删除 */
function paintPickBar() {
  const pb = $("#pickBar");
  if (pb) pb.style.display = PICK_ON ? "inline-flex" : "none";
  const bp = $("#btnPick");
  if (bp) {
    bp.textContent = PICK_ON ? "✕ 退出多选" : "☑ 多选";
    bp.classList.toggle("on", PICK_ON);
    bp.title = PICK_ON ? "回到普通模式（点卡片打开详情、可拖）"
                       : "勾选若干个角色一起删；也可以全选";
  }
  const pn = $("#pickN");
  if (pn) pn.textContent = String(PICKED.size);
  const del = $("#btnPickDel");
  if (del) del.disabled = PICKED.size === 0;
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
  // 打开一张只有 SVC 权重的卡 ⇒ 引擎默认切到 svc（否则界面说「没模型」、点了也不唱）
  const _eng0 = $("#selEngine");
  if (_eng0) {
    _eng0.value = (!(j.role.rvc && j.role.rvc.model) && j.role.svc && j.role.svc.model)
      ? "svc" : "rvc";
  }
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
  if (r.avatar) {
    av.className = "avatar hasimg";
    av.style.background = "";
    av.innerHTML = `<img src="${media(r.avatar)}" alt="">`;
  } else {
    av.className = "avatar";
    av.textContent = (r.name || "?").trim().slice(0, 1).toUpperCase();
    av.style.background = `linear-gradient(135deg,${color},${shade(color)})`;
  }
  // 点头像就能换一张 —— 比在简介里翻一个按钮更容易发现
  av.title = "点一下换一张头像";
  av.onclick = () => pickAvatar();
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
  $("#rvcInfo").textContent = (r.rvc && r.rvc.model)
    ? `已有歌声模型：${r.rvc.model}（${r.rvc.at}${r.rvc.index ? "，含特征索引库" : "，无索引库"}）`
    : ((r.svc && r.svc.model)
      ? `第二引擎（SVC）权重：${String(r.svc.model).split(/[\\/]/).pop()}（唱歌时引擎选 SVC）`
      : "还没有歌声模型。");
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
  // ★ 换了角色 ⇒ 多人名单必须跟着重画。
  //   `#multiRoles` 是**静态 DOM**，只有点「唱法」下拉时才会 paintMultiRoles；
  //   而 `#selMulti` 会保持用户上次选的「多人」⇒ 从云宝点进去，名单还是上一个人
  //   勾的「大帝 / 苹果丽丽」，云宝本人不在里面。老板看到的"角色名字一直没用变"
  //   就是这个。唱法开关本身不动（别把用户刚选的模式清掉）。
  if (($("#selMulti") || {}).value === "on") {
    paintMultiRoles();
    loadMultiPlan();
  }
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
      ${o.lyric_srt ? `<a class="btn sm" href="${media(o.lyric_srt)}" download
        title="字幕文件，剪辑软件 / 播放器都能直接挂上">下 .srt</a>` : ""}
      ${!o.lyric && o.stems && o.stems.src
        ? `<button class="btn sm" data-needlyr="${i}">识别歌词</button>` : ""}
      ${o.wav ? `<button class="btn sm" data-viz="${i}"
        title="把这条成品配上频谱画面，渲染成能直接发出去的 MP4">🎬 做视频</button>` : ""}
      ${soloBlock(o, i)}
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
  // 「独唱」：i 是成品下标、k 是第几个演唱者（`i-k` 一起塞在 data-solo 里，
  // 因为没人保证只有一个人 —— 8 个人就是 8 个按钮）
  box.querySelectorAll("button[data-solo]").forEach(b => {
    b.onclick = () => {
      const p = String(b.dataset.solo).split("-").map(Number);
      playSolo(p[0], p[1], b);
    };
  });
}

/* 头像：有图就用图，没图回到「首字母 + 该角色自己的渐变」。
   ★ 角色墙 / 抽屉 / 歌手槽都走这一支 —— 各写一遍的话，一定会出现
     "卡上有图、点进去还是首字母"这种半吊子状态。 */
function avatarHtml(r, cls) {
  const k = cls || "avatar";
  const c = (r && r.color) || COLORS[0];
  const initial = ((r && r.name) || "?").trim().slice(0, 1).toUpperCase();
  if (r && r.avatar) {
    return `<div class="${k} hasimg"><img src="${media(r.avatar)}" alt="" loading="lazy"></div>`;
  }
  return `<div class="${k}" style="background:linear-gradient(135deg,${c},${shade(c)})">${esc(initial)}</div>`;
}

function roleSlice(f) {
  return (CUR && CUR._paths && CUR._paths[f]) || f;
}

/* 多角色成品里「把某一个人单独拎出来听」------------------------------
   ★ 这些独唱轨**本来就有**：多角色是每个角色各唱一遍再拼的，那几条轨一直在
     final 目录里当中间产物。以前没人能看到它们，用户听到
     "这里面有个人唱得更合我意"，却没有任何办法单独听那一个人。
   ★ 2026-10-22 措辞修正：这里是"整首**长度**的轨"，但开了只唱分到的那几句
     之后，轨里**只有 TA 分到的那几句有声**，其余是静音 —— 标签再写"各唱一遍
     整首"，用户点开会以为坏了（听一半没声）。下面那行文案跟着改成实话。
   ★ 带伴奏版按需混（点一下几秒）：每个版本都是一整首歌的大小，8 个人
     预先混好就是几百 MB，而用户往往只想听一两个。
*/
function soloBlock(o, i) {
  // 同一条合唱会登记两条记录（成品 / 人声干声），独唱块只在成品那条上出现，
  // 否则同一个歌手名单会出现两遍 —— 看着像装了两套。
  if (o.track && !String(o.track).startsWith("成品")) return "";
  const solos = (o.params && o.params.solo_tracks) || [];
  if (!solos.length) return "";
  const chips = solos.map((s, k) => `
    <span class="solo">
      <button class="mini" data-solo="${i}-${k}"
        title="把「${esc(s.name)}」一个人的版本混上伴奏放出来（只有 TA 分到的那几句有声）">▶ ${esc(s.name)}</button>
      ${s.mix ? `<a class="mini" href="${media(s.mix)}" download title="下载这个人的独唱版">↓</a>` : ""}
    </span>`).join("");
  return `<div class="solos"><span class="sl">各人的单唱版（只有 TA 唱的那几句）：</span>${chips}</div>`;
}

async function playSolo(i, k, btn) {
  if (!CUR) return;
  const o = CUR.outputs[i];
  const s = (((o || {}).params || {}).solo_tracks || [])[k];
  if (!s) return;
  const oldTxt = btn.textContent;
  let src = s.mix;
  if (!src) {
    btn.disabled = true;
    btn.textContent = "混伴奏…";
    try {
      const d = await post("/api/multi/solo", { role: CUR.id, index: i, who: k });
      if (d && d.error) {
        toast(d.error, "bad");
        btn.disabled = false; btn.textContent = oldTxt;
        return;
      }
      src = d.mix;
      s.mix = d.mix;                 // 记在内存里，切来切去不用再混一次
    } catch (e) {
      toast(String((e && e.message) || e), "bad");
      btn.disabled = false; btn.textContent = oldTxt;
      return;
    }
    btn.disabled = false; btn.textContent = oldTxt;
  }
  // 播放器就地插在按钮下面：**不替换按钮**（替换掉就再也点不到第二次，
  // 想来回对比两个人的独唱还得刷新页面 —— 正是这个功能要解决的问题）。
  const host = btn.parentElement;
  let au = host.querySelector("audio");
  if (!au) {
    au = document.createElement("audio");
    au.controls = true;
    au.style.width = "100%";
    au.style.marginTop = "4px";
    host.appendChild(au);
  }
  au.src = media(src);
  au.play().catch(() => { /* 浏览器拦自动播放：用户自己点播放键 */ });
}

/* ---------------------------------------------------------- 头像 */

function pickAvatar() {
  const f = $("#fAvatar");
  if (!f) {
    toast("页面里没有头像输入框（需要刷新一下）", "bad");
    return;
  }
  f.value = "";            // 同一个文件选第二次也要能触发 change
  f.click();
}

async function uploadAvatar(file) {
  if (!CUR || !file) return;
  const hint = $("#avHint");
  const say = (t) => { if (hint) hint.textContent = t; };
  if (!/^image\//.test(file.type || "")) {
    toast("这不是图片 —— 要 jpg / png / webp", "bad");
    return;
  }
  say("正在存…");
  try {
    // 直接发 File 本体（raw body）：服务端只在 URL 上取一个原始文件名，
    // 用来判断后缀。multipart 在这里是多余的复杂度。
    const res = await fetch(
      `/api/role/${encodeURIComponent(CUR.id)}/avatar?name=${encodeURIComponent(file.name || "a.png")}`,
      { method: "POST", headers: { "Content-Type": "application/octet-stream" }, body: file });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || d.error || d.ok === false) {
      throw new Error(d.err || d.error || ("HTTP " + res.status));
    }
    CUR.avatar = d.avatar;
    paintRole();
    await loadRoles();       // 角色墙上的卡也要跟着换
    toast(d.cropped
      ? `头像已存 —— 原图不是方的，已居中裁成 ${d.px}×${d.px}`
      : `头像已存（${d.px}×${d.px}，${d.kb}KB）`, "good");
    say("方形最好；非方形会自动居中裁成方形（512×512）");
  } catch (e) {
    const m = String((e && e.message) || e);
    toast("头像没存上：" + m, "bad");
    say("没存上：" + m);
  }
}

async function clearAvatar() {
  if (!CUR || !CUR.avatar) { toast("现在就是首字母头像", "bad"); return; }
  if (!confirm("撤掉头像，回到「首字母 + 颜色」那块？")) return;
  try {
    const d = await fetch(`/api/role/${encodeURIComponent(CUR.id)}/avatar`,
      { method: "DELETE" }).then(r => r.json());
    if (d.err) throw new Error(d.err);
    delete CUR.avatar;
    paintRole();
    await loadRoles();
    toast("已撤掉头像", "good");
  } catch (e) {
    toast("没撤掉：" + String((e && e.message) || e), "bad");
  }
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

/* ---- 变调跨度的失真提醒 ----
   ★ 为什么需要：界面上 ±12 以前只写好处（"男声唱女声原曲"），不写代价。
     结果用户把它当成正经选项，唱出来一股电音也不知道是自己选错了。
     2026-10-24 实测（苹果嘉儿 + Old Town Road，同一模型、同一套 index/rms/
     protect，只改 pitch）：
         pitch  +0 → 谐波噪声比 14.9 dB、周期微扰 6.3%、高频占比 0.95%
         pitch +12 → 谐波噪声比 19.0 dB、周期微扰 5.2%、高频占比 3.45%
     参考值：原唱真人        谐波噪声比  9.6 dB、周期微扰 9.9%、高频占比 0.97%
     ⇒ 一个八度会把 4~8kHz 的能量推高 3.6 倍，这就是"金属/电音"的来源。
     规则不是拍的，按这三个数分档：超过半个八度开始偏离真人，一个八度明显失真。 */
const PITCH_WARN_FROM = 6;   // |半音| >= 这个值开始提醒
const PITCH_BAD_FROM = 11;   // |半音| >= 这个值算"明显失真"

function pitchRisk(n) {
  const v = Math.abs(parseInt(n, 10) || 0);
  if (v >= PITCH_BAD_FROM) {
    return {
      lv: "bad",
      msg: "±" + v + " 是整整一个八度 —— 实测会把 4~8kHz 的高频推到原调的 3.6 倍，"
         + "听上去就是明显的金属声/电音。这首歌和这个角色音区差太多："
         + "换一个音区接近的角色，比硬拉八度好得多。"
    };
  }
  if (v >= PITCH_WARN_FROM) {
    return {
      lv: "warn",
      msg: "±" + v + " 已经超过半个八度，齿音和金属感会从这里开始冒头。"
         + "还想要更高的调，优先换一个音区接近的角色。"
    };
  }
  return { lv: "", msg: "" };
}

function paintPitchWarn(n, box, cls) {
  const b = $(box);
  if (!b) return;
  const r = pitchRisk(n);
  b.textContent = r.msg;
  // 没有 .bad 这个类（只有 .warn 是红的），所以两档颜色用 CSS 变量直接设，
  // 不依赖样式表里碰巧存在某个类名。
  b.className = cls || "hint";
  b.style.color = r.lv === "bad" ? "var(--bad)" : (r.lv === "warn" ? "var(--warn)" : "");
  b.style.fontWeight = r.lv ? "600" : "";
  b.style.display = r.msg ? "" : "none";
  b.title = r.msg;
}

function onPitchPick() {
  const sel = $("#selPitch");
  const w = $("#pitchCustomWrap");
  if (sel && w) w.style.display = (sel.value === "custom") ? "" : "none";
  paintPitchWarn(pickedPitch(), "#pitchWarn", "hint");
}

/* 合唱台那根 slider 同样处理 —— 两个入口必须同一套口径，
   否则"在角色页被警告过，在合唱台就能随便拉到 ±12"。 */
function sgPitchWarn() {
  const s = $("#sgPitch");
  paintPitchWarn(s ? s.value : 0, "#sgPitchWarnBox", "hint");
}

/* ---- 多人合唱：勾人 + 分配方式 -------------------------------------
   ★ 只列「能唱」的角色（有 RVC 模型）。把唱不了的角色也摆上去，用户勾完点了
     才报错 —— 那是最差的一种交互（而且"说话模型不能唱歌"这件事他记不住）。
   ★ 变调默认留空 = 自动（每个角色各自按自己的音域量）。不默认 0：男声角色
     唱女声原曲用 0 会整首卡在自己的音区里，听起来像"念"不像"唱"。 */
/* 上一轮这份名单是为哪个角色画的。
   ★ 换角色必须重排勾选：`keep` 会保留上次勾的人 ⇒ 在苹果嘉儿那儿勾了
     大帝/苹果丽丽，点开云宝还是这两个，云宝本人根本不在名单里 ——
     老板的原话就是「角色名字一直没用变」。 */
let MULTI_ROLES_FOR = null;

function paintMultiRoles() {
  const box = $("#multiRoles");
  if (!box) return;
  // 拷贝一份再排：ROLES 是全局的，就地 splice 会把角色墙的顺序一起改掉
  // 对唱/叠唱里每个角色按**自己卡上的引擎**唱（RVC / SVC 都行），
  // 所以名单按 canSing 过滤，不再只认 RVC。
  const can = (ROLES || []).filter(canSing).slice();
  if (!can.length) {
    box.innerHTML = '<div class="hint">还没有任何角色训过歌声模型 —— '
      + '先给一个角色训好 RVC，再来对唱。</div>';
    return;
  }
  const curId = (CUR && CUR.id) || "";
  // 已经勾过的人保持勾选（重绘时不丢），第一次进来 / 换了角色则重排
  const keep = new Set(Array.from(box.querySelectorAll("input[type=checkbox]"))
    .filter(i => i.checked).map(i => i.value));
  const first = keep.size === 0 || MULTI_ROLES_FOR !== curId;
  // ★★ 默认勾的是**当前角色 + 另一个人**，不是"列表里前两个"。
  //   以前固定勾 can[0]/can[1] ⇒ 从任何角色点进来名单都是「大帝 / 苹果丽丽」，
  //   当前角色压根不在里面：看着像"角色挂错了"，实际是默认值没跟着人走。
  //   当前角色还要排到第 1 位 —— 叠唱模式下第一个勾的是主唱。
  if (first && curId) {
    const at = can.findIndex(r => r.id === curId);
    if (at > 0) { const c0 = can.splice(at, 1)[0]; can.unshift(c0); }
  }
  MULTI_ROLES_FOR = curId;
  box.innerHTML = can.map((r, i) => {
    const on = first ? (i < 2) : keep.has(r.id);
    return '<label class="mrole">'
      + '<input type="checkbox" value="' + esc(r.id) + '"' + (on ? " checked" : "")
      + ' onchange="onMultiRolesChanged()">'
      + '<span>' + esc(r.name || r.id) + '</span>'
      + '<input class="inp" type="number" min="-24" max="24" placeholder="自动"'
      + ' style="width:64px;padding:2px 6px;font-size:12px"'
      + ' title="这个角色的变调（半音）。留空 = 按它自己的音域自动估">'
      + '</label>';
  }).join("");
}

/* 勾了谁、去掉谁 → 分配表要重排（"谁唱多少"跟着人数变）。
   不重排的话预览停在上一次的人数上，用户会照着错的表决定要不要开跑。 */
function onMultiRolesChanged() {
  loadMultiPlan();
}

function onMultiPick() {
  const on = ($("#selMulti") && $("#selMulti").value) === "on";
  const w = $("#multiWrap");
  const m = $("#multiModeWrap");
  if (w) w.style.display = on ? "" : "none";
  if (m) m.style.display = on ? "" : "none";
  // ★ 引擎选择在多人模式下**真的不生效**（sing_multi 只走 RVC）。
  //   以前是"选了也不理"——属于最招人恨的那种静默失效。这里显式置灰并说清原因。
  // ★ 唱前/唱后这 11 步只在 sing_role 那条路上跑；sing_multi 有自己的链路
  //   （每人整首 RVC → 拼接 → 混伴奏），**不读** pre_skip/post_skip。
  //   以前这组勾选框在多人模式下照样能点、点了没反应 —— 和 selEngine 同一种静默失效。
  //   这里同样显式置灰 + 说清原因（不删控件：切回单人立刻可用）。
  [$("#swPre"), $("#swPost")].forEach((box) => {
    if (!box) return;
    box.querySelectorAll("input").forEach((el) => { el.disabled = on; });
  });
  const prSw = $("#swPresence");
  if (prSw) prSw.disabled = on;
  const swWhy = $("#swWhy");
  if (swWhy) swWhy.style.display = on ? "" : "none";
  const eng = $("#selEngine");
  if (eng) {
    eng.disabled = on;
    eng.title = on
      ? "多人时每个角色按自己卡上的引擎唱（RVC / SVC 自动），这里不用选"
      : "";
  }
  if (on) {
    paintMultiRoles();
    loadMultiPlan();
  }
  onMultiMode();
}

function onMultiMode() {
  const mode = ($("#selMultiMode") && $("#selMultiMode").value) || "phrase";
  const g = $("#multiGainWrap");
  // ★ manual 也要显示和声音量：逐句指定里可以把某几句标成"合唱"，
  //   那些句里非主唱的音量就是这个滑块 —— 只给 chorus 显示的话，
  //   用户标了合唱却找不到地方调它（看着像功能没做全）。
  if (g) {
    g.style.display = ((mode === "chorus" || mode === "manual") &&
      ($("#selMulti") && $("#selMulti").value) === "on") ? "" : "none";
  }
  loadMultiPlan();
}

/* 从勾选框里取当前参与的人（顺序 = 勾选顺序在 DOM 里的顺序）。 */
function multiPicks() {
  const box = $("#multiRoles");
  const picks = [];
  if (!box) return picks;
  box.querySelectorAll("label.mrole").forEach(lb => {
    const cb = lb.querySelector('input[type="checkbox"]');
    if (!cb || !cb.checked) return;
    const pr = lb.querySelector('input[type="number"]');
    const raw = pr ? String(pr.value).trim() : "";
    let p = "auto";
    if (raw !== "") {
      const n = parseInt(raw, 10);
      p = Number.isFinite(n) ? Math.max(-24, Math.min(24, n)) : "auto";
    }
    picks.push({ rid: cb.value, pitch: p });
  });
  return picks;
}

/* 分配表预览 / 手动逐句安排 ------------------------------------------
   ★ 这里只调 /api/multi/plan（纯算时间轴），**一次推理都不跑** ——
     所以可以随便改、随便重排。
   ★ 手动模式必须真的有句子可选：没有时间戳时（这首歌没认过词）
     按钮会明说"开跑时会先自动识别"，而不是给一块空白让人猜。 */
let MULTI_PLAN_TOKEN = 0;
let MULTI_PLAN = null;

async function loadMultiPlan(force) {
  const box = $("#multiPlan");
  if (!box) return;
  const on = ($("#selMulti") && $("#selMulti").value) === "on";
  if (!on || !pickedSong) { box.style.display = "none"; return; }
  const picks = multiPicks();
  if (picks.length < 2) { box.style.display = "none"; return; }
  box.style.display = "";
  const mode = ($("#selMultiMode") && $("#selMultiMode").value) || "phrase";
  const tok = ++MULTI_PLAN_TOKEN;
  try {
    const d = await post("/api/multi/plan", {
      song: pickedSong, casts: picks, mode,
      manual: (mode === "manual" && MULTI_PLAN && MULTI_PLAN.can_manual)
        ? currentManual() : null,
    });
    if (tok !== MULTI_PLAN_TOKEN) return;      // 后发的请求赢，别被旧响应覆盖
    if (d.error) { $("#multiPlanHint").textContent = d.error; return; }
    MULTI_PLAN = d;
    renderMultiPlan(d, mode);
  } catch (e) {
    if (tok !== MULTI_PLAN_TOKEN) return;
    const msg = String(e && e.message || e);
    $("#multiPlanHint").textContent = msg.indexOf("404") >= 0
      ? "分配表预览需要重启服务（当前是旧进程）" : msg;
  }
}

function currentManual() {
  return manualOf($("#multiLines"));
}

function renderMultiPlan(d, mode) {
  const sum = $("#multiPlanSum"), hint = $("#multiPlanHint"), list = $("#multiLines");
  // 歌词来源条（含"自己导入"）—— 句子是从歌词来的，用错歌的词整列都是别人的
  lrcBarRefresh("#multiLrc", pickedSong, () => loadMultiPlan(true));
  const per = (d.per_role || []).map(x => `${x.name}：${x.sec} 秒 / ${x.parts} 段`);
  sum.textContent = (mode === "chorus")
    ? `叠唱 · 全曲 ${d.duration} 秒（第一个人当主唱）`
    : `全曲 ${d.duration} 秒 · ` + per.join("　");
  if (mode === "manual" && !d.lyric_ready) {
    hint.innerHTML = `这首歌还没认过词 ⇒ 现在是按时间切的 <b>${d.lines.length}</b> 段`
      + `（切点会落在句子中间，不是真的句）。`
      + `<button class="btn sm" id="btnLyricFirst" style="margin:0 6px">`
      + `先认一次词（一两分钟）</button>`
      + `<br>认完下面的段会换成<b>真句子</b>，就能对着歌词挑「哪一句谁唱」了。`
      + `<b>必须认</b>：不认的话开跑时会自动认一次，句子数一变，`
      + `你现在点的这份指定就对不上了。`
      + `不认也照样能逐段指定：先点上面一支笔，再连着点下面的段。`;
    // ★ 服务端那段话（"已按时间对齐" / "XX 没有独唱句"）在**每个分支**都要显示：
    //   以前只有最底下那个 else 才追加 d.hint ⇒ 逐句指定模式下服务端说的话
    //   一句都到不了界面，等于白算（2026-10-24 第五轮）。
    // ★★ 必须**先追加、后绑定**：`innerHTML +=` 会把整块重新解析一遍，
    //   之前那个「先认一次词」按钮是**新元素**（onclick 没了）—— 实测就是这么
    //   把按钮点不动的（门禁"点了会真去认词"那条当场抓到）。
    if (d.hint) hint.innerHTML += `<br><span class="mwarn">⚠ ${esc(d.hint)}</span>`;
    const _bl = hint.querySelector("#btnLyricFirst");
    if (_bl) _bl.onclick = onLyricFirst;
  } else if (!d.lyric_ready) {
    hint.innerHTML = '这首歌还没认过歌词 ⇒ 现在只能按时间均分（切点会落在句子中间）。'
      + '开跑时会先<b>自动识别一次</b>，识别成了就按真实句子分。';
    if (d.hint) hint.innerHTML += `<br><span class="mwarn">⚠ ${esc(d.hint)}</span>`;
  } else if (mode === "manual") {
    // ★ 句子**是哪来的**要照实说：外部歌词（.lrc）的分段 / 认出来的 / 按时间切的
    //   —— 说清楚来源，才知道这一列能不能信。
    const srcTxt = d.line_src === "import"
      ? `用<b>你导入的那份歌词</b>共 <b>${d.lines.length}</b> 句（中英双语，一句没合并）`
      : d.line_src === "lrc"
        ? `按<b>外部歌词（.lrc）的分段</b>共 <b>${d.lines.length}</b> 句（中英双语，一句没合并）`
        : d.line_src === "asr"
        ? `认到 <b>${d.lines.length}</b> 句（中英双语，一句没合并）`
        : `这首歌没有歌词 ⇒ 现在是按时间切的 <b>${d.lines.length}</b> 段`;
    hint.innerHTML = srcTxt + ` —— 先点上面<b>一支笔</b>，`
      + `再连着点下面的歌词（点错就换支笔再点一次）；也可以一句话全给同一个人。`
      + `想让<b>某一句变成合唱</b>：点最右边那支「合唱（所有人）」笔，再点那几句。`
      + (d.zh_ready ? "" : `<br><span class="mwarn">⚠ 中文没翻出来（翻译服务没通）`
        + ` ⇒ 现在只有原文，照样能逐句指定。</span>`);
    if (d.hint) hint.innerHTML += `<br><span class="mwarn">⚠ ${esc(d.hint)}</span>`;
  } else {
    hint.innerHTML = `按 <b>${d.lines.length}</b> 句的真实换气位置分（${d.takes.length} 段）。`;
    // ★ 服务端会算出"这组数够不够分给这么多人"。不够时它会自动按句对半拆，
    //   并让 hint 带一句话 —— 必须显示出来：否则用户看到的是一份"正常"的
    //   分配表，跑完却发现自己只听到一个人（本轮实测缺陷）。
    if (d.hint) hint.innerHTML += `<br><span class="mwarn">⚠ ${esc(d.hint)}</span>`;
  }
  // ★ 以前这里是 `mode !== "manual" || !d.lyric_ready` —— 歌没认过词就把整个
  //   逐句指定面板藏起来，用户看到的是"这个选项点了没反应"。
  //   现在没词也有"按时间切的段"可以指定（后端拿均分段当伪句子），所以只按模式藏。
  if (mode !== "manual") {
    list.innerHTML = "";
    list.style.display = "none";
    return;
  }
  list.style.display = "";
  const names = (d.names || []);
  MULTI_NAMES = names;
  // 人数变了（勾掉一个人）时笔的下标可能越界 —— 别留一支点不到的笔
  MULTI_PEN = Math.min(MULTI_PEN, Math.max(0, names.length - 1));
  // ★ 「合唱」是第 -1 支笔：点它，接下来点的那些句就变成"所有人一起唱"。
  //   它必须**能走到后端**（后端 ROLE_ALL = -1）—— 前端画个按钮而后端不认 -
  //   1，症状就是"按钮点了没反应"，一句合唱都出不来。
  list.innerHTML = manualPensHTML(names, MULTI_PEN, "multiPens")
    + `<div class="mlines">${manualRowsHTML(d, names)}</div>`;
  wireManualList(list, {
    setPen: k => paintPen(k, list),
    all: () => assignAllLines(),
    line: el => setLineWho(el, MULTI_PEN, true, names, () => onManualPick()),
  });
}

/* ---- 逐句指定的「笔 + 歌词行」：大厅和角色页共用同一份 ----
   ★ 为什么必须共用、不许抄成两份：这两处画的是同一样东西（一句一行、点一下归谁）。
     抄两份的话，下次调行距/改合唱笔只会改到一边，另一边会一直停在旧样子 ——
     而"我改了怎么没生效"是最难查的那一类。
     （上一轮大厅干脆不给这个选项，理由是"没有逐句编辑器" ⇒ 老板到现在
      还是没法决定哪一句谁唱。这一轮就是把它补上。） */
function manualPensHTML(names, pen, boxId) {
  const pens = names.map((nm, k) =>
    `<button class="pen${Number(k) === Number(pen) ? " on" : ""}" data-pen="${k}"
       style="--pc:${COLORS[k % COLORS.length]}">${esc(nm)}</button>`).join("")
    + `<button class="pen${Number(pen) === MULTI_CHORUS ? " on" : ""}"
       data-pen="${MULTI_CHORUS}" style="--pc:${CHORUS_COLOR}"
       title="点了它，之后点的每一句都是所有人一起唱">合唱（所有人）</button>`;
  return `<div class="mpens" id="${boxId}">
      <span class="sl">现在用这支笔：</span>${pens}
      <button class="btn ghost sm" data-allpen="1"
        title="把整首歌的每一句都指给当前这支笔（选了合唱笔就是整首合唱）">整首都给 TA</button>
    </div>`;
}

function manualRowsHTML(d, names) {
  // ★ 每行不再是一个 <select>：那是"改一句要点两次（展开 + 选）"，
  //   20 句就是 40 下，而且下拉一展开就盖住下面的歌词，根本没法对着词点。
  //   现在点行体就指派 —— 真正的 <select> 还在（只是藏起来），
  //   因为读值的人（sgManualOf / currentManual）靠它读顺序，
  //   隐藏而不删除 = 数据口径一条没变。
  return (d.lines || []).map((ln, i) => {
    const mm = String(Math.floor(ln.start / 60)).padStart(2, "0");
    const ss = String(Math.floor(ln.start % 60)).padStart(2, "0");
    // ★ Number(-1) 是 -1（不是 0）：`|| 0` 那套"防 undefined"的写法对 -1 也成立，
    //   但**后面取颜色/取名字必须分开处理** —— COLORS[-1 % n] 在 JS 里是
    //   COLORS[-1] = undefined，会画出 undefined 的边框、空名字。
    const k = Number(ln.role) || 0;
    const isCho = (k === MULTI_CHORUS);
    const c = isCho ? CHORUS_COLOR : COLORS[k % COLORS.length];
    const who = isCho ? "合唱" : esc(names[k] || "");
    const opts = names.map((nm, j) =>
      `<option value="${j}"${j === k ? " selected" : ""}>${esc(nm)}</option>`).join("")
      + `<option value="${MULTI_CHORUS}"${isCho ? " selected" : ""}>合唱（所有人）</option>`;
    // ★★ 中英双语**两行都显示**（老板：「我要的是中英双语」）。
    //   以前是"有中文就显示中文、没有才显示原文、原文只挂 title 悬停" ——
    //   等于永远只看见一半，对着一屏英文数不清第几行是哪句。
    //   现在：原文一行（淡、小）+ 中文一行（主色）。没中文就只显示原文。
    // ★ 不再有"×N 合了几段"的角标：2026-10-25 起**一句都不合并**
    //   （老板：「不会压缩综合」），n_sub 永远是 1，留着只会让人以为还在合并。
    const zh = (ln.zh || "").trim();
    const ori = (ln.text || "").trim();
    const body = zh
      ? `<div class="mor">${esc(ori)}</div><div class="mzh">${esc(zh)}</div>`
      : `<div class="mor only">${esc(ori || ("第 " + (i + 1) + " 段"))}</div>`;
    return `<div class="mline" data-who="${k}" style="--pc:${c}">
      <span class="mt">${mm}:${ss}</span>
      <div class="mx">${body}</div>
      <span class="mwho" style="border-color:${c};background:${c}${isCho ? "33" : "22"};color:${c}">${who}</span>
      <select class="inp msel" style="display:none">${opts}</select>
    </div>`;
  }).join("");
}

/* ---- 歌词来源条：现在用的哪一份 + 自己导入 ------------------------
   ★ 为什么要这一条：句子是从歌词来的，而歌词可能是"自动在同一目录里
     按歌名猜的" —— 猜错了（同名歌、翻唱版）整列词就是别人的。
     老板要"我可以自己导入"，那就必须让他**看得见现在用的是哪份**，
     并且一键换掉/删掉（2026-10-25）。 */
const LRC_SRC_TXT = {
  import: ["自己导入的", "自己导入的那份"],
  lrc: ["外部歌词（.lrc）", "同目录下按歌名找到的那份 .lrc"],
  asr: ["认出来的", "识别出来的句子（没有现成歌词）"],
  none: ["没有歌词", "现在没有歌词 ⇒ 按时间均分"],
  import_bad: ["导入的那份没解析出句子", "导入的那份里带时间轴的行太少"],
  lrc_bad: ["找到的歌词没解析出句子", "找到的那份里带时间轴的行太少"],
};

function lrcBarHTML(st) {
  const t = LRC_SRC_TXT[st.src] || LRC_SRC_TXT.none;
  const n = st.lines ? ` · <b>${st.lines}</b> 句` : "";
  const tail = st.has_import
    ? `<button class="btn ghost sm" data-lrc="clear">删掉我导入的</button>`
    : "";
  return `<span class="sl">歌词：</span>${t[0]}${n}`
    + `<span class="hint" style="margin-left:6px">${t[1]}</span>`
    + `<button class="btn ghost sm" data-lrc="edit">${st.has_import ? "换一份" : "自己导入"}</button>`
    + tail
    + `<div class="lrcedit" style="display:none;margin-top:8px">`
    + `<div class="hint" style="margin-bottom:4px">`
    + `粘贴**带时间轴**的歌词（<code>[00:12.34]歌词</code> 这种格式，一行一句）；`
    + `也可以直接选一个 .lrc 文件。没有 [分:秒] 的行不会被当成一句。</div>`
    + `<textarea class="inp" rows="7" placeholder="[00:00.44] Why do all the monsters come out at night?&#10;[00:03.92] Why do we sleep where we want to hide?"></textarea>`
    + `<div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">`
    + `<button class="btn sm pri" data-lrc="save">保存这份</button>`
    + `<button class="btn sm ghost" data-lrc="cancel">取消</button>`
    + `<input type="file" accept=".lrc,.txt" class="lrcfile" style="max-width:230px">`
    + `</div></div>`;
}

function wireLrcBar(box, song, after) {
  if (!box) return;
  const ed = box.querySelector(".lrcedit");
  const ta = box.querySelector("textarea");
  const file = box.querySelector(".lrcfile");
  box.querySelectorAll("[data-lrc]").forEach(b => {
    const k = b.dataset.lrc;
    if (k === "edit") {
      b.onclick = () => {
        ed.style.display = ed.style.display === "none" ? "" : "none";
        if (ed.style.display !== "none" && ta) ta.focus();
      };
    } else if (k === "cancel") {
      b.onclick = () => { ed.style.display = "none"; };
    } else if (k === "save") {
      b.onclick = async () => {
        const txt = ta ? ta.value : "";
        if (!txt.trim()) { toast("歌词是空的", "bad"); return; }
        b.disabled = true;
        try {
          const r = await post("/api/multi/lrc/save", { song, text: txt });
          if (r.error) { toast(r.error, "bad"); return; }
          toast(`已用这份歌词：${r.lines} 句`, "good");
          if (after) after();
        } catch (e) {
          toast(String((e && e.message) || e), "bad");
        } finally { b.disabled = false; }
      };
    } else if (k === "clear") {
      b.onclick = async () => {
        try {
          const r = await post("/api/multi/lrc/clear", { song });
          if (r.error) { toast(r.error, "bad"); return; }
          toast("已删掉导入的那份 ⇒ 回到自动找的", "good");
          if (after) after();
        } catch (e) { toast(String((e && e.message) || e), "bad"); }
      };
    }
  });
  // 选文件：直接把内容读进框里（不自动保存 —— 让他先看一眼是不是这份）
  if (file) {
    file.onchange = () => {
      const f = file.files && file.files[0];
      if (!f) return;
      const rd = new FileReader();
      rd.onload = () => { if (ta) ta.value = String(rd.result || ""); };
      rd.readAsText(f, "utf-8");
    };
  }
}

async function lrcBarLoad(box, song, after) {
  if (!box || !song) return;
  try {
    const st = await post("/api/multi/lrc", { song });
    if (st.error) { box.innerHTML = ""; return; }
    box.innerHTML = lrcBarHTML(st);
    wireLrcBar(box, song, after);
  } catch (e) { box.innerHTML = ""; }
}

/* ★ 只有"歌换了"或"刚导入/删掉"才重画这一条：重排分配表也会走到
   renderMultiPlan，无脑重画会把用户正在编辑的框收起来、刚敲的字没了。 */
let MULTI_LRC_SONG = "";
let SG_LRC_SONG = "";

function lrcBarRefresh(boxId, song, after) {
  const box = $(boxId);
  if (!box) return;
  const key = boxId === "#sgLrc" ? "sg" : "multi";
  const prev = key === "sg" ? SG_LRC_SONG : MULTI_LRC_SONG;
  if (!song) { box.innerHTML = ""; return; }
  if (prev === song && box.innerHTML) return;
  if (key === "sg") SG_LRC_SONG = song; else MULTI_LRC_SONG = song;
  lrcBarLoad(box, song, () => {
    if (key === "sg") SG_LRC_SONG = ""; else MULTI_LRC_SONG = "";
    after();
  });
}

function wireManualList(list, api) {
  list.querySelectorAll(".pen[data-pen]").forEach(b => {
    b.onclick = () => api.setPen(Number(b.dataset.pen));
  });
  const all = list.querySelector("[data-allpen]");
  if (all) all.onclick = () => api.all();
  list.querySelectorAll(".mline").forEach(el => {
    el.onclick = () => api.line(el);
  });
}

/* 「笔」= 接下来点歌词时，这一句归谁。连着点十几句不用反复选人。 */
let MULTI_PEN = 0;
let MULTI_NAMES = [];
/* ★ 合唱笔：值为 -1，和后端 multising.ROLE_ALL 同一个约定。
   改这个值必须同时改后端（stages/multising.py 的 ROLE_ALL），否则界面上
   看着分好了、后端把它当"第 -1 个人"处理 —— 一句合唱都不会出现。 */
const MULTI_CHORUS = -1;
const CHORUS_COLOR = "#ffd45e";

/* `root` 是一份逐句列表的容器（角色页一份、大厅一份）。
   ★ 必须在 root 里查，不能 document 里查：两份同时存在时，
     在角色页换笔会把大厅那排笔也一起点亮 —— 看着像"我没动它它自己变了"。 */
function paintPenIn(root, k) {
  (root || document).querySelectorAll(".mpens .pen").forEach(b => {
    // ★ 用 Number 比：dataset 里取出来是字符串 "-1"，不能拿 === 直接比数字
    b.classList.toggle("on", Number(b.dataset.pen) === Number(k));
  });
}

function paintPen(k, root) {
  MULTI_PEN = k;
  paintPenIn(root, k);
}

/* 把某一句指给第 k 个人。`commit` 为真时才去问服务端算时长 ——
   "整首都给 TA"要连改 20 行，逐行发请求就是 20 个并发 POST。
   ★ 名字表和"改完通知谁"都由调用方给：同一个函数大厅也要用，
     它那一份不能跟着角色页的 MULTI_NAMES 走（两边人数可以不一样）。 */
function setLineWho(el, k, commit, names, done) {
  const nm = names || MULTI_NAMES;
  const sel = el.querySelector("select.msel");
  if (!sel || !nm.length) return;
  // ★ 合唱（-1）不许被夹成 0：夹了就是把"这一句合唱"静默改成"第一个人独唱"
  const kk = (Number(k) === MULTI_CHORUS)
    ? MULTI_CHORUS : Math.max(0, Math.min(Number(k) || 0, nm.length - 1));
  sel.value = String(kk);
  el.dataset.who = String(kk);
  // 刚点过的那一句亮一下（只留一句）。只在**单句指派**时亮：
  // 「整首都给 TA」会连改十几行，全亮等于没亮，而且会让整张表看着像被选中了。
  if (commit) {
    const host = el.parentElement;
    if (host) host.querySelectorAll(".mline.on").forEach(
      e => { if (e !== el) e.classList.remove("on"); });
    el.classList.add("on");
  }
  const w = el.querySelector(".mwho");
  if (w) {
    const isCho = (kk === MULTI_CHORUS);
    const c = isCho ? CHORUS_COLOR : COLORS[kk % COLORS.length];
    w.textContent = isCho ? "合唱" : (nm[kk] || "");
    w.style.borderColor = c;
    w.style.background = c + (isCho ? "33" : "22");
    w.style.color = c;
  }
  if (commit && done) done();
}

function assignAllLines() {
  const rows = document.querySelectorAll("#multiLines .mline");
  rows.forEach(el => setLineWho(el, MULTI_PEN, false));
  onManualPick();
}

/* 从一份逐句列表容器里读出"每一句归谁"（顺序 = 行的顺序）。 */
function manualOf(root) {
  const out = [];
  const list = root || document;
  list.querySelectorAll("select.msel").forEach(s => {
    out.push(parseInt(s.value, 10) || 0);
  });
  return out;
}

/* ★★ 没认过词的歌 ⇒ 先认一次，认完自动把列表换成真句子。
   为什么必须有这个入口（它不是锦上添花）：
     逐句指定要的是"对着歌词挑哪一句"，而**真句子只来自歌词缓存**。
     预览接口只读缓存（没词就按时间切 22 段），开跑时才 auto_lyric 自动认
     ⇒ 你按 22 段点好，开跑认出 60 句，你的指定只覆盖前 22 句，后面全用
       第 22 个值 —— 后半首变成一个人独唱，还不报错（已在后端改成拒绝）。
     ⇒ 认完词两边句数才一致，你点的每一句才真的算数。
   以前这里只写一句"先认一次词"，但界面上根本没有认词的地方 —— 等于让人
   去走一条不存在的路。 */
/* `song` / `btnSel` / `after` 都可省：角色页和大厅共用同一个认词动作，
   差别只是"认完刷新哪一份列表"（两边同时开着，认完不能只刷新一边）。 */
async function onLyricFirst(song, btnSel, after) {
  const sp = song || pickedSong;
  const bsel = btnSel || "#btnLyricFirst";
  const b = $(bsel);
  if (!sp) { toast("先选一首歌", "bad"); return; }
  if (b) { b.disabled = true; b.textContent = "识别中…（一两分钟）"; }
  try {
    const r = await post("/api/jobs",
      { kind: "lyrics", song: sp, lang: "zh" });
    watchJob(r.job.id, async () => {
      let ok = false;
      try {
        const st = await api("/api/job/" + r.job.id);
        ok = ((st.job || {}).state === "done");
      } catch (e) { /* 拿不到就按没认成处理 */ }
      if (!ok) {
        const bb = $(bsel);
        if (bb) { bb.disabled = false; bb.textContent = "先认一次词（一两分钟）"; }
        toast("没认成 —— 看任务卡里的说明", "bad");
        return;
      }
      (after || loadMultiPlan)();   // 重排：列表从"按时间切的段"换成真句子
    });
  } catch (e) {
    if (b) { b.disabled = false; b.textContent = "先认一次词（一两分钟）"; }
    toast(String((e && e.message) || e), "bad");
  }
}

/* 手动改了某一句 → 只更新"谁唱多少"的汇总，不用再问服务端。
   为了不重复实现一遍分句逻辑，这里直接把新下标发给 /api/multi/plan
   让它算（它便宜：纯时间轴）。 */
function onManualPick() {
  const mode = ($("#selMultiMode") && $("#selMultiMode").value) || "phrase";
  if (mode !== "manual") return;
  const tok = ++MULTI_PLAN_TOKEN;
  post("/api/multi/plan", {
    song: pickedSong, casts: multiPicks(), mode: "manual", manual: currentManual(),
  }).then(d => {
    if (tok !== MULTI_PLAN_TOKEN || !d || d.error) return;
    // ★ 合唱秒数单独报：它不属于任何一个人的"独唱"，混进谁的秒数都会让
    //   "谁唱了多少"读不出真相（各人相加会超过曲长）。
    const per = (d.per_role || []).map(x => `${x.name}：${x.sec} 秒 / ${x.parts} 段`
      + ((x.chorus_sec ? `（另含合唱 ${x.chorus_sec} 秒）` : "")));
    $("#multiPlanSum").textContent = `全曲 ${d.duration} 秒 · ` + per.join("　");
    // 只更新汇总，**不重绘列表** —— 重绘会把用户刚点的那个下拉框弹回默认值
    MULTI_PLAN = Object.assign({}, MULTI_PLAN, { per_role: d.per_role, takes: d.takes });
  }).catch(() => {});
}

async function doSingMulti() {
  const picks = multiPicks();
  if (picks.length < 2) { toast("至少勾两个角色才叫对唱", "bad"); return; }
  if (picks.length > STAGE_MAX_CASTS) {
    toast(`最多 ${STAGE_MAX_CASTS} 个角色一起唱`, "bad"); return;
  }
  const mode = ($("#selMultiMode") && $("#selMultiMode").value) || "phrase";
  const hv = ($("#inpMultiGain") && $("#inpMultiGain").value) || "0.55";
  const body = {
    kind: "sing_multi", role: picks[0].rid, song: pickedSong, casts: picks,
    mode: mode,
    index_rate: parseFloat($("#inpIdx").value || "0.5"),
    // 与单角色同一个开关 → 同一个值。以前这里没传，选了也白选。
    hf: ($("#selHf") && $("#selHf").value) || "auto",
    demetal: ($("#selDemetal") && $("#selDemetal").value) || "auto",
    presence: ($("#swPresence") && !$("#swPresence").checked) ? "off" : "auto",
    auto_lyric: true,
    qc: true,
    // 只转自己分到的那几句（省一半以上时间）。不勾就不传，
    // 让服务端用它的默认值 —— 哪天实测过了改默认，前端不用动。
  };
  // ★ 一定传布尔值：这个开关现在**默认开**，只传 true 的话用户取消勾选就传不出去
  //   （缺省时服务端按"开"处理），会变成"界面关不掉"。
  if ($("#chkSegOnly")) body.seg_only = !!$("#chkSegOnly").checked;
  // ★ 和声音量不只在"整首叠唱"下有效：**逐句指定的合唱**同样用它
  //   （以前只给 chorus 传 ⇒ 逐句合唱那段永远用模块默认值，滑块调了没反应）。
  if (mode === "chorus" || mode === "manual") {
    body.chorus_gain = Math.max(0.1, Math.min(1, parseFloat(hv) || 0.55));
  }
  if (mode === "manual") {
    const man = currentManual();
    if (!man.length) {
      toast("「手动指定」要先把歌词认出来（列表是空的）—— 现在还没句子可分", "bad");
      return;
    }
    body.manual = man;
  }
  try {
    const j = await post("/api/jobs", body);
    watchJob(j.job.id, () => {
      loadRoles();
      if (CUR) api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); });
    });
  } catch (e) {
    // 404 = 服务是旧进程，这条接口还不存在。直接说"重启"，
    // 比丢一句"请求失败 404"强 —— 这件事老板已经踩过两次了。
    const m = String(e && e.message || e);
    toast(m.indexOf("404") >= 0
      ? "多人合唱需要重启服务才生效：当前跑的还是旧进程，双击「启动VoiceStudio.bat」"
      : m, "bad");
  }
}

async function doSing(preview) {
  if (!pickedSong) { toast("先选一首歌", "bad"); return; }
  // 多人合唱：走另一条接口（每条轨要各自推理，参数结构不一样）。
  // ★ 小样**没有做**（后端 multi 这条链路上没有 preview 参数）。
  //   原来那句理由写的是"每个角色各唱一遍整首，小样省不下时间" —— 改成
  //   每人只唱分到的那几句之后这个理由已经不成立了，留着就是一句假话。
  //   照实说：没做，直接跑一遍就好。
  if (($("#selMulti") && $("#selMulti").value) === "on") {
    if (preview) { toast("多人合唱还没有小样（没做）—— 直接跑一遍，约 2 分钟出成品", "bad"); return; }
    return doSingMulti();
  }
  const body = {
    kind: "sing", role: CUR.id, song: pickedSong,
    pitch: pickedPitch(),
    // ★ 0.5 不是随便写的：studio 里实测过 index_rate 的四档（音色 vs 八度错
    //   是反的），0.50 是"音色已经明显变好、音准还没开始掉"的交叉点。
    //   这里原来写 0.75 —— 用户不改就会用 0.75，八度错会从 9.9% 升到 11.5%，
    //   正好越过音高体检的坏档线。界面默认值必须和后端默认值同源。
    index_rate: parseFloat($("#inpIdx").value || "0.5"),
    engine: ($("#selEngine") && $("#selEngine").value) || "rvc",
    hf: ($("#selHf") && $("#selHf").value) || "auto",
    demetal: ($("#selDemetal") && $("#selDemetal").value) || "auto",
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
  } catch (e) {
    // 409 = 同一个角色 + 同一首歌已经有一个在跑（再开会两个抢同一张卡，
    // 实测慢 5 倍还会炸 CUDA）。服务端把那个任务一起回来了 —— 直接盯上它，
    // 别让用户对着一句"已经在唱了"自己去找。
    toast(e.message, "bad");
    const old = e.payload && e.payload.job;
    if (old && old.id) watchJob(old.id, () => {
      loadRoles();
      api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); });
    });
  }
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
  inp.accept = mode === "song"
    ? "audio/*,.wav,.mp3,.flac,.m4a,.aac,.ogg,.wma"
    : "video/*,audio/*,.mp4,.mkv,.wav,.mp3";
  inp.click();
}

/* 角色内选歌：从「我的音乐」曲库里挑一首。
   ★ 为什么角色内必须有这条路：浏览器文件选择器**看不到**曲库里已扫出的歌
     （那些条目只有路径），于是"我的音乐里明明能用，进角色唱歌就选不到"。
   选歌器本体在 loc.js（songPickOpen），扫描口径与「我的音乐」面板同一套。 */
function pickSongFromLib() {
  if (typeof songPickOpen !== "function") {
    toast("选歌器没加载出来，刷新一下页面", "bad");
    return;
  }
  songPickOpen((path, title) => songPickedIntoRole(path, title));
}

/* 角色内"歌已选好"之后统一走这里。
   ★ 上传文件 / 从曲库选 两条路必须共用同一段收尾 ——
     分开写迟早会出现"一条路记得把按钮点亮、另一条忘了"（选了歌还是灰的按钮）。 */
function songPickedIntoRole(path, title) {
  pickedSong = path;
  const info = $("#songInfo");
  if (info) info.textContent = "已选：" + (title || String(path).split(/[\\/]/).pop());
  // 选了歌也未必能唱：没有歌声模型、素材又不够的角色，点下去 sing_job 会直接
  // 报「只有 0 段干声」。既然状态是已知的，就别让按钮亮着骗人。
  const singOk = !(CUR && CUR.ability && CUR.ability.sing
                   && CUR.ability.sing.state === "blocked");
  if ($("#btnSing")) $("#btnSing").disabled = !singOk;
  if ($("#btnPreview")) $("#btnPreview").disabled = !singOk;
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
    songPickedIntoRole(j.path, f.name);
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

/* 「原歌词校对」：把认出来的每一句摊开给用户改错字。
   为什么必须有这一步（不是锦上添花）：
     原词（ref_text）是要送进模型**做音素化**的 —— 官方 align_lrc_sentence_level
     把 `|` 换空格、把标点归一成逗号，文本只提供"唱哪些音素"。认错一个字，
     它就照着错的那个字唱。而 whisper 在歌唱上的错字是肉眼可见的。
     靠调扩散步数救不回来，只能让用户改一句 —— 比整首重唱便宜得多。
   改完存回服务端，改词时用的就是改过的那份（studio.lyric_segments_for 直接读它）。 */
let PROOF = [];

function fmtT(s) {
  s = Math.max(0, Number(s) || 0);
  return Math.floor(s / 60) + ":" + (s % 60).toFixed(1).padStart(4, "0");
}

function renderProof() {
  const list = $("#proofList");
  if (!list) return;
  list.innerHTML = "";
  PROOF.forEach((x, i) => {
    const row = document.createElement("div");
    row.className = "proof-row";
    row.innerHTML =
      `<span class="proof-no">${i + 1}</span>`
      + `<span class="proof-tm" title="这一句在歌里的位置">${fmtT(x.start)} → ${fmtT(x.end)}</span>`
      + `<input class="inp sm proof-tx" data-i="${i}" value="${esc(x.text || "")}">`
      + `<button class="btn sm proof-del" data-del="${i}" title="删掉这一句">✕</button>`;
    list.appendChild(row);
  });
  list.querySelectorAll("input.proof-tx").forEach((el) => {
    el.addEventListener("input", () => {
      const i = parseInt(el.dataset.i, 10);
      if (PROOF[i]) PROOF[i].text = el.value;
    });
  });
  list.querySelectorAll("button.proof-del").forEach((el) => {
    el.addEventListener("click", () => {
      PROOF.splice(parseInt(el.dataset.del, 10), 1);
      renderProof();
    });
  });
}

async function loadProof() {
  const box = $("#proofBox");
  const list = $("#proofList");
  if (!box || !list || !STAGE_SONG) return;
  try {
    // 档位要跟着界面上选的那档走：medium 和 large 是两份不同的缓存，
    // 不带上它，用精准档认完再刷新页面就会读回标准档那份（看似"没变准"）。
    const asr = ($("#sgAsr") && $("#sgAsr").value) || "medium";
    const j = await api("/api/lyrics?path=" + encodeURIComponent(STAGE_SONG)
      + "&lang=zh&asr=" + encodeURIComponent(asr));
    if (!j.ok || !(j.segments || []).length) { box.style.display = "none"; return; }
    PROOF = j.segments;
    renderProof();
    box.style.display = "";
    const h = $("#proofHint");
    if (h) h.textContent = j.proofread ? "已校对过" : "";
  } catch (e) {
    // 服务是旧进程时没有这个接口 —— 悄悄收起，别拿报错吓用户
    box.style.display = "none";
  }
}

const _proofSaveBtn = $("#btnProofSave");
if (_proofSaveBtn) {
  _proofSaveBtn.addEventListener("click", async () => {
    if (!STAGE_SONG) { toast("先把歌拖进②号槽", "bad"); return; }
    if (!PROOF.length) { toast("还没有可保存的句子", "bad"); return; }
    _proofSaveBtn.disabled = true;
    try {
      const j = await post("/api/lyrics/save",
        { path: STAGE_SONG, segments: PROOF, lang: "zh",
          asr: ($("#sgAsr") && $("#sgAsr").value) || "medium" });
      PROOF = j.segments || PROOF;
      const box = $("#sgOrig");
      if (box && j.line) { box.value = j.line; stageLyricHint(); }
      const h = $("#proofHint");
      if (h) h.textContent = "已保存，改词会用这一份";
      toast("校对已保存 —— 改词会按这一份来唱", "ok");
    } catch (e) {
      toast(e.message || "保存失败", "bad");
    }
    _proofSaveBtn.disabled = false;
  });
}

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
    // 「正确歌词」框里填了东西就一起送过去：识别完按它把错字订正掉。
    // 这一步是改词质量最划算的投入 —— 歌词网上就有，认错一个字就得重唱一整首。
    const refTxt = ($("#sgRef") && $("#sgRef").value || "").trim();
    // 识别档位：标准档快，精准档（large-v3）认错字明显更少。
    // 首次用精准档要下 3.1 GB，所以在按钮上先把话说清楚，别让人以为卡死。
    const asr = ($("#sgAsr") && $("#sgAsr").value) || "medium";
    _autoLyricBtn.disabled = true;
    if (hint) {
      hint.textContent = asr === "large"
        ? (refTxt
          ? "精准档识别中（首次要下 3.1G 模型，请稍候；之后按你给的歌词订正错字）…"
          : "精准档识别中（首次要下 3.1G 模型，请稍候）…")
        : (refTxt
          ? "识别中（会按你给的歌词订正错字，一两分钟）…"
          : "识别中（分离+识别，通常一两分钟）…");
    }
    try {
      const r = await post("/api/jobs",
        { kind: "lyrics", song: STAGE_SONG, lang: "zh", ref: refTxt, asr });
      watchJob(r.job.id, async () => {
        _autoLyricBtn.disabled = false;
        let line = "";
        try {
          const st = await api("/api/job/" + r.job.id);
          line = ((st.job || {}).lyric_result || {}).line || "";
        } catch (e) { /* 拿不到就当没识别出来 */ }
        await loadProof();
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

/* ---- 拖拽时自动滚屏 ------------------------------------------------
   【为什么必须有】角色墙在页面下半部分（.grid 那一大块），而「歌手」槽在页面
   **最上面**。拖一张卡过去要跨好几屏，而 **Chrome 在原生拖拽过程中忽略滚轮** ——
   只有把指针顶到视口边缘它才自己滚，而且慢得像不动。用户报的"拖不上去，
   翻不过页面"就是这个：手往前推、页面纹丝不动，只能放弃。

   这里自己做一个：指针离上/下边缘 DRAG_EDGE 以内就滚，越靠边越快。
   ★ 另有一个隐形坑：dragend 未必会来（拖到别的程序 / 被系统取消），
     所以用「最近一次 dragover 的时间戳」兜底 —— 超过 700ms 没动静就停，
     否则会变成一个永远在偷偷滚页面的定时器。 */
let DRAG_Y = -1;          // 最近一次 dragover 的纵坐标；-1 = 没在拖
let DRAG_T = 0;           // requestAnimationFrame 句柄，0 = 没在滚
let DRAG_AT = 0;          // 最近一次 dragover 的时刻
const DRAG_EDGE = 130;    // 离边缘多少像素开始滚
const DRAG_MAX = 22;      // 每帧最多滚多少像素

function dragScrollStep() {
  DRAG_T = 0;
  if (DRAG_Y < 0) return;
  if (Date.now() - DRAG_AT > 700) { DRAG_Y = -1; return; }   // 拖拽早没了
  const h = window.innerHeight || 800;
  let dy = 0;
  if (DRAG_Y < DRAG_EDGE) {
    dy = -DRAG_MAX * (1 - Math.max(0, DRAG_Y) / DRAG_EDGE);
  } else if (DRAG_Y > h - DRAG_EDGE) {
    dy = DRAG_MAX * (1 - (h - DRAG_Y) / DRAG_EDGE);
  }
  if (dy) {
    // 保底 4px：正好贴着边时也要滚得动，否则看起来还是"卡住"
    window.scrollBy(0, dy > 0 ? Math.max(4, dy) : Math.min(-4, dy));
  }
  DRAG_T = requestAnimationFrame(dragScrollStep);
}

function dragScrollStart() {
  if (!DRAG_T) DRAG_T = requestAnimationFrame(dragScrollStep);
}

function dragScrollStop() {
  DRAG_Y = -1;
  if (DRAG_T) { cancelAnimationFrame(DRAG_T); DRAG_T = 0; }
}

// 捕获阶段记坐标：任何 dragover 都算（包括落在槽位上被 stopPropagation 的那些）
window.addEventListener("dragover", (e) => {
  if (typeof e.clientY === "number") { DRAG_Y = e.clientY; DRAG_AT = Date.now(); }
  dragScrollStart();
}, true);

// 角色卡可以拖进歌手槽：拖动时把角色 id 放进 dataTransfer
document.addEventListener("dragstart", (e) => {
  const card = closestEl(e.target, '.card[data-role]');
  if (!card) return;
  e.dataTransfer.setData("text/role-id", card.dataset.role);
  e.dataTransfer.setData("text/plain", card.dataset.role);
  e.dataTransfer.effectAllowed = "copy";
  card.classList.add("dragging");
  DRAG_Y = typeof e.clientY === "number" ? e.clientY : -1;
  DRAG_AT = Date.now();
  dragScrollStart();
});
document.addEventListener("dragend", (e) => {
  const card = closestEl(e.target, '.card[data-role]');
  if (card) card.classList.remove("dragging");
  dragScrollStop();
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

const STAGE_MAX_CASTS = 8;   // 必须与后端 studio.MULTI_MAX_ROLES 一致（门禁会比对）
/* 大厅的唱法档位。值必须与角色内 #selMultiMode 的 option 值同集合
   （门禁 verify_hall_multi 有一条盯着这件事）—— 前端两份列表漂了，
   就会出现"大厅能选、后端不认"或者反过来。 */
const STAGE_MODES = [
  { v: "rotate", t: "逐句轮流 —— 一人一句" },
  { v: "phrase", t: "按乐句轮流 —— 一组句子一个人" },
  { v: "section", t: "按段落轮流 —— 主歌一人、副歌一人" },
  // ★ 缺了它，大厅（主路径）就只能"轮流"，用户想指定每句谁唱得绕到角色页。
  //   它下面那块 #sgPlan 面板就是它的编辑器 —— 只给选项不给面板 = 死按钮，
  //   所以门禁要同时盯着这两样（见 verify_hall_multi）。
  { v: "manual", t: "手动指定 —— 你自己安排每一句" },
  { v: "chorus", t: "叠唱 —— 同时唱，第一个是主唱" },
];

/* STAGE_ROLE 永远是名单里的第一个人 —— 老代码（成品区、历史 take、
   设为当前成品）全都按"一个主角色"写的，不让它们跟着数组改，才不会牵连一片。 */
function stageSyncLead() {
  const first = STAGE_CASTS[0];
  STAGE_ROLE = first ? (ROLES.find(x => x.id === first.rid) || null) : null;
}

function stageSetRole(rid) {
  const r = ROLES.find(x => x.id === rid);
  if (!r) return;
  if (STAGE_CASTS.some(c => c.rid === rid)) {
    // 再拖同一个人**不是**错误操作（用户只是想确认它在不在），说清楚就行
    toast(`「${r.name}」已经在名单里了`, "bad");
    stagePaintRoleSlot();
    return;
  }
  if (STAGE_CASTS.length >= STAGE_MAX_CASTS) {
    toast(`大厅最多 ${STAGE_MAX_CASTS} 个人一起唱 —— 每个人都要把整首唱一遍，再多推理时间就是 N 倍地涨`, "bad");
    return;
  }
  STAGE_CASTS.push({ rid: rid, name: r.name || rid });
  stageSyncLead();
  stagePaintRoleSlot();
  stageRefreshBtn();
  // 加了一个人 ⇒ 多一支笔、每句又多一个可选项，要重排
  stageLoadPlan();
  // 第一个人没歌声模型时，"已经训到第几轮"决定这次会不会被拦下，补问一次。
  // 多人时每个人都要有模型，缺的人直接在槽里标出来（不在这里逐个去问）。
  if (STAGE_CASTS.length === 1 && !canSing(r)) {
    rvcTrainedOf(rid).then(() => {
      if (STAGE_ROLE && STAGE_ROLE.id === rid) stagePaintRoleSlot();
    });
  }
}

function stageRemoveRole(rid) {
  const at = STAGE_CASTS.findIndex(c => c.rid === rid);
  if (at < 0) return;
  STAGE_CASTS.splice(at, 1);
  stageSyncLead();
  stagePaintRoleSlot();
  stageRefreshBtn();
  // 减了一个人 ⇒ 他分到的那些句还挂在他身上（下标已经越界），作废后重排
  sgDropPlan();
  stageLoadPlan();
}

/* 多人时切唱法：只换显示，**不重画整个槽** —— 重画会把下拉的焦点和
   用户刚填的和声量一起弄丢（重画后读到的是旧状态，等于"选了没生效"）。 */
function stageSetMode(v) {
  STAGE_MODE = v || "phrase";
  const g = $("#sgGainWrap");
  // ★ 手动指定也要和声量：把几句标成「合唱」之后，垫底的人多大声就是它。
  //   只给叠唱显示 ⇒ 逐句合唱那段永远用模块默认值，滑块调了没反应。
  if (g) g.style.display = (STAGE_MODE === "chorus" || STAGE_MODE === "manual")
    ? "" : "none";
  stageRefreshBtn();
  // 切到/切离「手动指定」都要重排：面板该出现或该收起来
  stageLoadPlan();
}

function stageSetGain(v) {
  const n = parseFloat(v);
  STAGE_GAIN = Number.isFinite(n) ? Math.max(0.1, Math.min(1, n)) : 0.55;
  stageRefreshBtn();
}

/* ================= 大厅的逐句指定 =================
   ★ 为什么大厅必须有它（这不是锦上添花，是主路径缺了一块）：
     大厅是「拖人 + 拖歌 → 一键合成」的地方，但它的唱法下拉里一直是
     轮流 / 乐句 / 段落 / 叠唱 —— 没有「手动指定」。上一轮门禁还专门写了
     「大厅没有逐句编辑器，给了就等于给个死按钮」，于是这个缺口当成设计
     保留下来了。结果就是：想指定每句谁唱，只能绕到下面的角色页
     （点开角色 → 开多人 → 选歌，三步），老板到第三轮还是「不能决定这一首歌
     哪个角色唱哪一句」。
     ⇒ 这一轮把编辑器补进大厅，那条门禁反过来要求它必须在。
   ★ 行/笔的 HTML 与角色页**共用同一份**（manualPensHTML / manualRowsHTML），
     但状态各自一份（SG_* 不碰 MULTI_*）：两边人数、当前笔都可能不同，
     共用状态会出现"在角色页换笔，大厅那排笔也跟着亮"。 */
let SG_PLAN = null;
let SG_PEN = 0;
let SG_NAMES = [];
let SG_PLAN_TOKEN = 0;

function sgCanManual() {
  return STAGE_CASTS.length >= 2 && !!(STAGE_ROLE && STAGE_SONG)
    && STAGE_MODE === "manual";
}

function sgManual() {
  return manualOf($("#sgLines"));
}

/* 换歌 / 加人 / 减人 / 换唱法之后都要重排：
   句数和人数都变了，手上那一份指定就是废的。 */
/* 手上的逐句安排作废的两种时刻：
   换了歌（句数变了）、去掉一个人（下标全错位）。
   ★ 切唱法 / 加人**不能**作废：切到叠唱再切回来，他点的那些句还在 ——
     切一次唱法就把整首的安排清空，谁还敢用。 */
function sgDropPlan() {
  SG_PLAN = null;
}

async function stageLoadPlan(force) {
  const box = $("#sgPlan");
  if (!box) return;
  if (!sgCanManual()) { box.style.display = "none"; return; }
  box.style.display = "";
  const tok = ++SG_PLAN_TOKEN;
  const hint = $("#sgPlanHint");
  if (hint && force) hint.textContent = "正在分句…";
  try {
    const d = await post("/api/multi/plan", {
      song: STAGE_SONG,
      casts: STAGE_CASTS.map(c => ({ rid: c.rid })),
      mode: "manual",
      // ★ 带上现有指定：重排不是"推倒重来"，人数变了也要留住已经点好的那些句
      manual: (SG_PLAN && SG_PLAN.can_manual) ? sgManual() : null,
    });
    if (tok !== SG_PLAN_TOKEN) return;      // 后发的请求赢，别被旧响应覆盖
    if (d.error) { if (hint) hint.textContent = d.error; return; }
    SG_PLAN = d;
    stageRenderPlan(d);
  } catch (e) {
    if (tok !== SG_PLAN_TOKEN) return;
    if (hint) hint.textContent = String((e && e.message) || e);
  }
}

function stageRenderPlan(d) {
  const sum = $("#sgPlanSum"), hint = $("#sgPlanHint"), list = $("#sgLines");
  if (!list) return;
  SG_NAMES = d.names || [];
  // 人数变了（减掉一个人）时笔的下标可能越界 —— 别留一支点不到的笔
  SG_PEN = Math.min(SG_PEN, Math.max(0, SG_NAMES.length - 1));
  const per = (d.per_role || []).map(x => `${x.name}：${x.sec} 秒 / ${x.parts} 段`
    + ((x.chorus_sec ? `（另含合唱 ${x.chorus_sec} 秒）` : "")));
  if (sum) sum.textContent = `全曲 ${d.duration} 秒 · ` + per.join("　");
  // 同上：角色页也要能看见/换掉歌词（两处共用同一个组件）
  lrcBarRefresh("#sgLrc", STAGE_SONG, () => stageLoadPlan(true));
  if (hint) {
    if (!d.lyric_ready) {
      hint.innerHTML = `这首歌还没认过词 ⇒ 现在是按时间切的 <b>${d.lines.length}</b> 段`
        + `（切点会落在句子中间，不是真的句）。`
        + `<button class="btn sm" id="btnSgLyricFirst" style="margin:0 6px">`
        + `先认一次词（一两分钟）</button>`
        + `<br>认完下面会换成<b>真句子</b>，就能对着歌词挑「哪一句谁唱」了。`
        + `<b>必须认</b>：不认的话开跑时会自动认一次，句子数一变，`
        + `你现在点的这份指定就对不上了。`;
      const bl = hint.querySelector("#btnSgLyricFirst");
      if (bl) bl.onclick = () => onLyricFirst(
        STAGE_SONG, "#btnSgLyricFirst", () => stageLoadPlan(true));
    } else {
      hint.innerHTML = `认到 <b>${d.lines.length}</b> 句 —— 先点上面<b>一支笔</b>，`
        + `再连着点下面的歌词；想让<b>某一句变成合唱</b>：点最右边那支笔再点那几句。`;
    }
  }
  list.innerHTML = manualPensHTML(SG_NAMES, SG_PEN, "sgPens")
    + `<div class="mlines">${manualRowsHTML(d, SG_NAMES)}</div>`;
  wireManualList(list, {
    setPen: k => { SG_PEN = k; paintPenIn(list, k); },
    all: () => stageAssignAll(),
    line: el => setLineWho(el, SG_PEN, true, SG_NAMES, () => sgManualPick()),
  });
}

function stageAssignAll() {
  const list = $("#sgLines");
  if (!list) return;
  list.querySelectorAll(".mline").forEach(
    el => setLineWho(el, SG_PEN, false, SG_NAMES));
  sgManualPick();
}

/* 改了一句 → 只更新"谁唱多少"的汇总。
   ★ 不重绘列表：重绘会把用户刚点下去的那一行弹回默认值。 */
function sgManualPick() {
  const tok = ++SG_PLAN_TOKEN;
  post("/api/multi/plan", {
    song: STAGE_SONG, casts: STAGE_CASTS.map(c => ({ rid: c.rid })),
    mode: "manual", manual: sgManual(),
  }).then(d => {
    if (tok !== SG_PLAN_TOKEN || !d || d.error) return;
    const per = (d.per_role || []).map(x => `${x.name}：${x.sec} 秒 / ${x.parts} 段`
      + ((x.chorus_sec ? `（另含合唱 ${x.chorus_sec} 秒）` : "")));
    const sum = $("#sgPlanSum");
    if (sum) sum.textContent = `全曲 ${d.duration} 秒 · ` + per.join("　");
    SG_PLAN = Object.assign({}, SG_PLAN, { per_role: d.per_role, takes: d.takes });
  }).catch(() => {});
}

/* 画歌手槽里那段说明。轮数口径与 doRvc/后端一致：RVC 的 N 是"训到第 N 轮"，
   所以已经训到 300 之后再选 300，是"没有更多可训的轮数"，要说清楚而不是等报错。 */
function stagePaintRoleSlot() {
  const slot = $("#slotSinger");
  if (!slot) return;
  stageSyncLead();
  const r = STAGE_ROLE;
  if (!r) {
    slot.classList.remove("has");
    slot.innerHTML = `<div class="slot-ph" id="singerPh">① 角色<br>
      <small>把下面角色卡拖进来（拖多个 = 对唱，最多 8 个）</small></div>`;
    return;
  }
  slot.classList.add("has");

  if (STAGE_CASTS.length === 1) {
    const color = r.color || COLORS[0];
    const initial = (r.name || "?").trim().slice(0, 1).toUpperCase();
    const rounds = sgRounds();
    const n = RVC_TRAINED[r.id];
    let meta;
    if (canSing(r)) {
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
    // 歌手槽上那颗头像：有图就用图（一眼认出是谁），没图还是首字母 + 渐变
    const av = r.avatar
      ? `<span class="av hasimg"><img src="${media(r.avatar)}" alt=""></span>`
      : `<span class="av" style="background:linear-gradient(135deg,${color},${shade(color)})">${esc(initial)}</span>`;
    slot.innerHTML =
      `<div class="slot-ph">
         <div class="who">${av}
         ${esc(r.name)}</div>
          <div class="meta">${meta}</div>
          <div class="meta">${r.stats ? (r.stats.segments || 0) + " 段素材" : ""}</div>
          <div class="meta cast-tip">再拖一张角色卡进来 = 对唱 / 叠唱</div>
       </div>`;
    const eng = $("#sgEngine");
    if (eng) {
      eng.disabled = false; eng.title = "";
      // 只有 SVC 权重的卡 ⇒ 默认 svc，否则按钮会说「会先训歌声模型」
      eng.value = (!(r.rvc && r.rvc.model) && r.svc && r.svc.model) ? "svc" : "rvc";
    }
    return;
  }

  // ---- 多人 ----
  const missing = STAGE_CASTS.filter(c => {
    const x = ROLES.find(y => y.id === c.rid);
    return !(x && canSing(x));
  });
  const chips = STAGE_CASTS.map((c, i) => {
    const x = ROLES.find(y => y.id === c.rid) || {};
    const col = x.color || COLORS[i % COLORS.length];
    const lack = !canSing(x);
    return `<span class="cast${lack ? " lack" : ""}" title="${esc(x.name || c.rid)}${lack ? " —— 还没歌声模型，唱不了" : ""}">
      <i class="dot" style="background:${col}"></i>${esc(x.name || c.name)}
      ${i === 0 ? '<b class="lead">主</b>' : ""}
      ${lack ? '<b class="warn">缺模型</b>' : ""}
      <b class="x" data-castdel="${esc(c.rid)}" title="从名单里去掉">✕</b></span>`;
  }).join("");
  const modeOpts = STAGE_MODES.map(m =>
    `<option value="${m.v}"${m.v === STAGE_MODE ? " selected" : ""}>${esc(m.t)}</option>`).join("");
  slot.innerHTML =
    `<div class="slot-ph">
       <div class="who">🎭 ${STAGE_CASTS.length} 人合唱</div>
       <div class="castbox">${chips}</div>
       <div class="cast-ctl">
         <label>唱法
           <select class="inp xs" id="sgMultiMode" onchange="stageSetMode(this.value)">${modeOpts}</select>
         </label>
         <label id="sgGainWrap" style="${(STAGE_MODE === "chorus" || STAGE_MODE === "manual") ? "" : "display:none"}">和声
           <input class="inp xs" id="sgMultiGain" type="number" min="0.1" max="1" step="0.05"
                  value="${STAGE_GAIN}" onchange="stageSetGain(this.value)"
                  title="叠唱时其余角色垫在底下的音量">
         </label>
       </div>
       <div class="meta">${missing.length
          ? `<b class="warn">${missing.map(m => esc(m.name)).join("、")} 还没有歌声模型</b> —— 多角色里每个都要有歌声模型（RVC 或 SVC 权重）`
          : `每人都会把<b>整首</b>唱一遍，时间约是单人的 ${STAGE_CASTS.length} 倍`}
         · 变调 ${$("#sgPitch") ? $("#sgPitch").value : 0} 对所有人生效</div>
     </div>`;
  slot.querySelectorAll("[data-castdel]").forEach(el => {
    el.onclick = (e) => { e.stopPropagation(); stageRemoveRole(el.dataset.castdel); };
  });
  // 多人只走 RVC（sing_multi 的链路就是 RVC）—— 选了不理是最招人恨的静默失效，
  // 这里显式置灰并写清原因（与角色内 #selEngine 同一口径）。
  const eng = $("#sgEngine");
  if (eng) {
    eng.disabled = true;
    eng.title = "多人时每个角色按自己卡上的引擎唱（RVC / SVC 自动），这里不用选";
  }
}

function stageSetSong(path, name) {
  STAGE_SONG = path;
  const slot = $("#slotSong");
  slot.classList.add("has");
  slot.innerHTML = `<div class="slot-ph"><div class="who">🎵 ${esc(name || path.split(/[\\/]/).pop())}</div>
    <div class="meta">已就位，点「一键合成」出成品</div></div>`;
  stageRefreshBtn();
  loadProof();
  // 换歌 = 换了一首歌，手上的逐句指定是废的 ⇒ 作废后立刻按新歌重排
  sgDropPlan();
  stageLoadPlan();
}

function stageRefreshBtn() {
  const n = STAGE_CASTS.length;
  const ok = !!(STAGE_ROLE && STAGE_SONG);
  const btn = $("#btnOneClick");
  if (!btn) return;
  btn.disabled = !ok;
  if (n >= 2) {
    const kind = STAGE_MODE === "chorus" ? "叠唱" : "对唱";
    btn.textContent = `⚡ 合成（${n} 人${kind}）`;
    return;
  }
  btn.textContent = STAGE_ROLE && canSing(STAGE_ROLE)
    ? "⚡ 一键合成" : (STAGE_ROLE ? "⚡ 一键合成（会先训歌声模型）" : "⚡ 一键合成");
}

async function stagePickSong(f) {
  if (!f) return;
  toast("上传歌曲…");
  const r = await fetch("/api/upload?name=" + encodeURIComponent(f.name), { method: "POST", body: f });
  const j = await r.json();
  if (!r.ok) { toast(j.error || "上传失败", "bad"); return; }
  stageSetSong(j.path, j.from_video ? (f.name + " 的音轨") : f.name);
  // 这首歌以前校对过的话，切进来就该直接摊开给他看（不用重新识别一遍）
  loadProof();
  // 视频要抽音轨，可能要几十秒 —— 不明说的话用户会以为卡住了
  toast(j.note ? j.note + "，歌已就位" : "歌已就位", "good");
}

async function stageOneClick() {
  if (!STAGE_ROLE || !STAGE_SONG) return;
  // 名单里有两个以上：走多角色链路（各唱整首 → 按分配表拼接 → 混音）
  if (STAGE_CASTS.length >= 2) return stageMultiClick();
  const needTrain = !canSing(STAGE_ROLE);
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
      // 「分离方式」：空串 = 标准分离（自动挑模型）；"karaoke" = 伴唱优先
      // （垫唱/和声留在伴奏轨，只把主唱交给人声轨 —— 见 studio.separate_song）。
      sep_model: ($("#sgSep") && $("#sgSep").value) || "",
      // ★ 这两个选择框以前在这个入口**根本没传**（只有唱歌台那条路传）——
      //   在一键唱里把它们调成"关"，实际照样补、照样修，属于"设了没生效"。
      hf: ($("#selHf") && $("#selHf").value) || "auto",
      demetal: ($("#selDemetal") && $("#selDemetal").value) || "auto",
      presence: ($("#swPresence") && !$("#swPresence").checked) ? "off" : "auto",
      // ★ 大厅那个「唱前清理」下拉 —— 这里必须传，否则它就成了个摆设
      //   （下拉能选、任务照样跑，只是每次都按默认档）。
      pre_mode: ($("#sgPreMode") && $("#sgPreMode").value) || "guard",
    };
    // 「功能开关」：传上去的是**关掉的那几步**；控件没渲染出来就不传（用默认）
    const _pre = swSkip("pre");
    if (_pre) body.pre_skip = _pre;
    const _post = swSkip("post");
    if (_post) body.post_skip = _post;
    if (_edit) {
      body.orig_lyric = _ol; body.new_lyric = _nl;
      // 采样步数交给用户选：步数越少越容易唱飘，默认 32（官方默认）
      body.lyric_nfe = ($("#sgNfe") && $("#sgNfe").value) || "32";
    }
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

/* ---- 大厅的多角色分支 ----
 * 和角色内的多人合唱走**同一条**后端链路（studio.sing_multi），
 * 这里只把大厅的三个参数（名单 / 唱法 / 和声量）翻译过去 —— 不另做一套分配逻辑，
 * 否则同一条歌在大厅和角色内会分出两种结果（"为什么大厅唱的和我预览的不一样"）。
 * 不做的事：不做分配表预览（大厅是"一键"，预览在角色内那块面板里）。
 * 必须做的事：开跑前把"谁唱不了"当面说清 —— 多角色里每个人都要先有 RVC 模型，
 * 少一个整条就跑不起来，等后端 Fail 就是白等一轮。
 */
async function stageMultiClick() {
  const pitch = parseInt($("#sgPitch").value, 10) || 0;
  const casts = [];
  const lack = [];
  for (const c of STAGE_CASTS) {
    const r = ROLES.find(x => x.id === c.rid);
    if (!r) { toast(`「${c.name}」已经不在角色库里了，刷新一下页面`, "bad"); return; }
    if (!canSing(r)) lack.push(r.name || r.id);
    casts.push({ rid: r.id, name: r.name, pitch: pitch });
  }
  if (lack.length) {
    toast(`「${lack.join("、")}」还没有歌声模型 —— 多角色里每个人都要有歌声模型（RVC 或 SVC 权重）`
          + `（点开角色卡 →「唱歌」训一次），否则这条跑不起来`, "bad");
    return;
  }
  const mode = STAGE_MODE || "phrase";
  const modeTxt = (STAGE_MODES.find(m => m.v === mode) || {}).t || mode;
  if (mode === "chorus" && !(STAGE_GAIN >= 0.1 && STAGE_GAIN <= 1)) {
    toast("和声音量要在 0.1~1 之间", "bad");
    return;
  }
  // ★ 手动指定：没有句子就别发 —— 空数组会被后端当成"没指定"，
  //   跑出来是默认分配，用户以为自己安排过了。
  if (mode === "manual") {
    const man = sgManual();
    if (!man.length) {
      toast("「手动指定」下面还没有句子 —— 先认一次词（列表是空的）", "bad");
      return;
    }
    if (!SG_PLAN || !SG_PLAN.lyric_ready) {
      toast("这首歌还没认过词 ⇒ 现在是按时间切的段，不是真的句子。"
            + "先点「先认一次词」，认完再安排", "bad");
      return;
    }
  }
  const detail = (mode === "manual" && SG_PLAN)
    ? "\n" + (SG_PLAN.per_role || []).map(
        x => `　${x.name}：${x.sec} 秒 / ${x.parts} 段`).join("\n")
    : "";
  if (!confirm(`让 ${casts.length} 个人（${casts.map(c => c.name).join(" / ")}）唱这首？\n\n`
    + `唱法：${modeTxt}\n变调：${pitch > 0 ? "+" : ""}${pitch}（对所有人）`
    + ((mode === "chorus" || mode === "manual") ? `\n和声音量：${STAGE_GAIN}` : "")
    + detail
    + `\n\n每人只唱分到自己那几句，所以总时间不会按人数翻倍。`
    + (mode === "manual" ? `\n（按上面这份逐句安排跑；要改就回面板再点）` : ""))) return;
  if (busyGuard($("#btnOneClick"))) return;
  const btn = $("#btnOneClick");
  const oldTxt = btn.textContent;
  btn.textContent = `⚡ ${casts.length} 人各唱一遍…`;
  try {
    const body = {
      kind: "sing_multi", song: STAGE_SONG, casts: casts, mode: mode,
      qc: true, auto_lyric: true, hf: "auto",
    };
    // 和声音量不只是叠唱在用：逐句指定的「合唱」那几句也是它。
    if (mode === "chorus" || mode === "manual") body.chorus_gain = STAGE_GAIN;
    if (mode === "manual") body.manual = sgManual();
    const j = await post("/api/jobs", body);
    watchJob(j.job.id, () => {
      btn.disabled = false;
      btn.textContent = oldTxt;
      stageShowResult(STAGE_CASTS.length ? STAGE_CASTS[0].rid : STAGE_ROLE.id);
      loadRoles();
    });
  } catch (e) {
    toast(e.message, "bad");
    btn.disabled = false;
    btn.textContent = oldTxt;
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

/* 多角色成品的专属信息条：逐人取样体检 + 「换法重拼」。
   ★ 为什么单列一条而不是塞进 qcBadge：qcBadge 的口径是"一个人唱整首"，
     多人版是**每人一段**取样，两者不能混着显示（混了就等于给假数）。
   ★ 为什么给「换法重拼」：换分配方式只是换一张表，重跑等于再花 N 倍时间。
     它调的是 multi_resplit —— 只重拼重混，一次推理都不跑。 */
function multiCard(o, realIdx) {
  const p = o.params || {};
  if (!p.multi_mode) return "";
  const rows = (p.multi_qc || []).map(it => {
    const nm = esc(it.name || "?");
    if (it.match === null || it.match === undefined) {
      return `<div class="mqr">${nm}：${esc(it.note || "没量出来")}</div>`;
    }
    const pct = Math.round((it.match || 0) * 100);
    const octv = Math.round((it.octave || 0) * 100);
    const sh = (it.shift === null || it.shift === undefined) ? ""
      : ` · 实变 ${it.shift > 0 ? "+" : ""}${Math.round(it.shift)}`;
    const mismatch = (it.shift !== null && it.shift !== undefined
      && Math.abs(Number(it.shift) - Number(it.pitch || 0)) > 1);
    const tip = `取样 ${Math.round(it.from || 0)}~${Math.round((it.from || 0) + (it.secs || 0))} 秒`
      + (mismatch ? `；设定 ${it.pitch} 半音但量到 ${Math.round(it.shift)} —— 音高设置可能没生效` : "");
    return `<div class="mqr${mismatch ? " bad" : ""}" title="${esc(tip)}">`
      + `${nm}：吻合 ${pct}% · 八度 ${octv}%${sh}${mismatch ? " ⚠" : ""}</div>`;
  }).join("");
  if (!rows) return "";
  // 找不到真实下标就不给重拼按钮（宁可不给，也别按 last 去猜一条来重拼）
  const canRs = (realIdx !== undefined && realIdx !== null && realIdx >= 0);
  const modes = [["rotate", "逐句轮流"], ["phrase", "按乐句"], ["section", "按段落"]]
    .map(([v, t]) => `<option value="${v}"${v === p.multi_mode ? " selected" : ""}>${t}</option>`)
    .join("");
  return `<div class="mcard">
    <div class="mct">逐人取样体检（每人只比它自己唱的那段）</div>
    ${rows}
    ${canRs ? `<div class="row mcr">
      <select class="inp rsmode" style="width:104px;padding:2px 6px;font-size:12px">${modes}</select>
      <button class="btn sm" data-rs="${realIdx}" onclick="doResplit(this)">换法重拼</button>
      <span class="hint">不重跑推理，几秒就好</span>
    </div>` : ""}
  </div>`;
}

async function doResplit(btn) {
  const idx = Number(btn.dataset.rs);
  const box = btn.closest(".mcr");
  const sel = box && box.querySelector("select.rsmode");
  const mode = sel ? sel.value : "phrase";
  if (!CUR) return;
  btn.disabled = true;
  try {
    const j = await post("/api/jobs", {
      kind: "multi_resplit", role: CUR.id, index: idx, mode: mode,
    });
    toast("正在按新分配方式重拼（不重跑推理）", "good");
    watchJob(j.job.id, () => {
      if (CUR) api("/api/role/" + CUR.id).then(x => { CUR = x.role; paintRole(); });
    });
  } catch (e) {
    const m = String(e && e.message || e);
    toast(m.indexOf("404") >= 0
      ? "换法重拼需要重启服务（当前跑的是旧进程）" : m, "bad");
  } finally {
    btn.disabled = false;
  }
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
      const realIdx = (window.__stageOutputs || []).indexOf(o);
      const vd = isMix && m
        ? takeVerdict(m, peers, rank.best && m.score >= rank.best.m.score - 1e-9,
                       rank.best && rank.best.m)
        : "";
      return `<div class="outcard">
        <div class="tt">${esc(o.track || "成品")} · ${o.seconds || "?"} 秒</div>
        <div class="nm">${esc(name)}</div>
        ${qcBadge(o)}
        ${isMix ? multiCard(o, realIdx) : ""}
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

/* 大厅①号槽点开 = 演唱名单（可多选）。
   为什么不用已有的 openList：它**点一下就关**，选第 2 个人要重新点开槽一次 ——
   这正是"大厅里凑不出两个人"的那道墙。这里点一下加入、再点一下去掉，菜单不关。
   第 1 个人是主唱（成品登记在这个角色名下，历史 take / 设为当前成品都跟着它）。 */
function openCastList(anchor) {
  const old = $(".poplist");
  if (old) old.remove();
  if (!ROLES.length) { toast("还没有角色，先导入一个视频", "bad"); return; }
  const pop = document.createElement("div");
  pop.className = "poplist castlist";
  const head = '<div class="opt-h">点一下加入 · 再点一下去掉（第一个是主唱）</div>';
  // ★ 只在第一次铺 DOM，之后**只改状态**（勾选符号 + class）。
  //   整块 innerHTML 重画会把刚被点的节点从文档里摘掉 —— 紧随其后的
  //   "点到外面就关" 监听就会把「已经不在文档里」当成「点到外面」，
  //   菜单当场消失，选第二个人要重新点开槽一次（实测就是这里卡住）。
  const build = () => {
    if (pop.querySelector(".opt[data-rid]")) return;
    pop.innerHTML = head + ROLES.map(r => {
      const ok = canSing(r);
      return `<div class="opt" data-rid="${esc(r.id)}">
        <b>＋</b>${esc(r.name)}
        ${ok ? "" : '<i class="warn">无歌声模型</i>'}</div>`;
    }).join("");
  };
  const paint = () => {
    build();
    pop.querySelectorAll(".opt[data-rid]").forEach(el => {
      const on = STAGE_CASTS.some(c => c.rid === el.dataset.rid);
      el.classList.toggle("on", on);
      const b = el.querySelector("b");
      if (b) b.textContent = on ? "✓" : "＋";
    });
  };
  paint();
  const r = anchor.getBoundingClientRect();
  pop.style.cssText = `position:fixed;left:${r.left}px;top:${r.bottom + 6}px;z-index:70;` +
    `background:var(--card2);border:1px solid var(--line2);border-radius:10px;padding:6px;` +
    `min-width:210px;box-shadow:var(--sh);max-height:300px;overflow:auto`;
  document.body.appendChild(pop);
  pop.addEventListener("click", (e) => {
    const it = closestEl(e.target, "[data-rid]");
    if (!it) return;
    const rid = it.dataset.rid;
    if (STAGE_CASTS.some(c => c.rid === rid)) stageRemoveRole(rid);
    else stageSetRole(rid);
    paint();                     // 菜单**不关**，接着选下一个
  });
  setTimeout(() => document.addEventListener("click", function h(ev) {
    // 节点已经不在文档里（被重画换掉）⇒ 这不是"点了外面"，别关。
    // 合成事件的 target 可能是 window/document（nodeType 不是 1），contains 会抛。
    if (!(ev.target && ev.target.nodeType === 1)) return;
    if (!document.contains(ev.target)) return;
    if (!pop.contains(ev.target) && ev.target !== anchor) {
      pop.remove(); document.removeEventListener("click", h);
    }
  }), 0);
}

/* 槽位交互：歌手槽点击 = 打开演唱名单（可多选）；歌曲槽点击 = 选文件 */
function initStage() {
  const singer = $("#slotSinger"), song = $("#slotSong");
  const HINT = {
    singer: { role: "松开 → 加入演唱名单（拖多个 = 对唱）", file: "这里要放角色卡，不是文件" },
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
    dragScrollStop();
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

  singer.onclick = () => openCastList(singer);
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
// ★ 这层罩子是 body.dropping 上的全屏 ::after（72% 黑底 + 虚线框 + 中间一行提示字）。
//   只要 class 没被清掉，整页就一直发暗、那行字擦不掉，看起来像"UI 坏了"。
//   所以：**任何一条出口都必须先清罩子**，不许在清之前 return。
const clearDropHint = () => document.body.classList.remove("dropping");
window.addEventListener("dragover", (e) => {
  if (e.dataTransfer && Array.from(e.dataTransfer.types || []).includes("Files")) {
    e.preventDefault();
    document.body.classList.add("dropping");
  }
});
// 离开窗口才清：判据用 relatedTarget 是否为 null。
// 原来判 clientX<=0||clientY<=0 —— 从右边/上下边拖出去时坐标仍是正数，清不掉。
window.addEventListener("dragleave", (e) => {
  if (!e.relatedTarget) clearDropHint();
});
// 拖拽被系统取消（Esc / 拖到别的程序 / 拖回桌面）时浏览器只发 dragend
window.addEventListener("dragend", clearDropHint);
window.addEventListener("keydown", (e) => { if (e.key === "Escape") clearDropHint(); });
document.addEventListener("visibilitychange", () => { if (document.hidden) clearDropHint(); });
window.addEventListener("drop", async (e) => {
  // ★ 先清再分支：往槽位里放文件时以前是 `if (inSlot) return;` 直接走人，
  //   罩子就永久卡住（实测必现，用户拖一次音频进翻唱台就中招）。
  clearDropHint();
  dragScrollStop();
  const inSlot = closestEl(e.target, ".slot, .drop");
  if (inSlot) return;                      // 槽位/导入框自己会处理
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (!f) return;
  e.preventDefault();
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
 * 后果是真实的：我的音乐入口其实一直排在候选目录第一位、扫描也正常，
 * 老板的反馈却是「不能看见我的音乐里的歌」—— **不是功能坏了，是入口看不见**。
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
  { id: "secFine", e: "🔧", t: "精修台" },
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

/* ============ 服务新鲜度自检 ============
   问题（真事，不是假设）：改了代码但没重启服务，界面上的新功能就是不出现，
   看起来像"这个功能没做"。门禁那边有 svc_probe 挡住，但**用户看不到门禁**。
   所以这里直接问 /api/health：进程启动时间 比 磁盘上代码的最新修改时间 还早
   ⇒ 就是旧进程，明明白白告诉他重启。 */
async function checkServiceFresh() {
  let h;
  try {
    const r = await fetch("/api/health", { cache: "no-store" });
    if (!r.ok) return;                    // 没有这个接口 = 更老的服务，下面也不用提示了
    h = await r.json();
  } catch (e) { return; }
  if (!h || !h.started_ts || !h.code_mtime) return;
  // ★ 只在**真有没生效的改动**时才弹横幅。
  //   服务返回的 stale_files 已经排除掉 stages/ 和 web/（那两类改完不用重启：
  //   stages 是子进程现读，web 是每次请求从盘读）。只按 code_mtime 判断的话，
  //   改一个 stage 也会弹"新功能不会生效" —— 那是假警报，弹多了用户就不看了。
  const sf0 = Array.isArray(h.stale_files) ? h.stale_files : [];
  if (!sf0.length) return;
  try {
    if (sessionStorage.getItem("vsStale") === String(h.started_ts)) return;
  } catch (e) {}

  const mins = Math.max(1, Math.round((Date.now() / 1000 - h.started_ts) / 60));
  const bar = document.createElement("div");
  bar.id = "staleBar";
  bar.style.cssText =
    "position:sticky;top:0;z-index:9999;background:#fff4d6;color:#6b4a00;" +
    "border-bottom:1px solid #e8c56a;padding:9px 14px;font-size:13px;line-height:1.6;" +
    "display:flex;gap:10px;align-items:flex-start";
  // ★ 把「改了没生效的文件」列出来：光说"要重启"，用户不知道自己等的那个
  //   修复到底在不在里面。2026-10-24 真事：19:08 改好的多角色分句一直没生效
  //   （服务 16:58 起的），老板看到的现象是「还是没修好」。
  const sf = sf0;
  const fileList = sf.length
    ? `<div style="margin-top:5px;font-size:12px;opacity:.9">没生效的改动：` +
      sf.map((f) => `<code style="background:rgba(0,0,0,.07);padding:1px 5px;` +
        `border-radius:3px;margin-right:5px">${esc(f.name)} ${esc(f.at || "")}</code>`)
        .join("") + `</div>`
    : "";
  bar.innerHTML =
    `<span style="font-size:15px">⚠</span>` +
    `<span style="flex:1">这个服务是 <b>${esc(h.started_at || "")}</b> 起的（已经跑了 ${mins} 分钟），` +
    `而代码在那之后改过 —— <b>新功能不会生效</b>。请关掉黑窗口，双击 <b>启动VoiceStudio.bat</b> 重启一次。` +
    fileList + `</span>`;
  const x = document.createElement("button");
  x.type = "button";
  x.textContent = "知道了";
  x.style.cssText = "background:#6b4a00;color:#fff;border:0;border-radius:6px;" +
    "padding:4px 10px;cursor:pointer;font-size:12px;flex:none";
  x.onclick = function () {
    bar.remove();
    try { sessionStorage.setItem("vsStale", String(h.started_ts)); } catch (e) {}
  };
  bar.appendChild(x);
  document.body.insertBefore(bar, document.body.firstChild);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => {
    checkServiceFresh();
    paintPitchInit();
    loadSwitches();
  });
} else {
  checkServiceFresh();
  paintPitchInit();
  loadSwitches();
}

/* ---------------------------------------------------------------- 功能开关
   老板（2026-10-26）：「我希望我能自己选择启用什么功能，来挨个尝试」。
   所以唱前/唱后每一步都给一个勾选框 —— 想听某一步在干什么，关掉它跑一遍。

   ★ 清单来自后端 /api/fine/polish/steps，**前端不许手抄步骤名**：
     抄漏一步 = 那一步永远关不掉（用户会以为开关坏了），而这种事没人会发现。
   ★ 传上去的是「关掉的那几步」（pre_skip / post_skip），后端还会再过一遍
     白名单 —— 拼错的名字一律忽略，不许把整步偷偷跑起来。
   ---------------------------------------------------------------------- */
let SW_STEPS = { pre: [], post: [] };
let SW_PRE_DEFAULT = "guard";

async function loadSwitches() {
  const boxPre = $("#swPre"), boxPost = $("#swPost");
  if (!boxPre || !boxPost) return;
  let modes = [];
  try {
    const d = await api("/api/fine/polish/steps");
    SW_STEPS.pre = d.pre || [];
    SW_STEPS.post = d.post || [];
    modes = d.pre_modes || [];
    SW_PRE_DEFAULT = d.pre_mode_default || "guard";
  } catch (e) {
    // 拿不到清单就**照实说**，不要悄悄留一片空白（那会让人以为功能没了）
    boxPre.innerHTML = '<span class="hint">开关清单没读到（服务端太旧？重启一次即可）</span>';
    return;
  }
  const mk = (arr, side, box) => {
    box.innerHTML = arr.map((s) =>
      `<label class="chk" title="${_fe(s.name)}">
         <input type="checkbox" data-sw="${side}" data-name="${_fe(s.name)}"
                ${s.default === false ? "" : "checked"}>
         <span>${_fe(s.label)}</span></label>`).join("");
  };
  mk(SW_STEPS.pre, "pre", boxPre);
  mk(SW_STEPS.post, "post", boxPost);
  // 默认**只开"防破音"那一步**（现在是"修削波"），其余默认关。
  // 为什么：默认全开那一版，同一模型同一首歌被啃到"丢词、丢音、还有点变调"
  // （逐字数过：6.7% 的帧咬字掉 >1dB、最狠一帧抹平了 39dB）。
  // 现在默认只做**有确定真值**的那一步，剩下的你自己勾 —— 开哪些、开到多大，
  // 由 `/api/fine/polish/steps` 那一条清单说了算，前端不发明。
  if (modes.length) {
    paintStagePreMode(modes);
  }
}

function swAll(on) {
  document.querySelectorAll('input[data-sw]').forEach((el) => { el.checked = !!on; });
  const pr = $("#swPresence");
  if (pr) pr.checked = !!on;
}

function swSkip(side) {
  // 返回"被关掉的那几步"；控件还没渲染出来时返回 null（后端按默认处理）
  const nodes = document.querySelectorAll(`input[data-sw="${side}"]`);
  if (!nodes.length) return null;
  const off = [];
  nodes.forEach((el) => { if (!el.checked) off.push(el.dataset.name); });
  return off;
}

/* ---- 大厅的「唱前清理」下拉 ----
   老板（2026-10-26）：「现在微调的过头了…… 搞成手动开关 我决定开不开 大厅要有按键」。

   ★ 为什么一定要放在大厅：大厅是「拖人 + 拖歌 → 一键合成」的主路径，而这些开关
     原先只在角色详情抽屉的「唱歌」页签里 —— 那要先点开角色卡、再切页签才看得到，
     绝大多数人根本不会发现（这正是"我明明说了我决定"却没做到）。

   ★ 选项**由后端给**（/api/fine/polish/steps 的 pre_modes），别在这里写死：
     写死了就会出现"后端早就只保留防破音了，界面上还写着全都做"。
*/
let SG_PRE_MODES = [];

function paintStagePreMode(modes) {
  SG_PRE_MODES = modes || [];
  const sel = $("#sgPreMode");
  if (!sel) return;
  sel.innerHTML = SG_PRE_MODES.map(
    (m) => `<option value="${_fe(m[0])}">${_fe(m[1])}</option>`).join("");
  sel.onchange = () => sgPreModeChange();
  const dflt = (SW_PRE_DEFAULT || "guard");
  sel.value = SG_PRE_MODES.some((m) => m[0] === dflt) ? dflt : "guard";
  sgPreModeChange();
}

function sgPreModeChange() {
  const v = ($("#sgPreMode") && $("#sgPreMode").value) || "guard";
  const hit = SG_PRE_MODES.find((m) => m[0] === v);
  const hint = $("#sgPreHint");
  if (hint) {
    hint.textContent = hit ? hit[2]
      : "唱之前不动干声，源头是什么样就什么样。";
  }
}

function paintPitchInit() {
  // 浏览器会**恢复上次填过的值**（含 select / number / range），刷新页面不等于
  // 回到默认。所以开局必须照当前值刷一次失真提醒 —— 否则用户刚重开页面，
  // 上次那个 ±12 还在，而警告条是空的。
  onPitchPick();
  sgPitchWarn();
}

/* ==========================================================================
   精修台：音块级微调（去哑音 / 补音 / 改调 / 拆和声）
   ==========================================================================
   老板（2026-10-25）要的是「像 RipX 那样自己逐个音抠」的能力。落到这里是：
     ① 把音频切成一个个音块画在钢琴卷帘上；
     ② 点一块 → 改调 / 提音量 / 挪时间 / 补音 / 静音 / 删掉；
     ③ 一键把「哑音 / 混响糊 / 叠音 / 爆音」这批都算出来；
     ④ 改完导出一版成品。
   ★ 所有"一键"都是**先生成修改清单**并显示在块上，不是直接改掉 ——
     用户必须看得见它打算动哪几块，否则一点就没了、没法反悔。
   ========================================================================== */

let FINE = null;            // {path,sr,dur,blocks,diag,wave,edits:{},sel,label}
let FINE_SRC = [];          // /api/fine/sources 回来的清单
let STEM_SONGS = [];        // 分轨另存的候选歌

const FINE_C = { dead: "#ff5f6d", weak: "#ffb648", reverb: "#b072ff",
                 stack: "#ffd93d", clip: "#ff4fd8", normal: "#4c8dff",
                 unvoiced: "#5b6b85" };
const FINE_NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];

function _fe(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fineNoteName(m) {
  if (!m || m <= 0) return "—";
  m = Math.round(m);
  return FINE_NOTE[((m % 12) + 12) % 12] + (Math.floor(m / 12) - 1);
}
function fineFlagOf(i) {
  if (!FINE) return "";
  const d = FINE.diag || {};
  for (const k of ["clip", "dead", "reverb", "stack", "weak"]) {
    if ((d[k] || []).indexOf(i) >= 0) return k;
  }
  return "";
}
function fineEd(i) {
  if (!FINE) return null;
  return FINE.edits[i] || null;
}

/* ---- 载入可选音频 ---- */
async function fineInit() {
  const sel = $("#fineSrc");
  if (!sel) return;
  try {
    const j = await api("/api/fine/sources");
    FINE_SRC = j.items || [];
    sel.innerHTML = '<option value="">— 选一段要修的音频 —</option>' +
      FINE_SRC.map((s, i) => `<option value="${i}">${_fe(s.label)}`
        + `　[${_fe(s.kind)} · ${s.mb}MB]</option>`).join("");
    if (!FINE_SRC.length) $("#fineSrcHint").textContent = "还没有可修的音频（先跑一次翻唱或导入一首歌）";
  } catch (e) {
    $("#fineSrcHint").textContent = "清单没读到：" + e.message;
  }
  stemInit();
  fineWireCanvas();     // 画布事件只挂一次，挂早了 canvas 还没显示出来
}

function fineSrcChange() {
  const i = $("#fineSrc").value;
  if (i === "" || !FINE_SRC[+i]) return;
  fineLoadPath(FINE_SRC[+i].path, FINE_SRC[+i].label);
}

function finePickFile() { $("#fineFile").click(); }

function fineFileChosen(f) {
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f);
  $("#fineSrcHint").textContent = "上传中…";
  fetch("/api/upload", { method: "POST", body: fd })
    .then((r) => r.json())
    .then((j) => {
      if (j && j.path) { fineLoadPath(j.path, f.name); return; }
      toast(j && j.error ? j.error : "上传失败", "bad");
      $("#fineSrcHint").textContent = "";
    })
    .catch((e) => { toast("上传失败：" + e.message, "bad"); $("#fineSrcHint").textContent = ""; });
}

async function fineLoadPath(path, label) {
  const hint = $("#fineSrcHint");
  hint.textContent = "正在切块分析…";
  try {
    const j = await post("/api/fine/analyze", { path });
    FINE = { path, sr: j.sr, dur: j.dur, blocks: j.blocks || [], diag: j.diag || {},
             wave: j.wave || [], edits: {}, sel: -1, label: label || "" };
    $("#fineTools").style.display = "";
    $("#fineCvWrap").style.display = "";
    $("#fineOut").style.display = "";
    $("#fineSide").style.display = "";
    $("#finePolish").style.display = "";
    $("#fineAudit").style.display = "";
    audFillSteps("post");
    audStop();            // ★ 换歌必须把上一条的试听停掉并清空，不然会放着上一首
    finePolMeasure();     // 载入就量一遍：让用户先看见"脏在哪"再决定修不修
    const d = FINE.diag || {};
    const bad = ["dead", "reverb", "stack", "clip"]
      .map((k) => `${({ dead: "哑音", reverb: "混响糊", stack: "叠音", clip: "爆音" })[k]} ${(d[k] || []).length}`)
      .join(" · ");
    hint.textContent = `${FINE.blocks.length} 个音块 · ${bad || "没查出明显问题"}`;
    fineCount();
    fineDraw();
    fineSelect(-1);
  } catch (e) {
    toast("没分析出来：" + e.message, "bad");
    hint.textContent = "";
  }
}

function fineCount() {
  const n = Object.keys((FINE && FINE.edits) || {}).length;
  $("#fineCount").textContent = n ? `已改 ${n} 块` : "还没改过";
}

/* ---- 画钢琴卷帘 ---- */
function fineGeom() {
  const wrap = $("#fineCvWrap");
  const W = Math.max(320, (wrap ? wrap.clientWidth : 900) - 2);
  const zoom = +$("#fineZoom").value || 1;
  const pps = Math.max(4, (W / Math.max(FINE.dur, 1))) * zoom;   // 每秒像素
  return { W: Math.max(W, FINE.dur * pps + 60), pps, PADL: 46, TOP: 8, PH: 236, WH: 54, AX: 20 };
}

function fineDraw() {
  const cv = $("#fineCv");
  if (!cv || !FINE) return;
  const g = fineGeom();
  const H = g.TOP + g.PH + g.WH + g.AX + 10;
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const cw = Math.round(g.W);
  if (cv.width !== Math.round(cw * dpr) || cv.height !== Math.round(H * dpr)) {
    cv.width = Math.round(cw * dpr);
    cv.height = Math.round(H * dpr);
  }
  cv.style.width = cw + "px";
  cv.style.height = H + "px";
  const c = cv.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, cw, H);
  c.fillStyle = "#111725";
  c.fillRect(0, 0, cw, H);

  // 音高范围：跟着这一条的实际情况走，别钉死 C2~C6（童声和男低音差三个八度）
  let lo = 999, hi = -999;
  for (const b of FINE.blocks) {
    if (b.midi > 0) { lo = Math.min(lo, b.midi); hi = Math.max(hi, b.midi); }
  }
  if (lo > hi) { lo = 48; hi = 72; }
  lo = Math.max(12, Math.floor(lo - 2)); hi = Math.min(108, Math.ceil(hi + 2));
  if (hi - lo < 12) { hi = lo + 12; }
  const fy = (m) => g.TOP + (1 - (m - lo) / (hi - lo)) * g.PH;

  // 八度网格 + 左侧音名
  c.font = "10px system-ui,sans-serif";
  for (let m = Math.ceil(lo / 12) * 12; m <= hi; m += 12) {
    const y = fy(m);
    c.strokeStyle = "#1e2739"; c.lineWidth = 1;
    c.beginPath(); c.moveTo(g.PADL, y); c.lineTo(cw - 6, y); c.stroke();
    c.fillStyle = "#6b7891"; c.textAlign = "right"; c.textBaseline = "middle";
    c.fillText(fineNoteName(m), g.PADL - 6, y);
  }
  // 半音细线（只画 C/E/G 这种好认的，画满 12 条会糊成一片）
  for (let m = Math.ceil(lo); m <= hi; m++) {
    if ([0, 4, 7].indexOf(((m % 12) + 12) % 12) < 0) continue;
    const y = fy(m);
    c.strokeStyle = "#172032";
    c.beginPath(); c.moveTo(g.PADL, y); c.lineTo(cw - 6, y); c.stroke();
  }

  // 波形
  if ($("#fineShowWave").checked && FINE.wave.length) {
    const y0 = g.TOP + g.PH + 6, hh = g.WH - 12, mid = y0 + hh / 2;
    c.fillStyle = "#0e1522"; c.fillRect(g.PADL, y0, cw - g.PADL - 6, hh);
    c.strokeStyle = "#3d5a8a"; c.lineWidth = 1; c.beginPath();
    const n = FINE.wave.length;
    for (let x = 0; x < cw - g.PADL - 6; x++) {
      const i = Math.min(n - 1, Math.floor(x / (cw - g.PADL - 6) * n));
      const v = Math.min(1, FINE.wave[i] * 3.2) * (hh / 2);
      c.moveTo(g.PADL + x + 0.5, mid - v); c.lineTo(g.PADL + x + 0.5, mid + v);
    }
    c.stroke();
  }

  // 音块
  for (const b of FINE.blocks) {
    const x = g.PADL + b.t0 * g.pps;
    const w = Math.max(2.5, (b.t1 - b.t0) * g.pps - 1);
    const flag = fineFlagOf(b.i);
    const ed = fineEd(b.i);
    let col = flag ? FINE_C[flag] : FINE_C.normal;
    if (b.midi <= 0) col = FINE_C.unvoiced;
    if (ed && (ed.mute || ed.del)) col = "#3a4256";
    const h = b.midi > 0 ? 14 : 6;
    const y = (b.midi > 0 ? fy(b.midi) : g.TOP + g.PH - 8) - h / 2;
    c.fillStyle = col;
    c.globalAlpha = ed ? 1 : 0.86;
    c.beginPath();
    const r = Math.min(4, h / 2, w / 2);
    c.moveTo(x + r, y); c.arcTo(x + w, y, x + w, y + h, r);
    c.arcTo(x + w, y + h, x, y + h, r); c.arcTo(x, y + h, x, y, r);
    c.arcTo(x, y, x + w, y, r); c.closePath(); c.fill();
    c.globalAlpha = 1;
    if (ed) {   // 改过的块描个白边，一眼看出动过哪几块
      c.strokeStyle = "#ffffff"; c.lineWidth = 1; c.stroke();
    }
    if (b.i === FINE.sel) {
      c.strokeStyle = "#fff"; c.lineWidth = 2;
      c.strokeRect(x - 2, y - 3, w + 4, h + 6);
    }
  }

  // 时间轴
  const ay = g.TOP + g.PH + g.WH + 12;
  c.strokeStyle = "#242e42"; c.beginPath();
  c.moveTo(g.PADL, ay); c.lineTo(cw - 6, ay); c.stroke();
  const step = FINE.dur > 240 ? 30 : (FINE.dur > 90 ? 15 : 5);
  c.fillStyle = "#6b7891"; c.textAlign = "center"; c.textBaseline = "top";
  for (let t = 0; t <= FINE.dur; t += step) {
    const x = g.PADL + t * g.pps;
    c.strokeStyle = "#242e42"; c.beginPath();
    c.moveTo(x, ay); c.lineTo(x, ay + 4); c.stroke();
    const mm = Math.floor(t / 60), ss = Math.round(t % 60);
    c.fillText(`${mm}:${String(ss).padStart(2, "0")}`, x, ay + 5);
  }
}

/* ---- 交互：点选 + 上下拖改调 ---- */
function fineXY(ev) {
  const cv = $("#fineCv");
  const r = cv.getBoundingClientRect();
  return { x: ev.clientX - r.left, y: ev.clientY - r.top };
}
function fineHitBlock(p) {
  if (!FINE) return -1;
  const g = fineGeom();
  const t = (p.x - g.PADL) / g.pps;
  let best = -1, bd = 1e9;
  for (const b of FINE.blocks) {
    if (t >= b.t0 - 0.02 && t <= b.t1 + 0.02) {
      const d = Math.abs(t - (b.t0 + b.t1) / 2);
      if (d < bd) { bd = d; best = b.i; }
    }
  }
  return best;
}
let FINE_DRAG = null;
function fineWireCanvas() {
  const cv = $("#fineCv");
  if (!cv || cv._wired) return;
  cv._wired = true;
  cv.addEventListener("mousedown", (ev) => {
    if (!FINE) return;
    const p = fineXY(ev);
    const i = fineHitBlock(p);
    if (i < 0) return;
    fineSelect(i);
    FINE_DRAG = { i, y: p.y, semi: (fineEd(i) || {}).semi || 0 };
  });
  cv.addEventListener("mousemove", (ev) => {
    if (!FINE_DRAG || !FINE) return;
    const p = fineXY(ev);
    // 6px 一个半音：手抖一格不至于跳两个半音，又不用拖很远
    const semi = Math.max(-12, Math.min(12,
      FINE_DRAG.semi - Math.round((p.y - FINE_DRAG.y) / 6)));
    if (semi !== ((fineEd(FINE_DRAG.i) || {}).semi || 0)) {
      fineEditProp("semi", semi, FINE_DRAG.i);
    }
  });
  window.addEventListener("mouseup", () => { FINE_DRAG = null; });
}

function fineSelect(i) {
  if (!FINE) return;
  FINE.sel = i;
  const box = $("#fsTitle"), sub = $("#fsSub");
  if (i < 0 || !FINE.blocks[i]) {
    box.textContent = "没选中";
    sub.textContent = "点下面图里任意一块，或者点「一键修」让它把问题块挑出来";
    return;
  }
  const b = FINE.blocks[i];
  const flag = fineFlagOf(i);
  const names = { dead: "哑音（这个音闷掉了）", weak: "偏弱", reverb: "混响糊（有声但听不出字）",
                  stack: "叠音（两个音打架）", clip: "爆音（削波了）" };
  box.textContent = `第 ${i + 1} 块　${fineNoteName(b.midi)}　${b.t0.toFixed(2)}–${b.t1.toFixed(2)}s`;
  sub.textContent = `${flag ? names[flag] + " · " : ""}响度 ${(b.rel != null ? b.rel : 1).toFixed(2)}× · 谐波度 ${b.harm}`;
  const ed = fineEd(i) || {};
  $("#fsSemi").value = ed.semi || 0; $("#fsSemiV").textContent = ed.semi || 0;
  $("#fsGain").value = ed.gain || 0; $("#fsGainV").textContent = ed.gain || 0;
  $("#fsShift").value = Math.round((ed.shift || 0) * 1000); $("#fsShiftV").textContent = Math.round((ed.shift || 0) * 1000);
  $("#fsDe").value = Math.round((ed.dereverb || 0) * 100); $("#fsDeV").textContent = Math.round((ed.dereverb || 0) * 100);
  fineNoteHint();
  fineDraw();
}
function fineNoteHint() {
  if (!FINE || FINE.sel < 0) { $("#fsNote").textContent = ""; return; }
  const b = FINE.blocks[FINE.sel];
  const s = +(($("#fsSemi").value) || 0);
  $("#fsNote").textContent = b.midi > 0
    ? `${fineNoteName(b.midi)} → ${fineNoteName(b.midi + s)}　(${b.f0 ? b.f0.toFixed(0) : "—"}Hz)` : "";
}
function fineEditProp(key, val, at) {
  if (!FINE) return;
  const i = (at == null ? FINE.sel : at);
  if (i < 0) return;
  if (!FINE.edits[i]) FINE.edits[i] = { i };
  FINE.edits[i][key] = val;
  if (key === "semi") { $("#fsSemiV").textContent = val; fineNoteHint(); }
  if (key === "gain") $("#fsGainV").textContent = val;
  if (key === "shift") $("#fsShiftV").textContent = Math.round(val * 1000);
  if (key === "dereverb") $("#fsDeV").textContent = Math.round(val * 100);
  if (at != null) { $("#fsSemi").value = val; $("#fsSemiV").textContent = val; }
  fineCount(); fineDraw();
}
function fineProp(key, val) {
  if (!FINE || FINE.sel < 0) { toast("先点一块", "bad"); return; }
  fineEditProp(key, val);
  toast(val === true ? "已标记（导出时生效）" : "已改");
}
function fineClearOne() {
  if (!FINE || FINE.sel < 0) return;
  delete FINE.edits[FINE.sel];
  fineCount(); fineSelect(FINE.sel);
}
function fineReset() {
  if (!FINE) return;
  FINE.edits = {};
  fineCount(); fineSelect(FINE.sel); fineDraw();
  toast("已全部还原");
}

/* ---- 一键修：只生成修改清单 ---- */
async function fineFix(kind) {
  if (!FINE) { toast("先载入一段音频", "bad"); return; }
  const names = { dedead: "去哑音", refill: "补音", dereverb: "去混响哑音",
                  deoverlap: "处理叠音", declip: "修爆音" };
  try {
    const j = await post("/api/fine/fix",
      { path: FINE.path, kind, blocks: FINE.blocks, diag: FINE.diag,
        amt: +($("#fineDv").value || 0) / 100 || 0.6 });
    const eds = j.edits || [];
    if (!eds.length) { toast(`没找到需要「${names[kind]}」的音块`, "bad"); return; }
    for (const e of eds) {
      if (!FINE.edits[e.i]) FINE.edits[e.i] = { i: e.i };
      Object.assign(FINE.edits[e.i], e);
    }
    fineCount(); fineDraw();
    toast(`「${names[kind]}」：${eds.length} 块已标记，可逐块核对后导出`);
    if (eds.length) fineSelect(eds[0].i);
  } catch (e) {
    toast("没算出来：" + e.message, "bad");
  }
}

/* ---- 补音：让后端找最接近的那块（同一个挑选规则，别在前端再写一遍）---- */
async function fineFill() {
  if (!FINE || FINE.sel < 0) { toast("先点一块", "bad"); return; }
  const i = FINE.sel;
  try {
    const j = await post("/api/fine/fix",
      { path: FINE.path, kind: "refill", blocks: FINE.blocks, diag: { dead: [i] } });
    const e = (j.edits || [])[0];
    if (!e) { toast("附近找不到能拿来补的音（音高差太大或它自己也哑）", "bad"); return; }
    if (!FINE.edits[i]) FINE.edits[i] = { i };
    FINE.edits[i].fill = e.fill;
    delete FINE.edits[i].mute; delete FINE.edits[i].del;
    fineCount(); fineDraw();
    toast(`用第 ${e.fill + 1} 块补这里（会自动按这块的目标音高对齐）`);
  } catch (err) { toast("补音失败：" + err.message, "bad"); }
}

/* ---- 导出 ---- */
async function fineRender() {
  if (!FINE) { toast("先载入一段音频", "bad"); return; }
  const eds = Object.values(FINE.edits);
  if (!eds.length && !+$("#fineDv").value && !+$("#fineGain").value) {
    toast("还没改过任何东西", "bad"); return;
  }
  const btn = $("#btnFineRender");
  btn.disabled = true; btn.textContent = "导出中…";
  try {
    const j = await post("/api/fine/render", {
      path: FINE.path, blocks: FINE.blocks, edits: eds,
      name: FINE.label || FINE.path,
      gain_db: +$("#fineGain").value || 0,
      dereverb: +$("#fineDv").value / 100 || 0,
    });
    $("#finePlayer").style.display = "";
    $("#fineAudio").src = media(j.path);
    $("#fineOutHint").textContent = `已导出：${j.name}（${j.mb}MB）`;
    toast("导出完成，可以听一下");
    fineInit();     // 产物会进可选清单，刷新一下
  } catch (e) {
    toast("没导出：" + e.message, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "导出成品";
  }
}

async function fineSplit() {
  if (!FINE) { toast("先载入一段音频", "bad"); return; }
  try {
    const j = await post("/api/fine/split",
      { path: FINE.path, n: +$("#fineLayers").value || 2 });
    $("#fineOutHint").textContent =
      `已分成 ${j.items.length} 层：${j.items.map((x) => `${x.name}(${x.blocks}块)`).join("、")} → ${j.dir}`;
    toast("分层完成（这是按音高归层，不是无损拆开叠在一起的声音）");
  } catch (e) { toast("没分成：" + e.message, "bad"); }
}

/* ---- 整轨微调：唱前 / 唱后（量 → 修 → 复测）---- */
//
// ★ 为什么单独一排，不塞进"一键修"：那边生成的是**逐块 edits**（给你看、
//   给你改），这边是**真的改整条轨**并出成品。两件事的粒度不一样 ——
//   底噪、齿音、拼接点都是整条轨的性质，切成 200ms 一块反而量不出来。
//
// ★ 界面必须把"为什么没动"说清楚：这套修法全部是"先量后修、没好转就回滚"，
//   所以"点了没变化"经常是**正确结果**（量的出来才修）。不说清楚，
//   用户只会以为功能坏了。

const POL_LABEL = {
  pre: "唱前（喂给模型之前）", post: "唱后（已经唱出来）",
};

function polChip(label, val, bad) {
  return `<span class="pm ${bad ? "bad" : (bad === false ? "ok" : "")}">`
    + `${label} <b>${val}</b></span>`;
}

function finePolShow(m) {
  const box = document.getElementById("finePolM");
  if (!box || !m) return;
  const p = m.pre || {}, q = m.post || {};
  const n = (v, d = 1) => (v === null || v === undefined ? "—" : (+v).toFixed(d));
  box.innerHTML =
    polChip("噪声底", n(p.floor_db) + " dB", p.snr_db < 15) +
    polChip("信噪比", n(p.snr_db) + " dB", p.snr_db < 15) +
    polChip("隆隆(20~80Hz)", n(p.rumble_db) + " dB", false) +
    polChip("削波", (p.clip_ratio * 100).toFixed(2) + "%", p.clip_ratio > 1e-4) +
    polChip("爆音点", String(p.clicks), p.clicks > 0) +
    polChip("齿音超出", n(q.sib_excess_db) + " dB", q.sib_excess_db >= 3) +
    polChip("刺耳超出", n(q.harsh_excess_db) + " dB", q.harsh_excess_db >= 2.5) +
    polChip("高音亮度", n(q.belt_rel_db) + " dB" + (q.belt_frames ? `（${q.belt_frames}帧）` : ""), null) +
    polChip("拼接点", String(q.splices), q.splices > 0) +
    polChip("峰值", n(q.peak, 3), q.peak > 0.985);
  box.style.display = "";
}

async function finePolMeasure() {
  if (!FINE) return;
  const hint = document.getElementById("finePolHint");
  // ★ 先把旧量表清掉：换一首歌时新那份还没量完，不清的话界面上显示的
  //   是**上一首**的数（真浏览器实测撞到过：载入脏片段后量表却是干净片段的
  //   ——噪声底 −200dB 那种一看就是另一条音频）。旧数据比没数据更害人。
  const box0 = document.getElementById("finePolM");
  if (box0) { box0.innerHTML = ""; box0.style.display = "none"; }
  FINE.polish = null;
  hint.textContent = "正在量…";
  try {
    const j = await post("/api/fine/polish", { path: FINE.path });
    FINE.polish = j.measure;
    finePolShow(j.measure);
    hint.textContent = "量完了 —— 红的是有问题、可以修的。";
  } catch (e) {
    hint.textContent = "没量出来：" + e.message;
  }
}

async function finePolish(side) {
  if (!FINE) { toast("先载入一段音频", "bad"); return; }
  const hint = document.getElementById("finePolHint");
  const btns = document.querySelectorAll("#finePolish button");
  btns.forEach((b) => { b.disabled = true; });
  hint.textContent = POL_LABEL[side] + "正在修…（先量，再逐项试，修完复测）";
  try {
    const j = await post("/api/fine/polish/apply", {
      path: FINE.path, side,
      amount: (+document.getElementById("finePolAmt").value || 100) / 100,
    });
    const steps = (j.steps || []).filter((s) => s.kept);
    if (!j.applied) {
      // ★ "没修"是这套修法的正常结果 —— 必须照实说，不能假装成功。
      hint.textContent = "量完了，但没有一项达标 ⇒ 一条都没动（音频是原样）。"
        + (j.reason ? "（" + j.reason + "）" : "");
      toast("不需要修，音频没动", "");
      if (j.steps) finePolShow(FINE.polish);
      return;
    }
    FINE.path = j.path;
    FINE.label = j.name || j.path;
    audStop();   // 换了成品 ⇒ 旧的试听片段对不上新音频了，停掉并清空
    const doneMsg = `已修 ${steps.length} 项：`
      + (steps.map((s) => s.name).join("、") || "无") + ` → ${j.name}`;
    toast("微调完成：" + j.name);
    await finePolMeasure();      // 修完再量一遍，让用户自己看到数变了
    // ★ 复测会把 hint 改成"量完了"，必须把"修了什么"再写回去 ——
    //   真浏览器实测：点了修，最后提示只剩"量完了"，用户根本看不见修了哪几项。
    hint.textContent = doneMsg + "（右边是修完再量一遍的数）";
    fineInit();                  // 产物会进可选清单
  } catch (e) {
    toast("没修成：" + e.message, "bad");
    hint.textContent = "没修成：" + e.message;
  } finally {
    btns.forEach((b) => { b.disabled = false; });
  }
}

/* ---- 试听（干湿 A/B）：让耳朵说了算 ----
 *
 * 为什么这么设计：上面那些"gain / harm"数字只能证明"该治的指标动了、不该碰的
 * 没动"，**证明不了好不好听**。老板要的是"时刻听见我调整的内容 来确认我调整
 * 是否对" ⇒ 验收权必须交回耳朵。
 *
 * 关键取舍：**干湿滑条不重算音频**。两条（修前 / 修后）各是一个 <audio>，
 * 同步播放、音量互补（各开一半能量），拖滑条只是改音量 —— 所以是即时的。
 * （照分轨那条 A/B 的写法：两条要一直贴着放，不然比的是两个时间点。）
 */

const AUD = { side: "post", dry: null, wet: null, tag: "", timer: 0, sec: 8 };

const AUD_STEPS = {
  pre: [["declip", "修削波"], ["declick", "修爆音"], ["hp", "去隆隆"],
        ["denoise", "去底噪"], ["level", "电平规整"]],
  post: [["formant", "共振峰修正"], ["deess", "去齿音"], ["deharsh", "去刺耳"],
         ["belt", "高音柔化"], ["heal", "修拼接"], ["limit", "软限幅"]],
};

function audFillSteps(side) {
  const sel = $("#audOnly");
  if (!sel) return;
  const v = sel.value;
  sel.innerHTML = '<option value="">整条链（全部步骤）</option>' +
    (AUD_STEPS[side] || []).map(([k, t]) => `<option value="${k}">只听：${t}</option>`).join("");
  sel.value = v;
  if (sel.selectedIndex < 0) sel.value = "";
}

/** 步骤名 → 人话。★ 后端给回来的是 deess 这种**内部名**，直接摆到界面上
 *  谁也看不懂（真浏览器实测：界面写的是「这次动的是：deess、heal」）。 */
function audStepName(k) {
  for (const side of ["pre", "post"]) {
    const hit = (AUD_STEPS[side] || []).find(x => x[0] === k);
    if (hit) return hit[1];
  }
  return k;
}

async function fineAudit(side) {
  if (!FINE) { toast("先载入一段音频", "bad"); return; }
  AUD.side = side || "post";
  audFillSteps(AUD.side);
  await fineAuditRun();
}

/** 换步骤 / 换力度时重跑（防抖——拖滑条会连续触发，不能每次都重算）。
 *  ★ 还没生成过试听就别自作主张跑：用户可能只是在调力度准备点「唱后」。 */
function fineAuditRerun() {
  clearTimeout(AUD.timer);
  if (!AUD.dry) return;
  AUD.timer = setTimeout(() => fineAuditRun(true), 350);
}

async function fineAuditRun(keepTime) {
  const hint = $("#audHint");
  const t0 = (keepTime && AUD.dry) ? AUD.dry.currentTime : 0;
  const wasPlaying = !!(AUD.dry && !AUD.dry.paused);
  hint.textContent = "正在挑问题最重的一小段、跑一遍修前/修后…";
  $("#btnAudPost").disabled = $("#btnAudPre").disabled = true;
  try {
    const j = await post("/api/fine/polish/audition", {
      path: FINE.path, side: AUD.side,
      only: ($("#audOnly") || {}).value || "",
      amount: (+$("#finePolAmt").value || 100) / 100,
      sec: AUD.sec,
    });
    AUD.dry = new Audio(media(j.dry));
    AUD.wet = new Audio(media(j.wet));
    [AUD.dry, AUD.wet].forEach(a => {
      a.preload = "auto";
      a.addEventListener("timeupdate", () => {
        // 两条必须贴着放：不然"对比"比的是两个不同的时间点，反而误导人
        const other = (a === AUD.dry) ? AUD.wet : AUD.dry;
        if (other && Math.abs(other.currentTime - a.currentTime) > 0.25) {
          other.currentTime = a.currentTime;
        }
      });
      a.addEventListener("ended", () => { $("#btnAudPlay").textContent = "▶ 听"; });
    });
    audCross();
    const st = (j.steps || []).filter(s => s.kept).map(s => audStepName(s.name));
    $("#audClip").style.display = "";
    $("#audClipInfo").textContent =
      `第 ${j.from} 秒起、${j.sec} 秒 · 修前 / 修后两条已就绪`
      + (j.applied ? ` · 这次动的是：${(st.join("、") || "—")}`
                   : ` · ${j.reason || "没达标，没修"}（两条听起来会一样）`);
    hint.textContent = j.applied
      ? "拖「干/湿」来回听：0 是原声、100 是修完的。"
      : "这一步没达标 ⇒ 没修，两条听起来一样。换个步骤或加大力度再试。";
    $("#btnAudPlay").disabled = false;
    if (wasPlaying || !keepTime) {
      AUD.dry.currentTime = t0; AUD.wet.currentTime = t0;
      audPlay();
    }
  } catch (e) {
    hint.textContent = "试听没做出来：" + e.message;
    toast("试听没做出来：" + e.message, "bad");
  } finally {
    $("#btnAudPost").disabled = $("#btnAudPre").disabled = false;
  }
}

/** 干湿交叉淡化：只改音量，不改音频 ⇒ 拖动是即时的。 */
function audCross() {
  const t = (+$("#audMix").value || 0) / 100;
  $("#audMixV").textContent = Math.round(t * 100);
  if (!AUD.dry || !AUD.wet) return;
  AUD.dry.volume = 1.0 - t;
  AUD.wet.volume = t;
}

function audStop() {
  [AUD.dry, AUD.wet].forEach(a => {
    if (!a) return;
    try { a.pause(); } catch (e) { /* 已经停了 */ }
  });
  AUD.dry = AUD.wet = null;
  const p = $("#btnAudPlay");
  if (p) p.textContent = "▶ 听";
  const c = $("#audClip");
  if (c) c.style.display = "none";
}

function audPlay() {
  if (!AUD.dry) return;
  audCross();
  AUD.dry.play().catch(() => {});
  AUD.wet.play().catch(() => {});
  $("#btnAudPlay").textContent = "⏸ 停";
}

function audToggle() {
  if (!AUD.dry || !AUD.wet) {
    toast("先点「唱前」或「唱后」生成试听片段", "");
    return;
  }
  if (AUD.dry.paused) { audPlay(); return; }
  AUD.dry.pause(); AUD.wet.pause();
  $("#btnAudPlay").textContent = "▶ 听";
}

/* ---- 分轨另存（中文文件夹）---- */
async function stemInit() {
  const sel = $("#stemSong");
  if (!sel) return;
  try {
    const j = await api("/api/music/library");
    STEM_SONGS = j.songs || [];
    sel.innerHTML = '<option value="">— 选一首歌 —</option>' +
      STEM_SONGS.map((s, i) => `<option value="${i}">${_fe(s.title || s.path)}</option>`).join("");
  } catch (e) { /* 曲库读不到不影响别的功能 */ }
}

function stemPath() {
  const i = $("#stemSong").value;
  return (i === "" ? "" : (STEM_SONGS[+i] || {}).path) || "";
}

async function stemStatus() {
  const p = stemPath(), box = $("#stemList"), hint = $("#stemHint");
  if (!p) { box.innerHTML = ""; return; }
  try {
    const j = await api("/api/fine/stems?song=" + encodeURIComponent(p));
    const all = (j.items || []).concat(j.extra || []);
    box.innerHTML = all.length
      ? all.map((x) => `<div class="fine-stemrow"><b>${_fe(x.folder)}</b>`
          + `<span>${_fe(x.sec != null ? x.sec.toFixed(1) + "s" : "")}</span>`
          + `<span class="pth" title="${_fe(x.path)}">${_fe(x.path)}</span></div>`).join("")
      : "";
    hint.textContent = j.exists ? `已另存在：${j.dir}` : "这首歌还没另存过分轨（点「另存一份」先分离 + 存好）";
  } catch (e) { hint.textContent = "状态没读到：" + e.message; }
}

async function stemSave() {
  const p = stemPath();
  if (!p) { toast("先选一首歌", "bad"); return; }
  $("#stemHint").textContent = "分离并另存中（第一次要跑分离，一两分钟）…";
  try {
    const j = await post("/api/fine/stems/save", { song: p });
    $("#stemHint").textContent = `已存到：${j.dir}`;
    toast("分轨已按中文文件夹存好");
    stemStatus();
  } catch (e) { toast("没存成：" + e.message, "bad"); $("#stemHint").textContent = ""; }
}

async function stemOpen() {
  const p = stemPath();
  if (!p) { toast("先选一首歌", "bad"); return; }
  try {
    const j = await api("/api/fine/stems?song=" + encodeURIComponent(p));
    if (!j.dir) return;
    await post("/api/open", { dir: j.dir });
  } catch (e) { toast("打不开：" + e.message, "bad"); }
}

async function stemClear() {
  const p = stemPath();
  if (!p) { toast("先选一首歌", "bad"); return; }
  try {
    await post("/api/fine/stems/clear", { song: p });
    toast("已删掉这份分轨（翻唱缓存没动，照样能跑）");
    stemStatus();
  } catch (e) { toast("没删掉：" + e.message, "bad"); }
}
