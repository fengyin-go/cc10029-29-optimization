<template>
  <section class="page" data-module="flowline">
    <header class="page-head">
      <div><h2>采集流程</h2><p class="page-desc">编排采集流程的多步骤序列，组合站点、提取和存储。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记流程编排</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出关系清单</button>
      </div>
    </header>
    <div class="dag-wrap" ref="dagRef">
      <svg class="dag-svg" :viewBox="`0 0 ${svgW} ${svgH}`">
        <defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" fill="#94a3b8"/></marker></defs>
        <g v-for="edge in graph.edges" :key="edge.from+edge.to">
          <line class="dag-edge" :x1="nodePos[edge.from]?.x||0" :y1="nodePos[edge.from]?.y||0" :x2="nodePos[edge.to]?.x||0" :y2="nodePos[edge.to]?.y||0"/>
          <text :x="(nodePos[edge.from]?.x+nodePos[edge.to]?.x)/2" :y="(nodePos[edge.from]?.y+nodePos[edge.to]?.y)/2 - 6" font-size="10" fill="#64748b" text-anchor="middle">{{ edge.label }}</text>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :class="['dag-node', {selected: selectedNode?.id===node.id}]" @click="selectNode(node)" @mousedown="startDrag($event, node)">
          <rect :x="nodePos[node.id]?.x - 60" :y="nodePos[node.id]?.y - 20" width="120" height="40" rx="6" :fill="nodeColor(node.status)"/>
          <text :x="nodePos[node.id]?.x" :y="nodePos[node.id]?.y + 4" text-anchor="middle">{{ node.name || node.label }}</text>
        </g>
      </svg>
    </div>
    <div v-if="selectedNode" class="dag-detail" data-testid="dag-detail">
      <dl><dt>编号</dt><dd>{{ selectedNode.id }}</dd></dl>
      <dl><dt>名称</dt><dd>{{ selectedNode.name || selectedNode.label }}</dd></dl>
      <dl v-for="f in detailFields" :key="f"><dt>{{ f }}</dt><dd>{{ selectedNode[f] ?? '—' }}</dd></dl>
      <div style="grid-column:1/-1; margin-top:8px; display:flex; gap:6px;">
        <button v-for="action in actions" :key="action" class="btn" :data-testid="`action-${selectedNode.id}-${action}`" @click="runAction(action, selectedNode)">{{ action }}</button>
      </div>
    </div>

    <section class="export-panel">
      <h3>流程关系导出数据包</h3>
      <p class="page-desc">节点状态不一致时可用关系先形成可下载版本，失败节点带错误码保留在包里，可单独补写。</p>
      <div class="page-actions">
        <select v-model="schemaVersion" class="schema-select" data-testid="schema-version">
          <option value="v2">新版（部分失败可补写）</option>
          <option value="v1">旧版（整包重做）</option>
        </select>
        <button class="btn primary" type="button" data-testid="export-package" :disabled="exporting" @click="createPackage">
          {{ exporting ? '导出中…' : '生成流程关系导出数据包' }}
        </button>
        <button v-if="pkg" class="btn" type="button" data-testid="download-package" @click="downloadPackage">下载可用关系</button>
        <button v-if="pkg && pkg.failed_nodes.length" class="btn" type="button" data-testid="retry-package" :disabled="retrying" @click="retryPackage">
          {{ retrying ? '补写中…' : `重试失败节点（${pkg.failed_nodes.length}）` }}
        </button>
        <button v-if="pkg" class="btn" type="button" data-testid="land-package" :disabled="landing" @click="landPackage">
          {{ landing ? '落地中…' : '生成落地批次回执' }}
        </button>
      </div>

      <div v-if="packageMessage" class="package-message" data-testid="package-message">{{ packageMessage }}</div>

      <div v-if="pkg" class="package-info" data-testid="package-info">
        <dl><dt>数据包</dt><dd>{{ pkg.package_id }}（{{ pkg.schema_version === 'v1' ? '旧版' : '新版' }}）</dd></dl>
        <dl><dt>状态</dt><dd>{{ pkg.status }}</dd></dl>
        <dl><dt>边界版本</dt><dd>{{ pkg.flow_version }} · 第 {{ pkg.revision }} 版</dd></dl>
        <dl><dt>版本摘要</dt><dd>节点 {{ pkg.version_summary.ok_count }}/{{ pkg.version_summary.node_count }} 通过 · 边 {{ pkg.version_summary.edge_count }} 条 · 失败 {{ pkg.version_summary.failed_count }} 个</dd></dl>
      </div>

      <table v-if="pkg" class="data-table package-table">
        <thead>
          <tr><th>节点</th><th>名称</th><th>流程状态</th><th>版本摘要</th><th>校验</th><th>错误码</th></tr>
        </thead>
        <tbody>
          <tr v-for="node in pkg.nodes" :key="node.node_id" :data-testid="`package-node-${node.node_id}`">
            <td>{{ node.node_id }}</td>
            <td>{{ node.name }}</td>
            <td>{{ node.status }}</td>
            <td class="mono">{{ node.version }}</td>
            <td>
              <span v-if="node.state === 'ok' && node.backfilled">已补写</span>
              <span v-else-if="node.state === 'ok'">通过</span>
              <span v-else class="error-text">失败</span>
            </td>
            <td>{{ node.error_code ?? '—' }}</td>
          </tr>
        </tbody>
      </table>

      <div v-if="retryDetail" class="retry-detail" data-testid="retry-detail">
        <span>补写：{{ retryDetail.backfilled.join('、') || '无' }}</span>
        <span>未改写（已校验通过）：{{ retryDetail.kept.join('、') || '无' }}</span>
        <span>仍失败：{{ retryDetail.still_failed.join('、') || '无' }}</span>
      </div>

      <div v-if="receipt" class="receipt-info" data-testid="receipt-info">
        落地批次回执 {{ receipt.receipt_id }}：{{ receipt.conclusion }}（模式：{{ receipt.mode }}，节点 {{ receipt.node_count }}，边 {{ receipt.edge_count }}）
      </div>

      <div v-if="repairLogs.length" class="repair-logs">
        <h4>修复证据</h4>
        <ul>
          <li v-for="log in repairLogs" :key="log.id" data-testid="repair-log">[{{ log.kind }}] {{ log.detail }}（{{ log.at }}）</li>
        </ul>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记流程编排</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <footer class="page-foot">
      <span>{{ graph.nodes.length }} 个节点</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/flowline'
