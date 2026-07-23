// ui_static/js/advanced_automation.js

// ----- tiny helpers -----
const $ = (s, r = document) => r.querySelector(s);
const create = (t, c) => { const el = document.createElement(t); if (c) el.className = c; return el; };

// ----- module state (set per-modal in init) -----
let currentSwitchId = "";
let automations = [];
let selectedId = null;
let sensorDirectory = [];
let switchLabels = {};
let switchChannelIds = {};
let switchChannels = 1;
let actorDirectory = [];
let emailActorEnabled = false;
let astralStatus = { ok: false, message: "" };

// ----- API helpers -----
async function fetchJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (!r.ok) throw new Error(`${url} ${r.status}`);
  return await r.json();
}

async function fetchSensorDirectory() {
  return await fetchJSON("/sensor-directory");
}

async function fetchSensorMetrics(sensorId) {
  const data = await fetchJSON(`/sensor-metrics?sensor_id=${encodeURIComponent(sensorId)}`);
  return Object.keys(data || {});
}

async function fetchSwitchInfo() {
  return await fetchJSON(`/switch-info?switch_id=${encodeURIComponent(currentSwitchId)}`);
}

async function fetchAdvancedAutomations() {
  const data = await fetchJSON("/advanced/automations?switch_id=__all__");
  return data.items || [];
}

async function fetchAutomationContext() {
  return await fetchJSON("/automation-context");
}

async function enableAutomation(ruleId, enabled) {
  const fd = new FormData();
  fd.append("switch_id", currentSwitchId);
  fd.append("rule_id", ruleId);
  fd.append("enabled", enabled ? "true" : "false");
  const resp = await fetch("/advanced/automations/enable", { method: "POST", body: fd });
  if (!resp.ok) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`Enable failed (${resp.status}) ${txt}`.trim());
  }
}

async function deleteAutomation(ruleId) {
  const fd = new FormData();
  fd.append("switch_id", currentSwitchId);
  fd.append("rule_id", ruleId);
  const resp = await fetch("/advanced/automations/delete", { method: "POST", body: fd });
  if (!resp.ok) {
    const txt = await resp.text().catch(() => "");
    throw new Error(`Delete failed (${resp.status}) ${txt}`.trim());
  }
}

// Returns the Switch Settings modal reliably from any descendant/backdrop.
function getModalRoot(fromEl) {
  return (
    (fromEl && fromEl.closest && fromEl.closest("#switchSettingsModal")) ||
    (fromEl && fromEl.closest && fromEl.closest("#setupPiModal")) ||
    (fromEl && fromEl.querySelector && fromEl.querySelector("#switchSettingsModal")) ||
    (fromEl && fromEl.querySelector && fromEl.querySelector("#setupPiModal")) ||
    document.querySelector("#setupPiModal") ||
    document.querySelector("#switchSettingsModal")
  );
}

// Wait until a selector exists under root (polls per animation frame). Returns the element or null on timeout.
async function waitForSelector(root, selector, timeoutMs = 2000) {
  const start = performance.now();
  for (;;) {
    const modal = getModalRoot(root) || root || document;
    const el = modal.querySelector(selector);
    if (el) return el;
    if (performance.now() - start > timeoutMs) {
      console.warn(`[AdvancedAutomation] waitForSelector timeout: ${selector}`);
      return null; // <-- do not throw
    }
    await new Promise(r => requestAnimationFrame(r));
  }
}

// ----- UI helpers (scoped to current modal) -----
function q(root, sel){ return root ? root.querySelector(sel) : null; }

function clampOneDecimal(value, fallback = 0) {
  const n = Number.parseFloat(String(value ?? ""));
  if (!Number.isFinite(n)) return fallback;
  return Math.round(n * 10) / 10;
}

function getEnabledChannelIndexSet(modal) {
  return new Set(
    String(modal?.dataset?.channelIndices || "")
      .split(",")
      .map(s => Number.parseInt(String(s).trim(), 10))
      .filter(n => Number.isFinite(n) && n > 0)
  );
}

function getActionOptionEntries(modal) {
  if (actorDirectory.length) {
    return actorDirectory.map((item, idx) => ({
      idx,
      lab: String(item.display || item.label || item.value || "").trim(),
      value: String(item.value || "").trim(),
    })).filter(item => item.lab && item.value);
  }
  const enabledIndexSet = getEnabledChannelIndexSet(modal);
  return Object.entries(switchLabels || {})
    .map(([idx, rawLabel]) => {
      const lab = String(rawLabel || "").trim();
      const channelId = String((switchChannelIds || {})[idx] || "").trim();
      return {
        idx: Number.parseInt(idx, 10),
        lab,
        channelId,
        value: `${currentSwitchId}::${channelId || lab}`,
      };
    })
    .filter(x => Number.isFinite(x.idx) && x.idx > 0 && x.lab)
    .filter(x => !enabledIndexSet.size || enabledIndexSet.has(x.idx))
    .sort((a, b) => a.idx - b.idx);
}

function fillActionSwitchOptions(selectEl, modal, preferredValue = "") {
  if (!selectEl) return;

  const entries = getActionOptionEntries(modal);
  selectEl.innerHTML = "";

  if (!entries.length) {
    if (!emailActorEnabled) {
      const opt = create("option");
      opt.value = "";
      opt.textContent = "No labeled channels";
      opt.disabled = true;
      opt.selected = true;
      selectEl.appendChild(opt);
      return;
    }
  }

  for (const { lab, value, channelId } of entries) {
    const opt = create("option");
    opt.value = value || `${currentSwitchId}::${channelId || lab}`;
    opt.textContent = lab;
    selectEl.appendChild(opt);
  }
  if (emailActorEnabled) {
    const opt = create("option");
    opt.value = "notify";
    opt.textContent = "Notify";
    selectEl.appendChild(opt);
  }

  if (!preferredValue) return;

  if ([...selectEl.options].some(o => o.value === preferredValue)) {
    selectEl.value = preferredValue;
    return;
  }

  const preferredText = String(preferredValue).trim();
  const delimIdx = preferredText.indexOf("::");
  const suffix = delimIdx >= 0 ? preferredText.slice(delimIdx + 2) : preferredText;
  const suffixLower = String(suffix || "").trim().toLowerCase();

  const aliasOption = [...selectEl.options].find(o => {
    const optionSuffix = String(o.value || "").split("::").slice(1).join("::").trim().toLowerCase();
    return optionSuffix === suffixLower || String(o.textContent || "").trim().toLowerCase() === suffixLower;
  });
  if (aliasOption) {
    selectEl.value = aliasOption.value;
  }
}

