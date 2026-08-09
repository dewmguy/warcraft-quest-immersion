const alphaMessage = document.querySelector("#alpha-message");
const alphaMessageTitle = alphaMessage.querySelector("[data-alpha-message-title]");
const alphaMessageDetail = alphaMessage.querySelector("[data-alpha-message-detail]");
const alphaMessageElapsed = alphaMessage.querySelector("[data-alpha-message-elapsed]");
const alphaMessageProgress = alphaMessage.querySelector("[data-alpha-message-progress]");
const alphaMessageClose = alphaMessage.querySelector("[data-alpha-message-close]");
const providerUsageIndicator = document.querySelector("[data-provider-usage-indicator]");
let alphaProviderTimer = null;
const alphaProviderRequests = new Map();
let alphaProviderRequestSequence = 0;
let providerStatusRequest = null;
let providerUsageRefreshTimer = null;

function stopAlphaProviderTimer() {
  if (alphaProviderTimer !== null) window.clearInterval(alphaProviderTimer);
  alphaProviderTimer = null;
}

function formatElapsedTime(seconds) {
  const wholeSeconds = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(wholeSeconds / 60);
  return `${minutes}:${String(wholeSeconds % 60).padStart(2, "0")}`;
}

function renderAlphaMessage({ title, detail = "", state = "working", elapsed = "", provider = false }) {
  alphaMessage.hidden = false;
  alphaMessage.dataset.state = state;
  if (provider) alphaMessage.dataset.provider = "true";
  else delete alphaMessage.dataset.provider;
  alphaMessageTitle.textContent = title;
  alphaMessageDetail.textContent = detail;
  alphaMessageDetail.hidden = !detail;
  alphaMessageElapsed.textContent = elapsed;
  alphaMessageElapsed.hidden = !elapsed;
  alphaMessageProgress.hidden = !provider || state !== "working";
  alphaMessageClose.hidden = state !== "failed";
}

function providerProgressDetail(request, elapsedSeconds) {
  if (request.readOnly) return "Reading the configured account and current usage. No audio is being generated.";
  const estimate = request.estimate
    ? ` Estimated finished audio: ${formatSeconds(request.estimate.seconds)}${request.estimate.kind === "voice-design" ? " per candidate" : ""}.`
    : "";
  if (elapsedSeconds < 5) return `Request sent. Waiting for ElevenLabs to begin processing.${estimate}`;
  if (elapsedSeconds < 30) return `ElevenLabs is processing the request. Keep this page open.${estimate}`;
  if (elapsedSeconds < 90) return "Still processing. Voice Design and longer speech requests commonly need more time; the server allows up to three minutes.";
  return "This is taking longer than usual, but the request remains active. The server will report a result or time out at three minutes.";
}

function renderElevenLabsProgress() {
  const requests = [...alphaProviderRequests.values()];
  if (!requests.length) return;
  const startedAt = Math.min(...requests.map((request) => request.startedAt));
  const elapsedSeconds = (Date.now() - startedAt) / 1000;
  const elapsed = `Elapsed ${formatElapsedTime(elapsedSeconds)}`;
  const operation = requests.length === 1
    ? requests[0].operation
    : `${requests.length} ElevenLabs requests in progress`;
  const detail = requests.length === 1
    ? providerProgressDetail(requests[0], elapsedSeconds)
    : `Running independently: ${requests.map((request) => request.operation).join("; ")}. Completed audio is stored even if another request fails.`;
  alphaMessageProgress.setAttribute("aria-valuetext", `${operation}; ${elapsed}`);
  renderAlphaMessage({
    title: operation,
    detail,
    state: "working",
    elapsed,
    provider: true,
  });
}

function startElevenLabsRequest(operation, estimate = null, readOnly = false) {
  const requestId = `provider-request-${++alphaProviderRequestSequence}`;
  alphaProviderRequests.set(requestId, {
    operation: operation || "Processing an ElevenLabs request",
    estimate,
    readOnly,
    startedAt: Date.now(),
  });
  renderElevenLabsProgress();
  if (alphaProviderTimer === null) {
    alphaProviderTimer = window.setInterval(renderElevenLabsProgress, 1000);
  }
  return requestId;
}

