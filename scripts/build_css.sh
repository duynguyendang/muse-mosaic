#!/usr/bin/env bash
# Rebuild web/studio.css (Tailwind v3) so the cockpit has no runtime CDN
# dependency for styling. Output is committed; run this after editing
# web/index.html, web/studio.js or web/tailwind.config.js.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
cd "$work"
npm init -y >/dev/null
npm install --silent --no-audit --no-fund \
  tailwindcss@3.4.17 @tailwindcss/forms@0.5.10 @tailwindcss/container-queries@0.1.1
printf '@tailwind base;\n@tailwind components;\n@tailwind utilities;\n' > in.css
# Resolve plugins from the temp install while reading config/content from web/.
NODE_PATH="$work/node_modules" "$work/node_modules/.bin/tailwindcss" \
  -c "$root/web/tailwind.config.js" -i in.css -o "$root/web/studio.css" --minify
echo "wrote web/studio.css ($(wc -c < "$root/web/studio.css") bytes)"
