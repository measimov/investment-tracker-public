// Axios 错误归一化（#142：此前完全无测试）。全站的中文错误文案都经这里，
// 消息取值的优先级链是契约：userMessage > detail 字符串 > detail 数组拼接 >
// detail 对象 > error.message > fallback。
import { describe, expect, test } from 'vitest'
import type { AxiosError } from 'axios'
import { getApiErrorMessage, isApiError, normalizeApiError } from './apiErrors'

function axiosError(overrides: Partial<AxiosError> = {}): AxiosError {
  return Object.assign(new Error('Request failed'), {
    isAxiosError: true,
    toJSON: () => ({}),
    name: 'AxiosError',
    ...overrides
  }) as AxiosError
}

describe('getApiErrorMessage', () => {
  test('userMessage wins over everything else', () => {
    const err = Object.assign(new Error('raw'), {
      userMessage: '已归一化的消息',
      response: { data: { detail: '后端 detail' } }
    })
    expect(getApiErrorMessage(err)).toBe('已归一化的消息')
  })

  test('string detail from the backend is used verbatim', () => {
    const err = { response: { data: { detail: '账户不存在' } } }
    expect(getApiErrorMessage(err)).toBe('账户不存在')
  })

  test('FastAPI validation detail arrays join with a Chinese semicolon', () => {
    const err = {
      response: { data: { detail: [{ msg: '缺少 symbol' }, { message: '缺少 market' }] } }
    }
    expect(getApiErrorMessage(err)).toBe('缺少 symbol；缺少 market')
  })

  test('object detail falls back to message/error keys', () => {
    expect(getApiErrorMessage({ response: { data: { detail: { message: '对象消息' } } } })).toBe(
      '对象消息'
    )
  })

  test('falls back to error.message, then to the provided fallback', () => {
    expect(getApiErrorMessage(new Error('boom'))).toBe('boom')
    expect(getApiErrorMessage(null)).toBe('请求失败，请稍后重试')
    expect(getApiErrorMessage({}, '自定义兜底')).toBe('自定义兜底')
  })
})

describe('normalizeApiError', () => {
  test('timeout and network failures get dedicated Chinese messages', () => {
    expect(normalizeApiError(axiosError({ code: 'ECONNABORTED' })).userMessage).toBe(
      '请求超时，请稍后重试'
    )
    expect(normalizeApiError(axiosError()).userMessage).toBe('网络连接失败，请检查网络')
  })

  test('status-specific fallbacks apply when the backend sends no detail', () => {
    const forbidden = normalizeApiError(
      axiosError({ response: { status: 403, data: {} } } as Partial<AxiosError>)
    )
    expect(forbidden.userMessage).toBe('没有权限执行此操作')
    const maintenance = normalizeApiError(
      axiosError({ response: { status: 503, data: {} } } as Partial<AxiosError>)
    )
    expect(maintenance.userMessage).toBe('服务暂时不可用，请稍后重试')
    const serverError = normalizeApiError(
      axiosError({ response: { status: 500, data: {} } } as Partial<AxiosError>)
    )
    expect(serverError.userMessage).toBe('服务器错误，请稍后重试')
  })

  test('backend detail wins over the status fallback', () => {
    const err = normalizeApiError(
      axiosError({ response: { status: 403, data: { detail: '只读账户' } } } as Partial<AxiosError>)
    )
    expect(err.userMessage).toBe('只读账户')
  })

  test('produces a real Error recognized by isApiError, keeping context', () => {
    const original = axiosError({ code: 'ECONNABORTED' })
    const normalized = normalizeApiError(original)
    expect(normalized).toBeInstanceOf(Error)
    expect(isApiError(normalized)).toBe(true)
    expect(normalized.originalError).toBe(original)
    expect(isApiError(new Error('plain'))).toBe(false)
  })
})
