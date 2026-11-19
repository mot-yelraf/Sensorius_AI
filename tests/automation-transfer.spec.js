import {test, expect} from '@playwright/test';

const filename = 'automations.26.323.16-15-42.json';
const document = {format: 'sensorius-automations', format_version: 1, application_version: 'v0.26.323.16', exported_at: '2026-11-19T15:42:00-07:00', value_units: 'native', groups: ['Other', 'Nursery'], automations: [
  {id: 'source', group: 'Nursery', position: 0, enabled: true, script: {name: 'Imported climate alert', enabled: true, conditions: [], actions: [{type: 'none', executor_switch_id: '__system__'}]}},
]};

async function setup(page) {
  const rules = [{rule_id: 'existing', enabled: true, group: 'Other', script_json: JSON.stringify({name: 'Existing rule', enabled: true, conditions: [], actions: []})}];
  const groups = ['Indoor', 'Outdoor', 'Greenhouse', 'Other'];
  const requests = [];
  await page.route('**/advanced/automations?*', route => route.fulfill({json: {items: rules, groups}}));
  await page.route('**/advanced/automations/export', route => route.fulfill({json: {filename, content: JSON.stringify(document, null, 2)}}));
  await page.route('**/advanced/automations/import/preview', route => {
    const doc = route.request().postDataJSON();
    return doc.format === document.format ? route.fulfill({json: {automation_count: 1, group_count: 2}}) : route.fulfill({status: 400, json: {error: 'This is not a Sensorius automation export.'}});
  });
  await page.route('**/advanced/automations/import', route => {
    requests.push({content: route.request().postData(), batch: route.request().headers()['x-automation-import-batch']});
    if (requests.length === 1) return route.fulfill({status: 503, json: {error: 'Temporary import failure.'}});
    groups.push('Nursery');
    rules.push({rule_id: 'new-copy', group: 'Nursery', enabled: false, script_json: JSON.stringify({...document.automations[0].script, enabled: false})});
    return route.fulfill({json: {ok: true, rule_ids: ['new-copy']}});
  });
  await page.goto('/');
  await page.getByRole('link', {name: 'Open General Settings'}).click();
  const modal = page.locator('#setupPiModal');
  await modal.getByRole('button', {name: 'Automations', exact: true}).click();
  await expect(modal.locator('.automation-group')).toHaveCount(4);
  return {modal, requests};
}

