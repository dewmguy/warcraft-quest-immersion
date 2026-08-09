const alphaMessage = document.querySelector("#alpha-message");

function showAlphaMessage(text, state = "working") {
  alphaMessage.hidden = false;
  alphaMessage.textContent = text;
  alphaMessage.dataset.state = state;
}

async function parseAlphaResponse(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "The action could not be completed.");
  return payload;
}

function jsonFromForm(form) {
  const data = new FormData(form);
  const payload = {};
  for (const [name, value] of data.entries()) {
    const field = form.elements.namedItem(name);
    const input = field instanceof RadioNodeList ? [...field].find((item) => item.checked) : field;
    if (input?.dataset?.array !== undefined) {
      if (!payload[name]) payload[name] = [];
      payload[name].push(value);
    } else {
      payload[name] = value;
    }
  }
  for (const checkbox of form.querySelectorAll('input[type="checkbox"]:not([data-array])')) {
    payload[checkbox.name] = checkbox.checked;
  }
  return payload;
}

function meteringEstimate(element) {
  const field = element.querySelector?.("[data-metered-text]");
  const rawCharacters = field ? field.value.trim().length : element.dataset.meteredCharacters;
  const characters = Number(rawCharacters);
  if (!Number.isFinite(characters) || characters <= 0) return null;
  return {
    characters,
    dollars: characters * 0.10 / 1000,
    seconds: characters * 60 / 1000,
    kind: element.dataset.metering || "speech",
  };
}

function formatSeconds(seconds) {
  if (seconds < 60) return `${Math.max(seconds, 1).toFixed(seconds < 10 ? 1 : 0)} seconds`;
  return `${(seconds / 60).toFixed(1)} minutes`;
}

function paidConfirmation(element) {
  const estimate = meteringEstimate(element);
  if (!estimate) return element.dataset.confirm || "";
  const action = estimate.kind === "voice-design"
    ? "This creates three Voice Design candidates; ElevenLabs charges the preview text once."
    : "This creates one speech candidate.";
  return `${action}\n\nPreflight estimate:\n- ${estimate.characters.toLocaleString()} metered characters\n- about $${estimate.dollars.toFixed(3)} at the published v2/v3 API list rate\n- about ${formatSeconds(estimate.seconds)} of audio per candidate\n\nYour plan and the provider response determine exact usage. Continue?`;
}

function syncMeteringCard(element) {
  const estimate = meteringEstimate(element);
  if (!estimate) return;
  const characters = element.querySelector("[data-estimate-characters]");
  const dollars = element.querySelector("[data-estimate-dollars]");
  const duration = element.querySelector("[data-estimate-duration]");
  if (characters) characters.textContent = estimate.characters.toLocaleString();
  if (dollars) dollars.textContent = `$${estimate.dollars.toFixed(3)}`;
  if (duration) duration.textContent = formatSeconds(estimate.seconds);
}

async function runAlphaAction({ url, method = "POST", body = null, paid = false, confirmText = "" }) {
  if (paid && !window.confirm(confirmText || "This action can contact ElevenLabs and may consume credits or a voice slot. Continue?")) return null;
  showAlphaMessage(paid ? "Sending the confirmed ElevenLabs request…" : "Saving…");
  const headers = { "X-WQI-Action": "confirmed" };
  if (paid) headers["X-WQI-Paid-Action"] = "confirmed";
  if (body !== null && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
  const response = await fetch(url, {
    method,
    headers,
    body: body === null ? null : body instanceof FormData ? body : JSON.stringify(body),
  });
  return parseAlphaResponse(response);
}

for (const form of document.querySelectorAll("[data-json-form]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = await runAlphaAction({
        url: form.dataset.url,
        method: form.dataset.method || "PATCH",
        body: jsonFromForm(form),
        paid: form.dataset.paid !== undefined,
        confirmText: paidConfirmation(form),
      });
      if (!payload) return;
      showAlphaMessage(payload.message || "Saved.", "complete");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      showAlphaMessage(error.message, "failed");
    }
  });
}

for (const form of document.querySelectorAll("[data-upload-form]")) {
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      const payload = await runAlphaAction({
        url: form.dataset.url,
        method: "POST",
        body: new FormData(form),
      });
      showAlphaMessage(payload.message || "Uploaded.", "complete");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      showAlphaMessage(error.message, "failed");
    }
  });
}

