<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { adminCreate, adminGet, adminQuery, requireAdminToken, type AdminStore } from '@/api/admin-catalog'
import { ApiProblem, errorMessage, type ApiResult } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminStore[]>([])
const loading = ref(true)
const error = ref('')
const searchInput = ref('')
const appliedQuery = ref('')
const status = ref('')
const sortBy = ref<'default' | 'revenue' | 'sales' | 'products' | 'rating_desc' | 'rating_asc'>('default')
const createOpen = ref(false)
const saving = ref(false)
const formError = ref('')
const fieldErrors = ref<Record<string, string>>({})
const form = reactive({ store_name: '', description: '', merchant_username: '', merchant_password: '', merchant_email: '' })

const visible = computed(() => {
  const keyword = appliedQuery.value.toLocaleLowerCase('zh-CN')
  const filtered = items.value.filter((item) => {
    const matchesStatus = !status.value || item.status === status.value
    return matchesStatus && (!keyword || item.store_name.toLocaleLowerCase('zh-CN').includes(keyword))
  })
  return [...filtered].sort((left, right) => {
    if (sortBy.value === 'revenue') return revenueMinor(right) - revenueMinor(left)
    if (sortBy.value === 'sales') return right.sales_count - left.sales_count
    if (sortBy.value === 'products') return (right.product_count ?? 0) - (left.product_count ?? 0)
    if (sortBy.value === 'rating_desc') return Number(right.rating_score) - Number(left.rating_score)
    if (sortBy.value === 'rating_asc') return Number(left.rating_score) - Number(right.rating_score)
    return left.store_id.localeCompare(right.store_id)
  })
})
const activeCount = computed(() => items.value.filter((item) => item.status === 'active').length)
const pausedCount = computed(() => items.value.filter((item) => item.status === 'suspended').length)

function token(): string { return requireAdminToken(auth.accessToken) }
function statusLabel(value: string): string { return ({ active: '营业中', suspended: '已暂停' } as Record<string, string>)[value] ?? value }
function initials(value: string): string { return value.slice(0, 1).toUpperCase() }
function chooseStatus(value: '' | 'active' | 'suspended') { status.value = value }
function revenueMinor(item: AdminStore): number { return Number(item.net_revenue?.minor_units ?? 0) }
function revenueLabel(item: AdminStore): string { return `¥${(revenueMinor(item) / 100).toFixed(2)}` }
function applySearch() { appliedQuery.value = searchInput.value.trim() }

