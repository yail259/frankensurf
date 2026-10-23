// Page interactions: router product window, scroll reveals, card spotlight.

import { reducedMotion } from "./motion";
import { READS, READS_CAPTURED } from "../data/reads";
const reduce = reducedMotion();
const manual = reduce || matchMedia("(max-width: 760px)").matches;

/* ---------- router window: real reads, replayed ---------- */

const TOOL_NAMES: Record<string, string> = {
  http: "Plain HTTP", jina_reader: "Jina Reader", scrapling: "Scrapling", camoufox: "Camoufox",
  local: "Chromium", patchright: "Patchright",
};
const OUTCOMES: Record<string, [cls: string, text: string]> = {
  kept: ["ok", "page kept"], page: ["page", "page, not kept"], BLOCKED: ["no", "blocked"],
  CAPTCHA: ["no", "challenge page"], NOT_FOUND: ["no", "not found"], TIMEOUT: ["no", "timed out"],
  CancelledError: ["stop", "stopped, race won"],
};
const secs = (s: number) => s.toFixed(1) + "s";
const esc = (text: string) => text.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]!);

function mountRouter(root: HTMLElement) {
  const lanes = root.querySelector<HTMLOListElement>("[data-ladder]")!;
  const code = root.querySelector<HTMLElement>("[data-code]")!;
  const summary = root.querySelector<HTMLElement>("[data-summary]")!;
  const plainEl = root.querySelector<HTMLElement>("[data-native]")!;
  const noteEl = root.querySelector<HTMLElement>("[data-note]")!;
  const siteTabs = root.querySelector<HTMLElement>("[data-sites]")!;
  let site = 0, timers: number[] = [], auto = !manual;

  siteTabs.innerHTML = READS.map((r, i) => `<button type="button" data-i="${i}">${esc(r.label)}</button>`).join("");

  function run() {
    timers.forEach(clearTimeout); timers = [];
    const r = READS[site];
    siteTabs.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", String(i === site)));
    code.innerHTML = `<span class="f">frankensurf</span> read <span class="s">"${esc(r.url)}"</span>`;
    plainEl.innerHTML = `<span class="n-label">A plain fetch alone</span><span class="n-bad${r.plain.status === "works" ? " n-ok" : ""}">${r.plain.status}</span><span class="n-why">${esc(r.plain.why)}</span>`;
    noteEl.textContent = "";
    summary.innerHTML = "";
    // Bars sit on one time axis: the read's own length, so a 1 s read and a 16 s read both fill the width.
    const span = Math.max(r.total, ...r.events.map((e) => e.end));
    // Played at real speed for short reads, compressed for long ones, so no tab takes over 6 s.
    const speed = Math.min(1, 6 / span);
    lanes.innerHTML = r.events.map((e) => {
      const [cls, text] = OUTCOMES[e.outcome] ?? ["no", e.outcome.toLowerCase().replace(/_/g, " ")];
      const left = (e.start / span) * 100, width = Math.max(((e.end - e.start) / span) * 100, 1.2);
      return `<li class="lane ${cls}"><span class="l-tool">${TOOL_NAMES[e.tool] ?? esc(e.tool)}</span>` +
        `<span class="l-track"><i style="left:${left}%;--w:${width}%;--d:${Math.max((e.end - e.start) * speed, 0.15)}s"></i></span>` +
        `<span class="l-out">${text}<small>${e.on ? e.on + " · " : ""}${secs(e.start)} to ${secs(e.end)}</small></span></li>`;
    }).join("");
    const items = Array.from(lanes.children) as HTMLElement[];
    const finish = () => {
      noteEl.textContent = r.note;
      summary.innerHTML = `<span><em>${secs(r.total)}</em> total</span>` +
        (r.ok ? `<span>page from <em>${TOOL_NAMES[r.method!] ?? r.method}</em> · ${r.chars.toLocaleString()} chars</span>`
              : `<span><em>no page</em> · says why (${r.failure?.toLowerCase()})</span>`) +
        `<span>real read · ${READS_CAPTURED} · home connection</span>`;
      if (auto) timers.push(window.setTimeout(() => { site = (site + 1) % READS.length; run(); }, 3200));
    };
    if (manual) { items.forEach((li) => li.classList.add("on", "done")); finish(); return; }
    // Each bar grows for as long as that tool really ran; its outcome shows when it finished.
    r.events.forEach((e, i) => {
      timers.push(window.setTimeout(() => items[i].classList.add("on"), 200 + e.start * speed * 1000));
      timers.push(window.setTimeout(() => items[i].classList.add("done"), 200 + e.end * speed * 1000));
    });
    timers.push(window.setTimeout(finish, 400 + span * speed * 1000));
  }
  siteTabs.addEventListener("click", (e) => { const b = (e.target as HTMLElement).closest("button"); if (!b) return; auto = false; site = +b.dataset.i!; run(); });

  if (manual) { run(); return; }
  let started = false;
  new IntersectionObserver(([en]) => { if (en.isIntersecting && !started) { started = true; run(); } }, { threshold: 0.25 }).observe(root);
}

