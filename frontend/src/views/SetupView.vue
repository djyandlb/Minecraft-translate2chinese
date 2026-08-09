<script setup>
import { onMounted, ref, watch } from 'vue'
import { getConfig, saveConfig } from '../api'

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
const MC_VERSIONS = ['1.12.2', '1.16.5', '1.18.2', '1.19.2', '1.20.1', '1.21']
const API_KEY_STORE = 'mc_translator_api_key'

const engine = ref('llm')            // llm | machine，互斥
const provider = ref('DeepSeek')
const baseUrl = ref('')
const model = ref('')
const apiKey = ref(localStorage.getItem(API_KEY_STORE) || '')
const sourceLang = ref('en_us')
const targetLang = ref('zh_cn')
const mcVersion = ref('1.20.1')
const saving = ref(false)
const error = ref('')
const tip = ref('')

function applyProvider(name) {
  const p = PROVIDERS[name]
  if (p) { baseUrl.value = p.base_url; model.value = p.model }
}
watch(provider, applyProvider)

onMounted(async () => {
  try {
    const cfg = await getConfig()
    if (cfg.engine) engine.value = cfg.engine
    if (cfg.provider && PROVIDERS[cfg.provider]) provider.value = cfg.provider
    if (cfg.source_lang) sourceLang.value = cfg.source_lang
    if (cfg.target_lang) targetLang.value = cfg.target_lang
    if (cfg.mc_version) mcVersion.value = cfg.mc_version
    if (cfg.llm) {
      baseUrl.value = cfg.llm.base_url || ''
      model.value = cfg.llm.model || ''
    }
  } catch (e) {
    tip.value = '读取后端配置失败（后端未启动？将使用默认值）'
  }
})

async function saveAndNext() {
  saving.value = true
  error.value = ''
  try {
    // api_key 只存浏览器本地，绝不随 config 发送给后端
    localStorage.setItem(API_KEY_STORE, apiKey.value)
    const body = {
      engine: engine.value,
      provider: provider.value,
      source_lang: sourceLang.value,
      target_lang: targetLang.value,
      mc_version: mcVersion.value,
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
    <p class="hint">选择翻译引擎与语言设置</p>

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
        <input type="password" v-model="apiKey" placeholder="sk-..." autocomplete="off" />
        <small class="sub">仅保存在本机浏览器（localStorage），不会发送给后端</small>
      </div>
    </template>

    <!-- 在线机翻区 -->
    <template v-else>
      <div class="field">
        <label>翻译方式</label>
        <div class="tip-box">使用 Google 免费翻译（免 Key）</div>
      </div>
    </template>

    <div class="field-row">
      <div class="field">
        <label>源语言</label>
        <select v-model="sourceLang">
          <option v-for="l in LANGUAGES" :key="l.code" :value="l.code">{{ l.label }}</option>
        </select>
      </div>
      <div class="field">
        <label>目标语言</label>
        <select v-model="targetLang">
          <option v-for="l in LANGUAGES" :key="l.code" :value="l.code">{{ l.label }}</option>
        </select>
      </div>
    </div>

    <div class="field">
      <label>MC 版本</label>
      <select v-model="mcVersion">
        <option v-for="v in MC_VERSIONS" :key="v" :value="v">{{ v }}</option>
      </select>
      <small class="sub">资源包格式（pack_format）由后端按版本默认处理</small>
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
