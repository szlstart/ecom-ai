import { expect, test, type Browser, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

type Scenario = {
  scenario_version: string
  consumer_username: string
  merchant_username: string
  administrator_username: string
  store_id: string
  product_id: string
  sku_id: string
}

const enabled = process.env.ECOM_LIVE_E2E === '1'
const password = 'Acceptance-only-password-2026!'
const frontendRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)))
const scenarioPath = path.resolve(frontendRoot, '../artifacts/acceptance/current/scenario.json')

function scenario(): Scenario {
  return JSON.parse(fs.readFileSync(scenarioPath, 'utf8')) as Scenario
}

async function loginConsumer(page: Page, username: string) {
  await page.goto('/?auth=login')
  await page.getByLabel('账号').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toHaveCount(0, { timeout: 15_000 })
  await expect(page.getByText(new RegExp(`好.+，${username}`))).toBeVisible()
}

async function loginMerchant(page: Page, username: string) {
  await page.goto('/merchant')
  await page.getByLabel('商家账号').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录商家中心' }).click()
  await expect(page).toHaveURL(/\/merchant\/products$/, { timeout: 15_000 })
}

async function loginAdministrator(page: Page, username: string) {
  await page.goto('/admin/login')
  await page.getByLabel('管理员账号').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录管理端' }).click()
  await expect(page).toHaveURL(/\/admin\/dashboard$/, { timeout: 15_000 })
}

async function expectTrace(page: Page) {
  const trace = page.getByRole('complementary', { name: 'AI 思考过程' })
  await expect(trace.getByText('思考过程', { exact: true })).toBeVisible()
  await expect(trace.getByText('已完成', { exact: true }).first()).toBeVisible({ timeout: 20_000 })
  const analysis = trace.locator('.agent-trace-analysis')
  await expect(analysis).toHaveCount(1)
  expect(await analysis.evaluate((item) => (item as HTMLDetailsElement).open)).toBe(false)
  await expect(analysis.getByText('分析与计划', { exact: true })).toBeVisible()
  await expect(trace).toContainText('服务端根据本次实际意图、上下文、权限、工具与知识检索记录')
  await expect(trace).not.toContainText(/理解当前消息|重建最近对话上下文|生成安全回复|结果整理完成|参考内容|原始思维链：|执行时间线|运行编号|可信来源|隐私保护|Kimi 意图路由/)
}

async function expectMessageWorkspaceFitsViewport(page: Page) {
  await expect(page.locator('.message-page-heading')).toHaveCount(0)
  await expect(page.locator('.message-page-surface')).toBeVisible()
  await expect.poll(() => page.evaluate(() => (
    document.documentElement.scrollHeight <= window.innerHeight + 1
    && document.body.scrollHeight <= window.innerHeight + 1
  ))).toBe(true)
}

