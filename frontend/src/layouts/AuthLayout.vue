<script setup lang="ts">
import { RouterLink, RouterView } from 'vue-router'
</script>

<template>
  <main class="centered-layout">
    <RouterLink to="/" class="brand">Ecom AI</RouterLink>
    <section class="surface auth-surface"><RouterView /></section>
  </main>
</template>