function finishElevenLabsRequest(requestId) {
  alphaProviderRequests.delete(requestId);
  if (alphaProviderRequests.size) {
    renderElevenLabsProgress();
    return;
  }
  stopAlphaProviderTimer();
}

function showAlphaMessage(text, state = "working") {
  if (alphaProviderRequests.size && state !== "working") {
    renderElevenLabsProgress();
    return;
  }
  renderAlphaMessage({ title: text, state });
}

alphaMessageClose.addEventListener("click", () => {
  alphaMessage.hidden = true;
});

async function parseAlphaResponse(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || "The action could not be completed.");
  return payload;
}

function providerStatusUrl() {
  return providerUsageIndicator?.dataset.providerUrl
    || document.querySelector("[data-provider-account]")?.dataset.providerUrl
    || null;
}

async function loadProviderStatus() {
  const url = providerStatusUrl();
  if (!url) return null;
  if (!providerStatusRequest) {
    providerStatusRequest = fetch(url).then(parseAlphaResponse);
  }
  const request = providerStatusRequest;
  try {
    return await request;
  } finally {
    if (providerStatusRequest === request) providerStatusRequest = null;
  }
}

function renderProviderUsageIndicator(account = {}) {
  if (!providerUsageIndicator) return;
  const output = providerUsageIndicator.querySelector("[data-provider-credit-usage]");
  const used = Number(account.credits_used);
  const limit = Number(account.credits_limit);
  const hasUsage = account.credits_used != null && account.credits_limit != null
    && Number.isFinite(used) && Number.isFinite(limit);
  const percentUsed = Number(account.percent_used);
  const reset = account.next_reset_at ? new Date(account.next_reset_at) : null;
  const resetLabel = reset && !Number.isNaN(reset.getTime()) ? reset.toLocaleString() : null;
  if (output) {
    output.textContent = hasUsage
      ? `${used.toLocaleString()} / ${limit.toLocaleString()} credits`
      : "Credits unavailable";
  }
  const details = hasUsage
    ? [`${used.toLocaleString()} of ${limit.toLocaleString()} ElevenLabs subscription credits used`]
    : ["ElevenLabs subscription credit usage is unavailable"];
  if (account.credits_remaining != null && Number.isFinite(Number(account.credits_remaining))) {
    details.push(`${Number(account.credits_remaining).toLocaleString()} credits remaining`);
  }
  if (resetLabel) details.push(`resets ${resetLabel}`);
  const label = details.join("; ");
  providerUsageIndicator.title = label;
  providerUsageIndicator.setAttribute("aria-label", label);
  providerUsageIndicator.classList.toggle("is-warning", Number.isFinite(percentUsed) && percentUsed >= 75 && percentUsed < 90);
  providerUsageIndicator.classList.toggle("is-critical", Number.isFinite(percentUsed) && percentUsed >= 90);
}

async function refreshProviderUsageIndicator() {
  if (!providerUsageIndicator) return;
  try {
    const payload = await loadProviderStatus();
    renderProviderUsageIndicator(payload?.account || {});
  } catch (error) {
    renderProviderUsageIndicator({});
    providerUsageIndicator.title = error.message;
  }
}

function queueProviderUsageRefresh() {
  if (!providerUsageIndicator) return;
  if (providerUsageRefreshTimer !== null) window.clearTimeout(providerUsageRefreshTimer);
  providerUsageRefreshTimer = window.setTimeout(() => {
    providerUsageRefreshTimer = null;
    refreshProviderUsageIndicator();
  }, 150);
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
    credits: characters,
    seconds: characters * 60 / 1000,
    kind: element.dataset.metering || "speech",
  };
}

