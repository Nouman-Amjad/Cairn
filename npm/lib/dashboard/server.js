/**
 * The dashboard server.
 *
 * Node's own http module, no framework, no build step. It binds to loopback
 * by default because it exposes your configuration and your logs, and a
 * dashboard that quietly listens on 0.0.0.0 is a data leak with a nice chart.
 */

import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import { analyze } from "../analyze/index.js";
import { LogStream } from "../logs.js";
import { ask } from "../chat.js";
import { load, save, set as setValue, get as getValue, DEFAULTS } from "../config.js";
import { render } from "./ui.js";

const HERE = dirname(fileURLToPath(import.meta.url));

function json(res, status, body) {
  const payload = JSON.stringify(body);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(payload),
    "cache-control": "no-store",
  });
  res.end(payload);
}

async function readBody(req, limit = 1024 * 256) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("request body too large");
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

export async function start(config, { port, host } = {}) {
  const state = {
    config,
    report: analyze(config),
    logs: await new LogStream(config).start(),
    clients: new Set(),
  };

  // Push new log lines to every open dashboard.
  state.logs.on("line", (entry) => broadcast(state, "log", entry));

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, `http://${req.headers.host}`);
    try {
      await route(url, req, res, state);
    } catch (error) {
      json(res, 500, { error: error.message });
    }
  });

  const listenPort = port ?? config.dashboard.port;
  const listenHost = host ?? config.dashboard.host;

  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(listenPort, listenHost, resolve);
  });

  const close = () => {
    state.logs.stop();
    for (const client of state.clients) client.end();
    server.close();
  };
  return { server, state, url: `http://${listenHost}:${listenPort}`, close };
}

function broadcast(state, event, data) {
  const frame = `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
  for (const client of state.clients) {
    // A slow reader must not hold up the log stream; drop rather than buffer.
    if (!client.writableEnded) client.write(frame);
  }
}

async function route(url, req, res, state) {
  const { pathname } = url;

  if (pathname === "/" && req.method === "GET") {
    const html = render(state);
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    return res.end(html);
  }

  // Two modules, served straight off disk. The allowlist is the whole of the
  // path handling here on purpose: never join a request-supplied name onto
  // HERE, or the dashboard becomes a file server for the machine it runs on.
  if (req.method === "GET" && (pathname === "/app.js" || pathname === "/charts.js")) {
    const file = pathname === "/app.js" ? "client.js" : "charts.js";
    const script = readFileSync(join(HERE, file), "utf8");
    res.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
    return res.end(script);
  }

  if (pathname === "/api/report" && req.method === "GET") {
    return json(res, 200, state.report);
  }

  if (pathname === "/api/report/refresh" && req.method === "POST") {
    state.config = load(state.config.root);
    state.report = analyze(state.config);
    broadcast(state, "report", { score: state.report.score, summary: state.report.summary });
    return json(res, 200, state.report);
  }

  if (pathname === "/api/logs" && req.method === "GET") {
    const level = url.searchParams.get("level");
    const limit = Number(url.searchParams.get("limit") || 200);
    return json(res, 200, {
      entries: state.logs.recent(limit, level && level !== "all" ? level : null),
      stats: state.logs.stats(),
    });
  }

  if (pathname === "/api/config" && req.method === "GET") {
    const { _exists, _path, ...visible } = state.config;
    return json(res, 200, { config: visible, defaults: DEFAULTS, path: _path });
  }

  if (pathname === "/api/config" && req.method === "POST") {
    const { key, value } = await readBody(req);
    if (typeof key !== "string" || !key) return json(res, 400, { error: "key is required" });
    try {
      setValue(state.config, key, value);
    } catch (error) {
      return json(res, 400, { error: error.message });
    }
    save(state.config, state.config.root);
    // Re-analyse: ignore lists and mute lists change what the report says.
    state.report = analyze(state.config);
    broadcast(state, "config", { key, value: getValue(state.config, key) });
    return json(res, 200, { key, value: getValue(state.config, key), saved: true });
  }

  if (pathname === "/api/chat" && req.method === "POST") {
    const { question } = await readBody(req);
    if (!question) return json(res, 400, { error: "question is required" });
    try {
      const answer = await ask(question, {
        config: state.config,
        report: state.report,
        logs: state.logs,
      });
      return json(res, 200, { answer, mode: state.config.chat.mode });
    } catch (error) {
      return json(res, 200, { answer: `**Chat failed.** ${error.message}`, error: true });
    }
  }

  if (pathname === "/api/services" && req.method === "GET") {
    return json(res, 200, { services: await probeServices(state.config) });
  }

  if (pathname === "/events" && req.method === "GET") {
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache",
      connection: "keep-alive",
    });
    res.write("retry: 3000\n\n");
    state.clients.add(res);
    req.on("close", () => state.clients.delete(res));
    return undefined;
  }

  if (pathname === "/api/health" && req.method === "GET") {
    return json(res, 200, { ok: true, name: state.config.name, score: state.report.score });
  }

  return json(res, 404, { error: "not found" });
}

/** Probe configured service health endpoints. Failure is a status, not an error. */
async function probeServices(config) {
  const results = [];
  for (const service of config.services) {
    if (!service.url) {
      results.push({ ...service, status: "unconfigured" });
      continue;
    }
    const target = service.url.replace(/\/$/, "") + (service.healthPath || "/");
    const started = Date.now();
    try {
      const response = await fetch(target, {
        signal: AbortSignal.timeout(3000),
       });
      results.push({
        ...service,
        status: response.ok ? "up" : "degraded",
        code: response.status,
        latencyMs: Date.now() - started,
      });
    } catch (error) {
      results.push({
        ...service,
        status: "down",
        error: error.name === "TimeoutError" ? "timed out" : error.message,
        latencyMs: Date.now() - started,
      });
    }
  }
  return results;
}
