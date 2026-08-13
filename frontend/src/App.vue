<script setup>
import { nextTick, onMounted, onUnmounted, ref } from 'vue'
import { autoTranslate, deleteProject, detect, getConfig, getTask, listProjects, taskStreamUrl, uploadFile } from './api'
import SetupView from './views/SetupView.vue'
import ScanView from './views/ScanView.vue'
import ProgressView from './views/ProgressView.vue'

// —— 配置持久：开屏判断改后端 config.json（pywebview 的 localStorage 不持久，
//    改用后端 configured 标记跨启动保留：保存过 → 不弹，否则首次开屏弹配置）——
const configured = ref(false)
const showSetup = ref(false)                    // getConfig 返回后：未配置才弹（已配置用户不闪窗）
const config = ref({ target_lang: 'zh_cn' })    // 前端只保留目标语言（源语言/版本后端自动识别）
// 后端就绪时是否已完成「开屏配置判断」（S2 修复：后端未就绪时不弹窗死锁，ping 恢复后补判）
const configChecked = ref(false)
// 未完成项目列表是否成功拉取过（M4 修复：启动竞态失败后 ping 恢复时补拉）
const projectsLoaded = ref(false)

// —— 后端连接状态：O1 逻辑，/api/config 定时 ping ——
const backendStatus = ref('checking')           // checking(检测中…) / ok(已连接) / fail(未连接)
let backendTimer = null
async function pingBackend() {
  try {
    await getConfig()
    backendStatus.value = 'ok'
    // 后端刚就绪：补上启动时因未就绪跳过的开屏判断（S2）+ 未完成项目列表（M4）
    if (!configChecked.value) {
      try {
        const cfg = await getConfig()
        config.value = { ...config.value, target_lang: cfg.target_lang || 'zh_cn' }
        if (cfg.configured) configured.value = true
        else showSetup.value = true
        configChecked.value = true
      } catch (e) { /* 仍不可用则下轮再试 */ }
    }
    if (!projectsLoaded.value) loadProjects()
  } catch (e) {
    backendStatus.value = 'fail'
  }
}
// 修复：拖放到 dropzone 之外（可识别区域外）会让浏览器默认打开/下载文件——
// 全局阻止拖放默认行为，只有 dropzone 内部 @drop 才处理文件
const preventDropDefault = (e) => e.preventDefault()

onMounted(async () => {
  pingBackend()
  backendTimer = setInterval(pingBackend, 30000)   // 每 30 秒复查
  window.addEventListener('dragover', preventDropDefault)
  window.addEventListener('drop', preventDropDefault)
  loadProjects()   // 断点续联：扫描临时文件直接显示未完成项目
  // 开屏判断：后端 config 已保存过（configured 标记）→ 直接主界面；否则首次开屏弹配置。
  // S2 修复：后端未就绪（getConfig 失败）时**不弹窗**——否则设置窗不可关+保存必败死锁；
  // 等 pingBackend 检测到后端 ok 后再补判（configChecked 标记）。
  try {
    const cfg = await getConfig()
    config.value = { ...config.value, target_lang: cfg.target_lang || 'zh_cn' }
    if (cfg.configured) configured.value = true
    else showSetup.value = true
    configChecked.value = true
  } catch (e) {
    configChecked.value = false      // 后端未就绪：标记待 ping 后重判，不弹窗
  }
})
onUnmounted(() => {
  clearInterval(backendTimer)
  window.removeEventListener('dragover', preventDropDefault)
  window.removeEventListener('drop', preventDropDefault)
})

