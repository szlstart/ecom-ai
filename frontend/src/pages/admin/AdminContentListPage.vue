<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { RouterLink } from 'vue-router'
import { listContent, type PlatformContent } from '@/api/admin-content'
import { errorMessage } from '@/api/http'
import PageState from '@/components/PageState.vue'
import { useAdminAuthStore } from '@/stores/admin-auth'
const auth = useAdminAuthStore(), items = ref<PlatformContent[]>([]), loading = ref(true), error = ref('')
async function load() { loading.value = true; try { if (!auth.accessToken) throw new Error('管理端登录已失效。'); items.value = (await listContent(auth.accessToken)).data.items } catch (cause) { error.value = errorMessage(cause) } finally { loading.value = false } }
onMounted(load)
</script>
<template><section class="admin-page-stack"><header class="page-heading"><div><p class="eyebrow">Platform Content</p><h1>平台内容</h1><p class="muted">首页 Banner、公告、帮助、关于与页脚采用不可变版本；公开端只读取当前生效版本。</p></div><RouterLink v-if="auth.has('content:manage')" class="button-link" to="/admin/content/new">新建内容</RouterLink></header><PageState :loading="loading" :error="error" :empty="!loading && items.length === 0" empty-title="暂无平台内容" @retry="load"><div class="card-list"><RouterLink v-for="item in items" :key="item.content_id" class="card" :to="`/admin/content/${item.content_id}`"><div class="card-heading"><div><h2>{{ item.title }}</h2><p>{{ item.content_key }} · {{ item.content_type }}</p></div><span class="badge">{{ item.status }}</span></div><p>版本数 {{ item.versions.length }} · 资源版本 {{ item.version }}</p></RouterLink></div></PageState></section></template>
