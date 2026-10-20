<script setup lang="ts">
import { ref, computed, onMounted, onBeforeUnmount, reactive, watch, nextTick } from 'vue'
import { FileSpreadsheet, Database, History, Search, Download, ChevronLeft, ChevronRight, ChevronDown, ArrowRight,
  Check, AlertCircle, RefreshCw, X, Plus, CheckCircle2, Clock3, PanelRightClose, PanelRightOpen, ExternalLink, Minus, Scan, SlidersHorizontal } from 'lucide-vue-next'
import VoucherTable from './VoucherTable.vue'
import { rateMatrix, scalar } from './rates'
import { api, clone, amount, type Draft, type Issue } from './api'

const page = ref('workbench'), meta = ref<any>({}), catalogs = ref<Record<string, any[]>>({})
const reviewMode = ref(false), reviewSearch = ref(false), scopeOpen = ref(false)
const zoom = ref(1), autoFit = ref(true), canvasElement = ref<HTMLElement | null>(null)
let canvasObserver: ResizeObserver | undefined
function fitPaper() {
  if (autoFit.value && canvasElement.value) zoom.value = Math.min(1, Math.max(.5, (canvasElement.value.clientWidth - 40) / 1200))
}
function changeZoom(delta: number) { autoFit.value = false; zoom.value = Math.min(1.4, Math.max(.5, Math.round((zoom.value + delta) * 100) / 100)) }
watch(canvasElement, element => { canvasObserver?.disconnect(); if (element) { canvasObserver = new ResizeObserver(fitPaper); canvasObserver.observe(element); fitPaper() } })
watch(reviewMode, () => nextTick(fitPaper))
const mode = ref('pending'), startDate = ref('2026-01-01'), endDate = ref(''), postingDate = ref('')
const batches = ref<any[]>([]), batchId = ref(''), drafts = ref<Draft[]>([]), selectedId = ref('')
const query = ref(''), statusFilter = ref('all'), error = ref(''), notice = ref('')
const tasks = ref<any[]>([]), activeTask = ref<any>(null), busy = ref(false)
const snapshot = ref<any>(null), selectedGroups = ref<string[]>([]), showRange = ref(false)
const exportComplete = ref<any>(null)
function downloadExport(entry: any) {
  const link = document.createElement('a')
  link.href = '/api/exports/' + encodeURIComponent(entry.id) + '/download'
  link.download = entry.filename || '用友凭证.xls'
  document.body.appendChild(link); link.click(); link.remove()
}
const batchMenu = ref<HTMLDetailsElement | null>(null)
const recentBatches = computed(() => [...batches.value].sort((a,b) => b.created_at.localeCompare(a.created_at)).slice(0, 20))
const batchLabel = computed(() => {
  const batch = batches.value.find(b => b.id === batchId.value)
  return batch ? batch.created_at.slice(0,16).replace('T',' ') + ' · ' + batch.draft_ids.length + '张' : '选择已保存批次'
})
function dismissBatch(event: Event) { if (!batchMenu.value?.contains(event.target as Node)) batchMenu.value?.removeAttribute('open') }
async function chooseBatch(id: string) { batchMenu.value?.removeAttribute('open'); await loadBatch(id) }
async function moreBatches() { batchMenu.value?.removeAttribute('open'); await switchPage('exports') }
const archiveQuery = ref(''), archiveIndex = ref(0)
const archiveDrafts = computed(() => (exportDetail.value?.drafts || []).filter((d: Draft) => (d.contracts.join(' ') + d.customs_no).toUpperCase().includes(archiveQuery.value.trim().toUpperCase())))
const archiveCurrent = computed(() => archiveDrafts.value[archiveIndex.value])
watch(archiveQuery, () => { archiveIndex.value = 0 })
async function openArchive(id: string) { exportDetail.value = await api('/exports/' + id); archiveQuery.value = ''; archiveIndex.value = 0 }
const exportPreview = ref<any>(null), exports = ref<any[]>([]), exportDetail = ref<any>(null)
const showInspector = ref(false), inspectorTab = ref('issues'), sourceIndex = ref(0)
const history = ref<any[] | null>(null), historyTitle = ref('')
const mappings = ref<any>({ version: 1, items: {} }), mapKind = ref('inventory'), mapQuery = ref('')
const mapEdit = ref<any>(null), mapError = ref('')
const referenceEdit = ref<any>(null), referenceError = ref(''), referenceVersion = ref(1), referenceSaving = ref(false)
const auxOptions = computed(() => [...new Map((catalogs.value.aux || []).flatMap(r => r.auxiliary_fields).map(f => [f.code, f])).values()])
function grouped(rows: any[], kind: string) {
  const groups = new Map<string, any[]>()
  for (const row of rows) {
    const archive = row.platform_code ? catalogs.value[kind]?.find(r => String(r.code) === row.platform_code) : row
    const group = archive?.group_name || '未分组'
    if (!groups.has(group)) groups.set(group, [])
    groups.get(group)!.push(row)
  }
  return [...groups].sort(([a], [b]) => a === '未分组' ? 1 : b === '未分组' ? -1 : a.localeCompare(b, 'zh')).map(([name, rows]) => ({name, rows}))
}
function openReference(row: any) { referenceError.value = ''; referenceEdit.value = clone(row) }
const rateChoices = ref<any[]>([])
function openRate(period = '', currency = '') {
  const [year, month] = period.split('-')
  rateChoices.value = (catalogs.value.rates || []).filter(r => scalar(r.fields['币种']) === currency && scalar(r.fields['实际年份']) === year && Number(scalar(r.fields['实际月份'])) === Number(month))
  const existing = rateChoices.value[0]
  openReference(existing ? {...existing, fields: {...existing.fields, 币种: scalar(existing.fields['币种'])}} : {fields: {实际年份: year || String(new Date().getFullYear()), 实际月份: month || '1', 币种: currency || 'USD', 汇率: '', 数据来源: '本地维护'}})
}
function selectRate(id: string) {
  const row = rateChoices.value.find(r => (r.record_id || r.id) === id)
  if (row) openReference({...row, fields: {...row.fields, 币种: scalar(row.fields['币种'])}})
}
function toggleAux(option: any, checked: boolean) {
  const fields = referenceEdit.value.auxiliary_fields
  referenceEdit.value.auxiliary_fields = checked ? [...fields, {...option, mustinput: false}] : fields.filter((f: any) => f.code !== option.code)
}
async function saveReference() {
  referenceSaving.value = true; referenceError.value = ''
  try {
    const row = referenceEdit.value
    await api('/catalogs/' + mapKind.value, 'PUT', {version: referenceVersion.value, key: mapKind.value === 'rates' ? row.record_id || row.id : mapKind.value === 'aux' ? row.account_code : row.code, item: row})
    referenceEdit.value = null
    await loadMappings()
    drafts.value.forEach(d => { d.mapping_stale = true })
    toast('数据源已保存；已有草稿可选择重新计算')
  } catch (e: any) { referenceError.value = e.message } finally { referenceSaving.value = false }
}
const conflict = ref<{ local: Draft; remote: Draft } | null>(null)
const turnDirection = ref(1), paperElement = ref<HTMLElement | null>(null), savePulse = ref(false)
const dirty = reactive(new Set<string>()), saveErrors = reactive(new Map<string, string>())
const saving = reactive(new Set<string>())
const inflight = new Map<string, Promise<void>>(), timers = new Map<string, ReturnType<typeof setTimeout>>()
let disposed = false, touchX = 0
const current = computed(() => drafts.value.find(d => d.id === selectedId.value))
const filtered = computed(() => {
  const q = query.value.toUpperCase().replace(/[-_\s]/g, '')
  return drafts.value.filter(d => (!q || (d.search_index + d.customs_no).toUpperCase().replace(/[-_\s]/g, '').includes(q))
    && (statusFilter.value === 'all' || (statusFilter.value === 'valid' ? d.valid : !d.valid)))
    .sort((a, b) => Number(a.valid) - Number(b.valid))
})
const index = computed(() => filtered.value.findIndex(d => d.id === selectedId.value))
watch([query, statusFilter], () => {
  if (!filtered.value.some(d => d.id === selectedId.value)) {
    selectedId.value = filtered.value[0]?.id || ''
    sourceIndex.value = 0
  }
})
const validCount = computed(() => drafts.value.filter(d => d.valid && !dirty.has(d.id) && !saving.has(d.id)).length)
const mapLabels: Record<string, string> = { inventory: '产品存货', suppliers: '供应商', customers: '客户', subjects: '会计科目', aux: '辅助核算', rates: '汇率' }
const mappingRows = computed(() => (mappings.value.items[mapKind.value] || []).filter((r: any) => `${r.alias} ${r.platform_code} ${r.platform_name}`.toLowerCase().includes(mapQuery.value.toLowerCase())))
const referenceRows = computed(() => (catalogs.value[mapKind.value] || []).filter((r: any) => JSON.stringify(r).toLowerCase().includes(mapQuery.value.toLowerCase())))
const mappingGroups = computed(() => grouped(mappingRows.value, mapKind.value))
const referenceGroups = computed(() => grouped(referenceRows.value, mapKind.value))
const rates = computed(() => rateMatrix(catalogs.value.rates || [], mapQuery.value))
const isMapping = computed(() => ['inventory','suppliers','customers'].includes(mapKind.value))
const saveLabel = computed(() => {
  if (!current.value) return '草稿自动保存'
  if (saveErrors.has(current.value.id)) return saveErrors.get(current.value.id)
  return dirty.has(current.value.id) || saving.has(current.value.id) ? '保存中…' : '所有修改已保存'
})