test.describe('LIVE-THREE-PORTAL connected acceptance', () => {
  test.skip(!enabled, 'set ECOM_LIVE_E2E=1 to exercise the real FastAPI test stack')
  test.describe.configure({ mode: 'serial' })

  test('LIVE-COMMERCE-BROWSER completes shopper payment and exposes it to merchant and admin', async ({ browser, isMobile }) => {
    test.setTimeout(60_000)
    test.skip(isMobile, 'the connected acceptance uses the desktop three-column workspaces')
    const data = scenario()
    expect(data.scenario_version).toBe('commerce-three-portal-v1')

    const consumerContext = await browser.newContext()
    const consumer = await consumerContext.newPage()
    await loginConsumer(consumer, data.consumer_username)
    await consumer.goto('/me/settings/security')
    await expect(consumer.getByRole('heading', { name: '账号安全' })).toBeVisible()
    await expect(consumer.getByRole('button', { name: '用户头像粘贴上传区' })).toBeVisible()
    await expect(consumer.getByRole('button', { name: '保存头像' })).toBeDisabled()
    await consumer.goto(`/products/${data.product_id}?sku_id=${data.sku_id}`)
    await expect(consumer.getByRole('heading', { name: '三端联动验收笔记本' })).toBeVisible()
    await expect(consumer.getByText('支付总额', { exact: true })).toBeVisible()
    await consumer.getByRole('button', { name: '加入购物车', exact: true }).click()
    await expect(consumer.getByText(/已加入购物车/)).toBeVisible()
    await consumer.getByRole('link', { name: '查看购物车' }).click()
    await expect(consumer.getByRole('heading', { name: '我的购物车' })).toBeVisible()
    await expect(consumer.getByText('验收文具店')).toBeVisible()
    await expect(consumer.getByText('三端联动验收笔记本').first()).toBeVisible()
    const cartSelection = consumer.getByRole('checkbox', { name: '选择 三端联动验收笔记本' })
    if (!await cartSelection.isChecked()) await cartSelection.check()
    const checkout = consumer.getByRole('button', { name: '去结算' })
    await expect(checkout).toBeEnabled()
    await checkout.click()
    const checkoutDialog = consumer.getByRole('dialog', { name: '确认所选商品' })
    await expect(checkoutDialog.getByRole('heading', { name: '收货信息' })).toBeVisible()
    await expect(checkoutDialog.getByText('配送方式：邮寄')).toBeVisible()
    await expect(checkoutDialog.locator('.delivery-summary').getByText('包邮', { exact: true })).toBeVisible()
    const submit = checkoutDialog.getByRole('button', { name: '提交订单' })
    await expect(submit).toBeEnabled()
    await submit.click()
    await expect(consumer).toHaveURL(/\/pay\/trd_/)
    await expect(consumer.getByRole('heading', { name: '支付订单' })).toBeVisible()
    await consumer.getByRole('button', { name: /确认支付/ }).click()
    await expect(consumer).toHaveURL(/\/payments\/pay_.+\/result/)
    await expect(consumer.getByRole('heading', { name: '支付成功' })).toBeVisible()
    await consumer.getByRole('link', { name: '查看我的订单' }).click()
    await expect(consumer.getByRole('heading', { name: '我的订单' })).toBeVisible()
    await expect(consumer.getByText('三端联动验收笔记本').first()).toBeVisible()
    await consumerContext.close()

    const merchantContext = await browser.newContext()
    const merchant = await merchantContext.newPage()
    await loginMerchant(merchant, data.merchant_username)
    await expect(merchant.getByText('三端联动验收笔记本').first()).toBeVisible()
    await merchant.getByRole('link', { name: '我的订单' }).click()
    await expect(merchant.getByRole('heading', { name: '我的订单' })).toBeVisible()
    await expect(merchant.getByText('三端联动验收笔记本').first()).toBeVisible()
    await merchantContext.close()

    const adminContext = await browser.newContext()
    const administrator = await adminContext.newPage()
    await loginAdministrator(administrator, data.administrator_username)
    await administrator.goto('/admin/stores')
    await expect(administrator.getByRole('heading', { name: '店铺与商家' })).toBeVisible()
    await administrator.getByRole('link', { name: /验收文具店/ }).click()
    await expect(administrator.getByRole('heading', { name: '验收文具店' })).toBeVisible()
    await expect(administrator.getByText('三端联动验收笔记本').first()).toBeVisible()
    await administrator.getByRole('button', { name: /店铺的订单/ }).click()
    await expect(administrator.getByText('三端联动验收笔记本').first()).toBeVisible()
    await adminContext.close()
  })

  test('LIVE-AGENT-BROWSER runs all three controlled agents with auditable trace summaries', async ({ browser, isMobile }) => {
    test.setTimeout(90_000)
    test.skip(isMobile, 'the trace rail is intentionally hidden below desktop width')
    const data = scenario()

    const consumerContext = await browser.newContext()
    const consumer = await consumerContext.newPage()
    await loginConsumer(consumer, data.consumer_username)
    await consumer.goto(`/products/${data.product_id}?sku_id=${data.sku_id}`)
    await consumer.getByRole('button', { name: '联系客服', exact: true }).click()
    await expect(consumer).toHaveURL(/\/messages\//)
    await expectMessageWorkspaceFitsViewport(consumer)
    const consumerWorkspace = consumer.getByLabel('用户消息中心')
    await expect(consumerWorkspace.getByRole('button', { name: '删除对话' })).toBeVisible()
    const attachmentButton = consumerWorkspace.getByRole('button', { name: '发送商品或订单' })
    await expect(attachmentButton).toBeEnabled()
    await attachmentButton.click()
    const attachmentDialog = consumer.getByRole('dialog', { name: '发送本店商品或订单' })
    await expect(attachmentDialog).toBeVisible()
    await expect(attachmentDialog.getByText('三端联动验收笔记本').first()).toBeVisible()
    await expect(consumer.getByText('请求字段校验失败。')).toHaveCount(0)
    await attachmentDialog.getByRole('button', { name: '关闭' }).click()
    await consumerWorkspace.getByRole('button', { name: /专属客服/ }).click()
    await expect(consumerWorkspace.getByText('售后协助授权', { exact: true })).toBeVisible()
    expect(await consumerWorkspace.locator('.agent-consent-card').evaluate((element) => (element as HTMLDetailsElement).open)).toBe(false)
    const incomingBubbles = consumerWorkspace.locator('.message-row.theirs .message-bubble')
    const incomingCount = await incomingBubbles.count()
    await consumerWorkspace.getByPlaceholder('输入消息…').fill('请介绍验收商品，并说明你使用了什么可信依据。')
    await consumerWorkspace.getByRole('button', { name: '发送', exact: true }).click()
    await expect(incomingBubbles).toHaveCount(incomingCount + 1, { timeout: 20_000 })
    await expect(incomingBubbles.last()).toContainText(/商品|暂无.*在售/, { timeout: 20_000 })
    await expectTrace(consumer)
    await consumerContext.close()

    const merchantContext = await browser.newContext()
    const merchant = await merchantContext.newPage()
    await loginMerchant(merchant, data.merchant_username)
    await merchant.getByRole('link', { name: /消息/ }).click()
    await expect(merchant).toHaveURL(/\/merchant\/messages/)
    await expectMessageWorkspaceFitsViewport(merchant)
    const merchantDialog = merchant.getByLabel('商家消息中心')
    await expect(merchantDialog.getByRole('button', { name: '删除对话' })).toBeVisible()
    await merchantDialog.getByPlaceholder('向专属客服描述经营问题…').fill('请概览当前店铺商品和库存。')
    await merchantDialog.getByRole('button', { name: '发送', exact: true }).click()
    await expectTrace(merchant)
    await merchantContext.close()

    const adminContext = await browser.newContext()
    const administrator = await adminContext.newPage()
    await loginAdministrator(administrator, data.administrator_username)
    await administrator.getByRole('link', { name: '打开消息中心' }).click()
    await expect(administrator).toHaveURL(/\/admin\/messages/)
    await expectMessageWorkspaceFitsViewport(administrator)
    const adminDialog = administrator.getByLabel('管理端消息中心')
    await expect(adminDialog.getByRole('button', { name: '删除对话' })).toBeVisible()
    await adminDialog.getByPlaceholder('询问平台概况、用户、店铺、订单或 Agent 运行状态…').fill('请用只读方式概览平台订单与 Agent 运行状态。')
    await adminDialog.getByRole('button', { name: '发送', exact: true }).click()
    await expectTrace(administrator)
    await adminContext.close()
  })
})
