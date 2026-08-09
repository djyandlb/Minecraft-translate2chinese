import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  // 相对路径 base：build 产物资源用 ./assets 相对引用，
  // 避免 file:// 直开 dist 或挂载子路径时绝对路径 /assets 解析错乱
  base: './',
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8000'
    }
  }
})
