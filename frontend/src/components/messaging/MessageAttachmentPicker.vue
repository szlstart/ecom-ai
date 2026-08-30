<script setup lang="ts">
import { computed } from 'vue'

export type MessagePickerProduct = {
  product_id: string
  product_name: string
  image_url: string | null
  price_label: string
  sku_id: string | null
  meta?: string
}

export type MessagePickerOrder = {
  order_id: string
  title: string
  image_url: string | null
  amount_label: string
  status_label: string
}

const props = withDefaults(defineProps<{
  open: boolean
  products?: MessagePickerProduct[]
  orders?: MessagePickerOrder[]
  loading?: boolean
  sendingId?: string | null
  title?: string
}>(), {
  products: () => [], orders: () => [], loading: false, sendingId: null, title: '发送卡片',
})

const emit = defineEmits<{
  close: []
  product: [item: MessagePickerProduct]
  order: [item: MessagePickerOrder]
}>()

const hasItems = computed(() => props.products.length > 0 || props.orders.length > 0)
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="message-picker-overlay" @mousedown.self="emit('close')">
      <section class="message-picker-dialog" role="dialog" aria-modal="true" :aria-label="title">
        <header><div><span>＋</span><div><strong>{{ title }}</strong><small>以清晰的商品或订单卡片发送，对方可直接查看</small></div></div><button type="button" aria-label="关闭" @click="emit('close')">×</button></header>
        <div class="message-picker-content">
          <p v-if="loading" class="message-picker-state">正在读取可发送内容…</p>
          <template v-else-if="hasItems">
            <section v-if="products.length"><h3>店铺商品</h3><div class="message-picker-grid"><article v-for="item in products" :key="item.product_id"><span class="message-picker-cover"><img v-if="item.image_url" :src="item.image_url" :alt="item.product_name" /><i v-else>商</i></span><div><strong>{{ item.product_name }}</strong><small>{{ item.meta || '商品卡片' }}</small><b>{{ item.price_label }}</b></div><button type="button" :disabled="Boolean(sendingId)" @click="emit('product', item)">{{ sendingId === item.product_id ? '发送中…' : '发送' }}</button></article></div></section>
            <section v-if="orders.length"><h3>本店订单</h3><div class="message-picker-grid"><article v-for="item in orders" :key="item.order_id"><span class="message-picker-cover"><img v-if="item.image_url" :src="item.image_url" :alt="item.title" /><i v-else>单</i></span><div><strong>{{ item.title }}</strong><small>{{ item.status_label }}</small><b>{{ item.amount_label }}</b></div><button type="button" :disabled="Boolean(sendingId)" @click="emit('order', item)">{{ sendingId === item.order_id ? '发送中…' : '发送' }}</button></article></div></section>
          </template>
          <div v-else class="message-picker-empty"><span>◇</span><strong>暂无可发送内容</strong><small>本店还没有可展示商品或关联订单。</small></div>
        </div>
      </section>
    </div>
  </Teleport>
</template>

<style scoped>
.message-picker-overlay{position:fixed;inset:0;z-index:1400;padding:24px;display:grid;place-items:center;background:rgb(15 24 35 / 54%);backdrop-filter:blur(7px)}
.message-picker-dialog{width:min(760px,calc(100vw - 32px));max-height:min(720px,calc(100vh - 48px));overflow:hidden;display:grid;grid-template-rows:auto minmax(0,1fr);border:1px solid rgb(255 255 255 / 70%);border-radius:22px;background:#fff;box-shadow:0 28px 90px rgb(7 18 34 / 28%)}
.message-picker-dialog>header{padding:17px 19px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid #e8ebf0;background:linear-gradient(135deg,#f9fbff,#f4faf7)}.message-picker-dialog>header>div{display:flex;align-items:center;gap:11px}.message-picker-dialog>header>div>span{width:38px;height:38px;display:grid;place-items:center;color:#fff;border-radius:12px;background:linear-gradient(145deg,#335de2,#258d6b);font-size:1.25rem}.message-picker-dialog>header div div{display:grid;gap:3px}.message-picker-dialog>header strong{font-size:1rem}.message-picker-dialog>header small{color:#7b8491;font-size:.7rem}.message-picker-dialog>header>button{width:34px;height:34px;padding:0;color:#596472;border:1px solid #dfe4ea;border-radius:10px;background:#fff;font-size:1.25rem}
.message-picker-content{padding:17px;overflow-y:auto;display:grid;align-content:start;gap:18px}.message-picker-content section{display:grid;gap:9px}.message-picker-content h3{margin:0;color:#596573;font-size:.75rem}.message-picker-grid{display:grid;gap:8px}.message-picker-grid article{padding:9px;display:grid;grid-template-columns:58px minmax(0,1fr) auto;align-items:center;gap:11px;border:1px solid #e4e8ed;border-radius:14px;background:#fff}.message-picker-cover,.message-picker-cover img,.message-picker-cover i{width:58px;height:58px}.message-picker-cover img,.message-picker-cover i{display:grid;place-items:center;border-radius:10px;background:#edf1f5;object-fit:cover;font-style:normal;font-weight:800}.message-picker-grid article>div{min-width:0;display:grid;gap:3px}.message-picker-grid article strong,.message-picker-grid article small{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.message-picker-grid article strong{font-size:.82rem}.message-picker-grid article small{color:#7f8996;font-size:.66rem}.message-picker-grid article b{color:#d83d34;font-size:.82rem}.message-picker-grid article>button{min-width:66px}.message-picker-state,.message-picker-empty{margin:0;padding:45px;text-align:center;color:#7d8794}.message-picker-empty{display:grid;justify-items:center;gap:6px}.message-picker-empty>span{font-size:1.8rem}.message-picker-empty strong{color:#33404d}
@media(max-width:600px){.message-picker-overlay{padding:10px}.message-picker-dialog{max-height:calc(100vh - 20px)}.message-picker-grid article{grid-template-columns:48px minmax(0,1fr) auto}.message-picker-cover,.message-picker-cover img,.message-picker-cover i{width:48px;height:48px}}
</style>
