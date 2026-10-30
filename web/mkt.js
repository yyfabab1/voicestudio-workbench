/* 声音市场：搜别人训练好的音色模型，挑中哪个下哪个，下完直接变成能唱的角色。
   结构跟 lib.js（在线曲库）保持一致：搜索条 / 作品 chip / 卡片网格 / 已装列表。

   两句话必须写在界面上，不许含糊：
     ① 这里只搜 Hugging Face 上**公开可下载**的模型，不破解任何付费内容；
     ② 怎么用受它自己的 license 约束，商用前自己看。                        */

let MKT_BUSY = false;
let MKT_WORK = null;      // 当前正在浏览的作品（点 chip 之后）
// 翻页状态：一次搜索/浏览会在服务端建一个"结果池"，这里只记住池 id 和翻到哪了。
// ★ 不这么做的话，"加载更多"要么把整个列表重新搜一遍（慢、还可能跟上一页撞车），
//   要么干脆翻不动。池里存的是候选清单，文件清单只在真要显示那一页时才去问。
let MKT = {qid: null, off: 0, total: 0, shown: 0};
let MKT_ITEMS = [];       // 当前屏的卡片原始数据（展开某个合集的包列表时要按 repo 找回来）
// 排序 / 「小马宝莉 M6」筛选：纯前端 —— 不新增接口、也不改服务端顺序。
// 为什么要有：HF 上这些合集仓库的 likes/downloads 在卡片上是有数的，但列表顺序是
// 服务端按下载量排的；用户想"先看评价最高的"或"只看那六个主角"时无从下手。
let MKT_SORT = "default";
let MKT_M6 = false;
const MKT_M6_PAT =
  /twilight|rainbow|pinkie|rarity|applejack|fluttershy|紫悦|云宝|碧琪|珍奇|苹果嘉儿|柔柔/i;

async function mktInit() {
  mktTags();
  mktWorks();
  const sel = $("#mktSort");
  if (sel) {
    sel.onchange = () => {
      MKT_SORT = sel.value || "default";
      mktCtlHint();
      mktRerender();
    };
  }
  loadMarket();
}

/* 按标签逛：HF 上训音色的人大多会给仓库打 rvc / svc 这类标签，按标签一次能列出
   几百个仓库（还按下载量排好），比按关键词搜一次十来个宽得多 —— 想"找到更多"
   就从这个入口进。 */
async function mktTags() {
  const box = $("#mktTags");
  if (!box) return;
  try {
    const j = await api("/api/models/tags");
    box.innerHTML = '<span class="hint" style="margin-right:6px">按标签逛：</span>' +
      (j.tags || []).map(t =>
        `<button class="btn ghost sm" data-tag="${esc(t.key)}">${esc(t.name)}</button>`).join("");
    box.querySelectorAll("button[data-tag]").forEach(b => {
      b.onclick = () => mktBrowse(b.dataset.tag);
    });
  } catch (e) {
    box.innerHTML = "";
  }
}

async function mktBrowse(tag) {
  if (MKT_BUSY) return;
  MKT_BUSY = true;
  MKT_WORK = null;
  $$("#mktWorks button[data-work]").forEach(b => b.classList.remove("pri"));
  const box = $("#mktGrid"), note = $("#mktNote");
  box.innerHTML = '<div class="lib-spinner">正在按标签列出热门模型…</div>';
  if (note) note.textContent = "";
  $("#mktMore").innerHTML = "";
  try {
    const j = await api("/api/models/browse?tag=" + encodeURIComponent(tag) + "&limit=24");
    MKT = {qid: j.qid || null, off: j.offset || 0, total: j.total || 0, shown: 0};
    mktPaint(j.items || [], j.tag_name || tag, false);
    if (note) note.textContent = j.note || j.error || "";
    mktMoreBtn(j.has_more);
  } catch (e) {
    box.innerHTML = '<div class="lib-empty" style="color:var(--bad)">读不到：' + esc(e.message) + "</div>";
  } finally {
    MKT_BUSY = false;
  }
}

