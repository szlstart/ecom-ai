<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'

import { checkRefundEligibility, createRefundApplication, type RefundEligibility, type RefundEligibilityInput } from '@/api/after-sales'
import { formatMoney } from '@/api/catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import { getMyOrder, type OrderDetail } from '@/api/orders'
import { confirmAction } from '@/composables/confirmation'
import { useUserAuthStore } from '@/stores/user-auth'

const route = useRoute()
const router = useRouter()
const auth = useUserAuthStore()
const order = ref<OrderDetail | null>(null)
const refundType = ref<'refund_only' | 'return_and_refund'>('refund_only')
const reasonCode = ref('NO_LONGER_NEEDED')
const reasonDetail = ref('')
const eligibility = ref<RefundEligibility | null>(null)
const error = ref('')
const busy = ref(false)

const reasonOptions = [
  ['NO_LONGER_NEEDED', '不想要了／拍错了'],
  ['QUALITY_ISSUE', '商品存在质量问题'],
  ['NOT_AS_DESCRIBED', '与商品描述不符'],
  ['WRONG_ITEM', '商家发错商品或款式'],
  ['DAMAGED', '收到时商品破损'],
  ['MISSING_PARTS', '缺少商品或配件'],
  ['DELIVERY_ISSUE', '物流长时间未送达'],
  ['OTHER', '其他原因'],
] as const

const refundableItems = computed(() => (order.value?.items ?? [])
  .map((item) => ({ ...item, refundable_quantity: Math.max(0, item.quantity - item.refunded_quantity) }))
  .filter((item) => item.refundable_quantity > 0))

const input = computed<RefundEligibilityInput | null>(() => {
  if (!order.value || !refundableItems.value.length) return null
  return {
    order_id: order.value.order_id,
    items: refundableItems.value.map((item) => ({ order_item_id: item.order_item_id, quantity: item.refundable_quantity })),
    requested_type: refundType.value,
    reason_code: reasonCode.value,
  }
})

const blockingMessage = computed(() => {
  if (!eligibility.value?.blocking_reasons.length) return '订单当前状态暂不支持申请，请稍后再试或联系专属客服。'
  const translations: Record<string, string> = {
    ORDER_NOT_COMPLETED: '订单尚未达到可申请售后的状态',
    REFUND_WINDOW_EXPIRED: '该订单已超过售后申请期限',
    NO_REFUNDABLE_QUANTITY: '商品已无可申请售后的数量',
    ACTIVE_REFUND_EXISTS: '该商品已有进行中的售后申请',
    REFUND_TYPE_NOT_ALLOWED: '当前订单不支持所选售后方式',
  }
  return eligibility.value.blocking_reasons.map((item) => translations[item] ?? '订单当前状态暂不支持本次申请').join('；')
})

function resetPreview() { eligibility.value = null }

async function load() {
  if (!auth.accessToken) return
  error.value = ''
  try { order.value = (await getMyOrder(String(route.params.orderId), auth.accessToken)).data }
  catch (cause) { error.value = errorMessage(cause) }
}

async function preview() {
  if (!input.value || !auth.accessToken) return
  busy.value = true
  error.value = ''
  try { eligibility.value = (await checkRefundEligibility(input.value, auth.accessToken)).data }
  catch (cause) { error.value = errorMessage(cause) }
  finally { busy.value = false }
}

async function submit() {
  if (!input.value || !eligibility.value?.eligible || !auth.accessToken) return
  if (!await confirmAction(`确认提交售后申请？预计退款 ${formatMoney(eligibility.value.suggested_refund_amount)}。`, { title: '确认售后信息', confirmText: '提交申请' })) return
  busy.value = true
  error.value = ''
  try {
    const result = await createRefundApplication(input.value, eligibility.value, reasonDetail.value.trim() || null, auth.accessToken)
    await router.replace(`/me/after-sales/${result.data.refund_id}`)
  } catch (cause) {
    error.value = errorMessage(cause)
    eligibility.value = null
  } finally { busy.value = false }
}

watch([refundType, reasonCode], resetPreview)
onMounted(load)
</script>