const STORAGE_ENDPOINT = '/api/storage'
const columns = ["流程编号", "流程名称", "步骤序列", "输入站点", "输出目标", "失败处理", "触发条件", "版本号"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const STATUS_COLORS: Record<string,string> = { '草稿':'#e2e8f0','待校验':'#fef3c7','已启用':'#d1fae5','已停用':'#fee2e2' }
const nodeColor = (s: string) => STATUS_COLORS[s] || '#e2e8f0'
const graph = reactive<{nodes: any[], edges: any[]}>({ nodes: [], edges: [] })
const nodePos = ref<Record<number,{x:number;y:number}>>({})
const selectedNode = ref<any>(null)
const detailFields = computed(() => columns.filter((f: string) => !['id','name','label','status'].includes(f)))
const svgW = ref(800); const svgH = ref(400)
const errorMessage = ref('')
const creating = ref(false); const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const schemaVersion = ref('v2')
const pkg = ref<any>(null)
const packageMessage = ref('')
const retryDetail = ref<any>(null)
const receipt = ref<any>(null)
const repairLogs = ref<any[]>([])
const exporting = ref(false); const retrying = ref(false); const landing = ref(false)
let dragNode: any = null; const dagRef = ref<HTMLElement|null>(null)
function layout(nodes: any[]) {
  const w = 800; const h = Math.max(400, nodes.length * 80)
  svgW.value = w; svgH.value = h
  const pos: Record<number,{x:number;y:number}> = {}
  nodes.forEach((n,i) => { pos[n.id] = { x: 120 + (i % 5) * 150, y: 50 + Math.floor(i / 5) * 80 } })
  nodePos.value = pos
}
function selectNode(node: any) { selectedNode.value = selectedNode.value?.id === node.id ? null : node }
function startDrag(e: MouseEvent, node: any) { dragNode = node; const onMove = (ev: MouseEvent) => { if(!dragNode) return; nodePos.value[node.id] = { x: ev.offsetX, y: ev.offsetY } }; const onUp = () => { dragNode = null; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }; document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp) }
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes) } catch(e) { errorMessage.value = '加载失败' }
}
async function runAction(action: string, node: any) {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/${node.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) })
    const payload = await res.json()
    if (!res.ok || payload.ok === false) throw new Error(payload.message || payload.detail || '动作未生效')
    await reload()
  } catch(e: any) { errorMessage.value = e.message || '操作失败' }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
