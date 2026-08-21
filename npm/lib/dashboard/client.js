/**
 * Dashboard client.
 *
 * Plain ES modules served straight from disk — no bundler, no framework. The
 * whole thing is small enough to read in one sitting, which is the point: a
 * dashboard you cannot debug is a dashboard you stop trusting.
 *
 * Motion lives here rather than in CSS wherever it needs a measurement: the
 * nav pill needs the width of a button, the dial needs an arc length, the
 * tilt needs a pointer position. Everything else is a class the stylesheet
 * already knows how to animate.
 */

import {
  donut, legend, barsH, area, bucketByTime, humanSpan, SEVERITY_COLOR,
} from "/charts.js";

const state = {
  report: window.__CAIRN__.report,
  logs: [],
  follow: true,
  logFilter: "all",
  sevFilter: "all",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

// One check, read once. Someone who has asked the OS to stop animating things
// should not have to ask twice.
const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = String(value ?? "");
  return div.innerHTML;
}

const icon = (name, cls = "i-sm") =>
  `<svg class="i ${cls}" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-${name}"/></svg>`;

const SEVERITY_ICON = { high: "shield", medium: "warn", low: "info" };

// ------------------------------------------------------------------- tabs

const navPill = $("#nav-pill");

function moveNavPill(button) {
  if (!button) return;
  navPill.style.width = `${button.offsetWidth}px`;
  navPill.style.transform = `translateX(${button.offsetLeft}px)`;
}

function showTab(name) {
  const target = $$("nav button").find((b) => b.dataset.tab === name);
  $$("nav button").forEach((button) =>
    button.setAttribute("aria-selected", String(button.dataset.tab === name)),
  );
  $$("section[data-panel]").forEach((section) => {
    const active = section.dataset.panel === name;
    section.classList.toggle("hide", !active);
    // Re-trigger the panel entrance. Removing and re-adding in the same frame
    // does nothing, so force a reflow between the two.
    if (active && !still) {
      section.style.animation = "none";
      void section.offsetWidth;
      section.style.animation = "";
    }
  });
  moveNavPill(target);
  if (name === "logs") loadLogs();
  if (name === "config") loadConfig();
  location.hash = name;
}

$$("nav button").forEach((button) =>
  button.addEventListener("click", () => showTab(button.dataset.tab)),
);

// The pill is positioned from a measurement, so it has to be re-measured when
// the layout changes under it.
addEventListener("resize", () =>
  moveNavPill($$("nav button").find((b) => b.getAttribute("aria-selected") === "true")),
);

// ----------------------------------------------------------------- motion

/**
 * Pointer-tracked tilt and specular highlight.
 *
 * Two custom properties carry the pointer position into the stylesheet, which
 * owns what to do with it. The rotation is deliberately small: past about six
 * degrees a card stops reading as a surface catching light and starts reading
 * as a page that will not sit still.
 */
function bindTilt(card) {
  if (still) return;
  card.addEventListener("pointermove", (event) => {
    const box = card.getBoundingClientRect();
    const x = (event.clientX - box.left) / box.width;
    const y = (event.clientY - box.top) / box.height;
    card.style.setProperty("--mx", `${x * 100}%`);
    card.style.setProperty("--my", `${y * 100}%`);
    card.style.transform =
      `perspective(1200px) rotateY(${(x - 0.5) * 6}deg) ` +
      `rotateX(${(0.5 - y) * 6}deg) translateZ(6px)`;
  });
  card.addEventListener("pointerleave", () => {
    card.style.transform = "";
  });
}

$$(".card.tilt").forEach(bindTilt);

