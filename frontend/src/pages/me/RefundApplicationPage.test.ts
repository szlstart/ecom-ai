import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useUserAuthStore } from '@/stores/user-auth'
import RefundApplicationPage from './RefundApplicationPage.vue'

const mocks = vi.hoisted(() => ({ getMyOrder: vi.fn() }))
vi.mock('@/api/orders', async (importOriginal) => ({ ...await importOriginal<typeof import('@/api/orders')>(), getMyOrder: mocks.getMyOrder }))

const money = (minor_units: string) => ({ minor_units, currency: 'CNY' })

describe('RefundApplicationPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getMyOrder.mockResolvedValue({ data: {
      order_id: 'ord_test', trade_order_id: 'trd_test', order_source: 'buy_now', store: { store_id: 'sto_test', store_name: '测试店铺', logo_url: null },
      order_status: 'completed', payment_status: 'paid', fulfillment_status: 'received', after_sale_status: 'none', matched_views: ['completed'],
      items: [{ order_item_id: 'oit_test', product_id: 'prd_test', product_available: true, sku_id: 'sku_test', product_name: '测试商品', sku_name: '标准款', spec_snapshot: [], image_url: '/files/product.jpg', quantity: 2, unit_price: money('600'), gross_amount: money('1200'), payable_amount: money('1200'), refunded_amount: money('600'), refunded_quantity: 1, review_status: 'pending', after_sale_status: 'none' }],
      item_count: 1, total_quantity: 2, amounts: { goods_amount: money('1200'), freight_amount: money('0'), adjustment_amount: money('0'), payable_amount: money('1200'), paid_amount: money('1200'), refunded_amount: money('600') },
      address: { recipient_name: '测试', phone_masked: '***', country_code: 'CN', province_code: '110000', city_code: '110100', district_code: '110101', address: '测试地址', postal_code: null },
      buyer_remark: null, policy_snapshot: {}, events: [], created_at: '2026-09-13T00:00:00Z', expires_at: '2026-09-13T00:30:00Z', available_actions: [], version: 1,
    } })
  })

  it('fixes the refund to the remaining order items and offers useful reasons', async () => {
    const pinia = createPinia(); setActivePinia(pinia); useUserAuthStore().accessToken = 'user-token'
    const stub = defineComponent({ render: () => h('div') })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/me/orders/:orderId/refund', component: RefundApplicationPage }, { path: '/me/orders/:orderId', component: stub },
    ] })
    await router.push('/me/orders/ord_test/refund'); await router.isReady()
    const wrapper = mount(RefundApplicationPage, { global: { plugins: [pinia, router] } })
    await flushPromises()

    expect(wrapper.text()).toContain('本次申请数量：1 件')
    expect(wrapper.findAll('input[type="checkbox"]')).toHaveLength(0)
    expect(wrapper.findAll('input[type="number"]')).toHaveLength(0)
    expect(wrapper.findAll('select option')).toHaveLength(8)
    expect(wrapper.text()).toContain('与商品描述不符')
    wrapper.unmount()
  })
})
