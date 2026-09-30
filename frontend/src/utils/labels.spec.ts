import { describe, expect, it } from 'vitest'
import {
  DELETED_ACCOUNT_LABEL,
  UNASSIGNED_ACCOUNT_LABEL,
  accountLabel,
  accountOptionLabel,
  accountShortName,
  maskAccountNumber,
  maskInlineIds
} from './labels'

const accounts = [
  { id: 1, account_name: '招商主账户', broker: '招商证券', account_number_masked: '****1234' },
  { id: 2, account_name: '', broker: 'IBKR', account_number_masked: 'U***68' },
  { id: 3, account_name: null, broker: null, account_number_masked: null }
]

describe('account labels（#284：全站一份）', () => {
  it('表格用简称，空 id 为未指定账户，找不到为已删除账户', () => {
    expect(accountLabel(accounts, 1)).toBe('招商主账户')
    expect(accountLabel(accounts, '1')).toBe('招商主账户')
    expect(accountLabel(accounts, null)).toBe(UNASSIGNED_ACCOUNT_LABEL)
    expect(accountLabel(accounts, '')).toBe(UNASSIGNED_ACCOUNT_LABEL)
    expect(accountLabel(accounts, 99)).toBe(DELETED_ACCOUNT_LABEL)
  })

  it('没有名称退回「券商 尾号」，再退回未命名账户', () => {
    expect(accountShortName(accounts[1])).toBe('IBKR U***68')
    expect(accountShortName(accounts[2])).toBe('未命名账户')
    expect(accountShortName(null)).toBe('未命名账户')
  })

  it('选项与 full 用全称「名称 · 券商 · 尾号」', () => {
    expect(accountOptionLabel(accounts[0])).toBe('招商主账户 · 招商证券 · ****1234')
    expect(accountLabel(accounts, 2, { full: true })).toBe('IBKR · U***68')
    expect(accountOptionLabel(accounts[2])).toBe('未命名账户')
  })
})

describe('账号脱敏（#286）', () => {
  it('账户名里夹带的账号打码，短词与已打码的不动', () => {
    expect(maskInlineIds('IBKR U19667968')).toBe('IBKR U***7968')
    expect(maskInlineIds('HSBC 港股')).toBe('HSBC 港股')
    expect(maskInlineIds('U***68')).toBe('U***68')
  })

  it('多个股东代码只显示第一个与数量', () => {
    expect(maskAccountNumber('0123456789 A123456789 0987654321')).toBe('***6789 等 3 个')
    expect(maskAccountNumber('U***68')).toBe('U***68')
    expect(maskAccountNumber('')).toBe('')
  })

  it('选项全称使用脱敏后的名称与账号', () => {
    expect(
      accountOptionLabel({
        account_name: 'IBKR U19667968',
        broker: 'IBKR',
        account_number_masked: 'U***67968'
      })
    ).toBe('IBKR U***7968 · IBKR · U***67968')
  })
})
