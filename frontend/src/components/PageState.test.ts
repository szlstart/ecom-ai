import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PageState from './PageState.vue'

describe('PageState', () => {
  it('keeps settled content mounted during a background refresh', async () => {
    const wrapper = mount(PageState, {
      attachTo: document.body,
      props: { loading: true },
      slots: { default: '<article data-testid="content"><input value="尚未保存" /></article>' },
    })

    expect(wrapper.text()).toContain('正在加载，请稍候')
    expect(wrapper.find('[data-testid="content"]').exists()).toBe(false)

    await wrapper.setProps({ loading: false })
    const input = wrapper.get<HTMLInputElement>('input')
    input.element.focus()

    await wrapper.setProps({ loading: true })

    expect(wrapper.text()).toContain('正在更新')
    expect(wrapper.get<HTMLInputElement>('input').element).toBe(input.element)
    expect(document.activeElement).toBe(input.element)
    wrapper.unmount()
  })

  it('can keep settled content without showing the background refresh notice', async () => {
    const wrapper = mount(PageState, {
      props: { loading: false, showRefreshStatus: false },
      slots: { default: '<article data-testid="content">订单列表</article>' },
    })

    await wrapper.setProps({ loading: true })

    expect(wrapper.get('[data-testid="content"]').text()).toBe('订单列表')
    expect(wrapper.text()).not.toContain('正在更新')
  })
})
