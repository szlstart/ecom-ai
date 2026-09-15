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
  putMerchantExclusiveReadCursor: vi.fn(),
  sendMerchantExclusiveMessageResilient: vi.fn(),
  adminGet: vi.fn(),
  realtimeOptions: null as null | {
    onEvent: (event: Record<string, unknown>) => void
    beforeReconnect: () => Promise<void>
  },
}))

vi.mock('@/api/admin-support', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-support')>(),
  listSupportConversations: mocks.listSupportConversations,
}))

vi.mock('@/api/admin-catalog', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/admin-catalog')>(),
  adminGet: mocks.adminGet,
}))

vi.mock('@/api/merchant-support', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/merchant-support')>(),
  getMerchantExclusiveConversation: mocks.getMerchantExclusiveConversation,
  listMerchantExclusiveMessages: mocks.listMerchantExclusiveMessages,
  putMerchantExclusiveReadCursor: mocks.putMerchantExclusiveReadCursor,
  sendMerchantExclusiveMessageResilient: mocks.sendMerchantExclusiveMessageResilient,
}))

vi.mock('@/api/realtime', async (importOriginal) => ({
  ...await importOriginal<typeof import('@/api/realtime')>(),
  RealtimeConnection: class {
    constructor(options: {
      onEvent: (event: Record<string, unknown>) => void
      beforeReconnect: () => Promise<void>
    }) { mocks.realtimeOptions = options }
    start() {}
    stop() {}
  },
}))

const conversation = {
  conversation_id: 'conv_customer', conversation_type: 'store', participant_type: 'user',
  participant_id: 'usr_customer', participant_name: '顾客小李', store_id: 'sto_1',
  participant_avatar_url: null,
  conversation_status: 'active', last_message_preview: '请问有库存吗？',
  last_message_at: '2026-08-27T09:01:00Z', unread_count: 2, requires_human: false,
  active_ticket_id: null, active_ticket_status: null, assigned_user_id: null,
}

