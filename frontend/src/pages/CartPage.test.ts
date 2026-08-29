import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useUserAuthStore } from '@/stores/user-auth'

import CartPage from './CartPage.vue'

const mocks = vi.hoisted(() => ({
  createCartCheckout: vi.fn(),
  getCart: vi.fn(),
}))

vi.mock('@/api/cart', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/cart')>(),
  getCart: mocks.getCart,
}))

vi.mock('@/api/checkout', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/checkout')>(),
  createCartCheckout: mocks.createCartCheckout,
}))

const CheckoutPageStub = defineComponent({
  props: { checkoutId: { type: String, required: true }, embedded: Boolean },
  emits: ['cartChanged'],
  setup(props, { emit }) {
    return () => h('button', {
      class: 'checkout-page-stub',
      'data-checkout-id': props.checkoutId,
      onClick: () => emit('cartChanged'),
    }, '结算内容')
  },
})

const cart = {
  cart_id: 'cart_01', cart_total_quantity: 2, selected_quantity: 2, valid_item_count: 1, version: 1,
  amount_summary: { selected_goods_amount: { minor_units: '1200', currency: 'CNY' } },
  groups: [{
    store_id: 'sto_01', store_name: '文具专卖店', store_logo_url: null, selected_quantity: 2,
    selected_amount: { minor_units: '1200', currency: 'CNY' },
    items: [{
      cart_item_id: 'ci_01', product_id: 'prd_01', sku_id: 'sku_01', product_name: '2B铅笔', sku_name: '6支', image_url: null,
      quantity: 2, is_selected: true, added_price: { minor_units: '600', currency: 'CNY' }, current_price: { minor_units: '600', currency: 'CNY' },
      price_changed: false, available_quantity: 10, is_valid: true, invalid_reason: null,
    }],
  }],
}

let wrapper: VueWrapper | null = null

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUserAuthStore().accessToken = 'user-token'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/cart', component: CartPage },
      { path: '/search', component: defineComponent({ render: () => h('div') }) },
      { path: '/stores/:storeId', component: defineComponent({ render: () => h('div') }) },
      { path: '/products/:productId', component: defineComponent({ render: () => h('div') }) },
    ],
  })
  await router.push('/cart')
  await router.isReady()
  wrapper = mount(CartPage, {
    attachTo: document.body,
    global: { plugins: [pinia, router], stubs: { CheckoutPage: CheckoutPageStub } },
  })
  await flushPromises()
  return { wrapper, router }
}

describe('CartPage checkout dialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    mocks.getCart.mockResolvedValue({ data: structuredClone(cart) })
    mocks.createCartCheckout.mockResolvedValue({ data: { checkout_id: 'chk_cart_modal' } })
  })

  afterEach(() => {
    wrapper?.unmount()
    wrapper = null
    document.body.classList.remove('modal-open')
    document.body.innerHTML = ''
  })

  it('opens checkout over the cart and closes on the backdrop without navigating away', async () => {
    const mounted = await mountPage()
    const checkoutButton = mounted.wrapper.findAll('button').find((item) => item.text() === '去结算')
    expect(checkoutButton).toBeDefined()

    await checkoutButton!.trigger('click')
    await flushPromises()

    expect(mocks.createCartCheckout).toHaveBeenCalledWith(['ci_01'], 'user-token')
    expect(mounted.router.currentRoute.value.fullPath).toBe('/cart')
    expect(document.querySelector('.buy-now-checkout-overlay')).not.toBeNull()
    expect(document.querySelector('.checkout-page-stub')?.getAttribute('data-checkout-id')).toBe('chk_cart_modal')

    document.querySelector('.buy-now-checkout-overlay')?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await flushPromises()

    expect(document.querySelector('.buy-now-checkout-overlay')).toBeNull()
    expect(mounted.router.currentRoute.value.fullPath).toBe('/cart')
  })

  it('reloads the cart after a quantity is changed in the checkout dialog', async () => {
    const mounted = await mountPage()
    await mounted.wrapper.findAll('button').find((item) => item.text() === '去结算')!.trigger('click')
    await flushPromises()
    ;(document.querySelector('.checkout-page-stub') as HTMLButtonElement).click()
    document.querySelector('.buy-now-checkout-close')?.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flushPromises()

    expect(mocks.getCart).toHaveBeenCalledTimes(2)
  })
})
