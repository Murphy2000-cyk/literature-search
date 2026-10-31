# Test Case: Paper Type Filter

## Input
```
搜索 "non-destructive testing" 的论文，仅期刊论文
```

## Expected
1. Search API with `publicationTypes=JournalArticle,Review`
2. Client-side filter: exclude papers where is_conference=True or is_preprint=True
3. Preprint detection: check externalIds for ArXiv + venue name keywords

## Verification
- [ ] No conference papers in results (based on publicationTypes, not venue keywords)
- [ ] No preprints in results (ArXiv, SSRN, etc.)
- [ ] All remaining papers have is_journal=True
