# Test Case: Basic Search

## Input
```
搜索 "knowledge graph construction" 的论文
```

## Expected
1. Search API with default parameters (limit=50)
2. No filters applied, all papers pass
3. Client-side filter: exclude conference/preprint (using publicationTypes)
4. Batch API fetches abstracts
5. Save initial data (txt + json) and filtered results (txt + json)
6. Output file path to user

## Verification
- [ ] 2 files saved (txt + json)
- [ ] JSON contains paper_id and doi fields
- [ ] Papers have is_journal, is_conference, is_preprint fields
- [ ] No conference papers or preprints in filtered results
