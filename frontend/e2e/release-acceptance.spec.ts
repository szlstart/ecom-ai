import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page, type Route } from '@playwright/test'

const responseMeta = {
  request_id: 'req_browser_acceptance',
  pagination: {
    previous_cursor: null,
    next_cursor: null,
    has_previous: false,
    has_next: false,
    limit: 20,
  },
}

async function json(route: Route, data: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(status >= 400 ? data : { data, meta: responseMeta }),
  })
}

async function installPublicApi(page: Page) {
  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname.endsWith('/auth/token-refresh') || url.pathname.endsWith('/auth/session-resume')) {
      await json(route, {
        title: 'Authentication required',
        status: 401,
        detail: '请先登录。',
        code: 'AUTHENTICATION_REQUIRED',
        request_id: 'req_browser_acceptance',
        retryable: false,
      }, 401)
      return
    }
    if (url.pathname.endsWith('/homepage')) {
      await json(route, {
        feed_version: 'browser-fixture-v1',
        announcements: [{ title: '平台公告：测试环境不会触达真实用户' }],
        banners: [{ title: '可信商品', subtitle: '浏览器验收固定数据' }],
        sections: [
          {
            section: 'recommended',
            title: '为你推荐',
            status: 'available',
            items: [],
            next_cursor: null,
            error_code: null,
          },
        ],
      })
      return
    }
    if (url.pathname.endsWith('/auth/registration-config')) {
      await json(route, {
        config_version: 'browser-v1',
        password_policy: { non_empty: true, forbid_whitespace: true },
        captcha: { captcha_id: 'browser-captcha-000001', question: '12 + 7 = ?', expires_in_seconds: 600 },
        required_agreements: [],
      })
      return
    }
    if (url.pathname.endsWith('/products')) {
      await json(route, { items: [] })
      return
    }
    if (url.pathname.endsWith('/categories') || url.pathname.endsWith('/brands')) {
      await json(route, [])
      return
    }
    await json(route, {
      title: 'Fixture not registered',
      status: 503,
      detail: '浏览器验收没有为此请求配置模拟响应。',
      code: 'BROWSER_FIXTURE_MISSING',
      request_id: 'req_browser_acceptance',
      retryable: false,
    }, 503)
  })
}

async function assertBaselineAccessibility(page: Page) {
  const results = await new AxeBuilder({ page })
    .withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'])
    .analyze()
  const blocking = results.violations.filter((item) =>
    item.impact === 'critical' || item.impact === 'serious')
  expect(blocking, JSON.stringify(blocking, null, 2)).toEqual([])
  const hasHorizontalOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > window.innerWidth + 1,
  )
  expect(hasHorizontalOverflow).toBe(false)
}

test.beforeEach(async ({ page }) => {
  await installPublicApi(page)
})

test('HOME-BROWSER renders deterministic loading-complete content without serious accessibility violations', async ({ page }, testInfo) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '找到真正适合你的商品' })).toBeVisible()
  await expect(page.getByText('平台公告：测试环境不会触达真实用户')).toBeVisible()
  await expect(page.getByRole('heading', { name: '为你推荐' })).toBeVisible()
  await expect(page.getByText('分类导航')).toHaveCount(0)
  await expect(page.getByText('热门商品')).toHaveCount(0)
  await expect(page.getByText('新品上架')).toHaveCount(0)
  await assertBaselineAccessibility(page)
  await page.keyboard.press('Tab')
  await expect.poll(() => page.evaluate(() => document.activeElement?.tagName)).not.toBe('BODY')
  await page.screenshot({ path: testInfo.outputPath('home.png'), fullPage: true })
})

test('SEARCH-BROWSER preserves URL filters and exposes an empty recovery state', async ({ page }, testInfo) => {
  await page.goto('/search?q=keyboard&sort=price_asc')
  await expect(page.getByRole('heading', { name: '搜索商品' })).toBeVisible()
  await expect(page.getByLabel('关键词')).toHaveValue('keyboard')
  await expect(page.getByRole('button', { name: '价格从低到高' })).toHaveAttribute('aria-pressed', 'true')
  await page.getByRole('button', { name: '销量排序' }).click()
  await expect(page).toHaveURL(/sort=sales/)
  await expect(page.getByRole('button', { name: '销量排序' })).toHaveAttribute('aria-pressed', 'true')
  await expect(page.getByText('没有找到匹配商品')).toBeVisible()
  await assertBaselineAccessibility(page)
  await page.screenshot({ path: testInfo.outputPath('search-empty.png'), fullPage: true })
})

