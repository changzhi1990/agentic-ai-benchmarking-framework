const state = {
  runs: [],
  report: null,
  selectedRun: "latest",
  plugins: null,
};

const chartStates = new Map();

const $ = (id) => document.getElementById(id);

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `${response.status} ${response.statusText}`);
  }
  return data;
}

async function refreshAll() {
  await refreshPlugins();
  await refreshRuns();
  await refreshStatus();
  await loadRun(state.selectedRun);
}

async function refreshPlugins() {
  const data = await requestJson("/api/plugins");
  state.plugins = data;
  fillPluginSelect("workloadSelect", data.workloads || []);
  fillPluginSelect("executorSelect", data.executors || []);
}

function fillPluginSelect(selectId, items) {
  const select = $(selectId);
  const previous = select.value;
  select.innerHTML = "";
  for (const item of items) {
    addOption(select, item.name, item.name);
  }
  if ([...select.options].some((item) => item.value === previous)) {
    select.value = previous;
  }
}

async function refreshRuns() {
  const data = await requestJson("/api/runs");
  state.runs = data.runs || [];
  const select = $("runSelect");
  const previous = select.value || state.selectedRun;
  select.innerHTML = "";
  addOption(select, "latest", "latest");
  for (const run of state.runs) {
    const label = run.display_name && run.display_name !== run.name ? `${run.display_name} - ${run.name}` : run.name;
    addOption(select, run.name, label);
  }
  select.value = [...select.options].some((item) => item.value === previous) ? previous : "latest";
  state.selectedRun = select.value;
}

async function refreshStatus() {
  const data = await requestJson("/api/status");
  const badge = $("statusBadge");
  const running = data.status && data.status.running;
  const vllmOk = data.vllm && data.vllm.ok;
  badge.className = `badge ${running || vllmOk ? "ok" : "warn"}`;
  badge.textContent = running ? "sweep running" : vllmOk ? "vLLM ready" : "vLLM unavailable";
}

async function loadRun(name) {
  state.selectedRun = name || "latest";
  const report = await requestJson(`/api/run?name=${encodeURIComponent(state.selectedRun)}`);
  state.report = report;
  renderReport(report);
}

function renderReport(report) {
  $("runTitle").textContent = report.name;
  $("runPath").textContent = report.path;
  $("kpiMaxAgents").textContent = report.overview.max_agents;
  $("kpiStable").textContent = report.overview.best_stable_agents;
  $("kpiCompleted").textContent = report.overview.total_completed;
  $("kpiFailed").textContent = report.overview.total_failed;
  $("kpiThroughput").textContent = fmt(report.overview.max_throughput_task_per_min_workload);
  renderResultsTable(report.points);
  renderTeamTables(report);
  renderFailures(report.points);
  renderPointSelect(report.points);
  drawChart("businessChart", report.points, [
    { key: "success_rate_pct", label: "success %", color: "#3aa675" },
    { key: "failed", label: "failed", color: "#d85f5f" },
    { key: "throughput_task_per_min_workload", label: "tasks/min", color: "#6da7ff" },
    { key: "lat_ok_p95_ms", label: "latency p95 s", color: "#d5a033", scale: 0.001 },
  ]);
  drawChart("cpuChart", report.points, [
    { key: "cpu_max_pct", label: "cpu max", color: "#d85f5f" },
    { key: "cpu_p95_pct", label: "cpu p95", color: "#b378ff" },
    { key: "dram_bw_max_gbps", label: "dram max", color: "#3aa675" },
    { key: "dram_bw_max_pct_of_peak", label: "dram %peak", color: "#d5a033" },
  ]);
  drawChart("gpuUtilChart", report.points, [
    { key: "gpu_util_p95_pct", label: "gpu active p95", color: "#6da7ff" },
    { key: "gpu_util_max_pct", label: "gpu active max", color: "#3aa675" },
    { key: "gpu_active_sample_pct", label: "gpu active samples", color: "#d5a033" },
    { key: "gpu_power_p95_w", label: "power p95 /4 W", color: "#d85f5f", scale: 0.25 },
  ]);
  drawChart("gpuMemoryChart", report.points, [
    { key: "gpu_mem_used_p95_mib", label: "mem used p95 MiB", color: "#54c6eb", scale: 1 / 1024 },
    { key: "gpu_mem_used_pct_p95", label: "mem used p95 %", color: "#e0c35a" },
    { key: "gpu_mem_used_pct_max", label: "mem used max %", color: "#f08ac0" },
    { key: "gpu_memctrl_p95_pct", label: "memctrl p95", color: "#d85f5f" },
    { key: "gpu_memctrl_max_pct", label: "memctrl max", color: "#ff8f6d" },
    { key: "gpu_memctrl_active_sample_pct", label: "memctrl active samples", color: "#f0a35a" },
  ]);
}

