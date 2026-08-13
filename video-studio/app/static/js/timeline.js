/* timeline.js — the sequence editor.
 *
 * Two things carry the weight here:
 *
 * 1. PLAYBACK. A pool of two <video> elements. The active one plays the current
 *    clip while the other sits pre-loaded and pre-seeked on the next clip; at a
 *    boundary we swap which is visible. That swap is what makes a multi-clip
 *    timeline play without a black flash between clips. The timeline clock is
 *    derived from the active element's currentTime, except across gaps where
 *    there is no video to read, so a wall clock takes over.
 *
 * 2. EDITS ARE SERVER-SIDE. The browser sends intents ("split c3 at 4.2") to
 *    /api/seq/<slug>/op and re-renders from the returned document. Dragging is
 *    optimistic locally, but the commit happens on release. One implementation
 *    of ripple/trim/split (sequence.py), not two that drift.
 */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return [].slice.call((r || document).querySelectorAll(s)); };
  var HEAD = 112;                      // must match --head in timeline.css

  var S = {
    slug: null, doc: null, dur: 0, sources: {},
    t: 0, playing: false, sel: null,
    pxs: 60, media: [], proxies: {}, filter: "",
    dragging: null, saving: 0, rafId: 0, wallLast: 0,
    activeClip: null, curEl: "A", audios: {}, thumbs: {},
  };

  /* ───────────────────────── util ───────────────────────── */

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function fmt(t) {
    t = Math.max(0, t || 0);
    var m = Math.floor(t / 60), s = t % 60;
    return m + ":" + (s < 10 ? "0" : "") + s.toFixed(1);
  }
  var toastTimer;
  function toast(msg, bad) {
    var el = $("#toast");
    el.textContent = msg;
    el.className = "toast on" + (bad ? " bad" : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { el.className = "toast"; }, bad ? 5200 : 2400);
  }
  async function api(path, opts) {
    var r = await fetch(path, opts);
    var txt = await r.text();
    var data;
    try { data = txt ? JSON.parse(txt) : {}; } catch (e) { data = { error: txt }; }
    if (!r.ok) {
      // carry the parsed body along — the 402 price check needs its estimate
      var err = new Error(data.error || data.message || txt.slice(0, 300) || r.status);
      err.status = r.status;
      err.data = data;
      throw err;
    }
    return data;
  }
  function post(path, body) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" },
                       body: JSON.stringify(body || {}) });
  }

  /* ───────────────────────── document access ───────────────────────── */

  function tracks() { return (S.doc && S.doc.tracks) || []; }
  function track(id) { return tracks().filter(function (t) { return t.id === id; })[0]; }
  function allClips() {
    return tracks().reduce(function (a, t) {
      return a.concat(t.clips.map(function (c) { return { c: c, t: t }; }));
    }, []);
  }
  function findClip(id) {
    var hit = allClips().filter(function (x) { return x.c.id === id; })[0];
    return hit || null;
  }
  function clipDur(c) {
    if (c.dur != null && c.out == null) return Math.max(0, c.dur);
    return Math.max(0, (c.out - c.in) / (c.speed || 1));
  }
  function clipEnd(c) { return (c.start || 0) + clipDur(c); }
  function videoTracks() { return tracks().filter(function (t) { return t.kind === "video"; }); }

  /* spine = V1: the track playback follows */
  function spine() { return videoTracks()[0]; }

  function clipAt(t) {
    var sp = spine();
    if (!sp) return null;
    for (var i = 0; i < sp.clips.length; i++) {
      var c = sp.clips[i];
      if (t >= c.start - 1e-4 && t < clipEnd(c) - 1e-4) return c;
    }
    return null;
  }
  function nextClipAfter(t) {
    var sp = spine();
    if (!sp) return null;
    var list = sp.clips.filter(function (c) { return c.start >= t - 1e-4; })
                       .sort(function (a, b) { return a.start - b.start; });
    return list[0] || null;
  }
  function mediaUrl(src) {
    return "/media/" + String(S.proxies[src] || src).replace(/^\/+/, "");
  }

  /* ───────────────────────── server ops ───────────────────────── */

  async function op(body) {
    S.saving++;
    $("#saved").textContent = "saving…";
    $("#saved").className = "saved busy";
    try {
      var d = await post("/api/seq/" + S.slug + "/op", body);
      applyDoc(d);
      return d;
    } catch (e) {
      toast(e.message, true);
      throw e;
    } finally {
      if (--S.saving <= 0) {
        $("#saved").textContent = "saved";
        $("#saved").className = "saved";
      }
    }
  }

  function applyDoc(d) {
    S.doc = d.doc;
    S.dur = d.duration || 0;
    S.sources = d.sources || {};
    if ($("#proj-name") !== document.activeElement) $("#proj-name").value = S.doc.name || "";
    renderAll();
    ensureProxies();
    clearTimeout(S._thumbTimer);
    S._thumbTimer = setTimeout(loadThumbs, 1200);   // player loads first
  }

  async function ensureProxies() {
    // proxies matter for long sources; a 5s clip scrubs fine as-is. Stagger the
    // requests — each one can spawn a server-side ffmpeg encode, and a burst at
    // boot once starved Chrome's media loaders into a full playback stall.
    var srcs = Object.keys(S.sources).filter(function (s) {
      var i = S.sources[s];
      return i && !i.missing && i.kind === "video" && (i.dur || 0) > 20;
    });
    for (var i = 0; i < srcs.length; i++) {
      var s = srcs[i];
      if (S.proxies[s] !== undefined) continue;
      S.proxies[s] = null;
      try {
        var r = await post("/api/seq/media/proxy", { src: s });
        if (r.cached && r.proxy) { S.proxies[s] = r.proxy; }
      } catch (e) { /* original file still plays; a proxy is only an optimisation */ }
      await new Promise(function (r2) { setTimeout(r2, 600); });
    }
  }

  /* ───────────────────────── render: timeline ───────────────────────── */

  function tlWidth() { return Math.max(600, S.dur * S.pxs + 240); }

  function renderRuler() {
    var W = tlWidth();
    $("#tlinner").style.width = (HEAD + W) + "px";
    var step = S.pxs >= 200 ? 0.5 : S.pxs >= 100 ? 1 : S.pxs >= 40 ? 2 : S.pxs >= 18 ? 5 : 10;
    var html = "";
    for (var t = 0; t <= S.dur + step; t += step) {
      html += '<div class="tick" style="left:' + (HEAD + t * S.pxs) + 'px">' +
              (step < 1 ? t.toFixed(1) : t) + "s</div>";
    }
    $("#ruler").innerHTML = html;
    $("#ruler").style.width = (HEAD + W) + "px";
  }

  function clipLabel(c, kind) {
    if (kind === "text") return esc(c.text || "text");
    var base = String(c.src || "").split("/").pop();
    return esc(base.length > 40 ? base.slice(0, 37) + "…" : base);
  }

  function renderLanes() {
    var html = "";
    tracks().forEach(function (t) {
      var body = "";
      t.clips.forEach(function (c) {
        var left = HEAD + c.start * S.pxs;
        var w = Math.max(14, clipDur(c) * S.pxs);
        var ai = (c.origin && c.origin.engine) ? '<span class="ai" title="AI: ' +
                 esc(c.origin.engine) + '">✨</span>' : "";
        body += '<div class="clip kind-' + t.kind + (S.sel === c.id ? " sel" : "") +
                '" data-clip="' + c.id + '" data-track="' + t.id + '"' +
                ' style="left:' + left + 'px;width:' + w + 'px">' +
                '<div class="h l" data-edge="in"></div>' +
                '<div class="lbl">' + clipLabel(c, t.kind) + "</div>" + ai +
                '<div class="h r" data-edge="out"></div></div>';
      });
      html += '<div class="lane" data-track="' + t.id + '">' +
              '<div class="lh"><span class="nm">' + esc(t.name || t.id) + "</span>" +
              '<div class="ctl"><button class="mute' + (t.muted ? " on" : "") +
              '" data-mute="' + t.id + '">' + (t.muted ? "muted" : "on") + "</button>" +
              '<span style="color:var(--tl-dim);font-size:9.5px;align-self:center">' +
              t.clips.length + "</span></div></div>" +
              '<div class="body" data-track="' + t.id + '">' + body + "</div></div>";
    });
    $("#lanes").innerHTML = html;
    applyThumbs();
    $("#lanes").style.width = (HEAD + tlWidth()) + "px";
    $("#tl-status").textContent = S.dur > 0
      ? allClips().length + " clips · " + fmt(S.dur) : "empty timeline";
  }

  function movePlayhead() {
    $("#playhead").style.left = (HEAD + S.t * S.pxs) + "px";
    $("#tc").textContent = fmt(S.t);
    $("#tcd").textContent = "/ " + fmt(S.dur);
  }

  /* ───────────────────────── render: player overlay ───────────────────────── */

  function renderOverlay() {
    var canvas = (S.doc && S.doc.canvas) || { w: 1080, h: 1920 };
    var frame = $("#frame");
    frame.style.aspectRatio = canvas.w + "/" + canvas.h;
    var box = frame.getBoundingClientRect();
    var k = box.height / canvas.h || 0;

    var html = "";
    tracks().filter(function (t) { return t.kind === "text" && !t.muted; })
      .forEach(function (t) {
        t.clips.forEach(function (c) {
          if (S.t < c.start || S.t >= clipEnd(c)) return;
          var st = c.style || {};
          var pos = st.pos || "bottom";
          var vpos = pos === "top" ? "top:8%;" : pos === "middle"
            ? "top:50%;transform:translateY(-50%);" : "bottom:8%;";
          html += '<div class="ov-text" style="' + vpos + "font-size:" +
                  Math.max(9, (st.size || 64) * k) + "px;color:" + esc(st.color || "#fff") +
                  ";font-weight:" + (st.bold === false ? 600 : 800) + '">' +
                  esc(c.text || "") + "</div>";
        });
      });
    $("#overlay").innerHTML = html;
  }

  /* ───────────────────────── render: inspector ───────────────────────── */

  function renderInspector() {
    var box = $("#inspector");
    var hit = S.sel ? findClip(S.sel) : null;
    if (!hit) { box.innerHTML = '<div class="empty">select a clip</div>'; return; }
    var c = hit.c, kind = hit.t.kind;
    var info = S.sources[c.src] || {};

    var h = "";
    if (kind === "text") {
      h += '<div class="grp"><h4>Text</h4>' + textInspector(c) + "</div>";
    } else {
      h += '<div class="grp"><h4>Source</h4><div class="meta">' +
           "<b>" + esc(String(c.src).split("/").pop()) + "</b><br>" +
           (info.w ? info.w + "×" + info.h + " · " + (info.fps || 0).toFixed(0) + "fps · " : "") +
           "source " + (info.dur || 0).toFixed(1) + "s" +
           (info.audio ? " · audio" : " · silent") + "</div>";
      if (c.origin && c.origin.engine) {
        h += '<div class="origin">✨ <b>' + esc(c.origin.engine) + "</b> replaced<br>" +
             esc(String(c.origin.parent || "").split("/").pop()) +
             '<br><button class="btn ghost sm" id="i-revert" style="margin-top:6px">' +
             "↩ Revert to original</button></div>";
      }
      h += "</div>";
      h += '<div class="grp"><h4>Timing</h4>' +
           '<div class="row"><label>in</label><input type="number" step="0.1" id="i-in" value="' +
           (+c.in).toFixed(2) + '"></div>' +
           '<div class="row"><label>out</label><input type="number" step="0.1" id="i-out" value="' +
           (+c.out).toFixed(2) + '"></div>' +
           '<div class="row"><label>start</label><input type="number" step="0.1" id="i-start" value="' +
           (+c.start).toFixed(2) + '"></div>' +
           '<div class="row"><label>speed</label><input type="range" id="i-speed" min="0.25" max="4" ' +
           'step="0.05" value="' + (c.speed || 1) + '"><span class="val" id="i-speedv">' +
           (c.speed || 1).toFixed(2) + "×</span></div></div>";
      h += '<div class="grp"><h4>Audio</h4><div class="row"><label>volume</label>' +
           '<input type="range" id="i-vol" min="0" max="2" step="0.05" value="' +
           (c.volume == null ? 1 : c.volume) + '"><span class="val" id="i-volv">' +
           Math.round((c.volume == null ? 1 : c.volume) * 100) + "%</span></div></div>";
      var fi = 0, fo = 0;
      (c.effects || []).forEach(function (e) {
        if (e.type === "fade_in") fi = e.d;
        if (e.type === "fade_out") fo = e.d;
      });
      h += '<div class="grp"><h4>Fades</h4>' +
           '<div class="row"><label>fade in</label><input type="number" step="0.1" min="0" max="5" ' +
           'id="i-fin" value="' + fi + '"></div>' +
           '<div class="row"><label>fade out</label><input type="number" step="0.1" min="0" max="5" ' +
           'id="i-fout" value="' + fo + '"></div></div>';
      var tr = c.transform || {};
      h += '<div class="grp"><h4>Transform</h4>' +
           '<div class="row"><label>scale</label><input type="range" id="i-scale" min="1" max="3" ' +
           'step="0.02" value="' + (tr.scale || 1) + '"><span class="val" id="i-scalev">' +
           (tr.scale || 1).toFixed(2) + "×</span></div>" +
           '<div class="row"><label>x</label><input type="number" step="10" id="i-x" value="' +
           (tr.x || 0) + '"></div>' +
           '<div class="row"><label>y</label><input type="number" step="10" id="i-y" value="' +
           (tr.y || 0) + '"></div></div>';
    }
    h += '<div class="grp"><button class="btn ghost sm" id="i-split">⧉ Split at playhead</button> ' +
         '<button class="btn ghost sm" id="i-del">🗑 Delete</button>' +
         (kind === "video"
           ? ' <button class="btn ghost sm" id="i-autosplit">🎬 Auto-split scenes</button>' : "") +
         "</div>";
    box.innerHTML = h;
    bindInspector(c, kind);
  }

  function bindInspector(c, kind) {
    function setPatch(patch) { return op({ op: "set", clip: c.id, patch: patch }); }

    var live = [["i-speed", "i-speedv", function (v) { return v.toFixed(2) + "×"; }, "speed"],
                ["i-vol", "i-volv", function (v) { return Math.round(v * 100) + "%"; }, "volume"],
                ["i-scale", "i-scalev", function (v) { return v.toFixed(2) + "×"; }, "scale"]];
    live.forEach(function (row) {
      var el = $("#" + row[0]);
      if (!el) return;
      el.oninput = function () { $("#" + row[1]).textContent = row[2](parseFloat(el.value)); };
      el.onchange = function () {
        var v = parseFloat(el.value);
        if (row[3] === "scale") setPatch({ transform: { scale: v } });
        else setPatch((function (o) { o[row[3]] = v; return o; })({}));
      };
    });
    ["i-x", "i-y"].forEach(function (id) {
      var el = $("#" + id);
      if (el) el.onchange = function () {
        var o = {}; o[id.slice(2)] = parseFloat(el.value) || 0;
        setPatch({ transform: o });
      };
    });
    var inEl = $("#i-in"), outEl = $("#i-out"), stEl = $("#i-start");
    if (inEl) inEl.onchange = function () {
      op({ op: "trim", clip: c.id, edge: "in",
           delta: (parseFloat(inEl.value) - c.in) / (c.speed || 1) });
    };
    if (outEl) outEl.onchange = function () {
      op({ op: "trim", clip: c.id, edge: "out",
           delta: (parseFloat(outEl.value) - c.out) / (c.speed || 1) });
    };
    if (stEl) stEl.onchange = function () {
      op({ op: "move", clip: c.id, start: parseFloat(stEl.value) || 0 });
    };
    var txt = $("#i-text");
    if (txt) txt.onchange = function () { setPatch({ text: txt.value }); };
    var tdur = $("#i-tdur");
    if (tdur) tdur.onchange = function () { setPatch({ dur: parseFloat(tdur.value) || 1 }); };
    var tsize = $("#i-size");
    if (tsize) tsize.onchange = function () { setPatch({ style: { size: parseInt(tsize.value, 10) || 64 } }); };
    var tpos = $("#i-pos");
    if (tpos) tpos.onchange = function () { setPatch({ style: { pos: tpos.value } }); };
    var tcol = $("#i-color");
    if (tcol) tcol.onchange = function () { setPatch({ style: { color: tcol.value } }); };

    var fin = $("#i-fin"), fou = $("#i-fout");
    function fadePatch() {
      setPatch({ fade_in: parseFloat(fin.value) || 0, fade_out: parseFloat(fou.value) || 0 });
    }
    if (fin) { fin.onchange = fadePatch; fou.onchange = fadePatch; }
    var asBtn = $("#i-autosplit");
    if (asBtn) asBtn.onclick = async function () {
      asBtn.disabled = true;
      showWorking("Detecting scene cuts", String(c.src).split("/").pop());
      try {
        var d = await post("/api/seq/" + S.slug + "/autosplit", { clip: c.id });
        applyDoc(d);
        toast(d.cuts.length ? "split into " + (d.cuts.length + 1) + " shots" : "no cuts found");
      } catch (e) { toast(e.message, true); }
      hideWorking();
    };
    var rev = $("#i-revert");
    if (rev) rev.onclick = function () { op({ op: "revert_source", clip: c.id }); };
    $("#i-split").onclick = doSplit;
    $("#i-del").onclick = function () { doDelete(false); };
  }

  /* text inspector body — kept out of the template above for readability */
  function textInspector(c) {
    var st = c.style || {};
    return '<div class="row"><label>text</label><input type="text" id="i-text" value="' +
      esc(c.text || "") + '"></div>' +
      '<div class="row"><label>dur</label><input type="number" step="0.1" id="i-tdur" value="' +
      (+clipDur(c)).toFixed(2) + '"></div>' +
      '<div class="row"><label>size</label><input type="number" step="4" id="i-size" value="' +
      (st.size || 64) + '"></div>' +
      '<div class="row"><label>pos</label><select id="i-pos">' +
      ["top", "middle", "bottom"].map(function (p) {
        return '<option value="' + p + '"' + ((st.pos || "bottom") === p ? " selected" : "") +
               ">" + p + "</option>";
      }).join("") + "</select></div>" +
      '<div class="row"><label>color</label><input type="text" id="i-color" value="' +
      esc(st.color || "#FFFFFF") + '"></div>';
  }

  /* ───────────────────────── render: media ───────────────────────── */

  function renderMedia() {
    var q = S.filter.toLowerCase();
    var list = S.media.filter(function (m) { return !q || m.name.toLowerCase().indexOf(q) >= 0; });
    if (!list.length) { $("#media-list").innerHTML = '<div class="empty">no media</div>'; return; }
    $("#media-list").innerHTML = list.slice(0, 250).map(function (m) {
      return '<div class="mi" data-src="' + esc(m.src) + '" title="' + esc(m.src) + '">' +
        '<span class="ic">' + (m.kind === "audio" ? "♪" : "▶") + "</span>" +
        '<span class="nm">' + esc(m.name) +
        '<div class="sub">' + esc(m.folder) + "</div></span></div>";
    }).join("");
    $$("#media-list .mi").forEach(function (el) {
      el.onclick = function () { addMedia(el.dataset.src); };
    });
  }

  async function addMedia(src) {
    try {
      await op({ op: "add", src: src, append: true });
      toast("added " + src.split("/").pop());
    } catch (e) { /* op() already reported it */ }
  }

  function renderAll() {
    renderRuler(); renderLanes(); renderInspector(); renderOverlay(); movePlayhead();
    if (typeof updateEstimate === "function") updateEstimate();
    if (typeof updateSrcPreview === "function") updateSrcPreview();
    syncPlayerToTime(true);
  }

  /* ───────────────────────── playback ───────────────────────── */

  function els() { return { A: $("#vA"), B: $("#vB") }; }
  function activeEl() { return els()[S.curEl]; }
  function idleEl() { return els()[S.curEl === "A" ? "B" : "A"]; }

  function showEl(which) {
    S.curEl = which;
    var e = els();
    e.A.classList.toggle("on", which === "A");
    e.B.classList.toggle("on", which === "B");
  }

  function mount(el, clip) {
    var url = mediaUrl(clip.src);
    if (el.dataset.src !== url) {
      el.dataset.src = url;
      el.src = url;
      el.load();
    }
    el.dataset.clip = clip.id;
  }

  function localTime(clip, t) {
    return clip.in + Math.max(0, t - clip.start) * (clip.speed || 1);
  }

  /* Put the visible frame where the playhead is. `hard` forces a reseek. */
  function syncPlayerToTime(hard) {
    var clip = clipAt(S.t);
    $("#noclip").classList.toggle("hide", !!clip);
    if (!clip) {
      els().A.classList.remove("on");
      els().B.classList.remove("on");
      S.activeClip = null;
      renderOverlay();
      return;
    }
    var el = activeEl();
    if (S.activeClip !== clip.id || el.dataset.clip !== clip.id || hard) {
      // the idle element may already hold this clip (we preloaded it)
      var other = idleEl();
      if (other.dataset.clip === clip.id && S.activeClip !== clip.id) {
        showEl(S.curEl === "A" ? "B" : "A");
        el = activeEl();
      } else {
        mount(el, clip);
        showEl(S.curEl);
      }
      S.activeClip = clip.id;
    }
    var want = localTime(clip, S.t);
    if (Math.abs((el.currentTime || 0) - want) > 0.08) {
      try { el.currentTime = want; } catch (e) { /* not seekable yet */ }
    }
    preloadNext(clip);
    syncAudio();
    renderOverlay();
  }

  function preloadNext(clip) {
    var nxt = nextClipAfter(clipEnd(clip) - 1e-3);
    if (!nxt || nxt.id === clip.id) return;
    var other = idleEl();
    if (other.dataset.clip === nxt.id) return;
    mount(other, nxt);
    var seekIt = function () {
      try { other.currentTime = nxt.in; } catch (e) { /* ignore */ }
      other.removeEventListener("loadeddata", seekIt);
    };
    other.addEventListener("loadeddata", seekIt);
  }

  /* audio tracks ride alongside; they are slaved to the timeline clock */
  function syncAudio() {
    var wanted = {};
    tracks().filter(function (t) { return t.kind === "audio" && !t.muted; })
      .forEach(function (t) {
        t.clips.forEach(function (c) {
          if (S.t < c.start || S.t >= clipEnd(c)) return;
          wanted[c.id] = c;
          var a = S.audios[c.id];
          if (!a) {
            a = new Audio(mediaUrl(c.src));
            a.preload = "auto";
            S.audios[c.id] = a;
          }
          a.volume = Math.max(0, Math.min(1, c.volume == null ? 1 : c.volume));
          var want = localTime(c, S.t);
          if (Math.abs(a.currentTime - want) > 0.25) {
            try { a.currentTime = want; } catch (e) { /* ignore */ }
          }
          if (S.playing && a.paused) a.play().catch(function () {});
          if (!S.playing && !a.paused) a.pause();
        });
      });
    Object.keys(S.audios).forEach(function (id) {
      if (!wanted[id] && !S.audios[id].paused) S.audios[id].pause();
    });
  }

  function tick() {
    if (!S.playing) return;
    var clip = clipAt(S.t);
    var now = performance.now();
    if (clip) {
      var el = activeEl();
      if (!el.paused && el.readyState >= 2) {
        var t = clip.start + (el.currentTime - clip.in) / (clip.speed || 1);
        // guard against a stale element reporting the previous clip's time
        S.t = (t >= clip.start - 0.5 && t <= clipEnd(clip) + 0.5) ? t : S.t + (now - S.wallLast) / 1000;
      } else {
        S.t += (now - S.wallLast) / 1000;
      }
      if (S.t >= clipEnd(clip) - 1e-3) {
        S.t = clipEnd(clip);
        var nxt = clipAt(S.t) || nextClipAfter(S.t);
        if (nxt) { S.t = Math.max(S.t, nxt.start); syncPlayerToTime(false); startActive(); }
      }
    } else {
      S.t += (now - S.wallLast) / 1000;           // crossing a gap: wall clock
      var upcoming = nextClipAfter(S.t);
      if (upcoming && S.t >= upcoming.start) { syncPlayerToTime(false); startActive(); }
      else renderOverlay();
    }
    S.wallLast = now;

    if (S.t >= S.dur - 1e-3) { S.t = S.dur; pause(); movePlayhead(); return; }
    movePlayhead();
    renderOverlay();
    syncAudio();
    keepVisible();
    S.rafId = requestAnimationFrame(tick);
  }

  function startActive() {
    var clip = clipAt(S.t);
    if (!clip) return;
    var el = activeEl();
    el.playbackRate = 1;
    el.play().catch(function () {});
  }

  function play() {
    if (S.playing || S.dur <= 0) return;
    if (S.t >= S.dur - 1e-3) S.t = 0;
    S.playing = true;
    $("#btn-play").textContent = "⏸";
    syncPlayerToTime(false);
    startActive();
    S.wallLast = performance.now();
    S.rafId = requestAnimationFrame(tick);
    // rAF stops entirely in occluded/minimized windows — this interval keeps the
    // clock and clip-boundary swaps running so playback survives backgrounding
    clearInterval(S.tickGuard);
    S.tickGuard = setInterval(function () {
      if (S.playing && performance.now() - S.wallLast > 300) tick();
    }, 250);
  }

  function pause() {
    S.playing = false;
    $("#btn-play").textContent = "▶";
    cancelAnimationFrame(S.rafId);
    clearInterval(S.tickGuard);
    els().A.pause(); els().B.pause();
    Object.keys(S.audios).forEach(function (k) { S.audios[k].pause(); });
  }

  function seek(t) {
    S.t = Math.max(0, Math.min(S.dur, t));
    movePlayhead();
    syncPlayerToTime(false);
    if (S.playing) { startActive(); S.wallLast = performance.now(); }
  }

  function keepVisible() {
    var sc = $("#tlscroll");
    var x = HEAD + S.t * S.pxs;
    if (x < sc.scrollLeft + HEAD + 20) sc.scrollLeft = x - HEAD - 20;
    else if (x > sc.scrollLeft + sc.clientWidth - 60) sc.scrollLeft = x - sc.clientWidth + 60;
  }

  /* ───────────────────────── editing actions ───────────────────────── */

  async function doSplit() {
    // cut whatever is under the playhead; the selection only wins when the
    // playhead is actually inside it (e.g. an overlay clip on another track)
    var hit = S.sel ? findClip(S.sel) : null;
    var clip = (hit && S.t > hit.c.start + 0.05 && S.t < clipEnd(hit.c) - 0.05)
      ? hit.c : clipAt(S.t);
    if (!clip) return toast("nothing under the playhead", true);
    if (S.t <= clip.start + 0.05 || S.t >= clipEnd(clip) - 0.05)
      return toast("move the playhead inside the clip first", true);
    await op({ op: "split", clip: clip.id, at: S.t });
    toast("split");
  }

  async function doDelete(ripple) {
    if (!S.sel) return toast("select a clip first", true);
    await op({ op: "remove", clip: S.sel, ripple: !!ripple });
    S.sel = null;
    renderInspector();
    toast(ripple ? "deleted + closed the gap" : "deleted");
  }

  async function addText() {
    await op({ op: "add_text", text: "NEW TEXT", start: S.t, dur: 2 });
    toast("text added at playhead");
  }

  /* ───────────────────────── timeline interaction ───────────────────────── */

  function timeFromEvent(e) {
    var inner = $("#tlinner").getBoundingClientRect();
    return Math.max(0, (e.clientX - inner.left - HEAD) / S.pxs);
  }

  /* snap to clip edges, 0 and the playhead — within ~7px */
  function snap(t, ignoreId) {
    var tol = 7 / S.pxs;
    var cands = [0, S.t];
    allClips().forEach(function (x) {
      if (x.c.id === ignoreId) return;
      cands.push(x.c.start, clipEnd(x.c));
    });
    var best = t, bd = tol;
    cands.forEach(function (c) {
      var d = Math.abs(c - t);
      if (d < bd) { bd = d; best = c; }
    });
    return Math.max(0, best);
  }

  function bindTimeline() {
    $("#ruler").addEventListener("pointerdown", function (e) {
      seek(timeFromEvent(e));
      var mv = function (ev) { seek(timeFromEvent(ev)); };
      var up = function () {
        window.removeEventListener("pointermove", mv);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", mv);
      window.addEventListener("pointerup", up);
    });

    $("#lanes").addEventListener("pointerdown", function (e) {
      var mute = e.target.closest("[data-mute]");
      if (mute) {
        var t = track(mute.dataset.mute);
        op({ op: "track", action: "mute", track: t.id, muted: !t.muted });
        return;
      }
      var clipEl = e.target.closest(".clip");
      if (!clipEl) {
        var laneBody = e.target.closest(".body");
        if (laneBody) seek(timeFromEvent(e));
        return;
      }
      var id = clipEl.dataset.clip;
      S.sel = id;
      renderLanes(); renderInspector();
      if (typeof updateEstimate === "function") updateEstimate();
      if (typeof updateSrcPreview === "function") updateSrcPreview();

      var hit = findClip(id);
      if (!hit) return;
      var clip = hit.c;
      var edge = e.target.dataset.edge;
      var t0 = timeFromEvent(e);
      var origStart = clip.start, origDur = clipDur(clip);
      var moved = false;

      // the element was just re-rendered by renderLanes — drag the fresh node
      clipEl = document.querySelector('.clip[data-clip="' + id + '"]') || clipEl;

      S.dragging = { id: id, edge: edge };
      clipEl.classList.add("drag");
      try { clipEl.setPointerCapture(e.pointerId); } catch (err) { /* synthetic/pen pointers */ }

      var mv = function (ev) {
        var dt = timeFromEvent(ev) - t0;
        if (Math.abs(dt) * S.pxs < 3 && !moved) return;
        moved = true;
        if (edge === "in") {
          var ns = snap(origStart + dt, id);
          var d = Math.min(ns - origStart, origDur - 0.1);
          clipEl.style.left = (HEAD + (origStart + d) * S.pxs) + "px";
          clipEl.style.width = Math.max(14, (origDur - d) * S.pxs) + "px";
        } else if (edge === "out") {
          var nd = Math.max(0.1, snap(origStart + origDur + dt, id) - origStart);
          clipEl.style.width = Math.max(14, nd * S.pxs) + "px";
        } else {
          clipEl.style.left = (HEAD + snap(Math.max(0, origStart + dt), id) * S.pxs) + "px";
        }
      };

      var up = async function (ev) {
        window.removeEventListener("pointermove", mv);
        window.removeEventListener("pointerup", up);
        clipEl.classList.remove("drag");
        S.dragging = null;
        if (!moved) return;
        var dt = timeFromEvent(ev) - t0;
        try {
          if (edge === "in") {
            var ns = snap(origStart + dt, id);
            await op({ op: "trim", clip: id, edge: "in",
                       delta: Math.min(ns - origStart, origDur - 0.1) });
          } else if (edge === "out") {
            var nd = Math.max(0.1, snap(origStart + origDur + dt, id) - origStart);
            await op({ op: "trim", clip: id, edge: "out", delta: nd - origDur });
          } else {
            await op({ op: "move", clip: id, start: snap(Math.max(0, origStart + dt), id) });
          }
        } catch (err) { renderLanes(); }
      };
      window.addEventListener("pointermove", mv);
      window.addEventListener("pointerup", up);
    });
  }

  /* ───────────────────────── projects ───────────────────────── */

  async function loadProjects() {
    var d = await api("/api/seq/projects");
    var box = $("#proj-list");
    if (!d.projects.length) {
      box.innerHTML = '<div class="empty">no projects yet — create one above</div>';
      return d.projects;
    }
    box.innerHTML = d.projects.map(function (p) {
      return '<div class="pi" data-slug="' + esc(p.slug) + '">' +
        '<span class="nm">' + esc(p.name || p.slug) +
        '<div class="sub">' + p.clips + " clips · " + fmt(p.duration) + " · v" + p.version +
        " · " + p.canvas.w + "×" + p.canvas.h + "</div></span>" +
        '<button class="del" data-del="' + esc(p.slug) + '" title="delete">🗑</button></div>';
    }).join("");
    $$("#proj-list .pi").forEach(function (el) {
      el.onclick = function (e) {
        if (e.target.dataset.del) return;
        openProject(el.dataset.slug);
      };
    });
    $$("#proj-list [data-del]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        if (!confirm("Delete this project? (moved to .trash)")) return;
        await post("/api/seq/" + b.dataset.del + "/delete", {});
        loadProjects();
      };
    });
    return d.projects;
  }

  async function openProject(slug) {
    var d = await api("/api/seq/" + slug);
    S.slug = slug;
    S.sel = null; S.t = 0; S.proxies = {}; S.audios = {}; S.thumbs = {};
    SCRIPT.clips = []; SCRIPT.flat = []; SCRIPT.selA = SCRIPT.selB = null;
    AI.history = [];
    pause();
    applyDoc(d);
    $("#projmodal").classList.remove("open");
    history.replaceState(null, "", "/timeline?p=" + encodeURIComponent(slug));
    toast("opened " + (d.doc.name || slug));
  }

  async function createProject() {
    var name = $("#new-name").value.trim() || "Untitled edit";
    var wh = $("#new-ratio").value.split("x");
    var d = await post("/api/seq/projects", { name: name, w: +wh[0], h: +wh[1], fps: 30 });
    $("#new-name").value = "";
    await openProject(d.slug);
  }

  /* ───────────────────────── render / export ───────────────────────── */

  async function doRender(draft) {
    if (!S.slug) return;
    if (S.dur <= 0) return toast("timeline is empty", true);
    var btn = draft ? $("#btn-draft") : $("#btn-render");
    btn.disabled = true;
    try {
      var d = await post("/api/seq/" + S.slug + "/render", { draft: !!draft });
      toast((draft ? "draft" : "export") + " started — watching…");
      watchJob(d.job_id, d.output, btn);
    } catch (e) {
      toast(e.message, true);
      btn.disabled = false;
    }
  }

  function watchJob(jobId, out, btn) {
    var timer = setInterval(async function () {
      try {
        var j = await api("/api/job/" + jobId);
        if (j.status === "running") return;
        clearInterval(timer);
        btn.disabled = false;
        if (j.status === "done") {
          toast("✓ rendered → " + out);
        } else {
          var tail = friendlyTail(j.lines, 2).join(" · ");
          toast("render " + j.status + ": " +
            (tail || "see Tools → Jobs").slice(0, 220), true);
        }
      } catch (e) { clearInterval(timer); btn.disabled = false; }
    }, 1500);
  }


  /* ═════════════════════ AI editor: chat, models, generation ═════════════════════
   * Free edits (cuts, text, timing) apply the moment Claude answers. Anything that
   * costs money is a proposal with a price on it — the server answers 402 until a
   * request carries confirm_cost, so a stray sentence can never spend.
   */

  var AI = { models: [], refs: [], busyJob: null, history: [] };

  /* The friendly-log filter lives in vs-core.js (VS.friendlyLine) so the
   * Creator's job drawer and the editor can't drift apart. */
  var friendlyLine = window.VS.friendlyLine;
  var friendlyTail = window.VS.friendlyTail;

  function chatAdd(cls, html) {
    var log = $("#chatlog");
    var d = document.createElement("div");
    d.className = "msg " + cls;
    d.innerHTML = html;
    log.appendChild(d);
    log.scrollTop = log.scrollHeight;
    return d;
  }

  function defaultClipId() {
    if (S.sel && findClip(S.sel)) return S.sel;
    var c = clipAt(S.t) || (((spine() || {}).clips) || [])[0];
    return c ? c.id : null;
  }

  async function loadModels() {
    try {
      var d = await api("/api/seq/models");
      AI.models = d.models || [];
    } catch (e) { AI.models = []; }
    var sel = $("#model-sel");
    var groups = { v2v: "Edit this clip (video → video)", i2v: "Image → video",
                   ref2v: "Reference images → video", t2v: "Text → video" };
    sel.innerHTML = Object.keys(groups).map(function (mode) {
      var ms = AI.models.filter(function (m) { return m.mode === mode; });
      if (!ms.length) return "";
      return '<optgroup label="' + groups[mode] + '">' + ms.map(function (m) {
        return '<option value="' + m.key + '">' + esc(m.label) + " — $" +
               m.usd_per_sec.toFixed(2) + "/s" + (m.price_verified ? "" : " ?") + "</option>";
      }).join("") + "</optgroup>";
    }).join("");
    sel.onchange = onModelChange;
    onModelChange();
  }

  function currentModel() {
    var key = $("#model-sel").value;
    return AI.models.filter(function (m) { return m.key === key; })[0] || null;
  }

  function onModelChange() {
    var m = currentModel();
    if (!m) return;
    $("#model-note").innerHTML = esc(m.note || "") +
      (m.price_verified ? "" :
        '<div class="unv">⚠ fal published no rate for this endpoint — the estimate is inferred.</div>');
    $("#ref-cap").textContent = m.refs ? "(up to " + m.refs + ")" : "(not used by this model)";
    // duration follows the model: capped at AND defaulting to its maximum.
    // For edit (v2v) models it also lets you run on just the first N seconds.
    var sec = $("#gen-sec");
    sec.disabled = false;
    sec.max = m.max_sec;
    sec.value = m.max_sec;
    $("#gen-max").textContent = "max " + m.max_sec + "s";
    $("#gen-res").innerHTML = (m.resolutions && m.resolutions.length
      ? m.resolutions : ["720p"]).map(function (r) {
        return '<option>' + r + "</option>"; }).join("");
    $("#btn-generate").textContent = m.mode === "v2v"
      ? "▶ Run model — edit selected clip" : "▶ Run model — new clip";
    updateEstimate();
  }

  function updateEstimate() {
    var m = currentModel();
    if (!m) { $("#gen-est").textContent = ""; return; }
    var secs;
    var chosen = Math.min(parseFloat($("#gen-sec").value) || m.max_sec, m.max_sec);
    if (m.mode === "v2v") {
      var id = defaultClipId();
      var hit = id ? findClip(id) : null;
      if (!hit) { $("#gen-est").innerHTML = "add a video to the timeline first"; return; }
      secs = Math.min(clipDur(hit.c), m.max_sec, chosen);
    } else {
      secs = chosen;
    }
    var rate = m.usd_per_sec *
      ($("#gen-res").value === "1080p" ? (m.hd_mult || 1) : 1);
    $("#gen-est").innerHTML = "≈ <b>$" + (rate * secs).toFixed(2) + "</b> for " +
      secs.toFixed(1) + "s — estimate only, confirmed before spending";
  }

  /* ── inspiration images ── */

  function renderRefs() {
    $("#refs").innerHTML = AI.refs.map(function (r, i) {
      return '<div class="r"><img src="/media/' + esc(r) + '" alt="">' +
             '<button data-ref="' + i + '">✕</button></div>';
    }).join("");
    $$("#refs [data-ref]").forEach(function (b) {
      b.onclick = function () {
        AI.refs.splice(+b.dataset.ref, 1);
        renderRefs();
      };
    });
  }

  async function uploadRefs(files) {
    for (var i = 0; i < files.length; i++) {
      var fd = new FormData();
      fd.append("image", files[i]);
      try {
        var r = await api("/api/seq/refs/upload", { method: "POST", body: fd });
        AI.refs.push(r.ref);
        renderRefs();
      } catch (e) { toast("upload failed: " + e.message, true); }
    }
  }

  /* ── the working overlay on the preview ── */

  function showWorking(title, sub) {
    $("#wtitle").textContent = title;
    $("#wsub").textContent = sub || "";
    $("#wlog").textContent = "";
    $("#working").classList.add("on");
  }
  function hideWorking() {
    $("#working").classList.remove("on");
    $$(".clip.busy").forEach(function (el) { el.classList.remove("busy"); });
  }
  function markBusyClip(id) {
    $$(".clip.busy").forEach(function (el) { el.classList.remove("busy"); });
    if (!id) return;
    var el = document.querySelector('.clip[data-clip="' + id + '"]');
    if (el) el.classList.add("busy");
  }

  /* ── chat ── */

  async function sendChat() {
    var box = $("#chatbox");
    var msg = box.value.trim();
    if (!msg) return;
    if (!S.slug) return toast("open a project first", true);
    box.value = "";
    chatAdd("me", esc(msg));
    var thinking = chatAdd("bot think", "thinking…");
    showWorking("Claude is reading your timeline", msg.slice(0, 90));
    $("#btn-send").disabled = true;

    try {
      AI.history.push({ role: "user", text: msg });
      var d = await post("/api/seq/" + S.slug + "/chat", {
        message: msg, playhead: S.t, selected: S.sel,
        history: AI.history.slice(0, -1).slice(-6) });
      AI.history.push({ role: "assistant", text: d.reply || "" });
      thinking.remove();
      applyDoc(d);

      var html = esc(d.reply || "(no reply)");
      if (d.applied && d.applied.length) {
        html += '<div class="did"><b>applied:</b> ' + d.applied.map(function (o) {
          return esc(o.op); }).join(", ") + "</div>";
      }
      if (d.errors && d.errors.length) {
        html += '<div class="did" style="color:var(--tl-bad)">' +
                d.errors.map(esc).join("<br>") + "</div>";
      }
      var node = chatAdd(d.errors && d.errors.length && !(d.applied || []).length ? "err" : "bot", html);

      if (d.generate) proposeGenerate(node, d.generate);
      if (d.faceswap) proposeFaceswap(node, d.faceswap);
      if (d.applied && d.applied.length) seek(S.t);       // refresh what's on screen
    } catch (e) {
      thinking.remove();
      chatAdd("err", esc(e.message));
    } finally {
      $("#btn-send").disabled = false;
      hideWorking();
    }
  }

  function proposeGenerate(node, gen) {
    var m = currentModel();
    var hit = gen.clip ? findClip(gen.clip) : null;
    var secs = hit ? Math.min(clipDur(hit.c), (m && m.max_sec) || 10) : 5;
    var rate = m ? m.usd_per_sec : 0.13;
    var box = document.createElement("div");
    box.className = "propose";
    box.innerHTML = '<div class="p1">✨ This one needs a model to redraw the picture.<br>' +
      "<b>" + esc(gen.prompt || "") + "</b><br>" +
      (m ? esc(m.label) : "") + " · " + secs.toFixed(1) + "s · ≈ <b>$" +
      (rate * secs).toFixed(2) + '</b> (estimate)</div>' +
      '<button class="btn go sm">Run it</button>' +
      '<button class="btn ghost sm">No thanks</button>';
    node.appendChild(box);
    var btns = box.querySelectorAll("button");
    btns[0].onclick = function () {
      if (gen.clip) { S.sel = gen.clip; renderLanes(); }
      box.remove();
      runGenerate(gen.prompt);
    };
    btns[1].onclick = function () { box.remove(); };
    $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  }

  function proposeFaceswap(node, fs) {
    var box = document.createElement("div");
    box.className = "propose";
    box.innerHTML = '<div class="p1">🎭 Changing WHO is on camera runs through ' +
      "<b>Face Swap</b> with a saved avatar — the generate models can't hold a " +
      "face and would waste the spend.</div>" +
      '<button class="btn go sm">🎭 Open Face swap</button>';
    node.appendChild(box);
    box.querySelector("button").onclick = function () {
      box.remove();
      if (fs.clip && findClip(fs.clip)) {
        S.sel = fs.clip;
        renderLanes(); renderInspector(); updateSrcPreview();
      }
      var fsbox = $("#fs-box");
      fsbox.open = true;
      fsbox.dispatchEvent(new Event("toggle"));
      $("#panel-ai").scrollTop = $("#panel-ai").scrollHeight;
      toast("pick an avatar (or 📸 grab one), then 🎭 Swap face on selected clip");
    };
    $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  }

  /* ── generation ── */

  async function runGenerate(promptOverride) {
    var m = currentModel();
    if (!m) return toast("pick a model", true);
    if (!S.slug) return toast("open a project first", true);

    var prompt = promptOverride;
    if (!prompt) {
      prompt = ($("#chatbox").value || "").trim();
      if (!prompt) {
        $("#chatbox").focus();
        return toast("type the change in the chat box first — the model needs a prompt", true);
      }
    }
    var target = m.mode === "v2v" ? defaultClipId() : null;
    if (m.mode === "v2v" && !target) return toast("add a video to the timeline first", true);

    var body = {
      model: m.key, prompt: prompt, clip: target,
      resolution: $("#gen-res").value,
      seconds: parseFloat($("#gen-sec").value) || 5,
      refs: AI.refs.slice(0, m.refs || 0),
    };
    if (m.mode === "i2v" && AI.refs.length) { body.image = AI.refs[0]; }

    // First call is the price check: the server answers 402 with an estimate and
    // spends nothing. Only the second call, carrying confirm_cost, can bill.
    var est;
    try {
      await post("/api/seq/" + S.slug + "/generate", body);
      return toast("unexpected: the server skipped the price check", true);
    } catch (e) {
      if (e.status === 402 && e.data && e.data.estimate) est = e.data.estimate;
      else return toast(e.message, true);
    }

    // inline confirmation -- a blocking confirm() dialog freezes the page (and
    // any automation); the price belongs in the conversation itself
    var node = chatAdd("bot", "$ " + esc(est.summary) +
      (est.verified ? "" :
        "<br>! fal published no rate for this endpoint - this figure is inferred."));
    var box = document.createElement("div");
    box.className = "propose";
    box.innerHTML = '<div class="p1">This spends real money on fal.ai.</div>' +
      '<button class="btn go sm">Confirm &amp; run</button> ' +
      '<button class="btn ghost sm">Cancel</button>';
    node.appendChild(box);
    var btns = box.querySelectorAll("button");
    btns[1].onclick = function () { box.remove(); };
    btns[0].onclick = function () { box.remove(); execGenerate(body, est, prompt); };
    $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  }

  async function execGenerate(body, est, prompt) {
    body.confirm_cost = true;
    $("#btn-generate").disabled = true;
    $("#chatbox").value = "";
    chatAdd("bot", "✨ Running <b>" + esc(est.model) + "</b> — " + esc(est.summary));
    markBusyClip(body.clip);
    showWorking(est.model, prompt.slice(0, 90));

    try {
      var d = await post("/api/seq/" + S.slug + "/generate", body);
      watchGenerate(d.job_id, body.clip);
    } catch (e) {
      toast(e.message, true);
      chatAdd("err", esc(e.message));
      hideWorking();
      $("#btn-generate").disabled = false;
    }
  }

  function watchGenerate(jobId, clipId) {
    AI.busyJob = jobId;
    var timer = setInterval(async function () {
      var j;
      try { j = await api("/api/job/" + jobId); }
      catch (e) { clearInterval(timer); hideWorking(); $("#btn-generate").disabled = false; return; }

      var tailLines = friendlyTail(j.lines, 3);
      $("#wlog").textContent = tailLines.join("\n");
      $("#wsub").textContent = (tailLines[tailLines.length - 1] || "working…").slice(0, 110);

      if (j.status === "running") return;
      clearInterval(timer);
      AI.busyJob = null;
      hideWorking();
      $("#btn-generate").disabled = false;

      if (j.status === "done") {
        chatAdd("bot", "✓ done — the edited video was ADDED to the end of the " +
                "timeline. Your original clip is untouched; keep the one you " +
                "like and delete the other.");
        try {
          applyDoc(await api("/api/seq/" + S.slug));
          var sp2 = spine();
          var last = sp2 && sp2.clips.length ? sp2.clips[sp2.clips.length - 1] : null;
          if (last) {
            S.sel = last.id;
            renderLanes(); renderInspector(); updateSrcPreview();
            seek(last.start + 0.05);
          }
        } catch (e) { /* the doc reload is a convenience */ }
      } else {
        var tail = friendlyTail(j.lines, 3).join(" · ");
        chatAdd("err", "generation " + esc(j.status) + ": " +
          esc((tail || "no details — see Tools → Jobs").slice(0, 400)));
      }
    }, 2000);
  }

  /* ── source preview: the video BEFORE any change ──
   * Shows the selected clip's original source (falls back to the clip under
   * the playhead, then the first clip). preload=metadata keeps it light so it
   * never competes with the main player for media connections.
   */
  function updateSrcPreview() {
    var box = $("#src-prev"), v = $("#vSrc"), lab = $("#src-label");
    if (!box) return;
    var hit = S.sel ? findClip(S.sel) : null;
    var clip = (hit && hit.t.kind === "video") ? hit.c
      : clipAt(S.t) || (((spine() || {}).clips) || [])[0];
    if (!clip || !clip.src) {
      box.classList.add("empty");
      lab.textContent = "no clip yet — drop a video below";
      return;
    }
    box.classList.remove("empty");
    var url = mediaUrl(clip.src);
    if (v.dataset.src !== url) {
      v.dataset.src = url;
      v.src = url;
      var seekOnce = function () {
        try { v.currentTime = clip.in || 0; } catch (e) { /* not seekable yet */ }
        v.removeEventListener("loadedmetadata", seekOnce);
      };
      v.addEventListener("loadedmetadata", seekOnce);
    }
    lab.textContent = String(clip.src).split("/").pop() + " · clip " +
      clipDur(clip).toFixed(1) + "s" + (S.sel === clip.id ? " · selected" : "");
  }

  /* ── upload straight onto the timeline ──
   * Files land in uploads/ via the same /api/upload the Creator uses (so the
   * video exists in BOTH places), then append themselves as clips here.
   */
  function bindUpload() {
    var drop = $("#ai-drop"), file = $("#ai-file"), msg = $("#ai-dropmsg");
    if (!drop) return;

    function uploadFiles(files) {
      if (!files || !files.length) return;
      if (!S.slug) return toast("open a project first", true);
      var list = [].slice.call(files);
      var i = 0;
      drop.classList.add("busy");

      function next() {
        if (i >= list.length) {
          drop.classList.remove("busy");
          msg.textContent = "Drop a video here — or click to upload";
          api("/api/seq/media/list").then(function (m) {
            S.media = m.media || [];
            renderMedia();
          }).catch(function () {});
          return;
        }
        var f = list[i++];
        var fd = new FormData();
        fd.append("file", f);
        var x = new XMLHttpRequest();
        x.open("POST", "/api/upload");
        x.upload.onprogress = function (e) {
          if (e.lengthComputable) {
            msg.textContent = "Uploading " + f.name + " — " +
              Math.round(e.loaded / e.total * 100) + "%";
          }
        };
        x.onload = async function () {
          if (x.status !== 200) {
            toast("upload failed: " + x.responseText.slice(0, 140), true);
            return next();
          }
          var res = {};
          try { res = JSON.parse(x.responseText); } catch (e) { }
          msg.textContent = "Adding " + res.name + " to the timeline…";
          try {
            await op({ op: "add", src: "uploads/" + res.name, append: true });
            toast("✓ " + res.name + " is on the timeline");
          } catch (e) { /* op() reported it */ }
          next();
        };
        x.onerror = function () { toast("upload failed", true); next(); };
        x.send(fd);
      }
      next();
    }

    drop.addEventListener("click", function () { file.click(); });
    file.addEventListener("change", function (e) { uploadFiles(e.target.files); e.target.value = ""; });
    drop.addEventListener("dragover", function (e) { e.preventDefault(); drop.classList.add("drag"); });
    drop.addEventListener("dragleave", function () { drop.classList.remove("drag"); });
    drop.addEventListener("drop", function (e) {
      e.preventDefault();
      drop.classList.remove("drag");
      uploadFiles(e.dataTransfer.files);
    });
  }

  /* ── ⏩ Extend (right pane): boomerang-extend, then hand off to dub+lipsync ──
   * The extended file lands in uploads/, so it exists for BOTH the timeline and
   * the Creator pipeline. "Dub & Lipsync" deep-links into the Creator's dub
   * step (/?v=<name>&step=dub) — that step owns voices, tiers and lipsync.
   */
  function extRefreshSources() {
    var sel = $("#ext-src");
    if (!sel) return;
    var seen = {}, opts = ['<option value="">— pick a video —</option>'];
    // timeline sources first (most likely what you want to extend)
    Object.keys(S.sources).forEach(function (s) {
      var i = S.sources[s];
      if (!i || i.missing || i.kind !== "video") return;
      seen[s] = 1;
      opts.push('<option value="' + esc(s) + '">' + esc(s.split("/").pop()) +
                " (" + (i.dur || 0).toFixed(1) + "s · on timeline)</option>");
    });
    S.media.filter(function (m) { return m.kind === "video" && !seen[m.src]; })
      .slice(0, 80).forEach(function (m) {
        opts.push('<option value="' + esc(m.src) + '">' + esc(m.name) + "</option>");
      });
    var keep = sel.value;
    sel.innerHTML = opts.join("");
    if (keep) sel.value = keep;
  }

  async function extShowInfo() {
    var src = $("#ext-src").value;
    var box = $("#ext-info");
    if (!src) { box.textContent = ""; return; }
    var i = S.sources[src];
    if (!i) {
      try { i = await api("/api/seq/media/probe?src=" + encodeURIComponent(src)); }
      catch (e) { i = null; }
    }
    if (i && i.dur) {
      box.textContent = i.dur.toFixed(1) + "s now · " + i.w + "×" + i.h +
        (i.dur > 90 ? " — over 90s: too long for the free loop" : "");
      var want = Math.max(10, Math.ceil(i.dur * 3));
      if (!$("#ext-sec").dataset.touched) $("#ext-sec").value = Math.min(600, want);
    } else box.textContent = "";
  }

  async function runExtend() {
    var src = $("#ext-src").value;
    if (!src) return toast("pick a video to extend", true);
    var secs = parseFloat($("#ext-sec").value) || 0;
    var btn = $("#ext-go");
    btn.disabled = true;
    $("#ext-status").textContent = "extending…";
    $("#ext-result").innerHTML = "";
    showWorking("Seamless extend", src.split("/").pop() + " → " + secs + "s");
    try {
      var d = await post("/api/seq/extend", { src: src, seconds: secs });
      var timer = setInterval(async function () {
        var j;
        try { j = await api("/api/job/" + d.job_id); }
        catch (e) { clearInterval(timer); hideWorking(); btn.disabled = false; return; }
        var last = friendlyTail(j.lines, 1)[0] || "extending…";
        $("#wsub").textContent = last.slice(0, 110);
        $("#ext-status").textContent = last.slice(0, 80);
        if (j.status === "running") return;
        clearInterval(timer);
        hideWorking();
        btn.disabled = false;
        if (j.status === "done") {
          $("#ext-status").textContent = "";
          extShowResult(d.name, d.src, secs);
          api("/api/seq/media/list").then(function (m) {
            S.media = m.media || []; renderMedia(); extRefreshSources();
          }).catch(function () {});
        } else {
          var tail = friendlyTail(j.lines, 2).join(" · ");
          $("#ext-status").textContent = "failed: " +
            (tail || "see Tools → Jobs for the log").slice(0, 160);
          toast("extend failed — see the note under the button", true);
        }
      }, 1500);
    } catch (e) {
      hideWorking();
      btn.disabled = false;
      $("#ext-status").textContent = e.message;
      toast(e.message, true);
    }
  }

  function extShowResult(name, src, secs) {
    var box = $("#ext-result");
    box.innerHTML = '<div class="card"><b>✓ ' + esc(name) + " · " + secs + "s</b>" +
      '<button class="btn ghost sm" id="ext-add">＋ Add to timeline</button>' +
      '<button class="btn go sm" id="ext-dub">🎙 Dub &amp; Lipsync →</button></div>';
    $("#ext-add").onclick = async function () {
      try { await op({ op: "add", src: src, append: true }); toast("added to the timeline"); }
      catch (e) { /* op() reported it */ }
    };
    $("#ext-dub").onclick = function () {
      // the Creator's dub step owns voice cloning, tiers and lipsync
      location.href = "/?v=" + encodeURIComponent(name) + "&step=dub";
    };
  }

  function bindExtend() {
    if (!$("#ext-go")) return;
    $$(".tabs .tab[data-rpanel]").forEach(function (t) {
      t.onclick = function () {
        $$(".tabs .tab[data-rpanel]").forEach(function (x) { x.classList.remove("on"); });
        t.classList.add("on");
        $$("#rpanel-media, #rpanel-extend").forEach(function (p) { p.classList.remove("on"); });
        $("#rpanel-" + t.dataset.rpanel).classList.add("on");
        if (t.dataset.rpanel === "extend") { extRefreshSources(); extShowInfo(); }
      };
    });
    $("#ext-src").onchange = extShowInfo;
    $("#ext-sec").oninput = function () { $("#ext-sec").dataset.touched = "1"; };
    $("#ext-go").onclick = runExtend;
  }


  /* ── 🎭 face swap: change the actor, keep the performance ──
   * Avatars are a global bank (like the Voice Bank): save a face once — from an
   * image or grabbed off the preview frame — and reuse it in every video. The
   * swap runs the video-face-swap skill's script, which carries the audio
   * conform + shot chunking + seam fixes; the result swaps into the clip with
   * the original kept revertible.
   */
  var FS = { avatars: [], sel: null };

  async function fsLoad() {
    try {
      var d = await api("/api/seq/avatars");
      FS.avatars = d.avatars || [];
    } catch (e) { FS.avatars = []; }
    fsRender();
  }

  function fsRender() {
    var box = $("#fs-avatars");
    if (!box) return;
    if (!FS.avatars.length) {
      box.innerHTML = '<span class="lblhint">none yet — add an image or grab one from the preview</span>';
      return;
    }
    box.innerHTML = FS.avatars.map(function (a) {
      return '<div class="r' + (FS.sel === a.id ? " sel" : "") + '" data-av="' + a.id +
        '" title="' + esc(a.name) + '"><img src="/media/' + esc(a.image) + '" alt="">' +
        '<button data-avdel="' + a.id + '">✕</button>' +
        '<span class="nm">' + esc(a.name) + "</span></div>";
    }).join("");
    $$("#fs-avatars .r").forEach(function (el) {
      el.onclick = function (e) {
        if (e.target.dataset.avdel) return;
        FS.sel = el.dataset.av;
        fsRender();
      };
    });
    fsPreview();
    $$("#fs-avatars [data-avdel]").forEach(function (b) {
      b.onclick = async function (e) {
        e.stopPropagation();
        try {
          await post("/api/seq/avatars/delete", { id: b.dataset.avdel });
          if (FS.sel === b.dataset.avdel) FS.sel = null;
          fsLoad();
        } catch (err) { toast(err.message, true); }
      };
    });
  }

  function fsPreview() {
    var box = $("#fs-preview"), img = $("#fs-preview-img"), lab = $("#fs-preview-label");
    if (!box) return;
    var a = FS.avatars.filter(function (x) { return x.id === FS.sel; })[0];
    if (!a) {
      box.classList.remove("has");
      lab.textContent = FS.avatars.length
        ? "pick an avatar to see the face you're swapping in"
        : "no avatars yet — add, grab or ✨ make one";
      return;
    }
    box.classList.add("has");
    img.src = "/media/" + a.image;
    lab.textContent = "🎭 swapping in: " + a.name;
  }

  async function fsAddImage(file) {
    var fd = new FormData();
    fd.append("image", file);
    fd.append("name", ($("#fs-name").value || "").trim() || file.name.replace(/\.[^.]+$/, ""));
    try {
      var d = await api("/api/seq/avatars/create", { method: "POST", body: fd });
      FS.sel = d.id;
      $("#fs-name").value = "";
      toast("avatar saved — reusable in every video");
      fsLoad();
    } catch (e) { toast(e.message, true); }
  }

  async function fsGrabFrame() {
    var v = $("#vSrc");
    var src = v && v.dataset.src ? v.dataset.src.replace(/^\/media\//, "") : null;
    if (!src) return toast("no clip in the preview to grab from", true);
    try {
      var d = await post("/api/seq/avatars/create", {
        from_video: decodeURIComponent(src),
        at: v.currentTime || 0,
        name: ($("#fs-name").value || "").trim() || "grabbed face",
      });
      FS.sel = d.id;
      $("#fs-name").value = "";
      toast("face grabbed from the preview frame");
      fsLoad();
    } catch (e) { toast(e.message, true); }
  }

  async function fsGenerate() {
    var prompt = ($("#fs-genprompt").value || "").trim();
    if (!prompt) return toast("describe the face first", true);
    var body = { prompt: prompt, name: ($("#fs-name").value || "").trim() };
    var est;
    try {
      await post("/api/seq/avatars/generate", body);
      return toast("unexpected: the server skipped the price check", true);
    } catch (e) {
      if (e.status === 402 && e.data && e.data.estimate) est = e.data.estimate;
      else return toast(e.message, true);
    }
    var node = chatAdd("bot", "🎭 " + esc(est.summary));
    var box = document.createElement("div");
    box.className = "propose";
    box.innerHTML = '<div class="p1">Generates a new face and saves it to the avatar bank.</div>' +
      '<button class="btn go sm">✨ Make it (~$0.04)</button> ' +
      '<button class="btn ghost sm">Cancel</button>';
    node.appendChild(box);
    var btns = box.querySelectorAll("button");
    btns[1].onclick = function () { box.remove(); };
    btns[0].onclick = async function () {
      box.remove();
      body.confirm_cost = true;
      $("#fs-gen").disabled = true;
      try {
        var d = await post("/api/seq/avatars/generate", body);
        var timer = setInterval(async function () {
          var j;
          try { j = await api("/api/job/" + d.job_id); }
          catch (e2) { clearInterval(timer); $("#fs-gen").disabled = false; return; }
          if (j.status === "running") return;
          clearInterval(timer);
          $("#fs-gen").disabled = false;
          if (j.status === "done") {
            $("#fs-genprompt").value = "";
            $("#fs-name").value = "";
            FS.sel = d.id;
            toast("✓ avatar made — that's the face you'll swap in");
            fsLoad();
          } else {
            var tail = friendlyTail(j.lines, 2).join(" · ");
            toast("avatar failed: " + (tail || "see Tools → Jobs").slice(0, 160), true);
          }
        }, 1500);
      } catch (e3) {
        toast(e3.message, true);
        $("#fs-gen").disabled = false;
      }
    };
    $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  }

  function fsEngineChanged() {
    var eng = $("#fs-engine").value;
    $("#fs-res").innerHTML = (eng === "pixverse"
      ? ["720p", "540p"] : ["720p", "480p"]).map(function (r) {
        return "<option>" + r + "</option>"; }).join("");
  }

  async function fsRun() {
    if (!S.slug) return toast("open a project first", true);
    var target = defaultClipId();
    if (!target) return toast("add a video to the timeline first", true);
    if (!FS.sel) return toast("pick an avatar (the new face) first", true);
    var body = { clip: target, avatar: FS.sel, engine: $("#fs-engine").value,
                 resolution: $("#fs-res").value };
    var est;
    try {
      await post("/api/seq/" + S.slug + "/faceswap", body);
      return toast("unexpected: the server skipped the price check", true);
    } catch (e) {
      if (e.status === 402 && e.data && e.data.estimate) est = e.data.estimate;
      else return toast(e.message, true);
    }
    var node = chatAdd("bot", "🎭 " + esc(est.summary) +
      "<br>The original clip stays revertible from the Inspector.");
    var box = document.createElement("div");
    box.className = "propose";
    box.innerHTML = '<div class="p1">This spends real money on fal.ai.</div>' +
      '<button class="btn go sm">Confirm &amp; swap</button> ' +
      '<button class="btn ghost sm">Cancel</button>';
    node.appendChild(box);
    var btns = box.querySelectorAll("button");
    btns[1].onclick = function () { box.remove(); };
    btns[0].onclick = async function () {
      box.remove();
      body.confirm_cost = true;
      $("#fs-go").disabled = true;
      markBusyClip(body.clip);
      showWorking("Face swap — " + body.engine, "keeping the performance, changing the actor");
      try {
        var d = await post("/api/seq/" + S.slug + "/faceswap", body);
        watchGenerate(d.job_id, body.clip);       // same completion path as generate
      } catch (e2) {
        toast(e2.message, true);
        hideWorking();
      }
      $("#fs-go").disabled = false;
    };
    $("#chatlog").scrollTop = $("#chatlog").scrollHeight;
  }

  function bindFaceSwap() {
    if (!$("#fs-go")) return;
    $("#fs-file").onchange = function (e) {
      if (e.target.files[0]) fsAddImage(e.target.files[0]);
      e.target.value = "";
    };
    $("#fs-grab").onclick = fsGrabFrame;
    $("#fs-gen").onclick = fsGenerate;
    $("#fs-desc").onclick = async function () {
      var v = $("#vSrc");
      var src = v && v.dataset.src ? v.dataset.src.replace(/^\/media\//, "") : null;
      if (!src) return toast("no clip in the preview to describe", true);
      var btn = $("#fs-desc");
      btn.disabled = true;
      btn.textContent = "🔍 looking…";
      try {
        var d = await post("/api/seq/avatars/describe",
          { from_video: decodeURIComponent(src), at: v.currentTime || 0 });
        $("#fs-genprompt").value = d.description;
        chatAdd("bot", "🔍 <b>Face:</b> " + esc(d.face) +
          (d.wearing ? "<br><b>Wearing:</b> " + esc(d.wearing) : "") +
          (d.background ? "<br><b>Background:</b> " + esc(d.background) : "") +
          "<br>All three are in the Make box — edit what should change, " +
          "then ✨ Make to generate the avatar.");
        toast("description ready — edit it, then ✨ Make");
      } catch (e) {
        toast(e.message, true);
      }
      btn.disabled = false;
      btn.textContent = "🔍 Describe";
    };
    $("#fs-engine").onchange = fsEngineChanged;
    $("#fs-go").onclick = fsRun;
    $("#fs-box").addEventListener("toggle", function () {
      if ($("#fs-box").open && !FS.avatars.length) fsLoad();
    });
  }

  function bindAI() {
    bindUpload();
    bindExtend();
    bindFaceSwap();
    // scoped to the LEFT pane — the right pane has its own tab set
    $$(".tabs .tab[data-panel]").forEach(function (t) {
      t.onclick = function () {
        $$(".tabs .tab[data-panel]").forEach(function (x) { x.classList.remove("on"); });
        t.classList.add("on");
        $$(".pane.left .panel").forEach(function (p) { p.classList.remove("on"); });
        $("#panel-" + t.dataset.panel).classList.add("on");
        if (t.dataset.panel === "script" && !SCRIPT.flat.length) loadTranscript();
      };
    });
    $("#btn-send").onclick = sendChat;
    $("#chatbox").addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });
    $("#btn-generate").onclick = function () { runGenerate(null); };
    $("#ref-file").onchange = function (e) { uploadRefs(e.target.files); e.target.value = ""; };
    $(".reflink").onclick = function () { $("#ref-file").click(); };
    $("#gen-sec").oninput = updateEstimate;
    $("#gen-res").onchange = updateEstimate;
    loadModels();
  }


  /* ═════════════════════ script panel: edit the video by its words ═════════════════════
   * Whisper words arrive already mapped to TIMELINE seconds; deleting a span is
   * an ordinary ripple_delete. One primitive under transcript editing, silence
   * removal and manual razor work alike.
   */

  var SCRIPT = { clips: [], flat: [], selA: null, selB: null, version: 0, watching: false };

  function scriptHint(msg) { $("#script-hint").textContent = msg; }

  async function loadTranscript() {
    if (!S.slug) return;
    var d;
    try { d = await api("/api/seq/" + S.slug + "/transcript"); }
    catch (e) { return scriptHint(e.message); }
    SCRIPT.clips = d.clips || [];
    SCRIPT.selA = SCRIPT.selB = null;
    renderScript(d.missing || []);
  }

  function renderScript(missing) {
    var body = $("#script-body");
    SCRIPT.flat = [];
    var html = "";
    SCRIPT.clips.forEach(function (c) {
      if (!c.words.length && !c.ready) return;
      html += '<div class="sc-clip">' + esc(String(c.src).split("/").pop()) +
              " · " + fmt(c.start) + "</div>";
      c.words.forEach(function (w) {
        var i = SCRIPT.flat.length;
        SCRIPT.flat.push({ w: w.w, s: w.s, e: w.e, track: c.track });
        html += '<span class="w" data-i="' + i + '">' + esc(w.w) + "</span>";
      });
      if (c.ready && !c.words.length) html += '<span class="empty" style="padding:4px">(no speech found)</span>';
    });
    if (!SCRIPT.flat.length && !html) {
      body.innerHTML = '<div class="empty">' + (missing && missing.length
        ? missing.length + " source(s) need transcribing — press 🎙 Transcribe"
        : "no video clips yet") + "</div>";
      $("#btn-delwords").disabled = true;
      return;
    }
    body.innerHTML = html;
    $$("#script-body .w").forEach(function (el) {
      el.onclick = function () {
        var i = +el.dataset.i;
        if (SCRIPT.selA == null || SCRIPT.selB != null) { SCRIPT.selA = i; SCRIPT.selB = null; }
        else { SCRIPT.selB = i; if (SCRIPT.selB < SCRIPT.selA) { var t = SCRIPT.selA; SCRIPT.selA = SCRIPT.selB; SCRIPT.selB = t; } }
        paintSelection();
        seek(SCRIPT.flat[i].s);              // hear where you are
      };
    });
    paintSelection();
    if (missing && missing.length) scriptHint(missing.length + " more source(s) still need transcribing");
  }

  function paintSelection() {
    var a = SCRIPT.selA, b = SCRIPT.selB == null ? SCRIPT.selA : SCRIPT.selB;
    $$("#script-body .w").forEach(function (el) {
      var i = +el.dataset.i;
      el.classList.toggle("sel", a != null && i >= a && i <= b);
    });
    var n = a == null ? 0 : b - a + 1;
    $("#btn-delwords").disabled = !n;
    $("#btn-delwords").textContent = n ? "🗑 Delete " + n + " word" + (n > 1 ? "s" : "") : "🗑 Delete words";
  }

  async function deleteWords() {
    var a = SCRIPT.selA, b = SCRIPT.selB == null ? SCRIPT.selA : SCRIPT.selB;
    if (a == null) return;
    var first = SCRIPT.flat[a], last = SCRIPT.flat[b];
    try {
      await op({ op: "ripple_delete", track: first.track,
                 a: Math.max(0, first.s - 0.04), b: last.e + 0.04 });
      toast("cut " + (b - a + 1) + " words — video rippled");
      loadTranscript();
    } catch (e) { /* op() reported it */ }
  }

  async function transcribeMissing() {
    if (!S.slug) return;
    var d;
    try { d = await post("/api/seq/" + S.slug + "/transcribe", {}); }
    catch (e) { return toast(e.message, true); }
    if (!d.jobs.length) { toast("transcripts are ready"); return loadTranscript(); }
    scriptHint("transcribing " + d.jobs.length + " source(s)… (GPU queue, watch the spinner)");
    $("#btn-transcribe").disabled = true;
    var pending = d.jobs.map(function (j) { return j.job_id; });
    var timer = setInterval(async function () {
      var left = [];
      for (var i = 0; i < pending.length; i++) {
        try {
          var j = await api("/api/job/" + pending[i]);
          if (j.status === "running") left.push(pending[i]);
        } catch (e) { /* treat as finished */ }
      }
      pending = left;
      if (!pending.length) {
        clearInterval(timer);
        $("#btn-transcribe").disabled = false;
        scriptHint("done — click a word, click another, Delete ripples the video");
        loadTranscript();
      }
    }, 3000);
  }

  async function findSilences() {
    if (!S.slug) return;
    scriptHint("scanning for silences…");
    var d;
    try { d = await post("/api/seq/" + S.slug + "/silence", {}); }
    catch (e) { return scriptHint(e.message); }
    if (!d.proposals.length) return scriptHint("no silences ≥0.6s found — already tight");
    var body = $("#script-body");
    var box = document.createElement("div");
    box.className = "props";
    box.innerHTML = "<b>" + d.proposals.length + " silent span(s), " +
      d.proposals.reduce(function (s, p) { return s + p.len; }, 0).toFixed(1) + "s total</b><br>" +
      d.proposals.map(function (p) {
        return '<div class="pr">' + fmt(p.a) + " → " + fmt(p.b) + " <b>(" + p.len + "s)</b></div>";
      }).join("") +
      '<div style="margin-top:6px"><button class="btn go sm">✂ Cut them all</button> ' +
      '<button class="btn ghost sm">dismiss</button></div>';
    body.insertBefore(box, body.firstChild);
    var btns = box.querySelectorAll("button");
    btns[0].onclick = async function () {
      try {
        var r = await post("/api/seq/" + S.slug + "/silence",
                           { apply: true, ranges: d.proposals, version: d.version });
        applyDoc(r);
        toast("cut " + r.applied + " silent spans");
        loadTranscript();
      } catch (e) { toast(e.message, true); }
    };
    btns[1].onclick = function () { box.remove(); };
    scriptHint("review the spans — Cut them all ripples every gap closed");
  }

  function bindScript() {
    $("#btn-transcribe").onclick = transcribeMissing;
    $("#btn-silence").onclick = findSilences;
    $("#btn-delwords").onclick = deleteWords;
  }

  /* ═════════════════════ filmstrip thumbnails on clips ═════════════════════ */

  async function loadThumbs() {
    var srcs = Object.keys(S.sources).filter(function (s) {
      var i = S.sources[s];
      return i && !i.missing && i.kind === "video";
    });
    for (var k = 0; k < srcs.length; k++) {
      var s = srcs[k];
      if (S.thumbs[s] !== undefined) continue;
      S.thumbs[s] = null;
      try {
        var d = await api("/api/qc/frames?path=" + encodeURIComponent(s) + "&count=10");
        S.thumbs[s] = (d.frames || []).map(function (f) {
          return "/media/" + String(f.path).replace(/^\/+/, "");
        });
        applyThumbs();
      } catch (e) { /* label-only clip is fine */ }
    }
  }

  function applyThumbs() {
    $$("#lanes .clip.kind-video").forEach(function (el) {
      var hit = findClip(el.dataset.clip);
      if (!hit) return;
      var arr = S.thumbs[hit.c.src], info = S.sources[hit.c.src];
      if (!arr || !arr.length || !info || !info.dur) return;
      var mid = ((hit.c.in + hit.c.out) / 2) / info.dur;
      var idx = Math.max(0, Math.min(arr.length - 1, Math.round(mid * (arr.length - 1))));
      el.style.backgroundImage =
        "linear-gradient(rgba(10,14,24,.3),rgba(10,14,24,.62)),url('" + arr[idx] + "')";
      el.style.backgroundSize = "cover";
      el.style.backgroundPosition = "center";
    });
  }


  /* ───────────────────────── adjustable layout ─────────────────────────
   * Three splitters: AI pane | player | media, plus timeline height.
   * Drag to taste, sizes persist in localStorage, double-click resets.
   */
  function initLayout() {
    var app = $("#tl-app");
    var saved = {};
    try { saved = JSON.parse(localStorage.vsTlLayout || "{}"); } catch (e) { saved = {}; }

    var VARS = { wl: "--wl", wr: "--wr", tlh: "--tlh" };
    function apply() {
      Object.keys(VARS).forEach(function (k) {
        if (saved[k]) app.style.setProperty(VARS[k], saved[k] + "px");
      });
      renderOverlay();
    }
    apply();

    function current(key) {
      if (key === "wl") return $(".pane.left").offsetWidth;
      if (key === "wr") return $(".pane.right").offsetWidth;
      return $("#tl").offsetHeight;
    }

    function bind(id, key, min, max, horiz, invert) {
      var el = $(id);
      if (!el) return;
      el.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        el.classList.add("dragging");
        try { el.setPointerCapture(e.pointerId); } catch (err) { /* synthetic */ }
        var startPos = horiz ? e.clientY : e.clientX;
        var base = current(key);
        var mv = function (ev) {
          var d = (horiz ? ev.clientY : ev.clientX) - startPos;
          if (invert) d = -d;
          saved[key] = Math.round(Math.max(min, Math.min(max, base + d)));
          apply();
        };
        var up = function () {
          el.classList.remove("dragging");
          window.removeEventListener("pointermove", mv);
          window.removeEventListener("pointerup", up);
          try { localStorage.vsTlLayout = JSON.stringify(saved); } catch (err) { /* private mode */ }
        };
        window.addEventListener("pointermove", mv);
        window.addEventListener("pointerup", up);
      });
      el.addEventListener("dblclick", function () {
        delete saved[key];
        app.style.removeProperty(VARS[key]);
        try { localStorage.vsTlLayout = JSON.stringify(saved); } catch (err) { /* ignore */ }
        renderOverlay();
      });
    }

    bind("#sp-left", "wl", 250, 640, false, false);
    bind("#sp-right", "wr", 140, 480, false, true);           // drag left = wider media
    bind("#sp-tl", "tlh", 110, Math.max(300, window.innerHeight * 0.6), true, true);
  }

  /* ───────────────────────── boot ───────────────────────── */

  function fitHeight() {
    var nav = document.getElementById("vs-shell");
    var h = nav ? nav.offsetHeight : 0;
    $("#tl-app").style.height = (window.innerHeight - h) + "px";
  }

  function bindChrome() {
    $("#btn-play").onclick = function () { S.playing ? pause() : play(); };
    $("#btn-start").onclick = function () { seek(0); };
    $("#btn-end").onclick = function () { seek(S.dur); };
    $("#btn-split").onclick = doSplit;
    $("#btn-del").onclick = function () { doDelete(false); };
    $("#btn-addtext").onclick = addText;
    $("#btn-addtrack").onclick = function () { op({ op: "track", action: "add", kind: "video" }); };
    $("#btn-render").onclick = function () { doRender(false); };
    $("#btn-draft").onclick = function () { doRender(true); };
    $("#btn-undo").onclick = async function () {
      try { applyDoc(await post("/api/seq/" + S.slug + "/undo", {})); toast("undone"); }
      catch (e) { toast(e.message, true); }
    };
    $("#btn-projects").onclick = function () {
      $("#projmodal").classList.add("open");
      loadProjects();
    };
    $("#proj-close").onclick = function () { $("#projmodal").classList.remove("open"); };
    $("#new-go").onclick = createProject;
    $("#media-search").oninput = function (e) { S.filter = e.target.value; renderMedia(); };
    $("#zoom").oninput = function (e) {
      S.pxs = +e.target.value;
      renderRuler(); renderLanes(); movePlayhead();
    };
    $("#proj-name").onchange = function (e) {
      op({ op: "rename", name: e.target.value });
    };

    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "select" || tag === "textarea") return;
      if (e.code === "Space") { e.preventDefault(); S.playing ? pause() : play(); }
      else if (e.key === "s" || e.key === "S") { e.preventDefault(); doSplit(); }
      else if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault(); doDelete(e.shiftKey);
      } else if (e.key === "ArrowLeft") { seek(S.t - (e.shiftKey ? 1 : 1 / 30)); }
      else if (e.key === "ArrowRight") { seek(S.t + (e.shiftKey ? 1 : 1 / 30)); }
      else if (e.key === "Home") { seek(0); }
      else if (e.key === "End") { seek(S.dur); }
      else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
        e.preventDefault(); $("#btn-undo").click();
      }
    });

    window.addEventListener("resize", function () { fitHeight(); renderOverlay(); });
  }

  async function boot() {
    fitHeight();
    initLayout();
    bindChrome();
    bindTimeline();
    bindAI();
    bindScript();
    try {
      var m = await api("/api/seq/media/list");
      S.media = m.media || [];
      renderMedia();
    } catch (e) { $("#media-list").innerHTML = '<div class="empty">could not list media</div>'; }

    var want = new URLSearchParams(location.search).get("p");
    var projects = await loadProjects();
    if (want) { try { await openProject(want); return; } catch (e) { /* fall through */ } }
    if (projects && projects.length) { await openProject(projects[0].slug); }
    else { $("#projmodal").classList.add("open"); }
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
