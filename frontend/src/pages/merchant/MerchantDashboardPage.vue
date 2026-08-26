<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { adminGet, requireAdminToken, type AdminProductSummary, type AdminStore } from '@/api/admin-catalog'
import { listAdminReviews, type AdminReview } from '@/api/admin-reviews'
import { listSupportTickets, type SupportTicket } from '@/api/admin-support'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const loading = ref(true)
const error = ref('')
const store = ref<AdminStore | null>(null)
const products = ref<AdminProductSummary[]>([])
const reviews = ref<AdminReview[]>([])
const tickets = ref<SupportTicket[]>([])
const onSale = computed(() => products.value.filter((item) => item.status === 'on_sale').length)
const drafts = computed(() => products.value.filter((item) => ['draft', 'rejected', 'off_shelf'].includes(item.status)).length)
const unansweredReviews = computed(() => reviews.value.filter((item) => item.review_status === 'published' && !item.merchant_reply).length)
const openTickets = computed(() => tickets.value.filter((item) => !['resolved', 'closed'].includes(item.ticket_status)).length)

function token() { return requireAdminToken(auth.accessToken) }
function statusLabel(value: string) { return ({ draft: '草稿', pending_review: '审核中', rejected: '需修改', on_sale: '销售中', off_shelf: '已下架' } as Record<string, string>)[value] ?? value }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [storeResult, productResult, reviewResult, ticketResult] = await Promise.all([
      adminGet<{ items: AdminStore[]; next_cursor: string | null }>('/admin/stores?limit=20', token()),
      adminGet<{ items: AdminProductSummary[]; next_cursor: string | null }>('/admin/products?limit=100', token()),
      listAdminReviews(token()),
      listSupportTickets({ queueType: 'store' }, token()),
    ])
    store.value = storeResult.data.items[0] ?? null
    products.value = productResult.data.items
    reviews.value = reviewResult.data.items
    tickets.value = ticketResult.data.items
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><p class="eyebrow">今日概览</p><h1>{{ store?.store_name || '店铺工作台' }}</h1><p>把需要处理的事情放在最前面，减少在菜单间来回寻找。</p></div><RouterLink class="button-link" to="/merchant/products/new">发布新商品</RouterLink></header>
    <PageState :loading="loading" :error="error" :empty="false" @retry="load">
      <div class="merchant-metric-grid">
        <RouterLink to="/merchant/products?status=on_sale"><span>销售中的商品</span><strong>{{ onSale }}</strong><small>查看全部在售商品</small></RouterLink>
        <RouterLink to="/merchant/products?status=draft"><span>待完善商品</span><strong>{{ drafts }}</strong><small>继续编辑并提交上架</small></RouterLink>
        <RouterLink to="/merchant/support"><span>待处理咨询</span><strong>{{ openTickets }}</strong><small>及时回复顾客问题</small></RouterLink>
        <RouterLink to="/merchant/reviews?unanswered=1"><span>待回复评价</span><strong>{{ unansweredReviews }}</strong><small>感谢并回应顾客</small></RouterLink>
      </div>
      <div class="merchant-dashboard-grid">
        <article class="card merchant-task-card"><header><div><p class="eyebrow">商品</p><h2>最近更新</h2></div><RouterLink to="/merchant/products">全部商品</RouterLink></header><div class="merchant-compact-list"><RouterLink v-for="item in products.slice(0, 5)" :key="item.product_id" :to="`/merchant/products/${item.product_id}`"><span><strong>{{ item.product_name }}</strong><small>{{ item.category_name }} · ¥{{ item.min_price }}</small></span><span class="badge">{{ statusLabel(item.status) }}</span></RouterLink><p v-if="!products.length" class="muted">还没有商品，先发布第一件商品吧。</p></div></article>
        <article class="card merchant-task-card"><header><div><p class="eyebrow">顾客</p><h2>等待处理</h2></div></header><div class="merchant-action-list"><RouterLink to="/merchant/support"><span>客户咨询</span><strong>{{ openTickets }} 件</strong></RouterLink><RouterLink to="/merchant/reviews?unanswered=1"><span>未回复评价</span><strong>{{ unansweredReviews }} 条</strong></RouterLink><RouterLink to="/merchant/inventory"><span>库存维护</span><strong>进入调整</strong></RouterLink></div></article>
      </div>
    </PageState>
  </section>
</template>
