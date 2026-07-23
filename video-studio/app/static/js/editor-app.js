/* editor-app.js — the CapCut-style editor, wired to the REAL Video Studio API.
   Reuses vs-core.js (VS.api/post/toast + the single 3s poller + pub-sub).
   Zones: tool tabs → Inspector panels · library browser · live preview ·
   pipeline tracks driven by /api/jobs + library flags. */
(function () {
  const VS = window.VS;
  const $ = (s, r) => (r || document).querySelector(s);

  const ED = {
    video: null,        // selected library video (live object from VS.state)
    tool: "media",
    take: "final",      // preview source: final | source
    voices: [],
  };

  /* ── tools registry ─────────────────────────────────────────────────── */
  const TOOLS = [
    {id: "media",    ic: "🎞", label: "Media"},
    {id: "clean",    ic: "🧹", label: "Clean"},
    {id: "script",   ic: "✍️", label: "Script"},
    {id: "dub",      ic: "🎙", label: "Dub"},
    {id: "voices",   ic: "🗣", label: "Voices"},
    {id: "fix",      ic: "🩹", label: "Fix"},
    {id: "captions", ic: "💬", label: "Captions"},
    {id: "i2v",      ic: "🎬", label: "Img→Vid"},
    {id: "clone",    ic: "🏆", label: "Clone"},
    {id: "deliver",  ic: "📤", label: "Deliver"},
  ];

  function renderTools() {
    $("#ed-tools").innerHTML = TOOLS.map(t =>
      `<button class="ed-tool ${ED.tool === t.id ? "on" : ""}" data-t="${t.id}">
         <span class="ic">${t.ic}</span>${t.label}</button>`).join("");
    VS.$$(".ed-tool").forEach(b => b.onclick = () => setTool(b.dataset.t));
  }

  function setTool(id) {
    ED.tool = id;
    renderTools();
    renderInspector();
    syncURL();
  }

  /* ── library browser ────────────────────────────────────────────────── */
  function renderGrid() {
    const g = $("#ed-grid");
    const vids = VS.state.videos;
    if (!vids.length) { g.innerHTML = '<div class="ed-empty">no videos yet — drop one above</div>'; return; }
    g.innerHTML = vids.map(v => {
      const run = VS.runningFor(v);
      const badge = run ? `<span class="bdg run">⏳ ${VS.esc(run.action)}</span>`
        : v.exported ? '<span class="bdg ok">delivered</span>'
        : v.dub ? '<span class="bdg" style="color:var(--accent2)">dubbed</span>' : "";
      return `<div class="ed-card ${ED.video && ED.video.name === v.name ? "sel" : ""}" data-n="${VS.esc(v.name)}">
        <div class="th"><img loading="lazy" src="/api/thumb/${encodeURIComponent(v.name)}" alt="">${badge}</div>
        <div class="nm">${VS.esc(v.title || v.name)}</div></div>`;
    }).join("");
    VS.$$(".ed-card").forEach(el => el.onclick = () => {
      const v = VS.state.videos.find(x => x.name === el.dataset.n);
      if (v) select(v);
    });
  }

  function select(v, tool) {
    ED.video = v;
    ED.take = v.dub ? "final" : "source";
    $("#ed-projname").textContent = v.title || v.name;
    $("#ed-deliverbtn").disabled = !v.dub;
    renderGrid();
    renderPreview();
    renderTracks();
    setTool(tool || ED.tool);
  }

  /* ── preview ────────────────────────────────────────────────────────── */
  function previewSrc() {
    const v = ED.video;
    if (!v) return null;
    if (ED.take === "final" && v.dub) return "/media/" + v.dub + "?v=" + (v.dub_mtime || "");
    return "/media/uploads/" + encodeURIComponent(v.name);
  }

  function renderPreview() {
    const f = $("#ed-frame");
    const src = previewSrc();
    if (!src) { f.innerHTML = '<div class="ph">← pick a video from the library</div>'; return; }
    let vid = f.querySelector("video");
    if (!vid) {
      f.innerHTML = "";
      vid = document.createElement("video");
      vid.playsInline = true;
      vid.preload = "metadata";
      f.appendChild(vid);
      vid.addEventListener("timeupdate", () => { $("#ed-tc").textContent = fmt(vid.currentTime); });
      vid.addEventListener("loadedmetadata", () => { $("#ed-dur").textContent = fmt(vid.duration); });
      vid.addEventListener("play", () => { $("#ed-play").textContent = "⏸"; });
      vid.addEventListener("pause", () => { $("#ed-play").textContent = "▶"; });
    }
    if (!vid.src.endsWith(encodeURI(src))) { vid.src = src; }
    $("#ed-take-final").classList.toggle("on", ED.take === "final");
    $("#ed-take-source").classList.toggle("on", ED.take === "source");
    $("#ed-take-final").style.display = ED.video && ED.video.dub ? "" : "none";
  }
  const fmt = s => isFinite(s) ? Math.floor(s / 60) + ":" + String(Math.floor(s % 60)).padStart(2, "0") : "0:00";

  /* ── pipeline tracks ────────────────────────────────────────────────── */
  function lane(label, color, clipHtml) {
    return `<div class="ed-lane"><div class="lab"><i style="background:${color}"></i>${label}</div>
      <div class="ed-clips">${clipHtml}</div></div>`;
  }
  function clip(cls, text, st) {
    return `<div class="ed-clip ${cls}">${text}${st ? `<span class="st">${st}</span>` : ""}</div>`;
  }

  function renderTracks() {
    const v = ED.video;
    const el = $("#ed-lanes");
    if (!v) { el.innerHTML = '<div class="ed-empty" style="padding:22px">pipeline appears when you pick a video</div>'; return; }
    const jobs = VS.jobsFor(v).filter(j => j.status === "running");
    const jb = a => jobs.find(j => a.includes(j.action));
    const pct = j => (j && j.progress && j.progress.pct != null) ? j.progress.pct + "%" : "running…";

    // video lane
    const cleanJob = jb(["clean-subs", "transcribe"]);
    let videoClip;
    if (cleanJob) videoClip = clip("video running", "source · " + cleanJob.action, pct(cleanJob));
    else videoClip = clip("video", "source" + (v.cleaned ? " · subtitles erased" : v.no_subs ? " · clean" : ""),
                          v.cleaned || v.no_subs ? "✓" : (v.transcript ? "transcribed" : "raw"));
    // voice lane
    const dubJob = jb(["dub", "duo", "diarize"]);
    let voiceClip;
    if (dubJob) voiceClip = clip("voice running", dubJob.label || "dubbing", pct(dubJob));
    else if (v.dub) voiceClip = clip("voice", "dubbed voice", "✓ final.mp4");
    else voiceClip = clip("ghost", v.script ? "ready — run the Dub tool" : "needs a script first");
    // captions lane
    const capJob = jb(["caption", "recaption"]);
    let capClip;
    if (capJob) capClip = clip("caps running", "burning captions", pct(capJob));
    else if (v.captioned) capClip = clip("caps", "captions burned", "✓");
    else capClip = clip("ghost", v.dub ? "ready — run the Captions tool" : "waiting for a dub");
    // deliver lane
    let delClip;
    if (v.exported) delClip = clip("deliver", "delivered to Desktop", "✓");
    else delClip = clip("ghost", v.dub ? "ready — Deliver when happy" : "finish the pipeline first");

    el.innerHTML =
      lane("Video", "#4a6bd8", videoClip) +
      lane("Voice", "#2f9e75", voiceClip) +
      lane("Captions", "#c98a2f", capClip) +
      lane("Deliver", "#7c5cff", delClip);

    const running = jobs[0];
    $("#ed-jobnote").textContent = running ? `⏳ ${running.label || running.action}` : "";
  }

  /* ── inspector panels ───────────────────────────────────────────────── */
  const P = {};   // panel renderers

  function needVideo(title, hint) {
    return `<h3>${title}</h3><div class="h2">Pick a video first</div>
      <div class="sub">${hint || "Choose a video in the library on the left."}</div>`;
  }

  P.media = () => {
    const v = ED.video;
    if (!v) return needVideo("Media");
    return `<h3>Media</h3><div class="h2">${VS.esc(v.title || v.name)}</div>
      <div class="sub">${VS.esc(v.name)} · ${VS.fmtSize(v.size)}</div>
      <div class="ed-field"><label>Title</label><input type="text" id="mi-title" value="${VS.esc(v.title || "")}" placeholder="${VS.esc(v.name)}"></div>
      <div class="ed-field"><label>Character</label><input type="text" id="mi-char" value="${VS.esc(v.character || "")}" placeholder="who's in it"></div>
      <div class="ed-field"><label>Tags</label><input type="text" id="mi-tags" value="${VS.esc((v.tags || []).join(", "))}" placeholder="comma, separated"></div>
      <button class="ed-run ghostly" id="mi-save">💾 Save details</button>
      <div class="ed-note">Progress: ${v.transcript ? "✓ transcribed" : "not transcribed"} · ${v.cleaned || v.no_subs ? "✓ clean" : "subs not cleaned"} · ${v.dub ? "✓ dubbed" : "not dubbed"} · ${v.captioned ? "✓ captioned" : "no captions"} · ${v.exported ? "✓ delivered" : "not delivered"}</div>`;
  };
  P.media.bind = el => {
    const v = ED.video;
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
    if (!v) return needVideo("Clean");
    const run = VS.runningFor(v, ["clean-subs"]);
    return `<h3>Clean</h3><div class="h2">Burned-in subtitles</div>
      <div class="sub">Erase old captions from the pixels before dubbing — free, on your GPU.</div>
      ${run ? `<div class="ed-msg">⏳ erasing… watch the pipeline below</div>` : ""}
      <button class="ed-run" id="cl-erase" ${run ? "disabled" : ""}>🧹 ${v.cleaned ? "Re-erase" : "Erase burned-in subtitles"}</button>
      ${v.cleaned ? `<button class="ed-run ghostly" id="cl-restore">↩ Restore original</button>` : ""}
      <div class="ed-field" style="margin-top:12px"><label style="display:flex;gap:6px;align-items:center">
        <input type="checkbox" id="cl-nosubs" ${v.no_subs ? "checked" : ""}> this video has no burned subtitles</label></div>
      <div class="ed-hr"></div>
      <div class="ed-note">Transcribe (needed for Script): ${v.transcript ? "✓ done" : ""}</div>
      <button class="ed-run ghostly" id="cl-transcribe">${v.transcript ? "↻ Re-transcribe" : "▶ Transcribe"}</button>`;
  };
  P.clean.bind = el => {
    const v = ED.video;
    $("#cl-erase", el).onclick = async () => {
      try { await VS.post("/api/clean-subs", {file: v.name, auto: true, mode: "erase"});
        VS.toast("🧹 Erasing — this is the long one"); renderTracks(); }
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
      try { await VS.post("/api/run", {action: "transcribe", file: v.name}); VS.toast("Transcribing…"); renderTracks(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
  };

  P.script = () => {
    const v = ED.video;
    if (!v) return needVideo("Script");
    return `<h3>Script</h3><div class="h2">The words they'll say</div>
      <div class="sub">${v.orig_words ? `original spoke ~${v.orig_words} words — stay close so the lips fit` : "write or load the script"}</div>
      <div class="ed-field"><textarea id="sc-text" rows="9" placeholder="loading…"></textarea></div>
      <div class="ed-field"><input type="text" id="sc-steer" placeholder="✨ AI rewrite — e.g. punchier hook, same length"></div>
      <button class="ed-run ghostly" id="sc-ai">✨ Rewrite with AI</button>
      <button class="ed-run" id="sc-save">💾 Save script</button>
      <div class="ed-cost free" id="sc-count"></div>`;
  };
  P.script.bind = async el => {
    const v = ED.video;
    const ta = $("#sc-text", el);
    const count = () => { $("#sc-count", el).textContent =
      ta.value.trim().split(/\s+/).filter(Boolean).length + " words"; };
    ta.oninput = count;
    try { const s = await VS.api("/api/script/" + encodeURIComponent(v.stem));
      ta.value = s.text || ""; ta.placeholder = "write the script…"; count(); } catch (e) { /* empty */ }
    $("#sc-save", el).onclick = async () => {
      const text = ta.value.trim();
      if (!text) { VS.toast("Write the script first"); return; }
      try { await VS.post("/api/script/" + encodeURIComponent(v.stem), {text});
        VS.toast("Script saved — Dub unlocked"); VS.refreshLibrary(); renderTracks(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
    $("#sc-ai", el).onclick = async () => {
      const b = $("#sc-ai", el);
      if (!ta.value.trim()) { VS.toast("Nothing to rewrite yet"); return; }
      b.disabled = true; b.textContent = "✨ rewriting… (~30s)";
      try { const r = await VS.post("/api/copywrite", {text: ta.value.trim(), instruction: $("#sc-steer", el).value.trim()});
        ta.value = r.text || ta.value; count(); VS.toast("Rewritten — review then Save"); }
      catch (e) { VS.toast("Rewrite failed: " + e.message); }
      b.disabled = false; b.textContent = "✨ Rewrite with AI";
    };
  };

  P.dub = () => {
    const v = ED.video;
    if (!v) return needVideo("Dub & Lip-sync");
    const run = VS.runningFor(v, ["dub", "duo"]);
    const voiceOpts = ['<option value="">🎤 On-screen speaker (clone from this video)</option>']
      .concat(ED.voices.map(vo => `<option value="${VS.esc(vo.id)}">🎙 ${VS.esc(vo.name)}</option>`)).join("");
    return `<h3>Dub &amp; Lip-sync</h3><div class="h2">Give it a voice</div>
      <div class="sub">Clone a voice and speak your script onto this footage.</div>
      <div class="ed-opt on" data-eng="local"><div class="t">💻 Local — FREE<span class="price" style="color:var(--ok)">$0 · ~15m</span></div>
        <div class="d">XTTS + Wav2Lip HD on your GPU. Great for testing.</div></div>
      <div class="ed-opt" data-eng="fal"><div class="t">☁ Premium cloud — PAID<span class="price" style="color:var(--warn)">~$1.50 + $3/min</span></div>
        <div class="d">MiniMax HD voice + sync.so pro — the winner pipeline.</div></div>
      <div class="ed-field"><label>Voice</label><select id="du-voice">${voiceOpts}</select></div>
      <div class="ed-field" id="du-localrow"><label>Lip-sync</label><select id="du-lip">
        <option value="wav2lip-hd" selected>Wav2Lip HD (GFPGAN, free)</option>
        <option value="wav2lip">Wav2Lip (faster, softer)</option>
        <option value="none">voice only (silent/generated clips)</option></select></div>
      <div class="ed-field" id="du-falrow" style="display:none"><label>Cloud voice · lip-sync tier</label>
        <select id="du-tts"><option value="hd" selected>MiniMax HD</option><option value="turbo">MiniMax turbo</option><option value="f5">F5 (no clone fee)</option></select>
        <select id="du-tier" style="margin-top:6px"><option value="standard" selected>sync v2 standard</option><option value="pro">sync v2 pro (best)</option><option value="veed">veed (cheap)</option><option value="latentsync">latentsync (cheapest)</option></select></div>
      ${run ? `<div class="ed-msg">⏳ ${VS.esc(run.label || "dub running")} — one dub at a time</div>` : ""}
      <button class="ed-run" id="du-run" ${run || !v.script ? "disabled" : ""}>🎙 Dub</button>
      <div class="ed-cost free" id="du-cost">✓ 100% local — nothing is charged</div>
      ${!v.script ? '<div class="ed-msg">save a Script first — the dub needs words</div>' : ""}
      <div class="ed-note">Two people talking? Interview mode lives in the <a href="/?v=${encodeURIComponent(v.name)}&step=dub">classic Dub step ↗</a> for now.</div>`;
  };
  P.dub.bind = el => {
    const v = ED.video;
    let engine = "local";
    VS.$$(".ed-opt[data-eng]", el).forEach(o => o.onclick = () => {
      engine = o.dataset.eng;
      VS.$$(".ed-opt[data-eng]", el).forEach(x => x.classList.toggle("on", x === o));
      $("#du-localrow", el).style.display = engine === "local" ? "" : "none";
      $("#du-falrow", el).style.display = engine === "fal" ? "" : "none";
      const c = $("#du-cost", el);
      if (engine === "fal") { c.textContent = "💰 voice + lip-sync charged on fal.ai — you approve before it runs"; c.className = "ed-cost paid"; }
      else { c.textContent = "✓ 100% local — nothing is charged"; c.className = "ed-cost free"; }
    });
    $("#du-run", el).onclick = async () => {
      const body = {action: "dub", file: v.name, engine};
      if (engine === "local") {
        body.lipsync = $("#du-lip", el).value;
        const vid = $("#du-voice", el).value;
        if (vid) body.voice_id = vid;
      } else {
        body.tts = $("#du-tts", el).value;
        body.tier = $("#du-tier", el).value;
        if (!confirm("⚠ This spends money on fal.ai (voice + lip-sync).\n\nApprove and start?")) return;
        body.confirm_cost = true;
      }
      try {
        await VS.post("/api/run", body);
        VS.toast(engine === "local" ? "🎙 Free local dub started" : "🎙 Cloud dub started");
        renderTracks(); renderInspector();
      } catch (e) { VS.toast("Couldn't start: " + e.message); }
    };
  };

  P.voices = () => {
    const v = ED.video;
    const list = ED.voices.length
      ? ED.voices.map(vo => `<div class="ed-opt" style="cursor:default"><div class="t">🎙 ${VS.esc(vo.name)}</div>
          ${vo.sample ? `<audio controls preload="none" src="/media/${vo.sample}" style="width:100%;margin-top:6px"></audio>` : ""}</div>`).join("")
      : '<div class="ed-note">no saved voices yet</div>';
    return `<h3>Voice Bank</h3><div class="h2">Saved voices</div>
      <div class="sub">Clone once, reuse on any character.</div>${list}
      ${v ? `<button class="ed-run ghostly" id="vo-add">➕ Clone the voice from “${VS.esc(v.title || v.name)}”</button>` : ""}
      <div class="ed-note"><a href="/voices">manage the full Voice Bank ↗</a></div>`;
  };
  P.voices.bind = el => {
    const v = ED.video;
    const b = $("#vo-add", el);
    if (b) b.onclick = async () => {
      b.disabled = true; b.textContent = "🎧 cloning… (a few seconds)";
      try { await VS.post("/api/voices/create", {file: v.name, name: v.character || v.title || v.stem});
        await loadVoices(); VS.toast("Voice saved"); renderInspector(); }
      catch (e) { VS.toast("Error: " + e.message); b.disabled = false; b.textContent = "➕ Clone this voice"; }
    };
  };

  P.fix = () => {
    const v = ED.video;
    if (!v) return needVideo("Fix");
    if (!v.dub) return `<h3>Fix</h3><div class="h2">Nothing to fix yet</div><div class="sub">Run a Dub first — repairs work on the dubbed video.</div>`;
    return `<h3>Fix &amp; QA</h3><div class="h2">Something looks wrong?</div>
      <div class="sub">Describe it — the AI picks the repair. Every fix is a new take; final is never lost.</div>
      <div class="ed-field"><input type="text" id="fx-txt" placeholder="e.g. the cup warps when he drinks"></div>
      <button class="ed-run ghostly" id="fx-advise">🪄 Analyze</button>
      <div id="fx-out"></div>
      <div class="ed-hr"></div>
      <div class="ed-field"><label>Quick repairs</label></div>
      <button class="ed-run ghostly" data-fix="relipsync">👄 Re-lip-sync (local)</button>
      <button class="ed-run ghostly" data-fix="renorm">🔊 Fix loudness</button>
      <button class="ed-run ghostly" data-fix="remux">📦 Remux voice onto source</button>
      <div class="ed-note">deep tools (box drawing, frame swap): <a href="/dubsync-lab" target="_blank">DubSync lab ↗</a> · review frames: <a href="/qc-lab" target="_blank">QC lab ↗</a></div>`;
  };
  P.fix.bind = el => {
    const v = ED.video;
    VS.$$("[data-fix]", el).forEach(b => b.onclick = async () => {
      const a = b.dataset.fix;
      try { await VS.post("/api/dubsync/repair", a === "relipsync" ? {stem: v.stem, action: a, restorer: "gfpgan"} : {stem: v.stem, action: a});
        VS.toast("🔧 Repair running — new take lands in the workdir"); renderTracks(); }
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
        out.innerHTML = `<div class="ed-opt" style="cursor:default"><div class="t">${VS.esc(a.action || "?")}</div>
          <div class="d">${VS.esc(a.explanation || "")}</div>
          <button class="ed-run" id="fx-doit" style="margin-top:8px">▶ Run this fix</button></div>`;
        $("#fx-doit", out).onclick = async () => {
          const body = {stem: v.stem, action: a.action};
          if (a.samples) body.samples = a.samples;
          if (a.box) body.box = a.box;
          if (a.track != null) body.track = a.track;
          if (a.action === "relipsync") body.restorer = "gfpgan";
          try { await VS.post("/api/dubsync/repair", body); VS.toast("🔧 Fix running"); renderTracks(); }
          catch (e) { VS.toast("Error: " + e.message); }
        };
      } catch (e) { out.innerHTML = `<div class="ed-msg">analysis failed: ${VS.esc(e.message)}</div>`; }
      btn.disabled = false;
    };
  };

  P.captions = () => {
    const v = ED.video;
    if (!v) return needVideo("Captions");
    const run = VS.runningFor(v, ["caption", "recaption"]);
    return `<h3>Captions</h3><div class="h2">Word-timed captions</div>
      <div class="sub">Bold captions timed to the actual audio, burned over the old band.</div>
      ${run ? '<div class="ed-msg">⏳ burning… watch the pipeline</div>' : ""}
      <button class="ed-run" id="cp-dub" ${!v.dub || run ? "disabled" : ""}>🔥 Caption the dubbed video</button>
      <button class="ed-run ghostly" id="cp-orig" ${run ? "disabled" : ""}>Caption the original instead</button>
      ${v.captioned ? `<div class="ed-note">✓ captioned — <a href="/captioned/${encodeURIComponent(v.stem)}" target="_blank">watch it ↗</a> · edit lines in the <a href="/?v=${encodeURIComponent(v.name)}&step=captions">classic view ↗</a></div>` : ""}`;
  };
  P.captions.bind = el => {
    const v = ED.video;
    $("#cp-dub", el).onclick = async () => {
      try { await VS.post("/api/run", {action: "caption", file: v.stem}); VS.toast("🔥 Captioning the dub…"); renderTracks(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
    $("#cp-orig", el).onclick = async () => {
      try { await VS.post("/api/recaption", {path: "uploads/" + v.name, mode: "captions"}); VS.toast("🔥 Captioning the original…"); renderTracks(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
  };

  P.i2v = () => `<h3>Image → Video</h3><div class="h2">Make clips from stills</h2>
    <div class="sub">Upload a picture + a motion prompt → a ~30s clip (fal.ai, cost-gated).</div>
    <a class="ed-run" style="display:block;text-align:center;text-decoration:none" href="/image-to-video">🎬 Open Image→Video studio ↗</a>
    <div class="ed-note">Finished clips land in your library here once you dub or deliver them.</div>`;

  P.clone = () => {
    const v = ED.video;
    if (!v || !v.dub) return `<h3>Clone Winner</h3><div class="h2">Scale a proven ad</div>
      <div class="sub">Pick a dubbed video first — then clone it with a fresh similar script.</div>`;
    return `<h3>Clone Winner</h3><div class="h2">Multiply “${VS.esc(v.title || v.stem)}”</div>
      <div class="sub">Same winning structure, fresh words — same or different actor.</div>
      <a class="ed-run" style="display:block;text-align:center;text-decoration:none"
         href="/?v=${encodeURIComponent(v.name)}&step=deliver">🏆 Open the Clone panel ↗</a>`;
  };

  P.deliver = () => {
    const v = ED.video;
    if (!v) return needVideo("Deliver");
    return `<h3>Deliver</h3><div class="h2">Send to Desktop</div>
      <div class="sub">Copies the finished video to your Desktop export folder.</div>
      <div class="ed-opt ${v.captioned ? "on" : ""}" data-del="captioned" style="${v.captioned ? "" : "opacity:.45;pointer-events:none"}">
        <div class="t">🔥 Captioned final</div><div class="d">recommended for ads</div></div>
      <div class="ed-opt ${v.captioned ? "" : "on"}" data-del="final" style="${v.dub ? "" : "opacity:.45;pointer-events:none"}">
        <div class="t">🎬 Final (no captions)</div></div>
      <div class="ed-field"><label style="display:flex;gap:6px;align-items:center">
        <input type="checkbox" id="dl-clean" checked> auto-clean workspace after export (24h, reversible)</label></div>
      <button class="ed-run" id="dl-send" ${v.dub ? "" : "disabled"}>📤 Send to Desktop</button>
      <div class="ed-note" id="dl-note"></div>`;
  };
  P.deliver.bind = el => {
    const v = ED.video;
    let what = v.captioned ? "captioned" : "final";
    VS.$$(".ed-opt[data-del]", el).forEach(o => o.onclick = () => {
      what = o.dataset.del;
      VS.$$(".ed-opt[data-del]", el).forEach(x => x.classList.toggle("on", x === o));
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
        VS.refreshLibrary(); renderTracks();
      } catch (e) { note.textContent = ""; VS.toast("Export failed: " + e.message); }
    };
  };

  function renderInspector() {
    const el = $("#ed-inspect");
    const p = P[ED.tool] || P.media;
    el.innerHTML = p();
    if (p.bind) { try { p.bind(el); } catch (e) { console.error(e); } }
  }

  /* ── upload ─────────────────────────────────────────────────────────── */
  function upload(files) {
    for (const f of files) {
      const fd = new FormData(); fd.append("file", f);
      const x = new XMLHttpRequest();
      x.open("POST", "/api/upload");
      x.onload = async () => {
        if (x.status === 200) {
          const res = JSON.parse(x.responseText);
          VS.toast(`Uploaded ${res.name} — transcribing…`);
          VS.post("/api/run", {action: "transcribe", file: res.name}).catch(() => {});
          await VS.refreshLibrary();
          const v = VS.state.videos.find(y => y.name === res.name);
          if (v) select(v, "clean");
        } else VS.toast("Upload failed: " + x.responseText.slice(0, 120));
      };
      x.onerror = () => VS.toast("Upload failed");
      x.send(fd);
    }
  }

  /* ── glue ───────────────────────────────────────────────────────────── */
  function syncURL() {
    const q = ED.video ? `?v=${encodeURIComponent(ED.video.name)}&tool=${ED.tool}` : "";
    history.replaceState(null, "", "/editor" + q);
  }

  async function loadVoices() {
    try { ED.voices = (await VS.api("/api/voices")).voices || []; } catch (e) { ED.voices = []; }
  }

  function onLibrary() {
    if (ED.video) {
      const fresh = VS.state.videos.find(v => v.name === ED.video.name);
      if (fresh) {
        const dubArrived = fresh.dub && !ED.video.dub;
        ED.video = fresh;
        if (dubArrived) { ED.take = "final"; renderPreview(); renderInspector(); }
        $("#ed-deliverbtn").disabled = !fresh.dub;
      }
    }
    renderGrid(); renderTracks();
  }

  (async () => {
    renderTools();
    // upload wiring
    const drop = $("#ed-drop"), file = $("#ed-file");
    drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("drag"); });
    drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
    drop.addEventListener("drop", e => { e.preventDefault(); drop.classList.remove("drag"); upload(e.dataTransfer.files); });
    file.addEventListener("change", e => { upload(e.target.files); e.target.value = ""; });
    // transport
    $("#ed-play").onclick = () => { const v = $("#ed-frame video"); if (v) v.paused ? v.play() : v.pause(); };
    $("#ed-take-final").onclick = () => { ED.take = "final"; renderPreview(); };
    $("#ed-take-source").onclick = () => { ED.take = "source"; renderPreview(); };
    $("#ed-deliverbtn").onclick = () => setTool("deliver");

    await loadVoices();
    await VS.start();                       // single poller (library + jobs)
    VS.on("library", onLibrary);
    VS.on("jobs", () => { renderTracks(); renderGrid(); });
    VS.on("job-done", () => { VS.refreshLibrary(); });

    // spend chip
    try { const s = await VS.api("/api/spend"); $("#ed-spend").textContent = `fal ⛁ $${s.total.toFixed(2)}`; } catch (e) { /* */ }

    renderGrid();
    // deep link
    const q = new URLSearchParams(location.search);
    const want = q.get("v");
    if (want) {
      const v = VS.state.videos.find(x => x.name === want);
      if (v) select(v, q.get("tool") || "media");
    }
  })();
})();
