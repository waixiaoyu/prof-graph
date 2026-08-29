import { useEffect, useState } from 'react'
import { api, ApiError } from '../api/client'
import { relTypeLabel } from '../api/types'
import type {
  OrgOption,
  PersonDetail,
  PersonEditView,
  RelationshipEvidenceDetail,
} from '../api/types'

/** 右侧详情面板：节点 → 人物详情（机构/研究方向/合作伙伴/论文，M2.5 可编辑）；
 * 边 → 混合证据链 + 关系删除/强度调整。 */
export function PersonDetailPanel({
  personId,
  onClose,
  onOpenRelationship,
  onDataChanged,
}: {
  personId: number
  onClose: () => void
  /** 点击某位合作伙伴 → 打开两人关系的证据链 */
  onOpenRelationship: (
    relationshipId: number,
    label: string,
    summary: string | null,
  ) => void
  /** 编辑/删除落库后通知图谱层刷新（改名/删人会改变图数据） */
  onDataChanged?: () => void
}) {
  const [detail, setDetail] = useState<PersonDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [editing, setEditing] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    setDetail(null)
    setError(null)
    setEditing(false)
    api
      .get<PersonDetail>(`/api/persons/${personId}`)
      .then(setDetail)
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '加载失败'),
      )
  }, [personId, reloadKey])

  return (
    <aside className="detail-panel">
      <button className="panel-close" onClick={onClose}>
        ×
      </button>
      {!detail && !error && <p className="muted">加载中…</p>}
      {error && <p className="error-text">{error}</p>}
      {detail && !editing && (
        <>
          <h3>
            {detail.name}
            {detail.title && (
              <span className="person-title muted small"> {detail.title}</span>
            )}
            <button
              className="edit-toggle"
              onClick={() => setEditing(true)}
              title="手动编辑（M2.5）"
            >
              编辑
            </button>
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
      {detail && editing && (
        <PersonEditForm
          personId={detail.id}
          displayName={detail.name}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            setReloadKey((k) => k + 1)
            onDataChanged?.()
          }}
          onDeleted={() => {
            onDataChanged?.()
            onClose()
          }}
        />
      )}
    </aside>
  )
}

/** 日期时间截短展示：2026-08-27T05:00:00+00:00 → 2026-08-27 */
function shortDate(iso: string | null): string {
  return iso ? iso.slice(0, 10) : '—'
}

/** 编辑表单（M2.5 FR-1/2/3/5）：字段 + 机构多选（仅已有机构）+ 标签替换；底部合规删除。
 * 数据源为管理端 edit-view（图谱端 API 不出 email，隐私口径）。 */
