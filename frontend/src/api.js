// 后端 API 封装：全部走相对路径 /api，vite dev 代理到 http://127.0.0.1:8000
const BASE = '/api'

async function req(path, options = {}) {
  // 超时兜底（AbortController）：fetch 无默认超时，若后端不响应/连接悬挂会永远 pending——
  // 点击「开始翻译」后 autoTranslate 一直不返回，右栏就卡在「正在启动」/空态出不来。
  // 60s 对识别/翻译启动足够（后端 create_task 毫秒级返回），超时报错让前端走到失败分支而非无限等；
  // 需要更长的调用（如动态测试吞吐，mimo 慢要跑几十秒）可传 options.timeout 覆盖（v1.2.6）。
  // 修复：fetch 响应头到达后 clearTimeout 会让 res.json() 失去超时保护（body 挂起无限等）——
  // 把整个 fetch+json 放同一个 timer 内。
  const timeout = (typeof options.timeout === 'number' && options.timeout > 0) ? options.timeout : 60000
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)
  try {
    const res = await fetch(BASE + path, {
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      ...options,
    })
    if (!res.ok) {
      let msg = `请求失败（HTTP ${res.status}）`
      try {
        const d = await res.json()
        if (d.detail) msg = String(d.detail)
      } catch (e) { /* 非 JSON 响应，保留默认提示 */ }
      const err = new Error(msg)
      err.httpError = true   // 标记：后端已响应但非 2xx（保留真实错误，别误报「无法连接后端」）
      throw err
    }
    // 修复（recheck）：2xx 但响应体非 JSON（空 204 / 纯文本 / 代理页）→ 明确提示格式异常，
    // 而不是被 catch 误报成「无法连接后端」误导排查方向
    const bodyText = await res.text()
    try { return JSON.parse(bodyText) } catch (e) {
      const err = new Error(`响应格式异常（HTTP ${res.status}，非 JSON 数据）`)
      err.httpError = true
      throw err
    }
  } catch (e) {
    if (e.name === 'AbortError') throw new Error(`请求超时（${Math.round(timeout / 1000)} 秒），请重试`)
    // 修复：HTTP 错误（res.ok=false，带真实 detail/状态）保留原因，不再一律显示
    // 「无法连接后端」误导（用户反馈清除失败只看到「无法连接后端」）
    if (e && e.httpError) throw e
    throw new Error('无法连接后端，请确认 uvicorn 已启动')
  } finally {
    clearTimeout(timer)
  }
}

// 配置：读 / 写（POST 时不携带 api_key，后端也会忽略该字段）
export const getConfig = () => req('/config')
export const saveConfig = (cfg) => req('/config', { method: 'POST', body: JSON.stringify(cfg) })

// 缓存：查询占用（{work_bytes, outputs_bytes, total_mb, work_path, outputs_path}）/ 清除（返回清理大小）
export const getCacheSize = () => req('/cache-size')
export const clearCache = () => req('/clear-cache', { method: 'POST' })

// API Key：写入后端 keyring（前端 localStorage 仅作 UI 回显，真正生效靠后端 keyring）
export const saveKey = (apiKey) => req('/key', { method: 'POST', body: JSON.stringify({ api_key: apiKey }) })

// API Key 状态：查询 keyring 是否已配置，返回 { configured: bool }（后端绝不返回 key 本身）
export const getKeyStatus = () => req('/key/status')

// 自动识别：POST /api/detect，返回 {kind, source_lang, pack_format, summary}
export const detect = (body) => req('/detect', { method: 'POST', body: JSON.stringify(body) })

// 统一全自动翻译：POST /api/auto-translate，返回 {task_id}
export const autoTranslate = (body) => req('/auto-translate', { method: 'POST', body: JSON.stringify(body) })

