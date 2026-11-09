// hearchurch.com's marketing pages (docs/seo-site-structure.md). Static HTML with the CSS inlined, served by the
// hearchurch-download Worker (download/wrangler.toml builds this before every deploy).
import { defineConfig } from "astro/config";

export default defineConfig({
  site: "https://hearchurch.com",
  trailingSlash: "never",
  build: { format: "file", inlineStylesheets: "always" },
});
