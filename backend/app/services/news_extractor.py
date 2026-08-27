"""资讯/新闻页 GLM 抽取器（M2-T10，plan §3.3，FR-3.1，RD-M2-6/11）。

输入：RSS 条目标题+摘要（accessibility=仅摘要 0.5）或新闻公示页
content_text（RD-M2-11：page_type=news 的 WebPage 路由至此，全文 0.8）。
输出 persons / projects / participations（含 explicitness、sufficiency 枚举）
→ projects upsert（name_normalized 归并）+ 人员消歧复用网页抽取链路。

分档映射（FR-6.2，置信度数值在 T11 project_linker 组装）：
- explicitness → 1.0/0.8/0.5/0.3；sufficiency → 1.0/0.8/0.5/0.3
- 数据源可信度：高校新闻页 1.0 / RSS tier known_media 0.8 / other 0.6
- 可访问性：全文 0.8 / 仅摘要 0.5

关系配对不在 GLM：participations 只给"人-项目"参与事实，两两 project_
cooperation 由 T11 project_linker 从同项目参与者推导。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import NewsItem, Project, WebPage
from app.services.breaker import JobClass
from app.services.glm import GLMClient
from app.services.news_collector import USER_AGENT  # noqa: F401 — 复用同一 UA 约定
from app.services.page_extractor import MAX_CHARS, Member, PageExtraction, resolve_member
from app.sources_config import RssSource, load_sources

log = logging.getLogger("prof-graph.news_extractor")

# 分档映射（plan §3.3 表）
EXPLICITNESS_SCORES = {
    "listed_members": 1.0, "stated_participation": 0.8, "implied": 0.5, "vague": 0.3,
}
SUFFICIENCY_SCORES = {
    "detailed_role": 1.0, "role_stated": 0.8, "mentioned": 0.5, "minimal": 0.3,
}
ACCESS_FULLTEXT, ACCESS_SUMMARY = 0.8, 0.5
SRC_NEWS_PAGE = 1.0  # 高校官网新闻公示页（RD-M2-11）

RSS_ENTRY_WEBPAGE = "webpage"  # rss_entry.source 标记：来自新闻公示页而非 RSS 源

NEWS_EXTRACT_SYSTEM = (
    "你是学术资讯抽取助手。从新闻/资讯文本中抽取人员、项目与参与关系。"
    '只输出 JSON：{"no_signal": true/false, '
    '"persons": [{"name": "姓名", "org": "单位或 null", "role": "身份或 null"}], '
    '"projects": [{"name": "项目名", '
    '"project_type": "国家重点研发/省市科技项目/企业合作/联合实验室/other 或 null", '
    '"time_start": "起始时间 YYYY / YYYY-MM / YYYY-MM-DD 或 null", '
    '"time_end": "结束时间或 null"}], '
    '"participations": [{"person_name": "姓名", "project_name": "项目名", '
    '"explicitness": "listed_members|stated_participation|implied|vague", '
    '"sufficiency": "detailed_role|role_stated|mentioned|minimal"}]}。'
    "participations 的 person_name/project_name 必须与 persons/projects 中的条目对应；"
    "explicitness：明确列出参与成员=listed_members、明确声明参与=stated_participation、"
    "可推断=implied、模糊提及=vague；sufficiency：角色详情=detailed_role、"
    "角色明确=role_stated、仅提及=mentioned、最少信息=minimal。"
    "文本无任何人员/项目参与信号时 no_signal=true 且三个数组为空。"
)


@dataclass
class NewsPerson:
    name: str
    org: str | None = None
    role: str | None = None
    person_id: int | None = None  # 消歧结果（resolve 后填）
    identity: float = 0.9


@dataclass
class NewsProject:
    name: str
    project_type: str | None = None
    time_start: dt.date | None = None
    time_end: dt.date | None = None
    project_id: int | None = None  # upsert 结果


@dataclass
class Participation:
    person_name: str
    project_name: str
    explicitness: str
    sufficiency: str


@dataclass
class NewsExtraction:
    no_signal: bool = True
    persons: list[NewsPerson] = field(default_factory=list)
    projects: list[NewsProject] = field(default_factory=list)
    participations: list[Participation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 抽取来源上下文（置信度分档输入，T11 使用）
    src_confidence: float = 0.6
    accessibility: float = ACCESS_SUMMARY
    source_desc: str = "资讯"


def _clean_str(v: object) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def normalize_project_name(name: str) -> str:
    """项目名归一：空白折叠 + 小写（拉丁），同名项目据此归并。"""
    return " ".join(name.split()).lower()


def parse_project_date(v: object) -> dt.date | None:
    """'YYYY-MM-DD' / 'YYYY-MM' / 'YYYY' → date（月初/年初对齐）。"""
    s = _clean_str(v)
    if not s:
        return None
    m = re.fullmatch(r"(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?", s)
    if not m:
        return None
    y, mo, d = (int(g) if g else None for g in m.groups())
    try:
        return dt.date(y, mo or 1, d or 1)
    except ValueError:
        return None


def validate_news_extraction(data: dict) -> NewsExtraction:
    """顶层 JSON → NewsExtraction。schema 破损抛 ValueError；逐条容错跳过。"""
    if not isinstance(data, dict):
        raise ValueError("顶层不是对象")

    def _items(key: str) -> list:
        v = data.get(key)
        if not isinstance(v, list):
            raise ValueError(f"缺少 {key} 数组")
        return v

    ext = NewsExtraction(no_signal=bool(data.get("no_signal")))
    for i, item in enumerate(_items("persons")):
        if not isinstance(item, dict):
            ext.warnings.append(f"persons[{i}] 非对象，跳过")
            continue
        name = _clean_str(item.get("name"))
        if not name:
            ext.warnings.append(f"persons[{i}] 缺 name，跳过")
            continue
        ext.persons.append(NewsPerson(name=name, org=_clean_str(item.get("org")), role=_clean_str(item.get("role"))))
    for i, item in enumerate(_items("projects")):
        if not isinstance(item, dict):
            ext.warnings.append(f"projects[{i}] 非对象，跳过")
            continue
        name = _clean_str(item.get("name"))
        if not name:
            ext.warnings.append(f"projects[{i}] 缺 name，跳过")
            continue
        ext.projects.append(
            NewsProject(
                name=name,
                project_type=_clean_str(item.get("project_type")),
                time_start=parse_project_date(item.get("time_start")),
                time_end=parse_project_date(item.get("time_end")),
            )
        )
    for i, item in enumerate(_items("participations")):
        if not isinstance(item, dict):
            ext.warnings.append(f"participations[{i}] 非对象，跳过")
            continue
        pname = _clean_str(item.get("person_name"))
        prname = _clean_str(item.get("project_name"))
        expl = item.get("explicitness")
        suff = item.get("sufficiency")
        if not pname or not prname:
            ext.warnings.append(f"participations[{i}] 缺 person/project name，跳过")
            continue
        if expl not in EXPLICITNESS_SCORES or suff not in SUFFICIENCY_SCORES:
            ext.warnings.append(f"participations[{i}] 枚举非法（{expl!r}/{suff!r}），跳过")
            continue
        ext.participations.append(Participation(pname, prname, expl, suff))

    if not ext.no_signal and not ext.participations:
        # 有人员/项目但无参与对：不足以建关系，按无信号处理
        ext.no_signal = True
    return ext


# ---------- 实体入库 ----------


async def upsert_project(session: AsyncSession, np: NewsProject) -> None:
    """projects upsert（name_normalized 归并，RD-M2-6）：已有则只补空字段。"""
    norm = normalize_project_name(np.name)
    proj = (
        await session.execute(select(Project).where(Project.name_normalized == norm))
    ).scalar_one_or_none()
    if proj is None:
        proj = Project(name=np.name, name_normalized=norm)
        session.add(proj)
    # 补空不覆盖（后到的报道可能带更完整的时间/类型）
    if np.project_type and proj.project_type is None:
        proj.project_type = np.project_type
    if np.time_start and proj.time_start is None:
        proj.time_start = np.time_start
    if np.time_end and proj.time_end is None:
        proj.time_end = np.time_end
    await session.flush()
    np.project_id = proj.id


async def resolve_news_person(session: AsyncSession, np: NewsPerson) -> None:
    """资讯人员消歧：复用网页链路（org 作机构锚，强归并/打分/新建 0.9）。"""
    ext = PageExtraction(org_school=np.org)  # org 字符串任一层级均可参与强归并
    member = Member(name=np.name)
    await resolve_member(session, member, ext)
    np.person_id = member.person_id
    np.identity = member.identity


# ---------- GLM 抽取 ----------


async def extract_news_item(session: AsyncSession, glm: GLMClient, item: NewsItem) -> NewsExtraction:
    """RSS 条目：标题+摘要输入（accessibility=仅摘要 0.5）。GLM 异常向上抛。"""
    data = await glm.complete_json(
        session,
        system=NEWS_EXTRACT_SYSTEM,
        user=f"标题：{item.title}\n摘要：{item.summary or ''}",
        job_type="news_extract",
        job_class=JobClass.extract,
        max_tokens=2000,
    )
    ext = validate_news_extraction(data)
    ext.accessibility = ACCESS_SUMMARY
    ext.src_confidence = await rss_source_confidence(item.source_id)
    ext.source_desc = f"RSS 资讯（{item.source_id}）"
    return ext


async def extract_news_page(session: AsyncSession, glm: GLMClient, page: WebPage) -> NewsExtraction:
    """新闻公示页（RD-M2-11）：content_text 输入，src=1.0 / accessibility=全文 0.8。"""
    data = await glm.complete_json(
        session,
        system=NEWS_EXTRACT_SYSTEM,
        user=(page.content_text or "")[:MAX_CHARS],
        job_type="news_extract",
        job_class=JobClass.extract,
        max_tokens=2000,
    )
    ext = validate_news_extraction(data)
    ext.accessibility = ACCESS_FULLTEXT
    ext.src_confidence = SRC_NEWS_PAGE
    ext.source_desc = "高校官网新闻页"
    return ext


async def rss_source_confidence(source_id: str) -> float:
    """RSS 源 tier → 数据源可信度（known_media 0.8 / other 0.6；未知源 0.6）。"""
    for src in load_sources().rss:
        if src.id == source_id:
            return src.confidence
    return RssSource(id="_", url="https://_", tier="other").confidence


async def sync_news_page_item(session: AsyncSession, page: WebPage) -> NewsItem:
    """新闻公示页 ↔ NewsItem 同步（C8：项目关系证据锚点统一为 news_items）。"""
    item = (
        await session.execute(select(NewsItem).where(NewsItem.url == page.url))
    ).scalar_one_or_none()
    if item is None:
        item = NewsItem(
            source_id=page.seed_id,
            url=page.url,
            title=page.title or page.url,
            summary=(page.content_text or "")[:2000],
            published_at=page.fetched_at or dt.datetime.now(dt.timezone.utc),
            rss_entry={
                "source": RSS_ENTRY_WEBPAGE,
                "seed_id": page.seed_id,
                "title": page.title,
                "link": page.url,
                "summary": (page.content_text or "")[:500],
            },
            status="pending_screen",
        )
        session.add(item)
        await session.flush()
    return item
