import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useAdminAuthStore } from '@/stores/admin-auth'

import MerchantOrderListPage from './MerchantOrderListPage.vue'

const mocks = vi.hoisted(() => ({
  adminGet: vi.fn(),
  apiRequest: vi.fn(),
  listOrders: vi.fn(),
  getOrder: vi.fn(),
  createShipment: vi.fn(),
  listRefunds: vi.fn(),
}))

vi.mock('@/api/admin-catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-catalog')>(),
  adminGet: mocks.adminGet,
}))
vi.mock('@/api/http', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/http')>(),
  apiRequest: mocks.apiRequest,
}))
vi.mock('@/api/orders', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/orders')>(),
  listAdminOrders: mocks.listOrders,
  getAdminOrder: mocks.getOrder,
}))
vi.mock('@/api/logistics', () => ({ createAdminShipment: mocks.createShipment }))
vi.mock('@/api/admin-after-sales', () => ({ listAdminRefunds: mocks.listRefunds }))

const money = (minor_units: string) => ({ minor_units, currency: 'CNY' })
let pinia: ReturnType<typeof createPinia>

function shippingOrder() {
  const order = {
    order_id: 'ord_partial', trade_order_id: 'trd_partial', order_source: 'buy_now', store: { store_id: 'sto_test', store_name: '我的店铺', logo_url: null },
    order_status: 'pending_shipment', payment_status: 'paid', fulfillment_status: 'partial', after_sale_status: 'none', matched_views: ['pending_shipment'],
    items: [{ order_item_id: 'oit_partial', product_id: 'prd_test', product_available: true, sku_id: 'sku_test', product_name: '分批发货商品', sku_name: '标准款', spec_snapshot: [], image_url: null, quantity: 5, unit_price: money('1000'), gross_amount: money('5000'), payable_amount: money('5000'), refunded_amount: money('1000'), refunded_quantity: 1, review_status: 'closed', after_sale_status: 'none' }],
    item_count: 1, total_quantity: 5, amounts: { goods_amount: money('5000'), freight_amount: money('0'), adjustment_amount: money('0'), payable_amount: money('5000'), paid_amount: money('5000'), refunded_amount: money('1000') },
    created_at: '2026-08-27T00:00:00Z', expires_at: '2026-08-27T01:00:00Z', available_actions: [], version: 3,
  }
  return { order, user_id: 'usr_test', user_name_masked: '顾*客', shippable_quantities: { oit_partial: 2 }, available_admin_actions: ['create_shipment'], events: [] }
}

describe('MerchantOrderListPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    pinia = createPinia()
    setActivePinia(pinia)
    useAdminAuthStore().accessToken = 'merchant-token'
    mocks.adminGet.mockResolvedValue({ data: { items: [{ store_id: 'sto_test', store_name: '我的店铺' }], next_cursor: null } })
    mocks.apiRequest.mockResolvedValue({ data: {
      gross_sales: money('12000'), refunded_amount: money('2000'), net_revenue: money('10000'),
      today_revenue: money('3000'), yesterday_revenue: money('2000'), last_30_days_revenue: money('9000'),
      all_order_count: 8, completed_order_count: 3, pending_payment_count: 1,
      pending_shipment_count: 1, in_transit_count: 1, after_sale_pending_count: 1, cancelled_count: 1,
    } })
    mocks.listOrders.mockResolvedValue({ data: { items: [] } })
    mocks.listRefunds.mockResolvedValue({ data: { items: [] } })
  })

  it('displays receipt-settled revenue and queries merchant order status tabs', async () => {
    const wrapper = mount(MerchantOrderListPage, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('总营业额')
    expect(wrapper.text()).toContain('¥100.00')
    expect(wrapper.text()).toContain('顾客确认收货后才计入营业额')
    expect(mocks.listOrders).toHaveBeenCalledWith({}, 'merchant-token')

    await wrapper.findAll('.merchant-order-tabs button')[4]!.trigger('click')
    await flushPromises()
    expect(mocks.listOrders).toHaveBeenLastCalledWith({ order_status: 'completed' }, 'merchant-token')

    await wrapper.findAll('.merchant-order-tabs button')[5]!.trigger('click')
    await flushPromises()
    expect(mocks.listOrders).toHaveBeenLastCalledWith({ after_sale_status: 'in_progress' }, 'merchant-token')
    wrapper.unmount()
  })

  it('uses server-calculated remaining quantities for a partial shipment', async () => {
    const item = shippingOrder()
    mocks.listOrders.mockResolvedValue({ data: { items: [item], next_cursor: null } })
    mocks.getOrder.mockResolvedValue({ data: item, headers: new Headers({ etag: '"v3"' }) })
    const wrapper = mount(MerchantOrderListPage, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.get('.merchant-order-card button').trigger('click')
    await flushPromises()
    const quantity = document.body.querySelector<HTMLInputElement>('.merchant-shipment-dialog input[type="number"]')
    expect(quantity?.max).toBe('2')
    expect(quantity?.value).toBe('2')
    expect(document.body.textContent).toContain('最多 2 件')
    wrapper.unmount()
  })
})
