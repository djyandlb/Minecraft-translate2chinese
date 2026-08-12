<script setup>
import { computed, ref } from 'vue'

// props：jobs（App 持有的任务队列，展示用）+ addPath/addUpload（App 注入的入队方法，可 await）
const props = defineProps({
  jobs: { type: Array, default: () => [] },
  processing: { type: Boolean, default: false },   // 队列处理中 → 禁用移除/清空/开始
  detecting: { type: Boolean, default: false },    // 有文件识别中 → 禁用添加
  addPath: { type: Function, required: true },     // 本地路径入队（内部 detect）
  addUpload: { type: Function, required: true },   // 上传文件入队（浏览器/无本地路径兜底）
  viewedTaskId: { type: String, default: '' },     // 右侧当前查看的任务（点击任务行切换）
  projects: { type: Array, default: () => [] },    // 未完成项目（断点续联，启动扫描临时文件显示）
})
const emit = defineEmits(['remove-job', 'clear-jobs', 'start-queue', 'select-job', 'delete-project', 'resume-project'])

// 桌面版（pywebview）检测：存在 window.pywebview → 可用 js_api 直接拿本地路径，不走上传
const isDesktop = typeof window !== 'undefined' && !!window.pywebview

const dragOver = ref(false)
const fileInput = ref(null)
const error = ref('')

const KIND_TEXT = { modpack: '整合包', modjar: '单个 mod', map: '地图存档', shader: '光影包' }
const STATUS_TEXT = { pending: '待翻译', running: '翻译中…', done: '完成 ✓', failed: '失败 ✗' }
const STATUS_ICON = { pending: '•', running: '◎', done: '✓', failed: '✗' }

