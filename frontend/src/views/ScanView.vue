<script setup>
import { computed, ref } from 'vue'
import { browse } from '../api'

// props：jobs（App 持有的任务队列，展示用）+ addPath/addUpload（App 注入的入队方法，可 await）
const props = defineProps({
  jobs: { type: Array, default: () => [] },
  processing: { type: Boolean, default: false },   // 队列处理中 → 禁用移除/清空/开始
  detecting: { type: Boolean, default: false },    // 有文件识别中 → 禁用添加
  addPath: { type: Function, required: true },     // 本地路径入队（内部 detect）
  addUpload: { type: Function, required: true },   // 上传文件入队（浏览器/无本地路径兜底）
})
const emit = defineEmits(['remove-job', 'clear-jobs', 'start-queue'])

// 桌面版（pywebview）检测：存在 window.pywebview → 可用 js_api 直接拿本地路径，不走上传
const isDesktop = typeof window !== 'undefined' && !!window.pywebview

const dragOver = ref(false)
const fileInput = ref(null)
const error = ref('')

const KIND_TEXT = { modpack: '整合包', modjar: '单个 mod', map: '地图存档' }
const STATUS_TEXT = { pending: '待翻译', running: '翻译中…', done: '完成 ✓', failed: '失败 ✗' }
const STATUS_ICON = { pending: '•', running: '◎', done: '✓', failed: '✗' }

const pendingCount = computed(() => props.jobs.filter(j => j.status === 'pending').length)
const startEnabled = computed(() => !props.processing && pendingCount.value > 0)
const failCountTip = computed(() => props.jobs.some(j => j.status === 'failed'))

// —— 目录浏览器（浏览器模式跨盘浏览，复用现有 browser，盘根可列盘符）——
const showBrowser = ref(false)
const browserPath = ref('')
const dirs = ref([])
const parent = ref('')
const browsing = ref(false)

const drives = ref([])   // 盘符快捷栏（C:\、D:\…，一键换盘）

// 加载盘符列表：盘根 browse 返回所有盘符；失败兜底常见盘符
async function loadDrives() {
  try {
    const r = await browse('C:\\')
    drives.value = r.dirs || []
  } catch {
    drives.value = ['C:/', 'D:/', 'E:/', 'F:/']
  }
}

async function openBrowser() {
  showBrowser.value = true
  await loadDrives()
  await loadDirs('')
}
async function loadDirs(p) {
  browsing.value = true
  try {
    const r = await browse(p)
    browserPath.value = p || r.parent || ''
    dirs.value = r.dirs
    parent.value = r.parent || ''
    error.value = ''
  } catch (e) {
    error.value = `读取目录失败：${e.message}`
  } finally {
    browsing.value = false
  }
}
function enterDir(name) {
  // 盘符（如 D:\）直接进入，不拼接当前路径（防 C:\/D:\ 拼接失效，永远进不了 D/E 盘）
  if (name.includes(':')) { loadDirs(name); return }
  loadDirs(browserPath.value ? `${browserPath.value}/${name}` : name)
}
function goUp() {
  if (parent.value) loadDirs(parent.value)
}
// 选中目录后收浏览器并立即入队（App 内自动识别）
async function pickPath() {
  showBrowser.value = false
  await props.addPath(browserPath.value)
}

// —— 桌面版 js_api：系统对话框选文件/目录，直接拿本地路径（不走上传）——
async function pickLocal(kind) {
  try {
    const paths = await window.pywebview.api.select_path(kind)
    if (paths && paths.length) {
      for (const p of paths) await props.addPath(p)   // file 可多选返回多路径；folder 单目录
    }
  } catch (e) {
    error.value = `选择${kind === 'folder' ? '目录' : '文件'}失败：${e.message}`
  }
}

