/**
 * Filesystem scan and stack detection.
 *
 * One walk, one inventory. Every rule reads from that inventory rather than
 * hitting the disk again, because a linter that stats the tree once per rule
 * is a linter nobody runs on a monorepo.
 */

import { readdirSync, readFileSync, statSync, existsSync } from "node:fs";
import { join, relative, extname, basename } from "node:path";

const TEXT_EXTENSIONS = new Set([
  ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".go", ".rs", ".rb",
  ".java", ".kt", ".php", ".sh", ".bash", ".yaml", ".yml", ".json", ".toml",
  ".ini", ".cfg", ".env", ".tf", ".tfvars", ".md", ".sql", ".xml", ".gradle",
]);

//: Files worth reading in full even without a useful extension.
const NAMED_FILES = new Set([
  "Dockerfile", "Makefile", "Procfile", "Jenkinsfile", "docker-compose.yml",
  "docker-compose.yaml", "requirements.txt", ".gitignore", ".dockerignore",
  ".env", ".env.local", ".env.production",
]);

const MAX_FILE_BYTES = 512 * 1024;

export function walk(root, { ignore = [], maxDepth = 6 } = {}) {
  const skip = new Set(ignore);
  const files = [];

  function visit(dir, depth) {
    if (depth > maxDepth) return;
    let entries;
    try {
      entries = readdirSync(dir, { withFileTypes: true });
    } catch {
      return; // unreadable directory is not worth failing the whole scan
    }
    for (const entry of entries) {
      if (skip.has(entry.name)) continue;
      const full = join(dir, entry.name);
      if (entry.isDirectory()) {
        visit(full, depth + 1);
      } else if (entry.isFile()) {
        let size = 0;
        try {
          size = statSync(full).size;
        } catch {
          continue;
        }
        files.push({
          path: full,
          rel: relative(root, full).split("\\").join("/"),
          name: entry.name,
          ext: extname(entry.name),
          size,
        });
      }
    }
  }

  visit(root, 0);
  return files;
}

export function readable(file) {
  return (
    file.size <= MAX_FILE_BYTES &&
    (TEXT_EXTENSIONS.has(file.ext) ||
      NAMED_FILES.has(file.name) ||
      file.name.startsWith("Dockerfile"))
  );
}

export function contentOf(file) {
  // Callers routinely pass the result of `.find(...)`, which is undefined when
  // the file does not exist. Returning null here beats a guard at every site.
  if (!file) return null;
  if (file._content !== undefined) return file._content;
  try {
    file._content = readable(file) ? readFileSync(file.path, "utf8") : null;
  } catch {
    file._content = null;
  }
  return file._content;
}

/** What kind of project is this? Multiple answers are normal. */
export function detectStack(root, files) {
  const has = (rel) => files.some((f) => f.rel === rel);
  const any = (predicate) => files.some(predicate);
  const stack = [];

  if (has("package.json")) stack.push("node");
  if (has("pyproject.toml") || has("requirements.txt") || has("setup.py")) stack.push("python");
  if (has("go.mod")) stack.push("go");
  if (has("Cargo.toml")) stack.push("rust");
  if (has("pom.xml") || has("build.gradle") || has("build.gradle.kts")) stack.push("jvm");
  if (has("Gemfile")) stack.push("ruby");
  if (any((f) => f.name.startsWith("Dockerfile"))) stack.push("docker");
  if (any((f) => f.name === "docker-compose.yml" || f.name === "docker-compose.yaml")) {
    stack.push("compose");
  }
  if (any((f) => f.ext === ".tf")) stack.push("terraform");
  if (looksKubernetes(files)) stack.push("kubernetes");
  if (any((f) => f.rel.startsWith(".github/workflows/"))) stack.push("github-actions");
  if (has("Chart.yaml") || any((f) => f.name === "Chart.yaml")) stack.push("helm");

  return [...new Set(stack)];
}

function looksKubernetes(files) {
  return files.some((file) => {
    if (![".yaml", ".yml"].includes(file.ext)) return false;
    if (file.rel.startsWith(".github/")) return false;
    const text = contentOf(file);
    return Boolean(text) && /^\s*kind:\s*\w+/m.test(text) && /^\s*apiVersion:/m.test(text);
  });
}

/**
 * Services this project appears to expose, with the port if we can find one.
 * Best effort by design: a wrong guess costs a dashboard row, not a decision.
 */
export function detectServices(root, files) {
  const services = [];

  const compose = files.find((f) => /^docker-compose\.ya?ml$/.test(f.name));
  if (compose) {
    const text = contentOf(compose) || "";
    // Everything under `services:` up to the next top-level key. Without the
    // stop condition a `volumes:` block reads as three more services.
    const after = text.split(/^services:/m)[1] || "";
    const block = after.split(/^(?=\S)/m)[0] || "";
    for (const match of block.matchAll(/^ {2}([a-zA-Z0-9_.-]+):/gm)) {
      services.push({ name: match[1], source: compose.rel, kind: "compose" });
    }
  }

  for (const file of files) {
    if (!file.name.startsWith("Dockerfile")) continue;
    const text = contentOf(file) || "";
    const expose = text.match(/^\s*EXPOSE\s+(\d+)/im);
    services.push({
      name: file.rel === "Dockerfile" ? basename(root) : file.rel,
      source: file.rel,
      kind: "container",
      port: expose ? Number(expose[1]) : undefined,
    });
  }

  const pkg = files.find((f) => f.rel === "package.json");
  if (pkg) {
    try {
      const parsed = JSON.parse(contentOf(pkg) || "{}");
      if (parsed.scripts?.start || parsed.scripts?.dev) {
        services.push({ name: parsed.name || basename(root), source: "package.json", kind: "node" });
      }
    } catch {
      /* malformed package.json is reported by a rule, not here */
    }
  }

  const seen = new Set();
  return services.filter((s) => !seen.has(s.name) && seen.add(s.name));
}

export function inventory(root, options) {
  const files = walk(root, options);
  return {
    root,
    files,
    stack: detectStack(root, files),
    services: detectServices(root, files),
    counts: countByExtension(files),
    totalBytes: files.reduce((sum, f) => sum + f.size, 0),
    hasGit: existsSync(join(root, ".git")),
  };
}

function countByExtension(files) {
  const counts = {};
  for (const file of files) {
    const key = file.ext || file.name;
    counts[key] = (counts[key] || 0) + 1;
  }
  return Object.fromEntries(
    Object.entries(counts).sort((a, b) => b[1] - a[1]).slice(0, 15),
  );
}