/* ---------- reveals: only elements below the fold start hidden ---------- */
function mountReveals() {
  const els = document.querySelectorAll<HTMLElement>("[data-reveal]");
  if (reduce) { els.forEach((el) => el.classList.add("in")); return; }
  const io = new IntersectionObserver((entries) => {
    for (const en of entries) if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
  }, { threshold: 0.15, rootMargin: "0px 0px -6% 0px" });
  els.forEach((el) => {
    if (el.getBoundingClientRect().top > innerHeight) { el.classList.add("pre"); io.observe(el); }
    else el.classList.add("in");
  });
}

/* ---------- spotlight cards ---------- */
function mountSpotlight() {
  document.querySelectorAll<HTMLElement>("[data-spot]").forEach((el) => {
    el.addEventListener("pointermove", (e) => {
      const r = el.getBoundingClientRect();
      el.style.setProperty("--mx", `${e.clientX - r.left}px`);
      el.style.setProperty("--my", `${e.clientY - r.top}px`);
    });
  });
}

/* ---------- head to head: rotate comparisons until someone picks one ---------- */
function mountVersus(root: HTMLElement) {
  const tabs = Array.from(root.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
  const slides = Array.from(root.querySelectorAll<HTMLElement>('[role="tabpanel"]'));
  const ms = 5000;
  let current = 0, timer = 0, auto = !reduce;
  root.style.setProperty("--vs-ms", ms + "ms");
  const show = (i: number, focus = false) => {
    current = (i + tabs.length) % tabs.length;
    tabs.forEach((t, j) => { t.setAttribute("aria-selected", String(j === current)); t.tabIndex = j === current ? 0 : -1; });
    slides.forEach((s, j) => { s.hidden = j !== current; });
    // on phones the tabs scroll sideways; keep the active one in view without moving the page
    const list = tabs[current].parentElement!;
    if (list.scrollWidth > list.clientWidth) list.scrollTo({ left: tabs[current].offsetLeft - 10, behavior: reduce ? "auto" : "smooth" });
    if (focus) tabs[current].focus();
    // restart the progress line on the active tab
    root.classList.remove("playing"); void root.offsetWidth; if (auto) root.classList.add("playing");
  };
  const stop = () => { auto = false; clearInterval(timer); root.classList.remove("playing"); };
  const start = () => { if (!auto) return; clearInterval(timer); root.classList.add("playing"); timer = window.setInterval(() => show(current + 1), ms); };
  tabs.forEach((t, i) => t.addEventListener("click", () => { stop(); show(i); }));
  root.querySelector('[role="tablist"]')!.addEventListener("keydown", (e) => {
    const k = (e as KeyboardEvent).key;
    if (k === "ArrowDown" || k === "ArrowRight") { e.preventDefault(); stop(); show(current + 1, true); }
    if (k === "ArrowUp" || k === "ArrowLeft") { e.preventDefault(); stop(); show(current - 1, true); }
  });
  // hold still while someone is reading it, and only run while it is on screen
  root.addEventListener("pointerenter", () => { clearInterval(timer); root.classList.remove("playing"); });
  root.addEventListener("pointerleave", () => { if (auto) { show(current); start(); } });
  new IntersectionObserver(([en]) => { if (en.isIntersecting) { show(current); start(); } else { clearInterval(timer); } }, { threshold: 0.3 }).observe(root);
}

export function mountPage() {
  const r = document.querySelector<HTMLElement>("[data-router]");
  if (r) mountRouter(r);
  const v = document.querySelector<HTMLElement>("[data-versus]");
  if (v) mountVersus(v);
  mountReveals();
  mountSpotlight();
}
