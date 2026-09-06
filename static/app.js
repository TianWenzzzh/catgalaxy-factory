/* 喵星图工厂 · 控制台前端（原生 JS，无构建） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const state = { pid: null, school: "示例校", report: null, generated: null, draftCsv: "",
                merge: null, roster: null, mergeLoaded: false };

/* ---------- 提示 ---------- */
let toastTimer = null;
function toast(msg, kind = "") {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "on " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, 3200);
}

/* ---------- 步骤条 ---------- */
function setStep(n, done = []) {
  $$("#steps li").forEach((li) => {
    const s = +li.dataset.s;
    li.classList.toggle("on", s === n);
    li.classList.toggle("done", done.includes(s));
  });
}

/* ---------- API ---------- */
async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = `${r.status} ${r.statusText}`;
    try { const j = await r.json(); detail = j.detail || JSON.stringify(j); } catch (e) {}
    throw new Error(detail);
  }
  const ct = r.headers.get("content-type") || "";
  return ct.includes("json") ? r.json() : r.text();
}

/* ---------- 长任务进度 ----------
 * 分两段，因为「上传」和「服务端处理」是两个不同的量：
 *   ① 浏览器把字节推上去 —— 只有 XHR 有 upload.onprogress，fetch 没有，
 *      所以照片上传走 uploadWithProgress 而不是 api()。
 *   ② 服务端解压 + 压缩 + 落盘 —— 几十秒，浏览器完全看不见，只能轮询。
 * 第二段一开始报数就以它为准：它发生在第一段之后，把条子往回拨没有意义。
 */
const PROG_POLL_MS = 300;
let progTimer = null;
let progDismissTimer = null;
let progRunId = null;        // 锁定本轮任务，见 progAccept
let progPhase2 = false;

function uploadWithProgress(url, fd, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    if (onProgress) {
      xhr.upload.addEventListener("progress", (e) => {
        if (e.lengthComputable) onProgress(e.loaded, e.total);
      });
    }
    xhr.addEventListener("load", () => {
      const text = xhr.responseText;
      if (xhr.status >= 200 && xhr.status < 300) {
        try { resolve(JSON.parse(text)); } catch (e) { resolve(text); }
        return;
      }
      // 错误解析跟 api() 保持一致，调用方才能拿到后端那句人话（比如 413 的限额说明）
      let detail = `${xhr.status} ${xhr.statusText}`;
      try { const j = JSON.parse(text); if (j.detail) detail = j.detail; } catch (e) {}
      reject(new Error(detail));
    });
    xhr.addEventListener("error", () => reject(new Error("网络错误：连接中断，文件没传完")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.send(fd);
  });
}

const mb = (n) => (n / 1048576).toFixed(n < 10485760 ? 2 : 1);

function progReset() {
  clearTimeout(progTimer); clearTimeout(progDismissTimer);
  progTimer = null; progDismissTimer = null; progRunId = null; progPhase2 = false;
}

function progHide() {
  clearTimeout(progDismissTimer);
  progDismissTimer = null;
  $("#prog").hidden = true;
}

/**
 * @param hasUpload 有没有「浏览器往上传字节」这一段。生成星图没有，直接进第二段。
 */
function progBegin(pid, task, hasUpload) {
  progReset();
  const p = $("#prog");
  p.hidden = false;
  p.className = "prog indet";
  $("#progTask").textContent = task;
  $("#progPct").textContent = "";
  $("#progFill").style.width = "";
  if (hasUpload) {
    $("#progPhase").textContent = "① 上传";
    $("#progPhase").className = "progPhase";
    $("#progNote").textContent = "等待发送…";
  } else {
    progEnterPhase2(task);
  }
  progPoll(pid);
}

function progEnterPhase2(task) {
  progPhase2 = true;
  $("#progPhase").textContent = "② 服务端处理";
  $("#progPhase").className = "progPhase s2";
  if (task) $("#progTask").textContent = task;
  /* 交接的瞬间先落到「不确定态」，别把上传的 100% 挂在「② 服务端处理」的
     标签下面——那是个假状态：服务端此刻连总量都还不知道（zip 没打开）。
     注意这不消除数字上的回拨：两段量的是完全不同的东西，条子必然从 100%
     跳回 3%，靠的是同时变脸的阶段标签和文件名说明，不是靠进度条本身。
     实测这个不确定态活不过一个轮询周期，第一条记录就带着真实分数来了。 */
  $("#prog").classList.add("indet");
  $("#progFill").style.width = "";
  $("#progPct").textContent = "";
  $("#progNote").textContent = "服务端开始处理…";
}

function progUpload(loaded, total) {
  if (progPhase2) return;
  $("#prog").classList.remove("indet");
  $("#progFill").style.width = `${total ? (loaded / total * 100).toFixed(1) : 0}%`;
  $("#progNote").textContent = "正在上传…";
  $("#progPct").textContent = `${mb(loaded)} / ${mb(total)} MB`;
}

function progAccept(rec) {
  /* 轮询是在自己那个请求还没回来时就开始的，磁盘上很可能是上一轮留下的记录
     ——服务端崩过的话它会永远停在 running。所以第一次只认「正在跑的、
     且是刚起来的」那一条，然后用 run_id 把本轮锁死。 */
  if (!rec || !rec.run_id || rec.state === "idle") return null;
  if (progRunId === null) {
    if (rec.state !== "running") return null;
    if (rec.server_now - rec.started_at > 5) return null;
    progRunId = rec.run_id;
  } else if (rec.run_id !== progRunId) {
    return null;
  }
  return rec;
}

function progRender(r) {
  if (r.state === "done") { progDone(r.message || "完成"); return; }
  if (r.state === "failed") { progFail(r.message || "任务失败"); return; }
  if (!progPhase2) progEnterPhase2(r.task);
  const p = $("#prog");
  const quiet = r.server_now - r.updated_at;
  const stale = quiet > (r.stale_after || 20);
  p.classList.toggle("stale", stale);
  if (r.fraction === null || r.fraction === undefined) {
    p.classList.add("indet");            // 总量未知（zip 还没打开）：不编百分比
    $("#progFill").style.width = "";
    $("#progPct").textContent = r.done ? `已处理 ${r.done}` : "";
  } else {
    p.classList.remove("indet");
    $("#progFill").style.width = `${(r.fraction * 100).toFixed(1)}%`;
    $("#progPct").textContent =
      `${r.done}${r.total ? "/" + r.total : ""} · ${(r.fraction * 100).toFixed(0)}%`;
  }
  $("#progNote").textContent = stale
    ? `已 ${Math.round(quiet)} 秒没有进展，可能已中断——别刷新，刷新会真的打断它`
    : (r.note || r.task);
}

function progPoll(pid) {
  progTimer = setTimeout(async () => {
    let rec = null;
    try { rec = await api(`/api/projects/${pid}/progress`); } catch (e) { /* 轮询失败不算错 */ }
    const r = progAccept(rec);
    if (r) progRender(r);
    if (progTimer) progPoll(pid);        // progReset 把它清空后就自然停下
  }, PROG_POLL_MS);
}

function progDone(msg) {
  progReset();
  const p = $("#prog");
  p.hidden = false;
  p.className = "prog done";
  $("#progPhase").textContent = "✓ 完成";
  $("#progNote").textContent = msg;
  $("#progPct").textContent = "";
  $("#progFill").style.width = "100%";
  progDismissTimer = setTimeout(progHide, 2600);
}

