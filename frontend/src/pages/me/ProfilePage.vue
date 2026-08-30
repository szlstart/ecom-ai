<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { apiRequest, errorMessage, resolveApiAssetUrl } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import { useUserAuthStore } from '@/stores/user-auth'
import { imageFileFromClipboard } from '@/utils/clipboard-image'

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
const profile = ref<Profile | null>(null)
const etag = ref('')
const error = ref('')
const message = ref('')
const pending = ref(false)
const avatarUpload = ref<FileUploadHandle | null>(null)
const avatarFileId = ref<string | null>(null)
const avatarPasteBusy = ref(false)
const avatarPasteFocused = ref(false)
const pendingAvatarUrl = ref<string | null>(null)
const avatarUrl = computed(() => pendingAvatarUrl.value || resolveApiAssetUrl(profile.value?.avatar_url ?? null) || null)
const avatarLabel = computed(() => (profile.value?.username || '用').slice(0, 1).toUpperCase())

async function load() {
  const result = await apiRequest<Profile>('/users/me', {}, auth.accessToken)
  profile.value = result.data
  etag.value = result.headers.get('etag') ?? ''
}

function avatarUploaded(fileId: string) {
  avatarFileId.value = fileId
  pendingAvatarUrl.value = resolveApiAssetUrl(`/api/v1/files/${fileId}`)
  message.value = '新头像已上传，点击“保存修改”后生效。'
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

async function save() {
  if (!profile.value) return
  pending.value = true; error.value = ''; message.value = ''
  try {
    const body: Record<string, unknown> = {
      nickname: profile.value.nickname,
      locale: profile.value.locale,
      timezone: profile.value.timezone,
    }
    if (avatarFileId.value) body.avatar_file_id = avatarFileId.value
    const result = await apiRequest<Profile>('/users/me', {
      method: 'PATCH',
      headers: { 'If-Match': etag.value },
      body: JSON.stringify(body),
    }, auth.accessToken)
    profile.value = result.data
    etag.value = result.headers.get('etag') ?? ''
    avatarFileId.value = null
    pendingAvatarUrl.value = null
    auth.updateUser({ nickname: result.data.nickname, avatar_url: result.data.avatar_url })
    message.value = '个人资料已保存。'
  } catch (cause) { error.value = errorMessage(cause) }
  finally { pending.value = false }
}

onMounted(() => load().catch((reason) => { error.value = errorMessage(reason) }))
</script>

<template>
  <section class="settings-page profile-settings-page">
    <div class="page-heading"><div><p class="eyebrow">我的</p><h1>个人信息</h1><p>头像会显示在消息、个人中心和账号入口；未设置时使用用户名首字作为头像。</p></div></div>
    <p v-if="message" class="alert success" role="status">{{ message }}</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <form v-if="profile" class="card profile-settings-card" @submit.prevent="save">
      <section class="profile-avatar-editor">
        <div class="profile-avatar-preview"><img v-if="avatarUrl" :src="avatarUrl" alt="当前用户头像" /><span v-else>{{ avatarLabel }}</span></div>
        <div class="profile-avatar-copy"><p class="eyebrow">个人头像</p><h2>{{ profile.username }}</h2><p>支持从本地选择，也可复制图片后点击下方区域，按 Command + V 或 Ctrl + V 粘贴。</p></div>
      </section>
      <div class="profile-avatar-paste-zone" :class="{ focused: avatarPasteFocused, busy: avatarPasteBusy }" tabindex="0" role="button" aria-label="用户头像粘贴上传区" @focus="avatarPasteFocused = true" @blur="avatarPasteFocused = false" @paste="pasteAvatar">
        <strong>{{ avatarPasteBusy ? '正在读取、扫描并上传头像…' : '上传或粘贴新头像' }}</strong>
        <AdminFileUpload ref="avatarUpload" purpose="user_avatar" :access-token="auth.accessToken" label="从本地选择头像" :disabled="pending" @uploaded="avatarUploaded" />
      </div>
      <div class="profile-settings-fields">
        <label>账号<input :value="profile.username" disabled /></label>
        <label>昵称<input v-model="profile.nickname" minlength="2" maxlength="20" required /></label>
        <label>语言<input v-model="profile.locale" /></label>
        <label>时区<input v-model="profile.timezone" /></label>
      </div>
      <div class="profile-bound-accounts"><strong>已绑定账号</strong><ul><li v-for="item in profile.bound_accounts" :key="item.type">{{ item.type }}：{{ item.masked }}</li></ul></div>
      <button :disabled="pending || avatarPasteBusy">{{ pending ? '正在保存…' : '保存修改' }}</button>
    </form>
  </section>
</template>
