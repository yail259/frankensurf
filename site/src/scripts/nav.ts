// Lab-bench navbar: a live EKG/voltage trace that spikes whenever lightning
// strikes in the hero and a surge flare around the bar.

import { reducedMotion } from "./motion";

export function mountNav() {
  const nav = document.getElementById("nav")!;
  const ekg = document.getElementById("ekg") as HTMLCanvasElement;
  const kv = document.getElementById("kv")!;
  const g = ekg.getContext("2d")!;
  const reduce = reducedMotion();
  const dpr = Math.min(devicePixelRatio || 1, 2);
  ekg.width = 120 * dpr; ekg.height = 28 * dpr; g.scale(dpr, dpr);

  const N = 60;
  const trace = new Float32Array(N);
  let charge = 0, t = 0, last = performance.now(), surgeTimer = 0;

  function draw() {
    g.clearRect(0, 0, 120, 28);
    g.strokeStyle = "rgba(255,255,255,.06)"; g.lineWidth = 1;
    g.beginPath(); g.moveTo(0, 14); g.lineTo(120, 14); g.stroke();
    const hot = charge > 0.15;
    g.strokeStyle = hot ? "#ffd36b" : "#9af0b0";
    g.shadowColor = hot ? "rgba(255,211,107,.9)" : "rgba(154,240,176,.7)"; g.shadowBlur = hot ? 8 : 4;
    g.lineWidth = 1.5; g.lineJoin = "round";
    g.beginPath();
    for (let i = 0; i < N; i++) { const x = (i / (N - 1)) * 120, y = 14 - trace[i] * 12; i ? g.lineTo(x, y) : g.moveTo(x, y); }
    g.stroke(); g.shadowBlur = 0;
    // leading dot
    g.fillStyle = hot ? "#fff6d2" : "#d9ffe4";
    g.beginPath(); g.arc(120 - 1.5, 14 - trace[N - 1] * 12, 2, 0, 7); g.fill();
  }

  function tick(now: number) {
    const dt = Math.min(0.05, (now - last) / 1000); last = now; t += dt;
    if (Math.floor(t * 30) !== Math.floor((t - dt) * 30)) {
      trace.copyWithin(0, 1);
      // idle heartbeat blip every ~1.2s, wild jitter while charged
      const beat = t % 1.2 < 0.05 ? 0.55 : t % 1.2 < 0.09 ? -0.35 : 0;
      trace[N - 1] = beat + (Math.random() - 0.5) * (0.08 + charge * 1.2);
      charge = Math.max(0, charge - 0.025);
      const volts = 0.4 + charge * 36 + Math.random() * 0.3;
      kv.textContent = volts.toFixed(1);
      nav.classList.toggle("charged", charge > 0.15);
    }
    draw();
    requestAnimationFrame(tick);
  }
  function restingTrace() {
    trace.fill(0);
    trace.set([0.08, 0.2, -0.24, 0.7, -0.32, 0.12, 0.04], 33);
  }
  if (reduce) { restingTrace(); kv.textContent = "0.4"; draw(); } else requestAnimationFrame(tick);

  /* Ambient loops hold still while the page scrolls, so the scroll is the only motion on screen.
   * The navbar current animates a custom property, which repaints every frame. */
  let scrolling = false, settle = 0;
  let held: Animation[] = [];
  const onScroll = () => {
    nav.classList.toggle("scrolled", scrollY > 24);
    if (reduce) return;
    if (!scrolling) {
      scrolling = true;
      held = document.getAnimations().filter((a) => a.playState === "running" && a.effect?.getComputedTiming().iterations === Infinity);
      for (const a of held) a.pause();
    }
    clearTimeout(settle);
    settle = window.setTimeout(() => { scrolling = false; for (const a of held) a.play(); held = []; }, 180);
  };
  addEventListener("scroll", onScroll, { passive: true }); onScroll();

  return {
    jolt(power = 1) {
      if (reduce) {
        charge = power > 0 ? 0.5 : 0;
        restingTrace(); draw(); kv.textContent = charge ? "0.8" : "0.4";
        document.getElementById("strike-status")!.textContent = charge ? "Scene charged." : "Scene resting.";
        return;
      }
      charge = Math.min(1, charge + power);
      trace[N - 1] = 0.7; trace[N - 2] = -0.5;
      nav.classList.remove("surge"); void nav.offsetWidth; nav.classList.add("surge");
      clearTimeout(surgeTimer);
      surgeTimer = window.setTimeout(() => nav.classList.remove("surge"), 1200);
    },
  };
}
