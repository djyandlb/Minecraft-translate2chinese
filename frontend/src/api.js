// 后端 API 封装：全部走相对路径 /api，vite dev 代理到 http://127.0.0.1:8000
const BASE = '/api'

async function req(path, options = {}) {
  let res
  try {
    res = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    })
  } catch (e) {
    throw new Error('无法连接后端，请确认 uvicorn 已启动')
  }
  if (!res.ok) {
    let msg = `请求失败（HTTP ${res.status}）`
    try {
      const d = await res.json()
      if (d.detail) msg = String(d.detail)
    } catch (e) { /* 非 JSON 响应，保留默认提示 */ }
    throw new Error(msg)
  }
  return res.json()
}

// 配置：读 / 写（POST 时不携带 api_key，后端也会忽略该字段）
export const getConfig = () => req('/config')
export const saveConfig = (cfg) => req('/config', { method: 'POST', body: JSON.stringify(cfg) })

// API Key：写入后端 keyring（前端 localStorage 仅作 UI 回显，真正生效靠后端 keyring）
export const saveKey = (apiKey) => req('/key', { method: 'POST', body: JSON.stringify({ api_key: apiKey }) })

// 自动识别：POST /api/detect，返回 {kind, source_lang, pack_format, summary}
export const detect = (body) => req('/detect', { method: 'POST', body: JSON.stringify(body) })

// 统一全自动翻译：POST /api/auto-translate，返回 {task_id}
export const autoTranslate = (body) => req('/auto-translate', { method: 'POST', body: JSON.stringify(body) })

// 文件上传：multipart，返回 {path, name, size}
export async function uploadFile(file) {
  const fd = new FormData()
  fd.append('file', file)
  const res = await fetch(BASE + '/upload', { method: 'POST', body: fd })
  if (!res.ok) {
    let msg = `上传失败（HTTP ${res.status}）`
    try { const d = await res.json(); if (d.detail) msg = String(d.detail) } catch (e) {}
    throw new Error(msg)
  }
  return res.json()
}

// 扫描：返回 { mods:[{modid,entries,gaps}], total_gaps }
export const scan = (body) => req('/scan', { method: 'POST', body: JSON.stringify(body) })

// 翻译：返回 { task_id }
export const translate = (body) => req('/translate', { method: 'POST', body: JSON.stringify(body) })

// 地图汉化：扫描世界存档返回 { entries, preview }；翻译返回 { task_id }
export const mapScan = (body) => req('/map-scan', { method: 'POST', body: JSON.stringify(body) })
export const mapTranslate = (body) => req('/map-translate', { method: 'POST', body: JSON.stringify(body) })

// 硬编码汉化：扫描 jar 内硬编码字符串返回 {strings, count}；翻译返回 { task_id }
export const hardcodeScan = (body) => req('/hardcode-scan', { method: 'POST', body: JSON.stringify(body) })
export const hardcodeTranslate = (body) => req('/hardcode-translate', { method: 'POST', body: JSON.stringify(body) })

// 任务状态 / 控制
export const getTask = (id) => req(`/task/${id}`)
export const cancelTask = (id) => req(`/task/${id}/cancel`, { method: 'POST' })
export const pauseTask = (id) => req(`/task/${id}/pause`, { method: 'POST' })
export const downloadUrl = (id) => `${BASE}/task/${id}/download`

// 目录浏览：返回 { parent, dirs[] }
export const browse = (path = '') => {
  const q = path ? `?path=${encodeURIComponent(path)}` : ''
  return req(`/browse${q}`)
}

// 术语表：body { path }，返回 { loaded }
export const uploadGlossary = (path) => req('/glossary', { method: 'POST', body: JSON.stringify({ path }) })
