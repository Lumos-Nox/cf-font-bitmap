"""
Cloudflare Python Worker: dynamic bitmap font rendering, multi-font.

Same three-stage pipeline as the original standalone server
(font_bitmap_demo.py), ported from a local ThreadingHTTPServer to
Cloudflare's Pyodide-based Workers runtime:

  1. CLOUD SIDE: given ?text=...&size=...&font=..., rasterize the text
     with Pillow, using whichever font file was requested.
  2. PROTOCOL: pack the rendered bitmap to 1 bit per pixel, row-major,
     MSB-first -- unchanged from the original.
  3. RESPONSE: return the packed payload (or a PNG, ASCII preview, or a
     JSON summary) depending on ?format=.

What changed and why:
  - `class Default(WorkerEntrypoint): async def fetch(self, request)`
    replaces BaseHTTPRequestHandler.do_GET -- the standard entry point
    shape for Python Workers.
  - No ThreadingHTTPServer / listening socket: Cloudflare's edge
    terminates HTTP and invokes fetch() per request. (The WASM/Pyodide
    runtime doesn't support threading or multiprocessing anyway, so this
    isn't a loss of functionality.)
  - --font <path> CLI arg -> the ASSETS binding. Font files live under
    ./assets/fonts/*.ttf (see wrangler.jsonc) and are fetched by name at
    request time via `env.ASSETS.fetch(...)`, so adding a new font is
    just dropping a file in that folder -- no code change, no redeploy
    of entry.py. Each font's bytes are cached per isolate in FONT_CACHE
    keyed by filename (mirrors the original's module-level FONT_PATH,
    generalized to many fonts).
  - The fetch response is read with `await resp.bytes()` -- Cloudflare's
    Python Workers wrap fetch() responses using Pyodide's FetchResponse,
    which exposes `async bytes() -> bytes` directly. (Not `.arrayBuffer()`
    + a manual conversion -- `.bytes()` already gives you Python bytes.)
"""

import io
import json
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageDraw, ImageFont
from workers import Response, WorkerEntrypoint

FONT_CACHE = {}  # font filename -> bytes, cached per isolate
DEFAULT_FONT = "SF-Pro.ttf"  # must exist under ./assets/fonts/


# ---------------------------------------------------------------------------
# 1. CLOUD SIDE: text -> grayscale bitmap (unchanged from the original)
# ---------------------------------------------------------------------------
def render_text_to_bitmap(text: str, font_bytes: bytes, pixel_size: int):
    font = ImageFont.truetype(io.BytesIO(font_bytes), pixel_size)

    scratch = Image.new("L", (pixel_size * len(text) * 2, pixel_size * 2), 0)
    draw = ImageDraw.Draw(scratch)
    draw.text((pixel_size, pixel_size // 2), text, font=font, fill=255)

    bbox = scratch.getbbox()
    if bbox is None:
        raise ValueError("Nothing rendered -- font may be missing glyphs for this text")
    return scratch.crop(bbox)


# ---------------------------------------------------------------------------
# 2. PROTOCOL: grayscale -> 1bpp packed bytes (unchanged from the original)
# ---------------------------------------------------------------------------
def pack_1bpp(img: Image.Image, threshold: int = 128) -> bytes:
    w, h = img.size
    px = img.load()
    out = bytearray()
    for y in range(h):
        byte = 0
        bits_in_byte = 0
        for x in range(w):
            bit = 1 if px[x, y] >= threshold else 0
            byte = (byte << 1) | bit
            bits_in_byte += 1
            if bits_in_byte == 8:
                out.append(byte)
                byte = 0
                bits_in_byte = 0
        if bits_in_byte:
            byte <<= (8 - bits_in_byte)
            out.append(byte)
    return bytes(out)


# ---------------------------------------------------------------------------
# 3. DEVICE SIDE: unpack bytes -> ASCII preview (unchanged from the original)
# ---------------------------------------------------------------------------
def unpack_and_preview(data: bytes, w: int, h: int) -> str:
    row_bytes = (w + 7) // 8
    lines = []
    for y in range(h):
        row_chars = []
        for x in range(w):
            byte = data[y * row_bytes + x // 8]
            bit = (byte >> (7 - (x % 8))) & 1
            row_chars.append("##" if bit else "  ")
        lines.append("".join(row_chars))
    return "\n".join(lines)


async def get_font_bytes(env, name: str) -> bytes:
    """Fetch a font from the ASSETS binding once per isolate and cache it."""
    if name not in FONT_CACHE:
        resp = await env.ASSETS.fetch(f"https://assets.local/fonts/{name}")
        if resp.status != 200:
            raise RuntimeError(f"font not found: {name}")
        FONT_CACHE[name] = await resp.bytes()
    return FONT_CACHE[name]


def json_response(status: int, payload: dict) -> Response:
    return Response(
        json.dumps(payload, ensure_ascii=False),
        status=status,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)

        if url.path == "/health":
            return json_response(200, {"status": "ok", "default_font": DEFAULT_FONT})

        if url.path != "/render":
            return json_response(
                404,
                {
                    "error": "not found",
                    "try": "/render?text=25%C2%B0C&size=48&format=ascii&font=SF-Pro.ttf",
                    "formats": ["json", "ascii", "png", "bin"],
                },
            )

        qs = parse_qs(url.query)
        text = qs.get("text", [None])[0]
        if not text:
            return json_response(400, {"error": "missing required query param: text"})

        try:
            size = int(qs.get("size", ["48"])[0])
        except ValueError:
            return json_response(400, {"error": "size must be an integer"})

        fmt = qs.get("format", ["json"])[0]
        font_name = qs.get("font", [DEFAULT_FONT])[0]

        try:
            font_bytes = await get_font_bytes(self.env, font_name)
            img = render_text_to_bitmap(text, font_bytes, size)
        except (OSError, RuntimeError, ValueError) as e:
            return json_response(400, {"error": str(e)})

        packed = pack_1bpp(img)
        w, h = img.size

        if fmt == "png":
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return Response(
                buf.getvalue(),
                headers={
                    "Content-Type": "image/png",
                    "X-Bitmap-Width": str(w),
                    "X-Bitmap-Height": str(h),
                },
            )

        if fmt == "bin":
            return Response(
                bytes(packed),
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Bitmap-Width": str(w),
                    "X-Bitmap-Height": str(h),
                    "X-Bitmap-Packed-Bytes": str(len(packed)),
                },
            )

        if fmt == "ascii":
            preview = unpack_and_preview(packed, w, h)
            return Response(preview, headers={"Content-Type": "text/plain; charset=utf-8"})

        # default: format=json -- summary + inline ascii preview
        return json_response(
            200,
            {
                "text": text,
                "font": font_name,
                "pixel_size": size,
                "bitmap_width": w,
                "bitmap_height": h,
                "grayscale_bytes_naive": w * h,
                "packed_1bpp_bytes": len(packed),
                "ascii_preview": unpack_and_preview(packed, w, h),
            },
        )
