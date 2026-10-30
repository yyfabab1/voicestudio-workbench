/* ---------------------------------------------------------- 本地音乐导入

把你自己机器上的歌导进曲库。跟在线源最大的不同：
  · 不用下载，只登记路径 —— 上百首歌导入几乎不占时间，也不吃额外硬盘
  · 保留原文件位置，所以后续还能做分轨编辑（人声/伴奏分轨）
  · 加密容器（.qmc/.mflac/.kugou 等）解不开，扫描时跳过并说明原因。

识别规则一律**先嗅魔数再定类型**，因为扩展名会骗人：
实测本机有 .m4a 其实是 AAC 裸流（真 m4a 第5-8字节是 'ftyp'，它是 ff f1）。
*/

let LOC_BUSY = false;
let LOC_SCAN = { items: [], skipped: [], root: "" };

function locFmtSize(b) {
  if (!b || b <= 0) return "";
  return b < 1048576 ? Math.round(b / 1024) + " KB"
                      : (b / 1048576).toFixed(1) + " MB";
}

function locFmtDur(sec) {
  if (!sec || sec <= 0) return "";
  const s = Math.round(sec);
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}

/* 候选目录：点一下就填进输入框。
   song_count 是后端真数出来的（只嗅魔数+查大小，不解码，很快）——
   没有它，用户点一个 0 首的目录等于白跑一趟，还会以为自己的音乐库坏了。 */
async function locLoadDirs() {
  try {
    const j = await api("/api/music/local/dirs");
    $("#locDirs").innerHTML = (j.dirs || []).map(d => {
      const dead = !d.exists;
      const n = d.song_count;
      // 三态：不存在 / 存在但没歌/ 有歌。0 首的目录照样可点（也许只是当前空），
      // 但要标出来，让人点之前就知道会扫出什么。
      const badge = dead ? "本机没有"
                : (n === 0 ? "没扫到歌" : (n != null ? n + " 首" : ""));
      const cls = dead ? "dead" : (n === 0 ? "empty" : "");
      return `<span class="loc-dir ${cls}"
        title="${esc(d.path)}\n${dead ? "本机没有这个目录" : "这个目录里扫到 " + (n == null ? "?" : n) + " 首可导入的音频"}"
        onclick="${d.exists ? `locPickDir('${esc(d.path.replace(/\\/g, "\\\\"))}')` : ""}">
        ${esc(d.name)}${badge ? `<i>${esc(badge)}</i>` : ""}</span>`;
    }).join("");
  } catch (e) { /* 拿不到就算了，不挡着用户手填路径 */ }
}

function locPickDir(p) {
  $("#locPath").value = p;
  locScan();
}

/* 扫描：只看有什么，不改曲库 —— 改曲库必须是用户的显式动作 */
async function locScan() {
  const d = $("#locPath").value.trim();
  if (!d) { toast("先填一个目录，比如你的音乐文件夹", "bad"); return; }
  if (LOC_BUSY) return;
  LOC_BUSY = true;
  const btn = $("#btnLocScan");
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "扫描中…";
  $("#locOut").innerHTML = '<div class="lib-spinner">正在扫描…</div>';
  try {
    const j = await api("/api/music/local/scan?dir=" + encodeURIComponent(d));
    if (!j.ok) throw new Error(j.err || "目录不存在");
    LOC_SCAN = { items: j.items || [], skipped: j.skipped || [], root: j.root };
    locRenderScan();
  } catch (e) {
    $("#locOut").innerHTML = '<div class="lib-empty" style="color:var(--bad)">扫描失败：'
      + esc(e.message) + "</div>";
  } finally {
    LOC_BUSY = false; btn.disabled = false; btn.textContent = old;
  }
}

