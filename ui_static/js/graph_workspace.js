const { TZ_OFFSET_S, TZ_NAME, GRAPH_GAUGE_CONFIG, SWITCH_MAP } = JSON.parse(document.getElementById('graph-config').textContent);
function toggleCustomTime(enabled){ const el = document.getElementById('custom_time_inputs'); if(el) el.style.display = enabled ? 'grid' : 'none'; }

    function toLocalMs(rawTs){
      if (rawTs == null) return NaN;
      if (typeof rawTs === 'number' && Number.isFinite(rawTs)) return rawTs * 1000;
      if (typeof rawTs === 'string' && /^\d+(\.\d+)?$/.test(rawTs.trim())) return Number(rawTs) * 1000;
      const t = new Date(rawTs).getTime();
      return Number.isFinite(t) ? t : NaN;
    }
    function localOrUndef(v){
      if (v === undefined || v === null || v === '') return undefined;
      const ms = toLocalMs(v);
      return Number.isFinite(ms) ? ms : undefined;
    }

    function graphMetricNameFromKey(seriesKey){
      const parts = String(seriesKey || '').split('::');
      return (parts.length ? parts[parts.length - 1] : seriesKey || '').trim();
    }

    function graphPressureMetric(metric){
      const text = String(metric || '').trim();
      return text === 'Pressure' || text === 'Baro-Pressure' || text.endsWith(' Baro-Pressure');
    }

    function normalizeGraphGaugeMetricName(value){
      return String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
    }

    function graphGaugeMetricEntry(metricName){
      const raw = String(metricName || '').trim();
      if (!raw) return null;
      if (GRAPH_GAUGE_CONFIG && GRAPH_GAUGE_CONFIG[raw]) {
        return { key: raw, config: GRAPH_GAUGE_CONFIG[raw] };
      }
      const withoutChannel = raw.replace(/^CH\d+\s+/i, '').trim();
      if (withoutChannel && GRAPH_GAUGE_CONFIG && GRAPH_GAUGE_CONFIG[withoutChannel]) {
        return { key: withoutChannel, config: GRAPH_GAUGE_CONFIG[withoutChannel] };
      }
      const rawNorm = normalizeGraphGaugeMetricName(raw);
      const channelNorm = normalizeGraphGaugeMetricName(withoutChannel);
      for (const key of Object.keys(GRAPH_GAUGE_CONFIG || {})) {
        const keyNorm = normalizeGraphGaugeMetricName(key);
        if (keyNorm === rawNorm || (channelNorm && keyNorm === channelNorm)) {
          return { key: key, config: GRAPH_GAUGE_CONFIG[key] };
        }
      }
      return null;
    }

    function graphDisplayValue(value, metricName, rate){
      if (value === null || value === undefined || value === '') return null;
      const numeric = Number(value);
      if (!Number.isFinite(numeric)) return null;
      const entry = graphGaugeMetricEntry(metricName);
      const cfg = (entry && entry.config) || {};
      const factor = Number(cfg.display_factor === undefined ? 1 : cfg.display_factor);
      const offset = rate ? 0 : Number(cfg.display_offset === undefined ? 0 : cfg.display_offset);
      return numeric * (Number.isFinite(factor) ? factor : 1) + (Number.isFinite(offset) ? offset : 0);
    }

    function soilFertilityGaugeZones(metricName){
      const entry = graphGaugeMetricEntry(metricName);
      if (!entry || entry.key !== 'Soil Fertility Index') return null;
      const cfg = entry.config || {};
      const zones = Array.isArray(cfg.zones) ? cfg.zones.map(function(z){
        return {
          min: Number(z && z.min),
          max: Number(z && z.max),
          color: (z && (z.strokeStyle || z.color)) || ''
        };
      }).filter(function(z){
        return Number.isFinite(z.min) && Number.isFinite(z.max) && !!z.color;
      }) : [];
      if (!zones.length) return null;
      return {
        min: Number(cfg.min),
        max: Number(cfg.max),
        zones: zones
      };
    }

    let GRAPH_SETUPS = [];
    let GRAPH_LAST_USED = '';
    let GRAPH_ACTIVE_SETUP = '';
    const GRAPH_SENSOR_METRIC_PAIRS = [
      ['sensor1_select', 'metric1_select'],
      ['sensor2_select', 'metric2_select'],
      ['sensor3_select', 'metric3_select']
    ];

    async function fetchJSON(url){
      const r = await fetch(url, {cache: 'no-store'});
      try { return await r.json(); } catch { return null; }
    }

    async function postJSON(url, payload){
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload || {})
      });
      let data = null;
      try { data = await r.json(); } catch {}
      if(!r.ok){
        const msg = (data && (data.error || data.detail)) ? String(data.error || data.detail) : ('HTTP ' + r.status);
        throw new Error(msg);
      }
      return data || {};
    }

    async function populateSensors(selectIds){
      const sensors = await fetchJSON('/sensor-ids') || [];
      for(const id of selectIds){
        const sel = document.getElementById(id);
        if(!sel) continue;
        sel.innerHTML = "<option value=''>-- Select Sensor --</option>";
        sensors.forEach(s => {
          const o = document.createElement('option');
          o.value = s;
          o.textContent = s;
          sel.appendChild(o);
        });
      }
      updateGraphButtonState();
    }

    async function populateMetricsFor(sensorSelId, metricSelId){
      const sidEl = document.getElementById(sensorSelId);
      const sid = (sidEl && sidEl.value ? sidEl.value : "").trim();
      const msel = document.getElementById(metricSelId);
      if(!msel) return;
      msel.innerHTML = "<option value=''>-- Select Metric --</option>";
      updateGraphButtonState();
      if(!sid) return;

      try{
        const payload = await fetchJSON(`/sensor-metrics?sensor_id=${encodeURIComponent(sid)}`);
        let metricNames = [];
        if(Array.isArray(payload)) metricNames = payload;
        else if(payload && typeof payload === 'object'){
          if(Array.isArray(payload.metrics)) metricNames = payload.metrics;
          else metricNames = Object.keys(payload);
        }
        metricNames.forEach(name => {
          const opt = document.createElement('option');
          opt.value = name;
          opt.textContent = name;
          msel.appendChild(opt);
        });
      }finally{
        updateGraphButtonState();
      }
    }

    function hasGraphSensorMetricSelection(){
      return GRAPH_SENSOR_METRIC_PAIRS.some(function(pair){
        const sensor = (document.getElementById(pair[0])?.value || '').trim();
        const metric = (document.getElementById(pair[1])?.value || '').trim();
        return !!(sensor && metric);
      });
    }

    function updateGraphButtonState(){
      const btn = document.getElementById('graphButton');
      if(!btn) return false;
      const hasSelection = hasGraphSensorMetricSelection();
      const isLoading = btn.dataset.loading === '1';
      btn.disabled = isLoading || !hasSelection;
      btn.setAttribute('aria-disabled', btn.disabled ? 'true' : 'false');
      if(isLoading){
        btn.title = 'Preparing graph';
      }else{
        btn.title = hasSelection ? 'Load graph' : 'Select at least one sensor and metric';
      }
      return hasSelection;
    }

    function renderSwitchChannels(){
      const swSel = document.getElementById('switch_select');
      const chBox = document.getElementById('channel_checkboxes');
      if(!swSel || !chBox) return;
      chBox.innerHTML = '';
      const sid = (swSel.value || '').trim();
      if(!sid) return;
      (SWITCH_MAP[sid] || []).forEach(label => {
        const encoded = btoa(unescape(encodeURIComponent(sid + '::' + label))).replace(/=/g,'');
        const id = 'ch_' + encoded;
        const wrap = document.createElement('label');
        wrap.innerHTML = "<input type='checkbox' id='" + id + "' data-label='" + label + "'> " + label;
        chBox.appendChild(wrap);
      });
    }

    function normalizeAstralMode(raw){
      const value = String(raw || 'none').trim().toLowerCase().replace(/[\s-]+/g, '_').replace(/&/g, '_');
      if (value === 'sun') return 'sun';
      if (value === 'moon') return 'moon';
      if (value === 'sun_moon' || value === 'sun__moon' || value === 'both' || value === 'sunmoon') return 'sun_moon';
      return 'none';
    }

    function setRangeSelection(rangeVal){
      const normalized = (rangeVal || '24h').trim().toLowerCase();
      const target = document.querySelector("input[name='range'][value='" + normalized + "']");
      const custom = normalized === 'custom';
      if(target){
        target.checked = true;
      }else{
        const fallback = document.querySelector("input[name='range'][value='24h']");
        if (fallback) fallback.checked = true;
      }
      toggleCustomTime(custom);
    }

    function getCurrentGraphConfig(){
      const rangeEl = document.querySelector("input[name='range']:checked");
      const range = rangeEl ? String(rangeEl.value || '24h') : '24h';
      const cfg = {
        sensor1_select: (document.getElementById('sensor1_select')?.value || '').trim(),
        sensor2_select: (document.getElementById('sensor2_select')?.value || '').trim(),
        sensor3_select: (document.getElementById('sensor3_select')?.value || '').trim(),
        metric1_select: (document.getElementById('metric1_select')?.value || '').trim(),
        metric2_select: (document.getElementById('metric2_select')?.value || '').trim(),
        metric3_select: (document.getElementById('metric3_select')?.value || '').trim(),
        range: range,
        astral_select: (document.getElementById('astral_select')?.value || 'none').trim(),
        start_time: (document.getElementById('start_time')?.value || '').trim(),
        end_time: (document.getElementById('end_time')?.value || '').trim(),
        switch_select: (document.getElementById('switch_select')?.value || '').trim(),
        channels: []
      };
      const cbs = document.querySelectorAll('#channel_checkboxes input[type="checkbox"]');
      (cbs || []).forEach(cb => {
        if(cb.checked){
          const label = (cb.getAttribute('data-label') || '').trim();
          if(label) cfg.channels.push(label);
        }
      });
      return cfg;
    }

    async function applyGraphConfig(cfg){
      const c = (cfg && typeof cfg === 'object') ? cfg : {};
      const s1 = document.getElementById('sensor1_select');
      const s2 = document.getElementById('sensor2_select');
      const s3 = document.getElementById('sensor3_select');
      if(s1) s1.value = String(c.sensor1_select || '');
      if(s2) s2.value = String(c.sensor2_select || '');
      if(s3) s3.value = String(c.sensor3_select || '');

      await populateMetricsFor('sensor1_select','metric1_select');
      await populateMetricsFor('sensor2_select','metric2_select');
      await populateMetricsFor('sensor3_select','metric3_select');

      const m1 = document.getElementById('metric1_select');
      const m2 = document.getElementById('metric2_select');
      const m3 = document.getElementById('metric3_select');
      if(m1) m1.value = String(c.metric1_select || '');
      if(m2) m2.value = String(c.metric2_select || '');
      if(m3) m3.value = String(c.metric3_select || '');
      updateGraphButtonState();

      setRangeSelection(String(c.range || '24h'));
      const startEl = document.getElementById('start_time');
      const endEl = document.getElementById('end_time');
      if(startEl) startEl.value = String(c.start_time || '');
      if(endEl) endEl.value = String(c.end_time || '');
      const astralEl = document.getElementById('astral_select');
      if(astralEl) astralEl.value = normalizeAstralMode(c.astral_select || 'none');

      const swSel = document.getElementById('switch_select');
      if(swSel){
        swSel.value = String(c.switch_select || '');
        renderSwitchChannels();
        const selected = new Set(Array.isArray(c.channels) ? c.channels.map(v => String(v)) : []);
        const cbs = document.querySelectorAll('#channel_checkboxes input[type="checkbox"]');
        (cbs || []).forEach(cb => {
          const label = String(cb.getAttribute('data-label') || '');
          cb.checked = selected.has(label);
        });
      }
    }

    function markActiveSetup(name){
      GRAPH_ACTIVE_SETUP = String(name || '');
      const removeBtn = document.getElementById('graphSetupRemoveBtn');
      if(removeBtn) removeBtn.disabled = !GRAPH_ACTIVE_SETUP;
      const nodes = document.querySelectorAll('#graphSetupList .setup-item');
      (nodes || []).forEach(n => {
        if((n.getAttribute('data-name') || '') === GRAPH_ACTIVE_SETUP) n.classList.add('active');
        else n.classList.remove('active');
      });
    }

    function renderGraphSetupList(){
      const list = document.getElementById('graphSetupList');
      if(!list) return;
      list.innerHTML = '';
      if(!Array.isArray(GRAPH_SETUPS) || !GRAPH_SETUPS.length){
        const empty = document.createElement('div');
        empty.className = 'setup-empty';
        empty.textContent = 'No saved graph setups.';
        list.appendChild(empty);
        markActiveSetup('');
        return;
      }
      GRAPH_SETUPS.forEach(item => {
        const btn = document.createElement('button');
        const setupName = String(item.name || '');
        btn.type = 'button';
        btn.className = 'setup-item';
        btn.setAttribute('data-name', setupName);
        btn.title = setupName ? `Load graph setup ${setupName}` : 'Load graph setup';
        btn.textContent = setupName;
        btn.onclick = () => loadSavedGraphSetup(setupName);
        list.appendChild(btn);
      });
      markActiveSetup(GRAPH_ACTIVE_SETUP || GRAPH_LAST_USED || '');
    }

    async function refreshGraphSetupsFromServer(){
      const payload = await fetchJSON('/graph-setups');
      const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
      GRAPH_SETUPS = items.map(it => ({
        name: String((it && it.name) || ''),
        config: (it && typeof it.config === 'object') ? it.config : {}
      })).filter(it => it.name);
      GRAPH_LAST_USED = String((payload && payload.last_used) || '');
      renderGraphSetupList();
    }

    async function loadSavedGraphSetup(name){
      const setupName = String(name || '').trim();
      if(!setupName) return;
      const hit = (GRAPH_SETUPS || []).find(it => String(it.name || '') === setupName);
      if(!hit) return;
      await applyGraphConfig(hit.config || {});
      markActiveSetup(setupName);
      try{
        await postJSON('/graph-setups/use', { name: setupName });
      }catch(e){
        console.warn('Failed to set last-used graph setup', e);
      }
      updateGraphButtonState();
    }

    async function saveGraphSetup(event){
      const btn = event && event.target ? event.target.closest('button') : document.getElementById('graphSaveButton');
      if(btn){
        if(!btn.dataset.baseLabel) btn.dataset.baseLabel = btn.textContent || 'Save';
        btn.disabled = true;
        btn.textContent = 'Saving...';
      }
      try{
        const suggested = GRAPH_ACTIVE_SETUP || '';
        const rawName = window.prompt('Save graph setup as:', suggested);
        if(rawName === null) return;
        const name = String(rawName || '').trim();
        if(!name){
          if(typeof window.showToast === 'function') window.showToast('Setup name is required.', 'error');
          return;
        }
        const config = getCurrentGraphConfig();
        const payload = await postJSON('/graph-setups/save', { name: name, config: config });
        const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
        GRAPH_SETUPS = items.map(it => ({
          name: String((it && it.name) || ''),
          config: (it && typeof it.config === 'object') ? it.config : {}
        })).filter(it => it.name);
        GRAPH_LAST_USED = String((payload && payload.last_used) || name);
        renderGraphSetupList();
        markActiveSetup(name);
        if(typeof window.showToast === 'function') window.showToast('Graph setup saved', 'ok');
      }catch(e){
        console.error('Save graph setup failed', e);
        if(typeof window.showToast === 'function') window.showToast('Failed to save graph setup: ' + (e && e.message ? e.message : 'unknown error'), 'error');
      }finally{
        if(btn){
          btn.disabled = false;
          btn.textContent = btn.dataset.baseLabel || 'Save';
        }
      }
    }

    async function removeGraphSetup(){
      const name = String(GRAPH_ACTIVE_SETUP || '').trim();
      if(!name) return;
      if(!window.confirm("Remove saved graph setup '" + name + "'?")) return;
      const btn = document.getElementById('graphSetupRemoveBtn');
      if(btn) btn.disabled = true;
      try{
        const payload = await postJSON('/graph-setups/remove', { name: name });
        const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
        GRAPH_SETUPS = items.map(it => ({
          name: String((it && it.name) || ''),
          config: (it && typeof it.config === 'object') ? it.config : {}
        })).filter(it => it.name);
        GRAPH_LAST_USED = String((payload && payload.last_used) || '');
        renderGraphSetupList();
        if(GRAPH_LAST_USED){
          await loadSavedGraphSetup(GRAPH_LAST_USED);
        } else {
          markActiveSetup('');
        }
        if(typeof window.showToast === 'function') window.showToast('Graph setup removed', 'ok');
      }catch(e){
        console.error('Remove graph setup failed', e);
        alert('Failed to remove graph setup: ' + (e && e.message ? e.message : 'unknown error'));
      }finally{
        if(btn) btn.disabled = !GRAPH_ACTIVE_SETUP;
      }
    }

    window.saveGraphSetup = saveGraphSetup;
    window.removeGraphSetup = removeGraphSetup;

    async function initGraphBuilder(){
      try{
        await populateSensors(['sensor1_select','sensor2_select','sensor3_select']);
        const s1 = document.getElementById('sensor1_select');
        const s2 = document.getElementById('sensor2_select');
        const s3 = document.getElementById('sensor3_select');

        if(s1) s1.onchange = () => populateMetricsFor('sensor1_select','metric1_select');
        if(s2) s2.onchange = () => populateMetricsFor('sensor2_select','metric2_select');
        if(s3) s3.onchange = () => populateMetricsFor('sensor3_select','metric3_select');

        await populateMetricsFor('sensor1_select','metric1_select');
        await populateMetricsFor('sensor2_select','metric2_select');
        await populateMetricsFor('sensor3_select','metric3_select');
        const m1 = document.getElementById('metric1_select');
        const m2 = document.getElementById('metric2_select');
        const m3 = document.getElementById('metric3_select');
        if(m1) m1.onchange = updateGraphButtonState;
        if(m2) m2.onchange = updateGraphButtonState;
        if(m3) m3.onchange = updateGraphButtonState;

        const swSel = document.getElementById('switch_select');
        if(swSel){
          swSel.innerHTML = "<option value=''>-- Select Switch --</option>";
          Object.keys(SWITCH_MAP).forEach(sid => {
            const o = document.createElement('option');
            o.value = sid;
            o.textContent = sid;
            swSel.appendChild(o);
          });
          swSel.onchange = () => renderSwitchChannels();
        }
        await refreshGraphSetupsFromServer();
        if(GRAPH_LAST_USED){
          await loadSavedGraphSetup(GRAPH_LAST_USED);
        }else{
          markActiveSetup('');
        }
      }catch(e){
        console.error('Graph builder init failed', e);
      }finally{
        updateGraphButtonState();
      }
    }

    function loadGraph(event){
      if(!hasGraphSensorMetricSelection()){
        updateGraphButtonState();
        return;
      }
      const button = event && event.target ? event.target.closest('button') : document.getElementById('graphButton');
      if(!button) return;
      const spinner = button.querySelector('.spinner');
      const text = button.querySelector('.button-text');
      button.dataset.loading = '1';
      button.disabled = true;
      if(spinner) spinner.style.display='inline-block';
      if(text) text.textContent='Preparing Graph...';

      const s1 = document.getElementById('sensor1_select').value;
      const s2 = document.getElementById('sensor2_select').value;
      const s3 = document.getElementById('sensor3_select').value;
      const m1 = document.getElementById('metric1_select').value;
      const m2 = document.getElementById('metric2_select').value;
      const m3 = document.getElementById('metric3_select').value;

      const rangeEl  = document.querySelector('input[name="range"]:checked');
      const rangeVal = rangeEl ? rangeEl.value : '24h';
      const start = document.getElementById('start_time') ? document.getElementById('start_time').value : "";
      const end   = document.getElementById('end_time')   ? document.getElementById('end_time').value   : "";
      const astralSel = document.getElementById('astral_select');

      const params = new URLSearchParams({
        sensor_id: s1 || '',
        metric1: m1 || '',
        metric2: m2 || '',
        metric3: m3 || '',
        sensor_id1: s1 || '',
        sensor_id2: s2 || '',
        sensor_id3: s3 || '',
        range: rangeVal,
        astral: astralSel ? normalizeAstralMode(astralSel.value || 'none') : 'none'
      });

      if(rangeVal === 'custom'){
        if(!start || !end){
          alert('Enter start and end times.');
          delete button.dataset.loading;
          if(spinner) spinner.style.display='none';
          if(text) text.textContent='Graph It';
          updateGraphButtonState();
          return;
        }
        params.set('start', start);
        params.set('end', end);
      }

      const swSel = document.getElementById('switch_select');
      if(swSel && swSel.value){
        params.set('switch_id', swSel.value);
        const chBox = document.getElementById('channel_checkboxes');
        const cbs = chBox ? chBox.querySelectorAll('input[type="checkbox"]') : [];
        (cbs || []).forEach(cb => {
          if(cb.checked){
            const label = cb.getAttribute('data-label') || '';
            if(label) params.append('channels', label);
          }
        });
      }

      fetch('/graph-data?' + params.toString())
        .then(r => r.json())
        .then(data => {
          if (data && data.no_data) {
            alert(data.detail || 'No data in selected graph window');
            return;
          }
          if (GRAPH_ACTIVE_SETUP){
            postJSON('/graph-setups/use', { name: GRAPH_ACTIVE_SETUP }).catch(() => {});
          }
          document.getElementById('graphModal').style.display='none';
          renderGraphFullscreen_V2(data);
        })
        .catch(e => {
          console.error('Graph error:', e);
          alert('Graph load failed');
        })
        .finally(() => {
          delete button.dataset.loading;
          if(spinner) spinner.style.display='none';
          if(text) text.textContent='Graph It';
          updateGraphButtonState();
        });
    }

    const FULLSCREEN_GRAPH_SELECTION_LIMIT = 4;
    let fullscreenGraphRefreshTimer = null;
    let fullscreenGraphRequestToken = 0;
    let fullscreenGraphOptionsReady = false;
    let fullscreenGraphAbortController = null;
    let fullscreenGraphRealtimeTimer = null;

    function selectedFullscreenMetrics(){
      return Array.from(document.querySelectorAll('.fullscreen-metric-checkbox:checked')).map(function(cb){
        return {
          sensor: String(cb.getAttribute('data-sensor') || ''),
          metric: String(cb.getAttribute('data-metric') || ''),
          label: String(cb.getAttribute('data-display-label') || cb.getAttribute('data-metric') || '')
        };
      });
    }

    function selectedFullscreenSwitches(){
      return Array.from(document.querySelectorAll('.fullscreen-switch-checkbox:checked')).map(function(cb){
        return {
          switchId: String(cb.getAttribute('data-switch-id') || ''),
          channel: String(cb.getAttribute('data-channel') || ''),
          label: String(cb.getAttribute('data-display-label') || cb.getAttribute('data-channel') || '')
        };
      });
    }

    function fullscreenSelectionTotal(){
      return selectedFullscreenMetrics().length + selectedFullscreenSwitches().length;
    }

    function updateFullscreenSelectionUi(){
      const metrics = selectedFullscreenMetrics();
      const switches = selectedFullscreenSwitches();
      const total = metrics.length + switches.length;
      const count = document.getElementById('graphSelectionCount');
      if(count) count.textContent = String(total) + ' of ' + String(FULLSCREEN_GRAPH_SELECTION_LIMIT);
      document.querySelectorAll('.fullscreen-metric-checkbox, .fullscreen-switch-checkbox').forEach(function(cb){
        cb.disabled = total >= FULLSCREEN_GRAPH_SELECTION_LIMIT && !cb.checked;
      });
      const chips = document.getElementById('fullscreenSelectedChips');
      if(chips){
        chips.innerHTML = '';
        metrics.concat(switches).forEach(function(item){
          const chip = document.createElement('span');
          chip.className = 'fullscreen-selection-chip';
          chip.textContent = item.label;
          chip.title = item.label;
          chips.appendChild(chip);
        });
      }
    }

    function handleFullscreenSelectionChange(checkbox){
      if(checkbox && checkbox.checked && fullscreenSelectionTotal() > FULLSCREEN_GRAPH_SELECTION_LIMIT){
        checkbox.checked = false;
        if(typeof window.showToast === 'function') window.showToast('Select up to four graph observations.', 'warn');
      }
      updateFullscreenSelectionUi();
      scheduleFullscreenGraphRefresh();
    }

    function graphMetricPayloadNames(payload){
      if(Array.isArray(payload)) return payload.map(String);
      if(payload && typeof payload === 'object'){
        if(Array.isArray(payload.metrics)) return payload.metrics.map(String);
        return Object.keys(payload);
      }
      return [];
    }

    function graphOptionGroup(title, location){
      const details = document.createElement('details');
      details.className = 'graph-option-group';
      const summary = document.createElement('summary');
      const heading = document.createElement('span');
      heading.className = 'graph-option-heading';
      const deviceId = document.createElement('span');
      deviceId.className = 'graph-option-device-id';
      deviceId.textContent = title;
      heading.appendChild(deviceId);
      const locationText = String(location || '').trim();
      if(locationText){
        const locationEl = document.createElement('span');
        locationEl.className = 'graph-option-location';
        locationEl.textContent = locationText;
        heading.appendChild(locationEl);
      }
      summary.appendChild(heading);
      details.appendChild(summary);
      const items = document.createElement('div');
      items.className = 'graph-option-items';
      details.appendChild(items);
      return { details: details, items: items };
    }

    async function fullscreenGraphDeviceLocations(){
      const payload = await fetchJSON('/device-locations');
      const locations = { sensor: {}, switch: {} };
      (Array.isArray(payload) ? payload : []).forEach(function(item){
        const id = String((item && item.id) || '').trim();
        const type = String((item && item.type) || '').trim().toLowerCase();
        const location = String((item && item.location) || '').trim();
        if(id && location && locations[type]) locations[type][id] = location;
      });
      return locations;
    }

    function graphCheckboxRow(cssClass, labelText, attrs){
      const label = document.createElement('label');
      label.className = 'graph-option-item';
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.className = cssClass;
      Object.entries(attrs || {}).forEach(function(pair){ input.setAttribute(pair[0], pair[1]); });
      input.addEventListener('change', function(){ handleFullscreenSelectionChange(input); });
      const text = document.createElement('span');
      text.textContent = labelText;
      label.appendChild(input);
      label.appendChild(text);
      return label;
    }

    async function populateFullscreenSensorOptions(sensorLocations){
      const host = document.getElementById('fullscreenSensorOptions');
      if(!host) return;
      host.innerHTML = '';
      const sensors = await fetchJSON('/sensor-ids') || [];
      const results = await Promise.all((Array.isArray(sensors) ? sensors : []).map(async function(sensor){
        const payload = await fetchJSON('/sensor-metrics?sensor_id=' + encodeURIComponent(sensor));
        return { sensor: String(sensor), metrics: graphMetricPayloadNames(payload) };
      }));
      results.forEach(function(entry, index){
        if(!entry.metrics.length) return;
        const group = graphOptionGroup(entry.sensor, (sensorLocations || {})[entry.sensor]);
        if(index === 0) group.details.open = true;
        entry.metrics.forEach(function(metric){
          const display = entry.sensor + ' · ' + metric;
          group.items.appendChild(graphCheckboxRow('fullscreen-metric-checkbox', metric, {
            'data-sensor': entry.sensor,
            'data-metric': metric,
            'data-display-label': display
          }));
        });
        host.appendChild(group.details);
      });
      if(!host.children.length){
        const empty = document.createElement('div');
        empty.className = 'graph-control-note';
        empty.textContent = 'No sensor metrics found.';
        host.appendChild(empty);
      }
    }

    function populateFullscreenSwitchOptions(switchLocations){
      const host = document.getElementById('fullscreenSwitchOptions');
      if(!host) return;
      host.innerHTML = '';
      Object.keys(SWITCH_MAP || {}).forEach(function(switchId){
        const channels = Array.isArray(SWITCH_MAP[switchId]) ? SWITCH_MAP[switchId] : [];
        if(!channels.length) return;
        const group = graphOptionGroup(switchId, (switchLocations || {})[switchId]);
        channels.forEach(function(channel){
          const display = switchId + ' · ' + channel;
          group.items.appendChild(graphCheckboxRow('fullscreen-switch-checkbox', channel, {
            'data-switch-id': switchId,
            'data-channel': channel,
            'data-display-label': display
          }));
        });
        host.appendChild(group.details);
      });
      if(!host.children.length){
        const empty = document.createElement('div');
        empty.className = 'graph-control-note';
        empty.textContent = 'No switches found.';
        host.appendChild(empty);
      }
    }

    function fullscreenRangeTitle(value){
      const labels = {
        '1h':'Last hour', '3h':'Last 3 hours', '6h':'Last 6 hours', '12h':'Last 12 hours',
        '24h':'Last 24 hours', '3d':'Last 3 days', '7d':'Last 7 days', '14d':'Last 14 days',
        '30d':'Last 30 days', '60d':'Last 60 days', '90d':'Last 90 days', 'custom':'Custom time range'
      };
      return labels[String(value || '').toLowerCase()] || 'Selected time range';
    }

    function renderFullscreenGraphEmpty(message){
      if(window.graphChart){
        window.graphChart.destroy();
        window.graphChart = null;
      }
      const canvas = document.getElementById('fullscreen_graph');
      if(!canvas) return;
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(rect.width || 800));
      canvas.height = Math.max(1, Math.round(rect.height || 400));
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      ctx.fillStyle = '#61796f';
      ctx.font = '16px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(message || 'Select a sensor metric to begin.', canvas.width / 2, canvas.height / 2);
    }

    function scheduleFullscreenGraphRefresh(){
      const rangeEl = document.querySelector("input[name='range']:checked");
      const title = document.getElementById('fullscreenGraphRangeTitle');
      if(title) title.textContent = fullscreenRangeTitle(rangeEl ? rangeEl.value : '24h');
      if(fullscreenGraphRefreshTimer) window.clearTimeout(fullscreenGraphRefreshTimer);
      fullscreenGraphRefreshTimer = window.setTimeout(loadFullscreenGraph, 160);
    }

    function fullscreenGraphIsOpen(){
      const workspace = document.getElementById('fullscreen_graph_container');
      return !!workspace && window.getComputedStyle(workspace).display !== 'none';
    }

    function notifyFullscreenGraphDataChanged(){
      if(document.hidden || !fullscreenGraphIsOpen() || !selectedFullscreenMetrics().length) return;
      scheduleFullscreenGraphRefresh();
    }

    async function loadFullscreenGraph(){
      const token = ++fullscreenGraphRequestToken;
      const metrics = selectedFullscreenMetrics().slice(0, FULLSCREEN_GRAPH_SELECTION_LIMIT);
      const switches = selectedFullscreenSwitches();
      if(!metrics.length){
        const loading = document.getElementById('graphLoadingState');
        if(loading) loading.style.display = 'none';
        renderFullscreenGraphEmpty(switches.length ? 'Select a sensor metric to display switch transitions.' : 'Select a sensor metric to begin.');
        return;
      }
      const rangeEl = document.querySelector("input[name='range']:checked");
      const rangeVal = rangeEl ? rangeEl.value : '24h';
      const start = document.getElementById('start_time')?.value || '';
      const end = document.getElementById('end_time')?.value || '';
      if(rangeVal === 'custom' && (!start || !end)){
        const loading = document.getElementById('graphLoadingState');
        if(loading) loading.style.display = 'none';
        renderFullscreenGraphEmpty('Choose both custom start and end times.');
        return;
      }
      const params = new URLSearchParams({ range: rangeVal });
      metrics.forEach(function(item, index){
        const slot = index + 1;
        params.set('sensor_id' + String(slot), item.sensor);
        params.set('metric' + String(slot), item.metric);
      });
      params.set('sensor_id', metrics[0].sensor);
      const astral = document.getElementById('astral_select');
      params.set('astral', normalizeAstralMode(astral ? astral.value : 'none'));
      if(rangeVal === 'custom'){
        params.set('start', start);
        params.set('end', end);
      }
      switches.forEach(function(item){ params.append('switch_channels', item.switchId + '::' + item.channel); });
      const loading = document.getElementById('graphLoadingState');
      if(loading) loading.style.display = 'inline';
      if(fullscreenGraphAbortController) fullscreenGraphAbortController.abort();
      const requestController = new AbortController();
      fullscreenGraphAbortController = requestController;
      try{
        const response = await fetch('/graph-data?' + params.toString(), {
          cache:'no-store',
          signal:requestController.signal
        });
        const data = await response.json();
        if(token !== fullscreenGraphRequestToken) return;
        if(!response.ok) throw new Error(String((data && (data.detail || data.error)) || ('HTTP ' + response.status)));
        if(data && data.no_data){
          renderFullscreenGraphEmpty(data.detail || 'No data in this time range.');
          return;
        }
        renderGraphFullscreen_V2(data);
      }catch(error){
        if(error && error.name === 'AbortError') return;
        if(token !== fullscreenGraphRequestToken) return;
        console.error('Full-screen graph load failed', error);
        renderFullscreenGraphEmpty('Graph data could not be loaded.');
      }finally{
        if(fullscreenGraphAbortController === requestController) fullscreenGraphAbortController = null;
        if(token === fullscreenGraphRequestToken && loading) loading.style.display = 'none';
      }
    }

    async function initFullscreenGraphWorkspace(){
      if(!fullscreenGraphOptionsReady){
        const deviceLocations = await fullscreenGraphDeviceLocations();
        populateFullscreenSwitchOptions(deviceLocations.switch);
        await populateFullscreenSensorOptions(deviceLocations.sensor);
        const start = document.getElementById('start_time');
        const end = document.getElementById('end_time');
        if(start) start.addEventListener('change', scheduleFullscreenGraphRefresh);
        if(end) end.addEventListener('change', scheduleFullscreenGraphRefresh);
        fullscreenGraphOptionsReady = true;
      }
      updateFullscreenSelectionUi();
      const rangeEl = document.querySelector("input[name='range']:checked");
      const title = document.getElementById('fullscreenGraphRangeTitle');
      if(title) title.textContent = fullscreenRangeTitle(rangeEl ? rangeEl.value : '24h');
      if(selectedFullscreenMetrics().length) await loadFullscreenGraph();
      else renderFullscreenGraphEmpty('Select a sensor metric to begin.');
      if(!fullscreenGraphRealtimeTimer){
        fullscreenGraphRealtimeTimer = window.setInterval(notifyFullscreenGraphDataChanged, 15000);
      }
    }

    window.scheduleFullscreenGraphRefresh = scheduleFullscreenGraphRefresh;
    window.initFullscreenGraphWorkspace = initFullscreenGraphWorkspace;
    window.notifyFullscreenGraphDataChanged = notifyFullscreenGraphDataChanged;

    const vpdBackgroundPlugin = {
      id: 'vpdBackground',
      beforeDraw(chart) {
        const enabled = !!(chart && chart.options && chart.options.plugins &&
                           chart.options.plugins.vpdZones &&
                           chart.options.plugins.vpdZones.enabled);
        if (!enabled) return;

        const ctx = chart.ctx;
        const chartArea = chart.chartArea;
        const scales = chart.scales;
        if (!chartArea) return;

        const zones = [
          { color: '#800080', min: 0.0, max: 0.4 },
          { color: '#3399ff', min: 0.4, max: 0.8 },
          { color: '#add8e6', min: 0.8, max: 1.2 },
          { color: '#66cc66', min: 1.2, max: 1.6 },
          { color: '#f00',    min: 1.6, max: 5.0 }
        ];

        const allScales = scales || {};
        const yScale =
          allScales.y1 ||
          Object.values(allScales).find(s => s && s.axis === 'y');
        if (!yScale) return;

        ctx.save();
        ctx.globalAlpha = 0.3;
        zones.forEach(z => {
          const y1 = yScale.getPixelForValue(z.min);
          const y2 = yScale.getPixelForValue(z.max);
          const top = Math.min(y1, y2);
          const height = Math.abs(y2 - y1);
          ctx.fillStyle = z.color;
          ctx.fillRect(chartArea.left, top, chartArea.right - chartArea.left, height);
        });
        ctx.globalAlpha = 1.0;
        ctx.restore();
      }
    };

    const gaugeZonesBackgroundGraph = {
      id: 'gaugeZonesBackgroundGraph',
      beforeDraw(chart) {
        const zonesByAxis = chart && chart.options && chart.options.plugins &&
          chart.options.plugins.gaugeZonesBackgroundGraph &&
          chart.options.plugins.gaugeZonesBackgroundGraph.zonesByAxis;
        if (!zonesByAxis || typeof zonesByAxis !== 'object') return;
        const ctx = chart.ctx;
        const chartArea = chart.chartArea;
        const scales = chart.scales || {};
        if (!ctx || !chartArea) return;
        ctx.save();
        ctx.globalAlpha = 0.3;
        Object.entries(zonesByAxis).forEach(function(pair){
          const axisId = pair[0];
          const zones = pair[1];
          const yScale = scales[axisId];
          if (!yScale || !Array.isArray(zones) || !zones.length) return;
          zones.forEach(function(z){
            const zMin = Number(z && z.min);
            const zMax = Number(z && z.max);
            const color = (z && z.color) || '';
            if (!Number.isFinite(zMin) || !Number.isFinite(zMax) || !color) return;
            const y1 = yScale.getPixelForValue(zMin);
            const y2 = yScale.getPixelForValue(zMax);
            const top = Math.min(y1, y2);
            const bottom = Math.max(y1, y2);
            const clippedTop = Math.max(chartArea.top, Math.min(chartArea.bottom, top));
            const clippedBottom = Math.max(chartArea.top, Math.min(chartArea.bottom, bottom));
            if (clippedBottom <= clippedTop) return;
            ctx.fillStyle = color;
            ctx.fillRect(chartArea.left, clippedTop, chartArea.right - chartArea.left, clippedBottom - clippedTop);
          });
        });
        ctx.globalAlpha = 1.0;
        ctx.restore();
      }
    };

    function graphSkyYMapper(yBase, topY, bottomY, maxElev, minElev){
      const rawTop = Number(topY);
      const rawBottom = Number(bottomY);
      const top = Number.isFinite(rawTop) ? Math.min(yBase - 1, rawTop) : yBase - 1;
      const bottom = Number.isFinite(rawBottom) ? Math.max(yBase + 1, rawBottom) : yBase + 1;
      const abovePx = Math.max(1, yBase - top);
      const belowPx = Math.max(1, bottom - yBase);
      const maxE = Math.max(1, Math.abs(Number(maxElev) || 1));
      const minE = Math.max(1, Math.abs(Number(minElev) || 1));
      const posScale = abovePx / maxE;
      const negScale = belowPx / minE;
      const bandLimit = Math.max(1, Math.min(maxE, minE));
      const band = Math.max(0.5, Math.min(bandLimit, 12, Math.max(5, bandLimit * 0.35)));
      const hermite = (t, y0, m0, y1, m1, span) => {
        const tt = Math.max(0, Math.min(1, t));
        const t2 = tt * tt;
        const t3 = t2 * tt;
        return ((2 * t3 - 3 * t2 + 1) * y0) + ((t3 - 2 * t2 + tt) * span * m0) + ((-2 * t3 + 3 * t2) * y1) + ((t3 - t2) * span * m1);
      };
      const midSlope = -((posScale + negScale) / 2);
      return (e) => {
        const elev = Number(e);
        if (!Number.isFinite(elev)) return yBase;
        if (elev >= band) return yBase - (Math.min(elev, maxE) * posScale);
        if (elev <= -band) return yBase - (Math.max(elev, -minE) * negScale);
        if (elev < 0) {
          const t = (elev + band) / band;
          return hermite(t, yBase + (band * negScale), -negScale, yBase, midSlope, band);
        }
        return hermite(elev / band, yBase, midSlope, yBase - (band * posScale), -posScale, band);
      };
    }

    function fullscreenAstralSeriesPoints(series){
      const ts = Array.isArray(series && series.ts) ? series.ts : [];
      const vals = Array.isArray(series && series.vals) ? series.vals : [];
      const points = [];
      for (let i = 0; i < ts.length; i++){
        const x = toLocalMs(ts[i]);
        const e = Number(vals[i]);
        if (Number.isFinite(x) && Number.isFinite(e)){
          points.push({ x: x, e: e });
        }
      }
      return points;
    }

    function resizeFullscreenCanvasToDisplay(canvas){
      const rect = canvas.getBoundingClientRect();
      const w = Math.max(1, Math.round(rect.width || canvas.clientWidth || 1));
      const h = Math.max(1, Math.round(rect.height || canvas.clientHeight || 1));
      if (canvas.width !== w || canvas.height !== h){
        canvas.width = w;
        canvas.height = h;
      }
      return { w: w, h: h };
    }

    function fullscreenAstralTickValues(xMin, xMax, targetCount){
      const min = Number(xMin);
      const max = Number(xMax);
      if (!Number.isFinite(min) || !Number.isFinite(max) || max <= min) return [];
      const count = Math.max(2, Math.min(8, Number(targetCount) || 5));
      const out = [];
      for (let i = 0; i < count; i++){
        out.push(min + (((max - min) * i) / (count - 1)));
      }
      return out;
    }

    function fullscreenAstralLabel(ms, spanMs){
      const d = new Date(ms);
      if (!Number.isFinite(d.getTime())) return '';
      if (spanMs <= 36 * 3600 * 1000){
        return d.toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
      }
      return d.toLocaleDateString(undefined, { month:'short', day:'numeric' });
    }

    function drawFullscreenAstralGraph(payload, xMin, xMax){
      const canvas = document.getElementById('fullscreen_astral_graph');
      if (!canvas) return;
      const size = resizeFullscreenCanvasToDisplay(canvas);
      const ctx = canvas.getContext('2d');
      const w = size.w;
      const h = size.h;
      const padL = 44;
      const padR = 16;
      const padT = 12;
      const padB = 34;
      const graphW = Math.max(1, w - padL - padR);
      const graphH = Math.max(48, h - padT - padB);
      const yBase = padT + (graphH * 0.56);
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = '#fff9d6';
      ctx.fillRect(0, 0, w, h);
      ctx.fillStyle = '#dff1ff';
      ctx.fillRect(padL, padT, graphW, Math.max(1, yBase - padT));
      ctx.fillStyle = '#000000';
      ctx.fillRect(padL, yBase, graphW, Math.max(1, (padT + graphH) - yBase));
      ctx.strokeStyle = '#8fa4b3';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padL, yBase);
      ctx.lineTo(w - padR, yBase);
      ctx.stroke();

      const series = (payload && payload.series) || {};
      const sunPoints = fullscreenAstralSeriesPoints(series.sun);
      const moonPoints = fullscreenAstralSeriesPoints(series.moon);
      const allPoints = sunPoints.concat(moonPoints);
      const minX = Number.isFinite(Number(xMin)) ? Number(xMin) : Math.min(...allPoints.map((p) => p.x));
      const maxX = Number.isFinite(Number(xMax)) ? Number(xMax) : Math.max(...allPoints.map((p) => p.x));
      const spanMs = Math.max(1, maxX - minX);
      const ticks = fullscreenAstralTickValues(minX, maxX, w < 700 ? 4 : 6);
      const xForMs = (ms) => padL + (((Math.max(minX, Math.min(maxX, ms)) - minX) / spanMs) * graphW);

      ctx.save();
      ctx.strokeStyle = 'rgba(39, 49, 58, 0.22)';
      ctx.fillStyle = '#27313a';
      ctx.font = '10px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.textBaseline = 'top';
      ticks.forEach((tick, idx) => {
        const x = xForMs(tick);
        ctx.beginPath();
        ctx.moveTo(x, padT);
        ctx.lineTo(x, padT + graphH);
        ctx.stroke();
        ctx.textAlign = idx === 0 ? 'left' : (idx === ticks.length - 1 ? 'right' : 'center');
        ctx.fillText(fullscreenAstralLabel(tick, spanMs), idx === 0 ? x + 2 : (idx === ticks.length - 1 ? x - 2 : x), padT + graphH + 5);
      });
      ctx.restore();

      const elevs = allPoints.map((p) => p.e).filter((v) => Number.isFinite(v));
      if (!elevs.length){
        ctx.fillStyle = '#27313a';
        ctx.font = '13px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        const detail = String((payload && payload.detail) || 'Astral data unavailable');
        ctx.fillText(detail, padL + (graphW / 2), padT + (graphH / 2));
        return;
      }

      const posElevs = elevs.filter((v) => v > 0);
      const negElevs = elevs.filter((v) => v < 0);
      const maxElev = Math.max(20, ...(posElevs.length ? posElevs : [20]));
      const minElev = Math.min(-18, ...(negElevs.length ? negElevs : [-18]));
      const yForElev = graphSkyYMapper(yBase, padT + 2, padT + graphH - 2, maxElev, minElev);
      const curveHermite = (t, y0, s0, y1, s1, span) => {
        const tt = Math.max(0, Math.min(1, t));
        const t2 = tt * tt;
        const t3 = t2 * tt;
        return ((2 * t3 - 3 * t2 + 1) * y0) + ((t3 - 2 * t2 + tt) * span * s0) + ((-2 * t3 + 3 * t2) * y1) + ((t3 - t2) * span * s1);
      };

      const buildSmoothAstralKeys = (points) => {
        const sorted = Array.isArray(points)
          ? points.filter((p) => Number.isFinite(p && p.x) && Number.isFinite(p && p.e)).sort((a, b) => a.x - b.x)
          : [];
        if (sorted.length < 2) return [];
        const enriched = [sorted[0]];
        for (let i = 1; i < sorted.length; i++){
          const prev = sorted[i - 1];
          const next = sorted[i];
          if (prev.x < next.x && prev.e !== 0 && next.e !== 0 && ((prev.e < 0 && next.e > 0) || (prev.e > 0 && next.e < 0))){
            const f = Math.max(0, Math.min(1, (0 - prev.e) / (next.e - prev.e)));
            enriched.push({ x: prev.x + ((next.x - prev.x) * f), e: 0, crossing: true });
          }
          enriched.push(next);
        }
        const boundaryIndexes = [];
        enriched.forEach((p, idx) => {
          if (idx === 0 || idx === enriched.length - 1 || p.crossing) boundaryIndexes.push(idx);
        });
        const keys = [];
        const pushKey = (raw, kind) => {
          if (!raw || !Number.isFinite(raw.x) || !Number.isFinite(raw.e)) return;
          const y = raw.crossing || Math.abs(raw.e) < 0.0001 ? yBase : yForElev(raw.e);
          const last = keys[keys.length - 1];
          const key = { x: raw.x, y: y, e: raw.e, kind: kind || (raw.crossing ? 'crossing' : 'point'), s: 0 };
          if (last && Math.abs(last.x - key.x) < 1) {
            keys[keys.length - 1] = key;
          } else {
            keys.push(key);
          }
        };
        for (let b = 0; b < boundaryIndexes.length - 1; b++){
          const startIdx = boundaryIndexes[b];
          const endIdx = boundaryIndexes[b + 1];
          const seg = enriched.slice(startIdx, endIdx + 1).filter((p) => Number.isFinite(p && p.x) && Number.isFinite(p && p.e));
          if (seg.length < 2) continue;
          pushKey(seg[0], seg[0].crossing ? 'crossing' : 'point');
          const inner = seg.filter((p) => !p.crossing && p.x > seg[0].x && p.x < seg[seg.length - 1].x);
          if (inner.length){
            const signProbe = inner.reduce((acc, p) => acc + p.e, 0);
            const positiveLobe = signProbe >= 0;
            let extreme = inner[0];
            inner.forEach((p) => {
              if (positiveLobe ? p.e > extreme.e : p.e < extreme.e) extreme = p;
            });
            pushKey(extreme, 'extreme');
          }
          pushKey(seg[seg.length - 1], seg[seg.length - 1].crossing ? 'crossing' : 'point');
        }
        if (keys.length < 2) return [];
        keys.sort((a, b) => a.x - b.x);
        for (let i = 0; i < keys.length; i++){
          const key = keys[i];
          if (key.kind === 'extreme'){
            key.s = 0;
            continue;
          }
          const prev = keys[i - 1];
          const next = keys[i + 1];
          if (key.kind === 'crossing' && prev && next && prev.x < key.x && key.x < next.x){
            const leftMag = (Math.abs(key.y - prev.y) * Math.PI) / (2 * Math.max(1, key.x - prev.x));
            const rightMag = (Math.abs(next.y - key.y) * Math.PI) / (2 * Math.max(1, next.x - key.x));
            const sign = next.y < prev.y ? -1 : 1;
            key.s = sign * Math.min(leftMag, rightMag);
          } else if (prev && next && prev.x < next.x) {
            key.s = (next.y - prev.y) / (next.x - prev.x);
          } else if (next && key.x < next.x) {
            key.s = (next.y - key.y) / (next.x - key.x);
          } else if (prev && prev.x < key.x) {
            key.s = (key.y - prev.y) / (key.x - prev.x);
          } else {
            key.s = 0;
          }
        }
        return keys;
      };

      const drawPath = (points, color, width) => {
        if (!Array.isArray(points) || points.length < 2) return;
        const keys = buildSmoothAstralKeys(points)
          .filter((p) => p.x >= minX && p.x <= maxX && Number.isFinite(p.y) && Number.isFinite(p.s));
        if (keys.length < 2) return;
        ctx.save();
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        ctx.lineCap = 'round';
        ctx.lineJoin = 'round';
        ctx.beginPath();
        ctx.moveTo(xForMs(keys[0].x), keys[0].y);
        const pxStepMs = Math.max(1, spanMs / Math.max(80, Math.min(600, graphW)));
        for (let i = 0; i < keys.length - 1; i++){
          const a = keys[i];
          const b = keys[i + 1];
          const span = Math.max(1, b.x - a.x);
          for (let x = a.x + pxStepMs; x < b.x; x += pxStepMs){
            ctx.lineTo(xForMs(x), curveHermite((x - a.x) / span, a.y, a.s, b.y, b.s, span));
          }
          ctx.lineTo(xForMs(b.x), b.y);
        }
        ctx.stroke();
        ctx.restore();
      };

      drawPath(sunPoints, '#f3d34a', 2.1);
      drawPath(moonPoints, '#69bdf2', 1.75);

      ctx.save();
      ctx.font = '11px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
      ctx.textBaseline = 'top';
      ctx.textAlign = 'right';
      let labelX = w - padR - 4;
      if (sunPoints.length){
        ctx.fillStyle = '#8d7115';
        ctx.fillText('Sun', labelX - (moonPoints.length ? 44 : 0), padT + 4);
      }
      if (moonPoints.length){
        ctx.fillStyle = '#2e78a9';
        ctx.fillText('Moon', labelX, padT + 4);
      }
      ctx.fillStyle = '#27313a';
      ctx.textAlign = 'left';
      ctx.fillText('Astral Position', padL + 4, padT + 4);
      ctx.restore();
    }

    function renderGraphFullscreen_V2(data){
      const canvas = document.getElementById('fullscreen_graph');
      if (!canvas) return;
      const ctx = canvas.getContext('2d');
      const fullscreenContainer = document.getElementById('fullscreen_graph_container');
      if (fullscreenContainer) fullscreenContainer.style.display = 'flex';

      const win = (data && data.window) || {};
      const xMin = localOrUndef(
        win.since_iso !== undefined ? win.since_iso : win.since_epoch_s
      );
      const xMax = localOrUndef(
        win.until_iso !== undefined ? win.until_iso : win.until_epoch_s
      );
      const xSpanMs = (Number.isFinite(xMin) && Number.isFinite(xMax)) ? (xMax - xMin) : 0;
      const useDailyXAxis = xSpanMs > (24 * 3600 * 1000);
      const useSixHourXAxis = useDailyXAxis && xSpanMs < (10 * 24 * 3600 * 1000);

      const datasets = [];
      const series = (data && data.series) || {};
      const avgAll = (data && (data.simple_avg || data.rolling_ema)) || {};
      const keys = Object.keys(series || {});
      let leftAssigned = false;
      const baseColors = ['#1f77b4', '#2ca02c', '#7f3fbf', '#d97816'];
      const gaugeZonesByAxis = {};
      const gaugeAxisBounds = {};
      const averageAxisLabels = { y1: [], y2: [] };

      keys.forEach(function(k, idx){
        const entry = series[k] || {};
        const ts   = entry.ts   || [];
        const vals = entry.vals || [];
        const metricName = graphMetricNameFromKey(k);
        const gaugeEntry = graphGaugeMetricEntry(metricName);
        const metricUnit = String((gaugeEntry && gaugeEntry.config && gaugeEntry.config.unit) || '').trim();
        const points = [];
        for (let i = 0; i < ts.length; i++){
          const x = toLocalMs(ts[i]);
          const y = graphDisplayValue(vals[i], metricName, false);
          if (Number.isFinite(x) && Number.isFinite(y)){
            points.push({ x: x, y: y });
          }
        }
        const yAxisID = leftAssigned ? 'y2' : 'y1';
        if (!leftAssigned) leftAssigned = true;

        const metricZones = soilFertilityGaugeZones(metricName);
        if (metricZones && !gaugeZonesByAxis[yAxisID]) {
          gaugeZonesByAxis[yAxisID] = metricZones.zones;
          if (Number.isFinite(metricZones.min) && Number.isFinite(metricZones.max)) {
            gaugeAxisBounds[yAxisID] = { min: metricZones.min, max: metricZones.max };
          }
        }

        const baseColor = baseColors[idx % baseColors.length];
        datasets.push({
          label: ((data.display_names && data.display_names[k]) || k) + (metricUnit ? ' (' + metricUnit + ')' : ''),
          data: points,
          borderColor: baseColor,
          yAxisID: yAxisID,
          pressureMetric: graphPressureMetric(metricName),
          order: 1,
          tension: 0.2,
          pointRadius: (points.length <= 1 ? 3 : 0),
          pointHoverRadius: (points.length <= 1 ? 4 : 3)
        });

        const roll = (avgAll && avgAll[k]) || {};
        const rollTs = roll.ts || [];
        const rollVals = roll.vals || [];
        if (rollTs.length && rollVals.length){
          const rollPoints = [];
          for (let i = 0; i < rollTs.length; i++){
            const x = toLocalMs(rollTs[i]);
            const y = graphDisplayValue(rollVals[i], metricName, false);
            if (Number.isFinite(x) && Number.isFinite(y)){
              rollPoints.push({ x: x, y: y });
            }
          }
          if (rollPoints.length){
            const averageValue = rollPoints[0].y;
            const averageText = graphPressureMetric(metricName)
              ? Number(averageValue).toFixed(1)
              : Number(Number(averageValue).toFixed(2)).toString();
            averageAxisLabels[yAxisID].push({
              value: Number(averageValue),
              label: 'Avg ' + averageText + (metricUnit ? ' ' + metricUnit : '')
            });
            datasets.push({
              label: ((data.display_names && data.display_names[k]) || k) +
                " (Average: " + averageText + (metricUnit ? ' ' + metricUnit : '') + ")",
              data: rollPoints,
              borderColor: 'purple',
              borderDash: [6, 3],
              yAxisID: yAxisID,
              pressureMetric: graphPressureMetric(metricName),
              order: 2,
              tension: 0.2,
              pointRadius: (rollPoints.length <= 1 ? 3 : 0),
              pointHoverRadius: (rollPoints.length <= 1 ? 4 : 3)
            });
          }
        }
      });

      function isVPDLabel(s){
        return /(^|\b)(ambient\s+)?vpd(\b|$)/i.test(String(s));
      }
      const leftName   = datasets[0] ? (datasets[0].label || '') : '';
      const rightNames = datasets.filter(function(d){ return d.yAxisID === 'y2'; })
                                 .map(function(d){ return d.label; });

      const leftIsVPD  = isVPDLabel(leftName);
      const rightIsVPD = rightNames.some(isVPDLabel);
      const leftIsBarometricPressure = datasets.some(function(d){ return d.yAxisID === 'y1' && d.pressureMetric; });
      const rightIsBarometricPressure = datasets.some(function(d){ return d.yAxisID === 'y2' && d.pressureMetric; });
      const anyVPD     = leftIsVPD || rightIsVPD;

      const legendContainer = document.getElementById('switch_legend');
      if (legendContainer) legendContainer.innerHTML = '';
      const allAnnotations = {};
      if (data && data.switch_lines){
        let colorIdx = 0;
        const pal = [
          { on:'#006400', off:'#8B0000' },
          { on:'#228B22', off:'#B22222' },
          { on:'#2F4F4F', off:'#A0522D' },
          { on:'#008080', off:'#8B4513' }
        ];
        Object.entries(data.switch_lines).forEach(function(pair){
          const label  = pair[0];
          const events = pair[1] || [];
          const colors = pal[colorIdx % pal.length];
          colorIdx += 1;

          if (legendContainer){
            const el = document.createElement('div');
            el.innerHTML =
              "<span style='color:" + colors.on  + "'>&#9632;</span> " +
              label + " (ON), " +
              "<span style='color:" + colors.off + "'>&#9632;</span> OFF";
            legendContainer.appendChild(el);
          }

          events.forEach(function(ev, i){
            const stamp = ev[0];
            const state = ev[1];
            const tMs = toLocalMs(stamp);
            if (!Number.isFinite(tMs)) return;
            const id = label + "_" + (state ? "on" : "off") + "_" + String(i);
            allAnnotations[id] = {
              type: 'line',
              xMin: tMs,
              xMax: tMs,
              borderColor: state ? colors.on : colors.off,
              borderWidth: 2
            };
          });
        });
      }

      function isMidnight(ms){
        const d = new Date(ms);
        return d.getHours() === 0 && d.getMinutes() === 0;
      }
      function fmtDate(ms){
        return new Date(ms).toLocaleDateString(undefined, { month:'short', day:'numeric' });
      }
      function fmtTime(ms){
        return new Date(ms).toLocaleTimeString([], { hour:'2-digit', minute:'2-digit' });
      }
      function fmtDayMarkerTime(ms){
        return new Date(ms).toLocaleTimeString([], { hour:'numeric' });
      }

      if (window.graphChart){
        window.graphChart.destroy();
      }

      const astralPayload = (data && data.astral) || null;
      const astralMode = normalizeAstralMode(astralPayload && astralPayload.mode);
      const showAstral = astralMode !== 'none';
      if (fullscreenContainer) fullscreenContainer.classList.toggle('has-astral', showAstral);
      if (!showAstral){
        window.__lastFullscreenAstralGraph = null;
        const astralCanvas = document.getElementById('fullscreen_astral_graph');
        if (astralCanvas){
          const astralCtx = astralCanvas.getContext('2d');
          astralCtx.clearRect(0, 0, astralCanvas.width, astralCanvas.height);
        }
      }

      const axisTitles = {
        y1: (data.axis_titles && data.axis_titles.y1) || 'Left',
        y2: (data.axis_titles && data.axis_titles.y2) ||
            (rightNames.join(' / ') || '')
      };

      const formatYAxisTick = function(val, pressureAxis){
        const num = Number(val);
        if (!Number.isFinite(num)) return val;
        if (pressureAxis) return num.toFixed(1);
        return Number(num.toFixed(2)).toString();
      };

      const averageLabelsForTick = function(axisID, val){
        const num = Number(val);
        if (!Number.isFinite(num)) return [];
        return (averageAxisLabels[axisID] || []).filter(function(item){
          const tolerance = Math.max(1e-9, Math.abs(item.value) * 1e-9);
          return Math.abs(item.value - num) <= tolerance;
        }).map(function(item){ return item.label; });
      };

      const addAverageAxisTicks = function(axis, axisID){
        const ticks = Array.isArray(axis.ticks) ? axis.ticks : [];
        (averageAxisLabels[axisID] || []).forEach(function(item){
          if (!Number.isFinite(item.value) || item.value < axis.min || item.value > axis.max) return;
          if (!averageLabelsForTick(axisID, item.value).length) return;
          const exists = ticks.some(function(tick){
            const value = Number(tick && tick.value);
            const tolerance = Math.max(1e-9, Math.abs(item.value) * 1e-9);
            return Number.isFinite(value) && Math.abs(value - item.value) <= tolerance;
          });
          if (!exists) ticks.push({ value: item.value, major: true });
        });
        ticks.sort(function(a, b){ return Number(a.value) - Number(b.value); });
        axis.ticks = ticks;
      };

      const yAxisTickOptions = function(axisID, pressureAxis){
        return {
          autoSkip: false,
          callback: function(val){
            const averageLabels = averageLabelsForTick(axisID, val);
            return averageLabels.length ? averageLabels.join(' / ') : formatYAxisTick(val, pressureAxis);
          },
          color: function(context){
            return averageLabelsForTick(axisID, context && context.tick ? context.tick.value : undefined).length
              ? 'purple'
              : Chart.defaults.color;
          },
          font: function(context){
            return averageLabelsForTick(axisID, context && context.tick ? context.tick.value : undefined).length
              ? { weight: 'bold' }
              : {};
          }
        };
      };

      const y1Opts = {
        position: 'left',
        beginAtZero: false,
        title: { display: true, text: axisTitles.y1 },
        afterBuildTicks: function(axis){ addAverageAxisTicks(axis, 'y1'); },
        ticks: yAxisTickOptions('y1', leftIsBarometricPressure)
      };
      const y2Opts = {
        position: 'right',
        beginAtZero: false,
        title: { display: (keys.length > 1), text: axisTitles.y2 },
        grid: { drawOnChartArea: false },
        display: (keys.length > 1),
        afterBuildTicks: function(axis){ addAverageAxisTicks(axis, 'y2'); },
        ticks: yAxisTickOptions('y2', rightIsBarometricPressure)
      };

      if (leftIsVPD){
        y1Opts.min = 0;
        y1Opts.max = 5;
      }
      if (rightIsVPD){
        y2Opts.min = 0;
        y2Opts.max = 5;
      }
      if (gaugeAxisBounds.y1){
        y1Opts.min = gaugeAxisBounds.y1.min;
        y1Opts.max = gaugeAxisBounds.y1.max;
      }
      if (gaugeAxisBounds.y2){
        y2Opts.min = gaugeAxisBounds.y2.min;
        y2Opts.max = gaugeAxisBounds.y2.max;
      }

      const annotationPlugin = Chart.registry.getPlugin('annotation');
      const pluginsArr = [vpdBackgroundPlugin, gaugeZonesBackgroundGraph];
      if (annotationPlugin){
        pluginsArr.push(annotationPlugin);
      }

      window.graphChart = new Chart(ctx, {
        type: 'line',
        data: { datasets: datasets },
        options: {
          parsing: false,
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          scales: {
            x: {
              type: 'time',
              min: xMin,
              max: xMax,
              afterBuildTicks: function(axis){
                if (!useSixHourXAxis || !Number.isFinite(xMin) || !Number.isFinite(xMax)) return;
                const cursor = new Date(xMin);
                cursor.setMinutes(0, 0, 0);
                const remainder = cursor.getHours() % 6;
                if (remainder !== 0) cursor.setHours(cursor.getHours() + (6 - remainder));
                if (cursor.getTime() < xMin) cursor.setHours(cursor.getHours() + 6);
                const sixHourTicks = [];
                while (cursor.getTime() <= xMax){
                  sixHourTicks.push({ value: cursor.getTime() });
                  cursor.setHours(cursor.getHours() + 6);
                }
                axis.ticks = sixHourTicks;
              },
              time: {
                unit: useSixHourXAxis ? 'hour' : (useDailyXAxis ? 'day' : 'hour'),
                stepSize: useSixHourXAxis ? 6 : 1,
                tooltipFormat: 'PP p'
              },
              title: {
                display: true,
                text: (typeof TZ_NAME === 'string' && TZ_NAME)
                  ? ('Time (' + TZ_NAME + ')')
                  : 'Time'
              },
              ticks: {
                source: 'auto',
                autoSkip: !useDailyXAxis,
                maxRotation: 0,
                callback: function(val, idx, ticks){
                  const tval = (ticks[idx] && ('value' in ticks[idx]))
                    ? ticks[idx].value
                    : val;
                  if (isMidnight(tval)) return fmtDate(tval);
                  return useSixHourXAxis ? fmtDayMarkerTime(tval) : fmtTime(tval);
                }
              },
              grid: {
                color: function(c){
                  const v = c && c.tick ? c.tick.value : undefined;
                  return isMidnight(v)
                    ? 'rgba(0,0,0,0.25)'
                    : (useSixHourXAxis ? 'rgba(0,0,0,0.16)' : 'rgba(0,0,0,0.1)');
                },
                lineWidth: function(c){
                  const v = c && c.tick ? c.tick.value : undefined;
                  return isMidnight(v) ? 1.5 : 1;
                }
              }
            },
            y1: y1Opts,
            y2: y2Opts
          },
          plugins: {
            annotation: { annotations: allAnnotations },
            vpdZones: { enabled: anyVPD },
            gaugeZonesBackgroundGraph: { zonesByAxis: gaugeZonesByAxis },
            tooltip: { callbacks: { label: function(context){
              const label = context.dataset.label || '';
              const numeric = Number(context.parsed && context.parsed.y);
              if (!Number.isFinite(numeric)) return label;
              const text = context.dataset.pressureMetric
                ? numeric.toFixed(1)
                : Number(numeric.toFixed(2)).toString();
              return label ? (label + ': ' + text) : text;
            } } }
          }
        },
        plugins: pluginsArr
      });
      if (showAstral){
        window.__lastFullscreenAstralGraph = { payload: astralPayload, xMin: xMin, xMax: xMax };
        requestAnimationFrame(function(){
          drawFullscreenAstralGraph(astralPayload, xMin, xMax);
          if (window.graphChart && typeof window.graphChart.resize === 'function') window.graphChart.resize();
        });
      }
    }

    function closeFullscreenGraph(){
      const cont = document.getElementById('fullscreen_graph_container');
      if (cont) cont.style.display = 'none';
      window.setMobileWorkspaceViewport?.(cont, false);
      if (cont) cont.classList.remove('has-astral');
      fullscreenGraphRequestToken += 1;
      if(fullscreenGraphRefreshTimer) window.clearTimeout(fullscreenGraphRefreshTimer);
      if(fullscreenGraphAbortController){
        fullscreenGraphAbortController.abort();
        fullscreenGraphAbortController = null;
      }
      if (window.graphChart){
        window.graphChart.destroy();
        window.graphChart = null;
      }
      window.__lastFullscreenAstralGraph = null;
    }

    if (!window.__fullscreenAstralResizeBound){
      window.__fullscreenAstralResizeBound = true;
      window.addEventListener('resize', function(){
        const cont = document.getElementById('fullscreen_graph_container');
        if (!cont || window.getComputedStyle(cont).display === 'none') return;
        const last = window.__lastFullscreenAstralGraph;
        if (last) drawFullscreenAstralGraph(last.payload, last.xMin, last.xMax);
      });
    }

    window.openGraphModal = async function(){
      window.ModalBusyCursor.begin();
      try {
        const workspace = document.getElementById('fullscreen_graph_container');
        if (workspace){
          window.setMobileWorkspaceViewport?.(workspace, true,
            '(max-width: 760px), (max-device-width: 760px), (pointer: coarse) and (max-height: 600px)');
          workspace.style.display = 'flex';
          workspace.querySelector('.fullscreen-graph-body').scrollTop = 0;
          if (window.ensureButtonTooltips) window.ensureButtonTooltips(workspace);
          await window.ModalBusyCursor.untilPaint();
          await initFullscreenGraphWorkspace();
        }
      } finally {
        window.ModalBusyCursor.end();
      }
    };
