// Hi-fi ASCII surf. Each frame paints a full-colour scene (storm sky, moon,
// a barreling wave, Frankenstein on a board) into an offscreen canvas at one
// pixel per character cell, then shades it as coloured ASCII: luminance picks
// a glyph from a 70-step ramp, Sobel edges become | / - \ outlines, and
// colours are quantized to a fixed palette so each colour is one fillText per row.

const RAMP = " .'`^\",:;Il!i><~+_-?][}{1)(|/tfjrxnuvczXYUJCLQ0OZmwqpdbkhao*#MW&8%B@$";
const PALETTE = [
  "#0b1118", "#17212c", "#2b3846", "#56667a", "#eef3f6", // 0-4 sky, clouds, moon
  "#0c3943", "#135a6a", "#1f8595", "#52c3ca", "#a8ecea", "#f6fffd", // 5-10 water to foam
  "#3d7a39", "#79c46b", "#b8f2a0", // 11-13 skin
  "#4a2c46", "#7d5779", // 14-15 coat
  "#ffd36b", "#fff6d2", // 16-17 bolts, lightning
  "#eadcbc", "#e2694a", "#060708", // 18 board, 19 stripe/stitch, 20 black
  "#2a2238", // 21 hair
  "#aab6c0", // 22 moon shade
];
// Glyph colours. Dark tones are lifted so they read on a dark page; "" means blank.
const DISPLAY = [
  "", "#1a2531", "#3e5164", "#7d8ea3", "#eef3f6",
  "#145463", "#1d7a8c", "#2ea2b2", "#52c3ca", "#a8ecea", "#f6fffd",
  "#55a34e", "#79c46b", "#b8f2a0",
  "#9a6694", "#c99bc3",
  "#ffd36b", "#fff6d2",
  "#eadcbc", "#e2694a", "",
  "#7a6a9e", "#aab6c0",
];
const GLOW: Record<number, [string, number]> = {
  16: ["rgba(255,211,107,.8)", 10], 17: ["rgba(255,246,210,.9)", 14],
  13: ["rgba(184,242,160,.45)", 6], 10: ["rgba(230,255,252,.35)", 6], 4: ["rgba(238,243,246,.5)", 10],
};
const NP = PALETTE.length;
const RGB = PALETTE.map((h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));
const LUT = new Uint8Array(32768);
for (let i = 0; i < 32768; i++) {
  const r = ((i >> 10) & 31) * 8.2, g = ((i >> 5) & 31) * 8.2, b = (i & 31) * 8.2;
  let best = 0, bd = 1e9;
  for (let p = 0; p < NP; p++) {
    const d = (r - RGB[p][0]) ** 2 * 0.8 + (g - RGB[p][1]) ** 2 * 1.2 + (b - RGB[p][2]) ** 2;
    if (d < bd) { bd = d; best = p; }
  }
  LUT[i] = best;
}

type P = { x: number; y: number; vx: number; vy: number; life: number; max: number; r: number; c: string };
const clamp = (v: number, a: number, b: number) => (v < a ? a : v > b ? b : v);
const rand = (a: number, b: number) => a + Math.random() * (b - a);
function bez(p0: number, p1: number, p2: number, p3: number, s: number) {
  const m = 1 - s;
  return m * m * m * p0 + 3 * m * m * s * p1 + 3 * m * s * s * p2 + s * s * s * p3;
}

type Surface = HTMLCanvasElement | OffscreenCanvas;
type DrawingContext = CanvasRenderingContext2D | OffscreenCanvasRenderingContext2D;
export type SurfRenderer = ReturnType<typeof createSurfRenderer>;

