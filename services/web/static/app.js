(() => {
  "use strict";

  const API = "/api";
  const state = { task: null, tasks: [], tab: "open-ports", timer: null, metricsTimer: null };
  const $ = (id) => document.getElementById(id);
  const text = (value) => String(value ?? "");
  const statusLabel = (status) => ({ provisioning: "создание БД", queued: "в очереди", running: "работает", completed: "завершён", failed: "ошибка", cancelled: "отменён" }[status] || status);
  const message = (id, value, error = false) => { const node = $(id); node.textContent = value || ""; node.classList.toggle("error", error); };

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
      button.innerHTML = "<strong></strong><span></span>"; button.querySelector("strong").textContent = task.name; button.querySelector("span").textContent = `${statusLabel(task.status)} · ${task.progress_percent ?? 0}%`; button.onclick = () => selectTask(task.id); tabs.append(button);
    });
  }

  function renderTasks(page) {
    state.tasks = page.items || []; renderTaskTabs(); const table = $("tasks-table"); table.replaceChildren(); $("tasks-empty").classList.toggle("hidden", state.tasks.length !== 0);
    state.tasks.forEach((task) => {
      const row = document.createElement("tr"); [task.name, statusLabel(task.status), `${task.progress_percent ?? 0}%`, task.target_count, task.open_port_count, task.result_count].forEach((value) => { const cell = document.createElement("td"); cell.textContent = text(value); row.append(cell); });
      const cell = document.createElement("td"); const button = document.createElement("button"); button.className = "secondary small"; button.textContent = "Открыть"; button.onclick = () => selectTask(task.id); cell.append(button); row.append(cell); table.append(row);
    });
  }

  async function refreshTasks() { try { const page = await request("/tasks?limit=100"); renderTasks(page); if (!state.task && page.items?.length) await selectTask(page.items[0].id); } catch (error) { message("task-form-message", error.message, true); } }
  const mib = (value) => value == null ? "—" : `${(Number(value) / 1048576).toFixed(1)} МиБ`;
  const bytes = (value) => value == null ? "—" : `${(Number(value) / 1048576).toFixed(1)} МиБ`;
  async function refreshMetrics() {
    try {
      const data = await request("/operations/container-metrics"); $("metrics-state").textContent = `${data.status || "нет данных"} · ${data.sampled_at || ""}`; const table = $("metrics-table"); table.replaceChildren();
      (data.containers || []).forEach((item) => { const row = document.createElement("tr"); [item.service || item.name, item.state, item.cpu_percent == null ? "—" : `${Number(item.cpu_percent).toFixed(1)}%`, mib(item.memory_usage_bytes), item.pids ?? "—", `${bytes(item.network_rx_bytes)} / ${bytes(item.network_tx_bytes)}`].forEach((value) => { const cell = document.createElement("td"); cell.textContent = text(value); row.append(cell); }); table.append(row); });
    } catch (error) { $("metrics-state").textContent = `метрики недоступны: ${error.message}`; }
  }

  function stage(id, value) { $(id).textContent = text(value); }
  function argv(value) { return Array.isArray(value) ? value.map((item) => JSON.stringify(String(item))).join(" ") : "—"; }
  function renderDetail(task) {
    const selected = state.tasks.findIndex((item) => item.id === task.id);
    if (selected >= 0) state.tasks[selected] = task;
    $("task-panel").classList.remove("hidden"); $("detail-name").textContent = task.name; $("detail-database").textContent = `${task.database_name} · ${statusLabel(task.status)}`; $("detail-progress-bar").style.width = `${Math.min(100, Math.max(0, Number(task.progress_percent || 0)))}%`; $("detail-progress-label").textContent = `Общий прогресс: ${task.progress_percent || 0}% · исходный файл: ${task.source_filename}`;
    const live = task.masscan_live && task.masscan_live.chunk_no ? ` · поток: ${Math.round(task.masscan_live.rate_pps || 0)} п/с · найдено ${task.masscan_live.found_count || 0}` : "";
    stage("stage-scope-value", `${task.target_count} IP`); stage("stage-masscan-value", `${task.masscan_chunks.completed || 0}/${task.masscan_chunks.total || 0} чанков · ${task.open_port_count} открытых портов${live}`); stage("stage-httpx-value", `${task.httpx_chunks.completed || 0}/${task.httpx_chunks.total || 0} чанков`); stage("stage-results-value", `${task.result_count} совпадений`);
    ["stage-scope", "stage-masscan", "stage-httpx", "stage-results"].forEach((id) => $(id).classList.remove("done", "active")); if (task.target_count) $("stage-scope").classList.add("done"); if (task.masscan_chunks.running || (task.masscan_chunks.total && task.masscan_chunks.completed < task.masscan_chunks.total)) $("stage-masscan").classList.add("active"); if (task.httpx_chunks.running || (task.httpx_chunks.total && task.httpx_chunks.completed < task.httpx_chunks.total)) $("stage-httpx").classList.add("active"); if (task.result_count) $("stage-results").classList.add("done");
    $("masscan-command").textContent = argv(task.config?.commands?.masscan); $("httpx-command").textContent = argv(task.config?.commands?.httpx); renderTaskTabs();
  }

  const columns = { targets: [["ip", "IP"], ["source_chunk_no", "chunk"], ["masscan_status", "Masscan"], ["ready_for_masscan", "ready"]], results: [["id", "id"], ["ip", "ip"], ["port", "port"], ["protocol", "protocol"], ["url", "url"], ["status_code", "status"], ["title", "title"], ["matched", "matched"]], "open-ports": [["ip", "IP"], ["port", "port"], ["protocol", "protocol"], ["masscan_open", "Masscan"], ["httpx_status", "HTTPX"], ["httpx_chunk_no", "chunk"]] };
  function renderItems(data, kind) {
    const table = document.createElement("table"); const head = document.createElement("thead"); const header = document.createElement("tr"); columns[kind].forEach(([, label]) => { const th = document.createElement("th"); th.textContent = label; header.append(th); }); head.append(header); table.append(head); const body = document.createElement("tbody");
    (data.items || []).forEach((item) => { const row = document.createElement("tr"); columns[kind].forEach(([key]) => { const cell = document.createElement("td"); const value = item[key]; cell.textContent = typeof value === "object" ? JSON.stringify(value) : text(value); row.append(cell); }); body.append(row); }); table.append(body); return table;
  }
  async function loadTab() {
    if (!state.task) return; $("detail-content").replaceChildren(); $("detail-content").textContent = "Загрузка…";
    try { const id = state.task.id; const endpoint = state.tab === "targets" ? `/tasks/${id}/targets?limit=200` : state.tab === "results" ? `/tasks/${id}/results?matched_only=true&limit=200` : `/tasks/${id}/open-ports?limit=200`; const data = await request(endpoint); $("detail-content").replaceChildren(renderItems(data, state.tab)); } catch (error) { message("detail-message", error.message, true); }
  }
  async function selectTask(id) { try { state.task = await request(`/tasks/${id}`); renderDetail(state.task); await loadTab(); clearTimeout(state.timer); state.timer = setTimeout(pollTask, 2000); } catch (error) { message("detail-message", error.message, true); } }
  async function pollTask() { if (!state.task) return; try { state.task = await request(`/tasks/${state.task.id}`); renderDetail(state.task); if (["running", "queued", "provisioning"].includes(state.task.status)) state.timer = setTimeout(pollTask, 2000); else await refreshTasks(); } catch (error) { message("detail-message", error.message, true); } }

  function updateFormPreview() {
    const value = (id) => $(id).value.trim();
    const lines = (id) => value(id).split(/\s+/).filter(Boolean);
    const masscan = ["masscan", "--includefile", "{CIDR_SHARD}", "--ports", value("masscan-ports"), "--rate", value("masscan-rate"), "--output-format", value("masscan-output-format"), "--output-filename", "{OUTPUT_FILE}", "--wait", "5", "--retries", "0", ...lines("masscan-extra-args")];
    const httpx = ["httpx", "-list", "{OPEN_ENDPOINTS_FILE}", "-output", "{OUTPUT_JSONL}", "-json", "-silent", "-no-color", "-disable-update-check", "-no-fallback-scheme", "-include-response", "-threads", "500", "-timeout", "5", "-follow-redirects", "-follow-host-redirects", "-path", "/remote/login,/remote/index,/ssl-vpn/login.html", "-match-string", "ftnt-fortinet-grid", "-match-string", 'name="magic"', "-match-string", "SVPNCOOKIE", "-rate-limit", value("httpx-rate-limit"), ...lines("httpx-extra-args")];
    $("form-masscan-command").textContent = argv(masscan); $("form-httpx-command").textContent = argv(httpx);
  }
  document.querySelectorAll("#task-form input, #task-form select, #task-form textarea").forEach((field) => field.addEventListener("input", updateFormPreview));
  updateFormPreview();
  $("task-form").addEventListener("submit", async (event) => { event.preventDefault(); const form = new FormData(event.currentTarget); message("task-form-message", "Создаём TASK, физическую базу и сохраняем argv…"); try { const task = await request("/tasks", { method: "POST", body: form }); message("task-form-message", `TASK #${task.id} создан.`); event.currentTarget.reset(); updateFormPreview(); await refreshTasks(); await selectTask(task.id); } catch (error) { message("task-form-message", error.message, true); } });
  $("refresh-tasks").onclick = refreshTasks; $("refresh-metrics").onclick = refreshMetrics;
  document.querySelectorAll("[data-tab]").forEach((button) => button.addEventListener("click", () => { document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active")); button.classList.add("active"); state.tab = button.dataset.tab; loadTab(); }));
  $("delete-task").onclick = async () => { if (!state.task) return; try { await request(`/tasks/${state.task.id}`, { method: "DELETE" }); state.task = null; $("task-panel").classList.add("hidden"); await refreshTasks(); } catch (error) { message("detail-message", error.message, true); } };
  $("export-results").onclick = async () => { if (!state.task) return; try { const data = await request(`/tasks/${state.task.id}/export`); const blob = new Blob([data.targets.join("\n") + (data.targets.length ? "\n" : "")], { type: "text/plain;charset=utf-8" }); const link = document.createElement("a"); link.href = URL.createObjectURL(blob); link.download = `task-${state.task.id}-fortinet.txt`; link.click(); URL.revokeObjectURL(link.href); } catch (error) { message("detail-message", error.message, true); } };
  refreshTasks(); refreshMetrics(); state.metricsTimer = setInterval(refreshMetrics, 5000);
})();