describe('MerchantMessageCenter', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.defineProperty(document, 'visibilityState', { configurable: true, value: 'visible' })
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 1 })
    mocks.realtimeOptions = null
    mocks.listSupportConversations.mockResolvedValue({ data: { items: [conversation] } })
    mocks.getMerchantExclusiveConversation.mockResolvedValue({
      data: { conversation_id: 'conv_exclusive', unread_count: 3 },
    })
    mocks.listMerchantExclusiveMessages.mockResolvedValue({ data: { items: [] } })
    mocks.putMerchantExclusiveReadCursor.mockResolvedValue({ data: { unread_count: 0 } })
    mocks.sendMerchantExclusiveMessageResilient.mockResolvedValue({
      data: { message_id: 'msg_user', sequence_no: 1, sender_type: 'user', sent_at: '2026-09-14T00:00:00Z' },
    })
    mocks.adminGet.mockResolvedValue({
      data: { items: [{ store_id: 'sto_1', store_name: '测试店铺', logo_url: null }] },
    })
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

  it('marks the visible exclusive conversation read in the standalone workspace', async () => {
    mocks.listMerchantExclusiveMessages.mockResolvedValue({
      data: { items: [{ message_id: 'msg_ai', sequence_no: 3, sender_type: 'agent' }] },
    })
    const pinia = createPinia()
    setActivePinia(pinia)
    useAdminAuthStore().accessToken = 'merchant-token'
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/merchant/messages', component: { template: '<div />' } },
    ] })
    await router.push('/merchant/messages')
    await router.isReady()
    const wrapper = mount(MerchantMessageCenter, {
      props: { standalone: true },
      global: { plugins: [pinia, router], stubs: { Teleport: true } },
    })
    await flushPromises()

    expect(mocks.putMerchantExclusiveReadCursor).toHaveBeenCalledWith(
      expect.objectContaining({ message_id: 'msg_ai' }),
      'merchant-token',
    )
    expect(wrapper.text()).not.toContain('3 条未读')
  })

  it('labels the merchant agent clearly and lets operators collapse the customer list', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAdminAuthStore().accessToken = 'merchant-token'
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/merchant/messages', component: { template: '<div />' } },
    ] })
    await router.push('/merchant/messages')
    await router.isReady()
    const wrapper = mount(MerchantMessageCenter, {
      props: { standalone: true },
      global: { plugins: [pinia, router], stubs: { Teleport: true } },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('AI 经营助理')
    expect(wrapper.text()).toContain('顾客咨询')
    expect(wrapper.text()).toContain('顾客小李')

    await wrapper.get('.merchant-chat-group-title').trigger('click')
    expect(wrapper.text()).not.toContain('顾客小李')
  })

  it('sends an AI operations question from the visible button', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAdminAuthStore().accessToken = 'merchant-token'
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/merchant/messages', component: { template: '<div />' } },
    ] })
    await router.push('/merchant/messages')
    await router.isReady()
    const wrapper = mount(MerchantMessageCenter, {
      props: { standalone: true },
      global: { plugins: [pinia, router], stubs: { Teleport: true } },
    })
    await flushPromises()

    await wrapper.get('textarea').setValue('分析库存与待发货订单')
    await wrapper.get('.unified-chat-send').trigger('click')
    await flushPromises()

    expect(mocks.sendMerchantExclusiveMessageResilient).toHaveBeenCalledWith(
      '分析库存与待发货订单',
      'merchant-token',
    )
  })

  it('clears an ephemeral model stream when REST catches up the durable run', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAdminAuthStore().accessToken = 'merchant-token'
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/merchant/messages', component: { template: '<div />' } },
    ] })
    await router.push('/merchant/messages')
    await router.isReady()
    const wrapper = mount(MerchantMessageCenter, {
      props: { standalone: true },
      global: { plugins: [pinia, router], stubs: { Teleport: true } },
    })
    await flushPromises()

    mocks.realtimeOptions!.onEvent({
      type: 'agent.response.started',
      data: { conversation_id: 'conv_exclusive', run_id: 'run_complete' },
    })
    mocks.realtimeOptions!.onEvent({
      type: 'agent.response.delta',
      data: {
        conversation_id: 'conv_exclusive',
        run_id: 'run_complete',
        chunk_index: 1,
        text_so_far: '正在整理经营建议',
      },
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.find('.agent-stream').exists()).toBe(true)

    mocks.listMerchantExclusiveMessages.mockResolvedValueOnce({
      data: {
        items: [{
          message_id: 'msg_complete',
          sequence_no: 2,
          sender_type: 'agent',
          message_type: 'text',
          text: '建议已经整理完成。',
          message_status: 'sent',
          moderation_status: 'passed',
          content: {
            run_id: 'run_complete',
            execution_trace: { run_id: 'run_complete', status: 'completed' },
          },
          sent_at: '2026-09-14T00:00:01Z',
        }],
        previous_cursor: null,
      },
    })
    await mocks.realtimeOptions!.beforeReconnect()
    await flushPromises()

    expect(wrapper.find('.agent-stream').exists()).toBe(false)
    expect(wrapper.text()).toContain('建议已经整理完成。')
  })

  it('stops the live reasoning state when a waiting-confirmation card is persisted', async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAdminAuthStore().accessToken = 'merchant-token'
    const router = createRouter({ history: createMemoryHistory(), routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/merchant/messages', component: { template: '<div />' } },
    ] })
    await router.push('/merchant/messages')
    await router.isReady()
    const wrapper = mount(MerchantMessageCenter, {
      props: { standalone: true },
      global: { plugins: [pinia, router], stubs: { Teleport: true } },
    })
    await flushPromises()

    mocks.realtimeOptions!.onEvent({
      type: 'agent.response.started',
      data: { conversation_id: 'conv_exclusive', run_id: 'run_waiting' },
    })
    await wrapper.vm.$nextTick()
    expect(wrapper.text()).toContain('思考中')
    expect(wrapper.find('.agent-stream').exists()).toBe(true)

    mocks.realtimeOptions!.onEvent({
      type: 'message.created',
      data: {
        conversation_id: 'conv_exclusive',
        message: {
          sender_type: 'agent',
          message_type: 'agent_action_approval',
          content: { run_id: 'run_waiting' },
        },
      },
    })
    await wrapper.vm.$nextTick()

    expect(wrapper.find('.agent-stream').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('思考中')
  })
})
