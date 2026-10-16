"use strict";

// The page holds no clinical logic. Every category, confidence and ledger entry
// on screen came from the engine over HTTP -- there is nowhere here for a UI to
// quietly disagree with the system it is showing.

const RANK = { RED: 1, ORANGE: 2, YELLOW: 3, GREEN: 4, BLUE: 5 };
const LABEL = { RED: "Immediate", ORANGE: "Very urgent", YELLOW: "Urgent",
                GREEN: "Standard", BLUE: "Non-urgent" };
let TARGET = { RED: 0, ORANGE: 10, YELLOW: 60, GREEN: 120, BLUE: 240 };
// Overwritten from /api/targets on boot so the pack stays the authority.

const EXAMPLE = "I've had this pain in my upper stomach since about six this " +
                "morning. I feel sick and I'm a bit sweaty. It's not going away.";

// A missing element used to surface as "Cannot set properties of null", which
// names neither the element nor the caller. Fail loudly with the id instead --
// a typo'd or renamed id is otherwise a five-minute hunt.
const $ = (id) => {
  const el = document.getElementById(id);
  if (!el) throw new Error(`page element #${id} is missing from index.html`);
  return el;
};
let state = null;
let nextStep = null;
let chosen = "RED";

// Which patient THIS kiosk/tab is talking to. The server can hold many
// Encounters at once now -- every request has to say which one it means,
// the same way current() used to mean "the one and only patient" implicitly.
let PATIENT_REF = null;

async function api(path, body) {
  // Every POST is about one specific patient, so the ref rides along
  // automatically rather than every call site remembering to add it. The
  // one call that predates having a ref (/api/patient/new) just sends null,
  // which the server ignores -- it isn't looking anyone up yet.
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ref: PATIENT_REF, ...body }) }
    : {};
  const r = await fetch(path, opts);
  const data = await r.json();
  if (!r.ok) {
    if (data.no_such_patient) {
      // The in-memory session directory doesn't survive a server restart --
      // a stale ref from before one is the ordinary case, not a bug. Start
      // fresh rather than surfacing a raw error for something recoverable.
      await startNewPatient();
      throw new Error("Session was reset (server restarted or session " +
        "expired) -- a new patient record has been started. Please re-enter details.");
    }
    throw new Error(data.error || "request failed");
  }
  return data;
}

function busy(on) {
  document.querySelector(".shell").classList.toggle("busy", on);
}

// ---------------------------------------------------------------- rendering

function renderChecks() {
  const host = $("checks");
  host.innerHTML = "";
  if (!state.checks.length) {
    host.innerHTML = '<p class="empty">Nothing recorded yet. A check nobody ' +
      "heard stays unknown — it never becomes “no”.</p>";
    return;
  }
  for (const c of state.checks) {
    const row = document.createElement("div");
    row.className = "check";
    row.innerHTML =
      `<span class="check-name${c.positive ? "" : " neg"}">` +
      `${c.positive ? "" : "Not present: "}${c.text}</span>` +
      `<span class="src">${c.origin}</span>` +
      (c.quote ? `<span class="q">“${c.quote}”</span>` : "");
    host.appendChild(row);
  }
}

function renderVerdict() {
  // After an override the whole chip belongs to the clinician, target included.
  // Taking the label from the override and the target from the engine produced
  // "Immediate — 10 min", which is Very urgent's target under an Immediate
  // heading: two different decisions rendered as one.
  const shown = state.override ? state.override.category : state.category;
  $("chip").dataset.c = shown;
  $("chipL").textContent = LABEL[shown];
  $("chipT").textContent = TARGET[shown] === 0 ? "immediate" : TARGET[shown] + " min";

  $("because").innerHTML = state.override
    ? `<b>Clinician override</b><span class="on">set by ${state.override.by}, not by the engine</span>`
    : state.fired
      ? `<b>${state.fired}</b><span class="on">${
          state.fired_general
            ? "a general check — carried by every branch"
            : "on: " + state.fired_on.join(", ")}</span>`
      : state.from_vitals
        ? "<b>Vital-sign score</b><span class=\"on\">no positive check yet — the " +
          "category floor came from vitals</span>"
        : "Nothing positive yet.";

  $("band").dataset.b = state.confidence.band;
  $("band").textContent = state.confidence.band;
  $("why").textContent = state.confidence.text.replace(/^\w+ -- /, "");

  const decided = state.checks.length > 0;
  $("accept").disabled = !decided || !!state.override || !!state.admitted;
  $("accept").textContent = state.admitted
    ? `Admitted${state.bed ? " — bed " + state.bed : " — queued, no bed free"}`
    : "Accept";
  $("override").disabled = !decided || !!state.override;
}

