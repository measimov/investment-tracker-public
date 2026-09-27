/**
 * 观点页「采集器」卡片的数据层：状态（含作者名单、最近运行）+ 管理员操作。
 *
 * 采集本身在独立的 xueqiu-collector 进程里跑；这里只读状态、改作者名单、写「立即运行」
 * 请求。任何写操作成功后整体重取状态——名单与最近运行都在同一个响应里。
 */

import { computed, reactive } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import api from '@/api'
import type { CollectorAuthor, CollectorCube, CollectorStatus } from '@/types'
import { getApiErrorMessage } from '@/utils/apiErrors'
import { showApiError } from '@/utils/showApiError'
import { collectorHealth, isValidCubeId, isValidXueqiuUserId } from './collectorStatus'

export function useCollector() {
  const state = reactive({
    loading: false,
    loadError: '',
    saving: false,
    requesting: false,
    status: null as CollectorStatus | null,
    form: { userId: '', displayName: '' },
    cubeForm: { cubeId: '', displayName: '' }
  })

  const health = computed(() => collectorHealth(state.status))
  const formValid = computed(() => isValidXueqiuUserId(state.form.userId))
  const cubeFormValid = computed(() => isValidCubeId(state.cubeForm.cubeId))

  async function load() {
    state.loading = true
    state.loadError = ''
    try {
      const response = await api.getCollectorStatus()
      state.status = response.data
    } catch (error) {
      state.loadError = getApiErrorMessage(error, '采集器状态加载失败')
    } finally {
      state.loading = false
    }
  }

  async function addAuthor() {
    if (!formValid.value) {
      ElMessage.warning('雪球用户 ID 必须是数字（主页链接 xueqiu.com/u/<ID> 里的那串）')
      return
    }
    state.saving = true
    try {
      await api.createCollectorAuthor({
        xueqiu_user_id: state.form.userId.trim(),
        display_name: state.form.displayName.trim(),
        note: '',
        enabled: true
      })
      ElMessage.success('已加入关注名单，下一轮采集生效')
      state.form.userId = ''
      state.form.displayName = ''
      await load()
    } catch (error) {
      showApiError(error, '添加作者失败')
    } finally {
      state.saving = false
    }
  }

  async function toggleAuthor(author: CollectorAuthor, enabled: boolean) {
    state.saving = true
    try {
      await api.updateCollectorAuthor(author.xueqiu_user_id, { enabled })
      await load()
    } catch (error) {
      showApiError(error, '更新作者失败')
    } finally {
      state.saving = false
    }
  }

  async function removeAuthor(author: CollectorAuthor) {
    try {
      await ElMessageBox.confirm(
        `把「${author.display_name || author.xueqiu_user_id}」移出关注名单？已采集的发言会保留。`,
        '移出关注名单',
        { type: 'warning', confirmButtonText: '移出', cancelButtonText: '取消' }
      )
    } catch {
      return
    }
    state.saving = true
    try {
      await api.deleteCollectorAuthor(author.xueqiu_user_id)
      ElMessage.success('已移出关注名单')
      await load()
    } catch (error) {
      showApiError(error, '移出作者失败')
    } finally {
      state.saving = false
    }
  }

  async function runNow(target: 'authors' | 'symbols' = 'authors') {
    state.requesting = true
    try {
      const response = await api.requestCollectorRun(target)
      state.status = response.data
      ElMessage.success(
        target === 'symbols'
          ? '已请求立即跑一轮按标的采集，采集器将在当前轮次结束后开始'
          : '已请求立即运行，采集器将在 30 秒内开始'
      )
    } catch (error) {
      showApiError(error, '请求立即运行失败')
    } finally {
      state.requesting = false
    }
  }

  async function addCube() {
    if (!cubeFormValid.value) {
      ElMessage.warning('组合代号是两位字母加数字（组合页链接 xueqiu.com/P/<代号>，如 ZH000001）')
      return
    }
    state.saving = true
    try {
      await api.createCollectorCube({
        cube_id: state.cubeForm.cubeId.trim().toUpperCase(),
        display_name: state.cubeForm.displayName.trim(),
        note: '',
        enabled: true
      })
      ElMessage.success('已加入组合跟踪，下一轮按标的采集生效')
      state.cubeForm.cubeId = ''
      state.cubeForm.displayName = ''
      await load()
    } catch (error) {
      showApiError(error, '添加组合失败')
    } finally {
      state.saving = false
    }
  }

  async function toggleCube(cube: CollectorCube, enabled: boolean) {
    state.saving = true
    try {
      await api.updateCollectorCube(cube.cube_id, { enabled })
      await load()
    } catch (error) {
      showApiError(error, '更新组合失败')
    } finally {
      state.saving = false
    }
  }

  async function removeCube(cube: CollectorCube) {
    try {
      await ElMessageBox.confirm(
        `把组合「${cube.display_name || cube.cube_id}」移出跟踪名单？已采集的调仓记录会保留。`,
        '移出组合',
        { type: 'warning', confirmButtonText: '移出', cancelButtonText: '取消' }
      )
    } catch {
      return
    }
    state.saving = true
    try {
      await api.deleteCollectorCube(cube.cube_id)
      ElMessage.success('已移出组合跟踪名单')
      await load()
    } catch (error) {
      showApiError(error, '移出组合失败')
    } finally {
      state.saving = false
    }
  }

  return {
    state,
    health,
    formValid,
    cubeFormValid,
    load,
    addAuthor,
    toggleAuthor,
    removeAuthor,
    runNow,
    addCube,
    toggleCube,
    removeCube
  }
}
