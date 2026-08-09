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

async function runAlphaAction({ url, method = "POST", body = null, paid = false }) {
  if (paid && !window.confirm("This action can contact ElevenLabs and may consume credits or a voice slot. Continue?")) return null;
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