function setAutomationView(modal, mode) {
  const chooser = q(modal, "#automationChooser");
  const editor = q(modal, "#automationEditorWrap");
  if (!chooser || !editor) return;
  const showChooser = mode === "chooser";
  chooser.hidden = !showChooser;
  editor.hidden = showChooser;
  chooser.style.display = showChooser ? "flex" : "none";
  editor.style.display = showChooser ? "none" : "block";
}

function updateAstralDependencyWarning(modal) {
  const box = q(modal, "#astralDependencyWarning");
  if (!box) return;
  const hasAstral = [...modal.querySelectorAll("#conditionsContainer .cond-group select")]
    .some(sel => String(sel?.value || "").trim().toLowerCase() === "astral");
  if (!hasAstral || astralStatus?.ok) {
    box.hidden = true;
    box.textContent = "";
    return;
  }
  box.hidden = false;
  box.textContent = String(
    astralStatus?.message ||
    "Astral location is not currently resolved. Astral automations will evaluate false until location/timezone is available."
  );
}

function renderList(rootLike) {
  const modal = getModalRoot(rootLike);
  if (!modal) { console.warn("[AdvancedAutomation] modal root missing in renderList"); return; }
  const list = modal.querySelector("#automationList");
  if (!list)  { console.warn("[AdvancedAutomation] #automationList not found"); return; }

  list.innerHTML = "";
  if (!automations.length) {
    const empty = create("div", "muted");
    empty.style.padding = ".6rem .75rem";
    empty.textContent = "No saved automations yet.";
    list.appendChild(empty);
    return;
  }

  automations.forEach(a => {
    const item = create("div", "list-item" + (a.id === selectedId ? " active" : ""));
    item.onclick = () => {
      selectedId = a.id;
      renderList(modal);
      loadSelectedIntoForm(modal);
      setAutomationView(modal, "editor");
    };
    const name  = create("div", "item-name");  name.textContent  = a.name || "(unnamed)";
    const badge = create("div", `item-badge ${a.enabled ? "enabled" : "disabled"}`);
    badge.textContent = a.enabled ? "Enabled" : "Disabled";
    item.append(name, badge);
    list.appendChild(item);
  });
}

function _pad2(num) {
  const n = Number.parseInt(String(num ?? ""), 10);
  if (!Number.isFinite(n)) return "00";
  return String(n).padStart(2, "0");
}

function _normalizeTwentyFourHourTime(value) {
  const text = String(value || "").trim();
  const match = text.match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return "00:00";
  const hour = Number.parseInt(match[1], 10);
  const minute = Number.parseInt(match[2], 10);
  if (!Number.isFinite(hour) || !Number.isFinite(minute)) return "00:00";
  if (hour < 0 || hour > 24) return "00:00";
  if (minute < 0 || minute > 59) return "00:00";
  if (hour === 24 && minute !== 0) return "00:00";
  return `${_pad2(hour)}:${_pad2(minute)}`;
}

function _timePartsFromTwentyFourHour(value) {
  const normalized = _normalizeTwentyFourHourTime(value);
  if (normalized === "24:00") {
    return { hour12: "12", minute: "00", period: "24" };
  }
  const [hourText, minuteText] = normalized.split(":");
  const hour24 = Number.parseInt(hourText, 10);
  const minute = _pad2(minuteText);
  const period = hour24 >= 12 ? "PM" : "AM";
  let hour12 = hour24 % 12;
  if (hour12 === 0) hour12 = 12;
  return { hour12: String(hour12), minute, period };
}

function _timePartsToTwentyFourHour(hour12, minute, period) {
  const periodText = String(period || "AM").trim().toUpperCase();
  if (periodText === "24") return "24:00";
  let hour = Number.parseInt(String(hour12 || "12"), 10);
  let mins = Number.parseInt(String(minute || "0"), 10);
  if (!Number.isFinite(hour) || hour < 1 || hour > 12) hour = 12;
  if (!Number.isFinite(mins) || mins < 0 || mins > 59) mins = 0;
  let hour24 = hour % 12;
  if (periodText === "PM") hour24 += 12;
  return `${_pad2(hour24)}:${_pad2(mins)}`;
}

function createAutomationTimePicker(className, value) {
  const parts = _timePartsFromTwentyFourHour(value);
  const picker = create("div", `automation-time-picker ${className}`.trim());

  const hourSel = create("select");
  hourSel.classList.add("time-hour");
  for (let i = 1; i <= 12; i += 1) {
    const opt = create("option");
    opt.value = String(i);
    opt.textContent = String(i);
    hourSel.appendChild(opt);
  }
  hourSel.value = parts.hour12;

  const minuteSel = create("select");
  minuteSel.classList.add("time-minute");
  for (let i = 0; i < 60; i += 1) {
    const opt = create("option");
    opt.value = _pad2(i);
    opt.textContent = _pad2(i);
    minuteSel.appendChild(opt);
  }
  minuteSel.value = parts.minute;

  const periodSel = create("select");
  periodSel.classList.add("time-period");
  periodSel.innerHTML = `
    <option value="AM">AM</option>
    <option value="PM">PM</option>
    <option value="24">24:00</option>`;
  periodSel.value = parts.period;

  const syncDisabled = () => {
    const disableClock = periodSel.value === "24";
    hourSel.disabled = disableClock;
    minuteSel.disabled = disableClock;
  };
  periodSel.addEventListener("change", syncDisabled);
  syncDisabled();

  picker.append(hourSel, minuteSel, periodSel);
  return picker;
}

