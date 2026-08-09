<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { cancelTask, downloadUrl, getTask, pauseTask } from '../api'

// props：taskId（App 持有的当前运行任务）+ jobs（共享任务队列，已完成汇总从这里取）
const props = defineProps({
  taskId: { type: String, default: '' },
  jobs: { type: Array, default: () => [] },
})

const task = ref(null)
const error = ref('')
let timer = null

const STATUS_TEXT = {
  pending: '等待中', running: '翻译中', paused: '已暂停',
  done: '已完成', failed: '失败', cancelled: '已取消',
}
const STATUS_CLS = { done: 'ok', failed: 'bad', cancelled: 'bad', error: 'bad' }

const isActive = computed(() =>
  !!task.value && ['pending', 'running', 'paused'].includes(task.value.status))
const percent = computed(() => {
  if (!task.value || !task.value.total) return 0
  return Math.round((task.value.done / task.value.total) * 100)
})
// 失败时：取 progress 里最后一条 error 信息
const failInfo = computed(() => {
  const last = (task.value?.progress || []).filter(p => p.status === 'error').pop()
  return last?.error || ''
})
// 取 progress 里最后一条 warn 信息（任务非失败时显示黄色提示）
const warnInfo = computed(() => {
  const last = (task.value?.progress || []).filter(p => p.status === 'warn').pop()
  return last?.error || ''
})
// 最新在前展示明细
const rows = computed(() => (task.value?.progress || []).slice().reverse())

// —— 已完成汇总：jobs 中 done/failed 的列表 ——
const doneJobs = computed(() => props.jobs.filter(j => j.status === 'done' || j.status === 'failed'))
const empty = computed(() => !props.taskId && doneJobs.value.length === 0)

async function refresh() {
  if (!props.taskId) return
  try {
    task.value = await getTask(props.taskId)
    error.value = ''
    if (task.value && ['done', 'failed', 'cancelled'].includes(task.value.status)) stopPolling()
  } catch (e) {
    error.value = `读取任务状态失败：${e.message}`
  }
}
function startPolling() {
  if (!props.taskId) return            // 防空转，无任务 ID 不启动轮询
  stopPolling()                        // 幂等：若已有 timer 先清再设
  timer = setInterval(refresh, 1000)   // running/paused 时每秒轮询
}
function stopPolling() {
  if (timer) { clearInterval(timer); timer = null }
}

async function togglePause() {
  try {
    await pauseTask(props.taskId)
    refresh()
  } catch (e) { error.value = e.message }
}
async function doCancel() {
  try {
    await cancelTask(props.taskId)
    refresh()
  } catch (e) { error.value = e.message }
}
function doDownload() {
  window.open(downloadUrl(props.taskId), '_blank')
}
// 汇总项下载：与当前任务一致用 window.open（pywebview 下 a[target=_blank] 可能新开窗口）
function downloadJob(taskId) {
  if (taskId) window.open(downloadUrl(taskId), '_blank')
}

// 队列逐个切换任务：taskId 变化时重置旧数据并重启轮询，否则停留在旧任务状态
watch(() => props.taskId, (newId) => {
  if (newId) {
    task.value = null
    error.value = ''
    refresh()
    startPolling()
  } else {
    stopPolling()
  }
})

onMounted(() => { refresh(); startPolling() })
onUnmounted(stopPolling)
</script>

