import { describe, expect, it } from 'vitest'

import { appScrollBehavior } from './index'

describe('appScrollBehavior', () => {
  it('preserves the viewport for query-only changes on the same page', () => {
    const result = appScrollBehavior(
      { path: '/products/prd_test', hash: '', query: { sku_id: 'sku_2' } } as never,
      { path: '/products/prd_test', hash: '', query: { sku_id: 'sku_1' } } as never,
      null,
    )

    expect(result).toBe(false)
  })

  it('starts a genuinely different page at the top', () => {
    const result = appScrollBehavior(
      { path: '/me/orders/ord_test', hash: '', query: {} } as never,
      { path: '/me/orders', hash: '', query: {} } as never,
      null,
    )

    expect(result).toEqual({ top: 0 })
  })

  it('keeps explicit anchors and browser back positions authoritative', () => {
    expect(appScrollBehavior(
      { path: '/search', hash: '#search-results', query: {} } as never,
      { path: '/search', hash: '', query: {} } as never,
      null,
    )).toEqual({ el: '#search-results', top: 88, behavior: 'smooth' })
    expect(appScrollBehavior({} as never, {} as never, { left: 0, top: 640 })).toEqual({ left: 0, top: 640 })
  })
})