function progFail(msg) {
  progReset();
  const p = $("#prog");
  p.hidden = false;
  p.className = "prog err";
  $("#progPhase").textContent = "✕ 失败";
  $("#progNote").textContent = msg;
  $("#progPct").textContent = "";
  progDismissTimer = setTimeout(progHide, 9000);   // 失败原因留久一点，让人看清
}

$("#progClose").addEventListener("click", () => {
  progReset(); progHide();
  toast("进度条已收起，后台任务仍在继续跑", "");
});

/* ---------- 1 建项目 ---------- */
$("#btnCreate").addEventListener("click", async () => {
  const school = $("#school").value.trim() || "示例校";
  try {
    const meta = await api("/api/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ school, subtitle: $("#subtitle").value.trim() }),
    });
    state.pid = meta.id; state.school = school;
    $("#pidHint").textContent = `项目已创建：${meta.id}`;
    $("#rosterState").textContent = "未上传"; $("#rosterState").className = "";
    $("#photoState").textContent = "未上传"; $("#photoState").className = "";
    $("#uploadNote").hidden = true; $("#uploadNote").textContent = "";
    $("#mapState").textContent = "自动生成"; $("#mapState").className = "";
    $("#report").innerHTML = '<p class="empty">还没有校验结果。上传名册与照片后点「运行校验」。</p>';
    state.report = null; state.generated = null;
    $("#btnPreview").disabled = true; $("#btnDownload").disabled = true;
    $("#frame").src = "about:blank"; $(".frameWrap").classList.remove("loaded");
    $("#genInfo").classList.remove("on");
    $("#mergeList").classList.remove("on"); $("#mergeList").innerHTML = "";
    $("#mergeState").textContent = "尚未扫描";
    $("#editList").classList.remove("on"); $("#editList").innerHTML = "";
    pending.clear(); updateApplyBtn();
    state.merge = null; state.roster = null; state.mergeLoaded = false;
    setStep(2, [1]);
    await refreshProjects();
    await refreshCalib();
    await loadTheme();
    toast(`项目「${school}」已创建`, "ok");
  } catch (e) { toast("创建失败：" + e.message, "err"); }
});

