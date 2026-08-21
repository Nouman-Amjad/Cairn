import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, mkdirSync, rmSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { load, save, get, set, coerce, DEFAULTS } from "../lib/config.js";
import { analyze, exitCode } from "../lib/analyze/index.js";
import { inventory } from "../lib/analyze/scan.js";
import { runRules } from "../lib/analyze/rules.js";
import { classify, tailFile } from "../lib/logs.js";
import { cluster, offlineAnswer } from "../lib/chat.js";
import { parseArgs } from "../lib/cli.js";

/** Build a throwaway project on disk. */
function fixture(files) {
  const root = mkdtempSync(join(tmpdir(), "cairn-test-"));
  for (const [rel, content] of Object.entries(files)) {
    const path = join(root, rel);
    mkdirSync(join(path, ".."), { recursive: true });
    writeFileSync(path, content, "utf8");
  }
  return root;
}

function reportFor(files) {
  const root = fixture(files);
  try {
    return analyze(load(root));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
}

const ids = (report) => report.findings.map((f) => f.id);

// ------------------------------------------------------------------ config

test("config falls back to defaults when the file is absent", () => {
  const root = fixture({ "README.md": "# x" });
  const config = load(root);
  assert.equal(config.dashboard.port, DEFAULTS.dashboard.port);
  assert.equal(config._exists, false);
  assert.ok(config.name, "name defaults to the directory");
  rmSync(root, { recursive: true, force: true });
});

test("a user config is merged over defaults, not replacing them", () => {
  const root = fixture({ "cairn.config.json": JSON.stringify({ dashboard: { port: 9999 } }) });
  const config = load(root);
  assert.equal(config.dashboard.port, 9999, "override applies");
  assert.equal(config.dashboard.host, "127.0.0.1", "siblings survive the merge");
  assert.equal(config.chat.mode, "offline", "unrelated sections survive");
  rmSync(root, { recursive: true, force: true });
});

test("invalid JSON names the file rather than throwing a parse trace", () => {
  const root = fixture({ "cairn.config.json": "{ not json" });
  assert.throws(() => load(root), /cairn\.config\.json is not valid JSON/);
  rmSync(root, { recursive: true, force: true });
});

test("values coerce to the type the default declares", () => {
  assert.equal(coerce("7777", 0), 7777);
  assert.equal(coerce("true", false), true);
  assert.deepEqual(coerce("a, b", []), ["a", "b"]);
  assert.deepEqual(coerce('["x"]', []), ["x"]);
  assert.equal(coerce("hello", ""), "hello");
  // A port that silently became a string fails much later and elsewhere.
  assert.throws(() => coerce("not-a-number", 0), /expected a number/);
});

test("set and get round-trip through nested keys", () => {
  const config = structuredClone(DEFAULTS);
  set(config, "dashboard.port", "8080");
  assert.equal(get(config, "dashboard.port"), 8080);
  assert.equal(typeof get(config, "dashboard.port"), "number");
});

test("save writes only persistable keys", () => {
  const root = fixture({});
  const config = load(root);
  save(config, root);
  const written = JSON.parse(readFileSync(join(root, "cairn.config.json"), "utf8"));
  assert.ok(!("_exists" in written), "internal fields are not persisted");
  assert.ok(!("root" in written), "absolute paths are not persisted");
  rmSync(root, { recursive: true, force: true });
});

// ------------------------------------------------------------------- scan

test("stack detection recognises several ecosystems at once", () => {
  const root = fixture({
    "package.json": "{}",
    "pyproject.toml": "[project]",
    "Dockerfile": "FROM node:22",
    "main.tf": 'provider "aws" {}',
  });
  const inv = inventory(root, { ignore: [], maxDepth: 4 });
  for (const expected of ["node", "python", "docker", "terraform"]) {
    assert.ok(inv.stack.includes(expected), `expected ${expected} in ${inv.stack}`);
  }
  rmSync(root, { recursive: true, force: true });
});

test("ignored directories are not walked", () => {
  const root = fixture({
    "index.js": "1",
    "node_modules/pkg/index.js": "1",
  });
  const inv = inventory(root, { ignore: ["node_modules"], maxDepth: 4 });
  assert.ok(!inv.files.some((f) => f.rel.includes("node_modules")));
  rmSync(root, { recursive: true, force: true });
});

test("compose services are read without swallowing the volumes block", () => {
  const root = fixture({
    "docker-compose.yml": [
      "services:",
      "  api:",
      "    image: api:1",
      "  worker:",
      "    image: worker:1",
      "volumes:",
      "  pgdata:",
      "  cache:",
    ].join("\n"),
  });
  const inv = inventory(root, { ignore: [], maxDepth: 3 });
  const names = inv.services.map((s) => s.name);
  assert.deepEqual(names.sort(), ["api", "worker"]);
  rmSync(root, { recursive: true, force: true });
});

// ------------------------------------------------------------------ rules

test("a root container with an unpinned base is flagged", () => {
  const report = reportFor({ Dockerfile: "FROM node:latest\nCMD [\"node\"]\n" });
  assert.ok(ids(report).includes("docker.root-user"));
  assert.ok(ids(report).includes("docker.latest-tag"));
});

test("a hardened Dockerfile produces neither finding", () => {
  const report = reportFor({
    Dockerfile: "FROM node:22-slim\nUSER 10001:10001\nHEALTHCHECK CMD true\nCMD [\"node\"]\n",
  });
  assert.ok(!ids(report).includes("docker.root-user"));
  assert.ok(!ids(report).includes("docker.latest-tag"));
});

test("a kind reference inside another block is not treated as a workload", () => {
  // An ArgoCD ignoreDifferences block mentions `kind: Job` while declaring no
  // workload at all. Treating it as one flagged files that were fine.
  const report = reportFor({
    "app.yaml": [
      "apiVersion: argoproj.io/v1alpha1",
      "kind: Application",
      "spec:",
      "  ignoreDifferences:",
      "    - group: batch",
      "      kind: Job",
    ].join("\n"),
  });
  assert.deepEqual(
    ids(report).filter((id) => id.startsWith("k8s.")),
    [],
    "no kubernetes findings for a file that declares no workload",
  );
});

test("a real deployment without limits or probes is flagged", () => {
  const report = reportFor({
    "deploy.yaml": [
      "apiVersion: apps/v1",
      "kind: Deployment",
      "spec:",
      "  template:",
      "    spec:",
      "      containers:",
      "        - name: api",
      "          image: api:latest",
    ].join("\n"),
  });
  const found = ids(report);
  assert.ok(found.includes("k8s.no-resources"));
  assert.ok(found.includes("k8s.latest-tag"));
  assert.ok(found.includes("k8s.no-probes"));
});

test("helm templates are skipped rather than reported as broken manifests", () => {
  const report = reportFor({
    "tpl.yaml": [
      "apiVersion: apps/v1",
      "kind: Deployment",
      "spec:",
      "  {{- if .Values.enabled }}",
      "  replicas: 1",
      "  {{- end }}",
    ].join("\n"),
  });
  assert.ok(!ids(report).includes("k8s.no-resources"));
});

test("a missing lockfile is high severity for both node and python", () => {
  const node = reportFor({ "package.json": '{"name":"x"}' });
  assert.ok(ids(node).includes("node.no-lockfile"));

  const python = reportFor({ "requirements.txt": "flask\nrequests==2.0\n" });
  const found = ids(python);
  assert.ok(found.includes("python.no-lockfile"));
  assert.ok(found.includes("python.unpinned"), "an unpinned requirement is reported");
});

test("a lockfile removes the finding", () => {
  const report = reportFor({ "package.json": '{"name":"x"}', "package-lock.json": "{}" });
  assert.ok(!ids(report).includes("node.no-lockfile"));
});

test("credential shapes are detected, ordinary text is not", () => {
  const leaked = reportFor({ "config.js": 'const key = "AKIAIOSFODNN7EXAMPLE";' });
  assert.ok(ids(leaked).includes("secrets.hardcoded"));

  const clean = reportFor({ "config.js": 'const key = process.env.AWS_KEY;' });
  assert.ok(!ids(clean).includes("secrets.hardcoded"));
});

test("muted rules are suppressed", () => {
  const root = fixture({
    Dockerfile: "FROM node:latest\n",
    "cairn.config.json": JSON.stringify({ analyze: { mute: ["docker.root-user"] } }),
  });
  const report = analyze(load(root));
  assert.ok(!ids(report).includes("docker.root-user"), "muted");
  assert.ok(ids(report).includes("docker.latest-tag"), "others still fire");
  rmSync(root, { recursive: true, force: true });
});

test("every finding carries a locatable id and an actionable fix", () => {
  const report = reportFor({
    Dockerfile: "FROM node:latest\n",
    "package.json": '{"name":"x"}',
  });
  assert.ok(report.findings.length > 0);
  for (const finding of report.findings) {
    assert.match(finding.id, /^[a-z0-9]+\.[a-z0-9-]+$/, `id shape: ${finding.id}`);
    assert.ok(finding.fix && finding.fix.length > 10, `${finding.id} has no usable fix`);
    assert.ok(["high", "medium", "low"].includes(finding.severity));
  }
});

test("findings are ordered worst first", () => {
  const report = reportFor({ Dockerfile: "FROM node:latest\n", "package.json": '{"name":"x"}' });
  const rank = { high: 0, medium: 1, low: 2 };
  const severities = report.findings.map((f) => rank[f.severity]);
  assert.deepEqual(severities, [...severities].sort((a, b) => a - b));
});

// ------------------------------------------------------------------ score

test("score falls as findings accumulate and is bounded at zero", () => {
  const clean = reportFor({
    "README.md": "# ok",
    LICENSE: "Apache",
    ".gitignore": "node_modules",
  });
  const messy = reportFor({
    Dockerfile: "FROM node:latest\n",
    "package.json": '{"name":"x"}',
    "requirements.txt": "flask\n",
  });
  assert.ok(clean.score > messy.score, `${clean.score} should beat ${messy.score}`);
  assert.ok(messy.score >= 0);
  assert.ok(clean.score <= 100);
});

test("exitCode gates CI at the requested severity", () => {
  const report = reportFor({ "package.json": '{"name":"x"}' }); // high: no lockfile
  assert.equal(exitCode(report, "high"), 1);
  assert.equal(exitCode(report, "none"), 0, "an unknown severity never fails the build");
});

// ------------------------------------------------------------------- logs

test("log levels are classified from the line", () => {
  assert.equal(classify("ERROR something broke"), "error");
  assert.equal(classify("Traceback (most recent call last):"), "error");
  assert.equal(classify("WARN disk filling"), "warn");
  assert.equal(classify("listening on 8080"), "info");
  assert.equal(classify("DEBUG payload"), "debug");
});

test("tailFile returns the last lines without reading the whole file", async () => {
  const root = fixture({ "app.log": Array.from({ length: 5000 }, (_, i) => `line ${i}`).join("\n") });
  const lines = await tailFile(join(root, "app.log"), 10);
  assert.equal(lines.length, 10);
  assert.equal(lines.at(-1), "line 4999");
  rmSync(root, { recursive: true, force: true });
});

test("tailFile on a missing file is empty rather than an exception", async () => {
  assert.deepEqual(await tailFile(join(tmpdir(), "definitely-not-here.log")), []);
});

// ------------------------------------------------------------------- chat

test("clustering collapses repeats that differ only by ids and numbers", () => {
  const entries = Array.from({ length: 50 }, (_, i) => ({
    at: new Date().toISOString(),
    source: "app",
    level: "error",
    line: `request 8f3a${i} failed after ${i * 10}ms`,
  }));
  const groups = cluster(entries);
  assert.equal(groups.length, 1, "fifty near-identical lines are one fact");
  assert.equal(groups[0].count, 50);
});

test("clustering keeps genuinely different messages apart", () => {
  const groups = cluster([
    { at: "", source: "a", level: "error", line: "database connection refused" },
    { at: "", source: "a", level: "error", line: "disk full" },
  ]);
  assert.equal(groups.length, 2);
});

test("the offline answer is grounded and says what it is", () => {
  const report = reportFor({ Dockerfile: "FROM node:latest\n" });
  const answer = offlineAnswer("any security risks?", { report, logs: null });
  assert.match(answer, /high-severity|No high-severity/);
  assert.match(answer, /Answered offline/, "it must not pass itself off as an agent");
});

test("the offline answer says so when no logs are collected", () => {
  const report = reportFor({ "README.md": "x" });
  const answer = offlineAnswer("why are there errors in the logs?", { report, logs: null });
  assert.match(answer, /No logs are being collected/);
});

// -------------------------------------------------------------------- cli

test("argument parsing separates commands, values and boolean flags", () => {
  const args = parseArgs(["analyze", "--json", "--fail-on", "medium", "--cwd", "/tmp"]);
  assert.deepEqual(args._, ["analyze"]);
  assert.equal(args.flags.json, true);
  assert.equal(args.flags["fail-on"], "medium");
  assert.equal(args.flags.cwd, "/tmp");
});

test("a flag followed by another flag is boolean, not a value", () => {
  const args = parseArgs(["dashboard", "--open", "--port", "8080"]);
  assert.equal(args.flags.open, true);
  assert.equal(args.flags.port, "8080");
});

test("chat questions survive as positional arguments", () => {
  const args = parseArgs(["chat", "why", "is", "it", "slow?"]);
  assert.deepEqual(args._, ["chat", "why", "is", "it", "slow?"]);
});
