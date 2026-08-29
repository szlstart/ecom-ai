<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import { adminCreate, adminGet, adminQuery, requireAdminToken, type AdminInventory } from '@/api/admin-catalog'
import { ApiProblem, errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const props = withDefaults(defineProps<{ portal?: 'admin' | 'merchant' }>(), { portal: 'admin' })

const auth = useAdminAuthStore()
const items = ref<AdminInventory[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const message = ref('')
const q = ref('')
const storeId = ref('')
const selected = ref<AdminInventory | null>(null)
const form = reactive({ delta: 0, reason_code: 'STOCKTAKE_CORRECTION', reason: '', reference_no: '' })
const projectedOnHand = computed(() => (selected.value?.on_hand_quantity ?? 0) + Number(form.delta || 0))
const projectedAvailable = computed(() => (selected.value?.available_quantity ?? 0) + Number(form.delta || 0))

async function load() {
  loading.value = true; error.value = ''
  try {
    items.value = (await adminGet<{ items: AdminInventory[] }>(`/admin/inventories${adminQuery({ q: q.value.trim(), store_id: props.portal === 'admin' ? storeId.value.trim() : '', limit: 100 })}`, requireAdminToken(auth.accessToken))).data.items
    if (selected.value) selected.value = items.value.find((item) => item.sku_id === selected.value?.sku_id) ?? null
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function choose(item: AdminInventory) {
  selected.value = item
  Object.assign(form, { delta: 0, reason_code: 'STOCKTAKE_CORRECTION', reason: '', reference_no: '' })
}

async function adjust() {
  if (!selected.value) return
  saving.value = true; error.value = ''; message.value = ''
  try {
    await adminCreate('/admin/inventory-adjustments', {
      sku_id: selected.value.sku_id,
      on_hand_delta: Number(form.delta),
      reason_code: form.reason_code,
      reason: form.reason,
      reference_no: form.reference_no,
      expected_version: selected.value.version,
    }, requireAdminToken(auth.accessToken), 'inventory-adjust')
    message.value = `库存已调整 ${form.delta > 0 ? '+' : ''}${form.delta}。`
    await load()
    if (selected.value) choose(selected.value)
  } catch (cause) {
    if (cause instanceof ApiProblem && (cause.body.status === 409 || cause.body.status === 412)) {
      const preserved = { ...form }
      await load()
      Object.assign(form, preserved)
      error.value = '库存已被其他操作更新。已刷新最新数量并保留你的输入，请重新核对后确认。'
    } else error.value = errorMessage(cause)
  } finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading"><div><p class="eyebrow">商品与库存</p><h1>库存调整</h1><p class="muted">只能提交有来源的增减量；预占量和安全库存不在此页面直接修改。</p></div></header>
    <form class="filter-bar" @submit.prevent="load"><label>搜索 SKU / 商品<input v-model="q" maxlength="100" /></label><label v-if="props.portal === 'admin'">店铺公开 ID<input v-model="storeId" maxlength="40" placeholder="sto_…" /></label><button>查询</button></form>
    <p v-if="message" class="alert success" aria-live="polite">{{ message }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div class="admin-split inventory-layout">
      <PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="没有匹配的库存">
        <div class="table-wrap"><table><thead><tr><th>SKU / 商品</th><th v-if="props.portal === 'admin'">店铺</th><th>现有/预占</th><th>可售</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.sku_id" :class="{ selected: selected?.sku_id === item.sku_id }"><td><strong>{{ item.sku_name }}</strong><small>{{ item.product_name }} · {{ item.sku_id }}</small></td><td v-if="props.portal === 'admin'">{{ item.store_name }}</td><td>{{ item.on_hand_quantity }} / {{ item.reserved_quantity }}</td><td>{{ item.available_quantity }}</td><td><span class="badge">{{ item.status }}</span></td><td><button type="button" class="secondary small" @click="choose(item)">调整</button></td></tr></tbody></table></div>
      </PageState>
      <form v-if="selected" class="card admin-editor sticky-editor" @submit.prevent="adjust"><h2>调整 {{ selected.sku_name }}</h2><p class="muted">当前版本 v{{ selected.version }} · {{ selected.sku_id }}</p><dl class="preview-grid"><div><dt>当前现有</dt><dd>{{ selected.on_hand_quantity }}</dd></div><div><dt>调整后现有</dt><dd>{{ projectedOnHand }}</dd></div><div><dt>当前可售</dt><dd>{{ selected.available_quantity }}</dd></div><div><dt>调整后可售</dt><dd>{{ projectedAvailable }}</dd></div></dl><p v-if="projectedOnHand < selected.reserved_quantity || projectedAvailable < 0" class="alert error">该调整会使库存低于已预占或安全库存，服务端将拒绝。</p><label>调整量（有符号）<input v-model.number="form.delta" type="number" required /></label><label>原因码<input v-model.trim="form.reason_code" required pattern="[A-Z][A-Z0-9_]{1,63}" maxlength="64" /></label><label>业务单号<input v-model.trim="form.reference_no" required minlength="2" maxlength="64" /></label><label>调整说明<textarea v-model.trim="form.reason" required minlength="2" maxlength="500" /></label><button :disabled="saving || form.delta === 0">{{ saving ? '提交中…' : '确认调整' }}</button></form>
      <aside v-else class="card empty-detail"><h2>选择一个 SKU</h2><p>选择列表中的库存记录后，可预览调整前后数量。</p></aside>
    </div>
  </section>
</template>
