import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import MergeWorksheetView from '@/views/MergeWorksheetView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/merge-worksheets', name: 'merge-worksheets', component: MergeWorksheetView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
