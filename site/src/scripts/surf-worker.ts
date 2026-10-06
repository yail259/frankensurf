import { createSurfRenderer, type SurfRenderer } from "./surf-renderer";

type Message =
  | { type: "init"; canvas: OffscreenCanvas; width: number; height: number; dpr: number; reduceMotion: boolean; fontUrl: string }
  | { type: "resize"; width: number; height: number; dpr: number }
  | { type: "running"; active: boolean }
  | { type: "pointer"; x: number; y: number }
  | { type: "strike"; x?: number; y?: number };

const scope = self as unknown as DedicatedWorkerGlobalScope & { fonts: FontFaceSet };
let renderer: SurfRenderer | undefined;
const pending: Message[] = [];

function apply(message: Message) {
  if (!renderer) { pending.push(message); return; }
  if (message.type === "resize") renderer.resize(message.width, message.height, message.dpr);
  else if (message.type === "running") renderer.setRunning(message.active);
  else if (message.type === "pointer") renderer.pointerMove(message.x, message.y);
  else if (message.type === "strike") renderer.strike(message.x, message.y);
}

scope.addEventListener("message", async ({ data }: MessageEvent<Message>) => {
  if (data.type !== "init") { apply(data); return; }
  // Canvas fonts have a separate font set in a worker.
  try {
    const font = new FontFace("Geist Mono", `url(${data.fontUrl})`, { weight: "100 900" });
    scope.fonts.add(await font.load());
  } catch { /* Keep the system monospace fallback available. */ }
  renderer = createSurfRenderer(data.canvas, new OffscreenCanvas(1, 1), {
    width: data.width, height: data.height, dpr: data.dpr, reduceMotion: data.reduceMotion,
    onStrike: (power) => scope.postMessage({ type: "strike", power }),
  });
  pending.splice(0).forEach(apply);
  scope.postMessage({ type: "ready" });
});