function PersonEditForm({
  personId,
  displayName,
  onCancel,
  onSaved,
  onDeleted,
}: {
  personId: number
  displayName: string
  onCancel: () => void
  onSaved: () => void
  onDeleted: () => void
}) {
  const [view, setView] = useState<PersonEditView | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [title, setTitle] = useState('')
  const [homepage, setHomepage] = useState('')
  const [email, setEmail] = useState('')
  const [tags, setTags] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // 机构多选：已选 chips + 关键字搜候选（FR-2 只能选已有机构 RD-8）
  const [selectedOrgs, setSelectedOrgs] = useState<OrgOption[]>([])
  const [orgQuery, setOrgQuery] = useState('')
  const [candidates, setCandidates] = useState<OrgOption[]>([])

  useEffect(() => {
    api
      .get<PersonEditView>(`/api/admin/persons/${personId}/edit-view`)
      .then((v) => {
        setView(v)
        setName(v.name)
        setTitle(v.title ?? '')
        setHomepage(v.homepage ?? '')
        setEmail(v.email ?? '')
        setTags(v.research_tags.join('，'))
        setSelectedOrgs(v.orgs.map((o) => ({ id: o.id, name: o.name, level: null })))
      })
      .catch((e: unknown) =>
        setLoadError(e instanceof ApiError ? e.message : '加载失败'),
      )
  }, [personId])

  useEffect(() => {
    const t = setTimeout(() => {
      api
        .get<{ orgs: OrgOption[] }>(
          `/api/admin/orgs?q=${encodeURIComponent(orgQuery)}`,
        )
        .then((r) => setCandidates(r.orgs))
        .catch(() => setCandidates([]))
    }, 250)
    return () => clearTimeout(t)
  }, [orgQuery])

  const splitTags = (raw: string): string[] =>
    raw
      .split(/[,，;；]/)
      .map((t) => t.trim())
      .filter(Boolean)

  const save = async () => {
    if (!reason.trim()) {
      setError('必须填写修改原因（审计要求）')
      return
    }
    setBusy(true)
    setError(null)
    try {
      const base = `/api/admin/persons/${personId}`
      // 只提交变化的字段（空串 = 清空；name 不可清空，undefined 被 JSON 丢弃）
      const fields: Record<string, string> = {}
      if (view) {
        if (name.trim() !== view.name && name.trim()) fields.name = name.trim()
        if (title.trim() !== (view.title ?? '')) fields.title = title.trim()
        if (homepage.trim() !== (view.homepage ?? '')) fields.homepage = homepage.trim()
        if (email.trim() !== (view.email ?? '')) fields.email = email.trim()
      }
      if (Object.keys(fields).length > 0) {
        await api.patch(base, { ...fields, reason: reason.trim() })
      }
      const orgIds = selectedOrgs.map((o) => o.id)
      const oldOrgIds = view ? view.orgs.map((o) => o.id) : []
      const orgsChanged =
        orgIds.length !== oldOrgIds.length ||
        [...orgIds].sort().join(',') !== [...oldOrgIds].sort().join(',')
      if (orgsChanged) {
        await api.put(`${base}/orgs`, { org_ids: orgIds, reason: reason.trim() })
      }
      const newTags = splitTags(tags)
      const tagsChanged =
        view !== null &&
        (newTags.length !== view.research_tags.length ||
          newTags.join('\u0000') !== [...view.research_tags].join('\u0000'))
      if (tagsChanged) {
        await api.put(`${base}/research-tags`, {
          tags: newTags,
          reason: reason.trim(),
        })
      }
      if (Object.keys(fields).length === 0 && !orgsChanged && !tagsChanged) {
        setError('没有检测到修改')
        return
      }
      onSaved()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '保存失败')
    } finally {
      setBusy(false)
    }
  }

  const [delReason, setDelReason] = useState('')
  const deletePerson = async () => {
    if (!delReason.trim()) {
      setError('删除必须填写原因（合规级联不可逆）')
      return
    }
    if (
      !window.confirm(
        `确认删除「${displayName}」？\n` +
          '将级联：全部关系墓碑化、证据物理删除、论文署名断开、待审合并取消。\n' +
          '论文与机构保留，操作不可逆。',
      )
    ) {
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.del(`/api/admin/persons/${personId}`, {
        reason: delReason.trim(),
      })
      onDeleted()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : '删除失败')
    } finally {
      setBusy(false)
    }
  }

  if (loadError) {
    return (
      <div className="edit-form">
        <h3>编辑学者</h3>
        <p className="error-text">{loadError}</p>
        <button onClick={onCancel}>返回</button>
      </div>
    )
  }
  if (!view) {
    return (
      <div className="edit-form">
        <h3>编辑学者</h3>
        <p className="muted">加载中…</p>
      </div>
    )
  }

  return (
    <div className="edit-form">
      <h3>编辑学者</h3>
      <label>
        姓名
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label>
        职位/职称
        <input value={title} onChange={(e) => setTitle(e.target.value)} />
      </label>
      <label>
        个人主页
        <input value={homepage} onChange={(e) => setHomepage(e.target.value)} />
      </label>
      <label>
        邮箱
        <input value={email} onChange={(e) => setEmail(e.target.value)} />
      </label>
      <label>
        研究方向（逗号分隔，保存时整组替换）
        <input value={tags} onChange={(e) => setTags(e.target.value)} />
      </label>

      <div className="org-picker">
        <span className="label-text">机构归属（只能选择已有机构）</span>
        <div className="tag-row">
          {selectedOrgs.map((o) => (
            <span key={o.id} className="tag removable">
              {o.name}
              <button
                onClick={() =>
                  setSelectedOrgs((s) => s.filter((x) => x.id !== o.id))
                }
                title="移除"
              >
                ×
              </button>
            </span>
          ))}
          {selectedOrgs.length === 0 && <span className="muted small">未选择</span>}
        </div>
        <input
          className="org-search"
          placeholder="搜索机构名…"
          value={orgQuery}
          onChange={(e) => setOrgQuery(e.target.value)}
        />
        <ul className="search-results static">
          {candidates
            .filter((c) => !selectedOrgs.some((s) => s.id === c.id))
            .slice(0, 8)
            .map((c) => (
              <li key={c.id}>
                <button onClick={() => setSelectedOrgs((s) => [...s, c])}>
                  + {c.name}
                  {c.level && <span className="muted small">（{c.level}）</span>}
                </button>
              </li>
            ))}
        </ul>
      </div>

      <label>
        修改原因（必填，记入操作日志）
        <input value={reason} onChange={(e) => setReason(e.target.value)} />
      </label>
      {error && <p className="error-text">{error}</p>}
      <div className="edit-actions">
        <button onClick={save} disabled={busy}>
          保存
        </button>
        <button onClick={onCancel} disabled={busy}>
          取消
        </button>
      </div>

      <div className="danger-zone">
        <span className="label-text">删除此人（合规，不可逆）</span>
        <input
          placeholder="删除原因（必填）"
          value={delReason}
          onChange={(e) => setDelReason(e.target.value)}
        />
        <button className="danger" onClick={deletePerson} disabled={busy}>
          删除
        </button>
      </div>
    </div>
  )
}

