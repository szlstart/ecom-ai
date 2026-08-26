import 'vue-router'

export {}

declare module 'vue-router' {
  interface RouteMeta {
    layout: 'storefront' | 'auth' | 'legal' | 'admin' | 'admin-auth' | 'merchant' | 'merchant-auth' | 'system'
    audience: 'public' | 'user' | 'admin' | 'merchant'
    requiresAuth: boolean
    title: string
    requirementId: string
    requiredPermission?: string
    requiredAnyPermission?: string[]
  }
}
