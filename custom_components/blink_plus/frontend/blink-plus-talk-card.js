/*
 * Blink Plus talk card: live view with sound plus push-to-talk.
 *
 * Ships with the integration (served from /blink_plus/, loaded on every
 * dashboard), so no resource has to be added.
 *
 *   type: custom:blink-plus-talk-card
 *   title: Front door            # optional
 *   cameras:
 *     - entity: camera.front_door
 *       name: Front door          # optional
 *     - camera.driveway           # short form
 *
 * Optional URL parameters, e.g. for a notification action:
 *   ?camera=camera.front_door&live=1   pick the camera and start the live view
 *
 * Talk works like a walkie-talkie: hold the button, speak, release. The
 * recording (WebM/Opus or whatever the browser records) goes to
 * POST /api/blink_plus/talk/<entity>, which transcodes it with ffmpeg and
 * plays it through the camera speaker while the live view is open.
 * Browsers only allow the microphone in a secure context (https).
 */
const CARD_VERSION = "0.5.0";
const MAX_MS = 30000;
const MIN_MS = 400;
const LIVE_WAIT_MS = 40000;

const TEXT = {
  en: {
    title: "Intercom",
    live: "▶ Live with sound",
    hangup: "■ Hang up",
    off: "Live view off",
    talk: "🎙️ Hold to talk",
    talking: "🔴 Talking … release to send",
    connecting: "Connecting …",
    streaming: "Live – sound via the speaker icon in the picture",
    micAsk: "Allowing the microphone …",
    micNo: "No microphone access: ",
    micInsecure: "The microphone only works over https. Listening works anyway.",
    tooShort: "Too short – keep the button pressed while you speak",
    waitLive: "Waiting for the live view …",
    noLive: "Live view did not start",
    sending: "Sending {s} s …",
    sent: "Said ✓",
    error: "Error: ",
    notFound: "Camera not found",
    recording: "Recording",
  },
  de: {
    title: "Sprechanlage",
    live: "▶ Live + Ton",
    hangup: "■ Auflegen",
    off: "Live-Bild aus",
    talk: "🎙️ Gedrückt halten zum Sprechen",
    talking: "🔴 Spricht … loslassen zum Senden",
    connecting: "Verbinde …",
    streaming: "Live – Ton über das Lautsprechersymbol im Bild",
    micAsk: "Mikrofon wird freigegeben …",
    micNo: "Kein Mikrofonzugriff: ",
    micInsecure: "Das Mikrofon geht nur über https. Zuhören geht trotzdem.",
    tooShort: "Zu kurz – Knopf beim Sprechen gedrückt halten",
    waitLive: "Warte auf das Live-Bild …",
    noLive: "Live-Bild kam nicht",
    sending: "Sende {s} s …",
    sent: "Gesagt ✓",
    error: "Fehler: ",
    notFound: "Kamera nicht gefunden",
    recording: "Aufnahme läuft",
  },
};

class BlinkPlusTalkCard extends HTMLElement {
  static getStubConfig(hass) {
    const cam = Object.keys(hass.states).find(
      (e) => e.startsWith("camera.") && hass.states[e].attributes.live_view_state !== undefined,
    );
    return { cameras: cam ? [cam] : [] };
  }

