export interface StoreStatusSignal {
  storeId: string
  status: string
  suspensionSource: 'merchant' | 'platform' | null
  changedAt: number
}

const eventName = 'ecom:store-status-changed'
const storageKey = 'ecom-store-status-sync'

export function publishStoreStatus(signal: Omit<StoreStatusSignal, 'changedAt'>): void {
  const detail: StoreStatusSignal = { ...signal, changedAt: Date.now() }
  window.dispatchEvent(new CustomEvent<StoreStatusSignal>(eventName, { detail }))
  try {
    localStorage.setItem(storageKey, JSON.stringify(detail))
  } catch {
    // 状态同步失败时由布局的短轮询兜底，不能影响营业状态命令本身。
  }
}

export function subscribeStoreStatus(listener: (signal: StoreStatusSignal) => void): () => void {
  const customHandler = (event: Event) => {
    listener((event as CustomEvent<StoreStatusSignal>).detail)
  }
  const storageHandler = (event: StorageEvent) => {
    if (event.key !== storageKey || !event.newValue) return
    try {
      listener(JSON.parse(event.newValue) as StoreStatusSignal)
    } catch {
      // 忽略损坏的非权威浏览器通知，随后从服务端重新读取。
    }
  }
  window.addEventListener(eventName, customHandler)
  window.addEventListener('storage', storageHandler)
  return () => {
    window.removeEventListener(eventName, customHandler)
    window.removeEventListener('storage', storageHandler)
  }
}
