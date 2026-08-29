import { useCallback, useEffect, useRef, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { AdminEditItem, AdminMetrics, BatchStatusDto } from '../api/types'

const STAGE_LABELS: Record<string, string> = {
  init: '初始化',
  collect: '采集',
  filter: 'AI 过滤',
  tag: '方向打标',
  extract: 'GLM 抽取',
  openalex: '机构补全',
  disambiguate: '实体消歧',
  link: '建立关系',
  done: '完成',
  error: '失败',
}

/** M2.5 FR-6：操作日志的动作显示名 */
const EDIT_ACTION_LABELS: Record<string, string> = {
  update_person: '编辑学者字段',
  set_orgs: '设置机构归属',
  set_research_tags: '设置研究方向',
  delete_person: '删除学者（合规）',
  delete_relationship: '删除关系',
  adjust_strength: '调整强度',
}

/** 管理后台（US-5，AC-7）：立即更新+进度轮询、token 用量、熔断状态+放行、失败任务。 */
export default function AdminPage() {
  const [metrics, setMetrics] = useState<AdminMetrics | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [batch, setBatch] = useState<BatchStatusDto | null>(null)
  const [triggerMsg, setTriggerMsg] = useState<string | null>(null)
  const [edits, setEdits] = useState<AdminEditItem[] | null>(null)
  const pollRef = useRef<ReturnType<typeof setInterval>>()

  const loadEdits = useCallback(() => {
    api
      .get<{ total: number; items: AdminEditItem[] }>('/api/admin/edits?limit=20')
      .then((r) => setEdits(r.items))
      .catch(() => setEdits([]))
  }, [])

  const loadMetrics = useCallback(() => {
    api
      .get<AdminMetrics>('/api/admin/metrics')
      .then((m) => {
        setMetrics(m)
        setError(null)
      })
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '指标加载失败'),
      )
  }, [])

  useEffect(() => {
    loadMetrics()
    loadEdits()
    const t = setInterval(loadMetrics, 30_000)
    return () => clearInterval(t)
  }, [loadMetrics, loadEdits])

  useEffect(() => () => clearInterval(pollRef.current), [])

  const triggerUpdate = async () => {
    setTriggerMsg(null)
    try {
      const { batch_id } = await api.post<{ batch_id: string }>('/api/admin/trigger-update')
      setTriggerMsg(`批次 ${batch_id} 已启动`)
      clearInterval(pollRef.current)
      pollRef.current = setInterval(async () => {
        try {
          const b = await api.get<BatchStatusDto>(`/api/admin/update-status/${batch_id}`)
          setBatch(b)
          if (!b.running) {
            clearInterval(pollRef.current)
            loadMetrics()
          }
        } catch {
          clearInterval(pollRef.current)
        }
      }, 1000)
    } catch (e) {
      setTriggerMsg(e instanceof ApiError ? e.message : '触发失败')
    }
  }

  const resumeBreaker = async () => {
    try {
      await api.post('/api/admin/breaker/resume')
      loadMetrics()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '放行失败')
    }
  }

  return (
    <div className="admin-page">
      <h2>管理后台</h2>
      {error && <div className="banner error">{error}</div>}

      <section className="admin-card">
        <h3>数据更新</h3>
        <div className="admin-row">
          <button onClick={triggerUpdate} disabled={batch?.running}>
            {batch?.running ? '采集中…' : '立即更新'}
          </button>
          {triggerMsg && <span className="muted small">{triggerMsg}</span>}
        </div>
        {batch && (
          <div className="batch-progress">
            <p>
              批次 <strong>{batch.batch_id}</strong> · 阶段：
              <strong>{STAGE_LABELS[batch.stage] ?? batch.stage}</strong>
              {batch.counts.collect && (
                <span className="muted small">
                  （新增 {batch.counts.collect.added ?? 0} 篇 / 跳过{' '}
                  {batch.counts.collect.skipped ?? 0} / 失败分类{' '}
                  {batch.counts.collect.failed_categories ?? 0}）
                </span>
              )}
            </p>
            {batch.error && <p className="error-text">{batch.error}</p>}
          </div>
        )}
      </section>

      <section className="admin-card">
        <h3>操作日志（M2.5，最近 20 条）</h3>
        {edits === null && <p className="muted small">加载中…</p>}
        {edits !== null && edits.length === 0 && (
          <p className="muted small">暂无手动编辑记录</p>
        )}
        {edits !== null && edits.length > 0 && (
          <table className="rel-table">
            <thead>
              <tr>
                <th>时间</th>
                <th>操作</th>
                <th>实体</th>
                <th>原因</th>
              </tr>
            </thead>
            <tbody>
              {edits.map((e) => (
                <tr key={e.id}>
                  <td className="small">
                    {new Date(e.created_at).toLocaleString()}
                  </td>
                  <td>{EDIT_ACTION_LABELS[e.action] ?? e.action}</td>
                  <td className="small">
                    {e.entity_type === 'person' ? '学者' : '关系'} #{e.entity_id}
                  </td>
                  <td className="small" title={fmtSnapshot(e)}>
                    {e.reason}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      {metrics && (
        <>
          <section className="admin-card">
            <h3>GLM Token 用量</h3>
            <UsageBar
              label="今日"
              used={metrics.token_usage.daily_used}
              budget={metrics.token_usage.daily_budget}
            />
            <UsageBar
              label="本周"
              used={metrics.token_usage.weekly_used}
              budget={metrics.token_usage.weekly_budget}
            />
          </section>

          <section className="admin-card">
            <h3>熔断状态</h3>
            <div className={`breaker-level ${metrics.breaker.level}`}>
              {breakerText(metrics.breaker.level)}
              {metrics.breaker.manual_override_until && (
                <span className="small">
                  （已手动放行至 {new Date(metrics.breaker.manual_override_until).toLocaleString()}）
                </span>
              )}
            </div>
            {metrics.breaker.level !== 'ok' && !metrics.breaker.manual_override_until && (
              <button onClick={resumeBreaker}>放行继续（当日有效）</button>
            )}
          </section>

          <section className="admin-card">
            <h3>失败任务（{metrics.failed_jobs.length}）</h3>
            {metrics.failed_jobs.length === 0 && (
              <p className="muted small">无失败任务</p>
            )}
            <table className="rel-table">
              <thead>
                <tr>
                  <th>类型</th>
                  <th>目标</th>
                  <th>状态</th>
                  <th>已尝试</th>
                  <th>下次重试</th>
                </tr>
              </thead>
              <tbody>
                {metrics.failed_jobs.map((j) => (
                  <tr key={j.id} title={j.error}>
                    <td>{j.job_type}</td>
                    <td className="small">{j.target}</td>
                    <td>
                      <span className={`job-status ${j.status}`}>{statusText(j.status)}</span>
                    </td>
                    <td>{j.attempt}/3</td>
                    <td className="small">
                      {j.next_retry_at
                        ? new Date(j.next_retry_at).toLocaleTimeString()
                        : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </>
      )}
    </div>
  )
}

function UsageBar({ label, used, budget }: { label: string; used: number; budget: number }) {
  const pct = Math.min(100, Math.round((used / budget) * 100))
  return (
    <div className="usage-bar">
      <div className="usage-head">
        <span>{label}</span>
        <span className="small muted">
          {(used / 10_000).toFixed(1)} 万 / {(budget / 10_000).toFixed(0)} 万（{pct}%）
        </span>
      </div>
      <div className="usage-track">
        <div
          className={pct >= 100 ? 'full' : pct >= 80 ? 'warn' : ''}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function breakerText(level: string): string {
  switch (level) {
    case 'ok':
      return '正常'
    case 'warn':
      return '告警：日用量已达 80%'
    case 'daily_stop':
      return '日熔断：细筛已停（待定论文放行，抽取继续）'
    case 'weekly_stop':
      return '周熔断：全部 GLM 调用已停'
    default:
      return level
  }
}

function statusText(status: string): string {
  return status === 'retrying' ? '重试中' : status === 'dead' ? '死信' : status
}

/** 悬浮展示审计行的前后快照（NFR-4 配对核对） */
function fmtSnapshot(e: AdminEditItem): string {
  const fmt = (v: Record<string, unknown> | null) =>
    v === null ? '—' : JSON.stringify(v)
  return `修改前: ${fmt(e.before)}\n修改后: ${fmt(e.after)}`
}
