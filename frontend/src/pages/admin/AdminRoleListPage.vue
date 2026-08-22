<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
interface Role { role_id: string; role_code: string; role_name: string; scope_type: string; role_type: string; status: string }
const auth = useAdminAuthStore(), roles = ref<Role[]>([]), error = ref(''), message = ref('')
const roleCode = ref(''), roleName = ref(''), scopeType = ref<'platform' | 'store'>('platform'), description = ref('')
async function load() { roles.value = (await apiRequest<Role[]>('/admin/roles', {}, auth.accessToken)).data }
onMounted(() => load().catch((reason) => { error.value = errorMessage(reason) }))
async function createRole() { error.value = ''; try { await apiRequest('/admin/roles', { method: 'POST', headers: { 'Idempotency-Key': createIdempotencyKey('role-create') }, body: JSON.stringify({ role_code: roleCode.value, role_name: roleName.value, scope_type: scopeType.value, description: description.value || null }) }, auth.accessToken); roleCode.value = ''; roleName.value = ''; description.value = ''; message.value = '自定义角色已创建。'; await load() } catch (reason) { error.value = errorMessage(reason) } }
</script>
<template><section><p class="eyebrow">用户与权限</p><h1>角色权限</h1><p v-if="message" class="alert success">{{ message }}</p><p v-if="error" class="alert error">{{ error }}</p><form v-if="auth.has('rbac:manage')" class="card" @submit.prevent="createRole"><h2>新建自定义角色</h2><div class="form-grid"><label>角色编码<input v-model="roleCode" pattern="[a-z][a-z0-9_]{2,63}" required /></label><label>角色名称<input v-model="roleName" minlength="2" required /></label><label>范围类型<select v-model="scopeType"><option value="platform">平台</option><option value="store">店铺</option></select></label></div><label>说明<textarea v-model="description" /></label><button>创建角色</button></form><div class="card-grid"><RouterLink v-for="role in roles" :key="role.role_id" class="card nav-card" :to="`/admin/roles/${role.role_id}`"><span class="badge">{{ role.scope_type }}</span><h2>{{ role.role_name }}</h2><p>{{ role.role_code }} · {{ role.role_type }} · {{ role.status }}</p></RouterLink></div></section></template>
