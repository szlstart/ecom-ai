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
      if (route.path !== '/admin/security') expect(route.meta.requiredPermission).toBeTruthy()
    }
  })
})
