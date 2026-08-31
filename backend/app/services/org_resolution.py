"""机构消解（M4 第一刀，2026-08-31）。

同一真实机构以不同表面串入库成多行（分号/逗号变体、全角/半角括号、
尾部国家词），让消歧 org 打分假性失配（复核队列 181 条"分号变体"类）。

规则只收零风险等价类：
- R1 标点塌缩后 token 序列相同（`;` `，` `（` `-` `&` 等一律当空格）；
- R2 尾部纯国家词剥离，仅当剩余部分仍有 ≥1 个非通用显著词。

明确不做的（M4 后续，需要 OpenAlex institution id / 人工别名表）：
城市/校区词剥离（会把哈工大本部并深圳校区、HKUST 并广州校区——
校区是机构身份的一部分）、多机构拼接串拆分、缩写↔全称。
"""
from __future__ import annotations

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AdminEdit, Organization, PersonOrg

# 尾部可剥的纯地理词：国家 + 省份（不含城市——城市常是校区标识，
# 哈工大深圳/威海、中大珠海都靠城市词区分；省份尾缀只是信封信息）
COUNTRY_SUFFIX = {
    "china", "usa", "us", "uk", "england", "japan", "korea", "singapore",
    "germany", "france", "australia", "canada", "netherlands", "spain",
    "italy", "sweden", "switzerland", "austria", "india", "israel",
    "guangdong", "jiangsu", "zhejiang", "shandong", "sichuan", "hubei",
    "hunan", "fujian", "anhui", "henan", "hebei", "shaanxi", "liaoning",
    "jilin", "heilongjiang",
}
# 剥掉尾部国家词后，若剩余名字只剩这些泛词则不剥（"Bank of China"
# 不能剥成 "bank of"，否则与 "Bank of England" 撞成同一个键）
_GENERIC = {"the", "of", "and", "for", "a", "bank", "company", "co",
            "inc", "ltd", "research"}
_ORG_STRIP = {"university", "univ", "institute", "inst", "college",
              "lab", "laboratory", "school", "academy"}
_PUNCT = str.maketrans({
    ";": " ", "；": " ", ",": " ", "，": " ", "(": " ", ")": " ",
    "（": " ", "）": " ", "-": " ", "&": " ", "/": " ",
})


def org_key(name: str) -> str:
    """机构规范化键：R1 标点塌缩 + R2 尾部国家词剥离（剥前确认剩余名字仍含显著词）。"""
    s = name.lower().replace(".", "").translate(_PUNCT)
    toks = [t for t in s.split() if t and t not in _ORG_STRIP]
    while toks and toks[-1] in COUNTRY_SUFFIX:
        # 剩余名字必须仍有显著词（非泛词且长度≥4），否则停止剥离
        if not any(t not in _GENERIC and len(t) >= 4 for t in toks[:-1]):
            break
        toks.pop()
    return " ".join(toks) or name.lower().strip() or "unknown-org"


async def plan_org_merges(session: AsyncSession) -> list[dict]:
    """按 org_key 聚簇，给出每簇的代表行与被并行（不写库）。"""
    orgs = (
        await session.execute(
            select(Organization.id, Organization.name)
        )
    ).all()
    refs = dict(
        (await session.execute(
            select(PersonOrg.org_id, func.count()).group_by(PersonOrg.org_id)
        )).all()
    )
    by_key: dict[str, list[tuple[int, str]]] = {}
    for oid, name in orgs:
        by_key.setdefault(org_key(name), []).append((oid, name))
    plans = []
    for key, members in by_key.items():
        if len(members) < 2:
            continue
        ranked = sorted(
            members,
            key=lambda m: (-refs.get(m[0], 0), len(m[1]), m[0]),
        )
        keep = ranked[0]
        plans.append({
            "key": key,
            "keep": {"id": keep[0], "name": keep[1],
                     "refs": refs.get(keep[0], 0)},
            "drops": [{"id": oid, "name": name, "refs": refs.get(oid, 0)}
                      for oid, name in ranked[1:]],
        })
    return plans


async def apply_org_merges(
    session: AsyncSession, plans: list[dict], reason: str
) -> dict[str, int]:
    """执行簇合并 + 全量键重写，单事务；RD-7 审计日志。"""
    merged_rows = moved = 0
    for plan in plans:
        keep_id = plan["keep"]["id"]
        drop_ids = [d["id"] for d in plan["drops"]]
        pos = (
            await session.execute(
                select(PersonOrg).where(PersonOrg.org_id.in_(drop_ids))
            )
        ).scalars().all()
        for po in pos:
            existing = (
                await session.execute(
                    select(PersonOrg).where(
                        PersonOrg.person_id == po.person_id,
                        PersonOrg.org_id == keep_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(
                    PersonOrg(
                        person_id=po.person_id,
                        org_id=keep_id,
                        org_confidence=po.org_confidence,
                        source="org_merge",
                        paper_id=po.paper_id,
                    )
                )
                moved += 1
            elif float(existing.org_confidence) < float(po.org_confidence):
                existing.org_confidence = po.org_confidence
        await session.execute(
            delete(Organization).where(Organization.id.in_(drop_ids))
        )
        merged_rows += len(drop_ids)
        session.add(
            AdminEdit(
                action="merge_orgs",
                entity_type="organization",
                entity_id=keep_id,
                before={"key": plan["key"], "drops": plan["drops"]},
                after={"keep": plan["keep"]},
                reason=reason,
            )
        )

    # 全量键重写：新插入侧 upsert_organization 用同一 org_key，
    # 存量行不同步重写的话同串仍会另起新行
    orgs = (
        await session.execute(
            select(Organization.id, Organization.name, Organization.name_normalized)
        )
    ).all()
    for oid, name, old_key in orgs:
        new_key = org_key(name)
        if new_key != old_key:
            await session.execute(
                update(Organization)
                .where(Organization.id == oid)
                .values(name_normalized=new_key)
            )
    await session.commit()
    return {"merged_rows": merged_rows, "clusters": len(plans),
            "person_org_moved": moved}


async def org_merge_dry_run(session: AsyncSession) -> dict:
    """巡检口径：当前簇数与涉及行数。"""
    plans = await plan_org_merges(session)
    return {
        "clusters": len(plans),
        "mergeable_rows": sum(len(p["drops"]) for p in plans),
    }
