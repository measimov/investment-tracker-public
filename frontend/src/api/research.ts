/** 研究：观察清单、标的档案与分析、标的检索、雪球采集器与观点、官方公告、财报摘要回填、AI 复盘。 */
import { apiClient, type QueryParams } from './client'
import type {
  ActiveAnalysisJob,
  AnalysisBatchJob,
  AnalysisJob,
  AnalysisSummaryRow,
  CollectorAuthor,
  CollectorAuthorCreate,
  CollectorAuthorUpdate,
  CollectorCube,
  CollectorCubeCreate,
  CollectorCubeUpdate,
  CollectorStatus,
  DigestBatchJob,
  LlmReportAskResponse,
  LlmReportDetail,
  LlmReportListItem,
  LlmReportSchedule,
  OpinionBatchJob,
  OpinionJob,
  OpinionSummariesResponse,
  OpinionSummaryDetail,
  RecentAnnouncements,
  SecurityAnnouncements,
  SecurityIndustryItem,
  SecurityResolveResponse,
  SecuritySearchResponse,
  StartedJob,
  WatchlistItem,
  WatchlistItemCreate,
  WatchlistItemUpdate,
  WatchlistMembership,
  XueqiuCookieAdminStatus,
  XueqiuCookieUpdateRequest,
  XueqiuCookieUpdateResponse,
  XueqiuSymbolFeed
} from '@/types'

