<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import {
  getKnowledgeDocument,
  publishKnowledgeDocument,
  withdrawKnowledgeDocument,
  type KnowledgeDocument,
} from '@/api/admin-ai'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { confirmAction } from '@/composables/confirmation'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const route = useRoute()
const router = useRouter()
const documentId = String(route.params.documentId)
const item = ref<KnowledgeDocument | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')

function token() {
  if (!auth.accessToken) throw new Error('管理会话不可用')
  return auth.accessToken
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    item.value = (await getKnowledgeDocument(documentId, token())).data
  } catch (cause) {
    error.value = errorMessage(cause)
  } finally {
    loading.value = false
  }
}

async function publish() {
  if (!item.value || !await confirmAction('确认发布并创建影子索引任务吗？')) return
  error.value = ''
  try {
    item.value = (await publishKnowledgeDocument(documentId, token())).data
    notice.value = item.value.index_job_no
      ? `索引任务 ${item.value.index_job_no} 已创建。`
      : '索引任务已创建。'
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function withdraw() {
  if (!item.value || !await confirmAction('确认撤回文档并立即从检索范围删除吗？', { tone: 'danger' })) return
  error.value = ''
  try {
    item.value = (await withdrawKnowledgeDocument(documentId, token())).data
    notice.value = '文档已撤回。'
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

onMounted(load)
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading">
      <div>
        <p class="eyebrow">Knowledge Document</p>
        <h1>{{ item?.title ?? '知识文档详情' }}</h1>
        <p class="muted">文档编号 {{ documentId }}</p>
      </div>
      <RouterLink class="button-link secondary" to="/admin/knowledge/documents">返回列表</RouterLink>
    </header>
    <p v-if="notice" class="alert success">{{ notice }}</p>
    <PageState :loading="loading" :error="error" :empty="!loading && !item" empty-title="文档不存在或不在当前数据范围" @retry="load">
      <article v-if="item" class="card wide-editor">
        <dl class="detail-list">
          <dt>状态</dt><dd><span class="badge">{{ item.status }}</span></dd>
          <dt>数据范围</dt><dd>{{ item.scope_type }} / {{ item.scope_id }}</dd>
          <dt>内容版本</dt><dd>{{ item.content_version }}</dd>
          <dt>最近索引任务</dt><dd>{{ item.index_job_no ?? '-' }}</dd>
          <dt>索引状态</dt><dd>{{ item.index_status ?? '-' }}</dd>
        </dl>
        <p class="muted">正文不在详情响应中回传，避免管理端列表或浏览器缓存扩大知识内容暴露面。</p>
        <div class="actions">
          <button v-if="auth.has('knowledge:publish') && item.status !== 'withdrawn'" @click="publish">发布/重建索引</button>
          <button v-if="auth.has('knowledge:manage') && item.status !== 'withdrawn'" class="danger" @click="withdraw">撤回文档</button>
          <button v-if="item.index_job_no" class="secondary" @click="router.push(`/admin/knowledge/indexing-jobs/${encodeURIComponent(item.index_job_no!)}`)">查看索引任务</button>
        </div>
      </article>
    </PageState>
  </section>
</template>
