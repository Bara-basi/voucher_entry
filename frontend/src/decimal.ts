// Decimal arithmetic without binary floating-point rounding or expression evaluation.
function decimal(value: string) {
  const text = value.trim()
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(text) || text.length > 80) throw Error('请输入有效数字')
  const scale = text.split('.')[1]?.length || 0
  return { value: BigInt(text.replace('.', '')), scale }
}
function format(value: bigint, scale: number, places: number) {
  const sign = value < 0n ? '-' : ''
  let n = value < 0n ? -value : value
  if (scale > places) {
    const divisor = 10n ** BigInt(scale - places)
    n = (n + divisor / 2n) / divisor
  } else n *= 10n ** BigInt(places - scale)
  const text = n.toString().padStart(places + 1, '0')
  return (n ? sign : '') + (places ? text.slice(0, -places) + '.' + text.slice(-places) : text)
}
export function multiply(a: string, b: string) {
  const x = decimal(a), y = decimal(b)
  return format(x.value * y.value, x.scale + y.scale, 2)
}
export function calculate(expression: string, places = 2) {
  const text = expression.replace(/^=/, '').replace(/\s/g, '')
  if (!/^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[+-](?:\d+(?:\.\d*)?|\.\d+))*$/.test(text) || text.length > 500)
    throw Error('加减公式不完整')
  const terms = text.match(/[+-]?(?:\d+(?:\.\d*)?|\.\d+)/g)!.map(decimal)
  const scale = Math.max(...terms.map(t => t.scale))
  return format(terms.reduce((sum, t) => sum + t.value * 10n ** BigInt(scale - t.scale), 0n), scale, places)
}
