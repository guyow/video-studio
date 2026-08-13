"""liitt layout compositor — deterministic Pillow renderer for brand posters.

Reuses the text-rendering helper LOGIC (text width with tracking, glyph-by-glyph
draw, binary-size descent for headline fit, wrap) from video-studio's
compositor.py, but is built for the brand folder layout used by the ComfyUI
pipeline (liitt_layout_templates-revised.json + brand_guides.json).

The diffusion model paints ONLY background imagery; this module guarantees that
brand text and the liitt wordmark are pixel-identical on every render — they
are drawn here in pure Pillow from the real fonts + hex tokens.

Pure stdlib + Pillow only. No torch / folder_paths imports (3.11+3.13 compat).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
CHOSEN_LAYOUT_RE = re.compile(r"\[CHOSEN LAYOUT\][^\n]*\n\s*(?P<body>[^\n]+)")
LAYOUT_SPEC_RE = re.compile(
    r"(?P<template>[\w-]+)\s*\|\s*logo_position\s*:\s*(?P<position>\w+)"
)

FALLBACK_HEADLINE_HEX = "#C9C3DA"
FALLBACK_BODY_HEX = "#8E899F"
FALLBACK_BODY_BRIGHT_HEX = "#FAFAF7"
FALLBACK_SUBTITLE_HEX = "#C9C3DA"
DEFAULT_BG_HEX = "#0A0A0A"
DEFAULT_SHADOW_COLOR = (11, 10, 28)
DEFAULT_SHADOW_ALPHA = 102
DEFAULT_SHADOW_OFFSET = (0, 4)
DEFAULT_SHADOW_BLUR = 8
# Light glow for dark backgrounds (liitt's dark-first aesthetic)
GLOW_COLOR = (244, 241, 232)   # #F4F1E8 — brand "text_on_dark" soft white
GLOW_ALPHA = 60
GLOW_OFFSET = (0, 0)
GLOW_BLUR = 12
LUMINANCE_THRESHOLD = 128
LOGOMARK_MIN_HEIGHT = 120
LOGOMARK_MAX_HEIGHT = 150
# Vertical gap kept between the wordmark and text zones when they collide
# (as a fraction of canvas height).
LOGO_CLEARANCE_RATIO = 0.03

# Map family + weight → static .ttf filename.
FONT_FILES: dict[str, dict[str, str]] = {
    "Bricolage Grotesque": {
        "bold": "BricolageGrotesque-Bold.ttf",
        "regular": "BricolageGrotesque-Regular.ttf",
        "medium": "BricolageGrotesque-Medium.ttf",
        "semibold": "BricolageGrotesque-SemiBold.ttf",
        "extrabold": "BricolageGrotesque-ExtraBold.ttf",
        "light": "BricolageGrotesque-Light.ttf",
    },
    "Hanken Grotesk": {
        "medium": "HankenGrotesk-Medium.ttf",
        "regular": "HankenGrotesk-Regular.ttf",
        "bold": "HankenGrotesk-Bold.ttf",
        "semibold": "HankenGrotesk-SemiBold.ttf",
        "light": "HankenGrotesk-Light.ttf",
    },
    "Newsreader": {
        "regular": "Newsreader_14pt-Regular.ttf",
        "light": "Newsreader_14pt-Light.ttf",
        "medium": "Newsreader_14pt-Medium.ttf",
        "bold": "Newsreader_14pt-Bold.ttf",
    },
}

FAMILY_DIR = {
    "Bricolage Grotesque": "Bricolage_Grotesque",
    "Hanken Grotesk": "Hanken_Grotesk",
    "Newsreader": "Newsreader",
}


class CompositorError(Exception):
    """Raised for hard-fail conditions (missing template, malformed input)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hex_rgb(value, default=(0, 0, 0)):
    """Resolve a "#RRGGBB" string to a (r,g,b) tuple, falling back to default."""
    if not value or not isinstance(value, str):
        return default
    v = value.strip()
    if not HEX_RE.match(v):
        return default
    v = v.lstrip("#")
    return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))


