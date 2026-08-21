/**
 * The `cairn` command.
 *
 * Argument parsing is by hand and stays that way: a CLI with six commands
 * does not need a dependency, and this package having zero of them is a
 * feature people can verify in one look at package.json.
 */

import { existsSync } from "node:fs";
import { relative } from "node:path";

import { load, save, get, set, configPath, CONFIG_NAME, DEFAULTS } from "./config.js";
import { analyze, exitCode } from "./analyze/index.js";
import { LogStream } from "./logs.js";
import { ask } from "./chat.js";

const VERSION = "0.1.0";

// ANSI, but only when someone is actually looking at a terminal. Piping to a
// file or a CI log should produce plain text.
const tty = process.stdout.isTTY && !process.env.NO_COLOR;
const paint = (code, text) => (tty ? `[${code}m${text}[0m` : text);
const bold = (t) => paint("1", t);
const dim = (t) => paint("2", t);
const red = (t) => paint("31", t);
const yellow = (t) => paint("33", t);
const green = (t) => paint("32", t);
const cyan = (t) => paint("36", t);

const SEVERITY_PAINT = { high: red, medium: yellow, low: dim };

const HELP = `${bold("cairn")} — platform analysis, a live dashboard and log chat for any project

${bold("USAGE")}
  cairn <command> [options]

${bold("COMMANDS")}
  init                     Write ${CONFIG_NAME} with detected defaults
  analyze                  Scan the project and print findings
  dashboard                Serve the live dashboard
  logs                     Tail configured log sources
  chat <question>          Ask about the project, its findings and its logs
  config [key] [value]     Show or set a configuration value
  help                     This message

${bold("OPTIONS")}
  --cwd <dir>              Run against another directory (default: here)
  --json                   Machine-readable output
  --fail-on <severity>     analyze: exit 1 at this severity or above
                           (high | medium | low | none, default: high)
  --port <n>               dashboard: override the port
  --host <addr>            dashboard: override the bind address
  --open                   dashboard: open a browser
  --version                Print the version

${bold("EXAMPLES")}
  npx cairn analyze
  npx cairn analyze --fail-on medium --json > report.json
  npx cairn dashboard --port 8080
  npx cairn chat "what should I fix first?"
  npx cairn config dashboard.port 9000
`;

export function parseArgs(argv) {
  const args = { _: [], flags: {} };
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) {
      args._.push(token);
      continue;
    }
    const key = token.slice(2);
    const next = argv[index + 1];
    // Boolean flags are those with no value, or followed by another flag.
    if (next === undefined || next.startsWith("--")) {
      args.flags[key] = true;
    } else {
      args.flags[key] = next;
      index += 1;
    }
  }
  return args;
}

function severityCounts(report) {
  const { high = 0, medium = 0, low = 0 } = report.summary.bySeverity;
  return [
    high ? red(`${high} high`) : dim("0 high"),
    medium ? yellow(`${medium} medium`) : dim("0 medium"),
    dim(`${low} low`),
  ].join(dim(" · "));
}

function printReport(report) {
  const scorePaint = report.score >= 85 ? green : report.score >= 60 ? yellow : red;
  console.log();
  console.log(`${bold(report.name)} ${dim(report.root)}`);
  console.log(
    `  score        ${scorePaint(`${report.score}/100`)}  ${dim(`(${report.durationMs} ms)`)}`,
  );
  console.log(`  stack        ${report.stack.join(", ") || dim("not detected")}`);
  console.log(`  files        ${report.files.total}`);
  console.log(
    `  services     ${report.services.map((s) => s.name).join(", ") || dim("none detected")}`,
  );
  console.log(`  findings     ${severityCounts(report)}`);

  if (!report.findings.length) {
    console.log();
    console.log(green("  Nothing to report. That is the good outcome."));
    console.log();
    return;
  }

  let group = null;
  for (const finding of report.findings) {
    if (finding.group !== group) {
      group = finding.group;
      console.log();
      console.log(bold(`  ${group}`));
    }
    const tint = SEVERITY_PAINT[finding.severity] || dim;
    console.log(`  ${tint(finding.severity.padEnd(6))} ${finding.title}`);
    if (finding.file) console.log(`         ${dim(finding.file)}`);
    console.log(`         ${dim(finding.detail)}`);
    console.log(`         ${cyan("fix")} ${finding.fix}`);
  }
  console.log();
  console.log(dim(`  Mute a rule by adding its id to analyze.mute in ${CONFIG_NAME}.`));
  console.log();
}

// -------------------------------------------------------------- commands

async function cmdInit(config, args) {
  const path = configPath(config.root);
  if (existsSync(path) && !args.flags.force) {
    console.log(`${CONFIG_NAME} already exists. Use --force to overwrite.`);
    return 0;
  }

  // Seed from what is actually here, so the first `analyze` is useful without
  // anyone editing anything.
  const report = analyze(config);
  const seeded = {
    ...DEFAULTS,
    name: config.name,
    services: report.services
      .filter((service) => service.port)
      .map((service) => ({
        name: service.name,
        url: `http://localhost:${service.port}`,
        healthPath: "/health",
      })),
  };
  delete seeded.root;

  save({ ...seeded, root: config.root }, config.root);
  console.log(`${green("created")} ${relative(process.cwd(), path) || CONFIG_NAME}`);
  console.log(`  stack     ${report.stack.join(", ") || "not detected"}`);
  console.log(`  services  ${seeded.services.length}`);
  console.log();
  console.log(`Next: ${cyan("npx cairn dashboard")}`);
  return 0;
}