function readAutomationTimePicker(group, className) {
  const picker = group.querySelector(`.automation-time-picker.${className}`);
  if (!picker) return "00:00";
  const hourSel = picker.querySelector(".time-hour");
  const minuteSel = picker.querySelector(".time-minute");
  const periodSel = picker.querySelector(".time-period");
  return _timePartsToTwentyFourHour(hourSel?.value, minuteSel?.value, periodSel?.value);
}

// ----- Conditions builder -----
function addCondition(modal, cond) {
  const container = q(modal, "#conditionsContainer");
  const initialType = (cond?.type || "sensor");
  const group = create("div", "cond-group");

  // Type
  const typeWrap = create("div");
  const typeLab = create("label");
  typeLab.textContent = "Type";
  const typeSel = create("select");
  typeSel.innerHTML = `
    <option value="sensor">sensor</option>
    <option value="time">time of day</option>
    <option value="astral">astral</option>
    <option value="timer">timer</option>
    <option value="or">or</option>`;
  typeSel.value = initialType;
  typeSel.style.width = "8rem";
  typeWrap.append(typeLab, typeSel);

  // Time inputs
  const startWrap = create("div");
  const startLab = create("label");
  startLab.textContent = "Start Time";
  const startPicker = createAutomationTimePicker("start-time", cond?.start || "00:00");
  startWrap.append(startLab, startPicker);

  const endWrap = create("div");
  const endLab = create("label");
  endLab.textContent = "Stop Time";
  const endPicker = createAutomationTimePicker("end-time", cond?.end || "00:00");
  endWrap.append(endLab, endPicker);

  // Day-of-week controls for time-of-day
  const dowWrap = create("div");
  dowWrap.style.display = "flex";
  dowWrap.style.flexWrap = "wrap";
  dowWrap.style.alignItems = "center";
  dowWrap.style.gap = "0.5rem";

  const dowLab = create("label");
  dowLab.textContent = "Days";

  const dowBox = create("div");
  dowBox.className = "dow-box";
  dowBox.style.display = "flex";
  dowBox.style.flexWrap = "wrap";
  dowBox.style.alignItems = "center";
  dowBox.style.gap = "0.5rem";

  const defaultDays = Array.isArray(cond?.days)
    ? cond.days
        .map(d => parseInt(d, 10))
        .filter(n => Number.isFinite(n) && n >= 0 && n <= 6)
    : [0, 1, 2, 3, 4, 5, 6];

  const dowLabels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
  dowLabels.forEach((name, idx) => {
    const cbLabel = create("label", "dow-pill");
    cbLabel.style.display = "inline-flex";
    cbLabel.style.alignItems = "center";

    const cb = create("input");
    cb.type = "checkbox";
    cb.classList.add("dow-checkbox");
    cb.dataset.day = String(idx);
    cb.checked = defaultDays.includes(idx);

    cbLabel.append(cb, document.createTextNode(" " + name));
    dowBox.appendChild(cbLabel);
  });
  dowWrap.append(dowLab, dowBox);

  // Astral controls
  const astralEventWrap = create("div");
  const astralEventLab = create("label");
  astralEventLab.textContent = "Event";
  const astralEventSel = create("select");
  astralEventSel.classList.add("astral-event");
  astralEventSel.innerHTML = `
    <option value="sunrise_to_sunset">sunrise to sunset (start & end)</option>
    <option value="sunset_to_sunrise">sunset to sunrise (start & end)</option>
    <option value="sunrise">sunrise (start)</option>
    <option value="sunset">sunset (start)</option>`;
  const astralRawEvent = String(cond?.astral_event || cond?.event || "sunrise").trim().toLowerCase();
  const astralKnownEvent = new Set(["sunrise_to_sunset", "sunset_to_sunrise", "sunrise", "sunset"]);
  astralEventSel.value = astralKnownEvent.has(astralRawEvent) ? astralRawEvent : "sunrise";
  astralEventWrap.append(astralEventLab, astralEventSel);

  const astralOffsetWrap = create("div");
  const astralOffsetLab = create("label");
  astralOffsetLab.textContent = "Offset (minutes)";
  const astralOffsetIn = create("input");
  astralOffsetIn.type = "number";
  astralOffsetIn.min = "-120";
  astralOffsetIn.max = "120";
  astralOffsetIn.step = "1";
  astralOffsetIn.classList.add("astral-offset");
  astralOffsetIn.value = cond?.offset_min ?? cond?.offset_minutes ?? 0;
  astralOffsetWrap.append(astralOffsetLab, astralOffsetIn);

  // Timer controls
  const timerDurWrap = create("div");
  timerDurWrap.style.display = "flex";
  timerDurWrap.style.flexDirection = "column";
  timerDurWrap.style.gap = "0.25rem";

  const timerDurLab  = create("label");
  timerDurLab.textContent = "Duration";

  const timerDurIn   = create("input");
  timerDurIn.type = "number";
  timerDurIn.min  = "1";
  timerDurIn.max  = "60";
  timerDurIn.step = "1";
  timerDurIn.value = cond?.duration_min ?? 5;
  timerDurIn.classList.add("timer-duration");
  timerDurIn.style.width = "4.5rem";

  timerDurWrap.append(timerDurLab, timerDurIn);

  const timerFreqWrap = create("div");
  timerFreqWrap.style.display = "flex";
  timerFreqWrap.style.flexDirection = "column";
  timerFreqWrap.style.gap = "0.25rem";

  const timerFreqLab  = create("label");
  timerFreqLab.textContent = "Every";

  const timerFreqSel  = create("select");
  timerFreqSel.classList.add("timer-frequency");
  timerFreqSel.innerHTML = `
    <option value="5">5 minutes</option>
    <option value="15">15 minutes</option>
    <option value="30">30 minutes</option>
    <option value="60">1 hour</option>
    <option value="180">3 hours</option>
    <option value="360">6 hours</option>
    <option value="720">12 hours</option>
    <option value="1440">24 hours</option>`;
  const timerPeriodMin = Number.parseInt(String(cond?.period_min ?? ((cond?.freq_hours || 1) * 60)), 10);
  timerFreqSel.value = String(Number.isFinite(timerPeriodMin) && timerPeriodMin > 0 ? timerPeriodMin : 60);
  timerFreqSel.style.width = "7rem";

  timerFreqWrap.append(timerFreqLab, timerFreqSel);

  // Sensor controls
  const sensorWrap = create("div");
  const sensorLab = create("label");
  sensorLab.textContent = "Sensor";
  const sensorSel = create("select");
  sensorSel.classList.add("sensor-select");
  sensorSel.innerHTML = "";
  if (sensorDirectory.length === 0) {
    const opt = create("option");
    opt.value = "";
    opt.textContent = "(no sensors)";
    opt.disabled = true;
    opt.selected = true;
    sensorSel.appendChild(opt);
  } else {
    for (const s of sensorDirectory) {
      const opt = create("option");
      opt.value = s.id;
      opt.textContent = (s.location && s.location !== "Unknown") ? `${s.id} @ ${s.location}` : s.id;
      sensorSel.appendChild(opt);
    }
    sensorSel.value = cond?.sensor || sensorDirectory[0]?.id || "";
  }
  sensorWrap.append(sensorLab, sensorSel);

  const metricWrap = create("div");
  const metricLab = create("label");
  metricLab.textContent = "Metric";
  const metricSel = create("select");
  metricSel.classList.add("metric-select");
  metricWrap.append(metricLab, metricSel);

  function refreshMetricOptions(){
    const sensorId = sensorSel.value;
    const found = sensorDirectory.find(s => s.id === sensorId);
    metricSel.innerHTML = "";
    const metrics = found?.metrics || [];
    if (metrics.length === 0) {
      const opt = create("option");
      opt.value = "";
      opt.textContent = "(no metrics)";
      opt.disabled = true;
      opt.selected = true;
      metricSel.appendChild(opt);
      return;
    }
    for (const m of metrics) {
      const opt = create("option");
      opt.value = m;
      opt.textContent = m;
      metricSel.appendChild(opt);
    }
    if (cond?.metric && metrics.includes(cond.metric)) metricSel.value = cond.metric;
  }

  const opWrap = create("div");
  const opLab = create("label");
  opLab.textContent = "Operator";
  const opSel = create("select");
  opSel.classList.add("op-select");
  opSel.innerHTML = `<option value=">">&gt;</option><option value="<">&lt;</option><option value="==">==</option><option value="!=">!=</option>`;
  opSel.value = cond?.op || ">";
  opWrap.append(opLab, opSel);

  const valueWrap = create("div");
  const valueLab = create("label");
  valueLab.textContent = "Threshold";
  const valueIn = create("input");
  valueIn.type = "number";
  valueIn.step = "1";
  valueIn.inputMode = "decimal";
  valueIn.value = cond?.value ?? 0;
  valueIn.addEventListener("blur", () => { valueIn.value = String(clampOneDecimal(valueIn.value, 0)); });
  valueWrap.append(valueLab, valueIn);

  const hystWrap = create("div");
  const hystLab = create("label");
  hystLab.textContent = "Hysteresis";
  const hystIn = create("input");
  hystIn.type = "number";
  hystIn.step = "1";
  hystIn.inputMode = "decimal";
  hystIn.value = cond?.hyst ?? 0;
  hystIn.addEventListener("blur", () => { hystIn.value = String(clampOneDecimal(hystIn.value, 0)); });
  hystWrap.append(hystLab, hystIn);

  const rem = create("button","remove");
  rem.type = "button";
  rem.setAttribute("aria-label","Remove condition");
  rem.textContent = "×";
  rem.title = "Remove condition";
  rem.onclick = () => { group.remove(); updateAstralDependencyWarning(modal); };

  const orChip = create("div","or-chip");
  orChip.textContent = "OR";

function renderAsTime() {
  group.innerHTML = "";

  // First row: Type + Start + End + Remove
  const row1 = create("div", "cond time");
  row1.append(typeWrap, startWrap, endWrap, rem);

  // Second row: Days (full width)
  const row2 = create("div", "cond time-days");
  row2.style.display = "block";
  row2.style.marginTop = "0.25rem";
  row2.append(dowWrap);

  group.append(row1, row2);
}

  function renderAsAstral() {
    group.innerHTML = "";

    const row = create("div", "cond astral");
    row.append(typeWrap, astralEventWrap, astralOffsetWrap, rem);

    const row2 = create("div", "cond astral-days");
    row2.style.display = "block";
    row2.style.marginTop = "0.25rem";
    row2.append(dowWrap);

    group.append(row, row2);
  }

  function renderAsTimer(){
    group.innerHTML = "";

    const row = create("div", "cond timer");
    row.append(typeWrap, timerFreqWrap, timerDurWrap, rem);
    group.appendChild(row);
  }

  function renderAsSensor(){
    group.innerHTML = "";
    const top = create("div", "cond sensor-top");
    top.append(typeWrap, sensorWrap);
    refreshMetricOptions();
    const bottom = create("div", "cond sensor-bottom");
    bottom.append(metricWrap, opWrap, valueWrap, hystWrap, rem);
    group.append(top, bottom);
  }

  function renderAsOr(){
    group.innerHTML = "";
    const row = create("div", "cond or");
    row.append(typeWrap, orChip, rem);
    group.appendChild(row);
  }

  if (initialType === "time")         renderAsTime();
  else if (initialType === "astral")  renderAsAstral();
  else if (initialType === "timer")   renderAsTimer();
  else if (initialType === "or")      renderAsOr();
  else                                renderAsSensor();

  sensorSel.addEventListener("change", refreshMetricOptions);
  typeSel.addEventListener("change", () => {
    if (typeSel.value === "time")         renderAsTime();
    else if (typeSel.value === "astral")  renderAsAstral();
    else if (typeSel.value === "timer")   renderAsTimer();
    else if (typeSel.value === "or")      renderAsOr();
    else                                  renderAsSensor();
    updateAstralDependencyWarning(modal);
  });

  container.appendChild(group);
  updateAstralDependencyWarning(modal);
}

