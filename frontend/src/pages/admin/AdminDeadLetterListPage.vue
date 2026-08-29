<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listAdminDeadLetters, type DeadLetterEvent } from '@/api/admin-events'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), items = ref<DeadLetterEvent[]>([]), loading = ref(true), error = ref(''), filters = reactive({ status: '', event_type: '' })
async function load() { loading.value = true; error.value = ''; try { items.value = (await listAdminDeadLetters(filters, auth.accessToken!)).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
function dateTime(value: string) { return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'short' }).format(new Date(value)) }
onMounted(load)
</script>
<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">事件治理</p><h1>死信事件</h1><p class="muted">逐条检查、预览和审批；不提供批量重放，也不允许修改原 Payload。</p></div></header><form class="filter-bar" @submit.prevent="load"><label>状态<select v-model="filters.status"><option value="">全部</option><option value="open">待处理</option><option value="replaying">重放中</option><option value="resolved">已解决</option><option value="ignored">已忽略</option></select></label><label>事件类型<input v-model.trim="filters.event_type" maxlength="128" /></label><button>查询</button></form><PageState :loading="loading" :error="error" :empty="!loading && !error && items.length === 0" empty-title="没有匹配死信" @retry="load"><div class="table-wrap"><table><thead><tr><th>死信</th><th>事件/来源</th><th>范围</th><th>失败</th><th>状态</th><th>最后失败</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.dead_letter_id"><td><strong>{{ item.dead_letter_id }}</strong><small>Schema v{{ item.schema_version }}</small></td><td>{{ item.event_type }}<small>{{ item.source_type }} · {{ item.source_id }}</small></td><td>{{ item.scope_type }}:{{ item.scope_id }}</td><td>{{ item.last_error_code }}<small>累计 {{ item.failure_count }} 次</small></td><td><span class="badge">{{ item.status }}</span></td><td>{{ dateTime(item.last_failed_at) }}</td><td><RouterLink :to="`/admin/system/dead-letter-events/${item.dead_letter_id}`">检查</RouterLink></td></tr></tbody></table></div></PageState></section></template>
