
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
        convState.selected = recipient;
        convState.userSelected = true;
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

  // -- Conversations ---------------------------------------------------
  //
  // Threads per station built from received messages (/aprs/messages,
  // from the history database) and sent ones (/aprs/outbox, which now
  // also survive restarts). Neither touches the TNC. "Read" state is a
  // per-browser convenience kept in localStorage.

  var convList = document.getElementById("convList");
  var convMessages = document.getElementById("convMessages");
  var convThreadTitle = document.getElementById("convThreadTitle");
  var convReplyBtn = document.getElementById("convReplyBtn");
  var convStatus = document.getElementById("convStatus");
  var convUnreadBadge = document.getElementById("convUnreadBadge");
  var convAlertsBtn = document.getElementById("convAlertsBtn");
  var baseTitle = document.title;

  var convState = {
    selected: null,
    // Only mark a thread read once the user has picked it themselves
    // (the auto-selected newest thread on page load doesn't count).
    userSelected: false,
    received: [],
    sent: [],
    outbox: null,
    maxSeenId: null,      // highest received-message id already alerted on
    loadedOnce: false
  };

  function storageGet(key, fallback) {
    try {
      var raw = window.localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch (e) {
      return fallback;
    }
  }

  function storageSet(key, value) {
    try { window.localStorage.setItem(key, JSON.stringify(value)); }
    catch (e) { /* private mode etc. -- read state just won't persist */ }
  }

  // station -> received_at of the newest message read in that thread
  var readUpTo = storageGet("kamxl.readUpTo", {});

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
      refreshConversations();
    }
  }

  function buildThreads() {
    var threads = {};

    function thread(call) {
      if (!threads[call]) {
        threads[call] = { call: call, items: [], last: 0, unread: 0 };
      }
      return threads[call];
    }

    convState.received.forEach(function (msg) {
      var t = thread(msg.source);
      t.items.push({ dir: "in", at: msg.received_at, msg: msg });
      t.last = Math.max(t.last, msg.received_at);
      if (msg.received_at > (readUpTo[msg.source] || 0)) t.unread++;
    });

    convState.sent.forEach(function (msg) {
      var t = thread(msg.addressee);
      t.items.push({ dir: "out", at: msg.sent_at, msg: msg });
      t.last = Math.max(t.last, msg.sent_at);
    });

    return Object.keys(threads).map(function (k) {
      var t = threads[k];
      t.items.sort(function (a, b) { return a.at - b.at; });
      return t;
    }).sort(function (a, b) { return b.last - a.last; });
  }

  function ackBadgeFor(msg) {
    var badge = document.createElement("span");
    if (msg.status === "acked") {
      badge.className = "ackBadge acked";
      badge.textContent = "Acknowledged";
    } else if (msg.status === "rejected") {
      badge.className = "ackBadge rejected";
      badge.textContent = "Rejected";
    } else if (msg.gave_up) {
      badge.className = "ackBadge gaveup";
      badge.textContent = "No ack";
    } else {
      badge.className = "ackBadge pending";
      badge.textContent = "Awaiting ack";
    }
    return badge;
  }

  function renderBubble(item) {
    var msg = item.msg;
    var bubble = document.createElement("div");
    bubble.className = "bubble " + item.dir;

    var text = document.createElement("div");
    text.textContent = item.dir === "in" ? msg.message : msg.text;
    bubble.appendChild(text);

    var meta = document.createElement("div");
    meta.className = "bubbleMeta";
    var when = document.createElement("span");
    when.textContent = new Date(item.at * 1000).toLocaleString();
    when.title = relativeTime(item.at);
    meta.appendChild(when);

    if (item.dir === "out") {
      meta.appendChild(ackBadgeFor(msg));
      var extra = [];
      extra.push("#" + msg.message_id);
      if (msg.attempts > 1) extra.push(msg.attempts + " attempts");
      if (msg.status === "pending" && !msg.gave_up && msg.next_retry_at &&
          convState.outbox && convState.outbox.auto_retry) {
        var secs = Math.max(0, Math.round(
          msg.next_retry_at - Date.now() / 1000));
        extra.push(msg.retries_left > 0
          ? "auto-resend in " + secs + "s"
          : "waiting " + secs + "s for a final ack");
      }
      if (msg.status !== "pending" && msg.acked_at) {
        extra.push(relativeTime(msg.acked_at));
      }
      var info = document.createElement("span");
      info.textContent = extra.join(" · ");
      meta.appendChild(info);

      if (msg.status === "pending") {
        var resend = document.createElement("button");
        resend.type = "button";
        resend.className = "action";
        resend.textContent = "Resend";
        resend.addEventListener("click", function () {
          resendAprsMessage(msg.message_id, resend);
        });
        meta.appendChild(resend);
      }
    }

    bubble.appendChild(meta);
    return bubble;
  }

  function markRead(call, threads) {
    var t = threads.find(function (x) { return x.call === call; });
    if (!t) return;
    var newest = 0;
    t.items.forEach(function (i) {
      if (i.dir === "in") newest = Math.max(newest, i.at);
    });
    if (newest > (readUpTo[call] || 0)) {
      readUpTo[call] = newest;
      storageSet("kamxl.readUpTo", readUpTo);
    }
  }

  function renderConversations() {
    var threads = buildThreads();

    if (convState.selected && convState.userSelected) {
      markRead(convState.selected, threads);
    }
    threads = buildThreads();

    var totalUnread = threads.reduce(function (n, t) { return n + t.unread; }, 0);
    convUnreadBadge.hidden = totalUnread === 0;
    convUnreadBadge.textContent = totalUnread + " new";
    document.title = totalUnread ? "(" + totalUnread + ") " + baseTitle : baseTitle;

    convList.replaceChildren();
    if (!threads.length) {
      convList.textContent = "No messages yet.";
    }
    threads.forEach(function (t) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "convRow" + (t.call === convState.selected ? " selected" : "");
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", t.call === convState.selected);

      var call = document.createElement("span");
      call.className = "convCall";
      call.textContent = t.call;
      row.appendChild(call);

      if (t.unread) {
        var badge = document.createElement("span");
        badge.className = "convUnread";
        badge.textContent = t.unread;
        row.appendChild(badge);
      }

      var last = t.items[t.items.length - 1];
      var preview = document.createElement("span");
      preview.className = "convPreview";
      preview.textContent = (last.dir === "out" ? "You: " : "") +
        (last.dir === "in" ? last.msg.message : last.msg.text);
      row.appendChild(preview);

      row.addEventListener("click", function () {
        convState.selected = t.call;
        convState.userSelected = true;
        renderConversations();
      });
      convList.appendChild(row);
    });

    var current = threads.find(function (t) { return t.call === convState.selected; });
    convMessages.replaceChildren();
    if (!current) {
      convThreadTitle.textContent = threads.length ? "Select a station" : "";
      convReplyBtn.hidden = true;
      return;
    }
    convThreadTitle.textContent = current.call;
    convReplyBtn.hidden = false;
    current.items.forEach(function (item) {
      convMessages.appendChild(renderBubble(item));
    });
    convMessages.scrollTop = convMessages.scrollHeight;
  }

  convReplyBtn.addEventListener("click", function () {
    if (!convState.selected) return;
    document.getElementById("aprsRecipient").value = convState.selected;
    var box = document.getElementById("aprsMessageText");
    box.focus();
    box.scrollIntoView({ block: "center", behavior: "smooth" });
  });

  // -- New-message alerts --------------------------------------------

  function playChime() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = new Ctx();
      [880, 1320].forEach(function (freq, i) {
        var osc = ctx.createOscillator();
        var gain = ctx.createGain();
        osc.frequency.value = freq;
        osc.type = "sine";
        var t0 = ctx.currentTime + i * 0.18;
        gain.gain.setValueAtTime(0.0001, t0);
        gain.gain.exponentialRampToValueAtTime(0.25, t0 + 0.02);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.16);
        osc.connect(gain).connect(ctx.destination);
        osc.start(t0);
        osc.stop(t0 + 0.18);
      });
      setTimeout(function () { ctx.close(); }, 800);
    } catch (e) { /* audio blocked until the page has been clicked */ }
  }

  function alertsAllowed() {
    return storageGet("kamxl.alerts", false) === true;
  }

  function updateAlertsButton() {
    var on = alertsAllowed();
    convAlertsBtn.textContent = on ? "Alerts on" : "Enable alerts";
    convAlertsBtn.title = on
      ? "Chime and desktop notification for new messages. Click to turn off."
      : "Chime and desktop notification when a message arrives";
  }

  convAlertsBtn.addEventListener("click", async function () {
    if (alertsAllowed()) {
      storageSet("kamxl.alerts", false);
      updateAlertsButton();
      return;
    }
    storageSet("kamxl.alerts", true);
    if ("Notification" in window && Notification.permission === "default") {
      try { await Notification.requestPermission(); } catch (e) {}
    }
    playChime();
    updateAlertsButton();
  });

  function announceNew(messages) {
    if (!messages.length || !alertsAllowed()) return;
    playChime();
    if ("Notification" in window && Notification.permission === "granted") {
      var first = messages[messages.length - 1];
      try {
        var note = new Notification("APRS message from " + first.source, {
          body: first.message +
            (messages.length > 1 ? " (+" + (messages.length - 1) + " more)" : ""),
          tag: "kamxl-aprs"
        });
        note.onclick = function () {
          window.focus();
          convState.selected = first.source;
          convState.userSelected = true;
          renderConversations();
        };
      } catch (e) {}
    }
  }

  // -- Loading -------------------------------------------------------

  var convTimer = null;

  async function refreshConversations() {
    var problems = [];

    try {
      var inbox = await apiGet("/aprs/messages?limit=200");
      if (inbox.enabled) {
        var received = (inbox.messages || []).filter(function (m) {
          return m.kind !== "ack" && m.kind !== "rej";
        });

        var maxId = received.reduce(function (n, m) { return Math.max(n, m.id); }, 0);
        if (convState.maxSeenId !== null) {
          var fresh = received.filter(function (m) {
            return m.id > convState.maxSeenId;
          }).sort(function (a, b) { return a.id - b.id; });
          announceNew(fresh);
        }
        convState.maxSeenId = Math.max(convState.maxSeenId || 0, maxId);
        convState.received = received;
      } else {
        convState.received = [];
        problems.push("received messages aren't stored (no --history-db)");
      }
    } catch (err) {
      problems.push("couldn't load received messages: " + String(err));
    }

    var pending = 0;
    try {
      var outbox = await apiGet("/aprs/outbox");
      convState.outbox = outbox;
      convState.sent = outbox.messages || [];
      pending = convState.sent.filter(function (m) {
        return m.status === "pending" && !m.gave_up;
      }).length;
    } catch (err) {
      problems.push("couldn't load sent messages: " + String(err));
    }

    if (!convState.selected) {
      var threads = buildThreads();
      if (threads.length) convState.selected = threads[0].call;
    }

    renderConversations();

    var parts = [];
    parts.push(convState.received.length + " received, " +
      convState.sent.length + " sent" +
      (pending ? " (" + pending + " awaiting ack)" : ""));
    if (convState.outbox) {
      parts.push("path " + (convState.outbox.path || "direct"));
    }
    convStatus.textContent = parts.join(" · ") +
      (problems.length ? " — " + problems.join("; ") : "");
    convStatus.className = problems.length ? "sub err" : "sub";

    // Poll faster while something is awaiting an ack.
    if (convTimer !== null) clearTimeout(convTimer);
    convTimer = setTimeout(refreshConversations, pending ? 5000 : 10000);
  }

  // Older name, still used by the send form.
  var refreshAprsOutbox = refreshConversations;

  var aprsClearBtn = document.getElementById("aprsClearInbox");

  aprsClearBtn.addEventListener("click", async function () {
    if (!window.confirm(
        "Delete all received and sent APRS messages? " +
        "Any automatic resends still scheduled are cancelled.")) {
      return;
    }

    aprsClearBtn.disabled = true;
    convStatus.textContent = "Clearing messages...";

    try {
      for (var path of ["/aprs/messages/clear", "/aprs/outbox/clear"]) {
        var res = await fetch(authedUrl(path), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}"
        });
        var payload = await res.json();
        if (!payload.ok) {
          throw new Error(
            (payload.error && payload.error.message) || "Request failed"
          );
        }
      }
      readUpTo = {};
      storageSet("kamxl.readUpTo", readUpTo);
      convState.selected = null;
    } catch (err) {
      convStatus.textContent = "Unable to clear messages: " + String(err);
    } finally {
      aprsClearBtn.disabled = false;
      refreshConversations();
    }
  });

  // -- Automatic resend ---------------------------------------------

  var aprsAutoRetry = document.getElementById("aprsAutoRetry");
  var aprsAutoRetryStatus = document.getElementById("aprsAutoRetryStatus");

  function showRetryStatus(st) {
    aprsAutoRetry.checked = !!st.enabled;
    var plan = "up to " + st.retry_count + " resends, first after " +
      Math.round(st.interval_seconds) + "s, then doubling";
    if (st.enabled && st.paused) {
      aprsAutoRetryStatus.className = "sub paused";
      aprsAutoRetryStatus.textContent = "Auto-resend paused: a terminal " +
        "or connected session may be open; resumes after the next APRS send.";
    } else if (st.enabled) {
      aprsAutoRetryStatus.className = "sub on";
      aprsAutoRetryStatus.textContent = "Auto-resend on (" + plan + ")" +
        (st.retries_sent ? ", " + st.retries_sent + " resent" : "") +
        ". Applies to messages sent from now on.";
    } else {
      aprsAutoRetryStatus.className = "sub off";
      aprsAutoRetryStatus.textContent = "Auto-resend off" +
        (st.disabled_reason ? " (" + st.disabled_reason + ")" : "") +
        ". Use Resend on a message to retry it by hand.";
    }
  }

  async function refreshAutoRetry() {
    try {
      showRetryStatus(await apiGet("/aprs/retry"));
    } catch (err) {
      aprsAutoRetryStatus.className = "sub";
      aprsAutoRetryStatus.textContent =
        "Unable to read auto-resend status: " + String(err);
    }
  }

  aprsAutoRetry.addEventListener("change", async function () {
    var wanted = aprsAutoRetry.checked;
    aprsAutoRetry.disabled = true;
    try {
      var res = await fetch(authedUrl("/aprs/retry"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: wanted })
      });
      var payload = await res.json();
      if (!payload.ok) {
        aprsAutoRetryStatus.textContent = "Could not change auto-resend: " +
          ((payload.error && payload.error.message) || "Unknown error");
        aprsAutoRetry.checked = !wanted;
        return;
      }
      showRetryStatus(payload.result);
    } catch (err) {
      aprsAutoRetryStatus.textContent =
        "Auto-resend request failed: " + String(err);
      aprsAutoRetry.checked = !wanted;
    } finally {
      aprsAutoRetry.disabled = false;
    }
  });

  updateAlertsButton();

  // -- Position beacon -----------------------------------------------

  var beaconToggle = document.getElementById("beaconToggle");
  var beaconStatus = document.getElementById("beaconStatus");
  var beaconPosition = document.getElementById("beaconPosition");
  var beaconNowBtn = document.getElementById("beaconNowBtn");

  function showBeaconStatus(st) {
    if (!st.configured) {
      beaconPosition.textContent =
        "No position set. Add KAMXL_LATITUDE and KAMXL_LONGITUDE to " +
        ".env and restart the daemon.";
      beaconToggle.checked = false;
      beaconToggle.disabled = true;
      beaconNowBtn.disabled = true;
      beaconStatus.textContent = "";
      return;
    }

    beaconToggle.disabled = false;
    beaconNowBtn.disabled = false;
    beaconPosition.textContent =
      st.position.latitude.toFixed(4) + ", " +
      st.position.longitude.toFixed(4) +
      (st.position.comment ? " — " + st.position.comment : "") +
      " (via " + (st.path || "direct") + ", every " +
      st.interval_minutes + " min)";
    beaconToggle.checked = !!st.enabled;

    var parts = [];
    if (st.enabled && st.paused) {
      beaconStatus.className = "sub paused";
      parts.push("Beacon paused: a terminal or connected session may be " +
        "open; resumes after the next APRS send");
    } else if (st.enabled) {
      beaconStatus.className = "sub on";
      parts.push("Beacon on");
      if (st.next_beacon_at) {
        var mins = Math.max(0, Math.round(
          (st.next_beacon_at - Date.now() / 1000) / 60));
        parts.push("next in " + mins + " min");
      }
    } else {
      beaconStatus.className = "sub off";
      parts.push("Beacon off" +
        (st.disabled_reason ? " (" + st.disabled_reason + ")" : ""));
    }
    if (st.last_beacon_at) {
      parts.push("last sent " + relativeTime(st.last_beacon_at));
    }
    beaconStatus.textContent = parts.join(", ") + ".";
  }

  async function refreshBeacon() {
    try {
      showBeaconStatus(await apiGet("/aprs/beacon"));
    } catch (err) {
      beaconStatus.textContent = "Unable to read beacon status: " + String(err);
    }
  }

  beaconToggle.addEventListener("change", async function () {
    var wanted = beaconToggle.checked;
    beaconToggle.disabled = true;
    beaconStatus.className = "sub";
    beaconStatus.textContent = wanted ? "Turning beacon on..." :
      "Turning beacon off...";

    try {
      var res = await fetch(authedUrl("/aprs/beacon"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: wanted })
      });
      var payload = await res.json();
      if (!payload.ok) {
        beaconStatus.textContent = "Could not change beacon: " +
          ((payload.error && payload.error.message) || "Unknown error");
        beaconToggle.checked = !wanted;
        return;
      }
      showBeaconStatus(payload.result);
    } catch (err) {
      beaconStatus.textContent = "Beacon request failed: " + String(err);
      beaconToggle.checked = !wanted;
    } finally {
      beaconToggle.disabled = false;
    }
  });

  beaconNowBtn.addEventListener("click", async function () {
    beaconNowBtn.disabled = true;
    beaconStatus.className = "sub";
    beaconStatus.textContent = "Sending beacon...";

    try {
      var res = await fetch(authedUrl("/aprs/beacon/now"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}"
      });
      var payload = await res.json();
      if (!payload.ok) {
        beaconStatus.textContent = "Beacon failed: " +
          ((payload.error && payload.error.message) || "Unknown error");
        return;
      }
      showBeaconStatus(payload.result.status);
    } catch (err) {
      beaconStatus.textContent = "Beacon request failed: " + String(err);
    } finally {
      beaconNowBtn.disabled = false;
    }
  });

  // -- Automatic acknowledgments ------------------------------------

  var aprsAutoAck = document.getElementById("aprsAutoAck");
  var aprsAutoAckStatus = document.getElementById("aprsAutoAckStatus");

  function showAutoAckStatus(st) {
    aprsAutoAck.checked = !!st.enabled;

    if (st.enabled && st.paused) {
      aprsAutoAckStatus.className = "sub paused";
      aprsAutoAckStatus.textContent = "Auto-ACK paused: " + st.paused_reason;
    } else if (st.enabled) {
      aprsAutoAckStatus.className = "sub on";
      aprsAutoAckStatus.textContent = "Auto-ACK on" +
        (st.acks_sent ? " — " + st.acks_sent + " sent, last to " +
          st.last_ack.to + " " + relativeTime(st.last_ack.at) : "") + ".";
    } else {
      aprsAutoAckStatus.className = "sub off";
      aprsAutoAckStatus.textContent = "Auto-ACK off" +
        (st.disabled_reason ? " (" + st.disabled_reason + ")" : "") +
        ". Senders will keep retrying until they give up.";
    }
  }

  async function refreshAutoAck() {
    try {
      showAutoAckStatus(await apiGet("/aprs/ack"));
    } catch (err) {
      aprsAutoAckStatus.className = "sub";
      aprsAutoAckStatus.textContent =
        "Unable to read auto-ACK status: " + String(err);
    }
  }

  aprsAutoAck.addEventListener("change", async function () {
    var wanted = aprsAutoAck.checked;
    aprsAutoAck.disabled = true;
    aprsAutoAckStatus.className = "sub";
    aprsAutoAckStatus.textContent = wanted
      ? "Checking the TNC and turning auto-ACK on..."
      : "Turning auto-ACK off...";

    try {
      var res = await fetch(authedUrl("/aprs/ack"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: wanted })
      });
      var payload = await res.json();

      if (!payload.ok) {
        aprsAutoAckStatus.textContent = "Could not change auto-ACK: " +
          ((payload.error && payload.error.message) || "Unknown error");
        aprsAutoAck.checked = !wanted;
        return;
      }
      await refreshAutoAck();
    } catch (err) {
      aprsAutoAckStatus.textContent =
        "Auto-ACK request failed: " + String(err);
      aprsAutoAck.checked = !wanted;
    } finally {
      aprsAutoAck.disabled = false;
    }
  });

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

      historySummary.textContent += history.retention_days
        ? " · packet history kept " + history.retention_days + " days"
        : " · packet history kept indefinitely";

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

  function showReceptionAlert(wd) {
    var box = document.getElementById("receptionAlert");
    var alerting = wd && (wd.state === "only_own" || wd.state === "silent");

    if (!alerting) {
      box.hidden = true;
      return;
    }

    box.hidden = false;
    box.className = wd.state;
    document.getElementById("receptionAlertTitle").textContent =
      wd.state === "silent"
        ? "Receiver hearing nothing"
        : "Receiver hearing only your own stations";
    var last = wd.last_other_packet_at
      ? " Last other station: " + wd.last_other_source + ", " +
        relativeTime(wd.last_other_packet_at) + "."
      : "";
    document.getElementById("receptionAlertText").textContent =
      wd.message + last;
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
    var wd = diag.watchdog || {};

    if (wd.state === "only_own") {
      element.className = "quiet";
      element.textContent = "Reception: Only own stations heard (" +
        Math.round(wd.minutes_since_other || 0) + " min)";
    } else if (wd.state === "silent") {
      element.className = "disconnected";
      element.textContent = "Reception: Nothing heard";
    } else if (packets > 0) {
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
      showReceptionAlert(
        status.connected && diag ? diag.watchdog : null
      );

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
      showReceptionAlert(null);
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
  refreshConversations();
  refreshAutoAck();
  refreshAutoRetry();
  refreshBeacon();

  setInterval(refreshStatus, 15000);
  setInterval(refreshBeacon, 15000);
  setInterval(refreshAutoRetry, 15000);
  setInterval(refreshAutoAck, 15000);
  setInterval(refreshStations, 15000);
  setInterval(refreshReceptionStats, 15000);
  setInterval(refreshPersistentHistory, 60000);
})();
