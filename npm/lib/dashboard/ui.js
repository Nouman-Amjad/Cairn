/**
 * The dashboard shell.
 *
 * Server-renders the frame and the first paint of data, then `client.js`
 * takes over for live updates. No bundler, no framework, no CDN — the whole
 * UI is two files you can read, and it works with the network unplugged.
 */

const CSS = `
:root {
  color-scheme: dark;
  --bg:#0c0f14; --panel:#141922; --panel2:#1b2230; --line:#263041;
  --text:#e6edf6; --muted:#8b9ab0; --accent:#6ea8fe;
  --good:#4ade80; --warn:#fbbf24; --bad:#f87171;
  --mono: ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
a{color:var(--accent);text-decoration:none}
header{border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:5}
header .inner{max-width:1160px;margin:0 auto;padding:14px 20px;display:flex;
  align-items:center;gap:20px;flex-wrap:wrap}
header h1{font-size:17px;margin:0;letter-spacing:.02em}
header .sub{color:var(--muted);font-size:13px}
nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}
nav button{background:transparent;border:1px solid transparent;color:var(--muted);
  padding:6px 13px;border-radius:7px;cursor:pointer;font:inherit;font-size:14px}
nav button:hover{color:var(--text)}
nav button[aria-selected="true"]{background:var(--panel2);border-color:var(--line);color:var(--text)}
main{max-width:1160px;margin:0 auto;padding:22px 20px 80px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:14px;margin-bottom:22px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.card h3{margin:0 0 10px;font-size:13px;color:var(--muted);font-weight:600;
  text-transform:uppercase;letter-spacing:.06em}
.metric{font-size:34px;font-weight:600;line-height:1.1}
.metric small{font-size:14px;color:var(--muted);font-weight:400}
.good{color:var(--good)} .warn{color:var(--warn)} .bad{color:var(--bad)} .muted{color:var(--muted)}
.mono{font-family:var(--mono);font-size:12.5px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-family:var(--mono);
  font-size:11px;border:1px solid var(--line);margin:2px 4px 2px 0}
.pill.high{color:var(--bad);border-color:#7f1d1d;background:#2c0b0b}
.pill.medium{color:var(--warn);border-color:#78350f;background:#2c1a05}
.pill.low{color:var(--muted)}
.pill.up{color:var(--good);border-color:#14532d;background:#052e1a}
.pill.down{color:var(--bad);border-color:#7f1d1d;background:#2c0b0b}
.finding{border-bottom:1px solid var(--line);padding:12px 0;display:flex;gap:12px}
.finding:last-child{border-bottom:none}
.finding .body{flex:1;min-width:0}
.finding .title{font-weight:600}
.finding .detail{color:var(--muted);font-size:13.5px;margin-top:3px}
.finding .fix{margin-top:6px;font-size:13.5px}
.finding .fix b{color:var(--accent);font-weight:600}
.finding .where{font-family:var(--mono);font-size:11.5px;color:var(--muted);margin-top:4px}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line)}
th{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
td.key{font-family:var(--mono);font-size:12.5px;color:var(--muted);width:34%}
input,select,textarea{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  padding:7px 10px;border-radius:6px;font:inherit;font-size:14px;width:100%}
input:focus,textarea:focus{outline:2px solid var(--accent);outline-offset:-1px}
button.act{background:#1d4ed8;border:1px solid #1d4ed8;color:#fff;padding:8px 16px;
  border-radius:7px;cursor:pointer;font:inherit}
button.act:hover{filter:brightness(1.12)}
button.ghost{background:var(--panel2);border:1px solid var(--line);color:var(--text);
  padding:6px 13px;border-radius:7px;cursor:pointer;font:inherit;font-size:13px}
#logs{background:#0a0d12;border:1px solid var(--line);border-radius:8px;padding:10px;
  height:60vh;overflow-y:auto;font-family:var(--mono);font-size:12.5px}
.logline{padding:1px 0;white-space:pre-wrap;word-break:break-word;border-left:2px solid transparent;
  padding-left:8px}
.logline.error,.logline.fatal{color:#fca5a5;border-left-color:var(--bad)}
.logline.warn{color:#fcd34d;border-left-color:var(--warn)}
.logline.debug{color:var(--muted)}
.logline .src{color:var(--muted);margin-right:8px}
.chatlog{height:52vh;overflow-y:auto;padding:4px 2px;margin-bottom:12px}
.msg{margin-bottom:14px;white-space:pre-wrap;line-height:1.6}
.msg.you{color:var(--accent);font-weight:600}
.row{display:flex;gap:10px;align-items:center}
.toolbar{display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap}
.hide{display:none}
.empty{color:var(--muted);padding:26px 0;text-align:center}
.bar{height:6px;background:var(--panel2);border-radius:3px;overflow:hidden;margin-top:8px}
.bar span{display:block;height:100%}
`;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function scoreClass(score) {
  return score >= 85 ? "good" : score >= 60 ? "warn" : "bad";
}