// ----- Actions builder -----
function addAction(modal, action) {
  const container = q(modal, "#actionsContainer");
  if (!container) return;

  const group = create("div", "action-group");

  const actorWrap = create("div");
  const actorLab = create("label");
  actorLab.textContent = "Actors";
  const actorSel = create("select");
  actorSel.classList.add("action-switch", "action-actor");
  actorWrap.append(actorLab, actorSel);

  const setWrap = create("div");
  const setLab = create("label");
  setLab.textContent = "State";
  const setSel = create("select");
  setSel.classList.add("action-set");
  setSel.innerHTML = `
    <option value="on">On</option>
    <option value="off">Off</option>`;
  setSel.value = action?.set ? "on" : "off";
  setWrap.append(setLab, setSel);

  const revertWrap = create("div");
  const revertLab = create("label");
  revertLab.textContent = "Revert Action";
  const revertSel = create("select");
  revertSel.classList.add("action-revert");
  revertSel.innerHTML = `
    <option value="previous_state">Previous State</option>
    <option value="do_nothing">Do Nothing</option>`;
  const revertValue = (typeof action?.revert_action === "string" && action.revert_action.trim())
    ? action.revert_action.trim()
    : "previous_state";
  revertSel.value = revertValue === "do_nothing" ? "do_nothing" : "previous_state";
  revertWrap.append(revertLab, revertSel);

  const delayWrap = create("div");
  const delayLab = create("label");
  delayLab.textContent = "Delay Before Action (secs)";
  const delayIn = create("input");
  delayIn.type = "number";
  delayIn.min = "0";
  delayIn.max = "60";
  delayIn.step = "1";
  delayIn.classList.add("action-delay");
  delayIn.title = "Wait this many seconds after the rule becomes true before applying the action.";
  const actionDelay = parseInt(String(action?.delay_s ?? "0"), 10);
  delayIn.value = String(Number.isFinite(actionDelay) ? Math.max(0, Math.min(60, actionDelay)) : 0);
  delayWrap.append(delayLab, delayIn);

  const toWrap = create("div");
  const toLab = create("label");
  toLab.textContent = "To";
  const toIn = create("input");
  toIn.type = "email";
  toIn.classList.add("action-notify-to");
  toIn.placeholder = "recipient@example.com";
  toIn.value = String(action?.to || "").trim();
  toWrap.append(toLab, toIn);

  const rem = create("button", "remove");
  rem.type = "button";
  rem.setAttribute("aria-label", "Remove action");
  rem.textContent = "×";
  rem.title = "Remove action";
  rem.onclick = () => {
    const groups = [...container.querySelectorAll(".action-group")];
    if (groups.length <= 1) {
      return;
    }
    group.remove();
  };

  const row = create("div", "action-row");
  row.append(actorWrap, setWrap, revertWrap, delayWrap, toWrap, rem);
  group.appendChild(row);
  container.appendChild(group);

  const preferredActor = String(
    action?.type === "notify" ? "notify" : (action?.switch_key || "")
  ).trim();
  fillActionSwitchOptions(actorSel, modal, preferredActor);
  const syncActorFields = () => {
    const isNotify = actorSel.value === "notify";
    row.classList.toggle("notify-action", isNotify);
    setWrap.hidden = isNotify;
    revertWrap.hidden = isNotify;
    delayWrap.hidden = isNotify;
    toWrap.hidden = !isNotify;
  };
  actorSel.addEventListener("change", syncActorFields);
  syncActorFields();
}

