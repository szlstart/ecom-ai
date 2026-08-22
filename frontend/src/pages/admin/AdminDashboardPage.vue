<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { apiRequest, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore()
const scopeText = computed(() => auth.scopes.some((item) => item.scope_type === 'platform') ? '全平台' : `${auth.scopes.length} 个店铺范围`)
interface Summary { generated_at: string; active_user_count: number | null; pending_approval_count: number; unavailable_sections: string[] }
const summary = ref<Summary | null>(null), error = ref('')
onMounted(async () => { try { summary.value = (await apiRequest<Summary>('/admin/dashboard', {}, auth.accessToken)).data } catch (reason) { error.value = errorMessage(reason) } })
</script>
<template><section><div class="page-heading"><div><p class="eyebrow">Admin Audience · {{ scopeText }}</p><h1>管理仪表盘</h1></div></div><p v-if="error" class="alert error">{{ error }}</p><div class="alert info">交易、商品与 AI 指标将在对应业务阶段接入；权限与范围投影已实时生效。</div><div v-if="summary" class="card-grid"><article class="card"><small>有效用户</small><h2>{{ summary.active_user_count ?? '仅平台范围可见' }}</h2></article><article class="card"><small>待处理审批</small><h2>{{ summary.pending_approval_count }}</h2></article></div><div class="card-grid"><RouterLink v-if="auth.has('users:read')" class="card nav-card" to="/admin/users"><h2>用户治理</h2><p>冻结、强制下线、密码重置和敏感字段授权。</p></RouterLink><RouterLink v-if="auth.has('rbac:read')" class="card nav-card" to="/admin/roles"><h2>角色权限</h2><p>查看角色、Permission 与范围 Grant。</p></RouterLink><RouterLink v-if="auth.has('admin_approvals:read')" class="card nav-card" to="/admin/approval-requests"><h2>审批中心</h2><p>处理职责分离的高风险操作。</p></RouterLink><RouterLink v-if="auth.has('audit:read')" class="card nav-card" to="/admin/audit-logs"><h2>审计日志</h2><p>查看不可变的管理操作记录。</p></RouterLink></div></section></template>
