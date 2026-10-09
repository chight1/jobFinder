import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vitest/config";

// `npm run build` writes dist/, which `jobhunt serve` serves under /app/. `npm run dev` serves the app
// on its own port and passes API and stylesheet requests to the running jobhunt server.
export default defineConfig({
  base: "/app/",
  plugins: [vue()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/static": "http://127.0.0.1:8765",
    },
  },
  test: { environment: "jsdom" },
});
