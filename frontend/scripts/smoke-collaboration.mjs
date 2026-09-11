import * as Y from 'yjs';
import * as authProtocol from 'y-protocols/auth';
import { messageAuth, WebsocketProvider } from 'y-websocket';

const API_URL = (process.env.CONCORD_API_URL || 'http://localhost:8000').replace(/\/+$/, '');
const WS_URL = (process.env.CONCORD_WS_URL || 'ws://localhost:8000/ws').replace(/\/+$/, '');
const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
const password = 'Concord-smoke-9!';

async function request(path, { method = 'GET', token, body } = {}) {
  const response = await fetch(`${API_URL}${path}`, {
    method,
    headers: {
      ...(body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });

  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(`${method} ${path} failed (${response.status}): ${JSON.stringify(payload)}`);
  }
  return payload;
}

async function createUser(label) {
  const username = `${label}_${suffix}`;
  await request('/auth/register', {
    method: 'POST',
    body: { username, email: `${username}@example.com`, password },
  });
  const { access_token: token } = await request('/auth/login', {
    method: 'POST',
    body: { username, password },
  });
  const user = await request('/auth/me', { token });
  return { ...user, token };
}

function waitFor(predicate, label, timeoutMs = 12_000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const check = () => {
      if (predicate()) {
        resolve();
      } else if (Date.now() - started >= timeoutMs) {
        reject(new Error(`Timed out waiting for ${label}`));
      } else {
        setTimeout(check, 25);
      }
    };
    check();
  });
}

function connect(user, sessionId) {
  const doc = new Y.Doc();
  const provider = new WebsocketProvider(WS_URL, sessionId, doc, {
    params: { token: user.token },
    disableBc: true,
    maxBackoffTime: 1_000,
  });
  provider.messageHandlers[messageAuth] = (_encoder, decoder) => {
    authProtocol.readAuthMessage(decoder, doc, (_ydoc, reason) => {
      console.warn(`Collaboration permission denied: ${reason}`);
    });
  };
  provider.awareness.setLocalStateField('user', {
    name: user.username,
    color: '#58a6ff',
    userId: user.id,
  });
  return { doc, provider };
}

function disconnect(client) {
  if (!client) return;
  client.provider.destroy();
  client.doc.destroy();
}

const clients = [];
let owner;
let session;

try {
  owner = await createUser('owner');
  const editor = await createUser('editor');
  const viewer = await createUser('viewer');

  session = await request('/sessions', {
    method: 'POST',
    token: owner.token,
    body: { name: `Smoke ${suffix}`, language: 'python' },
  });
  await request(`/sessions/${session.id}/members`, {
    method: 'POST',
    token: owner.token,
    body: { user_id: editor.id, role: 'editor' },
  });
  await request(`/sessions/${session.id}/members`, {
    method: 'POST',
    token: owner.token,
    body: { user_id: viewer.id, role: 'viewer' },
  });

  const ownerClient = connect(owner, session.id);
  const editorClient = connect(editor, session.id);
  const viewerClient = connect(viewer, session.id);
  clients.push(ownerClient, editorClient, viewerClient);

  await Promise.all(clients.map((client, index) => waitFor(
    () => client.provider.wsconnected && client.provider.synced,
    `client ${index + 1} synchronization`,
  )));

  const expectedCode = `print('Concord ${suffix}')`;
  ownerClient.doc.getText('monaco').insert(0, expectedCode);
  await waitFor(
    () => editorClient.doc.getText('monaco').toString() === expectedCode,
    'owner-to-editor document delivery',
  );
  await waitFor(
    () => viewerClient.doc.getText('monaco').toString() === expectedCode,
    'owner-to-viewer document delivery',
  );

  await Promise.all(clients.map((client, index) => waitFor(
    () => client.provider.awareness.getStates().size === 3,
    `client ${index + 1} awareness convergence`,
  )));

  viewerClient.doc.getText('monaco').insert(expectedCode.length, '\nforbidden = True');
  await new Promise((resolve) => setTimeout(resolve, 300));
  if (ownerClient.doc.getText('monaco').toString() !== expectedCode) {
    throw new Error('A viewer update reached the authoritative document');
  }

  disconnect(editorClient);
  clients.splice(clients.indexOf(editorClient), 1);
  const reconnectingEditor = connect(editor, session.id);
  clients.push(reconnectingEditor);
  await waitFor(
    () => reconnectingEditor.provider.synced &&
      reconnectingEditor.doc.getText('monaco').toString() === expectedCode,
    'editor reconnect synchronization',
  );

  clients.splice(0).forEach(disconnect);
  await new Promise((resolve) => setTimeout(resolve, 500));

  const persistedClient = connect(owner, session.id);
  clients.push(persistedClient);
  await waitFor(
    () => persistedClient.provider.synced &&
      persistedClient.doc.getText('monaco').toString() === expectedCode,
    'database-backed document reload',
  );

  console.log(JSON.stringify({
    status: 'ok',
    sessionId: session.id,
    checks: [
      'auth and membership',
      'three-client document sync',
      'three-client awareness',
      'viewer update rejection',
      'editor reconnect',
      'disconnect persistence and reload',
    ],
  }, null, 2));
} finally {
  clients.splice(0).forEach(disconnect);
  if (owner && session) {
    await request(`/sessions/${session.id}`, {
      method: 'DELETE',
      token: owner.token,
    }).catch(() => undefined);
  }
}
