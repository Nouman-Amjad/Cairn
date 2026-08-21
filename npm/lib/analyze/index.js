/**
 * The analysis entry point: scan, run rules, score.
 *
 * Returns a plain object. The CLI prints it, the dashboard renders it, and
 * the chat command reasons over it — one shape, three consumers, no
 * formatting decisions buried in the analyser.
 */

import { inventory } from "./scan.js";
import { runRules, SEVERITY_ORDER } from "./rules.js";

export { inventory, runRules };

const WEIGHTS = { high: 12, medium: 4, low: 1 };

/**
 * A single number for the dashboard header.
 *
 * Deliberately crude and documented as such: it exists to make a trend
 * visible, not to be defensible to three significant figures. Anyone tuning
 * the weights to hit a target has misunderstood what it is for.
 */
export function score(findings) {
  const penalty = findings.reduce((sum, f) => sum + (WEIGHTS[f.severity] || 0), 0);
  return Math.max(0, 100 - penalty);
}

export function summarise(findings) {
  const bySeverity = { high: 0, medium: 0, low: 0 };
  const byGroup = {};
  for (const f of findings) {
    bySeverity[f.severity] = (bySeverity[f.severity] || 0) + 1;
    byGroup[f.group] = (byGroup[f.group] || 0) + 1;
  }
  return { total: findings.length, bySeverity, byGroup };
}

export function analyze(config) {
  const started = Date.now();
  const inv = inventory(config.root, {
    ignore: config.analyze.ignore,
    maxDepth: config.analyze.maxDepth,
  });
  const findings = runRules(inv, { mute: config.analyze.mute });

  return {
    name: config.name,
    root: inv.root,
    generatedAt: new Date().toISOString(),
    durationMs: Date.now() - started,
    stack: inv.stack,
    services: inv.services,
    files: {
      total: inv.files.length,
      bytes: inv.totalBytes,
      byExtension: inv.counts,
    },
    hasGit: inv.hasGit,
    findings,
    summary: summarise(findings),
    score: score(findings),
  };
}

/** Exit code for CI: non-zero when something at or above `failOn` is present. */
export function exitCode(report, failOn = "high") {
  const threshold = SEVERITY_ORDER[failOn];
  if (threshold === undefined) return 0;
  return report.findings.some((f) => SEVERITY_ORDER[f.severity] <= threshold) ? 1 : 0;
}
