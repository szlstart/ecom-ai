import { describe, expect, it } from 'vitest'

import { ORDER_VIEWS } from '@/api/orders'

describe('order API contract', () => {
  it('keeps all eight user order views in the documented order', () => {
    expect(ORDER_VIEWS).toEqual([
      'all',
      'pending_payment',
      'pending_shipment',
      'in_transit',
      'completed',
      'pending_review',
      'after_sale',
      'cancelled',
    ])
  })
})