export const researchApi = {
  // 观察清单（未持仓标的的观察区域；正式论点见 security_theses）
  getWatchlist() {
    return apiClient.get<WatchlistItem[]>('/watchlist')
  },
  // 详情页轻量 membership 查询（不拉整份 enriched 列表）
  watchlistContains(symbol: string, market: string) {
    return apiClient.get<WatchlistMembership>('/watchlist/contains', {
      params: { symbol, market }
    })
  },
  addWatchlistItem(data: WatchlistItemCreate) {
    return apiClient.post<WatchlistItem>('/watchlist', data)
  },
  updateWatchlistItem(id: number, data: WatchlistItemUpdate) {
    return apiClient.put<WatchlistItem>(`/watchlist/${id}`, data)
  },
  removeWatchlistItem(id: number) {
    return apiClient.delete<void>(`/watchlist/${id}`)
  },

  // 标的档案（基本面数据 + LLM 分析；A股/美股/港股）
  listSecurityAnalyses() {
    return apiClient.get<AnalysisSummaryRow[]>('/securities/analyses')
  },
  getSecurityAnalysis(market: string, symbol: string) {
    return apiClient.get(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/analysis`
    )
  },
  getSecurityProfile(market: string, symbol: string) {
    return apiClient.get(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/profile`
    )
  },
  startSecurityAnalysisJob(market: string, symbol: string) {
    return apiClient.post<StartedJob<AnalysisJob>>(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/analysis-jobs`
    )
  },
  getSecurityAnalysisJob(id: string) {
    return apiClient.get<AnalysisJob>(`/securities/analysis-jobs/${id}`)
  },
  startReportBackfillJob(market: string, symbol: string) {
    return apiClient.post(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/report-backfill-jobs`
    )
  },
  // 批量分析（持仓页一键分析；每用户单活跃任务，可能运行数十分钟到数小时）
  startSecurityAnalysisBatchJob(params?: QueryParams) {
    return apiClient.post<StartedJob<AnalysisBatchJob>>('/securities/analysis-batch-jobs', null, {
      params
    })
  },
  // 目标预览：确认框的数量/耗时估算必须与后端真实目标一致
  getSecurityAnalysisBatchTargets() {
    return apiClient.get('/securities/analysis-batch-targets')
  },
  getSecurityAnalysisBatchJob(jobId: string) {
    return apiClient.get<AnalysisBatchJob>(`/securities/analysis-batch-jobs/${jobId}`)
  },
  cancelSecurityAnalysisBatchJob(jobId: string) {
    return apiClient.post<AnalysisBatchJob>(`/securities/analysis-batch-jobs/${jobId}/cancel`)
  },
  // 无活跃任务时后端返回 200 + 空数组（刷新页面后恢复进度显示用）
  listActiveAnalysisJobs() {
    return apiClient.get<ActiveAnalysisJob[]>('/securities/active-analysis-jobs')
  },
  // 标的检索（账本行优先 + 标的全集）与手输代码的按需解析；失败静默——自动补全是锦上添花
  searchSecurities(params: { q?: string; market?: string; limit?: number }) {
    return apiClient.get<SecuritySearchResponse>('/securities/search', {
      params,
      skipGlobalErrorNotification: true
    })
  },
  // 持仓 ∪ 观察清单的行业分类（规则 > 官方 > 东方财富）；只读库不外呼，失败由调用方静默
  listSecurityIndustries() {
    return apiClient.get<SecurityIndustryItem[]>('/securities/industries', {
      skipGlobalErrorNotification: true
    })
  },
  resolveSecurity(params: { symbol: string; market: string }) {
    return apiClient.get<SecurityResolveResponse>('/securities/resolve', {
      params,
      skipGlobalErrorNotification: true
    })
  },
  // 雪球发言采集器：状态与作者名单对登录用户可读；增删改与「立即运行」仅管理员。
  // 立即运行只记请求，由独立的 xueqiu-collector 进程在 30s 内拾取
  getCollectorStatus() {
    return apiClient.get<CollectorStatus>('/xueqiu-collector/status')
  },
  createCollectorAuthor(data: CollectorAuthorCreate) {
    return apiClient.post<CollectorAuthor>('/xueqiu-collector/authors', data)
  },
  updateCollectorAuthor(userId: string, data: CollectorAuthorUpdate) {
    return apiClient.patch<CollectorAuthor>(
      `/xueqiu-collector/authors/${encodeURIComponent(userId)}`,
      data
    )
  },
  deleteCollectorAuthor(userId: string) {
    return apiClient.delete<void>(`/xueqiu-collector/authors/${encodeURIComponent(userId)}`)
  },
  requestCollectorRun(target: 'authors' | 'symbols' = 'authors') {
    return apiClient.post<CollectorStatus>('/xueqiu-collector/run-now', null, {
      params: { target }
    })
  },
  // 雪球 Cookie（仅管理员）：只返回名称与到期事实，永不回显值。
  // 更新的错误（缺主凭证/已过期/目录不可写）由对话框就地展示，不再弹全局通知
  getXueqiuCookieStatus() {
    return apiClient.get<XueqiuCookieAdminStatus>('/xueqiu-collector/cookie')
  },
  updateXueqiuCookie(data: XueqiuCookieUpdateRequest) {
    return apiClient.put<XueqiuCookieUpdateResponse>('/xueqiu-collector/cookie', data, {
      skipGlobalErrorNotification: true
    })
  },
  // 组合跟踪名单（按标的采集的组合调仓），权限同作者名单
  createCollectorCube(data: CollectorCubeCreate) {
    return apiClient.post<CollectorCube>('/xueqiu-collector/cubes', data)
  },
  updateCollectorCube(cubeId: string, data: CollectorCubeUpdate) {
    return apiClient.patch<CollectorCube>(
      `/xueqiu-collector/cubes/${encodeURIComponent(cubeId)}`,
      data
    )
  },
  deleteCollectorCube(cubeId: string) {
    return apiClient.delete<void>(`/xueqiu-collector/cubes/${encodeURIComponent(cubeId)}`)
  },
  // 采集器每日按标的落库的只读展示：标的的雪球公告/讨论。
  // 锦上添花的数据，失败由调用方静默处理，不弹全局通知
  getXueqiuSymbolFeed(params: { symbol: string; market: string; kind?: string; limit?: number }) {
    return apiClient.get<XueqiuSymbolFeed>('/xueqiu-collector/symbol-feed', {
      params,
      skipGlobalErrorNotification: true
    })
  },
  // 官方公告（巨潮/披露易/EDGAR，#306）：只读库不外呼。
  // 标的时间线失败由详情页 tab 自行展示；持仓/自选徽标失败静默
  getSecurityAnnouncements(
    market: string,
    symbol: string,
    params?: { importance?: string; category?: string; before?: string; limit?: number }
  ) {
    return apiClient.get<SecurityAnnouncements>(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/announcements`,
      { params, skipGlobalErrorNotification: true }
    )
  },
  getRecentAnnouncements(params?: { days?: number; importance?: string }) {
    return apiClient.get<RecentAnnouncements>('/announcements/recent', {
      params,
      skipGlobalErrorNotification: true
    })
  },
  // 雪球观点摘要（数据源 = 本仓雪球采集器写入的关注作者发言）
  listOpinionSummaries() {
    return apiClient.get<OpinionSummariesResponse>('/securities/opinion-summaries')
  },
  getOpinionFeed(params?: QueryParams) {
    return apiClient.get('/securities/opinion-feed', { params })
  },
  getOpinionSummary(market: string, symbol: string) {
    return apiClient.get<OpinionSummaryDetail>(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/opinion-summary`
    )
  },
  startOpinionJob(market: string, symbol: string) {
    return apiClient.post<OpinionJob>(
      `/securities/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}/opinion-jobs`
    )
  },
  getOpinionJob(id: string) {
    return apiClient.get<OpinionJob>(`/securities/opinion-jobs/${id}`)
  },
  startOpinionBatchJob(params?: QueryParams) {
    return apiClient.post<StartedJob<OpinionBatchJob>>('/securities/opinion-batch-jobs', null, {
      params
    })
  },
  getOpinionBatchTargets() {
    return apiClient.get('/securities/opinion-batch-targets')
  },
  getOpinionBatchJob(jobId: string) {
    return apiClient.get<OpinionBatchJob>(`/securities/opinion-batch-jobs/${jobId}`)
  },
  cancelOpinionBatchJob(jobId: string) {
    return apiClient.post<OpinionBatchJob>(`/securities/opinion-batch-jobs/${jobId}/cancel`)
  },
  getReportBackfillJob(id: string) {
    return apiClient.get(`/securities/report-backfill-jobs/${id}`)
  },
  // 批量财报摘要回填（持仓页：一次给全部持仓补摘要，可重复触发续跑加深）
  getDigestBackfillPreview() {
    return apiClient.get('/securities/digest-backfill-preview')
  },
  startDigestBackfillJob() {
    return apiClient.post<DigestBatchJob>('/securities/digest-backfill-jobs')
  },
  getDigestBackfillJob(jobId: string) {
    return apiClient.get<DigestBatchJob>(`/securities/digest-backfill-jobs/${jobId}`)
  },
  cancelDigestBackfillJob(jobId: string) {
    return apiClient.post<DigestBatchJob>(`/securities/digest-backfill-jobs/${jobId}/cancel`)
  },

  // AI 复盘报告（LLM）
  getLlmReports() {
    return apiClient.get<LlmReportListItem[]>('/llm-reports')
  },
  getLlmReport(id: number | string) {
    return apiClient.get<LlmReportDetail>(`/llm-reports/${id}`)
  },
  deleteLlmReport(id: number | string) {
    return apiClient.delete<void>(`/llm-reports/${id}`)
  },
  generateLlmReport() {
    return apiClient.post('/llm-reports/generate')
  },
  getLlmReportJob(jobId: number | string) {
    return apiClient.get(`/llm-reports/jobs/${jobId}`)
  },
  // 追问为同步 LLM 调用，单独放宽超时
  askLlmReport(id: number | string, content: string) {
    return apiClient.post<LlmReportAskResponse>(
      `/llm-reports/${id}/messages`,
      { content },
      {
        timeout: 180000
      }
    )
  },
  getLlmReportSchedule() {
    return apiClient.get<LlmReportSchedule>('/llm-reports/schedule')
  },
  updateLlmReportSchedule(cadence: string) {
    return apiClient.put<LlmReportSchedule>('/llm-reports/schedule', { cadence })
  }
}
