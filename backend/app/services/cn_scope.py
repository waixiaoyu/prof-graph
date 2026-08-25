"""M1 数据范围约束：论文是否含有中国学者（2026-08-31 补充约束）。

M1 只治理"含有中国学者"的论文——含其在国外机构任职的情形；
这些论文上的外国合作者及其机构作为"相关外国机构/学者"一并保留。

判定启发式（GLM 细筛判定待周预算恢复后叠加，本模块先行兜底）：
- 机构信号：任一作者署名机构命中中国机构关键词（归一化子串）或含 CJK 字符
- 姓名信号：任一作者姓名的首/末 token 是常见中文姓氏拼音
两信号任一命中即视为含中国学者（宁多勿漏，与 FR-1.2 口径一致）。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Paper, PaperAuthor
from app.utils.names import normalize_name

log = logging.getLogger("prof-graph.cn_scope")

# 归一化（去空格小写）后子串匹配；条目均 ≥4 字符以避免误配（如 cas 会命中 cast）
CN_ORG_KEYWORDS = (
    "china", "chineseacademy", "tsinghua", "peking", "fudan",
    "shanghaijiaotong", "jiaotonguniversity", "zhejiang", "hangzhou",
    "universityofscienceandtechnologyofchina", "ustc", "hefei",
    "harbin", "huazhong", "beihang", "beijinginstitute", "beijing",
    "tongji", "nankai", "xidian", "sunyatsen", "nanjing", "southeastuniversity",
    "northwesternpolytechnical", "universityofelectronicscience", "uestc",
    "wuhan", "changsha", "chongqing", "sichuan", "chengdu", "tianjin",
    "dalian", "jilin", "changchun", "xiamen", "shandong", "qingdao",
    "hunan", "henan", "hebei", "shanxi", "shenyang", "taiyuan",
    "yunnan", "kunming", "lanzhou", "guizhou", "guangxi", "nanning",
    "nanchang", "zhengzhou", "jiangsu", "jiangnan", "soochow", "suzhou",
    "shenzhen", "guangzhou", "guangdong", "shanghai", "xianjiaotong",
    "xian", "anhui", "fujian", "fuzhou", "urumqi", "xinjiang",
    "innermongolia", "hohhot", "ningbo", "wenzhou",
)

_CJK = re.compile(r"[\u4e00-\u9fff]")

# 常见中文姓氏拼音（含复姓；标准拼写与韩/越罗马化形式不同，重合有限）
CN_PINYIN_SURNAMES = frozenset((
    "wang", "zhang", "liu", "chen", "yang", "huang", "zhao", "wu", "zhou",
    "xu", "sun", "ma", "zhu", "hu", "guo", "he", "lin", "luo", "zheng",
    "liang", "xie", "song", "tang", "deng", "feng", "han", "cao", "zeng",
    "peng", "xiao", "cai", "pan", "tian", "dong", "yuan", "yu", "ye",
    "du", "su", "wei", "cheng", "lu", "ding", "ren", "shen", "yao",
    "jiang", "cui", "zhong", "tan", "fan", "shi", "liao", "jia", "xia",
    "fu", "fang", "bai", "zou", "meng", "xiong", "qin", "qiu", "hou",
    "shao", "chang", "qian", "duan", "ran", "yan", "dai", "mo", "kong",
    "xiang", "diao", "gong", "zhan", "you", "lai", "hong", "kan", "pu",
    "shang", "mi", "qi", "gui", "man", "geng", "ying", "qu", "rong",
    "xing", "ge", "ni", "ji", "lian", "lao", "nie", "xian", "shan",
    "kuang", "wen", "xue", "sheng", "le", "sang", "yi", "heng", "she",
    "jian", "cha", "sima", "ouyang", "shangguan", "zhuge",
    "xiahou", "huangfu", "changsun", "murong",
))


def is_cn_affiliation(affiliation: str | None) -> bool:
    """机构名是否为中国机构：CJK 字符或关键词命中。"""
    if not affiliation:
        return False
    if _CJK.search(affiliation):
        return True
    norm = normalize_name(affiliation)
    return any(kw in norm for kw in CN_ORG_KEYWORDS)


def is_cn_name(raw_name: str | None) -> bool:
    """姓名是否疑似中国学者：首/末 token（处理 'Zhang, Wei' 两种序）为中文姓氏拼音。"""
    if not raw_name:
        return False
    tokens = [normalize_name(t) for t in re.split(r"[,\s]+", raw_name) if t.strip()]
    if not tokens:
        return False
    return tokens[0] in CN_PINYIN_SURNAMES or tokens[-1] in CN_PINYIN_SURNAMES


def classify_paper(rows: list[PaperAuthor]) -> bool:
    """论文级判定：任一作者命中机构或姓名信号。"""
    return any(
        is_cn_affiliation(pa.affiliation) or is_cn_name(pa.raw_name)
        for pa in rows
    )


async def flag_papers(session: AsyncSession) -> dict:
    """重算已抽取论文的中国学者标记（幂等；每批重扫 False 的行，
    OpenAlex 后补的机构可提升召回）。返回统计。"""
    papers = (
        await session.execute(
            select(Paper).where(
                Paper.status == "extracted", Paper.has_cn_scholar.is_(False)
            )
        )
    ).scalars().all()

    flagged = 0
    for paper in papers:
        rows = (
            await session.execute(
                select(PaperAuthor).where(PaperAuthor.paper_id == paper.id)
            )
        ).scalars().all()
        if classify_paper(rows):
            paper.has_cn_scholar = True
            flagged += 1
    await session.commit()
    stats = {"scanned": len(papers), "flagged": flagged}
    if flagged:
        log.info("中国学者范围标记：%s", stats)
    return stats
