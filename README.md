# font-bitmap

Cloudflare Python Worker version of `font_bitmap_demo.py`. Same rendering
pipeline (Pillow rasterize -> pack 1bpp -> serve as json/ascii/png/bin),
running on Cloudflare's edge instead of a local `ThreadingHTTPServer` —
now supporting multiple fonts, selected per request.

## Setup

1. Install [`uv`](https://docs.astral.sh/uv/getting-started/installation/).
2. Drop one or more font files into `./assets/fonts/` (any
  `.ttf`/`.ttc`/`.otf`). The default is `SF-Pro.ttf` — set via
   `DEFAULT_FONT` in `src/entry.py`, or override per-request with
   `?font=<filename>`.

Adding a new font is just adding a file to `assets/fonts/` — no code
change, no `entry.py` redeploy needed for it to become fetchable.

## Run locally

```bash
uv run pywrangler dev
```

Then, e.g.:

```
http://localhost:8787/health
http://localhost:8787/render?text=25%C2%B0C&size=48&format=ascii
http://localhost:8787/render?text=Hello&size=32&format=png&font=SF-Pro.ttf
```

## Deploy

```bash
uv run pywrangler deploy
```

## What changed from the original script

- `BaseHTTPRequestHandler.do_GET` -> `class Default(WorkerEntrypoint): async def fetch(self, request)`.
- No `ThreadingHTTPServer`/listening socket — Cloudflare's edge invokes
  `fetch()` per request; the Pyodide/WASM runtime doesn't support
  `threading` or `multiprocessing` anyway.
- `--font <path>` CLI arg -> the **ASSETS binding**. Fonts live under
  `./assets/fonts/*.ttf` (declared in `wrangler.jsonc`) and are fetched by
  name at request time via `env.ASSETS.fetch(...)`, read with
  `await resp.bytes()`, and cached per isolate in `FONT_CACHE` (keyed by
  filename). This scales to many fonts without touching the code.
- Rendering, 1bpp packing, and the ASCII preview logic are byte-for-byte
  the same as the original — Pillow runs fine under Pyodide.

## Notes on the ASSETS binding (things that tripped this up)

- `env.ASSETS.fetch(url)` returns a `Response`-like object. The correct
  way to read its body as Python `bytes` is `await resp.bytes()` — *not*
  `await resp.array_buffer()` (doesn't exist on this wrapper) and not a
  manual `ArrayBuffer.to_bytes()` conversion. Cloudflare's Python Workers
  wrap fetch responses using Pyodide's `FetchResponse`, which exposes
  `async bytes() -> bytes` directly.
- Bundling a binary file *next to* `entry.py` (e.g. `src/font.ttf`, read
  via `Path(__file__).parent / "font.ttf"`) works for local dev but is
  unreliable once deployed — the deployed runtime doesn't necessarily
  preserve `src/`'s directory layout relative to `__file__`. The ASSETS
  binding is the robust way to ship non-Python files, especially more
  than one.
