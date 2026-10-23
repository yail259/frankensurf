// Real reads for the landing page, captured 23 October 2026 from a home connection with the
// Frankensurf Docker image (0.51.0 plus the speed fixes in this release). Every time is measured:
// start and end of each tool, in seconds from the start of the read. Regenerate, never edit by hand.
export const READS_CAPTURED = "23 October 2026";
export const READS = [
 {
  "key": "docs",
  "label": "Docs search",
  "url": "https://archlinux.org/packages/?q=nginx",
  "total": 1.24,
  "ok": true,
  "chars": 6702,
  "method": "http",
  "failure": null,
  "plain": {
   "status": "works",
   "why": "39 results"
  },
  "note": "Plain HTTP had all 39 results in about a second. Nothing else ran.",
  "events": [
   {
    "tool": "http",
    "start": 0.02,
    "end": 1.17,
    "on": null,
    "outcome": "kept"
   }
  ]
 },
 {
  "key": "js",
  "label": "JS search",
  "url": "https://jsr.io/packages?search=http",
  "total": 5.08,
  "ok": true,
  "chars": 3258,
  "method": "http",
  "failure": null,
  "plain": {
   "status": "works",
   "why": "20 results, but scripts may add more"
  },
  "note": "The page loads with scripts, so a browser checked it too. Plain HTTP already had the 20 results, so that page stayed.",
  "events": [
   {
    "tool": "http",
    "start": 0.02,
    "end": 1.18,
    "on": null,
    "outcome": "kept"
   },
   {
    "tool": "local",
    "start": 3.25,
    "end": 5.01,
    "on": null,
    "outcome": "page"
   }
  ]
 },
 {
  "key": "wall",
  "label": "Bot wall",
  "url": "https://teara.govt.nz/en/kiwi",
  "total": 6.56,
  "ok": true,
  "chars": 42171,
  "method": "camoufox",
  "failure": null,
  "plain": {
   "status": "fails",
   "why": "403 blocked"
  },
  "note": "Plain HTTP got a 403 from Cloudflare. Camoufox, Jina Reader and Scrapling all came back with pages; Camoufox's scored best on the content check, so it won.",
  "events": [
   {
    "tool": "http",
    "start": 0.02,
    "end": 0.07,
    "on": null,
    "outcome": "BLOCKED"
   },
   {
    "tool": "camoufox",
    "start": 0.07,
    "end": 5.41,
    "on": null,
    "outcome": "kept"
   },
   {
    "tool": "jina_reader",
    "start": 2.0,
    "end": 4.17,
    "on": null,
    "outcome": "page"
   },
   {
    "tool": "scrapling",
    "start": 2.51,
    "end": 6.07,
    "on": null,
    "outcome": "page"
   }
  ]
 },
 {
  "key": "images",
  "label": "Image search",
  "url": "https://commons.wikimedia.org/w/index.php?search=lighthouse&title=Special:MediaSearch&type=image",
  "total": 2.44,
  "ok": true,
  "chars": 24851,
  "method": "jina_reader",
  "failure": null,
  "plain": {
   "status": "fails",
   "why": "900 characters, results don't mention the query"
  },
  "note": "Plain HTTP got a 900-character shell with no lighthouses in it. Jina Reader had the image results 2.4 s in.",
  "events": [
   {
    "tool": "http",
    "start": 0.02,
    "end": 2.41,
    "on": null,
    "outcome": "CancelledError"
   },
   {
    "tool": "jina_reader",
    "start": 2.0,
    "end": 2.38,
    "on": null,
    "outcome": "kept"
   }
  ]
 },
 {
  "key": "rescue",
  "label": "Wrong URL",
  "url": "https://pricespy.co.nz/search?search=vacuum",
  "total": 19.78,
  "ok": true,
  "chars": 5865,
  "method": "http",
  "failure": null,
  "plain": {
   "status": "fails",
   "why": "404 not found"
  },
  "note": "The URL's search parameter no longer works: three tools agree it's a 404. Frankensurf found the site's real search URL in the home page's data and read that.",
  "events": [
   {
    "tool": "http",
    "start": 0.02,
    "end": 0.1,
    "on": null,
    "outcome": "NOT_FOUND"
   },
   {
    "tool": "jina_reader",
    "start": 0.1,
    "end": 10.76,
    "on": null,
    "outcome": "NOT_FOUND"
   },
   {
    "tool": "jina_reader",
    "start": 2.0,
    "end": 5.49,
    "on": null,
    "outcome": "NOT_FOUND"
   },
   {
    "tool": "scrapling",
    "start": 2.51,
    "end": 6.55,
    "on": null,
    "outcome": "NOT_FOUND"
   },
   {
    "tool": "http",
    "start": 12.81,
    "end": 13.04,
    "on": "home page",
    "outcome": "page"
   },
   {
    "tool": "local",
    "start": 15.12,
    "end": 16.53,
    "on": "home page",
    "outcome": "page"
   },
   {
    "tool": "http",
    "start": 18.62,
    "end": 19.49,
    "on": "site's own search",
    "outcome": "kept"
   }
  ]
 },
 {
  "key": "fail",
  "label": "Honest failure",
  "url": "https://www.fodors.com/world/europe/italy/rome",
  "total": 12.73,
  "ok": false,
  "chars": 0,
  "method": "local",
  "failure": "BLOCKED",
  "plain": {
   "status": "fails",
   "why": "403 blocked"
  },
  "note": "Cloudflare blocked every tool, including a second Camoufox try through the home page. Two tools that never get past Cloudflare were skipped. It says so instead of handing back a challenge page.",
  "events": [
   {
    "tool": "http",
    "start": 0.02,
    "end": 0.06,
    "on": null,
    "outcome": "BLOCKED"
   },
   {
    "tool": "camoufox",
    "start": 0.06,
    "end": 3.24,
    "on": null,
    "outcome": "BLOCKED"
   },
   {
    "tool": "jina_reader",
    "start": 2.0,
    "end": 3.21,
    "on": null,
    "outcome": "BLOCKED"
   },
   {
    "tool": "scrapling",
    "start": 2.51,
    "end": 5.03,
    "on": null,
    "outcome": "BLOCKED"
   },
   {
    "tool": "local",
    "start": 3.24,
    "end": 3.91,
    "on": null,
    "outcome": "BLOCKED"
   },
   {
    "tool": "camoufox",
    "start": 5.05,
    "end": 12.71,
    "on": "through the home page",
    "outcome": "BLOCKED"
   }
  ]
 }
] as const;
