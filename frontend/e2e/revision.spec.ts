import { expect, test } from '@playwright/test';
import { closeSheet, go, signInAs, signOut, unique, upload } from './helpers';

/* The review loop: the uploader edits Gemini's draft before sending it, a rejection with a
 * reason, the uploader reads it, edits again, and sends it back to the reviewer. */
test('a rejected draft is revised and returns to review', async ({ page }) => {
  const name = unique('revision');
  const reason = 'Mention the pier in the caption';

  await signInAs(page, 'Member');
  await upload(page, name);
  await expect(page.getByTitle(name)).toContainText('Draft ready', { timeout: 45_000 });
  await page.getByTitle(name).click();
  const first = page.getByRole('dialog', { name });
  await first.getByRole('button', { name: 'Edit' }).click();
  await expect(first.getByText(/Your changes stay with you/)).toBeVisible();
  await first.getByLabel('Caption').first().fill('Golden hour, hand-written.');
  await first.getByRole('button', { name: 'Save version' }).click();
  await expect(page.getByText(/Send it for review when it's ready/)).toBeVisible();
  await first.getByRole('button', { name: 'Send for review' }).click();
  await expect(page.getByText('Sent for review')).toBeVisible();
  await closeSheet(page);
  await signOut(page);

  await signInAs(page, 'Manager');
  await go(page, 'Review');
  await page.getByTitle(name).click();
  const review = page.getByRole('dialog', { name });
  await review.getByRole('button', { name: 'Reject…' }).click();
  await review.getByLabel('What needs to change?').fill(reason);
  await review.getByRole('button', { name: 'Reject', exact: true }).click();
  await expect(page.getByText('Rejected', { exact: true }).first()).toBeVisible();
  await signOut(page);

  // The uploader sees why, in the draft's comments, and edits the caption.
  await signInAs(page, 'Member');
  await page.getByTitle(name).click();
  const sheet = page.getByRole('dialog', { name });
  await sheet.getByRole('radio', { name: 'Comments (1)' }).click();
  await expect(sheet.getByText(reason)).toBeVisible();
  await sheet.getByRole('radio', { name: 'Draft' }).click();
  await sheet.getByRole('button', { name: 'Edit' }).click();
  await expect(sheet.getByText(/brings it back to you to send for review again/)).toBeVisible();
  const caption = sheet.getByLabel('Caption').first();
  await caption.fill('Golden hour on the pier, by the water.');
  await sheet.getByLabel('What changed (optional)').fill('Mentioned the pier');
  await sheet.getByRole('button', { name: 'Save version' }).click();
  await expect(page.getByText(/Send it for review when it's ready/)).toBeVisible();

  // History shows every version with the change.
  await sheet.getByRole('radio', { name: 'History' }).click();
  await expect(sheet.getByText('Version 3 · Mentioned the pier')).toBeVisible();
  await expect(sheet.locator('ins', { hasText: 'Golden hour on the pier, by the water.' })).toBeVisible();
  await sheet.getByRole('button', { name: 'Send for review' }).click();
  await expect(page.getByText('Sent for review')).toBeVisible();
  await closeSheet(page);
  await signOut(page);

  // Back in the manager's queue.
  await signInAs(page, 'Manager');
  await go(page, 'Review');
  await expect(page.getByTitle(name)).toBeVisible();
});
