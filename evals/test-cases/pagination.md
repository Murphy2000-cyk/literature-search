# Test Case: Pagination

## Input
```
搜索 "machine learning" 的论文，limit 250
```

## Expected
1. search_paginated() called with total_limit=250
2. 3 API calls: offset=0 (100), offset=100 (100), offset=200 (50)
3. Results deduplicated by paper_id
4. Total unique papers <= 250

## Verification
- [ ] Multiple API calls made with increasing offset
- [ ] No duplicate papers (by paper_id)
- [ ] Total count <= requested limit
- [ ] Progress messages printed for each page
