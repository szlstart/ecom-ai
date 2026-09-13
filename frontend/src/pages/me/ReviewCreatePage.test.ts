import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useUserAuthStore } from '@/stores/user-auth'
import ReviewCreatePage from './ReviewCreatePage.vue'

const mocks = vi.hoisted(() => ({ getReviewEligibility: vi.fn(), getMyOrder: vi.fn() }))
vi.mock('@/api/reviews', async (importOriginal) => ({ ...await importOriginal<typeof import('@/api/reviews')>(), getReviewEligibility: mocks.getReviewEligibility }))
vi.mock('@/api/orders', async (importOriginal) => ({ ...await importOriginal<typeof import('@/api/orders')>(), getMyOrder: mocks.getMyOrder }))

describe('ReviewCreatePage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getReviewEligibility.mockResolvedValue({ data: { order_item_id: 'oit_test', order_id: 'ord_test', product_id: 'prd_test', sku_id: 'sku_test', product_name: '测试商品', sku_name: '蓝色款', order_completed_at: '2026-09-13T00:00:00Z', review_deadline_at: '2026-10-13T00:00:00Z', eligible: true, reason_code: null, reason_message: null, existing_review_id: null, available_actions: ['create'] } })
    mocks.getMyOrder.mockResolvedValue({ data: { items: [{ order_item_id: 'oit_test', image_url: '/files/test.jpg' }] } })
  })

  it('uses a visual star picker and updates the rating copy', async () => {
    const pinia = createPinia(); setActivePinia(pinia); useUserAuthStore().accessToken = 'user-token'
    const stub = defineComponent({ render: () => h('div') })
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/me/order-items/:orderItemId/review', component: ReviewCreatePage }, { path: '/me/orders', component: stub }, { path: '/me/orders/:orderId', component: stub },
    ] })
    await router.push('/me/order-items/oit_test/review'); await router.isReady()
    const wrapper = mount(ReviewCreatePage, { global: { plugins: [pinia, router], stubs: { ReviewImageUpload: true } } })
    await flushPromises()

    expect(wrapper.findAll('.star-picker button')).toHaveLength(5)
    await wrapper.findAll('.star-picker button')[2]!.trigger('click')
    expect(wrapper.text()).toContain('3 星 · 一般')
    expect(wrapper.text()).toContain('蓝色款')
    wrapper.unmount()
  })
})
