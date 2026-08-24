/** 图布局：小图力导向（子网聚团），大图回退圆形（NFR-1 千节点性能）。 */

export interface LayoutNode {
  id: number
  x: number
  y: number
}

interface EdgeLite {
  source: number
  target: number
  strength: number
}

export function computeLayout(
  ids: number[],
  edges: EdgeLite[],
  width = 1200,
  height = 800,
): Map<number, { x: number; y: number }> {
  const positions = new Map<number, { x: number; y: number }>()
  if (ids.length === 0) return positions

  // 初始：圆形分布（大图直接用它，避免 O(n²) 迭代卡顿）
  const cx = width / 2
  const cy = height / 2
  const radius = Math.min(width, height) * 0.38
  ids.forEach((id, i) => {
    const angle = (2 * Math.PI * i) / ids.length
    positions.set(id, {
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    })
  })
  if (ids.length > 400 || edges.length === 0) return positions

  // 力导向：斥力（库仑）+ 引力（沿边），简单阻尼收敛
  const nodes = ids.map((id) => ({ id, ...positions.get(id)! }))
  const byId = new Map(nodes.map((n) => [n.id, n]))
  const iterations = 60
  const repulsion = 180_000
  const springK = 0.02

  for (let iter = 0; iter < iterations; iter++) {
    const fx = new Map<number, number>(ids.map((id) => [id, 0]))
    const fy = new Map<number, number>(ids.map((id) => [id, 0]))

    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i]
        const b = nodes[j]
        let dx = a.x - b.x
        let dy = a.y - b.y
        let distSq = dx * dx + dy * dy
        if (distSq < 1) {
          dx = Math.random() - 0.5
          dy = Math.random() - 0.5
          distSq = 1
        }
        const force = repulsion / distSq
        const dist = Math.sqrt(distSq)
        const ux = dx / dist
        const uy = dy / dist
        fx.set(a.id, fx.get(a.id)! + force * ux)
        fy.set(a.id, fy.get(a.id)! + force * uy)
        fx.set(b.id, fx.get(b.id)! - force * ux)
        fy.set(b.id, fy.get(b.id)! - force * uy)
      }
    }
    for (const e of edges) {
      const a = byId.get(e.source)
      const b = byId.get(e.target)
      if (!a || !b) continue
      const dx = b.x - a.x
      const dy = b.y - a.y
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1)
      const force = springK * dist * (0.5 + e.strength)
      const ux = (dx / dist) * force
      const uy = (dy / dist) * force
      fx.set(a.id, fx.get(a.id)! + ux)
      fy.set(a.id, fy.get(a.id)! + uy)
      fx.set(b.id, fx.get(b.id)! - ux)
      fy.set(b.id, fy.get(b.id)! - uy)
    }

    const damping = 0.85 * (1 - iter / iterations)
    for (const n of nodes) {
      n.x = Math.max(40, Math.min(width - 40, n.x + Math.max(-30, Math.min(30, fx.get(n.id)!)) * damping))
      n.y = Math.max(40, Math.min(height - 40, n.y + Math.max(-30, Math.min(30, fy.get(n.id)!)) * damping))
    }
  }
  nodes.forEach((n) => positions.set(n.id, { x: n.x, y: n.y }))
  return positions
}
