/* Step 2 — Script: edit the words the actor will say. AI rewrite included. */
(function () {
  const VS = window.VS;
  let fitOff = null, fitDone = null;      // live subscriptions, dropped on unmount

  VS.registerStep({
    id: "script",
    order: 2,
    title: "Script",

    status(v) {
      if (v.script) return "done";
      if (v.transcript) return "needs-you";
      return "todo";
    },

    async mount(el, ctx) {
      const v = ctx.video;
      el.innerHTML = `
        <div class="vs-panel">
          <h3>The words your actor will say
            <span class="r vs-hint" id="scr-src"></span></h3>
          <textarea class="vs-ta" id="scr-text" rows="10"
            placeholder="transcribe first, or paste a script here…"></textarea>
          <div class="vs-row">
            <button class="vs-btn primary" id="scr-save">💾 Save script</button>
            <span class="vs-hint" id="scr-count"></span>
          </div>
        </div>
        <div class="vs-panel">
          <h3>✨ AI rewrite <span class="r vs-free">uses your Claude subscription</span></h3>
          <input class="vs-in" id="scr-steer" style="width:100%"
            placeholder="how to change it — e.g. “punchier hook, mention the 3-pack, keep the same length”">
          <div class="vs-row" style="gap:14px;margin:6px 0 2px;flex-wrap:wrap">
            <label class="vs-hint" style="display:flex;gap:5px;align-items:center;cursor:pointer"
              title="open like breaking news — studies, veterans, laws loosening — in platform-safe wording">
              <input type="checkbox" id="scr-news" style="accent-color:#f0b25c">📰 News hook</label>
            <label class="vs-hint" style="display:flex;gap:5px;align-items:center;cursor:pointer"
              title="dodge Meta's restricted words (psychedelic, psilocybin, magic mushroom, microdosing) with creative compliant wording">
              <input type="checkbox" id="scr-safe" style="accent-color:#f0b25c" checked>🛡 Meta-safe</label>
          </div>
          <div class="vs-row">
            <button class="vs-btn primary" id="scr-liitt"
              title="turn ANY script — any brand, any product — into a liitt Fairy Flame script: blends into the existing script, keeps the winning hook & beats, stays compliant">🔥 liitt</button>
            <button class="vs-btn" id="scr-ai">✨ Rewrite with AI</button>
            <button class="vs-btn" id="scr-vanilla"
              title="Safety pass, not a rewrite: keeps your script exactly as it is and only swaps the words that get ads flagged for the nearest safe word — same lines, same rhythm, same length">🍦 Vanilla safe</button>
            <span class="vs-hint" id="scr-aimsg"></span>
          </div>
        </div>
        <div class="vs-panel">
          <h3>📏 Make the video fit the script
            <span class="r vs-hint">so a longer script doesn't get squeezed</span></h3>
          <div class="vs-hint" style="margin-bottom:8px">If your script takes longer to say than
            the footage lasts, the dub has to speed the voice up and the lips drift. This grows the
            <b>video</b> instead — it generates the missing seconds from the last frame, so your
            character keeps going, and proves the final length matches to the frame.</div>
          <div id="fit-body"><div class="vs-hint">checking…</div></div>
        </div>`;

      const ta = el.querySelector("#scr-text");
      const count = el.querySelector("#scr-count");
      const updCount = () => {
        const words = ta.value.trim().split(/\s+/).filter(Boolean).length;
        const pace = v.orig_words ? ` · original spoke ${v.orig_words} words — stay close so the lips fit` : "";
        count.textContent = `${words} words${pace}`;
      };
      ta.addEventListener("input", updCount);

      try {
        const s = await VS.api("/api/script/" + encodeURIComponent(v.stem));
        ta.value = s.text || "";
        el.querySelector("#scr-src").textContent =
          s.source === "edited" ? "saved edit" : s.source === "transcript" ? "from transcript — edit freely" : "empty";
      } catch (e) { /* leave blank */ }
      updCount();

      el.querySelector("#scr-save").onclick = async () => {
        const text = ta.value.trim();
        if (!text) { VS.toast("Write the script first"); return; }
        try {
          await VS.post("/api/script/" + encodeURIComponent(v.stem), {text});
          VS.toast("Script saved — Dub step unlocked");
          VS.refreshLibrary();
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      const aiBtn = el.querySelector("#scr-ai");
      const liBtn = el.querySelector("#scr-liitt");
      const vaBtn = el.querySelector("#scr-vanilla");
      const msg = el.querySelector("#scr-aimsg");
      const btns = [aiBtn, liBtn, vaBtn];
      // vanilla:true = a pure safety pass — the News hook add-on would inject a NEW
      // opening line, which is exactly what this button promises not to do, so it's
      // skipped. The steer box still applies (explicit user intent wins).
      const doRewrite = async (busy, done, extra, brandOn, vanilla) => {
        const text = ta.value.trim();
        if (!text) { VS.toast("Nothing to rewrite yet"); return; }
        btns.forEach(b => { if (b) b.disabled = true; });
        msg.textContent = busy;
        const steer = el.querySelector("#scr-steer").value.trim();
        const news = el.querySelector("#scr-news"), safe = el.querySelector("#scr-safe");
        const body = {text, instruction: [extra,
          !vanilla && news && news.checked ? VS.NEWS_HOOK_PROMPT : "",
          !vanilla && safe && safe.checked ? VS.META_SAFE_PROMPT : "",
          steer].filter(Boolean).join("\n\n")};
        if (brandOn) { body.brand = true; body.slug = "fairy-flame"; }  // grounded in offer.md + banks
        try {
          const r = await VS.post("/api/copywrite", body);
          ta.value = r.text || text;
          updCount();
          msg.textContent = done;
        } catch (e) { msg.textContent = ""; VS.toast("Rewrite failed: " + e.message); }
        btns.forEach(b => { if (b) b.disabled = false; });
      };
      aiBtn.onclick = () => doRewrite("🪄 rewriting… (~30s)", "✓ rewritten — review, tweak, then Save", "", false);
      liBtn.onclick = () => doRewrite("🔥 making it liitt… (~45s)",
        "🔥 Fairy Flame version ready — review, tweak, then Save", VS.LIITT_PROMPT, true);
      vaBtn.onclick = () => doRewrite("🍦 swapping the risky words… (~25s)",
        "🍦 vanilla version — same script, safer words. Diff it against your original, then Save",
        VS.VANILLA_PROMPT, false, true);

      /* ── Fit the video to the script (AI-extend) ────────────────────────
         Measure how long the script really takes to speak (free, local XTTS),
         then generate the missing seconds from the video's LAST FRAME so the
         character carries on. Same endpoints the Editor uses. */
      const body = el.querySelector("#fit-body");
      const stemUrl = encodeURIComponent(v.stem);
      const MOTION = "the same person keeps talking to camera, subtle natural movement, "
                   + "same setting, same lighting and framing";

      const running = () => VS.state.jobs.find(j =>
        j.slug === v.stem && j.status === "running" &&
        (j.action === "fit" || j.action === "fit-extend" || j.action === "fit-join"));

      /* how hard the join is blended. A few frames of dissolve hide the exposure
         step where the generated footage takes over; a hard cut is frame-exact but
         shows whatever colour difference is left. */
      const SMOOTH = `<select class="vs-sel" id="fit-blend">
          <option value="0.17">Silky join — recommended</option>
          <option value="0.35">Extra soft join</option>
          <option value="0">Hard cut — frame-exact</option></select>`;

      const seamLine = fit => {
        if (!fit.seam_ratio) return "";
        const word = {invisible: "you can't see the join", smooth: "the join is smooth",
                      visible: "the join is still noticeable"}[fit.verdict] || fit.verdict;
        const icon = fit.verdict === "visible" ? "⚠" : "🪄";
        return `<div class="vs-hint" style="margin-top:4px">${icon} seam at
          <b>${fit.seam_sec}s</b> — ${word} (${fit.seam_ratio}× the motion around it${
          fit.seam_pick && fit.seam_pick.trimmed_sec
            ? `, cut ${fit.seam_pick.trimmed_sec}s early on the stillest frame` : ""}).</div>`;
      };

      const draw = d => {
        const plan = d.plan || {}, fit = d.fit || {};
        const job = running();
        if (job) {
          body.innerHTML = `<div class="vs-hint">${
            job.action === "fit" ? "📏 measuring the script with the voice engine… (~1 min, free)"
            : job.action === "fit-join" ? "🪄 re-doing the join — free, about a minute…"
            : "⚡ generating the extra footage on fal.ai…"} — watch the job drawer.</div>`;
          return;
        }
        if (fit.final_sec) {
          body.innerHTML =
            `<div class="vs-free">✅ fitted — final video <b>${fit.final_sec}s</b> vs script
              <b>${fit.target_sec}s</b> (matches to
              ${Math.abs(fit.final_sec - fit.target_sec).toFixed(2)}s).</div>
             ${seamLine(fit)}
             ${fit.fitted ? `<div class="vs-hint" style="margin-top:4px">the longer clip is in your
               library as <b>${VS.esc(String(fit.fitted).split(/[\\/]/).pop())}</b> — open that one
               to dub it.</div>` : ""}
             <div class="vs-row" style="margin-top:8px;gap:8px;flex-wrap:wrap">
               ${SMOOTH}
               <button class="vs-btn" id="fit-join">🪄 Re-do the join</button>
               <button class="vs-btn" id="fit-an">↻ Check again</button>
               <span class="vs-hint">free — re-uses the footage you already paid for</span>
             </div>`;
        } else if (!plan.source_sec) {
          body.innerHTML =
            `<div class="vs-row"><button class="vs-btn primary" id="fit-an">📏 Check the length</button>
              <span class="vs-hint">free · ~1 min · speaks your script locally to time it</span></div>`;
        } else if (!plan.needs_extend) {
          body.innerHTML =
            `<div class="vs-free">✓ no extending needed — footage <b>${plan.source_sec}s</b> ≥
              script <b>${plan.target_sec}s</b>.</div>
             <div class="vs-row" style="margin-top:8px">
               <button class="vs-btn" id="fit-an">↻ Check again</button></div>`;
        } else {
          body.innerHTML =
            `<div class="vs-hint" style="margin-bottom:8px">footage <b>${plan.source_sec}s</b> →
              script needs <b>${plan.target_sec}s</b> → <b>${plan.gap}s</b> to generate.</div>
             <div class="vs-row" style="gap:8px;flex-wrap:wrap">
               <select class="vs-sel" id="fit-model">
                 <option value="kling-2.1">Kling 2.1 — balanced</option>
                 <option value="hailuo-02">Hailuo 02 — great motion</option>
                 <option value="kling-2.1-pro">Kling 2.1 Pro — best quality</option>
                 <option value="wan-2.2">Wan 2.2 — budget</option></select>
               <select class="vs-sel" id="fit-aspect">
                 <option value="auto">Match this video — recommended</option>
                 <option>9:16</option><option>16:9</option><option>1:1</option></select>
               ${SMOOTH}
             </div>
             <textarea class="vs-ta" id="fit-prompt" rows="2" style="margin-top:6px"
               placeholder="what the extra seconds should show">${VS.esc(MOTION)}</textarea>
             <div class="vs-row" style="margin-top:6px">
               <button class="vs-btn primary" id="fit-go">⚡ Extend the video</button>
               <button class="vs-btn" id="fit-an">↻ Check again</button>
               <span class="vs-hint">costs money on fal.ai — you approve the exact amount first</span>
             </div>
             <div class="vs-hint" style="margin-top:6px">The join is built to disappear:
               it cuts on the stillest frame, seeds the AI from that exact frame, colour-matches
               the new footage to yours and dissolves the last few frames.</div>`;
        }
        const an = el.querySelector("#fit-an");
        if (an) an.onclick = analyze;
        const go = el.querySelector("#fit-go");
        if (go) go.onclick = extend;
        const rj = el.querySelector("#fit-join");
        if (rj) rj.onclick = rejoin;
      };

      const load = async () => {
        try { draw(await VS.api("/api/fit/plan/" + stemUrl)); }
        catch (e) { draw({}); }
      };

      async function analyze() {
        if (!ta.value.trim()) { VS.toast("Write the script first"); return; }
        body.innerHTML = `<div class="vs-hint">📏 starting…</div>`;
        try {
          await VS.post("/api/fit/analyze", {file: v.name});
          VS.toast("📏 Measuring your script — about a minute, free");
          draw({});
        } catch (e) {
          const hint = /script first/i.test(e.message)
            ? " — press 💾 Save script above first." : "";
          body.innerHTML = `<div class="vs-hint">${VS.esc(e.message)}${hint}</div>
            <div class="vs-row" style="margin-top:8px">
              <button class="vs-btn" id="fit-an">↻ Try again</button></div>`;
          el.querySelector("#fit-an").onclick = analyze;
        }
      }

      async function rejoin() {
        const btn = el.querySelector("#fit-join");
        btn.disabled = true;
        try {
          await VS.post("/api/fit/join", {
            file: v.name, blend: parseFloat(el.querySelector("#fit-blend").value),
          });
          VS.toast("🪄 Re-doing the join — free");
          draw({});
        } catch (e) { VS.toast("Couldn't start: " + e.message); btn.disabled = false; }
      }

      async function extend() {
        const payload = extra => ({
          file: v.name,
          model: el.querySelector("#fit-model").value,
          aspect: el.querySelector("#fit-aspect").value,
          blend: parseFloat(el.querySelector("#fit-blend").value),
          prompt: el.querySelector("#fit-prompt").value.trim(),
          ...(extra || {}),
        });
        const go = el.querySelector("#fit-go");
        go.disabled = true;
        try {
          const r = await fetch("/api/fit/run", {method: "POST",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload())});
          if (r.status === 402) {
            const d = await r.json();
            if (!confirm(`⚠ This generates footage on fal.ai (spends money):\n\n${d.estimate.summary}\n\n≈ $${d.estimate.this_run.toFixed(2)}\n\nApprove and extend? (Cancel = nothing charged)`)) {
              VS.toast("Cancelled — nothing charged"); go.disabled = false; return;
            }
            const r2 = await VS.post("/api/fit/run", payload({confirm_cost: true}));
            if (!r2) throw new Error("no response");
          } else if (!r.ok) {
            throw new Error((await r.text()).slice(0, 200));
          }
          VS.toast("⚡ Extending — generating the extra footage");
          draw({});
        } catch (e) { VS.toast("Couldn't start: " + e.message); go.disabled = false; }
      }

      // redraw when a fit job starts/finishes so the panel follows the work
      let lastPlan = {};
      if (fitOff) fitOff();
      if (fitDone) fitDone();
      fitOff = VS.on("jobs", () => { if (el.isConnected) draw(lastPlan); });
      fitDone = VS.on("job-done", async j => {
        if (el.isConnected && j.slug === v.stem &&
            (j.action === "fit" || j.action === "fit-extend" || j.action === "fit-join")) {
          lastPlan = await VS.api("/api/fit/plan/" + stemUrl).catch(() => ({}));
          draw(lastPlan);
          if (j.action !== "fit") VS.refreshLibrary();
        }
      });
      lastPlan = await VS.api("/api/fit/plan/" + stemUrl).catch(() => ({}));
      draw(lastPlan);
    },

    sync() { /* no live state */ },

    unmount() {
      if (fitOff) { fitOff(); fitOff = null; }
      if (fitDone) { fitDone(); fitDone = null; }
    },
  });
})();
