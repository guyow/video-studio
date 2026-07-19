/* Step 4 — Fix & QA: describe what's wrong → AI advisor picks the repair;
   takes are versioned + promotable; frame-strip + AI review + verdict. */
(function () {
  const VS = window.VS;

  VS.registerStep({
    id: "fix",
    order: 4,
    title: "Fix & QA",

    status(v) {
      if (VS.runningFor(v, ["dubsync-repair", "qc-ai"])) return "running";
      if (v.qc_verdict === "pass" || (v.approved && v.approved.dub)) return "done";
      if (v.qc_verdict === "fail") return "needs-you";
      if (v.dub) return "ready";
      return "todo";
    },

    mount(el, ctx) {
      const v = ctx.video;
      const finalRel = `output/script-swap/${v.stem}/final.mp4`;
      let marks = [];   // swap ranges
      let advice = null;

      el.innerHTML = `
        <div class="vs-panel">
          <div class="vs-stage"><video controls playsinline id="fix-vid"></video></div>
          <div class="vs-hint" id="fix-nodub" style="display:none">No dub yet — run the Dub step first.</div>
        </div>

        <div class="vs-panel" id="fix-tools">
          <h3>🪄 Something looks wrong? Just describe it <span class="r vs-free">AI advisor · free</span></h3>
          <div class="vs-row">
            <input class="vs-in" id="fix-complaint" style="flex:1;min-width:220px"
              placeholder="e.g. “the cup gets warped when he drinks” or “mouth is out of sync at the end”">
            <button class="vs-btn" id="fix-advise">🪄 Analyze</button>
          </div>
          <div id="fix-advice" style="margin-top:10px"></div>

          <h3 style="margin-top:18px">Manual fixes</h3>
          <div class="vs-row">
            <button class="vs-btn" data-fix="relipsync" title="redo the mouth with local Wav2Lip HD">👄 Re-lip-sync</button>
            <button class="vs-btn" data-fix="renorm" title="fix loudness">🔊 Fix loudness</button>
            <button class="vs-btn" data-fix="remux" title="rebuild container">📦 Remux</button>
            <button class="vs-btn" data-fix="refit" title="re-stretch the voice to fit">⏱ Refit voice</button>
          </div>
          <div class="vs-row">
            <span class="vs-hint">Replace exact moments with the original footage:</span>
            <button class="vs-btn" id="fix-mark-in">⏺ mark start</button>
            <button class="vs-btn" id="fix-mark-out">⏹ mark end</button>
            <span class="vs-hint" id="fix-marks">no ranges marked</span>
            <button class="vs-btn" id="fix-swap" disabled>▶ Swap marked ranges</button>
          </div>
          <div class="vs-hint" style="margin-top:6px">need the drawing tools? <a href="/dubsync-lab" target="_blank">open the DubSync lab ↗</a></div>
        </div>

        <div class="vs-panel" id="fix-takespanel">
          <h3>Takes <span class="r vs-hint">every repair is a new take — promote the best one</span></h3>
          <div class="vs-takes" id="fix-takes"><span class="vs-hint">loading…</span></div>
        </div>

        <div class="vs-panel">
          <h3>Quality check</h3>
          <div class="vs-frames" id="fix-frames"><span class="vs-hint">loading frames…</span></div>
          <div class="vs-row">
            <button class="vs-btn" id="fix-airev">🤖 AI review (Claude watches it)</button>
            <span style="flex:1"></span>
            <button class="vs-btn" id="fix-pass" style="color:var(--ok)">✓ Pass — ready to deliver</button>
            <button class="vs-btn" id="fix-fail" style="color:var(--bad)">✗ Needs work</button>
          </div>
          <div class="vs-hint" id="fix-verdict" style="margin-top:6px"></div>
        </div>`;

      const vid = el.querySelector("#fix-vid");

      const runRepair = async body => {
        try {
          const r = await VS.post("/api/dubsync/repair", Object.assign({stem: v.stem}, body));
          VS.drawer.watch(r.job_id);
          VS.toast("🔧 Repair running — it lands in Takes when done");
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      VS.$$("[data-fix]", el).forEach(b => b.onclick = () => {
        const a = b.dataset.fix;
        runRepair(a === "relipsync" ? {action: a, restorer: "gfpgan"} : {action: a});
      });

      // swap ranges
      const updMarks = () => {
        el.querySelector("#fix-marks").textContent = marks.length
          ? marks.map(m => `${m[0].toFixed(2)}–${(m[1] || 0).toFixed(2)}s`).join(", ")
          : "no ranges marked";
        el.querySelector("#fix-swap").disabled = !marks.some(m => m[1] > m[0]);
      };
      el.querySelector("#fix-mark-in").onclick = () => {
        marks.push([vid.currentTime, null]);
        updMarks();
      };
      el.querySelector("#fix-mark-out").onclick = () => {
        const open = marks.find(m => m[1] === null);
        if (!open) { VS.toast("mark a start first"); return; }
        open[1] = Math.max(vid.currentTime, open[0] + 0.05);
        updMarks();
      };
      el.querySelector("#fix-swap").onclick = () => {
        const ranges = marks.filter(m => m[1] > m[0]).map(m => `${m[0].toFixed(2)}-${m[1].toFixed(2)}`);
        runRepair({action: "swap", ranges});
        marks = [];
        updMarks();
      };

      // advisor
      el.querySelector("#fix-advise").onclick = async () => {
        const text = el.querySelector("#fix-complaint").value.trim();
        if (!text) { VS.toast("Describe the problem first"); return; }
        const btn = el.querySelector("#fix-advise");
        const out = el.querySelector("#fix-advice");
        btn.disabled = true;
        out.innerHTML = '<span class="vs-hint">🪄 watching the video, comparing with the original… (~1 min)</span>';
        try {
          advice = await VS.post("/api/dubsync/advise", {stem: v.stem, text});
          out.innerHTML = `
            <div class="vs-radio" style="cursor:default;display:block">
              <b>${VS.esc(advice.action || "?")}</b> — ${VS.esc(advice.explanation || "")}
              ${advice.warning ? `<div class="vs-err" style="margin-top:4px">${VS.esc(advice.warning)}</div>` : ""}
              ${advice.img ? `<img src="${advice.img}" style="max-width:100%;border-radius:8px;margin-top:8px">` : ""}
              <div class="vs-row"><button class="vs-btn primary" id="fix-doit">▶ Run this fix</button></div>
            </div>`;
          out.querySelector("#fix-doit").onclick = () => {
            const body = {action: advice.action};
            if (advice.samples) body.samples = advice.samples;
            if (advice.box) body.box = advice.box;
            if (advice.track != null) body.track = advice.track;
            if (advice.action === "relipsync") body.restorer = "gfpgan";
            runRepair(body);
          };
        } catch (e) {
          out.innerHTML = `<span class="vs-err">analysis failed: ${VS.esc(e.message)}</span>`;
        }
        btn.disabled = false;
      };

      // takes
      const loadTakes = async () => {
        try {
          const d = await VS.api("/api/dubs");
          const me = (d.dubs || []).find(x => x.stem === v.stem);
          const box = el.querySelector("#fix-takes");
          if (!me || !(me.takes || []).length) {
            box.innerHTML = '<span class="vs-hint">no alternate takes yet — repairs will appear here</span>';
            return;
          }
          box.innerHTML = "";
          me.takes.forEach(t => {
            const name = typeof t === "string" ? t : t.file || t.name;
            const row = document.createElement("div");
            row.className = "take";
            row.innerHTML = `<span class="nm">${VS.esc(name)}</span>
              <a class="vs-hint" target="_blank" href="/media/output/script-swap/${encodeURIComponent(v.stem)}/${encodeURIComponent(name)}">▶ view</a>
              <button class="vs-btn">⭐ Promote to final</button>`;
            row.querySelector("button").onclick = async () => {
              try {
                await VS.post("/api/dub-promote", {stem: v.stem, file: name});
                VS.toast("Promoted — final.mp4 replaced (old one archived)");
                VS.refreshLibrary();
                loadTakes();
                this.sync(ctx);
              } catch (e) { VS.toast("Error: " + e.message); }
            };
            box.appendChild(row);
          });
        } catch (e) { /* ignore */ }
      };
      ctx.loadTakes = loadTakes;
      loadTakes();

      // QA
      const loadFrames = async () => {
        try {
          const d = await VS.api(`/api/qc/frames?path=${encodeURIComponent(finalRel)}&count=8`);
          el.querySelector("#fix-frames").innerHTML =
            (d.frames || []).map(f => `<img src="/${f.path.replace(/^\/+/, "")}" title="${f.t}s">`).join("")
            || '<span class="vs-hint">no frames</span>';
        } catch (e) {
          el.querySelector("#fix-frames").innerHTML = '<span class="vs-hint">frames unavailable (dub first)</span>';
        }
      };
      ctx.loadFrames = loadFrames;
      loadFrames();

      el.querySelector("#fix-airev").onclick = async () => {
        try {
          const r = await VS.post("/api/qc/ai-review", {path: finalRel});
          VS.drawer.watch(r.job_id);
          VS.toast("🤖 AI is watching your video…");
        } catch (e) { VS.toast("Error: " + e.message); }
      };
      const verdict = async verd => {
        try {
          await VS.post("/api/qc/review", {path: finalRel, verdict: verd, checks: {}, notes: ""});
          await VS.post("/api/creator/meta", {name: v.name, approved: {dub: verd === "pass"}});
          VS.toast(verd === "pass" ? "✅ Passed — Deliver step unlocked" : "Marked as needs-work");
          VS.refreshLibrary();
        } catch (e) { VS.toast("Error: " + e.message); }
      };
      el.querySelector("#fix-pass").onclick = () => verdict("pass");
      el.querySelector("#fix-fail").onclick = () => verdict("fail");

      VS.on("drawer-finished", ctx._df = j => {
        if (j.slug === v.stem && j.action === "dubsync-repair") { loadTakes(); }
      });

      this.sync(ctx);
    },

    sync(ctx) {
      const v = ctx.video;
      const el = ctx.el;
      const vid = el.querySelector("#fix-vid");
      if (!vid) return;
      if (v.dub) {
        const want = "/media/" + v.dub + "?v=" + (v.dub_mtime || "");
        if (!vid.src.endsWith(encodeURI(want))) vid.src = want;
        el.querySelector("#fix-nodub").style.display = "none";
        el.querySelector("#fix-tools").style.opacity = "";
      } else {
        el.querySelector("#fix-nodub").style.display = "";
        el.querySelector("#fix-tools").style.opacity = ".45";
      }
      el.querySelector("#fix-verdict").textContent =
        v.qc_verdict ? `current verdict: ${v.qc_verdict}` : "";
    },

    unmount(ctx) {
      if (ctx._df) VS.off("drawer-finished", ctx._df);
    },
  });
})();