// —— 多文件任务队列（左上传区 + 右流程区共享）——
// job: { path, name, kind, detectResult, status: pending|running|done|failed, taskId, error }
const jobs = ref([])
const viewedTaskId = ref('')      // 当前运行中任务，喂给右栏流程区轮询
// 修复（recheck）：手动查看锁定——用户点选历史任务查看时置 true，队列循环不再抢右栏
// （之前队列 A 一结束就把用户正在看的 B 顶回 A/空态）；重新「开始翻译」时重置 false 跟随队列。
const manualView = ref(false)
const processing = ref(false)      // 队列是否在处理中
const detectingCount = ref(0)      // 正在识别中的文件数（>0 时禁用添加）
// 未完成项目列表（断点续联，用户诉求）：启动扫描临时文件直接显示，不用拖入
const projects = ref([])
async function loadProjects() {
  try {
    projects.value = await listProjects()
    projectsLoaded.value = true
  } catch {
    projects.value = []
    projectsLoaded.value = false   // 失败：ping 恢复后补拉
  }
}
async function removeProject(pid) {
  // 修复（recheck）：删除失败时明确提示具体原因（之前静默吞掉，后端 500 时用户
  // 看到「✕ 叉不掉」却不知道是后端故障）；无论成败都重新拉取列表。
  try {
    await deleteProject(pid)
  } catch (e) {
    window.alert(`删除失败：${e.message}`)
  }
  await loadProjects()
}
// 未完成项目「续联」：把该项目原始路径入队（detect→续联），自动开始翻译。
// 后端 _check_resume / run() 按内容指纹匹配项目记忆/进度，命中跳过已翻条目。
async function resumeProject(p) {
  if (!p || !p.path) return
  // S1 修复：有同路径任务正在翻译 → 不移除（防失去对运行中任务的控制/浪费额度），明确提示
  if (jobs.value.some(j => j.path === p.path && j.status === 'running')) {
    window.alert('该文件正在翻译中，请先等待完成或取消当前任务再续联')
    return
  }
  // 续联前移除同路径的非运行中旧任务（用户从任务行「可断点续联」点击触发时，
  // 该任务还停在队列里，不移除会重复翻译同一文件）
  jobs.value = jobs.value.filter(j => j.path !== p.path)
  try {
    await addPath(p.path, p.name)
    // detect 完成后自动启动队列（无需再点「开始翻译」）
    if (!processing.value) startQueue()
  } catch (e) { /* addPath 内部已处理失败 */ }
}

// 已完成/失败统计由右栏 ProgressView 内部自算，这里不再重复声明（结构优化）

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
    let es = null
    let t = null
    let pollInflight = false     // 修复（recheck）：防重入——上次轮询未返回时跳过本轮
    const cleanup = () => { if (es) es.close(); if (t) clearInterval(t) }
    // 双线进行（修复：SSE 在 pywebview/WebView2 环境可能静默失效 → 队列等终态卡死）：
    // 1s 轮询兜底保证终态一定能等到；SSE 收到终态即时 resolve（联动更快，切换零延迟）。
    // 移除 10 分钟硬超时（修复：整合包翻译轻松超过 10 分钟，超时误判失败——后端仍在跑、
    // 前端却标 failed，且会让队列提前启动下一个任务造成并发）。终态由轮询 + SSE 双保障，
    // 仅后端连续 3 次读取失败（服务不可达）才放弃。
    t = setInterval(async () => {
      if (pollInflight) return   // 修复（recheck）：后端挂起时不再每秒堆积新请求
      pollInflight = true
      try {
        const st = await getTask(taskId)
        if (['done', 'failed', 'cancelled'].includes(st.status)) { cleanup(); resolve(st); return }
      } catch (e) {
        // 修复：读失败不 resolve(null)——否则队列会提前启动下一个任务，后端 A 可能还在跑，
        // 双任务并发双倍耗 API 额度。后端短暂不可达时继续轮询，恢复后读到终态再 resolve；
        // 后端永久不可达时任务保持 running，用户可手动取消（不造成并发）。
      } finally {
        pollInflight = false
      }
    }, 1000)
    if (typeof EventSource !== 'undefined') {
      es = new EventSource(taskStreamUrl(taskId))
      es.addEventListener('state', (ev) => {
        let st
        try { st = JSON.parse(ev.data) } catch { return }
        if (['done', 'failed', 'cancelled'].includes(st.status)) { cleanup(); resolve(st) }
      })
      es.onerror = () => {
        // 连接关闭：清引用（轮询继续兜底）；断线重连中由 EventSource 自动处理
        if (es.readyState === EventSource.CLOSED) es = null
      }
    }
  })
}
async function startQueue() {
  if (processing.value) return
  processing.value = true
  // 修复（recheck）：用户手动查看过历史任务 → 重置跟随队列（重新开始翻译让右栏跟随队列）
  manualView.value = false
  // 不用快照：每次循环重新找 pending，翻译中新增拖入的任务也会被处理
  while (true) {
    const job = jobs.value.find(j => j.status === 'pending')
    if (!job || !processing.value) break
    job.status = 'running'
    job.error = ''
    // 切换新任务前先清空右栏（不残留上一任务终态，明确「准备下一个」），
    // autoTranslate 返回后再喂新 task_id 显示新任务流程。
    // 修复（recheck）：用户手动查看历史任务（manualView=true）时不抢右栏
    if (!manualView.value) viewedTaskId.value = ''
    try {
      const r = await autoTranslate({ path: job.path, target_lang: config.value.target_lang })
      job.taskId = r.task_id
      if (!manualView.value) viewedTaskId.value = r.task_id   // 喂给右栏显示当前任务进度
      const final = await waitTaskDone(r.task_id)
      if (final && final.status === 'done') {
        job.status = 'done'
        // 修复（recheck）：断点续联**只在打开应用时检测**（onMounted loadProjects），
        // 任务完成**不**动态刷新续联列表。之前 done 后 loadProjects 与后端 finally 写满
        // progress 存在时序竞态（读到时还是运行中快照 done<total）→ 成功项目残留显示
        // 100%(28421/29081) 可续联。成功项目写满后启动检测自然不显示；失败/中断项目
        // 重启后由启动扫描列出续联。
        // 修复：nextTick hack 在队列有下一任务时竞态（微任务回调覆盖为旧 task_id，右栏闪回）。
        // 改为直接判断：还有 pending → 保持清空（循环下一步启动下一个，显示「正在启动」）；
        // 无 pending → 停留显示完成态（产物呈递）
        const hasNext = jobs.value.some(j => j.status === 'pending')
        if (!manualView.value) viewedTaskId.value = hasNext ? '' : r.task_id
      } else {
        job.status = 'failed'
        job.error = final ? (final.status === 'cancelled' ? '已取消' : '翻译失败') : '读取任务状态失败'
      }
      // 修复（recheck #3）：手动查看的任务已终态（done/failed/cancelled）→ 恢复跟随队列，
      // 右栏切回当前任务进度（不再永久锁定历史任务）
      if (manualView.value) {
        const _viewed = jobs.value.find(j => j.taskId === viewedTaskId.value)
        if (!_viewed || ['done', 'failed', 'cancelled'].includes(_viewed.status)) {
          manualView.value = false
        }
      }
    } catch (e) {
      job.status = 'failed'
      job.error = `启动翻译失败：${e.message}`
    }
  }
  processing.value = false
  // 修复：不再无条件清空 viewedTaskId——否则会覆盖上面「最后一个任务完成/失败」的终态
  // 停留展示（产物呈递）。停留与否由 144 行决定：有 pending 清空、无 pending 保留终态。
}

