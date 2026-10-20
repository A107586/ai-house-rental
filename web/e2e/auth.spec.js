// 登录：演示账号 tenant1 / test123（server/seed.py 生成的演示数据）。
import { expect, test } from '@playwright/test'

async function login(page, username, password) {
  await page.goto('/#/auth/login')
  await page.getByPlaceholder(/3–20 位字母/).fill(username)
  await page.getByPlaceholder(/6–64 位/).fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
}

test('租客登录后回到首页，顶栏换成已登录的样子', async ({ page }) => {
  await login(page, 'tenant1', 'test123')
  await expect(page).not.toHaveURL(/#\/auth/)
  // 「咨询消息」按钮页面上有两个（顶栏和移动端菜单各一个），断言时取第一个
  await expect(page.getByRole('button', { name: '咨询消息' }).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '登录 / 注册' })).toHaveCount(0)
})

test('密码错了留在登录页，并给出提示', async ({ page }) => {
  await login(page, 'tenant1', 'wrong-password')
  await expect(page).toHaveURL(/#\/auth\/login/)
  await expect(page.getByText(/密码|错误|不正确/).first()).toBeVisible()
  await expect(page.getByRole('button', { name: '咨询消息' })).toHaveCount(0)
})

// 这个标签页「现在是谁」：用它自己的令牌问一次后端
async function whoAmI(page) {
  return page.evaluate(async () => {
    const token = sessionStorage.getItem('rental_token')
    const res = await fetch('/api/auth/me', { headers: { Authorization: `Bearer ${token}` } })
    return (await res.json()).username
  })
}

test('令牌按标签页隔离：别的标签页换了账号，这一页的身份不变', async ({ browser }) => {
  // web/src/api.js 的 getToken 说明：令牌原先存在同源共享的 localStorage 里，
  // 两个标签页登两个房东，后登的会把先登的那页也换掉——「所有房东共享房源」就是这么来的。
  // 现在新标签页第一次从上次的登录继承，之后只认自己那一份。
  const context = await browser.newContext()
  const first = await context.newPage()
  await login(first, 'tenant1', 'test123')
  await expect(first.getByRole('button', { name: '咨询消息' }).first()).toBeVisible()

  const second = await context.newPage()
  await second.goto('/')
  await expect(second.getByRole('button', { name: '咨询消息' }).first()).toBeVisible()   // 继承了上次的登录
  await login(second, 'landlord1', 'test123')
  await expect.poll(() => whoAmI(second)).toBe('landlord1')

  expect(await whoAmI(first)).toBe('tenant1')
  await context.close()
})
