import { expect, test } from '@playwright/test';
import { closeSheet, go, signInAs, signOut, unique, upload } from './helpers';

/* The review loop: a rejection with a reason, the uploader reads it, edits the draft, and it
 * comes back to the reviewer as a new version. */
test('a rejected draft is revised and returns to review', async ({ page }) => {
  const name = unique('revision');
  const reason = 'Mention the pier in the caption';

  await signInAs(page, 'Member');
  await upload(page, name);
  await expect(page.getByTitle(name)).toContainText('In review', { timeout: 45_000 });
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
  await expect(sheet.getByText(/Saving a change sends it back for review/)).toBeVisible();
  const caption = sheet.getByLabel('Caption').first();
  await caption.fill('Golden hour on the pier, by the water.');
  await sheet.getByLabel('What changed (optional)').fill('Mentioned the pier');
  await sheet.getByRole('button', { name: 'Save version' }).click();
  await expect(page.getByText(/back in review/)).toBeVisible();

  // History shows both versions with the change.
  await sheet.getByRole('radio', { name: 'History' }).click();
  await expect(sheet.getByText('Version 2 · Mentioned the pier')).toBeVisible();
  await expect(sheet.locator('ins', { hasText: 'Golden hour on the pier, by the water.' })).toBeVisible();
  await closeSheet(page);
  await signOut(page);

  // Back in the manager's queue.
  await signInAs(page, 'Manager');
  await go(page, 'Review');
  await expect(page.getByTitle(name)).toBeVisible();
});