const pendingCount = computed(() => props.jobs.filter(j => j.status === 'pending').length)
const startEnabled = computed(() => !props.processing && pendingCount.value > 0)
const failCountTip = computed(() => props.jobs.some(j => j.status === 'failed'))

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
// 桌面版能拿本地路径 → 直接本地识别；拿不到 → 弹系统目录选择框
async function handleFolderDrop(file) {
  const localPath = isDesktop && file && file.path
  if (localPath) await props.addPath(localPath)   // 目录路径 → 后端 detect 识别为整合包
  else if (isDesktop) {
    // pywebview 对拖入文件夹的 File.path 可能不可用 → 顺手弹系统目录选择框
    error.value = '拖入的文件夹无法取得本地路径，已弹出系统目录选择框'
    await pickLocal('folder')
  } else {
    // 浏览器拖入文件夹只得到一个空 File（无 webkitdirectory）→ 提示走目录浏览
    error.value = '浏览器不支持拖入文件夹，请改拖入整合包压缩包（zip）'
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
    // 目录判定：Chromium 中拖入的文件夹表现为 File（type='' 且 size=0）。
    // 修复（recheck）：0 字节且 type 为空的真实文件（如无扩展名空文件）会被误判成文件夹——
    // 收紧为「无扩展名」才算目录（带扩展名的 0 字节空文件走文件上传）
    const isFolder = file.type === '' && file.size === 0 && !(file.name.split('.').length > 1)
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
    <p class="hint">拖入 mod / 整合包 / 地图 / 光影（支持多个，队列逐个翻译）</p>

    <div class="dropzone" :class="{ drag: dragOver }"
         @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop" @click="onZoneClick">
      <span class="upload-glyph" aria-hidden="true"><i></i><i></i><i></i><i></i><b></b></span>
      <p class="big">{{ dragOver ? '松手，即享自动翻译！' : '拖入 MOD / 整合包 / 地图 / 光影' }}</p>
      <p class="small">支持多个文件</p>
      <input ref="fileInput" type="file" multiple style="display:none" @change="onFilePicked" />
    </div>

    <!-- 只留大框：点击选文件 / 拖入文件 / 拖入文件夹自动填路径；识别中提示 -->
    <div v-if="detecting" class="detect-tip">识别中…</div>

    <!-- 未完成项目（断点续联，用户诉求：启动扫描临时文件直接显示，不用拖入） -->
    <div v-if="projects.length" class="proj-list">
      <div class="proj-head"><span class="proj-title">未完成项目（可续联）</span></div>
      <div v-for="p in projects" :key="p.project_id" class="proj-row">
        <span class="proj-icon">◎</span>
        <div class="proj-copy">
          <span class="proj-name" :title="p.project_id">{{ p.name }}</span>
          <span class="proj-meta" v-if="p.total > 0">已翻译 {{ p.done.toLocaleString() }}/{{ p.total.toLocaleString() }} · {{ p.pct }}%</span>
        </div>
        <span class="resume-tag" :title="`记录上次进度，可从断点继续`">可断点续联</span>
        <button v-if="p.path" class="btn mini" :title="'从上次进度继续翻译（自动命中记忆跳过已翻条目）'"
                @click.stop="emit('resume-project', p)">续联</button>
        <button class="btn mini x" :title="'删除项目（清理记忆/进度缓存）'"
                @click.stop="emit('delete-project', p.project_id)">✕</button>
      </div>
      <p class="tip proj-tip">拖入对应整合包即自动续联（内容指纹匹配）；删除会清理该项目记忆/进度缓存</p>
    </div>

    <!-- 任务列表 -->
    <div class="job-list" v-if="jobs.length">
      <div class="job-head">
        <span class="job-title">任务列表（{{ jobs.length }}）</span>
        <span class="spacer"></span>
        <button class="btn mini" :disabled="processing || detecting" @click="emit('clear-jobs')">清空</button>
      </div>
      <div v-for="(job, i) in jobs" :key="job.path + '-' + i" class="job-row"
           :class="['st-' + job.status, { viewing: job.taskId && job.taskId === props.viewedTaskId }]"
           :title="job.taskId ? '点击查看该任务汉化明细' : ''"
           @click="job.taskId && emit('select-job', job.taskId)">
        <span class="job-icon">{{ STATUS_ICON[job.status] || '•' }}</span>
        <div class="job-copy">
          <span class="job-name" :title="job.path">{{ job.name }}</span>
          <!-- 识别摘要（M1）：工作量预期——jar 数/待翻译词条/硬编码候选/源语言 -->
          <span v-if="job.detectResult && job.detectResult.summary" class="job-summary">
            <template v-if="job.detectResult.summary.jar_count > 0">{{ job.detectResult.summary.jar_count }} 个 jar</template><template v-if="job.detectResult.summary.total_entries > 0"> · 约 {{ Number(job.detectResult.summary.total_entries).toLocaleString() }} 条待翻译</template>
            <template v-if="job.detectResult.summary.total_hardcoded != null"> · 硬编码 {{ job.detectResult.summary.total_hardcoded }}</template>
          </span>
          <span v-else-if="job.detectResult && job.detectResult.source_lang" class="job-summary">源语言 {{ job.detectResult.source_lang }}</span>
          <!-- 断点续联标记：识别后即显示，**点击即续联**（用户诉求：不是死条目）。
               修复：任务完成/运行中隐藏——done 后仍显示与「完成 ✓」矛盾；running 时点续联会移除运行中任务。
               点击 @click.stop 阻止冒泡到任务行 select-job，直接触发续联。 -->
          <span v-if="job.detectResult?.resume?.available && job.status !== 'done' && job.status !== 'running'"
                class="resume-tag act" role="button" tabindex="0"
                :title="`点击续联：项目记忆 ${job.detectResult.resume.memory_count} 条${job.detectResult.resume.progress_pct != null ? `，上次进度 ${job.detectResult.resume.progress_pct}%` : ''}`"
                @click.stop="emit('resume-project', { path: job.path, name: job.name })">
            可断点续联{{ job.detectResult.resume.progress_pct != null ? ` · ${job.detectResult.resume.progress_pct}%` : '' }} → 点击续联
          </span>
        </div>
        <span class="job-kind">{{ KIND_TEXT[job.kind] || (job.kind || '未知') }}</span>
        <span class="job-status">{{ STATUS_TEXT[job.status] || job.status }}</span>
        <button class="btn mini x" :disabled="processing || job.status === 'running'" :title="'移除'" @click.stop="emit('remove-job', i)">✕</button>
      </div>
      <p v-if="failCountTip" class="tip fail-tip">失败项可在右侧查看原因，也可移除后重新添加</p>
    </div>
    <p v-else class="tip empty-tip">还没有任务，拖入文件开始</p>

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

.detect-tip { color: var(--text-dim); font-size: 12px; }

.dropzone {
  border: 2px dashed rgba(63, 101, 72, .25);
  border-radius: 0;
  padding: 36px 20px;
  text-align: center;
  background: var(--surface);
  color: var(--text-dim);
  cursor: pointer;
  transition: all .18s;
  margin-bottom: 16px;
}
.dropzone:hover, .dropzone.drag { border-color: var(--moss); background: rgba(63, 101, 72, .04); }
.dropzone.drag { color: var(--moss); }
.dropzone .big { margin: 10px 0 4px; font-size: 17px; color: var(--ink); font-weight: 700; }
.dropzone .small { margin: 0; font-size: 12px; }
.dropzone.drag .big { color: var(--moss); }
/* 上传图标（像素方块堆 + 像素下箭头） */
.upload-glyph { position: relative; width: 56px; height: 50px; display: inline-block; margin-top: 4px; }
.upload-glyph i { position: absolute; width: 12px; height: 12px; background: var(--accent); transition: transform .18s; }
.upload-glyph i:nth-child(1) { left: 22px; top: 0; }
.upload-glyph i:nth-child(2) { left: 8px; top: 16px; }
.upload-glyph i:nth-child(3) { right: 8px; top: 16px; }
.upload-glyph i:nth-child(4) { left: 22px; top: 16px; }
.upload-glyph b { position: absolute; left: 20px; top: 34px; width: 16px; height: 10px; background: var(--accent); }
.upload-glyph b::after { content: ""; position: absolute; left: -6px; top: -6px; border: 14px solid transparent; border-top-color: var(--accent); border-bottom: none; width: 0; height: 0; }
.dropzone:hover .upload-glyph i:nth-child(1), .dropzone.drag .upload-glyph i:nth-child(1) { transform: translateY(4px); }
.dropzone:hover .upload-glyph i:nth-child(4), .dropzone.drag .upload-glyph i:nth-child(4) { transform: translateY(4px); }
.dropzone:hover .upload-glyph i:nth-child(2), .dropzone.drag .upload-glyph i:nth-child(2) { transform: translate(-3px, 2px); }
.dropzone:hover .upload-glyph i:nth-child(3), .dropzone.drag .upload-glyph i:nth-child(3) { transform: translate(3px, 2px); }

/* 未完成项目（断点续联）：启动扫描临时文件直接显示 */
.proj-list { margin-bottom: 8px; }
.proj-head { display: flex; align-items: center; margin-bottom: 6px; }
.proj-title { color: var(--text-dim); font-size: 13px; font-weight: 600; }
.proj-row {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; border: 1px dashed var(--accent); border-radius: 0;
  background: rgba(63, 101, 72, .06); margin-bottom: 6px;
}
.proj-icon { width: 18px; text-align: center; flex-shrink: 0; color: var(--accent); }
.proj-copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 2px; }
.proj-name {
  min-width: 0; font-size: 13px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.proj-meta { color: var(--text-dim); font-size: 11px; font-variant-numeric: tabular-nums; }
.proj-tip { font-size: 11px; color: var(--text-dim); }

/* 任务列表 */
.job-list { margin-bottom: 8px; }
.job-head { display: flex; align-items: center; margin-bottom: 6px; }
.job-title { color: var(--text-dim); font-size: 13px; font-weight: 600; }
.job-row {
  display: flex; align-items: center; gap: 10px;
  padding: 10px 12px; border: 1px solid transparent; border-radius: 0;
  background: rgba(32, 37, 29, .03); margin-bottom: 6px;
  transition: border-color .18s, background .18s;
  cursor: pointer;
}
.job-row:hover { border-color: var(--line); }
.job-row.viewing { border-color: var(--accent); background: rgba(63, 101, 72, .06); }
.job-icon { width: 18px; text-align: center; flex-shrink: 0; color: var(--text-dim); }
.job-row.st-running { border-color: rgba(63, 101, 72, .28); background: rgba(63, 101, 72, .06); }
.job-row.st-running .job-icon { color: var(--moss); animation: item-pulse 1.2s ease-in-out infinite; }
.job-row.st-done { border-color: rgba(63, 101, 72, .28); }
.job-row.st-done .job-icon { color: var(--moss); }
.job-row.st-failed { border-color: rgba(185, 95, 61, .5); }
.job-row.st-failed .job-icon { color: var(--rust); }
@keyframes item-pulse { 50% { opacity: .45; } }
/* 任务行：名称 + 识别摘要（mod 工作量预期） */
.job-copy { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.job-name {
  min-width: 0; font-size: 13px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.job-summary { color: var(--text-dim); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
/* 断点续联标记：绿色小标签（识别后即显示） */
.resume-tag {
  display: inline-block; width: fit-content; font-size: 11px; padding: 1px 6px;
  background: rgba(63, 101, 72, .14); border: 1px solid var(--accent); color: var(--accent);
  cursor: default;
}
/* 可点击续联：hover 反色提示可操作 */
.resume-tag.act { cursor: pointer; text-decoration: underline; }
.resume-tag.act:hover { background: var(--accent); color: #f5f1e6; }
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