for (const button of document.querySelectorAll("[data-action]")) {
  button.addEventListener("click", async () => {
    try {
      const payload = await runAlphaAction({
        url: button.dataset.url,
        method: button.dataset.method || "POST",
        paid: button.dataset.paid !== undefined,
        confirmText: paidConfirmation(button),
      });
      if (!payload) return;
      showAlphaMessage(payload.message || "Saved.", "complete");
      if (button.dataset.goToVoice !== undefined && payload.voice_id) {
        window.location.assign(`/alpha/voices/${payload.voice_id}`);
      } else {
        window.setTimeout(() => window.location.reload(), 350);
      }
    } catch (error) {
      showAlphaMessage(error.message, "failed");
    }
  });
}

for (const input of document.querySelectorAll('input[type="range"]')) {
  const output = input.closest("label")?.querySelector("[data-range-output]");
  if (!output) continue;
  const sync = () => { output.value = input.value; output.textContent = input.value; };
  input.addEventListener("input", sync);
  sync();
}

const methodController = document.querySelector("[data-method-controller]");
if (methodController) {
  const selector = methodController.querySelector('[name="creation_method"]');
  const savedMethod = methodController.dataset.savedMethod;
  const panels = [...document.querySelectorAll("[data-method-panel]")];
  for (const panel of panels) {
    for (const button of panel.querySelectorAll("button")) {
      button.dataset.serverDisabled = button.disabled ? "true" : "false";
    }
  }
  const syncMethod = () => {
    const selected = selector.value;
    for (const copy of document.querySelectorAll("[data-method-copy] [data-method]")) {
      copy.hidden = copy.dataset.method !== selected;
    }
    for (const panel of panels) {
      const active = panel.dataset.methodPanel === selected;
      panel.hidden = !active;
      for (const field of panel.querySelectorAll("input, select, textarea, button")) {
        if (field.tagName === "BUTTON") {
          field.disabled = field.dataset.serverDisabled === "true" || selected !== savedMethod;
        } else {
          field.disabled = !active;
        }
      }
    }
  };
  selector.addEventListener("change", syncMethod);
  syncMethod();
}

for (const element of document.querySelectorAll("[data-metering]")) {
  const field = element.querySelector("[data-metered-text]");
  if (field) field.addEventListener("input", () => syncMeteringCard(element));
  syncMeteringCard(element);
}

const providerAccount = document.querySelector("[data-provider-account]");
if (providerAccount) {
  const loading = providerAccount.querySelector("[data-provider-loading]");
  const result = providerAccount.querySelector("[data-provider-result]");
  const errorBox = providerAccount.querySelector("[data-provider-error]");
  const refresh = providerAccount.querySelector("[data-provider-refresh]");

  const readAccount = async () => {
    if (!loading || !result) return;
    loading.hidden = false;
    result.hidden = true;
    if (errorBox) errorBox.hidden = true;
    if (refresh) refresh.disabled = true;
    try {
      const response = await fetch(providerAccount.dataset.providerUrl);
      const payload = await parseAlphaResponse(response);
      const account = payload.account || {};
      providerAccount.querySelector("[data-provider-connection]").textContent = "Verified";
      providerAccount.querySelector("[data-provider-tier]").textContent = `${account.tier || "Unknown"} / ${account.status || "unknown"}`;
      providerAccount.querySelector("[data-provider-usage]").textContent = account.character_count == null || account.character_limit == null
        ? "Not reported by this account"
        : `${Number(account.character_count).toLocaleString()} / ${Number(account.character_limit).toLocaleString()} characters`;
      providerAccount.querySelector("[data-provider-remaining]").textContent = account.remaining_characters == null
        ? "Not reported"
        : `${Number(account.remaining_characters).toLocaleString()} characters`;
      providerAccount.querySelector("[data-provider-reset]").textContent = account.next_reset_at
        ? new Date(account.next_reset_at).toLocaleString()
        : "Not reported";
      providerAccount.querySelector("[data-provider-voices]").textContent = account.voice_limit == null
        ? "Not reported"
        : `${Number(account.voice_limit).toLocaleString()} maximum${account.can_use_instant_voice_cloning === false ? " / cloning unavailable" : ""}`;
      const progress = providerAccount.querySelector("[data-provider-progress]");
      progress.value = account.percent_used || 0;
      providerAccount.querySelector("[data-provider-message]").textContent = payload.message;
      result.hidden = false;
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      }
    } finally {
      loading.hidden = true;
      if (refresh) refresh.disabled = false;
    }
  };

  if (refresh) refresh.addEventListener("click", readAccount);
  if (loading) readAccount();
}
