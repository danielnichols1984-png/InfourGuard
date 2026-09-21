import axios from "axios";

// In production the built SPA is served by the same backend it talks to
// (see backend/app/main.py, mounted at /app), so a relative baseURL keeps
// requests same-origin. `npm run dev` still needs the absolute local URL
// since Vite serves the SPA on a different port (5173) than the API (8000).
export const api = axios.create({
  baseURL: import.meta.env.PROD ? "" : "http://127.0.0.1:8000",
  withCredentials: true,
});
