<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <main class="merchant-auth-layout">
    <section class="merchant-auth-intro">
      <RouterLink class="merchant-wordmark" to="/merchant">Ecom AI 商家中心</RouterLink>
      <div>
        <p class="eyebrow">STORE CONSOLE</p>
        <h1>认真经营每一件商品，及时回应每一位顾客。</h1>
        <p>商品、库存、客服和评价集中处理；店铺之间的数据严格隔离。</p>
      </div>
      <small>这是商家专用入口，与消费者账号和平台管理端相互独立。</small>
    </section>
    <section class="merchant-auth-panel"><RouterView /></section>
  </main>
</template>
