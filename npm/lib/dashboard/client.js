/**
 * Dashboard client.
 *
 * Plain ES modules served straight from disk — no bundler, no framework. The
 * whole thing is small enough to read in one sitting, which is the point: a
 * dashboard you cannot debug is a dashboard you stop trusting.
 */

const state = {
  report: window.__CAIRN__.report,
  logs: [],
  follow: true,
  logFilter: "all",
  sevFilter: "all",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}

// ------------------------------------------------------------------- tabs

function showTab(name) {
  $$("nav button").forEach((button) =>
    button.setAttribute("aria-selected", String(button.dataset.tab === name)),
  );
  $$("section[data-panel]").forEach((section) =>
    section.classList.toggle("hide", section.dataset.panel !== name),
  );
  if (name === "logs") loadLogs();
  if (name === "config") loadConfig();
  location.hash = name;
}

$$("nav button").forEach((button) =>
  button.addEventListener("click", () => showTab(button.dataset.tab)),
);

// --------------------------------------------------------------- findings

const SEVERITY_RANK = { high: 0, medium: 1, low: 2 };

function renderFindings() {
  const limit =
    state.sevFilter === "all" ? 2 : state.sevFilter === "medium" ? 1 : 0;
  const shown = state.report.findings.filter((f) => SEVERITY_RANK[f.severity] <= limit);
  $("#finding-count").textContent = `${shown.length} of ${state.report.findings.length}`;

  $("#findings").innerHTML = shown.length
    ? shown
        .map(
          (f) => `<div class="finding">
        <span class="pill ${f.severity}">${f.severity}</span>
        <div class="body">
          <div class="title">${escapeHtml(f.title)}</div>
          <div class="detail">${escapeHtml(f.detail)}</div>
          <div class="fix"><b>Fix:</b> ${escapeHtml(f.fix)}</div>
          <div class="where">${escapeHtml(f.id)}${f.file ? ` · ${escapeHtml(f.file)}` : ""}</div>
        </div>
      </div>`,
        )
        .join("")
    : '<div class="empty">Nothing at this severity. That is the good outcome.</div>';
}

$("#sev-filter").addEventListener("change", (event) => {
  state.sevFilter = event.target.value;
  renderFindings();
});

$("#refresh").addEventListener("click", async () => {
  const status = $("#refresh-status");
  status.textContent = "analysing…";
  const response = await fetch("/api/report/refresh", { method: "POST" });
  state.report = await response.json();
  status.textContent = `done in ${state.report.durationMs} ms`;
  applyReport();
  setTimeout(() => (status.textContent = ""), 4000);
});

function applyReport() {
  const score = $("#score");
  score.textContent = state.report.score;
  score.insertAdjacentHTML("beforeend", "<small>/100</small>");
  score.className = `metric ${
    state.report.score >= 85 ? "good" : state.report.score >= 60 ? "warn" : "bad"
  }`;
  renderFindings();
}

// ------------------------------------------------------------------- logs

function logMatches(entry) {
  return state.logFilter === "all" || entry.level === state.logFilter;
}

function appendLog(entry) {
  const container = $("#logs");
  if (container.querySelector(".empty")) container.innerHTML = "";
  if (!logMatches(entry)) return;

  const line = document.createElement("div");
  line.className = `logline ${entry.level}`;
  line.innerHTML =
    `<span class="src">${escapeHtml(entry.source)}</span>${escapeHtml(entry.line)}`;
  container.appendChild(line);

  // Trim the DOM, not just the buffer: 50k nodes makes the tab unusable.
  while (container.children.length > 2000) container.removeChild(container.firstChild);
  if (state.follow) container.scrollTop = container.scrollHeight;
}

async function loadLogs() {
  const response = await fetch(`/api/logs?limit=500&level=${state.logFilter}`);
  const body = await response.json();
  const container = $("#logs");
  container.innerHTML = "";
  if (!body.entries.length) {
    container.innerHTML =
      '<div class="empty">No log lines yet. Add sources under <code>logs.files</code> ' +
      "or <code>logs.commands</code> in the Config tab.</div>";
  } else {
    for (const entry of body.entries) appendLog(entry);
  }
  const counts = body.stats.counts;
  $("#log-stats").textContent =
    `${body.stats.total} lines · ${counts.error + counts.fatal} errors · ${counts.warn} warnings`;
}

