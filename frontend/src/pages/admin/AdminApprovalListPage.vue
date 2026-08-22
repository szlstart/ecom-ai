<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
interface Approval { approval_request_id: string; approval_type: string; action_code: string; target_type: string; target_id: string; approved_count: number; required_approval_count: number; status: string; expires_at: string }
const auth = useAdminAuthStore(), items = ref<Approval[]>([]), error = ref('')
onMounted(async () => { try { items.value = (await apiRequest<Approval[]>('/admin/approval-requests', {}, auth.accessToken)).data } catch (reason) { error.value = errorMessage(reason) } })
</script>
<template><section><p class="eyebrow">职责分离</p><h1>审批中心</h1><p v-if="error" class="alert error">{{ error }}</p><div class="table-wrap"><table><thead><tr><th>申请</th><th>目标</th><th>进度</th><th>状态</th><th></th></tr></thead><tbody><tr v-for="item in items" :key="item.approval_request_id"><td><strong>{{ item.approval_type }}</strong><small>{{ item.action_code }}</small></td><td>{{ item.target_type }} · {{ item.target_id }}</td><td>{{ item.approved_count }}/{{ item.required_approval_count }}</td><td><span class="badge">{{ item.status }}</span></td><td><RouterLink :to="`/admin/approval-requests/${item.approval_request_id}`">查看</RouterLink></td></tr></tbody></table></div></section></template>
