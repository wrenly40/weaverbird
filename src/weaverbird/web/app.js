// Weaverbird app — deliberately tiny.
// One job: when a form that runs a factory action is submitted, mark
// its button as working and stop a second submit. The actions run on
// the server and answer with a result page; there is nothing else
// for the browser to do.
document.addEventListener("submit", function (event) {
  var form = event.target;
  if (!form.classList || !form.classList.contains("action")) return;
  var buttons = form.querySelectorAll("button[type=submit], input[type=submit]");
  buttons.forEach(function (button) {
    button.disabled = true;
    if (button.tagName === "BUTTON") button.textContent = "Working…";
  });
});