function renderBranches() {
  const host = $("branches");
  if (!state.branches.length) { host.className = "empty"; host.textContent = "—"; return; }
  host.className = "";
  host.innerHTML = state.branches.map(b =>
    `<div class="check"><span class="check-name">${b.name}</span>` +
    `<span class="src">${b.weight.toFixed(2)}</span></div>`).join("");
}

function renderActions() {
  const a = state.actions;
  $("actionsPanel").hidden = !a;
  if (!a) return;
  $("actionsBranch").textContent = a.branch;
  $("actions").innerHTML = a.items.map((text, i) =>
    `<div class="action-item"><span class="n">${i + 1}</span><span>${text}</span></div>`
  ).join("");
}

function renderRejected() {
  const host = $("rejected");
  const rejected = state.rejected || [];
  const fell = state.fallbacks || [];
  let html = "";

  // Two different things, shown differently. A dropped rung is routine; a
  // rejected finding means the model said something the guard would not believe.
  if (fell.length) {
    html += '<div class="fellback"><b>Running on a lower tier</b>' +
      fell.map(f => `<div>${f}</div>`).join("") + "</div>";
  }
  if (rejected.length) {
    html += '<div class="rejected"><b>Rejected by the guard</b>' +
      rejected.map(r => `<div>${r}</div>`).join("") + "</div>";
  }
  host.innerHTML = html;
}

function renderLedger() {
  const host = $("ledger");
  host.innerHTML = state.ledger.map(e => {
    const p = e.payload || {};
    let body;
    if (e.kind === "observed") {
      body = `${p.check} = ${p.value}` + (p.quote ? ` ‹ “${p.quote}”` : "");
    } else if (e.kind === "overridden") {
      body = `${e.actor} ${p.recommended} → ${p.chosen} · ${p.direction} · ${p.reason}` +
             (p.note ? `<br>“${p.note}”` : "");
    } else if (e.kind === "decided") {
      body = `${p.category} ${p.label} · fired ${p.fired || "—"}`;
    } else {
      body = Object.entries(p).map(([k, v]) => `${k}=${v}`).join(" ");
    }
    return `<div class="ev"><span class="ev-s">${String(e.seq).padStart(2, "0")}</span>` +
           `<span class="ev-k" data-k="${e.kind}">${e.kind}</span>` +
           `<span class="ev-b">${body}<br><span class="ev-h">${e.prev} → ${e.digest}</span></span></div>`;
  }).join("");
  $("evCount").textContent = state.ledger.length +
    (state.ledger.length === 1 ? " event" : " events");
  $("chain").hidden = !state.ledger.length;
  $("chainT").textContent = state.chain_ok
    ? `${state.ledger.length} events, chain intact` : "CHAIN BROKEN";
  $("hash").textContent = state.ledger.length
    ? state.ledger[state.ledger.length - 1].digest : "—";
}

function renderTranscript() {
  // For a non-English patient the English line is a TRANSLATION, and a
  // mistranslation is a new way to be wrong -- so the patient's own words are
  // shown first, and the English is labelled as what the system worked from.
  // Same boundary as everywhere else: the nurse can see exactly what was said
  // and exactly what was understood, and disagree with either.
  const native = state.transcript_native || [];
  $("said").innerHTML = state.transcript.map((t, i) => native[i]
    ? `<div class="said"><span lang="${state.language}">${native[i]}</span>` +
      `<span class="said-en"><b>understood as</b> ${t}</span></div>`
    : `<div class="said">${t}</div>`).join("");
  $("tCount").textContent = state.transcript.length +
    (state.transcript.length === 1 ? " utterance" : " utterances");
  $("cCount").textContent = state.checks.length +
    (state.checks.length === 1 ? " check" : " checks");
}

