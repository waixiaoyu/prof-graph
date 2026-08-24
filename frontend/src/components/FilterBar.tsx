import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { FilterOptions, PersonSearchItem } from '../api/types'

export interface GraphFilters {
  direction: string
  track: string
  strengthMin: number
}

export const DEFAULT_FILTERS: GraphFilters = {
  direction: '',
  track: '',
  strengthMin: 0,
}

/** 顶部筛选栏（FR-5.2/5.4/5.6）：方向/赛道下拉、姓名/机构防抖搜索、强度滑杆、视图切换。 */
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

      <div className="search-box">
        <div className="search-input-row">
          <select
            value={searchType}
            onChange={(e) => setSearchType(e.target.value as 'name' | 'org')}
          >
            <option value="name">姓名</option>
            <option value="org">机构</option>
          </select>
          <input
            placeholder={searchType === 'name' ? '输入姓名，如 Wei Zhang' : '输入机构，如 Tsinghua'}
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
