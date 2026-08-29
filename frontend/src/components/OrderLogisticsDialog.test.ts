import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { useUserAuthStore } from '@/stores/user-auth'

import OrderLogisticsDialog from './OrderLogisticsDialog.vue'

const mocks = vi.hoisted(() => ({
  listOrderShipments: vi.fn(),
  getShipment: vi.fn(),
}))

vi.mock('@/api/logistics', () => ({
  listOrderShipments: mocks.listOrderShipments,
  getShipment: mocks.getShipment,
}))

describe('OrderLogisticsDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    const pinia = createPinia()
    setActivePinia(pinia)
    useUserAuthStore().accessToken = 'user-token'
    mocks.listOrderShipments.mockResolvedValue({
      data: { order_id: 'ord_test', items: [{ shipment_id: 'shp_test' }] },
    })
    mocks.getShipment.mockResolvedValue({
      data: {
        shipment_id: 'shp_test',
        order_id: 'ord_test',
        carrier_code: 'fake_express',
        carrier_name: 'Ecom 速运',
        tracking_no: 'ECOMTEST123456',
        tracking_no_masked: '**********3456',
        shipment_status: 'in_transit',
        items: [{ order_item_id: 'oit_test', product_name: '测试商品', sku_name: '绿色款', quantity: 1 }],
        delivery_estimate: { type: 'delivery', status: 'available', min_at: '2026-08-28T08:00:20Z', max_at: '2026-08-28T08:00:25Z', source: 'carrier', updated_at: '2026-08-28T08:00:00Z', disclaimer: '' },
        latest_tracks: [
          { track_status: 'picked_up', provider_status: 'WAITING_PICKUP', description: '已发货，待揽收', location_text: '310000', occurred_at: '2026-08-28T08:00:05Z', received_at: '2026-08-28T08:00:05Z' },
          { track_status: 'in_transit', provider_status: 'PICKED_UP', description: '已揽收，开始运输', location_text: '310000', occurred_at: '2026-08-28T08:00:10Z', received_at: '2026-08-28T08:00:10Z' },
          { track_status: 'in_transit', provider_status: 'OUT_FOR_DELIVERY', description: '正在派送中…', location_text: '440106', occurred_at: '2026-08-28T08:00:15Z', received_at: '2026-08-28T08:00:15Z' },
        ],
        route: { origin_region_code: '310000', country_code: 'CN', province_code: '440000', city_code: '440100', district_code: '440106', destination_address: '体育西路 1 号' },
        shipped_at: '2026-08-28T08:00:00Z',
        last_synced_at: '2026-08-28T08:00:15Z',
        version: 3,
      },
    })
  })

  afterEach(() => { document.body.innerHTML = '' })

  it('renders a realistic persisted route and closes from the blank backdrop', async () => {
    const wrapper = mount(OrderLogisticsDialog, { props: { orderId: 'ord_test' } })
    await flushPromises()

    expect(document.body.textContent).toContain('Ecom 速运')
    expect(document.body.textContent).toContain('ECOMTEST123456')
    expect(document.body.textContent).toContain('正在派送中…')
    expect(document.body.textContent).toContain('天河区')
    expect(document.body.textContent).toContain('测试商品（绿色款）×1')
    expect(mocks.listOrderShipments).toHaveBeenCalledWith('ord_test', 'user-token')

    document.querySelector<HTMLElement>('.logistics-overlay')!.dispatchEvent(
      new MouseEvent('mousedown', { bubbles: true }),
    )
    await flushPromises()
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })
})