// ----- Form helpers -----
function loadSelectedIntoForm(rootLike){
  const modal = getModalRoot(rootLike);
  if (!modal) { console.warn("[AdvancedAutomation] modal root missing in loadSelectedIntoForm"); return; }

  const a = automations.find(x => x.id === selectedId);
  const auto = a || {
    id: selectedId || `auto-${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`,
    name: "", enabled:false, conditions: [], actions: []
  };

  const nameEl   = modal.querySelector("#autoName");
  const enEl     = modal.querySelector("#autoEnabled");
  const box      = modal.querySelector("#conditionsContainer");
  const actionsBox = modal.querySelector("#actionsContainer");

  if (!nameEl || !enEl || !box || !actionsBox) {
    console.warn("[AdvancedAutomation] form elements missing; skipping loadSelectedIntoForm");
    return;
  }

  nameEl.value      = auto.name || "";
  enEl.value        = auto.enabled === false ? "false" : "true";

  box.innerHTML = "";
  if ((auto.conditions||[]).length === 0) addCondition(modal,{type:"sensor"});
  else auto.conditions.forEach(c => addCondition(modal,c));

  actionsBox.innerHTML = "";
  const normalizedActions = Array.isArray(auto.actions) ? auto.actions : (auto.actions ? [auto.actions] : []);
  if (!normalizedActions.length) addAction(modal, {});
  else normalizedActions.forEach(a => addAction(modal, a));

  updateAstralDependencyWarning(modal);
}

