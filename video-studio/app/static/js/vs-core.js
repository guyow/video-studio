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

  // the universal "make it liitt" conversion — shared by the Creator's Script step
  // and the Editor so the prompt can't drift between the two.  Send with
  // {brand:true, slug:"fairy-flame"} so /api/copywrite grounds it in offer.md + banks.
  VS.LIITT_PROMPT = `CONVERT this script — whatever brand or product it currently sells — into a script for liitt's Fairy Flame microdose gummies. It's a WINNING script being adapted, so preserve what makes it win:
- Identify the brand/product being sold and replace it with Fairy Flame (by liitt). Remove every trace of the old brand, its product format, and its category language — nothing of the old product may survive.
- Re-map the product context intelligently, don't just delete it: the old format/ritual/dosing (drinks, cans, pills, powders, drops, smoking, coffee, whatever) → one flame-shaped gummy from the pouch; its store/link/handle → fairyflame dot com (spoken form); its benefit/mechanism claims → the Fairy Flame state-shift: lighter mood, clarity, focus, feeling like yourself again.
- Keep the actives general — "microdose gummies"; never name specific actives.
- KEEP THE HOOK: same opening pattern, same emotional beats, same rhythm and energy — that structure is why the script wins. Adapt its content to liitt, never flatten it into a generic ad.
- Compliance: no disease/medical claims, no cure/treat/heal language, no guaranteed outcomes, never promise a high or intoxication (this is sub-perceptual). Personal-experience framing ("I felt…") is fine.
- Stay within ±10% of the original word count — the lip-sync depends on it.
- Tone: premium, clean, intimate — not hypey, not stoner culture.`;

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