<template>
  <main class="refund-page page-shell">
    <RouterLink class="back-link" :to="`/me/orders/${route.params.orderId}`">← 返回订单详情</RouterLink>
    <header class="refund-heading">
      <div><p class="eyebrow">售后服务</p><h1>申请售后</h1><p>确认商品与问题，我们会清晰展示预计退款金额后再请你提交。</p></div>
      <ol aria-label="申请步骤"><li class="active"><b>1</b><span>填写申请</span></li><li :class="{ active: eligibility }"><b>2</b><span>确认金额</span></li><li><b>3</b><span>等待处理</span></li></ol>
    </header>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>

    <form v-if="order" class="refund-layout" @submit.prevent="eligibility ? submit() : preview()">
      <section class="refund-main">
        <article class="refund-card">
          <header><div><span>01</span><div><h2>本次售后商品</h2><p>系统已按订单锁定尚可售后的商品与数量，无需重复选择。</p></div></div><RouterLink :to="`/me/orders/${order.order_id}`">查看订单</RouterLink></header>
          <div v-if="refundableItems.length" class="refund-products">
            <article v-for="item in refundableItems" :key="item.order_item_id">
              <span class="refund-product-image"><img v-if="item.image_url" :src="resolveApiAssetUrl(item.image_url) ?? ''" :alt="item.product_name" /><i v-else>商品</i></span>
              <div><strong>{{ item.product_name }}</strong><small>款式：{{ item.sku_name }}</small><small>本次申请数量：{{ item.refundable_quantity }} 件</small></div>
              <b>{{ formatMoney(item.payable_amount) }}</b>
            </article>
          </div>
          <p v-else class="refund-empty">这些商品当前没有可申请售后的数量。</p>
        </article>

        <article class="refund-card">
          <header><div><span>02</span><div><h2>选择售后方式</h2><p>根据是否需要寄回商品选择。</p></div></div></header>
          <div class="refund-type-options">
            <label :class="{ selected: refundType === 'refund_only' }"><input v-model="refundType" type="radio" value="refund_only" /><span><b>仅退款</b><small>无需寄回商品，商家审核后原路退款</small></span><i>✓</i></label>
            <label :class="{ selected: refundType === 'return_and_refund' }"><input v-model="refundType" type="radio" value="return_and_refund" /><span><b>退货退款</b><small>审核通过后寄回商品，签收后退款</small></span><i>✓</i></label>
          </div>
        </article>

        <article class="refund-card">
          <header><div><span>03</span><div><h2>说明遇到的问题</h2><p>准确的原因和说明有助于更快处理。</p></div></div></header>
          <label class="refund-field"><span>申请原因</span><select v-model="reasonCode"><option v-for="option in reasonOptions" :key="option[0]" :value="option[0]">{{ option[1] }}</option></select></label>
          <label class="refund-field"><span>补充说明 <small>选填</small></span><textarea v-model="reasonDetail" maxlength="500" rows="5" placeholder="可以描述商品问题、期望处理方式等信息（最多 500 字）" /><small class="character-count">{{ reasonDetail.length }} / 500</small></label>
        </article>
      </section>

      <aside class="refund-summary">
        <article>
          <p class="eyebrow">申请摘要</p><h2>{{ refundType === 'refund_only' ? '仅退款' : '退货退款' }}</h2>
          <dl><div><dt>商品数量</dt><dd>{{ refundableItems.reduce((total, item) => total + item.refundable_quantity, 0) }} 件</dd></div><div><dt>订单实付</dt><dd>{{ formatMoney(order.amounts.paid_amount) }}</dd></div><div v-if="eligibility?.eligible" class="refund-total"><dt>预计退款</dt><dd>{{ formatMoney(eligibility.suggested_refund_amount) }}</dd></div></dl>
          <div v-if="eligibility" class="eligibility-result" :class="eligibility.eligible ? 'success' : 'failed'" aria-live="polite"><strong>{{ eligibility.eligible ? '✓ 售后资格校验通过' : '暂时无法提交' }}</strong><p v-if="!eligibility.eligible">{{ blockingMessage }}</p><p v-else>金额与商品已确认，请核对后提交申请。</p></div>
          <button type="submit" :disabled="busy || !input || (eligibility !== null && !eligibility.eligible)">{{ busy ? '正在处理…' : eligibility ? '确认提交申请' : '下一步，确认退款金额' }}</button>
          <button v-if="eligibility" type="button" class="secondary" :disabled="busy" @click="resetPreview">返回修改</button>
          <small class="refund-policy">提交后可在“我的订单 → 售后”查看处理进度。平台与商家会按照订单和售后规则进行审核。</small>
        </article>
      </aside>
    </form>
    <p v-else-if="!error" class="refund-loading">正在加载订单信息…</p>
  </main>
</template>

