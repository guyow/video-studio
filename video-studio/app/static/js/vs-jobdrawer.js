/* vs-jobdrawer.js — bottom job console. Steps call VS.drawer.watch(jobId).
   Only the focused job's log lines are ever fetched (1s offset polling);
   other running jobs surface through the /api/jobs badges. */
(function () {
  const VS = window.VS;
  let focused = null;      // job id
  let offset = 0;
  let timer = null;
  let lines = [];

  function el(id) { return VS.$("#" + id); }

  function show() { el("vs-drawer").classList.add("on"); }
  function hide() { el("vs-drawer").classList.remove("on"); el("vs-drawer").classList.remove("exp"); }

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
        const d = document.createElement("div");
        d.textContent = line;
        if (/error|failed|traceback/i.test(line)) d.className = "err";
        log.appendChild(d);
      }
      while (log.children.length > 400) log.removeChild(log.firstChild);
      log.scrollTop = log.scrollHeight;
    }
    offset = j.next_offset != null ? j.next_offset : offset + (j.lines || []).length;
    renderStrip(j);
    if (j.status !== "running") {
      clearInterval(timer);
      timer = null;
      const cost = j.cost;
      if (j.status === "done") {
        if (cost && cost.free) VS.toast(`✅ Done — FREE (local). fal total $${(cost.total || 0).toFixed(2)}`);
        else if (cost && cost.this_run) VS.toast(`💰 Done — ~$${cost.this_run.toFixed(3)} on fal.ai (total $${(cost.total || 0).toFixed(2)})`);
        else VS.toast(`✅ ${j.label || "Job"} finished`);
      } else {
        VS.toast(`❌ ${j.label || "Job"} ${j.status} — log in the drawer`);
        el("vs-drawer").classList.add("exp");
      }
      VS.emit("drawer-finished", j);
      setTimeout(() => { if (!timer && focused === j.id && j.status === "done") hide(); }, 6000);
    }
  }

  VS.drawer = {
    init() {
      el("vs-dstrip").onclick = e => {
        if (e.target.id === "vs-dstop") return;
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
      show();
      clearInterval(timer);
      timer = setInterval(pollLog, 1000);
      pollLog();
    },
  };
})();
