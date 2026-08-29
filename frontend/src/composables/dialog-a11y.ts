import { nextTick, onBeforeUnmount, type Ref, watch } from 'vue'

const FOCUSABLE = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

let scrollLockCount = 0
let originalOverflow = ''

function lockPageScroll() {
  if (scrollLockCount === 0) {
    originalOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
  }
  scrollLockCount += 1
}

function unlockPageScroll() {
  scrollLockCount = Math.max(0, scrollLockCount - 1)
  if (scrollLockCount === 0) document.body.style.overflow = originalOverflow
}

/** Shared modal behavior: initial focus, focus trap, Escape, scroll lock and focus restore. */
export function useDialogA11y(
  open: Ref<boolean>,
  dialog: Ref<HTMLElement | null>,
  close: () => void,
) {
  let previousFocus: HTMLElement | null = null
  let active = false

  function focusable(): HTMLElement[] {
    return dialog.value
      ? [...dialog.value.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((item) => !item.hidden)
      : []
  }

  function onKeydown(event: KeyboardEvent) {
    if (!active) return
    if (event.key === 'Escape') {
      event.preventDefault()
      close()
      return
    }
    if (event.key !== 'Tab') return
    const items = focusable()
    if (!items.length) {
      event.preventDefault()
      dialog.value?.focus()
      return
    }
    const first = items[0]!
    const last = items.at(-1)!
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault(); last.focus()
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault(); first.focus()
    }
  }

  function onFocus(event: FocusEvent) {
    if (active && dialog.value && !dialog.value.contains(event.target as Node)) {
      ;(focusable()[0] ?? dialog.value).focus()
    }
  }

  function deactivate(restore = true) {
    if (!active) return
    active = false
    document.removeEventListener('keydown', onKeydown, true)
    document.removeEventListener('focusin', onFocus, true)
    unlockPageScroll()
    if (restore) void nextTick(() => previousFocus?.focus())
  }

  watch(open, async (value) => {
    if (!value) { deactivate(); return }
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
    active = true
    lockPageScroll()
    document.addEventListener('keydown', onKeydown, true)
    document.addEventListener('focusin', onFocus, true)
    await nextTick()
    ;(focusable()[0] ?? dialog.value)?.focus()
  }, { immediate: true })

  onBeforeUnmount(() => deactivate())
}
