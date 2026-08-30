<script setup lang="ts">
import AdminMessageCenter from '@/components/admin/AdminMessageCenter.vue'
</script>

<template>
  <section class="message-page admin-message-page" aria-labelledby="admin-messages-title">
    <header class="message-page-heading">
      <div><p class="eyebrow">平台沟通与 AI 管家</p><h1 id="admin-messages-title">消息</h1></div>
      <p>统一处理用户、店铺和 AI 管家会话，并在右侧追踪工具、知识与安全节点。</p>
    </header>
    <AdminMessageCenter />
  </section>
</template>
