"""
REST API in front of kamxl_daemon.py -- translates HTTP requests into
the daemon's newline-delimited JSON protocol over its Unix socket, and
proxies its "packet" events out as Server-Sent Events for live
monitoring over plain HTTP (no websockets needed).

This process does NOT talk to the KAM-XL directly -- it's a thin HTTP
front end for the daemon, which remains the single owner of the
serial connection. Run kamxl_daemon.py first.

Run directly:

    python3 kamxl_rest.py --daemon-socket /tmp/kamxl.sock --port 8080

See docs/rest_api.md for the full endpoint list, authentication, and
usage examples.

Security note: binds to 0.0.0.0 (all interfaces) by default, since
the point is reaching it from other devices on the LAN (a phone,
another PC). That means it needs authentication -- a bearer token,
checked on every request. The token still travels in cleartext over
plain HTTP, though, which is an acceptable bar for a trusted home LAN
but NOT safe to expose over the open internet (e.g. via port
forwarding) without putting TLS in front of it first.
"""

import argparse
import json
import logging
import os
import re
import secrets
import select
import signal
import socket
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs


DEFAULT_DAEMON_SOCKET = "/tmp/kamxl.sock"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080

# Daemon error types that mean "the request was well-formed, but the
# KAM-XL (or the daemon itself) couldn't fulfill it" -- as opposed to
# a malformed request, which is a client-side (4xx) problem.
_DAEMON_ERROR_STATUS = 502

