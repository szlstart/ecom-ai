import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { afterEach, describe, expect, it } from 'vitest'

import { useDialogA11y } from './dialog-a11y'

const Harness = defineComponent({
  setup() {
    const open = ref(false)
    const dialog = ref<HTMLElement | null>(null)
    const close = () => { open.value = false }
    useDialogA11y(open, dialog, close)
    return { open, dialog, close }
  },
  template: '<button class="trigger" @click="open = true">打开</button><section v-if="open" ref="dialog" tabindex="-1"><button class="first">第一</button><button class="last">最后</button></section>',
})

afterEach(() => { document.body.style.overflow = '' })

describe('useDialogA11y', () => {
  it('locks scroll, traps focus and restores the trigger on Escape', async () => {
    const wrapper = mount(Harness, { attachTo: document.body })
    const trigger = wrapper.get<HTMLButtonElement>('.trigger')
    trigger.element.focus()
    await trigger.trigger('click')
    expect(document.body.style.overflow).toBe('hidden')
    expect(document.activeElement).toBe(wrapper.get('.first').element)

    wrapper.get<HTMLButtonElement>('.last').element.focus()
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Tab', bubbles: true }))
    expect(document.activeElement).toBe(wrapper.get('.first').element)
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await wrapper.vm.$nextTick()
    await wrapper.vm.$nextTick()
    expect(wrapper.find('section').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    expect(document.body.style.overflow).toBe('')
    wrapper.unmount()
  })
})
