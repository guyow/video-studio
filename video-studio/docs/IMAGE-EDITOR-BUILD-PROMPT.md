# BUILD PROMPT — Image Editor tab (Nano Banana via fal.ai)

Paste this whole file into Claude Code with the repo `Video AI editing/video-studio` open.

---

## What I want

Add a new tab to Video Studio: **🖼 Image Editor** at `/image-editor`.

I upload an image (or pick one already in the app) and then, in plain English:

1. **Replace an object** — "replace the cup with a liitt gummy jar", "swap the background to a bathroom shelf", "change the label text to FLAME".
2. **Erase an object** — remove the logo / the person in the back / the price sticker, and fill the hole so it looks like it was never there.
3. **Replace using a reference photo** — upload a second image (my real product) and say "put THIS jar on the table, same lighting". Nano Banana takes multiple input images, so this must be supported.
4. **Change the format easily** — one click to get 9:16, 4:5, 1:1, 16:9 versions, plus file type (png/jpg/webp) and pixel size. Cropping/padding/converting must be **free and local**; only the AI reframe (outpaint) costs money.

Everything runs through **fal.ai Nano Banana** (Google Gemini image model) using the `FAL_KEY` already in `autoVSL/.env`.

---

## Non-negotiable: follow the patterns this repo already has

Do **not** invent a new architecture. Copy the shape of the existing Image → Video feature:

| Concern | Copy from |
|---|---|
| fal.ai engine script | `app/engines/i2v_gen.py` (fal_client + httpx + `--env-file` loader + `load_env` + balance pre-flight upload) |
| Server endpoints + cost gate | `server.py` §"Image -> Video (fal.ai)" ~line 3469 (`/api/i2v/{models,upload,estimate,run,list}`) |
| 402 confirm-cost flow | `api_i2v_run` → `{"needs_confirm": True, "estimate": est}, 402` unless `b["confirm_cost"]` |
| Spend ledger | `run_i2v_job` → `record_spend(slug, est)` and the 💰/🧾 log lines |
| Job registration | `jobs_create(action, slug, label)` + `threading.Thread(target=..., daemon=True)`; **no `gpu=True`** — these are API calls, they must not take the GPU lock |
| Path containment | `img = (ROOT / rel).resolve()` then `if not str(img).startswith(str(UPLOADS.resolve())): abort(400)` |
| Front-end page | `app/static/image-to-video.html` — same dark theme, `vs.css` classes (`vs-btn`, `vs-ta`, `vs-sel`, `vs-hint`), `vs-core.js` (`VS.api`, `VS.post`, `VS.toast`), same live job-log watcher |
| Tab registration | add `["🖼 Image Editor", "/image-editor", ["/image-editor"]]` to the `TABS` array in `app/static/vs-nav.js`, right after Image → Video |

Python runs under the **cv venv** (`CONFIG["venvs"]["cv"]`) — it already has `fal_client`, `httpx`, and Pillow.

---

## VERIFY THE fal.ai API BEFORE YOU WRITE THE ENGINE

Do not trust model IDs or parameter names from memory. Before coding, check the current fal.ai docs/playground for the Nano Banana family and confirm the exact endpoint ids, input field names, and output shape. Expected (verify each):

- `fal-ai/nano-banana/edit` — image editing, cheap/fast. Inputs roughly: `prompt`, `image_urls` (array of URLs), `num_images`, `output_format`. Output: `images[].url` (+ a text `description`).
- `fal-ai/nano-banana` — text→image (no input image).
- `fal-ai/nano-banana-pro/edit` and `fal-ai/nano-banana-pro` — higher quality tier; this one is the tier that natively takes `aspect_ratio` and a `resolution` (1K/2K/4K). Use it for the AI-reframe path.

Write the registry so a wrong guess is a one-line fix:

```python
MODELS = {
  "nano-banana":      {"label": "Nano Banana — fast & cheap (default)", "endpoint": "...",       "edit_endpoint": "...", "cost_per_image": 0.04, "aspect_param": False},
  "nano-banana-pro":  {"label": "Nano Banana Pro — best quality",        "endpoint": "...",       "edit_endpoint": "...", "cost_per_image": 0.15, "aspect_param": True},
}
```

