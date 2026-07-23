/* Step 2 — Script: edit the words the actor will say. AI rewrite included. */
(function () {
  const VS = window.VS;

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
            <span class="vs-hint" id="scr-aimsg"></span>
          </div>
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
      const msg = el.querySelector("#scr-aimsg");
      const doRewrite = async (busy, done, extra, brandOn) => {
        const text = ta.value.trim();
        if (!text) { VS.toast("Nothing to rewrite yet"); return; }
        aiBtn.disabled = liBtn.disabled = true;
        msg.textContent = busy;
        const steer = el.querySelector("#scr-steer").value.trim();
        const news = el.querySelector("#scr-news"), safe = el.querySelector("#scr-safe");
        const body = {text, instruction: [extra,
          news && news.checked ? VS.NEWS_HOOK_PROMPT : "",
          safe && safe.checked ? VS.META_SAFE_PROMPT : "",
          steer].filter(Boolean).join("\n\n")};
        if (brandOn) { body.brand = true; body.slug = "fairy-flame"; }  // grounded in offer.md + banks
        try {
          const r = await VS.post("/api/copywrite", body);
          ta.value = r.text || text;
          updCount();
          msg.textContent = done;
        } catch (e) { msg.textContent = ""; VS.toast("Rewrite failed: " + e.message); }
        aiBtn.disabled = liBtn.disabled = false;
      };
      aiBtn.onclick = () => doRewrite("🪄 rewriting… (~30s)", "✓ rewritten — review, tweak, then Save", "", false);
      liBtn.onclick = () => doRewrite("🔥 making it liitt… (~45s)",
        "🔥 Fairy Flame version ready — review, tweak, then Save", VS.LIITT_PROMPT, true);
    },

    sync() { /* no live state */ },
  });
})();
