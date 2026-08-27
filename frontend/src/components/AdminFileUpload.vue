<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { apiRequest, createIdempotencyKey, errorMessage } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'

interface UploadPolicy {
  purpose: string
  allowed_mime_types: string[]
  allowed_extensions: string[]
  max_size_bytes: number
  max_count: number
}

interface FileVariant {
  file_id: string
  status: string
  scan_status: string
}

interface UploadSession {
  upload_id: string
  upload_status: string
  upload: { method: string; url: string; headers: Record<string, string>; expires_at: string } | null
  bindable_file: FileVariant | null
}

const props = defineProps<{ purpose: string; businessContextId?: string | null; label?: string }>()
const emit = defineEmits<{ uploaded: [fileId: string]; busyChanged: [busy: boolean] }>()
const auth = useAdminAuthStore()
const policy = ref<UploadPolicy | null>(null)
const selected = ref<File | null>(null)
const busy = ref(false)
const status = ref('')
const error = ref('')
const accept = computed(() => policy.value?.allowed_mime_types.join(',') || undefined)

async function choose(event: Event) {
  selected.value = (event.target as HTMLInputElement).files?.[0] ?? null
  error.value = ''
  if (!policy.value) {
    try { policy.value = (await apiRequest<UploadPolicy>(`/file-upload-policies/${encodeURIComponent(props.purpose)}`)).data }
    catch (cause) { error.value = errorMessage(cause) }
  }
}

async function loadPolicy() {
  try { policy.value = (await apiRequest<UploadPolicy>(`/file-upload-policies/${encodeURIComponent(props.purpose)}`)).data }
  catch (cause) { error.value = errorMessage(cause) }
}

async function upload(throwOnError = false) {
  if (!selected.value || !auth.accessToken) return
  busy.value = true; emit('busyChanged', true); error.value = ''; status.value = '正在计算文件校验值…'
  try {
    const sha256 = await digest(selected.value)
    if (!policy.value) policy.value = (await apiRequest<UploadPolicy>(`/file-upload-policies/${encodeURIComponent(props.purpose)}`)).data
    if (selected.value.size > policy.value.max_size_bytes) throw new Error(`文件超过 ${Math.ceil(policy.value.max_size_bytes / 1024 / 1024)} MB 上限`)
    status.value = '正在创建受控上传会话…'
    const session = (await apiRequest<UploadSession>('/file-upload-sessions', {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey('file-upload') },
      body: JSON.stringify({ purpose: props.purpose, filename: selected.value.name, size_bytes: selected.value.size, content_type: selected.value.type || 'application/octet-stream', sha256, business_context_id: props.businessContextId || null }),
    }, auth.accessToken)).data
    if (!session.upload) throw new Error('上传会话未返回对象存储指令')
    status.value = '正在上传到临时隔离区…'
    const uploadResponse = await fetch(session.upload.url, { method: session.upload.method, headers: session.upload.headers, body: selected.value })
    if (!uploadResponse.ok) throw new Error(`对象存储上传失败（${uploadResponse.status}）`)
    status.value = '正在验证并提交安全扫描…'
    await apiRequest<UploadSession>(`/file-upload-sessions/${encodeURIComponent(session.upload_id)}/complete`, {
      method: 'POST',
      headers: { 'Idempotency-Key': createIdempotencyKey('file-complete') },
      body: JSON.stringify({ sha256, provider_checksum: uploadResponse.headers.get('etag') }),
    }, auth.accessToken)
    const fileId = await waitUntilBindable(session.upload_id)
    status.value = `文件已通过扫描：${fileId}`
    emit('uploaded', fileId)
    selected.value = null
  } catch (cause) {
    error.value = cause instanceof Error && !(cause instanceof TypeError) ? cause.message : errorMessage(cause)
    if (throwOnError) throw new Error(error.value)
  }
  finally { busy.value = false; emit('busyChanged', false) }
}

async function uploadFile(file: File) {
  if (busy.value) throw new Error('当前文件正在处理中，请稍候。')
  selected.value = file
  await upload(true)
}

async function waitUntilBindable(uploadId: string): Promise<string> {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    const current = (await apiRequest<UploadSession>(`/file-upload-sessions/${encodeURIComponent(uploadId)}`, {}, auth.accessToken)).data
    if (current.bindable_file?.status === 'active' && current.bindable_file.scan_status === 'safe') return current.bindable_file.file_id
    if (current.bindable_file?.scan_status === 'rejected') throw new Error('文件未通过安全扫描，请更换文件。')
    status.value = `安全扫描处理中（${attempt + 1}/30）…`
    await new Promise((resolve) => window.setTimeout(resolve, 1000))
  }
  throw new Error('安全扫描仍在进行。可稍后从上传记录重试绑定。')
}

async function digest(file: File): Promise<string> {
  const hash = await crypto.subtle.digest('SHA-256', await file.arrayBuffer())
  return Array.from(new Uint8Array(hash), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

onMounted(loadPolicy)
defineExpose({ uploadFile })
</script>

<template>
  <div class="file-upload-control">
    <label>{{ label || '上传文件' }}<input type="file" :accept="accept" :disabled="busy" @change="choose" /></label>
    <button type="button" class="secondary small" :disabled="!selected || busy" @click="upload(false)">{{ busy ? '处理中…' : '上传并扫描' }}</button>
    <small v-if="status" class="success-text" aria-live="polite">{{ status }}</small>
    <small v-if="error" class="error-text" role="alert">{{ error }}</small>
  </div>
</template>
