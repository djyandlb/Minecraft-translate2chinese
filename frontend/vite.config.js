import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // 相对路径 base：build 产物资源用 ./assets 相对引用，
  // 避免 file:// 直开 dist 或挂载子路径时绝对路径 /assets 解析错乱
  base: './',
  // v1.5.0：构建目标降到 es2017——默认 'modules'(es2020) 的现代 JS（可选链/class 字段）
  // 在老版 WebView2 Runtime（Chromium <87）上解析失败 → 应用白屏（用户「其他电脑打开
  // 白屏」根因）。es2017 兼容老 WebView2，Vue3 运行时要求 es2015+，安全兼容。
  build: {
    target: 'es2017'
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000'
    }
  }
})
