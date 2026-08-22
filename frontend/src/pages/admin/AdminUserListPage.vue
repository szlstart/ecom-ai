<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
interface User { user_id: string; username: string; nickname: string; account_status: string; registered_at: string; last_login_at: string | null }
const auth = useAdminAuthStore(), items = ref<User[]>([]), error = ref(''), pending = ref(false)
async function load() { pending.value = true; try { items.value = (await apiRequest<{ items: User[] }>('/admin/users?limit=50', {}, auth.accessToken)).data.items } catch (reason) { error.value = errorMessage(reason) } finally { pending.value = false } }
onMounted(load)
</script>
<template><section><div class="page-heading"><div><p class="eyebrow">用户与权限</p><h1>用户治理</h1></div><button class="secondary" @click="load">刷新</button></div><p v-if="error" class="alert error">{{ error }}</p><div class="table-wrap"><table><thead><tr><th>用户</th><th>状态</th><th>注册时间</th><th>最近登录</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.user_id"><td><strong>{{ item.nickname }}</strong><small>{{ item.username }} · {{ item.user_id }}</small></td><td><span class="badge">{{ item.account_status }}</span></td><td>{{ item.registered_at }}</td><td>{{ item.last_login_at || '—' }}</td><td><RouterLink :to="`/admin/users/${item.user_id}`">查看详情</RouterLink></td></tr></tbody></table><p v-if="!pending && !items.length" class="empty-state">暂无数据</p></div></section></template>
