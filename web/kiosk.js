"use strict";

// This page never asks the server for, and never renders, anything about
// severity: no category, no confidence, no branches, no "why" a question was
// chosen. It only ever touches four things: who the patient is, what they
// said, the next question, and their reply to it. Compare app.js, which
// renders the full clinical picture -- that is a deliberate difference in
// what each page is TRUSTED with, not just what it happens to show. See the
// caveat in kiosk.html's own comment: this is a UI-level boundary, not an
// authentication one -- nothing stops a technical patient from calling
// /api/state directly and seeing the full payload anyway.

let PATIENT_REF = null;

async function api(path, body) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ref: PATIENT_REF, ...body }) }
    : {};
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) {
    if (data.no_such_patient) {
      await startSession();
      throw new Error("restarted");
    }
    throw new Error(data.error || "Something went wrong. Please try again.");
  }
  return data;
}

const $ = (id) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`page element #${id} is missing from kiosk.html`);
  return el;
};

const STAGES = ["stageIdentity", "stageNarrative", "stageQuestions", "stageWaiting", "stageDone"];
function show(stageId) {
  for (const id of STAGES) $(id).hidden = (id !== stageId);
}

function showError(fieldId, message) {
  const el = $(fieldId);
  el.textContent = message;
  el.style.display = message ? "block" : "none";
}

async function startSession() {
  const data = await api("/api/patient/new", { origin: "kiosk" });
  PATIENT_REF = data.ref;
  PATIENT_LANG = "en-IN";          // a fresh session always starts in English
  renderLanguagePicker();
  const url = new URL(window.location);
  url.searchParams.set("patient", PATIENT_REF);
  window.history.replaceState({}, "", url);
  show("stageIdentity");
}

// ---------------------------------------------------------------- language
// The picker only exists when the server says it can actually hear, translate
// and speak more than English -- see /api/languages. What it changes: how this
// patient's speech is transcribed, and which language questions are shown and
// spoken in. It changes nothing about how anyone is triaged; the engine reads
// the same English findings either way. The rest of this page's labels are
// still English -- localising them is a separate, larger job.

let PATIENT_LANG = "en-IN";
let OFFERED_LANGUAGES = [];

async function loadLanguages() {
  try {
    const r = await fetch("/api/languages");
    OFFERED_LANGUAGES = (await r.json()).languages || [];
  } catch {
    OFFERED_LANGUAGES = [];
  }
  renderLanguagePicker();
}

function renderLanguagePicker() {
  const box = $("langButtons");
  $("langPicker").hidden = OFFERED_LANGUAGES.length < 2;
  box.innerHTML = "";
  for (const lang of OFFERED_LANGUAGES) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "lang-btn";
    b.textContent = lang.native;
    b.lang = lang.code;
    b.setAttribute("aria-pressed", String(lang.code === PATIENT_LANG));
    b.addEventListener("click", async () => {
      try {
        await api("/api/language", { language: lang.code });
        PATIENT_LANG = lang.code;
        renderLanguagePicker();
      } catch (e) {
        if (e.message !== "restarted") showError("errIdentity", "That language isn't available right now.");
      }
    });
    box.appendChild(b);
  }
}

// ---------------------------------------------------------------- identity

$("btnIdentity").addEventListener("click", async () => {
  const name = $("k_name").value.trim();
  showError("errIdentity", "");
  $("btnIdentity").disabled = true;
  try {
    await api("/api/identity", {
      name,
      age: $("k_age").value,
      age_months: $("k_age_months").value,
      sex: $("k_sex").value,
      patient_id: $("k_id").value.trim(),
    });
    show("stageNarrative");
  } catch (e) {
    if (e.message !== "restarted") showError("errIdentity", e.message);
  } finally {
    $("btnIdentity").disabled = false;
  }
});

// ---------------------------------------------------------------- narrative

$("btnNarrative").addEventListener("click", async () => {
  const text = $("k_narrative").value.trim();
  showError("errNarrative", "");
  if (!text) {
    showError("errNarrative", "Please describe what's bothering you before continuing.");
    return;
  }
  $("btnNarrative").disabled = true;
  try {
    await api("/api/narrative", { text });
    await askNext();
  } catch (e) {
    if (e.message !== "restarted") showError("errNarrative", e.message);
    show("stageNarrative");
  } finally {
    $("btnNarrative").disabled = false;
  }
});

// ---------------------------------------------------------------- photo
// Deliberately minimal on this side: pick/take a photo, it uploads and gets
// analysed immediately, and this page shows only that a photo was added --
// never the caption, never a candidate check. That review happens on the
// nurse screen, same boundary as everything else here.
attachPhotoCapture($("btnTakePhoto"), $("btnUploadPhoto"), $("k_photo_upload"), async (fileOrBlob) => {
  const chip = document.createElement("div");
  chip.className = "photo-chip";
  chip.textContent = "Adding photo…";
  $("photoChips").appendChild(chip);

  try {
    const blob = await resizeImage(fileOrBlob);
    const preview = URL.createObjectURL(blob);
    await uploadPhoto(PATIENT_REF, blob);
    chip.innerHTML = "";
    const img = document.createElement("img");
    img.src = preview;
    chip.appendChild(img);
    chip.appendChild(document.createTextNode("Photo added"));
  } catch (e) {
    chip.textContent = "Couldn't add that photo — please try again.";
  }
});

