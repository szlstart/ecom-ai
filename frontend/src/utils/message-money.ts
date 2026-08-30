export function messageMoneyLabel(value: unknown): string {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return '—'
  const money = value as Record<string, unknown>
  const amount = Number(money.minor_units ?? money.amount)
  const currency = String(money.currency ?? 'CNY')
  if (!Number.isSafeInteger(amount)) return '—'
  try {
    return new Intl.NumberFormat('zh-CN', { style: 'currency', currency }).format(amount / 100)
  } catch {
    return '—'
  }
}
