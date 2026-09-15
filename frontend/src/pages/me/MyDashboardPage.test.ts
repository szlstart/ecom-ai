import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useUserAuthStore } from '@/stores/user-auth'

import MyDashboardPage from './MyDashboardPage.vue'

const mocks = vi.hoisted(() => ({ apiRequest: vi.fn(), getReadinessHealth: vi.fn() }))

vi.mock('@/api/http', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/http')>(),
  apiRequest: mocks.apiRequest,
}))

vi.mock('@/api/health', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/health')>(),
  getReadinessHealth: mocks.getReadinessHealth,
}))

describe('MyDashboardPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.apiRequest.mockImplementation((path: string) => Promise.resolve({ data: path === '/users/me/dashboard'
      ? {
          order_counts: { pending_payment: 1, pending_shipment: 2, in_transit: 3, pending_review: 4, after_sale: 0 },
          review_counts: {},
          default_address: { recipient_name: '测试用户', address: '上海市浦东新区测试路 1 号' },
          unread_message_count: 2,
          unavailable_sections: [],
        }
      : { balance: { minor_units: '6600', currency: 'CNY' }, total_recharged: { minor_units: '6600', currency: 'CNY' } } }))
    mocks.getReadinessHealth.mockResolvedValue({
      status: 'ready',
      dependencies: { agent_runtime: { status: 'up' }, agent_model: { status: 'up' } },
    })
  })

  it('makes every single-destination account module clickable as a complete surface', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const auth = useUserAuthStore()
    auth.accessToken = 'user-token'
    auth.user = { user_id: 'usr_test', username: 'tester', nickname: '测试用户', avatar_url: null, account_status: 'active' }
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/me', component: MyDashboardPage },
        { path: '/me/orders', component: { template: '<div />' } },
        { path: '/me/addresses', component: { template: '<div />' } },
        { path: '/me/favorites/products', component: { template: '<div />' } },
        { path: '/me/favorites/stores', component: { template: '<div />' } },
        { path: '/me/settings/security', component: { template: '<div />' } },
        { path: '/me/wallet', component: { template: '<div />' } },
      ],
    })
    await router.push('/me')
    await router.isReady()

    const wrapper = mount(MyDashboardPage, { global: { plugins: [pinia, router] } })
    await flushPromises()

    const cards = wrapper.findAll('.dashboard-sections .navigation-surface')
    expect(cards).toHaveLength(4)
    expect(cards.map((card) => card.get('.surface-primary-link').attributes('href'))).toEqual([
      '/me/orders?view=all',
      '/me/addresses',
      '/me/favorites/products',
      '/me/settings/security',
    ])
    expect(wrapper.get('.my-wallet-panel').classes()).toContain('navigation-surface')
    expect(wrapper.get('.my-wallet-panel .surface-primary-link').attributes('href')).toBe('/me/wallet')
    expect(wrapper.findAll('.my-order-shortcuts a')).toHaveLength(5)
    expect(wrapper.findAll('.dashboard-quick-links a')).toHaveLength(2)
    wrapper.unmount()
  })
})
