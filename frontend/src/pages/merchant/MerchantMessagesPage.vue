<script setup lang="ts">
import MerchantMessageCenter from '@/components/merchant/MerchantMessageCenter.vue'
</script>

<template>
  <section class="message-page merchant-message-page" aria-labelledby="merchant-messages-title">
    <header class="message-page-heading">
      <div><p class="eyebrow">顾客沟通与经营助理</p><h1 id="merchant-messages-title">消息</h1></div>
      <p>左侧选择顾客或专属客服，中间沟通，右侧查看 AI 的安全执行轨迹。</p>
    </header>
    <MerchantMessageCenter standalone />
  </section>
</template>
