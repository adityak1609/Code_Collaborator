import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import { fileURLToPath } from 'node:url'

const monacoEditorApi = fileURLToPath(
  new URL('./node_modules/monaco-editor/esm/vs/editor/editor.api.js', import.meta.url),
)

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  resolve: {
    // y-monaco still uses this legacy deep import, which Monaco 0.56's exports
    // map no longer resolves even though the module remains in the package.
    alias: {
      'monaco-editor/esm/vs/editor/editor.api.js': monacoEditorApi,
    },
  },
})
