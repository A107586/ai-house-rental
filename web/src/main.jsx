import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'
import './styles.css'
// 视觉升级层，必须排在 styles.css 之后——本层靠加载顺序覆盖，
// 不用 !important（那会让后面的人没法再覆盖回来）
import './theme.css'
import { purgeLegacy } from './storage'

// 把修复之前留下的、不带身份的那几个旧键删掉。
//
// **这是在清数据，不是在清代码。** 那几个键里现在就躺着某个人的
// 整段对话，而这台电脑后来可能已经换人用了。只改新代码不删旧数据，
// 泄出去的那份会一直留到有人手动清缓存为止。
purgeLegacy()

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary label="页面">
      <App />
    </ErrorBoundary>
  </React.StrictMode>
)
