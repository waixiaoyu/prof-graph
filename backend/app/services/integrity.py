"""数据不变量防护网（M1 做实，2026-08-26）。

linker 膨胀事故（2026-08-26，修复 1d46d1f）的教训：管线每轮全量重跑，
任何非幂等写入都会让数据悄悄变脏且界面上看不出来。本模块集中声明
"干净数据"的可验证不变量，只读不改，供三处调用：

- 凌晨管线跑完后自动巡检（app/scheduler.py，违例记 WARNING 日志）
- 后台可视化 GET /api/admin/integrity（app/api/admin.py）
- pytest 回归（tests/test_integrity.py，合并/幂等测试共用）

不变量清单（违例即数据脏，需人工或脚本修复）：
  C1 关系合作数 == 证据行数，且证据 ≥1 —— coop_count 的事实来源是证据表
  C2 strength / identity_confidence ∈ [0, 1]
  C3 无自环关系（person_a_id != person_b_id）
  C4 同类型关系无重复人对（(type, lo, hi) 唯一）
  C5 证据论文均为已抽取（extracted）且在 CN 范围内（has_cn_scholar）
  C6 关系两端均非消歧墓碑（merged_into_id IS NULL）
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Paper, Person, Relationship, RelationshipEvidence

SAMPLE_LIMIT = 5  # 每项检查最多展示的违例样本数


@dataclass(frozen=True)
class CheckResult:
    check: str  # 编号 + 名称，如 "C1 合作数与证据一致"
    violations: int
    sample: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.violations == 0


def _fmt(rows, template) -> list[str]:
    return [template.format(*r[: template.count("{")]) for r in rows[:SAMPLE_LIMIT]]


async def check_integrity(session: AsyncSession) -> dict:
    """跑全部不变量检查，返回 {ok, checked_at, checks:[...]}，只读不写。"""
    checks = [
        await _c1_coop_matches_evidence(session),
        await _c2_score_bounds(session),
        await _c3_no_self_loops(session),
        await _c4_no_duplicate_pairs(session),
        await _c5_evidence_paper_scope(session),
        await _c6_no_tombstone_refs(session),
    ]
    return {
        "ok": all(c.ok for c in checks),
        "checked_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "checks": [
            {"check": c.check, "violations": c.violations, "sample": c.sample}
            for c in checks
        ],
    }


async def _c1_coop_matches_evidence(session: AsyncSession) -> CheckResult:
    ev = func.count(RelationshipEvidence.paper_id)
    rows = (
        await session.execute(
            select(Relationship.id, Relationship.coop_count, ev)
            .outerjoin(RelationshipEvidence, RelationshipEvidence.relationship_id == Relationship.id)
            .group_by(Relationship.id)
            .having(or_(Relationship.coop_count != ev, ev == 0))
        )
    ).all()
    return CheckResult(
        "C1 合作数与证据一致",
        len(rows),
        _fmt(rows, "关系 {}: coop_count={}, 证据={} 行"),
    )


async def _c2_score_bounds(session: AsyncSession) -> CheckResult:
    rows = (
        await session.execute(
            select(Relationship.id, Relationship.strength, Relationship.identity_confidence).where(
                or_(
                    Relationship.strength < 0,
                    Relationship.strength > 1,
                    Relationship.identity_confidence < 0,
                    Relationship.identity_confidence > 1,
                )
            )
        )
    ).all()
    return CheckResult(
        "C2 强度/身份置信度在 [0,1]",
        len(rows),
        _fmt(rows, "关系 {}: strength={}, identity={}"),
    )


async def _c3_no_self_loops(session: AsyncSession) -> CheckResult:
    rows = (
        await session.execute(
            select(Relationship.id).where(Relationship.person_a_id == Relationship.person_b_id)
        )
    ).all()
    return CheckResult("C3 无自环关系", len(rows), [f"关系 {r[0]}: 两端同人" for r in rows[:SAMPLE_LIMIT]])


async def _c4_no_duplicate_pairs(session: AsyncSession) -> CheckResult:
    lo = func.least(Relationship.person_a_id, Relationship.person_b_id)
    hi = func.greatest(Relationship.person_a_id, Relationship.person_b_id)
    rows = (
        await session.execute(
            select(lo, hi, Relationship.type, func.count())
            .group_by(lo, hi, Relationship.type)
            .having(func.count() > 1)
        )
    ).all()
    return CheckResult(
        "C4 同类型关系无重复人对",
        len(rows),
        _fmt(rows, "人对 ({}, {}) 类型 {} 有 {} 条"),
    )


async def _c5_evidence_paper_scope(session: AsyncSession) -> CheckResult:
    rows = (
        await session.execute(
            select(RelationshipEvidence.relationship_id, RelationshipEvidence.paper_id)
            .join(Paper, Paper.id == RelationshipEvidence.paper_id)
            .where(or_(Paper.status != "extracted", Paper.has_cn_scholar.is_(False)))
        )
    ).all()
    return CheckResult(
        "C5 证据论文已抽取且在 CN 范围",
        len(rows),
        _fmt(rows, "关系 {} 的证据论文 {} 越界"),
    )


async def _c6_no_tombstone_refs(session: AsyncSession) -> CheckResult:
    tomb = select(Person.id).where(Person.merged_into_id.is_not(None))
    rows = (
        await session.execute(
            select(Relationship.id, Relationship.person_a_id, Relationship.person_b_id).where(
                or_(Relationship.person_a_id.in_(tomb), Relationship.person_b_id.in_(tomb))
            )
        )
    ).all()
    return CheckResult(
        "C6 关系不引用消歧墓碑",
        len(rows),
        _fmt(rows, "关系 {} 引用了已合并学者 ({}, {})"),
    )
