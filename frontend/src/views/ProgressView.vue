<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { cancelTask, getReport, getTask, openOutput, pauseTask, taskStreamUrl } from '../api'

// props：taskId（App 持有的当前运行任务）+ jobs（共享任务队列，已完成汇总从这里取）
const props = defineProps({
  taskId: { type: String, default: '' },
  jobs: { type: Array, default: () => [] },
})

const task = ref(null)
const error = ref('')
let timer = null

// 运行计时器：显示任务已运行时长（进度不动时让用户知道仍在处理）
const elapsed = ref('')
let elapsedTimer = null
function updateElapsed() {
  const t = task.value
  if (!t || !t.created_at) { elapsed.value = ''; return }
  const sec = Math.max(0, Math.floor(Date.now() / 1000 - t.created_at))
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60
  elapsed.value = h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`
}
function startElapsedTimer() {
  stopElapsedTimer()
  updateElapsed()
  elapsedTimer = setInterval(updateElapsed, 1000)
}
function stopElapsedTimer() {
  if (elapsedTimer) { clearInterval(elapsedTimer); elapsedTimer = null }
}

const STATUS_TEXT = {
  pending: '等待中', running: '翻译中', paused: '已暂停',
  done: '已完成', failed: '失败', cancelled: '已取消',
}
const STATUS_CLS = { done: 'ok', failed: 'bad', cancelled: 'bad', error: 'bad', running: 'ok', paused: 'warn' }

const isActive = computed(() =>
  !!task.value && !task.value.cancelled && ['pending', 'running', 'paused'].includes(task.value.status))
const percent = computed(() => {
  const t = task.value
  if (!t) return 0
  // 修复：任务完成（产物已生成）→ 进度直接 100%。此前 build 对账 total=done+failed，
  // 只要 failed>0 进度就停在 <100%（用户实测：产物已输出但大进度条不满）。
  // done/total 保持真实计数（不虚增，维持 done+failed<=total 不变量），done 态强显 100%。
  if (t.status === 'done') return 100
  if (!t.total) return 0
  // done / max(done, total) 防回退：后端 build 阶段才把产物单位计入 total，
  // 直接 done/total 会在 total 膨胀时进度「往回走」（用户实测观感像 bug）
  return Math.round((t.done / Math.max(t.done, t.total)) * 100)
})
// 当前阶段中文名（无 stage/兼容旧任务回退空——旧任务总览仍用纯百分比）
const STAGE_TEXT = {
  lang: '翻译语言文件', json: '翻译 JSON / 文本', pack: '翻译整合包文本',
  hardcode: 'AI 判断硬编码', build: '打包产物',
}
const stageText = computed(() => {
  if (!task.value || !task.value.stage) return ''
  return STAGE_TEXT[task.value.stage] || task.value.stage
})
// 当前阶段计数（stages 明细匹配当前 stage；后端未填 stages 时回退 null）
const currentStage = computed(() => {
  const t = task.value
  if (!t || !t.stage) return null
  return (t.stages || []).find(s => s.name === t.stage) || null
})
// 合并状态栏阶段计数：当前阶段 done/total（每个阶段都有确切数据流在动，用户诉求——
// 语言文件 3,456/37,025、硬编码 120/1,800 这种随进度跳；无阶段明细（解压/扫描）时回退全局）
const stageCount = computed(() => {
  const t = task.value
  if (!t || !isActive.value) return ''
  const cs = currentStage.value
  if (cs && cs.total > 0) return `${num(cs.done)}/${num(cs.total)}`
  if (t.total > 0) return `${num(t.done)}/${num(t.total)}`
  return ''
})
// 从 progress 末尾倒序找第一个匹配条目（修复：filter 全量遍历 O(n)×3 每次推送都跑，
// 几千条 progress 时浪费——倒序 find 遇到即返回）
function lastProgress(pred) {
  const arr = task.value?.progress || []
  for (let i = arr.length - 1; i >= 0; i--) {
    if (pred(arr[i])) return arr[i]
  }
  return null
}
// 当前正在执行的动作（最新 translating 条目的 note：审查产物/下载词库/AI 判断硬编码…），
// 状态条直观显示「当前在做啥」（用户反馈状态条不显示审查/下载进度）
const currentAction = computed(() => lastProgress(p => p.status === 'translating')?.note || '')
// 模式步骤（整合包 / mod / 地图 / 光影 各自独立流程——用户诉求：整合包与单 mod 流程不能统一）。
// 整合包独有：解压/扫描整合包（分析输入）、任务线/config/kubejs 目录文本、补丁包产物；
// 单 mod 独有：jar 内配置/文本、汉化 jar 产物。
const MODE_STEPS = {
  modpack: [
    { key: 'read', label: '分析输入' },                // 解压 + 扫描 355 个 mod
    { key: 'lang', label: '翻译语言文件' },            // mods/*.jar 语言文件 + jar 内文本
    { key: 'pack', label: '翻译任务线/配置/kubejs' },  // ftbquests 任务书 + config/kubejs
    { key: 'hardcode', label: 'AI 判断硬编码' },
    { key: 'save', label: '打包补丁包' },              // 资源包 + 汉化补丁包
  ],
  modjar: [
    { key: 'read', label: '分析输入' },
    { key: 'lang', label: '翻译语言文件' },
    { key: 'json', label: '翻译配置/文本' },           // jar 内 json/lines
    { key: 'hardcode', label: 'AI 判断硬编码' },
    { key: 'save', label: '打包汉化 jar' },
  ],
  map: [
    { key: 'read', label: '扫描存档' },
    { key: 'translate', label: '翻译文本' },
    { key: 'save', label: '写回存档' },
  ],
  shader: [
    { key: 'read', label: '扫描光影' },
    { key: 'translate', label: '翻译文本' },
    { key: 'save', label: '打包光影' },
  ],
}
// 当前任务模式：从任务队列找 taskId 对应的 job 拿 kind（detect 返回）；无则默认 mod
const currentKind = computed(() => {
  const job = props.jobs.find(j => j.taskId === props.taskId)
  return job?.kind || 'modjar'
})
// —— 标题区：进入翻译流程后顶替「翻译流程 / 队列自动逐个翻译」为当前文件名 ——
// 翻译中显示原名（Project Infinity 0.1），完成后显示翻译名（无限计划 0.1）+ 原英文淡化。
// 是 mod 显示 mod 名、整合包显示整合包名、地图显示地图名（用户诉求）。
const KIND_TEXT = { modpack: '整合包', modjar: '单个模组', map: '地图存档', shader: '光影包' }
const currentJob = computed(() => props.jobs.find(j => j.taskId === props.taskId) || null)
const jobName = computed(() => currentJob.value?.name || '')
const nameTranslated = computed(() => task.value?.display_name_translated || '')
// 完成态且翻译名可用且 != 原名 → 标题显示中文名、hint 淡化原英文（AI 保留原文时回退原名）
const showTranslatedName = computed(() =>
  task.value?.status === 'done' && nameTranslated.value && nameTranslated.value !== jobName.value)
// 标题副行：运行中「正在翻译」，终态用状态文案（已完成/失败/已取消）
const statusHint = computed(() => {
  const st = task.value?.status
  if (!st || st === 'running' || st === 'pending') return '正在翻译'
  return STATUS_TEXT[st] || st
})
const stageSteps = computed(() => MODE_STEPS[currentKind.value] || MODE_STEPS.modjar)
// 阶段 rail 当前步：按 kind 精确映射 stage → 步骤下标（整合包与单 mod 流程分开，不统一）。
// modpack：lang/json→「翻译语言文件」、pack→「任务线/配置/kubejs」、hardcode→3、build→4；
// modjar：lang→「翻译语言文件」、json→「配置/文本」、hardcode→3、build→4；
// map/shader：无细分 stage，翻译阶段=1。
const currentStep = computed(() => {
  const st = task.value?.stage
  const steps = stageSteps.value
  if (task.value?.status === 'done') return steps.length - 1
  if (currentKind.value === 'modpack') return ({ lang: 1, json: 1, pack: 2, hardcode: 3, build: 4 })[st] ?? 0
  if (currentKind.value === 'modjar') return ({ lang: 1, json: 2, hardcode: 3, build: 4 })[st] ?? 0
  // map / shader：无 lang/json/pack 细分，翻译阶段=1（stage 空按 done 推断）
  if (!st) return task.value?.done > 0 ? 1 : 0
  return 1
})
// 失败时：取 progress 里最后一条 error 信息
const failInfo = computed(() => lastProgress(p => p.status === 'error')?.error || '')
// 取 progress 里最后一条 warn 信息（任务非失败时显示黄色提示）
const warnInfo = computed(() => lastProgress(p => p.status === 'warn')?.error || '')
// 最新在前展示明细
// 最新在前展示明细；只渲染最近 100 条（修复：整合包几千条 progress 全量渲染 → 前端卡顿
// 阻塞 SSE/轮询事件 → 「翻译中右栏不实时」）。未翻译明细（untranslated）在终态置顶不受影响。
// 修复：未翻译汇总（untranslated）不再在明细里显示——大整合包失败几百条放不下，
// 具体原因见「翻译报告」弹窗（任务完成后点「阅读翻译报告」查看全部失败明细）
const rows = computed(() => (task.value?.progress || [])
  .filter(p => !p.untranslated)
  .slice(-100).reverse())
// 文件归属短化：jar 内路径 assets/jei/models/foo.json → models/foo.json；config/foo.json 保留两段
function shortFile(p) {
  const parts = String(p || '').split('/')
  return parts.slice(-2).join('/')
}
// 明细行归属标签：[mod] 或 [config/foo.json] 或 [mod · file] —— 整合包右侧要能看到
// 「在翻译哪个 mod / 哪个配置文件」，否则几千条 key 全无归属，看起来就像笼统的「翻译 config/翻译 mod」
function sourceTag(r) {
  const parts = []
  if (r.mod) parts.push(r.mod)
  if (r.file) parts.push(shortFile(r.file))
  return parts.join(' · ')
}

// —— 已完成汇总：jobs 中 done/failed 的列表 ——
const doneJobs = computed(() => props.jobs.filter(j => j.status === 'done' || j.status === 'failed'))
// 启动中任务：running 但还没有 taskId（App 点击「开始翻译」后、autoTranslate 返回 task_id 前）。
// 修复：这个间隙右栏空态（「添加你想翻译的模组」）让用户以为没反应——应显示「正在启动」，
// 点击开始翻译的瞬间右栏就要有变化（转圈 + 任务名），而不是卡在初始引导页等 HTTP 返回。
const startingJob = computed(() => props.jobs.find(j => j.status === 'running' && !j.taskId) || null)
const empty = computed(() => !props.taskId && !startingJob.value && doneJobs.value.length === 0)

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
  timer = setInterval(refresh, 1000)   // 兜底轮询（SSE 不可用/降级时）
}
function stopPolling() {
  if (timer) { clearInterval(timer); timer = null }
}

// 双线进行（修复：SSE 在 pywebview/WebView2 环境可能静默失效 → 右栏卡住不更新，
// 只有停止时 refresh 拉全量才涌出信息）：
//   - 1s 轮询默认兜底：保证进度一定更新（任何环境都不卡）
//   - SSE 实时推送：状态变更即时到达，更快速更精细（token 统计/中间状态）
// 两者并行写 task.value（都是同一任务最新状态），SSE 通常更快到达 → 显示更实时。
let es = null
const sseSupported = typeof EventSource !== 'undefined'
function startStreaming(taskId) {
  stopStreaming()
  if (!taskId) return
  startPolling()                 // 1s 轮询兜底（始终在跑）
  if (!sseSupported) return      // 无 SSE：仅轮询
  es = new EventSource(taskStreamUrl(taskId))
  es.addEventListener('state', (ev) => {
    try { task.value = JSON.parse(ev.data) } catch { return }
    error.value = ''
    if (task.value && ['done', 'failed', 'cancelled'].includes(task.value.status)) stopStreaming()
  })
  es.onerror = () => {
    // 断线自动重连（EventSource 自带）；终态后主动关闭防无限重连
    if (task.value && ['done', 'failed', 'cancelled'].includes(task.value.status)) stopStreaming()
  }
}
function stopStreaming() {
  if (es) { es.close(); es = null }
  stopPolling()
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
    // 后端置 cancelled 但 status 要等 pipeline 轮询才变，前端先即时标记「取消中」
    if (task.value) task.value.cancelled = true
    refresh()
  } catch (e) { error.value = e.message }
}
// 打开产物文件夹（用户诉求：完成态直接看产物，不选地方下载）——后端 os.startfile 弹资源管理器。
// 参数 id 可选：当前任务省略走 props.taskId；已完成汇总传各自 job.taskId 打开对应产物文件夹。
// 修复（recheck）：模板调用必须显式传空——`@click="doOpenOutput"` 会把 MouseEvent 当 id 传入，
// openOutput 拿到 [object MouseEvent] 拼 URL 必失败（完成态主按钮静默失效，用户实测）。
async function doOpenOutput(id) {
  try {
    await openOutput(id || props.taskId)
  } catch (e) {
    // 修复（recheck）：空 catch 吞错，非桌面环境点按钮毫无反馈——改为提示真实失败原因
    error.value = `打开产物文件夹失败：${e.message}`
  }
}

// —— 空态三折叠（实装）：动态效果区常态显示 + 使用说明/免责声明手风琴（同时只开一个）——
const openFold = ref('')   // '' 全收起 / 'usage' 使用说明 / 'disclaimer' 免责声明
function toggleFold(name, ev) {
  const wasOpen = openFold.value === name
  // 同步捕获当前折叠项（ev.currentTarget 在事件回调返回后会被浏览器置为 null，
  // 绝不能拖到 nextTick 异步回调里再访问——之前"只到中段/不滚"就是它害的）
  const headEl = ev?.currentTarget
  const foldEl = headEl && headEl.closest ? headEl.closest('.fold') : null
  openFold.value = wasOpen ? '' : name
  nextTick(() => {
    // 真实应用是顶层窗口（无父页面）：scrollIntoView 平滑滚到折叠项头部。
    // 宣传页 iframe 的滚动隔离由宣传页自己的脚本覆写 scrollIntoView 实现，
    // 本文件只保留真实应用的逻辑，不掺杂宣传页专用代码。
    const target = wasOpen ? flowPanelRef.value : (foldEl || flowPanelRef.value)
    target?.scrollIntoView({ block: 'start', behavior: 'smooth' })
  })
}

// —— 翻译报告弹窗（通用所有模式：整合包/mod/地图/光影）——
// 任务完成后点「阅读翻译报告」→ 拉 report.json（含全部未翻译条目）→ 弹窗阅读，不下载。
const reportData = ref(null)
const reportError = ref('')
const num = (n) => Number(n || 0).toLocaleString()
function fmtReportTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const p = (x) => String(x).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}
async function openReport() {
  reportError.value = ''
  try {
    reportData.value = await getReport(props.taskId)
  } catch (e) {
    reportError.value = `报告读取失败：${e.message}`
    reportData.value = { overview: { total: 0, translated: 0, failed: 0, coverage: 0 },
                         stages: [], products: [], failures: [], notes: [] }
  }
}
function closeReport() { reportData.value = null; reportError.value = '' }

// 运行计时器控制：任务运行中每秒走；终态/切空停表（显示最终时长）
watch(() => task.value?.status, (st) => {
  if (st && ['done', 'failed', 'cancelled'].includes(st)) stopElapsedTimer()
  else if (st) startElapsedTimer()
})
// 审查状态灯：红灯「静默审查中…」审查管道活跃时亮；绿灯「审查完成」只在审查结束
// 短暂显示后消失（用户诉求：审查完成不是常态存在）。任务结束（非运行）灯整体隐藏。
const reviewVisible = ref(false)
let hadReview = false
let reviewTimer = null
watch(() => task.value?.reviewing, (v) => {
  clearTimeout(reviewTimer)
  if (!task.value) return
  if (v) {
    hadReview = true          // 确有审查经历（区分「未开始审查」与「审查完成」）
    reviewVisible.value = true
  } else if (hadReview) {
    reviewVisible.value = true   // 审查完成：绿灯短暂显示后消失
    reviewTimer = setTimeout(() => { reviewVisible.value = false }, 3000)
  }
})
// 队列逐个切换任务：taskId 变化时重置旧数据并重启轮询，否则停留在旧任务状态
watch(() => props.taskId, (newId) => {
  if (newId) {
    task.value = null
    elapsed.value = ''
    stopElapsedTimer()
    error.value = ''
    reviewVisible.value = false
    hadReview = false
    clearTimeout(reviewTimer)
    refresh()                 // 连接即拉一次快照（SSE 端点也会发首帧，双保险）
    startStreaming(newId)     // 用 SSE 实时推送
  } else {
    stopStreaming()
    stopElapsedTimer()
    error.value = ''          // 切到空任务时清残留错误提示
  }
})

// —— 空态等比缩放（修复：固定字号导致 hero 状态行塞不下成两排）——
// 以 600px 设计宽度为基准，实际面板更窄时整体 transform scale 缩小，
// hero/进度条/折叠区全部件同步等比缩放，绝不换行（transform-origin 左上）。
const EMPTY_DESIGN_W = 600
const emptyScale = ref(1)
const flowPanelRef = ref(null)
let emptyObserver = null
function measureEmptyScale() {
  const w = flowPanelRef.value?.clientWidth || 0
  emptyScale.value = w > 0 ? Math.min(1, w / EMPTY_DESIGN_W) : 1
}

onMounted(() => {
  refresh()
  startStreaming(props.taskId)
  // 空态缩放：监听面板宽度变化实时重算 scale（窗口缩放/分栏拖动都跟随）
  if (typeof ResizeObserver !== 'undefined') {
    emptyObserver = new ResizeObserver(measureEmptyScale)
    if (flowPanelRef.value) emptyObserver.observe(flowPanelRef.value)
    measureEmptyScale()
  }
})
onUnmounted(() => {
  stopStreaming(); stopElapsedTimer(); clearTimeout(reviewTimer)
  if (emptyObserver) { emptyObserver.disconnect(); emptyObserver = null }
})
</script>

<template>
  <section ref="flowPanel" class="panel flow-panel">
    <!-- 标题区：进入翻译流程后，「翻译流程 / 队列自动逐个翻译」被当前文件名顶替——
         翻译中显示原名，完成后显示中文名 + 原英文淡化（用户诉求）。
         修复：空态（三折叠）时不显示此标题区，避免「翻译流程 / 队列自动逐个翻译」残留 -->
    <h2 v-if="!empty">
      <template v-if="taskId">{{ showTranslatedName ? nameTranslated : (jobName || '翻译流程') }}</template>
      <template v-else>翻译流程</template>
    </h2>
    <p class="hint" v-if="!empty">
      <template v-if="taskId">
        <span v-if="showTranslatedName" class="hint-dim">原文件名 {{ jobName }}</span>
        <span v-else>{{ KIND_TEXT[currentKind] || '任务' }} · {{ statusHint }}</span>
      </template>
      <template v-else>队列自动逐个翻译，任务运行中每秒刷新</template>
    </p>

    <!-- 空态等比缩放：以 600px 设计宽度为基准，面板更窄时整体 transform scale 缩小，
         hero/进度条/折叠区全部件同步等比缩放，绝不换行（修复：固定字号塞不下成两排） -->
    <div v-if="empty" class="task-empty" :style="{ '--empty-scale': emptyScale }">
      <!-- 动态效果区（常态显示）：深绿渐变 hero + 扫描光效 + 像素方块矩阵 -->
      <div class="hero">
        <div class="hero-badge">■ 已就绪 · 等待输入 <b>■</b></div>
        <div class="hero-head">
          <div class="pix-grid" aria-hidden="true">
            <span class="pix"></span><span class="pix"></span><span class="pix"></span><span class="pix"></span>
            <span class="pix"></span><span class="pix"></span><span class="pix"></span><span class="pix"></span>
            <span class="pix"></span><span class="pix"></span><span class="pix"></span><span class="pix"></span>
            <span class="pix"></span><span class="pix"></span><span class="pix"></span><span class="pix"></span>
          </div>
          <div class="hero-title">
            <h1>像素译站</h1>
            <p>每个方块，都有<b>中文</b></p>
          </div>
        </div>
        <div class="hero-status">
          <span class="cube"></span>
          <span>拖入整合包 / Mod / 地图 / 光影，开始汉化之旅</span>
          <span class="steps">识别 → 翻译 → 审查 → 产出</span>
        </div>
      </div>

      <!-- 使用说明（折叠） -->
      <div class="fold" :class="{ open: openFold === 'usage' }">
        <div class="fold-head" @click="toggleFold('usage', $event)">
          <span class="mark" :class="{ on: openFold === 'usage' }"></span>
          <span class="t">使用说明 · 全流程</span>
          <span class="arrow">▼</span>
        </div>
        <div class="fold-body" v-show="openFold === 'usage'">
          <h4>① 首次进入：配置翻译引擎</h4>
          <ol>
            <li>首次启动弹出「<b>设置</b>」窗口，之后点右上角「⚙ 设置」可随时重新打开</li>
            <li>选择翻译引擎：智谱 AI、DeepSeek、通义、Kimi、Ollama 或免费直连</li>
            <li>填写接口地址、模型名、API Key，Key 只保存在本机</li>
            <li>点击「<b>测试连接</b>」确认可用</li>
            <li>选择目标语言，点击保存</li>
          </ol>
          <h4>② 拖入文件开始翻译</h4>
          <ol>
            <li>将整合包文件夹、压缩包、Mod jar、地图或光影包拖入左侧虚线区，或点击按钮选择</li>
            <li>应用自动识别文件类型，支持 zip 嵌套结构</li>
            <li>社区人工词库已内置，翻译时自动优先命中，无需任何准备</li>
            <li>点击「<b>开始翻译</b>」，队列按顺序逐个处理</li>
            <li>已自带中文 / 对应语言的 Mod 会<b>自动识别并跳过</b>，不重复翻译、不浪费 AI 调用</li>
          </ol>
          <h4>③ 翻译进行中</h4>
          <ol>
            <li>右侧显示阶段流程：语言文件、JSON 与任务书、脚本、硬编码判断、打包产物</li>
            <li>可随时暂停或取消</li>
            <li>翻译完成后自动进行 AI 质量审查，专有名词统一译名</li>
            <li>整合包任务书与脚本内的文本同样翻译，长段描述不漏翻</li>
            <li>点击「<b>阅读翻译报告</b>」查看覆盖率与未翻译条目及原因</li>
          </ol>
          <h4>④ 查看产物</h4>
          <ol>
            <li>完成后点击「<b>📂 打开产物文件夹</b>」</li>
            <li>整合包：产物为「整合包汉化.zip」，解压后拷入整合包根目录即用，内含汉化资源包、i18n 汉化模组、Vault Patcher 硬编码补丁、任务书与配置补丁、使用说明</li>
            <li>单个 Mod：汉化 jar</li>
            <li>地图：汉化存档</li>
            <li>光影：汉化光影包</li>
            <li>产物自动按整合包 MC 版本写入兼容的资源包格式；材质包描述显示覆盖率</li>
            <li>中断过的项目：启动时自动检测，左侧「断点续联」列表点击即可继续，已翻译内容不重复</li>
          </ol>
          <h4>⑤ 缓存与项目管理</h4>
          <ol>
            <li>断点续联项目点「✕」删除该项目缓存，翻译记忆一并清除</li>
            <li>设置中可更换缓存目录，保存后即时生效</li>
            <li>设置中可清除缓存释放空间</li>
          </ol>
        </div>
      </div>

      <!-- 免责声明（折叠） -->
      <div class="fold" :class="{ open: openFold === 'disclaimer' }">
        <div class="fold-head" @click="toggleFold('disclaimer', $event)">
          <span class="mark" :class="{ on: openFold === 'disclaimer' }"></span>
          <span class="t">免责声明</span>
          <span class="arrow">▼</span>
        </div>
        <div class="fold-body" v-show="openFold === 'disclaimer'">
          <div class="disclaimer">
            <div class="head"><i></i>请务必阅读 · 使用即视为同意以下全部条款</div>
            <span class="sub">像素译站（Pixel Translation Station，以下简称「本软件」）为<b>个人开发、免费开源</b>的 Minecraft 汉化辅助工具，仅供学习交流使用。使用本软件前，请完整阅读并理解本免责声明；一旦使用本软件（包括下载、安装、运行、调用其任何功能），即视为你已阅读、理解并<b>无条件同意</b>本声明全部内容。</span>
          </div>
          <h4>一、性质与使用范围</h4>
          <ul>
            <li>本软件以「<b>仅限学习交流</b>」为目的免费发布，不构成任何形式的商业产品、服务或承诺；你不得将其用于任何商业目的、牟利行为或商业分发。</li>
            <li>本软件为开源项目，任何人可在遵循开源许可的前提下学习、研究其实现；但<b>不得</b>以本软件名义进行任何形式的二次销售、捆绑收费或变相收费。</li>
            <li>你需具备对 Minecraft 及其 Mod 生态的基本认识，并<b>自行判断</b>使用本软件是否违反你所使用平台（官方启动器、各类整合包平台、Mod 发布平台等）的规则与协议。</li>
          </ul>
          <h4>二、翻译内容与版权</h4>
          <ul>
            <li>本软件生成的翻译文本，其<b>原文版权</b>归原 Mod / 整合包 / 地图 / 光影的开发者与发行方所有；翻译内容仅作学习交流使用，<b>不得</b>擅自用于商业分发、盈利传播或冒认原创。</li>
            <li>本软件内置或下载的社区词库（如 CFPA 汉化包）、第三方 Mod（如 I18nUpdateMod、Vault Patcher）等资源，版权归其各自作者；本软件仅作离线集成与分发便利，不主张上述资源任何权利。</li>
            <li>汉化产物仅供个人游玩、学习交流，请勿在未经原作者许可的情况下对外公开发布、转售或作商用。</li>
          </ul>
          <h4>三、翻译质量与准确性</h4>
          <ul>
            <li>本软件依赖 AI 大模型与自动词库进行翻译，<b>不保证</b>翻译的准确性、完整性、一致性与最终效果；译文可能存在错译、漏译、机翻腔、专有名词不统一等问题。</li>
            <li>翻译结果可能包含不符合目标语言习惯或与上下文不符的表达，请在使用前<b>自行核对</b>关键内容。</li>
            <li>本软件会依据目标语言字符自动判定「已汉化内容」并跳过，该判定基于字符识别，对混排文本可能存在个别误判，请以实际产物为准；你仍可对对应项目删除缓存后重新翻译。</li>
            <li>本软件支持简体中文与繁体中文两种目标语言。</li>
            <li>任何因依赖本软件翻译结果而造成的误解、损失或不良影响，由使用者自行承担。</li>
          </ul>
          <h4>四、对游戏与数据的影响</h4>
          <ul>
            <li>使用本软件可能涉及修改 / 生成游戏资源包、Mod jar、整合包脚本（KubeJS）、存档等文件。请务必<b>提前备份</b>原文件；因使用本软件导致的 Mod 加载失败、游戏崩溃、进度丢失、存档损坏等问题，由使用者自行承担。</li>
            <li>本软件尽量保证「原文件只读」（只处理副本，产物为独立补丁 / 汉化文件），但不对任何运行环境、操作系统、硬件差异下的稳定性做任何保证。</li>
          </ul>
          <h4>五、网络与第三方资源</h4>
          <ul>
            <li>本软件的在线下载（Mod、词库等）依赖第三方平台（Modrinth、GitHub、CFPA 镜像等），本软件<b>不对</b>第三方资源的内容、版权、可用性、安全性负责；下载即代表你自行判断其来源与风险。</li>
            <li>本软件使用的 AI 服务由你自行配置的第三方接口提供；Key 仅保存于本机并仅用于请求对应服务，但你仍需自行承担使用该服务产生的费用与条款约束。</li>
          </ul>
          <h4>六、安全与法律责任</h4>
          <ul>
            <li>本软件<b>不包含</b>任何恶意代码、后门或数据收集；但不排除第三方环境、依赖或网络因素带来的风险，使用者应自行做好安全防护。</li>
            <li>严禁将本软件用于任何非法用途（包括但不限于：破解、作弊、绕过授权、侵犯他人权益、传播违法内容等）；因此产生的法律责任一律由使用者自行承担。</li>
            <li>开发者仅以个人身份免费提供本软件，对任何直接、间接、偶然、特殊或连带损失（包括但不限于：财产损失、数据丢失、名誉受损、商业利润损失），<b>不承担任何责任</b>，亦不对软件可用性、适用性作任何明示或默示担保。</li>
          </ul>
          <h4>七、协议变更</h4>
          <ul>
            <li>开发者保留随时更新、修改本免责声明的权利；更新后继续使用本软件即视为接受更新后的条款。</li>
          </ul>
        </div>
      </div>
    </div>

    <template v-else>
      <!-- 全局错误（含汇总项下载失败）：放在任务区外，无当前任务时也可见 -->
      <p v-if="error" class="err">{{ error }}</p>

      <!-- 当前任务 -->
      <template v-if="taskId">
        <h3 class="sub-title">当前任务</h3>
        <template v-if="task">
          <!-- 合并状态栏（UI 审查确认）：原 status-bar + activity-line 合成一行——
               左：状态 + 当前动作，右：计时器 + Token。挪到顶部，进度不动也明确在干活 -->
          <div class="status-line">
            <span v-if="isActive" class="spinner" aria-hidden="true"></span>
            <span class="status" :class="STATUS_CLS[task.status] || ''">
              {{ task.paused ? '已暂停' : (STATUS_TEXT[task.status] || task.status) }}
            </span>
            <template v-if="isActive">
              <span class="sep">·</span>
              <!-- 修复：动作名 + 阶段计数合成一个整体（用户诉求）——
                   不拆分、不互相挤，超长时整体省略号截断 -->
              <span class="action">
                {{ currentAction || stageText || '正在翻译…' }}
                <span v-if="stageCount" class="count">{{ stageCount }}</span>
              </span>
            </template>
            <span v-if="elapsed" class="timer">已运行 {{ elapsed }}</span>
            <span class="tokens" v-if="task.tokens_in || task.tokens_out">
              Token：进 {{ task.tokens_in }} / 出 {{ task.tokens_out }}
            </span>
          </div>

          <div class="stage-rail">
            <!-- 模式步骤：整合包/mod/地图 各自流程（光影预留） -->
            <div v-for="(s, i) in stageSteps" :key="s.key" class="stage-step"
                 :class="{ active: i === currentStep && task.status !== 'done',
                           done: i < currentStep || task.status === 'done' }">
              <i></i><span>{{ s.label }}</span>
            </div>
          </div>

          <div v-if="task.status === 'done'" class="result-card done">
            <span class="result-mark">✓</span>
            <div class="result-copy">
              <strong>翻译完成</strong>
              <p>{{ task.failed > 0 ? `有 ${task.failed} 条翻译失败（具体原因见流程结束后翻译报告）` : '产物已生成，点击下方按钮打开文件夹查看' }}</p>
            </div>
            <button class="btn ghost mini" @click="openReport">阅读翻译报告</button>
          </div>
          <div v-if="task.status === 'failed'" class="result-card failed">
            <span class="result-mark">✗</span>
            <div class="result-copy">
              <strong>翻译失败</strong>
              <p>{{ failInfo || '请查看下方明细或重新尝试' }}</p>
            </div>
          </div>

          <div class="progress-wrap">
            <div class="progress-track">
              <div class="progress-fill" :style="{ width: percent + '%' }"></div>
            </div>
            <span class="progress-num">{{ percent }}%（{{ task.done }}/{{ task.total }}）</span>
          </div>
          <div v-if="task.failed > 0" class="warn-box">
            有 {{ task.failed }} 条翻译失败（具体原因见流程结束后翻译报告）
          </div>
          <div v-if="failInfo" class="fail-box">失败原因：{{ failInfo }}</div>
          <div v-if="warnInfo && task.status !== 'failed'" class="warn-box">{{ warnInfo }}</div>

          <div class="actions">
            <button class="btn" :disabled="!isActive" @click="togglePause">
              {{ task.paused ? '继续' : '暂停' }}
            </button>
            <button class="btn danger" :disabled="!isActive" @click="doCancel">取消</button>
            <button v-if="task.status === 'done'" class="btn primary" @click="doOpenOutput()">📂 打开产物文件夹</button>
          </div>

          <div class="detail" v-if="rows.length">
            <div class="detail-head">
              <h3>翻译明细（最新在前）</h3>
              <!-- 审查状态灯：审查管道活跃红灯「静默审查中…」，审查完成绿灯 -->
              <span v-if="isActive && reviewVisible" class="review-indicator" :class="task.reviewing ? 'reviewing' : 'done'">
                <i class="dot" aria-hidden="true"></i>
                <span>{{ task.reviewing ? '静默审查中…' : '审查完成' }}</span>
              </span>
            </div>
            <div class="detail-list">
              <div v-for="(r, i) in rows" :key="r.key + '-' + i" class="detail-row"
                   :class="{ errrow: r.status === 'error', transrow: r.status === 'translating' }">
                <template v-if="r.status === 'translating'">
                  <!-- 批量翻译/判断/下载进行中：给用户「在干活」反馈，避免进度条/明细像卡死。
                       语言文件阶段 note 为空 → 默认「正在翻译」；硬编码/词库阶段 note 有具体说明。
                       count=0（词库下载）时不再显示「0 条」 -->
                  <div class="row-key">⏳ {{ r.note || '正在翻译' }}{{ r.count ? ` ${r.count} 条…` : '…' }}</div>
                </template>
                <template v-else-if="r.error">
                  <!-- 警告/失败条目：优先显示 error 内容（warn+key 条目走常规分支会渲染空箭头丢信息） -->
                  <div class="row-key">{{ r.key || r.jar || (r.file ? shortFile(r.file) : '') || '警告' }}</div>
                  <div class="row-langs"><span class="src">{{ r.error }}</span></div>
                </template>
                <template v-else-if="r.key || r.source || r.translated">
                  <!-- 常规翻译条目：长文本省略号截断（宽度恒定），悬停 title 看全文；
                       归属前缀 [mod]/[config/foo.json] 让整合包明细看到「在翻哪个 mod/配置」 -->
                  <div class="row-key" :title="(sourceTag(r) ? '[' + sourceTag(r) + '] ' : '') + r.key">
                    {{ sourceTag(r) ? '[' + sourceTag(r) + '] ' : '' }}{{ r.key }}
                  </div>
                  <div class="row-langs">
                    <span class="src" :title="r.source">{{ r.source }}</span>
                    <span class="arrow">→</span>
                    <span class="trans" :title="r.translated">{{ r.translated }}</span>
                  </div>
                </template>
                <template v-else>
                  <!-- 汇总/状态条目（任务完成 / 硬编码判断 / 警告错误 / 词库就绪）：
                       无 key/source/translated 时不渲染空行，按其携带字段显示有意义内容 -->
                  <div class="row-key">{{ r.jar || (r.file ? shortFile(r.file) : '') || (r.status === 'done' ? '完成' : (r.status || '汇总')) }}</div>
                  <div class="row-langs">
                    <template v-if="r.error">
                      <span class="src">{{ r.error }}</span>
                    </template>
                    <template v-else-if="r.judged !== undefined">
                      <span class="src">判断 {{ r.judged }} 条 / 翻译 {{ r.visible || 0 }} 条 / 未决 {{ r.unresolved || 0 }}</span>
                    </template>
                    <template v-else-if="r.pack || r.file">
                      <span class="src">产物已生成（打开文件夹查看）</span>
                    </template>
                    <template v-else>
                      <span class="src">{{ r.note || '' }}</span>
                    </template>
                  </div>
                </template>
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
      <div v-else class="idle">
        <!-- 启动中：点击「开始翻译」后右栏立即有反应（不等 autoTranslate 返回），
             转圈 + 任务名明确告知「已开始准备」而非卡在初始页 -->
        <template v-if="startingJob">
          <span class="spinner" aria-hidden="true"></span>
          <span>正在启动翻译任务：{{ startingJob.name }}…</span>
        </template>
        <template v-else>当前无运行任务</template>
      </div>

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
            <button v-if="job.status === 'done' && job.taskId" class="btn mini" @click="doOpenOutput(job.taskId)">📂 打开文件夹</button>
          </div>
        </div>
      </div>
    </template>

    <!-- 翻译报告弹窗（通用所有模式：整合包/mod/地图/光影，任务完成后阅读全部失败明细） -->
    <div v-if="reportData" class="overlay report-overlay" @click.self="closeReport">
      <div class="dialog report-dialog">
        <div class="report-head">
          <div>
            <h3>翻译报告</h3>
            <p class="report-sub">{{ reportData.input || '未知输入' }} · {{ reportData.target_lang }} · {{ fmtReportTime(reportData.created) }}</p>
          </div>
          <button class="btn mini" @click="closeReport">✕ 关闭</button>
        </div>
        <div class="report-body">
          <h4>翻译概览</h4>
          <div class="report-ov">
            <div class="ov-item"><b>{{ num(reportData.overview.total) }}</b><span>总词条</span></div>
            <div class="ov-item"><b>{{ num(reportData.overview.translated) }}</b><span>已翻译</span></div>
            <div class="ov-item bad"><b>{{ num(reportData.overview.failed) }}</b><span>未翻译</span></div>
            <div class="ov-item"><b>{{ reportData.overview.coverage }}%</b><span>覆盖率</span></div>
          </div>
          <div class="cov-bar"><i :style="{ width: Math.min(100, reportData.overview.coverage) + '%' }"></i></div>

          <h4>分阶段统计</h4>
          <table class="report-table" v-if="(reportData.stages||[]).length">
            <tr><th>阶段</th><th>词条数</th><th>已翻译</th><th>结果</th></tr>
            <tr v-for="(s, i) in (reportData.stages||[])" :key="i">
              <td>{{ s.name }}</td><td>{{ num(s.total) }}</td><td>{{ num(s.done) }}</td>
              <td :class="s.done >= s.total ? 'ok' : 'warn'">{{ s.done >= s.total ? '完成' : '进行中' }}</td>
            </tr>
          </table>

          <h4>生成产物</h4>
          <table class="report-table" v-if="(reportData.products||[]).length">
            <tr><th>产物</th><th>说明</th><th>大小</th></tr>
            <tr v-for="(p, i) in (reportData.products||[])" :key="i">
              <td>{{ p.name }}</td><td>{{ p.desc }}</td><td>{{ p.size_mb }} MB</td>
            </tr>
          </table>
          <p v-else class="report-empty">（无产物）</p>

          <h4>未翻译条目（全部 {{ (reportData.failures||[]).length }} 条）</h4>
          <div class="report-failures">
            <div v-for="(f, i) in (reportData.failures||[])" :key="i" class="fail-item">
              <span class="f-num">{{ i + 1 }}</span>
              <span class="f-text" :title="f.text">{{ f.text }}</span>
              <span class="f-reason" :title="f.reason">{{ f.reason }}</span>
            </div>
          </div>

          <h4>说明</h4>
          <ul class="report-notes">
            <li v-for="(n, i) in (reportData.notes||[])" :key="i">{{ n }}</li>
          </ul>
          <p v-if="reportError" class="err">{{ reportError }}</p>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.flow-panel { max-width: 100%; padding: 0; }
.sub-title { margin: 0 0 8px; font-size: 15px; color: var(--accent); }

.idle { color: var(--text-dim); padding: 18px 0 4px; font-size: 13px; display: flex; align-items: center; gap: 8px; }

/* 合并状态栏（UI 审查确认）：左状态+当前动作，右计时器+Token，顶部一行。
   固定单行（修复：动作文字含 jar 名太长会把一排撑成两排——nowrap + 动作截断省略号，
   计数/计时器/Token 不压缩） */
.status-line {
  display: flex; align-items: center; gap: 8px; flex-wrap: nowrap;
  padding: 8px 12px; background: var(--bg-2); border: 1px solid var(--line); margin-bottom: 14px;
}
.status { font-weight: 600; flex-shrink: 0; }
.status.ok { color: var(--accent); }
.status.bad { color: var(--danger); }
.status.warn { color: var(--warn); }
.sep { color: var(--text-dim); flex-shrink: 0; }
/* 动作文字（可能含 jar 名，很长）：单行截断省略号，防止撑爆一排 */
.action { color: var(--text); min-width: 0; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 阶段实时计数：等宽数字，随进度跳动 */
/* 运行计时器：单个进程已运行时长（进度不动时让用户知道仍在处理） */
.timer { color: var(--text-dim); font-size: 12px; font-variant-numeric: tabular-nums; flex-shrink: 0; white-space: nowrap; }
.tokens { color: var(--text-dim); font-size: 11px; flex-shrink: 0; white-space: nowrap; }
/* 完成态：标题下的原英文名淡化（灰色半透明） */
.hint-dim { opacity: .6; }
/* 运行中转圈：进度不动时（AI 审查/长请求）表示仍在处理 */
.spinner {
  width: 14px; height: 14px; flex-shrink: 0; margin-top: 2px;
  border: 2px solid var(--inactive);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: pixel-spin 0.7s linear infinite;
}
@keyframes pixel-spin { to { transform: rotate(360deg); } }

.progress-wrap { display: flex; align-items: center; gap: 12px; margin-bottom: 18px; }
.progress-track {
  flex: 1; height: 10px; border-radius: 0;
  background: var(--inactive); overflow: hidden;
}
.progress-fill { height: 100%; background: var(--moss); transition: width .3s; }
.progress-num { color: var(--text-dim); white-space: nowrap; }
/* 动作名内嵌的阶段计数（合成一体，用户诉求）：等宽数字 + 强调色 */
.action .count {
  color: var(--accent); font-weight: 600;
  font-variant-numeric: tabular-nums;
}

/* —— 空态三折叠：动态效果区（深绿渐变 hero 常显） + 使用说明/免责声明手风琴 —— */
.task-empty {
  text-align: left;
  transform-origin: top left;
  transform: scale(var(--empty-scale, 1));   /* 等比缩放：面板更窄整体缩小，不换行 */
}

/* 动态效果区：深绿渐变 hero */
.hero {
  position: relative; overflow: hidden;
  background: linear-gradient(160deg, var(--moss-dark) 0%, var(--moss) 55%, #4d7a55 100%);
  padding: 28px 32px 32px;
  border-bottom: 2px solid var(--line);
  color: #f5f1e6;
}
.hero::after {
  content: ""; position: absolute; left: -30%; top: 0; width: 25%; height: 100%;
  background: linear-gradient(100deg, transparent, rgba(200, 233, 106, .28), transparent);
  transform: skewX(-20deg);
  animation: hero-scan 3.6s ease-in-out infinite;
}
@keyframes hero-scan { 0% { left: -35%; } 60% { left: 130%; } 100% { left: 130%; } }
.hero-badge {
  display: inline-flex; align-items: center; gap: 8px;
  /* 修复：全局 --lime 被映射为 --accent（苔绿），深绿底上看不清——用真荧光绿并调亮 */
  font-size: 12px; letter-spacing: .18em; color: #dcf09f;
  border: 1px solid rgba(220, 240, 159, .55);
  padding: 4px 10px; margin-bottom: 18px;
}
.hero-badge b { animation: hero-blink 1.4s steps(2) infinite; }
@keyframes hero-blink { 50% { opacity: .35; } }
.hero-head { display: flex; align-items: center; gap: 22px; }
.pix-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 5px; flex-shrink: 0; }
.pix { width: 20px; height: 20px; border: 1px solid rgba(0,0,0,.25); animation: pix-bob 1.8s var(--ease) infinite; }
.pix:nth-child(1) { background: #c8e96a; }
.pix:nth-child(2) { background: #7fb069; animation-delay: .15s; }
.pix:nth-child(3) { background: #c8e96a; }
.pix:nth-child(4) { background: #a9c97a; animation-delay: .3s; }
.pix:nth-child(5) { background: #6b8f5a; animation-delay: .05s; }
.pix:nth-child(6) { background: #c8e96a; animation-delay: .2s; }
.pix:nth-child(7) { background: #93b86b; }
.pix:nth-child(8) { background: #c8e96a; animation-delay: .25s; }
.pix:nth-child(9) { background: #c8e96a; animation-delay: .1s; }
.pix:nth-child(10) { background: #5d8a4d; }
.pix:nth-child(11) { background: #c8e96a; animation-delay: .35s; }
.pix:nth-child(12) { background: #c8e96a; animation-delay: .2s; }
.pix:nth-child(13) { background: #a9c97a; animation-delay: .3s; }
.pix:nth-child(14) { background: #c8e96a; }
.pix:nth-child(15) { background: #7fb069; animation-delay: .15s; }
.pix:nth-child(16) { background: #c8e96a; animation-delay: .05s; }
@keyframes pix-bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-4px); } }
.hero-title h1 { margin: 0; font-size: clamp(24px, 2.4vw, 32px); letter-spacing: .1em; text-shadow: 3px 3px 0 rgba(0,0,0,.25); }
.hero-title p { margin: 8px 0 0; color: rgba(245, 241, 230, .85); font-size: clamp(12px, 1.1vw, 14px); letter-spacing: .12em; }
.hero-title p b { color: #c8e96a; font-weight: normal; }
/* 修复：状态行自适应字号（小屏不换行）+ 主文字弹性占位，steps 不换行 */
.hero-status {
  margin-top: 22px; padding-top: 14px;
  border-top: 1px dashed rgba(245, 241, 230, .35);
  display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  font-size: clamp(11px, 1.15vw, 13px); color: rgba(245, 241, 230, .95);
}
.hero-status > span:not(.cube):not(.steps) { flex: 1; min-width: 0; }
.hero-status .cube { width: 12px; height: 12px; background: #c8e96a; animation: hero-blink 1.4s steps(2) infinite; }
.hero-status .steps {
  margin-left: auto; white-space: nowrap;
  color: rgba(245, 241, 230, .72); font-size: clamp(10px, 1vw, 12px); letter-spacing: .06em;
}

/* 折叠区（使用说明 / 免责声明，手风琴同时只开一个） */
.fold { border-top: 2px solid var(--line); background: var(--bg-2); }
.fold-head {
  display: flex; align-items: center; gap: 10px;
  padding: 14px 18px; cursor: pointer; user-select: none;
  background: var(--bg-2);
}
.fold-head:hover { background: var(--bg-3); }
.fold .mark { width: 14px; height: 14px; background: var(--moss); flex-shrink: 0; }
.fold .mark.on { background: var(--lime); }
.fold-head .t { font-size: 15px; font-weight: 600; color: var(--moss-dark); letter-spacing: .06em; }
.fold-head .arrow { margin-left: auto; color: var(--text-dim); transition: transform .25s var(--ease); }
.fold.open .fold-head .arrow { transform: rotate(180deg); }
/* 折叠正文：底色比 --bg-3 淡一档（用户反馈太深），仍有与面板的层次 */
.fold-body { padding: 6px 22px 20px; background: #efeadc; border-top: 2px dashed var(--line); }
.fold-body h4 { font-size: 13px; color: var(--moss); margin: 16px 0 8px; border-left: 4px solid var(--lime); padding-left: 10px; }
.fold-body h4:first-child { margin-top: 8px; }
.fold-body ol { margin: 0; padding-left: 22px; }
.fold-body li { margin: 5px 0; color: var(--text); }
.fold-body li b { color: var(--moss-dark); }
.fold-body .sub { color: var(--text-dim); font-size: 12.5px; }
.fold-body ul { margin: 6px 0; padding-left: 20px; }
.fold-body ul li { margin: 4px 0; color: var(--text); }
.disclaimer { border: 2px solid var(--rust); background: #fbf1e8; padding: 12px 14px; margin: 14px 0; }
.disclaimer .head { display: flex; align-items: center; gap: 8px; color: var(--rust); font-weight: 600; margin-bottom: 6px; }
.disclaimer .head i { width: 12px; height: 12px; background: var(--rust); }

.stage-rail { display: flex; gap: 0; margin: 2px 0 14px; }
.stage-step { flex: 1; display: flex; align-items: center; gap: 8px; color: var(--text-dim); font-size: 12px; min-width: 0; }
.stage-step:not(:last-child)::after { content: ""; flex: 1; height: 2px; background: var(--inactive); }
.stage-step i { width: 12px; height: 12px; flex: none; border: 2px solid var(--inactive); transform: rotate(45deg); }
.stage-step.active { color: var(--moss); font-weight: 600; }
.stage-step.active i { border-color: var(--moss); background: var(--lime); }
.stage-step.done { color: var(--moss); }
.stage-step.done i { border-color: var(--moss); background: var(--lime); }
.stage-step.done::after { background: var(--moss); }

.fail-box {
  border: 1px solid var(--danger); color: var(--danger);
  border-radius: 0; padding: 10px; margin-bottom: 10px;
}
.warn-box {
  border: 1px solid var(--warn); color: var(--warn);
  background: rgba(232, 163, 61, .08);
  border-radius: 0; padding: 10px; margin-bottom: 10px;
}

.result-card { display: flex; align-items: center; gap: 12px; padding: 12px 14px; border-radius: 0; margin-bottom: 12px; }
.result-card.done { background: rgba(200, 233, 106, .18); border: 1px solid var(--moss); }
.result-card.failed { background: rgba(185, 95, 61, .12); border: 1px solid var(--rust); }
.result-mark { font-size: 22px; flex-shrink: 0; font-weight: 700; }
.result-card.done .result-mark { color: var(--moss); }
.result-card.failed .result-mark { color: var(--rust); }
.result-copy { flex: 1; min-width: 0; }
.result-copy strong { font-size: 15px; }
.result-copy p { margin: 2px 0 0; color: var(--text-dim); font-size: 13px; }

.detail { margin-top: 20px; }
.detail h3 { margin: 0; font-size: 14px; color: var(--text-dim); }

/* 明细标题行：标题左、审查状态灯右（与明细框右缘对齐） */
.detail-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; }
/* 审查状态灯：红灯「静默审查中…」脉冲 / 绿灯「审查完成」静态 */
.review-indicator { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; font-weight: 600; white-space: nowrap; }
.review-indicator .dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; border: 1px solid rgba(0,0,0,.12); }
.review-indicator.reviewing { color: var(--danger); }
.review-indicator.reviewing .dot { background: var(--danger); animation: review-blink 1.1s ease-in-out infinite; }
.review-indicator.done { color: var(--accent); }
.review-indicator.done .dot { background: var(--accent); }
@keyframes review-blink {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(185, 95, 61, .35); }
  50%      { opacity: .45; box-shadow: 0 0 0 5px rgba(185, 95, 61, 0); }
}
.detail-list {
  max-height: 320px; overflow-y: auto; overflow-x: hidden;   /* 宽度恒定：横向绝不出现滚动条，纵向保留 */
  border: 1px solid var(--line); border-radius: 0;
  background: var(--bg-2); padding: 4px;
}
.detail-row {
  display: flex; align-items: baseline; gap: 10px;
  padding: 7px 10px; border-bottom: 1px solid var(--line);
}
.detail-row:last-child { border-bottom: none; }
.detail-row.errrow { background: rgba(232,106,94,.08); }
.detail-row.transrow { background: rgba(200, 233, 106, .05); }
.row-key { color: var(--accent); font-size: 12px; min-width: 140px; max-width: 220px; flex-shrink: 0; word-break: break-word; white-space: normal; }
.row-langs { flex: 1; display: flex; gap: 8px; align-items: baseline; min-width: 0; flex-wrap: wrap; }
/* 文本自动换行 + 每排限约 12 字（用户诉求：长文本自行分行，12 字一行）
   ch 对中文≈1 汉字宽，12ch≈12 汉字/行 */
.row-langs .src, .row-langs .trans {
  min-width: 0; max-width: 12em;
  word-break: break-word; white-space: normal;
}
.row-langs .src { color: var(--text-dim); }
.row-langs .trans { color: var(--text); }
.arrow { color: var(--text-dim); flex-shrink: 0; }
.badge { font-size: 12px; }
.badge.bad { color: var(--danger); }
.badge.warn { color: var(--warn); }

/* 已完成汇总 */
.done-summary { margin-top: 26px; }
.done-list {
  border: 1px solid var(--line); border-radius: 0;
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

/* —— 翻译报告弹窗（通用所有模式）—— */
.report-overlay {
  position: fixed; inset: 0; background: rgba(6, 10, 13, .62);
  display: flex; align-items: center; justify-content: center; z-index: 60;
}
.report-dialog {
  width: 720px; max-width: 92vw; max-height: 86vh;
  display: flex; flex-direction: column;
  /* 修复：报告弹窗背景透明啥也看不到——缺背景色/边框，内容直接透到遮罩上 */
  background: var(--bg-2); border: 1px solid var(--line);
  box-shadow: 0 14px 44px rgba(0, 0, 0, .45);
}
.report-head { display: flex; align-items: center; justify-content: space-between; padding: 14px 20px; border-bottom: 1px solid var(--line); }
.report-head h3 { font-size: 17px; color: var(--accent); letter-spacing: .04em; }
.report-sub { color: var(--text-dim); font-size: 12px; margin-top: 3px; }
.report-body { padding: 16px 20px; overflow-y: auto; }
.report-body h4 { font-size: 14px; color: var(--accent-2); margin: 18px 0 10px; border-left: 4px solid var(--accent); padding-left: 10px; }
.report-body h4:first-child { margin-top: 0; }
.report-ov { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.ov-item { background: var(--bg-3); border: 1px solid var(--line); padding: 10px 12px; text-align: center; }
.ov-item b { display: block; font-size: 19px; color: var(--accent); font-variant-numeric: tabular-nums; }
.ov-item.bad b { color: var(--danger); }
.ov-item span { color: var(--text-dim); font-size: 11px; }
.cov-bar { height: 12px; background: var(--bg-3); border: 1px solid var(--line); margin-top: 10px; overflow: hidden; }
.cov-bar i { display: block; height: 100%; background: var(--accent); }
.report-table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.report-table th { text-align: left; color: var(--text-dim); font-weight: 600; padding: 6px 10px; border-bottom: 2px solid var(--line); }
.report-table td { padding: 6px 10px; border-bottom: 1px solid var(--line); }
.report-table td.ok { color: var(--accent); }
.report-table td.warn { color: var(--warn); }
.report-empty { color: var(--text-dim); font-size: 12px; }
.report-failures { max-height: 300px; overflow-y: auto; border: 1px solid var(--line); background: var(--bg-2); padding: 4px; }
.fail-item { display: flex; align-items: baseline; gap: 10px; padding: 5px 8px; border-bottom: 1px solid var(--line); }
.fail-item:last-child { border-bottom: none; }
.f-num { color: var(--text-dim); font-size: 11px; flex-shrink: 0; width: 26px; }
.f-text { color: var(--text); font-size: 12px; flex: 1; min-width: 0; word-break: break-word; }
.f-reason { color: var(--danger); font-size: 11px; flex-shrink: 0; max-width: 45%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.report-notes { color: var(--text-dim); font-size: 12px; line-height: 1.8; padding-left: 18px; }
</style>
