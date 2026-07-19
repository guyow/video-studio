/* Step 1 — Source & Clean: preview, transcribe, erase burned-in subtitles. */
(function () {
  const VS = window.VS;

  VS.registerStep({
    id: "source",
    order: 1,
    title: "Source & Clean",

    status(v, jobs) {
      if (VS.runningFor(v, ["transcribe", "clean-subs"])) return "running";
      if (v.cleaned || v.no_subs) return "done";
      if (!v.transcript) return "needs-you";
      return "ready";
    },

    mount(el, ctx) {
      const v = ctx.video;
      el.innerHTML = `
        <div class="vs-panel">
          <div class="vs-stage"><video controls playsinline
            src="/media/uploads/${encodeURIComponent(v.name)}"></video></div>
          <div class="vs-row">
            <span class="vs-hint">${VS.esc(v.name)} · ${VS.fmtSize(v.size)}</span>
          </div>
        </div>

        <div class="vs-panel">
          <h3>Transcribe <span class="r vs-free">free · local whisper</span></h3>
          <div class="vs-hint" id="src-tmsg"></div>
          <div class="vs-row">
            <button class="vs-btn" id="src-transcribe"></button>
          </div>
        </div>

        <div class="vs-panel">
          <h3>Burned-in subtitles <span class="r vs-free">free · AI erase on your GPU</span></h3>
          <div class="vs-hint">If the source has old captions burned into the pixels, erase them
            before dubbing — new captions go on at the end, clean.</div>
          <div class="vs-row">
            <button class="vs-btn" id="src-erase"></button>
            <button class="vs-btn" id="src-restore" style="display:none">↩ Restore original</button>
            <label class="vs-hint" style="display:flex;gap:6px;align-items:center">
              <input type="checkbox" id="src-nosubs"> this video has no burned subtitles</label>
          </div>
          <div class="vs-hint" id="src-emsg" style="margin-top:8px"></div>
        </div>`;

      el.querySelector("#src-transcribe").onclick = async () => {
        try {
          const r = await VS.post("/api/run", {action: "transcribe", file: v.name});
          VS.drawer.watch(r.job_id);
          VS.toast("Transcribing…");
        } catch (e) { VS.toast("Error: " + e.message); }
      };
      el.querySelector("#src-erase").onclick = async () => {
        try {
          const r = await VS.post("/api/clean-subs", {file: v.name, auto: true, mode: "erase"});
          VS.drawer.watch(r.job_id);
          VS.toast("Erasing burned-in subtitles — this is the long one");
        } catch (e) { VS.toast("Error: " + e.message); }
      };
      el.querySelector("#src-restore").onclick = async () => {
        if (!confirm("Put back the ORIGINAL (with the old subtitles)?")) return;
        try {
          await VS.post("/api/clean-restore", {file: v.name});
          VS.toast("Original restored");
          VS.refreshLibrary();
        } catch (e) { VS.toast("Error: " + e.message); }
      };
      el.querySelector("#src-nosubs").onchange = async e => {
        try {
          await VS.post("/api/creator/meta", {name: v.name, no_subs: e.target.checked});
          VS.refreshLibrary();
        } catch (err) { VS.toast("Error: " + err.message); }
      };

      this.sync(ctx);
    },

    sync(ctx) {
      const v = ctx.video;
      const el = ctx.el;
      const tBtn = el.querySelector("#src-transcribe");
      const tMsg = el.querySelector("#src-tmsg");
      if (!tBtn) return;
      const tJob = VS.runningFor(v, ["transcribe"]);
      if (tJob) { tBtn.disabled = true; tBtn.textContent = "⏳ transcribing…"; }
      else if (v.transcript) {
        tBtn.disabled = false; tBtn.textContent = "↻ Re-transcribe";
        tMsg.textContent = "✓ transcribed — the words are ready in the Script step.";
      } else {
        tBtn.disabled = false; tBtn.textContent = "▶ Transcribe";
        tMsg.textContent = "Get the original words first — the script editor builds on them.";
      }

      const eBtn = el.querySelector("#src-erase");
      const eMsg = el.querySelector("#src-emsg");
      const rBtn = el.querySelector("#src-restore");
      const cJob = VS.runningFor(v, ["clean-subs"]);
      el.querySelector("#src-nosubs").checked = !!v.no_subs;
      if (cJob) {
        eBtn.disabled = true;
        eBtn.textContent = "⏳ erasing… (" + ((cJob.progress && cJob.progress.label) || "working") + ")";
      } else if (v.cleaned) {
        eBtn.disabled = false; eBtn.textContent = "↻ Re-erase";
        eMsg.textContent = "✓ erased — the clean version replaced the upload (original backed up).";
        rBtn.style.display = "";
      } else {
        eBtn.disabled = false; eBtn.textContent = "🧹 Erase burned-in subtitles";
        rBtn.style.display = "none";
      }
    },
  });
})();
