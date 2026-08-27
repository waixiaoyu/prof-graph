import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import { relTypeLabel } from '../api/types'
import type {
  PersonDetail,
  RelationshipEvidenceDetail,
} from '../api/types'

/** 右侧详情面板：节点 → 人物详情（机构/研究方向/合作伙伴/论文）；边 → 混合证据链。 */
export function PersonDetailPanel({
  personId,
  onClose,
  onOpenRelationship,
}: {
  personId: number
  onClose: () => void
  /** 点击某位合作伙伴 → 打开两人关系的证据链 */
  onOpenRelationship: (
    relationshipId: number,
    label: string,
    summary: string | null,
  ) => void
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
          <h3>
            {detail.name}
            {detail.title && (
              <span className="person-title muted small"> {detail.title}</span>
            )}
          </h3>
          {detail.homepage && (
            <p className="small">
              <a href={detail.homepage} target="_blank" rel="noreferrer">
                个人主页 ↗
              </a>
            </p>
          )}
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
            <h4>合作关系（{detail.partners.length}）</h4>
            {detail.partners.length === 0 && <p className="muted">暂无合作关系</p>}
            <ul className="partner-list">
              {detail.partners.map((p) => (
                <li key={p.relationship_id}>
                  <button
                    onClick={() =>
                      onOpenRelationship(
                        p.relationship_id,
                        relTypeLabel(p.type, p.subtype || undefined),
                        p.summary,
                      )
                    }
                    title="查看两人关系证据链"
                  >
                    <span className="partner-name">{p.name}</span>
                    {p.org && <span className="muted small"> {p.org}</span>}
                    <span className="muted small">
                      {' '}
                      {p.type === 'paper_cooperation'
                        ? `合作 ${p.coop_count} 次`
                        : relTypeLabel(p.type, p.subtype || undefined)}{' '}
                      · 强度 {p.strength.toFixed(2)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
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

/** 日期时间截短展示：2026-08-27T05:00:00+00:00 → 2026-08-27 */
function shortDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '—'
}

export function EvidencePanel({
  relationshipId,
  label,
  summary,
  onClose,
}: {
  relationshipId: number
  /** 关系类型显示名（学术传承含子类型），如"学术传承（导师-学生）" */
  label: string
  summary: string | null
  onClose: () => void
}) {
  const [detail, setDetail] = useState<RelationshipEvidenceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setDetail(null)
    setError(null)
    api
      .get<RelationshipEvidenceDetail>(`/api/relationships/${relationshipId}/evidence`)
      .then(setDetail)
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '加载失败'),
      )
  }, [relationshipId])

  const papers = detail?.papers ?? []
  const pages = detail?.web_pages ?? []
  const news = detail?.news_items ?? []
  const empty = detail && papers.length + pages.length + news.length === 0

  return (
    <aside className="detail-panel">
      <button className="panel-close" onClick={onClose}>
        ×
      </button>
      <h3>关系证据链</h3>
      <p className="muted">
        {label}
        {detail && ` · 强度 ${detail.strength.toFixed(2)}`}
      </p>
      {summary && <p className="muted small">{summary}</p>}
      {!detail && !error && <p className="muted">加载中…</p>}
      {error && <p className="error-text">{error}</p>}
      {empty && <p className="muted">暂无证据</p>}
      {detail && papers.length > 0 && (
        <section>
          <h4>论文（{papers.length}）</h4>
          <ul className="paper-list">
            {papers.map((p) => (
              <li key={p.paper_id}>
                <span className="muted small">{p.year ?? '—'}</span>{' '}
                <a href={p.url} target="_blank" rel="noreferrer">
                  {p.title}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
      {detail && pages.length > 0 && (
        <section>
          <h4>网页快照（{pages.length}）</h4>
          <ul className="paper-list">
            {pages.map((w) => (
              <li key={w.web_page_id}>
                <span className="muted small">{shortDate(w.fetched_at)} 快照</span>{' '}
                <a href={w.url} target="_blank" rel="noreferrer">
                  {w.title || w.url}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
      {detail && news.length > 0 && (
        <section>
          <h4>资讯（{news.length}）</h4>
          <ul className="paper-list">
            {news.map((n) => (
              <li key={n.news_item_id}>
                <span className="muted small">
                  {shortDate(n.published_at)} {n.source}
                </span>{' '}
                <a href={n.url} target="_blank" rel="noreferrer">
                  {n.title}
                </a>
              </li>
            ))}
          </ul>
        </section>
      )}
    </aside>
  )
}