/** Count a number up to its target. Purely a way to draw the eye to it. */
function countUp(element, to, ms = 900) {
  if (still || to === 0) {
    element.textContent = String(to);
    return;
  }
  const start = performance.now();
  const from = Number(element.textContent) || 0;
  const step = (now) => {
    const t = Math.min(1, (now - start) / ms);
    const eased = 1 - Math.pow(1 - t, 3);
    element.textContent = String(Math.round(from + (to - from) * eased));
    if (t < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

const ARC = 2 * Math.PI * 52; // the dial circle's r, kept in sync with ui.js
const DIAL_STOPS = {
  good: ["#3ddc97", "#3fd2ff"],
  warn: ["#ffc247", "#ff9f45"],
  bad: ["#ff6b81", "#ff4d6d"],
};

function drawDial(score) {
  const cls = score >= 85 ? "good" : score >= 60 ? "warn" : "bad";
  const dial = $("#dial");
  const [g1, g2] = DIAL_STOPS[cls];
  dial.style.setProperty("--g1", g1);
  dial.style.setProperty("--g2", g2);
  $("#score-arc").style.strokeDashoffset = String(ARC * (1 - score / 100));
  $("#score").className = cls;
  countUp($("#score"), score);
}

// Kick the entrance off after first paint so the transition has a frame to
// animate from, rather than starting at its final value.
requestAnimationFrame(() => {
  drawDial(state.report.score);
  $$("[data-count]").forEach((element) => countUp(element, Number(element.dataset.count)));
  moveNavPill($$("nav button").find((b) => b.getAttribute("aria-selected") === "true"));
});

// A hairline and a shadow appear once the page has scrolled under the header,
// so the bar separates from the content only when it needs to.
const header = document.querySelector("header");
addEventListener(
  "scroll",
  () => header.classList.toggle("stuck", window.scrollY > 8),
  { passive: true },
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
          (f, index) => `<div class="finding ${f.severity}" style="--i:${index}">
        <div class="rail">${icon(SEVERITY_ICON[f.severity], "")}</div>
        <div class="body">
          <div class="title">${escapeHtml(f.title)}
            <span class="sev">${f.severity}</span>
          </div>
          <div class="detail">${escapeHtml(f.detail)}</div>
          <div class="fix">${icon("check")}<span><b>Fix:</b> ${escapeHtml(f.fix)}</span></div>
          <div class="where">${escapeHtml(f.id)}${f.file ? ` · ${escapeHtml(f.file)}` : ""}</div>
        </div>
      </div>`,
        )
        .join("")
    : `<div class="empty">${icon("check", "")}
       <div>Nothing at this severity. That is the good outcome.</div></div>`;
}

$("#sev-filter").addEventListener("change", (event) => {
  state.sevFilter = event.target.value;
  renderFindings();
});

$("#refresh").addEventListener("click", async () => {
  const button = $("#refresh");
  const status = $("#refresh-status");
  button.disabled = true;
  status.textContent = "analysing…";
  try {
    const response = await fetch("/api/report/refresh", { method: "POST" });
    state.report = await response.json();
    status.textContent = `done in ${state.report.durationMs} ms`;
    applyReport();
  } catch (error) {
    status.textContent = `failed: ${error.message}`;
  } finally {
    button.disabled = false;
    setTimeout(() => (status.textContent = ""), 4000);
  }
});

function applyReport() {
  drawDial(state.report.score);
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
      `<div class="empty">${icon("terminal", "")}<div>No log lines yet. Add sources under ` +
      "<code>logs.files</code> or <code>logs.commands</code> in the Config tab.</div></div>";
  } else {
    for (const entry of body.entries) appendLog(entry);
  }
  const counts = body.stats.counts;
  $("#log-stats").textContent =
    `${body.stats.total} lines · ${counts.error + counts.fatal} errors · ${counts.warn} warnings`;
  drawLogCharts(body.entries, counts);
}

/**
 * Volume over time, and the level mix.
 *
 * The volume chart says which clock it used. A tail of a static file has no
 * meaningful time axis, and a chart that quietly pretends otherwise is worse
 * than no chart.
 */
function drawLogCharts(entries, counts) {
  const bucketed = bucketByTime(entries, 34);
  $("#log-volume").innerHTML = area(bucketed.values, { label: "log lines" });
  $("#log-volume-note").innerHTML = bucketed.values.length
    ? `${icon("info")}<span>${
        bucketed.byPosition
          ? "Buckets are by position, not time: every line arrived in the same instant."
          : `Buckets span ${humanSpan(bucketed.spanMs)}, from timestamps parsed in ` +
            `${bucketed.parsed} of ${entries.length} lines.`
      }</span>`
    : "";

  const segments = [
    { label: "error", value: (counts.error || 0) + (counts.fatal || 0), color: "var(--bad)" },
    { label: "warn", value: counts.warn || 0, color: "var(--warn)" },
    { label: "info", value: counts.info || 0, color: "var(--a1)" },
    { label: "debug", value: counts.debug || 0, color: "var(--muted)" },
  ];
  const total = segments.reduce((sum, s) => sum + s.value, 0);
  $("#log-levels").innerHTML =
    donut(segments, { size: 122, centre: `<b>${total}</b><span>lines</span>` }) +
    legend(segments.filter((s) => s.value > 0));
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
        <td><input data-key="${escapeHtml(key)}" aria-label="${escapeHtml(key)}" value="${escapeHtml(
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
      button.innerHTML = "…";
      const result = await fetch("/api/config", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ key, value: input.value }),
      }).then((r) => r.json());
      if (result.error) {
        button.innerHTML = `${icon("close")}Failed`;
        input.style.borderColor = "var(--bad)";
        button.title = result.error;
      } else {
        button.innerHTML = `${icon("check")}Saved`;
        input.style.borderColor = "";
      }
      setTimeout(() => (button.textContent = "Save"), 2000);
    }),
  );
}

