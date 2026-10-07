/**
 * 更新雪球 Cookie：格式粗判、提交前检查、摘要文案；以及 composable 的「提交后必清空、
 * 从不回显」契约。全部用合成 Cookie。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api', () => ({
  default: {
    getXueqiuCookieStatus: vi.fn(),
    updateXueqiuCookie: vi.fn()
  }
}))

import api from '@/api'
import type { XueqiuCookieAdminStatus, XueqiuCookieUpdateResponse } from '@/types'
import {
  MAX_COOKIE_CONTENT_BYTES,
  cookieContentError,
  cookieFormatHint,
  cookieFormatLabel,
  cookieKeysText,
  cookieLevelLabel,
  cookieSourceLabel,
  detectCookieFormat,
  isAcceptedCookieFile,
  primaryExpirySummary,
  primaryFactText
} from './cookieUpdate'
import { useCookieUpdate } from './useCookieUpdate'

const mocked = api as unknown as {
  getXueqiuCookieStatus: ReturnType<typeof vi.fn>
  updateXueqiuCookie: ReturnType<typeof vi.fn>
}

const SECRET = 'SYNTH_A_TOKEN_7f3c9e0d'
const HEADER = `xq_a_token=${SECRET}; xqat=SYNTH_XQAT`

function status(overrides: Partial<XueqiuCookieAdminStatus> = {}): XueqiuCookieAdminStatus {
  return {
    source: 'file',
    file_path: '/app/secrets/xueqiu.com.json',
    file_exists: true,
    file_mtime: '2026-09-27T07:00:00Z',
    backup_exists: false,
    backup_mtime: null,
    writable: true,
    writable_reason: null,
    level: 'normal',
    message: 'ok',
    keys: ['xq_a_token', 'xqat'],
    primary: [
      { name: 'xq_a_token', present: true, expires_at: null, days_left: 12.34 },
      { name: 'xqat', present: true, expires_at: null, days_left: 12.34 }
    ],
    read_error: null,
    ...overrides
  }
}

function updateResponse(): XueqiuCookieUpdateResponse {
  return {
    status: status(),
    backup_created: true,
    source_format: 'header',
    notes: [],
    probe: { ok: true, detail: '探活成功' }
  }
}

afterEach(() => vi.clearAllMocks())

describe('cookieUpdate helpers', () => {
  it('粗判格式并给出提示', () => {
    expect(detectCookieFormat('')).toBe('empty')
    expect(detectCookieFormat('  \n')).toBe('empty')
    expect(detectCookieFormat('﻿{"cookies": []}')).toBe('json')
    expect(detectCookieFormat(' [ ]')).toBe('json')
    expect(detectCookieFormat(HEADER)).toBe('header')
    expect(cookieFormatHint('header')).toContain('探活')
    expect(cookieFormatHint('json')).toContain('到期时间')
    expect(cookieFormatHint('empty')).toBe('')
  })

  it('提交前检查：空与过大，文案不回显内容', () => {
    expect(cookieContentError('')).toContain('请粘贴')
    expect(cookieContentError(HEADER)).toBeNull()
    const huge = SECRET + 'x'.repeat(MAX_COOKIE_CONTENT_BYTES)
    const error = cookieContentError(huge)
    expect(error).toContain('过大')
    expect(error).not.toContain(SECRET)
    // 按 UTF-8 字节计：中文 3 字节
    expect(cookieContentError('中'.repeat(MAX_COOKIE_CONTENT_BYTES / 3 + 1))).toContain('过大')
  })

  it('文件类型与标签', () => {
    expect(isAcceptedCookieFile('xueqiu.com.json')).toBe(true)
    expect(isAcceptedCookieFile('cookie.TXT')).toBe(true)
    expect(isAcceptedCookieFile('cookie.png')).toBe(false)
    expect(isAcceptedCookieFile(undefined)).toBe(false)
    expect(cookieSourceLabel('file')).toContain('XUEQIU_COOKIE_FILE')
    expect(cookieSourceLabel('inline')).toContain('XUEQIU_COOKIES')
    expect(cookieSourceLabel('none')).toBe('未配置')
    expect(cookieFormatLabel('j2team')).toContain('J2Team')
    expect(cookieFormatLabel(null)).toBe('—')
    expect(cookieLevelLabel('critical')).toBe('失效')
    expect(cookieLevelLabel(undefined)).toBe('未知')
  })

  it('主凭证与名称摘要', () => {
    expect(primaryFactText({ name: 'xqat', present: false })).toBe('xqat 缺失')
    expect(primaryFactText({ name: 'xqat', present: true, days_left: null })).toBe(
      'xqat 无到期时间'
    )
    expect(primaryFactText({ name: 'xqat', present: true, days_left: -1.25 })).toBe(
      'xqat 已过期 1.3 天'
    )
    expect(primaryExpirySummary(status().primary)).toBe('xq_a_token 剩 12.3 天 · xqat 剩 12.3 天')
    expect(primaryExpirySummary([])).toBe('—')
    expect(cookieKeysText(['a', 'b'])).toBe('a、b')
    expect(cookieKeysText(['a', 'b', 'c'], 2)).toBe('a、b 等 3 个')
    expect(cookieKeysText(null)).toBe('—')
  })
})

describe('useCookieUpdate', () => {
  it('打开即加载状态；不可写时不能提交', async () => {
    mocked.getXueqiuCookieStatus.mockResolvedValue({
      data: status({ writable: false, writable_reason: '不可写' })
    })
    const dialog = useCookieUpdate()
    dialog.open()
    await vi.waitFor(() => expect(dialog.state.status).not.toBeNull())
    dialog.state.text = HEADER
    expect(dialog.canSubmit.value).toBe(false)
  })

  it('提交粘贴内容：成功后清空输入、回调刷新、结果不含 Cookie 值', async () => {
    mocked.getXueqiuCookieStatus.mockResolvedValue({ data: status() })
    mocked.updateXueqiuCookie.mockResolvedValue({ data: updateResponse() })
    const onUpdated = vi.fn()
    const dialog = useCookieUpdate(onUpdated)
    dialog.open()
    await vi.waitFor(() => expect(dialog.state.status).not.toBeNull())
    dialog.state.text = HEADER
    dialog.state.probe = true
    expect(dialog.canSubmit.value).toBe(true)
    await dialog.submit()

    expect(mocked.updateXueqiuCookie).toHaveBeenCalledWith({ content: HEADER, probe: true })
    expect(dialog.state.text).toBe('')
    expect(onUpdated).toHaveBeenCalledOnce()
    expect(JSON.stringify(dialog.state)).not.toContain(SECRET)
  })

  it('失败也清空输入并就地显示后端文案', async () => {
    mocked.getXueqiuCookieStatus.mockResolvedValue({ data: status() })
    mocked.updateXueqiuCookie.mockRejectedValue({
      response: { status: 422, data: { detail: '缺少雪球登录凭证：xqat' } }
    })
    const onUpdated = vi.fn()
    const dialog = useCookieUpdate(onUpdated)
    dialog.open()
    await vi.waitFor(() => expect(dialog.state.status).not.toBeNull())
    dialog.state.text = HEADER
    await dialog.submit()

    expect(dialog.state.submitError).toContain('缺少雪球登录凭证')
    expect(dialog.state.text).toBe('')
    expect(onUpdated).not.toHaveBeenCalled()
    expect(JSON.stringify(dialog.state)).not.toContain(SECRET)
  })

  it('选择文件：文本不进响应式状态，提交文件内容后清空', async () => {
    mocked.getXueqiuCookieStatus.mockResolvedValue({ data: status() })
    mocked.updateXueqiuCookie.mockResolvedValue({ data: updateResponse() })
    const dialog = useCookieUpdate()
    dialog.open()
    await vi.waitFor(() => expect(dialog.state.status).not.toBeNull())
    const content = JSON.stringify({ cookies: [{ name: 'xq_a_token', value: SECRET }] })

    dialog.state.text = 'leftover'
    await dialog.selectFile(new File([content], 'xueqiu.com.json'))
    expect(dialog.state.fileName).toBe('xueqiu.com.json')
    expect(dialog.state.text).toBe('')
    expect(JSON.stringify(dialog.state)).not.toContain(SECRET)
    expect(dialog.canSubmit.value).toBe(true)

    await dialog.submit()
    expect(mocked.updateXueqiuCookie).toHaveBeenCalledWith({ content, probe: false })
    expect(dialog.state.fileName).toBe('')
  })

  it('拒绝非 JSON/TXT 文件与空文件；关闭时清空', async () => {
    mocked.getXueqiuCookieStatus.mockResolvedValue({ data: status() })
    const dialog = useCookieUpdate()
    dialog.open()
    await dialog.selectFile(new File(['x'], 'a.png'))
    expect(dialog.state.submitError).toContain('.json')
    expect(dialog.state.fileName).toBe('')
    await dialog.selectFile(new File([''], 'empty.json'))
    expect(dialog.state.submitError).toContain('请粘贴')

    dialog.state.text = HEADER
    dialog.close()
    expect(dialog.state.text).toBe('')
    expect(dialog.state.visible).toBe(false)
  })

  // PR #256 评审 P1：新选择不合格时不得残留上一份文件；慢读的旧文件不得覆盖新选择
  async function openDialog() {
    mocked.getXueqiuCookieStatus.mockResolvedValue({ data: status() })
    mocked.updateXueqiuCookie.mockResolvedValue({ data: updateResponse() })
    const dialog = useCookieUpdate()
    dialog.open()
    await vi.waitFor(() => expect(dialog.state.status).not.toBeNull())
    return dialog
  }

  function fileWithText(name: string, text: Promise<string> | string): File {
    const file = new File(['placeholder'], name)
    Object.defineProperty(file, 'text', { value: () => Promise.resolve(text) })
    return file
  }

  it.each([
    ['扩展名不支持', () => new File(['x'], 'b.png'), '.json'],
    ['空文件', () => new File([''], 'b.json'), '请粘贴'],
    [
      '过大',
      () => {
        const file = new File(['x'], 'b.json')
        Object.defineProperty(file, 'size', { value: MAX_COOKIE_CONTENT_BYTES + 1 })
        return file
      },
      '过大'
    ]
  ])('合格的 A 之后选了不合格的 B（%s）：不能提交，显示 B 的错误', async (_label, makeB, msg) => {
    const dialog = await openDialog()
    const contentA = JSON.stringify({ cookies: [{ name: 'xq_a_token', value: SECRET }] })
    await dialog.selectFile(new File([contentA], 'a.json'))
    expect(dialog.state.fileName).toBe('a.json')
    expect(dialog.canSubmit.value).toBe(true)

    await dialog.selectFile(makeB())
    expect(dialog.state.fileName).toBe('')
    expect(dialog.state.submitError).toContain(msg)
    expect(dialog.canSubmit.value).toBe(false)
    await dialog.submit()
    expect(mocked.updateXueqiuCookie).not.toHaveBeenCalled()
  })

  it('慢读的 A 之后快读的 B：只提交 B', async () => {
    const dialog = await openDialog()
    let resolveA!: (text: string) => void
    const slowA = new Promise<string>((resolve) => {
      resolveA = resolve
    })
    const contentA = JSON.stringify({ cookies: [{ name: 'xq_a_token', value: 'SYNTH_OLD_A' }] })
    const contentB = JSON.stringify({ cookies: [{ name: 'xq_a_token', value: 'SYNTH_NEW_B' }] })

    const pendingA = dialog.selectFile(fileWithText('a.json', slowA))
    await dialog.selectFile(fileWithText('b.json', contentB))
    expect(dialog.state.fileName).toBe('b.json')
    resolveA(contentA) // A 在 B 之后才读完
    await pendingA
    expect(dialog.state.fileName).toBe('b.json')

    await dialog.submit()
    expect(mocked.updateXueqiuCookie).toHaveBeenCalledTimes(1)
    expect(mocked.updateXueqiuCookie).toHaveBeenCalledWith({ content: contentB, probe: false })
  })

  it('粘贴旧 Cookie 后改选文件：文件读完前不得提交旧内容', async () => {
    const dialog = await openDialog()
    dialog.state.text = HEADER
    let resolveFile!: (text: string) => void
    const reading = new Promise<string>((resolve) => {
      resolveFile = resolve
    })
    const content = JSON.stringify({
      cookies: [{ name: 'xq_a_token', value: 'SYNTH_NEW_FILE_TOKEN' }]
    })

    const pending = dialog.selectFile(fileWithText('new.json', reading))
    expect(dialog.canSubmit.value).toBe(false)
    await dialog.submit()
    expect(mocked.updateXueqiuCookie).not.toHaveBeenCalled()

    resolveFile(content)
    await pending
    expect(dialog.canSubmit.value).toBe(true)
    await dialog.submit()
    expect(mocked.updateXueqiuCookie).toHaveBeenCalledTimes(1)
    expect(mocked.updateXueqiuCookie).toHaveBeenCalledWith({ content, probe: false })
  })

  it('读文件期间关闭对话框：读完也不写回', async () => {
    const dialog = await openDialog()
    let resolveA!: (text: string) => void
    const slowA = new Promise<string>((resolve) => {
      resolveA = resolve
    })
    const pendingA = dialog.selectFile(fileWithText('a.json', slowA))
    dialog.close()
    resolveA(JSON.stringify({ cookies: [{ name: 'xq_a_token', value: SECRET }] }))
    await pendingA
    expect(dialog.state.fileName).toBe('')
    expect(dialog.canSubmit.value).toBe(false)
  })
})
