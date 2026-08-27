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

const money = (minor_units: string) => ({ minor_units, currency: 'CNY' })
let pinia: ReturnType<typeof createPinia>

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
})
