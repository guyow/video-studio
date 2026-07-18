/* Video Studio shell nav — injected at the top of every tab page. */
(function () {
  var TABS = [
    ["Library", "/library", ["/library"]],
    ["New Project", "/new", ["/new"]],
    ["Transcript & Script", "/transcript", ["/transcript"]],
    ["Subtitle Recovery", "/subtitles", ["/subtitles", "/eraser", "/recovery"]],
    ["Dubbing & Lip Sync", "/dubbing", ["/dubbing"]],
    ["Clone Winner", "/clone", ["/clone"]],
    ["DubSync Repair", "/dubsync", ["/dubsync"]],
    ["Captions", "/captions", ["/captions"]],
    ["QA Review", "/qc", ["/qc"]],
    ["Exports", "/exports", ["/exports"]],
    ["Brand Studio", "/brand-studio", ["/brand-studio"]],
    ["Ads Factory", "/creator", ["/creator", "/", "/mission", "/studio"]],
    ["Power Tools", "/tools", ["/tools"]],
  ];
  var SOON = {};

  var path = location.pathname.replace(/\/+$/, "") || "/";
  var bar = document.createElement("nav");
  bar.id = "vs-shell";
  var html = '<a class="vs-logo" href="/library"><span class="dot">▶</span>Video <b>Studio</b></a>';
  for (var i = 0; i < TABS.length; i++) {
    var t = TABS[i];
    var on = t[2].indexOf(path) >= 0 ? " on" : "";
    var soon = SOON[t[1]] ? '<span class="soon">soon</span>' : "";
    html += '<a class="vs-tab' + on + '" href="' + t[1] + '">' + t[0] + soon + "</a>";
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
