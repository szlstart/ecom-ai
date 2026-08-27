import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import MerchantProductListPage from './MerchantProductListPage.vue'

const apiMocks = vi.hoisted(() => ({
  adminGet: vi.fn(),
  adminCommand: vi.fn(),
  adminDelete: vi.fn(),
}))

vi.mock('@/api/admin-catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-catalog')>(),
  adminGet: apiMocks.adminGet,
  adminCommand: apiMocks.adminCommand,
  adminDelete: apiMocks.adminDelete,
}))

const product = {
  product_id: 'prd_test', store_id: 'sto_test', store_name: '测试店铺', category_id: 'cat_test', category_name: '内部分类', brand_id: null, brand_name: null,
  product_name: '交易规则测试商品', subtitle: '测试', status: 'on_sale', min_price: '10.00', max_price: '10.00', currency: 'CNY', cover_image_url: null,
  sku_count: 1, available_quantity: 8, sales_count: 1, review_count: 0, rating_score: '0.00', updated_at: '2026-08-27T00:00:00', version: 3,
}

async function mountPage(hasTransactions: boolean) {
  apiMocks.adminGet.mockImplementation(async (path: string) => {
    if (path.startsWith('/admin/products?')) return { data: { items: [product], next_cursor: null } }
    if (path.startsWith('/admin/stores')) return { data: { items: [{ store_id: 'sto_test', store_name: '测试店铺', description: null, logo_url: null, rating_score: '0.00' }], next_cursor: null } }
    if (path.endsWith('/deletion-eligibility')) return { data: hasTransactions
      ? { product_id: 'prd_test', current_status: 'on_sale', has_transactions: true, can_delete: false, can_off_shelf: true, recommended_action: 'off_shelf', message: '该商品已经产生订单交易，不能删除，只能下架。' }
      : { product_id: 'prd_test', current_status: 'on_sale', has_transactions: false, can_delete: true, can_off_shelf: false, recommended_action: 'delete', message: '该商品没有产生过交易，可以直接删除。' } }
    throw new Error(`unexpected path: ${path}`)
  })
  apiMocks.adminCommand.mockResolvedValue({ data: { status: 'off_shelf' } })
  apiMocks.adminDelete.mockResolvedValue({ data: { product_id: 'prd_test' } })
  const pinia = createPinia()
  setActivePinia(pinia)
  useAdminAuthStore().accessToken = 'merchant-token'
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/merchant/products', component: MerchantProductListPage }] })
  await router.push('/merchant/products')
  await router.isReady()
  const wrapper = mount(MerchantProductListPage, { global: { plugins: [pinia, router], stubs: { Teleport: true } } })
  await flushPromises()
  await wrapper.get('button.merchant-card-delete-action').trigger('click')
  await flushPromises()
  return wrapper
}

describe('MerchantProductListPage deletion guard', () => {
  beforeEach(() => vi.clearAllMocks())

  it('offers off-shelf instead of deletion when the product has transactions', async () => {
    const wrapper = await mountPage(true)
    expect(wrapper.text()).toContain('已有交易，不能删除')
    expect(wrapper.text()).not.toContain('直接删除')
    await wrapper.get('button.danger').trigger('click')
    await flushPromises()
    expect(apiMocks.adminCommand).toHaveBeenCalledOnce()
    expect(apiMocks.adminDelete).not.toHaveBeenCalled()
  })

  it('offers direct deletion only when the product has no transactions', async () => {
    const wrapper = await mountPage(false)
    expect(wrapper.text()).toContain('没有产生过交易')
    expect(wrapper.text()).toContain('直接删除')
    await wrapper.get('button.danger').trigger('click')
    await flushPromises()
    expect(apiMocks.adminDelete).toHaveBeenCalledOnce()
    expect(apiMocks.adminCommand).not.toHaveBeenCalled()
  })
})
