import glob
import os
import re

from folder_paths import get_input_directory
import numpy as np
from PIL import Image

try:
    import fal_client
except ImportError:
    fal_client = None


APP = "openrouter/router/vision"
DEFAULT_MODEL = "google/gemini-2.5-flash"


SYSTEM_PROMPT = """You are a Creative Director for the wellness brand **liitt**.
Your job is to write a DESIGN BRIEF that will be given to a poster designer 
(NanoBanana). The designer has access to the same liitt brand kit images 
that you see. Your brief must tell the designer WHAT to create, not HOW 
every pixel should look.

The designer is an image generation model that understands natural 
language instructions. Write your brief as if you are briefing a human 
designer who knows the liitt brand well.

================================================================
LIITT BRAND - ALWAYS ACTIVE
================================================================

You always build for **liitt** - a premium functional brand of compound 
gummies and instant drink sachets built on a spectrum of feeling states, 
from calm to cosmic.

BRAND PERSONA:
- Brand: liitt
- Products: Mushroom Coffee (10 Sachets), Fairy Flame Gummy (30 Flame Gummies)
- Category: Dietary Supplement
- Tagline: "Ignite Your Inner Light" / "Pick your Mood, Pick your Fire"
- Tone: aspirational but grounded, premium but never pretentious

HARD RULES - follow exactly, NO exceptions. Include these in every brief's 
[BRAND CONSTRAINTS] section:

1. LOGO: Wordmark "liitt" (lowercase). Flame rises between the two 'i' 
   characters (the twin wicks). Minimum size: clearly legible (120px+).
   NEVER: stretch, distort, recolor, add shadows/effects/outlines.
   Approved backgrounds: #141428 (dark navy), #FF5A1F (orange), #0A0A0A (black).

2. BACKGROUND: #0A0A0A or #0B0A1C Deep Matte Finish. Dark backgrounds, matte finish. 
   No exceptions. No gradients that stray from the dark palette.

3. NO CTA: No "Buy Now", "Shop", "Order", "Get Yours", "Limited Time", 
   "SHOP THE JAR", QR codes, or any purchase-prompt text. Absolute ban.

4. WORDMARK: "liitt" - always lowercase. No variation in spelling or casing.

5. NO invented brand marks: No URLs, social handles, price tags, 
   certifications, third-party logos. Omit entirely.

GUIDELINES - use your design judgment within these bounds:

6. COLOR PALETTE - Primary accents: #FFC233 (signature yellow), #FF5A1F 
   (Electric Flame Orange). Mood spectrum (accent only, never full background):
   Dream: #4B40C9 (firefly), Calm: #0389F3 (crescent moon+star),
   Open: #BE4EF3 (butterfly), Bliss: #9BCB31 (leaf/sprout),
   Wonder: #A23A6D (fairy), Focus: #F8A30A (phoenix),
   Fire: #C34605 (dragon), Cosmic: #C9C3DC (unicorn)

TEXT COLORS (approved for text on #0A0A0A backgrounds):
- Headlines / Display: #C9C3DA (Soft Lavender) or Ivory White (#FAFAF7) - primary headline color
- Headline alt: #FF5A1F (Electric Flame Orange) - alternative headline accent
- Headline accent / Wordmark: #FFC233 (Signature Yellow) - wordmark, price, emphasis
- Subtitles / Mood badges: use the chosen mood's hex color as accent
- Body copy / Descriptions: #8E899F (Dusty Purple Gray) or Ivory White (#FAFAF7) - editorial body text
- Captions / Secondary: #14122B (Dark Navy Blue) - subtle secondary text, low contrast on dark backgrounds

7. TYPOGRAPHY - Display/H1: Bricolage Grotesque bold. UI/labels: Hanken 
   Grotesk medium. Body: Newsreader regular/light serif. This is the liitt 
   font hierarchy. Do not substitute.

8. BANNED aesthetics: green smoothies, yoga poses, beige wellness palettes,
   candy-bright colors, neon, trippy/psychedelic cliches.

9. MOOD SPECTRUM - 8 moods = 8 creatures. Pick exactly ONE per poster.
   The flame is the master mark. Each mood gets its own creature.

10. COPY - Tagline: "Ignite Your Inner Light". Wordmark: "liitt" lowercase.

================================================================
LAYOUT TEMPLATE SELECTION — MODE B (NEW_GENERATION) ONLY
================================================================

THIS SECTION IS ACTIVE ONLY when:
(a) Mode is MODE B / NEW_GENERATION (no reference poster supplied), AND
(b) The user prompt contains an "AVAILABLE LAYOUT TEMPLATES" catalog.

If a reference design poster IS supplied (MODE A / REPLACE), the
reference poster provides the layout structure. IGNORE any layout
catalog entirely — do NOT output [CHOSEN LAYOUT] in MODE A.

When active (MODE B + catalog present):

1. Select exactly ONE layout from the catalog that best fits the
   creative brief, product, chosen mood, and copy text volume.

2. Add a [CHOSEN LAYOUT] section to your output immediately before
   [DIRECTIVE]. Format:
   <layout-name> | logo_position: <position>
   Example: "asymmetric-left | logo_position: top_left"

3. Use the template's logo_position_default unless the copy
   composition strongly suggests an alternative from the listed
   logo_position_alternatives.

4. EXCLUDE any template with body:NO if the creative brief or
   product_description needs body copy / description text. For
   image-only ads with no body copy, body:NO layouts are valid.

5. In [IMAGE SCENE], describe the scene respecting the chosen
   layout's zone areas. The [IMAGE SCENE] section MUST contain its
   3 labeled parts:
   - PRODUCT PLACEMENT: where the product sits (left/center/right,
     top/mid/bottom), angle, scale, surrounding props. CRITICAL:
     product packaging text, colors, proportions remain IDENTICAL.
   - NEGATIVE SPACE: list EACH text/logo zone (headline, subheadline,
     body, logo) as VISUALLY QUIET background — the scene continues
     through it naturally, but with NO text/logo/symbols/busy detail.
   - SCENE & LIGHTING: mood, photographic style, lighting, palette.
   - DO NOT use pixel coordinates or box numbers.

6. Output a [LAYOUT RATIONALE] section (1 sentence): why this
   layout fits the mood and product.

================================================================
MODE A - REPLACE (reference design image IS supplied)
================================================================

The user supplies a reference poster (Image 2). The intent: use the 
reference as a COMPOSITIONAL TEMPLATE. All brand identity elements come 
from liitt.

WHAT TO KEEP FROM THE REFERENCE:
- Composition, layout grid, element placement, framing, negative space
- Photographic style, lighting direction, shadow quality, camera angle, 
  depth of field, aspect ratio
- Decorative graphic STYLE (shapes, lines, badges, frames) - BUT recolor 
  them to the liitt palette
- CRITICAL: All text content (body copy, descriptions, taglines, sub-lines) 
  from the reference is PRESERVED word-for-word - EXCEPT brand names and 
  product names, which are swapped to "liitt" / "Fairy Flame Gummy" / 
  "Mushroom Coffee". Do NOT discard or rewrite non-brand text from the 
  reference. The reference's copywriting is part of the template.

WHAT LIITT OVERRIDES (comes from brand, NOT reference):
- Product/subject: the reference's hero element -> liitt product (Image 1)
- Logo: liitt wordmark (not the reference's logo)
- Color palette: liitt base + mood spectrum (not the reference's colors)
- Typography: Bricolage/Hanken/Newsreader (not the reference's fonts)
- Mood: one liitt creature (not the reference's mood/theme)
- Brand names in text: any brand/product names in the reference's text -> 
  "liitt" / product name

USER OVERRIDE: If the user supplies requirements via [BLOCK] syntax in 
product_description, those override specific text elements in the 
reference. See [USER REQUIREMENTS] below.

================================================================
MODE B - NEW GENERATION (no reference design image)
================================================================

Design a complete original liitt poster from scratch. Use 
product_description as the creative brief.

Generate:
- Composition and layout (dark-first liitt aesthetic)
- Color palette from LIITT BRAND (locked)
- Typography: FIXED liitt font hierarchy
- Marketing copy in liitt's brand voice with proper hierarchy
- Pick exactly ONE mood creature as the emotional anchor
- Pick the product: Fairy Flame Gummy, Mushroom Coffee, or both
- NO CTA, no invented marks, no banned cliches

================================================================
USER BLOCKS
================================================================

Users may include [BLOCK_NAME]: content in product_description. Parse 
these blocks and forward them to the [USER REQUIREMENTS] section of your 
output. Blocks use this syntax: [BLOCK_NAME]: value on the same line.

USER REQUIREMENTS override ALL constraints where they conflict - including 
HARD RULES. The client's explicit instructions take priority. If a user 
says [COLOR]: use white background, include it and note the conflict; 
the designer will follow the user's directive.

Non-block content in product_description (text without [BLOCK]: prefix) 
is creative brief context - use it for [IMAGE SCENE] and 
[COPY_ELEMENTS_MACHINE], but do NOT forward it verbatim as requirements.

================================================================
OUTPUT FORMAT
================================================================

Return EXACTLY this structure. Plain text, no markdown fences, no 
commentary, no preamble. OMIT sections that don't apply to the active mode.

[MODE]
<REPLACE or NEW_GENERATION>

[DIRECTIVE]                          (always)
<one-sentence role assignment and creative intent. Address the designer 
directly: "You are designing a liitt poster for [product]. The intent: 
[REPLACE/NEW_GENERATION]. ...">

[PRODUCT]                            (always)
<Fairy Flame Gummy, Mushroom Coffee, or both>

[MOOD]                               (always)
<chosen mood name, creature, accent hex color. 1-2 sentences on the 
emotional feel this poster should evoke.>

[REFERENCE]                          (MODE A only)
<what to keep from the reference poster: composition, lighting, camera 
angle, decorative graphic style recolored to liitt palette. Explicitly 
state: "All non-brand text from the reference is PRESERVED word-for-word 
except brand/product names which are swapped to liitt.">

[BRAND CONSTRAINTS]                  (always)
<Output ONLY the constraints relevant to IMAGE GENERATION. The compositor
handles logo rendering, wordmark spelling, typography/fonts, and copy —
so OMIT those entirely here. Include only:

HARD RULES (image-relevant):
- Approved backgrounds: #141428 (dark navy), #FF5A1F (orange), #0A0A0A
  (black). Deep Matte Finish. Dark backgrounds first. No exceptions.
  No gradients that stray from the palette.
- NO CTA: no "Buy Now", "Shop", "Order", "Get Yours", "Limited Time",
  QR codes, or any purchase-prompt text. Absolute ban.
- NO invented brand marks: no URLs, social handles, price tags,
  certifications, third-party logos. Omit entirely.

GUIDELINES (image-relevant):
- COLOR PALETTE (accents only, never full background): output the FULL
  palette from the brand section — the complete mood spectrum hexes
  (Dream #4B40C9, Calm #0389F3, Open #BE4EF3, Bliss #9BCB31,
  Wonder #A23A6D, Focus #F8A30A, Fire #C34605, Cosmic #C9C3DC) AND the
  text colors (headlines #C9C3DA, wordmark/accent #FFC233, headline alt
  #FF5A1F, subtitles = chosen mood hex, body #8E899F, captions #14122B).
  Keep the full palette so the scene's glow/accent lighting matches the
  brand — even though the compositor renders the actual text.
- BANNED aesthetics: green smoothies, yoga poses, beige wellness
  palettes, candy-bright colors, neon, trippy/psychedelic cliches.>

[BRAND OVERRIDE]                     (MODE A only)
<list of what comes from liitt NOT the reference: product, logo, palette, 
typography, mood, brand names in text>

[USER REQUIREMENTS]                  (only if user supplied [BLOCK] syntax)
<forward each user block verbatim: [BLOCK_NAME]: content. One per line.
Include a note: "These client requirements override all constraints 
where they conflict.">

[IMAGE SCENE]                        (always)
<This section IS the complete prompt the image generation model 
(NanoBanana) receives. It MUST contain exactly these 3 labeled parts, 
in this order. Each part is a short paragraph:

1. PRODUCT PLACEMENT: Explicitly state where the product sits in the 
frame using natural language — left/center/right, top/mid/bottom, 
angle, scale, and any surrounding props. CRITICAL: product packaging 
text, colors, and proportions must remain IDENTICAL to the original, 
never redesigned. Never describe the product without a clear screen 
position.

2. NEGATIVE SPACE (protected zones for composited text): List EACH 
text/logo zone from the chosen layout, by screen position, in natural 
language. Describe each zone as VISUALLY QUIET — the background scene 
continues through it naturally (forest, gradient, texture, etc.), but 
with NO text, NO logos, NO symbols, NO graphic shapes, and no busy 
high-contrast detail. Do NOT draw a dark box or empty void — the zone 
must look like a natural, calm part of the full design. Example: "Let 
the enchanted forest background continue softly through the top third 
of the frame — calm and uncluttered, with no text, logos, or symbols — 
a quiet area for the headline to sit on." You MUST explicitly mention 
the headline zone, subheadline zone, and logo zone (and body zone if 
the layout supports it) using top/center/bottom 
+ left/center/right descriptors.

3. SCENE & LIGHTING: The mood, photographic style, lighting direction, 
color palette, and energy. Give the designer creative room on the 
scene itself while being specific about the vibe. Use words like 
"think:", "evoke:", "feel:".>

DO NOT use pixel coordinates or box numbers anywhere. Describe 
positions in natural language only. The three parts above map 
directly to the layout's product / text-zone / background areas.>

IMPORTANT (always output this exact line, immediately after [IMAGE SCENE]):
Do not render any copy elements, brand or wordmark logo, only design scene.

[COPY_ELEMENTS_MACHINE]              (always)
<Machine-parseable copy elements for the compositor. One JSON object per
line - each line is a complete JSON object. This section is automatically
parsed into the copy_elements output and is NOT sent to the image model.

Format per line:
{"type": "HEADLINE", "text": "Unlock Your Inner Fairy", "font_family": "Bricolage Grotesque", "font_weight": "bold", "color": "#C9C3DA"}

Fields:
- type: HEADLINE, SUBTITLE, BODY, WORDMARK
  (MODE B / NEW_GENERATION. TAGLINE and CALLOUT are MODE A / REPLACE
  extraction only.)
- text: the EXACT text to render. WORDMARK text is always "liitt".
- font_family: Bricolage Grotesque, Hanken Grotesk, or Newsreader
- font_weight: bold, regular, medium, light
- color: hex color from the liitt TEXT COLORS palette (headline: #C9C3DA,
  wordmark/accent: #FFC233, subtitle: chosen mood hex, body: #8E899F)

MODE B ZONE MAPPING RULES (NEW_GENERATION only):
- Emit only elements that map 1:1 to the chosen layout's zones:
  HEADLINE -> headline zone
  SUBTITLE -> subheadline zone
  BODY     -> body zone (only if the layout supports body)
  WORDMARK -> logo position (from [CHOSEN LAYOUT])
- SUBTITLE must be 7-12 words - a substantive benefit/mood line, not a 2-word label.
- BODY COPY FORM (MODE B): Vary the body copy across ONE of these 3 forms, matched to the chosen mood/theme and layout — do NOT default to storytelling every time:
  1. STORYTELLING — a short narrative beat (a moment, a feeling, a before/after glimpse).
  2. SOCIAL PROOF / DATA-DRIVEN BENEFIT — a compact benefit list or a proof/data line (ratings, counts, or 2–3 inline benefits).
  3. PROBLEM-SOLUTION & REASON-WHY — the 'before' tension, then the reason the product resolves it.
  BODY must be 15–30 words. Write benefits INLINE (comma or short-phrase separated) — do NOT use bullet characters ("•", "-", "*") or line breaks; the compositor renders BODY as a single wrapped paragraph.
- TAGLINE is NOT a standalone element: if the tagline must appear, fold
  it into the HEADLINE or BODY text — never invent a new screen position.
- If the chosen layout has no body zone (body:NO), do NOT emit a BODY
  element.
- JSON must be valid - one complete object per line, no trailing commas.
- Return ONLY JSON lines, no other text in this section.>

The [DIRECTIVE] + [IMAGE SCENE] together form the complete image prompt.
The [COPY_ELEMENTS_MACHINE] section carries the compositor metadata
separately. Make every section concrete and visually specific. Use present
tense. Reference colors, materials, lighting, and composition explicitly
where relevant.
"""


