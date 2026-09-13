import { expect, test } from '@playwright/test';

test('registers, creates a collaborative session, connects, and saves', async ({
  page,
  request,
}) => {
  const unique = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const username = `e2e_${unique}`;
  const password = 'playwright-password';
  let sessionId: string | undefined;

  try {
    await page.goto('/login');
    await page.getByRole('link', { name: 'Create one' }).click();
    await page.getByLabel('Username').fill(username);
    await page.getByLabel('Email').fill(`${username}@example.com`);
    await page.getByLabel('Password').fill(password);
    await page.getByRole('button', { name: 'Create account' }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole('heading', { name: 'Concord' })).toBeVisible();
    await page.getByRole('button', { name: '+ New Session' }).click();
    await page.getByLabel('Session Name').fill(`Browser E2E ${unique}`);
    await page.getByLabel('Language').selectOption('javascript');
    await page.getByRole('button', { name: 'Create', exact: true }).click();

    await expect(page).toHaveURL(/\/session\/[0-9a-f-]+$/);
    sessionId = page.url().split('/').at(-1);
    await expect(page.getByText(`Browser E2E ${unique}`)).toBeVisible();
    await expect(page.locator('.workspace-header .badge-owner')).toHaveText('owner');
    await expect(page.getByText('Connected', { exact: true })).toBeVisible({
      timeout: 15_000,
    });

    const saveButton = page.getByRole('button', { name: 'Save', exact: true });
    await expect(saveButton).toBeEnabled({ timeout: 15_000 });
    await saveButton.click();
    await expect(page.locator('.save-status')).toContainText('Saved');
  } finally {
    if (sessionId) {
      const apiUrl = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
      const loginResponse = await request.post(`${apiUrl}/auth/login`, {
        data: { username, password },
      });
      if (loginResponse.ok()) {
        const { access_token: token } = await loginResponse.json();
        await request.delete(`${apiUrl}/sessions/${sessionId}`, {
          headers: { Authorization: `Bearer ${token}` },
        });
      }
    }
  }
});
