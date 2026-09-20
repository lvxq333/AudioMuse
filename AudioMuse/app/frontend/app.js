(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const state = { items: [], filter: "all", query: "", file: null, selectedId: null, busy: false };
  const statusMeta = {
    pending: ["等待中", "active", 0],
    transcribing: ["正在转写", "active", 1],
    summarizing: ["生成摘要", "active", 2],
    done: ["已完成", "done", 3],
    failed: ["处理失败", "failed", 1],
  };

  const els = {
    dropZone: $("#dropZone"), fileInput: $("#fileInput"), selectedFile: $("#selectedFile"),
    uploadButton: $("#uploadButton"), recordingList: $("#recordingList"), emptyState: $("#emptyState"),
    totalCount: $("#totalCount"), activeCount: $("#activeCount"), searchInput: $("#searchInput"),
    filters: $("#filters"), refresh: $("#refreshButton"), healthDot: $("#healthDot"), healthText: $("#healthText"),
    drawer: $("#detailDrawer"), detail: $("#detailContent"), recordButton: $("#recordButton"),
    recorder: $("#recorder"), recordTime: $("#recordTime"), recordHint: $("#recordHint"), waveform: $("#waveform"),
  };

  function escapeHtml(value = "") {
    return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }
  function bytes(value) {
    if (!Number.isFinite(value)) return "—";
    return value < 1024 * 1024 ? `${(value / 1024).toFixed(1)} KB` : `${(value / 1024 / 1024).toFixed(1)} MB`;
  }
  function dateTime(ms) {
    if (!ms) return "—";
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(ms));
  }
  function toast(message, type = "") {
    const item = document.createElement("div");
    item.className = `toast ${type}`; item.textContent = message;
    $("#toastRegion").appendChild(item);
    setTimeout(() => item.remove(), 3600);
  }
  function idempotencyKey(file) {
    // HTTP 请求头必须是 ASCII。对文件名做稳定哈希，避免中文文件名导致 fetch
    // 在请求发出前抛出 “String contains non ISO-8859-1 code point”。
    let hash = 2166136261;
    for (const char of file.name.normalize("NFC")) {
      hash ^= char.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return `audio-${file.size.toString(36)}-${file.lastModified.toString(36)}-${(hash >>> 0).toString(36)}`;
  }
  function friendlyError(error) {
    const message = error instanceof Error ? error.message : String(error);
    if (/fetch|network|Failed to fetch/i.test(message)) return "无法连接服务，请确认后端正在运行后重试";
    return message || "操作失败，请稍后重试";
  }
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    if (response.status === 204) return null;
    let body;
    try { body = await response.json(); } catch { body = null; }
    if (!response.ok) throw new Error(body?.error?.message || `请求失败（${response.status}）`);
    return body?.data;
  }

  async function healthcheck() {
    try {
      await request("/healthz");
      els.healthDot.className = "health-dot online"; els.healthText.textContent = "服务运行中";
    } catch {
      els.healthDot.className = "health-dot offline"; els.healthText.textContent = "服务未连接";
    }
  }

  function chooseFile(file) {
    if (!file) return;
    const ext = file.name.split(".").pop().toLowerCase();
    if (!["wav", "mp3", "m4a", "aac"].includes(ext)) return toast("请选择 WAV、MP3、M4A 或 AAC 音频", "error");
    if (file.size > 50 * 1024 * 1024) return toast("音频不能超过 50 MB", "error");
    state.file = file;
    els.selectedFile.hidden = false;
    els.selectedFile.textContent = `${file.name} · ${bytes(file.size)}`;
    els.uploadButton.disabled = false;
  }

  async function upload() {
    if (!state.file || state.busy) return;
    state.busy = true; els.uploadButton.classList.add("loading"); els.uploadButton.querySelector("span").textContent = "正在上传…";
    const form = new FormData(); form.append("file", state.file, state.file.name);
    try {
      const result = await request("/v1/recordings", { method: "POST", body: form, headers: { "Idempotency-Key": idempotencyKey(state.file) } });
      toast("已提交，正在为你处理声音");
      state.file = null; els.fileInput.value = ""; els.selectedFile.hidden = true; els.uploadButton.disabled = true;
      await loadList(); openDetail(result.recording_id);
    } catch (error) { toast(friendlyError(error), "error"); }
    finally { state.busy = false; els.uploadButton.classList.remove("loading"); els.uploadButton.querySelector("span").textContent = "开始智能处理"; }
  }

  function visibleItems() {
    return state.items.filter((item) => {
      const status = item.task_status;
      const filterMatch = state.filter === "all" || (state.filter === "active" && ["pending", "transcribing", "summarizing"].includes(status)) || status === state.filter;
      return filterMatch && item.original_filename.toLowerCase().includes(state.query);
    });
  }
  function renderList() {
    const items = visibleItems();
    els.totalCount.textContent = state.items.length;
    els.activeCount.textContent = state.items.filter((x) => ["pending", "transcribing", "summarizing"].includes(x.task_status)).length;
    els.emptyState.hidden = items.length !== 0;
    els.recordingList.innerHTML = items.map((item) => {
      const meta = statusMeta[item.task_status] || [item.task_status, "", 0];
      return `<article class="recording-row" data-recording-id="${escapeHtml(item.recording_id)}" tabindex="0">
        <div class="file-mark">${escapeHtml(item.extension)}</div>
        <div class="file-name"><b>${escapeHtml(item.original_filename)}</b><span>${escapeHtml(item.recording_id.slice(0, 8))} · 第 ${item.attempt_no} 次处理</span></div>
        <div class="row-status ${meta[1]}"><i></i><span>${meta[0]}</span></div>
        <div class="row-meta">${dateTime(item.created_at)}</div><div class="row-meta">${bytes(item.size_bytes)}</div><div class="row-arrow">›</div>
      </article>`;
    }).join("");
  }
  async function loadList(silent = false) {
    if (!silent) els.recordingList.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div>';
    try { const data = await request("/v1/recordings?page=1&page_size=100"); state.items = data.items; renderList(); }
    catch (error) { if (!silent) { state.items = []; renderList(); toast(error.message, "error"); } }
  }

  function listHtml(items, className = "") {
    return `<ul class="point-list ${className}">${items.length ? items.map((x) => `<li>${escapeHtml(x)}</li>`).join("") : "<li>暂未提取到内容</li>"}</ul>`;
  }
  function renderDetail(data) {
    const meta = statusMeta[data.status] || [data.status, "", 0];
    const progress = [1, 2, 3].map((n) => `<span class="progress-segment ${n <= meta[2] ? "on" : ""}"></span>`).join("");
    let result = "";
    if (data.status === "done") {
      const summary = data.summary || {};
      result = `<section class="result-block"><div class="result-label"><span>一句话摘要</span></div><p class="summary-quote">${escapeHtml(summary.summary || "暂无摘要")}</p></section>
        <section class="result-block"><div class="result-label"><span>核心要点</span></div>${listHtml(summary.key_points || [])}</section>
        <section class="result-block"><div class="result-label"><span>行动待办</span></div>${listHtml(summary.todos || [], "todos")}</section>
        <section class="result-block"><div class="result-label"><span>完整转写</span><button type="button" data-copy>复制全文</button></div><p class="transcript" id="transcriptText">${escapeHtml(data.transcript || "暂无转写")}</p></section>`;
    } else if (data.status === "failed") {
      result = `<div class="error-box"><b>${escapeHtml(data.error_code || "处理失败")}</b><br>${escapeHtml(data.error_message || "请稍后重试")}</div>`;
    } else {
      result = `<div class="processing-message"><b>${meta[0]}</b><br>后台正在处理这段声音，页面会自动刷新结果。</div>`;
    }
    els.detail.innerHTML = `<div class="detail-kicker">${escapeHtml(meta[0].toUpperCase())}</div><h2 class="detail-title">${escapeHtml(data.original_filename)}</h2>
      <div class="detail-sub">${bytes(data.size_bytes)} · ${dateTime(data.created_at)} · 第 ${data.attempt_no} 次处理</div><div class="detail-progress">${progress}</div>${result}
      <div class="detail-actions">${data.status === "failed" ? '<button type="button" class="retry" data-retry>重新处理</button>' : ""}<button type="button" class="danger" data-delete>删除记录</button></div>`;
  }
  async function openDetail(id) {
    state.selectedId = id; els.drawer.classList.add("open"); els.drawer.setAttribute("aria-hidden", "false"); document.body.style.overflow = "hidden";
    els.detail.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
    try { renderDetail(await request(`/v1/recordings/${id}`)); } catch (error) { toast(error.message, "error"); closeDrawer(); }
  }
  function closeDrawer() { state.selectedId = null; els.drawer.classList.remove("open"); els.drawer.setAttribute("aria-hidden", "true"); document.body.style.overflow = ""; }
  async function retrySelected() {
    const item = state.items.find((x) => x.recording_id === state.selectedId); if (!item) return;
    try { await request(`/v1/tasks/${item.task_id}/retry`, { method: "POST" }); toast("已重新提交处理"); await loadList(true); await openDetail(state.selectedId); } catch (error) { toast(error.message, "error"); }
  }
  async function deleteSelected() {
    if (!state.selectedId || !confirm("确定删除这条录音及其处理结果吗？此操作不可撤销。")) return;
    const id = state.selectedId;
    try { await request(`/v1/recordings/${id}`, { method: "DELETE" }); closeDrawer(); toast("录音记录已删除"); await loadList(true); } catch (error) { toast(error.message, "error"); }
  }

  // 浏览器端录制为 16-bit 单声道 WAV，生成的文件可直接进入后端真实 ASR 链路。
  let audioContext, stream, source, processor, analyser, chunks = [], recordStarted = 0, clockTimer, animationFrame;
  function encodeWav(buffers, sampleRate) {
    const length = buffers.reduce((sum, item) => sum + item.length, 0); const merged = new Float32Array(length); let offset = 0;
    buffers.forEach((item) => { merged.set(item, offset); offset += item.length; });
    const out = new ArrayBuffer(44 + merged.length * 2); const view = new DataView(out);
    const text = (at, value) => [...value].forEach((char, i) => view.setUint8(at + i, char.charCodeAt(0)));
    text(0, "RIFF"); view.setUint32(4, 36 + merged.length * 2, true); text(8, "WAVE"); text(12, "fmt "); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true); view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true); text(36, "data"); view.setUint32(40, merged.length * 2, true);
    for (let i = 0; i < merged.length; i++) { const sample = Math.max(-1, Math.min(1, merged[i])); view.setInt16(44 + i * 2, sample < 0 ? sample * 0x8000 : sample * 0x7fff, true); }
    return new Blob([view], { type: "audio/wav" });
  }
  function drawWave() {
    const ctx = els.waveform.getContext("2d"), width = els.waveform.width, height = els.waveform.height;
    ctx.clearRect(0, 0, width, height); ctx.strokeStyle = "rgba(36,41,35,.2)"; ctx.lineWidth = 1;
    let values = new Uint8Array(128); if (analyser) analyser.getByteTimeDomainData(values); else values.fill(128);
    ctx.beginPath(); for (let i = 0; i < values.length; i++) { const x = i / (values.length - 1) * width; const y = (values[i] / 255) * height; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); } ctx.stroke();
    animationFrame = requestAnimationFrame(drawWave);
  }
  async function startRecording() {
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true }, video: false });
      audioContext = new (window.AudioContext || window.webkitAudioContext)(); source = audioContext.createMediaStreamSource(stream); analyser = audioContext.createAnalyser(); analyser.fftSize = 256;
      processor = audioContext.createScriptProcessor(4096, 1, 1); chunks = []; processor.onaudioprocess = (event) => chunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
      source.connect(analyser); analyser.connect(processor); processor.connect(audioContext.destination); recordStarted = Date.now();
      els.recorder.classList.add("recording"); els.recordButton.querySelector("b").textContent = "结束录音"; els.recordHint.textContent = "正在录制 · 点击结束";
      clockTimer = setInterval(() => { const seconds = Math.floor((Date.now() - recordStarted) / 1000); els.recordTime.textContent = `${String(Math.floor(seconds / 60)).padStart(2,"0")}:${String(seconds % 60).padStart(2,"0")}`; }, 250);
    } catch { toast("无法使用麦克风，请检查浏览器权限", "error"); }
  }
  async function stopRecording() {
    clearInterval(clockTimer); processor?.disconnect(); source?.disconnect(); stream?.getTracks().forEach((track) => track.stop());
    const sampleRate = audioContext.sampleRate; await audioContext.close(); audioContext = source = processor = analyser = stream = null;
    els.recorder.classList.remove("recording"); els.recordButton.querySelector("b").textContent = "重新录音"; els.recordHint.textContent = "录音已就绪，可以提交";
    const blob = encodeWav(chunks, sampleRate); chooseFile(new File([blob], `录音-${new Date().toISOString().slice(0,19).replace(/[:T]/g,"-")}.wav`, { type: "audio/wav", lastModified: Date.now() }));
  }

  els.dropZone.addEventListener("click", () => els.fileInput.click());
  els.dropZone.addEventListener("keydown", (e) => { if (["Enter", " "].includes(e.key)) { e.preventDefault(); els.fileInput.click(); } });
  els.fileInput.addEventListener("change", () => chooseFile(els.fileInput.files[0]));
  ["dragenter", "dragover"].forEach((name) => els.dropZone.addEventListener(name, (e) => { e.preventDefault(); els.dropZone.classList.add("dragover"); }));
  ["dragleave", "drop"].forEach((name) => els.dropZone.addEventListener(name, (e) => { e.preventDefault(); els.dropZone.classList.remove("dragover"); }));
  els.dropZone.addEventListener("drop", (e) => chooseFile(e.dataTransfer.files[0])); els.uploadButton.addEventListener("click", upload);
  els.recordButton.addEventListener("click", () => audioContext ? stopRecording() : startRecording());
  els.filters.addEventListener("click", (e) => { const button = e.target.closest("button"); if (!button) return; els.filters.querySelectorAll("button").forEach((x) => x.classList.remove("active")); button.classList.add("active"); state.filter = button.dataset.filter; renderList(); });
  els.searchInput.addEventListener("input", () => { state.query = els.searchInput.value.trim().toLowerCase(); renderList(); });
  els.recordingList.addEventListener("click", (e) => { const row = e.target.closest("[data-recording-id]"); if (row) openDetail(row.dataset.recordingId); });
  els.recordingList.addEventListener("keydown", (e) => { if (e.key === "Enter") { const row = e.target.closest("[data-recording-id]"); if (row) openDetail(row.dataset.recordingId); } });
  document.querySelectorAll("[data-close-drawer]").forEach((x) => x.addEventListener("click", closeDrawer));
  els.detail.addEventListener("click", (e) => { if (e.target.closest("[data-retry]")) retrySelected(); if (e.target.closest("[data-delete]")) deleteSelected(); if (e.target.closest("[data-copy]")) navigator.clipboard.writeText($("#transcriptText")?.textContent || "").then(() => toast("转写内容已复制")); });
  els.refresh.addEventListener("click", () => loadList()); document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
  drawWave(); healthcheck(); loadList();
  setInterval(async () => { await loadList(true); if (state.selectedId) { try { renderDetail(await request(`/v1/recordings/${state.selectedId}`)); } catch {} } }, 3000);
  window.addEventListener("beforeunload", () => { cancelAnimationFrame(animationFrame); stream?.getTracks().forEach((track) => track.stop()); });
})();