async function mktWorks(showAll) {
  const box = $("#mktWorks");
  if (!box) return;
  try {
    const j = await api("/api/models/works");
    const all = j.works || [];
    // 二十几部作品全平铺会把半个屏幕占满 ⇒ 默认只摆前 10 部，其余收在按钮里
    const vis = showAll ? all : all.slice(0, 10);
    // ★ 固定 chip 放在最前：这六个角色散落在好几个合集仓库里（therealvul / MusicBox27 /
    //   MTNProductions…），按作品 chip 一个个点太费事，而"只想看那六个主角"是最常见的诉求。
    box.innerHTML = `<button class="btn ghost sm" data-m6="1"
      title="只看小马宝莉六个主角（紫悦/云宝/碧琪/珍奇/苹果嘉儿/柔柔）">小马宝莉 M6</button>` +
      vis.map(w =>
      `<button class="btn ghost sm" data-work="${esc(w.key)}">${esc(w.name)}<span
        style="color:var(--txt3);font-size:11px"> · ${w.chars} 角色</span></button>`).join("") +
      (all.length > 10
        ? `<button class="btn ghost sm" data-works="${showAll ? "0" : "1"}">` +
          (showAll ? "收起 ▲" : "更多作品（还有 " + (all.length - 10) + " 部）▼") + "</button>"
        : "");
    box.querySelectorAll("button[data-work]").forEach(b => {
      b.onclick = () => mktWork(b.dataset.work);
    });
    box.querySelectorAll("button[data-works]").forEach(b => {
      b.onclick = () => mktWorks(b.dataset.works === "1");
    });
    box.querySelectorAll("button[data-m6]").forEach(b => {
      b.onclick = () => {
        MKT_M6 = !MKT_M6;
        mktCtlHint();
        mktRerender();
      };
    });
  } catch (e) {
    box.innerHTML = '<span class="hint">作品列表读不到：' + esc(e.message) + "</span>";
  }
}

/* 「加载更多」：带上服务端给的 qid 只翻下一页，不重新搜 */
async function mktMore() {
  const btn = $("#btnMktMore");
  if (!btn || btn._busy || !MKT.qid) return;
  btn._busy = true;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "加载中…";
  try {
    const j = await api("/api/models/page?qid=" + encodeURIComponent(MKT.qid) +
                        "&offset=" + MKT.off + "&limit=24");
    mktPaint(j.items || [], "", true);
    MKT.off = j.offset || MKT.off;
    mktMoreBtn(j.has_more);
  } catch (e) {
    $("#mktNote").innerHTML = '<span style="color:var(--bad)">翻页失败：' + esc(e.message) + "</span>";
    btn._busy = false; btn.disabled = false; btn.textContent = old;
  }
}

function mktMoreBtn(hasMore) {
  const box = $("#mktMore");
  if (!box) return;
  if (!hasMore || !MKT.qid) {
    box.innerHTML = MKT.shown
      ? '<span class="hint">到底了（共 ' + MKT.shown + " 个带权重文件的仓库）</span>" : "";
    return;
  }
  box.innerHTML = '<button class="btn" id="btnMktMore">加载更多（已显示 ' +
    MKT.shown + (MKT.total ? " / 候选 " + MKT.total : "") + "）</button>";
  const b = $("#btnMktMore");
  if (b) b.onclick = mktMore;
}

async function mktWork(key) {
  MKT_WORK = key;
  $$("#mktWorks button[data-work]").forEach(b =>
    b.classList.toggle("pri", b.dataset.work === key));
  const box = $("#mktGrid"), note = $("#mktNote");
  box.innerHTML = '<div class="lib-spinner">正在按作品搜（作品名 + 每个角色名都搜一遍，稍等）…</div>';
  $("#mktMore").innerHTML = "";
  MKT = {qid: null, off: 0, total: 0, shown: 0};
  if (note) note.textContent = "";
  try {
    const j = await api("/api/models/work?key=" + encodeURIComponent(key));
    mktPaint(j.items || [], j.work_name || key, false);
    if (note) {
      note.innerHTML = esc(j.note || "") +
        ((j.missing || []).length
          ? '<br>没搜到的角色：<span style="color:var(--txt3)">' +
            esc((j.missing || []).join("、")) + "</span>"
          : "");
    }
  } catch (e) {
    box.innerHTML = '<div class="lib-empty" style="color:var(--bad)">搜失败：' + esc(e.message) + "</div>";
  }
}

