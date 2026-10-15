// Runs one in-page browser probe in headless Chromium:
//   node browser_probe_runner.cjs <base url> <page path> <probe module> <export name>
// The probe is an async function exported by an .mjs file that runs inside the
// page and returns a list of the cases it checked; it throws on a failure.
// Prints the list as JSON. Shared by the browser checks in tests/browser_probe.py.
const {pathToFileURL} = require("node:url");
const path = require("node:path");
const {chromium} = require("playwright");

(async () => {
  const [baseUrl, pagePath, probeFile, exportName] = process.argv.slice(2);
  const probe = (await import(pathToFileURL(path.resolve(__dirname, probeFile))))[exportName];
  if (typeof probe !== "function") throw new Error(`${probeFile} exports no function ${exportName}`);
  const browser = await chromium.launch({headless: true});
  try {
    const page = await browser.newPage();
    await page.goto(baseUrl + pagePath);
    const results = await page.evaluate(`(${probe.toString()})()`);
    console.log(JSON.stringify(results));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
