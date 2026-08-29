import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { ChatMessage } from '@/api/messaging'

import ChatMessageContent from './ChatMessageContent.vue'

const base: ChatMessage = {
  message_id: 'msg_1', sequence_no: 1, sender_type: 'user', message_type: 'product_card',
  text: null, message_status: 'sent', moderation_status: 'passed', viewer_reaction: null,
  sent_at: '2026-08-30T08:00:00Z', content: null,
}

async function render(message: ChatMessage, audience: 'user' | 'merchant' | 'admin' = 'user') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/products/:id', component: { template: '<div />' } },
      { path: '/me/orders/:id', component: { template: '<div />' } },
      { path: '/merchant/products/:id', component: { template: '<div />' } },
      { path: '/merchant/orders', component: { template: '<div />' } },
      { path: '/admin/products/:id', component: { template: '<div />' } },
      { path: '/admin/orders/:id', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  return mount(ChatMessageContent, { props: { message, audience }, global: { plugins: [router] } })
}

describe('ChatMessageContent', () => {
  it('renders the server-built product snapshot and ignores unsafe image URLs', async () => {
    const wrapper = await render({
      ...base,
      content: {
        schema_version: 2, product_id: 'prd_ABC', product_name: '旅行水杯', product_status: 'on_sale',
        sku_id: 'sku_BLUE', sku_name: '海盐蓝', image_url: 'javascript:alert(1)',
        price: { minor_units: '1299', currency: 'CNY' }, available_quantity: 8, sales_count: 31,
        stock_status: 'available', store: { store_id: 'sto_1', store_name: '生活商店', logo_url: '/api/v1/files/fil_LOGO' },
      },
    })
    expect(wrapper.text()).toContain('旅行水杯')
    expect(wrapper.text()).toContain('海盐蓝')
    expect(wrapper.text()).toContain('¥12.99')
    expect(wrapper.findAll('img')).toHaveLength(1)
    expect(wrapper.get('a').attributes('href')).toBe('/products/prd_ABC?sku_id=sku_BLUE')
  })

  it('renders a privacy-safe order snapshot and uses audience-specific navigation', async () => {
    const wrapper = await render({
      ...base, message_type: 'order_card',
      content: {
        schema_version: 2, order_id: 'ord_ABCDEFGHIJKLMN', display_order_id: 'ord_AB…KLMN',
        order_status: 'shipped', payable_amount: { minor_units: '8800', currency: 'CNY' },
        total_quantity: 2, store: { store_id: 'sto_1', store_name: '生活商店' },
        items: [{ product_id: 'prd_1', sku_id: 'sku_1', product_name: '旅行水杯', sku_name: '海盐蓝', quantity: 2 }],
      },
    }, 'admin')
    expect(wrapper.text()).toContain('运输中')
    expect(wrapper.text()).toContain('实付 ¥88.00')
    expect(wrapper.text()).not.toContain('ABCDEFGHIJKLMN')
    expect(wrapper.get('a').attributes('href')).toBe('/admin/orders/ord_ABCDEFGHIJKLMN')
  })
})
