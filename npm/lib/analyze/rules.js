/**
 * Platform analysis rules.
 *
 * Each rule reads the inventory and returns findings. Three properties keep
 * this from becoming another linter nobody runs:
 *
 *   1. Every finding names the file it came from. A finding you cannot locate
 *      is a complaint.
 *   2. Every finding carries a `fix` — the actual next action, not a lecture.
 *   3. Nothing is reported that the project has muted by id, so "we accepted
 *      that risk" lives in the repo and is reviewable.
 */

import { contentOf } from "./scan.js";

const HIGH = "high";
const MEDIUM = "medium";
const LOW = "low";

function finding(id, severity, title, detail, file, fix) {
  return { id, severity, title, detail, file: file || null, fix };
}

function lineOf(text, pattern) {
  const lines = text.split(/\r?\n/);
  const index = lines.findIndex((line) => pattern.test(line));
  return index === -1 ? null : index + 1;
}

// ------------------------------------------------------------------ docker

function dockerRules(inv) {
  const out = [];
  for (const file of inv.files.filter((f) => f.name.startsWith("Dockerfile"))) {
    const text = contentOf(file);
    if (!text) continue;

    if (!/^\s*USER\s+(?!root\b)/im.test(text)) {
      out.push(finding(
        "docker.root-user", HIGH,
        "Container runs as root",
        "No USER instruction switches away from root. A process escape starts with uid 0.",
        file.rel,
        "Add a non-root user and `USER 10001:10001` before CMD.",
      ));
    }
    if (/^\s*FROM\s+\S+:latest/im.test(text) || /^\s*FROM\s+[^\s:@]+\s*$/im.test(text)) {
      out.push(finding(
        "docker.latest-tag", MEDIUM,
        "Base image is not pinned",
        "FROM uses :latest or no tag, so the image you tested is not the image you ship.",
        file.rel,
        "Pin to a version, ideally a digest.",
      ));
    }
    if (!/HEALTHCHECK/i.test(text) && !inv.stack.includes("kubernetes")) {
      out.push(finding(
        "docker.no-healthcheck", LOW,
        "No HEALTHCHECK",
        "The orchestrator cannot tell a hung container from a working one.",
        file.rel,
        "Add HEALTHCHECK, or rely on Kubernetes probes if you deploy there.",
      ));
    }
    if (/apt-get install/i.test(text) && !/rm -rf \/var\/lib\/apt\/lists/i.test(text)) {
      out.push(finding(
        "docker.apt-cache", LOW,
        "apt cache left in the image",
        "Package lists stay in the layer, adding tens of megabytes to every pull.",
        file.rel,
        "Append a cleanup of /var/lib/apt/lists to the install step.",
      ));
    }
    const secretArg = text.match(
      /^\s*(?:ARG|ENV)\s+(\w*(?:SECRET|PASSWORD|TOKEN|KEY)\w*)\s*=\s*\S+/im,
    );
    if (secretArg) {
      out.push(finding(
        "docker.baked-secret", HIGH,
        `Secret-shaped value baked into the image (${secretArg[1]})`,
        "Anything in a build arg or ENV is readable in the image history forever.",
        `${file.rel}:${lineOf(text, /^\s*(ARG|ENV)\s+\w*(SECRET|PASSWORD|TOKEN|KEY)/i)}`,
        "Mount it at runtime, or use a BuildKit secret mount.",
      ));
    }
  }
  return out;
}

// -------------------------------------------------------------- kubernetes

