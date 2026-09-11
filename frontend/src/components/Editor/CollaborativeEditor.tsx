/**
 * CollaborativeEditor — Monaco editor with Yjs CRDT binding.
 *
 * Initializes a Y.Doc, connects to the backend via WebSocket,
 * and binds the Yjs text type to the Monaco editor model.
 * Handles cursor decorations, read-only mode for viewers,
 * and cleanup on unmount.
 */

import { useEffect, useRef, useCallback } from 'react';
import Editor from '@monaco-editor/react';
import type { OnMount } from '@monaco-editor/react';
import * as Y from 'yjs';
import * as authProtocol from 'y-protocols/auth';
import { messageAuth, WebsocketProvider } from 'y-websocket';
import { MonacoBinding } from 'y-monaco';
import { WS_URL } from '../../config';
import { useAuthStore } from '../../store/authStore';
import type { ConnectionStatus, PresenceUser } from '../../types/collaboration';

interface Props {
  sessionId: string;
  language: string;
  role: 'viewer' | 'editor' | 'owner';
  onConnectionChange?: (status: ConnectionStatus) => void;
  onPresenceUpdate?: (users: PresenceUser[]) => void;
}

// Map Concord language names to Monaco language IDs
const LANGUAGE_MAP: Record<string, string> = {
  python: 'python',
  cpp: 'cpp',
  javascript: 'javascript',
};

const PRESENCE_COLORS = [
  '#58a6ff',
  '#3fb950',
  '#d29922',
  '#f85149',
  '#a371f7',
  '#db61a2',
  '#39c5cf',
  '#e3b341',
] as const;

function presenceColorFor(userId: string) {
  let hash = 0;
  for (const character of userId) {
    hash = (hash * 31 + character.charCodeAt(0)) >>> 0;
  }
  return PRESENCE_COLORS[hash % PRESENCE_COLORS.length];
}

function isPresenceUser(value: unknown): value is PresenceUser {
  if (!value || typeof value !== 'object') return false;

  const candidate = value as Partial<PresenceUser>;
  return typeof candidate.name === 'string' &&
    typeof candidate.color === 'string' &&
    typeof candidate.userId === 'string';
}

export function CollaborativeEditor({
  sessionId,
  language,
  role,
  onConnectionChange,
  onPresenceUpdate,
}: Props) {
  const token = useAuthStore((s) => s.token);
  const user = useAuthStore((s) => s.user);
  const docRef = useRef<Y.Doc | null>(null);
  const providerRef = useRef<WebsocketProvider | null>(null);
  const bindingRef = useRef<MonacoBinding | null>(null);
  const presenceCleanupRef = useRef<(() => void) | null>(null);

  const handleEditorMount: OnMount = useCallback(
    (editor) => {
      if (!token || !user) return;

      // 1. Create Yjs document
      const ydoc = new Y.Doc();
      docRef.current = ydoc;

      // 2. Connect to backend WebSocket with JWT
      const provider = new WebsocketProvider(WS_URL, sessionId, ydoc, {
        params: { token },
        connect: true,
        maxBackoffTime: 30_000,
      });
      providerRef.current = provider;

      // y-websocket's default auth handler logs provider.url, which contains
      // the query-string JWT. Keep permission feedback without exposing it.
      provider.messageHandlers[messageAuth] = (_encoder, decoder) => {
        authProtocol.readAuthMessage(decoder, ydoc, (_doc, reason) => {
          console.warn(`Collaboration permission denied: ${reason}`);
        });
      };

      // 3. Set awareness (cursor info)
      provider.awareness.setLocalStateField('user', {
        name: user.username,
        color: presenceColorFor(user.id),
        userId: user.id,
      });

      // 4. Connection status tracking
      provider.on('status', (event: { status: string }) => {
        const status: ConnectionStatus = event.status === 'connected' ? 'connected' :
          event.status === 'connecting' ? 'connecting' : 'disconnected';
        onConnectionChange?.(status);
      });

      // 5. Awareness change tracking (presence)
      const updatePresence = () => {
        const usersById = new Map<string, PresenceUser>();
        provider.awareness.getStates().forEach((state) => {
          if (isPresenceUser(state.user)) {
            usersById.set(state.user.userId, state.user);
          }
        });
        onPresenceUpdate?.(Array.from(usersById.values()));
      };
      provider.awareness.on('change', updatePresence);
      presenceCleanupRef.current = () => {
        provider.awareness.off('change', updatePresence);
      };
      updatePresence();

      // 6. Bind Yjs text to Monaco
      const ytext = ydoc.getText('monaco');
      const binding = new MonacoBinding(
        ytext,
        editor.getModel()!,
        new Set([editor]),
        provider.awareness
      );
      bindingRef.current = binding;
    },
    [token, user, sessionId, onConnectionChange, onPresenceUpdate]
  );

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      presenceCleanupRef.current?.();
      bindingRef.current?.destroy();
      providerRef.current?.destroy();
      docRef.current?.destroy();
      presenceCleanupRef.current = null;
      bindingRef.current = null;
      providerRef.current = null;
      docRef.current = null;
    };
  }, []);

  const monacoLanguage = LANGUAGE_MAP[language] || 'plaintext';
  const isReadOnly = role === 'viewer';

  return (
    <div style={{ height: '100%', width: '100%' }}>
      <Editor
        height="100%"
        language={monacoLanguage}
        theme="vs-dark"
        onMount={handleEditorMount}
        options={{
          readOnly: isReadOnly,
          fontSize: 14,
          fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
          minimap: { enabled: true },
          lineNumbers: 'on',
          scrollBeyondLastLine: false,
          wordWrap: 'off',
          automaticLayout: true,
          padding: { top: 12 },
          cursorBlinking: 'smooth',
          smoothScrolling: true,
          renderWhitespace: 'selection',
          bracketPairColorization: { enabled: true },
        }}
      />
    </div>
  );
}
