import { expect, test, type Browser, type Locator, type Page } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

type Scenario = {
  scenario_version: string
  consumer_username: string
  consumer_display_name: string
  merchant_username: string
  administrator_username: string
  store_name: string
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
  await page.getByLabel('用户名').fill(username)
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
  // Do not mistake the initial idle state for a completed Agent turn.  The
  // trace evidence is only stable after the streamed run reaches completion.
  await expect(trace.getByText('已完成', { exact: true }).first()).toBeVisible({ timeout: 30_000 })
  await expect(trace.locator('.agent-trace-analysis')).toHaveCount(0)
  // Completed runs expose the public reasoning summary and verified execution
  // evidence as expandable sections.  Hidden provider chain-of-thought is
  // intentionally never rendered.
  await expect(trace.locator('details')).not.toHaveCount(0)
  await expect(trace.getByText('分析与计划', { exact: true })).toBeVisible()
  await expect(trace.getByText('结果', { exact: true })).toBeVisible()
  await expect(trace).not.toContainText(/理解当前消息|重建最近对话上下文|生成安全回复|结果整理完成|参考内容|执行时间线|运行编号|可信来源|隐私保护/)
  expect(await trace.evaluate((item) => getComputedStyle(item).backgroundColor)).toBe('rgb(255, 255, 255)')
}

async function expectMessageWorkspaceFitsViewport(page: Page) {
  await expect(page.locator('.message-page-heading')).toHaveCount(0)
  await expect(page.locator('.message-page-surface')).toBeVisible()
  await expect.poll(() => page.evaluate(() => (
    document.documentElement.scrollHeight <= window.innerHeight + 1
    && document.body.scrollHeight <= window.innerHeight + 1
  ))).toBe(true)
}