function locRenderScan() {
  const { items, skipped, root } = LOC_SCAN;
  if (!items.length) {
    $("#locOut").innerHTML =
      '<div class="lib-empty">这个目录里没找到可用的音频。'
      + (skipped.length ? `（跳过 ${skipped.length} 个：多为加密缓存或损坏文件）` : "")
      + "</div>";
    return;
  }
  /* ★ 路径**不能**塞进内联 onclick 属性：Windows 路径里的 \ 会被 HTML/JS
     当成转义符吃掉（"D:\Music\x.flac" 传进去变成 "D:Musicx.flac"，
     后端只会回一句"文件不存在"，其实是我们自己把路径弄坏了）。
     所以路径走 data-p 属性，事件在 JS 里绑。 */
  const rows = items.slice(0, 300).map(it => {
    return `<div class="loc-row" data-p="${esc(it.path)}">
       <div class="nm" title="${esc(it.path)}">${esc(it.name)}</div>
       <div class="kd">${esc(it.ext.replace(".", "").toUpperCase())} · ${locFmtSize(it.size)}`
       + (it.dur ? ` · ${locFmtDur(it.dur)}` : "") + `</div>
       <button class="btn ghost" style="padding:4px 10px;font-size:12px"
         data-p="${esc(it.path)}" data-n="${esc(it.name || "")}"
         >用它翻唱</button>
     </div>`;
  }).join("");
  const more = items.length > 300 ? `<div class="loc-skip">另有 ${items.length - 300} 首未列出，导入时会一起导。</div>` : "";
  const skip = skipped.length
    ? `<div class="loc-skip">跳过 ${skipped.length} 个：<br>`
      + skipped.slice(0, 12).map(s => `${esc(s.name)} —— ${esc(s.why)}`).join("<br>")
      + (skipped.length > 12 ? `<br>…另有 ${skipped.length - 12} 个` : "") + "</div>"
    : "";
  $("#locOut").innerHTML =
    `<div class="lib-note" style="margin-top:0">在 <b>${esc(root)}</b> 找到 <b>${items.length}</b> 首歌。</div>`
    + rows + more + skip
    + `<div style="margin-top:12px;display:flex;gap:8px">
         <button class="btn pri" id="btnLocImportAll" onclick="locImportAll(this)">
           全部导入曲库（${items.length} 首）</button>
       <button class="btn ghost" onclick="locUseFirst()">用第一首翻唱</button>
       <button class="btn ghost" onclick="locImportFilePick()">添加单个文件…</button>
     </div>`;
  // 事件在整段渲染完之后统一绑（见上面的说明：路径不能进内联 onclick）
  $("#locOut").querySelectorAll("button[data-p]").forEach(b => {
    b.onclick = () => locUseOne(b, b.dataset.p, b.dataset.n);
  });
}

/* 整目录导入：走后台任务，因为目录大时扫描+登记要几秒 */
async function locImportAll(btn) {
  if (btn.disabled || btn._busy || !LOC_SCAN.items.length) return;
  const root = LOC_SCAN.root;
  const tip = `\n只登记路径，不会复制或移动你的文件。`;
  if (!confirm(`把「${root}」下的 ${LOC_SCAN.items.length} 首歌都登记进曲库？` + tip)) return;
  btn._busy = true;
  const old = btn.textContent;
  btn.disabled = true; btn.textContent = "导入中…";
  try {
    const j = await post("/api/music/local/add", { dir: root });
    const fin = await locWatch(j.job.id, btn, old);
    if (fin) loadLibrary();
  } catch (e) {
    btn._busy = false; btn.disabled = false; btn.textContent = old;
    toast("导入失败：" + e.message, "bad");
  }
}

function locWatch(id, btn, old) {
  return new Promise((resolve) => {
    watchJob(id, (job) => {
      if (job.state !== "done") {
        btn._busy = false; btn.disabled = false; btn.textContent = old;
        toast("导入失败：" + (job.error || "未知原因"), "bad");
        resolve(false);
        return true;
      }
      const n = (job.result && job.result.added && job.result.added.length) || 0;
      const bad = (job.result && job.result.bad && job.result.bad.length) || 0;
      btn._busy = false; btn.disabled = false; btn.textContent = old;
      toast(`已导入 ${n} 首` + (bad ? `（${bad} 个不合格被拒）` : ""), n ? "good" : "bad");
      resolve(n > 0);
    }, () => { btn._busy = false; btn.disabled = false; btn.textContent = old; resolve(false); });
  });
}

