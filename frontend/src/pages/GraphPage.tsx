import {
  Background,
  Controls,
  ReactFlow,
  type Edge,
  type EdgeMouseHandler,
  type Node,
  type NodeMouseHandler,
  type ReactFlowInstance,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api/client'
import { ALL_REL_TYPES, relTypeLabel } from '../api/types'
import type { GraphData } from '../api/types'
import { computeLayout } from '../graph/layout'
import { PersonNode } from '../graph/PersonNode'
import { EvidencePanel, PersonDetailPanel } from '../components/DetailPanel'
import {
  DEFAULT_FILTERS,
  FilterBar,
  type GraphFilters,
} from '../components/FilterBar'

const nodeTypes = { person: PersonNode }

interface SelectedEdge {
  id: number
  summary: string | null
  label: string
}

interface CenterPerson {
  id: number
  name: string
}

export default function GraphPage() {
  // 支持 ?person=<id> / ?org=<名称> 直达（分享链接 / 书签）
  const [urlPerson, urlOrg] = useMemo(() => {
    const p = new URLSearchParams(window.location.search)
    return [p.get('person'), p.get('org') ?? '']
  }, [])
  const [filters, setFilters] = useState<GraphFilters>({ ...DEFAULT_FILTERS, org: urlOrg })
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const [data, setData] = useState<GraphData | null>(null)
  const [error, setError] = useState<string | null>(null)
  // M2.5：编辑/删除落库后 +1 触发图谱重拉
  const [graphVersion, setGraphVersion] = useState(0)
  const [selectedNode, setSelectedNode] = useState<number | null>(null)
  const [center, setCenter] = useState<CenterPerson | null>(
    urlPerson ? { id: Number(urlPerson), name: '' } : null,
  )
  const [selectedEdge, setSelectedEdge] = useState<SelectedEdge | null>(null)
  const [hoveredEdgeId, setHoveredEdgeId] = useState<string | null>(null)
  const [rfInstance, setRfInstance] = useState<ReactFlowInstance | null>(null)

  // 过滤参数/中心老师变化 → 重新拉图（滑杆与下拉共用一个 300ms 防抖）
  useEffect(() => {
    const t = setTimeout(() => {
      const params = new URLSearchParams()
      if (filters.direction) params.set('direction', filters.direction)
      if (filters.track) params.set('track', filters.track)
      if (filters.org) params.set('org', filters.org)
      // 关系类型：默认全开不发参数；有勾选差异时下发（FR-7.1）
      const sorted = [...filters.relTypes].sort()
      if (sorted.join(',') !== [...ALL_REL_TYPES].sort().join(',')) {
        params.set('rel_types', sorted.join(','))
      }
      if (filters.strengthMin > 0) params.set('strength_min', String(filters.strengthMin))
      if (filters.coopMin > 0) params.set('coop_min', String(filters.coopMin))
      params.set('limit', String(filters.limit))
      if (center) params.set('person', String(center.id))
      api
        .get<GraphData>(`/api/graph?${params.toString()}`)
        .then((d) => {
          setData(d)
          setError(null)
          // 中心老师的名字在数据到位后回填（搜索时可能还不在当前图里）
          setCenter((c) =>
            c && !c.name
              ? { ...c, name: d.nodes.find((n) => n.id === c.id)?.name ?? `#${c.id}` }
              : c,
          )
        })
        .catch((e: unknown) =>
          setError(e instanceof ApiError ? e.message : '图谱加载失败'),
        )
    }, 300)
    return () => clearTimeout(t)
  }, [filters, center, graphVersion])

  // 数据/中心变化后视口收拢（换机构/换老师时避免视口停在旧位置）
  useEffect(() => {
    if (data && data.nodes.length > 0) {
      rfInstance?.fitView({ duration: 300, padding: 0.15 })
    }
  }, [data, rfInstance])

  const layout = useMemo(
    () =>
      data
        ? computeLayout(
            data.nodes.map((n) => n.id),
            data.edges,
          )
        : new Map<number, { x: number; y: number }>(),
    [data],
  )

  // 邻接表（O(1) 邻居判定，替代此前每节点 O(m) 扫描）
  const { degrees, neighbors } = useMemo(() => {
    const degree = new Map<number, number>()
    const adj = new Map<number, Set<number>>()
    for (const e of data?.edges ?? []) {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1)
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1)
      if (!adj.has(e.source)) adj.set(e.source, new Set())
      if (!adj.has(e.target)) adj.set(e.target, new Set())
      adj.get(e.source)!.add(e.target)
      adj.get(e.target)!.add(e.source)
    }
    return { degrees: degree, neighbors: adj }
  }, [data])

  const focusId = selectedNode ?? center?.id ?? null

  const { rfNodes, rfEdges } = useMemo(() => {
    if (!data) return { rfNodes: [] as Node[], rfEdges: [] as Edge[] }
    const rfNodes: Node[] = data.nodes.map((n) => ({
      id: String(n.id),
      type: 'person',
      position: layout.get(n.id) ?? { x: 0, y: 0 },
      data: {
        person: n,
        degree: degrees.get(n.id) ?? 0,
        highlight: focusId === n.id,
        dimmed:
          focusId !== null &&
          focusId !== n.id &&
          !(neighbors.get(focusId)?.has(n.id) ?? false),
      },
    }))
    const rfEdges: Edge[] = data.edges.map((e) => ({
      id: String(e.id),
      source: String(e.source),
      target: String(e.target),
      // RD-M2-13：三类型边样式统一（实线），hover 时以标签显示类型（含传承子类型）
      label:
        hoveredEdgeId === String(e.id)
          ? relTypeLabel(e.type, e.subtype || undefined)
          : undefined,
      style: { strokeWidth: 1 + Math.round(e.strength * 2) },
      className:
        focusId === null || e.source === focusId || e.target === focusId
          ? 'coop-edge'
          : 'coop-edge edge-quiet',
    }))
    return { rfNodes, rfEdges }
  }, [data, layout, degrees, neighbors, focusId, hoveredEdgeId])

  const onNodeClick: NodeMouseHandler = (_, node) => {
    setSelectedEdge(null)
    setSelectedNode(Number(node.id))
  }
  const onEdgeClick: EdgeMouseHandler = (_, edge) => {
    const src = data?.edges.find((e) => String(e.id) === edge.id)
    setSelectedNode(null)
    setSelectedEdge({
      id: Number(edge.id),
      summary: src?.evidence_summary ?? null,
      label: src ? relTypeLabel(src.type, src.subtype || undefined) : '关系',
    })
  }
  const onPaneClick = () => {
    setSelectedNode(null)
    setSelectedEdge(null)
  }

  const onLocatePerson = (personId: number) => {
    // 搜索命中 → 进入"以该老师为中心"的合作子网
    const person = data?.nodes.find((n) => n.id === personId)
    setCenter({ id: personId, name: person?.name ?? '' })
    setSelectedEdge(null)
    setSelectedNode(null)
  }

  const nameOf = (id: number) =>
    data?.nodes.find((n) => n.id === id)?.name ?? `#${id}`

  return (
    <div className="graph-page">
      <FilterBar
        filters={filters}
        onFiltersChange={(next) => {
          setFilters(next)
          setSelectedEdge(null)
          setSelectedNode(null)
        }}
        onLocatePerson={onLocatePerson}
        view={view}
        onViewChange={setView}
      />

      {view === 'graph' ? (
        <div className="flow-wrap">
          {center && (
            <button
              className="ego-chip"
              onClick={() => setCenter(null)}
              title="退出以该老师为中心的视图"
            >
              以 <strong>{center.name || `#${center.id}`}</strong> 为中心 ✕
            </button>
          )}
          {error && <div className="banner error">图谱加载失败：{error}</div>}
          {!error && !data && <div className="banner">图谱加载中…</div>}
          {data && data.nodes.length === 0 && (
            <div className="banner">
              暂无图谱数据——{center ? '该老师暂无合作关系' : '等待采集管线运行（见管理后台"立即更新"）'}
            </div>
          )}
          {data && data.nodes.length > 0 && (
            <ReactFlow
              nodes={rfNodes}
              edges={rfEdges}
              nodeTypes={nodeTypes}
              onInit={setRfInstance}
              onNodeClick={onNodeClick}
              onEdgeClick={onEdgeClick}
              onEdgeMouseEnter={(_, edge) => setHoveredEdgeId(edge.id)}
              onEdgeMouseLeave={() => setHoveredEdgeId(null)}
              onPaneClick={onPaneClick}
              fitView
              minZoom={0.05}
              onlyRenderVisibleElements
              proOptions={{ hideAttribution: true }}
            >
              <Background gap={24} />
              <Controls />
            </ReactFlow>
          )}
        </div>
      ) : (
        <div className="list-view">
          {error && <p className="error-text">加载失败：{error}</p>}
          {data && data.edges.length === 0 && <p className="muted">无匹配关系</p>}
          {data && data.edges.length > 0 && (
            <table className="rel-table">
              <thead>
                <tr>
                  <th>人物 A</th>
                  <th>人物 B</th>
                  <th>关系类型</th>
                  <th>合作次数</th>
                  <th>强度</th>
                  <th>时间范围</th>
                  <th>证据</th>
                </tr>
              </thead>
              <tbody>
                {data.edges.map((e) => (
                  <tr
                    key={e.id}
                    onClick={() =>
                      setSelectedEdge({
                        id: e.id,
                        summary: e.evidence_summary,
                        label: relTypeLabel(e.type, e.subtype || undefined),
                      })
                    }
                  >
                    <td>{nameOf(e.source)}</td>
                    <td>{nameOf(e.target)}</td>
                    <td>{relTypeLabel(e.type, e.subtype || undefined)}</td>
                    <td>{e.coop_count}</td>
                    <td>{e.strength.toFixed(2)}</td>
                    <td>
                      {e.time_start ?? '—'} ~ {e.time_end ?? '—'}
                    </td>
                    <td className="muted small">{e.evidence_summary ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {selectedNode !== null && (
        <PersonDetailPanel
          personId={selectedNode}
          onClose={() => {
            setSelectedNode(null)
          }}
          onOpenRelationship={(relationshipId, label, summary) =>
            setSelectedEdge({ id: relationshipId, label, summary })
          }
          onDataChanged={() => setGraphVersion((v) => v + 1)}
        />
      )}
      {selectedEdge && (
        <EvidencePanel
          relationshipId={selectedEdge.id}
          label={selectedEdge.label}
          summary={selectedEdge.summary}
          onClose={() => setSelectedEdge(null)}
          onDataChanged={() => setGraphVersion((v) => v + 1)}
        />
      )}
    </div>
  )
}
