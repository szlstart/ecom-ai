import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useUserAuthStore } from '@/stores/user-auth'

import CheckoutPage from './CheckoutPage.vue'

const mocks = vi.hoisted(() => ({
  createOrder: vi.fn(),
  getCheckout: vi.fn(),
  listAddresses: vi.fn(),
  patchCheckout: vi.fn(),
  repriceCheckout: vi.fn(),
}))

vi.mock('@/api/checkout', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/checkout')>(),
  createOrder: mocks.createOrder,
  getCheckout: mocks.getCheckout,
  listAddresses: mocks.listAddresses,
  patchCheckout: mocks.patchCheckout,
  repriceCheckout: mocks.repriceCheckout,
}))

vi.mock('@/api/messaging', () => ({
  ensureStoreConversation: vi.fn(),
  setConversationContext: vi.fn(),
}))

const address = {
  address_id: 'addr_01', recipient_name: '宋子龙', phone_masked: '176****5523',
  province_code: '320000', city_code: '320300', district_code: '320321',
  address: '河滨嘉苑14-1', is_default: true,
}

function checkout(overrides: Record<string, unknown> = {}) {
  return {
    checkout_id: 'chk_test', source_type: 'buy_now', status: 'active', address_id: address.address_id,
    expires_at: '2026-08-29T12:00:00Z',
    store_groups: [{
      store_id: 'sto_01', store_name: '文具专卖店',
      items: [{ product_id: 'prd_01', sku_id: 'sku_01', product_name: '2B铅笔', sku_name: '6支', quantity: 2, unit_price: { minor_units: '600', currency: 'CNY' }, subtotal: { minor_units: '1200', currency: 'CNY' }, available_quantity: 10 }],
      goods_amount: { minor_units: '1200', currency: 'CNY' }, freight_amount: { minor_units: '0', currency: 'CNY' },
      delivery_options: [], selected_delivery_option: null, buyer_remark: null, policy_versions: {}, customer_service_context: {},
    }],
    amounts: { goods_amount: { minor_units: '1200', currency: 'CNY' }, freight_amount: { minor_units: '0', currency: 'CNY' }, payable_amount: { minor_units: '1200', currency: 'CNY' } },
    warnings: [], blocking_issues: [], available_actions: ['change_address', 'reprice', 'create_order'],
    pricing_version: 'pricing_v2_free_shipping', version: 2,
    ...overrides,
  }
}

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUserAuthStore().accessToken = 'user-token'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/checkout/:checkoutId', component: CheckoutPage },
      { path: '/cart', component: defineComponent({ render: () => h('div', '购物车') }) },
      { path: '/me/addresses', component: defineComponent({ render: () => h('div', '地址') }) },
      { path: '/stores/:storeId', component: defineComponent({ render: () => h('div') }) },
      { path: '/products/:productId', component: defineComponent({ render: () => h('div') }) },
      { path: '/pay/:tradeId', component: defineComponent({ render: () => h('div') }) },
    ],
  })
  await router.push('/checkout/chk_test')
  await router.isReady()
  const wrapper = mount(CheckoutPage, { global: { plugins: [pinia, router] } })
  await flushPromises()
  return wrapper
}

describe('CheckoutPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.listAddresses.mockResolvedValue({ data: { items: [address] } })
    mocks.patchCheckout.mockResolvedValue({ data: checkout() })
    mocks.repriceCheckout.mockResolvedValue({ data: checkout() })
  })

  it('automatically repairs a stale address and delivery snapshot without a manual refresh button', async () => {
    mocks.getCheckout.mockResolvedValue({ data: checkout({
      pricing_version: 'pricing_v1',
      blocking_issues: [
        { code: 'ADDRESS_REQUIRED', message: '请选择有效的收货地址。', store_id: null, sku_id: null },
        { code: 'DELIVERY_UNAVAILABLE', message: '当前地址暂无可用配送方式。', store_id: 'sto_01', sku_id: null },
      ],
      available_actions: ['change_address', 'reprice'],
      version: 1,
    }) })

    const wrapper = await mountPage()

    expect(mocks.repriceCheckout).toHaveBeenCalledWith('chk_test', 'user-token')
    expect(wrapper.text()).toContain('配送方式：邮寄')
    expect(wrapper.text()).toContain('包邮')
    expect(wrapper.text()).not.toContain('刷新价格与库存')
    expect(wrapper.text()).not.toContain('暂时无法提交订单')
    expect(wrapper.get('input[type="radio"]').element).toHaveProperty('checked', true)
  })

  it('automatically binds the default address when the checkout was created without one', async () => {
    mocks.getCheckout.mockResolvedValue({ data: checkout({ address_id: null, version: 1 }) })

    await mountPage()

    expect(mocks.patchCheckout).toHaveBeenCalledWith(
      'chk_test', { address_id: address.address_id }, 1, 'user-token',
    )
  })
})
