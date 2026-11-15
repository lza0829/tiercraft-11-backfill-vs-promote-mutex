<script setup>
import { onMounted, ref } from 'vue'
import { getJson } from '../api'

const rows = ref([])
const err = ref('')

onMounted(async () => {
  try {
    rows.value = await getJson('/api/domains')
  } catch (e) {
    err.value = String(e.message || e)
  }
})

function availability(d) {
  if (!d.available) return { text: '不可用待重跑', cls: 'fail' }
  if (d.in_flight_lock) return { text: '在途', cls: 'flight' }
  return { text: '可用', cls: 'ok' }
}
</script>
<template>
  <div>
    <h2>主题域</h2>
    <p class="lead">每个主题域对应一套贴源夹具与业务键，分层建设围绕主题域推进。</p>
    <p v-if="err" class="err">{{ err }}</p>
    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>编号</th><th>名称</th><th>业务键</th><th>夹具文件</th>
            <th>可用性</th><th>在途类型</th><th>对齐状态</th><th>不可用原因 / 说明</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="d in rows" :key="d.id">
            <td>{{ d.id }}</td>
            <td>{{ d.name }}</td>
            <td>{{ d.biz_key }}</td>
            <td>{{ d.fixture_file }}</td>
            <td :class="availability(d).cls">{{ availability(d).text }}</td>
            <td>{{ d.in_flight_lock || '—' }}</td>
            <td :class="d.alignment === '已对齐' ? 'ok' : 'warn'">{{ d.alignment }}</td>
            <td>{{ d.unavailable_reason || d.description }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
