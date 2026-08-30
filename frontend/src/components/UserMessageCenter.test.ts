import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useUserAuthStore } from '@/stores/user-auth'

import UserMessageCenter from './UserMessageCenter.vue'

const mocks = vi.hoisted(() => ({
  ensureExclusiveConversation: vi.fn(),
  listConversations: vi.fn(),
  realtimeOptions: null as null | { onEvent: (event: Record<string, unknown>) => void },
}))

vi.mock('@/api/messaging', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/messaging')>(),
  ensureExclusiveConversation: mocks.ensureExclusiveConversation,
  listConversations: mocks.listConversations,
}))

vi.mock('@/api/realtime', () => ({
  RealtimeConnection: class {
    constructor(options: { onEvent: (event: Record<string, unknown>) => void }) { mocks.realtimeOptions = options }
    start() {}
    stop() {}
  },
}))

const exclusive = {
  conversation_id: 'conv_exclusive', conversation_type: 'exclusive', conversation_status: 'active',
  store_id: null, title: '专属客服', is_fixed: true, fixed_rank: 1,
  last_message_preview: '欢迎回来', last_message_at: '2026-08-27T09:00:00Z', last_sequence_no: 3,
  unread_count: 2, version: 1, active_contexts: [],
}
const store = {
  ...exclusive, conversation_id: 'conv_store', conversation_type: 'store', store_id: 'sto_1',
  title: '示例店铺', is_fixed: false, fixed_rank: null, unread_count: 1,
}

async function mountCenter() {
  const pinia = createPinia()
  setActivePinia(pinia)
  useUserAuthStore().accessToken = 'user-token'
  const router = createRouter({ history: createMemoryHistory(), routes: [
    { path: '/', component: { template: '<div />' } },
    { path: '/messages', component: { template: '<div />' } },
  ] })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(UserMessageCenter, {
    global: {
      plugins: [pinia, router],
      stubs: { Teleport: true, ConversationPage: { template: '<div data-test="conversation" />' } },
    },
  })
  await flushPromises()
  return { wrapper, router, pinia }
}

describe('UserMessageCenter', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      callback(0)
      return 1
    })
    mocks.realtimeOptions = null
    mocks.ensureExclusiveConversation.mockResolvedValue({ data: exclusive })
    mocks.listConversations.mockResolvedValue({ data: { items: [exclusive, store] } })
  })

  it('navigates to the standalone messages page and shows the dynamic unread count', async () => {
    const { wrapper, router, pinia } = await mountCenter()
    expect(wrapper.get('.user-message-trigger').text()).toContain('3')

    await wrapper.get('.user-message-trigger').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/messages')
    expect(wrapper.find('[role="dialog"]').exists()).toBe(false)

    const page = mount(UserMessageCenter, {
      props: { standalone: true },
      global: { plugins: [pinia, router], stubs: { ConversationPage: { template: '<div data-test="conversation" />' } } },
    })
    await flushPromises()
    expect(page.get('[aria-label="用户消息中心"]').attributes('aria-label')).toBe('用户消息中心')
    expect(page.text()).toContain('专属客服')
    expect(page.text()).toContain('示例店铺')
  })

  it('updates unread badges and gently shakes when an incoming message arrives', async () => {
    const { wrapper } = await mountCenter()
    expect(mocks.realtimeOptions).not.toBeNull()

    mocks.realtimeOptions!.onEvent({
      type: 'unread.updated',
      data: { conversation_id: 'conv_store', conversation_unread: 5 },
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.user-message-trigger').text()).toContain('7')

    mocks.realtimeOptions!.onEvent({
      type: 'message.created',
      data: { conversation_id: 'conv_store', message: { sender_type: 'human' } },
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.get('.user-message-trigger').classes()).toContain('message-arrival-shake')
  })
})
