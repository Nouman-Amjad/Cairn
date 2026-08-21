/**
 * Ask a question about the project and its logs.
 *
 * Three modes, and the default is the one that needs nothing:
 *
 *   offline  deterministic correlation over the analysis and the log buffer.
 *            No network, no key, no cost. Answers the questions that are
 *            actually pattern matching rather than reasoning.
 *   api      a model API directly, given the analysis and recent logs.
 *   gateway  a running Cairn deployment — the full agent loop, tool calls
 *            against real observability backends, and approval gates.
 *
 * Offline is not a toy fallback. "Which errors spiked, and was there a deploy
 * near that time" is a join, not an inference, and answering it without a
 * model is faster and cheaper than answering it with one.
 */

import { classify } from "./logs.js";

const SYSTEM = `You are Cairn, a platform analysis assistant.
You are given a static analysis of a project and a window of its recent logs.

Rules:
- Ground every claim in the supplied analysis or log lines. Quote the line.
- If the data does not support an answer, say so plainly. An honest "the logs
  do not show this" is more useful than a confident guess.
- Be concise. The reader is probably mid-incident.
- Never invent a file path, a metric name or a log line.`;

/** Group similar log lines so a thousand repeats read as one fact. */
export function cluster(entries, limit = 8) {
  const groups = new Map();
  for (const entry of entries) {
    // Normalise the parts that differ per occurrence: numbers, uuids, hex ids,
    // quoted strings and timestamps. What remains is the message template.
    const key = entry.line
      .replace(/\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b/gi, "<uuid>")
      .replace(/\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\S*/g, "<ts>")
      .replace(/\b[0-9a-f]{7,}\b/gi, "<hex>")
      .replace(/\d+/g, "<n>")
      .replace(/"[^"]*"/g, '"<str>"')
      .trim()
      .slice(0, 200);
    const existing = groups.get(key);
    if (existing) {
      existing.count += 1;
      existing.last = entry.at;
    } else {
      groups.set(key, {
        template: key,
        example: entry.line.slice(0, 300),
        level: entry.level,
        source: entry.source,
        count: 1,
        first: entry.at,
        last: entry.at,
      });
    }
  }
  return [...groups.values()].sort((a, b) => b.count - a.count).slice(0, limit);
}

// ------------------------------------------------------------------ offline

export function offlineAnswer(question, { report, logs }) {
  const asked = question.toLowerCase();
  const entries = logs ? logs.recent(1000) : [];
  const problems = entries.filter((e) => e.level === "error" || e.level === "fatal");
  const lines = [];

  const wantsLogs = /log|error|fail|crash|exception|why|wrong|broke|spike/.test(asked);
  const wantsConfig = /config|setting|value|port|option/.test(asked);
  const wantsSecurity = /secur|secret|risk|vulnerab|safe|expos/.test(asked);
  const wantsStack = /stack|tech|what is|what does|overview|architecture/.test(asked);

  if (wantsStack || (!wantsLogs && !wantsConfig && !wantsSecurity)) {
    lines.push(
      `**${report.name}** — ${report.stack.join(", ") || "stack not detected"}.`,
      `${report.files.total} files, ${report.services.length} service(s) detected` +
        `${report.services.length ? `: ${report.services.map((s) => s.name).join(", ")}` : ""}.`,
    );
  }

  if (wantsSecurity || !wantsLogs) {
    const high = report.findings.filter((f) => f.severity === "high");
    if (high.length) {
      lines.push("", `**${high.length} high-severity finding(s):**`);
      for (const f of high.slice(0, 5)) {
        lines.push(`- ${f.title}${f.file ? ` (${f.file})` : ""} — ${f.fix}`);
      }
    } else if (wantsSecurity) {
      lines.push("", "No high-severity findings in the current analysis.");
    }
  }

  if (wantsLogs) {
    if (!entries.length) {
      lines.push(
        "",
        "No logs are being collected. Add sources under `logs.files` or " +
          "`logs.commands` in cairn.config.json and they will appear here.",
      );
    } else if (!problems.length) {
      lines.push("", `No errors in the last ${entries.length} lines collected.`);
    } else {
      const clusters = cluster(problems, 5);
      lines.push("", `**${problems.length} error line(s)**, grouped:`);
      for (const group of clusters) {
        lines.push(`- ${group.count}x \`${group.example}\` (${group.source})`);
      }
      const deployNote = correlateWithFindings(clusters, report);
      if (deployNote) lines.push("", deployNote);
    }
  }

  if (wantsConfig) {
    lines.push(
      "",
      "Configuration lives in `cairn.config.json`. Change values from the " +
        "Config tab of the dashboard, or `cairn config set <key> <value>`.",
    );
  }

  lines.push(
    "",
    "_Answered offline from the analysis and log buffer. For an agent that " +
      "queries your real metrics and traces, set `chat.mode` to `api` or `gateway`._",
  );
  return lines.join("\n");
}

