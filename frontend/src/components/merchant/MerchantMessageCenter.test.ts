import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import { useAdminAuthStore } from '@/stores/admin-auth'

import MerchantMessageCenter from './MerchantMessageCenter.vue'

const mocks = vi.hoisted(() => ({
  listSupportTickets: vi.fn(),
  getMerchantExclusiveConversation: vi.fn(),
  listMerchantExclusiveMessages: vi.fn(),
  realtimeOptions: null as null | { onEvent: (event: Record<string, unknown>) => void },
}))

vi.mock('@/api/admin-support', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-support')>(),
  listSupportTickets: mocks.listSupportTickets,
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

const ticket = {
  ticket_id: 'hst_1', conversation_id: 'conv_customer', queue_type: 'store', queue_code: 'store:1',
  ticket_type: 'general', priority: 'normal', ticket_status: 'active', assigned_user_id: 'usr_merchant',
  handoff_summary: '商品咨询', sla_due_at: null, waiting_reason_code: null, unread_count: 2,
  created_at: '2026-08-27T09:00:00Z', updated_at: '2026-08-27T09:01:00Z', version: 1,
}

describe('MerchantMessageCenter', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1 })
    mocks.realtimeOptions = null
    mocks.listSupportTickets.mockResolvedValue({ data: { items: [ticket] } })
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
