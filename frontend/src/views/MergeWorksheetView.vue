<template>
  <section class="page merge-review-page" data-testid="merge-review">
    <header class="page-head">
      <div>
        <h2>归并差异底稿</h2>
        <p class="page-desc">
          先复核两侧导出的同键差异，再重做归并。底稿只读源导出，裁定不会回写任何源值；
          发布明细逐行带批次、文件版本与原行行号，可回溯到这对输入。
        </p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create-worksheet" type="button" @click="showCreate = true">
          新建差异底稿
        </button>
        <button class="btn" type="button" @click="reloadLedger">刷新台账</button>
      </div>
    </header>

    <p v-if="errorMessage" class="error-text" data-testid="review-error">{{ errorMessage }}</p>

    <!-- 输入对漂移提示 -->
    <div v-if="current && current.reopen_check.pair_drifted" class="drift-banner" data-testid="pair-drift">
      ⚠ 输入对已漂移：
      <span v-if="current.reopen_check.primary_changed">主源导出文件已变更；</span>
      <span v-if="current.reopen_check.secondary_changed">从源导出文件已变更；</span>
      当前指纹 {{ current.reopen_check.current_pair_id }} ≠ 底稿指纹
      {{ current.reopen_check.stored_pair_id }}，请基于新导出重建底稿。
    </div>

    <!-- 台账 -->
    <table class="data-table" data-testid="worksheet-ledger">
      <thead>
        <tr>
          <th>底稿号</th>
          <th>状态</th>
          <th>归并键</th>
          <th>主源导出</th>
          <th>从源导出</th>
          <th>输入对指纹</th>
          <th>创建时间</th>
          <th>重开检查</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="entry in ledger" :key="entry.worksheet_id" :data-testid="`ledger-${entry.worksheet_id}`">
          <td>{{ entry.worksheet_id }}</td>
          <td>{{ entry.status }}</td>
          <td>{{ entry.merge_key }}</td>
          <td>#{{ entry.primary_export_id }}</td>
          <td>#{{ entry.secondary_export_id }}</td>
          <td class="mono">{{ entry.input_pair_id }}</td>
          <td>{{ formatTime(entry.created_at) }}</td>
          <td>
            <span :class="entry.reopen_check.pair_drifted ? 'tag tag-warn' : 'tag tag-ok'">
              {{ entry.reopen_check.pair_drifted ? '输入已变更' : '与原输入一致' }}
            </span>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="openWorksheet(entry.worksheet_id)">打开底稿</button>
          </td>
        </tr>
        <tr v-if="!ledger.length">
          <td colspan="9" class="empty-state">暂无底稿，先选择两份导出新建差异底稿</td>
        </tr>
      </tbody>
    </table>

    <!-- 新建底稿弹窗 -->
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-worksheet-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>新建归并差异底稿</h3>
        <label class="create-field">
          <span>主源导出 ID</span>
          <input v-model.number="createForm.primary_export_id" data-testid="create-primary" type="number" required />
        </label>
        <label class="create-field">
          <span>从源导出 ID</span>
          <input v-model.number="createForm.secondary_export_id" data-testid="create-secondary" type="number" required />
        </label>
        <label class="create-field">
          <span>归并键列名</span>
          <input v-model="createForm.merge_key" data-testid="create-key" placeholder="如 id" required />
        </label>
        <label class="create-field">
          <span>归并规则 ID（可选）</span>
          <input v-model.number="createForm.rule_id" data-testid="create-rule" type="number" />
        </label>
        <p class="page-desc">建立底稿时会固化两份导出文件的 SHA-256 指纹，任一侧换文件，重开即可区分。</p>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create-worksheet" type="submit" :disabled="submitting">
            {{ submitting ? '生成中…' : '生成底稿' }}
          </button>
        </div>
      </form>
    </div>

    <!-- 底稿详情 -->
    <div v-if="current" class="worksheet-detail" data-testid="worksheet-detail">
      <div class="detail-head">
        <div>
          <h3>底稿 #{{ current.worksheet_id }}（{{ current.status }}）</h3>
          <p class="page-desc mono">
            输入对指纹：{{ current.input_pair_id }} ｜ 归并键：{{ current.merge_key }}
          </p>
        </div>
        <div class="page-actions">
          <button
            v-if="current.status === '待复核'"
            class="btn"
            type="button"
            data-testid="apply-policy-primary"
            @click="onApplyPolicy('主源优先')"
          >按主源优先预填</button>
          <button
            v-if="current.status === '待复核'"
            class="btn"
            type="button"
            data-testid="apply-policy-secondary"
            @click="onApplyPolicy('从源覆盖')"
          >按从源覆盖预填</button>
          <button
            v-if="current.status === '待复核'"
            class="btn primary"
            type="button"
            data-testid="confirm-worksheet"
            @click="onConfirm"
          >确认底稿</button>
          <button
            v-if="current.status === '已确认'"
            class="btn primary"
            type="button"
            data-testid="publish-worksheet"
            @click="onPublish"
          >归并发布</button>
          <button class="btn ghost" type="button" @click="current = null">返回台账</button>
        </div>
      </div>

      <div class="stat-row">
        <article class="stat-card">
          <span class="stat-label">同键配对</span>
          <strong class="stat-value">{{ current.summary.pair_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">直通免纠错（相同值）</span>
          <strong class="stat-value">{{ current.summary.straight_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">差异纠错（差异值/缺失值）</span>
          <strong class="stat-value">{{ current.summary.diff_correction_count }}</strong>
        </article>
        <article class="stat-card">
          <span class="stat-label">待确认原行</span>
          <strong class="stat-value">{{ current.summary.pending_count }}</strong>
        </article>
      </div>

      <!-- 两侧数据源与文件版本 -->
      <div class="source-grid">
        <article v-for="meta in [current.primary_source, current.secondary_source]" :key="meta.side" class="source-card">
          <h4>{{ meta.side === 'primary' ? '主源' : '从源' }}数据源</h4>
          <dl>
            <dt>导出 ID / 批次</dt><dd>#{{ meta.export_id }} ｜ {{ meta.batch_no }}</dd>
            <dt>文件版本</dt><dd>{{ meta.file_version }}</dd>
            <dt>API</dt><dd>{{ meta.api_name || '—' }}</dd>
            <dt>数据行数</dt><dd>{{ meta.row_count }}</dd>
            <dt>文件指纹 SHA-256</dt><dd class="mono hash">{{ meta.sha256 }}</dd>
          </dl>
        </article>
      </div>

      <!-- 同键对照分组 -->
      <div v-for="group in current.review.key_groups" :key="group.merge_key_value" class="key-group"
           :data-testid="`key-group-${group.merge_key_value}`">
        <h4 class="key-group-title">
          归并键 {{ group.merge_key }} = {{ group.merge_key_value }}
          <span class="page-desc">
            主源 {{ group.primary_row_count }} 行 / 从源 {{ group.secondary_row_count }} 行
          </span>
        </h4>

        <div v-for="pair in group.pairs" :key="pair.pair_id" class="pair-block"
             :data-testid="`pair-${pair.pair_id}`">
          <div class="pair-head">
            <span class="tag" :class="routeTagClass(pair.route)">{{ pair.route }}</span>
            <span class="mono">{{ pair.pair_id }}</span>
            <span class="page-desc">
              主源 {{ refText(pair.primary_ref) }} ↔ 从源 {{ refText(pair.secondary_ref) }}
            </span>
          </div>
          <table class="data-table compare-table">
            <thead>
              <tr>
                <th>字段</th>
                <th>主源值（原行）</th>
                <th>从源值（原行）</th>
                <th>比对结论</th>
                <th v-if="current.status === '待复核'">复核裁定</th>
                <th v-else>裁定结果</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="cell in pair.cells" :key="cell.field" :data-verdict="cell.verdict"
                  :data-testid="`cell-${pair.pair_id}-${cell.field}`">
                <td>{{ cell.field }}<span v-if="cell.is_merge_key" class="key-badge">归并键</span></td>
                <td class="cell-value">{{ cell.primary_value ?? '—' }}</td>
                <td class="cell-value">{{ cell.secondary_value ?? '—' }}</td>
                <td><span class="tag" :class="verdictTagClass(cell.verdict)">{{ cell.verdict }}</span></td>
                <td>
                  <template v-if="cell.verdict === '相同值'">
                    <span class="page-desc">直通，无需纠错</span>
                  </template>
                  <template v-else-if="current.status === '待复核'">
                    <div class="resolve-row">
                      <button
                        v-for="choice in resolutionChoices"
                        :key="choice"
                        class="btn tiny"
                        :class="{ 'primary': cell.resolution === choice }"
                        type="button"
                        :data-testid="`resolve-${pair.pair_id}-${cell.field}-${choice}`"
                        @click="onResolve(pair.pair_id, cell.field, choice)"
                      >{{ choice }}</button>
                      <input
                        v-if="cell.resolution === '人工修正值'"
                        :value="cell.manual_value ?? ''"
                        class="manual-input"
                        data-testid="manual-value"
                        placeholder="输入修正值"
                        @change="onResolve(pair.pair_id, cell.field, '人工修正值', ($event.target as HTMLInputElement).value)"
                      />
                    </div>
                  </template>
                  <template v-else>
                    <span>{{ cell.resolution }}：{{ cell.resolved_value ?? '—' }}</span>
                  </template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 配不上对的待确认原行 -->
        <div v-for="pending in group.pending_rows" :key="pending.pending_id" class="pair-block pending-block"
             :data-testid="`pending-${pending.pending_id}`">
          <div class="pair-head">
            <span class="tag tag-warn">待确认原行</span>
            <span class="mono">{{ pending.pending_id }}</span>
            <span class="page-desc">
              {{ pending.side === 'primary' ? '主源' : '从源' }}原行
              {{ refText(pending.source_ref) }} 在对侧没有同键配对
            </span>
          </div>
          <table class="data-table compare-table">
            <thead>
              <tr><th>字段</th><th>原值（来自{{ pending.side === 'primary' ? '主源' : '从源' }}）</th><th>处置</th></tr>
            </thead>
            <tbody>
              <tr v-for="cell in pendingValueCells(pending)" :key="cell.field">
                <td>{{ cell.field }}</td>
                <td class="cell-value">{{ cell.primary_value ?? cell.secondary_value ?? '—' }}</td>
                <td>
                  <template v-if="current.status === '待复核'">
                    <button class="btn tiny" :class="{ primary: pending.handling === '手工保留' }" type="button"
                            @click="onPending(pending.pending_id, '手工保留')">手工保留</button>
                    <button class="btn tiny" :class="{ primary: pending.handling === '放弃该行' }" type="button"
                            @click="onPending(pending.pending_id, '放弃该行')">放弃该行</button>
                  </template>
                  <template v-else>{{ pending.handling }}</template>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- 发布产物与回溯 -->
      <div v-if="published" class="publish-panel" data-testid="publish-panel">
        <h4>发布明细 #{{ published.published_id }}（{{ published.row_count }} 行）</h4>
        <p class="page-desc">
          发布时间 {{ formatTime(published.published_at) }} ｜ 输入对指纹
          <span class="mono">{{ published.input_pair_id }}</span>
        </p>
        <div class="page-actions">
          <button class="btn" type="button" data-testid="verify-published" @click="onVerify">回溯核对这对输入</button>
        </div>
        <table v-if="verifyResult" class="data-table" data-testid="verify-result">
          <thead>
            <tr><th>侧</th><th>批次</th><th>文件版本</th><th>记录指纹</th><th>当前指纹</th><th>结论</th></tr>
          </thead>
          <tbody>
            <tr v-for="file in verifyResult.files" :key="file.side">
              <td>{{ file.side === 'primary' ? '主源' : '从源' }}</td>
              <td>{{ file.batch_no }}</td>
              <td>{{ file.file_version }}</td>
              <td class="mono hash">{{ file.recorded_sha256 }}</td>
              <td class="mono hash">{{ file.current_sha256 || '文件缺失' }}</td>
              <td>
                <span :class="file.unchanged ? 'tag tag-ok' : 'tag tag-warn'">
                  {{ file.unchanged ? '未变更' : '已变更' }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="verifyResult" :class="verifyResult.ok ? 'ok-text' : 'error-text'">
          {{ verifyResult.ok
            ? `核对通过：${verifyResult.files.length} 份源文件均与发布时一致，发布行溯源全部有效。`
            : `核对失败：输入对已不是发布时那一对（异常行号 ${verifyResult.bad_row_numbers.join('、') || '无'}）。` }}
        </p>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  applyPolicy,
  confirmWorksheet,
  createWorksheet,
  decidePending,
  getWorksheet,
  listWorksheets,
  publishWorksheet,
  resolveCell,
  verifyPublished,
} from '@/api/merge-worksheet'
import type {
  CellVerdict,
  CompareCell,
  PendingRecord,
  PublishedArtifact,
  RouteKind,
  SourceRef,
  VerifyResult,
  Worksheet,
  WorksheetIndexEntry,
} from '@/api/merge-worksheet'

const ledger = ref<WorksheetIndexEntry[]>([])
const current = ref<Worksheet | null>(null)
const published = ref<PublishedArtifact | null>(null)
const verifyResult = ref<VerifyResult | null>(null)
const errorMessage = ref('')
const showCreate = ref(false)
const submitting = ref(false)
const createForm = ref({
  primary_export_id: undefined as number | undefined,
  secondary_export_id: undefined as number | undefined,
  merge_key: 'id',
  rule_id: undefined as number | undefined,
})

const resolutionChoices = ['取主源', '取从源', '人工修正值'] as const

function formatTime(value: string): string {
  return value ? value.replace('T', ' ').slice(0, 19) : '—'
}

function refText(ref: SourceRef): string {
  return `${ref.batch_no} / ${ref.file_version} / 原行第${ref.row_number}行`
}

function verdictTagClass(verdict: CellVerdict): string {
  if (verdict === '相同值') return 'tag-ok'
  if (verdict === '差异值') return 'tag-warn'
  return 'tag-muted'
}

function routeTagClass(route: RouteKind): string {
  if (route === '直通免纠错') return 'tag-ok'
  if (route === '差异纠错') return 'tag-warn'
  return 'tag-muted'
}

function pendingValueCells(pending: PendingRecord): CompareCell[] {
  return pending.cells.filter(
    (cell) => !cell.is_merge_key && (cell.primary_value !== null || cell.secondary_value !== null),
  )
}

async function reloadLedger() {
  errorMessage.value = ''
  try {
    ledger.value = await listWorksheets()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '底稿台账读取失败'
  }
}

async function submitCreate() {
  if (!createForm.value.primary_export_id || !createForm.value.secondary_export_id) {
    errorMessage.value = '请填写两侧导出 ID'
    return
  }
  submitting.value = true
  errorMessage.value = ''
  try {
    const worksheet = await createWorksheet({
      primary_export_id: createForm.value.primary_export_id,
      secondary_export_id: createForm.value.secondary_export_id,
      merge_key: createForm.value.merge_key,
      rule_id: createForm.value.rule_id ?? null,
    })
    showCreate.value = false
    await reloadLedger()
    current.value = worksheet
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '底稿生成失败'
  } finally {
    submitting.value = false
  }
}

async function openWorksheet(id: number) {
  errorMessage.value = ''
  published.value = null
  verifyResult.value = null
  try {
    current.value = await getWorksheet(id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '底稿打开失败'
  }
}

async function onApplyPolicy(policy: string) {
  if (!current.value) return
  try {
    current.value = await applyPolicy(current.value.worksheet_id, policy)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '策略预填失败'
  }
}

async function onResolve(pairId: string, field: string, resolution: string, manualValue?: string) {
  if (!current.value) return
  try {
    current.value = await resolveCell(current.value.worksheet_id, pairId, field, resolution, manualValue)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '裁定失败'
  }
}

async function onPending(pendingId: string, decision: string) {
  if (!current.value) return
  try {
    current.value = await decidePending(current.value.worksheet_id, pendingId, decision)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '待确认原行处置失败'
  }
}

async function onConfirm() {
  if (!current.value) return
  try {
    current.value = await confirmWorksheet(current.value.worksheet_id)
    await reloadLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '底稿确认失败'
  }
}

async function onPublish() {
  if (!current.value) return
  try {
    published.value = await publishWorksheet(current.value.worksheet_id)
    current.value = await getWorksheet(current.value.worksheet_id)
    await reloadLedger()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '归并发布失败'
  }
}

async function onVerify() {
  if (!published.value) return
  try {
    verifyResult.value = await verifyPublished(published.value.published_id)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回溯核对失败'
  }
}

onMounted(reloadLedger)
</script>
