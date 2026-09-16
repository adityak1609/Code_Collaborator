import { expect, test } from '@playwright/test';

test('registers, creates a collaborative session, connects, and saves', async ({
  page,
  request,
}) => {
  const unique = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
  const username = `e2e_${unique}`;
  const inviteeUsername = `invitee_${unique}`;
  const password = 'playwright-password';
  const apiUrl = process.env.E2E_API_URL ?? 'http://127.0.0.1:8000';
  let sessionId: string | undefined;

  try {
    const inviteeRegistration = await request.post(`${apiUrl}/auth/register`, {
      data: {
        username: inviteeUsername,
        email: `${inviteeUsername}@example.com`,
        password,
      },
    });
    expect(inviteeRegistration.ok()).toBeTruthy();

    await page.goto('/login');
    await page.getByRole('link', { name: 'Create one' }).click();
    await page.getByLabel('Username').fill(username);
    await page.getByLabel('Email').fill(`${username}@example.com`);
    await page.getByLabel('Password').fill(password);
    await page.getByRole('button', { name: 'Create account' }).click();

    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole('heading', { name: 'Concord' })).toBeVisible();
    await page.getByRole('button', { name: /New workspace/ }).click();
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

    await page.waitForFunction(() => {
      const browserWindow = window as typeof window & {
        monaco?: { editor?: { getModels: () => Array<{ setValue: (value: string) => void }> } };
      };
      return Boolean(browserWindow.monaco?.editor?.getModels().length);
    });
    await page.evaluate(() => {
      const browserWindow = window as typeof window & {
        monaco: { editor: { getModels: () => Array<{ setValue: (value: string) => void }> } };
      };
      browserWindow.monaco.editor.getModels()[0].setValue(
        'console.log("browser-execution-ok");',
      );
    });
    await expect(page.getByText('Unsaved changes')).toBeVisible();

    await page.getByRole('button', { name: /Run/ }).click();
    await expect(page.locator('.terminal-output')).toContainText(
      'browser-execution-ok',
      { timeout: 20_000 },
    );
    await expect(page.locator('.execution-status')).toHaveText('COMPLETED');
    if (process.env.E2E_SCREENSHOT) {
      await page.screenshot({ path: 'test-results/workspace-desktop.png' });
      await page.setViewportSize({ width: 640, height: 900 });
      await page.screenshot({ path: 'test-results/workspace-mobile.png' });
      await page.setViewportSize({ width: 1280, height: 720 });
    }

    const saveButton = page.getByRole('button', { name: 'Save', exact: true });
    await expect(saveButton).toBeEnabled({ timeout: 15_000 });
    await saveButton.click();
    await expect(page.locator('.save-status')).toContainText('Saved');

    await page.getByLabel('Snapshot label').fill('Before browser refactor');
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    await expect(page.getByText('Before browser refactor', { exact: true })).toBeVisible();
    await page.evaluate(() => {
      const browserWindow = window as typeof window & {
        monaco: { editor: { getModels: () => Array<{ setValue: (value: string) => void }> } };
      };
      browserWindow.monaco.editor.getModels()[0].setValue(
        'console.log("temporary browser edit");',
      );
    });
    await expect(page.getByText('Unsaved changes')).toBeVisible();
    page.once('dialog', (dialog) => void dialog.accept());
    await page.getByRole('button', { name: 'Restore', exact: true }).click();
    await expect(page.getByText(/Restored Before browser refactor/)).toBeVisible();
    await page.waitForFunction(() => {
      const browserWindow = window as typeof window & {
        monaco: { editor: { getModels: () => Array<{ getValue: () => string }> } };
      };
      return browserWindow.monaco.editor.getModels()[0].getValue()
        === 'console.log("browser-execution-ok");';
    });
    await expect(page.getByText('Unsaved changes')).toBeVisible();

    await page.getByLabel('Invite by username').fill(inviteeUsername);
    await page.getByLabel('Invitation role').selectOption('viewer');
    await page.getByRole('button', { name: 'Add member' }).click();
    await expect(page.getByText(`${inviteeUsername} added as viewer.`)).toBeVisible();
    await expect(page.getByText(inviteeUsername, { exact: true })).toBeVisible();

    const inviteeRole = page.getByLabel(`Role for ${inviteeUsername}`);
    await expect(inviteeRole).toHaveValue('viewer');
    await inviteeRole.selectOption('editor');
    await expect(page.getByText('Member role changed to editor.')).toBeVisible();
    await expect(inviteeRole).toHaveValue('editor');

    await page.getByRole('button', { name: `Remove ${inviteeUsername}` }).click();
    await expect(page.getByText('Member removed.')).toBeVisible();
    await expect(page.getByText(inviteeUsername, { exact: true })).toHaveCount(0);
  } finally {
    if (sessionId) {
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
