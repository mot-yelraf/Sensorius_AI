import { expect, test } from '@playwright/test';

for (const width of [1280, 390]) {
  test(`Saved Automations groups and arrangement menu work at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    let groups = ['Indoor', 'Outdoor', 'Greenhouse', 'Other'];
    const rules = ['First', 'Second', 'Third'].map((name, index) => ({
      rule_id: `rule-${index}`, enabled: index === 0,
      script_json: JSON.stringify({name, conditions: [], actions: []}),
    }));
    await page.route('**/sensor-directory', route => route.fulfill({json: []}));
    await page.route('**/automation-context', route => route.fulfill({json: {
      actors: [], email_enabled: false, executor_switch_id: '__system__',
    }}));
    await page.route('**/advanced/automations?*', route => route.fulfill({json: {items: rules, groups}}));
    await page.route('**/advanced/automations/groups', async route => {
      const body = route.request().postData();
      const name = body.match(/name="group"\r\n\r\n([^\r]+)/)[1];
      const choice = body.match(/name="choice"\r\n\r\n([^\r]+)/)[1];
      if (choice === 'Add') groups.push(name);
      else if (choice === 'Remove') {
        groups = groups.filter(group => group !== name);
        rules.filter(rule => (rule.group || 'Other') === name).forEach(rule => {
          rule.group = groups.includes('Other') ? 'Other' : groups[0];
        });
      } else {
        const index = groups.indexOf(name);
        const neighbor = index + (choice === 'Up' ? -1 : 1);
        [groups[index], groups[neighbor]] = [groups[neighbor], groups[index]];
      }
      await route.fulfill({json: {ok: true}});
    });
    const requests = [];
    await page.route('**/advanced/automations/arrange', async route => {
      const body = route.request().postData();
      const id = body.match(/name="rule_id"\r\n\r\n([^\r]+)/)[1];
      const choice = body.match(/name="choice"\r\n\r\n([^\r]+)/)[1];
      requests.push({id, choice});
      const rule = rules.find(item => item.rule_id === id);
      const group = rule.group || 'Other';
      const peers = rules.filter(item => (item.group || 'Other') === group)
        .sort((a, b) => (a.position ?? 99) - (b.position ?? 99) || a.rule_id.localeCompare(b.rule_id));
      if (choice === 'Up' || choice === 'Down') {
        const index = peers.indexOf(rule);
        const neighbor = index + (choice === 'Up' ? -1 : 1);
        [peers[index], peers[neighbor]] = [peers[neighbor], peers[index]];
        peers.forEach((item, position) => { item.position = position; });
      } else {
        const target = rules.filter(item => item !== rule && (item.group || 'Other') === choice)
          .sort((a, b) => (a.position ?? 99) - (b.position ?? 99) || a.rule_id.localeCompare(b.rule_id));
        target.push(rule);
        target.forEach((item, position) => { item.group = choice; item.position = position; });
      }
      await route.fulfill({json: {ok: true}});
    });
    await page.goto('/');
    await page.getByRole('link', {name: 'Open General Settings'}).click();
    const modal = page.locator('#setupPiModal');
    await modal.getByRole('button', {name: 'Automations', exact: true}).click();
    const list = modal.locator('#automationList');
    const group = name => list.locator(`.automation-group[data-group="${name}"]`);
    const expectMenusToMatchSections = async () => {
      const displayedGroups = await list.locator('.automation-group').evaluateAll(
        sections => sections.map(section => section.dataset.group)
      );
      for (const choices of await list.locator('.automation-menu .automation-menu-choices').all()) {
        await expect(choices.locator('button')).toHaveText(['Up', 'Down', ...displayedGroups]);
      }
    };
    await expect(list.locator('.automation-group-label')).toHaveText(['Indoor - 0 Automations', 'Outdoor - 0 Automations', 'Greenhouse - 0 Automations', 'Other - 3 Automations']);
    await expectMenusToMatchSections();
    await expect(list.getByText('No saved automations.', {exact: true})).toHaveCount(0);
    await expect(modal.locator('#automationChooser .list-header button')).toHaveCount(0);
    await expect(modal.locator('#automationChooser .pane-footer').getByRole('button', {name: 'New', exact: true})).toBeVisible();
    await expect(group('Other').locator('.item-name')).toHaveText(['First', 'Second', 'Third']);
    await expect(modal.getByRole('button', {name: 'Remove Group', exact: true})).toBeDisabled();
    await expect(list.locator('.automation-group[open]')).toHaveCount(0);
    await expect(group('Other').getByText('First', {exact: true})).toBeHidden();
    await page.screenshot({path: test.info().outputPath('collapsed-group-columns.png')});
    const geometry = await list.locator('.automation-group > summary').evaluateAll(headers => headers.map(header => {
      const name = header.querySelector('.automation-group-name');
      const count = header.querySelector('.automation-group-count');
      const left = name.getBoundingClientRect();
      const right = count.getBoundingClientRect();
      const bounds = header.getBoundingClientRect();
      return {nameAlign: getComputedStyle(name).textAlign, countAlign: getComputedStyle(count).textAlign,
        divider: (left.right + right.left) / 2, center: (bounds.left + bounds.right) / 2};
    }));
    for (const header of geometry) {
      expect(header.nameAlign).toBe('right');
      expect(header.countAlign).toBe('left');
      expect(Math.abs(header.divider - header.center)).toBeLessThan(1);
    }
    await group('Other').locator(':scope > summary').click();
    await expect(group('Indoor')).not.toHaveAttribute('open', '');
    await list.getByLabel('Organize Second', {exact: true}).click();
    const menu = list.locator('.automation-menu[open]');
    await expect(menu.locator('button')).toHaveText(['Up', 'Down', 'Indoor', 'Outdoor', 'Greenhouse', 'Other']);
    await menu.getByRole('button', {name: 'Up', exact: true}).click();
    await expect(group('Other').locator('.item-name')).toHaveText(['Second', 'First', 'Third']);
    await expect(group('Indoor')).not.toHaveAttribute('open', '');
    await list.getByLabel('Organize Second', {exact: true}).click();
    await expect(menu.getByRole('button', {name: 'Up', exact: true})).toBeDisabled();
    await menu.getByRole('button', {name: 'Down', exact: true}).click();
    await expect(group('Other').locator('.item-name')).toHaveText(['First', 'Second', 'Third']);
    for (const target of ['Indoor', 'Outdoor', 'Greenhouse', 'Other']) {
      await list.getByLabel('Organize Second', {exact: true}).click();
      await menu.getByRole('button', {name: target, exact: true}).click();
      await expect(group(target).locator('.item-name')).toContainText(['Second']);
      await expect(group(target).locator('.automation-group-label')).toHaveText(`${target} - ${target === 'Other' ? 3 : 1} Automations`);
    }
    expect(requests.map(item => item.choice)).toEqual(['Up', 'Down', 'Indoor', 'Outdoor', 'Greenhouse', 'Other']);
    await group('Other').locator(':scope > summary').click();
    await expect(list.getByText('Second', {exact: true})).toBeHidden();
    await group('Other').locator(':scope > summary').click();
    await list.getByLabel('Organize Second', {exact: true}).click();
    await expect(menu.getByRole('button', {name: 'Down', exact: true})).toBeDisabled();
    await page.screenshot({path: test.info().outputPath('automation-groups.png')});
    const bounds = await menu.locator('.automation-menu-choices').boundingBox();
    expect(bounds.y).toBeGreaterThanOrEqual(0);
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(900);
    expect(bounds.x).toBeGreaterThanOrEqual(0);
    expect(bounds.x + bounds.width).toBeLessThanOrEqual(width);
    await list.getByLabel('Organize Second', {exact: true}).press('Escape');
    await expect(menu).toHaveCount(0);
    await list.getByLabel('Organize Second', {exact: true}).click();
    await modal.locator('#automationChooser .list-header').click();
    await expect(menu).toHaveCount(0);
    await page.route('**/advanced/automations/arrange', route => route.fulfill({
      status: 500, json: {error: 'Unable to organize automation.'},
    }));
    await list.getByLabel('Organize Second', {exact: true}).click();
    await menu.getByRole('button', {name: 'Indoor', exact: true}).click();
    await expect(modal.locator('#automationArrangeStatus')).toHaveText('Unable to organize automation.');
    await expect(group('Other').locator('.item-name')).toHaveText(['First', 'Third', 'Second']);
    await list.getByText('Second', {exact: true}).click();
    await expect(modal.locator('#autoName')).toHaveValue('Second');
    await modal.getByRole('button', {name: 'Saved Automations', exact: true}).click();
    await expect(group('Other').locator('.item-name')).toHaveText(['First', 'Third', 'Second']);
    await expect(group('Indoor')).toHaveClass(/integration-block/);
    await expect(modal.getByRole('button', {name: 'Remove Group', exact: true})).toBeDisabled();
    await expect(modal.getByRole('button', {name: 'Remove Group', exact: true})).toHaveAttribute('title', /Move all automations/);
    await group('Indoor').getByRole('button', {name: 'Organize group Indoor', exact: true}).click();
    const headerMenu = list.locator('.automation-group > .automation-menu-choices:popover-open');
    await expect(headerMenu.getByRole('button', {name: 'Up', exact: true})).toBeDisabled();
    await headerMenu.getByRole('button', {name: 'Down', exact: true}).click();
    await expect(list.locator('.automation-group-label')).toHaveText(['Outdoor - 0 Automations', 'Indoor - 0 Automations', 'Greenhouse - 0 Automations', 'Other - 3 Automations']);
    await expectMenusToMatchSections();
    await group('Indoor').getByRole('button', {name: 'Organize group Indoor', exact: true}).click();
    await headerMenu.getByRole('button', {name: 'Up', exact: true}).click();
    await expect(list.locator('.automation-group-label')).toHaveText(['Indoor - 0 Automations', 'Outdoor - 0 Automations', 'Greenhouse - 0 Automations', 'Other - 3 Automations']);
    page.once('dialog', dialog => dialog.accept('Nursery'));
    await modal.getByRole('button', {name: 'Add Group', exact: true}).click();
    await expect(group('Nursery')).toBeVisible();
    await expect(group('Nursery')).not.toHaveAttribute('open', '');
    await expect(group('Nursery').locator('.automation-group-label')).toHaveText('Nursery - 0 Automations');
    await expectMenusToMatchSections();
    await expect(modal.getByRole('button', {name: 'Remove Group', exact: true})).toBeEnabled();
    page.once('dialog', dialog => dialog.dismiss());
    await modal.getByRole('button', {name: 'Remove Group', exact: true}).click();
    await expect(group('Nursery')).toBeVisible();
    await expect(group('Nursery')).not.toHaveAttribute('open', '');
    await expect(group('Nursery').locator('.automation-group-label')).toHaveText('Nursery - 0 Automations');
    page.once('dialog', dialog => dialog.accept());
    await modal.getByRole('button', {name: 'Remove Group', exact: true}).click();
    await expect(group('Nursery')).toHaveCount(0);
    await expectMenusToMatchSections();
    await expect(modal.getByRole('button', {name: 'Library', exact: true})).toBeEnabled();
    await group('Other').locator(':scope > summary').click();
    await expect(modal.getByRole('button', {name: 'Remove Group', exact: true})).toBeDisabled();
    await expect(group('Other').locator('.item-name')).toHaveCount(3);
    if ((await group('Other').getAttribute('open')) === null) {
      await group('Other').locator(':scope > summary').click();
    }
    await expect(group('Other').getByText('Second', {exact: true})).toBeVisible();
    await page.screenshot({path: test.info().outputPath('custom-automation-groups.png')});
  });
}
