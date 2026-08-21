/**
 * Charts.
 *
 * Hand-built SVG, because the alternative is a charting library and this
 * package has no dependencies on purpose. Every function here takes plain
 * data and returns a string, so the same code renders the first paint on the
 * server and every update in the browser.
 *
 * Two rules the whole file follows:
 *   - Colour is never the only encoding. Every series carries a label and a
 *     value, so the chart still reads in greyscale or to a screen reader.
 *   - Nothing is drawn that was not measured. Where the underlying data is
 *     weak, the chart says so rather than inventing a smooth line.
 */

export const SEVERITY_COLOR = {
  high: "var(--bad)",
  medium: "var(--warn)",
  low: "var(--muted)",
};

const PALETTE = [
  "#5b8cff", "#a273ff", "#3fd2ff", "#3ddc97",
  "#ffc247", "#ff6b81", "#7de2d1", "#c084fc",
];

export const paletteAt = (index) => PALETTE[index % PALETTE.length];

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);

/* ------------------------------------------------------------------ donut
 *
 * Segments are drawn as dash gaps on one circle, which is why there is no
 * arc-path maths here. The legend carries the numbers; the ring only carries
 * the proportion.
 */
export function donut(segments, options = {}) {
  const { size = 132, thickness = 13, centre = "" } = options;
  const usable = segments.filter((s) => s.value > 0);
  const total = usable.reduce((sum, s) => sum + s.value, 0);
  const r = (size - thickness) / 2;
  const c = 2 * Math.PI * r;

  if (!total) {
    return `<div class="chart-empty" style="height:${size}px">No data</div>`;
  }

  let offset = 0;
  const rings = usable
    .map((s, index) => {
      const len = (s.value / total) * c;
      const dash = `<circle class="donut-seg" cx="${size / 2}" cy="${size / 2}" r="${r}"
        stroke="${s.color || paletteAt(index)}" stroke-width="${thickness}"
        stroke-dasharray="${len.toFixed(2)} ${(c - len).toFixed(2)}"
        stroke-dashoffset="${(-offset).toFixed(2)}"
        style="--len:${len.toFixed(2)};--c:${c.toFixed(2)};--i:${index}"
        ><title>${esc(s.label)}: ${s.value}</title></circle>`;
      offset += len;
      return dash;
    })
    .join("");

  return `<div class="donut" style="width:${size}px;height:${size}px">
    <svg viewBox="0 0 ${size} ${size}" role="img"
      aria-label="${esc(usable.map((s) => `${s.label} ${s.value}`).join(", "))}">
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none"
        stroke="rgba(255,255,255,.06)" stroke-width="${thickness}"/>
      <g fill="none" stroke-linecap="butt"
        transform="rotate(-90 ${size / 2} ${size / 2})">${rings}</g>
    </svg>
    ${centre ? `<div class="donut-centre">${centre}</div>` : ""}
  </div>`;
}

export function legend(segments) {
  return `<ul class="legend-list">${segments
    .map(
      (s, index) => `<li><span class="swatch" style="background:${
        s.color || paletteAt(index)
      }"></span><span class="lg-label">${esc(s.label)}</span>
      <b class="lg-val">${s.value}</b></li>`,
    )
    .join("")}</ul>`;
}

/* -------------------------------------------------------------- bar chart
 *
 * Horizontal, because category names are words and words are wider than they
 * are tall. Bars scale against the largest value rather than the total, so a
 * single dominant category does not flatten everything else into nothing.
 */
export function barsH(rows, options = {}) {
  if (!rows.length) return `<div class="chart-empty">No data</div>`;
  const { unit = "", raw = false } = options;
  const max = Math.max(...rows.map((r) => r.value), 1);

  return `<ul class="barsh${raw ? " raw" : ""}">${rows
    .map(
      (r, index) => `<li style="--i:${index}">
      <span class="bh-label" title="${esc(r.label)}">${
        r.icon ? `<svg class="i i-sm" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-${r.icon}"/></svg>` : ""
      }${esc(r.label)}</span>
      <span class="bh-track">
        <span class="bh-fill" style="--w:${((r.value / max) * 100).toFixed(1)}%;
          background:${r.color || paletteAt(index)}"></span>
      </span>
      <b class="bh-val">${r.value}${unit}</b>
    </li>`,
    )
    .join("")}</ul>`;
}

/* ------------------------------------------------------------ area chart
 *
 * A filled line for counts over buckets. The line is drawn with a
 * dash-offset animation, so it draws in left to right rather than fading.
 */
