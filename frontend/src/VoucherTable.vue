<script setup lang="ts">
import { ref } from 'vue'
import AmountInput from './AmountInput.vue'
import { multiply } from './decimal'
import CatalogInput from './CatalogInput.vue'
import { ArrowUp, ArrowDown, Trash2, Plus, CircleAlert } from 'lucide-vue-next'
import type { Draft, Row } from './api'
import { amount, newId } from './api'
const props = defineProps<{ draft: Draft; catalogs: Record<string, any[]>; readonly?: boolean }>()
const emit = defineEmits<{ change: []; blur: []; error: [message: string] }>()
const bad = (row: Row, field: string) => props.draft.issues.some(i => i.row_id === row.id && i.field === field)
const reason = (row: Row, field: string) => props.draft.issues.filter(i => i.row_id === row.id && i.field === field).map(i => i.message).join('；') || undefined
const edited = (row: Row, field: string) => props.draft.manual_fields.includes(row.id + '/' + field)
function focusIssue(event: MouseEvent) {
  const container = (event.currentTarget as HTMLElement).parentElement
  container?.querySelector<HTMLElement>('input,textarea,select')?.focus()
}
function update(row: Row, field: string, event: Event) {
  setValue(row, field, (event.target as HTMLInputElement).value)
}
function setValue(row: Row, field: string, value: string) {
  row[field] = value
  if (['汇率', '原币借方', '原币贷方'].includes(field)) {
    for (const [original, local] of [['原币借方', '借方金额'], ['原币贷方', '贷方金额']]) {
      if (field !== '汇率' && field !== original) continue
      try { row[local!] = row[original!] === '' || row['汇率'] === '' ? '' : multiply(row[original!]!, row['汇率']) } catch { /* Keep the last valid local amount while typing. */ }
    }
  }
  if (row['币种名称'] === 'CNY') {
    if (field === '借方金额') row['原币借方'] = value
    if (field === '贷方金额') row['原币贷方'] = value
  }
  emit('change')
}
function candidates(row: Row, field: string) {
  const fields = field.startsWith('原币') ? ['原币借方', '原币贷方'] : ['借方金额', '贷方金额']
  return props.draft.rows.flatMap((r, i) => fields.filter(f => !(r.id === row.id && f === field) && r[f] !== '' && Number(r[f]) !== 0).map(f => ({ label: `${i + 1} · ${f}`, value: r[f] })))
}
function move(index: number, direction: number) {
  const target = index + direction
  if (target < 0 || target >= props.draft.rows.length) return
  const row = props.draft.rows.splice(index, 1)[0]!
  props.draft.rows.splice(target, 0, row)
  emit('change')
}
function add() {
  props.draft.rows.push({ id: newId(), 摘要: '', 科目编码: '', 币种名称: 'CNY', 汇率: '1',
    原币借方: '0', 原币贷方: '0', 借方金额: '0', 贷方金额: '0', 客户编码: '', 供应商编码: '', 存货档案: '', 单价: '', 借方数量: '', 贷方数量: '' })
  emit('change')
}
const aux = [{ label: '客户', field: '客户编码', kind: 'customers' }, { label: '供应商', field: '供应商编码', kind: 'suppliers' }, { label: '存货', field: '存货档案', kind: 'inventory' }]
function visibleAux(row: Row) {
  const config = props.catalogs.aux?.find((a: any) => a.account_code === row['科目编码'])?.auxiliary_fields || []
  return aux.filter(a => config.some((c: any) => c.name.startsWith(a.label)))
}
function setCode(row: Row, field: string, value: string) {
  row[field] = value
  if (field === '科目编码' && props.catalogs.subjects?.some(s => String(s.code) === value)) {
    const allowed = visibleAux(row).map(a => a.field)
    aux.forEach(a => { if (!allowed.includes(a.field)) row[a.field] = '' })
  }
  emit('change')
}
function side(row: Row, debit: string, credit: string) {
  return Number(row[debit]) !== 0 || Number(row['借方金额']) !== 0 ? debit : credit
}
function quantityField(row: Row) { return side(row, '借方数量', '贷方数量') }
function originalField(row: Row) { return side(row, '原币借方', '原币贷方') }
function hasQuantity(row: Row) { return !!(Number(row['借方数量']) || Number(row['贷方数量']) || Number(row['单价'])) }
const quantityEditing = ref('')
</script>

