/* 喵星图工厂 · 控制台前端（原生 JS，无构建） */
"use strict";

const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];

const state = { pid: null, school: "示例校", report: null, generated: null, draftCsv: "" };

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
    $("#mapState").textContent = "自动生成"; $("#mapState").className = "";
    $("#report").innerHTML = '<p class="empty">还没有校验结果。上传名册与照片后点「运行校验」。</p>';
    state.report = null; state.generated = null;
    $("#btnPreview").disabled = true; $("#btnDownload").disabled = true;
    $("#frame").src = "about:blank"; $(".frameWrap").classList.remove("loaded");
    $("#genInfo").classList.remove("on");
    setStep(2, [1]);
    await refreshProjects();
    toast(`项目「${school}」已创建`, "ok");
  } catch (e) { toast("创建失败：" + e.message, "err"); }
});

async function refreshProjects() {
  try {
    const list = await api("/api/projects");
    const sel = $("#projList");
    sel.innerHTML = '<option value="">— 打开已有项目 —</option>' +
      list.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.school)} · ${m.photo_count}照 · ${escapeHtml(m.updated_at)}</option>`).join("");
  } catch (e) {}
}
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

bindDrop("#dropPhotos", "#photoFiles", "#photoState", async (files) => {
  if (!needPid()) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  $("#photoState").textContent = `上传 ${files.length} 个…`;
  try {
    const r = await api(`/api/projects/${state.pid}/photos`, { method: "POST", body: fd });
    const kb = (r.out_bytes / 1024).toFixed(0);
    $("#photoState").textContent = `${r.photos.length} 张 · ${kb}KB`;
    $("#photoState").className = "ok";
    if (r.skipped && r.skipped.length) toast(`已跳过 ${r.skipped.length} 个非图片文件`, "");
    toast(`照片已压缩入库：${r.saved} 张（长边≤${r.max_side || 1200}px）`, "ok");
    if (r.report) { state.report = r.report; renderReport(r.report); }
  } catch (e) { $("#photoState").textContent = "失败"; $("#photoState").className = "err"; toast("上传失败：" + e.message, "err"); }
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
  const form = currentForm();
  $("#btnGenerate").disabled = true;
  $("#btnGenerate").textContent = "生成中…";
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
      ${r.missing_photos.length ? `<div style="color:var(--red)">缺照片 ${r.missing_photos.length} 张：${r.missing_photos.slice(0, 5).map(escapeHtml).join(", ")}</div>` : ""}
      <div><span class="k">包名</span> <b>${escapeHtml(r.zip_name)}</b></div>`;
    $("#btnPreview").disabled = false;
    $("#btnDownload").disabled = false;
    $("#zipHint").textContent = `已就绪：${r.zip_name}（${(r.zip_bytes / 1024 / 1024).toFixed(2)} MB）`;
    loadPreview(r.preview_url);
    setStep(5, [1, 2, 3, 4]);
    toast(`星图已生成：${r.cats} 颗星`, "ok");
  } catch (e) {
    toast("生成失败：" + e.message, "err");
    setStep(3, [1, 2]);
  } finally {
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

/* ---------- 启动 ---------- */
refreshProjects();
setStep(1);