$("#log-filter").addEventListener("change", (event) => {
  state.logFilter = event.target.value;
  loadLogs();
});
$("#follow").addEventListener("change", (event) => (state.follow = event.target.checked));

// ----------------------------------------------------------------- config

function flatten(object, prefix = "") {
  const rows = [];
  for (const [key, value] of Object.entries(object)) {
    if (key.startsWith("_")) continue;
    const path = prefix ? `${prefix}.${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      rows.push(...flatten(value, path));
    } else {
      rows.push([path, value]);
    }
  }
  return rows;
}

async function loadConfig() {
  const response = await fetch("/api/config");
  const { config } = await response.json();
  $("#config-rows").innerHTML = flatten(config)
    .map(
      ([key, value]) => `<tr>
        <td class="key">${escapeHtml(key)}</td>
        <td><input data-key="${escapeHtml(key)}" value="${escapeHtml(
          Array.isArray(value) ? value.join(", ") : value,
        )}"></td>
        <td><button class="ghost" data-save="${escapeHtml(key)}">Save</button></td>
      </tr>`,
    )
    .join("");

  $$("[data-save]").forEach((button) =>
    button.addEventListener("click", async () => {
      const key = button.dataset.save;
      const input = document.querySelector(`input[data-key="${CSS.escape(key)}"]`);
      button.textContent = "…";
      const result = await fetch("/api/config", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ key, value: input.value }),
      }).then((r) => r.json());
      button.textContent = result.error ? "Failed" : "Saved";
      if (result.error) {
        input.style.outline = "2px solid var(--bad)";
        button.title = result.error;
      } else {
        input.style.outline = "";
      }
      setTimeout(() => (button.textContent = "Save"), 2000);
    }),
  );
}

// --------------------------------------------------------------- services

$("#probe").addEventListener("click", async () => {
  const target = $("#service-health");
  target.innerHTML = '<div class="empty">Probing…</div>';
  const { services } = await fetch("/api/services").then((r) => r.json());
  target.innerHTML = services.length
    ? `<table><thead><tr><th>Service</th><th>Status</th><th>Latency</th><th>Detail</th></tr></thead>
      <tbody>${services
        .map(
          (s) => `<tr>
        <td>${escapeHtml(s.name)}</td>
        <td><span class="pill ${s.status === "up" ? "up" : "down"}">${escapeHtml(s.status)}</span></td>
        <td class="mono">${s.latencyMs != null ? `${s.latencyMs} ms` : "—"}</td>
        <td class="mono muted">${escapeHtml(s.error || s.code || s.url || "")}</td>
      </tr>`,
        )
        .join("")}</tbody></table>`
    : '<div class="empty">No services configured. Add them under <code>services</code> in the Config tab.</div>';
});

// ------------------------------------------------------------------- chat

$("#chatform").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = $("#q");
  const question = input.value.trim();
  if (!question) return;

  const log = $("#chatlog");
  log.insertAdjacentHTML("beforeend", `<div class="msg you">${escapeHtml(question)}</div>`);
  input.value = "";
  const pending = document.createElement("div");
  pending.className = "msg muted";
  pending.textContent = "thinking…";
  log.appendChild(pending);
  log.scrollTop = log.scrollHeight;

  try {
    const { answer } = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    }).then((r) => r.json());
    // Minimal markdown: bold, inline code, and line breaks. Anything more
    // would mean shipping a parser to render six formatting characters.
    pending.className = "msg";
    pending.innerHTML = escapeHtml(answer)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, '<code class="mono">$1</code>');
  } catch (error) {
    pending.className = "msg bad";
    pending.textContent = `Failed: ${error.message}`;
  }
  log.scrollTop = log.scrollHeight;
});

// ------------------------------------------------------------------ live

const events = new EventSource("/events");
events.addEventListener("log", (event) => appendLog(JSON.parse(event.data)));
events.addEventListener("report", (event) => {
  const data = JSON.parse(event.data);
  state.report.score = data.score;
  state.report.summary = data.summary;
});

// ------------------------------------------------------------------ boot

renderFindings();
showTab(location.hash.replace("#", "") || "overview");
