#!/usr/bin/env bash
# Promote a rendered results page into docs/ for GitHub Pages.
#
# Refuses to publish without measured data: the published page is what a reader
# sees, so it must never show anything that was not measured.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f bench/results/sweep.json ]; then
  echo "No measured sweep at bench/results/sweep.json"
  echo "Run ./bench/run_benchmark.sh first."
  exit 1
fi

python3 bench/render_page.py

mkdir -p docs
cp bench/results/index.html docs/index.html
cp bench/results/sweep.json docs/
[ -f bench/results/k8s.json ] && cp bench/results/k8s.json docs/ || true

cat > docs/.gitignore <<'INNER'
# Intentionally empty: files in docs/ are published artefacts and ARE committed,
# overriding the bench/results ignore rules in the root .gitignore.
INNER

echo "Published to docs/:"
ls -1 docs/
echo
echo "Next: git add -f docs/ && git commit -m 'Publish results page' && git push"
echo "Then: Settings -> Pages -> Source: GitHub Actions"
