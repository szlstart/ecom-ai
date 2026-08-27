import { describe, expect, it } from 'vitest'

import { imageFileFromClipboard } from './clipboard-image'

function clipboardWith(file: File): DataTransfer {
  return {
    items: [{ kind: 'file', type: file.type, getAsFile: () => file }],
  } as unknown as DataTransfer
}

describe('imageFileFromClipboard', () => {
  it('gives a pasted PNG a safe filename accepted by the upload policy', () => {
    const result = imageFileFromClipboard(
      clipboardWith(new File(['image'], '', { type: 'image/png' })),
      1234,
    )
    expect(result?.name).toBe('clipboard-1234.png')
    expect(result?.type).toBe('image/png')
  })

  it('returns null when the clipboard has no image', () => {
    const data = { items: [{ kind: 'string', type: 'text/plain', getAsFile: () => null }] }
    expect(imageFileFromClipboard(data as unknown as DataTransfer)).toBeNull()
  })

  it('rejects image formats outside the product upload policy', () => {
    expect(() => imageFileFromClipboard(
      clipboardWith(new File(['image'], 'image.gif', { type: 'image/gif' })),
    )).toThrow('JPG、PNG 或 WebP')
  })
})
