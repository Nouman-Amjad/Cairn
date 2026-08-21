/**
 * Log collection: files that are tailed, commands whose output is streamed.
 *
 * Kept deliberately small. This is not a log aggregator — it is a window onto
 * the handful of sources you already know matter, so the dashboard and the
 * chat command have something concrete to reason about.
 */

import { createReadStream, existsSync, statSync, watch } from "node:fs";
import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { resolve } from "node:path";

const LEVELS = [
  [/\b(FATAL|CRITICAL)\b/i, "fatal"],
  [/\b(ERROR|ERR)\b|Traceback|Exception|panic:/i, "error"],
  [/\bWARN(ING)?\b/i, "warn"],
  [/\bDEBUG\b/i, "debug"],
];

export function classify(line) {
  for (const [pattern, level] of LEVELS) {
    if (pattern.test(line)) return level;
  }
  return "info";
}

/**
 * Read the last `limit` lines of a file without loading the whole thing.
 *
 * Reads a window from the end and grows it until enough newlines are found.
 * A 2 GB log is normal and `readFileSync` on one is how a dashboard takes the
 * machine down.
 */
export function tailFile(path, limit = 200) {
  if (!existsSync(path)) return [];
  const size = statSync(path).size;
  if (size === 0) return [];

  return new Promise((resolveLines) => {
    let window = Math.min(size, 64 * 1024);
    const attempt = () => {
      const start = Math.max(0, size - window);
      const chunks = [];
      createReadStream(path, { start, end: size, encoding: "utf8" })
        .on("data", (chunk) => chunks.push(chunk))
        .on("end", () => {
          const text = chunks.join("");
          const lines = text.split(/\r?\n/).filter(Boolean);
          // If the window did not reach the start of the file we may have cut
          // the first line in half; drop it rather than show a fragment.
          const clean = start > 0 ? lines.slice(1) : lines;
          if (clean.length >= limit || start === 0 || window >= size) {
            resolveLines(clean.slice(-limit));
          } else {
            window = Math.min(size, window * 4);
            attempt();
          }
        })
        .on("error", () => resolveLines([]));
    };
    attempt();
  });
}

export class LogStream extends EventEmitter {
  constructor(config) {
    super();
    this.config = config;
    this.buffer = [];
    this.max = config.logs.maxLines;
    this.watchers = [];
    this.children = [];
  }

  push(source, line) {
    if (!line.trim()) return;
    const entry = {
      at: new Date().toISOString(),
      source,
      level: classify(line),
      line: line.length > 4000 ? `${line.slice(0, 4000)}…` : line,
    };
    this.buffer.push(entry);
    // Ring buffer: an unbounded array is a memory leak with a nice name.
    if (this.buffer.length > this.max) this.buffer.splice(0, this.buffer.length - this.max);
    this.emit("line", entry);
  }

  async start() {
    for (const relPath of this.config.logs.files) {
      const path = resolve(this.config.root, relPath);
      if (!existsSync(path)) {
        this.push("cairn", `log file not found: ${relPath}`);
        continue;
      }
      for (const line of await tailFile(path, 200)) this.push(relPath, line);

      let position = statSync(path).size;
      const watcher = watch(path, { persistent: false }, () => {
        let current;
        try {
          current = statSync(path).size;
        } catch {
          return;
        }
        // Truncation (logrotate) resets the read position rather than
        // replaying the whole file as if it were new.
        if (current < position) position = 0;
        if (current === position) return;
        const stream = createReadStream(path, { start: position, end: current, encoding: "utf8" });
        let pending = "";
        stream.on("data", (chunk) => (pending += chunk));
        stream.on("end", () => {
          for (const line of pending.split(/\r?\n/)) this.push(relPath, line);
          position = current;
        });
      });
      this.watchers.push(watcher);
    }

    for (const command of this.config.logs.commands) {
      const child = spawn(command, { shell: true, cwd: this.config.root });
      const onData = (chunk) => {
        for (const line of chunk.toString().split(/\r?\n/)) this.push(command, line);
      };
      child.stdout.on("data", onData);
      child.stderr.on("data", onData);
      child.on("error", (error) => this.push("cairn", `${command}: ${error.message}`));
      this.children.push(child);
    }
    return this;
  }

  stop() {
    for (const watcher of this.watchers) watcher.close();
    for (const child of this.children) child.kill();
    this.watchers = [];
    this.children = [];
  }

  recent(limit = 200, level = null) {
    const filtered = level ? this.buffer.filter((e) => e.level === level) : this.buffer;
    return filtered.slice(-limit);
  }

  /** Counts by level, for the dashboard header and the chat context. */
  stats() {
    const counts = { fatal: 0, error: 0, warn: 0, info: 0, debug: 0 };
    for (const entry of this.buffer) counts[entry.level] = (counts[entry.level] || 0) + 1;
    return { total: this.buffer.length, counts };
  }
}
