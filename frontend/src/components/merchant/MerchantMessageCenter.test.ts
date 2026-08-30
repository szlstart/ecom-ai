import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import MerchantMessageCenter from './MerchantMessageCenter.vue'

const mocks = vi.hoisted(() => ({
  listSupportConversations: vi.fn(),
  getMerchantExclusiveConversation: vi.fn(),
  listMerchantExclusiveMessages: vi.fn(),
  realtimeOptions: null as null | { onEvent: (event: Record<string, unknown>) => void },
}))

vi.mock('@/api/admin-support', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-support')>(),
  listSupportConversations: mocks.listSupportConversations,
}))

vi.mock('@/api/merchant-support', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/merchant-support')>(),
  getMerchantExclusiveConversation: mocks.getMerchantExclusiveConversation,
  listMerchantExclusiveMessages: mocks.listMerchantExclusiveMessages,
}))

vi.mock('@/api/realtime', () => ({
  RealtimeConnection: class {
    constructor(options: { onEvent: (event: Record<string, unknown>) => void }) { mocks.realtimeOptions = options }
    start() {}
    stop() {}
  },
}))

const conversation = {
  conversation_id: 'conv_customer', conversation_type: 'store', participant_type: 'user',
  participant_id: 'usr_customer', participant_name: '顾客小李', store_id: 'sto_1',
  conversation_status: 'active', last_message_preview: '请问有库存吗？',
  last_message_at: '2026-08-27T09:01:00Z', unread_count: 2, requires_human: false,
  active_ticket_id: null, active_ticket_status: null, assigned_user_id: null,
}

describe('MerchantMessageCenter', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1 })
    mocks.realtimeOptions = null
    mocks.listSupportConversations.mockResolvedValue({ data: { items: [conversation] } })
    mocks.getMerchantExclusiveConversation.mockResolvedValue({
      data: { conversation_id: 'conv_exclusive', unread_count: 3 },
    })
    mocks.listMerchantExclusiveMessages.mockResolvedValue({ data: { items: [] } })
  })

  it('shows true unread totals without shaking on the initial load, then shakes for an incoming message', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAdminAuthStore().accessToken = 'merchant-token'
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/merchant/messages', component: { template: '<div />' } },
    ] })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(MerchantMessageCenter, {
      global: { plugins: [pinia, router], stubs: { Teleport: true } },
    })
    await flushPromises()

    const trigger = wrapper.get('.merchant-message-trigger')
    expect(trigger.text()).toContain('5')
    expect(trigger.classes()).not.toContain('message-arrival-shake')

    mocks.realtimeOptions!.onEvent({
      type: 'message.created',
      data: { conversation_id: 'conv_customer', message: { sender_type: 'user' } },
    })
    await wrapper.vm.$nextTick()
    expect(trigger.classes()).toContain('message-arrival-shake')
  })
})
