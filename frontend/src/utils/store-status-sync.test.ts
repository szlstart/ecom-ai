import { describe, expect, it, vi } from 'vitest'

import { publishStoreStatus, subscribeStoreStatus } from './store-status-sync'

describe('store status synchronization', () => {
  it('notifies the current tab immediately and persists a cross-tab signal', () => {
    const listener = vi.fn()
    const unsubscribe = subscribeStoreStatus(listener)

    publishStoreStatus({
      storeId: 'sto_test',
      status: 'suspended',
      suspensionSource: 'platform',
    })

    expect(listener).toHaveBeenCalledOnce()
    expect(listener.mock.calls[0]?.[0]).toMatchObject({
      storeId: 'sto_test',
      status: 'suspended',
      suspensionSource: 'platform',
    })
    expect(JSON.parse(localStorage.getItem('ecom-store-status-sync') ?? '{}')).toMatchObject({
      storeId: 'sto_test',
      status: 'suspended',
      suspensionSource: 'platform',
    })

    unsubscribe()
  })

  it('ignores malformed cross-tab payloads', () => {
    const listener = vi.fn()
    const unsubscribe = subscribeStoreStatus(listener)

    window.dispatchEvent(new StorageEvent('storage', {
      key: 'ecom-store-status-sync',
      newValue: '{broken',
    }))

    expect(listener).not.toHaveBeenCalled()
    unsubscribe()
  })
})
