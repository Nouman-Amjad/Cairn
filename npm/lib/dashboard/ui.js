/**
 * The dashboard shell.
 *
 * Server-renders the frame and the first paint of data, then `client.js`
 * takes over for live updates. No bundler, no framework, no CDN — the whole
 * UI is two files you can read, and it works with the network unplugged.
 *
 * That constraint shapes the design: every icon is an inline SVG symbol,
 * every effect is a CSS transform or filter, and the only font is the one
 * already on the machine. Nothing here waits on a network request.
 */

import {
  donut, legend, barsH, techGrid, paletteAt, SEVERITY_COLOR,
} from "./charts.js";


/* ------------------------------------------------------------------ icons
 *
 * A hand-drawn set on a 24px grid, 1.75 stroke, round caps and joins. One
 * family, one weight — mixing icon sets is the fastest way to make an
 * interface look assembled rather than designed. Rendered once as a <symbol>
 * sprite so a finding row costs a <use>, not a fresh copy of the paths.
 */
export const ICONS = {
  layers:
    '<path d="m12 2.5 8.5 4.6L12 11.7 3.5 7.1 12 2.5Z"/><path d="m3.5 12 8.5 4.6 8.5-4.6"/><path d="m3.5 16.9 8.5 4.6 8.5-4.6"/>',
  scan: '<path d="M3.5 8V5.8a2.3 2.3 0 0 1 2.3-2.3H8"/><path d="M16 3.5h2.2a2.3 2.3 0 0 1 2.3 2.3V8"/><path d="M20.5 16v2.2a2.3 2.3 0 0 1-2.3 2.3H16"/><path d="M8 20.5H5.8a2.3 2.3 0 0 1-2.3-2.3V16"/><circle cx="12" cy="12" r="3.2"/>',
  server:
    '<rect x="3" y="4" width="18" height="7" rx="2.2"/><rect x="3" y="13" width="18" height="7" rx="2.2"/><path d="M7 7.5h.01M7 16.5h.01"/><path d="M14 7.5h3M14 16.5h3"/>',
  terminal:
    '<rect x="2.5" y="3.5" width="19" height="17" rx="2.6"/><path d="m7 9.5 3 2.5-3 2.5"/><path d="M13 15h4"/>',
  sliders:
    '<path d="M4 7h8M18 7h2M4 12h2M12 12h8M4 17h10M20 17h0"/><circle cx="15" cy="7" r="2.1"/><circle cx="9" cy="12" r="2.1"/><circle cx="17" cy="17" r="2.1"/>',
  chat: '<path d="M20.5 11.6a7.9 7.9 0 0 1-11.4 7.1l-5.6 1.8 1.8-5.6A7.9 7.9 0 1 1 20.5 11.6Z"/><path d="M9 12h.01M12 12h.01M15 12h.01"/>',
  shield:
    '<path d="M12 3 5.5 5.8v5.4c0 4.1 2.8 7.9 6.5 9.3 3.7-1.4 6.5-5.2 6.5-9.3V5.8L12 3Z"/><path d="M12 8.8v3.6"/><path d="M12 15.6h.01"/>',
  warn: '<path d="M10.3 4.4 2.7 17.4a2 2 0 0 0 1.7 3h15.2a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0Z"/><path d="M12 9.6v3.8"/><path d="M12 16.8h.01"/>',
  info: '<circle cx="12" cy="12" r="8.7"/><path d="M12 11.2v4.6"/><path d="M12 8.2h.01"/>',
  refresh:
    '<path d="M20.3 12a8.3 8.3 0 1 1-2.6-6"/><path d="M20.6 4.6V10H15.2"/>',
  activity: '<path d="M2.8 12h4l2.6-7 5 14 2.6-7h4.2"/>',
  file: '<path d="M14 3.2H7.4A2.2 2.2 0 0 0 5.2 5.4v13.2a2.2 2.2 0 0 0 2.2 2.2h9.2a2.2 2.2 0 0 0 2.2-2.2V8.2L14 3.2Z"/><path d="M13.8 3.4v4.6h4.8"/>',
  box: '<path d="m12 2.8 8.2 4.4v9.6L12 21.2 3.8 16.8V7.2L12 2.8Z"/><path d="m3.8 7.2 8.2 4.5 8.2-4.5"/><path d="M12 11.7v9.5"/>',
  check: '<path d="m4.5 12.5 5 5 10-11"/>',
  close: '<path d="m5.5 5.5 13 13M18.5 5.5l-13 13"/>',
  play: '<path d="M7.5 4.8 19 12 7.5 19.2V4.8Z"/>',
  spark:
    '<path d="M12 2.8 13.9 9 20 10.9 13.9 12.8 12 19l-1.9-6.2L4 10.9 10.1 9 12 2.8Z"/><path d="M19 17.2 19.7 19.4 22 20l-2.3.7-.7 2.2-.7-2.2-2.3-.7 2.3-.6.7-2.2Z"/>',
  send: '<path d="M21 3 10.5 13.5"/><path d="M21 3 14.4 21l-3.9-7.5L3 9.6 21 3Z"/>',
  clock: '<circle cx="12" cy="12" r="8.7"/><path d="M12 7v5.3l3.4 2"/>',
  cpu: '<rect x="6.5" y="6.5" width="11" height="11" rx="2"/><path d="M10 3v3.5M14 3v3.5M10 17.5V21M14 17.5V21M3 10h3.5M3 14h3.5M17.5 10H21M17.5 14H21"/>',
  'tech-container': '<rect x="2.5" y="9" width="19" height="8.5" rx="1.6"/><path d="M6 9V6.2h3.2V9M11 9V6.2h3.2V9M16 9V5h2.6v4"/><path d="M4.5 20h15"/>',
  'tech-stack': '<path d="m12 3 8 4-8 4-8-4 8-4Z"/><path d="m4 11 8 4 8-4"/><path d="m4 15.5 8 4 8-4"/>',
  'tech-helm': '<circle cx="12" cy="12" r="8.6"/><circle cx="12" cy="12" r="3"/><path d="M12 3.4v5.2M12 15v5.6M3.6 12h5M15.4 12h5M6.2 6.2l3.6 3.6M14.2 14.2l3.6 3.6M17.8 6.2l-3.6 3.6M9.8 14.2l-3.6 3.6"/>',
  'tech-hex': '<path d="m12 2.6 8.4 4.7v9.4L12 21.4 3.6 16.7V7.3L12 2.6Z"/><path d="M9 15V9.6l6 4.8V9"/>',
  'tech-python': '<path d="M12 3c-3 0-4.4 1-4.4 2.8V9h4.6"/><path d="M7.6 9H5.4C3.7 9 3 10.4 3 12.6S3.7 16 5.4 16h2.2v-3.2c0-1.6 1.2-2.8 2.8-2.8h3.2c1.4 0 2.4-1 2.4-2.4V5.8C16 4 14.6 3 12 3Z"/><path d="M12 21c3 0 4.4-1 4.4-2.8V15h-4.6"/><path d="M16.4 15h2.2c1.7 0 2.4-1.4 2.4-3.6S20.3 8 18.6 8h-2.2v3.2c0 1.6-1.2 2.8-2.8 2.8h-3.2C9 14 8 15 8 16.4v1.8C8 20 9.4 21 12 21Z"/>',
  'tech-tf': '<path d="M9.4 5.2 14.6 8v5.6L9.4 10.8V5.2Z"/><path d="M15.6 8.6 20.4 11.4V17L15.6 14.2V8.6Z"/><path d="M3.6 3v5.6l4.8 2.8V5.8L3.6 3Z"/><path d="M9.4 15.4 14.6 18.2v3.2L9.4 18.6v-3.2Z"/>',
  'tech-flow': '<circle cx="5.5" cy="6" r="2.4"/><circle cx="5.5" cy="18" r="2.4"/><circle cx="18.5" cy="12" r="2.4"/><path d="M7.9 6.8c4 .9 6 2.4 8.3 4.2M7.9 17.2c4-.9 6-2.4 8.3-4.2"/>',
  'tech-plug': '<path d="M9 3v5M15 3v5"/><path d="M6.5 8h11v3.2a5.5 5.5 0 0 1-11 0V8Z"/><path d="M12 16.7V21"/>',
  'tech-cloud': '<path d="M7 18.5a4.3 4.3 0 0 1-.5-8.6 5.6 5.6 0 0 1 10.8-1.2A3.9 3.9 0 0 1 17.6 18.5H7Z"/>',
  'tech-gear': '<circle cx="12" cy="12" r="3.1"/><path d="M12 2.6v2.8M12 18.6v2.8M21.4 12h-2.8M5.4 12H2.6M18.6 5.4l-2 2M7.4 16.6l-2 2M18.6 18.6l-2-2M7.4 7.4l-2-2"/>',
  'tech-cup': '<path d="M4.5 8h12v6.5a4 4 0 0 1-4 4h-4a4 4 0 0 1-4-4V8Z"/><path d="M16.5 9.5h1.8a2.4 2.4 0 0 1 0 4.8h-1.8"/><path d="M8 2.5c-1 1.4-1 2.4 0 3.5M12 2.5c-1 1.4-1 2.4 0 3.5"/>',
  db: '<ellipse cx="12" cy="5.8" rx="7.5" ry="3.1"/><path d="M4.5 5.8v12.4c0 1.7 3.4 3.1 7.5 3.1s7.5-1.4 7.5-3.1V5.8"/><path d="M4.5 12c0 1.7 3.4 3.1 7.5 3.1s7.5-1.4 7.5-3.1"/>',
};

