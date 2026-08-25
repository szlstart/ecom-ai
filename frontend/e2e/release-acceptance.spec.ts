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
    if (url.pathname.endsWith('/auth/token-refresh')) {
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
        categories: [
          {
            category_id: 'cat_01BROWSERACCEPTANCE',
            parent_id: null,
            category_name: '数码设备',
            category_code: 'digital',
            level: 1,
            sort_order: 1,
            icon_url: null,
            children: [],
          },
        ],
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
  await assertBaselineAccessibility(page)
  await page.keyboard.press('Tab')
  await expect.poll(() => page.evaluate(() => document.activeElement?.tagName)).not.toBe('BODY')
  await page.screenshot({ path: testInfo.outputPath('home.png'), fullPage: true })
})

test('SEARCH-BROWSER preserves URL filters and exposes an empty recovery state', async ({ page }, testInfo) => {
  await page.goto('/search?q=keyboard&sort=price_asc')
  await expect(page.getByRole('heading', { name: '搜索商品' })).toBeVisible()
  await expect(page.getByLabel('关键词')).toHaveValue('keyboard')
  await expect(page.getByLabel('排序')).toHaveValue('price_asc')
  await expect(page.getByText('没有找到匹配商品')).toBeVisible()
  await assertBaselineAccessibility(page)
  await page.screenshot({ path: testInfo.outputPath('search-empty.png'), fullPage: true })
})

test('AUTH-BROWSER keeps user and admin authentication entry points keyboard reachable', async ({ page }, testInfo) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible()
  await page.getByLabel('账号').focus()
  await expect(page.getByLabel('账号')).toBeFocused()
  await assertBaselineAccessibility(page)
  await page.screenshot({ path: testInfo.outputPath('user-login.png'), fullPage: true })

  await page.goto('/admin/login')
  await expect(page.getByRole('heading', { name: '管理端登录' })).toBeVisible()
  await assertBaselineAccessibility(page)
})

test('AUTH-GUARD-BROWSER fails closed for protected user and admin routes', async ({ page }) => {
  await page.goto('/cart')
  await expect(page).toHaveURL(/\/login\?redirect=(?:%2F|\/)cart$/)
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible()

  await page.goto('/admin/dashboard')
  await expect(page).toHaveURL(/\/admin\/login$/)
  await expect(page.getByRole('heading', { name: '管理端登录' })).toBeVisible()
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
