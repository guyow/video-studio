/* vs-modal.js — the full-screen editing modal: step rail + one expanded body.
   Steps come from VS.registerStep(); bodies lazy-mount on first expand. */
(function () {
  const VS = window.VS;
  const mounted = {};          // stepId -> {el, ctx}
  let saveTimer = null;

  function statusOf(step, video) {
    try {
      return step.status(video, VS.state.jobs, (VS.state.open || {}).details || {}) || "todo";
    } catch (e) { return "todo"; }
  }

  const BADGE = {
    todo: ["", "○"], ready: ["ready", "●"], "needs-you": ["needs", "✍"],
    running: ["run", "⏳"], done: ["done", "✓"], failed: ["bad", "✗"],
  };

  function renderRail() {
    const o = VS.state.open;
    if (!o) return;
    const rail = VS.$("#vs-rail");
    rail.innerHTML = VS.steps.map((s, i) => {
      const st = statusOf(s, o.video);
      const [cls, icon] = BADGE[st] || BADGE.todo;
      return `<div class="vs-railstep ${o.stepId === s.id ? "sel" : ""}" data-step="${s.id}">
        <span class="vs-railnum ${cls}">${icon}</span>
        <span class="vs-railtitle">${i + 1} · ${VS.esc(s.title)}</span>
        <span class="vs-railstate ${cls}">${st === "todo" ? "" : st}</span>
      </div>`;
    }).join("");
    VS.$$(".vs-railstep", rail).forEach(el => {
      el.onclick = () => VS.modal.goto(el.dataset.step);
    });
  }

  function renderHeader() {
    const v = VS.state.open.video;
    VS.$("#vs-mtitle").value = v.title || "";
    VS.$("#vs-mtitle").placeholder = v.name;
    VS.$("#vs-mchar").value = v.character || "";
    VS.$("#vs-mtags").value = (v.tags || []).join(", ");
    VS.$("#vs-mchars-dl").innerHTML =
      VS.state.characters.map(c => `<option value="${VS.esc(c)}">`).join("");
  }

  function saveMeta() {
    const o = VS.state.open;
    if (!o) return;
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => {
      VS.post("/api/creator/meta", {
        name: o.video.name,
        title: VS.$("#vs-mtitle").value.trim(),
        character: VS.$("#vs-mchar").value.trim(),
        tags: VS.$("#vs-mtags").value.split(",").map(t => t.trim()).filter(Boolean),
      }).catch(e => VS.toast("meta save failed: " + e.message));
    }, 400);
  }

  function syncURL() {
    const o = VS.state.open;
    const url = o ? `/?v=${encodeURIComponent(o.video.name)}&step=${o.stepId}` : "/";
    history.replaceState(null, "", url);
  }

  function firstActionable(video) {
    for (const want of [["running"], ["needs-you"], ["ready"]]) {
      for (const s of VS.steps) {
        if (want.includes(statusOf(s, video))) return s.id;
      }
    }
    return VS.steps.length ? VS.steps[VS.steps.length - 1].id : null;
  }

  VS.modal = {
    open(video, stepId) {
      VS.state.open = {video, stepId: null, details: {}};
      VS.$("#vs-modal").classList.add("on");
      document.body.style.overflow = "hidden";
      renderHeader();
      this.goto(stepId || firstActionable(video));
      VS.emit("modal-open", video);
    },

    close() {
      for (const id of Object.keys(mounted)) this._unmount(id);
      VS.state.open = null;
      VS.$("#vs-modal").classList.remove("on");
      document.body.style.overflow = "";
      syncURL();
      VS.emit("modal-close");
    },

    goto(stepId) {
      const o = VS.state.open;
      if (!o || !stepId) return;
      const step = VS.steps.find(s => s.id === stepId);
      if (!step) return;
      if (o.stepId && o.stepId !== stepId) this._unmount(o.stepId);
      o.stepId = stepId;
      renderRail();
      syncURL();
      const body = VS.$("#vs-stepbody");
      body.innerHTML = "";
      const el = document.createElement("div");
      el.className = "vs-stepinner";
      body.appendChild(el);
      const ctx = {video: o.video, el, details: o.details};
      mounted[stepId] = {el, ctx, step};
      step.mount(el, ctx);
      VS.$("#vs-stepbody").scrollTop = 0;
    },

    _unmount(stepId) {
      const m = mounted[stepId];
      if (!m) return;
      try { m.step.unmount && m.step.unmount(m.ctx); } catch (e) { /* ignore */ }
      VS.$$("video", m.el).forEach(v => { v.pause(); v.removeAttribute("src"); v.load(); });
      delete mounted[stepId];
    },

    refresh() {          // re-run sync on the expanded step + rail badges
      const o = VS.state.open;
      if (!o) return;
      renderRail();
      const m = mounted[o.stepId];
      if (m && m.step.sync) { try { m.step.sync(m.ctx); } catch (e) { /* ignore */ } }
    },
  };

  VS.modalInit = () => {
    VS.$("#vs-mclose").onclick = () => VS.modal.close();
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && VS.state.open) VS.modal.close();
    });
    ["vs-mtitle", "vs-mchar", "vs-mtags"].forEach(id =>
      VS.$("#" + id).addEventListener("change", saveMeta));
    VS.on("jobs", () => VS.modal.refresh());
    VS.on("library", () => {
      if (VS.state.open) { renderHeader(); VS.modal.refresh(); }
    });
    // deep link: /?v=name&step=x  or ?new=1
    const q = new URLSearchParams(location.search);
    const want = q.get("v");
    const step = q.get("step");
    const openWhenReady = () => {
      if (want) {
        const v = VS.state.videos.find(x => x.name === want);
        if (v) { VS.modal.open(v, step); return; }
      }
      if (q.get("new") === "1") VS.$("#vs-file").click();
    };
    if (VS.state.videos.length) openWhenReady();
    else { const off = VS.on("library", () => { off(); openWhenReady(); }); }
  };
})();
