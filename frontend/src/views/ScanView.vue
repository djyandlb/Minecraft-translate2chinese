<script setup>
import { computed, ref } from 'vue'
import { autoTranslate, browse, detect, uploadFile } from '../api'

// props：targetLang（来自 App 配置）+ onTranslate(taskId) / onBack()
const props = defineProps({
  targetLang: { type: String, default: 'zh_cn' },
  onTranslate: Function,
  onBack: Function,
})

const path = ref('')
const result = ref(null)          // /api/detect 返回 {kind, source_lang, pack_format, summary}
const detecting = ref(false)
const uploading = ref(false)
const translating = ref(false)
const error = ref('')

// —— 硬编码候选选择（业界 MIT 模式：全选/反选/搜索过滤/每项勾选）——
const hardCandidates = ref([])         // [{text, occurrences}]，来自 summary.hardcoded_candidates
const selectedHard = ref(new Set())    // 选中集合，默认全选
const hardSearch = ref('')
// 搜索过滤只影响显示，不影响选择
const filteredHard = computed(() => {
  const q = hardSearch.value.trim().toLowerCase()
  if (!q) return hardCandidates.value
  return hardCandidates.value.filter(c => c.text.toLowerCase().includes(q))
})
const allHardChecked = computed(() =>
  hardCandidates.value.length > 0 && filteredHard.value.every(c => selectedHard.value.has(c.text))
)
function toggleHard(text) {
  const next = new Set(selectedHard.value)
  if (next.has(text)) next.delete(text); else next.add(text)
  selectedHard.value = next
}
function toggleAllHard() {
  // 全选/取消 作用于当前搜索过滤后的可见项
  const texts = filteredHard.value.map(c => c.text)
  const next = new Set(selectedHard.value)
  if (allHardChecked.value) texts.forEach(t => next.delete(t))
  else texts.forEach(t => next.add(t))
  selectedHard.value = next
}
function invertHard() {
  // 反选 当前过滤后的可见项
  const next = new Set(selectedHard.value)
  for (const c of filteredHard.value) {
    if (next.has(c.text)) next.delete(c.text); else next.add(c.text)
  }
  selectedHard.value = next
}

// —— 目录浏览器（跨盘浏览复用现有 browser，盘根可列盘符）——
const showBrowser = ref(false)
const browserPath = ref('')
const dirs = ref([])
const parent = ref('')
const browsing = ref(false)

