<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { getFavoriteProducts, setProductFavorite, type ProductCardData } from '@/api/catalog'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import ProductCard from '@/components/ProductCard.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const products = ref<ProductCardData[]>([])
const loading = ref(true)
const error = ref('')
const removing = ref<string | null>(null)

async function load() {
  if (!auth.accessToken) return
  loading.value = true
  error.value = ''
  try { products.value = (await getFavoriteProducts(auth.accessToken)).data.items }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function remove(product: ProductCardData) {
  if (!auth.accessToken) return
  removing.value = product.product_id
  try {
    await setProductFavorite(product.product_id, false, auth.accessToken)
    products.value = products.value.filter((item) => item.product_id !== product.product_id)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { removing.value = null }
}

onMounted(load)
</script>

<template>
  <section class="storefront-stack favorites-page">
    <header class="favorites-hero"><div><p class="eyebrow">我的收藏</p><h1>把喜欢的商品留在这里</h1><p>价格、销量和店铺信息均来自当前实时数据，点击商品即可继续选择款式并购买。</p></div><div><strong>{{ products.length }}</strong><span>件收藏商品</span><RouterLink class="button-link" to="/search">继续逛逛</RouterLink></div></header>
    <div class="favorites-toolbar"><strong>全部商品</strong><span>共 {{ products.length }} 件</span><RouterLink to="/me">返回我的</RouterLink></div>
    <PageState :loading="loading" :error="error" :empty="!loading && !error && products.length === 0" empty-title="还没有收藏商品" empty-detail="浏览商品时点击收藏，之后可以在这里快速找到。" @retry="load">
      <template #action><RouterLink class="button-link" to="/search">去逛逛</RouterLink></template>
      <div class="favorite-grid"><div v-for="product in products" :key="product.product_id" class="favorite-item"><ProductCard :product="product" return-to="/me/favorites/products" /><div class="favorite-actions"><RouterLink :to="`/products/${product.product_id}?return_to=/me/favorites/products`">查看商品</RouterLink><button type="button" class="secondary small" :disabled="removing === product.product_id" @click="remove(product)">{{ removing === product.product_id ? '移除中…' : '移除收藏' }}</button></div></div></div>
    </PageState>
  </section>
</template>
