const EXTENSIONS_BY_MIME: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/png': 'png',
  'image/webp': 'webp',
}

export function imageFileFromClipboard(
  clipboardData: DataTransfer | null,
  timestamp = Date.now(),
): File | null {
  if (!clipboardData) return null
  const item = Array.from(clipboardData.items).find((candidate) => (
    candidate.kind === 'file' && candidate.type.startsWith('image/')
  ))
  const source = item?.getAsFile() ?? (clipboardData.files
    ? Array.from(clipboardData.files).find((candidate) => candidate.type.startsWith('image/'))
    : undefined)
  if (!source) return null
  const mimeType = (source.type || item?.type || '').toLowerCase()
  const extension = EXTENSIONS_BY_MIME[mimeType]
  if (!extension) {
    throw new Error('剪贴板图片格式不受支持，请复制 JPG、PNG 或 WebP 图片。')
  }
  return new File([source], `clipboard-${timestamp}.${extension}`, {
    type: mimeType,
    lastModified: timestamp,
  })
}
