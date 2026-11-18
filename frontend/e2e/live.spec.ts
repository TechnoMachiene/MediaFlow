import { expect, test } from '@playwright/test';
import { closeSheet, go, signInAs, unique, upload } from './helpers';

/* Live updates: a Manager watching Review sees a draft appear the moment a Member sends it,
 * in another browser, without reloading or changing screens, and the bell lights up. */
test('changes by someone else show up without a reload', async ({ browser }, info) => {
  const viewport = info.project.use.viewport ?? undefined;
  const managerCtx = await browser.newContext({ viewport });
  const memberCtx = await browser.newContext({ viewport });
  const manager = await managerCtx.newPage();
  const member = await memberCtx.newPage();
  const name = unique('live');

  await signInAs(manager, 'Manager');
  await go(manager, 'Review');
  await expect(manager.getByTitle(name)).toHaveCount(0);

  await signInAs(member, 'Member');
  await upload(member, name);
  await expect(member.getByTitle(name)).toContainText('Draft ready', { timeout: 45_000 });
  await member.getByTitle(name).click();
  await member.getByRole('dialog', { name }).getByRole('button', { name: 'Send for review' }).click();
  await closeSheet(member);

  // No reload, no navigation on the manager's side.
  await expect(manager.getByTitle(name)).toBeVisible({ timeout: 10_000 });
  await expect(manager.getByRole('button', { name: /^Notifications, \d+ unread/ }).first())
    .toBeVisible({ timeout: 10_000 });

  // Clicking the notification opens that very draft.
  await manager.getByRole('button', { name: /^Notifications, \d+ unread/ }).first().click();
  await manager.getByRole('button', { name: new RegExp(`New draft to review: ${name}`) }).click();
  const review = manager.getByRole('dialog', { name });
  await expect(review.getByRole('button', { name: 'Reject…' })).toBeVisible();

  // And back: the manager rejects it; the member's Library updates by itself.
  await review.getByRole('button', { name: 'Reject…' }).click();
  await review.getByLabel('What needs to change?').fill('Live test');
  await review.getByRole('button', { name: 'Reject', exact: true }).click();
  await expect(member.getByTitle(name)).toContainText('Rejected', { timeout: 10_000 });

  await managerCtx.close();
  await memberCtx.close();
});
