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

  it('shows the complete user workspace and keeps orders in a modal', async () => {
    mocks.apiRequest.mockImplementation(async (path: string) => {
      if (path === '/admin/users/usr_test') return { data: {
        user_id: 'usr_test', username: 'customer_one', nickname: '顾客一号', account_status: 'active',
        registered_at: '2026-08-20T08:00:00Z', last_login_at: '2026-08-29T08:00:00Z', permission_version: 1, version: 3,
      }, headers: new Headers({ etag: '"v3"' }) }
      if (path.endsWith('/workspace')) return { data: { user_id: 'usr_test', username: 'customer_one', current_email: 'customer@example.com', presence_status: 'online', balance_minor: '1200', currency: 'CNY' } }
      if (path.endsWith('/addresses')) return { data: { items: [{
        address_id: 'addr_test', recipient_name: '张', phone: '13800000000',
        province_code: '110000', city_code: '110100', district_code: '110101',
        address: '东长安街 1 号', is_default: true, version: 2,
      }] } }
      if (path.endsWith('/favorite-products')) return { data: { items: [] } }
      if (path.endsWith('/followed-stores')) return { data: { items: [] } }
      if (path.endsWith('/cart')) return { data: { cart_id: null, groups: [], cart_total_quantity: 0, selected_quantity: 0, valid_item_count: 0, amount_summary: { selected_goods_amount: { minor_units: '0', currency: 'CNY' } }, version: 0 } }
      if (path.startsWith('/admin/orders?')) return { data: { items: [], next_cursor: null } }
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
    for (const heading of ['账号与安全', '余额与充值', '收货地址', '收藏的商品与店铺', '购物车商品', '删除用户']) {
      expect(wrapper.text()).toContain(heading)
    }
    expect(wrapper.text()).not.toContain('有效角色')
    expect(wrapper.text()).not.toContain('客服消息')
    expect(wrapper.text()).not.toContain('操作记录')
    expect(wrapper.text()).toContain('北京市 东城区 东长安街 1 号')

    await wrapper.get('.admin-address-grid .actions button').trigger('click')
    await flushPromises()
    const regionSelects = wrapper.findAll('.admin-address-region-selector select')
    expect(regionSelects).toHaveLength(3)
    expect((regionSelects[0]!.element as HTMLSelectElement).value).toBe('110000')
    expect(regionSelects[0]!.text()).toContain('北京市')
    expect(regionSelects[2]!.text()).toContain('东城区')

    await regionSelects[0]!.setValue('310000')
    await flushPromises()
    expect((wrapper.findAll('.admin-address-region-selector select')[1]!.element as HTMLSelectElement).value).toBe('')
    expect(wrapper.findAll('.admin-address-region-selector select')[1]!.text()).toContain('市辖区')

    await wrapper.get('.admin-user-orders-entry').trigger('click')
    await flushPromises()
    expect(wrapper.get('.admin-user-orders-dialog').text()).toContain('已取消订单不在管理端展示')
    wrapper.unmount()
  })
})
