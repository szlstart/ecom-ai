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
      { path: '/me/after-sales/:id', component: { template: '<div />' } },
      { path: '/cart', component: { template: '<div />' } },
      { path: '/merchant/products/:id', component: { template: '<div />' } },
      { path: '/merchant/orders', component: { template: '<div />' } },
      { path: '/admin/products/:id', component: { template: '<div />' } },
      { path: '/admin/orders/:id', component: { template: '<div />' } },
      { path: '/admin/observability', component: { template: '<div />' } },
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
        stock_status: 'available', store: { store_id: 'sto_1', store_name: '生活商店', logo_url: '/api/v1/files/file_LOGO' },
      },
    })
    expect(wrapper.text()).toContain('旅行水杯')
    expect(wrapper.text()).toContain('海盐蓝')
    expect(wrapper.text()).toContain('¥12.99')
    expect(wrapper.findAll('img')).toHaveLength(1)
    expect(wrapper.get('a').attributes('href')).toBe('/products/prd_ABC?sku_id=sku_BLUE')
    expect(wrapper.get('img').attributes('src')).toBe('http://127.0.0.1:8000/api/v1/files/file_LOGO')
  })

  it('renders canonical file images for product and order cards', async () => {
    const product = await render({
      ...base,
      content: {
        product_id: 'prd_ABC', product_name: '旅行水杯', product_status: 'on_sale',
        image_url: '/api/v1/files/file_PRODUCT?variant=thumbnail',
        price: { minor_units: '1299', currency: 'CNY' }, available_quantity: 8,
        stock_status: 'available', store: { store_name: '生活商店', logo_url: '/api/v1/files/file_STORE' },
      },
    })
    expect(product.findAll('img')).toHaveLength(2)

    const order = await render({
      ...base,
      message_type: 'order_card',
      content: {
        order_id: 'ord_ABC', order_status: 'paid', payable_amount: { minor_units: '1299', currency: 'CNY' },
        total_quantity: 1, store: { store_name: '生活商店', logo_url: '/api/v1/files/file_STORE' },
        items: [{ product_name: '旅行水杯', sku_name: '蓝色', quantity: 1, image_url: '/api/v1/files/file_ITEM?variant=thumbnail' }],
      },
    })
    expect(order.findAll('img')).toHaveLength(2)
  })

  it('renders a privacy-safe order snapshot and uses audience-specific navigation', async () => {
    const wrapper = await render({
      ...base, message_type: 'order_card',
      content: {
        schema_version: 2, order_id: 'ord_ABCDEFGHIJKLMN', display_order_id: 'ord_AB…KLMN',
        order_status: 'shipped', payable_amount: { minor_units: '8800', currency: 'CNY' },
        created_at: '2026-09-11T13:30:00+08:00',
        total_quantity: 2, store: { store_id: 'sto_1', store_name: '生活商店' },
        items: [{ product_id: 'prd_1', sku_id: 'sku_1', product_name: '旅行水杯', sku_name: '海盐蓝', quantity: 2 }],
      },
    }, 'admin')
    expect(wrapper.text()).toContain('运输中')
    expect(wrapper.text()).toContain('实付 ¥88.00')
    expect(wrapper.text()).not.toContain('ABCDEFGHIJKLMN')
    expect(wrapper.text()).not.toContain('ord_AB…KLMN')
    expect(wrapper.text()).toContain('下单于')
    expect(wrapper.get('a').attributes('href')).toBe('/admin/orders/ord_ABCDEFGHIJKLMN')
  })

  it('renders Agent text followed by clickable order cards without exposing raw IDs', async () => {
    const wrapper = await render({
      ...base,
      sender_type: 'agent',
      message_type: 'text',
      text: '找到你的 1 笔最近订单。点击卡片可查看详情或继续处理。',
      content: {
        order_cards: [{
          schema_version: 2, order_id: 'ord_PRIVATE123456', display_order_id: 'ord_PR…3456',
          order_status: 'completed', payable_amount: { minor_units: '600', currency: 'CNY' },
          created_at: '2026-09-11T13:30:00+08:00',
          total_quantity: 1, store: { store_name: '文具专卖店' },
          items: [{ product_name: '2B 铅笔', sku_name: '标准款', quantity: 1 }],
        }],
      },
    })

    expect(wrapper.text()).toContain('找到你的 1 笔最近订单')
    expect(wrapper.text()).toContain('2B 铅笔')
    expect(wrapper.text()).toContain('实付 ¥6.00')
    expect(wrapper.text()).not.toContain('PRIVATE123456')
    expect(wrapper.text()).not.toContain('ord_PR…3456')
    expect(wrapper.get('a').attributes('href')).toBe('/me/orders/ord_PRIVATE123456')
  })

  it('renders Agent recommendations as clickable product cards instead of a text list', async () => {
    const wrapper = await render({
      ...base,
      sender_type: 'agent',
      message_type: 'text',
      text: '为你找到 2 件本店在售商品。可以直接点击卡片查看详情。',
      content: {
        product_cards: [
          {
            product_id: 'prd_RULER', product_name: '透明考试直尺', product_status: 'on_sale',
            sku_id: 'sku_15CM', sku_name: '15cm', image_url: '/api/v1/files/file_RULER?variant=thumbnail',
            price: { minor_units: '871', currency: 'CNY' }, available_quantity: 18, sales_count: 72,
            stock_status: 'available', store: { store_name: '文具专卖店' },
          },
          {
            product_id: 'prd_PENCIL', product_name: '考试涂卡铅笔', product_status: 'on_sale',
            price: { minor_units: '600', currency: 'CNY' }, available_quantity: 30, sales_count: 99,
            stock_status: 'available', store: { store_name: '文具专卖店' },
          },
        ],
      },
    })

    expect(wrapper.text()).toContain('透明考试直尺')
    expect(wrapper.text()).toContain('考试涂卡铅笔')
    expect(wrapper.text()).toContain('查看商品 ›')
    expect(wrapper.text()).not.toContain('- 透明考试直尺')
    expect(wrapper.findAll('a')).toHaveLength(2)
    expect(wrapper.findAll('a')[0]?.attributes('href')).toBe('/products/prd_RULER?sku_id=sku_15CM')
  })

  it('renders operational results as a compact actionable detail card', async () => {
    const wrapper = await render({
      ...base,
      sender_type: 'agent',
      message_type: 'text',
      text: '已更新物流包裹的最新进度。',
      content: {
        detail_cards: [{
          kind: 'logistics', icon: '运', eyebrow: '订单物流', title: '包裹最新进度',
          badge: '实时轨迹', summary: '物流节点按最近一次同步结果展示。',
          rows: [{ label: '模拟快递', value: '运输中', meta: '上海市 · 包裹正在运输' }],
          action: { resource_type: 'order', resource_id: 'ord_TRACK', label: '查看完整物流' },
        }],
      },
    })

    expect(wrapper.text()).toContain('包裹最新进度')
    expect(wrapper.text()).toContain('模拟快递')
    expect(wrapper.text()).toContain('上海市 · 包裹正在运输')
    expect(wrapper.get('a').attributes('href')).toBe('/me/orders/ord_TRACK')
  })

  it('accepts only audience-scoped action paths from operational cards', async () => {
    const allowed = await render({
      ...base, sender_type: 'agent', message_type: 'text', text: '运行状态已核对。',
      content: { detail_cards: [{ title: '运行诊断', action: { path: '/admin/observability', label: '打开管理页面' } }] },
    }, 'admin')
    expect(allowed.get('a').attributes('href')).toBe('/admin/observability')

    const denied = await render({
      ...base, sender_type: 'agent', message_type: 'text', text: '无效跳转。',
      content: { detail_cards: [{ title: '无效入口', action: { path: '/merchant/products', label: '打开' } }] },
    }, 'admin')
    expect(denied.find('a').exists()).toBe(false)
  })

  it('renders the current cart as a compact clickable shopping card', async () => {
    const wrapper = await render({
      ...base,
      sender_type: 'agent',
      message_type: 'text',
      text: '购物车里共有 2 件商品，已选 1 件。',
      content: {
        cart_card: {
          total_quantity: 2,
          selected_quantity: 1,
          selected_amount: { minor_units: '600', currency: 'CNY' },
          groups: [{
            store_id: 'sto_1', store_name: '文具专卖店', selected_quantity: 1,
            items: [{
              product_id: 'prd_PENCIL', product_name: '考试涂卡铅笔', sku_name: '2B',
              quantity: 2, current_price: { minor_units: '600', currency: 'CNY' },
              image_url: '/api/v1/files/file_PENCIL?variant=thumbnail',
            }],
          }],
        },
      },
    })

    expect(wrapper.text()).toContain('我的购物车')
    expect(wrapper.text()).toContain('考试涂卡铅笔')
    expect(wrapper.text()).toContain('¥6.00')
    expect(wrapper.get('a').attributes('href')).toBe('/cart')
    expect(wrapper.find('img').attributes('src')).toContain('/api/v1/files/file_PENCIL')
  })
})
