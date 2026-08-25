import { useCallback, useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { PersonDetail, ReviewItem } from '../api/types'

const FACTOR_LABELS: Record<string, string> = {
  name: '姓名',
  org: '机构',
  research: '研究方向',
  time: '时间',
  network: '合作网络',
}

/** 审核队列页（AC-9）：两人对比 + 分项得分；合并（保留 A/B）/ 拒绝；操作后刷新。 */
export default function ReviewPage() {
  const [items, setItems] = useState<ReviewItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<number | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = useCallback(() => {
    api
      .get<{ items: ReviewItem[] }>('/api/disambiguation?status=pending')
      .then((r) => {
        setItems(r.items)
        setError(null)
      })
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '队列加载失败'),
      )
  }, [])

  useEffect(() => load(), [load])

  const act = async (item: ReviewItem, action: 'merge' | 'reject', keep?: number) => {
    setBusyId(item.id)
    setActionError(null)
    try {
      if (action === 'merge') {
        await api.post(`/api/disambiguation/${item.id}/merge`, { keep })
      } else {
        await api.post(`/api/disambiguation/${item.id}/reject`)
      }
      load()
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : '操作失败')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="review-page">
      <h2>消歧人工复核（{items?.length ?? 0} 条待审）</h2>
      {error && <p className="error-text">{error}</p>}
      {actionError && <div className="banner error">{actionError}</div>}
      {items && items.length === 0 && (
        <p className="muted">队列为空——相似度 0.5–0.8 的待定组合会出现在这里</p>
      )}
      {items?.map((item) => (
        <ReviewCard
          key={item.id}
          item={item}
          busy={busyId === item.id}
          onMerge={(keep) => act(item, 'merge', keep)}
          onReject={() => act(item, 'reject')}
        />
      ))}
    </div>
  )
}

function ReviewCard({
  item,
  busy,
  onMerge,
  onReject,
}: {
  item: ReviewItem
  busy: boolean
  onMerge: (keep: number) => void
  onReject: () => void
}) {
  const [detailA, setDetailA] = useState<PersonDetail | null>(null)
  const [detailB, setDetailB] = useState<PersonDetail | null>(null)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (!expanded || detailA) return
    api.get<PersonDetail>(`/api/persons/${item.person_a.id}`).then(setDetailA).catch(() => {})
    api.get<PersonDetail>(`/api/persons/${item.person_b.id}`).then(setDetailB).catch(() => {})
  }, [expanded, item, detailA])

  return (
    <div className="review-card">
      <div className="review-head">
        <div className="review-pair">
          <strong>#{item.person_a.id} {item.person_a.name}</strong>
          <span className="muted"> vs </span>
          <strong>#{item.person_b.id} {item.person_b.name}</strong>
        </div>
        <div className="review-score">
          总分 <strong>{item.score.toFixed(2)}</strong>
        </div>
      </div>

      <div className="factor-row">
        {Object.entries(FACTOR_LABELS).map(([key, label]) => {
          const v = item.score_detail[key]
          if (v === undefined) return null
          return (
            <div key={key} className="factor" title={`${label} ${v}`}>
              <span className="small muted">{label}</span>
              <div className="factor-bar">
                <div style={{ width: `${Math.round(v * 100)}%` }} />
              </div>
              <span className="small">{v.toFixed(2)}</span>
            </div>
          )
        })}
      </div>

      <button className="link-btn" onClick={() => setExpanded(!expanded)}>
        {expanded ? '收起对比' : '展开字段对比'}
      </button>

      {expanded && (
        <div className="compare-grid">
          <PersonCompareSide title={`A · ${item.person_a.name}`} detail={detailA} />
          <PersonCompareSide title={`B · ${item.person_b.name}`} detail={detailB} />
        </div>
      )}

      <div className="review-actions">
        <button disabled={busy} onClick={() => onMerge(item.person_a.id)}>
          合并 · 保留 A
        </button>
        <button disabled={busy} onClick={() => onMerge(item.person_b.id)}>
          合并 · 保留 B
        </button>
        <button className="danger" disabled={busy} onClick={onReject}>
          拒绝（非同人）
        </button>
      </div>
    </div>
  )
}

function PersonCompareSide({
  title,
  detail,
}: {
  title: string
  detail: PersonDetail | null
}) {
  return (
    <div className="compare-side">
      <h4>{title}</h4>
      {!detail && <p className="muted small">加载中…</p>}
      {detail && (
        <>
          <p className="small">
            <span className="muted">机构：</span>
            {detail.orgs.map((o) => `${o.name}(${o.confidence.toFixed(1)})`).join('、') || '无'}
          </p>
          <p className="small">
            <span className="muted">研究方向：</span>
            {detail.research_tags.join('、') || '无'}
          </p>
          <p className="small">
            <span className="muted">原始链接：</span>
            {detail.openalex_id ? (
              <a
                href={`https://openalex.org/${detail.openalex_id}`}
                target="_blank"
                rel="noreferrer"
              >
                OpenAlex 作者页 ↗
              </a>
            ) : (
              <a
                href={`https://openalex.org/authors?search=${encodeURIComponent(detail.name)}`}
                target="_blank"
                rel="noreferrer"
              >
                OpenAlex 搜索此人 ↗
              </a>
            )}
          </p>
          <p className="small">
            <span className="muted">论文（{detail.papers.length}）：</span>
          </p>
          <ul className="paper-list">
            {detail.papers.slice(0, 5).map((p) => (
              <li key={p.id}>
                <span className="muted small">{p.year ?? '—'} </span>
                <a
                  href={`https://arxiv.org/abs/${p.arxiv_id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  {p.title}
                </a>
              </li>
            ))}
            {detail.papers.length > 5 && (
              <li className="muted small">… 共 {detail.papers.length} 篇</li>
            )}
          </ul>
        </>
      )}
    </div>
  )
}
