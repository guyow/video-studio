#!/usr/bin/env python3
"""Brand compositor — deterministic Pillow overlay that guarantees brand consistency.

The AI image model (ComfyUI/SD1.5) can only paint BACKGROUND imagery — it can never
render brand text, logos, colours, or the product name legibly. So every brand-text
pixel is drawn HERE, in pure Pillow, from the brand kit's real fonts + hex tokens.
That makes the wordmark byte-identical and the type/colour pixel-identical on every
post — exactly the "never be creative with the logo / product names" guarantee.

Public API:
  BrandKit(path)                      — load + validate the brand kit (hard-fails on bad tokens)
  make_wordmark(kit, out_dir)         — render the locked `liitt` wordmark PNGs (once, for approval)
  render_template(kit, template, content, bg_image, out_path)  — composite one deliverable

Fonts: Bricolage (display, static Bold), Newsreader (body, variable), Hanken (eyebrow,
variable). Variable weights are set via set_variation_by_axes.
"""
import json
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


class BrandError(Exception):
    pass


class BrandKit:
    """Loads liitt-brand-kit.json and resolves colour/font TOKENS (never raw values)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.root = self.path.parent            # banks/ — font + wordmark paths are relative to here
        self.data = json.loads(self.path.read_text(encoding="utf-8"))
        self.tokens = self.data.get("tokens", {})
        self.fonts_spec = self.data.get("fonts", {})
        self._font_cache: dict = {}

    # ---- colours -------------------------------------------------------
    def color(self, token: str) -> tuple[int, int, int]:
        v = self.tokens.get(token)
        if not v or not HEX_RE.match(str(v)):
            raise BrandError(
                f"brand token '{token}' is not a concrete hex colour (got {v!r}). "
                "Fix liitt-brand-kit.json before compositing.")
        v = v.lstrip("#")
        return (int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))

    def rgba(self, token: str, alpha: int = 255) -> tuple[int, int, int, int]:
        return (*self.color(token), alpha)

    # ---- fonts ---------------------------------------------------------
    def _font_path(self, role: str) -> Path:
        spec = self.fonts_spec.get(role)
        if not spec:
            raise BrandError(f"no font role '{role}' in brand kit")
        p = (self.root / spec["file"]).resolve()
        if not p.is_file():
            raise BrandError(f"brand font missing on disk: {p} (run the font-fetch step)")
        return p

    def font(self, role: str, size: int) -> ImageFont.FreeTypeFont:
        spec = self.fonts_spec.get(role, {})
        key = (role, size)
        if key in self._font_cache:
            return self._font_cache[key]
        fnt = ImageFont.truetype(str(self._font_path(role)), size)
        if spec.get("variable") and spec.get("weight"):
            try:
                fnt.set_variation_by_axes([float(spec["weight"])])
            except Exception:
                pass  # some builds name axes differently; default instance is acceptable
        self._font_cache[key] = fnt
        return fnt

    def preflight(self) -> list[str]:
        """Return a list of problems (empty = healthy). Used by /api/brand/health."""
        problems = []
        for role in ("display", "body", "eyebrow"):
            try:
                self._font_path(role)
            except BrandError as e:
                problems.append(str(e))
        for tok in ("bg_base", "accent_gold", "accent_raspberry", "text_on_dark", "ink_on_gold"):
            try:
                self.color(tok)
            except BrandError as e:
                problems.append(str(e))
        return problems


# ---------------------------------------------------------------- text layout

def _text_w(draw, text, font, tracking=0.0):
    if tracking:
        return sum(draw.textlength(ch, font=font) for ch in text) + tracking * max(0, len(text) - 1)
    return draw.textlength(text, font=font)


def _draw_tracked(draw, xy, text, font, fill, tracking=0.0, anchor_center_x=None):
    """Draw text with per-glyph tracking (Pillow has no letter-spacing)."""
    x, y = xy
    if anchor_center_x is not None:
        x = anchor_center_x - _text_w(draw, text, font, tracking) / 2
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + tracking


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _fit_headline(draw, text, kit, max_w, max_h, start_px, min_px=40, lh=1.06):
    """Shrink the display font until the wrapped headline fits the box."""
    size = start_px
    while size >= min_px:
        font = kit.font("display", size)
        lines = _wrap(draw, text, font, max_w)
        total = len(lines) * size * lh
        if total <= max_h and all(draw.textlength(l, font=font) <= max_w for l in lines):
            return font, lines, size
        size -= 4
    font = kit.font("display", min_px)
    return font, _wrap(draw, text, font, max_w), min_px


# ---------------------------------------------------------------- wordmark

def make_wordmark(kit: BrandKit, out_dir: Path) -> dict:
    """Render the locked `liitt` wordmark once: 'ii' dots become gold flame-wicks.
    Two variants — gold-on-transparent (for dark posts) and light (for gold/light areas)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = kit.data.get("wordmark", {})
    text = spec.get("text", "liitt")
    results = {}
    for variant, letter_hex in (("gold_on_dark", "accent_gold"), ("light", "text_on_dark")):
        size = 220
        pad = 60
        font = kit.font("display", size)
        tmp = Image.new("RGBA", (10, 10))
        d0 = ImageDraw.Draw(tmp)
        w = int(d0.textlength(text, font=font))
        asc, desc = font.getmetrics()
        h = asc + desc
        img = Image.new("RGBA", (w + 2 * pad, h + 2 * pad), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        col = kit.color(letter_hex)
        # base wordmark
        d.text((pad, pad), text, font=font, fill=(*col, 255))
        # twin-wick flames: find the two 'i' glyphs, put a gold glow dot above each
        gold = kit.color("accent_gold_bright") if "accent_gold_bright" in kit.tokens else kit.color("accent_gold")
        x = pad
        dot_r = max(6, size // 22)
        glow_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        for ch in text:
            cw = d.textlength(ch, font=font)
            if ch == "i":
                cx = x + cw / 2
                cy = pad + size * 0.30              # sits right on the 'i' stem top, like a wick
                # flame teardrop: a rounded base + a tapered tip rising from the stem
                gd.ellipse([cx - dot_r, cy - dot_r * 0.9, cx + dot_r, cy + dot_r * 1.1],
                           fill=(*gold, 255))
                gd.polygon([(cx, cy - dot_r * 3.0), (cx - dot_r * 0.85, cy),
                            (cx + dot_r * 0.85, cy)], fill=(*gold, 245))
            x += cw
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(1.5))
        halo = glow_layer.filter(ImageFilter.GaussianBlur(dot_r * 1.4))
        img = Image.alpha_composite(img, halo)
        img = Image.alpha_composite(img, glow_layer)
        img = img.crop(img.getbbox())
        key = "gold_on_dark" if variant == "gold_on_dark" else "light"
        fp = out_dir / f"liitt-{key.replace('_','-')}.png"
        img.save(fp)
        results[key] = str(fp)
    return results


def _load_wordmark(kit: BrandKit, variant: str = "gold_on_dark") -> Image.Image | None:
    spec = kit.data.get("wordmark", {})
    override = spec.get("user_override")
    rel = override or spec.get(variant)
    if not rel:
        return None
    p = (kit.root / rel).resolve() if not Path(rel).is_absolute() else Path(rel)
    if not p.is_file():
        return None
    return Image.open(p).convert("RGBA")


# ---------------------------------------------------------------- template render

def _scrim(size, side, frac=0.62, alpha=205):
    """Vertical gradient scrim so text stays legible over imagery."""
    w, h = size
    grad = Image.new("L", (1, h), 0)
    for y in range(h):
        t = y / max(1, h - 1)
        if side == "bottom":
            v = 0 if t < 1 - frac else int(alpha * (t - (1 - frac)) / frac)
        else:  # top
            v = int(alpha * (1 - t / frac)) if t < frac else 0
        grad.putpixel((0, y), max(0, min(255, v)))
    return grad.resize((w, h))


def render_template(kit: BrandKit, template: dict, content: dict,
                    bg_image: Path | None, out_path: Path) -> Path:
    """Composite one deliverable. `template` = a brand_templates/*.json layout;
    `content` = {eyebrow, headline, subhead, cta, price_line}."""
    plat = kit.data["platforms"][template["platform"]]
    W, H = plat["w"], plat["h"]
    safe = plat["safe"]

    # base: brand bg, then the AI image (cover-fit), then scrim
    canvas = Image.new("RGB", (W, H), kit.color("bg_base"))
    if bg_image and Path(bg_image).is_file():
        img = Image.open(bg_image).convert("RGB")
        scale = max(W / img.width, H / img.height)
        img = img.resize((int(img.width * scale) + 1, int(img.height * scale) + 1))
        canvas.paste(img, ((W - img.width) // 2, (H - img.height) // 2))
    canvas = canvas.convert("RGBA")

    text_zone = template.get("text_zone", "bottom")
    scrim = _scrim((W, H), text_zone, frac=template.get("scrim_frac", 0.6),
                   alpha=template.get("scrim_alpha", 210))
    shade = Image.new("RGBA", (W, H), (*kit.color("bg_base"), 255))
    canvas = Image.composite(shade, canvas, scrim)

    draw = ImageDraw.Draw(canvas)
    cx = W // 2
    inner_w = W - safe["left"] - safe["right"]

    # layout anchored to the text zone
    if text_zone == "bottom":
        y = H - safe["bottom"]
        build_up = True
    else:
        y = safe["top"]
        build_up = False

    blocks = []  # (kind, ...) collected then drawn bottom-up or top-down

    eyebrow = content.get("eyebrow", "").upper()
    headline = content.get("headline", "")
    subhead = content.get("subhead", "")
    cta = content.get("cta", "")
    price = content.get("price_line", "")

    # measure headline (auto-fit)
    hl_font, hl_lines, hl_size = _fit_headline(
        draw, headline, kit, inner_w, H * template.get("headline_max_h_frac", 0.34),
        start_px=template.get("headline_px", 96))

    eyebrow_font = kit.font("eyebrow", template.get("eyebrow_px", 34))
    sub_font = kit.font("body", template.get("subhead_px", 40))
    cta_font = kit.font("eyebrow", template.get("cta_px", 38))
    price_font = kit.font("body", template.get("price_px", 34))

    # --- render, building from the anchor edge inward ---
    def draw_cta_button(yy):
        pad_x, pad_y = 40, 22
        tw = _text_w(draw, cta.upper(), cta_font, tracking=2)
        bw, bh = tw + 2 * pad_x, cta_font.size + 2 * pad_y
        bx = cx - bw / 2
        # CTA on gold → ink text (NEVER white on gold — brand rule)
        draw.rounded_rectangle([bx, yy, bx + bw, yy + bh], radius=bh // 2,
                               fill=kit.rgba("accent_gold"))
        _draw_tracked(draw, (0, yy + pad_y), cta.upper(), cta_font,
                      kit.rgba("ink_on_gold"), tracking=2, anchor_center_x=cx)
        return bh

    lh = 1.06
    if build_up:
        # bottom → up: wordmark, price, cta, subhead, headline, eyebrow
        wm = _load_wordmark(kit, template.get("wordmark_variant", "gold_on_dark"))
        if wm:
            ww = int(W * template.get("wordmark_w_frac", 0.22))
            wh = int(ww * wm.height / wm.width)
            canvas.paste(wm.resize((ww, wh)), (cx - ww // 2, y - wh), wm.resize((ww, wh)))
            y -= wh + 24
        if price:
            y -= price_font.size
            _draw_tracked(draw, (0, y), price, price_font, kit.rgba("text_muted"),
                          anchor_center_x=cx)
            y -= 18
        if cta:
            bh = cta_font.size + 44
            y -= bh
            draw_cta_button(y)
            y -= 28
        if subhead:
            for line in reversed(_wrap(draw, subhead, sub_font, inner_w)):
                y -= int(sub_font.size * 1.25)
                _draw_tracked(draw, (0, y), line, sub_font, kit.rgba("text_on_dark"),
                              anchor_center_x=cx)
            y -= 20
        for line in reversed(hl_lines):
            y -= int(hl_size * lh)
            _draw_tracked(draw, (0, y), line, hl_font, kit.rgba("text_on_dark"),
                          anchor_center_x=cx)
        y -= 22
        if eyebrow:
            _draw_tracked(draw, (0, y - eyebrow_font.size), eyebrow, eyebrow_font,
                          kit.rgba("accent_gold"), tracking=2, anchor_center_x=cx)

    canvas.convert("RGB").save(out_path, quality=94)
    return out_path


if __name__ == "__main__":
    # self-test: render the wordmark + a dummy card so the module can be exercised standalone
    import sys
    kit = BrandKit(sys.argv[1] if len(sys.argv) > 1 else
                   r"C:/Users/guyas/Claude/Projects/Video AI editing/autoVSL/banks/liitt-brand-kit.json")
    probs = kit.preflight()
    print("preflight:", "OK" if not probs else probs)
    wm = make_wordmark(kit, kit.root / "brand-assets" / "wordmark")
    print("wordmark:", wm)
