<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'

import { ApiProblem, apiRequest, createIdempotencyKey, errorMessage, resolveApiAssetUrl } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import { useUserAuthStore } from '@/stores/user-auth'
import { imageFileFromClipboard } from '@/utils/clipboard-image'

interface Security {
  password_set: boolean
  password_changed_at: string | null
  current_email: string | null
  bound_accounts: Array<{ type: string; masked: string }>
  active_session_count: number
}
interface Session {
  session_id: string
  device_name: string | null
  last_seen_at: string
  expires_at: string
  is_current: boolean
}
interface Profile {
  user_id: string
  username: string
  nickname: string
  avatar_url: string | null
  locale: string
  timezone: string
  bound_accounts: Array<{ type: string; masked: string }>
  version: number
}
interface FileUploadHandle { uploadFile: (file: File) => Promise<void> }

const auth = useUserAuthStore()
const security = ref<Security | null>(null)
const sessions = ref<Session[]>([])
const error = ref('')
const emailError = ref('')
const message = ref('')
const currentPassword = ref('')
const newPassword = ref('')
const newEmail = ref('')
const profile = ref<Profile | null>(null)
const profileEtag = ref('')
const avatarFileId = ref<string | null>(null)
const pendingAvatarUrl = ref<string | null>(null)
const avatarUpload = ref<FileUploadHandle | null>(null)
const avatarPasteBusy = ref(false)
const avatarPasteFocused = ref(false)
const avatarSaving = ref(false)
const avatarUrl = computed(() => pendingAvatarUrl.value || resolveApiAssetUrl(profile.value?.avatar_url ?? null) || null)
const avatarLabel = computed(() => (profile.value?.username || '用').slice(0, 1).toUpperCase())

async function load() {
  const [securityResult, sessionResult, profileResult] = await Promise.all([
    apiRequest<Security>('/users/me/security', {}, auth.accessToken),
    apiRequest<Session[]>('/auth/sessions', {}, auth.accessToken),
    apiRequest<Profile>('/users/me', {}, auth.accessToken),
  ])
  security.value = securityResult.data
  sessions.value = sessionResult.data
  profile.value = profileResult.data
  profileEtag.value = profileResult.headers.get('etag') ?? ''
}

function avatarUploaded(fileId: string) {
  avatarFileId.value = fileId
  pendingAvatarUrl.value = resolveApiAssetUrl(`/api/v1/files/${fileId}`)
  message.value = '新头像已上传，点击“保存头像”后生效。'
  error.value = ''
}

async function pasteAvatar(event: ClipboardEvent) {
  if (avatarPasteBusy.value) return
  error.value = ''
  try {
    const file = imageFileFromClipboard(event.clipboardData)
    if (!file) throw new Error('剪贴板中没有图片。请先复制 JPG、PNG 或 WebP 图片。')
    event.preventDefault()
    if (!avatarUpload.value) throw new Error('头像上传组件尚未准备好，请稍后重试。')
    avatarPasteBusy.value = true
    await avatarUpload.value.uploadFile(file)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : errorMessage(cause)
  } finally { avatarPasteBusy.value = false }
}

