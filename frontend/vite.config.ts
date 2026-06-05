import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Vite 配置：启用 React 插件；dev 时把 /api 代理到本地后端(将来真实对接用)
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 前端 dev 服务器把 /api 请求转发到后端 FastAPI(默认 8000)
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
})