function formatSeconds(seconds) {
  if (seconds < 60) return `${Math.max(seconds, 1).toFixed(seconds < 10 ? 1 : 0)} seconds`;
  return `${(seconds / 60).toFixed(1)} minutes`;
}

function syncMeteringCard(element) {
  const estimate = meteringEstimate(element);
  if (!estimate) return;
  const characters = element.querySelector("[data-estimate-characters]");
  const credits = element.querySelector("[data-estimate-credits]");
  const duration = element.querySelector("[data-estimate-duration]");
  if (characters) characters.textContent = estimate.characters.toLocaleString();
  if (credits) credits.textContent = `~${estimate.credits.toLocaleString()}`;
  if (duration) duration.textContent = formatSeconds(estimate.seconds);
}

async function runAlphaAction({ url, method = "POST", body = null, paid = false, confirmRequired = false, confirmText = "", providerOperation = "", providerEstimate = null }) {
  if (confirmRequired && !window.confirm(confirmText || "Confirm this action?")) return null;
  const providerRequestId = paid ? startElevenLabsRequest(providerOperation, providerEstimate) : null;
  if (!paid) showAlphaMessage("Saving…");
  try {
    const headers = { "X-WQI-Action": "confirmed" };
    if (paid) headers["X-WQI-Paid-Action"] = "confirmed";
    if (body !== null && !(body instanceof FormData)) headers["Content-Type"] = "application/json";
    const response = await fetch(url, {
      method,
      headers,
      body: body === null ? null : body instanceof FormData ? body : JSON.stringify(body),
    });
    return await parseAlphaResponse(response);
  } finally {
    if (paid) queueProviderUsageRefresh();
    if (providerRequestId) finishElevenLabsRequest(providerRequestId);
  }
}

const dirtyFormSnapshots = new WeakMap();

function syncDirtyForm(form) {
  const submit = form.querySelector("[data-dirty-submit]");
  if (!submit) return;
  submit.hidden = JSON.stringify(jsonFromForm(form)) === dirtyFormSnapshots.get(form);
}

for (const form of document.querySelectorAll("[data-dirty-form]")) {
  dirtyFormSnapshots.set(form, JSON.stringify(jsonFromForm(form)));
  for (const field of form.querySelectorAll("input, select, textarea")) {
    field.addEventListener("input", () => syncDirtyForm(form));
    field.addEventListener("change", () => syncDirtyForm(form));
  }
  syncDirtyForm(form);
}

for (const form of document.querySelectorAll("[data-json-form]")) {
  if (form.dataset.deliveryGeneration !== undefined) continue;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const conditionalSubmit = form.querySelector("[data-dirty-submit]");
    if (conditionalSubmit?.hidden) return;
    const requestBody = jsonFromForm(form);
    if (conditionalSubmit) conditionalSubmit.disabled = true;
    try {
      const payload = await runAlphaAction({
        url: form.dataset.url,
        method: form.dataset.method || "PATCH",
        body: requestBody,
        paid: form.dataset.paid !== undefined,
        confirmRequired: form.dataset.confirmRequired !== undefined,
        confirmText: form.dataset.confirm || "",
        providerOperation: form.dataset.providerOperation,
        providerEstimate: meteringEstimate(form),
      });
      if (!payload) return;
      showAlphaMessage(payload.message || "Saved.", "complete");
      if (form.dataset.dirtyForm !== undefined) {
        dirtyFormSnapshots.set(form, JSON.stringify(requestBody));
        syncDirtyForm(form);
        return;
      }
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      showAlphaMessage(error.message, "failed");
    } finally {
      if (conditionalSubmit) conditionalSubmit.disabled = false;
    }
  });
}

const deliveryGenerationForms = [...document.querySelectorAll("[data-delivery-generation]")];
const deliveryBatchButton = document.querySelector("[data-delivery-batch]");
const activeDeliveryGenerations = new Map();
let deliveryGenerationOutcomes = [];
let deliveryReloadTimer = null;

