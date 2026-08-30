import { describe, expect, it } from 'vitest'

import { messageMoneyLabel } from './message-money'

describe('messageMoneyLabel', () => {
  it('formats the canonical minor_units money contract', () => {
    expect(messageMoneyLabel({ minor_units: '600', currency: 'CNY' })).toBe('¥6.00')
  })

  it('keeps compatibility with legacy amount payloads and rejects malformed money', () => {
    expect(messageMoneyLabel({ amount: 1280, currency: 'CNY' })).toBe('¥12.80')
    expect(messageMoneyLabel({ minor_units: 'not-money', currency: 'CNY' })).toBe('—')
  })
})