function renderVitals() {
  const n = state.news2;
  const el = $("news2");
  if (!n) {
    // Withheld rather than wrong: the adult score is not validated in children
    // or pregnancy, and an incomplete set is not a score at all.
    el.textContent = state.vitals && Object.keys(state.vitals).length
      ? "score needs a full set" : "—";
    el.removeAttribute("data-band");
    return;
  }
  el.textContent = `NEWS2 ${n.total} · ${n.band}`;
  el.dataset.band = n.band;
}

function renderPaediatricFloor() {
  const reasons = state.paediatric_reasons || [];
  $("paedFloor").innerHTML = reasons.length
    ? '<div class="cwarn"><b>Paediatric vital-sign floor</b>' +
      reasons.map(r => `<div>${r}</div>`).join("") + "</div>"
    : "";
}

function formatAge(years) {
  // Under 2, years alone rounds away the distinction that actually matters
  // for paediatric vital banding -- show months instead.
  if (years < 2) return `${Math.round(years * 12)}mo`;
  return `${Math.round(years * 10) / 10}y`;
}

function renderIdentity() {
  if ($("p_lang").querySelector(`option[value="${state.language}"]`)) {
    $("p_lang").value = state.language;
  }
  const who = [];
  if (state.name) who.push(state.name);
  if (state.age !== null && state.age !== undefined) who.push(formatAge(state.age));
  if (state.sex) who.push(state.sex);
  $("whoLine").textContent = (who.join(" · ") || "unidentified") +
    " · walk-in · live engine, not a recording";

  const c = state.cohort || {};
  const chip = $("cohortChip");
  chip.textContent = c.cohort ? c.cohort.replace("Cohort.", "") : "—";
  // Unresolved is the interesting state: the adult thresholds are withheld
  // rather than applied to someone whose age nobody established.
  chip.dataset.guard = String(c.cohort === "unresolved" || !c.adult_score_applies);

  $("cohortWarn").innerHTML = (c.warnings || []).length
    ? '<div class="cwarn">' + c.warnings.map(w => `<div>${w}</div>`).join("") + "</div>"
    : "";

  const note = $("recordNote");
  if (state.record) {
    note.innerHTML = `<span class="rec">Record found — ${state.record.name}, ` +
      `${state.record.history.join(", ")}; on ${state.record.medications.join(", ")}` +
      (state.record.flags.length ? ` <b>[${state.record.flags.join(" · ")}]</b>` : "") +
      "</span>";
  } else if (state.patient_id) {
    note.textContent = "No record on file for that ID — first attendance.";
  } else {
    note.textContent = "No record looked up yet.";
  }
}

function renderTier() {
  $("ref").textContent = state.ref;
  $("tierText").textContent = state.tier === "none" ? "waiting" : state.tier + " extraction";
  $("tier").dataset.degraded = String(state.tier === "keyword");
  $("useModel").checked = state.use_model;
}

function renderQueue(board) {
  $("bedCount").textContent = `${board.capacity - board.free_beds}/${board.capacity} beds`;
  $("queue").innerHTML = board.patients.map(p => {
    const over = p.due_in < 0;
    const bedText = p.bed ? `<span class="pt-bed">bed ${p.bed}</span>`
                           : '<span class="pt-nobed">no bed — queued</span>';
    return `<div class="pt" data-c="${p.category}" data-ref="${p.ref}" data-overdue="${over}"
                 data-has-record="${p.has_record}">
      <div class="pt-top"><span class="pt-id">${p.name || p.ref}</span>
      <span class="pt-w">${p.waited}m</span></div>
      <span class="pt-c">${p.label}${p.raised_from ? " ← " + p.raised_from : ""}</span>
      ${bedText}
      <span class="due${over ? " over" : ""}">${
        p.interval === 0 ? "continuous"
        : over ? `re-check overdue ${-p.due_in}m` : `re-check in ${p.due_in}m`}</span>
      ${p.starved ? '<span class="flag">FIND THIS PATIENT</span>' : ""}
      <div class="row"><button type="button" class="reassess">Reassess</button></div>
    </div>`;
  }).join("");
  $("queue").querySelectorAll(".pt").forEach((el) => {
    el.addEventListener("click", () => showPatient(el.dataset.ref));
    // A quick glance (single click) vs actually working the patient (double
    // click, or the Reassess button) -- both of the latter load the full
    // record into the main dashboard, the same path the intake queue's own
    // cards use, so nothing here is a second, thinner view of the same data.
    // Guarded by has_record: the three seeded demo beds never went through a
    // real encounter, and without this guard, loading one silently recovers
    // onto a brand-new blank patient (api()'s no_such_patient handling) --
    // quietly hijacking the tab's current session rather than failing loudly.
    el.addEventListener("dblclick", () => openWorkingRecord(el.dataset.ref));
  });
  $("queue").querySelectorAll(".reassess").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      openWorkingRecord(btn.closest(".pt").dataset.ref);
    });
  });
}