<style scoped>
.refund-page { display: grid; gap: 20px; }
.refund-heading { padding: 26px 30px; display: flex; justify-content: space-between; align-items: center; gap: 30px; color: #fff; border-radius: 22px; background: linear-gradient(125deg, #172b62, #3158d8 70%, #5578df); }
.refund-heading h1, .refund-heading p { margin: 0; }.refund-heading h1 { margin-block: 4px 7px; }.refund-heading .eyebrow { color: #cbd7ff; }.refund-heading > div > p:last-child { color: #dce4ff; }
.refund-heading ol { margin: 0; padding: 0; display: flex; gap: 22px; list-style: none; }.refund-heading li { display: grid; justify-items: center; gap: 5px; color: #aebeea; font-size: .72rem; }.refund-heading b { width: 29px; height: 29px; display: grid; place-items: center; border: 1px solid rgb(255 255 255 / 32%); border-radius: 50%; }.refund-heading li.active { color: #fff; }.refund-heading li.active b { color: #3158d8; background: #fff; }
.refund-layout { display: grid; grid-template-columns: minmax(0, 1fr) minmax(300px, 350px); align-items: start; gap: 20px; }.refund-main { display: grid; gap: 18px; }.refund-card, .refund-summary > article { padding: 22px; border: 1px solid #dfe4ed; border-radius: 18px; background: #fff; box-shadow: 0 10px 32px rgb(27 49 101 / 6%); }.refund-card { display: grid; gap: 18px; }.refund-card > header { display: flex; align-items: center; justify-content: space-between; gap: 18px; }.refund-card > header > div { display: flex; align-items: center; gap: 12px; }.refund-card > header > div > span { width: 34px; height: 34px; display: grid; flex: 0 0 auto; place-items: center; color: #3158d8; border-radius: 11px; background: #edf1ff; font-size: .72rem; font-weight: 900; }.refund-card h2, .refund-card p { margin: 0; }.refund-card header p { margin-top: 3px; color: #778297; font-size: .78rem; }
.refund-products { display: grid; gap: 10px; }.refund-products > article { min-width: 0; padding: 12px; display: grid; grid-template-columns: 70px minmax(0,1fr) auto; align-items: center; gap: 14px; border-radius: 14px; background: #f7f9fc; }.refund-product-image, .refund-product-image img, .refund-product-image i { width: 70px; height: 70px; }.refund-product-image { display: block; }.refund-product-image img, .refund-product-image i { display: grid; place-items: center; border-radius: 11px; object-fit: cover; background: #e9edf5; font-style: normal; font-size: .72rem; }.refund-products article > div { min-width: 0; display: grid; gap: 5px; }.refund-products article > div strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.refund-products small { color: #7a8599; }.refund-products article > b { color: #d43b36; }.refund-empty { padding: 16px; color: #8c3941; border-radius: 12px; background: #fff2f3; }
.refund-type-options { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 12px; }.refund-type-options label { padding: 16px; display: grid; grid-template-columns: 0 minmax(0,1fr) 22px; gap: 10px; align-items: center; cursor: pointer; border: 1px solid #dfe4ed; border-radius: 14px; }.refund-type-options input { opacity: 0; }.refund-type-options span { display: grid; gap: 5px; }.refund-type-options small { color: #7b8498; }.refund-type-options i { width: 22px; height: 22px; display: none; place-items: center; color: #fff; border-radius: 50%; background: #3158d8; font-style: normal; }.refund-type-options label.selected { border-color: #3158d8; background: #f3f6ff; box-shadow: 0 0 0 2px rgb(49 88 216 / 8%); }.refund-type-options label.selected i { display: grid; }
.refund-field { display: grid; gap: 8px; }.refund-field > span { font-weight: 750; }.refund-field > span small { color: #8c95a6; font-weight: 500; }.refund-field textarea { resize: vertical; }.character-count { justify-self: end; color: #8a94a7; }
.refund-summary { position: sticky; top: 92px; }.refund-summary > article { display: grid; gap: 15px; }.refund-summary h2, .refund-summary p { margin: 0; }.refund-summary dl { margin: 0; display: grid; gap: 12px; }.refund-summary dl div { display: flex; justify-content: space-between; gap: 16px; }.refund-summary dt { color: #717d92; }.refund-summary dd { margin: 0; font-weight: 750; }.refund-summary .refund-total { padding-top: 14px; align-items: baseline; border-top: 1px solid #e5e8ef; }.refund-summary .refund-total dd { color: #d43b36; font-size: 1.45rem; }.eligibility-result { padding: 13px; display: grid; gap: 6px; border-radius: 12px; font-size: .78rem; }.eligibility-result.success { color: #1f704f; background: #eaf6ef; }.eligibility-result.failed { color: #8c3941; background: #fff0f1; }.eligibility-result p { line-height: 1.6; }.refund-summary button { width: 100%; }.refund-policy { color: #7b8498; line-height: 1.6; }.refund-loading { min-height: 320px; display: grid; place-items: center; color: #778297; }
@media (max-width: 850px) { .refund-heading { align-items: flex-start; flex-direction: column; }.refund-layout { grid-template-columns: 1fr; }.refund-summary { position: static; }.refund-type-options { grid-template-columns: 1fr; } }
@media (max-width: 560px) { .refund-heading, .refund-card, .refund-summary > article { padding: 18px; }.refund-heading ol { width: 100%; justify-content: space-between; gap: 8px; }.refund-products > article { grid-template-columns: 58px minmax(0,1fr); }.refund-product-image, .refund-product-image img, .refund-product-image i { width: 58px; height: 58px; }.refund-products article > b { grid-column: 2; } }
</style>