function renderTeamTables(report) {
  const agents = report.agents || [];
  const issues = report.issues || [];
  $("teamTable").innerHTML = agents.length
    ? table(
        ["agent", "status", "assigned", "verified", "context", "latency"],
        agents.map((agent) => [
          agent.agent_id || "-",
          agent.status || "-",
          (agent.assigned_issues || []).join(" "),
          agent.verified_success_issues ?? "-",
          agent.effective_context_length ?? "-",
          `${fmt(agent.latency_sec)} s`,
        ])
      )
    : `<div class="muted">No Agent Team v2 agent rows for this run.</div>`;
  $("issueTable").innerHTML = issues.length
    ? table(
        ["issue", "agent", "status", "rounds", "verified", "latency"],
        issues.map((issue) => [
          issue.issue_id || "-",
          issue.agent_id || "-",
          issue.status || "-",
          (issue.rounds || []).length,
          issue.verified ? "yes" : "no",
          `${fmt(issue.latency_sec)} s`,
        ])
      )
    : `<div class="muted">No Agent Team v2 issue rows for this run.</div>`;
}

function renderResultsTable(points) {
  $("resultsTable").innerHTML = table(
    [
      "Agents",
      "Context",
      "Mode",
      "Success",
      "Thr/Run",
      "Thr/Work",
      "Lat OK P95",
      "DRAM P95",
      "GPU Active Max",
      "SM Active P95",
      "Tensor Active P95",
      "DRAM Active P95",
      "GPU MemCtrl Max",
    ],
    points.map((p) => [
      p.agents,
      p.context_length || ((p.config.llm_context_kb || 0) * 1024) || "-",
      p.experiment_mode || p.config.experiment_mode || "-",
      `${fmt(p.success_rate_pct)}%`,
      fmt(p.throughput_task_per_min_run),
      fmt(p.throughput_task_per_min_workload),
      `${fmt(p.lat_ok_p95_ms)} ms`,
      `${fmt(p.dram_bw_p95_gbps)} GB/s`,
      `${fmt(p.gpu_util_max_pct)}%`,
      `${fmt(p.sm_active_p95_pct)}%`,
      `${fmt(p.tensor_active_p95_pct)}%`,
      `${fmt(p.dram_active_p95_pct)}%`,
      `${fmt(p.gpu_memctrl_max_pct)}%`,
    ])
  );
}

function renderFailures(points) {
  const failed = points.filter((p) => p.failed > 0);
  const target = $("failureList");
  if (!failed.length) {
    target.innerHTML = `<div class="muted">No failed tasks in this run.</div>`;
    return;
  }
  target.innerHTML = failed
    .map((p) => {
      const ids = p.failed_vm_ids && p.failed_vm_ids.length ? p.failed_vm_ids.join(" ") : "not listed";
      return `<div class="failure-item"><strong>agents=${p.agents}</strong> ${p.failed} failed task(s)<br><span class="muted">${escapeHtml(ids)}</span></div>`;
    })
    .join("");
}

function renderPointSelect(points) {
  const select = $("pointSelect");
  const previous = select.value;
  select.innerHTML = "";
  for (const point of points) {
    const label = point.case_id || `agents_${point.agents}`;
    addOption(select, String(point.agents), label);
  }
  if ([...select.options].some((item) => item.value === previous)) {
    select.value = previous;
  }
}

