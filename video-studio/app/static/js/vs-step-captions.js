/* Step 5 — Captions: word-timed captions burned onto the dubbed final. */
(function () {
  const VS = window.VS;

  VS.registerStep({
    id: "captions",
    order: 5,
    title: "Captions",

    status(v) {
      if (VS.runningFor(v, ["caption", "recaption"])) return "running";
      if (v.captioned) return "done";
      if (v.dub) return "ready";
      return "todo";
    },

    mount(el, ctx) {
      const v = ctx.video;
      el.innerHTML = `
        <div class="vs-panel">
          <h3>Burn captions <span class="r vs-free">free · word-timed, bold style</span></h3>
          <div class="vs-hint">Captions are generated from the ACTUAL audio (word-perfect timing)
            and burned over the old caption band.</div>
          <div class="vs-row">
            <label class="vs-radio" style="flex:1"><input type="radio" name="cap-src" value="dub" checked>
              <span><b>On the dubbed video</b> — captions match your new script <span class="vs-hint">(usual choice)</span></span></label>
            <label class="vs-radio" style="flex:1"><input type="radio" name="cap-src" value="orig">
              <span><b>On the original</b> — re-caption the source audio</span></label>
          </div>
          <div class="vs-row">
            <button class="vs-btn primary" id="cap-burn">🔥 Generate &amp; burn captions</button>
            <span class="vs-hint" id="cap-msg"></span>
          </div>
        </div>

        <div class="vs-panel" id="cap-editpanel" style="display:none">
          <h3>Edit the lines <span class="r vs-hint">fix any word, then burn again</span></h3>
          <div id="cap-lines" style="max-height:300px;overflow:auto"></div>
          <div class="vs-row">
            <button class="vs-btn" id="cap-aifix">✨ AI spell-fix</button>
            <button class="vs-btn" id="cap-save">💾 Save lines</button>
            <button class="vs-btn primary" id="cap-reburn">🔥 Burn with these lines</button>
          </div>
        </div>

        <div class="vs-panel" id="cap-resultwrap" style="display:none">
          <h3>Captioned result</h3>
          <div class="vs-stage"><video controls playsinline id="cap-result"></video></div>
        </div>`;

      let lines = null;

      const loadLines = async () => {
        try {
          const d = await VS.api("/api/captions/" + encodeURIComponent(v.stem));
          if (d.lines && d.lines.length) {
            lines = d.lines;
            const box = el.querySelector("#cap-lines");
            box.innerHTML = "";
            lines.forEach((ln, i) => {
              const row = document.createElement("div");
              row.className = "vs-row";
              row.style.cssText = "flex-wrap:nowrap;margin-top:5px";
              row.innerHTML = `<span class="vs-hint" style="flex:none;width:90px;font-family:var(--mono);font-size:10.5px">
                  ${VS.fmtT(ln.start || 0)}–${VS.fmtT(ln.end || 0)}</span>
                <input class="vs-in" style="flex:1;min-width:0" value="">`;
              const inp = row.querySelector("input");
              inp.value = ln.text || "";
              inp.oninput = () => { lines[i].text = inp.value; };
              box.appendChild(row);
            });
            el.querySelector("#cap-editpanel").style.display = "";
          }
        } catch (e) { /* no lines yet */ }
      };
      ctx.loadLines = loadLines;
      loadLines();

      const burn = async () => {
        const src = el.querySelector('input[name="cap-src"]:checked').value;
        try {
          let r;
          if (src === "dub") r = await VS.post("/api/run", {action: "caption", file: v.stem});
          else r = await VS.post("/api/recaption", {path: "uploads/" + v.name, mode: "captions"});
          VS.drawer.watch(r.job_id);
          VS.toast("🔥 Captioning…");
        } catch (e) { VS.toast("Error: " + e.message); }
      };
      el.querySelector("#cap-burn").onclick = burn;

      el.querySelector("#cap-save").onclick = async () => {
        if (!lines) return;
        try {
          await VS.post("/api/captions/" + encodeURIComponent(v.stem), {lines});
          VS.toast("Lines saved");
        } catch (e) { VS.toast("Error: " + e.message); }
      };
      el.querySelector("#cap-aifix").onclick = async () => {
        if (!lines) return;
        const b = el.querySelector("#cap-aifix");
        b.disabled = true;
        b.textContent = "✨ fixing…";
        try {
          const d = await VS.post("/api/aifix/" + encodeURIComponent(v.stem), {lines});
          if (d.lines) { lines = d.lines; await loadLines(); }
          VS.toast(d.changed ? `Fixed ${d.changed} line(s)` : "Nothing to fix — looks clean");
        } catch (e) { VS.toast("Error: " + e.message); }
        b.disabled = false;
        b.textContent = "✨ AI spell-fix";
      };
      el.querySelector("#cap-reburn").onclick = async () => {
        if (!lines) return;
        try {
          await VS.post("/api/captions/" + encodeURIComponent(v.stem), {lines});
          const r = await VS.post("/api/recaption", {path: "uploads/" + v.name, mode: "burn-lines"});
          VS.drawer.watch(r.job_id);
          VS.toast("🔥 Burning your edited lines…");
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      VS.on("drawer-finished", ctx._df = j => {
        if ((j.slug === v.stem || j.slug === v.name) && ["caption", "recaption"].includes(j.action)) {
          loadLines();
          VS.refreshLibrary();
        }
      });

      this.sync(ctx);
    },

    sync(ctx) {
      const v = ctx.video;
      const el = ctx.el;
      const wrap = el.querySelector("#cap-resultwrap");
      if (!wrap) return;
      el.querySelector("#cap-burn").disabled = !v.dub && !v.transcript;
      el.querySelector("#cap-msg").textContent = v.dub ? "" : "no dub yet — you can still caption the original";
      if (v.captioned) {
        wrap.style.display = "";
        const vid = el.querySelector("#cap-result");
        const src = `/captioned/${encodeURIComponent(v.stem)}?t=${Date.now() >> 14}`;
        if (!vid.dataset.loaded) { vid.src = src; vid.dataset.loaded = "1"; }
      }
    },

    unmount(ctx) {
      if (ctx._df) VS.off("drawer-finished", ctx._df);
    },
  });
})();
