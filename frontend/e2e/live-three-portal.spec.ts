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
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toHaveCount(0)
  await expect(page.getByText(new RegExp(`好.+，${username}`))).toBeVisible()
}

async function loginMerchant(page: Page, username: string) {
  await page.goto('/merchant')
  await page.getByLabel('商家账号').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录商家中心' }).click()
  await expect(page).toHaveURL(/\/merchant\/products$/)
}

async function loginAdministrator(page: Page, username: string) {
  await page.goto('/admin/login')
  await page.getByLabel('管理员账号').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录管理端' }).click()
  await expect(page).toHaveURL(/\/admin\/dashboard$/)
}

async function expectTrace(page: Page, panelTitle: string) {
  const trace = page.getByRole('complementary', { name: 'AI 安全执行记录' })
  await expect(trace.getByText(panelTitle)).toBeVisible()
  await expect(trace.getByText('执行时间线')).toBeVisible({ timeout: 20_000 })
  await expect(trace.getByText('隐私保护')).toBeVisible()
  await expect(trace).not.toContainText('原始思维链：')
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
    await consumer.getByRole('link', { name: '消息', exact: true }).click()
    await expect(consumer).toHaveURL(/\/messages/)
    const consumerWorkspace = consumer.getByLabel('用户消息中心')
    const incomingBubbles = consumerWorkspace.locator('.message-row.theirs .message-bubble')
    const incomingCount = await incomingBubbles.count()
    await consumerWorkspace.getByPlaceholder('输入消息…').fill('请介绍验收商品，并说明你使用了什么可信依据。')
    await consumerWorkspace.getByRole('button', { name: '发送', exact: true }).click()
    await expect(incomingBubbles).toHaveCount(incomingCount + 1, { timeout: 20_000 })
    await expect(incomingBubbles.last()).toContainText(/商品|暂无.*在售/, { timeout: 20_000 })
    await expectTrace(consumer, 'AI 工作记录')
    await consumerContext.close()

    const merchantContext = await browser.newContext()
    const merchant = await merchantContext.newPage()
    await loginMerchant(merchant, data.merchant_username)
    await merchant.getByRole('link', { name: /消息/ }).click()
    await expect(merchant).toHaveURL(/\/merchant\/messages/)
    const merchantDialog = merchant.getByLabel('商家消息中心')
    await merchantDialog.getByPlaceholder('向平台专属客服描述你的问题…').fill('请概览当前店铺商品和库存。')
    await merchantDialog.getByRole('button', { name: '发送', exact: true }).click()
    await expectTrace(merchant, 'AI 协作台')
    await merchantContext.close()

    const adminContext = await browser.newContext()
    const administrator = await adminContext.newPage()
    await loginAdministrator(administrator, data.administrator_username)
    await administrator.getByRole('link', { name: '打开消息中心' }).click()
    await expect(administrator).toHaveURL(/\/admin\/messages/)
    const adminDialog = administrator.getByLabel('管理端消息中心')
    await adminDialog.getByPlaceholder('询问平台概况、用户、店铺、订单或 Agent 运行状态…').fill('请用只读方式概览平台订单与 Agent 运行状态。')
    await adminDialog.getByRole('button', { name: '发送', exact: true }).click()
    await expectTrace(administrator, 'AI 运行轨迹')
    await adminContext.close()
  })
})
