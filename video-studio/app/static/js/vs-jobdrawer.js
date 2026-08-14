/* vs-jobdrawer.js — bottom job console. Steps call VS.drawer.watch(jobId).
   Only the focused job's log lines are ever fetched (1s offset polling);
   other running jobs surface through the /api/jobs badges.
   The log shows human lines by default (VS.friendlyLine); "raw" flips to the
   full debug log. A failed job renders its classified error as a banner. */
(function () {
  const VS = window.VS;
  let focused = null;      // job id
  let offset = 0;
  let timer = null;
  let lines = [];          // full raw log for the focused job
  let raw = false;         // false = friendly view

  function el(id) { return VS.$("#" + id); }

  function show() { el("vs-drawer").classList.add("on"); }
  function hide() { el("vs-drawer").classList.remove("on"); el("vs-drawer").classList.remove("exp"); }

  function banner() {
    let b = el("vs-derr");
    if (!b) {
      b = document.createElement("div");
      b.id = "vs-derr";
      b.style.cssText = "display:none;padding:8px 12px;background:#3a1518;color:#ffb4b4;" +
        "border-left:3px solid #e5484d;font-size:13px;line-height:1.5";
      el("vs-dlog").before(b);
    }
    return b;
  }

  function renderError(err) {
    const b = banner();
    if (!err) { b.style.display = "none"; return; }
    b.innerHTML = `<b>${VS.esc(err.title)}</b>` +
      (err.fix ? `<br><span style="color:#e8c8c8">Fix: ${VS.esc(err.fix)}</span>` : "");
    b.style.display = "";
  }

  function rawToggle() {
    let t = el("vs-draw");
    if (!t) {
      t = document.createElement("span");
      t.id = "vs-draw";
      t.className = "vs-hint";
      t.style.cursor = "pointer";
      t.title = "toggle full debug log";
      el("vs-dtoggle").before(t);
      t.onclick = e => {
        e.stopPropagation();
        raw = !raw;
        repaint();
      };
    }
    t.textContent = raw ? "friendly" : "raw";
    return t;
  }

  function appendLine(log, line) {
    const shown = raw ? line : VS.friendlyLine(line);
    if (!shown) return;
    const d = document.createElement("div");
    d.textContent = shown;
    if (/error|failed|traceback/i.test(shown)) d.className = "err";
    log.appendChild(d);
  }

  function repaint() {
    rawToggle();
    const log = el("vs-dlog");
    log.innerHTML = "";
    for (const line of lines) appendLine(log, line);
    while (log.children.length > 400) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  }

  function renderStrip(job) {
    el("vs-dlbl").textContent = job.label || job.action || "job";
    const p = job.progress || {};
    const pct = p.pct != null ? p.pct : (job.status === "running" ? null : 100);
    el("vs-dfill").style.width = (pct != null ? pct : 6) + "%";
    el("vs-dpct").textContent = p.label || (pct != null ? pct + "%" : "");
    el("vs-dspin").textContent = job.status === "running" ? "⏳"
      : job.status === "done" ? "✅" : "❌";
    el("vs-dstop").style.display = job.status === "running" ? "" : "none";
  }

  async function pollLog() {
    if (!focused) return;
    let j;
    try { j = await VS.api(`/api/job/${focused}?offset=${offset}`); }
    catch (e) { return; }
    if (j.lines && j.lines.length) {
      const log = el("vs-dlog");
      for (const line of j.lines) {
        lines.push(line);
        appendLine(log, line);
      }
      while (log.children.length > 400) log.removeChild(log.firstChild);
      log.scrollTop = log.scrollHeight;
    }
    offset = j.next_offset != null ? j.next_offset : offset + (j.lines || []).length;
    renderStrip(j);
    renderError(j.error);
    if (j.status !== "running") {
      clearInterval(timer);
      timer = null;
      const cost = j.cost;
      if (j.status === "done") {
        if (cost && cost.free) VS.toast(`✅ Done — FREE (local). fal total $${(cost.total || 0).toFixed(2)}`);
        else if (cost && cost.this_run) VS.toast(`💰 Done — ~$${cost.this_run.toFixed(3)} on fal.ai (total $${(cost.total || 0).toFixed(2)})`);
        else VS.toast(`✅ ${j.label || "Job"} finished`);
      } else {
        VS.toast(j.error ? `❌ ${j.error.title}` : `❌ ${j.label || "Job"} ${j.status} — log in the drawer`);
        el("vs-drawer").classList.add("exp");
      }
      VS.emit("drawer-finished", j);
      setTimeout(() => { if (!timer && focused === j.id && j.status === "done") hide(); }, 6000);
    }
  }

  VS.drawer = {
    init() {
      el("vs-dstrip").onclick = e => {
        if (e.target.id === "vs-dstop" || e.target.id === "vs-draw") return;
        el("vs-drawer").classList.toggle("exp");
        el("vs-dtoggle").textContent = el("vs-drawer").classList.contains("exp") ? "▼ hide" : "▲ log";
      };
      el("vs-dstop").onclick = async e => {
        e.stopPropagation();
        if (!focused) return;
        if (!confirm("Stop this job?")) return;
        try { await VS.post(`/api/job/${focused}/stop`); VS.toast("Job stopped"); }
        catch (err) { VS.toast("Stop failed: " + err.message); }
      };
      // auto-attach: when a modal opens and a job is already running for it
      VS.on("modal-open", video => {
        const running = VS.runningFor(video);
        if (running) this.watch(running.id);
      });
      VS.on("modal-close", () => { /* keep watching — job continues in background */ });
    },

    watch(jobId) {
      if (focused === jobId && timer) return;
      focused = jobId;
      offset = 0;
      lines = [];
      el("vs-dlog").innerHTML = "";
      renderError(null);
      rawToggle();
      show();
      clearInterval(timer);
      timer = setInterval(pollLog, 1000);
      pollLog();
    },
  };
})();
