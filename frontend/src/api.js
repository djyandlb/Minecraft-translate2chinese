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

// 扫描：返回 { mods:[{modid,entries,gaps}], total_gaps }
export const scan = (body) => req('/scan', { method: 'POST', body: JSON.stringify(body) })

// 翻译：返回 { task_id }
export const translate = (body) => req('/translate', { method: 'POST', body: JSON.stringify(body) })

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
