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

function cartCheckout() {
  return checkout({
    source_type: 'cart',
    store_groups: [{
      store_id: 'sto_01', store_name: '文具专卖店',
      items: [
        { cart_item_id: 'ci_01', product_id: 'prd_01', sku_id: 'sku_01', product_name: '2B铅笔', sku_name: '6支', quantity: 2, unit_price: { minor_units: '600', currency: 'CNY' }, subtotal: { minor_units: '1200', currency: 'CNY' }, available_quantity: 10 },
        { cart_item_id: 'ci_02', product_id: 'prd_02', sku_id: 'sku_02', product_name: '笔记本', sku_name: 'A5', quantity: 1, unit_price: { minor_units: '800', currency: 'CNY' }, subtotal: { minor_units: '800', currency: 'CNY' }, available_quantity: 5 },
      ],
      goods_amount: { minor_units: '2000', currency: 'CNY' }, freight_amount: { minor_units: '0', currency: 'CNY' },
      delivery_options: [], selected_delivery_option: null, buyer_remark: null, policy_versions: {}, customer_service_context: {},
    }],
    amounts: { goods_amount: { minor_units: '2000', currency: 'CNY' }, freight_amount: { minor_units: '0', currency: 'CNY' }, payable_amount: { minor_units: '2000', currency: 'CNY' } },
    available_actions: ['change_address', 'change_item_quantities', 'reprice', 'create_order'],
  })
}

async function mountPage(embedded = false) {
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
  const wrapper = mount(CheckoutPage, { props: embedded ? { checkoutId: 'chk_test', embedded: true } : {}, global: { plugins: [pinia, router] } })
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

  it('updates buy-now quantity and payable amount inside the embedded checkout', async () => {
    const changed = checkout()
    changed.store_groups[0]!.items[0]!.quantity = 3
    changed.store_groups[0]!.items[0]!.subtotal = { minor_units: '1800', currency: 'CNY' }
    changed.store_groups[0]!.goods_amount = { minor_units: '1800', currency: 'CNY' }
    changed.amounts = {
      goods_amount: { minor_units: '1800', currency: 'CNY' },
      freight_amount: { minor_units: '0', currency: 'CNY' },
      payable_amount: { minor_units: '1800', currency: 'CNY' },
    }
    changed.version = 3
    mocks.getCheckout.mockResolvedValue({ data: checkout() })
    mocks.patchCheckout.mockResolvedValue({ data: changed })

    const wrapper = await mountPage(true)
    await wrapper.get('button[aria-label="增加结算数量"]').trigger('click')
    await flushPromises()

    expect(mocks.patchCheckout).toHaveBeenCalledWith('chk_test', { quantity: 3 }, 2, 'user-token')
    expect((wrapper.get('input[aria-label="结算购买数量"]').element as HTMLInputElement).value).toBe('3')
    expect(wrapper.get('.checkout-embedded-total').text()).toContain('¥18.00')
    expect(wrapper.emitted('quantityChanged')).toEqual([[3]])
  })

  it('keeps the merchant remark collapsed until the user opens more', async () => {
    mocks.getCheckout.mockResolvedValue({ data: checkout() })

    const wrapper = await mountPage(true)
    const details = wrapper.get('.checkout-remark-details')

    expect(details.attributes('open')).toBeUndefined()
    expect(details.get('summary').text()).toContain('给商家留言')
    expect(details.get('summary').text()).toContain('（更多）')
  })

  it('updates each cart item quantity and the payable total inside the embedded checkout', async () => {
    const changed = cartCheckout()
    changed.store_groups[0]!.items[1]!.quantity = 2
    changed.store_groups[0]!.items[1]!.subtotal = { minor_units: '1600', currency: 'CNY' }
    changed.store_groups[0]!.goods_amount = { minor_units: '2800', currency: 'CNY' }
    changed.amounts = {
      goods_amount: { minor_units: '2800', currency: 'CNY' },
      freight_amount: { minor_units: '0', currency: 'CNY' },
      payable_amount: { minor_units: '2800', currency: 'CNY' },
    }
    changed.version = 3
    mocks.getCheckout.mockResolvedValue({ data: cartCheckout() })
    mocks.patchCheckout.mockResolvedValue({ data: changed })

    const wrapper = await mountPage(true)
    await wrapper.get('button[aria-label="增加笔记本数量"]').trigger('click')
    await flushPromises()

    expect(mocks.patchCheckout).toHaveBeenCalledWith(
      'chk_test',
      { item_quantities: [{ cart_item_id: 'ci_02', quantity: 2 }] },
      2,
      'user-token',
    )
    expect((wrapper.get('input[aria-label="笔记本结算数量"]').element as HTMLInputElement).value).toBe('2')
    expect(wrapper.get('.checkout-summary-bar').text()).toContain('¥28.00')
    expect(wrapper.emitted('cartChanged')).toEqual([[]])
  })
})