async function expectControlReceivesPointer(page: Page, locator: Locator) {
  await expect(locator).toBeVisible()
  const box = await locator.boundingBox()
  expect(box).not.toBeNull()
  const hit = await page.evaluate(({ x, y }) => {
    const target = document.elementFromPoint(x, y)
    return target instanceof Element ? target.closest('button, a, input, textarea')?.textContent?.trim() ?? '' : ''
  }, { x: box!.x + box!.width / 2, y: box!.y + box!.height / 2 })
  expect(hit).toContain((await locator.textContent())?.trim() ?? '')
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
    await expect(consumer.getByRole('button', { name: '减少购买数量' })).toBeDisabled()
    await expect(consumer.getByRole('button', { name: '增加购买数量' })).toBeEnabled()
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
    test.setTimeout(150_000)
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
    await expect(consumerWorkspace.getByRole('button', { name: '清除记录' })).toBeVisible()
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
    // The empty-conversation welcome is a transient UI prompt rather than a
    // persisted incoming message.  It is replaced by the first real reply, so
    // counting it would make a successful first Agent response look unchanged.
    const incomingBubbles = consumerWorkspace.locator(
      '.message-row.theirs:not(.conversation-welcome-row) .message-bubble',
    )
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
    await expect(merchantDialog.getByRole('button', { name: '清除记录' })).toBeVisible()
    await merchantDialog.getByPlaceholder('向 AI 经营助理描述经营问题…').fill('请概览当前店铺商品和库存。')
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
    await expect(adminDialog.getByRole('button', { name: '清除记录' })).toBeVisible()
    await adminDialog.getByPlaceholder('询问平台概况、用户、店铺、订单或 Agent 运行状态…').fill('请用只读方式概览平台订单与 Agent 运行状态。')
    await adminDialog.getByRole('button', { name: '发送', exact: true }).click()
    await expectTrace(administrator)
    await adminContext.close()
  })

  test('LIVE-MERCHANT-CATALOG preserves a complete draft, uploads its SKU image, publishes directly, and cleans it up', async ({ browser, isMobile }) => {
    test.setTimeout(150_000)
    test.skip(isMobile, 'the connected catalog editor acceptance uses the desktop two-column workspace')
    const data = scenario()
    const merchantContext = await browser.newContext()
    const merchant = await merchantContext.newPage()
    const productName = `浏览器自动验收商品-${Date.now()}`
    const png = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
      'base64',
    )

    await loginMerchant(merchant, data.merchant_username)
    await merchant.goto('/merchant/products/new')
    await expect(merchant).toHaveURL(/\/merchant\/products\/prd_/, { timeout: 15_000 })
    await expect(merchant.getByText('请先新增并完成一个款式')).toBeVisible()
    await merchant.getByLabel('商品名称').fill(productName)
    await merchant.getByRole('button', { name: '＋ 新增款式' }).click()
    const skuEditor = merchant.locator('.merchant-inline-sku-form')
    await skuEditor.getByLabel('款式名称').fill('自动验收款')
    await skuEditor.getByLabel('价格（元）').fill('1.23')
    await skuEditor.getByLabel('库存').fill('5')
    await skuEditor.getByRole('button', { name: '完成', exact: true }).click()
    await expect(merchant.getByText('当前款式：自动验收款')).toBeVisible()

    const skuUpload = merchant.locator('.merchant-image-actions .file-upload-control')
    // Exercise failure recovery before the happy path. An invalid selection
    // must explain the exact format problem locally and preserve all fields.
    await skuUpload.locator('input[type=file]').setInputFiles({
      name: 'not-an-image.txt',
      mimeType: 'text/plain',
      buffer: Buffer.from('this is not an image'),
    })
    await skuUpload.getByRole('button', { name: '上传并扫描' }).click()
    await expect(skuUpload.getByRole('alert')).toContainText('不支持 text/plain 格式的文件')
    await expect(merchant.getByLabel('商品名称')).toHaveValue(productName)
    await expect(merchant.getByRole('button', { name: /自动验收款.*¥1\.23.*库存 5/ })).toBeVisible()

    await skuUpload.locator('input[type=file]').setInputFiles({
      name: 'acceptance-sku.png',
      mimeType: 'image/png',
      buffer: png,
    })
    await skuUpload.getByRole('button', { name: '上传并扫描' }).click()
    await expect(skuUpload.getByText(/文件已通过扫描/)).toBeVisible({ timeout: 35_000 })
    await expect(merchant.getByRole('button', { name: '移除当前图片' })).toBeVisible()

    await merchant.getByRole('button', { name: '＋ 新增参数' }).click()
    await merchant.getByPlaceholder('参数名').fill('验收参数')
    await merchant.getByPlaceholder('参数值').fill('验收值')
    await merchant.getByLabel('省份').selectOption({ label: '浙江省' })
    await merchant.getByLabel('城市').selectOption({ label: '杭州市' })
    await merchant.getByLabel('购买须知').fill('该商品只用于隔离环境自动验收。')
    await merchant.getByPlaceholder('输入这一段商品介绍……').fill('用于验证商品详情能够直接保存并提交自动审核。')
    const detailUpload = merchant.locator('.merchant-detail-image-insert .file-upload-control')
    await detailUpload.locator('input[type=file]').setInputFiles({
      name: 'acceptance-detail.png',
      mimeType: 'image/png',
      buffer: png,
    })
    await detailUpload.getByRole('button', { name: '上传并扫描' }).click()
    await expect(merchant.getByText('详情图片已自动保存，刷新页面也不会丢失。')).toBeVisible({ timeout: 35_000 })
    await merchant.getByRole('button', { name: '＋ 新增', exact: true }).click()
    await merchant.getByLabel('问题').fill('这是正式商品吗？')
    await merchant.getByLabel('回答').fill('不是，这是隔离环境中的自动验收商品。')

    // Save once, leave the editor and reopen it. This catches the data-loss
    // regression that previously erased long descriptions after refresh.
    await merchant.getByRole('button', { name: '暂存为草稿' }).click()
    await expect(merchant).toHaveURL(/\/merchant\/products$/)
    await merchant.getByRole('link', { name: new RegExp(productName) }).click()
    await expect(merchant.getByLabel('商品名称')).toHaveValue(productName)
    await expect(merchant.getByPlaceholder('参数名')).toHaveValue('验收参数')
    await expect(merchant.getByLabel('购买须知')).toHaveValue('该商品只用于隔离环境自动验收。')
    await expect(merchant.getByPlaceholder('输入这一段商品介绍……')).toHaveValue('用于验证商品详情能够直接保存并提交自动审核。')
    await expect(merchant.getByLabel('问题')).toHaveValue('这是正式商品吗？')

    // Change a field after the explicit draft save and publish immediately.
    // The review action must persist the latest editor state itself.
    await merchant.getByLabel('购买须知').fill('隔离环境自动验收；这次修改不再手动暂存。')
    await merchant.getByRole('button', { name: '提交并自动审核' }).click()
    await expect(merchant.getByText(/系统自动审核通过并立即上架/)).toBeVisible({ timeout: 20_000 })
    await expect(merchant.getByText('销售中', { exact: true }).first()).toBeVisible()
    await merchant.getByRole('link', { name: '← 返回我的商品' }).click()
    const card = merchant.getByRole('article').filter({ hasText: productName })
    await expect(card).toBeVisible()
    await card.getByRole('button', { name: '删除商品' }).click()
    const dialog = merchant.getByRole('alertdialog', { name: new RegExp(`${productName}.*没有产生过交易`) })
    await expect(dialog).toBeVisible()
    await dialog.getByRole('button', { name: '直接删除' }).click()
    await expect(merchant.getByText(productName, { exact: true })).toHaveCount(0)
    await merchantContext.close()
  })

  test('LIVE-HUMAN-HANDOFF routes user and merchant requests to the administrator and resumes AI afterward', async ({ browser, isMobile }) => {
    test.setTimeout(120_000)
    test.skip(isMobile, 'the connected handoff acceptance uses the desktop three-column workspaces')
    const data = scenario()
    const consumerContext = await browser.newContext()
    const consumer = await consumerContext.newPage()
    const merchantContext = await browser.newContext()
    const merchant = await merchantContext.newPage()
    const adminContext = await browser.newContext()
    const administrator = await adminContext.newPage()

    await loginConsumer(consumer, data.consumer_username)
    await consumer.goto('/messages')
    const consumerWorkspace = consumer.getByLabel('用户消息中心')
    const consumerTimeline = consumerWorkspace.getByLabel('聊天消息')
    await consumerWorkspace.getByRole('button', { name: /专属客服/ }).click()
    await consumerWorkspace.getByPlaceholder('输入消息…').fill('我要找平台人工客服，请转人工处理')
    await consumerWorkspace.getByRole('button', { name: '发送', exact: true }).click()
    await expect(consumerTimeline.getByText('正在接入人工客服，请稍候。')).toBeVisible({ timeout: 25_000 })

    await loginAdministrator(administrator, data.administrator_username)
    await administrator.goto('/admin/messages')
    const adminWorkspace = administrator.getByLabel('管理端消息中心')
    const userConversation = adminWorkspace.getByRole('button', { name: new RegExp(`${data.consumer_display_name}.*等待人工接待`) })
    await expect(userConversation).toBeVisible({ timeout: 15_000 })
    await userConversation.click()
    const userClaim = adminWorkspace.getByRole('button', { name: '领取会话' })
    await expectControlReceivesPointer(administrator, userClaim)
    await userClaim.click()
    await expect(adminWorkspace.getByPlaceholder('输入人工回复…')).toBeEnabled()
    await adminWorkspace.getByPlaceholder('输入人工回复…').fill('平台人工已接入，这是一条用户转接验收回复。')
    await adminWorkspace.getByRole('button', { name: '发送', exact: true }).click()
    await expect(consumerTimeline.getByText('平台人工已接入，这是一条用户转接验收回复。')).toBeVisible({ timeout: 15_000 })
    await adminWorkspace.getByRole('button', { name: '结束人工服务' }).click()
    await expect(consumerTimeline.getByText('人工服务已结束。如有新问题，请继续发送消息。')).toBeVisible({ timeout: 15_000 })
    await expect(consumerTimeline.getByRole('button', { name: '已解决' })).toBeVisible()
    await expect(consumerTimeline.getByRole('button', { name: '没解决' })).toBeVisible()

    await loginMerchant(merchant, data.merchant_username)
    await merchant.goto('/merchant/messages')
    const merchantWorkspace = merchant.getByLabel('商家消息中心')
    const merchantTimeline = merchantWorkspace.locator('.merchant-chat-timeline')
    await merchantWorkspace.getByPlaceholder('向 AI 经营助理描述经营问题…').fill('我要找平台人工客服，请转人工处理')
    await merchantWorkspace.getByRole('button', { name: '发送', exact: true }).click()
    await expect(merchantTimeline.getByText('正在接入人工客服，请稍候。')).toBeVisible({ timeout: 25_000 })

    await administrator.reload()
    const storeConversation = adminWorkspace.getByRole('button', { name: new RegExp(`${data.store_name}.*等待平台人工接待`) })
    await expect(storeConversation).toBeVisible({ timeout: 15_000 })
    await storeConversation.click()
    const storeClaim = adminWorkspace.getByRole('button', { name: '领取会话' })
    await expectControlReceivesPointer(administrator, storeClaim)
    await storeClaim.click()
    await expect(adminWorkspace.getByPlaceholder('输入人工回复…')).toBeEnabled()
    await adminWorkspace.getByPlaceholder('输入人工回复…').fill('平台人工已接入，这是一条商家转接验收回复。')
    await adminWorkspace.getByRole('button', { name: '发送', exact: true }).click()
    await expect(merchantTimeline.getByText('平台人工已接入，这是一条商家转接验收回复。')).toBeVisible({ timeout: 15_000 })
    await adminWorkspace.getByRole('button', { name: '结束人工服务' }).click()
    await expect(merchantTimeline.getByText('人工服务已结束。如有新问题，请继续发送消息。')).toBeVisible({ timeout: 15_000 })

    await Promise.all([consumerContext.close(), merchantContext.close(), adminContext.close()])
  })

  test('LIVE-MOBILE-MESSAGES keeps all three composers usable without horizontal overflow', async ({ browser, isMobile }) => {
    test.setTimeout(75_000)
    test.skip(!isMobile, 'this case specifically validates the narrow-screen fallback')
    const data = scenario()

    async function expectNarrowWorkspace(page: Page, workspaceLabel: string, placeholder: string) {
      await expect(page.getByLabel(workspaceLabel)).toBeVisible()
      const composer = page.getByPlaceholder(placeholder)
      await expect(composer).toBeVisible()
      await expect.poll(() => page.evaluate(() => (
        document.documentElement.scrollWidth <= window.innerWidth + 1
        && document.body.scrollWidth <= window.innerWidth + 1
      ))).toBe(true)
      const rect = await composer.boundingBox()
      expect(rect).not.toBeNull()
      expect(rect!.x).toBeGreaterThanOrEqual(0)
      expect(rect!.x + rect!.width).toBeLessThanOrEqual(413)
      await expect(page.getByRole('complementary', { name: 'AI 思考过程' })).toBeHidden()
    }

    const consumerContext = await browser.newContext()
    const consumer = await consumerContext.newPage()
    await loginConsumer(consumer, data.consumer_username)
    await consumer.goto('/messages')
    const consumerWorkspace = consumer.getByLabel('用户消息中心')
    // Narrow screens collapse the conversation list and automatically select
    // the fixed exclusive-support conversation, so the list button is not
    // expected to remain visible here.
    await expect(consumerWorkspace.getByText('专属客服', { exact: true }).first()).toBeVisible()
    await expectNarrowWorkspace(consumer, '用户消息中心', '输入消息…')
    const plus = consumerWorkspace.getByRole('button', { name: '发送商品或订单' })
    await expect(plus).toBeVisible()
    const plusRect = await plus.boundingBox()
    const inputRect = await consumerWorkspace.getByPlaceholder('输入消息…').boundingBox()
    expect(plusRect).not.toBeNull()
    expect(inputRect).not.toBeNull()
    expect(plusRect!.x + plusRect!.width).toBeLessThanOrEqual(inputRect!.x)
    await consumerContext.close()

    const merchantContext = await browser.newContext()
    const merchant = await merchantContext.newPage()
    await loginMerchant(merchant, data.merchant_username)
    await merchant.goto('/merchant/messages')
    await expectNarrowWorkspace(merchant, '商家消息中心', '向 AI 经营助理描述经营问题…')
    await merchantContext.close()

    const adminContext = await browser.newContext()
    const administrator = await adminContext.newPage()
    await loginAdministrator(administrator, data.administrator_username)
    await administrator.goto('/admin/messages')
    await expectNarrowWorkspace(administrator, '管理端消息中心', '询问平台概况、用户、店铺、订单或 Agent 运行状态…')
    await adminContext.close()
  })
})
