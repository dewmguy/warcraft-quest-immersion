const alphaMessage = document.querySelector("#alpha-message");
const alphaMessageTitle = alphaMessage.querySelector("[data-alpha-message-title]");
const alphaMessageDetail = alphaMessage.querySelector("[data-alpha-message-detail]");
const alphaMessageElapsed = alphaMessage.querySelector("[data-alpha-message-elapsed]");
const alphaMessageProgress = alphaMessage.querySelector("[data-alpha-message-progress]");
let alphaProviderTimer = null;
let alphaProviderRequest = null;

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
  if (!alphaProviderRequest) return;
  const elapsedSeconds = (Date.now() - alphaProviderRequest.startedAt) / 1000;
  const elapsed = `Elapsed ${formatElapsedTime(elapsedSeconds)}`;
  alphaMessageProgress.setAttribute("aria-valuetext", `${alphaProviderRequest.operation}; ${elapsed}`);
  renderAlphaMessage({
    title: alphaProviderRequest.operation,
    detail: providerProgressDetail(alphaProviderRequest, elapsedSeconds),
    state: "working",
    elapsed,
    provider: true,
  });
}

function startElevenLabsRequest(operation, estimate = null, readOnly = false) {
  stopAlphaProviderTimer();
  alphaProviderRequest = {
    operation: operation || "Processing an ElevenLabs request",
    estimate,
    readOnly,
    startedAt: Date.now(),
  };
  renderElevenLabsProgress();
  alphaProviderTimer = window.setInterval(renderElevenLabsProgress, 1000);
}

function finishElevenLabsRequest(text, state) {
  const request = alphaProviderRequest;
  const elapsedSeconds = request ? (Date.now() - request.startedAt) / 1000 : 0;
  stopAlphaProviderTimer();
  alphaProviderRequest = null;
  renderAlphaMessage({
    title: state === "complete" ? "ElevenLabs request complete" : "ElevenLabs request failed",
    detail: text,
    state,
    elapsed: `${state === "complete" ? "Completed" : "Stopped"} in ${formatElapsedTime(elapsedSeconds)}`,
  });
}

function showAlphaMessage(text, state = "working") {
  if (alphaProviderRequest && state !== "working") {
    finishElevenLabsRequest(text, state);
    return;
  }
  stopAlphaProviderTimer();
  alphaProviderRequest = null;
  renderAlphaMessage({ title: text, state });
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
  const warning = element.dataset.confirm?.trim();
  const action = estimate.kind === "voice-design"
    ? "This creates three Voice Design candidates; ElevenLabs charges the preview text once."
    : "This creates one speech candidate.";
  const preface = warning ? `${warning}\n\n` : "";
  return `${preface}${action}\n\nPreflight estimate:\n- ${estimate.characters.toLocaleString()} metered characters\n- about $${estimate.dollars.toFixed(3)} at the published v2/v3 API list rate\n- about ${formatSeconds(estimate.seconds)} of audio per candidate\n\nYour plan and the provider response determine exact usage. Continue?`;
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

async function runAlphaAction({ url, method = "POST", body = null, paid = false, confirmRequired = false, confirmText = "", providerOperation = "", providerEstimate = null }) {
  const shouldConfirm = paid || confirmRequired;
  if (shouldConfirm && !window.confirm(confirmText || "This action can contact ElevenLabs and may consume credits or a voice slot. Continue?")) return null;
  if (paid) startElevenLabsRequest(providerOperation, providerEstimate);
  else showAlphaMessage("Saving…");
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
        providerOperation: form.dataset.providerOperation,
        providerEstimate: meteringEstimate(form),
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
      form.reset();
      showAlphaMessage(payload.message || "Uploaded.", "complete");
      window.setTimeout(() => window.location.reload(), 350);
    } catch (error) {
      showAlphaMessage(error.message, "failed");
    }
  });
}

const compactAudioPlayers = [...document.querySelectorAll("[data-compact-audio]")];
const referenceClips = [...document.querySelectorAll(".reference-clip")];

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

for (const clip of referenceClips) {
  const player = clip.querySelector("[data-compact-audio]");
  const audio = player?.querySelector("audio");
  if (!player || !audio) continue;
  clip.addEventListener("toggle", () => {
    if (!clip.open) {
      resetCompactAudio(player);
      return;
    }
    for (const otherClip of referenceClips) {
      if (otherClip === clip || !otherClip.open) continue;
      const otherPlayer = otherClip.querySelector("[data-compact-audio]");
      if (otherPlayer) resetCompactAudio(otherPlayer);
      otherClip.open = false;
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
        confirmText: paidConfirmation(button),
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
    startElevenLabsRequest("Checking ElevenLabs account", null, true);
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
      showAlphaMessage(payload.message || "ElevenLabs account status was refreshed.", "complete");
    } catch (error) {
      if (errorBox) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      }
      showAlphaMessage(error.message, "failed");
    } finally {
      loading.hidden = true;
      if (refresh) refresh.disabled = false;
    }
  };

  if (refresh) refresh.addEventListener("click", readAccount);
  if (loading) readAccount();
}
