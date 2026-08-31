/** 后端 API 返回类型（T15–T17 契约；M2-T13 增关系类型/混合证据）。 */

/** 关系类型固定三项（RD-M2-13）+ 学术传承四子类型的显示名 */
export const REL_TYPE_NAMES: Record<string, string> = {
  paper_cooperation: '论文合作',
  academic_mentorship: '学术传承',
  project_cooperation: '项目合作',
}

export const SUBTYPE_NAMES: Record<string, string> = {
  mentor_student: '导师-学生',
  same_lab: '同实验室',
  same_advisor: '同导师',
  same_cohort: '同届',
}

export const ALL_REL_TYPES = [
  'paper_cooperation',
  'academic_mentorship',
  'project_cooperation',
] as const

/** "学术传承（导师-学生）"；论文/项目合作为 "论文合作" */
export function relTypeLabel(type: string, subtype?: string): string {
  const base = REL_TYPE_NAMES[type] ?? type
  return subtype ? `${base}（${SUBTYPE_NAMES[subtype] ?? subtype}）` : base
}

export interface OrgRef {
  name: string
  confidence: number
}

export interface GraphNode {
  id: number
  name: string
  orgs: OrgRef[]
  directions: string[]
  tracks: string[]
  paper_count: number
}

export interface GraphEdge {
  id: number
  source: number
  target: number
  type: string
  subtype: string
  strength: number
  coop_count: number
  time_start: string | null
  time_end: string | null
  evidence_summary: string | null
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  /** include_potential=true 时才有（M3，FR-5.1；默认关不返回字段） */
  potential_edges?: PotentialEdge[]
}

export interface FilterOptions {
  directions: { id: string; name: string }[]
  tracks: { id: string; name: string }[]
  orgs: string[]
  relationship_types: { id: string; name: string }[]
}

export interface PersonSearchItem {
  id: number
  name: string
  org: string | null
}

// ---------- M3 潜在关系 ----------

/** 发现方法显示名（common_network / research_similarity） */
export const POTENTIAL_METHOD_NAMES: Record<string, string> = {
  common_network: '共同合作者',
  research_similarity: '研究方向相似',
}

/** 单一发现方法载荷（potential_edges 与 potential_connections 共用） */
export interface PotentialMethodItem {
  method: string
  confidence: number
  reason: string | null
  signals: Record<string, unknown> | null
}

/** /api/graph potential_edges：按 pair 聚合（RD-8） */
export interface PotentialEdge {
  a: number
  b: number
  methods: PotentialMethodItem[]
}

export interface PartnerItem {
  relationship_id: number
  person_id: number
  name: string
  org: string | null
  type: string
  subtype: string
  coop_count: number
  strength: number
  summary: string | null
}

export interface PersonDetail {
  id: number
  name: string
  openalex_id: string | null
  title: string | null
  homepage: string | null
  orgs: OrgRef[]
  research_tags: string[]
  partners: PartnerItem[]
  /** 潜在连接 top10（M3，FR-5.3）：对端最高 confidence 降序 */
  potential_connections: {
    person_id: number
    name: string | null
    orgs: OrgRef[]
    methods: PotentialMethodItem[]
  }[]
  papers: {
    id: number
    arxiv_id: string
    title: string
    year: number | null
    directions: string[]
    tracks: string[]
  }[]
}

export interface EvidencePaper {
  paper_id: number
  arxiv_id: string
  title: string
  url: string
  year: number | null
}

export interface EvidenceWebPage {
  web_page_id: number
  title: string | null
  url: string
  page_type: string
  fetched_at: string | null
}

export interface EvidenceNews {
  news_item_id: number
  title: string
  url: string
  source: string
  published_at: string | null
}

/** /api/relationships/{id}/evidence 混合证据（FR-7.3） */
export interface RelationshipEvidenceDetail {
  relationship_id: number
  type: string
  subtype: string
  strength: number
  evidence_summary: string | null
  papers: EvidencePaper[]
  web_pages: EvidenceWebPage[]
  news_items: EvidenceNews[]
}

export interface ReviewItem {
  id: number
  person_a: { id: number; name: string }
  person_b: { id: number; name: string }
  score: number
  score_detail: Record<string, number>
  created_at: string | null
}

export interface AdminMetrics {
  token_usage: {
    daily_used: number
    daily_budget: number
    weekly_used: number
    weekly_budget: number
  }
  breaker: {
    level: 'ok' | 'warn' | 'daily_stop' | 'weekly_stop'
    manual_override_until: string | null
  }
  failed_jobs: {
    id: number
    job_type: string
    target: string
    attempt: number
    status: string
    next_retry_at: string | null
    error: string
  }[]
}

export interface BatchStatusDto {
  batch_id: string
  started_at: string
  stage: string
  running: boolean
  error: string | null
  counts: Record<string, Record<string, number>>
}

// ---------- M2.5 手动编辑 ----------

/** GET /api/admin/orgs 候选（FR-2：只能选已有机构 RD-8） */
export interface OrgOption {
  id: number
  name: string
  level: string | null
}

/** GET /api/admin/persons/{id}/edit-view 编辑表单数据源（管理面含 email/机构 id） */
export interface PersonEditView {
  id: number
  name: string
  title: string | null
  homepage: string | null
  email: string | null
  deleted: boolean
  merged: boolean
  orgs: { id: number; name: string }[]
  research_tags: string[]
}

/** GET /api/admin/edits 操作日志行（FR-6） */
export interface AdminEditItem {
  id: number
  action: string
  entity_type: string
  entity_id: number
  before: Record<string, unknown> | null
  after: Record<string, unknown> | null
  reason: string | null
  created_at: string
}