function openWorkingRecord(ref) {
  const tile = $("queue").querySelector(`.pt[data-ref="${CSS.escape(ref)}"]`);
  if (tile && tile.dataset.hasRecord === "false") {
    alert("This is a seeded demo bed with no real encounter behind it -- " +
          "there is nothing further to open.");
    return;
  }
  viewPatient(ref);
}

async function showPatient(ref) {
  let p;
  try {
    p = await api(`/api/patient?ref=${encodeURIComponent(ref)}`);
  } catch (e) { alert(e.message); return; }

  $("pdName").textContent = p.name || p.ref;
  $("pdSub").textContent = [
    p.age !== null && p.age !== undefined ? p.age + "y" : null,
    p.sex || null,
    p.patient_id || null,
  ].filter(Boolean).join(" · ") || "no further identity recorded";

  $("pdChip").dataset.c = p.category;
  $("pdCat").textContent = p.label;
  $("pdBed").textContent = p.bed ? `bed ${p.bed}` : "no bed — queued";

  $("pdNote").innerHTML = p.note
    ? `<p class="why">${p.note}</p>`
    : (p.starved ? '<p class="warn">Waiting far past target for this category.</p>' : "");

  $("pdConfidence").textContent = p.confidence ? p.confidence.text : "";

  const checks = p.checks || [];
  $("pdChecks").innerHTML = checks.length
    ? checks.map(c =>
        `<div class="check"><span class="check-name${c.positive ? "" : " neg"}">` +
        `${c.positive ? "" : "Not present: "}${c.text}</span>` +
        `<span class="src">${c.origin}</span></div>`).join("")
    : '<p class="empty">Nothing recorded against this patient.</p>';

  const said = p.transcript || [];
  $("pdSaid").innerHTML = said.length
    ? said.map(t => `<div class="said">${t}</div>`).join("")
    : '<p class="empty">No narrative on file.</p>';

  $("patientDlg").showModal();
}

function renderNext() {
  if (!nextStep) { $("next").hidden = true; return; }
  $("next").hidden = false;

  const q = nextStep.question;
  $("nextActor").textContent = nextStep.actor || (nextStep.stopped ? "engine" : "staff");
  // A stop is a decision, not an absence. Saying "alert and stop" is the
  // difference between a system that finished and a screen that went blank.
  $("nextQ").textContent = q || nextStep.stopped ||
    "Nothing further the patient can answer.";
  // What the patient is actually being shown and told, when it isn't English.
  $("nextNative").textContent = nextStep.question_native || "";
  $("nextNative").hidden = !nextStep.question_native;
  $("nextNative").lang = nextStep.language || "en-IN";
  // A text box only when there is something for the patient to reply to.
  $("answerBox").hidden = !q;

  // The model's own reason for picking this one over the rest of the
  // shortlist -- display only, never written to belief, never touches the
  // category. Empty when the model agreed with the plain ranking, or wasn't
  // reachable and the system fell back to it directly.
  $("nextWhy").textContent = nextStep.why || "";
  $("nextWhy").hidden = !nextStep.why;

  // Everything the ranking wants that is NOT a patient question. These usually
  // outrank the question -- a saturation probe is cheap and settles several
  // checks at once -- so they belong on screen, just not in the question box.
  const pending = nextStep.pending || [];
  $("pending").innerHTML = pending.length
    ? '<div class="pend"><b>Also worth getting</b>' + pending.map(p =>
        `<div><span class="pactor">${p.actor}</span>${p.text}</div>`).join("") + "</div>"
    : "";
}

