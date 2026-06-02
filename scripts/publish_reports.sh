#!/usr/bin/env bash
#
# publish_reports.sh — Commit newly-archived report PDFs and push to master.
#
# Race-safe: multiple workflows (scanner, momentum, ticker-lookup) can finish
# near-simultaneously and all try to push. The earlier inline version did
#   git add  ->  git pull --rebase --autostash  ->  git commit
# which stashed the staged manifest.json, pulled a conflicting manifest.json,
# then failed to pop the autostash -> "Committing is not possible / unmerged
# files" -> job failure.
#
# Correct order: commit FIRST (clean tree), then fetch+rebase with retry.
# manifest.json is the only file that can conflict (report PDFs have unique
# timestamped names) — on conflict it is simply regenerated from the PDFs.
#
# Always exits 0: a lost archive race must not fail the parent job — the next
# run re-publishes the report.

set -u

git config user.name  "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

git add docs/reports/ || true
if git diff --staged --quiet; then
  echo "No new reports to publish"
  exit 0
fi

git commit -m "Archive reports $(date -u +%Y-%m-%dT%H:%MZ)"

for attempt in 1 2 3 4 5; do
  git fetch origin master

  if ! git rebase origin/master; then
    # EVERY file under docs/reports/ is machine-generated and rewritten
    # whole each run (scanners overwrite uoa_latest / uoa_edge /
    # uoa_signals_scored / uoa_oi_history; altdata_history append-only;
    # manifest rebuilt from PDFs). On a concurrent-run conflict the
    # INCOMING (origin) copy is always at least as fresh, so resolve
    # EVERY conflicted path with --theirs.
    #
    # The old code only did --theirs for altdata_history.json, then
    # blind-`git add`'d everything else — which staged uoa_latest.json
    # et al. WITH raw <<<<<<< / ======= / >>>>>>> markers and committed
    # corrupt JSON (broke the live Options Flow tab on 2026-06-02).
    conflicted=$(git diff --name-only --diff-filter=U)
    if [ -n "$conflicted" ]; then
      echo "Conflicts on: ${conflicted} — resolving with incoming (theirs)"
      for cf in ${conflicted}; do
        git checkout --theirs "${cf}" 2>/dev/null || true
        git add "${cf}" || true
      done
    fi
    # Manifest is derived from the PDF set — rebuild after taking theirs
    # so it reflects this run's newly-added reports too.
    python -c "from report_archive import rebuild_manifest; rebuild_manifest()" || true
    git add docs/reports/
    if ! GIT_EDITOR=true git rebase --continue; then
      git rebase --abort || true
      echo "Rebase failed on attempt ${attempt}; retrying..."
      sleep $((attempt * 2))
      continue
    fi
  fi

  # SAFETY GATE: never publish a tree that still contains git conflict
  # markers. If any docs/reports file has leftover <<<<<<< / ======= /
  # >>>>>>> lines, abort this attempt and hard-reset rather than push
  # corrupt JSON to the live site.
  if grep -rlE '^(<{7}|={7}|>{7})' docs/reports/ >/dev/null 2>&1; then
    echo "::error::Conflict markers detected in docs/reports/ — refusing to push."
    git reset --hard origin/master || true
    sleep $((attempt * 2))
    continue
  fi

  if git push origin master; then
    echo "Reports published to site (attempt ${attempt})"
    exit 0
  fi

  echo "Push rejected on attempt ${attempt}; retrying..."
  sleep $((attempt * 2))
done

echo "::warning::Could not publish reports to site after 5 attempts. "\
"The next run will re-publish."
exit 0
