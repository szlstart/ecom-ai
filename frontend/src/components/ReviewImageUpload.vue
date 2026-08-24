<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import {
  getUploadPolicy,
  loadProtectedFileObjectUrl,
  uploadBindableFile,
  type UploadPolicy,
} from '@/api/files'
import { errorMessage } from '@/api/http'
import { useUserAuthStore } from '@/stores/user-auth'

const props = defineProps<{ modelValue: string[]; disabled?: boolean }>()
const emit = defineEmits<{
  'update:modelValue': [fileIds: string[]]
  'busy-change': [busy: boolean]
}>()
const auth = useUserAuthStore()
const policy = ref<UploadPolicy | null>(null)
const selected = ref<File[]>([])
const previews = ref<Record<string, string>>({})
const busy = ref(false)
const status = ref('')
const error = ref('')
const maximum = computed(() => policy.value?.max_count ?? 6)
const remaining = computed(() => Math.max(0, maximum.value - props.modelValue.length))
const accept = computed(() => policy.value?.allowed_mime_types.join(',') || 'image/jpeg,image/png,image/webp')

function choose(event: Event) {
  error.value = ''
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (files.length > remaining.value) {
    error.value = `最多上传 ${maximum.value} 张图片，当前还可选择 ${remaining.value} 张。`
    selected.value = []
    return
  }
  selected.value = files
}

async function upload() {
  if (!auth.accessToken || !policy.value || selected.value.length === 0) return
  busy.value = true
  error.value = ''
  const pending = [...selected.value]
  const uploadedIds = [...props.modelValue]
  selected.value = []
  try {
    for (const [index, file] of pending.entries()) {
      const fileId = await uploadBindableFile(
        file,
        'review_image',
        auth.accessToken,
        policy.value,
        (message) => { status.value = `第 ${index + 1}/${pending.length} 张：${message}` },
      )
      previews.value[fileId] = URL.createObjectURL(file)
      uploadedIds.push(fileId)
      emit('update:modelValue', [...uploadedIds])
      await nextTick()
    }
    status.value = `${pending.length} 张图片已通过安全处理，可随评价一并提交。`
  } catch (cause) {
    error.value = cause instanceof Error && !(cause instanceof TypeError) ? cause.message : errorMessage(cause)
  } finally {
    busy.value = false
  }
}

function remove(fileId: string) {
  revokePreview(fileId)
  emit('update:modelValue', props.modelValue.filter((item) => item !== fileId))
}

async function loadPolicy() {
  try {
    policy.value = (await getUploadPolicy('review_image')).data
    await loadMissingPreviews(props.modelValue)
  } catch (cause) {
    error.value = errorMessage(cause)
  }
}

async function loadMissingPreviews(fileIds: string[]) {
  if (!auth.accessToken) return
  await Promise.all(fileIds.map(async (fileId) => {
    if (previews.value[fileId]) return
    try {
      previews.value[fileId] = await loadProtectedFileObjectUrl(
        `/files/${encodeURIComponent(fileId)}`,
        auth.accessToken!,
      )
    } catch {
      // A missing thumbnail must not prevent the user from retaining or removing the binding.
    }
  }))
}

function revokePreview(fileId: string) {
  const url = previews.value[fileId]
  if (url?.startsWith('blob:')) URL.revokeObjectURL(url)
  delete previews.value[fileId]
}

watch(() => props.modelValue, (fileIds, previous) => {
  for (const fileId of previous ?? []) if (!fileIds.includes(fileId)) revokePreview(fileId)
  void loadMissingPreviews(fileIds)
}, { deep: true })
watch(busy, (value) => emit('busy-change', value))

onMounted(loadPolicy)
onBeforeUnmount(() => Object.keys(previews.value).forEach(revokePreview))
</script>

<template>
  <fieldset class="review-image-upload" :disabled="disabled || busy">
    <legend>评价图片（{{ modelValue.length }}/{{ maximum }}）</legend>
    <div v-if="modelValue.length" class="review-image-grid">
      <article v-for="(fileId, index) in modelValue" :key="fileId">
        <img v-if="previews[fileId]" :src="previews[fileId]" :alt="`评价图片 ${index + 1}`" />
        <div v-else class="review-image-placeholder">图片 {{ index + 1 }}</div>
        <button type="button" class="danger small" :disabled="disabled || busy" @click="remove(fileId)">移除</button>
      </article>
    </div>
    <div class="file-upload-control">
      <label>选择图片
        <input type="file" multiple :accept="accept" :disabled="disabled || busy || remaining === 0" @change="choose" />
      </label>
      <button type="button" class="secondary small" :disabled="disabled || busy || selected.length === 0" @click="upload">
        {{ busy ? '处理中…' : `上传并扫描${selected.length ? `（${selected.length}）` : ''}` }}
      </button>
      <small>支持 JPG、PNG、WebP；单张不超过 {{ Math.ceil((policy?.max_size_bytes ?? 10485760) / 1024 / 1024) }} MB。图片通过安全处理后才会绑定评价。</small>
      <small v-if="status" class="success-text" aria-live="polite">{{ status }}</small>
      <small v-if="error" class="error-text" role="alert">{{ error }}</small>
    </div>
  </fieldset>
</template>
