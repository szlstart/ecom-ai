<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, RouterView, useRoute, useRouter } from 'vue-router'

import { listSupportTickets } from '@/api/admin-support'
import { apiRequest } from '@/api/http'
import { useAdminAuthStore } from '@/stores/admin-auth'
import { useDialogA11y } from '@/composables/dialog-a11y'

interface NavigationItem { code: string; title: string; route: string; required_permission: string }
interface AdminMe { user_id: string; username: string; nickname: string }

const auth = useAdminAuthStore()
const router = useRouter()
const route = useRoute()
const navigation = ref<NavigationItem[]>([])
const profile = ref<AdminMe | null>(null)
const navigationLoading = ref(true)
const sidebarOpen = ref(false)
const profileOpen = ref(false)
const searchOpen = ref(false)
const searchQuery = ref('')
const searchDialog = ref<HTMLElement | null>(null)
const unreadCount = ref(0)
let unreadTimer: number | undefined

const groupDefinitions = [
  { key: 'overview', title: '运营总览', icon: '⌂', codes: ['dashboard'] },
  { key: 'people', title: '用户与店铺', icon: '◎', codes: ['users', 'stores'] },
  { key: 'service', title: '服务与内容', icon: '◌', codes: ['support', 'content'] },
  { key: 'ai', title: 'AI 智能中心', icon: '✦', codes: ['ai-center', 'ai-agents', 'ai-skills', 'ai-tools', 'ai-policies', 'knowledge', 'ai-evaluations', 'observability'] },
  { key: 'governance', title: '运营治理', icon: '◇', codes: ['roles', 'approvals', 'audit'] },
  { key: 'system', title: '系统运维', icon: '⌘', codes: ['batch-jobs', 'dead-letter-events'] },
] as const

const groups = computed(() => groupDefinitions.map((definition) => ({
  ...definition,
  items: definition.codes.map((code) => navigation.value.find((item) => item.code === code)).filter((item): item is NavigationItem => Boolean(item)),
})).filter((group) => group.items.length))
const searchResults = computed(() => {
  const keyword = searchQuery.value.trim().toLocaleLowerCase()
  if (!keyword) return navigation.value.slice(0, 8)
  return navigation.value.filter((item) => `${item.title} ${item.code}`.toLocaleLowerCase().includes(keyword)).slice(0, 10)
})
const currentTitle = computed(() => String(route.meta.title || '管理中心'))
const initials = computed(() => (profile.value?.nickname || profile.value?.username || '管').slice(0, 1).toUpperCase())

function iconFor(code: string): string {
  return ({ dashboard: '⌂', users: '♙', stores: '▣', support: '◍', content: '▧', 'ai-center': '✦', 'ai-agents': '◈', 'ai-skills': '⌁', 'ai-tools': '⌘', 'ai-policies': '♢', knowledge: '▱', 'ai-evaluations': '⌁', observability: '⌇', roles: '♜', approvals: '✓', audit: '◎', 'batch-jobs': '⇄', 'dead-letter-events': '!' } as Record<string, string>)[code] ?? '•'
}

async function loadNavigation() {
  if (!auth.accessToken) return
  navigationLoading.value = true
  try {
    const [navigationResult, meResult] = await Promise.all([
      apiRequest<{ items: NavigationItem[] }>('/admin/navigation', {}, auth.accessToken),
      apiRequest<AdminMe>('/admin/me', {}, auth.accessToken),
    ])
    navigation.value = navigationResult.data.items
    profile.value = meResult.data
  } catch { navigation.value = [] }
  finally { navigationLoading.value = false }
}

async function refreshUnread() {
  if (!auth.accessToken || !auth.has('support:queue_read')) return
  try {
    const result = await listSupportTickets({}, auth.accessToken)
    unreadCount.value = result.data.items.reduce((total, ticket) => total + ticket.unread_count, 0)
  } catch { /* 消息中心内提供详细错误 */ }
}

function openSearch() {
  searchOpen.value = true
  window.setTimeout(() => document.querySelector<HTMLInputElement>('.admin-command-input')?.focus(), 0)
}
function closeSearch() { searchOpen.value = false }
useDialogA11y(searchOpen, searchDialog, closeSearch)
async function chooseSearchResult(item: NavigationItem) { searchOpen.value = false; searchQuery.value = ''; await router.push(item.route) }
function handleKeydown(event: KeyboardEvent) {
  if ((event.metaKey || event.ctrlKey) && event.key.toLocaleLowerCase() === 'k') { event.preventDefault(); openSearch() }
  if (event.key === 'Escape') { searchOpen.value = false; profileOpen.value = false; sidebarOpen.value = false }
}
function closeFloatingMenus() { profileOpen.value = false }
async function logout() { await auth.logout('admin'); await router.replace('/admin/login') }

