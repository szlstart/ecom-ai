import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import router from '@/router'

describe('phase two route contract', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('registers all user identity routes with traceability metadata', () => {
    const expected = new Map([
      ['/login', 'USR-AUTH-LOGIN-01'],
      ['/login/code', 'USR-AUTH-CODE-01'],
      ['/register', 'USR-AUTH-REGISTER-01'],
      ['/forgot-password', 'USR-AUTH-FORGOT-01'],
      ['/reset-password', 'USR-AUTH-RESET-01'],
      ['/me', 'USR-ME-01'],
      ['/me/profile', 'USR-PROFILE-01'],
      ['/me/settings/security', 'USR-SECURITY-01'],
      ['/me/settings/account-closure', 'USR-CLOSURE-01'],
      ['/me/addresses', 'USR-ADDRESS-01'],
    ])
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    for (const [path, requirementId] of expected) {
      expect(routes.get(path)?.meta.requirementId).toBe(requirementId)
    }
  })

  it('registers the phase three storefront routes with stable requirements', () => {
    const expected = new Map([
      ['/search', 'USR-SEARCH-01'],
      ['/products/:productId', 'USR-PRODUCT-01'],
      ['/products/:productId/reviews', 'USR-PRODUCT-REVIEWS-01'],
      ['/stores/:storeId', 'USR-STORE-01'],
      ['/cart', 'USR-CART-01'],
      ['/me/favorites/products', 'USR-FAVORITE-PRODUCT-01'],
      ['/me/favorites/stores', 'USR-FAVORITE-STORE-01'],
    ])
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    for (const [path, requirementId] of expected) {
      expect(routes.get(path)?.meta.requirementId).toBe(requirementId)
    }
  })

  it('keeps admin routes on the admin audience with permission metadata', () => {
    const protectedRoutes = router
      .getRoutes()
      .filter((route) => route.path.startsWith('/admin/') && !route.path.startsWith('/admin/login'))
    expect(protectedRoutes.length).toBeGreaterThanOrEqual(7)
    for (const route of protectedRoutes) {
      expect(route.meta.audience).toBe('admin')
      expect(route.meta.requiresAuth).toBe(true)
      if (route.path !== '/admin/security') expect(route.meta.requiredPermission || route.meta.requiredAnyPermission).toBeTruthy()
    }
  })

  it('registers every phase three administration route from the traceability matrix', () => {
    const expected = new Map([
      ['/admin/store-certifications', 'ADM-CERT-LIST-01'],
      ['/admin/store-certifications/:certificationId', 'ADM-STORE-01'],
      ['/admin/stores', 'ADM-STORE-LIST-01'],
      ['/admin/stores/:storeId', 'ADM-STORE-DETAIL-01'],
      ['/admin/stores/:storeId/policies', 'ADM-POLICY-01'],
      ['/admin/products', 'ADM-PRODUCT-LIST-01'],
      ['/admin/products/new', 'ADM-PRODUCT-NEW-01'],
      ['/admin/products/import', 'ADM-PRODUCT-IMPORT-01'],
      ['/admin/products/:productId', 'ADM-PRODUCT-01'],
      ['/admin/categories', 'ADM-CATEGORY-01'],
      ['/admin/brands', 'ADM-BRAND-01'],
      ['/admin/inventories', 'ADM-INV-01'],
      ['/admin/system/jobs', 'ADM-JOB-LIST-01'],
      ['/admin/system/jobs/:jobId', 'ADM-BATCH-01'],
    ])
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    for (const [path, requirementId] of expected) {
      expect(routes.get(path)?.meta.requirementId).toBe(requirementId)
    }
  })
})
