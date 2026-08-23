<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { adminCommand, adminGet, requireAdminToken, type AdminCertification, type AdminCertificationEvent } from '@/api/admin-catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const route = useRoute(); const auth = useAdminAuthStore()
const item = ref<AdminCertification | null>(null)
const events = ref<AdminCertificationEvent[]>([])
const loading = ref(true); const saving = ref(false); const error = ref(''); const message = ref('')
const form = reactive({ decision: 'approve', reason_code: 'DOCUMENTS_VERIFIED', reason: '', valid_from: '', valid_until: '', material_code: 'SUPPLEMENTARY_DOCUMENT', material_title: '', material_description: '', due_at: '' })
const materialFiles = ref<string[]>([]); const materialReason = ref('')
const id = computed(() => String(route.params.certificationId))

async function load() {
  loading.value = true; error.value = ''
  try {
    const token = requireAdminToken(auth.accessToken)
    const [detail, history] = await Promise.all([
      adminGet<AdminCertification>(`/admin/store-certifications/${encodeURIComponent(id.value)}`, token),
      adminGet<AdminCertificationEvent[]>(`/admin/store-certifications/${encodeURIComponent(id.value)}/events`, token),
    ])
    item.value = detail.data; events.value = history.data
  } catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

async function decide() {
  if (!item.value) return
  saving.value = true; error.value = ''; message.value = ''
  const payload: Record<string, unknown> = { decision: form.decision, reason_code: form.reason_code, reason: form.reason }
  if (form.decision === 'approve') { payload.valid_from = form.valid_from; payload.valid_until = form.valid_until }
  if (form.decision === 'request_more_info') payload.required_materials = [{ material_code: form.material_code, title: form.material_title, description: form.material_description || null, due_at: form.due_at ? new Date(form.due_at).toISOString() : null }]
  try {
    await adminCommand(`/admin/store-certifications/${encodeURIComponent(id.value)}/decisions`, payload, requireAdminToken(auth.accessToken), item.value.version, 'certification-decision')
    message.value = '审核决定已记录。'; await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

async function addMaterialVersion() {
  if (!item.value || materialFiles.value.length === 0) return
  saving.value = true; error.value = ''; message.value = ''
  try {
    await adminCommand(`/admin/store-certifications/${encodeURIComponent(id.value)}/material-versions`, { evidence_file_ids: materialFiles.value, reason: materialReason.value }, requireAdminToken(auth.accessToken), item.value.version, 'certification-material')
    message.value = '新材料版本已提交，旧版本历史保持不变。'; materialFiles.value = []; materialReason.value = ''; await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

onMounted(load)
</script>

<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">店铺认证审核</p><h1>{{ item?.store_name || '认证详情' }}</h1></div><RouterLink to="/admin/store-certifications">返回队列</RouterLink></header><p v-if="message" class="alert success" aria-live="polite">{{ message }}</p><PageState :loading="loading" :error="error" :empty="!loading && !item" empty-title="认证不存在" @retry="load"><template v-if="item"><div class="admin-detail-grid"><article class="card"><h2>认证事实</h2><dl class="detail-list"><div><dt>认证 ID</dt><dd>{{ item.certification_id }}</dd></div><div><dt>店铺</dt><dd>{{ item.store_name }} · {{ item.store_id }}</dd></div><div><dt>类型/状态</dt><dd>{{ item.certification_type }} / {{ item.review_status }}</dd></div><div><dt>材料版本</dt><dd>v{{ item.material_version }}</dd></div><div><dt>凭证文件</dt><dd><ul><li v-for="fileId in item.evidence_file_ids" :key="fileId"><a :href="resolveApiAssetUrl(`/api/v1/files/${fileId}`) || undefined" target="_blank" rel="noopener">{{ fileId }}</a></li></ul></dd></div></dl><form v-if="auth.has('stores:manage')" class="admin-editor command-box" @submit.prevent="addMaterialVersion"><h3>提交新材料版本</h3><AdminFileUpload purpose="store_certification" :business-context-id="item.store_id" label="上传补充材料" @uploaded="materialFiles.push($event)" /><ul><li v-for="fileId in materialFiles" :key="fileId">{{ fileId }}</li></ul><label>提交说明<textarea v-model.trim="materialReason" required minlength="2" maxlength="500" /></label><button :disabled="saving || materialFiles.length === 0">提交材料版本</button></form></article><form v-if="auth.has('stores:review')" class="card admin-editor" @submit.prevent="decide"><h2>提交审核决定</h2><label>决定<select v-model="form.decision"><option value="approve">通过</option><option value="reject">拒绝</option><option value="request_more_info">要求补件</option></select></label><label>原因码<input v-model.trim="form.reason_code" required pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>审核说明<textarea v-model.trim="form.reason" required minlength="2" maxlength="1000" /></label><template v-if="form.decision === 'approve'"><label>有效起始日<input v-model="form.valid_from" type="date" required /></label><label>有效截止日<input v-model="form.valid_until" type="date" required /></label></template><template v-if="form.decision === 'request_more_info'"><label>材料码<input v-model.trim="form.material_code" required pattern="[A-Z][A-Z0-9_]{1,63}" /></label><label>材料标题<input v-model.trim="form.material_title" required maxlength="128" /></label><label>材料说明<textarea v-model.trim="form.material_description" maxlength="500" /></label><label>补件截止时间<input v-model="form.due_at" type="datetime-local" /></label></template><button :disabled="saving">{{ saving ? '提交中…' : '确认决定' }}</button></form></div><article class="card"><h2>不可变事件历史</h2><ol class="timeline"><li v-for="event in events" :key="event.event_id"><strong>{{ event.event_type }}</strong><span>材料 v{{ event.material_version }} · 认证 v{{ event.certification_version }}</span><p>{{ event.reason || event.reason_code || '无说明' }}</p><time>{{ event.created_at }}</time></li></ol></article></template></PageState></section></template>
