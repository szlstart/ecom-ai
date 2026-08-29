<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { adminCommand, adminCreate, adminGet, adminUpdate, requireAdminToken, type AdminStorePolicy } from '@/api/admin-catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute(); const auth = useAdminAuthStore(); const storeId = String(route.params.storeId)
const items = ref<AdminStorePolicy[]>([]); const editing = ref<AdminStorePolicy | null>(null)
const loading = ref(true); const saving = ref(false); const error = ref(''); const message = ref('')
const form = reactive({ policy_type: 'return_policy', title: '', content: '', effective_at: '', expires_at: '', reason: '' })

function reset() { editing.value = null; Object.assign(form, { policy_type: 'return_policy', title: '', content: '', effective_at: '', expires_at: '', reason: '' }) }
function edit(item: AdminStorePolicy) { editing.value = item; Object.assign(form, { policy_type: item.policy_type, title: item.title, content: item.content, effective_at: localValue(item.effective_at), expires_at: localValue(item.expires_at), reason: '' }) }
function localValue(value: string | null) { return value ? value.slice(0, 16) : '' }
function iso(value: string) { return value ? new Date(value).toISOString() : null }

async function load() { loading.value = true; try { items.value = (await adminGet<AdminStorePolicy[]>(`/admin/stores/${encodeURIComponent(storeId)}/service-policies`, requireAdminToken(auth.accessToken))).data } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
async function save() {
  saving.value = true; error.value = ''; message.value = ''
  const payload = { title: form.title, content: form.content, effective_at: iso(form.effective_at), expires_at: iso(form.expires_at) }
  try {
    const token = requireAdminToken(auth.accessToken)
    if (editing.value) { await adminUpdate(`/admin/stores/${encodeURIComponent(storeId)}/service-policies/${encodeURIComponent(editing.value.policy_id)}`, payload, token, editing.value.version); message.value = '政策草稿已更新。' }
    else { await adminCreate(`/admin/stores/${encodeURIComponent(storeId)}/service-policies`, { ...payload, policy_type: form.policy_type }, token, 'store-policy-create'); message.value = '政策草稿已创建。'; reset() }
    await load()
  } catch (cause) { error.value = errorMessage(cause) } finally { saving.value = false }
}
async function command(item: AdminStorePolicy, action: 'publications' | 'withdrawals') {
  const reason = form.reason.trim()
  if (reason.length < 2) { error.value = '发布或撤回前，请在右侧填写至少 2 个字的操作原因。'; return }
  saving.value = true; error.value = ''
  try { await adminCommand(`/admin/stores/${encodeURIComponent(storeId)}/service-policies/${encodeURIComponent(item.policy_id)}/${action}`, { reason }, requireAdminToken(auth.accessToken), item.version, `store-policy-${action}`); message.value = action === 'publications' ? '政策版本已发布。' : '政策版本已撤回。'; await load() }
  catch (cause) { error.value = errorMessage(cause) } finally { saving.value = false }
}
onMounted(() => { reset(); void load() })
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">店铺运营</p><h1>服务政策</h1><p class="muted">编辑草稿不会改变公开版本；发布和撤回均写入版本事件。</p></div><RouterLink :to="`/admin/stores/${storeId}`">返回店铺</RouterLink></header><p v-if="message" class="alert success">{{ message }}</p><p v-if="error" class="alert error">{{ error }}</p><div class="admin-split"><PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="暂无服务政策"><div class="card-list"><article v-for="item in items" :key="item.policy_id" class="card"><div class="card-heading"><div><h2>{{ item.title }}</h2><p class="muted">{{ item.policy_type }} · 版本 {{ item.policy_version }}</p></div><span class="badge">{{ item.status }}</span></div><p class="policy-preview">{{ item.content }}</p><p class="muted">生效：{{ item.effective_at || '发布时' }} 至 {{ item.expires_at || '长期' }}</p><div class="actions"><button type="button" class="secondary small" :disabled="item.status !== 'draft'" @click="edit(item)">编辑草稿</button><button v-if="auth.has('store_policies:publish')" type="button" class="small" :disabled="item.status !== 'draft' || saving" @click="command(item, 'publications')">发布</button><button v-if="auth.has('store_policies:publish')" type="button" class="danger small" :disabled="item.status !== 'published' || saving" @click="command(item, 'withdrawals')">撤回</button></div></article></div></PageState><form class="card admin-editor" @submit.prevent="save"><h2>{{ editing ? '编辑政策草稿' : '新建政策草稿' }}</h2><label>政策类型<input v-model.trim="form.policy_type" required pattern="[a-z][a-z0-9_]{1,31}" :disabled="Boolean(editing)" /></label><label>标题<input v-model.trim="form.title" required maxlength="128" /></label><label>政策内容<textarea v-model.trim="form.content" required maxlength="100000" rows="10" /></label><label>计划生效时间<input v-model="form.effective_at" type="datetime-local" /></label><label>计划失效时间<input v-model="form.expires_at" type="datetime-local" /></label><label>发布/撤回原因<textarea v-model.trim="form.reason" maxlength="500" placeholder="执行明确命令时使用" /></label><div class="actions"><button :disabled="saving">{{ saving ? '保存中…' : '保存草稿' }}</button><button v-if="editing" type="button" class="secondary" @click="reset">取消编辑</button></div></form></div></section></template>