function toast(message: string) { notice.value = message; setTimeout(() => { notice.value = '' }, 5000) }
async function guard(fn: () => Promise<void>) {
  error.value = ''
  try { await fn() } catch (e: any) { error.value = e.message || '操作失败，请重试' }
}
async function refreshLists() {
  [batches.value, exports.value, tasks.value, meta.value] = await Promise.all([api('/batches?workbench=true'), api('/exports'), api('/tasks'), api('/meta')])
}
async function loadBatch(id: string) {
  await flushAll()
  const batch = await api('/batches/' + id + '?workbench=true')
  query.value = ''; statusFilter.value = 'all'
  drafts.value = batch.drafts
  batchId.value = id
  localStorage.setItem('voucher-batch', id)
  selectedId.value = filtered.value[0]?.id || ''
  sourceIndex.value = 0
  for (const draft of drafts.value) {
    const backup = localStorage.getItem('voucher-draft-' + draft.id)
    if (backup) {
      const local: Draft = JSON.parse(backup)
      if (local.version === draft.version) {
        Object.assign(draft, local); dirty.add(draft.id)
        saveErrors.set(draft.id, '发现未保存修改，点击重试保存')
      } else {
        conflict.value = { local, remote: clone(draft) }
      }
    }
  }
}
async function runTask(kind: string, payload: any, existing?: any) {
  busy.value = true
  try {
    let task = existing || await api('/tasks', 'POST', { kind, payload, request_id: crypto.randomUUID() })
    activeTask.value = task
    while (!['done','failed'].includes(task.status)) {
      await new Promise(resolve => setTimeout(resolve, 700))
      if (disposed) return
      task = await api('/tasks/' + task.id); activeTask.value = task
    }
    await refreshLists()
    if (task.status === 'failed') throw new Error(task.message)
    if (task.kind === 'query') {
      snapshot.value = await api('/snapshots/' + task.result.snapshot_id)
      selectedGroups.value = snapshot.value.groups.filter((g: any) => !g.blocked.length).map((g: any) => g.key)
      showRange.value = true
    } else if (task.kind === 'build') {
      showRange.value = false
      await loadBatch(task.result.batch_id)
      toast('凭证草稿已构建，异常位置已标记')
    } else {
      await refreshLists()
      page.value = 'exports'
      exportPreview.value = null
      if (batchId.value) await loadBatch(batchId.value)
      if (task.kind === 'export') {
        const entry = exports.value.find(e => e.id === task.result.export_id) || await api('/exports/' + task.result.export_id)
        exportComplete.value = entry
        downloadExport(entry)
      } else toast('回写状态已更新')
    }
  } finally { busy.value = false }
}
function markDirty() {
  const d = current.value
  if (!d) return
  dirty.add(d.id)
  // Recalculate display totals immediately; server remains the export authority.
  const sum = (f: string) => d.rows.reduce((n, r) => n + Math.round((Number(r[f]) || 0) * 100), 0) / 100
  d.totals = { debit: sum('借方金额').toFixed(2), credit: sum('贷方金额').toFixed(2), difference: (sum('借方金额') - sum('贷方金额')).toFixed(2) }
  d.valid = false
  localStorage.setItem('voucher-draft-' + d.id, JSON.stringify(d))
  clearTimeout(timers.get(d.id))
  if (!saveErrors.has(d.id)) timers.set(d.id, setTimeout(() => { void save(d.id).catch(() => {}) }, 800))
}
async function save(id: string): Promise<void> {
  const pending = inflight.get(id)
  if (pending) { await pending; if (dirty.has(id) && !saveErrors.has(id)) await save(id); return }
  if (!dirty.has(id)) return
  const d = drafts.value.find(v => v.id === id)
  if (!d) return
  clearTimeout(timers.get(id))
  const work = (async () => {
    const sent = clone(d)
    saving.add(id)
    dirty.delete(id); saveErrors.delete(id)
    try {
      const result: Draft = await api('/drafts/' + id, 'PATCH', { version: sent.version, header: sent.header, rows: sent.rows, overrides: sent.overrides })
      if (dirty.has(id)) {
        d.version = result.version; d.issues = result.issues; d.manual_fields = result.manual_fields
        localStorage.setItem('voucher-draft-' + id, JSON.stringify(d))
      } else {
        Object.assign(d, result); localStorage.removeItem('voucher-draft-' + id)
        savePulse.value = false
        requestAnimationFrame(() => { savePulse.value = true; setTimeout(() => { savePulse.value = false }, 1000) })
      }
    } catch (e: any) {
      dirty.add(id); saveErrors.set(id, e.message)
      if (e.status === 409) conflict.value = { local: clone(d), remote: await api('/drafts/' + id) }
      throw e
    }
  })()
  inflight.set(id, work)
  try { await work } finally { inflight.delete(id); saving.delete(id) }
  if (dirty.has(id) && !saveErrors.has(id)) await save(id)
}
async function flushAll() {
  for (const id of [...new Set([...dirty, ...inflight.keys()])]) {
    if (saveErrors.has(id)) throw new Error('有修改尚未保存，请先重试保存或解决版本冲突')
    await save(id)
  }
}
function blurSave() { if (current.value && !saveErrors.has(current.value.id)) void save(current.value.id).catch(() => {}) }
function selectDraft(id: string) {
  if (id === selectedId.value) return
  turnDirection.value = drafts.value.findIndex(d => d.id === id) >= drafts.value.findIndex(d => d.id === selectedId.value) ? 1 : -1
  selectedId.value = id; sourceIndex.value = 0
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    paperElement.value?.animate([
      { opacity: .35, transform: `translateX(${turnDirection.value * 16}px) translateY(3px)` },
      { opacity: 1, transform: 'translateX(0) translateY(0)' }
    ], { duration: 280, easing: 'cubic-bezier(.2,.7,.2,1)' })
  }
}
function navigate(direction: number) {
  const d = filtered.value[index.value + direction]
  if (d) selectDraft(d.id)
}
function keyboard(e: KeyboardEvent) {
  if (e.key === 'Escape' && batchMenu.value?.open) { batchMenu.value.open = false; return }
  if (exportDetail.value) { if (e.key === 'Escape') exportDetail.value = null; return }
  if (e.key === 'Escape' && reviewMode.value && !showRange.value && !exportPreview.value && !conflict.value && !history.value) { reviewMode.value = false; return }
  if (page.value !== 'workbench' || showRange.value || exportPreview.value || conflict.value || history.value || exportComplete.value) return
  if ((e.target as HTMLElement).closest('input,textarea,select,[contenteditable]')) return
  if (e.key === 'ArrowLeft') navigate(-1)
  if (e.key === 'ArrowRight') navigate(1)
}
function unload(e: BeforeUnloadEvent) {
  if (dirty.size || inflight.size) { e.preventDefault(); e.returnValue = '' }
}
function locate(issue: Issue) {
  if (issue.row_id && issue.field) {
    const target = [...document.querySelectorAll<HTMLElement>('[data-target]')].find(e => e.dataset.target === issue.row_id + '/' + issue.field)
    target?.scrollIntoView({ behavior: 'smooth', block: 'center', inline: 'center' }); target?.focus()
  } else if (issue.record_id) {
    showInspector.value = true
    inspectorTab.value = 'source'
    sourceIndex.value = current.value?.source.findIndex(r => r.record_id === issue.record_id) || 0
  }
}
function sourceValue(field: string) {
  const d = current.value, r = d?.source[sourceIndex.value]
  if (!d || !r) return ''
  return d.overrides[r.record_id]?.[field] ?? (Array.isArray(r[field]) ? r[field].map((i: any) => i?.text || i).join(' ') : r[field] ?? '')
}
function updateSource(field: string, e: Event) {
  const d = current.value!, r = d.source[sourceIndex.value]!
  d.overrides[r.record_id] ||= {}
  d.overrides[r.record_id]![field] = (e.target as HTMLInputElement).value
  markDirty()
}
async function recalculate() {
  const d = current.value
  if (!d) return
  await flushAll()
  if (d.manual_fields.length && !window.confirm('重新计算会替换人工修改的分录，原版本会留在历史中。确定重新计算？')) return
  if (d.incoming_source && !window.confirm('发现飞书来源更新。确认采用最新来源重新计算？本地来源补充值会继续应用。')) return
  const result = await api('/drafts/' + d.id + '/recalculate', 'POST', { version: d.version, replace_manual: true, refresh_source: !!d.incoming_source })
  delete d.incoming_source
  Object.assign(d, result)
  toast('已根据本地来源补充与最新映射重新计算')
}
async function openExport() {
  await flushAll()
  exportPreview.value = await api('/exports/preview', 'POST', { draft_ids: drafts.value.map(d => d.id) })
}
async function resolveConflict(keepLocal: boolean) {
  const value = conflict.value!
  const draft = drafts.value.find(d => d.id === value.local.id)
  if (!draft) return
  Object.assign(draft, keepLocal ? value.local : value.remote)
  draft.version = value.remote.version
  saveErrors.delete(draft.id)
  conflict.value = null
  if (keepLocal) { dirty.add(draft.id); await save(draft.id) }
  else { dirty.delete(draft.id); localStorage.removeItem('voucher-draft-' + draft.id) }
}
async function openHistory(entity: string, title: string) {
  historyTitle.value = title
  history.value = await api('/history/' + encodeURIComponent(entity))
}
async function loadMappings() {
  const [maps, refs, revision] = await Promise.all([api('/mappings'), api('/catalogs'), api('/catalog-edits')])
  mappings.value = maps; catalogs.value = refs; referenceVersion.value = revision.version
  mapEdit.value = null
}
async function saveMap() {
  mapError.value = ''
  try {
    mappings.value = await api('/mappings/' + mapKind.value, 'PUT', { version: mappings.value.version, item: mapEdit.value })
    mapEdit.value = null
    drafts.value.forEach(d => { d.mapping_stale = true })
    toast('映射已保存，后续生成立即生效')
  } catch (e: any) { mapError.value = e.message }
}
async function switchPage(value: string) {
  page.value = value
  if (value === 'sources') await loadMappings()
  if (value === 'exports') exports.value = await api('/exports')
}
onMounted(() => guard(async () => {
  await refreshLists()
  catalogs.value = await api('/catalogs')
  postingDate.value = meta.value.today
  endDate.value = meta.value.today
  const previous = localStorage.getItem('voucher-batch')
  if (previous && batches.value.some(b => b.id === previous)) await loadBatch(previous)
  else if (batches.value.length) await loadBatch(batches.value[0].id)
  window.addEventListener('pointerdown', dismissBatch)
  window.addEventListener('keydown', keyboard)
  window.addEventListener('beforeunload', unload)
}))
onBeforeUnmount(() => {
  canvasObserver?.disconnect()
  disposed = true
  window.removeEventListener('pointerdown', dismissBatch)
  window.removeEventListener('keydown', keyboard); window.removeEventListener('beforeunload', unload)
  timers.forEach(clearTimeout)
})
</script>

