import { expect, test } from '@playwright/test';
import { closeSheet, go, signInAs, signOut, unique, upload } from './helpers';

/* The main journey, in a real browser against the real app (demo mode: offline drafts, the rest
 * is the real API, queue, worker and database): a member's photo becomes a planned post. */
test('a photo goes from upload to draft, approval, schedule and calendar', async ({ page }) => {
  const name = unique('journey');

  // Member uploads; the worker drafts it.
  await signInAs(page, 'Member');
  await upload(page, name);
  const tile = page.getByTitle(name);
  await expect(tile).toContainText('In review', { timeout: 45_000 });
  await tile.click();
  const sheet = page.getByRole('dialog', { name });
  await expect(sheet.getByRole('radio', { name: 'Instagram' })).toBeVisible();
  await expect(sheet.getByText('Caption')).toBeVisible();
  await closeSheet(page);
  await signOut(page);

  // Manager approves it from the review queue.
  await signInAs(page, 'Manager');
  await go(page, 'Review');
  await page.getByTitle(name).click();
  await page.getByRole('dialog', { name }).getByRole('button', { name: 'Approve' }).click();
  await expect(page.getByText('Approved', { exact: true }).first()).toBeVisible();
  await expect(page.getByTitle(name)).toBeHidden(); // left the waiting queue
  await signOut(page);

  // Member schedules the approved draft.
  await signInAs(page, 'Member');
  await expect(page.getByTitle(name)).toContainText('Approved');
  await page.getByTitle(name).click();
  await page.getByRole('dialog', { name }).getByRole('button', { name: /Schedule/ }).click();
  await page.getByRole('dialog', { name }).getByRole('button', { name: 'Schedule', exact: true }).click();
  await expect(page.getByText(/^Scheduled for /)).toBeVisible();

  // ...and finds it on tomorrow's date in the calendar (which may be next month's grid). Other
  // runs may have planned posts that day too, so match the date, not a count.
  await go(page, 'Calendar');
  const tomorrow = await page.evaluate(() => {
    const d = new Date();
    d.setDate(d.getDate() + 1);
    return new Intl.DateTimeFormat(undefined, { weekday: 'long', month: 'long', day: 'numeric' }).format(d);
  });
  const day = page.getByRole('button', { name: new RegExp(`^${tomorrow}, [1-9]\\d* planned`) });
  if (!(await day.isVisible())) {
    await page.getByRole('button', { name: 'Next month' }).click();
  }
  await day.click();
  await expect(page.getByRole('button', { name: new RegExp(name) })).toBeVisible();
});
