<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink, useRouter } from 'vue-router'

import { apiRequest, errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'

interface Dashboard {
  order_counts: Record<string, number>
  review_counts: Record<string, number>
  default_address: { recipient_name: string; address: string } | null
  unread_message_count: number
  unavailable_sections: string[]
}
interface Wallet { balance: { minor_units: string; currency: string }; total_recharged: { minor_units: string; currency: string } }

const auth = useUserAuthStore()
const router = useRouter()
const data = ref<Dashboard | null>(null)
const wallet = ref<Wallet | null>(null)
const error = ref('')
const loggingOut = ref(false)
const orderCountLabels: Record<string, string> = {
  pending_payment: '待支付',
  pending_shipment: '待发货',
  in_transit: '运输中',
  pending_review: '待评价',
  after_sale: '售后中',
}

onMounted(async () => {
  try {
    const [dashboardResult, walletResult] = await Promise.all([apiRequest<Dashboard>('/users/me/dashboard', {}, auth.accessToken), apiRequest<Wallet>('/users/me/wallet', {}, auth.accessToken)])
    data.value = dashboardResult.data
    wallet.value = walletResult.data
  } catch (reason) {
    error.value = errorMessage(reason)
  }
})
function money(minorUnits = '0') { return `¥${(Number(minorUnits) / 100).toFixed(2)}` }

async function logout() {
  loggingOut.value = true
  await auth.logout()
  await router.replace('/')
}
</script>

<template>
  <section class="my-center-page">
    <header class="my-center-hero">
      <div class="my-center-profile">
        <span class="my-center-avatar">{{ (auth.user?.nickname || auth.user?.username || '我').slice(0, 1).toUpperCase() }}</span>
        <div><p class="eyebrow">Personal center</p><h1>你好，{{ auth.user?.nickname || auth.user?.username }}</h1><p>订单、地址、收藏和账户安全，都在这里统一管理。</p></div>
      </div>
      <div class="my-center-hero-actions">
        <RouterLink class="my-profile-button" to="/me/profile">编辑个人资料</RouterLink>
        <button class="my-logout-button" type="button" :disabled="loggingOut" @click="logout">{{ loggingOut ? '正在退出…' : '退出登录' }}</button>
      </div>
      <div class="my-wallet-panel">
        <div><p>账户余额</p><strong>{{ money(wallet?.balance.minor_units) }}</strong><small>模拟充值余额，可用于商城内支付</small></div>
        <RouterLink to="/me/wallet">充值与明细 <span aria-hidden="true">→</span></RouterLink>
      </div>
      <div v-if="data" class="my-ai-service-pill"><span aria-hidden="true">✦</span> AI 专属客服在线<span v-if="data.unread_message_count"> · {{ data.unread_message_count }} 条未读消息</span></div>
    </header>
    <p v-if="error" class="alert error">{{ error }}</p>
    <div v-if="data" class="dashboard-sections">
      <article class="dashboard-card dashboard-card-orders premium-dashboard-card">
        <div class="dashboard-card-heading"><div class="dashboard-heading-copy"><span class="dashboard-section-icon" aria-hidden="true">▣</span><div><p class="eyebrow">Orders</p><h2>我的订单</h2><p>查看订单进度，及时完成支付、收货与评价。</p></div></div><RouterLink class="dashboard-main-link" to="/me/orders?view=all">全部订单 <span aria-hidden="true">→</span></RouterLink></div>
        <div class="my-order-shortcuts"><RouterLink v-for="(count, key) in data.order_counts" :key="key" :to="{ path: '/me/orders', query: { view: String(key) } }"><span>{{ orderCountLabels[key] ?? key }}</span><strong>{{ count }}</strong><small>查看订单</small></RouterLink></div>
      </article>

      <article class="dashboard-card dashboard-card-address premium-dashboard-card dashboard-row-card">
        <span class="dashboard-section-icon" aria-hidden="true">⌖</span>
        <div><p class="eyebrow">Delivery</p><h2>默认收货地址</h2><p v-if="data.default_address" class="dashboard-primary-copy"><strong>{{ data.default_address.recipient_name }}</strong><span>{{ data.default_address.address }}</span></p><p v-else class="dashboard-empty-copy">尚未设置默认收货地址，添加后结算更快捷。</p></div>
        <RouterLink class="dashboard-main-link" to="/me/addresses">管理收货地址 <span aria-hidden="true">→</span></RouterLink>
      </article>

      <article class="dashboard-card dashboard-card-favorites premium-dashboard-card dashboard-row-card">
        <span class="dashboard-section-icon favorite" aria-hidden="true">♥</span>
        <div><p class="eyebrow">Favorites</p><h2>我的收藏</h2><p>心仪商品与关注店铺集中管理，随时继续选购。</p><div class="dashboard-quick-links"><RouterLink to="/me/favorites/products">商品收藏</RouterLink><RouterLink to="/me/favorites/stores">店铺收藏</RouterLink></div></div>
        <RouterLink class="dashboard-main-link" to="/me/favorites/products">查看收藏 <span aria-hidden="true">→</span></RouterLink>
      </article>

      <article class="dashboard-card dashboard-card-security premium-dashboard-card dashboard-row-card">
        <span class="dashboard-section-icon security" aria-hidden="true">◇</span>
        <div><p class="eyebrow">Security</p><h2>账号与安全</h2><p>管理登录密码、绑定邮箱与账户注销。</p></div>
        <RouterLink class="dashboard-main-link" to="/me/settings/security">安全设置 <span aria-hidden="true">→</span></RouterLink>
      </article>
    </div>
  </section>
</template>