let projSearchTimer = null;
async function refreshProjects() {
  try {
    const q = $("#projSearch").value.trim();
    const j = await api(`/api/projects?limit=100&offset=0&q=${encodeURIComponent(q)}`);
    const list = j.items || [];
    const sel = $("#projList");
    const cur = state.pid || "";
    sel.innerHTML = '<option value="">— 打开已有项目 —</option>' +
      list.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.school)} · ${m.photo_count}照 · ${escapeHtml(m.updated_at)}</option>`).join("") +
      (j.has_more ? `<option value="" disabled>…另有 ${j.total - list.length} 个未列出，用搜索框缩小范围</option>` : "");
    if (list.some((m) => m.id === cur)) sel.value = cur;
    $("#projMore").textContent = q ? `匹配到 ${j.total} 个项目（列出前 ${list.length} 个）`
                                   : `共 ${j.total} 个项目（列出前 ${list.length} 个）`;
  } catch (e) {}
}
$("#projSearch").addEventListener("input", () => {
  clearTimeout(projSearchTimer);
  projSearchTimer = setTimeout(refreshProjects, 250);
});
$("#projList").addEventListener("change", async (e) => {
  const pid = e.target.value;
  if (!pid) return;
  try {
    const d = await api(`/api/projects/${pid}`);
    state.pid = pid; state.school = d.meta.school;
    $("#school").value = d.meta.school;
    $("#subtitle").value = d.meta.subtitle || "";
    $("#pidHint").textContent = `已打开：${pid}`;
    $("#rosterState").textContent = d.meta.has_roster ? "已上传" : "未上传";
    $("#rosterState").className = d.meta.has_roster ? "ok" : "";
    $("#photoState").textContent = `${d.photos.length} 张`;
    $("#photoState").className = d.photos.length ? "ok" : "";
    $("#mapState").textContent = d.has_map ? "已就绪" : "自动生成";
    if (d.report) { state.report = d.report; renderReport(d.report); }
    setStep(3, [1, 2]);
    await refreshCalib();
    await loadTheme();
    await scanMerge();
    toast("项目已载入", "ok");
  } catch (err) { toast("载入失败：" + err.message, "err"); }
});

/* ---------- 2 传数据 ---------- */
function bindDrop(dropSel, inputSel, stateSel, handler) {
  const drop = $(dropSel), input = $(inputSel);
  drop.addEventListener("click", () => input.click());
  ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
    e.preventDefault(); drop.classList.remove("over");
  }));
  drop.addEventListener("drop", (e) => {
    if (e.dataTransfer.files.length) handler(e.dataTransfer.files);
  });
  input.addEventListener("change", () => { if (input.files.length) handler(input.files); });
}

function needPid() {
  if (!state.pid) { toast("请先创建或打开一个项目", "err"); return false; }
  return true;
}

bindDrop("#dropRoster", "#rosterFile", "#rosterState", async (files) => {
  if (!needPid()) return;
  const fd = new FormData(); fd.append("file", files[0]);
  $("#rosterState").textContent = "上传中…";
  try {
    const r = await api(`/api/projects/${state.pid}/roster`, { method: "POST", body: fd });
    $("#rosterState").textContent = `${r.rows} 行 · ${r.encoding}`;
    $("#rosterState").className = r.rows ? "ok" : "err";
    if (r.missing_columns && r.missing_columns.length) {
      toast("缺列：" + r.missing_columns.join("、"), "err");
    } else { toast(`名册已导入 ${r.rows} 行`, "ok"); }
    state.report = r.report; renderReport(r.report);
    setStep(3, [1, 2]);
  } catch (e) { $("#rosterState").textContent = "失败"; $("#rosterState").className = "err"; toast("导入失败：" + e.message, "err"); }
});

let busy = false;      // 同一时刻只放一个长任务：服务端项目锁是串行的，
                       // 第二个请求会一直等到超时才回 409，不如在前端就拦住

bindDrop("#dropPhotos", "#photoFiles", "#photoState", async (files) => {
  if (!needPid()) return;
  if (busy) { toast("上一个任务还没结束，等它跑完再传", "err"); return; }
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  $("#photoState").textContent = `上传 ${files.length} 个…`;
  $("#uploadNote").hidden = true;
  busy = true;
  progBegin(state.pid, `上传 ${files.length} 个文件`, true);
  try {
    const r = await uploadWithProgress(`/api/projects/${state.pid}/photos`, fd, progUpload);
    const kb = (r.out_bytes / 1024).toFixed(0);
    $("#photoState").textContent = `${r.photos.length} 张 · ${kb}KB`;
    $("#photoState").className = "ok";

    const lim = r.limits || {};
    const notes = [];
    if (r.skipped && r.skipped.length) notes.push(`已跳过 ${r.skipped.length} 个：${r.skipped.slice(0, 6).join("、")}${r.skipped.length > 6 ? " …" : ""}`);
    (r.warnings || []).forEach((w) => notes.push(w));
    if (lim.project_room_left !== undefined) {
      notes.push(`本项目共 ${lim.project_total} 张照片，还能再传 ${lim.project_room_left} 张`
                 + `（累计上限 ${lim.max_files_per_project}，单次上限 ${lim.max_files_this_request}）`);
    }
    if (notes.length) {
      const n = $("#uploadNote");
      n.textContent = notes.join(" ｜ ");
      n.hidden = false;
    }
    progDone(`${r.saved} 张已压缩入库 · 长边≤${r.max_side || 1200}px · 共 ${kb}KB`);
    toast(`照片已压缩入库：${r.saved} 张（长边≤${r.max_side || 1200}px，共 ${kb}KB）`,
          notes.length ? "" : "ok");
    if (r.report) { state.report = r.report; renderReport(r.report); }
  } catch (e) {
    $("#photoState").textContent = "被拒";
    $("#photoState").className = "err";
    const n = $("#uploadNote");
    n.textContent = `${e.message}。本次上传已整体回滚，项目里不会留下半截照片——`
                  + `把 zip 拆小一点、或分几次传再试。`;
    n.hidden = false;
    progFail(e.message);
    toast("上传被拒：" + e.message, "err");
  } finally {
    busy = false;
  }
});

bindDrop("#dropMap", "#mapFile", "#mapState", async (files) => {
  if (!needPid()) return;
  const fd = new FormData(); fd.append("file", files[0]);
  try {
    const r = await api(`/api/projects/${state.pid}/map`, { method: "POST", body: fd });
    $("#mapState").textContent = `${r.size[0]}×${r.size[1]}`;
    $("#mapState").className = "ok";
    toast("底图已设置", "ok");
  } catch (e) { toast("底图失败：" + e.message, "err"); }
});

$("#btnValidate").addEventListener("click", async () => {
  if (!needPid()) return;
  try {
    const r = await api(`/api/projects/${state.pid}/validate`, { method: "POST" });
    state.report = r; renderReport(r);
    setStep(3, [1, 2]);
    await scanMerge();
    toast(r.summary.ok ? "校验通过，可以生成星图" : `校验发现 ${r.summary.error_count} 个错误`,
          r.summary.ok ? "ok" : "err");
  } catch (e) { toast("校验失败：" + e.message, "err"); }
});

/* ---------- 3 报告渲染 ---------- */
let filter = "all";
function renderReport(r) {
  const s = r.summary;
  const box = $("#report");
  const lvName = { error: "错误", warning: "警告", info: "提示" };
  let html = `
    <div class="verdict ${s.ok ? "ok" : "bad"}">${s.ok ? "✅ 校验通过 · 可生成星图" : "❌ 存在错误 · 需先修复名册"}</div>
    <div class="sumgrid">
      <div><b>${s.total_rows}</b><span>数据行</span></div>
      <div><b>${s.valid_rows}</b><span>可入图</span></div>
      <div><b>${s.error_count}</b><span>错误</span></div>
      <div><b>${s.warning_count}</b><span>警告</span></div>
      <div><b>${s.info_count}</b><span>提示</span></div>
      <div><b>${s.photos_uploaded}</b><span>照片</span></div>
      <div><b>${s.photos_referenced}</b><span>已引用</span></div>
      <div><b>${s.photos_unused}</b><span>未使用</span></div>
    </div>`;

  if (s.id_min) {
    html += `<p class="hint">编号范围 <b>${escapeHtml(s.id_min)} ~ ${escapeHtml(s.id_max)}</b>`;
    if (s.id_gaps.length) html += ` ｜ 空缺 ${s.id_gaps.length} 个（弃用允许）：${s.id_gaps.slice(0, 8).map(escapeHtml).join(", ")}${s.id_gaps.length > 8 ? " …" : ""}`;
    html += `</p>`;
  }
  if (s.low_confidence.length) {
    html += `<p class="hint">低置信度警告清单（${s.low_confidence.length}）：${s.low_confidence.map(escapeHtml).join(", ")}</p>`;
  }

  const counts = { all: r.issues.length, error: 0, warning: 0, info: 0 };
  r.issues.forEach((i) => { counts[i.level] = (counts[i.level] || 0) + 1; });
  html += `<div class="filters">
    ${["all", "error", "warning", "info"].map((k) =>
      `<button data-f="${k}" class="${filter === k ? "on" : ""}">${k === "all" ? "全部" : lvName[k]} ${counts[k] || 0}</button>`).join("")}
  </div>`;

  const list = r.issues.filter((i) => filter === "all" || i.level === filter);
  if (!list.length) {
    html += `<p class="empty">该级别下没有问题项 🎉</p>`;
  } else {
    html += `<table class="iss"><thead><tr><th>级别</th><th>代码</th><th>行</th><th>编号</th><th>说明</th></tr></thead><tbody>`;
    html += list.slice(0, 400).map((i) => `<tr>
      <td><span class="lv ${i.level}">${lvName[i.level]}</span></td>
      <td><code class="cd">${i.code}</code></td>
      <td>${i.line || ""}</td><td>${i.cat_id || ""}</td><td>${escapeHtml(i.message)}</td></tr>`).join("");
    if (list.length > 400) html += `<tr><td colspan="5" class="hint">…另有 ${list.length - 400} 条，见 zip 内《校验报告.md》</td></tr>`;
    html += `</tbody></table>`;
  }
  box.innerHTML = html;
  $$("#report .filters button").forEach((b) => b.addEventListener("click", () => {
    filter = b.dataset.f; renderReport(r);
  }));
}
function escapeHtml(s) {
  return String(s || "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------- 4 生成 + 预览 ---------- */
function currentForm() { return $('input[name=form]:checked').value; }

$("#btnGenerate").addEventListener("click", async () => {
  if (!needPid()) return;
  if (busy) { toast("上一个任务还没结束，等它跑完再生成", "err"); return; }
  const form = currentForm();
  busy = true;
  $("#btnGenerate").disabled = true;
  $("#btnGenerate").textContent = "生成中…";
  progBegin(state.pid, "生成星图", false);
  try {
    const r = await api(`/api/projects/${state.pid}/generate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ form, exclude_low_confidence: $("#excludeLow").checked }),
    });
    state.generated = r;
    const zones = Object.entries(r.stats.zones || {}).map(([k, v]) => `${escapeHtml(k)} ${v}`).join(" ｜ ");
    const coats = Object.entries(r.stats.coats || {}).map(([k, v]) => `${escapeHtml(k)} ${v}`).join(" ｜ ");
    $("#genInfo").classList.add("on");
    $("#genInfo").innerHTML = `
      <div><span class="k">形态</span> <b>${form === "inline" ? "纯文本内嵌版（base64）" : "相对路径版（HTML+assets）"}</b></div>
      <div><span class="k">入图</span> <b>${r.cats}</b> 只 · <span class="k">照片</span> <b>${r.photos_embedded}</b> 张 ·
           <span class="k">产物</span> <b>${(r.bundle_bytes / 1024 / 1024).toFixed(2)} MB</b> ·
           <span class="k">zip</span> <b>${(r.zip_bytes / 1024 / 1024).toFixed(2)} MB</b></div>
      <div><span class="k">分区</span> ${zones || "—"}</div>
      <div><span class="k">毛色</span> ${coats || "—"}</div>
      <div><span class="k">文件</span> ${escapeHtml(r.html_name)} + ${r.files.length - 1} 个附件</div>
      ${r.theme ? `<div><span class="k">主题</span> <b>${escapeHtml(labelOf((themeOpts || {}).presets || [], r.theme.preset))}</b>` +
        (r.theme.custom_colors ? ` · 自定义色 ${r.theme.custom_colors} 项` : "") +
        ` · 校徽 ${r.theme.logo ? `已内嵌 ${(r.theme.logo_bytes / 1024).toFixed(0)}KB` : "无"}` +
        (r.theme.signature ? ` · 署名「${escapeHtml(r.theme.signature)}」` : "") + `</div>` : ""}
      ${r.calib ? `<div><span class="k">星位</span> 人工标定 <b>${r.calib.manual}</b> / ${r.calib.total} 颗${r.calib.manual ? "" : "（未标定 → 星星只按分区聚拢，不对应真实地理位置，可用下方 F8 面板标定）"}</div>` : ""}
      ${r.missing_photos.length ? `<div style="color:var(--red)">缺照片 ${r.missing_photos.length} 张：${r.missing_photos.slice(0, 5).map(escapeHtml).join(", ")}</div>` : ""}
      <div><span class="k">包名</span> <b>${escapeHtml(r.zip_name)}</b></div>`;
    $("#btnPreview").disabled = false;
    $("#btnDownload").disabled = false;
    $("#zipHint").textContent = `已就绪：${r.zip_name}（${(r.zip_bytes / 1024 / 1024).toFixed(2)} MB）`;
    loadPreview(r.preview_url);
    setStep(5, [1, 2, 3, 4]);
    await refreshCalib();
    progDone(`${r.cats} 颗星 · 内嵌 ${r.photos_embedded} 张照片 · zip ${(r.zip_bytes / 1048576).toFixed(2)}MB`);
    toast(`星图已生成：${r.cats} 颗星`, "ok");
  } catch (e) {
    progFail(e.message);
    toast("生成失败：" + e.message, "err");
    setStep(3, [1, 2]);
  } finally {
    busy = false;
    $("#btnGenerate").disabled = false;
    $("#btnGenerate").textContent = "生成星图";
  }
});

