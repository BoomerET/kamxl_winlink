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

# Dashboard: the landing page at GET / (moved there from the terminal
# page below, which now lives at GET /terminal -- see PROJECT.md for
# the reasoning). A self-contained page summarizing the other four:
# KAM-XL serial link health (GET /status), APRS station count/most-
# recently-heard (GET /stations), and a manual "Check PBBS" button
# (GET /pbbs/messages). Deliberately does NOT auto-check Winlink mail
# or auto-check PBBS on load -- unlike /status and /stations (cheap,
# passive reads of data the daemon already has in memory), both PBBS
# and Winlink mail checks drive a real AX.25 connect/command/
# disconnect cycle against actual radio hardware (see the comments
# above the PBBS and Winlink route handlers below); auto-triggering
# that just from loading a dashboard would be surprising on-air
# activity and a slow page load, not a good default. The Winlink card
# is a static shortcut only (no auto data pull at all) -- checking
# mail needs a password, which has no business being entered anywhere
# but the dedicated Winlink page's own form.
DASHBOARD_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>kamxl dashboard</title>
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
  #cards {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    padding: 16px;
    box-sizing: border-box;
  }
  .card {
    background: #101617;
    border: 1px solid #234;
    border-radius: 4px;
    padding: 14px 16px;
    min-width: 240px;
    flex: 1 1 260px;
    box-sizing: border-box;
  }
  .card h2 {
    margin: 0 0 10px;
    font-size: 12px;
    letter-spacing: 0.05em;
    color: #789;
    font-weight: normal;
    text-transform: uppercase;
  }
  .card .big { font-size: 20px; color: #cfc; }
  .card .sub { color: #9ab; font-size: 12px; margin-top: 4px; }
  .card a.action, .card button.action {
    display: inline-block;
    margin: 10px 8px 0 0;
    background: none;
    border: 1px solid #345;
    color: #9c9;
    font: inherit;
    font-size: 12px;
    padding: 4px 10px;
    cursor: pointer;
    text-decoration: none;
  }
  .card a.action:hover, .card button.action:hover { border-color: #5a8; color: #cfc; }
  .card button.action:disabled { opacity: 0.5; cursor: default; }
  /* APRS Messaging */
  #aprsMessagingCard {
    min-width: 300px;
  }

  #aprsMessageForm {
    display: flex;
    flex-direction: column;
    gap: 8px;
  }

  #aprsMessageForm > label[for] {
    display: block;
    font-size: 12px;
    color: #9ab;
    margin-bottom: -4px;
  }

  #aprsMessageForm > input[type="text"] {
    display: block;
    width: 100%;
    box-sizing: border-box;
    padding: 7px 9px;
    background: #0b0f10;
    color: #d4f7d4;
    border: 1px solid #345;
    border-radius: 3px;
    font: inherit;
    font-size: 13px;
  }

  #aprsMessageForm > label:not([for]) {
    display: flex;
    align-items: flex-start;
    gap: 8px;
    font-size: 12px;
    line-height: 1.5;
    cursor: pointer;
  }

  #aprsMessageForm input[type="checkbox"] {
    flex: 0 0 auto;
    margin: 2px 0 0;
    accent-color: #6c9;
  }

  #aprsMessageForm button.action {
    align-self: flex-start;
    margin-top: 8px;
  }

  #aprsSendStatus {
    margin-top: 12px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  #aprsOutbox { margin-top: 16px; }
  #aprsOutbox h3, #aprsInbox h3 { font-size: 13px; color: #9ab; margin: 0 0 6px; }
  .outboxEntry {
    padding: 8px 0;
    border-bottom: 1px solid #234;
    font-size: 12px;
  }
  .outboxHead { color: #9ab; display: flex; gap: 8px; flex-wrap: wrap; }
  .outboxText { margin-top: 4px; overflow-wrap: anywhere; }
  .outboxStatus { margin-top: 4px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .ackBadge {
    display: inline-block;
    padding: 1px 6px;
    border-radius: 3px;
    border: 1px solid #456;
    font-size: 11px;
  }
  .ackBadge.pending { color: #cc6; border-color: #665; }
  .ackBadge.acked { color: #6f6; border-color: #3a5; }
  .ackBadge.rejected { color: #f77; border-color: #844; }
  .outboxEntry button.action { margin-top: 0; padding: 2px 8px; font-size: 11px; }
  #statusDot.live::before { content: "\\25CF "; color: #6f6; }
  #statusDot.down::before { content: "\\2715 "; color: #f66; }
  #statusDot.checking::before { content: "\\25CB "; color: #cc6; }
  .err { color: #f77; }
  table { border-collapse: collapse; width: 100%; font-size: 12px; margin-top: 8px; }
  th, td {
    text-align: left;
    padding: 3px 8px 3px 0;
    border-bottom: 1px solid #1c2526;
    white-space: nowrap;
  }
  td.subject { white-space: normal; }
  /* APRS activity history */
  #activityCard {
    margin: 16px 20px;
  }

  #activityChart {
    display: block;
    width: 100%;
    height: 160px;
    margin-top: 12px;
  }

  #activityChart .activity-bar {
    fill: #6c9;
  }

  #activityChart .activity-grid {
    stroke: #345;
    stroke-width: 1;
  }

  #activityChart .activity-label {
    fill: #9ab;
    font: 11px monospace;
  }

  #activitySummary {
    margin-top: 8px;
  }

  /* Persistent APRS reception history */
  #historyCard {
    margin: 16px 20px;
  }

  #historyChart {
    display: block;
    width: 100%;
    height: 180px;
    margin-top: 12px;
  }

  #historyChart .activity-bar {
    fill: #6c9;
  }

  #historyChart .activity-grid {
    stroke: #345;
    stroke-width: 1;
  }

  #historyChart .activity-label {
    fill: #9ab;
    font: 11px monospace;
  }

  #historySummary {
    margin-top: 8px;
  }

  #receptionHealth {
    margin-top: 6px;
    font-weight: bold;
  }

  #receptionHealth.receiving {
    color: #50fa7b;
  }

  #receptionHealth.waiting,
  #receptionHealth.quiet {
    color: #f1c40f;
  }

  #receptionHealth.disconnected,
  #receptionHealth.unavailable {
    color: #ff5555;
  }

  .digiToggle {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    margin-right: 15px;
    color: #9c9;
    font-size: 11px;
    cursor: pointer;
  }

  .digiToggle input {
    accent-color: #22a4a7;
    cursor: pointer;
  }

  #receptionStatsCard {
    margin-top: 16px;
  }

  .receptionMetrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px;
    margin: 18px 0;
  }

  .receptionMetrics > div {
    padding: 12px;
    border: 1px solid #344;
    border-radius: 6px;
    min-width: 0;
  }

  .receptionMetricValue {
    font-size: 26px;
    font-weight: bold;
    color: #50fa7b;
    font-variant-numeric: tabular-nums;
  }

  .receptionLists {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 20px;
  }

  .receptionLists h3 {
    font-size: 14px;
    color: #9ab;
  }

  .receptionRow {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    padding: 6px 0;
    border-bottom: 1px solid #344;
    font-family: monospace;
  }

  .receptionChartRow {
    margin-bottom: 10px;
  }

  .receptionFilters {
    display: flex;
    flex-direction: column;
    gap: 10px;
    margin: 12px 0;
  }

  .receptionFilters input,
  .receptionFilters select {
    background: #142525;
    color: #e0eeee;
    border: 1px solid #385454;
    border-radius: 5px;
    padding: 7px 9px;
    max-width: 100%;
  }

  .receptionFilters input {
    width: 100%;
    box-sizing: border-box;
  }

  .receptionFilterOptions {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }

  .receptionFilterOptions label {
    display: flex;
    flex-direction: column;
    gap: 5px;
    font-size: 12px;
  }

  #rxSourceCount {
    margin-bottom: 10px;
  }

  .receptionBarTrack {
    height: 8px;
    background: #1c3030;
    border-radius: 4px;
    overflow: hidden;
    margin-top: 4px;
  }

  .receptionBarFill {
    height: 100%;
    background: #50fa7b;
    border-radius: 4px;
    transition: width 0.4s ease;
  }

  #rxTopDigis .receptionBarFill {
    background: #22a4a7;
  }

  .receptionRow span:last-child {
    color: #50fa7b;
    font-variant-numeric: tabular-nums;
  }

  .receptionStationLink {
    color: inherit;
    text-decoration: none;
    border-bottom: 1px dotted #50fa7b;
  }

  .receptionStationLink:hover,
  .receptionStationLink:focus-visible {
    color: #50fa7b;
    text-decoration: underline;
  }

  #rxStatsStatus {
    margin: 12px 0;
  }

  @media (max-width: 650px) {
    .receptionMetrics {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .receptionLists {
      grid-template-columns: 1fr;
    }
  }
</style>
</head>
<body>
<div class="paneHeader">
  <span>DASHBOARD</span>
  <span>
    <a href="#" id="terminalLink">terminal</a>
    <a href="#" id="pbbsLink">pbbs</a>
    <a href="#" id="mapLink">map</a>
    <a href="#" id="winlinkLink">winlink</a>
  </span>
</div>
<div id="cards">
  <div class="card" id="statusCard">
    <h2>KAM-XL Link</h2>
    <div class="big"><span id="statusDot" class="checking">checking...</span></div>
    <div class="sub" id="statusDetail"></div>
    <div class="sub" id="receptionHealth" role="status" aria-live="polite">
      Reception: checking...
    </div>
    <div class="sub" id="diagnosticsDetail">
      Waiting for APRS diagnostics...
    </div>
  </div>

  <div class="card" id="stationsCard">
    <h2>APRS Stations</h2>
    <div class="big" id="stationCount">-</div>
    <div class="sub" id="stationDetail">Loading...</div>
    <a class="action" href="#" id="mapAction">View map &rarr;</a>
  </div>

  <div class="card" id="aprsMessagingCard">
    <h2>APRS Messaging</h2>

    <form id="aprsMessageForm">
      <label for="aprsRecipient">Recipient callsign</label>
      <input id="aprsRecipient" type="text"
             placeholder="N5BA-7" maxlength="9" required
             autocomplete="off">

      <label for="aprsMessageText">Message</label>
      <input id="aprsMessageText" type="text"
             placeholder="Your APRS message"
             maxlength="67" required autocomplete="off">

      <label>
        <input id="aprsConfirmTransmit" type="checkbox" required>
        Transmit now (VHF ready on Port 2, HF disconnected from Port 1)
      </label>

      <button class="action" id="aprsSendBtn" type="submit">
        Send APRS Message
      </button>
    </form>

    <div class="sub" id="aprsSendStatus"
         role="status" aria-live="polite">
      No message submitted.
    </div>
    <div id="aprsOutbox">
      <h3>Sent Messages</h3>
      <div class="sub" id="aprsOutboxStatus"
           role="status" aria-live="polite">
        Nothing sent this session.
      </div>
      <div id="aprsOutboxMessages"></div>
    </div>

    <div id="aprsInbox">
      <h3>Received Messages</h3>

      <button class="action" id="aprsRefreshInbox"
              type="button">
        Refresh Inbox
      </button>

      <div class="sub" id="aprsInboxStatus"
           role="status" aria-live="polite">
        Inbox not loaded.
      </div>

      <div id="aprsInboxMessages"></div>
    </div>
  </div>

  <div class="card" id="pbbsCard">
    <h2>PBBS</h2>
    <div class="sub" id="pbbsSummary">Not checked this session.</div>
    <button class="action" id="pbbsCheckBtn" type="button">Check PBBS</button>
    <a class="action" href="#" id="pbbsAction">Open PBBS &rarr;</a>
    <div id="pbbsPreview"></div>
  </div>

  <div class="card" id="winlinkCard">
    <h2>Winlink</h2>
    <div class="sub">Check or send mail from the Winlink page (needs your account password -- entered there only).</div>
    <a class="action" href="#" id="winlinkAction">Open Winlink &rarr;</a>
  </div>
</div>
<div class="card" id="activityCard">
  <h2>APRS Activity History — Last 60 Minutes</h2>

  <svg id="activityChart"
       viewBox="0 0 900 160"
       role="img"
       aria-label="APRS packets received per minute over the last hour">
  </svg>

  <div class="sub" id="activitySummary">
    Waiting for APRS activity...
  </div>
</div>

<div class="card" id="historyCard">
  <h2>APRS Reception History</h2>

  <div class="receptionFilters">
    <label for="historyRange">Display period</label>
    <select id="historyRange">
      <option value="24h">Last 24 hours</option>
      <option value="7d">Last 7 days</option>
      <option value="30d">Last 30 days</option>
    </select>
  </div>
  <div class="sub">
    Persistent SQLite history — survives daemon restarts
  </div>

  <svg id="historyChart"
       viewBox="0 0 900 180"
       role="img"
       aria-label="APRS packets received per hour over the last 24 hours">
  </svg>

  <div class="sub" id="historySummary" role="status">
    Loading reception history...
  </div>
</div>

<div class="card" id="receptionStatsCard">
  <h2>AX.25 Reception Statistics</h2>
  <div class="sub">Since daemon startup — refreshes every 15 seconds</div>

  <div class="receptionMetrics">
    <div>
      <div class="receptionMetricValue" id="rxTotal">-</div>
      <div class="sub">Packets received</div>
    </div>
    <div>
      <div class="receptionMetricValue" id="rxSources">-</div>
      <div class="sub">Unique sources</div>
    </div>
    <div>
      <div class="receptionMetricValue" id="rxRelayed">-</div>
      <div class="sub">With identified used digi</div>
    </div>
    <div>
      <div class="receptionMetricValue" id="rxDirect">-</div>
      <div class="sub">No identified used digi</div>
    </div>
  </div>

  <div class="receptionLists">
   <div>
     <h3>Top Heard Sources</h3>

     <div class="receptionFilters">
       <input
         id="rxSourceSearch"
         type="search"
         placeholder="Search callsign..."
         aria-label="Search heard stations"
         autocomplete="off"
       >

       <div class="receptionFilterOptions">
         <label>
           Minimum packets
           <select id="rxSourceMinimum">
             <option value="1">1+</option>
             <option value="5">5+</option>
             <option value="10">10+</option>
             <option value="25">25+</option>
           </select>
         </label>

         <label>
           Sort by
           <select id="rxSourceSort">
             <option value="most">Most heard</option>
             <option value="least">Least heard</option>
             <option value="callsign">Callsign A-Z</option>
           </select>
         </label>
       </div>
     </div>

     <div id="rxSourceCount" class="sub" role="status"></div>
     <div id="rxTopSources" class="sub">Loading...</div>
   </div>
    <div>
      <h3>Top Digipeaters</h3>
      <div id="rxTopDigis" class="sub">Loading...</div>
    </div>
  </div>

  <div class="sub" id="rxStatsStatus" role="status"></div>
  <div class="sub">
    Digipeaters are identified from used (*) path entries.
    A packet without an identified used digi is not necessarily a direct RF reception.
  </div>
</div>
<script>
(function () {
  var params = new URLSearchParams(location.search);
  var token = params.get("token") || "";

  function authedUrl(path) {
    if (!token) return path;
    var sep = path.indexOf("?") === -1 ? "?" : "&";
    return path + sep + "token=" + encodeURIComponent(token);
  }

  document.getElementById("terminalLink").href = authedUrl("/terminal");
  document.getElementById("pbbsLink").href = authedUrl("/pbbs");
  document.getElementById("mapLink").href = authedUrl("/map");
  document.getElementById("winlinkLink").href = authedUrl("/winlink");
  document.getElementById("mapAction").href = authedUrl("/map");
  document.getElementById("pbbsAction").href = authedUrl("/pbbs");
  document.getElementById("winlinkAction").href = authedUrl("/winlink");

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

  var aprsForm = document.getElementById("aprsMessageForm");
  var aprsSendBtn = document.getElementById("aprsSendBtn");
  var aprsSendStatus = document.getElementById("aprsSendStatus");

  aprsForm.addEventListener("submit", async function (e) {
    e.preventDefault();

    var recipient = document.getElementById("aprsRecipient").value.trim().toUpperCase();
    var message = document.getElementById("aprsMessageText").value.trim();

    // One checkbox covers all three daemon-side confirmations.
    var confirmTransmit = document.getElementById("aprsConfirmTransmit");

    if (!recipient || !message || !confirmTransmit.checked) {
      return;
    }

    aprsSendBtn.disabled = true;
    aprsSendStatus.textContent = "Submitting APRS message to TNC...";

    try {
      var res = await fetch(authedUrl("/aprs/send"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          addressee: recipient,
          message: message,
          path: "",
          confirm_transmit: true,
          confirm_vhf_port2_radio_ready: true,
          confirm_hf_port1_disconnected: true
        })
      });

      var payload = await res.json();

      if (payload.ok) {
        aprsSendStatus.textContent =
          "Message #" + payload.result.message_id +
          " submitted to TNC. Waiting for " + recipient +
          " to acknowledge it.";
        document.getElementById("aprsMessageText").value = "";
        refreshAprsOutbox();
      } else {
        aprsSendStatus.textContent =
          "APRS submission failed: " +
          ((payload.error && payload.error.message) || "Unknown error");
      }
    } catch (err) {
      aprsSendStatus.textContent =
        "APRS request failed: " + String(err);
    } finally {
      confirmTransmit.checked = false;
      aprsSendBtn.disabled = false;
    }
  });

  // -- Sent messages / acknowledgments --------------------------------
  //
  // Each sent message carries an APRS message ID; the daemon matches
  // incoming ackID/rejID replies to it. Polled only while something is
  // still awaiting an ack -- this never touches the TNC.

  var aprsOutboxStatus = document.getElementById("aprsOutboxStatus");
  var aprsOutboxMessages = document.getElementById("aprsOutboxMessages");
  var aprsOutboxTimer = null;

  function aprsConfirmationsChecked() {
    return document.getElementById("aprsConfirmTransmit").checked;
  }

  function clearAprsConfirmations() {
    document.getElementById("aprsConfirmTransmit").checked = false;
  }

  async function resendAprsMessage(messageId, button) {
    if (!aprsConfirmationsChecked()) {
      aprsSendStatus.textContent =
        "To resend #" + messageId +
        ", tick the Transmit now box above first.";
      return;
    }

    button.disabled = true;
    aprsSendStatus.textContent = "Resending message #" + messageId + "...";

    try {
      var res = await fetch(authedUrl("/aprs/send"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          resend_id: messageId,
          path: "",
          confirm_transmit: true,
          confirm_vhf_port2_radio_ready: true,
          confirm_hf_port1_disconnected: true
        })
      });
      var payload = await res.json();

      aprsSendStatus.textContent = payload.ok
        ? "Message #" + messageId + " resubmitted to TNC."
        : "Resend failed: " +
          ((payload.error && payload.error.message) || "Unknown error");
    } catch (err) {
      aprsSendStatus.textContent = "Resend request failed: " + String(err);
    } finally {
      clearAprsConfirmations();
      refreshAprsOutbox();
    }
  }

  function renderOutboxEntry(msg) {
    var entry = document.createElement("div");
    entry.className = "outboxEntry";

    var head = document.createElement("div");
    head.className = "outboxHead";
    var to = document.createElement("span");
    to.textContent = "To " + msg.addressee;
    var when = document.createElement("span");
    when.textContent = new Date(msg.sent_at * 1000).toLocaleString();
    var id = document.createElement("span");
    id.textContent = "#" + msg.message_id;
    head.appendChild(to);
    head.appendChild(when);
    head.appendChild(id);

    var text = document.createElement("div");
    text.className = "outboxText";
    text.textContent = msg.text;

    var status = document.createElement("div");
    status.className = "outboxStatus";
    var badge = document.createElement("span");
    badge.className = "ackBadge " + msg.status;

    if (msg.status === "acked") {
      badge.textContent = "Acknowledged";
    } else if (msg.status === "rejected") {
      badge.textContent = "Rejected";
    } else {
      badge.textContent = "Awaiting ack";
    }
    status.appendChild(badge);

    var detail = document.createElement("span");
    if (msg.status === "pending") {
      detail.textContent = "sent " +
        relativeTime(msg.last_sent_at || msg.sent_at) +
        (msg.attempts > 1 ? " (" + msg.attempts + " attempts)" : "");
    } else {
      detail.textContent = "by " + msg.acked_by + ", " +
        relativeTime(msg.acked_at);
    }
    status.appendChild(detail);

    if (msg.status === "pending") {
      var resend = document.createElement("button");
      resend.type = "button";
      resend.className = "action";
      resend.textContent = "Resend";
      resend.addEventListener("click", function () {
        resendAprsMessage(msg.message_id, resend);
      });
      status.appendChild(resend);
    }

    entry.appendChild(head);
    entry.appendChild(text);
    entry.appendChild(status);
    return entry;
  }

  async function refreshAprsOutbox() {
    var pending = 0;

    try {
      var result = await apiGet("/aprs/outbox");
      var messages = result.messages || [];

      aprsOutboxMessages.replaceChildren();
      messages.forEach(function (msg) {
        if (msg.status === "pending") pending++;
        aprsOutboxMessages.appendChild(renderOutboxEntry(msg));
      });

      if (!messages.length) {
        aprsOutboxStatus.textContent = "Nothing sent this session.";
      } else {
        aprsOutboxStatus.textContent = messages.length +
          " sent, " + pending + " awaiting acknowledgment.";
      }
    } catch (err) {
      aprsOutboxStatus.textContent =
        "Unable to load sent messages: " + String(err);
    }

    // Keep checking only while something is still unacknowledged.
    if (aprsOutboxTimer !== null) {
      clearTimeout(aprsOutboxTimer);
      aprsOutboxTimer = null;
    }
    if (pending > 0) {
      aprsOutboxTimer = setTimeout(refreshAprsOutbox, 10000);
    }
  }

  var aprsInboxBtn = document.getElementById("aprsRefreshInbox");
  var aprsInboxStatus = document.getElementById("aprsInboxStatus");
  var aprsInboxMessages = document.getElementById("aprsInboxMessages");

  async function refreshAprsInbox() {
    aprsInboxBtn.disabled = true;
    aprsInboxStatus.textContent = "Loading received messages...";

    try {
      var result = await apiGet(
        "/aprs/messages?addressee=N5BA&limit=100"
      );

      if (!result.enabled) {
        aprsInboxStatus.textContent = "APRS message storage unavailable.";
        aprsInboxMessages.replaceChildren();
        return;
      }

      var messages = result.messages || [];
      aprsInboxStatus.textContent =
        messages.length + " received message(s).";

      aprsInboxMessages.replaceChildren();

      if (!messages.length) {
        aprsInboxMessages.textContent = "No messages received yet.";
        return;
      }

      messages.forEach(function (msg) {
        var entry = document.createElement("div");
        entry.style.padding = "8px 0";
        entry.style.borderBottom = "1px solid #234";
        entry.style.fontSize = "12px";

        var heading = document.createElement("div");
        heading.style.color = "#9ab";

        var timestamp = new Date(msg.received_at * 1000);
        heading.textContent =
          msg.source + " — " + timestamp.toLocaleString();

        var body = document.createElement("div");
        body.style.marginTop = "4px";
        body.style.overflowWrap = "anywhere";

        if (msg.kind === "ack") {
          body.textContent = "ACK: " + msg.message_id;
        } else if (msg.kind === "rej") {
          body.textContent = "REJ: " + msg.message_id;
        } else {
          body.textContent = msg.message;
        }

        entry.appendChild(heading);
        entry.appendChild(body);
        aprsInboxMessages.appendChild(entry);
      });

    } catch (err) {
      aprsInboxStatus.textContent =
        "Unable to load inbox: " + String(err);
    } finally {
      aprsInboxBtn.disabled = false;
    }
  }

  aprsInboxBtn.addEventListener("click", refreshAprsInbox);

  function relativeTime(epochSeconds) {
    var deltaSeconds = Math.max(0, (Date.now() / 1000) - epochSeconds);

    if (deltaSeconds < 60) return Math.floor(deltaSeconds) + "s ago";
    if (deltaSeconds < 3600) return Math.floor(deltaSeconds / 60) + "m ago";
    if (deltaSeconds < 86400) return Math.floor(deltaSeconds / 3600) + "h ago";
    return Math.floor(deltaSeconds / 86400) + "d ago";
  }

  var statusDot = document.getElementById("statusDot");
  var statusDetail = document.getElementById("statusDetail");

  var diagnosticsDetail = document.getElementById("diagnosticsDetail");
  var activityChart = document.getElementById("activityChart");
  var activitySummary = document.getElementById("activitySummary");

  var historyChart = document.getElementById("historyChart");
  var historySummary = document.getElementById("historySummary");
  var historyRange = document.getElementById("historyRange");

  function localDayStart(date) {
    return new Date(
      date.getFullYear(),
      date.getMonth(),
      date.getDate()
    );
  }

  function buildDailyHistory(rows, days) {
    var today = localDayStart(new Date());
    var buckets = [];
    var counts = new Map();

    // Build local calendar days, not fixed 86,400-second intervals.
    for (var i = days - 1; i >= 0; i--) {
      var start = new Date(
        today.getFullYear(),
        today.getMonth(),
        today.getDate() - i
      );

      buckets.push({
        timestamp: Math.floor(start.getTime() / 1000),
        count: 0
      });
    }

    rows.forEach(function(row) {
      var hourDate = new Date(Number(row.hour_start) * 1000);
      var day = localDayStart(hourDate);
      var key = Math.floor(day.getTime() / 1000);

      counts.set(
        key,
        (counts.get(key) || 0) + Number(row.packet_count)
      );
    });

    buckets.forEach(function(bucket) {
      bucket.count = counts.get(bucket.timestamp) || 0;
    });

    return buckets;
  }

  function renderPersistentHistory(rows, range) {
    var ns = "http://www.w3.org/2000/svg";
    var width = 900;
    var height = 180;
    var left = 40;
    var right = 10;
    var top = 10;
    var bottom = 35;

    var plotWidth = width - left - right;
    var plotHeight = height - top - bottom;

    var hours;

    if (range === "7d" || range === "30d") {
      hours = buildDailyHistory(
        rows,
        range === "7d" ? 7 : 30
      );
    } else {
      // Current hour plus the preceding 23 UTC-hour buckets.
      var currentHour = Math.floor(Date.now() / 3600000) * 3600;
      var firstHour = currentHour - 23 * 3600;
      var counts = new Map();

      rows.forEach(function(row) {
        counts.set(Number(row.hour_start), Number(row.packet_count));
      });

      hours = [];

      for (var i = 0; i < 24; i++) {
        var hourStart = firstHour + i * 3600;

        hours.push({
          timestamp: hourStart,
          count: counts.get(hourStart) || 0
        });
      }
    }

    var peak = Math.max(0, ...hours.map(function(h) {
      return h.count;
    }));

    var tickStep = Math.max(1, Math.ceil(peak / 4));
    var maxPackets = tickStep * 4;

    var total = hours.reduce(function(sum, hour) {
      return sum + hour.count;
    }, 0);

    historyChart.replaceChildren();

    function addSvg(tag, attrs, text) {
      var el = document.createElementNS(ns, tag);

      Object.entries(attrs).forEach(function(entry) {
        el.setAttribute(entry[0], entry[1]);
      });

      if (text !== undefined) {
        el.textContent = text;
      }

      historyChart.appendChild(el);
      return el;
    }

    // Horizontal grid lines.
    for (var j = 0; j <= 4; j++) {
      var y = top + plotHeight * j / 4;
      var value = tickStep * (4 - j);

      addSvg("line", {
        x1: left,
        y1: y,
        x2: width - right,
        y2: y,
        class: "activity-grid"
      });

      addSvg("text", {
        x: left - 8,
        y: y + 4,
        "text-anchor": "end",
        class: "activity-label"
      }, String(value));
    }

    // Hourly bars.
    var slotWidth = plotWidth / hours.length;
    var barWidth = slotWidth * 0.7;

    hours.forEach(function(hour, index) {
      var barHeight = maxPackets > 0
        ? hour.count / maxPackets * plotHeight
        : 0;

      var x = left + index * slotWidth +
        (slotWidth - barWidth) / 2;

      var y = top + plotHeight - barHeight;

      var bar = addSvg("rect", {
        x: x,
        y: y,
        width: barWidth,
        height: barHeight,
        class: "activity-bar"
      });

      var label = range === "24h"
        ? new Date(hour.timestamp * 1000).toLocaleString()
        : new Date(hour.timestamp * 1000).toLocaleDateString([], {
            weekday: "short",
            month: "short",
            day: "numeric",
            year: "numeric"
          });

      var tooltip = document.createElementNS(ns, "title");
      tooltip.textContent = label + ": " +
        hour.count + " packets";
      bar.appendChild(tooltip);

      // Show labels at appropriate intervals for each view.
      var labelInterval = range === "7d" ? 1 :
        range === "30d" ? 5 : 3;

      if (index % labelInterval === 0) {
        addSvg("text", {
          x: left + (index + 0.5) * slotWidth,
          y: height - 12,
          "text-anchor": "middle",
          class: "activity-label"
        }, range === "24h"
          ? new Date(hour.timestamp * 1000)
              .toLocaleTimeString([], {
                hour: "2-digit",
                minute: "2-digit"
              })
          : new Date(hour.timestamp * 1000)
              .toLocaleDateString([], {
                month: "short",
                day: "numeric"
              }));
      }
    });
    var description = range === "7d"
      ? "7 local calendar days"
      : range === "30d"
        ? "30 local calendar days"
        : "24 hourly buckets";

    historySummary.textContent =
      total.toLocaleString() +
      " packets across " + description;
  }

  async function refreshPersistentHistory() {
    var range = historyRange.value;
    var now = new Date();
    var since;

    if (range === "7d" || range === "30d") {
      var days = range === "7d" ? 7 : 30;

      // Local midnight, including daylight-saving transitions.
      var firstDay = new Date(
        now.getFullYear(),
        now.getMonth(),
        now.getDate() - (days - 1)
      );

      since = Math.floor(firstDay.getTime() / 1000);
    } else {
      range = "24h";

      // Current UTC hour plus the preceding 23 hours.
      var currentHour = Math.floor(Date.now() / 3600000) * 3600;
      since = currentHour - 23 * 3600;
    }

    try {
      var history = await apiGet(
        "/aprs/history?since=" + since
      );

      if (!history.enabled) {
        historyChart.replaceChildren();
        historySummary.textContent =
          "Persistent reception history is disabled. " +
          "Start the daemon with --history-db to enable it.";
        return;
      }

      renderPersistentHistory(history.hourly_counts || [], range);

      historyChart.setAttribute(
        "aria-label",
        range === "24h"
          ? "APRS packets received per hour over the last 24 hourly buckets"
          : "APRS packets received per local calendar day over the last " +
            (range === "7d" ? "7" : "30") + " days"
      );

    } catch (err) {
      historyChart.replaceChildren();
      historySummary.textContent =
        "Reception history unavailable: " +
        (err.message || String(err));
    }
  }

  function renderActivityHistory(history) {
    if (!Array.isArray(history) || history.length !== 60) {
      activityChart.replaceChildren();
      activitySummary.textContent = "Activity history unavailable";
      return;
    }

    var ns = "http://www.w3.org/2000/svg";
    var width = 900;
    var height = 160;
    var left = 35;
    var right = 10;
    var top = 10;
    var bottom = 25;

    var plotWidth = width - left - right;
    var plotHeight = height - top - bottom;

    var peakPackets = Math.max(0, ...history);
    var tickStep = Math.max(1, Math.ceil(peakPackets / 4));
    var maxPackets = tickStep * 4;
    var total = history.reduce(function(sum, value) {
      return sum + value;
    }, 0);

    activityChart.replaceChildren();

    function addSvg(tag, attrs, text) {
      var el = document.createElementNS(ns, tag);

      Object.entries(attrs).forEach(function(entry) {
        el.setAttribute(entry[0], entry[1]);
      });

      if (text !== undefined) el.textContent = text;
      activityChart.appendChild(el);
      return el;
    }

    // Horizontal grid lines and packet-count labels.
    for (var i = 0; i <= 4; i++) {
      var y = top + (plotHeight * i / 4);
      var value = tickStep * (4 - i);

      addSvg("line", {
        x1: left,
        y1: y,
        x2: width - right,
        y2: y,
        class: "activity-grid"
      });

      addSvg("text", {
        x: left - 6,
        y: y + 4,
        "text-anchor": "end",
        class: "activity-label"
      }, String(Math.round(value)));
    }

    // One bar for each minute.
    var step = plotWidth / 60;
    var barWidth = Math.max(1, step - 2);

    history.forEach(function(count, index) {
      var barHeight = count / maxPackets * plotHeight;

      var bar = addSvg("rect", {
        x: left + index * step + 1,
        y: top + plotHeight - barHeight,
        width: barWidth,
        height: barHeight,
        class: "activity-bar"
      });

      var tooltip = document.createElementNS(ns, "title");
      tooltip.textContent =
        count + " packets, " + (59 - index) + " minutes ago";
      bar.appendChild(tooltip);
    });

    // Time labels.
    [
      [0, "-60m"],
      [15, "-45m"],
      [30, "-30m"],
      [45, "-15m"],
      [59, "Now"]
    ].forEach(function(item) {
      addSvg("text", {
        x: left + (item[0] + 0.5) * step,
        y: height - 5,
        "text-anchor": "middle",
        class: "activity-label"
      }, item[1]);
    });

    activitySummary.textContent =
      "Packets in last 60 minutes: " + total +
      " | Peak minute: " + peakPackets + " packets";
  }

  function formatUptime(seconds) {
    seconds = Math.floor(Math.max(0, seconds));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;

    if (hours) return hours + "h " + minutes + "m";
    if (minutes) return minutes + "m " + secs + "s";
    return secs + "s";
  }

  function updateReceptionHealth(connected, diag) {
    var element = document.getElementById("receptionHealth");

    if (!connected) {
      element.className = "disconnected";
      element.textContent = "Reception: Disconnected";
      return;
    }

    if (!diag) {
      element.className = "unavailable";
      element.textContent = "Reception: Unknown (diagnostics unavailable)";
      return;
    }

    var packets = Number(diag.packets_last_5m || 0);
    var uptime = Number(diag.uptime_seconds || 0);

    if (packets > 0) {
      element.className = "receiving";
      element.textContent = "Reception: Receiving (last 5 minutes)";
    } else if (diag.last_packet_at == null && uptime < 300) {
      element.className = "waiting";
      element.textContent = "Reception: Waiting for first packet";
    } else {
      element.className = "quiet";
      element.textContent = "Reception: Quiet (no packets in 5 minutes)";
    }
  }

  async function refreshStatus() {
    try {
      var status = await apiGet("/status");

      statusDot.className = status.connected ? "live" : "down";
      statusDot.textContent = status.connected ? "connected" : "disconnected";
      statusDetail.textContent =
        status.port + " -- " + status.monitor_subscribers +
        " monitor subscriber" + (status.monitor_subscribers === 1 ? "" : "s");
      var diag = status.diagnostics;
      updateReceptionHealth(status.connected, diag);

      if (diag) {
        renderActivityHistory(diag.activity_history);
        var portCounts = Object.entries(diag.packets_by_port || {})
          .map(function(entry) {
            return "Port " + entry[0] + ": " + entry[1];
          })
          .join(" | ");

        var lastHeard = diag.last_packet_at == null
          ? "Never"
          : relativeTime(diag.last_packet_at);

        diagnosticsDetail.textContent =
          "Packets: " + diag.packets_received +
          " | 1m: " + Number(diag.rate_1m || 0).toFixed(1) + "/min" +
          " | 5m: " + Number(diag.rate_5m || 0).toFixed(1) + "/min" +
          " | " + (portCounts || "No packets yet") +
          " | Last heard: " + lastHeard +
          " | Uptime: " + formatUptime(diag.uptime_seconds);
      } else {
        diagnosticsDetail.textContent = "Diagnostics unavailable";
        renderActivityHistory(null);
      }
    } catch (err) {
      statusDot.className = "down";
      statusDot.textContent = "unreachable";
      statusDetail.textContent = err.message || String(err);
      diagnosticsDetail.textContent = "Diagnostics unavailable";
      renderActivityHistory(null);
      var receptionHealth = document.getElementById("receptionHealth");
      receptionHealth.className = "unavailable";
      receptionHealth.textContent =
          "Reception: Unavailable (daemon unreachable)";
    }
  }

  var stationCount = document.getElementById("stationCount");
  var stationDetail = document.getElementById("stationDetail");

  async function refreshStations() {
    try {
      var stations = await apiGet("/stations");

      stationCount.textContent = stations.length;

      if (!stations.length) {
        stationDetail.textContent = "None heard yet.";
        return;
      }

      var mostRecent = stations.reduce(function (a, b) {
        return a.last_heard > b.last_heard ? a : b;
      });

      stationDetail.textContent =
        "Last heard: " + mostRecent.callsign + " (" +
        relativeTime(mostRecent.last_heard) + ")";
    } catch (err) {
      stationDetail.textContent = err.message || String(err);
    }
  }

  var pbbsSummary = document.getElementById("pbbsSummary");
  var pbbsPreview = document.getElementById("pbbsPreview");
  var pbbsCheckBtn = document.getElementById("pbbsCheckBtn");

  pbbsCheckBtn.addEventListener("click", async function () {
    pbbsCheckBtn.disabled = true;
    pbbsSummary.textContent = "Connecting to PBBS...";
    pbbsPreview.innerHTML = "";

    try {
      var messages = await apiGet("/pbbs/messages");

      pbbsSummary.textContent =
        messages.length + " message" + (messages.length === 1 ? "" : "s") +
        " -- checked just now.";

      var rows = messages.slice(0, 5).map(function (m) {
        return (
          "<tr><td>" + escapeHtml(m.number) + "</td>" +
          "<td>" + escapeHtml(m.from_call) + "</td>" +
          '<td class="subject">' + escapeHtml(m.subject) + "</td></tr>"
        );
      }).join("");

      pbbsPreview.innerHTML = rows
        ? "<table><thead><tr><th>#</th><th>From</th><th>Subject</th></tr></thead><tbody>" +
          rows + "</tbody></table>" +
          (messages.length > 5
            ? '<div class="sub">+ ' + (messages.length - 5) + " more -- see full inbox.</div>"
            : "")
        : "";
    } catch (err) {
      pbbsSummary.textContent = "Check failed: " + (err.message || String(err));
      pbbsPreview.innerHTML = "";
    } finally {
      pbbsCheckBtn.disabled = false;
    }
  });

  function renderReceptionRows(elementId, entries, linkToMap) {
    var element = document.getElementById(elementId);

    if (!entries || !entries.length) {
      element.textContent = "None heard yet.";
      return;
    }

    var maxPackets = Math.max.apply(null, entries.map(function(entry) {
      return Number(entry.packets) || 0;
    }));

    element.innerHTML = entries.map(function(entry) {
      var packets = Number(entry.packets) || 0;
      var percentage = maxPackets > 0
        ? Math.max(0, Math.min(100, packets / maxPackets * 100))
        : 0;
      var callsign = escapeHtml(entry.callsign);
      var label = callsign;

      if (linkToMap) {
        var url = authedUrl(
          "/map?station=" + encodeURIComponent(entry.callsign)
        );

        label = '<a class="receptionStationLink" href="' +
          escapeHtml(url) +
          '" target="_blank" rel="noopener noreferrer">' +
          callsign + '</a>';
      }

      return '<div class="receptionChartRow">' +
        '<div class="receptionRow">' +
          '<span>' + label + '</span>' +
          '<span>' + packets.toLocaleString() + '</span>' +
        '</div>' +
        '<div class="receptionBarTrack">' +
          '<div class="receptionBarFill" style="width:' +
            percentage.toFixed(1) + '%"></div>' +
        '</div>' +
      '</div>';
    }).join("");
  }

  var receptionSources = [];

  function filterReceptionSources(entries, query, minimum, sortBy) {
    var search = String(query || "").trim().toUpperCase();
    var minPackets = Number(minimum) || 1;

    var filtered = (entries || []).filter(function(entry) {
      return entry.callsign.toUpperCase().includes(search) &&
        Number(entry.packets) >= minPackets;
    });

    filtered.sort(function(a, b) {
      if (sortBy === "least") {
        return Number(a.packets) - Number(b.packets) ||
          a.callsign.localeCompare(b.callsign);
      }

      if (sortBy === "callsign") {
        return a.callsign.localeCompare(b.callsign);
      }

      return Number(b.packets) - Number(a.packets) ||
        a.callsign.localeCompare(b.callsign);
    });

    return filtered;
  }

  function updateReceptionSourceDisplay() {
    var search = document.getElementById("rxSourceSearch").value;
    var minimum = document.getElementById("rxSourceMinimum").value;
    var sortBy = document.getElementById("rxSourceSort").value;

    var filtered = filterReceptionSources(
      receptionSources, search, minimum, sortBy
    );

    // Keep the dashboard compact until the user changes a filter.
    var defaultView =
      !search.trim() && minimum === "1" && sortBy === "most";

    var visible = defaultView ? filtered.slice(0, 10) : filtered;

    renderReceptionRows("rxTopSources", visible, true);

    var count = document.getElementById("rxSourceCount");

    if (defaultView) {
      count.textContent = "Showing top " + visible.length +
        " of " + receptionSources.length + " heard sources";
    } else {
      count.textContent = "Showing " + visible.length +
        " of " + receptionSources.length + " heard sources";
    }
  }

  async function refreshReceptionStats() {
    var statusElement = document.getElementById("rxStatsStatus");

    try {
      var stats = await apiGet("/aprs/stats");

      document.getElementById("rxTotal").textContent =
        Number(stats.total_packets).toLocaleString();

      document.getElementById("rxSources").textContent =
        Number(stats.unique_sources).toLocaleString();

      document.getElementById("rxRelayed").textContent =
        Number(stats.used_digipeater_packets).toLocaleString();

      document.getElementById("rxDirect").textContent =
        Number(stats.no_identified_digipeater_packets).toLocaleString();

      receptionSources = Array.isArray(stats.all_sources)
        ? stats.all_sources
        : stats.top_sources || [];

      updateReceptionSourceDisplay();
      renderReceptionRows("rxTopDigis", stats.top_digipeaters, false);


      statusElement.textContent = "";
    } catch (err) {
      statusElement.textContent =
        "Reception statistics unavailable: " + (err.message || String(err));
    }
  }

  document.getElementById("rxSourceSearch").addEventListener(
    "input", updateReceptionSourceDisplay
  );

  document.getElementById("rxSourceMinimum").addEventListener(
    "change", updateReceptionSourceDisplay
  );

  document.getElementById("rxSourceSort").addEventListener(
    "change", updateReceptionSourceDisplay
  );

  historyRange.addEventListener("change", refreshPersistentHistory);
  refreshStatus();
  refreshStations();
  refreshReceptionStats();
  refreshPersistentHistory();
  refreshAprsOutbox();

  setInterval(refreshStatus, 15000);
  setInterval(refreshStations, 15000);
  setInterval(refreshReceptionStats, 15000);
  setInterval(refreshPersistentHistory, 60000);
})();
</script>
</body>
</html>
"""

# Milestone 4 (terminal) + milestone 5 (live monitor): a single
# self-contained page (no build step, no CDN dependency) served
# directly by this process at GET /terminal -- a live packet feed on
# top (EventSource against /monitor/stream), a raw Terminal Mode
# command box below it (same as milestone 4). Reads ?token=... from
# its own URL and carries it forward on every request it makes, since
# that's the auth mechanism this REST layer accepts specifically so a
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
      <a href="#" id="homeLink" style="background:none;border:1px solid #345;color:#9c9;font:inherit;font-size:11px;padding:2px 8px;text-decoration:none;">home</a>
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

  document.getElementById("homeLink").href = authedUrl("/");
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
    <a href="#" id="homeLink">home</a>
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

  document.getElementById("homeLink").href = authedUrl("/");
  document.getElementById("terminalLink").href = authedUrl("/terminal");
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
    <a href="#" id="homeLink">home</a>
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
      <input id="mycall" placeholder="e.g. N5BA-10" />
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
      <input id="sendMycall" placeholder="e.g. N5BA-10" />
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

  document.getElementById("homeLink").href = authedUrl("/");
  document.getElementById("terminalLink").href = authedUrl("/terminal");
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
# overlay* (a digit/letter meant to be drawn over the base icon, e.g. a
# numbered object). This page renders the alternate table's base icon
# for that case, with the overlay character drawn as a small text
# badge in the icon's corner -- not as sprite art, because
# hessu/aprs-symbols has none: its own README says outright "does not
# contain additional symbols for overlays yet ... Maybe later!" (the
# aprs-symbols-SIZE-2.png filename is reserved for it but has no real
# artwork behind it as of the pinned commit above). Drawing a text
# badge instead of guessing at or fabricating overlay art keeps this
# consistent with the project's "never fabricate third-party art"
# rule while still surfacing the real information.
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
  /* Small text badge for an APRS overlay character (a digit/letter
     drawn over the base icon) -- see the module comment above
     MAP_HTML for why this is text rather than sprite art. */
  .aprsOverlay {
    position: absolute;
    right: -2px;
    bottom: -2px;
    min-width: 14px;
    height: 14px;
    line-height: 14px;
    text-align: center;
    font: bold 11px sans-serif;
    color: #000;
    background: #fc3;
    border-radius: 3px;
    padding: 0 2px;
    box-shadow: 0 0 0 1px #000;
  }
  #stationDetails {
    position: absolute;
    top: 42px;
    right: 12px;
    width: min(360px, calc(100vw - 40px));
    max-height: calc(100vh - 70px);
    overflow-y: auto;
    background: #101b1c;
    border: 1px solid #3a6;
    border-radius: 6px;
    padding: 14px;
    box-sizing: border-box;
    z-index: 1100;
    box-shadow: 0 6px 24px #0009;
  }

  #stationDetails[hidden] {
    display: none;
  }

  #stationDetails h2 {
    margin: 0 0 12px;
    color: #9f9;
    font-size: 17px;
  }

  #stationDetails .detailRow {
    margin: 8px 0;
    overflow-wrap: anywhere;
  }

  #stationDetails .detailLabel {
    color: #999;
  }

  #closeStationDetails {
    float: right;
    background: transparent;
    border: 1px solid #456;
    color: #cfc;
    cursor: pointer;
  }

  .packetRecord {
    border-top: 1px solid #345;
    padding: 9px 0;
    overflow-wrap: anywhere;
  }

  .packetMeta {
    color: #9aa;
    font-size: 11px;
    margin-bottom: 4px;
  }

  .packetPayload {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: #0b1213;
    border: 1px solid #234;
    padding: 7px;
    margin-top: 5px;
    font-size: 11px;
  }

  #digiStationFilter {
    background: #101617;
    color: #9c9;
    border: 1px solid #345;
    font: inherit;
    font-size: 11px;
    padding: 2px 5px;
    margin-right: 12px;
    max-width: 170px;
  }

  #stationSearchForm {
    display: flex;
    align-items: center;
    gap: 5px;
    margin: 0 12px;
  }

  #stationSearch {
    background: #101617;
    color: #d4f7d4;
    border: 1px solid #345;
    border-radius: 3px;
    font: inherit;
    font-size: 11px;
    padding: 4px 7px;
    width: 170px;
    min-width: 90px;
  }

  #stationSearch:focus {
    outline: 1px solid #5a8;
  }

  .paneHeader {
    flex-wrap: wrap;
    gap: 6px;
  }

  #stationSelectionStatus:not([hidden]) {
    position: absolute;
    top: 48px;
    left: 12px;
    z-index: 1100;
    background: #101b1c;
    color: #d4f7d4;
    border: 1px solid #3a6;
    border-radius: 5px;
    padding: 9px 12px;
    max-width: min(400px, calc(100vw - 40px));
    font-size: 12px;
  }

</style>
</head>
<body>
<div class="paneHeader">
  <span>STATION MAP <span id="stationCount"></span></span>
  <form id="stationSearchForm" role="search">
    <input
      id="stationSearch"
      type="search"
      placeholder="Find callsign..."
      aria-label="Find station on map"
      autocomplete="off"
      list="stationSearchOptions"
    >
    <datalist id="stationSearchOptions"></datalist>
    <button type="submit">Find</button>
  </form>

  <span>
    <label class="digiToggle">
      <input type="checkbox" id="showDigiPaths">
      Show Digipeater Paths
    </label>
    <select id="digiStationFilter" aria-label="Filter digipeater paths by station">
      <option value="">All stations</option>
    </select>
    <a href="#" id="homeLink">home</a>
    <a href="#" id="terminalLink">terminal</a>
    <a href="#" id="pbbsLink">pbbs</a>
    <a href="#" id="winlinkLink">winlink</a>
  </span>
</div>
<div id="empty" style="display:none;">No stations heard yet.</div>
<div id="stationSelectionStatus" role="status" hidden></div>
<div id="map"></div>
<aside id="stationDetails" hidden aria-label="APRS station details">
  <button id="closeStationDetails" type="button">Close</button>
  <h2 id="detailCallsign">Station Details</h2>
  <div id="stationDetailsBody">Loading...</div>
</aside>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
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

  function popupHtml(station) {
    // escapeHtml() added alongside the overlay-badge work above --
    // used here too now that it exists, since this popup content
    // (particularly station.comment) is over-the-air text, not
    // trusted markup.
    var html = "<b>" + escapeHtml(station.callsign) + "</b><br>";
    html += station.latitude.toFixed(5) + ", " + station.longitude.toFixed(5) + "<br>";
    if (station.comment) html += escapeHtml(station.comment) + "<br>";
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
      detail("Comment", station.comment || "(none)");

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
    ("GET", re.compile(r"^/$"), "_h_dashboard_page"),
    ("GET", re.compile(r"^/terminal$"), "_h_terminal_page"),
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
    ("GET", re.compile(r"^/aprs/stats$"), "_h_aprs_stats"),
    ("GET", re.compile(r"^/aprs/history$"), "_h_aprs_history"),
    ("GET", re.compile(r"^/aprs/messages$"), "_h_aprs_messages"),
    ("POST", re.compile(r"^/aprs/send$"), "_h_aprs_send_message"),
    ("GET", re.compile(r"^/aprs/outbox$"), "_h_aprs_outbox"),
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

    def _h_dashboard_page(self, params: Dict[str, str], query: Dict[str, Any]) -> None:
        body = DASHBOARD_HTML.encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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

    def _h_aprs_stats(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        self._relay("aprs.stats")

    def _h_aprs_messages(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Return stored APRS messages without transmitting."""

        addressee = query.get("addressee", ["N5BA"])[0]

        try:
            limit = int(query.get("limit", ["100"])[0])
        except (ValueError, TypeError):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "limit must be an integer",
                },
            })
            return

        if not 1 <= limit <= 500:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "limit must be between 1 and 500",
                },
            })
            return

        self._relay(
            "aprs.messages",
            addressee=addressee,
            limit=limit,
        )

    def _h_aprs_history(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Return persistent APRS reception history."""

        since_values = query.get("since")

        if since_values is None:
            self._relay("aprs.history")
            return

        if len(since_values) != 1:
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "'since' must be specified exactly once",
                },
            })
            return

        try:
            since = int(since_values[0])
        except (TypeError, ValueError):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "'since' must be a Unix timestamp integer",
                },
            })
            return

        self._relay("aprs.history", since=since)

    def _h_aprs_send_message(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Submit one explicitly authorized APRS message to the daemon."""
        body = self._read_json_body()

        resend_id = body.get("resend_id")

        if resend_id is not None and (
            not isinstance(resend_id, str) or not resend_id
        ):
            self._send_json(400, {
                "ok": False,
                "error": {
                    "type": "InvalidParam",
                    "message": "resend_id must be a nonempty string",
                },
            })
            return

        # A resend takes addressee/text from the original message.
        required = () if resend_id is not None else ("addressee", "message")

        for field in required:
            if not isinstance(body.get(field), str) or not body[field]:
                self._send_json(400, {
                    "ok": False,
                    "error": {
                        "type": "MissingParam",
                        "message": f"Missing required body field: {field!r}",
                    },
                })
                return

        extra = {"resend_id": resend_id} if resend_id is not None else {
            "addressee": body["addressee"],
            "message": body["message"],
        }

        self._relay(
            "aprs.send_message",
            **extra,
            path=body.get("path", ""),
            confirm_transmit=body.get("confirm_transmit") is True,
            confirm_vhf_port2_radio_ready=(
                body.get("confirm_vhf_port2_radio_ready") is True
            ),
            confirm_hf_port1_disconnected=(
                body.get("confirm_hf_port1_disconnected") is True
            ),
            _socket_timeout=45,
        )

    def _h_aprs_outbox(
        self,
        params: Dict[str, str],
        query: Dict[str, Any],
    ) -> None:
        """Sent APRS messages and their ack status (no TNC access)."""
        self._relay("aprs.outbox")

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