function serializeForm(modal){
  const name = q(modal,"#autoName").value.trim();
  const enabled = q(modal,"#autoEnabled").value !== "false";

  const groups = [...modal.querySelectorAll("#conditionsContainer .cond-group")];
  const conditions = groups.map(group => {
    const typeVal = group.querySelector("select")?.value || "sensor";

    if (typeVal === "time"){
      const start = readAutomationTimePicker(group, "start-time");
      const end   = readAutomationTimePicker(group, "end-time");

      const dayChecks = group.querySelectorAll(".time input.dow-checkbox");
      const days = Array.from(dayChecks)
        .filter(cb => cb.checked)
        .map(cb => parseInt(cb.dataset.day || "0", 10))
        .filter(n => Number.isFinite(n) && n >= 0 && n <= 6);

      return { type:"time", start, end, days };
    } else if (typeVal === "astral") {
      const eventSel = group.querySelector(".astral select.astral-event");
      const offsetIn = group.querySelector(".astral input.astral-offset");
      const eventRaw = String(eventSel?.value || "sunrise").trim().toLowerCase();
      const event = ["sunrise_to_sunset", "sunset_to_sunrise", "sunrise", "sunset"].includes(eventRaw)
        ? eventRaw
        : "sunrise";

      let offsetMin = parseInt(offsetIn?.value || "0", 10);
      if (!Number.isFinite(offsetMin)) offsetMin = 0;
      offsetMin = Math.max(-120, Math.min(120, offsetMin));

      const dayChecks = group.querySelectorAll(".dow-checkbox");
      const days = Array.from(dayChecks)
        .filter(cb => cb.checked)
        .map(cb => parseInt(cb.dataset.day || "0", 10))
        .filter(n => Number.isFinite(n) && n >= 0 && n <= 6);

      return { type:"astral", astral_event: event, offset_min: offsetMin, days };
    } else if (typeVal === "timer") {
      const durInput = group.querySelector(".timer-duration");
      const freqSel  = group.querySelector(".timer-frequency");

      let duration = parseInt(durInput?.value || "1", 10);
      if (!Number.isFinite(duration) || duration < 1) duration = 1;
      if (duration > 60) duration = 60;

      let periodMin = parseInt(freqSel?.value || "60", 10);
      if (!Number.isFinite(periodMin) || periodMin <= 0) periodMin = 60;
      if (duration >= periodMin) {
        throw new Error(`Timer duration must be less than Every (${periodMin} minutes).`);
      }

      const condition = { type:"timer", duration_min: duration, period_min: periodMin };
      if (periodMin >= 60 && periodMin % 60 === 0) {
        condition.freq_hours = periodMin / 60;
      }
      return condition;
    } else if (typeVal === "or"){
      return { type:"or" };
    } else {
      const sensor = group.querySelector(".sensor-top select.sensor-select")?.value || "";
      const metric = group.querySelector(".sensor-bottom select.metric-select")?.value || "";
      const op     = group.querySelector(".sensor-bottom select.op-select")?.value || ">";
      const nums   = [...group.querySelectorAll(".sensor-bottom input[type='number']")];
      const value  = clampOneDecimal(nums[0]?.value || "0", 0);
      const hyst   = clampOneDecimal(nums[1]?.value || "0", 0);
      return { type:"sensor", sensor, metric, op, value, hyst };
    }
  });

  const actionGroups = [...modal.querySelectorAll("#actionsContainer .action-group")];
  const actions = actionGroups.map(group => {
    const switchKey = String(group.querySelector(".action-actor")?.value || "").trim();
    if (switchKey === "notify") {
      return {
        type: "notify",
        to: String(group.querySelector(".action-notify-to")?.value || "").trim(),
        executor_switch_id: currentSwitchId,
      };
    }
    const set = group.querySelector(".action-set")?.value === "on";
    const revertAction = group.querySelector(".action-revert")?.value === "do_nothing"
      ? "do_nothing"
      : "previous_state";
    const delayRaw = parseInt(group.querySelector(".action-delay")?.value || "0", 10);
    const delay_s = Number.isFinite(delayRaw) ? Math.max(0, Math.min(60, delayRaw)) : 0;
    return { type: "switch", switch_key: switchKey, set, revert_action: revertAction, delay_s };
  }).filter(a => a.type === "notify" ? !!a.to : !!a.switch_key);

  if (!actions.length) {
    const fallback = q(modal, "#actionsContainer .action-actor");
    const fallbackKey = String(fallback?.value || "").trim();
    if (fallbackKey === "notify") {
      throw new Error("A To email address is required for the Notify actor.");
    }
    if (fallbackKey) {
      actions.push({ switch_key: fallbackKey, set: true, revert_action: "previous_state", delay_s: 0 });
    }
  }

  return {
    id: selectedId || `auto-${crypto.randomUUID?.() || Math.random().toString(36).slice(2)}`,
    name, enabled, conditions,
    actions
  };
}

// ----- Data loaders -----
async function loadSensors(){
  const entries = await fetchSensorDirectory();
  sensorDirectory = await Promise.all((entries || []).map(async item => {
    const id = String(item?.id || "").trim();
    const location = String(item?.location || "").trim();
    const label = String(item?.label || location || id).trim() || id;
    return {
      id,
      label,
      location,
      metrics: await fetchSensorMetrics(id).catch(() => []),
    };
  }));
}

