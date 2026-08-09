<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { cancelTask, downloadUrl, getTask, pauseTask } from '../api'

// props：taskId（由 App 持有，从扫描步传入）、onBack()
const props = defineProps({ taskId: { type: String, default: '' }, onBack: Function })

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
// M4-recheck：取 progress 里最后一条 warn 信息（任务非失败时显示黄色提示）
const warnInfo = computed(() => {
  const last = (task.value?.progress || []).filter(p => p.status === 'warn').pop()
  return last?.error || ''
})
// 最新在前展示明细
const rows = computed(() => (task.value?.progress || []).slice().reverse())

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
  if (!props.taskId) return            // M3：防空转，无任务 ID 不启动轮询
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

// App 用 v-show 常驻挂载，onMounted 只跑一次；第一个任务结束 stopPolling 后，
// 第二次任务 taskId 变化时需重置旧数据并重启轮询，否则进度页停留在旧任务状态
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
  <section class="panel">
    <h2>③ 翻译进度</h2>
    <p class="hint">任务运行中会每秒自动刷新</p>

    <div v-if="!taskId" class="empty">还没有进行中的任务，请先在扫描页发起翻译</div>

    <template v-else-if="task">
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
        <button class="btn" @click="props.onBack?.()">上一步</button>
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
  </section>
</template>

<style scoped>
.empty { color: var(--text-dim); padding: 40px 0; }

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

.detail { margin-top: 26px; }
.detail h3 { margin: 0 0 8px; font-size: 16px; }
.detail-list {
  max-height: 420px; overflow: auto;
  border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg-2); padding: 4px;
}
.detail-row {
  display: flex; align-items: baseline; gap: 10px;
  padding: 7px 10px; border-bottom: 1px solid var(--line);
}
.detail-row:last-child { border-bottom: none; }
.detail-row.errrow { background: rgba(232,106,94,.08); }
.row-key { color: var(--accent); font-size: 12px; min-width: 160px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.row-langs { flex: 1; display: flex; gap: 8px; align-items: baseline; min-width: 0; }
.row-langs .src { color: var(--text-dim); }
.row-langs .trans { color: var(--text); }
.arrow { color: var(--text-dim); }
.badge { font-size: 12px; }
.badge.bad { color: var(--danger); }
.badge.warn { color: var(--warn); }
</style>
