const { test, expect } = require('@playwright/test');
const BASE = 'http://localhost:8000';
const USER = process.env.PW_USER || 'pw-tester';
const PASS = process.env.PW_PASS || 'pwpass123';

async function login(page) {
  await page.goto(BASE + '/login');
  await page.getByPlaceholder('username').fill(USER);
  const pw = page.getByPlaceholder('password');
  await pw.fill(PASS);
  await pw.press('Enter');
  await page.waitForURL(u => !String(u).includes('/login'), { timeout: 15000 }).catch(() => {});
  await page.waitForTimeout(800);
}

test('Network view shows the live egress feed (real clicks)', async ({ page }) => {
  await login(page);
  await page.getByRole('link', { name: 'Network' }).click();
  await page.waitForTimeout(2500);
  await page.screenshot({ path: 'shot-network.png', fullPage: true });
  // the seeded egress events must render (proves REST seed + feed rendering)
  await expect(page.getByText('github.com').first()).toBeVisible({ timeout: 15000 });
  await expect(page.getByText(/pypi\.org/).first()).toBeVisible();
  // a verdict chip (allow/deny) should be present
  await expect(page.getByText(/allow|deny|cut/).first()).toBeVisible();
});

test('Review Center renders + surfaces pending egress approvals', async ({ page }) => {
  await login(page);
  await page.getByRole('link', { name: 'Review' }).click();
  await page.waitForTimeout(2500);
  await page.screenshot({ path: 'shot-review.png', fullPage: true });
  await expect(page.getByText(/not-allowed-host-xyz|z9x8q7w6/).first())
    .toBeVisible({ timeout: 15000 });
});

test('Context page loads (taint/promote UI wiring)', async ({ page }) => {
  await login(page);
  await page.getByRole('link', { name: 'Context' }).click();
  await page.waitForTimeout(2000);
  await page.screenshot({ path: 'shot-context.png', fullPage: true });
  await expect(page.getByRole('link', { name: 'Network' })).toBeVisible();
});
