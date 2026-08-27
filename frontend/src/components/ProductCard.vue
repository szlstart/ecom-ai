<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import { formatMoney, type ProductCardData } from '@/api/catalog'
import { resolveApiAssetUrl } from '@/api/http'

const props = defineProps<{ product: ProductCardData; returnTo?: string }>()
const detailTarget = computed(() => ({
  path: `/products/${props.product.product_id}`,
  query: props.returnTo ? { return_to: props.returnTo } : undefined,
}))
</script>

<template>
  <article class="product-card" :id="`product-${product.product_id}`">
    <RouterLink :to="detailTarget" class="product-card-image" :aria-label="`查看 ${product.product_name}`">
      <img
        v-if="product.main_image"
        :src="resolveApiAssetUrl(product.main_image.thumbnail_url) || undefined"
        :alt="product.main_image.alt_text || product.product_name"
        loading="lazy"
        width="320"
        height="320"
      />
      <span v-else class="image-placeholder" aria-hidden="true">暂无图片</span>
    </RouterLink>
    <div class="product-card-body">
      <RouterLink :to="detailTarget" class="product-name">{{ product.product_name }}</RouterLink>
      <p class="product-price">
        {{ formatMoney(product.price) }}
        <small v-if="product.price_range">起</small>
      </p>
      <div class="product-meta">
        <span>已售 {{ product.sales_count }}</span>
        <span>评分 {{ product.rating_score }}</span>
      </div>
      <RouterLink :to="`/stores/${product.store_id}`" class="store-link">
        {{ product.store_name }} →
      </RouterLink>
    </div>
  </article>
</template>