const icon = (name, className = "") =>
  `<svg class="i ${className}" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`;

const sprite = () =>
  `<svg class="sprite" aria-hidden="true"><defs>${Object.entries(ICONS)
    .map(([name, path]) => `<symbol id="i-${name}" viewBox="0 0 24 24">${path}</symbol>`)
    .join("")}</defs></svg>`;

/* --------------------------------------------------------------------- css */

const CSS = `
/* Design tokens. Everything downstream reads from here, so a theme change
   is an edit in one block rather than a search across the file. */
:root {
  color-scheme: dark;

  /* Surfaces are translucent and stacked, so depth comes from layering
     rather than from drawing more borders. */
  --bg:#06080e;
  --bg-lift:#0a0d16;
  --glass:rgba(23,29,45,.56);
  --glass-2:rgba(31,39,60,.5);
  --glass-3:rgba(16,21,34,.72);
  --edge:rgba(140,167,222,.13);
  --edge-hi:rgba(150,182,255,.32);

  --text:#e9eefb;
  --muted:#93a3c0;
  --faint:#606f8b;

  --a1:#5b8cff;
  --a2:#a273ff;
  --a3:#3fd2ff;
  --good:#3ddc97;
  --warn:#ffc247;
  --bad:#ff6b81;

  /* One motion vocabulary. Durations and curves are shared so every
     transition in the app feels like it came from the same place. */
  --d1:140ms;
  --d2:260ms;
  --d3:420ms;
  --e-out:cubic-bezier(.16,1,.3,1);
  --e-in:cubic-bezier(.7,0,.84,0);
  --e-spring:cubic-bezier(.34,1.4,.64,1);

  --r-s:10px;
  --r-m:16px;
  --r-l:22px;

  --mono:ui-monospace,"SF Mono","Cascadia Code",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",sans-serif;
}

*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{
  margin:0;min-height:100dvh;overflow-x:hidden;
  background:var(--bg);color:var(--text);
  font:15px/1.6 var(--sans);
  -webkit-font-smoothing:antialiased;
}
a{color:var(--a3);text-decoration:none}
a:hover{text-decoration:underline}
h1,h2,h3{margin:0;font-weight:600;letter-spacing:-.01em}
code{font-family:var(--mono);font-size:.9em}
.sprite{position:absolute;width:0;height:0;overflow:hidden}

/* Icons inherit colour and sit on the text baseline. Sizing is a token,
   not a per-instance decision. */
.i{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;
  stroke-width:1.75;stroke-linecap:round;stroke-linejoin:round}
.i-lg{width:22px;height:22px}
.i-sm{width:15px;height:15px;stroke-width:2}

:focus-visible{outline:2px solid var(--a3);outline-offset:3px;border-radius:8px}
::selection{background:rgba(91,140,255,.35)}

*::-webkit-scrollbar{width:11px;height:11px}
*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{background:rgba(120,145,195,.2);border-radius:99px;
  border:3px solid transparent;background-clip:content-box}
*::-webkit-scrollbar-thumb:hover{background:rgba(145,175,240,.42);background-clip:content-box}

.skip{position:absolute;left:-9999px;top:8px;z-index:99;background:var(--a1);
  color:#fff;padding:10px 18px;border-radius:8px;font-weight:600}
.skip:focus{left:16px}

/* ---------------------------------------------------------- depth layers
   Three fixed layers behind the content: drifting aurora, a receding grid
   floor, and a fine noise wash. They never repaint on scroll because they
   only ever animate transform and background-position. */

.aurora{position:fixed;inset:-25%;z-index:-3;pointer-events:none;
  filter:blur(90px);opacity:.55}
.aurora b{position:absolute;display:block;border-radius:50%;
  mix-blend-mode:screen;will-change:transform}
.aurora b:nth-child(1){width:46vw;height:46vw;left:2%;top:-4%;
  background:radial-gradient(circle,#2d5cff 0%,transparent 68%);
  animation:drift-a 30s ease-in-out infinite alternate}
.aurora b:nth-child(2){width:40vw;height:40vw;right:0;top:14%;
  background:radial-gradient(circle,#8b46ff 0%,transparent 68%);
  animation:drift-b 36s ease-in-out infinite alternate}
.aurora b:nth-child(3){width:36vw;height:36vw;left:26%;bottom:-8%;
  background:radial-gradient(circle,#00b4d8 0%,transparent 68%);
  animation:drift-c 26s ease-in-out infinite alternate}
@keyframes drift-a{to{transform:translate3d(14vw,10vh,0) scale(1.22)}}
@keyframes drift-b{to{transform:translate3d(-16vw,8vh,0) scale(.82)}}
@keyframes drift-c{to{transform:translate3d(10vw,-12vh,0) scale(1.18)}}

.floor{position:fixed;left:0;right:0;bottom:0;height:56vh;z-index:-2;
  pointer-events:none;perspective:280px;opacity:.2;
  -webkit-mask-image:linear-gradient(to top,#000 0%,transparent 92%);
  mask-image:linear-gradient(to top,#000 0%,transparent 92%)}
.floor::before{content:"";position:absolute;inset:-60% -60% -20%;
  background-image:
    linear-gradient(rgba(120,163,255,.55) 1px,transparent 1px),
    linear-gradient(90deg,rgba(120,163,255,.55) 1px,transparent 1px);
  background-size:52px 52px;
  transform:rotateX(76deg);transform-origin:50% 100%;
  animation:floor-run 14s linear infinite}
@keyframes floor-run{to{background-position:0 52px}}

.grain{position:fixed;inset:0;z-index:-1;pointer-events:none;opacity:.035;
  background-image:radial-gradient(#fff 1px,transparent 1px);
  background-size:3px 3px}

/* ---------------------------------------------------------------- header */

header{position:sticky;top:0;z-index:20;
  background:linear-gradient(to bottom,rgba(6,8,14,.92),rgba(6,8,14,.62));
  backdrop-filter:blur(20px) saturate(160%);
  -webkit-backdrop-filter:blur(20px) saturate(160%);
  border-bottom:1px solid transparent;
  transition:border-color var(--d3) var(--e-out),box-shadow var(--d3) var(--e-out)}
header.stuck{border-bottom-color:var(--edge);
  box-shadow:0 18px 40px -32px rgba(0,0,0,.95)}
header .inner{max-width:1200px;margin:0 auto;padding:14px 22px;
  display:flex;align-items:center;gap:18px;flex-wrap:wrap}

/* The mark is a cairn: four stacked stones. It rotates in 3D on a long,
   slow loop — present enough to feel alive, slow enough to ignore. */
.mark{width:34px;height:34px;flex:none;perspective:200px}
.mark svg{width:100%;height:100%;animation:mark-spin 14s ease-in-out infinite;
  transform-style:preserve-3d;filter:drop-shadow(0 0 10px rgba(91,140,255,.55))}
@keyframes mark-spin{
  0%,100%{transform:rotateY(-16deg) rotateX(6deg)}
  50%{transform:rotateY(16deg) rotateX(-4deg)}}

/* min-width:0 on both the flex item and its text child: without it a long
   project path refuses to shrink and pushes the nav off the screen. */
.brand{display:flex;align-items:center;gap:12px;min-width:0;flex:1 1 260px}
.brand-text{min-width:0}
.brand h1{font-size:17px;letter-spacing:.16em;text-transform:uppercase;
  background:linear-gradient(100deg,var(--text) 20%,var(--a3) 50%,var(--text) 80%);
  background-size:220% 100%;-webkit-background-clip:text;background-clip:text;
  -webkit-text-fill-color:transparent;color:transparent;
  animation:sheen 7s linear infinite}
@keyframes sheen{to{background-position:-220% 0}}
.brand .sub{font-family:var(--mono);font-size:11.5px;color:var(--faint);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100%}

/* Pushes a trailing chip in a card heading to the far edge. */
.h3-tail{margin-left:auto;margin-right:0}

.live{margin-left:auto;display:flex;align-items:center;gap:7px;
  font-family:var(--mono);font-size:11px;color:var(--faint);
  text-transform:uppercase;letter-spacing:.08em}
.live .dot{width:7px;height:7px;border-radius:50%;background:var(--good);
  box-shadow:0 0 0 0 rgba(61,220,151,.6);animation:pulse 2.4s var(--e-out) infinite}
.live.off .dot{background:var(--bad);animation:none}
@keyframes pulse{
  0%{box-shadow:0 0 0 0 rgba(61,220,151,.55)}
  70%{box-shadow:0 0 0 9px rgba(61,220,151,0)}
  100%{box-shadow:0 0 0 0 rgba(61,220,151,0)}}

/* -------------------------------------------------------------------- nav
   The active pill is one element that slides and resizes between tabs
   instead of six that fade. The movement is the thing that tells you where
   you went. */
nav{position:relative;display:flex;gap:2px;width:100%;
  padding:4px;border-radius:99px;
  background:var(--glass-3);border:1px solid var(--edge);
  backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);
  overflow-x:auto;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
.nav-pill{position:absolute;top:4px;bottom:4px;left:0;border-radius:99px;
  background:linear-gradient(135deg,rgba(91,140,255,.9),rgba(162,115,255,.85));
  box-shadow:0 6px 18px -6px rgba(91,140,255,.85),0 1px 0 rgba(255,255,255,.22) inset;
  transition:transform var(--d3) var(--e-spring),width var(--d3) var(--e-spring);
  will-change:transform,width;pointer-events:none}
nav button{position:relative;z-index:1;display:flex;align-items:center;gap:8px;
  background:none;border:0;color:var(--muted);cursor:pointer;
  font:inherit;font-size:13.5px;font-weight:500;white-space:nowrap;
  padding:9px 16px;border-radius:99px;min-height:40px;
  transition:color var(--d2) var(--e-out),transform var(--d1) var(--e-out)}
nav button:hover{color:var(--text)}
nav button:active{transform:scale(.96)}
nav button[aria-selected="true"]{color:#fff}
nav button[aria-selected="true"] .i{transform:scale(1.06)}
nav button .i{transition:transform var(--d2) var(--e-spring)}

/* ------------------------------------------------------------------- main
   The perspective lives on the container so cards can tilt against a shared
   vanishing point rather than each inventing their own. */
main{max-width:1200px;margin:0 auto;padding:26px 22px 96px;perspective:1500px}

section[data-panel]{animation:panel-in var(--d3) var(--e-out) both}
@keyframes panel-in{from{opacity:0;transform:translate3d(0,14px,0)}to{opacity:1;transform:none}}
.hide{display:none}

.grid{display:grid;gap:16px;margin-bottom:20px;
  grid-template-columns:repeat(auto-fit,minmax(230px,1fr))}
.grid.hero{grid-template-columns:minmax(320px,1.05fr) minmax(260px,.95fr) minmax(220px,.8fr)}
.grid.two{grid-template-columns:repeat(auto-fit,minmax(310px,1fr))}
@media (max-width:1080px){.grid.hero{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}}

/* ------------------------------------------------------------------ cards
   Glass, with a specular highlight that tracks the pointer and a small 3D
   tilt. The highlight is what sells the material; the tilt is what sells
   the depth. Both are driven by two custom properties set in client.js. */
.card{position:relative;padding:20px 22px;border-radius:var(--r-m);
  background:var(--glass);border:1px solid var(--edge);
  backdrop-filter:blur(18px) saturate(155%);
  -webkit-backdrop-filter:blur(18px) saturate(155%);
  box-shadow:0 1px 0 rgba(255,255,255,.055) inset,0 22px 44px -30px rgba(0,0,0,.95);
  transform-style:preserve-3d;
  transition:border-color var(--d2) var(--e-out),box-shadow var(--d2) var(--e-out),
    transform var(--d2) var(--e-out)}
.card::before{content:"";position:absolute;inset:0;border-radius:inherit;
  pointer-events:none;opacity:0;transition:opacity var(--d2) var(--e-out);
  background:radial-gradient(340px circle at var(--mx,50%) var(--my,0%),
    rgba(150,185,255,.15),transparent 62%)}
.card:hover::before{opacity:1}
.card:hover{border-color:var(--edge-hi);
  box-shadow:0 1px 0 rgba(255,255,255,.09) inset,0 30px 60px -34px rgba(0,0,0,1),
    0 0 0 1px rgba(120,160,255,.06)}
.card > h3{display:flex;align-items:center;gap:8px;margin-bottom:14px;
  font-size:11.5px;font-weight:600;color:var(--muted);
  text-transform:uppercase;letter-spacing:.1em}
.card > h3 .i{color:var(--a3);opacity:.9}

/* Entrance. A short rise with a little rotation on X, staggered by index so
   the grid assembles instead of appearing. */
@keyframes rise{
  from{opacity:0;transform:translate3d(0,26px,0) rotateX(-10deg) scale(.97)}
  to{opacity:1;transform:none}}
.rise{animation:rise var(--d3) var(--e-out) both;
  animation-delay:calc(var(--i,0) * 55ms)}

/* ----------------------------------------------------------- score dial */
/* Grid cells stretch to the tallest card in the row, so a short card would
   otherwise sit against its top edge with a void underneath. Making the card
   a column and letting its body grow centres the content in whatever height
   the row ends up being. */
.grid.hero > .card,.grid.two > .card{display:flex;flex-direction:column}
.grid.hero > .card > .score,
.grid.hero > .card > .chart-row{flex:1;align-content:center}

.score{display:flex;align-items:center;gap:20px}
.dial{position:relative;width:124px;height:124px;flex:none}
.dial svg{width:100%;height:100%;transform:rotate(-90deg)}
.dial .track{fill:none;stroke:rgba(255,255,255,.07);stroke-width:9}
.dial .arc{fill:none;stroke:url(#dialGrad);stroke-width:9;stroke-linecap:round;
  stroke-dasharray:326.7;stroke-dashoffset:326.7;
  transition:stroke-dashoffset 1.1s var(--e-out);
  filter:drop-shadow(0 0 7px rgba(91,140,255,.6))}
.dial .val{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;line-height:1}
.dial .val b{font-size:36px;font-weight:650;font-variant-numeric:tabular-nums;
  letter-spacing:-.02em}
.dial .val span{font-size:10.5px;color:var(--faint);font-family:var(--mono);
  text-transform:uppercase;letter-spacing:.12em;margin-top:5px}
.score .legend{min-width:0}
.score .legend p{margin:0 0 10px;color:var(--muted);font-size:13.5px}

.metric{font-size:34px;font-weight:650;line-height:1.05;letter-spacing:-.02em;
  font-variant-numeric:tabular-nums}
.metric small{font-size:13px;color:var(--faint);font-weight:400;margin-left:2px}
.good{color:var(--good)} .warn{color:var(--warn)} .bad{color:var(--bad)}
.muted{color:var(--muted)} .faint{color:var(--faint)}
.mono{font-family:var(--mono);font-size:12.5px}

/* ------------------------------------------------------------------ pills
   Severity is never colour alone: every pill carries an icon and a word. */
.pill{display:inline-flex;align-items:center;gap:5px;vertical-align:middle;
  padding:3px 10px;margin:2px 5px 2px 0;border-radius:99px;
  font-family:var(--mono);font-size:11px;letter-spacing:.02em;
  border:1px solid var(--edge);color:var(--muted);background:var(--glass-2);
  transition:transform var(--d1) var(--e-spring),border-color var(--d2) var(--e-out)}
.pill:hover{transform:translateY(-1px)}
.pill.high{color:#ffb3bd;border-color:rgba(255,107,129,.4);background:rgba(255,107,129,.11)}
.pill.medium{color:#ffdb96;border-color:rgba(255,194,71,.38);background:rgba(255,194,71,.1)}
.pill.low{color:var(--muted)}
.pill.up{color:#8ff0c4;border-color:rgba(61,220,151,.4);background:rgba(61,220,151,.11)}
.pill.down{color:#ffb3bd;border-color:rgba(255,107,129,.4);background:rgba(255,107,129,.11)}
.pill.tech{color:#bcd0f5;border-color:rgba(120,160,255,.3);background:rgba(120,160,255,.1)}

/* --------------------------------------------------------------- findings
   Each finding is its own small surface. The severity rail on the left is a
   3D-rotated slab so the list reads as physical rows, not table lines. */
.finding{position:relative;display:flex;gap:14px;padding:15px 16px;
  border-radius:var(--r-s);background:rgba(255,255,255,.018);
  border:1px solid transparent;margin-bottom:8px;
  transform-style:preserve-3d;
  transition:background var(--d2) var(--e-out),border-color var(--d2) var(--e-out),
    transform var(--d2) var(--e-out);
  animation:slide-in var(--d3) var(--e-out) both;
  animation-delay:calc(var(--i,0) * 32ms)}
@keyframes slide-in{
  from{opacity:0;transform:translate3d(-14px,0,0) rotateY(6deg)}
  to{opacity:1;transform:none}}
.finding:hover{background:rgba(255,255,255,.045);border-color:var(--edge);
  transform:translateX(4px)}
.finding .rail{width:38px;flex:none;display:flex;align-items:flex-start;
  justify-content:center;padding-top:1px}
.finding .rail .i{width:20px;height:20px;
  transition:transform var(--d3) var(--e-spring)}
.finding:hover .rail .i{transform:rotateY(180deg)}
.finding.high .rail{color:var(--bad)}
.finding.medium .rail{color:var(--warn)}
.finding.low .rail{color:var(--faint)}
.finding .body{flex:1;min-width:0}
.finding .title{font-weight:600;font-size:15px;display:flex;align-items:center;
  gap:9px;flex-wrap:wrap}
.finding .sev{font-family:var(--mono);font-size:10px;text-transform:uppercase;
  letter-spacing:.1em;padding:2px 7px;border-radius:5px;font-weight:600}
.finding.high .sev{color:#ffb3bd;background:rgba(255,107,129,.14)}
.finding.medium .sev{color:#ffdb96;background:rgba(255,194,71,.13)}
.finding.low .sev{color:var(--muted);background:rgba(255,255,255,.05)}
.finding .detail{color:var(--muted);font-size:13.5px;margin-top:5px}
.finding .fix{margin-top:8px;font-size:13.5px;display:flex;gap:7px;
  align-items:flex-start}
.finding .fix .i{color:var(--good);margin-top:3px}
.finding .where{font-family:var(--mono);font-size:11px;color:var(--faint);
  margin-top:8px}

/* ----------------------------------------------------------------- charts
   Every chart animates in from nothing, staggered by index, and every one of
   them stops moving when the system asks for reduced motion. */

.chart-empty{display:flex;align-items:center;justify-content:center;
  color:var(--faint);font-size:13px;padding:22px 0}

.chart-row{display:flex;gap:22px;align-items:center;flex-wrap:wrap}
.chart-row > .donut{flex:none}
.chart-row > .legend-list{flex:1;min-width:150px}

.donut{position:relative;flex:none}
.donut svg{width:100%;height:100%;display:block}
.donut-seg{animation:donut-draw 900ms var(--e-out) both;
  animation-delay:calc(var(--i,0) * 110ms);transform-box:fill-box}
@keyframes donut-draw{
  from{stroke-dasharray:0 var(--c)}
  to{stroke-dasharray:var(--len) calc(var(--c) - var(--len))}}
.donut-centre{position:absolute;inset:0;display:flex;flex-direction:column;
  align-items:center;justify-content:center;line-height:1.15;text-align:center}
.donut-centre b{font-size:26px;font-weight:650;font-variant-numeric:tabular-nums}
.donut-centre span{font-size:10px;color:var(--faint);font-family:var(--mono);
  text-transform:uppercase;letter-spacing:.11em;margin-top:3px}

.legend-list{list-style:none;margin:0;padding:0;display:flex;
  flex-direction:column;gap:9px}
.legend-list li{display:flex;align-items:center;gap:10px;font-size:13.5px}
.swatch{width:10px;height:10px;border-radius:3px;flex:none;
  box-shadow:0 0 8px -1px currentColor}
.lg-label{color:var(--muted);text-transform:capitalize}
.lg-val{margin-left:auto;font-variant-numeric:tabular-nums;font-weight:600}

.barsh{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:11px}
.barsh li{display:grid;grid-template-columns:minmax(84px,auto) 1fr auto;
  gap:12px;align-items:center;font-size:13.5px}
.bh-label{display:flex;align-items:center;gap:7px;color:var(--muted);
  text-transform:capitalize;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
/* Labels that are already literal — file extensions, service names — must not
   be title-cased into ".Json". */
.barsh.raw .bh-label{text-transform:none}
.bh-label .i{color:var(--a3);opacity:.85}
.bh-track{height:9px;border-radius:99px;background:rgba(255,255,255,.06);
  overflow:hidden}
.bh-fill{display:block;height:100%;width:var(--w);border-radius:99px;
  transform-origin:left center;
  animation:bar-grow 720ms var(--e-out) both;
  animation-delay:calc(var(--i,0) * 55ms)}
@keyframes bar-grow{from{transform:scaleX(0)}to{transform:scaleX(1)}}
.bh-val{font-variant-numeric:tabular-nums;font-weight:600;
  font-family:var(--mono);font-size:12.5px}

.area{width:100%;height:130px;display:block;overflow:visible}
.area-line{stroke-dasharray:1400;stroke-dashoffset:1400;
  animation:draw 1.4s var(--e-out) forwards;
  filter:drop-shadow(0 0 6px rgba(63,210,255,.5))}
@keyframes draw{to{stroke-dashoffset:0}}
.area-fill{opacity:0;animation:fade-in 900ms var(--e-out) 420ms forwards}
@keyframes fade-in{to{opacity:1}}

.spark{display:flex;align-items:flex-end;gap:3px;width:100%}
.spark span{position:relative;flex:1;min-width:2px;border-radius:3px 3px 0 0;
  height:var(--h);background:linear-gradient(to top,var(--a1),var(--a2));
  transform-origin:bottom;animation:bar-rise 620ms var(--e-out) both;
  animation-delay:calc(var(--i,0) * 18ms)}
@keyframes bar-rise{from{transform:scaleY(0)}to{transform:scaleY(1)}}
.spark span i{position:absolute;left:50%;bottom:100%;transform:translateX(-50%);
  font-style:normal;font-family:var(--mono);font-size:10px;color:var(--muted);
  opacity:0;transition:opacity var(--d1) var(--e-out);pointer-events:none}
.spark span:hover i{opacity:1}
.spark span:hover{filter:brightness(1.3)}

/* Tech tiles. The glyphs are representative, not trademarks — the label is
   what identifies each one. */
.tech-grid{list-style:none;margin:0;padding:0;display:grid;gap:10px;
  grid-template-columns:repeat(auto-fill,minmax(96px,1fr))}
.tech-grid li{display:flex;flex-direction:column;align-items:center;gap:9px;
  padding:15px 8px;border-radius:var(--r-s);text-align:center;
  background:rgba(255,255,255,.03);border:1px solid var(--edge);
  transform-style:preserve-3d;
  transition:transform var(--d2) var(--e-spring),border-color var(--d2) var(--e-out),
    background var(--d2) var(--e-out);
  animation:rise var(--d3) var(--e-out) both;
  animation-delay:calc(var(--i,0) * 45ms)}
.tech-grid li:hover{transform:translateY(-4px) rotateX(8deg);
  border-color:var(--edge-hi);background:rgba(255,255,255,.06)}
.tech-ico{display:grid;place-items:center;width:42px;height:42px;border-radius:12px;
  background:linear-gradient(140deg,rgba(91,140,255,.22),rgba(162,115,255,.14));
  border:1px solid rgba(150,182,255,.2);color:var(--a3)}
.tech-ico .i{width:22px;height:22px}
.tech-name{font-size:11.5px;color:var(--muted);font-family:var(--mono);
  word-break:break-word;line-height:1.3}

.chart-note{margin-top:12px;font-size:11.5px;color:var(--faint);
  display:flex;align-items:flex-start;gap:7px}
.chart-note .i{flex:none;margin-top:2px;width:13px;height:13px}

/* ------------------------------------------------------------------ table
   Tables get their own scroll container. A wide table should scroll inside
   the card, never push the page sideways. */
.t-wrap{overflow-x:auto;margin:0 -6px;padding:0 6px}
table{width:100%;min-width:420px;border-collapse:separate;border-spacing:0;font-size:14px}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid rgba(255,255,255,.05)}
th{color:var(--faint);font-size:10.5px;font-weight:600;text-transform:uppercase;
  letter-spacing:.1em}
tbody tr{transition:background var(--d1) var(--e-out)}
tbody tr:hover{background:rgba(255,255,255,.03)}
tbody tr:last-child td{border-bottom:none}
td.key{font-family:var(--mono);font-size:12px;color:var(--muted);width:36%}
td .i{vertical-align:-3px;margin-right:7px;color:var(--a3);opacity:.8}

/* ------------------------------------------------------------- form bits */
input,select,textarea{width:100%;padding:9px 12px;border-radius:9px;
  font:inherit;font-size:14px;color:var(--text);
  background:rgba(10,14,24,.7);border:1px solid var(--edge);
  transition:border-color var(--d2) var(--e-out),box-shadow var(--d2) var(--e-out)}
input:hover,select:hover{border-color:rgba(150,182,255,.24)}
input:focus,textarea:focus,select:focus{outline:none;border-color:var(--a1);
  box-shadow:0 0 0 3px rgba(91,140,255,.16)}
select{cursor:pointer}
input[type=checkbox]{width:auto;accent-color:var(--a1);cursor:pointer}

button.act{display:inline-flex;align-items:center;gap:8px;white-space:nowrap;
  padding:10px 20px;min-height:42px;border-radius:11px;cursor:pointer;
  font:inherit;font-size:14px;font-weight:600;color:#fff;border:0;
  background:linear-gradient(135deg,var(--a1),var(--a2));
  box-shadow:0 10px 26px -12px rgba(91,140,255,.9),0 1px 0 rgba(255,255,255,.2) inset;
  transition:transform var(--d1) var(--e-spring),box-shadow var(--d2) var(--e-out),
    filter var(--d2) var(--e-out)}
button.act:hover{transform:translateY(-2px);filter:brightness(1.08);
  box-shadow:0 16px 34px -12px rgba(91,140,255,1),0 1px 0 rgba(255,255,255,.28) inset}
button.act:active{transform:translateY(0) scale(.97)}
button.act[disabled]{opacity:.45;cursor:not-allowed;transform:none;filter:none}

button.ghost{display:inline-flex;align-items:center;gap:7px;white-space:nowrap;
  padding:8px 15px;min-height:38px;border-radius:9px;cursor:pointer;
  font:inherit;font-size:13px;color:var(--text);
  background:var(--glass-2);border:1px solid var(--edge);
  transition:transform var(--d1) var(--e-spring),border-color var(--d2) var(--e-out),
    background var(--d2) var(--e-out)}
button.ghost:hover{border-color:var(--edge-hi);background:rgba(255,255,255,.06);
  transform:translateY(-1px)}
button.ghost:active{transform:scale(.96)}

.toolbar{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.toolbar select{width:auto;min-width:170px}
.row{display:flex;gap:12px;align-items:center}

/* -------------------------------------------------------------- terminal
   Log lines flash once on arrival. It is the cheapest possible answer to
   "did anything just happen while I was reading?" */
#logs{height:62vh;overflow-y:auto;padding:12px 6px;border-radius:var(--r-m);
  background:linear-gradient(180deg,rgba(4,6,12,.9),rgba(6,9,17,.82));
  border:1px solid var(--edge);
  font-family:var(--mono);font-size:12.5px;line-height:1.72;
  box-shadow:0 24px 50px -34px rgba(0,0,0,1) ,0 1px 0 rgba(255,255,255,.04) inset}
.logline{padding:1px 10px 1px 12px;white-space:pre-wrap;word-break:break-word;
  border-left:2px solid transparent;color:#c3d0e6;
  animation:log-in 320ms var(--e-out) both}
@keyframes log-in{from{opacity:0;transform:translateX(-8px);
  background:rgba(120,160,255,.14)}to{opacity:1;transform:none;background:transparent}}
.logline.error,.logline.fatal{color:#ffa6b2;border-left-color:var(--bad);
  background:rgba(255,107,129,.05)}
.logline.warn{color:#ffd98a;border-left-color:var(--warn);
  background:rgba(255,194,71,.04)}
.logline.debug{color:var(--faint)}
.logline .src{color:var(--faint);margin-right:10px}

/* ------------------------------------------------------------------ chat */
.chatlog{height:54vh;overflow-y:auto;padding:6px 2px 6px 0;margin-bottom:14px}
.msg{width:fit-content;max-width:82%;margin-bottom:14px;padding:12px 16px;border-radius:16px;
  white-space:pre-wrap;line-height:1.65;font-size:14.5px;
  background:var(--glass-2);border:1px solid var(--edge);
  animation:msg-in var(--d3) var(--e-spring) both}
@keyframes msg-in{from{opacity:0;transform:translate3d(0,14px,0) scale(.95)}
  to{opacity:1;transform:none}}
.msg.you{margin-left:auto;color:#fff;border:0;
  background:linear-gradient(135deg,var(--a1),var(--a2));
  box-shadow:0 10px 26px -14px rgba(91,140,255,.9)}
.msg.muted{background:transparent;border:0;color:var(--muted);padding-left:2px}
.msg code{background:rgba(255,255,255,.08);padding:1px 6px;border-radius:5px}
.dots{display:inline-flex;gap:4px;vertical-align:middle}
.dots i{width:6px;height:6px;border-radius:50%;background:var(--muted);
  animation:blink 1.3s var(--e-out) infinite}
.dots i:nth-child(2){animation-delay:.16s}
.dots i:nth-child(3){animation-delay:.32s}
@keyframes blink{0%,60%,100%{opacity:.25;transform:translateY(0)}
  30%{opacity:1;transform:translateY(-3px)}}

/* ----------------------------------------------------------------- misc */
.empty{display:flex;flex-direction:column;align-items:center;gap:10px;
  padding:44px 20px;text-align:center;color:var(--muted);font-size:14px}
.empty .i{width:30px;height:30px;color:var(--faint);opacity:.55;
  animation:float 4.5s ease-in-out infinite}
@keyframes float{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}

.bar{height:6px;border-radius:99px;overflow:hidden;margin-top:10px;
  background:rgba(255,255,255,.07)}
.bar span{display:block;height:100%;border-radius:99px;
  background:linear-gradient(90deg,var(--a1),var(--a2));
  transition:width 1s var(--e-out)}

.stack-list{display:flex;flex-wrap:wrap;gap:2px}

@media (max-width:720px){
  main{padding:20px 14px 80px}
  .card{padding:17px 16px}
  .grid.hero{grid-template-columns:1fr}
  .score{flex-direction:column;align-items:flex-start;gap:14px}
  .msg{max-width:92%}
  .brand .sub{display:none}
}

/* Motion is decoration for some people and a problem for others. When the
   system says reduce it, everything still works — it just stops moving. */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{
    animation-duration:1ms !important;animation-iteration-count:1 !important;
    transition-duration:1ms !important;scroll-behavior:auto !important}
  .aurora,.floor{display:none}
}
`;