function render(payload) {
  if (payload.encounter) state = payload.encounter;
  else state = payload;
  renderTier(); renderTranscript(); renderChecks(); renderVerdict();
  renderBranches(); renderActions(); renderRejected(); renderLedger(); renderNext();
  renderVitals(); renderPaediatricFloor(); renderIdentity(); renderPhotos();
}

// ---------------------------------------------------------------- photos
// A caption here is a model's proposal, exactly like a suggested next
// question's "why" is -- it never touched the belief state on its own. Only
// Confirm (via /api/photo/confirm, the same observe() a typed staff finding
// uses) makes anything here a recorded check.

function humanCheck(id) {
  return id.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function renderPhotos() {
  const photos = state.photos || [];
  $("photoCount").textContent = photos.length;
  if (!photos.length) {
    $("photos").innerHTML = '<p class="photo-empty">No photos added for this patient yet.</p>';
    return;
  }
  $("photos").innerHTML = photos.map((p) => {
    const src = `/api/photo?ref=${encodeURIComponent(PATIENT_REF)}&id=${encodeURIComponent(p.id)}`;
    if (!p.available) {
      return `
        <div class="photo">
          <img class="photo-thumb" src="${src}" onclick="window.open('${src}','_blank')">
          <div>
            <p class="photo-unavailable">No vision model was reachable when this photo
              was added -- please review the image directly.</p>
            <div class="photo-meta">${p.detail}</div>
          </div>
        </div>`;
    }
    const options = p.candidate_checks.map((c) =>
      `<option value="${c}">${humanCheck(c)}</option>`).join("");
    return `
      <div class="photo" data-photo-id="${p.id}">
        <img class="photo-thumb" src="${src}" onclick="window.open('${src}','_blank')">
        <div>
          <p class="photo-caption">${p.caption || "(no description returned)"}</p>
          <div class="photo-meta">${p.tier} tier</div>
          ${p.status === "pending" ? `
            <div class="photo-actions">
              ${options ? `<select class="photoCheckSelect">${options}</select>` : ""}
              <button class="photoConfirm">${options ? "Confirm" : "Mark reviewed"}</button>
              <button class="photoReject">Reject</button>
            </div>` : `
            <div class="photo-actions"><span class="photo-status" data-s="${p.status}">${p.status}</span></div>`}
        </div>
      </div>`;
  }).join("");

  $("photos").querySelectorAll(".photoConfirm").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".photo");
      const select = card.querySelector(".photoCheckSelect");
      busy(true);
      try {
        render(await api("/api/photo/confirm",
          { id: card.dataset.photoId, check_id: select ? select.value : null }));
      } catch (e) { alert(e.message); } finally { busy(false); }
    });
  });
  $("photos").querySelectorAll(".photoReject").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const card = btn.closest(".photo");
      busy(true);
      try {
        render(await api("/api/photo/reject", { id: card.dataset.photoId }));
      } catch (e) { alert(e.message); } finally { busy(false); }
    });
  });
}

// ---------------------------------------------------------------- actions

async function refresh() {
  const data = await api(`/api/state?ref=${encodeURIComponent(PATIENT_REF)}`);
  render(data);
  renderQueue(data.board);
  await refreshIntake();
}

// ------------------------------------------------------- kiosk intake queue

async function refreshIntake() {
  // No ref needed -- /api/queue lists every in-progress kiosk session, not
  // one patient's state. A plain fetch rather than api(), since this must
  // not fail (or recover-onto-a-new-session) just because THIS tab's own
  // PATIENT_REF happens to be stale -- the queue is about everyone else too.
  let rows;
  try {
    rows = await (await fetch("/api/queue")).json();
  } catch {
    return;   // transient network hiccup -- leave the panel as it was
  }
  renderIntake(rows);
}