/* 单曲：直接进翻唱台②号槽，不进曲库也无所谓——它已经在你硬盘上了。 */
async function locUseOne(btn, path, name) {
  if (btn.disabled) return;
  stageSetSong(path, name);
  btn.textContent = "已就位 ✓";
  setTimeout(() => { btn.textContent = "用它翻唱"; }, 1400);
}

async function locUseFirst() {
  if (!LOC_SCAN.items.length) { toast("先扫出歌来", "bad"); return; }
  const it = LOC_SCAN.items[0];
  stageSetSong(it.path, it.name);
  toast("已填进翻唱台 ②号槽", "good");
}

/* 拖拽单个音频文件：浏览器拿不到真实路径，这是安全限制，
   所以这里不假装能用，而是让用户改用"添加单个文件"（走文件选择器）。 */
function locDragHint(on) {
  const el = $("#locDrop");
  if (el) el.classList.toggle("on", !!on);
}

/* 单文件拖入/选择：**真的能用**。
   之前这个按钮点了只会告诉用户"浏览器拿不到路径，做不到"—— 一个死路。

   现在的做法：浏览器的 File 拿不到磁盘路径（已实测，这是浏览器的安全限制），
   但**拿得到内容**。所以把内容 POST 给后端存进 data/music/ 再登记进曲库。
   注意这跟"整目录导入"是两种语义，代价不同，必须说清楚：
     · 整目录导入 = 只登记路径，不复制（省硬盘，原文件留在原地可分轨编辑）
     · 单文件拖入 = 复制一份到 data/music（你不必先知道它在哪个盘）
   */
function locImportFilePick() {
  const inp = document.createElement("input");
  inp.type = "file";
  inp.accept = "audio/*,.mp3,.flac,.wav,.m4a,.aac,.ogg,.opus,.wma,.ape,.aiff";
  inp.multiple = true;
  inp.onchange = () => locUploadFiles(Array.from(inp.files || []));
  inp.click();
}

async function locUploadFiles(files) {
  if (!files.length) return;
  const box = $("#locOut");
  let ok = 0; const bad = [];
  for (const f of files) {
    // toast 用 textContent + 固定 2.6s，传第 3、4 个参数不生效；详细过程写长提示条
    toast(`正在导入 ${f.name}…`);
    try {
      const r = await fetch("/api/music/local/upload?name=" + encodeURIComponent(f.name),
                            { method: "POST", body: f });
      const j = await r.json();
      if (r.ok && j.ok) { ok++; continue; }
      bad.push(f.name + "：" + (j.err || j.error || "未知原因"));
    } catch (e) {
      bad.push(f.name + "：" + e.message);
    }
  }
  if (box) {
    box.innerHTML = `<div class="lib-note" style="margin-top:8px">`
      + (ok ? `已导入 <b>${ok}</b> 个文件（复制了一份到 data/music）。<br>` : "")
      + (bad.length ? `没能导入 ${bad.length} 个：<br>` + bad.slice(0, 8).map(esc).join("<br>")
                      + (bad.length > 8 ? `<br>…另有 ${bad.length - 8} 个` : "") : "")
      + `<br>小提示：想<b>不复制文件</b>、直接登记硬盘上的原文件，就把所在文件夹路径
             粘进上面的输入框点「扫描」——那样只登记路径，原文件留在原地不动。</div>`;
  }
  toast(ok ? `已导入 ${ok} 个` : "都没导进来", ok ? "good" : "bad");
  if (ok) loadLibrary();
}

function locInit() {
  locLoadDirs();
  const dp = $("#locDrop");
  if (dp) {
    dp.onclick = () => locImportFilePick();
    dp.ondragover = (e) => { e.preventDefault(); locDragHint(true); };
    dp.ondragleave = () => locDragHint(false);
    /* 拖进来的文件直接上传入库 —— 以前 ondrop 只是重新弹一次文件选择器：
       用户明明已经把文件拖到手上了，还得再点一次、重新选一遍，多选还会丢。 */
    dp.ondrop = (e) => {
      e.preventDefault(); locDragHint(false);
      const files = Array.from(e.dataTransfer.files || []).filter(f => f.size > 0);
      if (files.length) locUploadFiles(files);
    };
  }
}

