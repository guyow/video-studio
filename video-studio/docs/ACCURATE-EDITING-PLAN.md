# Plan: accurate, localized image editing (stop changing the whole picture)

Research + plan, 2026-08-03. Nothing built yet — this is for approval.

---

## 1. Why it keeps changing the whole image

Nano Banana (Gemini) is a **generative re-render**, not an editor. It has **no mask input** in its API — verified against fal's OpenAPI: the only fields are `prompt`, `image_urls`, `num_images`, `aspect_ratio`, `output_format`, `safety_tolerance`, `seed`. Every call reconstructs the entire frame from its own understanding of your photo. "Keep everything else exactly as it is" is a *suggestion to a generator*, and it will never be reliable.

My stamp-back mitigation helped but is structurally limited:
- it needs you to drag a box first;
- a **rectangle is not the shape of the object**, so the fix is coarse;
- inside the box, the content is still a re-render whose lighting/grain may not match;
- if the model shifts framing, inside and outside don't line up.

**The fix is not a better prompt or a better composite. It's a different class of model.**

## 2. The right architecture

> **segment → mask → mask-native model → verified composite**

A mask-native (inpainting) model takes `image + mask` and, by construction, only writes inside the mask.

### Evidence I ran today

Loaded `big-lama.pt` (already on disk at `tools/vsr/backend/models/big-lama/`, used by Subtitle Studio's eraser) in the cv venv:

```
big-lama loaded 0.7s
inpaint 4.1s on CPU, out (1, 3, 1280, 720)
outside the mask identical (model output raw): True   ← bit-exact, no compositing needed
```

Erasing the caption band from the UGC frame worked and **every pixel outside the mask was byte-identical** — guaranteed by the model, not by a trick. The only flaw was leftover black at the edges, because I hand-typed a sloppy rectangle mask that didn't cover the caption's rounded corners.

**Conclusion: the model class is right; mask accuracy is what decides quality.**

## 3. What's available (verified against fal's OpenAPI today)

### Mask makers
| Model | Input | Price | Note |
|---|---|---|---|
| `fal-ai/evf-sam` | **text** ("the coffee cup") | $0.005 | grounding-dino + SAM; has `expand_mask`, `blur_mask`, `fill_holes` built in |
| `fal-ai/sam2/image` | box or point prompts | ~$0.005 | precise object mask from the box you already drag |
| `fal-ai/sam-3/image` | text/box | $0.005 | newest generation |

### Mask-native editors
| Task | Model | Price | Why |
|---|---|---|---|
| **Erase** | **local `big-lama`** | **$0** | already on disk, 4s CPU, provably mask-only |
| Erase (hard) | `fal-ai/bria/eraser` | $0.04 | mask-native, commercially licensed |
| **Replace / Add** | `fal-ai/flux-pro/v1/fill` | $0.05/MP | true inpainting, only masked pixels |
| Replace (cheap) | `fal-ai/bria/genfill` | $0.04 | mask-native generative fill |
| Text / label edits | `fal-ai/ideogram/v3/edit` | mask-native | best at rendering text |
| Whole-image restyle | keep **Nano Banana** | $0.039 | correct tool when you *want* a re-render |

Confirmed mask fields: `flux-pro/v1/fill.mask_url`, `bria/eraser.mask_url`+`mask_type`, `bria/genfill.mask_url`, `ideogram/v3/edit.mask_url`, `lama.mask_image_url`, `flux-lora/inpainting.mask_url`.

## 4. The pipeline to build

**Stage 1 — Mask.** Type what to change ("the black caption box"). EVF-SAM returns a pixel-accurate mask. You see it as a coloured overlay and can grow/shrink/invert it **before anything is spent**. Fallback: the box you drag → SAM2. Manual brush as last resort.

**Stage 2 — Route by task.** Erase → local big-lama (free). Replace/Add → FLUX Fill or Bria GenFill. Restyle whole photo → Nano Banana. The app picks; you can override.

**Stage 3 — Deterministic composite.** Even when a cloud model returns a full frame: align it to the source (ECC / ORB homography) if size or framing moved, composite through the **object** mask (dilated + feathered — not a rectangle), and match statistics in a ring around the seam (or `cv2.seamlessClone`) so lighting and grain agree.

**Stage 4 — Prove it.** Diff the result against the source *outside* the mask. Report the number: "0.0% of pixels outside the mask changed." If it exceeds a threshold, **reject the take automatically** and say why. Optional diff heat-map.

## 5. Phases

| Phase | What | Cost to run | Done when |
|---|---|---|---|
| **1** | Local mask tools (brush + threshold-from-box) → **big-lama erase** → verify+prove | **$0** | caption/logo/object erase, outside bit-exact, proven by the diff number |
| **2** | EVF-SAM text→mask + mask preview/adjust UI before spending | $0.005/edit | you type "the cup", see the mask, approve |
| **3** | FLUX Fill / Bria GenFill for replace + add, through the same composite+verify | ~$0.05/edit | swap a product in, rest of frame untouched |
| **4** | Auto-routing + Nano Banana demoted to "restyle whole photo" | — | one box, right engine chosen for you |

Phase 1 alone fixes the complaint, for free, with no API involved.

## 6. Risks / open questions

- **Mask quality on hair, motion blur, transparency** — SAM is strong but not perfect; the preview + grow/shrink control is the mitigation.
- **big-lama is best at removal**, not invention. It rebuilds background convincingly; it will not paint you a new product. That's what FLUX Fill is for.
- **Large regions**: LaMa works on crops around the mask; very large areas need tiling — 4.1s full-frame CPU here, so this is a perf detail, not a blocker.
- **FLUX Fill bills per megapixel** — a 4K edit costs meaningfully more than a 1K one; the estimate must reflect real pixels.
- Nano Banana stays useful; it's just no longer the default for "change this one thing."

## 7. Recommendation

Build **Phase 1 now** — it's free, uses assets already on this machine, and turns "don't change my picture" from a hope into a guarantee the model enforces. Then Phase 2 (half a cent per mask) is the quality-of-life jump: describe the object instead of drawing it.
