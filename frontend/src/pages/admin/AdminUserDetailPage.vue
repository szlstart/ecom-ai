<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import areaData from 'china-area-data'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { formatMoney, type ProductCardData, type StoreData } from '@/api/catalog'
import type { CartData } from '@/api/cart'
import { adjustAdminUserWallet, deleteAdminUser, getAdminUserWorkspace, replaceAdminUserPassword, updateAdminUser, type AdminUserSummary, type AdminUserWorkspace } from '@/api/admin-users'
import { ApiProblem, apiRequest, createIdempotencyKey, errorMessage, resolveApiAssetUrl } from '@/api/http'
import { listAdminOrders, type AdminOrderSummary, type OrderAction, type OrderSummary } from '@/api/orders'
import PageState from '@/components/PageState.vue'
import { confirmAction } from '@/composables/confirmation'
import { useAdminAuthStore } from '@/stores/admin-auth'
import { formatChinaRegion } from '@/utils/china-regions'

interface Address { address_id: string; recipient_name: string; phone: string; province_code: string; city_code: string; district_code: string; address: string; is_default: boolean; version: number }
interface RegionOption { code: string; name: string }
type AddressField = 'recipient_name' | 'phone' | 'province_code' | 'city_code' | 'district_code' | 'address'

const excludedProvinceCodes = new Set(['810000', '820000'])
const provinces = Object.entries(areaData['86'] ?? {})
  .filter(([code]) => !excludedProvinceCodes.has(code))
  .map(([code, name]) => ({ code, name }))

const route = useRoute(), router = useRouter(), auth = useAdminAuthStore(), userId = String(route.params.userId)
const user = ref<AdminUserSummary | null>(null), workspace = ref<AdminUserWorkspace | null>(null)
const addresses = ref<Address[]>([]), products = ref<ProductCardData[]>([]), stores = ref<StoreData[]>([]), cart = ref<CartData | null>(null), orders = ref<AdminOrderSummary[]>([])
const loading = ref(true), busy = ref(false), error = ref(''), notice = ref(''), deleteOpen = ref(false), ordersOpen = ref(false), addressOpen = ref(false), orderBusy = ref(''), addressError = ref('')
const editingAddress = ref<Address | null>(null)
const profile = reactive({ username: '', email: '' }), password = ref(''), recharge = ref<number | null>(null)
const addressForm = reactive({ recipient_name: '', phone: '', province_code: '', city_code: '', district_code: '', address: '', is_default: false })
const addressFieldErrors = reactive<Record<AddressField, string>>({ recipient_name: '', phone: '', province_code: '', city_code: '', district_code: '', address: '' })
const initials = computed(() => (user.value?.username || '用').slice(0, 1).toUpperCase())
const presenceLabel = computed(() => ({ online: '在线', offline: '离线', frozen: '冻结' }[workspace.value?.presence_status ?? 'offline']))
const cartItems = computed(() => cart.value?.groups.flatMap((group) => group.items.map((item) => ({ ...item, store_name: group.store_name }))) ?? [])
const cities = computed(() => regionOptions(addressForm.province_code))
const districts = computed(() => regionOptions(addressForm.city_code))

function token() { if (!auth.accessToken) throw new Error('missing admin token'); return auth.accessToken }
function dateTime(value: string | null | undefined) { return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '从未登录' }
function statusLabel(value: string) { return ({ pending_payment: '待付款', paid: '已支付', pending_shipment: '待发货', shipped: '运输中', completed: '已完成' } as Record<string, string>)[value] ?? value }
function actionLabel(code: string) { return ({ pay: '去支付', cancel_order: '取消订单', apply_after_sale: '发起售后', view_after_sale: '查看售后', view_logistics: '查看物流', review: '评价', delete_order: '删除订单', confirm_receipt: '确认收货', contact_store: '联系商家', repurchase: '再次购买' } as Record<string, string>)[code] ?? code }

