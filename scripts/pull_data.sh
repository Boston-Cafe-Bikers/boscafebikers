#!/usr/bin/env bash
# Pull the generated site data out of the `data` branch into site/.
#
#     scripts/pull_data.sh [remote]      # remote defaults to origin
#
# events.json, events-past.json, cafe-points.json, rides.ics, maps/, posters/,
# instagram-posts.json and instagram/ are not committed on master or dev — the
# only copy lives on the orphan `data` branch, laid out exactly as it appears
# inside site/. The sync bot writes it there and pages.yml copies it into the
# published trees; this is how a local checkout gets real rides and the latest
# gallery posts.
#
# Safe to re-run: it overwrites only those generated paths. The
# data branch's own README.md is deliberately *not* extracted — it documents
# the branch, it is not part of the site.
set -euo pipefail

remote="${1:-origin}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
site="$repo_root/site"

paths=(events.json events-past.json cafe-points.json rides.ics maps posters instagram-posts.json instagram)

echo "pull_data: fetching $remote/data"
git -C "$repo_root" fetch --quiet "$remote" data

# Ask only for what the branch actually carries: a fresh data branch with no
# route maps or mirrored photos yet has no maps/ or posters/ entry, and git
# archive fails on a pathspec that matches nothing.
present=()
for path in "${paths[@]}"; do
  if git -C "$repo_root" cat-file -e "$remote/data:$path" 2>/dev/null; then
    present+=("$path")
  else
    echo "pull_data: $remote/data carries no $path — skipping"
  fi
done

if [ "${#present[@]}" -eq 0 ]; then
  echo "pull_data: nothing to extract from $remote/data" >&2
  exit 1
fi

mkdir -p "$site"
git -C "$repo_root" archive "$remote/data" "${present[@]}" | tar -x -C "$site"

echo "pull_data: extracted from $remote/data at $(git -C "$repo_root" rev-parse --short "$remote/data") into site/:"
for path in "${present[@]}"; do
  echo "  site/$path"
done
