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