// —— 任务列表操作 ——
function removeJob(i) {
  const job = jobs.value[i]
  if (!job) return
  if (job.status === 'running') return   // 运行中不可移除（防后台任务失控）
  // 修复（recheck #2）：队列处理中也允许移除 pending/failed——用户可停止不需要的后续任务，
  // 防额度浪费；队列循环 find(pending) 会跳过已移除的
  jobs.value.splice(i, 1)
  if (viewedTaskId.value && job.taskId === viewedTaskId.value) viewedTaskId.value = ''
}
function clearJobs() {
  // 修复（recheck #2）：识别中禁止（防清空后 detect 完成又入队）；处理中允许清空
  // **待翻/失败任务**（running 保留）——用户可停止队列后续任务防额度浪费，running 完成后队列自然结束
  if (detectingCount.value > 0) return
  if (processing.value) {
    jobs.value = jobs.value.filter(j => j.status === 'running')
  } else {
    jobs.value = []
  }
  viewedTaskId.value = ''
  manualView.value = false
}

// 点击左侧任务 → 右侧查看该任务汉化明细（历史回看，找汉化纰漏）。
// 修复：反复点击同一任务行，viewedTaskId 不变 → ProgressView watch 不触发，
// 无法重新拉取终态呈递产物。改为「先清空再设」（nextTick 分隔强制触发 watch 两次），
// 保证每次点击都强制刷新右栏拉到最新状态。
function selectJob(taskId) {
  if (!taskId) return
  // 修复（recheck）：标记手动查看——队列循环不再抢右栏（用户点开历史任务 B，队列 A 结束不顶掉）
  manualView.value = true
  viewedTaskId.value = ''
  nextTick(() => { viewedTaskId.value = taskId })
}

