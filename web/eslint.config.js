// 前端静态检查：**只查会出错的，不管风格。**
//
//   cd web && npm run lint
//
// 为什么加：2026-10-21 查出「每次退出登录都抛 ReferenceError」——9-11 删掉 showWork
// 这个状态时，logout() 里那行 setShowWork(false) 留了下来。用户照样被登出，
// 所以一个多月没人发现；`no-undef` 一条规则当场就能指出来。
//
// 所以这里刻意只开「错一定是错」的规则：未定义的名字（含 JSX 里的组件名）、
// Hooks 写在条件分支里、列表漏 key 之类。缩进、引号、分号一概不管——
// 风格规则一开就是几百条，真正的错淹在里面，没人会看。
//
// 级别：error 让 CI 变红；warn 只是提示（没用到的变量、依赖数组漏项），不拦合并。
import js from '@eslint/js'
import react from 'eslint-plugin-react'
import reactHooks from 'eslint-plugin-react-hooks'
import globals from 'globals'

export default [
  { ignores: ['dist/', 'node_modules/', 'playwright-report/', 'test-results/'] },
  {
    files: ['src/**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: globals.browser,
      parserOptions: { ecmaFeatures: { jsx: true } },
    },
    plugins: { react, 'react-hooks': reactHooks },
    settings: { react: { version: '18' } },
    rules: {
      ...js.configs.recommended.rules,
      // JSX 里用到的组件名也算「用过」，否则每个 import 进来的组件都报未使用
      'react/jsx-uses-vars': 'error',
      // <Foo /> 而 Foo 没定义：编译能过，渲染到这里才炸
      'react/jsx-no-undef': 'error',
      'react/jsx-key': 'warn',
      'react/no-direct-mutation-state': 'error',
      'react/no-danger-with-children': 'error',
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',
      'no-unused-vars': ['warn', { args: 'none', caughtErrors: 'none', ignoreRestSiblings: true }],
      'no-empty': ['warn', { allowEmptyCatch: true }],
      // 文案里的全角空格（U+3000，如资质前面那个）是排版，不是误输入
      'no-irregular-whitespace': ['error', { skipStrings: true, skipTemplates: true, skipJSXText: true }],
    },
  },
  {
    // Playwright 用例跑在 Node 里，但 page.evaluate(() => …) 里那段跑在浏览器里，两边的全局都要认
    files: ['e2e/**/*.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: { ...globals.node, ...globals.browser } },
    rules: js.configs.recommended.rules,
  },
  {
    files: ['*.config.js'],
    languageOptions: { ecmaVersion: 2022, sourceType: 'module', globals: globals.node },
    rules: js.configs.recommended.rules,
  },
]
