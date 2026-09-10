import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import ErrorBoundary from './components/ErrorBoundary'
import './styles.css'
// 视觉升级层，必须排在 styles.css 之后——本层靠加载顺序覆盖，
// 不用 !important（那会让后面的人没法再覆盖回来）
import './theme.css'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary label="页面">
      <App />
    </ErrorBoundary>
  </React.StrictMode>
)
