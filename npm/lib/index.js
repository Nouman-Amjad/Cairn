/**
 * Programmatic API.
 *
 *   import { analyze, load } from "cairn";
 *   const report = analyze(load("."));
 *
 * Exported so the analysis can run inside your own tooling — a CI script, a
 * custom dashboard, a bot — without shelling out to the CLI and parsing text.
 */

export { load, save, get, set, DEFAULTS, CONFIG_NAME } from "./config.js";
export { analyze, inventory, runRules, score, summarise, exitCode } from "./analyze/index.js";
export { RULE_SETS } from "./analyze/rules.js";
export { LogStream, tailFile, classify } from "./logs.js";
export { ask, cluster, offlineAnswer } from "./chat.js";
export { start as startDashboard } from "./dashboard/server.js";
export { run as runCli, parseArgs } from "./cli.js";
