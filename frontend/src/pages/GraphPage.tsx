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
}

export default function GraphPage() {
  const [filters, setFilters] = useState<GraphFilters>(DEFAULT_FILTERS)
  const [view, setView] = useState<'graph' | 'list'>('graph')
  const [data, setData] = useState<GraphData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedNode, setSelectedNode] = useState<number | null>(null)
  const [locatedPerson, setLocatedPerson] = useState<number | null>(null)
  const [selectedEdge, setSelectedEdge] = useState<SelectedEdge | null>(null)
  const [rfInstance, setRfInstance] = useState<ReactFlowInstance | null>(null)

  // 过滤参数变化 → 重新拉图（滑杆与下拉共用一个 300ms 防抖）
  useEffect(() => {
    const t = setTimeout(() => {
      const params = new URLSearchParams()
      if (filters.direction) params.set('direction', filters.direction)
      if (filters.track) params.set('track', filters.track)
      if (filters.strengthMin > 0) params.set('strength_min', String(filters.strengthMin))
      const qs = params.toString()
      api
        .get<GraphData>(`/api/graph${qs ? `?${qs}` : ''}`)
        .then((d) => {
          setData(d)
          setError(null)
        })
        .catch((e: unknown) =>
          setError(e instanceof ApiError ? e.message : '图谱加载失败'),
        )
    }, 300)
    return () => clearTimeout(t)
  }, [filters])

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

  const degrees = useMemo(() => {
    const degree = new Map<number, number>()
    for (const e of data?.edges ?? []) {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1)
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1)
    }
    return degree
  }, [data])

  const { rfNodes, rfEdges } = useMemo(() => {
    if (!data) return { rfNodes: [] as Node[], rfEdges: [] as Edge[] }
    const rfNodes: Node[] = data.nodes.map((n) => ({
      id: String(n.id),
      type: 'person',
      position: layout.get(n.id) ?? { x: 0, y: 0 },
      data: {
        person: n,
        degree: degrees.get(n.id) ?? 0,
        highlight: selectedNode === n.id || locatedPerson === n.id,
        dimmed:
          locatedPerson !== null &&
          locatedPerson !== n.id &&
          !data.edges.some(
            (e) =>
              (e.source === locatedPerson && e.target === n.id) ||
              (e.target === locatedPerson && e.source === n.id),
          ),
      },
    }))
    const rfEdges: Edge[] = data.edges.map((e) => ({
      id: String(e.id),
      source: String(e.source),
      target: String(e.target),
      style: { strokeWidth: 1 + Math.round(e.strength * 5) },
      className: 'coop-edge',
    }))
    return { rfNodes, rfEdges }
  }, [data, layout, degrees, selectedNode, locatedPerson])

  const onNodeClick: NodeMouseHandler = (_, node) => {
    setSelectedEdge(null)
    setSelectedNode(Number(node.id))
  }
  const onEdgeClick: EdgeMouseHandler = (_, edge) => {
    const src = data?.edges.find((e) => String(e.id) === edge.id)
    setSelectedNode(null)
    setSelectedEdge({ id: Number(edge.id), summary: src?.evidence_summary ?? null })
  }
  const onPaneClick = () => {
    setSelectedNode(null)
    setSelectedEdge(null)
  }

  const onLocatePerson = (personId: number) => {
    setLocatedPerson(personId)
    setSelectedNode(personId)
    // 等节点渲染后聚焦到该节点（图谱视图）
    setTimeout(() => {
      rfInstance?.fitView({ nodes: [{ id: String(personId) }], duration: 400, maxZoom: 1.2 })
    }, 50)
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
          setLocatedPerson(null)
        }}
        onLocatePerson={onLocatePerson}
        view={view}
        onViewChange={setView}
      />

      {view === 'graph' ? (
        <div className="flow-wrap">
          {error && <div className="banner error">图谱加载失败：{error}</div>}
          {!error && !data && <div className="banner">图谱加载中…</div>}
          {data && data.nodes.length === 0 && (
            <div className="banner">
              暂无图谱数据——等待采集管线运行（见管理后台“立即更新”）
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
              onPaneClick={onPaneClick}
              fitView
              minZoom={0.05}
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
                  <th>合作次数</th>
                  <th>强度</th>
                  <th>时间范围</th>
                  <th>证据</th>
                </tr>
              </thead>
              <tbody>
                {data.edges.map((e) => (
                  <tr key={e.id} onClick={() => setSelectedEdge({ id: e.id, summary: e.evidence_summary })}>
                    <td>{nameOf(e.source)}</td>
                    <td>{nameOf(e.target)}</td>
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
            setLocatedPerson(null)
          }}
        />
      )}
      {selectedEdge && (
        <EvidencePanel
          relationshipId={selectedEdge.id}
          summary={selectedEdge.summary}
          onClose={() => setSelectedEdge(null)}
        />
      )}
    </div>
  )
}