/* ============================================================ 选歌器
 *
 * 为什么单独做一个「从我的音乐里选」（角色内 / 翻唱台共用）：
 *   · 浏览器 <input type=file> **看不到**「我的音乐」里已经扫出来的歌 ——
 *     那些条目只是硬盘上的路径，没有一个文件选择入口能指向它们；
 *   这件事「我的音乐」面板里早就解决了，这里只是把**同一条**链路
 *   （scan_dir → add_files）接到角色内，绝不另写一套口径 ——
 *   否则同一首歌在两个地方会算出两种结果（以前已经栽过一次：
 *   候选目录 chips 写「5 首」、点进去扫出「9 首」，用户以为自己的歌丢了）。
 *
 * 与 LOC_* 分开存状态：选歌器可能开在「我的音乐」面板扫过之后，
 * 共用一份 SPK/LOC 状态会让两边互相把对方的列表顶掉。
 */
let SPK_CB = null;                        // 选中后的回调 (path, title)
let SPK_SCAN = { items: [], skipped: [], root: "" };
let SPK_BUSY = false;

function songPickOpen(onPick, opts) {
  const m = $("#maskSongPick");
  if (!m) return;
  SPK_CB = onPick;
  SPK_SCAN = { items: [], skipped: [], root: "" };
  openMask("maskSongPick");
  const body = $("#spkBody");
  if (body) body.innerHTML = '<div class="lib-spinner">正在读曲库…</div>';
  songPickDirs();
  songPickLib();
}

function songPickClose() {
  closeMask("maskSongPick");
  SPK_CB = null;
}

/* 候选目录：与「我的音乐」面板同一个接口，所以两处的"几首"必然一致 */
async function songPickDirs() {
  const box = $("#spkDirs");
  if (!box) return;
  try {
    const j = await api("/api/music/local/dirs");
    const dirs = j.dirs || [];
    box.innerHTML = dirs.map(d => {
      const dead = !d.exists;
      const n = d.song_count;
      const badge = dead ? "本机没有"
                  : (n === 0 ? "没扫到歌" : (n != null ? n + " 首" : ""));
      const cls = dead ? "dead" : (n === 0 ? "empty" : "");
      return `<span class="loc-dir ${cls}" data-spkdir="${esc(d.path)}"
        title="${esc(d.path)}">${esc(d.name)}${badge ? `<i>${esc(badge)}</i>` : ""}</span>`;
    }).join("");
    // 路径走 data- 属性、事件在 JS 里绑：Windows 路径里的 \ 塞进内联 onclick
    // 会被当成转义符吃掉（"D:\Music" → "D:Music"），后端只会说"不存在"。
    box.querySelectorAll("[data-spkdir]").forEach(el => {
      el.onclick = () => {
        const inp = $("#spkPath");
        if (inp) inp.value = el.dataset.spkdir;
        songPickScan();
      };
    });
    const inp = $("#spkPath");
    if (inp && !inp.value) {
      const best = dirs.find(d => d.exists && (d.song_count || 0) > 0);
      if (best) inp.value = best.path;
    }
  } catch (e) { /* 拿不到目录清单就算了，手填路径照样能扫 */ }
}

/* 曲库段：已经登记过的歌先摆出来 —— 大多数时候用户要的就是这里面的一首 */
async function songPickLib() {
  try {
    const j = await api("/api/music/library");
    // missing / 客户端提示音不列：选进来也唱不了，列出来只是骗他再点一次
    const songs = (j.songs || []).filter(s => s.path && !s.missing && !s.prog_sound);
    songPickRender(songs);
  } catch (e) {
    songPickRender([]);
  }
}

