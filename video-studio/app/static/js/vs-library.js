/* vs-library.js — the Creator front page: upload drop + video grid.
   Click a card → VS.modal.open(video). */
(function () {
  const VS = window.VS;
  let filter = "";

  const STAGE_DOTS = [
    ["transcript", v => v.transcript],
    ["clean", v => v.cleaned || v.no_subs],
    ["script", v => v.script],
    ["dub", v => !!v.dub],
    ["done", v => v.exported],
  ];

  function card(v) {
    const running = VS.runningFor(v);
    const dots = STAGE_DOTS.map(([k, fn]) =>
      `<i class="${fn(v) ? "on" : ""}" title="${k}"></i>`).join("");
    const title = v.title || v.name;
    const badge = running
      ? `<span class="vs-badge run">⏳ ${VS.esc((running.progress && running.progress.label) || running.action)}</span>`
      : v.exported ? '<span class="vs-badge ok">delivered</span>'
      : v.dub ? '<span class="vs-badge dub">dubbed</span>' : "";
    return `<div class="vs-card" data-name="${VS.esc(v.name)}">
      <div class="vs-thumb"><img loading="lazy" src="/api/thumb/${encodeURIComponent(v.name)}" alt="">
        ${badge}</div>
      <div class="vs-cardbody">
        <div class="vs-cardtitle" title="${VS.esc(v.name)}">${VS.esc(title)}</div>
        <div class="vs-cardmeta">
          ${v.character ? `<span>👤 ${VS.esc(v.character)}</span>` : ""}
          <span>${VS.fmtSize(v.size)}</span><span>${VS.fmtAgo(v.mtime)}</span>
        </div>
        <div class="vs-dots">${dots}</div>
      </div>
    </div>`;
  }

  function render() {
    const grid = VS.$("#vs-grid");
    if (!grid) return;
    const q = filter.toLowerCase();
    const vids = VS.state.videos.filter(v =>
      !q || v.name.toLowerCase().includes(q) || (v.title || "").toLowerCase().includes(q)
      || (v.character || "").toLowerCase().includes(q)
      || (v.tags || []).some(t => t.toLowerCase().includes(q)));
    grid.innerHTML = vids.map(card).join("") ||
      `<div class="vs-empty">${VS.state.videos.length
        ? "nothing matches that search"
        : "drop your first video above to start creating"}</div>`;
    VS.$$(".vs-card", grid).forEach(el => {
      el.onclick = () => {
        const v = VS.state.videos.find(x => x.name === el.dataset.name);
        if (v) VS.modal.open(v);
      };
    });
    const n = VS.state.videos;
    VS.$("#vs-chips").innerHTML =
      `<span>${n.length} videos</span>` +
      `<span>${n.filter(v => v.dub).length} dubbed</span>` +
      `<span>${n.filter(v => v.exported).length} delivered</span>` +
      `<span>fal spend $${VS.state.fal_spend.toFixed(2)}</span>`;
  }

  function upload(files) {
    for (const f of files) {
      const fd = new FormData();
      fd.append("file", f);
      const msg = VS.$("#vs-dropmsg");
      const x = new XMLHttpRequest();
      x.open("POST", "/api/upload");
      x.upload.onprogress = e => {
        if (e.lengthComputable) msg.textContent = `Uploading ${f.name} — ${Math.round(e.loaded / e.total * 100)}%`;
      };
      x.onload = async () => {
        msg.textContent = "⬆ Drop a video here or click to upload";
        if (x.status === 200) {
          const res = JSON.parse(x.responseText);
          VS.toast(`Uploaded ${res.name} — transcribing…`);
          VS.post("/api/run", {action: "transcribe", file: res.name}).catch(() => {});
          await VS.refreshLibrary();
          const v = VS.state.videos.find(y => y.name === res.name);
          if (v) VS.modal.open(v);
        } else VS.toast("Upload failed: " + x.responseText.slice(0, 120));
      };
      x.onerror = () => VS.toast("Upload failed");
      x.send(fd);
    }
  }

  VS.library = {
    init() {
      const drop = VS.$("#vs-drop");
      const file = VS.$("#vs-file");
      drop.addEventListener("click", () => file.click());
      file.addEventListener("change", e => { upload(e.target.files); e.target.value = ""; });
      drop.addEventListener("dragover", e => { e.preventDefault(); drop.classList.add("drag"); });
      drop.addEventListener("dragleave", () => drop.classList.remove("drag"));
      drop.addEventListener("drop", e => {
        e.preventDefault(); drop.classList.remove("drag"); upload(e.dataTransfer.files);
      });
      VS.$("#vs-search").addEventListener("input", e => { filter = e.target.value; render(); });
      VS.on("library", render);
      VS.on("jobs", render);
    },
  };
})();
