import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { ALL_REL_TYPES, REL_TYPE_NAMES } from '../api/types'
import type { FilterOptions, PersonSearchItem } from '../api/types'

export interface GraphFilters {
  direction: string
  track: string
  /** 关系类型多选（默认三项全开；RD-M2-13 类型筛选器） */
  relTypes: string[]
  strengthMin: number
  /** 合作次数下限（0 = 不限；隐藏"合作 1 次"这类弱关系） */
  coopMin: number
  /** 机构切入（机构名，来自 /filters/options 的 orgs） */
  org: string
  /** 图谱规模：返回的关系条数上限（边越多节点越多） */
  limit: number
}

/** 规模三档（2026-08-31 降噪）：默认精简，完整档含性能提示 */
export const SCALE_OPTIONS = [
  { value: 150, label: '精简' },
  { value: 400, label: '标准' },
  { value: 1000, label: '完整' },
] as const

/** 合作次数档位：与强度滑杆互补，用户视角更直观 */
export const COOP_OPTIONS = [
  { value: 0, label: '合作不限' },
  { value: 2, label: '合作 ≥2 次' },
  { value: 3, label: '合作 ≥3 次' },
  { value: 5, label: '合作 ≥5 次' },
] as const

export const DEFAULT_FILTERS: GraphFilters = {
  direction: '',
  track: '',
  relTypes: [...ALL_REL_TYPES],
  strengthMin: 0,
  coopMin: 0,
  org: '',
  limit: 150,
}

/** 顶部筛选栏（FR-5.2/5.4/5.6）：方向/赛道/机构下拉、姓名搜索、合作次数、强度滑杆、规模三档、视图切换。 */
export function FilterBar({
  filters,
  onFiltersChange,
  onLocatePerson,
  view,
  onViewChange,
}: {
  filters: GraphFilters
  onFiltersChange: (next: GraphFilters) => void
  onLocatePerson: (personId: number) => void
  view: 'graph' | 'list'
  onViewChange: (v: 'graph' | 'list') => void
}) {
  const [options, setOptions] = useState<FilterOptions | null>(null)
  const [searchType, setSearchType] = useState<'name' | 'org'>('name')
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<PersonSearchItem[]>([])
  const [searching, setSearching] = useState(false)
  const debounceRef = useRef<ReturnType<typeof setTimeout>>()

  useEffect(() => {
    api
      .get<FilterOptions>('/api/filters/options')
      .then(setOptions)
      .catch(() => setOptions(null))
  }, [])

  // 防抖搜索（300ms）
  useEffect(() => {
    clearTimeout(debounceRef.current)
    if (query.trim().length < 2) {
      setResults([])
      setSearching(false)
      return
    }
    setSearching(true)
    debounceRef.current = setTimeout(() => {
      api
        .get<{ items: PersonSearchItem[] }>(
          `/api/persons/search?q=${encodeURIComponent(query.trim())}&type=${searchType}`,
        )
        .then((r) => setResults(r.items))
        .catch(() => setResults([]))
        .finally(() => setSearching(false))
    }, 300)
    return () => clearTimeout(debounceRef.current)
  }, [query, searchType])

  return (
    <div className="filter-bar">
      <select
        value={filters.direction}
        onChange={(e) => onFiltersChange({ ...filters, direction: e.target.value })}
      >
        <option value="">全部业务方向</option>
        {options?.directions.map((d) => (
          <option key={d.id} value={d.id}>
            {d.name}
          </option>
        ))}
      </select>

      <select
        value={filters.track}
        onChange={(e) => onFiltersChange({ ...filters, track: e.target.value })}
      >
        <option value="">全部学术赛道</option>
        {options?.tracks.map((t) => (
          <option key={t.id} value={t.id}>
            {t.name}
          </option>
        ))}
      </select>

      <select
        className="org-select"
        value={filters.org}
        onChange={(e) => onFiltersChange({ ...filters, org: e.target.value })}
      >
        <option value="">全部机构</option>
        {options?.orgs.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>

      {/* 关系类型多选（FR-7.1，RD-M2-13）：至少保留一项，避免空集 */}
      <div className="seg-group">
        <span className="seg-label">类型</span>
        <div className="seg-toggle">
          {ALL_REL_TYPES.map((t) => (
            <button
              key={t}
              className={filters.relTypes.includes(t) ? 'active' : ''}
              title={REL_TYPE_NAMES[t]}
              onClick={() => {
                const next = filters.relTypes.includes(t)
                  ? filters.relTypes.filter((x) => x !== t)
                  : [...filters.relTypes, t]
                if (next.length > 0) onFiltersChange({ ...filters, relTypes: next })
              }}
            >
              {REL_TYPE_NAMES[t]}
            </button>
          ))}
        </div>
      </div>

      <div className="search-box">
        <div className="search-input-row">
          <select
            value={searchType}
            onChange={(e) => setSearchType(e.target.value as 'name' | 'org')}
          >
            <option value="name">姓名</option>
          </select>
          <input
            placeholder="输入姓名，如 Wei Zhang（回车定位其合作网络）"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
        </div>
        {(results.length > 0 || searching) && (
          <ul className="search-results">
            {searching && <li className="muted small">搜索中…</li>}
            {results.map((r) => (
              <li key={r.id}>
                <button
                  onClick={() => {
                    onLocatePerson(r.id)
                    setQuery('')
                    setResults([])
                  }}
                >
                  {r.name}
                  {r.org && <span className="muted small"> · {r.org}</span>}
                </button>
              </li>
            ))}
            {!searching && results.length === 0 && query.trim().length >= 2 && (
              <li className="muted small">无匹配</li>
            )}
          </ul>
        )}
      </div>

      <select
        value={filters.coopMin}
        title="只看合作达到该次数的关系（隐藏单次合作）"
        onChange={(e) => onFiltersChange({ ...filters, coopMin: Number(e.target.value) })}
      >
        {COOP_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>

      <label className="slider-label">
        强度 ≥ {filters.strengthMin.toFixed(2)}
        <input
          type="range"
          min={0}
          max={1}
          step={0.05}
          value={filters.strengthMin}
          onChange={(e) =>
            onFiltersChange({ ...filters, strengthMin: Number(e.target.value) })
          }
        />
      </label>

      <div className="seg-group">
        <span className="seg-label">规模</span>
        <div className="seg-toggle">
          {SCALE_OPTIONS.map((o) => (
            <button
              key={o.value}
              className={filters.limit === o.value ? 'active' : ''}
              onClick={() => onFiltersChange({ ...filters, limit: o.value })}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      <div className="view-toggle">
        <button
          className={view === 'graph' ? 'active' : ''}
          onClick={() => onViewChange('graph')}
        >
          图谱
        </button>
        <button
          className={view === 'list' ? 'active' : ''}
          onClick={() => onViewChange('list')}
        >
          列表
        </button>
      </div>
    </div>
  )
}
