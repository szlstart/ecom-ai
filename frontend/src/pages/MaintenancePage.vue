<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const retryAfter = computed(() => typeof route.query.retry_after === 'string' ? route.query.retry_after : '')
</script>

<template>
  <main class="system-state" aria-labelledby="maintenance-title">
    <p class="eyebrow">维护中</p>
    <h1 id="maintenance-title">服务正在维护</h1>
    <p>已有订单和支付数据不会因刷新而丢失，请等待服务恢复后再继续操作。</p>
    <p v-if="retryAfter" class="muted">建议重试时间：{{ retryAfter }}</p>
    <nav class="actions" aria-label="恢复操作">
      <button type="button" @click="$router.go(0)">检查是否恢复</button>
      <RouterLink class="button-link secondary" to="/">返回首页</RouterLink>
    </nav>
  </main>
</template>
