# Test Case: BGE Ranking

## Input
```
搜索 "external wall insulation non-destructive testing" 的论文，Q1/Q2
```

## Expected
1. Search API fetches papers
2. Client-side filter: exclude conference/preprint
3. JCR quartile lookup + filter Q1/Q2
4. BGE ranking:
   - Query split into keywords: ["external", "wall", "insulation", "non", "destructive", "testing"]
   - Each keyword encoded separately
   - Each paper score = **mean** of keyword-title similarities (v2.0；此前为 sum)
   - Papers sorted by score descending (NOT filtered)
5. Papers have relevance_score field
6. Output sorted results

## Verification
- [ ] All papers preserved (none deleted by ranking)
- [ ] Papers have relevance_score field
- [ ] Papers sorted by relevance_score descending
- [ ] Top-ranked papers are more relevant to the query
- [ ] BGE model loads correctly (**bge-base-en-v1.5**, 768 维，本地缓存，可离线加载)
