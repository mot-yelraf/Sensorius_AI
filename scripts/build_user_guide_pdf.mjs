import { readFile, rename, rm, writeFile } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import path from "node:path";

import { chromium } from "playwright";
import { marked } from "marked";

const root = path.resolve(import.meta.dirname, "..");
const sourcePath = path.join(root, "docs", "user_guide.md");
const outputPath = path.join(root, "docs", "user_guide.pdf");
const temporaryHtml = path.join(root, "docs", ".user_guide.print.html");
const temporaryPdf = path.join(root, "docs", ".user_guide.pdf.tmp");

const markdown = await readFile(sourcePath, "utf8");
const parsedBody = await marked.parse(markdown, { gfm: true });
const keepTogetherStart = "<!-- pdf-keep-together:start -->";
const keepTogetherEnd = "<!-- pdf-keep-together:end -->";
const hasKeepTogetherPair =
  parsedBody.includes(keepTogetherStart) && parsedBody.includes(keepTogetherEnd);
const body = hasKeepTogetherPair
  ? parsedBody
      .replace(keepTogetherStart, '<section class="pdf-keep-together">')
      .replace(keepTogetherEnd, "</section>")
  : parsedBody;
const baseHref = pathToFileURL(path.join(root, "docs") + path.sep).href;
const html = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <base href="${baseHref}">
  <title>Sensorius User Guide</title>
  <style>
    @page { size: Letter; margin: 0.65in 0.62in 0.7in; }
    * { box-sizing: border-box; }
    body { color: #17212b; font: 10.2pt/1.42 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; }
    h1, h2, h3, h4 { color: #143d38; break-after: avoid-page; line-height: 1.2; }
    h1 { border-bottom: 3px solid #3a8d7d; font-size: 25pt; margin: 0 0 18pt; padding-bottom: 8pt; }
    h2 { border-bottom: 1px solid #aac9c2; font-size: 17pt; margin: 22pt 0 8pt; padding-bottom: 3pt; }
    h3 { font-size: 13pt; margin: 16pt 0 6pt; }
    h4 { font-size: 11pt; margin: 12pt 0 5pt; }
    p, li { orphans: 3; widows: 3; }
    a { color: #176b61; text-decoration: none; }
    code { background: #eef3f2; border-radius: 3px; font: 9pt ui-monospace, SFMono-Regular, Menlo, monospace; padding: 1px 3px; }
    pre { background: #eef3f2; border-left: 3px solid #3a8d7d; break-inside: avoid-page; overflow-wrap: anywhere; padding: 8pt; white-space: pre-wrap; }
    pre code { padding: 0; }
    table { border-collapse: collapse; font-size: 9pt; margin: 10pt 0; width: 100%; }
    th, td { border: 1px solid #b8c8c5; padding: 5pt; text-align: left; vertical-align: top; }
    th { background: #e4efec; }
    img { display: block; height: auto; margin: 8pt auto; max-height: 8.15in; max-width: 100%; object-fit: contain; }
    blockquote { border-left: 3px solid #7aa99f; color: #40514e; margin-left: 0; padding-left: 10pt; }
    hr { border: 0; border-top: 1px solid #aac9c2; }
    .pdf-keep-together { break-inside: avoid-page; page-break-inside: avoid; }
  </style>
</head>
<body>${body}</body>
</html>`;

await writeFile(temporaryHtml, html);
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  await page.goto(pathToFileURL(temporaryHtml).href, { waitUntil: "networkidle" });
  await page.emulateMedia({ media: "print" });
  await page.pdf({
    path: temporaryPdf,
    format: "Letter",
    printBackground: true,
    displayHeaderFooter: true,
    headerTemplate: "<span></span>",
    footerTemplate: '<div style="font-size:8px;color:#60706d;text-align:center;width:100%">Sensorius User Guide &nbsp;·&nbsp; <span class="pageNumber"></span> / <span class="totalPages"></span></div>',
    margin: { top: "0.65in", right: "0.62in", bottom: "0.7in", left: "0.62in" },
  });
  await rename(temporaryPdf, outputPath);
} finally {
  await browser.close();
  await rm(temporaryHtml, { force: true });
  await rm(temporaryPdf, { force: true });
}

console.log(`Wrote ${path.relative(root, outputPath)}`);