function renderIntake(rows) {
  $("intakeCount").textContent = rows.length +
    (rows.length === 1 ? " patient" : " patients");
  $("intakeQueue").innerHTML = rows.length
    ? rows.map(r => `
        <div class="in" data-c="${r.category}" data-critical="${r.critical}" data-ref="${r.ref}">
          <div class="in-top">
            <span class="in-name">${r.name || "Unidentified"}</span>
            <span class="in-w">${r.waited_minutes}m</span>
          </div>
          <span class="in-c">${r.label}${r.critical ? " — needs attention" : ""}</span>
          ${r.untranslated ? '<span class="in-flag">Not translated — read their own words</span>' : ""}
          <div class="in-meta">${[
            r.age !== null && r.age !== undefined ? formatAge(r.age) : null,
            r.sex, `${r.checks_recorded} checks`,
          ].filter(Boolean).join(" · ")}</div>
        </div>`).join("")
    : '<p class="in-empty">No kiosk check-ins waiting on review.</p>';
  $("intakeQueue").querySelectorAll(".in").forEach(el =>
    el.addEventListener("click", () => viewPatient(el.dataset.ref)));
}

function populatePatientForm(s) {
  // Unlike normal typing (never overwritten mid-input, see api()'s comment
  // on that), switching to review a DIFFERENT patient must show what they
  // actually entered -- that is the whole point of a review queue.
  $("p_name").value = s.name || "";
  const known = s.age !== null && s.age !== undefined;
  $("p_age").value = known && s.age >= 2 ? s.age : "";
  $("p_age_months").value = known && s.age < 2 ? Math.round(s.age * 12) : "";
  $("p_sex").value = s.sex || "";
  $("p_id").value = s.patient_id || "";
}

async function viewPatient(ref) {
  PATIENT_REF = ref;
  const url = new URL(window.location);
  url.searchParams.set("patient", ref);
  window.history.replaceState({}, "", url);
  nextStep = null;
  const data = await api(`/api/state?ref=${encodeURIComponent(ref)}`);
  populatePatientForm(data.encounter);
  render(data);
  await refresh();
}

async function askNext() {
  const data = await api(`/api/next?ref=${encodeURIComponent(PATIENT_REF)}`);
  nextStep = data.next;
  render(data);
}

$("send").addEventListener("click", async () => {
  const text = $("narrative").value.trim();
  if (!text) return;
  busy(true);
  try {
    render(await api("/api/narrative", { text }));
    $("narrative").value = "";
    await askNext();
  } catch (e) { alert(e.message); } finally { busy(false); }
});

$("example").addEventListener("click", () => { $("narrative").value = EXAMPLE; });

$("sendReply").addEventListener("click", async () => {
  const reply = $("reply").value.trim();
  if (!reply || !nextStep) return;
  busy(true);
  try {
    const data = await api("/api/answer", { check: nextStep.check, reply });
    render(data);
    $("reply").value = "";
    if (data.outcome === "unclear") {
      // Unclear is a normal outcome, not a failure -- the check stays unknown
      // and the solver moves on rather than guessing.
      $("nextQ").textContent = "Unclear — left unknown. Next:";
    }
    await askNext();
  } catch (e) { alert(e.message); } finally { busy(false); }
});

async function observe(positive) {
  const check = $("obsCheck").value;
  if (!check) return;
  busy(true);
  try {
    render(await api("/api/observe", { check, positive, note: "observed by staff" }));
    await askNext();
  } catch (e) { alert(e.message); } finally { busy(false); }
}
$("saveId").addEventListener("click", async () => {
  busy(true);
  try {
    render(await api("/api/identity", {
      name: $("p_name").value, age: $("p_age").value,
      age_months: $("p_age_months").value,
      sex: $("p_sex").value, patient_id: $("p_id").value,
    }));
  } catch (e) { alert(e.message); } finally { busy(false); }
});

$("saveVitals").addEventListener("click", async () => {
  busy(true);
  try {
    render(await api("/api/vitals", {
      respiratory_rate: $("v_rr").value, spo2: $("v_spo2").value,
      systolic_bp: $("v_sbp").value, pulse: $("v_hr").value,
      temperature: $("v_temp").value, pain_score: $("v_pain").value,
      on_oxygen: $("v_o2").checked, alert: $("v_alert").checked,
      looks_unwell: $("v_unwell").checked,
    }));
    await askNext();
  } catch (e) { alert(e.message); } finally { busy(false); }
});