/* ------------------------------------------------------------------ render */

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[character]);
}

function scoreClass(score) {
  return score >= 85 ? "good" : score >= 60 ? "warn" : "bad";
}

function scoreVerdict(score, total) {
  if (total === 0) return "Nothing to fix. Re-run after your next change.";
  if (score >= 85) return "Healthy. The remaining items are polish.";
  if (score >= 60) return "Workable, with real gaps worth booking time for.";
  return "Several high-severity issues. Start at the top of Findings.";
}

const MARK = `<svg viewBox="0 0 40 40" aria-hidden="true">
  <defs>
    <linearGradient id="mk" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0" stop-color="#5b8cff"/><stop offset=".55" stop-color="#a273ff"/>
      <stop offset="1" stop-color="#3fd2ff"/>
    </linearGradient>
  </defs>
  <g fill="url(#mk)">
    <rect x="8"  y="27" width="24" height="7"   rx="3.5" opacity=".95"/>
    <rect x="11" y="19" width="18" height="6.5" rx="3.2" opacity=".85"/>
    <rect x="13.5" y="12" width="13" height="5.5" rx="2.7" opacity=".72"/>
    <rect x="16" y="6"  width="8"  height="4.5" rx="2.2" opacity=".6"/>
  </g>
</svg>`;

