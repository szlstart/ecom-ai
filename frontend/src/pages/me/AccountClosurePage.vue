<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'

import { apiRequest, errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'

const auth = useUserAuthStore()
const router = useRouter()
const confirmation = ref('')
const pending = ref(false)
const error = ref('')

async function submit() {
  if (!confirm('确认永久删除账号吗？该操作没有冷静期，也无法恢复。')) return
  pending.value = true
  error.value = ''
  try {
    await apiRequest('/users/me', { method: 'DELETE', body: JSON.stringify({ confirmation: confirmation.value }) }, auth.accessToken)
    auth.clear()
    await router.replace('/?accountDeletionRequested=1')
  } catch (cause) { error.value = errorMessage(cause) }
  finally { pending.value = false }
}
</script>

<template>
  <section class="narrow-page"><p class="eyebrow danger-text">不可恢复</p><h1>账号注销</h1><div class="alert warning"><strong>提交后会立即退出并禁止再次登录。</strong><p>系统会在后台按清理清单删除收货地址、收藏、余额、AI 记忆和非交易文件；中途失败会安全重试，不会出现无法追踪的“半删除”。存在历史订单时，为保护双方交易记录，系统会阻止物理删除并提示联系平台客服。</p></div><p v-if="error" class="alert error" role="alert">{{ error }}</p><form class="card" @submit.prevent="submit"><label>输入 DELETE_MY_ACCOUNT 以确认<input v-model="confirmation" required autocomplete="off" /></label><button class="danger" :disabled="pending || confirmation !== 'DELETE_MY_ACCOUNT'">{{ pending ? '正在提交注销任务…' : '确认注销账号' }}</button></form></section>
</template>
