<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { createAdminUser, listAdminUsers, type AdminUserSummary } from '@/api/admin-users'
import { ApiProblem, errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminUserSummary[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const notice = ref('')
const searchInput = ref('')
const appliedQuery = ref('')
const status = ref('')
const sortBy = ref<'default' | 'registered_desc' | 'registered_asc' | 'login_desc' | 'username'>('default')
const createOpen = ref(false)
const fieldErrors = ref<Record<string, string>>({})
const form = reactive({ username: '', nickname: '', email: '', password: '' })

const filtered = computed(() => {
  const keyword = appliedQuery.value.toLocaleLowerCase()
  const matches = items.value.filter((item) => {
    const matchesKeyword = !keyword || `${item.username} ${item.nickname} ${item.user_id}`.toLocaleLowerCase().includes(keyword)
    return matchesKeyword && (!status.value || item.account_status === status.value)
  })
  return [...matches].sort((left, right) => {
    if (sortBy.value === 'registered_desc') return Date.parse(right.registered_at) - Date.parse(left.registered_at)
    if (sortBy.value === 'registered_asc') return Date.parse(left.registered_at) - Date.parse(right.registered_at)
    if (sortBy.value === 'login_desc') return loginTime(right) - loginTime(left)
    if (sortBy.value === 'username') return left.username.localeCompare(right.username, 'zh-CN')
    return left.user_id.localeCompare(right.user_id)
  })
})
const activeCount = computed(() => items.value.filter((item) => item.account_status === 'active').length)
const suspendedCount = computed(() => items.value.filter((item) => item.account_status === 'suspended').length)

function statusLabel(value: string): string { return ({ active: '正常', suspended: '已冻结', closed: '已注销' } as Record<string, string>)[value] ?? value }
function dateTime(value: string | null): string { return value ? new Intl.DateTimeFormat('zh-CN', { dateStyle: 'medium', timeStyle: 'short' }).format(new Date(value)) : '从未登录' }
function initials(item: AdminUserSummary): string { return (item.nickname || item.username).slice(0, 1).toUpperCase() }
function loginTime(item: AdminUserSummary): number { return item.last_login_at ? Date.parse(item.last_login_at) : Number.NEGATIVE_INFINITY }
function chooseStatus(value: '' | 'active' | 'suspended') { status.value = value }
function applySearch() { appliedQuery.value = searchInput.value.trim() }

async function load() {
  loading.value = true; error.value = ''
  try { items.value = (await listAdminUsers(auth.accessToken!)).data.items }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function resetForm() { Object.assign(form, { username: '', nickname: '', email: '', password: '' }); fieldErrors.value = {} }
function closeCreate() { createOpen.value = false; resetForm() }
async function createUser() {
  saving.value = true; error.value = ''; fieldErrors.value = {}
  try {
    await createAdminUser({ username: form.username, nickname: form.nickname || null, email: form.email, password: form.password }, auth.accessToken!)
    notice.value = `用户“${form.username}”已创建。`
    closeCreate(); await load()
  } catch (cause) {
    if (cause instanceof ApiProblem) for (const item of cause.body.errors ?? []) fieldErrors.value[item.pointer.replace('/', '')] = item.message
    error.value = errorMessage(cause)
  } finally { saving.value = false }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack admin-entity-page">
    <header class="admin-entity-hero"><div><p class="eyebrow">用户中心</p><h1>用户管理</h1><p>从账号资料、安全状态到交易关系，集中查看并执行受控操作。</p></div><button v-if="auth.has('users:manage')" @click="createOpen = true">＋ 创建用户</button></header>
    <p v-if="notice" class="alert success">{{ notice }}</p><p v-if="error && !createOpen" class="alert error">{{ error }}</p>
    <section class="admin-operations-board">
      <div class="admin-entity-stats admin-store-status-filters admin-operations-status" aria-label="用户状态筛选"><button type="button" :class="{ active: status === '' }" :aria-pressed="status === ''" @click="chooseStatus('')"><span class="blue">♙</span><div><small>全部用户</small><strong>{{ items.length }}</strong></div></button><button type="button" :class="{ active: status === 'active' }" :aria-pressed="status === 'active'" @click="chooseStatus('active')"><span class="green">✓</span><div><small>正常用户</small><strong>{{ activeCount }}</strong></div></button><button type="button" :class="{ active: status === 'suspended' }" :aria-pressed="status === 'suspended'" @click="chooseStatus('suspended')"><span class="red">!</span><div><small>已冻结</small><strong>{{ suspendedCount }}</strong></div></button></div>
      <header class="admin-list-toolbar admin-operations-toolbar">
        <form class="admin-store-search-form" role="search" @submit.prevent="applySearch"><label class="admin-inline-search"><span>⌕</span><input v-model="searchInput" aria-label="搜索用户" placeholder="搜索用户名、昵称或用户 ID" /></label><button type="submit">搜索</button></form>
        <div class="admin-visible-sort" role="group" aria-label="用户排序"><span>排序</span><button type="button" :class="{ active: sortBy === 'default' }" :aria-pressed="sortBy === 'default'" @click="sortBy = 'default'">综合排序</button><button type="button" :class="{ active: sortBy === 'registered_desc' }" :aria-pressed="sortBy === 'registered_desc'" @click="sortBy = 'registered_desc'">注册最新</button><button type="button" :class="{ active: sortBy === 'registered_asc' }" :aria-pressed="sortBy === 'registered_asc'" @click="sortBy = 'registered_asc'">注册最早</button><button type="button" :class="{ active: sortBy === 'login_desc' }" :aria-pressed="sortBy === 'login_desc'" @click="sortBy = 'login_desc'">最近登录</button><button type="button" :class="{ active: sortBy === 'username' }" :aria-pressed="sortBy === 'username'" @click="sortBy = 'username'">用户名</button></div>
        <small class="admin-filter-summary">{{ status ? statusLabel(status) : '全部用户' }} · {{ filtered.length }} 人</small>
      </header>
      <PageState :loading="loading" :error="error" :empty="!loading && !error && filtered.length === 0" empty-title="没有找到符合条件的用户" @retry="load">
        <div class="admin-modern-table"><div class="admin-modern-table-head"><span>用户</span><span>账号状态</span><span>注册时间</span><span>最近登录</span><span>操作</span></div><RouterLink v-for="item in filtered" :key="item.user_id" :to="`/admin/users/${item.user_id}`" class="admin-modern-row"><span class="admin-user-cell"><i>{{ initials(item) }}</i><span><strong>{{ item.nickname }}</strong><small>@{{ item.username }} · {{ item.user_id }}</small></span></span><span><b class="admin-status-dot" :class="item.account_status" />{{ statusLabel(item.account_status) }}</span><span>{{ dateTime(item.registered_at) }}</span><span>{{ dateTime(item.last_login_at) }}</span><span class="admin-row-action">管理用户 →</span></RouterLink></div>
      </PageState>
    </section>

    <div v-if="createOpen" class="admin-form-overlay" @click.self="closeCreate"><form class="admin-form-dialog" @submit.prevent="createUser"><header><div><p class="eyebrow">CREATE USER</p><h2>创建普通用户</h2><p>创建后用户可以立即登录；商家和管理员请使用各自独立入口。</p></div><button type="button" aria-label="关闭" @click="closeCreate">×</button></header><p v-if="error" class="alert error">{{ error }}</p><div class="admin-form-fields"><label>用户名<input v-model.trim="form.username" required minlength="4" maxlength="32" autocomplete="off" placeholder="4–32 位字母、数字或下划线" /><small v-if="fieldErrors.username" class="error-text">{{ fieldErrors.username }}</small></label><label>昵称（选填）<input v-model.trim="form.nickname" maxlength="64" placeholder="用户在商城展示的名称" /></label><label class="wide">找回邮箱<input v-model.trim="form.email" required type="email" autocomplete="off" placeholder="用于忘记密码时重置密码" /><small v-if="fieldErrors.email" class="error-text">{{ fieldErrors.email }}</small></label><label class="wide">初始密码<input v-model="form.password" required type="password" autocomplete="new-password" placeholder="不能为空且不能包含空格" /><small v-if="fieldErrors.password" class="error-text">{{ fieldErrors.password }}</small></label></div><footer><button type="button" class="secondary" @click="closeCreate">取消</button><button :disabled="saving">{{ saving ? '正在创建…' : '确认创建' }}</button></footer></form></div>
  </section>
</template>
