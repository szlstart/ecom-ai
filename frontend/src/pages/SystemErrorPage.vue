<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const requestId = computed(() => typeof route.query.request_id === 'string' ? route.query.request_id : '')
</script>

<template>
  <main class="system-state" aria-labelledby="error-title">
    <p class="eyebrow">系统异常</p>
    <h1 id="error-title">暂时无法完成请求</h1>
    <p>请稍后重试。重复提交订单、支付或退款前，请先返回对应列表确认权威状态。</p>
    <p v-if="requestId" class="muted">请求编号：<code>{{ requestId }}</code></p>
    <nav class="actions" aria-label="恢复操作">
      <button type="button" @click="$router.go(0)">重新加载</button>
      <RouterLink class="button-link secondary" to="/">返回首页</RouterLink>
    </nav>
  </main>
</template>
