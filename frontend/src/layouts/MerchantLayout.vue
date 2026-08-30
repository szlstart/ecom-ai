<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { adminGet, requireAdminToken, type AdminStore } from '@/api/admin-catalog'
import { useAdminAuthStore } from '@/stores/admin-auth'
import MerchantMessageCenter from '@/components/merchant/MerchantMessageCenter.vue'
import { subscribeStoreStatus, type StoreStatusSignal } from '@/utils/store-status-sync'

const auth = useAdminAuthStore()
const route = useRoute()
const router = useRouter()
const stores = ref<AdminStore[]>([])
const currentStore = computed(() => stores.value[0] ?? null)
const emojiIndex = ref(0)
const goodEmojis = ['😊', '😄', '🥳', '🤗', '🌟', '✨', '💚', '👍', '🎉', '🚀']
const pausedEmojis = ['😔', '😞', '😟', '🙁', '😢', '🥺', '🌧️', '💤', '⏸️', '🫥']
const storeStatusClass = computed(() => currentStore.value?.status === 'active' ? 'active' : 'suspended')
const storeStatusText = computed(() => {
  if (!currentStore.value) return '正在读取状态'
  const active = currentStore.value.status === 'active'
  const emojis = active ? goodEmojis : pausedEmojis
  return `${active ? '营业中' : '暂停营业'} ${emojis[emojiIndex.value % emojis.length]}`
})
let emojiTimer: number | undefined
let storePollTimer: number | undefined
let unsubscribeStatus: (() => void) | undefined
let storeLoading = false
const navigation = [
  { to: '/merchant/products', label: '我的商品', icon: '▦', hint: '上架与编辑' },
  { to: '/merchant/orders', label: '我的订单', icon: '▤', hint: '发货、收益与售后' },
  { to: '/merchant/after-sales', label: '售后处理', icon: '↺', hint: '退款与退货审核' },
  { to: '/merchant/store', label: '店铺资料', icon: '◇', hint: '顾客看到的信息' },
]

function active(path: string): boolean {
  return route.path === path || (path !== '/merchant/dashboard' && route.path.startsWith(`${path}/`))
}

async function loadStore() {
  if (!auth.accessToken || storeLoading || document.hidden) return
  storeLoading = true
  try {
    stores.value = (await adminGet<{ items: AdminStore[]; next_cursor: string | null }>(
      '/admin/stores?limit=20',
      requireAdminToken(auth.accessToken),
    )).data.items
  } catch {
    // 保留上一次成功读取的公开店铺状态，认证错误由全局请求层处理。
  } finally {
    storeLoading = false
  }
}

function applyStatus(signal: StoreStatusSignal) {
  const item = currentStore.value
  if (!item || item.store_id !== signal.storeId) return
  stores.value = [{ ...item, status: signal.status, suspension_source: signal.suspensionSource }]
  emojiIndex.value = 0
}

function refreshVisibleStore() {
  if (!document.hidden) void loadStore()
}

async function logout() {
  await auth.logout('merchant')
  await router.replace('/merchant')
}

onMounted(() => {
  void loadStore()
  unsubscribeStatus = subscribeStoreStatus(applyStatus)
  emojiTimer = window.setInterval(() => { emojiIndex.value += 1 }, 5000)
  storePollTimer = window.setInterval(() => void loadStore(), 3000)
  window.addEventListener('focus', refreshVisibleStore)
  document.addEventListener('visibilitychange', refreshVisibleStore)
})

onUnmounted(() => {
  unsubscribeStatus?.()
  if (emojiTimer !== undefined) window.clearInterval(emojiTimer)
  if (storePollTimer !== undefined) window.clearInterval(storePollTimer)
  window.removeEventListener('focus', refreshVisibleStore)
  document.removeEventListener('visibilitychange', refreshVisibleStore)
})
</script>

<template>
  <div class="merchant-shell" :class="{ 'message-workspace-shell': route.path === '/merchant/messages' }">
    <aside class="merchant-sidebar">
      <RouterLink class="merchant-wordmark" to="/merchant/products"><span>E</span><b>Ecom AI</b><small>商家中心</small></RouterLink>
      <div class="merchant-store-card">
        <span class="merchant-store-logo">{{ currentStore?.store_name.slice(0, 1) || '店' }}</span>
        <div><strong>{{ currentStore?.store_name || '正在读取店铺' }}</strong><small class="merchant-sidebar-store-status" :class="storeStatusClass">{{ storeStatusText }}</small></div>
      </div>
      <nav aria-label="商家中心导航">
        <RouterLink v-for="item in navigation" :key="item.to" :to="item.to" :class="{ active: active(item.to) }"><span aria-hidden="true">{{ item.icon }}</span><b>{{ item.label }}</b><small>{{ item.hint }}</small></RouterLink>
      </nav>
      <div class="merchant-sidebar-footer">
        <RouterLink v-if="currentStore" :to="`/stores/${currentStore.store_id}`" target="_blank">查看用户端店铺</RouterLink>
        <button class="merchant-logout" type="button" @click="logout">退出商家中心</button>
      </div>
    </aside>
    <main class="merchant-main">
      <header class="merchant-topbar"><div><span>当前店铺</span><strong>{{ currentStore?.store_name || '正在读取店铺' }}</strong></div><div class="merchant-topbar-actions"><MerchantMessageCenter :store-id="currentStore?.store_id" :store-name="currentStore?.store_name" :store-logo-url="currentStore?.logo_url" /><RouterLink class="merchant-profile-entry" to="/merchant/store"><span>{{ currentStore?.store_name.slice(0, 1) || '店' }}</span><b>店铺资料</b></RouterLink></div></header>
      <div class="merchant-content" :class="{ 'message-workspace-content': route.path === '/merchant/messages' }"><RouterView :key="route.path" /></div>
    </main>
  </div>
</template>
