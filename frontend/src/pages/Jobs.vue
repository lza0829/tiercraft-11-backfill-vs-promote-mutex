<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { getJson, postJson } from '../api'

const domains = ref([])
const domainId = ref(1)
const jobs = ref([])
const locks = ref([])
const bizDay = ref('2026-11-10')
const err = ref('')
const info = ref('')
const busy = ref(false)
let timer = null

const currentDomain = computed(() =>
  domains.value.find((d) => d.id === domainId.value) || null
)
const currentLock = computed(() =>
  locks.value.find((l) => l.domain_id === domainId.value) || null
)
const hasInFlight = computed(() => locks.value.length > 0)

async function refresh() {
  const [jobRows, lockRows] = await Promise.all([
    getJson(`/api/jobs?domain_id=${domainId.value}`),
    getJson(`/api/locks?domain_id=${domainId.value}`),
  ])
  jobs.value = jobRows
  locks.value = lockRows
}

async function loadDomains() {
  domains.value = await getJson('/api/domains')
  if (domains.value.length) domainId.value = domains.value[0].id
}

onMounted(async () => {
  try {
    await loadDomains()
    await refresh()
    // 在途期间自动轮询：在途态与结果均以数据库为同源，刷新页面不丢
    timer = setInterval(async () => {
      if (hasInFlight.value) {
        try {
          await refresh()
        } catch {
          /* 轮询失败忽略，下一轮重试 */
        }
      }
    }, 2000)
  } catch (e) {
    err.value = String(e.message || e)
  }
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

async function switchDomain() {
  err.value = ''
  info.value = ''
  await refresh()
}

async function run(kind) {
  busy.value = true
  err.value = ''
  info.value = ''
  try {
    if (kind === '明细') {
      await postJson('/api/jobs/paste-to-detail', { domain_id: domainId.value })
    } else if (kind === '汇总') {
      await postJson('/api/jobs/detail-to-summary', { domain_id: domainId.value })
    } else {
      await postJson('/api/jobs/backfill', {
        domain_id: domainId.value,
        biz_day: bizDay.value,
      })
    }
    info.value = '任务已受理，在途锁与任务结果由后端数据库统一记录'
    await refresh()
  } catch (e) {
    // 互斥冲突（409）等错误：后端已落「互斥失败」记录，刷新列表即可见
    err.value = String(e.message || e)
    await refresh()
  } finally {
    busy.value = false
  }
}

function statusClass(status) {
  if (status === '成功') return 'ok'
  if (status === '在途') return 'flight'
  if (status === '失败') return 'fail'
  return 'warn'
}

function conflictText(j) {
  if (j.status === '互斥失败') {
    return `${j.conflict_holder_label || '对端在途任务'}（任务#${j.conflict_holder_job_id}）`
  }
  if (j.status === '在途') {
    return j.step_label || j.step
  }
  return '—'
}
</script>
<template>
  <div>
    <h2>分层任务</h2>
    <p class="lead">
      同一主题域内，提升（贴源到明细、明细到汇总）与定点回刷持可查询的在途锁互斥；
      冲突由后端拒绝并记录，按钮不禁用也无法绕过。
    </p>
    <div class="toolbar">
      <label>
        主题域
        <select v-model.number="domainId" @change="switchDomain">
          <option v-for="d in domains" :key="d.id" :value="d.id">{{ d.name }}</option>
        </select>
      </label>
      <label>
        回刷业务日
        <input v-model="bizDay" placeholder="YYYY-MM-DD" class="day-input" />
      </label>
      <button :disabled="busy" @click="run('明细')">跑贴源到明细</button>
      <button class="secondary" :disabled="busy" @click="run('汇总')">跑明细到汇总</button>
      <button class="secondary" :disabled="busy" @click="run('回刷')">定点回刷该业务日</button>
      <button class="secondary" :disabled="busy" @click="refresh">刷新</button>
    </div>

    <div v-if="currentDomain && !currentDomain.available" class="err">
      主题域当前不可用待重跑：{{ currentDomain.unavailable_reason || '上趟任务失败，已回滚并标记' }}
    </div>
    <div v-if="currentLock" class="flight-banner">
      在途：<strong>{{ currentLock.lock_type_label }}</strong>
      （任务#{{ currentLock.job_id }}，{{ currentLock.created_at }} 起持有在途锁）
    </div>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="info" class="ok">{{ info }}</p>

    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>编号</th><th>步骤</th><th>结果</th><th>在途类型 / 互斥对端</th>
            <th>原因与说明</th><th>业务日</th><th>输入</th><th>写入</th><th>剔除</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="j in jobs" :key="j.id">
            <td>{{ j.id }}</td>
            <td>{{ j.step_label || j.step }}</td>
            <td :class="statusClass(j.status)">{{ j.status }}</td>
            <td>{{ conflictText(j) }}</td>
            <td>{{ j.message }}</td>
            <td>{{ j.target_biz_day || '—' }}</td>
            <td>{{ j.in_count }}</td>
            <td>{{ j.out_count }}</td>
            <td>{{ j.drop_count }}</td>
            <td>{{ j.finished_at || j.started_at || j.created_at }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!jobs.length" class="muted">还没有任务记录。</p>
    </div>
  </div>
</template>
