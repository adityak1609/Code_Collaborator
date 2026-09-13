import { createHash } from 'node:crypto';
import * as Y from 'yjs';
import * as decoding from 'lib0/decoding';
import * as authProtocol from 'y-protocols/auth';
import { messageAuth, WebsocketProvider } from 'y-websocket';

const API_URL = (process.env.CONCORD_API_URL || 'http://localhost:8000').replace(/\/+$/, '');
const WS_URL = (process.env.CONCORD_WS_URL || 'ws://localhost:8000/ws').replace(/\/+$/, '');
const suffix = `${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
const password = 'Concord-smoke-9!';
const MESSAGE_DOCUMENT_SAVED = 4;

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

async function saveDocument(sessionId, user, doc) {
  const response = await fetch(`${API_URL}/sessions/${sessionId}/save`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${user.token}`,
      'Content-Type': 'application/octet-stream',
    },
    body: Y.encodeStateAsUpdate(doc),
  });
  const payload = await response.json().catch(() => null);
  return { response, payload };
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

async function waitForAsync(predicate, label, timeoutMs = 12_000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

function documentHash(doc) {
  return createHash('sha256')
    .update(Y.encodeStateAsUpdate(doc))
    .digest('hex');
}

function connect(user, sessionId) {
  const doc = new Y.Doc();
  const saveEvents = [];
  const closeEvents = [];
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
  provider.messageHandlers[MESSAGE_DOCUMENT_SAVED] = (_encoder, decoder) => {
    const message = JSON.parse(decoding.readVarString(decoder));
    if (message?.type === 'document_saved') saveEvents.push(message);
  };
  provider.on('connection-close', (event) => {
    if (event) closeEvents.push({ code: event.code, reason: event.reason });
  });
  provider.awareness.setLocalStateField('user', {
    name: user.username,
    color: '#58a6ff',
    userId: user.id,
  });
  return { doc, provider, saveEvents, closeEvents };
}

function disconnect(client) {
  if (!client) return;
  client.provider.destroy();
  client.doc.destroy();
}

const clients = [];
let owner;
let session;
let sessionClosed = false;

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

  let expectedCode = `print('Concord ${suffix}')`;
  ownerClient.doc.getText('monaco').insert(0, expectedCode);
  await waitFor(
    () => editorClient.doc.getText('monaco').toString() === expectedCode,
    'owner-to-editor document delivery',
  );
  await waitFor(
    () => viewerClient.doc.getText('monaco').toString() === expectedCode,
    'owner-to-viewer document delivery',
  );

  const explicitSave = await saveDocument(session.id, owner, ownerClient.doc);
  if (!explicitSave.response.ok || explicitSave.payload?.size_bytes <= 0) {
    throw new Error(
      `Explicit save failed (${explicitSave.response.status}): ` +
      JSON.stringify(explicitSave.payload),
    );
  }
  const savedStateVector = Buffer.from(
    Y.encodeStateVector(ownerClient.doc),
  ).toString('base64');
  if (explicitSave.payload?.state_vector !== savedStateVector) {
    throw new Error('Explicit save returned the wrong state vector');
  }
  const savedStateHash = documentHash(ownerClient.doc);
  if (explicitSave.payload?.state_hash !== savedStateHash || explicitSave.payload?.dirty) {
    throw new Error('Explicit save returned the wrong state hash');
  }
  await Promise.all(clients.map((client, index) => waitFor(
    () => client.saveEvents.some((event) =>
      event.state_hash === savedStateHash &&
      event.dirty === false &&
      event.saved_by === owner.username),
    `client ${index + 1} saved-state event`,
  )));

  const collaboratorSuffix = '\n# collaborator edit';
  editorClient.doc.getText('monaco').insert(expectedCode.length, collaboratorSuffix);
  expectedCode += collaboratorSuffix;
  await waitFor(
    () => ownerClient.doc.getText('monaco').toString() === expectedCode &&
      viewerClient.doc.getText('monaco').toString() === expectedCode,
    'editor contribution delivery',
  );

  const deletionIndex = expectedCode.indexOf('Concord');
  ownerClient.doc.getText('monaco').delete(deletionIndex, 1);
  expectedCode = expectedCode.slice(0, deletionIndex) + expectedCode.slice(deletionIndex + 1);
  await waitFor(
    () => editorClient.doc.getText('monaco').toString() === expectedCode &&
      viewerClient.doc.getText('monaco').toString() === expectedCode,
    'deletion-only update delivery',
  );

  const deletionSave = await saveDocument(session.id, owner, ownerClient.doc);
  const deletionHash = documentHash(ownerClient.doc);
  if (!deletionSave.response.ok ||
      deletionSave.payload?.state_hash !== deletionHash ||
      deletionSave.payload?.dirty) {
    throw new Error(
      `Deletion save failed (${deletionSave.response.status}): ` +
      JSON.stringify(deletionSave.payload),
    );
  }
  if (clients.some((client) => documentHash(client.doc) !== deletionHash)) {
    throw new Error('Multi-client documents produced different canonical state hashes');
  }
  await Promise.all(clients.map((client, index) => waitFor(
    () => client.saveEvents.some((event) =>
      event.state_hash === deletionHash && event.dirty === false),
    `client ${index + 1} deletion saved-state event`,
  )));

  await Promise.all(clients.map((client, index) => waitFor(
    () => client.provider.awareness.getStates().size === 3,
    `client ${index + 1} awareness convergence`,
  )));

  viewerClient.doc.getText('monaco').insert(expectedCode.length, '\nforbidden = True');
  await new Promise((resolve) => setTimeout(resolve, 300));
  if (ownerClient.doc.getText('monaco').toString() !== expectedCode) {
    throw new Error('A viewer update reached the authoritative document');
  }

  const viewerSave = await saveDocument(session.id, viewer, viewerClient.doc);
  if (viewerSave.response.status !== 403) {
    throw new Error(`Viewer save returned ${viewerSave.response.status}, expected 403`);
  }

  await request(`/sessions/${session.id}/members/${viewer.id}`, {
    method: 'DELETE',
    token: owner.token,
  });
  await waitFor(
    () => viewerClient.closeEvents.some((event) => event.code === 4403),
    'removed viewer permanent disconnect',
  );

  disconnect(editorClient);
  clients.splice(clients.indexOf(editorClient), 1);
  const reconnectingEditor = connect(editor, session.id);
  clients.push(reconnectingEditor);
  await waitFor(
    () => reconnectingEditor.provider.synced &&
      reconnectingEditor.doc.getText('monaco').toString() === expectedCode,
    'editor reconnect synchronization',
  );

  await request(`/sessions/${session.id}/members/${editor.id}`, {
    method: 'PATCH',
    token: owner.token,
    body: { role: 'viewer' },
  });
  await waitFor(
    () => reconnectingEditor.closeEvents.some((event) => event.code === 4001),
    'editor role-change disconnect',
  );
  await waitFor(
    () => reconnectingEditor.provider.wsconnected && reconnectingEditor.provider.synced,
    'demoted editor reauthentication',
  );
  reconnectingEditor.doc.getText('monaco').insert(
    expectedCode.length,
    '\nrole_change_forbidden = True',
  );
  await new Promise((resolve) => setTimeout(resolve, 300));
  if (ownerClient.doc.getText('monaco').toString() !== expectedCode) {
    throw new Error('A demoted editor update reached the authoritative document');
  }

  disconnect(reconnectingEditor);
  clients.splice(clients.indexOf(reconnectingEditor), 1);
  const demotedViewer = connect(editor, session.id);
  clients.push(demotedViewer);
  await waitFor(
    () => demotedViewer.provider.synced &&
      demotedViewer.doc.getText('monaco').toString() === expectedCode,
    'demoted member viewer synchronization',
  );

  const recoveredCode = `${expectedCode}\n# recovered from Redis`;
  ownerClient.doc.getText('monaco').insert(expectedCode.length, '\n# recovered from Redis');
  await waitFor(
    () => demotedViewer.doc.getText('monaco').toString() === recoveredCode,
    'unsaved edit delivery before checkpoint',
  );

  clients.splice(0).forEach(disconnect);
  await waitForAsync(
    async () => (await request('/health')).active_sessions === 0,
    'last-user document checkpoint and eviction',
  );

  const recoveredClient = connect(owner, session.id);
  clients.push(recoveredClient);
  await waitFor(
    () => recoveredClient.provider.synced &&
      recoveredClient.doc.getText('monaco').toString() === recoveredCode,
    'Redis-backed document recovery',
  );

  await request(`/sessions/${session.id}`, {
    method: 'DELETE',
    token: owner.token,
  });
  sessionClosed = true;
  await waitFor(
    () => recoveredClient.closeEvents.some((event) => event.code === 4410),
    'closed-session permanent disconnect',
  );

  console.log(JSON.stringify({
    status: 'ok',
    sessionId: session.id,
    checks: [
      'auth and membership',
      'three-client document sync',
      'three-client awareness',
      'explicit PostgreSQL save',
      'shared saved-state metadata',
      'multi-client deletion-aware save hash',
      'viewer update rejection',
      'viewer save rejection',
      'removed-member live revocation',
      'editor reconnect',
      'role-change reauthentication and demotion fence',
      'Redis checkpoint recovery of unsaved edits',
      'closed-session live revocation',
    ],
  }, null, 2));
} finally {
  clients.splice(0).forEach(disconnect);
  if (owner && session && !sessionClosed) {
    await request(`/sessions/${session.id}`, {
      method: 'DELETE',
      token: owner.token,
    }).catch(() => undefined);
  }
}
