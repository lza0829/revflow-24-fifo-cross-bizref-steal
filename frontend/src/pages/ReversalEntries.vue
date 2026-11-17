<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/entries?kind=冲正')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function statusClass(status) {
  if (status === '已归并') return 'on'
  if (status === '已隔离') return 'bad'
  if (status === '部分归并') return 'warn'
  return ''
}
</script>

<template>
  <div>
    <h1>冲正流水</h1>
    <p class="lead">
      冲正优先关联原正向流水键；只带业务单的冲正按同账户、同业务单的入账时间 FIFO 占用，
      占用结果落到匹配占用表，后续归并只认该占用。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>金额（元）</th><th>原单键 / FIFO 占用</th>
            <th>业务单</th><th>已占用</th><th>未消化</th><th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td>{{ e.entry_key }}</td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td>
              <div v-if="e.target_entry_key" class="alloc-line">
                <code>{{ e.target_entry_key }}</code>
                <span class="muted">（显式原单）</span>
              </div>
              <div v-for="a in e.allocations" :key="a.id" class="alloc-line">
                <code>{{ a.forward_entry_key }}</code>
                <span class="split">+{{ yuan(a.allocated_cents) }}</span>
              </div>
              <span v-if="!e.target_entry_key && !e.allocations.length" class="muted">—</span>
              <div v-if="e.fifo_short" class="tag warn">FIFO 不足</div>
            </td>
            <td>{{ e.biz_ref }}</td>
            <td>{{ yuan(e.allocated_cents) }}</td>
            <td>
              <span v-if="e.residual_cents > 0" class="tag warn">{{ yuan(e.residual_cents) }}</span>
              <span v-else class="muted">0.00</span>
            </td>
            <td>
              <span class="tag" :class="statusClass(e.status)">{{ e.status }}</span>
              <div v-if="e.quarantine_reason && e.status !== '已归并'" class="muted small">
                {{ e.quarantine_reason }}
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.alloc-line {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-family: var(--mono);
  font-size: 12px;
}
.alloc-line code { color: var(--accent); }
.split { color: var(--ok); }
.muted { color: var(--muted); }
.small { font-size: 11px; margin-top: 4px; }
</style>
