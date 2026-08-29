import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import AdminUserDetailPage from './AdminUserDetailPage.vue'

const mocks = vi.hoisted(() => ({ apiRequest: vi.fn() }))

vi.mock('@/api/http', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/http')>(),
  apiRequest: mocks.apiRequest,
}))

describe('AdminUserDetailPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('shows direct-edit sections in one vertical workspace and keeps long audit history in a modal', async () => {
    mocks.apiRequest.mockImplementation(async (path: string) => {
      if (path === '/admin/users/usr_test') return { data: {
        user_id: 'usr_test', username: 'customer_one', nickname: '顾客一号', account_status: 'active',
        registered_at: '2026-08-20T08:00:00Z', last_login_at: '2026-08-29T08:00:00Z', permission_version: 1, version: 3,
      }, headers: new Headers({ etag: '"v3"' }) }
      if (path.endsWith('/role-grants')) return { data: [{ grant_id: 'grant_1', role_id: 'role_user', role_name: '普通用户', scope_type: 'platform', scope_id: 0, status: 'active', version: 1 }] }
      if (path.endsWith('/status-events')) return { data: [{ status_event_id: 'status_1', to_status: 'active', reason: '完成注册', effective_at: '2026-08-20T08:00:00Z' }] }
      if (path.endsWith('/role-grant-events')) return { data: [{ event_id: 'event_1', event_type: 'granted', reason: '默认角色', created_at: '2026-08-20T08:00:00Z' }] }
      throw new Error(`unexpected path: ${path}`)
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAdminAuthStore()
    auth.accessToken = 'admin-token'
    auth.permissions = ['users:manage', 'users:force_password_reset', 'rbac:read']
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/admin/users', component: { template: '<div />' } },
      { path: '/admin/users/:userId', component: AdminUserDetailPage },
      { path: '/admin/orders', component: { template: '<div />' } },
      { path: '/admin/support/tickets', component: { template: '<div />' } },
    ] })
    await router.push('/admin/users/usr_test')
    await router.isReady()
    const wrapper = mount(AdminUserDetailPage, { global: { plugins: [pinia, router], stubs: { Teleport: true } } })
    await flushPromises()

    expect(wrapper.find('.admin-detail-tabs').exists()).toBe(false)
    for (const heading of ['编辑用户资料', '冻结、恢复与下线', '设置临时密码', '调整账户余额', '角色与数据范围', '删除用户']) {
      expect(wrapper.text()).toContain(heading)
    }
    expect(wrapper.text()).not.toContain('完成注册')
    await wrapper.get('.admin-user-context-links button').trigger('click')
    expect(wrapper.get('.admin-activity-dialog').text()).toContain('完成注册')
    expect(wrapper.get('.admin-activity-dialog').text()).toContain('默认角色')
    await wrapper.get('.admin-activity-dialog footer button').trigger('click')
    expect(wrapper.find('.admin-activity-dialog').exists()).toBe(false)
    wrapper.unmount()
  })
})
