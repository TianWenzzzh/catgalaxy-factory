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
    await refreshCalib();
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

bindDrop("#dropPhotos", "#photoFiles", "#photoState", async (files) => {
  if (!needPid()) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  $("#photoState").textContent = `上传 ${files.length} 个…`;
  $("#uploadNote").hidden = true;
  try {
    const r = await api(`/api/projects/${state.pid}/photos`, { method: "POST", body: fd });
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
    toast("上传被拒：" + e.message, "err");
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
      ${r.calib ? `<div><span class="k">星位</span> 人工标定 <b>${r.calib.manual}</b> / ${r.calib.total} 颗${r.calib.manual ? "" : "（未标定 → 星星只按分区聚拢，不对应真实地理位置，可用下方 F8 面板标定）"}</div>` : ""}
      ${r.missing_photos.length ? `<div style="color:var(--red)">缺照片 ${r.missing_photos.length} 张：${r.missing_photos.slice(0, 5).map(escapeHtml).join(", ")}</div>` : ""}
      <div><span class="k">包名</span> <b>${escapeHtml(r.zip_name)}</b></div>`;
    $("#btnPreview").disabled = false;
    $("#btnDownload").disabled = false;
    $("#zipHint").textContent = `已就绪：${r.zip_name}（${(r.zip_bytes / 1024 / 1024).toFixed(2)} MB）`;
    loadPreview(r.preview_url);
    setStep(5, [1, 2, 3, 4]);
    await refreshCalib();
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
        ${its.map((i) => `<span class="lv ${i.level}">${i.code}</span>`).join(" ")}</div>
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
    if (r.report) { state.report = r.report; renderReport(r.report); renderEditList(state.roster); }
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

/* ---------- 启动 ---------- */
refreshProjects();
setStep(1);
