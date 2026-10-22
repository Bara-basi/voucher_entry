export type Row = { id: string; [key: string]: string }
export type Issue = { id: string; message: string; row_id?: string; field?: string; severity: string; record_id?: string; source_field?: string }
export type Draft = {
  id: string; version: number; customs_no: string; contracts: string[]; search_index: string;
  rows: Row[]; header: Record<string, string>; source: Record<string, any>[];
  overrides: Record<string, Record<string, any>>; issues: Issue[];
  manual_exception?: boolean; manual_fields: string[]; valid: boolean; mapping_stale?: boolean; exports: string[];
  incoming_source?: Record<string, any>[];
  totals: { debit: string; credit: string; difference: string }; updated_at: string;
}

export function newId(): string {
  const cryptoApi = globalThis.crypto
  if (typeof cryptoApi?.randomUUID === 'function') return cryptoApi.randomUUID()

  const bytes = new Uint8Array(16)
  if (typeof cryptoApi?.getRandomValues === 'function') {
    cryptoApi.getRandomValues(bytes)
  } else {
    for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256)
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  bytes[8] = (bytes[8] & 0x3f) | 0x80
  const hex = [...bytes].map(value => value.toString(16).padStart(2, '0')).join('')
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
}

const session = localStorage.getItem('voucher-session') || newId()
localStorage.setItem('voucher-session', session)
export async function api<T = any>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method, headers: { 'Content-Type': 'application/json', 'X-Session-ID': session },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const data = await response.json()
  if (!response.ok) throw Object.assign(new Error(typeof data.detail === 'string' ? data.detail : '请求参数无效'), { status: response.status })
  return data
}
export const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value))
export const amount = (value: unknown) => value === '' || value == null ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
