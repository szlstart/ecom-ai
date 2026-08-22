<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { apiRequest, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
interface Log { operation_id: string; operator_user_id: string; permission_code: string; action: string; target_type: string; target_id: string; result_status: string; reason: string | null; created_at: string }
const auth = useAdminAuthStore(), items = ref<Log[]>([]), error = ref('')
onMounted(async () => { try { items.value = (await apiRequest<Log[]>('/admin/audit-logs?limit=100', {}, auth.accessToken)).data } catch (reason) { error.value = errorMessage(reason) } })
</script>
<template><section><p class="eyebrow">只读审计</p><h1>管理员操作日志</h1><p v-if="error" class="alert error">{{ error }}</p><div class="table-wrap"><table><thead><tr><th>时间</th><th>操作者</th><th>权限 / 动作</th><th>目标</th><th>结果</th></tr></thead><tbody><tr v-for="item in items" :key="item.operation_id"><td>{{ item.created_at }}</td><td>{{ item.operator_user_id }}</td><td><strong>{{ item.permission_code }}</strong><small>{{ item.action }}</small></td><td>{{ item.target_type }} · {{ item.target_id }}</td><td><span class="badge">{{ item.result_status }}</span><small>{{ item.reason }}</small></td></tr></tbody></table></div></section></template>