function kubernetesRules(inv) {
  const out = [];
  const manifests = inv.files.filter(
    (f) => [".yaml", ".yml"].includes(f.ext) && !f.rel.startsWith(".github/"),
  );

  for (const file of manifests) {
    const text = contentOf(file);
    // `kind:` at column zero only. Indented occurrences are references to a
    // kind (an ArgoCD ignoreDifferences block, an RBAC rule), not a workload
    // being declared, and treating them as one flags files that define none.
    if (!text || !/^kind:\s*(Deployment|StatefulSet|DaemonSet|Pod|Job|CronJob)\s*$/m.test(text)) {
      continue;
    }
    // Helm templates are rendered later; scanning the raw template produces a
    // false positive on every block guarded by a conditional.
    const isTemplate = text.includes("{{");

    if (!/resources:/.test(text) && !isTemplate) {
      out.push(finding(
        "k8s.no-resources", HIGH,
        "Workload has no resource requests or limits",
        "Without a memory limit one pod can evict every neighbour on the node.",
        file.rel,
        "Set resources.requests.cpu and resources.limits.memory.",
      ));
    }
    if (/image:\s*\S+:latest/.test(text)) {
      out.push(finding(
        "k8s.latest-tag", HIGH,
        "Workload pins an image to :latest",
        "A restart can silently change the running version; rollback becomes guesswork.",
        `${file.rel}:${lineOf(text, /image:\s*\S+:latest/)}`,
        "Use an immutable tag or digest set by CI.",
      ));
    }
    if (/privileged:\s*true/.test(text)) {
      out.push(finding(
        "k8s.privileged", HIGH,
        "Privileged container",
        "A privileged container is root on the node.",
        `${file.rel}:${lineOf(text, /privileged:\s*true/)}`,
        "Drop privileged and grant only the capabilities actually needed.",
      ));
    }
    if (!/(readiness|liveness)Probe:/.test(text) && !isTemplate) {
      out.push(finding(
        "k8s.no-probes", MEDIUM,
        "No readiness or liveness probe",
        "Traffic reaches the pod before it is ready, and a wedged process is never restarted.",
        file.rel,
        "Add a readinessProbe; add a livenessProbe only if a restart genuinely fixes it.",
      ));
    }
    if (!/runAsNonRoot:\s*true/.test(text) && !isTemplate) {
      out.push(finding(
        "k8s.no-securitycontext", MEDIUM,
        "No runAsNonRoot in the security context",
        "The pod may run as uid 0 even when the image sets a USER.",
        file.rel,
        "Set securityContext.runAsNonRoot true and a numeric runAsUser.",
      ));
    }
  }
  return out;
}

// -------------------------------------------------------------------- node

function nodeRules(inv) {
  const out = [];
  const pkg = inv.files.find((f) => f.rel === "package.json");
  if (!pkg) return out;

  let parsed;
  try {
    parsed = JSON.parse(contentOf(pkg) || "{}");
  } catch (error) {
    return [finding(
      "node.bad-package-json", HIGH,
      "package.json is not valid JSON",
      error.message, pkg.rel,
      "Fix the syntax; every npm command is broken until you do.",
    )];
  }

  const hasLock = inv.files.some((f) =>
    ["package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb"].includes(f.rel),
  );
  if (!hasLock) {
    out.push(finding(
      "node.no-lockfile", HIGH,
      "No lockfile committed",
      "Installs are not reproducible, and a transitive dependency can change under you.",
      "package.json",
      "Commit package-lock.json, or your package manager's equivalent.",
    ));
  }
  if (!parsed.scripts || !parsed.scripts.test) {
    out.push(finding(
      "node.no-test-script", MEDIUM,
      "No test script",
      "There is no single command that tells you whether the project works.",
      "package.json",
      "Add a test script, even if it only runs a smoke check.",
    ));
  }
  if (!parsed.engines || !parsed.engines.node) {
    out.push(finding(
      "node.no-engines", LOW,
      "No engines.node range",
      "Contributors and CI can silently run different Node versions.",
      "package.json",
      "Declare a supported Node range in engines.node.",
    ));
  }
  const deps = Object.keys(parsed.dependencies || {}).length;
  if (deps > 40) {
    out.push(finding(
      "node.dependency-count", LOW,
      `${deps} direct runtime dependencies`,
      "Every one is a supply-chain entry and an upgrade you will eventually owe.",
      "package.json",
      "Audit for packages a few lines of your own code would replace.",
    ));
  }
  return out;
}

// ------------------------------------------------------------------ python