$("saveFlacc").addEventListener("click", async () => {
  busy(true);
  try {
    render(await api("/api/flacc", {
      face: $("f_face").value, legs: $("f_legs").value,
      activity: $("f_activity").value, cry: $("f_cry").value,
      consolability: $("f_consolability").value,
    }));
    await askNext();
  } catch (e) { alert(e.message); } finally { busy(false); }
});

$("obsYes").addEventListener("click", () => observe(true));
$("obsNo").addEventListener("click", () => observe(false));

$("accept").addEventListener("click", async () => {
  busy(true);
  try {
    const result = await api("/api/accept", { clinician: "rn.k.mensah" });
    render(result);
    await refresh();   // the board panel only updates from /api/state
  } catch (e) { alert(e.message); } finally { busy(false); }
});

$("useModel").addEventListener("change", async (e) => {
  await api("/api/tier", { use_model: e.target.checked });
  await refresh();
});

// Every raw <input>/<textarea> the nurse can type into for THIS patient.
// None of these are driven by `state` on every render -- deliberately, so
// typing mid-sentence never gets clobbered by a background refresh -- which
// is exactly why Reset has to clear them explicitly rather than relying on
// the next render to overwrite them.
function clearPatientForm() {
  for (const id of ["p_name", "p_age", "p_age_months", "p_id", "narrative",
                     "v_rr", "v_spo2", "v_sbp", "v_hr", "v_temp", "v_pain"]) {
    $(id).value = "";
  }
  $("p_sex").value = "";
  $("v_o2").checked = false;
  $("v_alert").checked = true;
  $("v_unwell").checked = false;
  for (const id of ["f_face", "f_legs", "f_activity", "f_cry", "f_consolability"]) {
    $(id).value = "0";
  }
  $("reply").value = "";
}

function _adoptPatient(data) {
  PATIENT_REF = data.ref;
  const url = new URL(window.location);
  url.searchParams.set("patient", PATIENT_REF);
  window.history.replaceState({}, "", url);
  nextStep = null;
  clearPatientForm();
  render(data);
}

async function startNewPatient() {
  // No prior session to clean up -- first load, or recovering from a ref
  // the server no longer recognises (most likely it restarted; ENCOUNTERS
  // is in-memory only). Nothing to abandon server-side, just create.
  _adoptPatient(await api("/api/patient/new", { origin: "nurse" }));
  await refresh();
}

$("reset").addEventListener("click", async () => {
  // The kiosk's own "done, next patient" action. Unlike startNewPatient(),
  // this DOES have a session to clean up -- /api/reset pops the old ref out
  // of ENCOUNTERS server-side before handing back a new one, so a kiosk
  // cycling through patients all day doesn't leak an abandoned Encounter
  // for every single one of them.
  _adoptPatient(await api("/api/reset", {}));
  await refresh();
});

// ---------------------------------------------------------------- override

function selectCat(cat) {
  chosen = cat;
  document.querySelectorAll("#ovCats button").forEach(b =>
    b.setAttribute("aria-pressed", String(b.dataset.c === cat)));
  // Lowering a priority is the one thing the system will not do on its own.
  $("ovWarn").hidden = RANK[cat] <= RANK[state.category];
}
document.querySelectorAll("#ovCats button").forEach(b =>
  b.addEventListener("click", () => selectCat(b.dataset.c)));

$("override").addEventListener("click", async () => {
  await api("/api/shown", { to: "rn.k.mensah" });
  $("ovLead").innerHTML =
    `System recommends <b>${state.label}</b>. Your decision replaces it and is ` +
    "recorded against your name.";
  $("ovErr").hidden = true;
  selectCat(state.category === "RED" ? "ORANGE" : "RED");
  $("ovDlg").showModal();
});

$("ovDlg").addEventListener("close", async () => {
  if ($("ovDlg").returnValue !== "confirm") return;
  busy(true);
  try {
    render(await api("/api/override", {
      clinician: $("ovWho").value.trim(),
      category: chosen,
      reason: $("ovReason").value,
      note: $("ovNote").value.trim(),
    }));
  } catch (e) {
    // The ledger refuses an unattributable override; show its actual reason.
    $("ovErr").textContent = e.message;
    $("ovErr").hidden = false;
    $("ovDlg").showModal();
  } finally { busy(false); }
});