async function mktSearch() {
  const q = ($("#mktQ").value || "").trim();
  if (!q) { toast("先输入关键词，比如角色名或作品名", "bad"); return; }
  if (MKT_BUSY) return;
  MKT_BUSY = true;
  const btn = $("#btnMktSearch");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "搜索中…";
  MKT_WORK = null;
  $$("#mktWorks button[data-work]").forEach(b => b.classList.remove("pri"));
  $("#mktGrid").innerHTML = '<div class="lib-spinner">正在搜「' + esc(q) + '」…（要逐个仓库问有没有权重文件，稍慢）</div>';
  $("#mktMore").innerHTML = "";
  const note = $("#mktNote");
  if (note) note.textContent = "";
  try {
    const j = await api("/api/models/search?q=" + encodeURIComponent(q) + "&limit=24");
    MKT = {qid: j.qid || null, off: j.offset || 0, total: j.total || 0, shown: 0};
    mktPaint(j.items || [], q, false);
    if (note) note.textContent = j.note || "";
    mktMoreBtn(j.has_more);
  } catch (e) {
    $("#mktGrid").innerHTML = '<div class="lib-empty" style="color:var(--bad)">搜索失败：' + esc(e.message) + "</div>";
  } finally {
    MKT_BUSY = false;
    btn.disabled = false; btn.textContent = old;
  }
}

/* ---- 排序 / 筛选：只改这一屏的显示顺序与可见项，不碰服务端顺序 ---- */
function mktIsM6(it) {
  const hay = [it.repo, it.name, it.cn, it.char,
    (it.packs || []).map(p => (p.file || "") + " " + (p.cn || "")).join(" "),
    (it.files || []).join(" ")].join(" ").toLowerCase();
  return MKT_M6_PAT.test(hay);
}

function mktSortFilter(items) {
  let a = (items || []).slice();
  if (MKT_M6) a = a.filter(mktIsM6);
  if (MKT_SORT === "likes") a.sort((x, y) => (y.likes || 0) - (x.likes || 0));
  else if (MKT_SORT === "dl") a.sort((x, y) => (y.downloads || 0) - (x.downloads || 0));
  else if (MKT_SORT === "new") {
    a.sort((x, y) => String(y.lastModified || y.at || "")
      .localeCompare(String(x.lastModified || x.at || "")));
  } else if (MKT_SORT === "size") a.sort((x, y) => (x.total_mb || 0) - (y.total_mb || 0));
  // default = 服务端顺序，原样返回
  return a;
}

function mktCtlHint() {
  const el = $("#mktCtl");
  const parts = [];
  if (MKT_M6) parts.push("小马宝莉 M6");
  if (MKT_SORT !== "default") {
    parts.push({likes: "喜欢最多", dl: "下载最多", new: "最新", size: "体积最小"}[MKT_SORT] ||
               MKT_SORT);
  }
  if (el) el.textContent = parts.length ? (parts.join(" · ") + " · " + MKT.shown + " 个") : "";
  $$("#mktWorks button[data-m6]").forEach(b => b.classList.toggle("pri", MKT_M6));
}

/* 改排序/筛选时只重画当前这一屏（不发新请求、不丢已翻的页） */
function mktRerender() {
  const box = $("#mktGrid");
  if (!box) return;
  if (!MKT_ITEMS.length) { mktCtlHint(); return; }
  mktPaint(MKT_ITEMS.slice(), "", false);
}

function mktPaint(items, title, append) {
  const box = $("#mktGrid");
  const list = mktSortFilter(items);
  const html = list.map(it => mktCardHtml(it, false)).join("");
  if (append) {
    // 翻页是**追加**，不是替换：用户刚看完的那一屏不该被冲掉
    box.insertAdjacentHTML("beforeend", html);
    MKT.shown += list.length;
    MKT_ITEMS = MKT_ITEMS.concat(items);
  } else if (list.length) {
    box.innerHTML = html;
    MKT.shown = list.length;
    MKT_ITEMS = items.slice();
  } else if ((items || []).length) {
    // 有数据但被筛掉了：不许显示成"没搜到"（用户会以为库里没有这个模型）
    box.innerHTML = '<div class="lib-empty">当前筛选下没有结果（这一屏一共 ' +
      items.length + ' 个仓库）。再点一次「小马宝莉 M6」可以取消筛选。</div>';
    MKT.shown = 0;
    MKT_ITEMS = items.slice();
  } else {
    box.innerHTML = '<div class="lib-empty">没搜到「' + esc(title) +
      '」的公开模型。换个名字试试（英文角色名命中率高很多），或点上面的标签随便逛。</div>';
    MKT.shown = 0;
    MKT_ITEMS = [];
  }
  mktBind(box);
  mktCtlHint();
}