export function area(values, options = {}) {
  const { w = 560, h = 130, label = "", pad = 6 } = options;
  if (values.length < 2) return `<div class="chart-empty" style="height:${h}px">Not enough data yet</div>`;

  const max = Math.max(...values, 1);
  const step = (w - pad * 2) / (values.length - 1);
  const y = (v) => h - pad - (v / max) * (h - pad * 2);
  const pts = values.map((v, index) => [pad + index * step, y(v)]);

  // Catmull-Rom-ish smoothing: a midpoint quadratic through each pair. Cheap,
  // and it never overshoots below zero the way a naive cubic does.
  let line = `M ${pts[0][0].toFixed(1)} ${pts[0][1].toFixed(1)}`;
  for (let i = 1; i < pts.length; i += 1) {
    const [px, py] = pts[i - 1];
    const [cx, cy] = pts[i];
    const mx = (px + cx) / 2;
    line += ` Q ${px.toFixed(1)} ${py.toFixed(1)} ${mx.toFixed(1)} ${((py + cy) / 2).toFixed(1)}`;
    line += ` Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${cx.toFixed(1)} ${cy.toFixed(1)}`;
  }
  const fill = `${line} L ${pts[pts.length - 1][0].toFixed(1)} ${h - pad} L ${pts[0][0].toFixed(1)} ${h - pad} Z`;

  return `<svg class="area" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none"
    role="img" aria-label="${esc(label)}: peak ${max}">
    <defs>
      <linearGradient id="areaFill" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0" stop-color="var(--a1)" stop-opacity=".55"/>
        <stop offset="1" stop-color="var(--a1)" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <path class="area-fill" d="${fill}" fill="url(#areaFill)"/>
    <path class="area-line" d="${line}" fill="none" stroke="var(--a3)"
      stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`;
}

/* ---------------------------------------------------------- spark columns
 * Small discrete columns. Used where each bucket is a countable thing and a
 * continuous line would imply data between the points that does not exist.
 */
export function sparkColumns(values, options = {}) {
  const { h = 54, label = "", colorFor } = options;
  if (!values.length) return `<div class="chart-empty" style="height:${h}px">No data</div>`;
  const max = Math.max(...values, 1);
  return `<div class="spark" style="height:${h}px" role="img"
    aria-label="${esc(label)}: peak ${max}">${values
    .map(
      (v, index) =>
        `<span style="--h:${((v / max) * 100).toFixed(1)}%;--i:${index}${
          colorFor ? `;background:${colorFor(v, index)}` : ""
        }"><i>${v}</i></span>`,
    )
    .join("")}</div>`;
}

/* ------------------------------------------------------------------ time
 *
 * Log lines carry two possible clocks: the timestamp printed in the line
 * itself, and the moment Cairn read it. For a file that is being tailed from
 * the end, the second one is all-but-identical for every line and would draw
 * a single meaningless spike. So: prefer a timestamp parsed out of the line,
 * fall back to ingest time, and report which one was used so the chart can
 * say so rather than imply a precision it does not have.
 */
const TS_PATTERNS = [
  /\b(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)/,
  /\b(\d{2}\/[A-Za-z]{3}\/\d{4}:\d{2}:\d{2}:\d{2})/,
];

export function timestampOf(entry) {
  for (const pattern of TS_PATTERNS) {
    const match = pattern.exec(entry.line);
    if (match) {
      const parsed = Date.parse(match[1].replace(" ", "T"));
      if (!Number.isNaN(parsed)) return { at: parsed, parsed: true };
    }
  }
  const fallback = Date.parse(entry.at);
  return { at: Number.isNaN(fallback) ? Date.now() : fallback, parsed: false };
}

export function bucketByTime(entries, buckets = 32) {
  if (!entries.length) return { values: [], parsed: 0, spanMs: 0 };
  const stamps = entries.map(timestampOf);
  const parsed = stamps.filter((s) => s.parsed).length;
  const times = stamps.map((s) => s.at);
  const min = Math.min(...times);
  const max = Math.max(...times);
  const span = max - min;

  // Every line landed in the same instant — a tail of a static file, most
  // likely. Bucketing by time would be a lie; bucket by position instead.
  if (span < 1000) {
    const values = new Array(buckets).fill(0);
    entries.forEach((_, index) => {
      values[Math.min(buckets - 1, Math.floor((index / entries.length) * buckets))] += 1;
    });
    return { values, parsed, spanMs: 0, byPosition: true };
  }

  const values = new Array(buckets).fill(0);
  for (const t of times) {
    values[Math.min(buckets - 1, Math.floor(((t - min) / span) * buckets))] += 1;
  }
  return { values, parsed, spanMs: span, byPosition: false };
}

export function humanSpan(ms) {
  if (ms < 1000) return "under a second";
  const s = Math.round(ms / 1000);
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m`;
  return `${Math.round(m / 60)}h`;
}

/* -------------------------------------------------------------- tech grid
 *
 * These are representative glyphs, not brand logos: shipping approximations
 * of someone else's trademark is worse than shipping an honest abstraction.
 * The name under each tile is what actually identifies it.
 */
export const TECH_ICON = {
  docker: "tech-container",
  compose: "tech-stack",
  kubernetes: "tech-helm",
  node: "tech-hex",
  python: "tech-python",
  terraform: "tech-tf",
  "github-actions": "tech-flow",
  postgres: "db",
  redis: "db",
  mcp: "tech-plug",
  helm: "tech-helm",
  aws: "tech-cloud",
  go: "tech-hex",
  rust: "tech-gear",
  java: "tech-cup",
};

export function techGrid(stack) {
  if (!stack.length) {
    return `<div class="chart-empty">Nothing detected</div>`;
  }
  return `<ul class="tech-grid">${stack
    .map(
      (name, index) => `<li style="--i:${index}">
      <span class="tech-ico"><svg class="i" viewBox="0 0 24 24" aria-hidden="true"><use
        href="#i-${TECH_ICON[name] || "box"}"/></svg></span>
      <span class="tech-name">${esc(name)}</span>
    </li>`,
    )
    .join("")}</ul>`;
}
