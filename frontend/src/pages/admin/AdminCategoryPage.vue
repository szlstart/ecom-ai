<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'

import {
  adminCreate,
  adminGet,
  adminUpdate,
  requireAdminToken,
  type AdminCategory,
} from '@/api/admin-catalog'
import { errorMessage } from '@/api/http'
import AdminFileUpload from '@/components/AdminFileUpload.vue'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'

const auth = useAdminAuthStore()
const items = ref<AdminCategory[]>([])
const loading = ref(true)
const saving = ref(false)
const error = ref('')
const message = ref('')
const editing = ref<AdminCategory | null>(null)
const form = reactive({ parent_id: '', category_name: '', category_code: '', sort_order: 0, status: 'active', icon_file_id: '' })
const parentOptions = computed(() => items.value.filter((item) => item.category_id !== editing.value?.category_id))

async function load() {
  loading.value = true
  error.value = ''
  try { items.value = (await adminGet<AdminCategory[]>('/admin/categories', requireAdminToken(auth.accessToken))).data }
  catch (cause) { error.value = errorMessage(cause) }
  finally { loading.value = false }
}

function startCreate() {
  editing.value = null
  Object.assign(form, { parent_id: '', category_name: '', category_code: '', sort_order: 0, status: 'active', icon_file_id: '' })
}

function startEdit(item: AdminCategory) {
  editing.value = item
  Object.assign(form, { parent_id: item.parent_id ?? '', category_name: item.category_name, category_code: item.category_code, sort_order: item.sort_order, status: item.status, icon_file_id: '' })
}

async function save() {
  saving.value = true
  error.value = ''
  message.value = ''
  try {
    const token = requireAdminToken(auth.accessToken)
    if (editing.value) {
      const payload: Record<string, unknown> = {
        parent_id: form.parent_id || null,
        category_name: form.category_name,
        category_code: form.category_code,
        sort_order: form.sort_order,
        status: form.status,
      }
      if (form.icon_file_id) payload.icon_file_id = form.icon_file_id
      await adminUpdate(`/admin/categories/${encodeURIComponent(editing.value.category_id)}`, payload, token, editing.value.version)
      message.value = '分类已更新。'
    } else {
      await adminCreate('/admin/categories', {
        parent_id: form.parent_id || null,
        category_name: form.category_name,
        category_code: form.category_code,
        sort_order: form.sort_order,
        icon_file_id: form.icon_file_id || null,
      }, token, 'category-create')
      message.value = '分类已创建。'
      startCreate()
    }
    await load()
  } catch (cause) { error.value = errorMessage(cause) }
  finally { saving.value = false }
}

onMounted(() => { startCreate(); void load() })
</script>

<template>
  <section class="admin-page-stack">
    <header class="page-heading"><div><p class="eyebrow">商品与库存</p><h1>平台分类</h1><p class="muted">移动分类会执行防环校验；已被引用的分类只能停用。</p></div><button type="button" class="secondary" @click="startCreate">新建分类</button></header>
    <p v-if="message" class="alert success" aria-live="polite">{{ message }}</p>
    <p v-if="error" class="alert error" role="alert">{{ error }}</p>
    <div class="admin-split">
      <PageState :loading="loading" :error="''" :empty="!loading && items.length === 0" empty-title="暂无分类">
        <div class="table-wrap"><table><thead><tr><th>分类</th><th>层级/路径</th><th>状态</th><th>排序</th><th>操作</th></tr></thead><tbody><tr v-for="item in items" :key="item.category_id"><td><strong>{{ item.category_name }}</strong><small>{{ item.category_code }} · {{ item.category_id }}</small></td><td>{{ item.level }} · {{ item.path }}</td><td><span class="badge">{{ item.status }}</span></td><td>{{ item.sort_order }}</td><td><button type="button" class="secondary small" @click="startEdit(item)">编辑</button></td></tr></tbody></table></div>
      </PageState>
      <form class="card admin-editor" @submit.prevent="save">
        <h2>{{ editing ? '编辑分类' : '新建分类' }}</h2>
        <label>分类名称<input v-model.trim="form.category_name" required maxlength="64" /></label>
        <label>分类编码<input v-model.trim="form.category_code" required pattern="[a-z][a-z0-9-]{1,63}" maxlength="64" /></label>
        <label>父分类<select v-model="form.parent_id"><option value="">无（根分类）</option><option v-for="item in parentOptions" :key="item.category_id" :value="item.category_id">{{ item.category_name }} · {{ item.path }}</option></select></label>
        <label>排序<input v-model.number="form.sort_order" type="number" min="0" max="1000000" required /></label>
        <AdminFileUpload purpose="category_icon" label="上传分类图标" @uploaded="form.icon_file_id = $event" />
        <p v-if="form.icon_file_id" class="muted">待绑定文件：{{ form.icon_file_id }}</p>
        <label v-if="editing">状态<select v-model="form.status"><option value="active">启用</option><option value="disabled">停用</option></select></label>
        <div class="actions"><button :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button><button v-if="editing" type="button" class="secondary" @click="startCreate">取消编辑</button></div>
      </form>
    </div>
  </section>
</template>