onMounted(async () => {
  await loadNavigation(); await refreshUnread()
  unreadTimer = window.setInterval(() => void refreshUnread(), 15_000)
  window.addEventListener('keydown', handleKeydown)
  window.addEventListener('click', closeFloatingMenus)
})
onBeforeUnmount(() => { if (unreadTimer) window.clearInterval(unreadTimer); window.removeEventListener('keydown', handleKeydown); window.removeEventListener('click', closeFloatingMenus) })
watch(() => auth.permissions.join(','), loadNavigation)
watch(() => route.fullPath, () => { sidebarOpen.value = false; profileOpen.value = false })
</script>

<template>
  <div class="admin-shell premium-admin-shell" :class="{ 'message-workspace-shell': route.path === '/admin/messages' }">
    <button v-if="sidebarOpen" class="admin-sidebar-backdrop" aria-label="关闭导航" @click="sidebarOpen = false" />
    <aside class="admin-sidebar premium-admin-sidebar" :class="{ open: sidebarOpen }">
      <RouterLink class="admin-brand" to="/admin/dashboard"><span class="admin-brand-mark">E</span><span><strong>Ecom AI</strong><small>超级管理中心</small></span></RouterLink>
      <div class="admin-scope-pill"><span />平台全局管理</div>
      <nav aria-label="管理端主导航">
        <div v-if="navigationLoading" class="admin-nav-loading">正在加载工作区…</div>
        <section v-for="group in groups" :key="group.key" class="admin-nav-group">
          <p><span>{{ group.icon }}</span>{{ group.title }}</p>
          <RouterLink v-for="item in group.items" :key="item.code" :to="item.route"><span class="admin-nav-icon">{{ iconFor(item.code) }}</span><span>{{ item.title }}</span></RouterLink>
        </section>
      </nav>
      <div class="admin-sidebar-footer"><div class="admin-help-card"><span>?</span><div><strong>需要帮助？</strong><small>使用顶部 AI 管家快速定位功能</small></div></div><button class="admin-sidebar-logout" @click="logout"><span>↪</span>退出管理端</button></div>
    </aside>

    <div class="admin-workspace">
      <header class="admin-topbar">
        <div class="admin-topbar-title"><button class="admin-mobile-menu" aria-label="打开导航" @click="sidebarOpen = true">☰</button><div><small>管理工作区</small><strong>{{ currentTitle }}</strong></div></div>
        <button class="admin-search-trigger" @click="openSearch"><span>⌕</span><span>搜索管理功能</span><kbd>⌘ K</kbd></button>
        <div class="admin-topbar-actions">
          <RouterLink v-if="auth.has('support:queue_read')" class="admin-message-trigger" :class="{ unread: unreadCount > 0 }" aria-label="打开消息中心" to="/admin/messages"><span>◍</span><span>消息</span><b v-if="unreadCount">{{ unreadCount > 99 ? '99+' : unreadCount }}</b></RouterLink>
          <div class="admin-profile-menu" @click.stop><button @click="profileOpen = !profileOpen"><span class="admin-profile-avatar">{{ initials }}</span><span class="admin-profile-copy"><strong>{{ profile?.nickname || '平台管理员' }}</strong><small>{{ profile?.username || '正在载入…' }}</small></span><span>⌄</span></button><div v-if="profileOpen" class="admin-profile-dropdown"><button @click="logout">退出登录</button></div></div>
        </div>
      </header>
      <main class="admin-content" :class="{ 'message-workspace-content': route.path === '/admin/messages' }"><RouterView :key="route.path" /></main>
    </div>

    <div v-if="searchOpen" class="admin-command-overlay" @click.self="closeSearch"><section ref="searchDialog" class="admin-command-panel" role="dialog" aria-modal="true" aria-label="管理功能搜索" tabindex="-1"><header><span>⌕</span><input v-model="searchQuery" class="admin-command-input" placeholder="输入功能名称，例如：用户治理、审批、Skill" /><kbd>ESC</kbd></header><div class="admin-command-results"><p>{{ searchQuery ? '功能搜索结果' : '常用功能' }}</p><button v-for="item in searchResults" :key="item.code" @click="chooseSearchResult(item)"><span class="admin-nav-icon">{{ iconFor(item.code) }}</span><span><strong>{{ item.title }}</strong><small>{{ item.route }}</small></span><b>↵</b></button><div v-if="!searchResults.length" class="empty-state">没有找到对应管理功能</div></div></section></div>
  </div>
</template>
