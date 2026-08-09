<script setup>
import { nextTick, onMounted, ref, watch } from 'vue'
import { getConfig, getKeyStatus, saveConfig, saveKey, testConnection } from '../api'

// props：onDone(配置对象) / onNext() 由 App 注入，走完一步 → next()
const props = defineProps({ onDone: Function, onNext: Function })

// 厂商预置映射：选中自动带出 base_url + model（允许手动覆盖）
const PROVIDERS = {
  'DeepSeek': { base_url: 'https://api.deepseek.com', model: 'deepseek-chat' },
  '通义千问': { base_url: 'https://dashscope.aliyuncs.com/compatible-mode', model: 'qwen-plus' },
  'Kimi': { base_url: 'https://api.moonshot.cn/v1', model: 'moonshot-v1-8k' },
  'Ollama': { base_url: 'http://127.0.0.1:11434/v1', model: 'qwen2.5:7b' },
  '自定义': { base_url: '', model: '' },
}
const LANGUAGES = [
  { code: 'en_us', label: '英文（美式）' },
  { code: 'zh_cn', label: '简体中文' },
  { code: 'zh_tw', label: '繁体中文' },
  { code: 'ja_jp', label: '日文' },
  { code: 'ko_kr', label: '韩文' },
  { code: 'fr_fr', label: '法文' },
  { code: 'de_de', label: '德文' },
]
const API_KEY_STORE = 'mc_translator_api_key'
// keyring 已配置时的回显占位符：避免用户每次误以为要重输（保存时跳过该占位值）
const API_KEY_PLACEHOLDER = '已配置（••••）'

const engine = ref('llm')            // llm | machine，互斥
const provider = ref('DeepSeek')
const baseUrl = ref('')
const model = ref('')
const apiKey = ref(localStorage.getItem(API_KEY_STORE) || '')
const targetLang = ref('zh_cn')
const saving = ref(false)
const error = ref('')
const tip = ref('')
// 测试连接状态：testing 进行中 / testResult 结果文本 / testOk 成功与否
const testing = ref(false)
const testResult = ref('')
const testOk = ref(null)

async function runTest(which) {
  testing.value = true
  testResult.value = ''
  testOk.value = null
  try {
    // 传当前表单值（base_url/model/api_key），后端仅本次测试使用，不落盘
    const body = which === 'llm'
      ? { engine: 'llm', llm: { base_url: baseUrl.value.trim(), model: model.value.trim() }, api_key: apiKey.value || undefined }
      : { engine: 'machine' }
    const r = await testConnection(body)
    testOk.value = !!r.ok
    testResult.value = r.message || ''
  } catch (e) {
    testOk.value = false
    testResult.value = e.message
  } finally {
    testing.value = false
  }
}

// 回填配置期间为 true：applyProvider 只在用户手动切换厂商下拉时覆盖 base_url/model，
// 避免 onMounted 回填用户保存过的自定义值时被厂商预置覆盖
let loading = true

function applyProvider(name) {
  if (loading) return
  const p = PROVIDERS[name]
  if (p) { baseUrl.value = p.base_url; model.value = p.model }
}
watch(provider, applyProvider)

onMounted(async () => {
  try {
    const cfg = await getConfig()
    if (cfg.engine) engine.value = cfg.engine
    if (cfg.provider && PROVIDERS[cfg.provider]) provider.value = cfg.provider
    if (cfg.target_lang) targetLang.value = cfg.target_lang
    if (cfg.llm) {
      baseUrl.value = cfg.llm.base_url || ''
      model.value = cfg.llm.model || ''
    }
    // O2：本地未存 key 时，问后端 keyring 是否已配置过——已配置则回显占位符，不空白
    if (!apiKey.value) {
      try {
        const st = await getKeyStatus()
        if (st.configured) apiKey.value = API_KEY_PLACEHOLDER
      } catch (e) { /* 后端不可用则保持空白，让用户手动填 */ }
    }
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

async function saveAndNext() {
  saving.value = true
  error.value = ''
  try {
    // api_key 写进后端 keyring（AI 引擎真正读取的地方），localStorage 仅作 UI 回显；
    // 占位符「已配置（••••）」不是真实 key：跳过写 keyring / localStorage，避免覆盖已存 key
    if (apiKey.value && apiKey.value !== API_KEY_PLACEHOLDER) await saveKey(apiKey.value)
    if (apiKey.value !== API_KEY_PLACEHOLDER) localStorage.setItem(API_KEY_STORE, apiKey.value)
    else localStorage.removeItem(API_KEY_STORE)
    const body = {
      engine: engine.value,
      provider: provider.value,
      target_lang: targetLang.value,
      llm: { base_url: baseUrl.value.trim(), model: model.value.trim() },
    }
    await saveConfig(body)
    props.onDone?.(body)
    props.onNext?.()
  } catch (e) {
    error.value = `保存失败：${e.message}`
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h2>① 配置</h2>
    <p class="hint">选择翻译引擎与目标语言（源语言自动识别）</p>

    <div class="field">
      <label>翻译引擎</label>
      <div class="radio-row">
        <label class="radio"><input type="radio" value="llm" v-model="engine" /> AI 接入</label>
        <label class="radio"><input type="radio" value="machine" v-model="engine" /> 在线机翻</label>
      </div>
    </div>

    <!-- AI 接入区 -->
    <template v-if="engine === 'llm'">
      <div class="field">
        <label>厂商</label>
        <select v-model="provider">
          <option v-for="(_, name) in PROVIDERS" :key="name" :value="name">{{ name }}</option>
        </select>
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
          : '经后端写入本机系统凭据库（keyring），不落配置文件' }}</small>
      </div>
      <div class="field test-row">
        <button class="btn" :disabled="testing" @click="runTest('llm')">
          {{ testing ? '测试中…' : '测试连接' }}
        </button>
        <span v-if="testResult" class="test-result" :class="testOk ? 'ok' : 'fail'">
          {{ testOk ? '✓ 连接成功' : '✗ ' + testResult }}
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

    <p v-if="tip" class="tip">{{ tip }}</p>
    <p v-if="error" class="err">{{ error }}</p>

    <div class="actions">
      <button class="btn primary" :disabled="saving" @click="saveAndNext">
        {{ saving ? '保存中…' : '保存并下一步' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
/* 测试连接：按钮 + 结果横排，结果绿=成功 / 红=失败 */
.test-row { display: flex; align-items: center; gap: 12px; }
.test-result { font-size: 13px; }
.test-result.ok { color: var(--accent); }
.test-result.fail { color: var(--danger); white-space: pre-wrap; }
</style>
