// Сборка: npm run build → dist/ (Django раздаёт как static/app/, страница — /app/).
// Разработка: npm run dev — интерфейс на http://localhost:5173/static/app/, запросы /api и /admin
// уходят на Django (python manage.py runserver на :8000); войдите сначала в http://localhost:8000/admin/.
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  base: "/static/app/",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true, sourcemap: false },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/admin": "http://127.0.0.1:8000",
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test-setup.ts"],
  },
});