Mirror this registry server-side (like `I2V_MODELS` mirrors the engine's `MODELS`) so `/api/img/estimate` never has to spawn Python. Label every price in the UI as an **estimate — fal's invoice is authoritative**, same wording as the i2v tab.

**Important truth to design around:** Nano Banana edits are *prompt-driven*, not mask-driven. There is no true inpaint mask. So "erase this object" is achieved with a well-written instruction, not with a brush. Build the UI around that honestly — do not fake a mask tool and pretend it's pixel-accurate.

---

## Modes (the right panel)

Five mode tabs. Each one builds the final prompt from a template + my words, so I never have to know how to prompt this model.

### 1. Replace
- Text: "what should change" → `"Replace the {thing} with {replacement}."`
- Optional reference image upload → appended as a second entry in `image_urls`, with prompt text `"Use the second image as the exact reference for the replacement object — match its shape, colour, label and branding."`
- Always append the preservation clause: `"Keep everything else in the image exactly as it is — same camera angle, same lighting, same shadows, same background, same composition. Only change what was asked."`

### 2. Erase
- Text: what to remove → `"Remove the {thing} completely from the image. Rebuild whatever was behind it so the result looks natural and untouched, matching the surrounding texture, lighting and shadows. Leave no trace, outline or blur where it used to be."` + preservation clause.

### 3. Add
- `"Add {thing} to the scene, {where}. Match the existing lighting direction, shadow softness, perspective and colour grade so it looks photographed, not pasted."` + preservation clause.

### 4. Retouch / Style
- Free-text plus quick chips: *brighten*, *remove background → white*, *clean up skin*, *make it look like a real phone photo*, *remove text/watermark*, *change label text to…*.

### 5. Format  ← the "change format easily" one
Two sub-modes, and the free one is the default:

- **Crop / convert (FREE, local, instant)** — Pillow in the cv venv. Choose one or many presets, hit **Export all**, get every size at once. Presets:

  | Preset | Ratio | Pixels |
  |---|---|---|
  | Reel / Story / TikTok | 9:16 | 1080×1920 |
  | IG feed | 4:5 | 1080×1350 |
  | Square / Meta ad | 1:1 | 1080×1080 |
  | Landscape / YouTube | 16:9 | 1920×1080 |
  | Pinterest | 2:3 | 1000×1500 |
  | Shopify product | 1:1 | 2048×2048 |

  Plus: file type (png / jpg / webp), quality slider, max-dimension cap, and a live output-size readout. Center-crop-to-fill by default with a draggable focus point, mirroring what `/api/export-aspects` does for video.

- **AI reframe / outpaint (costs money)** — when cropping would cut off the product. Pad the source onto the target canvas locally with Pillow (transparent or mid-grey fill, subject positioned by the focus point), send that padded image to Nano Banana Pro with `"Extend this image naturally to fill the empty area. Continue the existing background, lighting and perspective seamlessly. Do not alter, move, crop or restyle the original subject."` and, where the model supports it, also pass the native `aspect_ratio`. This must go through the 402 cost gate like every other paid call.

---

## Region hint (optional, be honest about it)

Let me drag a box on the canvas to point at the object. Convert it into **words** appended to the prompt — e.g. `"The target object is in the lower-left area of the image, roughly one third from the bottom."` Derive the phrase from the box's normalized center + size.

Add a checkbox **"send a marked copy to help it aim"**: when on, generate a second image with a translucent highlight over the box and pass both, telling the model the marker shows the target and must not appear in the output. Off by default. Label it clearly as a hint, not a mask — do not promise pixel accuracy.

---

## Versions, undo, and chaining (this is the part that makes it usable)

Every image lives in a workdir:

```
output/images/<slug>/
  v00.png          # the original upload, never modified
  v01.png v02.png  # each edit result
  thumb-v01.jpg    # small preview for the filmstrip
  edits.json       # [{version, parent, mode, user_text, final_prompt, model, refs[], cost, created}]
```

- Editing **always writes a new version** — nothing is ever overwritten. Undo = click an earlier thumbnail.
- Editing continues **from the currently selected version**, so I can stack: erase the logo → replace the cup → reframe to 9:16.
- Filmstrip of versions down the left with mode + prompt on hover.
- Delete uses the existing `soft_delete()` (goes to `.trash`), never a hard `unlink`.
- `num_images` 1–4 → show variants side by side, I pick one, the picked one becomes the new version and the rest are kept as `v03-alt1.png` etc.

---

## Endpoints to add (namespace `/api/img/`)

```
GET  /api/img/models                 -> registry + labels + per-image cost
POST /api/img/upload                 -> multipart; saves to output/images/_uploads; returns rel path
                                        allow .png .jpg .jpeg .webp, cap ~20 MB, secure_filename
POST /api/img/estimate               -> {model, num_images, mode} -> {this_run, summary, engine:"fal-image"}
POST /api/img/run                    -> 402 {needs_confirm, estimate} unless confirm_cost; else spawns job
GET  /api/img/list                   -> gallery: slugs, latest version, thumb, mode, cost, job status
GET  /api/img/item/<slug>            -> edits.json + version list (for the filmstrip)
POST /api/img/format                 -> FREE local crop/convert, no job, no cost, returns written files
POST /api/img/send                   -> copy a version into output/i2v/_uploads (Image→Video) or EXPORTS_DIR
POST /api/img/delete                 -> soft_delete a version or a whole slug
GET  /image-editor                   -> send_from_directory(STATIC, "image-editor.html")
```

Job action string: `"imgedit"`. Label: `f"Image edit — {slug} [{mode}]"`.

Also register images in the existing exports aggregator (`/api/exports`, `_export_item`) as kind `"image"` so **Send to Desktop** works with zero new UI.

---

## Front-end: `app/static/image-editor.html`

Single page, classic-JS module style (no framework — match `image-to-video.html`).

```
┌ versions ┬──────── canvas ────────┬──── controls ────┐
│ v00 ▣    │                        │ [Replace][Erase] │
│ v01 ▣    │   big preview, drag a  │ [Add][Style][Fmt]│
│ v02 ▣    │   box to point at an   │                  │
│ + upload │   object; before/after │ what to change:  │
│          │   slider on hover      │ [textarea]       │
│          │                        │ [+ reference img]│
│          │                        │ model ▾  count ▾ │
│          │                        │ est: ~$0.04      │
│          │                        │ [✨ Apply edit]  │
└──────────┴────────────────────────┴──────────────────┘
   gallery of past images along the bottom
```

Requirements:
- Live cost estimate updates as model/count changes; the **Apply** button shows the price and pops the confirm on the 402 — identical UX to the i2v tab.
- Log watcher streaming the job log while it runs (reuse the i2v page's poller).
- Before/after compare on the canvas (slider or press-and-hold).
- "✨ Improve my prompt" button → local Claude CLI via the existing `/api/copywrite` pattern, rewriting my rough words into a full descriptive edit instruction. Free, no fal call.
- Download button per version + "Send to Image → Video" + "Send to Desktop".
- Keyboard: `Ctrl+Z` selects the previous version, `Enter` in the textarea applies.

---

## Guardrails

- `FAL_KEY` never reaches the browser. All fal calls happen in the engine subprocess.
- Balance/auth pre-flight before any paid call (copy the `fal_client.upload(b"ok", ...)` trick from `i2v_gen.py`) so a locked account fails instantly instead of mid-render.
- Validate `mode`, `model`, `num_images` (1–4) server-side; reject unknown values with 400.
- Every filesystem path resolved and contained under `output/images` before use.
- If fal returns no image, exit non-zero with the response snippet in the log — no silent empty version.
- Never delete or modify `v00`.

---

## Definition of done — verify, don't assume

Run these and paste real output:

1. Server starts on 5180, `/image-editor` returns 200, the tab shows in the nav on every page.
2. `POST /api/img/upload` with a real jpg → file lands in `output/images/_uploads`.
3. `POST /api/img/run` **without** `confirm_cost` → 402 with a sane estimate. (Free to check.)
4. `POST /api/img/format` → real files on disk at the exact preset pixel dimensions; confirm with a Pillow/ffprobe size read. Confirm it cost $0 and created no job.
5. Path-escape attempt (`../../etc/passwd`) → 400.
6. Existing smoke harness (`scratchpad/smoke.py`) still green; Creator, Editor, Image → Video pages unaffected.
7. **Then stop and ask me before spending money.** One real Nano Banana edit is only a few cents, but I decide when the first paid call happens. When I say go: run one erase and one replace on a real photo, show me the before/after and the logged cost, and confirm `record_spend` incremented the fal ledger.

Report honestly what is real vs. stubbed. If a fal parameter turned out different from the guesses above, say so and show the corrected schema.