function mktBind(box) {
  box.querySelectorAll("button[data-install]").forEach(b => {
    b.onclick = () => mktInstall(b, b.dataset.install, b.dataset.file || "",
                                 b.dataset.name || "", b.dataset.all === "1");
  });
  // 「还有 N 个…」：把这一张卡片按完整包列表重画（其余卡片不动）
  box.querySelectorAll("button[data-expand]").forEach(b => {
    b.onclick = () => {
      const it = (MKT_ITEMS || []).find(x => x.repo === b.dataset.expand);
      if (!it) return;
      const card = Array.from(box.querySelectorAll(".lib-card"))
        .find(c => c.dataset.repo === it.repo);
      if (!card) return;
      card.outerHTML = mktCardHtml(it, true);
      mktBind(box);
    };
  });
}

function mktKindLabel(kinds) {
  const m = {rvc: "RVC 权重", sovits: "SoVITS 权重", pack: "压缩包（含多角色）",
             safetensors: "safetensors（要先转格式）"};
  return (kinds || []).map(k => m[k] || k).join(" / ");
}

/* 合集仓库里的每个包一行，想装哪个装哪个。
   ★ 以前只给「装全部 / 只装第一个」两个按钮 —— 用户要的是中间那个（比如合集中
   只想装 Twilight Sparkle），以前只能整包下（十几个 GB）或者拿第一个边角角色。
   包太多时先摆 8 个，其余收在「还有 N 个」里，别一张卡片占满整个屏幕。 */
function mktPacksHtml(it, limit) {
  const packs = it.packs || [];
  if (packs.length <= 1) return "";
  const show = limit ? Math.min(packs.length, 8) : packs.length;
  const rows = packs.slice(0, show).map(p =>
    `<div style="display:flex;align-items:center;gap:6px;padding:3px 0;
                 border-top:1px solid var(--line)">
       <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
                    white-space:nowrap;font-size:12px"
             title="${esc(p.file)}">${esc(p.cn || p.name || p.file)}</span>
       <button class="btn ghost sm" data-install="${esc(it.repo)}" data-all="0"
               data-file="${esc(p.file)}" data-name="${esc(p.cn || p.name || "")}">装这个</button>
     </div>`).join("");
  const more = packs.length > show
    ? `<button class="btn ghost sm" style="margin-top:4px"
         data-expand="${esc(it.repo)}">还有 ${packs.length - show} 个…</button>`
    : "";
  return '<div style="margin:6px 0 2px">' + rows + more + "</div>";
}

function mktCardHtml(it, expanded) {
  const files = (it.files || []).filter(f => !f.startsWith(".") && f !== "README.md");
  const packs = it.packs || [];
  const tags = [];
  if (it.char) tags.push("角色：" + it.char);
  // ★ 合集仓库要把"里面有几个角色"写在卡片上：小马宝莉/地狱客栈这类作品在 HF 上
  //   大多是"一个仓库躺十几个 zip"，不写清楚的话用户点一次只能拿到第一个。
  // 体积要说清楚：合集仓库可能几十个包、几个 GB，用户得自己决定下不下
  const mb = it.total_mb || 0;
  if (packs.length > 1) {
    tags.push("合集 · " + packs.length + " 个角色" +
      (mb ? " · 共 " + (mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb + " MB") : ""));
  }
  if (it.downloads) tags.push("下载 " + it.downloads);
  if (it.likes) tags.push("♥ " + it.likes);
  return `<div class="lib-card" data-repo="${esc(it.repo || "")}">
    <div class="t">${esc(it.cn || it.name)}${it.cn && it.cn !== it.name
      ? '<span style="color:var(--txt3);font-size:11px"> · ' + esc(it.name) + "</span>" : ""}</div>
    <div class="a">${esc(it.author || "")}${it.at ? " · " + esc(it.at) : ""}</div>
    <div class="tags">${tags.map(t => "<span>" + esc(t) + "</span>").join("")}</div>
    <div class="why">${esc(mktKindLabel(it.kinds))}<br>
      ${esc((files[0] || "").slice(0, 46))}${files.length > 1 ? " 等 " + files.length + " 个文件" : ""}</div>
    <div class="lic unknown">许可：${esc(it.license || "未标注")}</div>
    <div class="acts">
      ${packs.length > 1
        ? `<button class="btn pri" data-install="${esc(it.repo)}" data-all="1"
              data-n="${packs.length}" data-mb="${mb}"
              data-file="" data-name="">装全部 ${packs.length} 个</button>`
        : `<button class="btn pri" data-install="${esc(it.repo)}" data-all="0"
              data-file="${esc((packs[0] || {}).file || files[0] || "")}"
              data-name="${esc(it.char || "")}">安装</button>`}
    </div>
    ${mktPacksHtml(it, !expanded)}
  </div>`;
}