function spkRow(it, tag) {
  const nm = it.title || it.name || String(it.path || "").split(/[\\/]/).pop();
  const bits = [];
  if (it.dur) bits.push(locFmtDur(it.dur));
  if (it.size) bits.push(locFmtSize(it.size));
  if (it.missing) bits.push("文件已不在这个位置");
  return `<div class="spk-row${it.missing ? " dead" : ""}" data-p="${esc(it.path)}"
      data-n="${esc(nm)}">
    <div class="nm" title="${esc(it.path)}">${esc(nm)}
      ${tag ? `<b class="spk-tag">${esc(tag)}</b>` : ""}</div>
    <div class="kd">${esc(bits.join(" · "))}</div>
    <button class="btn sm ghost" type="button">用这首</button>
  </div>`;
}

function songPickRender(lib) {
  const body = $("#spkBody");
  if (!body) return;
  const { items, skipped, root } = SPK_SCAN;
  const libHtml = lib.length
    ? `<div class="spk-sec">曲库里已有的歌（${lib.length} 首）</div>`
      + lib.map(s => spkRow(s, "")).join("")
    : `<div class="spk-sec">曲库里还没有歌</div>
       <div class="hint">点上面的目录扫一下（或者把路径粘进输入框点「扫描」）——
       扫到的歌选一首就能直接用，不会复制你的文件。</div>`;

  let scanHtml = "";
  if (items.length) {
    scanHtml = `<div class="spk-sec">「${esc(root)}」里扫到 ${items.length} 首</div>`
      + items.slice(0, 300).map(it => spkRow(it, "")).join("")
      + (items.length > 300 ? `<div class="loc-skip">另有 ${items.length - 300} 首未列出</div>` : "");
  } else if (root) {
    scanHtml = `<div class="spk-sec">「${esc(root)}」</div>
      <div class="hint">这个目录里没扫到可用的音频${skipped.length ? `（跳过 ${skipped.length} 个：加密缓存或损坏文件）` : ""}。</div>`;
  }
  if (skipped.length) {
    scanHtml += `<div class="loc-skip">跳过 ${skipped.length} 个：<br>`
      + skipped.slice(0, 6).map(s => `${esc(s.name)} —— ${esc(s.why)}`).join("<br>")
      + (skipped.length > 6 ? `<br>…另有 ${skipped.length - 6} 个` : "") + "</div>";
  }
  body.innerHTML = libHtml + scanHtml;
  body.querySelectorAll(".spk-row").forEach(row => {
    const b = row.querySelector("button");
    if (!b) return;
    b.onclick = () => songPickUse(b, row.dataset.p, row.dataset.n);
  });
}

async function songPickScan() {
  const inp = $("#spkPath");
  const d = inp ? inp.value.trim() : "";
  if (!d) { toast("先填一个音乐目录，比如你的音乐文件夹", "bad"); return; }
  if (SPK_BUSY) return;
  SPK_BUSY = true;
  const btn = $("#btnSpkScan");
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "扫描中…"; }
  try {
    const j = await api("/api/music/local/scan?dur=1&dir=" + encodeURIComponent(d));
    if (j.error) throw new Error(j.error);
    SPK_SCAN = { items: j.items || [], skipped: j.skipped || [], root: j.root || d };
    // 扫描段要摆到曲库段后面，所以这里把曲库重新取一次（很快，本地文件）
    await songPickLib();
  } catch (e) {
    toast("扫描失败：" + e.message, "bad");
  } finally {
    SPK_BUSY = false;
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

/* 选中一首：把它的路径交给调用方（原文件留在原地，不复制、不改动）。 */
async function songPickUse(btn, path, name) {
  if (!path) return;
  const old = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "载入中…"; }
  try {
    if (SPK_CB) SPK_CB(path, name);
    songPickClose();
    toast("已载入：" + name, "good");
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = old; }
    toast("没用成：" + e.message, "bad");
  }
}

function songPickInit() {
  const m = $("#maskSongPick");
  if (!m) return;
  // 点遮罩空白处关闭：弹层是 grid 居中的，只有点在 mask 自身才算"点空白"
  m.addEventListener("click", (e) => { if (e.target === m) songPickClose(); });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && m.classList.contains("on")) songPickClose();
  });
}
songPickInit();
