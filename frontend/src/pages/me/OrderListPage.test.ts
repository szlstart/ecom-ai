import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useUserAuthStore } from '@/stores/user-auth'
import OrderListPage from './OrderListPage.vue'

const mocks = vi.hoisted(() => ({ listMyOrders: vi.fn() }))
vi.mock('@/api/orders', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/orders')>(),
  listMyOrders: mocks.listMyOrders,
}))
vi.mock('@/api/logistics', () => ({ listOrderShipments: vi.fn() }))

const money = (minor_units: string) => ({ minor_units, currency: 'CNY' })

describe('OrderListPage actions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    const pinia = createPinia()
    setActivePinia(pinia)
    useUserAuthStore().accessToken = 'user-token'
    mocks.listMyOrders.mockResolvedValue({ data: { items: [{
      order_id: 'ord_test', trade_order_id: 'trd_test', order_source: 'buy_now', store: { store_id: 'sto_test', store_name: '测试店铺', logo_url: null },
      order_status: 'completed', payment_status: 'paid', fulfillment_status: 'received', after_sale_status: 'none', matched_views: ['completed', 'pending_review'],
      items: [{ order_item_id: 'oit_test', product_id: 'prd_test', product_available: true, sku_id: 'sku_test', product_name: '测试商品', sku_name: '标准款', spec_snapshot: [], image_url: null, quantity: 1, unit_price: money('600'), gross_amount: money('600'), payable_amount: money('600'), refunded_amount: money('0'), refunded_quantity: 0, review_status: 'pending', after_sale_status: 'none' }],
      item_count: 1, total_quantity: 1, amounts: { goods_amount: money('600'), freight_amount: money('0'), adjustment_amount: money('0'), payable_amount: money('600'), paid_amount: money('600'), refunded_amount: money('0') },
      created_at: '2026-09-13T00:00:00Z', expires_at: '2026-09-13T00:30:00Z', version: 1,
      available_actions: [{ code: 'review', enabled: true, reason_code: null, reason_message: null, requires_confirmation: false, target: { type: 'route', name: 'my-review-create', params: { orderItemId: 'oit_test' } } }],
    }] }, meta: {} })
  })

  it('navigates to the review editor when the review action is clicked', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useUserAuthStore().accessToken = 'user-token'
    const stub = defineComponent({ render: () => h('div', '评价页面') })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/me/orders', component: OrderListPage },
      { path: '/me/orders/:orderId', component: stub },
      { path: '/me/order-items/:orderItemId/review', name: 'my-review-create', component: stub },
      { path: '/stores/:storeId', component: stub },
    ] })
    await router.push('/me/orders?view=pending_review')
    await router.isReady()
    const wrapper = mount(OrderListPage, { global: { plugins: [pinia, router], stubs: { OrderLogisticsDialog: true } } })
    await flushPromises()

    await wrapper.findAll('button').find((button) => button.text() === '评价')!.trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/me/order-items/oit_test/review')
    wrapper.unmount()
  })
})
