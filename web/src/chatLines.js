/**
 * 租客—房东会话的消息合并。ChatWithLandlord（租客侧）与 Workbench（房东侧）共用。
 *
 * 两边都是「发出去的那条立刻上屏 + 定时按 after_id 拉增量」。这两路会撞：
 * 轮询请求按「最后一条是 10」发出去还没回来，用户这时发了第 11 条并上屏；
 * 轮询回来的结果里也有第 11 条——直接拼接，同一句话就在屏幕上出现两次。
 * 所以并入时按 id 去掉已有的那条。
 */
export function appendLines(prev, rows) {
  if (!rows?.length) return prev
  const seen = new Set(prev.map((line) => line.id))
  const fresh = rows.filter((line) => !seen.has(line.id))
  return fresh.length ? [...prev, ...fresh] : prev
}

/** 增量游标只往前走：晚回来的旧结果不能把它拨回去，否则下一轮会重拉一遍 */
export function lastLineId(current, rows) {
  if (!rows?.length) return current
  return Math.max(current, rows[rows.length - 1].id)
}
