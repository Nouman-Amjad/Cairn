/**
 * cairn.config.json — the one file Cairn writes into your project.
 *
 * Everything is optional. A project with no config still analyses, because a
 * tool that demands configuration before it tells you anything useful never
 * gets adopted.
 */

import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { resolve, join, basename } from "node:path";

export const CONFIG_NAME = "cairn.config.json";

export const DEFAULTS = {
  name: null,               // defaults to the directory name
  root: ".",
  dashboard: { port: 7777, host: "127.0.0.1", open: false },
  analyze: {
    ignore: ["node_modules", ".git", ".venv", "dist", "build", ".next", "target", "vendor"],
    maxDepth: 6,
    // Rule ids to silence, e.g. ["docker.root-user"]. Suppression is explicit
    // and lives in the repo, so "we accepted that risk" is reviewable.
    mute: [],
  },
  logs: {
    // Either files (globs are not supported on purpose — be explicit) or
    // commands whose stdout is streamed.
    files: [],
    commands: [],
    maxLines: 2000,
  },
  chat: {
    // "gateway"  -> a running Cairn deployment (full agent, approval gates)
    // "api"      -> a model API directly, using apiKeyEnv
    // "offline"  -> deterministic local analysis, no network
    mode: "offline",
    gatewayUrl: "http://localhost:8000",
    apiUrl: "https://api.anthropic.com/v1/messages",
    apiKeyEnv: "ANTHROPIC_API_KEY",
    model: "claude-sonnet-4-5",
    maxTokens: 1200,
  },
  services: [],   // [{ name, url, healthPath }]
};

function deepMerge(base, override) {
  if (Array.isArray(base) || Array.isArray(override)) return override ?? base;
  if (typeof base !== "object" || base === null) return override ?? base;
  if (typeof override !== "object" || override === null) return base;
  const out = { ...base };
  for (const [key, value] of Object.entries(override)) {
    out[key] = key in base ? deepMerge(base[key], value) : value;
  }
  return out;
}

export function configPath(root = process.cwd()) {
  return join(resolve(root), CONFIG_NAME);
}

export function load(root = process.cwd()) {
  const path = configPath(root);
  let user = {};
  if (existsSync(path)) {
    try {
      user = JSON.parse(readFileSync(path, "utf8"));
    } catch (error) {
      throw new Error(`${CONFIG_NAME} is not valid JSON: ${error.message}`);
    }
  }
  const merged = deepMerge(DEFAULTS, user);
  merged.name ||= basename(resolve(root));
  merged.root = resolve(root);
  merged._exists = existsSync(path);
  merged._path = path;
  return merged;
}

export function save(config, root = process.cwd()) {
  const { _exists, _path, root: _r, ...persist } = config;
  writeFileSync(configPath(root), JSON.stringify(persist, null, 2) + "\n", "utf8");
  return configPath(root);
}

/** Read a nested value: get(cfg, "dashboard.port") */
export function get(config, dotted) {
  return dotted.split(".").reduce((node, key) => (node == null ? node : node[key]), config);
}

/**
 * Write a nested value, coercing to the type the default declares.
 *
 * Without coercion every value set from the dashboard or the CLI arrives as a
 * string, and `port: "7777"` fails in a way that points at the wrong file.
 */
export function set(config, dotted, raw) {
  const keys = dotted.split(".");
  const last = keys.pop();
  let node = config;
  let template = DEFAULTS;
  for (const key of keys) {
    node[key] ??= {};
    node = node[key];
    template = template?.[key];
  }
  const existing = template?.[last];
  node[last] = coerce(raw, existing);
  return config;
}

export function coerce(raw, like) {
  if (typeof raw !== "string") return raw;
  if (typeof like === "number") {
    const parsed = Number(raw);
    if (Number.isNaN(parsed)) throw new Error(`expected a number, got ${JSON.stringify(raw)}`);
    return parsed;
  }
  if (typeof like === "boolean") {
    if (!["true", "false"].includes(raw)) {
      throw new Error(`expected true or false, got ${JSON.stringify(raw)}`);
    }
    return raw === "true";
  }
  if (Array.isArray(like)) {
    const trimmed = raw.trim();
    if (trimmed.startsWith("[")) return JSON.parse(trimmed);
    return trimmed === "" ? [] : trimmed.split(",").map((s) => s.trim());
  }
  return raw;
}
