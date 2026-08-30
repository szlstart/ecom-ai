import { describe, expect, it } from 'vitest'

import { userOrderStatusLabel } from './order-status'

describe('userOrderStatusLabel', () => {
  it('distinguishes delivered parcels from orders still in transit', () => {
    expect(userOrderStatusLabel('shipped', 'in_transit')).toBe('运输中')
    expect(userOrderStatusLabel('shipped', 'delivered')).toBe('已签收，待确认收货')
  })

  it('keeps completed order state authoritative', () => {
    expect(userOrderStatusLabel('completed', 'delivered')).toBe('已完成')
  })
})
