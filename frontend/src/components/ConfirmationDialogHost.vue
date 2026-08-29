<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'

import { activeConfirmation, settleConfirmation } from '@/composables/confirmation'

const primary = ref<HTMLButtonElement | null>(null)
const input = ref<HTMLTextAreaElement | null>(null)
const dialog = ref<HTMLElement | null>(null)
const inputValue = ref('')
const valid = computed(() => {
  const field = activeConfirmation.value?.input
  return !field || inputValue.value.trim().length >= field.minLength
})

watch(activeConfirmation, async (request) => {
  document.body.classList.toggle('modal-open', Boolean(request))
  inputValue.value = request?.input?.value ?? ''
  if (request) {
    await nextTick()
    ;(input.value ?? primary.value)?.focus()
  }
})
onBeforeUnmount(() => document.body.classList.remove('modal-open'))

function accept() {
  if (!valid.value) return
  settleConfirmation(activeConfirmation.value?.input ? inputValue.value.trim() : true)
}

function trapFocus(event: KeyboardEvent) {
  const elements = Array.from(dialog.value?.querySelectorAll<HTMLElement>('button:not([disabled]), textarea:not([disabled])') ?? [])
  if (elements.length === 0) return
  const first = elements[0]
  const last = elements[elements.length - 1]
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first?.focus()
  }
}
</script>

<template>
  <Teleport to="body">
    <Transition name="confirm-fade">
      <div
        v-if="activeConfirmation"
        class="confirm-backdrop"
        role="presentation"
        @click.self="settleConfirmation(false)"
        @keydown.esc="settleConfirmation(false)"
      >
        <section
          ref="dialog"
          class="confirm-dialog"
          :class="{ danger: activeConfirmation.tone === 'danger' }"
          role="alertdialog"
          aria-modal="true"
          aria-labelledby="global-confirm-title"
          aria-describedby="global-confirm-message"
          @keydown.tab="trapFocus"
        >
          <span class="confirm-dialog-icon" aria-hidden="true">{{ activeConfirmation.tone === 'danger' ? '!' : '✓' }}</span>
          <div>
            <h2 id="global-confirm-title">{{ activeConfirmation.title }}</h2>
            <p id="global-confirm-message">{{ activeConfirmation.message }}</p>
          </div>
          <label v-if="activeConfirmation.input" class="confirm-dialog-input">
            {{ activeConfirmation.input.label }}
            <textarea
              ref="input"
              v-model="inputValue"
              :placeholder="activeConfirmation.input.placeholder"
              :minlength="activeConfirmation.input.minLength"
              :maxlength="activeConfirmation.input.maxLength"
              rows="4"
              @keydown.meta.enter="accept"
              @keydown.ctrl.enter="accept"
            />
          </label>
          <footer>
            <button class="secondary" type="button" @click="settleConfirmation(false)">{{ activeConfirmation.cancelText }}</button>
            <button ref="primary" type="button" :class="{ danger: activeConfirmation.tone === 'danger' }" :disabled="!valid" @click="accept">
              {{ activeConfirmation.confirmText }}
            </button>
          </footer>
        </section>
      </div>
    </Transition>
  </Teleport>
</template>
