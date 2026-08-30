const ORDER_STATUS_LABELS: Record<string, string> = {
  pending_payment: '待付款',
  paid: '已支付',
  pending_shipment: '待发货',
  shipped: '运输中',
  completed: '已完成',
  cancelled: '已取消',
  closed: '已关闭',
}

export function userOrderStatusLabel(orderStatus: string, shipmentStatus = ''): string {
  if (orderStatus === 'shipped' && shipmentStatus === 'delivered') return '已签收，待确认收货'
  return ORDER_STATUS_LABELS[orderStatus] ?? orderStatus
}
