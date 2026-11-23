const element = (tag, className, text) => {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};
const button = (text, color = 'black') => {
  const node = element('button', `button ${color}`, text);
  node.type = 'button';
  return node;
};
async function json(url, options) {
  const response = await fetch(url, {cache: 'no-store', ...options});
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `Unable to load ${url}`);
  return body;
}
let fieldNumber = 0;
function field(label, input) {
  const node = element('label', 'library-field');
  const caption = element('span', '', label);
  caption.id = `automation-library-field-${++fieldNumber}`;
  input.setAttribute('aria-labelledby', caption.id);
  if (input.type === 'checkbox') {
    node.classList.add('library-checkbox');
    node.append(input, caption);
  } else node.append(caption, input);
  return node;
}
function select(options, value = '') {
  const input = element('select');
  options.forEach(([key, label]) => input.add(new Option(label, key)));
  input.value = value;
  return input;
}
function newBatch() {
  return crypto.randomUUID?.() || 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, char => {
    const random = Math.floor(Math.random() * 16);
    return (char === 'x' ? random : (random & 3) | 8).toString(16);
  });
}

export async function openAutomationLibrary(modal, onImported) {
  modal.querySelector('#automationLibrary')?.remove();
  const chooser = modal.querySelector('#automationChooser');
  const panel = element('div', 'automation-library');
  panel.id = 'automationLibrary';
  chooser.hidden = true;
  chooser.style.display = 'none';
  modal.querySelector('#pane-automations').append(panel);
  const close = () => {
    panel.remove();
    chooser.hidden = false;
    chooser.style.display = 'flex';
  };
  panel.append(element('p', '', 'Loading automation library…'));
  let data;
  try {
    data = await json('/advanced/automation-library');
  } catch (error) {
    panel.replaceChildren(element('p', 'sai-live-status', error.message));
    const back = button('Saved Automations');
    back.onclick = close;
    panel.append(back);
    return;
  }
  const selected = new Map();
  let batch = null;
  let destinationGroup = null;
  let dryingStartedAt = '';
  let plantings = [];
  let bindings = [];
  let warning = '';
  const results = await Promise.allSettled([
    json('/api/biodynamic-calendar-app/plantings'),
    json('/sensor-directory').then(async sensors => {
      const directories = await Promise.all(sensors.map(async sensor => {
        const metrics = await json(`/sensor-metrics?sensor_id=${encodeURIComponent(sensor.id)}&input_units=true`);
        return Object.entries(metrics).map(([metric, descriptor]) => ({
          sensor: sensor.id, metric, unit: descriptor.unit || descriptor.native_unit || '',
          label: `${sensor.label || sensor.location || sensor.id} / ${metric} (${descriptor.unit || descriptor.native_unit || 'native units'})`,
        }));
      }));
      return directories.flat();
    }),
  ]);
  if (results[0].status === 'fulfilled') plantings = results[0].value.plantings || [];
  else warning += 'Planting links are unavailable. ';
  if (results[1].status === 'fulfilled') bindings = results[1].value;
  else warning += 'Sensor bindings could not be loaded; reopen the library to retry. ';
  const filters = {environment: 'Greenhouse', part: '', crop: '', stage: 'seedling'};
  const keyFor = (crop, stage, template) => `${crop.id}:${stage}:${template.id}`;
  const stagesFor = crop => crop.recipe
    ? Object.entries(crop.recipe).map(([key, stage]) => [key, stage.name])
    : Object.entries(data.stages);
  const heading = () => {
    panel.replaceChildren(element('h3', 'pane-title', 'Automation Library'));
    panel.append(element('p', 'muted', 'Choose a starter or recipe. Review targets, sensor bindings, and schedules before enabling imported Alert rules.'));
    if (warning) panel.append(element('p', 'sai-live-status', warning));
  };
  const footer = (...controls) => {
    const node = element('div', 'pane-footer pane-footer-actions-right');
    node.append(...controls);
    panel.append(node);
  };
  function renderBrowse() {
    heading();
    const bar = element('div', 'library-filters library-browse-filters');
    const crops = data.crops.filter(crop => (!filters.environment || crop.environment === filters.environment) && (!filters.part || crop.part === filters.part));
    const visibleCrops = crops.filter(crop => !filters.crop || crop.id === filters.crop);
    const stageOptions = [...new Map(visibleCrops.flatMap(stagesFor))];
    if (!stageOptions.some(([key]) => key === filters.stage)) filters.stage = '';
    const options = {
      environment: [['', 'All environments'], ...data.environments.map(value => [value, value])],
      part: [['', 'All crop types'], ...data.parts.map(value => [value, value])],
      crop: [['', 'All matching crops'], ...crops.map(crop => [crop.id, crop.name])],
      stage: [['', 'All stages'], ...stageOptions],
    };
    for (const [key, label] of [['environment', 'Environment'], ['part', 'Crop type'], ['crop', 'Crop'], ['stage', 'Stage']]) {
      const input = select(options[key], filters[key]);
      input.onchange = () => {
        filters[key] = input.value;
        if (key === 'environment' || key === 'part') filters.crop = '';
        renderBrowse();
      };
      bar.append(field(label, input));
    }
    panel.append(bar);
    const listing = element('div', 'library-list');
    const review = button(`Review Selected (${selected.size})`, 'green');
    const updateCount = () => {
      review.textContent = `Review Selected (${selected.size})`;
      review.disabled = selected.size === 0 || selected.size > 100;
    };
    updateCount();
    for (const crop of visibleCrops) {
      for (const [stage, stageName] of stagesFor(crop).filter(([key]) => !filters.stage || key === filters.stage)) {
        const recipeStage = crop.recipe?.[stage];
        const templates = data.templates.filter(template => recipeStage
          ? recipeStage.templates.includes(template.id)
          : !template.recipe_only);
        const bundle = element('details', 'integration-block library-bundle');
        bundle.setAttribute('data-mobile-inline', '');
        bundle.append(element('summary', '', `${crop.environment} / ${crop.name} / ${crop.part} / ${stageName}`));
        const content = element('div', 'integration-block-content');
        if (recipeStage) content.append(element('p', 'muted', recipeStage.description));
        const all = element('input');
        all.type = 'checkbox';
        const inputs = [];
        const sync = () => {
          const count = inputs.filter(input => input.checked).length;
          all.checked = count === inputs.length;
          all.indeterminate = count > 0 && count < inputs.length;
          updateCount();
        };
        const choose = (template, checked) => {
          const key = keyFor(crop, stage, template);
          if (checked && !selected.has(key)) selected.set(key, {
            crop_id: crop.id, template_id: template.id, stage, name: crop.name,
            planting_id: '', variety: '', start: '', end: '', hyst: 0,
          });
          if (!checked) selected.delete(key);
          batch = null;
        };
        all.onchange = () => {
          templates.forEach((template, index) => { choose(template, all.checked); inputs[index].checked = all.checked; });
          sync();
        };
        content.append(field('Select bundle', all));
        for (const template of templates) {
          const input = element('input');
          input.type = 'checkbox';
          input.checked = selected.has(keyFor(crop, stage, template));
          input.onchange = () => { choose(template, input.checked); sync(); };
          inputs.push(input);
          const label = field(template.title, input);
          if (template.description) label.title = template.description;
          content.append(label);
        }
        sync();
        bundle.append(content);
        listing.append(bundle);
      }
    }
    panel.append(listing, element('p', 'muted', 'Selections are retained while you change filters. Up to 100 rules per import.'));
    const cancel = button('Saved Automations');
    cancel.onclick = close;
    review.onclick = renderReview;
    footer(cancel, review);
  }
  async function renderReview() {
    heading();
    const status = element('p', 'sai-live-status');
    status.setAttribute('role', 'status');
    panel.append(status);
    status.textContent = 'Refreshing destination groups…';
    try {
      const current = await json('/advanced/automation-library');
      data.groups = current.groups;
      status.textContent = '';
    } catch (error) {
      status.textContent = error.message;
      const back = button('Back to Library');
      back.onclick = renderBrowse;
      footer(back);
      return;
    }
    if (!panel.isConnected) return;
    const form = element('form', 'library-review');
    if (destinationGroup === null) destinationGroup = data.groups.includes(filters.environment) ? filters.environment : '';
    else if (!data.groups.includes(destinationGroup)) { destinationGroup = ''; batch = null; }
    const group = select([['', 'Choose a saved group'], ...data.groups.map(name => [name, name])], destinationGroup);
    group.required = true;
    group.onchange = () => { destinationGroup = group.value; batch = null; };
    form.append(field('Destination group', group));
    const timedSelections = [...selected.values()].filter(entry =>
      data.crops.find(crop => crop.id === entry.crop_id)?.recipe?.[entry.stage]?.hours);
    const usesDryingStart = timedSelections.length > 0;
    if (usesDryingStart) {
      const start = element('input');
      start.type = 'datetime-local';
      start.required = true;
      start.value = dryingStartedAt;
      start.oninput = () => { dryingStartedAt = start.value; batch = null; };
      form.append(field('Drying start (hub timezone)', start),
        element('p', 'muted', 'All selected cannabis rules use this start time. Drying ends after 240 hours and curing continues until its rules are disabled.'));
    }
    if (timedSelections.length < selected.size) {
      form.append(element('p', 'muted', 'Set a stage window or link a calendar planting to limit when alerts apply. Blank dates leave unlinked alerts active whenever enabled. End dates are exclusive, in the hub timezone. Daily times use the system clock.'));
    }
    const roles = [...new Set([...selected.values()].map(entry => data.templates.find(item => item.id === entry.template_id)).filter(template => template.kind === 'sensor').map(template => template.role))];
    if (roles.length) {
      const common = element('fieldset', 'library-review-entry');
      common.append(element('legend', '', 'Shared sensor bindings'));
      for (const role of roles) {
        const template = data.templates.find(item => item.role === role);
        const available = bindings.filter(item => template.metrics.includes(item.metric));
        const input = select([['', 'Choose a shared sensor / metric'], ...available.map(item => [JSON.stringify([item.sensor, item.metric]), item.label])]);
        input.onchange = () => {
          if (!input.value) return;
          form.querySelectorAll('select[data-library-role]').forEach(control => {
            if (control.dataset.libraryRole === role) {
              control.value = input.value;
              control.dispatchEvent(new Event('change'));
            }
          });
        };
        common.append(field(`Shared ${role} sensor / metric`, input));
      }
      form.append(common);
    }
    for (const [key, entry] of selected) {
      const crop = data.crops.find(item => item.id === entry.crop_id);
      const template = data.templates.find(item => item.id === entry.template_id);
      const recipeStage = crop.recipe?.[entry.stage];
      const stageName = recipeStage?.name || data.stages[entry.stage];
      const card = element('fieldset', 'library-review-entry');
      card.append(element('legend', '', `${crop.name} / ${stageName} / ${template.title}`), element('p', 'muted', `${crop.environment} / ${crop.part}`));
      const remove = button('Remove selection', 'red');
      remove.onclick = () => { selected.delete(key); batch = null; if (selected.size) renderReview(); else renderBrowse(); };
      const fields = element('div', 'library-filters');
      const name = element('input');
      name.value = entry.name;
      name.required = true;
      name.maxLength = 80;
      name.oninput = () => { entry.name = name.value; batch = null; };
      const variety = element('input');
      variety.value = entry.variety || '';
      variety.maxLength = 80;
      variety.oninput = () => { entry.variety = variety.value; batch = null; };
      fields.append(field('Planting name', name), field('Variety (optional)', variety));
      if (!recipeStage?.hours) {
        const planting = select([['', 'No calendar planting'], ...plantings.filter(item => (!item.plant_part || item.plant_part === crop.part) && (!item.environment || item.environment === crop.environment)).map(item => [item.id, `${item.name}${item.variety ? ' / ' + item.variety : ''}`])], entry.planting_id);
        const start = element('input');
        const end = element('input');
        start.type = end.type = 'date';
        const dates = () => {
          const linked = plantings.find(item => item.id === entry.planting_id);
          if (linked) {
            const window = linked.stage_schedule?.[entry.stage] || {};
            entry.start = window.start || '';
            entry.end = window.end || '';
          }
          start.value = entry.start;
          end.value = entry.end;
          start.disabled = end.disabled = !!linked;
          start.required = end.required = template.kind === 'start' || !!entry.start || !!entry.end || !!linked;
        };
        planting.onchange = () => { entry.planting_id = planting.value; batch = null; dates(); };
        start.oninput = () => { entry.start = start.value; batch = null; end.required = !!start.value || template.kind === 'start'; };
        end.oninput = () => { entry.end = end.value; batch = null; start.required = !!end.value || template.kind === 'start'; };
        dates();
        if (!crop.recipe) fields.append(field('BD Calendar planting', planting));
        fields.append(field('Stage start', start), field('Stage end (exclusive)', end));
      }
      if (template.kind === 'sensor') {
        const available = bindings.filter(item => template.metrics.includes(item.metric));
        const sensor = select([['', 'Choose a sensor / metric'], ...available.map(item => [JSON.stringify([item.sensor, item.metric]), item.label])], entry.sensor ? JSON.stringify([entry.sensor, entry.metric]) : '');
        sensor.required = true;
        sensor.dataset.libraryRole = template.role;
        if (!available.length) fields.append(element('p', 'sai-live-status', 'No compatible sensor / metric is available for this alert.'));
        const threshold = element('input');
        threshold.type = 'number';
        threshold.step = 'any';
        threshold.required = true;
        const preset = () => {
          if (!entry.sensor || template.default_value === undefined) return '';
          const value = template.default_value;
          if (template.default_unit === '°F' && entry.unit === '°C') return Number(((value - 32) * 5 / 9).toFixed(2));
          if (template.default_unit === '°C' && entry.unit === '°F') return Number((value * 9 / 5 + 32).toFixed(2));
          return value;
        };
        threshold.value = entry.value ?? preset();
        const thresholdLabel = field(`Threshold (${entry.unit || 'select sensor units'})`, threshold);
        sensor.onchange = () => {
          const binding = available.find(item => JSON.stringify([item.sensor, item.metric]) === sensor.value);
          entry.sensor = binding?.sensor || '';
          entry.metric = binding?.metric || '';
          entry.unit = binding?.unit || '';
          delete entry.value;
          threshold.value = preset();
          entry.hyst = 0;
          hyst.value = '0';
          thresholdLabel.firstChild.textContent = `Threshold (${entry.unit || 'native units'})`;
          batch = null;
        };
        threshold.oninput = () => { entry.value = threshold.value; batch = null; };
        const hyst = element('input');
        hyst.type = 'number';
        hyst.step = 'any';
        hyst.min = '0';
        hyst.required = true;
        hyst.value = entry.hyst;
        hyst.oninput = () => { entry.hyst = hyst.value; batch = null; };
        fields.append(field('Sensor / metric', sensor), thresholdLabel, field('Hysteresis (same units)', hyst));
      }
      if (template.kind === 'time') {
        const startTime = element('input');
        const endTime = element('input');
        startTime.type = endTime.type = 'time';
        startTime.required = endTime.required = true;
        startTime.value = entry.time_start || '';
        endTime.value = entry.time_end || '';
        const validateTimes = () => endTime.setCustomValidity(endTime.value && endTime.value === startTime.value ? 'Choose an end time different from the start time.' : '');
        const updateTimes = () => {
          entry.time_start = startTime.value;
          entry.time_end = endTime.value;
          validateTimes();
          batch = null;
        };
        validateTimes();
        startTime.oninput = endTime.oninput = updateTimes;
        fields.append(field('Daily start time', startTime), field('Daily end time', endTime));
      }
      card.append(fields, remove);
      form.append(card);
    }
    const importButton = button(`Import Selected (${selected.size})`, 'green');
    importButton.type = 'submit';
    const back = button('Back to Library');
    back.onclick = renderBrowse;
    const actions = element('div', 'pane-footer pane-footer-actions-right');
    actions.append(back, importButton);
    form.append(actions);
    form.onsubmit = async event => {
      event.preventDefault();
      if (!form.reportValidity()) return;
      const entries = [...selected.values()];
      for (const entry of entries) {
        if ((entry.start || entry.end || entry.planting_id) && (!entry.start || !entry.end || entry.start >= entry.end)) {
          status.textContent = 'Set an ordered stage window for each dated selection. Linked windows are edited in BD Calendar Plantings.';
          return;
        }
      }
      batch ||= newBatch();
      const controls = form.querySelectorAll('input, select, button');
      const disabled = new Map([...controls].map(control => [control, control.disabled]));
      controls.forEach(control => { control.disabled = true; });
      status.textContent = 'Importing disabled automations…';
      try {
        const payload = {batch_id: batch, group: group.value, entries};
        if (usesDryingStart) payload.drying_started_at = dryingStartedAt;
        const result = await json('/advanced/automation-library/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)});
        await onImported(result.rule_ids, group.value);
        close();
        const message = modal.querySelector('#automationArrangeStatus');
        if (message) message.textContent = `Imported ${result.rule_ids.length} disabled automations.`;
      } catch (error) {
        status.textContent = error.message;
        controls.forEach(control => { control.disabled = disabled.get(control); });
      }
    };
    panel.append(form);
  }
  renderBrowse();
}
