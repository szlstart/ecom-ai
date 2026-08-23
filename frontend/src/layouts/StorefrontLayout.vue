<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'
import { useUserAuthStore } from '@/stores/user-auth'
const auth = useUserAuthStore()
const router = useRouter()
const searchTerm = ref('')

function search() {
  const q = searchTerm.value.trim()
  void router.push({ path: '/search', query: q ? { q } : {} })
}

onMounted(() => {
  if (!auth.isAuthenticated) void auth.refresh()
})
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
          <RouterLink v-if="!auth.isAuthenticated" to="/login">登录</RouterLink>
          <RouterLink v-else to="/me">{{ auth.user?.nickname || '我的' }}</RouterLink>
          <span aria-disabled="true">购物车</span>
          <span aria-disabled="true">消息</span>
          <RouterLink v-if="auth.isAuthenticated" to="/me/favorites/products">收藏</RouterLink>
          <RouterLink v-if="auth.isAuthenticated" to="/me/addresses">地址</RouterLink>
        </div>
      </nav>
    </header>
    <main id="main-content" class="page-content"><RouterView /></main>
    <footer class="site-footer">企业级在线商城 · 安全交易与智能客服</footer>
  </div>
</template>
