<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { listAdminRefunds } from '@/api/admin-after-sales'
import type { RefundApplication } from '@/api/after-sales'
import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<RefundApplication[]>([])
const loading = ref(true)
const error = ref('')
const nextCursor = ref<string | null>(null)
const loadingMore = ref(false)

const statusLabels: Record<string, string> = {
  submitted: '等待领取', merchant_review: '商家审核中', approved: '已批准',
  waiting_return: '等待顾客退货', returning: '顾客退货中', received: '已收到退货',
  refunding: '退款处理中', succeeded: '退款成功', rejected: '已拒绝',
  cancelled: '顾客已撤销', closed: '已关闭',
}

async function load() {
  loading.value = true; error.value = ''
  try {
    const response = await listAdminRefunds(auth.accessToken!)
    items.value = response.data.items
    nextCursor.value = response.data.next_cursor
  }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function loadMore() {
  if (!nextCursor.value || loadingMore.value) return
  loadingMore.value = true; error.value = ''
  try {
    const response = await listAdminRefunds(auth.accessToken!, nextCursor.value)
    items.value.push(...response.data.items)
    nextCursor.value = response.data.next_cursor
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loadingMore.value = false }
}

onMounted(load)
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">售后服务</p><h1>售后处理</h1><p>这里只展示当前店铺的申请。待处理申请可进入详情领取并审核。</p></div><button type="button" class="secondary" :disabled="loading" @click="load">刷新</button></header>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <PageState :loading="loading" :error="''" :empty="!items.length" empty-title="暂无售后申请" empty-message="顾客提交申请后会出现在这里。" @retry="load">
      <div class="merchant-order-list">
        <RouterLink v-for="item in items" :key="item.refund_id" class="merchant-order-card merchant-after-sale-link" :to="`/merchant/after-sales/${item.refund_id}`">
          <header><div><strong>{{ statusLabels[item.refund_status] ?? item.refund_status }}</strong><small>售后单 {{ item.refund_id }}</small></div><span>{{ new Date(item.submitted_at).toLocaleString('zh-CN') }}</span></header>
          <div><strong>订单 {{ item.order_id }}</strong><p>{{ item.refund_type === 'refund_only' ? '仅退款' : '退货退款' }} · {{ item.reason_detail || item.reason_code }}</p></div>
          <footer><span>申请金额 <b>{{ formatMoney(item.requested_amount) }}</b></span><strong>{{ ['submitted', 'merchant_review'].includes(item.refund_status) ? '去处理 →' : '查看进度 →' }}</strong></footer>
        </RouterLink>
        <button v-if="nextCursor" type="button" class="secondary merchant-load-more" :disabled="loadingMore" @click="loadMore">{{ loadingMore ? '正在加载…' : '加载更多售后' }}</button>
      </div>
    </PageState>
  </section>
</template>
