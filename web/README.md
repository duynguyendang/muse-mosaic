# web/ — Muse Mosaic frontend (separate deploy)

Static, no build step: `index.html` + `app.js`. UX source: `docs/internal/muse-mosaic-ux-design.2.1.md`
(Sec 4.13 Autonomous Loop). Holds no API key, calls no model directly, derives nothing —
every value renders from the mosaic-api ledger with proof (rule, policy, taxonomy versions).

## Local / demo

mosaic-api still serves this folder at `/` for the recorded demo:

```bash
cd mosaic-api
USE_MOCK_MUSE=true uvicorn main:app --reload
# open http://127.0.0.1:8000
```

## Separate frontend deploy

Upload this folder (both files, same directory) to any static host. Point it at the API:

```html
<script>window.MOSAIC_API_BASE = "https://api.example.com"</script>
<script src="./app.js"></script>
```

Same-origin default (`MOSAIC_API_BASE` unset) keeps working. The API host must allow the
frontend origin (CORS) — configure that on the API side, not here.

Stack (per CSD, applied as-is): vanilla JS + hand-written CSS + inline SVG, system
sans-serif (UX §6.2) — deliberately **no** Tailwind Play CDN, Google Fonts, Material
Symbols, Cytoscape, D3, or Alpine. Nothing loads from a CDN, so a network loss during
video recording cannot strip styling; hand-drawn SVG keeps the F1→F5 hop order
deterministic. Revisit Cytoscape only if hand SVG becomes unmaintainable (zoom/pan).
