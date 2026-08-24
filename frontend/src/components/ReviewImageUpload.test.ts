import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import ReviewImageUpload from '@/components/ReviewImageUpload.vue'
import { useUserAuthStore } from '@/stores/user-auth'

const uploadBindableFile = vi.fn()
const loadProtectedFileObjectUrl = vi.fn()
let pinia: ReturnType<typeof createPinia>

vi.mock('@/api/files', () => ({
  getUploadPolicy: vi.fn(async () => ({
    data: {
      purpose: 'review_image',
      allowed_mime_types: ['image/jpeg', 'image/png', 'image/webp'],
      allowed_extensions: ['jpg', 'jpeg', 'png', 'webp'],
      max_size_bytes: 10 * 1024 * 1024,
      max_count: 6,
    },
  })),
  uploadBindableFile: (...args: unknown[]) => uploadBindableFile(...args),
  loadProtectedFileObjectUrl: (...args: unknown[]) => loadProtectedFileObjectUrl(...args),
}))

describe('ReviewImageUpload', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, value: vi.fn(() => 'blob:preview') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, value: vi.fn() })
    pinia = createPinia()
    setActivePinia(pinia)
    useUserAuthStore(pinia).$patch({ accessToken: 'user-token' })
  })

  it('uploads every selected image and emits the complete binding list', async () => {
    uploadBindableFile.mockResolvedValueOnce('fil_safe_1').mockResolvedValueOnce('fil_safe_2')
    const wrapper = mount(ReviewImageUpload, {
      props: { modelValue: [] },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    const first = new File(['first'], 'first.jpg', { type: 'image/jpeg' })
    const second = new File(['second'], 'second.png', { type: 'image/png' })
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [first, second] })
    await input.trigger('change')
    await wrapper.get('button.secondary').trigger('click')
    await flushPromises()

    expect(uploadBindableFile).toHaveBeenCalledTimes(2)
    expect(uploadBindableFile).toHaveBeenNthCalledWith(
      1,
      first,
      'review_image',
      'user-token',
      expect.objectContaining({ max_count: 6 }),
      expect.any(Function),
    )
    expect(wrapper.emitted('update:modelValue')).toEqual([
      [['fil_safe_1']],
      [['fil_safe_1', 'fil_safe_2']],
    ])
    expect(wrapper.text()).toContain('2 张图片已通过安全处理')
  })

  it('retains existing bindings and emits removal without requiring a thumbnail', async () => {
    loadProtectedFileObjectUrl.mockRejectedValue(new Error('preview unavailable'))
    const wrapper = mount(ReviewImageUpload, {
      props: { modelValue: ['fil_existing'] },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('图片 1')
    await wrapper.get('button.danger').trigger('click')
    expect(wrapper.emitted('update:modelValue')).toEqual([[[]]])
  })

  it('rejects a selection that would exceed the server policy limit', async () => {
    const wrapper = mount(ReviewImageUpload, {
      props: { modelValue: ['1', '2', '3', '4', '5'] },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    const first = new File(['first'], 'first.jpg', { type: 'image/jpeg' })
    const second = new File(['second'], 'second.jpg', { type: 'image/jpeg' })
    const input = wrapper.get('input[type="file"]')
    Object.defineProperty(input.element, 'files', { configurable: true, value: [first, second] })
    await input.trigger('change')

    expect(wrapper.get('[role="alert"]').text()).toContain('当前还可选择 1 张')
    expect(uploadBindableFile).not.toHaveBeenCalled()
  })
})