// —— 配置弹窗：保存由后端 config.json 持久（configured 标记），不再依赖 localStorage；设置按钮可随时重开 ——
function onConfigSaved(cfg) {
  const prevCache = config.value.cache_dir
  config.value = { ...config.value, ...cfg }
  // 修复（用户诉求）：更换缓存目录后重新检测未完成项目——后端 _switch_work_dir
  // 已把 progress/memory 迁移到新目录，前端需重新扫描才能显示可续联项目。
  // 目录变化（含清空回默认）都刷新
  if (cfg.cache_dir !== prevCache) loadProjects()
}
function closeSetup() {
  showSetup.value = false
  configured.value = true
}
function openSetup() { showSetup.value = true }
</script>

<template>
  <div class="app">
    <header class="topbar">
      <div class="brand">
        <span class="brand-mark"><img src="/icon.svg" alt="像素译站" /></span>
        <div>
          <h1>像素译站</h1>
          <p>每个方块，都有中文</p>
        </div>
      </div>
      <span class="spacer"></span>
      <div class="conn" :class="backendStatus">
        <span class="dot"></span>
        <span>{{ backendStatus === 'ok' ? '已连接后端' : backendStatus === 'fail' ? '未连接后端' : '检测中…' }}</span>
      </div>
      <button class="btn" @click="openSetup">⚙ 设置</button>
    </header>

    <main class="workbench" :class="{ 'has-task': !!viewedTaskId }">
      <section class="batch-panel">
        <ScanView class="panel"
                  :jobs="jobs" :processing="processing" :detecting="detectingCount > 0"
                  :add-path="addPath" :add-upload="addUpload" :projects="projects"
                  :viewed-task-id="viewedTaskId" @select-job="selectJob"
                  @remove-job="removeJob" @clear-jobs="clearJobs" @start-queue="startQueue"
                  @delete-project="removeProject" @resume-project="resumeProject" />
      </section>
      <section class="task-panel">
        <ProgressView class="panel" :task-id="viewedTaskId" :jobs="jobs" />
      </section>
    </main>

    <!-- 设置弹窗：首次开屏（强制配置）或 ⚙设置 随时打开 -->
    <div v-if="showSetup" class="overlay">
      <div class="dialog">
        <SetupView :on-done="onConfigSaved" :on-close="closeSetup" :closable="configured"
                   :on-cache-cleared="loadProjects" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.app { display: flex; flex-direction: column; height: 100%; }

.topbar {
  display: flex; align-items: center; gap: 14px;
  padding: 0 26px; height: 74px; flex-shrink: 0;
  border-bottom: 1px solid var(--line);
}
.brand { display: flex; align-items: center; gap: 13px; }
.brand-mark {
  width: 44px; height: 44px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
}
.brand-mark img { width: 100%; height: 100%; display: block; object-fit: contain; }
.brand h1 { margin: 0; font-size: 22px; letter-spacing: .12em; color: var(--moss); }
.brand p { margin: 3px 0 0; color: var(--text-dim); font-size: 12px; letter-spacing: .06em; }
.spacer { flex: 1; }

/* 后端连接状态指示：绿=已连接 / 红=未连接 / 灰=检测中 */
.conn { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--text-dim); }
.conn .dot { width: 8px; height: 8px; border-radius: 0; background: var(--text-dim); flex-shrink: 0; }
.conn.ok { color: var(--moss); }
.conn.ok .dot { background: var(--moss); }
.conn.fail { color: var(--rust); }
.conn.fail .dot { background: var(--rust); }

.workbench { flex: 1; display: flex; min-height: 0; padding: 0; }
.batch-panel {
  width: 46%; min-width: 0; overflow: auto; padding: 24px 26px;
  border-right: 1px solid var(--line); background: var(--bg-2);
  transition: width .36s var(--ease);
}
.task-panel { flex: 1; min-width: 0; overflow: auto; padding: 24px 28px; background: var(--bg); }
.workbench.has-task .batch-panel { width: 36%; }
.workbench.has-task .task-panel { width: 64%; }

/* —— 配置弹窗遮罩 —— */
.overlay {
  position: fixed; inset: 0; z-index: 50;
  background: rgba(6, 10, 13, .62);
  display: flex; align-items: center; justify-content: center;
}
.dialog {
  background: var(--bg-2); border: 1px solid var(--line);
  border-radius: 0; padding: 24px 26px;
  width: 540px; max-width: 92vw; max-height: 88vh; overflow: auto;
  box-shadow: 0 14px 44px rgba(0, 0, 0, .45);
}
</style>
