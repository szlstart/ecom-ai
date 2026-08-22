<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <div class="admin-shell">
    <aside class="admin-sidebar">
      <strong>Ecom AI 管理端</strong>
      <RouterLink to="/admin/dashboard">仪表盘</RouterLink>
    </aside>
    <main class="admin-content"><RouterView /></main>
  </div>
</template>

