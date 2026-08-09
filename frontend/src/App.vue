<script setup>
import { onMounted, onUnmounted, ref } from 'vue'
import { getConfig } from './api'
import SetupView from './views/SetupView.vue'
import ScanView from './views/ScanView.vue'
import ProgressView from './views/ProgressView.vue'

// 三步向导：0 配置 / 1 汉化 / 2 进度
const current = ref(0)
const taskId = ref('')
// 配置收敛：源语言/版本由后端自动识别，前端只保留目标语言
const config = ref({ target_lang: 'zh_cn' })

// 后端连接状态：checking(检测中…) / ok(已连接) / fail(未连接)
const backendStatus = ref('checking')
let backendTimer = null

async function pingBackend() {
  try {
    await getConfig()
    backendStatus.value = 'ok'
  } catch (e) {
    backendStatus.value = 'fail'
  }
}

onMounted(() => {
  pingBackend()                       // 挂载即 ping 一次
  backendTimer = setInterval(pingBackend, 30000)   // 每 30 秒复查
})
onUnmounted(() => clearInterval(backendTimer))      // 页面卸载清定时器

const steps = [
  { title: '配置', desc: '引擎与目标语言' },
  { title: '汉化', desc: '识别与翻译' },
  { title: '进度', desc: '下载产物' },
]

function next() { current.value = Math.min(2, current.value + 1) }
function back() { current.value = Math.max(0, current.value - 1) }
function onConfigSaved(cfg) { config.value = { ...config.value, ...cfg } }
function onTranslate(id) { taskId.value = id; next() }
</script>

<template>
  <div class="app">
    <aside class="sidebar">
      <h1 class="brand">MC 自动翻译器</h1>
      <p class="slogan">资源包一键汉化</p>
      <div class="conn" :class="backendStatus">
        <span class="dot"></span>
        <span>{{ backendStatus === 'ok' ? '已连接' : backendStatus === 'fail' ? '未连接' : '检测中…' }}</span>
      </div>
      <nav class="steps">
        <button v-for="(s, i) in steps" :key="i" class="step"
                :class="{ active: current === i, done: i < current }" @click="current = i">
          <span class="idx">{{ i + 1 }}</span>
          <span class="txt"><em>{{ s.title }}</em><small>{{ s.desc }}</small></span>
        </button>
      </nav>
      <footer class="side-foot">API Key 仅存本机浏览器</footer>
    </aside>

    <main class="main">
      <SetupView v-show="current === 0" :on-done="onConfigSaved" :on-next="next" />
      <ScanView v-show="current === 1" :target-lang="config.target_lang"
                :on-translate="onTranslate" :on-back="back" />
      <ProgressView v-show="current === 2" :task-id="taskId" :on-back="back" />
    </main>
  </div>
</template>

<style scoped>
.app { display: flex; height: 100%; }

.sidebar {
  width: 230px;
  flex-shrink: 0;
  background: var(--bg-2);
  border-right: 1px solid var(--line);
  padding: 22px 16px;
  display: flex;
  flex-direction: column;
}
.brand { font-size: 18px; margin: 0 0 2px; color: var(--accent); }
.slogan { color: var(--text-dim); font-size: 12px; margin: 0 0 10px; }

/* 后端连接状态指示：绿=已连接 / 红=未连接 / 灰=检测中 */
.conn { display: flex; align-items: center; gap: 8px; margin: 0 0 22px; font-size: 12px; color: var(--text-dim); }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-dim); flex-shrink: 0; }
.conn.ok { color: var(--accent); }
.conn.ok .dot { background: var(--accent); }
.conn.fail { color: var(--danger); }
.conn.fail .dot { background: var(--danger); }

.steps { display: flex; flex-direction: column; gap: 8px; flex: 1; }
.step {
  display: flex;
  align-items: center;
  gap: 12px;
  background: transparent;
  border: 1px solid transparent;
  border-radius: 8px;
  padding: 10px 12px;
  cursor: pointer;
  text-align: left;
  transition: all .15s;
}
.step .idx {
  width: 26px; height: 26px;
  border-radius: 50%;
  background: var(--bg-3);
  border: 1px solid var(--line);
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 13px; color: var(--text-dim);
  flex-shrink: 0;
}
.step .txt { display: flex; flex-direction: column; }
.step em { font-style: normal; color: var(--text); }
.step small { color: var(--text-dim); font-size: 12px; }
.step:hover { background: var(--bg-3); }
.step.active { background: var(--bg-3); border-color: var(--accent); }
.step.active .idx { background: var(--accent); border-color: var(--accent); color: #0b1510; }
.step.active em { color: var(--accent); }
.step.done .idx { background: var(--accent-2); border-color: var(--accent-2); color: #0b1510; }

.side-foot { color: var(--text-dim); font-size: 11px; margin-top: 20px; }

.main { flex: 1; overflow: auto; padding: 34px 40px; }
</style>
