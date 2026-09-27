import { describe, expect, it } from 'vitest'

import type { BrokerAccount } from '@/types'
import { importAccountChoice } from './importAccounts'

const account = (id: number, broker: string, isActive = true) =>
  ({ id, broker, account_name: `账户${id}`, is_active: isActive }) as BrokerAccount

describe('importAccountChoice', () => {
  it('非券商导入模式返回 null', () => {
    expect(importAccountChoice([account(1, '招商证券')], 'standard')).toBeNull()
  })

  it('券商名的常见写法都能匹配', () => {
    const accounts = [
      account(1, '招商证券'),
      account(2, '招行'),
      account(3, '盈透证券'),
      account(4, 'Interactive Brokers'),
      account(5, '东财'),
      account(6, '东方财富证券')
    ]
    expect(importAccountChoice(accounts, 'cmb')?.options.map((a) => a.id)).toEqual([1, 2])
    expect(importAccountChoice(accounts, 'ibkr')?.options.map((a) => a.id)).toEqual([3, 4])
    expect(importAccountChoice(accounts, 'eastmoney')?.options.map((a) => a.id)).toEqual([5, 6])
  })

  it('停用账户不进候选，并说明原因', () => {
    const choice = importAccountChoice([account(1, '招商证券', false)], 'cmb')
    expect(choice?.options).toEqual([])
    expect(choice?.emptyReason).toContain('已停用')
  })

  it('没有匹配账户时说明原因', () => {
    const choice = importAccountChoice([account(1, '汇丰香港')], 'ibkr')
    expect(choice?.options).toEqual([])
    expect(choice?.emptyReason).toContain('还没有券商为「IBKR」的账户')
  })

  it('有候选时没有原因', () => {
    expect(importAccountChoice([account(1, 'IBKR')], 'ibkr')?.emptyReason).toBeNull()
  })
})
