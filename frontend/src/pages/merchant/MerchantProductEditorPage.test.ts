import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import MerchantProductEditorPage from './MerchantProductEditorPage.vue'

const mocks = vi.hoisted(() => ({
  adminGet: vi.fn(),
  adminCommand: vi.fn(),
  adminCreate: vi.fn(),
  adminReplace: vi.fn(),
  adminUpdate: vi.fn(),
  getCategories: vi.fn(),
  listAdminReviews: vi.fn(),
  pastedFile: null as File | null,
  persistedImages: [] as Array<Record<string, unknown>>,
}))

vi.mock('@/api/admin-catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-catalog')>(),
  adminGet: mocks.adminGet,
  adminCommand: mocks.adminCommand,
  adminCreate: mocks.adminCreate,
  adminReplace: mocks.adminReplace,
  adminUpdate: mocks.adminUpdate,
}))
vi.mock('@/api/catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/catalog')>(),
  getCategories: mocks.getCategories,
}))
vi.mock('@/api/admin-reviews', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-reviews')>(),
  listAdminReviews: mocks.listAdminReviews,
}))

const product = {
  product_id: 'prd_test', store_id: 'sto_test', store_name: '测试店铺', category_id: 'cat_test', category_name: '内部分类', brand_id: null, brand_name: null,
  product_name: '剪贴板测试商品', subtitle: null, description: null, status: 'draft', min_price: '10.00', max_price: '10.00', currency: 'CNY', cover_image_url: null,
  sku_count: 1, available_quantity: 0, sales_count: 0, review_count: 0, rating_score: '0.00', updated_at: '2026-08-27T00:00:00Z', version: 1,
  default_sku_id: 'sku_test', current_detail_content_version_id: null, published_detail_content_version_id: null,
  completeness: { basic: true, sku: true, main_image: false, attributes: false, fulfillment: false, detail_content: false, missing_requirements: ['商品图片'] },
  available_actions: [], published_at: null, off_shelf_at: null,
}

const store = {
  store_id: 'sto_test', owner_user_id: 'usr_merchant', store_name: '测试店铺', description: null, logo_file_id: null, logo_url: null,
  status: 'active', rating_score: '0.00', rating_count: 0, follower_count: 0, sales_count: 0,
  store_name_changed_at: null, store_name_change_available_at: null, opened_at: null, suspended_at: null, closed_at: null, version: 1,
}

const sku = {
  sku_id: 'sku_test', product_id: 'prd_test', merchant_sku_code: null, sku_name: '黑色款', spec_values: [{ name: '颜色', value: '黑色' }],
  sale_price: '10.00', market_price: '10.00', currency: 'CNY', weight_grams: null, barcode: null, status: 'active', version: 1,
}

const inventory = {
  sku_id: 'sku_test', sku_name: '黑色款', product_id: 'prd_test', product_name: '剪贴板测试商品', store_id: 'sto_test', store_name: '测试店铺',
  on_hand_quantity: 8, reserved_quantity: 1, safety_stock_quantity: 0, available_quantity: 7, sold_quantity: 0, status: 'active', last_reconciled_at: null, version: 1,
}

const FileUploadStub = defineComponent({
  emits: ['uploaded'],
  setup(_props, { emit, expose }) {
    expose({
      async uploadFile(file: File) {
        mocks.pastedFile = file
        emit('uploaded', 'fil_clipboard')
      },
    })
    return () => h('div', { class: 'file-upload-stub' }, '从本地选择图片')
  },
})

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useAdminAuthStore().accessToken = 'merchant-token'
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/merchant/products/:productId', component: MerchantProductEditorPage }],
  })
  await router.push('/merchant/products/prd_test')
  await router.isReady()
  const wrapper = mount(MerchantProductEditorPage, {
    global: { plugins: [pinia, router], stubs: { AdminFileUpload: FileUploadStub } },
  })
  await flushPromises()
  return wrapper
}

