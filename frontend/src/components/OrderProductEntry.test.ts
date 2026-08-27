import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import OrderProductEntry from './OrderProductEntry.vue'

async function mountEntry(productAvailable: boolean) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/orders', component: { template: '<div />' } },
      { path: '/products/:productId', component: { template: '<div />' } },
    ],
  })
  await router.push('/orders')
  await router.isReady()
  const wrapper = mount(OrderProductEntry, {
    props: {
      productId: 'prd_history',
      skuId: 'sku_history',
      productName: '历史订单商品',
      productAvailable,
    },
    slots: { default: '<strong>历史订单商品</strong>' },
    global: { plugins: [router], stubs: { Teleport: true } },
  })
  return { router, wrapper }
}

describe('OrderProductEntry', () => {
  it('opens an available product with its historical SKU selected', async () => {
    const { router, wrapper } = await mountEntry(true)
    await wrapper.get('button.order-product-entry').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/products/prd_history?sku_id=sku_history')
    expect(wrapper.text()).not.toContain('该商品已被下架')
  })

  it('stays on the order page and explains when a historical product is unavailable', async () => {
    const { router, wrapper } = await mountEntry(false)
    await wrapper.get('button.order-product-entry').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.fullPath).toBe('/orders')
    expect(wrapper.get('[role="alertdialog"]').text()).toContain('该商品已被下架')
    await wrapper.get('[role="alertdialog"] button').trigger('click')
    expect(wrapper.find('[role="alertdialog"]').exists()).toBe(false)
  })
})
