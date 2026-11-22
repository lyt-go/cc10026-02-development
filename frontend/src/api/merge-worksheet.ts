/**
 * 归并差异底稿接口封装。
 * 后端契约见 backend/app/routers/merge_worksheet.py，前端只做薄封装与错误透出。
 */
import { request } from './client'

export type CellVerdict = '相同值' | '差异值' | '缺失值'
export type CellResolution = '待复核' | '取主源' | '取从源' | '人工修正值' | '放弃'
export type RouteKind = '直通免纠错' | '差异纠错' | '待确认原行'
export type WorksheetStatus = '待复核' | '已确认' | '已发布'

export type SourceRef = {
  side: 'primary' | 'secondary'
  export_id: number
  batch_no: string
  file_version: string
  file_path: string
  sha256: string
  row_number: number
  source_row_id: string
}

export type CompareCell = {
  field: string
  is_merge_key: boolean
  primary_value: string | null
  secondary_value: string | null
  verdict: CellVerdict
  resolution: CellResolution
  resolved_value: string | null
  manual_value: string | null
}

export type PairRecord = {
  pair_id: string
  merge_key_value: string
  row_status: string
  route: RouteKind
  equal_field_count: number
  diff_field_count: number
  primary_ref: SourceRef
  secondary_ref: SourceRef
  cells: CompareCell[]
}

export type PendingRecord = {
  pending_id: string
  side: 'primary' | 'secondary'
  merge_key_value: string
  row_status: string
  route: '待确认原行'
  source_ref: SourceRef
  cells: CompareCell[]
  handling: string
}

export type KeyGroup = {
  merge_key: string
  merge_key_value: string
  primary_row_count: number
  secondary_row_count: number
  pairs: PairRecord[]
  pending_rows: PendingRecord[]
}

export type SourceMeta = {
  side: 'primary' | 'secondary'
  export_id: number
  batch_no: string
  file_version: string
  file_path: string
  sha256: string
  api_name: string
  columns: string[]
  row_count: number
}

export type ReopenCheck = {
  available: boolean
  stored_pair_id: string
  current_pair_id: string
  primary_changed: boolean
  secondary_changed: boolean
  primary_missing: boolean
  secondary_missing: boolean
  pair_drifted: boolean
}

export type Worksheet = {
  worksheet_id: number
  status: WorksheetStatus
  rule_id: number | null
  merge_key: string
  fields: string[]
  input_pair_id: string
  primary_source: SourceMeta
  secondary_source: SourceMeta
  review: { key_groups: KeyGroup[] }
  summary: {
    key_count: number
    pair_count: number
    pending_count: number
    straight_count: number
    diff_correction_count: number
    route_counts: Record<string, number>
    open_diff_cell_count: number
  }
  created_at: string
  confirmed_at: string | null
  published_id: number | null
  reviewer?: string
  reopen_check: ReopenCheck
}

export type WorksheetIndexEntry = {
  worksheet_id: number
  input_pair_id: string
  status: WorksheetStatus
  rule_id: number | null
  merge_key: string
  primary_export_id: number
  secondary_export_id: number
  primary_sha256: string
  secondary_sha256: string
  created_at: string
  reopen_check: ReopenCheck
}

export type PublishedArtifact = {
  published_id: number
  worksheet_id: number
  input_pair_id: string
  merge_key: string
  columns: string[]
  rows: string[][]
  row_count: number
  correction_routes: Record<string, unknown>[]
  source_pair: { primary: SourceMeta; secondary: SourceMeta }
  published_at: string
}

export type VerifyResult = {
  ok: boolean
  pair_intact: boolean
  row_trace_ok: boolean
  files: {
    side: string
    export_id: number
    batch_no: string
    file_version: string
    file_path: string
    recorded_sha256: string
    current_sha256: string
    unchanged: boolean
  }[]
  bad_row_numbers: number[]
}

const BASE = '/api/merge-worksheets'

async function parse<T>(response: Response): Promise<T> {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    throw new Error(body.detail ?? `接口返回 ${response.status}`)
  }
  return body as T
}

export async function listWorksheets(): Promise<WorksheetIndexEntry[]> {
  const response = await request(BASE)
  const body = await parse<{ items: WorksheetIndexEntry[] }>(response)
  return body.items
}

export async function createWorksheet(input: {
  primary_export_id: number
  secondary_export_id: number
  merge_key: string
  rule_id?: number | null
}): Promise<Worksheet> {
  const response = await request(BASE, { method: 'POST', body: JSON.stringify(input) })
  return parse<Worksheet>(response)
}

export async function getWorksheet(id: number): Promise<Worksheet> {
  const response = await request(`${BASE}/${id}`)
  return parse<Worksheet>(response)
}

export async function applyPolicy(id: number, policy: string): Promise<Worksheet> {
  const response = await request(`${BASE}/${id}/apply-policy`, {
    method: 'POST',
    body: JSON.stringify({ policy }),
  })
  return parse<Worksheet>(response)
}

export async function resolveCell(
  id: number,
  pairId: string,
  field: string,
  resolution: string,
  manualValue?: string,
): Promise<Worksheet> {
  const response = await request(`${BASE}/${id}/resolve-cell`, {
    method: 'POST',
    body: JSON.stringify({ pair_id: pairId, field, resolution, manual_value: manualValue ?? null }),
  })
  return parse<Worksheet>(response)
}

export async function decidePending(
  id: number,
  pendingId: string,
  decision: string,
): Promise<Worksheet> {
  const response = await request(`${BASE}/${id}/pending-decision`, {
    method: 'POST',
    body: JSON.stringify({ pending_id: pendingId, decision }),
  })
  return parse<Worksheet>(response)
}

export async function confirmWorksheet(id: number, reviewer = '复核员'): Promise<Worksheet> {
  const response = await request(`${BASE}/${id}/confirm`, {
    method: 'POST',
    body: JSON.stringify({ reviewer }),
  })
  return parse<Worksheet>(response)
}

export async function publishWorksheet(id: number): Promise<PublishedArtifact> {
  const response = await request(`${BASE}/${id}/publish`, { method: 'POST' })
  return parse<PublishedArtifact>(response)
}

export async function verifyPublished(publishedId: number): Promise<VerifyResult> {
  const response = await request(`${BASE}/published/${publishedId}/verify`)
  return parse<VerifyResult>(response)
}
