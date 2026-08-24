"""T10 单测：三源置信度 / 机构归一化合并 / OpenAlex 匹配回填（FR-2.4，RD-2）。"""
from __future__ import annotations

import json

import httpx
import respx
from sqlalchemy import select

from app.models import Organization, Paper, PaperAuthor, Person, PersonOrg
from app.services.openalex import (
    OpenAlexClient,
    enrich_papers,
    normalize_org,
    sync_person_org,
    upsert_organization,
)


async def _mk_person_with_authors(
    db_session, *, glm_affil: str | None, openalex: dict | None
) -> Person:
    paper = Paper(
        arxiv_id="2608.200",
        title="Some paper",
        authors_raw=["Wei Zhang"],
        categories=["cs.AI"],
        status="extracted",
    )
    db_session.add(paper)
    await db_session.flush()
    person = Person(name="Wei Zhang", name_normalized="weizhang")
    db_session.add(person)
    await db_session.flush()
    pa = PaperAuthor(
        paper_id=paper.id,
        author_seq=0,
        raw_name="Wei Zhang",
        person_id=person.id,
        name_confidence=1.0,
        affiliation=glm_affil,
        openalex_id=(openalex or {}).get("openalex_id"),
        org_source=(openalex or {}).get("org_source"),
    )
    db_session.add(pa)
    await db_session.flush()
    db_session.add(PaperAuthor(paper_id=paper.id, author_seq=1, raw_name="Other"))
    return person


async def test_sync_glm_source_confidence_1(db_session) -> None:
    """GLM 有机构 → org_confidence=1.0，source=glm。"""
    person = await _mk_person_with_authors(
        db_session, glm_affil="Peking University", openalex=None
    )
    po = await sync_person_org(db_session, person.id)
    assert po is not None
    assert float(po.org_confidence) == 1.0 and po.source == "glm"
    org = await db_session.get(Organization, po.org_id)
    assert org.name == "Peking University"


async def test_sync_openalex_source_confidence_08_and_id_writeback(db_session) -> None:
    """无 GLM 机构、有 OpenAlex 匹配 → 0.8，且回写 person.openalex_id。"""
    person = await _mk_person_with_authors(
        db_session,
        glm_affil=None,
        openalex={"openalex_id": "A507", "org_source": "openalex"},
    )
    # OpenAlex 行需带机构才构成 0.8 路径
    pa = (
        await db_session.execute(select(PaperAuthor).where(PaperAuthor.raw_name == "Wei Zhang"))
    ).scalar_one()
    pa.affiliation = "Tsinghua University"

    po = await sync_person_org(db_session, person.id)
    assert po is not None
    assert float(po.org_confidence) == 0.8 and po.source == "openalex"
    await db_session.refresh(person)
    assert person.openalex_id == "A507"


async def test_sync_no_source_no_row(db_session) -> None:
    """均无机构 → 不写 person_org（0.4 兜底语义），不抛错。"""
    person = await _mk_person_with_authors(db_session, glm_affil=None, openalex=None)
    po = await sync_person_org(db_session, person.id)
    assert po is None
    rows = (await db_session.execute(select(PersonOrg))).scalars().all()
    assert rows == []


def test_normalize_org_variants() -> None:
    assert normalize_org("Tsinghua University") == normalize_org("Tsinghua Univ.")
    assert normalize_org("Tsinghua University") == "tsinghua"
    assert normalize_org("Peking University") != normalize_org("Tsinghua University")


async def test_upsert_organization_merges(db_session) -> None:
    """"Tsinghua University" 先入库，再 upsert "Tsinghua Univ." → 同一行。"""
    org1 = await upsert_organization(db_session, "Tsinghua University")
    org2 = await upsert_organization(db_session, "Tsinghua Univ.")
    assert org1.id == org2.id
    orgs = (await db_session.execute(select(Organization))).scalars().all()
    assert len(orgs) == 1
    assert orgs[0].name == "Tsinghua University"  # 保留首见原始名


@respx.mock
async def test_enrich_papers_fills_openalex(db_session) -> None:
    """mock OpenAlex：DOI 命中 → 缺机构的作者行回填 openalex_id/机构/org_source。"""
    paper = Paper(
        arxiv_id="2608.201v2",
        title="Intent-Based Network Slicing",
        authors_raw=["Wei Zhang", "Li Wang"],
        categories=["cs.NI"],
        status="extracted",
    )
    db_session.add(paper)
    await db_session.flush()
    db_session.add(PaperAuthor(paper_id=paper.id, author_seq=0, raw_name="Wei Zhang",
                               affiliation="Peking University"))  # GLM 已有 → 不动
    db_session.add(PaperAuthor(paper_id=paper.id, author_seq=1, raw_name="Li Wang"))  # 待补
    await db_session.flush()

    authorships = [
        {"author": {"id": "https://openalex.org/A111", "display_name": "Wei Zhang"},
         "institutions": [{"display_name": "Peking University"}]},
        {"author": {"id": "https://openalex.org/A222", "display_name": "Li Wang"},
         "raw_affiliation_strings": ["Tsinghua University"]},
    ]
    respx.get("https://api.openalex.org/works/https://doi.org/10.48550/arxiv.2608.201").mock(
        return_value=httpx.Response(200, text=json.dumps({"authorships": authorships}))
    )

    http = httpx.AsyncClient()
    enriched = await enrich_papers(db_session, http=http)
    await http.aclose()

    assert enriched == 1
    rows = (
        await db_session.execute(select(PaperAuthor).order_by(PaperAuthor.author_seq))
    ).scalars().all()
    assert rows[0].openalex_id is None and rows[0].org_source is None  # GLM 行未动
    assert rows[1].openalex_id == "A222"
    assert rows[1].affiliation == "Tsinghua University"
    assert rows[1].org_source == "openalex"


@respx.mock
async def test_lookup_paper_title_fallback(db_session) -> None:
    """DOI 404 → 标题搜索命中同题论文。"""
    respx.get("https://api.openalex.org/works/https://doi.org/10.48550/arxiv.2608.202").mock(
        return_value=httpx.Response(404)
    )
    results = {"results": [
        {"display_name": "Totally Different Paper", "authorships": []},
        {"display_name": "Exact Title Match", "authorships": [
            {"author": {"id": "https://openalex.org/A333", "display_name": "Ann Lee"},
             "institutions": []}]},
    ]}
    respx.get("https://api.openalex.org/works").mock(
        return_value=httpx.Response(200, text=json.dumps(results))
    )

    async with httpx.AsyncClient() as http:
        client = OpenAlexClient(http=http)
        authorships = await client.lookup_paper("2608.202v1", "exact  title    match")
    assert authorships is not None
    assert authorships[0]["author"]["id"].endswith("A333")
