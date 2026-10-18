
(function () {
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";
  var requestedStation = (params.get("station") || "").trim().toUpperCase();
  var stationSelectionHandled = false;
  var stationSelectionStatus = document.getElementById("stationSelectionStatus");
  var empty = document.getElementById("empty");
  var stationCount = document.getElementById("stationCount");

  function authedUrl(path) {
    if (!token) return path;
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(token);
  }

  document.getElementById("homeLink").href = authedUrl("/");
  document.getElementById("terminalLink").href = authedUrl("/terminal");
  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");

  var map = L.map("map").setView([0, 0], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  var markers = {};
  var trails = {};
  var trailPoints = {};
  var digiPaths = L.layerGroup();
  var fitDone = false;

  var showDigiPaths = document.getElementById("showDigiPaths");

  showDigiPaths.addEventListener("change", function() {
    if (showDigiPaths.checked) {
      digiPaths.addTo(map);
    } else {
      map.removeLayer(digiPaths);
    }
  });

  var digiStationFilter = document.getElementById("digiStationFilter");
  var currentDigiLinks = [];

  digiStationFilter.addEventListener("change", function() {
    drawDigipeaterPaths();
  });

  function drawDigipeaterPaths() {
    digiPaths.clearLayers();

    var selected = digiStationFilter.value;

    currentDigiLinks.forEach(function(link) {
      if (selected && link.source !== selected) return;

      L.polyline([link.from, link.to], {
        color: "#22a4a7",
        weight: 2,
        opacity: 0.35,
        dashArray: "7, 5",
        interactive: true,
        bubblingMouseEvents: false
      })
      .bindTooltip(
        escapeHtml(link.source) +
        " &rarr; " +
        escapeHtml(link.digipeater) +
        " (inferred relay relationship)",
        { sticky: true }
      )
      .addTo(digiPaths);
    });
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function relativeTime(epochSeconds) {
    var deltaSeconds = Math.max(0, (Date.now() / 1000) - epochSeconds);

    if (deltaSeconds < 60) return Math.floor(deltaSeconds) + "s ago";
    if (deltaSeconds < 3600) return Math.floor(deltaSeconds / 60) + "m ago";
    if (deltaSeconds < 86400) return Math.floor(deltaSeconds / 3600) + "h ago";
    return Math.floor(deltaSeconds / 86400) + "d ago";
  }

  function resolveDigipeaterLinks(stations) {
    var byCallsign = {};
    var links = [];
    var seen = {};

    stations.forEach(function(station) {
      byCallsign[station.callsign.toUpperCase()] = station;
    });

    stations.forEach(function(station) {
      var packets = station.recent_packets || [];

      packets.forEach(function(packet) {
        var path = packet.digipeaters || [];

        // Only consider path entries marked as already used.
        path.forEach(function(entry) {
          if (!entry.endsWith("*")) return;

          var digiCallsign = entry.slice(0, -1).toUpperCase();

          // APRS routing aliases aren't uniquely located stations.
          if (/^(WIDE|TRACE|RELAY|TCPIP|TCPXX)/i.test(digiCallsign)) {
            return;
          }

          var digi = byCallsign[digiCallsign];
          if (!digi || digi.callsign === station.callsign) return;

          var coordinates = [
            station.latitude,
            station.longitude,
            digi.latitude,
            digi.longitude
          ];

          if (!coordinates.every(Number.isFinite)) return;

          var key = station.callsign + ">" + digi.callsign;

          if (seen[key]) return;
          seen[key] = true;

          links.push({
            source: station.callsign,
            digipeater: digi.callsign,
            from: [station.latitude, station.longitude],
            to: [digi.latitude, digi.longitude]
          });
        });
      });
    });

    return links;
  }

  // APRS symbol-table icon rendering. Translation table and sprite-grid
  // math ported from OK-DMR/aprs-symbols' aprs-symbols.js/.css (MIT
  // License, Copyright (c) 2019 Marek Sebera --
  // github.com/OK-DMR/aprs-symbols); the sprite images are
  // hessu/aprs-symbols (github.com/hessu/aprs-symbols), pinned to a
  // specific commit and loaded via jsdelivr's GitHub-raw CDN. See this
  // file's module-level comment above MAP_HTML for the full licensing
  // and scope notes -- this is a deliberate, one-off exception to this
  // project's normal cdnjs-only rule for browser assets.
  var APRS_ICON_SIZE = 32;
  var APRS_SPRITE_BASE =
    "https://cdn.jsdelivr.net/gh/hessu/aprs-symbols@" +
    "f2286a9cd43eb6ba4501250b4c39fff111e3796c/png/";

  // Each string is one row of the APRS symbol chart -- 94 printable
  // ASCII characters (0x21 "!" through 0x7E "~"), 16 per row except
  // the final partial row. Row/column index here is exactly the
  // sprite sheet's row/column (each cell APRS_ICON_SIZE px square).
  var APRS_SYMBOL_ROWS = [
    "!\"#$%&'()*+,-./0",
    "123456789:;<=>?@",
    "ABCDEFGHIJKLMNOP",
    "QRSTUVWXYZ[\\]^_`",
    "abcdefghijklmnop",
    "qrstuvwxyz{|}~"
  ];

  function aprsSymbolPosition(symbolTable, symbolCode) {
    // Real APRS symbol tables are literally "/" (primary, sprite
    // table 0) or "\" (alternate, sprite table 1) -- any other
    // character in this position means an *alternate table with
    // overlay* (a digit/letter meant to be drawn over the base icon,
    // e.g. a numbered object). This function returns the alternate
    // table's base icon position for that case, plus the overlay
    // character itself (aprsIcon() draws it as a text badge -- see
    // the module comment above MAP_HTML for why not sprite art).
    var table = symbolTable === "/" ? 0 : 1;
    var overlay = (symbolTable !== "/" && symbolTable !== "\\") ? symbolTable : null;

    for (var row = 0; row < APRS_SYMBOL_ROWS.length; row++) {
      var col = APRS_SYMBOL_ROWS[row].indexOf(symbolCode);
      if (col !== -1) return { table: table, row: row, col: col, overlay: overlay };
    }

    return null;
  }

  function aprsIcon(symbolTable, symbolCode) {
    var pos = aprsSymbolPosition(symbolTable, symbolCode);

    if (!pos) return null;

    var spriteUrl =
      APRS_SPRITE_BASE + "aprs-symbols-" + APRS_ICON_SIZE + "-" + pos.table + ".png";
    var bgX = -(pos.col * APRS_ICON_SIZE);
    var bgY = -(pos.row * APRS_ICON_SIZE);

    var html =
      '<div style="width:' + APRS_ICON_SIZE + "px;height:" + APRS_ICON_SIZE + "px;position:relative;" +
      "background-image:url(&quot;" + spriteUrl + "&quot;);" +
      "background-position:" + bgX + "px " + bgY + "px;" +
      'background-repeat:no-repeat;">';

    if (pos.overlay) {
      html += '<div class="aprsOverlay">' + escapeHtml(pos.overlay) + "</div>";
    }

    html += "</div>";

    return L.divIcon({
      className: "aprsIcon",
      html: html,
      iconSize: [APRS_ICON_SIZE, APRS_ICON_SIZE],
      iconAnchor: [APRS_ICON_SIZE / 2, APRS_ICON_SIZE / 2],
      popupAnchor: [0, -(APRS_ICON_SIZE / 2)]
    });
  }

  // -- Weather ---------------------------------------------------------

  var COMPASS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                 "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"];

  function compass(degrees) {
    return COMPASS[Math.round((degrees % 360) / 22.5) % 16];
  }

  function hasValue(v) {
    return v !== null && v !== undefined;
  }

  // Plain-text lines describing a weather report (caller escapes).
  function weatherLines(w) {
    var lines = [];
    var first = [];

    if (hasValue(w.temperature_f)) {
      var c = Math.round((w.temperature_f - 32) * 5 / 9);
      first.push(w.temperature_f + "\u00b0F (" + c + "\u00b0C)");
    }
    if (hasValue(w.humidity_pct)) first.push("humidity " + w.humidity_pct + "%");
    if (hasValue(w.pressure_mb)) {
      first.push(w.pressure_mb.toFixed(1) + " mb (" +
        (w.pressure_mb * 0.02953).toFixed(2) + " inHg)");
    }
    if (first.length) lines.push(first.join(", "));

    var wind = [];
    if (hasValue(w.wind_speed_mph)) {
      wind.push(w.wind_speed_mph === 0
        ? "calm"
        : (hasValue(w.wind_direction) ? "from " + compass(w.wind_direction) + " " : "") +
          w.wind_speed_mph + " mph");
    }
    if (hasValue(w.wind_gust_mph) && w.wind_gust_mph > 0) {
      wind.push("gusts " + w.wind_gust_mph + " mph");
    }
    if (wind.length) lines.push("Wind " + wind.join(", "));

    var rain = [];
    if (hasValue(w.rain_1h_in)) rain.push(w.rain_1h_in.toFixed(2) + " in last hour");
    if (hasValue(w.rain_since_midnight_in)) {
      rain.push(w.rain_since_midnight_in.toFixed(2) + " in since midnight");
    }
    if (hasValue(w.rain_24h_in)) rain.push(w.rain_24h_in.toFixed(2) + " in 24 h");
    if (rain.length) lines.push("Rain " + rain.join(", "));

    if (hasValue(w.snow_24h_in) && w.snow_24h_in > 0) {
      lines.push("Snow " + w.snow_24h_in.toFixed(1) + " in 24 h");
    }
    if (hasValue(w.luminosity_wm2)) {
      lines.push("Solar " + w.luminosity_wm2 + " W/m\u00b2");
    }
    return lines;
  }

  function weatherHtml(station) {
    if (!station.weather) return "";
    var lines = weatherLines(station.weather);
    if (!lines.length) return "";
    return "<div class='wxBlock'><b>Weather</b>" +
      (station.weather_at ? " <span class='wxAge'>(" +
        relativeTime(station.weather_at) + ")</span>" : "") +
      "<br>" + lines.map(escapeHtml).join("<br>") + "</div>";
  }

  // A weather station's position comment is mostly the raw weather
  // fields ("220/012g018t068..."); show just what follows them.
  function displayComment(station) {
    var c = station.comment || "";
    var w = station.weather;
    if (w && station.symbol_code === "_" && typeof w.comment === "string" &&
        c !== w.comment && c.slice(c.length - w.comment.length) === w.comment) {
      return w.comment;
    }
    return c;
  }

  function temperatureLabel(station) {
    var w = station.weather;
    if (!w || !hasValue(w.temperature_f)) return null;
    return w.temperature_f + "\u00b0F";
  }

  var showTemps = document.getElementById("showTemps");

  try {
    var savedTemps = window.localStorage.getItem("kamxl.map.showTemps");
    if (savedTemps !== null) showTemps.checked = savedTemps === "1";
  } catch (e) { /* storage unavailable: default on */ }

  function applyTemperatureLabel(marker, station) {
    var text = showTemps.checked ? temperatureLabel(station) : null;
    var tooltip = marker.getTooltip();

    if (!text) {
      if (tooltip) marker.unbindTooltip();
      return;
    }
    if (tooltip) {
      marker.setTooltipContent(escapeHtml(text));
    } else {
      marker.bindTooltip(escapeHtml(text), {
        permanent: true,
        direction: "right",
        offset: [APRS_ICON_SIZE / 2, 0],
        className: "wxLabel",
        interactive: false
      });
    }
  }

  var lastStations = [];

  showTemps.addEventListener("change", function () {
    try {
      window.localStorage.setItem(
        "kamxl.map.showTemps", showTemps.checked ? "1" : "0"
      );
    } catch (e) {}
    lastStations.forEach(function (station) {
      if (markers[station.callsign]) {
        applyTemperatureLabel(markers[station.callsign], station);
      }
    });
  });

  function popupHtml(station) {
    // escapeHtml() added alongside the overlay-badge work above --
    // used here too now that it exists, since this popup content
    // (particularly station.comment) is over-the-air text, not
    // trusted markup.
    var html = "<b>" + escapeHtml(station.callsign) + "</b><br>";
    html += station.latitude.toFixed(5) + ", " + station.longitude.toFixed(5) + "<br>";
    var comment = displayComment(station);
    if (comment) html += escapeHtml(comment) + "<br>";
    html += weatherHtml(station);
    html += "symbol: " + escapeHtml(station.symbol_table) + escapeHtml(station.symbol_code) + "<br>";
    html += "heard " + relativeTime(station.last_heard) +
      " (" + station.packet_count + " report" +
      (station.packet_count === 1 ? "" : "s") + ")";
    html += "<br><a href='#' class='viewStationDetails' data-callsign='" +
      escapeHtml(station.callsign) + "'>View Details &rarr;</a>";
    return html;
  }

  var stationDetailsRequestId = 0;
  var stationDetails = document.getElementById("stationDetails");
  var detailCallsign = document.getElementById("detailCallsign");
  var stationDetailsBody = document.getElementById("stationDetailsBody");

  document.getElementById("closeStationDetails").addEventListener(
    "click", function () {
      stationDetailsRequestId++;
      stationDetails.hidden = true;
    }
  );

  async function showStationDetails(callsign) {
    var requestId = ++stationDetailsRequestId;

    stationDetails.hidden = false;
    detailCallsign.textContent = callsign;
    stationDetailsBody.textContent = "Loading station information...";

    try {
      var response = await fetch(
        authedUrl("/stations/" + encodeURIComponent(callsign))
      );

      if (!response.ok) {
        throw new Error("HTTP " + response.status);
      }

      var payload = await response.json();
      if (requestId !== stationDetailsRequestId) return;

      if (!payload.ok || !payload.result) {
        stationDetailsBody.textContent = "Station no longer available.";
        return;
      }

      var station = payload.result;
      var history = station.history || [];

      var html = "";

      function detail(label, value) {
        html += "<div class='detailRow'>" +
          "<span class='detailLabel'>" + escapeHtml(label) + ":</span> " +
          escapeHtml(String(value)) + "</div>";
      }

      detail("Last heard", relativeTime(station.last_heard));
      detail("Position reports", station.packet_count);
      detail("Latitude", Number(station.latitude).toFixed(5));
      detail("Longitude", Number(station.longitude).toFixed(5));
      detail("Symbol", station.symbol_table + station.symbol_code);
      detail("Comment", displayComment(station) || "(none)");

      if (station.weather) {
        var wxLines = weatherLines(station.weather);
        if (wxLines.length) {
          html += "<h3>Weather" + (station.weather_at
            ? " (" + escapeHtml(relativeTime(station.weather_at)) + ")"
            : "") + "</h3>";
          wxLines.forEach(function (line) {
            html += "<div class='detailRow'>" + escapeHtml(line) + "</div>";
          });
        }
      }

      html += "<h3>Position History (" + history.length + ")</h3>";

      if (!history.length) {
        html += "<div>No position history available.</div>";
      } else {
        history.slice().reverse().forEach(function(point) {
          html += "<div class='detailRow'>" +
            escapeHtml(new Date(point.timestamp * 1000).toLocaleString()) +
            "<br>" +
            Number(point.latitude).toFixed(5) + ", " +
            Number(point.longitude).toFixed(5) +
            "</div>";
        });
      }
      var packets = station.recent_packets || [];

      html += "<h3>Recent APRS Packets (" + packets.length + ")</h3>";

      if (!packets.length) {
        html += "<div class='detailRow'>No recent packets available.</div>";
      } else {
        packets.slice().reverse().forEach(function(packet) {
          var received = new Date(packet.timestamp * 1000).toLocaleString();

          var path = packet.digipeaters && packet.digipeaters.length
            ? packet.digipeaters.join(", ")
            : "Direct (no digipeaters listed)";

          html += "<div class='packetRecord'>";

          html += "<div class='packetMeta'>" +
            escapeHtml(received) +
            " | " + escapeHtml(packet.frame_type || "UI") +
            " | Port " + escapeHtml(String(packet.port)) +
            "</div>";

          html += "<div><span class='detailLabel'>Destination:</span> " +
            escapeHtml(packet.destination) + "</div>";

          html += "<div><span class='detailLabel'>Path:</span> " +
            escapeHtml(path) + "</div>";

          html += "<div class='packetPayload'>" +
            escapeHtml(packet.payload) + "</div>";

          html += "</div>";
        });
      }
      stationDetailsBody.innerHTML = html;
    } catch (error) {
      if (requestId !== stationDetailsRequestId) return;
      stationDetailsBody.textContent =
        "Unable to load station details: " + error.message;
    }
  }

  document.addEventListener("click", function(event) {
    var link = event.target.closest(".viewStationDetails");

    if (!link) return;

    event.preventDefault();

    var callsign = link.getAttribute("data-callsign");
    if (callsign) showStationDetails(callsign);
  });

  function updateTrailPoints(station) {
    var callsign = station.callsign;
    var history = station.history || [];

    // Keep existing markers so open popups survive refreshes.
    if (!trailPoints[callsign]) {
      trailPoints[callsign] = [];
    }

    var existing = trailPoints[callsign];

    // History is bounded to 25 positions. If it rolls over,
    // rebuild only the markers whose positions changed.
    history.forEach(function (point, index) {
      var current = existing[index];


      if (
        current &&
        current.positionTimestamp === point.timestamp &&
        current.positionLat === point.latitude &&
        current.positionLon === point.longitude
      ) {
        var html =
          "<b>" + escapeHtml(callsign) + "</b><br>" +
          "Position " + (index + 1) + " of " + history.length + "<br>" +
          "Received: " +
          escapeHtml(new Date(point.timestamp * 1000).toLocaleString()) + "<br>" +
          "Latitude: " + Number(point.latitude).toFixed(5) + "<br>" +
          "Longitude: " + Number(point.longitude).toFixed(5);

        current.setPopupContent(html);
        return;
      }


      if (current) {
        map.removeLayer(current);
      }

      var timestamp = new Date(point.timestamp * 1000);

      var html =
        "<b>" + escapeHtml(callsign) + "</b><br>" +
        "Position " + (index + 1) + " of " + history.length + "<br>" +
        "Received: " + escapeHtml(timestamp.toLocaleString()) + "<br>" +
        "Latitude: " + Number(point.latitude).toFixed(5) + "<br>" +
        "Longitude: " + Number(point.longitude).toFixed(5);

      var marker = L.circleMarker(
        [point.latitude, point.longitude],
        {
          radius: 5,
          color: "#3388ff",
          fillColor: "#ffffff",
          fillOpacity: 1,
          weight: 2,
          bubblingMouseEvents: false
        }
      );

      marker.positionTimestamp = point.timestamp;
      marker.positionLat = point.latitude;
      marker.positionLon = point.longitude;

      marker.bindPopup(html);

      marker.on("click", function () {
        marker.setRadius(7);
      });

      marker.on("popupclose", function () {
        marker.setRadius(5);
      });

      marker.addTo(map);
      existing[index] = marker;
    });

    // Remove markers no longer present in the history.
    while (existing.length > history.length) {
      map.removeLayer(existing.pop());
    }

    // Don't show historical circles for a single position.
    if (history.length < 2) {
      existing.forEach(function (marker) {
        map.removeLayer(marker);
      });
      trailPoints[callsign] = [];
    }
  }

  function updateDigipeaterPaths(stations) {
    currentDigiLinks = resolveDigipeaterLinks(stations);

    var selected = digiStationFilter.value;
    var sources = Array.from(
      new Set(currentDigiLinks.map(function(link) {
        return link.source;
      }))
    ).sort();

    digiStationFilter.replaceChildren(
      new Option("All stations", "")
    );

    sources.forEach(function(callsign) {
      digiStationFilter.add(new Option(callsign, callsign));
    });

    if (selected && sources.includes(selected)) {
      digiStationFilter.value = selected;
    }

    drawDigipeaterPaths();
  }

  function selectMapStation(callsign) {
    var selected = String(callsign || "").trim().toUpperCase();

    if (!selected) {
      stationSelectionStatus.hidden = false;
      stationSelectionStatus.textContent = "Enter a callsign to search.";
      return false;
    }

    var marker = markers[selected];

    if (!marker) {
      stationSelectionStatus.hidden = false;
      stationSelectionStatus.textContent =
        selected + " has no known position on this map. " +
        "The station may have transmitted AX.25 packets " +
        "without an APRS position report.";
      return false;
    }

    map.setView(marker.getLatLng(), 13);
    marker.openPopup();
    showStationDetails(selected);

    stationSelectionStatus.hidden = true;
    stationSelectionStatus.textContent = "";

    return true;
  }

  document.getElementById("stationSearchForm").addEventListener(
    "submit",
    function(event) {
      event.preventDefault();
      // A manual search supersedes any pending deep-link selection.
      stationSelectionHandled = true;

      var search = document.getElementById("stationSearch").value
        .trim()
        .toUpperCase();

      var matches = Object.keys(markers)
        .filter(function(callsign) {
          return callsign.toUpperCase().includes(search);
        })
        .sort();

      if (!search) {
        selectMapStation("");
      } else if (markers[search]) {
        selectMapStation(search);
      } else if (matches.length === 1) {
        selectMapStation(matches[0]);
      } else if (matches.length > 1) {
        stationSelectionStatus.hidden = false;
        stationSelectionStatus.textContent =
          matches.length + " stations match '" + search +
          "'. Choose a complete callsign from the suggestions.";
      } else {
        selectMapStation(search);
      }
    }
  );

  async function refresh() {
    try {
      var res = await fetch(authedUrl("/stations"));
      var payload = await res.json();

      if (!payload.ok) return;

      var stations = payload.result;
      stationCount.textContent = stations.length
        ? "(" + stations.length + ")"
        : "";
      empty.style.display = stations.length ? "none" : "block";

      var seen = {};
      lastStations = stations;

      stations.forEach(function (station) {
        seen[station.callsign] = true;

        var latLng = [station.latitude, station.longitude];
        var icon = aprsIcon(station.symbol_table, station.symbol_code);

        if (markers[station.callsign]) {
          markers[station.callsign].setLatLng(latLng);
          markers[station.callsign].setPopupContent(popupHtml(station));
          // Symbol can change between reports (rare, but not
          // impossible) -- always re-apply rather than assuming the
          // first-seen icon stays correct forever.
          if (icon) markers[station.callsign].setIcon(icon);
        } else {
          // Fall back to Leaflet's plain default marker if the symbol
          // code isn't in the chart (malformed/missing data) rather
          // than drawing nothing -- same "skip, don't guess" pattern
          // as aprs.py's own parser.
          var marker = icon ? L.marker(latLng, { icon: icon }) : L.marker(latLng);
          markers[station.callsign] = marker.addTo(map).bindPopup(popupHtml(station));
        }
        applyTemperatureLabel(markers[station.callsign], station);
        // Draw a trail when at least two distinct positions exist.
        var history = station.history || [];

        if (history.length >= 2) {
          var points = history.map(function (point) {
            return [point.latitude, point.longitude];
          });

          if (trails[station.callsign]) {
            trails[station.callsign].setLatLngs(points);
          } else {
            trails[station.callsign] = L.polyline(points, {
              color: "#3388ff",
              weight: 3,
              opacity: 0.75,
              interactive: false
            }).addTo(map);

            // Keep the trail underneath station markers.
            trails[station.callsign].bringToBack();
          }
        } else if (trails[station.callsign]) {
          map.removeLayer(trails[station.callsign]);
          delete trails[station.callsign];
        }

        updateTrailPoints(station);
      });

      Object.keys(markers).forEach(function (callsign) {
        if (!seen[callsign]) {
          map.removeLayer(markers[callsign]);
          delete markers[callsign];
        }
      });

      // Remove trails belonging to expired stations.
      Object.keys(trails).forEach(function (callsign) {
        if (!seen[callsign]) {
          map.removeLayer(trails[callsign]);
          delete trails[callsign];
        }
      });

      // Remove historical position markers for expired stations.
      Object.keys(trailPoints).forEach(function (callsign) {
        if (!seen[callsign]) {
          trailPoints[callsign].forEach(function (marker) {
            map.removeLayer(marker);
          });
          delete trailPoints[callsign];
        }
      });

      updateDigipeaterPaths(stations);

      var searchOptions = document.getElementById(
        "stationSearchOptions"
      );

      searchOptions.replaceChildren();

      Object.keys(markers).sort().forEach(function(callsign) {
        var option = document.createElement("option");
        option.value = callsign;
        searchOptions.appendChild(option);
      });

      if (!fitDone && stations.length) {
        fitDone = true;

        var bounds = stations.map(function (s) {
          return [s.latitude, s.longitude];
        });

        map.fitBounds(bounds, { maxZoom: 12, padding: [30, 30] });
      }

      if (requestedStation && !stationSelectionHandled) {
        stationSelectionHandled = selectMapStation(requestedStation);
      }

    } catch (err) {
      // Transient fetch failure -- next poll will retry.
    }
  }

  refresh();
  setInterval(refresh, 15000);
})();
