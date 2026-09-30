// @vitest-environment jsdom
// LLM 输出经 v-html 渲染，消毒是唯一防线：这里钉住 DOMPurify 真的在 marked 之后生效（#285）
import { describe, expect, it } from 'vitest'
import { renderMarkdown } from './markdown'

describe('renderMarkdown', () => {
  it('渲染常规 Markdown', () => {
    const html = renderMarkdown('## 结论\n\n- **估值**偏低\n- 第二行')
    expect(html).toContain('<h2>结论</h2>')
    expect(html).toContain('<strong>估值</strong>')
    expect(html).toContain('<li>')
  })

  it('空输入返回空串', () => {
    expect(renderMarkdown(null)).toBe('')
    expect(renderMarkdown(undefined)).toBe('')
    expect(renderMarkdown('')).toBe('')
  })

  it('剥掉事件属性与脚本', () => {
    const html = renderMarkdown('<img src="x" onerror="alert(1)">\n\n<script>alert(2)</script>')
    expect(html).not.toMatch(/onerror/i)
    expect(html).not.toMatch(/<script/i)
    expect(html).not.toContain('alert(2)')
  })

  it('剥掉 javascript: 链接', () => {
    const md = renderMarkdown('[点我](javascript:alert(1))')
    const raw = renderMarkdown('<a href="javascript:alert(1)">点我</a>')
    expect(md).not.toMatch(/javascript:/i)
    expect(raw).not.toMatch(/javascript:/i)
  })

  it('保留普通 http 链接', () => {
    expect(renderMarkdown('[公告](https://example.com/a)')).toContain(
      'href="https://example.com/a"'
    )
  })
})
