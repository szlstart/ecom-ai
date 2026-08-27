import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it } from 'vitest'

import router from '@/router'

describe('phase two route contract', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('registers all user identity routes with traceability metadata', () => {
    const expected = new Map([
      ['/login', 'USR-AUTH-LOGIN-01'],
      ['/register', 'USR-AUTH-REGISTER-01'],
      ['/forgot-password', 'USR-AUTH-FORGOT-01'],
      ['/reset-password', 'USR-AUTH-RESET-01'],
      ['/me', 'USR-ME-01'],
      ['/me/profile', 'USR-PROFILE-01'],
      ['/me/settings/security', 'USR-SECURITY-01'],
      ['/me/settings/ai-personalization', 'USR-AI-PRIVACY-01'],
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
      ['/checkout/:checkoutId', 'USR-CHECKOUT-01'],
      ['/pay/:tradeOrderId', 'USR-PAY-01'],
      ['/me/orders', 'USR-ORDER-01'],
      ['/me/orders/:orderId', 'USR-ORDER-02'],
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
      if (!['/admin/security', '/admin/reauthenticate'].includes(route.path)) expect(route.meta.requiredPermission || route.meta.requiredAnyPermission).toBeTruthy()
    }
  })

  it('keeps the merchant portal isolated from platform administration', () => {
    const expected = new Map([
      ['/merchant', 'MCH-AUTH-01'],
      ['/merchant/dashboard', 'MCH-DASH-01'],
      ['/merchant/products', 'MCH-PRODUCT-LIST-01'],
      ['/merchant/products/new', 'MCH-PRODUCT-NEW-01'],
      ['/merchant/products/:productId', 'MCH-PRODUCT-EDIT-01'],
      ['/merchant/orders', 'MCH-ORDER-01'],
      ['/merchant/after-sales', 'MCH-REFUND-LIST-01'],
      ['/merchant/after-sales/:refundId', 'MCH-REFUND-01'],
      ['/merchant/inventory', 'MCH-INVENTORY-01'],
      ['/merchant/support', 'MCH-SUPPORT-LIST-01'],
      ['/merchant/support/:ticketId', 'MCH-SUPPORT-01'],
      ['/merchant/reviews', 'MCH-REVIEW-LIST-01'],
      ['/merchant/reviews/:reviewId', 'MCH-REVIEW-01'],
      ['/merchant/store', 'MCH-STORE-01'],
    ])
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    for (const [path, requirementId] of expected) {
      const route = routes.get(path)
      expect(route?.meta.requirementId).toBe(requirementId)
      expect(route?.meta.audience).toBe('merchant')
      if (path !== '/merchant') expect(route?.meta.requiresAuth).toBe(true)
    }
    const merchantPermissions = router
      .getRoutes()
      .filter((route) => route.path.startsWith('/merchant'))
      .flatMap((route) => [route.meta.requiredPermission, ...(route.meta.requiredAnyPermission ?? [])])
      .filter(Boolean)
    expect(merchantPermissions).not.toContain('users:read')
    expect(routes.get('/merchant/dashboard')?.meta.requiredPermission).toBe('stores:read')
    expect(merchantPermissions).not.toContain('dashboard:read')
    expect(merchantPermissions).not.toContain('reviews:moderate')
    expect(merchantPermissions).not.toContain('products:review')
    expect(routes.get('/merchant/login')?.redirect).toBeTruthy()
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
      ['/admin/orders', 'ADM-ORDER-LIST-01'],
      ['/admin/orders/:orderId', 'ADM-ORDER-01'],
      ['/admin/payments', 'ADM-PAYMENT-LIST-01'],
      ['/admin/payments/:paymentId', 'ADM-PAYMENT-01'],
      ['/admin/shipments/:shipmentId', 'ADM-SHIP-02'],
      ['/admin/system/jobs', 'ADM-JOB-LIST-01'],
      ['/admin/system/jobs/:jobId', 'ADM-BATCH-01'],
      ['/admin/system/dead-letter-events', 'ADM-DLQ-LIST-01'],
      ['/admin/system/dead-letter-events/:deadLetterId', 'ADM-DLQ-01'],
    ])
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    for (const [path, requirementId] of expected) {
      expect(routes.get(path)?.meta.requirementId).toBe(requirementId)
    }
  })

  it('registers the phase six user messaging and support workspace routes', () => {
    const expected = new Map([
      ['/messages', 'USR-MSG-01'],
      ['/messages/:conversationId', 'USR-MSG-02'],
      ['/admin/support/tickets', 'ADM-SUPPORT-LIST-01'],
      ['/admin/support/tickets/:ticketId', 'ADM-SUPPORT-01'],
    ])
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    for (const [path, requirementId] of expected) {
      expect(routes.get(path)?.meta.requirementId).toBe(requirementId)
    }
  })

  it('registers phase nine AI governance and knowledge routes', () => {
    const expected = new Map([
      ['/admin/ai/agents', 'ADM-AI-AGENT-01'],
      ['/admin/ai/skills', 'ADM-AI-SKILL-01'],
      ['/admin/ai/tools', 'ADM-AI-TOOL-LIST-01'],
      ['/admin/ai/tools/:toolId', 'ADM-AI-01'],
      ['/admin/ai/policies', 'ADM-AI-POLICY-01'],
      ['/admin/knowledge/documents', 'ADM-KNOW-LIST-01'],
      ['/admin/knowledge/documents/:documentId', 'ADM-KNOW-01'],
      ['/admin/knowledge/indexing-jobs', 'ADM-KNOW-JOBS-01'],
      ['/admin/knowledge/indexing-jobs/:jobId', 'ADM-KNOW-JOB-01'],
    ])
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    for (const [path, requirementId] of expected) {
      expect(routes.get(path)?.meta.requirementId).toBe(requirementId)
    }
  })

  it('registers phase eleven evaluation and observability routes', () => {
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    expect(routes.get('/admin/ai/evaluations')?.meta.requirementId).toBe('ADM-EVAL-01')
    expect(routes.get('/admin/ai/runs/:runId')?.meta.requirementId).toBe('ADM-AI-RUN-01')
    expect(routes.get('/admin/observability')?.meta.requirementId).toBe('ADM-OBS-01')
    expect(routes.get('/admin/ai/evaluations')?.meta.requiredPermission).toBe(
      'ai_evaluations:read',
    )
    expect(routes.get('/admin/ai/runs/:runId')?.meta.requiredPermission).toBe(
      'ai_observability:read',
    )
    expect(routes.get('/admin/observability')?.meta.requiredPermission).toBe(
      'observability:read',
    )
    expect(routes.get('/admin/content')?.meta.requirementId).toBe('ADM-CONTENT-LIST-01')
    expect(routes.get('/admin/content/:contentId')?.meta.requirementId).toBe('ADM-CONTENT-01')
  })

  it('registers versioned public content routes', () => {
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    expect(routes.get('/help')?.meta.requirementId).toBe('USR-CONTENT-01')
    expect(routes.get('/help/:contentKey')?.meta.requirementId).toBe('USR-CONTENT-02')
    expect(routes.get('/about')?.meta.requirementId).toBe('USR-CONTENT-03')
  })

  it('registers deterministic recovery routes', () => {
    const routes = new Map(router.getRoutes().map((route) => [route.path, route]))
    expect(routes.get('/403')?.meta.requirementId).toBe('USR-SYSTEM-403')
    expect(routes.get('/gone')?.meta.requirementId).toBe('USR-SYSTEM-GONE')
    expect(routes.get('/error')?.meta.requirementId).toBe('USR-SYSTEM-ERROR')
    expect(routes.get('/maintenance')?.meta.requirementId).toBe('USR-SYSTEM-MAINT')
    expect(routes.get('/:pathMatch(.*)*')?.meta.requirementId).toBe('USR-SYSTEM-404')
  })
})
