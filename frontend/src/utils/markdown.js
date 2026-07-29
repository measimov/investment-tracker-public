// Markdown 渲染（AI 复盘报告与追问回答）：marked 解析 + DOMPurify 消毒后才可 v-html。
import DOMPurify from 'dompurify'
import { marked } from 'marked'

marked.setOptions({ gfm: true, breaks: true })

export function renderMarkdown(text) {
  if (!text) return ''
  return DOMPurify.sanitize(marked.parse(text))
}
