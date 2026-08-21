# cairn

Platform analysis, a live dashboard and log chat for any project.
**Zero dependencies.** Node 18.17+.

```bash
npx cairn analyze
```

That is the whole first step. No config file, no signup, no API key.

```
Cairn  /home/you/checkout-api
  score        62/100  (81 ms)
  stack        node, docker, kubernetes, github-actions
  files        1,284
  services     checkout-api, worker
  findings     2 high · 3 medium · 1 low

  docker
  high   Container runs as root
         Dockerfile
         No USER instruction switches away from root. A process escape starts with uid 0.
         fix Add a non-root user and `USER 10001:10001` before CMD.

  kubernetes
  high   Workload has no resource requests or limits
         k8s/deployment.yaml
         Without a memory limit one pod can evict every neighbour on the node.
         fix Set resources.requests.cpu and resources.limits.memory.
```

## The dashboard

```bash
npx cairn dashboard
```

Opens on `http://127.0.0.1:7777` with six tabs:

| Tab | What it shows |
|---|---|
| **Overview** | Health score, stack, detected services, file inventory |
| **Findings** | Every finding with its file, why it matters, and the fix |
| **Services** | Live health probes against the services you configure |
| **Logs** | Your log sources, tailed and streamed live, coloured by level |
| **Config** | Every setting, editable in place, written straight back to disk |
| **Chat** | Ask about the project, the findings and the logs |

It binds to loopback by default, because it shows your configuration and your
logs and a dashboard quietly listening on `0.0.0.0` is a data leak with a nice
chart.

## Configure it

```bash
npx cairn init
```

Writes `cairn.config.json`, seeded from what it found — services it detected
get health-check entries already filled in. Everything is optional.

```jsonc
{
  "name": "checkout-api",
  "dashboard": { "port": 7777, "host": "127.0.0.1" },
  "analyze": {
    "ignore": ["node_modules", ".git", "dist"],
    "mute": ["docker.no-healthcheck"]   // accepted risks live in the repo
  },
  "logs": {
    "files": ["logs/app.log"],
    "commands": ["kubectl logs -f deploy/checkout-api"]
  },
  "services": [
    { "name": "api", "url": "http://localhost:3000", "healthPath": "/health" }
  ],
  "chat": { "mode": "offline" }
}
```

Change any value from the Config tab, or:

```bash
npx cairn config                      # list everything
npx cairn config dashboard.port       # read one
npx cairn config dashboard.port 9000  # write one
```

Values are coerced to the type the default declares, so `port` stays a number
and a typo is rejected at the point you make it rather than three layers down.

## Chat about your logs

```bash
npx cairn chat "what should I fix first?"
npx cairn chat "why are there errors?"
```

Three modes, set with `chat.mode`:

- **`offline`** (default) — deterministic correlation over the analysis and the
  log buffer. No network, no key, no cost. It clusters a thousand repeated
  errors into one fact and tells you which findings mention the same files.
  Plenty of "why is this failing" is a join, not an inference.
- **`api`** — sends the analysis and a log summary to a model API. Set
  `chat.apiKeyEnv` to the env var holding your key.
- **`gateway`** — hands the question to a running [Cairn
  deployment](https://github.com/Nouman-Amjad/cairn): the full agent loop, real
  tool calls against your metrics and traces, and human approval gates on
  anything that writes.

## In CI

```bash
npx cairn analyze --fail-on high      # exit 1 if anything high is found
npx cairn analyze --json > report.json
```

```yaml
- run: npx cairn analyze --fail-on high
```

## What it checks

| Group | Examples |
|---|---|
| `docker` | root user, unpinned base image, secrets baked into layers, apt cache left behind |
| `k8s` | no resource limits, `:latest` images, privileged containers, missing probes |
| `node` | no lockfile, no test script, no engines range, dependency sprawl |
| `python` | no lockfile, unpinned requirements |
| `terraform` | no remote state backend, unencrypted buckets |
| `compose` | host networking, privileged services |
| `ci` | no pipeline, a pipeline that never runs tests |
| `secrets` | credential shapes in source, `.env` not gitignored |
| `repo` | no README, no LICENSE, no .gitignore, files over 5 MB |

Every finding carries a rule id, the file it came from, why it matters, and
the fix. Mute one by adding its id to `analyze.mute` — so "we accepted that
risk" lives in the repository and shows up in review.

## As a library

```js
import { analyze, load, exitCode } from "cairn";

const report = analyze(load("."));
console.log(report.score, report.findings.length);
process.exitCode = exitCode(report, "high");
```

## Why zero dependencies

Open `package.json` and look at `dependencies`. It is `{}`.

A tool that audits your supply chain should not enlarge it. Everything here is
Node's standard library: the dashboard is `node:http`, the log tail is
`node:fs`, the CLI parses its own arguments. `npx cairn` downloads one small
package and nothing else, and there is no transitive tree to review, upgrade,
or get compromised.

## Commands

```
cairn init                     Write cairn.config.json with detected defaults
cairn analyze                  Scan the project and print findings
cairn dashboard                Serve the live dashboard
cairn logs                     Tail configured log sources
cairn chat <question>          Ask about the project, its findings and its logs
cairn config [key] [value]     Show or set a configuration value

  --cwd <dir>        Run against another directory
  --json             Machine-readable output
  --fail-on <sev>    analyze: exit 1 at this severity or above
  --port <n>         dashboard: override the port
  --open             dashboard: open a browser
```

## Licence

Apache-2.0. Part of [Cairn](https://github.com/Nouman-Amjad/cairn).
