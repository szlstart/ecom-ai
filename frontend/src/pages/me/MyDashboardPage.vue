<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'
interface Dashboard { order_counts: Record<string, number>; review_counts: Record<string, number>; default_address: { recipient_name: string; address: string } | null; unread_message_count: number; unavailable_sections: string[] }
const auth = useUserAuthStore(), data = ref<Dashboard | null>(null), error = ref('')
onMounted(async () => { try { data.value = (await apiRequest<Dashboard>('/users/me/dashboard', {}, auth.accessToken)).data } catch (reason) { error.value = errorMessage(reason) } })
</script>
<template><section><div class="page-heading"><div><p class="eyebrow">个人中心</p><h1>你好，{{ auth.user?.nickname }}</h1></div><RouterLink class="button-link" to="/me/profile">编辑资料</RouterLink></div>
  <p v-if="error" class="alert error">{{ error }}</p><div v-if="data" class="card-grid"><article class="card"><h2>我的订单</h2><dl class="metric-list"><div v-for="(count, key) in data.order_counts" :key="key"><dt>{{ key }}</dt><dd>{{ count }}</dd></div></dl><p v-if="data.unavailable_sections.includes('orders')" class="muted">订单模块将在第四阶段接入。</p></article><article class="card"><h2>默认收货地址</h2><p v-if="data.default_address">{{ data.default_address.recipient_name }} · {{ data.default_address.address }}</p><p v-else class="muted">尚未设置地址</p><RouterLink to="/me/addresses">管理地址</RouterLink></article><article class="card"><h2>我的收藏</h2><p>集中查看收藏的商品和关注的店铺。</p><div class="actions"><RouterLink to="/me/favorites/products">商品收藏</RouterLink><RouterLink to="/me/favorites/stores">店铺收藏</RouterLink></div></article><article class="card"><h2>账号与安全</h2><p>管理密码、联系方式和登录设备。</p><RouterLink to="/me/settings/security">进入安全设置</RouterLink></article></div>
</section></template>
