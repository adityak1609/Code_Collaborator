import { chromium, request } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import gifenc from 'gifenc';
import { PNG } from 'pngjs';

const { GIFEncoder, applyPalette, quantize } = gifenc;

const frontendUrl = process.env.DEMO_FRONTEND_URL ?? 'http://127.0.0.1:5173';
const apiUrl = process.env.DEMO_API_URL ?? 'http://127.0.0.1:8000';
const ffmpeg = process.env.FFMPEG_PATH ?? 'ffmpeg';
const scriptDir = dirname(fileURLToPath(import.meta.url));
const frontendDir = resolve(scriptDir, '..');
const outputPath = resolve(frontendDir, '..', 'docs', 'assets', 'concord-live-collaboration.gif');
const videoDir = resolve(frontendDir, 'test-results', 'demo-video');
const framesDir = resolve(videoDir, 'frames');
const password = 'concord-demo-password';
const unique = Date.now().toString(36);
const owner = `alex_${unique}`;
const collaborator = `sam_${unique}`;

const wait = (milliseconds) =>
  new Promise((resolveWait) => setTimeout(resolveWait, milliseconds));

async function register(api, username) {
  const response = await api.post(`${apiUrl}/auth/register`, {
    data: { username, email: `${username}@example.com`, password },
  });
  if (!response.ok()) {
    throw new Error(`Registration failed for ${username}: ${response.status()}`);
  }
}

async function loginApi(api, username) {
  const response = await api.post(`${apiUrl}/auth/login`, {
    data: { username, password },
  });
  if (!response.ok()) {
    throw new Error(`Login failed for ${username}: ${response.status()}`);
  }
  return (await response.json()).access_token;
}

async function loginPage(page, username) {
  await page.goto(`${frontendUrl}/login`);
  await page.getByLabel('Username').fill(username);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.waitForURL(`${frontendUrl}/`);
}

async function waitForEditor(page) {
  await page.getByText('Connected', { exact: true }).waitFor({ timeout: 20_000 });
  await page.waitForFunction(() => Boolean(window.monaco?.editor?.getModels().length));
}

async function openSession(page, sessionId) {
  await page.getByText('Realtime Pairing Session', { exact: true }).click();
  await page.waitForURL(`${frontendUrl}/session/${sessionId}`);
  await waitForEditor(page);
}

rmSync(videoDir, { recursive: true, force: true });
mkdirSync(videoDir, { recursive: true });
mkdirSync(dirname(outputPath), { recursive: true });

const api = await request.newContext();
let sessionId;
let ownerToken;
let browser;
let ownerContext;
let collaboratorContext;

try {
  await register(api, owner);
  await register(api, collaborator);
  ownerToken = await loginApi(api, owner);

  const sessionResponse = await api.post(`${apiUrl}/sessions`, {
    headers: { Authorization: `Bearer ${ownerToken}` },
    data: { name: 'Realtime Pairing Session', language: 'javascript' },
  });
  if (!sessionResponse.ok()) {
    throw new Error(`Session creation failed: ${sessionResponse.status()}`);
  }
  sessionId = (await sessionResponse.json()).id;

  const memberResponse = await api.post(
    `${apiUrl}/sessions/${sessionId}/members`,
    {
      headers: { Authorization: `Bearer ${ownerToken}` },
      data: { username: collaborator, role: 'editor' },
    },
  );
  if (!memberResponse.ok()) {
    throw new Error(`Member invitation failed: ${memberResponse.status()}`);
  }

  browser = await chromium.launch({ headless: true });
  ownerContext = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: videoDir, size: { width: 1280, height: 720 } },
  });
  collaboratorContext = await browser.newContext({
    viewport: { width: 1280, height: 720 },
  });
  const ownerPage = await ownerContext.newPage();
  const collaboratorPage = await collaboratorContext.newPage();

  await loginPage(ownerPage, owner);
  await openSession(ownerPage, sessionId);

  await loginPage(collaboratorPage, collaborator);
  await openSession(collaboratorPage, sessionId);
  await ownerPage.getByText('Members (2)', { exact: true }).waitFor();

  const editor = collaboratorPage.locator('.monaco-editor');
  await editor.click({ position: { x: 230, y: 42 } });

  const lines = [
    'const teammates = ["Alex", "Sam"];',
    'const message = teammates.map(name => `Hello, ${name}!`);',
    'console.log(message.join("  "));',
  ];
  for (const [index, line] of lines.entries()) {
    if (index === 0) {
      await collaboratorPage.keyboard.type(line[0], { delay: 38 });
      await ownerPage.waitForFunction((name) => {
        const cursorStyles = document.querySelector('#concord-remote-cursors');
        return cursorStyles?.textContent?.includes(name);
      }, collaborator);
      await wait(1_500);
      await collaboratorPage.keyboard.type(line.slice(1), { delay: 38 });
    } else {
      await collaboratorPage.keyboard.type(line, { delay: 38 });
    }
    if (index < lines.length - 1) {
      await collaboratorPage.keyboard.press('Enter');
    }
    await wait(450);
  }
  await wait(1_200);

  await ownerPage.getByRole('button', { name: /Run/ }).click();
  await ownerPage
    .locator('.terminal-output')
    .getByText(/Hello, Alex!/)
    .waitFor({ timeout: 20_000 });
  await wait(2_500);
} finally {
  if (collaboratorContext) {
    await collaboratorContext.close().catch(() => undefined);
  }
  if (ownerContext) {
    await ownerContext.close().catch(() => undefined);
  }
  if (browser) {
    await browser.close().catch(() => undefined);
  }
  if (sessionId && ownerToken) {
    await api
      .delete(`${apiUrl}/sessions/${sessionId}`, {
        headers: { Authorization: `Bearer ${ownerToken}` },
      })
      .catch(() => undefined);
  }
  await api.dispose();
}

const video = readdirSync(videoDir).find((name) => name.endsWith('.webm'));
if (!video) {
  throw new Error('Playwright did not produce a demo video.');
}

mkdirSync(framesDir, { recursive: true });
execFileSync(
  ffmpeg,
  [
    '-y',
    '-sseof',
    '-11',
    '-i',
    join(videoDir, video),
    '-vf',
    'scale=960:-1',
    '-r',
    '10',
    join(framesDir, 'frame-%04d.png'),
  ],
  { stdio: 'inherit' },
);

const frameNames = readdirSync(framesDir)
  .filter((name) => name.endsWith('.png'))
  .sort();
if (frameNames.length === 0) {
  throw new Error('FFmpeg did not produce demo frames.');
}

const gif = GIFEncoder();
for (const frameName of frameNames) {
  const frame = PNG.sync.read(readFileSync(join(framesDir, frameName)));
  const palette = quantize(frame.data, 128);
  const index = applyPalette(frame.data, palette);
  gif.writeFrame(index, frame.width, frame.height, {
    palette,
    delay: 100,
    repeat: 0,
  });
}
gif.finish();
writeFileSync(outputPath, gif.bytes());

console.log(`Demo GIF written to ${outputPath}`);
