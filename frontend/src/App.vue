<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { autoTranslate, detect, getConfig, getTask, uploadFile } from './api'
import SetupView from './views/SetupView.vue'
import ScanView from './views/ScanView.vue'
import ProgressView from './views/ProgressView.vue'

// —— 配置持久：localStorage 记录「已配置过」→ 首次开屏弹配置面板，非首次直接主界面 ——
const CONFIGURED_KEY = 'mc_translator_configured'
const configured = ref(localStorage.getItem(CONFIGURED_KEY) === '1')
const showSetup = ref(!configured.value)        // 首次开屏 → 弹配置面板
const config = ref({ target_lang: 'zh_cn' })    // 前端只保留目标语言（源语言/版本后端自动识别）

// —— 后端连接状态：O1 逻辑，/api/config 定时 ping ——
const backendStatus = ref('checking')           // checking(检测中…) / ok(已连接) / fail(未连接)
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
  pingBackend()
  backendTimer = setInterval(pingBackend, 30000)   // 每 30 秒复查
})
onUnmounted(() => clearInterval(backendTimer))

// —— 多文件任务队列（左上传区 + 右流程区共享）——
// job: { path, name, kind, detectResult, status: pending|running|done|failed, taskId, error }
const jobs = ref([])
const currentTaskId = ref('')      // 当前运行中任务，喂给右栏流程区轮询
const processing = ref(false)      // 队列是否在处理中
const detectingCount = ref(0)      // 正在识别中的文件数（>0 时禁用添加）

const doneCount = computed(() => jobs.value.filter(j => j.status === 'done').length)
const failCount = computed(() => jobs.value.filter(j => j.status === 'failed').length)

// —— 入队：本地路径（桌面选文件/目录、浏览器浏览、拖放拿到本地路径）→ detect → 加入 ——
function basename(p) {
  const s = String(p || '').replace(/\\/g, '/')
  return s.split('/').pop() || s
}
async function addPath(path, name) {
  detectingCount.value++
  try {
    const r = await detect({ path, target_lang: config.value.target_lang })
    if (r.kind === 'unknown') {
      jobs.value.push({ path, name: name || basename(path), kind: 'unknown', detectResult: null, status: 'failed', error: '无法识别输入类型（需整合包目录 / mod jar / 地图存档）' })
      return
    }
    jobs.value.push({ path, name: name || basename(path), kind: r.kind, detectResult: r, status: 'pending', taskId: '', error: '' })
  } catch (e) {
    jobs.value.push({ path, name: name || basename(path), kind: 'unknown', detectResult: null, status: 'failed', error: `识别失败：${e.message}` })
  } finally {
    detectingCount.value--
  }
}

// —— 入队：上传文件（浏览器/桌面拿不到本地路径时走 uploadFile 兜底）→ 复用 addPath 的 detect ——
async function addUpload(file) {
  detectingCount.value++
  try {
    const r = await uploadFile(file)
    await addPath(r.path, file.name)
  } catch (e) {
    jobs.value.push({ path: '', name: file.name, kind: 'unknown', detectResult: null, status: 'failed', error: `上传失败：${e.message}` })
  } finally {
    detectingCount.value--
  }
}

// —— 队列：串行逐个 autoTranslate ——
function waitTaskDone(taskId) {
  return new Promise((resolve) => {
    const t = setInterval(async () => {
      try {
        const st = await getTask(taskId)
        if (['done', 'failed', 'cancelled'].includes(st.status)) {
          clearInterval(t); resolve(st)
        }
      } catch (e) {
        clearInterval(t); resolve(null)   // 读取失败按终止处理，避免卡死队列
      }
    }, 1000)
  })
}
async function startQueue() {
  if (processing.value) return
  processing.value = true
  const pendings = jobs.value.filter(j => j.status === 'pending')
  for (const job of pendings) {
    if (!processing.value) break           // 清空/外部中止时退出
    job.status = 'running'
    job.error = ''
    try {
      const r = await autoTranslate({ path: job.path, target_lang: config.value.target_lang })
      job.taskId = r.task_id
      currentTaskId.value = r.task_id       // 喂给右栏显示当前任务进度
      const final = await waitTaskDone(r.task_id)
      if (final && final.status === 'done') {
        job.status = 'done'
      } else {
        job.status = 'failed'
        job.error = final ? (final.status === 'cancelled' ? '已取消' : '翻译失败') : '读取任务状态失败'
      }
    } catch (e) {
      job.status = 'failed'
      job.error = `启动翻译失败：${e.message}`
    }
  }
  processing.value = false
  currentTaskId.value = ''                  // 全部结束，右栏回到空态（汇总常驻）
}

