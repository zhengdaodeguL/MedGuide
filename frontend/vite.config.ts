import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiProxyTarget = process.env.MEDGUIDE_API_PROXY_TARGET
    || process.env.MEDGUIDE_API_PROXY
    || env.MEDGUIDE_API_PROXY_TARGET
    || env.MEDGUIDE_API_PROXY
    || env.VITE_API_PROXY;

  const proxy = apiProxyTarget ? {
    "/api": {
      target: apiProxyTarget,
      changeOrigin: true,
    },
  } : undefined;

  return {
    plugins: [react()],
    server: {
      port: 5173,
      ...(proxy ? { proxy } : {}),
    },
    // `vite preview` serves the production build without the dev server,
    // so it needs the same /api proxy or /api/auth/me answers with HTML.
    preview: {
      port: 4173,
      ...(proxy ? { proxy } : {}),
    },
  };
});
