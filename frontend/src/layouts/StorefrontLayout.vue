<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { onClickOutside } from '@vueuse/core'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'
import type { LocationQueryRaw } from 'vue-router'

import AuthModal from '@/components/AuthModal.vue'
import UserMessageCenter from '@/components/UserMessageCenter.vue'
import { useMessageCenterStore } from '@/stores/message-center'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const messageCenter = useMessageCenterStore()
const route = useRoute()
const router = useRouter()
const searchTerm = ref('')
const userMenu = ref<HTMLDetailsElement | null>(null)
const openMessagesAfterAuth = ref(false)
const authMode = computed<'login' | 'register'>(() => route.query.auth === 'register' ? 'register' : 'login')
const authModalOpen = computed(() => route.query.auth === 'login' || route.query.auth === 'register')
const displayName = computed(() => auth.user?.nickname || auth.user?.username || '用户')
const greeting = computed(() => {
  const hour = new Date().getHours()
  if (hour >= 5 && hour < 11) return '早上好'
  if (hour >= 11 && hour < 14) return '中午好'
  if (hour >= 14 && hour < 18) return '下午好'
  return '晚上好'
})

onClickOutside(userMenu, () => userMenu.value?.removeAttribute('open'))

function search() {
  const q = searchTerm.value.trim()
  void router.push({ path: '/search', query: q ? { q } : {} })
}

function openAuth(mode: 'login' | 'register' = 'login', redirect?: string) {
  const query: LocationQueryRaw = { ...route.query, auth: mode }
  if (redirect) query.redirect = redirect
  else delete query.redirect
  void router.push({ path: route.path, query, hash: route.hash })
}

function closeAuth() {
  const query: LocationQueryRaw = { ...route.query }
  delete query.auth
  delete query.redirect
  void router.replace({ path: route.path, query, hash: route.hash })
}

async function finishAuth() {
  if (openMessagesAfterAuth.value) {
    openMessagesAfterAuth.value = false
    closeAuth()
    messageCenter.show()
    return
  }
  const redirect = typeof route.query.redirect === 'string' ? route.query.redirect : null
  if (redirect?.startsWith('/') && !redirect.startsWith('//')) {
    await router.replace(redirect)
  } else {
    closeAuth()
  }
}

function requestMessages() {
  openMessagesAfterAuth.value = true
  openAuth('login')
}

async function logout() {
  userMenu.value?.removeAttribute('open')
  await auth.logout()
  if (route.meta.requiresAuth) await router.replace('/')
}

onMounted(() => {
  if (!auth.isAuthenticated) void auth.refresh()
})

watch(() => route.fullPath, () => userMenu.value?.removeAttribute('open'))
</script>

<template>
  <div class="app-shell">
    <header class="storefront-header">
      <nav aria-label="主导航" class="nav-content">
        <RouterLink to="/" class="brand">Ecom AI</RouterLink>
        <form class="header-search" role="search" @submit.prevent="search">
          <label class="sr-only" for="global-search">搜索商品</label>
          <input id="global-search" v-model="searchTerm" type="search" maxlength="100" placeholder="搜索商品" />
          <button type="submit" class="small">搜索</button>
        </form>
        <div class="nav-links">
          <RouterLink v-if="auth.isAuthenticated" class="storefront-nav-entry" to="/cart"><span aria-hidden="true">🛒</span><span class="nav-entry-label">购物车</span></RouterLink><button v-else class="nav-link-button storefront-nav-entry" type="button" @click="openAuth('login', '/cart')"><span aria-hidden="true">🛒</span><span class="nav-entry-label">购物车</span></button>
          <UserMessageCenter v-if="auth.isAuthenticated" /><button v-else class="nav-link-button storefront-nav-entry" type="button" @click="requestMessages"><span aria-hidden="true">💬</span><span class="nav-entry-label">消息</span></button>
          <RouterLink v-if="auth.isAuthenticated" class="storefront-nav-entry" to="/me/favorites/products"><span aria-hidden="true">♥</span><span class="nav-entry-label">收藏</span></RouterLink><button v-else class="nav-link-button storefront-nav-entry" type="button" @click="openAuth('login', '/me/favorites/products')"><span aria-hidden="true">♥</span><span class="nav-entry-label">收藏</span></button>
          <RouterLink v-if="auth.isAuthenticated" class="storefront-nav-entry" to="/me/addresses"><span aria-hidden="true">📍</span><span class="nav-entry-label">收货地址</span></RouterLink><button v-else class="nav-link-button storefront-nav-entry" type="button" @click="openAuth('login', '/me/addresses')"><span aria-hidden="true">📍</span><span class="nav-entry-label">收货地址</span></button>
          <RouterLink v-if="auth.isAuthenticated" class="storefront-nav-entry" to="/me"><span aria-hidden="true">👤</span><span class="nav-entry-label">我的</span></RouterLink><button v-else class="nav-link-button storefront-nav-entry" type="button" @click="openAuth('login', '/me')"><span aria-hidden="true">👤</span><span class="nav-entry-label">我的</span></button>

          <details v-if="auth.isAuthenticated" ref="userMenu" class="user-menu">
            <summary>{{ greeting }}，{{ displayName }}</summary>
            <div class="user-menu-panel">
              <RouterLink to="/me">查看我的</RouterLink>
              <button type="button" @click="logout">退出登录</button>
            </div>
          </details>
          <button v-else class="account-entry" type="button" @click="openAuth('login')">注册/登录</button>
        </div>
      </nav>
    </header>
    <main id="main-content" class="page-content"><RouterView /></main>
    <footer class="site-footer">企业级在线商城 · 安全交易与智能客服</footer>
    <AuthModal v-if="authModalOpen" :key="authMode" :initial-mode="authMode" @close="closeAuth" @authenticated="finishAuth" />
  </div>
</template>
