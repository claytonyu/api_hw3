# 15-113 HW3 - Using APIs with Generative AI

## Overview
This homework assignment generates a citation graph using [Semantic Scholar](https://www.semanticscholar.org/), showing selected references and citations of a paper. The search page GETs the `/paper/search` endpoint to get lists of relevant papers for a given search term. Once a seed paper is selected, the app PUSHes the `/paper/batch` endpoint to get bulk amounts of paper data at a time. It uses a BFS-like algorithm to sequentially find papers that are farther and farther related to the seed paper. Graphics were accomplished through PyQt. 

## API Specifics
Most endpoints include a `'fields'` parameter, which accept a list of comma-separated properties that the API will return along with the pper. For example, if `'fields'` is set to `'referenceCount,citationCount,title'`, then the API would return:
```json
[
  {
    "paperId": "649def34f8be52c8b66281af98ae884c09aef38b",
    "title": "Construction of the Literature Graph in Semantic Scholar",
    "referenceCount": 27,
    "citationCount": 299
  },
  {
    "paperId": "f712fab0d58ae6492e3cdfc1933dae103ec12d5d",
    "title": "Reinfection and low cross-immunity as drivers of epidemic resurgence under high seroprevalence: a model-based approach with application to Amazonas, Brazil",
    "referenceCount": 13,
    "citationCount": 0
  }
]
```

## API Key
The API does technically work without a key, but severe rate limiting applies (it was not feasible to even run the search function without a key, which only requires 1 call). An API Key can be requested on the [Semantic Scholar API Website](https://www.semanticscholar.org/product/api). It took around ~15 minutes for access to get approved on a university-affiliated email.
I would be willing to email TAs the API key drectly if that would help grade my assignment. 

## Requirements
This was tested on Python 3.14. Required modules:
- PyQt6
- requests
- dotenv