// —— 任务列表操作 ——
function removeJob(i) {
  if (jobs.value[i]?.status === 'running') return
  jobs.value.splice(i, 1)
}
function clearJobs() {
  if (processing.value) return
  jobs.value = []
  currentTaskId.value = ''
}

// —— 配置弹窗：保存后写 localStorage 标记；设置按钮可随时重开 ——
function onConfigSaved(cfg) { config.value = { ...config.value, ...cfg } }
function closeSetup() {
  showSetup.value = false
  configured.value = true
  localStorage.setItem(CONFIGURED_KEY, '1')
}
function openSetup() { showSetup.value = true }
</script>

<template>
  <div class="app">
    <!-- 顶栏：品牌 + 连接状态点 + 设置按钮 -->
    <header class="topbar">
      <h1 class="brand">MC 自动翻译器</h1>
      <span class="slogan">资源包一键汉化</span>
      <span class="spacer"></span>
      <div class="conn" :class="backendStatus">
        <span class="dot"></span>
        <span>{{ backendStatus === 'ok' ? '已连接' : backendStatus === 'fail' ? '未连接' : '检测中…' }}</span>
      </div>
      <button class="btn" @click="openSetup">⚙ 设置</button>
    </header>

    <!-- 主区左右分栏：左上传区 / 右流程区 -->
    <main class="workspace">
      <ScanView class="col left"
                :jobs="jobs" :processing="processing" :detecting="detectingCount > 0"
                :add-path="addPath" :add-upload="addUpload"
                @remove-job="removeJob" @clear-jobs="clearJobs" @start-queue="startQueue" />
      <ProgressView class="col right" :task-id="currentTaskId" :jobs="jobs" />
    </main>

    <!-- 配置弹窗：首次开屏（强制配置）或 ⚙设置 随时打开 -->
    <div v-if="showSetup" class="overlay">
      <div class="dialog">
        <SetupView :on-done="onConfigSaved" :on-close="closeSetup" :closable="configured" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.app { display: flex; flex-direction: column; height: 100%; }

/* —— 顶栏 —— */
.topbar {
  display: flex; align-items: center; gap: 14px;
  padding: 0 20px; height: 56px; flex-shrink: 0;
  background: var(--bg-2); border-bottom: 1px solid var(--line);
}
.topbar .brand { margin: 0; font-size: 17px; color: var(--accent); }
.topbar .slogan { color: var(--text-dim); font-size: 12px; margin: 0; }
.spacer { flex: 1; }

/* 后端连接状态指示：绿=已连接 / 红=未连接 / 灰=检测中 */
.conn { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-dim); }
.conn .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-dim); flex-shrink: 0; }
.conn.ok { color: var(--accent); }
.conn.ok .dot { background: var(--accent); }
.conn.fail { color: var(--danger); }
.conn.fail .dot { background: var(--danger); }

/* —— 主区左右分栏 —— */
.workspace { flex: 1; display: flex; min-height: 0; }
.col { overflow: auto; padding: 20px 22px; }
.col.left { width: 460px; flex-shrink: 0; border-right: 1px solid var(--line); background: var(--bg); }
.col.right { flex: 1; min-width: 0; }

/* —— 配置弹窗遮罩 —— */
.overlay {
  position: fixed; inset: 0; z-index: 50;
  background: rgba(6, 10, 13, .62);
  display: flex; align-items: center; justify-content: center;
}
.dialog {
  background: var(--bg-2); border: 1px solid var(--line);
  border-radius: 12px; padding: 24px 26px;
  width: 540px; max-width: 92vw; max-height: 88vh; overflow: auto;
  box-shadow: 0 14px 44px rgba(0, 0, 0, .45);
}
</style>