/** Very small correlation: do error templates mention anything the analysis flagged? */
function correlateWithFindings(clusters, report) {
  const hits = [];
  for (const group of clusters) {
    for (const f of report.findings) {
      const token = (f.file || "").split("/").pop();
      if (token && token.length > 3 && group.example.includes(token)) {
        hits.push(`\`${token}\` appears in both the errors and the finding "${f.title}"`);
      }
    }
  }
  return hits.length ? `Possibly related: ${hits.slice(0, 3).join("; ")}.` : null;
}

// ---------------------------------------------------------------------- api

function buildContext({ report, logs }) {
  const entries = logs ? logs.recent(400) : [];
  const problems = entries.filter((e) => e.level === "error" || e.level === "fatal");
  return [
    `PROJECT: ${report.name}`,
    `STACK: ${report.stack.join(", ") || "unknown"}`,
    `SERVICES: ${report.services.map((s) => s.name).join(", ") || "none detected"}`,
    `SCORE: ${report.score}/100`,
    "",
    "FINDINGS:",
    ...report.findings
      .slice(0, 25)
      .map((f) => `- [${f.severity}] ${f.id}: ${f.title}${f.file ? ` (${f.file})` : ""}`),
    "",
    `RECENT LOG SUMMARY (${entries.length} lines held, ${problems.length} at error or above):`,
    ...cluster(problems, 12).map((g) => `- ${g.count}x [${g.level}] ${g.example}`),
  ].join("\n");
}

async function askApi(question, context, config) {
  const key = process.env[config.chat.apiKeyEnv];
  if (!key) {
    throw new Error(
      `chat.mode is "api" but ${config.chat.apiKeyEnv} is not set. ` +
        `Export it, or set chat.mode to "offline".`,
    );
  }
  const response = await fetch(config.chat.apiUrl, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-api-key": key,
      "anthropic-version": "2023-06-01",
    },
    body: JSON.stringify({
      model: config.chat.model,
      max_tokens: config.chat.maxTokens,
      system: SYSTEM,
      messages: [{ role: "user", content: `${context}\n\nQUESTION: ${question}` }],
    }),
  });
  if (!response.ok) {
    throw new Error(`model API returned ${response.status}: ${(await response.text()).slice(0, 300)}`);
  }
  const body = await response.json();
  return (body.content || [])
    .filter((block) => block.type === "text")
    .map((block) => block.text)
    .join("")
    .trim();
}

async function askGateway(question, config) {
  const base = config.chat.gatewayUrl.replace(/\/$/, "");
  const response = await fetch(`${base}/v1/queries`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(process.env.CAIRN_TOKEN
        ? { authorization: `Bearer ${process.env.CAIRN_TOKEN}` }
        : { "x-cairn-dev-user": process.env.USER || process.env.USERNAME || "cairn-npm" }),
    },
    body: JSON.stringify({ query: question }),
  });
  if (!response.ok) {
    throw new Error(
      `gateway returned ${response.status}: ${(await response.text()).slice(0, 300)}`,
    );
  }
  const body = await response.json();
  return (
    `Investigation queued: ${body.trajectory_id}\n` +
    `Follow it at ${base}${body.stream}\n\n` +
    "The gateway runs the full agent loop, so the answer arrives on that stream."
  );
}

export async function ask(question, { config, report, logs }) {
  const mode = config.chat.mode;
  if (mode === "offline") return offlineAnswer(question, { report, logs });
  if (mode === "gateway") return askGateway(question, config);
  if (mode === "api") return askApi(question, buildContext({ report, logs }), config);
  throw new Error(`unknown chat.mode ${JSON.stringify(mode)}`);
}