def tensor_to_pil(tensor):
    arr = np.clip(tensor[0].detach().cpu().numpy() * 255.0, 0, 255).astype(np.uint8)
    return Image.fromarray(arr, "RGB")


def _parse_user_blocks(text):
    """Extract [BLOCK_NAME]: content patterns from user input."""
    import re

    pattern = r"^\[([A-Za-z0-9_\- ]+)\]:\s*(.+)$"
    blocks = []
    for line in text.strip().split("\n"):
        match = re.match(pattern, line.strip())
        if match:
            blocks.append((match.group(1).strip(), match.group(2).strip()))
    return blocks


def _describe_zone(box):
    """Convert a [x0, y0, x1, y1] box to a compact position description."""
    if not box or len(box) < 4:
        return "?"
    x0, y0, x1, y1 = box[:4]
    cx = (x0 + x1) / 2
    # Horizontal
    if x0 < 0.05 and x1 > 0.95:
        h = "full-width"
    elif cx < 0.35:
        h = "left"
    elif cx > 0.65:
        h = "right"
    else:
        h = "center"
    # Vertical
    if y1 < 0.30:
        v = "top"
    elif y0 > 0.70:
        v = "bottom"
    else:
        v = "mid"
    return f"{h}-{v}"


def _load_layout_templates(filepath):
    """Load layout templates JSON, validate, return compact catalog string.

    Args:
        filepath: Absolute or relative path to the JSON file.

    Returns:
        str: Compact catalog string for prompt injection.

    Raises:
        RuntimeError: File not found, invalid JSON, no templates, etc.
    """
    import json
    import os

    path = filepath.strip()
    if not path:
        raise RuntimeError("[UGCVisionPlanner] layout_templates is empty")

    # Resolve relative paths against ComfyUI root
    if not os.path.isabs(path):
        from folder_paths import get_input_directory

        comfy_root = os.path.dirname(get_input_directory())
        path = os.path.join(comfy_root, path)
        path = os.path.realpath(path)

    if not os.path.isfile(path):
        raise RuntimeError(
            f"[UGCVisionPlanner] layout_templates file not found: {path}"
        )

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"[UGCVisionPlanner] layout_templates JSON is invalid: {e}"
        ) from e

    templates = data.get("templates")
    if not templates or not isinstance(templates, dict):
        raise RuntimeError(
            f"[UGCVisionPlanner] layout_templates JSON has no 'templates' dict: {path}"
        )

    if len(templates) == 0:
        raise RuntimeError(
            f"[UGCVisionPlanner] layout_templates JSON has 0 templates: {path}"
        )

    # Generate compact catalog
    lines = ["AVAILABLE LAYOUT TEMPLATES:"]
    for idx, (name, tmpl) in enumerate(templates.items(), 1):
        p_zone = tmpl.get("product", {}).get("box")
        p_desc = _describe_zone(p_zone) if p_zone else "?"
        h_zone = tmpl.get("headline", {}).get("box")
        h_desc = _describe_zone(h_zone) if h_zone else "?"
        s_zone = tmpl.get("subheadline", {}).get("box")
        s_desc = _describe_zone(s_zone) if s_zone else "?"
        body = tmpl.get("body", {})
        body_supported = body.get("supported", True)
        b_zone = body.get("box")
        b_desc = _describe_zone(b_zone) if b_zone else "?"
        logo_def = tmpl.get("logo_position_default", "?")
        logo_alt = tmpl.get("logo_position_alternatives", [])
        logo_str = logo_def
        if logo_alt:
            logo_str += f" [alt: {', '.join(logo_alt)}]"

        body_flag = "body:OK" if body_supported else "body:NO"
        line = (
            f"{idx}. {name}: "
            f"H={h_desc}, P={p_desc}, SubH={s_desc}, Body={b_desc}. "
            f"Logo: {logo_str}. {body_flag}"
        )
        lines.append(line)

    catalog = "\n".join(lines)
    print(f"[UGCVisionPlanner] Loaded {len(templates)} layout templates from {path}")
    return catalog


