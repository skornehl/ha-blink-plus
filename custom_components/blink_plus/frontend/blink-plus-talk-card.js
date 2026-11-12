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
 *   exoplayer: false             # optional, see below
 *
 * Optional URL parameters, e.g. for a notification action:
 *   ?camera=camera.front_door&live=1   pick the camera and start the live view
 *
 * Live view: the card first wakes the camera with blink_plus.start_live_view
 * and waits until it streams; only then it attaches the player. A battery
 * camera needs 5-16 s to wake, and a player that starts cold gives up before
 * the first HLS segment exists. If the camera is in cool-down (another camera
 * on the same sync module just took over, or the last try failed), the card
 * says so and tries again when the cool-down is over, for up to 40 s. If the
 * player shows no picture 15 s after it was attached, it is recreated once.
 * The live view plays in the web view (hls.js / WebRTC), not in the Companion
 * app's native ExoPlayer, so the card can see whether a picture arrived;
 * exoplayer: true switches back to the native player.
 *
 * Talk works like a walkie-talkie: hold the button, speak, release. The
 * recording (WebM/Opus or whatever the browser records) goes to
 * POST /api/blink_plus/talk/<entity>, which transcodes it with ffmpeg and
 * plays it through the camera speaker while the live view is open.
 * Browsers only allow the microphone in a secure context (https).
 */
const CARD_VERSION = "0.6.0";
const MAX_MS = 30000;
const MIN_MS = 400;
const LIVE_WAIT_MS = 40000;
const WARM_MS = 40000; // retries (cool-downs) only within this time
const WARM_MAX_MS = 90000; // hard limit, even while the server is still starting
const HOLD_S = 60; // start_live_view keeps the camera awake this long ...
const HOLD_RENEW_MS = 45000; // ... and the card renews it while it shows the live view
const PICTURE_MS = 15000; // no picture after this long: recreate the player once