function pasteEvent(items: Array<{ kind: string; type: string; getAsFile: () => File | null }>) {
  const event = new Event('paste', { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'clipboardData', { value: { items } })
  return event
}

describe('MerchantProductEditorPage clipboard image upload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.pastedFile = null
    mocks.persistedImages = []
    mocks.adminCreate.mockImplementation(async (path: string) => {
      if (path.endsWith('/shipping-templates')) return { data: { template_id: 'sht_default', version: 1 } }
      return { data: {} }
    })
    mocks.adminCommand.mockResolvedValue({ data: { template_id: 'sht_default', status: 'effective', version: 2 } })
    mocks.adminUpdate.mockResolvedValue({ data: sku })
    mocks.adminReplace.mockImplementation(async (path: string, payload: { items?: Array<Record<string, unknown>> }) => {
      if (path.endsWith('/images')) {
        mocks.persistedImages = (payload.items ?? []).map((item) => ({ ...item, image_url: `/api/v1/files/${item.file_id}`, width: 100, height: 100, status: 'active' }))
        return { data: mocks.persistedImages }
      }
      return { data: {} }
    })
    mocks.getCategories.mockResolvedValue({ data: [] })
    mocks.listAdminReviews.mockResolvedValue({ data: { items: [], next_cursor: null } })
    mocks.adminGet.mockImplementation(async (path: string) => {
      if (path === '/admin/stores?limit=20') return { data: { items: [store], next_cursor: null } }
      if (path === '/admin/products/prd_test') return { data: product }
      if (path.endsWith('/skus')) return { data: [sku] }
      if (path.endsWith('/images')) return { data: mocks.persistedImages }
      if (path.endsWith('/attributes') || path.endsWith('/faqs')) return { data: [] }
      if (path.startsWith('/admin/inventories?')) return { data: { items: [inventory] } }
      if (path.endsWith('/fulfillment-profile')) return { data: null }
      if (path.endsWith('/shipping-templates')) return { data: [] }
      throw new Error(`unexpected path: ${path}`)
    })
  })

  it('uploads a pasted image through the existing upload control and keeps local upload available', async () => {
    const wrapper = await mountPage()
    const source = new File(['png'], '', { type: 'image/png' })
    wrapper.get('.merchant-paste-image-zone').element.dispatchEvent(pasteEvent([
      { kind: 'file', type: 'image/png', getAsFile: () => source },
    ]))
    await flushPromises()

    expect(mocks.pastedFile?.name).toMatch(/^clipboard-\d+\.png$/)
    expect(wrapper.text()).toContain('图片已上传并自动保存，刷新页面也不会丢失')
    expect(mocks.adminReplace).toHaveBeenCalledWith(
      '/admin/products/prd_test/images',
      expect.objectContaining({ items: [expect.objectContaining({ file_id: 'fil_clipboard', image_type: 'main', sku_id: null })] }),
      'merchant-token',
      1,
    )
    expect(wrapper.text()).toContain('从本地选择图片')
    expect(wrapper.find('.merchant-product-editor').exists()).toBe(true)

    wrapper.unmount()
    const refreshed = await mountPage()
    expect(refreshed.find('.merchant-main-image img').exists()).toBe(true)
  })

  it('shows a local explanation and keeps the editor visible when no image was copied', async () => {
    const wrapper = await mountPage()
    wrapper.get('.merchant-paste-image-zone').element.dispatchEvent(pasteEvent([
      { kind: 'string', type: 'text/plain', getAsFile: () => null },
    ]))
    await flushPromises()

    expect(wrapper.text()).toContain('剪贴板中没有图片')
    expect(wrapper.find('.merchant-product-info-editor').exists()).toBe(true)
  })

  it('shows only the direct style fields and keeps stock beside its style', async () => {
    const wrapper = await mountPage()

    expect(wrapper.text()).not.toContain('一句话卖点')
    expect(wrapper.text()).not.toContain('商品简介')
    expect(wrapper.text()).not.toContain('划线价')
    expect(wrapper.text()).not.toContain('选填信息')
    expect(wrapper.text()).not.toContain('运费模板')
    expect(wrapper.text()).toContain('库存 8 · 可售 7')
    expect(wrapper.text()).toContain('发货地')

    await wrapper.get('.merchant-style-picker button[type="button"]').trigger('click')
    expect(wrapper.get('.merchant-simple-sku-fields').text()).toContain('款式名称')
    expect(wrapper.get('.merchant-simple-sku-fields').text()).toContain('价格（元）')
    expect(wrapper.get('.merchant-simple-sku-fields').text()).toContain('库存')
    expect(wrapper.find('.merchant-inline-stock').exists()).toBe(false)
    expect(wrapper.find('.merchant-spec-editor').exists()).toBe(false)
  })

  it('saves style name, price and stock from the same form', async () => {
    const wrapper = await mountPage()
    await wrapper.get('.merchant-style-picker > div > button').trigger('click')
    await wrapper.get('.merchant-simple-sku-fields input[placeholder]').setValue('曜石黑')
    const numberInputs = wrapper.findAll<HTMLInputElement>('.merchant-simple-sku-fields input[type="number"]')
    await numberInputs[0]!.setValue('12.50')
    await numberInputs[1]!.setValue('10')
    await wrapper.get('.merchant-inline-sku-form').trigger('submit')
    await flushPromises()

    expect(mocks.adminUpdate).toHaveBeenCalledWith(
      '/admin/products/prd_test/skus/sku_test',
      expect.objectContaining({
        sku_name: '曜石黑',
        spec_values: [{ name: '款式', value: '曜石黑' }],
        sale_price_amount: 1250,
        market_price_amount: 1250,
      }),
      'merchant-token',
      1,
    )
    expect(mocks.adminCreate).toHaveBeenCalledWith(
      '/admin/inventory-adjustments',
      expect.objectContaining({ sku_id: 'sku_test', on_hand_delta: 2, expected_version: 1 }),
      'merchant-token',
      'merchant-stock-adjust',
    )
  })

  it('creates the hidden default delivery configuration when only a shipping origin is entered', async () => {
    const wrapper = await mountPage()
    await wrapper.get('.merchant-origin-field select').setValue('110000')
    await wrapper.findAll('.merchant-detail-editors > form')[1]!.trigger('submit')
    await flushPromises()

    expect(mocks.adminCreate).toHaveBeenCalledWith(
      '/admin/stores/sto_test/shipping-templates',
      expect.objectContaining({ template_name: '系统默认配送', charge_mode: 'fixed' }),
      'merchant-token',
      'merchant-default-shipping-create',
    )
    expect(mocks.adminCommand).toHaveBeenCalledWith(
      '/admin/stores/sto_test/shipping-templates/sht_default/publications',
      expect.any(Object),
      'merchant-token',
      1,
      'merchant-default-shipping-publish',
    )
    expect(mocks.adminReplace).toHaveBeenCalledWith(
      '/admin/products/prd_test/fulfillment-profile',
      expect.objectContaining({ shipping_template_id: 'sht_default', origin_region_code: '110000' }),
      'merchant-token',
      1,
    )
  })
})
