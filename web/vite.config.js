import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发时把两类请求分别代理到业务后端与智能体服务，
// 前端代码里统一用相对路径，不用关心端口，也就不存在跨域问题。
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/agent': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