const TEXT = {
  en: {
    title: "Intercom",
    live: "▶ Live with sound",
    hangup: "■ Hang up",
    off: "Live view off",
    talk: "🎙️ Hold to talk",
    talking: "🔴 Talking … release to send",
    connecting: "Connecting …",
    waking: "Waking the camera …",
    busy: "Camera busy (another live view on the same sync module), retrying in {n} s",
    failedRetry: "Live view failed: {e} – retrying in {n} s",
    busyLater: "Camera busy (another live view on the same sync module) – try again in {n} s",
    failedLater: "Live view failed: {e} – try again in {n} s",
    reconnect: "No picture yet – reconnecting the player …",
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
    waking: "Kamera wird geweckt …",
    busy: "Kamera belegt (anderes Live-Bild am selben Sync-Modul), neuer Versuch in {n} s",
    failedRetry: "Live-Bild fehlgeschlagen: {e} – neuer Versuch in {n} s",
    busyLater: "Kamera belegt (anderes Live-Bild am selben Sync-Modul) – in {n} s nochmal versuchen",
    failedLater: "Live-Bild fehlgeschlagen: {e} – in {n} s nochmal versuchen",
    reconnect: "Noch kein Bild – Player wird neu verbunden …",
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
    return this._attrs().live_view_state;
  }

  _attrs() {
    const st = this._hass && this._hass.states[this._cam.entity];
    return (st && st.attributes) || {};
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
    const wasLive = !!this._stream || !!this._starting;
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
    if (this._stream || this._starting || !this._hass) return;
    if (!this._hass.states[this._cam.entity]) {
      this._status(this._t("notFound"));
      return;
    }
    // Every start, hang-up and camera switch gets a new run number; an older
    // run that is still waiting just stops.
    const run = (this._run = (this._run || 0) + 1);
    this._starting = true;
    this._status(this._t("connecting"));
    try {
      if (await this._warmUp(run)) await this._attach(run);
    } catch (e) {
      if (run === this._run) this._status(this._t("error") + (e.message || e));
    } finally {
      if (run === this._run) this._starting = false;
    }
  }

  // Open the live view with blink_plus.start_live_view and wait until it
  // streams. Returns false (with a message) if it doesn't.
  async _warmUp(run) {
    const entity = this._cam.entity;
    const t0 = Date.now();
    let next = 0; // when to call start_live_view (again)
    let wait = null; // { until, err } while the camera is in cool-down
    while (run === this._run) {
      const a = this._attrs();
      if (a.live_view_state === "streaming") return true;
      // Always once at the start (someone else may have started the live
      // view; the hold keeps it open for us), then only while not starting.
      if (Date.now() >= next && (next === 0 || a.live_view_state !== "starting")) {
        let r;
        try {
          r = await this._hold(entity, true);
        } catch (e) {
          // Integration without the action (or no permission): let the
          // player try on its own, as before.
          console.warn("blink-plus-talk-card: start_live_view failed", e);
          return run === this._run;
        }
        if (run !== this._run) return false;
        if (r.state === "streaming") return true;
        if (r.cooldown_s > 0) {
          const until = Date.now() + r.cooldown_s * 1000 + 500;
          if (until - t0 > WARM_MS) {
            const vars = { n: r.cooldown_s, e: r.last_error };
            this._status(this._t(r.last_error ? "failedLater" : "busyLater", vars));
            return false;
          }
          wait = { until, err: r.last_error };
          next = until;
        } else {
          wait = null;
          next = Date.now() + 3000; // let the session start before asking again
        }
      }
      const now = Date.now();
      if (wait && now < wait.until) {
        // until has 0.5 s of slack on top of the cool-down; don't show it.
        const vars = { n: Math.max(1, Math.ceil((wait.until - 500 - now) / 1000)), e: wait.err };
        this._status(this._t(wait.err ? "failedRetry" : "busy", vars));
      } else {
        this._status(this._t(a.live_view_state === "starting" ? "waking" : "connecting"));
      }
      // Give up after WARM_MS, unless the server is still busy starting (it
      // has its own time limits); never wait longer than WARM_MAX_MS.
      const spent = now - t0;
      if ((spent > WARM_MS && a.live_view_state !== "starting") || spent > WARM_MAX_MS) {
        this._status(this._t("noLive") + (a.live_view_last_error ? `: ${a.live_view_last_error}` : ""));
        this._stopLive(entity);
        return false;
      }
      await new Promise((r) => setTimeout(r, 250));
    }
    return false;
  }

  // start_live_view for one camera. With answer, returns its status
  // ({state, cooldown_s, last_error, ...}); falls back to the attributes on
  // a Home Assistant that can't return action responses.
  async _hold(entity, answer = false) {
    const res = await this._hass.callService(
      "blink_plus", "start_live_view", { seconds: HOLD_S }, { entity_id: entity }, false, answer,
    );
    if (!answer) return null;
    const r = res && res.response && res.response[entity];
    if (r) return r;
    const a = this._attrs();
    return { state: a.live_view_state, cooldown_s: 0, last_error: a.live_view_last_error };
  }

  async _createStream() {
    if (await this._streamElementReady()) {
      const el = document.createElement("ha-camera-stream");
      el.hass = this._hass;
      el.stateObj = this._hass.states[this._cam.entity];
      el.controls = true;
      el.muted = false;
      // In the web view, not the Companion app's native player: the card can
      // see whether a picture arrived, and hls.js keeps retrying (see README).
      el.allowExoPlayer = !!this._config.exoplayer;
      return el;
    }
    const helpers = await window.loadCardHelpers();
    const el = await helpers.createCardElement({
      type: "picture-entity", entity: this._cam.entity, camera_view: "live", show_name: false, show_state: false,
    });
    el.hass = this._hass;
    return el;
  }

  // The camera streams: attach the player, keep the camera awake while it is
  // shown, and recreate the player once if no picture arrives.
  async _attach(run) {
    const el = await this._createStream();
    if (run !== this._run) return;
    this._el.view.innerHTML = "";
    this._el.view.appendChild(el);
    this._stream = el;
    this._status(this._t("streaming"));
    const entity = this._cam.entity;
    clearInterval(this._renew);
    this._renew = setInterval(() => {
      if (run !== this._run || !this._stream) return clearInterval(this._renew);
      this._hold(entity).catch(() => {});
    }, HOLD_RENEW_MS);
    if (el.allowExoPlayer) return; // the native player can't be looked into
    clearTimeout(this._picTimer);
    this._picTimer = setTimeout(async () => {
      if (run !== this._run || !this._stream || this._hasPicture(this._stream)) return;
      this._status(this._t("reconnect"));
      try {
        await this._hold(entity).catch(() => {});
        const again = await this._createStream();
        if (run !== this._run || !this._stream) return;
        this._stream.replaceWith(again);
        this._stream = again;
        this._status(this._t("streaming"));
      } catch (e) {
        if (run === this._run) this._status(this._t("error") + (e.message || e));
      }
    }, PICTURE_MS);
  }

  // A playing <video> (HLS / WebRTC) or a loaded <img> (MJPEG) anywhere
  // inside the player, through its shadow roots.
  _hasPicture(root) {
    const todo = [root];
    while (todo.length) {
      const n = todo.pop();
      if (n.tagName === "VIDEO" && n.readyState >= 2 && n.videoWidth > 0) return true;
      if (n.tagName === "IMG" && n.complete && n.naturalWidth > 0) return true;
      if (n.shadowRoot) todo.push(...n.shadowRoot.children);
      if (n.children) todo.push(...n.children);
    }
    return false;
  }

  _stopTimers() {
    clearInterval(this._renew);
    clearTimeout(this._picTimer);
  }

  _stopLive(entity) {
    return this._hass.callService("blink_plus", "stop_live_view", { entity_id: entity }).catch(() => {
      /* already off */
    });
  }

  async _hangUp() {
    this._run = (this._run || 0) + 1;
    this._starting = false;
    this._stopTimers();
    if (this._stream) {
      this._stream.remove();
      this._stream = null;
    }
    this._el.view.innerHTML = `<div class="empty">${this._t("off")}</div>`;
    this._status("");
    await this._stopLive(this._cam.entity);
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
    // Stops a warm-up still running and the renewing; an open live view ends
    // on its own (the hold runs out, then Home Assistant's player).
    this._run = (this._run || 0) + 1;
    this._starting = false;
    this._stopTimers();
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
