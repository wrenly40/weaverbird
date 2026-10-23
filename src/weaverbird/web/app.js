// Weaverbird app — deliberately small.
//
// Two jobs: (1) when a form that runs a factory action is
// submitted, mark its button as working and stop a second submit;
// (2) follow live job cards — every long action now runs as a
// background job, so a card polls /api/jobs/<id> about every
// 1.5s and updates its status, elapsed time, progress bar and
// event log in place. When a job lands, the page reloads once
// (keeping the finished card via ?job=) so the rest of the page
// shows the new state; the standalone job page just settles.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (form.hasAttribute && form.hasAttribute("data-cancel-form")) {
    // Cancel is an API call, not a navigation: ask the server,
    // then let the card's polling show the outcome.
    event.preventDefault();
    var action = form.getAttribute("action") || "";
    var id = action.split("/")[2];
    fetch("/api/jobs/" + encodeURIComponent(id) + "/cancel",
          { method: "POST" }).catch(function () {});
    form.querySelectorAll("button").forEach(function (b) {
      b.disabled = true;
      b.textContent = "Cancelling…";
    });
    return;
  }
  if (!form.classList || !form.classList.contains("action")) return;
  var buttons = form.querySelectorAll("button[type=submit], input[type=submit]");
  buttons.forEach(function (button) {
    button.disabled = true;
    if (button.tagName === "BUTTON") button.textContent = "Working…";
  });
});

var TERMINAL = ["done", "failed", "cancelled", "interrupted"];

function updateJobCard(card, job) {
  card.setAttribute("data-job-status", job.status);
  var status = card.querySelector('[data-role="status"]');
  if (status) status.textContent = job.status;
  var elapsed = card.querySelector('[data-role="elapsed"]');
  if (elapsed && typeof job.elapsed_s === "number") {
    elapsed.textContent = Math.round(job.elapsed_s) + "s";
  }
  var fill = card.querySelector('[data-role="fill"]');
  if (fill) {
    var progress = job.progress || {};
    if (job.status === "done") {
      fill.style.width = "100%";
      fill.classList.remove("indeterminate");
    } else if (progress.total) {
      fill.style.width =
        Math.round(100 * (progress.done || 0) / progress.total) + "%";
      fill.classList.remove("indeterminate");
    } else if (TERMINAL.indexOf(job.status) >= 0) {
      fill.style.width = "100%";
      fill.classList.remove("indeterminate");
    }
  }
  var log = card.querySelector('[data-role="log"]');
  if (log && job.events) {
    log.innerHTML = "";
    job.events.slice(-30).forEach(function (event) {
      var li = document.createElement("li");
      var at = document.createElement("span");
      at.className = "hint";
      at.textContent = event.at || "";
      li.appendChild(at);
      li.appendChild(document.createTextNode(" " + (event.text || "")));
      log.appendChild(li);
    });
    log.scrollTop = log.scrollHeight;
  }
  var outcome = card.querySelector('[data-role="outcome"]');
  var text = job.error || job.result || "";
  if (text) {
    if (!outcome) {
      outcome = document.createElement("p");
      outcome.setAttribute("data-role", "outcome");
      outcome.style.marginTop = "10px";
      card.appendChild(outcome);
    }
    outcome.textContent = text;
    if (job.error) outcome.style.color = "#D24545";
  }
  if (TERMINAL.indexOf(job.status) >= 0) {
    var cancel = card.querySelector("[data-cancel-form]");
    if (cancel) cancel.remove();
  }
}

function followJobCard(card) {
  var id = card.getAttribute("data-job-id");
  if (!id) return;
  var initial = card.getAttribute("data-job-status");
  if (TERMINAL.indexOf(initial) >= 0) return;  // already landed
  var reloadOnDone = card.hasAttribute("data-reload-on-done");
  function tick() {
    fetch("/api/jobs/" + encodeURIComponent(id))
      .then(function (response) { return response.json(); })
      .then(function (job) {
        updateJobCard(card, job);
        if (TERMINAL.indexOf(job.status) >= 0) {
          if (reloadOnDone) {
            var url = new URL(window.location.href);
            url.searchParams.set("job", id);
            window.location.replace(url.toString());
          }
          return;
        }
        setTimeout(tick, 1500);
      })
      .catch(function () { setTimeout(tick, 3000); });
  }
  setTimeout(tick, 600);
}

document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-job-id]").forEach(followJobCard);
});
