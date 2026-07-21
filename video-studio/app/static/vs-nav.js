/* Video Studio shell nav — injected at the top of every tab page.
   One-page era: the Creator at "/" IS the workflow; everything else is a studio. */
(function () {
  var TABS = [
    ["🎬 Creator", "/", ["/"]],
    ["Image → Video", "/image-to-video", ["/image-to-video"]],
    ["Voices", "/voices", ["/voices"]],
    ["Brand Studio", "/brand-studio", ["/brand-studio"]],
    ["Ads Factory", "/creator", ["/creator", "/mission", "/studio"]],
    ["Power Tools", "/tools", ["/tools", "/exports", "/qc-lab", "/dubsync-lab",
                               "/clone-lab", "/subtitles-lab", "/dubbing-lab", "/transcript-lab"]],
    ["Settings", "/settings", ["/settings"]],
  ];

  var path = location.pathname.replace(/\/+$/, "") || "/";
  var bar = document.createElement("nav");
  bar.id = "vs-shell";
  var html = '<a class="vs-logo" href="/"><span class="dot">▶</span>Video <b>Studio</b></a>';
  for (var i = 0; i < TABS.length; i++) {
    var t = TABS[i];
    var on = t[2].indexOf(path) >= 0 ? " on" : "";
    html += '<a class="vs-tab' + on + '" href="' + t[1] + '">' + t[0] + "</a>";
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
  }
  if (document.body) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();
