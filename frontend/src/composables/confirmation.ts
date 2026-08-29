import { shallowRef } from 'vue'

export interface ConfirmOptions {
  title?: string
  confirmText?: string
  cancelText?: string
  tone?: 'default' | 'danger'
}

export interface PromptOptions extends ConfirmOptions {
  label?: string
  initialValue?: string
  placeholder?: string
  minLength?: number
  maxLength?: number
}

export interface ConfirmationRequest extends Required<ConfirmOptions> {
  message: string
  input: null | {
    label: string
    value: string
    placeholder: string
    minLength: number
    maxLength: number
  }
  resolve: (value: boolean | string | null) => void
}

export const activeConfirmation = shallowRef<ConfirmationRequest | null>(null)

function defaults(options: ConfirmOptions): Required<ConfirmOptions> {
  return {
    title: options.title ?? '请确认操作',
    confirmText: options.confirmText ?? '确认',
    cancelText: options.cancelText ?? '取消',
    tone: options.tone ?? 'default',
  }
}

export function confirmAction(message: string, options: ConfirmOptions = {}): Promise<boolean> {
  activeConfirmation.value?.resolve(false)
  return new Promise((resolve) => {
    activeConfirmation.value = {
      ...defaults(options),
      message,
      input: null,
      resolve: (value) => resolve(value === true),
    }
  })
}

export function promptAction(message: string, options: PromptOptions = {}): Promise<string | null> {
  activeConfirmation.value?.resolve(null)
  return new Promise((resolve) => {
    activeConfirmation.value = {
      ...defaults(options),
      message,
      input: {
        label: options.label ?? '补充说明',
        value: options.initialValue ?? '',
        placeholder: options.placeholder ?? '',
        minLength: options.minLength ?? 0,
        maxLength: options.maxLength ?? 1000,
      },
      resolve: (value) => resolve(typeof value === 'string' ? value : null),
    }
  })
}

export function settleConfirmation(value: boolean | string | null): void {
  const request = activeConfirmation.value
  if (!request) return
  activeConfirmation.value = null
  request.resolve(value)
}