<template>
  <fieldset class="voucher-fields" :disabled="readonly" :class="{ 'read-only': readonly }">
  <div class="voucher-scroll">
    <table class="voucher-grid">
      <colgroup><col style="width:4%"><col style="width:15%"><col style="width:15%"><col style="width:16%"><col style="width:5%"><col style="width:11%"><col style="width:10%"><col style="width:11%"><col style="width:12%"></colgroup>
      <thead><tr><th>分录</th><th>摘要</th><th>会计科目</th><th>辅助核算项</th><th>币种</th><th>原币</th><th>数量</th><th>借方金额</th><th>贷方金额</th></tr></thead>
      <tbody>
        <tr v-for="(row, index) in draft.rows" :key="row.id">
          <td class="sequence"><span>{{ index + 1 }}</span><div class="row-tools">
            <button title="上移" aria-label="上移分录" @click="move(index, -1)"><ArrowUp :size="12" /></button><button title="下移" aria-label="下移分录" @click="move(index, 1)"><ArrowDown :size="12" /></button>
            <button title="删除分录" aria-label="删除分录" @click="draft.rows.splice(index, 1); emit('change')"><Trash2 :size="12" /></button></div></td>
          <td :class="{ invalid: bad(row, '摘要'), modified: edited(row, '摘要') }" :title="reason(row, '摘要')"><button v-if="bad(row, '摘要')" type="button" class="cell-issue" :aria-label="reason(row, '摘要')" :title="reason(row, '摘要')" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button><textarea :data-target="row.id + '/摘要'" aria-label="摘要" :value="row['摘要']" @input="update(row, '摘要', $event)" @blur="emit('blur')" /></td>
          <td :class="{ invalid: bad(row, '科目编码'), modified: edited(row, '科目编码') }" :title="reason(row, '科目编码')"><button v-if="bad(row, '科目编码')" type="button" class="cell-issue" :aria-label="reason(row, '科目编码')" :title="reason(row, '科目编码')" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button>
            <CatalogInput :model-value="row['科目编码']" :items="catalogs.subjects?.filter(s => s.leaf) || []" label="科目编码" :target="row.id + '/科目编码'" @update:model-value="setCode(row, '科目编码', $event)" @blur="emit('blur')" />
          </td>
          <td><div v-for="a in visibleAux(row)" :key="a.field" class="aux-entry" :class="{ invalid: bad(row, a.field), modified: edited(row, a.field) }" :title="reason(row, a.field)"><button v-if="bad(row, a.field)" type="button" class="cell-issue" :aria-label="reason(row, a.field)" :title="reason(row, a.field)" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button>
            <small>【{{ a.label }}】</small><CatalogInput :model-value="row[a.field]" :items="catalogs[a.kind] || []" :label="a.field" :target="row.id + '/' + a.field" @update:model-value="setCode(row, a.field, $event)" @blur="emit('blur')" />
          </div></td>
          <td :class="{ invalid: bad(row, '币种名称') }" :title="reason(row, '币种名称')"><button v-if="bad(row, '币种名称')" type="button" class="cell-issue" :aria-label="reason(row, '币种名称')" :title="reason(row, '币种名称')" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button><select aria-label="币种" :value="row['币种名称']" @change="update(row, '币种名称', $event)"><option v-for="c in ['CNY','USD','EUR','GBP','JPY']">{{ c }}</option></select></td>
          <td class="original"><div class="original-stack"><small>{{ draft.header.posting_date }}　基准汇率</small>
            <label :class="{ invalid: bad(row, '汇率') }" :title="reason(row, '汇率')"><button v-if="bad(row, '汇率')" type="button" class="cell-issue" :aria-label="reason(row, '汇率')" :title="reason(row, '汇率')" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button><span>*</span><AmountInput :model-value="row['汇率']" label="汇率" :target="row.id + '/汇率'" :places="6" :blank-zero="false" :candidates="candidates(row, '汇率')" @update:model-value="setValue(row, '汇率', $event)" @blur="emit('blur')" @error="emit('error', $event)" /></label>
            <label class="original-amount" :class="{ invalid: bad(row, originalField(row)) }" :title="reason(row, originalField(row))"><button v-if="bad(row, originalField(row))" type="button" class="cell-issue" :aria-label="reason(row, originalField(row))" :title="reason(row, originalField(row))" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button>
              <AmountInput :model-value="row[originalField(row)]" :label="originalField(row)" :target="row.id + '/' + originalField(row)" :candidates="candidates(row, originalField(row))" @update:model-value="setValue(row, originalField(row), $event)" @blur="emit('blur')" @error="emit('error', $event)" /></label>
          </div></td>
          <td class="quantities" @focusin="quantityEditing = row.id" @focusout="quantityEditing = ''; emit('blur')">
            <div class="quantity-content" :class="{ 'quantity-empty': !hasQuantity(row) && quantityEditing !== row.id }">
              <label :class="{ invalid: bad(row, quantityField(row)) }" :title="reason(row, quantityField(row))"><button v-if="bad(row, quantityField(row))" type="button" class="cell-issue" :aria-label="reason(row, quantityField(row))" :title="reason(row, quantityField(row))" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button><small>数量:</small><AmountInput :model-value="row[quantityField(row)]" :label="quantityField(row)" :target="row.id + '/' + quantityField(row)" :candidates="candidates(row, quantityField(row))" @update:model-value="setValue(row, quantityField(row), $event)" @blur="emit('blur')" @error="emit('error', $event)" /><span>KG</span></label>
              <label :class="{ invalid: bad(row, '单价') }" :title="reason(row, '单价')"><button v-if="bad(row, '单价')" type="button" class="cell-issue" :aria-label="reason(row, '单价')" :title="reason(row, '单价')" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button><small>单价:</small><AmountInput :model-value="row['单价']" label="单价" :target="row.id + '/单价'" :candidates="candidates(row, '单价')" @update:model-value="setValue(row, '单价', $event)" @blur="emit('blur')" @error="emit('error', $event)" /></label>
            </div>
          </td>
          <td v-for="f in ['借方金额','贷方金额']" :key="f" class="money" :class="{ invalid: bad(row, f), modified: edited(row, f) }" :title="reason(row, f)"><button v-if="bad(row, f)" type="button" class="cell-issue" :aria-label="reason(row, f)" :title="reason(row, f)" @click.stop.prevent="focusIssue"><CircleAlert :size="15" /></button>
            <AmountInput :model-value="row[f]" :label="f" :target="row.id + '/' + f" :candidates="candidates(row, f)" @update:model-value="setValue(row, f, $event)" @blur="emit('blur')" @error="emit('error', $event)" />
          </td>
        </tr>
      </tbody>
      <tfoot><tr><td colspan="7">合计 <span class="muted">人民币 · CNY</span></td><td>{{ amount(draft.totals.debit) }}</td><td>{{ amount(draft.totals.credit) }}</td></tr></tfoot>
    </table>
  </div>
  <button v-if="!readonly" class="add-row" @click="add"><Plus :size="14" /> 添加分录</button>
  </fieldset>
</template>
