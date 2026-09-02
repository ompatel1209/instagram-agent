"""Pillow renderer: colorful gradient graphics for feed posts and Stories.

Outputs JPEG (the Instagram Content Publishing API accepts JPEG only for
image posts) at 1080x1350 (feed, 4:5) and 1080x1920 (story, 9:16).
"""
import pathlib

from PIL import Image, ImageDraw, ImageFont

from .config import FONTS_DIR

FEED_SIZE = (1080, 1350)
STORY_SIZE = (1080, 1920)

FONT_FILES = {
    "regular": "Poppins-Regular.ttf",
    "medium": "Poppins-Medium.ttf",
    "semibold": "Poppins-SemiBold.ttf",
    "bold": "Poppins-Bold.ttf",
    "extrabold": "Poppins-ExtraBold.ttf",
}


def _font(weight: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONTS_DIR / FONT_FILES[weight]), size)


def _lerp(a, b, t: float) -> float:
    return a + (b - a) * t


def gradient(size: tuple[int, int], colors: list[tuple[int, int, int]]) -> Image.Image:
    """Vertical multi-stop linear gradient."""
    w, h = size
    img = Image.new("RGB", (w, h))
    px = img.load()
    stops = len(colors) - 1
    for y in range(h):
        t = y / (h - 1)
        seg = min(int(t * stops), stops - 1)
        local = t * stops - seg
        c1, c2 = colors[seg], colors[seg + 1]
        px_row = (
            (int(_lerp(c1[0], c2[0], local)),
             int(_lerp(c1[1], c2[1], local)),
             int(_lerp(c1[2], c2[2], local))),
        )
        for x in range(w):
            px[x, y] = px_row[0]
    return img


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> list[str]:
    """Greedy word-wrap against the font's measured widths."""
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not line:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def draw_wrapped(draw, text: str, font, xy_center: tuple[int, int],
                 max_width: int, fill, line_gap: int = 14) -> None:
    """Draw wrapped, horizontally centered text; returns nothing."""
    x, y = xy_center
    lines = wrap_text(draw, text, font, max_width)
    total_h = 0
    line_h = []
    for l in lines:
        bbox = draw.textbbox((0, 0), l, font=font)
        h = bbox[3] - bbox[1]
        line_h.append(h)
        total_h += h + line_gap
    total_h -= line_gap
    cursor = y - total_h // 2
    for l, h in zip(lines, line_h):
        w = draw.textlength(l, font=font)
        draw.text((x - w / 2, cursor), l, font=font, fill=fill)
        cursor += h + line_gap


# --- Feed post -------------------------------------------------------------

def render_feed(quote: dict, palette: list[list[int]], handle: str,
                out_path: pathlib.Path) -> pathlib.Path:
    size = FEED_SIZE
    img = gradient(size, [tuple(c) for c in palette])
    draw = ImageDraw.Draw(img, "RGBA")

    # Decorative translucent circles for depth.
    draw.ellipse((size[0] - 340, -160, size[0] + 120, 300),
                 fill=(255, 255, 255, 22))
    draw.ellipse((-180, size[1] - 320, 260, size[1] + 100),
                 fill=(255, 255, 255, 20))

    quote_font = _font("bold", 72)
    author_font = _font("medium", 40)
    handle_font = _font("semibold", 34)

    # Quote mark accent.
    mark_font = _font("extrabold", 200)
    draw_wrapped(draw, "“", mark_font, (size[0] // 2, 220), 900,
                 (255, 255, 255, 90), line_gap=0)

    # Main quote text.
    draw_wrapped(draw, quote["text"], quote_font, (size[0] // 2, size[1] // 2),
                 860, (255, 255, 255, 255), line_gap=22)

    # Author line with accent bar.
    author = f"— {quote['author']}" if quote["author"].lower() != "unknown" else ""
    if author:
        draw_wrapped(draw, author, author_font, (size[0] // 2, size[1] // 2 + 260),
                     700, (255, 255, 255, 235), line_gap=8)

    # Handle watermark.
    draw_wrapped(draw, handle, handle_font, (size[0] // 2, size[1] - 120),
                 600, (255, 255, 255, 200), line_gap=0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path


# --- Story -----------------------------------------------------------------

def render_story(tip: dict, palette: list[list[int]], handle: str,
                 out_path: pathlib.Path) -> pathlib.Path:
    size = STORY_SIZE
    img = gradient(size, [tuple(c) for c in palette])
    draw = ImageDraw.Draw(img, "RGBA")

    # Soft decorative blobs.
    draw.ellipse((size[0] - 300, 200, size[0] + 150, 650),
                 fill=(255, 255, 255, 20))
    draw.ellipse((-200, size[1] - 700, 300, size[1] - 200),
                 fill=(255, 255, 255, 18))

    label_font = _font("semibold", 44)
    title_font = _font("extrabold", 96)
    body_font = _font("medium", 52)
    handle_font = _font("semibold", 36)

    # Top label pill.
    label = "DAILY TIP"
    lw = draw.textlength(label, font=label_font)
    pill_w, pill_h = lw + 80, 96
    cx = size[0] // 2
    draw.rounded_rectangle(
        (cx - pill_w // 2, 300, cx + pill_w // 2, 300 + pill_h),
        radius=pill_h // 2, fill=(255, 255, 255, 46),
        outline=(255, 255, 255, 130), width=3,
    )
    draw.text((cx - lw / 2, 300 + (pill_h - 44) / 2), label,
              font=label_font, fill=(255, 255, 255, 255))

    # Tip title.
    draw_wrapped(draw, tip["title"].upper(), title_font,
                 (size[0] // 2, 640), 900, (255, 255, 255, 255), line_gap=24)

    # Tip body.
    draw_wrapped(draw, tip["text"], body_font,
                 (size[0] // 2, 1150), 880, (255, 255, 255, 240), line_gap=20)

    # Handle watermark.
    draw_wrapped(draw, handle, handle_font, (size[0] // 2, size[1] - 260),
                 600, (255, 255, 255, 200), line_gap=0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.convert("RGB").save(out_path, "JPEG", quality=92)
    return out_path
