<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const props = withDefaults(defineProps<{ returnPath?: string; returnLabel?: string }>(), {
  returnPath: '/',
  returnLabel: '返回首页',
})
const route = useRoute()
const deniedPermission = computed(() => typeof route.query.denied === 'string' ? route.query.denied : '')
</script>

<template>
  <main class="system-state" aria-labelledby="forbidden-title">
    <p class="eyebrow">403</p>
    <h1 id="forbidden-title">你没有访问此内容的权限</h1>
    <p>请确认当前登录身份，或返回有权限访问的页面。为保护数据安全，这里不会展示目标资源是否存在。</p>
    <p v-if="deniedPermission" class="muted">当前账号缺少权限：<code>{{ deniedPermission }}</code></p>
    <nav class="actions" aria-label="恢复操作">
      <RouterLink v-if="props.returnPath" class="button-link" :to="props.returnPath">{{ props.returnLabel }}</RouterLink>
      <button class="secondary" type="button" @click="$router.back()">返回上一页</button>
    </nav>
  </main>
</template>
