import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// 开发期代理 /api 到本机 FastAPI（默认 8000），前端代码统一用相对路径
export default defineConfig({
  plugins: [react()],
  server: {
    // host 固定在 npm script 里传 --host 127.0.0.1（本机 config 内 host 字段实测不生效）
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_PROXY_TARGET ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
