/**
 * 标的录入补丁的行为契约（含 PR #188 评审 P1 回归：选中候选必须完整替换元数据）。
 */
import { describe, expect, it } from 'vitest'
import {
  MARKETS,
  followMarketCurrency,
  freeTextFormPatch,
  inferCurrency,
  normalizeSymbolInput,
  resolvedFormPatch,
  securityFormPatch
} from './securities'
import type { SecuritySearchItem } from '@/types'

function candidate(overrides: Partial<SecuritySearchItem>): SecuritySearchItem {
  return {
    symbol: '00700',
    market: '港股',
    name: null,
    name_en: null,
    name_trad: null,
    pinyin: null,
    currency: null,
    security_type: 'unknown',
    board: null,
    exchange: null,
    list_status: 'unknown',
    in_catalog: false,
    origins: ['watchlist'],
    last_used: null,
    ...overrides
  }
}

describe('MARKETS / inferCurrency', () => {
  it('市场常量与后端 VALID_MARKETS 一致', () => {
    expect([...MARKETS]).toEqual(['A股', 'B股', '港股', '美股', '新加坡股', '加密货币'])
  })

  it('B股按代码段：沪B 美元、深B 港元、其他推不出', () => {
    expect(inferCurrency('B股', '900926')).toBe('USD')
    expect(inferCurrency('B股', '200596')).toBe('HKD')
    expect(inferCurrency('B股', '123456')).toBe('')
    expect(inferCurrency('加密货币', 'BTC')).toBe('')
    expect(inferCurrency('港股', '00700')).toBe('HKD')
    expect(inferCurrency(undefined, '00700')).toBe('')
  })

  it('normalizeSymbolInput 去空白大写', () => {
    expect(normalizeSymbolInput('  aapl ')).toBe('AAPL')
    expect(normalizeSymbolInput(null)).toBe('')
  })
})

describe('securityFormPatch', () => {
  it('watchlist-only 港股候选：无 currency 时按市场推导 HKD，而非沿用表单默认 CNY', () => {
    const patch = securityFormPatch(candidate({ name: '腾讯控股' }))
    expect(patch).toEqual({ symbol: '00700', market: '港股', name: '腾讯控股', currency: 'HKD' })
  })

  it('候选自带 currency 时优先用候选值（目录里 80700 人民币柜台是 CNY）', () => {
    expect(
      securityFormPatch(candidate({ currency: 'USD', market: '美股', symbol: 'PDD' })).currency
    ).toBe('USD')
    expect(securityFormPatch(candidate({ currency: 'CNY', symbol: '80700' })).currency).toBe('CNY')
  })

  it('B股按代码段推断；加密货币清空逼用户显式选择', () => {
    expect(securityFormPatch(candidate({ market: 'B股', symbol: '200596' })).currency).toBe('HKD')
    expect(securityFormPatch(candidate({ market: 'B股', symbol: '900926' })).currency).toBe('USD')
    expect(securityFormPatch(candidate({ market: '加密货币', symbol: 'BTC' })).currency).toBe('')
  })

  it('编辑态换标的：补丁整体覆盖旧标的的 name/currency，无值字段清空', () => {
    const form = {
      symbol: '00700',
      market: '港股',
      name: '腾讯控股',
      currency: 'HKD',
      quantity: 100
    }
    Object.assign(form, securityFormPatch(candidate({ symbol: 'BTC', market: '加密货币' })))
    expect(form).toEqual({
      symbol: 'BTC',
      market: '加密货币',
      name: '',
      currency: '',
      quantity: 100
    })
  })
})

describe('freeTextFormPatch', () => {
  const picked = candidate({ name: '腾讯控股', currency: 'HKD' })

  it('换代码后仍是自动带出的名称被清空，币种按市场重推', () => {
    const current = { symbol: '00700', market: '港股', name: '腾讯控股', currency: 'HKD' }
    expect(freeTextFormPatch(current, { symbol: ' 00005 ', lastPicked: picked })).toEqual({
      symbol: '00005',
      name: '',
      currency: 'HKD'
    })
  })

  it('用户手改过的名称绝不动', () => {
    const current = { symbol: '00700', market: '港股', name: '我的备注名', currency: 'HKD' }
    expect(freeTextFormPatch(current, { symbol: '00005', lastPicked: picked })).toEqual({
      symbol: '00005',
      currency: 'HKD'
    })
  })

  it('从未选过候选：只改代码；推不出币种的市场不碰币种', () => {
    expect(
      freeTextFormPatch(
        { market: '加密货币', name: 'x', currency: 'USDT' },
        { symbol: 'eth', lastPicked: null }
      )
    ).toEqual({
      symbol: 'ETH'
    })
  })
})

describe('resolvedFormPatch', () => {
  const resolved = { symbol: '900926', market: 'B股', name: '宝信B', currency: 'USD' }

  it('名称空才填；币种为市场推断值时用解析值覆盖', () => {
    expect(
      resolvedFormPatch({ symbol: '900926', market: 'B股', name: '', currency: 'USD' }, resolved)
    ).toEqual({
      name: '宝信B'
    })
    expect(
      resolvedFormPatch(
        { symbol: '80700', market: '港股', name: '', currency: 'HKD' },
        {
          symbol: '80700',
          market: '港股',
          name: '腾讯控股-R',
          currency: 'CNY'
        }
      )
    ).toEqual({ name: '腾讯控股-R', currency: 'CNY' })
  })

  it('用户显式选过的币种与手填的名称不动', () => {
    expect(
      resolvedFormPatch(
        { symbol: '900926', market: 'B股', name: '手填', currency: 'HKD' },
        resolved
      )
    ).toEqual({})
  })

  it('币种为空时填入解析值', () => {
    expect(
      resolvedFormPatch({ symbol: '900926', market: 'B股', name: '', currency: '' }, resolved)
    ).toEqual({
      name: '宝信B',
      currency: 'USD'
    })
  })

  it('解析结果不属于当前代码（用户已经改了输入）时整体忽略', () => {
    expect(
      resolvedFormPatch({ symbol: '900927', market: 'B股', name: '', currency: '' }, resolved)
    ).toEqual({})
  })
})

describe('followMarketCurrency', () => {
  it('自动态：按新市场重推；推不出（加密货币 / 未知 B 股段）必须清空而不是保留默认 CNY', () => {
    expect(followMarketCurrency('CNY', '港股', '00700', true)).toBe('HKD')
    expect(followMarketCurrency('CNY', '加密货币', 'BTC', true)).toBe('')
    expect(followMarketCurrency('CNY', 'B股', '123456', true)).toBe('')
    expect(followMarketCurrency('CNY', 'B股', '900926', true)).toBe('USD')
  })

  it('用户手选过币种：不跟随市场', () => {
    expect(followMarketCurrency('USD', '港股', '00700', false)).toBe('USD')
    expect(followMarketCurrency('', '加密货币', 'BTC', false)).toBe('')
  })
})
