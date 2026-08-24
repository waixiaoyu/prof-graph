import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { EvidenceItem, PersonDetail } from '../api/types'

/** 右侧详情面板：节点 → 人物详情（机构/研究方向/论文）；边 → 证据链。 */
export function PersonDetailPanel({
  personId,
  onClose,
}: {
  personId: number
  onClose: () => void
}) {
  const [detail, setDetail] = useState<PersonDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setDetail(null)
    setError(null)
    api
      .get<PersonDetail>(`/api/persons/${personId}`)
      .then(setDetail)
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '加载失败'),
      )
  }, [personId])

  return (
    <aside className="detail-panel">
      <button className="panel-close" onClick={onClose}>
        ×
      </button>
      {!detail && !error && <p className="muted">加载中…</p>}
      {error && <p className="error-text">{error}</p>}
      {detail && (
        <>
          <h3>{detail.name}</h3>
          {detail.openalex_id && (
            <p className="muted small">OpenAlex: {detail.openalex_id}</p>
          )}
          <section>
            <h4>机构归属</h4>
            {detail.orgs.length === 0 && <p className="muted">无（置信度 0.4 兜底）</p>}
            <ul>
              {detail.orgs.map((o) => (
                <li key={o.name}>
                  {o.name}
                  <span className="muted small">（置信度 {o.confidence.toFixed(1)}）</span>
                </li>
              ))}
            </ul>
          </section>
          <section>
            <h4>研究方向</h4>
            <div className="tag-row">
              {detail.research_tags.map((t) => (
                <span key={t} className="tag">
                  {t}
                </span>
              ))}
            </div>
          </section>
          <section>
            <h4>论文（{detail.papers.length}）</h4>
            <ul className="paper-list">
              {detail.papers.map((p) => (
                <li key={p.id}>
                  <span className="muted small">{p.year ?? '—'}</span>{' '}
                  <a
                    href={`https://arxiv.org/abs/${p.arxiv_id}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {p.title}
                  </a>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </aside>
  )
}

export function EvidencePanel({
  relationshipId,
  summary,
  onClose,
}: {
  relationshipId: number
  summary: string | null
  onClose: () => void
}) {
  const [items, setItems] = useState<EvidenceItem[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setItems(null)
    setError(null)
    api
      .get<{ items: EvidenceItem[] }>(`/api/relationships/${relationshipId}/evidence`)
      .then((r) => setItems(r.items))
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '加载失败'),
      )
  }, [relationshipId])

  return (
    <aside className="detail-panel">
      <button className="panel-close" onClick={onClose}>
        ×
      </button>
      <h3>合作证据链</h3>
      {summary && <p className="muted">{summary}</p>}
      {!items && !error && <p className="muted">加载中…</p>}
      {error && <p className="error-text">{error}</p>}
      {items && (
        <>
          {items.length === 0 && <p className="muted">暂无证据论文</p>}
          <ul className="paper-list">
            {items.map((p) => (
              <li key={p.paper_id}>
                <span className="muted small">{p.year ?? '—'}</span>{' '}
                <a
                  href={`https://arxiv.org/abs/${p.arxiv_id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  {p.title}
                </a>
              </li>
            ))}
          </ul>
        </>
      )}
    </aside>
  )
}
