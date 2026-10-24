const stateEl = document.getElementById("state");
const detailEl = document.getElementById("detail");

function render(status) {
  stateEl.className = status.state;
  if (status.state === "ok") {
    stateEl.textContent = `Ready: @${status.user}`;
    detailEl.textContent = `Saving to ${status.output}`;
  } else if (status.state === "down") {
    stateEl.textContent = "Server not running";
    detailEl.textContent = "Start it with: python download-insta-tab/server.py --output <folder>";
  } else {
    stateEl.textContent = "Not logged in / auth failed";
    detailEl.textContent = status.message;
  }
}

chrome.runtime.sendMessage({ type: "status" })
  .then(render)
  .catch((err) => {
    stateEl.className = "down";
    stateEl.textContent = "Extension error";
    detailEl.textContent = err.message;
  });
