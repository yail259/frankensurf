// Page interactions: router product window, scroll reveals, card spotlight.

import { reducedMotion } from "./motion";
const reduce = reducedMotion();
const manual = reduce || matchMedia("(max-width: 760px)").matches;

/* ---------- router window ---------- */
type Step = [ok: 0 | 1 | 2, ms?: number, note?: string];
const PROVIDERS: Record<string, { label: string; native: string }> = {
  claude: { label: "Claude", native: "web_fetch" },
  gpt: { label: "GPT", native: "web_search" },
  gemini: { label: "Gemini", native: "url_context" },
  open: { label: "Open-weight", native: "none" },
};
const SITES: { k: string; label: string; url: string; task: string; native: string; steps: Step[]; tokens: string }[] = [
  { k: "docs", label: "Docs page", url: "docs.acme.dev/api/limits", task: "rate limits per plan", native: "raw HTML, 14k tokens of nav",
    steps: [[1, 160, "origin served markdown"]], tokens: "1.9k" },
  { k: "spa", label: "JS app", url: "app.acme.dev/billing", task: "current invoice total", native: "empty app shell",
    steps: [[0, 90, "no markdown variant"], [0, 140, "empty shell, needs JS"], [1, 1100, "rendered, trimmed"]], tokens: "3.1k" },
  { k: "cf", label: "Cloudflare", url: "shop.example.com/p/2231", task: "price and stock", native: "challenge page",
    steps: [[0, 80, "403 challenge"], [0, 120, "Turnstile"], [0, 900, "challenge loop"], [1, 2300, "signed agent accepted"]], tokens: "2.8k" },
  { k: "dd", label: "DataDome", url: "retail.example.com/deals", task: "top 10 deals", native: "blocked at edge",
    steps: [[0, 80, "blocked"], [0, 110, "blocked"], [0, 850, "fingerprint flagged"], [0, 1900, "flagged mid-session"], [1, 3400, "unblocker route"]], tokens: "4.6k" },
  { k: "login", label: "Login wall", url: "portal.example.com/orders", task: "last 5 orders", native: "login page",
    steps: [[0, 70, "401"], [0, 90, "login redirect"], [1, 1400, "saved session reused"]], tokens: "3.8k" },
  { k: "mfa", label: "2FA", url: "bank.example.com/statements", task: "download statement", native: "stuck at 2FA",
    steps: [[0, 70, "401"], [0, 90, "login redirect"], [0, 1200, "2FA prompt"], [2], [2], [1, 1800, "approval sent to you"]], tokens: "<1k" },
];
const RUNGS = [
  ["T0", "Content negotiation"], ["T1", "Light fetch"], ["T2", "Managed browser"],
  ["T3", "Stealth + signed identity"], ["T4", "Unblocker network"], ["T5", "Human handoff"],
];

const fmt = (ms: number) => (ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : ms + "ms");

function mountRouter(root: HTMLElement) {
  const ladder = root.querySelector<HTMLOListElement>("[data-ladder]")!;
  const code = root.querySelector<HTMLElement>("[data-code]")!;
  const summary = root.querySelector<HTMLElement>("[data-summary]")!;
  const nativeEl = root.querySelector<HTMLElement>("[data-native]")!;
  const siteTabs = root.querySelector<HTMLElement>("[data-sites]")!;
  const provTabs = root.querySelector<HTMLElement>("[data-providers]")!;
  let site = 0, prov = "claude", timers: number[] = [], auto = !manual, cycle = 0;

  siteTabs.innerHTML = SITES.map((s, i) => `<button type="button" data-i="${i}">${s.label}</button>`).join("");
  provTabs.innerHTML = Object.entries(PROVIDERS).map(([k, p]) => `<button type="button" data-k="${k}">${p.label}</button>`).join("");
  ladder.innerHTML = RUNGS.map(([id, n]) => `<li><span class="r-id">${id}</span><span class="r-name">${n}<small></small></span><span class="r-ms"></span></li>`).join("");

  function run() {
    timers.forEach(clearTimeout); timers = [];
    const s = SITES[site], p = PROVIDERS[prov];
    siteTabs.querySelectorAll("button").forEach((b, i) => b.setAttribute("aria-pressed", String(i === site)));
    provTabs.querySelectorAll<HTMLButtonElement>("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.k === prov)));
    code.innerHTML =
      `<span class="k">const</span> page = <span class="k">await</span> <span class="f">surf</span>(<span class="s">"https://${s.url}"</span>, {\n` +
      `  model: <span class="s">"${prov}"</span>,\n  task: <span class="s">"${s.task}"</span>,\n})`;
    nativeEl.innerHTML = `<span class="n-label">${p.label} alone · <code>${p.native}</code></span><span class="n-bad">${s.k === "docs" ? "works, wasteful" : "fails"}</span><span class="n-why">${s.native}</span>`;
    summary.innerHTML = "";
    const lis = Array.from(ladder.children) as HTMLLIElement[];
    lis.forEach((li) => { li.className = ""; li.querySelector("small")!.textContent = ""; li.querySelector(".r-ms")!.textContent = ""; });
    let total = 0, winIdx = 0, winMs = 0;
    s.steps.forEach((st, i) => { if (st[0] !== 2) total += st[1]!; if (st[0] === 1) { winIdx = i; winMs = st[1]!; } });
    let delay = 0;
    s.steps.forEach((st, i) => {
      const show = () => {
        const li = lis[i];
        if (st[0] === 2) { li.className = "skip"; li.querySelector(".r-ms")!.textContent = "skipped"; return; }
        li.className = st[0] === 1 ? "ok" : "no";
        li.querySelector("small")!.textContent = st[2]!;
        li.querySelector(".r-ms")!.textContent = fmt(st[1]!);
        if (i === s.steps.length - 1) {
          summary.innerHTML = `<span><em>${fmt(total)}</em> total</span><span><em>${s.tokens}</em> tokens to model</span><span>next visit starts at <em>${RUNGS[winIdx][0]}</em> · ${fmt(winMs)}</span>`;
          if (auto) timers.push(window.setTimeout(() => {
            site = (site + 1) % SITES.length; cycle++;
            if (cycle % SITES.length === 0) { const keys = Object.keys(PROVIDERS); prov = keys[(keys.indexOf(prov) + 1) % keys.length]; }
            run();
          }, 2600));
        }
      };
      if (manual) show(); else { delay += i === 0 ? 250 : 420; timers.push(window.setTimeout(show, delay)); }
    });
  }
  siteTabs.addEventListener("click", (e) => { const b = (e.target as HTMLElement).closest("button"); if (!b) return; auto = false; site = +b.dataset.i!; run(); });
  provTabs.addEventListener("click", (e) => { const b = (e.target as HTMLElement).closest<HTMLButtonElement>("button"); if (!b) return; auto = false; prov = b.dataset.k!; run(); });

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
