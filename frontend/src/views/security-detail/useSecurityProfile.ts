/**
 * 标的详情页的数据层（issue #140 约定：取数与业务状态不进父 .vue）：分析 + 档案取数、
 * 生成分析 / 补齐摘要两个后台任务、观察状态。tab 子组件只读 `state`。
 *
 * 请求身份守卫：同业跳转复用同一组件实例，A 的请求慢于 B 时，A 的响应会在 B 的标题下
 * 写入 B 的数据区。每次发起时领取一个**单调递增的代次**，返回时不是最新代次就整条丢弃
 * （成功与失败都丢——过期的错误提示同样是误导）。
 *
 * 用代次而不是 `market/symbol`：后者无法区分同一标的的两代请求。A₁（慢）→ 切到 B →
 * 再切回 A 发起 A₂（快）时，A₂ 先渲染、A₁ 后到，业务 key 又相等，旧的 A₁ 仍会覆盖更新的
 * A₂（ABA）。
 */
import type { AnalysisJob } from '@/types'
import { reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { pollJobUntilDone } from '@/utils/polling'
import { showApiError } from '@/utils/showApiError'
import { backfillHasIssues } from '@/utils/reportBackfill'
import type { ProfileRow, SecurityProfileState } from './types'

function emptyState(): SecurityProfileState {
  return {
    analysisLoading: true,
    profileLoading: true,
    analysisError: '',
    profileError: '',
    analysisHasLoaded: false,
    profileHasLoaded: false,
    analysis: null,
    datasets: {},
    latestPeriods: {},
    events: [],
    supported: true,
    capabilities: {},
    business: { profile: null, peers: [], industry: null },
    reportDigests: [],
    digestProgress: { digested: 0, failed_capped: 0 },
    statementProgress: null,
    earningsQuality: {},
    grahamScreen: {},
    latestDataAt: null,
    generating: false,
    analysisJob: null,
    backfilling: false,
    backfillResult: null,
    // 观察状态三态：unknown（清单未加载完）期间不渲染按钮，防止先闪"加入观察"
    // 再变"已在观察"的抖动
    watchState: 'unknown'
  }
}

export function useSecurityProfile({
  market,
  symbol,
  isUnmounted
}: {
  market: () => string
  symbol: () => string
  isUnmounted: () => boolean
}) {
  const state = reactive<SecurityProfileState>(emptyState())

  let requestGeneration = 0

  function nextGeneration(): number {
    requestGeneration += 1
    return requestGeneration
  }

  function isStale(generation: number): boolean {
    return isUnmounted() || generation !== requestGeneration
  }

  async function loadAnalysis(generation: number) {
    try {
      const response = await api.getSecurityAnalysis(market(), symbol())
      if (isStale(generation)) return
      state.analysis = response.data
      state.analysisHasLoaded = true
      state.analysisError = ''
    } catch (error) {
      if (isStale(generation)) return
      if ((error as { response?: { status?: number } })?.response?.status === 404) {
        state.analysis = null
        state.analysisHasLoaded = true // 404 = 已知尚未生成，空态引导
        state.analysisError = ''
      } else {
        state.analysisError = getApiErrorMessage(error, '加载 AI 分析失败')
        showApiError(error, '加载 AI 分析失败')
      }
    } finally {
      if (!isStale(generation)) state.analysisLoading = false
    }
  }

  async function loadProfile(generation: number) {
    try {
      const response = await api.getSecurityProfile(market(), symbol())
      if (isStale(generation)) return
      const data = response.data
      state.datasets = data.datasets || {}
      state.latestPeriods = data.latest_periods || {}
      state.events = data.events || []
      state.supported = data.supported !== false
      state.capabilities = data.capabilities || {}
      state.business = data.business || { profile: null, peers: [], industry: null }
      state.reportDigests = data.report_digests || []
      state.digestProgress = data.digest_progress || { digested: 0, failed_capped: 0 }
      state.statementProgress = data.statement_progress || null
      state.earningsQuality = data.earnings_quality || {}
      state.grahamScreen = data.graham_screen || {}
      state.latestDataAt = data.latest_data_at || null
      state.profileHasLoaded = true
      state.profileError = ''
    } catch (error) {
      if (isStale(generation)) return
      state.profileError = getApiErrorMessage(error, '加载标的档案失败')
      showApiError(error, '加载标的档案失败')
    } finally {
      if (!isStale(generation)) state.profileLoading = false
    }
  }

  /** 一次导航 = 一个代次，analysis 与 profile 共用，互不作废 */
  function reloadAll() {
    const generation = nextGeneration()
    state.analysisLoading = true
    state.profileLoading = true
    state.analysisError = ''
    state.profileError = ''
    loadAnalysis(generation)
    loadProfile(generation)
  }

  function retryAnalysis() {
    if (state.analysisLoading || state.generating) return
    state.analysisLoading = true
    state.analysisError = ''
    return loadAnalysis(requestGeneration)
  }

  function retryProfile() {
    if (state.profileLoading || state.generating || state.backfilling) return
    state.profileLoading = true
    state.profileError = ''
    return loadProfile(requestGeneration)
  }

  // 观察状态请求纳入路由代次守卫（评审 P2）：快照当前 (market, symbol, generation)，
  // 快速同业跳转时旧响应晚到不得覆盖新标的的状态；走轻量 membership 端点，
  // 不为判断"在不在"拉整份 enriched 列表
  async function loadWatchState() {
    const generation = requestGeneration
    try {
      const response = await api.watchlistContains(symbol(), market())
      if (isStale(generation)) return
      state.watchState = response.data.watching ? 'watching' : 'not-watching'
    } catch {
      // 观察状态是锦上添花：失败保持 unknown，不打断详情页主流程
    }
  }

  async function addToWatchlist() {
    // 打开 prompt **之前**快照身份与代次（评审 P2 二轮）：prompt 挂起期间用浏览器
    // 前进/后退切到同组件的 B 页再确认，晚快照会读到 B 的 symbol/generation，
    // 把为 A 输入的理由加到 B 且 isStale 不会触发。prompt 返回后先判 stale 再 POST。
    const generation = requestGeneration
    const targetMarket = market()
    const targetSymbol = symbol()
    const targetName = state.analysis?.name || null
    let note = ''
    try {
      // ElMessageBox.prompt 的返回类型是宽联合，value 需显式收窄
      const result = (await ElMessageBox.prompt(
        '观察理由/买入条件（可留空，之后可在观察清单页编辑）',
        '加入观察',
        {
          confirmButtonText: '加入',
          cancelButtonText: '取消',
          inputType: 'textarea',
          inputPlaceholder: '如：等待 PB 回到 1.2 以下'
        }
      )) as { value?: string }
      note = (result.value || '').trim()
    } catch {
      return // 用户取消
    }
    if (isStale(generation)) return // prompt 期间已离开该标的：不代它加入
    try {
      await api.addWatchlistItem({
        symbol: targetSymbol,
        market: targetMarket,
        name: targetName,
        note: note || null
      })
      if (isStale(generation)) return
      state.watchState = 'watching'
      ElMessage.success('已加入观察清单')
    } catch (error) {
      if (isStale(generation)) return
      showApiError(error, '加入观察失败')
    }
  }

  async function backfillDigests() {
    // 用户操作沿用当前代次（不新开）：后续导航会作废它，包括切走再切回
    const generation = requestGeneration
    state.backfilling = true
    try {
      const startResponse = await api.startReportBackfillJob(market(), symbol())
      const job = await pollJobUntilDone(() => api.getReportBackfillJob(startResponse.data.id), {
        intervalMs: 3000,
        maxAttempts: 1200,
        isCancelled: () => isStale(generation),
        failureMessage: '财报摘要回填失败',
        timeoutMessage: '财报摘要回填仍在后台运行，请稍后刷新标的页查看'
      })
      if (!job || isStale(generation)) return
      state.backfillResult = (job.result as ProfileRow) || null
      const result = job.result as ProfileRow | null
      if (backfillHasIssues(result))
        ElMessage.warning('财报摘要回填完成，存在失败或待核对项，请查看结果')
      else ElMessage.success('财报摘要回填完成')
      await loadProfile(generation)
    } catch (error) {
      if (isStale(generation)) return
      showApiError(error, '财报摘要回填失败')
    } finally {
      if (!isStale(generation)) state.backfilling = false
    }
  }

  async function generateAnalysis() {
    const generation = requestGeneration
    state.generating = true
    state.analysisJob = null
    try {
      const startResponse = await api.startSecurityAnalysisJob(market(), symbol())
      if (isStale(generation)) return
      state.analysisJob = startResponse.data // 立刻显示「排队中」
      const job = await pollJobUntilDone(() => api.getSecurityAnalysisJob(startResponse.data.id), {
        intervalMs: 3000,
        maxAttempts: 400, // 3s × 400 ≈ 20 分钟：冷启动含财报摘要时 6 分钟不够
        isCancelled: () => isStale(generation),
        onUpdate: (job) => {
          // onUpdate 是每轮无条件调用的（取消检查在 fetch 之前），必须自己判过期，
          // 否则同业跳转后旧标的的进度会画进新标的的页面
          if (isStale(generation)) return
          state.analysisJob = job as AnalysisJob
        },
        timeoutMessage: '分析仍在后台运行，请稍后刷新本页查看',
        failureMessage: '标的分析生成失败'
      })
      if (!job || isStale(generation)) return
      state.analysisJob = null // 成功即收起：正文本身就是完成证据
      ElMessage.success('分析已生成')
      await Promise.all([loadAnalysis(generation), loadProfile(generation)])
    } catch (error) {
      if (isStale(generation)) return
      // 失败保留进度块：卡在哪个阶段 + 错误原文是唯一有用的残留（消息会消失）
      const message = getApiErrorMessage(error, '标的分析生成失败')
      state.analysisJob = { ...(state.analysisJob || {}), status: 'failed', error: message }
      showApiError(error, '标的分析生成失败')
    } finally {
      if (!isStale(generation)) state.generating = false
    }
  }

  /**
   * 路由参数变化（同业跳转复用组件）：清空全部派生态并重载。旧标的的在途请求由代次
   * 守卫拦下，不会写进新标的的页面（评审 P2：格雷厄姆准则卡等 profile 派生态一并清空）。
   */
  function resetAndReload() {
    Object.assign(state, emptyState())
    reloadAll()
    // 必须在 reloadAll 之后：它快照的是 reloadAll 刚推进的新代次；放在前面
    // 会拿到旧代次，自己的响应回来就被 isStale 丢弃
    loadWatchState()
  }

  function init() {
    reloadAll()
    loadWatchState()
  }

  return {
    state,
    init,
    resetAndReload,
    retryAnalysis,
    retryProfile,
    addToWatchlist,
    backfillDigests,
    generateAnalysis
  }
}
