# Test Case: Year Filter

## Input
```
搜索 "defect detection" 的论文，2020-2026年
```

## Expected
1. Search API with `year=2020-2026` parameter (API-level filter)
2. Client-side also validates year range as backup

## Verification
- [ ] All papers have year between 2020 and 2026
- [ ] API-level filter reduces unnecessary data transfer
