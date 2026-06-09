// Verify every inline <script> in an HTML file parses (node --check).
// Used by the Pages build to gate minified output: if terser ever produces
// broken JS, the build falls back to deploying the unminified index.html.
// Exit 0 = all inline blocks parse; exit 1 = at least one failed.
const fs = require("fs");
const os = require("os");
const path = require("path");
const cp = require("child_process");

const file = process.argv[2];
if (!file) { console.error("usage: verify_inline_js.js <html-file>"); process.exit(2); }

const html = fs.readFileSync(file, "utf8");
const re = /<script(\b[^>]*)>([\s\S]*?)<\/script>/gi;
let m, i = 0, bad = 0;
while ((m = re.exec(html))) {
  const attrs = m[1] || "";
  if (/\bsrc=/.test(attrs)) continue;                       // external
  if (/type=/.test(attrs) && !/javascript|module/i.test(attrs)) continue; // json-ld etc.
  i++;
  const f = path.join(os.tmpdir(), "vij_" + i + ".js");
  fs.writeFileSync(f, m[2]);
  try { cp.execSync('node --check "' + f + '"', { stdio: "pipe" }); }
  catch (e) { bad++; console.error("inline <script> block #" + i + " failed to parse"); }
}
if (bad) { console.error(bad + " inline block(s) failed"); process.exit(1); }
console.log("inline JS OK (" + i + " blocks)");
