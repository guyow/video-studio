/* Step 3 — Dub & Lip-sync: single voice (local free / fal paid) or
   Interview mode (two speakers, both voices cloned, active-speaker sync). */
(function () {
  const VS = window.VS;
  // offline fallback only — real rates come from /api/prices (one source of truth)
  const SC = {latentsync: "~$0.20/40s", musetalk: "~$0.20/40s", veed: "~$0.40/min",
              hummingbird: "~$2.10/min", standard: "~$3/min", pro: "~$6/min",
              sync3: "~$8/min"};
  VS.api("/api/prices").then(p => {
    for (const [tier, perSec] of Object.entries(p.lipsync_per_sec || {})) {
      if (perSec > 0) SC[tier] = `~$${(perSec * 60).toFixed(2)}/min`;
    }
  }).catch(() => {});
  const TTSNAMES = {hd: "MiniMax 02-HD", turbo: "MiniMax 02-turbo",
                    hd25: "MiniMax 2.5-HD", turbo25: "MiniMax 2.5-turbo",
                    f5: "F5-TTS", chatterbox: "Chatterbox HD"};
  const NOCLONE = ["f5", "chatterbox"];   // zero-shot voices — no $1.50 clone fee

  function plan(el) {
    const engine = el.querySelector(".dub-eng.on").dataset.eng;
    if (engine === "local") {
      const lip = el.querySelector("#dub-llip").value;
      if (lip === "none" || lip.startsWith("wav2lip"))
        return {paid: false, engine,
          summary: "100% local & free — XTTS voice + " +
            (lip === "none" ? "no lip-sync" : "Wav2Lip" + (lip === "wav2lip-hd" ? " HD (GFPGAN)" : "")) + " on your GPU."};
      return {paid: true, engine,
        summary: `Voice is free/local; the ${lip} lip-sync is charged on fal.ai (${SC[lip]}).`};
    }
    const tts = el.querySelector("#dub-tts").value, tier = el.querySelector("#dub-tier").value;
    return {paid: true, engine,
      summary: `Cloud: ${TTSNAMES[tts] || tts} voice (${NOCLONE.includes(tts) ? "no clone fee" : "+$1.50 one-time clone"}) + ${tier} lip-sync (${SC[tier]}).`};
  }

  VS.registerStep({
    id: "dub",
    order: 3,
    title: "Dub & Lip-sync",

    status(v) {
      if (VS.runningFor(v, ["dub", "duo", "diarize"])) return "running";
      if (v.dub) return "done";
      if (v.script) return "ready";
      return "todo";
    },

    mount(el, ctx) {
      const v = ctx.video;
      el.innerHTML = `
        <div class="vs-panel">
          <h3>Mode</h3>
          <div class="vs-row">
            <button class="vs-btn dub-mode on" data-mode="single">🎙 Single voice</button>
            <button class="vs-btn dub-mode" data-mode="duo">🎤 Interview — two speakers</button>
          </div>
        </div>

        <div id="dub-single">
          <div class="vs-panel">
            <h3>Quality &amp; price — pick your engine</h3>
            <div class="vs-row" style="align-items:stretch">
              <label class="vs-radio dub-eng on" data-eng="local" style="flex:1;min-width:230px;display:block">
                <b>💻 Local — FREE</b><br>
                <span class="vs-hint">XTTS voice clone + Wav2Lip HD on your GPU.
                  Good for testing; softer mouth detail.</span><br>
                <span class="vs-free">$0 · ~15 min</span>
              </label>
              <label class="vs-radio dub-eng" data-eng="fal" style="flex:1;min-width:230px;display:block">
                <b>☁ Premium cloud — PAID</b><br>
                <span class="vs-hint">MiniMax HD voice + sync.so pro lip-sync — the pipeline
                  that made your winners. Best for the ad you'll actually run.</span><br>
                <span class="vs-paid">~$1.50 clone + ~$3/min · ~5 min</span>
              </label>
            </div>
            <div id="dub-local" class="vs-row">
              <label class="vs-hint">voice
                <select class="vs-sel" id="dub-voice">
                  <option value="">🎤 On-screen speaker (clone from this video)</option>
                </select>
                <a href="/voices" target="_blank" class="vs-hint" style="margin-left:6px">manage ↗</a></label>
              <label class="vs-hint">lip-sync
                <select class="vs-sel" id="dub-llip">
                  <option value="wav2lip-hd" selected>💸 CHEAPEST — Wav2Lip HD, local GPU (FREE)</option>
                  <option value="latentsync">⭐ BEST VALUE — LatentSync, near-best quality ~$0.20/40s 💰</option>
                  <option value="hummingbird">🐦 Hummingbird (Tavus) — photorealistic ~$2.10/min 💰</option>
                  <option value="pro">🏆 sync v2 PRO, studio-grade ~$6/min 💰</option>
                  <option value="sync3">👑 sync-3 — newest, most powerful ~$8/min 💰</option>
                  <option value="musetalk">MuseTalk — fal.ai, fast + balanced ~$0.20/40s 💰</option>
                  <option value="wav2lip">Wav2Lip — local GPU, faster but soft mouth (FREE)</option>
                  <option value="none">None — keep the original mouth (FREE)</option>
                  <option value="veed">VEED lipsync — fal.ai ~$0.40/min 💰</option>
                  <option value="standard">sync-lipsync v2 — fal.ai ~$3/min 💰</option>
                </select></label>
              <label class="vs-hint">language
                <select class="vs-sel" id="dub-lang">
                  <option value="en" selected>English</option><option value="es">Spanish</option>
                  <option value="de">German</option><option value="fr">French</option>
                  <option value="pt">Portuguese</option><option value="it">Italian</option>
                </select></label>
            </div>
            <div id="dub-fal" class="vs-row" style="display:none">
              <label class="vs-hint">voice
                <select class="vs-sel" id="dub-tts">
                  <option value="hd25" selected>⭐ MiniMax speech-2.5-HD — newest, best ($0.06/1k + $1.50 clone) 💰</option>
                  <option value="chatterbox">💸 Chatterbox HD — $0.04/1k, clones FREE from source 💰</option>
                  <option value="turbo25">MiniMax speech-2.5-turbo — $0.04/1k (+$1.50 clone) 💰</option>
                  <option value="hd">MiniMax speech-02-hd — proven ($0.10/1k + $1.50 clone) 💰</option>
                  <option value="turbo">MiniMax speech-02-turbo (+$1.50 one-time clone) 💰</option>
                  <option value="f5">F5-TTS — $0.05/1k chars, clones free from source 💰</option>
                </select></label>
              <label class="vs-hint">lip-sync tier
                <select class="vs-sel" id="dub-tier">
                  <option value="standard" selected>sync-lipsync v2 — ~$3/min 💰</option>
                  <option value="sync3">👑 sync-3 — newest, most powerful ~$8/min 💰</option>
                  <option value="pro">🏆 sync v2 PRO, studio-grade ~$6/min 💰</option>
                  <option value="hummingbird">🐦 Hummingbird (Tavus) — photorealistic ~$2.10/min 💰</option>
                  <option value="latentsync">⭐ BEST VALUE — LatentSync, near-best quality ~$0.20/40s 💰</option>
                  <option value="musetalk">MuseTalk — fast + balanced ~$0.20/40s 💰</option>
                  <option value="veed">VEED lipsync — ~$0.40/min 💰</option>
                </select></label>
            </div>
            <div class="vs-row">
              <button class="vs-btn primary" id="dub-run">🎙 Dub</button>
              <span class="vs-hint" id="dub-cost"></span>
            </div>
            <div class="vs-hint vs-err" id="dub-busy" style="margin-top:6px"></div>
          </div>
        </div>

        <div id="dub-duo" style="display:none">
          <div class="vs-panel">
            <h3>Who speaks when <span class="r vs-free">detection is free &amp; local</span></h3>
            <div class="vs-hint">Interviewer + guest? Detect the speakers, fix any wrong labels,
              then both voices get cloned and each face lip-syncs only during its own lines.</div>
            <div class="vs-row">
              <button class="vs-btn" id="duo-detect">🔍 Detect speakers</button>
              <span class="vs-hint" id="duo-hint"></span>
            </div>
            <div id="duo-editor" style="display:none">
              <div class="vs-row">
                <label class="vs-hint">Speaker A <input class="vs-in" id="duo-na" placeholder="Interviewer"></label>
                <label class="vs-hint">Speaker B <input class="vs-in" id="duo-nb" placeholder="Guest"></label>
              </div>
              <div id="duo-turns" style="max-height:320px;overflow:auto;margin-top:8px"></div>
              <div class="vs-row">
                <button class="vs-btn" id="duo-save">💾 Save turns</button>
                <select class="vs-sel" id="duo-tier">
                  <option value="pro" selected>sync v2 PRO — best multi-face</option>
                  <option value="standard">sync v2 standard</option>
                </select>
                <button class="vs-btn primary" id="duo-run">🎙 Dub interview (2 voices)</button>
              </div>
              <div class="vs-hint vs-paid" id="duo-cost" style="margin-top:6px"></div>
            </div>
          </div>
        </div>

        <div class="vs-panel" id="dub-resultwrap" style="display:none">
          <h3>Current dub</h3>
          <div class="vs-stage"><video controls playsinline id="dub-result"></video></div>
          <div class="vs-hint">Not happy? Fix it in the next step — takes are versioned, nothing is lost.</div>
        </div>`;

      // mode + engine toggles
      VS.$$(".dub-mode", el).forEach(b => b.onclick = () => {
        VS.$$(".dub-mode", el).forEach(x => x.classList.toggle("on", x === b));
        el.querySelector("#dub-single").style.display = b.dataset.mode === "single" ? "" : "none";
        el.querySelector("#dub-duo").style.display = b.dataset.mode === "duo" ? "" : "none";
        if (b.dataset.mode === "duo") loadDuo();
      });
      VS.$$(".dub-eng", el).forEach(b => b.onclick = () => {
        VS.$$(".dub-eng", el).forEach(x => {
          const on = x === b;
          x.classList.toggle("on", on);
          x.style.borderColor = on ? "var(--accent)" : "";
          x.style.background = on ? "rgba(124,92,255,.12)" : "";
        });
        el.querySelector("#dub-local").style.display = b.dataset.eng === "local" ? "" : "none";
        el.querySelector("#dub-fal").style.display = b.dataset.eng === "fal" ? "" : "none";
        updCost();
      });
      // highlight the default selection
      const defEng = el.querySelector(".dub-eng.on");
      defEng.style.borderColor = "var(--accent)";
      defEng.style.background = "rgba(124,92,255,.12)";
      const updCost = () => {
        const p = plan(el);
        const c = el.querySelector("#dub-cost");
        c.textContent = (p.paid ? "💰 " : "✓ ") + p.summary;
        c.style.color = p.paid ? "var(--warn)" : "var(--ok)";
      };
      ["dub-llip", "dub-tts", "dub-tier"].forEach(id =>
        el.querySelector("#" + id).addEventListener("change", updCost));
      updCost();

      // single dub
      el.querySelector("#dub-run").onclick = async () => {
        const p = plan(el);
        const body = {action: "dub", file: v.name, engine: p.engine};
        if (p.engine === "local") {
          body.lipsync = el.querySelector("#dub-llip").value;
          body.language = el.querySelector("#dub-lang").value;
          const vsel = el.querySelector("#dub-voice");
          if (vsel && vsel.value) body.voice_id = vsel.value;
        } else {
          body.tts = el.querySelector("#dub-tts").value;
          body.tier = el.querySelector("#dub-tier").value;
        }
        // ask the server for the real price first (402 → {estimate}); only send
        // confirm_cost after the user approves the actual dollar number
        try {
          const r = await VS.post("/api/run", body);
          VS.drawer.watch(r.job_id);
          VS.toast(p.paid ? "🎙 Cloud dub started" : "🎙 Free local dub started");
          return;
        } catch (e) {
          if (e.status !== 402 || !e.body || !e.body.estimate) {
            el.querySelector("#dub-busy").textContent =
              e.status === 409
                ? "⏳ another dub is already running — one at a time keeps the GPU alive. It'll free up soon."
                : "⚠ " + e.message;
            if (e.status !== 409) VS.toast("Couldn't start: " + e.message);
            return;
          }
          const est = e.body.estimate;
          const warn = est.warn ? "\n\n🚨 That's above the $5 warning threshold." : "";
          if (!confirm(`⚠ This run spends money on fal.ai:\n\n${est.summary}\n\n≈ $${est.this_run.toFixed(2)} for THIS video${warn}\n\nApprove and start? (Cancel = nothing charged)`)) {
            VS.toast("Cancelled — nothing charged");
            return;
          }
        }
        try {
          const r = await VS.post("/api/run", {...body, confirm_cost: true});
          VS.drawer.watch(r.job_id);
          VS.toast("🎙 Cloud dub started");
        } catch (e) {
          el.querySelector("#dub-busy").textContent =
            e.status === 409
              ? "⏳ another dub is already running — one at a time keeps the GPU alive. It'll free up soon."
              : "⚠ " + e.message;
          if (e.status !== 409) VS.toast("Couldn't start: " + e.message);
        }
      };

      // ── interview mode ──
      let duoCfg = null;
      const loadDuo = async () => {
        try {
          const d = await VS.api("/api/duo/config?file=" + encodeURIComponent(v.name));
          if (d.config) { duoCfg = d.config; renderTurns(); }
          el.querySelector("#duo-hint").textContent =
            d.config ? `✓ ${d.config.segments.length} turns` + (d.voices_cloned ? " · voices already cloned" : "")
                     : (d.has_transcript ? "" : "will transcribe first automatically");
        } catch (e) { /* none yet */ }
      };
      const renderTurns = () => {
        el.querySelector("#duo-editor").style.display = "";
        el.querySelector("#duo-na").value = duoCfg.speakers?.A?.label || "Speaker A";
        el.querySelector("#duo-nb").value = duoCfg.speakers?.B?.label || "Speaker B";
        const box = el.querySelector("#duo-turns");
        box.innerHTML = "";
        duoCfg.segments.forEach(s => {
          const win = s.end - s.start, lo = Math.round(win * 2.3), hi = Math.round(win * 3.0);
          const row = document.createElement("div");
          row.className = "vs-row";
          row.style.cssText = "border:1px solid var(--line);border-radius:10px;padding:7px 9px;margin-top:6px;flex-wrap:nowrap";
          row.innerHTML = `
            <button class="vs-btn" style="width:38px;padding:5px;background:${s.speaker === "A" ? "#7c5cff" : "#e8863a"};border:none;color:#fff">${s.speaker}</button>
            <span class="vs-hint" style="cursor:pointer;flex:none;width:82px;font-family:var(--mono);font-size:11px">${VS.fmtT(s.start)}–${VS.fmtT(s.end)}</span>
            <input class="vs-in" style="flex:1;min-width:0" value="">
            <span class="vs-hint" style="flex:none;width:58px;text-align:right;font-size:10.5px"></span>`;
          const [chip, tm, , wc] = [row.children[0], row.children[1], row.children[2], row.children[3]];
          const txt = row.children[2];
          txt.value = s.text;
          const upd = () => {
            const n = txt.value.trim().split(/\s+/).filter(Boolean).length;
            wc.textContent = `${n}/${lo}-${hi}w`;
            wc.style.color = (n < lo - 2 || n > hi + 2) ? "var(--warn)" : "var(--dim)";
          };
          upd();
          chip.onclick = () => {
            s.speaker = s.speaker === "A" ? "B" : "A";
            chip.textContent = s.speaker;
            chip.style.background = s.speaker === "A" ? "#7c5cff" : "#e8863a";
          };
          txt.oninput = () => { s.text = txt.value; upd(); };
          tm.onclick = () => {
            const vid = el.querySelector("#dub-result") || null;
            VS.toast(`turn starts at ${VS.fmtT(s.start)}`);
            if (vid && vid.src) { vid.currentTime = s.start; vid.play(); }
          };
          box.appendChild(row);
        });
      };
      el.querySelector("#duo-detect").onclick = async () => {
        el.querySelector("#duo-detect").disabled = true;
        el.querySelector("#duo-hint").textContent = "⏳ listening to both voices…";
        try {
          const r = await VS.post("/api/duo/diarize", {file: v.name});
          VS.drawer.watch(r.job_id);
          if (ctx._duoOff) ctx._duoOff();   // don't stack subscriptions on repeat detects
          ctx._duoOff = VS.on("drawer-finished", async j => {
            if (j.id !== r.job_id) return;
            if (ctx._duoOff) { ctx._duoOff(); ctx._duoOff = null; }
            el.querySelector("#duo-detect").disabled = false;
            if (j.status === "done") { await loadDuo(); VS.toast("Speakers detected — review the turns"); }
            else el.querySelector("#duo-hint").textContent = "❌ failed — see the log";
          });
        } catch (e) {
          VS.toast("Error: " + e.message);
          el.querySelector("#duo-detect").disabled = false;
        }
      };
      const saveDuo = async () => {
        if (!duoCfg) return;
        duoCfg.speakers.A.label = el.querySelector("#duo-na").value.trim() || "Speaker A";
        duoCfg.speakers.B.label = el.querySelector("#duo-nb").value.trim() || "Speaker B";
        await VS.post("/api/duo/config", {file: v.name, config: duoCfg});
      };
      el.querySelector("#duo-save").onclick = async () => {
        try { await saveDuo(); VS.toast("Turns saved"); }
        catch (e) { VS.toast("Error: " + e.message); }
      };
      el.querySelector("#duo-run").onclick = async () => {
        if (!duoCfg) { VS.toast("Detect speakers first"); return; }
        try { await saveDuo(); } catch (e) { VS.toast("Error: " + e.message); return; }
        const tier = el.querySelector("#duo-tier").value;
        let est = null;
        try {
          const d = await VS.post("/api/duo/run", {file: v.name, tier});
          VS.drawer.watch(d.job_id);   // no confirm needed (shouldn't happen — server 402s)
          return;
        } catch (e) {
          if (e.status === 402 && e.body) est = e.body.estimate;
          else { VS.toast("Error: " + e.message); return; }
        }
        el.querySelector("#duo-cost").textContent =
          `Estimated $${est.this_run.toFixed(2)} — ${est.summary}`;
        if (!confirm(`⚠ Interview dub spends money on fal.ai:\n\n${est.summary}\n\n≈ $${est.this_run.toFixed(2)}${est.warn ? "\n\n🚨 That's above the $5 warning threshold." : ""}\n\nApprove and start?`)) {
          VS.toast("Cancelled — nothing charged");
          return;
        }
        try {
          const d = await VS.post("/api/duo/run", {file: v.name, tier, confirm_cost: true});
          VS.drawer.watch(d.job_id);
          VS.toast("🎙 Interview dub started — cloning both voices");
        } catch (e) { VS.toast("Error: " + e.message); }
      };

      // populate the Voice Bank picker (local path)
      VS.api("/api/voices").then(d => {
        const sel = el.querySelector("#dub-voice");
        if (!sel) return;
        if (!d.voices || !d.voices.length) {
          const o = document.createElement("option");
          o.value = ""; o.disabled = true;
          o.textContent = "— no saved voices yet (add in Voices tab) —";
          sel.appendChild(o);
          return;
        }
        d.voices.forEach(vo => {
          const o = document.createElement("option");
          o.value = vo.id;
          o.textContent = "🎙 " + vo.name;
          sel.appendChild(o);
        });
      }).catch(() => {});

      if (v.duo) loadDuo();
      this.sync(ctx);
    },

    sync(ctx) {
      const v = ctx.video;
      const el = ctx.el;
      const wrap = el.querySelector("#dub-resultwrap");
      if (!wrap) return;
      const vid = el.querySelector("#dub-result");
      if (v.dub) {
        wrap.style.display = "";
        const want = "/media/" + v.dub + "?v=" + (v.dub_mtime || "");
        if (!vid.src.endsWith(encodeURI(want))) vid.src = want;
      } else wrap.style.display = "none";
      const busy = el.querySelector("#dub-busy");
      const run = VS.runningFor(v, ["dub", "duo"]);
      el.querySelector("#dub-run").disabled = !!run || !v.script;
      el.querySelector("#duo-run").disabled = !!run;
      if (!run && busy) busy.textContent = v.script ? "" : "save a script first — the Dub needs words to speak";
    },

    unmount(ctx) {   // drop the diarize subscription so it can't fire on a torn-down DOM
      if (ctx._duoOff) { ctx._duoOff(); ctx._duoOff = null; }
    },
  });
})();
