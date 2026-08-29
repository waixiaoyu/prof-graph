"""后台手动编辑服务层（M2.5-T2，FR-1~FR-6）。

统一模式：校验 → before 快照 → 变更 → after 快照 → 写 admin_edits 日志行，
全部在同一调用方事务内完成（NFR-4 落库成功 ⟺ 日志成对）。

墓碑语义（RD-2）：
- 关系删除 = 打 deleted_at 墓碑，证据保留作审计；linker 后续轮次跳过墓碑行
  （FR-4.2，拦截点在三个 linker，本模块只负责置墓碑）
- 按人删除（FR-5，合规）= person 打墓碑 + 其关系全部墓碑并物理删证据行
  （合规抹除，与 FR-4 保留证据的不对称是有意的）+ 清归属/标签/作者挂接
  （paper_authors.person_id 置 NULL，行保留——论文作者名单不缺位）+ 取消 pending 消歧
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AdminEdit,
    DisambiguationQueue,
    Organization,
    PaperAuthor,
    Person,
    PersonOrg,
    PersonResearchTag,
    Relationship,
    RelationshipEvidence,
    RelationshipEvidenceNews,
    RelationshipEvidencePage,
)
from app.utils.names import normalize_person_name

# 可经 PATCH 修改的 Person 展示字段（FR-1.1）
PERSON_EDITABLE_FIELDS = ("name", "title", "homepage", "email")


class AdminEditError(Exception):
    """业务校验失败；status_code 由 API 层转为 HTTPException。"""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _get_live_person(session: AsyncSession, person_id: int) -> Person:
    person = (
        await session.execute(select(Person).where(Person.id == person_id))
    ).scalar_one_or_none()
    if person is None:
        raise AdminEditError(f"学者 {person_id} 不存在", 404)
    if person.merged_into_id is not None:
        raise AdminEditError(f"学者 {person_id} 已并入他人（消歧墓碑），不可编辑", 409)
    if person.deleted_at is not None:
        raise AdminEditError(f"学者 {person_id} 已被删除（合规墓碑），不可编辑", 409)
    return person


async def record_edit(
    session: AsyncSession,
    action: str,
    entity_type: str,
    entity_id: int,
    before: dict | None,
    after: dict | None,
    reason: str | None,
) -> None:
    session.add(
        AdminEdit(
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            reason=reason,
        )
    )


def _person_snapshot(person: Person, orgs: list[str] | None = None, tags: list[str] | None = None) -> dict:
    snap = {
        "name": person.name,
        "title": person.title,
        "homepage": person.homepage,
        "email": person.email,
    }
    if orgs is not None:
        snap["orgs"] = orgs
    if tags is not None:
        snap["tags"] = tags
    return snap


async def update_person_fields(
    session: AsyncSession, person_id: int, fields: dict, reason: str
) -> Person:
    """FR-1：改 name/title/homepage/email；改 name 同步重算归一。"""
    unknown = set(fields) - set(PERSON_EDITABLE_FIELDS)
    if unknown:
        raise AdminEditError(f"不可编辑字段: {sorted(unknown)}")
    person = await _get_live_person(session, person_id)
    before = _person_snapshot(person)
    for key, value in fields.items():
        setattr(person, key, value)
    if "name" in fields:
        person.name_normalized = normalize_person_name(fields["name"])
    await record_edit(
        session, "update_person", "person", person_id, before, _person_snapshot(person), reason
    )
    return person


async def set_person_orgs(
    session: AsyncSession, person_id: int, org_ids: list[int], reason: str
) -> list[dict]:
    """FR-2：全量替换机构归属；只能选已有 organizations（RD-8），source='admin'。"""
    person = await _get_live_person(session, person_id)
    orgs = (
        (
            await session.execute(
                select(Organization).where(Organization.id.in_(org_ids))
            )
        )
        .scalars()
        .all()
        if org_ids
        else []
    )
    if len(orgs) != len(set(org_ids)):
        missing = sorted(set(org_ids) - {o.id for o in orgs})
        raise AdminEditError(f"机构不存在: {missing}", 404)

    before_names = (
        select(Organization.name)
        .join(PersonOrg, PersonOrg.org_id == Organization.id)
        .where(PersonOrg.person_id == person_id)
    )
    before = _person_snapshot(person, orgs=[r[0] for r in (await session.execute(before_names)).all()])

    await session.execute(delete(PersonOrg).where(PersonOrg.person_id == person_id))
    for oid in dict.fromkeys(org_ids):  # 去重保序
        session.add(PersonOrg(person_id=person_id, org_id=oid, org_confidence=1.0, source="admin"))

    after = _person_snapshot(person, orgs=[o.name for o in orgs])
    await record_edit(session, "set_orgs", "person", person_id, before, after, reason)
    return [{"org_id": o.id, "name": o.name} for o in orgs]


async def set_person_research_tags(
    session: AsyncSession, person_id: int, tags: list[str], reason: str
) -> list[str]:
    """FR-3：整体替换研究方向标签集合。"""
    person = await _get_live_person(session, person_id)
    before_rows = (
        await session.execute(
            select(PersonResearchTag.tag).where(PersonResearchTag.person_id == person_id)
        )
    ).all()
    before = _person_snapshot(person, tags=[r[0] for r in before_rows])

    await session.execute(
        delete(PersonResearchTag).where(PersonResearchTag.person_id == person_id)
    )
    for tag in dict.fromkeys(t.strip() for t in tags if t and t.strip()):
        session.add(PersonResearchTag(person_id=person_id, tag=tag))

    after = _person_snapshot(person, tags=sorted({t.strip() for t in tags if t and t.strip()}))
    await record_edit(session, "set_research_tags", "person", person_id, before, after, reason)
    return after["tags"]


async def _get_live_relationship(session: AsyncSession, rel_id: int) -> Relationship:
    rel = (
        await session.execute(select(Relationship).where(Relationship.id == rel_id))
    ).scalar_one_or_none()
    if rel is None:
        raise AdminEditError(f"关系 {rel_id} 不存在", 404)
    if rel.deleted_at is not None:
        raise AdminEditError(f"关系 {rel_id} 已删除（墓碑），不可再操作", 409)
    return rel


async def delete_relationship(
    session: AsyncSession, rel_id: int, reason: str
) -> Relationship:
    """FR-4.1：墓碑删除，证据保留作审计（RD-2）。"""
    rel = await _get_live_relationship(session, rel_id)
    before = {
        "type": rel.type,
        "subtype": rel.subtype,
        "person_a_id": rel.person_a_id,
        "person_b_id": rel.person_b_id,
        "strength": float(rel.strength),
        "deleted": False,
    }
    rel.deleted_at = _now()
    rel.deleted_reason = reason
    await record_edit(
        session,
        "delete_relationship",
        "relationship",
        rel_id,
        before,
        {**before, "deleted": True},
        reason,
    )
    return rel


async def adjust_relationship_strength(
    session: AsyncSession, rel_id: int, strength: float, reason: str
) -> Relationship:
    """FR-4.3：人工调整强度 ∈ [0,1]（降权可疑关系）。"""
    if not (0 <= strength <= 1):
        raise AdminEditError("strength 必须在 [0, 1]", 422)
    rel = await _get_live_relationship(session, rel_id)
    before = {"strength": float(rel.strength), "adjusted": False}
    rel.strength = strength
    await record_edit(
        session,
        "adjust_strength",
        "relationship",
        rel_id,
        before,
        {"strength": strength, "adjusted": True},
        reason,
    )
    return rel


async def delete_person(session: AsyncSession, person_id: int, reason: str) -> dict:
    """FR-5：合规级联删除（论文与机构实体保留，RD-4）。

    与 FR-4 的不对称是有意的：此处物理删证据行（合规抹除该人关联数据）。
    """
    person = await _get_live_person(session, person_id)

    rels = (
        (
            await session.execute(
                select(Relationship).where(
                    (Relationship.person_a_id == person_id)
                    | (Relationship.person_b_id == person_id)
                )
            )
        )
        .scalars()
        .all()
    )
    live_rel_ids = [r.id for r in rels if r.deleted_at is None]
    now = _now()
    if live_rel_ids:
        await session.execute(
            update(Relationship)
            .where(Relationship.id.in_(live_rel_ids))
            .values(deleted_at=now, deleted_reason=f"按人删除级联: {reason}")
        )
        await session.execute(
            delete(RelationshipEvidence).where(RelationshipEvidence.relationship_id.in_(live_rel_ids))
        )
        await session.execute(
            delete(RelationshipEvidencePage).where(
                RelationshipEvidencePage.relationship_id.in_(live_rel_ids)
            )
        )
        await session.execute(
            delete(RelationshipEvidenceNews).where(
                RelationshipEvidenceNews.relationship_id.in_(live_rel_ids)
            )
        )

    await session.execute(delete(PersonOrg).where(PersonOrg.person_id == person_id))
    await session.execute(
        delete(PersonResearchTag).where(PersonResearchTag.person_id == person_id)
    )
    # 作者挂接置 NULL：paper_authors 行保留（raw_name 仍在，论文作者名单不缺位），
    # person 侧引用解除
    await session.execute(
        update(PaperAuthor)
        .where(PaperAuthor.person_id == person_id)
        .values(person_id=None)
    )
    cancelled = (
        await session.execute(
            update(DisambiguationQueue)
            .where(
                (DisambiguationQueue.person_a_id == person_id)
                | (DisambiguationQueue.person_b_id == person_id),
                DisambiguationQueue.status == "pending",
            )
            .values(status="cancelled", resolved_at=now)
            .returning(DisambiguationQueue.id)
        )
    ).all()

    person.deleted_at = now
    before = _person_snapshot(person)
    before["relation_count"] = len(rels)
    after = {**before, "deleted": True}
    await record_edit(session, "delete_person", "person", person_id, before, after, reason)
    return {
        "ok": True,
        "cascaded": {
            "relations": len(live_rel_ids),
            "already_tombstoned": len(rels) - len(live_rel_ids),
            "evidence": "cleared for live relations",
            "orgs": "cleared",
            "tags": "cleared",
            "paper_links": "unlinked (rows kept)",
            "queue_cancelled": len(cancelled),
        },
    }


async def list_edits(
    session: AsyncSession,
    entity_type: str | None = None,
    entity_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """FR-6.2：操作日志查询（按实体过滤，倒序分页）。"""
    cond = []
    if entity_type is not None:
        cond.append(AdminEdit.entity_type == entity_type)
    if entity_id is not None:
        cond.append(AdminEdit.entity_id == entity_id)
    base = select(AdminEdit).where(*cond)
    total = (
        await session.execute(
            select(func.count()).select_from(base.subquery())
        )
    ).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(AdminEdit.id.desc()).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {
        "items": [
            {
                "id": e.id,
                "action": e.action,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "before": e.before,
                "after": e.after,
                "reason": e.reason,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in rows
        ],
        "total": total,
    }