function loadPreview(url) {
  const f = $("#frame");
  f.src = url;
  f.onload = () => $(".frameWrap").classList.add("loaded");
  $(".frameWrap").classList.add("loaded");
}
$("#btnPreview").addEventListener("click", async () => {
  if (!needPid()) return;
  try {
    const r = await api(`/api/projects/${state.pid}/preview?form=${currentForm()}`);
    loadPreview(r.url);
    toast("预览已载入 · 拖拽平移 / 滚轮缩放 / 点星看档案", "ok");
  } catch (e) { toast(e.message, "err"); }
});

/* ---------- 5 下载 ---------- */
$("#btnDownload").addEventListener("click", () => {
  if (!needPid()) return;
  const form = currentForm();
  window.location.href = `/api/projects/${state.pid}/download?form=${form}`;
  toast("开始下载 zip…", "ok");
});

/* ---------- F7 摘要 ---------- */
$("#btnSummary").addEventListener("click", async () => {
  if (!needPid()) return;
  try {
    const r = await api(`/api/projects/${state.pid}/summary`, { method: "POST" });
    const blob = new Blob([r.markdown], { type: "text/markdown;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `${state.school}-归并决策摘要.md`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast("归并决策摘要已生成并下载", "ok");
  } catch (e) { toast("生成失败：" + e.message, "err"); }
});

/* ---------- F6 普查解析 ---------- */
bindDrop("#dropCensus", "#censusFile", "#censusState", async (files) => {
  const fd = new FormData(); fd.append("file", files[0]); fd.append("as_csv", "false");
  $("#censusState").textContent = "解析中…";
  try {
    const r = await api("/api/census/parse", { method: "POST", body: fd });
    state.draftCsv = r.draft_csv;
    $("#censusState").textContent = `${r.count} 条`;
    $("#censusState").className = "ok";
    $("#btnCensusCsv").disabled = !r.count;
    const box = $("#censusOut");
    box.classList.add("on");
    box.innerHTML = `
      <h4>解析结果</h4>
      <div>记录 <b>${r.count}</b> 条 · 编码 ${r.encoding} · 警告 ${r.warnings.length} 条</div>
      ${r.warnings.length ? `<h4>警告 / 归并提示</h4><ul>${r.warnings.slice(0, 20).map((w) => `<li class="warn">${escapeHtml(w)}</li>`).join("")}${r.warnings.length > 20 ? `<li>…另有 ${r.warnings.length - 20} 条</li>` : ""}</ul>` : ""}
      ${r.suggestions && r.suggestions.length ? `<h4>疑似同猫归并候选组</h4><ul>${r.suggestions.slice(0, 12).map((s) =>
        `<li>${escapeHtml(s.coat)} @ ${escapeHtml(s.area)} · ${s.count} 张：${s.files.slice(0, 3).map(escapeHtml).join(", ")}${s.files.length > 3 ? " …" : ""}</li>`).join("")}</ul>` : ""}
      <h4>草稿前 5 行</h4>
      <div><code class="cd">${escapeHtml(r.draft_csv.split("\n").slice(0, 6).join("\n"))}</code></div>`;
    toast(`普查解析完成：${r.count} 条草稿行`, "ok");
  } catch (e) {
    $("#censusState").textContent = "失败"; $("#censusState").className = "err";
    toast("解析失败：" + e.message, "err");
  }
});
$("#btnCensus").addEventListener("click", () => $("#censusFile").click());
$("#btnCensusCsv").addEventListener("click", () => {
  if (!state.draftCsv) return;
  const blob = new Blob(["\ufeff" + state.draftCsv], { type: "text/csv;charset=utf-8" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "名册草稿-普查解析.csv";
  a.click();
  URL.revokeObjectURL(a.href);
  toast("草稿 CSV 已下载，可直接作为名册上传", "ok");
});

/* ---------- F8 星位标定 ---------- */
function calibApi() {
  const w = $("#frame").contentWindow;
  return (w && w.__catgalaxy) || null;
}

async function refreshCalib() {
  if (!state.pid) { $("#calibState").textContent = "尚未选择项目"; return; }
  try {
    const j = await api(`/api/projects/${state.pid}/calib`);
    state.calib = j;
    const s = j.stats || {};
    const bits = [`名册 ${s.total || 0} 颗星`,
                  `已人工标定 ${s.manual || 0} 颗`,
                  `算法推导 ${s.derived || 0} 颗`,
                  j.source === "manual" ? `来源：人工（${escapeHtml(j.updated_at || "")}）` : "来源：算法推导"];
    $("#calibState").innerHTML = bits.join(" ｜ ");
    $("#btnCalibSave").disabled = !(s.total > 0);
    $("#btnCalibClear").disabled = j.source !== "manual";
  } catch (e) {
    $("#calibState").textContent = "标定状态读取失败：" + e.message;
  }
}

$("#btnCalibMode").addEventListener("click", () => {
  const c = calibApi();
  if (!c) { toast("请先生成星图并载入预览，再开启标定模式", "err"); return; }
  const on = !c.info().calibMode;
  c.setCalibMode(on);
  $("#btnCalibMode").textContent = on ? "✎ 标定模式已开启（点击关闭）" : "✎ 在预览里开启标定模式";
  toast(on ? "标定模式已开启：在预览里把星星拖到它真实出没的位置" : "标定模式已关闭", "ok");
});

$("#btnCalibSave").addEventListener("click", async () => {
  if (!needPid()) return;
  const c = calibApi();
  if (!c) { toast("预览未载入，读不到星位。请先生成星图", "err"); return; }
  const onlyDragged = $("#calibOnlyDragged").checked;
  const positions = onlyDragged ? c.getUserCalib() : c.getCalib();
  const n = Object.keys(positions).length;
  if (!n) {
    toast(onlyDragged ? "你还没有拖动过任何星星" : "预览里没有星位可保存", "err");
    return;
  }
  try {
    const r = await api(`/api/projects/${state.pid}/calib`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions, note: onlyDragged ? "预览中人工拖动" : "固化当前全部星位" }),
    });
    toast(`已保存 ${r.saved} 颗星的标定（项目累计 ${r.total} 颗）· 记得重新生成星图`, "ok");
    if (r.ignored_unknown_ids && r.ignored_unknown_ids.length) {
      toast(`名册里不存在，已忽略：${r.ignored_unknown_ids.join(", ")}`, "err");
    }
    await refreshCalib();
  } catch (e) { toast("保存标定失败：" + e.message, "err"); }
});

$("#btnCalibClear").addEventListener("click", async () => {
  if (!needPid()) return;
  try {
    await api(`/api/projects/${state.pid}/calib`, { method: "DELETE" });
    const c = calibApi(); if (c) c.clearCalib();
    toast("项目标定已清除，回到算法推导坐标", "ok");
    await refreshCalib();
  } catch (e) { toast("清除失败：" + e.message, "err"); }
});

$("#btnCalibLocalClear").addEventListener("click", () => {
  const c = calibApi();
  if (!c) { toast("预览未载入", "err"); return; }
  c.clearCalib();
  toast("已清除预览的本机标定（项目里的 calib.json 未动）", "ok");
});

/* ---------- F9 归并工作台 ---------- */
const VERDICTS = [["same", "同一只猫"], ["different", "不是同一只"], ["unsure", "存疑待复核"]];
const verdictLabel = (v) => (VERDICTS.find((x) => x[0] === v) || [, v])[1];

async function scanMerge() {
  if (!state.pid) return;
  try {
    const j = await api(`/api/projects/${state.pid}/merge`);
    state.merge = j; state.mergeLoaded = true;
    const s = j.stats;
    $("#mergeState").innerHTML =
      `候选 <b>${s.groups}</b> 组 / ${s.members} 行 ｜ 已判定 <b>${s.decided}</b>` +
      `（同猫 ${s.same} · 不同 ${s.different} · 存疑 ${s.unsure}）｜ 待判定 ${s.pending}` +
      (s.stale_gids.length
        ? ` ｜ <span style="color:var(--gold)">名册已改动，${s.stale_gids.length} 条旧判定失效</span>` : "");
    renderMerge(j.groups);
  } catch (e) {
    state.mergeLoaded = false;
    $("#mergeState").textContent = "扫描失败：" + e.message;
  }
}

function renderMerge(groups) {
  const box = $("#mergeList");
  box.classList.add("on");
  if (!groups.length) {
    box.innerHTML = `<p class="empty">没有疑似重复建档的候选组 🎉</p>`;
    return;
  }
  box.innerHTML = groups.slice(0, 40).map((g) => {
    const d = g.decision;
    return `
    <div class="mgroup ${d ? "decided" : ""}" data-gid="${escapeHtml(g.gid)}">
      <div class="mgHead">
        <b>${g.kind === "same-photo" ? "共用照片" : "同色同区"}</b>
        <span class="score">可疑度 ${(g.score * 100).toFixed(0)}%</span>
        <code class="cd">${escapeHtml(g.gid)}</code>
        ${d ? `<span class="lv info">已判定：${verdictLabel(d.verdict)}</span>` : ""}
      </div>
      <div class="mgWhy">${escapeHtml(g.reason)}</div>
      <div class="mgCards">${g.members.map((m) => `
        <div class="mcard">
          ${m.photo_url
            ? `<img src="${escapeHtml(m.photo_url)}" alt="${escapeHtml(m.name || m.id)}" loading="lazy">`
            : `<div class="nophoto">照片未上传<br><small>${escapeHtml(m.photo || "名册未填")}</small></div>`}
          <div class="mcBody">
            <b>${escapeHtml(m.name || "(未命名)")}</b><code class="cd">${escapeHtml(m.id)}</code>
            <div>${escapeHtml(m.coat || "毛色未记")} ｜ ${escapeHtml(m.area || "区域未填")}</div>
            <div class="feat">${escapeHtml(m.features || "无特征描述")}</div>
            <div class="meta">置信度 ${escapeHtml(m.confidence || "—")} ｜ 照片 ${m.photo_count || 0} 张 ｜ 第 ${m.line} 行</div>
            <label class="pick"><input type="radio" name="keep-${escapeHtml(g.gid)}" value="${escapeHtml(m.id)}"
              ${d && d.keep === m.id ? "checked" : ""}> 判同猫时保留这只</label>
            <label class="pick"><input type="checkbox" name="drop-${escapeHtml(g.gid)}" value="${escapeHtml(m.id)}"
              ${d && (d.drop || []).includes(m.id) ? "checked" : ""}> 弃用此编号</label>
          </div>
        </div>`).join("")}</div>
      <div class="mgJudge">
        ${VERDICTS.map(([v, l]) =>
          `<button data-v="${v}" class="${d && d.verdict === v ? "on" : ""}">${l}</button>`).join("")}
        <input class="reason" placeholder="理由（判同猫/不同猫必填，会写进归并决策摘要）"
               value="${escapeHtml(d ? d.reason : "")}">
        <button class="primary save">保存判定</button>
        ${d ? `<button class="undo">撤销</button>` : ""}
      </div>
    </div>`;
  }).join("") + (groups.length > 40 ? `<p class="hint">…另有 ${groups.length - 40} 组未显示</p>` : "");

  box.querySelectorAll(".mgroup").forEach((el) => {
    const gid = el.dataset.gid;
    const vbtns = [...el.querySelectorAll(".mgJudge button[data-v]")];
    vbtns.forEach((b) => b.addEventListener("click", () => {
      vbtns.forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
    }));
    el.querySelector(".save").addEventListener("click", async () => {
      const on = el.querySelector(".mgJudge button[data-v].on");
      if (!on) { toast("先选一个判定结论", "err"); return; }
      const verdict = on.dataset.v;
      const keepEl = el.querySelector(`input[name="keep-${gid}"]:checked`);
      const body = {
        gid, verdict,
        keep: keepEl ? keepEl.value : "",
        drop: [...el.querySelectorAll(`input[name="drop-${gid}"]:checked`)].map((x) => x.value),
        reason: el.querySelector(".reason").value.trim(),
      };
      try {
        await api(`/api/projects/${state.pid}/merge`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        toast(`已记录：${verdictLabel(verdict)}` +
              `${body.keep ? ` · 保留 ${body.keep}` : ""}` +
              `${body.drop.length ? ` · 弃用 ${body.drop.join(",")}` : ""}`, "ok");
        await scanMerge();
      } catch (e) { toast("判定被拒：" + e.message, "err"); }
    });
    const undo = el.querySelector(".undo");
    if (undo) undo.addEventListener("click", async () => {
      try {
        await api(`/api/projects/${state.pid}/merge/${encodeURIComponent(gid)}`,
                  { method: "DELETE" });
        toast("已撤销这条判定", "ok");
        await scanMerge();
      } catch (e) { toast("撤销失败：" + e.message, "err"); }
    });
  });
}

$("#btnMergeScan").addEventListener("click", async () => {
  if (!needPid()) return;
  $("#mergeState").textContent = "扫描中…";
  await scanMerge();
});

/* ---------- F10 名册在线编辑 ---------- */
const pending = new Map();          // `${line}|${field}` → {line, field, value}

function updateApplyBtn() {
  $("#btnApplyEdits").textContent =
    pending.size ? `应用 ${pending.size} 处改动并重跑校验` : "应用改动并重跑校验";
  $("#btnApplyEdits").disabled = !pending.size;
  $("#btnClearEdits").disabled = !pending.size;
}

function renderEditList(j) {
  const rep = state.report || j.report;
  const issues = (rep && rep.issues) || [];
  const bad = new Map();
  issues.forEach((i) => {
    if (!i.line) return;
    if (!bad.has(i.line)) bad.set(i.line, []);
    bad.get(i.line).push(i);
  });
  const lines = [...bad.keys()].sort((a, b) => a - b).slice(0, 60);
  const box = $("#editList");
  box.classList.add("on");
  if (!lines.length) {
    box.innerHTML = `<p class="empty">没有带行号的问题项 —— 名册干净，无需在线修改。</p>`;
    return;
  }
  box.innerHTML = lines.map((ln) => {
    const row = j.rows[ln - j.line_offset] || [];
    const its = bad.get(ln);
    const hit = [...new Set(its.map((i) => i.field).filter((f) => f && j.mapping[f] !== undefined))];
    const fields = hit.length ? hit : j.editable_columns;
    return `<div class="editRow">
      <div class="erHead">第 ${ln} 行 · <code class="cd">${escapeHtml(row[0] || "(编号空)")}</code>
        ${its.map((i) => `<span class="lv ${i.level}">${i.code}</span>`).join(" ")}
        <button type="button" class="delRow" data-del="${ln}"
                title="只删名册里这一行，照片不动（会变成「未使用照片」提示）">✕ 删这行</button></div>
      <div class="erMsg">${its.map((i) => escapeHtml(i.message)).join("<br>")}</div>
      ${fields.map((f) => {
        const cur = row[j.mapping[f]];
        return `<label class="erField">${escapeHtml(f)}
          <input data-line="${ln}" data-field="${escapeHtml(f)}" value="${escapeHtml(cur === undefined ? "" : cur)}">
        </label>`;
      }).join("")}
    </div>`;
  }).join("") + (bad.size > 60 ? `<p class="hint">…另有 ${bad.size - 60} 行有问题，先改前 60 行</p>` : "");

  box.querySelectorAll("input").forEach((inp) => inp.addEventListener("input", () => {
    pending.set(`${inp.dataset.line}|${inp.dataset.field}`,
                { line: +inp.dataset.line, field: inp.dataset.field, value: inp.value });
    inp.classList.add("dirty");
    updateApplyBtn();
  }));

  /* 删一行会让它下面所有行的物理行号往上挪一格，待改里记的行号立刻全部作废，
     所以删完必须丢掉 pending 并向服务端重读名册，不能拿本地这份旧行号继续用。 */
  box.querySelectorAll("[data-del]").forEach((btn) => btn.addEventListener("click", async () => {
    const ln = +btn.dataset.del;
    const row = j.rows[ln - j.line_offset] || [];
    const who = (row[0] || "").trim() || "(编号空)";
    if (!confirm(`删除第 ${ln} 行（编号 ${who}）？\n\n只删名册里这一行；照片文件不动，` +
                 `之后会以「未使用照片」提示出现在报告里。`)) return;
    btn.disabled = true;
    try { await deleteRosterRow(ln, who); }
    catch (e) { btn.disabled = false; toast("删除失败：" + e.message, "err"); }
  }));
}

/* 两个删除入口（问题行右侧的「✕ 删这行」、工具条上按编号挑的「－ 删一行」）
   共用这一趟往返：删完名册行号全变，报告、编辑列表、F8 标定、F9 归并都得跟着刷新。 */
async function deleteRosterRow(ln, who) {
  const r = await api(`/api/projects/${state.pid}/roster/rows/${ln}`, { method: "DELETE" });
  pending.clear(); updateApplyBtn();
  if (r.report) { state.report = r.report; renderReport(r.report); }
  await reloadRoster();
  const unused = r.report
    ? r.report.issues.filter((i) => i.code === "I_PHOTO_UNUSED").length : 0;
  toast(`已删除第 ${ln} 行（${who}），错误剩 ${r.report ? r.report.summary.error_count : "—"}` +
        (unused ? ` ｜ 未使用照片 ${unused} 张（文件没删，要不要清由你定）` : ""), "ok");
  await refreshCalib();
  if (state.mergeLoaded) await scanMerge();
  return r;
}

$("#btnRosterEdit").addEventListener("click", async () => {
  if (!needPid()) return;
  try {
    const j = await api(`/api/projects/${state.pid}/roster`);
    state.roster = j;
    renderEditList(j);
    toast(`名册已载入：${j.rows.length} 行 · ${j.encoding}`, "ok");
  } catch (e) { toast("读取名册失败：" + e.message, "err"); }
});

$("#btnApplyEdits").addEventListener("click", async () => {
  if (!needPid() || !pending.size) return;
  try {
    const r = await api(`/api/projects/${state.pid}/roster`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ edits: [...pending.values()] }),
    });
    pending.clear(); updateApplyBtn();
    if (r.report) { state.report = r.report; renderReport(r.report); }
    await reloadRoster();          // 重读名册：摊开的行得显示改完的值，不是改前那份快照
    const rej = r.rejected || [];
    toast(`已应用 ${r.applied.length} 处改动，错误剩 ${r.report ? r.report.summary.error_count : "—"}` +
          (rej.length ? ` ｜ ${rej.length} 处被拒：${rej[0].why}` : ""), rej.length ? "err" : "ok");
    await refreshCalib();
    if (state.mergeLoaded) await scanMerge();
  } catch (e) { toast("修改失败：" + e.message, "err"); }
});

