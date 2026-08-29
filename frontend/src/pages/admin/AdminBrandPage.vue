<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { adminCreate, adminGet, adminUpdate, requireAdminToken, type AdminBrand } from '@/api/admin-catalog'
import { errorMessage, resolveApiAssetUrl } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminBrand[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const message = ref('')
const editing = ref<AdminBrand | null>(null)
const form = reactive({ brand_name: '', logo_file_id: '', description: '', status: 'active' })

async function load() {
  loading.value = true
  try { items.value = (await adminGet<AdminBrand[]>('/admin/brands', requireAdminToken(auth.accessToken))).data }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function reset() { editing.value = null; Object.assign(form, { brand_name: '', logo_file_id: '', description: '', status: 'active' }) }
function edit(item: AdminBrand) { editing.value = item; Object.assign(form, { brand_name: item.brand_name, logo_file_id: '', description: item.description ?? '', status: item.status }) }

async function save() {
  saving.value = true; error.value = ''; message.value = ''
  try {
    const token = requireAdminToken(auth.accessToken)
    if (editing.value) {
      const payload: Record<string, unknown> = { brand_name: form.brand_name, description: form.description || null, status: form.status }
      if (form.logo_file_id) payload.logo_file_id = form.logo_file_id
      await adminUpdate(`/admin/brands/${encodeURIComponent(editing.value.brand_id)}`, payload, token, editing.value.version)
      message.value = '品牌已更新。'
    } else {
      await adminCreate('/admin/brands', { brand_name: form.brand_name, logo_file_id: form.logo_file_id || null, description: form.description || null }, token, 'brand-create')
      message.value = '品牌已创建。'; reset()
    }
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

onMounted(() => { reset(); void load() })
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading"><div><p class="eyebrow">商品与库存</p><h1>品牌管理</h1><p class="muted">Logo 必须先完成上传、扫描和激活；被商品引用的品牌只允许停用。</p></div><button type="button" class="secondary" @click="reset">新建品牌</button></header>
    <p v-if="message" class="alert success" aria-live="polite">{{ message }}</p><p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div class="admin-split">
      <PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="暂无品牌">
        <div class="table-wrap"><table><thead><tr><th>品牌</th><th>简介</th><th>状态</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.brand_id"><td><span class="entity-with-image"><img v-if="item.logo_url" :src="resolveApiAssetUrl(item.logo_url) || undefined" alt="" width="40" height="40" /><span><strong>{{ item.brand_name }}</strong><small>{{ item.brand_id }}</small></span></span></td><td>{{ item.description || '—' }}</td><td><span class="badge">{{ item.status }}</span></td><td><button type="button" class="secondary small" @click="edit(item)">编辑</button></td></tr></tbody></table></div>
      </PageState>
      <form class="card admin-editor" @submit.prevent="save"><h2>{{ editing ? '编辑品牌' : '新建品牌' }}</h2><label>品牌名称<input v-model.trim="form.brand_name" required maxlength="128" /></label><AdminFileUpload purpose="brand_logo" label="上传品牌 Logo" @uploaded="form.logo_file_id = $event" /><p v-if="form.logo_file_id" class="muted">待绑定文件：{{ form.logo_file_id }}</p><label>品牌简介<textarea v-model.trim="form.description" maxlength="2000" /></label><label v-if="editing">状态<select v-model="form.status"><option value="active">启用</option><option value="disabled">停用</option></select></label><div class="actions"><button :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button><button v-if="editing" type="button" class="secondary" @click="reset">取消编辑</button></div></form>
    </div>
  </section>
</template>
