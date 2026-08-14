/* Video Studio shell nav — injected at the top of every tab page.
 *
 * IA (2026-08-08 cleanup): the old bar was 12 flat tabs; nobody could tell
 * which page did what. Now it follows the actual workflow:
 *
 *   Create (the pipeline) → Edit (the timeline NLE) → Generate (AI studios)
 *   → Brand & Ads → Voices → Tools (labs + legacy) → Settings
 *
 * Every old route is still reachable — grouped, not removed.
 */
(function () {
  // ["Label", href] for a plain tab; {label, items:[[label, href, hint?]...]} for a group
  var NAV = [
    ["🎬 Create", "/", ["/"]],
    ["✂ Edit", "/timeline", ["/timeline"]],
    { label: "✨ Generate", match: ["/commercial", "/image-to-video", "/broll", "/image-editor", "/frame-reader", "/motion-capture"],
      items: [
        ["🤳 UGC Factory", "/commercial", "avatar photo → talking Veo 3 UGC video"],
        ["📺 Text → Commercial AI", "/commercial?mode=cml", "describe the product → finished ad"],
        ["🎞 Image → Video", "/image-to-video", "a still becomes a 10–30s clip"],
        ["🎬 B-Roll Factory", "/broll", "reference → recipe → generated b-roll"],
        ["🖼 Image Editor", "/image-editor", "erase / replace / re-frame stills"],
        ["🔬 Frame Reader", "/frame-reader", "winning UGC → shot-by-shot script"],
        ["🕺 Motion Capture", "/motion-capture", "video or webcam drives a 3D avatar"],
      ] },
    { label: "🏷 Brand & Ads", match: ["/batches", "/brand-studio", "/creator", "/mission", "/studio", "/poster"],
      items: [
        ["📦 Ad Batches", "/batches", "template → copy → production tracker"],
        ["🎨 Brand Studio", "/brand-studio", "brand-locked social statics"],
        ["🖼 Poster Studio", "/poster", "product photo → branded poster"],
        ["🏭 Ads Factory", "/creator", "products, research, VSL builds"],
        ["🚀 Missions", "/mission", "multi-step ad missions"],
        ["🧪 ComfyUI (raw)", "/studio", "direct image generation"],
      ] },
    ["🎙 Voices", "/voices", ["/voices"]],
    { label: "🧰 Tools", match: ["/tools", "/exports", "/editor", "/qc-lab", "/dubsync-lab",
                                 "/clone-lab", "/subtitles-lab", "/dubbing-lab", "/transcript-lab"],
      items: [
        ["⚙ Jobs & Power Tools", "/tools", "job table, spend ledger"],
        ["📤 Exports", "/exports", "everything rendered, send to Desktop"],
        ["🔍 QC Lab", "/qc-lab", "frame-by-frame review"],
        ["🩹 DubSync Lab", "/dubsync-lab", "repair a damaged dub"],
        ["🧬 Clone Lab", "/clone-lab", "scale a proven winner"],
        ["💬 Subtitles Lab", "/subtitles-lab", "erase + recaption"],
        ["🗣 Dubbing Lab", "/dubbing-lab", "voice swap deep-dive"],
        ["📜 Transcript Lab", "/transcript-lab", "transcripts & scripts"],
        ["🎛 Editor β (legacy)", "/editor", "old pipeline dashboard"],
      ] },
    ["⚙ Settings", "/settings", ["/settings"]],
  ];

  var path = location.pathname.replace(/\/+$/, "") || "/";
  var bar = document.createElement("nav");
  bar.id = "vs-shell";
  var html = '<a class="vs-logo" href="/"><span class="dot">▶</span>Video <b>Studio</b></a>';

  for (var i = 0; i < NAV.length; i++) {
    var t = NAV[i];
    if (Array.isArray(t)) {
      var on = t[2].indexOf(path) >= 0 ? " on" : "";
      html += '<a class="vs-tab' + on + '" href="' + t[1] + '">' + t[0] + "</a>";
    } else {
      var groupOn = t.match.indexOf(path) >= 0;
      var menu = "";
      for (var k = 0; k < t.items.length; k++) {
        var it = t.items[k];
        menu += '<a class="vs-mi' + (it[1] === path ? " on" : "") + '" href="' + it[1] + '">' +
                '<span class="mi-l">' + it[0] + "</span>" +
                (it[2] ? '<span class="mi-h">' + it[2] + "</span>" : "") + "</a>";
      }
      html += '<div class="vs-group' + (groupOn ? " on" : "") + '">' +
              '<button class="vs-tab grp' + (groupOn ? " on" : "") + '" type="button">' +
              t.label + ' <span class="car">▾</span></button>' +
              '<div class="vs-menu">' + menu + "</div></div>";
    }
  }
  bar.innerHTML = html;

  function mount() {
    document.body.insertBefore(bar, document.body.firstChild);
    document.title = document.title.replace(/autoVSL(\s+dashboard)?/i, "Video Studio");
    // pages with their own sticky header need to stick below the shell bar
    var h = bar.offsetHeight;
    var st = document.createElement("style");
    st.textContent =
      "body>header{top:" + h + "px !important}" +
      "#jobstrip{top:calc(" + h + "px + 57px) !important}";
    document.head.appendChild(st);

    // click toggles a group (touch/pen); hover handles itself via CSS
    var groups = bar.querySelectorAll(".vs-group");
    for (var g = 0; g < groups.length; g++) {
      (function (grp) {
        grp.querySelector("button").addEventListener("click", function (e) {
          e.stopPropagation();
          for (var j = 0; j < groups.length; j++) {
            if (groups[j] !== grp) groups[j].classList.remove("open");
          }
          grp.classList.toggle("open");
        });
      })(groups[g]);
    }
    document.addEventListener("click", function () {
      for (var j = 0; j < groups.length; j++) groups[j].classList.remove("open");
    });
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
