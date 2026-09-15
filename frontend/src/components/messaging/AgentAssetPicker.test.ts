import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AgentAssetPicker from './AgentAssetPicker.vue'

const mocks = vi.hoisted(() => ({ adminGet: vi.fn(), listAdminUsers: vi.fn() }))

vi.mock('@/api/admin-catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-catalog')>(),
  adminGet: mocks.adminGet,
}))

vi.mock('@/api/admin-users', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-users')>(),
  listAdminUsers: mocks.listAdminUsers,
}))

const store = {
  store_id: 'sto_test', owner_user_id: 'usr_owner', store_name: '文具专卖店',
  description: null, logo_file_id: null, logo_url: null, status: 'active',
  suspension_source: null, rating_score: '5.00', rating_count: 1, follower_count: 0,
  sales_count: 1, product_count: 1, net_revenue: null, store_name_changed_at: null,
  store_name_change_available_at: null, opened_at: null, suspended_at: null,
  closed_at: null, version: 1,
}

const product = {
  product_id: 'prd_test', store_id: 'sto_test', store_name: '文具专卖店',
  category_id: 'cat_test', category_name: '文具', brand_id: null, brand_name: null,
  product_name: '考试铅笔', subtitle: null, status: 'on_sale', min_price: '6.00',
  max_price: '6.00', currency: 'CNY', cover_image_url: null, sku_count: 1,
  available_quantity: 10, sales_count: 2, review_count: 0, rating_score: '0.00',
  updated_at: '2026-09-15T00:00:00Z', version: 1,
}

describe('AgentAssetPicker', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.adminGet.mockImplementation((path: string) => {
      if (path.startsWith('/admin/stores')) return Promise.resolve({ data: { items: [store] } })
      if (path.startsWith('/admin/products/prd_test/skus')) {
        return Promise.resolve({
          data: [{
            sku_id: 'sku_test', product_id: 'prd_test', merchant_sku_code: null,
            sku_name: '2B 六支装', spec_values: [], sale_price: '6.00', market_price: '6.00',
            currency: 'CNY', weight_grams: null, barcode: null, status: 'active', version: 1,
          }],
        })
      }
      if (path.startsWith('/admin/products')) return Promise.resolve({ data: { items: [product] } })
      throw new Error(`unexpected path: ${path}`)
    })
    mocks.listAdminUsers.mockResolvedValue({
      data: {
        items: [{
          user_id: 'usr_buyer', username: 'tulubi', nickname: 'tulubi',
          account_status: 'active', registered_at: '2026-09-15T00:00:00Z',
          last_login_at: null, permission_version: 1, version: 1,
        }],
        next_cursor: null,
      },
    })
  })

  it('loads the admin SKU array contract and exposes the selected SKU target', async () => {
    const wrapper = mount(AgentAssetPicker, {
      props: { open: true, audience: 'admin', accessToken: 'admin-token' },
      global: {
        stubs: {
          Teleport: true,
          AdminFileUpload: { template: '<div data-test="upload" />' },
        },
      },
    })
    await flushPromises()

    const purposeButton = wrapper.findAll('nav button').find((button) => button.text().includes('替换款式图片'))
    expect(purposeButton).toBeTruthy()
    await purposeButton!.trigger('click')
    await flushPromises()

    const productSelect = wrapper.findAll('select')[1]!
    await productSelect.setValue('prd_test')
    await flushPromises()

    const skuSelect = wrapper.findAll('select')[2]!
    await skuSelect.setValue('sku_test')
    await flushPromises()

    expect(mocks.adminGet).toHaveBeenCalledWith('/admin/products/prd_test/skus', 'admin-token')
    expect(wrapper.text()).toContain('2B 六支装 · ¥6.00')
    expect(wrapper.text()).toContain('文具专卖店 / 考试铅笔 / 2B 六支装')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('lets the administrator target a user avatar without a store binding', async () => {
    const wrapper = mount(AgentAssetPicker, {
      props: { open: true, audience: 'admin', accessToken: 'admin-token' },
      global: {
        stubs: {
          Teleport: true,
          AdminFileUpload: {
            name: 'AdminFileUpload',
            props: ['purpose', 'businessContextId'],
            template: '<div data-test="upload" />',
          },
        },
      },
    })
    await flushPromises()

    const purposeButton = wrapper.findAll('nav button').find((button) => button.text().includes('更新用户头像'))
    expect(purposeButton).toBeTruthy()
    await purposeButton!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('用户 tulubi 的公开头像')
    const upload = wrapper.getComponent({ name: 'AdminFileUpload' })
    expect(upload.props('purpose')).toBe('user_avatar')
    expect(upload.props('businessContextId')).toBe('usr_buyer')
  })
})
