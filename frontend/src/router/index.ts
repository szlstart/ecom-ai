import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAdminAuthStore } from '@/stores/admin-auth'
import { useUserAuthStore } from '@/stores/user-auth'

const authMeta = { layout: 'auth', audience: 'user', requiresAuth: false } as const
const userMeta = { layout: 'storefront', audience: 'user', requiresAuth: true } as const
const adminMeta = { layout: 'admin', audience: 'admin', requiresAuth: true } as const

const routes: RouteRecordRaw[] = [
  { path: '/', component: () => import('@/layouts/StorefrontLayout.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '在线商城', requirementId: 'USR-HOME-01' }, children: [
    { path: '', component: () => import('@/pages/HomePage.vue') },
    { path: 'me', component: () => import('@/pages/me/MyDashboardPage.vue'), meta: { ...userMeta, title: '我的', requirementId: 'USR-ME-01' } },
    { path: 'me/profile', component: () => import('@/pages/me/ProfilePage.vue'), meta: { ...userMeta, title: '个人信息', requirementId: 'USR-PROFILE-01' } },
    { path: 'me/settings/security', component: () => import('@/pages/me/SecuritySettingsPage.vue'), meta: { ...userMeta, title: '账号安全', requirementId: 'USR-SECURITY-01' } },
    { path: 'me/settings/account-closure', component: () => import('@/pages/me/AccountClosurePage.vue'), meta: { ...userMeta, title: '账号注销', requirementId: 'USR-CLOSURE-01' } },
    { path: 'me/addresses', component: () => import('@/pages/me/AddressListPage.vue'), meta: { ...userMeta, title: '收货地址', requirementId: 'USR-ADDRESS-01' } },
  ] },
  { path: '/login', component: () => import('@/layouts/AuthLayout.vue'), meta: { ...authMeta, title: '登录', requirementId: 'USR-AUTH-LOGIN-01' }, children: [{ path: '', component: () => import('@/pages/LoginPage.vue') }] },
  { path: '/login/code', component: () => import('@/layouts/AuthLayout.vue'), meta: { ...authMeta, title: '验证码登录', requirementId: 'USR-AUTH-CODE-01' }, children: [{ path: '', component: () => import('@/pages/CodeLoginPage.vue') }] },
  { path: '/register', component: () => import('@/layouts/AuthLayout.vue'), meta: { ...authMeta, title: '注册', requirementId: 'USR-AUTH-REGISTER-01' }, children: [{ path: '', component: () => import('@/pages/RegisterPage.vue') }] },
  { path: '/forgot-password', component: () => import('@/layouts/AuthLayout.vue'), meta: { ...authMeta, title: '找回密码', requirementId: 'USR-AUTH-FORGOT-01' }, children: [{ path: '', component: () => import('@/pages/ForgotPasswordPage.vue') }] },
  { path: '/reset-password', component: () => import('@/layouts/AuthLayout.vue'), meta: { ...authMeta, title: '重置密码', requirementId: 'USR-AUTH-RESET-01' }, children: [{ path: '', component: () => import('@/pages/ResetPasswordPage.vue') }] },
  { path: '/legal/:documentType', component: () => import('@/layouts/LegalLayout.vue'), meta: { layout: 'legal', audience: 'public', requiresAuth: false, title: '协议文档', requirementId: 'USR-LEGAL-01' }, children: [{ path: '', component: () => import('@/pages/LegalDocumentPage.vue') }] },
  { path: '/admin/login', component: () => import('@/layouts/AdminAuthLayout.vue'), meta: { layout: 'admin-auth', audience: 'admin', requiresAuth: false, title: '管理端登录', requirementId: 'ADM-AUTH-01' }, children: [{ path: '', component: () => import('@/pages/admin/AdminLoginPage.vue') }] },
  { path: '/admin/login/mfa', component: () => import('@/layouts/AdminAuthLayout.vue'), meta: { layout: 'admin-auth', audience: 'admin', requiresAuth: false, title: '管理端安全验证', requirementId: 'ADM-AUTH-02' }, children: [{ path: '', component: () => import('@/pages/admin/AdminMfaPage.vue') }] },
  { path: '/admin', component: () => import('@/layouts/AdminLayout.vue'), meta: { ...adminMeta, title: '管理后台', requirementId: 'ADM-SHELL-01' }, children: [
    { path: 'dashboard', component: () => import('@/pages/admin/AdminDashboardPage.vue'), meta: { ...adminMeta, title: '管理仪表盘', requirementId: 'ADM-DASH-01', requiredPermission: 'dashboard:read' } },
    { path: 'users', component: () => import('@/pages/admin/AdminUserListPage.vue'), meta: { ...adminMeta, title: '用户治理', requirementId: 'ADM-USER-LIST-01', requiredPermission: 'users:read' } },
    { path: 'users/:userId', component: () => import('@/pages/admin/AdminUserDetailPage.vue'), meta: { ...adminMeta, title: '用户详情', requirementId: 'ADM-USER-01', requiredPermission: 'users:read' } },
    { path: 'roles', component: () => import('@/pages/admin/AdminRoleListPage.vue'), meta: { ...adminMeta, title: '角色权限', requirementId: 'ADM-ROLE-LIST-01', requiredPermission: 'rbac:read' } },
    { path: 'roles/:roleId', component: () => import('@/pages/admin/AdminRoleDetailPage.vue'), meta: { ...adminMeta, title: '角色详情', requirementId: 'ADM-ROLE-01', requiredPermission: 'rbac:read' } },
    { path: 'approval-requests', component: () => import('@/pages/admin/AdminApprovalListPage.vue'), meta: { ...adminMeta, title: '审批中心', requirementId: 'ADM-APPROVAL-LIST-01', requiredPermission: 'admin_approvals:read' } },
    { path: 'approval-requests/:approvalRequestId', component: () => import('@/pages/admin/AdminApprovalDetailPage.vue'), meta: { ...adminMeta, title: '审批详情', requirementId: 'ADM-APPROVAL-01', requiredPermission: 'admin_approvals:read' } },
    { path: 'audit-logs', component: () => import('@/pages/admin/AdminAuditLogPage.vue'), meta: { ...adminMeta, title: '审计日志', requirementId: 'ADM-AUDIT-01', requiredPermission: 'audit:read' } },
    { path: 'security', component: () => import('@/pages/admin/AdminSecurityPage.vue'), meta: { ...adminMeta, title: '管理身份安全', requirementId: 'ADM-AUTH-03' } },
  ] },
  { path: '/:pathMatch(.*)*', component: () => import('@/layouts/SystemLayout.vue'), meta: { layout: 'system', audience: 'public', requiresAuth: false, title: '页面不存在', requirementId: 'USR-SYSTEM-404' }, children: [{ path: '', component: () => import('@/pages/NotFoundPage.vue') }] },
]

const router = createRouter({ history: createWebHistory(), routes, scrollBehavior: () => ({ top: 0 }) })
router.beforeEach(async (to) => {
  if (!to.meta.requiresAuth) return true
  if (to.meta.audience === 'user') {
    const auth = useUserAuthStore()
    if (!auth.isAuthenticated && !(await auth.refresh())) return { path: '/login', query: { redirect: to.fullPath } }
  }
  if (to.meta.audience === 'admin') {
    const auth = useAdminAuthStore()
    if (!auth.isAuthenticated && !(await auth.refresh())) return { path: '/admin/login' }
    if (to.meta.requiredPermission && !auth.has(to.meta.requiredPermission)) return { path: '/admin/dashboard', query: { denied: to.meta.requiredPermission } }
  }
  return true
})
router.afterEach((to) => { document.title = to.meta.title })
export default router