function cmdAnalyze(config, args) {
  const report = analyze(config);
  if (args.flags.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    printReport(report);
  }
  const failOn = args.flags["fail-on"] ?? "high";
  return failOn === "none" ? 0 : exitCode(report, failOn);
}

async function cmdDashboard(config, args) {
  const { start } = await import("./dashboard/server.js");
  const port = args.flags.port ? Number(args.flags.port) : undefined;
  const host = args.flags.host;

  let handle;
  try {
    handle = await start(config, { port, host });
  } catch (error) {
    if (error.code === "EADDRINUSE") {
      console.error(
        red(`port ${port ?? config.dashboard.port} is already in use.`),
        "\nPass --port to choose another.",
      );
      return 1;
    }
    throw error;
  }

  console.log();
  console.log(`  ${bold("Cairn")} dashboard for ${bold(config.name)}`);
  console.log(`  ${cyan(handle.url)}`);
  console.log(`  ${dim(`score ${handle.state.report.score}/100 · ` +
    `${handle.state.report.summary.total} findings · chat mode ${config.chat.mode}`)}`);
  console.log(`  ${dim("ctrl-c to stop")}`);
  console.log();

  if (args.flags.open || config.dashboard.open) openBrowser(handle.url);

  await new Promise((resolve) => {
    const stop = () => {
      handle.close();
      resolve();
    };
    process.on("SIGINT", stop);
    process.on("SIGTERM", stop);
  });
  return 0;
}

async function cmdLogs(config) {
  const stream = await new LogStream(config).start();
  if (!config.logs.files.length && !config.logs.commands.length) {
    console.log(
      dim(`No log sources configured. Add them under logs.files or logs.commands in ${CONFIG_NAME}.`),
    );
    stream.stop();
    return 0;
  }
  for (const entry of stream.recent(100)) printLog(entry);
  stream.on("line", printLog);
  await new Promise((resolve) => process.on("SIGINT", () => (stream.stop(), resolve())));
  return 0;
}

function printLog(entry) {
  const tint = { error: red, fatal: red, warn: yellow, debug: dim }[entry.level] || ((t) => t);
  console.log(`${dim(entry.source.padEnd(18).slice(0, 18))} ${tint(entry.line)}`);
}

async function cmdChat(config, args) {
  const question = args._.slice(1).join(" ").trim();
  if (!question) {
    console.error("Ask something: cairn chat \"why are there errors?\"");
    return 1;
  }
  const report = analyze(config);
  const logs = await new LogStream(config).start();
  try {
    const answer = await ask(question, { config, report, logs });
    console.log();
    console.log(answer);
    console.log();
  } catch (error) {
    console.error(red(error.message));
    return 1;
  } finally {
    logs.stop();
  }
  return 0;
}

function cmdConfig(config, args) {
  const [, key, ...rest] = args._;

  if (!key) {
    const flat = (object, prefix = "") =>
      Object.entries(object).flatMap(([k, v]) => {
        if (k.startsWith("_") || k === "root") return [];
        const path = prefix ? `${prefix}.${k}` : k;
        return v && typeof v === "object" && !Array.isArray(v)
          ? flat(v, path)
          : [[path, v]];
      });
    for (const [path, value] of flat(config)) {
      console.log(`${path.padEnd(28)} ${dim(JSON.stringify(value))}`);
    }
    return 0;
  }

  if (!rest.length) {
    const value = get(config, key);
    if (value === undefined) {
      console.error(`no such key: ${key}`);
      return 1;
    }
    console.log(typeof value === "object" ? JSON.stringify(value, null, 2) : value);
    return 0;
  }

  try {
    set(config, key, rest.join(" "));
  } catch (error) {
    console.error(red(error.message));
    return 1;
  }
  save(config, config.root);
  console.log(`${green("set")} ${key} = ${JSON.stringify(get(config, key))}`);
  return 0;
}

function openBrowser(url) {
  import("node:child_process").then(({ spawn }) => {
    const command =
      process.platform === "darwin" ? "open" : process.platform === "win32" ? "start" : "xdg-open";
    try {
      spawn(command, [url], { shell: process.platform === "win32", detached: true, stdio: "ignore" })
        .unref();
    } catch {
      /* not being able to open a browser is not worth failing over */
    }
  });
}

// ------------------------------------------------------------------ entry

export async function run(argv = process.argv.slice(2)) {
  const args = parseArgs(argv);
  const command = args._[0] || "help";

  if (args.flags.version || command === "version") {
    console.log(VERSION);
    return 0;
  }
  if (command === "help" || args.flags.help) {
    console.log(HELP);
    return 0;
  }

  let config;
  try {
    config = load(args.flags.cwd || process.cwd());
  } catch (error) {
    console.error(red(error.message));
    return 1;
  }

  switch (command) {
    case "init":
      return cmdInit(config, args);
    case "analyze":
    case "analyse":
      return cmdAnalyze(config, args);
    case "dashboard":
    case "dash":
      return cmdDashboard(config, args);
    case "logs":
      return cmdLogs(config);
    case "chat":
      return cmdChat(config, args);
    case "config":
      return cmdConfig(config, args);
    default:
      console.error(`unknown command: ${command}\n`);
      console.log(HELP);
      return 1;
  }
}
