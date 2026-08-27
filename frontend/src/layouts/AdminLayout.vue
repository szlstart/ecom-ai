<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRouter } from 'vue-router'
import { apiRequest } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), router = useRouter()
const navigation = ref<Array<{ code: string; title: string; route: string; required_permission: string }>>([])
async function loadNavigation() { if (!auth.accessToken) return; try { navigation.value = (await apiRequest<{ items: typeof navigation.value }>('/admin/navigation', {}, auth.accessToken)).data.items } catch { navigation.value = [] } }
async function logout() { await auth.logout('admin'); await router.replace('/admin/login') }
onMounted(loadNavigation)
watch(() => auth.permissions.join(','), loadNavigation)
</script>

<template>
  <div class="admin-shell">
    <aside class="admin-sidebar">
      <strong>Ecom AI 管理端</strong>
      <nav aria-label="管理导航"><span class="nav-section">授权菜单</span><RouterLink v-for="item in navigation" :key="item.code" :to="item.route">{{ item.title }}</RouterLink><span class="nav-section">身份安全</span><RouterLink to="/admin/security">安全会话</RouterLink></nav><button class="secondary" @click="logout">退出管理端</button>
    </aside>
    <main class="admin-content"><RouterView /></main>
  </div>
</template>