def _box_px(box, W, H):
    """Convert relative [x0,y0,x1,y1] (0..1) to absolute pixel coords."""
    if not box or len(box) != 4:
        return (0, 0, W, H)
    x0, y0, x1, y1 = box
    return (int(x0 * W), int(y0 * H), int(x1 * W), int(y1 * H))


def _overlaps(a, b) -> bool:
    """True when two pixel boxes intersect on BOTH the x and y axes."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _shift_box(box, dy: int):
    """Translate a pixel box vertically by dy (keeps width/height)."""
    x0, y0, x1, y1 = box
    return (x0, y0 + dy, x1, y1 + dy)


def _avoid_logo_overlap(
    logo_box,
    logo_is_top: bool,
    headline_box,
    subhead_box,
    body_box,
    H: int,
):
    """Nudge text zones so the pasted wordmark never covers text.

    A TOP logo collides with the headline: the whole text stack (headline,
    subheadline, body) is moved DOWN together (preserving relative spacing) so
    the headline starts below the logo. A BOTTOM logo collides with the
    body/subheadline: those boxes are moved UP so they end above the logo.
    Boxes that don't overlap the logo (horizontally or vertically) are left
    untouched. Returns the possibly-adjusted (headline, subheadline, body).
    """
    margin = int(LOGO_CLEARANCE_RATIO * H)
    if logo_is_top:
        if _overlaps(logo_box, headline_box):
            dy = (logo_box[3] + margin) - headline_box[1]
            if dy > 0:
                headline_box = _shift_box(headline_box, dy)
                subhead_box = _shift_box(subhead_box, dy)
                if body_box:
                    body_box = _shift_box(body_box, dy)
    else:
        if body_box and _overlaps(logo_box, body_box):
            dy = (logo_box[1] - margin) - body_box[3]
            if dy < 0:
                body_box = _shift_box(body_box, dy)
        if _overlaps(logo_box, subhead_box):
            dy = (logo_box[1] - margin) - subhead_box[3]
            if dy < 0:
                subhead_box = _shift_box(subhead_box, dy)
    return headline_box, subhead_box, body_box


def _normalize_weight(weight: str) -> str:
    if not weight:
        return "regular"
    return weight.lower().replace(" ", "").replace("-", "")


def _load_font(font_path: Path, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(font_path), size)


def _text_w(draw: ImageDraw.ImageDraw, text: str, font, tracking: float = 0.0) -> float:
    """Total advance width of text with optional per-glyph tracking."""
    if tracking:
        return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(
            0, len(text) - 1
        )
    return draw.textlength(text, font=font)


def _wrap_text(text: str, font, max_w: float) -> list[str]:
    """Greedy word-wrap to fit max_w using the given font."""
    if not text:
        return []
    words = text.split()
    if not words:
        return []
    scratch = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(scratch)
    lines: list[str] = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if d.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_headline(
    text: str,
    font_path: Path,
    max_w: int,
    max_h: int,
    start_px: int,
    min_px: int = 40,
    lh: float = 1.06,
):
    """Binary descent — shrink the font until the wrapped headline fits."""
    size = start_px
    scratch = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(scratch)
    while size >= min_px:
        font = _load_font(font_path, size)
        lines = _wrap_text(text, font, max_w)
        total = len(lines) * size * lh
        widths_ok = all(d.textlength(line, font=font) <= max_w for line in lines)
        if total <= max_h and widths_ok:
            return font, lines, size
        size -= 4
    font = _load_font(font_path, min_px)
    return font, _wrap_text(text, font, max_w), min_px


# ---------------------------------------------------------------------------
# text / shadow ops
# ---------------------------------------------------------------------------


def _emit_lines(
    text_lines: Iterable[str],
    font,
    fill,
    box_px,
    align: str = "left",
    tracking: float = 0.0,
    lh: float = 1.06,
) -> list[tuple]:
    """Position each line inside the box. Returns [(text, x, y, w, font, fill), ...]."""
    if not text_lines:
        return []
    x0, y0, x1, y1 = box_px
    box_w = max(1, x1 - x0)
    box_h = max(1, y1 - y0)
    line_height = max(1, int(font.size * lh))
    lines = list(text_lines)
    total_h = len(lines) * line_height
    # top-anchor with vertical centering for short stacks
    y = y0 + max(0, (box_h - total_h) // 2)
    scratch = Image.new("RGB", (1, 1))
    d = ImageDraw.Draw(scratch)
    ops = []
    for line in lines:
        w = _text_w(d, line, font, tracking)
        if align == "center":
            x = x0 + max(0, (box_w - w) // 2)
        elif align == "right":
            x = x1 - w
        else:
            x = x0
        ops.append((line, x, y, w, font, fill))
        y += line_height
    return ops


def _is_dark(image: Image.Image) -> bool:
    """Mean luminance of the image — True if the canvas is dark overall."""
    gray = image.convert("L")
    hist = gray.histogram()
    total = sum(hist)
    if total <= 0:
        return True
    mean = sum(i * c for i, c in enumerate(hist)) / total
    return mean < LUMINANCE_THRESHOLD


def _render_with_shadow(
    canvas: Image.Image,
    ops,
    shadow_color=DEFAULT_SHADOW_COLOR,
    shadow_alpha: int = DEFAULT_SHADOW_ALPHA,
    offset=DEFAULT_SHADOW_OFFSET,
    blur: int = DEFAULT_SHADOW_BLUR,
) -> Image.Image:
    """Composite text ops with a depth effect underneath.

    Dark backgrounds get a soft LIGHT GLOW (halo) — a dark drop shadow would be
    invisible. Light backgrounds get a classic dark drop shadow. The choice is
    made from the canvas mean luminance.
    """
    if not ops:
        return canvas
    W, H = canvas.size
    rgba = canvas.convert("RGBA")
    effect_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fg_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ed = ImageDraw.Draw(effect_layer)
    fd = ImageDraw.Draw(fg_layer)

    if _is_dark(canvas):
        eff_color = GLOW_COLOR
        eff_alpha = GLOW_ALPHA
        off = GLOW_OFFSET
        blur_r = GLOW_BLUR
    else:
        eff_color = shadow_color
        eff_alpha = shadow_alpha
        off = offset
        blur_r = blur

    for text, x, y, _w, font, fill in ops:
        ed.text((x + off[0], y + off[1]), text, font=font, fill=(*eff_color, eff_alpha))
        if isinstance(fill, tuple) and len(fill) == 4:
            fd.text((x, y), text, font=font, fill=fill)
        else:
            fd.text((x, y), text, font=font, fill=(*fill, 255))
    if blur_r > 0:
        effect_layer = effect_layer.filter(ImageFilter.GaussianBlur(blur_r))
    rgba = Image.alpha_composite(rgba, effect_layer)
    rgba = Image.alpha_composite(rgba, fg_layer)
    return rgba


# ---------------------------------------------------------------------------
# logo + scrim
# ---------------------------------------------------------------------------


def _fit_logo(
    img: Image.Image,
    box_px,
    min_h: int = LOGOMARK_MIN_HEIGHT,
    max_h: int = LOGOMARK_MAX_HEIGHT,
    canvas_size=None,
):
    """Resize logo to fit inside box_px while preserving aspect ratio.

    If the requested box is shorter than min_h, scale it outward preserving
    the box center. If the box is taller than max_h, scale it inward preserving
    the box center. When canvas_size=(W, H) is supplied the final box is
    clamped to the canvas so a top logo can never render off-canvas.
    Returns (resized_rgba, (paste_x, paste_y)).
    """
    x0, y0, x1, y1 = box_px
    box_w = x1 - x0
    box_h = y1 - y0
    iw, ih = img.size
    if box_h < min_h and box_h > 0:
        scale = min_h / box_h
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        box_w = max(min_h, int(box_w * scale))
        box_h = max(min_h, int(box_h * scale))
        x0 = int(cx - box_w / 2)
        y0 = int(cy - box_h / 2)
        x1 = x0 + box_w
        y1 = y0 + box_h
    if box_h > max_h and box_h > 0:
        scale = max_h / box_h
        cx = (x0 + x1) / 2
        cy = (y0 + y1) / 2
        box_w = max(1, int(box_w * scale))
        box_h = max(1, int(box_h * scale))
        x0 = int(cx - box_w / 2)
        y0 = int(cy - box_h / 2)
        x1 = x0 + box_w
        y1 = y0 + box_h
    # Clamp the (possibly outward-scaled) box to the canvas so the paste stays
    # on-screen — top logos scaled around their center can push y0 negative.
    if canvas_size:
        W, H = canvas_size
        x0 = max(0, x0)
        y0 = max(0, y0)
        x1 = min(W, x1)
        y1 = min(H, y1)
        box_w = x1 - x0
        box_h = y1 - y0
        if box_w <= 0 or box_h <= 0:
            return img, (0, 0)
    if iw <= 0 or ih <= 0:
        return img, (x0, y0)
    scale = min(box_w / iw, box_h / ih)
    new_w = max(1, int(iw * scale))
    new_h = max(1, int(ih * scale))
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    paste_x = x0 + (box_w - new_w) // 2
    paste_y = y0 + (box_h - new_h) // 2
    return resized, (paste_x, paste_y)


def _light_scrim(size, boxes, alpha: int = 80, blur: int = 24) -> Image.Image:
    """Soft-edged scrim over each text/logo zone.

    Draws the zone rectangles then blurs them so the edges fade out instead of
    showing a hard box. Only used when scrim=True.
    """
    W, H = size
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    if not boxes:
        return scrim
    d = ImageDraw.Draw(scrim)
    for box in boxes:
        if not box or len(box) != 4:
            continue
        d.rectangle(box, fill=(0, 0, 0, alpha))
    if blur > 0:
        scrim = scrim.filter(ImageFilter.GaussianBlur(blur))
    return scrim


# ---------------------------------------------------------------------------
# brand / layout loaders
# ---------------------------------------------------------------------------


class BrandAssets:
    """Resolves font files + wordmark path under brand_dir."""

    def __init__(self, brand_dir):
        self.brand_dir = Path(brand_dir)
        if not self.brand_dir.is_dir():
            raise CompositorError(f"brand_dir not found: {self.brand_dir}")

    def font_path(self, family: str, weight: str) -> Path:
        folder = FAMILY_DIR.get(family, "Bricolage_Grotesque")
        family_table = FONT_FILES.get(family, FONT_FILES["Bricolage Grotesque"])
        wk = _normalize_weight(weight)
        fname = (
            family_table.get(wk)
            or family_table.get("regular")
            or FONT_FILES["Bricolage Grotesque"]["regular"]
        )
        p = self.brand_dir / "fonts" / folder / "static" / fname
        if p.is_file():
            return p
        fallback = (
            self.brand_dir
            / "fonts"
            / "Bricolage_Grotesque"
            / "static"
            / FONT_FILES["Bricolage Grotesque"]["regular"]
        )
        if fallback.is_file():
            return fallback
        raise CompositorError(
            f"font file not found for family={family!r} weight={weight!r} "
            f"(resolved path={p})"
        )

    def wordmark_path(self) -> Path:
        return self.brand_dir / "brand-kit" / "01-liit-wordmark-brand-logo.png"

    def bg_color(self) -> tuple:
        return _hex_rgb(DEFAULT_BG_HEX, default=(10, 10, 10))


class LayoutTemplates:
    """Reads liitt_layout_templates-revised.json — templates + canvas presets + logo_positions."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            raise CompositorError(f"layout_json_path not found: {self.path}")
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise CompositorError(f"layout JSON invalid: {e}") from e
        self.templates = self.data.get("templates", {})
        if not self.templates:
            raise CompositorError(f"layout JSON has no 'templates' dict: {self.path}")
        self.canvas_presets = self.data.get("canvas_presets", {})
        if not self.canvas_presets:
            raise CompositorError(
                f"layout JSON has no 'canvas_presets' dict: {self.path}"
            )
        self.logo_positions = self.data.get("logo_positions", {})
        if not self.logo_positions:
            raise CompositorError(
                f"layout JSON has no 'logo_positions' dict: {self.path}"
            )

    def canvas_size(self, preset: str) -> tuple:
        if preset not in self.canvas_presets:
            raise CompositorError(
                f"invalid canvas_preset: {preset!r}. "
                f"Available: {list(self.canvas_presets.keys())}"
            )
        p = self.canvas_presets[preset]
        return (int(p["width"]), int(p["height"]))

    def template(self, template_id: str) -> dict:
        if template_id not in self.templates:
            raise CompositorError(
                f"template {template_id!r} not found. Available: "
                f"{list(self.templates.keys())}"
            )
        return self.templates[template_id]

    def logo_box(self, position: str) -> list:
        if position not in self.logo_positions:
            raise CompositorError(
                f"logo_position {position!r} not found. Available: "
                f"{list(self.logo_positions.keys())}"
            )
        return list(self.logo_positions[position])