async function saveAvatar() {
  if (!profile.value || !avatarFileId.value) return
  avatarSaving.value = true; error.value = ''; message.value = ''
  try {
    const result = await apiRequest<Profile>('/users/me', {
      method: 'PATCH',
      headers: { 'If-Match': profileEtag.value },
      body: JSON.stringify({ avatar_file_id: avatarFileId.value }),
    }, auth.accessToken)
    profile.value = result.data
    profileEtag.value = result.headers.get('etag') ?? ''
    avatarFileId.value = null
    pendingAvatarUrl.value = null
    auth.updateUser({ nickname: result.data.nickname, avatar_url: result.data.avatar_url })
    message.value = '头像已更新。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { avatarSaving.value = false }
}

onMounted(() => load().catch((reason) => { error.value = errorMessage(reason) }))

async function changePassword() {
  error.value = ''
  message.value = ''
  if (!newPassword.value || /\s/u.test(newPassword.value)) {
    error.value = '新密码不能为空，也不能包含空格、换行或其他空白字符。'
    return
  }
  try {
    await apiRequest('/users/me/password', {
      method: 'PUT',
      headers: { 'Idempotency-Key': createIdempotencyKey('password-change') },
      body: JSON.stringify({ current_password: currentPassword.value, new_password: newPassword.value }),
    }, auth.accessToken)
    currentPassword.value = ''
    newPassword.value = ''
    message.value = '密码已修改。'
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}

async function changeEmail() {
  error.value = ''
  emailError.value = ''
  message.value = ''
  try {
    await apiRequest('/users/me/contact-changes', {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey('email-change') },
      body: JSON.stringify({ new_email: newEmail.value }),
    }, auth.accessToken)
    newEmail.value = ''
    message.value = '邮箱已更新。'
    await load()
  } catch (reason) {
    if (reason instanceof ApiProblem) {
      emailError.value = reason.body.errors?.find((item) => item.pointer === '/new_email')?.message ?? ''
    }
    if (!emailError.value) error.value = errorMessage(reason)
  }
}

async function revoke(sessionId: string) {
  error.value = ''
  try {
    await apiRequest(`/auth/sessions/${sessionId}`, { method: 'DELETE' }, auth.accessToken)
    await load()
  } catch (reason) {
    error.value = errorMessage(reason)
  }
}
</script>

<template>
  <section class="settings-page">
    <p class="eyebrow">我的</p>
    <h1>账号安全</h1>
    <p v-if="message" class="alert success" role="status">{{ message }}</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>

    <article v-if="profile" class="card profile-settings-card security-avatar-card">
      <section class="profile-avatar-editor">
        <div class="profile-avatar-preview"><img v-if="avatarUrl" :src="avatarUrl" alt="当前用户头像" /><span v-else>{{ avatarLabel }}</span></div>
        <div class="profile-avatar-copy"><p class="eyebrow">个人头像</p><h2>{{ profile.username }}</h2><p>头像会显示在消息气泡、个人中心和账号入口。支持本地选择，也支持 Command + V 或 Ctrl + V 粘贴图片。</p></div>
      </section>
      <div class="profile-avatar-paste-zone" :class="{ focused: avatarPasteFocused, busy: avatarPasteBusy }" tabindex="0" role="button" aria-label="用户头像粘贴上传区" @focus="avatarPasteFocused = true" @blur="avatarPasteFocused = false" @paste="pasteAvatar">
        <strong>{{ avatarPasteBusy ? '正在读取、扫描并上传头像…' : '上传或粘贴新头像' }}</strong>
        <AdminFileUpload ref="avatarUpload" purpose="user_avatar" :access-token="auth.accessToken" label="从本地选择头像" :disabled="avatarSaving" @uploaded="avatarUploaded" />
      </div>
      <button type="button" :disabled="!avatarFileId || avatarSaving || avatarPasteBusy" @click="saveAvatar">{{ avatarSaving ? '正在保存…' : '保存头像' }}</button>
    </article>

    <div class="settings-grid">
      <form class="card" @submit.prevent="changePassword">
        <h2>修改密码</h2>
        <label>当前密码<input v-model="currentPassword" autocomplete="current-password" type="password" required /></label>
        <label>新密码<input v-model="newPassword" autocomplete="new-password" type="password" required /><small>密码不能为空，且不能包含空格、换行或其他空白字符；长度不限。</small></label>
        <button>修改密码</button>
      </form>

      <form class="card" @submit.prevent="changeEmail">
        <h2>更换邮箱</h2>
        <label>当前邮箱<output class="readonly-field">{{ security?.current_email || '尚未设置' }}</output></label>
        <label>新的邮箱<input v-model.trim="newEmail" autocomplete="email" type="email" required /><small v-if="emailError" class="field-error" role="alert">{{ emailError }}</small></label>
        <button>确认换绑</button>
      </form>
    </div>

    <article class="card">
      <div class="page-heading"><h2>登录设备（{{ security?.active_session_count ?? 0 }}）</h2></div>
      <ul class="session-list">
        <li v-for="item in sessions" :key="item.session_id">
          <div><strong>{{ item.device_name || '未知设备' }}</strong><small>{{ item.is_current ? '当前会话' : `最近活动 ${item.last_seen_at}` }}</small></div>
          <button v-if="!item.is_current" class="danger small" type="button" @click="revoke(item.session_id)">退出设备</button>
        </li>
      </ul>
    </article>
    <article class="card danger-zone"><div><p class="eyebrow danger-text">不可恢复</p><h2>注销账号</h2><p>永久删除账号、余额及非交易数据；存在历史订单时系统会阻止直接删除。</p></div><RouterLink class="button-link danger" to="/me/settings/account-closure">进入账号注销</RouterLink></article>
  </section>
</template>
