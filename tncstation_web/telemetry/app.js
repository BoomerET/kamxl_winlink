(function () {
  // Telemetry graphs: one small chart per analog channel (each has its
  // own units, so never a shared axis), on/off strips for the bits,
  // and a data table. ?station=W5NGU-3&days=7 selects what's shown.
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
  document.getElementById("logLink").href = authedUrl("/log");
  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");

  var daysEl = document.getElementById("days");
  var statusEl = document.getElementById("status");
  var listEl = document.getElementById("stationList");
  var selectEl = document.getElementById("stationSelect");
  var stationEl = document.getElementById("station");
  var tooltip = document.getElementById("tooltip");
  var SVGNS = "http://www.w3.org/2000/svg";

  var selected = (params.get("station") || "").toUpperCase();
  if (params.get("days")) daysEl.value = params.get("days");
  if (!daysEl.value) daysEl.value = "7";
  var current = null;   // the station data being shown

  async function apiGet(path) {
    var res = await fetch(authedUrl(path));
    var payload = await res.json();
    if (!payload.ok) {
      throw new Error((payload.error && payload.error.message) || "Request failed");
    }
    return payload.result;
  }

  function el(tag, attrs, text) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (text != null) node.textContent = text;
    return node;
  }

  function svg(tag, attrs) {
    var node = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    return node;
  }

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  function ago(epoch) {
    var s = Math.max(0, Date.now() / 1000 - epoch);
    if (s < 90) return "just now";
    if (s < 5400) return Math.round(s / 60) + " min ago";
    if (s < 172800) return Math.round(s / 3600) + " h ago";
    return Math.round(s / 86400) + " days ago";
  }

  function when(epoch) {
    return new Date(epoch * 1000).toLocaleString([], {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"
    });
  }

  // Up to 3 significant digits, no exponent for normal ranges.
  function fmt(v) {
    if (v == null || !isFinite(v)) return "—";
    var a = Math.abs(v);
    if (a >= 1000) return Math.round(v).toLocaleString();
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    return v.toFixed(2).replace(/\.?0+$/, "") || "0";
  }

  function remember() {
    var q = new URLSearchParams();
    if (selected) q.set("station", selected);
    q.set("days", daysEl.value);
    if (token) q.set("token", token);
    try { history.replaceState(null, "", location.pathname + "?" + q.toString()); }
    catch (e) { /* harmless */ }
  }

  // -- station list ---------------------------------------------------

  function renderList(stations) {
    listEl.replaceChildren();
    selectEl.replaceChildren();
    stations.forEach(function (s) {
      var btn = el("button", { type: "button", "class": "stationBtn" });
      btn.appendChild(el("span", { "class": "call" }, s.callsign));
      var meta = (s.project ? s.project + " · " : "") + plural(s.reports, "report") + " · " +
        ago(s.last_heard);
      btn.appendChild(el("span", { "class": "meta" }, meta));
      if (s.callsign === selected) btn.setAttribute("aria-current", "true");
      btn.addEventListener("click", function () { choose(s.callsign); });
      listEl.appendChild(btn);

      var opt = el("option", { value: s.callsign }, s.callsign +
        (s.project ? " — " + s.project : ""));
      selectEl.appendChild(opt);
    });
    selectEl.value = selected;
  }

  function choose(callsign) {
    selected = callsign;
    Array.prototype.forEach.call(listEl.children, function (b) {
      b.setAttribute("aria-current",
        b.querySelector(".call").textContent === callsign ? "true" : "false");
    });
    selectEl.value = callsign;
    remember();
    loadStation();
  }

  selectEl.addEventListener("change", function () { choose(selectEl.value); });

  // -- charts ---------------------------------------------------------

  // "Nice" axis ticks covering [lo, hi].
  function ticks(lo, hi, count) {
    if (lo === hi) { lo -= 1; hi += 1; }
    var span = hi - lo;
    var step = Math.pow(10, Math.floor(Math.log10(span / count)));
    var err = span / count / step;
    if (err >= 7.5) step *= 10; else if (err >= 3.5) step *= 5; else if (err >= 1.5) step *= 2;
    var start = Math.floor(lo / step) * step;
    var end = Math.ceil(hi / step) * step;
    var out = [];
    for (var v = start; v <= end + step / 2; v += step) out.push(+v.toFixed(10));
    return out;
  }

  // Split points where the station went quiet (gap > 3x the usual
  // interval), so the line doesn't draw across missing data.
  function segments(points) {
    if (points.length < 3) return [points];
    var gaps = [];
    for (var i = 1; i < points.length; i++) gaps.push(points[i][0] - points[i - 1][0]);
    var sorted = gaps.slice().sort(function (a, b) { return a - b; });
    var limit = Math.max(sorted[Math.floor(sorted.length / 2)] * 3, 900);
    var out = [[points[0]]];
    for (var j = 1; j < points.length; j++) {
      if (points[j][0] - points[j - 1][0] > limit) out.push([]);
      out[out.length - 1].push(points[j]);
    }
    return out;
  }

  function showTip(evt, html) {
    tooltip.replaceChildren();
    html.forEach(function (part) { tooltip.appendChild(part); });
    tooltip.hidden = false;
    var x = evt.clientX + 14, y = evt.clientY - 10;
    var w = tooltip.offsetWidth, h = tooltip.offsetHeight;
    if (x + w > window.innerWidth - 8) x = evt.clientX - w - 14;
    if (y + h > window.innerHeight - 8) y = window.innerHeight - h - 8;
    tooltip.style.left = Math.max(8, x) + "px";
    tooltip.style.top = Math.max(8, y) + "px";
  }

  function hideTip() { tooltip.hidden = true; }

  function lineChart(series, t0, t1, W) {
    var H = 150, L = 46, R = 10, T = 8, B = 20;
    var values = series.points.map(function (p) { return p[1]; });
    var yt = ticks(Math.min.apply(null, values), Math.max.apply(null, values), 3);
    var y0 = yt[0], y1 = yt[yt.length - 1];
    // Tick labels get as many decimals as the tick spacing needs, so
    // 13.80 / 13.82 / 13.84 don't all print as "13.8".
    var stepSize = yt.length > 1 ? Math.abs(yt[1] - yt[0]) : 1;
    var decimals = Math.min(4, Math.max(0, -Math.floor(Math.log10(stepSize) + 1e-9)));
    function tickLabel(v) { return v.toFixed(decimals); }
    var X = function (t) { return L + (t - t0) / Math.max(1, t1 - t0) * (W - L - R); };
    var Y = function (v) { return T + (1 - (v - y0) / (y1 - y0 || 1)) * (H - T - B); };

    var root = svg("svg", { "class": "chart", viewBox: "0 0 " + W + " " + H,
      width: W, height: H, role: "img",
      "aria-label": series.name + " over time" });

    yt.forEach(function (v) {
      root.appendChild(svg("line", { "class": "grid", x1: L, x2: W - R, y1: Y(v), y2: Y(v) }));
      var label = svg("text", { "class": "axis", x: L - 6, y: Y(v) + 3, "text-anchor": "end" });
      label.textContent = tickLabel(v);
      root.appendChild(label);
    });
    (W < 360 ? [t0, t1] : [t0, (t0 + t1) / 2, t1]).forEach(function (t, i, all) {
      var label = svg("text", { "class": "axis", x: X(t), y: H - 5,
        "text-anchor": i === 0 ? "start" : i === all.length - 1 ? "end" : "middle" });
      label.textContent = when(t);
      root.appendChild(label);
    });

    segments(series.points).forEach(function (seg) {
      if (seg.length === 1) {
        root.appendChild(svg("circle", { "class": "dot", cx: X(seg[0][0]), cy: Y(seg[0][1]), r: 3 }));
        return;
      }
      root.appendChild(svg("path", { "class": "line", d: seg.map(function (p, i) {
        return (i ? "L" : "M") + X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1);
      }).join("") }));
    });

    // Hover: crosshair snaps to the nearest report.
    var cross = svg("line", { "class": "cross", y1: T, y2: H - B, visibility: "hidden" });
    var dot = svg("circle", { "class": "dot", r: 4, visibility: "hidden" });
    root.appendChild(cross);
    root.appendChild(dot);
    var hit = svg("rect", { x: L, y: 0, width: W - L - R, height: H, fill: "transparent" });
    root.appendChild(hit);

    function move(evt) {
      var box = root.getBoundingClientRect();
      var t = t0 + ((evt.clientX - box.left) / box.width * W - L) / (W - L - R) * (t1 - t0);
      var best = series.points[0];
      series.points.forEach(function (p) {
        if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      });
      cross.setAttribute("x1", X(best[0])); cross.setAttribute("x2", X(best[0]));
      dot.setAttribute("cx", X(best[0])); dot.setAttribute("cy", Y(best[1]));
      cross.setAttribute("visibility", "visible"); dot.setAttribute("visibility", "visible");
      var value = el("strong", {}, fmt(best[1]) + (series.unit ? " " + series.unit : ""));
      showTip(evt, [value, el("br"), document.createTextNode(series.name + " · " + when(best[0]))]);
    }
    function leave() {
      cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden");
      hideTip();
    }
    hit.addEventListener("pointermove", move);
    hit.addEventListener("pointerdown", move);
    hit.addEventListener("pointerleave", leave);
    return root;
  }

  function bitStrip(bit, t0, t1, W) {
    var H = 14;
    var root = svg("svg", { "class": "strip", viewBox: "0 0 " + W + " " + H,
      width: W, height: H, role: "img", "aria-label": bit.name + " on/off over time" });
    var X = function (t) { return (t - t0) / Math.max(1, t1 - t0) * W; };
    // One bar per run of the same state, 2px gap between runs.
    var runs = [];
    bit.points.forEach(function (p) {
      var last = runs[runs.length - 1];
      if (last && last.on === p[1]) return;
      runs.push({ on: p[1], start: p[0] });
    });
    runs.forEach(function (run, i) {
      var end = i + 1 < runs.length ? runs[i + 1].start : t1;
      var x = X(run.start), w = Math.max(2, X(end) - x - 2);
      var r = svg("rect", { "class": run.on ? "on" : "off", x: x, y: 0, width: w, height: H, rx: 2 });
      r.addEventListener("pointermove", function (evt) {
        showTip(evt, [el("strong", {}, run.on ? "ON" : "OFF"), el("br"),
          document.createTextNode(bit.name + " · " + when(run.start) + " – " + when(end))]);
      });
      r.addEventListener("pointerleave", hideTip);
      root.appendChild(r);
    });
    return root;
  }

  function renderStation(data) {
    current = data;
    stationEl.replaceChildren();
    var head = el("div", { "class": "stationHead" });
    head.appendChild(el("h1", {}, data.callsign + (data.project ? " — " + data.project : "")));
    var sub = el("div", { "class": "sub" });
    sub.appendChild(document.createTextNode(data.reports
      ? plural(data.reports, "report") + " · last " + ago(data.last_heard) + " · "
      : "No telemetry in this period · "));
    var log = el("a", { href: authedUrl("/log?callsign=" + encodeURIComponent(data.callsign) +
      "&text=" + encodeURIComponent("T#") + "&days=" + daysEl.value) }, "packets");
    var map = el("a", { href: authedUrl("/map?station=" + encodeURIComponent(data.callsign)) }, "map");
    sub.appendChild(log); sub.appendChild(document.createTextNode(" · ")); sub.appendChild(map);
    head.appendChild(sub);
    stationEl.appendChild(head);

    if (!data.reports) return;
    if (!data.defined) {
      stationEl.appendChild(el("div", { "class": "note" },
        "This station hasn't sent channel names or scaling (PARM/UNIT/EQNS " +
        "messages) since history began, so these are raw values."));
    }

    var t0 = data.first_heard, t1 = data.last_heard;
    var charts = el("div", { "class": "charts" });
    data.analog.forEach(function (s) {
      var card = el("section", { "class": "chartCard" });
      var title = el("div", { "class": "chartTitle" });
      title.appendChild(el("h2", {}, s.name));
      var latest = el("span", { "class": "latest" }, fmt(s.latest));
      if (s.unit) latest.appendChild(el("span", { "class": "unit" }, s.unit));
      title.appendChild(latest);
      card.appendChild(title);
      card.appendChild(el("div", { "class": "range" },
        "low " + fmt(s.min) + " · high " + fmt(s.max)));
      var holder = el("div", { "class": "plot" });
      holder._draw = function () {
        holder.replaceChildren(lineChart(s, t0, t1, Math.max(200, holder.clientWidth)));
      };
      card.appendChild(holder);
      charts.appendChild(card);
    });
    stationEl.appendChild(charts);

    if (data.bits.length) {
      var bits = el("section", { "class": "bits chartCard" });
      bits.appendChild(el("h2", {}, "On/off"));
      data.bits.forEach(function (b) {
        var row = el("div", { "class": "bitRow" });
        row.appendChild(el("span", {}, b.name));
        var strip = el("div", { "class": "plot" });
        strip._draw = function () {
          strip.replaceChildren(bitStrip(b, t0, t1, Math.max(60, strip.clientWidth)));
        };
        row.appendChild(strip);
        row.appendChild(el("span", { "class": "state" }, b.latest ? "ON" : "OFF"));
        bits.appendChild(row);
      });
      stationEl.appendChild(bits);
    }

    // Table view: every value without hovering (newest 100 reports).
    var details = el("details");
    details.appendChild(el("summary", {}, "Data table"));
    var wrap = el("div", { "class": "tableWrap" });
    var table = el("table");
    var hr = el("tr");
    hr.appendChild(el("th", {}, "Time"));
    data.analog.forEach(function (s) {
      hr.appendChild(el("th", {}, s.name + (s.unit ? " (" + s.unit + ")" : "")));
    });
    var thead = el("thead"); thead.appendChild(hr); table.appendChild(thead);
    var times = {};
    data.analog.forEach(function (s, i) {
      s.points.forEach(function (p) { (times[p[0]] = times[p[0]] || {})[i] = p[1]; });
    });
    var tbody = el("tbody");
    Object.keys(times).map(Number).sort(function (a, b) { return b - a; })
      .slice(0, 100).forEach(function (t) {
        var tr = el("tr");
        tr.appendChild(el("td", {}, when(t)));
        data.analog.forEach(function (s, i) { tr.appendChild(el("td", {}, fmt(times[t][i]))); });
        tbody.appendChild(tr);
      });
    table.appendChild(tbody);
    wrap.appendChild(table);
    details.appendChild(wrap);
    stationEl.appendChild(details);
    drawPlots();
  }

  // Charts are drawn at their real pixel width (so text isn't
  // stretched), after layout, and again when the window resizes.
  function drawPlots() {
    stationEl.querySelectorAll(".plot").forEach(function (p) { p._draw(); });
  }
  var resizeTimer = null;
  window.addEventListener("resize", function () {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(drawPlots, 150);
  });

  async function loadStation() {
    if (!selected) return;
    stationEl.style.opacity = "0.5";   // keep the frame while refetching
    try {
      renderStation(await apiGet("/telemetry/" + encodeURIComponent(selected) +
        "?days=" + daysEl.value));
    } catch (err) {
      statusEl.className = "err";
      statusEl.textContent = "Unable to load " + selected + ": " + String(err.message || err);
    } finally {
      stationEl.style.opacity = "";
    }
  }

  async function loadList() {
    statusEl.className = "";
    try {
      var result = await apiGet("/telemetry/stations?days=" + daysEl.value);
      var stations = result.stations;
      var period = daysEl.options[daysEl.selectedIndex].text;
      if (!stations.length) {
        statusEl.textContent = "No stations sent telemetry in the last " + period +
          ". Digipeaters and weather stations often do; it shows up here as " +
          "it's heard.";
        listEl.replaceChildren();
        selectEl.replaceChildren();
        if (selected) loadStation();
        return;
      }
      statusEl.textContent = stations.length + " station" +
        (stations.length === 1 ? "" : "s") + " sent telemetry in the last " + period + ".";
      if (!selected || !stations.some(function (s) { return s.callsign === selected; })) {
        selected = selected || stations[0].callsign;
      }
      renderList(stations);
      remember();
      loadStation();
    } catch (err) {
      statusEl.className = "err";
      statusEl.textContent = String(err.message || err);
    }
  }

  daysEl.addEventListener("change", function () { remember(); loadList(); });
  setInterval(function () { if (selected) loadStation(); }, 120000);
  loadList();
})();
