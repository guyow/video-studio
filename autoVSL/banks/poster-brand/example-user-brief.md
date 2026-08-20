# Reference & Examples: USER BLOCKS in the liitt Poster Brief

This document explains the **USER BLOCKS / [USER REQUIREMENTS]** section of
`autoVSL/banks/poster-brand/planner-prompt.txt` — what it is, how the parser
actually works, which blocks are practical today, which blocks *don't exist yet*
but could be added, and how to use them in a real brief.

---

## 1. What a USER BLOCK is

Everything you type into the **brief / product_description** field is read by the
planner (`video-studio/app/engines/poster_planner.py`) and split into two parts:

| Line type | Example | Where it goes |
|---|---|---|
| **Block** (line starting with `[NAME]:`) | `[MOOD]: Fire — dragon` | Forwarded **verbatim** into the `[USER REQUIREMENTS]` section of the planner output |
| **Non-block** (plain sentence) | `Poster for 30-45 office workers who can't focus in the morning.` | Becomes the *creative brief* — feeds `[IMAGE SCENE]` and `[COPY_ELEMENTS_MACHINE]`, never forwarded raw |

### The rules (straight from the parser)

```python
pattern = re.compile(r"^\[([A-Za-z0-9_\- ]+)\]:\s*(.+)$")
```

1. **One block = one line.** The value must sit on the same line as the name.
   A value on the next line is silently ignored.
2. **Block names are free-form** — only letters, digits, `_`, `-`, and spaces are
   allowed. There is no whitelist. `[MOOD]`, `[Background Color]`, and `[NO-CTA]`
   are all valid.
3. **Colon plus content is required.** An empty `[MOOD]:` is skipped.
4. The parser is **case-insensitive by construction**, but the team convention is
   **UPPERCASE**.
5. Block lines are **removed** from the creative brief, so nothing is counted twice.

### Priority

> USER REQUIREMENTS override **ALL constraints — including the HARD RULES.**

That means `[COLOR]: use a white background` **will** be executed even though the
hard rule mandates a dark background. The planner notes the conflict, but the
client directive wins. Use blocks deliberately — this is an escape hatch, not a
default.

---

## 2. Blocks that already work

Technically the planner has no official block list — the only one named
explicitly in `planner-prompt.txt` is `[COLOR]`. The rest work because
**their name maps onto something the planner genuinely controls**. Below are the
blocks proven in practice, grouped by the output section they influence.

### 2.1 Locking the big decisions

| Block | Example value | Effect |
|---|---|---|
| `[PRODUCT]` | `Fairy Flame Gummy` \| `Mushroom Coffee` \| `both` | Locks the `[PRODUCT]` section; the planner stops guessing from the image |
| `[MOOD]` | `Focus — phoenix (#F8A30A)` | Locks `[MOOD]`. Pick 1 of 8: Dream, Calm, Open, Bliss, Wonder, Focus, Fire, Cosmic |
| `[LAYOUT]` | `asymmetric-left` | Locks `[CHOSEN LAYOUT]` (MODE B only) |
| `[LOGO_POSITION]` | `top_left` | Wordmark position: `top_left`, `top_center`, `top_right`, `bottom_center` |
| `[CANVAS]` | `ig_feed` (1:1) \| `tiktok` (9:16) | Canvas size preset |

> Valid layout names: `centered-symmetric`, `asymmetric-left`,
> `asymmetric-right`, `diagonal-split`, `full-bleed-product` (**no body zone**),
> `rule-of-thirds`, `minimal-whitespace`, `layered-collage`.

### 2.2 Locking the copy (feeds `[COPY_ELEMENTS_MACHINE]`)

