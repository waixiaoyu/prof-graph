import { Handle, Position, type NodeProps } from '@xyflow/react'
import { memo } from 'react'
import type { GraphNode } from '../api/types'

export interface PersonNodeData extends Record<string, unknown> {
  person: GraphNode
  degree: number
  highlight: boolean
  dimmed: boolean
}

/** Person 节点：大小按度数，主机构一行；高亮/暗化支持子网聚焦。 */
function PersonNodeInner({ data }: NodeProps) {
  const d = data as PersonNodeData
  const { person, degree, highlight, dimmed } = d
  const org = person.orgs[0]?.name
  return (
    <div
      className={`person-node${highlight ? ' highlight' : ''}${dimmed ? ' dimmed' : ''}`}
      style={{ width: 28 + Math.min(degree, 8) * 7 }}
    >
      <Handle type="source" position={Position.Top} style={{ opacity: 0 }} />
      <div className="person-node-name">{person.name}</div>
      {org && <div className="person-node-org">{org}</div>}
      <div className="person-node-meta">{person.paper_count} 篇 · 度 {degree}</div>
      <Handle type="target" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  )
}

export const PersonNode = memo(PersonNodeInner)