// ---------------------------------------------------------------- the loop

async function askNext() {
  show("stageWaiting");
  let data;
  try {
    data = await api(`/api/next?ref=${encodeURIComponent(PATIENT_REF)}`);
  } catch (e) {
    if (e.message === "restarted") return;
    // A transient failure here shouldn't strand the patient on a blank
    // screen -- treat it the same as "nothing more to ask" rather than show
    // an error a patient has no way to act on; staff still see the record.
    show("stageDone");
    return;
  }
  const next = data.next;
  if (!next || next.stopped || !next.question) {
    show("stageDone");
    return;
  }
  // Deliberately narrow: only the question text (and the check id needed to
  // submit a reply against) reach the page. next.why and next.pending both
  // exist in the API response and are never read here.
  LAST_QUESTION = next;
  // In the patient's language when there is one; English otherwise (including
  // when the translation service is down, which beats a blank question box).
  $("k_question").textContent = next.question_native || next.question;
  $("k_question").dataset.check = next.check;
  $("k_reply").value = "";
  show("stageQuestions");
  $("k_reply").focus();
  speakQuestion(next);   // read aloud for a patient who can't read the screen
}

let LAST_QUESTION = null;

function speakQuestion(next) {
  if (!next) return;
  if (PATIENT_LANG !== "en-IN" && next.question_native) {
    speakNative(PATIENT_REF, next.question_native, PATIENT_LANG);   // server voice, browser voice as fallback
  } else {
    speak(next.question);
  }
}

async function sendReply() {
  const reply = $("k_reply").value.trim();
  if (!reply) return;
  $("btnReply").disabled = true;
  try {
    await api("/api/answer", { check: $("k_question").dataset.check, reply });
  } catch (e) {
    if (e.message === "restarted") return;
  } finally {
    $("btnReply").disabled = false;
  }
  await askNext();
}

$("btnReply").addEventListener("click", sendReply);
$("k_reply").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendReply();
});

$("btnFinish").addEventListener("click", () => show("stageDone"));
$("btnReplay").addEventListener("click", () => speakQuestion(LAST_QUESTION));

// ---------------------------------------------------------------- voice
// Never the only way to answer -- the text boxes above always still work.
// Errors here stay deliberately vague, matching the rest of this page's
// boundary: nothing technical is ever surfaced to a patient. Clear pairs
// with every Speak button, so a wrong transcription is one click to redo,
// not a select-all-delete.

attachVoiceInput($("voiceNarrative"), $("k_narrative"), {
  getLang: () => PATIENT_LANG,
  onError: () => showError("errNarrative", "Voice input isn't available right now — please type instead."),
});
attachClearButton($("clearNarrative"), $("k_narrative"));

attachVoiceInput($("voiceReply"), $("k_reply"), {
  getLang: () => PATIENT_LANG,
  onError: () => showError("errQuestions", "Voice input isn't available right now — please type instead."),
});
attachClearButton($("clearReply"), $("k_reply"));

// One spoken self-introduction ("My name is John Smith, I'm 45, male") fills
// the whole form below, instead of a separate mic per field. The transcript
// goes into a hidden buffer field (never shown), gets sent to
// /api/extract_identity, and only fields the extraction actually found get
// filled -- everything typed already, or typed afterward to correct it,
// still works exactly like typing always does. Patient ID is deliberately
// left out of this: a misheard digit there could pull up the wrong person's
// record, which is a different and worse failure than a misheard name.
attachVoiceInput($("voiceIdentity"), $("k_identity_raw"), {
  getLang: () => PATIENT_LANG,
  onError: () => showError("errIdentity", "Voice input isn't available right now — please type instead."),
  onFinal: async (transcript) => {
    if (!transcript.trim()) return;
    showError("identityVoiceNote", "Listening for your name, age and sex…");
    try {
      const r = await fetch("/api/extract_identity", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: transcript, language: PATIENT_LANG }),
      });
      const found = await r.json();
      if (found.name) $("k_name").value = found.name;
      if (found.sex) $("k_sex").value = found.sex;
      if (found.age_months != null) $("k_age_months").value = found.age_months;
      else if (found.age != null) $("k_age").value = found.age;
      const any = found.name || found.sex || found.age != null || found.age_months != null;
      showError("identityVoiceNote", any
        ? "Heard: “" + transcript + "” — please check the details below and fix anything wrong."
        : "Didn't catch a name, age or sex in that — please fill the details in below.");
    } catch {
      showError("identityVoiceNote", "Couldn't process that — please fill the details in below.");
    }
  },
});
attachClearButton($("clearIdentity"), $("k_identity_raw"));
$("clearIdentity").addEventListener("click", () => showError("identityVoiceNote", ""));

// ---------------------------------------------------------------- boot

(async function boot() {
  const urlRef = new URL(window.location).searchParams.get("patient");
  if (urlRef) {
    PATIENT_REF = urlRef;
    try {
      const st = await api(`/api/state?ref=${encodeURIComponent(PATIENT_REF)}`);
      PATIENT_LANG = (st.encounter && st.encounter.language) || "en-IN";
      show("stageIdentity");   // a resumed session still starts at check-in;
                                // nothing here re-derives which stage it was on
    } catch (e) {
      if (e.message !== "restarted") await startSession();
    }
  } else {
    await startSession();
  }
  await loadLanguages();
})();
