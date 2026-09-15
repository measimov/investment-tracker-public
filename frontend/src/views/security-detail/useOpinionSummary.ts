/**
 * 标的详情页的雪球观点摘要（issue #140：逻辑不进父 .vue）。
 *
 * 世代守卫复刻父页模式：同标的详情组件会被同类路由复用（同业跳转），
 * 任何异步返回后必须先判 stale 再写状态。404（暂无摘要）不是错误；
 * 409（数据源未接入 / 互斥任务进行中）展示为 section 内信息条而非 toast。
 */

import { reactive } from 'vue'
import api from '@/api'
import { pollJobUntilDone } from '@/utils/polling'
import { getApiErrorMessage } from '@/utils/apiErrors'
import type { OpinionJob, OpinionSummaryDetail } from './types'

export const OPINION_POLL_INTERVAL_MS = 2000
export const OPINION_POLL_MAX_ATTEMPTS = 150 // 单标的一次 LLM 调用，5 分钟兜底

export function useOpinionSummary({
  symbol,
  market,
  isUnmounted
}: {
  symbol: () => string
  market: () => string
  isUnmounted: () => boolean
}) {
  const state = reactive({
    loading: false,
    generating: false,
    summary: null as OpinionSummaryDetail | null,
    job: null as OpinionJob | null,
    notice: '' as string, // 409 等预检提示（信息条，非错误 toast）
    error: '' as string
  })

  let generation = 0
  const nextGeneration = () => ++generation
  const isStale = (value: number) => isUnmounted() || value !== generation

  // 生成态（generating/job）的所有权令牌：每次 load（含路由换标的触发的那次）
  // 与每次新 generate 都会递增。旧任务失去所有权后：既不能再写状态（onUpdate/
  // 收尾被跳过），也拦不住新页面——评审 P1：A 生成中切到 B，B 页面曾永久
  // 停留在 A 的 loading 与进度上。
  let activeOp = 0

  async function load() {
    const current = nextGeneration()
    activeOp += 1
    state.generating = false
    state.job = null
    state.loading = true
    state.error = ''
    state.notice = ''
    try {
      const response = await api.getOpinionSummary(market(), symbol())
      if (isStale(current)) return
      state.summary = response.data as OpinionSummaryDetail
    } catch (error: unknown) {
      if (isStale(current)) return
      state.summary = null
      const status = (error as { response?: { status?: number } })?.response?.status
      if (status !== 404) state.error = getApiErrorMessage(error, '观点摘要加载失败')
    } finally {
      if (!isStale(current)) state.loading = false
    }
  }

  async function generate() {
    // 所有权判据：本次操作持有令牌才能读写生成态。成功路径末尾的 load() 会
    // 递增令牌（顺带已把 generating/job 复位），收尾自然幂等；路由换标的时
    // watch 触发的 load() 同样递增令牌并清态——旧任务被取消且不会反过来
    // 清掉新页面上后启动的任务。
    const op = ++activeOp
    const owns = () => !isUnmounted() && activeOp === op
    state.generating = true
    state.notice = ''
    state.error = ''
    try {
      const started = await api.startOpinionJob(market(), symbol())
      if (!owns()) return
      const jobId = started.data.id as string
      state.job = started.data as OpinionJob
      const finished = await pollJobUntilDone(() => api.getOpinionJob(jobId), {
        intervalMs: OPINION_POLL_INTERVAL_MS,
        maxAttempts: OPINION_POLL_MAX_ATTEMPTS,
        isCancelled: () => !owns(),
        onUpdate: (job) => {
          if (owns()) state.job = job as OpinionJob
        },
        timeoutMessage: '观点摘要生成超时，请稍后刷新查看',
        failureMessage: '观点摘要生成失败'
      })
      if (!owns() || !finished) return
      await load()
    } catch (error: unknown) {
      if (!owns()) return
      const status = (error as { response?: { status?: number } })?.response?.status
      const message = getApiErrorMessage(error, '观点摘要生成失败')
      if (status === 409) state.notice = message
      else state.error = message
    } finally {
      if (owns()) {
        state.generating = false
        state.job = null
      }
    }
  }

  return { state, load, generate }
}
