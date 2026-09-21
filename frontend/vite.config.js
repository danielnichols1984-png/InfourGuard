import react, { reactCompilerPreset } from '@vitejs/plugin-react'
import babel from '@rolldown/plugin-babel'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig(({ command }) => ({
  // Production build is served under /app by the backend (see
  // backend/app/main.py) — /login, /dashboard etc. are already taken by
  // the Jinja pages at root. The dev server keeps running at root so
  // `npm run dev` at http://localhost:5173 is unaffected.
  base: command === 'build' ? '/app/' : '/',
  plugins: [
    react(),
    babel({ presets: [reactCompilerPreset()] })
  ],
}))