async function loadSwitchInfoInto(rootLike) {
  const modal = getModalRoot(rootLike);
  if (!modal) { console.warn("[AdvancedAutomation] modal root missing in loadSwitchInfoInto"); return; }

  if (modal.querySelector("[data-automation-scope='system']")) {
    const context = await fetchAutomationContext().catch(err => {
      console.warn("[AdvancedAutomation] automation context failed:", err);
      return { actors: [], email_enabled: false, executor_switch_id: "" };
    });
    actorDirectory = Array.isArray(context.actors) ? context.actors : [];
    emailActorEnabled = !!context.email_enabled;
    currentSwitchId = String(context.executor_switch_id || "").trim();
    if (!currentSwitchId && actorDirectory.length) {
      currentSwitchId = String(actorDirectory[0]?.switch_id || "").trim();
    }
    const info = currentSwitchId
      ? await fetchSwitchInfo().catch(() => ({}))
      : {};
    astralStatus = info.astral_status || { ok: false, message: "" };
    modal.querySelectorAll("#actionsContainer .action-actor").forEach(sel => {
      fillActionSwitchOptions(sel, modal, sel.value);
    });
    updateAstralDependencyWarning(modal);
    return;
  }

  actorDirectory = [];
  emailActorEnabled = false;
  const info = await fetchSwitchInfo().catch(err => {
    console.warn("[AdvancedAutomation] fetchSwitchInfo failed:", err);
    return { labels:{}, channels:1 };
  });

  switchLabels = info.labels || {};
  switchChannelIds = info.channel_ids || {};
  switchChannels = info.channels || 1;
  astralStatus = info.astral_status || { ok: false, message: "" };

  if (!Object.keys(switchLabels).length) {
    const fromForm = {};
    const fields = modal.querySelectorAll("input[name^='SWITCH_'][name$='_LABEL'], input[id^='SWITCH_'][id$='_LABEL']");
    for (const el of fields) {
      const key = (el.name || el.id || "").toUpperCase();
      const m = key.match(/^SWITCH_(\d+)_LABEL$/);
      if (!m) continue;
      const idx = Number.parseInt(m[1], 10);
      const text = String(el.value || "").trim();
      if (Number.isFinite(idx) && idx > 0 && text) fromForm[idx] = text;
    }
    if (Object.keys(fromForm).length) {
      switchLabels = fromForm;
      switchChannelIds = {};
      switchChannels = Math.max(switchChannels || 1, ...Object.keys(fromForm).map(n => Number.parseInt(n, 10)));
    }
  }

  const actionSelectors = [...modal.querySelectorAll("#actionsContainer .action-switch")];
  updateAstralDependencyWarning(modal);
  if (!actionSelectors.length) return;
  actionSelectors.forEach(sel => fillActionSwitchOptions(sel, modal, sel.value));
}

async function loadAutomationsListInto(rootLike, opts = {}) {
  const modal = getModalRoot(rootLike);
  if (!modal) { console.warn("[AdvancedAutomation] modal root missing in loadAutomationsListInto"); return; }
  const prevSelectedId = selectedId;

  const items = await fetchAdvancedAutomations().catch(err => {
    console.warn("[AdvancedAutomation] fetchAdvancedAutomations failed:", err);
    return [];
  });

  automations = (items || []).map(it => {
    let parsed = {};
    try { parsed = JSON.parse(it.script_json || "{}"); } catch {}
    const parsedActions = Array.isArray(parsed.actions) ? parsed.actions : (parsed.actions ? [parsed.actions] : []);
    return {
      id: it.rule_id,
      name: parsed.name || it.rule_id,
      enabled: !!it.enabled,
      conditions: parsed.conditions || [],
      actions: parsedActions.map(action => ({
        ...action,
        revert_action: (typeof action?.revert_action === "string" && action.revert_action.trim())
          ? action.revert_action.trim()
          : "do_nothing"
      }))
    };
  });

  if (opts.preserveSelection && prevSelectedId && automations.some(a => a.id === prevSelectedId)) {
    selectedId = prevSelectedId;
  } else {
    selectedId = null;
  }

  renderList(modal);

  if (!automations.length) {
    loadSelectedIntoForm(modal);
    setAutomationView(modal, "editor");
    return;
  }

  if (opts.openEditor && selectedId) {
    loadSelectedIntoForm(modal);
    setAutomationView(modal, "editor");
    return;
  }

  setAutomationView(modal, "chooser");
}

window.refreshAdvancedAutomationModal = async function(rootLike) {
  const modal = getModalRoot(rootLike);
  if (!modal) return false;
  await loadSwitchInfoInto(modal);
  await loadAutomationsListInto(modal);
  return true;
};