export function createSurfRenderer(canvas: Surface, off: Surface, opts: {
  width: number; height: number; dpr: number; reduceMotion: boolean;
  onStrike?: (power: number) => void;
}) {
  const ctx = canvas.getContext("2d") as DrawingContext;
  const o = off.getContext("2d", { willReadFrequently: true }) as DrawingContext;
  const reduce = opts.reduceMotion;

  let W = 0, H = 0, dpr = 1, fs = 8, cw = 5, lh = 9, cols = 0, rows = 0, narrow = false;
  let lum = new Float32Array(0), pal = new Uint8Array(0), ch = new Uint16Array(0), rowMask = new Uint32Array(0);
  let t = 0, last = performance.now(), running = true, disposed = false, animationFrame = 0;
  const frameInterval = 1000 / 24;
  let charged = 0, flash = 0, nextStrike = 4, blink = 0, shake = 0;
  const alive = true;
  let bolt: { pts: [number, number][][]; t0: number } | null = null;
  const parts: P[] = [];
  const ripples: { x: number; y: number; t0: number }[] = [];
  const clouds = Array.from({ length: 10 }, (_, i) => ({ x: i / 10 + rand(0, 0.08), y: i % 3 === 0 ? rand(0.1, 0.2) : rand(0.02, 0.32), r: rand(0.07, 0.15), s: rand(0.003, 0.008), i }));
  const rain = Array.from({ length: 140 }, () => ({ x: Math.random(), y: Math.random(), s: rand(0.5, 0.9) }));
  let head = { x: 0, y: 0 };
  let geo = { cx: 0, cy: 0, y0: 0, WW: 0, S: 0 };

  function resize(width: number, height: number, pixelRatio: number) {
    W = Math.max(1, width); H = Math.max(1, height); dpr = Math.min(pixelRatio || 1, 2);
    narrow = W < 760;
    canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
    fs = narrow ? 5.2 : clamp(W / 175, 7, 9.5);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.font = `600 ${fs}px "Geist Mono", ui-monospace, Menlo, Consolas, monospace`;
    cw = ctx.measureText("M".repeat(40)).width / 40;
    lh = fs * 1.12;
    cols = Math.ceil(W / cw) + 1; rows = Math.ceil(H / lh) + 1;
    off.width = cols; off.height = rows;
    const n = cols * rows;
    lum = new Float32Array(n); pal = new Uint8Array(n); ch = new Uint16Array(n); rowMask = new Uint32Array(rows);
    const WW = narrow ? W * 1.3 : W;
    geo = { cx: narrow ? W * 0.36 : W * (W > 1100 ? 0.6 : 0.63), cy: H * 0.1, y0: H * (narrow ? 0.76 : 0.72), WW, S: H * (narrow ? 0.44 : 0.3) };
  }

  /* ---------------- scene painting (world = CSS px) ---------------- */
  function paintSky() {
    const g = o.createLinearGradient(0, 0, 0, geo.y0);
    g.addColorStop(0, "#04070b"); g.addColorStop(1, "#172634");
    o.fillStyle = g; o.fillRect(0, 0, W, H);
    // moon
    const mx = narrow ? W * 0.9 : W * 0.925, my = H * (narrow ? 0.2 : 0.3), mr = H * (narrow ? 0.05 : 0.045);
    const mg = o.createRadialGradient(mx, my, mr * 0.6, mx, my, mr * 4);
    mg.addColorStop(0, "rgba(220,235,240,.35)"); mg.addColorStop(1, "rgba(220,235,240,0)");
    o.fillStyle = mg; o.fillRect(mx - mr * 4, my - mr * 4, mr * 8, mr * 8);
    o.fillStyle = "#eef3f6"; o.beginPath(); o.arc(mx, my, mr, 0, 7); o.fill();
    o.fillStyle = "#aab6c0";
    for (const [dx, dy, rr] of [[-0.3, -0.2, 0.22], [0.25, 0.3, 0.16], [0.1, -0.45, 0.1]]) { o.beginPath(); o.arc(mx + dx * mr, my + dy * mr, rr * mr, 0, 7); o.fill(); }
    // clouds
    for (const c of clouds) {
      const x = ((c.x + t * c.s) % 1.3) * (W * 1.3) - W * 0.15, y = c.y * H, r = c.r * H * 1.4;
      const cg = o.createRadialGradient(x, y, 0, x, y, r);
      const lit = flash > 0.15;
      cg.addColorStop(0, lit ? "rgba(110,125,145,.9)" : "rgba(62,78,96,.85)");
      cg.addColorStop(1, "rgba(30,40,52,0)");
      o.fillStyle = cg; o.beginPath(); o.ellipse(x, y, r * 1.8, r * 0.7, 0, 0, 7); o.fill();
    }
    // rain
    o.strokeStyle = "rgba(70,90,110,.55)"; o.lineWidth = 1;
    o.beginPath();
    for (const d of rain) {
      const x = ((d.x - t * 0.05 * d.s) % 1 + 1) % 1 * W, y = ((d.y + t * 0.6 * d.s) % 1) * geo.y0;
      o.moveTo(x, y); o.lineTo(x - H * 0.012, y + H * 0.035);
    }
    o.stroke();
  }

  function paintSea() {
    const { y0 } = geo;
    const g = o.createLinearGradient(0, y0, 0, H);
    g.addColorStop(0, "#1a7f8e"); g.addColorStop(0.25, "#0f4c5a"); g.addColorStop(1, "#071d24");
    o.fillStyle = g; o.fillRect(0, y0, W, H - y0);
    // moving swell bands
    o.lineWidth = Math.max(1, H * 0.004);
    for (let i = 0; i < 14; i++) {
      const yy = y0 + (i / 14) ** 1.6 * (H - y0);
      o.strokeStyle = `rgba(120,215,220,${0.32 - i * 0.018})`;
      o.beginPath();
      for (let x = 0; x <= W; x += 12) {
        const v = yy + Math.sin(x * 0.012 + t * 1.6 + i) * (2 + i * 0.6);
        x ? o.lineTo(x, v) : o.moveTo(x, v);
      }
      o.stroke();
    }
    // ripples from the cursor
    for (const r of ripples) {
      const age = t - r.t0, rad = age * H * 0.22;
      o.strokeStyle = `rgba(220,255,250,${Math.max(0, 0.8 - age * 0.5)})`;
      o.lineWidth = Math.max(1, H * 0.004);
      o.beginPath(); o.ellipse(r.x, r.y, rad * 1.8, rad * 0.45, 0, 0, 7); o.stroke();
    }
  }

  function wavePath() {
    const { cx, cy, y0, WW } = geo;
    const sway = Math.sin(t * 0.5) * WW * 0.006;
    const p = new Path2D();
    p.moveTo(-20, y0);
    p.bezierCurveTo(WW * 0.18, y0, cx - WW * 0.2, cy + H * 0.14, cx + sway, cy);
    p.bezierCurveTo(cx + WW * 0.14, cy - H * 0.035, cx + WW * 0.245 + sway, cy + H * 0.11, cx + WW * 0.205 + sway, cy + H * 0.29); // outer lip to tip
    p.bezierCurveTo(cx + WW * 0.18, cy + H * 0.19, cx + WW * 0.1, cy + H * 0.09, cx + WW * 0.04, cy + H * 0.13); // inner lip back to throat
    p.bezierCurveTo(cx + WW * 0.025, cy + H * 0.33, cx + WW * 0.12, y0 - H * 0.04, cx + WW * 0.32, y0); // face to trough
    p.lineTo(W + 20, y0); p.lineTo(W + 20, H); p.lineTo(-20, H); p.closePath();
    return { p, sway };
  }
  const faceAt = (s: number) => {
    const { cx, cy, y0, WW } = geo;
    return {
      x: bez(cx + WW * 0.04, cx + WW * 0.025, cx + WW * 0.12, cx + WW * 0.32, s),
      y: bez(cy + H * 0.13, cy + H * 0.33, y0 - H * 0.04, y0, s),
    };
  };

  function paintWave() {
    const { cx, cy, y0, WW } = geo;
    // the hollow of the tube, behind everything
    const hx = cx + WW * 0.12, hy = cy + H * 0.21;
    const hg = o.createRadialGradient(hx, hy, 0, hx, hy, WW * 0.13);
    hg.addColorStop(0, "#082a33"); hg.addColorStop(0.7, "#0f4a57"); hg.addColorStop(1, "#1a7584");
    o.fillStyle = hg; o.beginPath(); o.ellipse(hx, hy, WW * 0.12, H * 0.17, 0, 0, 7); o.fill();
    // spiral swirl inside the tube
    o.strokeStyle = "rgba(90,190,200,.35)"; o.lineWidth = Math.max(1, H * 0.006);
    for (let k = 0; k < 3; k++) {
      o.beginPath();
      for (let a = 0; a < 5.5; a += 0.12) {
        const rr = (0.25 + a * 0.12) * WW * 0.05 * (1 + k * 0.35), ang = a + t * 2.2 + k * 2;
        const x = hx + Math.cos(ang) * rr * 1.2, y = hy + Math.sin(ang) * rr * 1.4;
        a ? o.lineTo(x, y) : o.moveTo(x, y);
      }
      o.stroke();
    }

    const { p } = wavePath();
    const g = o.createLinearGradient(0, cy - H * 0.04, 0, y0 + H * 0.05);
    g.addColorStop(0, "#6fd0d4"); g.addColorStop(0.2, "#2b98a6"); g.addColorStop(0.55, "#125f6d"); g.addColorStop(1, "#0b3540");
    o.fillStyle = g; o.fill(p);

    o.save(); o.clip(p);
    // water racing up the face
    o.setLineDash([H * 0.05, H * 0.035]);
    o.lineDashOffset = t * H * 0.6;
    o.lineWidth = Math.max(1, H * 0.005);
    for (let i = 0; i < 26; i++) {
      const x = cx - WW * 0.06 + i * WW * 0.011;
      o.strokeStyle = `rgba(200,250,248,${0.12 + 0.12 * Math.sin(i * 1.7)})`;
      o.beginPath(); o.moveTo(x + WW * 0.03, y0 + 4); o.quadraticCurveTo(x, cy + H * 0.3, x + WW * 0.02, cy - 10); o.stroke();
    }
    o.setLineDash([]);
    // keep the back of the swell in shadow so the surfer is the focal point
    const bk = o.createLinearGradient(0, 0, cx, 0);
    bk.addColorStop(0, "rgba(5,16,22,.75)"); bk.addColorStop(1, "rgba(5,16,22,0)");
    o.fillStyle = bk; o.fillRect(0, 0, cx, H);
    // back of the swell: horizontal shimmer
    for (let i = 0; i < 9; i++) {
      const yy = cy + H * 0.08 + i * H * 0.065;
      o.strokeStyle = `rgba(170,240,240,${0.18 - i * 0.015})`;
      o.beginPath();
      for (let x = -10; x < cx; x += 10) { const v = yy + Math.sin(x * 0.02 - t * 1.4 + i) * 3; x > -10 ? o.lineTo(x, v) : o.moveTo(x, v); }
      o.stroke();
    }
    // foam band at the base of the face
    const wx = cx + WW * 0.24, wy = y0 - H * 0.01;
    const fg = o.createRadialGradient(wx, wy, 0, wx, wy, WW * 0.12);
    fg.addColorStop(0, "rgba(240,255,252,.75)"); fg.addColorStop(1, "rgba(240,255,252,0)");
    o.fillStyle = fg; o.beginPath(); o.ellipse(wx, wy, WW * 0.12, H * 0.035, 0, 0, 7); o.fill();
    o.restore();

    // white water along the lip
    const sway = Math.sin(t * 0.5) * WW * 0.006;
    o.strokeStyle = "#f6fffd"; o.lineCap = "round";
    o.lineWidth = H * 0.016;
    o.beginPath(); o.moveTo(cx - WW * 0.03 + sway, cy + H * 0.012);
    o.bezierCurveTo(cx + WW * 0.14, cy - H * 0.035, cx + WW * 0.245 + sway, cy + H * 0.11, cx + WW * 0.205 + sway, cy + H * 0.29);
    o.stroke();
    o.lineWidth = H * 0.007; o.strokeStyle = "#bff3ef";
    o.beginPath(); o.moveTo(cx + WW * 0.04, cy + H * 0.13);
    o.bezierCurveTo(cx + WW * 0.1, cy + H * 0.09, cx + WW * 0.18, cy + H * 0.19, cx + WW * 0.205 + sway, cy + H * 0.29);
    o.stroke();
  }

  /* ---------------- the monster ---------------- */
  function part(k: number, fn: () => void, from: [number, number, number]) {
    void k; void from;
    o.save(); fn(); o.restore();
  }

  function paintMonster() {
    const s = 0.64 + 0.05 * Math.sin(t * 0.9);
    const ft = faceAt(s), f2 = faceAt(s + 0.02);
    const slope = Math.atan2(f2.y - ft.y, f2.x - ft.x);
    const tilt = clamp(slope * 0.35, -0.4, 0.45) - 0.12;
    const u = geo.S / 100;
    const crouch = Math.sin(t * 1.8) * 3 + 4;
    const lean = -tilt * 0.6;
    o.save();
    o.translate(ft.x + u * 6, ft.y - u * 2);
    o.rotate(tilt);
    o.lineCap = "round"; o.lineJoin = "round";
    const far = geo.S * 2.2;

    // board
    part(0, () => {
      o.fillStyle = "#eadcbc";
      o.beginPath(); o.ellipse(0, 4 * u, 40 * u, 5 * u, 0, 0, 7); o.fill();
      o.beginPath(); o.ellipse(36 * u, 2.5 * u, 8 * u, 3.5 * u, -0.35, 0, 7); o.fill();
      o.strokeStyle = "#e2694a"; o.lineWidth = 1.6 * u;
      o.beginPath(); o.moveTo(-34 * u, 4 * u); o.lineTo(38 * u, 3 * u); o.stroke();
      o.fillStyle = "#060708"; o.fillRect(-40 * u, 3 * u, 3 * u, 6 * u);
    }, [far * 0.9, far * 0.15, 0.8]);

    o.rotate(lean);
    const hipY = -40 * u + crouch * u;
    // legs
    part(1, () => {
      o.strokeStyle = "#4a2c46"; o.lineWidth = 9 * u;
      o.beginPath(); o.moveTo(19 * u, 0); o.lineTo(17 * u, -21 * u + crouch * u * 0.5); o.lineTo(5 * u, hipY); o.stroke();
      o.beginPath(); o.moveTo(-17 * u, 0); o.lineTo(-19 * u, -19 * u + crouch * u * 0.5); o.lineTo(-5 * u, hipY); o.stroke();
      o.fillStyle = "#060708";
      o.fillRect(13 * u, -4 * u, 12 * u, 5 * u); o.fillRect(-23 * u, -4 * u, 12 * u, 5 * u);
    }, [-far, far * 0.3, -1.2]);

    // torso: a tattered coat
    part(2, () => {
      o.fillStyle = "#4a2c46";
      o.beginPath();
      o.moveTo(-13 * u, hipY + 6 * u); o.lineTo(-15 * u, hipY - 34 * u); o.lineTo(15 * u, hipY - 34 * u); o.lineTo(13 * u, hipY + 6 * u);
      for (let i = 0; i <= 6; i++) o.lineTo((13 - i * 4.33) * u, hipY + (i % 2 ? 10 : 5) * u);
      o.closePath(); o.fill();
      o.strokeStyle = "#7d5779"; o.lineWidth = 2 * u;
      o.beginPath(); o.moveTo(0, hipY - 33 * u); o.lineTo(-3 * u, hipY + 4 * u); o.stroke();
      o.beginPath(); o.moveTo(-15 * u, hipY - 34 * u); o.lineTo(-6 * u, hipY - 22 * u); o.moveTo(15 * u, hipY - 34 * u); o.lineTo(6 * u, hipY - 22 * u); o.stroke();
      o.strokeStyle = "#e2694a"; o.lineWidth = 1 * u;
      o.beginPath(); o.moveTo(8 * u, hipY - 14 * u); o.lineTo(12 * u, hipY - 6 * u); o.stroke();
    }, [far * 0.2, -far, 0.9]);

    // arms out for balance
    const sh = hipY - 31 * u;
    const swing = Math.sin(t * 1.3) * 0.25;
    part(3, () => {
      o.strokeStyle = "#4a2c46"; o.lineWidth = 8 * u;
      const arm = (side: number, a1: number, a2: number) => {
        const ex = side * 15 * u + Math.cos(a1) * 18 * u * side, ey = sh + Math.sin(a1) * 18 * u;
        const hx = ex + Math.cos(a2) * 17 * u * side, hy = ey + Math.sin(a2) * 17 * u;
        o.beginPath(); o.moveTo(side * 13 * u, sh); o.lineTo(ex, ey); o.lineTo(hx, hy); o.stroke();
        o.fillStyle = "#79c46b"; o.beginPath(); o.arc(hx, hy, 4.6 * u, 0, 7); o.fill();
      };
      arm(1, -0.25 + swing, 0.15 + swing);
      arm(-1, -0.45 - swing, -0.1 - swing);
    }, [-far * 0.7, -far * 0.6, 1.6]);

    // head, neck, bolts
    part(4, () => {
      const nb = sh - 2 * u;
      o.fillStyle = "#3d7a39"; o.fillRect(-5 * u, nb - 8 * u, 10 * u, 9 * u);
      // neck bolts
      const hot = alive;
      if (alive && charged > 0) {
        for (const sx of [-1, 1]) {
          const bg = o.createRadialGradient(sx * 11 * u, nb - 4 * u, 0, sx * 11 * u, nb - 4 * u, 16 * u);
          bg.addColorStop(0, "rgba(255,240,180,.8)"); bg.addColorStop(1, "rgba(255,211,107,0)");
          o.fillStyle = bg; o.fillRect(sx * 11 * u - 16 * u, nb - 20 * u, 32 * u, 32 * u);
        }
      }
      o.fillStyle = alive ? (hot ? "#ffd36b" : "#fff6d2") : "#56667a";
      o.fillRect(-14 * u, nb - 6.5 * u, 6 * u, 5 * u); o.fillRect(8 * u, nb - 6.5 * u, 6 * u, 5 * u);
      o.fillRect(-16 * u, nb - 7.5 * u, 2.5 * u, 7 * u); o.fillRect(13.5 * u, nb - 7.5 * u, 2.5 * u, 7 * u);
      // head block
      const top = nb - 34 * u;
      o.fillStyle = "#79c46b"; o.fillRect(-12.5 * u, top, 25 * u, 27 * u);
      o.fillStyle = "#3d7a39"; o.fillRect(7.5 * u, top, 5 * u, 27 * u); o.fillRect(-12.5 * u, nb - 10 * u, 25 * u, 3 * u);
      o.fillStyle = "#b8f2a0"; o.fillRect(-12.5 * u, top + 6 * u, 3 * u, 15 * u);
      // flat-top hair with a jagged fringe
      o.fillStyle = "#2a2238";
      o.beginPath(); o.moveTo(-13.5 * u, top - 3 * u); o.lineTo(13.5 * u, top - 3 * u); o.lineTo(13.5 * u, top + 4 * u);
      for (let i = 0; i <= 8; i++) o.lineTo((13.5 - i * 3.375) * u, top + (i % 2 ? 8 : 4) * u);
      o.closePath(); o.fill();
      // forehead stitches
      o.strokeStyle = "#060708"; o.lineWidth = 1.2 * u;
      o.beginPath(); o.moveTo(-10 * u, top + 11 * u); o.lineTo(9 * u, top + 11 * u);
      for (let i = -8; i <= 8; i += 4) { o.moveTo(i * u, top + 9 * u); o.lineTo(i * u, top + 13 * u); }
      o.stroke();
      // heavy brow + eyes
      o.fillStyle = "#1e3d1c"; o.fillRect(-11 * u, top + 14 * u, 22 * u, 3 * u);
      const blinkNow = blink > 0;
      for (const ex of [-5.5, 5.5]) {
        o.fillStyle = "#060708"; o.fillRect((ex - 3.5) * u, top + 17 * u, 7 * u, 4 * u);
        if (alive && !blinkNow) {
          o.fillStyle = charged > 0 ? "#fff6d2" : "#d6ffc0";
          o.fillRect((ex - 1.5) * u, top + 18 * u, 3 * u, 2.4 * u);
        }
      }
      // mouth with a stitched scar
      o.strokeStyle = "#060708"; o.lineWidth = 1.4 * u;
      o.beginPath(); o.moveTo(-6 * u, top + 24 * u); o.lineTo(6 * u, top + 24 * u); o.stroke();
      o.strokeStyle = "#e2694a"; o.lineWidth = 0.9 * u;
      o.beginPath(); o.moveTo(5 * u, top + 6 * u); o.lineTo(9 * u, top + 15 * u);
      for (let i = 0; i < 3; i++) { o.moveTo(5.5 * u + i * 1.4 * u, top + 8 * u + i * 3 * u); o.lineTo(8.5 * u + i * 1.4 * u, top + 7 * u + i * 3 * u); }
      o.stroke();
      // remember where the bolts are in world space for the next strike
      const m = o.getTransform();
      const sx = W / cols, sy = H / rows;
      head = { x: (m.a * 0 + m.c * (nb - 4 * u) + m.e) * sx, y: (m.b * 0 + m.d * (nb - 4 * u) + m.f) * sy };
    }, [-far * 1.1, -far * 1.2, -2.2]);
    o.restore();

    // spray off the tail
    {
      const tail = faceAt(Math.max(0, s - 0.06));
      const n = charged > 0 ? 3 : 2;
      for (let i = 0; i < n; i++) parts.push({ x: tail.x - geo.S * 0.1, y: tail.y, vx: rand(-H * 0.5, -H * 0.15), vy: rand(-H * 0.55, -H * 0.2), life: 0, max: rand(0.4, 0.9), r: rand(1.5, 3.5) * u, c: "#f6fffd" });
    }
  }

  /* ---------------- lightning ---------------- */
  function strike(tx?: number, ty?: number) {
    const toMonster = tx === undefined;
    const ex = toMonster ? head.x : tx!, ey = toMonster ? head.y : ty!;
    const branches: [number, number][][] = [];
    const walk = (x: number, y: number, ex2: number, ey2: number, depth: number) => {
      const pts: [number, number][] = [[x, y]];
      const steps = 18;
      for (let i = 1; i <= steps; i++) {
        const k = i / steps;
        const jitter = (1 - k) * H * 0.05;
        const px = x + (ex2 - x) * k + rand(-jitter, jitter), py = y + (ey2 - y) * k;
        pts.push([px, py]);
        if (depth < 2 && Math.random() < 0.1 && k < 0.8) walk(px, py, px + rand(-H * 0.25, H * 0.25), py + rand(H * 0.08, H * 0.2), depth + 1);
      }
      pts[pts.length - 1] = [ex2, ey2];
      branches.push(pts);
    };
    walk(ex + rand(-H * 0.3, H * 0.3), -10, ex, ey, 0);
    bolt = { pts: branches, t0: t };
    flash = 0.85; shake = 0.16;
    if (toMonster) {
      charged = 2.4;
    } else {
      ripples.push({ x: ex, y: Math.max(ey, geo.y0 + 6), t0: t });
      for (let i = 0; i < 30; i++) parts.push({ x: ex, y: ey, vx: rand(-H * 0.4, H * 0.4), vy: rand(-H * 0.7, -H * 0.1), life: 0, max: rand(0.5, 1.2), r: rand(1.5, 4), c: "#fff6d2" });
    }
    opts.onStrike?.(toMonster ? 1 : 0.75);
  }

  function paintFx() {
    for (const p of parts) {
      o.globalAlpha = 1 - p.life / p.max;
      o.fillStyle = p.c; o.beginPath(); o.arc(p.x, p.y, p.r, 0, 7); o.fill();
    }
    o.globalAlpha = 1;
    if (bolt) {
      const e = t - bolt.t0;
      if (e < 0.09 || (e > 0.13 && e < 0.24) || (e > 0.28 && e < 0.42)) {
        for (const [w, c] of [[H * 0.02, "rgba(255,240,190,.32)"], [H * 0.007, "#fff6d2"]] as const) {
          o.strokeStyle = c; o.lineWidth = w; o.lineJoin = "round";
          for (const b of bolt.pts) { o.beginPath(); b.forEach(([x, y], i) => (i ? o.lineTo(x, y) : o.moveTo(x, y))); o.stroke(); }
        }
      }
    }
    if (flash > 0.02) { o.fillStyle = `rgba(255,244,215,${flash * 0.28})`; o.fillRect(0, 0, W, H); }
  }

  /* ---------------- ASCII shading ---------------- */
  function shade() {
    const d = o.getImageData(0, 0, cols, rows).data;
    const n = cols * rows;
    for (let i = 0, j = 0; i < n; i++, j += 4) {
      const r = d[j], g = d[j + 1], b = d[j + 2];
      lum[i] = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
      pal[i] = LUT[((r >> 3) << 10) | ((g >> 3) << 5) | (b >> 3)];
    }
    rowMask.fill(0);
    const L = RAMP.length - 1;
    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < cols; x++) {
        const i = y * cols + x, l = lum[i];
        if (l < 0.075 && pal[i] !== 14 && pal[i] !== 21) { ch[i] = 32; continue; }
        const l2 = pal[i] === 14 || pal[i] === 21 ? Math.max(l, 0.32) : l;
        let c = RAMP.charCodeAt(Math.min(L, Math.floor(Math.pow(l2, 0.85) * L)));
        if (x > 0 && y > 0 && x < cols - 1 && y < rows - 1) {
          const a = lum[i - cols - 1], b2 = lum[i - cols], c2 = lum[i - cols + 1];
          const dl = lum[i - 1], dr = lum[i + 1];
          const e1 = lum[i + cols - 1], e2 = lum[i + cols], e3 = lum[i + cols + 1];
          const gx = c2 + 2 * dr + e3 - a - 2 * dl - e1;
          const gy = e1 + 2 * e2 + e3 - a - 2 * b2 - c2;
          const m = Math.hypot(gx, gy * 0.6);
          if (m > 0.5) {
            const ang = Math.atan2(gy, gx);
            const q = ((Math.round(ang / (Math.PI / 4)) % 4) + 4) % 4;
            c = ["|", "/", "-", "\\"][q].charCodeAt(0);
            // brighten edges by one palette step in the same family
          }
        }
        ch[i] = c;
        rowMask[y] |= 1 << pal[i];
      }
    }
  }

  function render() {
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    if (shake > 0) ctx.translate(rand(-1, 1) * shake * 9, rand(-1, 1) * shake * 9);
    ctx.textBaseline = "top";
    ctx.font = `600 ${fs}px "Geist Mono", ui-monospace, Menlo, Consolas, monospace`;
    const buf: string[] = new Array(cols);
    for (let k = 0; k < NP; k++) {
      if (!DISPLAY[k]) continue;
      const gl = GLOW[k];
      ctx.shadowColor = gl ? gl[0] : "transparent"; ctx.shadowBlur = gl ? gl[1] : 0;
      ctx.fillStyle = DISPLAY[k];
      const bit = 1 << k;
      for (let y = 0; y < rows; y++) {
        if (!(rowMask[y] & bit)) continue;
        const r0 = y * cols;
        for (let x = 0; x < cols; x++) buf[x] = pal[r0 + x] === k && ch[r0 + x] !== 32 ? String.fromCharCode(ch[r0 + x]) : " ";
        ctx.fillText(buf.join(""), 0, y * lh);
      }
    }
    ctx.shadowBlur = 0;
  }

  function step(dt: number) {
    if (t > nextStrike && !bolt) {
      // mostly distant strikes out at sea; now and then one charges his bolts
      if (Math.random() < 0.5) strike();
      else strike(rand(0.05, 0.95) * W, geo.y0 + rand(0, H * 0.04));
      nextStrike = t + rand(7, 12);
    }
    if (bolt && t - bolt.t0 > 0.45) bolt = null;
    flash = Math.max(0, flash - dt * 4);
    shake = Math.max(0, shake - dt);
    charged = Math.max(0, charged - dt);
    blink = blink > 0 ? blink - dt : Math.random() < dt * 0.25 ? 0.14 : 0;
    for (const p of parts) { p.life += dt; p.vy += H * 1.4 * dt; p.x += p.vx * dt; p.y += p.vy * dt; }
    for (let i = parts.length - 1; i >= 0; i--) if (parts[i].life > parts[i].max) parts.splice(i, 1);
    for (let i = ripples.length - 1; i >= 0; i--) if (t - ripples[i].t0 > 1.8) ripples.splice(i, 1);
    // crest spindrift and the curtain falling off the lip
    if (t > 0.5) {
      const { cx, cy, WW } = geo;
      parts.push({ x: cx + rand(-WW * 0.02, WW * 0.02), y: cy, vx: rand(-H * 0.5, -H * 0.15), vy: rand(-H * 0.25, -H * 0.05), life: 0, max: rand(0.4, 0.9), r: rand(1.5, 3), c: "#f6fffd" });
      parts.push({ x: cx + WW * 0.205 + rand(-6, 6), y: cy + H * 0.29, vx: rand(-H * 0.05, H * 0.08), vy: rand(0, H * 0.2), life: 0, max: rand(0.3, 0.6), r: rand(2, 4), c: "#d8fbf7" });
    }
  }

  function frame() {
    o.setTransform(cols / W, 0, 0, rows / H, 0, 0);
    paintSky(); paintSea(); paintWave(); paintMonster(); paintFx();
    shade(); render();
  }

  function loop(now: number) {
    animationFrame = 0;
    if (disposed || !running || reduce) return;
    const elapsed = now - last;
    if (elapsed >= frameInterval) {
      const dt = Math.min(0.05, elapsed / 1000); last = now;
      t += dt; step(dt); frame();
    }
    animationFrame = requestAnimationFrame(loop);
  }

  function setRunning(active: boolean) {
    running = active;
    if (!active) { cancelAnimationFrame(animationFrame); animationFrame = 0; }
    else if (!reduce && !disposed && !animationFrame) {
      last = performance.now(); animationFrame = requestAnimationFrame(loop);
    }
  }

  let lastRipple = 0;
  resize(opts.width, opts.height, opts.dpr);
  if (reduce) t = 3;
  frame();
  setRunning(true);
  return {
    resize(width: number, height: number, pixelRatio: number) {
      resize(width, height, pixelRatio); frame();
    },
    setRunning,
    pointerMove(x: number, y: number) {
      if (!running || reduce) return;
      if (y > geo.y0 && t - lastRipple > 0.12) {
        ripples.push({ x, y, t0: t }); lastRipple = t;
        if (ripples.length > 10) ripples.shift();
      }
    },
    strike(x?: number, y?: number) {
      if (reduce) {
        charged = charged ? 0 : 0.35;
        frame(); opts.onStrike?.(charged ? 0.5 : 0);
        return;
      }
      if (x === undefined || y === undefined || Math.hypot(x - head.x, y - head.y) < geo.S * 0.35) strike();
      else strike(x, y);
    },
    destroy() {
      disposed = true; running = false; cancelAnimationFrame(animationFrame);
    },
  };
}