class UGCVisionPlanner:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "product_image": ("IMAGE",),
                "product_description": (
                    "STRING",
                    {"default": "", "multiline": True},
                ),
                "model": (
                    ["google/gemini-2.5-flash", "google/gemini-2.5-pro"],
                    {"default": DEFAULT_MODEL},
                ),
                "temperature": (
                    "FLOAT",
                    {"default": 0.35, "min": 0.0, "max": 1.0, "step": 0.05},
                ),
            },
            "optional": {
                "reference_image": ("IMAGE",),
                "additional_object_image": (
                    "IMAGE",
                    {"forceInput": True, "multiline": True, "io": True},
                ),
                "brand_guide": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "Absolute path to brand images folder, or name relative to comfyui/brand/",
                    },
                ),
                "layout_templates": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "placeholder": "Absolute path to layout templates JSON file",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("prompt", "copy_elements")
    FUNCTION = "plan"
    CATEGORY = "UGC Poster/AI"

    def plan(
        self,
        product_image,
        product_description,
        model,
        temperature,
        reference_image=None,
        additional_object_image=None,
        brand_guide="",
        layout_templates="",
    ):
        if fal_client is None:
            raise RuntimeError(
                "fal-client is not installed. Run: " "python -m pip install fal-client"
            )

        if not os.getenv("FAL_KEY"):
            raise RuntimeError(
                "FAL_KEY is not set. Set your fal.ai API key in the "
                "environment used to launch ComfyUI."
            )

        client = fal_client.SyncClient()

        # Order: [product, reference?, additional_object?]
        product_pil = tensor_to_pil(product_image)
        image_urls = [client.upload_image(product_pil, format="jpeg")]

        if reference_image is not None:
            ref_pil = tensor_to_pil(reference_image)
            image_urls.append(client.upload_image(ref_pil, format="jpeg"))

        if additional_object_image is not None:
            obj_pil = tensor_to_pil(additional_object_image)
            image_urls.append(client.upload_image(obj_pil, format="jpeg"))

        # --- Brand guide image loading ---
        brand_urls = []
        brand_count = 0
        if brand_guide and brand_guide.strip():
            brand_guide_val = brand_guide.strip()
            # Resolve the brand folder: an absolute path is used as-is; a
            # relative name resolves against comfyui/brand/ (the shared brand
            # assets root, mirroring the LiittCompositor).
            if os.path.isabs(brand_guide_val):
                brand_dir = brand_guide_val
            else:
                brand_root = os.path.realpath(
                    os.path.join(os.path.dirname(__file__), "..", "..", "..", "brand")
                )
                brand_dir = os.path.join(brand_root, brand_guide_val)
            brand_dir = os.path.realpath(brand_dir)
            if not os.path.isdir(brand_dir):
                raise RuntimeError(f"brand_guide folder not found: {brand_dir}")
            png_files = sorted(glob.glob(os.path.join(brand_dir, "*.png")))
            if not png_files:
                print(
                    f"[UGCVisionPlanner] WARNING: brand_guide folder contains "
                    f"no .png files: {brand_dir}"
                )
            else:
                for png_path in png_files:
                    try:
                        brand_pil = Image.open(png_path).convert("RGB")
                        brand_url = client.upload_image(brand_pil, format="jpeg")
                        brand_urls.append(brand_url)
                        brand_count += 1
                    except Exception as e:
                        print(
                            f"[UGCVisionPlanner] WARNING: skipping unreadable "
                            f"brand image {os.path.basename(png_path)}: {e}"
                        )
                if brand_count == 0:
                    print(
                        f"[UGCVisionPlanner] WARNING: {len(png_files)} .png files "
                        f"found in brand_guide but none could be loaded"
                    )

        # Extend image_urls: [product, reference?, additional_object?, *brand]
        image_urls.extend(brand_urls)

        # Compute main_image_count
        main_image_count = 1
        if reference_image is not None:
            main_image_count += 1
        if additional_object_image is not None:
            main_image_count += 1

        # --- 14-image limit enforcement (NanoBanana Edit constraint) ---
        # In REPLACE mode: product(1) + reference(1) + additional_object?(1) + brand
        # Must stay <= 14 total. If brand_count would exceed, drop excess brand images.
        total_images = main_image_count + brand_count
        if total_images > 14:
            excess = total_images - 14
            keep = brand_count - excess
            if keep < 0:
                keep = 0
            # Trim image_urls: keep core images + only keep brand images
            image_urls = image_urls[: main_image_count + keep]
            print(
                f"[UGCVisionPlanner] WARNING: NanoBanana Edit supports max 14 "
                f"images. Dropping {excess} brand image(s) to stay within limit. "
                f"Total: {main_image_count} core + {keep} brand = "
                f"{main_image_count + keep}/14."
            )
            brand_count = keep

        # --- Layout templates loading (MODE B / NEW_GENERATION only) ---
        layout_catalog = ""
        if layout_templates and layout_templates.strip() and reference_image is None:
            try:
                layout_catalog = _load_layout_templates(layout_templates.strip())
            except RuntimeError as e:
                raise RuntimeError(
                    f"[UGCVisionPlanner] Failed to load layout templates: {e}"
                ) from e

        user_prompt = self._make_user_prompt(
            product_description=product_description,
            has_reference=reference_image is not None,
            has_additional_object=additional_object_image is not None,
            main_image_count=main_image_count,
            brand_count=brand_count,
            layout_catalog=layout_catalog,
        )

        arguments = {
            "image_urls": image_urls,
            "prompt": user_prompt,
            "system_prompt": SYSTEM_PROMPT,
            "model": model,
            "temperature": float(temperature),
            "max_tokens": 4000,
        }

        result = client.run(APP, arguments)
        output_text = result.get("output", "")

        # --- Extract copy_elements for machine parsing ---
        copy_elements = ""
        if "[COPY_ELEMENTS_MACHINE]" in output_text:
            parts = output_text.split("[COPY_ELEMENTS_MACHINE]")
            if len(parts) > 1:
                section = parts[1].strip()
                next_section_match = re.search(r"\n\s*\[", section)
                if next_section_match:
                    section = section[: next_section_match.start()]
                copy_elements = section.strip()

        # --- Remove [COPY_ELEMENTS_MACHINE] from prompt sent to NanoBanana ---
        if "[COPY_ELEMENTS_MACHINE]" in output_text:
            # Split on the section header and keep everything before it
            output_text = output_text.split("[COPY_ELEMENTS_MACHINE]")[0].strip()

        return (output_text, copy_elements)

    @staticmethod
    def _make_user_prompt(
        product_description,
        has_reference,
        has_additional_object,
        main_image_count,
        brand_count,
        layout_catalog="",
    ):
        lines = []

        if has_reference:
            lines.append(
                "Image 1 = product to feature (liitt product). "
                "Image 2 = reference design poster. "
                "MODE: REPLACE. Use the reference as a COMPOSITIONAL TEMPLATE. "
                "CRITICAL: All non-brand text from the reference (body copy, "
                "descriptions, taglines) is PRESERVED word-for-word. Only "
                "brand names and product names in the text are swapped to "
                "'liitt' / product name. Do NOT rewrite or discard the "
                "reference's copywriting. Brand visual identity (logo, colors, "
                "fonts, mood) always comes from liitt - see system prompt. "
                "Swap the subject with the product in Image 1."
            )
        else:
            lines.append(
                "Image 1 = product to feature. No reference design was "
                "supplied. MODE: NEW_GENERATION. Design a complete original "
                "liitt-branded UGC product poster from the product_description "
                "below."
            )

        if has_additional_object:
            obj_idx = main_image_count
            if has_reference:
                lines.append(
                    f"Image {obj_idx} = additional object. Treat it as a "
                    "small supporting prop styled to match the reference's "
                    "photographic language; do not let it disrupt the layout."
                )
            else:
                lines.append(
                    f"Image {obj_idx} = additional object. Weave it into the "
                    "scene as a supporting prop beside, behind, or interacting "
                    "with the main product - never a competing focal point."
                )

        # Brand image labels
        if brand_count > 0:
            lines.append("")
            brand_start = main_image_count
            lines.append(
                f"BRAND REFERENCE IMAGES (images {brand_start} to "
                f"{brand_start + brand_count - 1}): These are liitt brand "
                "reference assets - wordmark, logo lockups, approved color "
                "swatches, brand photography, packaging shots, mood imagery. "
                "Use them as the visual source of truth for the liitt brand "
                "identity. Match the color palette, wordmark style, typography "
                "feel, and overall aesthetic. The final poster must look like "
                "it belongs in this brand family."
            )

        # Parse user blocks and creative brief
        if product_description and product_description.strip():
            blocks = _parse_user_blocks(product_description)
            # Extract non-block text (text not matching [BLOCK]: syntax)
            creative_brief = ""
            if blocks:
                import re

                block_line_pattern = re.compile(r"^\[([A-Za-z0-9_\- ]+)\]:\s*.+$")
                non_block_lines = []
                for line in product_description.strip().split("\n"):
                    if not block_line_pattern.match(line.strip()):
                        non_block_lines.append(line)
                creative_brief = "\n".join(non_block_lines).strip()
            else:
                creative_brief = product_description.strip()

            if blocks:
                lines.append("")
                lines.append("USER BLOCKS - forward these to [USER REQUIREMENTS]:")
                for block_name, block_value in blocks:
                    lines.append(f"  [{block_name}]: {block_value}")
                lines.append(
                    "These override ALL constraints - including HARD RULES - "
                    "where they conflict. Client directives take priority."
                )
            if creative_brief:
                lines.append("")
                lines.append("PRODUCT DESCRIPTION / CREATIVE BRIEF:")
                lines.append(creative_brief)
        else:
            if not has_reference:
                lines.append("")
                lines.append(
                    "PRODUCT DESCRIPTION: [none supplied]. The product image "
                    "itself is the only product information available. Use the "
                    "liitt brand identity from the system prompt. Do not "
                    "invent brand names, taglines, or marketing copy."
                )
            else:
                lines.append("")
                lines.append(
                    "The LIITT BRAND identity in the system prompt is the single "
                    "source of truth for brand voice, colors, typography, and "
                    "wordmark. The reference is a COMPOSITIONAL TEMPLATE - preserve "
                    "its copy text (swap brand names only), but use liitt brand "
                    "identity for all visual elements."
                )

        # MODE B only: inject layout catalog (skip for MODE A / REPLACE)
        if layout_catalog and layout_catalog.strip() and not has_reference:
            lines.append("")
            lines.append(layout_catalog.strip())
            lines.append("")
            lines.append(
                "MODE B — SELECT a layout from the catalog above that best fits "
                "this creative brief and chosen mood. Output your choice in "
                "[CHOSEN LAYOUT] format: <layout-name> | logo_position: <position>. "
                "Also add a 1-sentence [LAYOUT RATIONALE]."
            )

        lines.append("")
        lines.append(
            "Emit the structured prompt as specified in the system instructions. "
            f"Active mode: {'REPLACE' if has_reference else 'NEW_GENERATION'}."
        )
        return "\n".join(lines)


NODE_CLASS_MAPPINGS = {
    "UGCVisionPlanner": UGCVisionPlanner,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "UGCVisionPlanner": "UGC Vision Planner",
}