const TABS = [
  ["overview", "Overview", "layers"],
  ["findings", "Findings", "scan"],
  ["services", "Services", "server"],
  ["logs", "Logs", "terminal"],
  ["config", "Config", "sliders"],
  ["chat", "Chat", "chat"],
];


const GROUP_ICON = {
  docker: "tech-container",
  compose: "tech-stack",
  kubernetes: "tech-helm",
  node: "tech-hex",
  python: "tech-python",
  terraform: "tech-tf",
  ci: "tech-flow",
  secrets: "shield",
  hygiene: "spark",
};

/* Findings grouped by the area of the stack they came from. Real counts from
   the report, ordered worst-first so the chart ranks rather than just lists. */
function groupRows(report) {
  const weight = { high: 3, medium: 2, low: 1 };
  const worst = {};
  for (const f of report.findings) {
    worst[f.group] = Math.max(worst[f.group] || 0, weight[f.severity] || 0);
  }
  return Object.entries(report.summary.byGroup)
    .map(([label, value]) => ({
      label,
      value,
      icon: GROUP_ICON[label] || "box",
      color:
        worst[label] === 3 ? "var(--bad)" : worst[label] === 2 ? "var(--warn)" : "var(--a1)",
    }))
    .sort((a, b) => b.value - a.value);
}

