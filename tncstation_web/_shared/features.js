/*
 * tncstation page features -- shared by every page (inlined by
 * load_page() into <head>).
 *
 * Not every TNC can use every page: the PBBS needs the KAM-XL's built-in
 * mailbox, Winlink has only been written for the KAM-XL, and nothing
 * but monitoring and APRS works in KISS mode. The daemon's status says
 * which apply ("features"); this hides the rest:
 *
 *   data-feature="pbbs"        on a link or card: hidden when the
 *                              station has no PBBS (likewise "winlink",
 *                              "terminal")
 *   data-hide-when-empty       on a card whose rows carry data-feature:
 *                              hidden too once every row is
 *   <body data-page-feature>   on the page itself: a note at the top
 *                              explains why it won't work here
 *   data-two-port-text="..."   replaces the element's text on a TNC with
 *                              two radio ports (the KAM-XL's "VHF Port
 *                              2" wording); the markup has the generic
 *                              text for one-port TNCs
 *
 * It also adds the TNC to the tab title ("tncstation dashboard ·
 * OpenTNC"), so tabs from different stations can be told apart.
 *
 * Fails open: if the status can't be read, everything stays visible.
 */
(function () {
  "use strict";

  var LABELS = {
    pbbs: "PBBS (no built-in mailbox)",
    winlink: "Winlink",
    terminal: "command terminal"
  };

  var token = new URLSearchParams(location.search).get("token") || "";
  var statusUrl = "/status" + (token ? "?token=" + encodeURIComponent(token) : "");

  function apply(status) {
    var tnc = status && (status.tnc_label || status.tnc_model);
    if (tnc) document.title = document.title + " \u00b7 " + tnc;

    if (status && status.radio_ports > 1) {
      var worded = document.querySelectorAll("[data-two-port-text]");
      for (var j = 0; j < worded.length; j++) {
        worded[j].textContent = worded[j].getAttribute("data-two-port-text");
      }
    }

    var features = status && status.features;
    if (!features) return;

    var marked = document.querySelectorAll("[data-feature]");
    for (var i = 0; i < marked.length; i++) {
      if (features[marked[i].getAttribute("data-feature")] === false) {
        marked[i].style.display = "none";
      }
    }

    var groups = document.querySelectorAll("[data-hide-when-empty]");
    for (var g = 0; g < groups.length; g++) {
      var rows = groups[g].querySelectorAll("[data-feature]");
      var shown = 0;
      for (var r = 0; r < rows.length; r++) {
        if (rows[r].style.display !== "none") shown++;
      }
      if (rows.length && !shown) groups[g].style.display = "none";
    }

    var own = document.body.getAttribute("data-page-feature");
    if (own && features[own] === false) {
      var note = document.createElement("div");
      note.className = "featureNote";
      note.setAttribute("role", "status");
      note.style.cssText =
        "margin:12px;padding:10px 12px;border:1px solid var(--t-border-warn);" +
        "color:var(--t-warn);background:var(--t-surface);";
      var mode = status.tnc_mode === "kiss" ? ", KISS mode" : "";
      note.textContent =
        "Not available on this station: its TNC (" + (status.tnc_model || "TNC") +
        mode + ") has no " + (LABELS[own] || own) + ".";
      var header = document.querySelector(".paneHeader");
      if (header && header.parentNode) {
        header.parentNode.insertBefore(note, header.nextSibling);
      } else {
        document.body.insertBefore(note, document.body.firstChild);
      }
    }
  }

  function run() {
    fetch(statusUrl)
      .then(function (res) { return res.json(); })
      .then(function (payload) { if (payload && payload.ok) apply(payload.result); })
      .catch(function () { /* fail open */ });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", run);
  } else {
    run();
  }
})();