// 文件上传：multipart，返回 {path, name, size}
export async function uploadFile(file) {
  const fd = new FormData()
  fd.append('file', file)
  // 修复：uploadFile 之前裸 fetch 无超时 → 上传大包后端悬挂会永远 pending，
  // detectingCount 永不归零，ScanView 的「添加」被永久禁用。加 120s 超时（大文件放宽）。
  // 修复（recheck）：res.json() 之前在 clearTimeout 之后调用，body 挂起时脱离超时保护
  //（detectingCount 仍会卡死）——把 fetch + json 整个放同一 timer 内，对齐 req() 的结构。
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), 120000)
  try {
    const res = await fetch(BASE + '/upload', { method: 'POST', body: fd, signal: controller.signal })
    if (!res.ok) {
      let msg = `上传失败（HTTP ${res.status}）`
      try { const d = await res.json(); if (d.detail) msg = String(d.detail) } catch (e) {}
      const err = new Error(msg)
      err.httpError = true   // 标记：后端已响应但非 2xx（保留真实原因，别误报「无法连接后端」）
      throw err
    }
    return await res.json()
  } catch (e) {
    // F13-review：网络层错误统一为中文提示（对齐 req 的「无法连接后端」）
    if (e.name === 'AbortError') throw new Error('上传超时（120 秒），请重试')
    if (e && e.httpError) throw e
    throw new Error('无法连接后端，请确认 uvicorn 已启动')
  } finally {
    clearTimeout(timer)
  }
}

// 任务状态 / 控制（统一入口走 /auto-translate；旧两步流程端点已由前端不再调用，仅后端保留）
export const getTask = (id) => req(`/task/${id}`)
export const cancelTask = (id) => req(`/task/${id}/cancel`, { method: 'POST' })
export const pauseTask = (id) => req(`/task/${id}/pause`, { method: 'POST' })
// 打开产物文件夹（完成态直接看产物，不选地方下载）：后端 os.startfile 弹资源管理器
export const openOutput = (id) => req(`/task/${id}/open-output`, { method: 'POST' })
// 翻译报告：任务完成后弹窗阅读（含全部未翻译条目，通用所有模式）
export const getReport = (id) => req(`/task/${id}/report`)

// SSE 任务流：状态变更即时推送（替代 1s 轮询，前后端联动更及时）
export const taskStreamUrl = (id) => `${BASE}/task/${id}/stream`

// 目录浏览：返回 { parent, dirs[] }
export const browse = (path = '') => {
  const q = path ? `?path=${encodeURIComponent(path)}` : ''
  return req(`/browse${q}`)
}

// 未完成项目列表（断点续联）：启动扫描临时文件直接显示，不用拖入
export const listProjects = () => req('/projects')
// 删除项目：清理对应临时文件（memory/progress/extracted）
export const deleteProject = (id) => req(`/project/${id}`, { method: 'DELETE' })

// CFPA 社区人工翻译词库：状态（{downloaded, mc_version, count, size_mb}）/ 下载（body {mc_version}）
export const getCfpaStatus = () => req('/cfpa/status')
export const downloadCfpa = (mcVersion) => req('/cfpa/download', { method: 'POST', body: JSON.stringify({ mc_version: mcVersion }) })

// 测试连接：body 可带当前表单的 engine / llm {base_url, model} / api_key（api_key 仅本次测试用，后端不落盘）
// 返回 { ok: bool, message: string }，后端绝不回显 key
export const testConnection = (body = {}) => req('/test-connection', { method: 'POST', body: JSON.stringify(body) })

// 测试吞吐档位：后端逐档探测并发/批大小，返回建议档位（{ ok, preset, concurrency, batch_size, scan_concurrency, message }）
// v1.4.6：超时 180s→300s——慢 API（mimo/stepfun 单批 >60s）后端测量 _measure_w(2×60s)+
// 爬坡(150s) 可达 270s，180s 必被前端 abort（后端协程还在跑、结果丢弃）
export const testThroughput = (body = {}) => req('/test-throughput', { method: 'POST', body: JSON.stringify(body), timeout: 300000 })

// 检查更新内置资源（kind: cfpa/i18n/vp）：有更新下载到应用目录，返回 { ok, status, message, version }
export const checkUpdate = (body = {}) => req('/check-update', { method: 'POST', body: JSON.stringify(body) })