export function render(state) {
  const { report, config } = state;
  const { bySeverity } = report.summary;

  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${escapeHtml(report.name)} — Cairn</title>
<style>${CSS}</style>
</head><body>
<header><div class="inner">
  <h1>Cairn</h1>
  <span class="sub">${escapeHtml(report.name)} · ${escapeHtml(report.root)}</span>
  <nav role="tablist">
    <button role="tab" data-tab="overview" aria-selected="true">Overview</button>
    <button role="tab" data-tab="findings" aria-selected="false">Findings</button>
    <button role="tab" data-tab="services" aria-selected="false">Services</button>
    <button role="tab" data-tab="logs" aria-selected="false">Logs</button>
    <button role="tab" data-tab="config" aria-selected="false">Config</button>
    <button role="tab" data-tab="chat" aria-selected="false">Chat</button>
  </nav>
</div></header>

<main>
  <section data-panel="overview">
    <div class="grid">
      <div class="card">
        <h3>Health score</h3>
        <div class="metric ${scoreClass(report.score)}" id="score">${report.score}<small>/100</small></div>
        <div class="bar"><span class="${scoreClass(report.score)}"
          style="width:${report.score}%;background:currentColor"></span></div>
      </div>
      <div class="card">
        <h3>Findings</h3>
        <div class="metric">${report.summary.total}</div>
        <div>
          <span class="pill high">${bySeverity.high || 0} high</span>
          <span class="pill medium">${bySeverity.medium || 0} medium</span>
          <span class="pill low">${bySeverity.low || 0} low</span>
        </div>
      </div>
      <div class="card">
        <h3>Stack</h3>
        <div>${report.stack.map((s) => `<span class="pill">${escapeHtml(s)}</span>`).join("") ||
          '<span class="muted">not detected</span>'}</div>
      </div>
      <div class="card">
        <h3>Files scanned</h3>
        <div class="metric">${report.files.total}</div>
        <div class="muted mono">${(report.files.bytes / 1048576).toFixed(1)} MB</div>
      </div>
    </div>

    <div class="card">
      <h3>Detected services</h3>
      ${report.services.length
        ? `<table><thead><tr><th>Name</th><th>Kind</th><th>Port</th><th>Source</th></tr></thead><tbody>
          ${report.services.map((s) => `<tr>
            <td>${escapeHtml(s.name)}</td><td class="mono">${escapeHtml(s.kind)}</td>
            <td class="mono">${s.port ?? "—"}</td><td class="mono muted">${escapeHtml(s.source)}</td>
          </tr>`).join("")}
        </tbody></table>`
        : '<div class="empty">No services detected. Cairn looks at Dockerfiles, compose files and package.json.</div>'}
    </div>

    <div class="card" style="margin-top:14px">
      <h3>Analysis</h3>
      <div class="muted">Generated ${escapeHtml(report.generatedAt)} in ${report.durationMs} ms.</div>
      <div class="row" style="margin-top:12px">
        <button class="act" id="refresh">Re-analyse</button>
        <span class="muted mono" id="refresh-status"></span>
      </div>
    </div>
  </section>

  <section data-panel="findings" class="hide">
    <div class="toolbar">
      <select id="sev-filter" style="width:auto">
        <option value="all">All severities</option>
        <option value="high">High only</option>
        <option value="medium">Medium and above</option>
      </select>
      <span class="muted mono" id="finding-count"></span>
    </div>
    <div class="card" id="findings"></div>
  </section>

  <section data-panel="services" class="hide">
    <div class="toolbar">
      <button class="ghost" id="probe">Probe health endpoints</button>
      <span class="muted">Configured under <code class="mono">services</code> in cairn.config.json</span>
    </div>
    <div class="card" id="service-health">
      <div class="empty">Not probed yet.</div>
    </div>
  </section>

  <section data-panel="logs" class="hide">
    <div class="toolbar">
      <select id="log-filter" style="width:auto">
        <option value="all">All levels</option>
        <option value="error">Errors</option>
        <option value="warn">Warnings</option>
      </select>
      <label class="row muted" style="width:auto">
        <input type="checkbox" id="follow" checked style="width:auto"> follow
      </label>
      <span class="muted mono" id="log-stats"></span>
    </div>
    <div id="logs"><div class="empty">Waiting for log lines…</div></div>
  </section>

  <section data-panel="config" class="hide">
    <div class="card">
      <h3>cairn.config.json</h3>
      <div class="muted mono" style="margin-bottom:12px">${escapeHtml(config._path)}</div>
      <table><thead><tr><th>Key</th><th>Value</th><th></th></tr></thead>
      <tbody id="config-rows"></tbody></table>
    </div>
  </section>

  <section data-panel="chat" class="hide">
    <div class="card">
      <h3>Ask about this project — mode: <span class="mono">${escapeHtml(config.chat.mode)}</span></h3>
      <div class="chatlog" id="chatlog">
        <div class="msg muted">Ask what is wrong, what the errors mean, or what to fix first.</div>
      </div>
      <form class="row" id="chatform">
        <input id="q" placeholder="why are there errors in the logs?" autocomplete="off">
        <button class="act" type="submit">Ask</button>
      </form>
    </div>
  </section>
</main>

<script>window.__CAIRN__ = ${JSON.stringify({
    report: state.report,
    chatMode: config.chat.mode,
  }).replace(/</g, "\\u003c")};</script>
<script type="module" src="/app.js"></script>
</body></html>`;
}
