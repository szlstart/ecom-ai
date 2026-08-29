<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { claimAdminRefund, decideAdminRefund, getAdminRefund, isApprovalRequired } from '@/api/admin-after-sales'
import type { RefundApplication } from '@/api/after-sales'
import { formatMoney } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute()
const router = useRouter()
const auth = useAdminAuthStore()
const item = ref<RefundApplication | null>(null)
const etag = ref('')
const reason = ref('')
const loading = ref(true)
const busy = ref(false)
const error = ref('')
const notice = ref('')
const canReview = computed(() => item.value && ['submitted', 'merchant_review'].includes(item.value.refund_status))

const statusLabels: Record<string, string> = {
  submitted: '等待领取', merchant_review: '商家审核中', approved: '已批准',
  waiting_return: '等待顾客退货', returning: '顾客退货中', received: '已收到退货',
  refunding: '退款处理中', succeeded: '退款成功', rejected: '已拒绝',
  cancelled: '顾客已撤销', closed: '已关闭',
}

async function load() {
  loading.value = true; error.value = ''
  try {
    const response = await getAdminRefund(String(route.params.refundId), auth.accessToken!)
    item.value = response.data
    etag.value = response.headers.get('etag') ?? ''
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function claim() {
  if (!item.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await claimAdminRefund(item.value.refund_id, etag.value, auth.accessToken!)
    item.value = response.data; etag.value = response.headers.get('etag') ?? ''
    notice.value = '售后申请已领取，现在可以提交审核结果。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function decide(decision: 'approve' | 'reject') {
  if (!item.value || reason.value.trim().length < 2) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const response = await decideAdminRefund(item.value.refund_id, etag.value, decision, decision === 'approve' ? 'MERCHANT_APPROVED' : 'MERCHANT_REJECTED', reason.value.trim(), auth.accessToken!)
    if (isApprovalRequired(response.data)) {
      notice.value = '该退款金额达到平台复核门槛，申请已提交平台审批；审批完成后系统会继续处理。'
      await load()
      return
    }
    item.value = response.data; etag.value = response.headers.get('etag') ?? ''
    notice.value = decision === 'approve' ? '已批准申请，后续退款或退货流程已启动。' : '已拒绝申请，顾客端状态已同步。'
    reason.value = ''
  } catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="merchant-page-stack">
    <header class="merchant-page-heading"><div><button type="button" class="link-button" @click="router.push('/merchant/after-sales')">← 返回售后列表</button><p class="eyebrow">售后单 {{ item?.refund_id }}</p><h1>{{ item ? (statusLabels[item.refund_status] ?? item.refund_status) : '售后详情' }}</h1></div><button type="button" class="secondary" :disabled="loading" @click="load">刷新状态</button></header>
    <p v-if="notice" class="alert success" role="status">{{ notice }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <PageState :loading="loading" :error="''" :empty="!item" empty-title="售后申请不存在" @retry="load">
      <template v-if="item">
        <div class="settings-grid"><article class="card"><h2>申请信息</h2><dl class="detail-list"><div><dt>关联订单</dt><dd>{{ item.order_id }}</dd></div><div><dt>申请类型</dt><dd>{{ item.refund_type === 'refund_only' ? '仅退款' : '退货退款' }}</dd></div><div><dt>申请金额</dt><dd>{{ formatMoney(item.requested_amount) }}</dd></div><div><dt>申请原因</dt><dd>{{ item.reason_detail || item.reason_code }}</dd></div><div><dt>提交时间</dt><dd>{{ new Date(item.submitted_at).toLocaleString('zh-CN') }}</dd></div></dl></article><article class="card"><h2>商品明细</h2><div v-for="line in item.items" :key="line.order_item_id" class="merchant-refund-line"><span>订单商品 {{ line.order_item_id }}</span><b>× {{ line.quantity }} · {{ formatMoney(line.requested_amount) }}</b></div></article></div>
        <article v-if="canReview" class="card merchant-refund-decision"><template v-if="!item.claimed"><h2>领取后开始审核</h2><p>领取可以避免多人重复处理；领取前不会改变顾客的售后状态。</p><button :disabled="busy" @click="claim">{{ busy ? '正在领取…' : '领取这条售后' }}</button></template><form v-else @submit.prevent><h2>提交审核结果</h2><label>处理说明<textarea v-model.trim="reason" required minlength="2" maxlength="500" placeholder="请填写同意或拒绝的具体依据，顾客可据此理解结果。" /></label><div class="actions"><button type="button" class="danger" :disabled="busy || reason.length < 2" @click="decide('reject')">拒绝申请</button><button type="button" :disabled="busy || reason.length < 2" @click="decide('approve')">批准申请</button></div><small>大额退款会自动进入平台复核，不会由单个店铺账号直接执行。</small></form></article>
        <article v-else class="card"><h2>当前进度</h2><p>该申请当前为“{{ statusLabels[item.refund_status] ?? item.refund_status }}”，无需重复审核。顾客端会同步显示相同进度。</p></article>
      </template>
    </PageState>
  </section>
</template>
