import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { ProductCardData } from '@/api/catalog'

import ProductCard from './ProductCard.vue'

describe('ProductCard', () => {
  it('only shows the public product name and actual price', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/products/:productId', component: { template: '<div />' } },
        { path: '/stores/:storeId', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()
    const product = {
      product_id: 'prd_public',
      store_id: 'sto_public',
      store_name: '公开店铺',
      product_name: '公开商品名称',
      price: { minor_units: '1299', currency: 'CNY' },
      price_range: null,
      sales_count: 8,
      rating_score: '4.9',
      review_count: 3,
      main_image: null,
      subtitle: '不应展示的一句话卖点',
      description: '不应展示的旧商品简介',
      market_price: { minor_units: '9999', currency: 'CNY' },
    } as unknown as ProductCardData

    const wrapper = mount(ProductCard, {
      props: { product },
      global: { plugins: [router] },
    })

    expect(wrapper.text()).toContain('公开商品名称')
    expect(wrapper.text()).toContain('¥12.99')
    expect(wrapper.text()).not.toContain('一句话卖点')
    expect(wrapper.text()).not.toContain('旧商品简介')
    expect(wrapper.text()).not.toContain('99.99')
  })
})
