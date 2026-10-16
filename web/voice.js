"use strict";

// Voice input/output, shared by kiosk.js and app.js. Loaded as a plain
// classic script (this project has no build step) -- include it BEFORE
// kiosk.js / app.js, which call what this file defines on `window`:
// attachVoiceInput(), attachClearButton(), speak(), and the two parse
// helpers (parseSpokenNumber, parseSpokenSex) for reception fields.
//
// Two speech-to-text tiers, tried in order, mirroring the same "try the
// model, degrade honestly" ladder the rest of this app uses for extraction
// (triage/ollama.py, triage/local_stt.py):
//
//   1. Web Speech API (SpeechRecognition) -- live, in-browser, free, no
//      server round trip. The tradeoff: Chrome/Edge ship the audio to
//      Google's servers to transcribe it. Not available in Firefox at all.
//   2. A local recorder that encodes 16-bit PCM WAV client-side (no ffmpeg,
//      no server-side decode step) and POSTs it to /api/transcribe, where
//      triage/local_stt.py runs Vosk entirely on this machine. Used only
//      when tier 1 is unavailable, or errors out for this session.
//
// Either tier only ever fills the SAME text box a person could have typed
// into -- nothing is ever auto-submitted from voice. A misheard "no" is
// exactly the kind of silent corruption the rest of this system's guards
// (quote verification, denial checks) exist to catch, and none of that
// applies if voice bypasses the box entirely.

const _SpeechRecognitionCtor = window.SpeechRecognition || window.webkitSpeechRecognition;
const VOICE_INPUT_POSSIBLE = !!(_SpeechRecognitionCtor ||
  (navigator.mediaDevices && navigator.mediaDevices.getUserMedia));

function speak(text, lang) {
  if (!window.speechSynthesis || !text) return;
  window.speechSynthesis.cancel();   // don't stack utterances if called again quickly
  const u = new SpeechSynthesisUtterance(text);
  if (lang) u.lang = lang;           // the browser picks a matching voice if it has one
  window.speechSynthesis.speak(u);
}

// A question in the patient's own language, spoken by the server's
// text-to-speech (Sarvam Bulbul) -- far better Indian-language voices than a
// browser usually has, and cached server-side so a question asked of many
// patients is only ever synthesised once. Falls back to the browser's own
// voice if the server can't (no key, rate limit, offline): a worse voice
// beats silence for someone who can't read the screen. `ref` ties the request
// to a live session so this can't be used as an open text-to-speech proxy.
let _spoken = null;

function stopSpeaking() {
  if (_spoken) { _spoken.pause(); _spoken = null; }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
}

async function speakNative(ref, text, lang) {
  stopSpeaking();
  if (!text) return false;
  try {
    const r = await fetch("/api/tts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ref, text, language: lang }),
    });
    if (!r.ok) throw new Error("tts " + r.status);
    const url = URL.createObjectURL(await r.blob());
    const audio = new Audio(url);
    audio.onended = () => URL.revokeObjectURL(url);
    _spoken = audio;
    await audio.play();
    return true;
  } catch {
    speak(text, lang);
    return false;
  }
}

// ---------------------------------------------------- tier 2: local record

function _encodeWav(samples, sampleRate) {
  // samples: Int16Array, mono. A minimal 16-bit PCM WAV header, no deps --
  // this is the one format triage/local_stt.py's transcribe_wav() accepts.
  const buf = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buf);
  const str = (offset, s) => { for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i)); };
  str(0, "RIFF"); view.setUint32(4, 36 + samples.length * 2, true);
  str(8, "WAVE"); str(12, "fmt "); view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true); view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  str(36, "data"); view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) view.setInt16(44 + i * 2, samples[i], true);
  return new Blob([buf], { type: "audio/wav" });
}

function _downsampleTo16k(float32, inRate) {
  const outRate = 16000;
  if (inRate === outRate) return float32;
  const ratio = inRate / outRate;
  const out = new Float32Array(Math.floor(float32.length / ratio));
  for (let i = 0; i < out.length; i++) out[i] = float32[Math.floor(i * ratio)];
  return out;
}

