<template>
  <section class="page">
    <header class="page-head">
      <div>
        <h2>运营概览</h2>
        <p class="page-desc">汇总各业务模块的关键指标，先看总量再看异常。</p>
      </div>
    </header>
    <div class="stat-row">
      <article v-for="card in cards" :key="card.label" class="stat-card">
        <span class="stat-label">{{ card.label }}</span>
        <strong class="stat-value">{{ card.value }}</strong>
      </article>
    </div>
    <table class="data-table">
      <thead>
        <tr><th>业务模块</th><th>今日新增</th><th>待处理</th><th>异常量</th></tr>
      </thead>
      <tbody>
        <tr v-for="row in moduleRows" :key="row.name">
          <td>{{ row.name }}</td>
          <td>{{ row.created }}</td>
          <td>{{ row.pending }}</td>
          <td>{{ row.abnormal }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { fetchJson } from '@/api/client'

type Overview = {
  cards: { label: string; value: number }[]
  modules: { name: string; created: number; pending: number; abnormal: number }[]
}

const cards = ref<Overview['cards']>([])
const moduleRows = ref<Overview['modules']>([])

onMounted(async () => {
  try {
    const payload = await fetchJson<Overview>('/api/overview')
    cards.value = payload.cards
    moduleRows.value = payload.modules
  } catch {
    cards.value = [{"label": "业务模块", "value": 0}, {"label": "今日新增", "value": 0}]
    moduleRows.value = [{"name": "API注册", "created": 0, "pending": 0, "abnormal": 0}, {"name": "查询路由", "created": 0, "pending": 0, "abnormal": 0}, {"name": "订阅管理", "created": 0, "pending": 0, "abnormal": 0}, {"name": "频率限制", "created": 0, "pending": 0, "abnormal": 0}, {"name": "用量计量", "created": 0, "pending": 0, "abnormal": 0}, {"name": "缓存分层", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据过滤", "created": 0, "pending": 0, "abnormal": 0}, {"name": "字段脱敏", "created": 0, "pending": 0, "abnormal": 0}, {"name": "SLA监控", "created": 0, "pending": 0, "abnormal": 0}, {"name": "结果格式化", "created": 0, "pending": 0, "abnormal": 0}, {"name": "查询优化", "created": 0, "pending": 0, "abnormal": 0}, {"name": "异常归类", "created": 0, "pending": 0, "abnormal": 0}, {"name": "数据归并", "created": 0, "pending": 0, "abnormal": 0}, {"name": "导出任务", "created": 0, "pending": 0, "abnormal": 0}, {"name": "调用审计", "created": 0, "pending": 0, "abnormal": 0}, {"name": "版本兼容", "created": 0, "pending": 0, "abnormal": 0}, {"name": "批量查询", "created": 0, "pending": 0, "abnormal": 0}, {"name": "SDK配置", "created": 0, "pending": 0, "abnormal": 0}]
  }
})
</script>
