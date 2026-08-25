/** 后端 API 返回类型（T15–T17 契约）。 */

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
}

export interface PersonSearchItem {
  id: number
  name: string
  org: string | null
}

export interface PersonDetail {
  id: number
  name: string
  openalex_id: string | null
  orgs: OrgRef[]
  research_tags: string[]
  papers: {
    id: number
    arxiv_id: string
    title: string
    year: number | null
    directions: string[]
    tracks: string[]
  }[]
}

export interface EvidenceItem {
  paper_id: number
  arxiv_id: string
  title: string
  year: number | null
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
