/**
 * 「更新雪球 Cookie」对话框的数据层（仅管理员）。
 *
 * Cookie 是登录凭证：粘贴框内容与所选文件的文本只在提交时发给后端，提交后（无论成败）
 * 与关闭对话框时一律清空；文件文本不进响应式状态、从不渲染。后端也只回名称与到期事实，
 * 所以对话框里任何时候都看不到 Cookie 值。
 */

import { computed, reactive } from 'vue'
import api from '@/api'
import type { XueqiuCookieAdminStatus, XueqiuCookieUpdateResponse } from '@/types'
import { getApiErrorMessage } from '@/utils/apiErrors'
import {
  COOKIE_TOO_LARGE_MESSAGE,
  MAX_COOKIE_CONTENT_BYTES,
  cookieContentError,
  cookieFormatHint,
  detectCookieFormat,
  isAcceptedCookieFile
} from './cookieUpdate'

export function useCookieUpdate(onUpdated?: () => void) {
  const state = reactive({
    visible: false,
    loading: false,
    submitting: false,
    loadError: '',
    submitError: '',
    status: null as XueqiuCookieAdminStatus | null,
    text: '',
    fileName: '',
    probe: false,
    result: null as XueqiuCookieUpdateResponse | null
  })
  // 文件文本刻意不放进 reactive：不被 devtools/模板意外展示，只在提交时读取一次
  let fileContent = ''
  // 选择序号：每次选择/清空都 +1，异步读文件回来时序号已变就丢弃结果——否则先选的大文件
  // 读得慢，会在后选的文件之后写回，提交的就不是管理员眼前的那份（PR #256 评审 P1）
  let selection = 0

  function dropFile() {
    selection += 1
    state.fileName = ''
    fileContent = ''
  }

  const formatHint = computed(() =>
    state.fileName ? '' : cookieFormatHint(detectCookieFormat(state.text))
  )
  const canSubmit = computed(
    () =>
      Boolean(state.status?.writable) &&
      !state.submitting &&
      (state.fileName ? true : cookieContentError(state.text) === null)
  )

  function clearInput() {
    state.text = ''
    dropFile()
  }

  async function loadStatus() {
    state.loading = true
    state.loadError = ''
    try {
      const response = await api.getXueqiuCookieStatus()
      state.status = response.data
    } catch (error) {
      state.loadError = getApiErrorMessage(error, 'Cookie 状态加载失败')
    } finally {
      state.loading = false
    }
  }

  function open() {
    clearInput()
    state.visible = true
    state.submitError = ''
    state.result = null
    state.probe = false
    void loadStatus()
  }

  function close() {
    clearInput()
    state.visible = false
  }

  async function selectFile(file: File | null | undefined) {
    // 新的选择一开始就作废上一份文件：B 不合格时不能留着 A 还显示「已选择 A」、还能提交 A
    dropFile()
    const current = selection
    state.submitError = ''
    if (!file) return
    if (!isAcceptedCookieFile(file.name)) {
      state.submitError = '请选择浏览器插件导出的 .json 文件（或存成 .txt 的请求头）'
      return
    }
    if (file.size > MAX_COOKIE_CONTENT_BYTES) {
      state.submitError = COOKIE_TOO_LARGE_MESSAGE // 不必读进内存再判
      return
    }
    let text: string
    try {
      text = await file.text()
    } catch {
      if (current === selection) state.submitError = '读取文件失败，请重新选择'
      return
    }
    if (current !== selection) return // 期间又选了别的文件（或清空/关闭）：丢弃
    const error = cookieContentError(text)
    if (error) {
      state.submitError = error
      return
    }
    fileContent = text
    state.fileName = file.name
    state.text = '' // 文件与粘贴二选一，避免误以为两者都会提交
  }

  function removeFile() {
    dropFile()
  }

  async function submit() {
    const content = state.fileName ? fileContent : state.text
    const localError = cookieContentError(content)
    if (localError) {
      state.submitError = localError
      return
    }
    state.submitting = true
    state.submitError = ''
    state.result = null
    try {
      const response = await api.updateXueqiuCookie({ content, probe: state.probe })
      state.result = response.data
      state.status = response.data.status
      onUpdated?.()
    } catch (error) {
      state.submitError = getApiErrorMessage(error, '更新 Cookie 失败')
    } finally {
      clearInput()
      state.submitting = false
    }
  }

  return {
    state,
    formatHint,
    canSubmit,
    open,
    close,
    loadStatus,
    selectFile,
    removeFile,
    submit
  }
}
