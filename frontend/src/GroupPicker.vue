<script setup lang="ts">
import { computed, ref, watch } from 'vue'
const props = defineProps<{ modelValue?: string | null; options: string[] }>()
const emit = defineEmits<{ 'update:modelValue': [value: string]; validity: [valid: boolean] }>()
const creating = ref(false)
const options = computed(() => [...new Set([...props.options, ...(props.modelValue ? [props.modelValue] : [])])].sort((a,b) => a.localeCompare(b, 'zh')))
const selection = computed(() => creating.value ? 'new' : !props.modelValue ? 'none' : 'group:' + props.modelValue)
function select(event: Event) {
  const value = (event.target as HTMLSelectElement).value
  creating.value = value === 'new'
  emit('update:modelValue', value.startsWith('group:') ? value.slice(6) : '')
}
watch(() => [creating.value, props.modelValue], () => emit('validity', !creating.value || !!props.modelValue?.trim()), { immediate: true })
</script>
<template>
  <div class="group-picker">
    <label>分组<select aria-label="选择分组" :value="selection" @change="select">
      <option value="none">未分组</option>
      <option v-for="name in options" :key="name" :value="'group:' + name">{{ name }}</option>
      <option value="new">＋ 新建分组</option>
    </select></label>
    <label v-if="creating">新分组名称<input aria-label="新分组名称" maxlength="80" :value="modelValue || ''" placeholder="输入名称，随本条数据一起保存" @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"></label>
    <small>仅用于工作台分类，不改变用友档案层级。</small>
  </div>
</template>
