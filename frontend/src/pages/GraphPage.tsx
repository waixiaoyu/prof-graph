import {
  Background,
  Controls,
  ReactFlow,
  type Edge,
  type EdgeMouseHandler,
  type Node,
  type NodeMouseHandler,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { useEffect, useMemo, useState } from 'react'
import { api, ApiError } from '../api/client'
import type { GraphData } from '../api/types'
import { computeLayout } from '../graph/layout'
import { PersonNode } from '../graph/PersonNode'
import { EvidencePanel, PersonDetailPanel } from '../components/DetailPanel'

const nodeTypes = { person: PersonNode }

interface SelectedEdge {
  id: number
  summary: string | null
}

export default function GraphPage() {
  const [data, setData] = useState<GraphData | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [selectedNode, setSelectedNode] = useState<number | null>(null)
  const [selectedEdge, setSelectedEdge] = useState<SelectedEdge | null>(null)

  useEffect(() => {
    api
      .get<GraphData>('/api/graph')
      .then(setData)
      .catch((e: unknown) =>
        setError(e instanceof ApiError ? e.message : '图谱加载失败'),
      )
  }, [])

  const { rfNodes, rfEdges } = useMemo(() => {
    if (!data) return { rfNodes: [] as Node[], rfEdges: [] as Edge[] }
    const degree = new Map<number, number>()
    for (const e of data.edges) {
      degree.set(e.source, (degree.get(e.source) ?? 0) + 1)
      degree.set(e.target, (degree.get(e.target) ?? 0) + 1)
    }
    const layout = computeLayout(
      data.nodes.map((n) => n.id),
      data.edges,
    )
    const rfNodes: Node[] = data.nodes.map((n) => ({
      id: String(n.id),
      type: 'person',
      position: layout.get(n.id) ?? { x: 0, y: 0 },
      data: {
        person: n,
        degree: degree.get(n.id) ?? 0,
        highlight: selectedNode === n.id,
        dimmed: false,
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
  }, [data, selectedNode])

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

  return (
    <div className="graph-page">
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
        {selectedNode !== null && (
          <PersonDetailPanel
            personId={selectedNode}
            onClose={() => setSelectedNode(null)}
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
    </div>
  )
}
