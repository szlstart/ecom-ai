<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
import { useUserAuthStore } from '@/stores/user-auth'
const auth = useUserAuthStore()
</script>

<template>
  <div class="app-shell">
    <header class="storefront-header">
      <nav aria-label="主导航" class="nav-content">
        <RouterLink to="/" class="brand">Ecom AI</RouterLink>
        <div class="nav-links">
          <RouterLink v-if="!auth.isAuthenticated" to="/login">登录</RouterLink>
          <RouterLink v-else to="/me">{{ auth.user?.nickname || '我的' }}</RouterLink>
          <span aria-disabled="true">购物车</span>
          <span aria-disabled="true">消息</span>
          <RouterLink v-if="auth.isAuthenticated" to="/me/addresses">地址</RouterLink>
        </div>
      </nav>
    </header>
    <main id="main-content" class="page-content"><RouterView /></main>
    <footer class="site-footer">企业级在线商城 · 安全交易与智能客服</footer>
  </div>
</template>