$("#btnClearEdits").addEventListener("click", () => {
  pending.clear(); updateApplyBtn();
  $$("#editList input").forEach((i) => i.classList.remove("dirty"));
  toast("已清空待改（名册文件未动）", "");
});

/* ---------- F10 增行 / 删行 ---------- */

async function reloadRoster() {
  const j = await api(`/api/projects/${state.pid}/roster`);
  state.roster = j;
  renderEditList(j);
  return j;
}

function openAddRow() {
  const j = state.roster;
  const last = j.rows.length + 1;              // 物理行数 = 数据行 + 表头（第 1 行）
  const after = $("#addRowAfter");
  after.max = String(last);
  after.value = String(last);                  // 默认追加到末尾，这是最常做的动作
  $("#addRowFields").innerHTML = j.editable_columns.map((f) =>
    `<label class="erField">${escapeHtml(f)}<input data-new="${escapeHtml(f)}" value=""></label>`).join("");
  $("#addRowState").textContent =
    `名册共 ${last} 行（表头是第 1 行），可插在 1~${last} 之后` +
    (j.missing_columns && j.missing_columns.length ? `；名册缺列：${j.missing_columns.join("、")}` : "");
  $("#addRowState").className = "hint";
  $("#addRowBox").hidden = false;
  $("#btnAddRow").disabled = true;
}