async function openBrowser() {
  showBrowser.value = true
  await loadDirs(path.value || '')
}
async function loadDirs(p) {
  browsing.value = true
  try {
    const r = await browse(p)
    // 首次（p 为空）时 browse('') 返回的 parent 即 home 绝对路径，
    // 始终用绝对路径作为当前目录，后续点入子目录拼出的子路径才不偏位
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
// 选中目录后收浏览器并立即自动识别
async function pickPath() {
  path.value = browserPath.value
  showBrowser.value = false
  await autoDetect()
}

// —— 拖放 / 点选上传 ——
const dragOver = ref(false)
const fileInput = ref(null)
function onDragOver(e) { e.preventDefault(); dragOver.value = true }
function onDragLeave(e) {
  // F13-review：子元素间移动会误触发 dragleave 导致高亮闪断；仅离开整个 dropzone 才熄灭
  if (e.currentTarget.contains(e.relatedTarget)) return
  dragOver.value = false
}
async function onDrop(e) {
  e.preventDefault(); dragOver.value = false
  const file = e.dataTransfer.files?.[0]
  if (!file) return
  await handleFile(file)
}
function onClickDrop() { fileInput.value?.click() }
async function onFilePicked(e) {
  const file = e.target.files?.[0]
  if (file) await handleFile(file)
  e.target.value = ''     // 允许连续选择同一文件
}
// 统一上传入口：POST /api/upload → 拿到落盘路径 → 自动识别
async function handleFile(file) {
  uploading.value = true; error.value = ''
  try {
    const r = await uploadFile(file)
    path.value = r.path
    await autoDetect()
  } catch (err) {
    error.value = `上传失败：${err.message}`
  } finally {
    uploading.value = false
  }
}

// —— 自动识别 ——
async function autoDetect() {
  if (!path.value) { error.value = '请选择路径或拖入文件'; return }
  detecting.value = true; error.value = ''
  result.value = null   // F13-review：识别前清空旧摘要，防失败残留导致「摘要与翻译对象不一致」
  hardCandidates.value = []
  selectedHard.value = new Set()
  hardSearch.value = ''
  try {
    result.value = await detect({ path: path.value, target_lang: props.targetLang })
    // 初始化硬编码候选：存在候选列表时默认全选
    const cands = result.value.summary?.hardcoded_candidates || []
    hardCandidates.value = cands
    selectedHard.value = new Set(cands.map(c => c.text))
    if (result.value.kind === 'unknown') {
      error.value = '无法识别输入类型，请确认是整合包目录 / mod jar / 地图存档'
    }
  } catch (e) {
    error.value = `识别失败：${e.message}`
  } finally {
    detecting.value = false
  }
}

// —— 一键全部翻译（语言文件 + 硬编码并入，产物资源包 + 汉化 jar）——
async function startTranslate() {
  if (!result.value || result.value.kind === 'unknown') { error.value = '请先完成识别'; return }
  translating.value = true; error.value = ''
  try {
    const body = { path: path.value, target_lang: props.targetLang }
    // 有硬编码候选时把用户勾选集合传给后端（只翻选中项；全取消则一个硬编码都不翻）
    if (hardCandidates.value.length > 0) {
      body.selected_hardcoded = [...selectedHard.value]
    }
    const r = await autoTranslate(body)
    props.onTranslate?.(r.task_id)
  } catch (e) {
    error.value = `启动翻译失败：${e.message}`
  } finally {
    translating.value = false
  }
}

const KIND_TEXT = { modpack: '整合包', modjar: '单个 mod', map: '地图存档' }
</script>

<template>
  <section class="panel">
    <h2>② 汉化</h2>
    <p class="hint">拖入 mod / 整合包 / 地图文件，或选择目录 → 自动识别 → 一键翻译</p>

    <!-- 大拖放区：拖入 jar/压缩包/地图文件，或点击选文件 -->
    <div class="dropzone" :class="{ drag: dragOver }"
         @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop" @click="onClickDrop">
      <p class="big">{{ uploading ? '上传中…' : (dragOver ? '松手，放这里！' : '拖入 jar / 压缩包 / 地图文件') }}</p>
      <p class="small">或点击选择文件（整合包目录请用下方目录浏览）</p>
      <input ref="fileInput" type="file" style="display:none" @change="onFilePicked" />
    </div>

    <!-- 路径输入 + 目录浏览（跨盘） -->
    <div class="field">
      <label>资源路径</label>
      <div class="path-row">
        <input type="text" v-model="path" placeholder="选择整合包目录 / mod jar / 地图存档，或拖入文件" />
        <button class="btn" @click="openBrowser">浏览</button>
      </div>
    </div>

    <div v-if="showBrowser" class="browser">
      <div class="browser-head">
        <span class="cur">{{ browserPath || '（主目录）' }}</span>
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

    <!-- 识别按钮 -->
    <div class="actions detect-actions">
      <button class="btn" @click="autoDetect" :disabled="detecting || !path">
        {{ detecting ? '识别中…' : '自动识别' }}
      </button>
    </div>

    <!-- 识别结果摘要 -->
    <div v-if="result && result.kind !== 'unknown'" class="result-box">
      <h3>识别结果</h3>
      <div class="detect-grid">
        <div class="kv"><span>类型</span><strong>{{ KIND_TEXT[result.kind] || result.kind }}</strong></div>
        <div class="kv" v-if="result.kind !== 'map'">
          <span>源语言</span>
          <strong>{{ result.source_lang || '未检测到（可能已汉化）' }}</strong>
        </div>
        <div class="kv" v-if="result.kind !== 'map'">
          <span>资源包格式</span>
          <strong>{{ result.pack_format > 0 ? result.pack_format : '自动' }}</strong>
        </div>
      </div>
      <template v-if="result.summary">
        <p class="total">
          共 <strong>{{ result.summary.jar_count }}</strong> 个 jar、
          <strong>{{ result.summary.total_lang_files }}</strong> 个语言文件，
          可翻译词条约 <strong>{{ result.summary.total_entries }}</strong> 条
        </p>
        <p v-if="result.summary.total_hardcoded != null" class="total">
          硬编码字符串约 <strong>{{ result.summary.total_hardcoded }}</strong> 条（一并汉化）
        </p>
      </template>
      <p v-else-if="result.kind === 'map'" class="total">地图存档：翻译时自动扫描并写入，产物为 .mcworld</p>

      <!-- 硬编码候选选择区（业界 MIT 模式）：全选/反选/搜索过滤/每项勾选，翻译只翻选中的 -->
      <div v-if="hardCandidates.length" class="hard-select">
        <div class="hard-head">
          <span class="hard-title">硬编码候选（{{ hardCandidates.length }} 条，按出现频率排序）</span>
          <span class="spacer"></span>
          <label class="mini-check">
            <input type="checkbox" :checked="allHardChecked" @change="toggleAllHard" />
            全选
          </label>
          <button class="btn small" @click="toggleAllHard">全选/取消</button>
          <button class="btn small" @click="invertHard">反选</button>
          <input type="text" v-model="hardSearch" placeholder="搜索过滤…" class="hard-search" />
        </div>
        <ul class="hard-list">
          <li v-for="c in filteredHard" :key="c.text">
            <label class="hard-item">
              <input type="checkbox" :checked="selectedHard.has(c.text)" @change="toggleHard(c.text)" />
              <span class="hard-text">{{ c.text }}</span>
              <span class="hard-occ">×{{ c.occurrences }}</span>
            </label>
          </li>
        </ul>
        <p v-if="!filteredHard.length" class="tip">没有匹配的候选</p>
      </div>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <div class="actions">
      <button class="btn" @click="props.onBack?.()">上一步</button>
      <button class="btn primary" :disabled="translating || !result || result.kind === 'unknown'"
              @click="startTranslate">
        {{ translating ? '启动中…' : '开始翻译' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.path-row { display: flex; gap: 10px; }
.path-row input { flex: 1; }

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
  margin-bottom: 20px;
}
.browser-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
.browser-head .cur { color: var(--accent); word-break: break-all; font-size: 13px; }
.spacer { flex: 1; }
.browser-list { list-style: none; margin: 0; padding: 0; max-height: 240px; overflow: auto; }
.browser-list .dir {
  display: block; width: 100%; text-align: left;
  background: transparent; border: none; color: var(--text);
  padding: 6px 8px; border-radius: 5px; cursor: pointer;
}
.browser-list .dir:hover { background: var(--bg-3); }

/* 识别结果摘要 */
.detect-actions { margin-top: 0; }
.result-box { margin-top: 4px; }
.result-box h3 { margin: 0 0 8px; font-size: 16px; }
.detect-grid { display: flex; gap: 24px; margin-bottom: 6px; flex-wrap: wrap; }
.kv { display: flex; flex-direction: column; gap: 2px; }
.kv span { color: var(--text-dim); font-size: 12px; }
.kv strong { color: var(--accent); font-size: 14px; }
.total { color: var(--text-dim); margin: 6px 0 0; }
.total strong { color: var(--accent); }

/* 硬编码候选选择区 */
.hard-select {
  margin-top: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--bg-2);
  padding: 10px;
}
.hard-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 6px; }
.hard-title { font-size: 13px; color: var(--text-dim); }
.btn.small { padding: 2px 8px; font-size: 12px; }
.mini-check { display: flex; align-items: center; gap: 4px; font-size: 12px; color: var(--text-dim); }
.hard-search { flex: 1; min-width: 140px; }
.hard-list {
  list-style: none; margin: 0; padding: 0;
  max-height: 220px; overflow: auto;   /* 候选多时滚动展示 */
  border-top: 1px solid var(--line);
}
.hard-item { display: flex; align-items: center; gap: 8px; padding: 4px 6px; border-radius: 4px; cursor: pointer; }
.hard-item:hover { background: var(--bg-3); }
.hard-text { flex: 1; word-break: break-all; font-size: 13px; }
.hard-occ { color: var(--text-dim); font-size: 12px; }
.hard-select .tip { color: var(--text-dim); font-size: 12px; margin: 6px 0 0; }
</style>