export function EvidencePanel({
  relationshipId,
  label,
  summary,
  onClose,
  onDataChanged,
}: {
  relationshipId: number
  /** 关系类型显示名（学术传承含子类型），如"学术传承（导师-学生）" */
  label: string
  summary: string | null
  onClose: () => void
  /** 删除/调整强度落库后通知图谱层刷新 */
  onDataChanged?: () => void
}) {
  const [detail, setDetail] = useState<RelationshipEvidenceDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    setDetail(null)
    setError(null)
    api
      .get<RelationshipEvidenceDetail>(`/api/relationships/${relationshipId}/evidence`)
      .then(setDetail)
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '加载失败'),
      )
  }, [relationshipId, reloadKey])

  const [reason, setReason] = useState('')
  const [strength, setStrength] = useState('')
  const [busy, setBusy] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const deleteRelationship = async () => {
    if (!reason.trim()) {
      setActionError('删除关系必须填写原因')
      return
    }
    if (!window.confirm('确认删除该关系？管线重跑不会自动恢复（墓碑）。')) return
    setBusy(true)
    setActionError(null)
    try {
      await api.del(`/api/admin/relationships/${relationshipId}`, {
        reason: reason.trim(),
      })
      onDataChanged?.()
      onClose()
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : '删除失败')
    } finally {
      setBusy(false)
    }
  }

  const adjustStrength = async () => {
    const v = Number(strength)
    if (strength === '' || Number.isNaN(v) || v < 0 || v > 1) {
      setActionError('强度需在 0 到 1 之间')
      return
    }
    if (!reason.trim()) {
      setActionError('调整强度必须填写原因')
      return
    }
    setBusy(true)
    setActionError(null)
    try {
      await api.patch(`/api/admin/relationships/${relationshipId}`, {
        strength: v,
        reason: reason.trim(),
      })
      setStrength('')
      setReloadKey((k) => k + 1)
      onDataChanged?.()
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : '调整失败')
    } finally {
      setBusy(false)
    }
  }

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

      <div className="rel-manage">
        <span className="label-text">管理操作（M2.5）</span>
        <label>
          原因（必填）
          <input value={reason} onChange={(e) => setReason(e.target.value)} />
        </label>
        <div className="edit-actions">
          <input
            className="strength-input"
            type="number"
            min={0}
            max={1}
            step={0.05}
            placeholder="强度 0-1"
            value={strength}
            onChange={(e) => setStrength(e.target.value)}
          />
          <button onClick={adjustStrength} disabled={busy}>
            调整强度
          </button>
          <button className="danger" onClick={deleteRelationship} disabled={busy}>
            删除关系
          </button>
        </div>
        {actionError && <p className="error-text">{actionError}</p>}
      </div>

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
