// Commit message rules, checked by the lefthook commit-msg hook.
// Format: type(scope): short summary    e.g.  fix(scanner): handle N/A float values
// Plain-English version: CONTRIBUTING.md. Keep the scope list in sync with
// .github/workflows/pr-title.yml.
// CommonJS on purpose: a root package.json with "type": "module" would break
// scripts/verify_inline_js.js, which the Pages build runs with node.
module.exports = {
  extends: ["@commitlint/config-conventional"],
  rules: {
    "scope-enum": [
      2,
      "always",
      ["scanner", "brief", "reports", "desk", "site", "worker", "data", "repo", "deps", "ci", "main"],
    ],
    "header-max-length": [2, "always", 100],
  },
};