test('NAV-BROWSER opens the same authentication modal for every protected navigation entry', async ({ page }) => {
  await page.goto('/')
  for (const name of ['购物车', '消息', '收藏', '收货地址', '我的']) {
    await page.getByRole('button', { name, exact: true }).click()
    await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible()
    await page.getByRole('button', { name: '关闭注册登录弹窗' }).click()
    await expect(page.getByRole('heading', { name: '欢迎回来' })).toHaveCount(0)
  }
  await page.getByRole('button', { name: '注册/登录' }).click()
  await page.getByRole('tab', { name: '注册' }).click()
  await expect(page.getByRole('heading', { name: '注册账号' })).toBeVisible()
  await expect(page.getByText('验证码：12 + 7 = ?')).toBeVisible()
  await expect(page.getByLabel('计算结果')).toBeVisible()
  await expect(page.getByText('验证方式')).toHaveCount(0)
  await expect(page.getByText('手机号或邮箱')).toHaveCount(0)
  await expect(page.getByRole('link', { name: '验证码登录' })).toHaveCount(0)
  await assertBaselineAccessibility(page)
})

test('ACCOUNT-ADDRESS-BROWSER keeps messaging active and exposes the three-level region selector', async ({ page }) => {
  await page.unroute('**/api/v1/**')
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (url.pathname.endsWith('/auth/token-refresh') || url.pathname.endsWith('/auth/session-resume')) {
      await json(route, {
        user: { user_id: 'usr_BROWSER', username: 'browser_user', nickname: '验收用户', avatar_url: null, account_status: 'active' },
        session: {
          session_id: 'ses_BROWSER', client_type: 'web', device_name: 'Playwright', audience: 'user',
          authenticated_at: '2026-08-26T00:00:00Z', last_seen_at: '2026-08-26T00:00:00Z',
          expires_at: '2026-08-27T00:00:00Z', is_current: true,
        },
        access_token: 'browser-access-token', token_type: 'Bearer', expires_in: 900, csrf_token: 'browser-csrf',
      })
      return
    }
    if (url.pathname.endsWith('/users/me/addresses')) {
      await json(route, { items: [], active_count: 0, max_count: 20, can_create: true })
      return
    }
    await json(route, {
      title: 'Fixture not registered', status: 503, detail: '未配置接口。', code: 'BROWSER_FIXTURE_MISSING',
      request_id: 'req_browser_acceptance', retryable: false,
    }, 503)
  })

  await page.context().addCookies([{
    name: 'ecom_user_csrf',
    value: 'browser-csrf',
    domain: '127.0.0.1',
    path: '/',
  }])
  await page.goto('/me/addresses')
  await expect(page.getByRole('link', { name: '消息', exact: true })).toBeVisible()
  await expect(page.getByText(/(早上|中午|下午|晚上)好.+，browser_user/)).toBeVisible()
  await page.getByRole('button', { name: '新增地址' }).click()
  await expect(page.getByLabel('省份').locator('option')).toHaveCount(33)
  await page.getByLabel('省份').selectOption('440000')
  await page.getByLabel('城市').selectOption('440300')
  await expect(page.getByLabel('区 / 县')).toBeEnabled()
  await expect(page.getByText('省代码')).toHaveCount(0)
  await expect(page.getByText('邮编')).toHaveCount(0)
  await expect(page.getByText('标签')).toHaveCount(0)
  await assertBaselineAccessibility(page)
})

test('AUTH-BROWSER keeps user and admin authentication entry points keyboard reachable', async ({ page }, testInfo) => {
  await page.goto('/login')
  await expect(page).toHaveURL(/\/?auth=login$/)
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible()
  await page.getByLabel('用户名').focus()
  await expect(page.getByLabel('用户名')).toBeFocused()
  await assertBaselineAccessibility(page)
  await page.screenshot({ path: testInfo.outputPath('user-login.png'), fullPage: true })

  await page.goto('/login/code')
  await expect(page.getByRole('heading', { name: '页面不存在' })).toBeVisible()

  await page.goto('/admin/login')
  await expect(page.getByRole('heading', { name: '管理员登录' })).toBeVisible()
  await assertBaselineAccessibility(page)
})

test('AUTH-GUARD-BROWSER fails closed for protected user and admin routes', async ({ page }) => {
  await page.goto('/cart')
  await expect(page).toHaveURL(/\/?auth=login&redirect=(?:%2F|\/)cart$/)
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible()

  await page.goto('/admin/dashboard')
  await expect(page).toHaveURL(/\/admin\/login$/)
  await expect(page.getByRole('heading', { name: '管理员登录' })).toBeVisible()
})

test('SYSTEM-STATE-BROWSER exposes deterministic recovery pages', async ({ page }, testInfo) => {
  const pages = [
    ['/403', '你没有访问此内容的权限'],
    ['/gone', '内容已失效或被撤回'],
    ['/error', '暂时无法完成请求'],
    ['/maintenance', '服务正在维护'],
    ['/route-does-not-exist', '页面不存在'],
  ] as const
  for (const [path, heading] of pages) {
    await page.goto(path)
    await expect(page.getByRole('heading', { name: heading })).toBeVisible()
    await assertBaselineAccessibility(page)
  }
  await page.screenshot({ path: testInfo.outputPath('not-found.png'), fullPage: true })
})
