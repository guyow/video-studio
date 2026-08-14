/* Step 2½ — Background: green-screen actor → real scene.
   Runs BEFORE dub/lip-sync by default: the source footage is keyed in place
   (original backed up to uploads/.originals, restorable), so every later step
   — dub, lip-sync, captions — works on the video with the real background.
   Once a dub exists it switches to take mode: the dubbed final gets a new
   versioned take instead, promotable from Fix & QA. */
(function () {
  const VS = window.VS;

  VS.registerStep({
    id: "background",
    order: 2.5,
    title: "Background",

    status(v) {
      if (VS.runningFor(v, ["background-swap"])) return "running";
      return "ready";
    },

    mount(el, ctx) {
      const v = ctx.video;
      let selected = null;   // background name

      el.innerHTML = `
        <div class="vs-panel">
          <div class="vs-stage"><video controls playsinline id="bg-vid"></video></div>
          <div class="vs-hint" id="bg-mode" style="margin-top:6px"></div>
        </div>

        <div class="vs-panel" id="bg-tools">
          <h3>🌄 Green screen → real scene <span class="r vs-free">local ffmpeg · free</span></h3>
          <div class="vs-hint">The green screen is keyed out and the actor is composited over the
            scene you pick. Slight scene blur is on by default — it reads as real camera depth of
            field. Do this before the Dub step so dub + lip-sync run on the finished look.</div>

          <h3 style="margin-top:14px">Scene library</h3>
          <div class="vs-row" id="bg-lib" style="flex-wrap:wrap;gap:8px">
            <span class="vs-hint">loading scenes…</span>
          </div>
          <div class="vs-row">
            <input type="file" id="bg-upload" accept=".png,.jpg,.jpeg,.webp,.mp4,.mov,.webm" style="display:none">
            <button class="vs-btn" id="bg-upload-btn">⬆ Upload a scene (photo or video)</button>
            <span class="vs-hint">eye-level interiors ≥1080p look most real</span>
          </div>
          <h3 style="margin-top:14px">✨ Generate a scene with Nano Banana <span class="r vs-hint">~$0.04 on fal.ai</span></h3>
          <div class="vs-row">
            <input class="vs-in" id="bg-gen-hint" style="flex:1;min-width:220px"
              placeholder='describe the background you want — e.g. "sunny modern kitchen with white marble counters and plants"'>
            <button class="vs-btn primary" id="bg-gen">✨ Generate</button>
          </div>
          <div class="vs-row" style="margin-top:6px">
            <input type="file" id="bg-ref-file" accept=".png,.jpg,.jpeg,.webp" style="display:none">
            <button class="vs-btn" id="bg-ref-btn">🖼 Add a reference photo</button>
            <span id="bg-ref-chip" style="display:none;align-items:center;gap:6px">
              <img id="bg-ref-thumb" style="height:34px;border-radius:6px">
              <span class="vs-hint" id="bg-ref-name"></span>
              <button class="vs-btn" id="bg-ref-clear" title="remove the reference">✕</button>
            </span>
          </div>
          <div class="vs-hint">type it and Nano Banana builds exactly that · leave it empty and it
            reads this video's script and designs the room the speaker would really be in ·
            add a photo (your real room, a frame you like) and it recreates that place as a clean
            empty set — or, combined with a description, uses the photo as the style guide</div>

          <h3 style="margin-top:14px">Key tuning</h3>
          <div class="vs-row">
            <label class="vs-hint">engine
              <select class="vs-in" id="bg-engine" style="width:auto">
                <option value="chroma">green-screen key + 🛡 AI shield</option>
                <option value="ai">🤖 full AI key (no green screen needed)</option>
              </select>
            </label>
          </div>
          <div class="vs-row">
            <label class="vs-hint">strength <input type="range" id="bg-sim" min="0.05" max="0.30" step="0.01" value="0.15" style="vertical-align:middle">
              <span id="bg-sim-val">0.15</span></label>
            <label class="vs-hint">fill holes <input type="range" id="bg-holes" min="0" max="6" step="1" value="2" style="vertical-align:middle">
              <span id="bg-holes-val">2</span></label>
            <label class="vs-hint">scene blur <input type="range" id="bg-blur" min="0" max="16" step="1" value="6" style="vertical-align:middle">
              <span id="bg-blur-val">6</span></label>
            <label class="vs-hint">key <input class="vs-in" id="bg-key" value="auto" style="width:90px" title='"auto" samples the frame borders, or a hex like 0x1FBF3A'></label>
            <label class="vs-hint" title="local AI finds the person and forces them 100% solid — the key can only remove the screen, never the person">
              <input type="checkbox" id="bg-protect" checked> 🛡 don't touch the person (AI shield)</label>
          </div>
          <div class="vs-hint">green fringe left? nudge strength up (max ~0.25 — higher starts eating
            whites like shirts and teeth) · with the 🛡 shield ON the person can never turn
            see-through, so strength only affects the screen edges · 🤖 full AI key ignores the
            sliders completely: the AI decides person vs background — use it when the green screen
            is badly lit, or when there is no green screen at all</div>

          <div class="vs-row" style="margin-top:10px">
            <button class="vs-btn" id="bg-preview">👁 Preview this frame</button>
            <span style="flex:1"></span>
            <button class="vs-btn" id="bg-restore" style="display:none" title="put the backed-up original footage back (undoes the background swap)">↩ Restore original</button>
            <button class="vs-btn" id="bg-save" title="copy this video, with its replaced background, into the Desktop exports folder">💾 Save video</button>
            <button class="vs-btn primary" id="bg-run" disabled>🌄 Replace background</button>
          </div>
          <div id="bg-preview-out" style="margin-top:10px"></div>
        </div>

        <div class="vs-panel">
          <h3>🟩 Reverse — put a GREEN SCREEN behind the person <span class="r vs-free">local · free</span></h3>
          <div class="vs-hint">The opposite direction: the person stays, the background becomes clean
            solid green so the clip can be keyed anywhere later. Videos that already have a
            transparent background are flattened instantly; normal footage goes through local AI
            (RobustVideoMatting) that removes the real background — temporally smooth, soft natural
            hair edges, no flicker. No scene needed.</div>
          <div class="vs-row" style="margin-top:8px">
            <label class="vs-hint">green <input class="vs-in" id="bg-green" value="0x00FF00" style="width:100px" title="the screen colour, 0xRRGGBB"></label>
            <button class="vs-btn" id="bg-rev-preview">👁 Preview this frame</button>
            <span style="flex:1"></span>
            <button class="vs-btn primary" id="bg-rev-run">🟩 Make green screen</button>
          </div>
          <div id="bg-rev-preview-out" style="margin-top:10px"></div>

          <h3 style="margin-top:14px">👤 Extract the person</h3>
          <div class="vs-hint">Pulls the person out with a REAL transparent background — drop the file
            on top of anything in any editor. Works on this video as it is now (after a background
            replace it extracts from the replaced video).</div>
          <div class="vs-row" style="margin-top:8px">
            <select class="vs-in" id="bg-ext-fmt" style="width:auto">
              <option value="webm">transparent .webm (small, slower render)</option>
              <option value="mov">ProRes .mov (big file, fast render)</option>
            </select>
            <button class="vs-btn primary" id="bg-extract">👤 Extract person</button>
            <span class="vs-hint">lands in the Desktop exports folder</span>
          </div>
        </div>`;

      const vid = el.querySelector("#bg-vid");
      const params = () => ({
        stem: v.stem,
        background: selected,
        key_color: el.querySelector("#bg-key").value.trim() || "auto",
        similarity: parseFloat(el.querySelector("#bg-sim").value),
        fill_holes: parseInt(el.querySelector("#bg-holes").value, 10),
        bg_blur: parseFloat(el.querySelector("#bg-blur").value),
        protect_person: el.querySelector("#bg-protect").checked,
        ai_key: el.querySelector("#bg-engine").value === "ai",
      });
      const revParams = () => ({
        stem: v.stem,
        reverse: true,
        green_color: el.querySelector("#bg-green").value.trim() || "0x00FF00",
      });

      ["sim", "holes", "blur"].forEach(k => {
        el.querySelector(`#bg-${k}`).oninput = e =>
          el.querySelector(`#bg-${k}-val`).textContent = e.target.value;
      });

      el.querySelector("#bg-engine").onchange = e => {
        const ai = e.target.value === "ai";   // AI key decides everything itself
        ["bg-sim", "bg-holes", "bg-key", "bg-protect"].forEach(id =>
          el.querySelector("#" + id).disabled = ai);
      };

      const loadLib = async () => {
        const box = el.querySelector("#bg-lib");
        try {
          const d = await VS.api("/api/background/list");
          const items = d.backgrounds || [];
          if (!items.length) {
            box.innerHTML = '<span class="vs-hint">no scenes yet — upload one below (a cozy living room, a kitchen, an office…)</span>';
            return;
          }
          box.innerHTML = "";
          items.forEach(b => {
            const cell = document.createElement("div");
            cell.style.cssText = "width:120px;cursor:pointer;text-align:center";
            cell.innerHTML = b.video
              ? `<div style="height:68px;display:flex;align-items:center;justify-content:center;background:#222;border-radius:8px;border:2px solid transparent">🎞</div>`
              : `<img src="${b.url}" style="width:100%;height:68px;object-fit:cover;border-radius:8px;border:2px solid transparent">`;
            cell.innerHTML += `<div class="vs-hint" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${VS.esc(b.name)}</div>`;
            cell.onclick = () => {
              selected = b.name;
              VS.$$("#bg-lib > div", el).forEach(c => {
                const t = c.firstElementChild;
                if (t) t.style.borderColor = "transparent";
              });
              cell.firstElementChild.style.borderColor = "var(--acc, #6af)";
              el.querySelector("#bg-run").disabled = false;
            };
            box.appendChild(cell);
          });
        } catch (e) {
          box.innerHTML = `<span class="vs-err">couldn't load scenes: ${VS.esc(e.message)}</span>`;
        }
      };
      loadLib();

      el.querySelector("#bg-upload-btn").onclick = () => el.querySelector("#bg-upload").click();
      el.querySelector("#bg-upload").onchange = async e => {
        const f = e.target.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append("file", f);
        try {
          const r = await VS.api("/api/background/upload", {method: "POST", body: fd});
          selected = r.name;
          VS.toast("Scene added: " + r.name);
          await loadLib();
          el.querySelector("#bg-run").disabled = false;
        } catch (err) { VS.toast("Upload failed: " + err.message); }
        e.target.value = "";
      };

      let refName = null;
      el.querySelector("#bg-ref-btn").onclick = () => el.querySelector("#bg-ref-file").click();
      el.querySelector("#bg-ref-file").onchange = async e => {
        const f = e.target.files[0];
        if (!f) return;
        const fd = new FormData();
        fd.append("file", f);
        fd.append("kind", "ref");
        try {
          const r = await VS.api("/api/img/upload", {method: "POST", body: fd});
          refName = r.name;
          el.querySelector("#bg-ref-thumb").src = r.url;
          el.querySelector("#bg-ref-name").textContent = f.name;
          el.querySelector("#bg-ref-chip").style.display = "inline-flex";
          el.querySelector("#bg-ref-btn").style.display = "none";
        } catch (err) { VS.toast("Reference upload failed: " + err.message); }
        e.target.value = "";
      };
      el.querySelector("#bg-ref-clear").onclick = () => {
        refName = null;
        el.querySelector("#bg-ref-chip").style.display = "none";
        el.querySelector("#bg-ref-btn").style.display = "";
      };

      el.querySelector("#bg-gen").onclick = async () => {
        const btn = el.querySelector("#bg-gen");
        const hint = el.querySelector("#bg-gen-hint").value.trim();
        const body = {stem: v.stem, hint};
        if (refName) body.ref = refName;
        btn.disabled = true;
        try {
          let r;
          try {
            r = await VS.post("/api/background/generate", body);
          } catch (e) {
            if (e.status !== 402 || !e.body || !e.body.estimate) throw e;
            if (!confirm(`Generate a scene with Nano Banana?\n\n${e.body.estimate.summary}`)) {
              btn.disabled = false;
              return;
            }
            r = await VS.post("/api/background/generate", Object.assign({confirm_cost: true}, body));
          }
          VS.drawer.watch(r.job_id);
          VS.toast("✨ Nano Banana is designing the scene…");
        } catch (e) { VS.toast("Error: " + e.message); }
        btn.disabled = false;
      };

      el.querySelector("#bg-preview").onclick = async () => {
        if (!selected) { VS.toast("Pick a scene first"); return; }
        const out = el.querySelector("#bg-preview-out");
        const btn = el.querySelector("#bg-preview");
        btn.disabled = true;
        out.innerHTML = '<span class="vs-hint">⏳ compositing one frame…</span>';
        try {
          const r = await VS.post("/api/background/preview",
            Object.assign(params(), {at: vid.currentTime || 1.0}));
          out.innerHTML = `<img src="${r.img}" style="max-width:100%;border-radius:8px">
            <div class="vs-hint">${r.key ? `detected key: ${VS.esc(r.key)} · ` : ""}happy? hit Replace background</div>`;
        } catch (e) {
          out.innerHTML = `<span class="vs-err">preview failed: ${VS.esc(e.message)}</span>`;
        }
        btn.disabled = false;
      };

      el.querySelector("#bg-run").onclick = async () => {
        if (!selected) { VS.toast("Pick a scene first"); return; }
        try {
          const r = await VS.post("/api/background/replace", params());
          VS.drawer.watch(r.job_id);
          VS.toast(r.mode === "source"
            ? "🌄 Rendering — the source video gets the new background (original backed up)"
            : "🌄 Rendering — the dubbed video will carry the new background (old version archived in Takes)");
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      el.querySelector("#bg-rev-preview").onclick = async () => {
        const out = el.querySelector("#bg-rev-preview-out");
        const btn = el.querySelector("#bg-rev-preview");
        btn.disabled = true;
        out.innerHTML = '<span class="vs-hint">⏳ matting one frame… (the first ever run downloads the AI model, ~15 MB)</span>';
        try {
          const r = await VS.post("/api/background/preview",
            Object.assign(revParams(), {at: vid.currentTime || 1.0}));
          out.innerHTML = `<img src="${r.img}" style="max-width:100%;border-radius:8px">
            <div class="vs-hint">happy? hit Make green screen</div>`;
        } catch (e) {
          out.innerHTML = `<span class="vs-err">preview failed: ${VS.esc(e.message)}</span>`;
        }
        btn.disabled = false;
      };

      el.querySelector("#bg-rev-run").onclick = async () => {
        try {
          const r = await VS.post("/api/background/replace", revParams());
          VS.drawer.watch(r.job_id);
          VS.toast(r.mode === "source"
            ? "🟩 Rendering — the source video becomes green-screen footage (original backed up)"
            : "🟩 Rendering — the green-screen take lands in Fix & QA, promote it there");
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      el.querySelector("#bg-save").onclick = async () => {
        const btn = el.querySelector("#bg-save");
        btn.disabled = true;
        try {
          const r = await VS.post("/api/background/save", {stem: v.stem});
          VS.toast(`💾 Saved as ${r.saved} in ${r.dir}`);
        } catch (e) { VS.toast("Save failed: " + e.message); }
        btn.disabled = false;
      };

      el.querySelector("#bg-extract").onclick = async () => {
        try {
          const r = await VS.post("/api/background/extract",
            {stem: v.stem, format: el.querySelector("#bg-ext-fmt").value});
          VS.drawer.watch(r.job_id);
          VS.toast("👤 Extracting the person — the transparent file lands in the Desktop exports folder");
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      el.querySelector("#bg-restore").onclick = async () => {
        try {
          await VS.post("/api/clean-restore", {file: v.name});
          VS.toast("↩ Original footage restored");
          VS.refreshLibrary();
        } catch (e) { VS.toast("Restore failed: " + e.message); }
      };

      VS.on("drawer-finished", ctx._bgdf = j => {
        if (j.slug === v.stem && j.action === "background-swap") {
          VS.toast(v.dub
            ? "🌄 Background done — the dubbed video now carries the new scene; hit 💾 Save video to send it to Desktop"
            : "🌄 Background done — hit 💾 Save video to send it to Desktop, or continue to Dub");
          VS.refreshLibrary();
          if (ctx.loadTakes) ctx.loadTakes();
        }
        if (j.slug === v.stem && j.action === "person-extract") {
          VS.toast("👤 Person extracted — the transparent file is in the Desktop exports folder");
        }
        if (j.slug === v.stem && j.action === "background-scene") {
          VS.toast("✨ Scene ready — it's selected, hit Preview to see the actor in it");
          loadLib().then(() => {
            const first = el.querySelector("#bg-lib > div");   // list is newest-first
            if (first) first.click();
          });
        }
      });

      this.sync(ctx);
    },

    sync(ctx) {
      const v = ctx.video;
      const el = ctx.el;
      const vid = el.querySelector("#bg-vid");
      if (!vid) return;
      const want = v.dub
        ? "/media/" + v.dub + "?v=" + (v.dub_mtime || "")
        : "/media/uploads/" + encodeURIComponent(v.name) + "?v=" + (v.mtime || "");
      if (!vid.src.endsWith(encodeURI(want))) vid.src = want;
      el.querySelector("#bg-mode").textContent = v.dub
        ? "a dub exists → the swap renders a NEW take of the dubbed final (promote it in Fix & QA)"
        : "no dub yet → the swap applies to the source footage itself; dub + lip-sync will run on the keyed video (original backed up, restorable)";
      el.querySelector("#bg-restore").style.display = v.cleaned ? "" : "none";
    },

    unmount(ctx) {
      if (ctx._bgdf) VS.off("drawer-finished", ctx._bgdf);
    },
  });
})();
