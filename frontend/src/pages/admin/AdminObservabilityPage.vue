<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { getObservabilitySummary, type ObservabilitySummary } from '@/api/admin-observability'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), data = ref<ObservabilitySummary | null>(null), loading = ref(true), error = ref('')
async function load() { loading.value = true; try { if (!auth.accessToken) throw new Error('管理端登录已失效。'); data.value = (await getObservabilitySummary(auth.accessToken)).data } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
onMounted(load)
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">Telemetry Overview</p><h1>可观测性</h1><p class="muted">此页只展示当前进程的低基数聚合；链路与日志分别进入 Tempo、Loki，正文和敏感参数不进入观测属性。</p></div><button class="secondary" @click="load">刷新</button></header><PageState :loading="loading" :error="error" :empty="!loading && !data" empty-title="暂无观测数据" @retry="load"><template v-if="data"><div class="metric-grid"><article v-for="(value, key) in data.metrics" :key="key" class="card"><small>{{ key }}</small><strong>{{ value }}</strong></article></div><div class="card"><dl class="detail-list"><dt>Trace 后端</dt><dd>{{ data.trace_backend }}</dd><dt>Log 后端</dt><dd>{{ data.log_backend }}</dd><dt>统计窗口</dt><dd>{{ data.window }}</dd><dt>包含敏感正文</dt><dd>否</dd></dl></div></template></PageState></section></template>
