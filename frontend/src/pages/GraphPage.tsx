import { ReactFlow } from '@xyflow/react'
import '@xyflow/react/dist/style.css'

// T3 占位：T18 将接入真实图谱数据（GET /api/graph）
export default function GraphPage() {
  return (
    <div className="flow-wrap">
      <ReactFlow fitView proOptions={{ hideAttribution: false }}>
        {/* 占位空画布 */}
      </ReactFlow>
    </div>
  )
}
