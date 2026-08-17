#!/usr/bin/env python3
"""Poster Studio — vision planner (MODE B / NEW_GENERATION + MODE A / REPLACE).

Reads a product image + text brief — and, in --mode replace, a reference
poster to replicate — runs a vision LLM (headless claude CLI with the Read
tool, or fal.ai's openrouter/router/vision), and emits the structured text
the compositor + Nano Banana need:

    prompt         — full structured brief, [COPY_ELEMENTS_MACHINE] stripped
    copy_elements  — JSON-lines copy block for the compositor
    chosen_layout  — "[CHOSEN LAYOUT]\\n<template> | logo_position: <pos>"

Synchronous — this is a single fast LLM call; the server runs it inline.

    python poster_planner.py --provider claude --model sonnet \\
        --product-image <jpg> --brief-file <txt> \\
        --layout-json comfyui/brand/liitt_layout_templates-revised.json \\
        --prompt-file autoVSL/banks/poster-brand/planner-prompt.txt \\
        --claude-exe <claude> --out-dir <tmp>
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

GEMINI_APP = "openrouter/router/vision"
GEMINI_COST = 0.03

CHOSEN_LAYOUT_RE = re.compile(r"\[CHOSEN LAYOUT\][^\n]*\n\s*(?P<body>[^\n]+)")
LAYOUT_SPEC_RE = re.compile(
    r"(?P<template>[\w-]+)\s*\|\s*logo_position\s*:\s*(?P<position>\w+)"
)


def load_env(env_file: Path | None) -> None:
    if not env_file or not Path(env_file).is_file():
        return
    for line in Path(env_file).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _parse_user_blocks(text: str) -> list[tuple[str, str]]:
    pattern = re.compile(r"^\[([A-Za-z0-9_\- ]+)\]:\s*(.+)$")
    blocks = []
    for line in (text or "").strip().split("\n"):
        m = pattern.match(line.strip())
        if m:
            blocks.append((m.group(1).strip(), m.group(2).strip()))
    return blocks


def _describe_zone(box) -> str:
    if not box or len(box) < 4:
        return "?"
    x0, y0, x1, y1 = box[:4]
    cx = (x0 + x1) / 2
    if x0 < 0.05 and x1 > 0.95:
        h = "full-width"
    elif cx < 0.35:
        h = "left"
    elif cx > 0.65:
        h = "right"
    else:
        h = "center"
    if y1 < 0.30:
        v = "top"
    elif y0 > 0.70:
        v = "bottom"
    else:
        v = "mid"
    return f"{h}-{v}"


def _load_layout_templates(filepath: str) -> str:
    """Build the "AVAILABLE LAYOUT TEMPLATES" catalog. Direct path — no folder_paths."""
    path = Path(filepath.strip())
    if not path.is_file():
        raise RuntimeError(f"layout_templates file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    templates = data.get("templates")
    if not templates or not isinstance(templates, dict) or len(templates) == 0:
        raise RuntimeError(f"layout JSON has no 'templates' dict: {path}")

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
        lines.append(
            f"{idx}. {name}: "
            f"H={h_desc}, P={p_desc}, SubH={s_desc}, Body={b_desc}. "
            f"Logo: {logo_str}. {body_flag}"
        )
    print(f"[planner] loaded {len(templates)} layout templates from {path}", flush=True)
    return "\n".join(lines)


def _make_user_prompt(
    brief: str,
    has_object: bool,
    catalog: str,
    product_label: str,
    object_label: str = "",
    mode: str = "new",
    reference_label: str = "",
    aspect_desc: str = "",
) -> str:
    lines = []
    if reference_label:
        lines.append(reference_label)
    lines.append(product_label)
    if has_object:
        lines.append(object_label)
    lines.append("")

    if brief and brief.strip():
        blocks = _parse_user_blocks(brief)
        block_line_pattern = re.compile(r"^\[([A-Za-z0-9_\- ]+)\]:\s*.+$")
        creative_brief = ""
        if blocks:
            non_block_lines = [
                line for line in brief.strip().split("\n")
                if not block_line_pattern.match(line.strip())
            ]
            creative_brief = "\n".join(non_block_lines).strip()
        else:
            creative_brief = brief.strip()

        if blocks:
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
    elif mode == "replace":
        lines.append(
            "No extra brief supplied. Replicate the reference poster per the "
            "MODE A rules — its copywriting is the copy, its layout is the "
            "layout."
        )
    else:
        lines.append(
            "PRODUCT DESCRIPTION: [none supplied]. The product image itself "
            "is the only product information available. Use the liitt brand "
            "identity from the system prompt. Do not invent brand names, "
            "taglines, or marketing copy."
        )

    if mode == "replace" and aspect_desc:
        lines.append("")
        lines.append(
            f"TARGET CANVAS: {aspect_desc}. Normalize every "
            "[COPY_ELEMENTS_MACHINE] box coordinate to this canvas. If the "
            "reference poster's aspect ratio differs, adapt the layout "
            "proportionally — keep every element's relative position and "
            "relative size."
        )

    if catalog and catalog.strip():
        lines.append("")
        lines.append(catalog.strip())
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
        f"Active mode: {'REPLACE' if mode == 'replace' else 'NEW_GENERATION'}."
    )
    return "\n".join(lines)


def _extract_copy_elements(output_text: str) -> str:
    if "[COPY_ELEMENTS_MACHINE]" not in output_text:
        return ""
    parts = output_text.split("[COPY_ELEMENTS_MACHINE]")
    if len(parts) < 2:
        return ""
    section = parts[1].strip()
    m = re.search(r"\n\s*\[", section)
    if m:
        section = section[: m.start()]
    return section.strip()


def _strip_copy_elements(output_text: str) -> str:
    if "[COPY_ELEMENTS_MACHINE]" not in output_text:
        return output_text.strip()
    return output_text.split("[COPY_ELEMENTS_MACHINE]")[0].strip()


def _extract_chosen_layout(output_text: str) -> str | None:
    m = CHOSEN_LAYOUT_RE.search(output_text or "")
    if not m:
        return None
    body = m.group("body").strip()
    m2 = LAYOUT_SPEC_RE.search(body)
    if not m2:
        return None
    return f"[CHOSEN LAYOUT]\n{m2.group('template').strip()} | logo_position: {m2.group('position').strip()}"


def run_claude(claude_exe: str, model: str, system_prompt: str, user_prompt: str) -> str:
    env = os.environ.copy()
    env.pop("CLAUDECODE", None)   # allow a nested headless run
    env["PYTHONUTF8"] = "1"
    # The system prompt goes through stdin, NOT --append-system-prompt: the npm
    # claude.CMD shim dies with "The command line is too long." past ~8K chars,
    # and the planner rulebook is far bigger than that.
    cmd = [claude_exe, "-p", "--model", model, "--allowedTools", "Read"]
    payload = (f"<system_instructions>\n{system_prompt}\n</system_instructions>\n\n"
               f"{user_prompt}")
    try:
        r = subprocess.run(
            cmd, input=payload, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300, env=env,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("claude took too long to answer")
    out = (r.stdout or "").strip()
    if r.returncode != 0 or not out:
        raise RuntimeError(f"claude CLI failed (rc={r.returncode}): {(r.stderr or '')[:400]}")
    return out


def run_gemini(model: str, system_prompt: str, user_prompt: str,
               product_image: str, object_image: str | None,
               reference_image: str | None = None) -> str:
    if not os.environ.get("FAL_KEY"):
        raise RuntimeError("FAL_KEY not set — add it to autoVSL/.env")
    import fal_client

    client = fal_client.SyncClient()
    image_urls = []
    if reference_image:
        image_urls.append(fal_client.upload_file(str(reference_image)))
    image_urls.append(fal_client.upload_file(str(product_image)))
    if object_image:
        image_urls.append(fal_client.upload_file(str(object_image)))

    arguments = {
        "image_urls": image_urls,
        "prompt": user_prompt,
        "system_prompt": system_prompt,
        "model": model,
        "temperature": 0.35,
        "max_tokens": 4000,
    }
    try:
        result = client.run(GEMINI_APP, arguments)
    except Exception as e:
        # Thinking models (e.g. gemini-2.5-pro) refuse to run without
        # reasoning: "Reasoning is mandatory for this endpoint and cannot be
        # disabled." Retry with it on; reasoning tokens count against
        # max_tokens, so raise the budget too.
        if "reasoning" not in str(e).lower():
            raise
        arguments["reasoning"] = True
        arguments["max_tokens"] = 12000
        result = client.run(GEMINI_APP, arguments)
    return str(result.get("output", ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--provider", required=True, choices=["claude", "gemini"])
    ap.add_argument("--model", required=True)
    ap.add_argument("--product-image", required=True)
    ap.add_argument("--additional-object", default="")
    ap.add_argument("--mode", default="new", choices=["new", "replace"])
    ap.add_argument("--reference-image", default="")
    ap.add_argument("--aspect", default="ig_feed", choices=["ig_feed", "tiktok"])
    ap.add_argument("--brief-file", required=True)
    ap.add_argument("--layout-json", required=True)
    ap.add_argument("--prompt-file", required=True)
    ap.add_argument("--claude-exe", default="")
    ap.add_argument("--env-file", default="")
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    load_env(Path(args.env_file) if args.env_file else None)

    product_image = Path(args.product_image)
    if not product_image.is_file():
        print(f"ERROR: product image not found: {product_image}", flush=True)
        return 1
    reference_image: Path | None = None
    if args.mode == "replace":
        reference_image = Path(args.reference_image) if args.reference_image else None
        if not reference_image or not reference_image.is_file():
            print(f"ERROR: --mode replace needs --reference-image; "
                  f"not found: {args.reference_image}", flush=True)
            return 1
    brief_file = Path(args.brief_file)
    brief = brief_file.read_text(encoding="utf-8", errors="replace") if brief_file.is_file() else ""
    system_prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not system_prompt.strip():
        print(f"ERROR: prompt file empty: {args.prompt_file}", flush=True)
        return 1

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    replace = args.mode == "replace"
    # MODE A takes its layout from the reference — the catalog is MODE B only.
    catalog = "" if replace else _load_layout_templates(args.layout_json)
    aspect_desc = ("1080x1350 portrait (4:5 Instagram feed)"
                   if args.aspect == "ig_feed"
                   else "1080x1920 portrait (9:16 TikTok)")

    has_object = bool(args.additional_object) and Path(args.additional_object).is_file()

    if args.provider == "claude":
        if not args.claude_exe:
            print("ERROR: --claude-exe is required for the claude provider", flush=True)
            return 1
        if replace:
            reference_label = (
                f"REFERENCE POSTER — view it with the Read tool (this is the "
                f"design to replicate): {reference_image}"
            )
            product_label = (
                f"PRODUCT IMAGE — view it with the Read tool (this is the "
                f"product to feature): {product_image}\n"
                "MODE: REPLACE. Replicate the reference poster's design, "
                "layout, and copywriting featuring this product per the "
                "MODE A rules in the system prompt."
            )
        else:
            reference_label = ""
            product_label = (
                f"PRODUCT IMAGE — view it with the Read tool (this is the product "
                f"to feature): {product_image}\n"
                "No reference design was supplied. MODE: NEW_GENERATION. Design a "
                "complete original liitt-branded UGC product poster from the "
                "product_description below."
            )
        object_label = (
            f"ADDITIONAL OBJECT IMAGE — view it with the Read tool: "
            f"{args.additional_object}\nWeave it into the scene as a supporting "
            "prop beside, behind, or interacting with the main product - never "
            "a competing focal point."
        )
        user_prompt = _make_user_prompt(brief, has_object, catalog,
                                        product_label, object_label,
                                        mode=args.mode,
                                        reference_label=reference_label,
                                        aspect_desc=aspect_desc)
        output_text = run_claude(args.claude_exe, args.model, system_prompt, user_prompt)
        planner_cost = 0.0
    else:
        if replace:
            reference_label = "Image 1 = reference poster to replicate."
            product_label = (
                "Image 2 = product to feature. MODE: REPLACE. Replicate the "
                "reference poster's design, layout, and copywriting featuring "
                "this product per the MODE A rules in the system prompt."
            )
            object_label = (
                "Image 3 = additional object. Weave it into the scene as a "
                "supporting prop beside, behind, or interacting with the main "
                "product - never a competing focal point."
            )
        else:
            reference_label = ""
            product_label = (
                "Image 1 = product to feature. No reference design was supplied. "
                "MODE: NEW_GENERATION. Design a complete original liitt-branded "
                "UGC product poster from the product_description below."
            )
            object_label = (
                "Image 2 = additional object. Weave it into the scene as a "
                "supporting prop beside, behind, or interacting with the main "
                "product - never a competing focal point."
            )
        user_prompt = _make_user_prompt(brief, has_object, catalog,
                                        product_label, object_label,
                                        mode=args.mode,
                                        reference_label=reference_label,
                                        aspect_desc=aspect_desc)
        output_text = run_gemini(args.model, system_prompt, user_prompt,
                                 str(product_image),
                                 str(args.additional_object) if has_object else None,
                                 str(reference_image) if replace else None)
        planner_cost = GEMINI_COST

    if not output_text.strip():
        print("ERROR: planner returned empty output", flush=True)
        return 1

    copy_elements = _extract_copy_elements(output_text)
    prompt = _strip_copy_elements(output_text)
    chosen_layout = _extract_chosen_layout(output_text)

    if replace:
        # MODE A geometry comes from layout-overrides.json (derived from the
        # reference boxes); the template is only a carrier and must support
        # every zone, so force a body:OK template regardless of LLM output.
        chosen_layout = "[CHOSEN LAYOUT]\ncentered-symmetric | logo_position: top_center"
    elif chosen_layout is None:
        print("WARNING: no [CHOSEN LAYOUT] in output — compositor will fall back "
              "to the first template", flush=True)

    plan = {
        "provider": args.provider,
        "model": args.model,
        "mode": args.mode,
        "prompt": prompt,
        "copy_elements": copy_elements,
        "chosen_layout": chosen_layout,
        "created": time.time(),
    }
    if replace:
        plan["reference_image"] = str(reference_image.resolve())

    plan_path = out_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=1, ensure_ascii=False), encoding="utf-8")
    (out_dir / "brief.txt").write_text(brief, encoding="utf-8")
    (out_dir / "copy-elements.jsonl").write_text(copy_elements, encoding="utf-8")

    print(f"RESULT: {plan_path.resolve()}", flush=True)
    print(f"PLANNER_COST: {planner_cost}", flush=True)
    print("Done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
