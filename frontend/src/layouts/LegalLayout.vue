<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <main class="document-layout">
    <RouterLink to="/" class="brand">Ecom AI</RouterLink>
    <article class="surface"><RouterView /></article>
  </main>
</template>

