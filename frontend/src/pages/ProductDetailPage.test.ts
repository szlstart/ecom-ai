import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'

import { useUserAuthStore } from '@/stores/user-auth'

import ProductDetailPage from './ProductDetailPage.vue'

const mocks = vi.hoisted(() => ({
  createBuyNowCheckout: vi.fn(),
  getProduct: vi.fn(),
  getProductFaqs: vi.fn(),
  getProductSkus: vi.fn(),
}))

vi.mock('@/api/catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/catalog')>(),
  getProduct: mocks.getProduct,
  getProductFaqs: mocks.getProductFaqs,
  getProductSkus: mocks.getProductSkus,
}))

vi.mock('@/api/checkout', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/checkout')>(),
  createBuyNowCheckout: mocks.createBuyNowCheckout,
}))

const CheckoutPageStub = defineComponent({
  props: { checkoutId: { type: String, required: true }, embedded: Boolean },
  setup(props) {
    return () => h('div', { class: 'checkout-page-stub', 'data-checkout-id': props.checkoutId }, '结算内容')
  },
})

const product = {
  product_id: 'prd_01', product_name: '测试商品', product_status: 'on_sale', category_id: 'cat_01', brand_id: null,
  store: { store_id: 'sto_01', store_name: '测试店铺', logo_url: null, store_status: 'active', rating_score: '4.90' },
  price_range: [{ minor_units: '1200', currency: 'CNY' }, { minor_units: '1200', currency: 'CNY' }],
  sales_count: 8, review_count: 0, rating_score: '4.90', public_images: [], default_sku_id: 'sku_01',
  detail_content: null, attributes: [], origin_region_code: null,
  dispatch_estimate: { estimate_type: 'dispatch', status: 'unavailable', min_at: null, max_at: null, source: null, source_updated_at: null, calculated_at: '2026-08-28T00:00:00Z', timezone: 'Asia/Shanghai', disclaimer_code: null, unavailable_reason_code: 'NOT_CONFIGURED' },
  purchase_notice: null, fulfillment_profile_version: null, is_favorited: false,
}

const sku = {
  sku_id: 'sku_01', sku_name: '标准款', sale_price: { minor_units: '1200', currency: 'CNY' }, sku_status: 'active',
  stock_status: 'in_stock', low_stock_remaining: null, max_purchase_quantity: 10, sales_count: 8, images: [], image_fallback: 'none',
}

let mountedWrapper: VueWrapper | null = null

async function mountPage(): Promise<{ wrapper: VueWrapper; router: Router }> {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUserAuthStore().accessToken = 'user-token'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: defineComponent({ render: () => h('div') }) },
      { path: '/products/:productId', component: ProductDetailPage },
      { path: '/products/:productId/reviews', component: defineComponent({ render: () => h('div') }) },
      { path: '/stores/:storeId', component: defineComponent({ render: () => h('div') }) },
      { path: '/cart', component: defineComponent({ render: () => h('div') }) },
    ],
  })
  await router.push('/products/prd_01')
  await router.isReady()
  const wrapper = mount(ProductDetailPage, {
    attachTo: document.body,
    global: { plugins: [pinia, router], stubs: { CheckoutPage: CheckoutPageStub } },
  })
  mountedWrapper = wrapper
  await flushPromises()
  return { wrapper, router }
}

function buttonByText(wrapper: VueWrapper, text: string) {
  const button = wrapper.findAll('button').find((item) => item.text() === text)
  if (!button) throw new Error(`button not found: ${text}`)
  return button
}

describe('ProductDetailPage immediate purchase', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    document.body.innerHTML = ''
    mocks.getProduct.mockResolvedValue({ data: product })
    mocks.getProductSkus.mockResolvedValue({ data: { items: [sku] } })
    mocks.getProductFaqs.mockResolvedValue({ data: { items: [] } })
    mocks.createBuyNowCheckout.mockResolvedValue({ data: { checkout_id: 'chk_modal' } })
  })

  afterEach(() => {
    mountedWrapper?.unmount()
    mountedWrapper = null
    document.body.classList.remove('modal-open')
    document.body.innerHTML = ''
  })

  it('shows the payable total for one item and updates it with quantity', async () => {
    const { wrapper } = await mountPage()

    expect(wrapper.get('.purchase-total-card').text()).toContain('支付总额')
    expect(wrapper.get('.purchase-total-card').text()).toContain('1 件商品')
    expect(wrapper.get('.purchase-total-card').text()).toContain('¥12.00')

    await buttonByText(wrapper, '＋').trigger('click')

    expect(wrapper.get('.purchase-total-card').text()).toContain('2 件商品')
    expect(wrapper.get('.purchase-total-card').text()).toContain('¥24.00')

    await wrapper.get('input[aria-label="购买数量"]').setValue('3')

    expect(wrapper.get('.purchase-total-card').text()).toContain('3 件商品')
    expect(wrapper.get('.purchase-total-card').text()).toContain('¥36.00')
  })

  it('opens checkout in a dismissible overlay without leaving the product page', async () => {
    const { wrapper, router } = await mountPage()

    await buttonByText(wrapper, '立即购买').trigger('click')
    await flushPromises()

    expect(mocks.createBuyNowCheckout).toHaveBeenCalledWith('sku_01', 1, 'user-token')
    expect(router.currentRoute.value.fullPath).toBe('/products/prd_01')
    expect(document.querySelector('.buy-now-checkout-overlay')).not.toBeNull()
    expect(document.querySelector('.checkout-page-stub')?.getAttribute('data-checkout-id')).toBe('chk_modal')

    document.querySelector('.buy-now-checkout-overlay')?.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    await flushPromises()

    expect(document.querySelector('.buy-now-checkout-overlay')).toBeNull()
    expect(router.currentRoute.value.fullPath).toBe('/products/prd_01')
  })
})
