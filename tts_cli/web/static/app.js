const actionHeaders = { "X-WQI-Action": "confirmed" };
const jobStatus = document.querySelector("#job-status");
const apiUrl = (path) => `${window.location.origin}${path}`;

const currentUrl = new URL(window.location.href);
if (currentUrl.username || currentUrl.password) {
  currentUrl.username = "";
  currentUrl.password = "";
  window.history.replaceState(null, "", currentUrl);
}

function showMessage(message, state = "idle") {
  jobStatus.textContent = message;
  jobStatus.className = `job job-${state}`;
}

async function parseResponse(response) {
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.detail || "The request failed.");
  }
  return payload;
}

document.querySelector("#upload-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#data-file");
  if (!input.files.length) return;
  showMessage("Uploading and validating dialogue data…", "running");
  const body = new FormData();
  body.append("file", input.files[0]);
  try {
    const payload = await parseResponse(await fetch(apiUrl("/api/data"), {
      method: "POST",
      headers: actionHeaders,
      body,
    }));
    showMessage(payload.message, "complete");
    await refreshStatus();
  } catch (error) {
    showMessage(error.message, "failed");
  }
});

document.querySelector("#generate-button").addEventListener("click", async () => {
  showMessage("Starting lookup generation…", "running");
  try {
    const payload = await parseResponse(await fetch(apiUrl("/api/generate-lookups"), {
      method: "POST",
      headers: actionHeaders,
    }));
    showMessage(payload.message, "queued");
    window.setTimeout(refreshStatus, 500);
  } catch (error) {
    showMessage(error.message, "failed");
  }
});

async function refreshStatus() {
  try {
    const status = await parseResponse(await fetch(apiUrl("/api/status")));
    document.querySelector("#data-rows").textContent = status.data.rows.toLocaleString();
    document.querySelector("#data-state").textContent = status.data.available ? "Ready" : "Needs data";
    document.querySelector("#lookup-count").textContent = status.output.lookups;
    document.querySelector("#database-state").textContent = status.database.available ? "Online" : "Unavailable";
    document.querySelector("#database-detail").textContent = status.database.available
      ? `${status.database.tables} imported tables`
      : "Optional service not running";
    showMessage(status.job.message, status.job.state);
    if (status.job.state === "queued" || status.job.state === "running") {
      window.setTimeout(refreshStatus, 1500);
    }
  } catch (error) {
    showMessage(error.message, "failed");
  }
}

window.setTimeout(refreshStatus, 1000);
document.querySelector("#download-link").href = apiUrl("/api/download-lookups");