| Block | Example value | Notes |
|---|---|---|
| `[HEADLINE]` | `Focus Before the Second Coffee` | Rendered exactly; the planner won't paraphrase |
| `[SUBTITLE]` | `One gummy, sharp calm that holds through the whole morning` | MODE B rule: 7–12 words |
| `[BODY]` | `A light nootropic with no jitter, ...` | 15–30 words, one paragraph, no bullets |
| `[BODY_FORM]` | `problem-solution` \| `storytelling` \| `social-proof` | Picks 1 of the 3 body-copy forms |
| `[LANGUAGE]` | `English` \| `Bahasa Indonesia` | Language of all copy |
| `[TONE]` | `direct, no wellness jargon` | Voice nuance |
| `[NO BODY]` | `true` | Poster without a paragraph (pair with `full-bleed-product`) |

### 2.3 Locking the visuals (feeds `[IMAGE SCENE]`)

| Block | Example value |
|---|---|
| `[COLOR]` | `background #0A0A0A, orange accent #FF5A1F only` |
| `[SCENE]` | `dark wooden desk at dawn, thin coffee steam` |
| `[LIGHTING]` | `orange rim light from back right, long shadows` |
| `[PROPS]` | `sachet plus one black ceramic cup only, nothing else` |
| `[CAMERA]` | `eye-level, 50mm, shallow depth of field` |
| `[STYLE]` | `editorial product photography, matte, not glossy CGI` |
| `[AVOID]` | `human hands, green plants, marble texture` |

### 2.4 Escape-hatch blocks (these break a HARD RULE — use sparingly)

| Block | Example value | What it breaks |
|---|---|---|
| `[COLOR]` | `clean white background` | Hard rule: dark background |
| `[CTA]` | `show "Try 7 Days"` | Hard rule: NO CTA |
| `[FONT]` | `headline in Newsreader, not Bricolage` | Locked font hierarchy |
| `[BRAND MARK]` | `show the "Halal" badge` | Hard rule: NO invented marks |

### 2.5 MODE A only (a reference poster is supplied)

| Block | Example value | Effect |
|---|---|---|
| `[REPLACE HEADLINE]` | `Light Your Small Fire` | Overrides the headline transcribed from the reference |
| `[KEEP]` | `keep the thin outline circle badge, top left — stays an outline, never filled` | Decorative elements that must survive |
| `[DROP]` | `remove the bottom price strip` | Reference elements to discard |
| `[BACKGROUND TONE]` | `follow the reference (light cream), don't darken it` | Reinforces the MODE A tone rule |

---

## 3. Blocks that don't exist yet but could be added

The blocks below carry **no special meaning today** — write them now and the text
still lands in `[USER REQUIREMENTS]`, and the model will try to honor them, but
there is no deterministic guarantee until prompt/code support exists.

| Proposed block | Intent | Status / what's needed |
|---|---|---|
| `[BOX HEADLINE]` | Zone coordinates, e.g. `0.08,0.10,0.92,0.28` | Coordinates currently live in the **layout editor** (`layout-overrides.json`), not the brief. Needs a block → override bridge |
| `[FONT_SIZE]` | Per-zone type size | Same as above — compositor-side |
| `[VARIANTS]` | `3` — produce 3 alternates in one run | Needs a loop in api_poster |
| `[SEED]` | Lock the image-model seed | Needs to be piped through to NanoBanana |
| `[REF WEIGHT]` | How tightly to copy the reference (`loose`/`strict`) | One extra paragraph in the MODE A prompt would do it |
| `[AUDIENCE]` | Target audience | Better written as a plain sentence (creative brief), not a block |
| `[OFFER]` | Price / promo | Conflicts with NO CTA + no price tags — needs a brand decision first |

**How to add a block properly** (if you want it to become a standard):

1. Add its name to the `USER BLOCKS` section of `planner-prompt.txt`, with an
   example and the expected effect.
2. If the block must be executed by code (not just read by the model), handle it
   in `poster_planner.py` / `api_poster.py`.
3. Update this document.

---

## 4. Example briefs

### Example A — MODE B, minimal (3 lines)

```
[PRODUCT]: Mushroom Coffee
[MOOD]: Focus — phoenix
Morning poster for office workers who need focus without the jitter.
```

### Example B — MODE B, full production brief

