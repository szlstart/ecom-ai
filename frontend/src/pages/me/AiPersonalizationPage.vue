<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  changeAgentConsent,
  grantPersonalizationConsent,
  listAgentConsents,
  type AgentConsent,
} from '@/api/agent-runtime'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const items = ref<AgentConsent[]>([])
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
  try { items.value = (await listAgentConsents(token())).data.items }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
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
        </div>
      </section>
      <section class="card" aria-labelledby="memory-title">
        <h2 id="memory-title">我的长期记忆</h2>
        <p>记忆查看、更正、删除及清理任务接口正在纳入本次最终验收。在接口通过隐私、隔离与删除测试前，本页面不会伪造或展示本地模拟记忆。</p>
        <p class="muted">当前安全状态：长期记忆管理功能保持关闭；授权本身不代表已经写入任何记忆。</p>
      </section>
    </PageState>
  </main>
</template>