function setDeliveryGenerationState(form, running) {
  const button = form.querySelector('button[type="submit"]');
  const card = form.closest(".delivery-preset-card");
  form.ariaBusy = String(running);
  card?.classList.toggle("is-generating", running);
  if (!button) return;
  if (!button.dataset.idleLabel) button.dataset.idleLabel = button.textContent.trim();
  if (!button.dataset.serverDisabled) button.dataset.serverDisabled = button.disabled ? "true" : "false";
  button.disabled = running || button.dataset.serverDisabled === "true";
  button.textContent = running ? `Generating ${form.dataset.deliveryName}…` : button.dataset.idleLabel;
}

function availableDeliveryGenerationForms() {
  return deliveryGenerationForms.filter((form) => {
    const button = form.querySelector('button[type="submit"]');
    return button && button.dataset.serverDisabled !== "true" && !activeDeliveryGenerations.has(form);
  });
}

function syncDeliveryBatchButton() {
  if (!deliveryBatchButton) return;
  if (!deliveryBatchButton.dataset.serverDisabled) {
    deliveryBatchButton.dataset.serverDisabled = deliveryBatchButton.disabled ? "true" : "false";
  }
  const available = availableDeliveryGenerationForms();
  const serverDisabled = deliveryBatchButton.dataset.serverDisabled === "true";
  deliveryBatchButton.disabled = serverDisabled || !available.length;
  if (serverDisabled || !activeDeliveryGenerations.size) {
    deliveryBatchButton.textContent = "Generate all samples";
  } else if (available.length) {
    deliveryBatchButton.textContent = `Generate remaining ${available.length}`;
  } else {
    deliveryBatchButton.textContent = `${activeDeliveryGenerations.size} generations running…`;
  }
}

function finishDeliveryGenerationRun() {
  const successes = deliveryGenerationOutcomes.filter((outcome) => outcome.ok);
  const failures = deliveryGenerationOutcomes.filter((outcome) => !outcome.ok);
  const generated = `${successes.length} sample${successes.length === 1 ? "" : "s"} generated`;
  if (failures.length) {
    const failureDetail = failures.map((outcome) => `${outcome.delivery}: ${outcome.message}`).join("; ");
    showAlphaMessage(`${generated}; ${failures.length} failed. ${failureDetail}`, "failed");
  } else {
    showAlphaMessage(`${generated} successfully.`, "complete");
  }
  if (successes.length) {
    deliveryReloadTimer = window.setTimeout(() => window.location.reload(), failures.length ? 1400 : 600);
  }
}

function launchDeliveryGeneration(form) {
  if (activeDeliveryGenerations.has(form)) return activeDeliveryGenerations.get(form);
  if (!activeDeliveryGenerations.size) {
    if (deliveryReloadTimer !== null) window.clearTimeout(deliveryReloadTimer);
    deliveryReloadTimer = null;
    deliveryGenerationOutcomes = [];
  }
  setDeliveryGenerationState(form, true);
  const job = (async () => {
    try {
      const payload = await runAlphaAction({
        url: form.dataset.url,
        method: form.dataset.method || "POST",
        body: jsonFromForm(form),
        paid: true,
        providerOperation: form.dataset.providerOperation,
        providerEstimate: meteringEstimate(form),
      });
      deliveryGenerationOutcomes.push({
        ok: true,
        delivery: form.dataset.deliveryName,
        message: payload.message || "Generated",
      });
      return payload;
    } catch (error) {
      deliveryGenerationOutcomes.push({
        ok: false,
        delivery: form.dataset.deliveryName,
        message: error.message,
      });
      return null;
    } finally {
      activeDeliveryGenerations.delete(form);
      setDeliveryGenerationState(form, false);
      syncDeliveryBatchButton();
      if (!activeDeliveryGenerations.size) finishDeliveryGenerationRun();
    }
  })();
  activeDeliveryGenerations.set(form, job);
  syncDeliveryBatchButton();
  return job;
}

