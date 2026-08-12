<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { checkUpdate, clearCache, getCacheSize, getCfpaStatus, getConfig, getKeyStatus, saveConfig, saveKey, testConnection, testThroughput } from '../api'

// props：onDone(配置对象) / onClose() 由 App 注入（保存后回调）；closable=false 时隐藏「取消」（首次开屏强制配置）
const props = defineProps({ onDone: Function, onClose: Function, onCacheCleared: Function, closable: { type: Boolean, default: true } })

// 厂商预置映射：选中自动带出 base_url + model（允许手动覆盖）
const PROVIDERS = {
  'DeepSeek': { base_url: 'https://api.deepseek.com', model: 'deepseek-chat' },
  '通义千问': { base_url: 'https://dashscope.aliyuncs.com/compatible-mode', model: 'qwen-plus' },
  'Kimi': { base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
  'Ollama': { base_url: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:7b' },
  '自定义': { base_url: '', model: '' },
}
// 免费 API 平台预设（第三选项）：注册免费拿 Key（限量/限速），端点/模型经核实
const FREE_PROVIDERS = {
  '智谱AI': { base_url: 'https://open.bigmodel.cn/api/paas/v4', model: 'glm-4-flash',
              note: 'GLM-4-Flash 永久免费 · 需在 bigmodel.cn 免费注册拿 Key' },
  '讯飞星火': { base_url: 'https://spark-api-open.xf-yun.com/v1', model: 'spark-lite',
              note: 'Spark Lite 永久免费（限速）· 需在讯飞开放平台免费注册拿 Key' },
  '自定义免费': { base_url: '', model: '', note: '其他免费平台（硅基流动/OpenRouter 等）端点自填' },
}
// 目标语言：只需简中/繁中（用户诉求，其他语言去掉）
const LANGUAGES = [
  { code: 'zh_cn', label: '简体中文' },
  { code: 'zh_tw', label: '繁体中文' },
]
// keyring 已配置时的回显占位符：避免用户每次误以为要重输（保存时跳过该占位值）
const API_KEY_PLACEHOLDER = '已配置（••••）'
// 应用版本号：打包时同步更新（设置页「配置」标题右侧淡灰小字展示）
const APP_VERSION = '1.0.2'

const engine = ref('llm')            // llm(用户 API) | free(免费 API) | machine(机翻)，三选项互斥
const provider = ref('DeepSeek')
// 当前引擎对应的厂商表（llm→付费厂商；free→免费平台）；机翻无厂商
const providerTable = computed(() => engine.value === 'free' ? FREE_PROVIDERS : PROVIDERS)
const providerOptions = computed(() => Object.keys(providerTable.value))
const baseUrl = ref('')
const model = ref('')
// 修复：不再用 localStorage 明文存 API Key（冗余且不安全）——真正生效靠后端 keyring，
// 是否已配置由 /api/key/status 判定（onMounted 里回显占位符）
const apiKey = ref('')
const concurrency = ref(5)   // 并发数：同时进行的 AI 请求数（1-16，默认 5）
const scanConcurrency = ref(4)   // 扫描并发数：同时解压/解析的 jar 数（1-16，默认 4）
const batchSize = ref(null)      // 批量大小：一次请求翻译 N 条（5-60，空 = 厂商默认 25）
// 吞吐档位（用户诉求：并发 + 批次合并成一个调整项，同时调两个方便）。
// 档位同时设 concurrency（并发请求数）+ batch_size（每次翻译条数）——高吞吐更快，
// 但更吃 API 额度/易限流；网络或 API 慢时用低档更稳。
// 三档吞吐（用户诉求：删「极高」档——12 并发超 DeepSeek 默认 8 易限流，batch 过大
// 易超 max_tokens 截断反而慢）。高=新的最高档：批次 40（配合 max_tokens 8192 不截断）
// + 扫描 8（原极高能力），并发锁 8 卡在 DeepSeek 上限内——最快且不触发限流。
// 修复：high 回到 8/35/6——40 批 + max_tokens 8192 长文本易截断降级（同极高不可用根因），
// 且用户旧保存值 8/35/6 匹配不上 40 会导致档位回退「中」
const THROUGHPUT_PRESETS = {
  low:     { concurrency: 2, batch_size: 12, scan_concurrency: 2 },
  medium:  { concurrency: 5, batch_size: 25, scan_concurrency: 4 },
  high:    { concurrency: 8, batch_size: 35, scan_concurrency: 6 },
}
const throughput = ref('medium')
function applyThroughput() {
  const p = THROUGHPUT_PRESETS[throughput.value] || THROUGHPUT_PRESETS.medium
  concurrency.value = p.concurrency
  batchSize.value = p.batch_size
  scanConcurrency.value = p.scan_concurrency   // 扫描并发并入档位（用户诉求）
}
const sillyMode = ref(false)     // 胡言乱语模式：搞笑/热梗翻译但忠实原意（设置页开关）
const cacheDir = ref('')         // 缓存/工作目录（可改到其他盘省 C 盘；空 = 系统默认；重启生效）
async function pickCacheDir() {
  try {
    if (window.pywebview?.api?.select_path) {
      // 桌面版：系统目录选择框
      const p = await window.pywebview.api.select_path('folder')
      if (p && p.length) cacheDir.value = p[0]
    } else {
      // 浏览器版：手动输入路径（留空 = 系统默认）
      cacheDir.value = prompt('输入缓存目录路径（如 D:\\mc-cache，留空用系统默认）：', cacheDir.value) || ''
    }
  } catch (e) {
    error.value = `选择缓存目录失败：${e.message}`
  }
}
const targetLang = ref('zh_cn')
const saving = ref(false)
const error = ref('')
const tip = ref('')
// 测试连接状态：testing 进行中 / testResult 结果文本 / testOk 成功与否
const testing = ref(false)
const testResult = ref('')
const testOk = ref(null)
// 测试吞吐档位：tpTesting 进行中 / tpResult 结果 / tpOk 成功与否（自动选稳定档位）
const tpTesting = ref(false)
const tpResult = ref('')
const tpOk = ref(null)

// 缓存管理：占用显示（临时 WORK_DIR + 产物 OUTPUTS_DIR）+ 清除按钮
const cacheSize = ref(null)      // { work_bytes, outputs_bytes, total_mb, work_path, outputs_path }
const cacheMsg = ref('')         // 清理结果提示
const clearing = ref(false)      // 清除缓存进行中
const showClearConfirm = ref(false)   // 自写清除缓存确认弹窗（不用浏览器 window.confirm）

// CFPA 社区人工翻译词库：状态（已下载版本/词条数）；检查更新走统一 runCheckUpdate（修复 recheck：
// 原「下载词库」按钮换成新逻辑——版本检测 + 有更新下载/没更新提示/没连上重试，不再用老下载）
const cfpaStatus = ref(null)     // { downloaded, mc_version, count, size_mb } 或 null
const cfpaVersion = ref('1.20.1')   // 默认 MC 版本（可改）
// 三项内置资源（CFPA 词库 / i18n 汉化 mod / VP 硬编码 mod）「检查更新」状态
// updState[kind] = { checking, msg, ok }，kind ∈ cfpa/i18n/vp
const updState = ref({})
async function runCheckUpdate(kind) {
  updState.value[kind] = { checking: true, msg: '', ok: null }
  try {
    const r = await checkUpdate({ kind, mc_version: cfpaVersion.value.trim() })
    updState.value[kind] = { checking: false, msg: r.message || '完成', ok: !!r.ok }
    if (kind === 'cfpa' && r.ok) await refreshCfpaStatus()
  } catch (e) {
    updState.value[kind] = { checking: false, msg: e.message, ok: false }
  }
}

async function refreshCfpaStatus() {
  try {
    cfpaStatus.value = await getCfpaStatus()
    // 已下载 → 版本回填输入框（"Minecraft-Mod-Language-Modpack-1-20.zip" → "1.20"）
    if (cfpaStatus.value && cfpaStatus.value.downloaded) {
      const v = cfpaStatus.value.mc_version.replace(/^.*Modpack-/, '').replace(/\.zip$/, '')
      if (v) cfpaVersion.value = v.replace(/-/g, '.')
    }
  } catch { cfpaStatus.value = null }   // 后端不可用 → 显示「未下载」
}

async function refreshCacheSize() {
  try {
    cacheSize.value = await getCacheSize()
  } catch (e) {
    cacheSize.value = null       // 后端不可用 → 显示 —，不清空已展示路径
  }
}

function onClearCache() {
  // 弹自写确认弹窗：清缓存会删运行中任务的中间文件，明确警示再动手
  showClearConfirm.value = true
}
async function doClearCache() {
  showClearConfirm.value = false
  clearing.value = true
  cacheMsg.value = ''
  try {
    const r = await clearCache()
    cacheMsg.value = `已清除 ${r.cleared_mb} MB 缓存`
    await refreshCacheSize()      // 清理后刷新占用显示
    props.onCacheCleared?.()      // 清理缓存会删 progress/memory → 即时刷新断点续联列表（用户诉求）
  } catch (e) {
    cacheMsg.value = `清除失败：${e.message}`
  } finally {
    clearing.value = false
  }
}

async function runTest(which) {
  testing.value = true
  testResult.value = ''
  testOk.value = null
  try {
    // 传当前表单值（base_url/model/api_key），后端仅本次测试使用，不落盘。
    // 占位符「已配置（••••）」不是真实 key：不传，让后端回退 keyring 读取（否则占位符当 key → 401 链接失效）
    // free 引擎也走 LLM 测试（engine=free 后端用免费平台预设默认）
    const eng = which === 'machine' ? 'machine' : engine.value
    const body = eng === 'machine'
      ? { engine: 'machine' }
      : { engine: eng, provider: provider.value,
          llm: { base_url: baseUrl.value.trim(), model: model.value.trim() },
          api_key: (apiKey.value && apiKey.value !== API_KEY_PLACEHOLDER) ? apiKey.value : undefined }
    const r = await testConnection(body)
    testOk.value = !!r.ok
    testResult.value = r.message || ''
    if (r.ok) autoSave()   // 测试连接成功即保存当前设置（用户改了没点保存也不丢）
  } catch (e) {
    testOk.value = false
    testResult.value = e.message
  } finally {
    testing.value = false
  }
}

// 测试吞吐档位：后端从高到低逐档探测并发/批大小，返回绝对稳定不触发 API 报错的档位。
// 成功后应用到档位下拉 + 并发/批/扫描三个值（用户诉求：自动选稳定档位让用户舒心运行）。
async function runThroughputTest() {
  tpTesting.value = true
  tpResult.value = ''
  tpOk.value = null
  try {
    const body = engine.value === 'machine'
      ? { engine: 'machine' }
      : { engine: engine.value, provider: provider.value,
          llm: { base_url: baseUrl.value.trim(), model: model.value.trim() },
          api_key: (apiKey.value && apiKey.value !== API_KEY_PLACEHOLDER) ? apiKey.value : undefined }
    const r = await testThroughput(body)
    if (r.ok && r.preset) {
      throughput.value = r.preset
      concurrency.value = r.concurrency
      batchSize.value = r.batch_size
      scanConcurrency.value = r.scan_concurrency
      tpOk.value = true
      tpResult.value = r.message
      autoSave()   // 新档位即时保存
    } else {
      tpOk.value = false
      tpResult.value = r.message || '吞吐档位测试失败'
    }
  } catch (e) {
    tpOk.value = false
    tpResult.value = e.message
  } finally {
    tpTesting.value = false
  }
}

// 回填配置期间为 true：applyProvider 只在用户手动切换厂商下拉时覆盖 base_url/model，
// 避免 onMounted 回填用户保存过的自定义值时被厂商预置覆盖
let loading = true
// 配置读取成功标记：getConfig 失败（后端未就绪）时保持 false，autoSave/saveAndClose 据此
// 拒绝保存默认值——否则 onefile 冷启动期间前端先加载、后端还没起，回填失败 provider 是
// 默认 DeepSeek，用户一保存就把已有 config.json（stepfun/自定义等）覆盖成 deepseek（用户反馈）
let loaded = false

function applyProvider(name) {
  if (loading) return
  const p = providerTable.value[name]
  if (p) { baseUrl.value = p.base_url; model.value = p.model }
}
watch(provider, applyProvider)
// 引擎切换：厂商表变了，若当前 provider 不在新表则切到第一个，并重放该平台默认端点
watch(engine, () => {
  if (loading) return
  const table = providerTable.value
  if (!table[provider.value]) provider.value = Object.keys(table)[0]
  applyProvider(provider.value)
})

// 自动保存（设置改动即时落盘）：引擎/厂商/端点/模型/目标语言/并发任一变更，
// 防抖 800ms 后 saveConfig——用户不用点「保存」按钮，关闭窗口也不丢（「每次上线重选」根治）
let saveTimer = null
// 结构优化：保存配置 body 三处（autoSave/onUnmounted/saveAndClose）构造一致，提取复用
function buildConfigBody() {
  return {
    engine: engine.value, provider: provider.value,
    target_lang: targetLang.value, concurrency: concurrency.value,
    scan_concurrency: scanConcurrency.value,
    batch_size: batchSize.value ? Number(batchSize.value) : null,
    silly_mode: sillyMode.value,
    cache_dir: cacheDir.value,
    llm: { base_url: baseUrl.value.trim(), model: model.value.trim() },
  }
}

async function autoSave() {
  if (loading || !loaded) return
  clearTimeout(saveTimer)
  saveTimer = setTimeout(async () => {
    try {
      const body = buildConfigBody()
      await saveConfig(body)
      props.onDone?.(body)
    } catch (e) { /* 自动保存失败静默：用户点「保存」按钮时再显式报错 */ }
  }, 800)
}
watch([engine, provider, baseUrl, model, targetLang, concurrency, scanConcurrency, batchSize, sillyMode, cacheDir], autoSave)
// 组件卸载（关窗/销毁）时立即保存当前值，防防抖窗口期内的改动丢失（webview 关窗会丢弃 pending setTimeout）
onUnmounted(() => {
  clearTimeout(saveTimer)
  saveConfig(buildConfigBody()).catch(() => {})   // 幂等：与保存按钮同值，静默兜底
  // 修复（recheck）：点「关闭」关窗时同步前端 config——否则后端已存 target_lang，前端
  // config 还是旧值，后续 detect/autoTranslate 请求带旧语言。之前引用未定义的 body
  //（ReferenceError → onDone 不被调用，修复失效）；改用 buildConfigBody() 当前值。
  props.onDone?.(buildConfigBody())
})

onMounted(async () => {
  try {
    const cfg = await getConfig()
    if (cfg.engine) engine.value = cfg.engine
    const table = cfg.engine === 'free' ? FREE_PROVIDERS : PROVIDERS
    if (cfg.provider && table[cfg.provider]) provider.value = cfg.provider
    // 修复（recheck）：目标语言只剩简中/繁中——旧 config 若存过 ja_jp/en_us 等，直接回填
    // 会让下拉无匹配显示空白；不在支持列表内回退 zh_cn
    targetLang.value = (cfg.target_lang && LANGUAGES.some(l => l.code === cfg.target_lang))
      ? cfg.target_lang : 'zh_cn'
    // llm/free 参数：config 有则回填；否则/为空用平台预置默认（默认地址/模型，
    // 玩家无需手动填——用户反馈「默认地址和模型应是默认保存，非玩家手动」）
    const prov = table[provider.value] || {}
    baseUrl.value = (cfg.llm && cfg.llm.base_url) || prov.base_url || ''
    model.value = (cfg.llm && cfg.llm.model) || prov.model || ''
    // 并发数回填：未配置（None）时保持默认 5
    if (cfg.concurrency) concurrency.value = cfg.concurrency
    // 批量大小回填：未配置保持默认（厂商）
    if (cfg.batch_size) batchSize.value = cfg.batch_size
    // 吞吐档位回填：按保存的并发/批次匹配最近档位
    const _c = cfg.concurrency || 5
    // 修复：档位回填按「并发数」匹配（batch/scan 可能因版本调整与旧保存值不一致——
    // 精确匹配 concurrency+batch 会导致用户旧值回退到「中」）；并发匹配即认定该档位
    const _preset = Object.entries(THROUGHPUT_PRESETS)
      .find(([, v]) => v.concurrency === _c)?.[0]
    throughput.value = _preset || 'medium'
    // 扫描并发数回填：未配置时保持默认 4
    if (cfg.scan_concurrency) scanConcurrency.value = cfg.scan_concurrency
    // 胡言乱语模式回填：未配置默认关
    if (cfg.silly_mode != null) sillyMode.value = !!cfg.silly_mode
    // 缓存目录回填：未配置保持空（系统默认）
    if (cfg.cache_dir) cacheDir.value = cfg.cache_dir
    // O2：本地未存 key 时，问后端 keyring 是否已配置过——已配置则回显占位符，不空白
    if (!apiKey.value) {
      try {
        const st = await getKeyStatus()
        if (st.configured) apiKey.value = API_KEY_PLACEHOLDER
      } catch (e) { /* 后端不可用则保持空白，让用户手动填 */ }
    }
    loaded = true                 // getConfig 成功即允许保存（防后端未就绪时用默认覆盖已有配置）。
                                  // 必须放在 refresh 前——翻译中重开设置时 refresh 慢/失败不影响保存，
                                  // 否则用户立即改吞吐点保存会被「配置读取失败」误拦（用户实测）
    await refreshCacheSize()      // 回填配置后顺带加载缓存占用
    await refreshCfpaStatus()     // 加载 CFPA 社区词库状态
  } catch (e) {
    tip.value = '读取后端配置失败（后端未启动？将使用默认值）'
  } finally {
    // watch(provider) 默认 pre-flush，回调在下一微任务才执行；
    // nextTick 等它跑完再放开 loading，确保回填的 base_url/model 不被厂商预置覆盖
    await nextTick()
    loading = false
  }
})

// 聚焦 key 输入框时清掉占位符，让用户可直接输入新 key 覆盖
function onKeyFocus() {
  if (apiKey.value === API_KEY_PLACEHOLDER) apiKey.value = ''
}

async function saveAndClose() {
  saving.value = true
  error.value = ''
  try {
    // 修复：api_key 只写后端 keyring（AI 引擎真正读取的地方），不再落 localStorage——
    // 明文存 key 冗余且不安全（keyring 是权威，localStorage 这份拷贝毫无必要）
    // 占位符「已配置（••••）」不是真实 key：跳过写 keyring，避免覆盖已存 key
    if (apiKey.value && apiKey.value !== API_KEY_PLACEHOLDER) {
      try {
        await saveKey(apiKey.value)
      } catch (e) {
        // keyring 写失败（如 Windows 凭据库异常）不阻塞其余配置保存——用户反馈「配置保存不完全」
        error.value = `API Key 保存失败（${e.message}），其余配置已保存`
      }
    }
    const body = buildConfigBody()
    await saveConfig(body)
    props.onDone?.(body)
    props.onClose?.()
  } catch (e) {
    if (!error.value) error.value = `保存失败：${e.message}`
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h2>配置<span class="ver">v{{ APP_VERSION }}</span></h2>
    <p class="hint">选择翻译引擎与目标语言（源语言自动识别）</p>

    <div class="field">
      <label>翻译引擎</label>
      <div class="radio-row">
        <label class="radio"><input type="radio" value="llm" v-model="engine" /> AI 接入</label>
        <label class="radio"><input type="radio" value="free" v-model="engine" /> 免费 API</label>
        <label class="radio"><input type="radio" value="machine" v-model="engine" /> 在线机翻</label>
      </div>
    </div>

    <!-- AI 接入区（llm 用户 API / free 免费 API 共用字段） -->
    <template v-if="engine !== 'machine'">
      <!-- 免费 API 平台说明 -->
      <div v-if="engine === 'free'" class="field">
        <div class="tip-box">
          免费平台注册即送 API Key（不花钱，限量/限速）。选好平台后填它的 Key 即可。
          智谱 GLM-4-Flash 与讯飞 Spark Lite 官方承诺永久免费。
        </div>
      </div>
      <div class="field">
        <label>{{ engine === 'free' ? '免费平台' : '厂商' }}</label>
        <select v-model="provider">
          <option v-for="name in providerOptions" :key="name" :value="name">{{ name }}</option>
        </select>
        <small v-if="engine === 'free' && providerTable[provider]" class="sub">
          {{ providerTable[provider].note }}
        </small>
      </div>
      <div class="field">
        <label>API 地址（base_url）</label>
        <input type="text" v-model="baseUrl" placeholder="https://api.deepseek.com" />
      </div>
      <div class="field">
        <label>模型名（model）</label>
        <input type="text" v-model="model" placeholder="deepseek-chat" />
      </div>
      <div class="field">
        <label>API Key</label>
        <input type="password" v-model="apiKey" placeholder="sk-..." autocomplete="off" @focus="onKeyFocus" />
        <small class="sub">{{ apiKey === API_KEY_PLACEHOLDER
          ? '已配置（点击输入框可重新输入覆盖）'
          : (engine === 'free'
            ? '免费平台的 Key：在对应平台控制台免费申请，经后端写入本机系统凭据库（keyring）'
            : '经后端写入本机系统凭据库（keyring），不落配置文件') }}</small>
      </div>
      <div class="field">
        <label>吞吐档位</label>
        <select v-model="throughput" @change="applyThroughput">
          <option value="low">低（稳定 · 并发 2 · 批 12 · 扫描 2）</option>
          <option value="medium">中（均衡 · 并发 5 · 批 25 · 扫描 4）</option>
          <option value="high">高（较快 · 并发 8 · 批 35 · 扫描 6）</option>
        </select>
        <small class="sub">同时调「并发请求数 + 每次翻译条数 + 扫描并发数」：高档吞吐更快但更吃 API 额度/磁盘/易限流；网络或 API 慢时用低档更稳</small>
      </div>
      <div class="field">
        <label>胡言乱语模式</label>
        <button type="button" class="btn" :class="{ silly_on: sillyMode }" @click="sillyMode = !sillyMode">
          {{ sillyMode ? '✔ 已开启：搞笑 + 热梗翻译' : '打开胡言乱语模式' }}
        </button>
        <small class="sub">开启后翻译结合当下热梗搞笑输出，但忠实保留原意（仅 LLM 引擎；硬编码判断不受影响）</small>
      </div>
      <div class="field test-row">
        <button class="btn" :disabled="testing" @click="runTest('llm')">
          {{ testing ? '测试中…' : '测试连接' }}
        </button>
        <button class="btn" :disabled="tpTesting" @click="runThroughputTest">
          <span v-if="tpTesting" class="spinner"></span>{{ tpTesting ? '测试中……' : '测试吞吐档位' }}
        </button>
        <span v-if="testResult" class="test-result" :class="testOk ? 'ok' : 'fail'">
          {{ testOk ? '✓ 连接成功' : '✗ ' + testResult }}
        </span>
        <span v-if="tpResult" class="test-result" :class="tpOk ? 'ok' : 'fail'">
          {{ tpOk ? '✓ ' + tpResult : '✗ ' + tpResult }}
        </span>
      </div>
    </template>

    <!-- 在线机翻区 -->
    <template v-else>
      <div class="field">
        <label>翻译方式</label>
        <div class="tip-box">使用 Google 免费翻译（免 Key）</div>
      </div>
      <div class="field test-row">
        <button class="btn" :disabled="testing" @click="runTest('machine')">
          {{ testing ? '测试中…' : '测试连接' }}
        </button>
        <span v-if="testResult" class="test-result" :class="testOk ? 'ok' : 'fail'">
          {{ testOk ? '✓ 连接成功' : '✗ ' + testResult }}
        </span>
      </div>
    </template>

    <div class="field">
      <label>目标语言</label>
      <select v-model="targetLang">
        <option v-for="l in LANGUAGES" :key="l.code" :value="l.code">{{ l.label }}</option>
      </select>
      <small class="sub">源语言无需选择，识别步骤会自动判断</small>
    </div>

    <!-- 缓存目录：可改到其他盘省 C 盘（保存后即时生效） -->
    <div class="field cache-block">
      <label>缓存目录</label>
      <div class="cache-line">
        <input type="text" v-model="cacheDir" placeholder="留空 = 系统默认（temp/mc-translator）" style="flex:1;min-width:0" />
        <button class="btn" @click="pickCacheDir">选择目录</button>
      </div>
      <small class="sub">整合包解压缓存 / 地图副本 / 任务 / 已翻译记忆都存这里。改到其他盘（如 D:\mc-cache）省 C 盘空间；<b>保存后即时生效</b>（有翻译任务运行中时等任务完成，或重启生效）。留空用系统默认。</small>
    </div>

    <!-- 缓存管理：占用显示 + 清除按钮（清临时中间产物与旧产物） -->
    <div class="field cache-block">
      <label>缓存管理</label>
      <div class="cache-line">
        <span class="cache-size">
          缓存占用：{{ cacheSize ? cacheSize.total_mb + ' MB' : '—' }}
        </span>
        <button class="btn danger" :disabled="clearing" @click="onClearCache">
          {{ clearing ? '清理中…' : '清除缓存' }}
        </button>
      </div>
      <small v-if="cacheSize" class="sub">
        临时文件：{{ cacheSize.work_path }}<br />
        产物目录：{{ cacheSize.outputs_path }}
      </small>
      <p v-if="cacheMsg" class="tip">{{ cacheMsg }}</p>
    </div>

    <!-- CFPA 社区人工翻译词库：语言文件命中社区词库直接写回（零成本高质量），缺口才走 AI -->
    <div class="field cache-block">
      <label>社区词库（CFPA 人工翻译）</label>
      <div class="cache-line">
        <span class="cache-size">
          {{ cfpaStatus && cfpaStatus.downloaded
            ? (cfpaStatus.bundled_count > 0 && !cfpaStatus.mc_version
                ? `✅ 已内置 ${cfpaStatus.bundled_count} 版本汉化包`
                : `已下载 ${cfpaStatus.mc_version} · ${cfpaStatus.count} 词条 · ${cfpaStatus.size_mb}MB`)
            : '未下载' }}
        </span>
        <span class="cfpa-version">
          MC
          <input class="ver-input" v-model="cfpaVersion" placeholder="1.20.1" />
        </span>
        <button class="btn" :disabled="updState.cfpa?.checking" @click="runCheckUpdate('cfpa')">
          <span v-if="updState.cfpa?.checking" class="spinner"></span>{{ updState.cfpa?.checking ? '检查中……' : '检查更新' }}
        </button>
      </div>
      <small class="sub">应用已内置 6 版本 CFPA 人工翻译词库（1.12.2~1.21），翻译时按整合包 MC 版本自动加载对应版本；「检查更新」联网拉取最新版（有更新自动下载，无更新/无网络明确提示）。</small>
      <p v-if="updState.cfpa?.msg" class="tip" :class="{ err: !updState.cfpa?.ok }">{{ updState.cfpa.msg }}</p>
    </div>

    <!-- i18n 汉化模组：内置 + 检查更新（更新版下载到应用目录，清缓存不删） -->
    <div class="field cache-block">
      <label>i18n 汉化模组</label>
      <div class="cache-line">
        <span class="cache-size">进游戏自动下载 CFPA 全量汉化资源包</span>
        <button class="btn" :disabled="updState.i18n?.checking" @click="runCheckUpdate('i18n')">
          <span v-if="updState.i18n?.checking" class="spinner"></span>{{ updState.i18n?.checking ? '检查中……' : '检查更新' }}
        </button>
      </div>
      <p v-if="updState.i18n?.msg" class="tip" :class="{ err: !updState.i18n?.ok }">{{ updState.i18n.msg }}</p>
    </div>

    <!-- Vault Patcher 硬编码补丁：内置 + 检查更新（更新版下载到应用目录，清缓存不删） -->
    <div class="field cache-block">
      <label>Vault Patcher 硬编码补丁</label>
      <div class="cache-line">
        <span class="cache-size">汉化 Mod 字节码里写死的文本</span>
        <button class="btn" :disabled="updState.vp?.checking" @click="runCheckUpdate('vp')">
          <span v-if="updState.vp?.checking" class="spinner"></span>{{ updState.vp?.checking ? '检查中……' : '检查更新' }}
        </button>
      </div>
      <p v-if="updState.vp?.msg" class="tip" :class="{ err: !updState.vp?.ok }">{{ updState.vp.msg }}</p>
    </div>

    <!-- 自写清除缓存确认弹窗（不用浏览器 window.confirm） -->
    <div v-if="showClearConfirm" class="overlay" @click.self="showClearConfirm = false">
      <div class="dialog confirm-dialog">
        <h3>清除缓存</h3>
        <p class="confirm-text">将删除临时文件与旧产物，正在翻译的任务会受影响。确定清除？</p>
        <div class="actions">
          <button class="btn" @click="showClearConfirm = false">取消</button>
          <button class="btn danger" :disabled="clearing" @click="doClearCache">确定清除</button>
        </div>
      </div>
    </div>

    <p v-if="tip" class="tip">{{ tip }}</p>
    <p v-if="error" class="err">{{ error }}</p>

    <div class="actions">
      <button class="btn" v-if="closable" :disabled="saving" @click="props.onClose?.()">关闭</button>
      <button class="btn primary" :disabled="saving" @click="saveAndClose">
        {{ saving ? '保存中…' : '保存' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.ver { font-size: 11px; font-weight: 400; color: var(--text-dim); margin-left: 10px; letter-spacing: .05em; vertical-align: middle; }
/* 测试连接：按钮 + 结果横排，结果绿=成功 / 红=失败 */
.test-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.test-result { font-size: 13px; }
.test-result.ok { color: var(--accent); }
.test-result.fail { color: var(--danger); white-space: pre-wrap; }
/* 吞吐档位测试转圈动画（像素风：旋转小方块） */
.spinner {
  display: inline-block; width: 11px; height: 11px; margin-right: 7px;
  border: 2px solid var(--line); border-top-color: var(--accent);
  animation: tp-spin .7s steps(8, end) infinite; vertical-align: -1px;
}
@keyframes tp-spin { to { transform: rotate(360deg); } }

/* 缓存管理：占用文本 + 清除按钮横排；危险按钮红色 */
.cache-block { border-top: 1px solid var(--line); padding-top: 14px; margin-top: 6px; }
.cache-line { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
/* CFPA 词库：MC 版本输入框（窄，与下载按钮横排） */
.cfpa-version { display: inline-flex; align-items: center; gap: 4px; color: var(--text-dim); font-size: 12px; }
/* 词库下载进度：像素风轨道 + 荧光绿填充 + 百分比文本（无 content-length 时停滞 0%） */
.cfpa-progress { display: flex; align-items: center; gap: 10px; margin-top: 10px; }
.cfpa-progress-track {
  flex: 1; height: 10px; background: var(--bg-3);
  border: 1px solid var(--line); border-radius: 0; overflow: hidden;
}
.cfpa-progress-bar {
  height: 100%; background: var(--lime); border-radius: 0;
  transition: width .25s steps(12, end);
}
.cfpa-progress-text { font-size: 12px; color: var(--text-dim); white-space: nowrap; }
.ver-input {
  width: 68px; background: var(--bg-3); border: 1px solid var(--line);
  border-radius: 0; color: var(--text); padding: 3px 6px; font-size: 12px; outline: none;
}
.ver-input:focus { border-color: var(--accent); }
.cache-size { font-weight: 600; color: var(--text); }
.btn.danger { background: var(--danger); color: #fff; border-color: transparent; }
/* 胡言乱语模式开启态：主色填充，一眼可辨 */
.btn.silly_on { background: var(--accent); border-color: var(--accent); color: #f5f1e6; font-weight: 600; }

/* 自写清除缓存确认弹窗（替代浏览器 window.confirm） */
.overlay {
  position: fixed; inset: 0; background: rgba(0, 0, 0, .55);
  display: flex; align-items: center; justify-content: center; z-index: 100;
}
.dialog {
  background: var(--bg-2); border: 1px solid var(--line); border-radius: 0;
  padding: 22px 26px; width: 380px; max-width: 90vw; box-shadow: 0 8px 30px rgba(0,0,0,.35);
}
.confirm-dialog h3 { margin: 0 0 12px; color: var(--text); }
.confirm-text { color: var(--text-dim); margin: 0 0 18px; line-height: 1.6; }
.dialog .actions { display: flex; justify-content: flex-end; gap: 10px; }
</style>
