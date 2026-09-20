# Test Case: Citation Expansion

## Input
```
从论文 "abc123def456" 扩展引用网络
```

## Expected
1. get_paper_details() called with paper_id
2. Extract citations and references lists
3. Return expanded papers (excluding seed paper)

## Verification
- [ ] Seed paper not in results
- [ ] Citations and/or references retrieved
- [ ] Results are PaperInfo objects with standard fields
- [ ] limit_per_paper respected
