# Video Studio — desktop launcher

Makes Video Studio behave like installed software on this PC. No terminal, no
browser tabs: double-click the icon → the server boots hidden → the UI opens in
its own app window (Edge/Chrome `--app` mode, own taskbar entry).

## Files
- `install-shortcuts.ps1` — run once (or anytime, to repair): creates
  **Desktop → Video Studio** and **Start Menu → Video Studio** (+ *Stop Video
  Studio*) shortcuts with the brand icon.
- `Video Studio.vbs` → `start-video-studio.ps1` — silent launcher. Reuses a
  healthy server; if the port is bound but still booting it WAITS (never
  double-spawns); otherwise starts `video-studio/app/server.py` hidden under
  `autoVSL/.venv` and opens `http://localhost:5180` as an app window.
- `Stop Video Studio.vbs` → `stop-video-studio.ps1` — kills every python whose
  command line runs this app's server.py (venv shim parent + real interpreter
  child + Flask reloader pairs), so nothing respawns or lingers on :5180.
- `video-studio.ico` — generated from the brand wordmark.

## Notes
- The heavy engines (XTTS, Wav2Lip, whisper, ffmpeg, ComfyUI) stay where they
  are — the launcher only manages the web app process. Paths are resolved
  relative to this folder, so the repo can move as one unit.
- Start at login (optional): create a Logon task in Task Scheduler pointing at
  `wscript.exe "<this folder>\Video Studio.vbs"`.
- Phone access on the LAN keeps working as before (config `lan_access`).
