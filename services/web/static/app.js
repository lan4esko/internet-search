(() => {
  "use strict";

  const API = "/api";
  const state = { task: null, tasks: [], tab: "open-ports", timer: null, metricsTimer: null, eventsTimer: null, eventsCursor: 0, events: [], metricHistory: [], workerHistory: [], lastTabLoad: 0 };
  const $ = (id) => document.getElementById(id);
  const text = (value) => String(value ?? "");
  const statusLabel = (status) => ({ provisioning: "создание БД", queued: "в очереди", running: "работает", completed: "завершён", failed: "ошибка", cancelled: "отменён" }[status] || status);
  const message = (id, value, error = false) => { const node = $(id); if (!node) return; node.textContent = value || ""; node.classList.toggle("error", error); };
  const formatNumber = (value) => Number(value || 0).toLocaleString("ru-RU");
  const formatBytes = (value) => { if (value == null) return "—"; let number = Number(value); const units = ["Б", "КиБ", "МиБ", "ГиБ"]; let unit = 0; while (number >= 1024 && unit < units.length - 1) { number /= 1024; unit += 1; } return `${number.toFixed(unit ? 1 : 0)} ${units[unit]}`; };
  const formatRate = (value) => value == null ? "—" : `${formatBytes(value)}/с`;

  async function request(path, options = {}) {
    const response = await fetch(`${API}${path}`, options); const raw = await response.text(); let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch { data = { detail: raw }; }
    if (!response.ok) throw new Error(typeof data?.detail === "string" ? data.detail : `HTTP ${response.status}`);
    return data;
  }

  function renderTaskTabs() {
    const tabs = $("task-tabs"); tabs.replaceChildren();
    state.tasks.forEach((task) => {
      const button = document.createElement("button"); button.className = "task-tab"; button.setAttribute("role", "tab"); button.setAttribute("aria-selected", String(state.task?.id === task.id));
      if (state.task?.id === task.id) button.classList.add("active");
      button.innerHTML = "<strong></strong><span></span>"; button.querySelector("strong").textContent = `TASK #${task.id} · ${task.name}`; button.querySelector("span").textContent = `${statusLabel(task.status)} · ${task.progress_percent ?? 0}%`; button.onclick = () => selectTask(task.id); tabs.append(button);
    });
  }

  function renderTasks(page) {
    state.tasks = page.items || []; renderTaskTabs(); const table = $("tasks-table"); table.replaceChildren(); $("tasks-empty").classList.toggle("hidden", state.tasks.length !== 0);
    state.tasks.forEach((task) => {
      const row = document.createElement("tr"); row.className = "task-row"; row.onclick = () => selectTask(task.id); [`TASK #${task.id} · ${task.name}`, statusLabel(task.status), `${task.progress_percent ?? 0}%`, formatNumber(task.target_count), formatNumber(task.open_port_count), formatNumber(task.result_count)].forEach((value) => { const cell = document.createElement("td"); cell.textContent = text(value); row.append(cell); });
      const cell = document.createElement("td"); const button = document.createElement("button"); button.type = "button"; button.className = "secondary small open-task"; button.textContent = "Открыть"; button.onclick = (event) => { event.preventDefault(); event.stopPropagation(); selectTask(task.id); }; cell.append(button); row.append(cell); table.append(row);
    });
  }

  async function refreshTasks() { try { const page = await request("/tasks?limit=100"); renderTasks(page); if (!state.task && page.items?.length) await selectTask(page.items[0].id); } catch (error) { message("task-form-message", error.message, true); } }
  const mib = (value) => value == null ? "—" : `${(Number(value) / 1048576).toFixed(1)} МиБ`;
  const bytes = (value) => value == null ? "—" : `${(Number(value) / 1048576).toFixed(1)} МиБ`;
  function drawChart(id, series, labels) {
    const svg = $(id); if (!svg) return; svg.replaceChildren(); const width = Number(svg.viewBox.baseVal.width || 640); const height = Number(svg.viewBox.baseVal.height || 180); const values = series.flatMap((item) => item.values || []); const max = Math.max(1, ...values);
    if (!values.length || values.every((value) => value === 0)) { const empty = document.createElementNS("http://www.w3.org/2000/svg", "text"); empty.setAttribute("x", "24"); empty.setAttribute("y", "92"); empty.setAttribute("class", "chart-empty"); empty.textContent = "Нет данных"; svg.append(empty); return; }
    series.forEach((item) => { const points = (item.values || []).map((value, index) => `${(index / Math.max(1, item.values.length - 1)) * width},${height - (Number(value) / max) * (height - 16) - 8}`).join(" "); const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline"); line.setAttribute("points", points); line.setAttribute("class", `chart-line ${item.className || ""}`); svg.append(line); });
    labels.forEach((label, index) => { const legend = document.createElementNS("http://www.w3.org/2000/svg", "text"); legend.setAttribute("x", String(12 + index * 105)); legend.setAttribute("y", String(height - 5)); legend.setAttribute("class", `chart-legend ${label.className || ""}`); legend.textContent = label.text; svg.append(legend); });
  }
  async function refreshMetrics() {
    try {
      const data = await request("/operations/container-metrics"); const now = Date.now(); const containers = data.containers || []; const rx = containers.reduce((sum, item) => sum + Number(item.network_rx_bytes || 0), 0); const tx = containers.reduce((sum, item) => sum + Number(item.network_tx_bytes || 0), 0); const previous = state.metricHistory.at(-1); const seconds = previous ? Math.max(.1, (now - previous.time) / 1000) : 1; const sample = { time: now, rx, tx, rxRate: previous ? Math.max(0, (rx - previous.rx) / seconds) : 0, txRate: previous ? Math.max(0, (tx - previous.tx) / seconds) : 0, cpu: containers.reduce((sum, item) => sum + Number(item.cpu_percent || 0), 0) }; state.metricHistory.push(sample); state.metricHistory = state.metricHistory.slice(-60); $("telemetry-state").textContent = `${data.status || "нет данных"} · ${data.sampled_at || ""}`; $("metric-rx").textContent = formatRate(sample.rxRate); $("metric-tx").textContent = formatRate(sample.txRate); $("metric-cpu").textContent = `${sample.cpu.toFixed(1)}%`; $("metric-worker").textContent = `${Math.round(state.task?.masscan_live?.rate_pps || 0)} п/с`; const table = $("metrics-table"); if (table) table.replaceChildren();
      if (table) (data.containers || []).forEach((item) => { const row = document.createElement("tr"); [item.service || item.name, item.state, item.cpu_percent == null ? "—" : `${Number(item.cpu_percent).toFixed(1)}%`, mib(item.memory_usage_bytes), item.pids ?? "—", `${bytes(item.network_rx_bytes)} / ${bytes(item.network_tx_bytes)}`].forEach((value) => { const cell = document.createElement("td"); cell.textContent = text(value); row.append(cell); }); table.append(row); });
      drawChart("network-chart", [{ values: state.metricHistory.map((item) => item.rxRate), className: "rx" }, { values: state.metricHistory.map((item) => item.txRate), className: "tx" }], [{ text: "RX", className: "rx" }, { text: "TX", className: "tx" }]); drawChart("container-chart", [{ values: state.metricHistory.map((item) => item.cpu), className: "cpu" }], [{ text: "CPU всех контейнеров", className: "cpu" }]);
    } catch (error) { $("telemetry-state").textContent = `метрики недоступны: ${error.message}`; }
  }

  function stage(id, value) { $(id).textContent = text(value); }
  function argv(value) { return Array.isArray(value) ? value.map((item) => JSON.stringify(String(item))).join(" ") : "—"; }
  function renderDetail(task) {
    const selected = state.tasks.findIndex((item) => item.id === task.id);
    if (selected >= 0) state.tasks[selected] = task;
    $("task-panel").classList.remove("hidden"); $("detail-name").textContent = task.name; $("detail-database").textContent = `${task.database_name} · ${statusLabel(task.status)}`; $("detail-progress-bar").style.width = `${Math.min(100, Math.max(0, Number(task.progress_percent || 0)))}%`; $("detail-progress-label").textContent = `Общий прогресс: ${task.progress_percent || 0}% · исходный файл: ${task.source_filename}`;
    const live = task.masscan_live && task.masscan_live.chunk_no ? ` · поток: ${Math.round(task.masscan_live.rate_pps || 0)} п/с · найдено ${task.masscan_live.found_count || 0}` : "";
    const previousWorker = state.workerHistory.at(-1); const now = Date.now(); const elapsed = previousWorker ? Math.max(.1, (now - previousWorker.time) / 1000) : 1; state.workerHistory.push({ time: now, masscan: Number(task.masscan_live?.rate_pps || 0), httpx: previousWorker ? Math.max(0, (Number(task.result_count || 0) - previousWorker.responses) / elapsed) : 0, responses: Number(task.result_count || 0) }); state.workerHistory = state.workerHistory.slice(-60);
    stage("stage-scope-value", `${task.target_count} IP`); stage("stage-masscan-value", `${task.masscan_chunks.completed || 0}/${task.masscan_chunks.total || 0} чанков · ${task.open_port_count} открытых портов${live}`); stage("stage-httpx-value", `${task.httpx_chunks.completed || 0}/${task.httpx_chunks.total || 0} чанков`); stage("stage-results-value", `${task.result_count} совпадений`);
    ["stage-scope", "stage-masscan", "stage-httpx", "stage-results"].forEach((id) => $(id).classList.remove("done", "active")); if (task.target_count) $("stage-scope").classList.add("done"); if (task.masscan_chunks.running || (task.masscan_chunks.total && task.masscan_chunks.completed < task.masscan_chunks.total)) $("stage-masscan").classList.add("active"); if (task.httpx_chunks.running || (task.httpx_chunks.total && task.httpx_chunks.completed < task.httpx_chunks.total)) $("stage-httpx").classList.add("active"); if (task.result_count) $("stage-results").classList.add("done");
    $("masscan-command").textContent = argv(task.config?.commands?.masscan); $("httpx-command").textContent = argv(task.config?.commands?.httpx); renderTaskTabs(); drawChart("worker-chart", [{ values: state.workerHistory.map((item) => item.masscan), className: "masscan" }, { values: state.workerHistory.map((item) => item.httpx), className: "httpx" }], [{ text: "Masscan п/с", className: "masscan" }, { text: "HTTPX отв/с", className: "httpx" }]);
  }

  const columns = { targets: [["ip", "IP"], ["source_chunk_no", "chunk"], ["masscan_status", "Masscan"], ["ready_for_masscan", "ready"]], results: [["id", "id"], ["ip", "ip"], ["port", "port"], ["protocol", "protocol"], ["url", "url"], ["status_code", "status"], ["title", "title"], ["matched", "matched"]], "open-ports": [["ip", "IP"], ["port", "port"], ["protocol", "protocol"], ["masscan_open", "Masscan"], ["httpx_status", "HTTPX"], ["httpx_chunk_no", "chunk"]] };
  function endpointLinks(item) { const host = text(item.ip); const port = Number(item.port); const schemes = [80, 8000, 8080, 8081].includes(port) ? ["http", "https"] : ["https", "http"]; const fragment = document.createDocumentFragment(); schemes.forEach((scheme) => { const link = document.createElement("a"); link.className = "endpoint-link"; link.href = `${scheme}://${host}:${port}`; link.target = "_blank"; link.rel = "noopener noreferrer"; link.title = "Ctrl+клик откроет ссылку в новой вкладке"; link.textContent = scheme.toUpperCase(); fragment.append(link); }); return fragment; }
  function renderItems(data, kind) {
    const table = document.createElement("table"); const head = document.createElement("thead"); const header = document.createElement("tr"); columns[kind].forEach(([, label]) => { const th = document.createElement("th"); th.textContent = label; header.append(th); }); if (kind === "open-ports") { const th = document.createElement("th"); th.textContent = "Переход"; header.append(th); } head.append(header); table.append(head); const body = document.createElement("tbody");
    (data.items || []).forEach((item) => { const row = document.createElement("tr"); columns[kind].forEach(([key]) => { const cell = document.createElement("td"); const value = item[key]; if (kind === "results" && key === "url" && value) { const link = document.createElement("a"); link.href = value; link.target = "_blank"; link.rel = "noopener noreferrer"; link.className = "result-link"; link.textContent = value; cell.append(link); } else cell.textContent = typeof value === "object" ? JSON.stringify(value) : text(value); row.append(cell); }); if (kind === "open-ports") { const cell = document.createElement("td"); cell.className = "endpoint-actions"; cell.append(endpointLinks(item)); row.append(cell); } body.append(row); }); table.append(body); return table;
  }
  function renderEvents() { if (state.tab !== "events") return; const pre = document.createElement("pre"); pre.className = "event-log"; pre.textContent = state.events.length ? state.events.map((item) => `[${new Date(item.created_at).toLocaleTimeString("ru-RU")}] [${item.stage}] ${item.message}`).join("\n") : "Событий пока нет. Новый worker добавит поток здесь."; $("detail-content").replaceChildren(pre); pre.scrollTop = pre.scrollHeight; }
  async function loadEvents() { if (!state.task) return; try { const data = await request(`/tasks/${state.task.id}/events?after_id=${state.eventsCursor}&limit=200`); if (data.items?.length) { state.events.push(...data.items); state.events = state.events.slice(-400); state.eventsCursor = Number(data.last_id || state.eventsCursor); renderEvents(); } } catch (error) { message("detail-message", `Поток событий: ${error.message}`, true); } }
  async function loadTab() {
    if (!state.task) return; if (state.tab === "events") { renderEvents(); await loadEvents(); return; } $("detail-content").replaceChildren(); $("detail-content").textContent = "Загрузка…";
    try { const id = state.task.id; const endpoint = state.tab === "targets" ? `/tasks/${id}/targets?limit=200` : state.tab === "results" ? `/tasks/${id}/results?matched_only=true&limit=200` : `/tasks/${id}/open-ports?limit=200`; const data = await request(endpoint); $("detail-content").replaceChildren(renderItems(data, state.tab)); state.lastTabLoad = Date.now(); } catch (error) { message("detail-message", error.message, true); }
  }
  async function selectTask(id) { clearTimeout(state.timer); clearTimeout(state.eventsTimer); state.eventsCursor = 0; state.events = []; try { state.task = await request(`/tasks/${id}`); renderDetail(state.task); await loadTab(); await loadEvents(); state.timer = setTimeout(pollTask, 900); state.eventsTimer = setTimeout(pollEvents, 900); } catch (error) { message("detail-message", error.message, true); } }
  async function pollEvents() { if (!state.task) return; await loadEvents(); state.eventsTimer = setTimeout(pollEvents, ["running", "queued", "provisioning"].includes(state.task.status) ? 900 : 2500); }
  async function pollTask() { if (!state.task) return; try { state.task = await request(`/tasks/${state.task.id}`); renderDetail(state.task); if (Date.now() - state.lastTabLoad > 2500 && state.tab !== "events") await loadTab(); if (["running", "queued", "provisioning"].includes(state.task.status)) state.timer = setTimeout(pollTask, 900); else await refreshTasks(); } catch (error) { message("detail-message", error.message, true); state.timer = setTimeout(pollTask, 2000); } }

  function updateFormPreview() {
    const value = (id) => $(id).value.trim();
    const lines = (id) => value(id).split(/\s+/).filter(Boolean);
    const masscan = ["masscan", "--includefile", "{CIDR_SHARD}", "--ports", value("masscan-ports"), "--rate", value("masscan-rate"), "--output-format", value("masscan-output-format"), "--output-filename", "{OUTPUT_FILE}", "--wait", "5", "--retries", "0", ...lines("masscan-extra-args")];
    const httpx = ["httpx", "-list", "{OPEN_ENDPOINTS_FILE}", "-output", "{OUTPUT_JSONL}", "-json", "-silent", "-no-color", "-disable-update-check", "-no-fallback-scheme", "-include-response", "-threads", "500", "-timeout", "5", "-follow-redirects", "-follow-host-redirects", "-path", "/remote/login,/remote/index,/ssl-vpn/login.html", "-match-string", "ftnt-fortinet-grid", "-match-string", 'name="magic"', "-match-string", "SVPNCOOKIE", "-rate-limit", value("httpx-rate-limit"), ...lines("httpx-extra-args")];
    $("form-masscan-command").textContent = argv(masscan); $("form-httpx-command").textContent = argv(httpx);
  }
  async function previewCidr(file) { const box = $("cidr-preview"); if (!file) { box.textContent = "Загрузите файл, чтобы получить предварительную статистику."; box.classList.remove("error"); return; } box.textContent = "Считаем подсети, адреса и дубликаты…"; try { const form = new FormData(); form.append("file", file); const data = await request("/tasks/preview-cidr", { method: "POST", body: form }); box.replaceChildren(); [["Подсетей", data.subnet_count], ["IPv4-адресов", data.address_count], ["Дубликатов", data.duplicate_count], ["Ошибочных строк", data.invalid_count]].forEach(([label, value]) => { const item = document.createElement("span"); const small = document.createElement("small"); small.textContent = label; const strong = document.createElement("strong"); strong.textContent = formatNumber(value); item.append(small, strong); box.append(item); }); const note = document.createElement("em"); note.textContent = `${text(data.filename)} · SHA-256 ${text(data.sha256).slice(0, 12)}…`; box.append(note); box.classList.remove("error"); } catch (error) { box.textContent = `Предпросмотр недоступен: ${error.message}`; box.classList.add("error"); } }
  document.querySelectorAll("#task-form input, #task-form select, #task-form textarea").forEach((field) => field.addEventListener("input", updateFormPreview));
  $("task-file").addEventListener("change", (event) => previewCidr(event.target.files?.[0]));
  updateFormPreview();
  $("task-form").addEventListener("submit", async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); message("task-form-message", "Создаём TASK, физическую базу и сохраняем argv…"); try { const task = await request("/tasks", { method: "POST", body: form }); message("task-form-message", `TASK #${task.id} создан.`); event.currentTarget.reset(); updateFormPreview(); previewCidr(null); await refreshTasks(); await selectTask(task.id); } catch (error) { message("task-form-message", error.message, true); } });
  $("refresh-tasks").onclick = refreshTasks; $("refresh-metrics").onclick = refreshMetrics;
  document.querySelector(".tabs").addEventListener("click", (event) => { const button = event.target.closest("[data-tab]"); if (!button) return; document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.tab = button.dataset.tab; loadTab(); });
  $("delete-task").onclick = async () => { if (!state.task) return; try { await request(`/tasks/${state.task.id}`, { method: "DELETE" }); state.task = null; $("task-panel").classList.add("hidden"); await refreshTasks(); } catch (error) { message("detail-message", error.message, true); } };
  $("export-results").onclick = async () => { if (!state.task) return; try { const data = await request(`/tasks/${state.task.id}/export`); const blob = new Blob([data.targets.join("\n") + (data.targets.length ? "\n" : "")], { type: "text/plain;charset=utf-8" }); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `task-${state.task.id}-fortinet.txt`; link.click(); URL.revokeObjectURL(link.href); } catch (error) { message("detail-message", error.message, true); } };
  refreshTasks(); refreshMetrics(); state.metricsTimer = setInterval(refreshMetrics, 2500);
})();
