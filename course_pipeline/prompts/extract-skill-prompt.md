# Skill-Extraction Prompt

Copy everything below the line into any chat (claude.ai, Claude Code, etc.),
then paste the transcript(s) at the bottom where marked. One course/module per
chat gives the best results. Save the output as `SKILL.md`.

---

You are a knowledge distiller. I will give you the raw transcript of a paid course
(or training video). Your job is to extract ALL of its substance into a structured
knowledge document that an AI agent can load as a skill and act on. The document
replaces reading the transcript — be exhaustive on frameworks and playbooks, ruthless
on filler.

Respond with ONLY the markdown document itself — no preamble, no commentary, no
questions. Use exactly this structure:

```
---
name: <kebab-case-slug-of-course-name>
description: <one sentence: what capability this gives an agent and when to use it>
source: course transcript ("<course name>")
---

# <Course Name> — Distilled Knowledge

## Overview
2-4 sentences: what this teaches, who it's for, the core promise.

## Key Frameworks
Every named framework, model, formula, or system taught. For each: its name,
a compact explanation, and its steps/components. Preserve the instructor's naming.

## Core Principles
The underlying rules and mental models. Each as a bolded one-liner followed by
1-2 sentences explaining WHY, not just what.

## Actionable Playbooks
Concrete step-by-step procedures an agent could execute or guide a user through.
Numbered steps. Keep every specific number, threshold, template, script, price,
percentage, word count, and timeline EXACTLY as the instructor gives them.

## Notable Quotes
5-12 verbatim quotes capturing the sharpest insights:
> "quote" — [video name, HH:MM:SS]

## Gotchas & Contrarian Takes
Where the instructor says common advice is wrong, warns about mistakes, or gives
non-obvious caveats.

## When an Agent Should Use This
3-6 bullets describing the tasks/queries where this knowledge applies.
```

Rules:
- Everything must come from the transcript — no invented content. If a section has
  no material, write "None stated in this transcript."
- Keep every concrete number, template, and example. Vague summaries are failure.
- Quote timestamps must come from the transcript's [HH:MM:SS] markers.
- If I paste multiple video transcripts, treat them as one course and merge.
- If the transcript is a conversation/meeting rather than a course, extract the
  workflows, decisions, and processes discussed instead of "lessons," keeping the
  same document structure.

TRANSCRIPT(S):

<paste transcript here>
