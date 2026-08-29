<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { cancelKnowledgeIndexJob, getKnowledgeIndexJob, type KnowledgeIndexJob } from '@/api/admin-ai'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { confirmAction } from '@/composables/confirmation'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), route = useRoute(), item = ref<KnowledgeIndexJob | null>(null), loading = ref(true), error = ref('')
const id = String(route.params.jobId)
async function load() { loading.value = true; try { item.value = (await getKnowledgeIndexJob(id, auth.accessToken!)).data } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
async function cancel() { if (!await confirmAction('确认请求取消 PostgreSQL 索引子任务吗？', { tone: 'danger' })) return; try { item.value = (await cancelKnowledgeIndexJob(id, auth.accessToken!)).data } catch (cause) { error.value = errorMessage(cause) } }
onMounted(load)
</script>
<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">索引核对</p><h1>知识索引任务</h1><p class="muted">父任务 {{ id }}</p></div><RouterLink class="button-link secondary" to="/admin/knowledge/indexing-jobs">返回列表</RouterLink></header><PageState :loading="loading" :error="error" :empty="!loading && !item" empty-title="任务不存在" @retry="load"><article v-if="item" class="card wide-editor"><dl class="detail-list"><dt>MySQL 命令</dt><dd>{{ item.command_job_id }}</dd><dt>PostgreSQL 子任务</dt><dd>{{ item.job_id }}</dd><dt>状态</dt><dd><span class="badge">{{ item.status }}</span></dd><dt>进度</dt><dd>{{ item.progress }}%</dd><dt>错误码</dt><dd>{{ item.error_code ?? '-' }}</dd></dl><button v-if="auth.has('knowledge:manage') && ['queued','running'].includes(item.status)" class="danger" @click="cancel">取消任务</button></article></PageState></section></template>
