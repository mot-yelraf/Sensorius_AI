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
  const heading = () => {
    panel.replaceChildren(element('h3', 'pane-title', 'Automation Library'));
    panel.append(element('p', 'muted', 'Garden-crop starting points. Choose your own targets. Imported automations are disabled Alert rules; review them before enabling.'));
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
    const options = {
      environment: [['', 'All environments'], ...data.environments.map(value => [value, value])],
      part: [['', 'All crop types'], ...data.parts.map(value => [value, value])],
      crop: [['', 'All matching crops'], ...data.crops.filter(crop => (!filters.environment || crop.environment === filters.environment) && (!filters.part || crop.part === filters.part)).map(crop => [crop.id, crop.name])],
      stage: [['', 'All stages'], ...Object.entries(data.stages)],
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
    for (const crop of data.crops.filter(crop => (!filters.environment || crop.environment === filters.environment) && (!filters.part || crop.part === filters.part) && (!filters.crop || crop.id === filters.crop))) {
      for (const [stage, stageName] of Object.entries(data.stages).filter(([key]) => !filters.stage || key === filters.stage)) {
        const bundle = element('details', 'integration-block library-bundle');
        bundle.setAttribute('data-mobile-inline', '');
        bundle.append(element('summary', '', `${crop.environment} / ${crop.name} / ${crop.part} / ${stageName}`));
        const content = element('div', 'integration-block-content');
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
          data.templates.forEach((template, index) => { choose(template, all.checked); inputs[index].checked = all.checked; });
          sync();
        };
        content.append(field('Select bundle', all));
        for (const template of data.templates) {
          const input = element('input');
          input.type = 'checkbox';
          input.checked = selected.has(keyFor(crop, stage, template));
          input.onchange = () => { choose(template, input.checked); sync(); };
          inputs.push(input);
          const label = field(template.title, input);
          label.title = template.description;
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
    form.append(element('p', 'muted', 'Set a stage window or link a calendar planting to limit when alerts apply. Blank dates leave unlinked alerts active whenever enabled. End dates are exclusive, in the hub timezone. Planting dates are copied at import; later edits leave saved rules unchanged.'));
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
      const card = element('fieldset', 'library-review-entry');
      card.append(element('legend', '', `${crop.name} / ${data.stages[entry.stage]} / ${template.title}`), element('p', 'muted', `${crop.environment} / ${crop.part}`));
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
      fields.append(field('BD Calendar planting', planting), field('Stage start', start), field('Stage end (exclusive)', end));
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
        threshold.value = entry.value ?? '';
        const thresholdLabel = field(`Threshold (${entry.unit || 'select sensor units'})`, threshold);
        sensor.onchange = () => {
          const binding = available.find(item => JSON.stringify([item.sensor, item.metric]) === sensor.value);
          entry.sensor = binding?.sensor || '';
          entry.metric = binding?.metric || '';
          entry.unit = binding?.unit || '';
          entry.value = '';
          threshold.value = '';
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
        const result = await json('/advanced/automation-library/import', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({batch_id: batch, group: group.value, entries})});
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
