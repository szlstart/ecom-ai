<script setup lang="ts">
import { RouterLink } from 'vue-router'
</script>

<template>
  <section class="surface empty-state">
    <p class="eyebrow">404</p>
    <h1>页面不存在</h1>
    <RouterLink to="/">返回首页</RouterLink>
  </section>
</template>
