// 不登录也能做的事：看首页、翻房源、看详情、打开助手。
import { expect, test } from '@playwright/test'

test('首页能打开，看得到搜索框和房源', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '一句话说清，就能找房' })).toBeVisible()
  await expect(page.getByPlaceholder(/中山区两室一厅/)).toBeVisible()
  // 「全部房源」那一栏至少有一套房（演示数据有一百来套）
  await expect(page.getByRole('heading', { name: '全部房源' })).toBeVisible()
  await expect(page.locator('article.card').first()).toBeVisible()
})

test('从房源列表点进详情页，标题对得上', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: /查看全部房源/ }).click()
  await expect(page).toHaveURL(/#\/list\/all/)

  const card = page.locator('article.card').first()
  await expect(card).toBeVisible()
  // 卡片里第三行起是标题，如「沙河口区黑石礁 两室一厅 66平」；取包含「平」的那一行
  const title = (await card.innerText()).split('\n').find((line) => /\d+平$/.test(line.trim()))
  await card.click()

  await expect(page).toHaveURL(/#\/house\/DL\d+/)
  await expect(page.getByRole('heading', { name: title.trim() })).toBeVisible()
  await expect(page.getByText(/元\/月/).first()).toBeVisible()
})

test('直接打开一个不存在的房源，不会白屏', async ({ page }) => {
  const errors = []
  page.on('pageerror', (err) => errors.push(err.message))
  await page.goto('/#/house/DL9999')
  await expect(page.getByRole('button', { name: '找房' })).toBeVisible()
  expect(errors).toEqual([])
})

test('租房助手面板能打开，有开场白和输入框', async ({ page }) => {
  await page.goto('/')
  // 右下角的悬浮按钮。未登录时叫「租房助手」，登录后有的地方叫「打开租房助手」，按样式类定位最稳
  await page.locator('button.chat-fab').click()
  const input = page.getByPlaceholder(/说点什么/)
  await expect(input).toBeVisible()
  // 开场白：智能体连得上时是它给的，连不上时是前端自带的那句，总之有一条助手消息
  await expect(page.locator('.chat-log .bubble.assistant').first()).toBeVisible()
})
