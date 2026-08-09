<script setup>
import { ref } from 'vue'
import { browse, mapScan, mapTranslate, scan, translate } from '../api'

// props：sourceLang/targetLang/mcVersion（来自配置步）、onTranslate(taskId)、onBack()
const props = defineProps({
  sourceLang: { type: String, default: 'en_us' },
  targetLang: { type: String, default: 'zh_cn' },
  mcVersion: { type: String, default: '1.20.1' },
  onTranslate: Function,
  onBack: Function,
})

const mode = ref('modpack')          // modpack 整合包目录 / jar 单文件
const scope = ref('mods')            // 整合包模式下：mods 模组 / all 全部
const path = ref('')

// 目录浏览器
const showBrowser = ref(false)
const browserPath = ref('')
const dirs = ref([])
const parent = ref('')
const browsing = ref(false)

const result = ref(null)             // { mods:[{modid,entries,gaps}], total_gaps }
const scanning = ref(false)
const translating = ref(false)
const error = ref('')

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
  loadDirs(browserPath.value ? `${browserPath.value}/${name}` : name)
}
function goUp() {
  if (parent.value) loadDirs(parent.value)
}
function pickPath() {
  path.value = browserPath.value
  showBrowser.value = false
}

async function startScan() {
  if (!path.value) { error.value = '请先选择路径' ; return }
  scanning.value = true
  error.value = ''
  try {
    // 地图存档走 map-scan（返回 {entries, preview}），其余走 scan
    result.value = mode.value === 'map'
      ? await mapScan({
          path: path.value,
          source_lang: props.sourceLang,
          target_lang: props.targetLang,
        })
      : await scan({
          path: path.value,
          mode: mode.value,
          scope: scope.value,
          source_lang: props.sourceLang,
          target_lang: props.targetLang,
        })
  } catch (e) {
    error.value = `扫描失败：${e.message}`
  } finally {
    scanning.value = false
  }
}

async function startTranslate() {
  if (!result.value) { error.value = '请先完成扫描' ; return }
  translating.value = true
  error.value = ''
  try {
    // 地图存档走 map-translate（后台复制→翻译→写回→mcworld），其余走 translate
    const r = mode.value === 'map'
      ? await mapTranslate({
          path: path.value,
          source_lang: props.sourceLang,
          target_lang: props.targetLang,
        })
      : await translate({
          path: path.value,
          mode: mode.value,
          scope: scope.value,
          source_lang: props.sourceLang,
          target_lang: props.targetLang,
          mc_version: props.mcVersion,
        })
    props.onTranslate?.(r.task_id)
  } catch (e) {
    error.value = `启动翻译失败：${e.message}`
  } finally {
    translating.value = false
  }
}
</script>

<template>
  <section class="panel">
    <h2>② 扫描</h2>
    <p class="hint">选择要翻译的资源，扫描空缺词条</p>

    <div class="field">
      <label>输入方式</label>
      <div class="radio-row">
        <label class="radio"><input type="radio" value="modpack" v-model="mode" /> 整合包目录</label>
        <label class="radio"><input type="radio" value="jar" v-model="mode" /> 单个 jar 文件</label>
        <label class="radio"><input type="radio" value="map" v-model="mode" /> 地图存档</label>
      </div>
    </div>

    <div class="field">
      <label>资源路径</label>
      <div class="path-row">
        <input type="text" v-model="path"
               :placeholder="mode === 'map' ? '选择世界存档目录（含 level.dat）' : '选择整合包目录或 jar 文件'" />
        <button class="btn" @click="openBrowser">浏览</button>
      </div>
    </div>

    <div class="field" v-if="mode === 'modpack'">
      <label>扫描范围</label>
      <div class="radio-row">
        <label class="radio"><input type="radio" value="mods" v-model="scope" /> 仅模组目录</label>
        <label class="radio"><input type="radio" value="all" v-model="scope" /> 全部资源</label>
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

    <!-- 扫描结果表：整合包 / jar 模式 -->
    <div v-if="result && mode !== 'map'" class="result-box">
      <h3>扫描结果</h3>
      <table>
        <thead>
          <tr><th>模组 ID</th><th>词条数</th><th>空缺数</th></tr>
        </thead>
        <tbody>
          <tr v-for="m in result.mods" :key="m.modid">
            <td>{{ m.modid }}</td>
            <td>{{ m.entries }}</td>
            <td>{{ m.gaps }}</td>
          </tr>
        </tbody>
      </table>
      <p class="total">空缺词条总数：<strong>{{ result.total_gaps }}</strong></p>
    </div>

    <!-- 扫描结果表：地图存档模式（entries + 预览前 50 条） -->
    <div v-else-if="result" class="result-box">
      <h3>扫描结果</h3>
      <p class="total">可翻译词条数：<strong>{{ result.entries }}</strong></p>
      <table v-if="result.preview && result.preview.length">
        <thead>
          <tr><th>文件</th><th>原文</th></tr>
        </thead>
        <tbody>
          <tr v-for="(e, i) in result.preview" :key="i">
            <td class="fname">{{ e.file }}</td>
            <td>{{ e.text }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="tip">未扫描到可翻译词条</p>
    </div>

    <p v-if="error" class="err">{{ error }}</p>

    <div class="actions">
      <button class="btn" @click="props.onBack?.()">上一步</button>
      <button class="btn" :disabled="scanning" @click="startScan">
        {{ scanning ? '扫描中…' : '开始扫描' }}
      </button>
      <button class="btn primary" :disabled="translating || !result" @click="startTranslate">
        {{ translating ? '启动中…' : '开始翻译' }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.path-row { display: flex; gap: 10px; }
.path-row input { flex: 1; }

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

.result-box { margin-top: 4px; }
.result-box h3 { margin: 0 0 4px; font-size: 16px; }
.total { color: var(--text-dim); margin: 10px 0 0; }
.total strong { color: var(--accent); }
.fname { word-break: break-all; font-size: 12px; color: var(--text-dim); max-width: 300px; }
</style>
