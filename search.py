"""
Sample 01 - Finding papers (the entry points into the graph).

Three ways to turn a search term / partial string / known paper id into
paper data you can then expand into a citation graph. Each function returns
its result (rather than printing) so it can be reused by downstream code;
the __main__ block below is just a demo of calling them.

Run:  python3 search.py
"""

from __future__ import annotations

import s2client as s2


def relevance_search(query: str, min_citation_count: int | None = None,
                      year_range: str | None = None, limit: int = 15, offset: int = 0) -> dict:
    """
    /paper/search  --  AI-ranked relevance search. Best default for "a search term".

    Args:
      query               plain text query, no boolean syntax here
      min_citation_count  only return papers with >= N citations (optional)
      year_range          publication year range, e.g. "2019", "2016-2020",
                           "2010-", "-2015" (optional)
      limit                max results to return (<= 100, default 15)
      offset              Offset of results to read

    Returns the raw response dict: {total, offset, next, data: [...]}
    where each item in `data` has paperId plus the requested `fields`.
    """
    result = s2.get(
        "/paper/search",
        {
            "query": query,
            "fields": "title,year,citationCount,referenceCount,authors,fieldsOfStudy",
            "limit": limit,
            "offset": offset,
            "year": year_range,
            "minCitationCount": min_citation_count,
        },
    )
    return result


def autocomplete(query: str) -> dict:
    """
    /paper/autocomplete  --  lightweight title suggestions for a partial string.
    Only param is `query`. Returns id + title + authorsYear. Great for a UI.

    Returns the raw response dict: {matches: [{id, title, authorsYear}, ...]}
    """
    result = s2.get("/paper/autocomplete", {"query": query})
    return result


def recommendations(paper_id: str, limit: int = 3) -> dict:
    """
    Recommendations API (separate service):
      GET /recommendations/v1/papers/forpaper/{id}   -- similar to one paper

    Not citations, but "papers you might also want" - great for expanding a
    graph beyond direct citation links.

    Args:
      paper_id  the paper to find similar papers for (any accepted id format)
      limit     max recommended papers to return (default 3)

    Returns the raw response dict: {recommendedPapers: [...]}
    """
    result = s2.get(
        f"/papers/forpaper/{paper_id}",
        {"fields": "title,year,citationCount", "limit": limit},
        base=s2.REC_BASE,
    )
    return result


if __name__ == "__main__":
    search_result = relevance_search("graph neural networks",
                                      min_citation_count=100,
                                      year_range="2018-2022")
    s2.show("Relevance search: 'graph neural networks' (>=100 cites, 2018-2022)",
            {"total": search_result.get("total"),
             "returned": len(search_result.get("data", [])),
             "data": search_result.get("data")})

    autocomplete_result = autocomplete("attention is all you")
    s2.show("Autocomplete for 'attention is all you'", autocomplete_result)

    rec_result = recommendations("ARXIV:1706.03762")
    s2.show("Recommended papers similar to ARXIV:1706.03762",
            {"returned": len(rec_result.get("recommendedPapers", [])),
             "recommendedPapers": rec_result.get("recommendedPapers")})
