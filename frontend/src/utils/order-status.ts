const ORDER_STATUS_LABELS: Record<string, string> = {
  pending_payment: '待付款',
  paid: '已支付',
  pending_shipment: '待发货',
  shipped: '运输中',
  completed: '已完成',
  cancelled: '已取消',
  closed: '已关闭',
}

const STATE_STATUS_LABELS: Record<string, string> = {
  ...ORDER_STATUS_LABELS,
  unpaid: '等待付款',
  processing: '处理中',
  paid: '付款成功',
  failed: '处理失败',
  unknown: '状态确认中',
  unfulfilled: '等待备货',
  received: '已确认收货',
  none: '暂无售后',
  submitted: '申请已提交',
  approved: '申请已通过',
  rejected: '申请未通过',
  succeeded: '处理完成',
}

export interface ConsumerOrderEvent {
  event_id: number
  title: string
  description: string
  occurred_at: string
}

interface RawOrderEvent {
  event_id: number
  state_dimension: string
  to_status: string
  event_code: string
  reason: string | null
  occurred_at: string
}

const EVENT_COPY: Record<string, { title: string; description: string }> = {
  'order.created': { title: '订单已提交', description: '订单创建成功，正在等待付款。' },
  'payment.succeeded': { title: '付款成功', description: '付款已确认，商家将开始备货。' },
  'order.fulfillment_initialized': { title: '商家开始备货', description: '商品正在打包，物流信息生成后可随时查看。' },
  'shipment.automatic_dispatched': { title: '包裹已发出', description: '商品已交给承运商，运输进度将持续更新。' },
  'order.receipt_confirmed': { title: '已确认收货', description: '订单已完成，可以发表评价或申请售后。' },
  'order.receipt_auto_confirmed': { title: '系统确认收货', description: '物流签收已满 7 天，订单已自动完成。' },
  'order.user_cancelled': { title: '订单已取消', description: '订单已按你的申请取消。' },
  'order.admin_cancelled': { title: '订单已关闭', description: '订单由平台关闭，如有疑问可联系专属客服。' },
  'order.payment_timed_out': { title: '订单已关闭', description: '订单超过付款时间，系统已自动关闭。' },
  'order.amount_adjusted': { title: '订单金额已更新', description: '订单金额发生调整，请以金额明细为准。' },
}

const HIDDEN_TECHNICAL_EVENTS = new Set([
  'payment.attempt_started',
  'order.payment_succeeded',
  'order.automatic_shipped',
])

/** Convert immutable internal events into a short, consumer-facing journey. */
export function consumerOrderEvents(events: RawOrderEvent[]): ConsumerOrderEvent[] {
  const result: ConsumerOrderEvent[] = []
  const seen = new Set<string>()
  for (const event of events) {
    if (HIDDEN_TECHNICAL_EVENTS.has(event.event_code)) continue
    if (event.event_code === 'order.created' && event.state_dimension !== 'order') continue
    if ((event.event_code === 'order.receipt_confirmed' || event.event_code === 'order.receipt_auto_confirmed') && event.state_dimension !== 'order') continue

    const known = EVENT_COPY[event.event_code]
    const title = known?.title ?? '订单状态已更新'
    const safeReason = event.reason?.trim() && /[\u3400-\u9fff]/.test(event.reason) ? event.reason.trim() : ''
    const description = safeReason || known?.description || `${event.state_dimension === 'payment' ? '付款' : event.state_dimension === 'fulfillment' ? '配送' : '订单'}状态更新为“${STATE_STATUS_LABELS[event.to_status] ?? '处理中'}”。`
    const fingerprint = `${title}|${description}`
    if (seen.has(fingerprint)) continue
    seen.add(fingerprint)
    result.push({ event_id: event.event_id, title, description, occurred_at: event.occurred_at })
  }
  return result
}

export function userOrderStatusLabel(orderStatus: string, shipmentStatus = ''): string {
  if (orderStatus === 'shipped' && shipmentStatus === 'delivered') return '已签收，待确认收货'
  return ORDER_STATUS_LABELS[orderStatus] ?? orderStatus
}
