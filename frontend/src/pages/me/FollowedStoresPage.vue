<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { getFollowedStores, setStoreFollow, type StoreData } from '@/api/catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const stores = ref<StoreData[]>([])
const loading = ref(true)
const error = ref('')
const removing = ref<string | null>(null)

async function load() {
  if (!auth.accessToken) return
  loading.value = true
  error.value = ''
  try { stores.value = (await getFollowedStores(auth.accessToken)).data.items }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function remove(store: StoreData) {
  if (!auth.accessToken) return
  removing.value = store.store_id
  try {
    await setStoreFollow(store.store_id, false, auth.accessToken)
    stores.value = stores.value.filter((item) => item.store_id !== store.store_id)
  } catch (cause) { error.value = errorMessage(cause) }
  finally { removing.value = null }
}

onMounted(load)
</script>

<template>
  <section class="storefront-stack">
    <header class="page-heading"><div><p class="eyebrow">我的收藏</p><h1>收藏的店铺</h1></div><RouterLink to="/me">返回我的</RouterLink></header>
    <PageState :loading="loading" :error="error" :empty="!loading && !error && stores.length === 0" empty-title="还没有收藏店铺" empty-detail="从商品或店铺页面收藏感兴趣的店铺。" @retry="load">
      <template #action><RouterLink class="button-link" to="/search">发现商品</RouterLink></template>
      <div class="store-follow-grid"><article v-for="store in stores" :key="store.store_id" class="card store-follow-card"><RouterLink :to="`/stores/${store.store_id}`" class="store-identity"><img v-if="store.logo_url" :src="resolveApiAssetUrl(store.logo_url) || undefined" alt="" width="64" height="64" /><span v-else class="store-logo-placeholder">店</span><span><strong>{{ store.store_name }}</strong><small>评分 {{ store.rating_score }} · 在售 {{ store.active_product_count }} 件</small></span></RouterLink><p class="muted">{{ store.description || '店铺暂未填写简介' }}</p><div class="actions"><RouterLink :to="`/stores/${store.store_id}`">进入店铺</RouterLink><button type="button" class="secondary small" :disabled="removing === store.store_id" @click="remove(store)">取消收藏</button></div></article></div>
    </PageState>
  </section>
</template>
