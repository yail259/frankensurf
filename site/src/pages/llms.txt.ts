// /llms.txt: an index of the docs for language models (https://llmstxt.org).
import { getCollection } from "astro:content";

export async function GET() {
  const pages = (await getCollection("docs")).sort((a, b) => a.id.localeCompare(b.id));
  const lines = pages.map((page) => {
    const path = page.id === "docs" ? "/docs/" : `/${page.id}/`;
    return `- [${page.data.title}](https://frankensurf.pages.dev${path})${page.data.description ? ": " + page.data.description : ""}`;
  });
  const body = [
    "# Frankensurf",
    "",
    "> One call that gets your agent through any site, on any model. Frankensurf picks the right browsing tool for each page, climbs to a stronger one when a site pushes back, and returns raw page data with a receipt.",
    "",
    "## Docs",
    "",
    ...lines,
    "",
  ].join("\n");
  return new Response(body, { headers: { "Content-Type": "text/plain; charset=utf-8" } });
}
