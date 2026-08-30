<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { createTool, listTools, type ToolSummary } from '@/api/admin-ai'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), items = ref<ToolSummary[]>([]), loading = ref(true), error = ref(''), notice = ref('')
const definition = reactive({ tool_code: '', server_code: 'catalog-mcp', risk_level: 'read' })
const mcpServers = ['catalog-mcp', 'order-mcp', 'logistics-mcp', 'after-sale-mcp', 'support-mcp', 'memory-mcp', 'store-ops-mcp', 'governance-mcp', 'observability-mcp']
const token = () => auth.accessToken!
async function load() { loading.value = true; try { items.value = (await listTools(token())).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
async function addDefinition() { try { await createTool(definition, token()); notice.value = 'Tool Definition 已创建。'; definition.tool_code = ''; await load() } catch (cause) { error.value = errorMessage(cause) } }
onMounted(load)
</script>
<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">MCP 契约</p><h1>MCP Tool 管理</h1><p class="muted">Tool Definition 不保存 Schema；输入输出 Schema、评估报告和发布状态属于不可变 Version。</p></div></header><p v-if="error" class="alert error">{{ error }}</p><p v-if="notice" class="alert success">{{ notice }}</p><div class="admin-split"><PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="暂无 Tool"><div class="card-list"><article v-for="item in items" :key="item.tool_code" class="card"><div class="card-heading"><div><h2><RouterLink :to="`/admin/ai/tools/${encodeURIComponent(item.tool_code)}`">{{ item.tool_code }}</RouterLink></h2><p>{{ item.server_code }} · {{ item.risk_level }}</p></div><span class="badge">{{ item.status }}</span></div><p>最新 v{{ item.latest_version ?? '-' }} · 已发布 v{{ item.published_version ?? '-' }}</p><RouterLink class="button-link secondary" :to="`/admin/ai/tools/${encodeURIComponent(item.tool_code)}`">查看版本与发布控制</RouterLink></article></div></PageState><form v-if="auth.has('ai_tools:manage')" class="card admin-editor sticky-editor" @submit.prevent="addDefinition"><h2>新建 Definition</h2><label>Tool Code<input v-model="definition.tool_code" required></label><label>MCP Server<select v-model="definition.server_code"><option v-for="code in mcpServers" :key="code">{{ code }}</option></select></label><label>风险<select v-model="definition.risk_level"><option v-for="risk in ['read','low','medium','high']" :key="risk">{{ risk }}</option></select></label><button>创建</button></form></div></section></template>
