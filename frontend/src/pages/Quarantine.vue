<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/quarantine')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}
</script>

<template>
  <div>
    <h1>未平隔离</h1>
    <p class="lead">
      冲正无原单、账户不一致、超额或 FIFO 敞口不足的未消化部分。部分冲正的隔离项引用已占用部分，
      隔离金额只计未消化额度。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>原因</th><th>隔离金额（元）</th>
            <th>已占用引用</th><th>FIFO 占用明细</th><th>归并#</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="q in rows" :key="q.id">
            <td>{{ q.entry_key }}</td>
            <td>{{ q.account_code }}</td>
            <td>
              <span class="tag" :class="q.reason === 'FIFO 不足' ? 'warn' : 'bad'">{{ q.reason }}</span>
            </td>
            <td>{{ yuan(q.amount_cents) }}</td>
            <td>
              <template v-if="q.allocated_cents > 0">
                {{ yuan(q.allocated_cents) }}
                <span v-if="q.detail && q.detail['冲正金额分']" class="muted small">
                  / 冲正 {{ yuan(q.detail['冲正金额分']) }}
                </span>
              </template>
              <span v-else class="muted">—</span>
            </td>
            <td>
              <div v-for="(a, i) in q.allocations" :key="i" class="alloc-line">
                <code>{{ a['正向流水键'] }}</code>
                <span class="split">+{{ yuan(a['分摊分']) }}</span>
              </div>
              <span v-if="!q.allocations.length" class="muted">—</span>
            </td>
            <td>{{ q.settle_run_id }}</td>
            <td>{{ q.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="8">暂无隔离项（先跑归并）</td></tr>
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
.small { font-size: 11px; }
</style>
