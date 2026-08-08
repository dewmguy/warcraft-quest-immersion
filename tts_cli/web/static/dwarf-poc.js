const actionHeaders = { "Content-Type": "application/json", "X-WQI-Action": "confirmed" };
const tabs = [...document.querySelectorAll("[data-profile-tab]")];
const panels = [...document.querySelectorAll("[data-profile-panel]")];
const message = document.querySelector("#poc-message");
const dialog = document.querySelector("#decision-dialog");
const dialogTitle = document.querySelector("#decision-title");
const dialogDescription = document.querySelector("#decision-description");
const dialogNote = document.querySelector("#decision-note");

function selectProfile(profileId) {
  for (const tab of tabs) {
    const selected = tab.dataset.profileTab === profileId;
    tab.setAttribute("aria-selected", String(selected));
  }
  for (const panel of panels) panel.hidden = panel.dataset.profilePanel !== profileId;
  window.history.replaceState(null, "", `#${profileId}`);
}

for (const tab of tabs) {
  tab.addEventListener("click", () => selectProfile(tab.dataset.profileTab));
}

const initialProfile = window.location.hash.slice(1);
if (tabs.some((tab) => tab.dataset.profileTab === initialProfile)) selectProfile(initialProfile);

function showMessage(text, state = "idle") {
  message.textContent = text;
  message.className = `job job-${state}`;
}

async function mutate(url, method, body) {
  showMessage("Saving workflow state…", "running");
  const response = await fetch(url, {
    method,
    headers: actionHeaders,
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail || "The workflow update failed.");
  return payload;
}

for (const form of document.querySelectorAll(".settings-form")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    const enabledDeliveries = [...form.querySelectorAll('[name="enabled_deliveries"]:checked')]
      .map((input) => input.value);
    try {
      await mutate(`/api/poc/dwarves/${form.dataset.profileId}/settings`, "PATCH", {
        source_strategy: data.get("source_strategy"),
        reference_label: data.get("reference_label"),
        stability_mode: data.get("stability_mode"),
        neutral_script: data.get("neutral_script"),
        enabled_deliveries: enabledDeliveries,
        design_notes: data.get("design_notes"),
      });
      window.location.reload();
    } catch (error) {
      showMessage(error.message, "failed");
    }
  });
}

for (const form of document.querySelectorAll(".line-form")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const data = new FormData(form);
    try {
      await mutate(`/api/poc/dwarves/${form.dataset.profileId}/demo-line`, "PATCH", {
        processed_text: data.get("processed_text"),
        delivery_preset: data.get("delivery_preset"),
        route: data.get("route"),
      });
      window.location.reload();
    } catch (error) {
      showMessage(error.message, "failed");
    }
  });
}

function requestDecision(action, stageName) {
  const labels = {
    approve: ["Approve this gate?", "The next gate will become active."],
    request_changes: ["Mark this as needing changes?", "Later approvals will be reset, but their history remains."],
    reopen: ["Reopen this completed gate?", "Later approvals will be reset, but their history remains."],
    skip_unique: ["Mark the unique fork not required?", "The line will continue on the ordinary baseline route."],
  };
  dialogTitle.textContent = labels[action][0];
  dialogDescription.textContent = `${stageName}: ${labels[action][1]}`;
  dialogNote.value = "";
  dialog.showModal();
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      resolve(dialog.returnValue === "confirm" ? dialogNote.value : null);
    }, { once: true });
  });
}

for (const list of document.querySelectorAll("[data-stage-list]")) {
  list.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-action]");
    if (!button) return;
    const panel = button.closest("[data-profile-panel]");
    const stage = button.closest("[data-stage-id]");
    const profileId = panel.dataset.profilePanel;
    const stageId = stage.dataset.stageId;
    const stageName = stage.querySelector("strong").textContent;
    const note = await requestDecision(button.dataset.action, stageName);
    if (note === null) return;
    const workflow = list.dataset.stageList === "profile" ? "profile-stages" : "line-stages";
    try {
      await mutate(`/api/poc/dwarves/${profileId}/${workflow}/${stageId}`, "POST", {
        action: button.dataset.action,
        note,
      });
      window.location.reload();
    } catch (error) {
      showMessage(error.message, "failed");
    }
  });
}
