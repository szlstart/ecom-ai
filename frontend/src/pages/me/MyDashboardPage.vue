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
  <section>
    <div class="page-heading">
      <div><p class="eyebrow">个人中心</p><h1>你好，{{ auth.user?.nickname || auth.user?.username }}</h1></div>
      <div class="actions">
        <RouterLink class="button-link" to="/me/profile">编辑资料</RouterLink>
        <button class="danger" type="button" :disabled="loggingOut" @click="logout">
          {{ loggingOut ? '正在退出…' : '退出登录' }}
        </button>
      </div>
    </div>

    <p v-if="error" class="alert error">{{ error }}</p>
    <div v-if="data" class="dashboard-sections">
      <article class="card dashboard-card dashboard-card-wallet"><div class="dashboard-card-heading"><div><p class="eyebrow">余额</p><h2>账户余额</h2></div><RouterLink to="/me/wallet">充值与明细</RouterLink></div><p class="dashboard-balance">{{ money(wallet?.balance.minor_units) }}</p><small class="muted">当前充值为模拟流程，不会产生真实扣款。</small></article>
      <article class="card dashboard-card dashboard-card-orders">
        <div class="dashboard-card-heading"><div><p class="eyebrow">订单</p><h2>我的订单</h2></div><RouterLink to="/me/orders?view=all">查看全部订单</RouterLink></div>
        <dl class="metric-list"><div v-for="(count, key) in data.order_counts" :key="key"><dt>{{ orderCountLabels[key] ?? key }}</dt><dd>{{ count }}</dd></div></dl>
      </article>

      <article class="card dashboard-card dashboard-card-address">
        <div><p class="eyebrow">配送</p><h2>默认收货地址</h2></div>
        <p v-if="data.default_address">{{ data.default_address.recipient_name }} · {{ data.default_address.address }}</p>
        <p v-else class="muted">尚未设置默认收货地址</p>
        <RouterLink to="/me/addresses">管理地址</RouterLink>
      </article>

      <article class="card dashboard-card dashboard-card-favorites">
        <div><p class="eyebrow">收藏</p><h2>我的收藏</h2></div>
        <p>集中查看收藏的商品和关注的店铺。</p>
        <div class="actions dashboard-actions"><RouterLink to="/me/favorites/products">商品收藏</RouterLink><RouterLink to="/me/favorites/stores">店铺收藏</RouterLink></div>
      </article>

      <article class="card dashboard-card dashboard-card-security">
        <div><p class="eyebrow">账户</p><h2>账号与安全</h2></div>
        <p>管理密码、联系方式和登录设备。</p>
        <RouterLink to="/me/settings/security">进入安全设置</RouterLink>
      </article>
    </div>
  </section>
</template>
