# Contributing

## Commit messages and pull request titles

Every commit message and every pull request title uses the same short format:

```
type(scope): what changed, in a few words
```

- **type** says what kind of change it is:
  - `feat`: something new that users can see (a new report, a new page)
  - `fix`: something that was broken now works
  - `perf`: the same thing, but faster or cheaper
  - `docs`: documentation only
  - `refactor`: code reorganised, nothing changes for users
  - `test`: tests only
  - `build`: packages, Python version, project setup
  - `ci`: GitHub Actions workflows
  - `chore`: housekeeping that fits nowhere else
  - `revert`: undoes an earlier change
- **scope** says which part of the project it touches. Use one of:
  `scanner`, `brief`, `reports`, `desk`, `site`, `worker`, `data`, `repo`,
  `deps`, `ci`, `main`.
- **what changed**: lower-case start, no full stop at the end. The whole first
  line must fit in 100 characters. Add more detail on the lines below it if
  needed, after an empty line.

Six examples:

```
feat(reports): add an earnings preview page to the v5 report
fix(scanner): handle N/A float values from Claude
perf(data): download only the R2 files a job needs
docs(site): explain the Options Flow tab on the help page
ci(repo): run the tests on every pull request
build(deps): update yfinance to 1.8
```

Why it matters:

- Pull requests are **squash-merged**: the whole pull request becomes one
  commit on `master`, and its message is the **pull request title**. A check
  (`.github/workflows/pr-title.yml`) blocks titles that do not follow the
  format.
- The changelog is written automatically from these titles (release-please).
  `feat`, `fix`, `perf` and `revert` appear in it; the other types are hidden.
- On your own computer, the git hooks (installed with `bun install`) check
  each commit message before it is saved. `updated stuff` is rejected;
  `fix(scanner): handle N/A float values` is accepted.

## Before you push

The hooks run these for you; this is what they do:

```bash
uv run ruff check --fix .        # lint and auto-fix
uv run ruff format .             # format
uv run pyrefly check             # type check
uv run pytest -n auto -m "not network and not data"   # tests, as CI runs them
```

Pull request checks (`.github/workflows/ci.yml`) run the same commands and
must pass.
