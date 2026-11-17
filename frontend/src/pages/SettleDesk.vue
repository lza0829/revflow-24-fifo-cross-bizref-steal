<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getJson, postJson } from '../api'

const runs = ref([])
const proofs = ref([])
const selectedRun = ref(null)
const err = ref('')
const msg = ref('')
const busy = ref(false)

async function load() {
  runs.value = await getJson('/api/settle-runs')
  if (selectedRun.value == null && runs.value.length) {
    selectedRun.value = runs.value[0].id
  }
  if (selectedRun.value != null) {
    await loadProofs()
  }
}

async function loadProofs() {
  proofs.value = await getJson(`/api/allocation-proofs?settle_run_id=${selectedRun.value}`)
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch(selectedRun, async () => {
  try {
    await loadProofs()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function runSettle() {
  busy.value = true
  err.value = ''
  msg.value = ''
  try {
    const r = await postJson('/api/settle-runs')
    msg.value = `归并完成 #${r.id}：占用 ${(r.allocated_cents / 100).toFixed(2)}，净额 ${(r.net_cents / 100).toFixed(2)}，隔离 ${r.quarantine_count}，部分 ${r.partial_count}`
    selectedRun.value = r.id
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

const proofTotal = computed(() => proofs.value.reduce((s, p) => s + p['占用分'], 0))
const proofFailures = computed(() => proofs.value.filter((p) => p['结论'] === '失败').length)
</script>

<template>
  <div>
    <h1>归并台</h1>
    <p class="lead">对「待归并」正向与冲正跑一轮净额归并：按业务单匹配正向敞口并落余额；未消化部分视情况进隔离。</p>
    <p class="note-strip">匹配与余额写回在本趟完成；隔离项不计入账户余额。占用证明与占用表、敞口、余额同一事务落库。</p>
    <div class="row-actions">
      <button class="btn" :disabled="busy" @click="runSettle">跑净额归并</button>
    </div>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="msg">{{ err }}</p>
    <div class="panel">
      <h2>归并运行</h2>
      <table>
        <thead>
          <tr>
            <th>#</th><th>状态</th><th>正向</th><th>冲正</th><th>已归并</th><th>部分</th><th>隔离</th><th>占用（元）</th><th>净额（元）</th><th>结束</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.forward_count }}</td>
            <td>{{ r.reversal_count }}</td>
            <td>{{ r.settled_count }}</td>
            <td>{{ r.partial_count }}</td>
            <td>{{ r.quarantine_count }}</td>
            <td>{{ yuan(r.allocated_cents) }}</td>
            <td>{{ yuan(r.net_cents) }}</td>
            <td>{{ r.finished_at || '—' }}</td>
          </tr>
          <tr v-if="!runs.length"><td colspan="10">尚无归并运行</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h2>占用证明</h2>
        <label class="run-pick">
          归并运行
          <select v-model.number="selectedRun">
            <option v-for="r in runs" :key="r.id" :value="r.id">#{{ r.id }}</option>
          </select>
        </label>
      </div>
      <p class="proof-sum">
        证明占用合计 {{ yuan(proofTotal) }} 元 · 失败闭 {{ proofFailures }} 笔 · 共 {{ proofs.length }} 笔冲正
      </p>
      <table>
        <thead>
          <tr>
            <th>冲正流水键</th><th>业务单</th><th>冲正金额</th><th>占用分</th>
            <th>有序占用（FIFO 序 · 正向键 · 有序正向键）</th><th>结论</th><th>失败原因</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in proofs" :key="p['证明#'] + p['冲正流水键']">
            <td>{{ p['冲正流水键'] }}</td>
            <td>{{ p['业务单'] || '—' }}</td>
            <td>{{ yuan(p['冲正金额分']) }}</td>
            <td>{{ yuan(p['占用分']) }}</td>
            <td>
              <div v-for="s in p['占用']" :key="s['有序正向键']" class="alloc-line">
                <span class="rank">#{{ s['FIFO 序'] }}</span>
                <code>{{ s['正向流水键'] }}</code>
                <span class="split">+{{ yuan(s['占用分']) }}</span>
                <span class="ordered">{{ s['有序正向键'] }}</span>
              </div>
              <span v-if="!p['占用'].length" class="muted">—</span>
            </td>
            <td>
              <span class="tag" :class="{ on: p['结论'] === '占用', warn: p['结论'] === '部分占用', bad: p['结论'] === '失败' }">
                {{ p['结论'] }}
              </span>
            </td>
            <td>
              <span v-if="p['失败原因']" class="muted small">{{ p['失败原因'] }}</span>
              <span v-else class="muted">—</span>
            </td>
          </tr>
          <tr v-if="!proofs.length"><td colspan="7">该轮无占用证明</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 16px;
  flex-wrap: wrap;
}
.panel-head h2 { margin-bottom: 6px; }
.run-pick {
  font-family: var(--mono);
  font-size: 11px;
  color: var(--muted);
  display: flex;
  gap: 8px;
  align-items: center;
}
.run-pick select {
  font: inherit;
  padding: 4px 6px;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--ink);
}
.proof-sum {
  font-family: var(--mono);
  font-size: 12px;
  color: var(--muted);
  margin: 0 0 12px;
}
.alloc-line {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-family: var(--mono);
  font-size: 12px;
}
.alloc-line code { color: var(--accent); }
.rank { color: var(--muted); background: #eef2f7; padding: 0 4px; }
.ordered { color: var(--muted); font-size: 10.5px; }
.split { color: var(--ok); }
.muted { color: var(--muted); }
.small { font-size: 10.5px; }
</style>
