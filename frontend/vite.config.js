import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `npm run dev` proxies /api to a local backend. To develop against a test
// box instead, set JAV3_API=http://host:8000. A box behind a login also needs
// JAV3_COOKIE (the "name=value" of a session cookie; keep it in the
// environment, never in a file): it is attached to every proxied request, and
// the browser's own Origin/Referer are dropped so the box's same-origin gate
// sees a plain non-browser client, not a page from localhost:5173.
const target = process.env.JAV3_API || 'http://localhost:8000'
const cookie = process.env.JAV3_COOKIE || ''

export default defineConfig({
  plugins: [react()],
  // @novnc/novnc 1.7 uses a top-level await (core/util/browser.js), which the default
  // target (es2020, safari14) refuses. es2022 allows it, in browsers from 2021 on.
  build: { target: ['es2022', 'chrome89', 'edge89', 'firefox89', 'safari15'] },
  server: {
    proxy: {
      '/api': {
        target,
        changeOrigin: !!process.env.JAV3_API,
        ...(cookie ? {
          configure: (proxy) => {
            proxy.on('proxyReq', (req) => {
              req.setHeader('cookie', cookie)
              for (const h of ['origin', 'referer', 'sec-fetch-site', 'sec-fetch-mode', 'sec-fetch-dest'])
                req.removeHeader(h)
            })
          },
        } : {}),
      },
    },
  },
})
