<template>
  <section class="page" data-module="storage">
    <header class="page-head">
      <div>
        <h2>存储落地管理</h2>
        <p class="page-desc">管理采集结果持久化的存储类型、分区和生命周期。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记存储配置</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出存储落地清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无存储落地数据，可先登记存储配置</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记存储配置</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <section class="receipt-panel">
      <h3>落地批次回执清单</h3>
      <p class="page-desc">流程关系导出数据包通过完整校验后在此落地，回执指向具体导出版本，可核对两边差异。</p>
      <table class="data-table">
        <thead>
          <tr>
            <th>回执编号</th><th>数据包</th><th>边界版本</th><th>模式</th><th>节点/边</th><th>结论</th><th>落地时间</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="receipt in receipts" :key="receipt.receipt_id" :data-testid="`receipt-${receipt.receipt_id}`">
            <td>{{ receipt.receipt_id }}</td>
            <td>{{ receipt.package_id }}（第 {{ receipt.package_revision }} 版）</td>
            <td>{{ receipt.flow_version }}</td>
            <td>{{ receipt.mode }}</td>
            <td>{{ receipt.node_count }} / {{ receipt.edge_count }}</td>
            <td>{{ receipt.conclusion }}</td>
            <td>{{ receipt.created_at }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`diff-${receipt.receipt_id}`" @click="checkDiff(receipt)">差异核对</button>
            </td>
          </tr>
          <tr v-if="!receipts.length">
            <td colspan="8" class="empty-state">暂无落地批次回执，可先在采集流程生成导出数据包并落地</td>
          </tr>
        </tbody>
      </table>
      <div v-if="diffResult" class="diff-result" :data-testid="`diff-result-${diffResult.receipt_id}`">
        <strong>{{ diffResult.receipt_id }} 与 {{ diffResult.package_id }}：</strong>
        <span v-if="diffResult.matched">数据流一致，回执与数据包对得上。</span>
        <ul v-else>
          <li v-for="(item, index) in diffResult.differences" :key="index" class="error-text">{{ item }}</li>
        </ul>
      </div>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条存储落地记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>

const ENDPOINT = '/api/storage'
const columns = ["配置编号", "存储类型", "连接信息", "分区策略", "压缩格式", "保存天数", "备份副本", "清理策略"]
const actions = ["提交校验", "启用配置", "停用配置"]
const statuses = ["草稿", "待校验", "已启用", "已停用"]
const metricNames = ["存储配置总数", "待校验存储配置", "已启用存储配置"]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const receipts = ref<any[]>([])
const diffResult = ref<any>(null)
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'storage.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('存储落地动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储落地操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('存储配置列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '存储落地列表读取失败'
  }
}

async function loadReceipts() {
  try {
    const response = await request(`${ENDPOINT}/landing-receipts`)
    if (!response.ok) return
    const payload = await response.json()
    receipts.value = payload.items ?? []
  } catch (error) { /* 回执清单加载失败不影响存储配置主列表 */ }
}

async function checkDiff(receipt: any) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/landing-receipts/${receipt.receipt_id}/diff`)
    if (!response.ok) throw new Error('差异核对失败，请稍后重试')
    diffResult.value = await response.json()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '差异核对失败'
  }
}

onMounted(() => { reload(); loadReceipts() })
</script>

<style scoped>
.receipt-panel { margin-top: 16px; border-top: 1px solid var(--border); padding-top: 12px; }
.receipt-panel h3 { margin: 0 0 4px; font-size: 15px; }
.diff-result { margin-top: 8px; padding: 8px 10px; background: #f8fafc; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.diff-result ul { margin: 4px 0 0; padding-left: 18px; }
</style>