function table(headers, rows) {
  return `<table><thead><tr>${headers.map((h) => `<th>${escapeHtml(h)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((row) => `<tr>${row.map((cell) => `<td>${escapeHtml(String(cell))}</td>`).join("")}</tr>`)
    .join("")}</tbody></table>`;
}

function drawChart(canvasId, points, series, hoverIndex = null, hoverPoint = null) {
  const canvas = $(canvasId);
  const ctx = canvas.getContext("2d");
  const width = canvas.clientWidth || 600;
  if (!canvas.dataset.logicalHeight) {
    canvas.dataset.logicalHeight = canvas.getAttribute("height") || "220";
    canvas.style.height = `${canvas.dataset.logicalHeight}px`;
  }
  const height = Number(canvas.dataset.logicalHeight) || 220;
  const ratio = window.devicePixelRatio || 1;
  const pixelWidth = Math.round(width * ratio);
  const pixelHeight = Math.round(height * ratio);
  if (canvas.dataset.pixelWidth !== String(pixelWidth) || canvas.dataset.pixelHeight !== String(pixelHeight)) {
    canvas.width = pixelWidth;
    canvas.height = pixelHeight;
    canvas.dataset.pixelWidth = String(pixelWidth);
    canvas.dataset.pixelHeight = String(pixelHeight);
  }
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);
  paintChartBackground(ctx, width, height);
  const legendColumns = Math.max(1, Math.floor((width - 82) / 150));
  const legendRows = Math.ceil(series.length / legendColumns);
  const chartArea = {
    left: 58,
    right: 24,
    top: Math.max(34, 18 + legendRows * 16),
    bottom: 32,
  };
  const values = [];
  for (const item of series) {
    for (const point of points) {
      values.push(chartNumber(point[item.key]) * (item.scale || 1));
    }
  }
  const max = Math.max(...values, 1);
  drawYAxis(ctx, max, chartArea, width, height);
  drawXAxis(ctx, points, chartArea, width, height);
  ctx.fillStyle = "#9ba5b1";
  ctx.font = "12px system-ui";
  ctx.textAlign = "center";
  ctx.textBaseline = "alphabetic";
  points.forEach((point, index) => {
    const x = xPos(index, points.length, width, chartArea);
    ctx.fillText(String(point.agents), x, height - 10);
  });
  const renderedSeries = series.map((item) => {
    const coords = points.map((point, index) => {
      const rawValue = point[item.key];
      const value = chartNumber(point[item.key]) * (item.scale || 1);
      return {
        x: xPos(index, points.length, width, chartArea),
        y: yPos(value, max, chartArea, height),
        rawValue,
        value,
      };
    });
    drawSeriesArea(ctx, coords, item.color, chartArea, height);
    drawSmoothLine(ctx, coords, item.color);
    drawDataPoints(ctx, coords, item.color);
    return { item, coords };
  });

  renderedSeries.forEach(({ item }, sIndex) => {
    const legendColumn = sIndex % legendColumns;
    const legendRow = Math.floor(sIndex / legendColumns);
    ctx.fillStyle = item.color;
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    ctx.fillText(item.label, chartArea.left + legendColumn * 150, 18 + legendRow * 16);
  });

  chartStates.set(canvasId, { points, series, renderedSeries, chartArea, width, height, hoverIndex, hoverPoint });
  if (hoverIndex !== null) {
    drawHoverTooltip(ctx, hoverIndex, hoverPoint, points, renderedSeries, chartArea, width, height);
  }
  bindChartHover(canvas, canvasId);
}

function chartNumber(value) {
  if (value === null || value === undefined || value === "") return 0;
  const number = Number(value);
  return Number.isFinite(number) ? number : 0;
}

function paintChartBackground(ctx, width, height) {
  const background = ctx.createLinearGradient(0, 0, 0, height);
  background.addColorStop(0, "#151a1f");
  background.addColorStop(1, "#0d1013");
  ctx.fillStyle = background;
  ctx.fillRect(0, 0, width, height);
}

function drawYAxis(ctx, maxValue, chartArea, width, height) {
  const maxTickCount = 4;
  ctx.strokeStyle = "rgba(80, 92, 106, 0.45)";
  ctx.fillStyle = "#9ba5b1";
  ctx.lineWidth = 1;
  ctx.font = "11px system-ui";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";

  for (let i = 0; i <= maxTickCount; i += 1) {
    const value = maxValue - (maxValue * i) / maxTickCount;
    const y = yPos(value, maxValue, chartArea, height);
    line(ctx, chartArea.left, y, width - chartArea.right, y);
    ctx.fillText(formatAxisTick(value), chartArea.left - 8, y);
  }

  ctx.strokeStyle = "#46505c";
  line(ctx, chartArea.left, chartArea.top, chartArea.left, height - chartArea.bottom);
}

function drawXAxis(ctx, points, chartArea, width, height) {
  ctx.strokeStyle = "#46505c";
  line(ctx, chartArea.left, height - chartArea.bottom, width - chartArea.right, height - chartArea.bottom);
  if (points.length < 2) return;
  ctx.strokeStyle = "rgba(80, 92, 106, 0.22)";
  for (let index = 0; index < points.length; index += 1) {
    const x = xPos(index, points.length, width, chartArea);
    line(ctx, x, chartArea.top, x, height - chartArea.bottom);
  }
}

function drawSeriesArea(ctx, coords, color, chartArea, height) {
  if (!coords.length) return;
  const gradient = ctx.createLinearGradient(0, chartArea.top, 0, height - chartArea.bottom);
  gradient.addColorStop(0, `${color}38`);
  gradient.addColorStop(1, `${color}00`);
  ctx.fillStyle = gradient;
  ctx.beginPath();
  appendSmoothPath(ctx, coords);
  ctx.lineTo(coords[coords.length - 1].x, height - chartArea.bottom);
  ctx.lineTo(coords[0].x, height - chartArea.bottom);
  ctx.closePath();
  ctx.fill();
}

function drawSmoothLine(ctx, coords, color) {
  if (!coords.length) return;
  ctx.save();
  ctx.shadowColor = `${color}66`;
  ctx.shadowBlur = 10;
  ctx.strokeStyle = color;
  ctx.lineWidth = 2.4;
  ctx.lineJoin = "round";
  ctx.lineCap = "round";
  ctx.beginPath();
  appendSmoothPath(ctx, coords);
  ctx.stroke();
  ctx.restore();
}

function appendSmoothPath(ctx, coords) {
  ctx.moveTo(coords[0].x, coords[0].y);
  if (coords.length === 1) return;
  for (let index = 0; index < coords.length - 1; index += 1) {
    const current = coords[index];
    const next = coords[index + 1];
    const midX = (current.x + next.x) / 2;
    const midY = (current.y + next.y) / 2;
    ctx.quadraticCurveTo(current.x, current.y, midX, midY);
  }
  const last = coords[coords.length - 1];
  ctx.quadraticCurveTo(last.x, last.y, last.x, last.y);
}

function drawDataPoints(ctx, coords, color) {
  ctx.save();
  ctx.fillStyle = "#111417";
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  for (const coord of coords) {
    ctx.beginPath();
    ctx.arc(coord.x, coord.y, 3.4, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
  }
  ctx.restore();
}

function bindChartHover(canvas, canvasId) {
  if (canvas.dataset.hoverBound === "1") return;
  canvas.dataset.hoverBound = "1";
  canvas.addEventListener("mousemove", (event) => handleChartPointerMove(event, canvasId));
  canvas.addEventListener("mouseleave", () => handleChartPointerLeave(canvasId));
}

function handleChartPointerMove(event, canvasId) {
  const chart = chartStates.get(canvasId);
  if (!chart || chart.points.length === 0) return;
  const canvas = $(canvasId);
  const rect = canvas.getBoundingClientRect();
  const mouseX = event.clientX - rect.left;
  const mouseY = event.clientY - rect.top;
  const hoverPoint = {
    x: Math.min(Math.max(mouseX, chart.chartArea.left), chart.width - chart.chartArea.right),
    y: Math.min(Math.max(mouseY, chart.chartArea.top), chart.height - chart.chartArea.bottom),
  };
  let nearestIndex = 0;
  let nearestDistance = Number.POSITIVE_INFINITY;
  chart.renderedSeries[0].coords.forEach((coord, index) => {
    const distance = Math.abs(coord.x - mouseX);
    if (distance < nearestDistance) {
      nearestDistance = distance;
      nearestIndex = index;
    }
  });
  if (
    chart.hoverIndex === nearestIndex &&
    chart.hoverPoint &&
    Math.abs(chart.hoverPoint.x - hoverPoint.x) < 1 &&
    Math.abs(chart.hoverPoint.y - hoverPoint.y) < 1
  ) {
    return;
  }
  drawChart(canvasId, chart.points, chart.series, nearestIndex, hoverPoint);
}

function handleChartPointerLeave(canvasId) {
  const chart = chartStates.get(canvasId);
  if (!chart) return;
  drawChart(canvasId, chart.points, chart.series, null);
}

function drawHoverTooltip(ctx, hoverIndex, hoverPoint, points, renderedSeries, chartArea, width, height) {
  const firstSeries = renderedSeries[0];
  if (!firstSeries || !firstSeries.coords[hoverIndex]) return;
  const anchor = firstSeries.coords[hoverIndex];
  const tooltipAnchor = hoverPoint || anchor;
  ctx.save();
  ctx.strokeStyle = "rgba(230, 233, 237, 0.38)";
  ctx.lineWidth = 1;
  line(ctx, anchor.x, chartArea.top, anchor.x, height - chartArea.bottom);

  for (const { item, coords } of renderedSeries) {
    const coord = coords[hoverIndex];
    if (!coord) continue;
    ctx.fillStyle = item.color;
    ctx.beginPath();
    ctx.arc(coord.x, coord.y, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#111417";
    ctx.lineWidth = 2;
    ctx.stroke();
  }

  ctx.font = "11px system-ui";
  const title = `Agents ${points[hoverIndex].agents}`;
  const rows = renderedSeries.map(({ item, coords }) => ({
    color: item.color,
    label: item.label,
    value: fmt(coords[hoverIndex] ? coords[hoverIndex].value : 0),
  }));
  const tooltipWidth = Math.min(Math.max(
    ctx.measureText(title).width,
    ...rows.map((row) => ctx.measureText(`${row.label}: ${row.value}`).width)
  ) + 26, width - 16);
  const tooltipHeight = 24 + rows.length * 18;
  const { x, y } = placeTooltip(tooltipAnchor, tooltipWidth, tooltipHeight, width, height);
  ctx.fillStyle = "#111417";
  ctx.strokeStyle = "rgba(230, 233, 237, 0.2)";
  ctx.lineWidth = 1;
  ctx.beginPath();
  drawRoundedRect(ctx, x, y, tooltipWidth, tooltipHeight, 5);
  ctx.fill();
  ctx.stroke();
  ctx.fillStyle = "#e6e9ed";
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillText(title, x + 10, y + 13);
  rows.forEach((row, index) => {
    const rowY = y + 32 + index * 18;
    ctx.fillStyle = row.color;
    ctx.beginPath();
    ctx.arc(x + 11, rowY, 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#cfd6df";
    ctx.fillText(`${row.label}: ${row.value}`, x + 20, rowY);
  });
  ctx.restore();
}

function placeTooltip(anchor, tooltipWidth, tooltipHeight, width, height) {
  const gap = 12;
  const padding = 8;
  const rightSideX = anchor.x + gap;
  const leftSideX = anchor.x - tooltipWidth - gap;
  const preferredX = rightSideX + tooltipWidth <= width - padding ? rightSideX : leftSideX;
  return {
    x: clamp(preferredX, padding, Math.max(padding, width - tooltipWidth - padding)),
    y: clamp(anchor.y - tooltipHeight / 2, padding, Math.max(padding, height - tooltipHeight - padding)),
  };
}

function clamp(value, min, max) {
  if (max < min) return min;
  return Math.min(Math.max(value, min), max);
}

function drawRoundedRect(ctx, x, y, rectWidth, rectHeight, radius) {
  const r = Math.min(radius, rectWidth / 2, rectHeight / 2);
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + rectWidth - r, y);
  ctx.quadraticCurveTo(x + rectWidth, y, x + rectWidth, y + r);
  ctx.lineTo(x + rectWidth, y + rectHeight - r);
  ctx.quadraticCurveTo(x + rectWidth, y + rectHeight, x + rectWidth - r, y + rectHeight);
  ctx.lineTo(x + r, y + rectHeight);
  ctx.quadraticCurveTo(x, y + rectHeight, x, y + rectHeight - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function formatAxisTick(value) {
  const n = Number(value || 0);
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(1)}k`;
  if (Math.abs(n) >= 100) return n.toFixed(0);
  if (Math.abs(n) >= 10) return n.toFixed(1);
  return n.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function xPos(index, count, width, chartArea) {
  if (count <= 1) return width / 2;
  return chartArea.left + ((width - chartArea.left - chartArea.right) * index) / (count - 1);
}

function yPos(value, maxValue, chartArea, height) {
  const plotHeight = height - chartArea.top - chartArea.bottom;
  return height - chartArea.bottom - (value / maxValue) * plotHeight;
}

function line(ctx, x1, y1, x2, y2) {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
}

async function startSweep() {
  $("startMessage").textContent = "Starting sweep...";
  try {
    const payload = {
      agents: $("agentsInput").value,
      run_seconds: Number($("runSecondsInput").value),
      workload_grace_seconds: Number($("graceInput").value),
      memory_workers: Number($("memWorkersInput").value),
      memory_mb: Number($("memMbInput").value),
      vcpus_per_agent: Number($("vcpuInput").value),
      llm_context_kb: Number($("llmContextKbInput").value),
      llm_prompt_repeat: Number($("llmPromptRepeatInput").value),
      llm_max_tokens: Number($("llmMaxTokensInput").value),
      llm_load_mode: $("llmLoadModeSelect").value,
      llm_request_timeout_seconds: Number($("llmRequestTimeoutInput").value),
      llm_inter_task_sleep_ms: Number($("llmInterTaskSleepInput").value),
      run_name: $("runNameInput").value,
      workload: $("workloadSelect").value,
      executor: $("executorSelect").value,
      sudo_password: $("sudoInput").value,
    };
    const data = await requestJson("/api/sweep", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    $("startMessage").textContent = `Started ${data.status.sweep_root}`;
    await refreshStatus();
  } catch (error) {
    $("startMessage").textContent = error.message;
  }
}

async function stopSweep() {
  $("startMessage").textContent = "Stopping sweep...";
  try {
    await requestJson("/api/sweep/stop", { method: "POST" });
    $("startMessage").textContent = "Stop requested.";
    await refreshStatus();
  } catch (error) {
    $("startMessage").textContent = error.message;
  }
}

async function loadSelectedLog(type) {
  try {
    let url;
    if (type === "dashboard") {
      url = "/api/log?type=dashboard&lines=400";
    } else {
      url = `/api/log?run=${encodeURIComponent(state.selectedRun)}&point=${encodeURIComponent($("pointSelect").value)}&lines=400`;
    }
    const data = await requestJson(url);
    $("logView").textContent = data.exists ? data.lines.join("\n") : `Missing log: ${data.path}`;
  } catch (error) {
    $("logView").textContent = error.message;
  }
}

function addOption(select, value, text) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = text;
  select.appendChild(option);
}

function fmt(value) {
  if (value === null || value === undefined || value === "") return "-";
  const n = Number(value);
  if (!Number.isFinite(n)) return "-";
  if (Math.abs(n) >= 100) return n.toFixed(0);
  if (Math.abs(n) >= 10) return n.toFixed(1);
  return n.toFixed(2).replace(/0+$/, "").replace(/\.$/, "");
}

function escapeHtml(value) {
  return value.replace(/[&<>"']/g, (char) => {
    const map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return map[char];
  });
}

$("refreshButton").addEventListener("click", refreshAll);
$("runSelect").addEventListener("change", (event) => loadRun(event.target.value));
$("startButton").addEventListener("click", startSweep);
$("stopButton").addEventListener("click", stopSweep);
$("loadLogButton").addEventListener("click", () => loadSelectedLog("point"));
$("loadDashboardLogButton").addEventListener("click", () => loadSelectedLog("dashboard"));

refreshAll().catch((error) => {
  $("runTitle").textContent = "Dashboard error";
  $("runPath").textContent = error.message;
});

setInterval(() => {
  refreshStatus().catch(() => undefined);
}, 5000);
