<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
interface Role { role_id: string; role_code: string; role_name: string; description: string | null; role_type: string; status: string; version: number }
const route = useRoute(), auth = useAdminAuthStore(), role = ref<Role | null>(null), etag = ref(''), permissions = ref('dashboard:read'), reason = ref(''), error = ref(''), message = ref('')
onMounted(async () => { try { const result = await apiRequest<Role>(`/admin/roles/${String(route.params.roleId)}`, {}, auth.accessToken); role.value = result.data; etag.value = result.headers.get('etag') ?? '' } catch (cause) { error.value = errorMessage(cause) } })
async function replacePermissions() { if (!role.value) return; try { const result = await apiRequest<Role>(`/admin/roles/${role.value.role_id}/permissions`, { method: 'PUT', headers: { 'If-Match': etag.value }, body: JSON.stringify({ permission_codes: permissions.value.split(/\s*,\s*/).filter(Boolean), reason: reason.value }) }, auth.accessToken); role.value = result.data; etag.value = result.headers.get('etag') ?? ''; message.value = '权限集合已更新，受影响会话已失效。' } catch (cause) { error.value = errorMessage(cause) } }
</script>
<template><section v-if="role"><p class="eyebrow">角色 · {{ role.role_code }}</p><h1>{{ role.role_name }}</h1><p v-if="message" class="alert success">{{ message }}</p><p v-if="error" class="alert error">{{ error }}</p><article class="card"><p>{{ role.description || '无描述' }}</p><p><span class="badge">{{ role.role_type }}</span> {{ role.status }}</p></article><form v-if="role.role_type === 'custom'" class="card" @submit.prevent="replacePermissions"><h2>完整替换 Permission</h2><label>权限码（逗号分隔）<textarea v-model="permissions" required /></label><label>变更原因<textarea v-model="reason" required /></label><button>确认替换</button></form><div v-else class="alert info">系统角色权限只能通过受控发布流程更新。</div></section></template>