for (const form of deliveryGenerationForms) {
  setDeliveryGenerationState(form, false);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (activeDeliveryGenerations.has(form)) return;
    launchDeliveryGeneration(form);
  });
}

if (deliveryBatchButton) {
  deliveryBatchButton.addEventListener("click", () => {
    const forms = availableDeliveryGenerationForms();
    if (!forms.length) return;
    for (const form of forms) launchDeliveryGeneration(form);
  });
  syncDeliveryBatchButton();
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
      form.reset();
      showAlphaMessage(payload.message || "Uploaded.", "complete");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      showAlphaMessage(error.message, "failed");
    }
  });
}

const compactAudioPlayers = [...document.querySelectorAll("[data-compact-audio]")];
const audioDisclosures = [...document.querySelectorAll("[data-audio-disclosure]")];

function formatAudioTime(seconds) {
  const safeSeconds = Number.isFinite(seconds) && seconds > 0 ? seconds : 0;
  const minutes = Math.floor(safeSeconds / 60);
  const remainder = Math.floor(safeSeconds % 60).toString().padStart(2, "0");
  return `${minutes}:${remainder}`;
}

function syncCompactPlayer(player) {
  const audio = player.querySelector("audio");
  const button = player.querySelector("[data-audio-toggle]");
  const progress = player.querySelector("[data-audio-progress]");
  const time = player.querySelector("[data-audio-time]");
  if (!audio || !button || !progress || !time) return;
  const fallbackDuration = Number(player.dataset.duration) || 0;
  const duration = Number.isFinite(audio.duration) ? audio.duration : fallbackDuration;
  const currentTime = Number.isFinite(audio.currentTime) ? audio.currentTime : 0;
  const playing = !audio.paused && !audio.ended;
  progress.max = String(duration);
  progress.value = String(Math.min(currentTime, duration));
  time.textContent = `${formatAudioTime(currentTime)} / ${formatAudioTime(duration)}`;
  button.ariaPressed = String(playing);
  button.ariaLabel = `${playing ? "Pause" : "Play"} ${player.dataset.audioName}`;
  const icon = button.querySelector("i");
  if (icon) icon.className = `fa-solid ${playing ? "fa-pause" : "fa-play"}`;
}

function resetCompactAudio(player) {
  const audio = player.querySelector("audio");
  if (!audio) return;
  audio.pause();
  if (audio.currentTime !== 0) audio.currentTime = 0;
  syncCompactPlayer(player);
}

for (const player of compactAudioPlayers) {
  const audio = player.querySelector("audio");
  const toggle = player.querySelector("[data-audio-toggle]");
  const progress = player.querySelector("[data-audio-progress]");
  if (!audio || !toggle || !progress) continue;
  for (const eventName of ["loadedmetadata", "timeupdate", "pause"]) {
    audio.addEventListener(eventName, () => syncCompactPlayer(player));
  }
  audio.addEventListener("play", () => {
    for (const otherPlayer of compactAudioPlayers) {
      if (otherPlayer === player) continue;
      const otherAudio = otherPlayer.querySelector("audio");
      if (otherAudio && !otherAudio.paused) resetCompactAudio(otherPlayer);
    }
    syncCompactPlayer(player);
  });
  audio.addEventListener("ended", () => resetCompactAudio(player));
  toggle.addEventListener("click", () => {
    if (audio.paused) {
      audio.play().catch(() => {});
    } else {
      audio.pause();
    }
  });
  progress.addEventListener("input", () => {
    audio.currentTime = Number(progress.value);
    syncCompactPlayer(player);
  });
  syncCompactPlayer(player);
}