function closeAddRow() {
  $("#addRowBox").hidden = true;
  $("#btnAddRow").disabled = false;
  $("#addRowState").textContent = "";
}

$("#btnAddRow").addEventListener("click", async () => {
  if (!needPid()) return;
  if (!state.roster) {
    try { await reloadRoster(); }
    catch (e) { toast("加不了：" + e.message, "err"); return; }
  }
  openAddRow();
});

$("#btnAddRowCancel").addEventListener("click", () => {
  $$("#addRowFields input").forEach((i) => { i.value = ""; });
  closeAddRow();
});

$("#btnAddRowGo").addEventListener("click", async () => {
  if (!needPid() || !state.roster) return;
  const after = parseInt($("#addRowAfter").value, 10);
  if (!Number.isFinite(after) || after < 1) {
    toast("「插在第几行之后」要填一个 ≥1 的行号（表头是第 1 行）", "err"); return;
  }
  /* 空着的列根本不发出去：编号留空就该留空，让校验报「编号为空」由人来定号，
     前端替用户编一个号会让「弃用编号不复用」这条数据红线悄悄失效。 */
  const values = {};
  $$("#addRowFields input[data-new]").forEach((inp) => {
    const v = inp.value.trim();
    if (v) values[inp.dataset.new] = v;
  });
  if (!Object.keys(values).length) {
    toast("一行都没填 —— 至少给「昵称」「毛色」「代表照片文件」一个值", "err"); return;
  }
  $("#btnAddRowGo").disabled = true;
  $("#addRowState").textContent = "插入中…";
  try {
    const r = await api(`/api/projects/${state.pid}/roster/rows`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ after, values }),
    });
    pending.clear(); updateApplyBtn();
    if (r.report) { state.report = r.report; renderReport(r.report); }
    const j = await reloadRoster();
    const idIdx = j.mapping["编号"];
    const who = idIdx === undefined ? "" : String(r.row[idIdx] || "").trim();
    openAddRow();                              // 表单留着，方便连着加下一只
    const ign = r.ignored || [];
    $("#addRowState").textContent = ign.length
      ? `已插入第 ${r.line} 行；${ign.length} 列名册里没有，被忽略：${ign.map((x) => x.field).join("、")}`
      : `已插入第 ${r.line} 行${who ? `（编号 ${who}）` : "（编号未填，校验会报错）"}`;
    $("#addRowState").className = "hint " + (ign.length ? "err" : "ok");
    toast(`新猫已加在第 ${r.line} 行` +
          `，错误剩 ${r.report ? r.report.summary.error_count : "—"}`, "ok");
    await refreshCalib();
    if (state.mergeLoaded) await scanMerge();
  } catch (e) {
    $("#addRowState").textContent = "插入失败：" + e.message;
    $("#addRowState").className = "hint err";
    toast("插入失败：" + e.message, "err");
  } finally {
    $("#btnAddRowGo").disabled = false;
  }
});

