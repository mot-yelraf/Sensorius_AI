/* Share manual ordering between dashboard controls and General Settings. */
(() => {
  const sensorGroups = () => Array.from(document.querySelectorAll('.dashboard-content > .sensor-group[data-dashboard-row-id]'));
  const switchCards = () => Array.from(document.querySelectorAll('#row_switches > .switch-metric-container[data-switch-location]'));

  function fillList(listId, elements, key, title, attribute) {
    const list = document.getElementById(listId);
    if (!list) return;
    list.replaceChildren();
    elements.forEach((element, index) => {
      const id = key(element);
      const label = title(element);
      const row = document.createElement('div');
      row.className = 'dashboard-order-item';
      row.setAttribute('role', 'listitem');
      row.dataset.rowId = id;
      const name = document.createElement('span');
      name.textContent = label;
      row.appendChild(name);
      for (const direction of ['up', 'down']) {
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'button neutral';
        button.textContent = direction === 'up' ? 'Move up' : 'Move down';
        button.setAttribute('aria-label', `Move ${label} ${direction}`);
        button.setAttribute(attribute, direction);
        button.dataset.rowId = id;
        button.disabled = direction === 'up' ? index === 0 : index === elements.length - 1;
        row.appendChild(button);
      }
      list.appendChild(row);
    });
    if (!elements.length) list.textContent = 'No rows are available.';
  }

  function renderRows() {
    fillList('dashboard-row-order-list', sensorGroups(), element => element.dataset.dashboardRowId,
      element => element.querySelector('.sensor-group-title')?.textContent.trim().replace(/\s+/g, ' ') || element.dataset.dashboardRowId,
      'data-dashboard-move');
    const cards = switchCards();
    fillList('dashboard-switch-order-list', cards, element => element.dataset.switchLocation,
      element => element.querySelector('.switch-device-title')?.textContent.trim().replace(/\s+/g, ' ') || element.dataset.switchLocation,
      'data-switch-card-move');
    cards.forEach((card, index) => {
      card.querySelectorAll('[data-switch-card-move]').forEach(button => {
        button.disabled = button.dataset.switchCardMove === 'up' ? index === 0 : index === cards.length - 1;
      });
    });
  }

  function saved(field, message) {
    const select = document.getElementById(field);
    if (select) select.value = 'existing';
    const status = document.getElementById('dashboard-row-order-status');
    if (status) status.textContent = message;
  }

  let moving = false;
  document.addEventListener('toggle', event => {
    if (event.target.matches('.dashboard-order-section') && event.target.open) renderRows();
  }, true);
  document.addEventListener('DOMContentLoaded', renderRows);
  document.addEventListener('click', async event => {
    const button = event.target.closest('[data-dashboard-move], [data-switch-card-move]');
    if (!button || moving) return;
    moving = true;
    button.disabled = true;
    try {
      if (button.hasAttribute('data-dashboard-move')) {
        if (typeof window.reorderSensorGroup !== 'function') return;
        const result = await window.reorderSensorGroup(button.dataset.rowId, button.dataset.dashboardMove);
        if (result?.moved) saved('sensor_row_order', 'Dashboard row order saved.');
      } else {
        const cards = switchCards();
        const key = button.dataset.rowId || button.closest('[data-switch-location]')?.dataset.switchLocation;
        const index = cards.findIndex(card => card.dataset.switchLocation === key);
        const nextIndex = index + (button.dataset.switchCardMove === 'up' ? -1 : 1);
        if (index < 0 || nextIndex < 0 || nextIndex >= cards.length) return;
        [cards[index], cards[nextIndex]] = [cards[nextIndex], cards[index]];
        const response = await fetch('/dashboard/switch-order', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ order: cards.map(card => card.dataset.switchLocation) }),
        });
        if (!response.ok) throw new Error('Failed to save switch card order');
        const parent = cards[0].parentElement;
        cards.forEach(card => parent.appendChild(card));
        saved('switch_row_order', 'Switch card order saved.');
      }
    } catch (error) {
      const status = document.getElementById('dashboard-row-order-status');
      if (status) status.textContent = 'Could not save dashboard order. Please try again.';
      if (typeof window.showToast === 'function') window.showToast('Could not save dashboard order', 'error');
    } finally {
      moving = false;
      renderRows();
    }
  });
})();