<template>
  <section class="panel flow-panel">
    <h2>翻译流程</h2>
    <p class="hint">队列自动逐个翻译，任务运行中每秒刷新</p>

    <!-- 空态：无当前任务也无完成项 -->
    <div v-if="empty" class="empty">还没有任务，请在左侧添加文件并点「开始翻译」</div>

    <template v-else>
      <!-- 当前任务 -->
      <template v-if="taskId">
        <h3 class="sub-title">当前任务</h3>
        <template v-if="task">
          <div class="status-bar">
            <span class="status" :class="STATUS_CLS[task.status] || ''">
              状态：{{ task.paused ? '已暂停' : (STATUS_TEXT[task.status] || task.status) }}
            </span>
            <span class="tokens" v-if="task.tokens_in || task.tokens_out">
              Token：进 {{ task.tokens_in }} / 出 {{ task.tokens_out }}
            </span>
          </div>

          <div class="progress-wrap">
            <div class="progress-track">
              <div class="progress-fill" :style="{ width: percent + '%' }"></div>
            </div>
            <span class="progress-num">{{ percent }}%（{{ task.done }}/{{ task.total }}）</span>
          </div>

          <div v-if="task.failed > 0" class="warn-box">
            有 {{ task.failed }} 条翻译失败，可能因 API Key 无效或网络问题
          </div>
          <div v-if="failInfo" class="fail-box">失败原因：{{ failInfo }}</div>
          <div v-if="warnInfo && task.status !== 'failed'" class="warn-box">{{ warnInfo }}</div>
          <p v-if="error" class="err">{{ error }}</p>

          <div class="actions">
            <button class="btn" :disabled="!isActive" @click="togglePause">
              {{ task.paused ? '继续' : '暂停' }}
            </button>
            <button class="btn danger" :disabled="!isActive" @click="doCancel">取消</button>
            <button v-if="task.status === 'done'" class="btn primary" @click="doDownload">下载资源包</button>
          </div>

          <div class="detail" v-if="rows.length">
            <h3>翻译明细（最新在前）</h3>
            <div class="detail-list">
              <div v-for="(r, i) in rows" :key="r.key + '-' + i" class="detail-row"
                   :class="{ errrow: r.status === 'error' }">
                <div class="row-key">{{ r.key }}</div>
                <div class="row-langs">
                  <span class="src">{{ r.source }}</span>
                  <span class="arrow">→</span>
                  <span class="trans">{{ r.translated }}</span>
                </div>
                <span v-if="r.status === 'error'" class="badge bad">失败</span>
                <span v-else-if="r.status === 'warn'" class="badge warn">警告</span>
              </div>
            </div>
          </div>
          <p v-else class="tip">暂无明细</p>
        </template>
        <p v-else-if="error" class="err">{{ error }}</p>
        <p v-else class="tip">加载中…</p>
      </template>
      <div v-else class="idle">当前无运行任务</div>

      <!-- 已完成汇总 -->
      <div class="done-summary" v-if="doneJobs.length">
        <h3 class="sub-title">已完成汇总</h3>
        <div class="done-list">
          <div v-for="(job, i) in doneJobs" :key="job.taskId || i" class="done-row">
            <span class="done-icon" :class="job.status">{{ job.status === 'done' ? '✓' : '✗' }}</span>
            <span class="done-name" :title="job.path">{{ job.name }}</span>
            <span class="done-res" :class="job.status">
              {{ job.status === 'done' ? '已汉化' : (job.error || '失败') }}
            </span>
            <button v-if="job.status === 'done' && job.taskId" class="btn mini" @click="downloadJob(job.taskId)">下载</button>
          </div>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.flow-panel { max-width: 100%; padding: 0; }
.sub-title { margin: 0 0 8px; font-size: 15px; color: var(--accent); }

.empty { color: var(--text-dim); padding: 40px 0; text-align: center; }
.idle { color: var(--text-dim); padding: 18px 0 4px; font-size: 13px; }

.status-bar { display: flex; gap: 16px; margin-bottom: 14px; }
.status { font-weight: 600; }
.status.ok { color: var(--accent); }
.status.bad { color: var(--danger); }
.tokens { color: var(--text-dim); }

.progress-wrap { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.progress-track {
  flex: 1; height: 10px; border-radius: 5px;
  background: var(--bg-3); overflow: hidden;
}
.progress-fill { height: 100%; background: var(--accent); transition: width .3s; }
.progress-num { color: var(--text-dim); white-space: nowrap; }

.fail-box {
  border: 1px solid var(--danger); color: var(--danger);
  border-radius: 6px; padding: 10px; margin-bottom: 10px;
}
.warn-box {
  border: 1px solid var(--warn); color: var(--warn);
  background: rgba(232, 163, 61, .08);
  border-radius: 6px; padding: 10px; margin-bottom: 10px;
}

.detail { margin-top: 20px; }
.detail h3 { margin: 0 0 8px; font-size: 14px; color: var(--text-dim); }
.detail-list {
  max-height: 320px; overflow: auto;
  border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg-2); padding: 4px;
}
.detail-row {
  display: flex; align-items: baseline; gap: 10px;
  padding: 7px 10px; border-bottom: 1px solid var(--line);
}
.detail-row:last-child { border-bottom: none; }
.detail-row.errrow { background: rgba(232,106,94,.08); }
.row-key { color: var(--accent); font-size: 12px; min-width: 140px; max-width: 180px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-langs { flex: 1; display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.row-langs .src { color: var(--text-dim); }
.row-langs .trans { color: var(--text); }
.arrow { color: var(--text-dim); }
.badge { font-size: 12px; }
.badge.bad { color: var(--danger); }
.badge.warn { color: var(--warn); }

/* 已完成汇总 */
.done-summary { margin-top: 26px; }
.done-list {
  border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg-2); padding: 4px;
}
.done-row {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 10px; border-bottom: 1px solid var(--line);
}
.done-row:last-child { border-bottom: none; }
.done-icon { width: 18px; text-align: center; flex-shrink: 0; font-weight: 700; }
.done-icon.done { color: var(--accent); }
.done-icon.failed { color: var(--danger); }
.done-name {
  flex: 1; min-width: 0; font-size: 13px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.done-res { font-size: 12px; flex-shrink: 0; max-width: 40%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.done-res.done { color: var(--accent); }
.done-res.failed { color: var(--danger); }
.btn.mini { padding: 3px 10px; font-size: 12px; flex-shrink: 0; }
</style>
