<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { confirmAction } from '@/composables/confirmation'
import { useUserAuthStore } from '@/stores/user-auth'

interface Money { minor_units: string; currency: string }
interface Wallet { wallet_id: string; balance: Money; total_recharged: Money; wallet_status: string; version: number }
interface Transaction { transaction_id: string; direction: 'credit' | 'debit'; amount: Money; balance_after: Money; channel: string | null; description: string; occurred_at: string }
interface RechargeResult { wallet: Wallet }

const auth = useUserAuthStore()
const wallet = ref<Wallet | null>(null)
const transactions = ref<Transaction[]>([])
const amount = ref('100')
const channel = ref<'wechat' | 'alipay'>('wechat')
const loading = ref(true)
const charging = ref(false)
const error = ref('')
const notice = ref('')
const presets = [50, 100, 200, 500, 1000]

function money(value?: Money) { return `¥${(Number(value?.minor_units ?? 0) / 100).toFixed(2)}` }
function channelLabel(value: string | null) { return value === 'wechat' ? '微信' : value === 'alipay' ? '支付宝' : value === 'balance' ? '账户余额' : '—' }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [walletResult, transactionResult] = await Promise.all([
      apiRequest<Wallet>('/users/me/wallet', {}, auth.accessToken),
      apiRequest<{ items: Transaction[] }>('/users/me/wallet/transactions?limit=50', {}, auth.accessToken),
    ])
    wallet.value = walletResult.data
    transactions.value = transactionResult.data.items
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function recharge() {
  const yuan = Number(amount.value)
  error.value = ''
  notice.value = ''
  if (!Number.isFinite(yuan) || yuan < 1 || yuan > 50000 || Math.round(yuan * 100) !== yuan * 100) {
    error.value = '充值金额需为 1.00 至 50,000.00 元，最多保留两位小数。'
    return
  }
  if (!await confirmAction(`这是模拟充值，不会调用真实${channel.value === 'wechat' ? '微信' : '支付宝'}。确认模拟充值 ¥${yuan.toFixed(2)} 吗？`, { title: '确认模拟充值', confirmText: '确认充值' })) return
  charging.value = true
  try {
    const result = await apiRequest<RechargeResult>('/users/me/wallet/recharges', {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey('wallet-recharge') },
      body: JSON.stringify({ channel: channel.value, amount: { minor_units: String(Math.round(yuan * 100)), currency: 'CNY' } }),
    }, auth.accessToken)
    wallet.value = result.data.wallet
    notice.value = `模拟充值成功，当前余额 ${money(result.data.wallet.balance)}。`
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { charging.value = false }
}

onMounted(load)
</script>

<template>
  <section class="wallet-page">
    <header class="page-heading"><div><p class="eyebrow">我的</p><h1>账户余额</h1><p class="muted">当前仅提供模拟充值，用于开发和体验，不会发起真实扣款。</p></div></header>
    <p v-if="notice" class="alert success" role="status">{{ notice }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div v-if="!loading && wallet" class="wallet-grid"><article class="wallet-balance-card"><span>可用余额</span><strong>{{ money(wallet.balance) }}</strong><small>累计模拟充值 {{ money(wallet.total_recharged) }}</small></article><form class="card wallet-recharge-card" @submit.prevent="recharge"><h2>模拟充值</h2><fieldset><legend>选择渠道</legend><div class="wallet-channel-options"><label :class="{ active: channel === 'wechat' }"><input v-model="channel" type="radio" value="wechat" />微信</label><label :class="{ active: channel === 'alipay' }"><input v-model="channel" type="radio" value="alipay" />支付宝</label></div></fieldset><label>充值金额（元）<input v-model="amount" type="number" min="1" max="50000" step="0.01" required /></label><div class="wallet-presets"><button v-for="item in presets" :key="item" type="button" class="secondary small" @click="amount = String(item)">¥{{ item }}</button></div><div class="alert warning">模拟流程：确认后立即到账，不会打开微信或支付宝，也不会产生真实资金交易。</div><button :disabled="charging">{{ charging ? '正在模拟到账…' : '确认模拟充值' }}</button></form></div>
    <article class="card"><h2>余额明细</h2><div v-if="transactions.length" class="wallet-transactions"><div v-for="item in transactions" :key="item.transaction_id"><span><strong>{{ item.description }}</strong><small>{{ channelLabel(item.channel) }} · {{ new Date(item.occurred_at).toLocaleString('zh-CN') }}</small></span><span><b :class="item.direction === 'debit' ? 'wallet-debit' : 'wallet-credit'">{{ item.direction === 'debit' ? '−' : '+' }}{{ money(item.amount) }}</b><small>余额 {{ money(item.balance_after) }}</small></span></div></div><p v-else class="muted">暂无余额变动。</p></article>
  </section>
</template>
