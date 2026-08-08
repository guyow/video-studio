/* editor-app.js v3 — full professional taxonomy (user-spec), gray & white.
   Left: 9 tool tabs, each with its own sub-nav + content (grids / AI panels).
   Right: Details + action chips (Clean/Script/Dub/Fix/Deliver — the pipeline).
   Bottom: editing timeline — filmstrip, click-to-seek, live playhead, and a
   REAL Cut tool (mark In/Out → /api/edit exports the range as a new clip).
   Every wired control hits a real endpoint; unbuilt library packs are visible
   but tagged SOON — nothing pretends. */
(function () {
  const VS = window.VS;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => [...(r || document).querySelectorAll(s)];

  const ED = {
    video: null, tab: "media", sec: "media", chip: "details",
    take: "final", ratio: "9:16", fit: "contain",
    voices: [], i2v: [], actors: [],
    search: "", dur: 0, pxs: 100, filmKey: null,
    cutIn: null, cutOut: null,
    aiImg: null,            // uploaded reference for AI Image / Photo→Avatar
  };
  const HEADW = 118;

  function savedNow() {
    $("#ed-savetime").textContent = "Auto saved: " + new Date().toLocaleTimeString();
  }
  const _post = VS.post;
  VS.post = async (p, b) => { const r = await _post(p, b); savedNow(); return r; };

  /* ═════════════ LEFT: tabs + sub-nav spec ═════════════ */
  const TABS = [
    {id: "media",       ic: "🎞", label: "Media"},
    {id: "audio",       ic: "🎵", label: "Audio"},
    {id: "text",        ic: "🅃",  label: "Text"},
    {id: "stickers",    ic: "😀", label: "Stickers"},
    {id: "effects",     ic: "✨", label: "Effects"},
    {id: "transitions", ic: "⧉",  label: "Transit."},
    {id: "captions",    ic: "💬", label: "Captions"},
    {id: "filters",     ic: "🎚", label: "Filters"},
    {id: "avatar",      ic: "🧑‍🚀", label: "AI Avatar"},
  ];
  const NAV = {
    media: [["h", "Import"], ["media", "Media"], ["subprojects", "Subprojects"],
            ["h", "Yours"], ["fav", "Favorites"], ["my", "My"], ["brand", "Brand assets"],
            ["h", "Generate AI"], ["aiimage", "AI Image"], ["aivideo", "AI Video"], ["aidialog", "AI Dialog"]],
    audio: [["h", "Audio"], ["extract", "Extract audio"], ["import", "Import audio"], ["lib", "Library"]],
    text: [["h", "Text"], ["add", "Add text"], ["fx", "Text effects"], ["tpl", "Templates"],
           ["h", "Captions"], ["auto", "Auto caption"], ["local", "Local caption"]],
    stickers: [["h", "Stickers"], ["aigen", "AI generated"], ["trend", "Trending"],
               ["memes", "Memes"], ["classic", "Classic"], ["new", "New"]],
    effects: [["h", "Video effects"], ["trend", "Trending"], ["classic", "Classic"],
              ["h", "Body effects"], ["blur", "Blur"], ["heat", "Heat"], ["clone", "Clone"]],
    transitions: [["h", "Transitions"], ["trend", "Trending"], ["classic", "Classic"], ["new", "New"],
                  ["heat", "Heat"], ["free", "Free"], ["fire", "Fire"], ["overlay", "Overlay"]],
    captions: [["h", "Captions"], ["auto", "Auto caption"], ["tpl", "Templates"],
               ["lyrics", "Auto lyrics"], ["add", "Add caption"]],
    filters: [["h", "Filters"], ["movie", "Movie"], ["mono", "Mono"], ["portrait", "Portrait"],
              ["retro", "Retro"], ["night", "Night"], ["alt", "Alternative"], ["food", "Food"]],
    avatar: [["h", "AI Avatar"], ["lib", "Avatar library"], ["photo", "Photo to Avatar"],
             ["fashion", "Fashion model"], ["translate", "Video translator"], ["dialog", "AI Dialog"]],
  };

  function renderTabs() {
    $("#ed-tabs").innerHTML = TABS.map(t =>
      `<button class="ed-tab ${ED.tab === t.id ? "on" : ""}" data-t="${t.id}">
        <span class="ic">${t.ic}</span>${t.label}</button>`).join("");
    $$(".ed-tab").forEach(b => b.onclick = () => {
      ED.tab = b.dataset.t;
      ED.sec = (NAV[ED.tab].find(x => x[0] !== "h") || ["media"])[0];
      renderTabs(); renderSubnav(); renderContent(); syncURL();
    });
  }

  function renderSubnav() {
    $("#ed-subnav").innerHTML = (NAV[ED.tab] || []).map(([k, lab]) =>
      k === "h" ? `<div class="snh">${lab}</div>`
        : `<button class="sn ${ED.sec === k ? "on" : ""}" data-k="${k}">${lab}</button>`).join("");
    $$("#ed-subnav .sn").forEach(b => b.onclick = () => {
      ED.sec = b.dataset.k;
      renderSubnav(); renderContent(); syncURL();
    });
  }

  /* ═════════════ content helpers ═════════════ */
  const soonTiles = (items) => `<div class="ed-tiles">` + items.map(([e, n]) =>
    `<div class="ed-tile"><span class="soon">SOON</span><span class="big">${e}</span>${n}</div>`).join("") + "</div>";
  const panel = (h, sub, body) =>
    `<div class="ed-panel"><h4>${h}</h4><div class="ph4">${sub}</div>${body}</div>`;

  function videoCard(v) {
    const run = VS.runningFor(v);
    const fav = (v.tags || []).includes("fav");
    const badge = run ? `<span class="bdg run">⏳ ${VS.esc(run.action)}</span>`
      : v.exported ? '<span class="bdg ok">DELIVERED</span>'
      : v.dub ? '<span class="bdg">DUBBED</span>' : "";
    return `<div class="ed-card ${ED.video && ED.video.name === v.name ? "sel" : ""}" data-n="${VS.esc(v.name)}">
      <div class="th"><img loading="lazy" src="/api/thumb/${encodeURIComponent(v.name)}" alt="">${badge}
        <button class="ed-fav" data-fav="${VS.esc(v.name)}" title="favorite">${fav ? "♥" : "♡"}</button></div>
      <div class="nm">${VS.esc(v.title || v.name)}</div></div>`;
  }

  function bindCards(root) {
    $$(".ed-card[data-n]", root).forEach(el => el.onclick = e => {
      if (e.target.dataset.fav) return;
      const v = VS.state.videos.find(x => x.name === el.dataset.n);
      if (v) select(v);
    });
    $$(".ed-fav", root).forEach(b => b.onclick = async e => {
      e.stopPropagation();
      const v = VS.state.videos.find(x => x.name === b.dataset.fav);
      if (!v) return;
      const tags = new Set(v.tags || []);
      tags.has("fav") ? tags.delete("fav") : tags.add("fav");
      try { await VS.post("/api/creator/meta", {name: v.name, tags: [...tags]}); VS.refreshLibrary(); }
      catch (err) { VS.toast("Error: " + err.message); }
    });
  }

  function grid(vids, empty) {
    if (!vids.length) return `<div class="ed-empty">${empty}</div>`;
    return `<div class="ed-grid">` + vids.map(videoCard).join("") + "</div>";
  }

  /* ═════════════ content per tab.section ═════════════ */
  const C = {};

  // ── MEDIA ──
  C["media.media"] = () => {
    const q = ED.search.toLowerCase();
    const vids = VS.state.videos.filter(v => !q || v.name.toLowerCase().includes(q)
      || (v.title || "").toLowerCase().includes(q));
    let html = grid(vids, "no media yet — Import above");
    if (ED.i2v.length) {
      html += `<div class="snh" style="padding:12px 2px 6px">GENERATED CLIPS (Image→Video)</div><div class="ed-grid">`
        + ED.i2v.filter(c => c.ready).map(c =>
          `<div class="ed-card" data-clip="${VS.esc(c.clip)}"><div class="th">
             <video muted preload="metadata" src="/media/${c.clip}" style="width:100%;height:100%;object-fit:cover"></video>
             <span class="bdg">AI CLIP</span></div>
           <div class="nm">${VS.esc((c.prompt || c.slug).slice(0, 30))}</div></div>`).join("") + "</div>";
    }
    return html;
  };
  C["media.media"].bind = root => {
    bindCards(root);
    $$("[data-clip]", root).forEach(el => el.onclick = () => window.open("/media/" + el.dataset.clip, "_blank"));
  };
  C["media.subprojects"] = () => grid(VS.state.videos.filter(v => v.dub),
    "no subprojects yet — a video becomes one once it's dubbed");
  C["media.subprojects"].bind = bindCards;
  C["media.fav"] = () => grid(VS.state.videos.filter(v => (v.tags || []).includes("fav")),
    "no favorites — tap ♡ on any card");
  C["media.fav"].bind = bindCards;
  C["media.my"] = () => grid(VS.state.videos, "your uploads appear here");
  C["media.my"].bind = bindCards;
  C["media.brand"] = () => panel("Brand assets", "the locked liitt brand kit — used by Brand Studio",
    `<div class="ed-tiles">
      <div class="ed-tile live" data-b="banks/brand-assets/wordmark/official-logo.png"><span class="big">🏷</span>official logo</div>
      <div class="ed-tile live" data-b="banks/brand-assets/wordmark/liitt-gold-on-dark.png"><span class="big">🌙</span>gold on dark</div>
      <div class="ed-tile live" data-b="banks/brand-assets/wordmark/liitt-light.png"><span class="big">☀️</span>light</div>
    </div><div class="ed-note">fonts + kit tokens live in <a href="/brand-studio">Brand Studio ↗</a></div>`);
  C["media.brand"].bind = root => $$("[data-b]", root).forEach(t =>
    t.onclick = () => window.open("/media/" + t.dataset.b, "_blank"));

  C["media.aiimage"] = () => panel("AI Image", "describe an image — rendered locally on your GPU (free, ComfyUI)",
    `<div class="ed-field"><label>Reference image (optional — guides the result)</label>
       <button class="ed-run ghostly" id="ai-imgpick">${ED.aiImg ? "✓ image attached — replace" : "⬆ Upload an image"}</button>
       <input type="file" id="ai-imgfile" accept="image/*" style="display:none"></div>
     <div class="ed-field"><label>Describe your image</label>
       <textarea id="ai-prompt" rows="3" placeholder="e.g. mushroom gummies jar on a marble counter, soft morning light, photorealistic"></textarea></div>
     <div class="ed-field"><label>How many results</label>
       <select id="ai-count"><option>1</option><option>2</option><option selected>4</option><option>6</option><option>8</option></select></div>
     <button class="ed-run" id="ai-go">🖼 Generate</button>
     <div class="ed-cost free">free · local GPU · needs ComfyUI running</div>
     <div id="ai-results" class="ed-tiles" style="margin-top:10px"></div>`);
  C["media.aiimage"].bind = root => bindAiImage(root, "");
  function bindAiImage(root, promptSuffix) {
    const pick = $("#ai-imgpick", root), file = $("#ai-imgfile", root);
    pick.onclick = () => file.click();
    file.onchange = async e => {
      const f = e.target.files[0];
      if (!f) return;
      const fd = new FormData(); fd.append("image", f);
      pick.textContent = "uploading…";
      try {
        const r = await fetch("/api/studio/upload", {method: "POST", body: fd});
        const d = await r.json();
        ED.aiImg = d.path; pick.textContent = "✓ image attached — replace"; savedNow();
      } catch (err) { pick.textContent = "⬆ Upload an image"; VS.toast("Upload failed"); }
    };
    $("#ai-go", root).onclick = async () => {
      const prompt = ($("#ai-prompt", root).value.trim() + " " + promptSuffix).trim();
      if (!prompt) { VS.toast("Describe the image first"); return; }
      const btn = $("#ai-go", root), out = $("#ai-results", root);
      btn.disabled = true; btn.textContent = "🖼 rendering… (1-3 min)";
      out.innerHTML = "";
      try {
        const body = ED.aiImg
          ? {mode: "keyframe", image: ED.aiImg, prompt}
          : {mode: "generate", prompt, count: +$("#ai-count", root).value};
        const d = await VS.post("/api/studio/run", body);
        if (d.ok && d.results.length) {
          out.innerHTML = d.results.map(u =>
            `<div class="ed-tile live" style="padding:0;overflow:hidden" data-img="${u}">
               <img src="${u}" style="width:100%;height:100%;object-fit:cover"></div>`).join("");
          $$("[data-img]", out).forEach(t => t.onclick = () => window.open(t.dataset.img, "_blank"));
          VS.toast(`✓ ${d.results.length} image(s) rendered`);
        } else {
          out.innerHTML = `<div class="ed-empty">no result — is ComfyUI running? (run_nvidia_lowvram.bat)</div>`;
        }
      } catch (e) { VS.toast("Generate failed: " + e.message); }
      btn.disabled = false; btn.textContent = "🖼 Generate";
    };
  }

  C["media.aivideo"] = () => panel("AI Video", "make clips with AI",
    `<div class="ed-cats" id="av-cats">
       <button class="ed-cat on" data-c="i2v">Image to Video</button>
       <button class="ed-cat" data-c="t2v">Omni / Text to Video</button>
       <button class="ed-cat" data-c="mf">Multiframe</button></div>
     <div id="av-body"></div>`);
  C["media.aivideo"].bind = root => {
    const body = $("#av-body", root);
    const show = c => {
      $$(".ed-cat", root).forEach(x => x.classList.toggle("on", x.dataset.c === c));
      if (c === "i2v") {
        body.innerHTML = `<div class="ed-note" style="margin-bottom:8px">A picture + a motion prompt → up to a 30s clip (fal.ai, cost-gated). Chained segments keep it continuous.</div>
          <a class="ed-run" style="display:block;text-align:center;text-decoration:none" href="/image-to-video">🎬 Open Image→Video ↗</a>
          <div class="ed-note">finished clips appear under Media as “AI CLIP”.</div>`;
      } else if (c === "t2v") {
        const v = ED.video;
        if (!v) {
          body.innerHTML = `<div class="ed-note">Pick (or Import) a video in <b>Media</b> first. Text→Video copies a piece of it, then AI-generates new footage that continues from your new script.</div>`;
          return;
        }
        body.innerHTML = `
          <div class="ed-note" style="margin-bottom:8px">Keep a piece of <b>${VS.esc(v.title || v.name)}</b>, then fal.ai generates new footage that continues from its last frame following your script. Stitched into one clip.</div>
          <div class="ed-field"><label>Copy from → to (seconds · blank = whole clip)</label>
            <div style="display:flex;gap:8px">
              <input type="number" id="t2-start" placeholder="0" min="0" step="0.5">
              <input type="number" id="t2-end" placeholder="end" min="0" step="0.5"></div></div>
          <div class="ed-field"><label>New script — what the new footage shows / does</label>
            <textarea id="t2-prompt" rows="3" placeholder="e.g. she smiles and holds up the pouch, slow push-in, warm morning light"></textarea></div>
          <div class="ed-field"><label>Generate seconds</label>
            <select id="t2-secs"><option>5</option><option selected>10</option><option>15</option><option>20</option><option>30</option></select></div>
          <div class="ed-field"><label>Generator (fal.ai)</label>
            <select id="t2-model"><option value="kling-2.1">Kling 2.1 — balanced</option><option value="hailuo-02">Hailuo 02 — great motion</option><option value="kling-2.1-pro">Kling 2.1 Pro — best quality</option><option value="wan-2.2">Wan 2.2 — budget</option></select></div>
          <div class="ed-field"><label>Aspect</label><select id="t2-aspect"><option>9:16</option><option>16:9</option><option>1:1</option></select></div>
          <button class="ed-run" id="t2-go">⚡ Copy &amp; continue</button>
          <div class="ed-cost paid">💰 fal.ai — you approve the exact cost first</div>
          <div class="ed-note" id="t2-out"></div>`;
        const go = $("#t2-go", root);
        go.onclick = async () => {
          const prompt = $("#t2-prompt", root).value.trim();
          if (!prompt) { VS.toast("Describe the new footage first"); return; }
          const payload = extra => JSON.stringify({file: v.name,
            start: +($("#t2-start", root).value || 0), end: +($("#t2-end", root).value || 0),
            prompt, model: $("#t2-model", root).value, aspect: $("#t2-aspect", root).value,
            seconds: +$("#t2-secs", root).value, ...(extra || {})});
          const hdr = {"Content-Type": "application/json"};
          go.disabled = true;
          try {
            let r = await fetch("/api/t2v/continue", {method: "POST", headers: hdr, body: payload()});
            if (r.status === 402) {
              const d = await r.json();
              if (!confirm(`⚠ This generates footage on fal.ai (spends money).\n\n${d.estimate.summary}\n\nApprove and start?`)) { go.disabled = false; return; }
              r = await fetch("/api/t2v/continue", {method: "POST", headers: hdr, body: payload({confirm_cost: true})});
            }
            if (!r.ok) throw new Error((await r.text()).slice(0, 180));
            VS.toast("⚡ Copying + generating on fal.ai — watch the timeline"); renderTimeline();
            $("#t2-out", root).innerHTML = "⚡ working… the finished clip lands under <b>Media</b> as an AI CLIP when done.";
          } catch (e) { VS.toast("Couldn't start: " + e.message); go.disabled = false; }
        };
      } else {
        body.innerHTML = `<div class="ed-tiles"><div class="ed-tile"><span class="soon">SOON</span><span class="big">🎞</span>Multiframe</div></div>
          <div class="ed-note">keyframe-to-keyframe video is on the roadmap.</div>`;
      }
    };
    $$(".ed-cat", root).forEach(b => b.onclick = () => show(b.dataset.c));
    show("i2v");
  };
  C["media.aidialog"] = () => panel("AI Dialog", "two AI voices in conversation",
    soonTiles([["🗨️", "Dialog scenes"]]) +
    `<div class="ed-note">today you can already voice TWO real speakers with <a href="/?step=dub">Interview mode ↗</a>.</div>`);

  // ── AUDIO ──
  C["audio.extract"] = () => {
    const v = ED.video;
    return panel("Extract audio", "pull the audio track out of a video as an mp3",
      v ? `<div class="ed-note" style="margin-bottom:8px">from: <b>${VS.esc(v.title || v.name)}</b></div>
           <button class="ed-run" id="ax-go">🎵 Extract audio</button><div class="ed-note" id="ax-out"></div>`
        : `<div class="ed-empty">pick a video in Media first</div>`);
  };
  C["audio.extract"].bind = root => {
    const b = $("#ax-go", root);
    if (!b) return;
    b.onclick = async () => {
      b.disabled = true; b.textContent = "🎵 extracting…";
      try {
        const d = await VS.post("/api/extract-audio", {file: ED.video.name});
        $("#ax-out", root).innerHTML = `✓ <a href="/media/${d.audio}" target="_blank">${d.audio.split("/").pop()}</a> (${VS.fmtSize(d.size)}) — also in Exports`;
        VS.toast("Audio extracted");
      } catch (e) { VS.toast("Extract failed: " + e.message); }
      b.disabled = false; b.textContent = "🎵 Extract audio";
    };
  };
  C["audio.import"] = () => panel("Import audio", "mp3 · m4a · wav — lands in your library",
    `<button class="ed-run" id="au-imp">⬆ Import audio file</button>
     <div class="ed-note">voice references for the Voice Bank also live here.</div>`);
  C["audio.import"].bind = root => { $("#au-imp", root).onclick = () => $("#ed-file").click(); };
  C["audio.lib"] = () => {
    const auds = VS.state.videos.filter(v => /\.(mp3|m4a|wav)$/i.test(v.name));
    return panel("Audio library", "your imported audio",
      auds.length ? auds.map(a => `<div class="ed-opt" style="cursor:default"><div class="t">🎵 ${VS.esc(a.name)}</div>
        <audio controls preload="none" src="/media/uploads/${encodeURIComponent(a.name)}" style="width:100%;margin-top:6px"></audio></div>`).join("")
      : `<div class="ed-empty">no audio files yet</div>`);
  };

  // ── TEXT ──
  C["text.add"] = () => panel("Add text", "overlay titles on the video",
    `<div class="ed-cats"><button class="ed-cat on">Default</button><button class="ed-cat">Yours</button></div>` +
    soonTiles([["🅰", "Headline"], ["𝘉", "Sub line"], ["✎", "Handwrite"], ["◻", "Lower third"]]) +
    `<div class="ed-note">burned word-timed captions are LIVE under the Captions tab — text overlays are next.</div>`);
  C["text.fx"] = () => panel("Text effects", "styles & animations",
    soonTiles([["✨", "Glow"], ["🌈", "Gradient"], ["🫨", "Shake"], ["🌀", "Spin in"], ["💧", "Drop"], ["🔥", "Burn"]]));
  C["text.tpl"] = () => panel("Text templates", "ready-made layouts",
    soonTiles([["📰", "News bar"], ["💬", "Chat bubble"], ["🏷", "Price tag"], ["⭐", "Review"], ["📢", "CTA"], ["🎬", "Title card"]]));
  C["text.auto"] = () => panel("Auto caption", "word-timed captions from the audio",
    `<button class="ed-run" id="tx-cap">💬 Open Auto caption</button>`);
  C["text.auto"].bind = root => { $("#tx-cap", root).onclick = () => { ED.tab = "captions"; ED.sec = "auto";
    renderTabs(); renderSubnav(); renderContent(); }; };
  C["text.local"] = () => panel("Local caption", "import an existing caption file",
    soonTiles([["📄", "SRT"], ["🎼", "LRC"], ["🎬", "ASS"]]) +
    `<div class="ed-note">importing caption files is on the roadmap — generated captions are live under Captions.</div>`);

  // ── STICKERS ──
  C["stickers.aigen"] = () => panel("AI generated stickers", "make a sticker with the local image AI",
    `<div class="ed-field"><label>Describe the sticker</label>
       <textarea id="ai-prompt" rows="2" placeholder="e.g. cute mushroom mascot waving"></textarea></div>
     <div class="ed-field" style="display:none"><button id="ai-imgpick"></button><input type="file" id="ai-imgfile"></div>
     <div class="ed-field"><label>How many</label>
       <select id="ai-count"><option>1</option><option selected>4</option><option>6</option></select></div>
     <button class="ed-run" id="ai-go">😀 Generate stickers</button>
     <div id="ai-results" class="ed-tiles" style="margin-top:10px"></div>`);
  C["stickers.aigen"].bind = root => { ED.aiImg = null; bindAiImage(root, ", sticker style, bold outline, plain background"); };
  const stTiles = soonTiles([["😂", "LOL"], ["🔥", "Fire"], ["💯", "100"], ["🫶", "Hearts"], ["😎", "Cool"],
    ["🎉", "Party"], ["🐸", "Meme frog"], ["🍄", "Shroom"], ["⭐", "Stars"]]);
  C["stickers.trend"] = () => panel("Trending stickers", "what's hot right now", stTiles);
  C["stickers.memes"] = () => panel("Memes", "meme pack", stTiles);
  C["stickers.classic"] = () => panel("Classic", "evergreen pack", stTiles);
  C["stickers.new"] = () => panel("New", "fresh drops", stTiles);

  // ── EFFECTS ──
  const fxTiles = soonTiles([["📼", "Shaky glitch"], ["🌫", "Dreamy"], ["⚡", "Flash"], ["🎞", "Film grain"],
    ["🫧", "Bokeh"], ["🌈", "Prism"], ["🕶", "Negative split"], ["💥", "Zoom pop"]]);
  C["effects.trend"] = () => panel("Video effects — Trending", "the loud ones", fxTiles);
  C["effects.classic"] = () => panel("Video effects — Classic", "the timeless ones", fxTiles);
  C["effects.blur"] = () => panel("Body effects — Blur", "subject-aware blur",
    soonTiles([["🌀", "Body blur"], ["👤", "Face blur"], ["🏃", "Motion trail"]]));
  C["effects.heat"] = () => panel("Body effects — Heat", "energy on the subject",
    soonTiles([["🔥", "Aura"], ["⚡", "Lightning"], ["✨", "Sparkle skin"]]));
  C["effects.clone"] = () => panel("Body effects — Clone", "multiply the subject",
    soonTiles([["👥", "Echo clone"], ["🪞", "Mirror"], ["🌊", "Trail clone"]]) +
    `<div class="ed-note">object & face-aware REPAIRS (the practical cousin of body effects) are live — right panel → Fix.</div>`);

  // ── TRANSITIONS ──
  const trTiles = soonTiles([["◧", "Wipe"], ["⬒", "Split"], ["◐", "Circle"], ["✦", "Flash"],
    ["🌀", "Warp"], ["📄", "Page"], ["🎞", "Film burn"], ["⚡", "Glitch"]]);
  ["trend", "classic", "new", "heat", "free", "fire", "overlay"].forEach(k => {
    C["transitions." + k] = () => panel("Transitions — " + k[0].toUpperCase() + k.slice(1),
      "between-clip moves", trTiles);
  });

  // ── CAPTIONS ──
  C["captions.auto"] = () => {
    const v = ED.video;
    const run = v && VS.runningFor(v, ["caption", "recaption"]);
    return panel("Auto caption", "word-timed captions generated from the actual audio",
      (v ? `<div class="ed-field"><label>Spoken language</label>
        <select id="cc-lang"><option value="auto" selected>Auto detect</option><option>English</option>
        <option>Spanish</option><option>German</option><option>French</option><option>Hebrew</option></select></div>
      <div class="ed-field"><label style="display:flex;gap:6px;align-items:center">
        <input type="checkbox" disabled> Bilingual caption <span class="soon" style="position:static;margin-left:6px">SOON</span></label></div>
      ${run ? '<div class="ed-msg">⏳ burning… watch the timeline</div>' : ""}
      <button class="ed-run" id="cc-gen" ${run ? "disabled" : ""}>💬 Generate</button>
      <button class="ed-run ghostly" id="cc-del" disabled>🗑 Delete current caption <span class="soon" style="position:static;margin-left:4px">SOON</span></button>
      ${v.captioned ? `<div class="ed-note">✓ captioned — <a href="/captioned/${encodeURIComponent(v.stem)}" target="_blank">watch ↗</a> · edit lines in the <a href="/?v=${encodeURIComponent(v.name)}&step=captions">classic view ↗</a></div>` : ""}`
      : `<div class="ed-empty">pick a video in Media first</div>`));
  };
  C["captions.auto"].bind = root => {
    const b = $("#cc-gen", root);
    if (!b) return;
    b.onclick = async () => {
      const v = ED.video;
      try {
        if (v.dub) await VS.post("/api/run", {action: "caption", file: v.stem});
        else await VS.post("/api/recaption", {path: "uploads/" + v.name, mode: "captions"});
        VS.toast("💬 Generating captions…"); renderTimeline();
      } catch (e) { VS.toast("Error: " + e.message); }
    };
  };
  C["captions.tpl"] = () => panel("Caption templates", "designs for the burned captions",
    `<div class="ed-cats"><button class="ed-cat on">Trending</button><button class="ed-cat">Classic</button>
      <button class="ed-cat">New</button><button class="ed-cat">Heat</button></div>
     <div class="ed-tiles">
       <div class="ed-tile live" id="cc-house"><span class="big">💬</span>House Bold<br><span style="color:var(--ok);font-size:8px">LIVE</span></div>
       ${[["🌈", "Pop line"], ["📦", "Boxed"], ["🖍", "Marker"], ["⚡", "Impact"], ["🫧", "Soft"]].map(([e, n]) =>
         `<div class="ed-tile"><span class="soon">SOON</span><span class="big">${e}</span>${n}</div>`).join("")}
     </div>`);
  C["captions.tpl"].bind = root => {
    $("#cc-house", root).onclick = async () => {
      const v = ED.video;
      if (!v) { VS.toast("Pick a video first"); return; }
      try {
        if (v.dub) await VS.post("/api/run", {action: "caption", file: v.stem});
        else await VS.post("/api/recaption", {path: "uploads/" + v.name, mode: "captions"});
        VS.toast("💬 Burning House Bold captions…");
      } catch (e) { VS.toast("Error: " + e.message); }
    };
  };
  C["captions.lyrics"] = () => panel("Auto lyrics", "timed lyrics for music videos",
    `<div class="ed-field"><label>Language</label><select disabled><option>Auto detect</option></select></div>
     <button class="ed-run ghostly" disabled>🎼 Generate lyrics <span class="soon" style="position:static;margin-left:4px">SOON</span></button>`);
  C["captions.add"] = () => panel("Add caption", "import an existing caption file",
    `<div class="ed-tiles">${[["📄", "SRT"], ["🎼", "LRC"], ["🎬", "ASS"]].map(([e, n]) =>
      `<div class="ed-tile"><span class="soon">SOON</span><span class="big">${e}</span>${n}</div>`).join("")}</div>
     <div class="ed-note">supported formats will be SRT · LRC · ASS.</div>`);

  // ── FILTERS ──
  const filterPack = names => soonTiles(names.map(n => ["🎞", n]));
  C["filters.movie"] = () => panel("Filters — Movie", "cinema grades", filterPack(["Teal&Orange", "Noir", "Blockbuster", "Indie"]));
  C["filters.mono"] = () => panel("Filters — Mono", "black & white", filterPack(["Classic B&W", "High key", "Low key", "Silver"]));
  C["filters.portrait"] = () => panel("Filters — Portrait", "skin-friendly looks", filterPack(["Soft skin", "Golden hour", "Studio", "Natural"]));
  C["filters.retro"] = () => panel("Filters — Retro", "vintage vibes", filterPack(["VHS", "70s film", "Polaroid", "Sepia"]));
  C["filters.night"] = () => panel("Filters — Night", "after dark", filterPack(["Neon", "Moonlight", "City glow", "Midnight"]));
  C["filters.alt"] = () => panel("Filters — Alternative", "the weird ones", filterPack(["Bleach", "Cross process", "Infrared", "Duotone"]));
  C["filters.food"] = () => panel("Filters — Food", "make it delicious", filterPack(["Fresh", "Warm plate", "Crisp", "Juicy"]));

  // ── AI AVATAR ──
  C["avatar.lib"] = () => panel("Avatar library", "people from your own footage — ready to voice & clone",
    ED.actors.length
      ? `<div class="ed-grid">` + ED.actors.map(a =>
          `<div class="ed-card" data-act="${VS.esc(a.name)}"><div class="th">
             <img loading="lazy" src="/api/thumb/${encodeURIComponent(a.name)}"><span class="bdg">YOURS</span></div>
           <div class="nm">${VS.esc(a.name)}</div></div>`).join("") + "</div>"
        + `<div class="ed-note">use any of them as the actor in <a href="/?step=deliver">Clone Winner ↗</a>. A generated-avatar pack is coming.</div>`
      : soonTiles([["🧑‍🚀", "AI people pack"]]));
  C["avatar.lib"].bind = root => $$("[data-act]", root).forEach(el => el.onclick = () => {
    const v = VS.state.videos.find(x => x.name === el.dataset.act);
    if (v) select(v);
  });
  C["avatar.photo"] = () => panel("Photo to Avatar", "upload a photo → a stylized avatar (local GPU)",
    `<div class="ed-field"><label>Your photo</label>
       <button class="ed-run ghostly" id="ai-imgpick">${ED.aiImg ? "✓ photo attached — replace" : "⬆ Upload a photo"}</button>
       <input type="file" id="ai-imgfile" accept="image/*" style="display:none"></div>
     <div class="ed-field"><label>Avatar style</label>
       <textarea id="ai-prompt" rows="2" placeholder="e.g. professional headshot, warm studio light, confident smile"></textarea></div>
     <div class="ed-field" style="display:none"><select id="ai-count"><option>1</option></select></div>
     <button class="ed-run" id="ai-go">🧑‍🚀 Create avatar</button>
     <div id="ai-results" class="ed-tiles" style="margin-top:10px"></div>`);
  C["avatar.photo"].bind = root => { ED.aiImg = null; bindAiImage(root, ", portrait avatar, high detail"); };
  C["avatar.fashion"] = () => panel("Fashion model", "ready model bank",
    soonTiles([["🕴", "Studio A"], ["💃", "Street"], ["🧥", "Editorial"], ["👟", "Sport"]]));
  C["avatar.translate"] = () => {
    const v = ED.video;
    return panel("Video translator", "re-voice this video in another language — voice cloned, lips re-synced",
      v ? `<div class="ed-note" style="margin-bottom:6px">video: <b>${VS.esc(v.title || v.name)}</b>${v.script ? "" : " · <span style='color:var(--warn)'>needs a saved Script (the translated words)</span>"}</div>
        <div class="ed-field"><label>Target language</label>
          <select id="vt-lang"><option value="es">Spanish</option><option value="de">German</option>
          <option value="fr">French</option><option value="pt">Portuguese</option>
          <option value="it">Italian</option><option value="en">English</option></select></div>
        <div class="ed-field"><label style="display:flex;gap:6px;align-items:center">
          <input type="checkbox" id="vt-lips" checked> re-sync the lips (Wav2Lip HD, free)</label></div>
        <button class="ed-run" id="vt-go" ${v.script ? "" : "disabled"}>🌍 Translate & dub</button>
        <div class="ed-cost free">free · local XTTS speaks the saved script in the chosen language</div>
        <div class="ed-note">write the translated script first: right panel → Script (the ✨ AI rewrite can translate it).</div>`
      : `<div class="ed-empty">pick a video in Media first</div>`);
  };
  C["avatar.translate"].bind = root => {
    const b = $("#vt-go", root);
    if (!b) return;
    b.onclick = async () => {
      const v = ED.video;
      try {
        await VS.post("/api/run", {action: "dub", file: v.name, engine: "local",
          language: $("#vt-lang", root).value,
          lipsync: $("#vt-lips", root).checked ? "wav2lip-hd" : "none"});
        VS.toast("🌍 Translation dub started"); renderTimeline();
      } catch (e) { VS.toast("Couldn't start: " + e.message); }
    };
  };
  C["avatar.dialog"] = () => panel("AI Dialog", "AI-driven two-person dialog scenes",
    soonTiles([["🗨️", "Dialog builder"]]) +
    `<div class="ed-note">real two-speaker dubbing is already live: <a href="/?step=dub">Interview mode ↗</a>.</div>`);

  function renderContent() {
    const key = ED.tab + "." + ED.sec;
    const fn = C[key];
    const box = $("#ed-assets");
    box.innerHTML = fn ? fn() : `<div class="ed-empty">…</div>`;
    if (fn && fn.bind) { try { fn.bind(box); } catch (e) { console.error(key, e); } }
  }

  /* ═════════════ selection / player ═════════════ */
  function select(v, chip) {
    ED.video = v;
    ED.take = v.dub ? "final" : "source";
    ED.filmKey = null; ED.cutIn = ED.cutOut = null;
    $("#ed-title").textContent = v.title || v.name;
    $("#ed-pname").textContent = "— " + (v.title || v.name);
    $("#ed-deliverbtn").disabled = !v.dub;
    ED.chip = chip || "details";
    renderContent(); renderPreview(); renderTimeline(); renderChips(); renderInspector();
    syncURL();
  }

  function previewSrc() {
    const v = ED.video;
    if (!v) return null;
    if (ED.take === "final" && v.dub) return "/media/" + v.dub + "?v=" + (v.dub_mtime || "");
    return "/media/uploads/" + encodeURIComponent(v.name);
  }
  const fmtT = s => !isFinite(s) ? "00:00.0"
    : String(Math.floor(s / 60)).padStart(2, "0") + ":" + (s % 60).toFixed(1).padStart(4, "0");
  const playerVideo = () => $("#ed-frame video");

  function renderPreview() {
    const f = $("#ed-frame");
    f.className = "ed-frame" + (ED.ratio === "16:9" ? " wide" : ED.ratio === "1:1" ? " square" : "");
    const src = previewSrc();
    if (!src) { f.innerHTML = '<div class="ph">← pick a video from Media</div>'; return; }
    let vid = playerVideo();
    if (!vid) {
      f.innerHTML = "";
      vid = document.createElement("video");
      vid.playsInline = true; vid.preload = "metadata";
      f.appendChild(vid);
      vid.addEventListener("timeupdate", () => {
        $("#ed-tc-cur").textContent = fmtT(vid.currentTime);
        movePlayhead(vid.currentTime);
      });
      vid.addEventListener("loadedmetadata", () => {
        $("#ed-tc-tot").textContent = fmtT(vid.duration);
        ED.dur = vid.duration || 0;
        renderTimeline();
      });
      const setBtn = () => { const on = !vid.paused;
        $("#ed-play").textContent = on ? "⏸" : "▶"; $("#tl-play").textContent = on ? "⏸" : "▶"; };
      vid.addEventListener("play", setBtn); vid.addEventListener("pause", setBtn);
    }
    vid.style.objectFit = ED.fit;
    if (!vid.src.endsWith(encodeURI(src))) vid.src = src;
    $("#ed-take").textContent = ED.take === "final" ? "Final" : "Source";
    $("#ed-take").style.display = ED.video && ED.video.dub ? "" : "none";
    $("#ed-ratio").textContent = ED.ratio;
  }

  /* ═════════════ timeline ═════════════ */
  const tlWidth = () => Math.max(400, ED.dur * ED.pxs);
  function movePlayhead(t) { $("#tl-playhead").style.left = (HEADW + (t || 0) * ED.pxs) + "px"; }

  function renderTimeline() {
    const v = ED.video;
    const ruler = $("#tl-ruler"), lanes = $("#tl-lanes"), inner = $("#tl-inner");
    if (!v) {
      ruler.innerHTML = "";
      lanes.innerHTML = '<div class="ed-empty" style="padding:26px">the editing timeline appears when you pick a video</div>';
      return;
    }
    const W = tlWidth();
    inner.style.width = (HEADW + W + 40) + "px";
    const step = ED.pxs >= 180 ? 1 : ED.pxs >= 90 ? 2 : 5;
    let ticks = "";
    for (let t = 0; t <= Math.max(1, ED.dur); t += step)
      ticks += `<div class="tick" style="left:${HEADW + t * ED.pxs}px">${t}s</div>`;
    // cut range highlight
    if (ED.cutIn != null) {
      const a = HEADW + ED.cutIn * ED.pxs;
      const b = ED.cutOut != null ? HEADW + ED.cutOut * ED.pxs : a + 2;
      ticks += `<div class="ed-cutrange" style="left:${a}px;width:${Math.max(2, b - a)}px"></div>`;
    }
    ruler.innerHTML = ticks;

    const jobs = VS.jobsFor(v).filter(j => j.status === "running");
    const jb = a => jobs.find(j => a.includes(j.action));
    const pct = j => (j && j.progress && j.progress.pct != null) ? j.progress.pct + "%" : "running…";
    const bar = (cls, text, st) =>
      `<div class="ed-clipbar ${cls}" style="left:${HEADW}px;width:${W}px">${text}${st ? `<span class="st">${st}</span>` : ""}</div>`;
    const lane = (icons, name, body) =>
      `<div class="ed-lane"><div class="head">${icons.map(i => `<span class="ic">${i}</span>`).join("")}
        <span class="nm">${name}</span></div><div class="body">${body}</div></div>`;

    const running = jobs[0];
    const fxBody = running ? bar("effect", "⚙ " + VS.esc(running.label || running.action), pct(running))
      : bar("ghost", "no job running");
    const filmBody = `<div class="ed-film" id="tl-film" style="left:${HEADW}px;width:${W}px">
        <div class="cliplabel">${VS.esc(v.name)} · ${ED.dur ? ED.dur.toFixed(1) + "s" : ""} ${v.cleaned ? "· subtitles erased ✓" : ""}</div></div>`;
    const dubJob = jb(["dub", "duo", "diarize"]);
    const voiceBody = dubJob ? bar("running", "🎙 " + VS.esc(dubJob.action), pct(dubJob))
      : v.dub ? bar("voice", "🎙 dubbed voice", "✓ final.mp4")
      : bar("ghost", v.script ? "ready — Dub (right panel)" : "needs a script first");
    const capJob = jb(["caption", "recaption"]);
    const capsBody = capJob ? bar("running", "💬 burning captions", pct(capJob))
      : v.captioned ? bar("caps", "💬 captions burned", "✓")
      : bar("ghost", v.dub ? "ready — Captions tab" : "waiting for a dub");
    const nBroll = (ED.i2v || []).filter(c => c.ready).length;
    const brollBody = nBroll ? bar("effect", `🎞 ${nBroll} AI b-roll clip${nBroll > 1 ? "s" : ""}`, "ready")
      : bar("ghost", "no AI b-roll — make clips in Media ▸ AI Video");
    const delBody = v.exported ? bar("voice", "📤 delivered to Desktop", "✓")
      : v.dub ? bar("ghost", "ready — Deliver in the right panel")
      : bar("ghost", "finish the dub first");

    lanes.innerHTML =
      lane(["✦", "👁"], "FX/JOB", fxBody) +
      lane(["🎬", "🔒", "👁"], "VIDEO", filmBody) +
      lane(["🎞", "👁"], "B-ROLL", brollBody) +
      lane(["🔊", "👁"], "VOICE", voiceBody) +
      lane(["💬", "👁"], "CAPTIONS", capsBody) +
      lane(["📤"], "DELIVER", delBody);
    $("#tl-jobnote").textContent = running ? `⏳ ${running.label || running.action}` : "";
    loadFilmstrip();

    const seek = e => {
      const rect = inner.getBoundingClientRect();
      const t = Math.max(0, Math.min(ED.dur, (e.clientX - rect.left - HEADW) / ED.pxs));
      const vid = playerVideo();
      if (vid && isFinite(t)) { vid.currentTime = t; movePlayhead(t); }
    };
    ruler.onclick = seek;
    $$("#tl-lanes .body").forEach(b => b.onclick = seek);
    updateCutBtn();
  }

  async function loadFilmstrip() {
    const v = ED.video;
    const holder = $("#tl-film");
    if (!v || !holder) return;
    const rel = (ED.take === "final" && v.dub) ? v.dub : "uploads/" + v.name;
    const key = rel + "|" + (v.dub_mtime || 0);
    if (ED.filmKey === key && holder.querySelector("img")) return;
    ED.filmKey = key;
    try {
      const d = await VS.api(`/api/qc/frames?path=${encodeURIComponent(rel)}&count=12`);
      if (ED.filmKey !== key) return;
      holder.insertAdjacentHTML("beforeend",
        (d.frames || []).map(f => `<img src="/media/${f.path.replace(/^\/+/, "")}" alt="">`).join(""));
    } catch (e) { /* label-only strip */ }
  }

  /* cut tool */
  function updateCutBtn() {
    const ok = ED.video && ED.cutIn != null && ED.cutOut != null && ED.cutOut > ED.cutIn + 0.05;
    $("#tl-cut").disabled = !ok;
    $("#tl-label").textContent = ED.cutIn != null
      ? `cut: ${fmtT(ED.cutIn)} → ${ED.cutOut != null ? fmtT(ED.cutOut) : "…"}` : "";
  }

  /* ═════════════ RIGHT: chips + panels (the pipeline) ═════════════ */
  const CHIPS = [["details", "Details"], ["clean", "Clean"], ["script", "Script"],
                 ["dub", "Dub"], ["fix", "Fix"], ["deliver", "Deliver"]];
  function renderChips() {
    $("#ed-chips").innerHTML = CHIPS.map(([k, lab]) =>
      `<button class="ed-chip ${ED.chip === k ? "on" : ""}" data-c="${k}">${lab}</button>`).join("");
    $$(".ed-chip").forEach(b => b.onclick = () => { ED.chip = b.dataset.c; renderChips(); renderInspector(); });
  }

  const P = {};
  const need = msg => `<div class="ed-note">${msg || "Pick a video in Media first."}</div>`;
  const rows = pairs => pairs.map(([k, v]) =>
    `<div class="ed-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("");

  P.details = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Details";
    if (!v) return need();
    return rows([
      ["Name:", `<input type="text" id="mi-title" value="${VS.esc(v.title || "")}" placeholder="${VS.esc(v.name)}">`],
      ["File:", VS.esc(v.name)],
      ["Size:", VS.fmtSize(v.size)],
      ["Character:", `<input type="text" id="mi-char" value="${VS.esc(v.character || "")}" placeholder="who's in it">`],
      ["Tags:", `<input type="text" id="mi-tags" value="${VS.esc((v.tags || []).join(", "))}" placeholder="comma, separated">`],
    ]) + `<div class="ed-hr"></div>` + rows([
      ["Transcribed:", v.transcript ? "✓ yes" : "not yet"],
      ["Clean:", v.cleaned ? "✓ erased" : (v.no_subs ? "✓ no burned subs" : "not cleaned")],
      ["Dub:", v.dub ? "✓ final.mp4" : "—"],
      ["Captions:", v.captioned ? "✓ burned" : "—"],
      ["Delivered:", v.exported ? "✓ on Desktop" : "—"],
    ]) + `<button class="ed-run ghostly" id="mi-save" style="margin-top:12px">💾 Save details</button>`;
  };
  P.details.bind = el => {
    const v = ED.video; if (!v) return;
    $("#mi-save", el).onclick = async () => {
      try {
        await VS.post("/api/creator/meta", {name: v.name,
          title: $("#mi-title", el).value.trim(), character: $("#mi-char", el).value.trim(),
          tags: $("#mi-tags", el).value.split(",").map(t => t.trim()).filter(Boolean)});
        VS.toast("Saved"); VS.refreshLibrary();
      } catch (e) { VS.toast("Error: " + e.message); }
    };
  };

  P.clean = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Clean";
    if (!v) return need();
    const run = VS.runningFor(v, ["clean-subs"]);
    return `${run ? `<div class="ed-msg">⏳ erasing… watch the timeline</div>` : ""}
      <button class="ed-run" id="cl-erase" ${run ? "disabled" : ""}>🧹 ${v.cleaned ? "Re-erase subtitles" : "Erase burned-in subtitles"}</button>
      ${v.cleaned ? `<button class="ed-run ghostly" id="cl-restore">↩ Restore original</button>` : ""}
      <div class="ed-field"><label style="display:flex;gap:6px;align-items:center">
        <input type="checkbox" id="cl-nosubs" ${v.no_subs ? "checked" : ""}> no burned subtitles</label></div>
      <div class="ed-hr"></div>
      <button class="ed-run ghostly" id="cl-transcribe">${v.transcript ? "↻ Re-transcribe" : "▶ Transcribe"}</button>`;
  };
  P.clean.bind = el => {
    const v = ED.video; if (!v) return;
    $("#cl-erase", el).onclick = async () => {
      try { await VS.post("/api/clean-subs", {file: v.name, auto: true, mode: "erase"});
        VS.toast("🧹 Erasing — the long one"); renderTimeline(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
    const r = $("#cl-restore", el);
    if (r) r.onclick = async () => {
      if (!confirm("Put back the ORIGINAL (with the old subtitles)?")) return;
      try { await VS.post("/api/clean-restore", {file: v.name}); VS.toast("Original restored"); VS.refreshLibrary(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
    $("#cl-nosubs", el).onchange = async e => {
      try { await VS.post("/api/creator/meta", {name: v.name, no_subs: e.target.checked}); VS.refreshLibrary(); }
      catch (err) { VS.toast("Error: " + err.message); }
    };
    $("#cl-transcribe", el).onclick = async () => {
      try { await VS.post("/api/run", {action: "transcribe", file: v.name}); VS.toast("Transcribing…"); renderTimeline(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
  };

  P.script = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Script";
    if (!v) return need();
    return `<div class="ed-note" style="margin-bottom:8px">${v.orig_words ? `Original spoke ~${v.orig_words} words — stay close so the lips fit.` : "The words they'll say."}</div>
      <div class="ed-field"><textarea id="sc-text" rows="10" placeholder="loading…"></textarea></div>
      <div class="ed-field"><input type="text" id="sc-steer" placeholder="✨ AI rewrite — e.g. translate to Spanish / punchier hook"></div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin:2px 0 9px;font-size:11px;color:var(--dim)">
        <label style="display:flex;gap:5px;align-items:center;cursor:pointer"
          title="ground the rewrite in the Fairy Flame offer doc + proven hooks">
          <input type="checkbox" id="sc-brand" style="accent-color:#f0b25c">🔥 liitt brand</label>
        <label style="display:flex;gap:5px;align-items:center;cursor:pointer"
          title="open like breaking news — studies, veterans, laws loosening — in platform-safe wording">
          <input type="checkbox" id="sc-news" style="accent-color:#f0b25c">📰 News hook</label>
        <label style="display:flex;gap:5px;align-items:center;cursor:pointer"
          title="dodge Meta's restricted words (psychedelic, psilocybin, magic mushroom, microdosing) with creative compliant wording">
          <input type="checkbox" id="sc-safe" style="accent-color:#f0b25c" checked>🛡 Meta-safe</label>
      </div>
      <button class="ed-run ghostly" id="sc-liitt" title="turn ANY script — any brand, any product — into a liitt Fairy Flame script: detects what it sells, swaps the whole product world to microdose gummies, keeps the winning hook & beats, stays compliant">🔥 liitt</button>
      <button class="ed-run ghostly" id="sc-ai">✨ Rewrite with AI</button>
      <button class="ed-run ghostly" id="sc-vanilla" title="Safety pass, not a rewrite: keeps your script exactly as it is and only swaps the words that get ads flagged (psychedelic, microdose, high, cure, guaranteed, spoken URLs) for the nearest safe word — same lines, same rhythm, same length, so the lip-sync still fits">🍦 Vanilla safe</button>
      <button class="ed-run ghostly" id="sc-goldies" title="Goldies is a cannabis brand — this rewrites its script as Fairy Flame: brand swap + every weed / flower / smoking / high reference becomes the microdose-gummies equivalent, tone lifted from stoner to premium">🔄 Goldies → Fairy Flame</button>
      <button class="ed-run" id="sc-save">💾 Save script</button>
      <div class="ed-cost free" id="sc-count"></div>
      <div class="ed-hr"></div>
      <div id="sc-fit"></div>`;
  };
  P.script.bind = async el => {
    const v = ED.video; if (!v) return;
    const ta = $("#sc-text", el);
    const count = () => { $("#sc-count", el).textContent =
      ta.value.trim().split(/\s+/).filter(Boolean).length + " words"; };
    ta.oninput = count;
    try { const s = await VS.api("/api/script/" + encodeURIComponent(v.stem));
      ta.value = s.text || ""; ta.placeholder = "write the script…"; count(); } catch (e) { /* */ }
    $("#sc-save", el).onclick = async () => {
      const text = ta.value.trim();
      if (!text) { VS.toast("Write the script first"); return; }
      try { await VS.post("/api/script/" + encodeURIComponent(v.stem), {text});
        VS.toast("Script saved"); VS.refreshLibrary(); renderTimeline(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
    const brand = $("#sc-brand", el), aiBtn = $("#sc-ai", el),
          gBtn = $("#sc-goldies", el), lBtn = $("#sc-liitt", el), vBtn = $("#sc-vanilla", el);
    const btns = [aiBtn, gBtn, lBtn, vBtn];
    const syncAi = () => { aiBtn.textContent = brand && brand.checked ? "🔥 Build Fairy Flame script" : "✨ Rewrite with AI"; };
    if (brand) brand.onchange = syncAi;
    syncAi();
    // vanilla:true = a pure safety pass — brand grounding and the News hook add-on are
    // skipped (both pull the script toward NEW copy, which is what this button promises
    // not to do). The steer box still applies: explicit user intent wins.
    const doRewrite = async (btn, busy, extra, forceBrand, vanilla) => {
      if (!ta.value.trim()) { VS.toast("Load or write a script first"); return; }
      const on = !vanilla && (forceBrand || (brand && brand.checked));
      const idle = btn.textContent;
      btns.forEach(b => { if (b) b.disabled = true; });
      btn.textContent = busy;
      const steer = $("#sc-steer", el).value.trim();
      const news = $("#sc-news", el), safe = $("#sc-safe", el);
      const body = {text: ta.value.trim(), instruction: [extra,
        !vanilla && news && news.checked ? VS.NEWS_HOOK_PROMPT : "",
        !vanilla && safe && safe.checked ? VS.META_SAFE_PROMPT : "",
        steer].filter(Boolean).join("\n\n")};
      if (on) { body.brand = true; body.slug = "fairy-flame"; }   // grounded in products/fairy-flame/offer.md + hooks/angles banks
      try { const r = await VS.post("/api/copywrite", body);
        ta.value = r.text || ta.value; count();
        VS.toast(vanilla ? "🍦 Vanilla — same script, safer words. Check it, then Save"
          : on ? "🔥 Fairy Flame version ready — review then Save" : "Rewritten — review then Save"); }
      catch (e) { VS.toast("Rewrite failed: " + e.message); }
      btns.forEach(b => { if (b) b.disabled = false; });
      btn.textContent = idle; syncAi();
    };
    vBtn.onclick = () => doRewrite(vBtn, "🍦 swapping the risky words… (~25s)",
      VS.VANILLA_PROMPT, false, true);
    // the universal one-press: ANY script, ANY brand/category → a liitt Fairy Flame
    // script. Prompt lives in vs-core.js (VS.LIITT_PROMPT) — shared with the Creator.
    lBtn.onclick = () => doRewrite(lBtn, "🔥 making it liitt… (~45s)", VS.LIITT_PROMPT, true);
    aiBtn.onclick = () => doRewrite(aiBtn,
      brand && brand.checked ? "🔥 building… (~45s)" : "✨ rewriting… (~30s)", "", false);
    // Goldies is a CANNABIS brand — a plain brand-swap would leave weed context all
    // over the script. This converts the whole world of the script, not just the name.
    const GOLDIES_CONVERT = `CONVERT this script from "Goldies" (a marijuana/cannabis brand — the name may appear as Goldies, Goldie's or Blow Goldies) into a script for liitt's Fairy Flame microdose gummies. Blend it seamlessly — the result must read as if it was always written for Fairy Flame:
- Replace every Goldies brand mention with Fairy Flame (the company behind it is liitt).
- Convert ALL cannabis context to the gummies equivalent: flower / bud / strain / eighth / gram / pre-roll / blunt / joint → a pouch of Fairy Flame gummies (one gummy per serving); smoking / rolling / lighting up / hitting / vaping → taking a gummy; dispensary / plug → "the link down below" (NEVER speak a URL or domain); weed / marijuana / cannabis / THC → Fairy Flame gummies (keep the actives general — never name them).
- getting high / stoned / baked / faded / blazed → the Fairy Flame state-shift: lighter mood, clarity, focus, feeling like yourself again. NEVER promise a high, buzz or intoxication — this is sub-perceptual.
- Not one cannabis word may survive anywhere in the result.
- Keep the same hook structure, beats, energy and length (stay within ±10% of the original word count — the lip-sync depends on it).
- Lift the tone from stoner culture to premium and clean, but keep it punchy and native to short-form.`;
    gBtn.onclick = () => doRewrite(gBtn, "🔄 converting… (~45s)", GOLDIES_CONVERT, true);
    mountFit($("#sc-fit", el), v);
  };

  /* ── Fit video to script (AI-extend) — measure the script's real spoken length,
     then grow the video to it with fal.ai and prove the final length matches ── */
  async function mountFit(box, v) {
    if (!box) return;
    box.innerHTML = `<h4 style="font-size:12.5px;margin-bottom:2px">📏 Fit video to script</h4>
      <div class="ph4" style="color:var(--dim);font-size:11px;margin-bottom:8px">extend the video with AI so its length matches the script — proven to the frame</div>
      <div id="fit-body"><div class="ed-note">checking…</div></div>`;
    const body = $("#fit-body", box);
    const alive = () => document.body.contains(box) && ED.video && ED.video.name === v.name;

    const wireAnalyze = () => { const b = $("#fit-an", box); if (b) b.onclick = startAnalyze; };
    const draw = d => {
      const plan = d.plan || {}, fit = d.fit || {};
      if (fit && fit.final_sec) {
        body.innerHTML = `<div class="ed-cost free">✅ fitted → final <b>${fit.final_sec}s</b> vs script <b>${fit.target_sec}s</b> (matches to ${Math.abs(fit.final_sec - fit.target_sec).toFixed(2)}s).</div>
          ${fit.fitted ? `<div class="ed-note">added to your library as <b>${VS.esc(fit.fitted.split("/").pop())}</b> — pick it in Media to dub / lip-sync.</div>` : ""}
          <button class="ed-run ghostly" id="fit-an" style="margin-top:8px">↻ Re-analyze</button>`;
        wireAnalyze(); return;
      }
      if (!plan.source_sec) {
        body.innerHTML = `<button class="ed-run ghostly" id="fit-an">📏 Analyze fit</button>
          <div class="ed-note">measures the script's real spoken length with the voice engine (free, ~1 min).</div>`;
        wireAnalyze(); return;
      }
      if (!plan.needs_extend) {
        body.innerHTML = `<div class="ed-cost free">✓ already long enough — source <b>${plan.source_sec}s</b> ≥ script <b>${plan.target_sec}s</b>. No extension needed.</div>
          <button class="ed-run ghostly" id="fit-an" style="margin-top:8px">↻ Re-analyze</button>`;
        wireAnalyze(); return;
      }
      body.innerHTML = `<div class="ed-note" style="margin-bottom:8px">source <b>${plan.source_sec}s</b> → script needs <b>${plan.target_sec}s</b> → <b>${plan.gap}s</b> to generate.</div>
        <div class="ed-field"><label>Generator (fal.ai)</label><select id="fit-model">
          <option value="kling-2.1">Kling 2.1 — balanced</option>
          <option value="hailuo-02">Hailuo 02 — great motion</option>
          <option value="kling-2.1-pro">Kling 2.1 Pro — best quality</option>
          <option value="wan-2.2">Wan 2.2 — budget</option></select></div>
        <div class="ed-field"><label>Aspect</label><select id="fit-aspect">
          <option>9:16</option><option>16:9</option><option>1:1</option></select></div>
        <div class="ed-field"><label>Motion prompt (optional)</label>
          <textarea id="fit-prompt" rows="2" placeholder="the same person keeps talking to camera, subtle natural movement, same setting & lighting"></textarea></div>
        <button class="ed-run" id="fit-go">⚡ Extend &amp; fit</button>
        <div class="ed-cost paid">💰 fal.ai — you approve the exact cost first</div>
        <button class="ed-run ghostly" id="fit-an" style="margin-top:6px">↻ Re-analyze</button>`;
      wireAnalyze();
      $("#fit-go", box).onclick = () => runFit();
    };

    async function startAnalyze() {
      body.innerHTML = `<div class="ed-msg">📏 measuring the script (XTTS)… watch the timeline — this can take a minute.</div>`;
      try { await VS.post("/api/fit/analyze", {file: v.name}); renderTimeline(); pollPlan(0); }
      catch (e) {
        body.innerHTML = `<div class="ed-msg">${VS.esc(e.message)}</div><button class="ed-run ghostly" id="fit-an">↻ Try again</button>`;
        wireAnalyze();
      }
    }
    async function pollPlan(n) {
      if (!alive()) return;
      try {
        const d = await VS.api("/api/fit/plan/" + encodeURIComponent(v.stem));
        if (d.plan && d.plan.source_sec) { draw(d); return; }
      } catch (e) { /* keep waiting */ }
      if (n < 80) setTimeout(() => pollPlan(n + 1), 2500);
    }
    async function runFit() {
      const model = $("#fit-model", box).value, aspect = $("#fit-aspect", box).value;
      const prompt = $("#fit-prompt", box).value.trim();
      const btn = $("#fit-go", box); btn.disabled = true;
      const payload = m => JSON.stringify({file: v.name, model, aspect, prompt, ...(m || {})});
      const hdr = {"Content-Type": "application/json"};
      try {
        let r = await fetch("/api/fit/run", {method: "POST", headers: hdr, body: payload()});
        if (r.status === 402) {
          const d = await r.json();
          if (!confirm(`⚠ This generates footage on fal.ai (spends money).\n\n${d.estimate.summary}\n\nApprove and start?`)) { btn.disabled = false; return; }
          r = await fetch("/api/fit/run", {method: "POST", headers: hdr, body: payload({confirm_cost: true})});
        }
        if (!r.ok) throw new Error((await r.text()).slice(0, 200));
        VS.toast("⚡ Extending — generating footage on fal.ai"); renderTimeline();
        body.innerHTML = `<div class="ed-msg">⚡ generating &amp; fitting… watch the timeline. The fitted clip lands in Media when it's done.</div>`;
      } catch (e) { VS.toast("Couldn't start: " + e.message); btn.disabled = false; }
    }

    try { draw(await VS.api("/api/fit/plan/" + encodeURIComponent(v.stem))); }
    catch (e) {
      body.innerHTML = `<button class="ed-run ghostly" id="fit-an">📏 Analyze fit</button>`;
      wireAnalyze();
    }
  }

  P.dub = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Dub & Lip-sync";
    if (!v) return need();
    const run = VS.runningFor(v, ["dub", "duo"]);
    const voiceOpts = ['<option value="">🎤 On-screen speaker</option>']
      .concat(ED.voices.map(vo => `<option value="${VS.esc(vo.id)}">🎙 ${VS.esc(vo.name)}</option>`)).join("");
    return `
      <div class="ed-itabs"><button class="ed-itab on" data-it="basic">Basic</button>
        <button class="ed-itab" data-it="adv">Advanced</button></div>
      <div data-pane="basic">
        <div class="ed-opt on" data-eng="local"><div class="t">💻 Local — FREE<span class="price" style="color:var(--ok)">$0</span></div>
          <div class="d">XTTS + Wav2Lip HD on your GPU.</div></div>
        <div class="ed-opt" data-eng="fal"><div class="t">☁ Premium cloud<span class="price" style="color:var(--warn)">~$1.50+$3/m</span></div>
          <div class="d">MiniMax HD + sync.so pro — the winner pipeline.</div></div>
        <div class="ed-field"><label>Voice</label><select id="du-voice">${voiceOpts}</select></div>
        <div class="ed-field" id="du-localrow"><label>Lip-sync</label><select id="du-lip">
          <option value="wav2lip-hd" selected>Wav2Lip HD (free)</option>
          <option value="wav2lip">Wav2Lip (faster)</option>
          <option value="none">voice only (silent clips)</option></select></div>
        <div class="ed-field" id="du-falrow" style="display:none"><label>Cloud voice · tier</label>
          <select id="du-tts"><option value="hd" selected>MiniMax HD</option><option value="turbo">turbo</option><option value="f5">F5 (no clone fee)</option></select>
          <select id="du-tier" style="margin-top:6px"><option value="standard" selected>sync v2 standard</option><option value="pro">sync v2 pro</option><option value="veed">veed</option><option value="latentsync">latentsync</option></select></div>
      </div>
      <div data-pane="adv" style="display:none">
        <div class="ed-field"><label>Language</label><select id="du-lang">
          <option value="en" selected>English</option><option value="es">Spanish</option>
          <option value="de">German</option><option value="fr">French</option>
          <option value="pt">Portuguese</option><option value="it">Italian</option></select></div>
        <div class="ed-param"><span class="pl">Keep original audio (music bleed) %</span>
          <input type="range" id="du-keep" min="0" max="60" value="0">
          <input class="num" id="du-keepn" value="0"></div>
        <div class="ed-note">Two speakers? <a href="/?v=${encodeURIComponent(v.name)}&step=dub">Interview mode ↗</a></div>
      </div>
      ${run ? `<div class="ed-msg">⏳ ${VS.esc(run.label || "dub running")} — one at a time</div>` : ""}
      <button class="ed-run" id="du-run" ${run || !v.script ? "disabled" : ""}>🎙 Dub</button>
      <div class="ed-cost free" id="du-cost">✓ 100% local — nothing is charged</div>
      ${!v.script ? '<div class="ed-msg">save a Script first</div>' : ""}`;
  };
  P.dub.bind = el => {
    const v = ED.video; if (!v) return;
    let engine = "local";
    $$(".ed-itab", el).forEach(t => t.onclick = () => {
      $$(".ed-itab", el).forEach(x => x.classList.toggle("on", x === t));
      $$("[data-pane]", el).forEach(p => p.style.display = p.dataset.pane === t.dataset.it ? "" : "none");
    });
    $$(".ed-opt[data-eng]", el).forEach(o => o.onclick = () => {
      engine = o.dataset.eng;
      $$(".ed-opt[data-eng]", el).forEach(x => x.classList.toggle("on", x === o));
      $("#du-localrow", el).style.display = engine === "local" ? "" : "none";
      $("#du-falrow", el).style.display = engine === "fal" ? "" : "none";
      const c = $("#du-cost", el);
      if (engine === "fal") { c.textContent = "💰 charged on fal.ai — you approve first"; c.className = "ed-cost paid"; }
      else { c.textContent = "✓ 100% local — nothing is charged"; c.className = "ed-cost free"; }
    });
    const kr = $("#du-keep", el), kn = $("#du-keepn", el);
    kr.oninput = () => kn.value = kr.value;
    kn.onchange = () => kr.value = Math.max(0, Math.min(60, parseInt(kn.value) || 0));
    $("#du-run", el).onclick = async () => {
      const body = {action: "dub", file: v.name, engine};
      if (engine === "local") {
        body.lipsync = $("#du-lip", el).value;
        body.language = $("#du-lang", el).value;
        const keep = (parseInt(kr.value) || 0) / 100;
        if (keep > 0) body.keep_volume = keep;
        const vid = $("#du-voice", el).value;
        if (vid) body.voice_id = vid;
      } else {
        body.tts = $("#du-tts", el).value;
        body.tier = $("#du-tier", el).value;
        if (!confirm("⚠ This spends money on fal.ai (voice + lip-sync).\n\nApprove and start?")) return;
        body.confirm_cost = true;
      }
      try { await VS.post("/api/run", body);
        VS.toast(engine === "local" ? "🎙 Free local dub started" : "🎙 Cloud dub started");
        renderTimeline(); renderInspector(); }
      catch (e) { VS.toast("Couldn't start: " + e.message); }
    };
  };

  P.fix = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Fix & QA";
    if (!v) return need();
    if (!v.dub) return need("Run a Dub first — repairs work on the dubbed video.");
    return `<div class="ed-note" style="margin-bottom:8px">Describe what's wrong — the AI watches and picks the repair.</div>
      <div class="ed-field"><input type="text" id="fx-txt" placeholder="e.g. the cup warps when he drinks"></div>
      <button class="ed-run" id="fx-advise">🪄 Analyze</button>
      <div id="fx-out"></div>
      <div class="ed-hr"></div>
      <button class="ed-run ghostly" data-fix="relipsync">👄 Re-lip-sync</button>
      <button class="ed-run ghostly" data-fix="renorm">🔊 Fix loudness</button>
      <button class="ed-run ghostly" data-fix="remux">📦 Remux voice</button>
      <div class="ed-note">deep tools: <a href="/dubsync-lab" target="_blank">DubSync lab ↗</a> · <a href="/qc-lab" target="_blank">QC lab ↗</a></div>`;
  };
  P.fix.bind = el => {
    const v = ED.video; if (!v || !v.dub) return;
    $$("[data-fix]", el).forEach(b => b.onclick = async () => {
      const a = b.dataset.fix;
      try { await VS.post("/api/dubsync/repair", a === "relipsync" ? {stem: v.stem, action: a, restorer: "gfpgan"} : {stem: v.stem, action: a});
        VS.toast("🔧 Repair running"); renderTimeline(); }
      catch (e) { VS.toast("Error: " + e.message); }
    });
    $("#fx-advise", el).onclick = async () => {
      const text = $("#fx-txt", el).value.trim();
      if (!text) { VS.toast("Describe the problem first"); return; }
      const btn = $("#fx-advise", el), out = $("#fx-out", el);
      btn.disabled = true;
      out.innerHTML = '<div class="ed-note">🪄 watching the video… (~1 min)</div>';
      try {
        const a = await VS.post("/api/dubsync/advise", {stem: v.stem, text});
        out.innerHTML = `<div class="ed-opt" style="cursor:default;margin-top:8px"><div class="t">${VS.esc(a.action || "?")}</div>
          <div class="d">${VS.esc(a.explanation || "")}</div>
          <button class="ed-run" id="fx-doit" style="margin-top:8px">▶ Run this fix</button></div>`;
        $("#fx-doit", out).onclick = async () => {
          const body = {stem: v.stem, action: a.action};
          if (a.samples) body.samples = a.samples;
          if (a.box) body.box = a.box;
          if (a.track != null) body.track = a.track;
          if (a.action === "relipsync") body.restorer = "gfpgan";
          try { await VS.post("/api/dubsync/repair", body); VS.toast("🔧 Fix running"); renderTimeline(); }
          catch (e) { VS.toast("Error: " + e.message); }
        };
      } catch (e) { out.innerHTML = `<div class="ed-msg">analysis failed: ${VS.esc(e.message)}</div>`; }
      btn.disabled = false;
    };
  };

  P.deliver = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Deliver";
    if (!v) return need();
    return `<div class="ed-opt ${v.captioned ? "on" : ""}" data-del="captioned" style="${v.captioned ? "" : "opacity:.4;pointer-events:none"}">
        <div class="t">💬 Captioned final</div><div class="d">recommended for ads</div></div>
      <div class="ed-opt ${v.captioned ? "" : "on"}" data-del="final" style="${v.dub ? "" : "opacity:.4;pointer-events:none"}">
        <div class="t">🎬 Final (no captions)</div></div>
      <div class="ed-field"><label style="display:flex;gap:6px;align-items:center">
        <input type="checkbox" id="dl-clean" checked> auto-clean workspace after export (24h, reversible)</label></div>
      <button class="ed-run" id="dl-send" ${v.dub ? "" : "disabled"}>📤 Send to Desktop</button>
      <div class="ed-note" id="dl-note"></div>
      <div class="ed-hr"></div>
      <div class="ed-row" style="margin-bottom:4px"><span class="k">Masters</span>
        <span class="v" style="display:flex;gap:9px;flex-wrap:wrap">
          <label style="display:flex;gap:4px;align-items:center;font-size:11.5px"><input type="checkbox" class="dl-ar" value="9:16" checked>9:16</label>
          <label style="display:flex;gap:4px;align-items:center;font-size:11.5px"><input type="checkbox" class="dl-ar" value="1:1">1:1</label>
          <label style="display:flex;gap:4px;align-items:center;font-size:11.5px"><input type="checkbox" class="dl-ar" value="16:9">16:9</label>
        </span></div>
      <button class="ed-run ghostly" id="dl-aspects" ${v.dub ? "" : "disabled"}>🎬 Export aspect masters</button>
      <div class="ed-note" id="dl-anote"></div>
      <div class="ed-hr"></div>
      <div class="ed-note">🏆 winner? <a href="/?v=${encodeURIComponent(v.name)}&step=deliver">Clone it with a fresh script ↗</a></div>
      <div class="ed-hr"></div>
      <div class="ed-row"><span class="k">Pipeline</span><span class="v" style="font-size:10.5px;line-height:1.7;color:var(--dim)">
        ${[["Upload source", true], ["Shot inspect/split", null], ["Extract audio", true],
           ["Whisper + word timestamps", true], ["Transcript correction", true],
           ["Subtitle removal + restore", true], ["Rewrite / translate script", true],
           ["Dubbed audio", !!v.dub], ["Audio-duration match", true],
           ["Lip-sync", !!v.dub], ["DubSync repair + QA", true],
           ["AI B-roll / product visuals", true], ["Branded captions", !!v.captioned],
           ["CTA cards · music · transitions", null], ["Upscale", null],
           ["Multi-aspect export", true]].map(([n, s]) =>
             `${s === null ? "◌" : s ? "✓" : "○"} ${n}${s === null ? ' <span class="soon" style="position:static">SOON</span>' : ""}`
           ).join("<br>")}</span></div>`;
  };
  P.deliver.bind = el => {
    const v = ED.video; if (!v) return;
    let what = v.captioned ? "captioned" : "final";
    $$(".ed-opt[data-del]", el).forEach(o => o.onclick = () => {
      what = o.dataset.del;
      $$(".ed-opt[data-del]", el).forEach(x => x.classList.toggle("on", x === o));
    });
    $("#dl-send", el).onclick = async () => {
      const auto = $("#dl-clean", el).checked;
      const note = $("#dl-note", el);
      note.textContent = "sending…";
      try {
        let r;
        if (what === "captioned") {
          try { r = await VS.post("/api/exports/send", {path: `output/script-swap/${v.stem}/final-captioned.mp4`, stem: v.stem, auto_cleanup: auto}); }
          catch (e) { r = await VS.post("/api/exports/send", {kind: "captioned", stem: v.stem, auto_cleanup: auto}); }
        } else {
          r = await VS.post("/api/exports/send", {path: `output/script-swap/${v.stem}/final.mp4`, stem: v.stem, auto_cleanup: auto});
        }
        note.textContent = "✅ " + r.saved_to;
        VS.toast("📤 Delivered" + (auto ? " — workspace auto-cleans later" : ""));
        VS.refreshLibrary(); renderTimeline();
      } catch (e) { note.textContent = ""; VS.toast("Export failed: " + e.message); }
    };
    const ab = $("#dl-aspects", el);
    if (ab) ab.onclick = async () => {
      const aspects = $$(".dl-ar", el).filter(c => c.checked).map(c => c.value);
      if (!aspects.length) { VS.toast("Pick at least one aspect"); return; }
      const note = $("#dl-anote", el);
      ab.disabled = true; ab.textContent = "🎬 encoding masters…";
      note.textContent = "";
      try {
        const r = await VS.post("/api/export-aspects",
          {stem: v.stem, aspects, captioned: what === "captioned" && v.captioned});
        note.innerHTML = "✅ " + r.masters.map(m =>
          `<a href="/media/${m.path}" target="_blank">${m.aspect}</a>`).join(" · ") + " — in Exports";
        VS.toast(`🎬 ${r.masters.length} master(s) exported`);
      } catch (e) { VS.toast("Masters failed: " + e.message); }
      ab.disabled = false; ab.textContent = "🎬 Export aspect masters";
    };
  };

  function renderInspector() {
    const el = $("#ed-ibody");
    const p = P[ED.chip] || P.details;
    el.innerHTML = p();
    if (p.bind) { try { p.bind(el); } catch (e) { console.error(e); } }
  }

  /* ═════════════ upload / glue ═════════════ */
  function upload(files) {
    for (const f of files) {
      const fd = new FormData(); fd.append("file", f);
      const x = new XMLHttpRequest();
      x.open("POST", "/api/upload");
      x.onload = async () => {
        if (x.status === 200) {
          const res = JSON.parse(x.responseText);
          VS.toast(`Uploaded ${res.name}`);
          savedNow();
          if (!/\.(mp3|m4a|wav)$/i.test(res.name))
            VS.post("/api/run", {action: "transcribe", file: res.name}).catch(() => {});
          await VS.refreshLibrary();
          const v = VS.state.videos.find(y => y.name === res.name);
          if (v) select(v);
        } else VS.toast("Upload failed: " + x.responseText.slice(0, 120));
      };
      x.onerror = () => VS.toast("Upload failed");
      x.send(fd);
    }
  }

  function syncURL() {
    const q = new URLSearchParams();
    if (ED.video) q.set("v", ED.video.name);
    q.set("tab", ED.tab); q.set("sec", ED.sec);
    history.replaceState(null, "", "/editor?" + q.toString());
  }
  async function loadVoices() {
    try { ED.voices = (await VS.api("/api/voices")).voices || []; } catch (e) { ED.voices = []; }
  }
  async function loadExtras() {
    try { ED.i2v = (await VS.api("/api/i2v/list")).items || []; } catch (e) { ED.i2v = []; }
    try { ED.actors = (await VS.api("/api/clone/actors")).actors || []; } catch (e) { ED.actors = []; }
  }
  function onLibrary() {
    savedNow();
    if (ED.video) {
      const fresh = VS.state.videos.find(v => v.name === ED.video.name);
      if (fresh) {
        const dubArrived = fresh.dub && !ED.video.dub;
        const dubChanged = fresh.dub_mtime !== ED.video.dub_mtime;
        ED.video = fresh;
        if (dubArrived || dubChanged) { ED.take = "final"; ED.filmKey = null; renderPreview(); renderInspector(); }
        $("#ed-deliverbtn").disabled = !fresh.dub;
      }
    }
    renderContent(); renderTimeline();
  }

  (async () => {
    renderTabs(); renderSubnav(); renderChips();

    // ── resizable panels (drag the splitters; double-click resets) ──
    const main = $("#ed-main");
    const DEFAULTS = {wl: 360, wr: 318, hb: 218};
    const saved = JSON.parse(localStorage.getItem("ed-layout") || "{}");
    const applyLayout = l => {
      main.style.setProperty("--wl", (l.wl || DEFAULTS.wl) + "px");
      main.style.setProperty("--wr", (l.wr || DEFAULTS.wr) + "px");
      main.style.setProperty("--hb", (l.hb || DEFAULTS.hb) + "px");
    };
    applyLayout(saved);
    const layout = Object.assign({}, DEFAULTS, saved);
    function makeSplitter(id, key, horizontal, invert) {
      const sp = $(id);
      sp.addEventListener("pointerdown", e => {
        e.preventDefault();
        sp.classList.add("drag");
        document.body.classList.add("ed-dragging");
        document.body.style.cursor = horizontal ? "row-resize" : "col-resize";
        try { sp.setPointerCapture(e.pointerId); } catch (err) { /* synthetic/odd pointer */ }
        const start = horizontal ? e.clientY : e.clientX;
        const base = layout[key];
        // listen on the DOCUMENT so the drag never drops when the cursor
        // leaves the 6px splitter (the reason dragging felt dead before)
        const move = ev => {
          const delta = (horizontal ? ev.clientY : ev.clientX) - start;
          layout[key] = Math.max(140, Math.min(horizontal ? 460 : 640,
            base + (invert ? -delta : delta)));
          applyLayout(layout);
        };
        const up = () => {
          sp.classList.remove("drag");
          document.body.classList.remove("ed-dragging");
          document.body.style.cursor = "";
          document.removeEventListener("pointermove", move);
          document.removeEventListener("pointerup", up);
          document.removeEventListener("pointercancel", up);
          localStorage.setItem("ed-layout", JSON.stringify(layout));
          renderTimeline();
          const v = playerVideo(); if (v) movePlayhead(v.currentTime);
        };
        document.addEventListener("pointermove", move);
        document.addEventListener("pointerup", up);
        document.addEventListener("pointercancel", up);
      });
      sp.addEventListener("dblclick", () => {
        layout[key] = DEFAULTS[key];
        applyLayout(layout);
        localStorage.setItem("ed-layout", JSON.stringify(layout));
        renderTimeline();
      });
    }
    makeSplitter("#sp-left", "wl", false, false);    // wider drag → wider media panel
    makeSplitter("#sp-right", "wr", false, true);    // drag left → wider inspector
    makeSplitter("#sp-bottom", "hb", true, true);    // drag up → taller timeline

    // ── detachable panels: pop a square out, drag it anywhere, resize it,
    //    and its docked slot collapses so the rest reflow (CapCut-style) ──
    const FLOATS = {
      left:   {sel: ".ed-left",       cls: "fl-left",   title: "Media & Tools", def: {w: 340, h: 560}},
      player: {sel: ".ed-playerwrap", cls: "fl-player", title: "Player",        def: {w: 540, h: 520}},
      right:  {sel: ".ed-inspect",    cls: "fl-right",  title: "Details",       def: {w: 320, h: 560}},
      bottom: {sel: ".ed-tl",         cls: "fl-bottom", title: "Timeline",      def: {w: 940, h: 300}},
    };
    let zTop = 120;
    const floatState = JSON.parse(localStorage.getItem("ed-floats") || "{}");
    const saveFloats = () => localStorage.setItem("ed-floats", JSON.stringify(floatState));
    const isFloating = key => $(FLOATS[key].sel).classList.contains("ed-float");
    const clampX = (x, w) => Math.max(-w + 90, Math.min(window.innerWidth - 60, x));
    const clampY = (y) => Math.max(46, Math.min(window.innerHeight - 40, y));
    const raise = el => { el.style.zIndex = ++zTop; };
    const rememberFloat = (key, el) => {
      floatState[key] = {on: true, x: el.offsetLeft, y: el.offsetTop, w: el.offsetWidth, h: el.offsetHeight};
      saveFloats();
    };
    function updatePopBtns() {
      $$(".ed-pop").forEach(b => {
        const on = isFloating(b.dataset.pop);
        b.classList.toggle("on", on);
        b.title = on ? "dock this panel back" : "pop out — float this panel";
      });
    }
    function floatPanel(key, st) {
      const cfg = FLOATS[key], el = $(cfg.sel);
      if (el.classList.contains("ed-float")) return;
      const bar = document.createElement("div");
      bar.className = "ed-fbar";
      bar.innerHTML = `<span class="ttl">${cfg.title}</span><span class="dots">•••</span>
        <button class="dock" title="dock back">⤡</button>`;
      el.insertBefore(bar, el.firstChild);
      el.classList.add("ed-float");
      main.classList.add(cfg.cls);
      // collapse the docked track (inline, so it wins over applyLayout's inline vars)
      if (key === "left")   { main.style.setProperty("--wl", "0px"); main.style.setProperty("--spl", "0px"); }
      if (key === "right")  { main.style.setProperty("--wr", "0px"); main.style.setProperty("--spr", "0px"); }
      if (key === "bottom") { main.style.setProperty("--hb", "0px"); main.style.setProperty("--sphb", "0px"); }
      const s = st || floatState[key] || {};
      const w = s.w || cfg.def.w, h = s.h || cfg.def.h;
      el.style.width = w + "px"; el.style.height = h + "px";
      el.style.left = clampX(s.x != null ? s.x : (window.innerWidth - w) / 2, w) + "px";
      el.style.top  = clampY(s.y != null ? s.y : (window.innerHeight - h) / 2) + "px";
      raise(el);
      rememberFloat(key, el);
      bar.addEventListener("pointerdown", ev => {
        if (ev.target.closest(".dock")) return;
        ev.preventDefault(); raise(el);
        el.classList.add("drag"); document.body.classList.add("ed-dragging");
        try { bar.setPointerCapture(ev.pointerId); } catch (e) { /* synthetic */ }
        const ox = ev.clientX - el.offsetLeft, oy = ev.clientY - el.offsetTop;
        const mv = e => {
          el.style.left = clampX(e.clientX - ox, el.offsetWidth) + "px";
          el.style.top  = clampY(e.clientY - oy) + "px";
        };
        const up = () => {
          el.classList.remove("drag"); document.body.classList.remove("ed-dragging");
          document.removeEventListener("pointermove", mv);
          document.removeEventListener("pointerup", up);
          document.removeEventListener("pointercancel", up);
          rememberFloat(key, el);
          if (key === "bottom" || key === "player") { renderTimeline(); const vv = playerVideo(); if (vv) movePlayhead(vv.currentTime); }
        };
        document.addEventListener("pointermove", mv);
        document.addEventListener("pointerup", up);
        document.addEventListener("pointercancel", up);
      });
      el.addEventListener("pointerdown", () => raise(el), true);
      bar.querySelector(".dock").onclick = () => dockPanel(key);
      if (window.ResizeObserver) {
        const ro = new ResizeObserver(() => {
          if (!el.classList.contains("ed-float")) return;
          rememberFloat(key, el);
          if (key === "bottom") renderTimeline();
        });
        ro.observe(el); el._ro = ro;
      }
      updatePopBtns();
      if (key === "bottom" || key === "player") renderTimeline();
    }
    function dockPanel(key) {
      const cfg = FLOATS[key], el = $(cfg.sel);
      if (!el.classList.contains("ed-float")) return;
      const bar = el.querySelector(":scope > .ed-fbar"); if (bar) bar.remove();
      el.classList.remove("ed-float");
      main.classList.remove(cfg.cls);
      // restore the docked track
      if (key === "left")   { main.style.setProperty("--wl", layout.wl + "px"); main.style.removeProperty("--spl"); }
      if (key === "right")  { main.style.setProperty("--wr", layout.wr + "px"); main.style.removeProperty("--spr"); }
      if (key === "bottom") { main.style.setProperty("--hb", layout.hb + "px"); main.style.removeProperty("--sphb"); }
      el.style.cssText = el.style.cssText
        .replace(/(left|top|width|height|z-index):[^;]+;?/g, "");
      if (el._ro) { el._ro.disconnect(); el._ro = null; }
      floatState[key] = {on: false}; saveFloats();
      updatePopBtns();
      if (key === "bottom" || key === "player") renderTimeline();
    }
    const toggleFloat = key => isFloating(key) ? dockPanel(key) : floatPanel(key);
    $$(".ed-pop").forEach(b => b.onclick = () => toggleFloat(b.dataset.pop));
    $("#ghost-dock").onclick = () => dockPanel("player");
    // restore any panels that were left floating
    Object.keys(FLOATS).forEach(k => { if (floatState[k] && floatState[k].on) floatPanel(k, floatState[k]); });

    // top bar
    const mb = $("#ed-menubtn"), ml = $("#ed-menulist");
    mb.onclick = e => { e.stopPropagation(); ml.classList.toggle("open"); };
    document.addEventListener("click", () => ml.classList.remove("open"));
    $("#ed-toggle-left").onclick = () => { $("#ed-main").classList.toggle("no-left");
      $("#ed-toggle-left").classList.toggle("off"); };
    $("#ed-toggle-right").onclick = () => { $("#ed-main").classList.toggle("no-right");
      $("#ed-toggle-right").classList.toggle("off"); };
    $("#ed-deliverbtn").onclick = () => { ED.chip = "deliver"; renderChips(); renderInspector(); };

    // browser
    $("#ed-import").onclick = () => $("#ed-file").click();
    $("#ed-file").addEventListener("change", e => { upload(e.target.files); e.target.value = ""; });
    const assets = $("#ed-assets");
    assets.addEventListener("dragover", e => e.preventDefault());
    assets.addEventListener("drop", e => { e.preventDefault(); upload(e.dataTransfer.files); });
    $("#ed-search").addEventListener("input", e => { ED.search = e.target.value; renderContent(); });
    const openFix = () => { if (ED.video && ED.video.dub) { ED.chip = "fix"; renderChips(); renderInspector();
        const t = $("#fx-txt"); if (t) t.focus(); }
      else VS.toast(ED.video ? "Dub the video first — repairs work on the dub" : "Pick a video first"); };
    $("#ed-strip-btn").onclick = openFix;
    $("#ed-pilot").onclick = openFix;

    // player
    const togglePlay = () => { const v = playerVideo(); if (v) v.paused ? v.play() : v.pause(); };
    $("#ed-play").onclick = togglePlay;
    $("#tl-play").onclick = togglePlay;
    $("#tl-tostart").onclick = () => { const v = playerVideo(); if (v) { v.currentTime = 0; movePlayhead(0); } };
    $("#ed-fs").onclick = () => { const v = playerVideo(); if (v && v.requestFullscreen) v.requestFullscreen(); };
    // video zoom — scales the whole frame so you can inspect details up close
    ED.zoom = 1;
    const applyZoom = () => {
      const f = $("#ed-frame");
      f.style.transform = ED.zoom === 1 ? "" : `scale(${ED.zoom})`;
      $("#ed-zoomlvl").textContent = Math.round(ED.zoom * 100) + "%";
      $("#ed-zoomlvl").classList.toggle("on", ED.zoom !== 1);
    };
    $("#ed-zoomin").onclick = () => { ED.zoom = Math.min(3, +(ED.zoom + 0.25).toFixed(2)); applyZoom(); };
    $("#ed-zoomout").onclick = () => { ED.zoom = Math.max(0.5, +(ED.zoom - 0.25).toFixed(2)); applyZoom(); };
    $("#ed-zoomlvl").onclick = () => { ED.zoom = 1; applyZoom(); };
    // ctrl+scroll over the player also zooms
    $(".ed-stage").addEventListener("wheel", e => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      ED.zoom = Math.max(0.5, Math.min(3, +(ED.zoom + (e.deltaY < 0 ? 0.15 : -0.15)).toFixed(2)));
      applyZoom();
    }, {passive: false});
    $("#ed-fit").onclick = () => { ED.fit = ED.fit === "contain" ? "cover" : "contain";
      $("#ed-fit").classList.toggle("on", ED.fit === "cover"); renderPreview(); };
    $("#ed-ratio").onclick = () => { ED.ratio = ED.ratio === "9:16" ? "1:1" : ED.ratio === "1:1" ? "16:9" : "9:16";
      renderPreview(); };
    $("#ed-take").onclick = () => { ED.take = ED.take === "final" ? "source" : "final";
      ED.filmKey = null; renderPreview(); renderTimeline(); };
    $("#ed-pmenu").onclick = () => { const s = previewSrc(); if (s) window.open(s, "_blank"); };
    document.addEventListener("keydown", e => {
      if (e.code === "Space" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
        e.preventDefault(); togglePlay();
      }
    });

    // timeline
    $("#tl-zoom").addEventListener("input", e => { ED.pxs = +e.target.value; renderTimeline();
      const v = playerVideo(); if (v) movePlayhead(v.currentTime); });
    $("#tl-refresh").onclick = () => { ED.filmKey = null; VS.refreshLibrary(); renderTimeline(); };
    $("#tl-in").onclick = () => { const v = playerVideo(); if (!v) return;
      ED.cutIn = v.currentTime;
      if (ED.cutOut != null && ED.cutOut <= ED.cutIn) ED.cutOut = null;
      $("#tl-in").classList.add("armed"); renderTimeline(); };
    $("#tl-out").onclick = () => { const v = playerVideo(); if (!v) return;
      ED.cutOut = v.currentTime; $("#tl-out").classList.add("armed"); renderTimeline(); };
    $("#tl-cut").onclick = async () => {
      const v = ED.video;
      if (!v || ED.cutIn == null || ED.cutOut == null) return;
      const rel = (ED.take === "final" && v.dub) ? v.dub : "uploads/" + v.name;
      try {
        const r = await VS.post("/api/edit", {path: rel, start: +ED.cutIn.toFixed(2), end: +ED.cutOut.toFixed(2)});
        VS.toast(`✂ Cut exported — ${(ED.cutOut - ED.cutIn).toFixed(1)}s clip in Exports`);
        ED.cutIn = ED.cutOut = null;
        $("#tl-in").classList.remove("armed"); $("#tl-out").classList.remove("armed");
        renderTimeline();
      } catch (e) { VS.toast("Cut failed: " + e.message); }
    };

    await loadVoices();
    await loadExtras();
    await VS.start();
    VS.on("library", onLibrary);
    VS.on("jobs", () => { renderTimeline(); });
    VS.on("job-done", () => { VS.refreshLibrary(); loadVoices(); loadExtras(); });

    try { const s = await VS.api("/api/spend"); $("#ed-spend").textContent = `fal ⛁ $${s.total.toFixed(2)}`; } catch (e) { /* */ }

    renderContent();
    savedNow();
    const q = new URLSearchParams(location.search);
    ED.tab = q.get("tab") || "media";
    ED.sec = q.get("sec") || (NAV[ED.tab].find(x => x[0] !== "h") || ["media"])[0];
    renderTabs(); renderSubnav(); renderContent();
    const want = q.get("v");
    if (want) {
      const v = VS.state.videos.find(x => x.name === want);
      if (v) select(v);
    }
  })();
})();
