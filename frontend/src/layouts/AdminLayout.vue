<script setup lang="ts">
import { RouterLink, RouterView, useRouter } from 'vue-router'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), router = useRouter()
async function logout() { await auth.logout(); await router.replace('/admin/login') }
</script>

<template>
  <div class="admin-shell">
    <aside class="admin-sidebar">
      <strong>Ecom AI 管理端</strong>
      <nav aria-label="管理导航"><RouterLink to="/admin/dashboard">仪表盘</RouterLink><RouterLink v-if="auth.has('users:read')" to="/admin/users">用户治理</RouterLink><RouterLink v-if="auth.has('rbac:read')" to="/admin/roles">角色权限</RouterLink><RouterLink v-if="auth.has('admin_approvals:read')" to="/admin/approval-requests">审批中心</RouterLink><RouterLink v-if="auth.has('audit:read')" to="/admin/audit-logs">审计日志</RouterLink><RouterLink to="/admin/security">安全会话</RouterLink></nav><button class="secondary" @click="logout">退出管理端</button>
    </aside>
    <main class="admin-content"><RouterView /></main>
  </div>
</template>