/* 名册干净时 renderEditList 一个 data-del 都不渲染，所以还得有个不挑行的删除入口：
   最常删的恰恰是「没毛病但这只猫不再出现了」的行。选项列全部数据行，按编号认猫。 */
function openDelRow() {
  const j = state.roster;
  const idIdx = j.mapping["编号"], nameIdx = j.mapping["昵称"];
  $("#delRowPick").innerHTML = j.rows.map((row, i) => {
    const ln = i + j.line_offset;                  // 物理行号 = 数据行下标 + line_offset
    const id = idIdx === undefined ? "" : String(row[idIdx] || "").trim();
    const nm = nameIdx === undefined ? "" : String(row[nameIdx] || "").trim();
    return `<option value="${ln}">第 ${ln} 行 · ${escapeHtml(id || "(编号空)")} ${escapeHtml(nm)}</option>`;
  }).join("");
  $("#delRowState").textContent =
    `名册共 ${j.rows.length} 行数据（表头是第 1 行，不可删）`;
  $("#delRowState").className = "hint";
  $("#delRowBox").hidden = false;
  $("#btnDelRow").disabled = true;
}

function closeDelRow() {
  $("#delRowBox").hidden = true;
  $("#btnDelRow").disabled = false;
}

$("#btnDelRow").addEventListener("click", async () => {
  if (!needPid()) return;
  if (!state.roster) {
    try { await reloadRoster(); }
    catch (e) { toast("删不了：" + e.message, "err"); return; }
  }
  if (!state.roster.rows.length) { toast("名册里没有数据行可删", "err"); return; }
  openDelRow();
});

$("#btnDelRowCancel").addEventListener("click", closeDelRow);

$("#btnDelRowGo").addEventListener("click", async () => {
  if (!needPid() || !state.roster) return;
  const sel = $("#delRowPick");
  const ln = parseInt(sel.value, 10);
  if (!Number.isFinite(ln) || ln < 2) {
    toast("请挑一个数据行（表头是第 1 行，不能删）", "err"); return;
  }
  const label = sel.options[sel.selectedIndex].textContent.trim();
  if (!confirm(`删除「${label}」？\n\n只删名册里这一行；照片文件不动，` +
               `之后会以「未使用照片」提示出现在报告里。`)) return;
  /* toast 里已经带了「第 N 行」，这里只给认猫用的编号/昵称；直接拿选项文字，
     行号会在同一句话里出现两遍。 */
  const row = state.roster.rows[ln - state.roster.line_offset] || [];
  const at = (f) => { const i = state.roster.mapping[f]; return i === undefined ? "" : String(row[i] || "").trim(); };
  const who = at("编号") || at("昵称") || "(编号空)";
  $("#btnDelRowGo").disabled = true;
  $("#delRowState").textContent = "删除中…";
  try {
    await deleteRosterRow(ln, who);
    closeDelRow();
    if (state.roster.rows.length) openDelRow();     // 还有行就把下拉留着，方便连着删
    $("#delRowState").textContent =
      `已删除「${who}」，名册余 ${state.roster.rows.length} 行数据`;
    $("#delRowState").className = "hint ok";
  } catch (e) {
    $("#delRowState").textContent = "删除失败：" + e.message;
    $("#delRowState").className = "hint err";
    toast("删除失败：" + e.message, "err");
  } finally {
    $("#btnDelRowGo").disabled = false;
  }
});

/* ---------- F11 星图主题 ---------- */
let themeOpts = null;            // /api/theme/options 是全局的，拉一次就够
let themeCur = null;             // 最近一次服务端回的主题快照
let sigPending = null;           // 还没发出去的署名，见 buildThemeUI 里的 input 监听
let sigTimer = null;             // 署名的防抖定时器；换项目/重置时要一并取消

/* input[type=color] 只吃 #rrggbb。预设里的值本来就是，但 theme.json 是能被手改的，
   #rgb / #rrggbbaa 也得能显示出来，不然控件会静默退回黑色，看着像「主题丢了」。 */
function toColorInput(v, fallback) {
  let h = String(v || "").trim().replace("#", "");
  if (h.length === 3 || h.length === 4) h = h.split("").slice(0, 3).map((c) => c + c).join("");
  if (h.length === 8) h = h.slice(0, 6);
  h = "#" + (/^[0-9a-fA-F]{6}$/.test(h) ? h : String(fallback || "#000000").replace("#", ""));
  return h.toLowerCase();
}

