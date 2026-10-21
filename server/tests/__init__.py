import os

# 测试里不打访问日志（app/tracing.py），不然一千条测试的请求刷满屏幕
os.environ.setdefault("ACCESS_LOG", "0")
