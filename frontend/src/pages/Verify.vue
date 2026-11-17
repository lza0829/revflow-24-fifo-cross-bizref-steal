<script setup>
import { computed, onMounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const rows = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(false)
const expanded = ref(null)

async function load() {
  rows.value = await getJson('/api/verify-reports')
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runVerify() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/verify-reports')
    msg.value = r.consistent
      ? `互证一致：净额 ${(r.ledger_net_cents / 100).toFixed(2)} = 余额合计，占用 ${r.detail['全量占用分'] / 100} 元`
      : `互证不一致：流水 ${(r.ledger_net_cents / 100).toFixed(2)} vs 余额 ${(r.balance_sum_cents / 100).toFixed(2)}`
    await load()
    if (rows.value.length) expanded.value = rows.value[0].id
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function toggle(id) {
  expanded.value = expanded.value === id ? null : id
}

const expandedReport = computed(() => rows.value.find((r) => r.id === expanded.value))
</script>

<template>
  <div>
    <h1>净额互证</h1>
    <p class="lead">
      核对「正向 − FIFO 占用 = 账户余额」，并列出每笔冲正的 FIFO 占用明细；占用、敞口、余额与报告同源。
    </p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runVerify">跑净额互证</button>
    </div>
    <p v-if="msg" :class="msg.includes('一致') && !msg.includes('不一致') ? 'ok' : 'msg'">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>互证报告</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>归并#</th><th>流水净额</th><th>余额合计</th><th>隔离数</th><th>一致</th><th>时间</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.settle_run_id }}</td>
            <td>{{ yuan(r.ledger_net_cents) }}</td>
            <td>{{ yuan(r.balance_sum_cents) }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>
              <span class="tag" :class="r.consistent ? 'on' : 'bad'">
                {{ r.consistent ? '是' : '否' }}
              </span>
            </td>
            <td>{{ r.created_at }}</td>
            <td>
              <button class="link-btn" @click="toggle(r.id)">
                {{ expanded === r.id ? '收起' : '占用明细' }}
              </button>
            </td>
          </tr>
          <tr v-if="!rows.length"><td colspan="8">尚无报告</td></tr>
        </tbody>
      </table>
    </div>

    <div v-if="expandedReport" class="panel">
      <h2>报告 #{{ expandedReport.id }} · FIFO 占用明细</h2>
      <div class="checks">
        <span
          v-for="(ok, name) in expandedReport.detail['校验']"
          :key="name"
          class="tag"
          :class="ok ? 'on' : 'bad'"
        >{{ name }}</span>
      </div>
      <p class="totals">
        正向 {{ yuan(expandedReport.detail['全量正向分']) }}
        − 占用 {{ yuan(expandedReport.detail['全量占用分']) }}
        = 净额 {{ yuan(expandedReport.detail['全量流水净额分']) }}
        ；余额 {{ yuan(expandedReport.detail['全量账户余额分']) }}
        ；剩余敞口 {{ yuan(expandedReport.detail['全量剩余敞口分']) }}
      </p>
      <table>
        <thead>
          <tr>
            <th>冲正流水键</th><th>账户</th><th>业务单</th><th>冲正金额</th>
            <th>已占用</th><th>未消化</th><th>状态</th><th>FIFO 占用（原单键 + 分摊）</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in expandedReport.detail['每笔冲正占用']" :key="r['冲正流水键']">
            <td>{{ r['冲正流水键'] }}</td>
            <td>{{ r['账户'] }}</td>
            <td>{{ r['业务单'] || '—' }}</td>
            <td>{{ yuan(r['冲正金额分']) }}</td>
            <td>{{ yuan(r['已占用分']) }}</td>
            <td>
              <span v-if="r['未消化分'] > 0" class="tag warn">{{ yuan(r['未消化分']) }}</span>
              <span v-else class="muted">0.00</span>
            </td>
            <td>
              <span class="tag" :class="{ on: r['状态'] === '已归并', bad: r['状态'] === '已隔离', warn: r['状态'] === '部分归并' }">
                {{ r['状态'] }}
              </span>
              <div v-if="r['隔离原因']" class="muted small">{{ r['隔离原因'] }}</div>
            </td>
            <td>
              <div v-for="a in r['占用']" :key="a['正向流水键']" class="alloc-line">
                <code>{{ a['正向流水键'] }}</code>
                <span class="split">+{{ yuan(a['分摊分']) }}</span>
                <span class="muted small">{{ a['正向入账时间'] }}</span>
              </div>
              <span v-if="!r['占用'].length" class="muted">—</span>
            </td>
          </tr>
        </tbody>
      </table>

      <h3>正向敞口</h3>
      <table>
        <thead>
          <tr><th>正向流水键</th><th>账户</th><th>业务单</th><th>金额</th><th>已占用</th><th>剩余敞口</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in expandedReport.detail['正向敞口']" :key="e['正向流水键']">
            <td>{{ e['正向流水键'] }}</td>
            <td>{{ e['账户'] }}</td>
            <td>{{ e['业务单'] || '—' }}</td>
            <td>{{ yuan(e['金额分']) }}</td>
            <td>{{ yuan(e['已占用分']) }}</td>
            <td>{{ yuan(e['剩余敞口分']) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.link-btn {
  appearance: none;
  background: none;
  border: none;
  color: var(--accent);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  padding: 0;
  text-decoration: underline;
}
.checks { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
.totals {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  margin: 0 0 14px;
}
h3 {
  font-family: var(--display);
  font-size: 15px;
  letter-spacing: 0.08em;
  margin: 20px 0 8px;
}
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
.small { font-size: 10.5px; }
</style>
