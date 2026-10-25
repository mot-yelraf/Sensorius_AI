/* Keep the dashboard's At a Glance tile order in this browser. */
(() => {
  const row = document.querySelector('.dash-top-row');
  if (!row) return;
  const names = {
    bioBox: 'Biodynamic Calendar', moonBox: 'Moon Phase',
    displayDeviceBox: 'Display Device By', sunBox: 'Sun Position',
    weatherForecastBox: 'Weather Forecast',
  };
  const storageKey = 'sensorius.atAGlanceOrder';
  const tiles = () => Array.from(row.children).filter(tile => Object.hasOwn(names, tile.id));
  const updateButtons = () => {
    const current = tiles();
    current.forEach((tile, index) => {
      tile.querySelector('[data-direction="left"]').disabled = index === 0;
      tile.querySelector('[data-direction="right"]').disabled = index === current.length - 1;
    });
  };
  tiles().forEach(tile => {
    const controls = document.createElement('div');
    controls.className = 'at-a-glance-order-controls';
    controls.setAttribute('role', 'group');
    controls.setAttribute('aria-label', `Reorder ${names[tile.id]}`);
    controls.addEventListener('keydown', event => event.stopPropagation());
    controls.addEventListener('click', event => event.stopPropagation());
    ['left', 'right'].forEach(direction => {
      const button = document.createElement('button');
      button.type = 'button';
      button.dataset.direction = direction;
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('viewBox', '0 9 32 14');
      svg.setAttribute('width', '16');
      svg.setAttribute('height', '7');
      svg.setAttribute('aria-hidden', 'true');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', direction === 'left' ? 'M30 16H3 M10 10L3 16L10 22' : 'M2 16H29 M22 10L29 16L22 22');
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', 'currentColor');
      path.setAttribute('stroke-width', '2');
      path.setAttribute('stroke-linecap', 'round');
      path.setAttribute('stroke-linejoin', 'round');
      svg.appendChild(path);
      button.appendChild(svg);
      button.title = `Move ${names[tile.id]} ${direction}`;
      button.setAttribute('aria-label', button.title);
      button.addEventListener('click', () => {
        const current = tiles();
        const index = current.indexOf(tile);
        const neighbor = current[index + (direction === 'left' ? -1 : 1)];
        if (!neighbor) return;
        if (direction === 'left') row.insertBefore(tile, neighbor);
        else row.insertBefore(neighbor, tile);
        updateButtons();
        try {
          window.localStorage.setItem(storageKey, JSON.stringify(tiles().map(item => item.id)));
        } catch (_) { /* Reordering still works when browser storage is unavailable. */ }
        if (button.disabled) tile.querySelector('button[data-direction]:not(:disabled)')?.focus();
        else button.focus();
      });
      controls.appendChild(button);
    });
    tile.appendChild(controls);
  });
  try {
    const saved = JSON.parse(window.localStorage.getItem(storageKey));
    if (Array.isArray(saved)) {
      const current = tiles();
      const byId = new Map(current.map(tile => [tile.id, tile]));
      const ordered = [...new Set(saved)].map(id => byId.get(id)).filter(Boolean);
      ordered.push(...current.filter(tile => !ordered.includes(tile)));
      const fragment = document.createDocumentFragment();
      ordered.forEach(tile => fragment.appendChild(tile));
      // Insert before the overlays, which also live inside this grid.
      row.insertBefore(fragment, row.firstChild);
    }
  } catch (_) { /* Invalid or unavailable storage leaves the default order intact. */ }
  updateButtons();
})();