// —— 拖放 / 点选上传（支持多文件）——
function onDragOver(e) { e.preventDefault(); dragOver.value = true }
function onDragLeave(e) {
  // 子元素间移动会误触发 dragleave 导致高亮闪断；仅离开整个 dropzone 才熄灭
  if (e.currentTarget.contains(e.relatedTarget)) return
  dragOver.value = false
}
// 拖入的目录处理：不再当文件上传（否则报「无法连接后端」）
// 桌面版能拿本地路径 → 直接本地识别；拿不到 → 引导用「选择目录」按钮
async function handleFolderDrop(file) {
  const localPath = isDesktop && file && file.path
  if (localPath) await props.addPath(localPath)   // 目录路径 → 后端 detect 识别为整合包
  else if (isDesktop) {
    // pywebview 对拖入文件夹的 File.path 可能不可用 → 顺手弹系统目录选择框（与「选择目录」同路径）
    error.value = '拖入的文件夹无法取得本地路径，请用「选择目录」按钮'
    await pickLocal('folder')
  } else {
    // 浏览器拖入文件夹只得到一个空 File（无 webkitdirectory）→ 提示走目录浏览
    error.value = '浏览器不支持拖入文件夹，请用「选择目录」按钮选择整合包目录'
  }
}
async function onDrop(e) {
  e.preventDefault(); dragOver.value = false
  const files = Array.from(e.dataTransfer.files || [])
  if (!files.length) {
    // 某些环境拖入文件夹时 files 为空（items 里才是目录条目）→ 直接引导走「选择目录」
    if (Array.from(e.dataTransfer.items || []).some(it => it.kind === 'file')) {
      await handleFolderDrop(null)
    }
    return
  }
  for (const file of files) {
    // 目录判定：Chromium 中拖入的文件夹表现为 File（type='' 且 size=0）
    const isFolder = file.type === '' && file.size === 0
    if (isFolder) { await handleFolderDrop(file); continue }
    // 常规文件：桌面版 File 若暴露本地 path 属性（pywebview WebView2 实测），直接用本地地址不走上传
    const localPath = isDesktop && (file.path || file.webkitRelativePath)
    if (localPath) await props.addPath(localPath)
    else await props.addUpload(file)     // 浏览器 / 拿不到本地路径 → 走上传兜底
  }
}
function onClickDrop() { fileInput.value?.click() }
// 拖放区点击：桌面版弹系统文件对话框（多选），浏览器版走 input 选文件
function onZoneClick() {
  if (isDesktop) pickLocal('file')
  else onClickDrop()
}
async function onFilePicked(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''                    // 允许连续选择同一批文件
  for (const file of files) {
    const localPath = isDesktop && (file.path || file.webkitRelativePath)
    if (localPath) await props.addPath(localPath)
    else await props.addUpload(file)
  }
}
</script>