function pythonRules(inv) {
  const out = [];
  const requirements = inv.files.find((f) => f.rel === "requirements.txt");
  const pyproject = inv.files.find((f) => f.rel === "pyproject.toml");
  if (!requirements && !pyproject) return out;

  const hasLock = inv.files.some((f) =>
    ["uv.lock", "poetry.lock", "Pipfile.lock", "requirements.lock", "pdm.lock"].includes(f.rel),
  );
  if (!hasLock) {
    out.push(finding(
      "python.no-lockfile", HIGH,
      "No Python lockfile",
      "Installs resolve differently over time, so a green CI run does not mean a green deploy.",
      pyproject ? "pyproject.toml" : "requirements.txt",
      "Generate and commit a lockfile, or pin every requirement.",
    ));
  }
  if (requirements) {
    const text = contentOf(requirements) || "";
    const loose = text
      .split(/\r?\n/)
      .filter((line) => line.trim() && !line.trim().startsWith("#") && !/[=<>~]/.test(line));
    if (loose.length) {
      out.push(finding(
        "python.unpinned", MEDIUM,
        `${loose.length} unpinned requirement(s)`,
        `Unpinned: ${loose.slice(0, 5).map((l) => l.trim()).join(", ")}`,
        "requirements.txt",
        "Pin exact versions, or move to a lockfile.",
      ));
    }
  }
  return out;
}

// ---------------------------------------------------------------------- ci

function ciRules(inv) {
  const out = [];
  const workflows = inv.files.filter((f) => f.rel.startsWith(".github/workflows/"));
  const hasCI =
    workflows.length > 0 ||
    inv.files.some((f) =>
      [".gitlab-ci.yml", "Jenkinsfile", ".circleci/config.yml"].includes(f.rel),
    );

  if (!hasCI) {
    out.push(finding(
      "ci.missing", MEDIUM,
      "No CI configuration found",
      "Nothing checks a change before it merges.",
      null,
      "Add a workflow that at minimum installs, lints and tests.",
    ));
    return out;
  }

  const runsTests = workflows.some((file) => {
    const text = contentOf(file) || "";
    return /\b(pytest|npm test|go test|cargo test|jest|vitest|mvn test|make test)\b/.test(text);
  });
  if (workflows.length && !runsTests) {
    out.push(finding(
      "ci.no-tests", MEDIUM,
      "CI does not appear to run tests",
      "No workflow step invokes a recognisable test command.",
      workflows[0].rel,
      "Add a test step, and make the job fail when it fails.",
    ));
  }
  return out;
}

// ----------------------------------------------------------------- secrets

//: Shapes that are almost never a false positive.
const SECRET_PATTERNS = [
  [/\bAKIA[0-9A-Z]{16}\b/, "AWS access key id"],
  [/\bghp_[A-Za-z0-9]{36}\b/, "GitHub personal access token"],
  [/\bsk-ant-[A-Za-z0-9_-]{20,}/, "Anthropic API key"],
  [/\bxox[baprs]-[A-Za-z0-9-]{10,}/, "Slack token"],
  [/-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/, "private key"],
];

function secretRules(inv) {
  const out = [];
  const gitignore = inv.files.find((f) => f.rel === ".gitignore");
  const ignored = contentOf(gitignore) || "";

  for (const file of inv.files) {
    if (file.rel.startsWith(".env") && inv.hasGit && !/^\s*\.env/m.test(ignored)) {
      out.push(finding(
        "secrets.env-not-ignored", HIGH,
        `${file.rel} is not gitignored`,
        "An env file in a repository is a credential leak waiting for a push.",
        file.rel,
        "Add .env* to .gitignore and rotate anything already committed.",
      ));
    }
    const text = contentOf(file);
    if (!text) continue;
    for (const [pattern, label] of SECRET_PATTERNS) {
      if (pattern.test(text)) {
        out.push(finding(
          "secrets.hardcoded", HIGH,
          `Possible ${label} in ${file.rel}`,
          "A live credential in source is compromised the moment the repo is shared.",
          `${file.rel}:${lineOf(text, pattern)}`,
          "Remove it, rotate the credential, and load it from the environment.",
        ));
        break; // one finding per file is enough to act on
      }
    }
  }
  return out;
}

// --------------------------------------------------------------- terraform

