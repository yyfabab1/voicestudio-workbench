/* ---------------------------------------------------------- 在线曲库

搜索 → 看授权 → 一键导进翻唱台。

设计上的两条硬规矩：
1) 授权不是装饰。每个卡片都带判定结果，红的就是红的原因写清楚，
   绝不把"禁商用"的歌伪装成能用的。
2) 导歌是真的下载。点「导进翻唱台」会起一个后台下载任务，落到本地
   data/music 并量时长，成功后才填进②号槽位 —— 不塞一个半截文件进去。
*/

let LIB_BUSY = false;
/* 深挖翻页状态。要做"再挖一批"就得记住上次的档位/关键词，否则
   offset 算错会拉回同一批（界面看起来像"点了没反应"）。 */
const LIB_SEEN = new Set();
const LIB_STATE = { items: [], deep: 0, q: "", lic: "by" };
/* 丢失条目的标题。**必须声明在使用点之前** —— loadLibrary 是异步函数，
   之前放在文件末尾（210 行）而这里（157 行）就赋值，会撞 const 的TDZ：
   报ReferenceError，整张曲库都渲染不出来。 */
const LIB_LOST_TITLE = {};

function libFmtDur(sec) {
  if (!sec || sec <= 0) return "";
  const s = Math.round(sec);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

function libLicBadge(it) {
  const cls = it.status === "ok" ? "ok" : (it.status === "blocked" ? "blocked" : "unknown");
  const label = it.status === "ok" ? "✓ 可商用"
    : (it.status === "blocked" ? "⚠ 不可商用" : "? 授权不明");
  return `<span class="lic ${cls}">${label}${it.license ? " · " + esc(it.license.slice(0, 26)) : ""}</span>`;
}

function libCardHtml(it) {
  const blocked = it.status !== "ok";
  const tags = (it.tags || []).filter(Boolean).slice(0, 4)
    .map(t => `<span>${esc(t)}</span>`).join("");
  const dur = it.dur ? `<span>${libFmtDur(it.dur)}</span>` : "";
  const bpm = it.bpm ? `<span>${esc(it.bpm)} BPM</span>` : "";
  const page = it.page ? `<a href="${esc(it.page)}" target="_blank" rel="noopener">看原页</a>` : "";
  return `
    <div class="lib-card${blocked ? " blocked" : ""}" data-lib-id="${esc(it.id)}">
      <div class="t">${esc(it.title)}</div>
      <div class="a">${it.artist ? esc(it.artist) : "未署名"}${page ? " · " + page : ""}</div>
      <div>${libLicBadge(it)}</div>
      ${(tags || dur || bpm) ? `<div class="tags">${tags}${dur}${bpm}</div>` : ""}
      ${it.note ? `<div class="why">${esc(it.note)}</div>` : ""}
      <div class="acts">
        ${blocked
        ? `<button class="btn ghost" disabled>不可用</button>`
        : `<button class="btn pri" onclick="libFetch(this,'${esc(it.id)}','${esc(it.title)}')">导进翻唱台</button>`}
      </div>
    </div>`;
}

/* 深挖档位：单页上限 29 是站点 PHP 内存限制（limit>=30 返回 200+空body），
   所以"多要歌"只能靠翻页聚合。deep=页数额外层数。
   实测 deep=0→29 首、deep=4→145 首（约 17 秒）。 */
function libDeepPages() {
  const v = $("#libDeep") ? $("#libDeep").value : "2";
  return Math.max(0, Math.min(8, parseInt(v, 10) || 0));
}

async function libSearch(append) {
  const q = $("#libQ").value.trim();
  if (!q) { toast("先输入关键词，比如 piano 或 love", "bad"); return; }
  if (LIB_BUSY) return;
  LIB_BUSY = true;
  const btn = $("#btnLibSearch");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "搜索中…";
  const deep = libDeepPages();
  const expect = (deep + 1) * 29;
  $("#libGrid").innerHTML = append
    ? $("#libGrid").innerHTML
    : '<div class="lib-spinner">正在搜索「' + esc(q) + '」，最多拉取 ' + expect + ' 首…（深挖越多越慢）</div>';
  if (!append) $("#libSrcs").innerHTML = "";
  const lic = $("#libLic").value;
  const offset = append ? (LIB_SEEN.size ? 29 * (LIB_STATE.deep + 1) : 0) : 0;
  try {
    const j = await api("/api/music/search?limit=29&deep=" + deep + "&offset=" + offset +
      "&lic=" + encodeURIComponent(lic) + "&q=" + encodeURIComponent(q));
    // 源状态条：失败的源要显式说出来，否则"搜不到"会被人当成"没这歌"
    $("#libSrcs").innerHTML = (j.sources || []).map(s =>
      `<span class="lib-src ${s.ok ? "ok" : "bad"}" title="${esc(s.err || ("命中 " + s.n + " 首"))}">
         <i class="dot"></i>${esc(s.name)} · ${s.ok ? s.n + " 首" : esc((s.err || "").slice(0, 40))}
       </span>`).join("");

    const items = j.items || [];
    items.forEach(i => LIB_SEEN.add(i.id));
    LIB_STATE.deep = deep;
    LIB_STATE.q = q;
    LIB_STATE.lic = lic;

    const all = (append ? LIB_STATE.items : []).concat(items);
    LIB_STATE.items = all;
    if (!all.length) {
      $("#libGrid").innerHTML = '<div class="lib-empty">没搜到「' + esc(q) + '」。换个词试试（英文风格词命中率更高）。</div>';
    } else {
      const bad = all.filter(i => i.status !== "ok").length;
      const canMore = items.length >= 29 && deep < 8;
      $("#libGrid").innerHTML = all.map(libCardHtml).join("") +
        (canMore ? `<div style="text-align:center;margin-top:12px">
            <button class="btn ghost" onclick="libSearch(true)">↻ 再挖一批（已显示 ${all.length} 首）</button>
          </div>` : "") +
        (bad ? `<div class="lib-note">共 ${all.length} 首，其中 ${bad} 首授权不允许翻唱发布，已置灰。
                 放行规则：CC0 / 公共领域 / CC-BY / CC-BY-SA 可用；CC-BY-NC（禁商用）、CC-BY-ND（禁改编）不导。</div>` : "");
    }
  } catch (e) {
    $("#libGrid").innerHTML = '<div class="lib-empty" style="color:var(--bad)">搜索失败：' + esc(e.message) + "</div>";
  } finally {
    LIB_BUSY = false;
    btn.disabled = false; btn.textContent = old;
  }
}

/* 导歌：真下载 → 量时长 → 填进翻唱台②号槽 */
async function libFetch(btn, id, title) {
  if (btn.disabled || btn._busy) return;
  btn._busy = true;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ 下载中…";
  let jid = null;
  const restore = () => { btn._busy = false; btn.disabled = false; btn.textContent = old; };
  try {
    const j = await post("/api/music/fetch", { id: id, title: title });
    jid = j.job.id;
    // watchJob(id, onDone) 只回调一次且不传 job —— 所以完成后自己再拉一次拿 result
    watchJob(jid, async () => {
      let job = null;
      try { job = (await api("/api/job/" + jid)).job; } catch (e) { /* 拉不到就当失败 */ }
      if (!job || job.state !== "done") {
        restore();
        toast("导歌失败：" + ((job && job.error) || "拿不到任务结果"), "bad");
        return;
      }
      const song = job.result && job.result.path
        ? { path: job.result.path, title: (job.result.title) || title }
        : null;
      restore();
      loadLibrary();
      if (song) { stageSetSong(song.path, song.title); toast("已导进翻唱台 ②", "good"); }
      else { toast("歌已入库，但没拿到落盘路径，请从下方「已导入」里选", "bad"); }
    });
  } catch (e) {
    restore();
    toast(e.message, "bad");
  }
}

async function loadLibrary() {
  try {
    const j = await api("/api/music/library");
    const all = j.songs || [];
    // 文件不在原地的**照样列出来**，只是标红写明原因。
    // 以前是后端直接不返回这一条⇒ 歌从界面上凭空消失，用户不知道丢了什么。
    const alive = all.filter(s => !s.missing);
    const lost = all.filter(s => s.missing);
    for (const s of lost) LIB_LOST_TITLE[s.item || s.path] = s.title;
    $("#libCount").textContent = all.length ? "(" + all.length + ")" : "";
    // 客户端提示音：剪枝上线**之前**被扫进来并登记的老条目。它们还在曲库里
    // 显示成"本地已下载"，用户点试听只听到一声"叮"——所以要**当场点破**，
    // 并把"导进翻唱台"收掉（拿音效去翻唱是白费功夫），只留试听 + 移除。
    const card = (s) => {
      const prog = !!s.prog_sound;
      const tag = s.missing ? "文件丢失" : (prog ? "客户端提示音" : "本地已下载");
      return `
      <div class="lib-card ${s.missing ? "lost" : ""} ${prog ? "prog" : ""}">
        <div class="t">${esc(s.title)}</div>
        <div class="a">${libFmtDur(s.dur) || ""} ${s.size ? (s.size / 1048576).toFixed(1) + " MB" : ""}</div>
        <div class="tags"><span>${esc(s.src || "")}</span><span>${tag}</span></div>
        ${prog && !s.missing
          ? `<div class="progwhy">这是音乐客户端的<b>提示音</b>，不是歌曲
             —— 以前扫描没剪掉程序目录时混进来的。<br>「试听」听到的是一声音效，它没法拿去翻唱。</div>`
          : ""}
        ${s.missing
          ? `<div class="lostwhy">${esc(s.why || "文件已不在这个位置")}<br><span class="lp">${esc(s.path)}</span></div>`
          : ""}
        <div class="acts">
          ${s.missing
            ? `<button class="btn" data-act="relink" data-i="${esc(s.item || "")}">重新定位…</button>
               <button class="btn" data-act="forget" data-i="${esc(s.item || "")}" data-t="${esc(s.title)}">从曲库移除</button>`
            : prog
            ? `<button class="btn" data-act="preview" data-p="${esc(s.path)}">试听</button>
               <button class="btn" data-act="forget" data-i="${esc(s.item || "")}" data-t="${esc(s.title)}">从曲库移除</button>`
            : `<button class="btn" data-act="preview" data-p="${esc(s.path)}">试听</button>
               <button class="btn pri" data-act="use" data-p="${esc(s.path)}" data-t="${esc(s.title)}">导进翻唱台</button>`}
        </div>
      </div>`;
    };
    const progList = all.filter(s => s.prog_sound);
    const progBar = progList.length
      ? `<div class="lib-note prog-bar">曲库里有 <b>${progList.length}</b> 条是音乐客户端的
         <b>提示音</b>（不是歌）—— 扫描剪掉程序目录之前混进来的。
         它们不能拿去翻唱，留着只会占位、还会让你以为自己的歌少了。
         <button class="btn" id="libProgClean">清理这 ${progList.length} 条</button></div>`
      : "";
    $("#libMine").innerHTML = progBar + (all.length
      ? alive.map(card).join("")
        + (lost.length
          ? `<div class="lib-note" style="margin-top:10px">另有 <b>${lost.length}</b> 首文件已不在原位置
             （多半是被移动或删了）。条目保留在曲库里，可以逐个重新定位或移除。</div>`
             + lost.map(card).join("")
          : "")
      : '<div class="lib-empty">还没导入的歌。上面搜一首歌，导进来就会出现在这里。</div>');
    const pcb = $("#libProgClean");
    if (pcb) pcb.onclick = () => libCleanProg(progList);
    /* 事件在渲染完之后绑：路径/item 不能进内联 onclick 属性 —— Windows 路径
       里的 \ 会被 HTML 当转义符吃掉，"导进翻唱台"会拿到一条已经坏掉的路径
       （症状：点了没反应，后端只说"文件不存在"）。 */
    $("#libMine").querySelectorAll("button[data-act]").forEach(b => {
      const a = b.dataset.act;
      b.onclick = () => {
        if (a === "relink") libRelink(b.dataset.i);
        else if (a === "forget") libForget(b.dataset.i, b.dataset.t);
        else if (a === "preview") libPreview(b.dataset.p);
        else libUse(b.dataset.p, b.dataset.t);
      };
    });
  } catch (e) {
    $("#libMine").innerHTML = '<div class="lib-empty">曲库读取失败：' + esc(e.message) + "</div>";
  }
}

/* 文件丢了之后，用户手上多半还有这份歌（换了目录/换了盘）。
   让他指一个能打开的文件，把曲库指向新的位置 —— 而不是逼他重新下载。
   这里用 prompt 让用户粘路径，而不是走文件选择器：
   浏览器的 File 对象**没有真实磁盘路径**（已实测），拿它做重定位是假的。 */
function libRelink(item) {
  const p = prompt(
    "把《" + (LIB_LOST_TITLE[item] || "这首歌") + "》的新位置粘进来：\n" +
    "（在资源管理器里选中文件，地址栏或右键「复制为路径」就能拿到）",
    "");
  if (p == null) return;
  const np = p.trim().replace(/^"|"$/g, "");
  if (!np) return;
  post("/api/music/local/relink", { item: item, path: np })
    .then((j) => {
      if (!j.ok) { toast(j.err || "重新定位失败", "bad"); return; }
      toast(j.why || "已指向新位置", "good");
      loadLibrary();
    })
    .catch((e) => toast("重新定位失败：" + e.message, "bad"));
}

async function libForget(item, title) {
  if (!confirm(`把《${title}》从曲库移除？\n只删曲库里的这一条记录，不会动你的文件。`)) return;
  try {
    const j = await post("/api/music/local/forget", { item: item });
    if (!j.ok) { toast(j.err || "移除失败", "bad"); return; }
    toast("已从曲库移除", "good");
    loadLibrary();
  } catch (e) { toast("移除失败：" + e.message, "bad"); }
}

/* 批量清掉客户端提示音（只删曲库记录，绝不动磁盘上的文件）。
   逐条走已有的 forget 接口，不新开后端入口 —— 少一条路径就少一处能出错的地方。
   中途某条失败就跳过继续，最后如实报"清掉几条 / 共几条"。 */
async function libCleanProg(list) {
  if (!list || !list.length) return;
  if (!confirm(`从曲库移除这 ${list.length} 条客户端提示音？\n` +
               `只删曲库里的记录，你磁盘上的文件一个都不会动。`)) return;
  let ok = 0;
  for (const s of list) {
    try {
      const j = await post("/api/music/local/forget", { item: s.item });
      if (j.ok) ok++;
    } catch (e) { /* 单条失败不中断，最后一起报数 */ }
  }
  toast(ok === list.length ? `已清理 ${ok} 条提示音`
        : `清理 ${ok} / ${list.length} 条（其余失败，可再试一次）`,
        ok ? "good" : "bad");
  loadLibrary();
}

function libUse(path, title) { stageSetSong(path, title); toast("已放到翻唱台 ②", "good"); }

let LIB_AUDIO = null;
function libPreview(path) {
  if (!LIB_AUDIO) LIB_AUDIO = new Audio();
  if (LIB_AUDIO.dataset.p === path) { LIB_AUDIO.pause(); LIB_AUDIO.dataset.p = ""; return; }
  LIB_AUDIO.pause();
  LIB_AUDIO.src = media(path);
  LIB_AUDIO.dataset.p = path;
  LIB_AUDIO.play().catch(() => toast("试听失败，文件可能已被移动", "bad"));
}