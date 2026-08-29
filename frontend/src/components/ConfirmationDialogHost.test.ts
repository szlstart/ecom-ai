import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import { confirmAction, promptAction, settleConfirmation } from '@/composables/confirmation'
import ConfirmationDialogHost from './ConfirmationDialogHost.vue'

afterEach(() => {
  settleConfirmation(false)
  document.body.classList.remove('modal-open')
})

describe('ConfirmationDialogHost', () => {
  it('returns the explicit confirmation choice and exposes an accessible dialog', async () => {
    const wrapper = mount(ConfirmationDialogHost, { global: { stubs: { Teleport: true } } })
    const result = confirmAction('这项操作不可自动撤销。', { title: '确认测试', tone: 'danger' })
    await flushPromises()

    expect(wrapper.get('[role="alertdialog"]').attributes('aria-modal')).toBe('true')
    expect(wrapper.text()).toContain('这项操作不可自动撤销。')
    expect(document.body.classList.contains('modal-open')).toBe(true)

    await wrapper.get('button.danger').trigger('click')
    await expect(result).resolves.toBe(true)
    expect(document.body.classList.contains('modal-open')).toBe(false)
    wrapper.unmount()
  })

  it('validates and returns prompt text without using the browser prompt API', async () => {
    const wrapper = mount(ConfirmationDialogHost, { global: { stubs: { Teleport: true } } })
    const result = promptAction('原因会进入审计。', { label: '原因', minLength: 3, maxLength: 20 })
    await flushPromises()

    const submit = wrapper.get('footer button:last-child')
    expect(submit.attributes('disabled')).toBeDefined()
    await wrapper.get('textarea').setValue('正常原因')
    expect(submit.attributes('disabled')).toBeUndefined()
    await submit.trigger('click')

    await expect(result).resolves.toBe('正常原因')
    wrapper.unmount()
  })

  it('cancels when the user clicks the backdrop', async () => {
    const wrapper = mount(ConfirmationDialogHost, { global: { stubs: { Teleport: true } } })
    const result = confirmAction('是否继续？')
    await flushPromises()
    await wrapper.get('.confirm-backdrop').trigger('click')
    await expect(result).resolves.toBe(false)
    wrapper.unmount()
  })
})
