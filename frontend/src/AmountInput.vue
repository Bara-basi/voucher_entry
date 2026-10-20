<script setup lang="ts">
import { computed, nextTick, ref } from 'vue'
import { calculate } from './decimal'
const props = defineProps<{ modelValue: string; label: string; target: string; candidates: { label: string; value: string }[]; places?: number; blankZero?: boolean }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; blur: []; error: [message: string] }>()
const focused = ref(false), buffer = ref(''), input = ref<HTMLInputElement>(), active = ref(0)
const position = ref<Record<string, string>>({})
const formula = computed(() => focused.value && buffer.value.startsWith('='))
const display = computed(() => {
  if (focused.value) return buffer.value
  if (props.modelValue === '' || (props.blankZero !== false && Number(props.modelValue) === 0)) return ''
  if (!Number.isFinite(Number(props.modelValue))) return props.modelValue
  return Number(props.modelValue).toLocaleString('en-US', { minimumFractionDigits: props.places ?? 2, maximumFractionDigits: props.places ?? 2, useGrouping: props.places !== 6 })
})
function edit() {
  active.value = 0
  const rect = input.value!.getBoundingClientRect()
  position.value = { left: Math.max(8, Math.min(rect.left, window.innerWidth - 330)) + 'px',
    ...(window.innerHeight - rect.bottom < 220 ? { bottom: window.innerHeight - rect.top + 4 + 'px' } : { top: rect.bottom + 4 + 'px' }) }
  if (!buffer.value.startsWith('=') && (buffer.value === '' || /^[+-]?\d*\.?\d+$/.test(buffer.value)))
    emit('update:modelValue', buffer.value)
}
function commit(blur = false) {
  try {
    const value = buffer.value === '' ? '' : calculate(buffer.value, props.places ?? 2)
    if (value !== props.modelValue && (value === '' || props.modelValue === '' || Number(value) !== Number(props.modelValue)))
      emit('update:modelValue', value)
    buffer.value = value
  } catch {
    emit('error', blur ? '公式未完成，已保留该单元格原值' : '请补全加减公式后按 Enter')
  }
}
function insert(value: string) {
  const start = input.value?.selectionStart ?? buffer.value.length
  const end = input.value?.selectionEnd ?? start
  const prefix = buffer.value.slice(0, start), suffix = buffer.value.slice(end)
  const negative = Number(value) < 0
  let term = value
  if (negative && prefix.endsWith('-')) { buffer.value = prefix.slice(0, -1) + '+' + value.slice(1) + suffix }
  else if (negative && prefix.endsWith('+')) { buffer.value = prefix.slice(0, -1) + value + suffix }
  else { if (/\d$/.test(prefix) && !negative) term = '+' + term; buffer.value = prefix + term + suffix }
  input.value?.focus()
  nextTick(() => input.value?.setSelectionRange(buffer.value.length, buffer.value.length))
}
function key(e: KeyboardEvent) {
  if (e.key === 'Escape') { e.stopPropagation(); buffer.value = props.modelValue; return }
  if (e.key === 'Enter') {
    e.preventDefault()
    if (formula.value && /[=+-]$/.test(buffer.value) && props.candidates[active.value]) insert(props.candidates[active.value]!.value)
    else commit()
  }
  if (formula.value && ['ArrowUp','ArrowDown'].includes(e.key)) {
    e.preventDefault(); active.value = Math.max(0, Math.min(props.candidates.length - 1, active.value + (e.key === 'ArrowDown' ? 1 : -1)))
  }
}
</script>
<template>
  <input ref="input" :aria-label="label" :data-target="target" :value="display" inputmode="text"
    @focus="focused = true; buffer = modelValue" @input="buffer = ($event.target as HTMLInputElement).value; edit()"
    @keydown="key" @blur="commit(true); focused = false; emit('blur')">
  <Teleport to="body">
    <div v-if="formula && candidates.length" class="amount-options catalog-options" :style="position" @pointerdown.prevent>
      <button v-for="(item, i) in candidates" :key="item.label" :class="{ closest: i === active }" tabindex="-1" @click="insert(item.value)">
        <span>{{ item.label }}</span><b>{{ item.value }}</b>
      </button>
    </div>
  </Teleport>
</template>
