import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import SafeContentRenderer from './SafeContentRenderer.vue'

describe('SafeContentRenderer', () => {
  it('renders structured text and images in the exact block order without raw HTML', () => {
    const wrapper = mount(SafeContentRenderer, {
      props: {
        content: {
          content_format: 'structured_v1',
          content_version: 1,
          content_hash: 'hash',
          safe_blocks: [
            { type: 'paragraph', text: '第一段文字' },
            { type: 'image', file_id: 'file_01ARZ3NDEKTSV4RRFFQ69G5FAV', alt: '中间图片' },
            { type: 'paragraph', text: '第二段文字' },
          ],
          safe_html: null,
          safe_text_fallback: '第一段文字 中间图片 第二段文字',
        },
      },
    })

    const children = Array.from(wrapper.get('.safe-content-blocks').element.children)
    expect(children.map((element) => element.tagName)).toEqual(['P', 'FIGURE', 'P'])
    expect(children.map((element) => element.textContent)).toEqual(['第一段文字', '', '第二段文字'])
    expect(wrapper.get('img').attributes('src')).toContain('/api/v1/files/file_01ARZ3NDEKTSV4RRFFQ69G5FAV')
    expect(wrapper.html()).not.toContain('v-html')
  })
})
