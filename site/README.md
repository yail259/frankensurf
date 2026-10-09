# Frankensurf landing page

Static Astro site. The live ASCII hero runs in `src/scripts/surf-worker.ts` using
OffscreenCanvas. `surf-renderer.ts` paints and shades the scene at up to 24 frames
per second; `surf.ts` forwards resizing, visibility and pointer events. Older
browsers use the same renderer on the main thread. `src/scripts/nav.ts` drives the
navbar's voltage trace.

The fonts are served locally through `src/styles/fonts.css`. Navigation links
between the landing page and docs opt into Astro hover prefetching. The scene
pauses when hidden or outside the viewport and respects reduced-motion settings.
Add `?motion=reduced` to preview the calm layout. On phones, the router waits
for taps, logos form a complete grid and tool descriptions use native disclosures.
Clicking the scene toggles a single charged frame when motion is reduced.

```bash
npm install
npm run dev      # http://localhost:4322
npm run build    # static output in dist/
```

## Brand mark

The stitched wave is a transparent SVG in `src/assets/stitched-wave.svg`. Its
seams are cut out of the silhouette. The matching mint and ink variants serve
the dark and light Docs themes. `public/favicon-stitched-wave.svg` uses the same
geometry, centered on a square canvas and coloured for the browser theme.
`public/apple-touch-icon.png` provides the mark on a flat dark background for
mobile home-screen bookmarks.

## Publish to Cloudflare

Production: https://frankensurf.dev/

Documentation: https://frankensurf.dev/docs/

Deploy only from an up-to-date `main` (owner rule, 2026-10-06). Merge the change
first, then deploy; deploying a feature branch publishes a site that the next
deploy from `main` silently replaces, or one that drops work already on `main`.

From this `site/` directory, use the Ubuntu environment where Wrangler is logged in:

```bash
npm ci
npm run deploy
```

The deploy command builds the static site and uploads only `dist/` to the
`frankensurf` Cloudflare Pages project on its production branch (`main`). No
server runtime or Cloudflare adapter is required. Wrangler is pinned in the
command, and authentication uses your local Wrangler login.
