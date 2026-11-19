import {expect, test} from '@playwright/test';

for (const width of [1280, 390]) {
  test(`library selects across stages, reviews bindings, retries and imports disabled copies at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 900});
    const catalog = await (await page.request.get('/advanced/automation-library')).json();
    const rules = [{rule_id: 'existing', enabled: false, group: 'Other', script_json: JSON.stringify({name: 'Existing rule', conditions: [], actions: []})}];
    const planting = {id: 'lettuce-bed', name: 'Lettuce bed', plant_part: 'Leaf', environment: 'Greenhouse',
      stage_schedule: {seedling: {start: '2027-01-10', end: '2027-01-20'}, mature: {start: '2027-02-01', end: '2027-02-20'}}};
    await page.route('**/advanced/automation-library', route => route.fulfill({json: {...catalog, groups: ['Other', 'Nursery']}}));
    await page.route('**/advanced/automations?*', route => route.fulfill({json: {items: rules, groups: ['Other', 'Nursery']}}));
    await page.route('**/api/biodynamic-calendar-app/plantings', route => route.fulfill({json: {ok: true, plantings: [planting]}}));
    await page.route('**/sensor-directory', route => route.fulfill({json: [{id: 'air', label: 'Climate'}]}));
    await page.route('**/sensor-metrics?*', route => route.fulfill({json: {Temperature: {native_unit: '°C', unit: '°F', factor: 1.8, offset: 32}}}));
    await page.route('**/automation-context', route => route.fulfill({json: {actors: [], executor_switch_id: '__system__', email_enabled: false}}));
    const imports = [];
    await page.route('**/advanced/automation-library/import', async route => {
      const body = route.request().postDataJSON();
      imports.push(body);
      if (imports.length === 1) return route.fulfill({status: 503, json: {error: 'Temporary import failure'}});
      const ids = body.entries.map((entry, index) => {
        const template = catalog.templates.find(item => item.id === entry.template_id);
        const id = `imported-${index}`;
        rules.push({rule_id: id, enabled: false, group: body.group,
          library_json: JSON.stringify({planting_id: entry.planting_id}),
          script_json: JSON.stringify({name: `Lettuce / ${catalog.stages[entry.stage]} / ${template.title}`, enabled: false,
            conditions: [{type: 'calendar', calendar_kind: 'date_range', calendar_start: entry.start, calendar_end: entry.end, calendar_timezone: 'UTC'},
              {type: 'calendar', calendar_kind: 'bd_transitions', calendar_event: 'water', executor_switch_id: '__system__'}],
            actions: [{type: 'none', executor_switch_id: '__system__'}]})});
        return id;
      });
      await route.fulfill({json: {ok: true, rule_ids: ids}});
    });
    await page.goto('/');
    await page.getByRole('link', {name: 'Open General Settings'}).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', {name: 'Automations', exact: true}).click();
    await modal.getByRole('button', {name: 'Library', exact: true}).click();
    const library = modal.locator('#automationLibrary');
    await expect(library.locator('.library-browse-filters select')).toHaveCount(4);
    const filterBoxes = await library.locator('.library-browse-filters > label').evaluateAll(fields => fields.map(field => {
      const bounds = field.getBoundingClientRect();
      return {x: bounds.x, y: bounds.y, width: bounds.width};
    }));
    for (let index = 1; index < filterBoxes.length; index++) {
      if (width > 700) {
        expect(Math.abs(filterBoxes[index].y - filterBoxes[0].y)).toBeLessThan(1);
        expect(Math.abs(filterBoxes[index].width - filterBoxes[0].width)).toBeLessThan(1);
        expect(filterBoxes[index].x).toBeGreaterThan(filterBoxes[index - 1].x);
      } else {
        expect(filterBoxes[index].x).toBe(filterBoxes[0].x);
        expect(filterBoxes[index].y).toBeGreaterThan(filterBoxes[index - 1].y);
      }
    }
    await page.screenshot({path: test.info().outputPath('library-filter-columns.png')});
    await library.getByLabel('Crop type', {exact: true}).selectOption('Leaf');
    let bundle = library.locator('.library-bundle');
    await expect(bundle).toHaveCount(1);
    await expect(bundle).not.toHaveAttribute('open', '');
    await bundle.locator('summary').click();
    await bundle.getByLabel('Temperature low alert', {exact: true}).check();
    await bundle.getByLabel('Temperature high alert', {exact: true}).check();
    await bundle.getByLabel('Favorable BD tending period', {exact: true}).check();
    await library.getByLabel('Stage', {exact: true}).selectOption('mature');
    await bundle.locator('summary').click();
    await bundle.getByLabel('Favorable BD tending period', {exact: true}).check();
    await library.getByRole('button', {name: 'Review Selected (4)', exact: true}).click();
    await expect(library.locator('.library-review-entry legend').filter({hasText: 'Lettuce /'})).toHaveCount(4);
    const destination = library.getByLabel('Destination group');
    await expect(destination.locator('option')).toHaveText(['Choose a saved group', 'Other', 'Nursery']);
    await destination.selectOption('Nursery');
    for (const card of await library.locator('.library-review-entry').filter({has: page.locator('legend').filter({hasText: 'Lettuce /'})}).all()) {
      await card.getByLabel('BD Calendar planting', {exact: true}).selectOption('lettuce-bed');
      await expect(card.getByLabel('Stage start', {exact: true})).toBeDisabled();
    }
    const temperature = library.getByRole('group', {name: 'Lettuce / Seedling / Temperature low alert', exact: true});
    await library.getByLabel('Shared temperature sensor / metric', {exact: true}).selectOption(JSON.stringify(['air', 'Temperature']));
    const high = library.getByRole('group', {name: 'Lettuce / Seedling / Temperature high alert', exact: true});
    await expect(high.getByLabel('Sensor / metric', {exact: true})).toHaveValue(JSON.stringify(['air', 'Temperature']));
    await high.getByLabel('Threshold (°F)', {exact: true}).fill('104');
    await temperature.getByLabel('Threshold (°F)', {exact: true}).fill('68');
    await temperature.getByLabel('Hysteresis (same units)', {exact: true}).fill('1.8');
    await page.screenshot({path: test.info().outputPath('library-review.png'), fullPage: true});
    const bounds = await temperature.boundingBox();
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    await library.getByRole('button', {name: 'Import Selected (4)', exact: true}).click();
    await expect(library.getByRole('status')).toHaveText('Temporary import failure');
    await expect(temperature.getByLabel('Stage start', {exact: true})).toBeDisabled();
    await library.getByRole('button', {name: 'Import Selected (4)', exact: true}).click();
    await expect(library).toHaveCount(0);
    expect(imports).toHaveLength(2);
    expect(imports[0].batch_id).toBe(imports[1].batch_id);
    expect(imports[1].entries[0]).toMatchObject({sensor: 'air', metric: 'Temperature', unit: '°F', value: '68', start: '2027-01-10'});
    expect(imports[1].entries[3]).toMatchObject({stage: 'mature', start: '2027-02-01'});
    await expect(modal.locator('#automationArrangeStatus')).toHaveText('Imported 4 disabled automations.');
    await expect(modal.locator('.automation-group[data-group="Nursery"] .automation-group-count')).toHaveText('4 Automations');
    await expect(modal.locator('.automation-group[data-group="Other"] .automation-group-count')).toHaveText('1 Automations');
    await modal.locator('.automation-group[data-group="Nursery"] > summary').click();
    await modal.getByText('Lettuce / Mature / Favorable BD tending period', {exact: true}).click();
    await expect(modal.getByLabel('Stage start', {exact: true})).toHaveValue('2027-02-01');
    await expect(modal.getByLabel('Stage end (exclusive)', {exact: true})).toHaveValue('2027-02-20');
    const stageBounds = await modal.locator('.calendar-range input').evaluateAll(inputs => inputs.map(input => {
      const bounds = input.getBoundingClientRect();
      return {y: bounds.y, bottom: bounds.bottom};
    }));
    expect(Math.abs(stageBounds[0].y - stageBounds[1].y)).toBeLessThan(1);
    expect(Math.abs(stageBounds[0].bottom - stageBounds[1].bottom)).toBeLessThan(1);
    await page.screenshot({path: test.info().outputPath('stage-date-alignment.png')});
    await expect(modal.locator('#autoEnabled')).toHaveValue('false');
  });
}