/* The largest file types by count. Past about eight it is a long tail that
   makes the chart harder to read without telling you anything. */
function extensionRows(report) {
  return Object.entries(report.files.byExtension || {})
    .map(([label, value]) => ({ label: label || "(none)", value }))
    .sort((a, b) => b.value - a.value)
    .slice(0, 5)
    .map((row, index) => ({ ...row, color: paletteAt(index) }));
}

export function render(state) {
  const { report, config } = state;
  const { bySeverity } = report.summary;
  const cls = scoreClass(report.score);

  const severitySegments = [
    { label: "high", value: bySeverity.high || 0, color: SEVERITY_COLOR.high },
    { label: "medium", value: bySeverity.medium || 0, color: SEVERITY_COLOR.medium },
    { label: "low", value: bySeverity.low || 0, color: SEVERITY_COLOR.low },
  ];
  const groups = groupRows(report);
  const extensions = extensionRows(report);

  return `<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>${escapeHtml(report.name)} — Cairn</title>
<style>${CSS}</style>
</head><body>
${sprite()}
<a class="skip" href="#main">Skip to content</a>

<div class="aurora" aria-hidden="true"><b></b><b></b><b></b></div>
<div class="floor" aria-hidden="true"></div>
<div class="grain" aria-hidden="true"></div>

<header>
  <div class="inner">
    <div class="brand">
      <div class="mark">${MARK}</div>
      <div class="brand-text">
        <h1>Cairn</h1>
        <div class="sub" title="${escapeHtml(report.root)}">${escapeHtml(
          report.name,
        )} · ${escapeHtml(report.root)}</div>
      </div>
    </div>
    <div class="live" id="live" title="Live connection to the analyser">
      <span class="dot"></span><span id="live-text">live</span>
    </div>
    <nav role="tablist" aria-label="Sections">
      <span class="nav-pill" id="nav-pill" aria-hidden="true"></span>
      ${TABS.map(
        ([id, label, ico], index) => `<button role="tab" data-tab="${id}"
        aria-selected="${index === 0}" aria-controls="panel-${id}">
        ${icon(ico)}<span>${label}</span></button>`,
      ).join("")}
    </nav>
  </div>
</header>

<main id="main">
  <section data-panel="overview" id="panel-overview" role="tabpanel">
    <div class="grid hero">
      <div class="card tilt rise" style="--i:0">
        <h3>${icon("activity", "i-sm")} Health score</h3>
        <div class="score">
          <div class="dial ${cls}" id="dial">
            <svg viewBox="0 0 120 120">
              <defs>
                <linearGradient id="dialGrad" x1="0" y1="0" x2="1" y2="1">
                  <stop offset="0" stop-color="var(--g1,#5b8cff)"/>
                  <stop offset="1" stop-color="var(--g2,#a273ff)"/>
                </linearGradient>
              </defs>
              <circle class="track" cx="60" cy="60" r="52"/>
              <circle class="arc" id="score-arc" cx="60" cy="60" r="52"/>
            </svg>
            <div class="val">
              <b class="${cls}" id="score" data-to="${report.score}">0</b>
              <span>of 100</span>
            </div>
          </div>
          <div class="legend">
            <p id="verdict">${escapeHtml(scoreVerdict(report.score, report.summary.total))}</p>
            <button class="act" id="refresh">${icon("refresh", "i-sm")} Re-analyse</button>
            <div class="mono faint" id="refresh-status" style="margin-top:8px"></div>
          </div>
        </div>
      </div>

      <div class="card tilt rise" style="--i:1">
        <h3>${icon("scan", "i-sm")} Severity mix</h3>
        <div class="chart-row">
          ${donut(severitySegments, {
            size: 122,
            centre: `<b>${report.summary.total}</b><span>findings</span>`,
          })}
          ${legend(severitySegments)}
        </div>
      </div>

      <div class="card tilt rise" style="--i:2">
        <h3>${icon("file", "i-sm")} Files scanned</h3>
        <div class="metric" data-count="${report.files.total}">0</div>
        <div class="mono faint" style="margin-top:6px">${(
          report.files.bytes / 1048576
        ).toFixed(1)} MB · ${report.durationMs} ms</div>
        <div style="margin-top:16px">${barsH(extensions, { raw: true })}</div>
      </div>
    </div>

    <div class="grid two">
      <div class="card tilt rise" style="--i:3">
        <h3>${icon("layers", "i-sm")} Findings by area</h3>
        ${barsH(groups)}
        <div class="chart-note">${icon("info", "i-sm")}
          <span>Bar colour is the worst severity in that area, not its size.</span></div>
      </div>

      <div class="card tilt rise" style="--i:4">
        <h3>${icon("box", "i-sm")} Detected stack</h3>
        ${techGrid(report.stack)}
        <div class="chart-note">${icon("info", "i-sm")}
          <span>Glyphs are representative, not vendor logos.</span></div>
      </div>
    </div>

    <div class="card tilt rise" style="--i:5">
      <h3>${icon("server", "i-sm")} Detected services</h3>
      ${
        report.services.length
          ? `<div class="t-wrap"><table>
            <thead><tr><th>Name</th><th>Kind</th><th>Port</th><th>Source</th></tr></thead>
            <tbody>${report.services
              .map(
                (s) => `<tr>
              <td>${icon("box", "i-sm")}${escapeHtml(s.name)}</td>
              <td class="mono">${escapeHtml(s.kind)}</td>
              <td class="mono">${s.port ?? "—"}</td>
              <td class="mono faint">${escapeHtml(s.source)}</td>
            </tr>`,
              )
              .join("")}</tbody></table></div>`
          : `<div class="empty">${icon("server")}
             <div>No services detected.<br>Cairn reads Dockerfiles, compose files and package.json.</div>
             </div>`
      }
      <div class="mono faint" style="margin-top:14px">Generated ${escapeHtml(
        report.generatedAt,
      )}</div>
    </div>
  </section>

  <section data-panel="findings" id="panel-findings" role="tabpanel" class="hide">
    <div class="toolbar">
      <select id="sev-filter" aria-label="Filter by severity">
        <option value="all">All severities</option>
        <option value="high">High only</option>
        <option value="medium">Medium and above</option>
      </select>
      <span class="mono faint" id="finding-count"></span>
    </div>
    <div class="card" id="findings"></div>
  </section>

  <section data-panel="services" id="panel-services" role="tabpanel" class="hide">
    <div class="toolbar">
      <button class="ghost" id="probe">${icon("activity", "i-sm")} Probe health endpoints</button>
      <span class="faint" style="font-size:13px">Configured under
        <code>services</code> in cairn.config.json</span>
    </div>
    <div id="service-charts"></div>
    <div class="card" id="service-health">
      <div class="empty">${icon("activity")}
        <div>Not probed yet. Run a probe to check each configured endpoint.</div>
      </div>
    </div>
  </section>

  <section data-panel="logs" id="panel-logs" role="tabpanel" class="hide">
    <div class="grid two">
      <div class="card">
        <h3>${icon("activity", "i-sm")} Volume</h3>
        <div id="log-volume"><div class="chart-empty" style="height:130px">Loading…</div></div>
        <div class="chart-note" id="log-volume-note"></div>
      </div>
      <div class="card">
        <h3>${icon("terminal", "i-sm")} Level mix</h3>
        <div class="chart-row" id="log-levels">
          <div class="chart-empty" style="height:122px">Loading…</div>
        </div>
      </div>
    </div>
    <div class="toolbar">
      <select id="log-filter" aria-label="Filter by log level">
        <option value="all">All levels</option>
        <option value="error">Errors</option>
        <option value="warn">Warnings</option>
      </select>
      <label class="row faint" style="font-size:13px">
        <input type="checkbox" id="follow" checked> follow
      </label>
      <span class="mono faint" id="log-stats"></span>
    </div>
    <div id="logs"><div class="empty">${icon("terminal")}
      <div>Waiting for log lines…</div></div></div>
  </section>

  <section data-panel="config" id="panel-config" role="tabpanel" class="hide">
    <div class="card">
      <h3>${icon("sliders", "i-sm")} cairn.config.json</h3>
      <div class="mono faint" style="margin-bottom:14px">${escapeHtml(config._path)}</div>
      <div class="t-wrap"><table>
        <thead><tr><th>Key</th><th>Value</th><th></th></tr></thead>
        <tbody id="config-rows"></tbody></table></div>
    </div>
  </section>

  <section data-panel="chat" id="panel-chat" role="tabpanel" class="hide">
    <div class="card">
      <h3>${icon("spark", "i-sm")} Ask about this project
        <span class="pill tech h3-tail">${escapeHtml(config.chat.mode)}</span></h3>
      <div class="chatlog" id="chatlog">
        <div class="msg muted">Ask what is wrong, what the errors mean,
          or what to fix first.</div>
      </div>
      <form class="row" id="chatform">
        <input id="q" placeholder="why are there errors in the logs?" autocomplete="off">
        <button class="act" type="submit">${icon("send", "i-sm")} Ask</button>
      </form>
    </div>
  </section>
</main>

<script>window.__CAIRN__ = ${JSON.stringify({
    report: state.report,
    chatMode: config.chat.mode,
  }).replace(/</g, "\\u003c")};</script>
<script type="module" src="/app.js"></script>
</body></html>`;
}