class _LocalRecorder {
  async start() {
    this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const source = this.ctx.createMediaStreamSource(this.stream);
    // ScriptProcessorNode is deprecated in favour of AudioWorklet, but needs
    // no separate worklet file to load -- the simpler, more portable choice
    // for a prototype; still broadly supported everywhere this matters.
    this.node = this.ctx.createScriptProcessor(4096, 1, 1);
    this.chunks = [];
    this.node.onaudioprocess = (e) => this.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    source.connect(this.node);
    this.node.connect(this.ctx.destination);
  }

  async stop() {
    this.node.disconnect();
    this.stream.getTracks().forEach((t) => t.stop());
    const inRate = this.ctx.sampleRate;
    const total = this.chunks.reduce((n, c) => n + c.length, 0);
    const merged = new Float32Array(total);
    let off = 0;
    for (const c of this.chunks) { merged.set(c, off); off += c.length; }
    await this.ctx.close();
    const down = _downsampleTo16k(merged, inRate);
    const samples = new Int16Array(down.length);
    for (let i = 0; i < down.length; i++) {
      const s = Math.max(-1, Math.min(1, down[i]));
      samples[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return _encodeWav(samples, 16000);
  }
}

// Sarvam's REST speech-to-text refuses audio past 30 seconds (HTTP 422); stop
// recording a little before that rather than lose an answer at the very end.
const MAX_RECORD_MS = 28000;

async function _transcribeLocally(blob, lang) {
  const r = await fetch(`/api/transcribe?lang=${encodeURIComponent(lang || "en-IN")}`,
                        { method: "POST", body: blob });
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || "local transcription failed");
  return data.text || "";
}

// ---------------------------------------------------- parsing a spoken value
// For a free-text box (narrative, a reply, a note) the raw transcript IS the
// answer. A number field or a <select> needs the transcript turned into the
// one thing that field actually holds -- these two are for reception fields
// (age, sex) on both the kiosk and the nurse screen. Neither is asked to be
// clever: a number field takes the first digits spoken (Chrome's own engine
// already turns "twenty seven" into "27" for a clear utterance), and sex
// matches a small, obvious word list. Either can fail to find anything, in
// which case the field is left alone rather than guessing.

function parseSpokenNumber(transcript) {
  const m = transcript.match(/\d+(\.\d+)?/);
  return m ? m[0] : null;
}

const _SEX_WORDS = [
  [/\bfemale\b|\bwoman\b|\bgirl\b/i, "F"],
  [/\bmale\b|\bman\b|\bboy\b/i, "M"],
  [/\bother\b/i, "Other"],
];
function parseSpokenSex(transcript) {
  for (const [re, val] of _SEX_WORDS) if (re.test(transcript)) return val;
  return null;
}

// ---------------------------------------------------- the button people click

// attachVoiceInput(button, field, opts) wires a mic button to fill `field`
// (an <input> or <select> or <textarea>) with speech -- live if the browser
// can do it, recorded-then-transcribed if it has to fall back.
//
// Two modes:
//   - no opts.parse (the default): the raw transcript is APPENDED to
//     whatever's already in the field, exactly like typing more would be --
//     this is what narrative/reply/note boxes want.
//   - opts.parse(transcript) => value|null: the field is REPLACED with
//     whatever parse() returns, and left alone if it returns null -- this is
//     what a number or <select> field wants (an age or a sex is stated once,
//     not built up sentence by sentence). See parseSpokenNumber/
//     parseSpokenSex above for the two this app actually uses.
//
// `opts.onError(message)` is called on a failure the caller should surface --
// kiosk.js and app.js choose very different wording for the same failure
// (see their own call sites). `opts.onFinal(fieldValue)` fires once recording
// actually stops (not on every interim update) -- the kiosk's single-utterance
// identity extraction uses this to know exactly when to send the transcript
// off, rather than firing a server call on every word recognised so far.
function attachVoiceInput(button, field, opts = {}) {
  if (!VOICE_INPUT_POSSIBLE) {
    button.hidden = true;
    return null;
  }

  let recognition = null;
  let recorder = null;
  let recording = false;
  let useFallback = !_SpeechRecognitionCtor;
  let baseText = "";
  let mode = null;          // "native" | "server" -- how THIS recording is being captured
  let autoStop = null;
  // The patient's language is read at click time, not wired once: the kiosk's
  // picker, or a nurse switching a patient's language, changes it between
  // clicks. Anything but English skips the browser's recogniser entirely and
  // goes to the server (Sarvam), which hears Indian languages far better
  // than the browser does -- and, unlike English, has no local fallback.
  const langOf = () => (opts.getLang ? opts.getLang() : "en-IN");

  function setState(state) {
    button.dataset.voiceState = state;   // "idle" | "listening" | "transcribing"
    button.textContent =
      state === "listening" ? (opts.listeningLabel || "Stop") :
      state === "transcribing" ? "Transcribing…" :
      (opts.idleLabel || "Speak");
  }
  setState("idle");

  function apply(transcript) {
    if (opts.parse) {
      const value = opts.parse(transcript);
      if (value != null) field.value = value;
    } else {
      field.value = baseText + transcript;
    }
  }

  function startNative() {
    recognition = new _SpeechRecognitionCtor();
    recognition.continuous = true;
    recognition.interimResults = true;
    recognition.lang = opts.lang || "en-US";
    baseText = field.value ? field.value + " " : "";
    recognition.onresult = (e) => {
      let transcript = "";
      for (let i = 0; i < e.results.length; i++) transcript += e.results[i][0].transcript;
      apply(transcript);
    };
    recognition.onerror = (e) => {
      // A genuine "can't do this here" -- fall back for every click from now
      // on, not just this one, rather than making someone click twice each time.
      if (["not-allowed", "service-not-allowed", "audio-capture", "network"].includes(e.error)) {
        useFallback = true;
        if (opts.onError) opts.onError(`Voice recognition (${e.error}) -- switching to the local fallback.`);
      }
      recording = false;
      setState("idle");
    };
    recognition.onend = () => {
      recording = false;
      setState("idle");
      if (opts.onFinal) opts.onFinal(field.value);
    };
    try {
      recognition.start();
      recording = true;
      mode = "native";
      setState("listening");
    } catch {
      useFallback = true;
      startFallback();
    }
  }

  async function startFallback() {
    try {
      recorder = new _LocalRecorder();
      await recorder.start();
      baseText = field.value ? field.value + " " : "";
      recording = true;
      mode = "server";
      setState("listening");
      autoStop = setTimeout(() => { if (recording && mode === "server") stopFallback(); }, MAX_RECORD_MS);
    } catch (e) {
      setState("idle");
      if (opts.onError) opts.onError("Microphone access failed: " + e.message);
    }
  }

  async function stopFallback() {
    clearTimeout(autoStop);
    autoStop = null;
    setState("transcribing");
    try {
      const blob = await recorder.stop();
      const text = await _transcribeLocally(blob, langOf());
      apply(text);
      if (opts.onFinal) opts.onFinal(field.value);
    } catch (e) {
      if (opts.onError) opts.onError(e.message);
    } finally {
      recording = false;
      mode = null;
      setState("idle");
    }
  }

  button.addEventListener("click", () => {
    if (recording) {
      if (mode === "server") stopFallback(); else recognition.stop();
      return;
    }
    if (useFallback || langOf() !== "en-IN") startFallback(); else startNative();
  });

  return { get recording() { return recording; } };
}

// Pairs with attachVoiceInput: if a transcription came out wrong, clearing
// the field and re-recording should be one click, not a select-all-delete.
// Deliberately just clears -- it never re-triggers listening on its own, so
// clearing and speaking again is still two distinct, visible actions.
function attachClearButton(button, field) {
  button.addEventListener("click", () => {
    field.value = "";
    field.focus();
  });
}
