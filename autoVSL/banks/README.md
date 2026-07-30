# Banks — shared research memory for the creative pipeline

Canonical schemas live in **`.claude/skills/prospector/references/output-schemas.md`** — that file is the single source of truth. Do not redefine fields here or anywhere else.

| File | Contents | Written by | Queried by |
|---|---|---|---|
| `hooks.jsonl` | Verbatim hooks (scraped winners + our originals), classed and tagged | prospector, bloodhound | scriptwriter |
| `angles.jsonl` | Ranked ad angles with mechanism + evidence | prospector, bloodhound | scriptwriter |
| `pain-points.jsonl` | Pain-point matrix with VoC quotes, linked to avatars | prospector | scriptwriter, brand-architect |
| `broll.jsonl` | Tagged b-roll inventory + wishlist entries | editor (wishlist), prospector (references) | editor |

## Rules

- One JSON object per line (JSONL). Every entry has a stable `id` (`hk-`, `an-`, `pp-`, `br-` prefixes).
- `status` lifecycle: `untested → testing → proven | killed`. Only prospector Mode 4/5 or explicit human input changes status.
- Trend entries (from bloodhound) carry two extra fields: `expires` (ISO date, typically +7 to +14 days) and class `trend_jack`. Prospector Mode 5 sweeps expired entries to `status: "killed"`.
- Cross-references: pain-point → angle (`pain_ref`) → hook (`angle_ref`); scripts cite bank ids in frontmatter (`angle_refs`, `hook_refs`, `pain_refs`); campaign results flow back to those ids (see `docs/results-feedback.md`).
- `banks/*.jsonl` are version-controlled. Raw scrape dumps go to `research/raw/` (gitignored).
