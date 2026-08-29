import { describe, expect, it, vi } from 'vitest'

import { getStoreProducts } from '@/api/catalog'

describe('catalog API client', () => {
  it('only forwards filters supported by the store-products contract', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({
          data: { items: [] },
          meta: { request_id: 'req_catalog', pagination: null },
        }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      ),
    )

    await getStoreProducts('store/test', {
      q: '咖啡',
      group_id: 'group_test',
      category_id: 'category_must_not_leak',
      brand_id: 'brand_must_not_leak',
      store_id: 'store_must_not_leak',
      price_min: '100',
      price_max: '500',
      sort: 'sales',
      limit: 20,
    })

    const calledUrl = String(fetchMock.mock.calls[0]?.[0])
    expect(calledUrl).toContain('/stores/store%2Ftest/products?')
    expect(calledUrl).toContain('q=%E5%92%96%E5%95%A1')
    expect(calledUrl).toContain('group_id=group_test')
    expect(calledUrl).not.toContain('category_id')
    expect(calledUrl).not.toContain('brand_id')
    expect(calledUrl).not.toContain('price_min')
    expect(calledUrl).not.toContain('price_max')
    expect(calledUrl).not.toContain('store_id=')
  })
})