function terraformRules(inv) {
  const out = [];
  const tf = inv.files.filter((f) => f.ext === ".tf");
  if (!tf.length) return out;

  const hasBackend = tf.some((f) => /backend\s+"/.test(contentOf(f) || ""));
  if (!hasBackend) {
    out.push(finding(
      "terraform.no-backend", HIGH,
      "No remote state backend",
      "State lives on one laptop. Two people applying at once corrupts it.",
      tf[0].rel,
      "Configure a remote backend with locking.",
    ));
  }
  for (const file of tf) {
    const text = contentOf(file) || "";
    if (/resource\s+"aws_s3_bucket"/.test(text) && !/encryption/i.test(text)) {
      out.push(finding(
        "terraform.unencrypted-bucket", MEDIUM,
        "S3 bucket without server-side encryption",
        "Objects are stored unencrypted at rest.",
        file.rel,
        "Add a server-side encryption configuration resource.",
      ));
    }
  }
  return out;
}

// ------------------------------------------------------------------ compose

function composeRules(inv) {
  const out = [];
  for (const file of inv.files.filter((f) => /^docker-compose\.ya?ml$/.test(f.name))) {
    const text = contentOf(file) || "";
    if (/network_mode:\s*["']?host/.test(text)) {
      out.push(finding(
        "compose.host-network", MEDIUM,
        "Service uses host networking",
        "The container shares the host network namespace, bypassing port isolation.",
        file.rel,
        "Publish specific ports instead.",
      ));
    }
    if (/privileged:\s*true/.test(text)) {
      out.push(finding(
        "compose.privileged", HIGH,
        "Privileged compose service",
        "A privileged container is effectively root on the host.",
        file.rel,
        "Remove privileged and grant explicit capabilities.",
      ));
    }
  }
  return out;
}

// ----------------------------------------------------------------- hygiene

function hygieneRules(inv) {
  const out = [];
  const has = (name) => inv.files.some((f) => f.rel.toLowerCase() === name);

  if (!has(".gitignore") && inv.hasGit) {
    out.push(finding(
      "repo.no-gitignore", MEDIUM, "No .gitignore",
      "Build output and local env files will eventually be committed.",
      null, "Add a .gitignore for your stack.",
    ));
  }
  if (!inv.files.some((f) => /^readme(\.md)?$/i.test(f.rel))) {
    out.push(finding(
      "repo.no-readme", LOW, "No README",
      "Nobody can tell what this is or how to run it.",
      null, "Add a README with what it does and how to start it.",
    ));
  }
  if (!inv.files.some((f) => /^licen[sc]e/i.test(f.rel))) {
    out.push(finding(
      "repo.no-license", LOW, "No LICENSE",
      "Without a licence nobody may legally use or contribute to the code.",
      null, "Add a LICENSE file.",
    ));
  }
  const big = inv.files.filter((f) => f.size > 5 * 1024 * 1024);
  if (big.length) {
    out.push(finding(
      "repo.large-files", LOW, `${big.length} file(s) over 5 MB`,
      big.slice(0, 3).map((f) => `${f.rel} (${(f.size / 1048576).toFixed(1)} MB)`).join(", "),
      big[0].rel, "Move them to object storage or Git LFS.",
    ));
  }
  return out;
}

export const RULE_SETS = {
  docker: dockerRules,
  kubernetes: kubernetesRules,
  node: nodeRules,
  python: pythonRules,
  ci: ciRules,
  secrets: secretRules,
  terraform: terraformRules,
  compose: composeRules,
  hygiene: hygieneRules,
};

export const SEVERITY_ORDER = { high: 0, medium: 1, low: 2 };

export function runRules(inv, { mute = [] } = {}) {
  const muted = new Set(mute);
  const findings = [];
  for (const [group, rule] of Object.entries(RULE_SETS)) {
    for (const item of rule(inv)) {
      if (muted.has(item.id)) continue;
      findings.push({ ...item, group });
    }
  }
  findings.sort(
    (a, b) =>
      SEVERITY_ORDER[a.severity] - SEVERITY_ORDER[b.severity] || a.id.localeCompare(b.id),
  );
  return findings;
}
