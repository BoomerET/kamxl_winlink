
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
        "</div><div class=\"hdr\">" + escapeHtml(hdr) + "</div>" +
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
      '<div class="notice">No ?token=... in this page\'s URL. If the ' +
      "server was started with authentication, requests below will " +
      "fail with 401 -- reload with ?token=&lt;your key&gt; appended " +
      "to the URL.</div>";
  }
})();
