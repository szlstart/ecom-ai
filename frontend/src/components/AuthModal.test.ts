import { flushPromises, mount } from '@vue/test-utils'
import { createPinia } from 'pinia'
import { describe, expect, it } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import AuthModal from './AuthModal.vue'

describe('AuthModal', () => {
  it('navigates to password recovery without the close handler overriding the route', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [
        { path: '/', component: { template: '<div />' } },
        { path: '/forgot-password', component: { template: '<div />' } },
      ],
    })
    await router.push('/')
    await router.isReady()

    const wrapper = mount(AuthModal, {
      props: { initialMode: 'login' },
      global: {
        plugins: [createPinia(), router],
        stubs: { Teleport: true },
      },
    })

    await wrapper.get('a[href="/forgot-password"]').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.path).toBe('/forgot-password')
    expect(wrapper.emitted('close')).toBeUndefined()
  })
})