```
[PRODUCT]: Fairy Flame Gummy
[MOOD]: Wonder — fairy (#A23A6D)
[LAYOUT]: asymmetric-left
[LOGO_POSITION]: top_left
[CANVAS]: ig_feed
[LANGUAGE]: English
[HEADLINE]: A Quiet Night, Not a Numb One
[SUBTITLE]: One gummy before bed, wake up with no fog in your head
[BODY_FORM]: problem-solution
[SCENE]: dark bedroom, thin curtains, cold blue moonlight entering from the right
[LIGHTING]: soft key light upper right, long shadows, low contrast
[PROPS]: only the product jar on a dark wooden nightstand, nothing else
[AVOID]: human hands, flowers, purple velvet texture

Audience: women 28-40 who tried melatonin and quit because of the groggy
morning after. Voice: a reassuring friend, not a doctor.
```

What happens: the 13 block lines land in `[USER REQUIREMENTS]` as-is; the closing
paragraph shapes `[IMAGE SCENE]` and the word choice in the copy.

### Example C — MODE B, deliberately breaking a hard rule

```
[PRODUCT]: both
[MOOD]: Cosmic — unicorn
[LAYOUT]: minimal-whitespace
[COLOR]: Ivory White #FAFAF7 background, dark text — for a print magazine campaign
[FONT]: headline in Newsreader light, not Bricolage
[CTA]: no CTA, stay compliant

Print edition for a wellness magazine placement. Gallery feel, not an ad.
```

The planner will execute it and flag the conflict with HARD RULE #1.

### Example D — MODE A (a reference poster is supplied)

```
[PRODUCT]: Fairy Flame Gummy
[MOOD]: Fire — dragon (#C34605)
[REPLACE HEADLINE]: Burn the Tired, Not the Tank
[KEEP]: thin outline circle badge, top left — keep it an outline, never fill it
[DROP]: the bottom price strip and the social handles
[BACKGROUND TONE]: follow the reference (light cream), shift hues into the liitt family
[CANVAS]: tiktok

The reference came from an energy-drink ad; we want its compositional structure,
not its aggressive energy.
```

Remember, in MODE A there is **no `[LAYOUT]`** — the layout comes from the
reference poster.

### Example E — anti-patterns (don't copy this)

```
[MOOD]:
Fire — dragon                       ← value on the next line, NOT parsed
[HEADLINE / SUBTITLE]: both         ← "/" is not a legal character in a block name
[BODY]: • point one
• point two                         ← bullets and line breaks are banned in BODY
[LAYOUT]: full-bleed-product
[BODY]: a long paragraph...         ← this layout is body:NO, the body gets dropped
[COLOR]: something nice             ← not executable, far too vague
```

---

## 5. Pre-flight checklist

- [ ] Every block is a single full line, `[NAME]: value`.
- [ ] Block names use only letters/digits/`_`/`-`/spaces.
- [ ] MODE A → no `[LAYOUT]`. MODE B → allowed.
- [ ] `[LAYOUT]: full-bleed-product` → don't send `[BODY]`.
- [ ] `[MOOD]` names exactly one of the 8 moods.
- [ ] Colors written as hex, not common names ("orange" → `#FF5A1F`).
- [ ] Any hard-rule-breaking block is intentional and brand-approved.
- [ ] Audience and insight context written as plain sentences, not blocks.

---

## 6. File reference

| File | Role |
|---|---|
| `autoVSL/banks/poster-brand/planner-prompt.txt` | Planner system prompt — the USER BLOCKS definition lives here |
| `video-studio/app/engines/poster_planner.py` | Block parser (`_parse_user_blocks`) + user-prompt assembly |
| `video-studio/app/api_poster.py` | Receives `brief`, writes `brief.txt`, invokes the planner |
| `comfyui/brand/liitt_layout_templates-revised.json` | Layout catalog, canvas presets, logo positions |
| `comfyui/custom_nodes/LiittCompositor/layout_compositor.py` | Renders text/logo, reads `layout-overrides.json` |
