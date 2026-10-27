<script setup lang="ts">
import { computed, ref, watch } from 'vue'
const props = defineProps<{ modelValue: string; items: any[]; label: string; target: string; displayName?: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; blur: [] }>()
const input = ref<HTMLTextAreaElement>(), typing = ref(false), focused = ref(false), active = ref(0)
const position = ref<Record<string, string>>({})
const normalize = (s: string) => s.toLowerCase().replace(/[\s_-]/g, '')
const selected = computed(() => props.items.find(item => String(item.code) === props.modelValue))
const display = computed(() => focused.value ? props.modelValue : selected.value ? `${selected.value.code} ${props.displayName || selected.value.displayname || selected.value.name}` : props.modelValue)
const matches = computed(() => {
  const q = normalize(props.modelValue)
  if (!q) return []
  return props.items.map(item => {
    const code = normalize(String(item.code)), name = normalize(item.displayname || item.name || '')
    const score = code === q || name === q ? 0 : code.startsWith(q) ? 1 : name.startsWith(q) ? 2 : code.includes(q) || name.includes(q) ? 3 : 99
    return { item, score }
  }).filter(r => r.score < 99).sort((a, b) => a.score - b.score || String(a.item.code).localeCompare(String(b.item.code))).slice(0, 8).map(r => r.item)
})
function edit(event: Event) {
  emit('update:modelValue', (event.target as HTMLTextAreaElement).value)
  typing.value = true; active.value = 0
  const rect = input.value!.getBoundingClientRect()
  const below = window.innerHeight - rect.bottom
  position.value = { position: 'fixed', left: Math.max(8, Math.min(rect.left, window.innerWidth - 340)) + 'px', width: '330px',
    ...(below < 240 ? { bottom: window.innerHeight - rect.top + 4 + 'px' } : { top: rect.bottom + 4 + 'px' }),
    maxHeight: Math.max(120, Math.min(300, below < 240 ? rect.top - 12 : below - 12)) + 'px' }
}
function choose(item: any) { emit('update:modelValue', String(item.code)); typing.value = false }
function key(event: KeyboardEvent) {
  if (!typing.value) return
  if (event.key === 'Escape') { event.stopPropagation(); typing.value = false }
  if (event.key === 'Enter' && matches.value.length) { event.preventDefault(); choose(matches.value[active.value]) }
  if (['ArrowDown', 'ArrowUp'].includes(event.key) && matches.value.length) {
    event.preventDefault(); active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + matches.value.length) % matches.value.length
  }
}
watch(() => props.target, () => { typing.value = false; focused.value = false })
</script>
<template>
  <textarea rows="2" ref="input" class="catalog-input" :aria-label="label" :data-target="target" :value="display"
    :title="displayName && selected ? `用友档案：${selected.code} ${selected.name}` : undefined"
    role="combobox" aria-autocomplete="list" :aria-expanded="typing && matches.length > 0"
    :aria-controls="'options-' + target" :aria-activedescendant="typing && matches.length ? 'option-' + target + active : undefined"
    autocomplete="off" @focus="focused = true" @input="edit" @keydown="key"
    @blur="focused = false; typing = false; emit('blur')"></textarea>
  <Teleport to="body">
    <div v-if="typing && matches.length" :id="'options-' + target" class="catalog-options" role="listbox" :style="position" @pointerdown.prevent>
      <button v-for="(item, i) in matches" :id="'option-' + target + i" :key="item.code" role="option" :aria-selected="i === active"
        :class="{ closest: i === active }" tabindex="-1" @click="choose(item)">
        <span><b>{{ item.code }}</b> {{ item.displayname || item.name }}</span><small v-if="i === active">Enter 选取</small>
      </button>
    </div>
  </Teleport>
</template>
