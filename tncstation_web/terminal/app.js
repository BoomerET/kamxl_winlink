
(function () {
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";
  var output = document.getElementById("output");
  var input = document.getElementById("cmdInput");
  var monitorFeed = document.getElementById("monitorFeed");
  var monitorStatus = document.getElementById("monitorStatus");

  function authedUrl(path) {
    if (!token) return path;
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(token);
  }

  function log(text, cls) {
    var line = document.createElement("div");
    if (cls) line.className = cls;
    line.textContent = text;
    output.appendChild(line);
    output.scrollTop = output.scrollHeight;
  }

  if (!token) {
    log(
      "No ?token=... in this page's URL. If the server was started " +
      "with authentication, every command below will fail with 401 " +
      "-- reload with ?token=<your key> appended to the URL.",
      "notice"
    );
  }

  async function sendCommand(command) {
    log("cmd: " + command, "sent");

    try {
      var res = await fetch(authedUrl("/terminal/exec"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: command })
      });
      var payload = await res.json();

      if (payload.ok) {
        log(payload.result || "(no output)");
      } else {
        log((payload.error && payload.error.message) || "Unknown error", "err");
      }
    } catch (err) {
      log("Request failed: " + err, "err");
    }
  }

  input.addEventListener("keydown", function (e) {
    if (e.key !== "Enter") return;

    var command = input.value.trim();
    input.value = "";

    if (command) sendCommand(command);
  });

  log("kamxl web terminal -- type a command (e.g. VERSION, DISPLAY, MYCALL) and press Enter.");
  input.focus();

  document.getElementById("homeLink").href = authedUrl("/");
  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("mapLink").href = authedUrl("/map");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");
  document.getElementById("logLink").href = authedUrl("/log");
  document.getElementById("telemetryLink").href = authedUrl("/telemetry");

  // -- Live monitor (milestone 5) ------------------------------------
  //
  // Packets only actually flow if MONITOR is ON for the relevant
  // port(s) on the KAM-XL itself -- this page doesn't turn it on for
  // you. Type e.g. "MONITOR ON/ON" (multi-port values are
  // slash-separated, not space-separated) in the terminal above, or
  // use PUT /params/MONITOR, if the feed stays empty.

  function setStatus(state, text) {
    monitorStatus.className = state;
    monitorStatus.textContent = text;
  }

  function logPacket(evt) {
    var p = evt.data;
    var time = new Date().toLocaleTimeString();
    var route = p.source + " -> " + p.destination;

    if (p.digipeaters && p.digipeaters.length) {
      route += " via " + p.digipeaters.join(",");
    }

    var line = document.createElement("div");

    var timeSpan = document.createElement("span");
    timeSpan.className = "pktTime";
    timeSpan.textContent = time + "  ";

    var routeSpan = document.createElement("span");
    routeSpan.className = "pktRoute";
    // frame_type is the KAM-XL's own annotation (MCOM/MRESP, on by
    // default): "UI" is an ordinary beacon/unconnected frame; most
    // other values (C, UA, D, DM, I00, rr1, ...) are AX.25
    // connect-session control/supervisory traffic between two OTHER
    // stations, not something this library initiated -- shown as a
    // tag so it doesn't read as if it were another beacon.
    var tag = p.frame_type ? "<" + p.frame_type + "> " : "";
    routeSpan.textContent = "[port " + p.port + "] " + tag + route + ": ";

    line.appendChild(timeSpan);
    line.appendChild(routeSpan);
    line.appendChild(document.createTextNode(p.payload || ""));

    monitorFeed.appendChild(line);
    monitorFeed.scrollTop = monitorFeed.scrollHeight;
  }

  document.getElementById("clearMonitor").addEventListener("click", function () {
    monitorFeed.innerHTML = "";
  });

  if (typeof EventSource !== "undefined") {
    var source = new EventSource(authedUrl("/monitor/stream"));

    source.onopen = function () { setStatus("live", "live"); };
    source.onerror = function () { setStatus("down", "reconnecting..."); };

    source.onmessage = function (e) {
      setStatus("live", "live");

      var evt;
      try {
        evt = JSON.parse(e.data);
      } catch (err) {
        return;
      }

      if (evt.event === "packet") logPacket(evt);
    };
  } else {
    setStatus("down", "unsupported");
  }
})();