for (const width of [1280, 390]) {
  test(`saved automation footer and native file bridge support cancel, validate and retry at ${width}px`, async ({page}) => {
    await page.setViewportSize({width, height: 1000});
    await page.addInitScript(({filename, document}) => {
      window.fileTest = {opened: 0, saved: [], selection: null};
      window.pywebview = {api: {
        save_automation_export: async (content, name) => { window.fileTest.saved.push({content, name}); return {ok: true, name}; },
        open_automation_import: async () => { window.fileTest.opened++; return window.fileTest.selection || {cancelled: true}; },
      }};
      window.validFile = {ok: true, name: filename, content: JSON.stringify(document, null, 2)};
    }, {filename, document});
    const {modal, requests} = await setup(page);
    const footer = modal.locator('.automation-saved-footer');
    await expect(footer.getByRole('button')).toHaveText(['Remove Group', 'Add Group', 'Export', 'Import', 'Library', 'New']);
    const boxes = await footer.locator(':scope > div').evaluateAll(nodes => nodes.map(node => {
      const b = node.getBoundingClientRect(); return {x: b.x, y: b.y, right: b.right};
    }));
    if (width > 700) {
      expect(boxes[0].x).toBeLessThan(boxes[1].x);
      expect(boxes[1].x).toBeLessThan(boxes[2].x);
      expect(Math.abs(boxes[0].y - boxes[2].y)).toBeLessThan(1);
    } else {
      expect(boxes[0].y).toBeLessThan(boxes[1].y);
      expect(boxes[1].y).toBeLessThan(boxes[2].y);
    }
    const footerBox = await footer.boundingBox();
    for (const button of await footer.getByRole('button').all()) {
      const box = await button.boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(footerBox.x);
      expect(box.x + box.width).toBeLessThanOrEqual(footerBox.x + footerBox.width + 1);
    }
    await expect(footer.getByRole('button', {name: 'Export', exact: true})).toHaveCSS('background-color', 'rgb(168, 220, 169)');
    await footer.getByRole('button', {name: 'Export', exact: true}).click();
    await expect(modal.locator('#automationArrangeStatus')).toContainText(`Exported ${filename}`);
    const saved = await page.evaluate(() => window.fileTest.saved);
    expect(saved).toHaveLength(1);
    expect(saved[0].name).toBe(filename);
    expect(JSON.parse(saved[0].content)).toEqual(document);
    await footer.getByRole('button', {name: 'Import', exact: true}).click();
    await expect(modal.locator('#automationArrangeStatus')).toHaveText('Import cancelled.');
    await page.evaluate(() => { window.fileTest.selection = {ok: true, name: 'wrong.json', content: '{"format":"wrong"}'}; });
    await footer.getByRole('button', {name: 'Import', exact: true}).click();
    await expect(modal.locator('#automationArrangeStatus')).toHaveText('This is not a Sensorius automation export.');
    expect(requests).toHaveLength(0);
    await page.evaluate(() => { window.fileTest.selection = window.validFile; });
    page.once('dialog', dialog => dialog.dismiss());
    await footer.getByRole('button', {name: 'Import', exact: true}).click();
    await expect(modal.locator('#automationArrangeStatus')).toHaveText('Import cancelled.');
    expect(requests).toHaveLength(0);
    page.once('dialog', dialog => { expect(dialog.message()).toContain('Imported copies start disabled.'); return dialog.accept(); });
    await footer.getByRole('button', {name: 'Import', exact: true}).click();
    await expect(modal.locator('#automationArrangeStatus')).toContainText('Click Import to retry the same file.');
    const opened = await page.evaluate(() => window.fileTest.opened);
    page.once('dialog', dialog => dialog.accept());
    await footer.getByRole('button', {name: 'Import', exact: true}).click();
    await expect(modal.locator('#automationArrangeStatus')).toContainText('Imported 1 disabled automations.');
    expect(requests).toHaveLength(2);
    expect(requests[0]).toEqual(requests[1]);
    expect(await page.evaluate(() => window.fileTest.opened)).toBe(opened);
    const group = modal.locator('.automation-group[data-group="Nursery"]');
    await group.locator(':scope > summary').click();
    await expect(group.locator('.item-badge.disabled')).toHaveText('Disabled');
    await modal.locator('.automation-group[data-group="Other"] > summary').click();
    await expect(modal.locator('.automation-group[data-group="Other"] .item-badge.enabled')).toHaveText('Enabled');
    await expect(footer.getByRole('button', {name: 'Export', exact: true})).toBeEnabled();
    await page.screenshot({path: test.info().outputPath('automation-transfer-footer.png')});
  });
}

test('browser file open and download fallback use JSON filenames and readable contents', async ({page}) => {
  await page.addInitScript(() => { window.showOpenFilePicker = undefined; window.showSaveFilePicker = undefined; });
  const {modal, requests} = await setup(page);
  const downloadEvent = page.waitForEvent('download');
  await modal.getByRole('button', {name: 'Export', exact: true}).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toBe(filename);
  const chooserEvent = page.waitForEvent('filechooser');
  await modal.getByRole('button', {name: 'Import', exact: true}).click();
  const chooser = await chooserEvent;
  await chooser.setFiles({name: 'unrelated.json', mimeType: 'application/json', buffer: Buffer.from('{"format":"unrelated"}')});
  await expect(modal.locator('#automationArrangeStatus')).toHaveText('This is not a Sensorius automation export.');
  expect(requests).toHaveLength(0);
});