// --------------------------------------------------------------- services

$("#probe").addEventListener("click", async () => {
  const target = $("#service-health");
  const charts = $("#service-charts");
  target.innerHTML = `<div class="empty">${icon("activity", "")}<div>Probing…</div></div>`;
  charts.innerHTML = "";
  const { services } = await fetch("/api/services").then((r) => r.json());

  // Only services that actually answered get a latency bar. A refused
  // connection still records an elapsed time, and charting that would draw a
  // dead service as the fastest one on the board.
  const answered = services.filter((s) => s.status === "up" && s.latencyMs != null);
  const up = services.filter((s) => s.status === "up").length;
  charts.innerHTML = services.length
    ? `<div class="grid two">
        <div class="card"><h3>${icon("activity", "i-sm")} Response time</h3>
          ${
            answered.length
              ? barsH(
                  answered
                    .map((s) => ({
                      label: s.name,
                      value: s.latencyMs,
                      color: s.latencyMs < 100 ? "var(--good)"
                        : s.latencyMs < 500 ? "var(--warn)" : "var(--bad)",
                    }))
                    .sort((a, b) => b.value - a.value),
                  { unit: "ms", raw: true },
                )
              : `<div class="chart-empty">Nothing answered</div>`
          }
        </div>
        <div class="card"><h3>${icon("server", "i-sm")} Availability</h3>
          <div class="chart-row">
            ${donut(
              [
                { label: "up", value: up, color: "var(--good)" },
                { label: "down", value: services.length - up, color: "var(--bad)" },
              ],
              { size: 122, centre: `<b>${up}/${services.length}</b><span>up</span>` },
            )}
            ${legend([
              { label: "up", value: up, color: "var(--good)" },
              { label: "down", value: services.length - up, color: "var(--bad)" },
            ])}
          </div>
        </div>
      </div>`
    : "";

  target.innerHTML = services.length
    ? `<div class="t-wrap"><table>
      <thead><tr><th>Service</th><th>Status</th><th>Latency</th><th>Detail</th></tr></thead>
      <tbody>${services
        .map(
          (s) => `<tr>
        <td>${icon("box")}${escapeHtml(s.name)}</td>
        <td><span class="pill ${s.status === "up" ? "up" : "down"}">
          ${icon(s.status === "up" ? "check" : "close")}${escapeHtml(s.status)}</span></td>
        <td class="mono">${s.latencyMs != null ? `${s.latencyMs} ms` : "—"}</td>
        <td class="mono faint">${escapeHtml(s.error || s.code || s.url || "")}</td>
      </tr>`,
        )
        .join("")}</tbody></table></div>`
    : `<div class="empty">${icon("server", "")}<div>No services configured.
       Add them under <code>services</code> in the Config tab.</div></div>`;
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
  pending.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>';
  log.appendChild(pending);
  log.scrollTop = log.scrollHeight;

  try {
    const { answer } = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ question }),
    }).then((r) => r.json());
    // Minimal markdown: bold, italic, inline code, and line breaks. Anything
    // more would mean shipping a parser to render six formatting characters.
    // Applied after escaping, so the input is already inert.
    pending.className = "msg";
    pending.innerHTML = escapeHtml(answer)
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/_([^_\n]+)_/g, "<em>$1</em>");
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
  drawDial(data.score);
});
events.addEventListener("open", () => {
  $("#live").classList.remove("off");
  $("#live-text").textContent = "live";
});
events.addEventListener("error", () => {
  $("#live").classList.add("off");
  $("#live-text").textContent = "offline";
});

// ------------------------------------------------------------------ boot

renderFindings();
showTab(location.hash.replace("#", "") || "overview");