// ----- Public save/delete wired to backend -----
async function saveCurrent(modal){
  const saveBtn = modal ? modal.querySelector("#btnSetAutomation") : null;
  const statusEl = modal ? modal.querySelector("#automationSaveStatus") : null;
  if (saveBtn) {
    if (!saveBtn.dataset.baseLabel) saveBtn.dataset.baseLabel = saveBtn.textContent || "Save";
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
  }
  if (statusEl) statusEl.textContent = "Saving...";
  const doc = serializeForm(modal);
  const payload = {
    switch_id: currentSwitchId,
    rule_id: doc.id,
    enabled: doc.enabled ? "true" : "false",
    script_json: JSON.stringify({
      name: doc.name,
      enabled: doc.enabled,
      conditions: doc.conditions,
      actions: doc.actions
    })
  };
  const res = await fetch("/submit-advanced-trigger", {
    method: "POST",
    headers: {"Content-Type":"application/json"},
    body: JSON.stringify(payload)
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(String(body.error || body.message || "Save failed"));
  }
  const saved = await res.json().catch(() => ({}));
  const savedRuleId = String(saved?.rule_id || doc.id || "").trim() || doc.id;
  const nextDoc = { ...doc, id: savedRuleId };
  const existingIndex = automations.findIndex(item => item.id === savedRuleId);
  if (existingIndex >= 0) automations[existingIndex] = nextDoc;
  else automations.push(nextDoc);
  selectedId = savedRuleId;
  renderList(modal);
  loadSelectedIntoForm(modal);
  await loadAutomationsListInto(modal, { preserveSelection: true, openEditor: true });
  if (typeof window.refreshAndApplySwitchStatus === "function") {
    window.setTimeout(() => window.refreshAndApplySwitchStatus().catch?.(() => {}), 0);
  }
  if (typeof window.scheduleSwitchStatusRefreshes === "function") {
    window.scheduleSwitchStatusRefreshes([1500, 6000, 12000]);
  }
  if (statusEl) statusEl.textContent = "Saved.";
  if (typeof window.showToast === "function") window.showToast("Automation saved", "ok");
  if (saveBtn) {
    saveBtn.disabled = false;
    saveBtn.textContent = saveBtn.dataset.baseLabel || "Save";
  }
}

async function deleteSelected(modal){
  if (!selectedId) return;
  await deleteAutomation(selectedId);
  selectedId = null;
  await loadAutomationsListInto(modal);
}

// ----- Expose one init entry point -----
window.initAdvancedAutomationModal = async function (modalEl) {
  try {
    if (!modalEl) { console.error("initAdvancedAutomationModal: missing modalEl"); return false; }

    // Prefer backdrop as the wider scope; get the inner modal root early
    const scope = modalEl.closest(".modal-backdrop") || modalEl;
    const modalRoot = getModalRoot(modalEl) || modalEl;

    // Switch id from either element
    currentSwitchId =
      modalEl.dataset?.switchId ||
      scope.dataset?.switchId ||
      "";
    const isSystemEditor = !!modalRoot.querySelector("[data-automation-scope='system']");
    if (!currentSwitchId && !isSystemEditor) {
      console.error("initAdvancedAutomationModal: missing data-switch-id");
      return false;
    }

    // Ensure DOM is attached and subtree exists before queries
    await Promise.resolve();
    await new Promise(r => requestAnimationFrame(r));

    // Wait (non-throwing) for the critical elements inside the modal
    const elList   = await waitForSelector(modalRoot, "#automationList",       2000);
    const elActs   = await waitForSelector(modalRoot, "#actionsContainer",     2000);
    const elName   = await waitForSelector(modalRoot, "#autoName",             2000);
    const elConds  = await waitForSelector(modalRoot, "#conditionsContainer",  2000);

    // If essentials are missing, surface but don't throw
    if (!elList || !elActs || !elName || !elConds) {
      console.warn("[AdvancedAutomation] essential nodes missing; modal will still open but UI may be incomplete", {
        hasList: !!elList, hasActions: !!elActs, hasName: !!elName, hasConds: !!elConds
      });
    }

    // ---- wire buttons (use modalRoot; fall back to scope) ----
    const rootForQuery = modalRoot || scope;
    const btnNew  = rootForQuery.querySelector("#btnNewAutomation");
    const btnNewFromList = rootForQuery.querySelector("#btnNewFromList");
    const btnAdd  = rootForQuery.querySelector("#btnAddCondition");
    const btnAddAction = rootForQuery.querySelector("#btnAddAction");
    const btnSave = rootForQuery.querySelector("#btnSetAutomation");
    const btnDel  = rootForQuery.querySelector("#btnRemove");
    const btnSavedAutomations = rootForQuery.querySelector("#btnSavedAutomations");

    const startNewAutomation = () => {
      selectedId = `auto-${(crypto.randomUUID?.() || Math.random().toString(36).slice(2))}`;
      renderList(modalRoot);
      loadSelectedIntoForm(modalRoot);
      setAutomationView(modalRoot, "editor");
    };

    if (btnNew) btnNew.onclick = startNewAutomation;
    if (btnNewFromList) btnNewFromList.onclick = startNewAutomation;
    if (btnAdd)  btnAdd.onclick  = () => addCondition(modalRoot, { type:"sensor" });
    if (btnAddAction) btnAddAction.onclick = () => addAction(modalRoot, {});
    if (btnSave) btnSave.onclick = () => saveCurrent(modalRoot).catch((e) => {
      const statusEl = modalRoot.querySelector("#automationSaveStatus");
      const msg = e && e.message ? e.message : "Save failed";
      if (statusEl) statusEl.textContent = msg;
      if (typeof window.showToast === "function") window.showToast(msg, "error");
      if (btnSave) {
        btnSave.disabled = false;
        btnSave.textContent = btnSave.dataset.baseLabel || "Save";
      }
    });
    if (btnDel)  btnDel.onclick  = () => deleteSelected(modalRoot);
    if (btnSavedAutomations) btnSavedAutomations.onclick = () => setAutomationView(modalRoot, "chooser");

    // ---- load the critical UI path first (fast): switch info + saved automations ----
    await loadSwitchInfoInto(modalRoot);
    if (!currentSwitchId) {
      const statusEl = modalRoot.querySelector("#automationSaveStatus");
      if (statusEl) statusEl.textContent = "Add a switch before creating automations.";
    }
    await loadAutomationsListInto(modalRoot);

    // ---- load sensors in background so slow sensor endpoints don't block the list ----
    Promise.resolve()
      .then(() => loadSensors())
      .then(() => {
        // If editor is visible, refresh the form so sensor/metric selectors get populated.
        const chooser = q(modalRoot, "#automationChooser");
        const editorVisible = !!chooser && chooser.hidden;
        if (editorVisible) {
          loadSelectedIntoForm(modalRoot);
        }
      })
      .catch(e => console.warn("[AdvancedAutomation] loadSensors failed:", e));

    // ---- finally show (parent sets display:none initially) ----
    const backdrop = modalRoot.closest?.(".modal-backdrop") || scope.closest?.(".modal-backdrop") || scope;
    backdrop.style.display = "flex";

    return true;
  } catch (err) {
    console.error("initAdvancedAutomationModal failed:", err);
    return false; // never throw to caller
  }
};
