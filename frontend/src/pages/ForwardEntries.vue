<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/entries?kind=正向')
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
    <h1>正向流水</h1>
    <p class="lead">已装载的正向入账；归并后状态变为「已归并」，并显示 FIFO 已占用与剩余敞口。</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>流水键</th><th>账户</th><th>金额（元）</th><th>业务单</th>
            <th>已占用</th><th>剩余敞口</th><th>状态</th><th>入账时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="e in rows" :key="e.id">
            <td>{{ e.entry_key }}</td>
            <td>{{ e.account_code }}</td>
            <td>{{ yuan(e.amount_cents) }}</td>
            <td>{{ e.biz_ref }}</td>
            <td>
              <template v-if="e.allocated_cents > 0">
                {{ yuan(e.allocated_cents) }}
                <div class="alloc-hint">
                  <span v-for="a in e.allocations" :key="a.reversal_entry_id" class="chip">
                    {{ a.reversal_entry_key }}
                  </span>
                </div>
              </template>
              <span v-else class="muted">0.00</span>
            </td>
            <td>{{ yuan(e.remaining_cents) }}</td>
            <td><span class="tag" :class="{ on: e.status === '已归并' }">{{ e.status }}</span></td>
            <td>{{ e.booked_at }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.alloc-hint { margin-top: 4px; display: flex; flex-wrap: wrap; gap: 4px; }
.chip {
  font-family: var(--mono);
  font-size: 10.5px;
  padding: 1px 5px;
  background: #e8eef5;
  color: var(--accent);
}
.muted { color: var(--muted); }
</style>
