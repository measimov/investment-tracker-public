import { beforeEach, describe, expect, it, vi } from 'vitest'

const getLatestRates = vi.fn()
vi.mock('../api', () => ({ default: { getLatestRates: () => getLatestRates() } }))
vi.mock('element-plus', () => ({ ElMessage: { warning: vi.fn() } }))

import { ElMessage } from 'element-plus'
import { useExchangeRates } from './useExchangeRates'

describe('useExchangeRates', () => {
  beforeEach(() => {
    useExchangeRates().exchangeRates.value = {}
    useExchangeRates().loadFailed.value = false
    vi.mocked(ElMessage.warning).mockClear()
  })

  it('缺汇率返回 null，不静默返回原币值（#219）', () => {
    const { convertToCNY, convertToUSD, hasRate } = useExchangeRates()
    expect(convertToCNY(100, 'HKD')).toBeNull()
    expect(convertToUSD(100)).toBeNull()
    expect(hasRate('HKD')).toBe(false)
    expect(convertToCNY(100, 'CNY')).toBe(100)
    expect(convertToCNY(100, null)).toBe(100)
  })

  it('模块级单例：一个实例加载，另一个实例换算', async () => {
    getLatestRates.mockResolvedValue({ data: { rates: { HKD: '0.92', USD: 7.1 } } })
    await useExchangeRates().loadExchangeRates()
    const other = useExchangeRates()
    expect(other.convertToCNY(100, 'HKD')).toBeCloseTo(92)
    expect(other.convertToUSD(71)).toBeCloseTo(10)
  })

  it('加载失败只提示一次', async () => {
    getLatestRates.mockRejectedValue(new Error('down'))
    vi.spyOn(console, 'error').mockImplementation(() => {})
    await useExchangeRates().loadExchangeRates()
    await useExchangeRates().loadExchangeRates()
    expect(ElMessage.warning).toHaveBeenCalledTimes(1)
    expect(useExchangeRates().loadFailed.value).toBe(true)
  })
})
