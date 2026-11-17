import { deflateSync } from 'node:zlib';
import { expect, type Page } from '@playwright/test';

type Role = 'Member' | 'Manager' | 'Admin';

/** A real PNG, unique per call, so uploads never hit the dedup cache. */
export function png(): Buffer {
  const crcTable = Array.from({ length: 256 }, (_, n) => {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    return c >>> 0;
  });
  const crc = (buf: Buffer) => {
    let c = 0xffffffff;
    for (const b of buf) c = crcTable[(c ^ b) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
  };
  const chunk = (kind: string, data: Buffer) => {
    const body = Buffer.concat([Buffer.from(kind), data]);
    const len = Buffer.alloc(4); len.writeUInt32BE(data.length);
    const sum = Buffer.alloc(4); sum.writeUInt32BE(crc(body));
    return Buffer.concat([len, body, sum]);
  };
  const [w, h] = [120, 80];
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(w, 0); ihdr.writeUInt32BE(h, 4); ihdr.set([8, 2, 0, 0, 0], 8);
  const rgb = [0, 1, 2].map(() => Math.floor(Math.random() * 256));
  const row = Buffer.concat([Buffer.from([0]), Buffer.from(Array.from({ length: w }, () => rgb).flat())]);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', ihdr),
    chunk('IDAT', deflateSync(Buffer.concat(Array.from({ length: h }, () => row)))),
    chunk('IEND', Buffer.alloc(0)),
  ]);
}

export async function signInAs(page: Page, role: Role) {
  await page.goto('/');
  await page.getByRole('button', { name: `Continue as ${role}` }).click();
  await expect(page.getByRole('heading', { name: 'Library', level: 1 })).toBeVisible();
}

/** Desktop has Sign out in the sidebar; phones keep it in the account sheet. */
export async function signOut(page: Page) {
  const direct = page.getByRole('button', { name: 'Sign out' });
  if (!(await direct.first().isVisible())) {
    await page.getByRole('button', { name: /^Account:/ }).click();
  }
  await direct.first().click();
  await expect(page.getByRole('heading', { name: 'Sign in to MediaFlow' })).toBeVisible();
}

/** The sidebar on desktop, the tab bar on phones: whichever is showing. */
export async function go(page: Page, section: string) {
  await page.getByRole('link', { name: section, exact: true }).first().click();
  await expect(page.getByRole('heading', { name: section, level: 1 })).toBeVisible();
}

export async function upload(page: Page, name: string) {
  await page.locator('input[type="file"]').setInputFiles({ name, mimeType: 'image/png', buffer: png() });
}

export async function closeSheet(page: Page) {
  await page.getByRole('dialog').getByRole('button', { name: 'Close' }).click();
  await expect(page.getByRole('dialog')).toBeHidden();
}

export const unique = (label: string) => `${label}-${Date.now()}-${Math.floor(Math.random() * 1e4)}.png`;