  setConfig(config) {
    if (!config.cameras || !config.cameras.length) {
      throw new Error("cameras is required");
    }
    this._config = {
      ...config,
      cameras: config.cameras.map((c) => (typeof c === "string" ? { entity: c } : c)),
    };
    this._idx = 0;
    const p = new URLSearchParams(window.location.search);
    const wanted = p.get("camera") || p.get("kamera");
    if (wanted) {
      const i = this._config.cameras.findIndex((c) => c.entity === wanted);
      if (i >= 0) this._idx = i;
    }
    this._autoLive = p.get("live") === "1";
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._root) this._build();
    if (this._stream) this._stream.hass = hass;
    this._updateInfo();
  }

  getCardSize() {
    return 7;
  }

  _t(key, vars = {}) {
    const lang = (this._hass && this._hass.language) || "en";
    const table = TEXT[lang.split("-")[0]] || TEXT.en;
    return (table[key] || TEXT.en[key]).replace(/\{(\w+)\}/g, (_, k) => vars[k]);
  }

  get _cam() {
    return this._config.cameras[this._idx];
  }

  _name(c) {
    if (c.name) return c.name;
    const st = this._hass && this._hass.states[c.entity];
    return (st && st.attributes.friendly_name) || c.entity;
  }

  _build() {
    this._root = this.attachShadow({ mode: "open" });
    this._root.innerHTML = `
      <style>
        ha-card { padding: 12px; }
        .title { font-size: 1.2em; font-weight: 500; margin-bottom: 8px; }
        .pick { display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
        .pick button, .row button {
          flex: 1; padding: 10px; border-radius: 10px; font-size: 1em;
          border: 1px solid var(--divider-color); cursor: pointer;
          background: var(--card-background-color); color: var(--primary-text-color);
        }
        .pick button.active { background: var(--primary-color); color: var(--text-primary-color, #fff); border-color: var(--primary-color); }
        .pick:has(button:only-child) { display: none; }
        .view { position: relative; width: 100%; aspect-ratio: 16 / 9; background: #000;
          border-radius: 10px; overflow: hidden; display: flex; align-items: center; justify-content: center; }
        .view .empty { color: #bbb; text-align: center; padding: 12px; }
        .view > *:not(.empty) { width: 100%; }
        .row { display: flex; gap: 8px; margin-top: 8px; }
        .talk {
          margin-top: 10px; width: 100%; padding: 22px 10px; border-radius: 14px;
          font-size: 1.2em; font-weight: 500; border: none; cursor: pointer;
          background: var(--primary-color); color: var(--text-primary-color, #fff);
          touch-action: none; user-select: none; -webkit-user-select: none; -webkit-touch-callout: none;
        }
        .talk.on { background: var(--error-color, #db4437); }
        .talk[disabled] { opacity: 0.5; }
        .status { margin-top: 8px; min-height: 1.3em; color: var(--secondary-text-color); text-align: center; }
        .hint { margin-top: 6px; font-size: 0.85em; color: var(--warning-color, #e6a100); text-align: center; }
      </style>
      <ha-card>
        <div class="title"></div>
        <div class="pick"></div>
        <div class="view"><div class="empty"></div></div>
        <div class="row">
          <button class="live"></button>
          <button class="hangup"></button>
        </div>
        <button class="talk"></button>
        <div class="status"></div>
        <div class="hint"></div>
      </ha-card>`;
    const $ = (s) => this._root.querySelector(s);
    this._el = {
      title: $(".title"), pick: $(".pick"), view: $(".view"), live: $(".live"),
      hangup: $(".hangup"), talk: $(".talk"), status: $(".status"), hint: $(".hint"),
    };
    this._el.title.textContent = this._config.title || this._t("title");
    this._el.view.firstElementChild.textContent = this._t("off");
    this._el.live.textContent = this._t("live");
    this._el.hangup.textContent = this._t("hangup");
    this._el.talk.textContent = this._t("talk");
    this._config.cameras.forEach((c, i) => {
      const b = document.createElement("button");
      b.textContent = this._name(c);
      b.addEventListener("click", () => this._pickCamera(i));
      this._el.pick.appendChild(b);
    });
    this._el.live.addEventListener("click", () => this._liveOn());
    this._el.hangup.addEventListener("click", () => this._hangUp());
    const t = this._el.talk;
    t.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      if (t.setPointerCapture) t.setPointerCapture(e.pointerId);
      this._press();
    });
    ["pointerup", "pointercancel", "lostpointercapture"].forEach((ev) => t.addEventListener(ev, () => this._release()));
    t.addEventListener("contextmenu", (e) => e.preventDefault());
    if (!this._micPossible()) this._el.hint.textContent = this._t("micInsecure");
    this._mark();
    if (this._autoLive) setTimeout(() => this._liveOn(), 0);
  }

  _micPossible() {
    return window.isSecureContext && navigator.mediaDevices && window.MediaRecorder;
  }

  _mark() {
    [...this._el.pick.children].forEach((b, i) => b.classList.toggle("active", i === this._idx));
  }

  _status(text) {
    if (this._el) this._el.status.textContent = text;
  }

  _liveState() {
    const st = this._hass && this._hass.states[this._cam.entity];
    return st && st.attributes.live_view_state;
  }

  _updateInfo() {
    if (!this._el || !this._stream || this._rec || this._busy) return;
    const now = this._el.status.textContent;
    const lv = this._liveState();
    if (lv === "starting" && now === "") this._status(this._t("connecting"));
    if (lv === "streaming" && (now === "" || now === this._t("connecting"))) this._status(this._t("streaming"));
  }

  async _pickCamera(i) {
    if (i === this._idx || this._busy || this._rec) return;
    const wasLive = !!this._stream;
    if (wasLive) await this._hangUp();
    this._idx = i;
    this._mark();
    if (wasLive) this._liveOn();
  }

  async _streamElementReady() {
    if (customElements.get("ha-camera-stream")) return true;
    try {
      // Creating a picture-entity card loads ha-camera-stream as a side effect.
      const helpers = await window.loadCardHelpers();
      const c = await helpers.createCardElement({ type: "picture-entity", entity: this._cam.entity, camera_view: "live" });
      c.hass = this._hass;
    } catch (e) {
      /* fall back below */
    }
    try {
      await Promise.race([
        customElements.whenDefined("ha-camera-stream"),
        new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 5000)),
      ]);
      return true;
    } catch (e) {
      return false;
    }
  }

  async _liveOn() {
    if (this._stream || !this._hass) return;
    const st = this._hass.states[this._cam.entity];
    if (!st) {
      this._status(this._t("notFound"));
      return;
    }
    this._status(this._t("connecting"));
    let el;
    if (await this._streamElementReady()) {
      el = document.createElement("ha-camera-stream");
      el.hass = this._hass;
      el.stateObj = st;
      el.controls = true;
      el.muted = false;
      el.allowExoPlayer = true;
    } else {
      const helpers = await window.loadCardHelpers();
      el = await helpers.createCardElement({
        type: "picture-entity", entity: this._cam.entity, camera_view: "live", show_name: false, show_state: false,
      });
      el.hass = this._hass;
    }
    this._el.view.innerHTML = "";
    this._el.view.appendChild(el);
    this._stream = el;
  }

  async _hangUp() {
    if (this._stream) {
      this._stream.remove();
      this._stream = null;
    }
    this._el.view.innerHTML = `<div class="empty">${this._t("off")}</div>`;
    this._status("");
    try {
      await this._hass.callService("blink_plus", "stop_live_view", { entity_id: this._cam.entity });
    } catch (e) {
      /* already off */
    }
  }

  async _press() {
    if (this._busy || this._rec) return;
    this._pressed = true;
    if (!this._micPossible()) {
      this._status(this._t("micInsecure"));
      return;
    }
    try {
      if (!this._mic || !this._mic.active) {
        this._status(this._t("micAsk"));
        this._mic = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
      }
    } catch (e) {
      this._status(this._t("micNo") + (e.name || e.message));
      return;
    }
    if (!this._pressed) {
      this._status(this._t("tooShort"));
      return;
    }
    if (!this._stream) this._liveOn();
    const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus"]
      .find((m) => MediaRecorder.isTypeSupported(m)) || "";
    this._chunks = [];
    const rec = new MediaRecorder(this._mic, type ? { mimeType: type } : {});
    rec.ondataavailable = (e) => {
      if (e.data && e.data.size) this._chunks.push(e.data);
    };
    rec.start();
    this._rec = rec;
    this._t0 = Date.now();
    this._max = setTimeout(() => this._release(), MAX_MS);
    this._el.talk.classList.add("on");
    this._el.talk.textContent = this._t("talking");
    this._status(this._t("recording"));
  }

  _release() {
    this._pressed = false;
    clearTimeout(this._max);
    const rec = this._rec;
    if (!rec) return;
    this._rec = null;
    this._el.talk.classList.remove("on");
    this._el.talk.textContent = this._t("talk");
    const ms = Date.now() - this._t0;
    rec.onstop = () => this._send(new Blob(this._chunks, { type: rec.mimeType || "audio/webm" }), ms);
    rec.stop();
  }

  async _waitLive() {
    const end = Date.now() + LIVE_WAIT_MS;
    while (Date.now() < end) {
      if (this._liveState() === "streaming") return true;
      await new Promise((r) => setTimeout(r, 250));
    }
    return false;
  }

  async _send(blob, ms) {
    if (ms < MIN_MS || blob.size < 500) {
      this._status(this._t("tooShort"));
      return;
    }
    this._busy = true;
    this._el.talk.disabled = true;
    const entity = this._cam.entity;
    try {
      if (this._liveState() !== "streaming") {
        this._status(this._t("waitLive"));
        if (!(await this._waitLive())) throw new Error(this._t("noLive"));
      }
      this._status(this._t("sending", { s: (ms / 1000).toFixed(1) }));
      const r = await this._hass.fetchWithAuth(`/api/blink_plus/talk/${entity}`, {
        method: "POST",
        body: blob,
        headers: { "Content-Type": (blob.type || "audio/webm").split(";")[0] },
      });
      if (!r.ok) throw new Error((await r.text()) || r.status);
      this._status(this._t("sent"));
    } catch (e) {
      this._status(this._t("error") + (e.message || e));
    } finally {
      this._busy = false;
      this._el.talk.disabled = false;
    }
  }

  disconnectedCallback() {
    if (this._rec) this._release();
    if (this._mic) {
      this._mic.getTracks().forEach((t) => t.stop());
      this._mic = null;
    }
  }
}

if (!customElements.get("blink-plus-talk-card")) {
  customElements.define("blink-plus-talk-card", BlinkPlusTalkCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "blink-plus-talk-card",
    name: "Blink Plus talk",
    description: "Live view with sound and push-to-talk for Blink Plus cameras",
  });
  console.info(`%c BLINK-PLUS-TALK-CARD ${CARD_VERSION} `, "background:#03a9f4;color:#fff");
}