# Milestone 4 (terminal) + milestone 5 (live monitor): a single
# self-contained page (no build step, no CDN dependency) served
# directly by this process at GET / -- a live packet feed on top
# (EventSource against /monitor/stream), a raw Terminal Mode command
# box below it (same as milestone 4). Reads ?token=... from its own
# URL and carries it forward on every request it makes, since that's
# the auth mechanism this REST layer accepts specifically so a
# browser context (which can't always set a custom header) can use.
TERMINAL_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>kamxl web terminal</title>
<style>
  html, body {
    margin: 0;
    height: 100%;
    background: #0b0f10;
    color: #d4f7d4;
    font-family: "Courier New", Courier, monospace;
  }
  body {
    display: flex;
    flex-direction: column;
  }
  .paneHeader {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 10px;
    background: #101617;
    border-bottom: 1px solid #234;
    font-size: 12px;
    letter-spacing: 0.05em;
    color: #888;
    box-sizing: border-box;
  }
  .paneHeader button {
    background: none;
    border: 1px solid #345;
    color: #9c9;
    font: inherit;
    font-size: 11px;
    padding: 2px 8px;
    cursor: pointer;
  }
  .paneHeader button:hover { border-color: #5a8; color: #cfc; }
  #monitorStatus.live::before { content: "\\25CF "; color: #6f6; }
  #monitorStatus.connecting::before { content: "\\25CB "; color: #cc6; }
  #monitorStatus.down::before { content: "\\2715 "; color: #f66; }
  #monitorPane {
    flex: 1 1 40%;
    min-height: 80px;
    display: flex;
    flex-direction: column;
    border-bottom: 2px solid #234;
  }
  #monitorFeed {
    flex: 1;
    overflow-y: auto;
    padding: 8px 12px;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 13px;
  }
  #monitorFeed div { padding: 1px 0; }
  .pktTime { color: #567; }
  .pktRoute { color: #6cf; }
  #terminalPane {
    flex: 1 1 60%;
    display: flex;
    flex-direction: column;
    min-height: 120px;
  }
  #output {
    flex: 1;
    overflow-y: auto;
    padding: 12px;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 14px;
  }
  #inputRow {
    display: flex;
    height: 48px;
    flex: 0 0 48px;
    border-top: 1px solid #234;
    box-sizing: border-box;
  }
  #prompt {
    padding: 0 8px;
    display: flex;
    align-items: center;
    color: #6f6;
  }
  #cmdInput {
    flex: 1;
    background: #0b0f10;
    color: #d4f7d4;
    border: none;
    outline: none;
    font: inherit;
    font-size: 14px;
  }
  .err { color: #f77; }
  .sent { color: #6cf; }
  .notice { color: #999; font-style: italic; }
</style>
</head>
<body>
<div id="monitorPane">
  <div class="paneHeader">
    <span>LIVE MONITOR <span id="monitorStatus" class="connecting">connecting</span></span>
    <button id="clearMonitor" type="button">clear</button>
  </div>
  <div id="monitorFeed"></div>
</div>
<div id="terminalPane">
  <div class="paneHeader">
    <span>TERMINAL</span>
    <span>
      <a href="#" id="pbbsLink" style="background:none;border:1px solid #345;color:#9c9;font:inherit;font-size:11px;padding:2px 8px;text-decoration:none;">pbbs</a>
      <a href="#" id="mapLink" style="background:none;border:1px solid #345;color:#9c9;font:inherit;font-size:11px;padding:2px 8px;text-decoration:none;">map</a>
      <a href="#" id="winlinkLink" style="background:none;border:1px solid #345;color:#9c9;font:inherit;font-size:11px;padding:2px 8px;text-decoration:none;">winlink</a>
    </span>
  </div>
  <div id="output"></div>
  <div id="inputRow">
    <div id="prompt">cmd:</div>
    <input id="cmdInput" autocomplete="off" autofocus
           placeholder="type a KAM-XL command and press Enter..." />
  </div>
</div>
<script>
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

  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("mapLink").href = authedUrl("/map");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");

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
</script>
</body>
</html>
"""

# Milestone 6: a second self-contained page, served at GET /pbbs, for
# the KAM-XL's built-in firmware PBBS (mailbox) -- read-only per
# Dave's chosen scope: list messages, click one to read it. Kept as
# its own page rather than folded into the terminal/monitor page --
# unlike those two (both live, continuously-updating views that
# naturally share screen space), this is list/detail navigation, a
# different enough shape to stand on its own. Linked from the
# terminal page's header and back again, both carrying ?token=
# forward the same way the terminal and monitor pane already do.
PBBS_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>kamxl PBBS</title>
<style>
  html, body {
    margin: 0;
    min-height: 100%;
    background: #0b0f10;
    color: #d4f7d4;
    font-family: "Courier New", Courier, monospace;
  }
  .paneHeader {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 10px;
    background: #101617;
    border-bottom: 1px solid #234;
    font-size: 12px;
    letter-spacing: 0.05em;
    color: #888;
    box-sizing: border-box;
  }
  .paneHeader a, .paneHeader button {
    background: none;
    border: 1px solid #345;
    color: #9c9;
    font: inherit;
    font-size: 11px;
    padding: 2px 8px;
    cursor: pointer;
    text-decoration: none;
  }
  .paneHeader a:hover, .paneHeader button:hover { border-color: #5a8; color: #cfc; }
  #content { padding: 12px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td {
    text-align: left;
    padding: 4px 10px 4px 0;
    border-bottom: 1px solid #1c2526;
    white-space: nowrap;
  }
  th { color: #789; font-weight: normal; }
  td.subject { white-space: normal; }
  tr.msgRow { cursor: pointer; }
  tr.msgRow:hover { background: #101c1d; }
  .err { color: #f77; }
  .notice { color: #999; font-style: italic; }
  #detail {
    white-space: pre-wrap;
    word-break: break-word;
    padding-top: 8px;
    line-height: 1.4;
  }
  #detail .hdr { color: #6cf; margin-bottom: 8px; }
  #backLink { display: inline-block; margin-bottom: 10px; }
</style>
</head>
<body>
<div class="paneHeader">
  <span>PBBS</span>
  <span>
    <a href="#" id="terminalLink">terminal</a>
    <a href="#" id="mapLink">map</a>
    <a href="#" id="winlinkLink">winlink</a>
  </span>
</div>
<div id="content">
  <div class="notice">Loading messages...</div>
</div>
<script>
(function () {
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";
  var content = document.getElementById("content");

  function authedUrl(path) {
    if (!token) return path;
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(token);
  }

  document.getElementById("terminalLink").href = authedUrl("/");
  document.getElementById("mapLink").href = authedUrl("/map");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  async function apiGet(path) {
    var res = await fetch(authedUrl(path));
    var payload = await res.json();

    if (!payload.ok) {
      throw new Error(
        (payload.error && payload.error.message) || "Request failed"
      );
    }

    return payload.result;
  }

  function renderError(err) {
    content.innerHTML =
      '<div class="err">' + escapeHtml(err.message || String(err)) + "</div>";
  }

  async function showList() {
    content.innerHTML = '<div class="notice">Loading messages...</div>';

    try {
      var messages = await apiGet("/pbbs/messages");

      if (!messages.length) {
        content.innerHTML = '<div class="notice">No messages.</div>';
        return;
      }

      var html = "<table><thead><tr>" +
        "<th>#</th><th>Type</th><th>Size</th><th>To</th>" +
        "<th>From</th><th>Date</th><th>Subject</th>" +
        "</tr></thead><tbody>";

      messages.forEach(function (m) {
        html += '<tr class="msgRow" data-number="' + m.number + '">' +
          "<td>" + escapeHtml(m.number) + "</td>" +
          "<td>" + escapeHtml(m.msg_type) + (m.status ? "/" + escapeHtml(m.status) : "") + "</td>" +
          "<td>" + escapeHtml(m.size) + "</td>" +
          "<td>" + escapeHtml(m.to) + "</td>" +
          "<td>" + escapeHtml(m.from_call) + "</td>" +
          "<td>" + escapeHtml(m.date) + (m.pages ? " (" + m.pages + "p)" : "") + "</td>" +
          '<td class="subject">' + escapeHtml(m.subject) + "</td>" +
          "</tr>";
      });

      html += "</tbody></table>";
      content.innerHTML = html;

      Array.prototype.forEach.call(
        content.querySelectorAll(".msgRow"),
        function (row) {
          row.addEventListener("click", function () {
            showMessage(row.getAttribute("data-number"));
          });
        }
      );
    } catch (err) {
      renderError(err);
    }
  }

  async function showMessage(number) {
    content.innerHTML = '<div class="notice">Loading message ' +
      escapeHtml(number) + "...</div>";

    try {
      var message = await apiGet("/pbbs/messages/" + encodeURIComponent(number));

      if (!message) {
        content.innerHTML =
          '<a href="#" id="backLink">&larr; back to list</a>' +
          '<div class="err">Message ' + escapeHtml(number) + " not found.</div>";
      } else {
        var hdr = "MSG#" + message.number + "  " + message.date +
          "  FROM " + message.from_call + "  TO " + message.to +
          (message.routing ? "  " + message.routing : "");

        content.innerHTML =
          '<a href="#" id="backLink">&larr; back to list</a>' +
          '<div id="detail"><div class="hdr">' + escapeHtml(hdr) + "</div>" +
          escapeHtml(message.body) + "</div>";
      }

      document.getElementById("backLink").addEventListener(
        "click", function (e) { e.preventDefault(); showList(); }
      );
    } catch (err) {
      renderError(err);
    }
  }

  if (!token) {
    content.innerHTML =
      '<div class="notice">No ?token=... in this page\\'s URL. If the ' +
      "server was started with authentication, requests below will " +
      "fail with 401 -- reload with ?token=&lt;your key&gt; appended " +
      "to the URL.</div>";
  } else {
    showList();
  }
})();
</script>
</body>
</html>
"""

# Milestone 8: a page served at GET /winlink for checking (and, since
# the send-support extension, sending) Winlink mail. Two tabs sharing
# one page: "Check mail" (a form posting to POST /winlink/check,
# unchanged since milestone 8) and "Send mail" (a form posting to POST
# /winlink/send -- send-only, text-body-only, B2 only; see winlink.py's
# module docstring's "SEND SUPPORT" note for the full scope and its
# UNVERIFIED-AGAINST-A-REAL-GATEWAY caveat, which applies to this UI
# too). The password field on either form is never persisted anywhere
# (no localStorage, no cookie) and is cleared after every submit
# attempt, success or failure -- typed fresh each time, the same
# "acceptable for a trusted home LAN, not the open internet" posture
# already documented for the bearer token (see this file's own module
# docstring), extended here to a real Winlink account password rather
# than assumed to be fine without saying so.
WINLINK_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>kamxl Winlink</title>
<style>
  html, body {
    margin: 0;
    min-height: 100%;
    background: #0b0f10;
    color: #d4f7d4;
    font-family: "Courier New", Courier, monospace;
  }
  .paneHeader {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 10px;
    background: #101617;
    border-bottom: 1px solid #234;
    font-size: 12px;
    letter-spacing: 0.05em;
    color: #888;
    box-sizing: border-box;
  }
  .paneHeader a { color: #9c9; text-decoration: none; margin-left: 8px; }
  .paneHeader a:hover { color: #cfc; }
  #content { padding: 12px; max-width: 640px; }
  form { display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px; }
  label { font-size: 12px; color: #789; }
  input {
    background: #0b0f10;
    color: #d4f7d4;
    border: 1px solid #345;
    padding: 6px 8px;
    font: inherit;
    font-size: 13px;
  }
  input:focus { outline: none; border-color: #5a8; }
  button {
    background: none;
    border: 1px solid #345;
    color: #9c9;
    font: inherit;
    font-size: 13px;
    padding: 6px 10px;
    cursor: pointer;
    align-self: flex-start;
  }
  button:hover:not(:disabled) { border-color: #5a8; color: #cfc; }
  button:disabled { opacity: 0.5; cursor: default; }
  .notice { color: #999; font-style: italic; }
  .err { color: #f77; }
  .msg {
    border: 1px solid #1c2526;
    padding: 8px 10px;
    margin-bottom: 10px;
  }
  .msg .hdr { color: #6cf; margin-bottom: 6px; }
  .msg .body { white-space: pre-wrap; word-break: break-word; line-height: 1.4; }
  textarea {
    background: #0b0f10;
    color: #d4f7d4;
    border: 1px solid #345;
    padding: 6px 8px;
    font: inherit;
    font-size: 13px;
    min-height: 100px;
    resize: vertical;
  }
  textarea:focus { outline: none; border-color: #5a8; }
  .tabs { display: flex; gap: 4px; margin-bottom: 12px; }
  .tabs button {
    background: none;
    border: 1px solid #345;
    color: #789;
    font: inherit;
    font-size: 12px;
    padding: 4px 10px;
    cursor: pointer;
  }
  .tabs button.active { color: #cfc; border-color: #5a8; }
  .hidden { display: none; }
</style>
</head>
<body>
<div class="paneHeader">
  <span>WINLINK</span>
  <span>
    <a href="#" id="terminalLink">terminal</a>
    <a href="#" id="pbbsLink">pbbs</a>
    <a href="#" id="mapLink">map</a>
  </span>
</div>
<div id="content">
  <div class="tabs">
    <button id="checkTabBtn" type="button" class="active">Check mail</button>
    <button id="sendTabBtn" type="button">Send mail</button>
  </div>
  <form id="checkForm">
    <div>
      <label for="gateway">RMS gateway callsign</label>
      <input id="gateway" required placeholder="e.g. KD5EOC-10" />
    </div>
    <div>
      <label for="password">Winlink account password</label>
      <input id="password" type="password" autocomplete="off" required />
    </div>
    <div>
      <label for="mycall">My callsign (optional -- defaults to the KAM-XL's MYCALL)</label>
      <input id="mycall" placeholder="e.g. AI6K-10" />
    </div>
    <button id="submitBtn" type="submit">Check mail</button>
  </form>
  <form id="sendForm" class="hidden">
    <div>
      <label for="sendGateway">RMS gateway callsign</label>
      <input id="sendGateway" required placeholder="e.g. KD5EOC-10" />
    </div>
    <div>
      <label for="sendPassword">Winlink account password</label>
      <input id="sendPassword" type="password" autocomplete="off" required />
    </div>
    <div>
      <label for="sendMycall">My callsign (optional -- defaults to the KAM-XL's MYCALL)</label>
      <input id="sendMycall" placeholder="e.g. AI6K-10" />
    </div>
    <div>
      <label for="sendTo">To (comma-separated)</label>
      <input id="sendTo" required placeholder="e.g. N0CALL, W1AW" />
    </div>
    <div>
      <label for="sendCc">Cc (comma-separated, optional)</label>
      <input id="sendCc" placeholder="e.g. N1CALL" />
    </div>
    <div>
      <label for="sendSubject">Subject</label>
      <input id="sendSubject" required maxlength="128" />
    </div>
    <div>
      <label for="sendBody">Message body (text only -- no attachments)</label>
      <textarea id="sendBody" required></textarea>
    </div>
    <button id="sendBtn" type="submit">Send message</button>
  </form>
  <div id="results" class="notice">
    Connects, logs in, and downloads whatever mail is waiting (up to
    one block, 5 messages). Can take a while (AX.25 connect + login +
    proposal exchange), so don't worry if it sits for a bit.
  </div>
</div>
<script>
(function () {
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";
  var results = document.getElementById("results");
  var form = document.getElementById("checkForm");
  var submitBtn = document.getElementById("submitBtn");
  var passwordInput = document.getElementById("password");
  var sendForm = document.getElementById("sendForm");
  var sendBtn = document.getElementById("sendBtn");
  var sendPasswordInput = document.getElementById("sendPassword");
  var checkTabBtn = document.getElementById("checkTabBtn");
  var sendTabBtn = document.getElementById("sendTabBtn");

  var CHECK_NOTICE = "Connects, logs in, and downloads whatever mail " +
    "is waiting (up to one block, 5 messages). Can take a while " +
    "(AX.25 connect + login + proposal exchange), so don't worry if " +
    "it sits for a bit.";
  var SEND_NOTICE = "Send-only, text body only (no attachments) -- " +
    "see winlink.py's module docstring for the full scope. Connects, " +
    "proposes the message, uploads it if the gateway accepts, then " +
    "declines whatever the gateway offers back. UNVERIFIED against a " +
    "real gateway as of this build -- see PROJECT.md.";

  checkTabBtn.addEventListener("click", function () {
    checkTabBtn.classList.add("active");
    sendTabBtn.classList.remove("active");
    form.classList.remove("hidden");
    sendForm.classList.add("hidden");
    results.innerHTML = '<div class="notice">' + CHECK_NOTICE + "</div>";
  });

  sendTabBtn.addEventListener("click", function () {
    sendTabBtn.classList.add("active");
    checkTabBtn.classList.remove("active");
    sendForm.classList.remove("hidden");
    form.classList.add("hidden");
    results.innerHTML = '<div class="notice">' + SEND_NOTICE + "</div>";
  });

  function authedUrl(path) {
    if (!token) return path;
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(token);
  }

  document.getElementById("terminalLink").href = authedUrl("/");
  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("mapLink").href = authedUrl("/map");

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function splitAddresses(value) {
    return value.split(",").map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function renderMessages(messages) {
    if (!messages.length) {
      results.innerHTML = '<div class="notice">No mail waiting.</div>';
      return;
    }

    var html = "";

    messages.forEach(function (m) {
      // B2 ("FC") messages carry the real structured header (from_/to/
      // cc/mid); legacy ascii ("FB") messages only ever have whatever
      // the proposal line itself carried (sender/mid) -- see
      // winlink.py's module docstring for why the two tiers differ.
      var hdr;

      if (m.from_) {
        hdr = "FROM " + m.from_ + " TO " + (m.to || []).join(", ");

        if (m.cc && m.cc.length) {
          hdr += " CC " + m.cc.join(", ");
        }

        hdr += "  (" + m.mid + ")";
      } else {
        hdr = "FROM " + m.proposal.sender + "  (" + m.proposal.mid + ")";
      }

      html += '<div class="msg"><div class="hdr">' + escapeHtml(m.title) +
        "</div><div class=\\"hdr\\">" + escapeHtml(hdr) + "</div>" +
        '<div class="body">' + escapeHtml(m.body) + "</div>";

      if (m.attachments && m.attachments.length) {
        var names = m.attachments.map(function (a) {
          return a.name + " (" + a.size + " bytes)";
        }).join(", ");

        html += '<div class="hdr">Attachments: ' + escapeHtml(names) + "</div>";
      }

      html += "</div>";
    });

    results.innerHTML = html;
  }

  form.addEventListener("submit", async function (e) {
    e.preventDefault();

    var gateway = document.getElementById("gateway").value.trim();
    var password = passwordInput.value;
    var mycall = document.getElementById("mycall").value.trim();

    if (!gateway || !password) return;

    submitBtn.disabled = true;
    results.innerHTML = '<div class="notice">Connecting...</div>';

    var body = { gateway: gateway, password: password };
    if (mycall) body.mycall = mycall;

    try {
      var res = await fetch(authedUrl("/winlink/check"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      });
      var payload = await res.json();

      if (payload.ok) {
        renderMessages(payload.result);
      } else {
        results.innerHTML = '<div class="err">' +
          escapeHtml((payload.error && payload.error.message) || "Request failed") +
          "</div>";
      }
    } catch (err) {
      results.innerHTML = '<div class="err">' + escapeHtml(String(err)) + "</div>";
    } finally {
      // Never left sitting in the form after a submit attempt,
      // success or failure -- see this page's own comment in
      // kamxl_rest.py for why.
      passwordInput.value = "";
      submitBtn.disabled = false;
    }
  });

  sendForm.addEventListener("submit", async function (e) {
    e.preventDefault();

    var gateway = document.getElementById("sendGateway").value.trim();
    var password = sendPasswordInput.value;
    var mycall = document.getElementById("sendMycall").value.trim();
    var to = splitAddresses(document.getElementById("sendTo").value);
    var cc = splitAddresses(document.getElementById("sendCc").value);
    var subject = document.getElementById("sendSubject").value.trim();
    var msgBody = document.getElementById("sendBody").value;

    if (!gateway || !password || !to.length || !subject || !msgBody) return;

    sendBtn.disabled = true;
    results.innerHTML = '<div class="notice">Connecting...</div>';

    var reqBody = {
      gateway: gateway,
      password: password,
      messages: [{ to: to, cc: cc, subject: subject, body: msgBody }]
    };
    if (mycall) reqBody.mycall = mycall;

    try {
      var res = await fetch(authedUrl("/winlink/send"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reqBody)
      });
      var payload = await res.json();

      if (payload.ok) {
        var accepted = payload.result || [];

        if (accepted.length) {
          results.innerHTML = '<div class="notice">Sent -- gateway ' +
            "accepted message ID " + escapeHtml(accepted[0]) + ".</div>";
          sendForm.reset();
        } else {
          results.innerHTML = '<div class="notice">Connected, but the ' +
            "gateway did not accept the message (rejected, deferred, " +
            "or errored -- nothing more this UI can tell you about " +
            "why).</div>";
        }
      } else {
        results.innerHTML = '<div class="err">' +
          escapeHtml((payload.error && payload.error.message) || "Request failed") +
          "</div>";
      }
    } catch (err) {
      results.innerHTML = '<div class="err">' + escapeHtml(String(err)) + "</div>";
    } finally {
      sendPasswordInput.value = "";
      sendBtn.disabled = false;
    }
  });

  if (!token) {
    results.innerHTML =
      '<div class="notice">No ?token=... in this page\\'s URL. If the ' +
      "server was started with authentication, requests below will " +
      "fail with 401 -- reload with ?token=&lt;your key&gt; appended " +
      "to the URL.</div>";
  }
})();
</script>
</body>
</html>
"""

# Milestone 7: a third self-contained page, served at GET /map, showing
# stations.py's station database on a Leaflet + OpenStreetMap map.
# Leaflet is loaded from cdnjs (this project's one allowed CDN for
# browser-side JS, per its own conventions) -- the only external
# network dependency in this whole project, and only for the browser
# viewing this page, not for kamxl_rest.py itself (which never talks
# to the internet).
#
# Markers use real APRS symbol-table/symbol-code icons (Dave's explicit
# choice: the full ~190-symbol set from an external icon pack, not a
# hand-built SVG subset or plain color-coded dots -- see PROJECT.md).
# This is a deliberate, one-off exception to the cdnjs-only rule above,
# since no APRS icon set is available there. Two real, separately
# licensed third-party sources are used, both pinned to a specific
# commit rather than an unpinned "latest" branch or tag:
#
#   - Sprite PNGs: hessu/aprs-symbols (github.com/hessu/aprs-symbols),
#     the APRS icon set maintained by Heikki Hannikainen/OH7LZB
#     (operator of aprs.fi). Licensing is mixed per-symbol (mostly
#     CC-BY-SA 2.0 original work, some public-domain-sourced elements)
#     -- see that repo's COPYRIGHT.md. Pinned to commit
#     f2286a9cd43eb6ba4501250b4c39fff111e3796c (2024-10-10) and served
#     via jsdelivr's GitHub-raw CDN.
#   - Symbol lookup table and sprite-grid math: ported inline (below,
#     in the page's own <script>) from OK-DMR/aprs-symbols'
#     aprs-symbols.js/aprs-symbols.css (github.com/OK-DMR/aprs-symbols,
#     MIT License, Copyright (c) 2019 Marek Sebera) -- only the ~15
#     lines of translation-table/grid-position logic are reused, not
#     loaded from a CDN.
#
# Real APRS symbol tables are literally "/" (primary) or "\" (alternate)
# -- any other symbol_table character means an *alternate table with
# overlay* (a digit/letter drawn over the base icon, e.g. a numbered
# object). This page renders the alternate table's base icon for that
# case but does not draw the overlay glyph itself -- a real, documented
# gap (see the aprsSymbolPosition() comment below), not a guess.
# symbol_table/symbol_code are still also shown as text in each
# marker's popup, as supplementary/debugging info.
MAP_HTML = r"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>kamxl station map</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
  html, body {
    margin: 0;
    height: 100%;
    background: #0b0f10;
    color: #d4f7d4;
    font-family: "Courier New", Courier, monospace;
  }
  body { display: flex; flex-direction: column; }
  .paneHeader {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 4px 10px;
    background: #101617;
    border-bottom: 1px solid #234;
    font-size: 12px;
    letter-spacing: 0.05em;
    color: #888;
    box-sizing: border-box;
    flex: 0 0 auto;
  }
  .paneHeader a, .paneHeader button {
    background: none;
    border: 1px solid #345;
    color: #9c9;
    font: inherit;
    font-size: 11px;
    padding: 2px 8px;
    cursor: pointer;
    text-decoration: none;
  }
  .paneHeader a:hover, .paneHeader button:hover { border-color: #5a8; color: #cfc; }
  #map { flex: 1; }
  #empty {
    position: absolute;
    top: 50px;
    left: 0;
    right: 0;
    text-align: center;
    color: #999;
    font-style: italic;
    pointer-events: none;
    z-index: 500;
  }
  .leaflet-popup-content { font-family: "Courier New", Courier, monospace; font-size: 12px; }
  /* Leaflet's L.divIcon normally draws a white box + border -- our
     APRS icons supply their own sprite background, so strip both. */
  .aprsIcon { background: transparent; border: none; }
</style>
</head>
<body>
<div class="paneHeader">
  <span>STATION MAP <span id="stationCount"></span></span>
  <span>
    <a href="#" id="terminalLink">terminal</a>
    <a href="#" id="pbbsLink">pbbs</a>
    <a href="#" id="winlinkLink">winlink</a>
  </span>
</div>
<div id="empty" style="display:none;">No stations heard yet.</div>
<div id="map"></div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function () {
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";
  var empty = document.getElementById("empty");
  var stationCount = document.getElementById("stationCount");

  function authedUrl(path) {
    if (!token) return path;
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(token);
  }

  document.getElementById("terminalLink").href = authedUrl("/");
  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");

  var map = L.map("map").setView([0, 0], 2);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: "&copy; OpenStreetMap contributors"
  }).addTo(map);

  var markers = {};
  var fitDone = false;

  function relativeTime(epochSeconds) {
    var deltaSeconds = Math.max(0, (Date.now() / 1000) - epochSeconds);

    if (deltaSeconds < 60) return Math.floor(deltaSeconds) + "s ago";
    if (deltaSeconds < 3600) return Math.floor(deltaSeconds / 60) + "m ago";
    if (deltaSeconds < 86400) return Math.floor(deltaSeconds / 3600) + "h ago";
    return Math.floor(deltaSeconds / 86400) + "d ago";
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
    // table's base icon position for that case; it does not compute
    // an overlay glyph position -- overlay rendering isn't
    // implemented (a real, documented gap, not a guess).
    var table = symbolTable === "/" ? 0 : 1;

    for (var row = 0; row < APRS_SYMBOL_ROWS.length; row++) {
      var col = APRS_SYMBOL_ROWS[row].indexOf(symbolCode);
      if (col !== -1) return { table: table, row: row, col: col };
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
      '<div style="width:' + APRS_ICON_SIZE + "px;height:" + APRS_ICON_SIZE + "px;" +
      "background-image:url(&quot;" + spriteUrl + "&quot;);" +
      "background-position:" + bgX + "px " + bgY + "px;" +
      'background-repeat:no-repeat;"></div>';

    return L.divIcon({
      className: "aprsIcon",
      html: html,
      iconSize: [APRS_ICON_SIZE, APRS_ICON_SIZE],
      iconAnchor: [APRS_ICON_SIZE / 2, APRS_ICON_SIZE / 2],
      popupAnchor: [0, -(APRS_ICON_SIZE / 2)]
    });
  }

  function popupHtml(station) {
    var html = "<b>" + station.callsign + "</b><br>";
    html += station.latitude.toFixed(5) + ", " + station.longitude.toFixed(5) + "<br>";
    if (station.comment) html += station.comment + "<br>";
    html += "symbol: " + station.symbol_table + station.symbol_code + "<br>";
    html += "heard " + relativeTime(station.last_heard) +
      " (" + station.packet_count + " report" +
      (station.packet_count === 1 ? "" : "s") + ")";
    return html;
  }

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
      });

      Object.keys(markers).forEach(function (callsign) {
        if (!seen[callsign]) {
          map.removeLayer(markers[callsign]);
          delete markers[callsign];
        }
      });

      if (!fitDone && stations.length) {
        fitDone = true;

        var bounds = stations.map(function (s) {
          return [s.latitude, s.longitude];
        });

        map.fitBounds(bounds, { maxZoom: 12, padding: [30, 30] });
      }
    } catch (err) {
      // Transient fetch failure -- next poll will retry.
    }
  }

  refresh();
  setInterval(refresh, 15000);
})();
</script>
</body>
</html>
"""

logger = logging.getLogger("kamxl_rest")
logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# Daemon client
# ---------------------------------------------------------------------------

class DaemonUnavailable(Exception):
    """Couldn't reach the daemon's Unix socket at all."""
    pass


class DaemonTimeout(Exception):
    """
    The daemon didn't send a response within the expected time.

    Distinct from DaemonUnavailable: the daemon is reachable, it's just
    not answering yet -- most often because the underlying KAM-XL
    command (or AX.25 connect/disconnect) is still legitimately in
    progress and hasn't hit its own timeout yet.
    """
    pass


class DaemonClient:
    """
    Talks to a running kamxl_daemon.py over its Unix socket.

    Opens a fresh connection per call (and per SSE stream) rather than
    holding one open and sharing it across HTTP handler threads --
    simpler to reason about correctness-wise than synchronizing a
    single shared connection, and Unix socket connections are cheap
    enough that this isn't a meaningful cost for a LAN tool.
    """

    # Default socket-level read timeout for a single call(). Must stay
    # comfortably above kamxl.py's own default command_timeout (10s) --
    # otherwise this socket read gives up before the daemon has even
    # had a chance to hit *its* timeout and send back a clean JSON
    # error, and callers see a raw, unhandled socket.timeout instead.
    # Endpoints that relay a caller-supplied timeout (connect/
    # disconnect/read_connected, which can legitimately run well past
    # this) pass their own, larger _socket_timeout per call instead of
    # relying on this default -- see kamxl_rest.py's _h_connect etc.
    def __init__(self, socket_path: str, timeout: float = 15) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def call(
        self,
        method: str,
        _socket_timeout: Optional[float] = None,
        **params: Any
    ) -> Dict[str, Any]:
        sock = self._connect(_socket_timeout)

        try:
            sock.sendall(self._encode("1", method, params))

            rfile = sock.makefile("r")

            try:
                line = rfile.readline()
            except socket.timeout:
                raise DaemonTimeout(
                    f"Daemon didn't respond to {method!r} within "
                    f"{_socket_timeout if _socket_timeout is not None else self.timeout}s"
                )

            if not line:
                raise DaemonUnavailable(
                    "Daemon closed the connection unexpectedly"
                )

            return json.loads(line)
        finally:
            sock.close()

    def stream_events(self, method: str, **params: Any):
        """
        Subscribe via ``method`` (e.g. "monitor.subscribe") on a
        dedicated connection and yield each subsequent event dict.
        Yields None roughly every ``self.timeout`` seconds of silence
        instead of blocking forever, so callers (the SSE handler) can
        use that to send a keepalive and notice a closed HTTP client.

        The connection is closed (ending the daemon-side subscription
        too) once the caller stops iterating.
        """
        sock = self._connect()

        try:
            sock.sendall(self._encode("1", method, params))

            rfile = sock.makefile("r")
            ack = rfile.readline()

            if not ack:
                raise DaemonUnavailable(
                    "Daemon closed the connection unexpectedly"
                )

            while True:
                # Wait for readability with select() rather than just
                # calling readline() and catching socket.timeout.
                # Real-hardware bug: once a timed-out read fires once
                # on a socket.makefile() object, CPython's SocketIO
                # sets a sticky _timeout_occurred flag -- every *later*
                # read on that same file object then raises
                # "OSError: cannot read from timed out object"
                # immediately, instead of actually trying again. That
                # silently killed this generator (and the SSE stream
                # built on it) after the very first keepalive with no
                # packets, observed live: the connection would run for
                # exactly one ``self.timeout`` window, crash, and
                # EventSource would reconnect and repeat -- meaning
                # any packet arriving during the brief reconnect gap
                # was lost. select() only calls readline() once data
                # is actually known to be waiting, so the socket-level
                # timeout path (and that sticky flag) is never hit.
                ready, _, _ = select.select([sock], [], [], self.timeout)

                if not ready:
                    yield None
                    continue

                line = rfile.readline()

                if not line:
                    return

                line = line.strip()

                if line:
                    yield json.loads(line)
        finally:
            sock.close()

    def _connect(self, timeout: Optional[float] = None) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout if timeout is not None else self.timeout)

        try:
            sock.connect(self.socket_path)
        except (ConnectionRefusedError, FileNotFoundError) as exc:
            sock.close()
            raise DaemonUnavailable(
                f"Can't reach kamxl_daemon.py at {self.socket_path}: {exc}"
            )

        return sock

    @staticmethod
    def _encode(request_id: str, method: str, params: Dict[str, Any]) -> bytes:
        return (json.dumps({
            "id": request_id,
            "method": method,
            "params": params,
        }) + "\n").encode("ascii")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

# Each route: (HTTP method, compiled path regex, handler method name).
# Handlers are looked up by name on RESTRequestHandler so they can be
# plain instance methods with access to self.daemon / self._send_json
# / etc.
ROUTES: Tuple[Tuple[str, "re.Pattern", str], ...] = (
    ("GET", re.compile(r"^/ping$"), "_h_ping"),
    ("GET", re.compile(r"^/status$"), "_h_status"),
    ("GET", re.compile(r"^/configuration$"), "_h_get_configuration"),
    ("GET", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)$"), "_h_get_typed"),
    ("PUT", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)$"), "_h_set_typed"),
    ("GET", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)/raw$"), "_h_get_raw"),
    ("PUT", re.compile(r"^/params/(?P<command>[A-Za-z0-9_]+)/raw$"), "_h_set_raw"),
    ("POST", re.compile(r"^/connect$"), "_h_connect"),
    ("POST", re.compile(r"^/disconnect$"), "_h_disconnect"),
    ("POST", re.compile(r"^/connected/send$"), "_h_send_connected"),
    ("GET", re.compile(r"^/connected/read$"), "_h_read_connected"),
    ("GET", re.compile(r"^/monitor/stream$"), "_h_monitor_stream"),
    ("GET", re.compile(r"^/$"), "_h_terminal_page"),
    ("POST", re.compile(r"^/terminal/exec$"), "_h_terminal_exec"),
    ("GET", re.compile(r"^/pbbs$"), "_h_pbbs_page"),
    ("GET", re.compile(r"^/pbbs/messages$"), "_h_pbbs_list_messages"),
    ("GET", re.compile(r"^/pbbs/messages/(?P<number>\d+)$"), "_h_pbbs_read_message"),
    ("GET", re.compile(r"^/map$"), "_h_map_page"),
    ("GET", re.compile(r"^/stations$"), "_h_stations_list"),
    ("GET", re.compile(r"^/stations/(?P<callsign>[A-Za-z0-9\-]+)$"), "_h_stations_get"),
    ("POST", re.compile(r"^/winlink/check$"), "_h_winlink_check_mail"),
    ("POST", re.compile(r"^/winlink/send$"), "_h_winlink_send_message"),
    ("GET", re.compile(r"^/winlink/account/(?P<callsign>[A-Za-z0-9\-]+)$"), "_h_winlink_account_exists"),
    ("GET", re.compile(r"^/winlink/gateways/nearby$"), "_h_winlink_gateways_nearby"),
    ("GET", re.compile(r"^/winlink/gateways$"), "_h_winlink_gateways"),
    ("GET", re.compile(r"^/winlink$"), "_h_winlink_page"),
)


class RESTRequestHandler(BaseHTTPRequestHandler):
    # Set on the class by serve() below, so every handler instance
    # (one per request, per BaseHTTPRequestHandler's design) shares
    # the same DaemonClient and API key without needing a custom
    # server subclass to pass them through.
    daemon: DaemonClient
    api_key: Optional[str]

    def log_message(self, fmt: str, *args: Any) -> None:
        # Route stdlib's own per-request access log through our
        # logger instead of straight to stderr, so -v/--verbose and
        # the rest of the logging setup applies to it too. Skips
        # /favicon.ico -- every browser requests it automatically and
        # unauthenticated on first load of any page here, and logging
        # that routine, harmless request (as a 401, no less -- see
        # do_GET() below) just adds noise, not a real access worth a
        # line in the log.
        if urlparse(self.path).path == "/favicon.ico":
            return

        logger.info("%s - %s", self.address_string(), fmt % args)

    # -- Dispatch -----------------------------------------------------

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/favicon.ico":
            # Browsers request this unconditionally on first load of
            # any page here, without the Authorization header or
            # ?token= -- there's no cookie/session for them to have
            # picked it up from. Left to the normal dispatch path,
            # that always fails auth and logs a 401 that looks like a
            # security problem but is really just a browser being a
            # browser. Answering 204 here (no favicon to serve, and
            # none needed) tells the browser "nothing here, don't ask
            # again" instead -- deliberately bypasses _check_auth()
            # entirely, since there's nothing sensitive being served.
            self.send_response(204)
            self.end_headers()
            return

        self._dispatch("GET")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, http_method: str) -> None:
        if not self._check_auth():
            return

        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        method_matched_path = False

        for route_method, pattern, handler_name in ROUTES:
            match = pattern.match(path)

            if match is None:
                continue

            method_matched_path = True

            if route_method != http_method:
                continue

            handler = getattr(self, handler_name)

            try:
                handler(match.groupdict(), query)
            except DaemonTimeout as exc:
                self._send_json(504, {
                    "ok": False,
                    "error": {"type": "DaemonTimeout", "message": str(exc)},
                })
            except DaemonUnavailable as exc:
                self._send_json(503, {
                    "ok": False,
                    "error": {"type": "DaemonUnavailable", "message": str(exc)},
                })
            except Exception as exc:
                logger.exception("Unhandled error in %s", handler_name)
                self._send_json(500, {
                    "ok": False,
                    "error": {"type": type(exc).__name__, "message": str(exc)},
                })

            return

        if method_matched_path:
            self._send_json(405, {
                "ok": False,
                "error": {"type": "MethodNotAllowed", "message": f"{http_method} not allowed on {path}"},
            })
        else:
            self._send_json(404, {
                "ok": False,
                "error": {"type": "NotFound", "message": f"No such endpoint: {path}"},
            })

    def _check_auth(self) -> bool:
        if not self.api_key:
            return True

        header = self.headers.get("Authorization", "")
        expected = f"Bearer {self.api_key}"

        if secrets.compare_digest(header, expected):
            return True

        # The web terminal (milestone 4) and live monitor streaming
        # (milestone 5) both run in a browser context that can't
        # always set a custom header -- EventSource never can, and a
        # page loaded from a bare URL shouldn't require typing the
        # token into a JS prompt. Accept the same token as a
        # `?token=` query parameter as a fallback. Checked second
        # (the header is still preferred) since a URL is more likely
        # to end up logged or in browser history than a header is.
        query = parse_qs(urlparse(self.path).query)
        token = query.get("token", [None])[0]

        if token is not None and secrets.compare_digest(token, self.api_key):
            return True

        self._send_json(401, {
            "ok": False,
            "error": {
                "type": "Unauthorized",
                "message": (
                    "Missing or invalid Authorization: Bearer <token> "
                    "header (or ?token=<key> query parameter)"
                ),
            },
        })
        return False

    # -- Body / response helpers --------------------------------------

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))

        if length == 0:
            return {}

        raw = self.rfile.read(length)

        if not raw.strip():
            return {}

        return json.loads(raw)

    def _send_json(self, status: int, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _relay(
        self,
        method: str,
        _socket_timeout: Optional[float] = None,
        **params: Any
    ) -> None:
        """
        Call a daemon method and translate its {"ok": ...} response
        directly into the equivalent HTTP response.

        _socket_timeout overrides how long we wait on our end for the
        daemon's reply. It must stay above whatever "timeout" (if any)
        is being passed through in **params for the underlying KAM-XL
        operation -- otherwise this call gives up before the daemon
        could possibly have an answer yet, surfacing as a raw
        DaemonTimeout instead of the operation actually completing.
        """
        response = self.daemon.call(
            method,
            _socket_timeout=_socket_timeout,
            **params
        )

        if response.get("ok"):
            self._send_json(200, {"ok": True, "result": response.get("result")})
            return

        error = response.get("error") or {}

        status = (
            400
            if error.get("type") == "MissingParam"
            else _DAEMON_ERROR_STATUS
        )

        self._send_json(status, {"ok": False, "error": error})

    # -- Endpoint handlers ----------------------------------------------

    def _h_ping(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("ping")

    def _h_status(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("status")

    def _h_get_configuration(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get_configuration")

    def _h_get_typed(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get_typed", command=params["command"])

    def _h_set_typed(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "value" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'value'"},
            })
            return

        self._relay("set_typed", command=params["command"], value=body["value"])

    def _h_get_raw(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("get", command=params["command"])

    def _h_set_raw(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "value" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'value'"},
            })
            return

        self._relay("set", command=params["command"], value=body["value"])

    def _h_connect(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "callsign" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'callsign'"},
            })
            return

        connect_timeout = body.get("timeout", 60)

        self._relay(
            "connect_station",
            callsign=body["callsign"],
            via=body.get("via"),
            timeout=connect_timeout,
            # +5s margin over the KAM-XL-side timeout we just passed
            # through, so this socket doesn't give up before the
            # daemon could possibly have an answer.
            _socket_timeout=connect_timeout + 5,
        )

    def _h_disconnect(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()
        disconnect_timeout = body.get("timeout", 30)
        command_mode_timeout = body.get("command_mode_timeout", 5)

        self._relay(
            "disconnect_station",
            timeout=disconnect_timeout,
            command_mode_timeout=command_mode_timeout,
            # The Ctrl-C step and the DISCONNECT step run sequentially
            # on the daemon side, so the worst case is their sum, not
            # just the larger of the two.
            _socket_timeout=disconnect_timeout + command_mode_timeout + 5,
        )

    def _h_send_connected(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "text" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'text'"},
            })
            return

        self._relay(
            "send_connected",
            text=body["text"],
            add_cr=body.get("add_cr", True),
        )

    def _h_read_connected(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        timeout = float(query.get("timeout", ["5"])[0])
        self._relay(
            "read_connected",
            timeout=timeout,
            _socket_timeout=timeout + 5,
        )

    def _h_terminal_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = TERMINAL_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_terminal_exec(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        if "command" not in body:
            self._send_json(400, {
                "ok": False,
                "error": {"type": "MissingParam", "message": "Missing required body field: 'command'"},
            })
            return

        command_timeout = body.get("timeout", 10)

        self._relay(
            "send_command",
            command=body["command"],
            timeout=command_timeout,
            _socket_timeout=command_timeout + 5,
        )

    def _h_monitor_stream(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            for event in self.daemon.stream_events("monitor.subscribe"):
                if event is None:
                    chunk = b": keepalive\n\n"
                else:
                    chunk = f"data: {json.dumps(event)}\n\n".encode("utf-8")

                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # Client navigated away / closed the EventSource -- not an
            # error, just the end of this stream.
            pass

    # -- PBBS (milestone 6) ---------------------------------------------
    #
    # Read-only wrapper around the KAM-XL's own firmware PBBS. Each
    # call drives a full connect/command/disconnect cycle on the
    # daemon side (see KAMXL.list_pbbs_messages()/read_pbbs_message()),
    # so the socket timeout budget has to cover connect_timeout +
    # read_timeout + disconnect_station()'s own worst case (up to
    # ~35s between its command_mode_timeout and timeout defaults),
    # not just the individual step timeouts.

    def _h_pbbs_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = PBBS_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_pbbs_list_messages(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        mypbbs = query.get("mypbbs", [None])[0]
        connect_timeout = float(query.get("connect_timeout", ["15"])[0])
        read_timeout = float(query.get("read_timeout", ["10"])[0])

        self._relay(
            "pbbs.list_messages",
            mypbbs=mypbbs,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + read_timeout + 40,
        )

    def _h_pbbs_read_message(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        mypbbs = query.get("mypbbs", [None])[0]
        connect_timeout = float(query.get("connect_timeout", ["15"])[0])
        read_timeout = float(query.get("read_timeout", ["10"])[0])

        self._relay(
            "pbbs.read_message",
            number=int(params["number"]),
            mypbbs=mypbbs,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + read_timeout + 40,
        )

    # -- Stations / map (milestone 7) ------------------------------------
    #
    # Unlike PBBS, these never drive a connect/command/disconnect
    # cycle -- stations.list/stations.get just read the daemon's
    # already-built-up, in-memory StationTracker (fed passively by its
    # always-on monitor thread), so the default socket timeout is
    # plenty and no per-call _socket_timeout override is needed.

    def _h_map_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = MAP_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_stations_list(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("stations.list")

    def _h_stations_get(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay("stations.get", callsign=params["callsign"])

    # -- Winlink (milestone 8) -------------------------------------------
    #
    # Receive-only MVP (see winlink.py's module docstring) -- drives a
    # full connect/handshake/proposal/message/disconnect cycle on the
    # daemon side (KAMXL.check_winlink_mail()), so the socket timeout
    # budget mirrors the PBBS endpoints' reasoning above. POST with a
    # JSON body, not GET with query params, specifically because the
    # request carries a real account password -- never acceptable in
    # a URL (query strings end up in server access logs, browser
    # history, etc.).

    def _h_winlink_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = WINLINK_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _h_winlink_check_mail(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = self._read_json_body()

        for field in ("gateway", "password"):
            if field not in body:
                self._send_json(400, {
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required body field: {field!r}",
                    },
                })
                return

        connect_timeout = body.get("connect_timeout", 60)
        read_timeout = body.get("read_timeout", 30)

        self._relay(
            "winlink.check_mail",
            gateway=body["gateway"],
            password=body["password"],
            mycall=body.get("mycall"),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + (read_timeout * 3) + 40,
        )

    def _h_winlink_send_message(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        # Send-only, text-body-only, B2/FC only -- see winlink.py's
        # module docstring's "SEND SUPPORT" note for the full scope,
        # and its "UNVERIFIED AGAINST A REAL GATEWAY" caveat, which
        # still applies as of this endpoint's addition. Same POST/JSON
        # body reasoning as _h_winlink_check_mail() above (a real
        # account password, never in a URL).
        body = self._read_json_body()

        for field in ("gateway", "password", "messages"):
            if field not in body:
                self._send_json(400, {
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required body field: {field!r}",
                    },
                })
                return

        if not isinstance(body["messages"], list) or not body["messages"]:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "MissingParam",
                    "message": "'messages' must be a non-empty list",
                },
            })
            return

        for message in body["messages"]:
            for field in ("to", "subject", "body"):
                if field not in message:
                    self._send_json(400, {
                        "ok": False,
                        "error": {
                            "type": "MissingParam",
                            "message": f"Each message needs a {field!r} field",
                        },
                    })
                    return

        connect_timeout = body.get("connect_timeout", 60)
        read_timeout = body.get("read_timeout", 30)

        self._relay(
            "winlink.send_message",
            gateway=body["gateway"],
            password=body["password"],
            messages=body["messages"],
            mycall=body.get("mycall"),
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            _socket_timeout=connect_timeout + (read_timeout * 3) + 40,
        )

    # -- Winlink web-service API (August 2026) ----------------------------
    #
    # A different Winlink surface entirely from check/send above: these
    # three call api.winlink.org's HTTP API (winlink_api.py), not the
    # KAM-XL's own AX.25/B2F session -- no serial port involved, no
    # account password either, so plain GET with query params is fine
    # here (unlike check/send, which need POST/JSON specifically to
    # keep a password out of the URL). The daemon reads the actual
    # WINLINK_API_KEY itself (see kamxl_daemon.py) -- this REST layer
    # never sees or handles that key.
    #
    # 20s socket-timeout headroom is generous for a single HTTPS
    # round-trip to api.winlink.org (winlink_api.py's own default is
    # 15s) -- deliberately not tight, since a slow/loaded WDT server
    # shouldn't surface here as a raw DaemonTimeout when the daemon
    # itself is still legitimately waiting on a response.

    def _h_winlink_account_exists(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        self._relay(
            "winlink.account_exists",
            callsign=params["callsign"],
            _socket_timeout=20,
        )

    def _h_winlink_gateways(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        mode = query.get("mode", ["AnyAll"])[0]
        history_hours = int(query.get("history_hours", ["48"])[0])
        service_codes = query.get("service_codes", ["PUBLIC"])
        if len(service_codes) == 1 and "," in service_codes[0]:
            service_codes = [code.strip() for code in service_codes[0].split(",")]

        self._relay(
            "winlink.gateway_status",
            mode=mode,
            history_hours=history_hours,
            service_codes=service_codes,
            _socket_timeout=20,
        )

    def _h_winlink_gateways_nearby(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        if "lat" not in query or "lon" not in query:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "MissingParam",
                    "message": "Missing required query param: 'lat' and/or 'lon'",
                },
            })
            return

        max_distance_km = query.get("max_distance_km", [None])[0]
        limit = query.get("limit", [None])[0]

        self._relay(
            "winlink.nearby_gateways",
            latitude=float(query["lat"][0]),
            longitude=float(query["lon"][0]),
            max_distance_km=float(max_distance_km) if max_distance_km else None,
            limit=int(limit) if limit else None,
            _socket_timeout=20,
        )


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------

class _RestHTTPServer(ThreadingHTTPServer):
    # socketserver's default poll_interval (0.5s) is how often
    # serve_forever()'s loop wakes up to check for a pending
    # shutdown() -- overriding the default here (rather than passing
    # poll_interval at each serve_forever() call site) covers both
    # main() and anything else -- e.g. tests/test_rest.py -- that
    # calls serve_forever() directly on a server built by serve().
    def serve_forever(self, poll_interval: float = 0.1) -> None:
        super().serve_forever(poll_interval=poll_interval)


def serve(
    daemon_socket: str,
    host: str,
    port: int,
    api_key: Optional[str],
) -> ThreadingHTTPServer:
    RESTRequestHandler.daemon = DaemonClient(daemon_socket)
    RESTRequestHandler.api_key = api_key

    return _RestHTTPServer((host, port), RESTRequestHandler)


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(
        description="REST API for kamxl_daemon.py"
    )
    parser.add_argument(
        "--daemon-socket",
        default=os.environ.get("KAMXL_SOCKET", DEFAULT_DAEMON_SOCKET),
        help=f"Path to the daemon's Unix socket (default: {DEFAULT_DAEMON_SOCKET})",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("KAMXL_REST_HOST", DEFAULT_HOST),
        help=f"Address to bind to (default: {DEFAULT_HOST} -- all interfaces)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("KAMXL_REST_PORT", DEFAULT_PORT)),
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("KAMXL_REST_API_KEY"),
        help="Bearer token required on every request. Random one generated "
             "and printed at startup if not given. Pass --no-auth to disable "
             "(only if --host is 127.0.0.1 / localhost).",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Disable authentication. Refused unless --host is localhost.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Also log each proxied daemon call (DEBUG level)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.no_auth:
        if args.host not in ("127.0.0.1", "localhost"):
            parser.error(
                "--no-auth is only allowed with --host 127.0.0.1 "
                "(this API is reachable from your whole LAN otherwise)"
            )
        api_key = None
        logger.warning("Authentication disabled (--no-auth, localhost only)")
    else:
        api_key = args.api_key or secrets.token_urlsafe(32)

        if not args.api_key:
            logger.info("Generated API key (save this): %s", api_key)

    server = serve(args.daemon_socket, args.host, args.port, api_key)

    # Same reasoning as kamxl_daemon.py's main(): serve_forever() runs
    # on a background thread so that shutdown() -- called from the
    # signal handler's thread -- doesn't deadlock waiting for a loop
    # it would otherwise be blocking itself.
    shutdown_requested = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        logger.info("Shutting down (signal %s)...", signum)
        shutdown_requested.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    logger.info(
        "KAM-XL REST API: http://%s:%s -> %s",
        args.host, args.port, args.daemon_socket
    )

    try:
        while not shutdown_requested.is_set():
            shutdown_requested.wait(timeout=1)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


if __name__ == "__main__":
    main()
