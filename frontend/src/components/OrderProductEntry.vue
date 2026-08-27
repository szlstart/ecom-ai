<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

defineOptions({ inheritAttrs: false })

const props = defineProps<{
  productId: string
  skuId: string
  productName: string
  productAvailable: boolean
}>()

const router = useRouter()
const unavailableVisible = ref(false)

async function openProduct() {
  if (!props.productAvailable) {
    unavailableVisible.value = true
    return
  }
  await router.push({ path: `/products/${props.productId}`, query: { sku_id: props.skuId } })
}

function closeUnavailableNotice() {
  unavailableVisible.value = false
}
</script>

<template>
  <button
    v-bind="$attrs"
    type="button"
    class="order-product-entry"
    :aria-label="productAvailable ? `查看商品：${productName}` : `${productName}已下架，点击查看提示`"
    @click="openProduct"
  >
    <slot />
  </button>
  <Teleport to="body">
    <div
      v-if="unavailableVisible"
      class="order-product-unavailable-backdrop"
      @mousedown.self="closeUnavailableNotice"
      @keydown.esc="closeUnavailableNotice"
    >
      <section
        class="order-product-unavailable-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="order-product-unavailable-title"
      >
        <span aria-hidden="true">!</span>
        <h2 id="order-product-unavailable-title">该商品已被下架</h2>
        <p>“{{ productName }}”当前无法查看或购买，历史订单信息仍会完整保留。</p>
        <button type="button" autofocus @click="closeUnavailableNotice">我知道了</button>
      </section>
    </div>
  </Teleport>
</template>
