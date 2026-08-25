<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import {
  createKnowledgeDocument,
  listKnowledgeDocuments,
  publishKnowledgeDocument,
  withdrawKnowledgeDocument,
  type KnowledgeDocument,
} from '@/api/admin-ai'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<KnowledgeDocument[]>([]), loading = ref(true), error = ref(''), notice = ref('')
const form = reactive({ scope_type: 'platform', scope_id: 'platform', title: '', safe_text: '' })
const token = () => { if (!auth.accessToken) throw new Error('管理会话不可用'); return auth.accessToken }
async function load() { loading.value = true; error.value = ''; try { items.value = (await listKnowledgeDocuments(token())).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
async function create() { error.value = ''; notice.value = ''; try { await createKnowledgeDocument(form, token()); notice.value = '知识文档草稿已创建。'; form.title = ''; form.safe_text = ''; await load() } catch (cause) { error.value = errorMessage(cause) } }
async function publish(item: KnowledgeDocument) { if (!window.confirm('确认发布并创建影子索引任务吗？旧索引会持续服务到切换完成。')) return; try { const result = (await publishKnowledgeDocument(item.document_id, token())).data; notice.value = `索引任务 ${result.index_job_no ?? ''} 已创建。`; await load() } catch (cause) { error.value = errorMessage(cause) } }
async function withdraw(item: KnowledgeDocument) { if (!window.confirm('确认撤回文档并立即从检索范围删除吗？')) return; try { await withdrawKnowledgeDocument(item.document_id, token()); notice.value = '文档已撤回。'; await load() } catch (cause) { error.value = errorMessage(cause) } }
onMounted(load)
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">RAG · 权限隔离</p><h1>知识库文档</h1><p class="muted">MySQL 保存命令与文档权威状态，PostgreSQL 执行切片和索引；新索引成功前不替换旧版本。</p></div><RouterLink class="button-link secondary" to="/admin/knowledge/indexing-jobs">查看索引任务</RouterLink></header><p v-if="error" class="alert error">{{ error }}</p><p v-if="notice" class="alert success">{{ notice }}</p><div class="admin-split"><PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="暂无知识文档"><div class="card-list"><article v-for="item in items" :key="item.document_id" class="card"><div class="card-heading"><div><h2><RouterLink :to="`/admin/knowledge/documents/${encodeURIComponent(item.document_id)}`">{{ item.title }}</RouterLink></h2><p>{{ item.scope_type }} / {{ item.scope_id }} · {{ item.content_version }}</p></div><span class="badge">{{ item.status }}</span></div><div class="actions"><RouterLink class="button-link secondary" :to="`/admin/knowledge/documents/${encodeURIComponent(item.document_id)}`">查看详情</RouterLink><button v-if="auth.has('knowledge:publish')" @click="publish(item)">发布/重建索引</button><button v-if="auth.has('knowledge:manage')" class="secondary" @click="withdraw(item)">撤回</button></div></article></div></PageState><form v-if="auth.has('knowledge:manage')" class="card admin-editor sticky-editor" @submit.prevent="create"><h2>新建安全文本草稿</h2><label>范围<select v-model="form.scope_type" @change="form.scope_id = form.scope_type === 'platform' ? 'platform' : ''"><option value="platform">平台</option><option value="store">店铺</option></select></label><label>Scope ID<input v-model="form.scope_id" required maxlength="64" placeholder="平台填 platform；店铺填公开 store_id"></label><label>标题<input v-model="form.title" required maxlength="255"></label><label>已清洗正文<textarea v-model="form.safe_text" required maxlength="200000"></textarea></label><p class="muted">不得粘贴 Secret、个人敏感信息或未通过安全处理的原始文件内容。</p><button>保存草稿</button></form></div></section></template>
