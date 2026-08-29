import { describe, expect, it } from 'vitest'

import { formatChinaRegion } from './china-regions'

describe('formatChinaRegion', () => {
  it('converts division codes to customer-facing names', () => {
    expect(formatChinaRegion({ province_code: '320000', city_code: '320300', district_code: '320321' }))
      .toBe('江苏省 徐州市 丰县')
  })

  it('hides municipality placeholders', () => {
    expect(formatChinaRegion({ province_code: '110000', city_code: '110100', district_code: '110101' }))
      .toBe('北京市 东城区')
  })

  it('keeps an unknown code visible instead of losing address information', () => {
    expect(formatChinaRegion({ province_code: 'unknown', city_code: '320300', district_code: '320321' }))
      .toBe('unknown 徐州市 丰县')
  })
})