// ---------------------------------------------------------------- voice
// Same rule as the kiosk: voice only ever fills a text box that typing could
// have filled too, nothing is auto-submitted. Nurse-facing errors show the
// real detail (as a hover tooltip on the button) rather than the kiosk's
// deliberately vague wording -- staff already see raw tier/model detail
// elsewhere on this screen.
function _voiceErr(button) {
  return (message) => { button.title = message; console.warn("voice:", message); };
}
// The patient's language, not the nurse's: a nurse dictating a Hindi
// patient's words records them in Hindi and the server translates, exactly as
// it would at the kiosk. Everything else a nurse dictates stays English.
const _patientLang = () => (state && state.language) || "en-IN";

attachVoiceInput($("voiceNarrative"), $("narrative"),
  { getLang: _patientLang, onError: _voiceErr($("voiceNarrative")) });
attachClearButton($("clearNarrative"), $("narrative"));

attachVoiceInput($("voiceReply"), $("reply"),
  { getLang: _patientLang, onError: _voiceErr($("voiceReply")) });
attachClearButton($("clearReply"), $("reply"));

$("p_lang").addEventListener("change", async () => {
  busy(true);
  try {
    render(await api("/api/language", { language: $("p_lang").value }));
    if (nextStep) await askNext();   // a question already on screen is re-worded; none is started
  } catch (e) { alert(e.message); } finally { busy(false); }
});

attachVoiceInput($("voiceOvNote"), $("ovNote"), { onError: _voiceErr($("voiceOvNote")) });
attachClearButton($("clearOvNote"), $("ovNote"));

attachVoiceInput($("voiceName"), $("p_name"), { onError: _voiceErr($("voiceName")) });
attachClearButton($("clearName"), $("p_name"));

attachVoiceInput($("voiceSex"), $("p_sex"), { onError: _voiceErr($("voiceSex")), parse: parseSpokenSex });
attachClearButton($("clearSex"), $("p_sex"));

attachVoiceInput($("voiceAge"), $("p_age"), { onError: _voiceErr($("voiceAge")), parse: parseSpokenNumber });
attachClearButton($("clearAge"), $("p_age"));

attachVoiceInput($("voiceAgeMonths"), $("p_age_months"),
  { onError: _voiceErr($("voiceAgeMonths")), parse: parseSpokenNumber });
attachClearButton($("clearAgeMonths"), $("p_age_months"));

attachVoiceInput($("voiceId"), $("p_id"), { onError: _voiceErr($("voiceId")) });
attachClearButton($("clearId"), $("p_id"));

attachPhotoCapture($("btnTakePhoto"), $("btnUploadPhoto"), $("p_photo_upload"), async (fileOrBlob) => {
  busy(true);
  try {
    const blob = await resizeImage(fileOrBlob);
    await uploadPhoto(PATIENT_REF, blob);
    await refresh();
  } catch (e) { alert(e.message); } finally { busy(false); }
});

// ---------------------------------------------------------------- boot

(async function boot() {
  TARGET = await api("/api/targets");
  // Only what the server can actually do: English alone until a Sarvam key is set.
  const { languages } = await api("/api/languages");
  $("p_lang").innerHTML = languages.map(l =>
    `<option value="${l.code}">${l.english}${l.code === "en-IN" ? "" : " · " + l.native}</option>`).join("");
  const checks = await api("/api/checks");
  $("obsCheck").innerHTML = checks.map(c =>
    `<option value="${c.id}">${c.text} — ${c.source}</option>`).join("");

  // Resume the patient this tab was already on (a reload shouldn't strand
  // an in-progress interview) if the URL names one and the server still
  // recognises it; otherwise this tab is a fresh kiosk session.
  const urlRef = new URL(window.location).searchParams.get("patient");
  if (urlRef) {
    PATIENT_REF = urlRef;
    try {
      await refresh();
    } catch {
      // api() already recovered onto a new patient on a no_such_patient
      // error (stale ref, e.g. after a server restart) -- nothing further
      // to do here, refresh() was already re-run as part of that recovery.
    }
  } else {
    await startNewPatient();
  }
  setInterval(refresh, 20000);   // the queue clock keeps moving
})();
