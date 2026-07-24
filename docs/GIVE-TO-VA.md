# Giving Video Studio to a VA

**Do you need a backend server? No.** The backend is the Flask app + GPU engines,
and it can live in one of three places. Pick by what the VA's computer has:

| Option | VA's computer needs | Your PC | Cost | Best for |
|---|---|---|---|---|
| **A. Remote access (recommended)** | any laptop, even weak | must be ON while VA works | $0 | most VAs |
| **B. Full install on the VA's PC** | Windows + **NVIDIA GPU (4 GB+)** | not needed | $0 | a VA with a gaming/creator PC |
| C. Rented GPU cloud server | any | not needed | ~$150-300/mo | only if A and B both fail |

---

## Option A — VA works on YOUR backend (remote access)

Everything runs on your machine (GPU, weights, your fal key, your Claude login);
the VA just drives the web UI. The app already ships a PIN gate for this.

1. Install **Tailscale** (free) on your PC and on the VA's laptop; log both into
   the same tailnet (or use "Share" to invite their account).
2. Make sure Video Studio is running on your PC (the login autostart does this).
3. Give the VA: `http://<your-tailscale-ip>:5180` + the **remote PIN**
   (Settings page, or `video-studio/config.json` -> `remote_pin`).
4. Done — uploads, scripts, dubs, captions, exports all work from their browser.
   Heavy jobs run on YOUR GPU, one at a time (the job queue handles overlap).

## Option B — Full install on the VA's PC (this package)

Their machine must be Windows with an NVIDIA GPU (4 GB VRAM or more).

**You (once):** run `install\make-va-package.ps1` — builds
`Desktop\VideoStudio-VA-package.zip` (~3 GB: code + model weights, NO venvs, NO
uploads/outputs, NO .env/fal key). Send it however you like.

**The VA:**
1. Unzip anywhere (e.g. `C:\VideoStudio`). Path must not need admin rights.
2. Open the unzipped folder -> `install\` -> right-click `setup-machine.ps1`
   -> **Run with PowerShell**. It checks Python 3.11 / GPU / ffmpeg, rebuilds
   the three venvs from pinned requirements, patches the known gotchas, rewrites
   `config.json` for their paths, generates fresh secrets + PIN, installs the
   desktop icon, and boots the server. 20-40 min mostly pip downloads.
3. Open the **Video Studio** desktop icon.

**Keys & accounts on the VA machine (decide deliberately):**
- `FAL_KEY` (paid cloud dub / Image-to-Video / fit-extend): the setup prompts
  for it. If you give the VA YOUR key, their runs spend YOUR fal balance —
  every spend still shows a confirm dialog with the price first.
- AI script rewrite (the ✨ / 🔥 liitt buttons) shells out to **Claude Code** —
  the VA needs it installed and logged in on their machine, or that one feature
  errors (everything else works).
- First transcription auto-downloads the whisper model (~1.5 GB, one time).

## Option C — Cloud GPU server

Rent a Windows GPU box (e.g. Paperspace/Shadow), run Option B's setup on it,
both of you connect by remote desktop or Tailscale. Only worth ~$150-300/mo if
the VA has no GPU AND your PC can't stay on.
