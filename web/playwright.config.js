// 端到端（E2E）测试配置：用真浏览器打开网站，模拟用户点按钮、填表单。
//
// 跑之前要先把网站起来（业务后端在 8080 直接提供打包好的页面）：
//   cd web && npm run build          # 打包页面
//   start-everything.bat 或 docker compose up -d
//   cd web && npm run e2e            # 跑测试
//
// 测试只依赖业务后端和演示数据，不需要智能体和大模型——
// 助手那条用例只检查面板能打开、离线时也有开场白。
import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  expect: { timeout: 8_000 },
  // CI 上偶发的网络抖动重试一次；本地不重试，失败就是失败
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://localhost:8080',
    // 本地直接用电脑上装好的 Edge，不用另外下载浏览器；CI 上用 Playwright 自带的 Chromium
    channel: process.env.CI ? undefined : (process.env.E2E_CHANNEL || 'msedge'),
    locale: 'zh-CN',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
})
