/* editor-app.js v2 — pro editor wired to the real Video Studio API.
   Reuses vs-core.js (VS.api/post/toast + the single poller + pub-sub).
   Pro touches: duration-scaled timeline with click-to-seek + live playhead,
   real filmstrip frames, panel toggles, autosave chip, floating Advisor. */
(function () {
  const VS = window.VS;
  const $ = (s, r) => (r || document).querySelector(s);
  const $$ = (s, r) => [...(r || document).querySelectorAll(s)];

  const ED = {
    video: null, tool: "media", take: "final", ratio: "9:16", fit: "contain",
    voices: [], filter: "all", search: "",
    dur: 0, pxs: 100,             // timeline: seconds + px-per-second (zoom)
    filmKey: null,                // cache key of the loaded filmstrip
  };
  const HEADW = 118;              // timeline lane-head width (matches CSS)

  /* ── autosave chip ── */
  function savedNow() {
    $("#ed-savetime").textContent = "Auto saved: " + new Date().toLocaleTimeString();
  }
  const _post = VS.post;
  VS.post = async (p, b) => { const r = await _post(p, b); savedNow(); return r; };

  /* ═════════ tools ═════════ */
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
  const SUBNAV = {
    media:  [["all", "All"], ["dubbed", "Dubbed"], ["delivered", "Delivered"], ["drafts", "Drafts"]],
    voices: [["bank", "Bank"]],
  };

  function renderTabs() {
    $("#ed-tabs").innerHTML = TOOLS.map(t =>
      `<button class="ed-tab ${ED.tool === t.id ? "on" : ""}" data-t="${t.id}">
         <span class="ic">${t.ic}</span>${t.label}</button>`).join("");
    $$(".ed-tab").forEach(b => b.onclick = () => setTool(b.dataset.t));
  }

  function renderSubnav() {
    const items = SUBNAV[ED.tool] || [["panel", "Panel"]];
    $("#ed-subnav").innerHTML = items.map(([k, lab], i) =>
      `<button class="sn ${(ED.tool === "media" ? ED.filter === k : i === 0) ? "on" : ""}" data-k="${k}">${lab}</button>`).join("");
    $$("#ed-subnav .sn").forEach(b => b.onclick = () => {
      if (ED.tool === "media") { ED.filter = b.dataset.k; renderAssets(); renderSubnav(); }
    });
  }

  function setTool(id) {
    ED.tool = id;
    renderTabs(); renderSubnav(); renderAssets(); renderInspector();
    syncURL();
  }

  /* ═════════ browser assets ═════════ */
  function renderAssets() {
    const box = $("#ed-assets");
    if (ED.tool === "voices") {
      box.innerHTML = ED.voices.length
        ? `<div class="ed-grid" style="grid-template-columns:1fr">` + ED.voices.map(v =>
            `<div class="ed-card" style="padding:9px 11px"><b style="font-size:12px">🎙 ${VS.esc(v.name)}</b>
             ${v.sample ? `<audio controls preload="none" src="/media/${v.sample}" style="width:100%;margin-top:6px"></audio>` : ""}
             <div class="nm" style="padding:4px 0 0">from ${VS.esc(v.source || "?")}</div></div>`).join("") + "</div>"
        : '<div class="ed-empty">no saved voices — clone one from the Voices inspector</div>';
      return;
    }
    // media grid
    const q = ED.search.toLowerCase();
    let vids = VS.state.videos.filter(v =>
      !q || v.name.toLowerCase().includes(q) || (v.title || "").toLowerCase().includes(q));
    if (ED.filter === "dubbed") vids = vids.filter(v => v.dub);
    else if (ED.filter === "delivered") vids = vids.filter(v => v.exported);
    else if (ED.filter === "drafts") vids = vids.filter(v => !v.dub);
    if (!vids.length) { box.innerHTML = '<div class="ed-empty">nothing here — import a video above</div>'; return; }
    box.innerHTML = `<div class="ed-grid">` + vids.map(v => {
      const run = VS.runningFor(v);
      const badge = run ? `<span class="bdg run">⏳ ${VS.esc(run.action)}</span>`
        : v.exported ? '<span class="bdg ok">DELIVERED</span>'
        : v.dub ? '<span class="bdg" style="color:var(--cy)">DUBBED</span>' : "";
      return `<div class="ed-card ${ED.video && ED.video.name === v.name ? "sel" : ""}" data-n="${VS.esc(v.name)}">
        <div class="th"><img loading="lazy" src="/api/thumb/${encodeURIComponent(v.name)}" alt="">${badge}</div>
        <div class="nm">${VS.esc(v.title || v.name)}</div></div>`;
    }).join("") + "</div>";
    $$(".ed-card[data-n]").forEach(el => el.onclick = () => {
      const v = VS.state.videos.find(x => x.name === el.dataset.n);
      if (v) select(v);
    });
  }

  function select(v, tool) {
    ED.video = v;
    ED.take = v.dub ? "final" : "source";
    ED.filmKey = null;
    $("#ed-title").textContent = (v.title || v.name) + " — Video Studio";
    $("#ed-pname").textContent = "— " + (v.title || v.name);
    $("#ed-deliverbtn").disabled = !v.dub;
    renderAssets(); renderPreview(); renderTimeline();
    setTool(tool || ED.tool);
  }

  /* ═════════ player ═════════ */
  function previewSrc() {
    const v = ED.video;
    if (!v) return null;
    if (ED.take === "final" && v.dub) return "/media/" + v.dub + "?v=" + (v.dub_mtime || "");
    return "/media/uploads/" + encodeURIComponent(v.name);
  }
  const fmtT = s => {
    if (!isFinite(s)) return "00:00.0";
    return String(Math.floor(s / 60)).padStart(2, "0") + ":" +
      (s % 60).toFixed(1).padStart(4, "0");
  };

  function playerVideo() { return $("#ed-frame video"); }

  function renderPreview() {
    const f = $("#ed-frame");
    f.className = "ed-frame" + (ED.ratio === "16:9" ? " wide" : ED.ratio === "1:1" ? " square" : "");
    const src = previewSrc();
    if (!src) { f.innerHTML = '<div class="ph">← pick a video from the media panel</div>'; return; }
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

  /* ═════════ timeline ═════════ */
  function tlWidth() { return Math.max(400, ED.dur * ED.pxs); }

  function renderTimeline() {
    const v = ED.video;
    const ruler = $("#tl-ruler"), lanes = $("#tl-lanes"), inner = $("#tl-inner");
    if (!v) {
      ruler.innerHTML = ""; lanes.innerHTML =
        '<div class="ed-empty" style="padding:26px">the pipeline timeline appears when you pick a video</div>';
      return;
    }
    const W = tlWidth();
    inner.style.width = (HEADW + W + 40) + "px";

    // ruler ticks — adaptive step
    const step = ED.pxs >= 180 ? 1 : ED.pxs >= 90 ? 2 : 5;
    let ticks = "";
    for (let t = 0; t <= Math.max(1, ED.dur); t += step) {
      ticks += `<div class="tick" style="left:${HEADW + t * ED.pxs}px">${t}s</div>`;
    }
    ruler.innerHTML = ticks;

    const jobs = VS.jobsFor(v).filter(j => j.status === "running");
    const jb = a => jobs.find(j => a.includes(j.action));
    const pct = j => (j && j.progress && j.progress.pct != null) ? j.progress.pct + "%" : "running…";
    const bar = (cls, x, w, text, st) =>
      `<div class="ed-clipbar ${cls}" style="left:${HEADW + x}px;width:${w}px">${text}${st ? `<span class="st">${st}</span>` : ""}</div>`;
    const lane = (icons, name, body) =>
      `<div class="ed-lane"><div class="head">${icons.map(i => `<span class="ic">${i}</span>`).join("")}
        <span class="nm">${name}</span></div><div class="body" data-seek="1">${body}</div></div>`;

    // FX lane — a running job spans the full width (CapCut's purple effect band)
    const running = jobs[0];
    const fxBody = running
      ? bar("effect", 0, W, "⚙ " + VS.esc(running.label || running.action), pct(running))
      : bar("ghost", 0, W, "no job running");
    // VIDEO lane — filmstrip
    const filmBody = `<div class="ed-film" id="tl-film" style="left:${HEADW}px;width:${W}px">
        <div class="cliplabel">${VS.esc(v.name)} · ${ED.dur ? ED.dur.toFixed(1) + "s" : ""} ${v.cleaned ? "· subtitles erased ✓" : ""}</div>
      </div>`;
    // VOICE lane
    const dubJob = jb(["dub", "duo", "diarize"]);
    const voiceBody = dubJob ? bar("running", 0, W, "🎙 " + VS.esc(dubJob.action), pct(dubJob))
      : v.dub ? bar("voice", 0, W, "🎙 dubbed voice", "✓ final.mp4")
      : bar("ghost", 0, W, v.script ? "ready — run the Dub tool" : "needs a script first");
    // CAPTIONS lane
    const capJob = jb(["caption", "recaption"]);
    const capsBody = capJob ? bar("running", 0, W, "💬 burning captions", pct(capJob))
      : v.captioned ? bar("caps", 0, W, "💬 captions burned", "✓")
      : bar("ghost", 0, W, v.dub ? "ready — run the Captions tool" : "waiting for a dub");

    lanes.innerHTML =
      lane(["✦", "🔒", "👁"], "FX/JOB", fxBody) +
      lane(["🎬", "🔒", "👁"], "VIDEO", filmBody) +
      lane(["🔊", "🔒", "👁"], "VOICE", voiceBody) +
      lane(["💬", "🔒", "👁"], "CAPTIONS", capsBody);

    $("#tl-jobnote").textContent = running ? `⏳ ${running.label || running.action}` : "";
    loadFilmstrip();
    // click-to-seek on ruler + lane bodies
    const seek = e => {
      const rect = inner.getBoundingClientRect();
      const x = e.clientX - rect.left - HEADW + $("#tl-scroll").scrollLeft * 0;
      const t = Math.max(0, Math.min(ED.dur, (e.clientX - rect.left - HEADW) / ED.pxs));
      const vid = playerVideo();
      if (vid && isFinite(t)) { vid.currentTime = t; movePlayhead(t); }
    };
    ruler.onclick = seek;
    $$("#tl-lanes .body").forEach(b => b.onclick = seek);
  }

  function movePlayhead(t) {
    $("#tl-playhead").style.left = (HEADW + (t || 0) * ED.pxs) + "px";
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
      if (ED.filmKey !== key) return;                   // stale
      const imgs = (d.frames || []).map(f =>
        `<img src="/media/${f.path.replace(/^\/+/, "")}" alt="">`).join("");
      holder.insertAdjacentHTML("beforeend", imgs);
    } catch (e) { /* frames unavailable — label-only strip is fine */ }
  }

  /* ═════════ inspector panels ═════════ */
  const P = {};
  const need = (cap, msg) => { $("#ed-icap").textContent = cap;
    return `<div class="ed-note">${msg || "Pick a video in the media panel first."}</div>`; };
  const rows = pairs => pairs.map(([k, v]) =>
    `<div class="ed-row"><span class="k">${k}</span><span class="v">${v}</span></div>`).join("");

  P.media = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Details";
    if (!v) return need("Details");
    return rows([
      ["Name:", `<input type="text" id="mi-title" value="${VS.esc(v.title || "")}" placeholder="${VS.esc(v.name)}" style="width:100%;background:var(--bg2);border:1px solid var(--line);border-radius:6px;color:var(--text);padding:5px 8px;font-size:12px">`],
      ["File:", VS.esc(v.name)],
      ["Size:", VS.fmtSize(v.size)],
      ["Character:", `<input type="text" id="mi-char" value="${VS.esc(v.character || "")}" placeholder="who's in it" style="width:100%;background:var(--bg2);border:1px solid var(--line);border-radius:6px;color:var(--text);padding:5px 8px;font-size:12px">`],
      ["Tags:", `<input type="text" id="mi-tags" value="${VS.esc((v.tags || []).join(", "))}" placeholder="comma, separated" style="width:100%;background:var(--bg2);border:1px solid var(--line);border-radius:6px;color:var(--text);padding:5px 8px;font-size:12px">`],
    ]) + `<div class="ed-hr"></div>` + rows([
      ["Transcribed:", v.transcript ? "✓ yes" : "not yet"],
      ["Clean:", v.cleaned ? "✓ subtitles erased" : (v.no_subs ? "✓ no burned subs" : "not cleaned")],
      ["Dub:", v.dub ? "✓ final.mp4" : "—"],
      ["Captions:", v.captioned ? "✓ burned" : "—"],
      ["Delivered:", v.exported ? "✓ on Desktop" : "—"],
    ]) + `<button class="ed-run ghostly" id="mi-save" style="margin-top:12px">💾 Save details</button>`;
  };
  P.media.bind = el => {
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
    if (!v) return need("Clean");
    const run = VS.runningFor(v, ["clean-subs"]);
    return `<div class="ed-sec"><div class="shead">Burned-in subtitles</div>
      <div class="ed-note" style="margin:0 0 8px">Erase old captions from the pixels before dubbing — free, on your GPU.</div>
      ${run ? `<div class="ed-msg">⏳ erasing… watch the FX lane below</div>` : ""}
      <button class="ed-run" id="cl-erase" ${run ? "disabled" : ""}>🧹 ${v.cleaned ? "Re-erase" : "Erase burned-in subtitles"}</button>
      ${v.cleaned ? `<button class="ed-run ghostly" id="cl-restore">↩ Restore original</button>` : ""}
      <div class="ed-field"><label style="display:flex;gap:6px;align-items:center">
        <input type="checkbox" id="cl-nosubs" ${v.no_subs ? "checked" : ""}> this video has no burned subtitles</label></div></div>
      <div class="ed-hr"></div>
      <div class="ed-sec"><div class="shead">Transcribe</div>
      <div class="ed-note" style="margin:0 0 8px">${v.transcript ? "✓ transcribed — the Script tool has the words." : "Needed before Script."}</div>
      <button class="ed-run ghostly" id="cl-transcribe">${v.transcript ? "↻ Re-transcribe" : "▶ Transcribe"}</button></div>`;
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
    if (!v) return need("Script");
    return `<div class="ed-note" style="margin-bottom:8px">${v.orig_words ? `Original spoke ~${v.orig_words} words — stay close so the lips fit.` : "Write or load the words they'll say."}</div>
      <div class="ed-field"><textarea id="sc-text" rows="10" placeholder="loading…"></textarea></div>
      <div class="ed-field"><input type="text" id="sc-steer" placeholder="✨ AI rewrite — e.g. punchier hook, same length"></div>
      <button class="ed-run ghostly" id="sc-ai">✨ Rewrite with AI</button>
      <button class="ed-run" id="sc-save">💾 Save script</button>
      <div class="ed-cost free" id="sc-count"></div>`;
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
        VS.toast("Script saved — Dub unlocked"); VS.refreshLibrary(); renderTimeline(); }
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
    $("#ed-icap").textContent = "Dub & Lip-sync";
    if (!v) return need("Dub & Lip-sync");
    const run = VS.runningFor(v, ["dub", "duo"]);
    const voiceOpts = ['<option value="">🎤 On-screen speaker (clone from this video)</option>']
      .concat(ED.voices.map(vo => `<option value="${VS.esc(vo.id)}">🎙 ${VS.esc(vo.name)}</option>`)).join("");
    return `
      <div class="ed-itabs"><button class="ed-itab on" data-it="basic">Basic</button>
        <button class="ed-itab" data-it="adv">Advanced</button></div>
      <div data-pane="basic" style="margin-top:12px">
        <div class="ed-opt on" data-eng="local"><div class="t">💻 Local — FREE<span class="price" style="color:var(--ok)">$0 · ~15m</span></div>
          <div class="d">XTTS + Wav2Lip HD on your GPU. Great for testing.</div></div>
        <div class="ed-opt" data-eng="fal"><div class="t">☁ Premium cloud — PAID<span class="price" style="color:var(--warn)">~$1.50 + $3/min</span></div>
          <div class="d">MiniMax HD voice + sync.so pro — the winner pipeline.</div></div>
        <div class="ed-field"><label>Voice</label><select id="du-voice">${voiceOpts}</select></div>
        <div class="ed-field" id="du-localrow"><label>Lip-sync</label><select id="du-lip">
          <option value="wav2lip-hd" selected>Wav2Lip HD (GFPGAN, free)</option>
          <option value="wav2lip">Wav2Lip (faster, softer)</option>
          <option value="none">voice only (silent / generated clips)</option></select></div>
        <div class="ed-field" id="du-falrow" style="display:none"><label>Cloud voice · lip-sync tier</label>
          <select id="du-tts"><option value="hd" selected>MiniMax HD</option><option value="turbo">MiniMax turbo</option><option value="f5">F5 (no clone fee)</option></select>
          <select id="du-tier" style="margin-top:6px"><option value="standard" selected>sync v2 standard</option><option value="pro">sync v2 pro (best)</option><option value="veed">veed (cheap)</option><option value="latentsync">latentsync (cheapest)</option></select></div>
      </div>
      <div data-pane="adv" style="display:none;margin-top:12px">
        <div class="ed-field"><label>Language</label><select id="du-lang">
          <option value="en" selected>English</option><option value="es">Spanish</option>
          <option value="de">German</option><option value="fr">French</option>
          <option value="pt">Portuguese</option><option value="it">Italian</option></select></div>
        <div class="ed-param"><span class="pl">Keep original audio (music bleed)</span>
          <input type="range" id="du-keep" min="0" max="60" value="0">
          <input class="num" id="du-keepn" value="0"></div>
        <div class="ed-note">Two speakers? <a href="/?v=${encodeURIComponent(v.name)}&step=dub">Interview mode ↗</a> (classic view) handles who-says-what.</div>
      </div>
      ${run ? `<div class="ed-msg">⏳ ${VS.esc(run.label || "dub running")} — one dub at a time</div>` : ""}
      <button class="ed-run" id="du-run" ${run || !v.script ? "disabled" : ""}>🎙 Dub</button>
      <div class="ed-cost free" id="du-cost">✓ 100% local — nothing is charged</div>
      ${!v.script ? '<div class="ed-msg">save a Script first — the dub needs words</div>' : ""}`;
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
      if (engine === "fal") { c.textContent = "💰 voice + lip-sync charged on fal.ai — you approve first"; c.className = "ed-cost paid"; }
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
      try {
        await VS.post("/api/run", body);
        VS.toast(engine === "local" ? "🎙 Free local dub started" : "🎙 Cloud dub started");
        renderTimeline(); renderInspector();
      } catch (e) { VS.toast("Couldn't start: " + e.message); }
    };
  };

  P.voices = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Voice Bank";
    return `<div class="ed-note" style="margin-bottom:10px">Clone once, reuse on any character. Saved voices appear in the media panel and in the Dub voice picker.</div>
      ${v ? `<button class="ed-run" id="vo-add">➕ Clone the voice from “${VS.esc(v.title || v.name)}”</button>` : '<div class="ed-note">pick a video to clone its voice</div>'}
      <div class="ed-note"><a href="/voices">manage the full Voice Bank ↗</a></div>`;
  };
  P.voices.bind = el => {
    const v = ED.video;
    const b = $("#vo-add", el);
    if (b) b.onclick = async () => {
      b.disabled = true; b.textContent = "🎧 cloning… (a few seconds)";
      try { await VS.post("/api/voices/create", {file: v.name, name: v.character || v.title || v.stem});
        await loadVoices(); VS.toast("Voice saved"); renderAssets(); renderInspector(); }
      catch (e) { VS.toast("Error: " + e.message); b.disabled = false; b.textContent = "➕ Clone this voice"; }
    };
  };

  P.fix = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Fix & QA";
    if (!v) return need("Fix & QA");
    if (!v.dub) return `<div class="ed-note">Run a Dub first — repairs work on the dubbed video.</div>`;
    return `<div class="ed-note" style="margin-bottom:8px">Describe what looks wrong — the AI watches the video and picks the repair. Every fix is a new take.</div>
      <div class="ed-field"><input type="text" id="fx-txt" placeholder="e.g. the cup warps when he drinks"></div>
      <button class="ed-run" id="fx-advise">🪄 Analyze</button>
      <div id="fx-out"></div>
      <div class="ed-hr"></div>
      <div class="ed-sec"><div class="shead">Quick repairs</div>
      <button class="ed-run ghostly" data-fix="relipsync">👄 Re-lip-sync (local)</button>
      <button class="ed-run ghostly" data-fix="renorm">🔊 Fix loudness</button>
      <button class="ed-run ghostly" data-fix="remux">📦 Remux voice onto source</button></div>
      <div class="ed-note">deep tools: <a href="/dubsync-lab" target="_blank">DubSync lab ↗</a> · frames: <a href="/qc-lab" target="_blank">QC lab ↗</a></div>`;
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

  P.captions = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Captions";
    if (!v) return need("Captions");
    const run = VS.runningFor(v, ["caption", "recaption"]);
    return `<div class="ed-note" style="margin-bottom:8px">Bold, word-timed captions burned over the old band.</div>
      ${run ? '<div class="ed-msg">⏳ burning… watch the FX lane</div>' : ""}
      <button class="ed-run" id="cp-dub" ${!v.dub || run ? "disabled" : ""}>🔥 Caption the dubbed video</button>
      <button class="ed-run ghostly" id="cp-orig" ${run ? "disabled" : ""}>Caption the original instead</button>
      ${v.captioned ? `<div class="ed-note">✓ captioned — <a href="/captioned/${encodeURIComponent(v.stem)}" target="_blank">watch ↗</a> · edit lines in the <a href="/?v=${encodeURIComponent(v.name)}&step=captions">classic view ↗</a></div>` : ""}`;
  };
  P.captions.bind = el => {
    const v = ED.video; if (!v) return;
    $("#cp-dub", el).onclick = async () => {
      try { await VS.post("/api/run", {action: "caption", file: v.stem}); VS.toast("🔥 Captioning…"); renderTimeline(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
    $("#cp-orig", el).onclick = async () => {
      try { await VS.post("/api/recaption", {path: "uploads/" + v.name, mode: "captions"}); VS.toast("🔥 Captioning original…"); renderTimeline(); }
      catch (e) { VS.toast("Error: " + e.message); }
    };
  };

  P.i2v = () => { $("#ed-icap").textContent = "Image → Video";
    return `<div class="ed-note" style="margin-bottom:10px">Upload a picture + a motion prompt → a ~30s clip (fal.ai, cost-gated). Finished clips land in the media panel.</div>
    <a class="ed-run" style="display:block;text-align:center;text-decoration:none" href="/image-to-video">🎬 Open Image→Video studio ↗</a>`; };

  P.clone = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Clone Winner";
    if (!v || !v.dub) return `<div class="ed-note">Pick a dubbed video first — then multiply it with a fresh similar script.</div>`;
    return `<div class="ed-note" style="margin-bottom:10px">Same winning structure, fresh words — same or a different actor.</div>
      <a class="ed-run" style="display:block;text-align:center;text-decoration:none"
        href="/?v=${encodeURIComponent(v.name)}&step=deliver">🏆 Open the Clone panel ↗</a>`;
  };

  P.deliver = () => {
    const v = ED.video;
    $("#ed-icap").textContent = "Deliver";
    if (!v) return need("Deliver");
    return `<div class="ed-note" style="margin-bottom:8px">Copies the finished video to your Desktop export folder.</div>
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
  };

  function renderInspector() {
    const el = $("#ed-ibody");
    const p = P[ED.tool] || P.media;
    el.innerHTML = p();
    if (p.bind) { try { p.bind(el); } catch (e) { console.error(e); } }
  }

  /* ═════════ upload ═════════ */
  function upload(files) {
    for (const f of files) {
      const fd = new FormData(); fd.append("file", f);
      const x = new XMLHttpRequest();
      x.open("POST", "/api/upload");
      x.onload = async () => {
        if (x.status === 200) {
          const res = JSON.parse(x.responseText);
          VS.toast(`Uploaded ${res.name} — transcribing…`);
          savedNow();
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

  /* ═════════ glue ═════════ */
  function syncURL() {
    const q = ED.video ? `?v=${encodeURIComponent(ED.video.name)}&tool=${ED.tool}` : "";
    history.replaceState(null, "", "/editor" + q);
  }
  async function loadVoices() {
    try { ED.voices = (await VS.api("/api/voices")).voices || []; } catch (e) { ED.voices = []; }
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
    renderAssets(); renderTimeline();
  }

  (async () => {
    renderTabs(); renderSubnav();

    // top bar
    const mb = $("#ed-menubtn"), ml = $("#ed-menulist");
    mb.onclick = e => { e.stopPropagation(); ml.classList.toggle("open"); };
    document.addEventListener("click", () => ml.classList.remove("open"));
    $("#ed-toggle-left").onclick = () => { $("#ed-main").classList.toggle("no-left");
      $("#ed-toggle-left").classList.toggle("off"); };
    $("#ed-toggle-right").onclick = () => { $("#ed-main").classList.toggle("no-right");
      $("#ed-toggle-right").classList.toggle("off"); };
    $("#ed-deliverbtn").onclick = () => setTool("deliver");

    // browser
    $("#ed-import").onclick = () => $("#ed-file").click();
    $("#ed-file").addEventListener("change", e => { upload(e.target.files); e.target.value = ""; });
    const assets = $("#ed-assets");
    assets.addEventListener("dragover", e => e.preventDefault());
    assets.addEventListener("drop", e => { e.preventDefault(); upload(e.dataTransfer.files); });
    $("#ed-search").addEventListener("input", e => { ED.search = e.target.value; renderAssets(); });
    const openFix = () => { if (ED.video) { setTool("fix");
      const t = $("#fx-txt"); if (t) t.focus(); } else VS.toast("Pick a video first"); };
    $("#ed-strip-btn").onclick = openFix;
    $("#ed-pilot").onclick = openFix;

    // player controls
    const togglePlay = () => { const v = playerVideo(); if (v) v.paused ? v.play() : v.pause(); };
    $("#ed-play").onclick = togglePlay;
    $("#tl-play").onclick = togglePlay;
    $("#tl-tostart").onclick = () => { const v = playerVideo(); if (v) { v.currentTime = 0; movePlayhead(0); } };
    $("#ed-fs").onclick = () => { const v = playerVideo(); if (v && v.requestFullscreen) v.requestFullscreen(); };
    $("#ed-fit").onclick = () => { ED.fit = ED.fit === "contain" ? "cover" : "contain";
      $("#ed-fit").classList.toggle("on", ED.fit === "cover"); renderPreview(); };
    $("#ed-ratio").onclick = () => {
      ED.ratio = ED.ratio === "9:16" ? "1:1" : ED.ratio === "1:1" ? "16:9" : "9:16";
      renderPreview();
    };
    $("#ed-take").onclick = () => { ED.take = ED.take === "final" ? "source" : "final";
      ED.filmKey = null; renderPreview(); renderTimeline(); };
    $("#ed-pmenu").onclick = () => { const s = previewSrc(); if (s) window.open(s, "_blank"); };
    document.addEventListener("keydown", e => {
      if (e.code === "Space" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
        e.preventDefault(); togglePlay();
      }
    });

    // timeline controls
    $("#tl-zoom").addEventListener("input", e => { ED.pxs = +e.target.value; renderTimeline();
      const v = playerVideo(); if (v) movePlayhead(v.currentTime); });
    $("#tl-refresh").onclick = () => { ED.filmKey = null; VS.refreshLibrary(); renderTimeline(); };

    await loadVoices();
    await VS.start();
    VS.on("library", onLibrary);
    VS.on("jobs", () => { renderTimeline(); renderAssets(); });
    VS.on("job-done", () => { VS.refreshLibrary(); loadVoices(); });

    try { const s = await VS.api("/api/spend"); $("#ed-spend").textContent = `fal ⛁ $${s.total.toFixed(2)}`; } catch (e) { /* */ }

    renderAssets();
    savedNow();
    const q = new URLSearchParams(location.search);
    const want = q.get("v");
    if (want) {
      const v = VS.state.videos.find(x => x.name === want);
      if (v) select(v, q.get("tool") || "media");
    }
  })();
})();
