
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
  document.getElementById("logLink").href = authedUrl("/log");
  document.getElementById("telemetryLink").href = authedUrl("/telemetry");

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  async function apiGet(path, options) {
    var res = await fetch(authedUrl(path), options);
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

  // ``flash``: a line shown above the list, e.g. after a delete.
  async function showList(flash) {
    content.innerHTML = '<div class="notice">Loading messages...</div>';
    var top = flash ? '<div class="notice">' + escapeHtml(flash) + "</div>" : "";

    try {
      var messages = await apiGet("/pbbs/messages");

      if (!messages.length) {
        content.innerHTML = top + '<div class="notice">No messages.</div>';
        return;
      }

      var html = top + "<table><thead><tr>" +
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
          '<button type="button" id="deleteButton">delete</button>' +
          '<div id="deleteStatus"></div>' +
          '<div id="detail"><div class="hdr">' + escapeHtml(hdr) + "</div>" +
          escapeHtml(message.body) + "</div>";

        document.getElementById("deleteButton").addEventListener(
          "click", function () { deleteMessage(message.number); }
        );
      }

      document.getElementById("backLink").addEventListener(
        "click", function (e) { e.preventDefault(); showList(); }
      );
    } catch (err) {
      renderError(err);
    }
  }

  async function deleteMessage(number) {
    if (!confirm("Delete PBBS message " + number + "? This can't be undone.")) {
      return;
    }

    var button = document.getElementById("deleteButton");
    var status = document.getElementById("deleteStatus");
    button.disabled = true;
    status.className = "notice";
    status.textContent = "Deleting message " + number + "...";

    try {
      var result = await apiGet(
        "/pbbs/messages/" + encodeURIComponent(number) + "/delete",
        { method: "POST" }
      );

      if (result.deleted) {
        showList("Message " + number + " deleted.");
        return;
      }

      // The PBBS's own words say why (not allowed, ...).
      status.className = "err";
      status.textContent = !result.found
        ? "Message " + number + " is no longer in the mailbox."
        : "The PBBS didn't delete message " + number +
          (result.reply ? ": " + result.reply : ".");
    } catch (err) {
      status.className = "err";
      status.textContent = err.message || String(err);
    }
    button.disabled = false;
  }

  if (!token) {
    content.innerHTML =
      '<div class="notice">No ?token=... in this page\'s URL. If the ' +
      "server was started with authentication, requests below will " +
      "fail with 401 -- reload with ?token=&lt;your key&gt; appended " +
      "to the URL.</div>";
  } else {
    showList();
  }
})();