async function load() {
  loading.value = true; error.value = ''
  try {
    const [a, b, c, d, e, f] = await Promise.all([
      apiRequest<AdminUserSummary>(`/admin/users/${userId}`, {}, token()), getAdminUserWorkspace(userId, token()),
      apiRequest<{ items: Address[] }>(`/admin/users/${userId}/addresses`, {}, token()), apiRequest<{ items: ProductCardData[] }>(`/admin/users/${userId}/favorite-products`, {}, token()),
      apiRequest<{ items: StoreData[] }>(`/admin/users/${userId}/followed-stores`, {}, token()), apiRequest<CartData>(`/admin/users/${userId}/cart`, {}, token()),
    ])
    user.value = a.data; workspace.value = b.data; addresses.value = c.data.items; products.value = d.data.items; stores.value = e.data.items; cart.value = f.data
    profile.username = user.value.username; profile.email = workspace.value.current_email ?? ''
  } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false }
}
async function run(action: () => Promise<unknown>, success: string, reload = true) { busy.value = true; error.value = ''; notice.value = ''; try { await action(); notice.value = success; if (reload) await load() } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
async function saveAccount() { if (!user.value) return; const payload: { username?: string; email?: string } = {}; if (profile.username !== user.value.username) payload.username = profile.username; if (profile.email !== (workspace.value?.current_email ?? '')) payload.email = profile.email; if (!Object.keys(payload).length) { error.value = '没有需要保存的账号资料。'; return }; await run(() => updateAdminUser(userId, payload, user.value!.version, token()), '账号资料已保存。') }
async function savePassword() { if (!password.value) return; await run(() => replaceAdminUserPassword(userId, { temporary_password: password.value }, token()), '密码已更新，用户需要使用新密码重新登录。'); password.value = '' }
async function changeStatus(action: 'suspend' | 'resume') { if (!user.value) return; await run(() => apiRequest(`/admin/users/${userId}/status-changes`, { method: 'POST', headers: { 'If-Match': `"v${user.value!.version}"`, 'Idempotency-Key': createIdempotencyKey('admin-user-status') }, body: JSON.stringify({ action }) }, token()), action === 'suspend' ? '账号已冻结。' : '账号已恢复。') }
async function forceOffline() { await run(() => apiRequest(`/admin/users/${userId}/session-revocations`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('admin-user-offline') }, body: JSON.stringify({ scope: 'all' }) }, token()), '用户已强制下线。') }
async function addBalance() { if (!recharge.value || recharge.value <= 0) return; await run(() => adjustAdminUserWallet(userId, { direction: 'credit', amount_minor: Math.round(recharge.value! * 100) }, token()), '充值成功。'); recharge.value = null }
function regionOptions(parentCode: string): RegionOption[] { return Object.entries(areaData[parentCode] ?? {}).map(([code, name]) => ({ code, name })) }
function clearAddressFieldError(field: AddressField) { addressFieldErrors[field] = '' }
function clearAddressErrors() { addressError.value = ''; for (const field of Object.keys(addressFieldErrors) as AddressField[]) addressFieldErrors[field] = '' }
function selectProvince() { clearAddressFieldError('province_code'); clearAddressFieldError('city_code'); clearAddressFieldError('district_code'); addressForm.city_code = ''; addressForm.district_code = '' }
function selectCity() { clearAddressFieldError('city_code'); clearAddressFieldError('district_code'); addressForm.district_code = '' }
function validateAddress(): boolean {
  clearAddressErrors()
  if (!addressForm.recipient_name.trim()) addressFieldErrors.recipient_name = '请输入收货人。'
  if (!addressForm.phone.trim()) addressFieldErrors.phone = '请输入联系电话。'
  else if (addressForm.phone.trim().length < 7) addressFieldErrors.phone = '联系电话至少需要 7 个字符。'
  if (!addressForm.province_code) addressFieldErrors.province_code = '请选择省份。'
  if (!addressForm.city_code) addressFieldErrors.city_code = '请选择城市。'
  if (!addressForm.district_code) addressFieldErrors.district_code = '请选择区或县。'
  if (!addressForm.address.trim()) addressFieldErrors.address = '请输入详细地址。'
  else if (addressForm.address.trim().length < 2) addressFieldErrors.address = '详细地址至少需要 2 个字符。'
  return !Object.values(addressFieldErrors).some(Boolean)
}
function resetAddressForm() { Object.assign(addressForm, { recipient_name: '', phone: '', province_code: '', city_code: '', district_code: '', address: '', is_default: false }); clearAddressErrors() }
function closeAddress() { if (busy.value) return; addressOpen.value = false; editingAddress.value = null; resetAddressForm() }
function openNewAddress() { editingAddress.value = null; resetAddressForm(); addressOpen.value = true }
function openEditAddress(item: Address) { clearAddressErrors(); editingAddress.value = item; Object.assign(addressForm, { recipient_name: item.recipient_name, phone: item.phone, province_code: item.province_code, city_code: item.city_code, district_code: item.district_code, address: item.address, is_default: item.is_default }); addressOpen.value = true }
async function saveAddress() {
  if (!validateAddress()) return
  const item = editingAddress.value
  const payload = { ...addressForm, country_code: 'CN', postal_code: null, label: null }
  busy.value = true; addressError.value = ''; notice.value = ''
  try {
    await (item
      ? apiRequest(`/admin/users/${userId}/addresses/${item.address_id}`, { method: 'PATCH', headers: { 'If-Match': `"v${item.version}"` }, body: JSON.stringify(payload) }, token())
      : apiRequest(`/admin/users/${userId}/addresses`, { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('admin-address') }, body: JSON.stringify(payload) }, token()))
    notice.value = item ? '收货地址已更新。' : '收货地址已新增。'
    addressOpen.value = false; editingAddress.value = null; resetAddressForm()
    await load()
  } catch (cause) {
    if (cause instanceof ApiProblem && cause.body.status === 422) {
      let matched = false
      for (const issue of cause.body.errors ?? []) {
        const field = issue.pointer.split('/').at(-1) as AddressField | undefined
        if (field && field in addressFieldErrors) { addressFieldErrors[field] = issue.message; matched = true }
      }
      if (!matched) addressError.value = errorMessage(cause)
    } else addressError.value = errorMessage(cause)
  } finally { busy.value = false }
}
async function deleteAddress(item: Address) { if (await confirmAction('确认删除这个收货地址？', { title: '删除收货地址', confirmText: '确认删除', tone: 'danger' })) await run(() => apiRequest(`/admin/users/${userId}/addresses/${item.address_id}`, { method: 'DELETE', headers: { 'If-Match': `"v${item.version}"` } }, token()), '收货地址已删除。') }
async function removeProduct(id: string) { await run(() => apiRequest(`/admin/users/${userId}/favorite-products/${id}`, { method: 'DELETE' }, token()), '已取消商品收藏。') }
async function removeStore(id: string) { await run(() => apiRequest(`/admin/users/${userId}/followed-stores/${id}`, { method: 'DELETE' }, token()), '已取消店铺收藏。') }
async function cartQuantity(itemId: string, quantity: number) { if (!cart.value) return; await run(() => apiRequest(`/admin/users/${userId}/cart/items/${itemId}`, { method: 'PATCH', headers: { 'If-Match': `"v${cart.value!.version}"` }, body: JSON.stringify({ quantity }) }, token()), '购物车数量已更新。') }
async function removeCart(itemId: string) { if (!cart.value) return; await run(() => apiRequest(`/admin/users/${userId}/cart/items/${itemId}`, { method: 'DELETE', headers: { 'If-Match': `"v${cart.value!.version}"` } }, token()), '购物车商品已删除。') }
async function openOrders() { ordersOpen.value = true; orderBusy.value = 'loading'; error.value = ''; try { orders.value = (await listAdminOrders({ q: userId }, token())).data.items.filter((item) => item.order.order_status !== 'cancelled') } catch (cause) { error.value = errorMessage(cause) } finally { orderBusy.value = '' } }
async function runOrderAction(action: OrderAction, order: OrderSummary) { if (!action.enabled) return; if (action.code === 'cancel_order' || action.code === 'confirm_receipt') { if (!await confirmAction(`确认${actionLabel(action.code)}？`)) return; orderBusy.value = order.order_id; try { const command = action.code === 'cancel_order' ? 'cancellations' : 'receipt-confirmations'; await apiRequest(`/admin/users/${userId}/orders/${order.order_id}/${command}`, { method: 'POST', headers: { 'If-Match': `"v${order.version}"`, 'Idempotency-Key': createIdempotencyKey(`admin-order-${command}`) } }, token()); await openOrders(); notice.value = `${actionLabel(action.code)}成功。` } catch (cause) { error.value = errorMessage(cause) } finally { orderBusy.value = '' }; return }; await router.push(`/admin/orders/${order.order_id}`) }
async function removeUser() { if (!user.value) return; busy.value = true; try { await deleteAdminUser(userId, user.value.version, token()); await router.replace({ path: '/admin/users', query: { deleted: userId } }) } catch (cause) { deleteOpen.value = false; error.value = errorMessage(cause) } finally { busy.value = false } }
onMounted(load)
</script>

