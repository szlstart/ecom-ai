import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import MerchantProductEditorPage from './MerchantProductEditorPage.vue'

const mocks = vi.hoisted(() => ({
  adminGet: vi.fn(),
  getCategories: vi.fn(),
  listAdminReviews: vi.fn(),
  pastedFile: null as File | null,
}))

vi.mock('@/api/admin-catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-catalog')>(),
  adminGet: mocks.adminGet,
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
    mocks.getCategories.mockResolvedValue({ data: [] })
    mocks.listAdminReviews.mockResolvedValue({ data: { items: [], next_cursor: null } })
    mocks.adminGet.mockImplementation(async (path: string) => {
      if (path === '/admin/stores?limit=20') return { data: { items: [store], next_cursor: null } }
      if (path === '/admin/products/prd_test') return { data: product }
      if (path.endsWith('/skus')) return { data: [sku] }
      if (path.endsWith('/images') || path.endsWith('/attributes') || path.endsWith('/faqs')) return { data: [] }
      if (path.startsWith('/admin/inventories?')) return { data: { items: [] } }
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
    expect(wrapper.text()).toContain('剪贴板图片已上传并通过安全扫描')
    expect(wrapper.text()).toContain('从本地选择图片')
    expect(wrapper.find('.merchant-product-editor').exists()).toBe(true)
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
})
