import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

const monacoEditorApi = fileURLToPath(
  new URL('./node_modules/monaco-editor/esm/vs/editor/editor.api.js', import.meta.url),
);

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      'monaco-editor/esm/vs/editor/editor.api.js': monacoEditorApi,
    },
  },
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    restoreMocks: true,
  },
});
