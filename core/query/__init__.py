"""Query parsing, hybrid retrieval, fusion, rerank (PLAN.md §7)."""

from core.query.filters import QueryFilters, filter_clause, filters_from_dict
from core.query.fusion import rrf_fuse
from core.query.hybrid import hybrid_search
from core.query.parser import ParsedQuery, parse_query
from core.query.rerank import NoOpReranker, get_reranker, should_rerank

__all__ = [
    "QueryFilters",
    "filter_clause",
    "filters_from_dict",
    "rrf_fuse",
    "hybrid_search",
    "ParsedQuery",
    "parse_query",
    "NoOpReranker",
    "get_reranker",
    "should_rerank",
]
