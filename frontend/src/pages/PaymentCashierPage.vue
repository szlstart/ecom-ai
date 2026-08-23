<script setup lang="ts">
import { RouterLink, useRoute } from 'vue-router'

const route = useRoute()
</script>

<template>
  <section class="content-section payment-bridge-page">
    <p class="eyebrow">订单创建成功</p>
    <h1>等待支付</h1>
    <p>合并交易单 <strong>{{ route.params.tradeOrderId }}</strong> 已创建，库存已为本次交易预留。</p>
    <div class="notice info">支付渠道将在第五阶段接入。订单与库存预占不会因为支付入口暂未开放而重复创建。</div>
    <div class="actions"><RouterLink class="button" to="/me">返回我的</RouterLink><RouterLink to="/search">继续购物</RouterLink></div>
  </section>
</template>
