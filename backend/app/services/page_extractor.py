"""网页 GLM 抽取器（M2-T5，FR-3.1/3.2/3.6/3.7，plan §3.1）。

输入 web_pages.content_text（中文按 ~2 chars/token 估，截断 24k chars ≈ 12k
tokens）。输出结构化名单 → 消歧入库（persons / person_org source='webpage'
/ title·homepage·email 回填）。关系推导不在此做——抽取结果（PageExtraction）
交给 mentor_linker（T6）按 plan §3.1 规则配对。

维度分档映射（FR-5.2 输入，GLM 枚举 → 代码查表）：
- 数据源可信度 SRC_BY_CONTEXT：official_lab 1.0 / department_site·grad_list 0.8
  / unclear 0.6（致谢来源恒 0.6，RD-M2-8 → extractor/mentor_linker 侧）
- 信息明确性 CLARITY：role≠unknown 1.0 / unknown 0.6
- 时间吻合度 TIME：有 grad_year 1.0 / 无 0.5
- 推断确定性 INFER 按推导规则，见 mentor_linker（T6）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Person, PersonOrg, WebPage
from app.services.breaker import JobClass
from app.services.disambiguator import (
    AUTO_MERGE_THRESHOLD,
    QUEUE_THRESHOLD,
    ScoreDetail,
    enqueue_pair,
    find_candidates,
    person_org_norms,
    score_name,
    score_org,
    strong_merge_match,
)
from app.services.glm import GLMClient
from app.services.openalex import upsert_organization
from app.utils.names import normalize_person_name

log = logging.getLogger("prof-graph.page_extractor")

# 12k tokens 的中文近似（~2 chars/token）
MAX_CHARS = 24_000

ROLES = frozenset(
    {"professor", "associate_professor", "assistant_professor", "phd", "master", "alumni", "unknown"}
)
PAGE_CONTEXTS = frozenset({"official_lab", "department_site", "grad_list", "unclear"})

# 维度分档映射（plan §3.1 表）
SRC_BY_CONTEXT = {"official_lab": 1.0, "department_site": 0.8, "grad_list": 0.8, "unclear": 0.6}
CLARITY_KNOWN, CLARITY_UNKNOWN = 1.0, 0.6
TIME_WITH_YEAR, TIME_NO_YEAR = 1.0, 0.5

PAGE_EXTRACT_SYSTEM = (
    "你是学术信息抽取助手。从高校实验室/院系网页正文中抽取成员名单与机构信息。"
    '只输出 JSON：{"lab_name": "实验室名或 null", "org_school": "学校名或 null", '
    '"org_department": "院系名或 null", '
    '"members": [{"name": "姓名", '
    '"role": "professor|associate_professor|assistant_professor|phd|master|alumni|unknown", '
    '"advisor": "导师姓名或 null", "grad_year": 毕业年份整数或 null, '
    '"title": "职称或 null", "homepage": "个人主页 URL 或 null", "email": "邮箱或 null"}], '
    '"page_context": "official_lab|department_site|grad_list|unclear"}。'
    "members 覆盖页面列出的全部人员（教师/博士/硕士/校友，含分组标题下的人员）；"
    "advisor 仅在页面明示指导关系时给出；grad_year 仅毕业/年级信息明确时给出。"
    "page_context：实验室/课题组官方页=official_lab，院系网站=department_site，"
    "毕业名单页=grad_list，无法判断=unclear。页面无人员信息时 members 输出空数组。"
)


@dataclass
class Member:
    name: str
    role: str = "unknown"
    advisor: str | None = None
    grad_year: int | None = None
    title: str | None = None
    homepage: str | None = None
    email: str | None = None
    person_id: int | None = None  # 消歧结果（persist 后填）
    identity: float = 0.9  # 强归并 1.0 / 打分归并取分 / 新建 0.9（RD-M2-12）


@dataclass
class PageExtraction:
    lab_name: str | None = None
    org_school: str | None = None
    org_department: str | None = None
    page_context: str = "unclear"
    members: list[Member] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    org_ids: dict[str, int] = field(default_factory=dict)  # lab/department/school → org id

    @property
    def src_confidence(self) -> float:
        return SRC_BY_CONTEXT.get(self.page_context, 0.6)


def _clean_str(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def validate_page_extraction(data: dict) -> PageExtraction:
    """顶层 JSON → PageExtraction。schema 破损抛 ValueError；无成员返回空 members。"""
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")
    raw_members = data.get("members")
    if not isinstance(raw_members, list):
        raise ValueError("缺少 members 数组")

    ext = PageExtraction(
        lab_name=_clean_str(data.get("lab_name")),
        org_school=_clean_str(data.get("org_school")),
        org_department=_clean_str(data.get("org_department")),
        page_context=data.get("page_context") if data.get("page_context") in PAGE_CONTEXTS else "unclear",
    )
    for i, item in enumerate(raw_members):
        if not isinstance(item, dict):
            ext.warnings.append(f"members[{i}] 非对象，跳过")
            continue
        name = _clean_str(item.get("name"))
        if not name:
            ext.warnings.append(f"members[{i}] 缺 name，跳过")
            continue
        role = item.get("role") if item.get("role") in ROLES else "unknown"
        grad_year = item.get("grad_year")
        if isinstance(grad_year, bool) or not isinstance(grad_year, int):
            grad_year = None
        ext.members.append(
            Member(
                name=name,
                role=role,
                advisor=_clean_str(item.get("advisor")),
                grad_year=grad_year,
                title=_clean_str(item.get("title")),
                homepage=_clean_str(item.get("homepage")),
                email=_clean_str(item.get("email")),
            )
        )
    return ext


async def extract_page(session: AsyncSession, glm: GLMClient, page: WebPage) -> PageExtraction:
    """GLM 抽取单页。空成员名单 = 无信号（调用方置 no_signal）。

    GLM 异常向上抛（BreakerOpenError / GLMError 系），由批处理器记
    failed_jobs（job_type=page_extract）或跳过。
    """
    input_text = (page.content_text or "")[:MAX_CHARS]
    data = await glm.complete_json(
        session,
        system=PAGE_EXTRACT_SYSTEM,
        user=input_text,
        job_type="page_extract",
        job_class=JobClass.extract,
        # IPADS 量级名单（~260 人）实测 4000/8000 均在输出上限截断 JSON，16k 才够（实跑教训）
        max_tokens=16_000,
    )
    ext = validate_page_extraction(data)
    for w in ext.warnings:
        log.warning("页面 %s 抽取部分容错：%s", page.url, w)
    return ext


# ---------- 机构与人员入库 ----------


async def _upsert_org_with_level(session: AsyncSession, name: str, level: str) -> int:
    org = await upsert_organization(session, name)
    if org.level is None:
        org.level = level
    return org.id


async def upsert_page_orgs(session: AsyncSession, ext: PageExtraction) -> None:
    """机构三级 upsert（FR-4.3）：school=university / department / lab。"""
    if ext.org_school:
        ext.org_ids["school"] = await _upsert_org_with_level(session, ext.org_school, "university")
    if ext.org_department:
        ext.org_ids["department"] = await _upsert_org_with_level(
            session, ext.org_department, "department"
        )
    if ext.lab_name:
        ext.org_ids["lab"] = await _upsert_org_with_level(session, ext.lab_name, "lab")


async def _page_score_detail(session: AsyncSession, cand: Person, member: Member, ext: PageExtraction) -> ScoreDetail:
    """网页场景打分：姓名 + 机构有效，方向/时间/合作网络取中性 0.5（无论文上下文）。"""
    org_norms = await person_org_norms(session, cand.id)
    org_str = ext.lab_name or ext.org_department or ext.org_school
    return ScoreDetail(
        name=score_name(member.name, cand.name),
        org=score_org(org_str, org_norms),
        research=0.5,
        time=0.5,
        network=0.5,
    )


async def resolve_member(session: AsyncSession, member: Member, ext: PageExtraction) -> None:
    """消歧并入库：person_id + identity 基准写回 member（RD-M2-12）。

    强归并（姓名归一命中 + 任一级机构同实体）→ 1.0；
    打分 ≥0.8 归并取分；否则新建 0.9（0.5–0.8 与最佳候选入复核队列）。
    """
    org_chain = [ext.lab_name, ext.org_department, ext.org_school]
    for org_str in org_chain:
        if not org_str:
            continue
        strong = await strong_merge_match(session, member.name, org_str)
        if strong is not None:
            member.person_id = strong.id
            member.identity = 1.0
            return

    candidates = await find_candidates(session, member.name)
    best_person, best_detail = None, None
    for cand in candidates:
        detail = await _page_score_detail(session, cand, member, ext)
        if best_detail is None or detail.total > best_detail.total:
            best_person, best_detail = cand, detail

    if best_person is not None and best_detail.total >= AUTO_MERGE_THRESHOLD:
        member.person_id = best_person.id
        member.identity = round(best_detail.total, 2)
        return

    new_person = Person(name=member.name, name_normalized=normalize_person_name(member.name))
    session.add(new_person)
    await session.flush()
    member.person_id = new_person.id
    member.identity = 0.9
    if best_person is not None and best_detail.total >= QUEUE_THRESHOLD:
        await enqueue_pair(session, new_person.id, best_person.id, best_detail)


async def persist_page_result(session: AsyncSession, page: WebPage, ext: PageExtraction) -> None:
    """抽取结果落库：机构三级 + 逐成员消歧入库 + 三级挂靠 + 字段回填。

    状态流转由调用方（T6 阶段 runner）在关系建立后置 extracted；
    person_org.source='webpage'，置信度取页面数据源档。
    """
    await upsert_page_orgs(session, ext)
    org_conf = ext.src_confidence
    for member in ext.members:
        await resolve_member(session, member, ext)
        if not ext.org_ids:
            continue
        # 幂等：重抽（页面变化）时已挂靠的 org 不重复插入（PK 冲突）
        existing_org_ids = set(
            (
                await session.execute(
                    select(PersonOrg.org_id).where(PersonOrg.person_id == member.person_id)
                )
            ).scalars().all()
        )
        for org_id in set(ext.org_ids.values()):  # school/lab 同名时 upsert 出同一 org，去重防 PK 冲突
            if org_id not in existing_org_ids:
                session.add(
                    PersonOrg(
                        person_id=member.person_id,
                        org_id=org_id,
                        org_confidence=org_conf,
                        source="webpage",
                    )
                )
        # 教师页字段回填（FR-3.6，仅填空不覆盖既有值）
        person = await session.get(Person, member.person_id)
        for attr in ("title", "homepage", "email"):
            val = getattr(member, attr)
            if val and getattr(person, attr) is None:
                setattr(person, attr, val)
    await session.flush()