<template>
  <section class="panel scan-panel">
    <h2>添加文件</h2>
    <p class="hint">拖入 jar / 整合包 / 地图文件，或选择目录（支持多个，队列逐个翻译）</p>

    <!-- 大拖放区：拖入多个 jar/压缩包/地图文件，或点击选文件 -->
    <div class="dropzone" :class="{ drag: dragOver }"
         @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop" @click="onZoneClick">
      <p class="big">{{ dragOver ? '松手，放这里！' : '拖入 jar / 压缩包 / 地图文件' }}</p>
      <p class="small">支持多个文件；桌面版可直接拖入整合包目录（浏览器请用「浏览目录」）</p>
      <input ref="fileInput" type="file" multiple style="display:none" @change="onFilePicked" />
    </div>

    <!-- 选择按钮：桌面版系统对话框；浏览器版点选文件 + 目录浏览 -->
    <div class="path-row pick-row">
      <template v-if="isDesktop">
        <button class="btn" :disabled="detecting" @click="pickLocal('file')">选择文件</button>
        <button class="btn" :disabled="detecting" @click="pickLocal('folder')">选择目录</button>
      </template>
      <template v-else>
        <button class="btn" :disabled="detecting" @click="onClickDrop">选择文件</button>
        <button class="btn" :disabled="detecting" @click="openBrowser">浏览目录</button>
      </template>
      <span v-if="detecting" class="detect-tip">识别中…</span>
    </div>

    <!-- 目录浏览器（浏览器模式） -->
    <div v-if="showBrowser" class="browser">
      <!-- 盘符快捷栏：一键换盘（不用一层层进到盘根） -->
      <div class="drive-bar" v-if="drives.length">
        <span class="drive-label">盘符</span>
        <button v-for="d in drives" :key="d" class="drive" :class="{ active: browserPath.replace(/\\\\$/,'').toLowerCase() === d.replace(/\\\\$/,'').toLowerCase() }"
                @click="loadDirs(d)">{{ d }}</button>
      </div>
      <div class="browser-head">
        <!-- 路径可编辑：粘贴路径后回车/失焦跳转（用户反馈不能复制粘贴链接） -->
        <input class="cur-input" v-model="browserPath" placeholder="粘贴路径后回车跳转…"
               @keyup.enter="loadDirs(browserPath)" @blur="loadDirs(browserPath)" />
        <span class="spacer"></span>
        <button class="btn" :disabled="browsing" @click="goUp">上级</button>
        <button class="btn primary" :disabled="browsing" @click="pickPath">选择</button>
        <button class="btn" @click="showBrowser = false">关闭</button>
      </div>
      <ul class="browser-list" v-if="dirs.length">
        <li v-for="d in dirs" :key="d">
          <button class="dir" @click="enterDir(d)">📁 {{ d }}</button>
        </li>
      </ul>
      <p v-else class="tip">该目录下没有可进入的子目录</p>
    </div>

    <!-- 任务列表 -->
    <div class="job-list" v-if="jobs.length">
      <div class="job-head">
        <span class="job-title">任务列表（{{ jobs.length }}）</span>
        <span class="spacer"></span>
        <button class="btn mini" :disabled="processing" @click="emit('clear-jobs')">清空</button>
      </div>
      <div v-for="(job, i) in jobs" :key="job.path + '-' + i" class="job-row" :class="'st-' + job.status">
        <span class="job-icon">{{ STATUS_ICON[job.status] || '•' }}</span>
        <span class="job-name" :title="job.path">{{ job.name }}</span>
        <span class="job-kind">{{ KIND_TEXT[job.kind] || (job.kind || '未知') }}</span>
        <span class="job-status">{{ STATUS_TEXT[job.status] || job.status }}</span>
        <button class="btn mini x" :disabled="job.status === 'running'" :title="'移除'" @click="emit('remove-job', i)">✕</button>
      </div>
      <p v-if="failCountTip" class="tip fail-tip">失败项可在右侧查看原因，也可移除后重新添加</p>
    </div>
    <p v-else class="tip empty-tip">还没有任务，拖入文件或选择目录开始</p>

    <p v-if="error" class="err">{{ error }}</p>

    <!-- 开始翻译：串行队列 -->
    <div class="actions">
      <button class="btn primary start-btn" :disabled="!startEnabled" @click="emit('start-queue')">
        {{ processing ? '翻译中…' : `开始翻译 · ${pendingCount} 个` }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.scan-panel { max-width: 100%; padding: 0; }

.pick-row { display: flex; gap: 10px; align-items: center; margin-bottom: 16px; }
.detect-tip { color: var(--text-dim); font-size: 12px; }

/* 大拖放区 */
.dropzone {
  border: 2px dashed var(--line);
  border-radius: 10px;
  padding: 34px 20px;
  text-align: center;
  background: var(--bg-2);
  color: var(--text-dim);
  cursor: pointer;
  transition: all .15s;
  margin-bottom: 16px;
}
.dropzone:hover { border-color: var(--accent-2); }
.dropzone.drag { border-color: var(--accent); background: var(--bg-3); color: var(--accent); }
.dropzone .big { margin: 0 0 4px; font-size: 15px; }
.dropzone .small { margin: 0; font-size: 12px; }

/* 目录浏览器 */
.browser {
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-2);
  padding: 12px;
  margin-bottom: 16px;
}
.browser-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
/* 盘符快捷栏：一键换盘 */
.drive-bar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }
.drive-label { color: var(--text-dim); font-size: 12px; }
.drive {
  background: var(--bg-3); border: 1px solid var(--line); border-radius: 5px;
  color: var(--text); padding: 3px 10px; cursor: pointer; font-size: 12px;
}
.drive:hover { border-color: var(--accent); color: var(--accent); }
.drive.active { background: var(--accent-2); border-color: var(--accent-2); color: #0b1510; }
/* 路径可编辑：粘贴后回车/失焦跳转 */
.cur-input {
  flex: 1; background: var(--bg-3); border: 1px solid var(--line); border-radius: 5px;
  color: var(--text); padding: 5px 8px; font-size: 12px; outline: none; min-width: 120px;
}
.cur-input:focus { border-color: var(--accent); }
.spacer { flex: 1; }
.browser-list { list-style: none; margin: 0; padding: 0; max-height: 220px; overflow: auto; }
.browser-list .dir {
  display: block; width: 100%; text-align: left;
  background: transparent; border: none; color: var(--text);
  padding: 6px 8px; border-radius: 5px; cursor: pointer;
}
.browser-list .dir:hover { background: var(--bg-3); }

/* 任务列表 */
.job-list { margin-bottom: 8px; }
.job-head { display: flex; align-items: center; margin-bottom: 6px; }
.job-title { color: var(--text-dim); font-size: 13px; font-weight: 600; }
.job-row {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg-2); margin-bottom: 6px;
}
.job-icon { width: 16px; text-align: center; flex-shrink: 0; color: var(--text-dim); }
.job-row.st-running { border-color: var(--accent); }
.job-row.st-running .job-icon { color: var(--accent); }
.job-row.st-done { border-color: var(--accent-2); }
.job-row.st-done .job-icon { color: var(--accent); }
.job-row.st-failed { border-color: var(--danger); }
.job-row.st-failed .job-icon { color: var(--danger); }
.job-name {
  flex: 1; min-width: 0; font-size: 13px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.job-kind { color: var(--text-dim); font-size: 12px; flex-shrink: 0; }
.job-status { font-size: 12px; flex-shrink: 0; color: var(--text-dim); }
.job-row.st-running .job-status { color: var(--accent); }
.job-row.st-done .job-status { color: var(--accent); }
.job-row.st-failed .job-status { color: var(--danger); }

.btn.mini { padding: 3px 8px; font-size: 12px; flex-shrink: 0; }
.btn.mini.x { padding: 2px 7px; }

.fail-tip { margin-top: 6px; font-size: 12px; }
.empty-tip { margin-top: 4px; }

.start-btn { width: 100%; }
</style>
