import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import AdminStoreListPage from './AdminStoreListPage.vue'

const mocks = vi.hoisted(() => ({ adminGet: vi.fn(), adminCreate: vi.fn() }))

vi.mock('@/api/admin-catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-catalog')>(),
  adminGet: mocks.adminGet,
  adminCreate: mocks.adminCreate,
}))

function store(overrides: Record<string, unknown>) {
  return {
    store_id: 'sto_a', owner_user_id: 'usr_a', store_name: '晨光文具店', description: '办公与学习用品',
    logo_file_id: null, logo_url: null, status: 'active', suspension_source: null, rating_score: '4.80', rating_count: 10,
    follower_count: 2, sales_count: 30, product_count: 4, net_revenue: { currency: 'CNY', minor_units: '88000' },
    store_name_changed_at: null, store_name_change_available_at: null, opened_at: null, suspended_at: null, closed_at: null, version: 1,
    ...overrides,
  }
}

describe('AdminStoreListPage', () => {
  beforeEach(() => vi.clearAllMocks())

  it('searches only by store name and sorts by real operating metrics', async () => {
    mocks.adminGet.mockResolvedValue({ data: { items: [
      store({}),
      store({ store_id: 'sto_b', owner_user_id: 'usr_b', store_name: '生活杂货铺', sales_count: 8, product_count: 12, rating_score: '3.60', net_revenue: { currency: 'CNY', minor_units: '126000' } }),
    ], next_cursor: null } })
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useAdminAuthStore()
    auth.accessToken = 'admin-token'
    auth.permissions = ['stores:read', 'stores:manage']
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/admin/stores', component: AdminStoreListPage },
      { path: '/admin/stores/:storeId', component: { template: '<div />' } },
    ] })
    await router.push('/admin/stores')
    await router.isReady()
    const wrapper = mount(AdminStoreListPage, { global: { plugins: [pinia, router] } })
    await flushPromises()

    expect(wrapper.get('input[aria-label="搜索店铺名称"]').attributes('placeholder')).toBe('搜索店铺名称')
    expect(wrapper.findAll('.admin-store-card')).toHaveLength(2)
    await wrapper.get('input[aria-label="搜索店铺名称"]').setValue('sto_b')
    await wrapper.get('.admin-store-search-form').trigger('submit')
    expect(wrapper.findAll('.admin-store-card')).toHaveLength(0)
    await wrapper.get('input[aria-label="搜索店铺名称"]').setValue('生活')
    await wrapper.get('.admin-store-search-form').trigger('submit')
    expect(wrapper.findAll('.admin-store-card')).toHaveLength(1)
    expect(wrapper.text()).toContain('生活杂货铺')

    await wrapper.get('input[aria-label="搜索店铺名称"]').setValue('')
    await wrapper.get('.admin-store-search-form').trigger('submit')
    await wrapper.get('select[aria-label="店铺排序"]').setValue('revenue')
    expect(wrapper.findAll('.admin-store-card h2').map((node) => node.text())).toEqual(['生活杂货铺', '晨光文具店'])
    expect(wrapper.text()).toContain('¥1260.00')
    expect(wrapper.text()).toContain('商品数量')
    expect(wrapper.text()).toContain('评价从低到高')
    wrapper.unmount()
  })
})
