/* Step 6 — Deliver: send to Desktop (+ auto-clean workspace) and
   Clone Winner: multiply a proven video with a fresh similar script. */
(function () {
  const VS = window.VS;

  VS.registerStep({
    id: "deliver",
    order: 6,
    title: "Deliver & Scale",

    status(v) {
      if (VS.runningFor(v, ["dub"]) && VS.jobsFor(v).some(j => (j.slug || "").startsWith(v.stem + "-v"))) return "running";
      if (v.exported) return "done";
      if (v.dub) return "ready";
      return "todo";
    },

    async mount(el, ctx) {
      const v = ctx.video;
      el.innerHTML = `
        <div class="vs-panel">
          <h3>📤 Send to Desktop <span class="r vs-hint" id="del-dir"></span></h3>
          <div id="del-choices" class="vs-row"></div>
          <div class="vs-row">
            <label class="vs-hint" style="display:flex;gap:6px;align-items:center">
              <input type="checkbox" id="del-clean">
              auto-clean the heavy workspace after export (reversible, waits <span id="del-delay">24</span>h)</label>
          </div>
          <div class="vs-row">
            <button class="vs-btn primary" id="del-send">📤 Send to Desktop</button>
            <button class="vs-btn" id="del-open" title="open the Desktop exports folder">📁 Open folder</button>
            <span class="vs-hint" id="del-msg"></span>
          </div>
          <div id="del-queue" class="vs-hint" style="margin-top:8px"></div>
        </div>

        <div class="vs-panel">
          <h3>🏆 This one converting? Clone it
            <span class="r vs-hint">same winning structure, fresh words</span></h3>
          <div class="vs-row">
            <label class="vs-radio" style="flex:1"><input type="radio" name="cw-actor" value="same" checked>
              <span><b>Same actor</b> — reuse this footage, only the words change</span></label>
            <label class="vs-radio" style="flex:1"><input type="radio" name="cw-actor" value="new">
              <span><b>New actor</b> — pick another video from your library</span></label>
          </div>
          <div id="cw-actors" style="display:none;margin-top:8px" class="vs-row"></div>
          <input class="vs-in" id="cw-steer" style="width:100%;margin-top:8px"
            placeholder="optional direction — “different hook, more urgency”">
          <div class="vs-row">
            <button class="vs-btn" id="cw-gen">✨ Generate similar script</button>
            <span class="vs-hint" id="cw-genmsg"></span>
          </div>
          <textarea class="vs-ta" id="cw-script" rows="7" style="margin-top:8px"
            placeholder="the new script appears here — edit anything before you run"></textarea>
          <div class="vs-row">
            <select class="vs-sel" id="cw-engine">
              <option value="local" selected>Local engine — FREE (XTTS + Wav2Lip HD)</option>
              <option value="fal">fal.ai cloud — paid, best quality</option>
            </select>
            <button class="vs-btn primary" id="cw-run">🚀 Create the clone</button>
          </div>
          <div id="cw-clones" style="margin-top:10px"></div>
        </div>`;

      // settings → default auto-clean + delay
      try {
        const s = VS.state.settings || (VS.state.settings = await VS.api("/api/settings"));
        el.querySelector("#del-clean").checked = !!s.auto_cleanup.enabled;
        el.querySelector("#del-delay").textContent = s.auto_cleanup.delay_hours;
        el.querySelector("#del-dir").textContent = s.exports_dir;
      } catch (e) { /* defaults fine */ }

      // deliverable choices — everything /api/exports knows about THIS video,
      // not just the two script-swap finals (tagged b-roll, recaptions, …)
      let chosen = null;   // {path} or {kind:"captioned"}
      let opts = [];
      const KIND_ICON = {"dub-captioned": "🔥", dub: "🎬", i2v: "🎞", recaption: "💬",
                         captioned: "💬", edit: "✂️", ugc: "🧪", vsl: "📼"};
      const renderChoices = async () => {
        const box = el.querySelector("#del-choices");
        opts = [];
        try {
          const d = await VS.api("/api/exports");
          opts = (d.items || [])
            .filter(it => (it.path && it.path.includes(`/${v.stem}/`)) ||
                          (!it.path && it.kind === "captioned" && it.stem === v.stem))
            .map(it => ({
              label: `${KIND_ICON[it.kind] || "📦"} ${it.label}`,
              key: it.path || `kind:${it.kind}`,
              path: it.path, kind: it.kind,
            }));
          // captioned final first — it's the one that ships as an ad
          opts.sort((a, b) => (a.kind === "dub-captioned" ? -1 : 1) - (b.kind === "dub-captioned" ? -1 : 1));
        } catch (e) { /* fall back below */ }
        if (!opts.length) {
          if (v.captioned) opts.push({label: "🔥 Captioned final (recommended for ads)", key: "captioned-fb",
            path: `output/script-swap/${v.stem}/final-captioned.mp4`, kind: "dub-captioned"});
          if (v.dub) opts.push({label: "🎬 Final (no captions)", key: "final-fb",
            path: `output/script-swap/${v.stem}/final.mp4`, kind: "dub"});
        }
        if (!opts.length) {
          box.innerHTML = '<span class="vs-hint">nothing to deliver yet — dub first</span>';
          return;
        }
        if (!chosen || !opts.some(o => o.key === chosen)) chosen = opts[0].key;
        box.innerHTML = opts.map(o =>
          `<label class="vs-radio" style="flex:1;min-width:220px"><input type="radio" name="del-what" value="${VS.esc(o.key)}"
            ${chosen === o.key ? "checked" : ""}><span>${VS.esc(o.label)}</span></label>`).join("");
        VS.$$('input[name="del-what"]', box).forEach(i => i.onchange = () => { chosen = i.value; });
      };
      renderChoices();

      const showQueue = async () => {
        try {
          const q = await VS.api("/api/cleanup/queue");
          const mine = (q.items || []).find(i => i.stem === v.stem);
          const box = el.querySelector("#del-queue");
          if (!mine) { box.innerHTML = ""; return; }
          const h = Math.floor(mine.seconds_left / 3600), m = Math.floor(mine.seconds_left % 3600 / 60);
          box.innerHTML = `🧹 workspace auto-cleans in <b>${h}h ${m}m</b> (goes to .trash, recoverable)
            <button class="vs-btn" style="padding:3px 10px;font-size:11px" id="del-cancel">keep it</button>`;
          box.querySelector("#del-cancel").onclick = async () => {
            await VS.post("/api/cleanup/cancel", {stem: v.stem});
            VS.toast("Cleanup cancelled — workspace kept");
            showQueue();
          };
        } catch (e) { /* ignore */ }
      };
      showQueue();

      el.querySelector("#del-send").onclick = async () => {
        const o = opts.find(x => x.key === chosen);
        if (!o) return;
        const msg = el.querySelector("#del-msg");
        msg.textContent = "sending…";
        const auto = el.querySelector("#del-clean").checked;
        try {
          const body = o.path
            ? {path: o.path, stem: v.stem, auto_cleanup: auto}
            : {kind: "captioned", stem: v.stem, auto_cleanup: auto};
          const r = await VS.post("/api/exports/send", body);
          msg.textContent = "✅ saved to " + r.saved_to;
          VS.toast("📤 Delivered to Desktop" + (auto ? " — workspace auto-cleans later (reversible)" : ""));
          VS.refreshLibrary();
          showQueue();
        } catch (e) { msg.textContent = ""; VS.toast("Export failed: " + e.message); }
      };

      el.querySelector("#del-open").onclick = async () => {
        try { await VS.post("/api/exports/open"); }
        catch (e) { VS.toast("Couldn't open the folder: " + e.message); }
      };

      // ── Clone Winner ──
      let actor = "same";
      VS.$$('input[name="cw-actor"]', el).forEach(r => r.onchange = async () => {
        actor = VS.$('input[name="cw-actor"]:checked', el).value === "same" ? "same" : null;
        const grid = el.querySelector("#cw-actors");
        grid.style.display = actor === "same" ? "none" : "";
        if (actor !== "same" && !grid.children.length) {
          const d = await VS.api("/api/clone/actors");
          grid.innerHTML = (d.actors || []).map(a =>
            `<button class="vs-btn cw-a" data-n="${VS.esc(a.name)}" style="padding:6px 10px;font-size:11.5px">
               ${VS.esc(a.name)}</button>`).join("");
          VS.$$(".cw-a", grid).forEach(b => b.onclick = () => {
            VS.$$(".cw-a", grid).forEach(x => x.style.borderColor = "");
            b.style.borderColor = "var(--accent)";
            actor = b.dataset.n;
          });
        }
      });

      el.querySelector("#cw-gen").onclick = async () => {
        if (actor === null) { VS.toast("pick the new actor first"); return; }
        const btn = el.querySelector("#cw-gen"), msg = el.querySelector("#cw-genmsg");
        btn.disabled = true;
        msg.textContent = "🪄 writing a fresh take on the winning script… (~30s)";
        try {
          const d = await VS.post("/api/clone/script",
            {winner: v.stem, actor, steer: el.querySelector("#cw-steer").value.trim()});
          el.querySelector("#cw-script").value = d.script;
          msg.textContent = `✓ ${d.words} words for ${d.seconds}s of footage`;
        } catch (e) { msg.textContent = ""; VS.toast("Error: " + e.message); }
        btn.disabled = false;
      };

      const loadClones = async () => {
        try {
          const d = await VS.api("/api/clone/list");
          const mine = (d.clones || []).filter(c => c.winner === v.stem);
          const box = el.querySelector("#cw-clones");
          if (!mine.length) { box.innerHTML = ""; return; }
          box.innerHTML = "<div class='vs-hint' style='margin-bottom:4px'>clones of this video:</div>" +
            mine.map(c => `<div class="vs-row" style="margin-top:4px">
              <span class="vs-hint" style="font-family:var(--mono)">🧬 ${VS.esc(c.stem)}</span>
              ${c.ready
                ? `<a target="_blank" href="/media/output/script-swap/${encodeURIComponent(c.stem)}/final.mp4">▶ view</a>`
                : (c.job && c.job.status === "running" ? '<span class="vs-paid">rendering…</span>'
                   : '<span class="vs-err">failed</span>')}
            </div>`).join("");
        } catch (e) { /* ignore */ }
      };
      ctx.loadClones = loadClones;
      loadClones();

      el.querySelector("#cw-run").onclick = async () => {
        const script = el.querySelector("#cw-script").value.trim();
        if (script.split(/\s+/).filter(Boolean).length < 8) { VS.toast("Generate or paste a script first"); return; }
        if (actor === null) { VS.toast("pick the new actor first"); return; }
        const engine = el.querySelector("#cw-engine").value;
        const body = {winner: v.stem, actor, script, engine, lipsync: "wav2lip-hd",
                      tier: "standard", tts: "hd"};
        if (engine === "fal") {
          if (!confirm("⚠ The fal.ai clone spends money (voice + lip-sync). Approve?")) return;
          body.confirm_cost = true;
        }
        try {
          const d = await VS.post("/api/clone/run", body);
          VS.drawer.watch(d.job_id);
          VS.toast(`🧬 Cloning as “${d.stem}” — it appears below when done`);
          setTimeout(loadClones, 1500);
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      VS.on("drawer-finished", ctx._df = j => {
        if ((j.slug || "").startsWith(v.stem)) { loadClones(); VS.refreshLibrary(); }
      });

      this.sync(ctx);
    },

    sync(ctx) {
      const v = ctx.video;
      const el = ctx.el;
      const send = el.querySelector("#del-send");
      if (!send) return;
      send.disabled = !v.dub;
      el.querySelector("#cw-gen").disabled = !v.dub || !v.script;
      el.querySelector("#cw-run").disabled = !v.dub || !v.script;
    },

    unmount(ctx) {
      if (ctx._df) VS.off("drawer-finished", ctx._df);
    },
  });
})();
