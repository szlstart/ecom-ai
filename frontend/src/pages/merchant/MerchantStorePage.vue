<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { adminGet, adminUpdate, requireAdminToken, type AdminStore } from '@/api/admin-catalog'
import { apiRequest, errorMessage } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface Money { minor_units: string; currency: string }
interface MerchantRevenue { gross_sales: Money; refunded_amount: Money; net_revenue: Money; paid_order_count: number }

const auth = useAdminAuthStore()
const router = useRouter()
const store = ref<AdminStore | null>(null)
const revenue = ref<MerchantRevenue | null>(null)
const loading = ref(true)
const saving = ref(false)
const deleting = ref(false)
const error = ref('')
const deleteError = ref('')
const notice = ref('')
const profile = reactive({ store_name: '', description: '', logo_file_id: '' })

function token() { return requireAdminToken(auth.accessToken) }
function money(value?: Money) { return `¥${(Number(value?.minor_units ?? 0) / 100).toFixed(2)}` }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const stores = (await adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=20', token())).data.items
    store.value = stores[0] ?? null
    if (store.value) {
      Object.assign(profile, { store_name: store.value.store_name, description: store.value.description ?? '', logo_file_id: '' })
      revenue.value = (await apiRequest<MerchantRevenue>(`/merchant/stores/${encodeURIComponent(store.value.store_id)}/revenue`, {}, token())).data
    }
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function save() {
  if (!store.value) return
  saving.value = true
  error.value = ''
  notice.value = ''
  try {
    const payload: Record<string, unknown> = { store_name: profile.store_name, description: profile.description || null }
    if (profile.logo_file_id) payload.logo_file_id = profile.logo_file_id
    store.value = (await adminUpdate<AdminStore>(`/admin/stores/${encodeURIComponent(store.value.store_id)}`, payload, token(), store.value.version)).data
    profile.logo_file_id = ''
    notice.value = '店铺资料已保存，用户端相关位置会显示最新店铺名称。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

async function deleteAccount() {
  if (!store.value || !confirm(`确定永久注销“${store.value.store_name}”及其商家账号吗？\n\n未产生订单时，店铺、商品和账号数据会立即从数据库删除，且无法恢复。`)) return
  deleting.value = true
  deleteError.value = ''
  try {
    await apiRequest('/merchant/account', { method: 'DELETE', body: JSON.stringify({ confirmation: 'DELETE_MY_STORE_AND_ACCOUNT' }) }, token())
    auth.clear()
    await router.replace('/merchant/login?deleted=1')
  } catch (cause) { deleteError.value = errorMessage(cause) }
  finally { deleting.value = false }
}

onMounted(load)
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">店铺设置</p><h1>店铺资料</h1><p>维护用户看到的公开信息，并查看真实订单汇总的营业额。</p></div><RouterLink v-if="store" :to="`/stores/${store.store_id}`" target="_blank">预览用户端店铺</RouterLink></header>
    <p v-if="notice" class="alert success" role="status">{{ notice }}</p>
    <PageState :loading="loading" :error="error" :empty="!loading && !store" empty-title="账号尚未绑定店铺" @retry="load">
      <template v-if="store">
        <div class="merchant-store-settings">
          <form class="card" @submit.prevent="save"><h2>基础资料</h2><label>店铺名称<input v-model.trim="profile.store_name" required minlength="2" maxlength="128" /></label><small>名称不能与其他店铺重复，可随时修改。保存后，用户端商品、收藏和历史订单中的店铺名会同步显示最新名称。</small><label>店铺简介<textarea v-model.trim="profile.description" maxlength="2000" rows="8" placeholder="介绍你的店铺、品牌理念和主营商品" /></label><AdminFileUpload purpose="store_logo" :business-context-id="store.store_id" label="更换店铺 Logo" @uploaded="profile.logo_file_id = $event" /><small v-if="profile.logo_file_id">新 Logo 已上传，保存资料后生效。</small><button :disabled="saving">{{ saving ? '正在保存…' : '保存店铺资料' }}</button></form>
          <aside class="card merchant-store-preview"><p class="eyebrow">营业额</p><div class="merchant-revenue-value">{{ money(revenue?.net_revenue) }}</div><p>累计实收减累计退款</p><dl><dt>累计实收</dt><dd>{{ money(revenue?.gross_sales) }}</dd><dt>累计退款</dt><dd>{{ money(revenue?.refunded_amount) }}</dd><dt>已支付订单</dt><dd>{{ revenue?.paid_order_count ?? 0 }} 笔</dd><dt>营业状态</dt><dd>{{ store.status === 'active' ? '营业中' : store.status }}</dd></dl></aside>
        </div>
        <article class="card danger-zone"><div><p class="eyebrow danger-text">不可恢复</p><h2>注销店铺账号</h2><p>仅未产生任何订单的店铺可以直接注销。确认后会永久删除店铺、商品、商家账号及其非交易数据。</p></div><button class="danger" type="button" :disabled="deleting" @click="deleteAccount">{{ deleting ? '正在注销…' : '注销店铺与账号' }}</button><p v-if="deleteError" class="alert error" role="alert">{{ deleteError }}</p></article>
      </template>
    </PageState>
  </section>
</template>
