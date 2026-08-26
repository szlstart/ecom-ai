<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { adminGet, requireAdminToken, type AdminStore } from '@/api/admin-catalog'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const router = useRouter()
const stores = ref<AdminStore[]>([])
const currentStore = computed(() => stores.value[0] ?? null)
const navigation = [
  { to: '/merchant/dashboard', label: '工作台', icon: '⌂' },
  { to: '/merchant/products', label: '商品管理', icon: '□' },
  { to: '/merchant/inventory', label: '库存管理', icon: '▦' },
  { to: '/merchant/support', label: '客户咨询', icon: '◌' },
  { to: '/merchant/reviews', label: '评价回复', icon: '☆' },
  { to: '/merchant/store', label: '店铺资料', icon: '◇' },
]

function active(path: string): boolean {
  return route.path === path || (path !== '/merchant/dashboard' && route.path.startsWith(`${path}/`))
}

async function loadStore() {
  if (!auth.accessToken) return
  stores.value = (await adminGet<{ items: AdminStore[]; next_cursor: string | null }>(
    '/admin/stores?limit=20',
    requireAdminToken(auth.accessToken),
  )).data.items
}

async function logout() {
  await auth.logout()
  await router.replace('/merchant/login')
}

onMounted(() => void loadStore())
</script>

<template>
  <div class="merchant-shell">
    <aside class="merchant-sidebar">
      <RouterLink class="merchant-wordmark" to="/merchant/dashboard">Ecom AI 商家中心</RouterLink>
      <div class="merchant-store-card">
        <span class="merchant-store-logo">{{ currentStore?.store_name.slice(0, 1) || '店' }}</span>
        <div><strong>{{ currentStore?.store_name || '正在读取店铺' }}</strong><small>{{ currentStore?.status === 'active' ? '营业中' : currentStore?.status || '—' }}</small></div>
      </div>
      <nav aria-label="商家中心导航">
        <RouterLink v-for="item in navigation" :key="item.to" :to="item.to" :class="{ active: active(item.to) }"><span aria-hidden="true">{{ item.icon }}</span>{{ item.label }}</RouterLink>
      </nav>
      <div class="merchant-sidebar-footer">
        <RouterLink v-if="currentStore" :to="`/stores/${currentStore.store_id}`" target="_blank">查看用户端店铺</RouterLink>
        <RouterLink to="/merchant/reauthenticate">重新安全验证</RouterLink>
        <button class="merchant-logout" type="button" @click="logout">退出商家中心</button>
      </div>
    </aside>
    <main class="merchant-main">
      <header class="merchant-topbar"><div><span>商家工作空间</span><strong>{{ currentStore?.store_name || '当前店铺' }}</strong></div><span class="merchant-safe-badge">店铺数据已隔离</span></header>
      <div class="merchant-content"><RouterView /></div>
    </main>
  </div>
</template>
