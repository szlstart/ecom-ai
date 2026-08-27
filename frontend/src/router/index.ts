import { createRouter, createWebHistory, type RouteRecordRaw, type RouterScrollBehavior } from 'vue-router'
import { useAdminAuthStore } from '@/stores/admin-auth'
import { useUserAuthStore } from '@/stores/user-auth'

const authMeta = { layout: 'auth', audience: 'user', requiresAuth: false } as const
const userMeta = { layout: 'storefront', audience: 'user', requiresAuth: true } as const
const adminMeta = { layout: 'admin', audience: 'admin', requiresAuth: true } as const
const merchantMeta = { layout: 'merchant', audience: 'merchant', requiresAuth: true } as const

const routes: RouteRecordRaw[] = [
  { path: '/', component: () => import('@/layouts/StorefrontLayout.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '在线商城', requirementId: 'USR-HOME-01' }, children: [
    { path: '', component: () => import('@/pages/HomePage.vue') },
    { path: 'search', component: () => import('@/pages/ProductSearchPage.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '搜索商品', requirementId: 'USR-SEARCH-01' } },
    { path: 'products/:productId', component: () => import('@/pages/ProductDetailPage.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '商品详情', requirementId: 'USR-PRODUCT-01' } },
    { path: 'products/:productId/reviews', component: () => import('@/pages/ProductReviewsPage.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '商品评价', requirementId: 'USR-PRODUCT-REVIEWS-01' } },
    { path: 'stores/:storeId', component: () => import('@/pages/StorePage.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '店铺', requirementId: 'USR-STORE-01' } },
    { path: 'help', component: () => import('@/pages/HelpCenterPage.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '帮助中心', requirementId: 'USR-CONTENT-01' } },
    { path: 'help/:contentKey', component: () => import('@/pages/HelpArticlePage.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '帮助文章', requirementId: 'USR-CONTENT-02' } },
    { path: 'about', component: () => import('@/pages/AboutPage.vue'), meta: { layout: 'storefront', audience: 'public', requiresAuth: false, title: '关于平台', requirementId: 'USR-CONTENT-03' } },
    { path: 'cart', component: () => import('@/pages/CartPage.vue'), meta: { ...userMeta, title: '购物车', requirementId: 'USR-CART-01' } },
    { path: 'checkout/:checkoutId', component: () => import('@/pages/CheckoutPage.vue'), meta: { ...userMeta, title: '结算', requirementId: 'USR-CHECKOUT-01' } },
    { path: 'pay/:tradeOrderId', name: 'payment-cashier', component: () => import('@/pages/PaymentCashierPage.vue'), meta: { ...userMeta, title: '支付订单', requirementId: 'USR-PAY-01' } },
    { path: 'payments/:paymentId/result', name: 'payment-result', component: () => import('@/pages/PaymentResultPage.vue'), meta: { ...userMeta, title: '支付结果', requirementId: 'USR-PAY-02' } },
    { path: 'messages', component: () => import('@/pages/MessagePopupRedirectPage.vue'), meta: { ...userMeta, title: '消息', requirementId: 'USR-MSG-01' } },
    { path: 'messages/:conversationId', component: () => import('@/pages/MessagePopupRedirectPage.vue'), meta: { ...userMeta, title: '会话', requirementId: 'USR-MSG-02' } },
    { path: 'me', component: () => import('@/pages/me/MyDashboardPage.vue'), meta: { ...userMeta, title: '我的', requirementId: 'USR-ME-01' } },
    { path: 'me/profile', component: () => import('@/pages/me/ProfilePage.vue'), meta: { ...userMeta, title: '个人信息', requirementId: 'USR-PROFILE-01' } },
    { path: 'me/wallet', component: () => import('@/pages/me/WalletPage.vue'), meta: { ...userMeta, title: '账户余额', requirementId: 'USR-WALLET-01' } },
    { path: 'me/settings/security', component: () => import('@/pages/me/SecuritySettingsPage.vue'), meta: { ...userMeta, title: '账号安全', requirementId: 'USR-SECURITY-01' } },
    { path: 'me/settings/ai-personalization', component: () => import('@/pages/me/AiPersonalizationPage.vue'), meta: { ...userMeta, title: 'AI 个性化与记忆', requirementId: 'USR-AI-PRIVACY-01' } },
    { path: 'me/settings/account-closure', component: () => import('@/pages/me/AccountClosurePage.vue'), meta: { ...userMeta, title: '账号注销', requirementId: 'USR-CLOSURE-01' } },
    { path: 'me/addresses', component: () => import('@/pages/me/AddressListPage.vue'), meta: { ...userMeta, title: '收货地址', requirementId: 'USR-ADDRESS-01' } },
    { path: 'me/orders', name: 'my-orders', component: () => import('@/pages/me/OrderListPage.vue'), meta: { ...userMeta, title: '我的订单', requirementId: 'USR-ORDER-01' } },
    { path: 'me/orders/:orderId', name: 'my-order-detail', component: () => import('@/pages/me/OrderDetailPage.vue'), meta: { ...userMeta, title: '订单详情', requirementId: 'USR-ORDER-02' } },
    { path: 'me/orders/:orderId/logistics', name: 'my-order-logistics', component: () => import('@/pages/me/OrderLogisticsPage.vue'), meta: { ...userMeta, title: '订单物流', requirementId: 'USR-SHIP-01' } },
    { path: 'me/shipments/:shipmentId', component: () => import('@/pages/me/ShipmentDetailPage.vue'), meta: { ...userMeta, title: '物流详情', requirementId: 'USR-SHIP-02' } },
    { path: 'me/reviews', name: 'my-reviews', component: () => import('@/pages/me/MyReviewsPage.vue'), meta: { ...userMeta, title: '我的评价', requirementId: 'USR-REVIEW-01' } },
    { path: 'me/order-items/:orderItemId/review', name: 'my-review-create', component: () => import('@/pages/me/ReviewCreatePage.vue'), meta: { ...userMeta, title: '发表评价', requirementId: 'USR-REVIEW-02' } },
    { path: 'me/reviews/:reviewId/edit', component: () => import('@/pages/me/ReviewEditPage.vue'), meta: { ...userMeta, title: '编辑评价', requirementId: 'USR-REVIEW-03' } },
    { path: 'me/reviews/:reviewId/append', component: () => import('@/pages/me/ReviewAppendPage.vue'), meta: { ...userMeta, title: '追加评价', requirementId: 'USR-REVIEW-04' } },
    { path: 'me/orders/:orderId/refund', name: 'refund-application', component: () => import('@/pages/me/RefundApplicationPage.vue'), meta: { ...userMeta, title: '申请售后', requirementId: 'USR-REFUND-02' } },
    { path: 'me/after-sales', name: 'my-after-sales', component: () => import('@/pages/me/AfterSaleListPage.vue'), meta: { ...userMeta, title: '我的售后', requirementId: 'USR-REFUND-01' } },
    { path: 'me/after-sales/:refundId', name: 'my-after-sale-detail', component: () => import('@/pages/me/AfterSaleDetailPage.vue'), meta: { ...userMeta, title: '售后详情', requirementId: 'USR-REFUND-03' } },
    { path: 'me/after-sales/:refundId/return-shipment', component: () => import('@/pages/me/ReturnShipmentPage.vue'), meta: { ...userMeta, title: '填写退货物流', requirementId: 'USR-REFUND-04' } },
    { path: 'me/appeals/:appealId', component: () => import('@/pages/me/RefundAppealPage.vue'), meta: { ...userMeta, title: '售后申诉', requirementId: 'USR-APPEAL-01' } },
    { path: 'me/favorites/products', component: () => import('@/pages/me/FavoriteProductsPage.vue'), meta: { ...userMeta, title: '商品收藏', requirementId: 'USR-FAVORITE-PRODUCT-01' } },
    { path: 'me/favorites/stores', component: () => import('@/pages/me/FollowedStoresPage.vue'), meta: { ...userMeta, title: '店铺收藏', requirementId: 'USR-FAVORITE-STORE-01' } },
  ] },
  { path: '/login', redirect: (to) => ({ path: '/', query: { ...to.query, auth: 'login' } }), meta: { ...authMeta, title: '登录', requirementId: 'USR-AUTH-LOGIN-01' } },
  { path: '/register', redirect: (to) => ({ path: '/', query: { ...to.query, auth: 'register' } }), meta: { ...authMeta, title: '注册', requirementId: 'USR-AUTH-REGISTER-01' } },
  { path: '/forgot-password', component: () => import('@/layouts/AuthLayout.vue'), meta: { ...authMeta, title: '找回密码', requirementId: 'USR-AUTH-FORGOT-01' }, children: [{ path: '', component: () => import('@/pages/ForgotPasswordPage.vue') }] },
  { path: '/reset-password', component: () => import('@/layouts/AuthLayout.vue'), meta: { ...authMeta, title: '重置密码', requirementId: 'USR-AUTH-RESET-01' }, children: [{ path: '', component: () => import('@/pages/ResetPasswordPage.vue') }] },
  { path: '/legal/:documentType', component: () => import('@/layouts/LegalLayout.vue'), meta: { layout: 'legal', audience: 'public', requiresAuth: false, title: '协议文档', requirementId: 'USR-LEGAL-01' }, children: [{ path: '', component: () => import('@/pages/LegalDocumentPage.vue') }] },
  { path: '/403', component: () => import('@/layouts/SystemLayout.vue'), meta: { layout: 'system', audience: 'public', requiresAuth: false, title: '无权访问', requirementId: 'USR-SYSTEM-403' }, children: [{ path: '', component: () => import('@/pages/ForbiddenPage.vue') }] },
  { path: '/gone', component: () => import('@/layouts/SystemLayout.vue'), meta: { layout: 'system', audience: 'public', requiresAuth: false, title: '内容已失效', requirementId: 'USR-SYSTEM-GONE' }, children: [{ path: '', component: () => import('@/pages/GonePage.vue') }] },
  { path: '/error', component: () => import('@/layouts/SystemLayout.vue'), meta: { layout: 'system', audience: 'public', requiresAuth: false, title: '系统异常', requirementId: 'USR-SYSTEM-ERROR' }, children: [{ path: '', component: () => import('@/pages/SystemErrorPage.vue') }] },
  { path: '/maintenance', component: () => import('@/layouts/SystemLayout.vue'), meta: { layout: 'system', audience: 'public', requiresAuth: false, title: '系统维护', requirementId: 'USR-SYSTEM-MAINT' }, children: [{ path: '', component: () => import('@/pages/MaintenancePage.vue') }] },
  { path: '/merchant/login', redirect: (to) => ({ path: '/merchant', query: to.query }) },
  { path: '/merchant/reauthenticate', component: () => import('@/layouts/MerchantAuthLayout.vue'), meta: { layout: 'merchant-auth', audience: 'merchant', requiresAuth: true, title: '确认商家密码', requirementId: 'MCH-AUTH-03' }, children: [{ path: '', component: () => import('@/pages/merchant/MerchantReauthenticatePage.vue') }] },
  { path: '/merchant', component: () => import('@/layouts/MerchantGatewayLayout.vue'), meta: { layout: 'merchant-auth', audience: 'merchant', requiresAuth: false, title: '商家登录与注册', requirementId: 'MCH-AUTH-01' }, children: [
    { path: '', component: () => import('@/pages/merchant/MerchantLoginPage.vue'), meta: { layout: 'merchant-auth', audience: 'merchant', requiresAuth: false, title: '商家登录与注册', requirementId: 'MCH-AUTH-01' } },
    { path: 'dashboard', component: () => import('@/pages/merchant/MerchantProductListPage.vue'), meta: { ...merchantMeta, title: '我的商品', requirementId: 'MCH-DASH-01', requiredPermission: 'stores:read' } },
    { path: 'products', component: () => import('@/pages/merchant/MerchantProductListPage.vue'), meta: { ...merchantMeta, title: '商品管理', requirementId: 'MCH-PRODUCT-LIST-01', requiredPermission: 'products:read' } },
    { path: 'products/new', component: () => import('@/pages/merchant/MerchantProductEditorPage.vue'), meta: { ...merchantMeta, title: '新增商品', requirementId: 'MCH-PRODUCT-NEW-01', requiredPermission: 'products:create' } },
    { path: 'products/:productId', component: () => import('@/pages/merchant/MerchantProductEditorPage.vue'), meta: { ...merchantMeta, title: '编辑商品', requirementId: 'MCH-PRODUCT-EDIT-01', requiredPermission: 'products:read' } },
    { path: 'orders', component: () => import('@/pages/merchant/MerchantOrderListPage.vue'), meta: { ...merchantMeta, title: '我的订单', requirementId: 'MCH-ORDER-01', requiredPermission: 'orders:read' } },
    { path: 'inventory', component: () => import('@/pages/admin/AdminInventoryPage.vue'), props: { portal: 'merchant' }, meta: { ...merchantMeta, title: '库存管理', requirementId: 'MCH-INVENTORY-01', requiredPermission: 'inventories:read' } },
    { path: 'support', component: () => import('@/pages/merchant/MerchantSupportListPage.vue'), meta: { ...merchantMeta, title: '客户咨询', requirementId: 'MCH-SUPPORT-LIST-01', requiredPermission: 'support:queue_read' } },
    { path: 'support/:ticketId', component: () => import('@/pages/admin/AdminSupportWorkspacePage.vue'), props: { portal: 'merchant' }, meta: { ...merchantMeta, title: '客服会话', requirementId: 'MCH-SUPPORT-01', requiredPermission: 'support:queue_read' } },
    { path: 'reviews', component: () => import('@/pages/merchant/MerchantReviewListPage.vue'), meta: { ...merchantMeta, title: '评价回复', requirementId: 'MCH-REVIEW-LIST-01', requiredPermission: 'reviews:read' } },
    { path: 'reviews/:reviewId', component: () => import('@/pages/merchant/MerchantReviewDetailPage.vue'), meta: { ...merchantMeta, title: '回复评价', requirementId: 'MCH-REVIEW-01', requiredPermission: 'reviews:read' } },
    { path: 'store', component: () => import('@/pages/merchant/MerchantStorePage.vue'), meta: { ...merchantMeta, title: '店铺资料', requirementId: 'MCH-STORE-01', requiredPermission: 'stores:read' } },
  ] },
  { path: '/admin/login', component: () => import('@/layouts/AdminAuthLayout.vue'), meta: { layout: 'admin-auth', audience: 'admin', requiresAuth: false, title: '管理端登录', requirementId: 'ADM-AUTH-01' }, children: [{ path: '', component: () => import('@/pages/admin/AdminLoginPage.vue') }] },
  { path: '/admin/reauthenticate', component: () => import('@/layouts/AdminAuthLayout.vue'), meta: { layout: 'admin-auth', audience: 'admin', requiresAuth: true, title: '确认管理员密码', requirementId: 'ADM-AUTH-02' }, children: [{ path: '', component: () => import('@/pages/admin/AdminReauthenticatePage.vue'), meta: { layout: 'admin-auth', audience: 'admin', requiresAuth: true, title: '确认管理员密码', requirementId: 'ADM-AUTH-02' } }] },
  { path: '/admin', component: () => import('@/layouts/AdminLayout.vue'), meta: { ...adminMeta, title: '管理后台', requirementId: 'ADM-SHELL-01' }, children: [
    { path: 'dashboard', component: () => import('@/pages/admin/AdminDashboardPage.vue'), meta: { ...adminMeta, title: '管理仪表盘', requirementId: 'ADM-DASH-01', requiredPermission: 'dashboard:read' } },
    { path: 'users', component: () => import('@/pages/admin/AdminUserListPage.vue'), meta: { ...adminMeta, title: '用户治理', requirementId: 'ADM-USER-LIST-01', requiredPermission: 'users:read' } },
    { path: 'users/:userId', component: () => import('@/pages/admin/AdminUserDetailPage.vue'), meta: { ...adminMeta, title: '用户详情', requirementId: 'ADM-USER-01', requiredPermission: 'users:read' } },
    { path: 'roles', component: () => import('@/pages/admin/AdminRoleListPage.vue'), meta: { ...adminMeta, title: '角色权限', requirementId: 'ADM-RBAC-01', requiredPermission: 'rbac:read' } },
    { path: 'roles/:roleId', component: () => import('@/pages/admin/AdminRoleDetailPage.vue'), meta: { ...adminMeta, title: '角色详情', requirementId: 'ADM-RBAC-02', requiredPermission: 'rbac:read' } },
    { path: 'approval-requests', component: () => import('@/pages/admin/AdminApprovalListPage.vue'), meta: { ...adminMeta, title: '审批中心', requirementId: 'ADM-APPROVAL-LIST-01', requiredPermission: 'admin_approvals:read' } },
    { path: 'approval-requests/:approvalRequestId', component: () => import('@/pages/admin/AdminApprovalDetailPage.vue'), meta: { ...adminMeta, title: '审批详情', requirementId: 'ADM-APPROVAL-01', requiredPermission: 'admin_approvals:read' } },
    { path: 'audit-logs', component: () => import('@/pages/admin/AdminAuditLogPage.vue'), meta: { ...adminMeta, title: '审计日志', requirementId: 'ADM-AUDIT-01', requiredPermission: 'audit:read' } },
    { path: 'security', component: () => import('@/pages/admin/AdminSecurityPage.vue'), meta: { ...adminMeta, title: '管理身份安全', requirementId: 'ADM-AUTH-03' } },
    { path: 'store-certifications', component: () => import('@/pages/admin/AdminStoreCertificationListPage.vue'), meta: { ...adminMeta, title: '店铺认证审核', requirementId: 'ADM-CERT-LIST-01', requiredAnyPermission: ['stores:read', 'stores:review'] } },
    { path: 'store-certifications/:certificationId', component: () => import('@/pages/admin/AdminStoreCertificationDetailPage.vue'), meta: { ...adminMeta, title: '店铺认证详情', requirementId: 'ADM-STORE-01', requiredPermission: 'stores:review' } },
    { path: 'stores', component: () => import('@/pages/admin/AdminStoreListPage.vue'), meta: { ...adminMeta, title: '店铺运营', requirementId: 'ADM-STORE-LIST-01', requiredPermission: 'stores:read' } },
    { path: 'stores/:storeId', component: () => import('@/pages/admin/AdminStoreDetailPage.vue'), meta: { ...adminMeta, title: '店铺运营详情', requirementId: 'ADM-STORE-DETAIL-01', requiredPermission: 'stores:read' } },
    { path: 'stores/:storeId/policies', component: () => import('@/pages/admin/AdminStorePolicyPage.vue'), meta: { ...adminMeta, title: '店铺服务政策', requirementId: 'ADM-POLICY-01', requiredPermission: 'store_policies:read' } },
    { path: 'products', component: () => import('@/pages/admin/AdminProductListPage.vue'), meta: { ...adminMeta, title: '商品管理', requirementId: 'ADM-PRODUCT-LIST-01', requiredPermission: 'products:read' } },
    { path: 'products/new', component: () => import('@/pages/admin/AdminProductEditPage.vue'), meta: { ...adminMeta, title: '新建商品', requirementId: 'ADM-PRODUCT-NEW-01', requiredPermission: 'products:create' } },
    { path: 'products/import', component: () => import('@/pages/admin/AdminProductImportPage.vue'), meta: { ...adminMeta, title: '商品批量导入', requirementId: 'ADM-PRODUCT-IMPORT-01', requiredPermission: 'products:create' } },
    { path: 'products/:productId', component: () => import('@/pages/admin/AdminProductEditPage.vue'), meta: { ...adminMeta, title: '商品编辑', requirementId: 'ADM-PRODUCT-01', requiredPermission: 'products:read' } },
    { path: 'categories', component: () => import('@/pages/admin/AdminCategoryPage.vue'), meta: { ...adminMeta, title: '平台分类', requirementId: 'ADM-CATEGORY-01', requiredPermission: 'catalog_taxonomy:manage' } },
    { path: 'brands', component: () => import('@/pages/admin/AdminBrandPage.vue'), meta: { ...adminMeta, title: '品牌管理', requirementId: 'ADM-BRAND-01', requiredPermission: 'catalog_taxonomy:manage' } },
    { path: 'inventories', component: () => import('@/pages/admin/AdminInventoryPage.vue'), meta: { ...adminMeta, title: '库存管理', requirementId: 'ADM-INV-01', requiredPermission: 'inventories:read' } },
    { path: 'orders', component: () => import('@/pages/admin/AdminOrderListPage.vue'), meta: { ...adminMeta, title: '订单管理', requirementId: 'ADM-ORDER-LIST-01', requiredPermission: 'orders:read' } },
    { path: 'orders/:orderId', component: () => import('@/pages/admin/AdminOrderDetailPage.vue'), meta: { ...adminMeta, title: '订单详情', requirementId: 'ADM-ORDER-01', requiredPermission: 'orders:read' } },
    { path: 'payments', component: () => import('@/pages/admin/AdminPaymentListPage.vue'), meta: { ...adminMeta, title: '支付管理', requirementId: 'ADM-PAYMENT-LIST-01', requiredPermission: 'payments:read' } },
    { path: 'payments/:paymentId', component: () => import('@/pages/admin/AdminPaymentDetailPage.vue'), meta: { ...adminMeta, title: '支付详情', requirementId: 'ADM-PAYMENT-01', requiredPermission: 'payments:read' } },
    { path: 'shipments/:shipmentId', component: () => import('@/pages/admin/AdminShipmentDetailPage.vue'), meta: { ...adminMeta, title: '物流包裹详情', requirementId: 'ADM-SHIP-02', requiredPermission: 'shipments:read' } },
    { path: 'refund-applications', component: () => import('@/pages/admin/AdminRefundListPage.vue'), meta: { ...adminMeta, title: '退款申请', requirementId: 'ADM-REFUND-LIST-01', requiredPermission: 'refunds:read' } },
    { path: 'refund-applications/:refundId', component: () => import('@/pages/admin/AdminRefundDetailPage.vue'), meta: { ...adminMeta, title: '退款审核', requirementId: 'ADM-REFUND-01', requiredPermission: 'refunds:read' } },
    { path: 'refund-appeals', component: () => import('@/pages/admin/AdminRefundAppealListPage.vue'), meta: { ...adminMeta, title: '退款申诉', requirementId: 'ADM-APPEAL-LIST-01', requiredPermission: 'refund_appeals:read' } },
    { path: 'refund-appeals/:appealId', component: () => import('@/pages/admin/AdminRefundAppealDetailPage.vue'), meta: { ...adminMeta, title: '申诉复核', requirementId: 'ADM-APPEAL-01', requiredPermission: 'refund_appeals:read' } },
    { path: 'reviews', component: () => import('@/pages/admin/AdminReviewListPage.vue'), meta: { ...adminMeta, title: '评价管理', requirementId: 'ADM-REVIEW-LIST-01', requiredPermission: 'reviews:read' } },
    { path: 'reviews/:reviewId', component: () => import('@/pages/admin/AdminReviewDetailPage.vue'), meta: { ...adminMeta, title: '评价治理', requirementId: 'ADM-REVIEW-01', requiredPermission: 'reviews:read' } },
    { path: 'support/tickets', component: () => import('@/pages/admin/AdminSupportTicketListPage.vue'), meta: { ...adminMeta, title: '人工客服队列', requirementId: 'ADM-SUPPORT-LIST-01', requiredPermission: 'support:queue_read' } },
    { path: 'support/tickets/:ticketId', component: () => import('@/pages/admin/AdminSupportWorkspacePage.vue'), meta: { ...adminMeta, title: '人工客服工作台', requirementId: 'ADM-SUPPORT-01', requiredPermission: 'support:queue_read' } },
    { path: 'ai/agents', component: () => import('@/pages/admin/AdminAgentPage.vue'), meta: { ...adminMeta, title: 'Agent 管理', requirementId: 'ADM-AI-AGENT-01', requiredPermission: 'ai_agents:read' } },
    { path: 'ai/skills', component: () => import('@/pages/admin/AdminSkillPage.vue'), meta: { ...adminMeta, title: 'Skill 管理', requirementId: 'ADM-AI-SKILL-01', requiredPermission: 'ai_skills:read' } },
    { path: 'ai/tools', component: () => import('@/pages/admin/AdminToolPage.vue'), meta: { ...adminMeta, title: 'MCP Tool 管理', requirementId: 'ADM-AI-TOOL-LIST-01', requiredPermission: 'ai_tools:read' } },
    { path: 'ai/tools/:toolId', component: () => import('@/pages/admin/AdminAiToolDetailPage.vue'), meta: { ...adminMeta, title: 'MCP Tool 详情', requirementId: 'ADM-AI-01', requiredPermission: 'ai_tools:read' } },
    { path: 'ai/policies', component: () => import('@/pages/admin/AdminAiPolicyPage.vue'), meta: { ...adminMeta, title: 'AI 权限策略', requirementId: 'ADM-AI-POLICY-01', requiredPermission: 'ai_policies:read' } },
    { path: 'knowledge/documents', component: () => import('@/pages/admin/AdminKnowledgePage.vue'), meta: { ...adminMeta, title: '知识库', requirementId: 'ADM-KNOW-LIST-01', requiredPermission: 'knowledge:read' } },
    { path: 'knowledge/documents/:documentId', component: () => import('@/pages/admin/AdminKnowledgeDocumentDetailPage.vue'), meta: { ...adminMeta, title: '知识文档', requirementId: 'ADM-KNOW-01', requiredPermission: 'knowledge:read' } },
    { path: 'knowledge/indexing-jobs', component: () => import('@/pages/admin/AdminJobListPage.vue'), meta: { ...adminMeta, title: '知识索引任务', requirementId: 'ADM-KNOW-JOBS-01', requiredPermission: 'knowledge:read' } },
    { path: 'knowledge/indexing-jobs/:jobId', component: () => import('@/pages/admin/AdminKnowledgeJobPage.vue'), meta: { ...adminMeta, title: '知识索引任务详情', requirementId: 'ADM-KNOW-JOB-01', requiredPermission: 'knowledge:read' } },
    { path: 'ai/evaluations', component: () => import('@/pages/admin/AdminAiEvaluationPage.vue'), meta: { ...adminMeta, title: 'AI 评估', requirementId: 'ADM-EVAL-01', requiredPermission: 'ai_evaluations:read' } },
    { path: 'ai/runs/:runId', component: () => import('@/pages/admin/AdminAiRunDetailPage.vue'), meta: { ...adminMeta, title: 'Agent Run 详情', requirementId: 'ADM-AI-RUN-01', requiredPermission: 'ai_observability:read' } },
    { path: 'observability', component: () => import('@/pages/admin/AdminObservabilityPage.vue'), meta: { ...adminMeta, title: '可观测性', requirementId: 'ADM-OBS-01', requiredPermission: 'observability:read' } },
    { path: 'content', component: () => import('@/pages/admin/AdminContentListPage.vue'), meta: { ...adminMeta, title: '平台内容', requirementId: 'ADM-CONTENT-LIST-01', requiredPermission: 'content:read' } },
    { path: 'content/new', component: () => import('@/pages/admin/AdminContentEditPage.vue'), meta: { ...adminMeta, title: '新建平台内容', requirementId: 'ADM-CONTENT-NEW-01', requiredPermission: 'content:manage' } },
    { path: 'content/:contentId', component: () => import('@/pages/admin/AdminContentEditPage.vue'), meta: { ...adminMeta, title: '编辑平台内容', requirementId: 'ADM-CONTENT-01', requiredPermission: 'content:read' } },
    { path: 'system/jobs', component: () => import('@/pages/admin/AdminJobListPage.vue'), meta: { ...adminMeta, title: '批处理任务', requirementId: 'ADM-JOB-LIST-01', requiredPermission: 'jobs:read' } },
    { path: 'system/jobs/:jobId', component: () => import('@/pages/admin/AdminJobDetailPage.vue'), meta: { ...adminMeta, title: '批处理任务详情', requirementId: 'ADM-BATCH-01', requiredAnyPermission: ['jobs:read', 'products:create'] } },
    { path: 'system/dead-letter-events', component: () => import('@/pages/admin/AdminDeadLetterListPage.vue'), meta: { ...adminMeta, title: '死信事件', requirementId: 'ADM-DLQ-LIST-01', requiredPermission: 'events:read' } },
    { path: 'system/dead-letter-events/:deadLetterId', component: () => import('@/pages/admin/AdminDeadLetterDetailPage.vue'), meta: { ...adminMeta, title: '死信详情', requirementId: 'ADM-DLQ-01', requiredPermission: 'events:read' } },
  ] },
  { path: '/:pathMatch(.*)*', component: () => import('@/layouts/SystemLayout.vue'), meta: { layout: 'system', audience: 'public', requiresAuth: false, title: '页面不存在', requirementId: 'USR-SYSTEM-404' }, children: [{ path: '', component: () => import('@/pages/NotFoundPage.vue') }] },
]

export const appScrollBehavior: RouterScrollBehavior = (to, from, savedPosition) => {
  if (savedPosition) return savedPosition
  if (to.hash) return { el: to.hash, top: 88, behavior: 'smooth' }
  // Query-only updates select a SKU, apply an in-page filter or open an auth
  // modal. They are not page navigation and must not reset the user's viewport.
  if (to.path === from.path) return false
  return { top: 0 }
}

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: appScrollBehavior,
})
router.beforeEach(async (to) => {
  if (to.path === '/merchant' || to.path === '/merchant/') {
    const auth = useAdminAuthStore()
    if (auth.isAuthenticatedFor('merchant') || await auth.refresh('merchant')) return { path: '/merchant/products' }
    return true
  }
  if (!to.meta.requiresAuth) return true
  if (to.meta.audience === 'user') {
    const auth = useUserAuthStore()
    if (!auth.isAuthenticated && !(await auth.refresh())) return { path: '/', query: { auth: 'login', redirect: to.fullPath } }
  }
  if (to.meta.audience === 'admin') {
    const auth = useAdminAuthStore()
    if (!auth.isAuthenticatedFor('admin') && !(await auth.refresh('admin'))) return { path: '/admin/login' }
    if (to.meta.requiredAnyPermission && !to.meta.requiredAnyPermission.some((permission: string) => auth.has(permission))) return { path: '/admin/dashboard', query: { denied: to.meta.requiredAnyPermission.join('|') } }
    if (to.meta.requiredPermission && !auth.has(to.meta.requiredPermission)) return { path: '/admin/dashboard', query: { denied: to.meta.requiredPermission } }
  }
  if (to.meta.audience === 'merchant') {
    const auth = useAdminAuthStore()
    if (!auth.isAuthenticatedFor('merchant') && !(await auth.refresh('merchant'))) return { path: '/merchant', query: { redirect: to.fullPath } }
    if (!auth.scopes.some((scope) => scope.scope_type === 'store')) {
      await auth.logout('merchant')
      return { path: '/merchant', query: { denied: 'store_scope' } }
    }
    if (to.meta.requiredAnyPermission && !to.meta.requiredAnyPermission.some((permission: string) => auth.has(permission))) return { path: '/merchant/dashboard', query: { denied: to.meta.requiredAnyPermission.join('|') } }
    if (to.meta.requiredPermission && !auth.has(to.meta.requiredPermission)) return { path: '/merchant/dashboard', query: { denied: to.meta.requiredPermission } }
  }
  return true
})
router.afterEach((to) => { document.title = to.meta.title })
export default router
