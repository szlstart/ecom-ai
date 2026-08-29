<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  activateAiMemory,
  changeAgentConsent,
  deleteAiMemory,
  disableAllAiPersonalization,
  getAiCleanupTask,
  grantPersonalizationConsent,
  listAiMemories,
  listAgentConsents,
  retryAiCleanupTask,
  reviseAiMemory,
  type AiCleanupTask,
  type AiMemoryItem,
  type AgentConsent,
} from '@/api/agent-runtime'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const items = ref<AgentConsent[]>([])
const memories = ref<AiMemoryItem[]>([])
const cleanupTasks = ref<AiCleanupTask[]>([])
const memoryBusy = ref<string | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const personalization = computed(() => items.value.find((item) => item.consent_type === 'personalization'))

function token(): string {
  if (!auth.accessToken) throw new Error('登录状态不可用')
  return auth.accessToken
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [consents, memoryItems] = await Promise.all([
      listAgentConsents(token()),
      listAiMemories(token()),
    ])
    items.value = consents.data.items
    memories.value = memoryItems.data.items
  }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function upsertTask(task: AiCleanupTask) {
  cleanupTasks.value = [task, ...cleanupTasks.value.filter((item) => item.cleanup_task_id !== task.cleanup_task_id)]
}
async function reviseMemory(item: AiMemoryItem) {
  const value = window.prompt('请输入更正后的低敏偏好。密码、证件、支付信息和完整地址禁止写入。', item.value)
  if (value === null || !value.trim() || value.trim() === item.value || memoryBusy.value) return
  if (!window.confirm('确认以新版本替换这条记忆吗？旧版本将保留最小审计链但不再召回。')) return
  memoryBusy.value = item.memory_id; error.value = ''; notice.value = ''
  try {
    const revised = (await reviseAiMemory(item.memory_id, item.version, value.trim(), token())).data
    memories.value = [revised, ...memories.value.filter((candidate) => candidate.memory_id !== item.memory_id)]
    notice.value = '记忆已更正并生成新版本。'
  } catch (cause) { error.value = errorMessage(cause); await load() }
  finally { memoryBusy.value = null }
}
async function activateMemory(item: AiMemoryItem) {
  if (memoryBusy.value) return
  memoryBusy.value = item.memory_id; error.value = ''; notice.value = ''
  try {
    const active = (await activateAiMemory(item.memory_id, item.version, token())).data
    memories.value = memories.value.map((candidate) => candidate.memory_id === item.memory_id ? active : candidate)
    notice.value = '候选记忆已确认生效。'
  } catch (cause) { error.value = errorMessage(cause); await load() }
  finally { memoryBusy.value = null }
}
async function removeMemory(item: AiMemoryItem) {
  if (memoryBusy.value || !window.confirm('确认删除这条长期记忆吗？它会立即停止召回，派生向量和缓存进入可追踪清理任务。')) return
  memoryBusy.value = item.memory_id; error.value = ''; notice.value = ''
  try {
    const task = (await deleteAiMemory(item.memory_id, item.version, token())).data
    memories.value = memories.value.filter((candidate) => candidate.memory_id !== item.memory_id)
    upsertTask(task)
    notice.value = '该记忆已停止使用，派生数据正在清理。'
  } catch (cause) { error.value = errorMessage(cause); await load() }
  finally { memoryBusy.value = null }
}
async function disableAll() {
  if (memoryBusy.value || !window.confirm('确认关闭全部 AI 个性化吗？所有当前授权会立即撤销，长期记忆停止召回并进入清理流程。')) return
  memoryBusy.value = 'disable-all'; error.value = ''; notice.value = ''
  try {
    const task = (await disableAllAiPersonalization(token())).data
    upsertTask(task)
    notice.value = '全部 AI 个性化已关闭，清理任务已创建。'
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { memoryBusy.value = null }
}
async function refreshTask(task: AiCleanupTask) {
  memoryBusy.value = task.cleanup_task_id; error.value = ''
  try { upsertTask((await getAiCleanupTask(task.cleanup_task_id, token())).data) }
  catch (cause) { error.value = errorMessage(cause) }
  finally { memoryBusy.value = null }
}
async function retryTask(task: AiCleanupTask) {
  memoryBusy.value = task.cleanup_task_id; error.value = ''
  try { upsertTask((await retryAiCleanupTask(task.cleanup_task_id, task.version, token())).data) }
  catch (cause) { error.value = errorMessage(cause); await refreshTask(task) }
  finally { memoryBusy.value = null }
}

async function enable() {
  error.value = ''; notice.value = ''
  try {
    await grantPersonalizationConsent(token())
    notice.value = 'AI 个性化授权已开启。只有允许的低敏偏好才可进入长期记忆。'
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
}

async function command(commandName: 'pauses' | 'resumes' | 'revocations') {
  const item = personalization.value
  if (!item) return
  if (commandName === 'revocations' && !window.confirm('确认撤销个性化授权吗？撤销会立即停止新记忆写入，已有记忆将进入清理流程。')) return
  error.value = ''; notice.value = ''
  try {
    await changeAgentConsent(item.consent_id, commandName, token())
    notice.value = commandName === 'pauses' ? '个性化已暂停。' : commandName === 'resumes' ? '个性化已恢复。' : '授权已撤销。'
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
}

onMounted(load)
</script>

<template>
  <main class="page-stack" aria-labelledby="ai-personalization-title">
    <header class="page-heading">
      <div>
        <p class="eyebrow">隐私中心</p>
        <h1 id="ai-personalization-title">AI 个性化与记忆</h1>
        <p class="muted">你可以随时暂停或撤销授权。订单、密码、证件、支付信息和完整地址不会写入长期记忆。</p>
      </div>
    </header>
    <p v-if="error" role="alert" class="alert error">{{ error }}</p>
    <p v-if="notice" role="status" class="alert success">{{ notice }}</p>
    <PageState :loading="loading" :error="''" :empty="false" @retry="load">
      <section class="card admin-editor" aria-labelledby="personalization-consent-title">
        <div class="card-heading">
          <div>
            <h2 id="personalization-consent-title">个性化授权</h2>
            <p>允许专属客服根据你明确表达的低敏偏好改进搜索、推荐和解释。</p>
          </div>
          <span class="badge">{{ personalization?.status ?? '未授权' }}</span>
        </div>
        <dl v-if="personalization" class="detail-list">
          <div><dt>告知版本</dt><dd>{{ personalization.policy_version }}</dd></div>
          <div><dt>授权时间</dt><dd>{{ personalization.created_at }}</dd></div>
          <div><dt>作用范围</dt><dd>{{ personalization.scope_type }}</dd></div>
        </dl>
        <div class="actions">
          <button v-if="!personalization || personalization.status === 'revoked'" type="button" @click="enable">开启个性化</button>
          <button v-if="personalization?.status === 'active'" class="secondary" type="button" @click="command('pauses')">暂停</button>
          <button v-if="personalization?.status === 'paused'" type="button" @click="command('resumes')">恢复</button>
          <button v-if="personalization && personalization.status !== 'revoked'" class="danger" type="button" @click="command('revocations')">撤销授权</button>
          <button v-if="personalization && personalization.status !== 'revoked'" class="danger" type="button" :disabled="memoryBusy === 'disable-all'" @click="disableAll">关闭全部个性化并清理</button>
        </div>
      </section>
      <section class="card" aria-labelledby="memory-title">
        <h2 id="memory-title">我的长期记忆</h2>
        <p class="muted">这里只展示可由你管理的加密低敏偏好，不展示向量、内部评分或底层存储键。</p>
        <p v-if="!memories.length">当前没有可管理的长期记忆。授权本身不代表已经写入任何记忆。</p>
        <article v-for="memory in memories" :key="memory.memory_id" class="admin-list-row">
          <div><strong>{{ memory.value }}</strong><p>{{ memory.memory_type }} · {{ memory.namespace === 'store' ? `店铺 ${memory.store_id}` : '专属客服' }} · {{ memory.status }}</p><small>来源：{{ memory.source_type }} · 更新于 {{ memory.updated_at }}</small></div>
          <div v-if="['active','candidate'].includes(memory.status)" class="actions"><button v-if="memory.status === 'candidate'" type="button" class="small" :disabled="memoryBusy === memory.memory_id" @click="activateMemory(memory)">确认记住</button><button type="button" class="small secondary" :disabled="memoryBusy === memory.memory_id" @click="reviseMemory(memory)">更正</button><button type="button" class="small danger" :disabled="memoryBusy === memory.memory_id" @click="removeMemory(memory)">删除</button></div>
        </article>
      </section>
      <section v-if="cleanupTasks.length" class="card" aria-labelledby="cleanup-title">
        <h2 id="cleanup-title">清理任务</h2>
        <article v-for="task in cleanupTasks" :key="task.cleanup_task_id" class="admin-list-row">
          <div><strong>{{ task.cleanup_task_id }} · {{ task.status }}</strong><p>进度 {{ task.processed_count }}/{{ task.total_count }}，失败 {{ task.failed_count }}；清理失败不会恢复已撤销授权或已删除记忆。</p><small v-if="task.error_code">原因码：{{ task.error_code }}</small></div>
          <div class="actions"><button type="button" class="small secondary" :disabled="memoryBusy === task.cleanup_task_id" @click="refreshTask(task)">刷新</button><button v-if="task.can_retry" type="button" class="small" :disabled="memoryBusy === task.cleanup_task_id" @click="retryTask(task)">重试失败项</button></div>
        </article>
      </section>
    </PageState>
  </main>
</template>
