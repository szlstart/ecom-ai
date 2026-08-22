import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    component: () => import('@/layouts/StorefrontLayout.vue'),
    meta: {
      layout: 'storefront',
      audience: 'public',
      requiresAuth: false,
      title: '在线商城',
      requirementId: 'USR-HOME-01',
    },
    children: [{ path: '', component: () => import('@/pages/HomePage.vue') }],
  },
  {
    path: '/login',
    component: () => import('@/layouts/AuthLayout.vue'),
    meta: {
      layout: 'auth',
      audience: 'user',
      requiresAuth: false,
      title: '登录',
      requirementId: 'USR-AUTH-LOGIN-01',
    },
    children: [{ path: '', component: () => import('@/pages/LoginPage.vue') }],
  },
  {
    path: '/register',
    component: () => import('@/layouts/AuthLayout.vue'),
    meta: {
      layout: 'auth',
      audience: 'user',
      requiresAuth: false,
      title: '注册',
      requirementId: 'USR-AUTH-REGISTER-01',
    },
    children: [{ path: '', component: () => import('@/pages/RegisterPage.vue') }],
  },
  {
    path: '/admin/login',
    component: () => import('@/layouts/AdminAuthLayout.vue'),
    meta: {
      layout: 'admin-auth',
      audience: 'admin',
      requiresAuth: false,
      title: '管理端登录',
      requirementId: 'ADM-AUTH-01',
    },
    children: [{ path: '', component: () => import('@/pages/admin/AdminLoginPage.vue') }],
  },
  {
    path: '/admin/dashboard',
    component: () => import('@/layouts/AdminLayout.vue'),
    meta: {
      layout: 'admin',
      audience: 'admin',
      requiresAuth: true,
      title: '管理仪表盘',
      requirementId: 'ADM-DASH-01',
    },
    children: [{ path: '', component: () => import('@/pages/admin/AdminDashboardPage.vue') }],
  },
  {
    path: '/:pathMatch(.*)*',
    component: () => import('@/layouts/SystemLayout.vue'),
    meta: {
      layout: 'system',
      audience: 'public',
      requiresAuth: false,
      title: '页面不存在',
      requirementId: 'USR-SYSTEM-404',
    },
    children: [{ path: '', component: () => import('@/pages/NotFoundPage.vue') }],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  document.title = to.meta.title
})

export default router
