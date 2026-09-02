import { Component } from 'react'

/**
 * 错误边界。
 *
 * React 的默认行为是：任何一个组件在渲染中抛异常，整棵树都会被卸载，
 * 页面变成一片空白，控制台之外**什么线索都没有**。演示时最怕的就是这个——
 * 白屏既看不出哪儿坏了，也没法当场绕过去。
 *
 * 包一层之后，出错的那一块降级成一张提示卡，其余部分照常可用，
 * 而且把错误信息直接摆出来，不用去开控制台。
 *
 * 只能兜住**渲染期**的异常。事件回调与异步请求里的错误 React 捕获不到，
 * 那些地方各组件自己 try/catch 后用 setError 显示。
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { error: null }
  }

  static getDerivedStateFromError(error) {
    return { error }
  }

  componentDidCatch(error, info) {
    // 留一份在控制台，方便查栈；界面上只给可读的摘要
    console.error('[界面异常]', error, info?.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children

    return (
      <div className="crash">
        <div className="crash-title">
          {this.props.label || '这块内容'}出错了
        </div>
        <p>
          页面其余部分仍可正常使用。刷新一次通常就能恢复；
          如果反复出现，请把下面这行信息发给开发同学。
        </p>
        <pre className="crash-detail">
          {String(this.state.error?.message || this.state.error)}
        </pre>
        <button
          className="btn ghost small"
          onClick={() => this.setState({ error: null })}
        >
          重试
        </button>
      </div>
    )
  }
}
