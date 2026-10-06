// Keep scene painting, pixel shading and ASCII drawing off the navigation thread.
import { reducedMotion } from "./motion";
import fontUrl from "@fontsource-variable/geist-mono/files/geist-mono-latin-wght-normal.woff2?url";
import type { SurfRenderer } from "./surf-renderer";

export function mountSurf(canvas: HTMLCanvasElement, opts: { onStrike?: (power: number) => void } = {}) {
  const reduceMotion = reducedMotion();
  const events = new AbortController();
  let surface = canvas, worker: Worker | undefined, renderer: SurfRenderer | undefined;
  let inView = true, pageVisible = !document.hidden, disposed = false;
  const measure = () => {
    const rect = surface.getBoundingClientRect();
    return { width: rect.width, height: rect.height, dpr: devicePixelRatio || 1 };
  };
  const running = () => inView && pageVisible;
  function syncRunning() {
    if (worker) worker.postMessage({ type: "running", active: running() });
    else renderer?.setRunning(running());
  }
  const resizeObserver = new ResizeObserver(() => {
    const size = measure();
    if (worker) worker.postMessage({ type: "resize", ...size });
    else renderer?.resize(size.width, size.height, size.dpr);
  });
  const intersectionObserver = new IntersectionObserver(([entry]) => {
    inView = entry.isIntersecting; syncRunning();
  });
  function bindSurface() {
    surface.addEventListener("pointermove", (event) => {
      if (!running() || reduceMotion) return;
      const rect = surface.getBoundingClientRect();
      const x = event.clientX - rect.left, y = event.clientY - rect.top;
      if (worker) worker.postMessage({ type: "pointer", x, y });
      else renderer?.pointerMove(x, y);
    }, { passive: true, signal: events.signal });
    surface.addEventListener("click", (event) => {
      const rect = surface.getBoundingClientRect();
      strike(event.clientX - rect.left, event.clientY - rect.top);
    }, { signal: events.signal });
    resizeObserver.observe(surface); intersectionObserver.observe(surface);
  }
  function strike(x?: number, y?: number) {
    if (worker) worker.postMessage({ type: "strike", x, y });
    else renderer?.strike(x, y);
  }
  async function startFallback() {
    const [{ createSurfRenderer }] = await Promise.all([import("./surf-renderer"), document.fonts.ready]);
    if (disposed) return;
    const off = document.createElement("canvas");
    renderer = createSurfRenderer(surface, off, { ...measure(), reduceMotion, onStrike: opts.onStrike });
    renderer.setRunning(running()); surface.dataset.renderer = "main";
  }
  function recoverWorker() {
    if (disposed || !worker) return;
    worker.terminate(); worker = undefined;
    // A transferred canvas cannot regain a main-thread context.
    const replacement = surface.cloneNode(false) as HTMLCanvasElement;
    resizeObserver.unobserve(surface); intersectionObserver.unobserve(surface);
    surface.replaceWith(replacement); surface = replacement;
    bindSurface(); void startFallback();
  }
  bindSurface();
  if (typeof Worker !== "undefined" && typeof OffscreenCanvas !== "undefined" && "transferControlToOffscreen" in surface) {
    try {
      worker = new Worker(new URL("./surf-worker.ts", import.meta.url), { type: "module" });
      worker.addEventListener("message", ({ data }) => {
        if (data.type === "strike") opts.onStrike?.(data.power);
        if (data.type === "ready") surface.dataset.renderer = "worker";
      });
      worker.addEventListener("error", recoverWorker, { once: true });
      const offscreen = surface.transferControlToOffscreen();
      worker.postMessage({ type: "init", canvas: offscreen, ...measure(), reduceMotion, fontUrl }, [offscreen]);
      syncRunning();
    } catch {
      if (worker) recoverWorker(); else void startFallback();
    }
  } else void startFallback();
  document.addEventListener("visibilitychange", () => {
    pageVisible = !document.hidden; syncRunning();
  }, { signal: events.signal });
  window.addEventListener("pagehide", (event) => {
    pageVisible = false; syncRunning();
    if (!event.persisted) {
      disposed = true; worker?.terminate(); renderer?.destroy();
      resizeObserver.disconnect(); intersectionObserver.disconnect(); events.abort();
    }
  }, { signal: events.signal });
  window.addEventListener("pageshow", () => {
    pageVisible = !document.hidden; syncRunning();
  }, { signal: events.signal });
  return { strike: () => strike() };
}
