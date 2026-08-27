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

const newSku = {
  ...sku, sku_id: 'sku_new', sku_name: '新增款式', spec_values: [{ name: '款式', value: '新增款式' }], sale_price: '18.00', version: 1,
}

const inventory = {
  sku_id: 'sku_test', sku_name: '黑色款', product_id: 'prd_test', product_name: '剪贴板测试商品', store_id: 'sto_test', store_name: '测试店铺',
  on_hand_quantity: 8, reserved_quantity: 1, safety_stock_quantity: 0, available_quantity: 7, sold_quantity: 0, status: 'active', last_reconciled_at: null, version: 1,
}
const uploadedFileId = 'file_01ARZ3NDEKTSV4RRFFQ69G5FAV'

const FileUploadStub = defineComponent({
  emits: ['uploaded'],
  setup(_props, { emit, expose }) {
    expose({
      async uploadFile(file: File) {
        mocks.pastedFile = file
        emit('uploaded', uploadedFileId)
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
    document.body.innerHTML = ''
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
      expect.objectContaining({ items: [expect.objectContaining({ file_id: uploadedFileId, image_type: 'main', sku_id: null })] }),
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

  it('keeps pasted detail images and text in the merchant-defined vertical order', async () => {
    const wrapper = await mountPage()
    await wrapper.get('.merchant-detail-block textarea').setValue('第一段文字')

    const source = new File(['detail'], '', { type: 'image/png' })
    wrapper.get('.merchant-detail-image-insert').element.dispatchEvent(pasteEvent([
      { kind: 'file', type: 'image/png', getAsFile: () => source },
    ]))
    await flushPromises()
    expect(mocks.pastedFile?.name).toMatch(/^clipboard-\d+\.png$/)
    expect(wrapper.text()).toContain('图片已添加到商品详情末尾')
    expect(wrapper.get('.merchant-detail-block.is-image img').attributes('src')).toContain(uploadedFileId)
    expect(wrapper.findAll('.file-upload-stub')).toHaveLength(2)

    const addText = wrapper.findAll('button').find((button) => button.text().includes('添加文字'))
    await addText!.trigger('click')
    const textareas = wrapper.findAll<HTMLTextAreaElement>('.merchant-detail-block textarea')
    await textareas[1]!.setValue('第二段文字')
    const saveDetail = wrapper.findAll('button').find((button) => button.text() === '保存商品详情')
    await saveDetail!.trigger('click')
    await flushPromises()

    const detailCall = mocks.adminCreate.mock.calls.find(([requestPath]) => requestPath.endsWith('/detail-content-versions'))
    expect(detailCall?.[1]).toMatchObject({ source_format: 'structured' })
    expect(JSON.parse(detailCall?.[1].source_content as string)).toEqual([
      { type: 'paragraph', text: '第一段文字' },
      { type: 'image', file_id: uploadedFileId, alt: '剪贴板测试商品' },
      { type: 'paragraph', text: '第二段文字' },
    ])
    expect(wrapper.text()).toContain('商品详情已按当前图文顺序保存')
  })

  it('restores a previously saved structured detail version in the same order', async () => {
    mocks.adminGet.mockImplementation(async (requestPath: string) => {
      if (requestPath === '/admin/stores?limit=20') return { data: { items: [store], next_cursor: null } }
      if (requestPath === '/admin/products/prd_test') return { data: { ...product, current_detail_content_version_id: 'pcv_test' } }
      if (requestPath.endsWith('/detail-content-versions/pcv_test')) return { data: {
        version_id: 'pcv_test', content_version: 1, source_format: 'structured',
        source_content: JSON.stringify([
          { type: 'paragraph', text: '上方文字' },
          { type: 'image', file_id: uploadedFileId, alt: '中间图片' },
          { type: 'paragraph', text: '下方文字' },
        ]),
        public_content_format: 'structured_v1', safe_blocks: [], safe_html: null,
        safe_text: '上方文字 中间图片 下方文字', security_scan_status: 'passed', status: 'draft', created_at: '2026-08-27T00:00:00Z',
      } }
      if (requestPath.endsWith('/skus')) return { data: [sku] }
      if (requestPath.endsWith('/images') || requestPath.endsWith('/attributes') || requestPath.endsWith('/faqs')) return { data: [] }
      if (requestPath.startsWith('/admin/inventories?')) return { data: { items: [inventory] } }
      if (requestPath.endsWith('/fulfillment-profile')) return { data: null }
      if (requestPath.endsWith('/shipping-templates')) return { data: [] }
      throw new Error(`unexpected path: ${requestPath}`)
    })

    const wrapper = await mountPage()
    expect(wrapper.findAll('.merchant-detail-block').map((block) => block.classes().find((name) => name.startsWith('is-')))).toEqual([
      'is-paragraph', 'is-image', 'is-paragraph',
    ])
    expect(wrapper.findAll<HTMLTextAreaElement>('.merchant-detail-block textarea').map((field) => field.element.value)).toEqual(['上方文字', '下方文字'])
    expect(wrapper.get('.merchant-detail-block.is-image img').attributes('src')).toContain(uploadedFileId)
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
    expect(wrapper.get('.merchant-inline-sku-form').classes()).toContain('active')
    expect(wrapper.find('.merchant-style-picker > div > button.active').exists()).toBe(false)
    expect(wrapper.get('.merchant-simple-sku-fields').text()).toContain('款式名称')
    expect(wrapper.get('.merchant-simple-sku-fields').text()).toContain('价格（元）')
    expect(wrapper.get('.merchant-simple-sku-fields').text()).toContain('库存')
    expect(wrapper.find('.merchant-inline-stock').exists()).toBe(false)
    expect(wrapper.find('.merchant-spec-editor').exists()).toBe(false)
  })

  it('keeps a save failure inside the editor instead of replacing the whole page', async () => {
    const wrapper = await mountPage()
    await wrapper.get('.merchant-style-picker > div > button').trigger('click')
    const numberInputs = wrapper.findAll<HTMLInputElement>('.merchant-simple-sku-fields input[type="number"]')
    await numberInputs[1]!.setValue('10')
    mocks.adminCreate.mockRejectedValueOnce(new Error('inventory failed'))
    await wrapper.get('.merchant-inline-sku-form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('.merchant-product-info-editor').exists()).toBe(true)
    expect(wrapper.find('.merchant-inline-sku-form').exists()).toBe(true)
    expect(wrapper.get('.alert.error').text()).toContain('网络异常')
    expect(wrapper.text()).not.toContain('暂时无法加载')
  })

  it('selects a newly saved style after completing it', async () => {
    const wrapper = await mountPage()
    mocks.adminCreate.mockImplementation(async (requestPath: string) => {
      if (requestPath.endsWith('/skus')) return { data: newSku }
      if (requestPath === '/admin/inventory-adjustments') return { data: {} }
      return { data: {} }
    })
    mocks.adminGet.mockImplementation(async (requestPath: string) => {
      if (requestPath === '/admin/stores?limit=20') return { data: { items: [store], next_cursor: null } }
      if (requestPath === '/admin/products/prd_test') return { data: product }
      if (requestPath.endsWith('/skus')) return { data: [sku, newSku] }
      if (requestPath.endsWith('/images')) return { data: [] }
      if (requestPath.endsWith('/attributes') || requestPath.endsWith('/faqs')) return { data: [] }
      if (requestPath.startsWith('/admin/inventories?')) return { data: { items: [inventory, { ...inventory, sku_id: 'sku_new', sku_name: '新增款式', on_hand_quantity: 0, available_quantity: 0 }] } }
      if (requestPath.endsWith('/fulfillment-profile')) return { data: null }
      if (requestPath.endsWith('/shipping-templates')) return { data: [] }
      throw new Error(`unexpected path: ${requestPath}`)
    })

    await wrapper.get('.merchant-style-picker > header button').trigger('click')
    await wrapper.get('.merchant-simple-sku-fields input[placeholder]').setValue('新增款式')
    const numberInputs = wrapper.findAll<HTMLInputElement>('.merchant-simple-sku-fields input[type="number"]')
    await numberInputs[0]!.setValue('18')
    await numberInputs[1]!.setValue('6')
    await wrapper.get('.merchant-inline-sku-form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.merchant-style-picker > div > button.active').text()).toContain('新增款式')
    expect(wrapper.text()).toContain('新款式、价格和库存已添加')
  })

  it('removes an existing style from the merchant editor through the audited status command', async () => {
    const wrapper = await mountPage()
    await wrapper.get('.merchant-style-picker > div > button').trigger('click')
    expect(wrapper.get('.merchant-inline-sku-form').text()).toContain('删除款式')

    await wrapper.get('.merchant-inline-sku-form button.danger').trigger('click')
    expect(document.body.querySelector('.merchant-delete-dialog')?.textContent).toContain('删除“黑色款”款式')
    mocks.adminGet.mockImplementation(async (requestPath: string) => {
      if (requestPath === '/admin/stores?limit=20') return { data: { items: [store], next_cursor: null } }
      if (requestPath === '/admin/products/prd_test') return { data: product }
      if (requestPath.endsWith('/skus')) return { data: [{ ...sku, status: 'disabled', version: 2 }] }
      if (requestPath.endsWith('/images') || requestPath.endsWith('/attributes') || requestPath.endsWith('/faqs')) return { data: [] }
      if (requestPath.startsWith('/admin/inventories?')) return { data: { items: [inventory] } }
      if (requestPath.endsWith('/fulfillment-profile')) return { data: null }
      if (requestPath.endsWith('/shipping-templates')) return { data: [] }
      throw new Error(`unexpected path: ${requestPath}`)
    })
    document.body.querySelector<HTMLButtonElement>('.merchant-delete-dialog button.danger')?.click()
    await flushPromises()

    expect(mocks.adminCommand).toHaveBeenCalledWith(
      '/admin/products/prd_test/skus/sku_test/status-changes',
      expect.objectContaining({ action: 'disable', reason_code: 'MERCHANT_STYLE_REMOVE' }),
      'merchant-token',
      1,
      'merchant-style-delete',
    )
    expect(wrapper.find('.merchant-style-picker > div > button').exists()).toBe(false)
    expect(wrapper.text()).toContain('款式已删除，不再向顾客展示')
  })

  it('protects the final active style while an item is on sale', async () => {
    mocks.adminGet.mockImplementation(async (requestPath: string) => {
      if (requestPath === '/admin/stores?limit=20') return { data: { items: [store], next_cursor: null } }
      if (requestPath === '/admin/products/prd_test') return { data: { ...product, status: 'on_sale' } }
      if (requestPath.endsWith('/skus')) return { data: [sku] }
      if (requestPath.endsWith('/images') || requestPath.endsWith('/attributes') || requestPath.endsWith('/faqs')) return { data: [] }
      if (requestPath.startsWith('/admin/inventories?')) return { data: { items: [inventory] } }
      if (requestPath.endsWith('/fulfillment-profile')) return { data: null }
      if (requestPath.endsWith('/shipping-templates')) return { data: [] }
      throw new Error(`unexpected path: ${requestPath}`)
    })
    const wrapper = await mountPage()
    await wrapper.get('.merchant-style-picker > div > button').trigger('click')
    await wrapper.get('.merchant-inline-sku-form button.danger').trigger('click')

    expect(document.body.querySelector('.merchant-delete-dialog')?.textContent).toContain('当前不能删除最后一个在售款式')
    expect(document.body.querySelector('.merchant-delete-dialog button.danger')).toBeNull()
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
