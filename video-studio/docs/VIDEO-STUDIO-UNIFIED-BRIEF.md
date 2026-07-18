# Video Studio Unified Brief

## 1. Goal
Build one local-first Windows app called Video Studio, running at http://localhost:5180, that unifies subtitle recovery, transcript verification, dubbing, lip-sync, captions, QA, exports, Ads Factory, and Power Tools.

## 2. Current Context
The user is building a local computer workflow that uploads a video, transcribes it, removes subtitles, verifies transcript spelling, generates a new script, and then dubs and lip-syncs the result into a new video. The user also wants subtitle-removal to fit into an auto VSL pipeline.

## 3. Safe Rule
Do not overwrite or delete Subtitle Studio or AutoVSL yet. First inspect existing folders, scripts, routes, dependencies, static pages, and output structures.

## 4. Target Modules
- Library
- New Project
- Transcript & Script
- Subtitle Recovery
- Dubbing & Lip Sync
- DubSync Repair
- Captions
- QA Review
- Exports
- Ads Factory
- Power Tools

## 5. Migration Plan
1. Inventory existing projects.
2. Choose the safest base project.
3. Reuse working scripts and folders.
4. Build one new app shell.
5. Add the job queue.
6. Add media workflows.
7. Test on localhost.
8. Migrate features one by one.

## 6. Architecture
Suggested structure:
- app.py
- core/
- pipelines/
- static/
- templates/
- projects/
- outputs/
- docs/

## 7. Claude Prompt
Read this file first. Inspect sibling project folders in:
C:\Users\guyas\Claude\Projects\Video AI editing

Create only:
- docs/PROJECT-INVENTORY.md
- docs/MIGRATION-PLAN.md
- docs/ARCHITECTURE.md

Do not modify old projects yet. Summarize the safest migration order and tell me which project should be the base.

## 8. Next Step
Create the inventory and plan before writing code.