async function mktInstall(btn, repo, file, name, all) {
  if (btn._busy) return;
  // 整批装之前先说清代价（合集仓库可能 3 GB）：用户是"自己决定下载"的那个人
  if (all) {
    let n = Number(btn.dataset.n || 0), mb = Number(btn.dataset.mb || 0);
    // 搜索结果里不带体积（那要逐个包 HEAD），点"装全部"时再问一次，
    // 好把真实代价摆在确认框里。问不到就只报数量，不拦着用户。
    if (!mb) {
      try {
        const d = await api("/api/models/detail?repo=" + encodeURIComponent(repo));
        n = (d.packs || []).length || n;
        mb = d.total_mb || 0;
      } catch (e) { /* 问不到就按数量提示 */ }
    }
    const sz = mb >= 1024 ? (mb / 1024).toFixed(1) + " GB" : mb + " MB";
    if (!confirm("这个仓库里有 " + (n || "多") + " 个包" +
        (mb ? "，一共约 " + sz : "") +
        "。\n整批装会比较久（后台跑，有进度条），确定全装吗？\n\n" +
        "只想装某一个的话，展开卡片上的包列表，点那一个「装这个」。")) {
      return;
    }
  }
  btn._busy = true;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = all ? "⏳ 整批下载中…" : "⏳ 下载中…";
  const restore = () => { btn._busy = false; btn.disabled = false; btn.textContent = old; };
  try {
    const j = await post("/api/jobs", {kind: "install_model", repo: repo,
                                       file: file || null, name: name || null,
                                       all: !!all});
    const jid = j.job.id;
    watchJob(jid, async () => {
      let job = null;
      try { job = (await api("/api/job/" + jid)).job; } catch (e) { /* 拉不到就当失败 */ }
      restore();
      loadRoles();
      loadMarket();
      if (!job || job.state !== "done") {
        toast("安装失败：" + ((job && job.error) || "拿不到任务结果"), "bad");
        return;
      }
      const made = (job.result && job.result.installed) || [];
      toast(made.length ? ("装好了：" + made.map(m => m.name).join("、")) : "装完了但没建出角色",
            made.length ? "good" : "bad");
    });
  } catch (e) {
    restore();
    toast(e.message, "bad");
  }
}

async function loadMarket() {
  const box = $("#mktMine"), cnt = $("#mktCount");
  if (!box) return;
  try {
    const j = await api("/api/models/installed");
    const rows = j.installed || [];
    if (cnt) cnt.textContent = rows.length ? "（" + rows.length + "）" : "";
    if (!rows.length) {
      box.innerHTML = '<div class="hint">还没装过。上面搜一个，点「安装」就行。</div>';
      return;
    }
    box.innerHTML = rows.map(r => `<div class="lib-card">
      <div class="t">${esc(r.name || "")}</div>
      <div class="a">${esc(r.repo || "")}</div>
      <div class="tags"><span>${(r.rvc || {}).index ? "带索引库" : "无索引库"}</span>
        <span>${r.outputs || 0} 个成品</span></div>
      <div class="acts">
        <button class="btn" data-open="${esc(r.id)}">打开角色</button>
      </div>
    </div>`).join("");
    box.querySelectorAll("button[data-open]").forEach(b => {
      b.onclick = () => openRole(b.dataset.open, "paneSing");
    });
  } catch (e) {
    box.innerHTML = '<div class="hint">读不到已装列表：' + esc(e.message) + "</div>";
  }
}
