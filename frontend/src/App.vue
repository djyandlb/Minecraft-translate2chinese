<script setup>
import { ref } from 'vue'
import SetupView from './views/SetupView.vue'
import ScanView from './views/ScanView.vue'
import ProgressView from './views/ProgressView.vue'

// 三步向导：0 配置 / 1 扫描 / 2 翻译
const current = ref(0)
const taskId = ref('')
const config = ref({ source_lang: 'en_us', target_lang: 'zh_cn' })

const steps = [
  { title: '配置', desc: '引擎与语言' },
  { title: '扫描', desc: '选择资源' },
  { title: '翻译', desc: '进度与结果' },
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
      <ScanView v-show="current === 1" :source-lang="config.source_lang"
                :target-lang="config.target_lang" :on-translate="onTranslate" :on-back="back" />
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
.slogan { color: var(--text-dim); font-size: 12px; margin: 0 0 28px; }

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
