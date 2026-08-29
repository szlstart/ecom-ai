<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { changeKillSwitch, listKillSwitches, listMcpServers, type KillSwitch, type McpServerSummary } from '@/api/admin-ai'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { confirmAction, promptAction } from '@/composables/confirmation'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), servers = ref<McpServerSummary[]>([]), switches = ref<KillSwitch[]>([]), loading = ref(true), error = ref(''), notice = ref('')
const form = reactive({ target_type: 'mcp_server', target_code: '', reason: '' })
const token = () => auth.accessToken!
async function load() { loading.value = true; try { const [serverResult, switchResult] = await Promise.all([listMcpServers(token()), listKillSwitches(token())]); servers.value = serverResult.data.items; switches.value = switchResult.data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
async function change(active: boolean, item?: KillSwitch) {
  const targetType = item?.target_type ?? form.target_type
  const targetCode = item?.target_code ?? form.target_code
  const reason = item
    ? await promptAction('变更原因会写入审计记录。', { title: active ? '启用紧急阻断' : '解除紧急阻断', label: '变更原因', minLength: 3, maxLength: 500, tone: active ? 'danger' : 'default' })
    : form.reason
  if (!reason || reason.trim().length < 3 || !targetCode) return
  if (!await confirmAction(`${active ? '立即阻断' : '恢复'} ${targetType}/${targetCode}？`, { tone: active ? 'danger' : 'default' })) return
  try {
    await changeKillSwitch(targetType, targetCode, active, reason.trim(), token())
    notice.value = active ? '紧急阻断已立即生效。' : '紧急阻断已解除。'
    form.reason = ''
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
}
onMounted(load)
</script>
<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">Default Deny · Emergency Control</p><h1>AI 权限策略与 Kill Switch</h1><p class="muted">运行时以固定 Agent/Skill/Tool Version Binding 为准；未注册、未绑定、跨 Scope 或缺少确认的调用一律拒绝。</p></div></header><p v-if="error" class="alert error">{{ error }}</p><p v-if="notice" class="alert success">{{ notice }}</p><PageState :loading="loading" :error="''" :empty="false"><div class="admin-detail-grid"><article class="card"><h2>MCP Server 能力</h2><div v-for="server in servers" :key="server.server_code"><h3>{{ server.server_code }}</h3><p class="muted">默认超时 {{ server.timeout_seconds }} 秒</p><code>{{ server.tools.join('\n') }}</code></div></article><article class="card"><h2>当前 Kill Switch</h2><p v-if="switches.length === 0" class="muted">暂无已登记开关。</p><div v-for="item in switches" :key="item.switch_id" class="card-heading"><div><strong>{{ item.target_type }}/{{ item.target_code }}</strong><p>{{ item.reason }}</p></div><button v-if="auth.has('ai_runtime:kill')" :class="item.is_active ? 'secondary' : 'danger'" @click="change(!item.is_active, item)">{{ item.is_active ? '恢复' : '阻断' }}</button></div></article></div></PageState><form v-if="auth.has('ai_runtime:kill')" class="card admin-editor wide-editor" @submit.prevent="change(true)"><h2>紧急阻断</h2><div class="field-grid"><label>目标类型<select v-model="form.target_type"><option v-for="type in ['agent','skill','tool','mcp_server']" :key="type">{{ type }}</option></select></label><label>目标 Code<input v-model="form.target_code" required maxlength="128"></label></div><label>原因<textarea v-model="form.reason" required minlength="3" maxlength="500"></textarea></label><p class="alert warning">阻断会立即影响新调用；操作记录管理员、原因和审计链路。</p><button class="danger">立即阻断</button></form></section></template>