async function exportRows() {
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/export`)
    if (!res.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await res.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'flowline.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch(e: any) { errorMessage.value = e.message || '导出失败' }
}
async function createPackage() {
  exporting.value = true
  errorMessage.value = ''
  packageMessage.value = ''
  retryDetail.value = null
  receipt.value = null
  try {
    const res = await request(`${ENDPOINT}/export-packages`, { method: 'POST', body: JSON.stringify({ schema_version: schemaVersion.value }) })
    const payload = await res.json()
    packageMessage.value = payload.message || ''
    if (payload.package) pkg.value = payload.package
    if (!res.ok || payload.ok === false) throw new Error(payload.message || '导出数据包生成失败')
    await loadRepairs()
  } catch(e: any) { errorMessage.value = e.message || '导出数据包生成失败' }
  finally { exporting.value = false }
}
async function downloadPackage() {
  if (!pkg.value) return
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/export-packages/${pkg.value.package_id}/download`)
    if (!res.ok) throw new Error('下载失败，请稍后重试')
    const url = URL.createObjectURL(await res.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = `${pkg.value.package_id}.csv`
    link.click()
    URL.revokeObjectURL(url)
  } catch(e: any) { errorMessage.value = e.message || '下载失败' }
}
async function retryPackage() {
  if (!pkg.value) return
  retrying.value = true
  errorMessage.value = ''
  try {
    const res = await request(`${ENDPOINT}/export-packages/${pkg.value.package_id}/retry`, { method: 'POST', body: JSON.stringify({}) })
    const payload = await res.json()
    if (!res.ok) throw new Error(payload.detail || payload.message || '补写失败')
    pkg.value = payload.package
    retryDetail.value = payload.detail
    packageMessage.value = payload.message || ''
    await loadRepairs()
  } catch(e: any) { errorMessage.value = e.message || '补写失败' }
  finally { retrying.value = false }
}
async function landPackage() {
  if (!pkg.value) return
  landing.value = true
  errorMessage.value = ''
  try {
    const res = await request(`${STORAGE_ENDPOINT}/landing-receipts`, { method: 'POST', body: JSON.stringify({ package_id: pkg.value.package_id }) })
    const payload = await res.json()
    if (!res.ok || payload.ok === false) throw new Error(payload.message || '落地失败')
    receipt.value = payload.receipt
    packageMessage.value = payload.message || ''
  } catch(e: any) { errorMessage.value = e.message || '落地失败' }
  finally { landing.value = false }
}
async function loadCurrentPackage() {
  try {
    const res = await request(`${ENDPOINT}/export-packages/current`)
    if (!res.ok) return
    const payload = await res.json()
    if (payload.package) pkg.value = payload.package
  } catch(e) { /* 面板加载失败不影响图展示 */ }
}
async function loadRepairs() {
  try {
    const res = await request(`${ENDPOINT}/export-repairs`)
    if (!res.ok) return
    const payload = await res.json()
    repairLogs.value = payload.items ?? []
  } catch(e) { /* 修复证据加载失败不影响主流程 */ }
}
onMounted(() => { reload(); loadCurrentPackage(); loadRepairs() })
</script>
<style scoped>
.export-panel { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }
.export-panel h3 { margin: 0 0 4px; font-size: 15px; }
.schema-select { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; background: #fff; }
.package-message { margin: 8px 0; font-size: 13px; color: var(--muted); }
.package-info { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; margin: 8px 0; }
.package-info dl { display: flex; gap: 6px; margin: 0; font-size: 13px; }
.package-info dt { color: var(--muted); }
.package-table { margin-top: 8px; }
.mono { font-family: ui-monospace, monospace; font-size: 12px; }
.retry-detail { display: flex; gap: 16px; margin-top: 8px; font-size: 13px; }
.receipt-info { margin-top: 8px; padding: 8px 10px; background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 6px; font-size: 13px; }
.repair-logs { margin-top: 12px; }
.repair-logs h4 { margin: 0 0 4px; font-size: 13px; color: var(--muted); }
.repair-logs ul { margin: 0; padding-left: 18px; font-size: 12px; color: var(--muted); }
</style>
