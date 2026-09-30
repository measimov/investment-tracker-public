import { reactive, ref, type Ref } from 'vue'
import { showApiError } from '@/utils/showApiError'

export interface PageResult<T> {
  items: T[]
  total: number
}

/**
 * 服务端分页列表的公共壳（#284：交易页与公司行动页各一套，细节已分叉）。
 *
 * - 页码回退：删除/筛选后当前页超出总页数时回到最后一页并重载（此前只有交易页做了，
 *   公司行动删掉最后一页唯一一条后停在空页）；
 * - 请求序号守卫：快速翻页/改筛选时旧请求晚到不得覆盖新结果（两边此前都没有）；
 * - 失败走 showApiError（全局通知已弹过的不重复弹）。
 *
 * 筛选条件留在调用方：fetchPage 自己读筛选并拼参数。
 */
export function usePagedList<T>(options: {
  fetchPage: (
    page: { skip: number; limit: number },
    request: { force: boolean }
  ) => Promise<PageResult<T>>
  failureMessage: string
  pageSize?: number
}) {
  const loading = ref(false)
  const items = ref([]) as Ref<T[]>
  const pagination = reactive({ page: 1, pageSize: options.pageSize ?? 50, total: 0 })
  let requestSeq = 0

  async function load(request: { force?: boolean } = {}): Promise<void> {
    const seq = ++requestSeq
    const force = request.force === true
    loading.value = true
    try {
      const result = await options.fetchPage(
        { skip: (pagination.page - 1) * pagination.pageSize, limit: pagination.pageSize },
        { force }
      )
      if (seq !== requestSeq) return // 已有更新的请求：丢弃晚到的旧结果
      items.value = result.items
      pagination.total = result.total
      const lastPage = Math.max(1, Math.ceil(result.total / pagination.pageSize))
      if (pagination.page > lastPage) {
        pagination.page = lastPage
        await load({ force })
      }
    } catch (error) {
      if (seq === requestSeq) showApiError(error, options.failureMessage)
    } finally {
      if (seq === requestSeq) loading.value = false
    }
  }

  /** 条件变了：回第一页并强制重取 */
  function search() {
    pagination.page = 1
    return load({ force: true })
  }

  function changePageSize() {
    pagination.page = 1
    return load()
  }

  return { loading, items, pagination, load, search, changePageSize }
}
