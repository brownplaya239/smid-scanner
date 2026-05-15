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
# Always exits 0: a lost archive race must not fail the parent job (the report
# has already been delivered to Discord, and the next run re-publishes).

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
    # The only file that can conflict is the generated manifest — rebuild it.
    python -c "from report_archive import rebuild_manifest; rebuild_manifest()" || true
    git add docs/reports/
    if ! GIT_EDITOR=true git rebase --continue; then
      git rebase --abort || true
      echo "Rebase failed on attempt ${attempt}; retrying..."
      sleep $((attempt * 2))
      continue
    fi
  fi

  if git push origin master; then
    echo "Reports published to site (attempt ${attempt})"
    exit 0
  fi

  echo "Push rejected on attempt ${attempt}; retrying..."
  sleep $((attempt * 2))
done

echo "::warning::Could not publish reports to site after 5 attempts. "\
"The report was still delivered to Discord; the next run will re-publish."
exit 0