for (const disclosure of audioDisclosures) {
  const player = disclosure.querySelector("[data-compact-audio]");
  const audio = player?.querySelector("audio");
  if (!player || !audio) continue;
  disclosure.addEventListener("toggle", () => {
    if (!disclosure.open) {
      resetCompactAudio(player);
      return;
    }
    for (const otherDisclosure of audioDisclosures) {
      if (
        otherDisclosure === disclosure ||
        !otherDisclosure.open ||
        otherDisclosure.dataset.audioDisclosure !== disclosure.dataset.audioDisclosure
      ) {
        continue;
      }
      const otherPlayer = otherDisclosure.querySelector("[data-compact-audio]");
      if (otherPlayer) resetCompactAudio(otherPlayer);
      otherDisclosure.open = false;
    }
    if (audio.currentTime !== 0) audio.currentTime = 0;
    audio.play().catch(() => {
      // Browsers may still require the visible play control under strict autoplay policies.
    });
  });
}

for (const button of document.querySelectorAll("[data-action]")) {
  button.addEventListener("click", async () => {
    try {
      const payload = await runAlphaAction({
        url: button.dataset.url,
        method: button.dataset.method || "POST",
        paid: button.dataset.paid !== undefined,
        confirmRequired: button.dataset.confirmRequired !== undefined,
        confirmText: button.dataset.confirm || "",
        providerOperation: button.dataset.providerOperation,
        providerEstimate: meteringEstimate(button),
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
  const promptContext = methodController.querySelector("[data-prompt-context]");
  const promptField = promptContext?.querySelector("textarea");
  const panels = [...document.querySelectorAll("[data-method-panel]")];
  for (const panel of panels) {
    for (const button of panel.querySelectorAll("button")) {
      button.dataset.serverDisabled = button.disabled ? "true" : "false";
    }
  }
  const syncMethod = () => {
    const selected = selector.value;
    const usesVoiceDesign = selected === "designed" || selected === "reference_design";
    const showsDisabledPrompt = selected === "instant_clone";
    if (promptContext && promptField) {
      promptContext.hidden = !usesVoiceDesign && !showsDisabledPrompt;
      promptContext.classList.toggle("is-disabled", showsDisabledPrompt);
      promptField.disabled = !usesVoiceDesign;
    }
    for (const copy of document.querySelectorAll("[data-method-copy] [data-method]")) {
      copy.hidden = copy.dataset.method !== selected;
    }
    for (const panel of panels) {
      const active = panel.dataset.methodPanel === selected;
      panel.hidden = !active;
      for (const field of panel.querySelectorAll("input, select, textarea, button")) {
        if (field.tagName === "BUTTON") {
          field.disabled = field.dataset.serverDisabled === "true" || !active;
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
    let accountRequestId = startElevenLabsRequest("Checking ElevenLabs account", null, true);
    try {
      const payload = await loadProviderStatus();
      const account = payload.account || {};
      renderProviderUsageIndicator(account);
      providerAccount.querySelector("[data-provider-connection]").textContent = "Verified";
      providerAccount.querySelector("[data-provider-tier]").textContent = `${account.tier || "Unknown"} / ${account.status || "unknown"}`;
      providerAccount.querySelector("[data-provider-usage]").textContent = account.credits_used == null || account.credits_limit == null
        ? "Not reported by this account"
        : `${Number(account.credits_used).toLocaleString()} / ${Number(account.credits_limit).toLocaleString()} credits`;
      providerAccount.querySelector("[data-provider-remaining]").textContent = account.credits_remaining == null
        ? "Not reported"
        : `${Number(account.credits_remaining).toLocaleString()} credits`;
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
      finishElevenLabsRequest(accountRequestId);
      accountRequestId = null;
      showAlphaMessage(payload.message || "ElevenLabs account status was refreshed.", "complete");
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      }
      finishElevenLabsRequest(accountRequestId);
      accountRequestId = null;
      showAlphaMessage(error.message, "failed");
    } finally {
      if (accountRequestId) finishElevenLabsRequest(accountRequestId);
      loading.hidden = true;
      if (refresh) refresh.disabled = false;
    }
  };

  if (refresh) refresh.addEventListener("click", readAccount);
  if (loading) readAccount();
}

if (providerUsageIndicator) refreshProviderUsageIndicator();
