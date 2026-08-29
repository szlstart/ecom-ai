import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import AdminUserListPage from './AdminUserListPage.vue'

const mocks = vi.hoisted(() => ({ listAdminUsers: vi.fn(), createAdminUser: vi.fn() }))

vi.mock('@/api/admin-users', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-users')>(),
  listAdminUsers: mocks.listAdminUsers,
  createAdminUser: mocks.createAdminUser,
}))

describe('AdminUserListPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('joins status filters, visible sorting and user results into one workspace', async () => {
    mocks.listAdminUsers.mockResolvedValue({ data: { items: [
      { user_id: 'usr_a', username: 'alpha', nickname: '用户甲', account_status: 'active', registered_at: '2026-08-20T00:00:00Z', last_login_at: '2026-08-21T00:00:00Z', permission_version: 1, version: 1 },
      { user_id: 'usr_b', username: 'beta', nickname: '用户乙', account_status: 'suspended', registered_at: '2026-08-28T00:00:00Z', last_login_at: '2026-08-29T00:00:00Z', permission_version: 1, version: 1 },
    ], next_cursor: null } })
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAdminAuthStore()
    auth.accessToken = 'admin-token'
    auth.permissions = ['users:read', 'users:manage']
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/admin/users', component: AdminUserListPage },
      { path: '/admin/users/:userId', component: { template: '<div />' } },
    ] })
    await router.push('/admin/users')
    await router.isReady()
    const wrapper = mount(AdminUserListPage, { global: { plugins: [pinia, router] } })
    await flushPromises()

    const board = wrapper.get('.admin-operations-board')
    expect(board.find('.admin-operations-status').exists()).toBe(true)
    expect(board.find('.admin-visible-sort').exists()).toBe(true)
    expect(board.find('.admin-modern-table').exists()).toBe(true)
    expect(wrapper.find('select[aria-label="用户排序"]').exists()).toBe(false)
    expect(wrapper.findAll('.admin-visible-sort button')).toHaveLength(5)

    await wrapper.get('.admin-visible-sort button:nth-of-type(2)').trigger('click')
    expect(wrapper.findAll('.admin-modern-row .admin-user-cell strong').map((node) => node.text())).toEqual(['用户乙', '用户甲'])
    await wrapper.get('.admin-operations-status button:nth-child(3)').trigger('click')
    expect(wrapper.findAll('.admin-modern-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('用户乙')
    wrapper.unmount()
  })
})