function buildThemeUI() {
  const o = themeOpts;
  $("#sigMax").textContent = o.max_signature;
  $("#signature").maxLength = o.max_signature;

  $("#presetBox").innerHTML = o.presets.map((p) => `
    <button type="button" class="preset" data-k="${escapeHtml(p.key)}" title="${escapeHtml(p.blurb)}">
      <span class="sw">${p.swatch.map((c) => `<i style="background:${escapeHtml(c)}"></i>`).join("")}</span>
      <span class="pt"><b>${escapeHtml(p.label)}</b><small>${escapeHtml(p.blurb)}</small></span>
    </button>`).join("");
  $$("#presetBox .preset").forEach((b) => b.addEventListener("click", () => {
    /* 换预设连带清掉自定义色：那八个值是相对旧预设的「覆盖」，留着的话新预设
       会被上一套的零碎改动打得七零八落，用户看到的既不是这个预设也不是那个。 */
    putTheme({ preset: b.dataset.k, colors: {} }, `已切到「${labelOf(o.presets, b.dataset.k)}」，自定义色已清空`);
  }));

  const opts = o.fonts.map((f) => `<option value="${escapeHtml(f.key)}">${escapeHtml(f.label)}</option>`).join("");
  $("#titleFont").innerHTML = opts;
  $("#bodyFont").innerHTML = opts;
  $("#titleFont").addEventListener("change", (e) => putTheme({ title_font: e.target.value }, "标题字体已更新"));
  $("#bodyFont").addEventListener("change", (e) => putTheme({ body_font: e.target.value }, "正文字体已更新"));

  $("#colorBox").innerHTML = o.colors.map((c) => `
    <label class="cfield" data-k="${escapeHtml(c.key)}" title="${escapeHtml(c.css)}">
      <input type="color" data-k="${escapeHtml(c.key)}"><span>${escapeHtml(c.label)}</span><em hidden>改</em>
    </label>`).join("");
  $$("#colorBox input[type=color]").forEach((inp) => inp.addEventListener("change", (e) => {
    const k = e.target.dataset.k;
    const custom = Object.assign({}, (themeCur && themeCur.theme.colors) || {});
    /* 调回预设原值就当没改过——不然用户试了几个颜色又调回去，theme.json 里
       会留下一串毫无意义的覆盖项。 */
    if (e.target.value.toLowerCase() === toColorInput(effColors()[k], "").toLowerCase()) delete custom[k];
    else custom[k] = e.target.value;
    putTheme({ colors: custom }, "配色已更新");
  }));

  $("#signature").addEventListener("input", () => {
    /* 记下「还没发出去的值」而不是等 500ms 后再读输入框：这期间任何一次别的主题
       改动都会触发 paintTheme，把框重置成服务端还没收到署名的旧值，
       于是「打完署名顺手点个颜色」就把署名吞了。 */
    sigPending = $("#signature").value;
    clearTimeout(sigTimer);
    sigTimer = setTimeout(async () => {
      const val = sigPending;
      await putTheme({ footer_signature: val }, "页脚署名已更新");
      if (sigPending === val) sigPending = null;
    }, 500);
  });
}

const labelOf = (arr, k) => ((arr.find((x) => x.key === k) || {}).label) || k;
const effColors = () => (themeCur && themeCur.effective_colors) || {};

function paintTheme() {
  if (!themeCur) return;
  const t = themeCur.theme, eff = effColors();
  $$("#presetBox .preset").forEach((b) => b.classList.toggle("on", b.dataset.k === t.preset));
  $("#titleFont").value = t.title_font;
  $("#bodyFont").value = t.body_font;
  if (sigPending === null && document.activeElement !== $("#signature")) {
    $("#signature").value = t.footer_signature || "";
  }
  $$("#colorBox input[type=color]").forEach((inp) => {
    const k = inp.dataset.k;
    inp.value = toColorInput(eff[k], "#000000");
    const custom = Object.prototype.hasOwnProperty.call(t.colors || {}, k);
    inp.closest(".cfield").classList.toggle("custom", custom);
    inp.closest(".cfield").querySelector("em").hidden = !custom;
  });
  $("#logoState").textContent = themeCur.has_logo ? "已上传" : "未上传";
  $("#logoState").className = themeCur.has_logo ? "ok" : "";
  $("#btnLogoDel").disabled = !themeCur.has_logo;
  const pv = $("#logoPreview");
  pv.hidden = !themeCur.has_logo;
  if (themeCur.has_logo) pv.src = themeCur.logo_url;

  const nCustom = Object.keys(t.colors || {}).length;
  $("#themeState").innerHTML =
    `预设 <b>${escapeHtml(labelOf(themeOpts.presets, t.preset))}</b>` +
    (nCustom ? ` ｜ 自定义色 <b>${nCustom}</b> 项` : "") +
    (t.footer_signature ? ` ｜ 署名 ${t.footer_signature.length} 字` : "") +
    (themeCur.has_logo ? " ｜ 校徽 ✓" : "");

  const rej = themeCur.rejected || [];
  const box = $("#themeRejected");
  box.hidden = !rej.length;
  if (rej.length) {
    box.innerHTML = `<b>有 ${rej.length} 处输入被丢弃（不影响生成）：</b><br>` +
      rej.map((r) => "· " + escapeHtml(r)).join("<br>");
  }
}

async function putTheme(patch, okMsg) {
  if (!needPid()) { paintTheme(); return; }
  try {
    themeCur = await api(`/api/projects/${state.pid}/theme`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    });
    paintTheme();
    const rej = themeCur.rejected || [];
    toast(rej.length ? `${rej.length} 处被丢弃：${rej[0]}` : (okMsg + " · 记得重新生成星图"),
          rej.length ? "err" : "ok");
  } catch (e) { toast("主题保存失败：" + e.message, "err"); }
}

async function loadTheme() {
  clearTimeout(sigTimer); sigPending = null;      // 上个项目没发出去的署名不能带过来
  if (!state.pid) { themeCur = null; $("#themeState").textContent = "尚未选择项目"; return; }
  try {
    themeCur = await api(`/api/projects/${state.pid}/theme`);
    paintTheme();
  } catch (e) { $("#themeState").textContent = "主题读取失败：" + e.message; }
}

$("#btnThemeReset").addEventListener("click", async () => {
  if (!needPid()) return;
  clearTimeout(sigTimer); sigPending = null;      // 防抖里那条旧署名不许在重置之后追上来
  try {
    themeCur = await api(`/api/projects/${state.pid}/theme`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reset: true }),
    });
    paintTheme();
    toast(themeCur.has_logo ? "已恢复默认主题（校徽保留，要删请点「删除校徽」）"
                            : "已恢复默认主题", "ok");
  } catch (e) { toast("重置失败：" + e.message, "err"); }
});

bindDrop("#dropLogo", "#logoFile", "#logoState", async (files) => {
  if (!needPid()) return;
  const fd = new FormData(); fd.append("file", files[0]);
  $("#logoState").textContent = "上传中…";
  try {
    const r = await api(`/api/projects/${state.pid}/logo`, { method: "POST", body: fd });
    themeCur = r; paintTheme();
    toast(`校徽已入库：${r.width}×${r.height} · ${(r.bytes / 1024).toFixed(0)}KB` +
          (r.resized ? "（已缩放）" : "") + " · 记得重新生成星图", "ok");
  } catch (e) {
    $("#logoState").textContent = "被拒"; $("#logoState").className = "err";
    toast("校徽被拒：" + e.message, "err");
  }
});

$("#btnLogoDel").addEventListener("click", async () => {
  if (!needPid()) return;
  try {
    themeCur = await api(`/api/projects/${state.pid}/logo`, { method: "DELETE" });
    paintTheme();
    toast("校徽已删除，顶栏不再显示徽章 · 记得重新生成星图", "ok");
  } catch (e) { toast("删除失败：" + e.message, "err"); }
});

/* ---------- 启动 ---------- */
refreshProjects();
setStep(1);
api("/api/theme/options").then((o) => { themeOpts = o; buildThemeUI(); })
  .catch((e) => { $("#themeState").textContent = "主题选项载入失败：" + e.message; });
