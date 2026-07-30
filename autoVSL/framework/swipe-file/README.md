# Short Form Ads Swipe File

Proven, highly successful ad scripts — the source formulas that Stage 4 rewrites
against. **This folder needs to be populated** from the existing Short Form Ads
Swipe File (currently linked in Notion).

## Format: one script per file

`<brand-or-name>.md`:

```markdown
---
name: petlab-itchy-ears
brand: PetLab
result: 97M views
length: 60 sec
formula: emotional-pet-transformation
hook_type: emotional-story
---

# [Hook line]

[Full script, line by line]

## Why it works (psychology notes)
- [beat-by-beat breakdown, if available]
```

The frontmatter matters: when Stage 4's angle-matching prompt runs, the whole
folder gets pasted in, and later the script agent will query by `formula` /
`hook_type` instead of pasting everything.

## To populate
Export each script from the Notion swipe file into its own file here. Performance
data (views, revenue, length) goes in frontmatter.
