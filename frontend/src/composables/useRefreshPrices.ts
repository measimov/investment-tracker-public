import { ElMessage } from 'element-plus'
import api from '../api'
import { useHoldingsStore } from '../stores/holdings'
import { pollJobUntilDone } from '../utils/polling'

/** 价格刷新 job 的结果（此前 Holdings/Statistics 各写一份 interface） */
export interface PriceRefreshResult {
  success_count: number
  skipped_count: number
  failed_count: number
  failed_list?: Array<{ symbol: string; market: string; error?: string }>
  success_list?: Array<{ symbol: string; market: string; price?: number; source?: string }>
}

/**
 * 价格刷新编排（issue #139）。
 *
 * 此前 Holdings 与 Statistics 各自定义几乎相同的 pollPriceRefreshJob、各自
 * 拼"成功 X/跳过 Y/失败 Z"消息，且已分叉：Statistics 版有 isCancelled 保护、
 * Holdings 版没接——组件卸载后轮询不会停。收敛为一处，统一带卸载保护。
 */
export function useRefreshPrices(isUnmounted: () => boolean) {
  const holdingsStore = useHoldingsStore()

  /** 提交刷新 job 并轮询到终态；组件卸载或超时返回 null。 */
  async function refreshPrices(): Promise<PriceRefreshResult | null> {
    const response = await holdingsStore.refreshAllPrices()
    const job = await pollJobUntilDone(() => api.getPriceRefreshJob(response.data.id), {
      isCancelled: isUnmounted,
      timeoutMessage: '刷新仍在后台运行，请稍后重新查看持仓价格',
      failureMessage: '后台刷新失败'
    })
    return (job?.result ?? null) as PriceRefreshResult | null
  }

  /** 汇总文案（一处拼装；调用方可加后缀如"并完成计算"） */
  function refreshSummaryText(result: PriceRefreshResult): string {
    let message = `成功更新 ${result.success_count} 只股票`
    if (result.skipped_count > 0) message += `，跳过 ${result.skipped_count} 只（最近已更新）`
    if (result.failed_count > 0) message += `，${result.failed_count} 只更新失败`
    return message
  }

  /** 按结果分级弹消息：全成绿 / 部分成黄 / 全败红。 */
  function notifyRefreshResult(result: PriceRefreshResult, suffix = '') {
    const message = refreshSummaryText(result) + suffix
    if (result.failed_count === 0) ElMessage.success(message)
    else if (result.success_count > 0) ElMessage.warning(message)
    else ElMessage.error('刷新失败，请稍后重试')
    logRefreshDetails(result)
  }

  /** 失败/成功明细只进 DEV 控制台（单处维护，不再两页各写一版） */
  function logRefreshDetails(result: PriceRefreshResult) {
    if (!import.meta.env.DEV) return
    if (result.failed_list?.length) {
      console.group('📊 刷新失败详情')
      result.failed_list.forEach((item) => {
        console.error(`${item.symbol} (${item.market}): ${item.error}`)
      })
      console.groupEnd()
    }
    if (result.success_list?.length) {
      console.group('📈 刷新成功详情')
      result.success_list.forEach((item) => {
        console.log(`${item.symbol} (${item.market}): ${item.price} [${item.source}]`)
      })
      console.groupEnd()
    }
  }

  return { refreshPrices, refreshSummaryText, notifyRefreshResult }
}
