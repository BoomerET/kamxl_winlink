(function () {
  // Packet log: search everything the history database has stored.
  // The search lives in the URL (?callsign=&via=&text=&days=) so the
  // back button and bookmarks work, and other pages can link here.
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";

  function authedUrl(path) {
    if (!token) return path;
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(token);
  }

  document.getElementById("homeLink").href = authedUrl("/");
  document.getElementById("terminalLink").href = authedUrl("/terminal");
  document.getElementById("mapLink").href = authedUrl("/map");
  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");

  var form = document.getElementById("searchForm");
  var fields = {
    callsign: document.getElementById("callsign"),
    via: document.getElementById("via"),
    text: document.getElementById("text"),
    days: document.getElementById("days")
  };
  var statusEl = document.getElementById("status");
  var summaryEl = document.getElementById("summary");
  var resultsEl = document.getElementById("results");
  var moreBtn = document.getElementById("moreBtn");

  var current = null;   // the search being shown
  var lastId = null;    // id of the oldest packet shown (for paging)
  var shown = 0;

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function when(epoch) {
    return new Date(epoch * 1000).toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      second: "2-digit"
    });
  }

  function ago(epoch) {
    var s = Math.max(0, Date.now() / 1000 - epoch);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 172800) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }

  function callLink(call, field, extraClass) {
    return "<a class='call " + (extraClass || "") + "' href='#' data-field='" +
      field + "' data-value='" + escapeHtml(call) + "'>" +
      escapeHtml(call) + "</a>";
  }

  // Highlight the searched text inside an (escaped) payload.
  function highlight(payload, text) {
    var safe = escapeHtml(payload);
    if (!text) return safe;
    var needle = escapeHtml(text).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return safe.replace(new RegExp(needle, "gi"), function (m) {
      return "<mark>" + m + "</mark>";
    });
  }

  function pathHtml(hops) {
    if (!hops || !hops.length) return "<span class='dim'>direct</span>";
    return "via " + hops.map(function (hop) {
      var bare = hop.replace(/\*$/, "");
      if (/^(WIDE|TRACE|RELAY|TCPIP|TCPXX|qA)/i.test(bare)) {
        return escapeHtml(hop);
      }
      return callLink(bare, "via") + (hop !== bare ? "*" : "");
    }).join(",");
  }

  function packetHtml(p) {
    var body = p.payload == null
      ? "<div class='pktBody missing'>(contents not saved — heard before " +
        "the packet log was added)</div>"
      : "<div class='pktBody'>" + highlight(p.payload, current.text) + "</div>";
    return "<div class='pkt'><div class='pktHead'>" +
      "<span class='pktTime'>" + escapeHtml(when(p.received_at)) + "</span>" +
      callLink(p.source, "callsign", "src") +
      "<span class='dim'>&gt;" + escapeHtml(p.destination || "") + "</span>" +
      "<span class='pktPath'>" + pathHtml(p.digipeaters) +
      (p.port !== 2 ? " · port " + escapeHtml(p.port) : "") + "</span>" +
      (p.setup ? "<span class='pktSetup'>" + escapeHtml(p.setup) + "</span>" : "") +
      "</div>" + body + "</div>";
  }

  function summaryHtml(s) {
    if (!s.packets) {
      return "<h2>" + escapeHtml(s.callsign) + "</h2>Not heard in this period.";
    }
    var via = s.via.length
      ? s.via.map(function (v) {
          return callLink(v.callsign, "via") + " (" + v.packets + ")";
        }).join(", ")
      : "<span class='dim'>none</span>";
    var html = "<h2>" + escapeHtml(s.callsign) + "</h2>" +
      "<span class='label'>Last heard:</span> " +
      escapeHtml(when(s.last_heard)) + " <span class='dim'>(" +
      escapeHtml(ago(s.last_heard)) + ")</span><br>" +
      "<span class='label'>First heard:</span> " +
      escapeHtml(when(s.first_heard)) + "<br>" +
      "<span class='label'>Packets:</span> " + s.packets +
      " <span class='dim'>(" + s.direct + " heard direct)</span><br>" +
      "<span class='label'>Heard through:</span> " + via;
    if (s.sources.length > 1) {
      html += "<br><span class='label'>SSIDs:</span> " +
        s.sources.map(function (c) {
          return callLink(c.callsign, "callsign") + " (" + c.packets + ")";
        }).join(", ");
    }
    var mapCall = s.sources.length ? s.sources[0].callsign : s.callsign;
    html += "<br><a href='" + escapeHtml(authedUrl(
      "/map?station=" + encodeURIComponent(mapCall))) + "'>Show " +
      escapeHtml(mapCall) + " on the map</a>";
    return html;
  }

  function readForm() {
    return {
      callsign: fields.callsign.value.trim().toUpperCase(),
      via: fields.via.value.trim().toUpperCase(),
      text: fields.text.value.trim(),
      days: fields.days.value
    };
  }

  function fillForm(search) {
    fields.callsign.value = search.callsign || "";
    fields.via.value = search.via || "";
    fields.text.value = search.text || "";
    fields.days.value = search.days || "7";
    if (!fields.days.value) fields.days.value = "7";
  }

  function queryString(search, beforeId) {
    var q = new URLSearchParams();
    ["callsign", "via", "text"].forEach(function (k) {
      if (search[k]) q.set(k, search[k]);
    });
    q.set("days", search.days);
    if (beforeId != null) q.set("before_id", beforeId);
    return q.toString();
  }

  function remember(search, push) {
    var q = new URLSearchParams(queryString(search));
    if (token) q.set("token", token);
    var url = location.pathname + "?" + q.toString();
    try {
      if (push) history.pushState(search, "", url);
      else history.replaceState(search, "", url);
    } catch (e) { /* harmless */ }
  }

  async function run(search, beforeId) {
    var paging = beforeId != null;
    if (!paging) {
      current = search;
      shown = 0;
      lastId = null;
      resultsEl.innerHTML = "";
      summaryEl.hidden = true;
    }
    moreBtn.disabled = true;
    statusEl.className = "";
    statusEl.textContent = paging ? "Loading older packets..." : "Searching...";
    try {
      var res = await fetch(authedUrl("/packets/search?" +
        queryString(search, beforeId)));
      var payload = await res.json();
      if (!payload.ok) {
        throw new Error((payload.error && payload.error.message) ||
          "Search failed");
      }
      var result = payload.result;
      if (search !== current) return;  // a newer search started

      if (result.summary) {
        summaryEl.innerHTML = summaryHtml(result.summary);
        summaryEl.hidden = false;
      }
      resultsEl.insertAdjacentHTML("beforeend",
        result.packets.map(packetHtml).join(""));
      shown += result.packets.length;
      if (result.packets.length) {
        lastId = result.packets[result.packets.length - 1].id;
      }
      moreBtn.hidden = !result.more;
      moreBtn.disabled = false;

      var period = fields.days.options[fields.days.selectedIndex].text;
      statusEl.textContent = shown
        ? "Showing " + shown + (result.more ? " most recent" : "") +
          " packet" + (shown === 1 ? "" : "s") +
          (search.days === "0" ? "" : " from the last " + period) + "."
        : "No packets found" +
          (search.days === "0" ? "." : " in the last " + period + ".");
    } catch (err) {
      statusEl.className = "err";
      statusEl.textContent = String(err.message || err);
      moreBtn.disabled = false;
    }
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    var search = readForm();
    remember(search, true);
    run(search);
  });

  document.getElementById("clearBtn").addEventListener("click", function () {
    fillForm({ days: fields.days.value });
    var search = readForm();
    remember(search, true);
    run(search);
    fields.callsign.focus();
  });

  moreBtn.addEventListener("click", function () {
    if (current && lastId != null) run(current, lastId);
  });

  // Click a callsign or digipeater to search for it.
  resultsEl.parentNode.addEventListener("click", function (e) {
    var link = e.target.closest("a.call");
    if (!link) return;
    e.preventDefault();
    var search = readForm();
    var field = link.getAttribute("data-field");
    search.callsign = "";
    search.via = "";
    search[field] = link.getAttribute("data-value");
    fillForm(search);
    remember(search, true);
    run(search);
    window.scrollTo(0, 0);
  });

  window.addEventListener("popstate", function (e) {
    var search = e.state || {
      callsign: "", via: "", text: "", days: "7"
    };
    fillForm(search);
    run(readForm());
  });

  // Start with whatever the URL asked for (or everything, last 7 days).
  var initial = {
    callsign: (params.get("callsign") || "").toUpperCase(),
    via: (params.get("via") || "").toUpperCase(),
    text: params.get("text") || "",
    days: params.get("days") || "7"
  };
  fillForm(initial);
  initial = readForm();
  remember(initial, false);
  run(initial);
})();
