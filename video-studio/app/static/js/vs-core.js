/* vs-core.js — shared plumbing for the one-page Creator.
   ONE poller drives everything: /api/jobs every 3s, /api/creator/library every
   3rd tick or instantly when a job finishes. Steps subscribe to events and
   never own timers. */
(function () {
  const VS = window.VS = {
    state: {
      videos: [], characters: [], tags: [], fal_spend: 0,
      jobs: [],
      open: null,          // {video, stepId, details:{}}
      settings: null,
    },
    steps: [],             // registered step modules, sorted by order
  };

  // ── tiny helpers ───────────────────────────────────────────────────────────
  VS.$ = (sel, root) => (root || document).querySelector(sel);
  VS.$$ = (sel, root) => [...(root || document).querySelectorAll(sel)];
  VS.esc = s => String(s ?? "").replace(/[&<>"']/g,
    c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  VS.fmtSize = b => b > 1048576 * 900 ? (b / 1073741824).toFixed(1) + " GB"
    : b > 900000 ? (b / 1048576).toFixed(1) + " MB" : Math.round(b / 1024) + " KB";
  VS.fmtT = s => Math.floor(s / 60) + ":" + (s % 60).toFixed(1).padStart(4, "0");
  VS.fmtAgo = ts => {
    const d = (Date.now() / 1000 - ts);
    if (d < 3600) return Math.max(1, Math.round(d / 60)) + "m ago";
    if (d < 86400) return Math.round(d / 3600) + "h ago";
    return Math.round(d / 86400) + "d ago";
  };

  /* job logs are written for debugging (full commands, exe paths, argument
   * dumps). UIs show only the human lines by default — the raw log stays one
   * toggle away. Shared here so the drawer and the timeline editor agree. */
  VS.friendlyLine = l => {
    l = String(l || "").trim();
    if (!l) return "";
    if (l.charAt(0) === "+") return "";
    if (/^[A-Za-z]:[\\/]/.test(l)) return "";
    if (/ffmpeg\.exe|ffprobe\.exe|python\.exe/i.test(l)) return "";
    if (/^arguments?:/i.test(l)) return "";
    if (/^\[dashboard\]/.test(l)) return "";
    if (/^(File |Traceback|  at )/i.test(l)) return "";
    return l.replace(/^\[seq\]\s*/, "");
  };
  VS.friendlyTail = (lines, n) => {
    const out = [];
    (lines || []).forEach(l => {
      const c = VS.friendlyLine(l);
      if (c) out.push(c);
    });
    return out.slice(-(n || 3));
  };

  // the universal "make it liitt" conversion — shared by the Creator's Script step
  // and the Editor so the prompt can't drift between the two.  Send with
  // {brand:true, slug:"fairy-flame"} so /api/copywrite grounds it in offer.md + banks.
  VS.LIITT_PROMPT = `CONVERT this script — whatever brand or product it currently sells — into a script for liitt's Fairy Flame gummies. It's a WINNING script being adapted, so BLEND, don't rebuild: keep as much of the original wording, rhythm and sentence flow as possible — change ONLY what must change to make it Fairy Flame. The result must read as the same script, same person, same energy — now selling Fairy Flame.
- Identify the brand/product being sold and replace it with Fairy Flame (by liitt). Remove every trace of the old brand, its product format, and its category language — nothing of the old product may survive.
- Re-map the product context intelligently, don't just delete it: the old format/ritual/dosing (drinks, cans, pills, powders, drops, smoking, coffee, whatever) → one flame-shaped gummy from the pouch; its benefit/mechanism claims → the Fairy Flame state-shift: lighter mood, clarity, focus, feeling like yourself again.
- CTA rule: NEVER speak a URL or domain — no "dot com", no "go to …". Point to the on-screen link instead: "the link is down below", "tap the link below", "grab yours at the link below", or a fresh natural variant that fits the script's voice.
- Keep the actives general; never name specific actives.
- KEEP THE HOOK: same opening pattern, same emotional beats, same rhythm and energy — that structure is why the script wins. Adapt its content to liitt, never flatten it into a generic ad.
- Compliance: no disease/medical claims, no cure/treat/heal language, no guaranteed outcomes, never promise a high or intoxication (this is sub-perceptual). Personal-experience framing ("I felt…") is fine.
- Stay within ±10% of the original word count — the lip-sync depends on it.
- Tone: premium, clean, intimate — not hypey, not stoner culture.`;

  // optional add-on: open like breaking news (checkbox in both Script UIs)
  VS.NEWS_HOOK_PROMPT = `NEWS-ANGLE HOOK: open the script like legit breaking news the viewer is catching in real time — e.g. "This is actually real news…", "It's official…", "While you were scrolling, something big happened…", "They banned it for 50 years. Now the labs want it back." Anchor it to the real news themes around this product's world: university researchers studying it, veterans asking Congress for access, laws loosening state by state, athletes crediting their recovery — while OBEYING the wording restrictions (if Meta-safe rules are given, the news beat must not use a restricted word either). Land the news beat in the first 1–2 lines, then hand off smoothly into the script's existing story — the rest keeps its structure and beats.`;

  // optional add-on: dodge Meta's restricted words with creative compliant wording
  VS.META_SAFE_PROMPT = `META-SAFE WORDING (this ad must survive Meta/Facebook ad review): the words psychedelic, psilocybin, magic mushroom(s), shrooms, microdose / microdosing, trip / trippy, high, and any drug slang are RESTRICTED — never write any of them, even where other instructions or the product doc say "microdose gummies". Play around the restriction creatively instead: "one tiny flame-shaped gummy", "Fairy Flame gummies", "a certain little ingredient scientists keep studying", "nature's mood molecule", "the ingredient we can't name on this app", or the knowing wink ("we can't say the word here — but you know the one"). Plain "mushroom" in a general functional sense is low-risk; "magic mushroom" never. News framing stays indirect: "a compound banned for 50 years is back in the lab", "researchers at a top university", "veterans are asking Congress for access" — say the news without the restricted noun. And no disease-treatment claims (never promise to treat depression/PTSD/anxiety; "for the days you feel flat" framing is fine).`;

  // 🍦 Vanilla safe — the OPPOSITE of a creative rewrite. Keeps the script the user
  // already likes and only swaps the words that get ads flagged, each for its nearest
  // safe neighbour, so the rhythm (and therefore the lip-sync) survives untouched.
  // Used as a standalone one-press pass in both Script UIs.
  VS.VANILLA_PROMPT = `VANILLA SAFE PASS — this is NOT a creative rewrite and NOT a punch-up. Return the SAME script, word for word, with ONLY the risky words swapped for the nearest safe neighbour word. Think find-and-replace with taste. If you are tempted to improve a line, don't.

ONLY THESE MAY BE TOUCHED:
- Restricted platform vocabulary: psychedelic, psilocybin, magic mushroom(s), shrooms, microdose / microdosing, trip / trippy / tripping, high / getting high, stoned / baked / faded / blazed, and any other drug slang.
- Medical / disease-claim wording: cure, treat, heal, fix, "cures depression / anxiety / PTSD / insomnia", "clinically proven", "doctor recommended".
- Absolute promises: guaranteed, instantly, 100%, "it will make you…", "everyone who tries it…".
- A spoken URL or domain in the CTA ("go to something dot com") → "the link is down below" / "tap the link below".
- Anything promising a high, a buzz or intoxication (this product is sub-perceptual).

NEAREST-NEIGHBOUR RULE: replace each flagged word with the closest ordinary word that keeps the same meaning, the same tone, and as close as possible the same syllable count and rhythm — the audio is lip-synced, so length and cadence matter more than elegance. Prefer a 1-for-1 word swap; only rephrase the smallest possible span when a single word can't carry it.

SWAP BANK (pick the nearest fit; if a word repeats, rotate so it doesn't sound templated):
- magic mushroom / shrooms / psilocybin / psychedelic → "mushroom", "one little gummy", "a certain ingredient", "the ingredient we can't name on here", "nature's mood molecule"
- microdose / microdosing → "one tiny gummy", "one a day", "a little bit daily", "one small dose"
- trip / trippy → "the feeling", "the shift", "how it feels"
- high / stoned / buzzed → "lifted", "lighter", "clear", "calm", "like myself again"
- cures / treats / heals X → "helped me with X", "for the days I feel X", "since I started, I feel…"
- guaranteed / instantly → "for me it…", "within a few days"
- "go to <site> dot com" → "the link is down below"

HARD RULES:
- Same number of lines and sentences, same order, same hook, same CTA in the same place, same punctuation and casing everywhere you did not have to change.
- Word count within ±3% of the original.
- Add nothing: no new claims, no new hook, no news angle, no extra sentence — and no tidying of lines that were already safe. Those come back identical, character for character.
- Never invent facts, studies, numbers or names that weren't already in the script.
- If the script is already clean, return it exactly as received.
Output only the script.`;

  VS.api = async (path, opts) => {
    const r = await fetch(path, opts);
    if (!r.ok) {
      const t = await r.text();
      // Flask abort() returns an HTML page — surface just the human message
      let msg = t;
      const m = t.match(/<p>([\s\S]*?)<\/p>/i);
      if (m) msg = m[1].replace(/<[^>]+>/g, "").trim();
      else if (/^\s*</.test(t)) msg = `request failed (${r.status})`;
      const err = new Error(msg.slice(0, 300));
      err.status = r.status;
      try { err.body = JSON.parse(t); } catch (e) { /* not json */ }
      throw err;
    }
    return r.json();
  };
  VS.post = (path, body) => VS.api(path, {
    method: "POST", headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body || {}),
  });

  VS.toast = (msg, ms) => {
    let t = VS.$("#vs-toast");
    if (!t) {
      t = document.createElement("div");
      t.id = "vs-toast";
      document.body.appendChild(t);
    }
    t.textContent = msg;
    t.classList.add("on");
    clearTimeout(t._h);
    t._h = setTimeout(() => t.classList.remove("on"), ms || 4200);
  };

  // ── pub-sub ────────────────────────────────────────────────────────────────
  const subs = {};
  VS.on = (ev, fn) => { (subs[ev] = subs[ev] || []).push(fn); return () => VS.off(ev, fn); };
  VS.off = (ev, fn) => { subs[ev] = (subs[ev] || []).filter(f => f !== fn); };
  VS.emit = (ev, data) => (subs[ev] || []).forEach(fn => { try { fn(data); } catch (e) { console.error(ev, e); } });

  // ── step registry ──────────────────────────────────────────────────────────
  VS.registerStep = def => {
    VS.steps.push(def);
    VS.steps.sort((a, b) => a.order - b.order);
  };

  // ── job helpers ────────────────────────────────────────────────────────────
  VS.jobsFor = video => VS.state.jobs.filter(j =>
    j.slug === video.name || j.slug === video.stem ||
    (j.slug || "").startsWith(video.stem + "-v"));           // clones of this video
  VS.runningFor = (video, actions) => VS.jobsFor(video).find(j =>
    j.status === "running" && (!actions || actions.includes(j.action)));

  // ── the one poller ─────────────────────────────────────────────────────────
  let tick = 0, prevRunning = new Set();

  async function pollJobs() {
    try {
      const d = await VS.api("/api/jobs");
      VS.state.jobs = Array.isArray(d) ? d : (d.jobs || []);
      // detect running → terminal transitions
      const nowRunning = new Set(VS.state.jobs.filter(j => j.status === "running").map(j => j.id));
      let finished = false;
      for (const id of prevRunning) {
        if (!nowRunning.has(id)) {
          finished = true;
          const job = VS.state.jobs.find(j => j.id === id);
          if (job) VS.emit("job-done", job);
        }
      }
      prevRunning = nowRunning;
      VS.emit("jobs");
      if (finished) await pollLibrary();
    } catch (e) { /* server briefly away — keep ticking */ }
  }

  async function pollLibrary() {
    try {
      const d = await VS.api("/api/creator/library");
      VS.state.videos = d.videos || [];
      VS.state.characters = d.characters || [];
      VS.state.tags = d.tags || [];
      VS.state.fal_spend = d.fal_spend || 0;
      if (VS.state.open) {   // keep the open video reference live
        const fresh = VS.state.videos.find(v => v.name === VS.state.open.video.name);
        if (fresh) VS.state.open.video = fresh;
      }
      VS.emit("library");
    } catch (e) { /* ignore */ }
  }
  VS.refreshLibrary = pollLibrary;

  async function loop() {
    if (document.hidden) return;
    await pollJobs();
    if (tick % 3 === 0) await pollLibrary();
    tick++;
  }

  VS.start = async () => {
    await pollLibrary();
    await pollJobs();
    setInterval(loop, 3000);
  };
})();
