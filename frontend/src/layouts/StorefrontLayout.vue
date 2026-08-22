<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <div class="app-shell">
    <header class="storefront-header">
      <nav aria-label="主导航" class="nav-content">
        <RouterLink to="/" class="brand">Ecom AI</RouterLink>
        <div class="nav-links">
          <RouterLink to="/login">登录</RouterLink>
          <span aria-disabled="true">购物车</span>
          <span aria-disabled="true">消息</span>
          <span aria-disabled="true">我的</span>
        </div>
      </nav>
    </header>
    <main id="main-content" class="page-content"><RouterView /></main>
    <footer class="site-footer">企业级在线商城 · 基础开发骨架</footer>
  </div>
</template>