# ---------------------------------------------------------------------------
# input parsers
# ---------------------------------------------------------------------------


def parse_chosen_layout(spec: str) -> tuple:
    """Return (template_id, logo_position) from UGCVisionPlanner output.

    Returns (None, None) if no [CHOSEN LAYOUT] section is found.
    """
    if not spec or not spec.strip():
        return (None, None)
    m = CHOSEN_LAYOUT_RE.search(spec)
    if not m:
        return (None, None)
    body = m.group("body").strip()
    m2 = LAYOUT_SPEC_RE.search(body)
    if not m2:
        return (None, None)
    return (m2.group("template").strip(), m2.group("position").strip())


def parse_copy_elements(text: str) -> list[dict]:
    """Parse the JSON-lines [COPY_ELEMENTS_MACHINE] section into dicts.

    Skips lines that fail to parse as JSON.
    """
    elements: list[dict] = []
    if not text or not text.strip():
        return elements
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            elements.append(obj)
    return elements


# ---------------------------------------------------------------------------
# main render
# ---------------------------------------------------------------------------


def render_layout(
    bg_image,
    chosen_layout: str,
    copy_elements: str,
    layout_json_path,
    canvas_preset: str = "ig_feed",
    brand_dir=None,
    scrim: bool = False,
) -> Image.Image:
    """Composite one deliverable. Returns RGB PIL Image.

    Args:
        bg_image: PIL Image (any mode) — cover-fit, center-cropped onto canvas.
        chosen_layout: UGCVisionPlanner output text containing [CHOSEN LAYOUT].
        copy_elements: JSON-lines string from [COPY_ELEMENTS_MACHINE] section.
        layout_json_path: path to liitt_layout_templates-revised.json.
        canvas_preset: "ig_feed" (1080x1350) or "tiktok" (1080x1920).
        brand_dir: path to brand/ root (fonts/, brand-kit/, etc.).
        scrim: when True, paint a light scrim under text/logo zones (default OFF).

    Raises:
        CompositorError: missing layout JSON, no chosen_layout, invalid preset,
            template not found, missing wordmark (warns but doesn't crash).
    """
    brand = BrandAssets(brand_dir)
    templates = LayoutTemplates(layout_json_path)
    W, H = templates.canvas_size(canvas_preset)

    template_id, logo_position = parse_chosen_layout(chosen_layout)
    if template_id is None:
        raise CompositorError(
            "No valid [CHOSEN LAYOUT] in chosen_layout — MODE A unsupported. "
            "Run UGCVisionPlanner first (MODE B / NEW_GENERATION) so the planner "
            "can emit a chosen layout line."
        )
    template = templates.template(template_id)
    if logo_position is None:
        logo_position = template.get("logo_position_default", "top_center")

    # --- canvas + bg cover-fit center-crop
    canvas = Image.new("RGB", (W, H), brand.bg_color())
    if bg_image is not None:
        bg = bg_image.convert("RGB")
        scale = max(W / max(1, bg.width), H / max(1, bg.height))
        new_w = int(bg.width * scale) + 1
        new_h = int(bg.height * scale) + 1
        bg = bg.resize((new_w, new_h), Image.LANCZOS)
        canvas.paste(bg, ((W - new_w) // 2, (H - new_h) // 2))
    canvas = canvas.convert("RGBA")

    # --- zone geometry
    headline_zone = template.get("headline", {})
    subhead_zone = template.get("subheadline", {})
    body_zone_spec = template.get("body", {}) or {}
    body_supported = body_zone_spec.get("supported", True)

    headline_box = _box_px(headline_zone.get("box"), W, H)
    subhead_box = _box_px(subhead_zone.get("box"), W, H)
    body_box = _box_px(body_zone_spec.get("box"), W, H) if body_supported else None
    logo_box = _box_px(templates.logo_box(logo_position), W, H)

    # --- keep the wordmark clear of text (top logo vs headline, bottom vs body)
    logo_is_top = logo_position.startswith("top_")
    headline_box, subhead_box, body_box = _avoid_logo_overlap(
        logo_box, logo_is_top, headline_box, subhead_box, body_box, H
    )

    # --- optional scrim under zones
    if scrim:
        zones = [headline_box, subhead_box, logo_box]
        if body_box:
            zones.append(body_box)
        canvas = Image.alpha_composite(canvas, _light_scrim((W, H), zones, alpha=80))

    # --- parse copy elements (group, ignore WORDMARK)
    elements = parse_copy_elements(copy_elements)
    headline_el = next((e for e in elements if e.get("type") == "HEADLINE"), None)
    subtitle_el = next((e for e in elements if e.get("type") == "SUBTITLE"), None)
    body_el = next((e for e in elements if e.get("type") == "BODY"), None)

    all_ops: list[tuple] = []
    headline_final_size = 0

    # --- headline (binary descent fit)
    if headline_el:
        text = headline_el.get("text", "")
        color = _hex_rgb(
            headline_el.get("color"), default=_hex_rgb(FALLBACK_HEADLINE_HEX)
        )
        family = headline_el.get("font_family", "Bricolage Grotesque")
        weight = headline_el.get("font_weight", "bold")
        box_h = headline_box[3] - headline_box[1]
        start_px = min(120, max(40, int(0.8 * box_h)))
        min_px = 32 if canvas_preset == "ig_feed" else 36
        try:
            font, lines, headline_final_size = _fit_headline(
                text=text,
                font_path=brand.font_path(family, weight),
                max_w=headline_box[2] - headline_box[0],
                max_h=box_h,
                start_px=start_px,
                min_px=min_px,
            )
            align = headline_zone.get("align", "left")
            line_ops = _emit_lines(
                lines, font, color, headline_box, align, tracking=0.0, lh=1.06
            )
            all_ops.extend(line_ops)
        except CompositorError as e:
            print(f"[LiittCompositor] WARNING: headline render skipped: {e}")

        # --- subtitle
        if subtitle_el and headline_final_size > 0:
            sub_text = subtitle_el.get("text", "")
            sub_color = _hex_rgb(
                subtitle_el.get("color"),
                default=_hex_rgb(FALLBACK_SUBTITLE_HEX),
            )
            sub_family = subtitle_el.get("font_family", "Hanken Grotesk")
            sub_weight = subtitle_el.get("font_weight", "medium")
            sub_size = max(28, min(56, int(0.45 * headline_final_size)))
            try:
                sub_font = _load_font(brand.font_path(sub_family, sub_weight), sub_size)
                sub_lines = _wrap_text(
                    sub_text, sub_font, subhead_box[2] - subhead_box[0]
                )
                sub_align = subhead_zone.get("align", "left")
                sub_line_ops = _emit_lines(
                    sub_lines,
                    sub_font,
                    sub_color,
                    subhead_box,
                    sub_align,
                    tracking=2.0,
                    lh=1.06,
                )
                all_ops.extend(sub_line_ops)
            except CompositorError as e:
                print(f"[LiittCompositor] WARNING: subtitle render skipped: {e}")

        # --- body (only if template supports it)
        if body_el and body_supported and body_box and headline_final_size > 0:
            body_text = body_el.get("text", "")
            body_color = _hex_rgb(
                body_el.get("color"), default=_hex_rgb(FALLBACK_BODY_HEX)
            )
            if _is_dark(canvas) and body_color == _hex_rgb(FALLBACK_BODY_HEX):
                body_color = _hex_rgb(FALLBACK_BODY_BRIGHT_HEX)
            body_family = body_el.get("font_family", "Newsreader")
            body_weight = body_el.get("font_weight", "medium")
            body_size = max(16, min(38, int(0.48 * headline_final_size)))
            try:
                body_font = _load_font(
                    brand.font_path(body_family, body_weight), body_size
                )
                body_lines = _wrap_text(body_text, body_font, body_box[2] - body_box[0])
                body_align = body_zone_spec.get("align", "left")
                body_line_ops = _emit_lines(
                    body_lines,
                    body_font,
                    body_color,
                    body_box,
                    body_align,
                    tracking=0.0,
                    lh=1.18,
                )
                all_ops.extend(body_line_ops)
            except CompositorError as e:
                print(f"[LiittCompositor] WARNING: body render skipped: {e}")
        elif body_el and not body_supported:
            print(
                f"[LiittCompositor] WARNING: BODY element supplied but "
                f"template {template_id!r} has body:NO — skipping body"
            )
    else:
        print("[LiittCompositor] WARNING: no HEADLINE element in copy_elements")

    # --- text render with shadow
    canvas = _render_with_shadow(canvas, all_ops)

    # --- logo (flat paste, no shadow)
    wordmark_p = brand.wordmark_path()
    if wordmark_p.is_file():
        try:
            wm = Image.open(wordmark_p).convert("RGBA")
            wm_resized, paste_xy = _fit_logo(
                wm, logo_box, min_h=LOGOMARK_MIN_HEIGHT, canvas_size=(W, H)
            )
            canvas.paste(wm_resized, paste_xy, wm_resized)
        except Exception as e:
            print(f"[LiittCompositor] WARNING: failed to load wordmark: {e}")
    else:
        print(
            f"[LiittCompositor] WARNING: wordmark not found at {wordmark_p}, "
            f"skipping logo"
        )

    return canvas.convert("RGB")


# ---------------------------------------------------------------------------
# standalone self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) < 4:
        print(
            "usage: python layout_compositor.py <brand_dir> " "<layout_json> <bg_image>"
        )
        sys.exit(1)
    brand_dir = sys.argv[1]
    layout_json = sys.argv[2]
    bg_path = sys.argv[3]
    sample_chosen = "[CHOSEN LAYOUT]\nasymmetric-left | logo_position: top_left"
    sample_copy = (
        '{"type": "HEADLINE", "text": "Ignite Your Inner Light", '
        '"font_family": "Bricolage Grotesque", "font_weight": "bold", '
        '"color": "#C9C3DA"}\n'
        '{"type": "SUBTITLE", "text": "PICK YOUR MOOD", '
        '"font_family": "Hanken Grotesk", "font_weight": "medium", '
        '"color": "#FFC233"}\n'
        '{"type": "BODY", "text": "A premium functional brand of compound '
        'gummies and instant drink sachets.", '
        '"font_family": "Newsreader", "font_weight": "regular", '
        '"color": "#8E899F"}'
    )
    bg = Image.open(bg_path)
    out = render_layout(
        bg_image=bg,
        chosen_layout=sample_chosen,
        copy_elements=sample_copy,
        layout_json_path=layout_json,
        canvas_preset="ig_feed",
        brand_dir=brand_dir,
        scrim=False,
    )
    out.save("liitt_compositor_selftest.png")
    print(f"wrote liitt_compositor_selftest.png  size={out.size}")
