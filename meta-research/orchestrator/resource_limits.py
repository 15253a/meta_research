"""用户文件请求跨接纳/编译边界共享的不可变安全上限。

这些值同时约束 FileRequestService 写入不可变终态和 SqliteCompiler 渲染 goal-wide 回执；
禁止在两侧各写一份数字，否则会重新引入“合法接纳后永久不可渲染”的楔死状态。
"""

MAX_FILE_REQUESTS_PER_GOAL = 5
MAX_REQUEST_ITEMS = 10
MAX_ASSETS_PER_GOAL = 512
MAX_CANCEL_REASON_CHARS = 2000
