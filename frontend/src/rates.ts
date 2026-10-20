export function scalar(value: unknown): string {
  if (value == null) return ''
  if (Array.isArray(value)) return value.map(scalar).filter(Boolean).join(' / ')
  if (typeof value === 'object') {
    const item = value as Record<string, unknown>
    return scalar(item.text ?? item.name ?? item.value)
  }
  return String(value).trim()
}
export function rateMatrix(records: any[], query = '') {
  const periods = new Map<string, Record<string, { values: string[]; sources: string[] }>>()
  const currencies = new Set<string>()
  for (const record of records) {
    const fields = record.fields || record
    const currency = scalar(fields['币种']).toUpperCase()
    const year = scalar(fields['实际年份'] ?? fields['年份'])
    const month = scalar(fields['实际月份'] ?? fields['月份'])
    const period = year && month ? `${year}-${month.padStart(2, '0')}` : '年月待补充'
    if (!currency || ['CNY', 'RMB'].includes(currency)) continue
    currencies.add(currency)
    if (!periods.has(period)) periods.set(period, {})
    const cells = periods.get(period)!
    const cell = cells[currency] ||= { values: [], sources: [] }
    const value = scalar(fields['汇率'])
    const source = scalar(fields['数据来源'])
    if (value && !cell.values.includes(value)) cell.values.push(value)
    if (source && !cell.sources.includes(source)) cell.sources.push(source)
  }
  const q = query.trim().toUpperCase()
  const all = [...currencies].sort((a, b) => a === 'USD' ? -1 : b === 'USD' ? 1 : a.localeCompare(b))
  const matchedCurrencies = q ? all.filter(code => code.includes(q)) : []
  const columns = matchedCurrencies.length ? matchedCurrencies : all
  const rows = [...periods].sort(([a], [b]) => b.localeCompare(a)).filter(([period, cells]) =>
    !q || matchedCurrencies.length || period.includes(q) || Object.values(cells).some(c => c.values.some(v => v.includes(q)))
  ).map(([period, cells]) => ({ period, cells }))
  return { columns, rows }
}
