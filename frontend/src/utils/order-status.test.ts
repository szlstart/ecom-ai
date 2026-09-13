import { describe, expect, it } from 'vitest'

import { consumerOrderEvents } from './order-status'

describe('consumerOrderEvents', () => {
  it('hides internal mirror events and never exposes unknown event codes', () => {
    const base = { state_dimension: 'order', reason: null, occurred_at: '2026-09-13T00:00:00Z' }
    const result = consumerOrderEvents([
      { ...base, event_id: 1, event_code: 'order.created', to_status: 'pending_payment' },
      { ...base, event_id: 2, event_code: 'payment.attempt_started', state_dimension: 'payment', to_status: 'processing' },
      { ...base, event_id: 3, event_code: 'payment.succeeded', state_dimension: 'payment', to_status: 'paid' },
      { ...base, event_id: 4, event_code: 'order.payment_succeeded', state_dimension: 'order', to_status: 'paid' },
      { ...base, event_id: 5, event_code: 'new.internal_event', state_dimension: 'fulfillment', to_status: 'unknown' },
    ])

    expect(result.map((item) => item.title)).toEqual(['订单已提交', '付款成功', '订单状态已更新'])
    expect(JSON.stringify(result)).not.toMatch(/payment\.|order\.|internal_event|processing/)
  })
})
