import areaData from 'china-area-data'

interface RegionCodes {
  province_code: string
  city_code: string
  district_code: string
}

export function chinaRegionName(code: string): string {
  for (const regions of Object.values(areaData)) {
    if (regions[code]) return regions[code]
  }
  return code
}

export function formatChinaRegion(codes: RegionCodes): string {
  return [
    chinaRegionName(codes.province_code),
    chinaRegionName(codes.city_code),
    chinaRegionName(codes.district_code),
  ]
    .filter((name, index, values) => name !== '市辖区' && values.indexOf(name) === index)
    .join(' ')
}
