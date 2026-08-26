<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { adminGet, adminUpdate, requireAdminToken, type AdminStore } from '@/api/admin-catalog'
import { errorMessage } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const store = ref<AdminStore | null>(null)
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const profile = reactive({ store_name: '', description: '', logo_file_id: '' })
const canRename = computed(() => !store.value?.store_name_change_available_at || new Date(store.value.store_name_change_available_at).getTime() <= Date.now())
const renameAvailableLabel = computed(() => store.value?.store_name_change_available_at ? new Date(store.value.store_name_change_available_at).toLocaleString('zh-CN') : '')
function token() { return requireAdminToken(auth.accessToken) }

async function load() {
  loading.value = true; error.value = ''
  try { const stores = (await adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=20', token())).data.items; store.value = stores[0] ?? null; if (store.value) Object.assign(profile, { store_name: store.value.store_name, description: store.value.description ?? '', logo_file_id: '' }) }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function save() {
  if (!store.value) return
  saving.value = true; error.value = ''; notice.value = ''
  try { const payload: Record<string, unknown> = { store_name: profile.store_name, description: profile.description || null }; if (profile.logo_file_id) payload.logo_file_id = profile.logo_file_id; store.value = (await adminUpdate<AdminStore>(`/admin/stores/${encodeURIComponent(store.value.store_id)}`, payload, token(), store.value.version)).data; profile.logo_file_id = ''; notice.value = '店铺资料已保存，用户端店铺页会使用最新内容。' }
  catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">店铺设置</p><h1>店铺资料</h1><p>维护用户在店铺首页和商品页看到的公开信息。</p></div><RouterLink v-if="store" :to="`/stores/${store.store_id}`" target="_blank">预览用户端店铺</RouterLink></header>
    <p v-if="notice" class="alert success">{{ notice }}</p>
    <PageState :loading="loading" :error="error" :empty="!loading && !store" empty-title="账号尚未绑定店铺" @retry="load"><div v-if="store" class="merchant-store-settings"><form class="card" @submit.prevent="save"><h2>基础资料</h2><label>店铺名称<input v-model.trim="profile.store_name" required minlength="2" maxlength="128" :disabled="!canRename" /></label><small v-if="canRename">名称不能与其他店铺重复，修改成功后 7 天内不能再次修改。</small><small v-else>店铺名处于修改冷却期，下次可修改时间：{{ renameAvailableLabel }}</small><label>店铺简介<textarea v-model.trim="profile.description" maxlength="2000" rows="8" placeholder="介绍你的店铺、品牌理念和主营商品" /></label><AdminFileUpload purpose="store_logo" :business-context-id="store.store_id" label="更换店铺 Logo" @uploaded="profile.logo_file_id = $event" /><small v-if="profile.logo_file_id">新 Logo 已上传，保存资料后生效。</small><button :disabled="saving">{{ saving ? '正在保存…' : '保存店铺资料' }}</button></form><aside class="card merchant-store-preview"><p class="eyebrow">当前公开状态</p><div class="merchant-preview-logo">{{ store.store_name.slice(0, 1) }}</div><h2>{{ profile.store_name }}</h2><p>{{ profile.description || '暂未填写店铺简介' }}</p><dl><dt>营业状态</dt><dd>{{ store.status === 'active' ? '营业中' : store.status }}</dd><dt>店铺评分</dt><dd>{{ store.rating_score }}（{{ store.rating_count }} 条评价）</dd><dt>关注人数</dt><dd>{{ store.follower_count }}</dd><dt>累计销量</dt><dd>{{ store.sales_count }}</dd></dl></aside></div></PageState>
  </section>
</template>