<template>
  <div class="app-shell layout-002 theme-glass" :class="{ 'review-mode': reviewMode, 'workspace-page': page === 'workbench' }">
    <header class="topbar">
      <a class="brand" href="#" @click.prevent="page = 'workbench'"><span class="brand-mark">M<span>·</span></span><div><b>迈拓财务</b><small>FINANCE</small></div></a>
      <nav aria-label="主导航" :style="{ '--nav-index': ['workbench','sources','exports'].indexOf(page) }"><button aria-label="凭证工作台" title="凭证工作台" :class="{ active: page === 'workbench' }" @click="guard(() => switchPage('workbench'))"><FileSpreadsheet :size="17" />凭证工作台</button><button aria-label="数据源" title="数据源" :class="{ active: page === 'sources' }" @click="guard(() => switchPage('sources'))"><Database :size="17" />数据源</button><button aria-label="导出记录" title="导出记录" :class="{ active: page === 'exports' }" @click="guard(() => switchPage('exports'))"><History :size="17" />导出记录</button></nav>
      <div class="shared"><span v-if="meta.offline" class="demo-badge" title="使用本地快照，导出不会回写飞书">演示模式 · 不回写</span><span class="online-dot"></span>部门共享</div>
    </header>
    <div v-if="error" class="banner danger"><AlertCircle :size="16" />{{ error }}<button @click="error = ''"><X :size="16" /></button></div>
    <Transition name="toast"><div v-if="notice" class="toast"><CheckCircle2 :size="16" />{{ notice }}</div></Transition>

    <main v-if="page === 'workbench'" class="workbench">
      <section class="desk-command"><div><span class="desk-kicker">凭证工作台</span><h1>核对与出具 <span>{{ drafts.length }} 张</span></h1></div><div class="desk-actions"><button class="secondary" :aria-expanded="scopeOpen" @click="scopeOpen = !scopeOpen"><SlidersHorizontal :size="16" />选取范围</button><button class="primary" :disabled="!drafts.length || busy" @click="guard(openExport)"><Download :size="16" />导出 {{ validCount }} 张</button></div></section>
      <section v-show="scopeOpen" class="query-card">
        <div class="query-line"><div class="segmented" :style="{ '--mode-index': mode === 'pending' ? 0 : 1 }"><button :class="{ selected: mode === 'pending' }" @click="mode = 'pending'">飞书待开范围</button><button :class="{ selected: mode === 'date' }" @click="mode = 'date'">按日期查未开</button></div>
          <div v-if="mode === 'date'" class="date-range"><label>出口日期<input aria-label="开始日期" type="date" v-model="startDate"></label><span>—</span><input aria-label="结束日期" type="date" v-model="endDate"></div>
          <label class="posting-date">制单日期<input aria-label="制单日期" type="date" v-model="postingDate"></label>
          <button class="secondary query-submit" :disabled="busy || !postingDate" @click="guard(() => runTask('query', { mode, start_date: startDate, end_date: endDate }))"><Search :size="16" />检索记录</button>

        </div>
        <div class="source-strip"><span><Database :size="13" />{{ meta.source || '正在连接数据源' }}</span><span>26MT · 外销</span><span v-if="snapshot">更新于 {{ snapshot.created_at.replace('T',' ').slice(0,19) }}</span></div>
      </section>
      <Transition name="progress"><div v-if="busy" class="task-progress"><RefreshCw class="spin" :size="16" /><b>正在处理</b><span>{{ activeTask?.message }} · {{ activeTask?.progress || 0 }} {{ activeTask?.total ? '/ ' + activeTask.total : '条' }}</span><span>任务进度会自动保存</span><div class="task-track" :class="{ indeterminate: !activeTask?.total }"><i :style="{ width: activeTask?.total ? Math.min(100, (activeTask.progress / activeTask.total) * 100) + '%' : '35%' }"></i></div></div></Transition>
      <div v-if="tasks.some(t => ['failed','queued','running'].includes(t.status))" class="recover-bar">
        <span>可恢复任务</span><button v-for="t in tasks.filter(t => ['failed','queued','running'].includes(t.status)).slice(0,3)" :disabled="busy" @click="guard(async () => runTask(t.kind, {}, await api('/tasks/' + t.id + '/retry', 'POST')))">{{ ({query:'数据检索',build:'凭证构建',export:'文件导出',writeback:'飞书回写'} as any)[t.kind] }} · {{ t.status === 'failed' ? '重试' : '查看进度' }}</button>
      </div>
      <div v-if="reviewMode" class="review-header"><b>凭证核对</b><button class="review-search-toggle" title="搜索合同号" :aria-expanded="reviewSearch" @click="reviewSearch = !reviewSearch"><Search :size="17" /></button><div v-if="reviewSearch" class="search-box"><input aria-label="核对模式合同号搜索" v-model="query" placeholder="搜索合同号 / 报关单号"></div><span>{{ index < 0 ? 0 : index + 1 }} / {{ filtered.length }} 张</span><span class="review-save">{{ saveLabel }}</span><button class="secondary" @click="reviewMode = false"><X :size="16" />退出核对模式</button></div>
      <section class="workspace">
        <button v-if="reviewMode" class="review-arrow review-prev" aria-label="上一张凭证" :disabled="index <= 0" @click="navigate(-1)"><ChevronLeft :size="44" stroke-width="2.8" /></button>
        <button v-if="reviewMode" class="review-arrow review-next" aria-label="下一张凭证" :disabled="index < 0 || index >= filtered.length - 1" @click="navigate(1)"><ChevronRight :size="44" stroke-width="2.8" /></button>
        <aside class="voucher-list">
          <div class="list-heading"><b>凭证清单</b><span class="count">{{ drafts.length }}</span></div>
          <details ref="batchMenu" class="batch-dropdown"><summary aria-label="选择历史批次"><span>{{ batchLabel }}</span><ChevronDown :size="16" /></summary><div class="batch-popover"><div class="batch-menu-label">最近批次</div><div class="batch-options"><button v-for="b in recentBatches" :key="b.id" :class="{ selected: b.id === batchId }" @click="guard(() => chooseBatch(b.id))"><span><b>{{ b.created_at.slice(0,16).replace('T',' ') }}</b><small>{{ b.draft_ids.length }} 张凭证</small></span><Check v-if="b.id === batchId" :size="15" /></button><p v-if="!recentBatches.length">暂无历史批次</p></div><button class="batch-more" @click="guard(moreBatches)">查看更多 · 导出记录<ArrowRight :size="14" /></button></div></details>
          <div class="search-box"><Search :size="15" /><input aria-label="合同号搜索" v-model="query" placeholder="搜索合同号 / 报关单号"></div>
          <div class="list-tabs"><button :class="{ active: statusFilter === 'all' }" @click="statusFilter = 'all'">全部</button><button :class="{ active: statusFilter === 'invalid' }" @click="statusFilter = 'invalid'">异常 {{ drafts.filter(d => !d.valid).length }}</button><button :class="{ active: statusFilter === 'valid' }" @click="statusFilter = 'valid'">可导出</button></div>
          <div class="list-items"><button v-for="(d, i) in filtered" :key="d.id" class="voucher-item" :class="{ selected: d.id === selectedId }" @click="selectDraft(d.id)">
            <div class="item-top"><span class="item-number">{{ String(i + 1).padStart(2,'0') }}</span><span class="badge" :class="d.valid && !dirty.has(d.id) ? 'success' : 'warning'">{{ dirty.has(d.id) ? '修改中' : d.valid ? '可导出' : '待处理' }}</span></div><strong :title="d.contracts.join(' / ')">{{ d.contracts.join(' / ') }}</strong><small :title="d.customs_no">{{ d.customs_no }}</small><div class="item-bottom"><span>{{ d.header.posting_date }}</span><span>{{ d.rows.length }}条分录</span></div>
          </button><div v-if="!filtered.length" class="empty-list">{{ drafts.length ? '没有匹配的凭证' : '检索后，凭证将显示在这里' }}</div></div>

        </aside>
        <section ref="canvasElement" class="voucher-area">
          <template v-if="current">
            <div class="voucher-toolbar"><div class="canvas-identity"><b>{{ current.contracts[0] }}</b><span>{{ index + 1 }} / {{ filtered.length }}</span></div><div class="canvas-tools"><button title="缩小" aria-label="缩小凭证" :disabled="zoom <= .5" @click="changeZoom(-.1)"><Minus :size="16" /></button><span class="zoom-value">{{ Math.round(zoom * 100) }}%</span><button title="放大" aria-label="放大凭证" :disabled="zoom >= 1.4" @click="changeZoom(.1)"><Plus :size="16" /></button><button title="适应宽度" :aria-pressed="autoFit" @click="autoFit = true; fitPaper()"><Scan :size="17" /></button><span class="tool-separator"></span><button class="issues-toggle" :aria-expanded="showInspector" @click="showInspector = !showInspector; inspectorTab = 'issues'"><AlertCircle :size="16" />问题 {{ current.issues.length }}</button><button title="修改历史" @click="guard(() => openHistory(current!.id, '凭证修改历史'))"><History :size="16" /></button><button class="focus-button" @click="reviewMode = true">专注核对</button></div></div>
            <div v-if="current.mapping_stale" class="inline-warning">映射已更新。当前草稿仍使用生成时的映射。<button @click="guard(recalculate)">重新计算</button></div>
            <div v-if="current.incoming_source" class="inline-warning">飞书来源已有更新，请核对后重新计算。<button @click="guard(recalculate)">采用最新来源</button></div>

            <div ref="paperElement" class="voucher-paper" :style="{ zoom }">
              <div class="paper-heading"><span>嘉兴迈拓不锈钢有限公司</span><h2>记账凭证</h2><span class="paper-status" :class="current.valid ? 'success-text' : 'warning-text'"><CheckCircle2 v-if="current.valid" :size="15" /><AlertCircle v-else :size="15" />{{ current.valid ? '校验通过' : '待核对' }}</span></div>
              <div class="voucher-meta"><label>日期 <input aria-label="凭证日期" type="date" v-model="current.header.posting_date" @input="markDirty" @blur="blurSave"></label><label>类别 <input aria-label="凭证类别" class="short-input" v-model="current.header.category" @input="markDirty" @blur="blurSave"></label><span>报关单号 <b>{{ current.customs_no }}</b></span></div>
              <VoucherTable :key="current.id" :draft="current" :catalogs="catalogs" @change="markDirty" @blur="blurSave" @error="toast" />
              <div class="paper-footer"><label>制单人 <input aria-label="制单人" v-model="current.header.maker" @input="markDirty" @blur="blurSave"></label><span>{{ current.source.length }}条来源记录</span><span :class="Number(current.totals.difference) ? 'error-text' : 'success-text'">借贷差额 <b>{{ amount(current.totals.difference) }}</b></span></div>
            </div>

          </template>
          <div v-else class="empty-workspace"><div class="empty-icon"><FileSpreadsheet :size="34" stroke-width="1.3" /></div><h2>{{ reviewMode ? '没有匹配的凭证' : '准备好下一批凭证' }}</h2><p>从飞书待开记录开始，或按出口日期检索。<br>每一张凭证都可以核对、补充与编辑。</p><div class="steps"><span>01 选取范围</span><ArrowRight :size="14" /><span>02 核对凭证</span><ArrowRight :size="14" /><span>03 导出模板</span></div></div>
        </section>
        <aside v-if="current && showInspector && !reviewMode" class="inspector">
          <div class="inspector-title"><b>问题与来源</b><span class="count">{{ current.issues.length }}</span></div>
          <div class="list-tabs"><button :class="{ active: inspectorTab === 'issues' }" @click="inspectorTab = 'issues'">校验问题</button><button :class="{ active: inspectorTab === 'source' }" @click="inspectorTab = 'source'">来源补充</button></div>
          <div v-if="inspectorTab === 'issues'" class="issue-panel">
            <div v-if="!current.issues.length" class="all-good"><CheckCircle2 :size="28" /><b>这张凭证已准备就绪</b><p>借贷平衡，必填信息完整。</p></div>
            <button v-for="issue in current.issues" :key="issue.id" class="issue-card" @click="locate(issue)"><span><AlertCircle :size="14" />{{ issue.field || issue.source_field || '凭证校验' }}</span><p>{{ issue.message }}</p><small>定位并处理 <ArrowRight :size="12" /></small></button>

          </div>
          <div v-else class="source-panel"><select aria-label="来源记录" v-model="sourceIndex"><option v-for="(r,i) in current.source" :value="i">{{ i + 1 }} · {{ r['报关品名'] || r.record_id }}</option></select>
            <p class="muted">填写补充值后，点击重新计算。飞书原始值保留。</p>
            <label v-for="field in meta.source_fields" :key="field">{{ field }}<input :aria-label="'来源' + field" :value="sourceValue(field)" @input="updateSource(field, $event)" @blur="blurSave"><small v-if="current.overrides[current.source[sourceIndex]?.record_id]?.[field] !== undefined">原值：{{ current.source[sourceIndex]?.[field] ?? '空' }}</small></label>
            <button class="primary" @click="guard(recalculate)"><RefreshCw :size="14" />重新计算</button>
          </div>
        </aside>
      </section>
      <footer class="actionbar"><button class="save-indicator" :class="{ 'error-text': current && saveErrors.has(current.id), 'save-pulse': savePulse }" @click="current && guard(async () => { saveErrors.delete(current!.id); await save(current!.id) })"><RefreshCw v-if="current && (dirty.has(current.id) || saving.has(current.id))" :size="15" class="spin" /><CheckCircle2 v-else :size="15" />{{ saveLabel }}</button>
        <div class="pager" @touchstart="touchX = $event.changedTouches[0]!.clientX" @touchend="Math.abs($event.changedTouches[0]!.clientX - touchX) > 55 && navigate($event.changedTouches[0]!.clientX < touchX ? 1 : -1)">
          <button title="上一张" :disabled="index <= 0" @click="navigate(-1)"><ChevronLeft :size="17" /></button><span>{{ index < 0 ? 0 : index + 1 }} <span class="muted">/ {{ filtered.length }}</span></span><button title="下一张" :disabled="index >= filtered.length - 1" @click="navigate(1)"><ChevronRight :size="17" /></button></div>
        <div class="export-action"><span>共 {{ drafts.length }} 张 · <b>{{ validCount }}</b> 张可导出</span></div>
      </footer>
    </main>

    <main v-else-if="page === 'sources'" class="management">
      <section class="page-heading"><div><div class="eyebrow">DATA SOURCES</div><h1>数据源</h1><p>维护业务名称与用友档案的映射，修改后用于后续凭证生成。</p></div><button class="secondary" @click="guard(loadMappings)"><RefreshCw :size="15" />刷新映射</button></section>
      <section class="management-card"><div class="management-tabs"><button v-for="(label,kind) in mapLabels" :class="{ active: mapKind === kind }" @click="mapKind = kind; mapQuery = ''; mapEdit = null; referenceEdit = null">{{ label }}</button></div>
        <div class="table-tools"><div class="search-box"><Search :size="16" /><input v-model="mapQuery" :placeholder="mapKind === 'rates' ? '搜索年月 / 币种' : '搜索名称或编码'" aria-label="搜索数据源"></div><span class="table-count">{{ mapKind === 'rates' ? rates.rows.length + ' 个月 · ' + rates.columns.length + ' 种币种' : (isMapping ? mappingRows.length : referenceRows.length) + ' 条' }}</span><button v-if="isMapping" class="primary" @click="mapEdit = { alias: '', platform_code: '', enabled: true }; mapError = ''"><Plus :size="15" />新增映射</button><button v-if="mapKind === 'rates'" class="primary" @click="openRate()"><Plus :size="15" />新增汇率</button></div>
        <div class="data-scroll"><table v-if="isMapping" class="data-table mapping-table"><colgroup><col style="width:27%"><col style="width:37%"><col style="width:8%"><col style="width:17%"><col style="width:11%"></colgroup><thead><tr><th>来源名称</th><th>用友档案 · 编码 / 名称</th><th>状态</th><th>更新时间</th><th>操作</th></tr></thead><tbody><template v-for="group in mappingGroups" :key="group.name"><tr v-if="group.name !== '未分组' || mappingGroups.length > 1" class="group-row"><th colspan="5">{{ group.name }} <span>{{ group.rows.length }}</span></th></tr><tr v-for="row in group.rows" :key="row.id"><td><b class="row-name" :title="row.alias">{{ row.alias }}</b></td><td><div class="archive-line"><span class="code-chip">{{ row.platform_code }}</span><span class="row-name" :title="row.platform_name">{{ row.platform_name }}</span></div><small v-if="mapKind === 'inventory'" class="row-caption">{{ catalogs.inventory?.find((r: any) => String(r.code) === row.platform_code)?.subject_code }} · {{ catalogs.inventory?.find((r: any) => String(r.code) === row.platform_code)?.subject_name }}</small></td><td><span class="badge" :class="row.enabled ? 'success' : 'neutral'">{{ row.enabled ? '启用' : '停用' }}</span></td><td class="timestamp">{{ row.updated_at?.slice(0,16).replace('T',' ') || '初始映射' }}</td><td class="table-actions"><button @click="mapEdit = clone(row); mapError = ''">编辑</button><button @click="guard(() => openHistory('mapping:' + mapKind + ':' + row.id, '映射修改历史'))">历史</button></td></tr></template></tbody></table>
        <table v-else-if="mapKind === 'rates'" class="data-table rate-table"><thead><tr><th>年月</th><th v-for="currency in rates.columns" :key="currency">{{ currency }}</th></tr></thead><tbody><tr v-for="row in rates.rows" :key="row.period"><th>{{ row.period }}</th><td v-for="currency in rates.columns" :key="currency" :title="row.cells[currency]?.sources.join('；') || '无汇率记录'"><button class="rate-edit" :aria-label="'编辑' + row.period + currency + '汇率'" @click="openRate(row.period, currency)"><span v-if="(row.cells[currency]?.values.length || 0) > 1" class="rate-conflict" :title="'同月同币种存在多个汇率：' + row.cells[currency]!.values.join(' / ')"><AlertCircle :size="14" />{{ row.cells[currency]!.values.join(' / ') }}</span><span v-else>{{ row.cells[currency]?.values[0] || '—' }}</span></button></td></tr></tbody></table>
        <table v-else class="data-table reference-table"><colgroup><col style="width:19%"><col style="width:43%"><col style="width:23%"><col style="width:15%"></colgroup><thead><tr><th>编码</th><th>名称</th><th>配置</th><th>操作</th></tr></thead><tbody><template v-for="group in referenceGroups" :key="group.name"><tr v-if="group.name !== '未分组' || referenceGroups.length > 1" class="group-row"><th colspan="4">{{ group.name }} <span>{{ group.rows.length }}</span></th></tr><tr v-for="row in group.rows" :key="row.code || row.account_code"><td><span class="code-chip">{{ row.code || row.account_code }}</span></td><td><span class="row-name" :title="row.displayname || row.name || row.account_name">{{ row.displayname || row.name || row.account_name }}</span></td><td>{{ mapKind === 'subjects' ? (row.leaf ? '末级科目' : '上级科目') : row.auxiliary_fields.map((a: any) => a.name).join('、') }}</td><td class="table-actions"><button @click="openReference(row)">编辑</button><button @click="guard(() => openHistory('catalog:' + mapKind + ':' + (row.code || row.account_code), '数据源修改历史'))">历史</button></td></tr></template></tbody></table>
        <div v-if="!isMapping && !(mapKind === 'rates' ? rates.rows.length : referenceRows.length)" class="empty-list">没有匹配的记录</div>
        <div v-if="isMapping && !mappingRows.length" class="empty-list">暂无匹配的映射</div></div>
      </section>
    </main>

    <main v-else class="management">
      <section class="page-heading"><div><div class="eyebrow">EXPORT HISTORY</div><h1>导出记录</h1><p>下载已保存的模板，查看凭证版本与飞书回写进度。</p></div><button class="secondary" @click="guard(refreshLists)"><RefreshCw :size="15" />刷新记录</button></section>
      <section class="management-card"><div class="data-scroll"><table class="data-table export-table"><colgroup><col style="width:39%"><col style="width:12%"><col style="width:17%"><col style="width:13%"><col style="width:19%"></colgroup><thead><tr><th>导出文件</th><th>凭证 / 来源记录</th><th>生成时间</th><th>飞书回写</th><th>操作</th></tr></thead><tbody><tr v-for="e in exports" :key="e.id"><td><div class="file-title"><FileSpreadsheet :size="22" /><div><button class="archive-link" :title="'查看历史核算：' + e.filename" @click="guard(() => openArchive(e.id))">{{ e.filename }}</button><small :title="e.contracts.join(' / ')">{{ e.contracts.slice(0,2).join(' / ') }}{{ e.contracts.length > 2 ? ' …' : '' }}</small></div></div></td><td class="export-volume"><b>{{ e.vouchers }}</b> 张<span class="row-caption">{{ e.records }} 条来源</span></td><td class="timestamp">{{ e.created_at.slice(0,19).replace('T',' ') }}</td><td><span class="badge" :class="e.writeback === 'done' ? 'success' : ['offline','superseded'].includes(e.writeback) ? 'export-neutral' : 'warning'">{{ ({done:'已回写',failed:'回写失败',pending:'待回写',offline:'演示未回写',superseded:'已有新版'} as any)[e.writeback] }}</span><small class="block muted">{{ e.written }}/{{ e.records }}</small></td><td class="table-actions"><div class="export-row-actions"><a :href="'/api/exports/' + e.id + '/download'"><Download :size="14" />下载</a><button @click="guard(() => openArchive(e.id))">历史核算</button><button v-if="['pending','failed'].includes(e.writeback)" :disabled="busy" @click="guard(() => runTask('writeback', { export_id: e.id }))">重试回写</button></div></td></tr></tbody></table><div v-if="!exports.length" class="empty-workspace compact"><History :size="32" /><h2>还没有导出记录</h2><p>完成凭证核对后，导出的文件将在这里保留。</p></div></div></section>
    </main>

    <div v-if="showRange" class="modal-backdrop"><section class="modal wide"><div class="modal-heading"><div><small>选取生成范围</small><h2>找到 {{ snapshot.groups.length }} 张凭证</h2></div><button @click="showRange = false"><X :size="20" /></button></div><p class="muted">命中 {{ snapshot.matched }} 条记录 · 已开或已导出排除 {{ snapshot.excluded }} 条 · 无有效日期 {{ snapshot.dateless }} 条</p>
      <div class="modal-scroll"><table class="data-table"><thead><tr><th><input type="checkbox" aria-label="全选可生成凭证" :checked="selectedGroups.length === snapshot.groups.filter((g: any) => !g.blocked.length).length" @change="selectedGroups = ($event.target as HTMLInputElement).checked ? snapshot.groups.filter((g: any) => !g.blocked.length).map((g: any) => g.key) : []"></th><th>合同号 / 报关单号</th><th>记录</th><th>分组核验</th></tr></thead><tbody><tr v-for="g in snapshot.groups" :key="g.key"><td><input type="checkbox" :aria-label="'选择' + g.contracts.join(' ')" v-model="selectedGroups" :value="g.key" :disabled="g.blocked.length > 0"></td><td><b>{{ g.contracts.join(' / ') }}</b><small class="block muted">{{ g.customs_no || '缺少报关单号' }}</small></td><td>{{ g.count }} 条</td><td><span v-if="g.blocked.length" class="error-text">{{ g.blocked.join('；') }}</span><span v-else-if="g.added" class="warning-text">已补入同报关单 {{ g.added }} 条</span><span v-else class="success-text">分组完整</span><details><summary>查看来源</summary><p v-for="r in g.records">{{ r['合同号_1'] }} · {{ r['报关品名'] }} · {{ r['是否开凭证'] || '未标记' }}</p></details></td></tr></tbody></table></div>
      <footer class="modal-footer"><span>已选 {{ selectedGroups.length }} 张 · 制单日期 {{ postingDate }}</span><button class="primary" :disabled="!selectedGroups.length || busy" @click="guard(() => runTask('build', { snapshot_id: snapshot.id, keys: selectedGroups, posting_date: postingDate }))">构建凭证草稿<ArrowRight :size="15" /></button></footer>
    </section></div>
    <div v-if="exportComplete" class="modal-backdrop"><section class="modal small export-complete" role="dialog" aria-modal="true" aria-labelledby="export-complete-title"><div class="modal-heading"><h2 id="export-complete-title">导出文件已生成</h2><button aria-label="关闭导出完成提示" @click="exportComplete = null"><X :size="20" /></button></div><div class="completed-file"><FileSpreadsheet :size="28" /><b>{{ exportComplete.filename }}</b></div><p>已发起下载。若浏览器未开始下载，可点击下方“再次下载”。文件已保存在导出记录中，可随时重复下载。</p><p v-if="exportComplete.writeback === 'failed'" class="error-text">文件已生成，但飞书回写失败，可在导出记录中重试回写。</p><footer class="modal-footer"><button class="secondary" @click="downloadExport(exportComplete)"><Download :size="15" />再次下载</button><button class="primary" @click="reviewMode = false; page = 'exports'; exportComplete = null">前往导出记录<ArrowRight :size="15" /></button></footer></section></div>
    <div v-if="exportPreview" class="modal-backdrop"><section class="modal"><div class="modal-heading"><div><small>导出确认</small><h2>{{ exportPreview.valid.length }} 张凭证可以导出</h2></div><button @click="exportPreview = null"><X :size="20" /></button></div><p>{{ meta.offline ? '导出保存后的有效凭证，演示模式不回写飞书。' : '导出保存后的有效凭证，并将对应飞书记录标为「已开」。' }}生成后自动下载，也可在导出记录中重复下载。</p><div class="modal-scroll"><div v-for="d in exportPreview.valid" class="preview-item"><CheckCircle2 :size="16" class="success-text" /><b>{{ d.contracts.join(' / ') }}</b><span>版本 {{ d.version }}</span><small v-if="d.replaces.length" class="warning-text">替换已有导出</small></div><div v-if="exportPreview.excluded.length" class="inline-warning">以下 {{ exportPreview.excluded.length }} 张暂不导出，也不会回写飞书。</div><div v-for="d in exportPreview.excluded" class="excluded-item"><b>{{ d.contracts.join(' / ') }}</b><small>{{ d.issues.map((i: Issue) => i.message).join('；') }}</small></div></div><footer class="modal-footer"><span>53列 · Excel .xls</span><button class="primary" :disabled="!exportPreview.valid.length || busy" @click="guard(() => runTask('export', { drafts: exportPreview.valid.map((d: any) => ({ id: d.id, version: d.version })), replace_export: exportPreview.valid.some((d: any) => d.replaces.length) }))"><Download :size="15" />确认导出有效凭证</button></footer></section></div>
    <div v-if="referenceEdit" class="modal-backdrop"><section class="modal small"><div class="modal-heading"><h2>编辑{{ mapLabels[mapKind] }}</h2><button :disabled="referenceSaving" @click="referenceEdit = null"><X :size="20" /></button></div><div class="modal-scroll form-stack">
      <template v-if="mapKind === 'subjects'"><p>{{ referenceEdit.code }} · {{ referenceEdit.displayname }}</p><label>科目名称<input v-model="referenceEdit.name"></label><label>余额方向<select v-model="referenceEdit.direction"><option value="Debit">借方</option><option value="Credit">贷方</option></select></label></template>
      <template v-else-if="mapKind === 'aux'"><p>{{ referenceEdit.account_code }} · {{ referenceEdit.account_name }}</p><div v-for="option in auxOptions" :key="option.code" class="aux-config-row"><label><input type="checkbox" :checked="referenceEdit.auxiliary_fields.some((f: any) => f.code === option.code)" @change="toggleAux(option, ($event.target as HTMLInputElement).checked)">{{ option.name }}</label><label v-if="referenceEdit.auxiliary_fields.some((f: any) => f.code === option.code)"><input type="checkbox" v-model="referenceEdit.auxiliary_fields.find((f: any) => f.code === option.code).mustinput">必填</label></div></template>
      <template v-else><label v-if="rateChoices.length > 1">同月来源记录<select :value="referenceEdit.record_id || referenceEdit.id" @change="selectRate(($event.target as HTMLSelectElement).value)"><option v-for="r in rateChoices" :value="r.record_id || r.id">{{ scalar(r.fields['汇率']) }} · {{ scalar(r.fields['数据来源']) }}</option></select></label><label>年份<input v-model="referenceEdit.fields['实际年份']" :disabled="!!referenceEdit.id"></label><label>月份<input v-model="referenceEdit.fields['实际月份']" :disabled="!!referenceEdit.id"></label><label>币种<input v-model="referenceEdit.fields['币种']" :disabled="!!referenceEdit.id"></label><label>汇率<input v-model="referenceEdit.fields['汇率']" inputmode="decimal"></label><label>来源说明<input v-model="referenceEdit.fields['数据来源']"></label></template>
      <p class="error-text" role="alert">{{ referenceError }}</p></div><footer class="modal-footer"><span>仅更新工作台本地配置</span><button class="primary" :disabled="referenceSaving" @click="saveReference">{{ referenceSaving ? '保存中…' : '保存' }}</button></footer></section></div>
    <div v-if="mapEdit" class="modal-backdrop"><section class="modal small"><div class="modal-heading"><h2>{{ mapEdit.id ? '编辑' : '新增' }}{{ mapLabels[mapKind] }}映射</h2><button @click="mapEdit = null"><X :size="20" /></button></div><div class="form-stack"><label>来源名称<input v-model="mapEdit.alias"></label><label>用友档案编码<input list="mapping-options" v-model="mapEdit.platform_code" placeholder="输入编码或搜索档案名称"></label><datalist id="mapping-options"><option v-for="r in catalogs[mapKind]" :value="String(r.code)">{{ r.name }}</option></datalist><p class="muted">{{ catalogs[mapKind]?.find((r: any) => String(r.code) === mapEdit.platform_code)?.name || '选择有效档案后可保存' }}</p><label class="checkbox-label"><input type="checkbox" v-model="mapEdit.enabled">启用此映射</label><p class="error-text">{{ mapError }}</p></div><footer class="modal-footer"><span>保存后后台立即更新</span><button class="primary" @click="saveMap">保存映射</button></footer></section></div>
    <div v-if="conflict" class="modal-backdrop"><section class="modal"><div class="modal-heading"><h2>这张凭证已在其他窗口修改</h2></div><p>你的输入已保存在当前浏览器。比较版本后选择保留哪一份。</p><div class="conflict-columns"><div><b>本地输入 · 版本 {{ conflict.local.version }}</b><pre>{{ JSON.stringify({ header: conflict.local.header, rows: conflict.local.rows, overrides: conflict.local.overrides }, null, 2) }}</pre></div><div><b>服务器 · 版本 {{ conflict.remote.version }}</b><pre>{{ JSON.stringify({ header: conflict.remote.header, rows: conflict.remote.rows, overrides: conflict.remote.overrides }, null, 2) }}</pre></div></div><footer class="modal-footer"><button class="secondary" @click="guard(() => resolveConflict(false))">采用服务器版本</button><button class="primary" @click="guard(() => resolveConflict(true))">确认以本地输入覆盖</button></footer></section></div>
    <div v-if="history" class="modal-backdrop"><section class="modal wide"><div class="modal-heading"><h2>{{ historyTitle }}</h2><button @click="history = null"><X :size="20" /></button></div><div class="modal-scroll"><div v-for="h in history" class="history-item"><b>{{ h.at.replace('T',' ') }}</b><span>浏览器 {{ h.session.slice(0,8) }} · {{ ({edit:'编辑',build:'生成',recalculate:'重新计算',save_mapping:'维护映射',save_catalog:'维护数据源'} as any)[h.action] || h.action }}</span><details><summary>查看该版本内容</summary><pre>{{ JSON.stringify(h.after, null, 2) }}</pre></details></div><p v-if="!history.length">暂无修改记录</p></div></section></div>
    <div v-if="exportDetail" class="modal-backdrop"><section class="modal archive-modal" role="dialog" aria-modal="true" aria-labelledby="archive-title"><div class="modal-heading"><div><small>已导出快照 · 只读</small><h2 id="archive-title">历史核算</h2></div><span class="archive-filename" :title="exportDetail.filename">{{ exportDetail.filename }}</span><button aria-label="关闭历史核算" @click="exportDetail = null"><X :size="20" /></button></div><div class="archive-toolbar"><div class="search-box"><Search :size="16" /><input v-model="archiveQuery" placeholder="搜索合同号 / 报关单号" aria-label="搜索历史核算"></div><div class="pager"><button aria-label="上一张历史凭证" :disabled="archiveIndex <= 0" @click="archiveIndex--"><ChevronLeft :size="18" /></button><span>{{ archiveCurrent ? archiveIndex + 1 : 0 }} / {{ archiveDrafts.length }}</span><button aria-label="下一张历史凭证" :disabled="archiveIndex >= archiveDrafts.length - 1" @click="archiveIndex++"><ChevronRight :size="18" /></button></div><a class="secondary" :href="'/api/exports/' + exportDetail.id + '/download'"><Download :size="15" />下载此版本</a></div><div class="modal-scroll"><div v-if="archiveCurrent" class="voucher-paper"><div class="paper-heading"><span>{{ archiveCurrent.contracts.join(' / ') }}</span><h2>记账凭证</h2><span>版本 {{ archiveCurrent.version }}</span></div><div class="voucher-meta"><span>日期 {{ archiveCurrent.header.posting_date }}</span><span>类别 {{ archiveCurrent.header.category }}</span><span>报关单号 {{ archiveCurrent.customs_no }}</span></div><VoucherTable :key="archiveCurrent.id" :draft="archiveCurrent" :catalogs="catalogs" readonly /><div class="paper-footer"><span>制单人 {{ archiveCurrent.header.maker }}</span><span>借贷差额 {{ amount(archiveCurrent.totals.difference) }}</span></div></div><p v-else class="empty-list">没有匹配的凭证</p></div></section></div>
  </div>
</template>