<template>
  <section class="admin-page-stack admin-user-detail-page">
    <RouterLink class="admin-back-link" to="/admin/users">← 返回用户列表</RouterLink>
    <PageState :loading="loading" :error="error && !user ? error : ''" :empty="!loading && !user" empty-title="没有找到该用户" @retry="load">
      <template v-if="user && workspace">
        <header class="admin-user-detail-hero"><div class="admin-user-detail-profile"><span>{{ initials }}</span><div><p class="eyebrow">用户工作台</p><h1>{{ user.username }}</h1><p>{{ user.user_id }}</p></div></div><div class="admin-user-detail-status"><span><i :class="workspace.presence_status" />{{ presenceLabel }}</span><small>注册于 {{ dateTime(user.registered_at) }}</small></div></header>
        <p v-if="notice" class="alert success" role="status">{{ notice }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
        <section class="admin-user-fact-strip"><article><small>最近登录</small><strong>{{ dateTime(user.last_login_at) }}</strong></article><article><small>账号状态</small><strong>{{ presenceLabel }}</strong></article><article><small>用户编号</small><strong>{{ user.user_id }}</strong></article></section>
        <button type="button" class="admin-user-orders-entry" @click="openOrders"><span>▤</span><div><strong>购买订单</strong><small>弹窗查看该用户的有效订单、物流、评价和售后操作</small></div><b>查看订单 →</b></button>

        <section class="admin-user-workspace-section"><header class="admin-user-section-heading"><div><p class="eyebrow">01 · 账号资料</p><h2>账号与安全</h2><p>超级管理员可直接维护资料和状态；系统不保存可还原的明文密码。</p></div></header><div class="admin-detail-two-column">
          <form class="admin-panel admin-editor" @submit.prevent="saveAccount"><label>用户名<input v-model.trim="profile.username" required /></label><label>当前密码<input value="不可查看（仅保存不可逆密码哈希）" disabled /></label><label>当前邮箱<input :value="workspace.current_email ?? ''" disabled placeholder="尚未绑定邮箱" /></label><label>更改邮箱<input v-model.trim="profile.email" type="email" placeholder="输入完整新邮箱" /></label><button :disabled="busy">保存账号资料</button></form>
          <div class="admin-panel admin-editor"><form @submit.prevent="savePassword"><label>更改密码<input v-model="password" type="password" autocomplete="new-password" placeholder="输入新密码" required /></label><button :disabled="busy">直接设置新密码</button></form><hr/><strong>账号当前状态：{{ presenceLabel }}</strong><div class="actions"><button v-if="user.account_status === 'active'" class="danger" type="button" @click="changeStatus('suspend')">冻结</button><button v-else type="button" @click="changeStatus('resume')">恢复</button><button class="secondary" type="button" @click="forceOffline">强制下线</button></div></div>
        </div></section>

        <section class="admin-user-workspace-section"><header class="admin-user-section-heading"><div><p class="eyebrow">02 · 账户余额</p><h2>余额与充值</h2></div></header><form class="admin-panel admin-inline-money" @submit.prevent="addBalance"><div><small>当前余额</small><strong>¥{{ (Number(workspace.balance_minor) / 100).toFixed(2) }}</strong></div><label>充值金额（元）<input v-model.number="recharge" type="number" min="0.01" max="1000000" step="0.01" required /></label><button :disabled="busy">确认充值</button></form></section>

        <section class="admin-user-workspace-section"><header class="admin-user-section-heading"><div><p class="eyebrow">03 · 收货地址</p><h2>收货地址</h2></div><button @click="openNewAddress">＋ 新增地址</button></header><div class="admin-address-grid"><article v-for="item in addresses" :key="item.address_id" class="admin-panel"><strong>{{ item.recipient_name }} · {{ item.phone }}</strong><p>{{ formatChinaRegion(item) }} {{ item.address }}</p><span v-if="item.is_default">默认地址</span><div class="actions"><button type="button" @click="openEditAddress(item)">编辑</button><button class="danger-link" type="button" @click="deleteAddress(item)">删除</button></div></article><p v-if="!addresses.length" class="empty-state">该用户暂无收货地址</p></div></section>

        <section class="admin-user-workspace-section"><header class="admin-user-section-heading"><div><p class="eyebrow">04 · 用户收藏</p><h2>收藏的商品与店铺</h2></div></header><div class="admin-detail-two-column"><article class="admin-panel"><h3>商品收藏</h3><div class="admin-mini-list"><div v-for="item in products" :key="item.product_id"><img v-if="item.main_image" :src="resolveApiAssetUrl(item.main_image.thumbnail_url) ?? ''" alt=""/><span><strong>{{ item.product_name }}</strong><small>{{ item.store_name }} · {{ formatMoney(item.price) }}</small></span><button @click="removeProduct(item.product_id)">取消收藏</button></div><p v-if="!products.length" class="empty-state">暂无商品收藏</p></div></article><article class="admin-panel"><h3>店铺收藏</h3><div class="admin-mini-list"><div v-for="item in stores" :key="item.store_id"><span><strong>{{ item.store_name }}</strong><small>评分 {{ item.rating_score }} · 销量 {{ item.sales_count }}</small></span><button @click="removeStore(item.store_id)">取消收藏</button></div><p v-if="!stores.length" class="empty-state">暂无店铺收藏</p></div></article></div></section>

        <section class="admin-user-workspace-section"><header class="admin-user-section-heading"><div><p class="eyebrow">05 · 用户购物车</p><h2>购物车商品</h2></div></header><div class="admin-panel admin-cart-manager"><div v-for="item in cartItems" :key="item.cart_item_id"><img v-if="item.image_url" :src="resolveApiAssetUrl(item.image_url) ?? ''" alt=""/><span><strong>{{ item.product_name }}</strong><small>{{ item.store_name }} · {{ item.sku_name }}</small></span><input :value="item.quantity" type="number" min="1" max="99" @change="cartQuantity(item.cart_item_id, Number(($event.target as HTMLInputElement).value))"/><button class="danger-link" @click="removeCart(item.cart_item_id)">删除</button></div><p v-if="!cartItems.length" class="empty-state">购物车为空</p></div></section>
        <article class="admin-panel admin-danger-zone"><div><p class="eyebrow">不可恢复</p><h2>删除用户</h2><p>无交易历史时可物理删除；存在订单等法定留存数据时，后端会明确阻止。</p></div><button class="danger" @click="deleteOpen = true">删除这个用户</button></article>
      </template>
    </PageState>

    <Teleport to="body"><div v-if="deleteOpen" class="admin-form-overlay" @click.self="deleteOpen = false"><section class="admin-form-dialog"><header><div><p class="eyebrow">确认操作</p><h2>确认删除这个用户？</h2><p>删除后无法恢复，不需要填写原因或输入确认口令。</p></div><button @click="deleteOpen = false">×</button></header><footer><button class="secondary" @click="deleteOpen = false">取消</button><button class="danger" :disabled="busy" @click="removeUser">确认删除</button></footer></section></div></Teleport>
    <Teleport to="body"><div v-if="addressOpen" class="admin-form-overlay" @click.self="closeAddress"><form class="admin-form-dialog" novalidate @submit.prevent="saveAddress"><header><div><p class="eyebrow">收货地址</p><h2>{{ editingAddress ? '编辑收货地址' : '新增收货地址' }}</h2><p>按“省份 → 城市 → 区 / 县”依次选择，界面显示中文，系统自动保存标准地区代码。</p></div><button type="button" aria-label="关闭地址编辑" @click="closeAddress">×</button></header><p v-if="addressError" class="alert error" role="alert">{{ addressError }}</p><div class="admin-form-fields"><label>收货人<input v-model.trim="addressForm.recipient_name" minlength="1" maxlength="64" required :aria-invalid="Boolean(addressFieldErrors.recipient_name)" @input="clearAddressFieldError('recipient_name')" /><small v-if="addressFieldErrors.recipient_name" class="field-error" role="alert">{{ addressFieldErrors.recipient_name }}</small></label><label>联系电话<input v-model.trim="addressForm.phone" minlength="7" maxlength="32" required :aria-invalid="Boolean(addressFieldErrors.phone)" @input="clearAddressFieldError('phone')" /><small v-if="addressFieldErrors.phone" class="field-error" role="alert">{{ addressFieldErrors.phone }}</small></label><fieldset class="wide region-selector address-region-selector admin-address-region-selector"><legend>地区</legend><div class="field-row"><label>省份<select v-model="addressForm.province_code" required :aria-invalid="Boolean(addressFieldErrors.province_code)" @change="selectProvince"><option value="" disabled>请选择省份</option><option v-for="item in provinces" :key="item.code" :value="item.code">{{ item.name }}</option></select><small v-if="addressFieldErrors.province_code" class="field-error" role="alert">{{ addressFieldErrors.province_code }}</small></label><label>城市<select v-model="addressForm.city_code" required :disabled="!addressForm.province_code" :aria-invalid="Boolean(addressFieldErrors.city_code)" @change="selectCity"><option value="" disabled>请选择城市</option><option v-for="item in cities" :key="item.code" :value="item.code">{{ item.name }}</option></select><small v-if="addressFieldErrors.city_code" class="field-error" role="alert">{{ addressFieldErrors.city_code }}</small></label><label>区 / 县<select v-model="addressForm.district_code" required :disabled="!addressForm.city_code" :aria-invalid="Boolean(addressFieldErrors.district_code)" @change="clearAddressFieldError('district_code')"><option value="" disabled>请选择区或县</option><option v-for="item in districts" :key="item.code" :value="item.code">{{ item.name }}</option></select><small v-if="addressFieldErrors.district_code" class="field-error" role="alert">{{ addressFieldErrors.district_code }}</small></label></div></fieldset><label class="wide">详细地址<input v-model.trim="addressForm.address" minlength="2" maxlength="500" required placeholder="请输入街道、门牌号、小区、楼栋及房间号" :aria-invalid="Boolean(addressFieldErrors.address)" @input="clearAddressFieldError('address')" /><small v-if="addressFieldErrors.address" class="field-error" role="alert">{{ addressFieldErrors.address }}</small></label><label class="wide check-row"><input v-model="addressForm.is_default" type="checkbox"/>设为默认地址</label></div><footer><button type="button" class="secondary" :disabled="busy" @click="closeAddress">取消</button><button :disabled="busy">{{ busy ? '正在保存…' : '保存地址' }}</button></footer></form></div></Teleport>
    <Teleport to="body"><div v-if="ordersOpen" class="admin-form-overlay" @click.self="ordersOpen = false"><section class="admin-form-dialog admin-user-orders-dialog"><header><div><p class="eyebrow">用户订单</p><h2>{{ user?.username }} 的购买订单</h2><p>已取消订单不在管理端展示。</p></div><button @click="ordersOpen = false">×</button></header><div class="admin-user-order-list"><p v-if="orderBusy === 'loading'">正在载入订单…</p><article v-for="entry in orders" :key="entry.order.order_id"><header><span><strong>{{ entry.order.store.store_name }}</strong><small>{{ dateTime(entry.order.created_at) }} · {{ entry.order.order_id }}</small></span><b>{{ statusLabel(entry.order.order_status) }}</b></header><div v-for="item in entry.order.items" :key="item.order_item_id" class="admin-user-order-item"><img v-if="item.image_url" :src="resolveApiAssetUrl(item.image_url) ?? ''" alt=""/><span><strong>{{ item.product_name }}</strong><small>{{ item.sku_name }} × {{ item.quantity }}</small></span><b>{{ formatMoney(item.payable_amount) }}</b></div><footer><strong>实付 {{ formatMoney(entry.order.amounts.paid_amount) }}</strong><div><RouterLink :to="`/admin/orders/${entry.order.order_id}`">查看详情</RouterLink><button v-for="action in entry.order.available_actions.filter((item) => item.enabled)" :key="action.code" :disabled="Boolean(orderBusy)" @click="runOrderAction(action, entry.order)">{{ orderBusy === entry.order.order_id ? '处理中…' : actionLabel(action.code) }}</button></div></footer></article><p v-if="!orderBusy && !orders.length" class="empty-state">该用户暂无有效订单</p></div></section></div></Teleport>
  </section>
</template>