async function load() {
  loading.value = true
  error.value = ''
  try {
    const collected: AdminStore[] = []
    let cursor: string | null = null
    do {
      const response: ApiResult<{ items: AdminStore[]; next_cursor: string | null }> = await adminGet(
        `/admin/stores${adminQuery({ limit: 100, cursor })}`,
        token(),
      )
      collected.push(...response.data.items)
      cursor = response.data.next_cursor
    } while (cursor)
    items.value = collected
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function closeCreate() {
  createOpen.value = false
  formError.value = ''
  fieldErrors.value = {}
}

async function createStore() {
  saving.value = true
  formError.value = ''
  fieldErrors.value = {}
  try {
    await adminCreate('/admin/stores', { ...form, description: form.description || null }, token(), 'admin-store-create')
    Object.assign(form, { store_name: '', description: '', merchant_username: '', merchant_password: '', merchant_email: '' })
    closeCreate()
    await load()
  } catch (cause) {
    if (cause instanceof ApiProblem) {
      for (const item of cause.body.errors ?? []) fieldErrors.value[item.pointer.replace(/^\//, '')] = item.message
    }
    formError.value = errorMessage(cause)
  } finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack admin-store-list-page">
    <header class="admin-entity-hero admin-store-hero">
      <div><p class="eyebrow">店铺运营</p><h1>店铺与商家</h1><p>从建店、资料维护、商品运营到停业处理，在一个清晰入口完成。</p></div>
      <button v-if="auth.has('stores:manage')" @click="createOpen = true">＋ 创建店铺</button>
    </header>

    <section class="admin-operations-board">
      <div class="admin-entity-stats admin-store-status-filters admin-operations-status" aria-label="按经营状态筛选店铺">
        <button type="button" :class="{ active: status === '' }" :aria-pressed="status === ''" @click="chooseStatus('')"><span class="blue">店</span><div><small>全部</small><strong>{{ items.length }}</strong></div></button>
        <button type="button" :class="{ active: status === 'active' }" :aria-pressed="status === 'active'" @click="chooseStatus('active')"><span class="green">营</span><div><small>营业中</small><strong>{{ activeCount }}</strong></div></button>
        <button type="button" :class="{ active: status === 'suspended' }" :aria-pressed="status === 'suspended'" @click="chooseStatus('suspended')"><span class="red">停</span><div><small>已暂停</small><strong>{{ pausedCount }}</strong></div></button>
      </div>
      <header class="admin-list-toolbar admin-operations-toolbar">
        <form class="admin-store-search-form" role="search" @submit.prevent="applySearch"><label class="admin-inline-search"><span>⌕</span><input v-model="searchInput" aria-label="搜索店铺名称" placeholder="搜索店铺名称" /></label><button type="submit">搜索</button></form>
        <div class="admin-visible-sort" role="group" aria-label="店铺排序">
          <span>排序</span>
          <button type="button" :class="{ active: sortBy === 'default' }" :aria-pressed="sortBy === 'default'" @click="sortBy = 'default'">综合排序</button>
          <button type="button" :class="{ active: sortBy === 'revenue' }" :aria-pressed="sortBy === 'revenue'" @click="sortBy = 'revenue'">营业额</button>
          <button type="button" :class="{ active: sortBy === 'sales' }" :aria-pressed="sortBy === 'sales'" @click="sortBy = 'sales'">销量</button>
          <button type="button" :class="{ active: sortBy === 'products' }" :aria-pressed="sortBy === 'products'" @click="sortBy = 'products'">商品数量</button>
          <button type="button" :class="{ active: sortBy === 'rating_desc' }" :aria-pressed="sortBy === 'rating_desc'" @click="sortBy = 'rating_desc'">评价从高到低</button>
          <button type="button" :class="{ active: sortBy === 'rating_asc' }" :aria-pressed="sortBy === 'rating_asc'" @click="sortBy = 'rating_asc'">评价从低到高</button>
        </div>
        <small class="admin-filter-summary">{{ status ? statusLabel(status) : '全部店铺' }} · {{ visible.length }} 家</small>
      </header>
      <PageState :loading="loading" :error="error" :empty="!loading && !error && visible.length === 0" empty-title="没有匹配的店铺" @retry="load">
        <div class="admin-store-grid">
          <RouterLink v-for="item in visible" :key="item.store_id" class="admin-store-card" :to="`/admin/stores/${item.store_id}`">
            <div class="admin-store-cover"><img v-if="item.logo_url" :src="item.logo_url" alt="" /><span v-else>{{ initials(item.store_name) }}</span><i :class="item.status">{{ statusLabel(item.status) }}</i></div>
            <div class="admin-store-card-body"><h2>{{ item.store_name }}</h2><p>{{ item.description || '暂无店铺简介' }}</p><dl><div><dt>营业额</dt><dd>{{ revenueLabel(item) }}</dd></div><div><dt>销量</dt><dd>{{ item.sales_count }}</dd></div><div><dt>商品</dt><dd>{{ item.product_count ?? 0 }}</dd></div><div><dt>评分</dt><dd>{{ Number(item.rating_score).toFixed(1) }}</dd></div></dl><footer><small>{{ item.store_id }}</small><b>进入运营 →</b></footer></div>
          </RouterLink>
        </div>
      </PageState>
    </section>

    <div v-if="createOpen" class="admin-form-overlay" @click.self="closeCreate">
      <form class="admin-form-dialog" @submit.prevent="createStore">
        <header><div><p class="eyebrow">创建店铺</p><h2>创建店铺与商家账号</h2><p>一次创建独立商家身份和所属店铺；该账号不能登录用户端或超级管理端。</p></div><button type="button" @click="closeCreate">×</button></header>
        <p v-if="formError" class="alert error">{{ formError }}</p>
        <div class="admin-form-fields">
          <label>店铺名称<input v-model.trim="form.store_name" required minlength="2" maxlength="128" /><small v-if="fieldErrors.store_name" class="error-text">{{ fieldErrors.store_name }}</small></label>
          <label>商家登录名<input v-model.trim="form.merchant_username" required minlength="4" maxlength="32" pattern="[A-Za-z0-9_]+" /><small v-if="fieldErrors.merchant_username" class="error-text">{{ fieldErrors.merchant_username }}</small></label>
          <label>商家登录密码<input v-model="form.merchant_password" required type="password" autocomplete="new-password" placeholder="不能为空且不能包含空格" /></label>
          <label>商家邮箱<input v-model.trim="form.merchant_email" required type="email" /><small v-if="fieldErrors.merchant_email" class="error-text">{{ fieldErrors.merchant_email }}</small></label>
          <label class="wide">店铺简介（可选）<textarea v-model.trim="form.description" maxlength="2000" placeholder="向用户简洁介绍店铺特色" /></label>
        </div>
        <footer><button type="button" class="secondary" @click="closeCreate">取消</button><button :disabled="saving">{{ saving ? '正在创建…' : '创建并开通店铺' }}</button></footer>
      </form>
    </div>
  </section>
</template>
