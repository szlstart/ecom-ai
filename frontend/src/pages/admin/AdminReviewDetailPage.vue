<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { getAdminReview, moderateAdminReview, replyAdminReview, type AdminReview, type ReviewGovernanceAction } from '@/api/admin-reviews'
import { errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
const route = useRoute(), auth = useAdminAuthStore(), item = ref<AdminReview | null>(null), etag = ref(''), reply = ref(''), ruleCode = ref('CONTENT_POLICY'), reason = ref(''), error = ref(''), busy = ref(false)
async function load() { const response = await getAdminReview(String(route.params.reviewId), auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' }
onMounted(() => load().catch((cause) => { error.value = errorMessage(cause) }))
async function submitReply() { if (!reply.value.trim()) return; busy.value = true; try { const response = await replyAdminReview(item.value!.review_id, etag.value, reply.value.trim(), auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
async function moderate(action: ReviewGovernanceAction) { if (!reason.value.trim()) return; busy.value = true; try { const response = await moderateAdminReview(item.value!.review_id, etag.value, action, ruleCode.value, reason.value.trim(), auth.accessToken!); item.value = response.data; etag.value = response.headers.get('etag') ?? '' } catch (cause) { error.value = errorMessage(cause) } finally { busy.value = false } }
</script>
<template>
  <section v-if="item">
    <p class="eyebrow">评价 · {{ item.review_id }}</p>
    <h1>{{ item.product_name }}</h1>
    <p v-if="error" class="alert error">{{ error }}</p>
    <div class="settings-grid">
      <article class="card">
        <p>{{ item.rating }} 星 · {{ item.review_status }}</p>
        <p>{{ item.content || '用户未填写文字评价' }}</p>
        <small>订单 {{ item.order_id }} · 用户 {{ item.user_id }}（公开匿名：{{ item.is_anonymous ? '是' : '否' }}）</small>
      </article>
      <article v-if="item.append" class="card">
        <h2>用户追评</h2>
        <p>{{ item.append.content }}</p>
        <small>{{ item.append.append_status }} · {{ item.append.moderation_status }}</small>
      </article>
      <article class="card">
        <h2>商家回复</h2>
        <p v-if="item.merchant_reply">{{ item.merchant_reply.content }}</p>
        <form v-else-if="item.review_status === 'published' || item.review_status === 'hidden'" @submit.prevent="submitReply">
          <textarea v-model="reply" required minlength="2" maxlength="500" />
          <button :disabled="busy || !reply.trim()">发布回复</button>
        </form>
        <p v-else>评价通过审核后才可发布商家回复。</p>
      </article>
    </div>
    <form v-if="item.review_status === 'published' || item.review_status === 'hidden'" class="card" @submit.prevent>
      <h2>{{ item.review_status === 'published' ? '屏蔽评价' : '恢复评价' }}</h2>
      <label>规则码<input v-model="ruleCode" required pattern="[A-Z][A-Z0-9_]+" /></label>
      <label>治理理由<textarea v-model="reason" required minlength="2" maxlength="1000" /></label>
      <button :class="{ danger: item.review_status === 'published' }" :disabled="busy || !reason.trim()" @click="moderate(item.review_status === 'published' ? 'hide' : 'restore')">
        {{ item.review_status === 'published' ? '确认屏蔽' : '确认恢复' }}
      </button>
    </form>
    <form v-if="item.append?.append_status === 'pending' && item.append.moderation_status === 'manual'" class="card" @submit.prevent>
      <h2>追评人工审核</h2>
      <p>这条追评包含联系方式、链接或需要人工判断的内容。</p>
      <label>规则码<input v-model="ruleCode" required pattern="[A-Z][A-Z0-9_]+" /></label>
      <label>审核说明<textarea v-model="reason" required minlength="2" maxlength="1000" /></label>
      <div class="actions">
        <button :disabled="busy || !reason.trim()" @click="moderate('approve_append')">审核通过</button>
        <button class="danger" :disabled="busy || !reason.trim()" @click="moderate('reject_append')">拒绝发布</button>
      </div>
    </form>
    <form v-else-if="item.review_status === 'pending' && item.moderation_status === 'manual'" class="card" @submit.prevent>
      <h2>人工审核</h2>
      <p>自动规则无法可靠判断这条内容，请人工决定是否公开。</p>
      <label>规则码<input v-model="ruleCode" required pattern="[A-Z][A-Z0-9_]+" /></label>
      <label>审核说明<textarea v-model="reason" required minlength="2" maxlength="1000" /></label>
      <div class="actions">
        <button :disabled="busy || !reason.trim()" @click="moderate('approve')">审核通过</button>
        <button class="danger" :disabled="busy || !reason.trim()" @click="moderate('reject')">拒绝发布</button>
      </div>
    </form>
    <article class="card">
      <h2>不可变治理记录</h2>
      <ol class="timeline">
        <li v-for="record in item.governance_history" :key="record.governance_id">
          <strong>{{ record.action }} · {{ record.rule_code }}</strong>
          <p>{{ record.reason }}</p>
          <time>{{ new Date(record.occurred_at).toLocaleString('zh-CN') }}</time>
        </li>
      </ol>
      <p v-if="!item.governance_history.length">暂无治理记录。</p>
    </article>
  </section>
  <p v-else-if="!error">正在加载…</p>
</template>
